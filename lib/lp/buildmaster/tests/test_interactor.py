# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test BuilderInteractor features."""

__all__ = [
    "FakeBuildQueue",
    "MockBuilderFactory",
]

import hashlib
import os
import signal
import tempfile
import xmlrpc.client
from functools import partial

import six
import treq
from fixtures import MockPatchObject
from lpbuildd.builder import BuilderStatus
from testtools.matchers import ContainsAll
from testtools.testcase import ExpectedException
from testtools.twistedsupport import (
    AsynchronousDeferredRunTest,
    AsynchronousDeferredRunTestForBrokenTwisted,
    assert_fails_with,
)
from twisted.internet import defer
from twisted.internet.task import Clock
from twisted.python.failure import Failure

from lp.buildmaster.enums import (
    BuilderCleanStatus,
    BuilderResetProtocol,
    BuildQueueStatus,
    BuildStatus,
)
from lp.buildmaster.interactor import (
    BuilderInteractor,
    BuilderWorker,
    extract_vitals_from_db,
    make_download_process_pool,
    shut_down_default_process_pool,
)
from lp.buildmaster.interfaces.builder import (
    BuildDaemonIsolationError,
    CannotFetchFile,
    CannotResumeHost,
)
from lp.buildmaster.manager import BaseBuilderFactory, PrefetchedBuilderFactory
from lp.buildmaster.tests.mock_workers import (
    AbortingWorker,
    BuildingWorker,
    DeadProxy,
    LostBuildingBrokenWorker,
    MockBuilder,
    OkWorker,
    WaitingWorker,
    WorkerTestHelpers,
)
from lp.services.config import config
from lp.services.log.logger import BufferLogger
from lp.services.twistedsupport.testing import TReqFixture
from lp.services.twistedsupport.treq import check_status
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.model.binarypackagebuildbehaviour import (
    BinaryPackageBuildBehaviour,
)
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.fakemethod import FakeMethod
from lp.testing.layers import LaunchpadZopelessLayer, ZopelessDatabaseLayer


class FakeBuildQueue:
    def __init__(self, cookie="PACKAGEBUILD-1"):
        self.build_cookie = cookie
        self.reset = FakeMethod()
        self.status = BuildQueueStatus.RUNNING


class MockBuilderFactory(BaseBuilderFactory):
    """A mock builder factory which uses a preset Builder and BuildQueue."""

    def __init__(self, builder, build_queue):
        self.updateTestData(builder, build_queue)
        self.get_call_count = 0
        self.getVitals_call_count = 0

    def update(self):
        return

    def prescanUpdate(self):
        return

    def updateTestData(self, builder, build_queue):
        self._builder = builder
        self._build_queue = build_queue

    def __getitem__(self, name):
        self.get_call_count += 1
        return self._builder

    def getVitals(self, name):
        self.getVitals_call_count += 1
        return extract_vitals_from_db(self._builder, self._build_queue)


class TestBuilderInteractor(TestCase):
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        self.addCleanup(shut_down_default_process_pool)

    def resumeWorkerHost(self, builder):
        vitals = extract_vitals_from_db(builder)
        return BuilderInteractor.resumeWorkerHost(
            vitals, BuilderInteractor.makeWorkerFromVitals(vitals)
        )

    def test_resumeWorkerHost_nonvirtual(self):
        d = self.resumeWorkerHost(MockBuilder(virtualized=False))
        return assert_fails_with(d, CannotResumeHost)

    def test_resumeWorkerHost_no_vmhost(self):
        d = self.resumeWorkerHost(MockBuilder(virtualized=False, vm_host=None))
        return assert_fails_with(d, CannotResumeHost)

    def test_resumeWorkerHost_success(self):
        reset_config = """
            [builddmaster]
            vm_resume_command: /bin/echo -n snap %(buildd_name)s %(vm_host)s
            """
        config.push("reset", reset_config)
        self.addCleanup(config.pop, "reset")

        d = self.resumeWorkerHost(
            MockBuilder(
                url="http://crackle.ppa/", virtualized=True, vm_host="pop"
            )
        )

        def got_resume(output):
            self.assertEqual((b"snap crackle pop", b""), output)

        return d.addCallback(got_resume)

    def test_resumeWorkerHost_command_failed(self):
        reset_fail_config = """
            [builddmaster]
            vm_resume_command: /bin/false"""
        config.push("reset fail", reset_fail_config)
        self.addCleanup(config.pop, "reset fail")
        d = self.resumeWorkerHost(MockBuilder(virtualized=True, vm_host="pop"))
        return assert_fails_with(d, CannotResumeHost)

    def test_makeWorkerFromVitals(self):
        # BuilderInteractor.makeWorkerFromVitals returns a BuilderWorker
        # that points at the actual Builder.  The Builder is only ever used
        # in scripts that run outside of the security context.
        builder = MockBuilder(virtualized=False)
        vitals = extract_vitals_from_db(builder)
        worker = BuilderInteractor.makeWorkerFromVitals(vitals)
        self.assertEqual(builder.url, worker.url)
        self.assertEqual(10, worker.timeout)

        builder = MockBuilder(virtualized=True)
        vitals = extract_vitals_from_db(builder)
        worker = BuilderInteractor.makeWorkerFromVitals(vitals)
        self.assertEqual(5, worker.timeout)


