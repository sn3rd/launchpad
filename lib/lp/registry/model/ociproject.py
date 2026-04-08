# Copyright 2019-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""OCI Project implementation."""

__all__ = [
    "OCIProject",
    "OCIProjectSet",
]

from collections import defaultdict
from datetime import timezone

from storm.databases.postgres import JSON
from storm.expr import Join, LeftJoin, Or
from storm.locals import Bool, DateTime, Int, Reference, Unicode
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import (
    PRIVATE_INFORMATION_TYPES,
    PUBLIC_INFORMATION_TYPES,
    ServiceUsage,
)
from lp.app.interfaces.services import IService
from lp.bugs.model.bugtarget import BugTargetBase
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.code.model.branchnamespace import (
    BRANCH_POLICY_ALLOWED_TYPES,
    BRANCH_POLICY_REQUIRED_GRANTS,
)
from lp.oci.interfaces.ocirecipe import IOCIRecipeSet
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.ociproject import (
    CannotDeleteOCIProject,
    IOCIProject,
    IOCIProjectSet,
    OCIProjectRecipeInvalid,
)
from lp.registry.interfaces.ociprojectname import IOCIProjectNameSet
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.model.accesspolicy import reconcile_access_for_artifacts
from lp.registry.model.ociprojectname import OCIProjectName
from lp.registry.model.ociprojectseries import OCIProjectSeries
from lp.registry.model.person import Person
from lp.services.database.bulk import load_related
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase


def oci_project_modified(oci_project, event):
    """Update the date_last_modified property when an OCIProject is modified.

    This method is registered as a subscriber to `IObjectModifiedEvent`
    events on OCI projects.
    """
    # This attribute is normally read-only; bypass the security proxy to
    # avoid that.
    removeSecurityProxy(oci_project).date_last_modified = UTC_NOW


def user_has_special_oci_access(user):
    """Admins have special access.

    :param user: An `IPerson` or None.
    """
    if user is None:
        return False
    roles = IPersonRoles(user)
    return roles.in_admin


