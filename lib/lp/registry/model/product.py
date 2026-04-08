# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database classes including and related to Product."""

__all__ = [
    "get_precached_products",
    "LicensesModifiedEvent",
    "Product",
    "ProductSet",
]


import http.client
import operator
from datetime import datetime, time, timedelta, timezone

from lazr.lifecycle.event import ObjectModifiedEvent
from lazr.restful.declarations import error_status
from storm.databases.postgres import JSON
from storm.expr import (
    SQL,
    And,
    Coalesce,
    Desc,
    Exists,
    Func,
    Join,
    LeftJoin,
    Lower,
    Not,
    Or,
    Select,
)
from storm.info import ClassAlias
from storm.locals import (
    Bool,
    DateTime,
    Int,
    List,
    Reference,
    ReferenceSet,
    Store,
    Unicode,
)
from zope.component import getUtility
from zope.event import notify
from zope.interface import implementer

from lp.answers.enums import QUESTION_STATUS_DEFAULT_SEARCH
from lp.answers.model.faq import FAQ, FAQSearch
from lp.answers.model.question import (
    Question,
    QuestionTargetMixin,
    QuestionTargetSearch,
)
from lp.app.enums import (
    FREE_INFORMATION_TYPES,
    PILLAR_INFORMATION_TYPES,
    PROPRIETARY_INFORMATION_TYPES,
    PUBLIC_INFORMATION_TYPES,
    InformationType,
    ServiceUsage,
    service_uses_launchpad,
)
from lp.app.errors import NotFoundError, ServiceUsageForbidden
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.interfaces.services import IService
from lp.app.model.launchpad import InformationTypeMixin
from lp.archivepublisher.debversion import Version
from lp.blueprints.enums import SpecificationFilter
from lp.blueprints.model.specification import (
    SPECIFICATION_POLICY_ALLOWED_TYPES,
    SPECIFICATION_POLICY_DEFAULT_TYPES,
    HasSpecificationsMixin,
    Specification,
)
from lp.blueprints.model.specificationsearch import search_specifications
from lp.blueprints.model.sprint import HasSprintsMixin
from lp.bugs.interfaces.bugsummary import IBugSummaryDimension
from lp.bugs.interfaces.bugtarget import (
    BUG_POLICY_ALLOWED_TYPES,
    BUG_POLICY_DEFAULT_TYPES,
)
from lp.bugs.interfaces.bugtaskfilter import OrderedBugTask
from lp.bugs.model.bugtarget import BugTargetBase, OfficialBugTagTargetMixin
from lp.bugs.model.bugtask import BugTask
from lp.bugs.model.bugtaskflat import BugTaskFlat
from lp.bugs.model.bugwatch import BugWatch
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.code.enums import BranchType
from lp.code.interfaces.branch import DEFAULT_BRANCH_STATUS_IN_LISTING
from lp.code.interfaces.branchcollection import IBranchCollection
from lp.code.interfaces.gitcollection import IGitCollection
from lp.code.interfaces.gitrepository import IGitRepositorySet
from lp.code.model.branch import Branch
from lp.code.model.gitrepository import GitRepository
from lp.code.model.hasbranches import (
    HasBranchesMixin,
    HasCodeImportsMixin,
    HasMergeProposalsMixin,
)
from lp.code.model.sourcepackagerecipe import SourcePackageRecipe
from lp.code.model.sourcepackagerecipedata import SourcePackageRecipeData
from lp.registry.enums import (
    INCLUSIVE_TEAM_POLICY,
    BranchSharingPolicy,
    BugSharingPolicy,
    SpecificationSharingPolicy,
    VCSType,
)
from lp.registry.errors import (
    CannotChangeInformationType,
    CommercialSubscribersOnly,
    ProprietaryPillar,
)
from lp.registry.interfaces.ociproject import IOCIProjectSet
from lp.registry.interfaces.person import (
    IPersonSet,
    validate_person,
    validate_person_or_closed_team,
    validate_public_person,
)
from lp.registry.interfaces.pillar import IPillarNameSet
from lp.registry.interfaces.product import (
    ILicensesModifiedEvent,
    IProduct,
    IProductSet,
    License,
    LicenseStatus,
)
from lp.registry.interfaces.productrelease import IProductReleaseSet
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.model.accesspolicy import AccessPolicyGrantFlat
from lp.registry.model.announcement import MakesAnnouncements
from lp.registry.model.commercialsubscription import CommercialSubscription
from lp.registry.model.distribution import Distribution
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.hasdrivers import HasDriversMixin
from lp.registry.model.karma import KarmaContextMixin
from lp.registry.model.milestone import HasMilestonesMixin, Milestone
from lp.registry.model.oopsreferences import referenced_oops
from lp.registry.model.packaging import Packaging
from lp.registry.model.person import Person
from lp.registry.model.pillar import HasAliasMixin
from lp.registry.model.productlicense import ProductLicense
from lp.registry.model.productrelease import ProductRelease
from lp.registry.model.productseries import ProductSeries
from lp.registry.model.series import ACTIVE_STATUSES
from lp.registry.model.sharingpolicy import SharingPolicyMixin
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.model.teammembership import TeamParticipation
from lp.services.auth.model import AccessTokenTargetMixin
from lp.services.database import bulk
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import (
    ArrayAgg,
    ArrayIntersects,
    fti_search,
)
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.statistics.interfaces.statistic import ILaunchpadStatisticSet
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.snapshot import notify_modified
from lp.services.webhooks.model import WebhookTargetMixin
from lp.translations.enums import TranslationPermission
from lp.translations.interfaces.customlanguagecode import (
    IHasCustomLanguageCodes,
)
from lp.translations.interfaces.translations import (
    TranslationsBranchImportMode,
)
from lp.translations.model.customlanguagecode import (
    CustomLanguageCode,
    HasCustomLanguageCodesMixin,
)
from lp.translations.model.hastranslationimports import (
    HasTranslationImportsMixin,
)
from lp.translations.model.potemplate import POTemplate
from lp.translations.model.translationpolicy import TranslationPolicyMixin


@implementer(ILicensesModifiedEvent)
class LicensesModifiedEvent(ObjectModifiedEvent):
    """See `ILicensesModifiedEvent`."""

    def __init__(self, product, user=None):
        super().__init__(product, product, [], user)


def get_license_status(license_approved, project_reviewed, licenses):
    """Decide the licence status for an `IProduct`.

    :return: A LicenseStatus enum value.
    """
    # A project can only be marked 'license_approved' if it is
    # OTHER_OPEN_SOURCE.  So, if it is 'license_approved' we return
    # OPEN_SOURCE, which means one of our admins has determined it is good
    # enough for us for the project to freely use Launchpad.
    if license_approved:
        return LicenseStatus.OPEN_SOURCE
    elif License.OTHER_PROPRIETARY in licenses:
        return LicenseStatus.PROPRIETARY
    elif License.OTHER_OPEN_SOURCE in licenses:
        if project_reviewed:
            # The OTHER_OPEN_SOURCE licence was not manually approved
            # by setting license_approved to true.
            return LicenseStatus.PROPRIETARY
        else:
            # The OTHER_OPEN_SOURCE is pending review.
            return LicenseStatus.UNREVIEWED
    else:
        # The project has at least one licence and does not have
        # OTHER_PROPRIETARY or OTHER_OPEN_SOURCE as a licence.
        return LicenseStatus.OPEN_SOURCE


@error_status(http.client.BAD_REQUEST)
class UnDeactivateable(Exception):
    """Raised when a project is requested to deactivate but can not."""

    def __init__(self, msg):
        super().__init__(msg)


bug_policy_default = {
    InformationType.PUBLIC: BugSharingPolicy.PUBLIC,
    InformationType.PROPRIETARY: BugSharingPolicy.PROPRIETARY,
}


branch_policy_default = {
    InformationType.PUBLIC: BranchSharingPolicy.PUBLIC,
    InformationType.PROPRIETARY: BranchSharingPolicy.PROPRIETARY,
}


specification_policy_default = {
    InformationType.PUBLIC: SpecificationSharingPolicy.PUBLIC,
    InformationType.PROPRIETARY: SpecificationSharingPolicy.PROPRIETARY,
}