class TestBuilderInteractorCleanWorker(TestCase):
    run_tests_with = AsynchronousDeferredRunTest

    @defer.inlineCallbacks
    def assertCleanCalls(self, builder, worker, calls, done):
        actually_done = yield BuilderInteractor.cleanWorker(
            extract_vitals_from_db(builder),
            worker,
            MockBuilderFactory(builder, None),
        )
        self.assertEqual(done, actually_done)
        self.assertEqual(calls, worker.method_log)

    @defer.inlineCallbacks
    def test_virtual_1_1(self):
        # Virtual builders using protocol 1.1 get reset, and once the
        # trigger completes we're happy that it's clean.
        builder = MockBuilder(
            virtualized=True,
            clean_status=BuilderCleanStatus.DIRTY,
            vm_host="lol",
            vm_reset_protocol=BuilderResetProtocol.PROTO_1_1,
        )
        yield self.assertCleanCalls(
            builder, OkWorker(), ["resume", "echo"], True
        )

    @defer.inlineCallbacks
    def test_virtual_2_0_dirty(self):
        # Virtual builders using protocol 2.0 get reset and set to
        # CLEANING. It's then up to the non-Launchpad reset code to set
        # the builder back to CLEAN using the webservice.
        builder = MockBuilder(
            virtualized=True,
            clean_status=BuilderCleanStatus.DIRTY,
            vm_host="lol",
            vm_reset_protocol=BuilderResetProtocol.PROTO_2_0,
        )
        yield self.assertCleanCalls(builder, OkWorker(), ["resume"], False)
        self.assertEqual(BuilderCleanStatus.CLEANING, builder.clean_status)

    @defer.inlineCallbacks
    def test_virtual_2_0_cleaning(self):
        # Virtual builders using protocol 2.0 only get touched when
        # they're DIRTY. Once they're cleaning, they're not our problem
        # until they return to CLEAN, so we ignore them.
        builder = MockBuilder(
            virtualized=True,
            clean_status=BuilderCleanStatus.CLEANING,
            vm_host="lol",
            vm_reset_protocol=BuilderResetProtocol.PROTO_2_0,
        )
        yield self.assertCleanCalls(builder, OkWorker(), [], False)
        self.assertEqual(BuilderCleanStatus.CLEANING, builder.clean_status)

    @defer.inlineCallbacks
    def test_virtual_no_protocol(self):
        # Virtual builders fail to clean unless vm_reset_protocol is
        # set.
        builder = MockBuilder(
            virtualized=True,
            clean_status=BuilderCleanStatus.DIRTY,
            vm_host="lol",
        )
        builder.vm_reset_protocol = None
        with ExpectedException(
            CannotResumeHost, "Invalid vm_reset_protocol: None"
        ):
            yield BuilderInteractor.cleanWorker(
                extract_vitals_from_db(builder),
                OkWorker(),
                MockBuilderFactory(builder, None),
            )

    @defer.inlineCallbacks
    def test_nonvirtual_idle(self):
        # An IDLE non-virtual worker is already as clean as we can get it.
        yield self.assertCleanCalls(
            MockBuilder(
                virtualized=False, clean_status=BuilderCleanStatus.DIRTY
            ),
            OkWorker(),
            ["status"],
            True,
        )

    @defer.inlineCallbacks
    def test_nonvirtual_building(self):
        # A BUILDING non-virtual worker needs to be aborted. It'll go
        # through ABORTING and eventually be picked up from WAITING.
        yield self.assertCleanCalls(
            MockBuilder(
                virtualized=False, clean_status=BuilderCleanStatus.DIRTY
            ),
            BuildingWorker(),
            ["status", "abort"],
            False,
        )

    @defer.inlineCallbacks
    def test_nonvirtual_aborting(self):
        # An ABORTING non-virtual worker must be waited out. It should
        # hit WAITING eventually.
        yield self.assertCleanCalls(
            MockBuilder(
                virtualized=False, clean_status=BuilderCleanStatus.DIRTY
            ),
            AbortingWorker(),
            ["status"],
            False,
        )

    @defer.inlineCallbacks
    def test_nonvirtual_waiting(self):
        # A WAITING non-virtual worker just needs clean() called.
        yield self.assertCleanCalls(
            MockBuilder(
                virtualized=False, clean_status=BuilderCleanStatus.DIRTY
            ),
            WaitingWorker(),
            ["status", "clean"],
            True,
        )

    @defer.inlineCallbacks
    def test_nonvirtual_broken(self):
        # A broken non-virtual builder is probably unrecoverable, so the
        # method just crashes.
        builder = MockBuilder(
            virtualized=False, clean_status=BuilderCleanStatus.DIRTY
        )
        vitals = extract_vitals_from_db(builder)
        worker = LostBuildingBrokenWorker()
        try:
            yield BuilderInteractor.cleanWorker(
                vitals, worker, MockBuilderFactory(builder, None)
            )
        except xmlrpc.client.Fault:
            self.assertEqual(["status", "abort"], worker.call_log)
        else:
            self.fail("abort() should crash.")


