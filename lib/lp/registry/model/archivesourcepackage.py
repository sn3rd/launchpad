# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Classes to represent source packages in an archive."""

__all__ = [
    "ArchiveSourcePackage",
]

from zope.interface import implementer

from lp.bugs.model.bugtarget import BugTargetBase
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.registry.interfaces.archivesourcepackage import IArchiveSourcePackage
from lp.registry.interfaces.sourcepackagename import ISourcePackageName
from lp.services.propertycache import cachedproperty
from lp.soyuz.interfaces.archive import IArchive


@implementer(IArchiveSourcePackage)
class ArchiveSourcePackage(
    BugTargetBase,
    StructuralSubscriptionTargetMixin,
):
    """This is a "Magic Archive Source Package". It is not a Storm model, but
    instead it represents a package with a particular name in a particular
    archive and distribution.
    """

    def __init__(
        self,
        archive: IArchive,
        sourcepackagename: ISourcePackageName,
    ) -> "ArchiveSourcePackage":
        self.archive = archive
        self.sourcepackagename = sourcepackagename

    @property
    def name(self) -> str:
        """See `IArchiveSourcePackage`."""
        return self.sourcepackagename.name

    @cachedproperty
    def display_name(self) -> str:
        """See `IArchiveSourcePackage`."""
        return "%s in %s" % (
            self.sourcepackagename.name,
            self.archive.displayname,
        )

    # There are different places of launchpad codebase where they use
    # different display names
    @property
    def displayname(self) -> str:
        """See `IArchiveSourcePackage`."""
        return self.display_name

    @property
    def title(self) -> str:
        """See `IArchiveSourcePackage`."""
        return self.display_name

    @property
    def owner(self):
        """See `IHasOwner`."""
        return self.archive.owner

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

    @property
    def bugtargetdisplayname(self) -> str:
        """See `IBugTarget`."""
        return self.display_name

    @property
    def bugtargetname(self) -> str:
        """See `IBugTarget`."""
        return self.display_name

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} '{self.display_name}'>"

    def __eq__(self, other: "ArchiveSourcePackage") -> bool:
        """See `IArchiveSourcePackage`."""
        return (
            (IArchiveSourcePackage.providedBy(other))
            and (self.archive.id == other.archive.id)
            and (self.sourcepackagename.id == other.sourcepackagename.id)
        )

    def __ne__(self, other: "ArchiveSourcePackage") -> bool:
        """See `IArchiveSourcePackage`."""
        return not self.__eq__(other)

    def __hash__(self) -> int:
        """Return the combined attributes hash."""
        return hash((self.archive, self.sourcepackagename))
