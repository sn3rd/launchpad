# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An Archive in a DistroSeries.

Not to be confused with an ArchiveSourcePackageSeries, which represents
an Archive, DistroSeries, and SourcePackageName combination. This class
simply represents an Archive within a specific series context.
"""

__all__ = [
    "ArchiveSeries",
]

from storm.expr import And
from zope.interface import implementer

from lp.bugs.interfaces.bugtarget import ISeriesBugTarget
from lp.bugs.model.bugtarget import BugTargetBase
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.registry.interfaces.archiveseries import IArchiveSeries


@implementer(IArchiveSeries, ISeriesBugTarget)
class ArchiveSeries(
    BugTargetBase,
    StructuralSubscriptionTargetMixin,
):
    """See `IArchiveSeries`."""

    def __init__(self, archive, distroseries):
        """Create an ArchiveSeries.

        :param archive: An `IArchive`.
        :param distroseries: An `IDistroSeries`.
        """
        from lp.registry.interfaces.distroseries import IDistroSeries
        from lp.soyuz.interfaces.archive import IArchive

        if not IArchive.providedBy(archive):
            raise ValueError("archive must be an IArchive: %s" % repr(archive))
        if not IDistroSeries.providedBy(distroseries):
            raise ValueError(
                "distroseries must be an IDistroSeries: %s"
                % repr(distroseries)
            )

        self.archive = archive
        self.distroseries = distroseries

    @property
    def display_name(self):
        """See `IArchiveSeries`."""
        return "%s %s" % (self.archive.displayname, self.distroseries.name)

    @property
    def displayname(self):
        """See `IArchiveSeries`."""
        return self.display_name

    @property
    def title(self):
        """See `IArchiveSeries`."""
        return self.display_name

    @property
    def bugtargetdisplayname(self):
        """See `IBugTarget`."""
        return self.display_name

    @property
    def bugtargetname(self):
        """See `IBugTarget`."""
        return "%s/%s" % (
            self.archive.reference,
            self.distroseries.name,
        )

    @property
    def series(self):
        """See `ISeriesBugTarget`."""
        return self.distroseries

    @property
    def bugtarget_parent(self):
        """See `ISeriesBugTarget`."""
        # The parent of an archive series is the archive itself.
        return self.archive

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        # The parent of an archive series is the archive itself.
        return self.archive

    @property
    def owner(self):
        """See `IHasOwner`."""
        return self.archive.owner

    # Delegate official_bug_tags to the archive
    @property
    def official_bug_tags(self):
        """See `IHasOfficialBugTags`."""
        return self.archive.official_bug_tags

    @property
    def answers_usage(self):
        """See `IServiceUsage.`"""
        return self.archive.answers_usage

    @property
    def blueprints_usage(self):
        """See `IServiceUsage.`"""
        return self.archive.blueprints_usage

    @property
    def translations_usage(self):
        """See `IServiceUsage.`"""
        return self.archive.translations_usage

    @property
    def codehosting_usage(self):
        """See `IServiceUsage.`"""
        return self.archive.codehosting_usage

    @property
    def bug_tracking_usage(self):
        """See `IServiceUsage.`"""
        return self.archive.bug_tracking_usage

    @property
    def uses_launchpad(self):
        """See `IServiceUsage.`"""
        return self.archive.uses_launchpad

    def __eq__(self, other):
        """See `IArchiveSeries`."""
        return (
            IArchiveSeries.providedBy(other)
            and self.archive == other.archive
            and self.distroseries == other.distroseries
        )

    def __ne__(self, other):
        """See `IArchiveSeries`."""
        return not self == other

    def __hash__(self):
        """See `IArchiveSeries`."""
        return hash((self.archive, self.distroseries))

    def __repr__(self):
        return "<ArchiveSeries %s/%s>" % (
            self.archive.reference,
            self.distroseries.name,
        )

    def _getOfficialTagClause(self):
        """See `IHasOfficialBugTags`."""
        return self.archive._getOfficialTagClause()

    def _customizeSearchParams(self, search_params):
        """See `HasBugsBase`."""
        search_params.setArchiveSeries(self)

    def getBugSummaryContextWhereClause(self):
        """See `HasBugsBase`."""
        # Circular import avoidance
        from lp.bugs.model.bugsummary import BugSummary

        return And(
            BugSummary.archive == self.archive,
            BugSummary.distroseries == self.distroseries,
        )

    @property
    def bug_reporting_guidelines(self):
        """See `IBugTarget`."""
        return self.archive.bug_reporting_guidelines

    @property
    def content_templates(self):
        """See `IBugTarget`."""
        return self.archive.content_templates

    @property
    def bug_reported_acknowledgement(self):
        """See `IBugTarget`."""
        return self.archive.bug_reported_acknowledgement