class TestBuilderWorkerStatus(TestCase):
    # Verify what BuilderWorker.status returns with workers in different
    # states.

    run_tests_with = AsynchronousDeferredRunTest

    @defer.inlineCallbacks
    def assertStatus(
        self,
        worker,
        builder_status=None,
        build_status=None,
        build_id=False,
        logtail=False,
        filemap=None,
        dependencies=None,
    ):
        status = yield worker.status()

        expected = {}
        if builder_status is not None:
            expected["builder_status"] = builder_status
        if build_status is not None:
            expected["build_status"] = build_status
        if filemap is not None:
            expected["filemap"] = filemap
            expected["dependencies"] = dependencies

        # We don't care so much about the build_id or the content of the
        # logtail, just that they're there.
        if build_id:
            self.assertIn("build_id", status)
            del status["build_id"]
        if logtail:
            tail = status.pop("logtail")
            self.assertIsInstance(tail, xmlrpc.client.Binary)

        self.assertEqual(expected, status)

    def test_status_idle_worker(self):
        self.assertStatus(OkWorker(), builder_status="BuilderStatus.IDLE")

    def test_status_building_worker(self):
        self.assertStatus(
            BuildingWorker(),
            builder_status="BuilderStatus.BUILDING",
            build_id=True,
            logtail=True,
        )

    def test_status_waiting_worker(self):
        self.assertStatus(
            WaitingWorker(),
            builder_status="BuilderStatus.WAITING",
            build_status="BuildStatus.OK",
            build_id=True,
            filemap={},
        )

    def test_status_aborting_worker(self):
        self.assertStatus(
            AbortingWorker(),
            builder_status="BuilderStatus.ABORTING",
            build_id=True,
        )


