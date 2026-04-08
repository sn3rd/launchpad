# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "BinaryPackagePublishingHistory",
    "get_current_source_releases",
    "makePoolPath",
    "PublishingSet",
    "SourcePackagePublishingHistory",
]


import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from operator import attrgetter, itemgetter
from pathlib import Path
from typing import List

from storm.databases.postgres import JSON
from storm.expr import (
    And,
    Cast,
    Desc,
    Join,
    LeftJoin,
    Not,
    Or,
    Select,
    Sum,
    Union,
)
from storm.info import ClassAlias
from storm.properties import DateTime, Int, Unicode
from storm.references import Reference
from storm.store import Store
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import isinstance as zope_isinstance
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NotFoundError
from lp.buildmaster.enums import BuildStatus
from lp.registry.interfaces.person import validate_public_person
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.sourcepackage import SourcePackageType
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.channels import channel_list_to_string, channel_string_to_list
from lp.services.database import bulk
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import IsDistinctFrom
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.timeline.requesttimeline import temporary_request_timeline
from lp.services.webapp.errorlog import ErrorReportingUtility, ScriptRequest
from lp.services.worlddata.model.country import Country
from lp.soyuz.adapters.proxiedsourcefiles import ProxiedSourceLibraryFileAlias
from lp.soyuz.enums import (
    ArchiveRepositoryFormat,
    BinaryPackageFormat,
    PackagePublishingPriority,
    PackagePublishingStatus,
    PackageUploadStatus,
)
from lp.soyuz.interfaces.binarypackagebuild import (
    BuildSetStatus,
    IBinaryPackageBuildSet,
)
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.interfaces.distributionjob import (
    IDistroSeriesDifferenceJobSource,
)
from lp.soyuz.interfaces.publishing import (
    DeletionError,
    IBinaryPackagePublishingHistory,
    IgnorableArtifactoryPoolFileOverwriteError,
    IPublishingSet,
    ISourcePackagePublishingHistory,
    OverrideError,
    PoolFileOverwriteError,
    SourceUploadBuildInfo,
    SourceUploadInfo,
    active_publishing_status,
    name_priority_map,
)
from lp.soyuz.interfaces.section import ISectionSet
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.binarypackagename import BinaryPackageName
from lp.soyuz.model.binarypackagerelease import (
    BinaryPackageRelease,
    BinaryPackageReleaseDownloadCount,
)
from lp.soyuz.model.distroarchseries import DistroArchSeries
from lp.soyuz.model.files import BinaryPackageFile, SourcePackageReleaseFile
from lp.soyuz.model.section import Section
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease


def makePoolPath(source_name: str, component_name: str) -> str:
    # XXX cprov 2006-08-18: move it away, perhaps archivepublisher/pool.py
    """Return the pool path for a given source name and component name."""
    from lp.archivepublisher.diskpool import poolify

    return str(Path("pool") / poolify(source_name, component_name))


def get_component(archive, distroseries, component):
    """Override the component to fit in the archive, if possible.

    If the archive has a default component, and it forbids use of the
    requested component in the requested series, use the default.

    If there is no default, just return the given component.
    """
    if component is None:
        return None
    permitted_components = archive.getComponentsForSeries(distroseries)
    if (
        component not in permitted_components
        and archive.default_component is not None
    ):
        return archive.default_component
    return component


def proxied_urls(files, parent):
    """Run the files passed through `ProxiedLibraryFileAlias`."""
    return [ProxiedLibraryFileAlias(file, parent).http_url for file in files]


def proxied_source_urls(files, parent):
    """Return the files passed through `ProxiedSourceLibraryFileAlias`."""
    return [
        ProxiedSourceLibraryFileAlias(file, parent).http_url for file in files
    ]


class ArchivePublisherBase:
    """Base class for `IArchivePublisher`."""

    def setPublished(self):
        """see IArchiveSafePublisher."""
        # XXX cprov 2006-06-14:
        # Implement sanity checks before set it as published
        if (
            self.status in active_publishing_status
            and self.datepublished is None
        ):
            # update the DB publishing record status if they
            # are pending, don't do anything for the ones
            # already published (usually when we use -C
            # publish-distro.py option)
            if self.status == PackagePublishingStatus.PENDING:
                self.status = PackagePublishingStatus.PUBLISHED
            self.datepublished = UTC_NOW

    def publish(self, diskpool, log):
        """See `IPublishing`"""
        try:
            for pub_file in self.files:
                pool_name = self.pool_name
                pool_version = self.pool_version
                component = (
                    None if self.component is None else self.component.name
                )
                path = diskpool.pathFor(
                    component, pool_name, pool_version, pub_file
                )

                action = diskpool.addFile(
                    component, pool_name, pool_version, pub_file
                )
                if action == diskpool.results.FILE_ADDED:
                    log.debug("Added %s from library" % path)
                elif action == diskpool.results.SYMLINK_ADDED:
                    log.debug("%s created as a symlink." % path)
                elif action == diskpool.results.NONE:
                    log.debug(
                        "%s is already in pool with the same content." % path
                    )
        except PoolFileOverwriteError as e:
            message = "PoolFileOverwriteError: %s, skipping." % e
            properties = [("error-explanation", message)]
            request = ScriptRequest(properties)
            error_utility = ErrorReportingUtility()
            with temporary_request_timeline(request):
                error_utility.raising(sys.exc_info(), request)
            log.error("%s (%s)" % (message, request.oopsid))
        # XXX lgp171188 2025-03-24
        # There are some known PoolFileOverwriteError exceptions
        # in the Artifactory publishing process for non-deb (for example,
        # Python, Conda, source) packages on multiple architectures
        # that we need to ignore to avoid flooding the OOPS system.
        # The following `except` clause is needed for that.
        except IgnorableArtifactoryPoolFileOverwriteError:
            pass
        else:
            self.setPublished()

    def setSuperseded(self):
        """Set to SUPERSEDED status."""
        self.status = PackagePublishingStatus.SUPERSEDED
        self.datesuperseded = UTC_NOW

    def setDeleted(self, removed_by, removal_comment=None):
        """Set to DELETED status."""
        getUtility(IPublishingSet).setMultipleDeleted(
            self.__class__, [self.id], removed_by, removal_comment
        )

    def requestObsolescence(self):
        """See `IArchivePublisher`."""
        # The tactic here is to bypass the domination step when publishing,
        # and let it go straight to death row processing.  This is because
        # domination ignores stable distroseries, and that is exactly what
        # we're most likely to be obsoleting.
        #
        # Setting scheduleddeletiondate achieves that aim.
        self.status = PackagePublishingStatus.OBSOLETE
        self.scheduleddeletiondate = UTC_NOW
        return self

    @property
    def age(self):
        """See `IArchivePublisher`."""
        return datetime.now(timezone.utc) - self.datecreated

    @property
    def component_name(self):
        """See `IPublishingView`."""
        return self.component.name if self.component is not None else None

    @property
    def section_name(self):
        """See `IPublishingView`."""
        return self.section.name if self.section is not None else None


