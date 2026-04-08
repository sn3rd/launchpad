# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Publisher script class."""

__all__ = [
    "PublishDistro",
    "ARCHIVEPUBLISHER_HISTORY_ENABLED",
    "PUBLISHER_DIRECT_INDEXES",
]

import os
from filecmp import dircmp
from optparse import OptionValueError
from pathlib import Path

# XXX 2023-04-21 jugmac00: prefer `import shutil` as `copy` is so common
# This will break a lot of tests which mocked this import, so let's do this
# together with removing mocks from e.g.
# `test_syncOVALDataFilesForSuite_oval_data_missing_in_destination`
from shutil import copy
from subprocess import CalledProcessError, check_call

from storm.store import Store
from zope.component import getUtility

from lp.app.errors import NotFoundError
from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.interfaces.archivepublisherrun import (
    IArchivePublisherRunSet,
)
from lp.archivepublisher.interfaces.archivepublishinghistory import (
    IArchivePublishingHistorySet,
)
from lp.archivepublisher.interfaces.ctdeliveryjob import (
    ICTDeliveryDebJobSource,
)
from lp.archivepublisher.model.archivepublisherrun import ArchivePublisherRun
from lp.archivepublisher.model.archivepublishinghistory import (
    ArchivePublishingHistory,
)
from lp.archivepublisher.model.ctdeliveryjob import CTDeliveryJob
from lp.archivepublisher.publishing import (
    GLOBAL_PUBLISHER_LOCK,
    cannot_modify_suite,
    getPublisher,
)
from lp.archivepublisher.scripts.base import PublisherScript
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.features import getFeatureFlag
from lp.services.limitedlist import LimitedList
from lp.services.scripts.base import LaunchpadScriptFailure
from lp.services.webapp.adapter import (
    clear_request_started,
    set_request_started,
)
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveStatus,
)
from lp.soyuz.interfaces.archive import MAIN_ARCHIVE_PURPOSES, IArchiveSet

ARCHIVEPUBLISHER_HISTORY_ENABLED = "archivepublisher.history.enabled"
PUBLISHER_DIRECT_INDEXES = "archivepublisher.direct_indexes.enabled"


def is_ppa_private(ppa):
    """Is `ppa` private?"""
    return ppa.private


def is_ppa_public(ppa):
    """Is `ppa` public?"""
    return not ppa.private


def has_oval_data_changed(incoming_dir, published_dir):
    """Compare the incoming data with the already published one."""
    # XXX cjwatson 2023-04-19: `dircmp` in Python < 3.6 doesn't accept
    # path-like objects.
    compared = dircmp(str(incoming_dir), str(published_dir))
    return (
        bool(compared.left_only)
        or bool(compared.right_only)
        or bool(compared.diff_files)
        or bool(compared.funny_files)
    )


