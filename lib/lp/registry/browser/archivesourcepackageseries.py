# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "ArchiveSourcePackageSeriesBreadcrumb",
    "ArchiveSourcePackageSeriesBugsMenu",
    "ArchiveSourcePackageSeriesFacets",
    "ArchiveSourcePackageSeriesNavigation",
    "ArchiveSourcePackageSeriesURL",
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
from lp.registry.interfaces.archivesourcepackageseries import (
    IArchiveSourcePackageSeries,
)
from lp.services.propertycache import cachedproperty
from lp.services.webapp import Navigation, StandardLaunchpadFacets
from lp.services.webapp.breadcrumb import Breadcrumb
from lp.services.webapp.interfaces import (
    ICanonicalUrlData,
    IMultiFacetedBreadcrumb,
)


@implementer(IHeadingBreadcrumb, IMultiFacetedBreadcrumb)
class ArchiveSourcePackageSeriesBreadcrumb(Breadcrumb):
    """Builds a breadcrumb for an `IArchiveSourcePackageSeries`."""

    rootsite = "bugs"

    @property
    def text(self):
        return "%s in %s %s" % (
            self.context.sourcepackagename.name,
            self.context.archive.displayname,
            self.context.distroseries.name,
        )


class ArchiveSourcePackageSeriesFacets(StandardLaunchpadFacets):
    usedfor = IArchiveSourcePackageSeries
    enable_only = ["bugs"]


class ArchiveSourcePackageSeriesBugsMenu(
    BugTargetParentBugsMenu, StructuralSubscriptionMenuMixin
):
    """Menu for bugs facet of ArchiveSourcePackageSeries."""

    usedfor = IArchiveSourcePackageSeries
    facet = "bugs"

    @cachedproperty
    def links(self):
        links = ["filebug"]
        add_subscribe_link(links)
        return links


class ArchiveSourcePackageSeriesNavigation(
    BugTargetTraversalMixin,
    StructuralSubscriptionTargetTraversalMixin,
    Navigation,
):
    """Navigation for `IArchiveSourcePackageSeries`."""

    usedfor = IArchiveSourcePackageSeries


@implementer(ICanonicalUrlData)
class ArchiveSourcePackageSeriesURL:
    """Dynamic URL declaration for IArchiveSourcePackageSeries."""

    rootsite = "bugs"

    def __init__(self, context):
        self.context = context

    @property
    def inside(self):
        return self.context.archive

    @property
    def path(self):
        return "+source/%s/%s" % (
            self.context.sourcepackagename.name,
            self.context.distroseries.name,
        )
