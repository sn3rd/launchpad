# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `VanillaDistroSeriesView`."""

from datetime import datetime, timezone

from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.buildmaster.enums import BuildStatus
from lp.registry.browser.vanilla_distroseries import (
    BUILD_STATUS_ICONS,
    ERROR_ICON,
    HELP_ICON,
    LOADING_ICON,
    PENDING_ICON,
    POCKET_CHART_COLORS,
    SKIP_ICON,
    SUCCESS_ICON,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
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

    # -- bugs list methods --

    def test_list_bugs_subscriptions(self):
        """Subscriptions are fetched from IBugTaskSet for series and user."""
        distroseries = self._makeDistroSeries()
        other_distroseries = self._makeDistroSeries()
        person = self.factory.makePerson()

        subscribed_task = self.factory.makeBugTask(target=distroseries)
        other_series_task = self.factory.makeBugTask(target=other_distroseries)
        with person_logged_in(person):
            subscribed_task.bug.subscribe(person, subscribed_by=person)
            other_series_task.bug.subscribe(person, subscribed_by=person)

        view = self._getView(distroseries, principal=person)
        subscriptions = list(view.list_bugs_subscriptions())

        self.assertIn(subscribed_task, subscriptions)
        self.assertNotIn(other_series_task, subscriptions)

    def test_list_bugs_subscriptions_limit_and_offset(self):
        """Bugs with subscriptions are paginated with limit and offset."""
        distroseries = self._makeDistroSeries()
        person = self.factory.makePerson()

        for _ in range(3):
            bugtask = self.factory.makeBugTask(target=distroseries)
            with person_logged_in(person):
                bugtask.bug.subscribe(person, subscribed_by=person)

        view = self._getView(distroseries, principal=person)
        all_subscriptions = list(view.list_bugs_subscriptions())
        paged_subscriptions = list(
            view.list_bugs_subscriptions(limit=1, offset=1)
        )

        self.assertEqual(3, len(all_subscriptions))
        self.assertEqual(all_subscriptions[1:2], paged_subscriptions)

    def test_list_bugs_important(self):
        """Important bugs list delegates to _search_bug_tasks as CRITICAL."""
        distroseries = self._makeDistroSeries()
        other_distroseries = self._makeDistroSeries()

        critical_task = self.factory.makeBugTask(target=distroseries)
        non_critical_task = self.factory.makeBugTask(target=distroseries)
        other_series_critical = self.factory.makeBugTask(
            target=other_distroseries
        )
        with person_logged_in(critical_task.target.owner):
            critical_task.transitionToImportance(
                BugTaskImportance.CRITICAL, critical_task.target.owner
            )
            non_critical_task.transitionToImportance(
                BugTaskImportance.HIGH, non_critical_task.target.owner
            )
            other_series_critical.transitionToImportance(
                BugTaskImportance.CRITICAL, other_series_critical.target.owner
            )

        view = self._getView(distroseries)
        important_tasks = list(view.list_bugs_important())

        self.assertIn(critical_task, important_tasks)
        self.assertNotIn(non_critical_task, important_tasks)
        self.assertNotIn(other_series_critical, important_tasks)
        for bugtask in important_tasks:
            self.assertEqual(BugTaskImportance.CRITICAL, bugtask.importance)

    def test_list_bugs_important_limit_and_offset(self):
        """Important bugs are paginated with limit and offset."""
        distroseries = self._makeDistroSeries()
        for _ in range(3):
            bugtask = self.factory.makeBugTask(target=distroseries)
            with person_logged_in(bugtask.target.owner):
                bugtask.transitionToImportance(
                    BugTaskImportance.CRITICAL, bugtask.target.owner
                )

        view = self._getView(distroseries)
        all_important = list(view.list_bugs_important())
        paged_important = list(view.list_bugs_important(limit=1, offset=1))

        self.assertEqual(3, len(all_important))
        self.assertEqual(all_important[1:2], paged_important)

    def test_list_bugs_new(self):
        """New bugs list delegates to _search_bug_tasks with NEW status."""
        distroseries = self._makeDistroSeries()
        other_distroseries = self._makeDistroSeries()

        new_task = self.factory.makeBugTask(
            target=distroseries, status=BugTaskStatus.NEW
        )
        non_new_task = self.factory.makeBugTask(target=distroseries)
        with person_logged_in(non_new_task.target.owner):
            non_new_task.transitionToStatus(
                BugTaskStatus.TRIAGED, non_new_task.target.owner
            )
        other_series_new = self.factory.makeBugTask(
            target=other_distroseries, status=BugTaskStatus.NEW
        )

        view = self._getView(distroseries)
        new_tasks = list(view.list_bugs_new())

        self.assertIn(new_task, new_tasks)
        self.assertNotIn(non_new_task, new_tasks)
        self.assertNotIn(other_series_new, new_tasks)
        for bugtask in new_tasks:
            self.assertEqual(BugTaskStatus.NEW, bugtask.status)

    def test_list_bugs_new_limit_and_offset(self):
        """New bugs are paginated with limit and offset."""
        distroseries = self._makeDistroSeries()
        for _ in range(3):
            self.factory.makeBugTask(
                target=distroseries, status=BugTaskStatus.NEW
            )

        view = self._getView(distroseries)
        all_new = list(view.list_bugs_new())
        paged_new = list(view.list_bugs_new(limit=1, offset=1))

        self.assertEqual(3, len(all_new))
        self.assertEqual(all_new[1:2], paged_new)

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


class TestVanillaDistroSeriesBinaryPackagesSummary(TestCaseWithFactory):
    """Tests for the binary_packages_summary_all_time view property."""

    layer = LaunchpadFunctionalLayer

    def _makeBuild(self, distroseries, status, pocket):
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
        )
        build = self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=self.factory.makeDistroArchSeries(
                distroseries=distroseries
            ),
            archive=spph.archive,
            status=status,
            pocket=pocket,
        )
        # FULLYBUILT builds without date_finished are filtered as
        # gina-generated by the underlying query.
        if status == BuildStatus.FULLYBUILT:
            removeSecurityProxy(build).date_finished = datetime.now(
                timezone.utc
            )
        return build

    def _getCounts(self, distroseries):
        view = create_initialized_view(distroseries, "+vanilla")
        return view.binary_packages_summary_all_time

    def test_empty_distroseries_returns_zero_for_every_pocket(self):
        """With no builds, every pocket is present with a count of 0."""
        distroseries = self.factory.makeDistroSeries()
        self.assertEqual(
            {pocket: 0 for pocket in PackagePublishingPocket.items},
            self._getCounts(distroseries),
        )

    def test_groups_in_scope_statuses_by_pocket_and_excludes_others(self):
        """In-scope statuses are counted per-pocket; others are ignored."""
        distroseries = self.factory.makeDistroSeries()
        # Two in-scope builds in RELEASE, one in UPDATES.
        self._makeBuild(
            distroseries,
            BuildStatus.FULLYBUILT,
            PackagePublishingPocket.RELEASE,
        )
        self._makeBuild(
            distroseries,
            BuildStatus.BUILDING,
            PackagePublishingPocket.RELEASE,
        )
        self._makeBuild(
            distroseries,
            BuildStatus.UPLOADING,
            PackagePublishingPocket.UPDATES,
        )
        # Out-of-scope builds in RELEASE should not contribute.
        self._makeBuild(
            distroseries,
            BuildStatus.FAILEDTOBUILD,
            PackagePublishingPocket.RELEASE,
        )
        self._makeBuild(
            distroseries,
            BuildStatus.SUPERSEDED,
            PackagePublishingPocket.RELEASE,
        )

        counts = self._getCounts(distroseries)
        self.assertEqual(2, counts[PackagePublishingPocket.RELEASE])
        self.assertEqual(1, counts[PackagePublishingPocket.UPDATES])
        self.assertEqual(0, counts[PackagePublishingPocket.SECURITY])


