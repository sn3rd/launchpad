# Copyright 2009-2021 Canonical Ltd.  This software is licensed under
# the GNU Affero General Public License version 3 (see the file
# LICENSE).

__all__ = [
    "BuildFarmJob",
    "BuildFarmJobMixin",
    "SpecificBuildFarmJobSourceMixin",
]

from datetime import datetime, timezone

from storm.expr import Desc, LeftJoin, Or
from storm.locals import DateTime, Int, Reference
from storm.store import Store
from zope.component import getUtility
from zope.interface import implementer, provider

from lp.buildmaster.enums import BuildFarmJobType, BuildStatus
from lp.buildmaster.interfaces.buildfarmjob import (
    CannotBeRescored,
    CannotBeRetried,
    IBuildFarmJob,
    IBuildFarmJobDB,
    IBuildFarmJobSet,
    IBuildFarmJobSource,
)
from lp.buildmaster.model.buildqueue import BuildQueue
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.statsd.interfaces.statsd_client import IStatsdClient

VALID_STATUS_TRANSITIONS = {
    BuildStatus.NEEDSBUILD: tuple(BuildStatus.items),
    BuildStatus.FULLYBUILT: (),
    BuildStatus.FAILEDTOBUILD: (BuildStatus.NEEDSBUILD,),
    BuildStatus.MANUALDEPWAIT: (BuildStatus.NEEDSBUILD,),
    BuildStatus.CHROOTWAIT: (BuildStatus.NEEDSBUILD,),
    BuildStatus.SUPERSEDED: (),
    BuildStatus.BUILDING: tuple(BuildStatus.items),
    BuildStatus.FAILEDTOUPLOAD: (BuildStatus.NEEDSBUILD,),
    BuildStatus.GATHERING: (
        BuildStatus.NEEDSBUILD,
        BuildStatus.FAILEDTOBUILD,
        BuildStatus.UPLOADING,
    ),
    BuildStatus.UPLOADING: (
        BuildStatus.FULLYBUILT,
        BuildStatus.FAILEDTOUPLOAD,
        BuildStatus.SUPERSEDED,
        BuildStatus.CANCELLED,
    ),
    BuildStatus.CANCELLING: (BuildStatus.CANCELLED,),
    BuildStatus.CANCELLED: (BuildStatus.NEEDSBUILD,),
}


@implementer(IBuildFarmJob, IBuildFarmJobDB)
@provider(IBuildFarmJobSource)
class BuildFarmJob(StormBase):
    """A base implementation for `IBuildFarmJob` classes."""

    __storm_table__ = "BuildFarmJob"

    id = Int(primary=True)

    date_created = DateTime(
        name="date_created", allow_none=False, tzinfo=timezone.utc
    )

    date_finished = DateTime(
        name="date_finished", allow_none=True, tzinfo=timezone.utc
    )

    builder_id = Int(name="builder", allow_none=True)
    builder = Reference(builder_id, "Builder.id")

    status = DBEnum(name="status", allow_none=False, enum=BuildStatus)

    job_type = DBEnum(name="job_type", allow_none=False, enum=BuildFarmJobType)

    archive_id = Int(name="archive")
    archive = Reference(archive_id, "Archive.id")

    def __init__(
        self,
        job_type,
        status=BuildStatus.NEEDSBUILD,
        date_created=None,
        builder=None,
        archive=None,
    ):
        super().__init__()
        (self.job_type, self.status, self.builder, self.archive) = (
            job_type,
            status,
            builder,
            archive,
        )
        if date_created is not None:
            self.date_created = date_created

    @classmethod
    def new(
        cls,
        job_type,
        status=BuildStatus.NEEDSBUILD,
        date_created=None,
        builder=None,
        archive=None,
    ):
        """See `IBuildFarmJobSource`."""
        build_farm_job = BuildFarmJob(
            job_type, status, date_created, builder, archive
        )
        store = IPrimaryStore(BuildFarmJob)
        store.add(build_farm_job)
        return build_farm_job