class TestBuilderInteractorDB(TestCaseWithFactory):
    """BuilderInteractor tests that need a DB."""

    layer = ZopelessDatabaseLayer
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def test_getBuildBehaviour_idle(self):
        """An idle builder has no build behaviour."""
        self.assertIs(
            None,
            BuilderInteractor.getBuildBehaviour(None, MockBuilder(), None),
        )

    def test_getBuildBehaviour_building(self):
        """The current behaviour is set automatically from the current job."""
        # Set the builder attribute on the buildqueue record so that our
        # builder will think it has a current build.
        builder = self.factory.makeBuilder(name="builder")
        worker = BuildingWorker()
        build = self.factory.makeBinaryPackageBuild()
        bq = build.queueBuild()
        bq.markAsBuilding(builder)
        behaviour = BuilderInteractor.getBuildBehaviour(bq, builder, worker)
        self.assertIsInstance(behaviour, BinaryPackageBuildBehaviour)
        self.assertEqual(behaviour._builder, builder)
        self.assertEqual(behaviour._worker, worker)

    def _setupBuilder(self):
        processor = self.factory.makeProcessor(name="i386")
        builder = self.factory.makeBuilder(
            processors=[processor], virtualized=True, vm_host="bladh"
        )
        builder.setCleanStatus(BuilderCleanStatus.CLEAN)
        self.patch(BuilderWorker, "makeBuilderWorker", FakeMethod(OkWorker()))
        distroseries = self.factory.makeDistroSeries()
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )
        chroot = self.factory.makeLibraryFileAlias(db_only=True)
        das.addOrUpdateChroot(chroot)
        distroseries.nominatedarchindep = das
        return builder, distroseries, das

    def _setupRecipeBuildAndBuilder(self):
        # Helper function to make a builder capable of building a
        # recipe, returning both.
        builder, distroseries, distroarchseries = self._setupBuilder()
        build = self.factory.makeSourcePackageRecipeBuild(
            distroseries=distroseries
        )
        return builder, build

    def _setupBinaryBuildAndBuilder(self):
        # Helper function to make a builder capable of building a
        # binary package, returning both.
        builder, distroseries, distroarchseries = self._setupBuilder()
        build = self.factory.makeBinaryPackageBuild(
            distroarchseries=distroarchseries, builder=builder
        )
        return builder, build

    def test_findAndStartJob_returns_candidate(self):
        # findAndStartJob finds the next queued job using findBuildCandidate.
        # We don't care about the type of build at all.
        builder, build = self._setupRecipeBuildAndBuilder()
        candidate = build.queueBuild()
        builder_factory = MockBuilderFactory(builder, candidate)
        # findBuildCandidate is tested elsewhere, we just make sure that
        # findAndStartJob delegates to it.
        builder_factory.findBuildCandidate = FakeMethod(result=candidate)
        vitals = extract_vitals_from_db(builder)
        d = BuilderInteractor.findAndStartJob(
            vitals, builder, OkWorker(), builder_factory
        )
        return d.addCallback(self.assertEqual, candidate)

    @defer.inlineCallbacks
    def test_findAndStartJob_supersedes_builds(self):
        # findAndStartJob checks whether queued jobs are for superseded
        # source package releases and marks the corresponding build records
        # as SUPERSEDED.
        builder, distroseries, distroarchseries = self._setupBuilder()
        builds = [
            self.factory.makeBinaryPackageBuild(
                distroarchseries=distroarchseries
            )
            for _ in range(3)
        ]
        candidates = [build.queueBuild() for build in builds]
        builder_factory = PrefetchedBuilderFactory()
        candidates_iter = iter(candidates)
        builder_factory.findBuildCandidate = lambda _: next(candidates_iter)
        vitals = extract_vitals_from_db(builder)

        # Supersede some of the builds' source packages.
        for build in builds[:2]:
            publication = build.current_source_publication
            publication.status = PackagePublishingStatus.SUPERSEDED

        # Starting a job selects a non-superseded candidate, and supersedes
        # the candidates that have superseded source packages.
        candidate = yield BuilderInteractor.findAndStartJob(
            vitals, builder, OkWorker(), builder_factory
        )
        self.assertEqual(candidates[2], candidate)
        self.assertEqual(
            [
                BuildStatus.SUPERSEDED,
                BuildStatus.SUPERSEDED,
                BuildStatus.BUILDING,
            ],
            [build.status for build in builds],
        )

    def test_findAndStartJob_starts_job(self):
        # findAndStartJob finds the next queued job using findBuildCandidate
        # and then starts it.
        # We don't care about the type of build at all.
        builder, build = self._setupRecipeBuildAndBuilder()
        candidate = build.queueBuild()
        builder_factory = MockBuilderFactory(builder, candidate)
        builder_factory.findBuildCandidate = FakeMethod(result=candidate)
        vitals = extract_vitals_from_db(builder)
        d = BuilderInteractor.findAndStartJob(
            vitals, builder, OkWorker(), builder_factory
        )

        def check_build_started(candidate):
            self.assertEqual(candidate.builder, builder)
            self.assertEqual(BuildStatus.BUILDING, build.status)

        return d.addCallback(check_build_started)

    @defer.inlineCallbacks
    def test_findAndStartJob_requires_clean_worker(self):
        # findAndStartJob ensures that its worker starts CLEAN.
        builder, build = self._setupBinaryBuildAndBuilder()
        builder.setCleanStatus(BuilderCleanStatus.DIRTY)
        candidate = build.queueBuild()
        builder_factory = MockBuilderFactory(builder, candidate)
        builder_factory.findBuildCandidate = FakeMethod(result=candidate)
        vitals = extract_vitals_from_db(builder)
        with ExpectedException(
            BuildDaemonIsolationError,
            "Attempted to start build on a dirty worker.",
        ):
            yield BuilderInteractor.findAndStartJob(
                vitals, builder, OkWorker(), builder_factory
            )

    @defer.inlineCallbacks
    def test_findAndStartJob_dirties_worker(self):
        # findAndStartJob marks its builder DIRTY before dispatching.
        builder, build = self._setupBinaryBuildAndBuilder()
        candidate = build.queueBuild()
        builder_factory = MockBuilderFactory(builder, candidate)
        builder_factory.findBuildCandidate = FakeMethod(result=candidate)
        vitals = extract_vitals_from_db(builder)
        yield BuilderInteractor.findAndStartJob(
            vitals, builder, OkWorker(), builder_factory
        )
        self.assertEqual(BuilderCleanStatus.DIRTY, builder.clean_status)


