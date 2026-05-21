#  Copyright 2022 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import copy
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import Mock, patch

from zope.component import getUtility

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.enums import VulnerabilityStatus
from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.bugs.model.bug import Bug
from lp.bugs.model.bugtask import BugTask
from lp.bugs.scripts.uct import (
    CVE,
    UCTExporter,
    UCTImporter,
    UCTImportError,
    UCTRecord,
)
from lp.bugs.scripts.uct.subprojects import PPAReference, SubProjectPPAs
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.distroseries import IDistroSeriesSet
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.model.sourcepackage import SourcePackage
from lp.services.propertycache import clear_property_cache
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import ZopelessDatabaseLayer

TAG_SEPARATOR = UCTImporter.TAG_SEPARATOR


class TestUCTRecord(TestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.record = UCTRecord(
            parent_dir="sampledata",
            assigned_to="",
            bugs=[
                "https://github.com/mm2/Little-CMS/issues/29",
                "https://github.com/mm2/Little-CMS/issues/30",
                "https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=745471",
            ],
            cvss={
                "nvd": [
                    "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H "
                    "[7.8 HIGH]",
                ]
            },
            candidate="CVE-2022-23222",
            crd=None,
            public_date_at_USN=datetime(
                2022, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            public_date=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            description=(
                "kernel/bpf/verifier.c in the Linux kernel through "
                "5.15.14 allows local\nusers to gain privileges because "
                "of the availability of pointer arithmetic\nvia certain "
                "*_OR_NULL pointer types."
            ),
            discovered_by="",
            mitigation=(
                "seth-arnold> set kernel.unprivileged_bpf_disabled to 1"
            ),
            notes=(
                "sbeattie> Ubuntu 21.10 / 5.13+ kernels disable "
                "unprivileged BPF by default.\n  kernels 5.8 and "
                "older are not affected, priority high is "
                "for\n  5.10 and 5.11 based kernels only"
            ),
            priority=UCTRecord.Priority.CRITICAL,
            references=["https://ubuntu.com/security/notices/USN-5368-1"],
            ubuntu_description=(
                "It was discovered that the BPF verifier in the Linux "
                "kernel did not\nproperly restrict pointer types in "
                "certain situations. A local attacker\ncould use this to "
                "cause a denial of service (system crash) or possibly\n"
                "execute arbitrary code."
            ),
            packages=[
                UCTRecord.Package(
                    name="linux",
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="5.17~rc1",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="impish",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="5.13.0-37.42",
                            priority=UCTRecord.Priority.MEDIUM,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="devel",
                            status=UCTRecord.PackageStatus.NOT_AFFECTED,
                            reason="5.15.0-25.25",
                            priority=UCTRecord.Priority.MEDIUM,
                        ),
                    ],
                    priority=None,
                    tags={"not-ue"},
                    patches=[
                        UCTRecord.Patch(
                            patch_type="break-fix",
                            entry=(
                                "457f44363a8894135c85b7a9afd2bd8196db24ab "
                                "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                                "local-CVE-2022-23222-fix"
                            ),
                        ),
                        UCTRecord.Patch(
                            patch_type="upstream",
                            entry=(
                                "https://github.com/389ds/389-ds-base/commit/58dbf084a63e6dbbd999bf6a70475fad8255f26a (1.4.4)"  # noqa: 501
                            ),
                        ),
                        UCTRecord.Patch(
                            patch_type="upstream",
                            entry=(
                                "https://github.com/389ds/389-ds-base/commit/2e5b526012612d1d6ccace46398bee679a730271"  # noqa: 501
                            ),
                        ),
                    ],
                ),
                UCTRecord.Package(
                    name="linux-hwe",
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="5.17~rc1",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="impish",
                            status=UCTRecord.PackageStatus.DOES_NOT_EXIST,
                            reason="",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="devel",
                            status=UCTRecord.PackageStatus.DOES_NOT_EXIST,
                            reason="",
                            priority=None,
                        ),
                    ],
                    priority=UCTRecord.Priority.HIGH,
                    tags=set(),
                    patches=[],
                ),
            ],
            global_tags={"cisa-kev"},
        )

    def test_load_save(self):
        load_from = Path(__file__).parent / "sampledata" / "CVE-2022-23222"
        uct_record = UCTRecord.load(load_from)
        self.assertDictEqual(
            self.record.__dict__,
            uct_record.__dict__,
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_to_path = uct_record.save(output_dir)
        self.assertEqual(
            output_dir / "sampledata" / "CVE-2022-23222", saved_to_path
        )
        self.assertEqual(load_from.read_text(), saved_to_path.read_text())

    def test_load_save_with_priority_explanation(self):
        load_from = Path(__file__).parent / "sampledata" / "CVE-2023-32637"
        uct_record = UCTRecord.load(load_from)
        self.assertDictEqual(
            UCTRecord(
                parent_dir="sampledata",
                assigned_to="",
                bugs=[""],
                cvss={
                    "nvd": [
                        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H "
                        "[9.8 CRITICAL]",
                    ]
                },
                candidate="CVE-2023-32637",
                crd=None,
                public_date_at_USN=None,
                public_date=datetime(2023, 7, 25, 6, 15, tzinfo=timezone.utc),
                description=(
                    "GBrowse accepts files with any formats uploaded and "
                    "places them in the area\naccessible through "
                    "unauthenticated web requests. Therefore, anyone who can\n"
                    "upload files through the product may execute arbitrary "
                    "code on the server."
                ),
                discovered_by="",
                mitigation=None,
                notes=(
                    "ccdm94> this has likely been fixed in all 2.x "
                    "versions."
                ),
                priority=UCTRecord.Priority.HIGH,
                priority_explanation=(
                    "This has a high priority because it is a vulnerability "
                    "that allows a remote\nattacker to execute code in a "
                    "machine, and it looks to be easily exploitable\ngiven "
                    "that it involves regular functionalities provided by the "
                    "application."
                ),
                references=[
                    "https://jvn.jp/en/jp/JVN35897618/",
                    "https://jbrowse.org/jb2/",
                    "http://gmod.org/wiki/GBrowse",
                    "https://www.cve.org/CVERecord?id=CVE-2023-32637",
                ],
                ubuntu_description="",
                packages=[
                    UCTRecord.Package(
                        name="gbrowse",
                        statuses=[
                            UCTRecord.SeriesPackageStatus(
                                series="upstream",
                                status=UCTRecord.PackageStatus.RELEASED,
                                reason="2.56+dfsg-1",
                                priority=None,
                            ),
                            UCTRecord.SeriesPackageStatus(
                                series="trusty",
                                status=UCTRecord.PackageStatus.IGNORED,
                                reason="end of standard support",
                                priority=None,
                            ),
                            UCTRecord.SeriesPackageStatus(
                                series="xenial",
                                status=UCTRecord.PackageStatus.IGNORED,
                                reason="end of standard support",
                                priority=None,
                            ),
                        ],
                        priority=None,
                        tags=set(),
                        patches=[],
                    ),
                ],
                global_tags=set(),
            ).__dict__,
            uct_record.__dict__,
        )

        output_dir = Path(self.makeTemporaryDirectory())
        saved_to_path = uct_record.save(output_dir)
        self.assertEqual(
            output_dir / "sampledata" / "CVE-2023-32637", saved_to_path
        )
        self.assertEqual(load_from.read_text(), saved_to_path.read_text())

    def test_from_str(self):
        load_from = Path(__file__).parent / "sampledata" / "CVE-2022-23222"
        with open(load_from, "rb") as f:
            string = f.read()

        record = UCTRecord.from_str(string)

        # We are importing a blob, creating a temp file
        self.record.parent_dir = "tmp"
        self.assertEqual(record.__dict__, self.record.__dict__)

    def test_to_str(self):
        load_from = Path(__file__).parent / "sampledata" / "CVE-2022-23222"
        with open(load_from) as f:
            expected_record = f.read()
        record_str = self.record.to_str()
        self.assertEqual(expected_record, record_str)

    def test_to_str_with_empty_fields(self):
        """Test serialization of UCTRecord with empty/None fields."""
        minimal_record = UCTRecord(
            parent_dir="active",
            assigned_to="",
            bugs=[""],  # Empty bug URL
            cvss={},  # Empty CVSS
            candidate="CVE-2024-00001",
            crd=None,
            public_date_at_USN=None,
            public_date=None,
            description="Test description",
            discovered_by="",
            mitigation=None,  # No mitigation
            notes="",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="",
            references=[""],  # Empty reference
            ubuntu_description="",
            packages=[],  # No packages
            global_tags=set(),  # No tags
        )

        serialized = minimal_record.to_str()

        # Verify it contains required fields
        self.assertIn("Candidate: CVE-2024-00001", serialized)
        self.assertIn("Priority: high", serialized)
        self.assertIn("Description:", serialized)
        # Verify empty fields are handled
        self.assertIn("Bugs:\n", serialized)
        self.assertIn("References:\n", serialized)
        # Mitigation should not appear since it's None
        self.assertNotIn("Mitigation:", serialized)

    def test_to_str_multiline_notes(self):
        """Test serialization of multi-line notes."""
        record = UCTRecord(
            parent_dir="active",
            assigned_to="",
            bugs=[],
            cvss={},
            candidate="CVE-2024-00002",
            crd=None,
            public_date_at_USN=None,
            public_date=None,
            description="Test",
            discovered_by="",
            mitigation=None,
            notes="author1> Line 1\n Line 2\nauthor2> Line 3",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="",
            references=[],
            ubuntu_description="",
            packages=[],
            global_tags=set(),
        )

        serialized = record.to_str()

        # Verify multiline notes are properly formatted
        self.assertIn("author1> Line 1", serialized)
        self.assertIn(" Line 2", serialized)
        self.assertIn("author2> Line 3", serialized)

    def test_to_str_with_priority_explanation(self):
        """Test serialization includes priority explanation."""
        record = UCTRecord(
            parent_dir="active",
            assigned_to="",
            bugs=[],
            cvss={},
            candidate="CVE-2024-00003",
            crd=None,
            public_date_at_USN=None,
            public_date=None,
            description="Test",
            discovered_by="",
            mitigation=None,
            notes="",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="This is high priority\nbecause reasons",
            references=[],
            ubuntu_description="",
            packages=[],
            global_tags=set(),
        )

        serialized = record.to_str()

        # Priority explanation should be indented
        self.assertIn("Priority: high", serialized)
        self.assertIn(" This is high priority", serialized)
        self.assertIn(" because reasons", serialized)


class TestCVE(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer
    maxDiff = None

    def setUp(self, *args, **kwargs):
        super().setUp(*args, **kwargs)
        celebrities = getUtility(ILaunchpadCelebrities)
        ubuntu = celebrities.ubuntu
        supported_series = self.factory.makeDistroSeries(
            distribution=ubuntu,
            status=SeriesStatus.SUPPORTED,
            name="focal",
        )
        current_series = self.factory.makeDistroSeries(
            distribution=ubuntu,
            status=SeriesStatus.CURRENT,
            name="jammy",
        )
        devel_series = self.factory.makeDistroSeries(
            distribution=ubuntu,
            status=SeriesStatus.DEVELOPMENT,
            name="kinetic",
        )
        dsp1 = self.factory.makeDistributionSourcePackage(distribution=ubuntu)
        dsp2 = self.factory.makeDistributionSourcePackage(distribution=ubuntu)
        product_1 = self.factory.makePackagingLink(
            sourcepackagename=dsp1.sourcepackagename,
            distroseries=current_series,
        ).productseries.product
        product_2 = self.factory.makePackagingLink(
            sourcepackagename=dsp2.sourcepackagename,
            distroseries=current_series,
        ).productseries.product

        assignee = self.factory.makePerson()

        self.uct_record = UCTRecord(
            parent_dir="active",
            assigned_to=assignee.name,
            bugs=["https://github.com/mm2/Little-CMS/issues/29"],
            cvss={
                "nvd": [
                    "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H "
                    "[7.8 HIGH]",
                ],
            },
            candidate="CVE-2022-23222",
            crd=datetime(2020, 1, 14, 8, 15, tzinfo=timezone.utc),
            public_date_at_USN=datetime(
                2021, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            public_date=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            description="description",
            discovered_by="",
            mitigation="mitigation",
            notes="author> text",
            priority=UCTRecord.Priority.CRITICAL,
            priority_explanation="sample priority_explanation",
            references=["https://ubuntu.com/security/notices/USN-5368-1"],
            ubuntu_description="ubuntu-description",
            packages=[
                UCTRecord.Package(
                    name=dsp1.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series=supported_series.name,
                            status=UCTRecord.PackageStatus.NOT_AFFECTED,
                            reason="reason 1",
                            priority=UCTRecord.Priority.MEDIUM,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series=current_series.name,
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="reason 2",
                            priority=UCTRecord.Priority.MEDIUM,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="devel",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="reason 3",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="reason 4",
                            priority=None,
                        ),
                    ],
                    priority=None,
                    tags={"not-ue", "universe-binary"},
                    patches=[
                        UCTRecord.Patch(
                            patch_type="upstream",
                            entry=(
                                "https://github.com/389ds/389-ds-base/"
                                "commit/123 (1.4.4)"
                            ),
                        ),
                        UCTRecord.Patch(
                            patch_type="upstream",
                            entry=(
                                "https://github.com/389ds/389-ds-base/"
                                "commit/456"
                            ),
                        ),
                        UCTRecord.Patch(
                            patch_type="break-fix",
                            entry=(
                                "457f44363a8894135c85b7a9afd2bd8196db24ab "
                                "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                                "local-CVE-2022-23222-fix"
                            ),
                        ),
                    ],
                ),
                UCTRecord.Package(
                    name=dsp2.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series=supported_series.name,
                            status=UCTRecord.PackageStatus.DOES_NOT_EXIST,
                            reason="",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series=current_series.name,
                            status=UCTRecord.PackageStatus.DOES_NOT_EXIST,
                            reason="",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="devel",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="",
                            priority=None,
                        ),
                    ],
                    priority=UCTRecord.Priority.HIGH,
                    tags=set(),
                    patches=[],
                ),
            ],
            global_tags={"cisa-kev"},
        )

        self.cve = CVE(
            sequence="CVE-2022-23222",
            date_made_public=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            date_notice_issued=datetime(
                2021, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            date_coordinated_release=datetime(
                2020, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            distro_packages=[
                CVE.DistroPackage(
                    target=dsp1,
                    importance=None,
                    package_name=dsp1.sourcepackagename,
                    tags={"not-ue", "universe-binary"},
                ),
                CVE.DistroPackage(
                    target=dsp2,
                    importance=BugTaskImportance.HIGH,
                    package_name=dsp2.sourcepackagename,
                    tags=set(),
                ),
            ],
            series_packages=[
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=dsp1.sourcepackagename,
                        distroseries=supported_series,
                    ),
                    package_name=dsp1.sourcepackagename,
                    importance=BugTaskImportance.MEDIUM,
                    status=BugTaskStatus.INVALID,
                    status_explanation="reason 1",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=dsp1.sourcepackagename,
                        distroseries=current_series,
                    ),
                    package_name=dsp1.sourcepackagename,
                    importance=BugTaskImportance.MEDIUM,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="reason 2",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=dsp1.sourcepackagename,
                        distroseries=devel_series,
                    ),
                    package_name=dsp1.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="reason 3",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=dsp2.sourcepackagename,
                        distroseries=supported_series,
                    ),
                    package_name=dsp2.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.DOESNOTEXIST,
                    status_explanation="",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=dsp2.sourcepackagename,
                        distroseries=current_series,
                    ),
                    package_name=dsp2.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.DOESNOTEXIST,
                    status_explanation="",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=dsp2.sourcepackagename,
                        distroseries=devel_series,
                    ),
                    package_name=dsp2.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="",
                ),
            ],
            upstream_packages=[
                CVE.UpstreamPackage(
                    target=product_1,
                    package_name=dsp1.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="reason 4",
                ),
                CVE.UpstreamPackage(
                    target=product_2,
                    package_name=dsp2.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="",
                ),
            ],
            importance=BugTaskImportance.CRITICAL,
            importance_explanation="sample priority_explanation",
            status=VulnerabilityStatus.ACTIVE,
            assignee=assignee,
            discovered_by="",
            description="description",
            ubuntu_description="ubuntu-description",
            bug_urls=["https://github.com/mm2/Little-CMS/issues/29"],
            references=["https://ubuntu.com/security/notices/USN-5368-1"],
            notes="author> text",
            mitigation="mitigation",
            cvss={
                "nvd": [
                    "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H "
                    "[7.8 HIGH]",
                ],
            },
            patch_urls=[
                CVE.PatchURL(
                    package_name=dsp1.sourcepackagename,
                    type="upstream",
                    url="https://github.com/389ds/389-ds-base/" "commit/123",
                    notes="1.4.4",
                ),
                CVE.PatchURL(
                    package_name=dsp1.sourcepackagename,
                    type="upstream",
                    url="https://github.com/389ds/389-ds-base/" "commit/456",
                    notes=None,
                ),
            ],
            break_fix_data=[
                CVE.BreakFix(
                    package_name=dsp1.sourcepackagename,
                    broken="457f44363a8894135c85b7a9afd2bd8196db24ab",
                    fixed=(
                        "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                        "local-CVE-2022-23222-fix"
                    ),
                ),
            ],
            global_tags={"cisa-kev"},
        )
        self.distribution = ubuntu
        self.distroseries = current_series

    def test_make_from_uct_record(self):
        cve = CVE.make_from_uct_record(self.uct_record)
        self.assertDictEqual(self.cve.__dict__, cve.__dict__)

    def test_to_uct_record(self):
        uct_record = self.cve.to_uct_record()
        self.assertListEqual(self.uct_record.packages, uct_record.packages)
        self.assertDictEqual(self.uct_record.__dict__, uct_record.__dict__)

    def test_get_distro_series_uses_cache(self):
        # Check that when calling `get_distro_series` for the same series, we
        # get the cached value instead of calling `queryByName` which accesses
        # the database
        cache_entities = CVE.new_cache()

        distribution_set = Mock()
        distribution_set.getByName.return_value = self.distribution
        distro_series_set = Mock()
        distro_series_set.queryByName.return_value = self.distroseries

        def mock_getUtility(interface):
            if interface is IDistributionSet:
                return distribution_set
            if interface is IDistroSeriesSet:
                return distro_series_set
            raise AssertionError(f"Unexpected utility requested: {interface}")

        with patch("lp.bugs.scripts.uct.models.getUtility", mock_getUtility):
            first_result = CVE.get_distro_series(
                "jammy", cache_entities=cache_entities
            )
            second_result = CVE.get_distro_series(
                "jammy", cache_entities=cache_entities
            )

        # Assert first result is equal to second result
        self.assertIs(first_result, self.distroseries)
        self.assertIs(second_result, self.distroseries)

        # Assert queryByName only called once for series and distribution
        self.assertEqual(1, distro_series_set.queryByName.call_count)
        distro_series_set.queryByName.assert_called_once_with(
            self.distribution, "jammy"
        )
        self.assertEqual(1, distribution_set.getByName.call_count)
        distribution_set.getByName.assert_called_once_with("ubuntu")

        # Assert entities exist in cache
        self.assertIn("ubuntu/jammy", cache_entities["distroseries"])
        self.assertIsNotNone(cache_entities["distroseries"]["ubuntu/jammy"])
        self.assertIn("ubuntu", cache_entities["distribution"])
        self.assertIsNotNone(cache_entities["distribution"]["ubuntu"])

    def test_get_distro_series_caches_missing_series(self):
        # Check we also cache nonexistent series so that we don't need to
        # check from the database again
        cache_entities = CVE.new_cache()

        distribution_set = Mock()
        distribution_set.getByName.return_value = self.distribution
        distro_series_set = Mock()
        distro_series_set.queryByName.return_value = None

        def mock_getUtility(interface):
            if interface is IDistributionSet:
                return distribution_set
            if interface is IDistroSeriesSet:
                return distro_series_set
            raise AssertionError(f"Unexpected utility requested: {interface}")

        with patch("lp.bugs.scripts.uct.models.getUtility", mock_getUtility):
            first_result = CVE.get_distro_series(
                "nonexistent", cache_entities=cache_entities
            )
            second_result = CVE.get_distro_series(
                "nonexistent", cache_entities=cache_entities
            )

        self.assertIsNone(first_result)
        self.assertIsNone(second_result)
        self.assertEqual(1, distro_series_set.queryByName.call_count)
        self.assertEqual(1, distribution_set.getByName.call_count)
        self.assertIn("ubuntu/nonexistent", cache_entities["distroseries"])
        self.assertIsNone(cache_entities["distroseries"]["ubuntu/nonexistent"])

    def test_get_distro_series_caches_distribution_lookup(self):
        # Check that when checking for different series within the same
        # distribution, we only fetch the distribution from the database once

        cache_entities = CVE.new_cache()

        distribution_set = Mock()
        distribution_set.getByName.return_value = self.distribution
        distro_series_set = Mock()
        distro_series_set.queryByName.return_value = self.distroseries

        def mock_getUtility(interface):
            if interface is IDistributionSet:
                return distribution_set
            if interface is IDistroSeriesSet:
                return distro_series_set
            raise AssertionError(f"Unexpected utility requested: {interface}")

        with patch("lp.bugs.scripts.uct.models.getUtility", mock_getUtility):
            CVE.get_distro_series("jammy", cache_entities=cache_entities)
            CVE.get_distro_series("noble", cache_entities=cache_entities)

        # Assert distribution.getByName only called once even if
        # distroseries.getByName called twice
        self.assertEqual(1, distribution_set.getByName.call_count)
        self.assertIs(
            self.distribution,
            cache_entities["distribution"]["ubuntu"],
        )
        self.assertEqual(2, distro_series_set.queryByName.call_count)

    def test_make_from_uct_record_caches_products(self):
        cache_entities = CVE.new_cache()

        CVE.make_from_uct_record(
            self.uct_record, cache_entities=cache_entities
        )

        product_cache = cache_entities["product"]
        self.assertEqual(2, len(product_cache))
        package_names = {package.name for package in self.uct_record.packages}
        self.assertEqual(
            {("ubuntu", package_name) for package_name in package_names},
            set(product_cache.keys()),
        )
        for product in product_cache.values():
            self.assertIsNotNone(product)

    def test_get_product_cache_key_includes_distribution(self):
        spn = self.factory.makeSourcePackageName(name="openssl")
        self.assertEqual(
            ("ubuntu", "openssl"),
            CVE._get_product_cache_key(spn, "ubuntu"),
        )
        self.assertEqual(
            ("ubuntu-esm", "openssl"),
            CVE._get_product_cache_key(spn, "ubuntu-esm"),
        )

    def test_get_patches(self):
        spn = self.factory.makeSourcePackageName()
        self.assertListEqual(
            [
                CVE.PatchURL(
                    package_name=spn,
                    url="https://github.com/repo/1",
                    type="upstream",
                    notes=None,
                ),
                CVE.PatchURL(
                    package_name=spn,
                    url="https://github.com/repo/2",
                    type="upstream",
                    notes="1.2.3",
                ),
            ],
            list(
                CVE.get_patch_urls(
                    spn,
                    [
                        UCTRecord.Patch("break-fix", "- -"),
                        UCTRecord.Patch(
                            "upstream", "https://github.com/repo/1"
                        ),
                        UCTRecord.Patch(
                            "upstream", "https://github.com/repo/2 (1.2.3)"
                        ),
                        UCTRecord.Patch("other", "foo"),
                    ],
                )
            ),
        )

    def test_get_break_fix(self):
        spn = self.factory.makeSourcePackageName()
        self.assertListEqual(
            [
                CVE.BreakFix(
                    package_name=spn,
                    broken="d2406291483775ecddaee929231a39c70c08fda2",
                    fixed="f64e67e5d3a45a4a04286c47afade4b518acd47b",
                ),
                CVE.BreakFix(
                    package_name=spn,
                    broken="-",
                    fixed="f2ef6f7539c68c6bd6c32323d8845ee102b7c450",
                ),
            ],
            list(
                CVE.get_break_fix(
                    spn,
                    [
                        UCTRecord.Patch(
                            "break-fix",
                            "d2406291483775ecddaee929231a39c70c08fda2 "
                            "f64e67e5d3a45a4a04286c47afade4b518acd47b",
                        ),
                        UCTRecord.Patch(
                            "break-fix",
                            "- f2ef6f7539c68c6bd6c32323d8845ee102b7c450",
                        ),
                        UCTRecord.Patch(
                            "upstream", "https://github.com/repo/2 (1.2.3)"
                        ),
                        UCTRecord.Patch("other", "foo"),
                    ],
                )
            ),
        )