class TestPackagesSummaryAllTimeChart(TestCaseWithFactory):
    """Tests for packages_summary_all_time_chart."""

    layer = LaunchpadFunctionalLayer

    def _makeBuild(self, distroseries, status, pocket):
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=pocket,
        )
        build = self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=self.factory.makeDistroArchSeries(
                distroseries=distroseries
            ),
            archive=spph.archive,
            status=status,
            pocket=pocket,
        )
        if status == BuildStatus.FULLYBUILT:
            removeSecurityProxy(build).date_finished = datetime.now(
                timezone.utc
            )
        return spph, build

    def _getChart(self, distroseries, packages_type):
        view = create_initialized_view(distroseries, "+vanilla")
        return view.packages_summary_all_time_chart(packages_type)

    def test_source_empty_returns_zero_segments(self):
        """Empty distroseries returns segments with zero values."""
        distroseries = self.factory.makeDistroSeries()
        segments, total = self._getChart(distroseries, "source")
        self.assertEqual(0, total)
        for seg in segments:
            self.assertEqual(0, seg["value"])
            self.assertEqual(0, seg["percentage"])

    def test_binary_empty_returns_zero_segments(self):
        """Empty distroseries returns segments with zero values for binary."""
        distroseries = self.factory.makeDistroSeries()
        segments, total = self._getChart(distroseries, "binary")
        self.assertEqual(0, total)
        for seg in segments:
            self.assertEqual(0, seg["value"])
            self.assertEqual(0, seg["percentage"])

    def test_invalid_type_raises(self):
        """Invalid packages_type raises ValueError."""
        distroseries = self.factory.makeDistroSeries()
        view = create_initialized_view(distroseries, "+vanilla")
        self.assertRaises(
            ValueError,
            view.packages_summary_all_time_chart,
            "invalid",
        )

    def test_segment_keys(self):
        """Each segment dict has expected keys."""
        distroseries = self.factory.makeDistroSeries()
        segments, _ = self._getChart(distroseries, "source")
        expected_keys = {"name", "value", "percentage", "fill", "label"}
        for seg in segments:
            self.assertEqual(expected_keys, set(seg.keys()))

    def test_segment_order_matches_pocket_chart_colors(self):
        """Segments follow POCKET_CHART_COLORS order."""
        distroseries = self.factory.makeDistroSeries()
        segments, _ = self._getChart(distroseries, "source")
        expected_names = [p.title for p in POCKET_CHART_COLORS]
        actual_names = [seg["name"] for seg in segments]
        self.assertEqual(expected_names, actual_names)

    def test_segment_fill_matches_pocket_chart_colors(self):
        """Each segment fill matches its pocket color."""
        distroseries = self.factory.makeDistroSeries()
        segments, _ = self._getChart(distroseries, "source")
        expected_fills = list(POCKET_CHART_COLORS.values())
        actual_fills = [seg["fill"] for seg in segments]
        self.assertEqual(expected_fills, actual_fills)

    def test_source_counts_published_packages(self):
        """Source chart counts published source packages by pocket."""
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.RELEASE,
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.RELEASE,
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.SECURITY,
        )
        segments, total = self._getChart(distroseries, "source")
        self.assertEqual(3, total)
        by_name = {seg["name"]: seg for seg in segments}
        self.assertEqual(2, by_name["Release"]["value"])
        self.assertEqual(1, by_name["Security"]["value"])
        self.assertEqual(0, by_name["Backports"]["value"])

    def test_source_excludes_deleted_packages(self):
        """Source chart excludes non-active publishing statuses."""
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.RELEASE,
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.DELETED,
            pocket=PackagePublishingPocket.RELEASE,
        )
        segments, total = self._getChart(distroseries, "source")
        self.assertEqual(1, total)

    def test_binary_counts_by_pocket(self):
        """Binary chart counts in-scope builds by pocket."""
        distroseries = self.factory.makeDistroSeries()
        self._makeBuild(
            distroseries,
            BuildStatus.FULLYBUILT,
            PackagePublishingPocket.RELEASE,
        )
        self._makeBuild(
            distroseries,
            BuildStatus.BUILDING,
            PackagePublishingPocket.RELEASE,
        )
        self._makeBuild(
            distroseries,
            BuildStatus.UPLOADING,
            PackagePublishingPocket.UPDATES,
        )
        # Out-of-scope status should not count.
        self._makeBuild(
            distroseries,
            BuildStatus.FAILEDTOBUILD,
            PackagePublishingPocket.RELEASE,
        )
        segments, total = self._getChart(distroseries, "binary")
        self.assertEqual(3, total)
        by_name = {seg["name"]: seg for seg in segments}
        self.assertEqual(2, by_name["Release"]["value"])
        self.assertEqual(1, by_name["Updates"]["value"])

    def test_percentage_calculation(self):
        """Percentage is computed correctly per segment."""
        distroseries = self.factory.makeDistroSeries()
        # Create 3 source packages in RELEASE, 1 in SECURITY = 4 total.
        for _ in range(3):
            self.factory.makeSourcePackagePublishingHistory(
                distroseries=distroseries,
                archive=distroseries.main_archive,
                status=PackagePublishingStatus.PUBLISHED,
                pocket=PackagePublishingPocket.RELEASE,
            )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.SECURITY,
        )
        segments, total = self._getChart(distroseries, "source")
        self.assertEqual(4, total)
        by_name = {seg["name"]: seg for seg in segments}
        self.assertEqual(75, by_name["Release"]["percentage"])
        self.assertEqual(25, by_name["Security"]["percentage"])
        self.assertEqual(0, by_name["Backports"]["percentage"])

    def test_label_format(self):
        """Label is formatted as 'count pocket_title'."""
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.RELEASE,
        )
        segments, _ = self._getChart(distroseries, "source")
        by_name = {seg["name"]: seg for seg in segments}
        self.assertEqual("1 Release", by_name["Release"]["label"])
        self.assertEqual("0 Backports", by_name["Backports"]["label"])


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