class BuildFarmJobMixin:
    @property
    def builder_constraints(self):
        return None

    @property
    def dependencies(self):
        return None

    @property
    def external_dependencies(self):
        return None

    @property
    def title(self):
        """See `IBuildFarmJob`."""
        return self.job_type.title

    @property
    def duration(self):
        """See `IBuildFarmJob`."""
        if self.date_started is None or self.date_finished is None:
            return None
        return self.date_finished - self.date_started

    @cachedproperty
    def buildqueue_record(self):
        """See `IBuildFarmJob`."""
        return (
            Store.of(self)
            .find(BuildQueue, _build_farm_job_id=self.build_farm_job_id)
            .one()
        )

    @property
    def is_private(self):
        """See `IBuildFarmJob`.

        This base implementation assumes build farm jobs are public, but
        derived implementations can override as required.
        """
        return False

    @property
    def log_url(self):
        """See `IBuildFarmJob`.

        This base implementation of the property always returns None. Derived
        implementations need to override for their specific context.
        """
        return None

    @property
    def was_built(self):
        """See `IBuild`"""
        return self.status not in [
            BuildStatus.NEEDSBUILD,
            BuildStatus.BUILDING,
            BuildStatus.CANCELLED,
            BuildStatus.CANCELLING,
            BuildStatus.GATHERING,
            BuildStatus.UPLOADING,
            BuildStatus.SUPERSEDED,
        ]

    @property
    def build_cookie(self):
        """See `IBuildFarmJob`."""
        return "%s-%s" % (self.job_type.name, self.id)

    def setLog(self, log):
        """See `IBuildFarmJob`."""
        self.log = log

    def emitMetric(self, metric_name, **extra):
        """See `IBuildFarmJob`."""
        labels = {"job_type": self.job_type.name}
        if self.processor is not None:
            labels["arch"] = self.processor.name
        if self.builder is not None:
            labels.update(
                {
                    "builder_name": self.builder.name,
                    "virtualized": str(self.builder.virtualized),
                    "region": self.builder.region,
                }
            )
        labels.update(extra)
        getUtility(IStatsdClient).incr("build.%s" % metric_name, labels=labels)

    def updateStatus(
        self,
        status,
        builder=None,
        worker_status=None,
        date_started=None,
        date_finished=None,
        force_invalid_transition=False,
    ):
        """See `IBuildFarmJob`."""
        if (
            not force_invalid_transition
            and status != self.build_farm_job.status
            and status
            not in VALID_STATUS_TRANSITIONS[self.build_farm_job.status]
        ):
            raise AssertionError(
                "Can't change build status from %s to %s."
                % (self.build_farm_job.status.name, status.name)
            )

        self.build_farm_job.status = self.status = status

        # If there's a builder provided, set it if we don't already have
        # one, or otherwise crash if it's different from the one we
        # expected.
        if builder is not None:
            if self.builder is None:
                self.build_farm_job.builder = self.builder = builder
            else:
                assert self.builder == builder

        # If we're starting to build, set date_started and
        # date_first_dispatched if required.
        if self.date_started is None and status == BuildStatus.BUILDING:
            self.date_started = date_started or datetime.now(timezone.utc)
            if self.date_first_dispatched is None:
                self.date_first_dispatched = self.date_started

        # If we're in a final build state (or UPLOADING, which sort of
        # is), set date_finished if date_started is.
        if (
            self.date_started is not None
            and self.date_finished is None
            and status
            not in (
                BuildStatus.NEEDSBUILD,
                BuildStatus.BUILDING,
                BuildStatus.GATHERING,
                BuildStatus.CANCELLING,
            )
        ):
            # XXX cprov 20060615 bug=120584: Currently buildduration includes
            # the scanner latency, it should really be asking the worker for
            # the duration spent building locally.
            self.build_farm_job.date_finished = self.date_finished = (
                date_finished or datetime.now(timezone.utc)
            )
            self.emitMetric("finished", status=status.name)

    def gotFailure(self):
        """See `IBuildFarmJob`."""
        self.failure_count += 1

    def calculateScore(self):
        """See `IBuildFarmJob`."""
        raise NotImplementedError

    def estimateDuration(self):
        """See `IBuildFarmJob`."""
        raise NotImplementedError

    def queueBuild(self, suspended=False):
        """See `IBuildFarmJob`."""
        duration_estimate = self.estimateDuration()
        queue_entry = BuildQueue(
            estimated_duration=duration_estimate,
            build_farm_job=self.build_farm_job,
            processor=self.processor,
            virtualized=self.virtualized,
            builder_constraints=self.builder_constraints,
        )

        # This build queue job is to be created in a suspended state.
        if suspended:
            queue_entry.suspend()

        Store.of(self).add(queue_entry)
        del get_property_cache(self).buildqueue_record
        return queue_entry

    @property
    def can_be_retried(self):
        """See `IBuildFarmJob`.

        Implementations should override this method to first check whether
        their associated build behaviour would accept the build if it
        succeeded.
        """
        failed_statuses = [
            BuildStatus.FAILEDTOBUILD,
            BuildStatus.MANUALDEPWAIT,
            BuildStatus.CHROOTWAIT,
            BuildStatus.FAILEDTOUPLOAD,
            BuildStatus.CANCELLED,
            BuildStatus.SUPERSEDED,
        ]

        # If the build is currently in any of the failed states,
        # it may be retried.
        return self.status in failed_statuses

    @property
    def can_be_rescored(self):
        """See `IBuildFarmJob`."""
        return (
            self.buildqueue_record is not None
            and self.status is BuildStatus.NEEDSBUILD
        )

    @property
    def can_be_cancelled(self):
        """See `IBuildFarmJob`."""
        if not self.buildqueue_record:
            return False

        cancellable_statuses = [
            BuildStatus.BUILDING,
            BuildStatus.NEEDSBUILD,
        ]
        return self.status in cancellable_statuses

    def clearBuilder(self):
        """See `IBuildFarmJob`."""
        self.build_farm_job.builder = self.builder = None

    def resetBuild(self):
        """See `IBuildFarmJob`."""
        self.build_farm_job.status = self.status = BuildStatus.NEEDSBUILD
        self.build_farm_job.date_finished = self.date_finished = None
        self.date_started = None
        self.clearBuilder()
        self.log = None
        self.upload_log = None
        self.dependencies = None
        self.failure_count = 0

    def retry(self):
        """See `IBuildFarmJob`."""
        if not self.can_be_retried:
            raise CannotBeRetried(self.id)

        self.resetBuild()
        self.queueBuild()

    def rescore(self, score):
        """See `IBuildFarmJob`."""
        if not self.can_be_rescored:
            raise CannotBeRescored(self.id)

        self.buildqueue_record.manualScore(score)

    def cancel(self):
        """See `IBuildFarmJob`."""
        if not self.can_be_cancelled:
            return
        # BuildQueue.cancel() will decide whether to go straight to
        # CANCELLED, or go through CANCELLING to let buildd-manager clean up
        # the builder.
        self.buildqueue_record.cancel()

    def cancelUpload(self):
        """See `IBuildFarmJob`."""
        if self.status != BuildStatus.UPLOADING:
            return
        self.updateStatus(BuildStatus.CANCELLED)