@implementer(ISourcePackagePublishingHistory)
class SourcePackagePublishingHistory(StormBase, ArchivePublisherBase):
    """A source package release publishing record."""

    __storm_table__ = "SourcePackagePublishingHistory"

    id = Int(primary=True)
    sourcepackagename_id = Int(name="sourcepackagename")
    sourcepackagename = Reference(sourcepackagename_id, "SourcePackageName.id")
    sourcepackagerelease_id = Int(name="sourcepackagerelease")
    sourcepackagerelease = Reference(
        sourcepackagerelease_id, "SourcePackageRelease.id"
    )
    _format = DBEnum(
        name="format",
        enum=SourcePackageType,
        default=SourcePackageType.DPKG,
        allow_none=True,
    )
    distroseries_id = Int(name="distroseries")
    distroseries = Reference(distroseries_id, "DistroSeries.id")
    # DB constraint: non-nullable for SourcePackageType.DPKG.
    component_id = Int(name="component", allow_none=True)
    component = Reference(component_id, "Component.id")
    # DB constraint: non-nullable for SourcePackageType.DPKG.
    section_id = Int(name="section", allow_none=True)
    section = Reference(section_id, "Section.id")
    status = DBEnum(enum=PackagePublishingStatus)
    scheduleddeletiondate = DateTime(default=None, tzinfo=timezone.utc)
    datepublished = DateTime(default=None, tzinfo=timezone.utc)
    datecreated = DateTime(default=UTC_NOW, tzinfo=timezone.utc)
    datesuperseded = DateTime(default=None, tzinfo=timezone.utc)
    supersededby_id = Int(name="supersededby", default=None)
    supersededby = Reference(supersededby_id, "SourcePackageRelease.id")
    datemadepending = DateTime(default=None, tzinfo=timezone.utc)
    dateremoved = DateTime(default=None, tzinfo=timezone.utc)
    pocket = DBEnum(
        name="pocket",
        enum=PackagePublishingPocket,
        default=PackagePublishingPocket.RELEASE,
        allow_none=False,
    )
    _channel = JSON(name="channel", allow_none=True)
    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")
    copied_from_archive_id = Int(name="copied_from_archive", allow_none=True)
    copied_from_archive = Reference(copied_from_archive_id, "Archive.id")
    removed_by_id = Int(
        name="removed_by", validator=validate_public_person, default=None
    )
    removed_by = Reference(removed_by_id, "Person.id")
    removal_comment = Unicode(name="removal_comment", default=None)
    ancestor_id = Int(name="ancestor", default=None)
    ancestor = Reference(ancestor_id, "SourcePackagePublishingHistory.id")
    creator_id = Int(
        name="creator",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    creator = Reference(creator_id, "Person.id")
    sponsor_id = Int(
        name="sponsor",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    sponsor = Reference(sponsor_id, "Person.id")
    packageupload_id = Int(name="packageupload", allow_none=True, default=None)
    packageupload = Reference(packageupload_id, "PackageUpload.id")

    def __init__(
        self,
        sourcepackagename,
        sourcepackagerelease,
        format,
        distroseries,
        pocket,
        status,
        archive,
        component=None,
        section=None,
        scheduleddeletiondate=None,
        datepublished=None,
        datecreated=None,
        dateremoved=None,
        channel=None,
        copied_from_archive=None,
        ancestor=None,
        creator=None,
        sponsor=None,
        packageupload=None,
    ):
        super().__init__()
        self.sourcepackagename = sourcepackagename
        self.sourcepackagerelease = sourcepackagerelease
        self._format = format
        self.distroseries = distroseries
        self.pocket = pocket
        self.status = status
        self.archive = archive
        self.component = component
        self.section = section
        self.scheduleddeletiondate = scheduleddeletiondate
        self.datepublished = datepublished
        self.datecreated = datecreated
        self.dateremoved = dateremoved
        self._channel = channel
        self.copied_from_archive = copied_from_archive
        self.ancestor = ancestor
        self.creator = creator
        self.sponsor = sponsor
        self.packageupload = packageupload

    @property
    def format(self):
        # XXX cjwatson 2022-04-04: Remove once this column has been backfilled.
        return (
            self._format
            if self._format is not None
            else self.sourcepackagerelease.format
        )

    @property
    def package_creator(self):
        """See `ISourcePackagePublishingHistory`."""
        return self.sourcepackagerelease.creator

    @property
    def package_maintainer(self):
        """See `ISourcePackagePublishingHistory`."""
        return self.sourcepackagerelease.maintainer

    @property
    def package_signer(self):
        """See `ISourcePackagePublishingHistory`."""
        return self.sourcepackagerelease.signing_key_owner

    @cachedproperty
    def newer_distroseries_version(self):
        """See `ISourcePackagePublishingHistory`."""
        self.distroseries.setNewerDistroSeriesVersions([self])
        return get_property_cache(self).newer_distroseries_version

    @property
    def channel(self):
        """See `ISourcePackagePublishingHistory`."""
        if self._channel is None:
            return None
        return channel_list_to_string(*self._channel)

    def getPublishedBinaries(self, active_binaries_only=True):
        """See `ISourcePackagePublishingHistory`."""
        publishing_set = getUtility(IPublishingSet)
        result_set = publishing_set.getBinaryPublicationsForSources(
            self, active_binaries_only=active_binaries_only
        )
        return DecoratedResultSet(result_set, result_decorator=itemgetter(1))

    def getBuiltBinaries(self, want_files=False):
        """See `ISourcePackagePublishingHistory`."""
        # Circular import.
        from lp.code.model.cibuild import CIBuild

        clauses = [
            BinaryPackagePublishingHistory.binarypackagerelease_id
            == BinaryPackageRelease.id,
            BinaryPackagePublishingHistory.distroarchseries_id
            == DistroArchSeries.id,
            BinaryPackagePublishingHistory.archive == self.archive_id,
            BinaryPackagePublishingHistory.pocket == self.pocket,
            DistroArchSeries.distroseries == self.distroseries_id,
        ]
        if self.sourcepackagerelease.ci_build_id is not None:
            # Source and binary publications may come from different CI
            # builds, so just match the git commit.
            clauses.extend(
                [
                    BinaryPackageRelease.ci_build == CIBuild.id,
                    CIBuild.git_repository_id
                    == self.sourcepackagerelease.ci_build.git_repository_id,
                    CIBuild.commit_sha1
                    == self.sourcepackagerelease.ci_build.commit_sha1,
                ]
            )
        else:
            clauses.extend(
                [
                    BinaryPackageRelease.build == BinaryPackageBuild.id,
                    BinaryPackageBuild.source_package_release
                    == self.sourcepackagerelease_id,
                ]
            )
        binary_publications = list(
            Store.of(self)
            .find(BinaryPackagePublishingHistory, *clauses)
            .order_by(Desc(BinaryPackagePublishingHistory.id))
        )

        # Preload attached BinaryPackageReleases.
        bpr_ids = {pub.binarypackagerelease_id for pub in binary_publications}
        list(
            Store.of(self).find(
                BinaryPackageRelease, BinaryPackageRelease.id.is_in(bpr_ids)
            )
        )

        if want_files:
            # Preload BinaryPackageFiles.
            bpfs = list(
                Store.of(self).find(
                    BinaryPackageFile,
                    BinaryPackageFile.binarypackagerelease_id.is_in(bpr_ids),
                )
            )
            bpfs_by_bpr = defaultdict(list)
            for bpf in bpfs:
                bpfs_by_bpr[bpf.binarypackagerelease].append(bpf)
            for bpr in bpfs_by_bpr:
                get_property_cache(bpr).files = bpfs_by_bpr[bpr]

            # Preload LibraryFileAliases.
            lfa_ids = {bpf.libraryfile_id for bpf in bpfs}
            list(
                Store.of(self).find(
                    LibraryFileAlias, LibraryFileAlias.id.is_in(lfa_ids)
                )
            )

        unique_binary_publications = []
        for pub in binary_publications:
            if pub.binarypackagerelease.id in bpr_ids:
                unique_binary_publications.append(pub)
                bpr_ids.remove(pub.binarypackagerelease.id)
                if len(bpr_ids) == 0:
                    break

        return unique_binary_publications

    @staticmethod
    def _convertBuilds(builds_for_sources):
        """Convert from IPublishingSet getBuilds to SPPH getBuilds."""
        return [build[1] for build in builds_for_sources]

    def getBuilds(self):
        """See `ISourcePackagePublishingHistory`."""
        publishing_set = getUtility(IPublishingSet)
        result_set = publishing_set.getBuildsForSources([self])
        return SourcePackagePublishingHistory._convertBuilds(result_set)

    def getFileByName(self, name):
        """See `ISourcePackagePublishingHistory`."""
        changelog = self.sourcepackagerelease.changelog
        if changelog is not None and name == changelog.filename:
            return changelog
        raise NotFoundError(name)

    def changesFileUrl(self):
        """See `ISourcePackagePublishingHistory`."""
        # We use getChangesFileLFA() as opposed to getChangesFilesForSources()
        # because the latter is more geared towards the web UI and taxes the
        # db much more in terms of the join width and the pre-joined data.
        #
        # This method is accessed overwhelmingly via the LP API and calling
        # getChangesFileLFA() which is much lighter on the db has the
        # potential of performing significantly better.
        changes_lfa = getUtility(IPublishingSet).getChangesFileLFA(
            self.sourcepackagerelease
        )

        if changes_lfa is None:
            # This should not happen in practice, but the code should
            # not blow up because of bad data.
            return None

        # Return a webapp-proxied LibraryFileAlias so that restricted
        # librarian files are accessible.  Non-restricted files will get
        # a 302 so that webapp threads are not tied up.
        the_url = proxied_urls((changes_lfa,), self.archive)[0]
        return the_url

    def changelogUrl(self):
        """See `ISourcePackagePublishingHistory`."""
        lfa = self.sourcepackagerelease.changelog
        if lfa is not None:
            return proxied_urls((lfa,), self)[0]
        return None

    def createMissingBuilds(self, architectures_available=None, logger=None):
        """See `ISourcePackagePublishingHistory`."""
        return getUtility(IBinaryPackageBuildSet).createForSource(
            self.sourcepackagerelease,
            self.archive,
            self.distroseries,
            self.pocket,
            architectures_available,
            logger,
        )

    @property
    def files(self):
        """See `IPublishing`."""
        # XXX ilasc 2022-11-14 / cjwatson 2023-06-15: Source packages in
        # Conda repositories should never have any files attached; they
        # should be pure skeletons used to fit into the rest of Launchpad's
        # data model (see commit 397fbebfa3).  However, there is one case
        # where a file somehow got attached to a source package in a Conda
        # repository (see
        # https://bugs.launchpad.net/launchpad/+bug/2023315); this file
        # doesn't have enough metadata for the publisher to compute its URL
        # path, and so it crashes.  Until we figure out why this happened
        # and ensure that it can't happen again, forcibly return the empty
        # list for such source packages.
        if self.archive.repository_format == ArchiveRepositoryFormat.CONDA:
            return []

        files = self.sourcepackagerelease.files
        lfas = bulk.load_related(LibraryFileAlias, files, ["libraryfile_id"])
        bulk.load_related(LibraryFileContent, lfas, ["content_id"])
        return files

    def getSourceAndBinaryLibraryFiles(self):
        """See `IPublishing`."""
        publishing_set = getUtility(IPublishingSet)
        result_set = publishing_set.getFilesForSources(self)
        libraryfiles = [file for source, file, content in result_set]

        # XXX cprov 20080710: UNIONs cannot be ordered appropriately.
        # See IPublishing.getFilesForSources().
        return sorted(libraryfiles, key=attrgetter("filename"))

    @property
    def meta_sourcepackage(self):
        """see `ISourcePackagePublishingHistory`."""
        return self.distroseries.getSourcePackage(
            self.sourcepackagerelease.sourcepackagename
        )

    @property
    def meta_distributionsourcepackagerelease(self):
        """see `ISourcePackagePublishingHistory`."""
        return self.distroseries.distribution.getSourcePackageRelease(
            self.sourcepackagerelease
        )

    # XXX: StevenK 2011-09-13 bug=848563: This can die when
    # self.sourcepackagename is populated.
    @property
    def source_package_name(self):
        """See `ISourcePackagePublishingHistory`"""
        return self.sourcepackagerelease.name

    @property
    def source_package_version(self):
        """See `ISourcePackagePublishingHistory`"""
        return self.sourcepackagerelease.version

    @property
    def pool_name(self):
        """See `IPublishingView`."""
        return self.source_package_name

    @property
    def pool_version(self):
        """See `IPublishingView`."""
        return self.source_package_version

    @property
    def displayname(self):
        """See `IPublishing`."""
        release = self.sourcepackagerelease
        name = release.sourcepackagename.name
        return "%s %s in %s" % (name, release.version, self.distroseries.name)

    def supersede(self, dominant=None, logger=None):
        """See `ISourcePackagePublishingHistory`."""
        assert self.status in active_publishing_status, (
            "Should not dominate unpublished source %s"
            % self.sourcepackagerelease.title
        )

        self.setSuperseded()

        if dominant is not None:
            if logger is not None:
                logger.debug(
                    "%s/%s has been judged as superseded by %s/%s"
                    % (
                        self.sourcepackagerelease.sourcepackagename.name,
                        self.sourcepackagerelease.version,
                        dominant.sourcepackagerelease.sourcepackagename.name,
                        dominant.sourcepackagerelease.version,
                    )
                )

            self.supersededby = dominant.sourcepackagerelease

    def changeOverride(
        self, new_component=None, new_section=None, creator=None
    ):
        """See `ISourcePackagePublishingHistory`."""
        # Check we have been asked to do something
        if new_component is None and new_section is None:
            raise AssertionError(
                "changeOverride must be passed either a"
                " new component or new section"
            )

        # Check there is a change to make
        if new_component is None:
            new_component = self.component
        elif isinstance(new_component, str):
            new_component = getUtility(IComponentSet)[new_component]
        if new_section is None:
            new_section = self.section
        elif isinstance(new_section, str):
            new_section = getUtility(ISectionSet)[new_section]

        if new_component == self.component and new_section == self.section:
            return

        if new_component != self.component:
            # See if the archive has changed by virtue of the component
            # changing:
            distribution = self.distroseries.distribution
            new_archive = distribution.getArchiveByComponent(
                new_component.name
            )
            if new_archive != None and new_archive != self.archive:
                raise OverrideError(
                    "Overriding component to '%s' failed because it would "
                    "require a new archive." % new_component.name
                )

        # Refuse to create new publication records that will never be
        # published.
        if not self.archive.canModifySuite(self.distroseries, self.pocket):
            raise OverrideError(
                "Cannot change overrides in suite '%s'"
                % self.distroseries.getSuite(self.pocket)
            )

        return getUtility(IPublishingSet).newSourcePublication(
            distroseries=self.distroseries,
            sourcepackagerelease=self.sourcepackagerelease,
            pocket=self.pocket,
            component=new_component,
            section=new_section,
            creator=creator,
            archive=self.archive,
            channel=self.channel,
        )

    def copyTo(
        self,
        distroseries,
        pocket,
        archive,
        override=None,
        create_dsd_job=True,
        creator=None,
        sponsor=None,
        packageupload=None,
    ):
        """See `ISourcePackagePublishingHistory`."""
        component = self.component
        section = self.section
        if override is not None:
            if override.component is not None:
                component = override.component
            if override.section is not None:
                section = override.section

        return getUtility(IPublishingSet).newSourcePublication(
            archive,
            self.sourcepackagerelease,
            distroseries,
            pocket,
            component=component,
            section=section,
            ancestor=self,
            create_dsd_job=create_dsd_job,
            creator=creator,
            sponsor=sponsor,
            copied_from_archive=self.archive,
            packageupload=packageupload,
            channel=self.channel,
        )

    def getStatusSummaryForBuilds(self):
        """See `ISourcePackagePublishingHistory`."""
        return getUtility(
            IPublishingSet
        ).getBuildStatusSummaryForSourcePublication(self)

    def sourceFileUrls(self, include_meta=False):
        """See `ISourcePackagePublishingHistory`."""
        sources = Store.of(self).find(
            (LibraryFileAlias, LibraryFileContent),
            LibraryFileContent.id == LibraryFileAlias.content_id,
            LibraryFileAlias.id == SourcePackageReleaseFile.libraryfile_id,
            SourcePackageReleaseFile.sourcepackagerelease
            == self.sourcepackagerelease_id,
        )
        source_urls = proxied_source_urls(
            [source for source, _ in sources], self
        )
        if include_meta:
            meta = [
                (content.filesize, content.sha256) for _, content in sources
            ]
            return [
                dict(url=url, size=size, sha256=sha256)
                for url, (size, sha256) in zip(source_urls, meta)
            ]
        return source_urls

    def binaryFileUrls(self):
        """See `ISourcePackagePublishingHistory`."""
        publishing_set = getUtility(IPublishingSet)
        binaries = publishing_set.getBinaryFilesForSources(self).config(
            distinct=True
        )
        binary_urls = proxied_urls(
            [binary for _source, binary, _content in binaries], self.archive
        )
        return binary_urls

    def packageDiffUrl(self, to_version):
        """See `ISourcePackagePublishingHistory`."""
        # There will be only very few diffs for each package so
        # iterating is fine here, since the package_diffs property is a
        # multiple join and returns all the diffs quite quickly.
        for diff in self.sourcepackagerelease.package_diffs:
            if diff.to_source.version == to_version:
                return ProxiedLibraryFileAlias(
                    diff.diff_content, self.archive
                ).http_url
        return None

    def requestDeletion(
        self, removed_by, removal_comment=None, immutable_check=True
    ):
        """See `IPublishing`."""
        # Fail if operation would modify an immutable suite (eg. the
        # RELEASE pocket of a CURRENT series).
        if immutable_check and not self.archive.canModifySuite(
            self.distroseries, self.pocket
        ):
            raise DeletionError(
                "Cannot delete publications from suite '%s'"
                % self.distroseries.getSuite(self.pocket)
            )

        self.setDeleted(removed_by, removal_comment)
        if self.archive.is_main:
            dsd_job_source = getUtility(IDistroSeriesDifferenceJobSource)
            dsd_job_source.createForPackagePublication(
                self.distroseries,
                self.sourcepackagerelease.sourcepackagename,
                self.pocket,
            )

    def api_requestDeletion(self, removed_by, removal_comment=None):
        """See `IPublishingEdit`."""
        # Special deletion method for the api that makes sure binaries
        # get deleted too.
        getUtility(IPublishingSet).requestDeletion(
            [self], removed_by, removal_comment
        )

    def hasRestrictedFiles(self):
        """See ISourcePackagePublishingHistory."""
        for source_file in self.sourcepackagerelease.files:
            if source_file.libraryfile.restricted:
                return True

        for binary in self.getBuiltBinaries():
            for binary_file in binary.binarypackagerelease.files:
                if binary_file.libraryfile.restricted:
                    return True

        return False


@implementer(IBinaryPackagePublishingHistory)
class BinaryPackagePublishingHistory(StormBase, ArchivePublisherBase):
    """A binary package publishing record."""

    __storm_table__ = "BinaryPackagePublishingHistory"

    id = Int(primary=True)
    binarypackagename_id = Int(name="binarypackagename")
    binarypackagename = Reference(binarypackagename_id, "BinaryPackageName.id")
    binarypackagerelease_id = Int(name="binarypackagerelease")
    binarypackagerelease = Reference(
        binarypackagerelease_id, "BinaryPackageRelease.id"
    )
    _binarypackageformat = DBEnum(
        name="binarypackageformat", enum=BinaryPackageFormat, allow_none=True
    )
    distroarchseries_id = Int(name="distroarchseries")
    distroarchseries = Reference(distroarchseries_id, "DistroArchSeries.id")
    # DB constraint: non-nullable for BinaryPackageFormat.{DEB,UDEB,DDEB}.
    component_id = Int(name="component", allow_none=True)
    component = Reference(component_id, "Component.id")
    # DB constraint: non-nullable for BinaryPackageFormat.{DEB,UDEB,DDEB}.
    section_id = Int(name="section", allow_none=True)
    section = Reference(section_id, "Section.id")
    # DB constraint: non-nullable for BinaryPackageFormat.{DEB,UDEB,DDEB}.
    priority = DBEnum(
        name="priority", enum=PackagePublishingPriority, allow_none=True
    )
    status = DBEnum(name="status", enum=PackagePublishingStatus)
    phased_update_percentage = Int(
        name="phased_update_percentage", allow_none=True, default=None
    )
    scheduleddeletiondate = DateTime(default=None, tzinfo=timezone.utc)
    creator_id = Int(
        name="creator",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    creator = Reference(creator_id, "Person.id")
    datepublished = DateTime(default=None, tzinfo=timezone.utc)
    datecreated = DateTime(default=UTC_NOW, tzinfo=timezone.utc)
    datesuperseded = DateTime(default=None, tzinfo=timezone.utc)
    supersededby_id = Int(name="supersededby", default=None)
    supersededby = Reference(supersededby_id, "BinaryPackageBuild.id")
    datemadepending = DateTime(default=None, tzinfo=timezone.utc)
    dateremoved = DateTime(default=None, tzinfo=timezone.utc)
    pocket = DBEnum(name="pocket", enum=PackagePublishingPocket)
    _channel = JSON(name="channel", allow_none=True)
    archive_id = Int(name="archive", allow_none=False)
    archive = Reference(archive_id, "Archive.id")
    copied_from_archive_id = Int(name="copied_from_archive", allow_none=True)
    copied_from_archive = Reference(copied_from_archive_id, "Archive.id")
    removed_by_id = Int(
        name="removed_by", validator=validate_public_person, default=None
    )
    removed_by = Reference(removed_by_id, "Person.id")
    removal_comment = Unicode(name="removal_comment", default=None)
    sourcepackagename_id = Int(name="sourcepackagename", allow_none=True)
    sourcepackagename = Reference(sourcepackagename_id, "SourcePackageName.id")

    def __init__(
        self,
        binarypackagename,
        binarypackagerelease,
        binarypackageformat,
        distroarchseries,
        pocket,
        status,
        archive,
        sourcepackagename,
        component=None,
        section=None,
        priority=None,
        phased_update_percentage=None,
        scheduleddeletiondate=None,
        creator=None,
        datepublished=None,
        datecreated=None,
        dateremoved=None,
        channel=None,
        copied_from_archive=None,
    ):
        super().__init__()
        self.binarypackagename = binarypackagename
        self.binarypackagerelease = binarypackagerelease
        self._binarypackageformat = binarypackageformat
        self.distroarchseries = distroarchseries
        self.pocket = pocket
        self.status = status
        self.archive = archive
        self.sourcepackagename = sourcepackagename
        self.component = component
        self.section = section
        self.priority = priority
        self.phased_update_percentage = phased_update_percentage
        self.scheduleddeletiondate = scheduleddeletiondate
        self.creator = creator
        self.datepublished = datepublished
        self.datecreated = datecreated
        self.dateremoved = dateremoved
        self._channel = channel
        self.copied_from_archive = copied_from_archive

    @property
    def binarypackageformat(self):
        # XXX cjwatson 2022-04-04: Remove once this column has been backfilled.
        return (
            self._binarypackageformat
            if self._binarypackageformat is not None
            else self.binarypackagerelease.binpackageformat
        )

    @property
    def distroarchseriesbinarypackagerelease(self):
        """See `IBinaryPackagePublishingHistory`."""
        # Import here to avoid circular import.
        from lp.soyuz.model.distroarchseriesbinarypackagerelease import (
            DistroArchSeriesBinaryPackageRelease,
        )

        return DistroArchSeriesBinaryPackageRelease(
            self.distroarchseries, self.binarypackagerelease
        )

    @property
    def files(self):
        """See `IPublishing`."""
        files = self.binarypackagerelease.files
        lfas = bulk.load_related(LibraryFileAlias, files, ["libraryfile_id"])
        bulk.load_related(LibraryFileContent, lfas, ["content_id"])
        return files

    @property
    def distroseries(self):
        """See `IBinaryPackagePublishingHistory`"""
        return self.distroarchseries.distroseries

    # XXX: StevenK 2011-09-13 bug=848563: This can die when
    # self.binarypackagename is populated.
    @property
    def binary_package_name(self):
        """See `IBinaryPackagePublishingHistory`"""
        return self.binarypackagerelease.name

    @property
    def binary_package_version(self):
        """See `IBinaryPackagePublishingHistory`"""
        return self.binarypackagerelease.version

    @property
    def build(self):
        return self.binarypackagerelease.build

    @property
    def source_package_name(self):
        """See `ISourcePackagePublishingHistory`"""
        # XXX cjwatson 2022-09-12: Simplify this once self.sourcepackagename
        # is populated.
        return self.binarypackagerelease.sourcepackagename

    @property
    def source_package_version(self):
        """See `IBinaryPackagePublishingHistory`"""
        return self.binarypackagerelease.sourcepackageversion

    @property
    def pool_name(self):
        """See `IPublishingView`."""
        # XXX cjwatson 2022-06-08: If the publishing record came from
        # uploading a CI build, then it may be an isolated binary package
        # without a corresponding source package, in which case we won't
        # have a source package name.  For now, use the binary package name
        # instead in that case so that the pool has a name to use for
        # publishing files.  This is inelegant to say the least, but it gets
        # the job done for now.
        pool_name = self.source_package_name
        if self.build is None and pool_name is None:
            pool_name = self.binary_package_name
        return pool_name

    @property
    def pool_version(self):
        """See `IPublishingView`."""
        # XXX cjwatson 2022-06-08: If the publishing record came from
        # uploading a CI build, then it may be an isolated binary package
        # without a corresponding source package, in which case we won't
        # have a source package version.  For now, use the binary package
        # version instead in that case so that the pool has a version to use
        # for publishing files.  This is inelegant to say the least, but it
        # gets the job done for now.
        pool_version = self.source_package_version
        if self.build is None and pool_version is None:
            pool_version = self.binary_package_version
        return pool_version

    @property
    def architecture_specific(self):
        """See `IBinaryPackagePublishingHistory`"""
        return self.binarypackagerelease.architecturespecific

    @property
    def is_debug(self):
        """See `IBinaryPackagePublishingHistory`."""
        return (
            self.binarypackagerelease.binpackageformat
            == BinaryPackageFormat.DDEB
        )

    @property
    def priority_name(self):
        """See `IBinaryPackagePublishingHistory`"""
        return self.priority.name if self.priority is not None else None

    @property
    def displayname(self):
        """See `IPublishing`."""
        release = self.binarypackagerelease
        name = release.binarypackagename.name
        distroseries = self.distroarchseries.distroseries
        return "%s %s in %s %s" % (
            name,
            release.version,
            distroseries.name,
            self.distroarchseries.architecturetag,
        )

    @property
    def channel(self):
        """See `ISourcePackagePublishingHistory`."""
        if self._channel is None:
            return None
        return channel_list_to_string(*self._channel)

    def getDownloadCount(self):
        """See `IBinaryPackagePublishingHistory`."""
        return self.archive.getPackageDownloadTotal(self.binarypackagerelease)

    def publish(self, diskpool, log):
        """See `IPublishing`."""
        if self.is_debug and not self.archive.publish_debug_symbols:
            self.setPublished()
        else:
            super().publish(diskpool, log)

    def getOtherPublications(self):
        """See `IBinaryPackagePublishingHistory`."""
        available_architectures = [
            das.id for das in self.distroarchseries.distroseries.architectures
        ]
        return IPrimaryStore(BinaryPackagePublishingHistory).find(
            BinaryPackagePublishingHistory,
            BinaryPackagePublishingHistory.status.is_in(
                active_publishing_status
            ),
            BinaryPackagePublishingHistory.distroarchseries_id.is_in(
                available_architectures
            ),
            binarypackagerelease=self.binarypackagerelease,
            archive=self.archive,
            pocket=self.pocket,
            component=self.component,
            section=self.section,
            priority=self.priority,
            phased_update_percentage=self.phased_update_percentage,
        )

    def supersede(self, dominant=None, logger=None):
        """See `IBinaryPackagePublishingHistory`."""
        # At this point only PUBLISHED (ancient versions) or PENDING (
        # multiple overrides/copies) publications should be given. We
        # tolerate SUPERSEDED architecture-independent binaries, because
        # they are dominated automatically once the first publication is
        # processed.
        if self.status not in active_publishing_status:
            assert not self.binarypackagerelease.architecturespecific, (
                "Should not dominate unpublished architecture specific "
                "binary %s (%s)"
                % (
                    self.binarypackagerelease.title,
                    self.distroarchseries.architecturetag,
                )
            )
            return

        self.setSuperseded()

        if dominant is not None:
            # DDEBs cannot themselves be dominant; they are always dominated
            # by their corresponding DEB. Any attempt to dominate with a
            # dominant DDEB is a bug.
            assert (
                not dominant.is_debug
            ), "Should not dominate with %s (%s); DDEBs cannot dominate" % (
                dominant.binarypackagerelease.title,
                dominant.distroarchseries.architecturetag,
            )

            dominant_build = dominant.binarypackagerelease.build
            # XXX cjwatson 2022-05-01: We can't currently dominate with CI
            # builds, since supersededby is a reference to a BPB.  Just
            # leave supersededby unset in that case for now, which isn't
            # ideal but will work well enough.
            if dominant_build is not None:
                distroarchseries = dominant_build.distro_arch_series
                if logger is not None:
                    logger.debug(
                        "The %s build of %s has been judged as superseded by "
                        "the build of %s.  Arch-specific == %s"
                        % (
                            distroarchseries.architecturetag,
                            self.binarypackagerelease.title,
                            dominant_build.source_package_release.title,
                            self.binarypackagerelease.architecturespecific,
                        )
                    )
                # Binary package releases are superseded by the new build,
                # not the new binary package release. This is because
                # there may not *be* a new matching binary package -
                # source packages can change the binaries they build
                # between releases.
                self.supersededby = dominant_build

        debug = getUtility(IPublishingSet).findCorrespondingDDEBPublications(
            [self]
        )
        for dominated in debug:
            dominated.supersede(dominant, logger)

    def changeOverride(
        self,
        new_component=None,
        new_section=None,
        new_priority=None,
        new_phased_update_percentage=None,
        creator=None,
    ):
        """See `IBinaryPackagePublishingHistory`."""

        # Check we have been asked to do something
        if (
            new_component is None
            and new_section is None
            and new_priority is None
            and new_phased_update_percentage is None
        ):
            raise AssertionError(
                "changeOverride must be passed a new "
                "component, section, priority and/or "
                "phased_update_percentage."
            )

        if self.is_debug:
            raise OverrideError(
                "Cannot override ddeb publications directly; override "
                "the corresponding deb instead."
            )

        # Check there is a change to make
        if new_component is None:
            new_component = self.component
        elif isinstance(new_component, str):
            new_component = getUtility(IComponentSet)[new_component]
        if new_section is None:
            new_section = self.section
        elif isinstance(new_section, str):
            new_section = getUtility(ISectionSet)[new_section]
        if new_priority is None:
            new_priority = self.priority
        elif isinstance(new_priority, str):
            new_priority = name_priority_map[new_priority]
        if new_phased_update_percentage is None:
            new_phased_update_percentage = self.phased_update_percentage
        elif (
            new_phased_update_percentage < 0
            or new_phased_update_percentage > 100
        ):
            raise ValueError(
                "new_phased_update_percentage must be between 0 and 100 "
                "(inclusive)."
            )
        elif new_phased_update_percentage == 100:
            new_phased_update_percentage = None

        if (
            new_component == self.component
            and new_section == self.section
            and new_priority == self.priority
            and new_phased_update_percentage == self.phased_update_percentage
        ):
            return

        bpr = self.binarypackagerelease

        if new_component != self.component:
            # See if the archive has changed by virtue of the component
            # changing:
            distribution = self.distroarchseries.distroseries.distribution
            new_archive = distribution.getArchiveByComponent(
                new_component.name
            )
            if new_archive is not None and new_archive != self.archive:
                raise OverrideError(
                    "Overriding component to '%s' failed because it would "
                    "require a new archive." % new_component.name
                )

        # Refuse to create new publication records that will never be
        # published.
        if not self.archive.canModifySuite(self.distroseries, self.pocket):
            raise OverrideError(
                "Cannot change overrides in suite '%s'"
                % self.distroseries.getSuite(self.pocket)
            )

        # Search for related debug publications, and override them too.
        debugs = getUtility(IPublishingSet).findCorrespondingDDEBPublications(
            [self]
        )
        # We expect only one, but we will override all of them.
        for debug in debugs:
            BinaryPackagePublishingHistory(
                binarypackagename=debug.binarypackagename,
                binarypackagerelease=debug.binarypackagerelease,
                binarypackageformat=debug.binarypackageformat,
                distroarchseries=debug.distroarchseries,
                status=PackagePublishingStatus.PENDING,
                datecreated=UTC_NOW,
                pocket=debug.pocket,
                component=new_component,
                section=new_section,
                priority=new_priority,
                creator=creator,
                archive=debug.archive,
                phased_update_percentage=new_phased_update_percentage,
                channel=removeSecurityProxy(debug)._channel,
                sourcepackagename=debug.sourcepackagename,
            )

        # Append the modified package publishing entry
        bpph = BinaryPackagePublishingHistory(
            binarypackagename=bpr.binarypackagename,
            binarypackagerelease=bpr,
            binarypackageformat=bpr.binpackageformat,
            distroarchseries=self.distroarchseries,
            status=PackagePublishingStatus.PENDING,
            datecreated=UTC_NOW,
            pocket=self.pocket,
            component=new_component,
            section=new_section,
            priority=new_priority,
            archive=self.archive,
            creator=creator,
            phased_update_percentage=new_phased_update_percentage,
            channel=self._channel,
            sourcepackagename=(
                bpr.build.source_package_name
                if bpr.build is not None
                else None
            ),
        )
        IStore(bpph).flush()
        return bpph

    def copyTo(self, distroseries, pocket, archive):
        """See `BinaryPackagePublishingHistory`."""
        return getUtility(IPublishingSet).copyBinaries(
            archive, distroseries, pocket, [self]
        )

    def _getDownloadCountClauses(self, start_date=None, end_date=None):
        clauses = [
            BinaryPackageReleaseDownloadCount.archive == self.archive,
            BinaryPackageReleaseDownloadCount.binary_package_release
            == self.binarypackagerelease,
        ]

        if start_date is not None:
            clauses.append(BinaryPackageReleaseDownloadCount.day >= start_date)
        if end_date is not None:
            clauses.append(BinaryPackageReleaseDownloadCount.day <= end_date)

        return clauses

    def getDownloadCounts(self, start_date=None, end_date=None):
        """See `IBinaryPackagePublishingHistory`."""
        clauses = self._getDownloadCountClauses(start_date, end_date)

        return (
            Store.of(self)
            .using(
                BinaryPackageReleaseDownloadCount,
                LeftJoin(
                    Country,
                    BinaryPackageReleaseDownloadCount.country_id == Country.id,
                ),
            )
            .find(BinaryPackageReleaseDownloadCount, *clauses)
            .order_by(
                Desc(BinaryPackageReleaseDownloadCount.day), Country.name
            )
        )

    def getDailyDownloadTotals(self, start_date=None, end_date=None):
        """See `IBinaryPackagePublishingHistory`."""
        clauses = self._getDownloadCountClauses(start_date, end_date)

        results = (
            Store.of(self)
            .find(
                (
                    BinaryPackageReleaseDownloadCount.day,
                    Sum(BinaryPackageReleaseDownloadCount.count),
                ),
                *clauses,
            )
            .group_by(BinaryPackageReleaseDownloadCount.day)
        )

        def date_to_string(result):
            return (result[0].strftime("%Y-%m-%d"), result[1])

        return dict(date_to_string(result) for result in results)

    def api_requestDeletion(self, removed_by, removal_comment=None):
        """See `IPublishingEdit`."""
        # Special deletion method for the api.  We don't do anything
        # different here (yet).
        self.requestDeletion(removed_by, removal_comment)

    def requestDeletion(self, removed_by, removal_comment=None):
        """See `IPublishing`."""
        if not self.archive.canModifySuite(self.distroseries, self.pocket):
            raise DeletionError(
                "Cannot delete publications from suite '%s'"
                % self.distroseries.getSuite(self.pocket)
            )

        if self.is_debug:
            raise DeletionError(
                "Cannot delete ddeb publications directly; delete the "
                "corresponding deb instead."
            )

        self.setDeleted(removed_by, removal_comment)

    def binaryFileUrls(self, include_meta=False):
        """See `IBinaryPackagePublishingHistory`."""
        binaries = Store.of(self).find(
            (LibraryFileAlias, LibraryFileContent),
            LibraryFileContent.id == LibraryFileAlias.content_id,
            LibraryFileAlias.id == BinaryPackageFile.libraryfile_id,
            BinaryPackageFile.binarypackagerelease
            == self.binarypackagerelease_id,
        )
        binary_urls = proxied_urls(
            [binary for binary, _ in binaries], self.archive
        )
        if include_meta:
            meta = [
                (content.filesize, content.sha1, content.sha256)
                for _, content in binaries
            ]
            return [
                dict(url=url, size=size, sha1=sha1, sha256=sha256)
                for url, (size, sha1, sha256) in zip(binary_urls, meta)
            ]
        return binary_urls


def expand_binary_requests(distroseries, binaries):
    """Architecture-expand a dict of binary publication requests.

    For architecture-independent binaries, a tuple will be returned for each
    enabled architecture in the series.
    For architecture-dependent binaries, a tuple will be returned only for the
    architecture corresponding to the build architecture, if it exists and is
    enabled.

    :param binaries: A dict mapping `BinaryPackageReleases` to tuples of their
        desired overrides.

    :return: The binaries and the architectures in which they should be
        published, as a sequence of (`DistroArchSeries`,
        `BinaryPackageRelease`, (overrides)) tuples.
    """
    archs = list(distroseries.enabled_architectures)
    arch_map = {arch.architecturetag: arch for arch in archs}
    variant_map = {}
    for arch in archs:
        spf = arch.getSourceFilter()
        if spf and arch.underlying_architecturetag:
            variant_map.setdefault(arch.underlying_architecturetag, []).append(
                (arch, spf)
            )

    expanded = []
    for bpr, overrides in binaries.items():
        if bpr.architecturespecific:
            # Find the DAS in this series corresponding to the original
            # build arch tag. If it does not exist or is disabled, we should
            # not publish.
            target_arch = arch_map.get((bpr.build or bpr.ci_build).arch_tag)
            if target_arch is None:
                continue
            target_archs = [target_arch]
            for variant_arch, spf in variant_map.get(
                target_arch.architecturetag, []
            ):
                spn = bpr.build.source_package_release.sourcepackagename
                if not spf.isSourceIncluded(spn):
                    target_archs.append(variant_arch)
        else:
            target_archs = archs
        for target_arch in target_archs:
            expanded.append((target_arch, bpr, overrides))
    return expanded


@implementer(IPublishingSet)
class PublishingSet:
    """Utilities for manipulating publications in batches."""

    def publishBinaries(
        self,
        archive,
        distroseries,
        pocket,
        binaries,
        copied_from_archives=None,
        channel=None,
    ):
        """See `IPublishingSet`."""
        if copied_from_archives is None:
            copied_from_archives = {}
        if channel is not None:
            if pocket != PackagePublishingPocket.RELEASE:
                raise AssertionError(
                    "Channel publications must be in the RELEASE pocket"
                )
            channel = channel_string_to_list(channel)
        # Expand the dict of binaries into a list of tuples including the
        # architecture.
        if distroseries.distribution != archive.distribution:
            raise AssertionError(
                "Series distribution %s doesn't match archive distribution %s."
                % (distroseries.distribution.name, archive.distribution.name)
            )

        expanded = expand_binary_requests(distroseries, binaries)
        if len(expanded) == 0:
            # The binaries are for a disabled DistroArchSeries or for
            # an unsupported architecture.
            return []

        # Find existing publications.
        # We should really be able to just compare BPR.id, but
        # CopyChecker doesn't seem to ensure that there are no
        # conflicting binaries from other sources.
        def make_package_condition(archive, das, bpr):
            return And(
                BinaryPackagePublishingHistory.archive == archive,
                BinaryPackagePublishingHistory.distroarchseries == das,
                BinaryPackagePublishingHistory.binarypackagename
                == bpr.binarypackagename,
                Cast(BinaryPackageRelease.version, "text") == bpr.version,
            )

        candidates = (
            make_package_condition(archive, das, bpr)
            for das, bpr, overrides in expanded
        )
        already_published = (
            IPrimaryStore(BinaryPackagePublishingHistory)
            .find(
                (
                    BinaryPackagePublishingHistory.distroarchseries_id,
                    BinaryPackageRelease.binarypackagename_id,
                    BinaryPackageRelease.version,
                ),
                BinaryPackagePublishingHistory.pocket == pocket,
                Not(
                    IsDistinctFrom(
                        BinaryPackagePublishingHistory._channel,
                        json.dumps(channel) if channel is not None else None,
                    )
                ),
                BinaryPackagePublishingHistory.status.is_in(
                    active_publishing_status
                ),
                BinaryPackageRelease.id
                == BinaryPackagePublishingHistory.binarypackagerelease_id,
                Or(*candidates),
            )
            .config(distinct=True)
        )
        already_published = frozenset(already_published)

        needed = [
            (das, bpr, overrides)
            for (das, bpr, overrides) in expanded
            if (das.id, bpr.binarypackagename_id, bpr.version)
            not in already_published
        ]
        if not needed:
            return []

        BPPH = BinaryPackagePublishingHistory
        return bulk.create(
            (
                BPPH.archive,
                BPPH.copied_from_archive,
                BPPH.distroarchseries,
                BPPH.pocket,
                BPPH._channel,
                BPPH.binarypackagerelease,
                BPPH.binarypackagename,
                BPPH._binarypackageformat,
                BPPH.component,
                BPPH.section,
                BPPH.priority,
                BPPH.phased_update_percentage,
                BPPH.status,
                BPPH.datecreated,
                BPPH.sourcepackagename,
            ),
            [
                (
                    archive,
                    copied_from_archives.get(bpr),
                    das,
                    pocket,
                    channel,
                    bpr,
                    bpr.binarypackagename,
                    bpr.binpackageformat,
                    get_component(archive, das.distroseries, component),
                    section,
                    priority,
                    phased_update_percentage,
                    PackagePublishingStatus.PENDING,
                    UTC_NOW,
                    (
                        bpr.build.source_package_name
                        if bpr.build is not None
                        else None
                    ),
                )
                for (
                    das,
                    bpr,
                    (component, section, priority, phased_update_percentage),
                ) in needed
            ],
            get_objects=True,
        )

    def copyBinaries(
        self,
        archive,
        distroseries,
        pocket,
        bpphs,
        policy=None,
        source_override=None,
        channel=None,
    ):
        """See `IPublishingSet`."""
        from lp.soyuz.adapters.overrides import BinaryOverride

        if distroseries.distribution != archive.distribution:
            raise AssertionError(
                "Series distribution %s doesn't match archive distribution %s."
                % (distroseries.distribution.name, archive.distribution.name)
            )

        if bpphs is None:
            return

        if zope_isinstance(bpphs, list):
            if len(bpphs) == 0:
                return
        else:
            if bpphs.is_empty():
                return

        if policy is not None:
            bpn_archtag = {}
            ddebs = set()
            for bpph in bpphs:
                # DDEBs just inherit their corresponding DEB's
                # overrides, so don't ask for specific ones.
                if bpph.is_debug:
                    ddebs.add(bpph.binarypackagerelease)
                    continue

                bpn_archtag[
                    (
                        bpph.binarypackagerelease.binarypackagename,
                        bpph.distroarchseries.architecturetag,
                    )
                ] = bpph
            with_overrides = {}
            overrides = policy.calculateBinaryOverrides(
                {
                    (bpn, archtag): BinaryOverride(
                        source_override=source_override
                    )
                    for bpn, archtag in bpn_archtag.keys()
                }
            )
            for (bpn, archtag), override in overrides.items():
                bpph = bpn_archtag[(bpn, archtag)]
                new_component = override.component or bpph.component
                new_section = override.section or bpph.section
                new_priority = override.priority or bpph.priority
                # No "or bpph.phased_update_percentage" here; if the
                # override doesn't specify one then we leave it at None
                # (a.k.a. 100% of users).
                new_phased_update_percentage = (
                    override.phased_update_percentage
                )
                calculated = (
                    new_component,
                    new_section,
                    new_priority,
                    new_phased_update_percentage,
                )
                with_overrides[bpph.binarypackagerelease] = calculated

                # If there is a corresponding DDEB then give it our
                # overrides too. It should always be part of the copy
                # already.
                maybe_ddeb = bpph.binarypackagerelease.debug_package
                if maybe_ddeb is not None:
                    assert maybe_ddeb in ddebs
                    ddebs.remove(maybe_ddeb)
                    with_overrides[maybe_ddeb] = calculated
        else:
            with_overrides = {
                bpph.binarypackagerelease: (
                    bpph.component,
                    bpph.section,
                    bpph.priority,
                    None,
                )
                for bpph in bpphs
            }
        if not with_overrides:
            return list()
        copied_from_archives = {
            bpph.binarypackagerelease: bpph.archive for bpph in bpphs
        }
        return self.publishBinaries(
            archive,
            distroseries,
            pocket,
            with_overrides,
            copied_from_archives,
            channel=channel,
        )

    def newSourcePublication(
        self,
        archive,
        sourcepackagerelease,
        distroseries,
        pocket,
        component=None,
        section=None,
        ancestor=None,
        create_dsd_job=True,
        copied_from_archive=None,
        creator=None,
        sponsor=None,
        packageupload=None,
        channel=None,
    ):
        """See `IPublishingSet`."""
        # Circular import.
        from lp.registry.model.distributionsourcepackage import (
            DistributionSourcePackage,
        )

        if distroseries.distribution != archive.distribution:
            raise AssertionError(
                "Series distribution %s doesn't match archive distribution %s."
                % (distroseries.distribution.name, archive.distribution.name)
            )

        if channel is not None:
            if sourcepackagerelease.format == SourcePackageType.DPKG:
                raise AssertionError(
                    "Can't publish dpkg source packages to a channel"
                )
            if pocket != PackagePublishingPocket.RELEASE:
                raise AssertionError(
                    "Channel publications must be in the RELEASE pocket"
                )
            channel = channel_string_to_list(channel)

        if sourcepackagerelease.format == SourcePackageType.DPKG:
            if component is None:
                raise AssertionError(
                    "dpkg source publications require a component"
                )
            if section is None:
                raise AssertionError(
                    "dpkg source publications require a section"
                )

        pub = SourcePackagePublishingHistory(
            distroseries=distroseries,
            pocket=pocket,
            copied_from_archive=copied_from_archive,
            archive=archive,
            sourcepackagename=sourcepackagerelease.sourcepackagename,
            sourcepackagerelease=sourcepackagerelease,
            format=sourcepackagerelease.format,
            component=get_component(archive, distroseries, component),
            section=section,
            status=PackagePublishingStatus.PENDING,
            datecreated=UTC_NOW,
            ancestor=ancestor,
            creator=creator,
            sponsor=sponsor,
            packageupload=packageupload,
            channel=channel,
        )
        DistributionSourcePackage.ensure(pub)

        if create_dsd_job and archive == distroseries.main_archive:
            dsd_job_source = getUtility(IDistroSeriesDifferenceJobSource)
            dsd_job_source.createForPackagePublication(
                distroseries, sourcepackagerelease.sourcepackagename, pocket
            )
        Store.of(sourcepackagerelease).flush()
        del get_property_cache(sourcepackagerelease).published_archives

        return pub

    def getBuildsForSourceIds(
        self, source_publication_ids, archive=None, build_states=None
    ):
        """See `IPublishingSet`."""
        # We're interested in the binaries resulting from builds in the same
        # distroseries as the SPPH.
        bpb_tables = [
            LeftJoin(
                BinaryPackageBuild,
                And(
                    SourcePackagePublishingHistory.distroseries_id
                    == BinaryPackageBuild.distro_series_id,
                    SourcePackagePublishingHistory.sourcepackagerelease_id
                    == BinaryPackageBuild.source_package_release_id,
                ),
            ),
        ]

        extra_exprs = [
            SourcePackagePublishingHistory.id.is_in(source_publication_ids)
        ]

        # If an archive was passed in as a parameter, add an extra expression
        # to filter by archive:
        if archive is not None:
            extra_exprs.append(
                SourcePackagePublishingHistory.archive == archive
            )

        # If an optional list of build states was passed in as a parameter,
        # ensure that the result is limited to builds in those states.
        if build_states is not None:
            extra_exprs.append(BinaryPackageBuild.status.is_in(build_states))

        store = IStore(SourcePackagePublishingHistory)

        # First, we'll find the binary package builds that were built in the
        # same archive context as the published sources.
        build_ids_in_same_archive = Select(
            BinaryPackageBuild.id,
            tables=(SourcePackagePublishingHistory, *bpb_tables),
            where=And(
                SourcePackagePublishingHistory.archive_id
                == BinaryPackageBuild.archive_id,
                *extra_exprs,
            ),
        )

        # Next get all the binary package builds that have a binary
        # published in the same archive... even though the build was not
        # built in the same context archive.
        build_ids_copied_into_archive = Select(
            BinaryPackageBuild.id,
            tables=(
                SourcePackagePublishingHistory,
                *bpb_tables,
                Join(
                    BinaryPackageRelease,
                    BinaryPackageRelease.build == BinaryPackageBuild.id,
                ),
                Join(
                    BinaryPackagePublishingHistory,
                    And(
                        BinaryPackagePublishingHistory.archive
                        == SourcePackagePublishingHistory.archive_id,
                        BinaryPackagePublishingHistory.binarypackagerelease
                        == BinaryPackageRelease.id,
                    ),
                ),
            ),
            where=And(
                SourcePackagePublishingHistory.archive_id
                != BinaryPackageBuild.archive_id,
                *extra_exprs,
            ),
        )

        # Now that we have select expressions for all the builds, we'll use
        # them as subqueries to get the required publishing and arch to do
        # the ordering. We do this in this round-about way because we can't
        # sort on SourcePackagePublishingHistory.id after the union. See bug
        # 443353 for details.
        result_set = store.using(
            SourcePackagePublishingHistory,
            *bpb_tables,
            Join(
                DistroArchSeries,
                BinaryPackageBuild.distro_arch_series == DistroArchSeries.id,
            ),
        ).find(
            (
                SourcePackagePublishingHistory,
                BinaryPackageBuild,
                DistroArchSeries,
            ),
            BinaryPackageBuild.id.is_in(
                Union(build_ids_in_same_archive, build_ids_copied_into_archive)
            ),
            *extra_exprs,
        )

        return result_set.order_by(
            SourcePackagePublishingHistory.id, DistroArchSeries.architecturetag
        )

    def getByIdAndArchive(self, id, archive, source=True):
        """See `IPublishingSet`."""
        if source:
            baseclass = SourcePackagePublishingHistory
        else:
            baseclass = BinaryPackagePublishingHistory
        return (
            Store.of(archive)
            .find(
                baseclass, baseclass.id == id, baseclass.archive == archive.id
            )
            .one()
        )

    def _extractIDs(self, one_or_more_source_publications):
        """Return a list of database IDs for the given list or single object.

        :param one_or_more_source_publications: an single object or a list of
            `ISourcePackagePublishingHistory` objects.

        :return: a list of database IDs corresponding to the give set of
            objects.
        """
        try:
            source_publications = tuple(one_or_more_source_publications)
        except TypeError:
            source_publications = (one_or_more_source_publications,)

        return [
            source_publication.id for source_publication in source_publications
        ]

    def getBuildsForSources(self, one_or_more_source_publications):
        """See `IPublishingSet`."""
        source_publication_ids = self._extractIDs(
            one_or_more_source_publications
        )

        return self.getBuildsForSourceIds(source_publication_ids)

    def _getSourceBinaryJoinForSources(
        self, source_publication_ids, active_binaries_only=True
    ):
        """Return the join linking sources with binaries."""
        join = [
            SourcePackagePublishingHistory.sourcepackagerelease_id
            == BinaryPackageBuild.source_package_release_id,
            BinaryPackageRelease.build == BinaryPackageBuild.id,
            BinaryPackageRelease.binarypackagename_id == BinaryPackageName.id,
            SourcePackagePublishingHistory.distroseries_id
            == DistroArchSeries.distroseries_id,
            BinaryPackagePublishingHistory.distroarchseries_id
            == DistroArchSeries.id,
            BinaryPackagePublishingHistory.binarypackagerelease
            == BinaryPackageRelease.id,
            BinaryPackagePublishingHistory.pocket
            == SourcePackagePublishingHistory.pocket,
            BinaryPackagePublishingHistory.archive_id
            == SourcePackagePublishingHistory.archive_id,
            SourcePackagePublishingHistory.id.is_in(source_publication_ids),
        ]

        # If the call-site requested to join only on binaries published
        # with an active publishing status then we need to further restrict
        # the join.
        if active_binaries_only:
            join.append(
                BinaryPackagePublishingHistory.status.is_in(
                    active_publishing_status
                )
            )

        return join

    def getUnpublishedBuildsForSources(
        self, one_or_more_source_publications, build_states=None
    ):
        """See `IPublishingSet`."""
        # The default build state that we'll search for is FULLYBUILT
        if build_states is None:
            build_states = [BuildStatus.FULLYBUILT]

        source_publication_ids = self._extractIDs(
            one_or_more_source_publications
        )

        store = IStore(SourcePackagePublishingHistory)
        published_builds = store.find(
            (
                SourcePackagePublishingHistory,
                BinaryPackageBuild,
                DistroArchSeries,
            ),
            self._getSourceBinaryJoinForSources(
                source_publication_ids, active_binaries_only=False
            ),
            BinaryPackagePublishingHistory.datepublished != None,
            BinaryPackageBuild.status.is_in(build_states),
        )

        published_builds.order_by(
            SourcePackagePublishingHistory.id, DistroArchSeries.architecturetag
        )

        # Now to return all the unpublished builds, we use the difference
        # of all builds minus the published ones.
        unpublished_builds = self.getBuildsForSourceIds(
            source_publication_ids, build_states=build_states
        ).difference(published_builds)

        return unpublished_builds

    def getBinaryFilesForSources(self, one_or_more_source_publications):
        """See `IPublishingSet`."""
        source_publication_ids = self._extractIDs(
            one_or_more_source_publications
        )

        store = IStore(SourcePackagePublishingHistory)
        binary_result = store.find(
            (
                SourcePackagePublishingHistory,
                LibraryFileAlias,
                LibraryFileContent,
            ),
            LibraryFileContent.id == LibraryFileAlias.content_id,
            LibraryFileAlias.id == BinaryPackageFile.libraryfile_id,
            BinaryPackageFile.binarypackagerelease == BinaryPackageRelease.id,
            BinaryPackageRelease.build_id == BinaryPackageBuild.id,
            SourcePackagePublishingHistory.sourcepackagerelease_id
            == BinaryPackageBuild.source_package_release_id,
            BinaryPackagePublishingHistory.binarypackagerelease_id
            == BinaryPackageRelease.id,
            BinaryPackagePublishingHistory.archive_id
            == SourcePackagePublishingHistory.archive_id,
            SourcePackagePublishingHistory.id.is_in(source_publication_ids),
        )

        return binary_result.order_by(LibraryFileAlias.id)

    def getFilesForSources(self, one_or_more_source_publications):
        """See `IPublishingSet`."""
        source_publication_ids = self._extractIDs(
            one_or_more_source_publications
        )

        store = IStore(SourcePackagePublishingHistory)
        source_result = store.find(
            (
                SourcePackagePublishingHistory,
                LibraryFileAlias,
                LibraryFileContent,
            ),
            LibraryFileContent.id == LibraryFileAlias.content_id,
            LibraryFileAlias.id == SourcePackageReleaseFile.libraryfile_id,
            SourcePackageReleaseFile.sourcepackagerelease
            == SourcePackagePublishingHistory.sourcepackagerelease_id,
            SourcePackagePublishingHistory.id.is_in(source_publication_ids),
        )

        binary_result = self.getBinaryFilesForSources(
            one_or_more_source_publications
        )

        result_set = source_result.union(binary_result.config(distinct=True))

        return result_set

    def getBinaryPublicationsForSources(
        self, one_or_more_source_publications, active_binaries_only=True
    ):
        """See `IPublishingSet`."""
        source_publication_ids = self._extractIDs(
            one_or_more_source_publications
        )

        result_set = IStore(SourcePackagePublishingHistory).find(
            (
                SourcePackagePublishingHistory,
                BinaryPackagePublishingHistory,
                BinaryPackageRelease,
                BinaryPackageName,
                DistroArchSeries,
            ),
            self._getSourceBinaryJoinForSources(
                source_publication_ids,
                active_binaries_only=active_binaries_only,
            ),
        )

        result_set.order_by(
            SourcePackagePublishingHistory.id,
            BinaryPackageName.name,
            DistroArchSeries.architecturetag,
            Desc(BinaryPackagePublishingHistory.id),
        )

        return result_set

    def getBuiltPackagesSummaryForSourcePublication(self, source_publication):
        """See `IPublishingSet`."""
        result_set = IStore(BinaryPackageName).find(
            (
                BinaryPackageName.name,
                BinaryPackageRelease.summary,
                DistroArchSeries.architecturetag,
                BinaryPackagePublishingHistory.id,
            ),
            self._getSourceBinaryJoinForSources([source_publication.id]),
        )
        result_set.config(distinct=(BinaryPackageName.name,))
        result_set.order_by(
            BinaryPackageName.name,
            DistroArchSeries.architecturetag,
            Desc(BinaryPackagePublishingHistory.id),
        )
        return [
            {"binarypackagename": name, "summary": summary}
            for name, summary, _, _ in result_set
        ]

    def getActiveArchSpecificPublications(
        self, sourcepackagerelease, archive, distroseries, pocket
    ):
        """See `IPublishingSet`."""
        return IStore(BinaryPackagePublishingHistory).find(
            BinaryPackagePublishingHistory,
            BinaryPackageBuild.source_package_release_id
            == sourcepackagerelease.id,
            BinaryPackageRelease.build == BinaryPackageBuild.id,
            BinaryPackagePublishingHistory.binarypackagerelease_id
            == BinaryPackageRelease.id,
            BinaryPackagePublishingHistory.archive == archive,
            BinaryPackagePublishingHistory.distroarchseries_id
            == DistroArchSeries.id,
            DistroArchSeries.distroseries == distroseries,
            BinaryPackagePublishingHistory.pocket == pocket,
            BinaryPackagePublishingHistory.status.is_in(
                active_publishing_status
            ),
            BinaryPackageRelease.architecturespecific == True,
        )

    def getSourcesForPublishing(
        self, archive, distroseries=None, pocket=None, component=None
    ):
        """See `IPublishingSet`."""
        clauses = [
            SourcePackagePublishingHistory.archive == archive,
            SourcePackagePublishingHistory.status
            == PackagePublishingStatus.PUBLISHED,
            SourcePackagePublishingHistory.sourcepackagename
            == SourcePackageName.id,
        ]
        if distroseries is not None:
            clauses.append(
                SourcePackagePublishingHistory.distroseries == distroseries
            )
        if pocket is not None:
            clauses.append(SourcePackagePublishingHistory.pocket == pocket)
        if component is not None:
            clauses.append(
                SourcePackagePublishingHistory.component == component
            )
        spphs = (
            IStore(SourcePackagePublishingHistory)
            .find(SourcePackagePublishingHistory, *clauses)
            .order_by(SourcePackageName.name)
        )

        def eager_load(spphs):
            # Preload everything which will be used by archivepublisher's
            # build_source_stanza_fields.
            bulk.load_related(Section, spphs, ["section_id"])
            sprs = bulk.load_related(
                SourcePackageRelease, spphs, ["sourcepackagerelease_id"]
            )
            bulk.load_related(
                SourcePackageName, sprs, ["sourcepackagename_id"]
            )
            spr_ids = set(map(attrgetter("id"), sprs))
            sprfs = list(
                IStore(SourcePackageReleaseFile)
                .find(
                    SourcePackageReleaseFile,
                    SourcePackageReleaseFile.sourcepackagerelease_id.is_in(
                        spr_ids
                    ),
                )
                .order_by(SourcePackageReleaseFile.libraryfile_id)
            )
            file_map = defaultdict(list)
            for sprf in sprfs:
                file_map[sprf.sourcepackagerelease].append(sprf)
            for spr, files in file_map.items():
                get_property_cache(spr).files = files
            lfas = bulk.load_related(
                LibraryFileAlias, sprfs, ["libraryfile_id"]
            )
            bulk.load_related(LibraryFileContent, lfas, ["content_id"])

        return DecoratedResultSet(spphs, pre_iter_hook=eager_load)

    def getBinariesForPublishing(
        self, archive, distroarchseries=None, pocket=None, component=None
    ):
        """See `IPublishingSet`."""
        clauses = [
            BinaryPackagePublishingHistory.archive == archive,
            BinaryPackagePublishingHistory.status
            == PackagePublishingStatus.PUBLISHED,
            BinaryPackagePublishingHistory.binarypackagename
            == BinaryPackageName.id,
        ]
        if distroarchseries is not None:
            clauses.append(
                BinaryPackagePublishingHistory.distroarchseries
                == distroarchseries
            )
        if pocket is not None:
            clauses.append(BinaryPackagePublishingHistory.pocket == pocket)
        if component is not None:
            clauses.append(
                BinaryPackagePublishingHistory.component == component
            )
        bpphs = (
            IStore(BinaryPackagePublishingHistory)
            .find(BinaryPackagePublishingHistory, *clauses)
            .order_by(BinaryPackageName.name)
        )

        def eager_load(bpphs):
            # Preload everything which will be used by archivepublisher's
            # build_binary_stanza_fields.
            bulk.load_related(Section, bpphs, ["section_id"])
            bprs = bulk.load_related(
                BinaryPackageRelease, bpphs, ["binarypackagerelease_id"]
            )
            bpbs = bulk.load_related(BinaryPackageBuild, bprs, ["build_id"])
            sprs = bulk.load_related(
                SourcePackageRelease, bpbs, ["source_package_release_id"]
            )
            bpfs = bulk.load_referencing(
                BinaryPackageFile, bprs, ["binarypackagerelease_id"]
            )
            file_map = defaultdict(list)
            for bpf in bpfs:
                file_map[bpf.binarypackagerelease].append(bpf)
            for bpr, files in file_map.items():
                get_property_cache(bpr).files = files
            lfas = bulk.load_related(
                LibraryFileAlias, bpfs, ["libraryfile_id"]
            )
            bulk.load_related(LibraryFileContent, lfas, ["content_id"])
            bulk.load_related(
                SourcePackageName, sprs, ["sourcepackagename_id"]
            )
            bulk.load_related(
                BinaryPackageName, bprs, ["binarypackagename_id"]
            )

        return DecoratedResultSet(bpphs, pre_iter_hook=eager_load)

    def getChangesFilesForSources(self, one_or_more_source_publications):
        """See `IPublishingSet`."""
        # Avoid circular imports.
        from lp.soyuz.model.queue import PackageUpload, PackageUploadSource

        source_publication_ids = self._extractIDs(
            one_or_more_source_publications
        )

        result_set = IStore(SourcePackagePublishingHistory).find(
            (
                SourcePackagePublishingHistory,
                PackageUpload,
                SourcePackageRelease,
                LibraryFileAlias,
                LibraryFileContent,
            ),
            LibraryFileContent.id == LibraryFileAlias.content_id,
            LibraryFileAlias.id == PackageUpload.changes_file_id,
            PackageUpload.id == PackageUploadSource.packageupload_id,
            PackageUpload.status == PackageUploadStatus.DONE,
            PackageUpload.distroseries
            == SourcePackageRelease.upload_distroseries_id,
            PackageUpload.archive == SourcePackageRelease.upload_archive_id,
            PackageUploadSource.sourcepackagerelease
            == SourcePackageRelease.id,
            SourcePackageRelease.id
            == SourcePackagePublishingHistory.sourcepackagerelease_id,
            SourcePackagePublishingHistory.id.is_in(source_publication_ids),
        )

        result_set.order_by(SourcePackagePublishingHistory.id)
        return result_set

    def getChangesFileLFA(self, spr):
        """See `IPublishingSet`."""
        # Avoid circular imports.
        from lp.soyuz.model.queue import PackageUpload, PackageUploadSource

        return (
            IStore(SourcePackagePublishingHistory)
            .find(
                LibraryFileAlias,
                LibraryFileAlias.id == PackageUpload.changes_file_id,
                PackageUpload.status == PackageUploadStatus.DONE,
                PackageUpload.distroseries == spr.upload_distroseries,
                PackageUpload.archive == spr.upload_archive,
                PackageUploadSource.packageupload == PackageUpload.id,
                PackageUploadSource.sourcepackagerelease == spr,
            )
            .one()
        )

    def getBuildStatusSummariesForSourceIdsAndArchive(
        self, source_ids, archive
    ):
        """See `IPublishingSet`."""
        # source_ids can be None or an empty sequence.
        if not source_ids:
            return {}

        store = IStore(SourcePackagePublishingHistory)
        # Find relevant builds while also getting PackageBuilds and
        # BuildFarmJobs into the cache. They're used later.
        build_info = list(
            self.getBuildsForSourceIds(source_ids, archive=archive)
        )
        source_pubs = set()
        found_source_ids = set()
        for row in build_info:
            source_pubs.add(row[0])
            found_source_ids.add(row[0].id)
        pubs_without_builds = set(source_ids) - found_source_ids
        if pubs_without_builds:
            # Add in source pubs for which no builds were found: we may in
            # future want to make this a LEFT OUTER JOIN in
            # getBuildsForSourceIds but to avoid destabilising other code
            # paths while we fix performance, it is just done as a single
            # separate query for now.
            source_pubs.update(
                store.find(
                    SourcePackagePublishingHistory,
                    SourcePackagePublishingHistory.id.is_in(
                        pubs_without_builds
                    ),
                    SourcePackagePublishingHistory.archive == archive,
                )
            )
        # For each source_pub found, provide an aggregate summary of its
        # builds.
        binarypackages = getUtility(IBinaryPackageBuildSet)
        source_build_statuses = {}
        need_unpublished = set()
        for source_pub in source_pubs:
            source_builds = [
                build for build in build_info if build[0].id == source_pub.id
            ]
            builds = SourcePackagePublishingHistory._convertBuilds(
                source_builds
            )
            summary = binarypackages.getStatusSummaryForBuilds(builds)
            # Thank you, Zope, for security wrapping an abstract data
            # structure.
            summary = removeSecurityProxy(summary)
            summary["date_published"] = source_pub.datepublished
            summary["source_package_name"] = source_pub.source_package_name
            source_build_statuses[source_pub.id] = summary

            # If:
            #   1. the SPPH is in an active publishing state, and
            #   2. all the builds are fully-built, and
            #   3. the SPPH is not being published in a rebuild/copy
            #      archive (in which case the binaries are not published)
            #   4. There are unpublished builds
            # Then we augment the result with FULLYBUILT_PENDING and
            # attach the unpublished builds.
            if (
                source_pub.status in active_publishing_status
                and summary["status"] == BuildSetStatus.FULLYBUILT
                and not source_pub.archive.is_copy
            ):
                need_unpublished.add(source_pub)

        if need_unpublished:
            unpublished = list(
                self.getUnpublishedBuildsForSources(need_unpublished)
            )
            unpublished_per_source = defaultdict(list)
            for source_pub, build, _ in unpublished:
                unpublished_per_source[source_pub].append(build)
            for source_pub, builds in unpublished_per_source.items():
                summary = {
                    "status": BuildSetStatus.FULLYBUILT_PENDING,
                    "builds": builds,
                    "date_published": source_pub.datepublished,
                    "source_package_name": source_pub.source_package_name,
                }
                source_build_statuses[source_pub.id] = summary

        return source_build_statuses

    def getBuildStatusSummaryForSourcePublication(self, source_publication):
        """See `ISourcePackagePublishingHistory`.getStatusSummaryForBuilds.

        This is provided here so it can be used by both the SPPH as well
        as our delegate class ArchiveSourcePublication, which implements
        the same interface but uses cached results for builds and binaries
        used in the calculation.
        """
        source_id = source_publication.id
        return self.getBuildStatusSummariesForSourceIdsAndArchive(
            [source_id], source_publication.archive
        )[source_id]

    def getRecentSourceUploads(
        self, distroseries, creator=None, offset=0, limit=20
    ) -> List[SourceUploadInfo]:
        """See `IPublishingSet`."""
        clauses = [
            SourcePackagePublishingHistory.distroseries == distroseries,
            SourcePackagePublishingHistory.status.is_in(
                active_publishing_status
            ),
            SourcePackagePublishingHistory.archive_id.is_in(
                distroseries.distribution.all_distro_archive_ids
            ),
        ]
        if creator is not None:
            clauses.append(SourcePackagePublishingHistory.creator == creator)
        store = IStore(SourcePackagePublishingHistory)
        publications = list(
            store.find(SourcePackagePublishingHistory, *clauses)
            .order_by(Desc(SourcePackagePublishingHistory.datecreated))
            .config(offset=offset, limit=limit)
        )

        if not publications:
            return []

        # Bulk-load to avoid N+1 queries on .name and .version.
        releases = bulk.load_related(
            SourcePackageRelease, publications, ["sourcepackagerelease_id"]
        )
        bulk.load_related(
            SourcePackageName, releases, ["sourcepackagename_id"]
        )

        # Get builds for all publications in one UNION query.
        builds_by_publication: defaultdict[
            int, List[SourceUploadBuildInfo]
        ] = defaultdict(list)
        for pub, build, das in self.getBuildsForSources(publications):
            builds_by_publication[pub.id].append(
                SourceUploadBuildInfo(
                    arch_tag=das.architecturetag,
                    build_status=build.status,
                )
            )

        return [
            SourceUploadInfo(
                name=pub.source_package_name,
                version=pub.source_package_version,
                pocket_title=pub.pocket.title,
                builds=builds_by_publication.get(pub.id, []),
            )
            for pub in publications
        ]

    def setMultipleDeleted(
        self, publication_class, ids, removed_by, removal_comment=None
    ):
        """Mark multiple publication records as deleted."""
        ids = list(ids)
        if len(ids) == 0:
            return

        permitted_classes = [
            BinaryPackagePublishingHistory,
            SourcePackagePublishingHistory,
        ]
        assert publication_class in permitted_classes, "Deleting wrong type."

        if removed_by is None:
            removed_by_id = None
        else:
            removed_by_id = removed_by.id

        affected_pubs = IPrimaryStore(publication_class).find(
            publication_class, publication_class.id.is_in(ids)
        )
        affected_pubs.set(
            status=PackagePublishingStatus.DELETED,
            datesuperseded=UTC_NOW,
            removed_by_id=removed_by_id,
            removal_comment=removal_comment,
        )

        # Find and mark any related debug packages.
        if publication_class == BinaryPackagePublishingHistory:
            debug_ids = [
                pub.id
                for pub in self.findCorrespondingDDEBPublications(
                    affected_pubs
                )
            ]
            IPrimaryStore(publication_class).find(
                BinaryPackagePublishingHistory,
                BinaryPackagePublishingHistory.id.is_in(debug_ids),
            ).set(
                status=PackagePublishingStatus.DELETED,
                datesuperseded=UTC_NOW,
                removed_by_id=removed_by_id,
                removal_comment=removal_comment,
            )

    def findCorrespondingDDEBPublications(self, pubs):
        """See `IPublishingSet`."""
        ids = [pub.id for pub in pubs]
        deb_bpph = ClassAlias(BinaryPackagePublishingHistory)
        debug_bpph = BinaryPackagePublishingHistory
        origin = [
            deb_bpph,
            Join(
                BinaryPackageRelease,
                deb_bpph.binarypackagerelease_id == BinaryPackageRelease.id,
            ),
            Join(
                debug_bpph,
                debug_bpph.binarypackagerelease_id
                == BinaryPackageRelease.debug_package_id,
            ),
        ]
        return (
            IPrimaryStore(debug_bpph)
            .using(*origin)
            .find(
                debug_bpph,
                deb_bpph.id.is_in(ids),
                debug_bpph.status.is_in(active_publishing_status),
                deb_bpph.archive_id == debug_bpph.archive_id,
                deb_bpph.distroarchseries_id == debug_bpph.distroarchseries_id,
                deb_bpph.pocket == debug_bpph.pocket,
                deb_bpph.component_id == debug_bpph.component_id,
                deb_bpph.section_id == debug_bpph.section_id,
                deb_bpph.priority == debug_bpph.priority,
                Not(
                    IsDistinctFrom(
                        deb_bpph.phased_update_percentage,
                        debug_bpph.phased_update_percentage,
                    )
                ),
            )
        )

    def requestDeletion(self, pubs, removed_by, removal_comment=None):
        """See `IPublishingSet`."""
        pubs = list(pubs)
        sources = [
            pub
            for pub in pubs
            if ISourcePackagePublishingHistory.providedBy(pub)
        ]
        binaries = [
            pub
            for pub in pubs
            if IBinaryPackagePublishingHistory.providedBy(pub)
        ]
        if not sources and not binaries:
            return
        assert len(sources) + len(binaries) == len(pubs)

        locations = {
            (pub.archive, pub.distroseries, pub.pocket) for pub in pubs
        }
        for archive, distroseries, pocket in locations:
            if not archive.canModifySuite(distroseries, pocket):
                raise DeletionError(
                    "Cannot delete publications from suite '%s'"
                    % distroseries.getSuite(pocket)
                )

        spph_ids = [spph.id for spph in sources]
        self.setMultipleDeleted(
            SourcePackagePublishingHistory,
            spph_ids,
            removed_by,
            removal_comment=removal_comment,
        )

        getUtility(IDistroSeriesDifferenceJobSource).createForSPPHs(sources)

        # Append the sources' related binaries to our condemned list,
        # and mark them all deleted.
        bpph_ids = [bpph.id for bpph in binaries]
        bpph_ids.extend(
            self.getBinaryPublicationsForSources(sources).values(
                BinaryPackagePublishingHistory.id
            )
        )
        if len(bpph_ids) > 0:
            self.setMultipleDeleted(
                BinaryPackagePublishingHistory,
                bpph_ids,
                removed_by,
                removal_comment=removal_comment,
            )


def get_current_source_releases(
    context_sourcepackagenames,
    archive_ids_func,
    package_clause_func,
    extra_clauses,
    key_col,
):
    """Get the current source package releases in a context.

    You probably don't want to use this directly; try
    (Distribution|DistroSeries)(Set)?.getCurrentSourceReleases instead.
    """
    # Builds one query for all the distro_source_packagenames.
    # This may need tuning: its possible that grouping by the common
    # archives may yield better efficiency: the current code is
    # just a direct push-down of the previous in-python lookup to SQL.
    series_clauses = []
    for context, package_names in context_sourcepackagenames.items():
        clause = And(
            SourcePackagePublishingHistory.sourcepackagename_id.is_in(
                map(attrgetter("id"), package_names)
            ),
            SourcePackagePublishingHistory.archive_id.is_in(
                archive_ids_func(context)
            ),
            package_clause_func(context),
        )
        series_clauses.append(clause)
    if not len(series_clauses):
        return {}

    releases = (
        IStore(SourcePackageRelease)
        .find(
            (SourcePackageRelease, key_col),
            SourcePackagePublishingHistory.sourcepackagerelease_id
            == SourcePackageRelease.id,
            SourcePackagePublishingHistory.status.is_in(
                active_publishing_status
            ),
            Or(*series_clauses),
            *extra_clauses,
        )
        .config(distinct=(SourcePackageRelease.sourcepackagename_id, key_col))
        .order_by(
            SourcePackageRelease.sourcepackagename_id,
            key_col,
            Desc(SourcePackagePublishingHistory.id),
        )
    )
    return releases
