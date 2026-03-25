# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Vanilla view classes related to `IDistroSeries`."""

__all__ = [
    "VanillaDistroSeriesView",
]


import html as html_module
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Literal, Tuple
from urllib.parse import parse_qs, urlencode

from markupsafe import Markup, escape
from zope.component import getUtility

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
LOADING_ICON = "p-icon--spinner"
PENDING_ICON = "p-icon--loading-step"
SKIP_ICON = "p-icon--skip"

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


class Tabs:
    """Server-side helper for the Vanilla tabs pattern.

    https://vanillaframework.io/docs/patterns/tabs

    Renders the ``p-tabs`` navigation via :attr:`render` **without** ARIA
    tab roles — ``tabs.js`` hydrates the markup with ``role="tablist"``,
    ``role="tab"``, ``aria-selected``, and ``aria-controls`` when JS is
    available.  In no-JS mode, the active tab carries
    ``aria-current="page"`` and tabs act as plain navigation links.

    TAL usage::

        <tal:tabs tal:replace="structure view/my_tabs/render" />
        <div tal:attributes="id view/my_tabs/active_panel_id"
             tal:condition="python:view.my_tabs.active == 'key'">
          …panel content for 'key'…
        </div>
    """

    def __init__(
        self,
        param: str,
        default: str,
        tabs: List[Tuple[str, str]],
        request,
        base_url: str,
        swap_url: str,
        swap_target: str,
        swap_style: Literal["innerHTML", "outerHTML"],
        aria_label: str,
    ) -> None:
        self._param = param
        self._default = default
        self._tabs = tabs
        self._valid_keys = {k for k, _ in tabs}
        self._request = request
        self._base_url = base_url
        self._swap_url = swap_url
        self._swap_target = swap_target
        self._swap_style = swap_style
        self.aria_label = aria_label

    @property
    def active(self) -> str:
        """Return the key of the currently selected tab."""
        key = self._request.form.get(self._param, self._default)
        return key if key in self._valid_keys else self._default

    @property
    def active_panel_id(self) -> str:
        """Return the panel ID for the currently selected tab."""
        return "%s-%s-panel" % (self._param, self.active)

    def _build_url(self, base_url: str, key: str) -> str:
        """Build a URL preserving existing query params."""
        params = parse_qs(
            self._request.get("QUERY_STRING", ""),
            keep_blank_values=True,
        )
        if key != self._default:
            params[self._param] = [key]
        else:
            params.pop(self._param, None)
        qs = urlencode(params, doseq=True)
        return base_url + "?" + qs if qs else base_url

    def __iter__(self):
        selected = self.active
        for key, label in self._tabs:
            yield {
                "label": label,
                "href": self._build_url(self._base_url, key),
                "swap_url": self._swap_url,
                "swap_param_key": self._param,
                "swap_param_value": key,
                "is_default": key == self._default,
                "active": "true" if selected == key else "false",
                "panel_id": "%s-%s-panel" % (self._param, key),
            }

    @property
    def render(self) -> str:
        """Render the p-tabs navigation as an HTML string.

        The server-rendered markup intentionally omits ARIA tab roles
        (``role="tablist"``, ``role="tab"``, ``aria-selected``).  These
        are added by ``tabs.js`` on hydration so the tab contract is
        only active when JS can fulfil it.

        Instead, the active tab gets ``aria-current="page"`` (suitable
        for the no-JS page-navigation fallback) and each link carries a
        ``data-controls`` attribute that JS reads to set ``aria-controls``.
        """
        esc = html_module.escape
        items = []
        for tab in self:
            aria_current = (
                ' aria-current="page"' if tab["active"] == "true" else ""
            )
            swap_default = " swap-default" if tab["is_default"] else ""
            swap_current = " swap-current" if tab["active"] == "true" else ""
            items.append(
                '<div class="p-tabs__item">'
                '<a class="p-tabs__link"'
                ' href="%s"'
                ' swap-url="%s"'
                ' swap-target="%s"'
                ' swap-style="%s"'
                ' swap-param-key="%s"'
                ' swap-param-value="%s"'
                "%s"
                "%s"
                "%s"
                ' data-controls="%s">%s</a>'
                "</div>"
                % (
                    esc(tab["href"]),
                    esc(tab["swap_url"]),
                    esc(self._swap_target),
                    esc(self._swap_style),
                    esc(tab["swap_param_key"]),
                    esc(tab["swap_param_value"]),
                    swap_default,
                    swap_current,
                    aria_current,
                    esc(tab["panel_id"]),
                    esc(tab["label"]),
                ),
            )
        return (
            '<div class="p-tabs">'
            '<div class="p-tabs__list" data-js="tabs"'
            ' aria-label="%s">%s</div>'
            "</div>"
            % (
                esc(self.aria_label),
                "".join(items),
            )
        )


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
                    build["build_status"], PENDING_ICON
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
