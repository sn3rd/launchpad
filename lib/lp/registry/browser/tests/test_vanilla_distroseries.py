# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `VanillaDistroSeriesView`."""

from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.buildmaster.enums import BuildStatus
from lp.registry.browser.vanilla_distroseries import (
    BUILD_STATUS_ICONS,
    ERROR_ICON,
    HELP_ICON,
    LOADING_ICON,
    PENDING_ICON,
    SKIP_ICON,
    SUCCESS_ICON,
)
from lp.soyuz.enums import PackagePublishingStatus
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.views import create_initialized_view


class TestVanillaDistroSeriesPackagesList(TestCaseWithFactory):
    """Tests for the packages list view properties."""

    layer = DatabaseFunctionalLayer

    def _makeDistroSeries(self):
        distribution = self.factory.makeDistribution()
        return self.factory.makeDistroSeries(distribution=distribution)

    def _makeSpph(self, distroseries, **kwargs):
        return self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            **kwargs,
        )

    def _getView(self, distroseries, principal=None):
        return create_initialized_view(
            distroseries, "+vanilla", principal=principal
        )

    # -- packages_list_data --

    def test_packages_list_data_empty_state(self):
        """An empty-state <p> is rendered when there are no uploads."""
        distroseries = self._makeDistroSeries()
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn("No recent package uploads found", html)
        self.assertIn("<p", html)
        self.assertNotIn("<table", html)

    def test_packages_list_data_renders_table(self):
        """A <table> is rendered when uploads exist."""
        distroseries = self._makeDistroSeries()
        self._makeSpph(distroseries)
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn("<table>", html)
        self.assertIn("<thead>", html)
        self.assertIn("<tbody>", html)
        self.assertNotIn("No recent package uploads found", html)

    def test_packages_list_data_table_headers(self):
        """The table has the expected column headers."""
        distroseries = self._makeDistroSeries()
        self._makeSpph(distroseries)
        view = self._getView(distroseries)
        html = view.packages_list_data
        for header in ("Source package", "Version", "Pocket", "Builds"):
            self.assertIn("<th>%s</th>" % header, html)

    def test_packages_list_data_shows_package_info(self):
        """The table row contains the source package name and version."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(spph.source_package_name, html)
        self.assertIn(spph.source_package_version, html)

    # -- build status icons --

    def test_packages_list_data_build_success_icon(self):
        """Successfully built packages show the success icon."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(SUCCESS_ICON, html)
        self.assertIn("amd64", html)
        self.assertIn("Successfully built", html)

    def test_packages_list_data_build_failure_icon(self):
        """Failed builds show the error icon."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.FAILEDTOBUILD,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(ERROR_ICON, html)
        self.assertIn("Failed to build", html)

    def test_packages_list_data_build_in_progress_icon(self):
        """In-progress builds show the loading icon."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.BUILDING,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(LOADING_ICON, html)
        self.assertIn("Currently building", html)

    def test_packages_list_data_build_pending_icon(self):
        """Queued builds show the pending icon."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.NEEDSBUILD,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(PENDING_ICON, html)
        self.assertIn("Needs building", html)

    def test_packages_list_data_build_superseded_icon(self):
        """Superseded builds show the skip icon."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.SUPERSEDED,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(SKIP_ICON, html)

    def test_packages_list_data_multiple_builds(self):
        """Multiple builds for a source are all shown with correct icons."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das_amd64 = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        das_arm64 = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="arm64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_amd64,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_arm64,
            archive=spph.archive,
            status=BuildStatus.FAILEDTOBUILD,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn(SUCCESS_ICON, html)
        self.assertIn(ERROR_ICON, html)
        self.assertIn("amd64", html)
        self.assertIn("arm64", html)

    def test_packages_list_data_build_tooltip_markup(self):
        """Each build icon is wrapped in a Vanilla tooltip with ARIA."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn("p-tooltip--btm-center", html)
        self.assertIn('aria-describedby="build-tooltip-0"', html)
        self.assertIn('role="tooltip"', html)
        self.assertIn('id="build-tooltip-0"', html)
        self.assertIn("Successfully built", html)

    def test_packages_list_data_unknown_status_uses_help_icon(self):
        """Unknown statuses fall back to the pending icon."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das,
            archive=spph.archive,
            status=BuildStatus.NEEDSBUILD,
        )
        original = BUILD_STATUS_ICONS.pop(BuildStatus.NEEDSBUILD)
        try:
            view = self._getView(distroseries)
            html = view.packages_list_data
        finally:
            BUILD_STATUS_ICONS[BuildStatus.NEEDSBUILD] = original
        self.assertIn(HELP_ICON, html)
        self.assertIn("Needs building", html)

    def test_packages_list_data_build_tooltip_unique_ids(self):
        """Each build tooltip has a unique ID."""
        distroseries = self._makeDistroSeries()
        spph = self._makeSpph(distroseries)
        das_amd64 = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="amd64"
        )
        das_arm64 = self.factory.makeDistroArchSeries(
            distroseries=distroseries, architecturetag="arm64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_amd64,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_arm64,
            archive=spph.archive,
            status=BuildStatus.FAILEDTOBUILD,
        )
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn('id="build-tooltip-0"', html)
        self.assertIn('id="build-tooltip-1"', html)

    def test_packages_list_data_no_builds(self):
        """A source with no builds still renders a row (empty builds cell)."""
        distroseries = self._makeDistroSeries()
        self._makeSpph(distroseries)
        view = self._getView(distroseries)
        html = view.packages_list_data
        self.assertIn("<table>", html)
        # No icon classes should appear.
        self.assertNotIn(SUCCESS_ICON, html)
        self.assertNotIn(ERROR_ICON, html)

    # -- my_uploads_data --

    def test_my_uploads_data_anonymous_returns_empty(self):
        """When no user is logged in, my_uploads_data returns empty markup."""
        distroseries = self._makeDistroSeries()
        view = self._getView(distroseries, principal=None)
        self.assertEqual("", str(view.my_uploads_data))

    def test_my_uploads_data_with_user_no_uploads(self):
        """A logged-in user with no uploads sees the empty-state message."""
        distroseries = self._makeDistroSeries()
        person = self.factory.makePerson()
        with person_logged_in(person):
            view = self._getView(distroseries, principal=person)
            html = view.my_uploads_data
        self.assertIn("You have no recent uploads to this series", html)

    def test_my_uploads_data_with_user_has_uploads(self):
        """A logged-in user with uploads sees a table of their uploads."""
        distroseries = self._makeDistroSeries()
        person = self.factory.makePerson()
        self._makeSpph(distroseries, creator=person)
        # Also create an upload by someone else to confirm filtering.
        self._makeSpph(distroseries, creator=self.factory.makePerson())
        with person_logged_in(person):
            view = self._getView(distroseries, principal=person)
            html = view.my_uploads_data
        self.assertIn("<table>", html)
        # Only one row should appear (the user's upload).
        self.assertEqual(html.count("<tr>"), 2)  # 1 header + 1 data row


class TestBuildStatusIcons(TestCaseWithFactory):
    """Tests for the BUILD_STATUS_ICONS mapping completeness."""

    layer = DatabaseFunctionalLayer

    def test_all_build_statuses_have_icons(self):
        """Every BuildStatus value has a corresponding icon entry."""
        for status in BuildStatus.items:
            self.assertIn(
                status,
                BUILD_STATUS_ICONS,
                "BuildStatus.%s has no entry in BUILD_STATUS_ICONS"
                % status.name,
            )


class TestVanillaDistroSeriesBugsSummary(TestCaseWithFactory):
    """Tests for the get_bugs_summary view property."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()
        self.distroseries = self.factory.makeDistroSeries()

    def _makeBugTask(self, distroseries, status, importance, milestone=None):
        """Create a bug task for the given distroseries.

        If provided, assign the task to ``milestone``.
        """
        task = self.factory.makeBugTask(
            owner=self.person,
            target=distroseries,
        )

        # For distroseries targets, conjoined-task sync can leave the
        # initial values as NEW/UNDECIDED; apply explicit transitions so
        # BugSummary reflects the intended state in tests.
        with person_logged_in(self.person):
            if milestone is not None:
                task.transitionToMilestone(milestone, milestone.target.owner)
            if task.status != status:
                task.transitionToStatus(status)
            if task.importance != importance:
                task.transitionToImportance(importance)

    def _getView(self, distroseries, principal=None):
        return create_initialized_view(
            distroseries, "+vanilla", principal=principal
        )

    def test_bugs_summary_no_bugs(self):
        """With no bugs, all counts in bugs_summary are zero."""
        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary()
        self.assertEqual(summary["critical_bugs_count"], 0)
        self.assertEqual(summary["high_bugs_count"], 0)
        self.assertEqual(summary["inprogress_bugs_count"], 0)
        self.assertEqual(summary["open_bugs_count"], 0)

    def test_bugs_summary_resolved_bugs_not_counted_as_open(self):
        """Resolved bugs are not counted as open."""
        # Create an unresolved bug.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.LOW,
        )
        # Create a resolved bug.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.FIXRELEASED,
            importance=BugTaskImportance.LOW,
        )
        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary()
        # Only the unresolved bug should be counted.
        self.assertEqual(summary["open_bugs_count"], 1)

    def test_bugs_summary_excludes_resolved_from_all_counts(self):
        """Resolved bugs do not contribute to summary aggregates."""
        # Unresolved tasks.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.INPROGRESS,
            importance=BugTaskImportance.HIGH,
        )
        # Resolved tasks with the same importances should not contribute.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.FIXRELEASED,
            importance=BugTaskImportance.CRITICAL,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.FIXRELEASED,
            importance=BugTaskImportance.HIGH,
        )

        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary()

        self.assertEqual(summary["critical_bugs_count"], 1)
        self.assertEqual(summary["high_bugs_count"], 1)
        self.assertEqual(summary["inprogress_bugs_count"], 1)
        self.assertEqual(summary["open_bugs_count"], 2)

    def test_bugs_summary_mixed_bugs(self):
        """Verify that bugs with different statuses and importances are counted
        correctly"""
        # Create various bugs.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.CONFIRMED,
            importance=BugTaskImportance.CRITICAL,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.HIGH,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.INPROGRESS,
            importance=BugTaskImportance.MEDIUM,
        )

        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary()
        self.assertEqual(summary["critical_bugs_count"], 2)
        self.assertEqual(summary["high_bugs_count"], 1)
        self.assertEqual(summary["inprogress_bugs_count"], 1)
        self.assertEqual(summary["open_bugs_count"], 4)

    # -- milestone filter --

    def test_get_bugs_summary_no_milestone_returns_all(self):
        """Without a milestone filter all distroseries bugs are counted."""
        milestone = self.factory.makeMilestone(distroseries=self.distroseries)
        # One bug on the milestone, one not.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.HIGH,
            milestone=milestone,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
        )
        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary()
        # Both bugs should be counted.
        self.assertEqual(summary["open_bugs_count"], 2)
        self.assertEqual(summary["high_bugs_count"], 1)
        self.assertEqual(summary["critical_bugs_count"], 1)

    def test_get_bugs_summary_milestone_counts_only_milestone_bugs(self):
        """With a milestone filter only bugs on that milestone are counted."""
        milestone = self.factory.makeMilestone(distroseries=self.distroseries)
        # Bug assigned to the milestone.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
            milestone=milestone,
        )
        # Bug NOT assigned to the milestone.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.HIGH,
        )
        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary(milestone=milestone)
        self.assertEqual(summary["open_bugs_count"], 1)
        self.assertEqual(summary["critical_bugs_count"], 1)
        self.assertEqual(summary["high_bugs_count"], 0)

    def test_get_bugs_summary_milestone_no_bugs(self):
        """A milestone with no bugs returns all-zero counts."""
        milestone = self.factory.makeMilestone(distroseries=self.distroseries)
        # Bug is NOT on the milestone.
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
        )
        view = self._getView(self.distroseries, principal=self.person)
        summary = view.get_bugs_summary(milestone=milestone)
        self.assertEqual(summary["critical_bugs_count"], 0)
        self.assertEqual(summary["high_bugs_count"], 0)
        self.assertEqual(summary["inprogress_bugs_count"], 0)
        self.assertEqual(summary["open_bugs_count"], 0)

    def test_get_bugs_summary_milestone_excludes_other_milestone(self):
        """Bugs on a different milestone are not counted."""
        milestone_a = self.factory.makeMilestone(
            distroseries=self.distroseries
        )
        milestone_b = self.factory.makeMilestone(
            distroseries=self.distroseries
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.CRITICAL,
            milestone=milestone_a,
        )
        self._makeBugTask(
            self.distroseries,
            status=BugTaskStatus.NEW,
            importance=BugTaskImportance.HIGH,
            milestone=milestone_b,
        )
        view = self._getView(self.distroseries, principal=self.person)
        summary_a = view.get_bugs_summary(milestone=milestone_a)
        self.assertEqual(summary_a["critical_bugs_count"], 1)
        self.assertEqual(summary_a["high_bugs_count"], 0)
        self.assertEqual(summary_a["open_bugs_count"], 1)

        summary_b = view.get_bugs_summary(milestone=milestone_b)
        self.assertEqual(summary_b["critical_bugs_count"], 0)
        self.assertEqual(summary_b["high_bugs_count"], 1)
        self.assertEqual(summary_b["open_bugs_count"], 1)
