# Copyright 2010-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import logging
import os
import os.path
import tarfile
import tempfile
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import zstandard
from lazr.delegates import delegate_to
from packaging.utils import parse_wheel_filename
from pkginfo import SDist, Wheel
from storm.expr import And
from storm.locals import JSON, Int, Reference
from zope.component import getUtility
from zope.interface import implementer, provider
from zope.security.proxy import removeSecurityProxy

from lp.code.enums import RevisionStatusArtifactType
from lp.code.interfaces.cibuild import ICIBuildSet
from lp.code.interfaces.revisionstatus import (
    IRevisionStatusArtifact,
    IRevisionStatusArtifactSet,
    IRevisionStatusReport,
)
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.distroseries import IDistroSeriesSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.sourcepackage import SourcePackageFileType
from lp.services.config import config
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore
from lp.services.database.stormbase import StormBase
from lp.services.job.model.job import EnumeratedSubclass, Job
from lp.services.job.runner import BaseRunnableJob
from lp.services.librarian.interfaces.client import LibrarianServerError
from lp.services.librarian.utils import copy_and_close
from lp.services.mail.sendmail import format_address_for_person
from lp.soyuz.adapters.gomodparser import GoModParserException, parse_go_mod
from lp.soyuz.enums import (
    ArchiveJobType,
    ArchiveRepositoryFormat,
    BinaryPackageFileType,
    BinaryPackageFormat,
    PackageUploadStatus,
)
from lp.soyuz.interfaces.archivejob import (
    IArchiveJob,
    IArchiveJobSource,
    ICIBuildUploadJob,
    ICIBuildUploadJobSource,
    IPackageUploadNotificationJob,
    IPackageUploadNotificationJobSource,
)
from lp.soyuz.interfaces.binarypackagename import IBinaryPackageNameSet
from lp.soyuz.interfaces.publishing import IPublishingSet
from lp.soyuz.interfaces.queue import IPackageUploadSet
from lp.soyuz.model.archive import Archive

logger = logging.getLogger(__name__)


@implementer(IArchiveJob)
class ArchiveJob(StormBase):
    """Base class for jobs related to Archives."""

    __storm_table__ = "ArchiveJob"

    id = Int(primary=True)

    job_id = Int(name="job")
    job = Reference(job_id, Job.id)

    archive_id = Int(name="archive")
    archive = Reference(archive_id, Archive.id)

    job_type = DBEnum(enum=ArchiveJobType, allow_none=False)

    metadata = JSON("json_data")

    def __init__(self, archive, job_type, metadata):
        """Create an ArchiveJob.

        :param archive: the `IArchive` this job relates to.
        :param job_type: the `ArchiveJobType` of this job.
        :param metadata: the type-specific variables, as a json-compatible
            dict.
        """
        super().__init__()
        self.job = Job()
        self.archive = archive
        self.job_type = job_type
        self.metadata = metadata

    def makeDerived(self):
        return ArchiveJobDerived.makeSubclass(self)


@delegate_to(IArchiveJob)
@provider(IArchiveJobSource)
class ArchiveJobDerived(BaseRunnableJob, metaclass=EnumeratedSubclass):
    """Intermediate class for deriving from ArchiveJob."""

    def __init__(self, job):
        self.context = job

    @classmethod
    def create(cls, archive, metadata=None):
        """See `IArchiveJob`."""
        if metadata is None:
            metadata = {}
        job = ArchiveJob(archive, cls.class_job_type, metadata)
        derived = cls(job)
        derived.celeryRunOnCommit()
        return derived

    @classmethod
    def iterReady(cls):
        """Iterate through all ready ArchiveJobs."""
        store = IPrimaryStore(ArchiveJob)
        jobs = store.find(
            ArchiveJob,
            And(
                ArchiveJob.job_type == cls.class_job_type,
                ArchiveJob.job_id.is_in(Job.ready_jobs),
            ),
        )
        return (cls(job) for job in jobs)

    def getOopsVars(self):
        """See `IRunnableJob`."""
        vars = super().getOopsVars()
        vars.extend(
            [
                ("archive_id", self.context.archive.id),
                ("archive_job_id", self.context.id),
                ("archive_job_type", self.context.job_type.title),
            ]
        )
        return vars


