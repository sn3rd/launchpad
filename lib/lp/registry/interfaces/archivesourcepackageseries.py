# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An `IArchiveSourcePackageSeries` represents a package in an archive."""

__all__ = [
    "IArchiveSourcePackageSeries",
    "IArchiveSourcePackageSeriesView",
]

from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.interface import Attribute
from zope.schema import TextLine

from lp import _
from lp.app.interfaces.launchpad import IHeadingContext
from lp.bugs.interfaces.bugtarget import IBugTarget, IHasOfficialBugTags
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.role import IHasOwner
from lp.services.fields import PersonChoice
from lp.soyuz.interfaces.archive import IArchive


class IArchiveSourcePackageSeriesView(
    IHeadingContext, IBugTarget, IHasOfficialBugTags, IHasOwner
):
    """IArchiveSourcePackageSeries attributes requiring launchpad.View."""

    archive = exported(
        Reference(
            IArchive,
            title=_("The archive."),
            required=True,
            readonly=True,
        )
    )

    sourcepackagename = Attribute("The source package name.")

    name = exported(
        TextLine(title=_("The source package name as text."), readonly=True)
    )

    display_name = exported(
        TextLine(title=_("Display name for this package."), readonly=True)
    )

    displayname = Attribute("Display name (deprecated)")

    distroseries = exported(
        Reference(
            IDistroSeries,
            title=_("The distro series"),
            required=True,
            readonly=True,
        )
    )

    archive_sourcepackage = Attribute(
        "The IArchiveSourcePackage for this archive source package series."
    )

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
        """IArchiveSourcePackageSeries comparison method.

        Archive source package series compare equal only if their fields
        compare equal.
        """

    def __ne__(other):
        """IArchiveSourcePackageSeries comparison method.

        Archive source package series compare not equal if either of their
        fields compare not equal.
        """


@exported_as_webservice_entry(as_of="beta")
class IArchiveSourcePackageSeries(
    IArchiveSourcePackageSeriesView,
):
    """Represents an ArchiveSourcePackageSeries in a distroseries.

    Create IArchiveSourcePackageSeries by invoking
    `IArchive.getArchiveSourcePackageSeries()`.
    """