class PublishDistro(PublisherScript):
    """Distro publisher."""

    @property
    def lockfilename(self):
        return self.options.lockfilename or GLOBAL_PUBLISHER_LOCK

    def add_my_options(self):
        self.addDistroOptions()
        self.addBasePublisherOptions()

        self.parser.add_option(
            "-C",
            "--careful",
            action="store_true",
            dest="careful",
            default=False,
            help="Turns on all the below careful options.",
        )

        self.parser.add_option(
            "-P",
            "--careful-publishing",
            action="store_true",
            dest="careful_publishing",
            default=False,
            help="Make the package publishing process careful.",
        )

        self.parser.add_option(
            "-D",
            "--careful-domination",
            action="store_true",
            dest="careful_domination",
            default=False,
            help="Make the domination process careful.",
        )

        self.parser.add_option(
            "-A",
            "--careful-apt",
            action="store_true",
            dest="careful_apt",
            default=False,
            help="Make index generation (e.g. apt-ftparchive) careful.",
        )

        self.parser.add_option(
            "--careful-release",
            action="store_true",
            dest="careful_release",
            default=False,
            help="Make the Release file generation process careful.",
        )

        self.parser.add_option(
            "--disable-publishing",
            action="store_false",
            dest="enable_publishing",
            default=True,
            help="Disable the package publishing process.",
        )

        self.parser.add_option(
            "--disable-domination",
            action="store_false",
            dest="enable_domination",
            default=True,
            help="Disable the domination process.",
        )

        self.parser.add_option(
            "--disable-apt",
            action="store_false",
            dest="enable_apt",
            default=True,
            help="Disable index generation (e.g. apt-ftparchive).",
        )

        self.parser.add_option(
            "--disable-release",
            action="store_false",
            dest="enable_release",
            default=True,
            help="Disable the Release file generation process.",
        )

        self.parser.add_option(
            "--include-non-pending",
            action="store_true",
            dest="include_non_pending",
            default=False,
            help=(
                "When publishing PPAs, also include those that do not have "
                "pending publications."
            ),
        )

        self.parser.add_option(
            "-s",
            "--suite",
            metavar="SUITE",
            dest="suite",
            action="append",
            type="string",
            default=[],
            help="The suite to publish",
        )

        self.parser.add_option(
            "--dirty-suite",
            metavar="SUITE",
            dest="dirty_suites",
            action="append",
            default=[],
            help="Consider this suite dirty regardless of publications.",
        )

        self.parser.add_option(
            "-R",
            "--distsroot",
            dest="distsroot",
            metavar="SUFFIX",
            default=None,
            help=(
                "Override the dists path for generation of the PRIMARY and "
                "PARTNER archives only."
            ),
        )

        self.parser.add_option(
            "--ppa",
            action="store_true",
            dest="ppa",
            default=False,
            help="Only run over PPA archives.",
        )

        self.parser.add_option(
            "--private-ppa",
            action="store_true",
            dest="private_ppa",
            default=False,
            help="Only run over private PPA archives.",
        )

        self.parser.add_option(
            "--partner",
            action="store_true",
            dest="partner",
            default=False,
            help="Only run over the partner archive.",
        )

        self.parser.add_option(
            "--copy-archive",
            action="store_true",
            dest="copy_archive",
            default=False,
            help="Only run over the copy archives.",
        )

    def isCareful(self, option):
        """Is the given "carefulness" option enabled?

        Yes if the option is True, but also if the global "careful" option
        is set.

        :param option: The specific "careful" option to test, e.g.
            `self.options.careful_publishing`.
        :return: Whether the option should be treated as asking us to be
            careful.
        """
        return option or self.options.careful

    def describeCare(self, option):
        """Helper: describe carefulness setting of given option.

        Produces a human-readable string saying whether the option is set
        to careful mode; or "overridden" to careful mode by the global
        "careful" option; or is left in normal mode.
        """
        if self.options.careful:
            return "Careful (Overridden)"
        elif option:
            return "Careful"
        else:
            return "Normal"

    def logOption(self, name, value):
        """Describe the state of `option` to the debug log."""
        self.logger.debug("%14s: %s", name, value)

    def countExclusiveOptions(self):
        """Return the number of exclusive "mode" options that were set.

        In valid use, at most one of them should be set.
        """
        exclusive_options = [
            self.options.partner,
            self.options.ppa,
            self.options.private_ppa,
            self.options.copy_archive,
            self.options.archives,
        ]
        return len(list(filter(None, exclusive_options)))

    def logOptions(self):
        """Dump the selected options to the debug log."""
        if self.countExclusiveOptions() == 0:
            indexing_engine = "Apt-FTPArchive"
        else:
            indexing_engine = "Indexing"
        self.logOption("Distribution", self.options.distribution)
        log_items = [
            ("Publishing", self.options.careful_publishing),
            ("Domination", self.options.careful_domination),
            (indexing_engine, self.options.careful_apt),
            ("Release", self.options.careful_release),
        ]
        for name, option in log_items:
            self.logOption(name, self.describeCare(option))

        if self.options.excluded_archives:
            self.logger.debug(
                "Excluded archive references: %s",
                self.options.excluded_archives,
            )
        if self.options.archives:
            self.logger.debug(
                "Specified archive references: %s", self.options.archives
            )

    def validateOptions(self):
        """Check given options for user interface violations."""
        if len(self.args) > 0:
            raise OptionValueError(
                "publish-distro takes no arguments, only options."
            )
        if self.countExclusiveOptions() > 1:
            raise OptionValueError(
                "Can only specify one of partner, ppa, private-ppa, "
                "copy-archive, archive."
            )

        if self.options.all_derived and self.options.distribution is not None:
            raise OptionValueError(
                "Specify --distribution or --all-derived, but not both."
            )

        for_ppa = self.options.ppa or self.options.private_ppa
        if for_ppa and self.options.distsroot:
            raise OptionValueError(
                "We should not define 'distsroot' in PPA mode!",
            )

    def findSuite(self, distribution, suite):
        """Find the named `suite` in the selected `Distribution`.

        :param suite: The suite name to look for.
        :return: A tuple of distroseries and pocket.
        """
        try:
            series, pocket = distribution.getDistroSeriesAndPocket(suite)
        except NotFoundError as e:
            raise OptionValueError(e)
        return series, pocket

    def findAllowedSuites(self, distribution):
        """Find the selected suite(s)."""
        suites = set()
        for suite in self.options.suite:
            series, pocket = self.findSuite(distribution, suite)
            suites.add(series.getSuite(pocket))
        return suites

    def findExplicitlyDirtySuites(self, archive):
        """Find the suites that have been explicitly marked as dirty."""
        for suite in self.options.dirty_suites:
            yield self.findSuite(archive.distribution, suite)
        if archive.dirty_suites is not None:
            for suite in archive.dirty_suites:
                try:
                    yield archive.distribution.getDistroSeriesAndPocket(suite)
                except NotFoundError:
                    self.logger.exception(
                        "Failed to parse dirty suite '%s' for archive '%s'"
                        % (suite, archive.reference)
                    )

    def getCopyArchives(self, distribution):
        """Find copy archives for the selected distribution."""
        copy_archives = list(
            getUtility(IArchiveSet).getArchivesForDistribution(
                distribution, purposes=[ArchivePurpose.COPY]
            )
        )
        if copy_archives == []:
            raise LaunchpadScriptFailure("Could not find any COPY archives")
        return copy_archives

    def getPPAs(self, distribution):
        """Find private package archives for the selected distribution."""
        if (
            self.isCareful(self.options.careful_publishing)
            or self.options.include_non_pending
        ):
            return distribution.getAllPPAs()
        else:
            return distribution.getPendingPublicationPPAs()

    def getArchives(self, archive_references, distribution):
        """Find the archives with the given references."""
        archives = self.findArchives(archive_references, distribution)

        if (
            self.isCareful(self.options.careful_publishing)
            or self.options.include_non_pending
        ):
            return archives
        else:
            archive_ids = [archive.id for archive in archives]
            return distribution.getPendingPublicationPPAs(
                archive_ids=archive_ids
            )

    def getTargetArchives(self, distribution):
        """Find the archive(s) selected by the script's options."""
        if self.options.archives:
            return self.getArchives(self.options.archives, distribution)
        elif self.options.partner:
            return [distribution.getArchiveByComponent("partner")]
        elif self.options.ppa:
            archives = set(filter(is_ppa_public, self.getPPAs(distribution)))
            excluded_archives = set(
                self.findArchives(self.options.excluded_archives, distribution)
            )
            return archives - excluded_archives
        elif self.options.private_ppa:
            archives = set(filter(is_ppa_private, self.getPPAs(distribution)))
            excluded_archives = set(
                self.findArchives(self.options.excluded_archives, distribution)
            )
            return archives - excluded_archives
        elif self.options.copy_archive:
            return self.getCopyArchives(distribution)
        else:
            return [distribution.main_archive]

    def getPublisher(self, distribution, archive, allowed_suites):
        """Get a publisher for the given options."""
        if archive.purpose in MAIN_ARCHIVE_PURPOSES:
            description = "%s %s" % (distribution.name, archive.displayname)
            # Only let the primary/partner archives override the distsroot.
            distsroot = self.options.distsroot
        else:
            description = archive.archive_url
            distsroot = None

        self.logger.info("Processing %s", description)
        return getPublisher(archive, allowed_suites, self.logger, distsroot)

    def deleteArchive(self, archive, publisher):
        """Ask `publisher` to delete `archive`."""
        if (
            archive.purpose == ArchivePurpose.PPA
            and archive.publishing_method == ArchivePublishingMethod.LOCAL
        ):
            publisher.deleteArchive()
            return True
        else:
            # Other types of archives do not currently support deletion.
            self.logger.warning(
                "Deletion of %s skipped: operation not supported on %s",
                archive.displayname,
                archive.purpose.title,
            )
            return False

    def synchronizeSecondDirectoryWithFirst(
        self, first_dir, second_dir, ignore
    ):
        """Synchronize the contents of the second directory with the first."""
        comparison = dircmp(str(first_dir), str(second_dir), ignore=ignore)
        files_to_copy = (
            comparison.diff_files
            + comparison.left_only
            + comparison.funny_files
        )
        files_to_delete = comparison.right_only

        for file in files_to_copy:
            copy(str(first_dir / file), str(second_dir))

        for file in files_to_delete:
            os.unlink(str(second_dir / file))

        return bool(files_to_copy) or bool(files_to_delete)

    def syncOVALDataFilesForSuite(self, archive, suite):
        """Synchronize the OVAL data from the staging to the PPA directory."""
        updated = False
        staged_oval_data_for_suite = (
            Path(config.archivepublisher.oval_data_root)
            / archive.reference
            / suite
        )
        if staged_oval_data_for_suite.exists():
            for item in staged_oval_data_for_suite.iterdir():
                if not item.is_dir():
                    continue
                component = item
                staged_oval_data_dir = staged_oval_data_for_suite / component
                dest_dir = (
                    Path(getPubConfig(archive).distsroot)
                    / suite
                    / component.name
                    / "oval"
                )
                dest_dir.mkdir(parents=True, exist_ok=True)
                files_modified = self.synchronizeSecondDirectoryWithFirst(
                    staged_oval_data_dir, dest_dir, ignore=["by-hash"]
                )
                if files_modified:
                    updated = True
        return updated

    def publishArchive(self, archive, publisher):
        """Ask `publisher` to publish `archive`.

        Commits transactions along the way.
        """
        publishing_method = archive.publishing_method

        for distroseries, pocket in self.findExplicitlyDirtySuites(archive):
            if not cannot_modify_suite(archive, distroseries, pocket):
                publisher.markSuiteDirty(distroseries, pocket)

        dirty_suites = None
        if archive.dirty_suites is not None:
            # Clear the explicit dirt indicator before we start doing
            # time-consuming publishing, which might race with an
            # Archive.markSuiteDirty call.
            dirty_suites = archive.dirty_suites
            archive.dirty_suites = None
            self.txn.commit()

        publisher.setupArchiveDirs()
        if self.options.enable_publishing:
            publisher.A_publish(
                self.isCareful(self.options.careful_publishing)
            )
            self.txn.commit()

        if self.options.enable_domination:
            # Flag dirty pockets for any outstanding deletions.
            publisher.A2_markPocketsWithDeletionsDirty()
            publisher.B_dominate(
                self.isCareful(self.options.careful_domination)
            )
            self.txn.commit()

        if self.options.enable_apt:
            careful_indexing = self.isCareful(self.options.careful_apt)
            if publishing_method == ArchivePublishingMethod.LOCAL:
                # The primary and copy archives use apt-ftparchive to
                # generate the indexes, everything else uses the newer
                # internal LP code.
                if archive.purpose in (
                    ArchivePurpose.PRIMARY,
                    ArchivePurpose.COPY,
                ):
                    if getFeatureFlag(PUBLISHER_DIRECT_INDEXES):
                        publisher.C_doDirectIndexes(careful_indexing)
                    else:
                        publisher.C_doFTPArchive(careful_indexing)
                else:
                    publisher.C_writeIndexes(careful_indexing)
            elif publishing_method == ArchivePublishingMethod.ARTIFACTORY:
                publisher.C_updateArtifactoryProperties(careful_indexing)
            else:
                raise AssertionError(
                    "Unhandled publishing method: %r" % publishing_method
                )
            self.txn.commit()

        if (
            self.options.enable_release
            and publishing_method == ArchivePublishingMethod.LOCAL
        ):
            # XXX 2023-04-21 jugmac00: add test for the non-ppa case
            if (
                config.archivepublisher.oval_data_rsync_endpoint
                and archive.is_ppa
                and dirty_suites
            ):
                for dirty_suite in dirty_suites:
                    updated = self.syncOVALDataFilesForSuite(
                        archive, dirty_suite
                    )
                    if updated:
                        self.logger.info(
                            "Synchronized the OVAL data for %s",
                            archive.reference,
                        )
                    # XXX 2023-04-21 jugmac00: evaluate whether the above code
                    # would better fit into the `Publisher` class
            publisher.D_writeReleaseFiles(
                self.isCareful(
                    self.options.careful_apt or self.options.careful_release
                )
            )
            # The caller will commit this last step.

        if (
            self.options.enable_apt
            and publishing_method == ArchivePublishingMethod.LOCAL
        ):
            publisher.createSeriesAliases()

    def processArchive(self, archive_id, publisher_run_id, reset_store=True):
        set_request_started(
            request_statements=LimitedList(10000),
            txn=self.txn,
            enable_timeout=False,
        )
        try:
            archive = getUtility(IArchiveSet).get(archive_id)
            self.logger.info("Processing archive: %s", archive.reference)
            distribution = archive.distribution
            allowed_suites = self.findAllowedSuites(distribution)
            if archive.status == ArchiveStatus.DELETING:
                publisher = self.getPublisher(
                    distribution, archive, allowed_suites
                )
                work_done = self.deleteArchive(archive, publisher)
            elif archive.can_be_published:
                publisher = self.getPublisher(
                    distribution, archive, allowed_suites
                )
                self.publishArchive(archive, publisher)
                work_done = True
            else:
                work_done = False
        finally:
            clear_request_started()

        if work_done:
            self._finalize_archive_publish(
                archive, archive_id, publisher_run_id, reset_store
            )

    def _buildRsyncCommand(self, src, dest, extra_options=None):
        if extra_options is None:
            extra_options = []

        # Ensure that the rsync paths have a trailing slash.
        rsync_src = os.path.join(src, "")
        rsync_dest = os.path.join(dest, "")

        rsync_command = [
            "/usr/bin/rsync",
            "-a",
            "-q",
            "--timeout={}".format(
                config.archivepublisher.oval_data_rsync_timeout
            ),
            "--delete",
            "--delete-after",
        ]
        rsync_command.extend(extra_options)
        rsync_command.extend([rsync_src, rsync_dest])
        return rsync_command

    def _generateOVALDataRsyncCommands(self):
        if self.options.archives:
            return [
                self._buildRsyncCommand(
                    # -R: copies the OVALData and preserves the src path in
                    # dest directory. This effectively means if you specify
                    # src as ::oval/~person/distro/ppa-name, -R will create
                    # the whole tree `person/distro/ppa-name` in dest.
                    #
                    # --ignore-missing-args: If the source directory is not
                    # present, don't throw an error. rsync defaults to
                    # throwing an error when the src path is not found.
                    #
                    # `man rsync` can provide detailed explanation of these
                    # options with ellaborated examples.
                    extra_options=["-R", "--ignore-missing-args"],
                    src=os.path.join(
                        config.archivepublisher.oval_data_rsync_endpoint,
                        reference,
                    ),
                    dest=os.path.join(config.archivepublisher.oval_data_root),
                )
                for reference in self.options.archives
            ]

        exclude_options = []
        # If there are any archives specified to be excluded, exclude rsync
        # for them in the rsync command
        for excluded_archive_reference in self.options.excluded_archives:
            exclude_options.extend(["--exclude", excluded_archive_reference])
        return [
            self._buildRsyncCommand(
                extra_options=exclude_options,
                src=config.archivepublisher.oval_data_rsync_endpoint,
                dest=config.archivepublisher.oval_data_root,
            )
        ]

    def rsyncOVALData(self):
        for rsync_command in self._generateOVALDataRsyncCommands():
            try:
                self.logger.info(
                    "Attempting to rsync the OVAL data: %s",
                    rsync_command,
                )
                check_call(rsync_command)
            except CalledProcessError:
                self.logger.exception(
                    "Failed to rsync OVAL data: %s",
                    rsync_command,
                )
                raise

    def checkForUpdatedOVALData(self, distribution):
        """Compare the published OVAL files with the incoming one."""
        start_dir = Path(config.archivepublisher.oval_data_root)
        archive_set = getUtility(IArchiveSet)
        for owner_path in start_dir.iterdir():
            if not owner_path.name.startswith("~"):
                continue
            distribution_path = owner_path / distribution.name
            if not distribution_path.is_dir():
                continue
            for archive_path in distribution_path.iterdir():
                archive = archive_set.getPPAByDistributionAndOwnerName(
                    distribution, owner_path.name[1:], archive_path.name
                )
                if archive is None:
                    self.logger.info(
                        "Skipping OVAL data for '~%s/%s/%s' "
                        "(no such archive).",
                        owner_path.name[1:],
                        distribution.name,
                        archive_path.name,
                    )
                    continue
                # If --archive is set, run only for the specified archives
                # and skip others.
                if (
                    self.options.archives
                    and archive.reference not in self.options.archives
                ):
                    continue
                # skip any archive excluded by --exclude
                if archive.reference in self.options.excluded_archives:
                    continue
                for suite_path in archive_path.iterdir():
                    try:
                        series, pocket = distribution.getDistroSeriesAndPocket(
                            suite_path.name
                        )
                    except NotFoundError:
                        self.logger.info(
                            "Skipping OVAL data for '%s:%s' (no such suite).",
                            archive.reference,
                            suite_path.name,
                        )
                        continue
                    for component in archive.getComponentsForSeries(series):
                        incoming_dir = suite_path / component.name
                        published_dir = (
                            Path(getPubConfig(archive).distsroot)
                            / series.name
                            / component.name
                            / "oval"
                        )
                        if not published_dir.is_dir() or has_oval_data_changed(
                            incoming_dir=incoming_dir,
                            published_dir=published_dir,
                        ):
                            archive.markSuiteDirty(
                                distroseries=series, pocket=pocket
                            )
                            break

    def _create_publisher_run(self):
        """Create PublisherRun record for this run."""
        if not getFeatureFlag(ARCHIVEPUBLISHER_HISTORY_ENABLED):
            return None

        publisher_run_set = getUtility(IArchivePublisherRunSet)
        publisher_run = publisher_run_set.new()

        IStore(ArchivePublisherRun).flush()
        self.txn.commit()
        self.logger.debug(f"Created PublisherRun id={publisher_run.id}")
        return publisher_run.id

    def _create_publishing_history(self, archive_id, publisher_run_id):
        """Create PublishingHistory records for all processed archives."""
        if not publisher_run_id:
            return None

        archive = getUtility(IArchiveSet).get(archive_id)
        publisher_run = getUtility(IArchivePublisherRunSet).getById(
            publisher_run_id
        )

        publishing_history = getUtility(IArchivePublishingHistorySet).new(
            archive, publisher_run
        )
        IStore(ArchivePublishingHistory).flush()
        self.logger.debug(f"Created PublishingHistory archive_id={archive_id}")
        return publishing_history.id

    def _finalize_archive_publish(
        self, archive, archive_id, publisher_run_id, reset_store
    ):
        """Record history, enqueue CT job, and reset store if needed."""
        publishing_history_id = None
        try:
            publishing_history_id = self._create_publishing_history(
                archive_id, publisher_run_id
            )
        except Exception:
            self.logger.warning(
                "Failed to record ArchivePublishingHistory for archive %s",
                archive_id,
            )
        if publishing_history_id:
            try:
                getUtility(ICTDeliveryDebJobSource).create(
                    publishing_history_id
                )
                IStore(CTDeliveryJob).flush()
            except Exception:
                self.logger.warning(
                    "Failed to enqueue CTDeliveryDebJob for archive %s",
                    publishing_history_id,
                )
        self.txn.commit()
        if reset_store:
            # Reset the store after processing each dirty archive, as otherwise
            # the process of publishing large archives can accumulate a large
            # number of alive objects in the Storm store and cause performance
            # problems.
            Store.of(archive).reset()

    def _update_publisher_run_status(self, publisher_run_id, succeeded):
        """Update the status of the given PublisherRun.id."""
        if not publisher_run_id:
            return

        publisher_run = getUtility(IArchivePublisherRunSet).getById(
            publisher_run_id
        )

        if succeeded and publisher_run.publishing_history():
            publisher_run.mark_succeeded()
            self.logger.debug(
                f"Marked PublisherRun id={publisher_run_id} as succeeded"
            )
        elif succeeded and not publisher_run.publishing_history():
            publisher_run.destroySelf()
            self.logger.debug(
                f"Destroyed PublisherRun id={publisher_run_id} "
                "as no archive was published"
            )
        else:
            publisher_run.mark_failed()
            self.logger.debug(
                f"Marked PublisherRun id={publisher_run_id} as failed"
            )

        IStore(ArchivePublisherRun).flush()
        self.txn.commit()

    def main(self, reset_store_between_archives=True):
        """See `LaunchpadScript`."""
        publisher_run_id = self._create_publisher_run()

        succeeded = False
        archive_ids = []
        try:
            self.validateOptions()
            self.logOptions()

            if config.archivepublisher.oval_data_rsync_endpoint:
                self.rsyncOVALData()
            else:
                self.logger.info(
                    "Skipping the OVAL data sync as no rsync endpoint"
                    " has been configured."
                )

            for distribution in self.findDistros():
                if config.archivepublisher.oval_data_rsync_endpoint:
                    self.checkForUpdatedOVALData(distribution)
                for archive in self.getTargetArchives(distribution):
                    if archive.distribution != distribution:
                        raise AssertionError(
                            "Archive %s does not match distribution %r"
                            % (archive.reference, distribution)
                        )
                    archive_ids.append(archive.id)

            for archive_id in archive_ids:
                self.processArchive(
                    archive_id,
                    publisher_run_id,
                    reset_store=reset_store_between_archives,
                )

            succeeded = True

        except Exception:
            # Abort the transaction before finally block and raise the same
            # exception.
            self.txn.abort()
            raise

        finally:
            # Re-fetch the PublisherRun as processArchive resets the store.
            self._update_publisher_run_status(publisher_run_id, succeeded)

        self.logger.debug("Ciao")
