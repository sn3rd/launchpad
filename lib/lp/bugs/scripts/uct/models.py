#  Copyright 2022 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import logging
import re
import tempfile
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
)
from typing.io import TextIO

import dateutil.parser
from contrib.cve_lib import load_cve
from zope.component import getUtility
from zope.schema import URI
from zope.schema.interfaces import InvalidURI

from lp.bugs.enums import VulnerabilityStatus
from lp.bugs.interfaces.bugtask import BugTaskImportance, BugTaskStatus
from lp.bugs.scripts.svthandler import SVTRecord
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.distroseries import IDistroSeriesSet
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.registry.model.distribution import Distribution
from lp.registry.model.distributionsourcepackage import (
    DistributionSourcePackage,
)
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.person import Person
from lp.registry.model.product import Product
from lp.registry.model.sourcepackage import SourcePackage
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.propertycache import cachedproperty

__all__ = [
    "CVE",
    "UCTRecord",
]

logger = logging.getLogger(__name__)


@dataclass
class UCTRecord(SVTRecord):
    """
    UCTRecord represents a single CVE record (file) in the ubuntu-cve-tracker.

    It contains exactly the same information as a UCT CVE record.
    """

    GLOBAL_TAGS_KEY = "*"

    class Priority(Enum):
        CRITICAL = "critical"
        HIGH = "high"
        MEDIUM = "medium"
        LOW = "low"
        UNTRIAGED = "untriaged"
        NEGLIGIBLE = "negligible"

    class PackageStatus(Enum):
        IGNORED = "ignored"
        NEEDS_TRIAGE = "needs-triage"
        DOES_NOT_EXIST = "DNE"
        RELEASED = "released"
        NOT_AFFECTED = "not-affected"
        DEFERRED = "deferred"
        NEEDED = "needed"
        PENDING = "pending"

    class SeriesPackageStatus(NamedTuple):
        series: str
        status: "UCTRecord.PackageStatus"
        reason: str
        priority: Optional["UCTRecord.Priority"]

    class Patch(NamedTuple):
        patch_type: str
        entry: str

    class Package(NamedTuple):
        name: str
        statuses: List["UCTRecord.SeriesPackageStatus"]
        priority: Optional["UCTRecord.Priority"]
        tags: Set[str]
        patches: List["UCTRecord.Patch"]

    parent_dir: str
    assigned_to: str
    bugs: List[str]
    cvss: dict
    candidate: str
    crd: Optional[datetime]
    public_date: Optional[datetime]
    public_date_at_USN: Optional[datetime]
    description: str
    discovered_by: str
    mitigation: Optional[str]
    notes: str
    priority: "UCTRecord.Priority"
    references: List[str]
    ubuntu_description: str
    packages: List["UCTRecord.Package"]
    global_tags: Set[str]
    priority_explanation: str = ""

    def __eq__(self, other):
        if not isinstance(other, UCTRecord):
            raise ValueError("UCTRecord can only be compared to UCTRecord")
        return self.__dict__ == other.__dict__

    @classmethod
    def from_str(self, string: str) -> "UCTRecord":
        with tempfile.NamedTemporaryFile("wb") as fp:
            fp.write(string)
            fp.flush()
            return self.load(Path(fp.name))

    @classmethod
    def load(cls, cve_path: Path) -> "UCTRecord":
        """
        Create a `UCTRecord` instance from a file located at `cve_path`.

        The file is parsed to a dictionary using the code copied from
        `cve_lib` in `ubuntu-cve-tracker`.
        A `UCTRecord` instance is created from that dictionary,
        applying some data transformations along the way.
        """

        cve_data: Dict[str, Any] = load_cve(str(cve_path))

        packages = []
        tags: Dict[str, Set[str]] = cls._pop_cve_property(cve_data, "tags")
        global_tags = tags.pop(cls.GLOBAL_TAGS_KEY, set())

        patches: Dict[str, List[Tuple[str, str]]] = cls._pop_cve_property(
            cve_data, "patches"
        )
        for package, statuses_dict in cls._pop_cve_property(
            cve_data, "pkgs"
        ).items():
            statuses = []
            for series, (status, reason) in statuses_dict.items():
                series_priority = cls._pop_cve_property(
                    cve_data,
                    "Priority_{package}_{series}".format(
                        package=package,
                        series=series,
                    ),
                    required=False,
                )
                statuses.append(
                    cls.SeriesPackageStatus(
                        series=series,
                        status=cls.PackageStatus(status),
                        reason=reason,
                        priority=(
                            cls.Priority(series_priority[0])
                            if series_priority
                            else None
                        ),
                    )
                )
            package_priority = cls._pop_cve_property(
                cve_data,
                f"Priority_{package}",
                required=False,
            )
            packages.append(
                cls.Package(
                    name=package,
                    statuses=statuses,
                    priority=(
                        cls.Priority(package_priority[0])
                        if package_priority
                        else None
                    ),
                    tags=tags.pop(package, set()),
                    patches=[
                        cls.Patch(patch_type=patch_type, entry=entry)
                        for patch_type, entry in patches.pop(package, [])
                    ],
                )
            )

        crd = cls._pop_cve_property(cve_data, "CRD", required=False)
        if crd == "unknown":
            crd = None
        public_date = cls._pop_cve_property(
            cve_data, "PublicDate", required=False
        )
        if public_date == "unknown":
            public_date = None
        public_date_at_USN = cls._pop_cve_property(
            cve_data, "PublicDateAtUSN", required=False
        )
        if public_date_at_USN == "unknown":
            public_date_at_USN = None

        cvss = defaultdict(list)
        for c in cls._pop_cve_property(cve_data, "CVSS"):
            authority = c["source"]
            cvss[authority].append(
                "{} [{} {}]".format(
                    c["vector"],
                    c["baseScore"],
                    c["baseSeverity"],
                )
            )
        cvss = dict(cvss)

        _priority = cls._pop_cve_property(cve_data, "Priority")

        entry = UCTRecord(
            parent_dir=cve_path.absolute().parent.name,
            assigned_to=cls._pop_cve_property(cve_data, "Assigned-to"),
            bugs=cls._pop_cve_property(cve_data, "Bugs").split("\n"),
            cvss=cvss,
            candidate=cls._pop_cve_property(cve_data, "Candidate"),
            crd=dateutil.parser.parse(crd) if crd else None,
            public_date=(
                dateutil.parser.parse(public_date) if public_date else None
            ),
            public_date_at_USN=(
                dateutil.parser.parse(public_date_at_USN)
                if public_date_at_USN
                else None
            ),
            description=cls._pop_cve_property(cve_data, "Description"),
            discovered_by=cls._pop_cve_property(cve_data, "Discovered-by"),
            mitigation=cls._pop_cve_property(
                cve_data, "Mitigation", required=False
            ),
            notes=cls._format_notes(cls._pop_cve_property(cve_data, "Notes")),
            priority=cls.Priority(_priority[0]),
            priority_explanation="\n".join(_priority[1]),
            references=cls._pop_cve_property(cve_data, "References").split(
                "\n"
            ),
            ubuntu_description=cls._pop_cve_property(
                cve_data, "Ubuntu-Description"
            ),
            packages=packages,
            global_tags=global_tags,
        )

        # make sure all fields are consumed
        if cve_data:
            raise AssertionError(f"not all fields are consumed: {cve_data}")

        return entry

    def to_str(self) -> str:
        """
        Export UCTRecord to a str format.
        """
        output = StringIO()
        if self.public_date_at_USN:
            self._write_field(
                "PublicDateAtUSN",
                self._format_datetime(self.public_date_at_USN),
                output,
            )
        self._write_field("Candidate", self.candidate, output)
        if self.crd:
            self._write_field("CRD", self._format_datetime(self.crd), output)
        if self.public_date:
            self._write_field(
                "PublicDate", self._format_datetime(self.public_date), output
            )
        self._write_field("References", self.references, output)
        self._write_field("Description", self.description.split("\n"), output)
        self._write_field(
            "Ubuntu-Description", self.ubuntu_description.split("\n"), output
        )
        self._write_field("Notes", self.notes.split("\n"), output)
        if self.mitigation:
            self._write_field(
                "Mitigation", self.mitigation.split("\n"), output
            )
        self._write_field("Bugs", self.bugs, output)
        self._write_field(
            "Priority",
            self._format_priority(self.priority, self.priority_explanation),
            output,
        )
        self._write_field("Discovered-by", self.discovered_by, output)
        self._write_field("Assigned-to", self.assigned_to, output)
        if self.global_tags:
            self._write_field("Tags", " ".join(self.global_tags), output)
        self._write_field(
            "CVSS",
            [
                f"{authority}: {v}"
                for authority, vectors in self.cvss.items()
                for v in vectors
            ],
            output,
        )
        for package in self.packages:
            output.write("\n")
            patches = [
                f"{patch.patch_type}: {patch.entry}"
                for patch in package.patches
            ]
            self._write_field(f"Patches_{package.name}", patches, output)
            for status in package.statuses:
                self._write_field(
                    f"{status.series}_{package.name}",
                    (
                        f"{status.status.value} ({status.reason})"
                        if status.reason
                        else status.status.value
                    ),
                    output,
                )
            if package.priority:
                self._write_field(
                    f"Priority_{package.name}",
                    package.priority.value,
                    output,
                )
            for status in package.statuses:
                if status.priority:
                    self._write_field(
                        f"Priority_{package.name}_{status.series}",
                        status.priority.value,
                        output,
                    )

            if package.tags:
                self._write_field(
                    f"Tags_{package.name}",
                    " ".join(package.tags),
                    output,
                )

        return output.getvalue()

    def save(self, output_dir: Path) -> Path:
        """
        Save UCTRecord to a file in UCT format.
        """
        if not output_dir.is_dir():
            raise ValueError(
                "{} does not exist or is not a directory", output_dir
            )
        output_path = output_dir / self.parent_dir / self.candidate
        output_path.parent.mkdir(exist_ok=True)
        yaml_content = self.to_str()
        output_path.write_text(yaml_content)
        return output_path

    @classmethod
    def _pop_cve_property(
        cls, cve_data: Dict[str, Any], field_name: str, required=True
    ) -> Optional[Any]:
        if required:
            value = cve_data.pop(field_name)
        else:
            value = cve_data.pop(field_name, None)
        if isinstance(value, str):
            return value.strip()
        return value

    @classmethod
    def _write_field(
        cls, name: str, value: Union[str, List[str]], output: TextIO
    ) -> None:
        if isinstance(value, str):
            if value:
                output.write(f"{name}: {value}\n")
            else:
                output.write(f"{name}:\n")
        elif isinstance(value, list):
            output.write(f"{name}:\n")
            for line in value:
                if line != "":
                    output.write(f" {line}\n")
        else:
            raise AssertionError()

    @classmethod
    def _format_datetime(cls, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    @classmethod
    def _format_notes(cls, notes: List[Tuple[str, str]]) -> str:
        lines = []
        for author, text in notes:
            note_lines = text.split("\n")
            lines.append(f"{author}> {note_lines[0]}")
            for line in note_lines[1:]:
                lines.append("  " + line)
        return "\n".join(lines)

    @classmethod
    def _format_priority(cls, priority: Priority, explanation: str) -> str:
        lines = [priority.value]

        if explanation:
            for line in explanation.split("\n"):
                if line != "":
                    lines.append(" " + line)

        return "\n".join(lines)


class CVE:
    """
    `CVE` represents UCT CVE information mapped to Launchpad data structures.

    Do not confuse this with `Cve` database model.
    """

    # XXX enriqueesanchz 2025-04-21: We add tags to DistroPackage as we have
    # only one DistroPackage per UCTRecord.Package, so we match
    # UCTRecord.Package.tags with CVE.DistroPackage.tags
    class DistroPackage(NamedTuple):
        target: DistributionSourcePackage
        package_name: SourcePackageName
        importance: Optional[BugTaskImportance]
        tags: Set[str]

    class SeriesPackage(NamedTuple):
        target: SourcePackage
        package_name: SourcePackageName
        importance: Optional[BugTaskImportance]
        status: BugTaskStatus
        status_explanation: str

    class UpstreamPackage(NamedTuple):
        target: Product
        package_name: SourcePackageName
        importance: Optional[BugTaskImportance]
        status: BugTaskStatus
        status_explanation: str

    class PatchURL(NamedTuple):
        package_name: SourcePackageName
        type: str
        url: str
        notes: Optional[str]

    class BreakFix(NamedTuple):
        package_name: SourcePackageName
        broken: str
        fixed: str

    # Example:
    # https://github.com/389ds/389-ds-base/commit/123 (1.4.4)
    # https://github.com/389ds/389-ds-base/commit/345
    PATCH_URL_RE = re.compile(r"^(?P<url>.+?)(\s+\((?P<notes>.+)\))?$")

    PRIORITY_MAP = {
        UCTRecord.Priority.CRITICAL: BugTaskImportance.CRITICAL,
        UCTRecord.Priority.HIGH: BugTaskImportance.HIGH,
        UCTRecord.Priority.MEDIUM: BugTaskImportance.MEDIUM,
        UCTRecord.Priority.LOW: BugTaskImportance.LOW,
        UCTRecord.Priority.UNTRIAGED: BugTaskImportance.UNDECIDED,
        UCTRecord.Priority.NEGLIGIBLE: BugTaskImportance.WISHLIST,
    }
    PRIORITY_MAP_REVERSE = {v: k for k, v in PRIORITY_MAP.items()}

    BUG_TASK_STATUS_MAP = {
        UCTRecord.PackageStatus.IGNORED: BugTaskStatus.WONTFIX,
        UCTRecord.PackageStatus.NEEDS_TRIAGE: BugTaskStatus.UNKNOWN,
        UCTRecord.PackageStatus.DOES_NOT_EXIST: BugTaskStatus.DOESNOTEXIST,
        UCTRecord.PackageStatus.RELEASED: BugTaskStatus.FIXRELEASED,
        UCTRecord.PackageStatus.NOT_AFFECTED: BugTaskStatus.INVALID,
        UCTRecord.PackageStatus.DEFERRED: BugTaskStatus.DEFERRED,
        UCTRecord.PackageStatus.NEEDED: BugTaskStatus.NEW,
        UCTRecord.PackageStatus.PENDING: BugTaskStatus.FIXCOMMITTED,
    }
    BUG_TASK_STATUS_MAP_REVERSE = {
        v: k for k, v in BUG_TASK_STATUS_MAP.items()
    }

    VULNERABILITY_STATUS_MAP = {
        "active": VulnerabilityStatus.ACTIVE,
        "ignored": VulnerabilityStatus.IGNORED,
        "retired": VulnerabilityStatus.RETIRED,
    }
    VULNERABILITY_STATUS_MAP_REVERSE = {
        v: k for k, v in VULNERABILITY_STATUS_MAP.items()
    }

    def __init__(
        self,
        sequence: str,
        date_made_public: Optional[datetime],
        date_notice_issued: Optional[datetime],
        date_coordinated_release: Optional[datetime],
        distro_packages: List[DistroPackage],
        series_packages: List[SeriesPackage],
        upstream_packages: List[UpstreamPackage],
        importance: BugTaskImportance,
        status: VulnerabilityStatus,
        assignee: Optional[Person],
        discovered_by: str,
        description: str,
        ubuntu_description: str,
        bug_urls: List[str],
        references: List[str],
        notes: str,
        mitigation: str,
        cvss: dict,
        global_tags: Set[str],
        break_fix_data: List[BreakFix],
        patch_urls: Optional[List[PatchURL]] = None,
        importance_explanation: str = "",
    ):
        self.sequence = sequence
        self.date_made_public = date_made_public
        self.date_notice_issued = date_notice_issued
        self.date_coordinated_release = date_coordinated_release
        self.distro_packages = distro_packages
        self.series_packages = series_packages
        self.upstream_packages = upstream_packages
        self.importance = importance
        self.importance_explanation = importance_explanation
        self.status = status
        self.assignee = assignee
        self.discovered_by = discovered_by
        self.description = description
        self.ubuntu_description = ubuntu_description
        self.bug_urls = bug_urls
        self.references = references
        self.notes = notes
        self.mitigation = mitigation
        self.cvss = cvss
        self.global_tags = global_tags
        self.patch_urls: List[CVE.PatchURL] = patch_urls or []
        self.break_fix_data: List[CVE.BreakFix] = break_fix_data or []

    @classmethod
    def new_cache(cls) -> Dict[str, Dict[Any, Any]]:
        return {
            "distribution": {},
            "distroseries": {},
            "product": {},
        }

    @classmethod
    def _get_from_cache(
        cls, cache_entities: Dict[str, Dict[Any, Any]], bucket: str, key: Any
    ) -> Tuple[bool, Optional[Any]]:
        cache = cache_entities[bucket]
        if key in cache:
            return True, cache[key]
        return False, None

    @classmethod
    def _set_in_cache(
        cls,
        cache_entities: Dict[str, Dict[Any, Any]],
        bucket: str,
        key: Any,
        value: Any,
    ) -> None:
        cache_entities[bucket][key] = value

    @classmethod
    def _get_product_cache_key(
        cls,
        source_package_name: SourcePackageName,
        distribution_name: Optional[str],
    ) -> Tuple[Optional[str], str]:
        # upstream_product is resolved from DistributionSourcePackage, which is
        # distribution-scoped; series is not needed for this cache key.
        return (distribution_name, source_package_name.name)

    @classmethod
    def make_from_uct_record(
        cls,
        uct_record: UCTRecord,
        cache_entities: Optional[Dict[str, Dict[Any, Any]]] = None,
    ) -> "CVE":
        """
        Create a `CVE` from a `UCTRecord`

        This maps UCT CVE information to Launchpad data structures.
        """

        if cache_entities is None:
            cache_entities = cls.new_cache()

        distro_packages = []
        series_packages = []
        patch_urls = []
        break_fix_data = []

        spn_set = getUtility(ISourcePackageNameSet)

        upstream_statuses: Dict[
            SourcePackageName, UCTRecord.SeriesPackageStatus
        ] = OrderedDict()

        for uct_package in uct_record.packages:
            source_package_name = spn_set.getOrCreateByName(uct_package.name)

            patch_urls.extend(
                cls.get_patch_urls(source_package_name, uct_package.patches)
            )

            break_fix_data.extend(
                cls.get_break_fix(source_package_name, uct_package.patches)
            )

            package_importance = (
                cls.PRIORITY_MAP[uct_package.priority]
                if uct_package.priority
                else None
            )

            for uct_package_status in uct_package.statuses:
                if uct_package_status.status not in cls.BUG_TASK_STATUS_MAP:
                    logger.warning(
                        "Can't find a suitable bug task status for %s",
                        uct_package_status.status,
                    )
                    continue

                series_package_importance = (
                    cls.PRIORITY_MAP[uct_package_status.priority]
                    if uct_package_status.priority
                    else None
                )

                if uct_package_status.series == "upstream":
                    upstream_statuses[source_package_name] = uct_package_status
                    continue

                distro_series = cls.get_distro_series(
                    uct_package_status.series, cache_entities=cache_entities
                )
                if distro_series is None:
                    continue

                distro_package = cls.DistroPackage(
                    target=DistributionSourcePackage(
                        distribution=distro_series.distribution,
                        sourcepackagename=source_package_name,
                    ),
                    package_name=source_package_name,
                    importance=package_importance,
                    tags=uct_package.tags,
                )
                if distro_package not in distro_packages:
                    distro_packages.append(distro_package)

                series_packages.append(
                    cls.SeriesPackage(
                        target=SourcePackage(
                            sourcepackagename=source_package_name,
                            distroseries=distro_series,
                        ),
                        package_name=source_package_name,
                        importance=series_package_importance,
                        status=cls.BUG_TASK_STATUS_MAP[
                            uct_package_status.status
                        ],
                        status_explanation=uct_package_status.reason,
                    )
                )

        upstream_packages = []
        for source_package_name, upstream_status in upstream_statuses.items():

            product = None
            for distro_package in distro_packages:
                if source_package_name != distro_package.package_name:
                    continue

                product_cache_key = cls._get_product_cache_key(
                    source_package_name,
                    distro_package.target.distribution.name,
                )
                found, cached_product = cls._get_from_cache(
                    cache_entities,
                    "product",
                    product_cache_key,
                )
                if found:
                    product = cached_product
                else:
                    # This is the `Product` corresponding to the package of
                    # this name with the highest version across any of this
                    # distribution's series that has a packaging link
                    # (it can make a difference if a package name switches to a
                    # different upstream project between series)
                    product = distro_package.target.upstream_product
                    cls._set_in_cache(
                        cache_entities,
                        "product",
                        product_cache_key,
                        product,
                    )

                if product is not None:
                    break

            if product is None:
                logger.warning(
                    "Could not find the product for: %s",
                    source_package_name.name,
                )
                continue

            upstream_packages.append(
                cls.UpstreamPackage(
                    target=product,
                    package_name=source_package_name,
                    importance=(
                        cls.PRIORITY_MAP[upstream_status.priority]
                        if upstream_status.priority
                        else None
                    ),
                    status=cls.BUG_TASK_STATUS_MAP[upstream_status.status],
                    status_explanation=upstream_status.reason,
                )
            )

        if uct_record.assigned_to:
            assignee = getUtility(IPersonSet).getByName(uct_record.assigned_to)
            if not assignee:
                logger.warning(
                    "Could not find the assignee: %s", uct_record.assigned_to
                )
        else:
            assignee = None

        return cls(
            sequence=uct_record.candidate,
            date_made_public=uct_record.public_date,
            date_notice_issued=uct_record.public_date_at_USN,
            date_coordinated_release=uct_record.crd,
            distro_packages=distro_packages,
            series_packages=series_packages,
            upstream_packages=upstream_packages,
            importance=cls.PRIORITY_MAP[uct_record.priority],
            importance_explanation=uct_record.priority_explanation,
            status=cls.infer_vulnerability_status(uct_record),
            assignee=assignee,
            discovered_by=uct_record.discovered_by,
            description=uct_record.description,
            ubuntu_description=uct_record.ubuntu_description,
            bug_urls=uct_record.bugs,
            references=uct_record.references,
            notes=uct_record.notes,
            mitigation=uct_record.mitigation,
            cvss=uct_record.cvss,
            global_tags=uct_record.global_tags,
            patch_urls=patch_urls,
            break_fix_data=break_fix_data,
        )

    def to_uct_record(self) -> UCTRecord:
        """
        Convert a `CVE` to a `UCTRecord`.

        This maps Launchpad data structures to the format that UCT understands.
        """
        series_packages_by_name: Dict[
            SourcePackageName, List[CVE.SeriesPackage]
        ] = defaultdict(list)
        for series_package in self.series_packages:
            series_packages_by_name[series_package.package_name].append(
                series_package
            )

        packages_by_name: Dict[str, UCTRecord.Package] = OrderedDict()
        processed_packages: Set[SourcePackageName] = set()
        for distro_package in self.distro_packages:
            spn = distro_package.package_name
            if spn in processed_packages:
                continue
            processed_packages.add(spn)
            statuses: List[UCTRecord.SeriesPackageStatus] = []
            for series_package in series_packages_by_name[spn]:
                series = series_package.target.distroseries
                if series.status == SeriesStatus.DEVELOPMENT:
                    series_name = "devel"
                else:
                    series_name = series.name
                distro_name = distro_package.target.distribution.name
                if distro_name != "ubuntu":
                    if distro_name == "ubuntu-esm":
                        distro_name = "esm"
                    series_name = f"{series_name}/{distro_name}"
                statuses.append(
                    UCTRecord.SeriesPackageStatus(
                        series=series_name,
                        status=self.BUG_TASK_STATUS_MAP_REVERSE[
                            series_package.status
                        ],
                        reason=series_package.status_explanation,
                        priority=(
                            self.PRIORITY_MAP_REVERSE[
                                series_package.importance
                            ]
                            if series_package.importance
                            else None
                        ),
                    )
                )

            packages_by_name[spn.name] = UCTRecord.Package(
                name=spn.name,
                statuses=statuses,
                priority=(
                    self.PRIORITY_MAP_REVERSE[distro_package.importance]
                    if distro_package.importance
                    else None
                ),
                tags=distro_package.tags,
                patches=[],
            )

        for upstream_package in self.upstream_packages:
            status = UCTRecord.SeriesPackageStatus(
                series="upstream",
                status=self.BUG_TASK_STATUS_MAP_REVERSE[
                    upstream_package.status
                ],
                reason=upstream_package.status_explanation,
                priority=(
                    self.PRIORITY_MAP_REVERSE[upstream_package.importance]
                    if upstream_package.importance
                    else None
                ),
            )
            package_name = upstream_package.package_name.name
            if package_name in packages_by_name:
                packages_by_name[package_name].statuses.append(status)
            else:
                packages_by_name[package_name] = UCTRecord.Package(
                    name=package_name,
                    statuses=[status],
                    priority=None,
                    tags=set(),
                    patches=[],
                )

        for patch_url in self.patch_urls:
            entry = patch_url.url
            if patch_url.notes:
                entry = f"{entry} ({patch_url.notes})"
            packages_by_name[patch_url.package_name.name].patches.append(
                UCTRecord.Patch(
                    patch_type=patch_url.type,
                    entry=entry,
                )
            )

        for break_fix in self.break_fix_data:
            packages_by_name[break_fix.package_name.name].patches.append(
                UCTRecord.Patch(
                    patch_type="break-fix",
                    entry=f"{break_fix.broken} {break_fix.fixed}",
                )
            )

        return UCTRecord(
            parent_dir=self.VULNERABILITY_STATUS_MAP_REVERSE.get(
                self.status, ""
            ),
            assigned_to=self.assignee.name if self.assignee else "",
            bugs=self.bug_urls,
            cvss=self.cvss,
            candidate=self.sequence,
            crd=self.date_coordinated_release,
            public_date=self.date_made_public,
            public_date_at_USN=self.date_notice_issued,
            description=self.description,
            discovered_by=self.discovered_by,
            mitigation=self.mitigation,
            notes=self.notes,
            priority=self.PRIORITY_MAP_REVERSE[self.importance],
            priority_explanation=self.importance_explanation,
            references=self.references,
            ubuntu_description=self.ubuntu_description,
            global_tags=self.global_tags,
            packages=list(packages_by_name.values()),
        )

    @cachedproperty
    def affected_distributions(self) -> Set[Distribution]:
        return {p.target.distribution for p in self.distro_packages}

    @cachedproperty
    def affected_distro_series(self) -> Set[DistroSeries]:
        return {p.target.distroseries for p in self.series_packages}

    @classmethod
    def infer_vulnerability_status(
        cls, uct_record: UCTRecord
    ) -> VulnerabilityStatus:
        """
        Infer vulnerability status based on the parent folder of the CVE file.
        """
        return cls.VULNERABILITY_STATUS_MAP.get(
            uct_record.parent_dir, VulnerabilityStatus.NEEDS_TRIAGE
        )

    @classmethod
    def get_devel_series(
        cls, distribution: Distribution
    ) -> Optional[DistroSeries]:
        for series in distribution.series:
            if series.status == SeriesStatus.FROZEN:
                return series
        for series in distribution.series:
            if series.status == SeriesStatus.DEVELOPMENT:
                return series

    @classmethod
    def get_distro_series(
        cls,
        distro_series_name: str,
        cache_entities: Optional[Dict[str, Dict[Any, Any]]] = None,
    ) -> Optional[DistroSeries]:

        if cache_entities is None:
            cache_entities = cls.new_cache()

        # Standardize distro series name
        if "/" in distro_series_name:
            if distro_series_name.startswith("esm-"):
                distro_name = "ubuntu-esm"
                series_name = distro_series_name.split("/", 1)[1]
            elif distro_series_name.endswith("/esm"):
                distro_name = "ubuntu-esm"
                series_name = distro_series_name.split("/", 1)[0]
            else:
                series_name, distro_name = distro_series_name.split("/", 1)
        else:
            distro_name = "ubuntu"
            series_name = distro_series_name

        # Try get distro series from cache.
        distro_series_key = f"{distro_name}/{series_name}"
        found, distro_series = cls._get_from_cache(
            cache_entities, "distroseries", distro_series_key
        )
        if found:
            return distro_series

        found, distribution = cls._get_from_cache(
            cache_entities, "distribution", distro_name
        )
        if found and distribution is None:
            return None
        elif not found:
            distribution = getUtility(IDistributionSet).getByName(distro_name)
            cls._set_in_cache(
                cache_entities,
                "distribution",
                distro_name,
                distribution,
            )

        if distribution is None:
            logger.warning("Could not find the distribution: %s", distro_name)
            cls._set_in_cache(
                cache_entities,
                "distroseries",
                distro_series_key,
                None,
            )
            return None

        if series_name == "devel":
            distro_series = cls.get_devel_series(distribution)
        else:
            distro_series = getUtility(IDistroSeriesSet).queryByName(
                distribution, series_name
            )
        if not distro_series:
            logger.warning(
                "Could not find the distro series: %s",
                distro_series_name,
            )

        cls._set_in_cache(
            cache_entities,
            "distroseries",
            distro_series_key,
            distro_series,
        )
        return distro_series

    @classmethod
    def get_patch_urls(
        cls,
        source_package_name: SourcePackageName,
        patches: List[UCTRecord.Patch],
    ) -> Iterable[PatchURL]:
        for patch in patches:
            if patch.patch_type == "break-fix":
                continue
            match = cls.PATCH_URL_RE.match(patch.entry)
            if not match:
                logger.warning(
                    "Could not parse the patch entry: %s", patch.entry
                )
                continue

            try:
                url = URI().fromUnicode(match.groupdict()["url"])
            except InvalidURI:
                logger.error("Invalid patch URL: %s", patch.entry)
                continue

            notes = match.groupdict().get("notes")
            yield cls.PatchURL(
                package_name=source_package_name,
                type=patch.patch_type,
                url=url,
                notes=notes,
            )

    @classmethod
    def get_break_fix(
        cls,
        source_package_name: SourcePackageName,
        patches: List[UCTRecord.Patch],
    ) -> Iterable[PatchURL]:
        for patch in patches:
            if patch.patch_type != "break-fix":
                continue

            # patch.entry will always have 2 fields separated by 1 space
            # <break> <fix>
            if " " not in patch.entry:
                logger.warning(
                    "Could not parse the break-fix patch entry: %s",
                    patch.entry,
                )
                continue

            broken, fixed = patch.entry.split(maxsplit=1)
            yield cls.BreakFix(
                package_name=source_package_name,
                broken=broken,
                fixed=fixed,
            )
