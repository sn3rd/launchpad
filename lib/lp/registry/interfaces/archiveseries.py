# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An `IArchiveSeries` represents an archive in a specific distroseries."""

__all__ = [
    "IArchiveSeries",
    "IArchiveSeriesView",
]

from lazr.restful.declarations import exported, exported_as_webservice_entry
from lazr.restful.fields import Reference
from zope.schema import TextLine

from lp import _
from lp.app.interfaces.launchpad import IHeadingContext, IServiceUsage
from lp.bugs.interfaces.bugtarget import IBugTarget, IHasOfficialBugTags
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.role import IHasOwner
from lp.services.fields import PersonChoice
from lp.soyuz.interfaces.archive import IArchive


class IArchiveSeriesView(
    IHeadingContext, IBugTarget, IHasOfficialBugTags, IHasOwner, IServiceUsage
):
    """IArchiveSeries attributes requiring launchpad.View."""

    archive = exported(
        Reference(
            IArchive,
            title=_("The archive."),
            required=True,
            readonly=True,
        )
    )

    display_name = exported(
        TextLine(
            title=_("Display name for this archive series."), readonly=True
        )
    )

    displayname = TextLine(title=_("Display name (deprecated)"), readonly=True)

    distroseries = exported(
        Reference(
            IDistroSeries,
            title=_("The distro series"),
            required=True,
            readonly=True,
        )
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
        TextLine(title=_("Title for this archive series."), readonly=True)
    )

    def __eq__(other):
        """IArchiveSeries comparison method.

        Archive series compare equal only if their fields compare equal.
        """

    def __ne__(other):
        """IArchiveSeries comparison method.

        Archive series compare not equal if either of their fields
        compare not equal.
        """


@exported_as_webservice_entry(as_of="beta")
class IArchiveSeries(IArchiveSeriesView):
    """Represents an Archive in a specific distroseries.

    Create IArchiveSeries by invoking `IDistroSeries.getArchiveSeries()`.
    """
