# Copyright 2015-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `OCIRecipeBuildBehaviour`."""

import base64
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import fixtures
import six
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
    MatchesSetwise,
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
from lp.buildmaster.enums import BuildBaseImageType, BuildStatus
from lp.buildmaster.interactor import (
    BuilderInteractor,
    shut_down_default_process_pool,
)
from lp.buildmaster.interfaces.builder import BuildDaemonError, CannotBuild
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.interfaces.buildqueue import IBuildQueueSet
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.buildmaster.tests.builderproxy import (
    InProcessProxyAuthAPIFixture,
    ProxyURLMatcher,
    RevocationEndpointMatcher,
)
from lp.buildmaster.tests.mock_workers import (
    MockBuilder,
    OkWorker,
    WaitingWorker,
    WorkerTestHelpers,
)
from lp.buildmaster.tests.test_buildfarmjobbehaviour import (
    TestGetUploadMethodsMixin,
)
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.oci.model.ocirecipebuildbehaviour import OCIRecipeBuildBehaviour
from lp.registry.interfaces.series import SeriesStatus
from lp.services.authserver.testing import InProcessAuthServerFixture
from lp.services.config import config
from lp.services.features.testing import FeatureFixture
from lp.services.log.logger import DevNullLogger
from lp.services.propertycache import get_property_cache
from lp.services.statsd.tests import StatsMixin
from lp.services.webapp import canonical_url
from lp.soyuz.adapters.archivedependencies import get_sources_list_for_building
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import dbuser
from lp.testing.fakemethod import FakeMethod
from lp.testing.layers import LaunchpadZopelessLayer, ZopelessDatabaseLayer
from lp.testing.mail_helpers import pop_notifications


class MakeOCIBuildMixin:
    def makeBuild(self):
        build = self.factory.makeOCIRecipeBuild()
        build.queueBuild()
        return build

    def makeUnmodifiableBuild(self):
        build = self.factory.makeOCIRecipeBuild()
        build.distro_arch_series = "failed"
        build.queueBuild()
        return build

    def makeJob(self, git_ref=None, recipe=None, build=None, **kwargs):
        """Create a sample `IOCIRecipeBuildBehaviour`."""
        if build is None:
            if recipe is None:
                build = self.factory.makeOCIRecipeBuild(**kwargs)
            else:
                build = self.factory.makeOCIRecipeBuild(
                    recipe=recipe, **kwargs
                )
        if git_ref is None:
            [git_ref] = self.factory.makeGitRefs()
        build.recipe.git_ref = git_ref
        build.recipe.build_args = {"BUILD_VAR": "123"}

        job = IBuildFarmJobBehaviour(build)
        builder = MockBuilder()
        builder.processor = job.build.processor
        worker = self.useFixture(WorkerTestHelpers()).getClientWorker()
        job.setBuilder(builder, worker)
        self.addCleanup(worker.pool.closeCachedConnections)
        self.addCleanup(shut_down_default_process_pool)

        # Taken from test_archivedependencies.py
        for component_name in ("main", "universe"):
            self.factory.makeComponentSelection(
                build.distro_arch_series.distroseries, component_name
            )

        return job


