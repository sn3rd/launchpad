# Copyright 2010-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import os.path
import shutil
from pathlib import Path

import transaction
from debian.deb822 import Changes
from fixtures import FakeLogger, MockPatch, MockPatchObject, TempDir
from testtools.matchers import (
    ContainsDict,
    Equals,
    Is,
    MatchesDict,
    MatchesSetwise,
    MatchesStructure,
)

from lp.code.enums import RevisionStatusArtifactType
from lp.code.model.revisionstatus import RevisionStatusArtifact
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.sourcepackage import (
    SourcePackageFileType,
    SourcePackageType,
)
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.job.tests import block_on_job, pop_remote_notifications
from lp.services.librarian.interfaces.client import LibrarianServerError
from lp.services.mail.sendmail import format_address_for_person
from lp.soyuz.enums import (
    ArchiveJobType,
    ArchiveRepositoryFormat,
    BinaryPackageFileType,
    BinaryPackageFormat,
    PackageUploadStatus,
)
from lp.soyuz.model.archivejob import (
    ArchiveJob,
    ArchiveJobDerived,
    CIBuildUploadJob,
    PackageUploadNotificationJob,
    ScanException,
)
from lp.soyuz.tests import datadir
from lp.testing import TestCaseWithFactory, admin_logged_in, person_logged_in
from lp.testing.dbuser import dbuser
from lp.testing.layers import (
    CeleryJobLayer,
    LaunchpadZopelessLayer,
    ZopelessDatabaseLayer,
)
from lp.testing.mail_helpers import pop_notifications


