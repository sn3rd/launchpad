# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "ArchiveSourcePackageBreadcrumb",
    "ArchiveSourcePackageBugsMenu",
    "ArchiveSourcePackageFacets",
    "ArchiveSourcePackageNavigation",
    "ArchiveSourcePackageURL",
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
from lp.registry.interfaces.archivesourcepackage import IArchiveSourcePackage
from lp.services.propertycache import cachedproperty
from lp.services.webapp import Navigation, StandardLaunchpadFacets
from lp.services.webapp.breadcrumb import Breadcrumb
from lp.services.webapp.interfaces import (
    ICanonicalUrlData,
    IMultiFacetedBreadcrumb,
)


@implementer(IHeadingBreadcrumb, IMultiFacetedBreadcrumb)
class ArchiveSourcePackageBreadcrumb(Breadcrumb):
    """Builds a breadcrumb for an `IArchiveSourcePackage`."""

    rootsite = "bugs"

    @property
    def text(self):
        return "%s in %s" % (
            self.context.sourcepackagename.name,
            self.context.archive.displayname,
        )


class ArchiveSourcePackageFacets(StandardLaunchpadFacets):
    usedfor = IArchiveSourcePackage
    enable_only = ["bugs"]


class ArchiveSourcePackageBugsMenu(
    BugTargetParentBugsMenu, StructuralSubscriptionMenuMixin
):
    """Menu for bugs facet of ArchiveSourcePackage."""

    usedfor = IArchiveSourcePackage
    facet = "bugs"

    @cachedproperty
    def links(self):
        links = ["filebug"]
        add_subscribe_link(links)
        return links


class ArchiveSourcePackageNavigation(
    BugTargetTraversalMixin,
    StructuralSubscriptionTargetTraversalMixin,
    Navigation,
):
    """Navigation for `IArchiveSourcePackage`."""

    usedfor = IArchiveSourcePackage


@implementer(ICanonicalUrlData)
class ArchiveSourcePackageURL:
    """Dynamic URL declaration for IArchiveSourcePackage."""

    rootsite = "bugs"

    def __init__(self, context):
        self.context = context

    @property
    def inside(self):
        return self.context.archive

    @property
    def path(self):
        return "+source/%s" % self.context.sourcepackagename.name
