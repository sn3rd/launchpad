# Copyright 2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""OCIRecipeBuildJob tests"""

import os
import signal
from unittest import mock

import transaction
from fixtures import FakeLogger
from storm.locals import Store
from testtools.matchers import (
    Equals,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.oci.interfaces.ocirecipe import OCI_RECIPE_ALLOW_CREATE
from lp.oci.interfaces.ocirecipebuildjob import (
    IOCIRecipeBuildJob,
    IOCIRegistryUploadJob,
)
from lp.oci.interfaces.ocirecipejob import IOCIRecipeRequestBuildsJobSource
from lp.oci.interfaces.ociregistryclient import (
    BlobUploadFailed,
    IOCIRegistryClient,
)
from lp.oci.model.ocirecipebuildjob import (
    OCIRecipeBuildJob,
    OCIRecipeBuildJobDerived,
    OCIRecipeBuildJobType,
    OCIRegistryUploadJob,
)
from lp.services.config import config
from lp.services.database.locking import LockType, try_advisory_lock
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.job.tests import block_on_job
from lp.services.statsd.tests import StatsMixin
from lp.services.webapp import canonical_url
from lp.services.webhooks.testing import LogsScheduledWebhooks
from lp.testing import TestCaseWithFactory, admin_logged_in
from lp.testing.dbuser import dbuser, switch_dbuser
from lp.testing.fakemethod import FakeMethod
from lp.testing.fixture import ZopeUtilityFixture
from lp.testing.layers import (
    CelerySlowJobLayer,
    DatabaseFunctionalLayer,
    LaunchpadZopelessLayer,
)
from lp.testing.mail_helpers import pop_notifications


def run_isolated_jobs(jobs):
    """Run a sequence of jobs, ensuring transaction isolation.

    We abort the transaction after each job to make sure that there is no
    relevant uncommitted work.
    """
    for job in jobs:
        JobRunner([job]).runAll()
        transaction.abort()


@implementer(IOCIRegistryClient)
class FakeRegistryClient:
    def __init__(self):
        self.upload = FakeMethod()
        self.uploadManifestList = FakeMethod()


class FakeOCIBuildJob(OCIRecipeBuildJobDerived):
    """For testing OCIRecipeBuildJobDerived without a child class."""


class TestOCIRecipeBuildJob(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_provides_interface(self):
        oci_build = self.factory.makeOCIRecipeBuild()
        self.assertProvides(
            OCIRecipeBuildJob(
                oci_build, OCIRecipeBuildJobType.REGISTRY_UPLOAD, {}
            ),
            IOCIRecipeBuildJob,
        )

    def test_getOopsVars(self):
        oci_build = self.factory.makeOCIRecipeBuild()
        build_job = OCIRecipeBuildJob(
            oci_build, OCIRecipeBuildJobType.REGISTRY_UPLOAD, {}
        )
        derived = FakeOCIBuildJob(build_job)
        oops = derived.getOopsVars()
        expected = [
            ("job_id", build_job.job.id),
            ("job_type", build_job.job_type.title),
            ("build_id", oci_build.id),
            ("recipe_owner_id", oci_build.recipe.owner.id),
            ("oci_project_name", oci_build.recipe.oci_project.name),
        ]
        self.assertEqual(expected, oops)


class TestOCIRecipeBuildJobDerived(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))

    def test_repr(self):
        build = self.factory.makeOCIRecipeBuild()
        job = OCIRecipeBuildJob(
            build, OCIRecipeBuildJobType.REGISTRY_UPLOAD, {}
        )
        derived_job = OCIRecipeBuildJobDerived(job)
        expected_repr = (
            "<OCIRecipeBuildJobDerived for "
            "~%s/%s/+oci/%s/+recipe/%s/+build/%d>"
            % (
                build.recipe.owner.name,
                build.recipe.oci_project.pillar.name,
                build.recipe.oci_project.name,
                build.recipe.name,
                build.id,
            )
        )
        self.assertEqual(expected_repr, repr(derived_job))

    def test_repr_fails_to_get_an_attribute(self):
        class ErrorOCIRecipeBuildJobDerived(OCIRecipeBuildJobDerived):
            def __getattribute__(self, item):
                if item == "build":
                    raise AttributeError("Somethng is wrong with build")
                return super().__getattribute__(item)

        oci_build = self.factory.makeOCIRecipeBuild()
        job = OCIRecipeBuildJob(
            oci_build, OCIRecipeBuildJobType.REGISTRY_UPLOAD, {}
        )
        derived_job = ErrorOCIRecipeBuildJobDerived(job)
        self.assertEqual(
            "<ErrorOCIRecipeBuildJobDerived ID#%s>" % derived_job.job_id,
            repr(derived_job),
        )


class MultiArchRecipeMixin:
    def makeRecipe(
        self, include_i386=True, include_amd64=True, include_hppa=False
    ):
        processors = []
        if include_i386:
            processors.append("386")
        if include_amd64:
            processors.append("amd64")
        if include_hppa:
            processors.append("hppa")
        archs = []
        recipe = self.factory.makeOCIRecipe(require_virtualized=False)
        distroseries = self.factory.makeDistroSeries(
            distribution=recipe.oci_project.distribution
        )
        for processor_name in processors:
            proc = getUtility(IProcessorSet).getByName(processor_name)
            distro_arch = self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor_name,
                processor=proc,
            )
            distro_arch.addOrUpdateChroot(self.factory.makeLibraryFileAlias())
            archs.append(proc)
        recipe.setProcessors(archs)
        return recipe

    def makeBuildRequest(
        self, include_i386=True, include_amd64=True, include_hppa=False
    ):
        recipe = self.makeRecipe(include_i386, include_amd64, include_hppa)
        # Creates a build request with a build in it.
        build_request = recipe.requestBuilds(recipe.owner)
        with admin_logged_in():
            jobs = getUtility(IOCIRecipeRequestBuildsJobSource).iterReady()
            with dbuser(config.IOCIRecipeRequestBuildsJobSource.dbuser):
                JobRunner(jobs).runAll()
        return build_request


