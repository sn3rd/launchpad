# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Adapters for registry objects."""

__all__ = [
    "sourcepackage_to_archive",
    "distroseries_to_distribution",
    "PollSubset",
    "productseries_to_product",
    "sourcepackage_to_distribution",
]


from zope.component import getUtility
from zope.interface import implementer
from zope.interface.interfaces import ComponentLookupError

from lp.archivepublisher.interfaces.publisherconfig import IPublisherConfigSet
from lp.registry.enums import PollSort
from lp.registry.interfaces.poll import (
    IPollSet,
    IPollSubset,
    PollAlgorithm,
    PollStatus,
)
from lp.services.webapp.interfaces import ILaunchpadPrincipal


def sourcepackage_to_distribution(source_package):
    """Adapts `ISourcePackage` object to `IDistribution`.

    This also supports `IDistributionSourcePackage`
    """
    return source_package.distribution


def sourcepackage_to_archive(sourcepackage):
    """Adapts `IArchiveSourcePackage` and `IArchiveSourcePackageSeries`
    to `IArchive`.

    This is useful for adapting to `IServiceUsage` or `ILaunchpadUsage`.
    """
    return sourcepackage.archive


def distroseries_to_distribution(distroseries):
    """Adapts `IDistroSeries` object to `IDistribution`.

    This is useful for adapting to `IServiceUsage`
    or `ILaunchpadUsage`."""
    return distroseries.distribution


def person_from_principal(principal):
    """Adapt `ILaunchpadPrincipal` to `IPerson`."""
    if ILaunchpadPrincipal.providedBy(principal):
        if principal.person is None:
            raise ComponentLookupError
        return principal.person
    else:
        # This is not actually necessary when this is used as an adapter
        # from ILaunchpadPrincipal, as we know we always have an
        # ILaunchpadPrincipal.
        #
        # When Zope3 interfaces allow returning None for "cannot adapt"
        # we can return None here.
        # return None
        raise ComponentLookupError


@implementer(IPollSubset)
class PollSubset:
    """Adapt an `IPoll` to an `IPollSubset`."""

    title = "Team polls"

    def __init__(self, team=None):
        self.team = team

    def new(
        self,
        name,
        title,
        proposition,
        dateopens,
        datecloses,
        secrecy,
        allowspoilt,
        poll_type=PollAlgorithm.SIMPLE,
    ):
        """See IPollSubset."""
        assert (
            self.team is not None
        ), "team cannot be None to call this method."
        return getUtility(IPollSet).new(
            self.team,
            name,
            title,
            proposition,
            dateopens,
            datecloses,
            secrecy,
            allowspoilt,
            poll_type,
        )

    def getByName(self, name, default=None):
        """See IPollSubset."""
        assert (
            self.team is not None
        ), "team cannot be None to call this method."
        pollset = getUtility(IPollSet)
        return pollset.getByTeamAndName(self.team, name, default)

    def getAll(self):
        """See IPollSubset."""
        assert (
            self.team is not None
        ), "team cannot be None to call this method."
        return getUtility(IPollSet).findByTeam(self.team)

    def getOpenPolls(self, when=None):
        """See IPollSubset."""
        assert (
            self.team is not None
        ), "team cannot be None to call this method."
        return getUtility(IPollSet).findByTeam(
            self.team, [PollStatus.OPEN], order_by=PollSort.CLOSING, when=when
        )

    def getClosedPolls(self, when=None):
        """See IPollSubset."""
        assert (
            self.team is not None
        ), "team cannot be None to call this method."
        return getUtility(IPollSet).findByTeam(
            self.team,
            [PollStatus.CLOSED],
            order_by=PollSort.CLOSING,
            when=when,
        )

    def getNotYetOpenedPolls(self, when=None):
        """See IPollSubset."""
        assert (
            self.team is not None
        ), "team cannot be None to call this method."
        return getUtility(IPollSet).findByTeam(
            self.team,
            [PollStatus.NOT_YET_OPENED],
            order_by=PollSort.OPENING,
            when=when,
        )


def productseries_to_product(productseries):
    """Adapts `IProductSeries` object to `IProduct`.

    This is useful for adapting to `IHasExternalBugTracker`
    or `ILaunchpadUsage`.
    """
    return productseries.product


def distribution_to_publisherconfig(distro):
    """Adapts `IDistribution` to `IPublisherConfig`."""
    # Used for traversal from distro to +pubconf.
    config = getUtility(IPublisherConfigSet).getByDistribution(distro)
    return config


def package_to_sourcepackagename(package):
    """Adapts a package to its `ISourcePackageName`."""
    return package.sourcepackagename


def information_type_from_product(milestone):
    """Adapts a milestone to product for information_type."""
    return milestone.product
