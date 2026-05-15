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
from unittest.mock import patch

import responses
from testtools.matchers import Contains
from zope.component import getUtility

from lp.bugs.interfaces.cve import CveStatus, ICveSet
from lp.bugs.scripts.cveimport import (
    CVEUpdater,
    HTTPScriptFailure,
    retry_on_failure,
)
from lp.services.log.logger import BufferLogger, DevNullLogger
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

    def test_construct_github_url_candidates_delta(self):
        """Delta yields primary and +1h fallback URL."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        fixed_now = datetime(2026, 1, 29, 2, 0, tzinfo=timezone.utc)
        with patch("lp.bugs.scripts.cveimport.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            candidates = updater.construct_github_url_candidates(delta=True)

        self.assertEqual(2, len(candidates))
        # Primary: release tag and filename both match 0200Z
        self.assertThat(candidates[0], Contains("cve_2026-01-29_0200Z"))
        self.assertThat(
            candidates[0],
            Contains("2026-01-29_delta_CVEs_at_0200Z.zip"),
        )
        # Fallback: same release tag, filename shifted to 0300Z
        self.assertThat(candidates[1], Contains("cve_2026-01-29_0200Z"))
        self.assertThat(
            candidates[1],
            Contains("2026-01-29_delta_CVEs_at_0300Z.zip"),
        )

    def test_construct_github_url_candidates_delta_2300Z(self):
        """Delta at 2300Z yields primary and a next-day 0000Z fallback URL.

        The +1h filename wraps past midnight, so the fallback uses tomorrow's
        date with 0000Z within the same 2300Z release tag.
        """
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        fixed_now = datetime(2026, 1, 29, 23, 0, tzinfo=timezone.utc)
        with patch("lp.bugs.scripts.cveimport.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            candidates = updater.construct_github_url_candidates(delta=True)

        self.assertEqual(2, len(candidates))
        self.assertThat(candidates[0], Contains("cve_2026-01-29_2300Z"))
        self.assertThat(
            candidates[0],
            Contains("2026-01-29_delta_CVEs_at_2300Z.zip"),
        )
        # Fallback: same release tag, filename uses next day + 0000Z
        self.assertThat(candidates[1], Contains("cve_2026-01-29_2300Z"))
        self.assertThat(
            candidates[1],
            Contains("2026-01-30_delta_CVEs_at_0000Z.zip"),
        )

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

    @responses.activate
    def test_fetch_http_error_raises_http_script_failure(self):
        """fetchCVEURL raises HTTPScriptFailure with status_code."""
        url = "http://cve.example.com/allitems.xml"
        responses.add("GET", url, status=503)
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        exc = self.assertRaises(HTTPScriptFailure, updater.fetchCVEURL, url)
        self.assertEqual(503, exc.status_code)

    def test_retry_on_failure_swallows_404_on_last_attempt(self):
        """retry_on_failure does not raise if the final attempt gets a 404."""
        calls = []

        def always_404():
            calls.append(1)
            raise HTTPScriptFailure("HTTP 404 at 'url'", status_code=404)

        # Should not raise even though every attempt raises HTTPScriptFailure.
        result = retry_on_failure(
            always_404, max_retries=2, delay=0, logger=DevNullLogger()
        )
        self.assertIsNone(result)
        self.assertEqual(2, len(calls))

    def test_retry_on_failure_raises_non_404_on_last_attempt(self):
        """retry_on_failure re-raises non-404 HTTPScriptFailure."""

        def always_503():
            raise HTTPScriptFailure("HTTP 503 at 'url'", status_code=503)

        exc = self.assertRaises(
            HTTPScriptFailure,
            retry_on_failure,
            always_503,
            2,
            0,
            DevNullLogger(),
        )
        self.assertEqual(503, exc.status_code)

    @responses.activate
    def test_try_fetch_delta_candidates_falls_back_to_next_url(self):
        """_try_fetch_delta_candidates tries the next URL if the first fails"""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        success_response = b"zip content"
        responses.add("GET", "http://example.com/url1", status=404)
        responses.add("GET", "http://example.com/url2", body=success_response)

        result = updater._try_fetch_delta_candidates(
            ["http://example.com/url1", "http://example.com/url2"]
        )

        self.assertEqual(success_response, result)
        self.assertEqual(
            ["http://example.com/url1", "http://example.com/url2"],
            [call.request.url for call in responses.calls],
        )

    @responses.activate
    def test_try_fetch_delta_candidates_raises_when_all_fail(self):
        """_try_fetch_delta_candidates raises if all candidate URLs fail."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        urls = ["http://example.com/url1", "http://example.com/url2"]
        for url in urls:
            responses.add("GET", url, status=404)

        exc = self.assertRaises(
            HTTPScriptFailure,
            updater._try_fetch_delta_candidates,
            urls,
        )

        self.assertIn("All candidate URLs failed", str(exc))
        self.assertIn("http://example.com/url1", str(exc))
        self.assertIn("http://example.com/url2", str(exc))
        self.assertEqual(404, exc.status_code)

    @responses.activate
    def test_try_fetch_delta_candidates_raises_immediately_on_non_404(self):
        """_try_fetch_delta_candidates re-raises non-404 errors immediately."""
        updater = CVEUpdater(
            "cve-updater", test_args=[], logger=DevNullLogger()
        )
        responses.add("GET", "http://example.com/url1", status=503)

        exc = self.assertRaises(
            HTTPScriptFailure,
            updater._try_fetch_delta_candidates,
            ["http://example.com/url1", "http://example.com/url2"],
        )

        # Should have stopped after the first URL, not tried the second.
        self.assertEqual(1, len(responses.calls))
        self.assertEqual(503, exc.status_code)

    def test_handle_github_delta_all_404_returns_zero_and_logs_skip(self):
        """_handle_github_delta returns (0, 0) and logs a skip message when
        all candidate URLs return 404 across all retries (release skipped).
        """
        logger = BufferLogger()
        updater = CVEUpdater("cve-updater", test_args=[], logger=logger)

        # Simulate retry_on_failure exhausting all retries on 404s by
        # returning None, which is the contract documented in retry_on_failure.
        with patch(
            "lp.bugs.scripts.cveimport.retry_on_failure", return_value=None
        ):
            result = updater._handle_github_delta()

        self.assertEqual((0, 0), result)
        self.assertIn(
            "No delta release found for this hour",
            logger.getLogBuffer(),
        )
