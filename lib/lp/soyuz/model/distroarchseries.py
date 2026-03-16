# Copyright 2009-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = ["DistroArchSeries", "PocketChroot"]

import hashlib
from io import BytesIO

from storm.locals import Bool, Int, Join, Or, Reference, ReferenceSet, Unicode
from storm.store import EmptyResultSet
from zope.component import getUtility
from zope.interface import implementer

from lp.buildmaster.enums import BuildBaseImageType
from lp.buildmaster.model.processor import Processor
from lp.registry.interfaces.person import validate_public_person
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.database.constants import DEFAULT
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import fti_search, rank_by_fti
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.webapp.publisher import (
    get_raw_form_value_from_current_request,
)
from lp.soyuz.adapters.archivedependencies import pocket_dependencies
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuildSet
from lp.soyuz.interfaces.binarypackagename import IBinaryPackageName
from lp.soyuz.interfaces.buildrecords import IHasBuildRecords
from lp.soyuz.interfaces.distroarchseries import (
    ChrootNotPublic,
    FilterSeriesMismatch,
    IDistroArchSeries,
    InvalidChrootUploaded,
    IPocketChroot,
)
from lp.soyuz.interfaces.distroarchseriesfilter import (
    IDistroArchSeriesFilterSet,
)
from lp.soyuz.interfaces.publishing import active_publishing_status
from lp.soyuz.model.binarypackagename import BinaryPackageName
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease


