# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A set of functions related to the ability to parse the XML CVE database,
extract details of known CVE entries, and ensure that all of the known
CVE's are fully registered in Launchpad."""

import gzip
import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import defusedxml.ElementTree as ElementTree
import requests
import six
from zope.component import getUtility
from zope.event import notify
from zope.interface import implementer
from zope.lifecycleevent import ObjectModifiedEvent

from lp.archiveuploader.utils import SafeTarExtractError, safe_extract_zip
from lp.bugs.interfaces.cve import CveStatus, ICveSet
from lp.services.config import config
from lp.services.log.logger import LaunchpadLogger
from lp.services.looptuner import ITunableLoop, LoopTuner
from lp.services.scripts.base import (
    LaunchpadCronScript,
    LaunchpadScriptFailure,
)
from lp.services.timeout import override_timeout, urlfetch

CVEDB_NS = "{http://cve.mitre.org/cve/downloads/1.0}"

CVE_STATE_TO_STATUS = {
    "PUBLISHED": CveStatus.ENTRY,
    "REJECTED": CveStatus.REJECTED,
}


def getText(elem):
    """Get the text content of the given element"""
    text = six.ensure_text(elem.text or "")
    for e in elem:
        text += getText(e)
        if e.tail:
            text += six.ensure_text(e.tail)
    return text.strip()


def handle_references(cve_node, cve, log):
    """Handle the references on the given CVE xml DOM.

    This function is passed an XML dom representing a CVE, and a CVE
    database object. It looks for Refs in the XML data structure and ensures
    that those are correctly represented in the database.

    It will try to find a relevant reference, and if so, update it. If
    not, it will create a new reference.  Finally, it removes any old
    references that it no longer sees in the official CVE database.
    It will return True or False, indicating whether or not the cve was
    modified in the process.
    """
    modified = False
    # we make a copy of the references list because we will be removing
    # items from it, to see what's left over
    old_references = set(cve.references)
    new_references = set()

    # work through the refs in the xml dump
    for ref_node in cve_node.findall(".//%sref" % CVEDB_NS):
        refsrc = six.ensure_text(ref_node.get("source"))
        refurl = ref_node.get("url")
        if refurl is not None:
            refurl = six.ensure_text(refurl)
        reftxt = getText(ref_node)
        # compare it to each of the known references
        was_there_previously = False
        for ref in old_references:
            if (
                ref.source == refsrc
                and ref.url == refurl
                and ref.content == reftxt
            ):
                # we have found a match, remove it from the old list
                was_there_previously = True
                new_references.add(ref)
                break
        if not was_there_previously:
            log.info(
                "Creating new %s reference for %s" % (refsrc, cve.sequence)
            )
            ref = cve.createReference(refsrc, reftxt, url=refurl)
            new_references.add(ref)
            modified = True
    # now, if there are any refs in old_references that are not in
    # new_references, then we need to get rid of them
    for ref in sorted(
        old_references, key=lambda a: (a.source, a.content, a.url)
    ):
        if ref not in new_references:
            log.info(
                "Removing %s reference for %s" % (ref.source, cve.sequence)
            )
            cve.removeReference(ref)
            modified = True

    return modified


