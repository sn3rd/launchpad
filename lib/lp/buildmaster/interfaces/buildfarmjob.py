# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interface for Soyuz build farm jobs."""

__all__ = [
    "CannotBeRescored",
    "CannotBeRetried",
    "IBuildFarmJob",
    "IBuildFarmJobAdmin",
    "IBuildFarmJobDB",
    "IBuildFarmJobEdit",
    "IBuildFarmJobSet",
    "IBuildFarmJobSource",
    "IBuildFarmJobView",
    "InconsistentBuildFarmJobError",
    "ISpecificBuildFarmJobSource",
]

import http.client

from lazr.restful.declarations import (
    error_status,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
)
from lazr.restful.fields import Reference
from zope.interface import Attribute, Interface
from zope.schema import Bool, Choice, Datetime, Int, TextLine, Timedelta, Tuple

from lp import _
from lp.buildmaster.enums import BuildFarmJobType, BuildStatus
from lp.buildmaster.interfaces.builder import IBuilder
from lp.buildmaster.interfaces.processor import IProcessor
from lp.services.librarian.interfaces import ILibraryFileAlias


class InconsistentBuildFarmJobError(Exception):
    """Raised when a BuildFarmJob is in an inconsistent state.

    For example, if a BuildFarmJob has a job type for which no adapter
    is yet implemented. Or when adapting the BuildFarmJob to a specific
    type of build job (such as a BinaryPackageBuild) fails.
    """


@error_status(http.client.BAD_REQUEST)
class CannotBeRetried(Exception):
    """Raised when retrying a build that cannot be retried."""

    def __init__(self, build_id):
        super().__init__("Build %s cannot be retried." % build_id)


@error_status(http.client.BAD_REQUEST)
class CannotBeRescored(Exception):
    """Raised when rescoring a build that cannot be rescored."""

    def __init__(self, build_id):
        super().__init__("Build %s cannot be rescored." % build_id)


class IBuildFarmJobDB(Interface):
    """Operations on a `BuildFarmJob` DB row.

    This is deprecated while it's flattened into the concrete implementations.
    """

    id = Attribute("The build farm job ID.")

    job_type = Choice(
        title=_("Job type"),
        required=True,
        readonly=True,
        vocabulary=BuildFarmJobType,
        description=_("The specific type of job."),
    )


