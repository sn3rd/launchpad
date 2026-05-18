# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Classes to represent a binary package in a distroarchseries."""

__all__ = [
    "DistroArchSeriesBinaryPackage",
]

from zope.interface import implementer

from lp.app.errors import NotFoundError
from lp.services.database.interfaces import IStore
from lp.services.propertycache import cachedproperty
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.interfaces.distroarchseriesbinarypackage import (
    IDistroArchSeriesBinaryPackage,
)
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
from lp.soyuz.model.distroarchseriesbinarypackagerelease import (
    DistroArchSeriesBinaryPackageRelease,
)
from lp.soyuz.model.publishing import BinaryPackagePublishingHistory
from storm.expr import Cast
from storm.locals import Desc


@implementer(IDistroArchSeriesBinaryPackage)
class DistroArchSeriesBinaryPackage:
    """A Binary Package in the context of a Distro Arch Series.

    Binary Packages are "magic": they don't really exist in the
    database. Instead, they are synthesized based on information from
    the publishing and binarypackagerelease tables.
    """

    def __init__(self, distroarchseries, binarypackagename):
        self.distroarchseries = distroarchseries
        self.binarypackagename = binarypackagename

    @property
    def name(self):
        """See IDistroArchSeriesBinaryPackage."""
        return self.binarypackagename.name

    @property
    def distroseries(self):
        """See IDistroArchSeries."""
        return self.distroarchseries.distroseries

    @property
    def distribution(self):
        """See IDistroArchSeries."""
        return self.distroseries.distribution

    @property
    def displayname(self):
        """See IDistroArchSeriesBinaryPackage."""
        return "%s in %s %s" % (
            self.binarypackagename.name,
            self.distroarchseries.distroseries.name,
            self.distroarchseries.architecturetag,
        )

    @property
    def title(self):
        """See IDistroArchSeriesBinaryPackage."""
        return "%s binary package in %s" % (
            self.binarypackagename.name,
            self.distroarchseries.displayname,
        )

    @cachedproperty
    def cache(self):
        """See IDistroArchSeriesBinaryPackage."""
        from lp.soyuz.model.distroseriespackagecache import (
            DistroSeriesPackageCache,
        )

        return (
            IStore(DistroSeriesPackageCache)
            .find(
                DistroSeriesPackageCache,
                DistroSeriesPackageCache.distroseries == self.distroseries,
                DistroSeriesPackageCache.archive_id.is_in(
                    self.distribution.all_distro_archive_ids
                ),
                DistroSeriesPackageCache.binarypackagename
                == self.binarypackagename,
            )
            .one()
        )

    @property
    def summary(self):
        """See IDistroArchSeriesBinaryPackage."""
        curr = self.currentrelease
        if curr is not None:
            return curr.summary
        if self.cache is not None:
            return self.cache.summary
        return None

    @property
    def description(self):
        """See IDistroArchSeriesBinaryPackage."""
        curr = self.currentrelease
        if curr is not None:
            return curr.description
        if self.cache is not None:
            return self.cache.description
        return None

    def _getPublicationJoins(self):
        return [
            BinaryPackagePublishingHistory.distroarchseries
            == self.distroarchseries,
            BinaryPackagePublishingHistory.archive_id.is_in(
                self.distribution.all_distro_archive_ids
            ),
            BinaryPackagePublishingHistory.binarypackagename
            == self.binarypackagename,
            BinaryPackagePublishingHistory.binarypackagerelease_id
            == BinaryPackageRelease.id,
        ]

    def __getitem__(self, version):
        """See IDistroArchSeriesBinaryPackage."""
        bpph = (
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory,
                Cast(BinaryPackageRelease.version, "text") == version,
                *self._getPublicationJoins(),
            )
            .order_by(Desc(BinaryPackagePublishingHistory.datecreated))
            .first()
        )

        if bpph is None:
            return None
        return DistroArchSeriesBinaryPackageRelease(
            distroarchseries=self.distroarchseries,
            binarypackagerelease=bpph.binarypackagerelease,
        )

    @property
    def releases(self):
        """See IDistroArchSeriesBinaryPackage."""
        ret = (
            IStore(BinaryPackageRelease)
            .find(BinaryPackageRelease, *self._getPublicationJoins())
            .order_by(Desc(BinaryPackageRelease.datecreated))
            .config(distinct=True)
        )
        result = []
        versions = set()
        for bpr in ret:
            if bpr.version not in versions:
                versions.add(bpr.version)
                darbpr = DistroArchSeriesBinaryPackageRelease(
                    distroarchseries=self.distroarchseries,
                    binarypackagerelease=bpr,
                )
                result.append(darbpr)
        return result

    @property
    def currentrelease(self):
        """See IDistroArchSeriesBinaryPackage."""
        try:
            bpph = self.current_published
        except NotFoundError:
            return None
        return DistroArchSeriesBinaryPackageRelease(
            distroarchseries=self.distroarchseries,
            binarypackagerelease=bpph.binarypackagerelease,
        )

    @property
    def publishing_history(self):
        """See IDistroArchSeriesBinaryPackage."""
        return list(
            IStore(BinaryPackagePublishingHistory)
            .find(BinaryPackagePublishingHistory, *self._getPublicationJoins())
            .config(distinct=True)
            .order_by(Desc(BinaryPackagePublishingHistory.datecreated))
        )

    @property
    def current_published(self):
        """See IDistroArchSeriesBinaryPackage."""
        current = (
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory,
                BinaryPackagePublishingHistory.status
                == PackagePublishingStatus.PUBLISHED,
                *self._getPublicationJoins(),
            )
            .order_by(Desc(BinaryPackagePublishingHistory.datecreated))
            .first()
        )

        if current is None:
            raise NotFoundError(
                "Binary package %s not published in %s/%s"
                % (
                    self.binarypackagename.name,
                    self.distroarchseries.distroseries.name,
                    self.distroarchseries.architecturetag,
                )
            )

        return current

    @property
    def distro_source_package(self):
        """See `IDistroArchSeriesBinaryPackage`."""
        # We provide a source package iff we have a current release
        # with a source package release.
        current_release = self.currentrelease
        if current_release is None:
            return None

        src_pkg_release = current_release.distributionsourcepackagerelease
        if src_pkg_release is None:
            return None
        else:
            return src_pkg_release.sourcepackage