@implementer(IPackageUploadNotificationJob)
@provider(IPackageUploadNotificationJobSource)
class PackageUploadNotificationJob(ArchiveJobDerived):
    class_job_type = ArchiveJobType.PACKAGE_UPLOAD_NOTIFICATION

    config = config.IPackageUploadNotificationJobSource

    @classmethod
    def create(cls, packageupload, summary_text=None):
        """See `IPackageUploadNotificationJobSource`."""
        metadata = {
            "packageupload_id": packageupload.id,
            "packageupload_status": packageupload.status.title,
            "summary_text": summary_text,
        }
        return super().create(packageupload.archive, metadata)

    def getOopsVars(self):
        """See `ArchiveJobDerived`."""
        vars = super().getOopsVars()
        vars.extend(
            [
                ("packageupload_id", self.metadata["packageupload_id"]),
                (
                    "packageupload_status",
                    self.metadata["packageupload_status"],
                ),
                ("summary_text", self.metadata["summary_text"]),
            ]
        )
        return vars

    @property
    def packageupload(self):
        return getUtility(IPackageUploadSet).get(
            self.metadata["packageupload_id"]
        )

    @property
    def packageupload_status(self):
        return PackageUploadStatus.getTermByToken(
            self.metadata["packageupload_status"]
        ).value

    @property
    def summary_text(self):
        return self.metadata["summary_text"]

    def run(self):
        """See `IRunnableJob`."""
        packageupload = self.packageupload
        if packageupload.changesfile is None:
            changes_file_object = None
        else:
            changes_file_object = io.BytesIO(packageupload.changesfile.read())
        packageupload.notify(
            status=self.packageupload_status,
            summary_text=self.summary_text,
            changes_file_object=changes_file_object,
            logger=logger,
        )


class ScanException(Exception):
    """A CI build upload job failed to scan a file."""


class SourceArtifactMetadata:
    """Metadata extracted from a source package."""

    def __init__(
        self,
        format: SourcePackageFileType,
        name: str,
        version: str,
        user_defined_fields: Optional[List[Tuple[str, str]]] = None,
    ):
        self.format = format
        self.name = name
        self.version = version
        self.user_defined_fields = user_defined_fields


class BinaryArtifactMetadata:
    """Metadata extracted from a binary package."""

    def __init__(
        self,
        format: BinaryPackageFormat,
        name: str,
        version: str,
        summary: str,
        description: str,
        architecturespecific: bool,
        homepage: str,
        user_defined_fields: Optional[List[Tuple[str, str]]] = None,
    ):
        self.format = format
        self.name = name
        self.version = version
        self.summary = summary
        self.description = description
        self.architecturespecific = architecturespecific
        self.homepage = homepage
        self.user_defined_fields = user_defined_fields


ArtifactMetadata = Union[SourceArtifactMetadata, BinaryArtifactMetadata]


class ScannedArtifact:
    def __init__(
        self, *, artifact: IRevisionStatusArtifact, metadata: ArtifactMetadata
    ):
        self.artifact = artifact
        self.metadata = metadata

    @property
    def extra_fields(self) -> Optional[List[Tuple[str, Any]]]:
        """Extra fields to be stored in user_defined_fields.

        Combine fields from the report's properties (specified by the job)
        and the scanned metadata (determined by the scanner).  In cases of
        conflict, the scanner wins.
        """
        fields = {}
        if self.artifact.report.properties is not None:
            fields.update(removeSecurityProxy(self.artifact.report.properties))
        if self.metadata.user_defined_fields is not None:
            fields.update(self.metadata.user_defined_fields)
        if fields:
            return sorted(fields.items())
        else:
            return None


