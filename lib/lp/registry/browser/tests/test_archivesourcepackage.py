# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSourcePackage browser views."""

from lp.registry.browser.archivesourcepackage import (
    ArchiveSourcePackageBreadcrumb,
    ArchiveSourcePackageBugsMenu,
)
from lp.services.features.testing import FeatureFixture
from lp.services.webapp.publisher import canonical_url
from lp.soyuz.interfaces.archive import ARCHIVE_BUGS_FEATURE_FLAG
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.breadcrumbs import BaseBreadcrumbTestCase
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveSourcePackageBreadcrumb(BaseBreadcrumbTestCase):
    """Test breadcrumbs for ArchiveSourcePackage."""

    def test_breadcrumb_text(self):
        # The breadcrumb text is "<name> in <archive displayname>".
        asp = self.factory.makeArchiveSourcePackage()
        crumb = ArchiveSourcePackageBreadcrumb(asp)
        expected = "%s in %s" % (
            asp.sourcepackagename.name,
            asp.archive.displayname,
        )
        self.assertEqual(expected, crumb.text)

    def test_canonical_url(self):
        # The canonical URL is the archive bugs URL + "+source/<name>".
        asp = self.factory.makeArchiveSourcePackage()
        archive_url = canonical_url(asp.archive, rootsite="bugs")
        expected_url = "%s/+source/%s" % (
            archive_url,
            asp.sourcepackagename.name,
        )
        self.assertEqual(expected_url, canonical_url(asp))

    def test_breadcrumbs_with_traversal(self):
        # With the feature flag enabled the full breadcrumb chain resolves.
        self.useFixture(FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}))
        asp = self.factory.makeArchiveSourcePackage()
        login_person(asp.archive.owner)
        self.assertBreadcrumbTexts(
            [
                asp.archive.owner.displayname,
                asp.archive.displayname,
                "%s in %s"
                % (
                    asp.sourcepackagename.name,
                    asp.archive.displayname,
                ),
                "Bugs",
            ],
            asp,
            rootsite="bugs",
        )


class TestArchiveSourcePackageBugsMenu(TestCaseWithFactory):
    """Test bugs menu for ArchiveSourcePackage."""

    layer = DatabaseFunctionalLayer

    def test_bugs_menu_links(self):
        # The bugs menu includes "filebug" and structural subscription links.
        asp = self.factory.makeArchiveSourcePackage()
        menu = ArchiveSourcePackageBugsMenu(asp)
        self.assertIn("filebug", menu.links)
        self.assertIn("subscribe_to_bug_mail", menu.links)