class IBuildFarmJobView(Interface):
    """`IBuildFarmJob` attributes that require launchpad.View."""

    id = Attribute("The build farm job ID.")

    build_farm_job = Attribute("Generic build farm job record")

    processor = Reference(
        IProcessor,
        title=_("Processor"),
        required=False,
        readonly=True,
        description=_(
            "The Processor required by this build farm job. "
            "This should be None for processor-independent job types."
        ),
    )

    virtualized = Bool(
        title=_("Virtualized"),
        required=False,
        readonly=True,
        description=_(
            "The virtualization setting required by this build farm job. "
            "This should be None for job types that do not care whether "
            "they run virtualized."
        ),
    )

    builder_constraints = Tuple(
        title=_("Builder constraints"),
        required=False,
        readonly=True,
        value_type=TextLine(),
        description=_(
            "Builder resource tags required by this build farm job."
        ),
    )

    date_created = exported(
        Datetime(
            title=_("Date created"),
            required=True,
            readonly=True,
            description=_(
                "The timestamp when the build farm job was created."
            ),
        ),
        ("1.0", dict(exported_as="datecreated")),
        as_of="beta",
    )

    date_started = exported(
        Datetime(
            title=_("Date started"),
            required=False,
            readonly=True,
            description=_(
                "The timestamp when the build farm job was started."
            ),
        ),
        as_of="devel",
    )

    date_finished = exported(
        Datetime(
            title=_("Date finished"),
            required=False,
            readonly=True,
            description=_(
                "The timestamp when the build farm job was finished."
            ),
        ),
        ("1.0", dict(exported_as="datebuilt")),
        as_of="beta",
    )

    duration = exported(
        Timedelta(
            title=_("Duration"),
            required=False,
            readonly=True,
            description=_(
                "Duration interval, calculated when the "
                "result gets collected."
            ),
        ),
        as_of="devel",
    )

    date_first_dispatched = exported(
        Datetime(
            title=_("Date finished"),
            required=False,
            readonly=True,
            description=_(
                "The actual build start time. Set when the build "
                "is dispatched the first time and not changed in "
                "subsequent build attempts."
            ),
        )
    )

    builder = exported(
        Reference(
            title=_("Builder"),
            schema=IBuilder,
            required=False,
            readonly=True,
            description=_("The builder assigned to this job."),
        )
    )

    buildqueue_record = Reference(
        # Really IBuildQueue, patched in lp.buildmaster.interfaces.webservice.
        schema=Interface,
        required=True,
        title=_("Corresponding BuildQueue record"),
    )

    status = exported(
        Choice(
            title=_("Status"),
            required=True,
            vocabulary=BuildStatus,
            description=_("The current status of the job."),
        ),
        ("1.0", dict(exported_as="buildstate")),
        as_of="beta",
    )

    log = Reference(
        schema=ILibraryFileAlias,
        required=False,
        title=_(
            "The LibraryFileAlias containing the entire log for this job."
        ),
    )

    log_url = exported(
        TextLine(
            title=_("Build Log URL"),
            required=False,
            description=_(
                "A URL for the build log. None if there is no "
                "log available."
            ),
        ),
        ("1.0", dict(exported_as="build_log_url")),
        as_of="beta",
    )

    is_private = Bool(
        title=_("is private"),
        required=False,
        readonly=True,
        description=_("Whether the build should be treated as private."),
    )

    job_type = Choice(
        title=_("Job type"),
        required=True,
        readonly=True,
        vocabulary=BuildFarmJobType,
        description=_("The specific type of job."),
    )

    build_cookie = Attribute(
        "A string which uniquely identifies the job in the build farm."
    )

    failure_count = Int(
        title=_("Failure Count"),
        required=False,
        readonly=True,
        default=0,
        description=_("Number of consecutive failures for this job."),
    )

    def setLog(log):
        """Set the `LibraryFileAlias` that contains the job log."""

    def emitMetric(metric_name, **extra):
        """Emit a metric for this build.

        :param metric_name: The name of the metric (which will be prefixed
            with "build.".
        :param extra: Extra labels to attach to the metric.
        """

    def updateStatus(
        status,
        builder=None,
        worker_status=None,
        date_started=None,
        date_finished=None,
        force_invalid_transition=False,
    ):
        """Update job metadata when the build status changes.

        This automatically handles setting status, date_finished, builder,
        dependencies. Later it will manage the denormalised search schema.

        date_started and date_finished override the default (now).

        Only sensible transitions are permitted unless
        force_invalid_transition is set. The override only exists for
        tests and as an escape hatch for buildd-manager's failure
        counting. You do not want to use it.
        """

    def gotFailure():
        """Increment the failure_count for this job."""

    def calculateScore():
        """Calculate the build queue priority for this job."""

    def estimateDuration():
        """Estimate the build duration."""

    def queueBuild(suspended=False):
        """Create a BuildQueue entry for this build.

        :param suspended: Whether the associated `Job` instance should be
            created in a suspended state.
        """

    title = exported(TextLine(title=_("Title"), required=False), as_of="beta")

    was_built = Attribute("Whether or not modified by the builddfarm.")

    # This doesn't belong here.  It really belongs in IPackageBuild, but
    # the TAL assumes it can read this directly.
    dependencies = exported(
        TextLine(
            title=_("Dependencies"),
            required=False,
            description=_(
                "Debian-like dependency line that must be satisfied before "
                "attempting to build this request."
            ),
        ),
        as_of="beta",
    )

    # Only really used by IBinaryPackageBuild, but
    # get_sources_list_for_building looks up this attribute for all build
    # types.
    external_dependencies = Attribute(
        "Newline-separated list of repositories to be used to retrieve any "
        "external build-dependencies when performing this build."
    )

    can_be_rescored = exported(
        Bool(
            title=_("Can be rescored"),
            required=True,
            readonly=True,
            description=_(
                "Whether this build record can be rescored manually."
            ),
        )
    )

    can_be_retried = exported(
        Bool(
            title=_("Can be retried"),
            required=True,
            readonly=True,
            description=_("Whether this build record can be retried."),
        )
    )

    can_be_cancelled = exported(
        Bool(
            title=_("Can be cancelled"),
            required=True,
            readonly=True,
            description=_("Whether this build record can be cancelled."),
        )
    )

    def clearBuilder():
        """Clear this build record's builder.

        This is called by `BuildQueue.reset` as part of resetting jobs so
        that they can be re-dispatched.
        """


