# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ArchiveSeries browser views."""

from lp.registry.interfaces.archiveseries import IArchiveSeries
from lp.services.webapp import canonical_url
from lp.soyuz.enums import ArchivePurpose
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_initialized_view


class TestArchiveSeriesViews(TestCaseWithFactory):
    """Test ArchiveSeries browser views and navigation."""

    layer = DatabaseFunctionalLayer

    def test_archiveseries_facets(self):
        """ArchiveSeries has bugs facet enabled."""
        from lp.registry.browser.archiveseries import ArchiveSeriesFacets

        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        archiveseries = distroseries.getArchiveSeries(ppa)

        facets = ArchiveSeriesFacets(archiveseries)
        self.assertIn("bugs", facets.enable_only)

    def test_archiveseries_canonical_url(self):
        """ArchiveSeries has working canonical URLs."""
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        archiveseries = distroseries.getArchiveSeries(ppa)

        url = canonical_url(archiveseries, rootsite="bugs")
        self.assertIn("+series", url)
        self.assertIn(distroseries.name, url)

    def test_archiveseries_bugs_view_accessible(self):
        """The +bugs view is accessible for ArchiveSeries."""
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        archiveseries = distroseries.getArchiveSeries(ppa)

        with person_logged_in(ppa.owner):
            view = create_initialized_view(
                archiveseries, "+bugs", rootsite="bugs"
            )
            self.assertIsNotNone(view)
            self.assertEqual(archiveseries, view.context)

    def test_archiveseries_provides_IArchiveSeries(self):
        """Verify that ArchiveSeries properly provides IArchiveSeries."""
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        archiveseries = distroseries.getArchiveSeries(ppa)

        self.assertTrue(IArchiveSeries.providedBy(archiveseries))

    def test_archiveseries_navigation_filebug_redirect(self):
        """The +filebug navigation redirects to the archive's +filebug."""
        from lp.registry.browser.archiveseries import ArchiveSeriesNavigation
        from lp.testing import LaunchpadTestRequest

        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        archiveseries = distroseries.getArchiveSeries(ppa)

        with person_logged_in(ppa.owner):
            request = LaunchpadTestRequest()
            nav = ArchiveSeriesNavigation(archiveseries, request=request)
            redirect = nav.filebug()
            # The redirect should point to the archive's +filebug URL
            expected_url = canonical_url(ppa, view_name="+filebug")
            self.assertEqual(expected_url, redirect.target)

    def test_archiveseries_bugs_menu(self):
        """The bugs menu includes expected links."""
        from lp.registry.browser.archiveseries import ArchiveSeriesBugsMenu

        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        archiveseries = distroseries.getArchiveSeries(ppa)

        with person_logged_in(ppa.owner):
            menu = ArchiveSeriesBugsMenu(archiveseries)
            links = menu.links
            self.assertIn("filebug", links)
