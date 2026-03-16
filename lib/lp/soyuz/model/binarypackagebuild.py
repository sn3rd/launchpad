# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "BinaryPackageBuild",
    "BinaryPackageBuildSet",
    "COPY_ARCHIVE_SCORE_PENALTY",
    "PRIVATE_ARCHIVE_SCORE_BONUS",
    "SCORE_BY_COMPONENT",
    "SCORE_BY_POCKET",
    "SCORE_BY_URGENCY",
]

import warnings
from datetime import datetime, timedelta, timezone
from operator import attrgetter, itemgetter

import apt_pkg
import six
from debian.deb822 import PkgRelation
from storm.expr import And, Count, Desc, Exists, Join, LeftJoin, Or, Select
from storm.locals import Bool, DateTime, Int, Reference, Unicode
from storm.store import EmptyResultSet, Store
from storm.zope import IResultSet
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NotFoundError
from lp.buildmaster.enums import BuildFarmJobType, BuildStatus
from lp.buildmaster.interfaces.buildfarmjob import IBuildFarmJobSource
from lp.buildmaster.interfaces.packagebuild import is_upload_log
from lp.buildmaster.model.builder import Builder
from lp.buildmaster.model.buildfarmjob import (
    BuildFarmJob,
    SpecificBuildFarmJobSourceMixin,
)
from lp.buildmaster.model.buildqueue import BuildQueue
from lp.buildmaster.model.packagebuild import PackageBuildMixin
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackage import SourcePackageUrgency
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.config import config
from lp.services.database.bulk import load_related
from lp.services.database.constants import DEFAULT
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import sqlvalues
from lp.services.database.stormbase import StormBase
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.librarian.interfaces import ILibraryFileAlias
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.macaroons.interfaces import (
    NO_USER,
    BadMacaroonContext,
    IMacaroonIssuer,
)
from lp.services.macaroons.model import MacaroonIssuerBase
from lp.services.webapp.snapshot import notify_modified
from lp.soyuz.adapters.buildarch import determine_architectures_to_build
from lp.soyuz.enums import ArchivePurpose, BinarySourceReferenceType
from lp.soyuz.interfaces.archive import (
    IArchive,
    InvalidExternalDependencies,
    validate_external_dependencies,
)
from lp.soyuz.interfaces.binarypackagebuild import (
    BuildSetStatus,
    IBinaryPackageBuild,
    IBinaryPackageBuildSet,
    MissingDependencies,
    UnparsableDependencies,
)
from lp.soyuz.interfaces.binarysourcereference import (
    IBinarySourceReferenceSet,
    UnparsableBuiltUsing,
)
from lp.soyuz.interfaces.distroarchseries import IDistroArchSeries
from lp.soyuz.interfaces.packageset import IPackagesetSet
from lp.soyuz.mail.binarypackagebuild import BinaryPackageBuildMailer
from lp.soyuz.model.binarypackagename import BinaryPackageName
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
from lp.soyuz.model.component import Component
from lp.soyuz.model.files import BinaryPackageFile, SourcePackageReleaseFile
from lp.soyuz.model.packageset import Packageset
from lp.soyuz.model.queue import PackageUpload, PackageUploadBuild
from lp.soyuz.model.section import Section

SCORE_BY_POCKET = {
    PackagePublishingPocket.BACKPORTS: 0,
    PackagePublishingPocket.RELEASE: 1500,
    PackagePublishingPocket.PROPOSED: 3000,
    PackagePublishingPocket.UPDATES: 3000,
    PackagePublishingPocket.SECURITY: 4500,
}


SCORE_BY_COMPONENT = {
    "multiverse": 0,
    "universe": 250,
    "restricted": 750,
    "main": 1000,
    "partner": 1250,
}


SCORE_BY_URGENCY = {
    SourcePackageUrgency.LOW: 5,
    SourcePackageUrgency.MEDIUM: 10,
    SourcePackageUrgency.HIGH: 15,
    SourcePackageUrgency.EMERGENCY: 20,
}


PRIVATE_ARCHIVE_SCORE_BONUS = 10000


# Rebuilds have usually a lower priority than other builds.
# This will be subtracted from the final score, usually taking it
# below 0, ensuring they are built only when nothing else is waiting
# in the build farm.
COPY_ARCHIVE_SCORE_PENALTY = 2600


def is_build_virtualized(archive, processor):
    """Should a build for this `IArchive` and `IProcessor` be virtualized?"""
    return archive.require_virtualized or not processor.supports_nonvirtualized


def storm_validate_external_dependencies(build, attr, value):
    assert attr == "external_dependencies"
    errors = validate_external_dependencies(value)
    if len(errors) > 0:
        raise InvalidExternalDependencies(errors)
    return value


