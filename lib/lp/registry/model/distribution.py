# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database classes for implementing distribution items."""

__all__ = [
    "Distribution",
    "DistributionSet",
]

import itertools
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from operator import itemgetter

from storm.databases.postgres import JSON
from storm.expr import (
    SQL,
    And,
    Coalesce,
    Column,
    Desc,
    Exists,
    Func,
    Join,
    LeftJoin,
    Max,
    Not,
    Or,
    Select,
    Table,
)
from storm.info import ClassAlias
from storm.locals import Bool, DateTime, Int, List, Reference, Unicode
from storm.store import Store
from zope.component import getUtility
from zope.interface import implementer
from zope.security.interfaces import Unauthorized

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
)
from lp.app.errors import NotFoundError, ServiceUsageForbidden
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.interfaces.services import IService
from lp.app.model.launchpad import InformationTypeMixin
from lp.app.validators.name import sanitize_name, valid_name
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
from lp.bugs.interfaces.bugtask import BugTaskImportance
from lp.bugs.interfaces.bugtaskfilter import OrderedBugTask
from lp.bugs.interfaces.vulnerability import IVulnerabilitySet
from lp.bugs.model.bugtarget import BugTargetBase, OfficialBugTagTargetMixin
from lp.bugs.model.bugtaskflat import BugTaskFlat
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.bugs.model.vulnerability import (
    Vulnerability,
    get_vulnerability_privacy_filter,
)
from lp.code.interfaces.seriessourcepackagebranch import (
    IFindOfficialBranchLinks,
)
from lp.code.model.branch import Branch
from lp.oci.interfaces.ociregistrycredentials import IOCIRegistryCredentialsSet
from lp.registry.enums import (
    INCLUSIVE_TEAM_POLICY,
    BranchSharingPolicy,
    BugSharingPolicy,
    DistributionDefaultTraversalPolicy,
    SpecificationSharingPolicy,
    VCSType,
)
from lp.registry.errors import (
    CannotChangeInformationType,
    CommercialSubscribersOnly,
    NoSuchDistroSeries,
    ProprietaryPillar,
)
from lp.registry.interfaces.distribution import (
    IDistribution,
    IDistributionSet,
    NoOCIAdminForDistribution,
)
from lp.registry.interfaces.distributionmirror import (
    IDistributionMirror,
    MirrorContent,
    MirrorFreshness,
    MirrorStatus,
)
from lp.registry.interfaces.ociproject import (
    OCI_PROJECT_ALLOW_CREATE,
    IOCIProjectSet,
)
from lp.registry.interfaces.person import (
    validate_person,
    validate_person_or_closed_team,
    validate_public_person,
)
from lp.registry.interfaces.pillar import IPillarNameSet
from lp.registry.interfaces.pocket import suffixpocket
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackagename import (
    ISourcePackageName,
    ISourcePackageNameSet,
)
from lp.registry.model.accesspolicy import AccessPolicyGrantFlat
from lp.registry.model.announcement import MakesAnnouncements
from lp.registry.model.commercialsubscription import CommercialSubscription
from lp.registry.model.distributionmirror import (
    DistributionMirror,
    MirrorCDImageDistroSeries,
    MirrorDistroArchSeries,
    MirrorDistroSeriesSource,
)
from lp.registry.model.distributionsourcepackage import (
    DistributionSourcePackage,
)
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.distroseriesparent import DistroSeriesParent
from lp.registry.model.externalpackage import ExternalPackage
from lp.registry.model.hasdrivers import HasDriversMixin
from lp.registry.model.karma import KarmaContextMixin
from lp.registry.model.milestone import HasMilestonesMixin, Milestone
from lp.registry.model.ociprojectname import OCIProjectName
from lp.registry.model.oopsreferences import referenced_oops
from lp.registry.model.pillar import HasAliasMixin
from lp.registry.model.sharingpolicy import SharingPolicyMixin
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.model.teammembership import TeamParticipation
from lp.services.database.bulk import load_referencing, load_related
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import (
    ArrayAgg,
    ArrayIntersects,
    fti_search,
    rank_by_fti,
)
from lp.services.features import getFeatureFlag
from lp.services.helpers import backslashreplace, shortlist
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.url import urlparse
from lp.services.webhooks.model import WebhookTargetMixin
from lp.services.worlddata.model.country import Country
from lp.soyuz.enums import (
    ArchivePurpose,
    ArchiveStatus,
    PackagePublishingStatus,
    PackageUploadStatus,
)
from lp.soyuz.interfaces.archive import MAIN_ARCHIVE_PURPOSES, IArchiveSet
from lp.soyuz.interfaces.archivepermission import IArchivePermissionSet
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuildSet
from lp.soyuz.interfaces.binarypackagename import IBinaryPackageNameSet
from lp.soyuz.interfaces.publishing import active_publishing_status
from lp.soyuz.model.archive import Archive, get_enabled_archive_filter
from lp.soyuz.model.archivefile import ArchiveFile
from lp.soyuz.model.distributionsourcepackagerelease import (
    DistributionSourcePackageRelease,
)
from lp.soyuz.model.distroarchseries import DistroArchSeries
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
    get_current_source_releases,
)
from lp.soyuz.model.queue import PackageUpload
from lp.translations.enums import TranslationPermission
from lp.translations.model.hastranslationimports import (
    HasTranslationImportsMixin,
)
from lp.translations.model.potemplate import POTemplate
from lp.translations.model.translationpolicy import TranslationPolicyMixin

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


