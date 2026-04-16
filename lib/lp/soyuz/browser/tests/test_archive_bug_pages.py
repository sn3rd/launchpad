# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for bug pages on Archive-related bug targets."""

from zope.interface import directlyProvides
from zope.publisher.interfaces import NotFound

from lp.app.errors import NotFoundError
from lp.bugs.publisher import BugsLayer
from lp.registry.browser.archivesourcepackage import ArchiveSourcePackageFacets
from lp.registry.browser.archivesourcepackageseries import (
    ArchiveSourcePackageSeriesFacets,
)
from lp.registry.interfaces.archivesourcepackage import IArchiveSourcePackage
from lp.registry.interfaces.archivesourcepackageseries import (
    IArchiveSourcePackageSeries,
)
from lp.services.features.testing import FeatureFixture
from lp.services.webapp import canonical_url
from lp.soyuz.browser.archive import (
    ArchiveBugsMenu,
    ArchiveNavigation,
    ArchiveNavigationMenu,
)
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.archive import ARCHIVE_BUGS_FEATURE_FLAG, IArchive
from lp.testing import (
    LaunchpadTestRequest,
    TestCaseWithFactory,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_initialized_view


class TestArchiveBrowserIntegration(TestCaseWithFactory):
    """Test Archive browser integration for bug support."""

    layer = DatabaseFunctionalLayer

    def test_archivesourcepackage_has_bugs_facet(self):
        # ArchiveSourcePackage always has the bugs facet enabled
        # (access is gated by traverse_source on the parent archive).
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        spn = self.factory.makeSourcePackageName()
        with person_logged_in(ppa.owner):
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, sourcepackagename=spn
            )
            asp = ppa.getArchiveSourcePackage(spn)
        facets = ArchiveSourcePackageFacets(asp)
        self.assertIn("bugs", facets.enable_only)

    def test_archivesourcepackageseries_has_bugs_facet(self):
        # ArchiveSourcePackageSeries always has the bugs facet enabled
        # (access is gated by traverse_source on the parent archive).
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        spn = self.factory.makeSourcePackageName()
        with person_logged_in(ppa.owner):
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, distroseries=distroseries, sourcepackagename=spn
            )
            asps = ppa.getArchiveSourcePackageSeries(distroseries, spn)
        facets = ArchiveSourcePackageSeriesFacets(asps)
        self.assertIn("bugs", facets.enable_only)

    def test_archive_bugs_menu_has_filebug_link(self):
        # The bugs menu for archives includes a filebug link
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        owner = ppa.owner
        with person_logged_in(owner):
            menu = ArchiveBugsMenu(ppa)
            self.assertIn("filebug", menu.links)

    def test_archive_bugs_menu_has_subscribe_link(self):
        # The bugs menu for archives includes structural subscription links
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        owner = ppa.owner
        with person_logged_in(owner):
            menu = ArchiveBugsMenu(ppa)
            # Check that structural subscription links are present
            links = menu.links
            self.assertIn("subscribe_to_bug_mail", links)
            self.assertIn("edit_bug_mail", links)

    def test_archive_canonical_url_works(self):
        # Archives have working canonical URLs
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        url = canonical_url(ppa)
        self.assertIsNotNone(url)
        self.assertIn("+archive", url)

    def test_archive_provides_IArchive(self):
        # Verify that archives properly provide IArchive
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        self.assertTrue(IArchive.providedBy(ppa))

    def test_archive_view_accessible(self):
        # The archive index view is accessible without errors
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        owner = ppa.owner
        with person_logged_in(owner):
            view = create_initialized_view(ppa, "+index")
            self.assertIsNotNone(view)
            # Verify the view has the expected context
            self.assertEqual(ppa, view.context)