def update_one_cve(cve_node, log):
    """Update the state of a single CVE item."""
    # get the sequence number
    sequence = six.ensure_text(cve_node.get("seq"))
    # establish its status
    status = six.ensure_text(cve_node.get("type"))
    # get the description
    description = getText(cve_node.find(CVEDB_NS + "desc"))
    if not description:
        log.debug("No description for CVE-%s" % sequence)
    if status == "CAN":
        new_status = CveStatus.CANDIDATE
    elif status == "CVE":
        new_status = CveStatus.ENTRY
    else:
        log.error("Unknown status %s for CVE-%s" % (status, sequence))
        return
    # find or create the CVE entry in the db
    cveset = getUtility(ICveSet)
    cve = cveset[sequence]
    if cve is None:
        cve = cveset.new(sequence, description, new_status)
        log.info("CVE-%s created" % sequence)
    # update the CVE if needed
    modified = False
    if cve.status != new_status:
        log.info(
            "CVE-%s changed from %s to %s"
            % (cve.sequence, cve.status.title, new_status.title)
        )
        cve.status = new_status
        modified = True
    if cve.description != description:
        log.info("CVE-%s updated description" % cve.sequence)
        cve.description = description
        modified = True
    # make sure we have copies of all the references.
    if handle_references(cve_node, cve, log):
        modified = True
    # trigger an event if modified
    if modified:
        notify(ObjectModifiedEvent(cve))
    return


def retry_on_failure(
    func: Callable, max_retries: int, delay: int, logger: LaunchpadLogger
):
    """
    Retry a function on failure with a fixed delay between retries.

    :param func: The function to call.
    :param max_retries: Maximum number of retries.
    :param delay: The fixed delay (in seconds) between retries.
    :param logger: LaunchpadLogger used to print retry messages.
    :return: The result of the function if successful.
    """
    attempt = 0
    while attempt < max_retries:
        try:
            return func()
        except LaunchpadScriptFailure as e:
            attempt += 1
            if attempt < max_retries:
                logger.info(
                    f"Retrying... ({attempt}/{max_retries}) - "
                    f"waiting for {delay} seconds..."
                )
                time.sleep(delay)
            else:
                raise e


@implementer(ITunableLoop)
class CveUpdaterTunableLoop:
    """An `ITunableLoop` for updating CVEs."""

    total_updated = 0

    def __init__(self, cves, transaction, logger, offset=0):
        self.cves = cves
        self.transaction = transaction
        self.logger = logger
        self.offset = offset
        self.total_updated = 0

    def isDone(self):
        """See `ITunableLoop`."""
        return self.offset is None

    def __call__(self, chunk_size):
        """Retrieve a batch of CVEs and update them.

        See `ITunableLoop`.
        """
        chunk_size = int(chunk_size)

        self.logger.debug("More %d" % chunk_size)

        start = self.offset
        end = self.offset + chunk_size

        self.transaction.begin()

        cve_batch = self.cves[start:end]
        self.offset = None
        for cve in cve_batch:
            start += 1
            self.offset = start
            update_one_cve(cve, self.logger)
            self.total_updated += 1

        self.logger.debug("Committing.")
        self.transaction.commit()