class TestOCIBuildBehaviour(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_provides_interface(self):
        # OCIRecipeBuildBehaviour provides IBuildFarmJobBehaviour.
        job = OCIRecipeBuildBehaviour(self.factory.makeOCIRecipeBuild())
        self.assertProvides(job, IBuildFarmJobBehaviour)

    def test_adapts_IOCIRecipeBuild(self):
        # IBuildFarmJobBehaviour adapts an IOCIRecipeBuild.
        build = self.factory.makeOCIRecipeBuild()
        job = IBuildFarmJobBehaviour(build)
        self.assertProvides(job, IBuildFarmJobBehaviour)


class TestAsyncOCIRecipeBuildBehaviour(
    StatsMixin, MakeOCIBuildMixin, TestCaseWithFactory
):
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )
    layer = ZopelessDatabaseLayer

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
        self.useFixture(fixtures.MockPatch("time.time", return_value=self.now))
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        self.addCleanup(shut_down_default_process_pool)
        self.setUpStats()

    def assertHasNoZopeSecurityProxy(self, data):
        """Makes sure that data doesn't contain a security proxy.

        `data` can be a list, a tuple, a dict or an ordinary value. This
        method checks `data` itself, and if it's a collection, it checks
        each item in it.
        """
        self.assertFalse(
            isProxy(data), "%s should not be a security proxy." % data
        )
        # If it's a collection, keep searching for proxies.
        if isinstance(data, (list, tuple)):
            for i in data:
                self.assertHasNoZopeSecurityProxy(i)
        elif isinstance(data, dict):
            for k, v in data.items():
                self.assertHasNoZopeSecurityProxy(k)
                self.assertHasNoZopeSecurityProxy(v)

    @defer.inlineCallbacks
    def test_composeBuildRequest(self):
        [ref] = self.factory.makeGitRefs(paths=["refs/heads/v1.0-20.04"])
        job = self.makeJob(git_ref=ref)
        lfa = self.factory.makeLibraryFileAlias(db_only=True)
        job.build.distro_arch_series.addOrUpdateChroot(lfa)
        build_request = yield job.composeBuildRequest(None)
        self.assertThat(
            build_request,
            MatchesListwise(
                [
                    Equals("oci"),
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
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref)
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
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref)
        expected_exception_msg = (
            "builder_proxy_auth_api_admin_secret is not configured."
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.extraBuildArgs()

    @defer.inlineCallbacks
    def test_requestProxyToken(self):
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref)
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

    def makeRecipe(self, processor_names, **kwargs):
        recipe = self.factory.makeOCIRecipe(**kwargs)
        processors_list = []
        distroseries = self.factory.makeDistroSeries(
            distribution=recipe.oci_project.distribution
        )
        for proc_name in processor_names:
            proc = getUtility(IProcessorSet).getByName(proc_name)
            distro = self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=proc_name,
                processor=proc,
            )
            distro.addOrUpdateChroot(
                self.factory.makeLibraryFileAlias(db_only=True)
            )
            processors_list.append(proc)
        recipe.setProcessors(processors_list)
        return recipe

    def makeBuildRequest(self, recipe, requester):
        build_request = recipe.requestBuilds(requester)
        # Create the builds for the build request, and set them at the build
        # request job.
        builds = recipe.requestBuildsFromJob(requester, build_request)
        job = removeSecurityProxy(build_request).job
        removeSecurityProxy(job).builds = builds
        return build_request

    def test_getBuildInfoArgs_with_build_request(self):
        owner = self.factory.makePerson()
        owner.setPreferredEmail(self.factory.makeEmail("owner@foo.com", owner))
        oci_project = self.factory.makeOCIProject(registrant=owner)
        recipe = self.makeRecipe(
            processor_names=["amd64", "386"],
            oci_project=oci_project,
            registrant=owner,
            owner=owner,
        )
        build_request = self.makeBuildRequest(recipe, recipe.owner)
        self.assertEqual(2, build_request.builds.count())
        build = build_request.builds[0]
        build_per_proc = {i.processor.name: i for i in build_request.builds}
        job = self.makeJob(build=build)

        self.assertThat(
            job._getBuildInfoArgs(),
            MatchesDict(
                {
                    "architectures": MatchesSetwise(
                        Equals("amd64"), Equals("386")
                    ),
                    "recipe_owner": Equals(
                        {"name": recipe.owner.name, "email": "owner@foo.com"}
                    ),
                    "build_request_id": Equals(build_request.id),
                    "build_requester": Equals(
                        {
                            "name": build.requester.name,
                            "email": "owner@foo.com",
                        }
                    ),
                    "build_request_timestamp": Equals(
                        build_request.date_requested.isoformat()
                    ),
                    "build_urls": MatchesDict(
                        {
                            "amd64": Equals(
                                canonical_url(build_per_proc["amd64"])
                            ),
                            "386": Equals(
                                canonical_url(build_per_proc["386"])
                            ),
                        }
                    ),
                }
            ),
        )

    def test_getBuildInfoArgs_hide_email(self):
        owner = self.factory.makePerson()
        owner.setPreferredEmail(self.factory.makeEmail("owner@foo.com", owner))
        owner.hide_email_addresses = True
        oci_project = self.factory.makeOCIProject(registrant=owner)
        recipe = self.makeRecipe(
            processor_names=["amd64"],
            oci_project=oci_project,
            registrant=owner,
            owner=owner,
        )
        build_request = self.makeBuildRequest(recipe, recipe.owner)
        build = build_request.builds[0]
        job = self.makeJob(build=build)

        self.assertThat(
            job._getBuildInfoArgs(),
            MatchesDict(
                {
                    "architectures": Equals(["amd64"]),
                    "recipe_owner": Equals(
                        {"name": recipe.owner.name, "email": None}
                    ),
                    "build_request_id": Equals(build_request.id),
                    "build_requester": Equals(
                        {"name": build.requester.name, "email": None}
                    ),
                    "build_request_timestamp": Equals(
                        build_request.date_requested.isoformat()
                    ),
                    "build_urls": MatchesDict(
                        {
                            "amd64": Equals(
                                canonical_url(build_request.builds[0])
                            )
                        }
                    ),
                }
            ),
        )

    def test_getBuildInfoArgs_from_teams(self):
        registrant = self.factory.makePerson()
        team = self.factory.makeTeam(members=[registrant])
        oci_project = self.factory.makeOCIProject(registrant=registrant)
        recipe = self.makeRecipe(
            processor_names=["amd64"],
            oci_project=oci_project,
            registrant=registrant,
            owner=team,
        )
        build_request = self.makeBuildRequest(recipe, recipe.owner)
        build = build_request.builds[0]
        job = self.makeJob(build=build)

        self.assertThat(
            job._getBuildInfoArgs(),
            MatchesDict(
                {
                    "architectures": Equals(["amd64"]),
                    "recipe_owner": Equals(
                        {"name": recipe.owner.name, "email": None}
                    ),
                    "build_request_id": Equals(build_request.id),
                    "build_requester": Equals(
                        {"name": build.requester.name, "email": None}
                    ),
                    "build_request_timestamp": Equals(
                        build_request.date_requested.isoformat()
                    ),
                    "build_urls": MatchesDict(
                        {
                            "amd64": Equals(
                                canonical_url(build_request.builds[0])
                            )
                        }
                    ),
                }
            ),
        )

    def test_getBuildInfoArgs_without_build_request(self):
        recipe = self.makeRecipe(processor_names=["amd64"])
        distro_arch_series = removeSecurityProxy(
            recipe.getAllowedArchitectures()[0]
        )
        build = self.factory.makeOCIRecipeBuild(
            recipe=recipe, distro_arch_series=distro_arch_series
        )
        job = self.makeJob(build=build)
        self.assertThat(
            job._getBuildInfoArgs(),
            ContainsDict(
                {
                    "architectures": Equals(["amd64"]),
                    "build_request_id": Equals(None),
                    "build_request_timestamp": Equals(None),
                    "build_urls": MatchesDict(
                        {"amd64": Equals(canonical_url(build))}
                    ),
                }
            ),
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
        # Asserts that nothing here is a zope proxy, to avoid errors when
        # serializing it for XML-RPC call.
        self.assertHasNoZopeSecurityProxy(args)
        arch_tag = job.build.distro_arch_series.architecturetag
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_file": Equals(job.build.recipe.build_file),
                    "build_args": Equals(
                        {"BUILD_VAR": "123", "LAUNCHPAD_BUILD_ARCH": arch_tag}
                    ),
                    "build_path": Equals(job.build.recipe.build_path),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(ref.repository.git_https_url),
                    "git_path": Equals(ref.name),
                    "name": Equals(job.build.recipe.name),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals(
                        job.build.distro_arch_series.distroseries.name
                    ),
                    "trusted_keys": Equals(expected_trusted_keys),
                    # 'metadata' has detailed tests in
                    # TestAsyncOCIBuildBehaviour.
                    "metadata": ContainsDict(
                        {
                            "architectures": Equals(["i386"]),
                            "build_request_id": Equals(None),
                            "build_request_timestamp": Equals(None),
                            "build_urls": Equals(
                                {"i386": canonical_url(job.build)}
                            ),
                        }
                    ),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_git_private_repo(self):
        # extraBuildArgs returns appropriate arguments if asked to build a
        # job for a Git branch.
        self.useFixture(InProcessAuthServerFixture())
        self.pushConfig(
            "launchpad", internal_macaroon_secret_key="some-secret"
        )
        [ref] = self.factory.makeGitRefs()
        ref.repository.transitionToInformationType(
            InformationType.PRIVATESECURITY, ref.repository.owner
        )
        owner = self.factory.makePerson()
        recipe = self.factory.makeOCIRecipe(
            owner=owner,
            registrant=owner,
            information_type=InformationType.USERDATA,
        )
        job = self.makeJob(git_ref=ref, recipe=recipe)
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
        # Asserts that nothing here is a zope proxy, to avoid errors when
        # serializing it for XML-RPC call.
        self.assertHasNoZopeSecurityProxy(args)
        split_browse_root = urlsplit(config.codehosting.git_browse_root)
        arch_tag = job.build.distro_arch_series.architecturetag
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_file": Equals(job.build.recipe.build_file),
                    "build_args": Equals(
                        {"BUILD_VAR": "123", "LAUNCHPAD_BUILD_ARCH": arch_tag}
                    ),
                    "build_path": Equals(job.build.recipe.build_path),
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
                                    identifier=Equals("oci-recipe-build"),
                                    caveats=MatchesListwise(
                                        [
                                            MatchesStructure.byEquality(
                                                caveat_id=(
                                                    "lp.oci-recipe-build %s"
                                                    % job.build.id
                                                )
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
                    "name": Equals(job.build.recipe.name),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals(
                        job.build.distro_arch_series.distroseries.name
                    ),
                    "trusted_keys": Equals(expected_trusted_keys),
                    # 'metadata' has detailed tests in
                    # TestAsyncOCIBuildBehaviour.
                    "metadata": ContainsDict(
                        {
                            "architectures": Equals(["i386"]),
                            "build_request_id": Equals(None),
                            "build_request_timestamp": Equals(None),
                            "build_urls": Equals(
                                {"i386": canonical_url(job.build)}
                            ),
                        }
                    ),
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
        self.assertHasNoZopeSecurityProxy(args)
        arch_tag = job.build.distro_arch_series.architecturetag
        self.assertThat(
            args,
            MatchesDict(
                {
                    "archive_private": Is(False),
                    "archives": Equals(expected_archives),
                    "arch_tag": Equals("i386"),
                    "isa_tag": Equals("i386"),
                    "abi_tag": Equals("i386"),
                    "build_file": Equals(job.build.recipe.build_file),
                    "build_args": Equals(
                        {"BUILD_VAR": "123", "LAUNCHPAD_BUILD_ARCH": arch_tag}
                    ),
                    "build_path": Equals(job.build.recipe.build_path),
                    "build_url": Equals(canonical_url(job.build)),
                    "builder_constraints": Equals([]),
                    "fast_cleanup": Is(True),
                    "git_repository": Equals(ref.repository.git_https_url),
                    "name": Equals(job.build.recipe.name),
                    "proxy_url": ProxyURLMatcher(job, self.now),
                    "revocation_endpoint": RevocationEndpointMatcher(
                        job, self.now
                    ),
                    "series": Equals(
                        job.build.distro_arch_series.distroseries.name
                    ),
                    "trusted_keys": Equals(expected_trusted_keys),
                    # 'metadata' has detailed tests in
                    # TestAsyncOCIBuildBehaviour.
                    "metadata": ContainsDict(
                        {
                            "architectures": Equals(["i386"]),
                            "build_request_id": Equals(None),
                            "build_request_timestamp": Equals(None),
                            "build_urls": Equals(
                                {"i386": canonical_url(job.build)}
                            ),
                        }
                    ),
                    "use_fetch_service": Is(False),
                    "launchpad_instance": Equals("devel"),
                    "launchpad_server_url": Equals("launchpad.test"),
                }
            ),
        )

    @defer.inlineCallbacks
    def test_extraBuildArgs_archives(self):
        # The build uses the release, security, and updates pockets in the
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
    def test_composeBuildRequest_proxy_url_set(self):
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref)
        build_request = yield job.composeBuildRequest(None)
        self.assertThat(
            build_request[4]["proxy_url"], ProxyURLMatcher(job, self.now)
        )

    @defer.inlineCallbacks
    def test_composeBuildRequest_git_ref_deleted(self):
        # If the source Git reference has been deleted, composeBuildRequest
        # raises CannotBuild.
        repository = self.factory.makeGitRepository()
        [ref] = self.factory.makeGitRefs(
            repository=repository, paths=["refs/heads/v1.0-20.04"]
        )
        owner = self.factory.makePerson(name="oci-owner")

        distribution = self.factory.makeDistribution()
        distroseries = self.factory.makeDistroSeries(
            distribution=distribution, status=SeriesStatus.CURRENT
        )
        processor = getUtility(IProcessorSet).getByName("386")
        self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )

        oci_project = self.factory.makeOCIProject(
            pillar=distribution, registrant=owner
        )
        recipe = self.factory.makeOCIRecipe(
            oci_project=oci_project, registrant=owner, owner=owner, git_ref=ref
        )
        job = self.makeJob(ref, recipe=recipe)
        repository.removeRefs([ref.path])

        # Clean the git_ref cache
        del get_property_cache(job.build.recipe)._git_ref

        self.assertIsNone(job.build.recipe.git_ref)
        expected_exception_msg = (
            "Source repository for "
            "~oci-owner/{} has been deleted.".format(recipe.name)
        )
        with ExpectedException(CannotBuild, expected_exception_msg):
            yield job.composeBuildRequest(None)

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_prefers_lxd(self):
        self.pushConfig("builddmaster", builder_proxy_host=None)
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref, allow_internet=False)
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
            f"job_type=OCIRECIPEBUILD,region={builder.region}",
        )

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_falls_back_to_chroot(self):
        self.pushConfig("builddmaster", builder_proxy_host=None)
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref, allow_internet=False)
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

    @defer.inlineCallbacks
    def test_dispatchBuildToWorker_oci_feature_flag_enabled(self):
        self.pushConfig("builddmaster", builder_proxy_host=None)
        [ref] = self.factory.makeGitRefs()

        distribution = self.factory.makeDistribution()
        distroseries = self.factory.makeDistroSeries(
            distribution=distribution, status=SeriesStatus.CURRENT
        )
        processor = getUtility(IProcessorSet).getByName("386")
        self.useFixture(
            FeatureFixture(
                {
                    "oci.build_series.%s"
                    % distribution.name: distroseries.name,
                    OCI_RECIPE_ALLOW_CREATE: "on",
                }
            )
        )
        distro_arch_series = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="i386",
            processor=processor,
        )

        build = self.factory.makeOCIRecipeBuild(
            distro_arch_series=distro_arch_series
        )
        job = self.makeJob(git_ref=ref, build=build)
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
            distroseries.name, job.build.distro_arch_series.distroseries.name
        )
        self.assertEqual(
            ("ensurepresent", lxd_lfa.http_url, "", ""), worker.call_log[0]
        )
        # grab the build method log from the OKWorker and check inside the
        # arguments dict that we build for distro series
        self.assertEqual(distroseries.name, worker.call_log[1][5]["series"])

    @defer.inlineCallbacks
    def test_extraBuildArgs_disallow_internet(self):
        # If external network access is not allowed for the OCI Recipe,
        # extraBuildArgs does not dispatch a proxy token.
        [ref] = self.factory.makeGitRefs()
        job = self.makeJob(git_ref=ref, allow_internet=False)
        with dbuser(config.builddmaster.dbuser):
            args = yield job.extraBuildArgs()
        self.assertNotIn("proxy_url", args)
        self.assertNotIn("revocation_endpoint", args)
        self.assertNotIn("use_fetch_service", args)


