# Copyright 2015-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test snap package build behaviour."""

import base64
import json
import os.path
import time
import uuid
from datetime import datetime
from textwrap import dedent
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import fixtures
from aptsources.sourceslist import SourceEntry
from dateutil import tz
from pymacaroons import Macaroon
from testtools import ExpectedException
from testtools.matchers import (
    AfterPreprocessing,
    ContainsDict,
    Equals,
    Is,
    IsInstance,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
    StartsWith,
)
from testtools.twistedsupport import (
    AsynchronousDeferredRunTestForBrokenTwisted,
)
from twisted.internet import defer
from zope.component import getUtility
from zope.proxy import isProxy
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    IArchiveGPGSigningKey,
)
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import BuildBaseImageType, BuildStatus
from lp.buildmaster.interactor import shut_down_default_process_pool
from lp.buildmaster.interfaces.builder import CannotBuild
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.buildmaster.tests.builderproxy import (
    InProcessProxyAuthAPIFixture,
    ProxyURLMatcher,
    RevocationEndpointMatcher,
)
from lp.buildmaster.tests.fetchservice import (
    InProcessFetchServiceAuthAPIFixture,
)
from lp.buildmaster.tests.mock_workers import (
    MockBuilder,
    OkWorker,
    WorkerTestHelpers,
)
from lp.buildmaster.tests.test_buildfarmjobbehaviour import (
    TestGetUploadMethodsMixin,
    TestHandleStatusMixin,
    TestVerifySuccessfulBuildMixin,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.authserver.testing import InProcessAuthServerFixture
from lp.services.config import config
from lp.services.features.testing import FeatureFixture
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.services.macaroons.testing import MacaroonVerifies
from lp.services.statsd.tests import StatsMixin
from lp.services.webapp import canonical_url
from lp.snappy.interfaces.snap import (
    SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG,
    SnapBuildArchiveOwnerMismatch,
)
from lp.snappy.model.snapbuildbehaviour import (
    SnapBuildBehaviour,
    format_as_rfc3339,
)
from lp.soyuz.adapters.archivedependencies import get_sources_list_for_building
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.interfaces.archive import ArchiveDisabled
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.tests.soyuz import Base64KeyMatches
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.dbuser import dbuser
from lp.testing.gpgkeys import gpgkeysdir, import_public_key
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import ZopelessDatabaseLayer


class FormatAsRfc3339TestCase(TestCase):
    def test_simple(self):
        t = datetime(2016, 1, 1)
        self.assertEqual("2016-01-01T00:00:00Z", format_as_rfc3339(t))

    def test_microsecond_is_ignored(self):
        ts = datetime(2016, 1, 1, microsecond=10)
        self.assertEqual("2016-01-01T00:00:00Z", format_as_rfc3339(ts))

    def test_tzinfo_is_ignored(self):
        time_zone = datetime(2016, 1, 1, tzinfo=tz.gettz("US/Eastern"))
        self.assertEqual("2016-01-01T00:00:00Z", format_as_rfc3339(time_zone))


class TestSnapBuildBehaviourBase(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.pushConfig("snappy", tools_source=None, tools_fingerprint=None)

    def makeJob(
        self, archive=None, pocket=PackagePublishingPocket.UPDATES, **kwargs
    ):
        """Create a sample `ISnapBuildBehaviour`."""
        if archive is None:
            distribution = self.factory.makeDistribution(name="distro")
        else:
            distribution = archive.distribution
        distroseries = self.factory.makeDistroSeries(
            distribution=distribution, name="unstable"
        )
        processor = getUtility(IProcessorSet).getByName("386")
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )

        # Taken from test_archivedependencies.py
        for component_name in ("main", "universe"):
            self.factory.makeComponentSelection(distroseries, component_name)

        build = self.factory.makeSnapBuild(
            archive=archive,
            distroarchseries=distroarchseries,
            pocket=pocket,
            name="test-snap",
            target_architectures=["i386"],
            **kwargs,
        )
        return IBuildFarmJobBehaviour(build)


class TestSnapBuildBehaviour(TestSnapBuildBehaviourBase):
    def test_provides_interface(self):
        # SnapBuildBehaviour provides IBuildFarmJobBehaviour.
        job = SnapBuildBehaviour(None)
        self.assertProvides(job, IBuildFarmJobBehaviour)

    def test_adapts_ISnapBuild(self):
        # IBuildFarmJobBehaviour adapts an ISnapBuild.
        build = self.factory.makeSnapBuild()
        job = IBuildFarmJobBehaviour(build)
        self.assertProvides(job, IBuildFarmJobBehaviour)

    def test_verifyBuildRequest_valid(self):
        # verifyBuildRequest doesn't raise any exceptions when called with a
        # valid builder set.
        job = self.makeJob()
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        builder = MockBuilder()
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        job.verifyBuildRequest(logger)
        self.assertEqual("", logger.getLogBuffer())

    def test_verifyBuildRequest_virtual_mismatch(self):
        # verifyBuildRequest raises on an attempt to build a virtualized
        # build on a non-virtual builder.
        job = self.makeJob()
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        builder = MockBuilder(virtualized=False)
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        e = self.assertRaises(AssertionError, job.verifyBuildRequest, logger)
        self.assertEqual(
            "Attempt to build virtual item on a non-virtual builder.", str(e)
        )

    def test_verifyBuildRequest_archive_disabled(self):
        archive = self.factory.makeArchive(
            enabled=False, displayname="Disabled Archive"
        )
        job = self.makeJob(archive=archive)
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        builder = MockBuilder()
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        e = self.assertRaises(ArchiveDisabled, job.verifyBuildRequest, logger)
        self.assertEqual("Disabled Archive is disabled.", str(e))

    def test_verifyBuildRequest_archive_private_owners_match(self):
        archive = self.factory.makeArchive(private=True)
        job = self.makeJob(
            archive=archive, registrant=archive.owner, owner=archive.owner
        )
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        builder = MockBuilder()
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        job.verifyBuildRequest(logger)
        self.assertEqual("", logger.getLogBuffer())

    def test_verifyBuildRequest_archive_private_owners_mismatch(self):
        archive = self.factory.makeArchive(private=True)
        job = self.makeJob(archive=archive)
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        builder = MockBuilder()
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        e = self.assertRaises(
            SnapBuildArchiveOwnerMismatch, job.verifyBuildRequest, logger
        )
        self.assertEqual(
            "Snap package builds against private archives are only allowed "
            "if the snap package owner and the archive owner are equal.",
            str(e),
        )

    def test_verifyBuildRequest_no_chroot(self):
        # verifyBuildRequest raises when the DAS has no chroot.
        job = self.makeJob()
        builder = MockBuilder()
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        e = self.assertRaises(CannotBuild, job.verifyBuildRequest, logger)
        self.assertIn("Missing chroot", str(e))


class TestAsyncSnapBuildBehaviourFetchService(
    StatsMixin, TestSnapBuildBehaviourBase
):
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    @defer.inlineCallbacks
    def setUp(self):
        super().setUp()
        self.session = {
            "id": "1",
            "token": uuid.uuid4().hex,
        }
        self.fetch_service_url = (
            "http://{session_id}:{token}@{host}:{port}".format(
                session_id=self.session["id"],
                token=self.session["token"],
                host=config.builddmaster.fetch_service_host,
                port=config.builddmaster.fetch_service_port,
            )
        )
        self.fetch_service_api = self.useFixture(
            InProcessFetchServiceAuthAPIFixture()
        )
        yield self.fetch_service_api.start()
        self.now = time.time()
        self.useFixture(fixtures.MockPatch("time.time", return_value=self.now))
        self.addCleanup(shut_down_default_process_pool)
        self.setUpStats()

    def makeJob(self, **kwargs):
        # We need a builder worker in these tests, in order that requesting
        # a proxy token can piggyback on its reactor and pool.
        job = super().makeJob(**kwargs)
        builder = MockBuilder()
        builder.processor = job.build.processor
        worker = self.useFixture(WorkerTestHelpers()).getClientWorker()
        job.setBuilder(builder, worker)
        self.addCleanup(worker.pool.closeCachedConnections)
        return job

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_unconfigured(self):
        """Create a snap build request with an incomplete fetch service
        configuration.

        If `fetch_service_host` is not provided the function will return
        without populating `proxy_url` and `revocation_endpoint`.
        """
        self.pushConfig("builddmaster", fetch_service_host=None)
        snap = self.factory.makeSnap(use_fetch_service=True)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual([], self.fetch_service_api.sessions.requests)
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_no_certificate(self):
        """Create a snap build request with an incomplete fetch service
        configuration.

        If `fetch_service_mitm_certificate` is not provided
        the function raises a `CannotBuild` error.
        """
        self.pushConfig("builddmaster", fetch_service_mitm_certificate=None)

        snap = self.factory.makeSnap(use_fetch_service=True)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        expected_exception_msg = (
            "fetch_service_mitm_certificate is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_no_secret(self):
        """Create a snap build request with an incomplete fetch service
        configuration.

        If `fetch_service_control_admin_secret` is not provided
        the function raises a `CannotBuild` error.
        """
        self.pushConfig(
            "builddmaster", fetch_service_control_admin_secret=None
        )
        snap = self.factory.makeSnap(use_fetch_service=True)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        expected_exception_msg = (
            "fetch_service_control_admin_secret is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestFetchServiceSession(self):
        """Create a snap build request with a successful fetch service
        configuration.

        `proxy_url` and `revocation_endpoint` are correctly populated.
        """
        snap = self.factory.makeSnap(use_fetch_service=True)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        args = yield job.extraBuildArgs()
        request_matcher = MatchesDict(
            {
                "method": Equals(b"POST"),
                "uri": Equals(b"/session"),
                "headers": ContainsDict(
                    {
                        b"Authorization": MatchesListwise(
                            [
                                Equals(
                                    b"Basic "
                                    + base64.b64encode(
                                        b"admin-launchpad.test:admin-secret"
                                    )
                                )
                            ]
                        ),
                        b"Content-Type": MatchesListwise(
                            [
                                Equals(b"application/json"),
                            ]
                        ),
                    }
                ),
                "json": MatchesDict(
                    {
                        "policy": Equals("strict"),
                    }
                ),
            }
        )
        self.assertThat(
            self.fetch_service_api.sessions.requests,
            MatchesListwise([request_matcher]),
        )
        self.assertIn("proxy_url", args)
        self.assertIn("revocation_endpoint", args)
        self.assertTrue(args["use_fetch_service"])
        self.assertIn("secrets", args)
        self.assertIn("fetch_service_mitm_certificate", args["secrets"])
        self.assertIn(
            "fake-cert", args["secrets"]["fetch_service_mitm_certificate"]
        )

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_permissive(self):
        """Create a snap build request with a successful fetch service
        configuration.

        `proxy_url` and `revocation_endpoint` are correctly populated.
        """
        snap = self.factory.makeSnap(
            use_fetch_service=True,
            fetch_service_policy=FetchServicePolicy.PERMISSIVE,
        )
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        args = yield job.extraBuildArgs()
        request_matcher = MatchesDict(
            {
                "method": Equals(b"POST"),
                "uri": Equals(b"/session"),
                "headers": ContainsDict(
                    {
                        b"Authorization": MatchesListwise(
                            [
                                Equals(
                                    b"Basic "
                                    + base64.b64encode(
                                        b"admin-launchpad.test:admin-secret"
                                    )
                                )
                            ]
                        ),
                        b"Content-Type": MatchesListwise(
                            [
                                Equals(b"application/json"),
                            ]
                        ),
                    }
                ),
                "json": MatchesDict(
                    {
                        "policy": Equals("permissive"),
                    }
                ),
            }
        )
        self.assertThat(
            self.fetch_service_api.sessions.requests,
            MatchesListwise([request_matcher]),
        )
        self.assertIn("proxy_url", args)
        self.assertIn("revocation_endpoint", args)
        self.assertTrue(args["use_fetch_service"])
        self.assertIn("secrets", args)
        self.assertIn("fetch_service_mitm_certificate", args["secrets"])
        self.assertIn(
            "fake-cert", args["secrets"]["fetch_service_mitm_certificate"]
        )

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_mitm_certficate_redacted(self):
        """The `fetch_service_mitm_certificate` field in the build arguments
        is redacted in the build logs."""
        snap = self.factory.makeSnap(use_fetch_service=True)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        args = yield job.extraBuildArgs()

        chroot_lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(
            chroot_lfa, image_type=BuildBaseImageType.CHROOT
        )
        lxd_lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(
            lxd_lfa, image_type=BuildBaseImageType.LXD
        )
        deferred = defer.Deferred()
        deferred.callback(None)
        job._worker.sendFileToWorker = MagicMock(return_value=deferred)
        job._worker.build = MagicMock(return_value=(None, None))

        logger = BufferLogger()
        yield job.dispatchBuildToWorker(logger)

        # Secrets exist within the arguments
        self.assertIn(
            "fake-cert", args["secrets"]["fetch_service_mitm_certificate"]
        )
        # But are redacted in the log output
        self.assertIn(
            "'fetch_service_mitm_certificate': '<redacted>'",
            logger.getLogBuffer(),
        )

    @defer.inlineCallbacks
    def test_endProxySession(self):
        """By ending a fetch service session, metadata is retrieved from the
        fetch service and saved to a file; and call to end the session and
        removing resources are made.
        """
        tem_upload_path = self.useFixture(fixtures.TempDir()).path

        snap = self.factory.makeSnap(use_fetch_service=True)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)

        host = config.builddmaster.fetch_service_host
        port = config.builddmaster.fetch_service_port
        session_id = self.fetch_service_api.sessions.session_id
        revocation_endpoint = (
            f"http://{host}:{port}/session/{session_id}/token"
        )

        job._worker.proxy_info = MagicMock(
            return_value={
                "revocation_endpoint": revocation_endpoint,
                "use_fetch_service": True,
            }
        )
        yield job.extraBuildArgs()

        # End the session
        yield job.endProxySession(upload_path=tem_upload_path)

        # We expect 4 calls made to the fetch service API, in this order
        self.assertEqual(4, len(self.fetch_service_api.sessions.requests))

        # Request start a session
        start_session_request = self.fetch_service_api.sessions.requests[0]
        self.assertEqual(b"POST", start_session_request["method"])
        self.assertEqual(b"/session", start_session_request["uri"])

        # Request retrieve metadata
        retrieve_metadata_request = self.fetch_service_api.sessions.requests[1]
        self.assertEqual(b"GET", retrieve_metadata_request["method"])
        self.assertEqual(
            f"/session/{session_id}".encode(), retrieve_metadata_request["uri"]
        )

        # Request end session
        end_session_request = self.fetch_service_api.sessions.requests[2]
        self.assertEqual(b"DELETE", end_session_request["method"])
        self.assertEqual(
            f"/session/{session_id}".encode(), end_session_request["uri"]
        )

        # Request removal of resources
        remove_resources_request = self.fetch_service_api.sessions.requests[3]
        self.assertEqual(b"DELETE", remove_resources_request["method"])
        self.assertEqual(
            f"/resources/{session_id}".encode(),
            remove_resources_request["uri"],
        )

        # The expected file is created in the `tem_upload_path`
        expected_filename = f"{job.build.build_cookie}_metadata.json"
        expected_file_path = os.path.join(tem_upload_path, expected_filename)
        with open(expected_file_path) as f:
            self.assertEqual(
                json.dumps(self.fetch_service_api.sessions.responses[1]),
                f.read(),
            )

    @defer.inlineCallbacks
    def test_endProxySession_fetch_Service_false(self):
        """When `use_fetch_service` is False, we don't make any calls to the
        fetch service API."""
        snap = self.factory.makeSnap(use_fetch_service=False)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)

        job._worker.proxy_info = MagicMock(
            return_value={
                "revocation_endpoint": "https://builder-proxy.test/revoke",
                "use_fetch_service": False,
            }
        )

        yield job.extraBuildArgs()
        yield job.endProxySession(upload_path="test_path")

        # No calls go through to the fetch service
        self.assertEqual(0, len(self.fetch_service_api.sessions.requests))

    @defer.inlineCallbacks
    def test_endProxySession_fetch_Service_allow_internet_false(self):
        """When `allow_internet` is False, we don't send proxy variables to the
        buildd, and ending the session does not make calls to the fetch
        service."""
        snap = self.factory.makeSnap(allow_internet=False)
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        args = yield job.extraBuildArgs()

        # Scenario when we don't allow internet
        job._worker.proxy_info = MagicMock(
            return_value={
                "revocation_endpoint": None,
                "use_fetch_service": False,
            }
        )

        yield job.endProxySession(upload_path="test_path")

        # No proxy config sent to buildd
        self.assertIsNone(args.get("use_fetch_service"))
        self.assertIsNone(args.get("revocation_endpoint"))
        # No calls go through to the fetch service
        self.assertEqual(0, len(self.fetch_service_api.sessions.requests))


class TestAsyncSnapBuildBehaviourBuilderProxy(
    StatsMixin, TestSnapBuildBehaviourBase
):
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    @defer.inlineCallbacks
    def setUp(self):
        super().setUp()
        build_username = "SNAPBUILD-1"
        self.token = {
            "secret": uuid.uuid4().hex,
            "username": build_username,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self.proxy_url = (
            "http://{username}:{password}"
            "@{host}:{port}".format(
                username=self.token["username"],
                password=self.token["secret"],
                host=config.builddmaster.builder_proxy_host,
                port=config.builddmaster.builder_proxy_port,
            )
        )
        self.proxy_api = self.useFixture(InProcessProxyAuthAPIFixture())
        yield self.proxy_api.start()
        self.now = time.time()
        self.useFixture(fixtures.MockPatch("time.time", return_value=self.now))
        self.addCleanup(shut_down_default_process_pool)
        self.setUpStats()

    def makeJob(self, **kwargs):
        # We need a builder worker in these tests, in order that requesting
        # a proxy token can piggyback on its reactor and pool.
        job = super().makeJob(**kwargs)
        builder = MockBuilder()
        builder.processor = job.build.processor
        worker = self.useFixture(WorkerTestHelpers()).getClientWorker()
        job.setBuilder(builder, worker)
        self.addCleanup(worker.pool.closeCachedConnections)
        return job

    @defer.inlineCallbacks
    def test_composeBuildRequest(self):
        job = self.makeJob()
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        build_request = yield job.composeBuildRequest(None)
        self.assertThat(
            build_request,
            MatchesListwise(
                [
                    Equals("snap"),
                    Equals(job.build.distro_arch_series),
                    Equals(job.build.pocket),
                    Equals({}),
                    IsInstance(dict),
                ]
            ),
        )

    @defer.inlineCallbacks
    def test_requestProxyToken_unconfigured(self):
        self.pushConfig("builddmaster", builder_proxy_host=None)
        branch = self.factory.makeBranch()
        job = self.makeJob(branch=branch)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual([], self.proxy_api.tokens.requests)
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)
        self.assertNotIn("use_fetch_service", args)

    @defer.inlineCallbacks
    def test_requestProxyToken_no_secret(self):
        self.pushConfig(
            "builddmaster", builder_proxy_auth_api_admin_secret=None
        )
        branch = self.factory.makeBranch()
        job = self.makeJob(branch=branch)
        expected_exception_msg = (
            "builder_proxy_auth_api_admin_secret is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestProxyToken(self):
        branch = self.factory.makeBranch()
        job = self.makeJob(branch=branch)
        yield job.extraBuildArgs()
        expected_uri = urlsplit(
            config.builddmaster.builder_proxy_auth_api_endpoint
        ).path.encode("UTF-8")
        request_matcher = MatchesDict(
            {
                "method": Equals(b"POST"),
                "uri": Equals(expected_uri),
                "headers": ContainsDict(
                    {
                        b"Authorization": MatchesListwise(
                            [
                                Equals(
                                    b"Basic "
                                    + base64.b64encode(
                                        b"admin-launchpad.test:admin-secret"
                                    )
                                )
                            ]
                        ),
                        b"Content-Type": MatchesListwise(
                            [
                                Equals(b"application/json"),
                            ]
                        ),
                    }
                ),
                "json": MatchesDict(
                    {
                        "username": StartsWith(job.build.build_cookie + "-"),
                    }
                ),
            }
        )
        self.assertThat(
            self.proxy_api.tokens.requests, MatchesListwise([request_matcher])
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_no_security_proxy(self):
        # The result of  `extraBuildArgs` must not contain values wrapped with
        # zope security proxy because they can't be marshalled for XML-RPC
        # requests.
        snap = self.factory.makeSnap()
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        for key, value in args.items():
            self.assertFalse(isProxy(value), f"{key} is a security proxy")

    @defer.inlineCallbacks
    def test_extraBuildArgs_bzr(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for a Bazaar branch.
        branch = self.factory.makeBranch()
        job = self.makeJob(branch=branch)
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        for archive_line in expected_archives:
            self.assertIn("universe", archive_line)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()

        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "branch": Equals(branch.bzr_identity),
                    "build_source_tarball": Is(False),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "name": Equals("test-snap"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "target_architectures": Equals(["i386"]),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_build_request_args(self):
        snap = self.factory.makeSnap()
        request = self.factory.makeSnapBuildRequest(snap=snap)
        job = self.makeJob(snap=snap, build_request=request)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual(request.id, args["build_request_id"])
        expected_timestamp = format_as_rfc3339(request.date_requested)
        self.assertEqual(expected_timestamp, args["build_request_timestamp"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_git(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for a Git branch.
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref)
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        for archive_line in expected_archives:
            self.assertIn("universe", archive_line)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_source_tarball": Is(False),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(ref.repository.git_https_url),
                    "git_path": Equals(ref.name),
                    "name": Equals("test-snap"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "target_architectures": Equals(["i386"]),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_HEAD(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for the default branch in a Launchpad-hosted Git repository.
        [ref] = self.factory.makeGitRefs()
        removeSecurityProxy(ref.repository)._default_branch = ref.path
        job = self.makeJob(git_ref=ref.repository.getRefByPath("HEAD"))
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        for archive_line in expected_archives:
            self.assertIn("universe", archive_line)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_source_tarball": Is(False),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(ref.repository.git_https_url),
                    "name": Equals("test-snap"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "target_architectures": Equals(["i386"]),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_private(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for a private Git branch.
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        [ref] = self.factory.makeGitRefs(
            information_type=InformationType.USERDATA
        )
        job = self.makeJob(git_ref=ref, private=True)
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        for archive_line in expected_archives:
            self.assertIn("universe", archive_line)
        args = yield job.extraBuildArgs()
        split_browse_root = urlsplit(config.codehosting.git_browse_root)
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_source_tarball": Is(False),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": AfterPreprocessing(
                        urlsplit,
                        MatchesStructure(
                            scheme=Equals(split_browse_root.scheme),
                            username=Equals("+launchpad-services"),
                            password=AfterPreprocessing(
                                Macaroon.deserialize,
                                MatchesStructure(
                                    location=Equals(
                                        config.vhost.mainsite.hostname
                                    ),
                                    identifier=Equals("snap-build"),
                                    caveats=MatchesListwise(
                                        [
                                            MatchesStructure.byEquality(
                                                caveat_id="lp.snap-build %s"
                                                % job.build.id
                                            ),
                                        ]
                                    ),
                                ),
                            ),
                            hostname=Equals(split_browse_root.hostname),
                            port=Equals(split_browse_root.port),
                        ),
                    ),
                    "git_path": Equals(ref.name),
                    "name": Equals("test-snap"),
                    "private": Is(True),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "target_architectures": Equals(["i386"]),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_url(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for a Git branch backed by a URL for an external repository.
        url = "https://git.example.org/foo"
        ref = self.factory.makeGitRefRemote(
            repository_url=url, path="refs/heads/master"
        )
        job = self.makeJob(git_ref=ref)
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        for archive_line in expected_archives:
            self.assertIn("universe", archive_line)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_source_tarball": Is(False),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(url),
                    "git_path": Equals("master"),
                    "name": Equals("test-snap"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "target_architectures": Equals(["i386"]),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_url_HEAD(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for the default branch in an external Git repository.
        url = "https://git.example.org/foo"
        ref = self.factory.makeGitRefRemote(repository_url=url, path="HEAD")
        job = self.makeJob(git_ref=ref)
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        for archive_line in expected_archives:
            self.assertIn("universe", archive_line)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_source_tarball": Is(False),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(url),
                    "name": Equals("test-snap"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "target_architectures": Equals(["i386"]),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_prefers_store_name(self):
        # For the "name" argument, extraBuildArgs prefers Snap.store_name
        # over Snap.name if the former is set.
        job = self.makeJob(store_name="something-else")
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual("something-else", args["name"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archive_trusted_keys(self):
        # If the archive has a signing key, extraBuildArgs sends it.
        yield self.useFixture(InProcessKeyServerFixture()).start()
        archive = self.factory.makeArchive()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(archive).setSigningKey(
            key_path, async_keyserver=True
        )
        job = self.makeJob(archive=archive)
        self.factory.makeBinaryPackagePublishingHistory(
            distroarchseries=job.build.distro_arch_series,
            pocket=job.build.pocket,
            archive=archive,
            status=PackagePublishingStatus.PUBLISHED,
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
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

    @defer.inlineCallbacks
    def test_extraBuildArgs_tools_source_channels_apt(self):
        # If snapcraft is being installed from apt, extraBuildArgs sends an
        # extra archive to provide updates.
        yield self.useFixture(InProcessKeyServerFixture()).start()
        tools_source = (
            "deb http://ppa.launchpad.net/snappy-dev/snapcraft-daily/ubuntu "
            "%(series)s main"
        )
        tools_fingerprint = "A419AE861E88BC9E04B9C26FBA2B9389DFD20543"
        self.pushConfig(
            "snappy",
            tools_source=tools_source,
            tools_fingerprint=tools_fingerprint,
        )
        import_public_key("test@canonical.com")
        job = self.makeJob(channels={"snapcraft": "apt"})
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual(
            tools_source % {"series": job.build.distro_series.name},
            args["archives"][0],
        )
        self.assertThat(
            args["trusted_keys"],
            MatchesListwise(
                [
                    Base64KeyMatches(
                        "A419AE861E88BC9E04B9C26FBA2B9389DFD20543"
                    ),
                ]
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_tools_source_channels_snap(self):
        # If snapcraft is being installed from apt, extraBuildArgs ignores
        # tools_source.
        yield self.useFixture(InProcessKeyServerFixture()).start()
        tools_source = (
            "deb http://ppa.launchpad.net/snappy-dev/snapcraft-daily/ubuntu "
            "%(series)s main"
        )
        tools_fingerprint = "A419AE861E88BC9E04B9C26FBA2B9389DFD20543"
        self.pushConfig(
            "snappy",
            tools_source=tools_source,
            tools_fingerprint=tools_fingerprint,
        )
        import_public_key("test@canonical.com")
        job = self.makeJob(channels={"snapcraft": "stable"})
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertNotIn(tools_source, args["archives"])
        self.assertEqual([], args["trusted_keys"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_channels(self):
        # If the build needs particular channels, extraBuildArgs sends them.
        job = self.makeJob(channels={"snapcraft": "edge", "snapd": "edge"})
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertFalse(isProxy(args["channels"]))
        self.assertEqual(
            {"snapcraft": "edge", "snapd": "edge"}, args["channels"]
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_craft_platform(self):
        # If the build is for a particular platform, extraBuildArgs
        # sends it.
        job = self.makeJob(craft_platform="ubuntu-amd64")
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertFalse(isProxy(args["craft_platform"]))
        self.assertEqual("ubuntu-amd64", args["craft_platform"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_channels_apt(self):
        # {"snapcraft": "apt"} causes snapcraft to be installed from apt.
        job = self.makeJob(channels={"snapcraft": "apt"})
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertNotIn("channels", args)

    @defer.inlineCallbacks
    def test_extraBuildArgs_channels_feature_flag_real_channel(self):
        # If the snap.channels.snapcraft feature flag is set, it identifies
        # the default channel to be used for snapcraft.
        self.useFixture(
            FeatureFixture({SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG: "stable"})
        )
        job = self.makeJob()
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertFalse(isProxy(args["channels"]))
        self.assertEqual({"snapcraft": "stable"}, args["channels"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_channels_feature_flag_overridden(self):
        # The snap.channels.snapcraft feature flag can be overridden by
        # explicit configuration.
        self.useFixture(
            FeatureFixture({SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG: "stable"})
        )
        job = self.makeJob(channels={"snapcraft": "apt"})
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertNotIn("channels", args)

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_primary(self):
        # If the build is configured to use the primary archive as its
        # source, then by default it uses the release, security, and updates
        # pockets.
        job = self.makeJob()
        expected_archives = [
            "deb %s %s main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-updates main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            extra_args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_primary_non_default_pocket(self):
        # If the build is configured to use the primary archive as its
        # source, and it uses a non-default pocket, then it uses the
        # corresponding expanded pockets in the primary archive.
        job = self.makeJob(pocket=PackagePublishingPocket.SECURITY)
        expected_archives = [
            "deb %s %s main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            extra_args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_ppa(self):
        # If the build is configured to use a PPA as its source, then by
        # default it uses the release pocket in its source PPA, and the
        # release, security, and updates pockets in the primary archive.
        archive = self.factory.makeArchive()
        job = self.makeJob(archive=archive)
        self.factory.makeBinaryPackagePublishingHistory(
            archive=archive,
            distroarchseries=job.build.distro_series.architectures[0],
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        primary = job.build.distribution.main_archive
        expected_archives = [
            "deb %s %s main"
            % (archive.archive_url, job.build.distro_series.name),
            "deb %s %s main universe"
            % (primary.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (primary.archive_url, job.build.distro_series.name),
            "deb %s %s-updates main universe"
            % (primary.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            extra_args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives_ppa_with_archive_dependencies(self):
        # If the build is configured to use a PPA as its source, and it has
        # archive dependencies, then they are honoured.
        archive = self.factory.makeArchive()
        lower_archive = self.factory.makeArchive(
            distribution=archive.distribution
        )
        job = self.makeJob(archive=archive)
        self.factory.makeBinaryPackagePublishingHistory(
            archive=archive,
            distroarchseries=job.build.distro_series.architectures[0],
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=lower_archive,
            distroarchseries=job.build.distro_series.architectures[0],
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        primary = job.build.distribution.main_archive
        archive.addArchiveDependency(
            lower_archive, PackagePublishingPocket.RELEASE
        )
        archive.addArchiveDependency(primary, PackagePublishingPocket.SECURITY)
        expected_archives = [
            "deb %s %s main"
            % (archive.archive_url, job.build.distro_series.name),
            "deb %s %s main"
            % (lower_archive.archive_url, job.build.distro_series.name),
            "deb %s %s main universe"
            % (primary.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (primary.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            extra_args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, extra_args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_snap_base_with_archive_dependencies(self):
        # If the build is using a snap base that has archive dependencies,
        # extraBuildArgs sends them.
        snap_base = self.factory.makeSnapBase()
        job = self.makeJob(snap_base=snap_base)
        dependency = self.factory.makeArchive(
            distribution=job.archive.distribution
        )
        snap_base.addArchiveDependency(
            dependency,
            PackagePublishingPocket.RELEASE,
            getUtility(IComponentSet)["main"],
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=dependency,
            distroarchseries=job.build.distro_arch_series,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        expected_archives = [
            "deb %s %s main"
            % (dependency.archive_url, job.build.distro_series.name),
            "deb %s %s main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-updates main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_snap_base_with_private_archive_dependencies(self):
        # If the build is using a snap base that has archive dependencies on
        # private PPAs, extraBuildArgs excludes them.
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        snap_base = self.factory.makeSnapBase()
        job = self.makeJob(snap_base=snap_base)
        dependency = self.factory.makeArchive(
            distribution=job.archive.distribution, private=True
        )
        snap_base.addArchiveDependency(
            dependency,
            PackagePublishingPocket.RELEASE,
            getUtility(IComponentSet)["main"],
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=dependency,
            distroarchseries=job.build.distro_arch_series,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        job.build.updateStatus(BuildStatus.BUILDING)

        expected_archives = [
            "deb %s %s main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-updates main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
        ]
        self.assertEqual(expected_archives, args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_pro_enabled_snap_base_private_dependencies(self):
        # If the build is using a snap base that has archive dependencies on
        # private PPAs, extraBuildArgs doesn't exclude them if snap base is
        # pro-enabled
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        snap_base = self.factory.makeSnapBase()
        job = self.makeJob(snap_base=snap_base, pro_enable=True)
        dependency = self.factory.makeArchive(
            distribution=job.archive.distribution, private=True
        )
        snap_base.addArchiveDependency(
            dependency,
            PackagePublishingPocket.RELEASE,
            getUtility(IComponentSet)["main"],
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=dependency,
            distroarchseries=job.build.distro_arch_series,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        job.build.updateStatus(BuildStatus.BUILDING)

        self.assertThat(
            [SourceEntry(item) for item in args["archives"]],
            MatchesListwise(
                [
                    # Private dependency
                    MatchesStructure(
                        type=Equals("deb"),
                        uri=AfterPreprocessing(
                            urlsplit,
                            MatchesStructure(
                                scheme=Equals("http"),
                                username=Equals("buildd"),
                                password=MacaroonVerifies(
                                    "snap-build", dependency
                                ),
                                hostname=Equals("private-ppa.launchpad.test"),
                                path=Equals(
                                    "/%s/%s/%s"
                                    % (
                                        dependency.owner.name,
                                        dependency.name,
                                        dependency.distribution.name,
                                    )
                                ),
                            ),
                        ),
                        dist=Equals(job.build.distro_series.name),
                        comps=Equals(["main"]),
                    ),
                    # Non private dependencies
                    MatchesStructure.byEquality(
                        type="deb",
                        uri=job.archive.archive_url,
                        dist=job.build.distro_series.name,
                        comps=["main", "universe"],
                    ),
                    MatchesStructure.byEquality(
                        type="deb",
                        uri=job.archive.archive_url,
                        dist="%s-security" % job.build.distro_series.name,
                        comps=["main", "universe"],
                    ),
                    MatchesStructure.byEquality(
                        type="deb",
                        uri=job.archive.archive_url,
                        dist="%s-updates" % job.build.distro_series.name,
                        comps=["main", "universe"],
                    ),
                ]
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_ppa_and_snap_base_with_archive_dependencies(self):
        # If the build is using a PPA and a snap base that both have archive
        # dependencies, extraBuildArgs sends them all.
        snap_base = self.factory.makeSnapBase()
        upper_archive = self.factory.makeArchive()
        lower_archive = self.factory.makeArchive(
            distribution=upper_archive.distribution
        )
        snap_base_archive = self.factory.makeArchive(
            distribution=upper_archive.distribution
        )
        job = self.makeJob(archive=upper_archive, snap_base=snap_base)
        primary = job.build.distribution.main_archive
        for archive in (upper_archive, lower_archive, snap_base_archive):
            self.factory.makeBinaryPackagePublishingHistory(
                archive=archive,
                distroarchseries=job.build.distro_arch_series,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            )
        upper_archive.addArchiveDependency(
            lower_archive, PackagePublishingPocket.RELEASE
        )
        snap_base.addArchiveDependency(
            snap_base_archive,
            PackagePublishingPocket.RELEASE,
            getUtility(IComponentSet)["main"],
        )
        expected_archives = [
            "deb %s %s main"
            % (upper_archive.archive_url, job.build.distro_series.name),
            "deb %s %s main"
            % (lower_archive.archive_url, job.build.distro_series.name),
            "deb %s %s main"
            % (snap_base_archive.archive_url, job.build.distro_series.name),
            "deb %s %s main universe"
            % (primary.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (primary.archive_url, job.build.distro_series.name),
            "deb %s %s-updates main universe"
            % (primary.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_snap_base_without_archive_dependencies(self):
        # If the build is using a snap base that does not have archive
        # dependencies, extraBuildArgs only sends the base archive.
        snap_base = self.factory.makeSnapBase()
        job = self.makeJob(snap_base=snap_base)
        expected_archives = [
            "deb %s %s main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-security main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
            "deb %s %s-updates main universe"
            % (job.archive.archive_url, job.build.distro_series.name),
        ]
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual(expected_archives, args["archives"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_disallow_internet(self):
        # If external network access is not allowed for the snap,
        # extraBuildArgs does not dispatch a proxy token.
        job = self.makeJob(allow_internet=False)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)

    @defer.inlineCallbacks
    def test_extraBuildArgs_build_source_tarball(self):
        # If the snap requests building of a source tarball, extraBuildArgs
        # sends the appropriate arguments.
        job = self.makeJob(build_source_tarball=True)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertTrue(args["build_source_tarball"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_private(self):
        # If the snap is private, extraBuildArgs sends the appropriate
        # arguments.
        job = self.makeJob(private=True)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertTrue(args["private"])

    def test_endProxySession(self):
        branch = self.factory.makeBranch()
        job = self.makeJob(branch=branch)
        yield job.extraBuildArgs()

        # End the session
        yield job.endProxySession(upload_path="test_path")

    @defer.inlineCallbacks
    def test_composeBuildRequest_proxy_url_set(self):
        job = self.makeJob()
        build_request = yield job.composeBuildRequest(None)
        self.assertThat(
            build_request[4]["proxy_url"], ProxyURLMatcher(job, self.now)
        )

    @defer.inlineCallbacks
    def test_composeBuildRequest_deleted(self):
        # If the source branch/repository has been deleted,
        # composeBuildRequest raises CannotBuild.
        branch = self.factory.makeBranch()
        owner = self.factory.makePerson(name="snap-owner")
        job = self.makeJob(registrant=owner, owner=owner, branch=branch)
        branch.destroySelf(break_references=True)
        self.assertIsNone(job.build.snap.branch)
        self.assertIsNone(job.build.snap.git_repository)
        expected_exception_msg = (
            "Source branch/repository for "
            "~snap-owner/test-snap has been deleted."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.composeBuildRequest(None)

    @defer.inlineCallbacks
    def test_composeBuildRequest_git_ref_deleted(self):
        # If the source Git reference has been deleted, composeBuildRequest
        # raises CannotBuild.
        repository = self.factory.makeGitRepository()
        [ref] = self.factory.makeGitRefs(repository=repository)
        owner = self.factory.makePerson(name="snap-owner")
        job = self.makeJob(registrant=owner, owner=owner, git_ref=ref)
        repository.removeRefs([ref.path])
        self.assertIsNone(job.build.snap.git_ref)
        expected_exception_msg = (
            "Source branch/repository for "
            "~snap-owner/test-snap has been deleted."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.composeBuildRequest(None)

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_prefers_lxd(self):
        job = self.makeJob(allow_internet=False)
        builder = MockBuilder()
        builder.processor = job.build.processor
        worker = OkWorker()
        job.setBuilder(builder, worker)
        chroot_lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(
            chroot_lfa, image_type=BuildBaseImageType.CHROOT
        )
        lxd_lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(
            lxd_lfa, image_type=BuildBaseImageType.LXD
        )
        yield job.dispatchBuildToWorker(DevNullLogger())
        self.assertEqual(
            ("ensurepresent", lxd_lfa.http_url, "", ""), worker.call_log[0]
        )
        build_calls = self.filterStatsdCallsByName("build.")
        self.assertEqual(1, len(build_calls))
        self.assertEqual(
            build_calls[0],
            f"build.count,builder_name={builder.name},env=test,"
            f"job_type=SNAPBUILD,region={builder.region}",
        )

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_falls_back_to_chroot(self):
        job = self.makeJob(allow_internet=False)
        builder = MockBuilder()
        builder.processor = job.build.processor
        worker = OkWorker()
        job.setBuilder(builder, worker)
        chroot_lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(
            chroot_lfa, image_type=BuildBaseImageType.CHROOT
        )
        yield job.dispatchBuildToWorker(DevNullLogger())
        self.assertEqual(
            ("ensurepresent", chroot_lfa.http_url, "", ""), worker.call_log[0]
        )


class MakeSnapBuildMixin:
    """Provide the common makeBuild method returning a queued build."""

    def makeSnap(self):
        # We can't use self.pushConfig here since this is used in a
        # TrialTestCase instance.
        config_name = self.factory.getUniqueString()
        config.push(
            config_name,
            dedent(
                """
            [snappy]
            store_url: http://sca.example/
            store_upload_url: http://updown.example/
            """
            ),
        )
        self.addCleanup(config.pop, config_name)
        distroseries = self.factory.makeDistroSeries()
        snappyseries = self.factory.makeSnappySeries(
            usable_distro_series=[distroseries]
        )
        return self.factory.makeSnap(
            distroseries=distroseries,
            store_upload=True,
            store_series=snappyseries,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": Macaroon().serialize()},
        )

    def makeBuild(self):
        snap = self.makeSnap()
        build = self.factory.makeSnapBuild(
            requester=snap.registrant, snap=snap, status=BuildStatus.BUILDING
        )
        build.queueBuild()
        return build

    def makeUnmodifiableBuild(self):
        snap = self.makeSnap()
        build = self.factory.makeSnapBuild(
            requester=snap.registrant, snap=snap, status=BuildStatus.BUILDING
        )
        build.distro_series.status = SeriesStatus.OBSOLETE
        build.queueBuild()
        return build


class TestGetUploadMethodsForSnapBuild(
    MakeSnapBuildMixin, TestGetUploadMethodsMixin, TestCaseWithFactory
):
    """IPackageBuild.getUpload-related methods work with Snap builds."""


class TestVerifySuccessfulBuildForSnapBuild(
    MakeSnapBuildMixin, TestVerifySuccessfulBuildMixin, TestCaseWithFactory
):
    """IBuildFarmJobBehaviour.verifySuccessfulBuild works."""


class TestHandleStatusForSnapBuild(
    MakeSnapBuildMixin, TestHandleStatusMixin, TestCaseWithFactory
):
    """IPackageBuild.handleStatus works with Snap builds."""