@implementer(IBinaryPackageBuild)
class BinaryPackageBuild(PackageBuildMixin, StormBase):
    __storm_table__ = "BinaryPackageBuild"
    __storm_order__ = "id"

    job_type = BuildFarmJobType.PACKAGEBUILD

    id = Int(primary=True)

    build_farm_job_id = Int(name="build_farm_job")
    build_farm_job = Reference(build_farm_job_id, BuildFarmJob.id)

    distro_arch_series_id = Int(name="distro_arch_series", allow_none=False)
    distro_arch_series = Reference(
        distro_arch_series_id, "DistroArchSeries.id"
    )
    source_package_release_id = Int(
        name="source_package_release", allow_none=False
    )
    source_package_release = Reference(
        source_package_release_id, "SourcePackageRelease.id"
    )

    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")

    pocket = DBEnum(
        name="pocket", enum=PackagePublishingPocket, allow_none=False
    )

    arch_indep = Bool()

    upload_log_id = Int(name="upload_log")
    upload_log = Reference(upload_log_id, "LibraryFileAlias.id")

    dependencies = Unicode(name="dependencies")

    processor_id = Int(name="processor")
    processor = Reference(processor_id, "Processor.id")
    virtualized = Bool(name="virtualized")

    date_created = DateTime(
        name="date_created", tzinfo=timezone.utc, allow_none=False
    )
    date_started = DateTime(name="date_started", tzinfo=timezone.utc)
    date_finished = DateTime(name="date_finished", tzinfo=timezone.utc)
    date_first_dispatched = DateTime(
        name="date_first_dispatched", tzinfo=timezone.utc
    )

    builder_id = Int(name="builder")
    builder = Reference(builder_id, "Builder.id")

    status = DBEnum(name="status", enum=BuildStatus, allow_none=False)

    log_id = Int(name="log")
    log = Reference(log_id, "LibraryFileAlias.id")

    failure_count = Int(name="failure_count", allow_none=False)

    distribution_id = Int(name="distribution", allow_none=False)
    distribution = Reference(distribution_id, "Distribution.id")

    distro_series_id = Int(name="distro_series", allow_none=False)
    distro_series = Reference(distro_series_id, "DistroSeries.id")

    is_distro_archive = Bool(name="is_distro_archive", allow_none=False)

    source_package_name_id = Int(name="source_package_name", allow_none=False)
    source_package_name = Reference(
        source_package_name_id, "SourcePackageName.id"
    )

    external_dependencies = Unicode(
        name="external_dependencies",
        validator=storm_validate_external_dependencies,
    )

    buildinfo_id = Int(name="buildinfo")
    buildinfo = Reference(buildinfo_id, "LibraryFileAlias.id")

    def __init__(
        self,
        build_farm_job,
        distro_arch_series,
        source_package_release,
        archive,
        pocket,
        arch_indep,
        status,
        processor=None,
        virtualized=None,
        date_created=DEFAULT,
        builder=None,
        buildinfo=None,
    ):
        # Compute these before creating the row, to avoid violating not-null
        # constraints if these other rows aren't in the cache.
        distribution = distro_arch_series.distroseries.distribution
        distro_series = distro_arch_series.distroseries
        is_distro_archive = archive.is_main
        source_package_name = source_package_release.sourcepackagename

        super().__init__()
        self.build_farm_job = build_farm_job
        self.distro_arch_series = distro_arch_series
        self.source_package_release = source_package_release
        self.archive = archive
        self.pocket = pocket
        self.arch_indep = arch_indep
        self.status = status
        self.processor = processor
        self.virtualized = virtualized
        self.date_created = date_created
        self.builder = builder
        self.buildinfo = buildinfo
        self.distribution = distribution
        self.distro_series = distro_series
        self.is_distro_archive = is_distro_archive
        self.source_package_name = source_package_name

    def getLatestSourcePublication(self):
        from lp.soyuz.model.publishing import SourcePackagePublishingHistory

        store = Store.of(self)
        results = store.find(
            SourcePackagePublishingHistory,
            SourcePackagePublishingHistory.archive == self.archive,
            SourcePackagePublishingHistory.distroseries == self.distro_series,
            SourcePackagePublishingHistory.sourcepackagerelease
            == self.source_package_release,
        )
        return results.order_by(
            Desc(SourcePackagePublishingHistory.id)
        ).first()

    @property
    def current_component(self):
        """See `IBuild`."""
        latest_publication = self.getLatestSourcePublication()
        # Production has some buggy builds without source publications.
        # They seem to have been created by early versions of gina and
        # the readding of hppa.
        if latest_publication is not None:
            return latest_publication.component

    @property
    def current_source_publication(self):
        """See `IBuild`."""
        from lp.soyuz.interfaces.publishing import active_publishing_status

        latest_publication = self.getLatestSourcePublication()
        if (
            latest_publication is not None
            and latest_publication.status in active_publishing_status
        ):
            return latest_publication
        return None

    @property
    def api_source_package_name(self):
        """See `IBuild`."""
        return self.source_package_release.name

    @property
    def source_package_version(self):
        """See `IBuild`."""
        return self.source_package_release.version

    @property
    def upload_changesfile(self):
        """See `IBuild`"""
        package_upload = self.package_upload
        if package_upload is None:
            return None
        return package_upload.changesfile

    @property
    def changesfile_url(self):
        """See `IBinaryPackageBuild`."""
        changesfile = self.upload_changesfile
        if changesfile is None:
            return None
        return ProxiedLibraryFileAlias(changesfile, self).http_url

    @property
    def package_upload(self):
        """See `IBuild`."""
        store = Store.of(self)
        # The join on 'changesfile' is used for pre-fetching the
        # corresponding library file, so callsites don't have to issue an
        # extra query.
        origin = [
            PackageUploadBuild,
            Join(
                PackageUpload,
                PackageUploadBuild.packageupload == PackageUpload.id,
            ),
            Join(
                LibraryFileAlias,
                LibraryFileAlias.id == PackageUpload.changes_file_id,
            ),
            Join(
                LibraryFileContent,
                LibraryFileContent.id == LibraryFileAlias.content_id,
            ),
        ]
        results = store.using(*origin).find(
            (PackageUpload, LibraryFileAlias, LibraryFileContent),
            PackageUploadBuild.build == self,
            PackageUpload.archive == self.archive,
            PackageUpload.distroseries == self.distro_series,
        )

        # Return the unique `PackageUpload` record that corresponds to the
        # upload of the result of this `Build`, load the `LibraryFileAlias`
        # and the `LibraryFileContent` in cache because it's most likely
        # they will be needed.
        return DecoratedResultSet(results, itemgetter(0)).one()

    @property
    def title(self):
        """See `IBuild`"""
        return "%s build of %s %s in %s %s %s" % (
            self.distro_arch_series.architecturetag,
            self.source_package_release.name,
            self.source_package_release.version,
            self.distribution.name,
            self.distro_series.name,
            self.pocket.name,
        )

    @property
    def was_built(self):
        """See `IBuild`"""
        return self.status not in [
            BuildStatus.NEEDSBUILD,
            BuildStatus.BUILDING,
            BuildStatus.GATHERING,
            BuildStatus.UPLOADING,
            BuildStatus.SUPERSEDED,
        ]

    @property
    def arch_tag(self):
        """See `IBuild`."""
        return self.distro_arch_series.architecturetag

    @property
    def log_url(self):
        """See `IPackageBuild`.

        Overridden here for the case of builds for distro archives,
        currently only supported for binary package builds.
        """
        if self.log is None:
            return None
        return ProxiedLibraryFileAlias(self.log, self).http_url

    @property
    def upload_log_url(self):
        """See `IPackageBuild`.

        Overridden here for the case of builds for distro archives,
        currently only supported for binary package builds.
        """
        if self.upload_log is None:
            return None
        return ProxiedLibraryFileAlias(self.upload_log, self).http_url

    @property
    def buildinfo_url(self):
        """See `IBinaryPackageBuild`."""
        buildinfo = self.buildinfo
        if buildinfo is None:
            return None
        return ProxiedLibraryFileAlias(buildinfo, self).http_url

    @property
    def distributionsourcepackagerelease(self):
        """See `IBuild`."""
        from lp.soyuz.model.distributionsourcepackagerelease import (
            DistributionSourcePackageRelease,
        )

        return DistributionSourcePackageRelease(
            distribution=self.distribution,
            sourcepackagerelease=self.source_package_release,
        )

    def calculateScore(self):
        """See `IBuildFarmJob`."""
        score = 0

        # Private builds get uber score.
        if self.archive.private:
            score += PRIVATE_ARCHIVE_SCORE_BONUS

        if self.archive.is_copy:
            score -= COPY_ARCHIVE_SCORE_PENALTY

        score += self.archive.relative_build_score

        # Language packs don't get any of the usual package-specific
        # score bumps, as they unduly delay the building of packages in
        # the main component otherwise.
        if self.source_package_release.section.name == "translations":
            return score

        # Calculates the urgency-related part of the score.
        score += SCORE_BY_URGENCY[self.source_package_release.urgency]

        # Calculates the pocket-related part of the score.
        score += SCORE_BY_POCKET[self.pocket]

        # Calculates the component-related part of the score.
        score += SCORE_BY_COMPONENT.get(self.current_component.name, 0)

        # Calculates the package-set-related part of the score.
        package_sets = getUtility(IPackagesetSet).setsIncludingSource(
            self.source_package_release.name, distroseries=self.distro_series
        )
        if not self.archive.is_ppa and not package_sets.is_empty():
            score += package_sets.max(Packageset.relative_build_score)

        return score

    def getBinaryPackageNamesForDisplay(self):
        """See `IBuildView`."""
        store = Store.of(self)
        result = store.find(
            (BinaryPackageRelease, BinaryPackageName),
            BinaryPackageRelease.build == self,
            BinaryPackageRelease.binarypackagename == BinaryPackageName.id,
            BinaryPackageName.id == BinaryPackageRelease.binarypackagename_id,
        )
        return result.order_by(
            [BinaryPackageName.name, BinaryPackageRelease.id]
        )

    def getBinaryFilesForDisplay(self):
        """See `IBuildView`."""
        store = Store.of(self)
        result = store.find(
            (
                BinaryPackageRelease,
                BinaryPackageFile,
                LibraryFileAlias,
                LibraryFileContent,
            ),
            BinaryPackageRelease.build == self,
            BinaryPackageRelease.id
            == BinaryPackageFile.binarypackagerelease_id,
            LibraryFileAlias.id == BinaryPackageFile.libraryfile_id,
            LibraryFileContent.id == LibraryFileAlias.content_id,
        )
        return result.order_by(
            [LibraryFileAlias.filename, BinaryPackageRelease.id]
        ).config(distinct=True)

    @property
    def binarypackages(self):
        """See `IBuild`."""
        return DecoratedResultSet(
            Store.of(self)
            .using(
                BinaryPackageRelease,
                LeftJoin(
                    BinaryPackageName,
                    BinaryPackageRelease.binarypackagename_id
                    == BinaryPackageName.id,
                ),
                LeftJoin(
                    Component,
                    BinaryPackageRelease.component_id == Component.id,
                ),
                LeftJoin(
                    Section, BinaryPackageRelease.section_id == Section.id
                ),
            )
            .find(
                (BinaryPackageRelease, BinaryPackageName, Component, Section),
                BinaryPackageRelease.build == self,
            )
            .order_by(BinaryPackageName.name, BinaryPackageRelease.id),
            result_decorator=itemgetter(0),
        )

    @property
    def distroarchseriesbinarypackages(self):
        """See `IBuild`."""
        # Avoid circular import by importing locally.
        from lp.soyuz.model.distroarchseriesbinarypackagerelease import (
            DistroArchSeriesBinaryPackageRelease,
        )

        return [
            DistroArchSeriesBinaryPackageRelease(self.distro_arch_series, bp)
            for bp in self.binarypackages
        ]

    def updateStatus(
        self,
        status,
        builder=None,
        worker_status=None,
        date_started=None,
        date_finished=None,
        force_invalid_transition=False,
    ):
        edited_fields = set()
        with notify_modified(
            self, edited_fields, snapshot_names=("status",)
        ) as previous_obj:
            super().updateStatus(
                status,
                builder=builder,
                worker_status=worker_status,
                date_started=date_started,
                date_finished=date_finished,
                force_invalid_transition=force_invalid_transition,
            )
            if self.status != previous_obj.status:
                edited_fields.add("status")

    @property
    def can_be_retried(self):
        """See `IBuildFarmJob`."""
        # First check that the worker scanner would pick up the build record
        # if we reset it.
        if not self.archive.canModifySuite(self.distro_series, self.pocket):
            # The worker scanner would not pick this up, so it cannot be
            # re-tried.
            return False
        return super().can_be_retried

    def resetBuild(self):
        """See `IBuildFarmJob`."""
        super().resetBuild()
        self.virtualized = is_build_virtualized(self.archive, self.processor)

    @property
    def api_score(self):
        """See `IBinaryPackageBuild`."""
        # Score of the related buildqueue record (if any)
        if self.buildqueue_record is None:
            return None
        else:
            return self.buildqueue_record.lastscore

    def _parseDependencyToken(self, token):
        """Parse the given token.

        Raises AssertionError if the given token couldn't be parsed.

        Return a triple containing the corresponding (name, version,
        relation) for the given dependency token.
        """
        assert "name" in token
        assert "version" in token
        if token["version"] is None:
            relation = ""
            version = ""
        else:
            relation, version = token["version"]
            if relation == "<":
                relation = "<<"
            elif relation == ">":
                relation = ">>"
        return (token["name"], version, relation)

    def _checkDependencyVersion(self, available, required, relation):
        """Return True if the available version satisfies the context."""
        # This dict maps the package version relationship syntax in lambda
        # functions which returns boolean according to the results of the
        # apt_pkg.version_compare function (see the order above).
        # For further information about pkg relationship syntax see:
        #
        # http://www.debian.org/doc/debian-policy/ch-relationships.html
        #
        version_relation_map = {
            # any version is acceptable if no relationship is given
            "": lambda x: True,
            # strictly later
            ">>": lambda x: x > 0,
            # later or equal
            ">=": lambda x: x >= 0,
            # strictly equal
            "=": lambda x: x == 0,
            # earlier or equal
            "<=": lambda x: x <= 0,
            # strictly earlier
            "<<": lambda x: x < 0,
        }

        # Use apt_pkg function to compare versions
        # it behaves similar to cmp, i.e. returns negative
        # if first < second, zero if first == second and
        # positive if first > second.
        dep_result = apt_pkg.version_compare(available, required)

        return version_relation_map[relation](dep_result)

    def _isDependencySatisfied(self, token):
        """Check if the given dependency token is satisfied.

        Check if the dependency exists and that its version constraint is
        satisfied.
        """
        name, version, relation = self._parseDependencyToken(token)

        # There may be several published versions in the available
        # archives and pockets. If any one of them satisfies our
        # constraints, the dependency is satisfied.
        dep_candidates = self.archive.findDepCandidates(
            self.distro_arch_series,
            self.pocket,
            self.current_component,
            self.source_package_release.sourcepackagename.name,
            name,
        )

        for dep_candidate in dep_candidates:
            if self._checkDependencyVersion(
                dep_candidate.binarypackagerelease.version, version, relation
            ):
                return True

        return False

    def updateDependencies(self):
        """See `IBuild`."""

        # apt_pkg requires init_system to get version_compare working
        # properly.
        apt_pkg.init_system()

        # Check package build dependencies using debian.deb822

        if self.dependencies is None:
            raise MissingDependencies(
                "Build dependencies for %s (%s) are missing.\n"
                "It indicates that something is wrong in buildd-workers."
                % (self.title, self.id)
            )

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                parsed_deps = PkgRelation.parse_relations(self.dependencies)
        except (AttributeError, Warning):
            raise UnparsableDependencies(
                "Build dependencies for %s (%s) could not be parsed: '%s'\n"
                "It indicates that something is wrong in buildd-workers."
                % (self.title, self.id, self.dependencies)
            )

        # For each dependency check if there is a name (without spaces) and
        # version. See TestBuildUpdateDependencies.testInvalidDependencies()

        def has_valid_name(dep):
            return (
                dep.get("name") is not None
                and len(dep.get("name", "").split(" ")) == 1
            )

        def has_valid_version(dep):
            return dep.get("version") is None or len(dep.get("version")) == 2

        for or_dep in parsed_deps:
            for dep in or_dep:
                if not has_valid_name(dep) or not has_valid_version(dep):
                    raise UnparsableDependencies(
                        "Build dependencies for %s (%s) could not be parsed: "
                        "'%s'\nIt indicates that something is wrong in "
                        "buildd-workers."
                        % (self.title, self.id, self.dependencies)
                    )

        remaining_deps = []
        for or_dep in parsed_deps:
            if not any(self._isDependencySatisfied(token) for token in or_dep):
                remaining_deps.append(or_dep)

        # Update dependencies line
        self.dependencies = six.ensure_text(PkgRelation.str(remaining_deps))

    def __getitem__(self, name):
        return self.getBinaryPackageRelease(name)

    def getBinaryPackageRelease(self, name):
        """See `IBuild`."""
        for binpkg in self.binarypackages:
            if binpkg.name == name:
                return binpkg
        raise NotFoundError('No binary package "%s" in build' % name)

    def createBinaryPackageRelease(
        self,
        binarypackagename,
        version,
        summary,
        description,
        binpackageformat,
        component,
        section,
        priority,
        installedsize,
        architecturespecific,
        shlibdeps=None,
        depends=None,
        recommends=None,
        suggests=None,
        conflicts=None,
        replaces=None,
        provides=None,
        pre_depends=None,
        enhances=None,
        breaks=None,
        built_using=None,
        essential=False,
        debug_package=None,
        user_defined_fields=None,
        homepage=None,
    ):
        """See IBuild."""
        bpr = BinaryPackageRelease(
            build=self,
            binarypackagename=binarypackagename,
            version=version,
            summary=summary,
            description=description,
            binpackageformat=binpackageformat,
            component=component,
            section=section,
            priority=priority,
            shlibdeps=shlibdeps,
            depends=depends,
            recommends=recommends,
            suggests=suggests,
            conflicts=conflicts,
            replaces=replaces,
            provides=provides,
            pre_depends=pre_depends,
            enhances=enhances,
            breaks=breaks,
            essential=essential,
            installedsize=installedsize,
            architecturespecific=architecturespecific,
            debug_package=debug_package,
            user_defined_fields=user_defined_fields,
            homepage=homepage,
        )
        if built_using:
            try:
                getUtility(IBinarySourceReferenceSet).createFromRelationship(
                    bpr, built_using, BinarySourceReferenceType.BUILT_USING
                )
            except UnparsableBuiltUsing:
                # XXX cjwatson 2020-04-29: We intend for this to fail the
                # upload eventually, but still have some details to sort
                # out, so ignore it for now.
                pass
        return bpr

    def estimateDuration(self):
        """See `IPackageBuild`."""
        # Always include the primary archive when looking for
        # past build times (just in case that none can be found
        # in a PPA or copy archive).
        archives = [self.archive.id]
        if self.archive.purpose != ArchivePurpose.PRIMARY:
            archives.append(self.distro_arch_series.main_archive.id)

        # Look for all sourcepackagerelease instances that match the name
        # and get the (successfully built) build records for this
        # package.
        completed_builds = Store.of(self).find(
            BinaryPackageBuild,
            BinaryPackageBuild.archive_id.is_in(archives),
            BinaryPackageBuild.distro_arch_series == self.distro_arch_series,
            BinaryPackageBuild.source_package_name == self.source_package_name,
            BinaryPackageBuild.date_finished != None,
            BinaryPackageBuild.status == BuildStatus.FULLYBUILT,
            BinaryPackageBuild.id != self.id,
        )
        most_recent_build = completed_builds.order_by(
            Desc(BinaryPackageBuild.date_finished), Desc(BinaryPackageBuild.id)
        ).first()
        if most_recent_build is not None and most_recent_build.duration:
            # Historic build data exists, use the most recent value -
            # assuming it has valid data.
            return most_recent_build.duration

        # Estimate the build duration based on package size if no
        # historic build data exists.
        # Get the package size in KB.
        package_size = self.source_package_release.getPackageSize()
        if package_size > 0:
            # Analysis of previous build data shows that a build rate
            # of 6 KB/second is realistic. Furthermore we have to add
            # another minute for generic build overhead.
            estimate = int(package_size / 6.0 / 60 + 1)
        else:
            # No historic build times and no package size available,
            # assume a build time of 5 minutes.
            estimate = 5
        return timedelta(minutes=estimate)

    def addBuildInfo(self, buildinfo):
        """See `IBinaryPackageBuild`."""
        if self.buildinfo is None:
            self.buildinfo = buildinfo
        else:
            assert self.buildinfo == buildinfo

    def verifySuccessfulUpload(self) -> bool:
        return not self.binarypackages.is_empty()

    def notify(self, extra_info=None):
        """See `IPackageBuild`.

        If config.buildmaster.send_build_notification is disabled, simply
        return.

        If config.builddmaster.notify_owner is enabled and SPR.creator
        has preferredemail it will send an email to the creator, Bcc:
        to the config.builddmaster.default_recipient. If one of the
        conditions was not satisfied, no preferredemail found (autosync
        or untouched packages from debian) or config options disabled,
        it will only send email to the specified default recipient.

        This notification will contain useful information about
        the record in question (all states are supported), see
        doc/build-notification.rst for further information.
        """
        if not config.builddmaster.send_build_notification:
            return
        if self.status == BuildStatus.FULLYBUILT:
            return
        mailer = BinaryPackageBuildMailer.forStatus(
            self, extra_info=extra_info
        )
        mailer.sendAll()

    def _getDebByFileName(self, filename):
        """Helper function to get a .deb LFA in the context of this build."""
        bpf = self.getBinaryPackageFileByName(filename)
        if bpf is not None:
            return bpf.libraryfile
        else:
            return None

    def getFileByName(self, filename):
        """See `IBuild`."""
        if filename.endswith(".changes"):
            file_object = self.upload_changesfile
        elif filename.endswith(".buildinfo"):
            file_object = self.buildinfo
        elif filename.endswith(".txt.gz"):
            file_object = self.log
        elif is_upload_log(filename):
            file_object = self.upload_log
        elif filename.endswith("deb"):
            file_object = self._getDebByFileName(filename)
        else:
            raise NotFoundError(filename)

        if file_object is not None and file_object.filename == filename:
            return file_object

        raise NotFoundError(filename)

    def getBinaryPackageFileByName(self, filename):
        """See `IBuild`."""
        return (
            Store.of(self)
            .find(
                BinaryPackageFile,
                BinaryPackageRelease.build == self.id,
                BinaryPackageFile.binarypackagerelease
                == BinaryPackageRelease.id,
                LibraryFileAlias.id == BinaryPackageFile.libraryfile_id,
                LibraryFileAlias.filename == filename,
            )
            .one()
        )

    def getUploader(self, changes):
        """See `IBinaryPackageBuild`."""
        return changes.signer

    @property
    def api_external_dependencies(self):
        return self.external_dependencies

    @api_external_dependencies.setter
    def api_external_dependencies(self, value):
        self.external_dependencies = value