class IBuildFarmJobEdit(Interface):
    """`IBuildFarmJob` methods that require launchpad.Edit."""

    def resetBuild():
        """Reset this build record to a clean state.

        This method should only be called by `BuildFarmJobMixin.retry`, but
        subclasses may override it to reset additional state.
        """

    @export_write_operation()
    @operation_for_version("devel")
    def retry():
        """Restore the build record to its initial state.

        Build record loses its history, is moved to NEEDSBUILD and a new
        non-scored BuildQueue entry is created for it.
        """

    @export_write_operation()
    @operation_for_version("devel")
    def cancel():
        """Cancel the build if it is either pending or in progress.

        Check the can_be_cancelled property prior to calling this method to
        find out if cancelling the build is possible.

        If the build is in progress, it is marked as CANCELLING until the
        buildd manager terminates the build and marks it CANCELLED.  If the
        build is not in progress, it is marked CANCELLED immediately and is
        removed from the build queue.

        If the build is not in a cancellable state, this method is a no-op.
        """


class IBuildFarmJobAdmin(Interface):
    """`IBuildFarmJob` methods that require launchpad.Admin."""

    @operation_parameters(score=Int(title=_("Score"), required=True))
    @export_write_operation()
    @operation_for_version("devel")
    def rescore(score):
        """Change the build's score."""

    @export_write_operation()
    @operation_for_version("devel")
    def cancelUpload():
        """Cancel a build in the UPLOADING state.

        When a build transitions to UPLOADING its BuildQueue record is
        destroyed, so the ordinary cancel() path cannot reach it. This
        admin-only endpoint transitions the build directly to CANCELLED.

        `BuildUploadHandler.process()` will take care of cleaning the build
        directory in `incoming/` in case it exists.

        If the build is not in UPLOADING state this method is a no-op.
        """


@exported_as_webservice_entry(as_of="beta")
class IBuildFarmJob(IBuildFarmJobView, IBuildFarmJobEdit, IBuildFarmJobAdmin):
    """Operations that jobs for the build farm must implement."""


class ISpecificBuildFarmJobSource(Interface):
    """A utility for retrieving objects of a specific IBuildFarmJob type.

    Implementations are registered with their BuildFarmJobType's name.
    """

    def getByID(id):
        """Look up a concrete `IBuildFarmJob` by ID.

        :param id: An ID of the concrete job class to look up.
        """

    def getByBuildFarmJobs(build_farm_jobs):
        """Look up the concrete `IBuildFarmJob`s for a list of BuildFarmJobs.

        :param build_farm_jobs: A list of BuildFarmJobs for which to get the
            concrete jobs.
        """

    def getByBuildFarmJob(build_farm_job):
        """Look up the concrete `IBuildFarmJob` for a BuildFarmJob.

        :param build_farm_job: A BuildFarmJob for which to get the concrete
            job.
        """

    def addCandidateSelectionCriteria():
        """Provide a sub-query to refine the candidate job selection.

        Return a sub-query to narrow down the list of candidate jobs.
        The sub-query will become part of an "outer query" and is free to
        refer to the `BuildQueue` and `BuildFarmJob` tables already utilized
        in the latter.

        :return: a string containing a sub-query that narrows down the list of
            candidate jobs.
        """

    def postprocessCandidate(job, logger):
        """True if the candidate job is fine and should be dispatched
        to a builder, False otherwise.

        :param job: The `BuildQueue` instance to be scrutinized.
        :param logger: The logger to use.

        :return: True if the candidate job should be dispatched
            to a builder, False otherwise.
        """


class IBuildFarmJobSource(Interface):
    """A utility of BuildFarmJob used to create _things_."""

    def new(
        job_type, status=None, processor=None, virtualized=None, builder=None
    ):
        """Create a new `IBuildFarmJob`.

        :param job_type: A `BuildFarmJobType` item.
        :param status: A `BuildStatus` item, defaulting to PENDING.
        :param processor: An optional processor for this job.
        :param virtualized: An optional boolean indicating whether
            this job should be run virtualized.
        :param builder: An optional `IBuilder`.
        """


class IBuildFarmJobSet(Interface):
    """A utility representing a set of build farm jobs."""

    def getBuildsForBuilder(builder_id, status=None, user=None):
        """Return `IBuildFarmJob` records touched by a builder.

        :param builder_id: The id of the builder for which to find builds.
        :param status: If given, limit the search to builds with this status.
        :param user: If given, this will be used to determine private builds
            that should be included.
        :return: a `ResultSet` representing the requested builds.
        """

    def getBuildsForArchive(archive, status=None):
        """Return `IBuildFarmJob` records targeted to a given `IArchive`.

        :param archive: The archive for which builds will be returned.
        :param status: If status is provided, only builders with that
            status will be returned.
        :return: a `ResultSet` representing the requested `IBuildFarmJobs`.
        """
