# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Classes to represent external packages in a distroseries."""

__all__ = [
    "ExternalPackageSeries",
]

from zope.interface import implementer

from lp.bugs.interfaces.bugtarget import ISeriesBugTarget
from lp.bugs.model.bugtarget import BugTargetBase
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.interfaces.externalpackageseries import IExternalPackageSeries
from lp.registry.interfaces.sourcepackagename import ISourcePackageName
from lp.registry.model.hasdrivers import HasDriversMixin
from lp.services.channels import channel_list_to_string, channel_string_to_list
from lp.services.propertycache import cachedproperty


@implementer(IExternalPackageSeries, ISeriesBugTarget)
class ExternalPackageSeries(
    BugTargetBase,
    HasDriversMixin,
    StructuralSubscriptionTargetMixin,
):
    """This is a "Magic External Package". It is not a Storm model, but instead
    it represents a package with a particular name, type and channel in a
    particular distroseries.
    """

    def __init__(
        self,
        distroseries: IDistroSeries,
        sourcepackagename: ISourcePackageName,
        packagetype: ExternalPackageType,
        channel: (str, tuple, list),
    ) -> "ExternalPackageSeries":
        self.distroseries = distroseries
        self.sourcepackagename = sourcepackagename
        self.packagetype = packagetype
        self.channel = self.validate_channel(channel)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} '{self.display_name}'>"

    def validate_channel(self, channel: (str, tuple, list)) -> tuple:
        if channel is None:
            return None

        if not isinstance(channel, (str, tuple, list)):
            raise ValueError("Channel must be a str, tuple or list")

        # always return a 3 element tuple or None
        return channel_string_to_list(channel)

    @property
    def name(self) -> str:
        """See `IExternalPackageSeries`."""
        return self.sourcepackagename.name

    @property
    def distribution(self) -> str:
        """See `IExternalPackageSeries`."""
        return self.distroseries.distribution

    @property
    def display_channel(self) -> str:
        """See `IExternalPackageSeries`."""
        if not self.channel:
            return None

        return channel_list_to_string(*self.channel)

    @cachedproperty
    def display_name(self) -> str:
        """See `IExternalPackageSeries`."""
        if self.channel:
            return "%s - %s @%s in %s" % (
                self.sourcepackagename.name,
                self.packagetype.title,
                self.display_channel,
                self.distroseries.display_name,
            )

        return "%s - %s in %s" % (
            self.sourcepackagename.name,
            self.packagetype.title,
            self.distroseries.display_name,
        )

    # There are different places of launchpad codebase where they use different
    # display names
    @property
    def displayname(self) -> str:
        """See `IExternalPackageSeries`."""
        return self.display_name

    @property
    def bugtargetdisplayname(self) -> str:
        """See `IExternalPackageSeries`."""
        return self.display_name

    @property
    def bugtargetname(self) -> str:
        """See `IExternalPackageSeries`."""
        return self.display_name

    @property
    def bugtarget_parent(self):
        """See `ISeriesBugTarget`."""
        return self.distribution_sourcepackage

    @property
    def distribution_sourcepackage(self):
        """See `IExternalPackageSeries`."""
        return self.distribution.getExternalPackage(
            self.sourcepackagename, self.packagetype, self.channel
        )

    @property
    def series(self):
        """See `ISeriesBugTarget`."""
        return self.distroseries

    @property
    def title(self) -> str:
        """See `IExternalPackageSeries`."""
        return self.display_name

    def __eq__(self, other: "ExternalPackageSeries") -> str:
        """See `IExternalPackageSeries`."""
        return (
            (IExternalPackageSeries.providedBy(other))
            and (self.distroseries.id == other.distroseries.id)
            and (self.sourcepackagename.id == other.sourcepackagename.id)
            and (self.packagetype == other.packagetype)
            and (self.channel == other.channel)
        )

    def __hash__(self) -> int:
        """Return the combined attributes hash."""
        return hash(
            (
                self.distroseries,
                self.sourcepackagename,
                self.packagetype,
                self.display_channel,
            )
        )

    @property
    def drivers(self) -> list:
        """See `IHasDrivers`."""
        return self.distroseries.drivers

    @property
    def official_bug_tags(self) -> list:
        """See `IHasBugs`."""
        return self.distroseries.official_bug_tags

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        return self.distroseries.bug_target_parent

    @property
    def pillar(self) -> IDistribution:
        """See `IBugTarget`."""
        return self.distroseries.pillar

    @property
    def bug_reporting_guidelines(self):
        """See `IBugTarget`."""
        return self.distribution.bug_reporting_guidelines

    @property
    def content_templates(self):
        """See `IBugTarget`."""
        return self.distribution.content_templates

    @property
    def bug_reported_acknowledgement(self):
        """See `IBugTarget`."""
        return self.distribution.bug_reported_acknowledgement

    def getBugSummaryContextWhereClause(self):
        """See `IBugSummaryDimension`."""
        # 2025-08-06 TODO: add support for BugSummary
        return False

    def _getOfficialTagClause(self):
        """See `IBugTarget`."""
        return self.distroseries._getOfficialTagClause()

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this external package series."""
        search_params.setExternalPackageSeries(self)