class SpecificBuildFarmJobSourceMixin:
    @staticmethod
    def addCandidateSelectionCriteria():
        """See `ISpecificBuildFarmJobSource`."""
        return ""

    @staticmethod
    def postprocessCandidate(job, logger):
        """See `ISpecificBuildFarmJobSource`."""
        return True


@implementer(IBuildFarmJobSet)
class BuildFarmJobSet:
    def getBuildsForBuilder(self, builder_id, status=None, user=None):
        """See `IBuildFarmJobSet`."""
        # Imported here to avoid circular imports.
        from lp.soyuz.model.archive import Archive, get_archive_privacy_filter

        clauses = [
            BuildFarmJob.builder == builder_id,
            Or(Archive.id == None, get_archive_privacy_filter(user)),
        ]
        if status is not None:
            clauses.append(BuildFarmJob.status == status)

        # We need to ensure that we don't include any private builds.
        # Currently only package builds can be private (via their
        # related archive), but not all build farm jobs will have a
        # related package build - hence the left join.
        origin = [
            BuildFarmJob,
            LeftJoin(Archive, Archive.id == BuildFarmJob.archive_id),
        ]

        return (
            IStore(BuildFarmJob)
            .using(*origin)
            .find(BuildFarmJob, *clauses)
            .order_by(Desc(BuildFarmJob.date_finished), BuildFarmJob.id)
        )

    def getBuildsForArchive(self, archive, status=None):
        """See `IBuildFarmJobSet`."""

        extra_exprs = []

        if status is not None:
            extra_exprs.append(BuildFarmJob.status == status)

        result_set = IStore(BuildFarmJob).find(
            BuildFarmJob, BuildFarmJob.archive == archive, *extra_exprs
        )

        # When we have a set of builds that may include pending or
        # superseded builds, we order by -date_created (as we won't
        # always have a date_finished). Otherwise we can order by
        # -date_finished.
        unfinished_states = [
            BuildStatus.NEEDSBUILD,
            BuildStatus.BUILDING,
            BuildStatus.GATHERING,
            BuildStatus.UPLOADING,
            BuildStatus.SUPERSEDED,
        ]
        if status is None or status in unfinished_states:
            result_set.order_by(
                Desc(BuildFarmJob.date_created), BuildFarmJob.id
            )
        else:
            result_set.order_by(
                Desc(BuildFarmJob.date_finished), BuildFarmJob.id
            )

        return result_set
