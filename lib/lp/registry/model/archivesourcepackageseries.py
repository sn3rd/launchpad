# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Classes to represent archive source package series in a distroseries."""

__all__ = [
    "ArchiveSourcePackageSeries",
]

from zope.interface import implementer

from lp.bugs.interfaces.bugtarget import ISeriesBugTarget
from lp.bugs.model.bugtarget import BugTargetBase
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.registry.interfaces.archivesourcepackageseries import (
    IArchiveSourcePackageSeries,
)
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.sourcepackagename import ISourcePackageName
from lp.services.propertycache import cachedproperty
from lp.soyuz.interfaces.archive import IArchive


@implementer(IArchiveSourcePackageSeries, ISeriesBugTarget)
class ArchiveSourcePackageSeries(
    BugTargetBase,
    StructuralSubscriptionTargetMixin,
):
    """This is a "Magic Archive Source Package Series". It is not a Storm
    model, but instead it represents a package with a particular name in a
    particular archive and distroseries.
    """

    def __init__(
        self,
        archive: IArchive,
        distroseries: IDistroSeries,
        sourcepackagename: ISourcePackageName,
    ) -> "ArchiveSourcePackageSeries":
        assert (
            archive.distribution == distroseries.distribution
        ), "Archive and distroseries must belong to the same distribution"
        self.archive = archive
        self.distroseries = distroseries
        self.sourcepackagename = sourcepackagename
        self.distribution = distroseries.distribution

    @property
    def name(self) -> str:
        """See `IArchiveSourcePackageSeries`."""
        return self.sourcepackagename.name

    @cachedproperty
    def display_name(self) -> str:
        """See `IArchiveSourcePackageSeries`."""
        return (
            f"{self.sourcepackagename.name} in {self.archive.displayname} "
            f"({self.distribution.display_name} "
            f"{self.distroseries.display_name})"
        )

    # There are different places of launchpad codebase where they use
    # different display names
    @property
    def displayname(self) -> str:
        """See `IArchiveSourcePackageSeries`."""
        return self.display_name

    @property
    def bugtargetdisplayname(self) -> str:
        """See `IArchiveSourcePackageSeries`."""
        return self.display_name

    @property
    def bugtargetname(self) -> str:
        """See `IArchiveSourcePackageSeries`."""
        return self.display_name

    @property
    def bugtarget_parent(self):
        """See `ISeriesBugTarget`."""
        return self.archive_sourcepackage

    @property
    def archive_sourcepackage(self):
        """See `IArchiveSourcePackageSeries`."""
        return self.archive.getArchiveSourcePackage(self.sourcepackagename)

    @property
    def series(self):
        """See `ISeriesBugTarget`."""
        return self.distroseries

    @property
    def title(self) -> str:
        """See `IArchiveSourcePackageSeries`."""
        return self.display_name

    @property
    def official_bug_tags(self) -> list:
        """See `IHasBugs`."""

        # Archive doesn't have the bug tag mixin yet
        # return self.archive.official_bug_tags
        return None

    @property
    def bug_target_parent(self) -> IArchive:
        """See `IBugTarget`."""
        return self.archive

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} '{self.display_name}'>"

    def __eq__(self, other: "ArchiveSourcePackageSeries") -> bool:
        """See `IArchiveSourcePackageSeries`."""
        return (
            (IArchiveSourcePackageSeries.providedBy(other))
            and (self.archive.id == other.archive.id)
            and (self.distroseries.id == other.distroseries.id)
            and (self.sourcepackagename.id == other.sourcepackagename.id)
        )

    def __ne__(self, other: "ArchiveSourcePackageSeries") -> bool:
        """See `IArchiveSourcePackageSeries`."""
        return not self.__eq__(other)

    def __hash__(self) -> int:
        """Return the combined attributes hash."""
        return hash((self.archive, self.distroseries, self.sourcepackagename))