class TestOCIRegistryUploadJob(
    TestCaseWithFactory, MultiArchRecipeMixin, StatsMixin
):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({OCI_RECIPE_ALLOW_CREATE: "on"}))
        self.setUpStats()

    def makeOCIRecipeBuild(self, **kwargs):
        ocibuild = self.factory.makeOCIRecipeBuild(
            builder=self.factory.makeBuilder(), **kwargs
        )
        ocibuild.updateStatus(BuildStatus.FULLYBUILT)
        self.makeWebhook(ocibuild.recipe)
        return ocibuild

    def makeWebhook(self, recipe):
        self.factory.makeWebhook(
            target=recipe, event_types=["oci-recipe:build:0.1"]
        )

    def assertWebhookDeliveries(
        self, ocibuild, expected_registry_upload_statuses, logger
    ):
        hook = ocibuild.recipe.webhooks.one()
        deliveries = list(hook.deliveries)
        deliveries.reverse()
        build_req_url = (
            None
            if ocibuild.build_request is None
            else canonical_url(ocibuild.build_request, force_local_path=True)
        )
        expected_payloads = [
            {
                "recipe_build": Equals(
                    canonical_url(ocibuild, force_local_path=True)
                ),
                "action": Equals("status-changed"),
                "recipe": Equals(
                    canonical_url(ocibuild.recipe, force_local_path=True)
                ),
                "build_request": Equals(build_req_url),
                "status": Equals("Successfully built"),
                "registry_upload_status": Equals(expected),
            }
            for expected in expected_registry_upload_statuses
        ]
        matchers = [
            MatchesStructure(
                event_type=Equals("oci-recipe:build:0.1"),
                payload=MatchesDict(expected_payload),
            )
            for expected_payload in expected_payloads
        ]
        self.assertThat(deliveries, MatchesListwise(matchers))
        with dbuser(config.IWebhookDeliveryJobSource.dbuser):
            for delivery in deliveries:
                self.assertEqual(
                    "<WebhookDeliveryJob for webhook %d on %r>"
                    % (hook.id, hook.target),
                    repr(delivery),
                )
            self.assertThat(
                logger.output,
                LogsScheduledWebhooks(
                    [
                        (
                            hook,
                            "oci-recipe:build:0.1",
                            MatchesDict(expected_payload),
                        )
                        for expected_payload in expected_payloads
                    ]
                ),
            )

    def test_provides_interface(self):
        # `OCIRegistryUploadJob` objects provide `IOCIRegistryUploadJob`.
        ocibuild = self.factory.makeOCIRecipeBuild()
        job = OCIRegistryUploadJob.create(ocibuild)
        self.assertProvides(job, IOCIRegistryUploadJob)

    def test_run(self):
        logger = self.useFixture(FakeLogger())
        build_request = self.makeBuildRequest(include_i386=False)
        recipe = build_request.recipe

        self.assertEqual(1, build_request.builds.count())
        ocibuild = build_request.builds[0]
        ocibuild.updateStatus(BuildStatus.FULLYBUILT)
        self.makeWebhook(recipe)

        self.assertContentEqual([], ocibuild.registry_upload_jobs)
        job = OCIRegistryUploadJob.create(ocibuild)
        client = FakeRegistryClient()
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))
        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            run_isolated_jobs([job])

        self.assertEqual([((ocibuild,), {})], client.upload.calls)
        self.assertEqual(
            [((build_request, {ocibuild}), {})],
            client.uploadManifestList.calls,
        )
        self.assertContentEqual([job], ocibuild.registry_upload_jobs)
        self.assertIsNone(job.error_summary)
        self.assertIsNone(job.errors)
        self.assertEqual([], pop_notifications())
        self.assertWebhookDeliveries(ocibuild, ["Pending", "Uploaded"], logger)
        calls = self.filterStatsdCallsByName("job")
        self.assertThat(
            calls,
            MatchesListwise(
                [
                    Equals(
                        "job.start_count,env=test,"
                        "type=OCIRecipeRequestBuildsJob"
                    ),
                    Equals(
                        "job.complete_count,env=test,"
                        "type=OCIRecipeRequestBuildsJob"
                    ),
                    Equals(
                        "job.start_count,env=test,type=OCIRegistryUploadJob"
                    ),
                    Equals(
                        "job.complete_count,env=test,type=OCIRegistryUploadJob"
                    ),
                ]
            ),
        )

    def test_run_multiple_architectures(self):
        build_request = self.makeBuildRequest()
        builds = list(build_request.builds)
        self.assertEqual(2, len(builds))
        self.assertEqual(builds[0].build_request, builds[1].build_request)

        upload_jobs = []
        for build in builds:
            self.assertContentEqual([], build.registry_upload_jobs)
            upload_jobs.append(OCIRegistryUploadJob.create(build))

        client = FakeRegistryClient()
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))

        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            JobRunner([upload_jobs[0]]).runAll()
        self.assertEqual([((builds[0],), {})], client.upload.calls)
        # Should have tried to upload the manifest list with only the first
        # build.
        self.assertEqual(
            [((build_request, set(builds[:1])), {})],
            client.uploadManifestList.calls,
        )

        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            JobRunner([upload_jobs[1]]).runAll()
        self.assertEqual(
            [((builds[0],), {}), ((builds[1],), {})], client.upload.calls
        )
        # Should have tried to upload the manifest list with both builds.
        self.assertEqual(
            [
                ((build_request, set(builds[:1])), {}),
                ((build_request, set(builds)), {}),
            ],
            client.uploadManifestList.calls,
        )
        calls = self.filterStatsdCallsByName("job")
        self.assertThat(
            calls,
            MatchesListwise(
                [
                    Equals(
                        "job.start_count,env=test,"
                        "type=OCIRecipeRequestBuildsJob"
                    ),
                    Equals(
                        "job.complete_count,env=test,"
                        "type=OCIRecipeRequestBuildsJob"
                    ),
                    Equals(
                        "job.start_count,env=test,type=OCIRegistryUploadJob"
                    ),
                    Equals(
                        "job.complete_count,env=test,type=OCIRegistryUploadJob"
                    ),
                    Equals(
                        "job.start_count,env=test,type=OCIRegistryUploadJob"
                    ),
                    Equals(
                        "job.complete_count,env=test,type=OCIRegistryUploadJob"
                    ),
                ]
            ),
        )

    def test_failing_upload_does_not_retries_automatically(self):
        build_request = self.makeBuildRequest(include_i386=False)
        builds = build_request.builds
        self.assertEqual(1, builds.count())

        build = builds.one()
        self.assertContentEqual([], build.registry_upload_jobs)
        upload_job = OCIRegistryUploadJob.create(build)

        client = mock.Mock()
        client.upload.side_effect = Exception("Nope! Error.")
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))

        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            JobRunner([upload_job]).runAll()
        self.assertEqual(1, client.upload.call_count)
        self.assertEqual(0, client.uploadManifestList.call_count)
        self.assertEqual(JobStatus.FAILED, upload_job.status)
        self.assertFalse(upload_job.build_uploaded)

    def test_failing_upload_manifest_list_retries(self):
        build_request = self.makeBuildRequest(include_i386=False)
        builds = build_request.builds
        self.assertEqual(1, builds.count())

        build = builds.one()
        self.assertContentEqual([], build.registry_upload_jobs)
        upload_job = OCIRegistryUploadJob.create(build)

        client = mock.Mock()
        client.uploadManifestList.side_effect = (
            OCIRegistryUploadJob.ManifestListUploadError("Nope! Error.")
        )
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))

        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            JobRunner([upload_job]).runAll()
        self.assertEqual(1, client.upload.call_count)
        self.assertEqual(1, client.uploadManifestList.call_count)
        self.assertEqual(JobStatus.WAITING, upload_job.status)
        self.assertTrue(upload_job.is_pending)
        self.assertTrue(upload_job.build_uploaded)

        # Retry should skip client.upload and only run
        # client.uploadManifestList:
        client.uploadManifestList.side_effect = None
        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            JobRunner([upload_job]).runAll()
        self.assertEqual(1, client.upload.call_count)
        self.assertEqual(2, client.uploadManifestList.call_count)
        self.assertEqual(JobStatus.COMPLETED, upload_job.status)
        self.assertTrue(upload_job.build_uploaded)

    def test_getUploadedBuilds(self):
        # Create a build request with 3 builds.
        build_request = self.makeBuildRequest(
            include_i386=True, include_amd64=True, include_hppa=True
        )
        builds = build_request.builds
        self.assertEqual(3, builds.count())

        # Create the upload job for the first build.
        upload_job1 = OCIRegistryUploadJob.create(builds[0])
        upload_job1 = removeSecurityProxy(upload_job1)

        upload_job2 = OCIRegistryUploadJob.create(builds[1])
        upload_job2 = removeSecurityProxy(upload_job2)

        client = FakeRegistryClient()
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))
        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            run_isolated_jobs([upload_job1])

        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            run_isolated_jobs([upload_job2])

        result = upload_job1.getUploadedBuilds(build_request)
        self.assertEqual({builds[0], builds[1]}, result)

    def test_run_failed_registry_error(self):
        # A run that fails with a registry error sets the registry upload
        # status to FAILED, and stores the detailed errors.
        logger = self.useFixture(FakeLogger())
        ocibuild = self.makeOCIRecipeBuild()
        self.assertContentEqual([], ocibuild.registry_upload_jobs)
        job = OCIRegistryUploadJob.create(ocibuild)
        client = FakeRegistryClient()
        error_summary = "Upload of some-digest for some-image failed"
        errors = [
            {
                "code": "UNAUTHORIZED",
                "message": "authentication required",
                "detail": [
                    {
                        "Type": "repository",
                        "Class": "",
                        "Name": "some-image",
                        "Action": "pull",
                    },
                    {
                        "Type": "repository",
                        "Class": "",
                        "Name": "some-image",
                        "Action": "push",
                    },
                ],
            },
        ]
        client.upload.failure = BlobUploadFailed(error_summary, errors)
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))
        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            run_isolated_jobs([job])
        self.assertEqual([((ocibuild,), {})], client.upload.calls)
        self.assertContentEqual([job], ocibuild.registry_upload_jobs)
        self.assertEqual(error_summary, job.error_summary)
        self.assertEqual(errors, job.errors)
        self.assertEqual([], pop_notifications())
        self.assertWebhookDeliveries(
            ocibuild, ["Pending", "Failed to upload"], logger
        )

    def test_run_failed_other_error(self):
        # A run that fails for some reason other than a registry error sets
        # the registry upload status to FAILED.
        logger = self.useFixture(FakeLogger())
        ocibuild = self.makeOCIRecipeBuild()
        self.assertContentEqual([], ocibuild.registry_upload_jobs)
        job = OCIRegistryUploadJob.create(ocibuild)
        client = FakeRegistryClient()
        client.upload.failure = ValueError("An upload failure")
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))
        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            run_isolated_jobs([job])
        self.assertEqual([((ocibuild,), {})], client.upload.calls)
        self.assertContentEqual([job], ocibuild.registry_upload_jobs)
        self.assertEqual("An upload failure", job.error_summary)
        self.assertIsNone(job.errors)
        self.assertEqual([], pop_notifications())
        self.assertWebhookDeliveries(
            ocibuild, ["Pending", "Failed to upload"], logger
        )

    def test_run_does_not_oops(self):
        # The job can OOPS, but it is hidden by our exception handling
        # Check that it's actually empty
        build_request = self.makeBuildRequest(include_i386=False)
        recipe = build_request.recipe

        self.assertEqual(1, build_request.builds.count())
        ocibuild = build_request.builds[0]
        ocibuild.updateStatus(BuildStatus.FULLYBUILT)
        self.makeWebhook(recipe)

        self.assertContentEqual([], ocibuild.registry_upload_jobs)
        job = OCIRegistryUploadJob.create(ocibuild)
        client = FakeRegistryClient()
        self.useFixture(ZopeUtilityFixture(client, IOCIRegistryClient))
        with dbuser(config.IOCIRegistryUploadJobSource.dbuser):
            run_isolated_jobs([job])

        self.assertEqual(0, len(self.oopses))

    def test_advisorylock_on_run(self):
        # The job should take an advisory lock and any attempted
        # simultaneous jobs should retry
        logger = self.useFixture(FakeLogger())
        build_request = self.makeBuildRequest(include_i386=False)
        recipe = build_request.recipe

        self.assertEqual(1, build_request.builds.count())
        ocibuild = build_request.builds[0]
        ocibuild.updateStatus(BuildStatus.FULLYBUILT)
        self.makeWebhook(recipe)

        self.assertContentEqual([], ocibuild.registry_upload_jobs)
        job = OCIRegistryUploadJob.create(ocibuild)
        switch_dbuser(config.IOCIRegistryUploadJobSource.dbuser)
        # Fork so that we can take an advisory lock from a different
        # PostgreSQL session.
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(read)
            with try_advisory_lock(
                LockType.REGISTRY_UPLOAD,
                ocibuild.recipe.id,
                Store.of(ocibuild.recipe),
            ):
                os.write(write, b"1")
                try:
                    signal.pause()
                except KeyboardInterrupt:
                    pass
            os._exit(0)
        else:  # parent
            try:
                os.close(write)
                os.read(read, 1)
                runner = JobRunner([job])
                runner.runAll()
                self.assertEqual(JobStatus.WAITING, job.status)
                self.assertEqual([], runner.oops_ids)
                self.assertIn(
                    "Scheduling retry due to AdvisoryLockHeld", logger.output
                )
            finally:
                os.kill(pid, signal.SIGINT)


class TestOCIRegistryUploadJobViaCelery(
    TestCaseWithFactory, MultiArchRecipeMixin
):
    """Runs OCIRegistryUploadJob via Celery, to make sure the machinery
    around it works.

    It's important to have this test specially because this job does some
    dodgy things with its own status and the database transaction,
    so we should make sure we are not breaking anything in the interaction
    with the job lifecycle via celery.
    """

    layer = CelerySlowJobLayer

    def setUp(self):
        super().setUp()
        self.useFixture(
            FeatureFixture(
                {
                    OCI_RECIPE_ALLOW_CREATE: "on",
                    "jobs.celery.enabled_classes": "OCIRegistryUploadJob",
                }
            )
        )

    def test_run_upload(self):
        build_request = self.makeBuildRequest()
        builds = build_request.builds
        self.assertEqual(2, builds.count())

        with block_on_job():
            for build in builds:
                OCIRegistryUploadJob.create(build)
            transaction.commit()
        messages = [message.as_string() for message in pop_notifications()]
        self.assertEqual(0, len(messages))