@implementer(IBinaryPackageBuildSet)
class BinaryPackageBuildSet(SpecificBuildFarmJobSourceMixin):
    def new(
        self,
        source_package_release,
        archive,
        distro_arch_series,
        pocket,
        arch_indep=None,
        status=BuildStatus.NEEDSBUILD,
        builder=None,
        buildinfo=None,
    ):
        """See `IBinaryPackageBuildSet`."""
        # Force the current timestamp instead of the default UTC_NOW for
        # the transaction, avoid several row with same datecreated.
        if arch_indep is None:
            arch_indep = distro_arch_series.isNominatedArchIndep
        date_created = datetime.now(timezone.utc)
        # Create the BuildFarmJob for the new BinaryPackageBuild.
        build_farm_job = getUtility(IBuildFarmJobSource).new(
            BinaryPackageBuild.job_type, status, date_created, builder, archive
        )
        processor = distro_arch_series.processor
        virtualized = is_build_virtualized(archive, processor)
        build = BinaryPackageBuild(
            build_farm_job=build_farm_job,
            distro_arch_series=distro_arch_series,
            source_package_release=source_package_release,
            archive=archive,
            pocket=pocket,
            arch_indep=arch_indep,
            status=status,
            processor=processor,
            virtualized=virtualized,
            builder=builder,
            buildinfo=buildinfo,
            date_created=date_created,
        )
        IStore(build).flush()
        return build

    def getByID(self, id):
        """See `IBinaryPackageBuildSet`."""
        build = IStore(BinaryPackageBuild).get(BinaryPackageBuild, id)
        if build is None:
            raise NotFoundError(id)
        return build

    def getByBuildFarmJob(self, build_farm_job):
        """See `ISpecificBuildFarmJobSource`."""
        return (
            Store.of(build_farm_job)
            .find(BinaryPackageBuild, build_farm_job_id=build_farm_job.id)
            .one()
        )

    def preloadBuildsData(self, builds):
        # Circular imports.
        from lp.registry.model.distribution import Distribution
        from lp.registry.model.distroseries import DistroSeries
        from lp.registry.model.person import Person
        from lp.soyuz.model.archive import Archive
        from lp.soyuz.model.distroarchseries import DistroArchSeries

        self._prefetchBuildData(builds)
        das = load_related(DistroArchSeries, builds, ["distro_arch_series_id"])
        archives = load_related(Archive, builds, ["archive_id"])
        load_related(Person, archives, ["owner_id"])
        distroseries = load_related(DistroSeries, das, ["distroseries_id"])
        load_related(Distribution, distroseries, ["distribution_id"])

    def getByBuildFarmJobs(self, build_farm_jobs):
        """See `ISpecificBuildFarmJobSource`."""
        if len(build_farm_jobs) == 0:
            return EmptyResultSet()
        rows = Store.of(build_farm_jobs[0]).find(
            BinaryPackageBuild,
            BinaryPackageBuild.build_farm_job_id.is_in(
                bfj.id for bfj in build_farm_jobs
            ),
        )
        return DecoratedResultSet(rows, pre_iter_hook=self.preloadBuildsData)

    def getBySourceAndLocation(
        self, source_package_release, archive, distro_arch_series
    ):
        return (
            IStore(BinaryPackageBuild)
            .find(
                BinaryPackageBuild,
                source_package_release=source_package_release,
                archive=archive,
                distro_arch_series=distro_arch_series,
            )
            .one()
        )

    def handleOptionalParamsForBuildQueries(
        self,
        clauses,
        origin,
        status=None,
        name=None,
        pocket=None,
        arch_tag=None,
    ):
        """Construct query clauses needed/shared by all getBuild..() methods.

        This method is not exposed via the public interface as it is only
        used to DRY-up trusted code.

        :param clauses: container to which to add any resulting query clauses.
        :param origin: container to which to add joined tables.
        :param status: optional build state for which to add a query clause if
            present.
        :param name: optional source package release name (or list of source
            package release names) for which to add a query clause if
            present.
        :param pocket: optional pocket (or list of pockets) for which to add a
            query clause if present.
        :param arch_tag: optional architecture tag for which to add a
            query clause if present.
        """
        # Circular. :(
        from lp.soyuz.model.distroarchseries import DistroArchSeries

        origin.append(BinaryPackageBuild)

        # Add query clause that filters on build state if the latter is
        # provided.
        if status is not None:
            clauses.append(BinaryPackageBuild.status == status)

        # Add query clause that filters on pocket if the latter is provided.
        if pocket:
            if not isinstance(pocket, (list, tuple)):
                pocket = (pocket,)
            clauses.append(BinaryPackageBuild.pocket.is_in(pocket))

        # Add query clause that filters on architecture tag if provided.
        if arch_tag is not None:
            clauses.append(
                BinaryPackageBuild.distro_arch_series_id == DistroArchSeries.id
            )
            if not isinstance(arch_tag, (list, tuple)):
                arch_tag = (arch_tag,)
            clauses.append(DistroArchSeries.architecturetag.is_in(arch_tag))
            origin.append(DistroArchSeries)

        # Add query clause that filters on source package release name if the
        # latter is provided.
        if name is not None:
            clauses.append(
                BinaryPackageBuild.source_package_name_id
                == SourcePackageName.id
            )
            origin.extend([SourcePackageName])
            if not isinstance(name, (list, tuple)):
                clauses.append(SourcePackageName.name.contains_string(name))
            else:
                clauses.append(SourcePackageName.name.is_in(name))

    def getBuildsForBuilder(
        self,
        builder_id,
        status=None,
        name=None,
        pocket=None,
        arch_tag=None,
        user=None,
    ):
        """See `IBinaryPackageBuildSet`."""
        # Circular. :(
        from lp.soyuz.model.archive import Archive, get_archive_privacy_filter

        clauses = [
            BinaryPackageBuild.archive_id == Archive.id,
            BinaryPackageBuild.builder_id == builder_id,
            get_archive_privacy_filter(user),
        ]
        origin = [Archive]

        self.handleOptionalParamsForBuildQueries(
            clauses, origin, status, name, pocket, arch_tag=arch_tag
        )

        return (
            IStore(BinaryPackageBuild)
            .using(*origin)
            .find(BinaryPackageBuild, *clauses)
            .order_by(
                Desc(BinaryPackageBuild.date_finished), BinaryPackageBuild.id
            )
        )

    def getBuildsForArchive(
        self, archive, status=None, name=None, pocket=None, arch_tag=None
    ):
        """See `IBinaryPackageBuildSet`."""
        clauses = [BinaryPackageBuild.archive_id == archive.id]
        origin = []

        self.handleOptionalParamsForBuildQueries(
            clauses, origin, status, name, pocket, arch_tag
        )

        # Ordering according status
        # * SUPERSEDED & All by -datecreated
        # * FULLYBUILT & FAILURES by -datebuilt
        # It should present the builds in a more natural order.
        if status == BuildStatus.SUPERSEDED or status is None:
            orderBy = [Desc(BinaryPackageBuild.date_created)]
        else:
            orderBy = [Desc(BinaryPackageBuild.date_finished)]
        # All orders fallback to id if the primary order doesn't succeed
        orderBy.append(BinaryPackageBuild.id)

        return self._decorate_with_prejoins(
            IStore(BinaryPackageBuild)
            .using(*origin)
            .find(BinaryPackageBuild, *clauses)
            .order_by(*orderBy)
        )

    def getBuildsForDistro(
        self, context, status=None, name=None, pocket=None, arch_tag=None
    ):
        """See `IBinaryPackageBuildSet`."""
        if IDistribution.providedBy(context):
            col = BinaryPackageBuild.distribution_id
        elif IDistroSeries.providedBy(context):
            col = BinaryPackageBuild.distro_series_id
        elif IDistroArchSeries.providedBy(context):
            col = BinaryPackageBuild.distro_arch_series_id
        else:
            raise AssertionError("Unsupported context: %r" % context)
        condition_clauses = [
            col == context.id,
            BinaryPackageBuild.is_distro_archive,
        ]

        # XXX cprov 2006-09-25: It would be nice if we could encapsulate
        # the chunk of code below (which deals with the optional parameters)
        # and share it with ISourcePackage.getBuildRecords()

        # exclude gina-generated and security (dak-made) builds
        # status == FULLYBUILT && datebuilt == null
        if status == BuildStatus.FULLYBUILT:
            condition_clauses.append(BinaryPackageBuild.date_finished != None)
        else:
            condition_clauses.append(
                Or(
                    BinaryPackageBuild.status != BuildStatus.FULLYBUILT,
                    BinaryPackageBuild.date_finished != None,
                )
            )

        # Ordering according status
        # * NEEDSBUILD, BUILDING, GATHERING & UPLOADING by -lastscore
        # * SUPERSEDED & All by -BinaryPackageBuild.id
        #   (nearly equivalent to -datecreated, but much more
        #   efficient.)
        # * FULLYBUILT & FAILURES by -datebuilt
        # It should present the builds in a more natural order.
        clauseTables = []
        order_by_table = None
        if status in [
            BuildStatus.NEEDSBUILD,
            BuildStatus.BUILDING,
            BuildStatus.GATHERING,
            BuildStatus.UPLOADING,
        ]:
            order_by = [Desc(BuildQueue.lastscore), BinaryPackageBuild.id]
            order_by_table = BuildQueue
            clauseTables.append(BuildQueue)
            condition_clauses.append(
                BuildQueue._build_farm_job_id
                == BinaryPackageBuild.build_farm_job_id
            )
        elif status == BuildStatus.SUPERSEDED or status is None:
            order_by = [Desc(BinaryPackageBuild.id)]
        else:
            order_by = [
                Desc(BinaryPackageBuild.date_finished),
                BinaryPackageBuild.id,
            ]

        # End of duplication (see XXX cprov 2006-09-25 above).

        self.handleOptionalParamsForBuildQueries(
            condition_clauses, clauseTables, status, name, pocket, arch_tag
        )

        find_spec = (BinaryPackageBuild,)
        if order_by_table:
            find_spec = find_spec + (order_by_table,)
        result_set = (
            IStore(BinaryPackageBuild)
            .using(*clauseTables)
            .find(find_spec, *condition_clauses)
        )
        result_set.order_by(*order_by)

        return self._decorate_with_prejoins(
            DecoratedResultSet(result_set, result_decorator=itemgetter(0))
        )

    def getCountsForDistro(self, context, date_finished_since=None):
        """See `IBinaryPackageBuildSet`."""
        if IDistribution.providedBy(context):
            col = BinaryPackageBuild.distribution_id
        elif IDistroSeries.providedBy(context):
            col = BinaryPackageBuild.distro_series_id
        elif IDistroArchSeries.providedBy(context):
            col = BinaryPackageBuild.distro_arch_series_id
        else:
            raise AssertionError("Unsupported context: %r" % context)
        clauses = [
            col == context.id,
            BinaryPackageBuild.is_distro_archive,
            # Exclude gina-generated builds.
            Or(
                BinaryPackageBuild.status != BuildStatus.FULLYBUILT,
                BinaryPackageBuild.date_finished != None,
            ),
        ]
        if date_finished_since is not None:
            clauses.append(
                BinaryPackageBuild.date_finished >= date_finished_since
            )
        rows = (
            IStore(BinaryPackageBuild)
            .find(
                (BinaryPackageBuild.status, Count()),
                *clauses,
            )
            .group_by(BinaryPackageBuild.status)
        )
        return dict(rows)

    def _decorate_with_prejoins(self, result_set):
        """Decorate build records with related data prefetch functionality."""
        # Grab the native storm result set.
        result_set = IResultSet(result_set)
        decorated_results = DecoratedResultSet(
            result_set, pre_iter_hook=self._prefetchBuildData
        )
        return decorated_results

    def getBuildsBySourcePackageRelease(
        self, sourcepackagerelease_ids, buildstate=None
    ):
        """See `IBinaryPackageBuildSet`."""
        if (
            sourcepackagerelease_ids is None
            or len(sourcepackagerelease_ids) == 0
        ):
            return []
        query = [
            BinaryPackageBuild.source_package_release_id.is_in(
                sourcepackagerelease_ids
            ),
            BinaryPackageBuild.is_distro_archive,
        ]

        if buildstate is not None:
            query.append(BinaryPackageBuild.status == buildstate)

        resultset = IStore(BinaryPackageBuild).find(BinaryPackageBuild, *query)
        resultset.order_by(
            Desc(BinaryPackageBuild.date_created), BinaryPackageBuild.id
        )
        return resultset

    def findBuiltOrPublishedBySourceAndArchive(
        self, sourcepackagerelease, archive
    ):
        """See `IBinaryPackageBuildSet`."""
        from lp.soyuz.model.publishing import BinaryPackagePublishingHistory

        published_query = Select(
            1,
            tables=(
                BinaryPackagePublishingHistory,
                Join(
                    BinaryPackageRelease,
                    BinaryPackageRelease.id
                    == BinaryPackagePublishingHistory.binarypackagerelease_id,
                ),
            ),
            where=And(
                BinaryPackagePublishingHistory.archive == archive,
                BinaryPackageRelease.build == BinaryPackageBuild.id,
            ),
        )
        builds = list(
            IStore(BinaryPackageBuild).find(
                BinaryPackageBuild,
                BinaryPackageBuild.status == BuildStatus.FULLYBUILT,
                BinaryPackageBuild.source_package_release
                == sourcepackagerelease,
                Or(
                    BinaryPackageBuild.archive == archive,
                    Exists(published_query),
                ),
            )
        )
        # XXX wgrant 2014-11-07: We should be able to assert here that
        # each archtag appeared only once, as a second successful build
        # should fail to upload due to file conflicts. Unfortunately,
        # there are 1141 conflicting builds in the primary archive on
        # production, and they're non-trivial to clean up.
        # Let's just hope that nothing of value will be lost if we omit
        # duplicates.
        arch_map = {
            build.distro_arch_series.architecturetag: build for build in builds
        }
        return arch_map

    def getStatusSummaryForBuilds(self, builds):
        """See `IBinaryPackageBuildSet`."""

        # Create a small helper function to collect the builds for a given
        # list of build states:
        def collect_builds(*states):
            wanted = []
            for state in states:
                candidates = [
                    build for build in builds if build.status == state
                ]
                wanted.extend(candidates)
            return wanted

        failed = collect_builds(
            BuildStatus.FAILEDTOBUILD,
            BuildStatus.MANUALDEPWAIT,
            BuildStatus.CHROOTWAIT,
            BuildStatus.FAILEDTOUPLOAD,
        )
        needsbuild = collect_builds(BuildStatus.NEEDSBUILD)
        building = collect_builds(
            BuildStatus.BUILDING, BuildStatus.GATHERING, BuildStatus.UPLOADING
        )
        successful = collect_builds(BuildStatus.FULLYBUILT)

        # Note: the BuildStatus DBItems are used here to summarize the
        # status of a set of builds:s
        if len(building) != 0:
            return {
                "status": BuildSetStatus.BUILDING,
                "builds": building,
            }
        elif len(needsbuild) != 0:
            return {
                "status": BuildSetStatus.NEEDSBUILD,
                "builds": needsbuild,
            }
        elif len(failed) != 0:
            return {
                "status": BuildSetStatus.FAILEDTOBUILD,
                "builds": failed,
            }
        else:
            return {
                "status": BuildSetStatus.FULLYBUILT,
                "builds": successful,
            }

    def _prefetchBuildData(self, results):
        """Used to pre-populate the cache with build related data.

        When dealing with a group of Build records we can't use the
        prejoin facility to also fetch BuildQueue, SourcePackageRelease
        and LibraryFileAlias records in a single query because the
        result set is too large and the queries time out too often.

        So this method receives a list of Build instances and fetches the
        corresponding SourcePackageRelease and LibraryFileAlias rows
        (prejoined with the appropriate SourcePackageName and
        LibraryFileContent respectively) as well as builders related to the
        Builds at hand.
        """
        from lp.registry.model.sourcepackagename import SourcePackageName
        from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease

        # Prefetching is not needed if the original result set is empty.
        if len(results) == 0:
            return

        build_ids = [build.id for build in results]
        origin = (
            BinaryPackageBuild,
            Join(
                SourcePackageRelease,
                (
                    SourcePackageRelease.id
                    == BinaryPackageBuild.source_package_release_id
                ),
            ),
            Join(
                SourcePackageName,
                SourcePackageName.id
                == SourcePackageRelease.sourcepackagename_id,
            ),
            LeftJoin(
                LibraryFileAlias,
                LibraryFileAlias.id == BinaryPackageBuild.log_id,
            ),
            LeftJoin(
                LibraryFileContent,
                LibraryFileContent.id == LibraryFileAlias.content_id,
            ),
            LeftJoin(Builder, Builder.id == BinaryPackageBuild.builder_id),
        )
        result_set = (
            IStore(BinaryPackageBuild)
            .using(*origin)
            .find(
                (
                    SourcePackageRelease,
                    LibraryFileAlias,
                    SourcePackageName,
                    LibraryFileContent,
                    Builder,
                ),
                BinaryPackageBuild.id.is_in(build_ids),
            )
        )

        # Force query execution so that the ancillary data gets fetched
        # and added to StupidCache.
        # We are doing this here because there is no "real" caller of
        # this (pre_iter_hook()) method that will iterate over the
        # result set and force the query execution that way.
        return list(result_set)

    def getByQueueEntry(self, queue_entry):
        """See `IBinaryPackageBuildSet`."""
        bfj_id = removeSecurityProxy(queue_entry)._build_farm_job_id
        return (
            IStore(BinaryPackageBuild)
            .find(BinaryPackageBuild, build_farm_job_id=bfj_id)
            .one()
        )

    @staticmethod
    def addCandidateSelectionCriteria():
        """See `ISpecificBuildFarmJobSource`."""
        return """
            SELECT TRUE FROM BinaryPackageBuild
            WHERE
            BinaryPackageBuild.build_farm_job = BuildQueue.build_farm_job AND
            BinaryPackageBuild.status = %s
        """ % sqlvalues(
            BuildStatus.NEEDSBUILD
        )

    @staticmethod
    def postprocessCandidate(job, logger):
        """See `ISpecificBuildFarmJobSource`."""
        # Mark build records targeted to old source versions as SUPERSEDED
        # and build records target to SECURITY pocket or against an OBSOLETE
        # distroseries without a flag as FAILEDTOBUILD.
        # Builds in those situation should not be built because they will
        # be wasting build-time.  In the former case, there is already a
        # newer source; the latter case needs an overhaul of the way
        # security builds are handled (by copying from a PPA) to avoid
        # creating duplicate builds.
        build = getUtility(IBinaryPackageBuildSet).getByQueueEntry(job)
        distroseries = build.distro_arch_series.distroseries
        if build.pocket == PackagePublishingPocket.SECURITY or (
            distroseries.status == SeriesStatus.OBSOLETE
            and not build.archive.permit_obsolete_series_uploads
        ):
            # We never build anything in the security pocket, or for obsolete
            # series without the flag set.
            logger.debug(
                "Build %s FAILEDTOBUILD, queue item %s REMOVED"
                % (build.id, job.id)
            )
            build.updateStatus(BuildStatus.FAILEDTOBUILD)
            job.destroySelf()
            return False

        publication = build.current_source_publication
        if publication is None:
            # The build should be superseded if it no longer has a
            # current publishing record.
            logger.debug(
                "Build %s SUPERSEDED, queue item %s REMOVED"
                % (build.id, job.id)
            )
            build.updateStatus(BuildStatus.SUPERSEDED)
            job.destroySelf()
            return False

        return True

    def _getAllowedArchitectures(self, archive, available_archs):
        """Filter out any restricted architectures not specifically allowed
        for an archive.

        :param available_archs: Architectures to consider
        :return: Sequence of `IDistroArch` instances.
        """
        # Return all distroarches with unrestricted processors or with
        # processors the archive is explicitly associated with.
        return [
            das
            for das in available_archs
            if (
                das.enabled
                and das.processor in archive.processors
                and (
                    das.processor.supports_virtualized
                    or not archive.require_virtualized
                )
            )
        ]

    def createForSource(
        self,
        sourcepackagerelease,
        archive,
        distroseries,
        pocket,
        architectures_available=None,
        logger=None,
    ):
        """See `ISourcePackagePublishingHistory`."""

        # Exclude any architectures which already have built or copied
        # binaries. A new build with the same archtag could never
        # succeed; its files would conflict during upload.
        relevant_builds = list(
            self.findBuiltOrPublishedBySourceAndArchive(
                sourcepackagerelease, archive
            ).values()
        )

        # Find any architectures that already have a build that exactly
        # matches, regardless of status. We can't create a second build
        # with the same (SPR, Archive, DAS).
        # XXX wgrant 2014-11-06: Should use a bulk query.
        for das in distroseries.architectures:
            build = self.getBySourceAndLocation(
                sourcepackagerelease, archive, das
            )
            if build is not None:
                relevant_builds.append(build)

        skip_archtags = {
            bpb.distro_arch_series.architecturetag for bpb in relevant_builds
        }
        # We need to assign the arch-indep role to a build unless an
        # arch-indep build has already succeeded, or another build in
        # this series already has it set.
        need_arch_indep = not any(bpb.arch_indep for bpb in relevant_builds)

        # Find the architectures for which the source could end up with
        # new binaries. Exclude architectures not allowed in this
        # archive and architectures that have already built. Order by
        # Processor.id so determine_architectures_to_build is
        # deterministic.
        # XXX wgrant 2014-11-06: The fact that production's
        # Processor 1 is i386, a good arch-indep candidate, is a
        # total coincidence and this isn't a hack. I promise.
        need_archs = sorted(
            (
                das
                for das in self._getAllowedArchitectures(
                    archive,
                    architectures_available
                    or distroseries.buildable_architectures,
                )
                if das.architecturetag not in skip_archtags
                and das.isSourceIncluded(
                    sourcepackagerelease.sourcepackagename
                )
            ),
            key=attrgetter("processor.id"),
        )
        nominated_arch_indep_tag = (
            distroseries.nominatedarchindep.architecturetag
            if distroseries.nominatedarchindep
            else None
        )

        def abi_tag(das):
            if das.underlying_architecturetag is not None:
                return das.underlying_architecturetag
            else:
                return das.architecturetag

        # Filter the valid archs against the hint list and work out
        # their arch-indepness.
        create_tag_map = determine_architectures_to_build(
            sourcepackagerelease.architecturehintlist,
            sourcepackagerelease.getUserDefinedField(
                "Build-Indep-Architecture"
            ),
            [abi_tag(das) for das in need_archs],
            nominated_arch_indep_tag,
            need_arch_indep,
        )

        # Create builds for the remaining architectures.
        new_builds = []
        for das in sorted(need_archs, key=attrgetter("architecturetag")):
            if abi_tag(das) not in create_tag_map:
                continue
            if (
                sourcepackagerelease.architecturehintlist == "all"
                and das.underlying_architecturetag is not None
            ):
                continue
            indep = (
                False
                if das.underlying_architecturetag is not None
                else create_tag_map[das.architecturetag]
            )
            build = self.new(
                source_package_release=sourcepackagerelease,
                distro_arch_series=das,
                archive=archive,
                pocket=pocket,
                arch_indep=indep,
            )
            new_builds.append(build)
            # Create the builds in suspended mode for disabled archives.
            build_queue = build.queueBuild(suspended=not archive.enabled)
            Store.of(build).flush()
            if logger is not None:
                logger.debug(
                    "Created %s [%d] in %s (%d)"
                    % (
                        build.title,
                        build.id,
                        build.archive.displayname,
                        build_queue.lastscore,
                    )
                )
        return new_builds


