# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test charm recipe build behaviour."""

import base64
import json
import os.path
import time
import uuid
from datetime import datetime
from unittest.mock import MagicMock
from urllib.parse import urlsplit

from fixtures import MockPatch, TempDir
from pymacaroons import Macaroon
from testtools import ExpectedException
from testtools.matchers import (
    ContainsDict,
    Equals,
    Is,
    IsInstance,
    MatchesDict,
    MatchesListwise,
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
from lp.buildmaster.builderproxy import (
    FetchServicePolicy,
    ProxyServiceException,
)
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
from lp.charms.interfaces.charmrecipe import (
    CHARM_RECIPE_ALLOW_CREATE,
    CHARM_RECIPE_PRIVATE_FEATURE_FLAG,
)
from lp.charms.model.charmrecipebuildbehaviour import CharmRecipeBuildBehaviour
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.features.testing import FeatureFixture
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.services.statsd.tests import StatsMixin
from lp.services.webapp import canonical_url
from lp.soyuz.adapters.archivedependencies import get_sources_list_for_building
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.tests.soyuz import Base64KeyMatches
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import dbuser
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import ZopelessDatabaseLayer


class TestCharmRecipeBuildBehaviourBase(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        super().setUp()

    def makeJob(self, distribution=None, **kwargs):
        """Create a sample `ICharmRecipeBuildBehaviour`."""
        if distribution is None:
            distribution = self.factory.makeDistribution(name="distro")
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

        build = self.factory.makeCharmRecipeBuild(
            distro_arch_series=distroarchseries, name="test-charm", **kwargs
        )
        return IBuildFarmJobBehaviour(build)


class TestCharmRecipeBuildBehaviour(TestCharmRecipeBuildBehaviourBase):
    def test_provides_interface(self):
        # CharmRecipeBuildBehaviour provides IBuildFarmJobBehaviour.
        job = CharmRecipeBuildBehaviour(None)
        self.assertProvides(job, IBuildFarmJobBehaviour)

    def test_adapts_ICharmRecipeBuild(self):
        # IBuildFarmJobBehaviour adapts an ICharmRecipeBuild.
        build = self.factory.makeCharmRecipeBuild()
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

    def test_verifyBuildRequest_no_chroot(self):
        # verifyBuildRequest raises when the DAS has no chroot.
        job = self.makeJob()
        builder = MockBuilder()
        job.setBuilder(builder, OkWorker())
        logger = BufferLogger()
        e = self.assertRaises(CannotBuild, job.verifyBuildRequest, logger)
        self.assertIn("Missing chroot", str(e))


class TestAsyncCharmRecipeBuildBehaviour(
    StatsMixin, TestCharmRecipeBuildBehaviourBase
):
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    @defer.inlineCallbacks
    def setUp(self):
        super().setUp()
        build_username = "OCIBUILD-1"
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
        self.useFixture(MockPatch("time.time", return_value=self.now))
        self.addCleanup(shut_down_default_process_pool)
        self.setUpStats()

    def makeJob(self, **kwargs):
        # We need a builder in these tests, in order that requesting a proxy
        # token can piggyback on its reactor and pool.
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
                    Equals("charm"),
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
        job = self.makeJob()
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual([], self.proxy_api.tokens.requests)
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)

    @defer.inlineCallbacks
    def test_requestProxyToken_no_secret(self):
        self.pushConfig(
            "builddmaster", builder_proxy_auth_api_admin_secret=None
        )
        job = self.makeJob()
        expected_exception_msg = (
            "builder_proxy_auth_api_admin_secret is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestProxyToken(self):
        job = self.makeJob()
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
                            [Equals(b"application/json")]
                        ),
                    }
                ),
                "json": MatchesDict(
                    {"username": StartsWith(job.build.build_cookie + "-")}
                ),
            }
        )
        self.assertThat(
            self.proxy_api.tokens.requests,
            MatchesListwise([request_matcher]),
        )

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
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "channels": Equals({}),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(ref.repository.git_https_url),
                    "git_path": Equals(ref.name),
                    "name": Equals("test-charm"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
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
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "channels": Equals({}),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(ref.repository.git_https_url),
                    "name": Equals("test-charm"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_prefers_store_name(self):
        # For the "name" argument, extraBuildArgs prefers
        # CharmRecipe.store_name over CharmRecipe.name if the former is set.
        job = self.makeJob(store_name="something-else")
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual("something-else", args["name"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archive_trusted_keys(self):
        # If the archive has a signing key, extraBuildArgs sends it.
        yield self.useFixture(InProcessKeyServerFixture()).start()
        distribution = self.factory.makeDistribution()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(distribution.main_archive).setSigningKey(
            key_path, async_keyserver=True
        )
        job = self.makeJob(distribution=distribution)
        self.factory.makeBinaryPackagePublishingHistory(
            distroarchseries=job.build.distro_arch_series,
            pocket=job.build.pocket,
            archive=distribution.main_archive,
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
    def test_extraBuildArgs_channels(self):
        # If the build needs particular channels, extraBuildArgs sends them.
        job = self.makeJob(channels={"charmcraft": "edge"})
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertFalse(isProxy(args["channels"]))
        self.assertEqual({"charmcraft": "edge"}, args["channels"])

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
    def test_extraBuildArgs_archives_primary(self):
        # The build uses the release, security, and updates pockets from the
        # primary archive.
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
    def test_extraBuildArgs_build_path(self):
        # If the recipe specifies a build path, extraBuildArgs sends it.
        job = self.makeJob(build_path="src")
        (
            expected_archives,
            expected_trusted_keys,
        ) = yield get_sources_list_for_building(
            job, job.build.distro_arch_series, None
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual("src", args["build_path"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_private(self):
        # If the recipe is private, extraBuildArgs sends the appropriate
        # arguments.
        self.useFixture(
            FeatureFixture(
                {
                    CHARM_RECIPE_ALLOW_CREATE: "on",
                    CHARM_RECIPE_PRIVATE_FEATURE_FLAG: "on",
                }
            )
        )
        job = self.makeJob(information_type=InformationType.PROPRIETARY)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertTrue(args["private"])

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
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "channels": Equals({}),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(url),
                    "git_path": Equals("master"),
                    "name": Equals("test-charm"),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals("unstable"),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_composeBuildRequest_proxy_url_set(self):
        job = self.makeJob()
        build_request = yield job.composeBuildRequest(None)
        self.assertThat(
            build_request[4]["proxy_url"], ProxyURLMatcher(job, self.now)
        )
        self.assertFalse(build_request[4]["use_fetch_service"])

    @defer.inlineCallbacks
    def test_composeBuildRequest_git_ref_deleted(self):
        # If the source Git reference has been deleted, composeBuildRequest
        # raises CannotBuild.
        repository = self.factory.makeGitRepository()
        [ref] = self.factory.makeGitRefs(repository=repository)
        owner = self.factory.makePerson(name="charm-owner")
        project = self.factory.makeProduct(name="charm-project")
        job = self.makeJob(
            registrant=owner, owner=owner, project=project, git_ref=ref
        )
        repository.removeRefs([ref.path])
        self.assertIsNone(job.build.recipe.git_ref)
        expected_exception_msg = (
            r"Source repository for "
            r"~charm-owner/charm-project/\+charm/test-charm has been deleted."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.composeBuildRequest(None)

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_prefers_lxd(self):
        self.pushConfig("builddmaster", builder_proxy_host=None)
        job = self.makeJob()
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
            f"job_type=CHARMRECIPEBUILD,region={builder.region}",
        )

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_falls_back_to_chroot(self):
        self.pushConfig("builddmaster", builder_proxy_host=None)
        job = self.makeJob()
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


class TestAsyncCharmRecipeBuildBehaviourFetchService(
    StatsMixin,
    TestCharmRecipeBuildBehaviourBase,
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
        self.useFixture(MockPatch("time.time", return_value=self.now))
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
        """Create a charm recipe build request with an incomplete fetch service
        configuration.

        If `fetch_service_host` is not provided the function will return
        without populating `proxy_url` and `revocation_endpoint`.
        """
        self.pushConfig("builddmaster", fetch_service_host=None)
        job = self.makeJob(use_fetch_service=True)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual([], self.fetch_service_api.sessions.requests)
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_no_certificate(self):
        """Create a charm recipe build request with an incomplete fetch service
        configuration.

        If `fetch_service_mitm_certificate` is not provided
        the function raises a `CannotBuild` error.
        """
        self.pushConfig("builddmaster", fetch_service_mitm_certificate=None)
        job = self.makeJob(use_fetch_service=True)
        expected_exception_msg = (
            "fetch_service_mitm_certificate is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestFetchServiceSession_no_secret(self):
        """Create a charm recipe build request with an incomplete fetch service
        configuration.

        If `fetch_service_control_admin_secret` is not provided
        the function raises a `CannotBuild` error.
        """
        self.pushConfig(
            "builddmaster", fetch_service_control_admin_secret=None
        )
        job = self.makeJob(use_fetch_service=True)
        expected_exception_msg = (
            "fetch_service_control_admin_secret is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestFetchServiceSession(self):
        """Create a charm recipe build request with a successful fetch service
        configuration.

        `proxy_url` and `revocation_endpoint` are correctly populated.
        """
        job = self.makeJob(use_fetch_service=True)
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
        """Create a charm recipe build request with a successful fetch service
        configuration.

        `proxy_url` and `revocation_endpoint` are correctly populated.
        """
        job = self.makeJob(
            use_fetch_service=True,
            fetch_service_policy=FetchServicePolicy.PERMISSIVE,
        )
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

        job = self.makeJob(use_fetch_service=True)
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
        tem_upload_path = self.useFixture(TempDir()).path

        job = self.makeJob(use_fetch_service=True)

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
        self.assertTrue(os.path.exists(expected_file_path))
        with open(expected_file_path) as f:
            self.assertEqual(
                json.dumps(self.fetch_service_api.sessions.responses[1]),
                f.read(),
            )

    @defer.inlineCallbacks
    def test_endProxySession_fetch_Service_false(self):
        """When `use_fetch_service` is False, we don't make any calls to the
        fetch service API."""

        job = self.makeJob(use_fetch_service=False)

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
    def test_endProxySession_bad_revocation_endpoint(self):
        """When `revocation_endpoint` is not properly set, it should raise
        an exception."""

        job = self.makeJob(use_fetch_service=True)
        job._worker.proxy_info = MagicMock(
            return_value={
                "use_fetch_service": True,
                "revocation_endpoint": "http://bad/endpoint",
            }
        )
        yield job.extraBuildArgs()
        with ExpectedException(ProxyServiceException):
            yield job.endProxySession(upload_path="ignored")


class MakeCharmRecipeBuildMixin:
    """Provide the common makeBuild method returning a queued build."""

    def makeCharmRecipe(self):
        self.useFixture(FeatureFixture({CHARM_RECIPE_ALLOW_CREATE: "on"}))
        return self.factory.makeCharmRecipe(
            store_upload=True,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": Macaroon().serialize()},
        )

    def makeBuild(self):
        recipe = self.makeCharmRecipe()
        build = self.factory.makeCharmRecipeBuild(
            requester=recipe.registrant,
            recipe=recipe,
            status=BuildStatus.BUILDING,
        )
        build.queueBuild()
        return build

    def makeUnmodifiableBuild(self):
        recipe = self.makeCharmRecipe()
        build = self.factory.makeCharmRecipeBuild(
            requester=recipe.registrant,
            recipe=recipe,
            status=BuildStatus.BUILDING,
        )
        build.distro_series.status = SeriesStatus.OBSOLETE
        build.queueBuild()
        return build


class TestGetUploadMethodsForCharmRecipeBuild(
    MakeCharmRecipeBuildMixin, TestGetUploadMethodsMixin, TestCaseWithFactory
):
    """IPackageBuild.getUpload* methods work with charm recipe builds."""


class TestVerifySuccessfulBuildForCharmRecipeBuild(
    MakeCharmRecipeBuildMixin,
    TestVerifySuccessfulBuildMixin,
    TestCaseWithFactory,
):
    """IBuildFarmJobBehaviour.verifySuccessfulBuild works."""


class TestHandleStatusForCharmRecipeBuild(
    MakeCharmRecipeBuildMixin, TestHandleStatusMixin, TestCaseWithFactory
):
    """IPackageBuild.handleStatus works with charm recipe builds."""
