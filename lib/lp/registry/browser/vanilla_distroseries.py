# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Vanilla view classes related to `IDistroSeries`."""

__all__ = [
    "VanillaDistroSeriesView",
]


import html as html_module
from datetime import datetime, timedelta, timezone
from enum import Enum
from urllib.parse import parse_qs, urlencode

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


class Tabs:
    """Server-side helper for the Vanilla tabs pattern.

    https://vanillaframework.io/docs/patterns/tabs

    Renders the ``p-tabs`` navigation via :attr:`render` and yields
    per-tab dicts (``label``, ``href``, ``swap_url``, ``active``,
    ``panel_id``) for building ``role="tabpanel"`` elements in TAL.

    TAL usage::

        <tal:tabs tal:replace="structure view/my_tabs/render" />
        <div role="tabpanel"
             tal:attributes="id view/my_tabs/active_panel_id"
             tal:condition="python:view.my_tabs.active == 'key'">
          …panel content for 'key'…
        </div>
    """

    def __init__(
        self,
        param,
        default,
        tabs,
        request,
        base_url,
        swap_url,
        aria_label,
    ):
        self._param = param
        self._default = default
        self._tabs = tabs
        self._request = request
        self._base_url = base_url
        self._swap_url = swap_url
        self.aria_label = aria_label

    @property
    def active(self):
        """Return the key of the currently selected tab."""
        return self._request.form.get(self._param, self._default)

    @property
    def active_panel_id(self):
        """Return the panel ID for the currently selected tab."""
        return "%s-%s-panel" % (self._param, self.active)

    def _build_url(self, base_url, key):
        """Build a URL, omitting the param for the default tab."""
        params = parse_qs(
            self._request.get("QUERY_STRING", ""), keep_blank_values=True
        )
        if key == self._default:
            params.pop(self._param, None)
        else:
            params[self._param] = [key]
        qs = urlencode(params, doseq=True)
        return base_url + "?" + qs if qs else base_url

    def __iter__(self):
        selected = self.active
        for key, label in self._tabs:
            yield {
                "label": label,
                "href": self._build_url(self._base_url, key),
                "swap_url": self._build_url(self._swap_url, key),
                "active": "true" if selected == key else "false",
                "panel_id": "%s-%s-panel" % (self._param, key),
            }

    @property
    def render(self):
        """Render the p-tabs navigation as an HTML string."""
        esc = html_module.escape
        items = []
        for tab in self:
            items.append(
                '<div class="p-tabs__item">'
                '<a class="p-tabs__link" role="tab"'
                ' href="%s"'
                ' aria-selected="%s"'
                ' aria-controls="%s">%s</a>'
                "</div>"
                % (
                    esc(tab["href"], quote=True),
                    esc(tab["active"], quote=True),
                    esc(tab["panel_id"], quote=True),
                    esc(tab["label"]),
                )
            )
        return (
            '<div class="p-tabs">'
            '<div class="p-tabs__list" role="tablist" data-js="tabs"'
            ' aria-label="%s">%s</div>'
            "</div>"
            % (
                esc(self.aria_label, quote=True),
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
