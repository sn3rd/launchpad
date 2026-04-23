# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Vanilla view classes related to `IDistroSeries`."""

__all__ = [
    "VanillaDistroSeriesView",
]


from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Tuple
from urllib.parse import quote

from markupsafe import Markup, escape
from zope.component import getMultiAdapter, getUtility

from lp.app.browser.vanilla import Tabs
from lp.bugs.browser.buglisting import get_buglisting_search_filter_url
from lp.bugs.interfaces.bugtask import (
    BugTaskImportance,
    BugTaskStatus,
    IBugTaskSet,
)
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.buildmaster.enums import BuildStatus
from lp.layers import VanillaLayer, setAdditionalLayer
from lp.registry.browser import MilestoneOverlayMixin
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.features import getFeatureFlag
from lp.services.propertycache import cachedproperty
from lp.services.searchbuilder import not_equals
from lp.services.webapp.publisher import LaunchpadView, canonical_url
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuildSet
from lp.soyuz.interfaces.publishing import (
    IPublishingSet,
    active_publishing_status,
)

CLASSIC_DISTROSERIES_INDEX_FEATURE_FLAG = "view.distroseries.classic.default"


class ChipColor(str, Enum):
    """Vanilla chip color CSS classes.

    See: https://vanillaframework.io/docs/patterns/chip#colour-coding
    """

    NEUTRAL = "p-chip"
    POSITIVE = "p-chip--positive"
    INFORMATION = "p-chip--information"
    CAUTION = "p-chip--caution"
    NEGATIVE = "p-chip--negative"

    def __str__(self) -> str:
        return self.value


STATUS_CHIP_COLORS = {
    SeriesStatus.CURRENT: ChipColor.POSITIVE,
    SeriesStatus.SUPPORTED: ChipColor.POSITIVE,
    SeriesStatus.DEVELOPMENT: ChipColor.INFORMATION,
    SeriesStatus.FROZEN: ChipColor.INFORMATION,
    SeriesStatus.FUTURE: ChipColor.INFORMATION,
    SeriesStatus.EXPERIMENTAL: ChipColor.CAUTION,
    SeriesStatus.OBSOLETE: ChipColor.CAUTION,
}

# Vanilla icon CSS classes used for build status indicators.
# See: lib/canonical/launchpad/icing/vanilla/icons.scss
SUCCESS_ICON = "p-icon--success-grey"
ERROR_ICON = "p-icon--error-grey is-negative"
WARNING_ICON = "p-icon--warning-grey is-caution"
LOADING_ICON = "p-icon--spinner u-animation--spin"
PENDING_ICON = "p-icon--loading-step"
SKIP_ICON = "p-icon--skip is-muted"
HELP_ICON = "p-icon--help"

IMPORTANCE_ICONS = {
    BugTaskImportance.CRITICAL: "p-icon--warning-grey is-negative",
    BugTaskImportance.HIGH: "p-icon--warning-grey is-caution",
    BugTaskImportance.MEDIUM: "p-icon--warning-grey is-caution",
    BugTaskImportance.LOW: "p-icon--circle is-information",
    BugTaskImportance.WISHLIST: "p-icon--circle is-muted",
    BugTaskImportance.UNDECIDED: "p-icon--help is-muted",
    BugTaskImportance.UNKNOWN: "p-icon--help",
}

BUILD_STATUS_ICONS = {
    BuildStatus.FULLYBUILT: SUCCESS_ICON,
    BuildStatus.FAILEDTOBUILD: ERROR_ICON,
    BuildStatus.FAILEDTOUPLOAD: ERROR_ICON,
    BuildStatus.NEEDSBUILD: PENDING_ICON,
    BuildStatus.BUILDING: LOADING_ICON,
    BuildStatus.UPLOADING: LOADING_ICON,
    BuildStatus.GATHERING: LOADING_ICON,
    BuildStatus.SUPERSEDED: SKIP_ICON,
    BuildStatus.CANCELLED: SKIP_ICON,
    BuildStatus.CANCELLING: SKIP_ICON,
    BuildStatus.MANUALDEPWAIT: WARNING_ICON,
    BuildStatus.CHROOTWAIT: WARNING_ICON,
}