@implementer(IBugSummaryDimension, IHasCustomLanguageCodes, IProduct)
class Product(
    StormBase,
    AccessTokenTargetMixin,
    BugTargetBase,
    HasDriversMixin,
    OfficialBugTagTargetMixin,
    HasBranchesMixin,
    HasMergeProposalsMixin,
    HasMilestonesMixin,
    HasSprintsMixin,
    HasTranslationImportsMixin,
    TranslationPolicyMixin,
    KarmaContextMixin,
    MakesAnnouncements,
    HasCodeImportsMixin,
    QuestionTargetMixin,
    HasSpecificationsMixin,
    StructuralSubscriptionTargetMixin,
    InformationTypeMixin,
    HasAliasMixin,
    HasCustomLanguageCodesMixin,
    SharingPolicyMixin,
    WebhookTargetMixin,
):
    """A Product."""

    __storm_table__ = "Product"

    id = Int(primary=True)
    projectgroup_id = Int(name="project", allow_none=True, default=None)
    projectgroup = Reference(projectgroup_id, "ProjectGroup.id")
    _owner_id = Int(
        name="owner",
        validator=validate_person_or_closed_team,
        allow_none=False,
    )
    _owner = Reference(_owner_id, "Person.id")
    registrant_id = Int(
        name="registrant", validator=validate_public_person, allow_none=False
    )
    registrant = Reference(registrant_id, "Person.id")
    bug_supervisor_id = Int(
        name="bug_supervisor",
        validator=validate_person,
        allow_none=True,
        default=None,
    )
    bug_supervisor = Reference(bug_supervisor_id, "Person.id")
    driver_id = Int(
        name="driver", validator=validate_person, allow_none=True, default=None
    )
    driver = Reference(driver_id, "Person.id")
    name = Unicode(name="name", allow_none=False)
    display_name = Unicode(name="displayname", allow_none=False)
    _title = Unicode(name="title", allow_none=False)
    summary = Unicode(name="summary", allow_none=False)
    description = Unicode(allow_none=True, default=None)
    datecreated = DateTime(
        name="datecreated",
        allow_none=False,
        default=UTC_NOW,
        tzinfo=timezone.utc,
    )
    homepageurl = Unicode(name="homepageurl", allow_none=True, default=None)
    homepage_content = Unicode(default=None)
    icon_id = Int(name="icon", allow_none=True, default=None)
    icon = Reference(icon_id, "LibraryFileAlias.id")
    logo_id = Int(name="logo", allow_none=True, default=None)
    logo = Reference(logo_id, "LibraryFileAlias.id")
    mugshot_id = Int(name="mugshot", allow_none=True, default=None)
    mugshot = Reference(mugshot_id, "LibraryFileAlias.id")
    screenshotsurl = Unicode(
        name="screenshotsurl", allow_none=True, default=None
    )
    wikiurl = Unicode(name="wikiurl", allow_none=True, default=None)
    programminglang = Unicode(
        name="programminglang", allow_none=True, default=None
    )
    downloadurl = Unicode(name="downloadurl", allow_none=True, default=None)
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
    translation_focus_id = Int(
        name="translation_focus", allow_none=True, default=None
    )
    translation_focus = Reference(translation_focus_id, "ProductSeries.id")
    bugtracker_id = Int(name="bugtracker", allow_none=True, default=None)
    bugtracker = Reference(bugtracker_id, "BugTracker.id")
    official_answers = Bool(
        name="official_answers", allow_none=False, default=False
    )
    official_blueprints = Bool(
        name="official_blueprints", allow_none=False, default=False
    )
    official_malone = Bool(
        name="official_malone", allow_none=False, default=False
    )
    remote_product = Unicode(
        name="remote_product", allow_none=True, default=None
    )
    vcs = DBEnum(enum=VCSType, allow_none=True)

    # Cache of AccessPolicy.ids that convey launchpad.LimitedView.
    # Unlike artifacts' cached access_policies, an AccessArtifactGrant
    # to an artifact in the policy is sufficient for access.
    access_policies = List(type=Int())

    _creating = False

    def __init__(
        self,
        owner,
        registrant,
        name,
        display_name,
        title,
        summary,
        projectgroup=None,
        bug_supervisor=None,
        driver=None,
        description=None,
        homepageurl=None,
        icon=None,
        logo=None,
        mugshot=None,
        screenshotsurl=None,
        wikiurl=None,
        programminglang=None,
        downloadurl=None,
        vcs=None,
        information_type=InformationType.PUBLIC,
        project_reviewed=False,
        sourceforgeproject=None,
        license_info=None,
    ):
        super().__init__()
        try:
            self._creating = True
            self.owner = owner
            self.registrant = registrant
            self.name = name
            self.display_name = display_name
            self._title = title
            self.summary = summary
            self.projectgroup = projectgroup
            self.bug_supervisor = bug_supervisor
            self.driver = driver
            self.description = description
            self.homepageurl = homepageurl
            self.icon = icon
            self.logo = logo
            self.mugshot = mugshot
            self.screenshotsurl = screenshotsurl
            self.wikiurl = wikiurl
            self.programminglang = programminglang
            self.downloadurl = downloadurl
            self.vcs = vcs
            self.information_type = information_type
            self.project_reviewed = project_reviewed
            self.sourceforgeproject = sourceforgeproject
            self.license_info = license_info
        except Exception:
            # If validating references such as `owner` fails, then the new
            # object may have been added to the store first.  Remove it
            # again in that case.
            store = Store.of(self)
            if store is not None:
                store.remove(self)
            raise
        del self._creating

    @property
    def displayname(self):
        return self.display_name

    @property
    def title(self):
        return self.display_name

    @property
    def date_next_suggest_packaging(self):
        """See `IProduct`

        Returns None; exists only to maintain API compatibility.
        """
        return None

    @cachedproperty
    def inferred_vcs(self):
        """Use vcs, otherwise infer from existence of git or bzr branches.

        Bzr take precedence over git, if no project vcs set.
        """
        if self.vcs:
            return self.vcs
        if not IBranchCollection(self).is_empty():
            return VCSType.BZR
        elif not IGitCollection(self).is_empty():
            return VCSType.GIT

    @date_next_suggest_packaging.setter  # pyflakes:ignore
    def date_next_suggest_packaging(self, value):
        """See `IProduct`

        Ignores supplied value; exists only to maintain API compatibility.
        """
        pass

    def _valid_product_information_type(self, attr, value):
        for exception in self.checkInformationType(value):
            raise exception
        return value

    def checkInformationType(self, value):
        """Check whether the information type change should be permitted.

        Iterate through exceptions explaining why the type should not be
        changed.  Has the side-effect of creating a commercial subscription if
        permitted.
        """
        if value not in PILLAR_INFORMATION_TYPES:
            yield CannotChangeInformationType("Not supported for Projects.")
        if value in PROPRIETARY_INFORMATION_TYPES:
            if self.answers_usage == ServiceUsage.LAUNCHPAD:
                yield CannotChangeInformationType("Answers is enabled.")
        if self._creating or value not in PROPRIETARY_INFORMATION_TYPES:
            return
        # Additional checks when transitioning an existing product to a
        # proprietary type
        # All specs located by an ALL search are public.
        public_specs = self.specifications(
            None, filter=[SpecificationFilter.ALL]
        )
        if not public_specs.is_empty():
            # Unlike bugs and branches, specifications cannot be USERDATA or a
            # security type.
            yield CannotChangeInformationType("Some blueprints are public.")
        store = Store.of(self)
        series_ids = [series.id for series in self.series]
        non_proprietary_bugs = store.find(
            BugTaskFlat,
            BugTaskFlat.information_type.is_in(FREE_INFORMATION_TYPES),
            Or(
                BugTaskFlat.product == self.id,
                BugTaskFlat.productseries_id.is_in(series_ids),
            ),
        )
        if not non_proprietary_bugs.is_empty():
            yield CannotChangeInformationType(
                "Some bugs are neither proprietary nor embargoed."
            )
        # Default returns all public branches.
        non_proprietary_branches = store.find(
            Branch,
            Branch.product == self.id,
            Not(Branch.information_type.is_in(PROPRIETARY_INFORMATION_TYPES)),
        )
        if not non_proprietary_branches.is_empty():
            yield CannotChangeInformationType(
                "Some branches are neither proprietary nor embargoed."
            )
        questions = store.find(Question, Question.product == self.id)
        if not questions.is_empty():
            yield CannotChangeInformationType("This project has questions.")
        templates = store.find(
            POTemplate,
            ProductSeries.product == self.id,
            POTemplate.productseries == ProductSeries.id,
        )
        if not templates.is_empty():
            yield CannotChangeInformationType("This project has translations.")
        if not self.getTranslationImportQueueEntries().is_empty():
            yield CannotChangeInformationType(
                "This project has queued translations."
            )
        import_productseries = store.find(
            ProductSeries,
            ProductSeries.product == self.id,
            ProductSeries.translations_autoimport_mode
            != TranslationsBranchImportMode.NO_IMPORT,
        )
        if not import_productseries.is_empty():
            yield CannotChangeInformationType(
                "Some product series have translation imports enabled."
            )
        if not self.packagings.is_empty():
            yield CannotChangeInformationType("Some series are packaged.")
        if self.translations_usage == ServiceUsage.LAUNCHPAD:
            yield CannotChangeInformationType("Translations are enabled.")
        # Proprietary check works only after creation, because during
        # creation, has_commercial_subscription cannot give the right value
        # and triggers an inappropriate DB flush.

        # If you're changing the license, and setting a PROPRIETARY
        # information type, yet you don't have a subscription, you get one
        # when the license is set.

        # Create the complimentary commercial subscription for the product.
        self._ensure_complimentary_subscription()

        # If you have a commercial subscription, but it's not current, you
        # cannot set the information type to a PROPRIETARY type.
        if not self.has_current_commercial_subscription:
            yield CommercialSubscribersOnly(
                "A valid commercial subscription is required for private"
                " Projects."
            )
        if (
            self.bug_supervisor is not None
            and self.bug_supervisor.membership_policy in INCLUSIVE_TEAM_POLICY
        ):
            yield CannotChangeInformationType(
                "Bug supervisor has inclusive membership."
            )

    _information_type = DBEnum(
        enum=InformationType,
        default=InformationType.PUBLIC,
        name="information_type",
        validator=_valid_product_information_type,
    )

    def _get_information_type(self):
        return self._information_type or InformationType.PUBLIC

    def _set_information_type(self, value):
        old_info_type = self._information_type
        self._information_type = value
        # Make sure that policies are updated to grant permission to the
        # maintainer as required for the Product.
        # However, only on edits. If this is a new Product it's handled
        # already.
        if not self._creating:
            if (
                old_info_type == InformationType.PUBLIC
                and value != InformationType.PUBLIC
            ):
                self.setBranchSharingPolicy(branch_policy_default[value])
                self.setBugSharingPolicy(bug_policy_default[value])
                self.setSpecificationSharingPolicy(
                    specification_policy_default[value]
                )
            self._ensurePolicies([value])

    information_type = property(_get_information_type, _set_information_type)

    security_contact = None

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        return self

    @property
    def pillar(self):
        """See `IBugTarget`."""
        return self

    @property
    def pillar_category(self):
        """See `IPillar`."""
        return "Project"

    @cachedproperty
    def _default_git_repository(self):
        return getUtility(IGitRepositorySet).getDefaultRepository(self)

    @property
    def official_codehosting(self):
        repository = self._default_git_repository
        return (
            self.development_focus.branch is not None or repository is not None
        )

    @property
    def official_anything(self):
        return True in (
            self.official_malone,
            self.translations_usage == ServiceUsage.LAUNCHPAD,
            self.official_blueprints,
            self.official_answers,
            self.official_codehosting,
        )

    _answers_usage = DBEnum(
        name="answers_usage",
        allow_none=False,
        enum=ServiceUsage,
        default=ServiceUsage.UNKNOWN,
    )
    _blueprints_usage = DBEnum(
        name="blueprints_usage",
        allow_none=False,
        enum=ServiceUsage,
        default=ServiceUsage.UNKNOWN,
    )

    def validate_translations_usage(self, attr, value):
        if value == ServiceUsage.LAUNCHPAD and self.private:
            raise ProprietaryPillar(
                "Translations are not supported for proprietary products."
            )
        return value

    translations_usage = DBEnum(
        name="translations_usage",
        allow_none=False,
        enum=ServiceUsage,
        default=ServiceUsage.UNKNOWN,
        validator=validate_translations_usage,
    )

    @property
    def codehosting_usage(self):
        repository = self._default_git_repository
        if self.development_focus.branch is None and repository is None:
            return ServiceUsage.UNKNOWN
        elif (
            repository is not None
            or self.development_focus.branch.branch_type == BranchType.HOSTED
        ):
            # XXX cjwatson 2015-07-07: Fix this when we have
            # GitRepositoryType; an imported default repository should imply
            # ServiceUsage.EXTERNAL.
            return ServiceUsage.LAUNCHPAD
        elif self.development_focus.branch.branch_type in (
            BranchType.MIRRORED,
            BranchType.REMOTE,
            BranchType.IMPORTED,
        ):
            return ServiceUsage.EXTERNAL
        return ServiceUsage.NOT_APPLICABLE

    @property
    def bug_tracking_usage(self):
        if self.official_malone:
            return ServiceUsage.LAUNCHPAD
        elif self.bugtracker is None:
            return ServiceUsage.UNKNOWN
        else:
            return ServiceUsage.EXTERNAL

    @property
    def uses_launchpad(self):
        """Does this distribution actually use Launchpad?"""
        return ServiceUsage.LAUNCHPAD in (
            self.answers_usage,
            self.blueprints_usage,
            self.translations_usage,
            self.codehosting_usage,
            self.bug_tracking_usage,
        )

    def _getMilestoneCondition(self):
        """See `HasMilestonesMixin`."""
        return Milestone.product == self

    enable_bug_expiration = Bool(
        name="enable_bug_expiration", allow_none=False, default=False
    )
    project_reviewed = Bool(name="reviewed", allow_none=False, default=False)
    reviewer_whiteboard = Unicode(allow_none=True, default=None)
    private_bugs = False
    bug_sharing_policy = DBEnum(
        enum=BugSharingPolicy, allow_none=True, default=None
    )
    branch_sharing_policy = DBEnum(
        enum=BranchSharingPolicy, allow_none=True, default=None
    )
    specification_sharing_policy = DBEnum(
        enum=SpecificationSharingPolicy,
        allow_none=True,
        default=SpecificationSharingPolicy.PUBLIC,
    )
    autoupdate = Bool(name="autoupdate", allow_none=False, default=False)
    freshmeatproject = None
    sourceforgeproject = Unicode(allow_none=True, default=None)
    # While the interface defines this field as required, we need to
    # allow it to be NULL so we can create new product records before
    # the corresponding series records.
    development_focus_id = Int(
        name="development_focus", allow_none=True, default=None
    )
    development_focus = Reference(development_focus_id, "ProductSeries.id")
    bug_reporting_guidelines = Unicode(default=None)
    content_templates = JSON(name="content_templates", default=None)
    bug_reported_acknowledgement = Unicode(default=None)
    enable_bugfiling_duplicate_search = Bool(allow_none=False, default=True)

    def _validate_active(self, attr, value):
        # Validate deactivation.
        if self.active == True and value == False:
            if len(self.sourcepackages) > 0:
                raise UnDeactivateable(
                    "This project cannot be deactivated since it is "
                    "linked to source packages."
                )
        return value

    active = Bool(
        name="active",
        allow_none=False,
        default=True,
        validator=_validate_active,
    )

    def _validate_license_info(self, attr, value):
        if not self._creating and value != self.license_info:
            # Clear the project_reviewed and license_approved flags
            # if the licence changes.
            self._resetLicenseReview()
        return value

    license_info = Unicode(
        name="license_info", default=None, validator=_validate_license_info
    )

    def _validate_license_approved(self, attr, value):
        """Ensure licence approved is only applied to the correct licences."""
        if not self._creating:
            licenses = list(self.licenses)
            if value:
                if (
                    License.OTHER_PROPRIETARY in licenses
                    or [License.DONT_KNOW] == licenses
                ):
                    raise ValueError(
                        "Projects without a licence or have "
                        "'Other/Proprietary' may not be approved."
                    )
                # Approving a licence implies it has been reviewed.  Force
                # `project_reviewed` to be True.
                self.project_reviewed = True
        return value

    license_approved = Bool(
        name="license_approved",
        allow_none=False,
        default=False,
        validator=_validate_license_approved,
    )

    def getAllowedBugInformationTypes(self):
        """See `IProduct.`"""
        return BUG_POLICY_ALLOWED_TYPES[self.bug_sharing_policy]

    def getDefaultBugInformationType(self):
        """See `IProduct.`"""
        return BUG_POLICY_DEFAULT_TYPES[self.bug_sharing_policy]

    def getAllowedSpecificationInformationTypes(self):
        """See `ISpecificationTarget`."""
        return SPECIFICATION_POLICY_ALLOWED_TYPES[
            self.specification_sharing_policy
        ]

    def getDefaultSpecificationInformationType(self):
        """See `ISpecificationTarget`."""
        return SPECIFICATION_POLICY_DEFAULT_TYPES[
            self.specification_sharing_policy
        ]

    @cachedproperty
    def commercial_subscription(self):
        return (
            IStore(CommercialSubscription)
            .find(CommercialSubscription, product=self)
            .one()
        )

    @property
    def has_current_commercial_subscription(self):
        now = datetime.now(timezone.utc)
        return (
            self.commercial_subscription
            and self.commercial_subscription.date_expires > now
        )

    @property
    def qualifies_for_free_hosting(self):
        """See `IProduct`."""
        if self.license_approved:
            # The licence was manually approved for free hosting.
            return True
        elif License.OTHER_PROPRIETARY in self.licenses:
            # Proprietary licenses need a subscription without
            # waiting for a review.
            return False
        elif self.project_reviewed and (
            License.OTHER_OPEN_SOURCE in self.licenses
            or self.license_info not in ("", None)
        ):
            # We only know that an unknown open source licence
            # requires a subscription after we have reviewed it
            # when we have not set license_approved to True.
            return False
        elif len(self.licenses) == 0:
            # The owner needs to choose a licence.
            return False
        else:
            # The project has only valid open source licence(s).
            return True

    @property
    def commercial_subscription_is_due(self):
        """See `IProduct`.

        If True, display subscription warning to project owner.
        """
        if self.qualifies_for_free_hosting:
            return False
        elif (
            self.commercial_subscription is None
            or not self.commercial_subscription.is_active
        ):
            # The project doesn't have an active subscription.
            return True
        else:
            warning_date = (
                self.commercial_subscription.date_expires - timedelta(30)
            )
            now = datetime.now(timezone.utc)
            if now > warning_date:
                # The subscription is close to being expired.
                return True
            else:
                # The subscription is good.
                return False

    @property
    def is_permitted(self):
        """See `IProduct`.

        If False, disable many tasks on this project.
        """
        if self.qualifies_for_free_hosting:
            # The project qualifies for free hosting.
            return True
        elif self.commercial_subscription is None:
            return False
        else:
            return self.commercial_subscription.is_active

    @property
    def license_status(self):
        """See `IProduct`.

        :return: A LicenseStatus enum value.
        """
        return get_license_status(
            self.license_approved, self.project_reviewed, self.licenses
        )

    def _resetLicenseReview(self):
        """When the licence is modified, it must be reviewed again."""
        self.project_reviewed = False
        self.license_approved = False

    def _get_answers_usage(self):
        if self._answers_usage != ServiceUsage.UNKNOWN:
            # If someone has set something with the enum, use it.
            return self._answers_usage
        elif self.official_answers:
            return ServiceUsage.LAUNCHPAD
        return self._answers_usage

    def _set_answers_usage(self, val):
        if val == ServiceUsage.LAUNCHPAD:
            if self.information_type in PROPRIETARY_INFORMATION_TYPES:
                raise ServiceUsageForbidden(
                    "Answers not allowed for non-public projects."
                )
        self._answers_usage = val
        if val == ServiceUsage.LAUNCHPAD:
            self.official_answers = True
        else:
            self.official_answers = False

    answers_usage = property(
        _get_answers_usage,
        _set_answers_usage,
        doc="Indicates if the product uses the answers service.",
    )

    def _get_blueprints_usage(self):
        if self._blueprints_usage != ServiceUsage.UNKNOWN:
            # If someone has set something with the enum, use it.
            return self._blueprints_usage
        elif self.official_blueprints:
            return ServiceUsage.LAUNCHPAD
        return self._blueprints_usage

    def _set_blueprints_usage(self, val):
        self._blueprints_usage = val
        if val == ServiceUsage.LAUNCHPAD:
            self.official_blueprints = True
        else:
            self.official_blueprints = False

    blueprints_usage = property(
        _get_blueprints_usage,
        _set_blueprints_usage,
        doc="Indicates if the product uses the blueprints service.",
    )

    @cachedproperty
    def _cached_licenses(self):
        """Get the licenses as a tuple."""
        product_licenses = (
            IStore(ProductLicense)
            .find(ProductLicense, product=self)
            .order_by(ProductLicense.license)
        )
        return tuple(
            product_license.license for product_license in product_licenses
        )

    def _getLicenses(self):
        return self._cached_licenses

    def _setLicenses(self, licenses, reset_project_reviewed=True):
        """Set the licences from a tuple of license enums.

        The licenses parameter must not be an empty tuple.
        """
        licenses = set(licenses)
        old_licenses = set(self.licenses)
        if licenses == old_licenses:
            return
        # Clear the project_reviewed and license_approved flags
        # if the licence changes.
        # ProductSet.createProduct() passes in reset_project_reviewed=False
        # to avoid changing the value when a Launchpad Admin sets
        # project_reviewed & licences at the same time.
        if reset_project_reviewed:
            self._resetLicenseReview()
        if len(licenses) == 0:
            raise ValueError("licenses argument must not be empty.")
        for license in licenses:
            if license not in License:
                raise ValueError("%s is not a License." % license)

        for license in old_licenses.difference(licenses):
            product_license = (
                IStore(ProductLicense)
                .find(ProductLicense, product=self, license=license)
                .one()
            )
            product_license.destroySelf()

        for license in licenses.difference(old_licenses):
            ProductLicense(product=self, license=license)
        get_property_cache(self)._cached_licenses = tuple(sorted(licenses))
        if (
            License.OTHER_PROPRIETARY in licenses
            and self.commercial_subscription is None
        ):
            self._ensure_complimentary_subscription()

        notify(LicensesModifiedEvent(self))

    licenses = property(_getLicenses, _setLicenses)

    def _ensure_complimentary_subscription(self):
        """Create a complementary commercial subscription for the product"""
        if not self.commercial_subscription:
            lp_janitor = getUtility(ILaunchpadCelebrities).janitor
            now = datetime.now(timezone.utc)
            date_expires = now + timedelta(days=30)
            sales_system_id = "complimentary-30-day-%s" % now
            whiteboard = (
                "Complimentary 30 day subscription. -- Launchpad %s"
                % now.date().isoformat()
            )
            subscription = CommercialSubscription(
                pillar=self,
                date_starts=now,
                date_expires=date_expires,
                registrant=lp_janitor,
                purchaser=lp_janitor,
                sales_system_id=sales_system_id,
                whiteboard=whiteboard,
            )
            get_property_cache(self).commercial_subscription = subscription

    def _getOwner(self):
        """Get the owner."""
        return self._owner

    def _setOwner(self, new_owner):
        """Set the owner.

        Send an IObjectModifiedEvent to notify subscribers that the owner
        changed.
        """
        if self.owner is None:
            # This is being initialized.
            self._owner = new_owner
        elif self.owner != new_owner:
            with notify_modified(self, ["_owner"]):
                self._owner = new_owner
        else:
            # The new owner is the same as the current owner.
            pass

    owner = property(_getOwner, _setOwner)

    def getExternalBugTracker(self):
        """See `IHasExternalBugTracker`."""
        if self.official_malone:
            return None
        elif self.bugtracker is not None:
            return self.bugtracker
        elif self.projectgroup is not None:
            return self.projectgroup.bugtracker
        else:
            return None

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this product.."""
        search_params.setProduct(self)

    _series = ReferenceSet(
        "id", "ProductSeries.product_id", order_by="ProductSeries.name"
    )

    @cachedproperty
    def series(self):
        return self._series

    @property
    def active_or_packaged_series(self):
        store = Store.of(self)
        tables = [
            ProductSeries,
            LeftJoin(Packaging, Packaging.productseries == ProductSeries.id),
        ]
        result = store.using(*tables).find(
            ProductSeries,
            ProductSeries.product == self,
            Or(
                ProductSeries.status.is_in(ACTIVE_STATUSES),
                Packaging.id != None,
            ),
        )
        result = result.order_by(Desc(ProductSeries.name))
        result.config(distinct=True)
        return result

    @property
    def packagings(self):
        store = Store.of(self)
        result = store.find(
            (Packaging, DistroSeries),
            Packaging.distroseries == DistroSeries.id,
            Packaging.productseries == ProductSeries.id,
            ProductSeries.product == self,
        )
        result = result.order_by(
            DistroSeries.version, ProductSeries.name, Packaging.id
        )

        def decorate(row):
            packaging, distroseries = row
            return packaging

        return DecoratedResultSet(result, decorate)

    @property
    def releases(self):
        store = Store.of(self)
        origin = [
            ProductRelease,
            Join(Milestone, ProductRelease.milestone == Milestone.id),
        ]
        result = store.using(*origin)
        result = result.find(ProductRelease, Milestone.product == self)
        return result.order_by(Milestone.name)

    @property
    def drivers(self):
        """See `IProduct`."""
        drivers = set()
        drivers.add(self.driver)
        if self.projectgroup is not None:
            drivers.add(self.projectgroup.driver)
        drivers.discard(None)
        if len(drivers) == 0:
            if self.projectgroup is not None:
                drivers.add(self.projectgroup.owner)
            else:
                drivers.add(self.owner)
        return sorted(drivers, key=lambda driver: driver.displayname)

    @property
    def sourcepackages(self):
        from lp.registry.model.sourcepackage import SourcePackage

        ret = DecoratedResultSet(
            IStore(Packaging)
            .using(
                Packaging,
                Join(
                    ProductSeries, Packaging.productseries == ProductSeries.id
                ),
                Join(
                    SourcePackageName,
                    Packaging.sourcepackagename == SourcePackageName.id,
                ),
                Join(DistroSeries, Packaging.distroseries == DistroSeries.id),
                Join(
                    Distribution, DistroSeries.distribution == Distribution.id
                ),
            )
            .find(
                (Packaging, SourcePackageName, DistroSeries, Distribution),
                ProductSeries.product == self,
            ),
            result_decorator=operator.itemgetter(0),
        )
        sps = [
            SourcePackage(
                sourcepackagename=r.sourcepackagename,
                distroseries=r.distroseries,
            )
            for r in ret
        ]
        return sorted(
            sps,
            key=lambda x: (
                x.sourcepackagename.name,
                x.distroseries.name,
                x.distroseries.distribution.name,
            ),
        )

    @cachedproperty
    def distrosourcepackages(self):
        from lp.registry.model.distributionsourcepackage import (
            DistributionSourcePackage,
        )

        dsp_info = get_distro_sourcepackages([self])
        return [
            DistributionSourcePackage(
                sourcepackagename=sourcepackagename, distribution=distro
            )
            for sourcepackagename, distro, product_id in dsp_info
        ]

    @cachedproperty
    def ubuntu_packages(self):
        """The Ubuntu `IDistributionSourcePackage`s linked to the product."""
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        return [
            package
            for package in self.distrosourcepackages
            if package.distribution == ubuntu
        ]

    @property
    def bugtargetdisplayname(self):
        """See IBugTarget."""
        return self.display_name

    @property
    def bugtargetname(self):
        """See `IBugTarget`."""
        return self.name

    def getOCIProject(self, name):
        return getUtility(IOCIProjectSet).getByPillarAndName(self, name)

    def canAdministerOCIProjects(self, person):
        if person is None:
            return False
        # XXX: pappacena 2020-05-25: Maybe we should have an attribute named
        # oci_project_admin on Product too, the same way we have on
        # Distribution.
        if person.inTeam(self.driver):
            return True
        person_roles = IPersonRoles(person)
        if person_roles.in_admin or person_roles.isOwner(self):
            return True
        return False

    def getPackage(self, distroseries):
        """See `IProduct`."""
        if isinstance(distroseries, Distribution):
            distroseries = distroseries.currentrelease
        for pkg in self.sourcepackages:
            if pkg.distroseries == distroseries:
                return pkg
        else:
            raise NotFoundError(distroseries)

    def getMilestone(self, name):
        """See `IProduct`."""
        return IStore(Milestone).find(Milestone, product=self, name=name).one()

    def getBugSummaryContextWhereClause(self):
        """See BugTargetBase."""
        # Circular fail.
        from lp.bugs.model.bugsummary import BugSummary

        return And(
            BugSummary.product_id == self.id, BugSummary.ociproject_id == None
        )

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
            product=self,
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

    def getTargetTypes(self):
        """See `QuestionTargetMixin`.

        Defines product as self.
        """
        return {"product": self}

    def newFAQ(self, owner, title, content, keywords=None, date_created=None):
        """See `IFAQTarget`."""
        return FAQ.new(
            owner=owner,
            title=title,
            content=content,
            keywords=keywords,
            date_created=date_created,
            product=self,
        )

    def findReferencedOOPS(self, start_date, end_date):
        """See `IHasOOPSReferences`."""
        return list(
            referenced_oops(
                start_date,
                end_date,
                "product=%(product)s",
                {"product": self.id},
            )
        )

    def findSimilarFAQs(self, summary):
        """See `IFAQTarget`."""
        return FAQ.findSimilar(summary, product=self)

    def getFAQ(self, id):
        """See `IFAQCollection`."""
        return FAQ.getForTarget(id, self)

    def searchFAQs(self, search_text=None, owner=None, sort=None):
        """See `IFAQCollection`."""
        return FAQSearch(
            search_text=search_text, owner=owner, sort=sort, product=self
        ).getResults()

    @property
    def translatable_packages(self):
        """See `IProduct`."""
        packages = {
            package
            for package in self.sourcepackages
            if package.has_current_translation_templates
        }

        # Sort packages by distroseries.version (descending order)
        # and package.name (ascending order)
        sorted_by_names = sorted(packages, key=lambda p: p.name)
        return sorted(
            sorted_by_names,
            key=lambda p: Version(p.distroseries.version),
            reverse=True,
        )

    @property
    def translatable_series(self):
        """See `IProduct`."""
        if not service_uses_launchpad(self.translations_usage):
            return []
        translatable_product_series = {
            product_series
            for product_series in self.series
            if product_series.has_current_translation_templates
        }
        return sorted(
            translatable_product_series, key=operator.attrgetter("datecreated")
        )

    def getVersionSortedSeries(self, statuses=None, filter_statuses=None):
        """See `IProduct`."""
        store = Store.of(self)
        dev_focus = store.find(
            ProductSeries, ProductSeries.id == self.development_focus.id
        )
        other_series_conditions = [
            ProductSeries.product == self,
            ProductSeries.id != self.development_focus.id,
        ]
        if statuses is not None:
            other_series_conditions.append(
                ProductSeries.status.is_in(statuses)
            )
        if filter_statuses is not None:
            other_series_conditions.append(
                Not(ProductSeries.status.is_in(filter_statuses))
            )
        other_series = store.find(ProductSeries, other_series_conditions)
        # The query will be much slower if the version_sort_key is not
        # the first thing that is sorted, since it won't be able to use
        # the productseries_name_sort index.
        other_series.order_by(SQL("version_sort_key(name) DESC"))
        # UNION ALL must be used to preserve the sort order from the
        # separate queries. The sorting should not be done after
        # unioning the two queries, because that will prevent it from
        # being able to use the productseries_name_sort index.
        return dev_focus.union(other_series, all=True)

    @property
    def obsolete_translatable_series(self):
        """See `IProduct`."""
        obsolete_product_series = {
            product_series
            for product_series in self.series
            if product_series.has_obsolete_translation_templates
        }
        return sorted(obsolete_product_series, key=lambda s: s.datecreated)

    @property
    def primary_translatable(self):
        """See `IProduct`."""
        packages = self.translatable_packages
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        targetseries = ubuntu.currentseries
        product_series = self.translatable_series

        if product_series:
            # First, go with translation focus
            if self.translation_focus in product_series:
                return self.translation_focus
            # Next, go with development focus
            if self.development_focus in product_series:
                return self.development_focus
            # Next, go with the latest product series that has templates:
            return product_series[-1]
        # Otherwise, look for an Ubuntu package in the current distroseries:
        for package in packages:
            if package.distroseries == targetseries:
                return package
        # now let's make do with any ubuntu package
        for package in packages:
            if package.distribution == ubuntu:
                return package
        # or just any package
        if len(packages) > 0:
            return packages[0]
        # capitulate
        return None

    @property
    def translationgroups(self):
        return reversed(self.getTranslationGroups())

    def isTranslationsOwner(self, person):
        """See `ITranslationPolicy`."""
        # A Product owner gets special translation privileges.
        return person.inTeam(self.owner)

    def getInheritedTranslationPolicy(self):
        """See `ITranslationPolicy`."""
        # A Product inherits parts of its effective translation policy from
        # its ProjectGroup, if any.
        return self.projectgroup

    def sharesTranslationsWithOtherSide(
        self, person, language, sourcepackage=None, purportedly_upstream=False
    ):
        """See `ITranslationPolicy`."""
        assert sourcepackage is None, "Got a SourcePackage for a Product!"
        # Product translations are considered upstream.  They are
        # automatically shared.
        return True

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
        """See `IHasSpecifications`."""
        base_clauses = [Specification.product == self]
        return search_specifications(
            self,
            base_clauses,
            user,
            sort,
            quantity,
            filter,
            need_people=need_people,
            need_branches=need_branches,
            need_workitems=need_workitems,
        )

    def getSpecification(self, name):
        """See `ISpecificationTarget`."""
        return (
            IStore(Specification)
            .find(Specification, product=self, name=name)
            .one()
        )

    def getSeries(self, name):
        """See `IProduct`."""
        return (
            IStore(ProductSeries)
            .find(ProductSeries, product=self, name=name)
            .one()
        )

    def newSeries(
        self, owner, name, summary, branch=None, releasefileglob=None
    ):
        series = ProductSeries(
            product=self,
            owner=owner,
            name=name,
            summary=summary,
            branch=branch,
            releasefileglob=releasefileglob,
        )
        if owner.inTeam(self.driver) and not owner.inTeam(self.owner):
            # The user is a product driver, and should be the driver of this
            # series to make them the release manager.
            series.driver = owner
        Store.of(series).flush()
        return series

    def getRelease(self, version):
        """See `IProduct`."""
        origin = [
            ProductRelease,
            Join(Milestone, ProductRelease.milestone == Milestone.id),
        ]
        return (
            Store.of(self)
            .using(*origin)
            .find(
                ProductRelease,
                And(Milestone.product == self, Milestone.name == version),
            )
            .one()
        )

    def getMilestonesAndReleases(self):
        """See `IProduct`."""

        def strip_product_id(row):
            return row[0], row[1]

        return DecoratedResultSet(
            get_milestones_and_releases([self]), strip_product_id
        )

    def composeCustomLanguageCodeMatch(self):
        """See `HasCustomLanguageCodesMixin`."""
        return CustomLanguageCode.product == self

    def userCanEdit(self, user):
        """See `IProduct`."""
        if user is None:
            return False
        celebs = getUtility(ILaunchpadCelebrities)
        return (
            user.inTeam(celebs.registry_experts)
            or user.inTeam(celebs.admin)
            or user.inTeam(self.owner)
        )

    def getLinkedBugWatches(self):
        """See `IProduct`."""
        return Store.of(self).find(
            BugWatch,
            And(
                BugTask.product == self.id,
                BugTask.bugwatch == BugWatch.id,
                BugWatch.bugtracker == self.getExternalBugTracker(),
            ),
        )

    def getTimeline(self, include_inactive=False):
        """See `IProduct`."""

        def decorate(series):
            return series.getTimeline(include_inactive=include_inactive)

        if include_inactive is True:
            statuses = None
        else:
            statuses = ACTIVE_STATUSES
        return DecoratedResultSet(
            self.getVersionSortedSeries(statuses=statuses), decorate
        )

    @property
    def recipes(self):
        """See `IHasRecipes`."""
        tables = [
            SourcePackageRecipe,
            SourcePackageRecipeData,
            LeftJoin(Branch, SourcePackageRecipeData.base_branch == Branch.id),
            LeftJoin(
                GitRepository,
                SourcePackageRecipeData.base_git_repository
                == GitRepository.id,
            ),
        ]
        recipes = (
            Store.of(self)
            .using(*tables)
            .find(
                SourcePackageRecipe,
                SourcePackageRecipe.id
                == SourcePackageRecipeData.sourcepackage_recipe_id,
                Or(Branch.product == self, GitRepository.project == self),
            )
        )
        hook = SourcePackageRecipe.preLoadDataForSourcePackageRecipes
        return DecoratedResultSet(recipes, pre_iter_hook=hook)

    def getBugTaskWeightFunction(self):
        """Provide a weight function to determine optimal bug task.

        Full weight is given to tasks for this product.

        Given that there must be a product task for a series of that product
        to have a task, we give no more weighting to a productseries task than
        any other.
        """
        productID = self.id

        def weight_function(bugtask):
            if bugtask.product_id == productID:
                return OrderedBugTask(1, bugtask.id, bugtask)
            return OrderedBugTask(2, bugtask.id, bugtask)

        return weight_function

    @cachedproperty
    def _known_viewers(self):
        """A set of known persons able to view this product."""
        return set()

    def userCanView(self, user):
        """See `IProductPublic`."""
        if self.information_type in PUBLIC_INFORMATION_TYPES:
            return True
        if user is None:
            return False
        if user.id in self._known_viewers:
            return True
        if not IPersonRoles.providedBy(user):
            user = IPersonRoles(user)
        if user.in_commercial_admin or user.in_admin:
            self._known_viewers.add(user.id)
            return True
        if getUtility(IService, "sharing").checkPillarAccess(
            [self], self.information_type, user
        ):
            self._known_viewers.add(user.id)
            return True
        return False

    def userCanLimitedView(self, user):
        """See `IProductPublic`."""
        if self.userCanView(user):
            return True
        if user is None:
            return False
        return (
            not Store.of(self)
            .find(
                Product,
                Product.id == self.id,
                ProductSet.getProductPrivacyFilter(user.person),
            )
            .is_empty()
        )

    @property
    def valid_webhook_event_types(self):
        return ["bug:0.1", "bug:comment:0.1"]


def get_precached_products(
    products,
    need_licences=False,
    need_projectgroups=False,
    need_series=False,
    need_releases=False,
    role_names=None,
    need_role_validity=False,
    need_codehosting_usage=False,
    need_packages=False,
):
    """Load and cache product information.

    :param products: the products for which to pre-cache information
    :param need_licences: whether to cache license information
    :param need_projectgroups: whether to cache project group information
    :param need_series: whether to cache series information
    :param need_releases: whether to cache release information
    :param role_names: the role names to cache eg bug_supervisor
    :param need_role_validity: whether to cache validity information
    :param need_codehosting_usage: whether to cache codehosting usage
        information
    :return: a list of products
    """

    # Circular imports.
    from lp.code.interfaces.gitrepository import IGitRepositorySet
    from lp.registry.model.projectgroup import ProjectGroup

    product_ids = {obj.id for obj in products}
    if not product_ids:
        return
    products_by_id = {product.id: product for product in products}
    caches = {product.id: get_property_cache(product) for product in products}
    for cache in caches.values():
        if not hasattr(cache, "commercial_subscription"):
            cache.commercial_subscription = None
        if need_licences and not hasattr(cache, "_cached_licenses"):
            cache._cached_licenses = []
        if need_packages and not hasattr(cache, "distrosourcepackages"):
            cache.distrosourcepackages = []
        if need_series and not hasattr(cache, "series"):
            cache.series = []

    from lp.registry.model.distributionsourcepackage import (
        DistributionSourcePackage,
    )

    if need_packages:
        distrosourcepackages = get_distro_sourcepackages(products)
        for sourcepackagename, distro, product_id in distrosourcepackages:
            cache = caches[product_id]
            dsp = DistributionSourcePackage(
                sourcepackagename=sourcepackagename, distribution=distro
            )
            cache.distrosourcepackages.append(dsp)

    if need_series:
        series_caches = {}
        for series in IStore(ProductSeries).find(
            ProductSeries, ProductSeries.product_id.is_in(product_ids)
        ):
            series_cache = get_property_cache(series)
            if need_releases and not hasattr(series_cache, "_cached_releases"):
                series_cache._cached_releases = []

            series_caches[series.id] = series_cache
            cache = caches[series.product_id]
            cache.series.append(series)
        if need_releases:
            release_caches = {}
            all_releases = []
            milestones_and_releases = get_milestones_and_releases(products)
            for milestone, release, _ in milestones_and_releases:
                release_cache = get_property_cache(release)
                release_caches[release.id] = release_cache
                if not hasattr(release_cache, "files"):
                    release_cache.files = []
                all_releases.append(release)
                series_cache = series_caches[milestone.productseries.id]
                series_cache._cached_releases.append(release)

            prs = getUtility(IProductReleaseSet)
            files = prs.getFilesForReleases(all_releases)
            for file in files:
                release_cache = release_caches[file.productrelease.id]
                release_cache.files.append(file)

    for subscription in IStore(CommercialSubscription).find(
        CommercialSubscription,
        CommercialSubscription.product_id.is_in(product_ids),
    ):
        cache = caches[subscription.product_id]
        cache.commercial_subscription = subscription
    if need_licences:
        for license in IStore(ProductLicense).find(
            ProductLicense, ProductLicense.product_id.is_in(product_ids)
        ):
            cache = caches[license.product_id]
            if license.license not in cache._cached_licenses:
                cache._cached_licenses.append(license.license)
    if need_projectgroups:
        bulk.load_related(
            ProjectGroup, products_by_id.values(), ["projectgroup_id"]
        )
    bulk.load_related(
        ProductSeries, products_by_id.values(), ["development_focus_id"]
    )
    if role_names is not None:
        person_ids = set()
        for attr_name in role_names:
            person_ids.update(
                map(
                    lambda x: getattr(x, attr_name + "_id"),
                    products_by_id.values(),
                )
            )
        person_ids.discard(None)
        list(
            getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                person_ids, need_validity=need_role_validity
            )
        )
    if need_codehosting_usage:
        repository_set = getUtility(IGitRepositorySet)
        repository_map = repository_set.preloadDefaultRepositoriesForProjects(
            products
        )
        for product_id in product_ids:
            caches[product_id]._default_git_repository = repository_map.get(
                product_id
            )
    return products


# XXX: jcsackett 2010-08-23 bug=620494
# The second clause in the order_by in this method is a bandaid
# on a sorting issue caused by date vs datetime conflicts in the
# database. A fix is coming out, but this deals with the edge
# case responsible for the referenced bug.
def get_milestones_and_releases(products):
    """Bulk load the milestone and release information for the products."""
    store = IStore(Product)
    product_ids = [product.id for product in products]
    result = store.find(
        (Milestone, ProductRelease, ProductSeries.product_id),
        And(
            ProductRelease.milestone == Milestone.id,
            Milestone.productseries == ProductSeries.id,
            ProductSeries.product_id.is_in(product_ids),
        ),
    )
    return result.order_by(
        Desc(ProductRelease.datereleased), Desc(Milestone.name)
    )


def get_distro_sourcepackages(products):
    """Bulk load the source package information for the products."""
    store = IStore(Packaging)
    origin = [
        Packaging,
        Join(
            SourcePackageName,
            Packaging.sourcepackagename == SourcePackageName.id,
        ),
        Join(ProductSeries, Packaging.productseries == ProductSeries.id),
        Join(DistroSeries, Packaging.distroseries == DistroSeries.id),
        Join(Distribution, DistroSeries.distribution == Distribution.id),
    ]
    product_ids = [product.id for product in products]
    result = store.using(*origin).find(
        (SourcePackageName, Distribution, ProductSeries.product_id),
        ProductSeries.product_id.is_in(product_ids),
    )
    result = result.order_by(SourcePackageName.name, Distribution.name)
    result.config(distinct=True)
    return result


@implementer(IProductSet)
class ProductSet:
    def __init__(self):
        self.title = "Projects in Launchpad"

    def __getitem__(self, name):
        """See `IProductSet`."""
        product = self.getByName(name=name, ignore_inactive=True)
        if product is None:
            raise NotFoundError(name)
        return product

    def __iter__(self):
        """See `IProductSet`."""
        return iter(self.get_all_active(None))

    @property
    def people(self):
        return getUtility(IPersonSet)

    @classmethod
    def latest(cls, user, quantity=5):
        """See `IProductSet`."""
        result = cls.get_all_active(user)
        if quantity is not None:
            result = result[:quantity]
        return result

    @staticmethod
    def getProductPrivacyFilter(user):
        # Anonymous users can only see public projects. This is also
        # sometimes used with an outer join with eg. Distribution, so we
        # let NULL through too.
        public_filter = Or(
            Product._information_type == None,
            Product._information_type == InformationType.PUBLIC,
        )
        if user is None:
            return public_filter

        # (Commercial) admins can see any project.
        roles = IPersonRoles(user)
        if roles.in_admin or roles.in_commercial_admin:
            return True

        # In places where this method is used, they might want to use
        # TeamParticipation. This ensures that we use a different one.
        ownership_participation = ClassAlias(TeamParticipation)

        # Normal users can see any project for which they can see either
        # an entire policy or an artifact.
        # XXX wgrant 2015-06-26: This is slower than ideal for people in
        # teams with lots of artifact grants, as there can be tens of
        # thousands of APGF rows for a single policy. But it's tens of
        # milliseconds at most.
        grant_filter = Coalesce(
            ArrayIntersects(
                SQL("Product.access_policies"),
                Select(
                    ArrayAgg(AccessPolicyGrantFlat.policy_id),
                    tables=(
                        AccessPolicyGrantFlat,
                        Join(
                            ownership_participation,
                            ownership_participation.team_id
                            == AccessPolicyGrantFlat.grantee_id,
                        ),
                    ),
                    where=(ownership_participation.person_id == user.id),
                ),
            ),
            False,
        )
        return Or(public_filter, grant_filter)

    @classmethod
    def get_users_private_products(cls, user):
        """List the non-public products the user owns."""
        result = IStore(Product).find(
            Product,
            Product._owner == user,
            Product._information_type.is_in(PROPRIETARY_INFORMATION_TYPES),
        )
        return result

    @classmethod
    def get_all_active(cls, user, eager_load=True):
        clause = cls.getProductPrivacyFilter(user)
        result = (
            IStore(Product)
            .find(Product, Product.active, clause)
            .order_by(Desc(Product.datecreated), Product.id)
        )
        if not eager_load:
            return result

        def do_eager_load(rows):
            owner_ids = set(map(operator.attrgetter("_owner_id"), rows))
            # +detailed-listing renders the person with team branding.
            list(
                getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                    owner_ids, need_validity=True, need_icon=True
                )
            )

        return DecoratedResultSet(result, pre_iter_hook=do_eager_load)

    def get(self, productid):
        """See `IProductSet`."""
        product = IStore(Product).get(Product, productid)
        if product is None:
            raise NotFoundError(
                "Product with ID %s does not exist" % str(productid)
            )
        return product

    def getByName(self, name, ignore_inactive=False):
        """See `IProductSet`."""
        pillar = getUtility(IPillarNameSet).getByName(name, ignore_inactive)
        if not IProduct.providedBy(pillar):
            return None
        return pillar

    def getProductsWithBranches(self, num_products=None):
        """See `IProductSet`."""
        results = (
            IStore(Product)
            .find(
                Product,
                Product.id.is_in(
                    Select(
                        Branch.product_id,
                        where=Branch.lifecycle_status.is_in(
                            DEFAULT_BRANCH_STATUS_IN_LISTING
                        ),
                        distinct=True,
                    )
                ),
                Product.active,
            )
            .order_by(Product.name)
        )
        if num_products is not None:
            results = results[:num_products]
        return results

    def createProduct(
        self,
        owner,
        name,
        display_name,
        title,
        summary,
        description=None,
        projectgroup=None,
        homepageurl=None,
        screenshotsurl=None,
        wikiurl=None,
        downloadurl=None,
        freshmeatproject=None,
        sourceforgeproject=None,
        programminglang=None,
        project_reviewed=False,
        mugshot=None,
        logo=None,
        icon=None,
        licenses=None,
        license_info=None,
        registrant=None,
        bug_supervisor=None,
        driver=None,
        information_type=None,
        vcs=None,
    ):
        """See `IProductSet`."""
        if registrant is None:
            registrant = owner
        if licenses is None:
            licenses = set()
        if information_type is None:
            information_type = InformationType.PUBLIC
        if (
            information_type in PILLAR_INFORMATION_TYPES
            and information_type in PROPRIETARY_INFORMATION_TYPES
        ):
            # This check is skipped in _valid_product_information_type during
            # creation, so done here.  It predicts whether a commercial
            # subscription will be generated based on the selected license,
            # duplicating product._setLicenses
            if License.OTHER_PROPRIETARY not in licenses:
                raise CommercialSubscribersOnly(
                    "A valid commercial subscription is required for private"
                    " Projects."
                )
        product = Product(
            owner=owner,
            registrant=registrant,
            name=name,
            display_name=display_name,
            title=title,
            projectgroup=projectgroup,
            summary=summary,
            description=description,
            homepageurl=homepageurl,
            screenshotsurl=screenshotsurl,
            wikiurl=wikiurl,
            downloadurl=downloadurl,
            sourceforgeproject=sourceforgeproject,
            programminglang=programminglang,
            project_reviewed=project_reviewed,
            icon=icon,
            logo=logo,
            mugshot=mugshot,
            license_info=license_info,
            bug_supervisor=bug_supervisor,
            driver=driver,
            information_type=information_type,
            vcs=vcs,
        )

        # Set up the product licence.
        if len(licenses) > 0:
            product._setLicenses(licenses, reset_project_reviewed=False)
        product.setBugSharingPolicy(bug_policy_default[information_type])
        product.setBranchSharingPolicy(branch_policy_default[information_type])
        product.setSpecificationSharingPolicy(
            specification_policy_default[information_type]
        )

        # Create a default trunk series and set it as the development focus
        trunk = product.newSeries(
            owner,
            "trunk",
            (
                'The "trunk" series represents the primary line of '
                "development rather than a stable release branch. This is "
                "sometimes also called MAIN or HEAD."
            ),
        )
        product.development_focus = trunk
        return product

    def forReview(
        self,
        user,
        search_text=None,
        active=None,
        project_reviewed=None,
        license_approved=None,
        licenses=None,
        created_after=None,
        created_before=None,
        has_subscription=None,
        subscription_expires_after=None,
        subscription_expires_before=None,
        subscription_modified_after=None,
        subscription_modified_before=None,
    ):
        """See lp.registry.interfaces.product.IProductSet."""

        conditions = [self.getProductPrivacyFilter(user)]

        if project_reviewed is not None:
            conditions.append(Product.project_reviewed == project_reviewed)

        if license_approved is not None:
            conditions.append(Product.license_approved == license_approved)

        if active is not None:
            conditions.append(Product.active == active)

        if search_text is not None and search_text.strip() != "":
            text = search_text.lower()
            conditions.append(
                Or(
                    fti_search(Product, text),
                    Product.name == text,
                    Func("strpos", Lower(Product.license_info), text) > 0,
                    Func("strpos", Lower(Product.reviewer_whiteboard), text)
                    > 0,
                )
            )

        def dateToDatetime(date):
            """Convert a datetime.date to a datetime.datetime

            The returned time will have a zero time component and be based on
            UTC.
            """
            return datetime.combine(date, time(tzinfo=timezone.utc))

        if created_after is not None:
            if not isinstance(created_after, datetime):
                created_after = dateToDatetime(created_after)
                created_after = datetime(
                    created_after.year,
                    created_after.month,
                    created_after.day,
                    tzinfo=timezone.utc,
                )
            conditions.append(Product.datecreated >= created_after)

        if created_before is not None:
            if not isinstance(created_before, datetime):
                created_before = dateToDatetime(created_before)
            conditions.append(Product.datecreated <= created_before)

        subscription_conditions = []
        if subscription_expires_after is not None:
            if not isinstance(subscription_expires_after, datetime):
                subscription_expires_after = dateToDatetime(
                    subscription_expires_after
                )
            subscription_conditions.append(
                CommercialSubscription.date_expires
                >= subscription_expires_after
            )

        if subscription_expires_before is not None:
            if not isinstance(subscription_expires_before, datetime):
                subscription_expires_before = dateToDatetime(
                    subscription_expires_before
                )
            subscription_conditions.append(
                CommercialSubscription.date_expires
                <= subscription_expires_before
            )

        if subscription_modified_after is not None:
            if not isinstance(subscription_modified_after, datetime):
                subscription_modified_after = dateToDatetime(
                    subscription_modified_after
                )
            subscription_conditions.append(
                CommercialSubscription.date_last_modified
                >= subscription_modified_after
            )
        if subscription_modified_before is not None:
            if not isinstance(subscription_modified_before, datetime):
                subscription_modified_before = dateToDatetime(
                    subscription_modified_before
                )
            subscription_conditions.append(
                CommercialSubscription.date_last_modified
                <= subscription_modified_before
            )

        assert not subscription_conditions or has_subscription is not False
        if subscription_conditions or has_subscription is not None:
            subscription_expr = Exists(
                Select(
                    1,
                    tables=[CommercialSubscription],
                    where=And(
                        *[CommercialSubscription.product == Product.id]
                        + subscription_conditions
                    ),
                )
            )
            if has_subscription is False:
                subscription_expr = Not(subscription_expr)
            conditions.append(subscription_expr)

        if licenses:
            conditions.append(
                Exists(
                    Select(
                        1,
                        tables=[ProductLicense],
                        where=And(
                            ProductLicense.product_id == Product.id,
                            ProductLicense.license.is_in(licenses),
                        ),
                    )
                )
            )

        result = (
            IStore(Product)
            .find(Product, *conditions)
            .order_by(Product.datecreated, Desc(Product.id))
        )

        def eager_load(products):
            return get_precached_products(
                products,
                role_names=["_owner", "registrant"],
                need_role_validity=True,
                need_licences=True,
                need_series=True,
                need_releases=True,
                need_codehosting_usage=True,
                need_packages=True,
            )

        return DecoratedResultSet(result, pre_iter_hook=eager_load)

    @classmethod
    def _request_user_search(cls):
        return cls.search(getUtility(ILaunchBag).user)

    @classmethod
    def search(cls, user=None, text=None):
        """See lp.registry.interfaces.product.IProductSet."""
        conditions = [Product.active, cls.getProductPrivacyFilter(user)]
        if text:
            conditions.append(fti_search(Product, text))
        result = IStore(Product).find(Product, *conditions)

        def eager_load(products):
            return get_precached_products(
                products,
                need_licences=True,
                need_projectgroups=True,
                role_names=[
                    "_owner",
                    "registrant",
                    "bug_supervisor",
                    "driver",
                ],
            )

        return DecoratedResultSet(result, pre_iter_hook=eager_load)

    def getTranslatables(self):
        """See `IProductSet`"""
        results = (
            IStore(Product)
            .find(
                (Product, Person),
                Product.active == True,
                Product.id == ProductSeries.product_id,
                POTemplate.productseries_id == ProductSeries.id,
                Product.translations_usage == ServiceUsage.LAUNCHPAD,
                Person.id == Product._owner_id,
            )
            .config(distinct=True)
            .order_by(Product.display_name)
        )

        # We only want Product - the other tables are just to populate
        # the cache.
        return DecoratedResultSet(results, operator.itemgetter(0))

    @cachedproperty
    def stats(self):
        return getUtility(ILaunchpadStatisticSet)

    def count_all(self):
        return self.stats.value("active_products")

    def count_translatable(self):
        return self.stats.value("products_with_translations")

    def count_reviewed(self):
        return self.stats.value("reviewed_products")

    def count_buggy(self):
        return self.stats.value("projects_with_bugs")

    def count_featureful(self):
        return self.stats.value("products_with_blueprints")

    def count_answered(self):
        return self.stats.value("products_with_questions")

    def count_codified(self):
        return self.stats.value("products_with_branches")

    def getProductsWithNoneRemoteProduct(self, bugtracker_type=None):
        """See `IProductSet`."""
        # Circular.
        from lp.bugs.model.bugtracker import BugTracker

        conditions = [Product.remote_product == None]
        if bugtracker_type is not None:
            conditions.extend(
                [
                    Product.bugtracker == BugTracker.id,
                    BugTracker.bugtrackertype == bugtracker_type,
                ]
            )
        return IStore(Product).find(Product, And(*conditions))

    def getSFLinkedProductsWithNoneRemoteProduct(self):
        """See `IProductSet`."""
        return IStore(Product).find(
            Product,
            Product.remote_product == None,
            Product.sourceforgeproject != None,
        )
