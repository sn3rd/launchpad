# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test CI build behaviour."""

import base64
import json
import os.path
import re
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from fixtures import MockPatch
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

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    IArchiveGPGSigningKey,
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
from lp.code.enums import RevisionStatusResult
from lp.code.model.cibuildbehaviour import CIBuildBehaviour, build_secrets
from lp.services.authserver.testing import InProcessAuthServerFixture
from lp.services.config import config
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.services.statsd.tests import StatsMixin
from lp.services.webapp import canonical_url
from lp.soyuz.adapters.archivedependencies import get_sources_list_for_building
from lp.soyuz.enums import PackagePublishingStatus
from lp.soyuz.tests.soyuz import Base64KeyMatches
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.dbuser import dbuser
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import BaseLayer, ZopelessDatabaseLayer


class TestCIBuildBehaviourBase(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def makeJob(self, **kwargs):
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        distroseries = self.factory.makeDistroSeries(distribution=ubuntu)
        processor = getUtility(IProcessorSet).getByName("386")
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )

        # Taken from test_archivedependencies.py
        for component_name in ("main", "universe"):
            self.factory.makeComponentSelection(distroseries, component_name)

        build = self.factory.makeCIBuild(
            distro_arch_series=distroarchseries, **kwargs
        )
        return IBuildFarmJobBehaviour(build)


class TestCIBuildBehaviour(TestCIBuildBehaviourBase):
    def test_provides_interface(self):
        # CIBuildBehaviour provides IBuildFarmJobBehaviour.
        job = CIBuildBehaviour(None)
        self.assertProvides(job, IBuildFarmJobBehaviour)

    def test_adapts_ICIBuild(self):
        # IBuildFarmJobBehaviour adapts an ICIBuild.
        build = self.factory.makeCIBuild()
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


_unset = object()


