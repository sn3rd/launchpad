# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Vanilla view classes related to `IDistroSeries`."""

__all__ = [
    "VanillaDistroSeriesView",
]


from datetime import datetime, timedelta, timezone
from enum import Enum

from markupsafe import Markup, escape
from zope.component import getUtility

from lp.app.browser.vanilla import Tabs
from lp.bugs.interfaces.bugtask import (
    BugTaskImportance,
    BugTaskStatus,
    IBugTaskSet,
)
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.buildmaster.enums import BuildStatus
from lp.layers import VanillaLayer, setAdditionalLayer
from lp.registry.browser import MilestoneOverlayMixin
from lp.registry.interfaces.series import SeriesStatus
from lp.services.webapp.publisher import LaunchpadView, canonical_url
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuildSet
from lp.soyuz.interfaces.publishing import IPublishingSet


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


class VanillaDistroSeriesView(LaunchpadView, MilestoneOverlayMixin):
    """View for the vanilla distroseries page."""

    def initialize(self):
        super().initialize()
        setAdditionalLayer(self.request, VanillaLayer)
        base_url = canonical_url(self.context, view_name="+vanilla")
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
        self.packages_list_tabs = Tabs(
            param="packages-list",
            aria_label="Package uploads",
            tabs=[
                ("latest", "Latest uploads"),
                ("my-uploads", "My uploads"),
            ],
            default="latest",
            request=self.request,
            base_url=base_url,
            swap_url=canonical_url(
                self.context,
                view_name="+vanilla-distroseries-packages-list",
            ),
            swap_target="#packages-list",
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

    def _search_bug_tasks(self, **kwargs):
        """Search bug tasks with eager loading disabled.

        Since we only need counts, we use ``_noprejoins`` to skip the
        expensive eager loading that ``searchTasks`` performs by default.
        """
        params = BugTaskSearchParams(
            orderby="-datecreated",
            omit_dupes=True,
            user=self.user,
            **kwargs,
        )
        params.setDistroSeries(self.context)
        return getUtility(IBugTaskSet).search(params, _noprejoins=True)

    @property
    def bugs_summary(self):
        """Return the bugs summary (critical, in progress, triaged counts)."""
        critical_bugs = self._search_bug_tasks(
            importance=BugTaskImportance.CRITICAL,
        )
        inprogress_bugs = self._search_bug_tasks(
            status=BugTaskStatus.INPROGRESS,
        )
        triaged_bugs = self._search_bug_tasks(
            status=BugTaskStatus.TRIAGED,
        )

        return {
            "critical_bugs_count": critical_bugs.count(),
            "inprogress_bugs_count": inprogress_bugs.count(),
            "triaged_bugs_count": triaged_bugs.count(),
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

    def _build_packages_list_data(
        self, creator=None, empty_message="No recent package uploads found."
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

        rows = []
        tooltip_idx = 0
        for upload in uploads:
            build_chips = []
            for build in upload["builds"]:
                tooltip_id = "build-tooltip-%d" % tooltip_idx
                tooltip_idx += 1
                icon_class = BUILD_STATUS_ICONS.get(
                    build["build_status"], HELP_ICON
                )
                status_label = build["build_status"].title
                build_chips.append(
                    Markup(
                        '<span class="u-flex--row p-tooltip--btm-center"'
                        ' aria-describedby="{}">'
                        ' <i class="{}"></i>{}'
                        '<span class="p-tooltip__message" role="tooltip"'
                        ' id="{}">{}</span>'
                        "</span>"
                    ).format(
                        tooltip_id,
                        icon_class,
                        escape(build["arch_tag"]),
                        tooltip_id,
                        escape(status_label),
                    )
                )
            cells = [
                escape(upload["name"]),
                escape(upload["version"]),
                escape(upload["pocket_title"]),
                Markup(
                    "<span"
                    ' class="u-flex--row"'
                    ' style="gap: var(--dimension-spacing-inline-s);"'
                    ">{}</span>"
                ).format(Markup(" ").join(build_chips)),
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
            Markup("<th>{}</th>").format(h) for h in headers
        )
        return Markup(
            "<table>"
            "<colgroup>{}</colgroup>"
            "<thead><tr>{}</tr></thead>"
            "<tbody>{}</tbody>"
            "</table>"
        ).format(colgroup, header_row, Markup("").join(rows))

    @property
    def packages_list_data(self):
        """Return recent source uploads table HTML for the template."""
        return self._build_packages_list_data()

    @property
    def my_uploads_data(self):
        """Return the current user's recent uploads table HTML."""
        if self.user is None:
            return Markup("")
        return self._build_packages_list_data(
            creator=self.user,
            empty_message="You have no recent uploads to this series.",
        )

    @property
    def packages_url(self):
        """URL to the upload queue for this distroseries."""
        return canonical_url(self.context, view_name="+queue")

    @property
    def my_related_packages_url(self):
        """URL to the current user's +related-packages page, or None."""
        if self.user is None:
            return None
        return canonical_url(self.user, view_name="+related-packages")

    @property
    def next_milestone(self):
        """Return the closest upcoming milestone by expected date."""
        today = datetime.today().date()
        # `self.context.milestones` already returns active milestones for this
        # distroseries; we further restrict to those with a date on or after
        # today and pick the one with the earliest expected date.
        upcoming = [
            milestone
            for milestone in self.context.milestones
            if milestone.dateexpected is not None
            and milestone.dateexpected >= today
        ]

        if not upcoming:
            return None

        return min(
            upcoming,
            key=lambda milestone: (milestone.dateexpected, milestone.name),
        )