@implementer(IBugSummaryDimension, IDistribution)
class Distribution(
    StormBase,
    BugTargetBase,
    MakesAnnouncements,
    HasSpecificationsMixin,
    HasSprintsMixin,
    HasAliasMixin,
    HasTranslationImportsMixin,
    KarmaContextMixin,
    OfficialBugTagTargetMixin,
    QuestionTargetMixin,
    StructuralSubscriptionTargetMixin,
    HasMilestonesMixin,
    HasDriversMixin,
    TranslationPolicyMixin,
    InformationTypeMixin,
    SharingPolicyMixin,
    WebhookTargetMixin,
):
    """A distribution of an operating system, e.g. Debian GNU/Linux."""

    __storm_table__ = "Distribution"
    __storm_order__ = "name"

    id = Int(primary=True)
    name = Unicode(allow_none=False)
    display_name = Unicode(name="displayname", allow_none=False)
    _title = Unicode(name="title", allow_none=False)
    summary = Unicode(allow_none=False)
    description = Unicode(allow_none=False)
    homepage_content = Unicode(default=None)
    icon_id = Int(name="icon", default=None)
    icon = Reference(icon_id, "LibraryFileAlias.id")
    logo_id = Int(name="logo", default=None)
    logo = Reference(logo_id, "LibraryFileAlias.id")
    mugshot_id = Int(name="mugshot", default=None)
    mugshot = Reference(mugshot_id, "LibraryFileAlias.id")
    domainname = Unicode(allow_none=False)
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
    bug_supervisor_id = Int(
        name="bug_supervisor",
        validator=validate_person,
        allow_none=True,
        default=None,
    )
    bug_supervisor = Reference(bug_supervisor_id, "Person.id")
    bug_reporting_guidelines = Unicode(default=None)
    content_templates = JSON(name="content_templates", default=None)
    bug_reported_acknowledgement = Unicode(default=None)
    driver_id = Int(
        name="driver",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    driver = Reference(driver_id, "Person.id")
    members_id = Int(
        name="members", validator=validate_public_person, allow_none=False
    )
    members = Reference(members_id, "Person.id")
    mirror_admin_id = Int(
        name="mirror_admin", validator=validate_public_person, allow_none=False
    )
    mirror_admin = Reference(mirror_admin_id, "Person.id")
    oci_project_admin_id = Int(
        name="oci_project_admin",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    oci_project_admin = Reference(oci_project_admin_id, "Person.id")
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
    # Distributions can't be deactivated.  This is just here in order to
    # implement the `IPillar` interface.
    active = True
    official_packages = Bool(allow_none=False, default=False)
    supports_ppas = Bool(allow_none=False, default=False)
    supports_mirrors = Bool(allow_none=False, default=False)
    package_derivatives_email = Unicode(allow_none=True, default=None)
    redirect_release_uploads = Bool(allow_none=False, default=False)
    development_series_alias = Unicode(allow_none=True, default=None)
    vcs = DBEnum(enum=VCSType, allow_none=True)
    default_traversal_policy = DBEnum(
        enum=DistributionDefaultTraversalPolicy,
        allow_none=True,
        default=DistributionDefaultTraversalPolicy.SERIES,
    )
    redirect_default_traversal = Bool(allow_none=True, default=False)
    oci_registry_credentials_id = Int(name="oci_credentials", allow_none=True)
    oci_registry_credentials = Reference(
        oci_registry_credentials_id, "OCIRegistryCredentials.id"
    )

    _creating = False

    def __init__(
        self,
        name,
        display_name,
        title,
        description,
        summary,
        domainname,
        members,
        owner,
        registrant,
        mugshot=None,
        logo=None,
        icon=None,
        vcs=None,
        information_type=None,
    ):
        self._creating = True
        try:
            self.name = name
            self.display_name = display_name
            self._title = title
            self.description = description
            self.summary = summary
            self.domainname = domainname
            self.members = members
            self.mirror_admin = owner
            self.owner = owner
            self.registrant = registrant
            self.mugshot = mugshot
            self.logo = logo
            self.icon = icon
            self.vcs = vcs
            self.information_type = information_type
        except Exception:
            IStore(self).remove(self)
            raise
        del self._creating

    def __repr__(self):
        display_name = backslashreplace(self.display_name)
        return "<%s '%s' (%s)>" % (
            self.__class__.__name__,
            display_name,
            self.name,
        )

    @property
    def displayname(self):
        return self.display_name

    @property
    def title(self):
        return self.display_name

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        return self

    @property
    def pillar(self):
        """See `IBugTarget`."""
        return self

    def _valid_distribution_information_type(self, attr, value):
        for exception in self.checkInformationType(value):
            raise exception
        return value

    def checkInformationType(self, value):
        """See `IDistribution`."""
        if value not in PILLAR_INFORMATION_TYPES:
            yield CannotChangeInformationType(
                "Not supported for distributions."
            )
        if value in PROPRIETARY_INFORMATION_TYPES:
            if self.answers_usage == ServiceUsage.LAUNCHPAD:
                yield CannotChangeInformationType("Answers is enabled.")
        if self._creating or value not in PROPRIETARY_INFORMATION_TYPES:
            return
        # Additional checks when transitioning an existing distribution to a
        # proprietary type.
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
                BugTaskFlat.distribution == self.id,
                BugTaskFlat.distroseries_id.is_in(series_ids),
            ),
        )
        if not non_proprietary_bugs.is_empty():
            yield CannotChangeInformationType(
                "Some bugs are neither proprietary nor embargoed."
            )
        # Default returns all public branches.
        non_proprietary_branches = store.find(
            Branch,
            DistroSeries.distribution == self.id,
            Branch.distroseries == DistroSeries.id,
            Not(Branch.information_type.is_in(PROPRIETARY_INFORMATION_TYPES)),
        )
        if not non_proprietary_branches.is_empty():
            yield CannotChangeInformationType(
                "Some branches are neither proprietary nor embargoed."
            )
        questions = store.find(Question, Question.distribution == self.id)
        if not questions.is_empty():
            yield CannotChangeInformationType(
                "This distribution has questions."
            )
        templates = store.find(
            POTemplate,
            DistroSeries.distribution == self.id,
            POTemplate.distroseries == DistroSeries.id,
        )
        if not templates.is_empty():
            yield CannotChangeInformationType(
                "This distribution has translations."
            )
        if not self.getTranslationImportQueueEntries().is_empty():
            yield CannotChangeInformationType(
                "This distribution has queued translations."
            )
        if self.translations_usage == ServiceUsage.LAUNCHPAD:
            yield CannotChangeInformationType("Translations are enabled.")
        bug_supervisor = self.bug_supervisor
        if (
            bug_supervisor is not None
            and bug_supervisor.membership_policy in INCLUSIVE_TEAM_POLICY
        ):
            yield CannotChangeInformationType(
                "Bug supervisor has inclusive membership."
            )

        # Proprietary check works only after creation, because during
        # creation, has_current_commercial_subscription cannot give the
        # right value and triggers an inappropriate DB flush.

        # Create the complimentary commercial subscription for the
        # distribution.
        self._ensure_complimentary_subscription()

        # If you have a commercial subscription, but it's not current, you
        # cannot set the information type to a PROPRIETARY type.
        if not self.has_current_commercial_subscription:
            yield CommercialSubscribersOnly(
                "A valid commercial subscription is required for private"
                " distributions."
            )

    _information_type = DBEnum(
        enum=InformationType,
        default=InformationType.PUBLIC,
        name="information_type",
        validator=_valid_distribution_information_type,
    )

    @property
    def information_type(self):
        return self._information_type or InformationType.PUBLIC

    @information_type.setter
    def information_type(self, value):
        old_info_type = self._information_type
        self._information_type = value
        # Make sure that policies are updated to grant permission to the
        # maintainer as required for the Distribution.
        # However, only on edits.  If this is a new Distribution it's
        # handled already.
        if not self._creating:
            if (
                old_info_type == InformationType.PUBLIC
                and value != InformationType.PUBLIC
            ):
                self._ensure_complimentary_subscription()
                self.setBranchSharingPolicy(branch_policy_default[value])
                self.setBugSharingPolicy(bug_policy_default[value])
                self.setSpecificationSharingPolicy(
                    specification_policy_default[value]
                )
            self._ensurePolicies([value])

    @property
    def pillar_category(self):
        """See `IPillar`."""
        return "Distribution"

    bug_sharing_policy = DBEnum(
        enum=BugSharingPolicy, allow_none=True, default=BugSharingPolicy.PUBLIC
    )
    branch_sharing_policy = DBEnum(
        enum=BranchSharingPolicy,
        allow_none=True,
        default=BranchSharingPolicy.PUBLIC,
    )
    specification_sharing_policy = DBEnum(
        enum=SpecificationSharingPolicy,
        allow_none=True,
        default=SpecificationSharingPolicy.PUBLIC,
    )

    # Cache of AccessPolicy.ids that convey launchpad.LimitedView.
    # Unlike artifacts' cached access_policies, an AccessArtifactGrant
    # to an artifact in the policy is sufficient for access.
    access_policies = List(type=Int())

    @cachedproperty
    def commercial_subscription(self):
        return (
            IStore(CommercialSubscription)
            .find(CommercialSubscription, distribution=self)
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
    def commercial_subscription_is_due(self):
        """See `IDistribution`.

        If True, display subscription warning to distribution owner.
        """
        if self.information_type not in PROPRIETARY_INFORMATION_TYPES:
            return False
        elif (
            self.commercial_subscription is None
            or not self.commercial_subscription.is_active
        ):
            # The distribution doesn't have an active subscription.
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

    def _ensure_complimentary_subscription(self):
        """Create a complementary commercial subscription for the distro."""
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

    @property
    def uploaders(self):
        """See `IDistribution`."""
        # Get all the distribution archives and find out the uploaders
        # for each.
        distro_uploaders = []
        permission_set = getUtility(IArchivePermissionSet)
        for archive in self.all_distro_archives:
            uploaders = permission_set.uploadersForComponent(archive)
            distro_uploaders.extend(uploaders)

        return distro_uploaders

    official_answers = Bool(
        name="official_answers", allow_none=False, default=False
    )
    official_blueprints = Bool(
        name="official_blueprints", allow_none=False, default=False
    )
    official_malone = Bool(
        name="official_malone", allow_none=False, default=False
    )

    @property
    def official_codehosting(self):
        # XXX: Aaron Bentley 2008-01-22
        # At this stage, we can't directly associate branches with source
        # packages or anything else resulting in a distribution, so saying
        # that a distribution supports codehosting at this stage makes
        # absolutely no sense at all.
        return False

    @property
    def official_anything(self):
        return True in (
            self.official_malone,
            self.translations_usage == ServiceUsage.LAUNCHPAD,
            self.official_blueprints,
            self.official_answers,
        )

    _answers_usage = DBEnum(
        name="answers_usage",
        allow_none=False,
        enum=ServiceUsage,
        default=ServiceUsage.UNKNOWN,
    )

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
                    "Answers not allowed for non-public distributions."
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

    _blueprints_usage = DBEnum(
        name="blueprints_usage",
        allow_none=False,
        enum=ServiceUsage,
        default=ServiceUsage.UNKNOWN,
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

    def validate_translations_usage(self, attr, value):
        if value == ServiceUsage.LAUNCHPAD and self.private:
            raise ProprietaryPillar(
                "Translations are not supported for proprietary "
                "distributions."
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
        return ServiceUsage.NOT_APPLICABLE

    @property
    def bug_tracking_usage(self):
        if not self.official_malone:
            return ServiceUsage.UNKNOWN
        else:
            return ServiceUsage.LAUNCHPAD

    @property
    def uses_launchpad(self):
        """Does this distribution actually use Launchpad?"""
        return self.official_anything

    enable_bug_expiration = Bool(
        name="enable_bug_expiration", allow_none=False, default=False
    )
    translation_focus_id = Int(
        name="translation_focus", allow_none=True, default=None
    )
    translation_focus = Reference(translation_focus_id, "DistroSeries.id")
    date_created = DateTime(
        allow_none=True, default=UTC_NOW, tzinfo=timezone.utc
    )
    language_pack_admin_id = Int(
        name="language_pack_admin",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    language_pack_admin = Reference(language_pack_admin_id, "Person.id")
    security_admin_id = Int(
        name="security_admin",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    security_admin = Reference(security_admin_id, "Person.id")
    code_admin_id = Int(
        name="code_admin",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    code_admin = Reference(code_admin_id, "Person.id")

    @cachedproperty
    def main_archive(self):
        """See `IDistribution`."""
        return (
            Store.of(self)
            .find(Archive, distribution=self, purpose=ArchivePurpose.PRIMARY)
            .one()
        )

    @cachedproperty
    def all_distro_archives(self):
        """See `IDistribution`."""
        return Store.of(self).find(
            Archive,
            Archive.distribution == self,
            Archive.purpose.is_in(MAIN_ARCHIVE_PURPOSES),
        )

    @cachedproperty
    def all_distro_archive_ids(self):
        """See `IDistribution`."""
        return [archive.id for archive in self.all_distro_archives]

    def _getMilestoneCondition(self):
        """See `HasMilestonesMixin`."""
        return Milestone.distribution == self

    def getArchiveIDList(self, archive=None):
        """See `IDistribution`."""
        if archive is None:
            return self.all_distro_archive_ids
        else:
            return [archive.id]

    def _getMirrors(
        self,
        content=None,
        enabled=True,
        status=MirrorStatus.OFFICIAL,
        by_country=False,
        needs_fresh=False,
        needs_cdimage_series=False,
    ):
        """Builds the query to get the mirror data for various purposes."""
        clauses = [
            DistributionMirror.distribution == self.id,
            DistributionMirror.status == status,
        ]
        if content is not None:
            clauses.append(DistributionMirror.content == content)
        if enabled is not None:
            clauses.append(DistributionMirror.enabled == enabled)
        if status != MirrorStatus.UNOFFICIAL:
            clauses.append(DistributionMirror.official_candidate == True)
        mirrors = list(Store.of(self).find(DistributionMirror, And(*clauses)))

        if by_country and mirrors:
            # Since country data is needed, fetch countries into the cache.
            list(
                Store.of(self).find(
                    Country,
                    Country.id.is_in(mirror.country_id for mirror in mirrors),
                )
            )

        if needs_fresh and mirrors:
            # Preload the distribution_mirrors' cache for mirror freshness.
            mirror_ids = [mirror.id for mirror in mirrors]

            arch_mirrors = list(
                Store.of(self)
                .find(
                    (
                        MirrorDistroArchSeries.distribution_mirror_id,
                        Max(MirrorDistroArchSeries.freshness),
                    ),
                    MirrorDistroArchSeries.distribution_mirror_id.is_in(
                        mirror_ids
                    ),
                )
                .group_by(MirrorDistroArchSeries.distribution_mirror_id)
            )
            arch_mirror_freshness = {}
            arch_mirror_freshness.update(
                [
                    (mirror_id, MirrorFreshness.items[mirror_freshness])
                    for (mirror_id, mirror_freshness) in arch_mirrors
                ]
            )

            source_mirrors = list(
                Store.of(self)
                .find(
                    (
                        MirrorDistroSeriesSource.distribution_mirror_id,
                        Max(MirrorDistroSeriesSource.freshness),
                    ),
                    MirrorDistroSeriesSource.distribution_mirror_id.is_in(
                        [mirror.id for mirror in mirrors]
                    ),
                )
                .group_by(MirrorDistroSeriesSource.distribution_mirror_id)
            )
            source_mirror_freshness = {}
            source_mirror_freshness.update(
                [
                    (mirror_id, MirrorFreshness.items[mirror_freshness])
                    for (mirror_id, mirror_freshness) in source_mirrors
                ]
            )

            for mirror in mirrors:
                cache = get_property_cache(mirror)
                cache.arch_mirror_freshness = arch_mirror_freshness.get(
                    mirror.id, None
                )
                cache.source_mirror_freshness = source_mirror_freshness.get(
                    mirror.id, None
                )

        if needs_cdimage_series and mirrors:
            all_cdimage_series = load_referencing(
                MirrorCDImageDistroSeries, mirrors, ["distribution_mirror_id"]
            )
            cdimage_series = defaultdict(list)
            for series in all_cdimage_series:
                cdimage_series[series.distribution_mirror_id].append(series)
            for mirror in mirrors:
                cache = get_property_cache(mirror)
                cache.cdimage_series = cdimage_series.get(mirror.id, [])

        return mirrors

    @property
    def archive_mirrors(self):
        """See `IDistribution`."""
        return self._getMirrors(content=MirrorContent.ARCHIVE)

    @property
    def archive_mirrors_by_country(self):
        """See `IDistribution`."""
        return self._getMirrors(
            content=MirrorContent.ARCHIVE, by_country=True, needs_fresh=True
        )

    @property
    def cdimage_mirrors(self, by_country=False):
        """See `IDistribution`."""
        return self._getMirrors(
            content=MirrorContent.RELEASE, needs_cdimage_series=True
        )

    @property
    def cdimage_mirrors_by_country(self):
        """See `IDistribution`."""
        return self._getMirrors(
            content=MirrorContent.RELEASE,
            by_country=True,
            needs_cdimage_series=True,
        )

    @property
    def disabled_mirrors(self):
        """See `IDistribution`."""
        return self._getMirrors(
            enabled=False, by_country=True, needs_fresh=True
        )

    @property
    def unofficial_mirrors(self):
        """See `IDistribution`."""
        return self._getMirrors(
            enabled=None,
            status=MirrorStatus.UNOFFICIAL,
            by_country=True,
            needs_fresh=True,
        )

    @property
    def pending_review_mirrors(self):
        """See `IDistribution`."""
        return self._getMirrors(
            enabled=None, by_country=True, status=MirrorStatus.PENDING_REVIEW
        )

    @property
    def drivers(self):
        """See `IDistribution`."""
        if self.driver is not None:
            return [self.driver]
        else:
            return [self.owner]

    @property
    def _sort_key(self):
        """Return something that can be used to sort distributions,
        putting Ubuntu and its major derivatives first.

        This is used to ensure that the list of distributions displayed in
        Soyuz generally puts Ubuntu at the top.
        """
        if self.name == "ubuntu":
            return (0, "ubuntu")
        if self.name in ["kubuntu", "xubuntu", "edubuntu"]:
            return (1, self.name)
        if "buntu" in self.name:
            return (2, self.name)
        return (3, self.name)

    @cachedproperty
    def series(self):
        """See `IDistribution`."""
        ret = Store.of(self).find(DistroSeries, distribution=self)
        return sorted(ret, key=lambda a: Version(a.version), reverse=True)

    @cachedproperty
    def derivatives(self):
        """See `IDistribution`."""
        ParentDistroSeries = ClassAlias(DistroSeries)
        # XXX rvb 2011-04-08 bug=754750: The clause
        # 'DistroSeries.distribution_id!=self.id' is only required
        # because the previous_series attribute has been (mis-)used
        # to denote other relations than proper derivation
        # relationships. We should be rid of this condition once
        # the bug is fixed.
        ret = Store.of(self).find(
            DistroSeries,
            ParentDistroSeries.id == DistroSeries.previous_series_id,
            ParentDistroSeries.distribution_id == self.id,
            DistroSeries.distribution_id != self.id,
        )
        return ret.config(distinct=True).order_by(
            Desc(DistroSeries.date_created)
        )

    @property
    def architectures(self):
        """See `IDistribution`."""
        architectures = []

        # Concatenate architectures list since they are distinct.
        for series in self.series:
            architectures += series.architectures

        return architectures

    @property
    def bugtargetdisplayname(self):
        """See IBugTarget."""
        return self.display_name

    @property
    def bugtargetname(self):
        """See `IBugTarget`."""
        return self.name

    def getBugSummaryContextWhereClause(self):
        """See BugTargetBase."""
        # Circular fail.
        from lp.bugs.model.bugsummary import BugSummary

        return And(
            BugSummary.distribution_id == self.id,
            BugSummary.sourcepackagename_id == None,
            BugSummary.ociproject_id == None,
        )

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this distribution."""
        search_params.setDistribution(self)

    def getBranchTips(self, user=None, since=None):
        """See `IDistribution`."""
        from lp.code.model.branch import Branch, get_branch_privacy_filter
        from lp.code.model.seriessourcepackagebranch import (
            SeriesSourcePackageBranch,
        )

        # This method returns thousands of branch unique names in a
        # single call, so the query is perilous and awkwardly tuned to
        # get a good plan with the normal dataset (the charms distro).
        OfficialSeries = ClassAlias(DistroSeries)

        ds_ids = Select(
            DistroSeries.id,
            tables=[DistroSeries],
            where=DistroSeries.distribution_id == self.id,
        )
        clauses = [
            DistroSeries.id.is_in(ds_ids),
            get_branch_privacy_filter(user),
        ]

        if since is not None:
            # If "since" was provided, take into account.
            clauses.append(Branch.last_scanned > since)

        branches = (
            IStore(self)
            .using(
                Branch,
                Join(DistroSeries, DistroSeries.id == Branch.distroseries_id),
                LeftJoin(
                    Join(
                        SeriesSourcePackageBranch,
                        OfficialSeries,
                        OfficialSeries.id
                        == SeriesSourcePackageBranch.distroseriesID,
                    ),
                    And(
                        SeriesSourcePackageBranch.branchID == Branch.id,
                        SeriesSourcePackageBranch.distroseriesID.is_in(ds_ids),
                    ),
                ),
            )
            .find(
                (
                    Branch.unique_name,
                    Branch.last_scanned_id,
                    OfficialSeries.name,
                ),
                And(*clauses),
            )
            .order_by(Branch.unique_name, Branch.last_scanned_id)
        )

        # Group on location (unique_name) and revision (last_scanned_id).
        result = []
        for key, group in itertools.groupby(branches, itemgetter(0, 1)):
            result.append(list(key))
            # Pull out all the official series names and append them as a list
            # to the end of the current record.
            result[-1].append(list(filter(None, map(itemgetter(2), group))))

        return result

    def getMirrorByName(self, name):
        """See `IDistribution`."""
        return (
            Store.of(self)
            .find(DistributionMirror, distribution=self, name=name)
            .one()
        )

    def getCountryMirror(self, country, mirror_type):
        """See `IDistribution`."""
        return (
            Store.of(self)
            .find(
                DistributionMirror,
                distribution=self,
                country=country,
                content=mirror_type,
                country_dns_mirror=True,
            )
            .one()
        )

    def getBestMirrorsForCountry(self, country, mirror_type):
        """See `IDistribution`."""
        # As per mvo's request we only return mirrors which have an
        # http_base_url.
        base_query = And(
            DistributionMirror.distribution == self,
            DistributionMirror.content == mirror_type,
            DistributionMirror.enabled,
            DistributionMirror.http_base_url != None,
            DistributionMirror.official_candidate == True,
            DistributionMirror.status == MirrorStatus.OFFICIAL,
        )
        query = And(DistributionMirror.country == country, base_query)
        # The list of mirrors returned by this method is fed to apt through
        # launchpad.net, so we order the results randomly in a lame attempt to
        # balance the load on the mirrors.
        order_by = [Func("random")]
        mirrors = shortlist(
            IStore(DistributionMirror)
            .find(DistributionMirror, query)
            .order_by(order_by),
            longest_expected=200,
        )

        if not mirrors and country is not None:
            query = And(
                Country.continent == country.continent,
                DistributionMirror.country == Country.id,
                base_query,
            )
            mirrors.extend(
                shortlist(
                    IStore(DistributionMirror)
                    .find(DistributionMirror, query)
                    .order_by(order_by),
                    longest_expected=300,
                )
            )

        if mirror_type == MirrorContent.ARCHIVE:
            main_mirror = getUtility(
                ILaunchpadCelebrities
            ).ubuntu_archive_mirror
        elif mirror_type == MirrorContent.RELEASE:
            main_mirror = getUtility(
                ILaunchpadCelebrities
            ).ubuntu_cdimage_mirror
        else:
            raise AssertionError("Unknown mirror type: %s" % mirror_type)
        assert main_mirror is not None, "Main mirror was not found"
        if main_mirror not in mirrors:
            mirrors.append(main_mirror)
        return mirrors

    def newMirror(
        self,
        owner,
        speed,
        country,
        content,
        display_name=None,
        description=None,
        http_base_url=None,
        https_base_url=None,
        ftp_base_url=None,
        rsync_base_url=None,
        official_candidate=False,
        enabled=False,
        whiteboard=None,
    ):
        """See `IDistribution`."""
        # NB this functionality is only available to distributions that have
        # the full functionality of Launchpad enabled. This is Ubuntu and
        # commercial derivatives that have been specifically given this
        # ability
        if not self.supports_mirrors:
            return None

        urls = {
            "http_base_url": http_base_url,
            "https_base_url": https_base_url,
            "ftp_base_url": ftp_base_url,
            "rsync_base_url": rsync_base_url,
        }
        for name, value in urls.items():
            if value is not None:
                urls[name] = IDistributionMirror[name].normalize(value)

        url = (
            urls["https_base_url"]
            or urls["http_base_url"]
            or urls["ftp_base_url"]
        )
        assert (
            url is not None
        ), "A mirror must provide at least one HTTP/HTTPS/FTP URL."
        host = urlparse(url).netloc
        name = sanitize_name("%s-%s" % (host, content.name.lower()))

        orig_name = name
        count = 1
        while self.getMirrorByName(name=name) is not None:
            count += 1
            name = "%s%s" % (orig_name, count)

        mirror = DistributionMirror(
            distribution=self,
            owner=owner,
            name=name,
            speed=speed,
            country=country,
            content=content,
            display_name=display_name,
            description=description,
            http_base_url=urls["http_base_url"],
            https_base_url=urls["https_base_url"],
            ftp_base_url=urls["ftp_base_url"],
            rsync_base_url=urls["rsync_base_url"],
            official_candidate=official_candidate,
            enabled=enabled,
            whiteboard=whiteboard,
        )
        IStore(DistributionMirror).add(mirror)
        return mirror

    @property
    def currentseries(self):
        """See `IDistribution`."""
        # XXX kiko 2006-03-18:
        # This should be just a selectFirst with a case in its
        # order by clause.

        # If we have a frozen one, return that.
        for series in self.series:
            if series.status == SeriesStatus.FROZEN:
                return series
        # If we have one in development, return that.
        for series in self.series:
            if series.status == SeriesStatus.DEVELOPMENT:
                return series
        # If we have a stable one, return that.
        for series in self.series:
            if series.status == SeriesStatus.CURRENT:
                return series
        # If we have ANY, return the first one.
        if len(self.series) > 0:
            return self.series[0]
        return None

    def __getitem__(self, name):
        for series in self.series:
            if series.name == name:
                return series
        raise NotFoundError(name)

    def __iter__(self):
        return iter(self.series)

    def getArchive(self, name):
        """See `IDistribution.`"""
        return getUtility(IArchiveSet).getByDistroAndName(self, name)

    def resolveSeriesAlias(self, name):
        """See `IDistribution`."""
        if self.development_series_alias == name:
            currentseries = self.currentseries
            if currentseries is not None:
                return currentseries
        raise NoSuchDistroSeries(name)

    def getSeries(self, name_or_version, follow_aliases=False):
        """See `IDistribution`."""
        distroseries = (
            Store.of(self)
            .find(
                DistroSeries,
                Or(
                    DistroSeries.name == name_or_version,
                    DistroSeries.version == name_or_version,
                ),
                DistroSeries.distribution == self,
            )
            .one()
        )
        if distroseries:
            return distroseries
        if follow_aliases:
            return self.resolveSeriesAlias(name_or_version)
        raise NoSuchDistroSeries(name_or_version)

    def getDevelopmentSeries(self):
        """See `IDistribution`."""
        return Store.of(self).find(
            DistroSeries, distribution=self, status=SeriesStatus.DEVELOPMENT
        )

    def getNonObsoleteSeries(self):
        """See `IDistribution`."""
        for series in self.series:
            if series.status != SeriesStatus.OBSOLETE:
                yield series

    def getMilestone(self, name):
        """See `IDistribution`."""
        return (
            Store.of(self).find(Milestone, distribution=self, name=name).one()
        )

    def getOCIProject(self, name):
        oci_project = getUtility(IOCIProjectSet).getByPillarAndName(self, name)
        return oci_project

    def getSourcePackage(self, name):
        """See `IDistribution`."""
        if ISourcePackageName.providedBy(name):
            sourcepackagename = name
        else:
            sourcepackagename = getUtility(ISourcePackageNameSet).queryByName(
                name
            )
            if sourcepackagename is None:
                return None
        return DistributionSourcePackage(self, sourcepackagename)

    def getExternalPackage(self, name, packagetype, channel):
        """See `IDistribution`."""
        if ISourcePackageName.providedBy(name):
            sourcepackagename = name
        else:
            sourcepackagename = getUtility(ISourcePackageNameSet).queryByName(
                name
            )
            if sourcepackagename is None:
                return None
        return ExternalPackage(self, sourcepackagename, packagetype, channel)

    def getSourcePackageRelease(self, sourcepackagerelease):
        """See `IDistribution`."""
        return DistributionSourcePackageRelease(self, sourcepackagerelease)

    def getCurrentSourceReleases(self, source_package_names):
        """See `IDistribution`."""
        return getUtility(IDistributionSet).getCurrentSourceReleases(
            {self: source_package_names}
        )

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
        """See `IHasSpecifications`.

        In the case of distributions, there are two kinds of filtering,
        based on:

          - completeness: we want to show INCOMPLETE if nothing is said
          - informationalness: we will show ANY if nothing is said

        """
        base_clauses = [Specification.distribution == self]
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
            .find(Specification, distribution=self, name=name)
            .one()
        )

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
            distribution=self,
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

        Defines distribution as self and sourcepackagename as None.
        """
        return {"distribution": self, "sourcepackagename": None}

    def questionIsForTarget(self, question):
        """See `QuestionTargetMixin`.

        Return True when the Question's distribution is self.
        """
        if question.distribution != self:
            return False
        return True

    def newFAQ(self, owner, title, content, keywords=None, date_created=None):
        """See `IFAQTarget`."""
        return FAQ.new(
            owner=owner,
            title=title,
            content=content,
            keywords=keywords,
            date_created=date_created,
            distribution=self,
        )

    def findReferencedOOPS(self, start_date, end_date):
        """See `IHasOOPSReferences`."""
        return list(
            referenced_oops(
                start_date,
                end_date,
                "distribution=%(distribution)s",
                {"distribution": self.id},
            )
        )

    def findSimilarFAQs(self, summary):
        """See `IFAQTarget`."""
        return FAQ.findSimilar(summary, distribution=self)

    def getFAQ(self, id):
        """See `IFAQCollection`."""
        return FAQ.getForTarget(id, self)

    def searchFAQs(self, search_text=None, owner=None, sort=None):
        """See `IFAQCollection`."""
        return FAQSearch(
            search_text=search_text, owner=owner, sort=sort, distribution=self
        ).getResults()

    def getDistroSeriesAndPocket(
        self, distroseries_name, follow_aliases=False
    ):
        """See `IDistribution`."""
        # Get the list of suffixes.
        suffixes = [suffix for suffix, ignored in suffixpocket.items()]
        # Sort it longest string first.
        suffixes.sort(key=len, reverse=True)

        for suffix in suffixes:
            if distroseries_name.endswith(suffix):
                left_size = len(distroseries_name) - len(suffix)
                left = distroseries_name[:left_size]
                try:
                    return self[left], suffixpocket[suffix]
                except KeyError:
                    if follow_aliases:
                        try:
                            resolved = self.resolveSeriesAlias(left)
                            return resolved, suffixpocket[suffix]
                        except NoSuchDistroSeries:
                            pass
                    # Swallow KeyError to continue round the loop.

        raise NotFoundError(distroseries_name)

    def getSeriesByStatus(self, status):
        """See `IDistribution`."""
        return Store.of(self).find(
            DistroSeries,
            DistroSeries.distribution == self,
            DistroSeries.status == status,
        )

    def getBuildRecords(
        self,
        build_state=None,
        name=None,
        pocket=None,
        arch_tag=None,
        user=None,
        binary_only=True,
    ):
        """See `IHasBuildRecords`"""
        # Ignore "user", since it would not make any difference to the
        # records returned here (private builds are only in PPA right
        # now).
        # The "binary_only" option is not yet supported for
        # IDistribution.
        return getUtility(IBinaryPackageBuildSet).getBuildsForDistro(
            self, build_state, name, pocket, arch_tag
        )

    def searchSourcePackageCaches(
        self, text, has_packaging=None, publishing_distroseries=None
    ):
        """See `IDistribution`."""
        from lp.registry.model.packaging import Packaging
        from lp.soyuz.model.distributionsourcepackagecache import (
            DistributionSourcePackageCache,
        )

        # The query below tries exact matching on the source package
        # name as well; this is because source package names are
        # notoriously bad for fti matching -- they can contain dots, or
        # be short like "at", both things which users do search for.
        store = Store.of(self)
        find_spec = (
            DistributionSourcePackageCache,
            SourcePackageName,
            SQL("ts_rank(fti, ftq(?)) AS rank", params=(text,)),
        )
        origin = [
            DistributionSourcePackageCache,
            Join(
                SourcePackageName,
                DistributionSourcePackageCache.sourcepackagename
                == SourcePackageName.id,
            ),
        ]

        conditions = [
            DistributionSourcePackageCache.distribution == self,
            Or(
                DistributionSourcePackageCache.archive_id.is_in(
                    self.all_distro_archive_ids
                ),
                DistributionSourcePackageCache.archive == None,
            ),
            Or(
                fti_search(DistributionSourcePackageCache, text),
                DistributionSourcePackageCache.name.contains_string(
                    text.lower()
                ),
            ),
        ]

        if has_packaging is not None:
            packaging_query = Exists(
                Select(
                    1,
                    tables=[Packaging],
                    where=(
                        Packaging.sourcepackagename_id == SourcePackageName.id
                    ),
                )
            )
            if has_packaging is False:
                packaging_query = Not(packaging_query)
            conditions.append(packaging_query)

        if publishing_distroseries is not None:
            origin.append(
                Join(
                    SourcePackagePublishingHistory,
                    SourcePackagePublishingHistory.sourcepackagename_id
                    == DistributionSourcePackageCache.sourcepackagename_id,
                )
            )
            conditions.extend(
                [
                    SourcePackagePublishingHistory.distroseries
                    == publishing_distroseries,
                    SourcePackagePublishingHistory.archive_id.is_in(
                        self.all_distro_archive_ids
                    ),
                ]
            )

        dsp_caches_with_ranks = (
            store.using(*origin)
            .find(find_spec, *conditions)
            .order_by(Desc(SQL("rank")), DistributionSourcePackageCache.name)
        )
        dsp_caches_with_ranks.config(distinct=True)
        return dsp_caches_with_ranks

    def searchSourcePackages(
        self, text, has_packaging=None, publishing_distroseries=None
    ):
        """See `IDistribution`."""

        dsp_caches_with_ranks = self.searchSourcePackageCaches(
            text,
            has_packaging=has_packaging,
            publishing_distroseries=publishing_distroseries,
        )

        # Create a function that will decorate the resulting
        # DistributionSourcePackageCaches, converting
        # them from the find_spec above into DSPs:
        def result_to_dsp(result):
            cache, source_package_name, rank = result
            return DistributionSourcePackage(self, source_package_name)

        # Return the decorated result set so the consumer of these
        # results will only see DSPs
        return DecoratedResultSet(dsp_caches_with_ranks, result_to_dsp)

    def searchBinaryPackages(self, package_name, exact_match=False):
        """See `IDistribution`."""
        from lp.soyuz.model.distributionsourcepackagecache import (
            DistributionSourcePackageCache,
        )

        store = Store.of(self)

        select_spec = (DistributionSourcePackageCache,)

        find_spec = (
            DistributionSourcePackageCache.distribution == self,
            DistributionSourcePackageCache.archive_id.is_in(
                self.all_distro_archive_ids
            ),
        )

        if exact_match:
            # To match BinaryPackageName.name exactly requires a very
            # slow 8 table join. So let's instead use binpkgnames, with
            # an ugly set of LIKEs matching spaces or either end of the
            # string on either side of the name. A regex is several
            # times slower and harder to escape.
            match_clause = (
                Or(
                    DistributionSourcePackageCache.binpkgnames.like(
                        "%% %s %%" % package_name.lower()
                    ),
                    DistributionSourcePackageCache.binpkgnames.like(
                        "%% %s" % package_name.lower()
                    ),
                    DistributionSourcePackageCache.binpkgnames.like(
                        "%s %%" % package_name.lower()
                    ),
                    DistributionSourcePackageCache.binpkgnames
                    == package_name.lower(),
                ),
            )
        else:
            # In this case we can use a simplified find-spec as the
            # binary package names are present on the
            # DistributionSourcePackageCache records.
            match_clause = (
                DistributionSourcePackageCache.binpkgnames.like(
                    "%%%s%%" % package_name.lower()
                ),
            )

        result_set = store.find(
            *(select_spec + find_spec + match_clause)
        ).config(distinct=True)

        return result_set.order_by(DistributionSourcePackageCache.name)

    def searchOCIProjects(self, text=None):
        """See `IDistribution`."""
        # circular import
        from lp.registry.model.ociproject import OCIProject

        store = Store.of(self)
        clauses = [OCIProject.distribution == self]
        if text is not None:
            clauses += [
                OCIProject.ociprojectname_id == OCIProjectName.id,
                OCIProjectName.name.contains_string(text),
            ]
        return store.find(OCIProject, *clauses)

    def guessPublishedSourcePackageName(self, pkgname):
        """See `IDistribution`"""
        assert isinstance(pkgname, str), "Expected string. Got: %r" % pkgname

        pkgname = pkgname.strip().lower()
        if not valid_name(pkgname):
            raise NotFoundError("Invalid package name: %s" % pkgname)

        if self.currentseries is None:
            # Distribution with no series can't have anything
            # published in it.
            raise NotFoundError(
                "%s has no series; %r was never "
                "published in it" % (self.displayname, pkgname)
            )

        sourcepackagename = getUtility(ISourcePackageNameSet).queryByName(
            pkgname
        )
        if sourcepackagename:
            # Note that in the source package case, we don't restrict
            # the search to the distribution release, making a best
            # effort to find a package.
            publishing = (
                IStore(SourcePackagePublishingHistory)
                .find(
                    SourcePackagePublishingHistory,
                    # We use an extra query to get the IDs instead of an
                    # inner join on archive because of the skewness in the
                    # archive data. (There are many, many PPAs to consider
                    # and PostgreSQL picks a bad query plan resulting in
                    # timeouts).
                    SourcePackagePublishingHistory.archive_id.is_in(
                        self.all_distro_archive_ids
                    ),
                    SourcePackagePublishingHistory.sourcepackagename
                    == sourcepackagename,
                    SourcePackagePublishingHistory.status.is_in(
                        active_publishing_status
                    ),
                )
                .order_by(Desc(SourcePackagePublishingHistory.id))
                .first()
            )
            if publishing is not None:
                return sourcepackagename

            # Look to see if there is an official source package branch.
            # That's considered "published" enough.
            branch_links = getUtility(IFindOfficialBranchLinks)
            results = branch_links.findForDistributionSourcePackage(
                self.getSourcePackage(sourcepackagename)
            )
            if results.any() is not None:
                return sourcepackagename

        # At this point we don't have a published source package by
        # that name, so let's try to find a binary package and work
        # back from there.
        binarypackagename = getUtility(IBinaryPackageNameSet).queryByName(
            pkgname
        )
        if binarypackagename:
            # Ok, so we have a binarypackage with that name. Grab its
            # latest publication in the distribution (this may be an old
            # package name the end-user is groping for) -- and then get
            # the sourcepackagename from that.
            bpph = (
                IStore(BinaryPackagePublishingHistory)
                .find(
                    BinaryPackagePublishingHistory,
                    # See comment above for rationale for using an extra query
                    # instead of an inner join. (Bottom line, it would time out
                    # otherwise.)
                    BinaryPackagePublishingHistory.archive_id.is_in(
                        self.all_distro_archive_ids
                    ),
                    BinaryPackagePublishingHistory.binarypackagename
                    == binarypackagename,
                    BinaryPackagePublishingHistory.status.is_in(
                        active_publishing_status
                    ),
                )
                .order_by(Desc(BinaryPackagePublishingHistory.id))
                .first()
            )
            if bpph is not None:
                spr = bpph.binarypackagerelease.build.source_package_release
                return spr.sourcepackagename

        # We got nothing so signal an error.
        if sourcepackagename is None:
            # Not a binary package name, not a source package name,
            # game over!
            if binarypackagename:
                raise NotFoundError(
                    "Binary package %s not published in %s"
                    % (pkgname, self.displayname)
                )
            else:
                raise NotFoundError("Unknown package: %s" % pkgname)
        else:
            raise NotFoundError(
                "Package %s not published in %s" % (pkgname, self.displayname)
            )

    # XXX cprov 20071024:  move this API to IArchiveSet, Distribution is
    # already too long and complicated.
    def getAllPPAs(self):
        """See `IDistribution`"""
        return (
            Store.of(self)
            .find(Archive, distribution=self, purpose=ArchivePurpose.PPA)
            .order_by("id")
        )

    def searchPPAs(self, text=None, show_inactive=False, user=None):
        """See `IDistribution`."""
        ValidPersonOrTeamCache = Table("ValidPersonOrTeamCache")
        clauses = [
            Archive.distribution == self,
            Archive.owner == Column("id", ValidPersonOrTeamCache),
        ]

        order_by = [Archive.displayname]

        if not show_inactive:
            clauses.append(
                Archive.id.is_in(
                    Select(
                        SourcePackagePublishingHistory.archive_id,
                        SourcePackagePublishingHistory.status.is_in(
                            active_publishing_status
                        ),
                    )
                )
            )

        if text:
            order_by.insert(0, rank_by_fti(Archive, text))
            clauses.append(fti_search(Archive, text))

        clauses.append(
            get_enabled_archive_filter(
                user, purpose=ArchivePurpose.PPA, include_public=True
            )
        )

        return IStore(Archive).find(Archive, *clauses).order_by(order_by)

    def getPendingAcceptancePPAs(self):
        """See `IDistribution`."""
        return (
            IStore(Archive)
            .find(
                Archive,
                Archive.purpose == ArchivePurpose.PPA,
                Archive.distribution == self,
                PackageUpload.archive == Archive.id,
                PackageUpload.status == PackageUploadStatus.ACCEPTED,
            )
            .order_by(Archive.id)
            .config(distinct=True)
        )

    def getPendingPublicationPPAs(self, archive_ids=None):
        """See `IDistribution`."""

        archive_ids_clause = []
        if archive_ids:
            archive_ids_clause = [Archive.id.is_in(archive_ids)]

        src_archives = (
            IStore(Archive)
            .find(
                Archive,
                *archive_ids_clause,
                Archive.purpose == ArchivePurpose.PPA,
                Archive.distribution == self,
                SourcePackagePublishingHistory.archive == Archive.id,
                SourcePackagePublishingHistory.scheduleddeletiondate == None,
                SourcePackagePublishingHistory.dateremoved == None,
                Or(
                    And(
                        SourcePackagePublishingHistory.status.is_in(
                            active_publishing_status
                        ),
                        SourcePackagePublishingHistory.datepublished == None,
                    ),
                    SourcePackagePublishingHistory.status
                    == PackagePublishingStatus.DELETED,
                ),
            )
            .order_by(Archive.id)
            .config(distinct=True)
        )

        bin_archives = (
            IStore(Archive)
            .find(
                Archive,
                *archive_ids_clause,
                Archive.purpose == ArchivePurpose.PPA,
                Archive.distribution == self,
                BinaryPackagePublishingHistory.archive == Archive.id,
                BinaryPackagePublishingHistory.scheduleddeletiondate == None,
                BinaryPackagePublishingHistory.dateremoved == None,
                Or(
                    And(
                        BinaryPackagePublishingHistory.status.is_in(
                            active_publishing_status
                        ),
                        BinaryPackagePublishingHistory.datepublished == None,
                    ),
                    BinaryPackagePublishingHistory.status
                    == PackagePublishingStatus.DELETED,
                ),
            )
            .order_by(Archive.id)
            .config(distinct=True)
        )

        reapable_af_archives = (
            IStore(Archive)
            .find(
                Archive,
                *archive_ids_clause,
                Archive.purpose == ArchivePurpose.PPA,
                Archive.distribution == self,
                ArchiveFile.archive == Archive.id,
                ArchiveFile.scheduled_deletion_date < UTC_NOW,
                ArchiveFile.date_removed == None,
            )
            .order_by(Archive.id)
            .config(distinct=True)
        )

        dirty_suites_archives = (
            IStore(Archive)
            .find(
                Archive,
                *archive_ids_clause,
                Archive.purpose == ArchivePurpose.PPA,
                Archive.distribution == self,
                Archive.dirty_suites != None,
            )
            .order_by(Archive.id)
        )

        deleting_archives = (
            IStore(Archive)
            .find(
                Archive,
                *archive_ids_clause,
                Archive.purpose == ArchivePurpose.PPA,
                Archive.distribution == self,
                Archive.status == ArchiveStatus.DELETING,
            )
            .order_by(Archive.id)
        )

        return (
            src_archives.union(bin_archives)
            .union(reapable_af_archives)
            .union(dirty_suites_archives)
            .union(deleting_archives)
        )

    def getArchiveByComponent(self, component_name):
        """See `IDistribution`."""
        # XXX Julian 2007-08-16
        # These component names should be Soyuz-wide constants.
        componentMapToArchivePurpose = {
            "main": ArchivePurpose.PRIMARY,
            "restricted": ArchivePurpose.PRIMARY,
            "universe": ArchivePurpose.PRIMARY,
            "multiverse": ArchivePurpose.PRIMARY,
            "partner": ArchivePurpose.PARTNER,
            "contrib": ArchivePurpose.PRIMARY,
            "non-free": ArchivePurpose.PRIMARY,
            "non-free-firmware": ArchivePurpose.PRIMARY,
        }

        try:
            # Map known components.
            return getUtility(IArchiveSet).getByDistroPurpose(
                self, componentMapToArchivePurpose[component_name]
            )
        except KeyError:
            # Otherwise we defer to the caller.
            return None

    def getAllowedBugInformationTypes(self):
        """See `IDistribution.`"""
        return BUG_POLICY_ALLOWED_TYPES[self.bug_sharing_policy]

    def getDefaultBugInformationType(self):
        """See `IDistribution.`"""
        return BUG_POLICY_DEFAULT_TYPES[self.bug_sharing_policy]

    def userCanEdit(self, user):
        """See `IDistribution`."""
        if user is None:
            return False
        admins = getUtility(ILaunchpadCelebrities).admin
        return user.inTeam(self.owner) or user.inTeam(admins)

    def canAdministerOCIProjects(self, person):
        """See `IDistribution`."""
        if person is None:
            return False
        if person.inTeam(self.oci_project_admin):
            return True
        person_roles = IPersonRoles(person)
        if person_roles.in_admin or person_roles.isOwner(self):
            return True
        return False

    def newSeries(
        self,
        name,
        display_name,
        title,
        summary,
        description,
        version,
        previous_series,
        registrant,
    ):
        """See `IDistribution`."""
        series = DistroSeries(
            distribution=self,
            name=name,
            display_name=display_name,
            title=title,
            summary=summary,
            description=description,
            version=version,
            status=SeriesStatus.EXPERIMENTAL,
            previous_series=previous_series,
            registrant=registrant,
        )
        if registrant.inTeam(self.driver) and not registrant.inTeam(
            self.owner
        ):
            # This driver is a release manager.
            series.driver = registrant

        # May wish to add this to the series rather than clearing the cache --
        # RBC 20100816.
        del get_property_cache(self).series

        IStore(series).flush()
        return series

    @property
    def has_published_binaries(self):
        """See `IDistribution`."""
        store = Store.of(self)
        results = store.find(
            BinaryPackagePublishingHistory,
            DistroArchSeries.distroseries == DistroSeries.id,
            DistroSeries.distribution == self,
            BinaryPackagePublishingHistory.distroarchseries
            == DistroArchSeries.id,
            BinaryPackagePublishingHistory.status
            == PackagePublishingStatus.PUBLISHED,
        ).config(limit=1)

        return not results.is_empty()

    def sharesTranslationsWithOtherSide(
        self, person, language, sourcepackage=None, purportedly_upstream=False
    ):
        """See `ITranslationPolicy`."""
        assert (
            sourcepackage is not None
        ), "Translations sharing policy requires a SourcePackage."

        if not sourcepackage.has_sharing_translation_templates:
            # There is no known upstream template or series.  Take the
            # uploader's word for whether these are upstream translations
            # (in which case they're shared) or not.
            # What are the consequences if that value is incorrect?  In
            # the case where translations from upstream are purportedly
            # from Ubuntu, we miss a chance at sharing when the package
            # is eventually matched up with a productseries.  An import
            # or sharing-script run will fix that.  In the case where
            # Ubuntu translations are purportedly from upstream, an
            # import can fix it once a productseries is selected; or a
            # merge done by a script will give precedence to the Product
            # translations for upstream.
            return purportedly_upstream

        productseries = sourcepackage.productseries
        return productseries.product.invitesTranslationEdits(person, language)

    def getBugTaskWeightFunction(self):
        """Provide a weight function to determine optimal bug task.

        Full weight is given to tasks for this distribution.

        Given that there must be a distribution task for a series of that
        distribution to have a task, we give no more weighting to a
        distroseries task than any other.
        """
        distribution_id = self.id

        def weight_function(bugtask):
            if bugtask.distribution_id == distribution_id:
                return OrderedBugTask(1, bugtask.id, bugtask)
            return OrderedBugTask(2, bugtask.id, bugtask)

        return weight_function

    @cachedproperty
    def has_published_sources(self):
        if not self.all_distro_archives:
            return False

        if (
            Store.of(self)
            .find(
                SourcePackagePublishingHistory,
                SourcePackagePublishingHistory.archive_id.is_in(
                    self.all_distro_archive_ids
                ),
            )
            .is_empty()
        ):
            return False
        return True

    def newOCIProject(self, registrant, name, description=None):
        """Create an `IOCIProject` for this distro."""
        if not getFeatureFlag(
            OCI_PROJECT_ALLOW_CREATE
        ) and not self.canAdministerOCIProjects(registrant):
            raise Unauthorized("Creating new OCI projects is not allowed.")
        return getUtility(IOCIProjectSet).new(
            pillar=self,
            registrant=registrant,
            name=name,
            description=description,
        )

    def setOCICredentials(
        self, registrant, registry_url, region, username, password
    ):
        """See `IDistribution`."""
        if not self.oci_project_admin:
            raise NoOCIAdminForDistribution()
        new_credentials = getUtility(IOCIRegistryCredentialsSet).getOrCreate(
            registrant,
            self.oci_project_admin,
            registry_url,
            {"username": username, "password": password, "region": region},
            override_owner=True,
        )
        old_credentials = self.oci_registry_credentials
        if self.oci_registry_credentials != new_credentials:
            # Remove the old credentials as we're assigning new ones
            # or clearing them
            self.oci_registry_credentials = new_credentials
            if old_credentials:
                old_credentials.destroySelf()

    def deleteOCICredentials(self):
        """See `IDistribution`."""
        old_credentials = self.oci_registry_credentials
        if old_credentials:
            self.oci_registry_credentials = None
            old_credentials.destroySelf()

    def newVulnerability(
        self,
        status,
        creator,
        information_type,
        importance=BugTaskImportance.UNDECIDED,
        cve=None,
        description=None,
        notes=None,
        mitigation=None,
        importance_explanation=None,
        date_made_public=None,
    ):
        """See `IDistribution`."""
        return getUtility(IVulnerabilitySet).new(
            self,
            status,
            importance,
            creator,
            information_type,
            cve,
            description,
            notes,
            mitigation,
            importance_explanation,
            date_made_public,
        )

    @cachedproperty
    def _known_viewers(self):
        """A set of known persons able to view this distribution."""
        return set()

    def userCanView(self, user):
        """See `IDistributionPublic`."""
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
        """See `IDistributionPublic`."""
        if self.userCanView(user):
            return True
        if user is None:
            return False
        return (
            not Store.of(self)
            .find(
                Distribution,
                Distribution.id == self.id,
                DistributionSet.getDistributionPrivacyFilter(user.person),
            )
            .is_empty()
        )

    def getVulnerabilitiesVisibleToUser(self, user, check_permissions=True):
        """See `IDistribution`."""

        privacy_filter = True
        if check_permissions:
            privacy_filter = get_vulnerability_privacy_filter(user)

        vulnerabilities = Store.of(self).find(
            Vulnerability,
            Vulnerability.distribution == self,
            privacy_filter,
        )
        vulnerabilities.order_by(Desc(Vulnerability.date_created))

        def preload_cves(rows):
            # Avoid circular import
            from lp.bugs.model.cve import Cve

            load_related(Cve, rows, ["cve_id"])

        return DecoratedResultSet(vulnerabilities, pre_iter_hook=preload_cves)

    @property
    def vulnerabilities(self):
        """See `IDistribution`."""
        return self.getVulnerabilitiesVisibleToUser(
            getUtility(ILaunchBag).user
        )

    def getVulnerability(self, vulnerability_id):
        """See `IDistribution`."""
        return (
            Store.of(self)
            .find(Vulnerability, distribution=self, id=vulnerability_id)
            .one()
        )

    @property
    def valid_webhook_event_types(self):
        return ["bug:0.1", "bug:comment:0.1"]


@implementer(IDistributionSet)
class DistributionSet:
    """This class is to deal with Distribution related stuff"""

    title = "Registered Distributions"

    def __iter__(self):
        """See `IDistributionSet`."""
        return iter(self.getDistros())

    def __getitem__(self, name):
        """See `IDistributionSet`."""
        distribution = self.getByName(name)
        if distribution is None:
            raise NotFoundError(name)
        return distribution

    def get(self, distributionid):
        """See `IDistributionSet`."""
        return IStore(Distribution).get(Distribution, distributionid)

    def count(self):
        """See `IDistributionSet`."""
        return IStore(Distribution).find(Distribution).count()

    def getDistros(self):
        """See `IDistributionSet`."""
        distros = IStore(Distribution).find(Distribution)
        return sorted(
            shortlist(distros, 100), key=lambda distro: distro._sort_key
        )

    def getByName(self, name):
        """See `IDistributionSet`."""
        pillar = getUtility(IPillarNameSet).getByName(name)
        if not IDistribution.providedBy(pillar):
            return None
        return pillar

    @staticmethod
    def getDistributionPrivacyFilter(user):
        # Anonymous users can only see public distributions.  This is also
        # sometimes used with an outer join with e.g. Product, so we let
        # NULL through too.
        public_filter = Or(
            Distribution._information_type == None,
            Distribution._information_type == InformationType.PUBLIC,
        )
        if user is None:
            return public_filter

        # (Commercial) admins can see any project.
        roles = IPersonRoles(user)
        if roles.in_admin or roles.in_commercial_admin:
            return True

        # Normal users can see any project for which they can see either
        # an entire policy or an artifact.
        # XXX wgrant 2015-06-26: This is slower than ideal for people in
        # teams with lots of artifact grants, as there can be tens of
        # thousands of APGF rows for a single policy. But it's tens of
        # milliseconds at most.
        grant_filter = Coalesce(
            ArrayIntersects(
                SQL("Distribution.access_policies"),
                Select(
                    ArrayAgg(AccessPolicyGrantFlat.policy_id),
                    tables=(
                        AccessPolicyGrantFlat,
                        Join(
                            TeamParticipation,
                            TeamParticipation.team_id
                            == AccessPolicyGrantFlat.grantee_id,
                        ),
                    ),
                    where=(TeamParticipation.person == user),
                ),
            ),
            False,
        )
        return Or(public_filter, grant_filter)

    def new(
        self,
        name,
        display_name,
        title,
        description,
        summary,
        domainname,
        members,
        owner,
        registrant,
        mugshot=None,
        logo=None,
        icon=None,
        vcs=None,
        information_type=None,
    ):
        """See `IDistributionSet`."""
        if information_type is None:
            information_type = InformationType.PUBLIC
        distro = Distribution(
            name=name,
            display_name=display_name,
            title=title,
            description=description,
            summary=summary,
            domainname=domainname,
            members=members,
            owner=owner,
            registrant=registrant,
            mugshot=mugshot,
            logo=logo,
            icon=icon,
            vcs=vcs,
            information_type=information_type,
        )
        IStore(distro).add(distro)
        getUtility(IArchiveSet).new(
            distribution=distro, owner=owner, purpose=ArchivePurpose.PRIMARY
        )
        if information_type != InformationType.PUBLIC:
            distro._ensure_complimentary_subscription()
        distro.setBugSharingPolicy(bug_policy_default[information_type])
        distro.setBranchSharingPolicy(branch_policy_default[information_type])
        distro.setSpecificationSharingPolicy(
            specification_policy_default[information_type]
        )
        return distro

    def getCurrentSourceReleases(self, distro_source_packagenames):
        """See `IDistributionSet`."""
        releases = get_current_source_releases(
            distro_source_packagenames,
            lambda distro: distro.all_distro_archive_ids,
            lambda distro: DistroSeries.distribution == distro,
            [
                SourcePackagePublishingHistory.distroseries_id
                == DistroSeries.id
            ],
            DistroSeries.distribution_id,
        )
        result = {}
        for spr, distro_id in releases:
            distro = getUtility(IDistributionSet).get(distro_id)
            result[distro.getSourcePackage(spr.sourcepackagename)] = (
                DistributionSourcePackageRelease(distro, spr)
            )
        return result

    def getDerivedDistributions(self):
        """See `IDistributionSet`."""
        ubuntu_id = getUtility(ILaunchpadCelebrities).ubuntu.id
        return (
            IStore(DistroSeries)
            .find(
                Distribution,
                Distribution.id == DistroSeries.distribution_id,
                DistroSeries.id == DistroSeriesParent.derived_series_id,
                DistroSeries.distribution_id != ubuntu_id,
            )
            .config(distinct=True)
        )
