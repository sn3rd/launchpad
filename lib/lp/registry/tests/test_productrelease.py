# Copyright 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test product releases and product release set."""

from datetime import datetime, timezone

from zope.component import getUtility

from lp.app.enums import InformationType
from lp.registry.errors import InvalidFilename, ProprietaryPillar
from lp.registry.interfaces.productrelease import (
    IProductReleaseSet,
    UpstreamFileType,
)
from lp.services.database.interfaces import IStore
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import LaunchpadFunctionalLayer


class ProductReleaseSetTestcase(TestCaseWithFactory):
    """Tests for ProductReleaseSet."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.product_release_set = getUtility(IProductReleaseSet)

    def test_getBySeriesAndVersion_match(self):
        # The release is returned when there is a matching release version.
        milestone = self.factory.makeMilestone(name="0.0.1")
        release = self.factory.makeProductRelease(milestone=milestone)
        found = self.product_release_set.getBySeriesAndVersion(
            milestone.series_target, "0.0.1"
        )
        self.assertEqual(release, found)

    def test_getBySeriesAndVersion_none(self):
        # None is returned when there is no matching release version.
        milestone = self.factory.makeMilestone(name="0.0.1")
        found = self.product_release_set.getBySeriesAndVersion(
            milestone.series_target, "0.0.1"
        )
        self.assertEqual(None, found)

    def test_getBySeriesAndVersion_caches_milestone(self):
        # The release's milestone was cached when the release was retrieved.
        milestone = self.factory.makeMilestone(name="0.0.1")
        self.factory.makeProductRelease(milestone=milestone)
        series = milestone.series_target
        IStore(series).invalidate()
        release = self.product_release_set.getBySeriesAndVersion(
            series, "0.0.1"
        )
        self.assertStatementCount(0, getattr, release, "milestone")

    def test_getLatestReleaseWithDownloadFiles_no_releases(self):
        # Returns None when the product has no releases
        product = self.factory.makeProduct()
        result = self.product_release_set.getLatestReleaseWithDownloadFiles(
            product
        )
        self.assertIsNone(result)

    def test_getLatestReleaseWithDownloadFiles_no_files(self):
        # Returns None when releases exist but none have download files
        product = self.factory.makeProduct()
        self.factory.makeProductRelease(product=product)
        result = self.product_release_set.getLatestReleaseWithDownloadFiles(
            product
        )
        self.assertIsNone(result)

    def test_getLatestReleaseWithDownloadFiles_returns_latest(self):
        # Returns the most recent release that has at least one file
        product = self.factory.makeProduct()
        file1 = self.factory.makeProductReleaseFile(product=product)
        file2 = self.factory.makeProductReleaseFile(product=product)
        release1 = file1.productrelease
        release2 = file2.productrelease
        # Make release1 newer so it should be returned.
        with person_logged_in(product.owner):
            release1.datereleased = datetime(2025, 6, 1, tzinfo=timezone.utc)
            release2.datereleased = datetime(2024, 1, 1, tzinfo=timezone.utc)
        IStore(release1).flush()
        result = self.product_release_set.getLatestReleaseWithDownloadFiles(
            product
        )
        self.assertEqual(release1, result)


class ProductReleaseFileTestcase(TestCaseWithFactory):
    """Tests for ProductReleaseFile."""

    layer = LaunchpadFunctionalLayer

    def test_hasReleaseFile(self):
        release = self.factory.makeProductRelease()
        release_file = self.factory.makeProductReleaseFile(release=release)
        file_name = release_file.libraryfile.filename
        self.assertTrue(release.hasReleaseFile(file_name))
        self.assertFalse(release.hasReleaseFile("pting"))

    def test_addReleaseFile(self):
        release = self.factory.makeProductRelease()
        self.assertTrue(release.can_have_release_files)
        maintainer = release.milestone.product.owner
        with person_logged_in(maintainer):
            release_file = release.addReleaseFile(
                "pting.txt",
                b"test",
                "text/plain",
                maintainer,
                file_type=UpstreamFileType.README,
                description="desc",
            )
        self.assertEqual("desc", release_file.description)
        self.assertEqual(UpstreamFileType.README, release_file.filetype)
        self.assertEqual("pting.txt", release_file.libraryfile.filename)
        self.assertEqual("text/plain", release_file.libraryfile.mimetype)

    def test_addReleaseFile_duplicate(self):
        release_file = self.factory.makeProductReleaseFile()
        release = release_file.productrelease
        library_file = release_file.libraryfile
        maintainer = release.milestone.product.owner
        with person_logged_in(maintainer):
            self.assertRaises(
                InvalidFilename,
                release.addReleaseFile,
                library_file.filename,
                b"test",
                "text/plain",
                maintainer,
            )

    def test_addReleaseFile_only_works_on_public_products(self):
        owner = self.factory.makePerson()
        product = self.factory.makeProduct(
            information_type=InformationType.PROPRIETARY, owner=owner
        )
        with person_logged_in(owner):
            release = self.factory.makeProductRelease(product=product)
            self.assertFalse(release.can_have_release_files)
            self.assertRaises(
                ProprietaryPillar,
                release.addReleaseFile,
                "README",
                b"test",
                "text/plain",
                owner,
            )