class TestArchiveJob(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_instantiate(self):
        # ArchiveJob.__init__() instantiates a ArchiveJob instance.
        archive = self.factory.makeArchive()

        metadata = ("some", "arbitrary", "metadata")
        archive_job = ArchiveJob(
            archive, ArchiveJobType.PACKAGE_UPLOAD_NOTIFICATION, metadata
        )

        self.assertEqual(archive, archive_job.archive)
        self.assertEqual(
            ArchiveJobType.PACKAGE_UPLOAD_NOTIFICATION, archive_job.job_type
        )

        # When we actually access the ArchiveJob's metadata it gets
        # deserialized from JSON, so the representation returned by
        # archive_job.metadata will be different from what we originally
        # passed in.
        metadata_expected = ("some", "arbitrary", "metadata")
        self.assertEqual(metadata_expected, archive_job.metadata)


class TestArchiveJobDerived(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def test_create_explodes(self):
        # ArchiveJobDerived.create() will blow up because it needs to be
        # subclassed to work properly.
        archive = self.factory.makeArchive()
        self.assertRaises(AttributeError, ArchiveJobDerived.create, archive)


class TestPackageUploadNotificationJob(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def test_getOopsVars(self):
        upload = self.factory.makePackageUpload(
            status=PackageUploadStatus.ACCEPTED
        )
        job = PackageUploadNotificationJob.create(
            upload, summary_text="Fake summary"
        )
        expected = [
            ("job_id", job.context.job.id),
            ("archive_id", upload.archive.id),
            ("archive_job_id", job.context.id),
            ("archive_job_type", "Package upload notification"),
            ("packageupload_id", upload.id),
            ("packageupload_status", "Accepted"),
            ("summary_text", "Fake summary"),
        ]
        self.assertEqual(expected, job.getOopsVars())

    def test_metadata(self):
        upload = self.factory.makePackageUpload(
            status=PackageUploadStatus.ACCEPTED
        )
        job = PackageUploadNotificationJob.create(
            upload, summary_text="Fake summary"
        )
        expected = {
            "packageupload_id": upload.id,
            "packageupload_status": "Accepted",
            "summary_text": "Fake summary",
        }
        self.assertEqual(expected, job.metadata)
        self.assertEqual(upload, job.packageupload)
        self.assertEqual(
            PackageUploadStatus.ACCEPTED, job.packageupload_status
        )
        self.assertEqual("Fake summary", job.summary_text)

    def test_run(self):
        # Running a job produces a notification.  Detailed tests of which
        # notifications go to whom live in the PackageUpload and
        # PackageUploadMailer tests.
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        changes = Changes({"Changed-By": format_address_for_person(creator)})
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            changes_file_content=changes.dump().encode("UTF-8"),
        )
        upload.addSource(self.factory.makeSourcePackageRelease())
        self.factory.makeComponentSelection(
            upload.distroseries, upload.sourcepackagerelease.component
        )
        upload.setAccepted()
        job = PackageUploadNotificationJob.create(
            upload, summary_text="Fake summary"
        )
        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()
        [email] = pop_notifications()
        self.assertEqual(format_address_for_person(creator), email["To"])
        self.assertIn("(Accepted)", email["Subject"])
        self.assertIn("Fake summary", email.get_payload()[0].get_payload())


class TestCIBuildUploadJob(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def makeCIBuild(self, distribution, **kwargs):
        # CIBuilds must be in a package namespace in order to be uploaded to
        # an archive.
        dsp = self.factory.makeDistributionSourcePackage(
            distribution=distribution
        )
        repository = self.factory.makeGitRepository(target=dsp)
        return self.factory.makeCIBuild(git_repository=repository, **kwargs)

    def test_repr_no_channel(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
        )
        self.assertEqual(
            "<CIBuildUploadJob to upload %r to %s %s>"
            % (build, archive.reference, distroseries.name),
            repr(job),
        )

    def test_repr_channel(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        self.assertEqual(
            "<CIBuildUploadJob to upload %r to %s %s {edge}>"
            % (build, archive.reference, distroseries.name),
            repr(job),
        )

    def test_getOopsVars(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        expected = [
            ("job_id", job.context.job.id),
            ("archive_id", archive.id),
            ("archive_job_id", job.context.id),
            ("archive_job_type", "CI build upload"),
            ("ci_build_id", build.id),
            ("target_distroseries_id", distroseries.id),
            ("target_pocket", "Release"),
            ("target_channel", "edge"),
        ]
        self.assertEqual(expected, job.getOopsVars())

    def test_metadata(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        expected = {
            "ci_build_id": build.id,
            "target_distroseries_id": distroseries.id,
            "target_pocket": "Release",
            "target_channel": "edge",
        }
        self.assertEqual(expected, job.metadata)
        self.assertEqual(build, job.ci_build)
        self.assertEqual(distroseries, job.target_distroseries)
        self.assertEqual(PackagePublishingPocket.RELEASE, job.target_pocket)
        self.assertEqual("edge", job.target_channel)

    def test__scanFiles_wheel_indep(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path("wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.WHL),
                        name=Equals("wheel-indep"),
                        version=Equals("0.0.1"),
                        summary=Equals("Example description"),
                        description=Equals("Example long description\n"),
                        architecturespecific=Is(False),
                        homepage=Equals(""),
                    ),
                }
            ),
        )

    def test__scanFiles_wheel_arch(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path(
            "wheel-arch/dist/wheel_arch-0.0.1-cp310-cp310-linux_x86_64.whl"
        )
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.WHL),
                        name=Equals("wheel-arch"),
                        version=Equals("0.0.1"),
                        summary=Equals("Example description"),
                        description=Equals("Example long description\n"),
                        architecturespecific=Is(True),
                        homepage=Equals("http://example.com/"),
                    ),
                }
            ),
        )

    def test__scanFiles_wheel_no_description(self):
        """Ensure scanning a wheel without a description produces metadata with
        empty string instead of None"""
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path(
            "wheel-no-description/dist/"
            "wheel_no_description-0.0.1-py3-none-any.whl"
        )
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)

        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.WHL),
                        name=Equals("wheel-no-description"),
                        version=Equals("0.0.1"),
                        summary=Equals(""),
                        description=Equals(""),
                        architecturespecific=Is(False),
                        homepage=Equals(""),
                    ),
                }
            ),
        )

    def test__scanFiles_sdist(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path("wheel-arch/dist/wheel-arch-0.0.1.tar.gz")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure.byEquality(
                        format=SourcePackageFileType.SDIST,
                        name="wheel-arch",
                        version="0.0.1",
                        user_defined_fields=[("package-name", "wheel-arch")],
                    ),
                }
            ),
        )

    def test__scanFiles_conda_indep(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path("conda-indep/dist/noarch/conda-indep-0.1-0.tar.bz2")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.CONDA_V1),
                        name=Equals("conda-indep"),
                        version=Equals("0.1"),
                        summary=Equals("Example summary"),
                        description=Equals("Example description"),
                        architecturespecific=Is(False),
                        homepage=Equals(""),
                        user_defined_fields=Equals([("subdir", "noarch")]),
                    ),
                }
            ),
        )

    def test__scanFiles_conda_arch(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path("conda-arch/dist/linux-64/conda-arch-0.1-0.tar.bz2")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.CONDA_V1),
                        name=Equals("conda-arch"),
                        version=Equals("0.1"),
                        summary=Equals("Example summary"),
                        description=Equals("Example description"),
                        architecturespecific=Is(True),
                        homepage=Equals("http://example.com/"),
                        user_defined_fields=Equals([("subdir", "linux-64")]),
                    ),
                }
            ),
        )

    def test__scanFiles_conda_v2_indep(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path("conda-v2-indep/dist/noarch/conda-v2-indep-0.1-0.conda")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.CONDA_V2),
                        name=Equals("conda-v2-indep"),
                        version=Equals("0.1"),
                        summary=Equals("Example summary"),
                        description=Equals("Example description"),
                        architecturespecific=Is(False),
                        homepage=Equals(""),
                        user_defined_fields=Equals([("subdir", "noarch")]),
                    ),
                }
            ),
        )

    def test__scanFiles_conda_v2_arch(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        path = Path("conda-v2-arch/dist/linux-64/conda-v2-arch-0.1-0.conda")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    path.name: MatchesStructure(
                        format=Equals(BinaryPackageFormat.CONDA_V2),
                        name=Equals("conda-v2-arch"),
                        version=Equals("0.1"),
                        summary=Equals("Example summary"),
                        description=Equals("Example description"),
                        architecturespecific=Is(True),
                        homepage=Equals("http://example.com/"),
                        user_defined_fields=Equals([("subdir", "linux-64")]),
                    ),
                }
            ),
        )

    def test__scanFiles_go(self):
        self.useFixture(FakeLogger())
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(ci_build=build)
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        info_path = Path("go/dist/v0.0.1.info")
        mod_path = Path("go/dist/v0.0.1.mod")
        zip_path = Path("go/dist/v0.0.1.zip")
        tmpdir = Path(self.useFixture(TempDir()).path)
        shutil.copy2(datadir(str(info_path)), str(tmpdir))
        shutil.copy2(datadir(str(mod_path)), str(tmpdir))
        shutil.copy2(datadir(str(zip_path)), str(tmpdir))
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    info_path.name: MatchesStructure.byEquality(
                        format=SourcePackageFileType.GO_MODULE_INFO,
                        name="example.com/t",
                        version="v0.0.1",
                        user_defined_fields=[("module-path", "example.com/t")],
                    ),
                    mod_path.name: MatchesStructure.byEquality(
                        format=SourcePackageFileType.GO_MODULE_MOD,
                        name="example.com/t",
                        version="v0.0.1",
                        user_defined_fields=[("module-path", "example.com/t")],
                    ),
                    zip_path.name: MatchesStructure.byEquality(
                        format=SourcePackageFileType.GO_MODULE_ZIP,
                        name="example.com/t",
                        version="v0.0.1",
                        user_defined_fields=[("module-path", "example.com/t")],
                    ),
                }
            ),
        )

    def test__scanFiles_generic_source(self):
        self.useFixture(FakeLogger())
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(
            title="build-source", ci_build=build
        )
        report.update(
            properties={
                "name": "foo",
                "version": "1.0",
                "source": True,
            }
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        tmpdir = Path(self.useFixture(TempDir()).path)
        (tmpdir / "foo-1.0.tar.gz").write_bytes(b"source artifact")
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    "foo-1.0.tar.gz": MatchesStructure.byEquality(
                        format=SourcePackageFileType.GENERIC,
                        name="foo",
                        version="1.0",
                    )
                }
            ),
        )

    def test__scanFiles_generic_binary(self):
        archive = self.factory.makeArchive()
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        build = self.makeCIBuild(archive.distribution)
        report = self.factory.makeRevisionStatusReport(
            title="build-binary", ci_build=build
        )
        report.update(properties={"name": "foo", "version": "1.0"})
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        tmpdir = Path(self.useFixture(TempDir()).path)
        (tmpdir / "test-binary").write_bytes(b"binary artifact")
        all_metadata = job._scanFiles(report, tmpdir)
        self.assertThat(
            all_metadata,
            MatchesDict(
                {
                    "test-binary": MatchesStructure.byEquality(
                        format=BinaryPackageFormat.GENERIC,
                        name="foo",
                        version="1.0",
                        summary="",
                        description="",
                        architecturespecific=True,
                        homepage="",
                    )
                }
            ),
        )

    def test_run_indep(self):
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        report.setLog(b"log data")
        path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(path), mode="rb") as f:
            report.attach(name=os.path.basename(path), data=f.read())
        artifact = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .one()
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        version=Equals("0.0.1"),
                        format=Equals(SourcePackageType.CI_BUILD),
                        architecturehintlist=Equals(""),
                        creator=Equals(build.git_repository.owner),
                        files=Equals([]),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                )
            ),
        )
        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                *(
                    MatchesStructure(
                        binarypackagename=MatchesStructure.byEquality(
                            name="wheel-indep"
                        ),
                        binarypackagerelease=MatchesStructure(
                            ci_build=Equals(build),
                            binarypackagename=MatchesStructure.byEquality(
                                name="wheel-indep"
                            ),
                            version=Equals("0.0.1"),
                            summary=Equals("Example description"),
                            description=Equals("Example long description\n"),
                            binpackageformat=Equals(BinaryPackageFormat.WHL),
                            architecturespecific=Is(False),
                            homepage=Equals(""),
                            files=MatchesSetwise(
                                MatchesStructure.byEquality(
                                    libraryfile=artifact.library_file,
                                    filetype=BinaryPackageFileType.WHL,
                                )
                            ),
                        ),
                        binarypackageformat=Equals(BinaryPackageFormat.WHL),
                        distroarchseries=Equals(das),
                    )
                    for das in dases
                )
            ),
        )

    def test_run_arch(self):
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        report.setLog(b"log data")
        sdist_path = "wheel-arch/dist/wheel-arch-0.0.1.tar.gz"
        wheel_path = (
            "wheel-arch/dist/wheel_arch-0.0.1-cp310-cp310-linux_x86_64.whl"
        )
        with open(datadir(sdist_path), mode="rb") as f:
            report.attach(name=os.path.basename(sdist_path), data=f.read())
        with open(datadir(wheel_path), mode="rb") as f:
            report.attach(name=os.path.basename(wheel_path), data=f.read())
        artifacts = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .order_by("id")
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        version=Equals("0.0.1"),
                        format=Equals(SourcePackageType.CI_BUILD),
                        architecturehintlist=Equals(""),
                        creator=Equals(build.git_repository.owner),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[0].library_file,
                                filetype=SourcePackageFileType.SDIST,
                            )
                        ),
                        user_defined_fields=Equals(
                            [["package-name", "wheel-arch"]]
                        ),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                )
            ),
        )
        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                MatchesStructure(
                    binarypackagename=MatchesStructure.byEquality(
                        name="wheel-arch"
                    ),
                    binarypackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        binarypackagename=MatchesStructure.byEquality(
                            name="wheel-arch"
                        ),
                        version=Equals("0.0.1"),
                        summary=Equals("Example description"),
                        description=Equals("Example long description\n"),
                        binpackageformat=Equals(BinaryPackageFormat.WHL),
                        architecturespecific=Is(True),
                        homepage=Equals("http://example.com/"),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[1].library_file,
                                filetype=BinaryPackageFileType.WHL,
                            )
                        ),
                    ),
                    binarypackageformat=Equals(BinaryPackageFormat.WHL),
                    distroarchseries=Equals(dases[0]),
                )
            ),
        )

    def test_run_conda(self):
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.CONDA
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        report.setLog(b"log data")
        path = "conda-arch/dist/linux-64/conda-arch-0.1-0.tar.bz2"
        with open(datadir(path), mode="rb") as f:
            report.attach(name=os.path.basename(path), data=f.read())
        artifact = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .one()
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        version=Equals("0.1"),
                        format=Equals(SourcePackageType.CI_BUILD),
                        architecturehintlist=Equals(""),
                        creator=Equals(build.git_repository.owner),
                        files=Equals([]),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                )
            ),
        )
        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                MatchesStructure(
                    binarypackagename=MatchesStructure.byEquality(
                        name="conda-arch"
                    ),
                    binarypackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        binarypackagename=MatchesStructure.byEquality(
                            name="conda-arch"
                        ),
                        version=Equals("0.1"),
                        summary=Equals("Example summary"),
                        description=Equals("Example description"),
                        binpackageformat=Equals(BinaryPackageFormat.CONDA_V1),
                        architecturespecific=Is(True),
                        homepage=Equals("http://example.com/"),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifact.library_file,
                                filetype=BinaryPackageFileType.CONDA_V1,
                            )
                        ),
                    ),
                    binarypackageformat=Equals(BinaryPackageFormat.CONDA_V1),
                    distroarchseries=Equals(dases[0]),
                )
            ),
        )

    def test_run_conda_v2(self):
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.CONDA
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        report.setLog(b"log data")
        path = "conda-v2-arch/dist/linux-64/conda-v2-arch-0.1-0.conda"
        with open(datadir(path), mode="rb") as f:
            report.attach(name=os.path.basename(path), data=f.read())
        artifact = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .one()
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        version=Equals("0.1"),
                        format=Equals(SourcePackageType.CI_BUILD),
                        architecturehintlist=Equals(""),
                        creator=Equals(build.git_repository.owner),
                        files=Equals([]),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                )
            ),
        )
        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                MatchesStructure(
                    binarypackagename=MatchesStructure.byEquality(
                        name="conda-v2-arch"
                    ),
                    binarypackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        binarypackagename=MatchesStructure.byEquality(
                            name="conda-v2-arch"
                        ),
                        version=Equals("0.1"),
                        summary=Equals("Example summary"),
                        description=Equals("Example description"),
                        binpackageformat=Equals(BinaryPackageFormat.CONDA_V2),
                        architecturespecific=Is(True),
                        homepage=Equals("http://example.com/"),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifact.library_file,
                                filetype=BinaryPackageFileType.CONDA_V2,
                            )
                        ),
                    ),
                    binarypackageformat=Equals(BinaryPackageFormat.CONDA_V2),
                    distroarchseries=Equals(dases[0]),
                )
            ),
        )

    def test_run_go(self):
        self.useFixture(FakeLogger())
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.GO_PROXY
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        report.setLog(b"log data")
        info_path = "go/dist/v0.0.1.info"
        mod_path = "go/dist/v0.0.1.mod"
        zip_path = "go/dist/v0.0.1.zip"
        for path in (info_path, mod_path, zip_path):
            with open(datadir(path), mode="rb") as f:
                report.attach(name=os.path.basename(path), data=f.read())
        artifacts = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .order_by("id")
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        version=Equals("v0.0.1"),
                        format=Equals(SourcePackageType.CI_BUILD),
                        architecturehintlist=Equals(""),
                        creator=Equals(build.git_repository.owner),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[0].library_file,
                                filetype=SourcePackageFileType.GO_MODULE_INFO,
                            ),
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[1].library_file,
                                filetype=SourcePackageFileType.GO_MODULE_MOD,
                            ),
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[2].library_file,
                                filetype=SourcePackageFileType.GO_MODULE_ZIP,
                            ),
                        ),
                        user_defined_fields=Equals(
                            [["module-path", "example.com/t"]]
                        ),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                )
            ),
        )
        self.assertContentEqual([], archive.getAllPublishedBinaries())

    def test_run_generic(self):
        self.useFixture(FakeLogger())
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.GENERIC
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        source_report = build.getOrCreateRevisionStatusReport("build-source:0")
        source_report.setLog(b"log data")
        source_report.update(
            properties={
                "name": "foo",
                "version": "1.0",
                "source": True,
            }
        )
        source_report.attach(name="foo-1.0.tar.gz", data=b"source artifact")
        binary_report = build.getOrCreateRevisionStatusReport("build-binary:0")
        binary_report.setLog(b"log data")
        binary_report.update(properties={"name": "foo", "version": "1.0"})
        binary_report.attach(name="test-binary", data=b"binary artifact")
        artifacts = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                RevisionStatusArtifact.report_id.is_in(
                    {source_report.id, binary_report.id}
                ),
                RevisionStatusArtifact.artifact_type
                == RevisionStatusArtifactType.BINARY,
            )
            .order_by("id")
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        version=Equals("1.0"),
                        format=Equals(SourcePackageType.CI_BUILD),
                        architecturehintlist=Equals(""),
                        creator=Equals(build.git_repository.owner),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[0].library_file,
                                filetype=SourcePackageFileType.GENERIC,
                            )
                        ),
                        user_defined_fields=MatchesSetwise(
                            Equals(["name", "foo"]),
                            Equals(["version", "1.0"]),
                            Equals(["source", True]),
                        ),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                )
            ),
        )
        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                MatchesStructure(
                    binarypackagename=MatchesStructure.byEquality(name="foo"),
                    binarypackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        binarypackagename=MatchesStructure.byEquality(
                            name="foo"
                        ),
                        version=Equals("1.0"),
                        summary=Equals(""),
                        description=Equals(""),
                        binpackageformat=Equals(BinaryPackageFormat.GENERIC),
                        architecturespecific=Is(True),
                        homepage=Equals(""),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[1].library_file,
                                filetype=BinaryPackageFileType.GENERIC,
                            )
                        ),
                        user_defined_fields=MatchesSetwise(
                            Equals(["name", "foo"]),
                            Equals(["version", "1.0"]),
                        ),
                    ),
                    binarypackageformat=Equals(BinaryPackageFormat.GENERIC),
                    distroarchseries=Equals(dases[0]),
                )
            ),
        )

    def test_run_attaches_properties(self):
        # The upload process attaches properties from the report as
        # `SourcePackageRelease.user_defined_fields` or
        # `BinaryPackageRelease.user_defined_fields` as appropriate.
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        build = self.makeCIBuild(
            archive.distribution, distro_arch_series=dases[0]
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        report.setLog(b"log data")
        report.update(
            properties={
                "license": {"spdx": "MIT"},
                # The sdist scanner sets this key.  Ensure that the scanner
                # wins, so that it can't be confused by oddly-written jobs.
                "package-name": "nonsense",
            }
        )
        sdist_path = "wheel-indep/dist/wheel-indep-0.0.1.tar.gz"
        with open(datadir(sdist_path), mode="rb") as f:
            report.attach(name=os.path.basename(sdist_path), data=f.read())
        wheel_path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(wheel_path), mode="rb") as f:
            report.attach(name=os.path.basename(wheel_path), data=f.read())
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagerelease=MatchesStructure(
                        user_defined_fields=Equals(
                            [
                                ["license", {"spdx": "MIT"}],
                                # The sdist scanner sets this key, and wins.
                                ["package-name", "wheel-indep"],
                            ]
                        ),
                    ),
                )
            ),
        )
        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                *(
                    MatchesStructure(
                        binarypackagerelease=MatchesStructure(
                            user_defined_fields=Equals(
                                [
                                    ["license", {"spdx": "MIT"}],
                                    # The wheel scanner doesn't set this
                                    # key, so the job is allowed to do so.
                                    ["package-name", "nonsense"],
                                ]
                            ),
                        ),
                    )
                    for das in dases
                )
            ),
        )

    def test_existing_source_and_binary_releases(self):
        # A `CIBuildUploadJob` can be run even if the build in question was
        # already uploaded somewhere, and in that case may add publications
        # in other locations for the same package.
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        build = self.makeCIBuild(archive.distribution, distro_arch_series=das)
        report = build.getOrCreateRevisionStatusReport("build:0")
        sdist_path = "wheel-indep/dist/wheel-indep-0.0.1.tar.gz"
        with open(datadir(sdist_path), mode="rb") as f:
            report.attach(name=os.path.basename(sdist_path), data=f.read())
        wheel_path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(wheel_path), mode="rb") as f:
            report.attach(name=os.path.basename(wheel_path), data=f.read())
        artifacts = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .order_by("id")
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()
        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="0.0.1/edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        spphs = archive.getPublishedSources()
        # The source publications are for the same source package release,
        # which has a single file attached to it.
        self.assertEqual(1, len({spph.sourcepackagename for spph in spphs}))
        self.assertEqual(1, len({spph.sourcepackagerelease for spph in spphs}))
        self.assertThat(
            spphs,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        sourcepackagename=MatchesStructure.byEquality(
                            name=build.git_repository.target.name
                        ),
                        sourcepackagerelease=MatchesStructure(
                            ci_build=Equals(build),
                            files=MatchesSetwise(
                                MatchesStructure.byEquality(
                                    libraryfile=artifacts[0].library_file,
                                    filetype=SourcePackageFileType.SDIST,
                                )
                            ),
                        ),
                        format=Equals(SourcePackageType.CI_BUILD),
                        distroseries=Equals(distroseries),
                        channel=Equals(channel),
                    )
                    for channel in ("edge", "0.0.1/edge")
                )
            ),
        )
        bpphs = archive.getAllPublishedBinaries()
        # The binary publications are for the same binary package release,
        # which has a single file attached to it.
        self.assertEqual(1, len({bpph.binarypackagename for bpph in bpphs}))
        self.assertEqual(1, len({bpph.binarypackagerelease for bpph in bpphs}))
        self.assertThat(
            bpphs,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        binarypackagename=MatchesStructure.byEquality(
                            name="wheel-indep"
                        ),
                        binarypackagerelease=MatchesStructure(
                            ci_build=Equals(build),
                            files=MatchesSetwise(
                                MatchesStructure.byEquality(
                                    libraryfile=artifacts[1].library_file,
                                    filetype=BinaryPackageFileType.WHL,
                                )
                            ),
                        ),
                        binarypackageformat=Equals(BinaryPackageFormat.WHL),
                        distroarchseries=Equals(das),
                        channel=Equals(channel),
                    )
                    for channel in ("edge", "0.0.1/edge")
                )
            ),
        )

    def test_existing_binary_release_no_existing_source_release(self):
        # A `CIBuildUploadJob` can be run even if the build in question was
        # already uploaded somewhere, and in that case may add publications
        # in other locations for the same package.  This works even if there
        # was no existing source package release (because
        # `CIBuildUploadJob`s didn't always create one).
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        build = self.makeCIBuild(archive.distribution, distro_arch_series=das)
        report = build.getOrCreateRevisionStatusReport("build:0")
        sdist_path = "wheel-indep/dist/wheel-indep-0.0.1.tar.gz"
        with open(datadir(sdist_path), mode="rb") as f:
            report.attach(name=os.path.basename(sdist_path), data=f.read())
        wheel_path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(wheel_path), mode="rb") as f:
            report.attach(name=os.path.basename(wheel_path), data=f.read())
        artifacts = (
            IStore(RevisionStatusArtifact)
            .find(
                RevisionStatusArtifact,
                report=report,
                artifact_type=RevisionStatusArtifactType.BINARY,
            )
            .order_by("id")
        )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()
        with MockPatchObject(CIBuildUploadJob, "_uploadSources"):
            with dbuser(job.config.dbuser):
                JobRunner([job]).runAll()
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="0.0.1/edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        # There is a source publication for a new source package release.
        self.assertThat(
            archive.getPublishedSources(),
            MatchesSetwise(
                MatchesStructure(
                    sourcepackagename=MatchesStructure.byEquality(
                        name=build.git_repository.target.name
                    ),
                    sourcepackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        files=MatchesSetwise(
                            MatchesStructure.byEquality(
                                libraryfile=artifacts[0].library_file,
                                filetype=SourcePackageFileType.SDIST,
                            )
                        ),
                    ),
                    format=Equals(SourcePackageType.CI_BUILD),
                    distroseries=Equals(distroseries),
                    channel=Equals("0.0.1/edge"),
                )
            ),
        )
        bpphs = archive.getAllPublishedBinaries()
        # The binary publications are for the same binary package release,
        # which has a single file attached to it.
        self.assertEqual(1, len({bpph.binarypackagename for bpph in bpphs}))
        self.assertEqual(1, len({bpph.binarypackagerelease for bpph in bpphs}))
        self.assertThat(
            bpphs,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        binarypackagename=MatchesStructure.byEquality(
                            name="wheel-indep"
                        ),
                        binarypackagerelease=MatchesStructure(
                            ci_build=Equals(build),
                            files=MatchesSetwise(
                                MatchesStructure.byEquality(
                                    libraryfile=artifacts[1].library_file,
                                    filetype=BinaryPackageFileType.WHL,
                                )
                            ),
                        ),
                        binarypackageformat=Equals(BinaryPackageFormat.WHL),
                        distroarchseries=Equals(das),
                        channel=Equals(channel),
                    )
                    for channel in ("edge", "0.0.1/edge")
                )
            ),
        )

    def test_skips_disallowed_binary_formats(self):
        # A CI job might build multiple types of packages, of which only
        # some are interesting to upload to archives with a given repository
        # format.  Others are skipped.
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        build = self.makeCIBuild(archive.distribution, distro_arch_series=das)
        wheel_report = build.getOrCreateRevisionStatusReport("build-wheel:0")
        wheel_path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        conda_path = "conda-v2-arch/dist/linux-64/conda-v2-arch-0.1-0.conda"
        with open(datadir(wheel_path), mode="rb") as f:
            wheel_report.attach(
                name=os.path.basename(wheel_path), data=f.read()
            )
        conda_report = build.getOrCreateRevisionStatusReport("build-conda:0")
        with open(datadir(conda_path), mode="rb") as f:
            conda_report.attach(
                name=os.path.basename(conda_path), data=f.read()
            )
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                MatchesStructure(
                    binarypackagename=MatchesStructure.byEquality(
                        name="wheel-indep"
                    ),
                    binarypackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        binarypackagename=MatchesStructure.byEquality(
                            name="wheel-indep"
                        ),
                        version=Equals("0.0.1"),
                        binpackageformat=Equals(BinaryPackageFormat.WHL),
                    ),
                    binarypackageformat=Equals(BinaryPackageFormat.WHL),
                    distroarchseries=Equals(das),
                )
            ),
        )

    def test_librarian_server_error_retries(self):
        # A run that gets an error from the librarian server schedules
        # itself to be retried.
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        build = self.makeCIBuild(archive.distribution, distro_arch_series=das)
        report = build.getOrCreateRevisionStatusReport("build:0")
        path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(path), mode="rb") as f:
            report.attach(name=os.path.basename(path), data=f.read())
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="0.0.1/edge",
        )
        transaction.commit()

        def fake_open(*args):
            raise LibrarianServerError

        with dbuser(job.config.dbuser):
            with MockPatch(
                "lp.services.librarian.model.LibraryFileAlias.open", fake_open
            ):
                JobRunner([job]).runAll()

        self.assertEqual(JobStatus.WAITING, job.job.status)

        # Try again.  The job is retried, and this time it succeeds.
        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertEqual(JobStatus.COMPLETED, job.job.status)

    def test_skips_binaries_for_wrong_series(self):
        # A CI job might build binaries on multiple series.  When uploading
        # the results to an archive, only the binaries for the corresponding
        # series are selected.
        #
        # The build distribution is always Ubuntu for now, but the target
        # distribution may differ.
        logger = self.useFixture(FakeLogger())
        target_distribution = self.factory.makeDistribution()
        archive = self.factory.makeArchive(
            distribution=target_distribution,
            repository_format=ArchiveRepositoryFormat.PYTHON,
        )
        ubuntu_distroserieses = [
            self.factory.makeUbuntuDistroSeries() for _ in range(2)
        ]
        target_distroserieses = [
            self.factory.makeDistroSeries(distribution=target_distribution)
            for ubuntu_distroseries in ubuntu_distroserieses
        ]
        for ubuntu_distroseries, target_distroseries in zip(
            ubuntu_distroserieses, target_distroserieses
        ):
            self.factory.makeDistroSeriesParent(
                parent_series=ubuntu_distroseries,
                derived_series=target_distroseries,
            )
        processor = self.factory.makeProcessor()
        ubuntu_dases = [
            self.factory.makeDistroArchSeries(
                distroseries=ubuntu_distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
            for ubuntu_distroseries in ubuntu_distroserieses
        ]
        target_dases = [
            self.factory.makeDistroArchSeries(
                distroseries=target_distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
            for target_distroseries in target_distroserieses
        ]
        build = self.makeCIBuild(
            target_distribution, distro_arch_series=ubuntu_dases[0]
        )
        reports = [
            build.getOrCreateRevisionStatusReport(
                "build-wheel:%d" % i, distro_arch_series=ubuntu_das
            )
            for i, ubuntu_das in enumerate(ubuntu_dases)
        ]
        # We wouldn't normally expect to see only an
        # architecture-independent package in one series and only an
        # architecture-dependent package in another, but these test files
        # are handy.
        paths = [
            "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl",
            "wheel-arch/dist/wheel_arch-0.0.1-cp310-cp310-linux_x86_64.whl",
        ]
        for report, path in zip(reports, paths):
            with open(datadir(path), mode="rb") as f:
                report.attach(name=os.path.basename(path), data=f.read())
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            target_distroserieses[0],
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertThat(
            archive.getAllPublishedBinaries(),
            MatchesSetwise(
                MatchesStructure(
                    binarypackagename=MatchesStructure.byEquality(
                        name="wheel-indep"
                    ),
                    binarypackagerelease=MatchesStructure(
                        ci_build=Equals(build),
                        binarypackagename=MatchesStructure.byEquality(
                            name="wheel-indep"
                        ),
                        version=Equals("0.0.1"),
                        binpackageformat=Equals(BinaryPackageFormat.WHL),
                    ),
                    binarypackageformat=Equals(BinaryPackageFormat.WHL),
                    distroarchseries=Equals(target_dases[0]),
                )
            ),
        )
        self.assertEqual(JobStatus.COMPLETED, job.job.status)
        self.assertIn(
            "Skipping wheel_arch-0.0.1-cp310-cp310-linux_x86_64.whl (built "
            "for %s, not %s)"
            % (ubuntu_distroserieses[1].name, target_distroserieses[0].name),
            logger.output.splitlines(),
        )

    def test_run_failed(self):
        # A failed run sets the job status to FAILED and notifies the
        # requester.
        logger = self.useFixture(FakeLogger())
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        build = self.makeCIBuild(archive.distribution, distro_arch_series=das)
        report = build.getOrCreateRevisionStatusReport("build:0")
        path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(path), mode="rb") as f:
            # Use an invalid file name to force a scan failure.
            report.attach(name="_invalid.whl", data=f.read())
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="0.0.1/edge",
        )
        transaction.commit()

        with dbuser(job.config.dbuser):
            JobRunner([job]).runAll()

        self.assertEqual(JobStatus.FAILED, job.job.status)
        expected_logs = [
            "Running %r (ID %d) in status Waiting" % (job, job.job_id),
            "Failed to scan _invalid.whl as a Python wheel: Invalid wheel "
            "filename (wrong number of parts): '_invalid'",
            "%r (ID %d) failed with user error %r."
            % (
                job,
                job.job_id,
                ScanException(
                    "Could not find any usable files in ['_invalid.whl']"
                ),
            ),
        ]
        self.assertEqual(expected_logs, logger.output.splitlines())
        [notification] = self.assertEmailQueueLength(1)
        self.assertThat(
            dict(notification),
            ContainsDict(
                {
                    "From": Equals(config.canonical.noreply_from_address),
                    "To": Equals(
                        format_address_for_person(build.git_repository.owner)
                    ),
                    "Subject": Equals(
                        "Launchpad error while uploading %s to %s"
                        % (build.title, archive.reference)
                    ),
                }
            ),
        )
        self.assertEqual(
            "Launchpad encountered an error during the following operation: "
            "uploading %s to %s.  "
            "Could not find any usable files in ['_invalid.whl']"
            % (build.title, archive.reference),
            notification.get_payload(decode=True).decode(),
        )


