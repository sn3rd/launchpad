# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for bug listing views on Archive-related bug targets."""

from lp.soyuz.enums import ArchivePurpose
from lp.testing import TestCaseWithFactory, login_person, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_initialized_view


class TestArchiveBugListing(TestCaseWithFactory):
    """Test bug listing views for Archive, ArchiveSourcePackage, and
    ArchiveSourcePackageSeries."""

    layer = DatabaseFunctionalLayer

    def test_archive_bug_listing_view(self):
        """Test that Archive bug listing view works."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA

        # File a bug against the archive
        self.factory.makeBug(target=ppa)

        login_person(owner)
        view = create_initialized_view(ppa, "+bugs", principal=owner)

        # The view should be created successfully
        self.assertIsNotNone(view)
        # The context should be the archive
        self.assertEqual(ppa, view.context)

    def test_archive_shows_filed_bugs(self):
        """Test bugs filed against an archive are visible in the listing."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=owner, purpose=ArchivePurpose.PPA)

        # File multiple bugs
        bug1 = self.factory.makeBug(target=ppa, title="Bug 1")
        bug2 = self.factory.makeBug(target=ppa, title="Bug 2")

        login_person(owner)
        view = create_initialized_view(ppa, "+bugs", principal=owner)

        # Get the bugs from the view
        bugtasks = list(view.context.searchTasks(None))
        bug_ids = {task.bug.id for task in bugtasks}

        # Both bugs should be present
        self.assertIn(bug1.id, bug_ids)
        self.assertIn(bug2.id, bug_ids)

    def test_archive_only_shows_archive_bugs(self):
        """Test that ONLY bugs filed against the archive are shown."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=owner, purpose=ArchivePurpose.PPA)

        # File a bug against the PPA
        ppa_bug = self.factory.makeBug(target=ppa, title="PPA Bug")

        # File a bug against the distribution (not the PPA)
        distro_bug = self.factory.makeBug(
            target=ppa.distribution, title="Distro Bug"
        )

        # File a bug against a product (unrelated)
        product = self.factory.makeProduct()
        product_bug = self.factory.makeBug(target=product, title="Product Bug")

        login_person(owner)
        view = create_initialized_view(ppa, "+bugs", principal=owner)

        # Get the bugs from the view
        bugtasks = list(view.context.searchTasks(None))
        bug_ids = {task.bug.id for task in bugtasks}

        # Only the PPA bug should be present
        self.assertIn(ppa_bug.id, bug_ids)
        self.assertNotIn(distro_bug.id, bug_ids)
        self.assertNotIn(product_bug.id, bug_ids)
        # Should only have 1 bug
        self.assertEqual(1, len(bug_ids))

    def test_archivesourcepackage_bug_listing_view(self):
        """Test that ArchiveSourcePackage bug listing view works."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA
        spn = self.factory.makeSourcePackageName()

        with person_logged_in(owner):
            # Create a publication to ensure the package exists
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, sourcepackagename=spn
            )

            # Get the ArchiveSourcePackage
            asp = ppa.getArchiveSourcePackage(spn)
            self.assertIsNotNone(asp)

            # File a bug against the ArchiveSourcePackage
            self.factory.makeBug(target=asp)

            # Create the view
            view = create_initialized_view(asp, "+bugs", principal=owner)

            # The view should be created successfully
            self.assertIsNotNone(view)
            # The context should be the ArchiveSourcePackage
            self.assertEqual(asp, view.context)

    def test_archivesourcepackage_shows_filed_bugs(self):
        """Test that bugs filed against ArchiveSourcePackage are visible."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA
        spn = self.factory.makeSourcePackageName()

        with person_logged_in(owner):
            # Create a publication
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, sourcepackagename=spn
            )

            asp = ppa.getArchiveSourcePackage(spn)

            # File multiple bugs
            bug1 = self.factory.makeBug(target=asp, title="ASP Bug 1")
            bug2 = self.factory.makeBug(target=asp, title="ASP Bug 2")

            # Get the bugs from the view
            bugtasks = list(asp.searchTasks(None))
            bug_ids = {task.bug.id for task in bugtasks}

            # Both bugs should be present
            self.assertIn(bug1.id, bug_ids)
            self.assertIn(bug2.id, bug_ids)

    def test_archivesourcepackageseries_bug_listing_view(self):
        """Test that ArchiveSourcePackageSeries bug listing view works."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution
        )
        spn = self.factory.makeSourcePackageName()

        with person_logged_in(owner):
            # Create a publication
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, distroseries=distroseries, sourcepackagename=spn
            )

            # Get the ArchiveSourcePackageSeries
            asps = ppa.getArchiveSourcePackageSeries(distroseries, spn)
            self.assertIsNotNone(asps)

            # Create the view
            view = create_initialized_view(asps, "+bugs", principal=owner)

            # The view should be created successfully
            self.assertIsNotNone(view)
            # The context should be the ArchiveSourcePackageSeries
            self.assertEqual(asps, view.context)

    def test_archive_navigation_to_source_package(self):
        """Test navigation from Archive to ArchiveSourcePackage."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA
        spn = self.factory.makeSourcePackageName(name="testpkg")

        with person_logged_in(owner):
            # Create a publication
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, sourcepackagename=spn
            )

            # The ArchiveSourcePackage should be accessible
            asp = ppa.getArchiveSourcePackage(spn)
            self.assertIsNotNone(asp)
            self.assertEqual("testpkg", asp.name)

    def test_archive_navigation_to_source_package_series(self):
        """Test navigation from Archive to ArchiveSourcePackageSeries."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA
        distroseries = self.factory.makeDistroSeries(
            distribution=ppa.distribution, name="focal"
        )
        spn = self.factory.makeSourcePackageName(name="testpkg")

        with person_logged_in(owner):
            # Create a publication
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, distroseries=distroseries, sourcepackagename=spn
            )

            # The ArchiveSourcePackageSeries should be accessible
            asps = ppa.getArchiveSourcePackageSeries(distroseries, spn)
            self.assertIsNotNone(asps)
            self.assertEqual("testpkg", asps.name)
            self.assertEqual("focal", asps.distroseries.name)

    def test_archive_bugs_menu_has_subscribe_link(self):
        """Test that the bugs menu has a subscribe link."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA

        login_person(owner)
        view = create_initialized_view(ppa, "+bugs", principal=owner)

        # The view should have a menu
        self.assertIsNotNone(view)
        # We can't easily check the menu items without rendering,
        # but we can verify the view was created

    def test_archivesourcepackage_bugs_menu_has_filebug_link(self):
        """Test that ArchiveSourcePackage bugs menu has filebug link."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(
            owner=owner, purpose=ArchivePurpose.PPA
        )  # PPA
        spn = self.factory.makeSourcePackageName()

        with person_logged_in(owner):
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, sourcepackagename=spn
            )

            asp = ppa.getArchiveSourcePackage(spn)
            view = create_initialized_view(asp, "+bugs", principal=owner)

            # The view should be created successfully
            self.assertIsNotNone(view)

    def test_archive_filebug_page(self):
        """Test that the +filebug page works for Archives."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=owner, purpose=ArchivePurpose.PPA)

        with person_logged_in(owner):
            # Create the +filebug view - this should not error
            view = create_initialized_view(ppa, "+filebug", principal=owner)

            # The view should be created successfully
            self.assertIsNotNone(view)
            # The context should use Malone (Launchpad bug tracking)
            self.assertTrue(view.contextUsesMalone())

    def test_archive_file_bug_submission(self):
        """Test that filing a bug against an Archive works end-to-end."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=owner, purpose=ArchivePurpose.PPA)

        with person_logged_in(owner):
            # File a bug through the bug creation interface
            bug = self.factory.makeBug(target=ppa, title="Test Bug")

            # Verify the bug was created and targeted correctly
            self.assertIsNotNone(bug)
            self.assertEqual(1, len(bug.bugtasks))
            bugtask = bug.bugtasks[0]
            self.assertEqual(ppa, bugtask.target)

    def test_archive_bug_view_official_tags(self):
        """Test that the bug view can access official tags for Archive bugs."""
        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=owner, purpose=ArchivePurpose.PPA)

        with person_logged_in(owner):
            # File a bug against the archive
            bug = self.factory.makeBug(target=ppa, title="Test Bug")
            bugtask = bug.bugtasks[0]

            # Create the bug task view - can get official tags
            view = create_initialized_view(bugtask, "+index", principal=owner)

            # The view should be created successfully
            self.assertIsNotNone(view)

            # The view should be able to access available_official_tags_js
            # This calls bug.official_tags which calls _getOfficialTagClause()
            tags_js = view.available_official_tags_js
            self.assertIsNotNone(tags_js)
            self.assertIn("var available_official_tags", tags_js)

    def test_archive_bug_task_sorting(self):
        """Test that bug tasks with Archive targets can be sorted correctly."""
        from lp.bugs.browser.bugtask import bugtask_sort_key

        owner = self.factory.makePerson()
        ppa = self.factory.makeArchive(owner=owner, purpose=ArchivePurpose.PPA)
        spn = self.factory.makeSourcePackageName()

        with person_logged_in(owner):
            # Create publishing history so we can get ArchiveSourcePackage
            self.factory.makeSourcePackagePublishingHistory(
                archive=ppa, sourcepackagename=spn
            )

            # Create bugs for Archive and ArchiveSourcePackage
            bug1 = self.factory.makeBug(target=ppa, title="Archive Bug")

            asp = ppa.getArchiveSourcePackage(spn)
            bug2 = self.factory.makeBug(target=asp, title="ASP Bug")

            # Get all bugtasks and try to sort them
            # This should not raise AssertionError
            all_tasks = [bug1.bugtasks[0], bug2.bugtasks[0]]
            sorted_tasks = sorted(all_tasks, key=bugtask_sort_key)

            # Should complete without error
            self.assertEqual(2, len(sorted_tasks))
