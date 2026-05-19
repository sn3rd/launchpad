#  Copyright 2022 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bug import IBugSet
from lp.bugs.interfaces.bugattachment import BugAttachmentType
from lp.bugs.interfaces.bugtask import BugTaskImportance
from lp.bugs.model.bug import Bug as BugModel
from lp.bugs.model.bugtask import BugTask
from lp.bugs.model.vulnerability import Vulnerability
from lp.bugs.scripts.svthandler import SVTExporter
from lp.bugs.scripts.uct.models import CVE, UCTRecord
from lp.bugs.scripts.uct.subprojects import load_subprojects
from lp.bugs.scripts.uct.uctimport import UCTImporter
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.registry.model.archivesourcepackage import ArchiveSourcePackage
from lp.registry.model.archivesourcepackageseries import (
    ArchiveSourcePackageSeries,
)
from lp.registry.model.distributionsourcepackage import (
    DistributionSourcePackage,
)
from lp.registry.model.product import Product
from lp.registry.model.sourcepackage import SourcePackage
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.security import SecurityAdminDistribution

__all__ = [
    "UCTExporter",
]

TAG_SEPARATOR = UCTImporter.TAG_SEPARATOR
logger = logging.getLogger(__name__)

_CONTRIB_SUBPROJECTS_JSON = (
    Path(__file__).parent.parent.parent.parent.parent
    / "contrib"
    / "subprojects.json"
)