@implementer(ICIBuildUploadJob)
@provider(ICIBuildUploadJobSource)
class CIBuildUploadJob(ArchiveJobDerived):
    class_job_type = ArchiveJobType.CI_BUILD_UPLOAD

    user_error_types = (ScanException,)
    retry_error_types = (LibrarianServerError,)
    max_retries = 3

    config = config.ICIBuildUploadJobSource

    # XXX cjwatson 2022-06-10: There doesn't seem to be a very clear
    # conceptual distinction between BinaryPackageFormat and
    # BinaryPackageFileType, but we end up having to add entries to both for
    # each new package type because they're used in different database
    # columns.  Try to minimize the hassle involved in this by maintaining a
    # mapping here of all the formats we're interested in.
    filetype_by_format = {
        BinaryPackageFormat.WHL: BinaryPackageFileType.WHL,
        BinaryPackageFormat.CONDA_V1: BinaryPackageFileType.CONDA_V1,
        BinaryPackageFormat.CONDA_V2: BinaryPackageFileType.CONDA_V2,
        BinaryPackageFormat.GENERIC: BinaryPackageFileType.GENERIC,
    }

    # We're only interested in uploading certain kinds of packages to
    # certain kinds of archives.
    format_by_repository_format = {
        ArchiveRepositoryFormat.DEBIAN: {
            BinaryPackageFormat.DEB,
            BinaryPackageFormat.UDEB,
            BinaryPackageFormat.DDEB,
        },
        ArchiveRepositoryFormat.PYTHON: {
            SourcePackageFileType.SDIST,
            BinaryPackageFormat.WHL,
        },
        ArchiveRepositoryFormat.CONDA: {
            BinaryPackageFormat.CONDA_V1,
            BinaryPackageFormat.CONDA_V2,
        },
        ArchiveRepositoryFormat.GO_PROXY: {
            SourcePackageFileType.GO_MODULE_INFO,
            SourcePackageFileType.GO_MODULE_MOD,
            SourcePackageFileType.GO_MODULE_ZIP,
        },
        ArchiveRepositoryFormat.GENERIC: {
            SourcePackageFileType.GENERIC,
            BinaryPackageFormat.GENERIC,
        },
    }

    @classmethod
    def create(
        cls,
        ci_build,
        requester,
        target_archive,
        target_distroseries,
        target_pocket,
        target_channel=None,
    ):
        """See `ICIBuildUploadJobSource`."""
        metadata = {
            "ci_build_id": ci_build.id,
            "target_distroseries_id": target_distroseries.id,
            "target_pocket": target_pocket.title,
            "target_channel": target_channel,
        }
        derived = super().create(target_archive, metadata)
        derived.job.requester = requester
        return derived

    def __repr__(self):
        """Returns an informative representation of the job."""
        parts = [
            "%s to upload %r to %s %s"
            % (
                self.__class__.__name__,
                self.ci_build,
                self.archive.reference,
                self.target_distroseries.getSuite(self.target_pocket),
            ),
        ]
        if self.target_channel is not None:
            parts.append(" {%s}" % self.target_channel)
        return "<%s>" % "".join(parts)

    def getOopsVars(self):
        vars = super().getOopsVars()
        vars.extend(
            [
                (key, self.metadata[key])
                for key in (
                    "ci_build_id",
                    "target_distroseries_id",
                    "target_pocket",
                    "target_channel",
                )
            ]
        )
        return vars

    def getOperationDescription(self):
        """See `IRunnableJob`."""
        return "uploading %s to %s" % (
            self.ci_build.title,
            self.archive.reference,
        )

    def getErrorRecipients(self):
        return [format_address_for_person(self.requester)]

    @property
    def ci_build(self):
        return getUtility(ICIBuildSet).getByID(self.metadata["ci_build_id"])

    @property
    def target_distroseries(self):
        return getUtility(IDistroSeriesSet).get(
            self.metadata["target_distroseries_id"]
        )

    @property
    def target_pocket(self):
        return PackagePublishingPocket.getTermByToken(
            self.metadata["target_pocket"]
        ).value

    @property
    def target_channel(self):
        return self.metadata["target_channel"]

    # XXX cjwatson 2023-02-13: The patterns in the various _scan* methods here
    # must currently be kept in sync with the patterns matched by
    # lp.archivepublisher.artifactory.ArtifactoryPool.getArtifactPatterns.
    # This is cumbersome, and it would be helpful if we could somehow ensure
    # that there's no drift.

    def _scanWheel(
        self, report: IRevisionStatusReport, paths: Iterable[Path]
    ) -> Dict[str, ArtifactMetadata]:
        all_metadata = {}
        for path in paths:
            filename = str(os.path.basename(os.fsdecode(path)))
            if not filename.endswith(".whl"):
                continue
            try:
                _, _, _, tags = parse_wheel_filename(str(filename))
                platforms = [tag.platform for tag in set(tags)]
                wheel = Wheel(str(path))
            except Exception as e:
                logger.warning(
                    "Failed to scan %s as a Python wheel: %s", path.name, e
                )
                continue
            logger.info("%s is a Python wheel", path.name)
            all_metadata[path.name] = BinaryArtifactMetadata(
                format=BinaryPackageFormat.WHL,
                name=wheel.name,
                version=wheel.version,
                summary=wheel.summary or "",
                description=wheel.description or "",
                architecturespecific="any" not in platforms,  # != platform,
                homepage=wheel.home_page or "",
            )
        return all_metadata

    def _scanSDist(
        self, report: IRevisionStatusReport, paths: Iterable[Path]
    ) -> Dict[str, ArtifactMetadata]:
        all_metadata = {}
        for path in paths:
            if not path.name.endswith((".tar.gz", ".zip")):
                continue
            try:
                sdist = SDist(str(path))
            except Exception as e:
                logger.warning(
                    "Failed to scan %s as a Python sdist: %s", path.name, e
                )
                continue
            logger.info("%s is a Python sdist", path.name)
            all_metadata[path.name] = SourceArtifactMetadata(
                format=SourcePackageFileType.SDIST,
                name=sdist.name,
                version=sdist.version,
                # Arrange for this to be explicitly stored in the database.
                # The source package name will be taken from the namespace
                # of the originating repository, but we need the true
                # upstream package name as well in order to publish files to
                # the correct location.
                user_defined_fields=[("package-name", sdist.name)],
            )
        return all_metadata

    def _scanCondaMetadata(
        self,
        format: BinaryPackageFormat,
        index: Dict[Any, Any],
        about: Dict[Any, Any],
    ) -> Optional[BinaryArtifactMetadata]:
        return BinaryArtifactMetadata(
            format=format,
            name=index["name"],
            version=index["version"],
            summary=about.get("summary", ""),
            description=about.get("description", ""),
            architecturespecific=index["platform"] is not None,
            homepage=about.get("home", ""),
            # We should perhaps model this explicitly since it's used by the
            # publisher, but this gives us an easy way to pass this through
            # without needing to add a column to a large table that's only
            # relevant to a tiny minority of rows.
            user_defined_fields=[("subdir", index["subdir"])],
        )

    def _scanCondaV1(
        self, report: IRevisionStatusReport, paths: Iterable[Path]
    ) -> Dict[str, ArtifactMetadata]:
        all_metadata = {}
        for path in paths:
            if not path.name.endswith(".tar.bz2"):
                continue
            try:
                with tarfile.open(str(path)) as tar:
                    index = json.loads(
                        tar.extractfile("info/index.json").read().decode()
                    )
                    about = json.loads(
                        tar.extractfile("info/about.json").read().decode()
                    )
            except Exception as e:
                logger.warning(
                    "Failed to scan %s as a Conda v1 package: %s", path.name, e
                )
                continue
            logger.info("%s is a Conda v1 package", path.name)
            all_metadata[path.name] = self._scanCondaMetadata(
                BinaryPackageFormat.CONDA_V1, index, about
            )
        return all_metadata

    def _scanCondaV2(
        self, report: IRevisionStatusReport, paths: Iterable[Path]
    ) -> Dict[str, ArtifactMetadata]:
        all_metadata = {}
        for path in paths:
            if not path.name.endswith(".conda"):
                continue
            try:
                with zipfile.ZipFile(str(path)) as zipf:
                    info = io.BytesIO()
                    with zipf.open("info-%s.tar.zst" % path.stem) as raw_info:
                        zstandard.ZstdDecompressor().copy_stream(
                            raw_info, info
                        )
                    info.seek(0)
                    with tarfile.open(fileobj=info) as tar:
                        index = json.loads(
                            tar.extractfile("info/index.json").read().decode()
                        )
                        about = json.loads(
                            tar.extractfile("info/about.json").read().decode()
                        )
            except Exception as e:
                logger.warning(
                    "Failed to scan %s as a Conda v2 package: %s", path.name, e
                )
                continue
            logger.info("%s is a Conda v2 package", path.name)
            all_metadata[path.name] = self._scanCondaMetadata(
                BinaryPackageFormat.CONDA_V2, index, about
            )
        return all_metadata

    def _scanGoMod(
        self, report: IRevisionStatusReport, paths: Iterable[Path]
    ) -> Dict[str, ArtifactMetadata]:
        all_metadata = {}
        for path in paths:
            if not path.name.endswith(".mod"):
                continue
            info_path = path.parent / ("%s.info" % path.stem)
            if not info_path.is_file():
                logger.warning("%s has no corresponding .info file", path.name)
                continue
            zip_path = path.parent / ("%s.zip" % path.stem)
            if not zip_path.is_file():
                logger.warning("%s has no corresponding .zip file", path.name)
                continue
            with open(str(info_path)) as info_file:
                try:
                    version = json.load(info_file)["Version"]
                except Exception as e:
                    logger.warning(
                        "Failed to load Go module version from %s: %s",
                        info_path.name,
                        e,
                    )
            try:
                with open(str(path)) as mod_file:
                    module_path = parse_go_mod(mod_file.read())
            except GoModParserException as e:
                logger.warning(
                    "Failed to scan %s as a Go module: %s", path.name, e
                )
                continue
            logger.info(
                "(%s, %s, %s) are a Go module",
                info_path.name,
                path.name,
                zip_path.name,
            )
            metadata_kwargs = {
                "name": module_path,
                "version": version,
                # We should perhaps model this explicitly since it's used by
                # the publisher, but this gives us an easy way to pass this
                # through without needing to add a column to a large table
                # that's only relevant to a tiny minority of rows.
                "user_defined_fields": [("module-path", module_path)],
            }
            all_metadata[info_path.name] = SourceArtifactMetadata(
                format=SourcePackageFileType.GO_MODULE_INFO,
                **metadata_kwargs,
            )
            all_metadata[path.name] = SourceArtifactMetadata(
                format=SourcePackageFileType.GO_MODULE_MOD,
                **metadata_kwargs,
            )
            all_metadata[zip_path.name] = SourceArtifactMetadata(
                format=SourcePackageFileType.GO_MODULE_ZIP,
                **metadata_kwargs,
            )
        return all_metadata

    def _scanGeneric(
        self, report: IRevisionStatusReport, paths: Iterable[Path]
    ) -> Dict[str, ArtifactMetadata]:
        properties = report.properties
        if (
            properties is None
            or "name" not in properties
            or "version" not in properties
        ):
            return {}

        all_metadata = {}
        for path in paths:
            if properties.get("source", False):
                logger.info("%s is a generic source artifact", path.name)
                all_metadata[path.name] = SourceArtifactMetadata(
                    format=SourcePackageFileType.GENERIC,
                    name=properties["name"],
                    version=properties["version"],
                )
            else:
                logger.info("%s is a generic binary artifact", path.name)
                all_metadata[path.name] = BinaryArtifactMetadata(
                    format=BinaryPackageFormat.GENERIC,
                    name=properties["name"],
                    version=properties["version"],
                    summary="",
                    description="",
                    architecturespecific=True,
                    homepage="",
                )
        return all_metadata

    def _scanFiles(
        self, report: IRevisionStatusReport, directory: Path
    ) -> Dict[str, ArtifactMetadata]:
        scanners = (
            self._scanWheel,
            self._scanSDist,
            self._scanCondaV1,
            self._scanCondaV2,
            self._scanGoMod,
            self._scanGeneric,
        )
        paths = [directory / child for child in directory.iterdir()]
        all_metadata = OrderedDict()
        for scanner in scanners:
            for name, metadata in scanner(report, paths).items():
                all_metadata[name] = metadata
            paths = [path for path in paths if path.name not in all_metadata]
        return all_metadata

    def _scanArtifacts(
        self, artifacts: Iterable[IRevisionStatusArtifact]
    ) -> List[ScannedArtifact]:
        """Scan an iterable of `RevisionStatusArtifact`s for metadata.

        Skips log artifacts, artifacts we don't understand, and artifacts
        not relevant to the target archive's repository format.

        Returns a list of `ScannedArtifact`s containing metadata for
        relevant artifacts.
        """
        allowed_formats = self.format_by_repository_format.get(
            self.archive.repository_format, set()
        )
        scanned = []
        with tempfile.TemporaryDirectory(prefix="ci-build-copy-job") as tmpdir:
            tmpdirpath = Path(tmpdir)
            artifact_by_name = {}
            report_by_id = {}
            for artifact in artifacts:
                if artifact.artifact_type == RevisionStatusArtifactType.LOG:
                    continue
                name = artifact.library_file.filename
                contents = tmpdirpath / str(artifact.report.id) / name
                if not contents.parent.exists():
                    contents.parent.mkdir()
                artifact.library_file.open()
                copy_and_close(
                    artifact.library_file, open(str(contents), "wb")
                )
                artifact_by_name[name] = artifact
                report_by_id[str(artifact.report.id)] = artifact.report
            all_metadata = {}
            for report_path in sorted(tmpdirpath.iterdir()):
                report = report_by_id[report_path.name]
                all_metadata.update(self._scanFiles(report, report_path))
            for name, metadata in all_metadata.items():
                if metadata.format not in allowed_formats:
                    logger.info(
                        "Skipping %s (not relevant to %s archives)",
                        name,
                        self.archive.repository_format,
                    )
                    continue
                scanned.append(
                    ScannedArtifact(
                        artifact=artifact_by_name[name], metadata=metadata
                    )
                )
        return scanned

    def _uploadSources(self, scanned: Sequence[ScannedArtifact]) -> None:
        """Upload sources from an sequence of `ScannedArtifact`s."""
        # Launchpad's data model generally assumes that a single source is
        # associated with multiple binaries.  However, a source package
        # release can have multiple (or indeed no) files attached to it, so
        # we make use of that if necessary.
        releases = {
            release.sourcepackagename: release
            for release in self.ci_build.sourcepackages
        }
        distroseries = self.ci_build.distro_arch_series.distroseries
        build_target = self.ci_build.git_repository.target
        spr = releases.get(build_target.sourcepackagename)
        if spr is None:
            # Arbitrarily pick the first scanned source artifact to provide
            # additional metadata, falling back to the first non-source
            # artifact if there are no source artifacts.  (This may need
            # refinement in future.)
            for scanned_artifact in scanned:
                metadata = scanned_artifact.metadata
                if isinstance(metadata, SourceArtifactMetadata):
                    first_scanned_artifact = scanned_artifact
                    break
            else:
                first_scanned_artifact = scanned[0]
            spr = self.ci_build.createSourcePackageRelease(
                distroseries=distroseries,
                sourcepackagename=build_target.sourcepackagename,
                # We don't have a good concept of source version here, but
                # the data model demands one.
                version=first_scanned_artifact.metadata.version,
                creator=self.requester,
                archive=self.archive,
                user_defined_fields=first_scanned_artifact.extra_fields,
            )
        for scanned_artifact in scanned:
            metadata = scanned_artifact.metadata
            if not isinstance(metadata, SourceArtifactMetadata):
                continue
            library_file = scanned_artifact.artifact.library_file
            logger.info(
                "Uploading %s to %s %s (%s)",
                library_file.filename,
                self.archive.reference,
                self.target_distroseries.getSuite(self.target_pocket),
                self.target_channel,
            )
            for sprf in spr.files:
                if (
                    sprf.libraryfile == library_file
                    and sprf.filetype == metadata.format
                ):
                    break
            else:
                spr.addFile(library_file, filetype=metadata.format)
        getUtility(IPublishingSet).newSourcePublication(
            archive=self.archive,
            sourcepackagerelease=spr,
            distroseries=self.target_distroseries,
            pocket=self.target_pocket,
            creator=self.requester,
            channel=self.target_channel,
        )

    def _uploadBinaries(self, scanned: Iterable[ScannedArtifact]) -> None:
        """Upload binaries from an iterable of `ScannedArtifact`s."""
        releases = {
            (release.binarypackagename, release.binpackageformat): release
            for release in self.ci_build.binarypackages
        }
        binaries = OrderedDict()
        for scanned_artifact in scanned:
            metadata = scanned_artifact.metadata
            if not isinstance(metadata, BinaryArtifactMetadata):
                continue
            artifact = scanned_artifact.artifact
            library_file = artifact.library_file
            if artifact.report.distro_arch_series is not None:
                build_distroseries = (
                    artifact.report.distro_arch_series.distroseries
                )
                # It might seem that we ought to check that the build job's
                # series is equal to the target series, but in practice what
                # we're doing is taking the output of build jobs that were
                # run on (e.g.) Ubuntu focal and uploading them to (e.g.)
                # <private distribution> focal, so we need to check parent
                # series links as well.
                if (
                    build_distroseries != self.target_distroseries
                    and build_distroseries
                    not in self.target_distroseries.getParentSeries()
                ):
                    logger.info(
                        "Skipping %s (built for %s, not %s)",
                        library_file.filename,
                        build_distroseries.name,
                        self.target_distroseries.name,
                    )
                    continue
            logger.info(
                "Uploading %s to %s %s (%s)",
                library_file.filename,
                self.archive.reference,
                self.target_distroseries.getSuite(self.target_pocket),
                self.target_channel,
            )
            binarypackagename = getUtility(IBinaryPackageNameSet).ensure(
                metadata.name
            )
            filetype = self.filetype_by_format[metadata.format]
            bpr = releases.get((binarypackagename, metadata.format))
            if bpr is None:
                bpr = self.ci_build.createBinaryPackageRelease(
                    binarypackagename=binarypackagename,
                    version=metadata.version,
                    summary=metadata.summary,
                    description=metadata.description,
                    binpackageformat=metadata.format,
                    architecturespecific=metadata.architecturespecific,
                    homepage=metadata.homepage,
                    user_defined_fields=scanned_artifact.extra_fields,
                )
            for bpf in bpr.files:
                if (
                    bpf.libraryfile == library_file
                    and bpf.filetype == filetype
                ):
                    break
            else:
                bpr.addFile(library_file, filetype=filetype)
            # The publishBinaries interface was designed for .debs,
            # which need extra per-binary "override" information
            # (component, etc.).  None of this is relevant here.
            binaries[bpr] = (None, None, None, None)
        getUtility(IPublishingSet).publishBinaries(
            self.archive,
            self.target_distroseries,
            self.target_pocket,
            binaries,
            channel=self.target_channel,
        )

    def run(self) -> None:
        """See `IRunnableJob`."""
        build_target = self.ci_build.git_repository.target
        if not IDistributionSourcePackage.providedBy(build_target):
            # This should be caught by `Archive.uploadCIBuild`, but check it
            # here as well just in case.
            logger.warning(
                "Source CI build is for %s, which is not a package",
                repr(build_target),
            )
            return
        artifacts = getUtility(IRevisionStatusArtifactSet).findByCIBuild(
            self.ci_build
        )
        scanned = self._scanArtifacts(artifacts)
        if scanned:
            self._uploadSources(scanned)
            self._uploadBinaries(scanned)
        else:
            names = [artifact.library_file.filename for artifact in artifacts]
            raise ScanException(
                "Could not find any usable files in %s" % names
            )
