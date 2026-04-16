# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "ArchiveSeriesBreadcrumb",
    "ArchiveSeriesBugsMenu",
    "ArchiveSeriesFacets",
    "ArchiveSeriesNavigation",
    "ArchiveSeriesURL",
]


from zope.interface import implementer

from lp.app.interfaces.headings import IHeadingBreadcrumb
from lp.bugs.browser.bugtask import BugTargetTraversalMixin
from lp.bugs.browser.structuralsubscription import (
    StructuralSubscriptionMenuMixin,
    StructuralSubscriptionTargetTraversalMixin,
)
from lp.registry.browser import add_subscribe_link
from lp.registry.browser.pillar import BugTargetParentBugsMenu
from lp.registry.interfaces.archiveseries import IArchiveSeries
from lp.services.propertycache import cachedproperty
from lp.services.webapp import Navigation, StandardLaunchpadFacets, stepto
from lp.services.webapp.breadcrumb import Breadcrumb
from lp.services.webapp.interfaces import (
    ICanonicalUrlData,
    IMultiFacetedBreadcrumb,
)
from lp.services.webapp.publisher import canonical_url


@implementer(IHeadingBreadcrumb, IMultiFacetedBreadcrumb)
class ArchiveSeriesBreadcrumb(Breadcrumb):
    """Builds a breadcrumb for an `IArchiveSeries`."""

    rootsite = "bugs"

    @property
    def text(self):
        return "%s %s" % (
            self.context.archive.displayname,
            self.context.distroseries.name,
        )


class ArchiveSeriesFacets(StandardLaunchpadFacets):
    usedfor = IArchiveSeries
    enable_only = ["bugs"]


class ArchiveSeriesBugsMenu(
    BugTargetParentBugsMenu, StructuralSubscriptionMenuMixin
):
    """Menu for bugs facet of ArchiveSeries."""

    usedfor = IArchiveSeries
    facet = "bugs"

    @cachedproperty
    def links(self):
        # Filebug link redirects to parent archive (bugs can't be filed
        # directly on series).
        links = ["filebug"]
        add_subscribe_link(links)
        return links


class ArchiveSeriesNavigation(
    BugTargetTraversalMixin,
    StructuralSubscriptionTargetTraversalMixin,
    Navigation,
):
    """Navigation for `IArchiveSeries`."""

    usedfor = IArchiveSeries

    @stepto("+filebug")
    def filebug(self):
        """Redirect to the parent archive's +filebug page."""
        archive = self.context.archive

        redirection_url = canonical_url(archive, view_name="+filebug")
        if self.request.form.get("no-redirect") is not None:
            redirection_url += "?no-redirect"
        return self.redirectSubTree(redirection_url, status=303)


@implementer(ICanonicalUrlData)
class ArchiveSeriesURL:
    """Archive series URL creation rules."""

    rootsite = "bugs"

    def __init__(self, context):
        self.context = context

    @property
    def inside(self):
        return self.context.archive

    @property
    def path(self):
        return "+series/%s" % self.context.distroseries.name