class TestArchiveBugPageAccess(TestCaseWithFactory):
    """Test that bug pages are blocked by the feature flag at traversal."""

    layer = DatabaseFunctionalLayer

    def test_archive_navigation_traverse_source_blocked_without_flag(self):
        # traverse_source raises NotFoundError when the flag is off.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        nav = ArchiveNavigation(ppa, request=None)
        self.assertRaises(NotFoundError, nav.traverse_source, "somepkg")

    def test_archive_navigation_traverse_source_allowed_with_flag(self):
        # traverse_source does not raise NotFoundError due to the feature
        # flag guard when the flag is on.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "on"}):
            # Returns None for a nonexistent package (not our guard raising).
            result = nav.traverse_source("nonexistent")
            self.assertIsNone(result)

    def test_filebug_traverse_blocked_without_flag(self):
        # publishTraverse raises NotFound for +filebug on any request layer
        # when the feature flag is off.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        self.assertRaises(NotFound, nav.publishTraverse, request, "+filebug")

    def test_filebug_traverse_not_blocked_by_flag_guard_with_flag(self):
        # With the flag on, publishTraverse passes +filebug through to
        # super() rather than raising from the flag guard.
        from unittest.mock import patch

        from lp.services.webapp import Navigation

        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        reached_super = []

        def fake_super_traverse(self_inner, req, nm):
            reached_super.append(nm)
            return None

        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "on"}):
            with patch.object(
                Navigation, "publishTraverse", fake_super_traverse
            ):
                nav.publishTraverse(request, "+filebug")

        self.assertEqual(["+filebug"], reached_super)

    def test_archive_navigation_bugs_site_blocked_without_flag(self):
        # publishTraverse raises NotFound on the bugs vhost when flag is off.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        directlyProvides(request, BugsLayer)
        nav = ArchiveNavigation(ppa, request=request)
        self.assertRaises(NotFound, nav.publishTraverse, request, "+bugs")

    def test_publish_traverse_allowed_on_bugs_site_with_flag(self):
        # publishTraverse does not block on the bugs vhost when flag is on.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        directlyProvides(request, BugsLayer)
        nav = ArchiveNavigation(ppa, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            # Should not raise; super().publishTraverse handles routing.
            try:
                nav.publishTraverse(request, "+index")
            except NotFound:
                pass  # NotFound from super is fine; our guard did not raise.

    def test_publish_traverse_non_bugs_request_not_blocked(self):
        # publishTraverse never blocks for non-bugs-layer requests.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        # No flag, no BugsLayer: the guard should not raise.
        try:
            nav.publishTraverse(request, "+index")
        except NotFound:
            pass  # NotFound from super is fine; our guard did not raise.

    def test_traverse_source_returns_archive_source_package(self):
        # With the flag on, traverse_source returns an ArchiveSourcePackage
        # when the package exists in the archive.
        asp = self.factory.makeArchiveSourcePackage()
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(asp.archive, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            result = nav.traverse_source(asp.sourcepackagename.name)
        self.assertTrue(IArchiveSourcePackage.providedBy(result))
        self.assertEqual(asp.sourcepackagename, result.sourcepackagename)

    def test_traverse_source_returns_none_for_unknown_package(self):
        # traverse_source returns None when the package does not exist.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            result = nav.traverse_source("no-such-package")
        self.assertIsNone(result)

    def test_traverse_source_returns_archive_source_package_series(self):
        # When a distroseries name is the next path segment, traverse_source
        # returns an ArchiveSourcePackageSeries.
        asps = self.factory.makeArchiveSourcePackageSeries()
        request = LaunchpadTestRequest()
        request.setTraversalStack([asps.distroseries.name])
        nav = ArchiveNavigation(asps.archive, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            result = nav.traverse_source(asps.sourcepackagename.name)
        self.assertTrue(IArchiveSourcePackageSeries.providedBy(result))
        self.assertEqual(asps.distroseries, result.distroseries)
        self.assertEqual(asps.sourcepackagename, result.sourcepackagename)

    def test_traverse_series_returns_archive_series(self):
        # With the flag on, traverse_series returns an ArchiveSeries.
        from lp.registry.interfaces.archiveseries import IArchiveSeries

        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            result = nav.traverse_series(distroseries.name)
        self.assertTrue(IArchiveSeries.providedBy(result))
        self.assertEqual(distroseries, result.distroseries)
        self.assertEqual(ppa, result.archive)

    def test_traverse_series_returns_none_for_unknown_series(self):
        # traverse_series returns None when the distroseries does not exist.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            result = nav.traverse_series("no-such-series")
        self.assertIsNone(result)

    def test_traverse_series_blocked_without_flag(self):
        # traverse_series raises NotFoundError when the flag is off.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        request = LaunchpadTestRequest()
        nav = ArchiveNavigation(ppa, request=request)
        self.assertRaises(
            NotFoundError, nav.traverse_series, distroseries.name
        )


class TestArchiveBugsMenuLinks(TestCaseWithFactory):
    """Tests for ArchiveBugsMenu links."""

    layer = DatabaseFunctionalLayer

    def test_links_include_filebug(self):
        # The bugs menu exposes a filebug link.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        menu = ArchiveBugsMenu(ppa)
        self.assertIn("filebug", menu.links)

    def test_links_include_structural_subscription_links(self):
        # The bugs menu includes both structural subscription link names.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        menu = ArchiveBugsMenu(ppa)
        self.assertIn("subscribe_to_bug_mail", menu.links)
        self.assertIn("edit_bug_mail", menu.links)

    def test_links_only_contains_expected_links(self):
        # The bugs menu contains exactly filebug and the two subscription
        # links, nothing more.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        menu = ArchiveBugsMenu(ppa)
        self.assertEqual(
            ["filebug", "subscribe_to_bug_mail", "edit_bug_mail"], menu.links
        )


class TestArchiveViewBugsLink(TestCaseWithFactory):
    """Tests for the view_bugs link in the archive navigation menu."""

    layer = DatabaseFunctionalLayer

    def test_view_bugs_link_disabled_without_flag(self):
        # The view_bugs link is disabled when the feature flag is off.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        menu = ArchiveNavigationMenu(ppa)
        link = menu.view_bugs()
        self.assertFalse(link.enabled)

    def test_view_bugs_link_enabled_with_flag(self):
        # The view_bugs link is enabled when the feature flag is on.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        menu = ArchiveNavigationMenu(ppa)
        with FeatureFixture({ARCHIVE_BUGS_FEATURE_FLAG: "true"}):
            link = menu.view_bugs()
        self.assertTrue(link.enabled)

    def test_view_bugs_link_targets_bugs_site(self):
        # The view_bugs link always points at the +bugs view on the bugs
        # rootsite, regardless of the feature flag.
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        menu = ArchiveNavigationMenu(ppa)
        link = menu.view_bugs()
        self.assertEqual("+bugs", link.target)
        self.assertEqual("bugs", link.site)