class TestAsyncCIBuildBehaviour(StatsMixin, TestCIBuildBehaviourBase):
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    @defer.inlineCallbacks
    def setUp(self):
        super().setUp()
        build_username = "CIBUILD-1"
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
        self.pushConfig(
            "artifactory",
            base_url="canonical.artifactory.com",
            read_credentials="user:pass",
        )
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
                    Equals("ci"),
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
            self.proxy_api.tokens.requests, MatchesListwise([request_matcher])
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for a Git commit.
        job = self.makeJob(stages=[[("test", 0)]])
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
                    "fast_cleanup": Is(True),
                    "git_path": Equals(job.build.commit_sha1),
                    "git_repository": Equals(
                        job.build.git_repository.git_https_url
                    ),
                    "jobs": Equals([[["test", 0]]]),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals(job.build.distro_series.name),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_for_non_soss_distribution(self):
        # create a distribution with a name other than "soss", see
        # schema-lazr.conf -> cibuild.soss
        # for this distribution neither Artifactory environment variables nor
        # apt repositories will be dispatched

        # we need to provide the soss specific data anyway and then make sure
        # it is not accessible
        self.pushConfig(
            "cibuild.soss",
            environment_variables=json.dumps(
                {
                    "PIP_INDEX_URL": "https://%(read_auth)s@canonical.example.com/artifactory/api/pypi/soss-python-stable/simple/",  # noqa: E501
                    "SOME_PATH": "/bin/zip",
                }
            ),
            package_repositories=json.dumps(
                [
                    "deb https://%(read_auth)s@canonical.example.com/artifactory/soss-deb-stable focal main universe",  # noqa: E501
                    "deb https://public_ppa.example.net/repository focal main",
                ]
            ),
            plugin_settings=json.dumps(
                {
                    "miniconda_conda_channel": "https://%(read_auth)s@canonical.example.com/artifactory/soss-conda-stable-local/",  # noqa: E501
                    "foo": "bar",
                }
            ),
            secrets=json.dumps({"soss_read_auth": "%(read_auth)s"}),
        )
        package = self.factory.makeDistributionSourcePackage(
            distribution=self.factory.makeDistribution(name="distribution-123")
        )
        git_repository = self.factory.makeGitRepository(target=package)
        job = self.makeJob(
            stages=[[("test", 0)]], git_repository=git_repository
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        # make sure the distribution specific additional args are included
        # but have no values set
        self.assertEqual({}, args["environment_variables"])
        self.assertEqual([], args["package_repositories"])
        self.assertEqual({}, args["plugin_settings"])
        self.assertEqual({}, args["secrets"])
        self.assertFalse(args["scan_malware"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_no_artifactory_configuration(self):
        # If the cibuild.* section exists for the distribution but has no
        # relevant configuration entries, neither Artifactory environment
        # variables nor apt repositories will be dispatched.
        package = self.factory.makeDistributionSourcePackage(
            distribution=self.factory.makeDistribution(name="soss")
        )
        git_repository = self.factory.makeGitRepository(target=package)
        job = self.makeJob(
            stages=[[("test", 0)]], git_repository=git_repository
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual({}, args["environment_variables"])
        self.assertNotIn([], args["package_repositories"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_include_artifactory_configuration(self):
        # we use the `soss` distribution which refers to `cibuild.soss` in the
        # global configuration
        # suitable Artifactory configuration data will be dispatched for this
        # distribution
        self.pushConfig(
            "cibuild.soss",
            environment_variables=json.dumps(
                {
                    "PIP_INDEX_URL": "https://%(read_auth)s@canonical.example.com/artifactory/api/pypi/soss-python-stable/simple/",  # noqa: E501
                    "SOME_PATH": "/bin/zip",
                }
            ),
            package_repositories=json.dumps(
                [
                    "deb https://%(read_auth)s@canonical.example.com/artifactory/soss-deb-stable focal main universe",  # noqa: E501
                    "deb https://public_ppa.example.net/repository focal main",
                ]
            ),
            plugin_settings=json.dumps(
                {
                    "miniconda_conda_channel": "https://%(read_auth)s@canonical.example.com/artifactory/soss-conda-stable-local/",  # noqa: E501
                    "foo": "bar",
                }
            ),
            secrets=json.dumps({"soss_read_auth": "%(read_auth)s"}),
        )
        package = self.factory.makeDistributionSourcePackage(
            distribution=self.factory.makeDistribution(name="soss")
        )
        git_repository = self.factory.makeGitRepository(target=package)
        job = self.makeJob(
            stages=[[("test", 0)]], git_repository=git_repository
        )
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
                    "fast_cleanup": Is(True),
                    "git_path": Equals(job.build.commit_sha1),
                    "git_repository": Equals(
                        job.build.git_repository.git_https_url
                    ),
                    "jobs": Equals([[["test", 0]]]),
                    "private": Is(False),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "scan_malware": Is(False),
                    "series": Equals(job.build.distro_series.name),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "environment_variables": Equals(
                        {
                            "PIP_INDEX_URL": "https://user:pass@canonical.example.com/artifactory/api/pypi/soss-python-stable/simple/",  # noqa: E501
                            "SOME_PATH": "/bin/zip",
                        }
                    ),
                    "package_repositories": Equals(
                        [
                            "deb https://user:pass@canonical.example.com/artifactory/soss-deb-stable focal main universe",  # noqa: E501
                            "deb https://public_ppa.example.net/repository focal main",  # noqa: E501
                        ]
                    ),
                    "plugin_settings": Equals(
                        {
                            "miniconda_conda_channel": "https://user:pass@canonical.example.com/artifactory/soss-conda-stable-local/",  # noqa: E501
                            "foo": "bar",
                        }
                    ),
                    "secrets": Equals({"soss_read_auth": "user:pass"}),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_redacts_secrets(self):
        self.pushConfig(
            "cibuild.soss",
            secrets=json.dumps(
                {
                    "soss_read_auth": "%(read_auth)s",
                    "more_secrets": "confidential",
                }
            ),
        )
        self.pushConfig("builddmaster", builder_proxy_host=None)

        package = self.factory.makeDistributionSourcePackage(
            distribution=self.factory.makeDistribution(name="soss")
        )
        git_repository = self.factory.makeGitRepository(target=package)
        job = self.makeJob(
            stages=[[("test", 0)]], git_repository=git_repository
        )

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
        logger = BufferLogger()
        yield job.dispatchBuildToWorker(logger)

        # Secrets are redacted in the log output.
        self.assertIn("'soss_read_auth': '<redacted>'", logger.getLogBuffer())
        self.assertIn("'more_secrets': '<redacted>'", logger.getLogBuffer())

        # ... but not in the arguments dispatched to the worker.
        self.assertEqual(
            {"soss_read_auth": "user:pass", "more_secrets": "confidential"},
            worker.call_log[1][5]["secrets"],
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_scan_malware(self):
        self.pushConfig("cibuild.soss", scan_malware=True)
        package = self.factory.makeDistributionSourcePackage(
            distribution=self.factory.makeDistribution(name="soss")
        )
        git_repository = self.factory.makeGitRepository(target=package)
        job = self.makeJob(
            stages=[[("test", 0)]], git_repository=git_repository
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertTrue(args["scan_malware"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_archive_trusted_keys(self):
        # If the archive has a signing key, extraBuildArgs sends it.
        yield self.useFixture(InProcessKeyServerFixture()).start()
        job = self.makeJob()
        distribution = job.build.distribution
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(distribution.main_archive).setSigningKey(
            key_path, async_keyserver=True
        )
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
    def test_extraBuildArgs_private(self):
        # If the repository is private, extraBuildArgs sends the appropriate
        # arguments.
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        repository = self.factory.makeGitRepository(
            information_type=InformationType.USERDATA
        )
        job = self.makeJob(git_repository=repository, stages=[[("test", 0)]])
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
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_path": Equals(job.build.commit_sha1),
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
                                    identifier=Equals("ci-build"),
                                    caveats=MatchesListwise(
                                        [
                                            MatchesStructure.byEquality(
                                                caveat_id="lp.ci-build %s"
                                                % job.build.id
                                            ),
                                        ]
                                    ),
                                ),
                            ),
                            hostname=Equals(split_browse_root.hostname),
                            port=Equals(split_browse_root.port),
                            path=Equals(
                                "/" + job.build.git_repository.shortened_path
                            ),
                        ),
                    ),
                    "jobs": Equals([[["test", 0]]]),
                    "private": Is(True),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals(job.build.distro_series.name),
                    "trusted_keys": Equals(expected_trusted_keys),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_builder_constraints(self):
        git_repository = self.factory.makeGitRepository(
            builder_constraints=["gpu"]
        )
        job = self.makeJob(
            stages=[[("test", 0)]], git_repository=git_repository
        )
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertEqual(["gpu"], args["builder_constraints"])

    @defer.inlineCallbacks
    def test_composeBuildRequest_proxy_url_set(self):
        job = self.makeJob()
        build_request = yield job.composeBuildRequest(None)
        self.assertThat(
            build_request[4]["proxy_url"], ProxyURLMatcher(job, self.now)
        )

    @defer.inlineCallbacks
    def test_composeBuildRequest_no_stages_defined(self):
        # If the build has no stages, composeBuildRequest raises CannotBuild.
        job = self.makeJob(stages=[])
        expected_exception_msg = re.escape(
            "No stages defined for %s:%s"
            % (job.build.git_repository.unique_name, job.build.commit_sha1)
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
        build_calls = self.filterStatsdCallsByName("build")
        self.assertEqual(1, len(build_calls))
        self.assertEqual(
            build_calls[0],
            f"build.count,builder_name={builder.name},env=test,"
            f"job_type=CIBUILD,region={builder.region}",
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


class MakeCIBuildMixin:
    """Provide the common makeBuild method returning a queued build."""

    def makeBuild(self):
        build = self.factory.makeCIBuild(status=BuildStatus.BUILDING)
        build.queueBuild()
        return build

    def makeUnmodifiableBuild(self):
        self.skipTest("Not relevant for CI builds.")


class TestGetUploadMethodsForCIBuild(
    MakeCIBuildMixin, TestGetUploadMethodsMixin, TestCaseWithFactory
):
    """IPackageBuild.getUpload* methods work with CI builds."""


class TestVerifySuccessfulBuildForCIBuild(
    MakeCIBuildMixin, TestVerifySuccessfulBuildMixin, TestCaseWithFactory
):
    """IBuildFarmJobBehaviour.verifySuccessfulBuild works with CI builds."""


class TestHandleStatusForCIBuild(
    MakeCIBuildMixin, TestHandleStatusMixin, TestCaseWithFactory
):
    """IPackageBuild.handleStatus works with CI builds."""

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_OK_with_jobs(self):
        # If the worker status includes a "jobs" item, then we additionally
        # save that as the build's results and update its reports.
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.OK",
                    "filemap": {"build:0.log": "test_file_hash"},
                    "jobs": {
                        "build:0": {
                            "log": "test_file_hash",
                            "result": "SUCCEEDED",
                        },
                    },
                },
            )
        self.assertEqual(
            {"build:0": {"log": "test_file_hash", "result": "SUCCEEDED"}},
            self.build.results,
        )
        self.assertEqual(
            RevisionStatusResult.SUCCEEDED,
            self.build.getOrCreateRevisionStatusReport("build:0").result,
        )

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_PACKAGEFAIL_with_jobs(self):
        # If the worker status includes a "jobs" item, then we additionally
        # save that as the build's results and update its reports, even if
        # the build failed.
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.PACKAGEFAIL",
                    "filemap": {"build:0.log": "test_file_hash"},
                    "jobs": {
                        "build:0": {
                            "log": "test_file_hash",
                            "result": "FAILED",
                        },
                    },
                },
            )
        self.assertEqual(
            {"build:0": {"log": "test_file_hash", "result": "FAILED"}},
            self.build.results,
        )
        self.assertEqual(
            RevisionStatusResult.FAILED,
            self.build.getOrCreateRevisionStatusReport("build:0").result,
        )


class TestBuildSecrets(TestCase):
    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.pushConfig(
            "artifactory",
            base_url="canonical.artifactory.com",
            read_credentials="user:pass",
        )
        self.pushConfig(
            "cibuild.soss",
            secrets=json.dumps({"soss_read_auth": "%(read_auth)s"}),
        )

    def test_with_soss_distribution(self):
        # builds for the soss distribution have access to the soss secrets
        rv = build_secrets(distribution_name="soss")

        self.assertEqual({"soss_read_auth": "user:pass"}, rv)

    def test_with_other_than_soss_distribution(self):
        # builds for other distributions do not have access to the soss secrets
        rv = build_secrets(distribution_name="some-other-distribution")

        self.assertEqual({}, rv)
