# Copyright 2010-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for BinaryPackageBuildBehaviour."""

import gzip
import os
import shutil
import tempfile

from storm.store import Store
from testtools.matchers import Equals, MatchesListwise
from testtools.twistedsupport import (
    AsynchronousDeferredRunTest,
    AsynchronousDeferredRunTestForBrokenTwisted,
)
from twisted.internet import defer
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    IArchiveGPGSigningKey,
)
from lp.buildmaster.enums import BuilderCleanStatus, BuildStatus
from lp.buildmaster.interactor import BuilderInteractor, extract_vitals_from_db
from lp.buildmaster.interfaces.builder import CannotBuild
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.manager import BuilddManager
from lp.buildmaster.tests.mock_workers import (
    AbortingWorker,
    BuildingWorker,
    OkWorker,
    WaitingWorker,
)
from lp.buildmaster.tests.test_buildfarmjobbehaviour import (
    TestGetUploadMethodsMixin,
    TestHandleStatusMixin,
    TestVerifySuccessfulBuildMixin,
)
from lp.buildmaster.tests.test_manager import MockBuilderFactory
from lp.registry.interfaces.pocket import PackagePublishingPocket, pocketsuffix
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackage import SourcePackageFileType
from lp.services.authserver.testing import InProcessAuthServerFixture
from lp.services.config import config
from lp.services.database.sqlbase import flush_database_caches
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.log.logger import BufferLogger
from lp.services.macaroons.testing import MacaroonVerifies
from lp.services.statsd.tests import StatsMixin
from lp.services.webapp import canonical_url
from lp.soyuz.adapters.archivedependencies import get_sources_list_for_building
from lp.soyuz.enums import ArchivePurpose, PackagePublishingStatus
from lp.soyuz.tests.soyuz import Base64KeyMatches
from lp.testing import StormStatementRecorder, TestCaseWithFactory
from lp.testing.dbuser import switch_dbuser
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import LaunchpadZopelessLayer, ZopelessDatabaseLayer
from lp.testing.matchers import HasQueryCount


