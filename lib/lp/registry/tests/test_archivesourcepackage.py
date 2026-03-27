# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSourcePackage."""

from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveSourcePackage(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "mypackage"
        )
        self.distribution = self.factory.makeDistribution(name="mydistro")
        self.archive = self.factory.makeArchive(
            distribution=self.distribution, name="myppa"
        )
        self.other_archive = self.factory.makeArchive(
            distribution=self.distribution, name="otherppa"
        )

        self.archivesourcepackage = self.factory.makeArchiveSourcePackage(
            sourcepackagename=self.sourcepackagename,
            archive=self.archive,
        )

        self.archivesourcepackage_via_archive = (
            self.archive.getArchiveSourcePackage(
                name=self.sourcepackagename,
            )
        )
        self.archivesourcepackage_other = (
            self.factory.makeArchiveSourcePackage(
                sourcepackagename=self.sourcepackagename,
                archive=self.other_archive,
            )
        )
        self.archivesourcepackage_copy = self.archive.getArchiveSourcePackage(
            name=self.sourcepackagename,
        )

    def test_repr(self):
        """Test __repr__ function"""
        expected = (
            f"<ArchiveSourcePackage '{self.sourcepackagename.name} in "
            f"{self.archive.displayname}'>"
        )
        self.assertEqual(expected, self.archivesourcepackage.__repr__())

    def test_archive_convenience_method(self):
        """Test that archive.getArchiveSourcePackage() works correctly."""
        # Both methods should return equivalent objects
        asp_via_archive = self.archive.getArchiveSourcePackage(
            name=self.sourcepackagename,
        )
        self.assertEqual(
            self.archivesourcepackage.archive,
            asp_via_archive.archive,
        )
        self.assertEqual(
            self.archivesourcepackage.sourcepackagename,
            asp_via_archive.sourcepackagename,
        )
        # Test with string name
        asp_string = self.archive.getArchiveSourcePackage("mypackage")
        self.assertEqual(
            self.archivesourcepackage.sourcepackagename,
            asp_string.sourcepackagename,
        )
        # Test with nonexistent package
        asp_none = self.archive.getArchiveSourcePackage("nonexistent")
        self.assertIsNone(asp_none)

    def test_name(self):
        """Test name property"""
        self.assertEqual(
            self.sourcepackagename.name,
            self.archivesourcepackage.name,
        )

    def test_display_name(self):
        """Test display_name property"""
        asp = self.archivesourcepackage
        expected = (
            f"{self.sourcepackagename.name} in {asp.archive.displayname}"
        )
        self.assertEqual(expected, asp.display_name)

    def test_displayname(self):
        """Test displayname property (deprecated)"""
        asp = self.archivesourcepackage
        self.assertEqual(
            asp.display_name,
            asp.displayname,
        )

    def test_bugtargetdisplayname(self):
        """Test bugtargetdisplayname property"""
        asp = self.archivesourcepackage
        self.assertEqual(
            asp.display_name,
            asp.bugtargetdisplayname,
        )

    def test_bugtargetname(self):
        """Test bugtargetname property"""
        asp = self.archivesourcepackage
        self.assertEqual(
            asp.display_name,
            asp.bugtargetname,
        )

    def test_title(self):
        """Test title property"""
        asp = self.archivesourcepackage
        self.assertEqual(
            asp.display_name,
            asp.title,
        )

    def test_archive(self):
        """Test archive property"""
        self.assertEqual(
            self.archive,
            self.archivesourcepackage.archive,
        )

    def test_sourcepackagename(self):
        """Test sourcepackagename property"""
        self.assertEqual(
            self.sourcepackagename,
            self.archivesourcepackage.sourcepackagename,
        )

    def test_compare_equal(self):
        """Test __eq__ for equal objects"""
        self.assertEqual(
            self.archivesourcepackage,
            self.archivesourcepackage_copy,
        )

    def test_compare_different_archive(self):
        """Test __eq__ for different archives"""
        self.assertNotEqual(
            self.archivesourcepackage,
            self.archivesourcepackage_other,
        )

    def test_compare_different_package(self):
        """Test __eq__ for different source package names"""
        other_asp = self.factory.makeArchiveSourcePackage(
            sourcepackagename="otherpackage",
            archive=self.archive,
        )
        self.assertNotEqual(
            self.archivesourcepackage,
            other_asp,
        )

    def test_not_equal(self):
        """Test __ne__"""
        self.assertFalse(
            self.archivesourcepackage != self.archivesourcepackage_copy
        )
        self.assertTrue(
            self.archivesourcepackage != self.archivesourcepackage_other
        )

    def test_hash(self):
        """Test __hash__"""
        self.assertEqual(
            self.archivesourcepackage.__hash__(),
            self.archivesourcepackage_copy.__hash__(),
        )
        self.assertNotEqual(
            self.archivesourcepackage.__hash__(),
            self.archivesourcepackage_other.__hash__(),
        )

    def test_bug_target_parent(self):
        """Test bug_target_parent property"""
        self.assertEqual(
            self.archivesourcepackage.bug_target_parent,
            self.archive,
        )

    def test_different_archives_same_package(self):
        """Test that same package in different archives are different."""
        main_archive = self.distribution.main_archive
        # Use factory method to create publication in the main archive
        main_asp = self.factory.makeArchiveSourcePackage(
            sourcepackagename=self.sourcepackagename,
            archive=main_archive,
        )
        ppa_asp = self.archive.getArchiveSourcePackage(self.sourcepackagename)
        self.assertNotEqual(main_asp, ppa_asp)
        self.assertNotEqual(
            main_asp.__hash__(),
            ppa_asp.__hash__(),
        )