class TestWorker(TestCase):
    """
    Integration tests for BuilderWorker that verify how it works against a
    real worker server.
    """

    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        self.worker_helper = self.useFixture(WorkerTestHelpers())
        self.addCleanup(shut_down_default_process_pool)

    @defer.inlineCallbacks
    def test_abort(self):
        worker = self.worker_helper.getClientWorker()
        # We need to be in a BUILDING state before we can abort.
        build_id = "some-id"
        response = yield self.worker_helper.triggerGoodBuild(worker, build_id)
        self.assertEqual([BuilderStatus.BUILDING, build_id], response)
        response = yield worker.abort()
        self.assertEqual(BuilderStatus.ABORTING, response)

    @defer.inlineCallbacks
    def test_build(self):
        # Calling 'build' with an expected builder type, a good build id,
        # valid chroot & filemaps works and returns a BuilderStatus of
        # BUILDING.
        build_id = "some-id"
        worker = self.worker_helper.getClientWorker()
        response = yield self.worker_helper.triggerGoodBuild(worker, build_id)
        self.assertEqual([BuilderStatus.BUILDING, build_id], response)

    def test_clean(self):
        worker = self.worker_helper.getClientWorker()
        # XXX: JonathanLange 2010-09-21: Calling clean() on the worker requires
        # it to be in either the WAITING or ABORTED states, and both of these
        # states are very difficult to achieve in a test environment. For the
        # time being, we'll just assert that a clean attribute exists.
        self.assertNotEqual(getattr(worker, "clean", None), None)

    @defer.inlineCallbacks
    def test_echo(self):
        # Calling 'echo' contacts the server which returns the arguments we
        # gave it.
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        response = yield worker.echo("foo", "bar", 42)
        self.assertEqual(["foo", "bar", 42], response)

    @defer.inlineCallbacks
    def test_info(self):
        # Calling 'info' gets some information about the worker.
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        info = yield worker.info()
        # We're testing the hard-coded values, since the version is hard-coded
        # into the remote worker, the supported build managers are hard-coded
        # into the tac file for the remote worker and config is returned from
        # the configuration file.
        self.assertEqual(3, len(info))
        self.assertEqual(["1.0", "i386"], info[:2])
        self.assertThat(
            info[2],
            ContainsAll(
                (
                    "sourcepackagerecipe",
                    "translation-templates",
                    "binarypackage",
                    "livefs",
                    "snap",
                )
            ),
        )

    @defer.inlineCallbacks
    def test_initial_status(self):
        # Calling 'status' returns the current status of the worker. The
        # initial status is IDLE.
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        status = yield worker.status()
        self.assertEqual(BuilderStatus.IDLE, status["builder_status"])

    @defer.inlineCallbacks
    def test_status_after_build(self):
        # Calling 'status' returns the current status of the worker.  After a
        # build has been triggered, the status is BUILDING.
        worker = self.worker_helper.getClientWorker()
        build_id = "status-build-id"
        response = yield self.worker_helper.triggerGoodBuild(worker, build_id)
        self.assertEqual([BuilderStatus.BUILDING, build_id], response)
        status = yield worker.status()
        self.assertEqual(BuilderStatus.BUILDING, status["builder_status"])
        self.assertEqual(build_id, status["build_id"])
        self.assertIsInstance(status["logtail"], xmlrpc.client.Binary)

    @defer.inlineCallbacks
    def test_ensurepresent_not_there(self):
        # ensurepresent checks to see if a file is there.
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        response = yield worker.ensurepresent("blahblah", None, None, None)
        self.assertEqual([False, "No URL"], response)

    @defer.inlineCallbacks
    def test_ensurepresent_actually_there(self):
        # ensurepresent checks to see if a file is there.
        tachandler = self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        self.worker_helper.makeCacheFile(tachandler, "blahblah")
        response = yield worker.ensurepresent("blahblah", None, None, None)
        self.assertEqual([True, "No URL"], response)

    @defer.inlineCallbacks
    def test_sendFileToWorker_not_there(self):
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()

        test_logger = BufferLogger()

        d = worker.sendFileToWorker(
            "blahblah", None, None, None, logger=test_logger
        )

        yield assert_fails_with(d, CannotFetchFile)

        log_output = test_logger.getLogBuffer()
        self.assertIn("Failed to fetch", log_output)
        self.assertIn("blahblah", log_output)

    @defer.inlineCallbacks
    def test_sendFileToWorker_actually_there(self):
        tachandler = self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()

        test_logger = BufferLogger()

        self.worker_helper.makeCacheFile(tachandler, "blahblah")
        yield worker.sendFileToWorker(
            "blahblah", None, None, None, logger=test_logger
        )
        response = yield worker.ensurepresent("blahblah", None, None, None)
        self.assertEqual([True, "No URL"], response)

        log_output = test_logger.getLogBuffer()
        self.assertIn("ensure it has", log_output)
        self.assertIn("blahblah", log_output)

    @defer.inlineCallbacks
    def test_sendFileToWorker_ensurepresent_raises(self):
        # When ensurepresent raises an exception, sendFileToWorker should
        # log it and raise CannotFetchFile.
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()

        test_logger = BufferLogger()

        # Mock ensurepresent to raise an exception
        test_error = RuntimeError("Test connection error")
        worker.ensurepresent = FakeMethod(failure=test_error)

        # sendFileToWorker should catch the exception and raise CannotFetchFile
        d = worker.sendFileToWorker(
            "blahblah", None, None, None, logger=test_logger
        )

        yield assert_fails_with(d, CannotFetchFile)

        # Verify that logging occurred - check for exception logging
        log_output = test_logger.getLogBuffer()
        self.assertIn("Failed to ensure", log_output)
        self.assertIn("blahblah", log_output)
        self.assertIn("Test connection error", log_output)

    @defer.inlineCallbacks
    def test_resumeHost_success(self):
        # On a successful resume resume() fires the returned deferred
        # callback with (stdout, stderr, subprocess exit code).
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()

        # The configuration testing command-line.
        self.assertEqual(
            "echo %(vm_host)s", config.builddmaster.vm_resume_command
        )

        out, err, code = yield worker.resume()
        self.assertEqual(os.EX_OK, code)
        # XXX: JonathanLange 2010-09-23: We should instead pass the
        # expected vm_host into the client worker. Not doing this now,
        # since the WorkerHelper is being moved around.
        self.assertEqual("%s\n" % worker._vm_host, six.ensure_str(out))

    def test_resumeHost_failure(self):
        # On a failed resume, 'resumeHost' fires the returned deferred
        # errorback with the `ProcessTerminated` failure.
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()

        # Override the configuration command-line with one that will fail.
        failed_config = """
        [builddmaster]
        vm_resume_command: test "%(vm_host)s = 'no-sir'"
        """
        config.push("failed_resume_command", failed_config)
        self.addCleanup(config.pop, "failed_resume_command")

        # On failures, the response is a twisted `Failure` object containing
        # a tuple.
        def check_resume_failure(failure):
            out, err, code = failure.value
            # The process will exit with a return code of "1".
            self.assertEqual(code, 1)

        d = worker.resume()
        d.addBoth(check_resume_failure)
        return d

    def test_resumeHost_timeout(self):
        # On a resume timeouts, 'resumeHost' fires the returned deferred
        # errorback with the `TimeoutError` failure.

        # Override the configuration command-line with one that will timeout.
        timeout_config = """
        [builddmaster]
        vm_resume_command: sleep 5
        socket_timeout: 1
        """
        config.push("timeout_resume_command", timeout_config)
        self.addCleanup(config.pop, "timeout_resume_command")

        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()

        # On timeouts, the response is a twisted `Failure` object containing
        # a `TimeoutError` error.
        def check_resume_timeout(failure):
            self.assertIsInstance(failure, Failure)
            out, err, code = failure.value
            self.assertEqual(code, signal.SIGKILL)

        clock = Clock()
        d = worker.resume(clock=clock)
        # Move the clock beyond the socket_timeout but earlier than the
        # sleep 5.  This stops the test having to wait for the timeout.
        # Fast tests FTW!
        clock.advance(2)
        d.addBoth(check_resume_timeout)
        return d


