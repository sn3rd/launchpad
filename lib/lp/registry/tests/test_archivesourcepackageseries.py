# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSourcePackageSeries."""

from lp.soyuz.enums import PackagePublishingStatus
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveSourcePackageSeries(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "mypackage"
        )
        self.distribution = self.factory.makeDistribution(name="mydistro")
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.distribution, name="myseries"
        )
        self.archive = self.factory.makeArchive(
            distribution=self.distribution, name="myppa"
        )
        self.other_archive = self.factory.makeArchive(
            distribution=self.distribution, name="otherppa"
        )
        self.other_series = self.factory.makeDistroSeries(
            distribution=self.distribution, name="otherseries"
        )

        # Use factory methods to create ArchiveSourcePackageSeries
        self.archivesourcepackageseries = (
            self.factory.makeArchiveSourcePackageSeries(
                sourcepackagename=self.sourcepackagename,
                archive=self.archive,
                distroseries=self.distroseries,
            )
        )
        self.archivesourcepackageseries_other_archive = (
            self.factory.makeArchiveSourcePackageSeries(
                sourcepackagename=self.sourcepackagename,
                archive=self.other_archive,
                distroseries=self.distroseries,
            )
        )
        self.archivesourcepackageseries_other_series = (
            self.factory.makeArchiveSourcePackageSeries(
                sourcepackagename=self.sourcepackagename,
                archive=self.archive,
                distroseries=self.other_series,
            )
        )
        self.archivesourcepackageseries_copy = (
            self.factory.makeArchiveSourcePackageSeries(
                sourcepackagename=self.sourcepackagename,
                archive=self.archive,
                distroseries=self.distroseries,
            )
        )

    def test_repr(self):
        """Test __repr__ function"""
        expected = (
            f"<ArchiveSourcePackageSeries '{self.sourcepackagename.name} "
            f"in {self.archive.displayname} "
            f"({self.distribution.display_name} "
            f"{self.distroseries.display_name})'>"
        )
        self.assertEqual(expected, self.archivesourcepackageseries.__repr__())

    def test_archive_convenience_method(self):
        """Test archive.getArchiveSourcePackageSeries() works correctly."""
        asp_via_archive = self.archive.getArchiveSourcePackageSeries(
            distroseries=self.distroseries,
            name=self.sourcepackagename,
        )

        self.assertEqual(
            self.archivesourcepackageseries.archive,
            asp_via_archive.archive,
        )
        self.assertEqual(
            self.archivesourcepackageseries.sourcepackagename,
            asp_via_archive.sourcepackagename,
        )
        self.assertEqual(
            self.archivesourcepackageseries.distroseries,
            asp_via_archive.distroseries,
        )

    def test_name(self):
        """Test name property"""
        self.assertEqual(
            self.sourcepackagename.name,
            self.archivesourcepackageseries.name,
        )

    def test_archive(self):
        """Test archive property"""
        self.assertEqual(
            self.archive,
            self.archivesourcepackageseries.archive,
        )

    def test_distroseries(self):
        """Test distroseries property"""
        self.assertEqual(
            self.distroseries,
            self.archivesourcepackageseries.distroseries,
        )

    def test_series(self):
        """Test series property"""
        self.assertEqual(
            self.distroseries,
            self.archivesourcepackageseries.series,
        )

    def test_sourcepackagename(self):
        """Test sourcepackagename property"""
        self.assertEqual(
            self.sourcepackagename,
            self.archivesourcepackageseries.sourcepackagename,
        )

    def test_display_name(self):
        """Test display_name property"""
        expected = (
            f"{self.sourcepackagename.name} in {self.archive.displayname} "
            f"({self.distribution.display_name} "
            f"{self.distroseries.display_name})"
        )
        self.assertEqual(
            expected,
            self.archivesourcepackageseries.display_name,
        )

    def test_displayname(self):
        """Test displayname property"""
        self.assertEqual(
            self.archivesourcepackageseries.display_name,
            self.archivesourcepackageseries.displayname,
        )

    def test_bugtargetdisplayname(self):
        """Test bugtargetdisplayname property"""
        self.assertEqual(
            self.archivesourcepackageseries.display_name,
            self.archivesourcepackageseries.bugtargetdisplayname,
        )

    def test_bugtargetname(self):
        """Test bugtargetname property"""
        self.assertEqual(
            self.archivesourcepackageseries.display_name,
            self.archivesourcepackageseries.bugtargetname,
        )

    def test_title(self):
        """Test title property"""
        self.assertEqual(
            self.archivesourcepackageseries.display_name,
            self.archivesourcepackageseries.title,
        )

    def test_compare_equal(self):
        """Test __eq__ for identical objects"""
        self.assertEqual(
            self.archivesourcepackageseries,
            self.archivesourcepackageseries_copy,
        )

    def test_compare_different_archive(self):
        """Test __eq__ for different archives"""
        self.assertNotEqual(
            self.archivesourcepackageseries,
            self.archivesourcepackageseries_other_archive,
        )

    def test_compare_different_series(self):
        """Test __eq__ for different series"""
        self.assertNotEqual(
            self.archivesourcepackageseries,
            self.archivesourcepackageseries_other_series,
        )

    def test_compare_different_package(self):
        """Test __eq__ for different source package names"""
        other_spn = self.factory.makeSourcePackageName("otherpackage")
        # Create publication for other package
        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename=other_spn,
            archive=self.archive,
            distroseries=self.distroseries,
            status=PackagePublishingStatus.PUBLISHED,
        )
        other_asp = self.archive.getArchiveSourcePackageSeries(
            self.distroseries, other_spn
        )
        self.assertNotEqual(
            self.archivesourcepackageseries,
            other_asp,
        )

    def test_not_equal(self):
        """Test __ne__"""
        self.assertFalse(
            self.archivesourcepackageseries
            != self.archivesourcepackageseries_copy
        )
        self.assertTrue(
            self.archivesourcepackageseries
            != self.archivesourcepackageseries_other_archive
        )

    def test_hash(self):
        """Test __hash__"""
        self.assertEqual(
            self.archivesourcepackageseries.__hash__(),
            self.archivesourcepackageseries_copy.__hash__(),
        )
        self.assertNotEqual(
            self.archivesourcepackageseries.__hash__(),
            self.archivesourcepackageseries_other_archive.__hash__(),
        )

    def test_official_bug_tags(self):
        """Test official_bug_tags property"""

        # Official bug tags aren't implemented yet due to Archive not having
        # the bug tag mixin, so this should return None for now
        self.assertEqual(
            self.archivesourcepackageseries.official_bug_tags,
            None,
        )

    def test_bug_target_parent(self):
        """Test bug_target_parent property"""
        self.assertEqual(
            self.archive,
            self.archivesourcepackageseries.bug_target_parent,
        )

    def test_bugtarget_parent(self):
        """The bugtarget parent is an ArchiveSourcePackage."""
        parent = self.archivesourcepackageseries.bugtarget_parent
        self.assertEqual(self.archive, parent.archive)
        self.assertEqual(
            self.sourcepackagename,
            parent.sourcepackagename,
        )

    def test_init_raises_assertion_for_different_distributions(self):
        """Creating ArchiveSourcePackageSeries with mismatched distributions
        raises AssertionError."""
        from lp.registry.model.archivesourcepackageseries import (
            ArchiveSourcePackageSeries,
        )

        # Create a distroseries from a different distribution
        other_distribution = self.factory.makeDistribution()
        other_distroseries = self.factory.makeDistroSeries(
            distribution=other_distribution
        )
        # Try to create ArchiveSourcePackageSeries with mismatched
        # distributions
        self.assertRaises(
            AssertionError,
            ArchiveSourcePackageSeries,
            self.archive,
            other_distroseries,
            self.sourcepackagename,
        )