@implementer(IDistroArchSeries, IHasBuildRecords)
class DistroArchSeries(StormBase):
    __storm_table__ = "DistroArchSeries"
    __storm_order__ = "id"

    id = Int(primary=True)
    distroseries_id = Int(name="distroseries", allow_none=False)
    distroseries = Reference(distroseries_id, "DistroSeries.id")
    processor_id = Int(name="processor", allow_none=False)
    processor = Reference(processor_id, Processor.id)
    architecturetag = Unicode(allow_none=False)
    underlying_architecturetag = Unicode(allow_none=True)
    official = Bool(allow_none=False)
    owner_id = Int(
        name="owner", validator=validate_public_person, allow_none=False
    )
    owner = Reference(owner_id, "Person.id")
    package_count = Int(allow_none=False, default=DEFAULT)
    enabled = Bool(allow_none=True, default=True)

    packages = ReferenceSet(
        "id",
        "BinaryPackagePublishingHistory.distroarchseries_id",
        "BinaryPackagePublishingHistory.binarypackagerelease_id",
        "BinaryPackageRelease.id",
    )

    def __init__(
        self,
        distroseries,
        processor,
        architecturetag,
        official,
        owner,
        enabled=True,
        underlying_architecturetag=None,
    ):
        super().__init__()
        self.distroseries = distroseries
        self.processor = processor
        self.architecturetag = architecturetag
        self.official = official
        self.owner = owner
        self.enabled = enabled
        self.underlying_architecturetag = underlying_architecturetag

    def __getitem__(self, name):
        return self.getBinaryPackage(name)

    @property
    def title(self):
        """See `IDistroArchSeries`."""
        return "%s for %s (%s)" % (
            self.distroseries.title,
            self.architecturetag,
            self.processor.name,
        )

    @property
    def displayname(self):
        """See `IDistroArchSeries`."""
        return "%s %s %s" % (
            self.distroseries.distribution.displayname,
            self.distroseries.displayname,
            self.architecturetag,
        )

    @property
    def supports_virtualized(self):
        return self.processor.supports_virtualized

    def updatePackageCount(self):
        """See `IDistroArchSeries`."""
        from lp.soyuz.model.publishing import BinaryPackagePublishingHistory

        self.package_count = (
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory,
                BinaryPackagePublishingHistory.distroarchseries == self,
                BinaryPackagePublishingHistory.archive_id.is_in(
                    self.distroseries.distribution.all_distro_archive_ids
                ),
                BinaryPackagePublishingHistory.status
                == PackagePublishingStatus.PUBLISHED,
                BinaryPackagePublishingHistory.pocket
                == PackagePublishingPocket.RELEASE,
            )
            .count()
        )

    @property
    def isNominatedArchIndep(self):
        """See `IDistroArchSeries`."""
        return (
            self.distroseries.nominatedarchindep is not None
            and self.id == self.distroseries.nominatedarchindep.id
        )

    def getPocketChroot(self, pocket, exact_pocket=False, image_type=None):
        """See `IDistroArchSeries`."""
        if image_type is None:
            image_type = BuildBaseImageType.CHROOT
        pockets = [pocket] if exact_pocket else pocket_dependencies[pocket]
        pocket_chroots = {
            pocket_chroot.pocket: pocket_chroot
            for pocket_chroot in IStore(PocketChroot).find(
                PocketChroot,
                PocketChroot.distroarchseries == self,
                PocketChroot.pocket.is_in(pockets),
                PocketChroot.image_type == image_type,
            )
        }
        for pocket_dep in reversed(pockets):
            if pocket_dep in pocket_chroots:
                pocket_chroot = pocket_chroots[pocket_dep]
                # We normally only return a PocketChroot row that is
                # actually populated with a chroot, but if exact_pocket is
                # set then we return even an unpopulated row in order to
                # avoid constraint violations in addOrUpdateChroot.
                if pocket_chroot.chroot is not None or exact_pocket:
                    return pocket_chroot
        return None

    def getChroot(self, default=None, pocket=None, image_type=None):
        """See `IDistroArchSeries`."""
        if pocket is None:
            pocket = PackagePublishingPocket.RELEASE
        pocket_chroot = self.getPocketChroot(pocket, image_type=image_type)

        if pocket_chroot is None:
            return default

        return pocket_chroot.chroot

    def getChrootURL(self, pocket=None, image_type=None):
        """See `IDistroArchSeries`."""
        chroot = self.getChroot(pocket=pocket, image_type=image_type)
        if chroot is None:
            return None
        return chroot.getURL()

    def getChrootHash(self, pocket, image_type):
        """See `IDistroArchSeries`."""
        chroot = self.getChroot(pocket=pocket, image_type=image_type)
        if chroot is None:
            return None
        return {"sha256": chroot.content.sha256}

    @property
    def chroot_url(self):
        """See `IDistroArchSeries`."""
        return self.getChrootURL()

    def addOrUpdateChroot(self, chroot, pocket=None, image_type=None):
        """See `IDistroArchSeries`."""
        if pocket is None:
            pocket = PackagePublishingPocket.RELEASE
        if image_type is None:
            image_type = BuildBaseImageType.CHROOT
        pocket_chroot = self.getPocketChroot(
            pocket, exact_pocket=True, image_type=image_type
        )

        if pocket_chroot is None:
            return PocketChroot(
                distroarchseries=self,
                pocket=pocket,
                chroot=chroot,
                image_type=image_type,
            )
        else:
            pocket_chroot.chroot = chroot

        return pocket_chroot

    def setChroot(self, data, sha1sum, pocket=None, image_type=None):
        """See `IDistroArchSeries`."""
        # XXX: StevenK 2013-06-06 bug=1116954: We should not need to refetch
        # the file content from the request, since the passed in one has been
        # wrongly encoded.
        data = get_raw_form_value_from_current_request(data, "data")
        if isinstance(data, bytes):
            filecontent = data
        else:
            filecontent = data.read()

        # Due to http://bugs.python.org/issue1349106 launchpadlib sends
        # MIME with \n line endings, which is illegal. lazr.restful
        # parses each ending as \r\n, resulting in a binary that ends
        # with \r getting the last byte chopped off. To cope with this
        # on the server side we try to append \r if the SHA-1 doesn't
        # match.
        content_sha1sum = hashlib.sha1(filecontent).hexdigest()  # nosec B324
        if content_sha1sum != sha1sum:
            filecontent += b"\r"
            content_sha1sum = hashlib.sha1(
                filecontent
            ).hexdigest()  # nosec B324
        if content_sha1sum != sha1sum:
            raise InvalidChrootUploaded("Chroot upload checksums do not match")

        # This duplicates addOrUpdateChroot, but we need it to build a
        # reasonable filename.
        if image_type is None:
            image_type = BuildBaseImageType.CHROOT

        filename = "%s-%s-%s-%s.tar.gz" % (
            image_type.name.lower().split()[0],
            self.distroseries.distribution.name,
            self.distroseries.name,
            self.architecturetag,
        )
        lfa = getUtility(ILibraryFileAliasSet).create(
            name=filename,
            size=len(filecontent),
            file=BytesIO(filecontent),
            contentType="application/octet-stream",
        )
        if lfa.content.sha1 != sha1sum:
            raise InvalidChrootUploaded("Chroot upload checksums do not match")
        self.addOrUpdateChroot(lfa, pocket=pocket, image_type=image_type)

    def setChrootFromBuild(
        self, livefsbuild, filename, pocket=None, image_type=None
    ):
        """See `IDistroArchSeries`."""
        if livefsbuild.is_private:
            # This is disallowed partly because files that act as base
            # images for other builds (including public ones) ought to be
            # public on principle, and partly because
            # BuildFarmJobBehaviourBase.dispatchBuildToWorker doesn't
            # currently support sending a token that would allow builders to
            # fetch private URLs.  If we ever need to change this (perhaps
            # for the sake of short-lived security fixes in base images?),
            # then we need to fix the latter problem first.
            raise ChrootNotPublic()
        self.addOrUpdateChroot(
            livefsbuild.getFileByName(filename),
            pocket=pocket,
            image_type=image_type,
        )

    def removeChroot(self, pocket=None, image_type=None):
        """See `IDistroArchSeries`."""
        self.addOrUpdateChroot(None, pocket=pocket, image_type=image_type)

    def searchBinaryPackages(self, text):
        """See `IDistroArchSeries`."""
        from lp.soyuz.model.publishing import BinaryPackagePublishingHistory

        origin = [
            BinaryPackageRelease,
            Join(
                BinaryPackagePublishingHistory,
                BinaryPackagePublishingHistory.binarypackagerelease
                == BinaryPackageRelease.id,
            ),
            Join(
                BinaryPackageName,
                BinaryPackageRelease.binarypackagename == BinaryPackageName.id,
            ),
        ]

        find_spec = [BinaryPackageRelease, BinaryPackageName]
        archives = self.distroseries.distribution.getArchiveIDList()

        clauses = [
            BinaryPackagePublishingHistory.distroarchseries == self,
            BinaryPackagePublishingHistory.archive_id.is_in(archives),
            BinaryPackagePublishingHistory.status.is_in(
                active_publishing_status
            ),
        ]
        order_by = [BinaryPackageName.name]
        if text:
            ranking = rank_by_fti(BinaryPackageRelease, text)
            find_spec.append(ranking)
            clauses.append(
                Or(
                    fti_search(BinaryPackageRelease, text),
                    BinaryPackageName.name.contains_string(text.lower()),
                )
            )
            order_by.insert(0, ranking)
        result = (
            IStore(BinaryPackageName)
            .using(*origin)
            .find(tuple(find_spec), *clauses)
            .config(distinct=True)
            .order_by(*order_by)
        )

        # import here to avoid circular import problems
        from lp.soyuz.model.distroarchseriesbinarypackagerelease import (
            DistroArchSeriesBinaryPackageRelease,
        )

        # Create a function that will decorate the results, converting
        # them from the find_spec above into DASBPRs.
        def result_to_dasbpr(row):
            return DistroArchSeriesBinaryPackageRelease(
                distroarchseries=self, binarypackagerelease=row[0]
            )

        # Return the decorated result set so the consumer of these
        # results will only see DSPs.
        return DecoratedResultSet(result, result_to_dasbpr)

    def getBinaryPackage(self, name):
        """See `IDistroArchSeries`."""
        from lp.soyuz.model.distroarchseriesbinarypackage import (
            DistroArchSeriesBinaryPackage,
        )

        if not IBinaryPackageName.providedBy(name):
            name = (
                IStore(BinaryPackageName)
                .find(BinaryPackageName, name=name)
                .one()
            )
            if name is None:
                return None
        return DistroArchSeriesBinaryPackage(self, name)

    def getBuildRecords(
        self,
        build_state=None,
        name=None,
        pocket=None,
        arch_tag=None,
        user=None,
        binary_only=True,
    ):
        """See IHasBuildRecords"""
        # Ignore "user", since it would not make any difference to the
        # records returned here (private builds are only in PPA right
        # now).
        # Ignore "binary_only" as for a distro arch series it is only
        # the binaries that are relevant.

        # For consistency we return an empty resultset if arch_tag
        # is provided but doesn't match our architecture.
        if arch_tag is not None and arch_tag != self.architecturetag:
            return EmptyResultSet()

        # Use the facility provided by IBinaryPackageBuildSet to
        # retrieve the records.
        return getUtility(IBinaryPackageBuildSet).getBuildsForDistro(
            self, build_state, name, pocket
        )

    @property
    def main_archive(self):
        return self.distroseries.distribution.main_archive

    def getSourceFilter(self):
        """See `IDistroArchSeries`."""
        return getUtility(IDistroArchSeriesFilterSet).getByDistroArchSeries(
            self
        )

    def setSourceFilter(self, packageset, sense, creator):
        """See `IDistroArchSeries`."""
        if self.distroseries != packageset.distroseries:
            raise FilterSeriesMismatch(self, packageset)
        self.removeSourceFilter()
        getUtility(IDistroArchSeriesFilterSet).new(
            self, packageset, sense, creator
        )

    def removeSourceFilter(self):
        """See `IDistroArchSeries`."""
        dasf = self.getSourceFilter()
        if dasf is not None:
            dasf.destroySelf()

    def isSourceIncluded(self, sourcepackagename):
        """See `IDistroArchSeries`."""
        dasf = self.getSourceFilter()
        if dasf is None:
            return True
        return dasf.isSourceIncluded(sourcepackagename)


@implementer(IPocketChroot)
class PocketChroot(StormBase):
    __storm_table__ = "PocketChroot"

    id = Int(primary=True)

    distroarchseries_id = Int(name="distroarchseries", allow_none=False)
    distroarchseries = Reference(distroarchseries_id, "DistroArchSeries.id")

    pocket = DBEnum(
        enum=PackagePublishingPocket,
        default=PackagePublishingPocket.RELEASE,
        allow_none=False,
    )

    chroot_id = Int(name="chroot", allow_none=True)
    chroot = Reference(chroot_id, "LibraryFileAlias.id")

    image_type = DBEnum(
        enum=BuildBaseImageType,
        default=BuildBaseImageType.CHROOT,
        allow_none=False,
    )

    def __init__(
        self,
        distroarchseries,
        pocket,
        chroot=None,
        image_type=BuildBaseImageType.CHROOT,
    ):
        super().__init__()
        self.distroarchseries = distroarchseries
        self.pocket = pocket
        self.chroot = chroot
        self.image_type = image_type
