# Copyright 2015-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test snap packages."""

import base64
import json
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from textwrap import dedent
from urllib.parse import urlsplit

import iso8601
import responses
import transaction
from fixtures import FakeLogger, MockPatch
from nacl.public import PrivateKey
from pymacaroons import Macaroon
from storm.exceptions import LostObjectError
from storm.locals import Store
from testtools.matchers import (
    AfterPreprocessing,
    ContainsDict,
    Equals,
    GreaterThan,
    Is,
    IsInstance,
    LessThan,
    MatchesAll,
    MatchesDict,
    MatchesSetwise,
    MatchesStructure,
)
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.app.errors import SubscriptionPrivacyViolation
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import BuildQueueStatus, BuildStatus
from lp.buildmaster.interfaces.buildqueue import IBuildQueue
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.buildmaster.model.buildfarmjob import BuildFarmJob
from lp.buildmaster.model.buildqueue import BuildQueue
from lp.code.errors import (
    BranchFileNotFound,
    BranchHostingFault,
    GitRepositoryBlobNotFound,
    GitRepositoryScanFault,
)
from lp.code.tests.helpers import BranchHostingFixture, GitHostingFixture
from lp.registry.enums import (
    BranchSharingPolicy,
    PersonVisibility,
    TeamMembershipPolicy,
)
from lp.registry.interfaces.accesspolicy import IAccessPolicySource
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.model.accesspolicy import AccessArtifact, AccessArtifactGrant
from lp.services.config import config
from lp.services.crypto.interfaces import IEncryptedContainer
from lp.services.database.constants import ONE_DAY_AGO, UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import (
    flush_database_caches,
    get_transaction_timestamp,
)
from lp.services.features.testing import MemoryFeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.log.logger import BufferLogger
from lp.services.propertycache import clear_property_cache
from lp.services.timeout import default_timeout
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.snapshot import notify_modified
from lp.services.webhooks.testing import LogsScheduledWebhooks
from lp.snappy.interfaces.snap import (
    BadSnapSearchContext,
    CannotFetchSnapcraftYaml,
    CannotModifySnapProcessor,
    CannotParseSnapcraftYaml,
    ISnap,
    ISnapSet,
    ISnapView,
    MissingSnapcraftYaml,
    NoSourceForSnap,
    SnapBuildAlreadyPending,
    SnapBuildDisallowedArchitecture,
    SnapBuildRequestStatus,
    SnapPrivacyMismatch,
)
from lp.snappy.interfaces.snapbase import ISnapBaseSet, NoSuchSnapBase
from lp.snappy.interfaces.snapbuild import (
    ISnapBuild,
    ISnapBuildSet,
    SnapBuildStoreUploadStatus,
)
from lp.snappy.interfaces.snapbuildjob import ISnapStoreUploadJobSource
from lp.snappy.interfaces.snapjob import ISnapRequestBuildsJobSource
from lp.snappy.interfaces.snapstoreclient import ISnapStoreClient
from lp.snappy.model.snap import Snap, SnapSet, get_snap_privacy_filter
from lp.snappy.model.snapbuild import SnapFile
from lp.snappy.model.snapbuildjob import SnapBuildJob
from lp.snappy.model.snapjob import SnapJob
from lp.snappy.tests.test_snapbuildjob import (
    FakeSnapStoreClient,
    run_isolated_jobs,
)
from lp.testing import (
    ANONYMOUS,
    StormStatementRecorder,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login,
    login_admin,
    login_celebrity,
    logout,
    person_logged_in,
    record_two_runs,
)
from lp.testing.dbuser import dbuser
from lp.testing.fakemethod import FakeMethod
from lp.testing.fixture import ZopeUtilityFixture
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import DoesNotSnapshot, HasQueryCount
from lp.testing.pages import webservice_for_person


class TestSnapPermissions(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_delete_permissions_registry_experts(self):
        # A snap package can be deleted from registry_experts,
        # commercial_admin, admin and owner.

        not_owner = login_celebrity("registry_experts")
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )

        with person_logged_in(not_owner):
            snap.destroySelf()
        flush_database_caches()
        # The deleted snap is gone.
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))

    def test_delete_permissions_commercial_admin(self):
        # A snap package can be deleted from registry_experts,
        # commercial_admin, admin and owner.

        not_owner = login_celebrity("commercial_admin")
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )

        with person_logged_in(not_owner):
            snap.destroySelf()
        flush_database_caches()
        # The deleted snap is gone.
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))

    def test_delete_permissions_admin(self):
        # A snap package can be deleted from registry_experts,
        # commercial_admin, admin and owner.

        not_owner = login_celebrity("admin")
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )

        with person_logged_in(not_owner):
            snap.destroySelf()
        flush_database_caches()
        # The deleted snap is gone.
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))

    def test_delete_permissions_owner(self):
        # A snap package can be deleted from registry_experts,
        # commercial_admin, admin and owner.

        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )

        with person_logged_in(owner):
            snap.destroySelf()
        flush_database_caches()
        # The deleted snap is gone.
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))

    def test_delete_permissions_unauthorized(self):
        # A snap package cannot be deleted if unauthorized

        not_owner = self.factory.makePerson()
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )

        with person_logged_in(not_owner):
            self.assertRaises(Unauthorized, getattr, snap, "destroySelf")
        flush_database_caches()
        # The snap is not deleted.
        self.assertTrue(getUtility(ISnapSet).exists(owner, "condemned"))