POCKET_CHART_COLORS = {
    PackagePublishingPocket.RELEASE: "var(--color-chart-green-primary)",
    PackagePublishingPocket.PROPOSED: "var(--color-chart-green-secondary)",
    PackagePublishingPocket.SECURITY: "var(--color-chart-blue-primary)",
    PackagePublishingPocket.UPDATES: "var(--color-chart-blue-secondary)",
    PackagePublishingPocket.BACKPORTS: "var(--color-chart-grey-primary)",
}


class VanillaDistroSeriesView(LaunchpadView, MilestoneOverlayMixin):
    """View for the vanilla distroseries page."""

    def index(self):
        """Render +classic when the feature flag is on, else default to
        +vanilla.

        TODO ines-almeida 2026-04-23: Remove the delegation and the classic
        view once the vanilla view is fully rolled out and stable.
        """
        view_name = (
            "+classic"
            if bool(getFeatureFlag(CLASSIC_DISTROSERIES_INDEX_FEATURE_FLAG))
            else "+vanilla"
        )
        return getMultiAdapter((self.context, self.request), name=view_name)()

    def initialize(self):
        super().initialize()
        setAdditionalLayer(self.request, VanillaLayer)
        base_url = canonical_url(self.context)
        self.packages_chart_tabs = Tabs(
            param="packages-chart",
            aria_label="Package builds",
            tabs=[("source", "Source"), ("binary", "Binary")],
            default="source",
            request=self.request,
            base_url=base_url,
            swap_url=canonical_url(
                self.context,
                view_name="+vanilla-distroseries-packages-chart",
            ),
            swap_target="#packages-chart",
            swap_style="outerHTML",
        )

        latest_uploads_tab = ("latest", "Latest uploads")
        my_uploads_tab = ("my-uploads", "My uploads")
        packages_list_tabs = [latest_uploads_tab, my_uploads_tab]
        packages_list_default = packages_list_tabs[0][0]
        self.packages_list_tabs = Tabs(
            param="packages-list",
            aria_label="Package uploads",
            tabs=packages_list_tabs,
            default=packages_list_default,
            request=self.request,
            base_url=base_url,
            swap_url=canonical_url(
                self.context,
                view_name="+vanilla-distroseries-packages-list",
            ),
            swap_target="#packages-list",
            swap_style="outerHTML",
        )

        subscriptions_tab = ("subscriptions", "My Subscriptions")
        important_tab = ("important", "Important")
        new_tab = ("new", "New")
        bugs_list_tabs = [important_tab, new_tab, subscriptions_tab]
        bugs_list_default = bugs_list_tabs[0][0]
        self.bugs_list_tabs = Tabs(
            param="bugs-list",
            aria_label="Bug tasks",
            tabs=bugs_list_tabs,
            default=bugs_list_default,
            request=self.request,
            base_url=base_url,
            swap_url=canonical_url(
                self.context,
                view_name="+vanilla-distroseries-bugs-list",
            ),
            swap_target="#bugs-list",
            swap_style="outerHTML",
        )

    @property
    def page_title(self):
        """Return the HTML page title."""
        return "%s (%s) : %s" % (
            self.context.displayname,
            self.context.version,
            self.context.distribution.displayname,
        )

    @property
    def status_chip_color(self) -> ChipColor:
        """Return the status chip color."""
        return STATUS_CHIP_COLORS.get(
            self.context.status,
            ChipColor.INFORMATION,
        )

    def list_bugs_subscriptions(self, limit=None, offset=None):
        """Return the bug subscriptions for this series."""
        return self._search_bug_tasks(
            subscriber=self.user,
            limit=limit,
            offset=offset,
        )

    def list_bugs_important(self, limit=None, offset=None):
        """Return the important bug tasks for this series."""
        return self._search_bug_tasks(
            importance=BugTaskImportance.CRITICAL,
            status=not_equals(BugTaskStatus.FIXRELEASED),
            limit=limit,
            offset=offset,
        )

    def list_bugs_new(self, limit=None, offset=None):
        """Return the new bug tasks for this series."""
        return self._search_bug_tasks(
            status=BugTaskStatus.NEW,
            limit=limit,
            offset=offset,
        )

    def _build_bugs_list_markup(self, bugtasks, caption, empty_message):
        """Return an HTML table of bug tasks, or an empty-state ``<p>``."""
        bugtasks = list(bugtasks)
        if not bugtasks:
            return Markup('<p class="u-text--muted">{}</p>').format(
                empty_message
            )

        rows = []
        for bugtask in bugtasks:
            if bugtask.sourcepackagename is not None:
                target_url = canonical_url(bugtask.target)
                bug_url = "%s/+bug/%d" % (
                    canonical_url(bugtask.target, rootsite="bugs"),
                    bugtask.bug.id,
                )
                affects_cell = Markup(
                    '<a class="p-link--soft" href="{}">{}</a>'
                ).format(target_url, escape(bugtask.sourcepackagename.name))
            # affects the series itself, no other cases are
            # possible in this context
            else:
                bug_url = canonical_url(bugtask.bug)
                affects_cell = Markup("-")
            importance_icon = IMPORTANCE_ICONS.get(
                bugtask.importance, "p-icon--help"
            )
            importance_cell = Markup(
                '<span class="u-flex--row"'
                ' style="gap: var(--dimension-spacing-inline-xs);'
                ' align-items: center;">'
                '<i class="{}"></i>{}</span>'
            ).format(importance_icon, escape(bugtask.importance.title))
            cells = [
                Markup("<a class='p-link--soft' href='{}'>{}</a>").format(
                    bug_url, bugtask.bug.id
                ),
                Markup('<a class="p-link--soft" href="{}">{}</a>').format(
                    bug_url, bugtask.bug.title
                ),
                affects_cell,
                importance_cell,
                escape(bugtask.status.title),
            ]
            rows.append(
                Markup("<tr>{}</tr>").format(
                    Markup("").join(
                        Markup("<td>{}</td>").format(cell) for cell in cells
                    )
                )
            )

        col_widths = ["8%", "42%", "15%", "15%", "20%"]
        colgroup = Markup("").join(
            Markup('<col style="width: {}">').format(w) for w in col_widths
        )
        headers = ["Bug", "Title", "Affects", "Importance", "Status"]
        header_row = Markup("").join(
            Markup('<th scope="col">{}</th>').format(h) for h in headers
        )
        return Markup(
            '<div class="table-container">'
            "<table>"
            '<caption class="u-off-screen">{}</caption>'
            "<colgroup>{}</colgroup>"
            "<thead><tr>{}</tr></thead>"
            "<tbody>{}</tbody>"
            "</table>"
            "</div>"
        ).format(escape(caption), colgroup, header_row, Markup("").join(rows))

    @property
    def bugs_subscriptions_markup(self):
        """Return the user's subscribed bugs table HTML."""
        return self._build_bugs_list_markup(
            self.list_bugs_subscriptions(limit=10),
            caption="Subscribed bugs in this series",
            empty_message="You have no bug subscriptions in this series.",
        )

    @property
    def bugs_important_markup(self):
        """Return the critical bugs table HTML."""
        return self._build_bugs_list_markup(
            self.list_bugs_important(limit=10),
            caption="Critical bugs in this series",
            empty_message="No critical bugs in this series.",
        )

    @property
    def bugs_new_markup(self):
        """Return the new bugs table HTML."""
        return self._build_bugs_list_markup(
            self.list_bugs_new(limit=10),
            caption="New bugs in this series",
            empty_message="No new bugs in this series.",
        )

    def _search_bug_tasks(self, limit=10, offset=0, **kwargs):
        """Search bug tasks for this series with the given criteria."""
        params = BugTaskSearchParams(
            orderby="-datecreated",
            omit_dupes=True,
            user=self.user,
            **kwargs,
        )
        params.setDistroSeries(self.context)
        result = getUtility(IBugTaskSet).search(params)
        if limit is not None:
            result = result.config(limit=limit)
        if offset is not None:
            result = result.config(offset=offset)
        return result

    def get_bugs_summary(self, milestone=None):
        """Return the bugs summary (critical, high, in progress, open counts).

        Uses the DistroSeries BugSummary API grouped by status/importance.

        :param milestone: An optional milestone to restrict the counts to.
        """
        counts = self.context.getBugSummaryStatusImportanceCounts(
            self.user, milestone=milestone
        )

        critical = high = inprogress = open_count = 0
        for (status, importance), count in counts.items():
            open_count += count

            # Counts by importance
            if importance == BugTaskImportance.CRITICAL:
                critical += count
            elif importance == BugTaskImportance.HIGH:
                high += count

            # Counts by status
            if status == BugTaskStatus.INPROGRESS:
                inprogress += count

        return {
            "critical_bugs_count": critical,
            "high_bugs_count": high,
            "inprogress_bugs_count": inprogress,
            "open_bugs_count": open_count,
        }

    @property
    def packages_summary_24h(self):
        """Return the packages summary for the last 24 hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        counts = getUtility(IBinaryPackageBuildSet).getCountsForDistro(
            self.context, date_finished_since=cutoff
        )

        successful_builds = counts.get(BuildStatus.FULLYBUILT, 0)
        failed_to_build = counts.get(BuildStatus.FAILEDTOBUILD, 0)
        failed_to_upload = counts.get(BuildStatus.FAILEDTOUPLOAD, 0)
        total_built = sum(counts.values())

        return {
            "built_packages_percentage": (
                round(successful_builds / total_built * 100, 1)
                if total_built
                else 0
            ),
            "failed_to_build_packages_count": failed_to_build,
            "failed_to_upload_packages_count": failed_to_upload,
        }

    def packages_summary_all_time_chart(
        self, packages_type: Literal["source", "binary"]
    ) -> Tuple[list, int]:
        """Return package counts by status for all time."""
        if packages_type == "source":
            pocket_counts = getUtility(
                IPublishingSet
            ).getPocketCountsForDistro(
                self.context, statuses=active_publishing_status
            )
        elif packages_type == "binary":
            pocket_counts = getUtility(
                IBinaryPackageBuildSet
            ).getPocketCountsForDistro(
                self.context,
                statuses=[
                    BuildStatus.FULLYBUILT,
                    BuildStatus.BUILDING,
                    BuildStatus.GATHERING,
                    BuildStatus.MANUALDEPWAIT,
                    BuildStatus.UPLOADING,
                ],
            )
        else:
            raise ValueError(f"Invalid packages type: {packages_type}")

        segments = []
        total = sum(pocket_counts.values())
        for pocket, fill in POCKET_CHART_COLORS.items():
            count = pocket_counts.get(pocket, 0)
            percentage = int(count / total * 100) if total else 0
            segments.append(
                {
                    "name": pocket.title,
                    "value": count,
                    "percentage": percentage,
                    "fill": fill,
                    "label": f"{count:,} {pocket.title}",
                }
            )
        return segments, total

    @property
    def binary_packages_summary_all_time(self):
        """Return the packages build status summary of all time."""
        return getUtility(IBinaryPackageBuildSet).getPocketCountsForDistro(
            self.context,
            statuses=[
                BuildStatus.FULLYBUILT,
                BuildStatus.BUILDING,
                BuildStatus.GATHERING,
                BuildStatus.MANUALDEPWAIT,
                BuildStatus.UPLOADING,
            ],
        )

    def _build_packages_list_data(
        self,
        creator=None,
        caption="Recent source package uploads",
        empty_message="No recent package uploads found.",
    ):
        """Return an HTML table of recent source uploads, or an empty-state
        ``<p>`` element."""
        uploads = getUtility(IPublishingSet).getRecentSourceUploads(
            self.context, creator=creator, limit=10
        )
        if not uploads:
            return Markup('<p class="u-text--muted">{}</p>').format(
                empty_message
            )

        series_url = canonical_url(self.context)
        distro_url = canonical_url(self.context.distribution)
        rows = []
        tooltip_idx = 0
        for upload in uploads:
            name_url = "{}/+source/{}".format(
                series_url, quote(upload["name"], safe="")
            )
            version_url = "{}/+source/{}/{}".format(
                distro_url,
                quote(upload["name"], safe=""),
                quote(upload["version"], safe=""),
            )
            build_chips = []
            for build in upload["builds"]:
                tooltip_id = "build-tooltip-%d" % tooltip_idx
                tooltip_idx += 1
                icon_class = BUILD_STATUS_ICONS.get(
                    build["build_status"], HELP_ICON
                )
                status_label = build["build_status"].title
                build_url = build["build_url"]
                build_chips.append(
                    Markup(
                        '<a class="p-link--soft u-flex--row'
                        ' p-tooltip--btm-center" href="{}"'
                        ' aria-describedby="{}">'
                        ' <i class="{}"></i>{}'
                        '<span class="p-tooltip__message" role="tooltip"'
                        ' id="{}">{}</span>'
                        "</a>"
                    ).format(
                        build_url,
                        tooltip_id,
                        icon_class,
                        escape(build["arch_tag"]),
                        tooltip_id,
                        escape(status_label),
                    )
                )
            builds_markup = Markup(
                "<span"
                ' class="u-flex--row"'
                ' style="gap: var(--dimension-spacing-inline-s);"'
                ">{}</span>"
            ).format(Markup(" ").join(build_chips))
            cells = [
                Markup('<a class="p-link--soft" href="{}">{}</a>').format(
                    name_url, escape(upload["name"])
                ),
                Markup('<a class="p-link--soft" href="{}">{}</a>').format(
                    version_url, escape(upload["version"])
                ),
                escape(upload["pocket_title"]),
                builds_markup,
            ]
            rows.append(
                Markup("<tr>{}</tr>").format(
                    Markup("").join(
                        Markup("<td>{}</td>").format(cell) for cell in cells
                    )
                )
            )

        col_widths = ["20%", "15%", "15%", "50%"]
        colgroup = Markup("").join(
            Markup('<col style="width: {}">').format(w) for w in col_widths
        )
        headers = ["Source package", "Version", "Pocket", "Builds"]
        header_row = Markup("").join(
            Markup('<th scope="col">{}</th>').format(h) for h in headers
        )
        return Markup(
            '<div class="table-container">'
            "<table>"
            '<caption class="u-off-screen">{}</caption>'
            "<colgroup>{}</colgroup>"
            "<thead><tr>{}</tr></thead>"
            "<tbody>{}</tbody>"
            "</table>"
            "</div>"
        ).format(escape(caption), colgroup, header_row, Markup("").join(rows))

    @property
    def packages_list_data(self):
        """Return recent source uploads table HTML for the template."""
        return self._build_packages_list_data(
            caption="Latest source package uploads to this series",
        )

    @property
    def my_uploads_data(self):
        """Return the current user's recent uploads table HTML."""
        if self.user is None:
            return Markup("")
        return self._build_packages_list_data(
            creator=self.user,
            caption="Your recent source package uploads to this series",
            empty_message="You have no recent uploads to this series.",
        )

    @cachedproperty
    def bugs_url(self):
        """Mapping of named bug filters to +bugs URLs on the bugs rootsite."""
        base_url = canonical_url(self.context, rootsite="bugs")

        def build_bugs_url(**kwargs):
            return "%s/%s" % (
                base_url,
                get_buglisting_search_filter_url(**kwargs),
            )

        urls = {
            "critical": build_bugs_url(
                importance=BugTaskImportance.CRITICAL.title
            ),
            "high": build_bugs_url(importance="High"),
            "in_progress": build_bugs_url(status="In Progress"),
            "new": build_bugs_url(status="New"),
            "latest": build_bugs_url(orderby="-datecreated"),
        }
        if self.user is not None:
            urls["subscriptions"] = build_bugs_url(subscriber=self.user.name)
        return urls

    @property
    def packages_url(self):
        """URL to the upload queue for this distroseries."""
        return canonical_url(self.context, view_name="+queue")

    @property
    def my_related_packages_url(self):
        """URL to the current user's +uploaded-packages page, or None."""
        if self.user is None:
            return None
        return canonical_url(self.user, view_name="+uploaded-packages")

    @property
    def distroserieslanguages(self):
        """Return the DistroSeriesLanguage rows for this series."""
        return list(self.context.distroserieslanguages.config(limit=10))

    @property
    def bugs_milestones(self):
        """Milestones offered in the bugs-summary milestone selector."""
        return list(self.context.all_milestones.config(limit=100))

    @property
    def selected_bugs_milestone(self):
        """
        Return the milestone selected via the ``bugs-milestone`` query param.

        Returns None when the param is missing, non-numeric, or does not
        match a milestone of this distroseries.
        """
        raw = self.request.form.get("bugs-milestone")
        if not raw:
            return None
        try:
            milestone_id = int(raw)
        except (TypeError, ValueError):
            return None

        return self.context.get_milestone(milestone_id)

    @property
    def selected_bugs_milestone_url(self):
        """URL to the +bugs page filtered by the selected milestone."""
        milestone = self.selected_bugs_milestone
        if milestone is None:
            return None
        search_filter = get_buglisting_search_filter_url(
            milestone=milestone.id,
        )
        return "%s/%s" % (
            canonical_url(self.context, rootsite="bugs"),
            search_filter,
        )

    @property
    def next_milestone(self):
        """
        Return the closest upcoming milestone by expected date.

        Returns None if no upcoming milestone is found.
        """
        return self.context.get_upcoming_milestone()
