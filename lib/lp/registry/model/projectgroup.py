# Copyright 2009-2013 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Launchpad ProjectGroup-related Database Table Objects."""

__all__ = [
    "ProjectGroup",
    "ProjectGroupSeries",
    "ProjectGroupSet",
]

from datetime import timezone

from storm.databases.postgres import JSON
from storm.expr import And, Desc, Func, In, Is, Join, Min
from storm.locals import Bool, DateTime, Int, Reference, Unicode
from storm.store import Store
from zope.component import getUtility
from zope.interface import implementer

from lp.answers.enums import QUESTION_STATUS_DEFAULT_SEARCH
from lp.answers.interfaces.faqcollection import IFAQCollection
from lp.answers.interfaces.questioncollection import ISearchableByQuestionOwner
from lp.answers.model.faq import FAQ, FAQSearch
from lp.answers.model.question import Question, QuestionTargetSearch
from lp.app.enums import ServiceUsage
from lp.app.errors import NotFoundError
from lp.blueprints.enums import SprintSpecificationStatus
from lp.blueprints.model.specification import (
    HasSpecificationsMixin,
    Specification,
)
from lp.blueprints.model.specificationsearch import search_specifications
from lp.blueprints.model.sprint import HasSprintsMixin, Sprint
from lp.blueprints.model.sprintspecification import SprintSpecification
from lp.bugs.interfaces.bugsummary import IBugSummaryDimension
from lp.bugs.model.bugtarget import BugTargetBase, OfficialBugTag
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.code.model.hasbranches import HasBranchesMixin, HasMergeProposalsMixin
from lp.registry.interfaces.person import (
    validate_person_or_closed_team,
    validate_public_person,
)
from lp.registry.interfaces.pillar import IPillarNameSet
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.projectgroup import (
    IProjectGroup,
    IProjectGroupSeries,
    IProjectGroupSet,
)
from lp.registry.model.announcement import MakesAnnouncements
from lp.registry.model.hasdrivers import HasDriversMixin
from lp.registry.model.karma import KarmaContextMixin
from lp.registry.model.milestone import (
    HasMilestonesMixin,
    Milestone,
    ProjectMilestone,
)
from lp.registry.model.pillar import HasAliasMixin
from lp.registry.model.product import Product, ProductSet
from lp.registry.model.productseries import ProductSeries
from lp.services.database.constants import UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import fti_search
from lp.services.helpers import shortlist
from lp.services.propertycache import cachedproperty
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.worlddata.model.language import Language
from lp.translations.enums import TranslationPermission
from lp.translations.model.potemplate import POTemplate
from lp.translations.model.translationpolicy import TranslationPolicyMixin