class TestSnap(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_implements_interfaces(self):
        # Snap implements ISnap.
        snap = self.factory.makeSnap()
        with admin_logged_in():
            self.assertProvides(snap, ISnap)

    def test___repr__(self):
        # `Snap` objects have an informative __repr__.
        snap = self.factory.makeSnap()
        self.assertEqual(
            "<Snap ~%s/+snap/%s>" % (snap.owner.name, snap.name), repr(snap)
        )

    def test_avoids_problematic_snapshots(self):
        self.assertThat(
            self.factory.makeSnap(),
            DoesNotSnapshot(
                [
                    "pending_build_requests",
                    "failed_build_requests",
                    "builds",
                    "completed_builds",
                    "pending_builds",
                ],
                ISnapView,
            ),
        )

    def test_initial_date_last_modified(self):
        # The initial value of date_last_modified is date_created.
        snap = self.factory.makeSnap(date_created=ONE_DAY_AGO)
        self.assertEqual(snap.date_created, snap.date_last_modified)

    def test_modifiedevent_sets_date_last_modified(self):
        # When a Snap receives an object modified event, the last modified
        # date is set to UTC_NOW.
        snap = self.factory.makeSnap(date_created=ONE_DAY_AGO)
        with notify_modified(removeSecurityProxy(snap), ["name"]):
            pass
        self.assertSqlAttributeEqualsDate(snap, "date_last_modified", UTC_NOW)

    def makeBuildableDistroArchSeries(self, **kwargs):
        das = self.factory.makeDistroArchSeries(**kwargs)
        fake_chroot = self.factory.makeLibraryFileAlias(
            filename="fake_chroot.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_chroot)
        return das

    def test_requestBuild(self):
        # requestBuild creates a new SnapBuild.
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries,
            processors=[distroarchseries.processor],
        )
        build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
            target_architectures=["amd64", "i386"],
        )
        self.assertTrue(ISnapBuild.providedBy(build))
        self.assertThat(
            build,
            MatchesStructure(
                requester=Equals(snap.owner),
                archive=Equals(snap.distro_series.main_archive),
                distro_arch_series=Equals(distroarchseries),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                snap_base=Is(None),
                channels=Is(None),
                status=Equals(BuildStatus.NEEDSBUILD),
                target_architectures=Equals(["amd64", "i386"]),
            ),
        )
        store = Store.of(build)
        store.flush()
        build_queue = store.find(
            BuildQueue,
            BuildQueue._build_farm_job_id
            == removeSecurityProxy(build).build_farm_job_id,
        ).one()
        self.assertProvides(build_queue, IBuildQueue)
        self.assertEqual(
            snap.distro_series.main_archive.require_virtualized,
            build_queue.virtualized,
        )
        self.assertEqual(BuildQueueStatus.WAITING, build_queue.status)

    def test_requestBuild_score(self):
        # Build requests have a relatively low queue score (2510).
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries,
            processors=[distroarchseries.processor],
        )
        build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
        )
        queue_record = build.buildqueue_record
        queue_record.score()
        self.assertEqual(2510, queue_record.lastscore)

    def test_requestBuild_relative_build_score(self):
        # Offsets for archives are respected.
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries, processors=[processor]
        )
        archive = self.factory.makeArchive(owner=snap.owner)
        removeSecurityProxy(archive).relative_build_score = 100
        build = snap.requestBuild(
            snap.owner,
            archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
        )
        queue_record = build.buildqueue_record
        queue_record.score()
        self.assertEqual(2610, queue_record.lastscore)

    def test_requestBuild_snap_base(self):
        # requestBuild can select a snap base.
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries, processors=[processor]
        )
        with admin_logged_in():
            snap_base = self.factory.makeSnapBase(
                distro_series=distroarchseries.distroseries
            )
        build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
            snap_base=snap_base,
        )
        self.assertEqual(snap_base, build.snap_base)

    def test_requestBuild_channels(self):
        # requestBuild can select non-default channels.
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries, processors=[processor]
        )
        build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
            channels={"snapcraft": "edge", "snapd": "edge"},
        )
        self.assertEqual(
            {"snapcraft": "edge", "snapd": "edge"}, build.channels
        )

    def test_requestBuild_rejects_repeats(self):
        # requestBuild refuses if there is already a pending build.
        distroseries = self.factory.makeDistroSeries()
        procs = []
        arches = []
        for i in range(2):
            procs.append(self.factory.makeProcessor(supports_virtualized=True))
            arches.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=procs[i]
                )
            )
        snap = self.factory.makeSnap(
            distroseries=distroseries, processors=[procs[0], procs[1]]
        )
        old_build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
        )
        self.assertRaises(
            SnapBuildAlreadyPending,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
        )
        # We can build for a different archive.
        snap.requestBuild(
            snap.owner,
            self.factory.makeArchive(owner=snap.owner),
            arches[0],
            PackagePublishingPocket.UPDATES,
        )
        # We can build for a different distroarchseries.
        snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            arches[1],
            PackagePublishingPocket.UPDATES,
        )
        # channels=None and channels={} are treated as equivalent, but
        # anything else allows a new build.
        self.assertRaises(
            SnapBuildAlreadyPending,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
            channels={},
        )
        snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
            channels={"core": "edge"},
        )
        self.assertRaises(
            SnapBuildAlreadyPending,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
            channels={"core": "edge"},
        )
        # target_architectures are taken into account when looking for pending
        # builds. the order of the list should not matter.
        snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
            target_architectures=["i386", "amd64"],
        )
        self.assertRaises(
            SnapBuildAlreadyPending,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
            target_architectures=["amd64", "i386"],
        )
        # Changing the status of the old build allows a new build.
        old_build.updateStatus(BuildStatus.BUILDING)
        old_build.updateStatus(BuildStatus.FULLYBUILT)
        snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
        )

    def test_requestBuild_rejects_unconfigured_arch(self):
        # Snap.requestBuild only allows dispatching a build for one of the
        # configured architectures.
        distroseries = self.factory.makeDistroSeries()
        procs = []
        arches = []
        for i in range(2):
            procs.append(self.factory.makeProcessor(supports_virtualized=True))
            arches.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=procs[i]
                )
            )
        snap = self.factory.makeSnap(
            distroseries=distroseries, processors=[procs[0]]
        )
        snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
        )
        self.assertRaises(
            SnapBuildDisallowedArchitecture,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            arches[1],
            PackagePublishingPocket.UPDATES,
        )
        inactive_proc = self.factory.makeProcessor(supports_virtualized=True)
        inactive_arch = self.makeBuildableDistroArchSeries(
            distroseries=self.factory.makeDistroSeries(
                status=SeriesStatus.OBSOLETE
            ),
            processor=inactive_proc,
        )
        snap_no_ds = self.factory.makeSnap(distroseries=None)
        snap_no_ds.requestBuild(
            snap_no_ds.owner,
            distroseries.main_archive,
            arches[0],
            PackagePublishingPocket.UPDATES,
        )
        snap_no_ds.requestBuild(
            snap_no_ds.owner,
            distroseries.main_archive,
            arches[1],
            PackagePublishingPocket.UPDATES,
        )
        self.assertRaises(
            SnapBuildDisallowedArchitecture,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            inactive_arch,
            PackagePublishingPocket.UPDATES,
        )

    def test_requestBuild_virtualization(self):
        # New builds are virtualized if any of the processor, snap or
        # archive require it.
        proc_arches = {}
        for proc_nonvirt in True, False:
            processor = self.factory.makeProcessor(
                supports_virtualized=True, supports_nonvirtualized=proc_nonvirt
            )
            distroarchseries = self.makeBuildableDistroArchSeries(
                processor=processor
            )
            proc_arches[proc_nonvirt] = (processor, distroarchseries)
        for proc_nonvirt, snap_virt, archive_virt, build_virt in (
            (True, False, False, False),
            (True, False, True, True),
            (True, True, False, True),
            (True, True, True, True),
            (False, False, False, True),
            (False, False, True, True),
            (False, True, False, True),
            (False, True, True, True),
        ):
            processor, distroarchseries = proc_arches[proc_nonvirt]
            snap = self.factory.makeSnap(
                distroseries=distroarchseries.distroseries,
                require_virtualized=snap_virt,
                processors=[processor],
            )
            archive = self.factory.makeArchive(
                distribution=distroarchseries.distroseries.distribution,
                owner=snap.owner,
                virtualized=archive_virt,
            )
            build = snap.requestBuild(
                snap.owner,
                archive,
                distroarchseries,
                PackagePublishingPocket.UPDATES,
            )
            self.assertEqual(build_virt, build.virtualized)

    def test_requestBuild_nonvirtualized(self):
        # A non-virtualized processor can build a snap package iff the snap
        # has require_virtualized set to False.
        processor = self.factory.makeProcessor(
            supports_virtualized=False, supports_nonvirtualized=True
        )
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries, processors=[processor]
        )
        self.assertRaises(
            SnapBuildDisallowedArchitecture,
            snap.requestBuild,
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
        )
        with admin_logged_in():
            snap.require_virtualized = False
        snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
        )

    def test_requestBuild_triggers_webhooks(self):
        # Requesting a build triggers webhooks.
        logger = self.useFixture(FakeLogger())
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries, processors=[processor]
        )
        hook = self.factory.makeWebhook(
            target=snap, event_types=["snap:build:0.1"]
        )
        build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
        )
        expected_payload = {
            "snap_build": Equals(canonical_url(build, force_local_path=True)),
            "action": Equals("created"),
            "snap": Equals(canonical_url(snap, force_local_path=True)),
            "build_request": Is(None),
            "status": Equals("Needs building"),
            "store_upload_status": Equals("Unscheduled"),
        }
        with person_logged_in(snap.owner):
            delivery = hook.deliveries.one()
            self.assertThat(
                delivery,
                MatchesStructure(
                    event_type=Equals("snap:build:0.1"),
                    payload=MatchesDict(expected_payload),
                ),
            )
            with dbuser(config.IWebhookDeliveryJobSource.dbuser):
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
                                "snap:build:0.1",
                                MatchesDict(expected_payload),
                            )
                        ]
                    ),
                )

    def test_requestBuild_with_platform_name(self):
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            processor=processor
        )
        snap = self.factory.makeSnap(
            distroseries=distroarchseries.distroseries, processors=[processor]
        )
        build = snap.requestBuild(
            snap.owner,
            snap.distro_series.main_archive,
            distroarchseries,
            PackagePublishingPocket.UPDATES,
            craft_platform="ubuntu-amd64",
        )
        self.assertEqual("ubuntu-amd64", build.craft_platform)

    def test_requestBuilds(self):
        # requestBuilds schedules a job and returns a corresponding
        # SnapBuildRequest.
        snap = self.factory.makeSnap()
        now = get_transaction_timestamp(IStore(snap))
        with person_logged_in(snap.owner.teamowner):
            request = snap.requestBuilds(
                snap.owner.teamowner,
                snap.distro_series.main_archive,
                PackagePublishingPocket.UPDATES,
                channels={"snapcraft": "edge", "snapd": "edge"},
            )
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                snap=Equals(snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(set, MatchesSetwise()),
                requester=Equals(snap.owner.teamowner),
                archive=Equals(snap.distro_series.main_archive),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                channels=MatchesDict(
                    {"snapcraft": Equals("edge"), "snapd": Equals("edge")}
                ),
                architectures=Is(None),
            ),
        )
        [job] = getUtility(ISnapRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                snap=Equals(snap),
                requester=Equals(snap.owner.teamowner),
                archive=Equals(snap.distro_series.main_archive),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                channels=Equals({"snapcraft": "edge", "snapd": "edge"}),
                architectures=Is(None),
            ),
        )

    def test_requestBuilds_without_distroseries(self):
        # requestBuilds schedules a job for a snap without a distroseries.
        snap = self.factory.makeSnap(distroseries=None)
        archive = self.factory.makeArchive()
        now = get_transaction_timestamp(IStore(snap))
        with person_logged_in(snap.owner.teamowner):
            request = snap.requestBuilds(
                snap.owner.teamowner,
                archive,
                PackagePublishingPocket.UPDATES,
                channels={"snapcraft": "edge", "snapd": "edge"},
            )
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                snap=Equals(snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(set, MatchesSetwise()),
                requester=Equals(snap.owner.teamowner),
                archive=Equals(archive),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                channels=MatchesDict(
                    {"snapcraft": Equals("edge"), "snapd": Equals("edge")}
                ),
                architectures=Is(None),
            ),
        )
        [job] = getUtility(ISnapRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                snap=Equals(snap),
                requester=Equals(snap.owner.teamowner),
                archive=Equals(archive),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                channels=Equals({"snapcraft": "edge", "snapd": "edge"}),
                architectures=Is(None),
            ),
        )

    def test_requestBuilds_with_architectures(self):
        # If asked to build for particular architectures, requestBuilds
        # passes those through to the job.
        snap = self.factory.makeSnap()
        now = get_transaction_timestamp(IStore(snap))
        with person_logged_in(snap.owner.teamowner):
            request = snap.requestBuilds(
                snap.owner.teamowner,
                snap.distro_series.main_archive,
                PackagePublishingPocket.UPDATES,
                channels={"snapcraft": "edge", "snapd": "edge"},
                architectures={"amd64", "i386"},
            )
        self.assertThat(
            request,
            MatchesStructure(
                date_requested=Equals(now),
                date_finished=Is(None),
                snap=Equals(snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(set, MatchesSetwise()),
                requester=Equals(snap.owner.teamowner),
                archive=Equals(snap.distro_series.main_archive),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                channels=MatchesDict(
                    {"snapcraft": Equals("edge"), "snapd": Equals("edge")}
                ),
                architectures=MatchesSetwise(Equals("amd64"), Equals("i386")),
            ),
        )
        [job] = getUtility(ISnapRequestBuildsJobSource).iterReady()
        self.assertThat(
            job,
            MatchesStructure(
                job_id=Equals(request.id),
                job=MatchesStructure.byEquality(status=JobStatus.WAITING),
                snap=Equals(snap),
                requester=Equals(snap.owner.teamowner),
                archive=Equals(snap.distro_series.main_archive),
                pocket=Equals(PackagePublishingPocket.UPDATES),
                channels=Equals({"snapcraft": "edge", "snapd": "edge"}),
                architectures=MatchesSetwise(Equals("amd64"), Equals("i386")),
            ),
        )

    def test__findBase_without_default(self):
        with admin_logged_in():
            snap_bases = [self.factory.makeSnapBase() for _ in range(2)]
        for snap_base in snap_bases:
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"base": snap_base.name}),
            )
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"base": "bare", "build-base": snap_base.name}),
            )
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"build-base": snap_base.name}),
            )
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"type": "base", "name": snap_base.name}),
            )
        self.assertRaises(
            NoSuchSnapBase, Snap._findBase, {"base": "nonexistent"}
        )
        self.assertRaises(NoSuchSnapBase, Snap._findBase, {"base": "bare"})
        self.assertRaises(
            NoSuchSnapBase,
            Snap._findBase,
            {"base": "bare", "build-base": "nonexistent"},
        )
        self.assertRaises(
            NoSuchSnapBase, Snap._findBase, {"build-base": "nonexistent"}
        )
        self.assertEqual((None, None), Snap._findBase({}))
        self.assertEqual(
            (None, None), Snap._findBase({"name": snap_bases[0].name})
        )

    def test__findBase_with_default(self):
        with admin_logged_in():
            snap_bases = [self.factory.makeSnapBase() for _ in range(2)]
        with admin_logged_in():
            getUtility(ISnapBaseSet).setDefault(snap_bases[0])
        for snap_base in snap_bases:
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"base": snap_base.name}),
            )
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"base": "bare", "build-base": snap_base.name}),
            )
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"build-base": snap_base.name}),
            )
            self.assertEqual(
                (snap_base, snap_base.name),
                Snap._findBase({"type": "base", "name": snap_base.name}),
            )
        self.assertRaises(
            NoSuchSnapBase, Snap._findBase, {"base": "nonexistent"}
        )
        self.assertRaises(NoSuchSnapBase, Snap._findBase, {"base": "bare"})
        self.assertRaises(
            NoSuchSnapBase,
            Snap._findBase,
            {"base": "bare", "build-base": "nonexistent"},
        )
        self.assertRaises(
            NoSuchSnapBase, Snap._findBase, {"build-base": "nonexistent"}
        )
        self.assertEqual((snap_bases[0], None), Snap._findBase({}))
        self.assertEqual(
            (snap_bases[0], None), Snap._findBase({"name": snap_bases[0].name})
        )

    def makeRequestBuildsJob(self, arch_tags, git_ref=None):
        distro = self.factory.makeDistribution()
        distroseries = self.factory.makeDistroSeries(distribution=distro)
        processors = [
            self.factory.makeProcessor(
                name=arch_tag, supports_virtualized=True
            )
            for arch_tag in arch_tags
        ]
        for processor in processors:
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        if git_ref is None:
            [git_ref] = self.factory.makeGitRefs()
        snap = self.factory.makeSnap(
            git_ref=git_ref, distroseries=distroseries, processors=processors
        )
        return getUtility(ISnapRequestBuildsJobSource).create(
            snap,
            snap.owner.teamowner,
            distro.main_archive,
            PackagePublishingPocket.RELEASE,
            {"snapcraft": "edge", "snapd": "edge"},
        )

    def assertRequestedBuildsMatch(
        self, builds, job, arch_tags, snap_base, channels, distro_series=None
    ):
        if distro_series is None:
            distro_series = job.snap.distro_series
        self.assertThat(
            builds,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        requester=Equals(job.requester),
                        snap=Equals(job.snap),
                        archive=Equals(job.archive),
                        distro_arch_series=Equals(distro_series[arch_tag]),
                        pocket=Equals(job.pocket),
                        snap_base=Equals(snap_base),
                        channels=Equals(channels),
                    )
                    for arch_tag in arch_tags
                )
            ),
        )

    def test_requestBuildsFromJob_restricts_explicit_list(self):
        # requestBuildsFromJob limits builds targeted at an explicit list of
        # architectures to those allowed for the snap.
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
            architectures:
              - build-on: sparc
              - build-on: i386
              - build-on: avr
            """
                )
            )
        )
        job = self.makeRequestBuildsJob(["sparc"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                channels=removeSecurityProxy(job.channels),
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds, job, ["sparc"], None, job.channels
        )

    def test_requestBuildsFromJob_no_explicit_architectures(self):
        # If the snap doesn't specify any architectures,
        # requestBuildsFromJob requests builds for all configured
        # architectures.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        job = self.makeRequestBuildsJob(["mips64el", "riscv64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                channels=removeSecurityProxy(job.channels),
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds, job, ["mips64el", "riscv64"], None, job.channels
        )

    def test_requestBuildsFromJob_architectures_parameter(self):
        # If an explicit set of architectures was given as a parameter,
        # requestBuildsFromJob intersects those with any other constraints
        # when requesting builds.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        job = self.makeRequestBuildsJob(["avr", "mips64el", "riscv64"])
        self.assertEqual(
            get_transaction_timestamp(IStore(job.snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                channels=removeSecurityProxy(job.channels),
                architectures={"avr", "riscv64"},
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds, job, ["avr", "riscv64"], None, job.channels
        )

    def test_requestBuildsFromJob_no_distroseries_explicit_base(self):
        # If the snap doesn't specify a distroseries but has an explicit
        # base, requestBuildsFromJob requests builds for the appropriate
        # distroseries for the base.
        self.useFixture(GitHostingFixture(blob="base: test-base\n"))
        distroseries = self.factory.makeDistroSeries()
        for arch_tag in ("mips64el", "riscv64"):
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=arch_tag,
                processor=self.factory.makeProcessor(
                    name=arch_tag, supports_virtualized=True
                ),
            )
        with admin_logged_in():
            snap_base = self.factory.makeSnapBase(
                name="test-base",
                distro_series=distroseries,
                build_channels={"snapcraft": "stable/launchpad-buildd"},
            )
            self.factory.makeSnapBase()
        snap = self.factory.makeSnap(
            distroseries=None, git_ref=self.factory.makeGitRefs()[0]
        )
        job = getUtility(ISnapRequestBuildsJobSource).create(
            snap,
            snap.owner.teamowner,
            distroseries.main_archive,
            PackagePublishingPocket.RELEASE,
            None,
        )
        self.assertEqual(
            get_transaction_timestamp(IStore(snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds,
            job,
            ["mips64el", "riscv64"],
            snap_base,
            snap_base.build_channels,
            distro_series=distroseries,
        )

    def test_requestBuildsFromJob_no_distroseries_no_explicit_base(self):
        # If the snap doesn't specify a distroseries and has no explicit
        # base, requestBuildsFromJob requests builds for the appropriate
        # distroseries for the default base.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        distroseries = self.factory.makeDistroSeries()
        for arch_tag in ("mips64el", "riscv64"):
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=arch_tag,
                processor=self.factory.makeProcessor(
                    name=arch_tag, supports_virtualized=True
                ),
            )
        with admin_logged_in():
            snap_base = self.factory.makeSnapBase(
                distro_series=distroseries,
                build_channels={"snapcraft": "stable/launchpad-buildd"},
            )
            getUtility(ISnapBaseSet).setDefault(snap_base)
            self.factory.makeSnapBase()
        snap = self.factory.makeSnap(
            distroseries=None, git_ref=self.factory.makeGitRefs()[0]
        )
        job = getUtility(ISnapRequestBuildsJobSource).create(
            snap,
            snap.owner.teamowner,
            distroseries.main_archive,
            PackagePublishingPocket.RELEASE,
            None,
        )
        self.assertEqual(
            get_transaction_timestamp(IStore(snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds,
            job,
            ["mips64el", "riscv64"],
            snap_base,
            snap_base.build_channels,
            distro_series=distroseries,
        )

    def test_requestBuildsFromJob_no_distroseries_no_default_base(self):
        # If the snap doesn't specify a distroseries and has an explicit
        # base, and there is no default base, requestBuildsFromJob gives up.
        self.useFixture(GitHostingFixture(blob="name: foo\n"))
        with admin_logged_in():
            snap_base = self.factory.makeSnapBase(
                build_channels={"snapcraft": "stable/launchpad-buildd"}
            )
        snap = self.factory.makeSnap(
            distroseries=None, git_ref=self.factory.makeGitRefs()[0]
        )
        job = getUtility(ISnapRequestBuildsJobSource).create(
            snap,
            snap.owner.teamowner,
            snap_base.distro_series.main_archive,
            PackagePublishingPocket.RELEASE,
            None,
        )
        transaction.commit()
        with person_logged_in(job.requester):
            self.assertRaises(
                NoSuchSnapBase,
                snap.requestBuildsFromJob,
                job.requester,
                job.archive,
                job.pocket,
                build_request=job.build_request,
            )

    def test_requestBuildsFromJob_snap_base_architectures(self):
        # requestBuildsFromJob intersects the architectures supported by the
        # snap base with any other constraints.
        self.useFixture(GitHostingFixture(blob="base: test-base\n"))
        processors = [
            self.factory.makeProcessor(supports_virtualized=True)
            for _ in range(3)
        ]
        distroseries = self.factory.makeDistroSeries()
        for processor in processors:
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        with admin_logged_in():
            snap_base = self.factory.makeSnapBase(
                name="test-base",
                distro_series=distroseries,
                build_channels={"snapcraft": "stable/launchpad-buildd"},
                processors=processors[:2],
            )
        snap = self.factory.makeSnap(
            distroseries=None, git_ref=self.factory.makeGitRefs()[0]
        )
        job = getUtility(ISnapRequestBuildsJobSource).create(
            snap,
            snap.owner.teamowner,
            snap_base.distro_series.main_archive,
            PackagePublishingPocket.RELEASE,
            None,
        )
        self.assertEqual(
            get_transaction_timestamp(IStore(snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds,
            job,
            [processor.name for processor in processors[:2]],
            snap_base,
            snap_base.build_channels,
            distro_series=snap_base.distro_series,
        )

    def test_requestBuildsFromJob_snap_base_build_channels_by_arch(self):
        # If the snap base declares different build channels for specific
        # architectures, then requestBuildsFromJob uses those when requesting
        # builds for those architectures.
        self.useFixture(GitHostingFixture(blob="base: test-base\n"))
        processors = [
            self.factory.makeProcessor(supports_virtualized=True)
            for _ in range(3)
        ]
        distroseries = self.factory.makeDistroSeries()
        for processor in processors:
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        with admin_logged_in():
            snap_base = self.factory.makeSnapBase(
                name="test-base",
                distro_series=distroseries,
                build_channels={
                    "snapcraft": "stable/launchpad-buildd",
                    "_byarch": {
                        processors[0].name: {
                            "core": "candidate",
                            "snapcraft": "5.x/stable",
                        },
                    },
                },
            )
        snap = self.factory.makeSnap(
            distroseries=None, git_ref=self.factory.makeGitRefs()[0]
        )
        job = getUtility(ISnapRequestBuildsJobSource).create(
            snap,
            snap.owner.teamowner,
            snap_base.distro_series.main_archive,
            PackagePublishingPocket.RELEASE,
            None,
        )
        self.assertEqual(
            get_transaction_timestamp(IStore(snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                build_request=job.build_request,
            )
        self.assertThat(
            builds,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        requester=Equals(job.requester),
                        snap=Equals(job.snap),
                        archive=Equals(job.archive),
                        distro_arch_series=Equals(
                            snap_base.distro_series[processor.name]
                        ),
                        pocket=Equals(job.pocket),
                        snap_base=Equals(snap_base),
                        channels=Equals(channels),
                    )
                    for processor, channels in (
                        (
                            processors[0],
                            {"core": "candidate", "snapcraft": "5.x/stable"},
                        ),
                        (
                            processors[1],
                            {"snapcraft": "stable/launchpad-buildd"},
                        ),
                        (
                            processors[2],
                            {"snapcraft": "stable/launchpad-buildd"},
                        ),
                    )
                )
            ),
        )

    def test_requestBuildsFromJob_unsupported_remote(self):
        # If the snap is based on an external Git repository from which we
        # don't support fetching blobs, requestBuildsFromJob falls back to
        # requesting builds for all configured architectures.
        git_ref = self.factory.makeGitRefRemote(
            repository_url="https://example.com/foo.git"
        )
        job = self.makeRequestBuildsJob(
            ["mips64el", "riscv64"], git_ref=git_ref
        )
        self.assertEqual(
            get_transaction_timestamp(IStore(job.snap)), job.date_created
        )
        transaction.commit()
        with person_logged_in(job.requester):
            builds = job.snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                removeSecurityProxy(job.channels),
                build_request=job.build_request,
            )
        self.assertRequestedBuildsMatch(
            builds, job, ["mips64el", "riscv64"], None, job.channels
        )

    def test_requestBuildsFromJob_triggers_webhooks(self):
        # requestBuildsFromJob triggers webhooks, and the payload includes a
        # link to the build request.
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
            architectures:
              - build-on: avr
              - build-on: riscv64
            """
                )
            )
        )
        logger = self.useFixture(FakeLogger())
        job = self.makeRequestBuildsJob(["avr", "riscv64", "sparc"])
        hook = self.factory.makeWebhook(
            target=job.snap, event_types=["snap:build:0.1"]
        )
        with person_logged_in(job.requester):
            builds = job.snap.requestBuildsFromJob(
                job.requester,
                job.archive,
                job.pocket,
                removeSecurityProxy(job.channels),
                build_request=job.build_request,
            )
            self.assertEqual(2, len(builds))
            payload_matchers = [
                MatchesDict(
                    {
                        "snap_build": Equals(
                            canonical_url(build, force_local_path=True)
                        ),
                        "action": Equals("created"),
                        "snap": Equals(
                            canonical_url(job.snap, force_local_path=True)
                        ),
                        "build_request": Equals(
                            canonical_url(
                                job.build_request, force_local_path=True
                            )
                        ),
                        "status": Equals("Needs building"),
                        "store_upload_status": Equals("Unscheduled"),
                    }
                )
                for build in builds
            ]
            self.assertThat(
                hook.deliveries,
                MatchesSetwise(
                    *(
                        MatchesStructure(
                            event_type=Equals("snap:build:0.1"),
                            payload=payload_matcher,
                        )
                        for payload_matcher in payload_matchers
                    )
                ),
            )
            self.assertThat(
                logger.output,
                LogsScheduledWebhooks(
                    [
                        (hook, "snap:build:0.1", payload_matcher)
                        for payload_matcher in payload_matchers
                    ]
                ),
            )

    def test_requestAutoBuilds(self):
        # requestAutoBuilds creates new builds for all configured
        # architectures with appropriate parameters.
        distroseries = self.factory.makeDistroSeries()
        dases = []
        for _ in range(3):
            processor = self.factory.makeProcessor(supports_virtualized=True)
            dases.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=processor
                )
            )
        archive = self.factory.makeArchive()
        snap = self.factory.makeSnap(
            distroseries=distroseries,
            processors=[das.processor for das in dases[:2]],
            auto_build_archive=archive,
            auto_build_pocket=PackagePublishingPocket.PROPOSED,
        )
        with person_logged_in(snap.owner):
            builds = snap.requestAutoBuilds()
        self.assertThat(
            builds,
            MatchesSetwise(
                *(
                    MatchesStructure(
                        requester=Equals(snap.owner),
                        snap=Equals(snap),
                        archive=Equals(archive),
                        distro_arch_series=Equals(das),
                        pocket=Equals(PackagePublishingPocket.PROPOSED),
                        channels=Is(None),
                    )
                    for das in dases[:2]
                )
            ),
        )

    def test_requestAutoBuilds_channels(self):
        # requestAutoBuilds honours Snap.auto_build_channels.
        distroseries = self.factory.makeDistroSeries()
        dases = []
        for _ in range(3):
            processor = self.factory.makeProcessor(supports_virtualized=True)
            dases.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=processor
                )
            )
        archive = self.factory.makeArchive()
        snap = self.factory.makeSnap(
            distroseries=distroseries,
            processors=[das.processor for das in dases[:2]],
            auto_build_archive=archive,
            auto_build_pocket=PackagePublishingPocket.PROPOSED,
            auto_build_channels={"snapcraft": "edge", "snapd": "edge"},
        )
        with person_logged_in(snap.owner):
            builds = snap.requestAutoBuilds()
        self.assertThat(
            builds,
            MatchesSetwise(
                *(
                    MatchesStructure.byEquality(
                        requester=snap.owner,
                        snap=snap,
                        archive=archive,
                        distro_arch_series=das,
                        pocket=PackagePublishingPocket.PROPOSED,
                        channels={"snapcraft": "edge", "snapd": "edge"},
                    )
                    for das in dases[:2]
                )
            ),
        )

    def test_getBuilds(self):
        # Test the various getBuilds methods.
        snap = self.factory.makeSnap()
        builds = [self.factory.makeSnapBuild(snap=snap) for x in range(3)]
        # We want the latest builds first.
        builds.reverse()

        self.assertEqual(builds, list(snap.builds))
        self.assertEqual([], list(snap.completed_builds))
        self.assertEqual(builds, list(snap.pending_builds))

        # Change the status of one of the builds and retest.
        builds[0].updateStatus(BuildStatus.BUILDING)
        builds[0].updateStatus(BuildStatus.FULLYBUILT)
        self.assertEqual(builds, list(snap.builds))
        self.assertEqual(builds[:1], list(snap.completed_builds))
        self.assertEqual(builds[1:], list(snap.pending_builds))

    def test_getBuilds_cancelled_never_started_last(self):
        # A cancelled build that was never even started sorts to the end.
        snap = self.factory.makeSnap()
        fullybuilt = self.factory.makeSnapBuild(snap=snap)
        instacancelled = self.factory.makeSnapBuild(snap=snap)
        fullybuilt.updateStatus(BuildStatus.BUILDING)
        fullybuilt.updateStatus(BuildStatus.FULLYBUILT)
        instacancelled.updateStatus(BuildStatus.CANCELLED)
        self.assertEqual([fullybuilt, instacancelled], list(snap.builds))
        self.assertEqual(
            [fullybuilt, instacancelled], list(snap.completed_builds)
        )
        self.assertEqual([], list(snap.pending_builds))

    def test_getBuilds_privacy(self):
        # The various getBuilds methods exclude builds against invisible
        # archives.
        snap = self.factory.makeSnap()
        archive = self.factory.makeArchive(
            distribution=snap.distro_series.distribution,
            owner=snap.owner,
            private=True,
        )
        with person_logged_in(snap.owner):
            build = self.factory.makeSnapBuild(snap=snap, archive=archive)
            self.assertEqual([build], list(snap.builds))
            self.assertEqual([build], list(snap.pending_builds))
        self.assertEqual([], list(snap.builds))
        self.assertEqual([], list(snap.pending_builds))

    def test_delete_without_builds(self):
        # A snap package with no builds can be deleted.
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )
        self.assertTrue(getUtility(ISnapSet).exists(owner, "condemned"))
        with person_logged_in(snap.owner):
            snap.destroySelf()
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))

    def test_getBuildByStoreRevision(self):
        snap1 = self.factory.makeSnap()
        build = self.factory.makeSnapBuild(
            snap=snap1, status=BuildStatus.FULLYBUILT
        )
        snap1_lfa = self.factory.makeLibraryFileAlias(
            filename="test-snap.snap",
            content=b"dummy snap content",
            db_only=True,
        )
        self.factory.makeSnapFile(snapbuild=build, libraryfile=snap1_lfa)

        # There is no build with revision 5 for snap1
        self.assertIsNone(snap1.getBuildByStoreRevision(5))

        # Upload build1 and check we return it by version 1
        job = getUtility(ISnapStoreUploadJobSource).create(build)
        client = FakeSnapStoreClient()
        client.uploadFile.result = 1
        client.push.result = (
            "http://sca.example/dev/api/snaps/1/builds/1/status"
        )
        client.checkStatus.result = (
            "http://sca.example/dev/click-apps/1/rev/1/",
            1,
        )
        self.useFixture(ZopeUtilityFixture(client, ISnapStoreClient))
        with dbuser(config.ISnapStoreUploadJobSource.dbuser):
            run_isolated_jobs([job])
        self.assertEqual(
            SnapBuildStoreUploadStatus.UPLOADED, build.store_upload_status
        )
        self.assertEqual(build.store_upload_revision, 1)
        self.assertEqual(snap1.getBuildByStoreRevision(1), build)

        # build & upload again, check revision
        # and that we return the second build for revision 2
        build2 = self.factory.makeSnapBuild(
            snap=snap1, status=BuildStatus.FULLYBUILT
        )
        snap2_lfa = self.factory.makeLibraryFileAlias(
            filename="test-snap.snap",
            content=b"dummy snap content",
            db_only=True,
        )
        self.factory.makeSnapFile(snapbuild=build2, libraryfile=snap2_lfa)
        job = getUtility(ISnapStoreUploadJobSource).create(build2)
        client = FakeSnapStoreClient()
        client.uploadFile.result = 2
        client.push.result = (
            "http://sca.example/dev/api/snaps/1/builds/2/status"
        )
        client.checkStatus.result = (
            "http://sca.example/dev/click-apps/1/rev/2/",
            2,
        )
        self.useFixture(ZopeUtilityFixture(client, ISnapStoreClient))
        with dbuser(config.ISnapStoreUploadJobSource.dbuser):
            run_isolated_jobs([job])
        self.assertEqual(
            SnapBuildStoreUploadStatus.UPLOADED, build2.store_upload_status
        )
        self.assertEqual(build2.store_upload_revision, 2)
        self.assertEqual(snap1.getBuildByStoreRevision(2), build2)
        self.assertEqual(snap1.getBuildByStoreRevision(1), build)

    def test_getBuildSummariesForSnapBuildIds(self):
        snap1 = self.factory.makeSnap()
        snap2 = self.factory.makeSnap()
        build11 = self.factory.makeSnapBuild(snap=snap1)
        build12 = self.factory.makeSnapBuild(snap=snap1)
        build2 = self.factory.makeSnapBuild(snap=snap2)
        self.factory.makeSnapBuild()
        summary1 = snap1.getBuildSummariesForSnapBuildIds(
            [build11.id, build12.id]
        )
        summary2 = snap2.getBuildSummariesForSnapBuildIds([build2.id])
        summary_matcher = MatchesDict(
            {
                "status": Equals("NEEDSBUILD"),
                "buildstate": Equals("Needs building"),
                "when_complete": Is(None),
                "when_complete_estimate": Is(False),
                "build_log_url": Is(None),
                "build_log_size": Is(None),
            }
        )
        self.assertThat(
            summary1,
            MatchesDict(
                {
                    build11.id: summary_matcher,
                    build12.id: summary_matcher,
                }
            ),
        )
        self.assertThat(summary2, MatchesDict({build2.id: summary_matcher}))

    def test_getBuildSummariesForSnapBuildIds_empty_input(self):
        snap = self.factory.makeSnap()
        self.factory.makeSnapBuild(snap=snap)
        self.assertEqual({}, snap.getBuildSummariesForSnapBuildIds(None))
        self.assertEqual({}, snap.getBuildSummariesForSnapBuildIds([]))
        self.assertEqual({}, snap.getBuildSummariesForSnapBuildIds(()))
        self.assertEqual({}, snap.getBuildSummariesForSnapBuildIds([None]))

    def test_getBuildSummariesForSnapBuildIds_not_matching_snap(self):
        # Should not return build summaries of other snaps.
        snap1 = self.factory.makeSnap()
        snap2 = self.factory.makeSnap()
        self.factory.makeSnapBuild(snap=snap1)
        build2 = self.factory.makeSnapBuild(snap=snap2)
        summary1 = snap1.getBuildSummariesForSnapBuildIds([build2.id])
        self.assertEqual({}, summary1)

    def test_getBuildSummariesForSnapBuildIds_when_complete_field(self):
        # Summary "when_complete" should be None unless estimate date or
        # finish date is available.
        snap = self.factory.makeSnap()
        build = self.factory.makeSnapBuild(snap=snap)
        self.assertIsNone(build.date)
        summary = snap.getBuildSummariesForSnapBuildIds([build.id])
        self.assertIsNone(summary[build.id]["when_complete"])
        removeSecurityProxy(build).date_finished = UTC_NOW
        summary = snap.getBuildSummariesForSnapBuildIds([build.id])
        self.assertEqual("a moment ago", summary[build.id]["when_complete"])

    def test_getBuildSummariesForSnapBuildIds_log_size_field(self):
        # Summary "build_log_size" should be None unless the build has a log.
        snap = self.factory.makeSnap()
        build = self.factory.makeSnapBuild(snap=snap)
        self.assertIsNone(build.log)
        summary = snap.getBuildSummariesForSnapBuildIds([build.id])
        self.assertIsNone(summary[build.id]["build_log_size"])
        removeSecurityProxy(build).log = self.factory.makeLibraryFileAlias(
            content=b"x" * 12345, db_only=True
        )
        summary = snap.getBuildSummariesForSnapBuildIds([build.id])
        self.assertEqual(12345, summary[build.id]["build_log_size"])

    def test_getBuildSummariesForSnapBuildIds_query_count(self):
        # DB query count should remain constant regardless of number of builds.
        def snap_build_creator(snap):
            build = self.factory.makeSnapBuild(snap=snap)
            removeSecurityProxy(build).log = self.factory.makeLibraryFileAlias(
                db_only=True
            )
            return build

        snap = self.factory.makeSnap()
        # Use an in-memory feature controller to avoid feature flag queries.
        with MemoryFeatureFixture({}):
            recorder1, recorder2 = record_two_runs(
                lambda: snap.getBuildSummariesForSnapBuildIds(
                    build.id for build in snap.builds
                ),
                lambda: snap_build_creator(snap),
                1,
                5,
            )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_getBuildSummaries(self):
        snap1 = self.factory.makeSnap()
        snap2 = self.factory.makeSnap()
        request11 = self.factory.makeSnapBuildRequest(snap=snap1)
        request12 = self.factory.makeSnapBuildRequest(snap=snap1)
        request2 = self.factory.makeSnapBuildRequest(snap=snap2)
        self.factory.makeSnapBuildRequest()
        build11 = self.factory.makeSnapBuild(snap=snap1)
        build12 = self.factory.makeSnapBuild(snap=snap1)
        build2 = self.factory.makeSnapBuild(snap=snap2)
        self.factory.makeSnapBuild()
        summary1 = snap1.getBuildSummaries(
            request_ids=[request11.id, request12.id],
            build_ids=[build11.id, build12.id],
        )
        summary2 = snap2.getBuildSummaries(
            request_ids=[request2.id], build_ids=[build2.id]
        )
        request_summary_matcher = MatchesDict(
            {
                "status": Equals("PENDING"),
                "error_message": Is(None),
                "builds": Equals([]),
            }
        )
        build_summary_matcher = MatchesDict(
            {
                "status": Equals("NEEDSBUILD"),
                "buildstate": Equals("Needs building"),
                "when_complete": Is(None),
                "when_complete_estimate": Is(False),
                "build_log_url": Is(None),
                "build_log_size": Is(None),
            }
        )
        self.assertThat(
            summary1,
            MatchesDict(
                {
                    "requests": MatchesDict(
                        {
                            request11.id: request_summary_matcher,
                            request12.id: request_summary_matcher,
                        }
                    ),
                    "builds": MatchesDict(
                        {
                            build11.id: build_summary_matcher,
                            build12.id: build_summary_matcher,
                        }
                    ),
                }
            ),
        )
        self.assertThat(
            summary2,
            MatchesDict(
                {
                    "requests": MatchesDict(
                        {request2.id: request_summary_matcher}
                    ),
                    "builds": MatchesDict({build2.id: build_summary_matcher}),
                }
            ),
        )

    def test_getBuildSummaries_empty_input(self):
        snap = self.factory.makeSnap()
        self.factory.makeSnapBuildRequest(snap=snap)
        self.assertEqual(
            {"requests": {}, "builds": {}},
            snap.getBuildSummaries(request_ids=None, build_ids=None),
        )
        self.assertEqual(
            {"requests": {}, "builds": {}},
            snap.getBuildSummaries(request_ids=[], build_ids=[]),
        )
        self.assertEqual(
            {"requests": {}, "builds": {}},
            snap.getBuildSummaries(request_ids=(), build_ids=()),
        )

    def test_getBuildSummaries_not_matching_snap(self):
        # getBuildSummaries does not return information for other snaps.
        snap1 = self.factory.makeSnap()
        snap2 = self.factory.makeSnap()
        self.factory.makeSnapBuildRequest(snap=snap1)
        self.factory.makeSnapBuild(snap=snap1)
        request2 = self.factory.makeSnapBuildRequest(snap=snap2)
        build2 = self.factory.makeSnapBuild(snap=snap2)
        summary1 = snap1.getBuildSummaries(
            request_ids=[request2.id], build_ids=[build2.id]
        )
        self.assertEqual({"requests": {}, "builds": {}}, summary1)

    def test_getBuildSummaries_request_error_message_field(self):
        # The error_message field for a build request should be None unless
        # the build request failed.
        snap = self.factory.makeSnap()
        request = self.factory.makeSnapBuildRequest(snap=snap)
        self.assertIsNone(request.error_message)
        summary = snap.getBuildSummaries(request_ids=[request.id])
        self.assertIsNone(summary["requests"][request.id]["error_message"])
        job = removeSecurityProxy(request)._job
        removeSecurityProxy(job).error_message = "Boom"
        summary = snap.getBuildSummaries(request_ids=[request.id])
        self.assertEqual(
            "Boom", summary["requests"][request.id]["error_message"]
        )

    def test_getBuildSummaries_request_builds_field(self):
        # The builds field should be an empty list unless the build request
        # has completed and produced builds.
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
            architectures:
              - build-on: mips64el
              - build-on: riscv64
            """
                )
            )
        )
        job = self.makeRequestBuildsJob(["mips64el", "riscv64", "sh4"])
        snap = job.snap
        request = snap.getBuildRequest(job.job_id)
        self.assertEqual([], list(request.builds))
        summary = snap.getBuildSummaries(request_ids=[request.id])
        self.assertEqual([], summary["requests"][request.id]["builds"])
        with person_logged_in(job.requester):
            with dbuser(config.ISnapRequestBuildsJobSource.dbuser):
                JobRunner([job]).runAll()
        summary = snap.getBuildSummaries(request_ids=[request.id])
        expected_snap_url = "/~%s/+snap/%s" % (snap.owner.name, snap.name)
        builds = sorted(request.builds, key=attrgetter("id"), reverse=True)
        expected_builds = [
            {
                "self_link": expected_snap_url + "/+build/%d" % build.id,
                "id": build.id,
                "distro_arch_series_link": "/%s/%s/%s"
                % (
                    snap.distro_series.distribution.name,
                    snap.distro_series.name,
                    build.distro_arch_series.architecturetag,
                ),
                "architecture_tag": build.distro_arch_series.architecturetag,
                "archive_link": (
                    '<a href="/%s" class="sprite distribution">%s</a>'
                    % (
                        build.archive.distribution.name,
                        build.archive.displayname,
                    )
                ),
                "status": "NEEDSBUILD",
                "buildstate": "Needs building",
                "when_complete": None,
                "when_complete_estimate": False,
                "build_log_url": None,
                "build_log_size": None,
            }
            for build in builds
        ]
        self.assertEqual(
            expected_builds, summary["requests"][request.id]["builds"]
        )

    def test_getBuildSummaries_query_count(self):
        # The DB query count remains constant regardless of the number of
        # requests and the number of builds resulting from them.
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
            architectures:
              - build-on: mips64el
              - build-on: riscv64
            """
                )
            )
        )
        job = self.makeRequestBuildsJob(["mips64el", "riscv64", "sh4"])
        snap = job.snap
        request_ids = []
        build_ids = []

        def create_items():
            request = self.factory.makeSnapBuildRequest(
                snap=snap, archive=self.factory.makeArchive()
            )
            request_ids.append(request.id)
            job = removeSecurityProxy(request)._job
            with person_logged_in(snap.owner.teamowner):
                # Using the normal job runner interferes with SQL statement
                # recording, so we run the job by hand.
                job.start()
                job.run()
                job.complete()
            # XXX cjwatson 2018-06-20: Queued builds with
            # BuildQueueStatus.WAITING incur extra queries per build due to
            # estimating start times.  For the moment, we dodge this by
            # starting the builds.
            for build in job.builds:
                build.buildqueue_record.markAsBuilding(
                    self.factory.makeBuilder()
                )
            build_ids.append(
                self.factory.makeSnapBuild(
                    snap=snap, archive=self.factory.makeArchive()
                ).id
            )

        recorder1, recorder2 = record_two_runs(
            lambda: snap.getBuildSummaries(
                request_ids=request_ids, build_ids=build_ids
            ),
            create_items,
            1,
            5,
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_snap_use_fetch_service(self):
        person = self.factory.makePerson()
        with person_logged_in(person):
            snap = self.factory.makeSnap(registrant=person, owner=person)

            self.assertFalse(snap.use_fetch_service)
            snap.use_fetch_service = True
            self.assertTrue(snap.use_fetch_service)

    def test_snap_fetch_service_policy(self):
        person = self.factory.makePerson()
        with person_logged_in(person):
            snap = self.factory.makeSnap(registrant=person, owner=person)

            snap.use_fetch_service = True
            self.assertEqual(
                snap.fetch_service_policy, FetchServicePolicy.STRICT
            )
            snap.fetch_service_policy = FetchServicePolicy.PERMISSIVE
            self.assertEqual(
                snap.fetch_service_policy, FetchServicePolicy.PERMISSIVE
            )


class TestSnapDeleteWithBuilds(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_delete_with_builds(self):
        # A snap package with builds can be deleted.  Doing so deletes all
        # its builds, their files, and any associated build jobs too.
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
        )
        build = self.factory.makeSnapBuild(snap=snap)
        build_queue = build.queueBuild()
        snapfile = self.factory.makeSnapFile(snapbuild=build)
        snap_build_job = getUtility(ISnapStoreUploadJobSource).create(build)
        self.assertTrue(getUtility(ISnapSet).exists(owner, "condemned"))
        other_build = self.factory.makeSnapBuild()
        other_build.queueBuild()
        store = Store.of(build)
        store.flush()
        build_id = build.id
        build_queue_id = build_queue.id
        build_farm_job_id = removeSecurityProxy(build).build_farm_job_id
        snap_build_job_id = snap_build_job.job.job_id
        snapfile_id = removeSecurityProxy(snapfile).id
        with person_logged_in(snap.owner):
            snap.destroySelf()
        flush_database_caches()
        # The deleted snap and its builds are gone.
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))
        self.assertIsNone(getUtility(ISnapBuildSet).getByID(build_id))
        self.assertIsNone(store.get(BuildQueue, build_queue_id))
        self.assertIsNone(store.get(BuildFarmJob, build_farm_job_id))
        self.assertIsNone(store.get(SnapFile, snapfile_id))
        self.assertIsNone(store.get(SnapBuildJob, snap_build_job_id))
        # Unrelated builds are still present.
        clear_property_cache(other_build)
        self.assertEqual(
            other_build, getUtility(ISnapBuildSet).getByID(other_build.id)
        )
        self.assertIsNotNone(other_build.buildqueue_record)

    def test_delete_with_build_requests(self):
        # A snap package with build requests can be deleted.
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        processor = self.factory.makeProcessor(supports_virtualized=True)
        das = self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag=processor.name,
            processor=processor,
        )
        das.addOrUpdateChroot(
            self.factory.makeLibraryFileAlias(
                filename="fake_chroot.tar.gz", db_only=True
            )
        )
        self.useFixture(
            GitHostingFixture(
                blob=dedent(
                    """\
            architectures:
              - build-on: %s
            """
                    % processor.name
                )
            )
        )
        [git_ref] = self.factory.makeGitRefs()
        condemned_snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            name="condemned",
            git_ref=git_ref,
        )
        other_snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            distroseries=distroseries,
            git_ref=git_ref,
        )
        self.assertTrue(getUtility(ISnapSet).exists(owner, "condemned"))
        with person_logged_in(owner):
            requests = []
            jobs = []
            for snap in (condemned_snap, other_snap):
                requests.append(
                    snap.requestBuilds(
                        owner,
                        distroseries.main_archive,
                        PackagePublishingPocket.UPDATES,
                    )
                )
                jobs.append(removeSecurityProxy(requests[-1])._job)
            with dbuser(config.ISnapRequestBuildsJobSource.dbuser):
                JobRunner(jobs).runAll()
            for job in jobs:
                self.assertEqual(JobStatus.COMPLETED, job.job.status)
            for request in requests:
                self.assertNotEqual([], request.builds)
        store = Store.of(condemned_snap)
        store.flush()
        job_ids = [job.job_id for job in jobs]
        build_ids = [
            [build.id for build in request.builds] for request in requests
        ]
        with person_logged_in(condemned_snap.owner):
            condemned_snap.destroySelf()
        flush_database_caches()
        # The deleted snap, its build requests, and its builds are gone.
        self.assertFalse(getUtility(ISnapSet).exists(owner, "condemned"))
        self.assertIsNone(store.get(SnapJob, job_ids[0]))
        for build_id in build_ids[0]:
            self.assertIsNone(getUtility(ISnapBuildSet).getByID(build_id))
        # Unrelated build requests and builds are still present.
        self.assertEqual(
            removeSecurityProxy(jobs[1]).context,
            store.get(SnapJob, job_ids[1]),
        )
        other_builds = [
            getUtility(ISnapBuildSet).getByID(build_id)
            for build_id in build_ids[1]
        ]
        self.assertEqual(list(requests[1].builds), other_builds)

    def test_related_webhooks_deleted(self):
        owner = self.factory.makePerson()
        snap = self.factory.makeSnap(registrant=owner, owner=owner)
        webhook = self.factory.makeWebhook(target=snap)
        with person_logged_in(snap.owner):
            webhook.ping()
            snap.destroySelf()
            transaction.commit()
            self.assertRaises(LostObjectError, getattr, webhook, "target")


