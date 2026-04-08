# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Archive source package interfaces."""

__all__ = [
    "IArchiveSourcePackage",
]

from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.interface import Attribute
from zope.schema import TextLine

from lp import _
from lp.app.interfaces.launchpad import IHeadingContext
from lp.bugs.interfaces.bugtarget import IBugTarget, IHasOfficialBugTags
from lp.registry.interfaces.role import IHasOwner
from lp.services.fields import PersonChoice
from lp.soyuz.interfaces.archive import IArchive


@exported_as_webservice_entry(as_of="beta")
class IArchiveSourcePackageView(
    IHeadingContext,
    IBugTarget,
    IHasOfficialBugTags,
    IHasOwner,
):
    """`IArchiveSourcePackage` attributes that require launchpad.View."""

    archive = exported(
        Reference(IArchive, title=_("The archive."), readonly=True)
    )

    sourcepackagename = Attribute("The source package name.")

    name = exported(
        TextLine(title=_("The source package name as text."), readonly=True)
    )
    display_name = exported(
        TextLine(title=_("Display name for this package."), readonly=True)
    )
    displayname = Attribute("Display name (deprecated)")

    owner = exported(
        PersonChoice(
            title=_("Owner"),
            required=True,
            vocabulary="ValidOwner",
            description=_("""The archive owner."""),
        )
    )

    title = exported(
        TextLine(title=_("Title for this package."), readonly=True)
    )

    def __eq__(other):
        """IArchiveSourcePackage comparison method.

        Archive source packages compare equal only if their fields compare
        equal.
        """

    def __ne__(other):
        """IArchiveSourcePackage comparison method.

        Archive source packages compare not equal if either of their
        fields compare not equal.
        """


@exported_as_webservice_entry(as_of="beta")
class IArchiveSourcePackage(
    IArchiveSourcePackageView,
):
    """Represents a source package in an archive.

    Create IArchiveSourcePackage by invoking
    `IArchive.getArchiveSourcePackage()`.
    """