class TestBinaryBuildPackageBehaviour(StatsMixin, TestCaseWithFactory):
    """Tests for the BinaryPackageBuildBehaviour.

    In particular, these tests are about how the BinaryPackageBuildBehaviour
    interacts with the build worker.  We test this by using a test double that
    implements the same interface as `BuilderWorker` but instead of actually
    making XML-RPC calls, just records any method invocations along with
    interesting parameters.
    """

    layer = ZopelessDatabaseLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        switch_dbuser("testadmin")
        self.setUpStats()

    @defer.inlineCallbacks
    def assertExpectedInteraction(
        self,
        call_log,
        builder,
        build,
        behaviour,
        chroot,
        archive,
        archive_purpose,
        component=None,
    ):
        matcher = yield self.makeExpectedInteraction(
            builder,
            build,
            behaviour,
            chroot,
            archive,
            archive_purpose,
            component,
        )
        self.assertThat(call_log, matcher)

    @defer.inlineCallbacks
    def makeExpectedInteraction(
        self,
        builder,
        build,
        behaviour,
        chroot,
        archive,
        archive_purpose,
        component=None,
    ):
        """Build the log of calls that we expect to be made to the worker.

        :param builder: The builder we are using to build the binary package.
        :param build: The build being done on the builder.
        :param behaviour: The build behaviour.
        :param chroot: The `LibraryFileAlias` for the chroot in which we are
            building.
        :param archive: The `IArchive` into which we are building.
        :param archive_purpose: The ArchivePurpose we are sending to the
            builder. We specify this separately from the archive because
            sometimes the behaviour object has to give a different purpose
            in order to trick the worker into building correctly.
        :return: A list of the calls we expect to be made.
        """
        das = build.distro_arch_series
        ds_name = das.distroseries.name
        suite = ds_name + pocketsuffix[build.pocket]
        archives, trusted_keys = yield get_sources_list_for_building(
            behaviour, das, build.source_package_release.name
        )
        arch_indep = das.isNominatedArchIndep
        if component is None:
            component = build.current_component.name
        files = build.source_package_release.files

        uploads = [(chroot.http_url, "", "")]
        for sprf in files:
            if sprf.libraryfile.restricted:
                password = MacaroonVerifies(
                    "binary-package-build", build.archive
                )
            else:
                password = ""
            uploads.append((sprf.libraryfile.getURL(), "", password))
        upload_logs = [
            MatchesListwise(
                [Equals("ensurepresent")]
                + [
                    item if hasattr(item, "match") else Equals(item)
                    for item in upload
                ]
            )
            for upload in uploads
        ]

        extra_args = {
            "arch_indep": arch_indep,
            "arch_tag": das.architecturetag,
            "abi_tag": das.architecturetag,
            "isa_tag": das.architecturetag,
            "archive_private": archive.private,
            "archive_purpose": archive_purpose.name,
            "archives": archives,
            "build_debug_symbols": archive.build_debug_symbols,
            "build_url": canonical_url(build),
            "builder_constraints": [],
            "fast_cleanup": builder.virtualized,
            "image_type": "chroot",
            "launchpad_instance": "devel",
            "launchpad_server_url": "launchpad.test",
            "ogrecomponent": component,
            "series": ds_name,
            "suite": suite,
            "trusted_keys": trusted_keys,
        }
        build_log = [
            (
                "build",
                build.build_cookie,
                "binarypackage",
                chroot.content.sha1,
                [sprf.libraryfile.filename for sprf in files],
                extra_args,
            )
        ]
        return MatchesListwise(
            [
                item if hasattr(item, "match") else Equals(item)
                for item in upload_logs + build_log
            ]
        )

    @defer.inlineCallbacks
    def test_non_virtual_ppa_dispatch(self):
        # When the BinaryPackageBuildBehaviour dispatches PPA builds to
        # non-virtual builders, it stores the chroot on the server and
        # requests a binary package build, lying to say that the archive
        # purpose is "PRIMARY" because this ensures that the package mangling
        # tools will run over the built packages.
        archive = self.factory.makeArchive(virtualized=False)
        worker = OkWorker()
        builder = self.factory.makeBuilder(virtualized=False)
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        vitals = extract_vitals_from_db(builder)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        build.source_package_release.addFile(
            self.factory.makeLibraryFileAlias(db_only=True),
            filetype=SourcePackageFileType.ORIG_TARBALL,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        interactor = BuilderInteractor()
        behaviour = interactor.getBuildBehaviour(bq, builder, worker)
        yield interactor._startBuild(
            bq, vitals, builder, worker, behaviour, BufferLogger()
        )
        yield self.assertExpectedInteraction(
            worker.call_log,
            builder,
            build,
            behaviour,
            lf,
            archive,
            ArchivePurpose.PRIMARY,
            "universe",
        )
        build_calls = self.filterStatsdCallsByName("build.")
        self.assertEqual(1, len(build_calls))
        self.assertEqual(
            build_calls[0],
            f"build.count,builder_name={builder.name},env=test,"
            f"job_type=PACKAGEBUILD,region={builder.region}",
        )

    @defer.inlineCallbacks
    def test_non_virtual_ppa_dispatch_with_primary_ancestry(self):
        # If there is a primary component override, it is honoured for
        # non-virtual PPA builds too.
        archive = self.factory.makeArchive(virtualized=False)
        worker = OkWorker()
        builder = self.factory.makeBuilder(virtualized=False)
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        vitals = extract_vitals_from_db(builder)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        build.source_package_release.addFile(
            self.factory.makeLibraryFileAlias(db_only=True),
            filetype=SourcePackageFileType.ORIG_TARBALL,
        )
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=build.distro_series,
            archive=archive.distribution.main_archive,
            sourcepackagename=build.source_package_release.sourcepackagename,
            component="main",
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        interactor = BuilderInteractor()
        behaviour = interactor.getBuildBehaviour(bq, builder, worker)
        yield interactor._startBuild(
            bq, vitals, builder, worker, behaviour, BufferLogger()
        )
        yield self.assertExpectedInteraction(
            worker.call_log,
            builder,
            build,
            behaviour,
            lf,
            archive,
            ArchivePurpose.PRIMARY,
            "main",
        )

    @defer.inlineCallbacks
    def test_virtual_ppa_dispatch(self):
        archive = self.factory.makeArchive(virtualized=True)
        worker = OkWorker()
        builder = self.factory.makeBuilder(virtualized=True, vm_host="foohost")
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        vitals = extract_vitals_from_db(builder)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        build.source_package_release.addFile(
            self.factory.makeLibraryFileAlias(db_only=True),
            filetype=SourcePackageFileType.ORIG_TARBALL,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        interactor = BuilderInteractor()
        behaviour = interactor.getBuildBehaviour(bq, builder, worker)
        yield interactor._startBuild(
            bq, vitals, builder, worker, behaviour, BufferLogger()
        )
        yield self.assertExpectedInteraction(
            worker.call_log,
            builder,
            build,
            behaviour,
            lf,
            archive,
            ArchivePurpose.PPA,
        )
        build_calls = self.filterStatsdCallsByName("build.")
        self.assertEqual(1, len(build_calls))
        self.assertEqual(
            build_calls[0],
            f"build.count,builder_name={builder.name},env=test,"
            f"job_type=PACKAGEBUILD,region={builder.region}",
        )

    @defer.inlineCallbacks
    def test_private_source_file_dispatch(self):
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        archive = self.factory.makeArchive(private=True)
        worker = OkWorker()
        builder = self.factory.makeBuilder()
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        vitals = extract_vitals_from_db(builder)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        build.source_package_release.addFile(
            self.factory.makeLibraryFileAlias(restricted=True, db_only=True),
            filetype=SourcePackageFileType.ORIG_TARBALL,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        interactor = BuilderInteractor()
        behaviour = interactor.getBuildBehaviour(bq, builder, worker)
        yield interactor._startBuild(
            bq, vitals, builder, worker, behaviour, BufferLogger()
        )
        yield self.assertExpectedInteraction(
            worker.call_log,
            builder,
            build,
            behaviour,
            lf,
            archive,
            ArchivePurpose.PPA,
        )

    @defer.inlineCallbacks
    def test_public_source_file_in_private_archive_dispatch(self):
        # A source file in a private archive might be unrestricted if the
        # same source package is also published in a public archive.  The
        # librarian will only serve restricted files if given a token, so
        # make sure that we don't send a token for unrestricted files.
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        archive = self.factory.makeArchive(private=True)
        worker = OkWorker()
        builder = self.factory.makeBuilder()
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        vitals = extract_vitals_from_db(builder)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        build.source_package_release.addFile(
            self.factory.makeLibraryFileAlias(db_only=True),
            filetype=SourcePackageFileType.ORIG_TARBALL,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        interactor = BuilderInteractor()
        behaviour = interactor.getBuildBehaviour(bq, builder, worker)
        yield interactor._startBuild(
            bq, vitals, builder, worker, behaviour, BufferLogger()
        )
        yield self.assertExpectedInteraction(
            worker.call_log,
            builder,
            build,
            behaviour,
            lf,
            archive,
            ArchivePurpose.PPA,
        )

    @defer.inlineCallbacks
    def test_partner_dispatch_no_publishing_history(self):
        archive = self.factory.makeArchive(
            virtualized=False, purpose=ArchivePurpose.PARTNER
        )
        worker = OkWorker()
        builder = self.factory.makeBuilder(virtualized=False)
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        vitals = extract_vitals_from_db(builder)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        build.source_package_release.addFile(
            self.factory.makeLibraryFileAlias(db_only=True),
            filetype=SourcePackageFileType.ORIG_TARBALL,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        interactor = BuilderInteractor()
        behaviour = interactor.getBuildBehaviour(bq, builder, worker)
        yield interactor._startBuild(
            bq, vitals, builder, worker, behaviour, BufferLogger()
        )
        yield self.assertExpectedInteraction(
            worker.call_log,
            builder,
            build,
            behaviour,
            lf,
            archive,
            ArchivePurpose.PARTNER,
        )

    def test_dont_dispatch_release_builds(self):
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        builder = self.factory.makeBuilder()
        distroseries = self.factory.makeDistroSeries(
            status=SeriesStatus.CURRENT, distribution=archive.distribution
        )
        distro_arch_series = self.factory.makeDistroArchSeries(
            distroseries=distroseries
        )
        build = self.factory.makeBinaryPackageBuild(
            builder=builder,
            archive=archive,
            distroarchseries=distro_arch_series,
            pocket=PackagePublishingPocket.RELEASE,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        e = self.assertRaises(
            AssertionError, behaviour.verifyBuildRequest, BufferLogger()
        )
        expected_message = (
            "%s (%s) can not be built for pocket %s: invalid pocket due "
            "to the series status of %s."
            % (
                build.title,
                build.id,
                build.pocket.name,
                build.distro_series.name,
            )
        )
        self.assertEqual(expected_message, str(e))

    def test_dont_dispatch_security_builds(self):
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        builder = self.factory.makeBuilder()
        build = self.factory.makeBinaryPackageBuild(
            builder=builder,
            archive=archive,
            pocket=PackagePublishingPocket.SECURITY,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        e = self.assertRaises(
            AssertionError, behaviour.verifyBuildRequest, BufferLogger()
        )
        self.assertEqual(
            "Soyuz is not yet capable of building SECURITY uploads.", str(e)
        )

    @defer.inlineCallbacks
    def test_arch_indep(self):
        # BinaryPackageBuild.arch_indep is passed through to the worker.
        builder = self.factory.makeBuilder()
        build = self.factory.makeBinaryPackageBuild(arch_indep=False)
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        extra_args = yield behaviour.extraBuildArgs()
        self.assertFalse(extra_args["arch_indep"])
        build = self.factory.makeBinaryPackageBuild(arch_indep=True)
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        extra_args = yield behaviour.extraBuildArgs()
        self.assertTrue(extra_args["arch_indep"])

    @defer.inlineCallbacks
    def test_determineFilesToSend_query_count(self):
        build = self.factory.makeBinaryPackageBuild()
        behaviour = IBuildFarmJobBehaviour(build)

        def add_file(build):
            build.source_package_release.addFile(
                self.factory.makeLibraryFileAlias(db_only=True),
                filetype=SourcePackageFileType.COMPONENT_ORIG_TARBALL,
            )

        # This is more or less `lp.testing.record_two_runs`, but that
        # doesn't work with asynchronous code, and it's easy enough to
        # inline the relevant bits.
        for _ in range(2):
            add_file(build)
        flush_database_caches()
        with StormStatementRecorder() as recorder1:
            filemap = yield behaviour.determineFilesToSend()
            self.assertEqual(2, len(list(filemap)))
        for _ in range(2):
            add_file(build)
        flush_database_caches()
        with StormStatementRecorder() as recorder2:
            filemap = yield behaviour.determineFilesToSend()
            self.assertEqual(4, len(list(filemap)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_proposed(self):
        # A build in the primary archive's proposed pocket uses the release,
        # security, updates, and proposed pockets in the primary archive.
        builder = self.factory.makeBuilder()
        build = self.factory.makeBinaryPackageBuild(
            pocket=PackagePublishingPocket.PROPOSED
        )
        for component_name in ("main", "universe"):
            self.factory.makeComponentSelection(
                build.distro_series, component_name
            )
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        expected_archives = [
            "deb %s %s main universe"
            % (build.archive.archive_url, build.distro_series.name),
            "deb %s %s-security main universe"
            % (build.archive.archive_url, build.distro_series.name),
            "deb %s %s-updates main universe"
            % (build.archive.archive_url, build.distro_series.name),
            "deb %s %s-proposed main universe"
            % (build.archive.archive_url, build.distro_series.name),
        ]
        extra_args = yield behaviour.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_backports(self):
        # A build in the primary archive's backports pocket uses the
        # release, security, updates, and backports pockets in the primary
        # archive.
        builder = self.factory.makeBuilder()
        build = self.factory.makeBinaryPackageBuild(
            pocket=PackagePublishingPocket.BACKPORTS
        )
        for component_name in ("main", "universe"):
            self.factory.makeComponentSelection(
                build.distro_series, component_name
            )
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        expected_archives = [
            "deb %s %s main universe"
            % (build.archive.archive_url, build.distro_series.name),
            "deb %s %s-security main universe"
            % (build.archive.archive_url, build.distro_series.name),
            "deb %s %s-updates main universe"
            % (build.archive.archive_url, build.distro_series.name),
            "deb %s %s-backports main universe"
            % (build.archive.archive_url, build.distro_series.name),
        ]
        extra_args = yield behaviour.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_ppa(self):
        # A build in a PPA uses the release pocket in the PPA and, by
        # default, the release, security, and updates pockets in the primary
        # archive.
        archive = self.factory.makeArchive()
        builder = self.factory.makeBuilder()
        build = self.factory.makeBinaryPackageBuild(archive=archive)
        for component_name in ("main", "universe"):
            self.factory.makeComponentSelection(
                build.distro_series, component_name
            )
        self.factory.makeBinaryPackagePublishingHistory(
            distroarchseries=build.distro_arch_series,
            pocket=build.pocket,
            archive=archive,
            status=PackagePublishingStatus.PUBLISHED,
        )
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        primary = build.distribution.main_archive
        expected_archives = [
            "deb %s %s main" % (archive.archive_url, build.distro_series.name),
            "deb %s %s main universe"
            % (primary.archive_url, build.distro_series.name),
            "deb %s %s-security main universe"
            % (primary.archive_url, build.distro_series.name),
            "deb %s %s-updates main universe"
            % (primary.archive_url, build.distro_series.name),
        ]
        extra_args = yield behaviour.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archive_trusted_keys(self):
        # If the archive has a signing key, extraBuildArgs sends it.
        yield self.useFixture(InProcessKeyServerFixture()).start()
        archive = self.factory.makeArchive()
        builder = self.factory.makeBuilder()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(archive).setSigningKey(
            key_path, async_keyserver=True
        )
        build = self.factory.makeBinaryPackageBuild(archive=archive)
        self.factory.makeBinaryPackagePublishingHistory(
            distroarchseries=build.distro_arch_series,
            pocket=build.pocket,
            archive=archive,
            status=PackagePublishingStatus.PUBLISHED,
        )
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        args = yield behaviour.extraBuildArgs()
        self.assertThat(
            args["trusted_keys"],
            MatchesListwise(
                [
                    Base64KeyMatches(
                        "0D57E99656BEFB0897606EE9A022DD1F5001B46D"
                    ),
                ]
            ),
        )

    def test_verifyBuildRequest(self):
        # Don't allow a virtual build on a non-virtual builder.
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        builder = self.factory.makeBuilder(virtualized=False)
        build = self.factory.makeBinaryPackageBuild(
            builder=builder,
            archive=archive,
            pocket=PackagePublishingPocket.RELEASE,
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        build.distro_arch_series.addOrUpdateChroot(lf)
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        e = self.assertRaises(
            AssertionError, behaviour.verifyBuildRequest, BufferLogger()
        )
        self.assertEqual(
            "Attempt to build virtual archive on a non-virtual builder.",
            str(e),
        )

    def test_verifyBuildRequest_no_chroot(self):
        # Don't dispatch a build when the DAS has no chroot.
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        builder = self.factory.makeBuilder()
        build = self.factory.makeBinaryPackageBuild(
            builder=builder, archive=archive
        )
        behaviour = IBuildFarmJobBehaviour(build)
        behaviour.setBuilder(builder, None)
        e = self.assertRaises(
            CannotBuild, behaviour.verifyBuildRequest, BufferLogger()
        )
        self.assertIn("Missing CHROOT", str(e))


class TestBinaryBuildPackageBehaviourBuildCollection(TestCaseWithFactory):
    """Tests for the BinaryPackageBuildBehaviour.

    Using various mock workers, we check how updateBuild() behaves in
    various scenarios.
    """

    # XXX: These tests replace part of an old doctest.
    # It was checking that each call to updateBuild was sending 3 (!)
    # emails but this behaviour is so ill-defined and dependent on the
    # sample data that I've not replicated that here.  We need to
    # examine that behaviour separately somehow, but the old tests gave
    # NO clue as to what, exactly, they were testing.

    layer = LaunchpadZopelessLayer
    run_tests_with = AsynchronousDeferredRunTest

    def _cleanup(self):
        if os.path.exists(config.builddmaster.root):
            shutil.rmtree(config.builddmaster.root)

    def setUp(self):
        super().setUp()
        switch_dbuser("testadmin")

        self.builder = self.factory.makeBuilder()
        self.manager = BuilddManager()
        self.interactor = BuilderInteractor()
        self.build = self.factory.makeBinaryPackageBuild(
            builder=self.builder, pocket=PackagePublishingPocket.RELEASE
        )
        lf = self.factory.makeLibraryFileAlias(db_only=True)
        self.build.distro_arch_series.addOrUpdateChroot(lf)
        self.candidate = self.build.queueBuild()
        self.candidate.markAsBuilding(self.builder)
        # This is required so that uploaded files from the buildd don't
        # hang around between test runs.
        self.addCleanup(self._cleanup)

    @defer.inlineCallbacks
    def updateBuild(self, candidate, worker):
        bf = MockBuilderFactory(self.builder, candidate)
        worker_status = yield worker.status()
        yield self.interactor.updateBuild(
            bf.getVitals("foo"),
            worker,
            worker_status,
            bf,
            self.interactor.getBuildBehaviour,
            self.manager,
        )
        self.manager.flushLogTails()

    def assertBuildProperties(self, build):
        """Check that a build happened by making sure some of its properties
        are set."""
        self.assertIsNot(None, build.builder)
        self.assertIsNot(None, build.date_finished)
        self.assertIsNot(None, build.duration)
        self.assertIsNot(None, build.log)

    def test_packagefail_collection(self):
        # When a package fails to build, make sure the builder notes are
        # stored and the build status is set as failed.
        def got_update(ignored):
            self.assertBuildProperties(self.build)
            self.assertEqual(BuildStatus.FAILEDTOBUILD, self.build.status)

        d = self.updateBuild(
            self.candidate, WaitingWorker("BuildStatus.PACKAGEFAIL")
        )
        return d.addCallback(got_update)

    def test_depwait_collection(self):
        # Package build was left in dependency wait.
        DEPENDENCIES = "baz (>= 1.0.1)"

        def got_update(ignored):
            self.assertBuildProperties(self.build)
            self.assertEqual(BuildStatus.MANUALDEPWAIT, self.build.status)
            self.assertEqual(DEPENDENCIES, self.build.dependencies)

        d = self.updateBuild(
            self.candidate, WaitingWorker("BuildStatus.DEPFAIL", DEPENDENCIES)
        )
        return d.addCallback(got_update)

    def test_chrootfail_collection(self):
        # There was a chroot problem for this build.
        def got_update(ignored):
            self.assertBuildProperties(self.build)
            self.assertEqual(BuildStatus.CHROOTWAIT, self.build.status)

        d = self.updateBuild(
            self.candidate, WaitingWorker("BuildStatus.CHROOTFAIL")
        )
        return d.addCallback(got_update)

    def test_building_collection(self):
        # The builder is still building the package.
        def got_update(ignored):
            # The fake log is returned from the BuildingWorker() mock.
            self.assertEqual("This is a build log: 0", self.candidate.logtail)

        d = self.updateBuild(self.candidate, BuildingWorker())
        return d.addCallback(got_update)

    def test_aborting_collection(self):
        # The builder is in the process of aborting.
        def got_update(ignored):
            self.assertEqual(
                "Waiting for worker process to be terminated",
                self.candidate.logtail,
            )

        d = self.updateBuild(self.candidate, AbortingWorker())
        return d.addCallback(got_update)

    def test_collection_for_deleted_source(self):
        # If we collected a build for a superseded/deleted source then
        # the build should get marked superseded as the build results
        # get discarded.
        spr = removeSecurityProxy(self.build.source_package_release)
        pub = self.build.current_source_publication
        pub.requestDeletion(spr.creator)

        def got_update(ignored):
            self.assertEqual(BuildStatus.SUPERSEDED, self.build.status)

        d = self.updateBuild(self.candidate, WaitingWorker("BuildStatus.OK"))
        return d.addCallback(got_update)

    def test_uploading_collection(self):
        # After a successful build, the status should be UPLOADING.
        def got_update(ignored):
            self.assertEqual(self.build.status, BuildStatus.UPLOADING)
            # We do not store any upload log information when the binary
            # upload processing succeeded.
            self.assertIs(None, self.build.upload_log)

        d = self.updateBuild(self.candidate, WaitingWorker("BuildStatus.OK"))
        return d.addCallback(got_update)

    def test_log_file_collection(self):
        self.build.updateStatus(BuildStatus.FULLYBUILT)
        old_tmps = sorted(os.listdir("/tmp"))

        worker = WaitingWorker("BuildStatus.OK")

        def got_log(logfile_lfa_id):
            # Grabbing logs should not leave new files in /tmp (bug #172798)
            logfile_lfa = getUtility(ILibraryFileAliasSet)[logfile_lfa_id]
            new_tmps = sorted(os.listdir("/tmp"))
            self.assertEqual(old_tmps, new_tmps)

            # The new librarian file is stored compressed with a .gz
            # extension and text/plain file type for easy viewing in
            # browsers, as it decompresses and displays the file inline.
            self.assertTrue(
                logfile_lfa.filename.endswith("_FULLYBUILT.txt.gz")
            )
            self.assertEqual("text/plain", logfile_lfa.mimetype)
            self.layer.txn.commit()

            # LibrarianFileAlias does not implement tell() or seek(), which
            # are required by gzip.open(), so we need to read the file out
            # of the librarian first.
            fd, fname = tempfile.mkstemp()
            self.addCleanup(os.remove, fname)
            tmp = os.fdopen(fd, "wb")
            tmp.write(logfile_lfa.read())
            tmp.close()
            uncompressed_file = gzip.open(fname).read()

            # Now make a temp filename that getFile() can write to.
            fd, tmp_orig_file_name = tempfile.mkstemp()
            self.addCleanup(os.remove, tmp_orig_file_name)

            # Check that the original file from the worker matches the
            # uncompressed file in the librarian.
            def got_orig_log(ignored):
                with open(tmp_orig_file_name, "rb") as orig_file:
                    orig_file_content = orig_file.read()
                self.assertEqual(orig_file_content, uncompressed_file)

            d = removeSecurityProxy(worker).getFile(
                "buildlog", tmp_orig_file_name
            )
            return d.addCallback(got_orig_log)

        behaviour = IBuildFarmJobBehaviour(self.build)
        behaviour.setBuilder(self.builder, worker)
        d = behaviour.getLogFromWorker(self.build.buildqueue_record)
        return d.addCallback(got_log)

    def test_private_build_log_storage(self):
        # Builds in private archives should have their log uploaded to
        # the restricted librarian.

        # Go behind Storm's back since the field validator on
        # Archive.private prevents us from setting it to True with
        # existing published sources.
        Store.of(self.build).execute(
            """
            UPDATE archive SET private=True
            WHERE archive.id = %s"""
            % self.build.archive.id
        )
        Store.of(self.build).invalidate()

        def got_update(ignored):
            # Librarian needs a commit.  :(
            self.layer.txn.commit()
            self.assertTrue(self.build.log.restricted)

        d = self.updateBuild(self.candidate, WaitingWorker("BuildStatus.OK"))
        return d.addCallback(got_update)


class MakeBinaryPackageBuildMixin:
    """Provide the makeBuild method returning a queud build."""

    def makeBuild(self):
        build = self.factory.makeBinaryPackageBuild()
        build.updateStatus(BuildStatus.BUILDING)
        build.queueBuild()
        return build

    def makeUnmodifiableBuild(self):
        build = self.factory.makeBinaryPackageBuild()
        build.distro_series.status = SeriesStatus.CURRENT
        build.updateStatus(BuildStatus.BUILDING)
        build.queueBuild()
        return build


class TestGetUploadMethodsForBinaryPackageBuild(
    MakeBinaryPackageBuildMixin, TestGetUploadMethodsMixin, TestCaseWithFactory
):
    """IPackageBuild.getUpload-related methods work with binary builds."""


class TestVerifySuccessfulBuildForBinaryPackageBuild(
    MakeBinaryPackageBuildMixin,
    TestVerifySuccessfulBuildMixin,
    TestCaseWithFactory,
):
    """IBuildFarmJobBehaviour.verifySuccessfulBuild works."""


class TestHandleStatusForBinaryPackageBuild(
    MakeBinaryPackageBuildMixin, TestHandleStatusMixin, TestCaseWithFactory
):
    """IPackageBuild.handleStatus works with binary builds."""