class UCTExporter(SVTExporter):
    """
    `UCTExporter` is used to export LP Bugs, Vulnerabilities and Cve's to
    UCT CVE files.
    """

    class ParsedDescription(NamedTuple):
        description: str
        references: List[str]

    def __init__(self):
        self.subprojects = load_subprojects(_CONTRIB_SUBPROJECTS_JSON)

    def to_record(
        self,
        bug: BugModel,
        vulnerability: Vulnerability,
    ) -> UCTRecord:
        """
        Export the bug and vulnerability related to a cve in a distribution
        and return a `UCTRecord` instance.

        :param bug: `Bug` model
        :param vulnerability: `Vulnerability` model
        :return: `UCTRecord` instance
        """
        if bug is None:
            raise ValueError("Bug can't be None")
        if vulnerability is None:
            raise ValueError("Vulnerability can't be None")

        cve = self._import_cve(bug, vulnerability)
        return cve.to_uct_record(subprojects=self.subprojects)

    def checkUserPermissions(self, user):
        """Only users with security admin permissions to Ubuntu can use
        this handler"""
        ubuntu = getUtility(IDistributionSet).getByName("ubuntu")
        return SecurityAdminDistribution(ubuntu).checkAuthenticated(
            IPersonRoles(user)
        )

    def export_bug_to_uct_file(
        self, bug_id: int, output_dir: Path
    ) -> Optional[Path]:
        """
        Export a bug with the given bug_id as a
        UCT CVE record file in the `output_dir`

        :param bug_id: ID of the `Bug` model to be exported
        :param output_dir: the directory where the exported file will be stored
        :return: path to the exported file
        """
        bug = getUtility(IBugSet).get(bug_id)
        if not bug:
            logger.error("Could not find a bug with ID: %s", bug_id)
            return
        cve = self._make_cve_from_bug(bug)
        uct_record = cve.to_uct_record(subprojects=self.subprojects)
        save_to_path = uct_record.save(output_dir)
        logger.info(
            "Bug with ID: %s is exported to: %s", bug_id, str(save_to_path)
        )
        return save_to_path

    def _make_cve_from_bug(self, bug: BugModel) -> CVE:
        """
        Create a `CVE` instances from a `Bug` model and the related
        Vulnerabilities and `Cve`.

        `BugTasks` are converted to `CVE.DistroPackage` and `CVE.SeriesPackage`
        objects.

        Other `CVE` fields are populated from the information contained in the
        `Bug`, its related Vulnerabilities and LP `Cve` model.

        :param bug: `Bug` model
        :return: `CVE` instance
        """
        vulnerabilities = list(bug.vulnerabilities)
        if not vulnerabilities:
            raise ValueError(
                f"Bug with ID: {bug.id} does not have vulnerabilities"
            )

        vulnerability: Vulnerability = vulnerabilities[0]
        if not vulnerability.cve:
            raise ValueError(
                "Bug with ID: {} - vulnerability "
                "is not linked to a CVE".format(bug.id)
            )

        return self._import_cve(bug, vulnerability)

    def _import_cve(
        self,
        bug: BugModel,
        vulnerability: Vulnerability,
    ) -> CVE:
        """
        Create a `CVE` instances from a `Bug` model and the related
        Vulnerabilities and `Cve`.

        `BugTasks` are converted to `CVE.DistroPackage` and `CVE.SeriesPackage`
        objects.

        Other `CVE` fields are populated from the information contained in the
        `Bug`, its related Vulnerabilities and LP `Cve` model.

        :param bug: `Bug` model to import
        :param vulnerability: `Vulnerability` model
        :return: `CVE` instance
        """

        parsed_description = self._parse_bug_description(bug.description)

        bug_urls = []
        for bug_watch in bug.watches:
            bug_urls.append(bug_watch.url)

        bug_tasks: List[BugTask] = list(bug.bugtasks)

        cve_importance: BugTaskImportance = vulnerability.importance

        tags_by_pkg: Dict[str, Set[str]] = defaultdict(set)
        global_tags: Set[str] = set()
        for tag in bug.tags:
            if TAG_SEPARATOR in tag:
                package_name, tag_value = tag.split(TAG_SEPARATOR, 1)
                tags_by_pkg[package_name].add(tag_value)
            else:
                global_tags.add(tag)

        # When exporting, we shouldn't output the importance value if it
        # hasn't been specified in the original UCT file.
        # So, the following logic is used:
        #  - DistroPackage: export importance only if it's different from
        #  the CVE importance
        #  - SeriesPackage: export importance only if it's different from the
        #  DistroPackage importance
        package_importances: Dict[SourcePackageName, BugTaskImportance] = {}

        # Map products to source package names for upstream tasks
        package_name_by_product: Dict[Product, SourcePackageName] = {}

        # We need to process distribution package tasks before processing
        # series tasks to collect importance value for each package.
        distro_packages: List[CVE.DistroPackage] = []
        ppa_packages: List[CVE.PPAPackage] = []
        for bug_task in bug_tasks:
            target = removeSecurityProxy(bug_task.target)

            # Skip if not a package-level target
            if not isinstance(
                target, (DistributionSourcePackage, ArchiveSourcePackage)
            ):
                continue

            # Handle DistributionSourcePackage
            if isinstance(target, DistributionSourcePackage):
                # This is the `Product` corresponding to the package of this
                # name with the highest version across any of this
                # distribution's series that has a packaging link
                # (it can make a difference if a package name switches to a
                # different upstream project between series)
                product = target.upstream_product
                if product:
                    package_name_by_product[product] = target.sourcepackagename

                importance = bug_task.importance
                package_importances[target.sourcepackagename] = importance

                distro_packages.append(
                    CVE.DistroPackage(
                        target=target,
                        package_name=target.sourcepackagename,
                        importance=(
                            importance
                            if importance != cve_importance
                            else None
                        ),
                        tags=(
                            tags_by_pkg[target.sourcepackagename.name].copy()
                            if target.sourcepackagename.name in tags_by_pkg
                            else set()
                        ),
                    )
                )
                continue

            # Handle ArchiveSourcePackage (PPA packages)
            # For PPA packages, try to find upstream product through the
            # corresponding DistributionSourcePackage (if it exists).
            distro = target.archive.distribution
            dsp = distro.getSourcePackage(target.sourcepackagename)
            if dsp and dsp.upstream_product:
                package_name_by_product[dsp.upstream_product] = (
                    target.sourcepackagename
                )

            importance = bug_task.importance
            package_importances[target.sourcepackagename] = importance

            ppa_packages.append(
                CVE.PPAPackage(
                    target=target,
                    package_name=target.sourcepackagename,
                    importance=(
                        importance if importance != cve_importance else None
                    ),
                    tags=(
                        tags_by_pkg[target.sourcepackagename.name].copy()
                        if target.sourcepackagename.name in tags_by_pkg
                        else set()
                    ),
                )
            )

        # Collect series-level tasks
        series_packages: List[CVE.SeriesPackage] = []
        ppa_series_packages: List[CVE.PPASeriesPackage] = []
        for bug_task in bug_tasks:
            target = removeSecurityProxy(bug_task.target)

            # Skip if not a series-level target
            if not isinstance(
                target, (SourcePackage, ArchiveSourcePackageSeries)
            ):
                continue

            # Handle SourcePackage
            if isinstance(target, SourcePackage):
                importance = bug_task.importance
                package_importance = package_importances.get(
                    target.sourcepackagename
                )
                series_packages.append(
                    CVE.SeriesPackage(
                        target=target,
                        package_name=target.sourcepackagename,
                        importance=(
                            importance
                            if importance != package_importance
                            else None
                        ),
                        status=bug_task.status,
                        status_explanation=bug_task.status_explanation,
                    )
                )
                continue

            # Handle ArchiveSourcePackageSeries (PPA series packages)
            importance = bug_task.importance
            package_importance = package_importances.get(
                target.sourcepackagename
            )
            ppa_series_packages.append(
                CVE.PPASeriesPackage(
                    target=target,
                    package_name=target.sourcepackagename,
                    importance=(
                        importance
                        if importance != package_importance
                        else None
                    ),
                    status=bug_task.status,
                    status_explanation=bug_task.status_explanation,
                )
            )

        # Collect upstream tasks
        upstream_packages: List[CVE.UpstreamPackage] = []
        for bug_task in bug_tasks:
            target = removeSecurityProxy(bug_task.target)
            if not isinstance(target, Product):
                continue
            if target not in package_name_by_product:
                logger.warning(
                    "Could not find a source package for product %s",
                    target.name,
                )
                continue
            package_name = package_name_by_product[target]
            up_importance = bug_task.importance
            package_importance = package_importances.get(package_name)
            upstream_packages.append(
                CVE.UpstreamPackage(
                    target=target,
                    package_name=package_name,
                    importance=(
                        up_importance
                        if up_importance != package_importance
                        else None
                    ),
                    status=bug_task.status,
                    status_explanation=bug_task.status_explanation,
                )
            )

        patch_urls = []
        for attachment in bug.attachments:
            if attachment.url:
                # We should not get an url as we are only using
                # vulnerability_patches
                logger.warning(
                    f"Got {attachment.url} url for {attachment.title} "
                    "attachment"
                )

            if (
                not attachment.vulnerability_patches
                or not attachment.type == BugAttachmentType.PATCH
            ):
                continue

            package_name = getUtility(ISourcePackageNameSet).queryByName(
                attachment.title
            )
            for patch in attachment.vulnerability_patches:
                patch_urls.append(
                    CVE.PatchURL(
                        package_name=package_name,
                        type=patch["name"],
                        url=patch["value"],
                        notes=patch["comment"],
                    )
                )

        break_fix_data = []
        for bugpresence in bug.presences:
            for break_fix in bugpresence.break_fix_data:
                break_fix_data.append(
                    CVE.BreakFix(
                        package_name=bugpresence.source_package_name,
                        broken=break_fix.get("break"),
                        fixed=break_fix.get("fix"),
                    )
                )

        lp_cve = vulnerability.cve

        return CVE(
            sequence=f"CVE-{lp_cve.sequence}",
            date_made_public=vulnerability.date_made_public,
            date_notice_issued=vulnerability.date_notice_issued,
            date_coordinated_release=vulnerability.date_coordinated_release,
            distro_packages=distro_packages,
            series_packages=series_packages,
            upstream_packages=upstream_packages,
            importance=cve_importance,
            importance_explanation=vulnerability.importance_explanation,
            status=vulnerability.status,
            assignee=bug_tasks[0].assignee,
            discovered_by=lp_cve.discovered_by or "",
            description=parsed_description.description,
            ubuntu_description=vulnerability.description,
            bug_urls=bug_urls,
            references=parsed_description.references,
            notes=vulnerability.notes,
            mitigation=vulnerability.mitigation,
            cvss=vulnerability.cvss,
            global_tags=global_tags,
            patch_urls=patch_urls,
            break_fix_data=break_fix_data,
            ppa_packages=ppa_packages,
            ppa_series_packages=ppa_series_packages,
        )

    def _parse_bug_description(
        self, bug_description: str
    ) -> "ParsedDescription":
        """
        Some `CVE` fields can't be mapped to Launchpad models.
        They are saved to bug description.

        This method extracts those fields from the bug description.

        :param bug_description: bug description
        :return: parsed description
        """
        field_values = defaultdict(list)
        current_field = "description"
        known_fields = {
            "References:": "references",
        }
        lines = bug_description.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line in known_fields:
                current_field = known_fields[line]
                continue
            field_values[current_field].append(line)
        return UCTExporter.ParsedDescription(
            description="\n".join(field_values.get("description", [])),
            references=field_values.get("references", []),
        )