class CVEUpdater(LaunchpadCronScript):
    max_retries = 6

    def add_my_options(self):
        """Parse command line arguments."""
        self.parser.add_option(
            "-f",
            "--cvefile",
            help="An XML file containing the CVE database.",
        )
        self.parser.add_option(
            "-u",
            "--cveurl",
            default=config.cveupdater.cve_db_url,
            help="The URL for the XML CVE database.",
        )
        self.parser.add_option(
            "--baselinecvedir",
            help="Directory containing CVE JSON files in year/group structure",
        )
        self.parser.add_option(
            "--deltacvedir",
            help="Directory containing delta CVE JSON files (flat structure)",
        )
        self.parser.add_option(
            "-b",
            "--baseline",
            action="store_true",
            default=False,
            help="Download baseline full CVE data from GitHub releases",
        )
        self.parser.add_option(
            "-d",
            "--delta",
            action="store_true",
            default=False,
            help="Download and process hourly delta CVE data from GitHub "
            "releases",
        )

    def construct_github_url(self, delta=False):
        """Construct the GitHub release URL for CVE data.

        :param delta: If True, construct URL for hourly delta, otherwise for
            daily baseline
        :return: tuple of (url, year)
        """
        now = datetime.now(timezone.utc)
        year, _, date_str = now.strftime("%Y-%m-%d").partition("-")
        date_str = f"{year}-{date_str}"
        hour = now.hour

        # Go back 24 hours to get yesterday's date
        yesterday = now - timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y-%m-%d")

        base_url = config.cveupdater.github_cve_url

        if delta:
            # If we are past midnight and we run a delta update, it most likely
            # means we want the delta for the previous day (at_end_of_day).
            # A "real" delta update at midnight is empty since we just formed
            # a new baseline for the day.
            if hour == 0:
                release_tag = f"cve_{yesterday_str}_at_end_of_day"
                filename = f"{yesterday_str}_delta_CVEs_at_end_of_day.zip"
            else:
                # For all hours, use the standard hourly format
                hour_str = f"{hour:02d}00"
                release_tag = f"cve_{date_str}_{hour_str}Z"
                filename = f"{date_str}_delta_CVEs_at_{hour_str}Z.zip"
        else:
            release_tag = f"cve_{date_str}_0000Z"
            filename = f"{yesterday_str}_all_CVEs_at_midnight.zip.zip"

        # Construct the full URL
        url = urljoin(base_url, f"{release_tag}/{filename}")
        return url

    def process_delta_directory(self, delta_dir):
        """Process a directory containing delta CVE JSON files.

        Expected structure:
        deltaCves/
            CVE-XXXX-XXXX.json
            CVE-XXXX-YYYY.json
            ...

        :param delta_dir: Path to the directory containing delta CVE files
        :return: tuple of (processed_count, error_count)
        """
        total_processed = 0
        total_errors = 0

        delta_path = Path(delta_dir)
        if not delta_path.exists():
            raise LaunchpadScriptFailure(
                f"Delta directory not found: {delta_dir}"
            )

        # process each CVE JSON file in the delta directory
        for cve_file in sorted(delta_path.glob("CVE-*.json")):
            try:
                with open(cve_file) as f:
                    import json

                    cve_data = json.load(f)

                self.logger.debug(f"Processing delta {cve_file.name}")
                self.processCVEJSON(cve_data)
                total_processed += 1

                # commit after each CVE to avoid large transactions
                self.txn.commit()

            except (OSError, json.JSONDecodeError) as e:
                self.logger.error(
                    f"Error processing delta {cve_file}: {str(e)}"
                )
                total_errors += 1
                continue

            if total_processed % 10 == 0:
                self.logger.info(
                    f"Processed {total_processed} delta CVE files..."
                )

        return total_processed, total_errors

    def extract_github_zip(self, zip_content, delta=False):
        """Extract the GitHub ZIP file to a temporary directory.

        :param zip_content: The downloaded ZIP file content
        :param delta: If True, expect delta structure, otherwise baseline
            structure
        :return: Path to the extracted directory containing CVE files
        """
        import shutil
        import tempfile

        # create a temporary directory
        temp_dir = tempfile.mkdtemp(prefix="cve_import_")

        try:
            # write outer zip content to a temporary file
            outer_zip_path = os.path.join(temp_dir, "downloaded.zip")
            with open(outer_zip_path, "wb") as f:
                f.write(zip_content)

            # extract the outer zip file (safe extraction: no path traversal)
            with zipfile.ZipFile(outer_zip_path) as outer_zf:
                if delta:
                    target_dir = os.path.join(temp_dir, "deltaCves")

                    # If there are no delta cves, we return an empty dir
                    if not outer_zf.namelist():
                        self.logger.info(
                            "Zip file is empty: there are no delta changes"
                        )
                        os.mkdir(target_dir)
                        return target_dir

                    # for delta, extract deltacves directory
                    members = [
                        m
                        for m in outer_zf.namelist()
                        if m.startswith("deltaCves/")
                    ]
                    safe_extract_zip(outer_zf, temp_dir, members=members)
                else:
                    # for baseline, handle nested zip structure
                    if "cves.zip" not in outer_zf.namelist():
                        raise LaunchpadScriptFailure(
                            "There is no item named 'cves.zip' in the archive"
                        )

                    safe_extract_zip(outer_zf, temp_dir, members=["cves.zip"])
                    inner_zip_path = os.path.join(temp_dir, "cves.zip")

                    with zipfile.ZipFile(inner_zip_path) as inner_zf:
                        safe_extract_zip(inner_zf, temp_dir)

                    os.unlink(inner_zip_path)
                    target_dir = os.path.join(temp_dir, "cves")

            os.unlink(outer_zip_path)

            if not os.path.exists(target_dir):
                raise LaunchpadScriptFailure(
                    f"Expected directory not found in ZIP: {target_dir}"
                )

            return target_dir

        except SafeTarExtractError as e:
            shutil.rmtree(temp_dir)
            raise LaunchpadScriptFailure(
                f"Unsafe ZIP content (path traversal or absolute path): {e}"
            )
        except Exception as e:
            # clean up on any error
            shutil.rmtree(temp_dir)
            raise LaunchpadScriptFailure(
                f"Failed to extract ZIP files: {str(e)}"
            )

    def process_json_directory(self, base_dir):
        """Process a directory of CVE JSON files organized by year and groups.

        Expected structure:
        base_dir/
            1999/
                0xxx/
                    CVE-1999-0001.json
                    ...
                1xxx/
                    CVE-1999-1001.json
                    ...
            2024/
                0xxx/
                    CVE-2024-0001.json
                    ...
                1xxx/
                    CVE-2024-1001.json
                    ...
                56xxx/
                    CVE-2024-56001.json
                    ...

        :param base_dir: Path to the base directory containing year folders
        """
        base_path = Path(base_dir)
        total_processed = 0
        total_errors = 0

        # process each year directory
        for year in sorted(base_path.glob("[0-9][0-9][0-9][0-9]")):
            self.logger.info(f"Processing year {year.name}...")

            # process each group directory (0xxx, 1xxx, etc)
            for group in sorted(year.glob("[0-9]*xxx")):
                self.logger.info(f"Processing group {group.name}...")

                # process each cve json file
                for cve_file in sorted(group.glob("CVE-*.json")):
                    try:
                        with open(cve_file) as f:
                            import json

                            cve_data = json.load(f)

                        self.logger.debug(f"Processing {cve_file.name}")
                        self.processCVEJSON(cve_data)
                        total_processed += 1

                        # commit after each cve to avoid large transactions
                        self.txn.commit()

                    except (OSError, json.JSONDecodeError) as e:
                        self.logger.error(
                            f"Error processing {cve_file}: {str(e)}"
                        )
                        total_errors += 1
                        continue

                    if total_processed % 100 == 0:
                        self.logger.info(
                            f"Processed {total_processed} CVE files..."
                        )

        return total_processed, total_errors

    def main(self):
        self.logger.info("Initializing...")

        # handle GitHub delta download case
        if self.options.delta:
            try:
                url = self.construct_github_url(delta=True)
                self.logger.info(
                    f"Downloading delta CVE data from GitHub: {url}"
                )

                # download the ZIP file
                # We need to retry as we don't know the exact time the github
                # release will be published
                response = retry_on_failure(
                    lambda: self.fetchCVEURL(url),
                    max_retries=self.max_retries,
                    delay=10 * 60,
                    logger=self.logger,
                )

                # extract to temporary directory
                temp_dir = self.extract_github_zip(response, delta=True)

                try:
                    # process the extracted directory
                    total_processed, total_errors = (
                        self.process_delta_directory(temp_dir)
                    )
                    self.logger.info(
                        f"Processed {total_processed} delta CVE files "
                        f"({total_errors} errors)"
                    )
                finally:
                    # clean up temporary directory
                    import shutil

                    shutil.rmtree(temp_dir)

                return

            except Exception as e:
                raise LaunchpadScriptFailure(
                    f"Error processing GitHub delta CVE data: {str(e)}"
                )

        # handle local delta directory case
        if self.options.deltacvedir is not None:
            try:
                start_time = time.time()
                total_processed, total_errors = self.process_delta_directory(
                    self.options.deltacvedir
                )
                finish_time = time.time()

                self.logger.info(
                    f"Processed {total_processed} delta CVE files "
                    f"({total_errors} errors) in "
                    f"{finish_time - start_time:.2f} seconds"
                )
                return

            except Exception as e:
                raise LaunchpadScriptFailure(
                    f"Error processing local delta CVE directory: {str(e)}"
                )

        # handle GitHub download case
        if self.options.baseline:
            try:
                url = self.construct_github_url()

                # download the ZIP file
                response = self.fetchCVEURL(url)

                # extract to temporary directory
                temp_dir = self.extract_github_zip(response)

                try:
                    # process the extracted directory
                    total_processed, total_errors = (
                        self.process_json_directory(temp_dir)
                    )
                    self.logger.info(
                        f"Processed {total_processed} CVE files "
                        f"({total_errors} errors)"
                    )
                finally:
                    # clean up temporary directory
                    import shutil

                    shutil.rmtree(temp_dir)

                return

            except Exception as e:
                raise LaunchpadScriptFailure(
                    f"Error processing GitHub CVE data: {str(e)}"
                )

        # handle local JSON directory case
        if self.options.baselinecvedir is not None:
            try:
                start_time = time.time()
                total_processed, total_errors = self.process_json_directory(
                    self.options.baselinecvedir
                )
                finish_time = time.time()

                self.logger.info(
                    f"Processed {total_processed} CVE files "
                    f"({total_errors} errors) in "
                    f"{finish_time - start_time:.2f} seconds"
                )
                return

            except Exception as e:
                raise LaunchpadScriptFailure(
                    f"Error processing JSON CVE directory: {str(e)}"
                )

        # existing XML handling
        if self.options.cvefile is not None:
            try:
                with open(self.options.cvefile) as f:
                    cve_db = f.read()
            except OSError:
                raise LaunchpadScriptFailure(
                    "Unable to open CVE database in %s" % self.options.cvefile
                )
        elif self.options.cveurl is not None:
            cve_db = self.fetchCVEURL(self.options.cveurl)
        else:
            raise LaunchpadScriptFailure("No CVE database file or URL given.")

        # start analysing the data
        start_time = time.time()
        self.logger.info("Processing CVE XML...")
        self.processCVEXML(cve_db)
        finish_time = time.time()
        self.logger.info(
            "%d seconds to update database." % (finish_time - start_time)
        )

    def fetchCVEURL(self, url):
        """Fetch CVE data from a URL, decompressing if necessary."""
        self.logger.info("Downloading CVE database from %s..." % url)
        try:
            with override_timeout(config.cveupdater.timeout):
                # command-line options are trusted, so allow file://
                # URLs to ease testing
                response = urlfetch(url, use_proxy=True, allow_file=True)

        except requests.HTTPError as e:
            raise LaunchpadScriptFailure(
                f"{e.response} at '{url}'. CVE database probably has not "
                "been published yet, cves will be imported in the next "
                "delta."
            )
        except requests.RequestException:
            raise LaunchpadScriptFailure(
                f"Unable to connect for CVE database {url}"
            )

        cve_db = response.content
        self.logger.info("%d bytes downloaded." % len(cve_db))
        # requests will normally decompress this automatically, but that
        # might not be the case if we're given a file:// URL to a gzipped
        # file.
        if cve_db[:2] == b"\037\213":  # gzip magic
            cve_db = gzip.GzipFile(fileobj=io.BytesIO(cve_db)).read()
        return cve_db

    def processCVEXML(self, cve_xml):
        """Process the CVE XML file.

        :param cve_xml: The CVE XML as a string.
        """
        dom = ElementTree.fromstring(cve_xml, forbid_dtd=True)
        items = dom.findall(CVEDB_NS + "item")
        if len(items) == 0:
            raise LaunchpadScriptFailure("No CVEs found in XML file.")
        self.logger.info("Updating database...")

        # we use Looptuner to control the ideal number of CVEs
        # processed in each transaction, during at least 2 seconds
        loop = CveUpdaterTunableLoop(items, self.txn, self.logger)
        loop_tuner = LoopTuner(loop, 2)
        loop_tuner.run()

    def processCVEJSON(self, cve_json):
        """Process the CVE JSON data.

        :param cve_json: The CVE JSON as a string or dict.
        """
        if isinstance(cve_json, str):
            import json

            data = json.loads(cve_json)
        else:
            data = cve_json

        if data.get("dataType") != "CVE_RECORD":
            raise LaunchpadScriptFailure("Invalid CVE record format")

        # process each CVE record
        cve_metadata = data.get("cveMetadata", {})
        status = CVE_STATE_TO_STATUS.get(
            cve_metadata.get("state", ""), CveStatus.ENTRY
        )
        containers = data.get("containers", {})
        cna_data = containers.get("cna", {})

        # get basic CVE information
        sequence = cve_metadata.get("cveId", "").replace("CVE-", "")

        # get description (required to be in English)
        description = None
        if status == CveStatus.REJECTED:
            description_key = "rejectedReasons"
        else:
            description_key = "descriptions"

        for desc in cna_data.get(description_key, []):
            if desc.get("lang", "").startswith("en"):
                description = desc.get("value")
                break

        if not description:
            self.logger.debug(f"No description for CVE-{sequence}")
            return

        # get affected information
        affected = cna_data.get("affected", {})

        # find or create CVE entry
        cveset = getUtility(ICveSet)
        cve = cveset[sequence]
        if cve is None:
            cve = cveset.new(sequence, description, status)
            self.logger.info(f"CVE-{sequence} created")

        # update CVE if needed
        modified = False
        if cve.description != description:
            self.logger.info(f"CVE-{sequence} updated description")
            cve.description = description
            modified = True

        # handle references
        if self._handle_json_references(cna_data.get("references", []), cve):
            modified = True

        # Build metadata dict
        metadata = None
        if affected:
            metadata = {"affected": affected}

        # If anything changed, update cve.metadata
        if metadata != cve.metadata:
            cve.metadata = metadata
            modified = True

        # Get CVSS metrics
        cvss_list = cna_data.get("metrics", None)
        # If anything changed, update cve.cvss
        if cvss_list != cve.cvss:
            cve.cvss = cvss_list
            modified = True

        if modified:
            notify(ObjectModifiedEvent(cve))

    def _handle_json_references(self, references, cve):
        """Handle references from the JSON format.

        :param references: List of reference objects from JSON
        :param cve: CVE database object
        :return: True if references were modified
        """
        modified = False
        old_references = set(cve.references)
        new_references = set()

        for ref in references:
            url = ref.get("url")
            source = "external"  # default source
            content = ref.get("name", url)

            # look for existing reference
            was_there_previously = False
            for old_ref in old_references:
                if (
                    old_ref.url == url
                    and old_ref.source == source
                    and old_ref.content == content
                ):
                    was_there_previously = True
                    new_references.add(old_ref)
                    break

            if not was_there_previously:
                self.logger.info(
                    f"Creating new {source} reference for {cve.sequence}"
                )
                ref_obj = cve.createReference(source, content, url=url)
                new_references.add(ref_obj)
                modified = True

        # remove old references not in new set
        for ref in sorted(
            old_references, key=lambda a: (a.source, a.content, a.url)
        ):
            if ref not in new_references:
                self.logger.info(
                    f"Removing {ref.source} reference for {cve.sequence}"
                )
                cve.removeReference(ref)
                modified = True

        return modified