@implementer(
    IBugSummaryDimension,
    IProjectGroup,
    IFAQCollection,
    ISearchableByQuestionOwner,
)
class ProjectGroup(
    StormBase,
    BugTargetBase,
    HasSpecificationsMixin,
    MakesAnnouncements,
    HasSprintsMixin,
    HasAliasMixin,
    KarmaContextMixin,
    StructuralSubscriptionTargetMixin,
    HasBranchesMixin,
    HasMergeProposalsMixin,
    HasMilestonesMixin,
    HasDriversMixin,
    TranslationPolicyMixin,
):
    """A ProjectGroup"""

    __storm_table__ = "Project"

    # db field names
    id = Int(primary=True)
    owner_id = Int(
        name="owner",
        validator=validate_person_or_closed_team,
        allow_none=False,
    )
    owner = Reference(owner_id, "Person.id")
    registrant_id = Int(
        name="registrant", validator=validate_public_person, allow_none=False
    )
    registrant = Reference(registrant_id, "Person.id")
    name = Unicode(name="name", allow_none=False)
    display_name = Unicode(name="displayname", allow_none=False)
    _title = Unicode(name="title", allow_none=False)
    summary = Unicode(name="summary", allow_none=False)
    description = Unicode(name="description", allow_none=False)
    datecreated = DateTime(
        name="datecreated",
        allow_none=False,
        default=UTC_NOW,
        tzinfo=timezone.utc,
    )
    driver_id = Int(
        name="driver",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    driver = Reference(driver_id, "Person.id")
    homepageurl = Unicode(name="homepageurl", allow_none=True, default=None)
    homepage_content = Unicode(default=None)
    icon_id = Int(name="icon", allow_none=True, default=None)
    icon = Reference(icon_id, "LibraryFileAlias.id")
    logo_id = Int(name="logo", allow_none=True, default=None)
    logo = Reference(logo_id, "LibraryFileAlias.id")
    mugshot_id = Int(name="mugshot", allow_none=True, default=None)
    mugshot = Reference(mugshot_id, "LibraryFileAlias.id")
    wikiurl = Unicode(name="wikiurl", allow_none=True, default=None)
    sourceforgeproject = Unicode(
        name="sourceforgeproject", allow_none=True, default=None
    )
    freshmeatproject = None
    lastdoap = Unicode(name="lastdoap", allow_none=True, default=None)
    translationgroup_id = Int(
        name="translationgroup", allow_none=True, default=None
    )
    translationgroup = Reference(translationgroup_id, "TranslationGroup.id")
    translationpermission = DBEnum(
        name="translationpermission",
        allow_none=False,
        enum=TranslationPermission,
        default=TranslationPermission.OPEN,
    )
    active = Bool(name="active", allow_none=False, default=True)
    reviewed = Bool(name="reviewed", allow_none=False, default=False)
    bugtracker_id = Int(name="bugtracker", allow_none=True, default=None)
    bugtracker = Reference(bugtracker_id, "BugTracker.id")
    bug_reporting_guidelines = Unicode(default=None)
    content_templates = JSON(default=None)
    bug_reported_acknowledgement = Unicode(default=None)

    def __init__(
        self,
        owner,
        registrant,
        name,
        display_name,
        title,
        summary,
        description,
        homepageurl=None,
        icon=None,
        logo=None,
        mugshot=None,
    ):
        super().__init__()
        try:
            self.owner = owner
            self.registrant = registrant
            self.name = name
            self.display_name = display_name
            self._title = title
            self.summary = summary
            self.description = description
            self.homepageurl = homepageurl
            self.icon = icon
            self.logo = logo
            self.mugshot = mugshot
        except Exception:
            # If validating references such as `owner` fails, then the new
            # object may have been added to the store first.  Remove it
            # again in that case.
            IStore(self).remove(self)
            raise

    @property
    def displayname(self):
        return self.display_name

    @property
    def title(self):
        return self.display_name

    @property
    def pillar_category(self):
        """See `IPillar`."""
        return "Project Group"

    def getProducts(self, user):
        results = Store.of(self).find(
            Product,
            Product.projectgroup == self,
            Product.active == True,
            ProductSet.getProductPrivacyFilter(user),
        )
        return results.order_by(Product.display_name)

    @cachedproperty
    def products(self):
        return list(self.getProducts(getUtility(ILaunchBag).user))

    def getProduct(self, name):
        return (
            IStore(Product).find(Product, projectgroup=self, name=name).one()
        )

    def getConfigurableProducts(self):
        return [
            product
            for product in self.products
            if check_permission("launchpad.Edit", product)
        ]

    @property
    def drivers(self):
        """See `IHasDrivers`."""
        if self.driver is not None:
            return [self.driver]
        return []

    def getTranslatables(self):
        """Return an iterator over products that are translatable in LP.

        Only products with IProduct.translations_usage set to
        ServiceUsage.LAUNCHPAD are considered translatable.
        """
        store = Store.of(self)
        origin = [
            Product,
            Join(ProductSeries, Product.id == ProductSeries.product_id),
            Join(POTemplate, ProductSeries.id == POTemplate.productseries_id),
        ]
        return (
            store.using(*origin)
            .find(
                Product,
                Product.projectgroup == self.id,
                Product.translations_usage == ServiceUsage.LAUNCHPAD,
            )
            .config(distinct=True)
        )

    @cachedproperty
    def translatables(self):
        """See `IProjectGroup`."""
        return list(self.getTranslatables())

    def has_translatable(self):
        """See `IProjectGroup`."""
        return len(self.translatables) > 0

    def sharesTranslationsWithOtherSide(
        self, person, language, sourcepackage=None, purportedly_upstream=False
    ):
        """See `ITranslationPolicy`."""
        assert sourcepackage is None, "Got a SourcePackage for a ProjectGroup!"
        # ProjectGroup translations are considered upstream.  They are
        # automatically shared.
        return True

    def has_branches(self):
        """See `IProjectGroup`."""
        return not self.getBranches().is_empty()

    def _getBaseClausesForQueryingSprints(self):
        return [
            Product.projectgroup == self,
            Specification.product == Product.id,
            Specification.id == SprintSpecification.specification_id,
            SprintSpecification.sprint == Sprint.id,
            SprintSpecification.status == SprintSpecificationStatus.ACCEPTED,
        ]

    def specifications(
        self,
        user,
        sort=None,
        quantity=None,
        filter=None,
        series=None,
        need_people=True,
        need_branches=True,
        need_workitems=False,
    ):
        """See `IHasSpecifications`."""
        base_clauses = [
            Specification.product_id == Product.id,
            Product.projectgroup_id == self.id,
        ]
        tables = [Specification]
        if series:
            base_clauses.append(ProductSeries.name == series)
            tables.append(
                Join(
                    ProductSeries,
                    Specification.productseries_id == ProductSeries.id,
                )
            )
        return search_specifications(
            self,
            base_clauses,
            user,
            sort,
            quantity,
            filter,
            tables=tables,
            need_people=need_people,
            need_branches=need_branches,
            need_workitems=need_workitems,
        )

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this milestone."""
        search_params.setProjectGroup(self)

    def _getOfficialTagClause(self):
        """See `OfficialBugTagTargetMixin`."""
        And(
            ProjectGroup.id == Product.projectgroup_id,
            Product.id == OfficialBugTag.product_id,
        )

    @property
    def official_bug_tags(self):
        """See `IHasBugs`."""
        store = Store.of(self)
        result = store.find(
            OfficialBugTag.tag,
            OfficialBugTag.product == Product.id,
            Product.projectgroup == self.id,
        ).order_by(OfficialBugTag.tag)
        result.config(distinct=True)
        return result

    def getBugSummaryContextWhereClause(self):
        """See BugTargetBase."""
        # Circular fail.
        from lp.bugs.model.bugsummary import BugSummary

        product_ids = [product.id for product in self.products]
        if not product_ids:
            return False
        return BugSummary.product_id.is_in(product_ids)

    # IQuestionCollection
    def searchQuestions(
        self,
        search_text=None,
        status=QUESTION_STATUS_DEFAULT_SEARCH,
        language=None,
        sort=None,
        owner=None,
        needs_attention_from=None,
        unsupported=False,
        created_before=None,
        created_since=None,
    ):
        """See `IQuestionCollection`."""
        if unsupported:
            unsupported_target = self
        else:
            unsupported_target = None

        return QuestionTargetSearch(
            projectgroup=self,
            search_text=search_text,
            status=status,
            language=language,
            sort=sort,
            owner=owner,
            needs_attention_from=needs_attention_from,
            unsupported_target=unsupported_target,
            created_before=created_before,
            created_since=created_since,
        ).getResults()

    def getQuestionLanguages(self):
        """See `IQuestionCollection`."""
        return set(
            IStore(Language)
            .find(
                Language,
                Question.language == Language.id,
                Question.product == Product.id,
                Product.projectgroup == self.id,
            )
            .config(distinct=True)
        )

    @property
    def bugtargetname(self):
        """See IBugTarget."""
        return self.name

    # IFAQCollection
    def getFAQ(self, id):
        """See `IQuestionCollection`."""
        faq = FAQ.getForTarget(id, None)
        if (
            faq is not None
            and IProduct.providedBy(faq.target)
            and faq.target in self.products
        ):
            # Filter out faq not related to this project.
            return faq
        else:
            return None

    def searchFAQs(self, search_text=None, owner=None, sort=None):
        """See `IQuestionCollection`."""
        return FAQSearch(
            search_text=search_text, owner=owner, sort=sort, projectgroup=self
        ).getResults()

    def hasProducts(self):
        """Returns True if a project group has products associated with it,
        False otherwise.

        If the project group has < 1 product, selected links will be disabled.
        This is to avoid situations where users try to file bugs against
        empty project groups (Malone bug #106523).
        """
        return len(self.products) != 0

    def _getMilestoneCondition(self):
        """See `HasMilestonesMixin`."""
        user = getUtility(ILaunchBag).user
        privacy_filter = ProductSet.getProductPrivacyFilter(user)
        return And(
            Milestone.product_id == Product.id,
            Product.projectgroup_id == self.id,
            privacy_filter,
        )

    def _getMilestones(self, user, only_active):
        """Return a list of milestones for this project group.

        If only_active is True, only active milestones are returned,
        else all milestones.

        A project group has a milestone named 'A', if at least one of its
        products has a milestone named 'A'.
        """
        store = Store.of(self)

        columns = (
            Milestone.name,
            Min(Milestone.dateexpected),
            Func("bool_or", Milestone.active),
        )
        privacy_filter = ProductSet.getProductPrivacyFilter(user)
        conditions = And(
            Milestone.product == Product.id,
            Product.projectgroup == self,
            Product.active == True,
            privacy_filter,
        )
        result = store.find(columns, conditions)
        result.group_by(Milestone.name)
        if only_active:
            result.having(Is(Func("bool_or", Milestone.active), True))
        # Min(Milestone.dateexpected) has to be used to match the
        # aggregate function in the `columns` variable.
        result.order_by(
            Desc(
                Func(
                    "milestone_sort_key",
                    Min(Milestone.dateexpected),
                    Milestone.name,
                )
            )
        )
        # An extra query is required here in order to get the correct
        # products without affecting the group/order of the query above.
        products_by_name = {}
        if result.any() is not None:
            milestone_names = [data[0] for data in result]
            product_conditions = And(
                Product.projectgroup == self,
                Milestone.product == Product.id,
                Product.active == True,
                privacy_filter,
                In(Milestone.name, milestone_names),
            )
            for product, name in store.find(
                (Product, Milestone.name), product_conditions
            ):
                if name not in products_by_name.keys():
                    products_by_name[name] = product
        return shortlist(
            [
                ProjectMilestone(
                    self,
                    name,
                    dateexpected,
                    active,
                    products_by_name.get(name, None),
                )
                for name, dateexpected, active in result
            ]
        )

    @property
    def has_milestones(self):
        """See `IHasMilestones`."""
        store = Store.of(self)
        result = store.find(
            Milestone.id,
            And(
                Milestone.product == Product.id,
                Product.projectgroup == self,
                Product.active == True,
            ),
        )
        return result.any() is not None

    @property
    def milestones(self):
        """See `IProjectGroup`."""
        user = getUtility(ILaunchBag).user
        return self._getMilestones(user, only_active=True)

    @property
    def product_milestones(self):
        """Hack to avoid the ProjectMilestone in MilestoneVocabulary."""
        # XXX: bug=644977 Robert Collins - this is a workaround for
        # inconsistency in project group milestone use.
        return self._get_milestones()

    @property
    def all_milestones(self):
        """See `IProjectGroup`."""
        user = getUtility(ILaunchBag).user
        return self._getMilestones(user, only_active=False)

    def all_milestones_with_releases(self):
        """See `IHasMilestones`.

        ProjectMilestone objects always have product_release=None, so
        this is equivalent to all_milestones.
        """
        return self.all_milestones

    def getMilestone(self, name):
        """See `IProjectGroup`."""
        for milestone in self.all_milestones:
            if milestone.name == name:
                return milestone
        return None

    def getSeries(self, series_name):
        """See `IProjectGroup.`"""
        has_series = (
            IStore(ProductSeries)
            .find(
                ProductSeries,
                ProductSeries.product_id == Product.id,
                ProductSeries.name == series_name,
                Product.projectgroup == self,
            )
            .order_by(ProductSeries.id)
            .first()
        )

        if has_series is None:
            return None

        return ProjectGroupSeries(self, series_name)

    def _get_usage(self, attr):
        """Determine ProjectGroup usage based on individual projects.

        By default, return ServiceUsage.UNKNOWN.
        If any project uses Launchpad, return ServiceUsage.LAUNCHPAD.
        Otherwise, return the ServiceUsage of the last project that was
        not ServiceUsage.UNKNOWN.
        """
        result = ServiceUsage.UNKNOWN
        for product in self.products:
            product_usage = getattr(product, attr)
            if product_usage != ServiceUsage.UNKNOWN:
                result = product_usage
                if product_usage == ServiceUsage.LAUNCHPAD:
                    break
        return result

    @property
    def answers_usage(self):
        return self._get_usage("answers_usage")

    @property
    def blueprints_usage(self):
        return self._get_usage("blueprints_usage")

    @property
    def translations_usage(self):
        if self.has_translatable():
            return ServiceUsage.LAUNCHPAD
        return ServiceUsage.UNKNOWN

    @property
    def codehosting_usage(self):
        # Project groups do not support submitting code.
        return ServiceUsage.NOT_APPLICABLE

    @property
    def bug_tracking_usage(self):
        return self._get_usage("bug_tracking_usage")

    @property
    def uses_launchpad(self):
        if (
            self.answers_usage == ServiceUsage.LAUNCHPAD
            or self.blueprints_usage == ServiceUsage.LAUNCHPAD
            or self.translations_usage == ServiceUsage.LAUNCHPAD
            or self.codehosting_usage == ServiceUsage.LAUNCHPAD
            or self.bug_tracking_usage == ServiceUsage.LAUNCHPAD
        ):
            return True
        return False


@implementer(IProjectGroupSet)
class ProjectGroupSet:
    def __init__(self):
        self.title = "Project groups registered in Launchpad"

    def __iter__(self):
        return iter(IStore(ProjectGroup).find(ProjectGroup, active=True))

    def __getitem__(self, name):
        projectgroup = self.getByName(name=name, ignore_inactive=True)
        if projectgroup is None:
            raise NotFoundError(name)
        return projectgroup

    def get(self, projectgroupid):
        """See `lp.registry.interfaces.projectgroup.IProjectGroupSet`.

        >>> print(getUtility(IProjectGroupSet).get(1).name)
        apache
        >>> getUtility(IProjectGroupSet).get(-1)
        ... # doctest: +NORMALIZE_WHITESPACE
        Traceback (most recent call last):
        ...
        lp.app.errors.NotFoundError: -1
        """
        projectgroup = IStore(ProjectGroup).get(ProjectGroup, projectgroupid)
        if projectgroup is None:
            raise NotFoundError(projectgroupid)
        return projectgroup

    def getByName(self, name, ignore_inactive=False):
        """See `IProjectGroupSet`."""
        pillar = getUtility(IPillarNameSet).getByName(name, ignore_inactive)
        if not IProjectGroup.providedBy(pillar):
            return None
        return pillar

    def new(
        self,
        name,
        display_name,
        title,
        homepageurl,
        summary,
        description,
        owner,
        mugshot=None,
        logo=None,
        icon=None,
        registrant=None,
        bug_supervisor=None,
        driver=None,
    ):
        """See `lp.registry.interfaces.projectgroup.IProjectGroupSet`."""
        if registrant is None:
            registrant = owner
        projectgroup = ProjectGroup(
            name=name,
            display_name=display_name,
            title=title,
            summary=summary,
            description=description,
            homepageurl=homepageurl,
            owner=owner,
            registrant=registrant,
            mugshot=mugshot,
            logo=logo,
            icon=icon,
        )
        Store.of(projectgroup).flush()
        return projectgroup

    def count_all(self):
        return IStore(ProjectGroup).find(ProjectGroup).count()

    def forReview(self):
        return IStore(ProjectGroup).find(
            ProjectGroup, Is(ProjectGroup.reviewed, False)
        )

    def search(self, text=None, search_products=False, show_inactive=False):
        """Search through the Registry database for project groups that match
        the query terms. text is a piece of text in the title / summary /
        description fields of project group (and possibly product). soyuz,
        bazaar, malone etc are hints as to whether the search
        should be limited to projects that are active in those Launchpad
        applications.
        """
        joining_product = False
        clauses = []

        if text:
            if search_products:
                joining_product = True
                clauses.extend(
                    [
                        Product.projectgroup == ProjectGroup.id,
                        fti_search(Product, text),
                    ]
                )
            else:
                clauses.append(fti_search(ProjectGroup, text))

        if not show_inactive:
            clauses.append(ProjectGroup.active)
            if joining_product:
                clauses.append(Product.active)

        return (
            IStore(ProjectGroup)
            .find(ProjectGroup, *clauses)
            .config(distinct=True)
        )


@implementer(IProjectGroupSeries)
class ProjectGroupSeries(HasSpecificationsMixin):
    """See `IProjectGroupSeries`."""

    def __init__(self, projectgroup, name):
        self.projectgroup = projectgroup
        self.name = name

    def specifications(
        self,
        user,
        sort=None,
        quantity=None,
        filter=None,
        need_people=True,
        need_branches=True,
        need_workitems=False,
    ):
        return self.projectgroup.specifications(
            user,
            sort,
            quantity,
            filter,
            self.name,
            need_people=need_people,
            need_branches=need_branches,
            need_workitems=need_workitems,
        )

    @property
    def title(self):
        return "%s Series %s" % (self.projectgroup.title, self.name)

    @property
    def displayname(self):
        return self.name
