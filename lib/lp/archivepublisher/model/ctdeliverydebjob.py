# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = ["CTDeliveryDebJob", "CTDeliveryError", "CTUnavailableError"]

import csv
import json
import logging
from datetime import datetime, timedelta, timezone
from itertools import chain
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from storm.expr import (
    SQL,
    Alias,
    And,
    Column,
    Eq,
    Gt,
    Join,
    Le,
    LeftJoin,
    Lt,
    Ne,
    Select,
    Table,
)
from zope.component import getUtility
from zope.interface import implementer, provider

from lp.archivepublisher.interfaces.ctdeliveryjob import (
    CTDeliveryJobType,
    ICTDeliveryDebJob,
    ICTDeliveryDebJobSource,
)
from lp.archivepublisher.model.archivepublisherrun import (
    ArchivePublisherRun,
    ArchivePublisherRunStatus,
)
from lp.archivepublisher.model.archivepublishinghistory import (
    ArchivePublishingHistory,
)
from lp.archivepublisher.model.ctdeliveryjob import (
    CTDeliveryJob,
    CTDeliveryJobDerived,
)
from lp.registry.interfaces.distroseries import IDistroSeriesSet
from lp.registry.interfaces.pocket import PackagePublishingPocket, pocketsuffix
from lp.registry.interfaces.sourcepackage import SourcePackageFileType
from lp.services.commitmenttracker import get_commitment_tracker_client
from lp.services.config import config
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.features import getFeatureFlag
from lp.services.job.model.job import Job
from lp.soyuz.enums import (
    ArchivePurpose,
    BinaryPackageFormat,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.model.archive import ARCHIVE_REFERENCE_TEMPLATES

logger = logging.getLogger(__name__)

CT_DELIVERY_ENABLED = "commitment_tracker.delivery.enabled"

POCKET_TO_NAME = {
    item.value: (
        "release" if (_suffix := pocketsuffix[item]) == "" else _suffix[1:]
    )
    for item in PackagePublishingPocket.items
}


class CTDeliveryError(Exception):
    """CT delivery failure that should trigger a job retry."""


class CTUnavailableError(CTDeliveryError):
    """CT service is unreachable."""


@implementer(ICTDeliveryDebJob)
@provider(ICTDeliveryDebJobSource)
class CTDeliveryDebJob(CTDeliveryJobDerived):
    class_job_type = CTDeliveryJobType.DEB

    user_error_types = ()
    retry_error_types = (CTDeliveryError,)

    # Retry sooner than the default.
    retry_delay = timedelta(minutes=5)
    max_retries = 3

    config = config.ICTDeliveryDebJobSource

    @staticmethod
    def _is_delivery_enabled():
        """Return True if the CT delivery feature flag is enabled."""
        return bool(getFeatureFlag(CT_DELIVERY_ENABLED))

    @property
    def publishing_history(self):
        # Prefer the Storm-loaded reference on the context; fall back to an
        # explicit fetch by id if not already loaded.
        if getattr(self.context, "publishing_history", None) is not None:
            return self.context.publishing_history
        if self.context.publishing_history_id is not None:
            return IStore(ArchivePublishingHistory).get(
                self.context.publishing_history_id
            )
        return None

    @property
    def error_description(self):
        return self.metadata.get("result", {}).get("error_description", [])

    @property
    def metadata(self):
        return self.context.metadata

    @classmethod
    def create(
        cls, publishing_history_id: int
    ) -> Optional["CTDeliveryDebJob"]:
        """Create a new `CTDeliveryDebJob` using `IArchivePublishingHistory`.

        :param publishing_history_id: The id of the
            `IArchivePublishingHistory` associated with this job.
        """
        if not cls._is_delivery_enabled():
            logger.info(
                "[CT] Delivery disabled via feature flag "
                f"{CT_DELIVERY_ENABLED}"
            )
            return None

        if publishing_history_id is None:
            raise ValueError("publishing_history not found")

        # Schedule the initialization.
        metadata = {
            "result": {
                "error_description": [],
                "bpph": [],
                "spph": [],
                "ct_success_count": 0,
                "ct_failure_count": 0,
            },
        }

        ctdeliveryjob = CTDeliveryJob(
            publishing_history_id, cls.class_job_type, metadata
        )
        # Configure retry policy for the underlying Job.
        ctdeliveryjob.job.max_retries = cls.max_retries

        store = IPrimaryStore(CTDeliveryJob)
        store.add(ctdeliveryjob)
        derived_job = cls(ctdeliveryjob)
        derived_job.celeryRunOnCommit()
        IStore(CTDeliveryJob).flush()
        return derived_job

    @classmethod
    def create_manual(
        cls,
        archive_id: Optional[int] = None,
        date_start: Optional[datetime] = None,
        date_end: Optional[datetime] = None,
        distroseries: Optional[int] = None,
        status: int = PackagePublishingStatus.PUBLISHED.value,
        csv_output: Optional[str] = None,
    ) -> Optional["CTDeliveryDebJob"]:
        """Create a new `CTDeliveryDebJob` manually.

        :param archive_id: The id of the archive to process.
        :param date_start: Start of the date range.
        :param date_end: End of the date range.
        :param distroseries: The id of the distroseries to filter.
        :param status: The publishing status to filter by.
        :param csv_output: Path to CSV file for output instead of HTTP calls.
        """
        if not cls._is_delivery_enabled():
            logger.info(
                "[CT] Delivery disabled via feature flag "
                f"{CT_DELIVERY_ENABLED}"
            )
            return None

        if archive_id is None and distroseries is None:
            raise ValueError(
                "Either archive_id or distroseries must be specified"
            )

        if date_start and date_end and date_start > date_end:
            raise ValueError(
                "date_start must be less than or equal to date_end"
            )

        manual_mode = {
            "archive_id": archive_id,
            "status": status,
            "date_start": date_start.timestamp() if date_start else None,
            "date_end": date_end.timestamp() if date_end else None,
            "distroseries": distroseries,
            "csv_output": csv_output,
        }

        metadata = {
            "result": {
                "error_description": [],
                "bpph": [],
                "spph": [],
                "ct_success_count": 0,
                "ct_failure_count": 0,
            },
            "manual_mode": manual_mode,
        }

        ctdeliveryjob = CTDeliveryJob(None, cls.class_job_type, metadata)
        # Configure retry policy for the underlying Job.
        ctdeliveryjob.job.max_retries = cls.max_retries

        store = IPrimaryStore(CTDeliveryJob)
        store.add(ctdeliveryjob)
        derived_job = cls(ctdeliveryjob)
        IStore(CTDeliveryJob).flush()
        return derived_job

    @classmethod
    def iterReady(cls):
        """See `IJobSource`."""
        store = IPrimaryStore(CTDeliveryJob)
        jobs = store.find(
            CTDeliveryJob,
            And(
                CTDeliveryJob.job_type == cls.class_job_type,
                CTDeliveryJob.job_id.is_in(Job.ready_jobs),
            ),
        )
        return (cls(job) for job in jobs)

    @classmethod
    def get(cls, publishing_history) -> Optional["CTDeliveryDebJob"]:
        """See `ICTDeliveryDebJob`."""
        ctdelivery_job = (
            IStore(CTDeliveryJob)
            .find(
                CTDeliveryJob,
                CTDeliveryJob.job_id == Job.id,
                CTDeliveryJob.job_type == cls.class_job_type,
                CTDeliveryJob.publishing_history == publishing_history,
            )
            .one()
        )
        return None if ctdelivery_job is None else cls(ctdelivery_job)

    def __repr__(self) -> str:
        """Returns an informative representation of the job."""
        publishing_history_id = (
            self.publishing_history.id if self.publishing_history else None
        )
        return (
            f"<{self.__class__.__name__} for "
            f"publishing_history: {publishing_history_id}, "
            f"metadata: {self.metadata}>"
        )

    def run(self) -> None:
        """See `IRunnableJob`."""
        if not self._is_delivery_enabled():
            logger.info(
                "[CT] Delivery disabled via feature flag "
                f"{CT_DELIVERY_ENABLED}; skipping."
            )
            return

        result = self.metadata.get("result", {})
        failed_bpph_ids = result.get("failed_bpph_ids")
        failed_spph_ids = result.get("failed_spph_ids")

        if failed_bpph_ids or failed_spph_ids:
            self._run_retry_mode(failed_bpph_ids or [], failed_spph_ids or [])
            return

        manual_mode = self.metadata.get("manual_mode")

        if manual_mode:
            # Manual mode: process date range for an archive
            self._run_manual_mode(manual_mode)
        else:
            # Single publishing history mode
            self._run_publishing_mode()

    def _run_publishing_mode(self) -> None:
        """Run in single publishing history mode."""
        if self.publishing_history is None:
            logger.warning(
                f"Publishing history {self.context.publishing_history_id} not "
                "found; skipping job"
            )
            return

        archive = self.publishing_history.archive
        distribution_name = archive.distribution.name
        current_run = self.publishing_history.publisher_run

        curr_finished = current_run.date_finished
        if curr_finished is None:
            # If `curr_finished` is None, we can just use `now()`.
            # A job gets created with the last transaction in the publisher
            # for that archive so the xpph are all set for that archive.
            curr_finished = datetime.now(timezone.utc)

        lookback_start = datetime.now(timezone.utc) - timedelta(days=60)

        prev_row = self._find_previous_run(
            archive_id=archive.id, before_ts=curr_finished
        )
        if prev_row is None:
            # First run for this archive: fall back to the lookback window so
            # initial publishes are still delivered to CT.
            prev_run_id, prev_finished = None, lookback_start
            logger.info(
                f"No previous successful run found for archive {archive.id}; "
                f"run={current_run.id}. Using lookback window starting "
                f"{lookback_start}."
            )
        else:
            prev_run_id, prev_finished = prev_row

        self._process_publishing_window(
            archive=archive,
            distribution_name=distribution_name,
            datecreated_start=lookback_start,
            datecreated_end=curr_finished,
            datepublished_start=prev_finished,
            datepublished_end=curr_finished,
            prev_run_id=prev_run_id,
            current_run_id=current_run.id if current_run else None,
            distroseries=None,
        )

    def _run_manual_mode(self, manual_mode_params: dict) -> None:
        """Run in manual mode for initial population or backfilling."""
        archive_id = manual_mode_params.get("archive_id")
        if archive_id is not None:
            archive_id = int(archive_id)

        date_start = manual_mode_params.get("date_start")
        date_end = manual_mode_params.get("date_end")
        distroseries = manual_mode_params.get("distroseries")
        status_raw = manual_mode_params.get("status")
        status = (
            int(status_raw)
            if status_raw is not None
            else PackagePublishingStatus.PUBLISHED.value
        )
        csv_output = manual_mode_params.get("csv_output")

        lookback_start = None
        if date_start is not None:
            date_start = datetime.fromtimestamp(date_start, tz=timezone.utc)
            lookback_start = date_start - timedelta(days=60)
        if date_end is not None:
            date_end = datetime.fromtimestamp(date_end, tz=timezone.utc)

        # Fetch archive if specified
        archive = None
        distribution_name = None
        if archive_id is not None:
            archive = getUtility(IArchiveSet).get(archive_id)
            if archive is None:
                logger.warning(f"Archive {archive_id} not found; skipping job")
                return
            distribution_name = archive.distribution.name
        else:
            # Either archive or distroseries is specified
            distribution_name = (
                getUtility(IDistroSeriesSet)
                .get(distroseries)
                .distribution.name
            )

        logger.info(
            f"CTDeliveryDebJob manual mode: archive={archive_id} "
            f"date_start={date_start} date_end={date_end} "
            f"distroseries={distroseries} status={status} "
            f"csv_output={csv_output}"
        )

        self._process_publishing_window(
            archive=archive,
            distribution_name=distribution_name,
            datecreated_start=lookback_start,
            datecreated_end=date_end,
            datepublished_start=date_start,
            datepublished_end=date_end,
            prev_run_id=None,
            current_run_id=None,
            distroseries=distroseries,
            status=status,
            csv_output=csv_output,
        )

    def _run_retry_mode(
        self,
        failed_bpph_ids: List[int],
        failed_spph_ids: List[int],
    ) -> None:
        """Retry delivery for previously failed payloads by ID."""
        logger.info(
            "[CT] Retrying failed payloads: "
            f"bpph_ids={failed_bpph_ids}, spph_ids={failed_spph_ids}"
        )

        manual_mode = self.metadata.get("manual_mode")
        if manual_mode:
            archive_id = manual_mode.get("archive_id")
            if archive_id is not None:
                archive = getUtility(IArchiveSet).get(int(archive_id))
                if archive is None:
                    logger.warning(
                        f"Archive {archive_id} not found; cannot retry"
                    )
                    return
                distribution_name = archive.distribution.name
            else:
                distroseries_id = manual_mode.get("distroseries")
                distribution_name = (
                    getUtility(IDistroSeriesSet)
                    .get(distroseries_id)
                    .distribution.name
                )
                archive = None
        else:
            if self.publishing_history is None:
                logger.warning("Publishing history not found; cannot retry")
                return
            archive = self.publishing_history.archive
            distribution_name = archive.distribution.name

        archive_ref = (
            (
                "primary"
                if archive.purpose == ArchivePurpose.PRIMARY
                else str(archive.reference)
            )
            if archive
            else None
        )

        store = IStore(ArchivePublisherRun)
        bpph_rows = (
            self._query_bpph_rows(
                store=store,
                archive=archive,
                specific_ids=failed_bpph_ids,
            )
            if failed_bpph_ids
            else []
        )
        spph_rows = (
            self._query_spph_rows(
                store=store,
                archive=archive,
                specific_ids=failed_spph_ids,
            )
            if failed_spph_ids
            else []
        )

        payloads_gen, bpph_ids, spph_ids = self._build_payloads(
            bpph_rows=bpph_rows,
            spph_rows=spph_rows,
            distribution_name=distribution_name,
            archive_reference=archive_ref,
        )

        metadata = self.metadata
        result = metadata.setdefault("result", {})
        result["bpph"] = bpph_ids
        result["spph"] = spph_ids
        # Clear previous failure markers; _deliver_payloads will re-set
        # them if delivery fails again.
        result.pop("failed_bpph_ids", None)
        result.pop("failed_spph_ids", None)

        if bpph_rows or spph_rows:
            self._deliver_payloads(payloads_gen)
        else:
            logger.info(
                "[CT] No rows found for retry IDs; " "clearing failure state."
            )

    def _process_publishing_window(
        self,
        archive,
        distribution_name: str,
        datecreated_start: Optional[datetime],
        datecreated_end: Optional[datetime],
        datepublished_start: Optional[datetime],
        datepublished_end: Optional[datetime],
        prev_run_id: Optional[int],
        current_run_id: Optional[int],
        distroseries: Optional[int],
        status: int = PackagePublishingStatus.PUBLISHED.value,
        csv_output: Optional[str] = None,
    ) -> None:
        """Common processing for manual and celery mode.

        1. Fetches publishing history (BPPH/SPPH)
        2. Builds payloads from the history
        3. Delivers payloads (via CSV or HTTP)
        """
        archive_ref = (
            (
                "primary"
                if archive.purpose == ArchivePurpose.PRIMARY
                else str(archive.reference)
            )
            if archive
            else None
        )
        logger.debug(
            f"CTDeliveryDebJob for archive={archive_ref} "
            f"datecreated_start={datecreated_start} "
            f"datecreated_end={datecreated_end} "
            f"datepublished_start={datepublished_start} "
            f"datepublished_end={datepublished_end} "
            f"distroseries={distroseries} status={status}"
        )

        store = IStore(ArchivePublisherRun)

        logger.info(f"Querying BPPH rows for {archive_ref}")
        bpph_rows = self._query_bpph_rows(
            store=store,
            archive=archive,
            datecreated_start=datecreated_start,
            datecreated_end=datecreated_end,
            datepublished_start=datepublished_start,
            datepublished_end=datepublished_end,
            distroseries=distroseries,
            status=status,
        )

        logger.info(f"Querying SPPH rows for {archive_ref}")
        spph_rows = self._query_spph_rows(
            store=store,
            archive=archive,
            datecreated_start=datecreated_start,
            datecreated_end=datecreated_end,
            datepublished_start=datepublished_start,
            datepublished_end=datepublished_end,
            distroseries=distroseries,
            status=status,
        )

        logger.info(
            f"Building payloads for CT delivery: binaries={len(bpph_rows)} "
            f"sources={len(spph_rows)}"
        )
        payloads_gen, bpph_ids, spph_ids = self._build_payloads(
            bpph_rows=bpph_rows,
            spph_rows=spph_rows,
            distribution_name=distribution_name,
            archive_reference=archive_ref,
        )

        # Persist a light summary in metadata (keep payloads out).
        metadata = self.metadata
        metadata.setdefault("result", {})
        metadata["result"]["bpph"] = bpph_ids
        metadata["result"]["spph"] = spph_ids

        # Check if we have any rows to process
        if not bpph_rows and not spph_rows:
            logger.info("[CT] no payloads to deliver for this window.")
            return

        self._deliver_payloads(payloads_gen, csv_output)

        logger.info(
            f"Completed processing window: archive={archive_ref} "
            f"prev_run={prev_run_id}({datepublished_start}) "
            f"curr_run={current_run_id}({datepublished_end}) "
            f"binaries={len(bpph_rows)} sources={len(spph_rows)}"
        )

    def _deliver_payloads(
        self,
        tagged_payloads: Iterable[Tuple[str, Dict[str, Any]]],
        csv_output: Optional[str] = None,
    ) -> None:
        """Deliver payloads either to CSV file or via HTTP.

        :param tagged_payloads: Iterable of (tracking_id, payload) tuples.
        :param csv_output: If provided, path to CSV file for output.
                          Otherwise, sends via HTTP to Commitment Tracker.
        :raises CTUnavailableError: If CT health check fails.
        :raises CTDeliveryError: If any payloads fail delivery.
        """
        metadata = self.metadata.setdefault("result", {})

        if csv_output:
            logger.info(f"Writing payloads to CSV: {csv_output}")
            bare_payloads = (payload for _, payload in tagged_payloads)
            count = self._write_to_csv(bare_payloads, csv_output)
            metadata["ct_success_count"] = count
            metadata["ct_failure_count"] = 0
            metadata["csv_filename"] = csv_output
        else:
            # Send via HTTP to Commitment Tracker
            logger.info("Sending payloads to CT")
            client = get_commitment_tracker_client()

            if not client.check_health():
                raise CTUnavailableError("CT service health check failed")

            success_count, failed_tracking_ids = (
                client.send_payloads_with_results(tagged_payloads)
            )
            metadata["ct_success_count"] = success_count

            if failed_tracking_ids:
                failed_bpph, failed_spph = self._parse_failed_tracking_ids(
                    failed_tracking_ids
                )
                metadata["ct_failure_count"] = len(failed_tracking_ids)
                metadata["failed_bpph_ids"] = failed_bpph
                metadata["failed_spph_ids"] = failed_spph
                metadata.setdefault("error_description", []).extend(
                    f"Failed: {tid}" for tid in failed_tracking_ids
                )
                raise CTDeliveryError(
                    f"{len(failed_tracking_ids)} payload(s) failed delivery"
                )
            else:
                metadata["ct_failure_count"] = 0

    @staticmethod
    def _parse_failed_tracking_ids(
        tracking_ids: List[str],
    ) -> Tuple[List[int], List[int]]:
        """Parse tracking ID strings into separate bpph and spph ID lists."""
        failed_bpph_ids: List[int] = []
        failed_spph_ids: List[int] = []
        for tid in tracking_ids:
            if tid.startswith("bpph:"):
                for id_str in tid[5:].split(","):
                    id_str = id_str.strip()
                    if id_str:
                        failed_bpph_ids.append(int(id_str))
            elif tid.startswith("spph:"):
                failed_spph_ids.append(int(tid[5:].strip()))
        return failed_bpph_ids, failed_spph_ids

    def notifyUserError(self, error) -> None:
        """Calls up and also saves the error text in this job's metadata.

        See `BaseRunnableJob`.
        """
        # This method is called when error is an instance of
        # self.user_error_types.
        super().notifyUserError(error)
        logger.error(error)
        error_description = self.metadata.get("result").get(
            "error_description", []
        )
        error_description.append(str(error))
        self.metadata["result"]["error_description"] = error_description

    def getOopsVars(self) -> List[Tuple]:
        """See `IRunnableJob`."""
        vars = super().getOopsVars()
        vars.extend(
            [
                ("ctdeliveryjob_job_id", self.context.id),
                ("ctdeliveryjob_job_type", self.context.job_type.title),
                ("publishing_history", self.context.publishing_history),
            ]
        )
        return vars

    def _find_previous_run(
        self, archive_id: int, before_ts: datetime
    ) -> Optional[Tuple]:
        """Find previous successful run for this archive before a timestamp."""
        apr = Alias(Table("ArchivePublisherRun"), "apr")
        aph = Alias(Table("ArchivePublishingHistory"), "aph")
        tables = Join(
            aph, apr, on=Eq(Column("publisher_run", aph), Column("id", apr))
        )
        where = And(
            Eq(Column("archive", aph), archive_id),
            Eq(
                Column("status", apr),
                ArchivePublisherRunStatus.SUCCEEDED.value,
            ),
            Lt(Column("date_finished", apr), before_ts),
            Ne(Column("date_finished", apr), None),
        )
        select = Select(
            columns=[Column("id", apr), Column("date_finished", apr)],
            tables=tables,
            where=where,
            order_by=[SQL("apr.date_finished DESC")],
            limit=1,
        )
        row = IStore(ArchivePublisherRun).execute(select).get_one()
        return None if row is None else row

    def _build_archive_reference(
        self,
        purpose: int,
        owner_name: str,
        distribution_name: str,
        archive_name: str,
    ) -> str:
        """Build archive reference from database values."""
        if purpose == ArchivePurpose.PRIMARY.value:
            return "primary"

        purpose_enum = ArchivePurpose.items[purpose]
        template = ARCHIVE_REFERENCE_TEMPLATES[purpose_enum]
        return template % {
            "archive": archive_name,
            "owner": owner_name,
            "distribution": distribution_name,
        }

    def _query_bpph_rows(
        self,
        store,
        archive,
        datecreated_start: Optional[datetime] = None,
        datecreated_end: Optional[datetime] = None,
        datepublished_start: Optional[datetime] = None,
        datepublished_end: Optional[datetime] = None,
        distroseries: Optional[int] = None,
        status: int = PackagePublishingStatus.PUBLISHED.value,
        specific_ids: Optional[List[int]] = None,
    ) -> List[Tuple]:
        # Empty IN () is invalid SQL; callers asking for no IDs want no rows.
        if specific_ids is not None and not specific_ids:
            return []

        # Window is (prev_finished, curr_finished]
        bpph = Alias(Table("binarypackagepublishinghistory"), "bpph")
        bpr = Alias(Table("binarypackagerelease"), "bpr")
        bpn = Alias(Table("binarypackagename"), "bpn")
        das = Alias(Table("distroarchseries"), "das")
        ds = Alias(Table("distroseries"), "ds")
        archive_tbl = Alias(Table("archive"), "archive_tbl")
        component_tbl = Alias(Table("component"), "component_tbl")
        bpf = Alias(Table("binarypackagefile"), "bpf")
        lfa = Alias(Table("libraryfilealias"), "lfa")
        lfc = Alias(Table("libraryfilecontent"), "lfc")
        spn_src = Alias(Table("sourcepackagename"), "spn_src")
        bpb = Alias(Table("binarypackagebuild"), "bpb")
        spr_src = Alias(Table("sourcepackagerelease"), "spr_src")

        # BUILD table joins
        bpph_tables = Join(
            bpph,
            bpr,
            on=Eq(Column("binarypackagerelease", bpph), Column("id", bpr)),
        )
        bpph_tables = Join(
            bpph_tables,
            bpn,
            on=Eq(Column("binarypackagename", bpr), Column("id", bpn)),
        )
        bpph_tables = Join(
            bpph_tables,
            das,
            on=Eq(Column("distroarchseries", bpph), Column("id", das)),
        )
        bpph_tables = Join(
            bpph_tables,
            ds,
            on=Eq(Column("distroseries", das), Column("id", ds)),
        )
        bpph_tables = Join(
            bpph_tables,
            archive_tbl,
            on=Eq(Column("archive", bpph), Column("id", archive_tbl)),
        )
        bpph_tables = Join(
            bpph_tables,
            component_tbl,
            on=Eq(Column("component", bpph), Column("id", component_tbl)),
        )
        bpph_tables = Join(
            bpph_tables,
            bpf,
            on=Eq(
                Column("binarypackagerelease", bpph),
                Column("binarypackagerelease", bpf),
            ),
        )
        bpph_tables = Join(
            bpph_tables,
            lfa,
            on=Eq(Column("libraryfile", bpf), Column("id", lfa)),
        )
        bpph_tables = Join(
            bpph_tables, lfc, on=Eq(Column("content", lfa), Column("id", lfc))
        )
        bpph_tables = LeftJoin(
            bpph_tables,
            spn_src,
            on=Eq(Column("sourcepackagename", bpph), Column("id", spn_src)),
        )
        bpph_tables = LeftJoin(
            bpph_tables, bpb, on=Eq(Column("build", bpr), Column("id", bpb))
        )
        bpph_tables = LeftJoin(
            bpph_tables,
            spr_src,
            on=Eq(
                Column("source_package_release", bpb), Column("id", spr_src)
            ),
        )

        # BUILD WHERE clauses
        if specific_ids is not None:
            ids_sql = ", ".join(str(int(i)) for i in specific_ids)
            bpph_where_clauses = [
                SQL(f"bpph.id IN ({ids_sql})"),
                Eq(Column("filetype", bpf), BinaryPackageFormat.DEB.value),
            ]
        else:
            bpph_where_clauses = [
                Eq(Column("status", bpph), status),
                Eq(
                    Column("filetype", bpf),
                    BinaryPackageFormat.DEB.value,
                ),
            ]
            if datecreated_start is not None:
                bpph_where_clauses.append(
                    Gt(Column("datecreated", bpph), datecreated_start)
                )
            if datecreated_end is not None:
                bpph_where_clauses.append(
                    Le(Column("datecreated", bpph), datecreated_end)
                )
            if datepublished_start is not None:
                bpph_where_clauses.append(
                    Gt(Column("datepublished", bpph), datepublished_start)
                )
            if datepublished_end is not None:
                bpph_where_clauses.append(
                    Le(Column("datepublished", bpph), datepublished_end)
                )
            if distroseries:
                bpph_where_clauses.append(Eq(Column("id", ds), distroseries))

        arch_agg = SQL(
            "string_agg(DISTINCT das.architecturetag, ', ' "
            "ORDER BY das.architecturetag)"
        )
        bpph_id_agg = SQL(
            "string_agg(DISTINCT bpph.id::text, ', ' ORDER BY bpph.id::text)"
        )
        datepublished = SQL("MAX(bpph.datepublished)")

        # Build SELECT columns
        select_columns = [
            Column("name", bpn),  # package_name
            Column("name", component_tbl),  # component
            Column("pocket", bpph),  # pocket
            arch_agg,  # architectures
            Column("name", ds),  # distroseries
            Column("version", bpr),  # version
            Column("id", bpb),  # build id as artifact_id
            Column("name", spn_src),  # sourcepackagename
            Column("version", spr_src),  # sourcepackageversion
            Column("sha256", lfc),  # sha256
            bpph_id_agg,  # bpph ids
            datepublished,  # datepublished (one per group)
        ]

        # Build GROUP BY clause
        group_by_columns = [
            Column("id", bpn),
            Column("id", component_tbl),
            Column("pocket", bpph),
            Column("id", ds),
            Column("id", bpr),
            Column("id", bpb),
            Column("id", spn_src),
            Column("id", spr_src),
            Column("id", lfc),
        ]

        # For manual runs that do not specify an archive, we need to include
        # archive reference components
        if archive is None:
            person_tbl = Alias(Table("person"), "person_owner")
            dist_tbl = Alias(Table("distribution"), "dist_archive")
            bpph_tables = Join(
                bpph_tables,
                person_tbl,
                on=Eq(Column("owner", archive_tbl), Column("id", person_tbl)),
            )
            bpph_tables = Join(
                bpph_tables,
                dist_tbl,
                on=Eq(
                    Column("distribution", archive_tbl), Column("id", dist_tbl)
                ),
            )
            select_columns.extend(
                [
                    Column("purpose", archive_tbl),  # archive purpose
                    Column("name", person_tbl),  # owner name
                    Column("name", dist_tbl),  # distribution name
                    Column("name", archive_tbl),  # archive name
                ]
            )
            group_by_columns.extend(
                [
                    Column("id", archive_tbl),
                    Column("id", person_tbl),
                    Column("id", dist_tbl),
                ]
            )
        else:
            # Or just filter by archive.id if it's specified
            bpph_where_clauses.append(Eq(Column("archive", bpph), archive.id))

        bpph_where = And(*bpph_where_clauses)
        bpph_select = Select(
            columns=select_columns,
            tables=bpph_tables,
            where=bpph_where,
            group_by=group_by_columns,
        )
        return store.execute(bpph_select).get_all()

    def _query_spph_rows(
        self,
        store,
        archive,
        datecreated_start: Optional[datetime] = None,
        datecreated_end: Optional[datetime] = None,
        datepublished_start: Optional[datetime] = None,
        datepublished_end: Optional[datetime] = None,
        distroseries: Optional[int] = None,
        status: int = PackagePublishingStatus.PUBLISHED.value,
        specific_ids: Optional[List[int]] = None,
    ) -> List[Tuple]:
        if specific_ids is not None and not specific_ids:
            return []

        # SPPH aggregation in window.
        spph = Alias(Table("sourcepackagepublishinghistory"), "spph")
        spr = Alias(Table("sourcepackagerelease"), "spr")
        spn = Alias(Table("sourcepackagename"), "spn")
        archive_tbl_s = Alias(Table("archive"), "archive_s")
        component_tbl_s = Alias(Table("component"), "component_s")
        ds_s = Alias(Table("distroseries"), "ds_s")
        sprf = Alias(Table("sourcepackagereleasefile"), "sprf")
        lfa_s = Alias(Table("libraryfilealias"), "lfa_s")
        lfc_s = Alias(Table("libraryfilecontent"), "lfc_s")

        spph_tables = Join(
            spph,
            spr,
            on=Eq(Column("sourcepackagerelease", spph), Column("id", spr)),
        )
        spph_tables = Join(
            spph_tables,
            spn,
            on=Eq(Column("sourcepackagename", spr), Column("id", spn)),
        )
        spph_tables = Join(
            spph_tables,
            archive_tbl_s,
            on=Eq(Column("archive", spph), Column("id", archive_tbl_s)),
        )
        spph_tables = Join(
            spph_tables,
            component_tbl_s,
            on=Eq(Column("component", spph), Column("id", component_tbl_s)),
        )
        spph_tables = Join(
            spph_tables,
            ds_s,
            on=Eq(Column("distroseries", spph), Column("id", ds_s)),
        )
        spph_tables = Join(
            spph_tables,
            sprf,
            on=Eq(
                Column("sourcepackagerelease", spph),
                Column("sourcepackagerelease", sprf),
            ),
        )
        spph_tables = Join(
            spph_tables,
            lfa_s,
            on=Eq(Column("libraryfile", sprf), Column("id", lfa_s)),
        )
        spph_tables = Join(
            spph_tables,
            lfc_s,
            on=Eq(Column("content", lfa_s), Column("id", lfc_s)),
        )

        if specific_ids is not None:
            ids_sql = ", ".join(str(int(i)) for i in specific_ids)
            spph_where_clauses = [
                SQL(f"spph.id IN ({ids_sql})"),
                Eq(
                    Column("filetype", sprf),
                    SourcePackageFileType.DSC.value,
                ),
            ]
        else:
            spph_where_clauses = [
                Eq(Column("status", spph), status),
                Eq(
                    Column("filetype", sprf),
                    SourcePackageFileType.DSC.value,
                ),
            ]
            if datecreated_start is not None:
                spph_where_clauses.append(
                    Gt(Column("datecreated", spph), datecreated_start)
                )
            if datecreated_end is not None:
                spph_where_clauses.append(
                    Le(Column("datecreated", spph), datecreated_end)
                )
            if datepublished_start is not None:
                spph_where_clauses.append(
                    Gt(Column("datepublished", spph), datepublished_start)
                )
            if datepublished_end is not None:
                spph_where_clauses.append(
                    Le(Column("datepublished", spph), datepublished_end)
                )
            if distroseries is not None:
                spph_where_clauses.append(Eq(Column("id", ds_s), distroseries))

        select_columns_s = [
            Column("name", spn),  # package_name
            Column("name", component_tbl_s),  # component
            Column("pocket", spph),  # pocket
            Column("name", ds_s),  # distroseries
            Column("version", spr),  # version
            Column("sha256", lfc_s),  # sha256
            Column("id", spph),  # spph id
            Column("datepublished", spph),  # datepublished
        ]

        # For manual runs that do not specify an archive, we need to include
        # archive reference components
        if archive is None:
            person_tbl_s = Alias(Table("person"), "person_owner_s")
            dist_tbl_s = Alias(Table("distribution"), "dist_archive_s")

            spph_tables = Join(
                spph_tables,
                person_tbl_s,
                on=Eq(
                    Column("owner", archive_tbl_s), Column("id", person_tbl_s)
                ),
            )
            spph_tables = Join(
                spph_tables,
                dist_tbl_s,
                on=Eq(
                    Column("distribution", archive_tbl_s),
                    Column("id", dist_tbl_s),
                ),
            )
            select_columns_s.extend(
                [
                    Column("purpose", archive_tbl_s),  # archive purpose
                    Column("name", person_tbl_s),  # owner name
                    Column("name", dist_tbl_s),  # distribution name
                    Column("name", archive_tbl_s),  # archive name
                ]
            )
        else:
            # Or just filter by archive.id if it's specified
            spph_where_clauses.append(Eq(Column("archive", spph), archive.id))

        spph_where = And(*spph_where_clauses)
        spph_select = Select(
            columns=select_columns_s,
            tables=spph_tables,
            where=spph_where,
        )
        return store.execute(spph_select).get_all()

    def _build_payloads(
        self,
        bpph_rows: List[Tuple],
        spph_rows: List[Tuple],
        distribution_name: str,
        archive_reference: Optional[str],
    ) -> Tuple[Iterator[Tuple[str, Dict[str, Any]]], List[str], List[str]]:
        """Build tagged payloads from BPPH and SPPH rows as a generator.

        Returns (tagged_payload_generator, bpph_ids, spph_ids).
        The generator yields (tracking_id, payload) tuples one at a time.
        """
        bpph_ids = []
        spph_ids = []

        def binary_payload_generator():
            """Generate binary payloads from BPPH rows."""
            for row in bpph_rows:
                # bpph unpack
                (
                    package_name,
                    component,
                    pocket,
                    architectures_csv,
                    distroseries_name,
                    version,
                    build_id,
                    sourcepackagename,
                    sourcepackageversion,
                    sha256,
                    bpph_ids_agg,
                    bpph_datepublished,
                ) = row[:12]
                row_archive_ref = archive_reference

                if archive_reference is None:
                    # archive components unpack
                    (
                        archive_purpose,
                        archive_owner_name,
                        archive_dist_name,
                        archive_name,
                    ) = row[12:]
                    row_archive_ref = self._build_archive_reference(
                        archive_purpose,
                        archive_owner_name,
                        archive_dist_name,
                        archive_name,
                    )

                bpph_ids.append(str(bpph_ids_agg))
                architectures = (
                    [a.strip() for a in architectures_csv.split(",")]
                    if architectures_csv
                    else []
                )
                bpph_released_at = (
                    bpph_datepublished.replace(tzinfo=timezone.utc).isoformat()
                    if bpph_datepublished
                    else None
                )
                binary_payload: Dict[str, Any] = {
                    "release": {
                        "released_at": bpph_released_at,
                        "external_link": None,
                        "properties": {
                            "type": "deb",
                            "id": str(build_id),
                            "name": package_name,
                            "architectures": architectures,
                            "version": version,
                            "sha256": sha256,
                            "archive_base": distribution_name,
                            "archive_reference": row_archive_ref,
                            "archive_series": distroseries_name,
                            "archive_pocket": POCKET_TO_NAME.get(pocket),
                            "archive_component": component,
                        },
                    },
                }
                if sourcepackagename:
                    binary_payload["release"]["properties"][
                        "source_package_name"
                    ] = sourcepackagename
                if sourcepackageversion:
                    binary_payload["release"]["properties"][
                        "source_package_version"
                    ] = sourcepackageversion
                yield f"bpph:{bpph_ids_agg}", binary_payload

        def source_payload_generator():
            """Generate source payloads from SPPH rows."""
            for row in spph_rows:
                # spph unpack
                (
                    package_name,
                    component,
                    pocket,
                    distroseries_name,
                    version,
                    sha256,
                    spph_id,
                    spph_datepublished,
                ) = row[:8]
                row_archive_ref = archive_reference

                if archive_reference is None:
                    # archive components unpack
                    (
                        archive_purpose,
                        archive_owner_name,
                        archive_dist_name,
                        archive_name,
                    ) = row[8:]
                    row_archive_ref = self._build_archive_reference(
                        archive_purpose,
                        archive_owner_name,
                        archive_dist_name,
                        archive_name,
                    )

                spph_ids.append(str(spph_id))
                spph_released_at = (
                    spph_datepublished.replace(tzinfo=timezone.utc).isoformat()
                    if spph_datepublished
                    else None
                )
                source_payload: Dict[str, Any] = {
                    "release": {
                        "released_at": spph_released_at,
                        "external_link": None,
                        "properties": {
                            "type": "deb-source",
                            "name": package_name,
                            "version": version,
                            "architectures": [],
                            "sha256": sha256,
                            "archive_base": distribution_name,
                            "archive_reference": row_archive_ref,
                            "archive_series": distroseries_name,
                            "archive_pocket": POCKET_TO_NAME.get(pocket),
                            "archive_component": component,
                        },
                    },
                }
                yield f"spph:{spph_id}", source_payload

        # Chain both generators together
        payloads = chain(
            binary_payload_generator(), source_payload_generator()
        )

        return payloads, bpph_ids, spph_ids

    def _write_to_csv(
        self, payloads: Iterable[Dict[str, Any]], csv_filename: str
    ) -> int:
        """Write payloads to a CSV file formatted for PostgreSQL import.

        Returns the number of records written.
        """
        with open(csv_filename, "w", newline="", encoding="utf-8") as csvfile:
            fieldnames = [
                "properties",
                "external_link",
                "released_at",
                "publisher",
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            record_count = 0
            for payload in payloads:
                release = payload.get("release", {})

                # Convert properties to JSON string for JSONB column
                properties_json = json.dumps(release.get("properties", {}))

                # Extract other fields
                external_link = release.get("external_link") or ""
                released_at = release.get("released_at", "")

                writer.writerow(
                    {
                        "properties": properties_json,
                        "external_link": external_link,
                        "released_at": released_at,
                        "publisher": "launchpad",
                    }
                )
                record_count += 1

        logger.info(f"Wrote {record_count} records to CSV: {csv_filename}")
        return record_count