class TestSnapVisibility(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def getSnapGrants(self, snap, person=None):
        conditions = [AccessArtifact.snap == snap]
        if person is not None:
            conditions.append(AccessArtifactGrant.grantee == person)
        return IStore(AccessArtifactGrant).find(
            AccessArtifactGrant,
            AccessArtifactGrant.abstract_artifact_id == AccessArtifact.id,
            *conditions,
        )

    def test_only_owner_can_grant_access(self):
        owner = self.factory.makePerson()
        snap = self.factory.makeSnap(
            registrant=owner, owner=owner, private=True
        )
        other_person = self.factory.makePerson()
        with person_logged_in(other_person):
            self.assertRaises(Unauthorized, getattr, snap, "subscribe")
        with person_logged_in(owner):
            snap.subscribe(other_person, owner)

    def test_private_is_invisible_by_default(self):
        owner = self.factory.makePerson()
        person = self.factory.makePerson()
        snap = self.factory.makeSnap(
            registrant=owner, owner=owner, private=True
        )
        with person_logged_in(owner):
            self.assertFalse(snap.visibleByUser(person))

    def test_private_is_visible_by_team_member(self):
        person = self.factory.makePerson()
        team = self.factory.makeTeam(
            members=[person], membership_policy=TeamMembershipPolicy.MODERATED
        )
        snap = self.factory.makeSnap(
            private=True, owner=team, registrant=person
        )
        with person_logged_in(team):
            self.assertTrue(snap.visibleByUser(person))

    def test_subscribing_changes_visibility(self):
        person = self.factory.makePerson()
        owner = self.factory.makePerson()
        snap = self.factory.makeSnap(
            registrant=owner, owner=owner, private=True
        )

        with person_logged_in(owner):
            self.assertFalse(snap.visibleByUser(person))
            snap.subscribe(person, snap.owner)
            self.assertThat(
                snap.getSubscription(person),
                MatchesStructure(
                    person=Equals(person),
                    snap=Equals(snap),
                    subscribed_by=Equals(snap.owner),
                    date_created=IsInstance(datetime),
                ),
            )
            # Calling again should be a no-op.
            snap.subscribe(person, snap.owner)
            self.assertTrue(snap.visibleByUser(person))

            snap.unsubscribe(person, snap.owner)
            self.assertFalse(snap.visibleByUser(person))
            self.assertIsNone(snap.getSubscription(person))

    def test_snap_owner_can_unsubscribe_anyone(self):
        person = self.factory.makePerson()
        owner = self.factory.makePerson()
        admin = self.factory.makeAdministrator()
        snap = self.factory.makeSnap(
            registrant=owner, owner=owner, private=True
        )
        with person_logged_in(admin):
            snap.subscribe(person, admin)
            self.assertTrue(snap.visibleByUser(person))
        with person_logged_in(owner):
            snap.unsubscribe(person, owner)
            self.assertFalse(snap.visibleByUser(person))

    def test_reconcile_set_public(self):
        owner = self.factory.makePerson()
        snap = self.factory.makeSnap(
            registrant=owner, owner=owner, private=True
        )
        another_user = self.factory.makePerson()
        with admin_logged_in():
            snap.subscribe(another_user, snap.owner)
            self.assertEqual(1, self.getSnapGrants(snap, another_user).count())
            self.assertThat(
                snap.getSubscription(another_user),
                MatchesStructure(
                    person=Equals(another_user),
                    snap=Equals(snap),
                    subscribed_by=Equals(snap.owner),
                    date_created=IsInstance(datetime),
                ),
            )

            snap.information_type = InformationType.PUBLIC
            self.assertEqual(0, self.getSnapGrants(snap, another_user).count())
            self.assertThat(
                snap.getSubscription(another_user),
                MatchesStructure(
                    person=Equals(another_user),
                    snap=Equals(snap),
                    subscribed_by=Equals(snap.owner),
                    date_created=IsInstance(datetime),
                ),
            )

    def test_reconcile_permissions_setting_project(self):
        owner = self.factory.makePerson()
        old_project = self.factory.makeProduct()
        getUtility(IAccessPolicySource).create(
            [(old_project, InformationType.PROPRIETARY)]
        )

        snap = self.factory.makeSnap(
            project=old_project, private=True, registrant=owner, owner=owner
        )

        # Owner automatically gets a grant.
        with person_logged_in(owner):
            self.assertTrue(snap.visibleByUser(snap.owner))
            self.assertEqual(1, self.getSnapGrants(snap).count())

        new_project = self.factory.makeProduct()
        getUtility(IAccessPolicySource).create(
            [(new_project, InformationType.PROPRIETARY)]
        )
        another_person = self.factory.makePerson()
        with person_logged_in(owner):
            snap.subscribe(another_person, owner)
            self.assertTrue(snap.visibleByUser(another_person))
            self.assertEqual(2, self.getSnapGrants(snap).count())
            self.assertThat(
                snap.getSubscription(another_person),
                MatchesStructure(
                    person=Equals(another_person),
                    snap=Equals(snap),
                    subscribed_by=Equals(snap.owner),
                    date_created=IsInstance(datetime),
                ),
            )

            snap.setProject(new_project)
            self.assertTrue(snap.visibleByUser(another_person))
            self.assertEqual(2, self.getSnapGrants(snap).count())
            self.assertThat(
                snap.getSubscription(another_person),
                MatchesStructure(
                    person=Equals(another_person),
                    snap=Equals(snap),
                    subscribed_by=Equals(snap.owner),
                    date_created=IsInstance(datetime),
                ),
            )


class TestSnapSet(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_class_implements_interfaces(self):
        # The SnapSet class implements ISnapSet.
        self.assertProvides(getUtility(ISnapSet), ISnapSet)

    def makeSnapComponents(self, branch=None, git_ref=None):
        """Return a dict of values that can be used to make a Snap.

        Suggested use: provide as kwargs to ISnapSet.new.

        :param branch: An `IBranch`, or None.
        :param git_ref: An `IGitRef`, or None.
        """
        registrant = self.factory.makePerson()
        components = dict(
            registrant=registrant,
            owner=self.factory.makeTeam(owner=registrant),
            distro_series=self.factory.makeDistroSeries(),
            name=self.factory.getUniqueUnicode("snap-name"),
            pro_enable=False,
        )
        if branch is None and git_ref is None:
            branch = self.factory.makeAnyBranch()
        if branch is not None:
            components["branch"] = branch
        else:
            components["git_ref"] = git_ref
        return components

    def test_creation_bzr(self):
        # The metadata entries supplied when a Snap is created for a Bazaar
        # branch are present on the new object.
        branch = self.factory.makeAnyBranch()
        components = self.makeSnapComponents(branch=branch)
        snap = getUtility(ISnapSet).new(**components)
        self.assertEqual(components["registrant"], snap.registrant)
        self.assertEqual(components["owner"], snap.owner)
        self.assertEqual(components["distro_series"], snap.distro_series)
        self.assertEqual(components["name"], snap.name)
        self.assertEqual(branch, snap.branch)
        self.assertIsNone(snap.git_repository)
        self.assertIsNone(snap.git_path)
        self.assertIsNone(snap.git_ref)
        self.assertFalse(snap.auto_build)
        self.assertIsNone(snap.auto_build_archive)
        self.assertIsNone(snap.auto_build_pocket)
        self.assertIsNone(snap.auto_build_channels)
        self.assertTrue(snap.require_virtualized)
        self.assertFalse(snap.private)
        self.assertTrue(snap.allow_internet)
        self.assertFalse(snap.build_source_tarball)
        self.assertFalse(snap.pro_enable)
        self.assertFalse(snap.use_fetch_service)
        self.assertEqual(FetchServicePolicy.STRICT, snap.fetch_service_policy)

    def test_creation_git(self):
        # The metadata entries supplied when a Snap is created for a Git
        # branch are present on the new object.
        [ref] = self.factory.makeGitRefs()
        components = self.makeSnapComponents(git_ref=ref)
        snap = getUtility(ISnapSet).new(**components)
        self.assertEqual(components["registrant"], snap.registrant)
        self.assertEqual(components["owner"], snap.owner)
        self.assertEqual(components["distro_series"], snap.distro_series)
        self.assertEqual(components["name"], snap.name)
        self.assertIsNone(snap.branch)
        self.assertEqual(ref.repository, snap.git_repository)
        self.assertEqual(ref.path, snap.git_path)
        self.assertEqual(ref, snap.git_ref)
        self.assertFalse(snap.auto_build)
        self.assertIsNone(snap.auto_build_archive)
        self.assertIsNone(snap.auto_build_pocket)
        self.assertIsNone(snap.auto_build_channels)
        self.assertTrue(snap.require_virtualized)
        self.assertFalse(snap.private)
        self.assertTrue(snap.allow_internet)
        self.assertFalse(snap.build_source_tarball)
        self.assertFalse(snap.pro_enable)
        self.assertFalse(snap.use_fetch_service)
        self.assertEqual(FetchServicePolicy.STRICT, snap.fetch_service_policy)

    def test_creation_git_url(self):
        # A Snap can be backed directly by a URL for an external Git
        # repository, rather than a Git repository hosted in Launchpad.
        ref = self.factory.makeGitRefRemote()
        components = self.makeSnapComponents(git_ref=ref)
        snap = getUtility(ISnapSet).new(**components)
        self.assertIsNone(snap.branch)
        self.assertIsNone(snap.git_repository)
        self.assertEqual(ref.repository_url, snap.git_repository_url)
        self.assertEqual(ref.path, snap.git_path)
        self.assertEqual(ref, snap.git_ref)

    def test_non_admins_cannot_update_admin_only_fields(self):
        # The admin fields cannot be updated by a non-admin user

        non_admin = self.factory.makePerson()
        [ref] = self.factory.makeGitRefs(owner=non_admin)
        components = self.makeSnapComponents(git_ref=ref)
        snap = getUtility(ISnapSet).new(**components)

        admin_fields = {
            "allow_internet": True,
            "pro_enable": True,
            "require_virtualized": True,
        }

        for field_name, field_value in admin_fields.items():
            # exception is raised when user tries updating admin-only fields
            with person_logged_in(non_admin):
                self.assertRaises(
                    Unauthorized, setattr, snap, field_name, field_value
                )

    def test_admins_can_update_admin_only_fields(self):
        # The admin fields can be updated by an admin
        [ref] = self.factory.makeGitRefs()
        components = self.makeSnapComponents(git_ref=ref)
        snap = getUtility(ISnapSet).new(**components)

        admin_fields = {
            "allow_internet": True,
            "pro_enable": True,
            "require_virtualized": True,
        }

        for field_name, field_value in admin_fields.items():
            # exception isn't raised when an admin does the same
            with admin_logged_in():
                setattr(snap, field_name, field_value)
                self.assertEqual(field_value, getattr(snap, field_name))

    def test_create_private_snap_with_open_team_as_owner_fails(self):
        components = self.makeSnapComponents()
        with admin_logged_in():
            components["owner"].membership_policy = TeamMembershipPolicy.OPEN
            components["information_type"] = InformationType.PROPRIETARY
        self.assertRaises(
            SubscriptionPrivacyViolation,
            getUtility(ISnapSet).new,
            **components,
        )

    def test_private_snap_information_type_compatibility(self):
        login_admin()
        private = InformationType.PROPRIETARY
        public = InformationType.PUBLIC
        components = self.makeSnapComponents()
        components["owner"].membership_policy = TeamMembershipPolicy.MODERATED
        private_snap = getUtility(ISnapSet).new(
            information_type=private, **components
        )
        self.assertEqual(
            InformationType.PROPRIETARY, private_snap.information_type
        )

        public_snap = getUtility(ISnapSet).new(
            information_type=public, **self.makeSnapComponents()
        )
        self.assertEqual(InformationType.PUBLIC, public_snap.information_type)

    def test_private_snap_for_public_sources(self):
        # Creating private snaps for public sources is allowed.
        [ref] = self.factory.makeGitRefs()
        components = self.makeSnapComponents(git_ref=ref)
        with admin_logged_in():
            components["information_type"] = InformationType.PROPRIETARY
            components["owner"].membership_policy = (
                TeamMembershipPolicy.MODERATED
            )
        components["project"] = self.factory.makeProduct(
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        snap = getUtility(ISnapSet).new(**components)
        with person_logged_in(components["owner"]):
            self.assertTrue(snap.private)

    def test_private_git_requires_private_snap(self):
        # Snaps for a private Git branch cannot be public.
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            [git_ref] = self.factory.makeGitRefs(
                owner=owner, information_type=InformationType.PRIVATESECURITY
            )
            components = dict(
                registrant=owner,
                owner=owner,
                git_ref=git_ref,
                distro_series=self.factory.makeDistroSeries(),
                name=self.factory.getUniqueUnicode("snap-name"),
            )
            self.assertRaises(
                SnapPrivacyMismatch, getUtility(ISnapSet).new, **components
            )

    def test_private_bzr_requires_private_snap(self):
        # Snaps for a private Bzr branch cannot be public.
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            branch = self.factory.makeAnyBranch(
                owner=owner, information_type=InformationType.PRIVATESECURITY
            )
            components = dict(
                registrant=owner,
                owner=owner,
                branch=branch,
                distro_series=self.factory.makeDistroSeries(),
                name=self.factory.getUniqueUnicode("snap-name"),
            )
            self.assertRaises(
                SnapPrivacyMismatch, getUtility(ISnapSet).new, **components
            )

    def test_private_team_requires_private_snap(self):
        # Snaps owned by private teams cannot be public.
        registrant = self.factory.makePerson()
        with person_logged_in(registrant):
            private_team = self.factory.makeTeam(
                owner=registrant, visibility=PersonVisibility.PRIVATE
            )
            [git_ref] = self.factory.makeGitRefs()
            components = dict(
                registrant=registrant,
                owner=private_team,
                git_ref=git_ref,
                distro_series=self.factory.makeDistroSeries(),
                name=self.factory.getUniqueUnicode("snap-name"),
            )
            self.assertRaises(
                SnapPrivacyMismatch, getUtility(ISnapSet).new, **components
            )

    def test_creation_no_source(self):
        # Attempting to create a Snap with neither a Bazaar branch nor a Git
        # repository fails.
        registrant = self.factory.makePerson()
        self.assertRaises(
            NoSourceForSnap,
            getUtility(ISnapSet).new,
            registrant,
            registrant,
            self.factory.makeDistroSeries(),
            self.factory.getUniqueUnicode("snap-name"),
        )

    def test_exists(self):
        # ISnapSet.exists checks for matching Snaps.
        snap = self.factory.makeSnap()
        self.assertTrue(getUtility(ISnapSet).exists(snap.owner, snap.name))
        self.assertFalse(
            getUtility(ISnapSet).exists(self.factory.makePerson(), snap.name)
        )
        self.assertFalse(getUtility(ISnapSet).exists(snap.owner, "different"))

    def test_getByPillarAndName(self):
        owner = self.factory.makePerson()
        project = self.factory.makeProduct()
        project_snap = self.factory.makeSnap(
            name="proj-snap", owner=owner, registrant=owner, project=project
        )
        no_project_snap = self.factory.makeSnap(
            name="no-proj-snap", owner=owner, registrant=owner
        )

        snap_set = getUtility(ISnapSet)
        self.assertEqual(
            project_snap,
            snap_set.getByPillarAndName(owner, project, "proj-snap"),
        )
        self.assertEqual(
            no_project_snap,
            snap_set.getByPillarAndName(owner, None, "no-proj-snap"),
        )

    def test_findByOwner(self):
        # ISnapSet.findByOwner returns all Snaps with the given owner.
        owners = [self.factory.makePerson() for i in range(2)]
        snaps = []
        for owner in owners:
            for _ in range(2):
                snaps.append(
                    self.factory.makeSnap(registrant=owner, owner=owner)
                )
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(snaps[:2], snap_set.findByOwner(owners[0]))
        self.assertContentEqual(snaps[2:], snap_set.findByOwner(owners[1]))

    def test_findByPerson(self):
        # ISnapSet.findByPerson returns all Snaps with the given owner or
        # based on branches or repositories with the given owner.
        owners = [self.factory.makePerson() for i in range(2)]
        snaps = []
        for owner in owners:
            snaps.append(self.factory.makeSnap(registrant=owner, owner=owner))
            snaps.append(
                self.factory.makeSnap(
                    branch=self.factory.makeAnyBranch(owner=owner)
                )
            )
            [ref] = self.factory.makeGitRefs(owner=owner)
            snaps.append(self.factory.makeSnap(git_ref=ref))
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(snaps[:3], snap_set.findByPerson(owners[0]))
        self.assertContentEqual(snaps[3:], snap_set.findByPerson(owners[1]))

    def test_get_snap_privacy_filter_includes_grants(self):
        grantee, creator = (self.factory.makePerson() for i in range(2))
        # All snaps are owned by "creator", and "grantee" will later have
        # access granted using sharing service.
        snap_data = dict(registrant=creator, owner=creator, private=True)
        private_snaps = [self.factory.makeSnap(**snap_data) for _ in range(2)]
        shared_snaps = [self.factory.makeSnap(**snap_data) for _ in range(2)]
        snap_data["private"] = False
        public_snaps = [self.factory.makeSnap(**snap_data) for _ in range(3)]
        # Backwards compatibility check: NULL on information_type db column
        # should make us consider the "private" db column.
        snap = removeSecurityProxy(public_snaps[-1])
        snap._private = False
        snap.information_type = None
        Store.of(snap).flush()

        with admin_logged_in():
            for snap in shared_snaps:
                snap.subscribe(grantee, creator)

        def all_snaps_visible_by(person):
            return IStore(Snap).find(Snap, get_snap_privacy_filter(person))

        # Creator should get all snaps.
        self.assertContentEqual(
            public_snaps + private_snaps + shared_snaps,
            all_snaps_visible_by(creator),
        )

        # Grantee should get public and shared snaps.
        self.assertContentEqual(
            public_snaps + shared_snaps, all_snaps_visible_by(grantee)
        )

        with admin_logged_in():
            # After revoking, Grantee should have no access to the shared ones.
            for snap in shared_snaps:
                snap.unsubscribe(grantee, creator)
        self.assertContentEqual(public_snaps, all_snaps_visible_by(grantee))

    def test_findByProject(self):
        # ISnapSet.findByProject returns all Snaps based on branches or
        # repositories for the given project, and snaps associated directly
        # to the project.
        projects = [self.factory.makeProduct() for i in range(2)]
        snaps = []
        for project in projects:
            snaps.append(
                self.factory.makeSnap(
                    branch=self.factory.makeProductBranch(product=project)
                )
            )
            [ref] = self.factory.makeGitRefs(target=project)
            snaps.append(self.factory.makeSnap(git_ref=ref))
            snaps.append(self.factory.makeSnap(project=project))
        snaps.append(
            self.factory.makeSnap(branch=self.factory.makePersonalBranch())
        )
        [ref] = self.factory.makeGitRefs(target=None)
        snaps.append(self.factory.makeSnap(git_ref=ref))
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(snaps[:3], snap_set.findByProject(projects[0]))
        self.assertContentEqual(
            snaps[3:6], snap_set.findByProject(projects[1])
        )

    def test_findByBranch(self):
        # ISnapSet.findByBranch returns all Snaps with the given Bazaar branch.
        branches = [self.factory.makeAnyBranch() for i in range(2)]
        snaps = []
        for branch in branches:
            for _ in range(2):
                snaps.append(self.factory.makeSnap(branch=branch))
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(snaps[:2], snap_set.findByBranch(branches[0]))
        self.assertContentEqual(snaps[2:], snap_set.findByBranch(branches[1]))

    def test_findByGitRepository(self):
        # ISnapSet.findByGitRepository returns all Snaps with the given Git
        # repository.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        snaps = []
        for repository in repositories:
            for _ in range(2):
                [ref] = self.factory.makeGitRefs(repository=repository)
                snaps.append(self.factory.makeSnap(git_ref=ref))
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(
            snaps[:2], snap_set.findByGitRepository(repositories[0])
        )
        self.assertContentEqual(
            snaps[2:], snap_set.findByGitRepository(repositories[1])
        )

    def test_findByGitRepository_paths(self):
        # ISnapSet.findByGitRepository can restrict by reference paths.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        snaps = []
        for repository in repositories:
            for _ in range(3):
                [ref] = self.factory.makeGitRefs(repository=repository)
                snaps.append(self.factory.makeSnap(git_ref=ref))
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(
            [], snap_set.findByGitRepository(repositories[0], paths=[])
        )
        self.assertContentEqual(
            [snaps[0]],
            snap_set.findByGitRepository(
                repositories[0], paths=[snaps[0].git_ref.path]
            ),
        )
        self.assertContentEqual(
            snaps[:2],
            snap_set.findByGitRepository(
                repositories[0],
                paths=[snaps[0].git_ref.path, snaps[1].git_ref.path],
            ),
        )

    def test_findByGitRef(self):
        # ISnapSet.findByGitRef returns all Snaps with the given Git
        # reference.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        refs = []
        snaps = []
        for _ in repositories:
            refs.extend(
                self.factory.makeGitRefs(
                    paths=["refs/heads/master", "refs/heads/other"]
                )
            )
            snaps.append(self.factory.makeSnap(git_ref=refs[-2]))
            snaps.append(self.factory.makeSnap(git_ref=refs[-1]))
        snap_set = getUtility(ISnapSet)
        for ref, snap in zip(refs, snaps):
            self.assertContentEqual([snap], snap_set.findByGitRef(ref))

    def test_findByContext(self):
        # ISnapSet.findByContext returns all Snaps with the given context.
        person = self.factory.makePerson()
        project = self.factory.makeProduct()
        branch = self.factory.makeProductBranch(owner=person, product=project)
        other_branch = self.factory.makeProductBranch()
        repository = self.factory.makeGitRepository(target=project)
        refs = self.factory.makeGitRefs(
            repository=repository,
            paths=["refs/heads/master", "refs/heads/other"],
        )
        snaps = []
        snaps.append(self.factory.makeSnap(branch=branch))
        snaps.append(self.factory.makeSnap(branch=other_branch))
        snaps.append(
            self.factory.makeSnap(
                registrant=person, owner=person, git_ref=refs[0]
            )
        )
        snaps.append(self.factory.makeSnap(git_ref=refs[1]))
        snap_set = getUtility(ISnapSet)
        self.assertContentEqual(
            [snaps[0], snaps[2]], snap_set.findByContext(person)
        )
        self.assertContentEqual(
            [snaps[0], snaps[2], snaps[3]], snap_set.findByContext(project)
        )
        self.assertContentEqual([snaps[0]], snap_set.findByContext(branch))
        self.assertContentEqual(snaps[2:], snap_set.findByContext(repository))
        self.assertContentEqual([snaps[2]], snap_set.findByContext(refs[0]))
        self.assertRaises(
            BadSnapSearchContext,
            snap_set.findByContext,
            self.factory.makeDistribution(),
        )

    def test_findByURL(self):
        # ISnapSet.findByURL returns visible Snaps with the given URL.
        urls = ["https://git.example.org/foo", "https://git.example.org/bar"]
        owners = [self.factory.makePerson() for i in range(2)]
        snaps = []
        for url in urls:
            for owner in owners:
                snaps.append(
                    self.factory.makeSnap(
                        registrant=owner,
                        owner=owner,
                        git_ref=self.factory.makeGitRefRemote(
                            repository_url=url
                        ),
                    )
                )
        snaps.append(
            self.factory.makeSnap(branch=self.factory.makeAnyBranch())
        )
        snaps.append(
            self.factory.makeSnap(git_ref=self.factory.makeGitRefs()[0])
        )
        self.assertContentEqual(
            snaps[:2], getUtility(ISnapSet).findByURL(urls[0])
        )
        self.assertContentEqual(
            [snaps[0]],
            getUtility(ISnapSet).findByURL(urls[0], owner=owners[0]),
        )

    def test_findByURLPrefix(self):
        # ISnapSet.findByURLPrefix returns visible Snaps with the given URL
        # prefix.
        urls = [
            "https://git.example.org/foo/a",
            "https://git.example.org/foo/b",
            "https://git.example.org/bar",
        ]
        owners = [self.factory.makePerson() for i in range(2)]
        snaps = []
        for url in urls:
            for owner in owners:
                snaps.append(
                    self.factory.makeSnap(
                        registrant=owner,
                        owner=owner,
                        git_ref=self.factory.makeGitRefRemote(
                            repository_url=url
                        ),
                    )
                )
        snaps.append(
            self.factory.makeSnap(branch=self.factory.makeAnyBranch())
        )
        snaps.append(
            self.factory.makeSnap(git_ref=self.factory.makeGitRefs()[0])
        )
        prefix = "https://git.example.org/foo/"
        self.assertContentEqual(
            snaps[:4], getUtility(ISnapSet).findByURLPrefix(prefix)
        )
        self.assertContentEqual(
            [snaps[0], snaps[2]],
            getUtility(ISnapSet).findByURLPrefix(prefix, owner=owners[0]),
        )

    def test_findByURLPrefixes(self):
        # ISnapSet.findByURLPrefixes returns visible Snaps with any of the
        # given URL prefixes.
        urls = [
            "https://git.example.org/foo/a",
            "https://git.example.org/foo/b",
            "https://git.example.org/bar/a",
            "https://git.example.org/bar/b",
            "https://git.example.org/baz",
        ]
        owners = [self.factory.makePerson() for i in range(2)]
        snaps = []
        for url in urls:
            for owner in owners:
                snaps.append(
                    self.factory.makeSnap(
                        registrant=owner,
                        owner=owner,
                        git_ref=self.factory.makeGitRefRemote(
                            repository_url=url
                        ),
                    )
                )
        snaps.append(
            self.factory.makeSnap(branch=self.factory.makeAnyBranch())
        )
        snaps.append(
            self.factory.makeSnap(git_ref=self.factory.makeGitRefs()[0])
        )
        prefixes = [
            "https://git.example.org/foo/",
            "https://git.example.org/bar/",
        ]
        self.assertContentEqual(
            snaps[:8], getUtility(ISnapSet).findByURLPrefixes(prefixes)
        )
        self.assertContentEqual(
            [snaps[0], snaps[2], snaps[4], snaps[6]],
            getUtility(ISnapSet).findByURLPrefixes(prefixes, owner=owners[0]),
        )

    def test_findByStoreName(self):
        # ISnapSet.findByStoreName returns visible Snaps with the given
        # store name.
        store_names = ["foo", "bar"]
        owners = [self.factory.makePerson() for i in range(2)]
        snaps = []
        for store_name in store_names:
            for owner in owners:
                for private in (False, True):
                    snaps.append(
                        self.factory.makeSnap(
                            registrant=owner,
                            owner=owner,
                            private=private,
                            store_name=store_name,
                        )
                    )
        snaps.append(self.factory.makeSnap())
        self.assertContentEqual(
            [snaps[0], snaps[2]],
            getUtility(ISnapSet).findByStoreName(store_names[0]),
        )
        with person_logged_in(owners[0]):
            self.assertContentEqual(
                snaps[:2],
                getUtility(ISnapSet).findByStoreName(
                    store_names[0], owner=owners[0], visible_by_user=owners[0]
                ),
            )
            self.assertContentEqual(
                [snaps[2]],
                getUtility(ISnapSet).findByStoreName(
                    store_names[0], owner=owners[1], visible_by_user=owners[0]
                ),
            )

    def test_getSnapcraftYaml_snap_no_source(self):
        [git_ref] = self.factory.makeGitRefs()
        snap = self.factory.makeSnap(git_ref=git_ref)
        with admin_logged_in():
            git_ref.repository.destroySelf(break_references=True)
        self.assertRaisesWithContent(
            CannotFetchSnapcraftYaml,
            "Snap source is not defined",
            getUtility(ISnapSet).getSnapcraftYaml,
            snap,
        )

    def test_getSnapcraftYaml_bzr_snap_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == "snap/snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise BranchFileNotFound("dummy", filename)

        self.useFixture(
            BranchHostingFixture(blob=b"name: test-snap")
        ).getBlob = getBlob
        branch = self.factory.makeBranch()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(branch),
        )

    def test_getSnapcraftYaml_bzr_build_aux_snap_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == "build-aux/snap/snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise BranchFileNotFound("dummy", filename)

        self.useFixture(
            BranchHostingFixture(blob=b"name: test-snap")
        ).getBlob = getBlob
        branch = self.factory.makeBranch()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(branch),
        )

    def test_getSnapcraftYaml_bzr_plain_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == "snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise BranchFileNotFound("dummy", filename)

        self.useFixture(
            BranchHostingFixture(blob=b"name: test-snap")
        ).getBlob = getBlob
        branch = self.factory.makeBranch()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(branch),
        )

    def test_getSnapcraftYaml_bzr_dot_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == ".snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise BranchFileNotFound("dummy", filename)

        self.useFixture(
            BranchHostingFixture(blob=b"name: test-snap")
        ).getBlob = getBlob
        branch = self.factory.makeBranch()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(branch),
        )

    def test_getSnapcraftYaml_bzr_error(self):
        self.useFixture(BranchHostingFixture()).getBlob = FakeMethod(
            failure=BranchHostingFault
        )
        branch = self.factory.makeBranch()
        self.assertRaises(
            CannotFetchSnapcraftYaml,
            getUtility(ISnapSet).getSnapcraftYaml,
            branch,
        )

    def test_getSnapcraftYaml_git_snap_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == "snap/snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(git_ref),
        )

    def test_getSnapcraftYaml_git_build_aux_snap_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == "build-aux/snap/snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(git_ref),
        )

    def test_getSnapcraftYaml_git_plain_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == "snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(git_ref),
        )

    def test_getSnapcraftYaml_git_dot_snapcraft_yaml(self):
        def getBlob(path, filename, *args, **kwargs):
            if filename == ".snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        self.assertEqual(
            {"name": "test-snap"},
            getUtility(ISnapSet).getSnapcraftYaml(git_ref),
        )

    def test_getSnapcraftYaml_git_error(self):
        self.useFixture(GitHostingFixture()).getBlob = FakeMethod(
            failure=GitRepositoryScanFault
        )
        [git_ref] = self.factory.makeGitRefs()
        self.assertRaises(
            CannotFetchSnapcraftYaml,
            getUtility(ISnapSet).getSnapcraftYaml,
            git_ref,
        )

    def test_getSnapcraftYaml_snap_bzr(self):
        self.useFixture(
            BranchHostingFixture(
                blob=b"name: test-snap",
            )
        )
        branch = self.factory.makeBranch()
        snap = self.factory.makeSnap(branch=branch)
        self.assertEqual(
            {"name": "test-snap"}, getUtility(ISnapSet).getSnapcraftYaml(snap)
        )

    def test_getSnapcraftYaml_snap_git(self):
        self.useFixture(GitHostingFixture(blob=b"name: test-snap"))
        [git_ref] = self.factory.makeGitRefs()
        snap = self.factory.makeSnap(git_ref=git_ref)
        self.assertEqual(
            {"name": "test-snap"}, getUtility(ISnapSet).getSnapcraftYaml(snap)
        )

    @responses.activate
    def test_getSnapcraftYaml_snap_git_external_github(self):
        responses.add(
            "GET",
            "https://raw.githubusercontent.com/owner/name/HEAD/"
            "snap/snapcraft.yaml",
            body=b"name: test-snap",
        )
        git_ref = self.factory.makeGitRefRemote(
            repository_url="https://github.com/owner/name", path="HEAD"
        )
        snap = self.factory.makeSnap(git_ref=git_ref)
        with default_timeout(1.0):
            self.assertEqual(
                {"name": "test-snap"},
                getUtility(ISnapSet).getSnapcraftYaml(snap),
            )

    def test_getSnapcraftYaml_invalid_data(self):
        hosting_fixture = self.useFixture(GitHostingFixture())
        for invalid_result in (None, 123, b"", b"[][]", b"#name:test", b"]"):
            [git_ref] = self.factory.makeGitRefs()
            hosting_fixture.getBlob = FakeMethod(result=invalid_result)
            self.assertRaises(
                CannotParseSnapcraftYaml,
                getUtility(ISnapSet).getSnapcraftYaml,
                git_ref,
            )

    def test_getSnapcraftYaml_safe_yaml(self):
        self.useFixture(GitHostingFixture(blob=b"Malicious YAML!"))
        [git_ref] = self.factory.makeGitRefs()
        unsafe_load = self.useFixture(MockPatch("yaml.load"))
        safe_load = self.useFixture(MockPatch("yaml.safe_load"))
        self.assertRaises(
            CannotParseSnapcraftYaml,
            getUtility(ISnapSet).getSnapcraftYaml,
            git_ref,
        )
        self.assertEqual(0, unsafe_load.mock.call_count)
        self.assertEqual(1, safe_load.mock.call_count)

    @responses.activate
    def test_getSnapcraftYaml_symlink(self):
        for path in ("snap/snapcraft.yaml", "build-aux/snap/snapcraft.yaml"):
            responses.add(
                "GET",
                "https://raw.githubusercontent.com/owner/name/HEAD/%s" % path,
                status=404,
            )
        responses.add(
            "GET",
            "https://raw.githubusercontent.com/owner/name/HEAD/snapcraft.yaml",
            body=b"pkg/snap/snapcraft.yaml",
        )
        responses.add(
            "GET",
            "https://raw.githubusercontent.com/owner/name/HEAD/"
            "pkg/snap/snapcraft.yaml",
            body=b"name: test-snap",
        )
        git_ref = self.factory.makeGitRefRemote(
            repository_url="https://github.com/owner/name", path="HEAD"
        )
        snap = self.factory.makeSnap(git_ref=git_ref)
        with default_timeout(1.0):
            self.assertEqual(
                {"name": "test-snap"},
                getUtility(ISnapSet).getSnapcraftYaml(snap),
            )

    @responses.activate
    def test_getSnapcraftYaml_symlink_via_parent(self):
        responses.add(
            "GET",
            "https://raw.githubusercontent.com/owner/name/HEAD/"
            "snap/snapcraft.yaml",
            body=b"../pkg/snap/snapcraft.yaml",
        )
        responses.add(
            "GET",
            "https://raw.githubusercontent.com/owner/name/HEAD/"
            "pkg/snap/snapcraft.yaml",
            body=b"name: test-snap",
        )
        git_ref = self.factory.makeGitRefRemote(
            repository_url="https://github.com/owner/name", path="HEAD"
        )
        snap = self.factory.makeSnap(git_ref=git_ref)
        with default_timeout(1.0):
            self.assertEqual(
                {"name": "test-snap"},
                getUtility(ISnapSet).getSnapcraftYaml(snap),
            )

    def test_getSnapcraftYaml_snap_build_path(self):
        # When a snap has build_path set, getSnapcraftYaml looks for
        # snapcraft.yaml at {build_path}/snapcraft.yaml.
        def getBlob(path, filename, *args, **kwargs):
            if filename == "mydir/snapcraft.yaml":
                return b"name: test-snap"
            else:
                raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        snap = self.factory.makeSnap(git_ref=git_ref, build_path="mydir")
        self.assertEqual(
            {"name": "test-snap"}, getUtility(ISnapSet).getSnapcraftYaml(snap)
        )

    def test_getSnapcraftYaml_snap_build_path_missing(self):
        # When a snap has build_path set but there is no snapcraft.yaml at
        # that location, MissingSnapcraftYaml is raised.
        def getBlob(path, filename, *args, **kwargs):
            raise GitRepositoryBlobNotFound("dummy", filename)

        self.useFixture(GitHostingFixture()).getBlob = getBlob
        [git_ref] = self.factory.makeGitRefs()
        snap = self.factory.makeSnap(git_ref=git_ref, build_path="mydir")
        self.assertRaises(
            MissingSnapcraftYaml,
            getUtility(ISnapSet).getSnapcraftYaml,
            snap,
        )

    @responses.activate
    def test_getSnapcraftYaml_symlink_above_root(self):
        responses.add(
            "GET",
            "https://raw.githubusercontent.com/owner/name/HEAD/snapcraft.yaml",
            body=b"../pkg/snap/snapcraft.yaml",
        )
        git_ref = self.factory.makeGitRefRemote(
            repository_url="https://github.com/owner/name", path="HEAD"
        )
        snap = self.factory.makeSnap(git_ref=git_ref)
        with default_timeout(1.0):
            self.assertRaises(
                CannotFetchSnapcraftYaml,
                getUtility(ISnapSet).getSnapcraftYaml,
                snap,
            )

    def test_getSnapcraftYaml_emoji(self):
        self.useFixture(GitHostingFixture(blob="summary: \U0001f680\n"))
        [git_ref] = self.factory.makeGitRefs()
        self.assertEqual(
            {"summary": "\U0001f680"},
            getUtility(ISnapSet).getSnapcraftYaml(git_ref),
        )

    def test__findStaleSnaps(self):
        # Stale; not built automatically.
        self.factory.makeSnap(is_stale=True)
        # Not stale; built automatically.
        self.factory.makeSnap(auto_build=True, is_stale=False)
        # Stale; built automatically.
        stale_daily = self.factory.makeSnap(auto_build=True, is_stale=True)
        self.assertContentEqual([stale_daily], SnapSet._findStaleSnaps())

    def test__findStaleSnapsDistinct(self):
        # If a snap package has two builds due to two architectures, it only
        # returns one recipe.
        distroseries = self.factory.makeDistroSeries()
        dases = [
            self.factory.makeDistroArchSeries(distroseries=distroseries)
            for _ in range(2)
        ]
        snap = self.factory.makeSnap(
            distroseries=distroseries,
            processors=[das.processor for das in dases],
            auto_build=True,
            is_stale=True,
        )
        for das in dases:
            self.factory.makeSnapBuild(
                requester=snap.owner,
                snap=snap,
                archive=snap.auto_build_archive,
                distroarchseries=das,
                pocket=snap.auto_build_pocket,
                date_created=(datetime.now(timezone.utc) - timedelta(days=2)),
            )
        self.assertContentEqual([snap], SnapSet._findStaleSnaps())

    def makeBuildableDistroArchSeries(self, **kwargs):
        das = self.factory.makeDistroArchSeries(**kwargs)
        fake_chroot = self.factory.makeLibraryFileAlias(
            filename="fake_chroot.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_chroot)
        return das

    def makeAutoBuildableSnap(self, **kwargs):
        processor = self.factory.makeProcessor(supports_virtualized=True)
        das = self.makeBuildableDistroArchSeries(processor=processor)
        [git_ref] = self.factory.makeGitRefs()
        snap = self.factory.makeSnap(
            distroseries=das.distroseries,
            processors=[das.processor],
            git_ref=git_ref,
            auto_build=True,
            **kwargs,
        )
        return das, snap

    def test_makeAutoBuilds(self):
        # ISnapSet.makeAutoBuilds requests builds of
        # appropriately-configured Snaps where possible.
        self.assertEqual([], getUtility(ISnapSet).makeAutoBuilds())
        _, snap = self.makeAutoBuildableSnap(is_stale=True)
        logger = BufferLogger()
        [build_request] = getUtility(ISnapSet).makeAutoBuilds(logger=logger)
        self.assertThat(
            build_request,
            MatchesStructure(
                snap=Equals(snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                requester=Equals(snap.owner),
                archive=Equals(snap.auto_build_archive),
                pocket=Equals(snap.auto_build_pocket),
                channels=Is(None),
            ),
        )
        expected_log_entries = [
            "DEBUG Scheduling builds of snap package %s/%s"
            % (snap.owner.name, snap.name),
        ]
        self.assertEqual(
            expected_log_entries, logger.getLogBuffer().splitlines()
        )
        self.assertFalse(snap.is_stale)

    def test_makeAutoBuilds_skips_when_owner_mismatches(self):
        # ISnapSet.makeAutoBuilds skips snap packages if
        # snap.owner != snap.archive.owner
        _, snap = self.makeAutoBuildableSnap(
            is_stale=True,
        )
        # we need to create a snap with a private archive,
        # and a different snap.owner and snap.auto_build_archive.owner
        # to trigger the `SnapBuildArchiveOwnerMismatch` exception
        # which finally skips the build
        snap = removeSecurityProxy(snap)
        snap.owner = self.factory.makePerson(name="other")
        snap.auto_build_archive.private = True
        logger = BufferLogger()
        build_requests = getUtility(ISnapSet).makeAutoBuilds(logger=logger)
        self.assertEqual([], build_requests)
        self.assertEqual(
            [
                "DEBUG Scheduling builds of snap package %s/%s"
                % (snap.owner.name, snap.name),
                "ERROR Snap package builds against private archives are only "
                "allowed if the snap package owner and the archive owner are "
                "equal. Snap owner: Other, Archive owner: %s"
                % (snap.auto_build_archive.owner.displayname,),
            ],
            logger.getLogBuffer().splitlines(),
        )

    def test_makeAutoBuilds_skips_for_other_exceptions(self):
        # scheduling builds need to be unaffected by one erroring
        _, snap = self.makeAutoBuildableSnap(
            is_stale=True,
        )
        # builds cannot be scheduled when the archive is disabled
        snap = removeSecurityProxy(snap)
        snap.auto_build_archive._enabled = False
        logger = BufferLogger()
        build_requests = getUtility(ISnapSet).makeAutoBuilds(logger=logger)
        self.assertEqual([], build_requests)
        self.assertEqual(
            [
                "DEBUG Scheduling builds of snap package %s/%s"
                % (snap.owner.name, snap.name),
                "ERROR %s is disabled."
                % (snap.auto_build_archive.displayname,),
            ],
            logger.getLogBuffer().splitlines(),
        )

    def test_makeAutoBuilds_skips_and_no_logger_enabled(self):
        # This is basically the same test case as
        # `test_makeAutoBuilds_skips_when_if_owner_mismatches`
        # but we particularly test with no logger enabled.
        _, snap = self.makeAutoBuildableSnap(
            is_stale=True,
        )
        snap = removeSecurityProxy(snap)
        snap.owner = self.factory.makePerson(name="other")
        snap.auto_build_archive.private = True
        build_requests = getUtility(ISnapSet).makeAutoBuilds()
        self.assertEqual([], build_requests)

    def test_makeAutoBuilds_skips_if_built_recently(self):
        # ISnapSet.makeAutoBuilds skips snap packages that have been built
        # recently.
        das, snap = self.makeAutoBuildableSnap(is_stale=True)
        self.factory.makeSnapBuild(
            requester=snap.owner,
            snap=snap,
            archive=snap.auto_build_archive,
            distroarchseries=das,
        )
        logger = BufferLogger()
        build_requests = getUtility(ISnapSet).makeAutoBuilds(logger=logger)
        self.assertEqual([], build_requests)
        self.assertEqual([], logger.getLogBuffer().splitlines())

    def test_makeAutoBuilds_skips_if_built_recently_matching_channels(self):
        # ISnapSet.makeAutoBuilds only considers recently-requested builds
        # to match a snap if they match its auto_build_channels.
        das1, snap1 = self.makeAutoBuildableSnap(is_stale=True)
        das2, snap2 = self.makeAutoBuildableSnap(
            is_stale=True,
            auto_build_channels={"snapcraft": "edge", "snapd": "edge"},
        )
        # Create some builds with mismatched channels.
        self.factory.makeSnapBuild(
            requester=snap1.owner,
            snap=snap1,
            archive=snap1.auto_build_archive,
            distroarchseries=das1,
            channels={"snapcraft": "edge", "snapd": "edge"},
        )
        self.factory.makeSnapBuild(
            requester=snap2.owner,
            snap=snap2,
            archive=snap2.auto_build_archive,
            distroarchseries=das2,
            channels={"snapcraft": "stable"},
        )

        logger = BufferLogger()
        build_requests = getUtility(ISnapSet).makeAutoBuilds(logger=logger)
        self.assertThat(
            build_requests,
            MatchesSetwise(
                MatchesStructure(
                    snap=Equals(snap1),
                    status=Equals(SnapBuildRequestStatus.PENDING),
                    requester=Equals(snap1.owner),
                    archive=Equals(snap1.auto_build_archive),
                    pocket=Equals(snap1.auto_build_pocket),
                    channels=Is(None),
                ),
                MatchesStructure.byEquality(
                    snap=snap2,
                    status=SnapBuildRequestStatus.PENDING,
                    requester=snap2.owner,
                    archive=snap2.auto_build_archive,
                    pocket=snap2.auto_build_pocket,
                    channels={"snapcraft": "edge", "snapd": "edge"},
                ),
            ),
        )
        log_entries = logger.getLogBuffer().splitlines()
        self.assertEqual(2, len(log_entries))
        for snap in snap1, snap2:
            self.assertIn(
                "DEBUG Scheduling builds of snap package %s/%s"
                % (snap.owner.name, snap.name),
                log_entries,
            )
            self.assertFalse(snap.is_stale)

        # Run the build request jobs, mark the two snaps stale, and try again.
        # There are now matching builds so we don't try to request more.
        jobs = [
            removeSecurityProxy(build_request)._job
            for build_request in build_requests
        ]
        snapcraft_yaml = (
            dedent(
                """\
            architectures:
              - build-on: %s
              - build-on: %s
            """
            )
            % (das1.architecturetag, das2.architecturetag)
        )
        with GitHostingFixture(blob=snapcraft_yaml):
            with dbuser(config.ISnapRequestBuildsJobSource.dbuser):
                JobRunner(jobs).runAll()
        for snap in snap1, snap2:
            removeSecurityProxy(snap).is_stale = True
            IStore(snap).flush()
        logger = BufferLogger()
        build_requests = getUtility(ISnapSet).makeAutoBuilds(logger=logger)
        self.assertEqual([], build_requests)
        self.assertEqual([], logger.getLogBuffer().splitlines())

    def test_makeAutoBuilds_skips_non_stale_snaps(self):
        # ISnapSet.makeAutoBuilds skips snap packages that are not stale.
        _, snap = self.makeAutoBuildableSnap(is_stale=False)
        self.assertEqual([], getUtility(ISnapSet).makeAutoBuilds())

    def test_makeAutoBuilds_with_older_build(self):
        # If a previous build is not recent and the snap package is stale,
        # ISnapSet.makeAutoBuilds requests builds.
        das, snap = self.makeAutoBuildableSnap(is_stale=True)
        self.factory.makeSnapBuild(
            requester=snap.owner,
            snap=snap,
            archive=snap.auto_build_archive,
            distroarchseries=das,
            date_created=datetime.now(timezone.utc) - timedelta(days=1),
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=1),
        )
        build_requests = getUtility(ISnapSet).makeAutoBuilds()
        self.assertEqual(1, len(build_requests))

    def test_makeAutoBuilds_with_older_and_newer_builds(self):
        # If a snap package has been built twice, and the most recent build
        # is too recent, ISnapSet.makeAutoBuilds does not request builds.
        das, snap = self.makeAutoBuildableSnap(is_stale=True)
        for timediff in timedelta(days=1), timedelta(minutes=30):
            self.factory.makeSnapBuild(
                requester=snap.owner,
                snap=snap,
                archive=snap.auto_build_archive,
                distroarchseries=das,
                date_created=datetime.now(timezone.utc) - timediff,
                status=BuildStatus.FULLYBUILT,
                duration=timedelta(minutes=1),
            )
        self.assertEqual([], getUtility(ISnapSet).makeAutoBuilds())

    def test_makeAutoBuilds_with_recent_build_from_different_archive(self):
        # If a snap package has been built recently but from an archive
        # other than the auto_build_archive, ISnapSet.makeAutoBuilds
        # requests builds.
        das, snap = self.makeAutoBuildableSnap(is_stale=True)
        self.factory.makeSnapBuild(
            requester=snap.owner,
            snap=snap,
            distroarchseries=das,
            date_created=datetime.now(timezone.utc) - timedelta(minutes=30),
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=1),
        )
        build_requests = getUtility(ISnapSet).makeAutoBuilds()
        self.assertEqual(1, len(build_requests))

    def test_detachFromBranch(self):
        # ISnapSet.detachFromBranch clears the given Bazaar branch from all
        # Snaps.
        branches = [self.factory.makeAnyBranch() for i in range(2)]
        snaps = []
        for branch in branches:
            for _ in range(2):
                snaps.append(
                    self.factory.makeSnap(
                        branch=branch, date_created=ONE_DAY_AGO
                    )
                )
        getUtility(ISnapSet).detachFromBranch(branches[0])
        self.assertEqual(
            [None, None, branches[1], branches[1]],
            [snap.branch for snap in snaps],
        )
        for snap in snaps[:2]:
            self.assertSqlAttributeEqualsDate(
                snap, "date_last_modified", UTC_NOW
            )

    def test_detachFromGitRepository(self):
        # ISnapSet.detachFromGitRepository clears the given Git repository
        # from all Snaps.
        repositories = [self.factory.makeGitRepository() for i in range(2)]
        snaps = []
        paths = []
        refs = []
        for repository in repositories:
            for _ in range(2):
                [ref] = self.factory.makeGitRefs(repository=repository)
                paths.append(ref.path)
                refs.append(ref)
                snaps.append(
                    self.factory.makeSnap(
                        git_ref=ref, date_created=ONE_DAY_AGO
                    )
                )
        getUtility(ISnapSet).detachFromGitRepository(repositories[0])
        self.assertEqual(
            [None, None, repositories[1], repositories[1]],
            [snap.git_repository for snap in snaps],
        )
        self.assertEqual(
            [None, None, paths[2], paths[3]], [snap.git_path for snap in snaps]
        )
        self.assertEqual(
            [None, None, refs[2], refs[3]], [snap.git_ref for snap in snaps]
        )
        for snap in snaps[:2]:
            self.assertSqlAttributeEqualsDate(
                snap, "date_last_modified", UTC_NOW
            )