@implementer(IOCIProject)
class OCIProject(BugTargetBase, StructuralSubscriptionTargetMixin, StormBase):
    """See `IOCIProject` and `IOCIProjectSet`."""

    __storm_table__ = "OCIProject"

    id = Int(primary=True)
    date_created = DateTime(
        name="date_created", tzinfo=timezone.utc, allow_none=False
    )
    date_last_modified = DateTime(
        name="date_last_modified", tzinfo=timezone.utc, allow_none=False
    )

    registrant_id = Int(name="registrant", allow_none=False)
    registrant = Reference(registrant_id, "Person.id")

    distribution_id = Int(name="distribution", allow_none=True)
    distribution = Reference(distribution_id, "Distribution.id")

    project_id = Int(name="project", allow_none=True)
    project = Reference(project_id, "Product.id")

    ociprojectname_id = Int(name="ociprojectname", allow_none=False)
    ociprojectname = Reference(ociprojectname_id, "OCIProjectName.id")

    description = Unicode(name="description")

    bug_reporting_guidelines = Unicode(name="bug_reporting_guidelines")
    content_templates = JSON(name="content_templates", default=None)
    bug_reported_acknowledgement = Unicode(name="bug_reported_acknowledgement")
    enable_bugfiling_duplicate_search = Bool(
        name="enable_bugfiling_duplicate_search"
    )

    answers_usage = ServiceUsage.NOT_APPLICABLE
    blueprints_usage = ServiceUsage.NOT_APPLICABLE
    codehosting_usage = ServiceUsage.NOT_APPLICABLE
    translations_usage = ServiceUsage.NOT_APPLICABLE
    bug_tracking_usage = ServiceUsage.LAUNCHPAD
    uses_launchpad = True

    @property
    def name(self):
        return self.ociprojectname.name

    @name.setter
    def name(self, value):
        self.ociprojectname = getUtility(IOCIProjectNameSet).getOrCreateByName(
            value
        )

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        return self.project if self.project_id else self.distribution

    @property
    def pillar(self):
        """See `IBugTarget`."""
        return self.project if self.project_id else self.distribution

    @pillar.setter
    def pillar(self, pillar):
        """See `IBugTarget`."""
        # We need to reconcile access for all OCI recipes from this OCI
        # project if we are moving from one pillar to another.
        needs_reconcile_access = (
            self.pillar is not None and self.pillar != pillar
        )
        if IDistribution.providedBy(pillar):
            self.distribution = pillar
            self.project = None
        elif IProduct.providedBy(pillar):
            self.project = pillar
            self.distribution = None
        else:
            raise ValueError(
                "The target of an OCIProject must be either an IDistribution "
                "or IProduct instance."
            )
        if needs_reconcile_access:
            self._reconcileAccess()

    @property
    def display_name(self):
        """See `IOCIProject`."""
        return "OCI project %s for %s" % (
            self.ociprojectname.name,
            self.pillar.display_name,
        )

    @property
    def displayname(self):
        return "%s (%s)" % (self.name, self.pillar.display_name)

    bugtargetname = displayname
    bugtargetdisplayname = displayname
    title = displayname

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this OCI project."""
        search_params.setOCIProject(self)

    @property
    def driver(self):
        """See `IOCIProject`."""
        return self.pillar.driver

    @property
    def bug_supervisor(self):
        """See `IOCIProject`."""
        return self.pillar.bug_supervisor

    def getAllowedBugInformationTypes(self):
        """See `IOCIProject.`"""
        return self.pillar.getAllowedBugInformationTypes()

    def getBugSummaryContextWhereClause(self):
        """See BugTargetBase."""
        # Circular fail.
        from lp.bugs.model.bugsummary import BugSummary

        return BugSummary.ociproject_id == self.id

    def _getOfficialTagClause(self):
        return self.pillar._getOfficialTagClause()

    @property
    def official_bug_tags(self):
        return self.pillar.official_bug_tags

    def _reconcileAccess(self):
        """Reconcile access for all OCI recipes of this project."""
        from lp.oci.model.ocirecipe import OCIRecipe

        rs = IStore(OCIRecipe).find(OCIRecipe, OCIRecipe.oci_project == self)
        recipes_per_info_type = defaultdict(set)
        for recipe in rs:
            recipes_per_info_type[recipe.information_type].add(recipe)
        for information_type, recipes in recipes_per_info_type.items():
            reconcile_access_for_artifacts(
                recipes, information_type, [self.pillar]
            )

    def newRecipe(
        self,
        name,
        registrant,
        owner,
        git_ref,
        build_file,
        description=None,
        build_daily=False,
        require_virtualized=True,
        build_args=None,
    ):
        return getUtility(IOCIRecipeSet).new(
            name=name,
            registrant=registrant,
            owner=owner,
            oci_project=self,
            git_ref=git_ref,
            build_file=build_file,
            build_args=build_args,
            description=description,
            require_virtualized=require_virtualized,
            build_daily=build_daily,
        )

    def newSeries(
        self,
        name,
        summary,
        registrant,
        status=SeriesStatus.DEVELOPMENT,
        date_created=DEFAULT,
    ):
        """See `IOCIProject`."""
        series = OCIProjectSeries(
            oci_project=self,
            name=name,
            summary=summary,
            registrant=registrant,
            status=status,
        )
        return series

    @property
    def series(self):
        """See `IOCIProject`."""
        ret = (
            IStore(OCIProjectSeries)
            .find(OCIProjectSeries, OCIProjectSeries.oci_project == self)
            .order_by(OCIProjectSeries.date_created)
        )
        return ret

    def getSeriesByName(self, name):
        return self.series.find(OCIProjectSeries.name == name).one()

    def getRecipes(self, visible_by_user=None):
        """See `IOCIProject`."""
        from lp.oci.model.ocirecipe import (
            OCIRecipe,
            get_ocirecipe_privacy_filter,
        )

        rs = IStore(OCIRecipe).find(
            OCIRecipe,
            OCIRecipe.owner_id == Person.id,
            OCIRecipe.oci_project == self,
            get_ocirecipe_privacy_filter(visible_by_user),
        )
        return rs.order_by(Person.name, OCIRecipe.name)

    def getRecipeByNameAndOwner(
        self, recipe_name, owner_name, visible_by_user=None
    ):
        """See `IOCIProject`."""
        from lp.oci.model.ocirecipe import OCIRecipe

        q = self.getRecipes(visible_by_user=visible_by_user).find(
            OCIRecipe.name == recipe_name, Person.name == owner_name
        )
        return q.one()

    def searchRecipes(self, query, visible_by_user=None):
        """See `IOCIProject`."""
        from lp.oci.model.ocirecipe import OCIRecipe

        q = self.getRecipes(visible_by_user=visible_by_user).find(
            OCIRecipe.name.contains_string(query)
            | Person.name.contains_string(query)
        )
        return q.order_by(Person.name, OCIRecipe.name)

    def getOfficialRecipes(self, visible_by_user=None):
        """See `IOCIProject`."""
        from lp.oci.model.ocirecipe import OCIRecipe

        return self.getRecipes(visible_by_user=visible_by_user).find(
            OCIRecipe._official == True
        )

    def getUnofficialRecipes(self, visible_by_user=None):
        """See `IOCIProject`."""
        from lp.oci.model.ocirecipe import OCIRecipe

        return self.getRecipes(visible_by_user=visible_by_user).find(
            OCIRecipe._official == False
        )

    def setOfficialRecipeStatus(self, recipe, status):
        """See `IOCIProject`."""
        if recipe is not None and recipe.oci_project != self:
            raise OCIProjectRecipeInvalid()
        # Removing security proxy here because `_official` is a private
        # attribute not declared on the Interface, and we need to set it
        # regardless of security checks on OCIRecipe objects.
        recipe = removeSecurityProxy(recipe)
        recipe._official = status

    def getAllowedInformationTypes(self, user):
        """See `IOCIRecipe`."""
        if user_has_special_oci_access(user):
            # Admins can set any type.
            return set(PUBLIC_INFORMATION_TYPES + PRIVATE_INFORMATION_TYPES)
        required_grant = BRANCH_POLICY_REQUIRED_GRANTS[
            self.pillar.branch_sharing_policy
        ]
        if (
            required_grant is not None
            and not getUtility(IService, "sharing").checkPillarAccess(
                [self.pillar], required_grant, self.registrant
            )
            and (
                user is None
                or not getUtility(IService, "sharing").checkPillarAccess(
                    [self.pillar], required_grant, user
                )
            )
        ):
            return []
        return BRANCH_POLICY_ALLOWED_TYPES[self.pillar.branch_sharing_policy]

    def destroySelf(self):
        """See `IOCIProject`."""
        from lp.bugs.model.bugtask import BugTask
        from lp.code.model.gitrepository import GitRepository
        from lp.oci.model.ocirecipe import OCIRecipe

        # Cannot delete this OCI project if it has recipes associated if it.
        exists_recipes = (
            not IStore(OCIRecipe)
            .find(OCIRecipe, OCIRecipe.oci_project == self)
            .is_empty()
        )
        if exists_recipes:
            raise CannotDeleteOCIProject("This OCI project contains recipes.")

        # Cannot delete this OCI project if it has bugs associated with it.
        # XXX pappacena 2021-04-28: BugTask table has a
        # BugTask.ociprojectseries column, but it's not mapped to the
        # model yet since we do not currently support bugs associated to
        # OCIProjectSeries. Once we have support for that, this query
        # condition should be changed to something like:
        # Or(BugTask.ocirproject == self,
        #    BugTask.ociprojectseries.is_in(self.series)).
        exists_bugs = (
            not IStore(BugTask)
            .find(BugTask, BugTask.ociproject == self)
            .is_empty()
        )
        if exists_bugs:
            raise CannotDeleteOCIProject("This OCI project contains bugs.")

        # Cannot delete this OCI project if it has repos associated with it.
        exists_repos = (
            not IStore(GitRepository)
            .find(GitRepository, GitRepository.oci_project == self)
            .is_empty()
        )
        if exists_repos:
            raise CannotDeleteOCIProject(
                "There are git repositories associated with this OCI project."
            )
        for series in self.series:
            series.destroySelf()
        IStore(self).remove(self)


@implementer(IOCIProjectSet)
class OCIProjectSet:
    def new(
        self,
        registrant,
        pillar,
        name,
        date_created=DEFAULT,
        description=None,
        bug_reporting_guidelines=None,
        content_templates=None,
        bug_reported_acknowledgement=None,
        bugfiling_duplicate_search=False,
    ):
        """See `IOCIProjectSet`."""
        if isinstance(name, str):
            name = getUtility(IOCIProjectNameSet).getOrCreateByName(name)
        store = IPrimaryStore(OCIProject)
        target = OCIProject()
        target.date_created = date_created
        target.date_last_modified = date_created
        target.pillar = pillar
        target.registrant = registrant
        target.ociprojectname = name
        target.description = description
        target.bug_reporting_guidelines = bug_reporting_guidelines
        target.content_templates = content_templates
        target.bug_reported_acknowledgement = bug_reported_acknowledgement
        target.enable_bugfiling_duplicate_search = bugfiling_duplicate_search
        store.add(target)
        return target

    def _get_pillar_attribute(self, pillar):
        """Checks if the provided pillar is a valid one for OCIProject,
        returning the model attribute where this pillar would be stored.

        If pillar is not valid, raises ValueError.

        :param pillar: A Distribution or Product.
        :return: Storm attribute where the pillar would be stored.
                 If pillar is not valid, raises ValueError.
        """
        if IDistribution.providedBy(pillar):
            return OCIProject.distribution
        elif IProduct.providedBy(pillar):
            return OCIProject.project
        else:
            raise ValueError(
                "The target of an OCIProject must be either an "
                "IDistribution or an IProduct instance."
            )

    def getByPillarAndName(self, pillar, name):
        """See `IOCIProjectSet`."""
        from lp.registry.model.distribution import Distribution
        from lp.registry.model.product import Product

        # If pillar is not an string, we expect it to be either an
        # IDistribution or IProduct.
        if not isinstance(pillar, str):
            return (
                IStore(OCIProject)
                .find(
                    OCIProject,
                    self._get_pillar_attribute(pillar) == pillar,
                    OCIProject.ociprojectname == OCIProjectName.id,
                    OCIProjectName.name == name,
                )
                .one()
            )
        else:
            # If we got a pillar name instead, we need to join with both
            # Distribution and Product tables, to find out which one has the
            # provided name.
            tables = [
                OCIProject,
                Join(
                    OCIProjectName,
                    OCIProject.ociprojectname == OCIProjectName.id,
                ),
                LeftJoin(
                    Distribution, OCIProject.distribution == Distribution.id
                ),
                LeftJoin(Product, OCIProject.project == Product.id),
            ]
            return (
                IStore(OCIProject)
                .using(*tables)
                .find(
                    OCIProject,
                    Or(Distribution.name == pillar, Product.name == pillar),
                    OCIProjectName.name == name,
                )
                .one()
            )

    def findByPillarAndName(self, pillar, name_substring):
        """See `IOCIProjectSet`."""
        return IStore(OCIProject).find(
            OCIProject,
            self._get_pillar_attribute(pillar) == pillar,
            OCIProject.ociprojectname == OCIProjectName.id,
            OCIProjectName.name.contains_string(name_substring),
        )

    def searchByName(self, name_substring):
        return IStore(OCIProject).find(
            OCIProject,
            OCIProject.ociprojectname == OCIProjectName.id,
            OCIProjectName.name.contains_string(name_substring),
        )

    def preloadDataForOCIProjects(self, oci_projects):
        """See `IOCIProjectSet`."""
        oci_projects = [removeSecurityProxy(i) for i in oci_projects]

        person_ids = [i.registrant_id for i in oci_projects]
        list(
            getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                person_ids, need_validity=True
            )
        )

        load_related(OCIProjectName, oci_projects, ["ociprojectname_id"])