class TestUCTImporterExporter(TestCaseWithFactory):
    maxDiff = None
    layer = ZopelessDatabaseLayer

    def setUp(self, *args, **kwargs):
        super().setUp(*args, **kwargs)
        celebrities = getUtility(ILaunchpadCelebrities)
        self.ubuntu = celebrities.ubuntu

        # Create ESM PPA
        self.ppa_owner = self.factory.makePerson(name="ubuntu-esm")
        self.ppa = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=self.ppa_owner,
            name="esm-infra-security",
        )
        self.ppa_apps = self.factory.makeArchive(
            distribution=self.ubuntu,
            owner=self.ppa_owner,
            name="esm-apps-security",
        )

        self.bug_importer = celebrities.bug_importer
        self.ubuntu_supported_series = self.factory.makeDistroSeries(
            distribution=self.ubuntu,
            status=SeriesStatus.SUPPORTED,
            name="focal",
        )
        self.ubuntu_current_series = self.factory.makeDistroSeries(
            distribution=self.ubuntu, status=SeriesStatus.CURRENT, name="jammy"
        )
        self.ubuntu_devel_series = self.factory.makeDistroSeries(
            distribution=self.ubuntu,
            status=SeriesStatus.DEVELOPMENT,
            name="kinetic",
        )
        self.ubuntu_package = self.factory.makeDistributionSourcePackage(
            distribution=self.ubuntu
        )

        # Create ESM package in PPA
        self.esm_package = self.factory.makeArchiveSourcePackage(
            archive=self.ppa,
        )
        self.esm_ppa_asps_supported = (
            self.factory.makeArchiveSourcePackageSeries(
                sourcepackagename=self.esm_package.sourcepackagename,
                archive=self.ppa,
                distroseries=self.ubuntu_supported_series,
            )
        )
        self.esm_ppa_asps_current = (
            self.factory.makeArchiveSourcePackageSeries(
                sourcepackagename=self.esm_package.sourcepackagename,
                archive=self.ppa_apps,
                distroseries=self.ubuntu_current_series,
            )
        )
        self.esm_package_apps = self.factory.makeArchiveSourcePackage(
            sourcepackagename=self.esm_package.sourcepackagename,
            archive=self.ppa_apps,
        )

        self.product_1 = self.factory.makePackagingLink(
            sourcepackagename=self.ubuntu_package.sourcepackagename,
            distroseries=self.ubuntu_current_series,
        ).productseries.product
        self.esm_dsp = self.factory.makeDistributionSourcePackage(
            sourcepackagename=self.esm_package.sourcepackagename,
            distribution=self.ubuntu,
        )
        self.product_2 = self.factory.makePackagingLink(
            sourcepackagename=self.esm_package.sourcepackagename,
            distroseries=self.ubuntu_current_series,
        ).productseries.product

        # Create another PPA package for testing
        self._ppa_asp = self.factory.makeArchiveSourcePackage(archive=self.ppa)
        self._ppa_asps = self.factory.makeArchiveSourcePackageSeries(
            sourcepackagename=self._ppa_asp.sourcepackagename,
            archive=self.ppa,
            distroseries=self.ubuntu_supported_series,
        )
        self._ppa_dsp = self.factory.makeDistributionSourcePackage(
            sourcepackagename=self._ppa_asp.sourcepackagename,
            distribution=self.ubuntu,
        )
        self.product_3 = self.factory.makePackagingLink(
            sourcepackagename=self._ppa_asp.sourcepackagename,
            distroseries=self.ubuntu_current_series,
        ).productseries.product

        for series in (
            self.ubuntu_supported_series,
            self.ubuntu_current_series,
        ):
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=series,
                sourcepackagerelease=self.factory.makeSourcePackageRelease(
                    distroseries=series,
                    sourcepackagename=self.ubuntu_package.sourcepackagename,
                ),
            )
            # Publish esm_package and _ppa_asp so their DSPs are valid
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=series,
                sourcepackagerelease=self.factory.makeSourcePackageRelease(
                    distroseries=series,
                    sourcepackagename=self.esm_package.sourcepackagename,
                ),
            )
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=series,
                sourcepackagerelease=self.factory.makeSourcePackageRelease(
                    distroseries=series,
                    sourcepackagename=self._ppa_asp.sourcepackagename,
                ),
            )

        # Set up subprojects for PPA export
        self.subprojects = {
            "esm-infra/focal": SubProjectPPAs(
                ubuntu_series="focal",
                ppa=PPAReference(
                    owner=self.ppa_owner.name,
                    archive=self.ppa.name,
                    pocket="security",
                ),
            ),
            "esm-apps/jammy": SubProjectPPAs(
                ubuntu_series="jammy",
                ppa=PPAReference(
                    owner=self.ppa_owner.name,
                    archive=self.ppa_apps.name,
                    pocket="security",
                ),
            ),
        }

        # Mock get_devel_series to return our specific test development series
        # This avoids modifying database state and ensures consistent test
        # behavior without side effects on other tests
        patcher = patch.object(
            CVE, "get_devel_series", return_value=self.ubuntu_devel_series
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        assignee = self.factory.makePerson()
        self.lp_cve = self.factory.makeCVE("2022-23222")
        self.cve = CVE(
            sequence="CVE-2022-23222",
            date_made_public=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            date_notice_issued=datetime(
                2021, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            date_coordinated_release=datetime(
                2022, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            distro_packages=[
                CVE.DistroPackage(
                    target=self.ubuntu_package,
                    importance=BugTaskImportance.LOW,
                    package_name=self.ubuntu_package.sourcepackagename,
                    tags={"review-break-fix"},
                ),
            ],
            series_packages=[
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=self.ubuntu_package.sourcepackagename,  # noqa: E501
                        distroseries=self.ubuntu_supported_series,
                    ),
                    package_name=self.ubuntu_package.sourcepackagename,
                    importance=BugTaskImportance.HIGH,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="2.56+dfsg-1",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=self.ubuntu_package.sourcepackagename,  # noqa: E501
                        distroseries=self.ubuntu_current_series,
                    ),
                    package_name=self.ubuntu_package.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.DOESNOTEXIST,
                    status_explanation="does not exist",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=self.ubuntu_package.sourcepackagename,  # noqa: E501
                        distroseries=self.ubuntu_devel_series,
                    ),
                    package_name=self.ubuntu_package.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.INVALID,
                    status_explanation="not affected",
                ),
            ],
            upstream_packages=[
                CVE.UpstreamPackage(
                    target=self.product_1,
                    package_name=self.ubuntu_package.sourcepackagename,
                    importance=BugTaskImportance.HIGH,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="fix released",
                ),
                CVE.UpstreamPackage(
                    target=self.product_2,
                    package_name=self.esm_package.sourcepackagename,
                    importance=BugTaskImportance.LOW,
                    status=BugTaskStatus.WONTFIX,
                    status_explanation="ignored",
                ),
                CVE.UpstreamPackage(
                    target=self.product_3,
                    package_name=self._ppa_asp.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="released",
                ),
            ],
            importance=BugTaskImportance.MEDIUM,
            status=VulnerabilityStatus.ACTIVE,
            assignee=assignee,
            discovered_by="",
            description="description",
            ubuntu_description="ubuntu-description",
            bug_urls=["https://github.com/mm2/Little-CMS/issues/29"],
            references=["https://ubuntu.com/security/notices/USN-5368-1"],
            notes="author> text",
            mitigation="mitigation",
            cvss={
                "nvd": [
                    "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H "
                    "[7.8 HIGH]",
                ],
            },
            patch_urls=[
                CVE.PatchURL(
                    package_name=self.ubuntu_package.sourcepackagename,
                    type="upstream",
                    url="https://github.com/389ds/389-ds-base/" "commit/123",
                    notes="1.4.4",
                ),
                CVE.PatchURL(
                    package_name=self.esm_package.sourcepackagename,
                    type="upstream",
                    url="https://github.com/389ds/389-ds-base/" "commit/456",
                    notes=None,
                ),
            ],
            break_fix_data=[
                CVE.BreakFix(
                    package_name=self.ubuntu_package.sourcepackagename,
                    broken="457f44363a8894135c85b7a9afd2bd8196db24ab",
                    fixed=(
                        "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                        "local-CVE-2022-23222-fix"
                    ),
                ),
                CVE.BreakFix(
                    package_name=self.esm_package.sourcepackagename,
                    broken="457f44363a8894135c85b7a9afd2bd8196db24ab",
                    fixed=(
                        "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                        "local-CVE-2022-23222-fix"
                    ),
                ),
            ],
            global_tags={"cisa-kev"},
            ppa_packages=[
                CVE.PPAPackage(
                    target=self._ppa_asp,
                    package_name=self._ppa_asp.sourcepackagename,
                    importance=BugTaskImportance.LOW,
                    tags=set(),
                ),
                CVE.PPAPackage(
                    target=self.esm_package_apps,
                    package_name=self.esm_package_apps.sourcepackagename,
                    importance=None,
                    tags={"universe-binary"},
                ),
                CVE.PPAPackage(
                    target=self.esm_package,
                    package_name=self.esm_package.sourcepackagename,
                    importance=None,
                    tags={"universe-binary"},
                ),
            ],
            ppa_series_packages=[
                CVE.PPASeriesPackage(
                    target=self._ppa_asps,
                    package_name=self._ppa_asp.sourcepackagename,
                    importance=BugTaskImportance.HIGH,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="1.2.3-4ubuntu5",
                ),
                CVE.PPASeriesPackage(
                    target=self.esm_ppa_asps_supported,
                    package_name=self.esm_package.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.WONTFIX,
                    status_explanation="ignored",
                ),
                CVE.PPASeriesPackage(
                    target=self.esm_ppa_asps_current,
                    package_name=self.esm_package.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.UNKNOWN,
                    status_explanation="needs triage",
                ),
            ],
        )

        self.uct_record = UCTRecord(
            assigned_to=assignee.name,
            bugs=["https://github.com/mm2/Little-CMS/issues/29"],
            candidate="CVE-2022-23222",
            crd=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            cvss={
                "nvd": [
                    "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H "
                    "[7.8 HIGH]",
                ]
            },
            description="description",
            discovered_by="",
            global_tags={"cisa-kev"},
            mitigation="mitigation",
            notes="author> text",
            packages=[
                UCTRecord.Package(
                    name=self.ubuntu_package.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="focal",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="2.56+dfsg-1",
                            priority=UCTRecord.Priority.HIGH,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="jammy",
                            status=UCTRecord.PackageStatus.DOES_NOT_EXIST,
                            reason="does not exist",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="devel",
                            status=UCTRecord.PackageStatus.NOT_AFFECTED,
                            reason="not affected",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="fix released",
                            priority=UCTRecord.Priority.HIGH,
                        ),
                    ],
                    priority=UCTRecord.Priority.LOW,
                    tags={"review-break-fix"},
                    patches=[
                        UCTRecord.Patch(
                            patch_type="upstream",
                            entry=(
                                "https://github.com/389ds/389-ds-base/commit/123 (1.4.4)"  # noqa: E501
                            ),
                        ),
                        UCTRecord.Patch(
                            patch_type="break-fix",
                            entry=(
                                "457f44363a8894135c85b7a9afd2bd8196db24ab "
                                "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                                "local-CVE-2022-23222-fix"
                            ),
                        ),
                    ],
                ),
                UCTRecord.Package(
                    name=self.esm_package.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="esm-apps/jammy",
                            status=UCTRecord.PackageStatus.NEEDS_TRIAGE,
                            reason="needs triage",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="esm-infra/focal",
                            status=UCTRecord.PackageStatus.IGNORED,
                            reason="ignored",
                            priority=None,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.IGNORED,
                            reason="ignored",
                            priority=UCTRecord.Priority.LOW,
                        ),
                    ],
                    priority=None,
                    tags={"universe-binary"},
                    patches=[
                        UCTRecord.Patch(
                            patch_type="upstream",
                            entry=(
                                "https://github.com/389ds/389-ds-base/commit/456"  # noqa: E501
                            ),
                        ),
                        UCTRecord.Patch(
                            patch_type="break-fix",
                            entry=(
                                "457f44363a8894135c85b7a9afd2bd8196db24ab "
                                "c25b2ae136039ffa820c26138ed4a5e5f3ab3841|"
                                "local-CVE-2022-23222-fix"
                            ),
                        ),
                    ],
                ),
                UCTRecord.Package(
                    name=self._ppa_asp.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="esm-infra/focal",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="1.2.3-4ubuntu5",
                            priority=UCTRecord.Priority.HIGH,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="upstream",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="released",
                            priority=None,
                        ),
                    ],
                    priority=UCTRecord.Priority.LOW,
                    tags=set(),
                    patches=[],
                ),
            ],
            parent_dir="active",
            priority=UCTRecord.Priority.MEDIUM,
            priority_explanation="",
            public_date=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            public_date_at_USN=datetime(
                2021, 1, 14, 8, 15, tzinfo=timezone.utc
            ),
            references=["https://ubuntu.com/security/notices/USN-5368-1"],
            ubuntu_description="ubuntu-description",
        )

        self.importer = UCTImporter(self.ubuntu)
        self.exporter = UCTExporter()

    def checkBug(self, bug: Bug, cve: CVE):
        self.assertEqual(cve.sequence, bug.title)
        self.assertEqual(self.bug_importer, bug.owner)
        self.assertEqual(InformationType.PUBLICSECURITY, bug.information_type)

        expected_description = cve.description
        if cve.references:
            expected_description = "{}\n\nReferences:\n{}".format(
                expected_description, "\n".join(cve.references)
            )
        self.assertEqual(expected_description, bug.description)

        watches = list(bug.watches)
        self.assertEqual(len(cve.bug_urls), len(watches))
        self.assertEqual(sorted(cve.bug_urls), sorted(w.url for w in watches))

        self.checkBugTags(bug, cve)
        self.checkBugAttachments(bug, cve)
        self.checkBugPresences(bug, cve)

    def checkBugTags(self, bug: Bug, cve: CVE):
        tags = cve.global_tags.copy()
        for distro_package in cve.distro_packages:
            for tag in distro_package.tags:
                tags.add(
                    f"{distro_package.package_name.name}{TAG_SEPARATOR}{tag}"
                )
        for ppa_package in cve.ppa_packages:
            for tag in ppa_package.tags:
                tags.add(
                    f"{ppa_package.package_name.name}{TAG_SEPARATOR}{tag}"
                )
        self.assertEqual(sorted(bug.tags), sorted(list(tags)))

    def checkBugTasks(self, bug: Bug, cve: CVE):
        bug_tasks: List[BugTask] = bug.bugtasks

        self.assertEqual(
            len(cve.distro_packages)
            + len(cve.series_packages)
            + len(cve.upstream_packages)
            + len(cve.ppa_packages)
            + len(cve.ppa_series_packages),
            len(bug_tasks),
        )
        bug_tasks_by_target = {t.target: t for t in bug_tasks}

        package_importances = {}

        tags = set()

        for distro_package in cve.distro_packages:
            self.assertIn(distro_package.target, bug_tasks_by_target)
            t = bug_tasks_by_target[distro_package.target]
            package_importance = distro_package.importance or cve.importance
            package_importances[
                distro_package.target.sourcepackagename.name
            ] = package_importance
            conjoined_primary = t.conjoined_primary
            if conjoined_primary:
                expected_importance = conjoined_primary.importance
                expected_status = conjoined_primary.status
            else:
                expected_importance = package_importance
                expected_status = BugTaskStatus.NEW

            for tag in distro_package.tags:
                tags.add(f"{distro_package.package_name.name}.{tag}")

            self.assertEqual(expected_importance, t.importance)
            self.assertEqual(expected_status, t.status)
            self.assertIsNone(t.status_explanation)

        # Add PPA package tags to the expected tags set
        for ppa_package in cve.ppa_packages:
            for tag in ppa_package.tags:
                tags.add(f"{ppa_package.package_name.name}.{tag}")

        package_tags = {tag for tag in bug.tags if TAG_SEPARATOR in tag}
        self.assertEqual(tags, package_tags)

        for series_package in cve.series_packages:
            self.assertIn(series_package.target, bug_tasks_by_target)
            t = bug_tasks_by_target[series_package.target]
            package_importance = package_importances[
                series_package.target.sourcepackagename.name
            ]
            sp_importance = series_package.importance or package_importance
            self.assertEqual(sp_importance, t.importance)
            self.assertEqual(series_package.status, t.status)
            self.assertEqual(
                series_package.status_explanation, t.status_explanation
            )

        # Process PPA packages before upstream packages so package_importances
        # is populated for packages that only exist in PPAs
        for ppa_package in cve.ppa_packages:
            self.assertIn(ppa_package.target, bug_tasks_by_target)
            t = bug_tasks_by_target[ppa_package.target]
            pp_importance = ppa_package.importance or cve.importance
            package_importances[ppa_package.package_name.name] = pp_importance
            self.assertEqual(pp_importance, t.importance)

        for ppa_series_package in cve.ppa_series_packages:
            self.assertIn(ppa_series_package.target, bug_tasks_by_target)
            t = bug_tasks_by_target[ppa_series_package.target]
            package_importance = package_importances[
                ppa_series_package.package_name.name
            ]
            psp_importance = (
                ppa_series_package.importance or package_importance
            )
            self.assertEqual(psp_importance, t.importance)
            self.assertEqual(ppa_series_package.status, t.status)
            self.assertEqual(
                ppa_series_package.status_explanation, t.status_explanation
            )

        for upstream_package in cve.upstream_packages:
            self.assertIn(upstream_package.target, bug_tasks_by_target)
            t = bug_tasks_by_target[upstream_package.target]
            package_importance = package_importances[
                upstream_package.package_name.name
            ]
            sp_importance = upstream_package.importance or package_importance
            self.assertEqual(sp_importance, t.importance)
            self.assertEqual(upstream_package.status, t.status)
            self.assertEqual(
                upstream_package.status_explanation, t.status_explanation
            )

        for t in bug_tasks:
            self.assertEqual(cve.assignee, t.assignee)

    def checkBugPresences(self, bug: Bug, cve: CVE):
        presences_by_pkg = {
            presence.source_package_name: presence
            for presence in bug.presences
        }
        break_fix_by_pkg = defaultdict(list)
        for break_fix in cve.break_fix_data:
            break_fix_by_pkg[break_fix.package_name].append(break_fix)

        self.assertEqual(
            len(list(bug.presences)),
            len(break_fix_by_pkg),
            "Mismatch in presences count and break_fix count",
        )

        for package, break_fix_data in break_fix_by_pkg.items():
            presence = presences_by_pkg.get(package)

            self.assertIsNotNone(
                presence, f"Presence for package '{package}' not found"
            )

            self.assertEqual(package, presence.source_package_name)
            self.assertEqual(
                len(break_fix_data),
                len(presence.break_fix_data),
                "Number of break_fix_data don't match for package "
                f"'{package}'",
            )

            # Check content and its order
            for break_fix, presence_break_fix in zip(
                break_fix_data, presence.break_fix_data
            ):
                self.assertEqual(
                    break_fix.broken,
                    presence_break_fix["break"],
                    f"Break mismatch for patch in package '{package}'",
                )
                self.assertEqual(
                    break_fix.fixed,
                    presence_break_fix["fix"],
                    f"Fix mismatch for patch in package '{package}'",
                )

    def checkBugAttachments(self, bug: Bug, cve: CVE):
        # attachment.title is the package name
        attachments_by_pkg = {att.title: att for att in bug.attachments}
        patch_url_by_pkg = defaultdict(list)
        for patch_url in cve.patch_urls:
            patch_url_by_pkg[patch_url.package_name.name].append(patch_url)

        self.assertEqual(
            len(attachments_by_pkg),
            len(patch_url_by_pkg),
            "Mismatch in attachment count and patch URL count",
        )

        for pkg, patch_urls in patch_url_by_pkg.items():
            attachment = attachments_by_pkg.get(pkg)
            self.assertIsNotNone(
                attachment, f"Attachment for package '{pkg}' not found"
            )

            self.assertEqual(pkg, attachment.title)

            vulnerability_patches = attachment.vulnerability_patches
            self.assertEqual(
                len(patch_urls),
                len(vulnerability_patches),
                "Number of patches and vulnerabilities don't match for "
                f"package '{pkg}'",
            )

            # Check content and its order
            for patch_url, vulnerability_patch in zip(
                patch_urls, vulnerability_patches
            ):
                self.assertEqual(
                    patch_url.type,
                    vulnerability_patch["name"],
                    f"Type mismatch for patch in package '{pkg}'",
                )
                self.assertEqual(
                    patch_url.url,
                    vulnerability_patch["value"],
                    f"URL mismatch for patch in package '{pkg}'",
                )
                self.assertEqual(
                    patch_url.notes,
                    vulnerability_patch["comment"],
                    f"Notes mismatch for patch in package '{pkg}'",
                )

    def checkVulnerabilities(self, bug: Bug, cve: CVE):
        vulnerabilities = bug.vulnerabilities

        self.assertEqual(1, len(vulnerabilities))

        vulnerabilities_by_distro = {
            v.distribution: v for v in vulnerabilities
        }

        distro = self.ubuntu
        self.assertIn(distro, vulnerabilities_by_distro)
        vulnerability = vulnerabilities_by_distro[distro]

        self.assertEqual(self.bug_importer, vulnerability.creator)
        self.assertEqual(self.lp_cve, vulnerability.cve)
        self.assertEqual(cve.status, vulnerability.status)
        self.assertEqual(cve.ubuntu_description, vulnerability.description)
        self.assertEqual(cve.notes, vulnerability.notes)
        self.assertEqual(cve.mitigation, vulnerability.mitigation)
        self.assertEqual(cve.importance, vulnerability.importance)
        self.assertEqual(
            InformationType.PUBLICSECURITY, vulnerability.information_type
        )
        self.assertEqual(cve.date_made_public, vulnerability.date_made_public)
        self.assertEqual(
            cve.date_notice_issued, vulnerability.date_notice_issued
        )
        self.assertEqual(
            cve.date_coordinated_release,
            vulnerability.date_coordinated_release,
        )
        self.assertEqual([bug], vulnerability.bugs)
        self.assertEqual(vulnerability.cvss, cve.cvss)

    def checkCVE(self, expected: CVE, actual: CVE):
        self.assertEqual(expected.sequence, actual.sequence)
        self.assertEqual(expected.date_made_public, actual.date_made_public)
        self.assertEqual(
            expected.date_notice_issued, actual.date_notice_issued
        )
        self.assertEqual(
            expected.date_coordinated_release, actual.date_coordinated_release
        )
        self.assertListEqual(expected.distro_packages, actual.distro_packages)
        self.assertListEqual(expected.series_packages, actual.series_packages)
        self.assertListEqual(
            sorted(
                expected.upstream_packages, key=lambda x: x.package_name.name
            ),
            sorted(
                actual.upstream_packages, key=lambda x: x.package_name.name
            ),
        )
        self.assertListEqual(
            sorted(
                expected.ppa_packages,
                key=lambda x: (x.package_name.name, x.target.archive.name),
            ),
            sorted(
                actual.ppa_packages,
                key=lambda x: (x.package_name.name, x.target.archive.name),
            ),
        )
        self.assertListEqual(
            sorted(
                expected.ppa_series_packages,
                key=lambda x: (
                    x.package_name.name,
                    x.target.archive.name,
                    x.target.distroseries.name,
                ),
            ),
            sorted(
                actual.ppa_series_packages,
                key=lambda x: (
                    x.package_name.name,
                    x.target.archive.name,
                    x.target.distroseries.name,
                ),
            ),
        )
        self.assertEqual(expected.importance, actual.importance)
        self.assertEqual(expected.status, actual.status)
        self.assertEqual(expected.assignee, actual.assignee)
        self.assertEqual(expected.discovered_by, actual.discovered_by)
        self.assertEqual(expected.description, actual.description)
        self.assertEqual(
            expected.ubuntu_description, actual.ubuntu_description
        )
        self.assertListEqual(expected.bug_urls, actual.bug_urls)
        self.assertListEqual(expected.references, actual.references)
        self.assertEqual(expected.notes, actual.notes)
        self.assertEqual(expected.mitigation, actual.mitigation)
        self.assertEqual(expected.cvss, actual.cvss)
        self.assertListEqual(expected.patch_urls, actual.patch_urls)
        self.assertListEqual(expected.break_fix_data, actual.break_fix_data)
        self.assertEqual(expected.global_tags, actual.global_tags)

    def test_create_bug(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)

        self.checkBug(bug, self.cve)
        self.checkBugTasks(bug, self.cve)
        self.checkVulnerabilities(bug, self.cve)

        self.assertEqual([self.lp_cve], bug.cves)

        activities = list(bug.activity)
        self.assertEqual(8, len(activities))
        import_bug_activity = activities[-1]
        self.assertEqual(self.bug_importer, import_bug_activity.person)
        self.assertEqual("bug", import_bug_activity.whatchanged)
        self.assertEqual(
            "UCT CVE entry CVE-2022-23222", import_bug_activity.message
        )

    def test_create_bug_distribution_has_published_sources_false(self):
        distribution = self.factory.makeDistribution(
            name="no-published-sources"
        )
        self.assertFalse(distribution.has_published_sources)
        supported_series = self.factory.makeDistroSeries(
            distribution=distribution,
            status=SeriesStatus.SUPPORTED,
            name="supported-series",
        )
        current_series = self.factory.makeDistroSeries(
            distribution=distribution,
            status=SeriesStatus.CURRENT,
            name="current-series",
        )
        affected_package = self.factory.makeDistributionSourcePackage(
            distribution=distribution
        )
        cve = CVE(
            sequence="CVE-2022-1234",
            date_made_public=datetime(2022, 1, 1, 8, 15, tzinfo=timezone.utc),
            date_notice_issued=datetime(
                2021, 1, 1, 8, 15, tzinfo=timezone.utc
            ),
            date_coordinated_release=datetime(
                2020, 1, 1, 8, 15, tzinfo=timezone.utc
            ),
            distro_packages=[
                CVE.DistroPackage(
                    target=affected_package,
                    importance=BugTaskImportance.LOW,
                    package_name=affected_package.sourcepackagename,
                    tags={"universe-binary", "not-ue"},
                ),
            ],
            series_packages=[
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=affected_package.sourcepackagename,
                        distroseries=supported_series,
                    ),
                    package_name=affected_package.sourcepackagename,
                    importance=BugTaskImportance.HIGH,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="released",
                ),
                CVE.SeriesPackage(
                    target=SourcePackage(
                        sourcepackagename=affected_package.sourcepackagename,
                        distroseries=current_series,
                    ),
                    package_name=affected_package.sourcepackagename,
                    importance=None,
                    status=BugTaskStatus.DOESNOTEXIST,
                    status_explanation="does not exist",
                ),
            ],
            upstream_packages=[],
            importance=BugTaskImportance.MEDIUM,
            status=VulnerabilityStatus.ACTIVE,
            assignee=self.factory.makePerson(),
            discovered_by="",
            description="description",
            ubuntu_description="ubuntu-description",
            bug_urls=["https://github.com/mm2/Little-CMS/issues/29"],
            references=["https://ubuntu.com/security/notices/USN-5368-1"],
            notes="author> text",
            mitigation="mitigation",
            cvss={
                "nvd": [
                    "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H "
                    "[7.8 HIGH]",
                ],
            },
            patch_urls=[],
            break_fix_data=[],
            global_tags={"cisa-kev"},
        )
        lp_cve = self.factory.makeCVE(sequence="2022-1234")
        bug, _ = self.importer.create_bug(cve, lp_cve)
        self.checkBug(bug, cve)
        self.checkBugTasks(bug, cve)
        self.assertEqual([lp_cve], bug.cves)

    def test_find_existing_bug(self):
        self.assertIsNone(
            self.importer._find_existing_bug(self.lp_cve, self.ubuntu)
        )
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        self.assertEqual(
            self.importer._find_existing_bug(self.lp_cve, self.ubuntu),
            bug,
        )

    def test_find_existing_bug_multiple_bugs(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        another_bug = self.factory.makeBug(bug.bugtasks[0].target)
        self.assertEqual(len(bug.vulnerabilities), 1)
        vulnerability = bug.vulnerabilities[0]

        # Link another_bug so same vulnerability has more than one
        vulnerability.linkBug(another_bug)
        self.assertRaises(
            UCTImportError,
            self.importer._find_existing_bug,
            self.lp_cve,
            self.ubuntu,
        )

    def test_update_bug_new_package(self):
        package = self.factory.makeDistributionSourcePackage(
            distribution=self.ubuntu
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=self.ubuntu_current_series,
            sourcepackagerelease=self.factory.makeSourcePackageRelease(
                distroseries=self.ubuntu_current_series,
                sourcepackagename=package.sourcepackagename,
            ),
        )

        cve = self.cve
        bug, _ = self.importer.create_bug(cve, self.lp_cve)

        cve.distro_packages.append(
            CVE.DistroPackage(
                target=package,
                package_name=package.sourcepackagename,
                importance=BugTaskImportance.HIGH,
                tags={"review-break-fix"},
            )
        )
        cve.series_packages.append(
            CVE.SeriesPackage(
                target=SourcePackage(
                    sourcepackagename=package.sourcepackagename,
                    distroseries=self.ubuntu_current_series,
                ),
                package_name=package.sourcepackagename,
                importance=BugTaskImportance.CRITICAL,
                status=BugTaskStatus.FIXRELEASED,
                status_explanation="fix released",
            )
        )
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBugTasks(bug, cve)

    def test_update_bug_new_series(self):
        new_series = self.factory.makeDistroSeries(
            distribution=self.ubuntu, status=SeriesStatus.SUPPORTED
        )

        cve = self.cve
        bug, _ = self.importer.create_bug(cve, self.lp_cve)

        cve.series_packages.append(
            CVE.SeriesPackage(
                target=SourcePackage(
                    sourcepackagename=self.ubuntu_package.sourcepackagename,
                    distroseries=new_series,
                ),
                package_name=self.ubuntu_package.sourcepackagename,
                importance=BugTaskImportance.CRITICAL,
                status=BugTaskStatus.FIXRELEASED,
                status_explanation="fix released",
            )
        )
        cve.ppa_series_packages.append(
            CVE.PPASeriesPackage(
                target=self.factory.makeArchiveSourcePackageSeries(
                    sourcepackagename=self.esm_package.sourcepackagename,
                    archive=self.ppa,
                    distroseries=new_series,
                ),
                package_name=self.esm_package.sourcepackagename,
                importance=BugTaskImportance.HIGH,
                status=BugTaskStatus.FIXRELEASED,
                status_explanation="fix released in ppa",
            )
        )
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBugTasks(bug, cve)

    def test_update_bug_new_distro(self):
        new_distro = self.factory.makeDistribution(name="new-distro")
        new_series = self.factory.makeDistroSeries(
            distribution=new_distro, status=SeriesStatus.SUPPORTED
        )
        new_dsp = self.factory.makeDistributionSourcePackage(
            self.ubuntu_package.sourcepackagename, distribution=new_distro
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=new_series,
            sourcepackagerelease=self.factory.makeSourcePackageRelease(
                distroseries=new_series,
                sourcepackagename=new_dsp.sourcepackagename,
            ),
        )

        cve = self.cve
        bug, _ = self.importer.create_bug(cve, self.lp_cve)

        cve.distro_packages.append(
            CVE.DistroPackage(
                target=new_dsp,
                package_name=new_dsp.sourcepackagename,
                importance=BugTaskImportance.HIGH,
                tags={"not-ue"},
            )
        )
        cve.series_packages.append(
            CVE.SeriesPackage(
                target=SourcePackage(
                    sourcepackagename=new_dsp.sourcepackagename,
                    distroseries=new_series,
                ),
                package_name=new_dsp.sourcepackagename,
                importance=BugTaskImportance.CRITICAL,
                status=BugTaskStatus.FIXRELEASED,
                status_explanation="fix released",
            )
        )
        clear_property_cache(cve)

        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBugTasks(bug, cve)
        self.checkVulnerabilities(bug, cve)

    def test_update_bug_assignee_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve
        cve.assignee = self.factory.makePerson()
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBugTasks(bug, cve)

    def test_update_bug_cve_importance_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve
        self.assertNotEqual(cve.importance, BugTaskImportance.CRITICAL)
        cve.importance = BugTaskImportance.CRITICAL
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkVulnerabilities(bug, cve)

    def test_update_bug_cve_status_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve
        self.assertNotEqual(cve.status, VulnerabilityStatus.IGNORED)
        cve.status = VulnerabilityStatus.IGNORED
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkVulnerabilities(bug, cve)

    def test_update_bug_package_importance_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve
        self.assertNotEqual(
            cve.distro_packages[0].importance, BugTaskImportance.CRITICAL
        )
        self.assertNotEqual(
            cve.series_packages[0].importance, BugTaskImportance.CRITICAL
        )
        cve.distro_packages[0] = cve.distro_packages[0]._replace(
            importance=BugTaskImportance.CRITICAL,
        )
        cve.series_packages[0] = cve.series_packages[0]._replace(
            importance=BugTaskImportance.CRITICAL,
        )
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBugTasks(bug, cve)

    def test_update_bug_package_status_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve
        self.assertNotEqual(
            cve.series_packages[0].status, BugTaskStatus.DOESNOTEXIST
        )
        self.assertNotEqual(
            cve.series_packages[0].status_explanation, "does not exist"
        )
        cve.series_packages[0] = cve.series_packages[0]._replace(
            status=BugTaskStatus.DOESNOTEXIST,
            status_explanation="does not exist",
        )
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBugTasks(bug, cve)

    def test_update_bug_external_bugs_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        # Add new URL
        cve.bug_urls.append("https://github.com/mm2/Little-CMS/issues/29123")
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

        # Remove URL
        cve.bug_urls.pop(0)
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_update_bug_global_tags_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        cve.global_tags.add("another-tag")
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_update_bug_ubuntu_description_changed(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        cve.ubuntu_description += "new"
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_update_bug_references(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        # Add new URL
        cve.references.append("https://github.com/mm2/Little-CMS/issues/29123")
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

        # Remove URL
        cve.references.pop(0)
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_update_patch_urls(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        # Add new patch URL
        cve.patch_urls.append(
            CVE.PatchURL(
                package_name=cve.distro_packages[0].package_name,
                type="upstream",
                url="https://github.com/123",
                notes=None,
            )
        )
        cve.patch_urls.append(
            CVE.PatchURL(
                package_name=cve.ppa_packages[0].package_name,
                type="upstream",
                url="https://github.com/012",
                notes=None,
            )
        )

        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

        # Remove patch_url and check it removes from bug
        cve.patch_urls.pop()

        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_update_break_fix(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        # Add new break_fix
        cve.break_fix_data.append(
            CVE.BreakFix(
                package_name=cve.distro_packages[0].package_name,
                broken="d2406291483775ecddaee929231a39c70c08fda2",
                fixed=(
                    "f64e67e5d3a45a4a04286c47afade4b518acd47b"
                    "|cc8c837cf1b2f714dda723541c04acd1b8922d92"
                ),
            ),
        )
        cve.break_fix_data.append(
            CVE.BreakFix(
                package_name=cve.ppa_packages[0].package_name,
                broken="-",
                fixed="cffe487026be13eaf37ea28b783d9638ab147204",
            ),
        )
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

        # Remove break_fix and check if it removes from bug
        cve.break_fix_data.pop()
        cve.break_fix_data.pop()
        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_update_tags(self):
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)
        cve = self.cve

        # Add new tags
        cve.global_tags.add("global-test-tag")
        cve.distro_packages[0].tags.add("package-test-tag")
        cve.distro_packages[0].tags.add("another-package-test-tag")

        self.importer.update_bug(bug, cve, self.lp_cve)
        self.checkBug(bug, cve)

    def test_import_cve(self):
        self.importer.import_cve(self.cve)
        self.assertIsNotNone(
            self.importer._find_existing_bug(self.lp_cve, self.ubuntu)
        )

    def test_import_cve_dry_run(self):
        importer = UCTImporter(self.ubuntu, dry_run=True)
        importer.import_cve(self.cve)
        self.assertIsNone(
            importer._find_existing_bug(self.lp_cve, self.ubuntu)
        )

    def test_separate_importers_use_independent_caches(self):
        # Check that 2 different importer runs use 2 different caches
        importer_1 = UCTImporter(self.ubuntu, dry_run=True)
        importer_2 = UCTImporter(self.ubuntu, dry_run=True)

        self.assertIsNot(importer_1.cache_entities, importer_2.cache_entities)

        def mock_make_from_uct_record(
            record, cache_entities=None, subprojects=None
        ):
            self.assertIsNotNone(cache_entities)
            cache_entities["distribution"]["ubuntu"] = object()
            return Mock(sequence="CVE-mismatch")

        with patch(
            "lp.bugs.scripts.uct.uctimport.CVE.make_from_uct_record",
            side_effect=mock_make_from_uct_record,
        ):
            # An entity in cache 1 from importer 1 doesn't show in importer 2
            importer_1.from_record(Mock(), "CVE-2022-23222")
            self.assertIn("ubuntu", importer_1.cache_entities["distribution"])
            self.assertNotIn(
                "ubuntu", importer_2.cache_entities["distribution"]
            )
            importer_2.from_record(Mock(), "CVE-2022-23222")

        # Entities in cache 1 are not the same as the ones from cache 2
        # even if they point to the same object
        self.assertIsNot(
            importer_1.cache_entities["distribution"]["ubuntu"],
            importer_2.cache_entities["distribution"]["ubuntu"],
        )

    def test_same_importer_uses_same_cache_across_imports(self):
        # Check that a single importer instance reuses the same cache
        # across multiple CVE imports
        importer = UCTImporter(self.ubuntu, dry_run=True)
        cache_ref = importer.cache_entities

        # Track cache modifications across imports
        cached_entries = []

        def mock_make_from_uct_record(
            record, cache_entities=None, subprojects=None
        ):
            cached_entries.append(cache_entities)
            return Mock(sequence=record.sequence)

        with patch(
            "lp.bugs.scripts.uct.uctimport.CVE.make_from_uct_record",
            side_effect=mock_make_from_uct_record,
        ):
            # First import
            importer.from_record(
                Mock(sequence="CVE-2022-00001"), "CVE-2022-00001"
            )
            # Second import with same importer
            importer.from_record(
                Mock(sequence="CVE-2022-00002"),
                "CVE-2022-00002",
            )

        # Verify both imports used the same cache_entities instance
        self.assertIs(cache_ref, importer.cache_entities)
        self.assertIs(cached_entries[0], cached_entries[1])

    def test_naive_dates(self):
        cve = self.cve
        cve.date_made_public = cve.date_made_public.replace(tzinfo=None)
        cve.date_notice_issued = cve.date_notice_issued.replace(tzinfo=None)
        cve.date_coordinated_release = cve.date_coordinated_release.replace(
            tzinfo=None
        )
        bug, _ = self.importer.create_bug(cve, self.lp_cve)
        for date in (
            bug.vulnerabilities[0].date_made_public,
            bug.vulnerabilities[0].date_notice_issued,
            bug.vulnerabilities[0].date_coordinated_release,
        ):
            self.assertEqual(timezone.utc, date.tzinfo)
        self.importer.update_bug(bug, cve, self.lp_cve)
        for date in (
            bug.vulnerabilities[0].date_made_public,
            bug.vulnerabilities[0].date_notice_issued,
            bug.vulnerabilities[0].date_coordinated_release,
        ):
            self.assertEqual(timezone.utc, date.tzinfo)

    def test_make_cve_from_bug(self):
        self.importer.import_cve(self.cve)
        bug = self.importer._find_existing_bug(self.lp_cve, self.ubuntu)
        cve = self.exporter._make_cve_from_bug(bug)
        self.checkCVE(self.cve, cve)

    def test_export_bug_to_uct_file(self):
        self.importer.import_cve(self.cve)
        bug = self.importer._find_existing_bug(self.lp_cve, self.ubuntu)
        output_dir = Path(self.makeTemporaryDirectory())
        cve_path = self.exporter.export_bug_to_uct_file(bug.id, output_dir)
        uct_record = UCTRecord.load(cve_path)
        exported_cve = CVE.make_from_uct_record(
            uct_record,
            subprojects=self.subprojects,
        )
        self.checkCVE(self.cve, exported_cve)
        self.checkBug(bug, exported_cve)

    def test_import_cve_from_file(self):
        uct_record = self.cve.to_uct_record(subprojects=self.subprojects)

        cve_path = uct_record.save(Path(self.makeTemporaryDirectory()))
        bug, _, _ = self.importer.import_cve_from_file(cve_path)

        self.checkBug(bug, self.cve)
        self.checkVulnerabilities(bug, self.cve)

    def test_from_record(self):
        uct_record = self.cve.to_uct_record(subprojects=self.subprojects)
        bug, _, created = self.importer.from_record(
            uct_record, "CVE-2022-23222"
        )
        imported_cve = CVE.make_from_uct_record(
            uct_record,
            subprojects=self.subprojects,
        )
        self.checkCVE(self.cve, imported_cve)
        self.checkBug(bug, imported_cve)
        self.checkVulnerabilities(bug, imported_cve)
        self.assertTrue(created)

    def test_import_non_existing_cve(self):
        """Try to import a non existing cve won't create a bug and
        vulnerability."""
        self.cve.sequence = "CVE-2023-0000"
        uct_record = self.cve.to_uct_record()
        bug, vuln, created = self.importer.from_record(
            uct_record, "CVE-2023-0000"
        )
        self.assertEqual(bug, None)
        self.assertEqual(vuln, None)
        self.assertEqual(created, None)

    def test_import_duplicate(self):
        """Import more than once a cve and check that it does not duplicate."""
        self.importer.import_cve(self.cve)
        bug = self.importer._find_existing_bug(self.lp_cve, self.ubuntu)
        vulnerability = self.importer._find_existing_vulnerability(
            self.lp_cve, self.ubuntu
        )
        self.importer.import_cve(self.cve)
        bug_copy = self.importer._find_existing_bug(self.lp_cve, self.ubuntu)
        vulnerability_copy = self.importer._find_existing_vulnerability(
            self.lp_cve, self.ubuntu
        )
        self.assertEqual(bug, bug_copy)
        self.assertEqual(vulnerability, vulnerability_copy)

    def test_exporter_to_record(self):
        """Test to_record returns expected UCTRecord"""
        bug, vulnerability, _ = self.importer.import_cve(self.cve)

        uct_record = self.exporter.to_record(bug, vulnerability)

        self.assertListEqual(self.uct_record.packages, uct_record.packages)
        self.assertDictEqual(self.uct_record.__dict__, uct_record.__dict__)

    def test_create_bug_with_ppa_packages(self):
        """create_bug creates bug tasks for PPA package targets."""
        bug, _ = self.importer.create_bug(self.cve, self.lp_cve)

        bug_tasks_by_target = {t.target: t for t in bug.bugtasks}

        ppa_pkg = self.cve.ppa_packages[0]
        ppa_series_pkg = self.cve.ppa_series_packages[0]

        self.assertIn(ppa_pkg.target, bug_tasks_by_target)
        self.assertIn(ppa_series_pkg.target, bug_tasks_by_target)

        ppa_pkg_task = bug_tasks_by_target[ppa_pkg.target]
        self.assertEqual(BugTaskImportance.LOW, ppa_pkg_task.importance)

        ppa_series_task = bug_tasks_by_target[ppa_series_pkg.target]
        self.assertEqual(BugTaskImportance.HIGH, ppa_series_task.importance)
        self.assertEqual(BugTaskStatus.FIXRELEASED, ppa_series_task.status)
        self.assertEqual("1.2.3-4ubuntu5", ppa_series_task.status_explanation)

    def test_import_cve_with_only_ppa_packages_not_aborted(self):
        """import_cve doesn't abort when CVE has only PPA packages.

        This tests that a CVE with ppa_packages and ppa_series_packages but
        no distro/series/upstream packages can be successfully imported.
        """
        cve = copy.copy(self.cve)
        cve.distro_packages = []
        cve.series_packages = []
        cve.upstream_packages = []

        bug, vulnerability, created = self.importer.import_cve(cve)

        self.assertIsNotNone(bug)
        self.assertIsNotNone(vulnerability)
        self.assertTrue(created)

    def test_make_from_uct_record_with_subprojects(self):
        """make_from_uct_record populates ppa_packages/ppa_series_packages."""
        subprojects = {
            "esm-infra/focal": SubProjectPPAs(
                ubuntu_series="focal",
                ppa=PPAReference(
                    owner=self.ppa_owner.name,
                    archive=self.ppa.name,
                    pocket="security",
                ),
            ),
        }

        pkg_name = self.esm_package.sourcepackagename.name
        uct_record = UCTRecord(
            parent_dir="active",
            assigned_to=None,
            bugs=[],
            cvss={},
            candidate="CVE-2022-23222",
            crd=None,
            public_date_at_USN=None,
            public_date=datetime(2022, 1, 14, 8, 15, tzinfo=timezone.utc),
            description="description",
            discovered_by="",
            mitigation="",
            notes="",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="",
            references=[],
            ubuntu_description="",
            packages=[
                UCTRecord.Package(
                    name=pkg_name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="esm-infra/focal",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="fixed",
                            priority=UCTRecord.Priority.HIGH,
                        ),
                    ],
                    priority=UCTRecord.Priority.HIGH,
                    tags=set(),
                    patches=[],
                ),
            ],
            global_tags=set(),
        )

        cve = CVE.make_from_uct_record(
            uct_record,
            subprojects=subprojects,
        )

        self.assertEqual(1, len(cve.ppa_packages))
        self.assertEqual(1, len(cve.ppa_series_packages))
        self.assertEqual(0, len(cve.distro_packages))
        self.assertEqual(0, len(cve.series_packages))

        ppa_pkg = cve.ppa_packages[0]
        self.assertEqual(self.ppa, ppa_pkg.target.archive)
        self.assertEqual(
            self.esm_package.sourcepackagename,
            ppa_pkg.package_name,
        )
        self.assertEqual(BugTaskImportance.HIGH, ppa_pkg.importance)

        ppa_series_pkg = cve.ppa_series_packages[0]
        self.assertEqual(self.ppa, ppa_series_pkg.target.archive)
        self.assertEqual(
            self.ubuntu_supported_series, ppa_series_pkg.target.distroseries
        )
        self.assertEqual(BugTaskStatus.FIXRELEASED, ppa_series_pkg.status)
        self.assertEqual("fixed", ppa_series_pkg.status_explanation)

    def test_per_series_priority_round_trip(self):
        """Test that per-series priorities are preserved in round-trip."""
        uct_record = UCTRecord(
            parent_dir="active",
            assigned_to="",
            bugs=[],
            cvss={},
            candidate="CVE-2024-12345",
            crd=None,
            public_date_at_USN=None,
            public_date=datetime(2024, 1, 14, 8, 15, tzinfo=timezone.utc),
            description="Test CVE",
            discovered_by="",
            mitigation=None,
            notes="",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="",
            references=[],
            ubuntu_description="",
            packages=[
                UCTRecord.Package(
                    name=self.ubuntu_package.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="focal",
                            status=UCTRecord.PackageStatus.RELEASED,
                            reason="1.2.3",
                            priority=UCTRecord.Priority.CRITICAL,
                        ),
                        UCTRecord.SeriesPackageStatus(
                            series="jammy",
                            status=UCTRecord.PackageStatus.NEEDED,
                            reason="",
                            priority=UCTRecord.Priority.MEDIUM,
                        ),
                    ],
                    priority=UCTRecord.Priority.HIGH,
                    tags=set(),
                    patches=[],
                ),
            ],
            global_tags=set(),
        )

        # Convert to CVE and back
        cve = CVE.make_from_uct_record(uct_record)
        uct_record_roundtrip = cve.to_uct_record()

        # Verify per-series priorities are preserved
        package = uct_record_roundtrip.packages[0]
        focal_status = [s for s in package.statuses if s.series == "focal"][0]
        jammy_status = [s for s in package.statuses if s.series == "jammy"][0]

        self.assertEqual(UCTRecord.Priority.CRITICAL, focal_status.priority)
        self.assertEqual(UCTRecord.Priority.MEDIUM, jammy_status.priority)

        # Verify serialization includes per-series priorities
        serialized = uct_record.to_str()
        spn = self.ubuntu_package.sourcepackagename.name
        self.assertIn(
            f"Priority_{spn}_focal: critical",
            serialized,
        )
        self.assertIn(
            f"Priority_{spn}_jammy: medium",
            serialized,
        )

    def test_subproject_key_fallback_warning(self):
        """Test fallback behavior when subproject mapping is missing."""
        # Create CVE with PPA packages
        cve = CVE(
            sequence="CVE-2024-00001",
            date_made_public=None,
            date_notice_issued=None,
            date_coordinated_release=None,
            distro_packages=[],
            series_packages=[],
            upstream_packages=[],
            ppa_packages=[
                CVE.PPAPackage(
                    target=self.esm_package,
                    package_name=self.esm_package.sourcepackagename,
                    importance=BugTaskImportance.HIGH,
                    tags=set(),
                )
            ],
            ppa_series_packages=[
                CVE.PPASeriesPackage(
                    target=self.esm_ppa_asps_supported,
                    package_name=self.esm_package.sourcepackagename,
                    importance=BugTaskImportance.HIGH,
                    status=BugTaskStatus.FIXRELEASED,
                    status_explanation="",
                )
            ],
            importance=BugTaskImportance.HIGH,
            importance_explanation="",
            status=VulnerabilityStatus.ACTIVE,
            assignee=None,
            discovered_by="",
            description="Test",
            ubuntu_description="",
            bug_urls=[],
            references=[],
            notes="",
            mitigation="",
            cvss={},
            global_tags=set(),
            break_fix_data=[],
        )

        # Convert without providing subprojects mapping
        with self.expectedLog("No subproject mapping found"):
            uct_record = cve.to_uct_record(subprojects=None)

        # Verify fallback format is used
        package = uct_record.packages[0]
        series_names = {s.series for s in package.statuses}
        self.assertIn("esm-infra-security/focal", series_names)

    def test_get_patch_urls_with_invalid_url(self):
        """Test that invalid URLs are logged and skipped."""
        spn = self.factory.makeSourcePackageName()
        patches = [
            UCTRecord.Patch("upstream", "not-a-valid-url"),
            UCTRecord.Patch("upstream", "https://github.com/repo/commit/123"),
        ]

        with self.expectedLog("Invalid patch URL"):
            patch_urls = list(CVE.get_patch_urls(spn, patches))

        # Only the valid URL should be returned
        self.assertEqual(1, len(patch_urls))
        self.assertEqual(
            "https://github.com/repo/commit/123", str(patch_urls[0].url)
        )

    def test_get_patch_urls_with_unparseable_entry(self):
        """Test that unparseable patch entries are logged and skipped."""
        spn = self.factory.makeSourcePackageName()
        patches = [
            UCTRecord.Patch("upstream", ""),  # Empty entry
            UCTRecord.Patch("upstream", "https://valid.url"),
        ]

        with self.expectedLog("Could not parse the patch entry"):
            patch_urls = list(CVE.get_patch_urls(spn, patches))

        self.assertEqual(1, len(patch_urls))

    def test_get_break_fix_with_invalid_entry(self):
        """Test that invalid break-fix entries are logged and skipped."""
        spn = self.factory.makeSourcePackageName()
        patches = [
            UCTRecord.Patch("break-fix", "only-one-field"),  # Missing space
            UCTRecord.Patch("break-fix", "abc123 def456"),  # Valid
        ]

        with self.expectedLog("Could not parse the break-fix patch entry"):
            break_fixes = list(CVE.get_break_fix(spn, patches))

        self.assertEqual(1, len(break_fixes))
        self.assertEqual("abc123", break_fixes[0].broken)
        self.assertEqual("def456", break_fixes[0].fixed)

    def test_get_ppa_archive_with_missing_owner(self):
        """Test that missing PPA owner is handled gracefully."""
        cache_entities = CVE.new_cache()
        ppa_ref = PPAReference(
            owner="nonexistent-owner", archive="ppa-name", pocket="security"
        )

        with self.expectedLog("Could not find PPA owner"):
            archive = CVE._get_ppa_archive(
                ppa_ref, self.ubuntu, cache_entities
            )

        self.assertIsNone(archive)
        # Verify it's cached
        self.assertIn(
            ("nonexistent-owner", "ppa-name"), cache_entities["archive"]
        )
        self.assertIsNone(
            cache_entities["archive"][("nonexistent-owner", "ppa-name")]
        )

    def test_get_ppa_archive_uses_cache(self):
        """Test that PPA archive lookups are cached."""
        cache_entities = CVE.new_cache()
        ppa_ref = PPAReference(
            owner=self.ppa_owner.name, archive=self.ppa.name, pocket="security"
        )

        # First call
        archive1 = CVE._get_ppa_archive(ppa_ref, self.ubuntu, cache_entities)
        # Second call should use cache
        archive2 = CVE._get_ppa_archive(ppa_ref, self.ubuntu, cache_entities)

        self.assertIs(archive1, archive2)
        self.assertEqual(self.ppa, archive1)

    def test_make_from_uct_record_with_missing_assignee(self):
        """Test that missing assignee is handled gracefully."""
        uct_record = UCTRecord(
            parent_dir="active",
            assigned_to="nonexistent-user",
            bugs=[],
            cvss={},
            candidate="CVE-2024-99999",
            crd=None,
            public_date_at_USN=None,
            public_date=datetime(2024, 1, 14, 8, 15, tzinfo=timezone.utc),
            description="Test",
            discovered_by="",
            mitigation=None,
            notes="",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="",
            references=[],
            ubuntu_description="",
            packages=[],
            global_tags=set(),
        )

        with self.expectedLog("Could not find the assignee"):
            cve = CVE.make_from_uct_record(uct_record)

        self.assertIsNone(cve.assignee)

    def test_find_upstream_product_for_ppa_package(self):
        """Test upstream product lookup works for PPA packages."""
        cache_entities = CVE.new_cache()

        # The DSP has an upstream product linked
        ppa_packages = [
            CVE.PPAPackage(
                target=self.esm_package,
                package_name=self.esm_package.sourcepackagename,
                importance=BugTaskImportance.HIGH,
                tags=set(),
            )
        ]

        product = CVE._find_upstream_product(
            self.esm_package.sourcepackagename, ppa_packages, cache_entities
        )

        # Should find product_2 which is linked to esm_package
        self.assertEqual(self.product_2, product)

    def test_multiple_break_fix_patches(self):
        """Test handling of multiple break-fix patches for a package."""
        spn = self.factory.makeSourcePackageName()
        patches = [
            UCTRecord.Patch("break-fix", "abc def"),
            UCTRecord.Patch("break-fix", "123 456"),
            UCTRecord.Patch("break-fix", "- fixed_commit"),  # Break unknown
        ]

        break_fixes = list(CVE.get_break_fix(spn, patches))

        self.assertEqual(3, len(break_fixes))
        self.assertEqual("abc", break_fixes[0].broken)
        self.assertEqual("def", break_fixes[0].fixed)
        self.assertEqual("-", break_fixes[2].broken)
        self.assertEqual("fixed_commit", break_fixes[2].fixed)

    def test_empty_tags_handling(self):
        """Test that empty tag sets are handled correctly."""
        uct_record = UCTRecord(
            parent_dir="active",
            assigned_to="",
            bugs=[],
            cvss={},
            candidate="CVE-2024-00002",
            crd=None,
            public_date_at_USN=None,
            public_date=datetime(2024, 1, 14, 8, 15, tzinfo=timezone.utc),
            description="Test",
            discovered_by="",
            mitigation=None,
            notes="",
            priority=UCTRecord.Priority.HIGH,
            priority_explanation="",
            references=[],
            ubuntu_description="",
            packages=[
                UCTRecord.Package(
                    name=self.ubuntu_package.sourcepackagename.name,
                    statuses=[
                        UCTRecord.SeriesPackageStatus(
                            series="focal",
                            status=UCTRecord.PackageStatus.NEEDED,
                            reason="",
                            priority=None,
                        ),
                    ],
                    priority=None,
                    tags=set(),  # Empty tags
                    patches=[],
                ),
            ],
            global_tags=set(),  # Empty global tags
        )

        cve = CVE.make_from_uct_record(uct_record)
        uct_record_roundtrip = cve.to_uct_record()

        # Verify empty tags are preserved
        self.assertEqual(set(), uct_record_roundtrip.global_tags)
        self.assertEqual(set(), uct_record_roundtrip.packages[0].tags)

        # Verify serialization doesn't include Tags fields for empty sets
        serialized = uct_record.to_str()
        # Global Tags line should not be present (it's only added if non-empty)
        lines = serialized.split("\n")
        tag_lines = [line for line in lines if line.startswith("Tags")]
        self.assertEqual(0, len(tag_lines))

    def test_patch_url_with_special_characters_in_notes(self):
        """Test patch URLs with special characters in notes."""
        spn = self.factory.makeSourcePackageName()
        patches = [
            UCTRecord.Patch(
                "upstream", "https://example.com/commit/123 (v1.2.3-beta)"
            ),
            UCTRecord.Patch(
                "upstream",
                "https://example.com/commit/456 (fixes: CVE-2024-1234)",
            ),
        ]

        patch_urls = list(CVE.get_patch_urls(spn, patches))

        self.assertEqual(2, len(patch_urls))
        self.assertEqual("v1.2.3-beta", patch_urls[0].notes)
        self.assertEqual("fixes: CVE-2024-1234", patch_urls[1].notes)