class TestSnapProcessors(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp(user="foo.bar@canonical.com")
        self.default_procs = [
            getUtility(IProcessorSet).getByName("386"),
            getUtility(IProcessorSet).getByName("amd64"),
        ]
        self.unrestricted_procs = self.default_procs + [
            getUtility(IProcessorSet).getByName("hppa")
        ]
        self.arm = self.factory.makeProcessor(
            name="arm", restricted=True, build_by_default=False
        )

    def test_available_processors_with_distro_series(self):
        # If the snap has a distroseries, only those processors that are
        # enabled for that series are available.
        distroseries = self.factory.makeDistroSeries()
        for processor in self.default_procs:
            self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        self.factory.makeDistroArchSeries(
            architecturetag=self.arm.name, processor=self.arm
        )
        snap = self.factory.makeSnap(distroseries=distroseries)
        self.assertContentEqual(self.default_procs, snap.available_processors)

    def test_available_processors_without_distro_series(self):
        # If the snap does not have a distroseries, then processors that are
        # enabled for any active series are available.
        snap = self.factory.makeSnap(distroseries=None)
        # 386 and hppa have corresponding DASes in sampledata for active
        # distroseries.
        self.assertContentEqual(
            ["386", "hppa"],
            [processor.name for processor in snap.available_processors],
        )

    def test_new_default_processors(self):
        # SnapSet.new creates a SnapArch for each available Processor with
        # build_by_default set.
        new_procs = [
            self.factory.makeProcessor(name="default", build_by_default=True),
            self.factory.makeProcessor(
                name="nondefault", build_by_default=False
            ),
        ]
        owner = self.factory.makePerson()
        distroseries = self.factory.makeDistroSeries()
        for processor in self.unrestricted_procs + [self.arm] + new_procs:
            self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        snap = getUtility(ISnapSet).new(
            registrant=owner,
            owner=owner,
            distro_series=distroseries,
            name="snap",
            branch=self.factory.makeAnyBranch(),
        )
        self.assertContentEqual(
            ["386", "amd64", "hppa", "default"],
            [processor.name for processor in snap.processors],
        )

    def test_new_override_processors(self):
        # SnapSet.new can be given a custom set of processors.
        owner = self.factory.makePerson()
        snap = getUtility(ISnapSet).new(
            registrant=owner,
            owner=owner,
            distro_series=self.factory.makeDistroSeries(),
            name="snap",
            branch=self.factory.makeAnyBranch(),
            processors=[self.arm],
        )
        self.assertContentEqual(
            ["arm"], [processor.name for processor in snap.processors]
        )

    def test_set(self):
        # The property remembers its value correctly.
        snap = self.factory.makeSnap()
        snap.setProcessors([self.arm])
        self.assertContentEqual([self.arm], snap.processors)
        snap.setProcessors(self.unrestricted_procs + [self.arm])
        self.assertContentEqual(
            self.unrestricted_procs + [self.arm], snap.processors
        )
        snap.setProcessors([])
        self.assertContentEqual([], snap.processors)

    def test_set_non_admin(self):
        """Non-admins can only enable or disable unrestricted processors."""
        snap = self.factory.makeSnap()
        snap.setProcessors(self.default_procs)
        self.assertContentEqual(self.default_procs, snap.processors)
        with person_logged_in(snap.owner) as owner:
            # Adding arm is forbidden ...
            self.assertRaises(
                CannotModifySnapProcessor,
                snap.setProcessors,
                [self.default_procs[0], self.arm],
                check_permissions=True,
                user=owner,
            )
            # ... but removing amd64 is OK.
            snap.setProcessors(
                [self.default_procs[0]], check_permissions=True, user=owner
            )
            self.assertContentEqual([self.default_procs[0]], snap.processors)
        with admin_logged_in() as admin:
            snap.setProcessors(
                [self.default_procs[0], self.arm],
                check_permissions=True,
                user=admin,
            )
            self.assertContentEqual(
                [self.default_procs[0], self.arm], snap.processors
            )
        with person_logged_in(snap.owner) as owner:
            hppa = getUtility(IProcessorSet).getByName("hppa")
            self.assertFalse(hppa.restricted)
            # Adding hppa while removing arm is forbidden ...
            self.assertRaises(
                CannotModifySnapProcessor,
                snap.setProcessors,
                [self.default_procs[0], hppa],
                check_permissions=True,
                user=owner,
            )
            # ... but adding hppa while retaining arm is OK.
            snap.setProcessors(
                [self.default_procs[0], self.arm, hppa],
                check_permissions=True,
                user=owner,
            )
            self.assertContentEqual(
                [self.default_procs[0], self.arm, hppa], snap.processors
            )

    def test_pro_enabled_default_value_for_new_snap(self):
        """Snap pro_enable value defaults to False when creating a new Snap."""

        git_ref = self.factory.makeGitRefs()[0]
        blob = b"name: test-snap\nbase: core18\n"
        self.useFixture(GitHostingFixture()).getBlob = (
            lambda path, *args, **kwargs: blob
        )

        registrant = self.factory.makePerson()
        components = dict(
            registrant=registrant,
            owner=self.factory.makeTeam(owner=registrant),
            distro_series=self.factory.makeDistroSeries(),
            name=self.factory.getUniqueUnicode("snap-name"),
            git_ref=git_ref,
        )

        snap = getUtility(ISnapSet).new(**components)
        self.assertFalse(snap.pro_enable)


class TestSnapWebservice(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.snap_store_client = FakeMethod()
        self.snap_store_client.requestPackageUploadPermission = getUtility(
            ISnapStoreClient
        ).requestPackageUploadPermission
        self.useFixture(
            ZopeUtilityFixture(self.snap_store_client, ISnapStoreClient)
        )
        self.person = self.factory.makePerson(displayname="Test Person")
        self.webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PUBLIC
        )
        self.webservice.default_api_version = "devel"
        login(ANONYMOUS)

    def getURL(self, obj):
        return self.webservice.getAbsoluteUrl(api_url(obj))

    def makeSnap(
        self,
        owner=None,
        distroseries=None,
        branch=None,
        git_ref=None,
        processors=None,
        webservice=None,
        private=False,
        auto_build_archive=None,
        auto_build_pocket=None,
        **kwargs,
    ):
        if owner is None:
            owner = self.person
        if distroseries is None:
            distroseries = self.factory.makeDistroSeries(registrant=owner)
        if branch is None and git_ref is None:
            branch = self.factory.makeAnyBranch()
        if webservice is None:
            webservice = self.webservice
        transaction.commit()
        distroseries_url = api_url(distroseries)
        owner_url = api_url(owner)
        if branch is not None:
            kwargs["branch"] = api_url(branch)
        if git_ref is not None:
            kwargs["git_ref"] = api_url(git_ref)
        if processors is not None:
            kwargs["processors"] = [
                api_url(processor) for processor in processors
            ]
        if auto_build_archive is not None:
            kwargs["auto_build_archive"] = api_url(auto_build_archive)
        if auto_build_pocket is not None:
            kwargs["auto_build_pocket"] = auto_build_pocket.title
        logout()
        information_type = (
            InformationType.PROPRIETARY if private else InformationType.PUBLIC
        )
        response = webservice.named_post(
            "/+snaps",
            "new",
            owner=owner_url,
            distro_series=distroseries_url,
            name="mir",
            information_type=information_type.title,
            **kwargs,
        )
        self.assertEqual(201, response.status)
        return webservice.get(response.getHeader("Location")).jsonBody()

    def getCollectionLinks(self, entry, member):
        """Return a list of self_link attributes of entries in a collection."""
        collection = self.webservice.get(
            entry["%s_collection_link" % member]
        ).jsonBody()
        return [entry["self_link"] for entry in collection["entries"]]

    def test_new_bzr(self):
        # Ensure Snap creation based on a Bazaar branch works.
        team = self.factory.makeTeam(owner=self.person)
        distroseries = self.factory.makeDistroSeries(registrant=team)
        branch = self.factory.makeAnyBranch()
        snap = self.makeSnap(
            owner=team, distroseries=distroseries, branch=branch
        )
        with person_logged_in(self.person):
            self.assertEqual(self.getURL(self.person), snap["registrant_link"])
            self.assertEqual(self.getURL(team), snap["owner_link"])
            self.assertEqual(
                self.getURL(distroseries), snap["distro_series_link"]
            )
            self.assertEqual("mir", snap["name"])
            self.assertEqual(self.getURL(branch), snap["branch_link"])
            self.assertIsNone(snap["git_repository_link"])
            self.assertIsNone(snap["git_path"])
            self.assertIsNone(snap["git_ref_link"])
            self.assertTrue(snap["require_virtualized"])
            self.assertTrue(snap["allow_internet"])
            self.assertFalse(snap["build_source_tarball"])
            self.assertFalse(snap["pro_enable"])

    def test_new_git(self):
        # Ensure Snap creation based on a Git branch works.
        team = self.factory.makeTeam(owner=self.person)
        distroseries = self.factory.makeDistroSeries(registrant=team)
        [ref] = self.factory.makeGitRefs()
        snap = self.makeSnap(
            owner=team, distroseries=distroseries, git_ref=ref
        )
        with person_logged_in(self.person):
            self.assertEqual(self.getURL(self.person), snap["registrant_link"])
            self.assertEqual(self.getURL(team), snap["owner_link"])
            self.assertEqual(
                self.getURL(distroseries), snap["distro_series_link"]
            )
            self.assertEqual("mir", snap["name"])
            self.assertIsNone(snap["branch_link"])
            self.assertEqual(
                self.getURL(ref.repository), snap["git_repository_link"]
            )
            self.assertEqual(ref.path, snap["git_path"])
            self.assertEqual(self.getURL(ref), snap["git_ref_link"])
            self.assertTrue(snap["require_virtualized"])
            self.assertTrue(snap["allow_internet"])
            self.assertFalse(snap["build_source_tarball"])
            self.assertFalse(snap["pro_enable"])

    def test_new_private(self):
        # Ensure private Snap creation works.
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED, owner=self.person
        )
        distroseries = self.factory.makeDistroSeries(registrant=team)
        [ref] = self.factory.makeGitRefs()
        private_webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PRIVATE
        )
        private_webservice.default_api_version = "devel"
        login(ANONYMOUS)
        snap = self.makeSnap(
            owner=team,
            distroseries=distroseries,
            git_ref=ref,
            webservice=private_webservice,
            private=True,
        )
        with person_logged_in(self.person):
            self.assertTrue(snap["private"])

    def test_new_store_options(self):
        # Ensure store-related options in Snap.new work.
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        store_name = self.factory.getUniqueUnicode()
        snap = self.makeSnap(
            store_upload=True,
            store_series=api_url(snappy_series),
            store_name=store_name,
            store_channels=["edge"],
        )
        with person_logged_in(self.person):
            self.assertTrue(snap["store_upload"])
            self.assertEqual(
                self.getURL(snappy_series), snap["store_series_link"]
            )
            self.assertEqual(store_name, snap["store_name"])
            self.assertEqual(["edge"], snap["store_channels"])

    def test_duplicate(self):
        # An attempt to create a duplicate Snap fails.
        team = self.factory.makeTeam(owner=self.person)
        branch = self.factory.makeAnyBranch()
        branch_url = api_url(branch)
        self.makeSnap(owner=team)
        with person_logged_in(self.person):
            owner_url = api_url(team)
            distroseries_url = api_url(self.factory.makeDistroSeries())
        response = self.webservice.named_post(
            "/+snaps",
            "new",
            owner=owner_url,
            distro_series=distroseries_url,
            name="mir",
            branch=branch_url,
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"There is already a snap package with the same name and owner.",
            response.body,
        )

    def test_not_owner(self):
        # If the registrant is not the owner or a member of the owner team,
        # Snap creation fails.
        other_person = self.factory.makePerson(displayname="Other Person")
        other_team = self.factory.makeTeam(
            owner=other_person, displayname="Other Team"
        )
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        branch = self.factory.makeAnyBranch()
        transaction.commit()
        other_person_url = api_url(other_person)
        other_team_url = api_url(other_team)
        distroseries_url = api_url(distroseries)
        branch_url = api_url(branch)
        logout()
        response = self.webservice.named_post(
            "/+snaps",
            "new",
            owner=other_person_url,
            distro_series=distroseries_url,
            name="dummy",
            branch=branch_url,
        )
        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Test Person cannot create snap packages owned by Other Person.",
            response.body,
        )
        response = self.webservice.named_post(
            "/+snaps",
            "new",
            owner=other_team_url,
            distro_series=distroseries_url,
            name="dummy",
            branch=branch_url,
        )
        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Test Person is not a member of Other Team.", response.body
        )

    def test_cannot_make_snap_with_private_components_public(self):
        # If a Snap has private components, then trying to make it public
        # fails.
        branch = self.factory.makeAnyBranch(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        project = self.factory.makeProduct(
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            branch=branch,
            project=project,
            information_type=InformationType.PROPRIETARY,
        )
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner
        with person_logged_in(self.person):
            snap_url = api_url(snap)
        logout()
        admin_webservice = webservice_for_person(
            admin, permission=OAuthPermission.WRITE_PRIVATE
        )
        admin_webservice.default_api_version = "devel"
        data = json.dumps({"information_type": "Public"})
        content_type = "application/json"
        response = admin_webservice.patch(snap_url, content_type, data)
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"Snap recipe contains private information and cannot be public.",
            response.body,
        )

    def test_cannot_set_private_components_of_public_snap(self):
        # If a Snap is public, then trying to change any of its owner,
        # branch, or git_repository components to be private fails.
        bzr_snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            branch=self.factory.makeAnyBranch(),
        )
        git_snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            git_ref=self.factory.makeGitRefs()[0],
        )
        private_team = self.factory.makeTeam(
            owner=self.person, visibility=PersonVisibility.PRIVATE
        )
        private_branch = self.factory.makeAnyBranch(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        [private_ref] = self.factory.makeGitRefs(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        bzr_snap_url = api_url(bzr_snap)
        git_snap_url = api_url(git_snap)
        with person_logged_in(self.person):
            private_team_url = api_url(private_team)
            private_branch_url = api_url(private_branch)
            private_ref_url = api_url(private_ref)
        logout()
        private_webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PRIVATE
        )
        private_webservice.default_api_version = "devel"
        response = private_webservice.patch(
            bzr_snap_url,
            "application/json",
            json.dumps({"owner_link": private_team_url}),
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"A public snap cannot have a private owner.", response.body
        )
        response = private_webservice.patch(
            bzr_snap_url,
            "application/json",
            json.dumps({"branch_link": private_branch_url}),
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"A public snap cannot have a private branch.", response.body
        )
        response = private_webservice.patch(
            git_snap_url,
            "application/json",
            json.dumps({"owner_link": private_team_url}),
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"A public snap cannot have a private owner.", response.body
        )
        response = private_webservice.patch(
            git_snap_url,
            "application/json",
            json.dumps({"git_ref_link": private_ref_url}),
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"A public snap cannot have a private repository.", response.body
        )

    def test_cannot_set_git_path_for_bzr(self):
        # Setting git_path on a Bazaar-based Snap fails.
        snap = self.makeSnap(branch=self.factory.makeAnyBranch())
        response = self.webservice.patch(
            snap["self_link"],
            "application/json",
            json.dumps({"git_path": "HEAD"}),
        )
        self.assertEqual(400, response.status)

    def test_cannot_set_git_path_to_None(self):
        # Setting git_path to None fails.
        snap = self.makeSnap(git_ref=self.factory.makeGitRefs()[0])
        response = self.webservice.patch(
            snap["self_link"],
            "application/json",
            json.dumps({"git_path": None}),
        )
        self.assertEqual(400, response.status)

    def test_set_git_path(self):
        # Setting git_path on a Git-based Snap works.
        ref_master, _ = self.factory.makeGitRefs(
            paths=["refs/heads/master", "refs/heads/next"]
        )
        snap = self.makeSnap(git_ref=ref_master)
        response = self.webservice.patch(
            snap["self_link"],
            "application/json",
            json.dumps({"git_path": "refs/heads/next"}),
        )
        self.assertEqual(209, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "git_repository_link": Equals(snap["git_repository_link"]),
                    "git_path": Equals("refs/heads/next"),
                }
            ),
        )

    def test_set_git_path_external(self):
        # Setting git_path on a Snap backed by an external Git repository
        # works.
        ref = self.factory.makeGitRefRemote()
        repository_url = ref.repository_url
        snap = self.factory.makeSnap(
            registrant=self.person, owner=self.person, git_ref=ref
        )
        snap_url = api_url(snap)
        logout()
        response = self.webservice.patch(
            snap_url, "application/json", json.dumps({"git_path": "HEAD"})
        )
        self.assertEqual(209, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "git_repository_url": Equals(repository_url),
                    "git_path": Equals("HEAD"),
                }
            ),
        )

    def test_is_stale(self):
        # is_stale is exported and is read-only.
        snap = self.makeSnap()
        self.assertTrue(snap["is_stale"])
        response = self.webservice.patch(
            snap["self_link"],
            "application/json",
            json.dumps({"is_stale": False}),
        )
        self.assertEqual(400, response.status)

    def test_getByName(self):
        # lp.snaps.getByName returns a matching Snap.
        snap = self.makeSnap()
        with person_logged_in(self.person):
            owner_url = api_url(self.person)
        response = self.webservice.named_get(
            "/+snaps", "getByName", owner=owner_url, name="mir"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(snap, response.jsonBody())

    def test_getByName_missing(self):
        # lp.snaps.getByName returns 404 for a non-existent Snap.
        logout()
        with person_logged_in(self.person):
            owner_url = api_url(self.person)
        response = self.webservice.named_get(
            "/+snaps", "getByName", owner=owner_url, name="nonexistent"
        )
        self.assertEqual(404, response.status)
        self.assertEqual(
            b"No such snap package with this owner: 'nonexistent'.",
            response.body,
        )

    def test_findByOwner(self):
        # lp.snaps.findByOwner returns all visible Snaps with the given owner.
        persons = [self.factory.makePerson(), self.factory.makePerson()]
        snaps = []
        for person in persons:
            for private in (False, True):
                snaps.append(
                    self.factory.makeSnap(
                        registrant=person, owner=person, private=private
                    )
                )
        with admin_logged_in():
            person_urls = [api_url(person) for person in persons]
            ws_snaps = [
                self.webservice.getAbsoluteUrl(api_url(snap)) for snap in snaps
            ]
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner

        # Anonymous requests can only see public snaps.
        anon_webservice = webservice_for_person(None)
        response = anon_webservice.named_get(
            "/+snaps", "findByOwner", owner=person_urls[0], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[0]],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # persons[0] can see their own private snap as well, but not those
        # for other people.
        webservice = webservice_for_person(
            persons[0], permission=OAuthPermission.READ_PRIVATE
        )
        response = webservice.named_get(
            "/+snaps", "findByOwner", owner=person_urls[0], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:2],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = webservice.named_get(
            "/+snaps", "findByOwner", owner=person_urls[1], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[2]],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # Admins can see all snaps.
        admin_webservice = webservice_for_person(
            admin, permission=OAuthPermission.READ_PRIVATE
        )
        response = webservice.named_get(
            "/+snaps", "findByOwner", owner=person_urls[0], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:2],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = admin_webservice.named_get(
            "/+snaps", "findByOwner", owner=person_urls[1], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[2:],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

    def test_findByURL(self):
        # lp.snaps.findByURL returns visible Snaps with the given URL.
        persons = [self.factory.makePerson(), self.factory.makePerson()]
        urls = ["https://git.example.org/foo", "https://git.example.org/bar"]
        snaps = []
        for url in urls:
            for person in persons:
                for private in (False, True):
                    ref = self.factory.makeGitRefRemote(repository_url=url)
                    snaps.append(
                        self.factory.makeSnap(
                            registrant=person,
                            owner=person,
                            git_ref=ref,
                            private=private,
                        )
                    )
        with admin_logged_in():
            person_urls = [api_url(person) for person in persons]
            ws_snaps = [
                self.webservice.getAbsoluteUrl(api_url(snap)) for snap in snaps
            ]
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner

        # Anonymous requests can only see public snaps.
        anon_webservice = webservice_for_person(None)
        response = anon_webservice.named_get(
            "/+snaps", "findByURL", url=urls[0], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[0], ws_snaps[2]],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = anon_webservice.named_get(
            "/+snaps",
            "findByURL",
            url=urls[0],
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[0]],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # persons[0] can see both public snaps with this URL, as well as
        # their own private snap.
        webservice = webservice_for_person(
            persons[0], permission=OAuthPermission.READ_PRIVATE
        )
        response = webservice.named_get(
            "/+snaps", "findByURL", url=urls[0], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:3],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = webservice.named_get(
            "/+snaps",
            "findByURL",
            url=urls[0],
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:2],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # Admins can see all snaps with this URL.
        admin_webservice = webservice_for_person(
            admin, permission=OAuthPermission.READ_PRIVATE
        )
        response = admin_webservice.named_get(
            "/+snaps", "findByURL", url=urls[0], api_version="devel"
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:4],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByURL",
            url=urls[0],
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:2],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

    def test_findByURLPrefix(self):
        # lp.snaps.findByURLPrefix returns visible Snaps with the given URL
        # prefix.
        self.pushConfig("launchpad", default_batch_size=10)
        persons = [self.factory.makePerson(), self.factory.makePerson()]
        urls = [
            "https://git.example.org/foo/a",
            "https://git.example.org/foo/b",
            "https://git.example.org/bar",
        ]
        snaps = []
        for url in urls:
            for person in persons:
                for private in (False, True):
                    ref = self.factory.makeGitRefRemote(repository_url=url)
                    snaps.append(
                        self.factory.makeSnap(
                            registrant=person,
                            owner=person,
                            git_ref=ref,
                            private=private,
                        )
                    )
        with admin_logged_in():
            person_urls = [api_url(person) for person in persons]
            ws_snaps = [
                self.webservice.getAbsoluteUrl(api_url(snap)) for snap in snaps
            ]
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner
        prefix = "https://git.example.org/foo/"

        # Anonymous requests can only see public snaps.
        anon_webservice = webservice_for_person(None)
        response = anon_webservice.named_get(
            "/+snaps",
            "findByURLPrefix",
            url_prefix=prefix,
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 2, 4, 6)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = anon_webservice.named_get(
            "/+snaps",
            "findByURLPrefix",
            url_prefix=prefix,
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 4)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # persons[0] can see all public snaps with this URL prefix, as well
        # as their own matching private snaps.
        webservice = webservice_for_person(
            persons[0], permission=OAuthPermission.READ_PRIVATE
        )
        response = webservice.named_get(
            "/+snaps",
            "findByURLPrefix",
            url_prefix=prefix,
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 1, 2, 4, 5, 6)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = webservice.named_get(
            "/+snaps",
            "findByURLPrefix",
            url_prefix=prefix,
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 1, 4, 5)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # Admins can see all snaps with this URL prefix.
        admin_webservice = webservice_for_person(
            admin, permission=OAuthPermission.READ_PRIVATE
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByURLPrefix",
            url_prefix=prefix,
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:8],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByURLPrefix",
            url_prefix=prefix,
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 1, 4, 5)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

    def test_findByURLPrefixes(self):
        # lp.snaps.findByURLPrefixes returns visible Snaps with any of the
        # given URL prefixes.
        self.pushConfig("launchpad", default_batch_size=20)
        persons = [self.factory.makePerson(), self.factory.makePerson()]
        urls = [
            "https://git.example.org/foo/a",
            "https://git.example.org/foo/b",
            "https://git.example.org/bar/a",
            "https://git.example.org/bar/b",
            "https://git.example.org/baz",
        ]
        snaps = []
        for url in urls:
            for person in persons:
                for private in (False, True):
                    ref = self.factory.makeGitRefRemote(repository_url=url)
                    snaps.append(
                        self.factory.makeSnap(
                            registrant=person,
                            owner=person,
                            git_ref=ref,
                            private=private,
                        )
                    )
        with admin_logged_in():
            person_urls = [api_url(person) for person in persons]
            ws_snaps = [
                self.webservice.getAbsoluteUrl(api_url(snap)) for snap in snaps
            ]
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner
        prefixes = [
            "https://git.example.org/foo/",
            "https://git.example.org/bar/",
        ]

        # Anonymous requests can only see public snaps.
        anon_webservice = webservice_for_person(None)
        response = anon_webservice.named_get(
            "/+snaps",
            "findByURLPrefixes",
            url_prefixes=prefixes,
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 2, 4, 6, 8, 10, 12, 14)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = anon_webservice.named_get(
            "/+snaps",
            "findByURLPrefixes",
            url_prefixes=prefixes,
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 4, 8, 12)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # persons[0] can see all public snaps with any of these URL
        # prefixes, as well as their own matching private snaps.
        webservice = webservice_for_person(
            persons[0], permission=OAuthPermission.READ_PRIVATE
        )
        response = webservice.named_get(
            "/+snaps",
            "findByURLPrefixes",
            url_prefixes=prefixes,
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = webservice.named_get(
            "/+snaps",
            "findByURLPrefixes",
            url_prefixes=prefixes,
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 1, 4, 5, 8, 9, 12, 13)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # Admins can see all snaps with any of these URL prefixes.
        admin_webservice = webservice_for_person(
            admin, permission=OAuthPermission.READ_PRIVATE
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByURLPrefixes",
            url_prefixes=prefixes,
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:16],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByURLPrefixes",
            url_prefixes=prefixes,
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[i] for i in (0, 1, 4, 5, 8, 9, 12, 13)],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

    def test_findByStoreName(self):
        # lp.snaps.findByStoreName returns visible Snaps with the given
        # store name.
        persons = [self.factory.makePerson(), self.factory.makePerson()]
        store_names = ["foo", "bar"]
        snaps = []
        for store_name in store_names:
            for person in persons:
                for private in (False, True):
                    snaps.append(
                        self.factory.makeSnap(
                            registrant=person,
                            owner=person,
                            private=private,
                            store_name=store_name,
                        )
                    )
        with admin_logged_in():
            person_urls = [api_url(person) for person in persons]
            ws_snaps = [
                self.webservice.getAbsoluteUrl(api_url(snap)) for snap in snaps
            ]
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner

        # Anonymous requests can only see public snaps.
        anon_webservice = webservice_for_person(None)
        response = anon_webservice.named_get(
            "/+snaps",
            "findByStoreName",
            store_name=store_names[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[0], ws_snaps[2]],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = anon_webservice.named_get(
            "/+snaps",
            "findByStoreName",
            store_name=store_names[0],
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            [ws_snaps[0]],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # persons[0] can see both public snaps with this store name, as well
        # as their own private snap.
        webservice = webservice_for_person(
            persons[0], permission=OAuthPermission.READ_PRIVATE
        )
        response = webservice.named_get(
            "/+snaps",
            "findByStoreName",
            store_name=store_names[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:3],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = webservice.named_get(
            "/+snaps",
            "findByStoreName",
            store_name=store_names[0],
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:2],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

        # Admins can see all snaps with this store name.
        admin_webservice = webservice_for_person(
            admin, permission=OAuthPermission.READ_PRIVATE
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByStoreName",
            store_name=store_names[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:4],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )
        response = admin_webservice.named_get(
            "/+snaps",
            "findByStoreName",
            store_name=store_names[0],
            owner=person_urls[0],
            api_version="devel",
        )
        self.assertEqual(200, response.status)
        self.assertContentEqual(
            ws_snaps[:2],
            [entry["self_link"] for entry in response.jsonBody()["entries"]],
        )

    def setProcessors(self, user, snap, names):
        ws = webservice_for_person(
            user, permission=OAuthPermission.WRITE_PUBLIC
        )
        return ws.named_post(
            snap["self_link"],
            "setProcessors",
            processors=["/+processors/%s" % name for name in names],
            api_version="devel",
        )

    def assertProcessors(self, user, snap, names):
        body = (
            webservice_for_person(user)
            .get(snap["self_link"] + "/processors", api_version="devel")
            .jsonBody()
        )
        self.assertContentEqual(
            names, [entry["name"] for entry in body["entries"]]
        )

    def test_setProcessors_admin(self):
        """An admin can add a new processor to the enabled restricted set."""
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        self.factory.makeProcessor(
            "arm", "ARM", "ARM", restricted=True, build_by_default=False
        )
        distroseries = self.factory.makeDistroSeries()
        for processor in getUtility(IProcessorSet).getAll():
            self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        snap = self.makeSnap(distroseries=distroseries)
        self.assertProcessors(ppa_admin, snap, ["386", "hppa", "amd64"])

        response = self.setProcessors(ppa_admin, snap, ["386", "arm"])
        self.assertEqual(200, response.status)
        self.assertProcessors(ppa_admin, snap, ["386", "arm"])

    def test_setProcessors_non_owner_forbidden(self):
        """Only PPA admins and snap owners can call setProcessors."""
        self.factory.makeProcessor(
            "unrestricted",
            "Unrestricted",
            "Unrestricted",
            restricted=False,
            build_by_default=False,
        )
        non_owner = self.factory.makePerson()
        snap = self.makeSnap()

        response = self.setProcessors(non_owner, snap, ["386", "unrestricted"])
        self.assertEqual(401, response.status)

    def test_setProcessors_owner(self):
        """The snap owner can enable/disable unrestricted processors."""
        distroseries = self.factory.makeDistroSeries()
        for processor in getUtility(IProcessorSet).getAll():
            self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
            )
        snap = self.makeSnap(distroseries=distroseries)
        self.assertProcessors(self.person, snap, ["386", "hppa", "amd64"])

        response = self.setProcessors(self.person, snap, ["386"])
        self.assertEqual(200, response.status)
        self.assertProcessors(self.person, snap, ["386"])

        response = self.setProcessors(self.person, snap, ["386", "amd64"])
        self.assertEqual(200, response.status)
        self.assertProcessors(self.person, snap, ["386", "amd64"])

    def test_setProcessors_owner_restricted_forbidden(self):
        """The snap owner cannot enable/disable restricted processors."""
        ppa_admin_team = getUtility(ILaunchpadCelebrities).ppa_admin
        ppa_admin = self.factory.makePerson(member_of=[ppa_admin_team])
        self.factory.makeProcessor(
            "arm", "ARM", "ARM", restricted=True, build_by_default=False
        )
        snap = self.makeSnap()

        response = self.setProcessors(self.person, snap, ["386", "arm"])
        self.assertEqual(403, response.status)

        # If a PPA admin enables arm, the owner cannot disable it.
        response = self.setProcessors(ppa_admin, snap, ["386", "arm"])
        self.assertEqual(200, response.status)
        self.assertProcessors(self.person, snap, ["386", "arm"])

        response = self.setProcessors(self.person, snap, ["386"])
        self.assertEqual(403, response.status)

    def assertBeginsAuthorization(self, snap, **kwargs):
        snap_url = api_url(snap)
        root_macaroon = Macaroon()
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc, "", "dummy"
        )
        root_macaroon_raw = root_macaroon.serialize()
        self.pushConfig("snappy", store_url="http://sca.example/")
        logout()
        with responses.RequestsMock() as requests_mock:
            requests_mock.add(
                "POST",
                "http://sca.example/dev/api/acl/",
                json={"macaroon": root_macaroon_raw},
            )
            response = self.webservice.named_post(
                snap_url, "beginAuthorization", **kwargs
            )
            [call] = requests_mock.calls
        self.assertThat(
            call.request,
            MatchesStructure.byEquality(
                url="http://sca.example/dev/api/acl/", method="POST"
            ),
        )
        with person_logged_in(self.person):
            expected_body = {
                "packages": [
                    {
                        "name": snap.store_name,
                        "series": snap.store_series.name,
                    }
                ],
                "permissions": ["package_upload"],
            }
            self.assertEqual(expected_body, json.loads(call.request.body))
            self.assertEqual({"root": root_macaroon_raw}, snap.store_secrets)
        return response, root_macaroon.third_party_caveats()[0]

    def test_beginAuthorization(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        response, sso_caveat = self.assertBeginsAuthorization(snap)
        self.assertEqual(sso_caveat.caveat_id, response.jsonBody())

    def test_beginAuthorization_unauthorized(self):
        # A user without edit access cannot authorize snap package uploads.
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        snap_url = api_url(snap)
        other_person = self.factory.makePerson()
        other_webservice = webservice_for_person(
            other_person, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        response = other_webservice.named_post(snap_url, "beginAuthorization")
        self.assertEqual(401, response.status)

    def test_completeAuthorization(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        root_macaroon = Macaroon()
        discharge_macaroon = Macaroon()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize()},
        )
        snap_url = api_url(snap)
        logout()
        response = self.webservice.named_post(
            snap_url,
            "completeAuthorization",
            discharge_macaroon=discharge_macaroon.serialize(),
        )
        self.assertEqual(200, response.status)
        with person_logged_in(self.person):
            self.assertEqual(
                {
                    "root": root_macaroon.serialize(),
                    "discharge": discharge_macaroon.serialize(),
                },
                snap.store_secrets,
            )

    def test_completeAuthorization_without_beginAuthorization(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        snap_url = api_url(snap)
        logout()
        discharge_macaroon = Macaroon()
        response = self.webservice.named_post(
            snap_url,
            "completeAuthorization",
            discharge_macaroon=discharge_macaroon.serialize(),
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400,
                body=(
                    b"beginAuthorization must be called before "
                    b"completeAuthorization."
                ),
            ),
        )

    def test_completeAuthorization_unauthorized(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        root_macaroon = Macaroon()
        discharge_macaroon = Macaroon()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize()},
        )
        snap_url = api_url(snap)
        other_person = self.factory.makePerson()
        other_webservice = webservice_for_person(
            other_person, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        response = other_webservice.named_post(
            snap_url,
            "completeAuthorization",
            discharge_macaroon=discharge_macaroon.serialize(),
        )
        self.assertEqual(401, response.status)

    def test_completeAuthorization_both_macaroons(self):
        # It is possible to do the authorization work entirely externally
        # and send both root and discharge macaroons in one go.
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        snap_url = api_url(snap)
        logout()
        root_macaroon = Macaroon()
        discharge_macaroon = Macaroon()
        response = self.webservice.named_post(
            snap_url,
            "completeAuthorization",
            root_macaroon=root_macaroon.serialize(),
            discharge_macaroon=discharge_macaroon.serialize(),
        )
        self.assertEqual(200, response.status)
        with person_logged_in(self.person):
            self.assertEqual(
                {
                    "root": root_macaroon.serialize(),
                    "discharge": discharge_macaroon.serialize(),
                },
                snap.store_secrets,
            )

    def test_completeAuthorization_only_root_macaroon(self):
        # It is possible to store only a root macaroon.  This may make sense
        # if the store has some other way to determine that user consent has
        # been acquired and thus has not added a third-party caveat.
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        snap_url = api_url(snap)
        logout()
        root_macaroon = Macaroon()
        response = self.webservice.named_post(
            snap_url,
            "completeAuthorization",
            root_macaroon=root_macaroon.serialize(),
        )
        self.assertEqual(200, response.status)
        with person_logged_in(self.person):
            self.assertEqual(
                {"root": root_macaroon.serialize()}, snap.store_secrets
            )

    def test_completeAuthorization_malformed_root_macaroon(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        snap_url = api_url(snap)
        logout()
        response = self.webservice.named_post(
            snap_url, "completeAuthorization", root_macaroon="nonsense"
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=b"root_macaroon is invalid."
            ),
        )

    def test_completeAuthorization_malformed_discharge_macaroon(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
        )
        snap_url = api_url(snap)
        logout()
        response = self.webservice.named_post(
            snap_url,
            "completeAuthorization",
            root_macaroon=Macaroon().serialize(),
            discharge_macaroon="nonsense",
        )
        self.assertThat(
            response,
            MatchesStructure.byEquality(
                status=400, body=b"discharge_macaroon is invalid."
            ),
        )

    def test_completeAuthorization_encrypted(self):
        private_key = PrivateKey.generate()
        self.pushConfig(
            "snappy",
            store_secrets_public_key=base64.b64encode(
                bytes(private_key.public_key)
            ).decode("UTF-8"),
        )
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries()
        root_macaroon = Macaroon()
        discharge_macaroon = Macaroon()
        snap = self.factory.makeSnap(
            registrant=self.person,
            store_upload=True,
            store_series=snappy_series,
            store_name=self.factory.getUniqueUnicode(),
            store_secrets={"root": root_macaroon.serialize()},
        )
        snap_url = api_url(snap)
        logout()
        response = self.webservice.named_post(
            snap_url,
            "completeAuthorization",
            discharge_macaroon=discharge_macaroon.serialize(),
        )
        self.assertEqual(200, response.status)
        self.pushConfig(
            "snappy",
            store_secrets_private_key=base64.b64encode(
                bytes(private_key)
            ).decode("UTF-8"),
        )
        container = getUtility(IEncryptedContainer, "snap-store-secrets")
        with person_logged_in(self.person):
            self.assertThat(
                snap.store_secrets,
                MatchesDict(
                    {
                        "root": Equals(root_macaroon.serialize()),
                        "discharge_encrypted": AfterPreprocessing(
                            lambda data: container.decrypt(data).decode(
                                "UTF-8"
                            ),
                            Equals(discharge_macaroon.serialize()),
                        ),
                    }
                ),
            )

    def makeBuildableDistroArchSeries(self, **kwargs):
        das = self.factory.makeDistroArchSeries(**kwargs)
        fake_chroot = self.factory.makeLibraryFileAlias(
            filename="fake_chroot.tar.gz", db_only=True
        )
        das.addOrUpdateChroot(fake_chroot)
        return das

    def test_requestBuild(self):
        # Build requests can be performed and end up in snap.builds and
        # snap.pending_builds.
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archive_url = api_url(distroseries.main_archive)
        snap = self.makeSnap(distroseries=distroseries, processors=[processor])
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(201, response.status)
        build = self.webservice.get(response.getHeader("Location")).jsonBody()
        self.assertEqual(
            [build["self_link"]], self.getCollectionLinks(snap, "builds")
        )
        self.assertEqual([], self.getCollectionLinks(snap, "completed_builds"))
        self.assertEqual(
            [build["self_link"]],
            self.getCollectionLinks(snap, "pending_builds"),
        )

    def test_requestBuild_rejects_repeats(self):
        # Build requests are rejected if already pending.
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archive_url = api_url(distroseries.main_archive)
        snap = self.makeSnap(distroseries=distroseries, processors=[processor])
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(201, response.status)
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"An identical build of this snap package is already pending.",
            response.body,
        )

    def test_requestBuild_not_owner(self):
        # If the requester is not the owner or a member of the owner team,
        # build requests are rejected.
        other_team = self.factory.makeTeam(displayname="Other Team")
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archive_url = api_url(distroseries.main_archive)
        other_webservice = webservice_for_person(
            other_team.teamowner, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        login(ANONYMOUS)
        snap = self.makeSnap(
            owner=other_team,
            distroseries=distroseries,
            processors=[processor],
            webservice=other_webservice,
        )
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Test Person cannot create snap package builds owned by Other "
            b"Team.",
            response.body,
        )

    def test_requestBuild_archive_disabled(self):
        # Build requests against a disabled archive are rejected.
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(IDistributionSet)["ubuntu"],
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution,
            owner=self.person,
            enabled=False,
            displayname="Disabled Archive",
        )
        archive_url = api_url(archive)
        snap = self.makeSnap(distroseries=distroseries, processors=[processor])
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(403, response.status)
        self.assertEqual(b"Disabled Archive is disabled.", response.body)

    def test_requestBuild_archive_private_owners_match(self):
        # Build requests against a private archive are allowed if the Snap
        # and Archive owners match exactly.
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(IDistributionSet)["ubuntu"],
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution,
            owner=self.person,
            private=True,
        )
        archive_url = api_url(archive)
        snap = self.makeSnap(distroseries=distroseries, processors=[processor])
        private_webservice = webservice_for_person(
            self.person, permission=OAuthPermission.WRITE_PRIVATE
        )
        private_webservice.default_api_version = "devel"
        response = private_webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(201, response.status)

    def test_requestBuild_archive_private_owners_mismatch(self):
        # Build requests against a private archive are rejected if the Snap
        # and Archive owners do not match exactly.
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(IDistributionSet)["ubuntu"],
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archive = self.factory.makeArchive(
            distribution=distroseries.distribution, private=True
        )
        archive_url = api_url(archive)
        snap = self.makeSnap(distroseries=distroseries, processors=[processor])
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuild",
            archive=archive_url,
            distro_arch_series=distroarchseries_url,
            pocket="Updates",
        )
        self.assertEqual(403, response.status)
        self.assertEqual(
            b"Snap package builds against private archives are only allowed "
            b"if the snap package owner and the archive owner are equal.",
            response.body,
        )

    def test_requestBuilds(self):
        # Requests for builds for all relevant architectures can be
        # performed over the webservice, and the returned entry indicates
        # the status of the asynchronous job.
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        processors = [
            self.factory.makeProcessor(supports_virtualized=True)
            for _ in range(3)
        ]
        for processor in processors:
            self.makeBuildableDistroArchSeries(
                distroseries=distroseries,
                architecturetag=processor.name,
                processor=processor,
                owner=self.person,
            )
        archive_url = api_url(distroseries.main_archive)
        [git_ref] = self.factory.makeGitRefs()
        snap = self.makeSnap(
            git_ref=git_ref, distroseries=distroseries, processors=processors
        )
        now = get_transaction_timestamp(IStore(distroseries))
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuilds",
            archive=archive_url,
            pocket="Updates",
            channels={"snapcraft": "edge", "snapd": "edge"},
        )
        self.assertEqual(201, response.status)
        build_request_url = response.getHeader("Location")
        build_request = self.webservice.get(build_request_url).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, GreaterThan(now)
                    ),
                    "date_finished": Is(None),
                    "snap_link": Equals(snap["self_link"]),
                    "status": Equals("Pending"),
                    "error_message": Is(None),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        self.assertEqual([], self.getCollectionLinks(build_request, "builds"))
        with person_logged_in(self.person):
            snapcraft_yaml = "architectures:\n"
            for processor in processors:
                snapcraft_yaml += "  - build-on: %s\n" % processor.name
            self.useFixture(GitHostingFixture(blob=snapcraft_yaml))
            [job] = getUtility(ISnapRequestBuildsJobSource).iterReady()
            with dbuser(config.ISnapRequestBuildsJobSource.dbuser):
                JobRunner([job]).runAll()
        date_requested = iso8601.parse_date(build_request["date_requested"])
        now = get_transaction_timestamp(IStore(distroseries))
        build_request = self.webservice.get(
            build_request["self_link"]
        ).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, Equals(date_requested)
                    ),
                    "date_finished": AfterPreprocessing(
                        iso8601.parse_date,
                        MatchesAll(GreaterThan(date_requested), LessThan(now)),
                    ),
                    "snap_link": Equals(snap["self_link"]),
                    "status": Equals("Completed"),
                    "error_message": Is(None),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        builds = self.webservice.get(
            build_request["builds_collection_link"]
        ).jsonBody()["entries"]
        with person_logged_in(self.person):
            self.assertThat(
                builds,
                MatchesSetwise(
                    *(
                        ContainsDict(
                            {
                                "snap_link": Equals(snap["self_link"]),
                                "archive_link": Equals(
                                    self.getURL(distroseries.main_archive)
                                ),
                                "arch_tag": Equals(processor.name),
                                "pocket": Equals("Updates"),
                                "channels": Equals(
                                    {"snapcraft": "edge", "snapd": "edge"}
                                ),
                            }
                        )
                        for processor in processors
                    )
                ),
            )

    def test_requestBuilds_failure(self):
        # If the asynchronous build request job fails, this is reflected in
        # the build request entry.
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        processor = self.factory.makeProcessor(supports_virtualized=True)
        self.makeBuildableDistroArchSeries(
            distroseries=distroseries,
            architecturetag=processor.name,
            processor=processor,
            owner=self.person,
        )
        archive_url = api_url(distroseries.main_archive)
        [git_ref] = self.factory.makeGitRefs()
        snap = self.makeSnap(
            git_ref=git_ref, distroseries=distroseries, processors=[processor]
        )
        now = get_transaction_timestamp(IStore(distroseries))
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuilds",
            archive=archive_url,
            pocket="Updates",
        )
        self.assertEqual(201, response.status)
        build_request_url = response.getHeader("Location")
        build_request = self.webservice.get(build_request_url).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, GreaterThan(now)
                    ),
                    "date_finished": Is(None),
                    "snap_link": Equals(snap["self_link"]),
                    "status": Equals("Pending"),
                    "error_message": Is(None),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        self.assertEqual([], self.getCollectionLinks(build_request, "builds"))
        with person_logged_in(self.person):
            self.useFixture(GitHostingFixture()).getBlob.failure = Exception(
                "Something went wrong"
            )
            [job] = getUtility(ISnapRequestBuildsJobSource).iterReady()
            with dbuser(config.ISnapRequestBuildsJobSource.dbuser):
                JobRunner([job]).runAll()
        date_requested = iso8601.parse_date(build_request["date_requested"])
        now = get_transaction_timestamp(IStore(distroseries))
        build_request = self.webservice.get(
            build_request["self_link"]
        ).jsonBody()
        self.assertThat(
            build_request,
            ContainsDict(
                {
                    "date_requested": AfterPreprocessing(
                        iso8601.parse_date, Equals(date_requested)
                    ),
                    "date_finished": AfterPreprocessing(
                        iso8601.parse_date,
                        MatchesAll(GreaterThan(date_requested), LessThan(now)),
                    ),
                    "snap_link": Equals(snap["self_link"]),
                    "status": Equals("Failed"),
                    "error_message": Equals("Something went wrong"),
                    "builds_collection_link": Equals(
                        build_request_url + "/builds"
                    ),
                }
            ),
        )
        self.assertEqual([], self.getCollectionLinks(build_request, "builds"))

    def test_requestBuilds_not_owner(self):
        # If the requester is not the owner or a member of the owner team,
        # build requests are rejected.
        other_team = self.factory.makeTeam(displayname="Other Team")
        distroseries = self.factory.makeDistroSeries(registrant=self.person)
        archive_url = api_url(distroseries.main_archive)
        other_webservice = webservice_for_person(
            other_team.teamowner, permission=OAuthPermission.WRITE_PUBLIC
        )
        other_webservice.default_api_version = "devel"
        login(ANONYMOUS)
        snap = self.makeSnap(
            owner=other_team,
            distroseries=distroseries,
            webservice=other_webservice,
        )
        response = self.webservice.named_post(
            snap["self_link"],
            "requestBuilds",
            archive=archive_url,
            pocket="Updates",
        )
        self.assertEqual(401, response.status)
        self.assertEqual(
            b"Test Person cannot create snap package builds owned by Other "
            b"Team.",
            response.body,
        )

    def test_requestAutoBuilds(self):
        # requestAutoBuilds can be performed over the webservice.
        distroseries = self.factory.makeDistroSeries()
        dases = []
        das_urls = []
        for _ in range(3):
            processor = self.factory.makeProcessor(supports_virtualized=True)
            dases.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=processor
                )
            )
            das_urls.append(api_url(dases[-1]))
        archive = self.factory.makeArchive()
        snap = self.makeSnap(
            distroseries=distroseries,
            processors=[das.processor for das in dases[:2]],
            auto_build_archive=archive,
            auto_build_pocket=PackagePublishingPocket.PROPOSED,
        )
        response = self.webservice.named_post(
            snap["self_link"], "requestAutoBuilds"
        )
        self.assertEqual(200, response.status)
        builds = response.jsonBody()
        self.assertContentEqual(
            [
                self.webservice.getAbsoluteUrl(das_url)
                for das_url in das_urls[:2]
            ],
            [build["distro_arch_series_link"] for build in builds],
        )

    def test_requestAutoBuilds_requires_auto_build_archive(self):
        # requestAutoBuilds fails if auto_build_archive is not set.
        snap = self.makeSnap()
        response = self.webservice.named_post(
            snap["self_link"], "requestAutoBuilds"
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"This snap package cannot have automatic builds created for it "
            b"because auto_build_archive is not set.",
            response.body,
        )

    def test_requestAutoBuilds_requires_auto_build_pocket(self):
        # requestAutoBuilds fails if auto_build_pocket is not set.
        snap = self.makeSnap(auto_build_archive=self.factory.makeArchive())
        response = self.webservice.named_post(
            snap["self_link"], "requestAutoBuilds"
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            b"This snap package cannot have automatic builds created for it "
            b"because auto_build_pocket is not set.",
            response.body,
        )

    def test_requestAutoBuilds_requires_all_requests_to_succeed(self):
        # requestAutoBuilds fails if any one of its build requests fail.
        distroseries = self.factory.makeDistroSeries()
        dases = []
        for _ in range(2):
            processor = self.factory.makeProcessor(supports_virtualized=True)
            dases.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=processor
                )
            )
        archive = self.factory.makeArchive(enabled=False)
        snap = self.makeSnap(
            distroseries=distroseries,
            processors=[das.processor for das in dases[:2]],
            auto_build_archive=archive,
            auto_build_pocket=PackagePublishingPocket.PROPOSED,
        )
        response = self.webservice.named_post(
            snap["self_link"], "requestAutoBuilds"
        )
        self.assertEqual(403, response.status)
        self.assertEqual(
            ("%s is disabled." % archive.displayname).encode("UTF-8"),
            response.body,
        )
        response = self.webservice.get(snap["builds_collection_link"])
        self.assertEqual([], response.jsonBody()["entries"])

    def test_requestAutoBuilds_allows_already_pending(self):
        # requestAutoBuilds succeeds if some of its build requests are
        # already pending.
        distroseries = self.factory.makeDistroSeries()
        dases = []
        das_urls = []
        for _ in range(3):
            processor = self.factory.makeProcessor(supports_virtualized=True)
            dases.append(
                self.makeBuildableDistroArchSeries(
                    distroseries=distroseries, processor=processor
                )
            )
            das_urls.append(api_url(dases[-1]))
        archive = self.factory.makeArchive()
        snap = self.makeSnap(
            distroseries=distroseries,
            processors=[das.processor for das in dases[:2]],
            auto_build_archive=archive,
            auto_build_pocket=PackagePublishingPocket.PROPOSED,
        )
        with person_logged_in(self.person):
            db_snap = getUtility(ISnapSet).getByName(self.person, snap["name"])
            db_snap.requestBuild(
                self.person,
                archive,
                dases[0],
                pocket=PackagePublishingPocket.PROPOSED,
                target_architectures=[dases[0].architecturetag],
            )
        response = self.webservice.named_post(
            snap["self_link"], "requestAutoBuilds"
        )
        self.assertEqual(200, response.status)
        builds = response.jsonBody()
        self.assertContentEqual(
            [self.webservice.getAbsoluteUrl(das_urls[1])],
            [build["distro_arch_series_link"] for build in builds],
        )

    def test_getBuilds(self):
        # The builds, completed_builds, and pending_builds properties are as
        # expected.
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(IDistributionSet)["ubuntu"],
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        distroarchseries_url = api_url(distroarchseries)
        archives = [
            self.factory.makeArchive(
                distribution=distroseries.distribution, owner=self.person
            )
            for x in range(4)
        ]
        archive_urls = [api_url(archive) for archive in archives]
        snap = self.makeSnap(distroseries=distroseries, processors=[processor])
        builds = []
        for archive_url in archive_urls:
            response = self.webservice.named_post(
                snap["self_link"],
                "requestBuild",
                archive=archive_url,
                distro_arch_series=distroarchseries_url,
                pocket="Proposed",
            )
            self.assertEqual(201, response.status)
            build = self.webservice.get(
                response.getHeader("Location")
            ).jsonBody()
            builds.insert(0, build["self_link"])
        self.assertEqual(builds, self.getCollectionLinks(snap, "builds"))
        self.assertEqual([], self.getCollectionLinks(snap, "completed_builds"))
        self.assertEqual(
            builds, self.getCollectionLinks(snap, "pending_builds")
        )
        snap = self.webservice.get(snap["self_link"]).jsonBody()

        with person_logged_in(self.person):
            db_snap = getUtility(ISnapSet).getByName(self.person, snap["name"])
            db_builds = list(db_snap.builds)
            db_builds[0].updateStatus(
                BuildStatus.BUILDING, date_started=db_snap.date_created
            )
            db_builds[0].updateStatus(
                BuildStatus.FULLYBUILT,
                date_finished=db_snap.date_created + timedelta(minutes=10),
            )
        snap = self.webservice.get(snap["self_link"]).jsonBody()
        # Builds that have not yet been started are listed last.  This does
        # mean that pending builds that have never been started are sorted
        # to the end, but means that builds that were cancelled before
        # starting don't pollute the start of the collection forever.
        self.assertEqual(builds, self.getCollectionLinks(snap, "builds"))
        self.assertEqual(
            builds[:1], self.getCollectionLinks(snap, "completed_builds")
        )
        self.assertEqual(
            builds[1:], self.getCollectionLinks(snap, "pending_builds")
        )

        with person_logged_in(self.person):
            db_builds[1].updateStatus(
                BuildStatus.BUILDING, date_started=db_snap.date_created
            )
            db_builds[1].updateStatus(
                BuildStatus.FULLYBUILT,
                date_finished=db_snap.date_created + timedelta(minutes=20),
            )
        snap = self.webservice.get(snap["self_link"]).jsonBody()
        self.assertEqual(
            [builds[1], builds[0], builds[2], builds[3]],
            self.getCollectionLinks(snap, "builds"),
        )
        self.assertEqual(
            [builds[1], builds[0]],
            self.getCollectionLinks(snap, "completed_builds"),
        )
        self.assertEqual(
            builds[2:], self.getCollectionLinks(snap, "pending_builds")
        )

    def test_query_count(self):
        # Snap has a reasonable query count.
        snap = self.factory.makeSnap(registrant=self.person, owner=self.person)
        url = api_url(snap)
        logout()
        store = Store.of(snap)
        store.flush()
        store.invalidate()
        with StormStatementRecorder() as recorder:
            self.webservice.get(url)
        self.assertThat(recorder, HasQueryCount(Equals(15)))

    def test_builds_query_count(self):
        # The query count of Snap.builds is constant regardless of the number
        # of builds, even if they have store upload jobs.
        self.pushConfig(
            "snappy",
            store_url="http://sca.example/",
            store_upload_url="http://updown.example/",
        )
        with admin_logged_in():
            snappyseries = self.factory.makeSnappySeries()
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(IDistributionSet)["ubuntu"],
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        with person_logged_in(self.person):
            snap = self.factory.makeSnap(
                registrant=self.person,
                owner=self.person,
                distroseries=distroseries,
                processors=[processor],
            )
            snap.store_series = snappyseries
            snap.store_name = self.factory.getUniqueUnicode()
            snap.store_upload = True
            snap.store_secrets = {"root": Macaroon().serialize()}
        builds_url = "%s/builds" % api_url(snap)
        logout()

        def make_build():
            with person_logged_in(self.person):
                builder = self.factory.makeBuilder()
                build = snap.requestBuild(
                    self.person,
                    distroseries.main_archive,
                    distroarchseries,
                    PackagePublishingPocket.PROPOSED,
                )
                with dbuser(config.builddmaster.dbuser):
                    build.updateStatus(
                        BuildStatus.BUILDING, date_started=snap.date_created
                    )
                    build.updateStatus(
                        BuildStatus.FULLYBUILT,
                        builder=builder,
                        date_finished=(
                            snap.date_created + timedelta(minutes=10)
                        ),
                    )
                return build

        def get_builds():
            response = self.webservice.get(builds_url)
            self.assertEqual(200, response.status)
            return response

        recorder1, recorder2 = record_two_runs(get_builds, make_build, 2)
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def test_builds_metadata_url(self):
        # The DB load for the `build_metadata_url` attribute of builds returns
        # the expected value
        with admin_logged_in():
            snappyseries = self.factory.makeSnappySeries()
        distroseries = self.factory.makeDistroSeries(
            distribution=getUtility(IDistributionSet)["ubuntu"],
            registrant=self.person,
        )
        processor = self.factory.makeProcessor(supports_virtualized=True)
        distroarchseries = self.makeBuildableDistroArchSeries(
            distroseries=distroseries, processor=processor, owner=self.person
        )
        with person_logged_in(self.person):
            snap = self.factory.makeSnap(
                registrant=self.person,
                owner=self.person,
                distroseries=distroseries,
                processors=[processor],
            )
            snap.store_series = snappyseries
            snap.store_name = self.factory.getUniqueUnicode()
            snap.store_upload = True
            snap.store_secrets = {"root": Macaroon().serialize()}
        builds_url = "%s/builds" % api_url(snap)
        logout()

        with person_logged_in(self.person):
            build = snap.requestBuild(
                self.person,
                distroseries.main_archive,
                distroarchseries,
                PackagePublishingPocket.PROPOSED,
            )
            snap1_lfa = self.factory.makeLibraryFileAlias(
                filename="test.snap",
                content=b"dummy snap content",
                db_only=True,
            )
            metadata_filename = f"{build.build_cookie}_metadata.json"
            snap2_lfa = self.factory.makeLibraryFileAlias(
                filename=metadata_filename,
                content=b"dummy snap content",
                db_only=True,
            )
            self.factory.makeSnapFile(snapbuild=build, libraryfile=snap2_lfa)
            self.factory.makeSnapFile(snapbuild=build, libraryfile=snap1_lfa)

        response = self.webservice.get(builds_url)
        self.assertEqual(200, response.status)
        snap_builds = (response.jsonBody()["entries"],)
        self.assertEqual(1, len(snap_builds))
        self.assertIn(
            metadata_filename,
            snap_builds[0][0]["build_metadata_url"],
        )