class TestHandleStatusForOCIRecipeBuild(
    MakeOCIBuildMixin, TestCaseWithFactory
):
    # This is mostly copied from TestHandleStatusMixin, however
    # we can't use all of those tests, due to the way OCIRecipeBuildBehaviour
    # parses the file contents, rather than just retrieving all that are
    # available. There's also some differences in the filemap handling, as
    # we need a much more complex filemap here.

    layer = LaunchpadZopelessLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def _createTestFile(self, name, content, hash):
        path = os.path.join(self.test_files_dir, name)
        with open(path, "wb") as fp:
            fp.write(six.ensure_binary(content))
        self.worker.valid_files[hash] = path

    def setUp(self):
        super().setUp()
        self.useFixture(fixtures.FakeLogger())
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        self.build = self.makeBuild()
        # For the moment, we require a builder for the build so that
        # handleStatus_OK can get a reference to the worker.
        self.builder = self.factory.makeBuilder()
        self.build.buildqueue_record.markAsBuilding(self.builder)
        self.worker = WaitingWorker("BuildStatus.OK")
        self.worker.valid_files["test_file_hash"] = ""
        self.interactor = BuilderInteractor()
        self.behaviour = self.interactor.getBuildBehaviour(
            self.build.buildqueue_record, self.builder, self.worker
        )
        self.addCleanup(shut_down_default_process_pool)

        # We overwrite the buildmaster root to use a temp directory.
        tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tempdir)
        self.upload_root = tempdir
        self.pushConfig("builddmaster", root=self.upload_root)

        # We stub out our build's getUploaderCommand() method so
        # we can check whether it was called as well as
        # verifySuccessfulUpload().
        removeSecurityProxy(self.build).verifySuccessfulUpload = FakeMethod(
            result=True
        )

        digests = [
            {
                "diff_id_1": {
                    "digest": "digest_1",
                    "source": "test/base_1",
                    "layer_id": "layer_1",
                },
                "diff_id_2": {
                    "digest": "digest_2",
                    "source": "",
                    "layer_id": "layer_2",
                },
            }
        ]

        self.test_files_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_files_dir)
        self._createTestFile("buildlog", "", "buildlog")
        self._createTestFile(
            "manifest.json",
            '[{"Config": "config_file_1.json", '
            '"Layers": ["layer_1/layer.tar", "layer_2/layer.tar"]}]',
            "manifest_hash",
        )
        self._createTestFile(
            "digests.json", json.dumps(digests), "digests_hash"
        )
        self._createTestFile(
            "config_file_1.json",
            '{"rootfs": {"diff_ids": ["diff_id_1", "diff_id_2"]}}',
            "config_1_hash",
        )
        self._createTestFile("layer_2.tar.gz", "", "layer_2_hash")

        self.filemap = {
            "manifest.json": "manifest_hash",
            "digests.json": "digests_hash",
            "config_file_1.json": "config_1_hash",
            "layer_1.tar.gz": "layer_1_hash",
            "layer_2.tar.gz": "layer_2_hash",
        }
        self.factory.makeOCIFile(
            build=self.build,
            layer_file_digest="digest_1",
            content=b"retrieved from librarian",
        )

    def assertResultCount(self, count, result):
        self.assertEqual(
            1, len(os.listdir(os.path.join(self.upload_root, result)))
        )

    @defer.inlineCallbacks
    def test_handleStatus_BUILDING(self):
        # If the builder is BUILDING (or any status other than WAITING),
        # then the behaviour calls updateStatus but doesn't do anything
        # else.
        initial_status = self.build.status
        bq_id = self.build.buildqueue_record.id
        worker_status = {"builder_status": "BuilderStatus.BUILDING"}
        removeSecurityProxy(self.build).updateStatus = FakeMethod()
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record, worker_status
            )
        self.assertEqual(None, self.build.log)
        self.assertEqual(0, len(os.listdir(self.upload_root)))
        self.assertEqual(
            [
                (
                    (initial_status,),
                    {"builder": self.builder, "worker_status": worker_status},
                )
            ],
            removeSecurityProxy(self.build).updateStatus.calls,
        )
        self.assertEqual(0, len(pop_notifications()), "Notifications received")
        self.assertEqual(
            self.build.buildqueue_record, getUtility(IBuildQueueSet).get(bq_id)
        )

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_OK_normal_image(self):
        now = datetime.now()
        mock_datetime = self.useFixture(
            MockPatch("lp.buildmaster.model.buildfarmjobbehaviour.datetime")
        ).mock
        mock_datetime.now = lambda: now
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.OK",
                    "filemap": self.filemap,
                },
            )
        self.assertEqual(
            [
                "buildlog",
                "manifest_hash",
                "digests_hash",
                "config_1_hash",
                "layer_2_hash",
            ],
            self.worker._got_file_record,
        )
        # This hash should not appear as it is already in the librarian
        self.assertNotIn("layer_1_hash", self.worker._got_file_record)
        self.assertEqual(BuildStatus.UPLOADING, self.build.status)
        self.assertResultCount(1, "incoming")

        # layer_1 should have been retrieved from the librarian
        layer_1_path = os.path.join(
            self.upload_root,
            "incoming",
            self.behaviour.getUploadDirLeaf(self.build.build_cookie),
            str(self.build.archive.id),
            self.build.distribution.name,
            "layer_1.tar.gz",
        )
        with open(layer_1_path, "rb") as layer_1_fp:
            contents = layer_1_fp.read()
            self.assertEqual(contents, b"retrieved from librarian")

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_OK_reuse_from_other_build(self):
        """We should be able to reuse a layer file from a separate build."""
        oci_file = self.factory.makeOCIFile(
            layer_file_digest="digest_2",
            content=b"layer 2 retrieved from librarian",
        )

        now = datetime.now(timezone.utc)
        mock_datetime = self.useFixture(
            MockPatch("lp.buildmaster.model.buildfarmjobbehaviour.datetime")
        ).mock
        mock_oci_datetime = self.useFixture(
            MockPatch("lp.oci.model.ocirecipebuildbehaviour.datetime")
        ).mock
        mock_datetime.now = lambda: now
        mock_oci_datetime.now = lambda tzinfo=None: now
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.OK",
                    "filemap": self.filemap,
                },
            )
        self.assertEqual(
            ["buildlog", "manifest_hash", "digests_hash", "config_1_hash"],
            self.worker._got_file_record,
        )
        # This hash should not appear as it is already in the librarian
        self.assertNotIn("layer_1_hash", self.worker._got_file_record)
        self.assertNotIn("layer_2_hash", self.worker._got_file_record)

        # layer_2 should have been retrieved from the librarian
        layer_2_path = os.path.join(
            self.upload_root,
            "incoming",
            self.behaviour.getUploadDirLeaf(self.build.build_cookie),
            str(self.build.archive.id),
            self.build.distribution.name,
            "layer_2.tar.gz",
        )
        with open(layer_2_path, "rb") as layer_2_fp:
            contents = layer_2_fp.read()
            self.assertEqual(contents, b"layer 2 retrieved from librarian")
        self.assertEqual(now, oci_file.date_last_used)

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_OK_absolute_filepath(self):
        self._createTestFile(
            "manifest.json",
            '[{"Config": "/notvalid/config_file_1.json", '
            '"Layers": ["layer_1/layer.tar", "layer_2/layer.tar"]}]',
            "manifest_hash",
        )

        self.filemap["/notvalid/config_file_1.json"] = "config_1_hash"

        # A filemap that tries to write to files outside of the upload
        # directory will not be collected.
        with ExpectedException(
            BuildDaemonError,
            "Build returned a file named " "'/notvalid/config_file_1.json'.",
        ):
            with dbuser(config.builddmaster.dbuser):
                yield self.behaviour.handleStatus(
                    self.build.buildqueue_record,
                    {
                        "builder_status": "BuilderStatus.WAITING",
                        "build_status": "BuildStatus.OK",
                        "filemap": self.filemap,
                    },
                )

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_OK_relative_filepath(self):
        self._createTestFile(
            "manifest.json",
            '[{"Config": "../config_file_1.json", '
            '"Layers": ["layer_1/layer.tar", "layer_2/layer.tar"]}]',
            "manifest_hash",
        )

        self.filemap["../config_file_1.json"] = "config_1_hash"
        # A filemap that tries to write to files outside of
        # the upload directory will not be collected.
        with ExpectedException(
            BuildDaemonError,
            "Build returned a file named '../config_file_1.json'.",
        ):
            with dbuser(config.builddmaster.dbuser):
                yield self.behaviour.handleStatus(
                    self.build.buildqueue_record,
                    {
                        "builder_status": "BuilderStatus.WAITING",
                        "build_status": "BuildStatus.OK",
                        "filemap": self.filemap,
                    },
                )

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_OK_sets_build_log(self):
        # The build log is set during handleStatus.
        self.assertEqual(None, self.build.log)
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.OK",
                    "filemap": self.filemap,
                },
            )
        self.assertNotEqual(None, self.build.log)

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_ABORTED_cancels_cancelling(self):
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.CANCELLING)
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.ABORTED",
                },
            )
        self.assertEqual(0, len(pop_notifications()), "Notifications received")
        self.assertEqual(BuildStatus.CANCELLED, self.build.status)

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_ABORTED_illegal_when_building(self):
        self.builder.vm_host = "fake_vm_host"
        self.behaviour = self.interactor.getBuildBehaviour(
            self.build.buildqueue_record, self.builder, self.worker
        )
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.BUILDING)
            with ExpectedException(
                BuildDaemonError,
                "Build returned unexpected status: %r" % "ABORTED",
            ):
                yield self.behaviour.handleStatus(
                    self.build.buildqueue_record,
                    {
                        "builder_status": "BuilderStatus.WAITING",
                        "build_status": "BuildStatus.ABORTED",
                    },
                )

    @defer.inlineCallbacks
    def test_handleStatus_WAITING_ABORTED_cancelling_sets_build_log(self):
        # If a build is intentionally cancelled, the build log is set.
        self.assertEqual(None, self.build.log)
        with dbuser(config.builddmaster.dbuser):
            self.build.updateStatus(BuildStatus.CANCELLING)
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.ABORTED",
                },
            )
        self.assertNotEqual(None, self.build.log)

    @defer.inlineCallbacks
    def test_date_finished_set(self):
        # The date finished is updated during handleStatus_OK.
        self.assertEqual(None, self.build.date_finished)
        with dbuser(config.builddmaster.dbuser):
            yield self.behaviour.handleStatus(
                self.build.buildqueue_record,
                {
                    "builder_status": "BuilderStatus.WAITING",
                    "build_status": "BuildStatus.OK",
                    "filemap": self.filemap,
                },
            )
        self.assertNotEqual(None, self.build.date_finished)

    @defer.inlineCallbacks
    def test_givenback_collection(self):
        with ExpectedException(
            BuildDaemonError,
            "Build returned unexpected status: %r" % "GIVENBACK",
        ):
            with dbuser(config.builddmaster.dbuser):
                yield self.behaviour.handleStatus(
                    self.build.buildqueue_record,
                    {
                        "builder_status": "BuilderStatus.WAITING",
                        "build_status": "BuildStatus.GIVENBACK",
                    },
                )

    @defer.inlineCallbacks
    def test_builderfail_collection(self):
        with ExpectedException(
            BuildDaemonError,
            "Build returned unexpected status: %r" % "BUILDERFAIL",
        ):
            with dbuser(config.builddmaster.dbuser):
                yield self.behaviour.handleStatus(
                    self.build.buildqueue_record,
                    {
                        "builder_status": "BuilderStatus.WAITING",
                        "build_status": "BuildStatus.BUILDERFAIL",
                    },
                )

    @defer.inlineCallbacks
    def test_invalid_status_collection(self):
        with ExpectedException(
            BuildDaemonError, "Build returned unexpected status: %r" % "BORKED"
        ):
            with dbuser(config.builddmaster.dbuser):
                yield self.behaviour.handleStatus(
                    self.build.buildqueue_record,
                    {
                        "builder_status": "BuilderStatus.WAITING",
                        "build_status": "BuildStatus.BORKED",
                    },
                )


class TestGetUploadMethodsForOCIRecipeBuild(
    MakeOCIBuildMixin, TestGetUploadMethodsMixin, TestCaseWithFactory
):
    """IPackageBuild.getUpload-related methods work with OCI recipe builds."""

    def setUp(self):
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        super().setUp()