class TestWorkerTimeouts(TestCase):
    # Testing that the methods that call callRemote() all time out
    # as required.

    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        self.worker_helper = self.useFixture(WorkerTestHelpers())
        self.clock = Clock()
        self.proxy = DeadProxy(b"url")
        self.worker = self.worker_helper.getClientWorker(
            reactor=self.clock, proxy=self.proxy
        )
        self.addCleanup(shut_down_default_process_pool)

    def assertCancelled(self, d, timeout=None):
        self.clock.advance((timeout or config.builddmaster.socket_timeout) + 1)
        return assert_fails_with(d, defer.CancelledError)

    def test_timeout_abort(self):
        return self.assertCancelled(self.worker.abort())

    def test_timeout_clean(self):
        return self.assertCancelled(self.worker.clean())

    def test_timeout_echo(self):
        return self.assertCancelled(self.worker.echo())

    def test_timeout_info(self):
        return self.assertCancelled(self.worker.info())

    def test_timeout_status(self):
        return self.assertCancelled(self.worker.status())

    def test_timeout_ensurepresent(self):
        return self.assertCancelled(
            self.worker.ensurepresent(None, None, None, None),
            config.builddmaster.socket_timeout * 20,
        )

    def test_timeout_build(self):
        return self.assertCancelled(
            self.worker.build(None, None, None, None, None)
        )


