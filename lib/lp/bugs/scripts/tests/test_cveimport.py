# Copyright 2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import gzip
import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import responses
from testtools.matchers import Contains
from zope.component import getUtility

from lp.bugs.interfaces.cve import CveStatus, ICveSet
from lp.bugs.scripts.cveimport import CVEUpdater
from lp.services.log.logger import DevNullLogger
from lp.services.scripts.base import LaunchpadScriptFailure
from lp.testing import TestCase
from lp.testing.layers import LaunchpadZopelessLayer


class TestCVEUpdater(TestCase):
    @responses.activate
    def test_fetch_uncompressed(self):
        # Fetching a URL returning uncompressed data works.
        url = "http://cve.example.com/allitems.xml"
        body = b'<?xml version="1.0"?>'
        responses.add(
            "GET", url, headers={"Content-Type": "text/xml"}, body=body
        )
        cve_updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        self.assertEqual(body, cve_updater.fetchCVEURL(url))

    @responses.activate
    def test_fetch_content_encoding_gzip(self):
        # Fetching a URL returning Content-Encoding: gzip works.
        url = "http://cve.example.com/allitems.xml.gz"
        body = b'<?xml version="1.0"?>'
        gzipped_body_file = io.BytesIO()
        with gzip.GzipFile(fileobj=gzipped_body_file, mode="wb") as f:
            f.write(body)
        responses.add(
            "GET",
            url,
            headers={
                "Content-Type": "text/xml",
                "Content-Encoding": "gzip",
            },
            body=gzipped_body_file.getvalue(),
        )
        cve_updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        self.assertEqual(body, cve_updater.fetchCVEURL(url))

    @responses.activate
    def test_fetch_gzipped(self):
        # Fetching a URL returning gzipped data without Content-Encoding works.
        url = "http://cve.example.com/allitems.xml.gz"
        body = b'<?xml version="1.0"?>'
        gzipped_body_file = io.BytesIO()
        with gzip.GzipFile(fileobj=gzipped_body_file, mode="wb") as f:
            f.write(body)
        responses.add(
            "GET",
            url,
            headers={"Content-Type": "application/x-gzip"},
            body=gzipped_body_file.getvalue(),
        )
        cve_updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        self.assertEqual(body, cve_updater.fetchCVEURL(url))

    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)

    def create_test_json_cve(
        self, cve_id="2024-0001", description="Test description"
    ):
        """Helper to create a test CVE JSON file"""
        cve_data = {
            "dataType": "CVE_RECORD",
            "cveMetadata": {"cveId": f"CVE-{cve_id}"},
            "containers": {
                "cna": {
                    "affected": [
                        {
                            "vendor": "example vendor",
                            "product": "example product",
                        }
                    ],
                    "descriptions": [{"lang": "en", "value": description}],
                    "references": [
                        {
                            "url": "http://example.com/ref1",
                            "name": "Reference 1",
                        }
                    ],
                    "metrics": [
                        {
                            "cvssV3_0": {
                                "version": "3.0",
                                "baseScore": 7.3,
                                "vectorString": (
                                    "CVSS:3.0"
                                    "/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:L"
                                ),
                                "baseSeverity": "HIGH",
                            }
                        },
                        {
                            "cvssV2_0": {
                                "version": "2.0",
                                "baseScore": 7.5,
                                "vectorString": "AV:N/AC:L/Au:N/C:P/I:P/A:P",
                            }
                        },
                    ],
                }
            },
        }
        return cve_data

    def make_updater(self, test_args=None):
        """Helper to create a properly initialized CVEUpdater."""
        if test_args is None:
            test_args = []
        updater = CVEUpdater(
            "cve-updater", test_args=test_args, logger=DevNullLogger()
        )
        # Initialize just the database connection
        updater._init_db(isolation="read_committed")
        return updater

    def test_process_json_directory(self):
        """Test processing a directory of CVE JSON files."""
        # Create test directory structure
        base_dir = Path(self.temp_dir) / "cves"
        year_dir = base_dir / "2024"
        group_dir = year_dir / "0xxx"
        group_dir.mkdir(parents=True)

        # Create a test CVE file
        cve_file = group_dir / "CVE-2024-0001.json"
        cve_data = self.create_test_json_cve()
        cve_file.write_text(json.dumps(cve_data))

        # Process the directory using the script infrastructure
        updater = self.make_updater([str(base_dir)])
        processed, errors = updater.process_json_directory(str(base_dir))

        # Verify results
        self.assertEqual(1, processed)
        self.assertEqual(0, errors)

        # Verify CVE was created
        cveset = getUtility(ICveSet)
        cve = cveset["2024-0001"]
        self.assertIsNotNone(cve)
        self.assertEqual("Test description", cve.description)

    def test_process_json_directory_with_bigger_group_name(self):
        """Test processing a JSON CVE dir with sequence bigger than 9999.

        This test makes sure the regular expression used allows this group dirs
        and cve files.
        """
        # Create test directory structure
        base_dir = Path(self.temp_dir) / "cves"
        year_dir = base_dir / "2025"

        # CVE sequence number can be > 9999 so we can have groups like 10xxx
        # or 9000xxx. See cvelistV5/2014/1000xxx or 2024/56xxx
        group_dir = year_dir / "9000xxx"
        group_dir.mkdir(parents=True)

        # Create a test CVE file
        cve_file = group_dir / "CVE-2025-9000001.json"
        cve_data = self.create_test_json_cve(cve_id="2025-9000001")
        cve_file.write_text(json.dumps(cve_data))

        # Process the directory using the script infrastructure
        updater = self.make_updater([str(base_dir)])
        processed, errors = updater.process_json_directory(str(base_dir))

        # Verify results
        self.assertEqual(1, processed)
        self.assertEqual(0, errors)

        # Verify CVE was created
        cveset = getUtility(ICveSet)
        cve = cveset["2025-9000001"]
        self.assertIsNotNone(cve)
        self.assertEqual("Test description", cve.description)

    def test_process_delta_directory(self):
        """Test processing a directory of delta CVE files."""
        # Create test delta directory
        delta_dir = Path(self.temp_dir) / "deltaCves"
        delta_dir.mkdir()

        # Create a test delta CVE file
        cve_file = delta_dir / "CVE-2024-0002.json"
        cve_data = self.create_test_json_cve(
            cve_id="2024-0002", description="Delta CVE"
        )
        cve_file.write_text(json.dumps(cve_data))

        # Process the directory using the script infrastructure
        updater = self.make_updater([str(delta_dir)])
        processed, errors = updater.process_delta_directory(str(delta_dir))

        # Verify results
        self.assertEqual(1, processed)
        self.assertEqual(0, errors)

        # Verify CVE was created
        cveset = getUtility(ICveSet)
        cve = cveset["2024-0002"]
        self.assertIsNotNone(cve)
        self.assertEqual("Delta CVE", cve.description)

    def test_process_delta_directory_empty(self):
        """Test processing an empty directory of delta CVE files."""
        # Create empty test delta directory
        delta_dir = Path(self.temp_dir) / "deltaCves"
        delta_dir.mkdir()

        # Process the directory using the script infrastructure
        updater = self.make_updater([str(delta_dir)])
        processed, errors = updater.process_delta_directory(str(delta_dir))

        # Verify results
        self.assertEqual(0, processed)
        self.assertEqual(0, errors)

    def test_construct_github_url(self):
        """Test GitHub URL construction for different scenarios."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )

        # Test baseline URL
        url = updater.construct_github_url(delta=False)
        expected = "_all_CVEs_at_midnight.zip"
        self.assertThat(url, Contains(expected))

        # Test delta URL (normal hour)
        url = updater.construct_github_url(delta=True)
        current_hour = datetime.now(timezone.utc).hour
        if current_hour not in (0, 23):
            expected = f"_delta_CVEs_at_{current_hour:02d}00Z.zip"
            self.assertThat(url, Contains(expected))

    def test_processCVEJSON(self):
        """Test handling of CVE JSON data."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )

        test_cve = self.create_test_json_cve(
            cve_id="2024-0003", description="Test CVE 2024-0003"
        )
        updater.processCVEJSON(test_cve)

        # Verify CVE was created
        cveset = getUtility(ICveSet)
        cve = cveset["2024-0003"]

        self.assertEqual("2024-0003", cve.sequence)
        self.assertEqual("Test CVE 2024-0003", cve.description)
        self.assertEqual(CveStatus.ENTRY, cve.status)

        metrics = test_cve.get("containers").get("cna").get("metrics")
        self.assertEqual(metrics, cve.cvss)
        affected = test_cve.get("containers").get("cna").get("affected")
        self.assertEqual({"affected": affected}, cve.metadata)

    def test_processCVEJSON_rejected(self):
        """Test handling of rejected CVE JSON data."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )

        rejected_cve = {
            "dataType": "CVE_RECORD",
            "cveMetadata": {"cveId": "CVE-2024-0004", "state": "REJECTED"},
            "containers": {
                "cna": {
                    "rejectedReasons": [
                        {"lang": "en", "value": "This CVE has been rejected."}
                    ],
                }
            },
        }

        updater.processCVEJSON(rejected_cve)

        # Verify CVE was created
        cveset = getUtility(ICveSet)
        cve = cveset["2024-0004"]
        self.assertEqual("2024-0004", cve.sequence)
        self.assertEqual("This CVE has been rejected.", cve.description)
        self.assertEqual(CveStatus.REJECTED, cve.status)
        self.assertEqual(None, cve.cvss)
        self.assertEqual(None, cve.metadata)

    def test_invalid_json_cve(self):
        """Test handling of invalid CVE JSON data."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )

        # Test invalid dataType
        invalid_data = {
            "dataType": "INVALID",
            "cveMetadata": {"cveId": "CVE-2024-0003"},
        }

        self.assertRaises(
            LaunchpadScriptFailure, updater.processCVEJSON, invalid_data
        )

    def test_update_existing_cve(self):
        """Test updating an existing CVE with new data."""
        # First create a CVE
        original_desc = "Original description"
        original_metadata = {
            "affected": [
                {
                    "vendor": "original vendor",
                    "product": "original product",
                }
            ],
        }
        cveset = getUtility(ICveSet)

        # Create initial CVE using a properly initialized updater
        updater = self.make_updater()
        cveset.new(
            "2024-0004",
            original_desc,
            CveStatus.ENTRY,
            metadata=original_metadata,
        )
        updater.txn.commit()

        # Create updated data
        new_desc = "Updated description"
        cve_data = self.create_test_json_cve(
            cve_id="2024-0004", description=new_desc
        )
        new_metadata = {
            "affected": [
                {
                    "vendor": "example vendor",
                    "product": "example product",
                }
            ],
        }

        # Process the update with a fresh updater
        updater = self.make_updater()
        updater.processCVEJSON(cve_data)
        updater.txn.commit()

        # Verify the update
        updated_cve = cveset["2024-0004"]
        self.assertEqual(new_desc, updated_cve.description)
        self.assertEqual(new_metadata, updated_cve.metadata)

    def test_extract_github_zip(self):
        """Test extract_github_zip for complete releases."""
        updater = self.make_updater()
        outer_buffer = io.BytesIO()

        with zipfile.ZipFile(outer_buffer, "w") as outer_zip:
            # create inner cves.zip in memory
            inner_buffer = io.BytesIO()
            with zipfile.ZipFile(inner_buffer, "w") as inner_zip:
                inner_zip.writestr("cves/CVE-2025-8941.json", "CVE data")
            outer_zip.writestr("cves.zip", inner_buffer.getvalue())

        target_dir = updater.extract_github_zip(outer_buffer.getvalue())
        self.assertTrue(target_dir.endswith("cves"))
        self.assertEqual(os.listdir(target_dir), ["CVE-2025-8941.json"])

    def test_extract_empty_github_zip(self):
        """Test that extract_github_zip for complete releases raises
        LaunchpadScriptFailure when the zip is empty.
        """
        updater = self.make_updater()
        buffer = io.BytesIO()

        # Empty zipfile buffer
        with zipfile.ZipFile(buffer, "w"):
            pass

        self.assertRaisesWithContent(
            LaunchpadScriptFailure,
            "Failed to extract ZIP files: There is no item named 'cves.zip' "
            "in the archive",
            updater.extract_github_zip,
            buffer.getvalue(),
        )

    def test_extract_delta_github_zip(self):
        """Test extract_github_zip for delta releases."""
        updater = self.make_updater()
        buffer = io.BytesIO()

        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("deltaCves/CVE-2025-8941.json", "delta CVE data")

        empty_dir = updater.extract_github_zip(buffer.getvalue(), delta=True)
        self.assertTrue(empty_dir.endswith("deltaCves"))
        self.assertEqual(os.listdir(empty_dir), ["CVE-2025-8941.json"])

    def test_extract_empty_delta_github_zip(self):
        """Test that extract_github_zip for delta releases returns an empty dir
        if the zip is empty. There can be hours when no cves are updated so we
        will return an empty dir and will not import cves.
        """
        updater = self.make_updater()
        buffer = io.BytesIO()

        # Empty zipfile buffer
        with zipfile.ZipFile(buffer, "w"):
            pass

        empty_dir = updater.extract_github_zip(buffer.getvalue(), delta=True)
        self.assertTrue(empty_dir.endswith("deltaCves"))
        self.assertEqual(os.listdir(empty_dir), [])
