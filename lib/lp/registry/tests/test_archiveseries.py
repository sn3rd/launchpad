# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSeries."""

from lp.registry.model.archiveseries import ArchiveSeries
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveSeries(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

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

        self.archiveseries = self.factory.makeArchiveSeries(
            self.archive, self.distroseries
        )
        self.archiveseries_other_archive = self.factory.makeArchiveSeries(
            self.other_archive, self.distroseries
        )
        self.archiveseries_other_series = self.factory.makeArchiveSeries(
            self.archive, self.other_series
        )
        self.archiveseries_copy = self.factory.makeArchiveSeries(
            self.archive, self.distroseries
        )

    def test_constructor_validates_archive(self):
        """Test that constructor validates archive parameter."""
        self.assertRaises(
            ValueError,
            ArchiveSeries,
            "not an archive",
            self.distroseries,
        )

    def test_constructor_validates_distroseries(self):
        """Test that constructor validates distroseries parameter."""
        self.assertRaises(
            ValueError,
            ArchiveSeries,
            self.archive,
            "not a distroseries",
        )

    def test_repr(self):
        """Test __repr__ function."""
        expected = (
            f"<ArchiveSeries {self.archive.reference}/"
            f"{self.distroseries.name}>"
        )
        self.assertEqual(expected, repr(self.archiveseries))

    def test_display_name(self):
        """Test display_name property."""
        expected = f"{self.archive.displayname} {self.distroseries.name}"
        self.assertEqual(expected, self.archiveseries.display_name)

    def test_displayname(self):
        """Test displayname property (deprecated)."""
        self.assertEqual(
            self.archiveseries.display_name,
            self.archiveseries.displayname,
        )

    def test_title(self):
        """Test title property."""
        self.assertEqual(
            self.archiveseries.display_name,
            self.archiveseries.title,
        )

    def test_bugtargetdisplayname(self):
        """Test bugtargetdisplayname property."""
        self.assertEqual(
            self.archiveseries.display_name,
            self.archiveseries.bugtargetdisplayname,
        )

    def test_bugtargetname(self):
        """Test bugtargetname property."""
        expected = f"{self.archive.reference}/{self.distroseries.name}"
        self.assertEqual(expected, self.archiveseries.bugtargetname)

    def test_series(self):
        """Test series property (ISeriesBugTarget)."""
        self.assertEqual(self.distroseries, self.archiveseries.series)

    def test_bug_target_parent(self):
        """Test bug_target_parent property."""
        self.assertEqual(self.archive, self.archiveseries.bug_target_parent)

    def test_bugtarget_parent(self):
        """Test bugtarget_parent property (ISeriesBugTarget)."""
        self.assertEqual(self.archive, self.archiveseries.bugtarget_parent)

    def test_owner(self):
        """Test owner property."""
        self.assertEqual(self.archive.owner, self.archiveseries.owner)

    def test_official_bug_tags(self):
        """Test official_bug_tags delegation to archive."""
        self.assertEqual(
            self.archive.official_bug_tags,
            self.archiveseries.official_bug_tags,
        )

    def test_equality_same_objects(self):
        """Test ArchiveSeries with same archive and series are equal."""
        self.assertEqual(self.archiveseries, self.archiveseries_copy)

    def test_equality_different_archive(self):
        """Test that ArchiveSeries with different archives are not equal."""
        self.assertNotEqual(
            self.archiveseries,
            self.archiveseries_other_archive,
        )

    def test_equality_different_series(self):
        """Test that ArchiveSeries with different series are not equal."""
        self.assertNotEqual(
            self.archiveseries,
            self.archiveseries_other_series,
        )

    def test_equality_with_non_archiveseries(self):
        """Test that ArchiveSeries is not equal to non-ArchiveSeries
        objects."""
        self.assertNotEqual(self.archiveseries, "not an archiveseries")
        self.assertNotEqual(self.archiveseries, None)
        self.assertNotEqual(self.archiveseries, self.archive)

    def test_hash_equal_objects(self):
        """Test that equal ArchiveSeries have the same hash."""
        self.assertEqual(
            hash(self.archiveseries),
            hash(self.archiveseries_copy),
        )

    def test_hash_different_archive(self):
        """Test ArchiveSeries with different archives have different
        hashes."""
        self.assertNotEqual(
            hash(self.archiveseries),
            hash(self.archiveseries_other_archive),
        )

    def test_hash_different_series(self):
        """Test ArchiveSeries with different series have different
        hashes."""
        self.assertNotEqual(
            hash(self.archiveseries),
            hash(self.archiveseries_other_series),
        )

    def test_hashable_in_set(self):
        """Test that ArchiveSeries can be used in sets."""
        archive_series_set = {
            self.archiveseries,
            self.archiveseries_copy,
            self.archiveseries_other_archive,
        }
        # archiveseries and archiveseries_copy should be the same object in set
        self.assertEqual(2, len(archive_series_set))
        self.assertIn(self.archiveseries, archive_series_set)
        self.assertIn(self.archiveseries_other_archive, archive_series_set)
