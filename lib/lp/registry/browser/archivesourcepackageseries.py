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
from lp.services.webapp import Navigation, StandardLaunchpadFacets, stepto
from lp.services.webapp.breadcrumb import Breadcrumb
from lp.services.webapp.interfaces import (
    ICanonicalUrlData,
    IMultiFacetedBreadcrumb,
)
from lp.services.webapp.publisher import canonical_url


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
        # Filebug link redirects to parent package (bugs can't be filed
        # directly on series).
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

    @stepto("+filebug")
    def filebug(self):
        """Redirect +filebug to the parent package's +filebug page.

        Bugs cannot be filed directly on series. Redirect to the source
        package's +filebug page so the user can file on the package and
        target to series afterward.
        """
        package_url = canonical_url(
            self.context.archive_sourcepackage, rootsite="bugs"
        )
        return self.redirectSubTree(f"{package_url}/+filebug", status=303)


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