@implementer(IMacaroonIssuer)
class BinaryPackageBuildMacaroonIssuer(MacaroonIssuerBase):
    identifier = "binary-package-build"
    issuable_via_authserver = True

    @property
    def _primary_caveat_name(self):
        """See `MacaroonIssuerBase`."""
        # The "lp.principal" prefix indicates that this caveat constrains
        # the macaroon to access only resources that should be accessible
        # when acting on behalf of the named build, rather than to access
        # the named build directly.
        return "lp.principal.binary-package-build"

    def checkIssuingContext(self, context, **kwargs):
        """See `MacaroonIssuerBase`.

        For issuing, the context is an `IBinaryPackageBuild`.
        """
        if not removeSecurityProxy(context).archive.private:
            raise BadMacaroonContext(
                context, "Refusing to issue macaroon for public build."
            )
        return removeSecurityProxy(context).id

    def checkVerificationContext(self, context, **kwargs):
        """See `MacaroonIssuerBase`."""
        if not ILibraryFileAlias.providedBy(
            context
        ) and not IArchive.providedBy(context):
            raise BadMacaroonContext(context)
        return context

    def verifyPrimaryCaveat(
        self, verified, caveat_value, context, user=None, **kwargs
    ):
        """See `MacaroonIssuerBase`.

        For verification, the context is an `ILibraryFileAlias` or an
        `IArchive`.  We check that the file or archive is one of those
        required to build the `IBinaryPackageBuild` that is the context of
        the macaroon, and that the context build is currently building.
        """
        # Circular imports.
        from lp.soyuz.model.archive import Archive
        from lp.soyuz.model.archivedependency import ArchiveDependency

        # Binary package builds only support free-floating macaroons for
        # librarian or archive authentication, not ones bound to a user.
        if user:
            return False
        verified.user = NO_USER

        try:
            build_id = int(caveat_value)
        except ValueError:
            return False
        clauses = [
            BinaryPackageBuild.id == build_id,
            BinaryPackageBuild.status == BuildStatus.BUILDING,
        ]
        if ILibraryFileAlias.providedBy(context):
            clauses.extend(
                [
                    BinaryPackageBuild.source_package_release_id
                    == SourcePackageReleaseFile.sourcepackagerelease_id,
                    SourcePackageReleaseFile.libraryfile == context,
                ]
            )
        elif IArchive.providedBy(context):
            clauses.append(
                Or(
                    BinaryPackageBuild.archive == context,
                    BinaryPackageBuild.archive_id.is_in(
                        Select(
                            Archive.id,
                            where=And(
                                ArchiveDependency.archive == Archive.id,
                                ArchiveDependency.dependency == context,
                            ),
                        )
                    ),
                )
            )
        else:
            return False
        return (
            not IStore(BinaryPackageBuild)
            .find(BinaryPackageBuild, *clauses)
            .is_empty()
        )