class TestWorkerConnectionTimeouts(TestCase):
    # Testing that we can override the default 30 second connection
    # timeout.

    # The timeouts in test_connection_timeout are relative to the artificial
    # Clock rather than to true wallclock time, so it's not a problem for
    # this timeout to be shorter than them.
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        self.worker_helper = self.useFixture(WorkerTestHelpers())
        self.clock = Clock()
        self.addCleanup(shut_down_default_process_pool)

    def test_connection_timeout(self):
        # The default timeout of 30 seconds should not cause a timeout,
        # only the config value should.
        self.pushConfig("builddmaster", socket_timeout=180)

        worker = self.worker_helper.getClientWorker(reactor=self.clock)
        d = worker.echo()
        # Advance past the 30 second timeout.  The real reactor will
        # never call connectTCP() since we're not spinning it up.  This
        # avoids "connection refused" errors and simulates an
        # environment where the endpoint doesn't respond.
        self.clock.advance(31)
        self.assertFalse(d.called)

        self.clock.advance(config.builddmaster.socket_timeout + 1)
        self.assertTrue(d.called)
        return assert_fails_with(d, defer.CancelledError)


class TestWorkerWithLibrarian(TestCaseWithFactory):
    """Tests that need more of Launchpad to run."""

    layer = LaunchpadZopelessLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        self.worker_helper = self.useFixture(WorkerTestHelpers())
        self.addCleanup(shut_down_default_process_pool)

    def test_ensurepresent_librarian(self):
        # ensurepresent, when given an http URL for a file will download the
        # file from that URL and report that the file is present, and it was
        # downloaded.

        # Use the Librarian because it's a "convenient" web server.
        lf = self.factory.makeLibraryFileAlias(
            "HelloWorld.txt", content="Hello World"
        )
        self.layer.txn.commit()
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        d = worker.ensurepresent(lf.content.sha1, lf.http_url, "", "")
        d.addCallback(self.assertEqual, [True, "Download"])
        return d

    @defer.inlineCallbacks
    def test_retrieve_files_from_filecache(self):
        # Files that are present on the worker can be downloaded with a
        # filename made from the sha1 of the content underneath the
        # 'filecache' directory.
        from twisted.internet import reactor

        content = b"Hello World"
        lf = self.factory.makeLibraryFileAlias(
            "HelloWorld.txt", content=content
        )
        self.layer.txn.commit()
        expected_url = "%s/filecache/%s" % (
            self.worker_helper.base_url,
            lf.content.sha1,
        )
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        yield worker.ensurepresent(lf.content.sha1, lf.http_url, "", "")
        client = self.useFixture(TReqFixture(reactor)).client
        response = yield client.get(expected_url).addCallback(check_status)
        got_content = yield treq.content(response)
        self.assertEqual(content, got_content)

    def test_getFiles(self):
        # Test BuilderWorker.getFiles().
        # It also implicitly tests getFile() - I don't want to test that
        # separately because it increases test run time and it's going
        # away at some point anyway, in favour of getFiles().
        contents = ["content1", "content2", "content3"]
        self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        files = []
        content_map = {}

        def got_files(ignored):
            # Called back when getFiles finishes.  Make sure all the
            # content is as expected.
            for sha1, local_file in files:
                with open(local_file) as f:
                    self.assertEqual(content_map[sha1], f.read())

        def finished_uploading(ignored):
            d = worker.getFiles(files)
            return d.addCallback(got_files)

        # Set up some files on the builder and store details in
        # content_map so we can compare downloads later.
        dl = []
        for content in contents:
            filename = content + ".txt"
            lf = self.factory.makeLibraryFileAlias(filename, content=content)
            content_map[lf.content.sha1] = content
            files.append((lf.content.sha1, tempfile.mkstemp()[1]))
            self.addCleanup(os.remove, files[-1][1])
            # Add the same file contents again with a different name, to
            # ensure that we can tolerate duplication.
            files.append((lf.content.sha1, tempfile.mkstemp()[1]))
            self.addCleanup(os.remove, files[-1][1])
            self.layer.txn.commit()
            d = worker.ensurepresent(lf.content.sha1, lf.http_url, "", "")
            dl.append(d)

        return defer.DeferredList(dl).addCallback(finished_uploading)

    def test_getFiles_open_connections(self):
        # getFiles honours the configured limit on active download
        # connections.
        contents = [self.factory.getUniqueString() for _ in range(10)]
        self.worker_helper.getServerWorker()
        process_pool = make_download_process_pool(min=1, max=2)
        process_pool.start()
        self.addCleanup(process_pool.stop)
        worker = self.worker_helper.getClientWorker(process_pool=process_pool)
        files = []
        content_map = {}

        def got_files(ignored):
            # Called back when getFiles finishes.  Make sure all the
            # content is as expected.
            for sha1, local_file in files:
                with open(local_file) as f:
                    self.assertEqual(content_map[sha1], f.read())
            # Only two workers were used.
            self.assertEqual(2, len(process_pool.processes))

        def finished_uploading(ignored):
            d = worker.getFiles(files)
            return d.addCallback(got_files)

        # Set up some files on the builder and store details in
        # content_map so we can compare downloads later.
        dl = []
        for content in contents:
            filename = content + ".txt"
            lf = self.factory.makeLibraryFileAlias(filename, content=content)
            content_map[lf.content.sha1] = content
            files.append((lf.content.sha1, tempfile.mkstemp()[1]))
            self.addCleanup(os.remove, files[-1][1])
            self.layer.txn.commit()
            d = worker.ensurepresent(lf.content.sha1, lf.http_url, "", "")
            dl.append(d)

        return defer.DeferredList(dl).addCallback(finished_uploading)

    @defer.inlineCallbacks
    def test_getFiles_with_empty_file(self):
        # getFiles works with zero-length files.
        tachandler = self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        temp_fd, temp_name = tempfile.mkstemp()
        self.addCleanup(os.remove, temp_name)
        empty_sha1 = hashlib.sha1(b"").hexdigest()
        self.worker_helper.makeCacheFile(tachandler, empty_sha1, contents=b"")
        yield worker.getFiles([(empty_sha1, temp_name)])
        with open(temp_name, "rb") as f:
            self.assertEqual(b"", f.read())

    @defer.inlineCallbacks
    def test_getFiles_to_subdirectory(self):
        # getFiles works if asked to download files to a subdirectory.
        # (This is used by CI builds.)
        tachandler = self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        temp_dir = self.makeTemporaryDirectory()
        temp_name = os.path.join(temp_dir, "build:0", "log")
        empty_sha1 = hashlib.sha1(b"").hexdigest()
        self.worker_helper.makeCacheFile(tachandler, empty_sha1, contents=b"")
        yield worker.getFiles([(empty_sha1, temp_name)])
        with open(temp_name, "rb") as f:
            self.assertEqual(b"", f.read())

    @defer.inlineCallbacks
    def test_getFiles_retries(self):
        # getFiles retries failed download attempts rather than giving up on
        # the first failure.
        self.pushConfig("builddmaster", download_attempts=3)
        tachandler = self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        count = 0

        def fail_twice(original, *args, **kwargs):
            nonlocal count
            count += 1
            if count < 3:
                raise RuntimeError("Boom")
            return original(*args, **kwargs)

        self.useFixture(
            MockPatchObject(
                worker.process_pool,
                "doWork",
                side_effect=partial(fail_twice, worker.process_pool.doWork),
            )
        )
        temp_dir = self.makeTemporaryDirectory()
        temp_name = os.path.join(temp_dir, "log")
        sha1 = hashlib.sha1(b"log").hexdigest()
        self.worker_helper.makeCacheFile(tachandler, sha1, contents=b"log")
        yield worker.getFiles([(sha1, temp_name)])
        with open(temp_name, "rb") as f:
            self.assertEqual(b"log", f.read())

    @defer.inlineCallbacks
    def test_getFiles_limited_retries(self):
        # getFiles gives up on retrying downloads after the configured
        # number of attempts.
        self.pushConfig("builddmaster", download_attempts=3)
        tachandler = self.worker_helper.getServerWorker()
        worker = self.worker_helper.getClientWorker()
        self.useFixture(
            MockPatchObject(
                worker.process_pool, "doWork", side_effect=RuntimeError("Boom")
            )
        )
        temp_dir = self.makeTemporaryDirectory()
        temp_name = os.path.join(temp_dir, "log")
        sha1 = hashlib.sha1(b"log").hexdigest()
        self.worker_helper.makeCacheFile(tachandler, sha1, contents=b"log")
        with ExpectedException(RuntimeError, r"^Boom$"):
            yield worker.getFiles([(sha1, temp_name)])