class TestViaCelery(TestCaseWithFactory):
    layer = CeleryJobLayer

    def test_PackageUploadNotificationJob(self):
        # PackageUploadNotificationJob runs under Celery.
        self.useFixture(
            FeatureFixture(
                {"jobs.celery.enabled_classes": "PackageUploadNotificationJob"}
            )
        )
        creator = self.factory.makePerson()
        changes = Changes({"Changed-By": format_address_for_person(creator)})
        upload = self.factory.makePackageUpload(
            changes_file_content=changes.dump().encode()
        )
        with admin_logged_in():
            upload.addSource(self.factory.makeSourcePackageRelease())
            self.factory.makeComponentSelection(
                upload.distroseries, upload.sourcepackagerelease.component
            )
            upload.setAccepted()
        job = PackageUploadNotificationJob.create(upload)

        with block_on_job():
            transaction.commit()

        self.assertEqual(JobStatus.COMPLETED, job.status)
        self.assertEqual(1, len(pop_remote_notifications()))

    def test_CIBuildUploadJob(self):
        # CIBuildUploadJob runs under Celery.
        self.useFixture(
            FeatureFixture({"jobs.celery.enabled_classes": "CIBuildUploadJob"})
        )
        archive = self.factory.makeArchive(
            repository_format=ArchiveRepositoryFormat.PYTHON
        )
        distroseries = self.factory.makeDistroSeries(
            distribution=archive.distribution
        )
        das = self.factory.makeDistroArchSeries(distroseries=distroseries)
        dsp = self.factory.makeDistributionSourcePackage(
            distribution=archive.distribution
        )
        repository = self.factory.makeGitRepository(target=dsp)
        build = self.factory.makeCIBuild(
            git_repository=repository, distro_arch_series=das
        )
        report = build.getOrCreateRevisionStatusReport("build:0")
        path = "wheel-indep/dist/wheel_indep-0.0.1-py3-none-any.whl"
        with open(datadir(path), mode="rb") as f:
            with person_logged_in(build.git_repository.owner):
                report.attach(name=os.path.basename(path), data=f.read())
        job = CIBuildUploadJob.create(
            build,
            build.git_repository.owner,
            archive,
            das.distroseries,
            PackagePublishingPocket.RELEASE,
            target_channel="edge",
        )

        with block_on_job():
            transaction.commit()

        self.assertEqual(JobStatus.COMPLETED, job.status)
        self.assertEqual(1, archive.getAllPublishedBinaries().count())
