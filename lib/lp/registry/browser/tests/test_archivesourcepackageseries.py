# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSourcePackageSeries browser views."""

from lp.registry.browser.archivesourcepackageseries import (
    ArchiveSourcePackageSeriesBreadcrumb,
    ArchiveSourcePackageSeriesBugsMenu,
)
from lp.services.features.testing import FeatureFixture
from lp.services.webapp.publisher import canonical_url
from lp.soyuz.interfaces.archive import ARCHIVE_BUGS_FEATURE_FLAG
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.breadcrumbs import BaseBreadcrumbTestCase
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveSourcePackageSeriesBreadcrumb(BaseBreadcrumbTestCase):
    """Test breadcrumbs for ArchiveSourcePackageSeries."""

    def test_breadcrumb_text(self):
        # The breadcrumb text is "<name> in <archive displayname> <series>".
        asps = self.factory.makeArchiveSourcePackageSeries()
        crumb = ArchiveSourcePackageSeriesBreadcrumb(asps)
        expected = "%s in %s %s" % (
            asps.sourcepackagename.name,
            asps.archive.displayname,
            asps.distroseries.name,
        )
        self.assertEqual(expected, crumb.text)

    def test_canonical_url(self):
        # The canonical URL is archive bugs URL + "+source/<name>/<series>".
        asps = self.factory.makeArchiveSourcePackageSeries()
        archive_url = canonical_url(asps.archive, rootsite="bugs")
        expected_url = "%s/+source/%s/%s" % (
            archive_url,
            asps.sourcepackagename.name,
            asps.distroseries.name,
        )
        self.assertEqual(expected_url, canonical_url(asps))

    def test_breadcrumbs_with_traversal(self):
        # With the feature flag enabled the full breadcrumb chain resolves.
        self.useFixture(FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}))
        asps = self.factory.makeArchiveSourcePackageSeries()
        login_person(asps.archive.owner)
        self.assertBreadcrumbTexts(
            [
                asps.archive.owner.displayname,
                asps.archive.displayname,
                "%s in %s %s"
                % (
                    asps.sourcepackagename.name,
                    asps.archive.displayname,
                    asps.distroseries.name,
                ),
                "Bugs",
            ],
            asps,
            rootsite="bugs",
        )


class TestArchiveSourcePackageSeriesBugsMenu(TestCaseWithFactory):
    """Test bugs menu for ArchiveSourcePackageSeries."""

    layer = DatabaseFunctionalLayer

    def test_bugs_menu_links(self):
        # The bugs menu includes "filebug" and structural subscription links.
        asps = self.factory.makeArchiveSourcePackageSeries()
        menu = ArchiveSourcePackageSeriesBugsMenu(asps)
        self.assertIn("filebug", menu.links)
        self.assertIn("subscribe_to_bug_mail", menu.links)
