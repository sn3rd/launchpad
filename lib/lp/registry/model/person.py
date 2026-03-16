# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Implementation classes for a Person."""

__all__ = [
    "get_person_visibility_terms",
    "get_recipients",
    "generate_nick",
    "IrcID",
    "IrcIDSet",
    "JabberID",
    "JabberIDSet",
    "JoinTeamEvent",
    "NicknameGenerationError",
    "Owner",
    "Person",
    "person_sort_key",
    "PersonLanguage",
    "PersonSet",
    "PersonSettings",
    "SocialAccount",
    "SocialAccountSet",
    "SSHKey",
    "SSHKeySet",
    "TeamInvitationEvent",
    "ValidPersonCache",
    "WikiName",
    "WikiNameSet",
]

import base64
import copy
import random
import re
import weakref
from datetime import datetime, timedelta, timezone
from operator import attrgetter, itemgetter

import six
import transaction
from lazr.delegates import delegate_to
from lazr.lifecycle.event import ObjectCreatedEvent
from lazr.restful.utils import get_current_browser_request, smartquote
from requests import PreparedRequest
from storm.databases.postgres import JSON
from storm.expr import (
    SQL,
    Alias,
    And,
    Coalesce,
    Column,
    Desc,
    Exists,
    Is,
    IsNot,
    Join,
    LeftJoin,
    Min,
    Not,
    Or,
    Select,
    Table,
    Union,
    Upper,
    With,
)
from storm.info import ClassAlias
from storm.locals import Bool, DateTime, Int, Reference, ReferenceSet, Unicode
from storm.store import EmptyResultSet, Store
from twisted.conch.ssh.common import getNS
from twisted.conch.ssh.keys import Key
from zope.component import adapter, getUtility
from zope.event import notify
from zope.interface import alsoProvides, classImplements, implementer
from zope.interface.interfaces import ComponentLookupError
from zope.publisher.interfaces import Unauthorized
from zope.security.checker import canAccess, canWrite
from zope.security.proxy import ProxyFactory, removeSecurityProxy

from lp import _
from lp.answers.enums import QuestionStatus
from lp.answers.interfaces.questionsperson import IQuestionsPerson
from lp.answers.model.questionsperson import QuestionsPersonMixin
from lp.app.enums import PRIVATE_INFORMATION_TYPES
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.validators.email import valid_email
from lp.app.validators.name import sanitize_name, valid_name
from lp.blueprints.enums import SpecificationFilter
from lp.blueprints.model.specification import (
    HasSpecificationsMixin,
    Specification,
)
from lp.blueprints.model.specificationsearch import (
    get_specification_active_product_filter,
    get_specification_privacy_filter,
    search_specifications,
)
from lp.blueprints.model.specificationworkitem import SpecificationWorkItem
from lp.bugs.interfaces.bugtarget import IBugTarget
from lp.bugs.interfaces.bugtask import BugTaskStatusSearch, IBugTaskSet
from lp.bugs.interfaces.bugtasksearch import (
    BugTaskSearchParams,
    get_person_bugtasks_search_params,
)
from lp.bugs.model.bugtarget import HasBugsBase
from lp.bugs.model.structuralsubscription import StructuralSubscription
from lp.code.interfaces.branchcollection import IAllBranches
from lp.code.interfaces.gitcollection import IGitCollection
from lp.code.model.hasbranches import (
    HasBranchesMixin,
    HasMergeProposalsMixin,
    HasRequestedReviewsMixin,
)
from lp.registry.enums import (
    EXCLUSIVE_TEAM_POLICY,
    INCLUSIVE_TEAM_POLICY,
    PersonVisibility,
    TeamMembershipPolicy,
    TeamMembershipRenewalPolicy,
)
from lp.registry.errors import (
    InvalidName,
    JoinNotAllowed,
    NameAlreadyTaken,
    NoSuchAccount,
    NotPlaceholderAccount,
    PPACreationError,
    TeamMembershipPolicyError,
)
from lp.registry.interfaces.accesspolicy import (
    IAccessPolicyGrantSource,
    IAccessPolicySource,
)
from lp.registry.interfaces.codeofconduct import ISignedCodeOfConductSet
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.gpg import IGPGKeySet
from lp.registry.interfaces.irc import IIrcID, IIrcIDSet
from lp.registry.interfaces.jabber import IJabberID, IJabberIDSet
from lp.registry.interfaces.karma import IKarmaActionSet
from lp.registry.interfaces.mailinglist import (
    IMailingListSet,
    MailingListStatus,
    PostedMessageStatus,
)
from lp.registry.interfaces.mailinglistsubscription import (
    MailingListAutoSubscribePolicy,
)
from lp.registry.interfaces.person import (
    AlreadyConvertedException,
    ImmutableVisibilityError,
    IPerson,
    IPersonSet,
    IPersonSettings,
    ITeam,
    NoAccountError,
    PersonalStanding,
    PersonCreationRationale,
    TeamEmailAddressError,
    validate_membership_policy,
    validate_public_person,
)
from lp.registry.interfaces.persontransferjob import IPersonMergeJobSource
from lp.registry.interfaces.product import IProduct, IProductSet
from lp.registry.interfaces.projectgroup import IProjectGroup
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.interfaces.socialaccount import (
    SOCIAL_PLATFORM_TYPES_MAP,
    ISocialAccount,
    ISocialAccountSet,
    SocialPlatformType,
    validate_social_account_identity,
)
from lp.registry.interfaces.ssh import (
    SSH_TEXT_TO_KEY_TYPE,
    ISSHKey,
    ISSHKeySet,
    SSHKeyAdditionError,
    SSHKeyType,
)
from lp.registry.interfaces.teammembership import (
    ACTIVE_STATES,
    IJoinTeamEvent,
    ITeamInvitationEvent,
    TeamMembershipStatus,
)
from lp.registry.interfaces.wikiname import IWikiName, IWikiNameSet
from lp.registry.model.codeofconduct import SignedCodeOfConduct
from lp.registry.model.karma import (
    Karma,
    KarmaAssignedEvent,
    KarmaCache,
    KarmaCategory,
    KarmaTotalCache,
)
from lp.registry.model.milestone import Milestone
from lp.registry.model.personlocation import PersonLocation
from lp.registry.model.pillar import PillarName
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.registry.model.teammembership import (
    TeamMembership,
    TeamMembershipSet,
    TeamParticipation,
)
from lp.services.config import config
from lp.services.database import bulk, postgresql
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.policy import PrimaryDatabasePolicy
from lp.services.database.sqlbase import (
    convert_storm_clause_to_string,
    cursor,
    sqlvalues,
)
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import WithMaterialized, fti_search
from lp.services.helpers import backslashreplace, shortlist
from lp.services.identity.interfaces.account import (
    INACTIVE_ACCOUNT_STATUSES,
    AccountCreationRationale,
    AccountDeceasedError,
    AccountStatus,
    AccountSuspendedError,
    IAccount,
    IAccountSet,
)
from lp.services.identity.interfaces.emailaddress import (
    VALID_EMAIL_STATUSES,
    EmailAddressAlreadyTaken,
    EmailAddressStatus,
    IEmailAddress,
    IEmailAddressSet,
    InvalidEmailAddress,
)
from lp.services.identity.model.account import Account
from lp.services.identity.model.emailaddress import EmailAddress, HasOwnerMixin
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.mail.helpers import (
    get_contact_email_addresses,
    get_email_template,
)
from lp.services.mail.sendmail import simple_sendmail
from lp.services.oauth.model import OAuthAccessToken, OAuthRequestToken
from lp.services.openid.adapters.openid import CurrentOpenIDEndPoint
from lp.services.openid.model.openididentifier import OpenIdIdentifier
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.searchbuilder import any as search_any
from lp.services.statistics.interfaces.statistic import ILaunchpadStatisticSet
from lp.services.verification.interfaces.authtoken import LoginTokenType
from lp.services.verification.interfaces.logintoken import ILoginTokenSet
from lp.services.verification.model.logintoken import LoginToken
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.publisher import canonical_url
from lp.services.worlddata.model.language import Language
from lp.soyuz.enums import ArchivePurpose, ArchiveStatus
from lp.soyuz.interfaces.archive import IArchiveSet, NoSuchPPA
from lp.soyuz.interfaces.archivesubscriber import IArchiveSubscriberSet
from lp.soyuz.model.archive import Archive, validate_ppa
from lp.soyuz.model.publishing import SourcePackagePublishingHistory
from lp.soyuz.model.reporting import LatestPersonSourcePackageReleaseCache
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease
from lp.translations.interfaces.translationsperson import ITranslationsPerson
from lp.translations.model.hastranslationimports import (
    HasTranslationImportsMixin,
)


@implementer(IJoinTeamEvent)
class JoinTeamEvent:
    """See `IJoinTeamEvent`."""

    def __init__(self, person, team):
        self.person = person
        self.team = team


@implementer(ITeamInvitationEvent)
class TeamInvitationEvent:
    """See `IJoinTeamEvent`."""

    def __init__(self, member, team):
        self.member = member
        self.team = team


class ValidPersonCache(StormBase):
    """Flags if a Person is active and usable in Launchpad.

    This is readonly, as this is a view in the database.

    Note that it performs poorly at least some of the time, and if
    EmailAddress and Person are already being queried, its probably better to
    query Account directly. See bug
    https://bugs.launchpad.net/launchpad/+bug/615237 for some
    corroborating information.
    """

    __storm_table__ = "ValidPersonCache"

    id = Int(primary=True)


def validate_person_visibility(person, attr, value):
    """Validate changes in visibility.

    Prevent teams with links to other objects from transitioning,
    ignoring known-OK links.
    """
    warning = person.visibilityConsistencyWarning(value)
    if warning is not None:
        raise ImmutableVisibilityError(warning)
    return value


def get_person_visibility_terms(user):
    """Generate the query needed for person privacy filtering."""
    public_filter = Person.visibility == PersonVisibility.PUBLIC

    # Anonymous users can only see public people.
    if user is None:
        return public_filter

    # Admins and commercial admins can see everyone.
    roles = IPersonRoles(user)
    if roles.in_admin or roles.in_commercial_admin:
        return True

    # Otherwise only public people and private teams of which the user
    # is a member are visible.
    return Or(
        public_filter,
        And(
            Person.id.is_in(
                Select(
                    TeamParticipation.team_id,
                    tables=[TeamParticipation],
                    where=(TeamParticipation.person == user),
                )
            ),
            Person.teamowner != None,
            Person.visibility != PersonVisibility.PUBLIC,
        ),
    )


_person_sort_re = re.compile(r"(?:[^\w\s]|[\d_])", re.U)


def person_sort_key(person):
    """Identical to `person_sort_key` in the database."""
    # Strip noise out of display_name. We do not have to bother with
    # name, as we know it is just plain ascii.
    display_name = _person_sort_re.sub("", person.display_name.lower())
    return "%s, %s" % (display_name.strip(), person.name)


@implementer(IPersonSettings)
class PersonSettings(StormBase):
    "The relatively rarely used settings for person (not a team)."

    __storm_table__ = "PersonSettings"

    person_id = Int("person", default=None, primary=True)
    person = Reference(person_id, "Person.id")

    selfgenerated_bugnotifications = Bool(allow_none=False, default=False)

    expanded_notification_footers = Bool(allow_none=True, default=False)

    require_strong_email_authentication = Bool(allow_none=True, default=False)


def readonly_settings(message, interface):
    """Make an object that disallows writes to values on the interface.

    When you write, the message is raised in a NotImplementedError.
    """

    # We will make a class that has properties for each field on the
    # interface (we expect every name on the interface to correspond to a
    # zope.schema field).  Each property will have a getter that will
    # return the interface default for that name; and it will have a
    # setter that will raise a hopefully helpful error message
    # explaining why writing is not allowed.
    # This is the setter we are going to use for every property.
    def unwritable(self, value):
        raise NotImplementedError(message)

    # This will become the dict of the class.
    data = {}
    # The interface.names() method returns the names on the interface.  If
    # "all" is True, then you will also get the names on base
    # interfaces.  That is unlikely to be needed here, but would be the
    # expected behaviour if it were.
    for name in interface.names(all=True):
        # This next line is a work-around for a classic problem of
        # closures in a loop. Closures are bound (indirectly) to frame
        # locals, which are a mutable collection. Therefore, if we
        # naively make closures for each different value within a loop,
        # each closure will be bound to the value as it is at the *end
        # of the loop*. That's usually not what we want. To prevent
        # this, we make a helper function (which has its own locals)
        # that returns the actual closure we want.
        closure_maker = lambda result: lambda self: result
        # Now we make a property with the name-specific getter and the generic
        # setter, and put it in the dictionary of the class we are making.
        data[name] = property(
            closure_maker(interface[name].default), unwritable
        )
    # Now we have all the attributes we want.  We will make the class...
    cls = type("Unwritable" + interface.__name__, (), data)
    # ...specify that the class implements the interface that we are working
    # with...
    classImplements(cls, interface)
    # ...and return an instance.  We should only need one, since it is
    # read-only.
    return cls()


_readonly_person_settings = readonly_settings(
    "Teams do not support changing this attribute.", IPersonSettings
)


@implementer(IPerson)
@delegate_to(IPersonSettings, context="_person_settings")
class Person(
    StormBase,
    HasBugsBase,
    HasSpecificationsMixin,
    HasTranslationImportsMixin,
    HasBranchesMixin,
    HasMergeProposalsMixin,
    HasRequestedReviewsMixin,
    QuestionsPersonMixin,
):
    """A Person."""

    __storm_table__ = "Person"

    id = Int(primary=True)

    _creating = False

    def __init__(
        self,
        name,
        display_name,
        account=None,
        teamowner=None,
        description=None,
        membership_policy=DEFAULT,
        defaultrenewalperiod=None,
        defaultmembershipperiod=None,
        creation_rationale=None,
        creation_comment=None,
        registrant=None,
        hide_email_addresses=False,
    ):
        super().__init__()
        self._creating = True
        self.name = name
        self.display_name = display_name
        self.account = account
        self.teamowner = teamowner
        self.description = description
        self.membership_policy = membership_policy
        self.defaultrenewalperiod = defaultrenewalperiod
        self.defaultmembershipperiod = defaultmembershipperiod
        self.creation_rationale = creation_rationale
        self.creation_comment = creation_comment
        self.registrant = registrant
        self.hide_email_addresses = hide_email_addresses
        if not self.is_team:
            # Initialize our PersonSettings object/record.  This is a
            # Person, not a team.  Teams may want a TeamSettings in the
            # future.
            settings = PersonSettings()
            settings.person = self
        self.__storm_loaded__()
        del self._creating

    def __storm_loaded__(self):
        """Mark the person as a team when created or fetched from database."""
        if self.is_team:
            alsoProvides(self, ITeam)

    @cachedproperty
    def _person_settings(self):
        if self.is_team:
            # Teams need to provide these attributes for reading in order for
            # things like snapshots to work, but they are not actually
            # pertinent to teams, so they are not actually implemented for
            # writes.
            return _readonly_person_settings
        else:
            # This is a person.
            return (
                IStore(PersonSettings)
                .find(PersonSettings, PersonSettings.person == self)
                .one()
            )

    @property
    def exported_id(self):
        """See `IPerson`."""
        # Unfortunately, none of the standard permissions are suitable for
        # what we need here: launchpad.Moderate comes closest, but it isn't
        # quite right because that permission is defined with
        # access_level="write", so it only works with OAuth tokens that have
        # write permission.  Instead, we have to just use launchpad.View and
        # do the security checks manually.
        user = getUtility(ILaunchBag).user
        if user is None:
            raise Unauthorized
        roles = IPersonRoles(user)
        if (
            roles.in_admin
            # Allowing commercial admins is a bit of a cheat, but it allows
            # IS automation to see Person.id
            # (https://portal.admin.canonical.com/C158967) without needing
            # to use an account that's a fully-fledged member of ~admins.
            or roles.in_commercial_admin
            or roles.in_registry_experts
        ):
            return self.id
        else:
            raise Unauthorized

    sortingColumns = SQL("person_sort_key(Person.displayname, Person.name)")
    # If we're using SELECT DISTINCT, then we can't use sortingColumns
    # unless `person_sort_key(Person.displayname, Person.name)` is also in
    # the select list, which usually isn't convenient.  Provide a separated
    # version instead.
    _separated_sortingColumns = ("Person.displayname", "Person.name")
    # When doing any sort of set operations (union, intersect, except_) with
    # Storm we can't use sortingColumns because the table name Person is not
    # available in that context, so we use this one.
    _sortingColumnsForSetOperations = SQL("person_sort_key(displayname, name)")
    __storm_order__ = sortingColumns
    _visibility_warning_cache_key = None
    _visibility_warning_cache = None

    account_id = Int(name="account", default=None)
    account = Reference(account_id, "Account.id")

    def _validate_name(self, attr, value):
        """Check that rename is allowed."""
        # Renaming a team is prohibited for any team that has a non-purged
        # mailing list.  This is because renaming a mailing list is not
        # trivial in Mailman 2.1 (see Mailman FAQ item 4.70).  We prohibit
        # such renames in the team edit details view, but just to be safe, we
        # also assert that such an attempt is not being made here.  Watch
        # out for when Storm is creating this row, because in that case
        # self.name isn't yet available.
        if self.name is None:
            mailing_list = None
        else:
            mailing_list = getUtility(IMailingListSet).get(self.name)
        can_rename = (
            self._creating
            or not self.is_team
            or mailing_list is None
            or mailing_list.status == MailingListStatus.PURGED
        )
        assert can_rename, "Cannot rename teams with mailing lists"
        # Everything's okay, so let Storm do the normal thing.
        return value

    name = Unicode(name="name", allow_none=False, validator=_validate_name)

    def __repr__(self):
        displayname = backslashreplace(self.displayname)
        return "<Person %s (%s)>" % (self.name, displayname)

    display_name = Unicode(name="displayname", allow_none=False)

    @property
    def displayname(self):
        return self.display_name

    teamdescription = Unicode(name="teamdescription", default=None)
    homepage_content = Unicode(default=None)
    _description = Unicode(name="description", default=None)
    icon_id = Int(name="icon", allow_none=True, default=None)
    icon = Reference(icon_id, "LibraryFileAlias.id")
    logo_id = Int(name="logo", allow_none=True, default=None)
    logo = Reference(logo_id, "LibraryFileAlias.id")
    mugshot_id = Int(name="mugshot", allow_none=True, default=None)
    mugshot = Reference(mugshot_id, "LibraryFileAlias.id")

    @property
    def account_status(self):
        if self.account is not None:
            return self.account.status
        else:
            return AccountStatus.NOACCOUNT

    @property
    def account_status_history(self):
        if self.account is not None:
            return self.account.status_history

    def setAccountStatus(self, status, user, comment):
        if self.is_team or self.account is None:
            raise NoAccountError()
        self.account.setStatus(status, user, comment)

    teamowner_id = Int(
        name="teamowner",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    teamowner = Reference(teamowner_id, "Person.id")

    sshkeys = ReferenceSet("id", "SSHKey.person_id")

    renewal_policy = DBEnum(
        enum=TeamMembershipRenewalPolicy,
        default=TeamMembershipRenewalPolicy.NONE,
    )
    membership_policy = DBEnum(
        name="subscriptionpolicy",
        enum=TeamMembershipPolicy,
        default=TeamMembershipPolicy.RESTRICTED,
        validator=validate_membership_policy,
    )
    defaultrenewalperiod = Int(name="defaultrenewalperiod", default=None)
    defaultmembershipperiod = Int(name="defaultmembershipperiod", default=None)
    mailing_list_auto_subscribe_policy = DBEnum(
        enum=MailingListAutoSubscribePolicy,
        default=MailingListAutoSubscribePolicy.ON_REGISTRATION,
    )

    merged_id = Int(name="merged", allow_none=True, default=None)
    merged = Reference(merged_id, "Person.id")

    datecreated = DateTime(
        allow_none=False, default=UTC_NOW, tzinfo=timezone.utc
    )
    creation_rationale = DBEnum(enum=PersonCreationRationale, default=None)
    creation_comment = Unicode(default=None)
    registrant_id = Int(
        name="registrant",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    registrant = Reference(registrant_id, "Person.id")
    hide_email_addresses = Bool(allow_none=False, default=False)
    verbose_bugnotifications = Bool(allow_none=False, default=True)

    signedcocs = ReferenceSet("id", "SignedCodeOfConduct.owner_id")
    _ircnicknames = ReferenceSet("id", "IrcID.person_id")
    jabberids = ReferenceSet("id", "JabberID.person_id")
    _social_accounts = ReferenceSet("id", "SocialAccount.person_id")

    visibility = DBEnum(
        enum=PersonVisibility,
        default=PersonVisibility.PUBLIC,
        validator=validate_person_visibility,
    )

    personal_standing = DBEnum(
        enum=PersonalStanding,
        default=PersonalStanding.UNKNOWN,
        allow_none=False,
    )

    personal_standing_reason = Unicode(default=None)

    @property
    def description(self):
        """See `IPerson`."""
        if self._description is not None:
            return self._description
        else:
            # Fallback to obsolete sources.
            texts = [
                val
                for val in [self.homepage_content, self.teamdescription]
                if val is not None
            ]
            if len(texts) > 0:
                return "\n".join(texts)
            return None

    @description.setter  # pyflakes:ignore
    def description(self, value):
        self._description = value
        self.homepage_content = None
        self.teamdescription = None

    @cachedproperty
    def ircnicknames(self):
        return list(self._ircnicknames)

    @cachedproperty
    def social_accounts(self):
        return list(self._social_accounts)

    # TODO: write test for this function once we have more
    # than one Social Platform Type.
    def getSocialAccountsByPlatform(self, platform):
        return list(
            getUtility(ISocialAccountSet).getByPersonAndSocialPlatform(
                self, platform
            )
        )

    @cachedproperty
    def languages(self):
        """See `IPerson`."""
        results = Store.of(self).find(
            Language,
            And(
                Language.id == PersonLanguage.language_id,
                PersonLanguage.person_id == self.id,
            ),
        )
        results.order_by(Language.englishname)
        return list(results)

    def getLanguagesCache(self):
        """Return this person's cached languages.

        :raises AttributeError: If the cache doesn't exist.
        """
        return get_property_cache(self).languages

    def setLanguagesCache(self, languages):
        """Set this person's cached languages.

        Order them by name if necessary.
        """
        get_property_cache(self).languages = sorted(
            languages, key=attrgetter("englishname")
        )

    def deleteLanguagesCache(self):
        """Delete this person's cached languages, if it exists."""
        del get_property_cache(self).languages

    def addLanguage(self, language):
        """See `IPerson`."""
        person_language = (
            Store.of(self)
            .find(
                PersonLanguage,
                And(
                    PersonLanguage.language_id == language.id,
                    PersonLanguage.person_id == self.id,
                ),
            )
            .one()
        )
        if person_language is not None:
            # Nothing to do.
            return
        PersonLanguage(person=self, language=language)
        self.deleteLanguagesCache()

    def removeLanguage(self, language):
        """See `IPerson`."""
        person_language = (
            Store.of(self)
            .find(
                PersonLanguage,
                And(
                    PersonLanguage.language_id == language.id,
                    PersonLanguage.person_id == self.id,
                ),
            )
            .one()
        )
        if person_language is None:
            # Nothing to do.
            return
        person_language.delete()
        self.deleteLanguagesCache()

    def convertToTeam(self, team_owner):
        """See `IPerson`."""
        if self.is_team:
            raise AlreadyConvertedException(
                "%s has already been converted to a team." % self.name
            )
        assert self.account_status == AccountStatus.NOACCOUNT, (
            "Only Person entries whose account_status is NOACCOUNT can be "
            "converted into teams."
        )
        # Teams don't have Account records
        if self.account is not None:
            account = self.account
            self.account = None
            Store.of(account).remove(account)
        self.creation_rationale = None
        self.teamowner = team_owner
        alsoProvides(self, ITeam)
        # Add the owner as a team admin manually because we know what we're
        # doing and we don't want any email notifications to be sent.
        TeamMembershipSet().new(
            team_owner, self, TeamMembershipStatus.ADMIN, team_owner
        )

    @property
    def oauth_access_tokens(self):
        """See `IPerson`."""
        return Store.of(self).find(
            OAuthAccessToken,
            OAuthAccessToken.person == self,
            Or(
                OAuthAccessToken.date_expires == None,
                OAuthAccessToken.date_expires > UTC_NOW,
            ),
        )

    @property
    def oauth_request_tokens(self):
        """See `IPerson`."""
        return Store.of(self).find(
            OAuthRequestToken,
            OAuthRequestToken.person == self,
            Or(
                OAuthRequestToken.date_expires == None,
                OAuthRequestToken.date_expires > UTC_NOW,
            ),
        )

    @property
    def latitude(self):
        """See `IHasLocation`.

        We no longer allow users to set their geographical location but we
        need to keep this because it was exported on version 1.0 of the API.
        """
        return None

    @property
    def longitude(self):
        """See `IHasLocation`.

        We no longer allow users to set their geographical location but we
        need to keep this because it was exported on version 1.0 of the API.
        """
        return None

    @cachedproperty
    def location(self):
        """See `IObjectWithLocation`."""
        location = (
            IStore(PersonLocation)
            .find(PersonLocation, PersonLocation.person == self)
            .one()
        )
        return location

    @property
    def time_zone(self):
        """See `IHasLocation`."""
        if self.location is None:
            return "UTC"
        # Wrap the location with a security proxy to make sure the user has
        # enough rights to see it.
        return ProxyFactory(self.location).time_zone

    def setLocation(self, latitude, longitude, time_zone, user):
        """See `ISetLocation`."""
        assert not self.is_team, "Cannot edit team location."
        assert (latitude is None and longitude is None) or (
            latitude is not None and longitude is not None
        ), "Cannot set a latitude without longitude (and vice-versa)."

        if self.location is not None:
            self.location.time_zone = six.ensure_text(time_zone)
            self.location.latitude = latitude
            self.location.longitude = longitude
            self.location.last_modified_by = user
            self.location.date_last_modified = UTC_NOW
        else:
            get_property_cache(self).location = PersonLocation(
                person=self,
                time_zone=time_zone,
                latitude=latitude,
                longitude=longitude,
                last_modified_by=user,
            )

    def findVisibleAssignedInProgressSpecs(self, user):
        """See `IPerson`."""
        return self.specifications(
            user,
            in_progress=True,
            quantity=5,
            sort=Desc(Specification.date_started),
            filter=[SpecificationFilter.ASSIGNEE],
        )

    @property
    def unique_displayname(self):
        """See `IPerson`."""
        return "%s (%s)" % (self.displayname, self.name)

    def specifications(
        self,
        user,
        sort=None,
        quantity=None,
        filter=None,
        in_progress=False,
        need_people=True,
        need_branches=True,
        need_workitems=False,
    ):
        """See `IHasSpecifications`."""
        from lp.blueprints.model.specificationsubscription import (
            SpecificationSubscription,
        )

        # Make a new copy of the filter, so that we do not mutate what we
        # were passed as a filter.
        if filter is None:
            filter = set()
        else:
            filter = set(filter)

        # Now look at the filter and fill in the unsaid bits.

        # Defaults for acceptance: in this case we have nothing to do
        # because specs are not accepted/declined against a person.

        # Defaults for informationalness: we don't have to do anything
        # because the default if nothing is said is ANY.

        roles = {
            SpecificationFilter.CREATOR,
            SpecificationFilter.ASSIGNEE,
            SpecificationFilter.DRAFTER,
            SpecificationFilter.APPROVER,
            SpecificationFilter.SUBSCRIBER,
        }
        # If no roles are given, then we want everything.
        if filter.intersection(roles) == set():
            filter.update(roles)
        role_clauses = []
        id_clauses = []
        if SpecificationFilter.CREATOR in filter:
            role_clauses.append(Specification.owner == self)
        if SpecificationFilter.ASSIGNEE in filter:
            role_clauses.append(Specification._assignee == self)
        if SpecificationFilter.DRAFTER in filter:
            role_clauses.append(Specification._drafter == self)
        if SpecificationFilter.APPROVER in filter:
            role_clauses.append(Specification._approver == self)
        if SpecificationFilter.SUBSCRIBER in filter:
            id_clauses.append(
                Specification.id.is_in(
                    Select(
                        SpecificationSubscription.specification_id,
                        [SpecificationSubscription.person == self],
                    )
                )
            )

        clauses = [Or(*role_clauses)] if role_clauses else None
        if SpecificationFilter.COMPLETE not in filter:
            if (
                in_progress
                and SpecificationFilter.INCOMPLETE not in filter
                and SpecificationFilter.ALL not in filter
            ):
                filter.update(
                    [
                        SpecificationFilter.INCOMPLETE,
                        SpecificationFilter.STARTED,
                    ]
                )

        return search_specifications(
            self,
            clauses,
            user,
            sort,
            quantity,
            list(filter),
            need_people=need_people,
            need_branches=need_branches,
            need_workitems=need_workitems,
            base_id_clauses=id_clauses,
        )

    # XXX: Tom Berger 2008-04-14 bug=191799:
    # The implementation of these functions
    # is no longer appropriate, since it now relies on subscriptions,
    # rather than package bug supervisors.
    def getBugSubscriberPackages(self):
        """See `IPerson`."""
        # Avoid circular imports.
        from lp.registry.model.distribution import Distribution
        from lp.registry.model.distributionsourcepackage import (
            DistributionSourcePackage,
        )

        origin = (
            StructuralSubscription,
            Join(
                Distribution,
                StructuralSubscription.distributionID == Distribution.id,
            ),
            Join(
                SourcePackageName,
                StructuralSubscription.sourcepackagenameID
                == SourcePackageName.id,
            ),
        )
        result = (
            Store.of(self)
            .using(*origin)
            .find(
                (Distribution, SourcePackageName),
                StructuralSubscription.subscriberID == self.id,
            )
        )
        result.order_by(SourcePackageName.name)

        def decorator(row):
            return DistributionSourcePackage(*row)

        return DecoratedResultSet(result, decorator)

    def findPathToTeam(self, team):
        """See `IPerson`."""
        # This is our guarantee that _getDirectMemberIParticipateIn() will
        # never return None
        assert self.hasParticipationEntryFor(
            team
        ), "%s doesn't seem to be a member/participant in %s" % (
            self.name,
            team.name,
        )
        assert team.is_team, "You can't pass a person to this method."
        path = [team]
        team = self._getDirectMemberIParticipateIn(team)
        while team != self:
            path.insert(0, team)
            team = self._getDirectMemberIParticipateIn(team)
        return path

    def _getDirectMemberIParticipateIn(self, team):
        """Return a direct member of the given team that this person
        participates in.

        If there are more than one direct member of the given team that this
        person participates in, the one with the oldest creation date is
        returned.
        """
        clauses = (
            TeamMembership.team_id == team.id,
            TeamMembership.person_id == Person.id,
            TeamMembership.status.is_in(ACTIVE_STATES),
            TeamParticipation.team_id == Person.id,
            TeamParticipation.person_id == self.id,
        )
        member = (
            IStore(Person)
            .find(Person, *clauses)
            .order_by(Person.datecreated)
            .first()
        )
        assert member is not None, (
            "%(person)s is an indirect member of %(team)s but %(person)s "
            "is not a participant in any direct member of %(team)s"
            % dict(person=self.name, team=team.name)
        )
        return member

    @property
    def is_team(self):
        """See `IPerson`."""
        return self.teamowner_id is not None

    @property
    def mailing_list(self):
        """See `IPerson`."""
        return getUtility(IMailingListSet).get(self.name)

    def _customizeSearchParams(self, search_params):
        """No-op, to satisfy a requirement of HasBugsBase."""
        pass

    def searchTasks(self, search_params, *args, **kwargs):
        """See `IHasBugs`."""
        prejoins = kwargs.pop("prejoins", [])
        if search_params is None and len(args) == 0:
            # this method is called via webapi directly
            # calling this method on a Person object directly via the
            # webservice API means searching for user related tasks
            user = kwargs.pop("user")
            search_params = get_person_bugtasks_search_params(
                user, self, **kwargs
            )
            return getUtility(IBugTaskSet).search(
                *search_params, prejoins=prejoins
            )
        if len(kwargs) > 0:
            # if keyword arguments are supplied, use the default
            # implementation in HasBugsBase.
            return HasBugsBase.searchTasks(
                self, search_params, prejoins=prejoins, **kwargs
            )
        else:
            # Otherwise pass all positional arguments to the
            # implementation in BugTaskSet.
            return getUtility(IBugTaskSet).search(
                search_params, *args, prejoins=prejoins
            )

    def getProjectsAndCategoriesContributedTo(self, user, limit=5):
        """See `IPerson`."""
        contributions = []
        results = self._getProjectsWithTheMostKarma(user, limit=limit)
        for product, distro, _karma in results:
            pillar = product or distro
            contributions.append(
                {
                    "project": pillar,
                    "categories": self._getContributedCategories(pillar),
                }
            )
        return contributions

    def _getProjectsWithTheMostKarma(self, user, limit=10):
        """Return the product/distribution and karma points of this person.

        Inactive products are ignored.

        The results are ordered descending by the karma points and limited to
        the given limit.
        """
        # We want this person's total karma on a given context (that is,
        # across all different categories) here; that's why we use a
        # "KarmaCache.category IS NULL" clause here.
        from lp.registry.model.distribution import (
            Distribution,
            DistributionSet,
        )
        from lp.registry.model.product import Product, ProductSet

        tableset = Store.of(self).using(
            KarmaCache,
            LeftJoin(Product, Product.id == KarmaCache.product_id),
            LeftJoin(
                Distribution, Distribution.id == KarmaCache.distribution_id
            ),
        )
        result = tableset.find(
            (Product, Distribution, KarmaCache.karmavalue),
            KarmaCache.person == self.id,
            KarmaCache.category == None,
            KarmaCache.projectgroup == None,
            Or(
                And(
                    Product.id != None,
                    Product.active == True,
                    ProductSet.getProductPrivacyFilter(user),
                ),
                And(
                    Distribution.id != None,
                    Distribution.active == True,
                    DistributionSet.getDistributionPrivacyFilter(user),
                ),
            ),
        )
        result.order_by(
            Desc(KarmaCache.karmavalue),
            Coalesce(Product.name, Distribution.name),
        )
        return result[:limit]

    def _genAffiliatedProductSql(self, user=None):
        """Helper to generate the product sql for getAffiliatePillars"""
        from lp.registry.model.product import ProductSet

        base_query = """
            SELECT name, 3 as kind, displayname
            FROM product
            WHERE
                product.active = True
                AND (
                    product.driver = %(person)s
                    OR product.owner = %(person)s
                    OR product.bug_supervisor = %(person)s
                )
        """ % sqlvalues(
            person=self.id
        )

        return "%s AND (%s)" % (
            base_query,
            convert_storm_clause_to_string(
                ProductSet.getProductPrivacyFilter(user)
            ),
        )

    def getAffiliatedPillars(self, user):
        """See `IPerson`."""
        find_spec = (PillarName, SQL("kind"), SQL("displayname"))
        base = """PillarName
                  JOIN (
                    %s
            """ % self._genAffiliatedProductSql(
            user=user
        )

        origin = (
            base
            + """
                UNION
                SELECT name, 2 as kind, displayname
                FROM project
                WHERE
                    active = True AND
                    (driver = %(person)s
                    OR owner = %(person)s)
                UNION
                SELECT name, 1 as kind, displayname
                FROM distribution
                WHERE
                    driver = %(person)s
                    OR owner = %(person)s
                    OR bug_supervisor = %(person)s
                ) _pillar
                ON PillarName.name = _pillar.name
            """
            % sqlvalues(person=self.id)
        )

        results = IStore(self).using(SQL(origin)).find(find_spec)
        results = results.order_by("kind", "displayname")

        def get_pillar_name(result):
            pillar_name, kind, displayname = result
            return pillar_name

        return DecoratedResultSet(results, get_pillar_name)

    def getOwnedProjects(self, match_name=None, transitive=False, user=None):
        """See `IPerson`."""
        # Import here to work around a circular import problem.
        from lp.registry.model.product import Product, ProductSet

        clauses = [
            Product.active == True,
        ]

        if transitive:
            clauses.extend(
                [
                    Product._owner_id == TeamParticipation.team_id,
                    TeamParticipation.person_id == self.id,
                ]
            )
        else:
            clauses.append(Product._owner_id == self.id)

        # We only want to use the extra query if match_name is not None and it
        # is not the empty string ('' or u'').
        if match_name:
            clauses.append(
                Or(
                    Product.name.contains_string(match_name),
                    Product.display_name.contains_string(match_name),
                    fti_search(Product, match_name),
                )
            )

        if user is not None:
            clauses.append(ProductSet.getProductPrivacyFilter(user))

        return (
            IStore(Product)
            .find(Product, *clauses)
            .config(distinct=True)
            .order_by(Product.display_name)
        )

    def isAnyPillarOwner(self):
        """See IPerson."""

        store = IStore(self)
        with_sql = [
            With(
                "teams",
                Select(
                    TeamParticipation.team_id,
                    where=(TeamParticipation.person == self),
                ),
            ),
            WithMaterialized(
                "owned_entities",
                store,
                SQL(
                    """
                 SELECT Product.id
                 FROM Product
                 WHERE Product.owner IN (SELECT team FROM teams)
                 UNION ALL
                 SELECT Project.id
                 FROM Project
                 WHERE Project.owner IN (SELECT team FROM teams)
                 UNION ALL
                 SELECT Distribution.id
                 FROM Distribution
                 WHERE Distribution.owner IN (SELECT team FROM teams)
                """
                ),
            ),
        ]
        rs = (
            store.with_(with_sql)
            .using("owned_entities")
            .find(
                SQL("count(*) > 0"),
            )
        )
        return rs.one()

    def hasCurrentCommercialSubscription(self):
        """See `IPerson`."""
        # Circular imports.
        from lp.registry.model.commercialsubscription import (
            CommercialSubscription,
        )
        from lp.registry.model.distribution import Distribution
        from lp.registry.model.product import Product
        from lp.registry.model.teammembership import TeamParticipation

        person = (
            Store.of(self)
            .using(
                Person,
                Join(
                    TeamParticipation, Person.id == TeamParticipation.person_id
                ),
                LeftJoin(
                    Product, TeamParticipation.team_id == Product._owner_id
                ),
                LeftJoin(
                    Distribution,
                    TeamParticipation.team_id == Distribution.owner_id,
                ),
                Join(
                    CommercialSubscription,
                    Or(
                        CommercialSubscription.product_id == Product.id,
                        CommercialSubscription.distribution_id
                        == Distribution.id,
                    ),
                ),
            )
            .find(
                Person,
                CommercialSubscription.date_expires
                > datetime.now(timezone.utc),
                Person.id == self.id,
            )
        )
        return not person.is_empty()

    def _getContributedCategories(self, pillar):
        """Return the KarmaCategories to which this person has karma on the
        given pillar.

        The given pillar must be either an IProduct or an IDistribution.
        """
        if IProduct.providedBy(pillar):
            pillar_clause = KarmaCache.product == pillar
        elif IDistribution.providedBy(pillar):
            pillar_clause = KarmaCache.distribution == pillar
        else:
            raise AssertionError(
                "Pillar must be a product or distro, got %s" % pillar
            )
        return (
            IStore(KarmaCategory)
            .using(
                KarmaCategory,
                Join(KarmaCache, KarmaCache.category == KarmaCategory.id),
            )
            .find(
                KarmaCategory,
                pillar_clause,
                IsNot(KarmaCache.category_id, None),
                KarmaCache.person == self,
            )
        )

    @property
    def karma_category_caches(self):
        """See `IPerson`."""
        store = Store.of(self)
        conditions = And(
            KarmaCache.category == KarmaCategory.id,
            KarmaCache.person == self.id,
            KarmaCache.product == None,
            KarmaCache.projectgroup == None,
            KarmaCache.distribution == None,
            KarmaCache.sourcepackagename == None,
        )
        result = store.find((KarmaCache, KarmaCategory), conditions)
        result = result.order_by(KarmaCategory.title)
        return [karma_cache for (karma_cache, category) in result]

    @cachedproperty
    def karma(self):
        """See `IPerson`."""
        # May also be loaded from _members
        cache = (
            IStore(KarmaTotalCache).find(KarmaTotalCache, person=self).one()
        )
        if cache is None:
            # Newly created accounts may not be in the cache yet, meaning the
            # karma updater script hasn't run since the account was created.
            return 0
        else:
            return cache.karma_total

    @property
    def is_valid_person_or_team(self):
        """See `IPerson`."""
        # Teams are always valid
        if self.is_team:
            return True

        return self.is_valid_person

    @cachedproperty
    def is_valid_person(self):
        """See `IPerson`."""
        # This is prepopulated by various queries in and out of person.py.
        if self.is_team:
            return False
        return (
            IStore(ValidPersonCache).get(ValidPersonCache, self.id) is not None
        )

    @property
    def is_probationary(self):
        """See `IPerson`.

        Users without karma have not demonstrated their intentions may not
        have the same privileges as users who have made contributions.
        """
        return not self.is_team and self.karma == 0

    def assignKarma(
        self,
        action_name,
        product=None,
        distribution=None,
        sourcepackagename=None,
        datecreated=None,
    ):
        """See `IPerson`."""
        # Teams don't get Karma. Inactive accounts don't get Karma.
        # The system user and janitor, does not get karma.
        # No warning, as we don't want to place the burden on callsites
        # to check this.
        if (
            not self.is_valid_person
            or self.id == getUtility(ILaunchpadCelebrities).janitor.id
        ):
            return None

        if product is not None:
            assert distribution is None and sourcepackagename is None
        elif distribution is not None:
            assert product is None
        else:
            raise AssertionError(
                "You must provide either a product or a distribution."
            )

        action = getUtility(IKarmaActionSet).getByName(action_name)
        if action is None:
            raise AssertionError(
                "No KarmaAction found with name '%s'." % action_name
            )

        if datecreated is None:
            datecreated = UTC_NOW
        karma = Karma(
            person=self,
            action=action,
            product=product,
            distribution=distribution,
            sourcepackagename=sourcepackagename,
            datecreated=datecreated,
        )
        Store.of(karma).flush()
        notify(KarmaAssignedEvent(self, karma))
        return karma

    def latestKarma(self, quantity=25):
        """See `IPerson`."""
        return (
            IStore(Karma)
            .find(Karma, person=self)
            .order_by(Desc(Karma.datecreated))[:quantity]
        )

    # This is to cache TeamParticipation information as that's used tons of
    # times in each request.
    _inTeam_cache = None

    def inTeam(self, team):
        """See `IPerson`."""
        if team is None:
            return False

        # Translate the team name to an ITeam if we were passed a team.
        if isinstance(team, str):
            team = PersonSet().getByName(team)
            if team is None:
                # No team, no membership.
                return False

        if self.id == team.id:
            # A team is always a member of itself.
            return True

        if not team.is_team:
            # It is possible that this team is really a user since teams
            # are users are often interchangeable.
            return False

        if self._inTeam_cache is None:
            # Initialize cache
            self._inTeam_cache = {}
        else:
            # Return from cache or fall through.
            try:
                return self._inTeam_cache[team.id]
            except KeyError:
                pass

        tp = (
            IStore(TeamParticipation)
            .find(TeamParticipation, team=team, person=self)
            .one()
        )
        in_team = tp is not None
        self._inTeam_cache[team.id] = in_team
        return in_team

    def inAnyTeam(self, teams):
        """See `IPerson`."""
        if not teams:
            return False

        team_ids = set()
        for team in teams:
            if self.id == team.id:
                # A team is always a member of itself
                return True
            if team.is_team:
                team_ids.add(team.id)

        if self._inTeam_cache is None:
            # Initialize cache
            self._inTeam_cache = {}
        else:
            if any(self._inTeam_cache.get(team_id) for team_id in team_ids):
                return True

        unknown_team_ids = {
            team_id
            for team_id in team_ids
            if team_id not in self._inTeam_cache
        }
        if not unknown_team_ids:
            return False

        found_team_ids = set(
            IStore(TeamParticipation).find(
                TeamParticipation.team_id,
                TeamParticipation.team_id.is_in(unknown_team_ids),
                TeamParticipation.person_id == self.id,
            )
        )

        for team_id in unknown_team_ids:
            self._inTeam_cache[team_id] = team_id in found_team_ids

        return bool(found_team_ids)

    def hasParticipationEntryFor(self, team):
        """See `IPerson`."""
        return (
            not IStore(TeamParticipation)
            .find(TeamParticipation, person=self, team=team)
            .is_empty()
        )

    def leave(self, team):
        """See `IPerson`."""
        assert not ITeam.providedBy(self)
        self.retractTeamMembership(team, self)

    def join(self, team, requester=None, may_subscribe_to_list=True):
        """See `IPerson`."""
        if self in team.activemembers:
            return

        if requester is None:
            assert (
                not self.is_team
            ), "You need to specify a reviewer when a team joins another."
            requester = self

        proposed = TeamMembershipStatus.PROPOSED
        approved = TeamMembershipStatus.APPROVED

        if team.membership_policy == TeamMembershipPolicy.RESTRICTED:
            raise JoinNotAllowed("This is a restricted team")
        elif (
            team.membership_policy == TeamMembershipPolicy.MODERATED
            or team.membership_policy == TeamMembershipPolicy.DELEGATED
        ):
            status = proposed
        elif team.membership_policy == TeamMembershipPolicy.OPEN:
            status = approved
        else:
            raise AssertionError(
                "Unknown membership policy: %s" % team.membership_policy
            )

        # XXX Edwin Grubbs 2007-12-14 bug=117980
        # removeSecurityProxy won't be necessary after addMember()
        # is configured to call a method on the new member, so the
        # security configuration will verify that the logged in user
        # has the right permission to add the specified person to the team.
        naked_team = removeSecurityProxy(team)
        naked_team.addMember(
            self,
            reviewer=requester,
            status=status,
            force_team_add=True,
            may_subscribe_to_list=may_subscribe_to_list,
        )

    def clearInTeamCache(self):
        """See `IPerson`."""
        self._inTeam_cache = {}

    def __storm_invalidated__(self):
        super().__storm_invalidated__()
        self.clearInTeamCache()

    @cachedproperty
    def participant_ids(self):
        """See `IPerson`."""
        return list(
            Store.of(self).find(
                TeamParticipation.person_id,
                TeamParticipation.team_id == self.id,
            )
        )

    def getAssignedSpecificationWorkItemsDueBefore(self, date, user):
        """See `IPerson`."""
        from lp.registry.model.distribution import Distribution
        from lp.registry.model.product import Product

        store = Store.of(self)

        # Since a workitem's assignee defaults to its specification's
        # assignee, the PostgreSQL planner isn't always able to work out
        # the selectivity of the filter. Put that in a CTE to force it
        # to calculate the workitems up front, rather than doing a hash
        # join over all of Specification and SpecificationWorkItem.
        assigned_specificationworkitem = WithMaterialized(
            "assigned_specificationworkitem",
            store,
            Union(
                Select(
                    SpecificationWorkItem.id,
                    where=And(
                        SpecificationWorkItem.assignee_id.is_in(
                            self.participant_ids
                        ),
                        Not(SpecificationWorkItem.deleted),
                    ),
                ),
                Select(
                    SpecificationWorkItem.id,
                    where=And(
                        SpecificationWorkItem.specification_id.is_in(
                            Select(
                                Specification.id,
                                where=Specification._assignee_id.is_in(
                                    self.participant_ids
                                ),
                            )
                        ),
                        Not(SpecificationWorkItem.deleted),
                    ),
                ),
                all=True,
            ),
        )

        origin = [Specification]
        productjoin, query = get_specification_active_product_filter(self)
        origin.extend(productjoin)
        query.append(
            Exists(
                Select(
                    1,
                    tables=[Specification],
                    where=And(
                        Specification.id
                        == SpecificationWorkItem.specification_id,
                        *get_specification_privacy_filter(user),
                    ),
                )
            )
        )
        origin.extend(
            [
                Join(
                    SpecificationWorkItem,
                    SpecificationWorkItem.specification == Specification.id,
                ),
                # WorkItems may not have a milestone and in that case they
                # inherit the one from the spec.
                Join(
                    Milestone,
                    Coalesce(
                        SpecificationWorkItem.milestone_id,
                        Specification.milestone_id,
                    )
                    == Milestone.id,
                ),
            ]
        )
        today = datetime.today().date()
        query.extend(
            [
                Milestone.dateexpected <= date,
                Milestone.dateexpected >= today,
                SpecificationWorkItem.id.is_in(
                    Select(SQL("id"), tables="assigned_specificationworkitem")
                ),
            ]
        )
        result = (
            store.with_(assigned_specificationworkitem)
            .using(*origin)
            .find(SpecificationWorkItem, *query)
        )
        result.config(distinct=True)

        def eager_load(workitems):
            specs = bulk.load_related(
                Specification, workitems, ["specification_id"]
            )
            bulk.load_related(Product, specs, ["product_id"])
            bulk.load_related(Distribution, specs, ["distribution_id"])
            assignee_ids = set(
                [workitem.assignee_id for workitem in workitems]
                + [spec._assignee_id for spec in specs]
            )
            assignee_ids.discard(None)
            bulk.load(Person, assignee_ids, store)
            milestone_ids = set(
                [workitem.milestone_id for workitem in workitems]
                + [spec.milestone_id for spec in specs]
            )
            milestone_ids.discard(None)
            bulk.load(Milestone, milestone_ids, store)

        return DecoratedResultSet(result, pre_iter_hook=eager_load)

    def getAssignedBugTasksDueBefore(self, date, user):
        """See `IPerson`."""
        from lp.bugs.model.bugtask import BugTask

        today = datetime.today().date()
        search_params = BugTaskSearchParams(
            user,
            assignee=search_any(*self.participant_ids),
            milestone_dateexpected_before=date,
            milestone_dateexpected_after=today,
        )

        # Cast to a list to avoid DecoratedResultSet running pre_iter_hook
        # multiple times when load_related() iterates over the tasks.
        tasks = list(getUtility(IBugTaskSet).search(search_params))
        # Eager load the things we need that are not already eager loaded by
        # BugTaskSet.search().
        bulk.load_related(Person, tasks, ["assignee_id"])
        bulk.load_related(Milestone, tasks, ["milestone_id"])

        for task in tasks:
            # We skip primaries (instead of replicas) from conjoined
            # relationships because we can do that without hitting the DB,
            # which would not be possible if we wanted to skip the replicas.
            # The simple (but expensive) way to skip the replicas would be
            # to skip any tasks that have a non-None .conjoined_primary.
            productseries = task.productseries
            distroseries = task.distroseries
            if productseries is not None and task.product is None:
                dev_focus_id = productseries.product.development_focus_id
                if (
                    productseries.id == dev_focus_id
                    and task.status not in BugTask._NON_CONJOINED_STATUSES
                ):
                    continue
            elif distroseries is not None:
                candidate = None
                for possible_replica in tasks:
                    sourcepackagename_id = (
                        possible_replica.sourcepackagename_id
                    )
                    if sourcepackagename_id == task.sourcepackagename_id:
                        candidate = possible_replica
                # Distribution.currentseries is expensive to run for every
                # bugtask (as it goes through every series of that
                # distribution), but it's a cached property and there's only
                # one distribution with bugs in LP, so we can afford to do
                # it here.
                if (
                    candidate is not None
                    and distroseries.distribution.currentseries == distroseries
                ):
                    continue
            yield task

    #
    # ITeam methods
    #
    @property
    def subscription_policy(self):
        """Obsolete API 1.0 property. See `IPerson`."""
        return self.membership_policy

    @subscription_policy.setter  # pyflakes:ignore
    def subscription_policy(self, value):
        self.membership_policy = value

    @property
    def super_teams(self):
        """See `IPerson`."""
        return PersonSet()._getPrecachedPersons(
            [
                Join(
                    Person,
                    TeamParticipation,
                    Person.id == TeamParticipation.team_id,
                )
            ],
            And(
                TeamParticipation.person_id == self.id,
                TeamParticipation.team_id != self.id,
            ),
            need_api=True,
        )

    @property
    def sub_teams(self):
        """See `IPerson`."""
        return PersonSet()._getPrecachedPersons(
            [
                Join(
                    Person,
                    TeamParticipation,
                    Person.id == TeamParticipation.person_id,
                )
            ],
            And(
                TeamParticipation.team_id == self.id,
                TeamParticipation.person_id != self.id,
                IsNot(Person.teamowner_id, None),
            ),
            need_api=True,
        )

    def getTeamAdminsEmailAddresses(self):
        """See `IPerson`."""
        if not self.is_team:
            raise ValueError("This method must only be used for teams.")
        to_addrs = set()
        for admin in self.adminmembers:
            to_addrs.update(get_contact_email_addresses(admin))
        if not to_addrs:
            to_addrs.update(get_contact_email_addresses(self.teamowner))
        return sorted(to_addrs)

    def addMember(
        self,
        person,
        reviewer,
        comment=None,
        force_team_add=False,
        status=TeamMembershipStatus.APPROVED,
        may_subscribe_to_list=True,
    ):
        """See `IPerson`."""
        if not self.is_team:
            raise ValueError("You cannot add members to a person.")
        if status not in [
            TeamMembershipStatus.APPROVED,
            TeamMembershipStatus.PROPOSED,
            TeamMembershipStatus.ADMIN,
        ]:
            raise ValueError(
                "You can't add a member with this status: %s." % status.name
            )

        event = JoinTeamEvent
        tm = (
            IStore(TeamMembership)
            .find(TeamMembership, person=person, team=self)
            .one()
        )
        if tm is not None:
            if tm.status == TeamMembershipStatus.ADMIN or (
                tm.status == TeamMembershipStatus.APPROVED
                and status == TeamMembershipStatus.PROPOSED
            ):
                status = tm.status
        if person.is_team:
            assert not self.hasParticipationEntryFor(person), (
                "Team '%s' is a member of '%s'. As a consequence, '%s' can't "
                "be added as a member of '%s'"
                % (self.name, person.name, person.name, self.name)
            )
            # By default, teams can only be invited as members, meaning that
            # one of the team's admins will have to accept the invitation
            # before the team is made a member. If force_team_add is True,
            # or the user is also an admin of the proposed member, then
            # we'll add a team as if it was a person.
            is_reviewer_admin_of_new_member = (
                person in reviewer.getAdministratedTeams()
            )
            if not force_team_add and not is_reviewer_admin_of_new_member:
                if tm is None or tm.status not in (
                    TeamMembershipStatus.PROPOSED,
                    TeamMembershipStatus.APPROVED,
                    TeamMembershipStatus.ADMIN,
                ):
                    status = TeamMembershipStatus.INVITED
                    event = TeamInvitationEvent
                else:
                    if tm.status == TeamMembershipStatus.PROPOSED:
                        status = TeamMembershipStatus.APPROVED
                    else:
                        status = tm.status

        status_changed = True
        expires = self.defaultexpirationdate
        if tm is None:
            tm = TeamMembershipSet().new(
                person,
                self,
                status,
                reviewer,
                dateexpires=expires,
                comment=comment,
            )
            Store.of(tm).flush()
            notify(event(person, self))
        else:
            # We can't use tm.setExpirationDate() here because the reviewer
            # here will be the member themselves when they join an OPEN team.
            tm.dateexpires = expires
            status_changed = tm.setStatus(status, reviewer, comment)

        if not person.is_team and may_subscribe_to_list:
            person.autoSubscribeToMailingList(
                self.mailing_list, requester=reviewer
            )
        return (status_changed, tm.status)

    def _accept_or_decline_membership(self, team, status, comment):
        tm = (
            IStore(TeamMembership)
            .find(TeamMembership, person=self, team=team)
            .one()
        )
        assert tm is not None
        assert tm.status == TeamMembershipStatus.INVITED
        tm.setStatus(status, getUtility(ILaunchBag).user, comment=comment)

    # The three methods below are not in the IPerson interface because we want
    # to protect them with a launchpad.Edit permission. We could do that by
    # defining explicit permissions for all IPerson methods/attributes in
    # the zcml but that's far from optimal given the size of IPerson.
    def acceptInvitationToBeMemberOf(self, team, comment):
        """Accept an invitation to become a member of the given team.

        There must be a TeamMembership for this person and the given team with
        the INVITED status. The status of this TeamMembership will be changed
        to APPROVED.
        """
        self._accept_or_decline_membership(
            team, TeamMembershipStatus.APPROVED, comment
        )

    def declineInvitationToBeMemberOf(self, team, comment):
        """Decline an invitation to become a member of the given team.

        There must be a TeamMembership for this person and the given team with
        the INVITED status. The status of this TeamMembership will be changed
        to INVITATION_DECLINED.
        """
        self._accept_or_decline_membership(
            team, TeamMembershipStatus.INVITATION_DECLINED, comment
        )

    def retractTeamMembership(self, team, user, comment=None):
        """See `IPerson`"""
        # Include PROPOSED and INVITED so that teams can retract mistakes
        # without involving members of the other team.
        active_and_transitioning = {
            TeamMembershipStatus.ADMIN: TeamMembershipStatus.DEACTIVATED,
            TeamMembershipStatus.APPROVED: TeamMembershipStatus.DEACTIVATED,
            TeamMembershipStatus.PROPOSED: TeamMembershipStatus.DECLINED,
            TeamMembershipStatus.INVITED: (
                TeamMembershipStatus.INVITATION_DECLINED
            ),
        }
        constraints = And(
            TeamMembership.person_id == self.id,
            TeamMembership.team_id == team.id,
            TeamMembership.status.is_in(active_and_transitioning.keys()),
        )
        tm = Store.of(self).find(TeamMembership, constraints).one()
        if tm is not None:
            # Flush the cache used by the inTeam method.
            self._inTeam_cache = {}
            new_status = active_and_transitioning[tm.status]
            tm.setStatus(new_status, user, comment=comment)

    def renewTeamMembership(self, team):
        """Renew the TeamMembership for this person on the given team.

        The given team's renewal policy must be ONDEMAND and the membership
        must be active (APPROVED or ADMIN) and set to expire in less than
        DAYS_BEFORE_EXPIRATION_WARNING_IS_SENT days.
        """
        tm = (
            IStore(TeamMembership)
            .find(TeamMembership, person=self, team=team)
            .one()
        )
        assert (
            tm.canBeRenewedByMember()
        ), "This membership can't be renewed by the member themselves."

        assert (
            team.defaultrenewalperiod is not None
            and team.defaultrenewalperiod > 0
        ), (
            "Teams with a renewal policy of ONDEMAND must specify "
            "a default renewal period greater than 0."
        )
        # Keep the same status, change the expiration date and send a
        # notification explaining the membership has been renewed.
        tm.dateexpires += timedelta(days=team.defaultrenewalperiod)
        tm.sendSelfRenewalNotification()

    def setMembershipData(
        self, person, status, reviewer, expires=None, comment=None
    ):
        """See `IPerson`."""
        tm = (
            IStore(TeamMembership)
            .find(TeamMembership, person=person, team=self)
            .one()
        )
        assert tm is not None
        tm.setExpirationDate(expires, reviewer)
        tm.setStatus(status, reviewer, comment=comment)

    def getOwnedTeams(self, user=None):
        """See `IPerson`."""
        query = And(
            get_person_visibility_terms(user),
            Person.teamowner == self.id,
            Person.merged == None,
        )
        return (
            IStore(Person)
            .find(Person, query)
            .order_by(Upper(Person.display_name), Upper(Person.name))
        )

    @cachedproperty
    def administrated_teams(self):
        return list(self.getAdministratedTeams())

    def getAdministratedTeams(self):
        """See `IPerson`."""

        class RestrictedParticipation:
            __storm_table__ = "RestrictedParticipation"
            team_id = Int(primary=True, name="team")

        store = Store.of(self)
        restricted_participation_cte = WithMaterialized(
            "RestrictedParticipation",
            store,
            Select(
                TeamParticipation.team_id,
                tables=[TeamParticipation],
                where=TeamParticipation.person == self,
            ),
        )
        team_select = Select(
            RestrictedParticipation.team_id, tables=[RestrictedParticipation]
        )

        return (
            store.with_(restricted_participation_cte)
            .find(
                Person,
                Person.id.is_in(
                    Union(
                        Select(
                            Person.id,
                            tables=[Person],
                            where=Person.teamowner_id.is_in(team_select),
                        ),
                        Select(
                            TeamMembership.team_id,
                            tables=[TeamMembership],
                            where=And(
                                TeamMembership.status
                                == TeamMembershipStatus.ADMIN,
                                TeamMembership.person_id.is_in(team_select),
                            ),
                        ),
                    )
                ),
                Person.merged == None,
            )
            .order_by(self._sortingColumnsForSetOperations)
        )

    def getDirectAdministrators(self):
        """See `IPerson`."""
        if not self.is_team:
            raise ValueError("This method must only be used for teams.")
        owner = IStore(Person).find(Person, id=self.teamowner.id)
        return self.adminmembers.union(owner).order_by(
            self._sortingColumnsForSetOperations
        )

    def getMembersByStatus(self, status, order_by=None):
        """See `IPerson`."""
        if order_by is None:
            order_by = Person.sortingColumns
        return (
            IStore(Person)
            .find(
                Person,
                TeamMembership.team == self,
                TeamMembership.status == status,
                TeamMembership.person == Person.id,
            )
            .order_by(order_by)
        )

    def _getEmailsByStatus(self, status):
        return Store.of(self).find(EmailAddress, person=self, status=status)

    def checkInclusiveMembershipPolicyAllowed(self, policy="open"):
        """See `ITeam`"""
        if not self.is_team:
            raise ValueError("This method must only be used for teams.")

        # Does this team own any pillars?
        if self.isAnyPillarOwner():
            raise TeamMembershipPolicyError(
                "The team membership policy cannot be %s because it "
                "maintains one or more projects, project groups, or "
                "distributions." % policy
            )

        # Does this team have any PPAs
        for ppa in self.ppas:
            if ppa.status != ArchiveStatus.DELETED:
                raise TeamMembershipPolicyError(
                    "The team membership policy cannot be %s because it "
                    "has one or more active PPAs." % policy
                )

        # Does this team have any super teams that are closed?
        for team in self.super_teams:
            if team.membership_policy in EXCLUSIVE_TEAM_POLICY:
                raise TeamMembershipPolicyError(
                    "The team membership policy cannot be %s because one "
                    "or more if its super teams are not open." % policy
                )

        # Does the team own a productseries.branch?
        if not getUtility(IAllBranches).ownedBy(self).isSeries().is_empty():
            raise TeamMembershipPolicyError(
                "The team membership policy cannot be %s because it owns "
                "or more branches linked to project series." % policy
            )
        # Does this team subscribe or is assigned to any private bugs.
        # Circular imports.
        from lp.bugs.model.bug import Bug
        from lp.bugs.model.bugsubscription import BugSubscription
        from lp.bugs.model.bugtask import BugTask

        # The team cannot be open if it is subscribed to or assigned to
        # private bugs.
        private_bugs_involved = IStore(Bug).execute(
            Union(
                Select(
                    Bug.id,
                    tables=(
                        Bug,
                        Join(
                            BugSubscription, BugSubscription.bug_id == Bug.id
                        ),
                    ),
                    where=And(
                        Bug.information_type.is_in(PRIVATE_INFORMATION_TYPES),
                        BugSubscription.person_id == self.id,
                    ),
                ),
                Select(
                    Bug.id,
                    tables=(Bug, Join(BugTask, BugTask.bug_id == Bug.id)),
                    where=And(
                        Bug.information_type.is_in(PRIVATE_INFORMATION_TYPES),
                        BugTask.assignee == self.id,
                    ),
                ),
                limit=1,
            )
        )
        if private_bugs_involved.rowcount:
            raise TeamMembershipPolicyError(
                "The team membership policy cannot be %s because it is "
                "subscribed to or assigned to one or more private "
                "bugs." % policy
            )

    def checkExclusiveMembershipPolicyAllowed(self, policy="closed"):
        """See `ITeam`"""
        if not self.is_team:
            raise ValueError("This method must only be used for teams.")

        # The team must be open if any of it's members are open.
        for member in self.activemembers:
            if member.membership_policy in INCLUSIVE_TEAM_POLICY:
                raise TeamMembershipPolicyError(
                    "The team membership policy cannot be %s because one "
                    "or more if its member teams are Open." % policy
                )

    @property
    def wiki_names(self):
        """See `IPerson`."""
        result = Store.of(self).find(WikiName, WikiName.person == self.id)
        return result.order_by(WikiName.wiki, WikiName.wikiname)

    @property
    def title(self):
        """See `IPerson`."""
        if self.is_team:
            return smartquote('"%s" team') % self.displayname
        return self.displayname

    @property
    def allmembers(self):
        """See `IPerson`."""
        return self._members(direct=False)

    @property
    def api_all_members(self):
        """See `IPerson`."""
        return self._members(direct=False, preload_for_api=True)

    @staticmethod
    def _validity_queries(person_table=None):
        """Return storm expressions and a decorator function for validity.

        Preloading validity implies preloading preferred email addresses.

        :param person_table: The person table to join to. Only supply if
            ClassAliases are in use.
        :return: A dict with four keys joins, tables, conditions, decorators

        * joins are additional joins to use. e.g. [LeftJoin,LeftJoin]
        * tables are tables to use e.g. [EmailAddress, Account]
        * decorators are callbacks to call for each row. Each decorator takes
        (Person, column) where column is the column in the result set for that
        decorators type.
        """
        if person_table is None:
            person_table = Person
            email_table = EmailAddress
            account_table = Account
        else:
            email_table = ClassAlias(EmailAddress)
            account_table = ClassAlias(Account)
        origins = []
        columns = []
        decorators = []
        # Teams don't have email, so a left join
        origins.append(
            LeftJoin(
                email_table,
                And(
                    email_table.person_id == person_table.id,
                    email_table.status == EmailAddressStatus.PREFERRED,
                ),
            )
        )
        columns.append(email_table)
        origins.append(
            LeftJoin(
                account_table,
                And(
                    person_table.account_id == account_table.id,
                    account_table.status == AccountStatus.ACTIVE,
                ),
            )
        )
        columns.append(account_table)

        def handleemail(person, column):
            # -- preferred email caching
            if not person:
                return
            email = column
            get_property_cache(person).preferredemail = email

        decorators.append(handleemail)

        def handleaccount(person, column):
            # -- validity caching
            if not person:
                return
            # valid if:
            valid = (
                # -- valid account found
                column is not None
                # -- preferred email found
                and person.preferredemail is not None
            )
            get_property_cache(person).is_valid_person = valid

        decorators.append(handleaccount)
        return dict(joins=origins, tables=columns, decorators=decorators)

    def _members(self, direct, status=None, preload_for_api=False):
        """Lookup all members of the team with optional precaching.

        :param direct: If True only direct members are returned.
        :param preload_for_api: Preload attributes contained in the API
            JSON representation.
        """
        # TODO: consolidate this with getMembersWithPreferredEmails.
        #       The difference between the two is that
        #       getMembersWithPreferredEmails includes self, which is arguably
        #       wrong, but perhaps deliberate.
        origin = [Person]
        if not direct:
            assert status is None
            origin.append(
                Join(TeamParticipation, TeamParticipation.person == Person.id)
            )
            conditions = And(
                # Members of this team,
                TeamParticipation.team == self.id,
                # But not the team itself.
                TeamParticipation.person != self.id,
            )
        else:
            if not isinstance(status, tuple):
                status = (status,)
            origin.append(
                Join(TeamMembership, TeamMembership.person_id == Person.id)
            )
            conditions = And(
                # Membership in this team,
                TeamMembership.team == self.id,
                # And approved or admin status
                TeamMembership.status.is_in(status),
            )
        # Use a PersonSet object that is not security proxied to allow
        # manipulation of the object.
        person_set = PersonSet()
        return person_set._getPrecachedPersons(
            origin, conditions, store=Store.of(self), need_api=preload_for_api
        )

    def _getMembersWithPreferredEmails(self):
        """Helper method for public getMembersWithPreferredEmails.

        We can't return the preferred email address directly to the
        browser code, since it would circumvent the security restrictions
        on accessing person.preferredemail.
        """
        store = Store.of(self)
        origin = [
            Person,
            Join(TeamParticipation, TeamParticipation.person == Person.id),
            Join(EmailAddress, EmailAddress.person == Person.id),
        ]
        conditions = And(
            TeamParticipation.team == self.id,
            EmailAddress.status == EmailAddressStatus.PREFERRED,
        )
        return store.using(*origin).find((Person, EmailAddress), conditions)

    def getMembersWithPreferredEmails(self):
        """See `IPerson`."""
        result = self._getMembersWithPreferredEmails()
        person_list = []
        for person, email in result:
            get_property_cache(person).preferredemail = email
            person_list.append(person)
        return person_list

    def getMembersWithPreferredEmailsCount(self):
        """See `IPerson`."""
        result = self._getMembersWithPreferredEmails()
        return result.count()

    @property
    def all_member_count(self):
        """See `IPerson`."""
        return self.allmembers.count()

    @property
    def invited_members(self):
        """See `IPerson`."""
        return self.getMembersByStatus(TeamMembershipStatus.INVITED)

    @property
    def api_invited_members(self):
        return self._members(
            True, status=TeamMembershipStatus.INVITED, preload_for_api=True
        )

    @property
    def invited_member_count(self):
        """See `IPerson`."""
        return self.invited_members.count()

    @property
    def deactivatedmembers(self):
        """See `IPerson`."""
        return self.getMembersByStatus(TeamMembershipStatus.DEACTIVATED)

    @property
    def api_deactivatedmembers(self):
        return self._members(
            True, status=TeamMembershipStatus.DEACTIVATED, preload_for_api=True
        )

    @property
    def deactivated_member_count(self):
        """See `IPerson`."""
        return self.deactivatedmembers.count()

    @property
    def expiredmembers(self):
        """See `IPerson`."""
        return self.getMembersByStatus(TeamMembershipStatus.EXPIRED)

    @property
    def api_expiredmembers(self):
        return self._members(
            True, status=TeamMembershipStatus.EXPIRED, preload_for_api=True
        )

    @property
    def expired_member_count(self):
        """See `IPerson`."""
        return self.expiredmembers.count()

    @property
    def proposedmembers(self):
        """See `IPerson`."""
        return self.getMembersByStatus(TeamMembershipStatus.PROPOSED)

    @property
    def api_proposedmembers(self):
        return self._members(
            True, status=TeamMembershipStatus.PROPOSED, preload_for_api=True
        )

    @property
    def proposed_member_count(self):
        """See `IPerson`."""
        return self.proposedmembers.count()

    @property
    def adminmembers(self):
        """See `IPerson`."""
        return self.getMembersByStatus(TeamMembershipStatus.ADMIN)

    @property
    def api_adminmembers(self):
        return self._members(
            True, status=TeamMembershipStatus.ADMIN, preload_for_api=True
        )

    @property
    def approvedmembers(self):
        """See `IPerson`."""
        return self.getMembersByStatus(TeamMembershipStatus.APPROVED)

    @property
    def activemembers(self):
        """See `IPerson`."""
        return self.approvedmembers.union(self.adminmembers).order_by(
            self._sortingColumnsForSetOperations
        )

    @property
    def api_activemembers(self):
        """See `IPerson`."""
        return self._members(
            direct=True, status=tuple(ACTIVE_STATES), preload_for_api=True
        )

    @property
    def active_member_count(self):
        """See `IPerson`."""
        return self.activemembers.count()

    @property
    def inactivemembers(self):
        """See `IPerson`."""
        return self.expiredmembers.union(self.deactivatedmembers).order_by(
            self._sortingColumnsForSetOperations
        )

    @property
    def inactive_member_count(self):
        """See `IPerson`."""
        return self.inactivemembers.count()

    @property
    def pendingmembers(self):
        """See `IPerson`."""
        return self.proposedmembers.union(self.invited_members).order_by(
            self._sortingColumnsForSetOperations
        )

    @property
    def team_memberships(self):
        """See `IPerson`."""
        Team = ClassAlias(Person, "Team")
        store = Store.of(self)
        # Join on team to sort by team names. Upper is used in the sort so
        # sorting works as is user expected, e.g. (A b C) not (A C b).
        return store.find(
            TeamMembership,
            TeamMembership.person == self,
            TeamMembership.team_id == Team.id,
            TeamMembership.status.is_in(ACTIVE_STATES),
        ).order_by(Upper(Team.display_name), Upper(Team.name))

    def anyone_can_join(self):
        open_types = (
            TeamMembershipPolicy.OPEN,
            TeamMembershipPolicy.DELEGATED,
        )
        return self.membership_policy in open_types

    @property
    def open_membership_invitations(self):
        """See `IPerson`."""
        return (
            IStore(TeamMembership)
            .find(
                TeamMembership,
                TeamMembership.person == self,
                TeamMembership.status == TeamMembershipStatus.INVITED,
                TeamMembership.team == Person.id,
            )
            .order_by(Person.sortingColumns)
        )

    def canDeactivate(self):
        """See `IPerson`."""
        # Users that own non-public products cannot be deactivated until the
        # products are reassigned.
        errors = []
        product_set = getUtility(IProductSet)
        non_public_products = product_set.get_users_private_products(self)
        if not non_public_products.is_empty():
            errors.append(
                (
                    "This account cannot be deactivated because it owns"
                    " the following non-public products: "
                )
                + ",".join([p.name for p in non_public_products])
            )

        if self.account_status != AccountStatus.ACTIVE:
            errors.append("This account is already deactivated.")

        return errors

    def preDeactivate(self, comment):
        self.account.setStatus(AccountStatus.DEACTIVATED, self, comment)
        for email in self.validatedemails:
            email.status = EmailAddressStatus.NEW
        self.preferredemail.status = EmailAddressStatus.NEW
        del get_property_cache(self).preferredemail

    def deactivate(self, comment=None, validate=True, pre_deactivate=True):
        """See `IPersonSpecialRestricted`."""
        if validate:
            # The person can only be deactivated if they do not own any
            # non-public products.
            errors = self.canDeactivate()
            assert not errors, " & ".join(errors)

        if pre_deactivate and not comment:
            raise AssertionError("Require a comment to deactivate.")

        # Set account status, and set all emails to NEW.
        if pre_deactivate:
            self.preDeactivate(comment)

        for membership in self.team_memberships:
            self.leave(membership.team)

        # Deactivate CoC signatures, unassign bug tasks and specs and reassign
        # pillars and teams.
        for coc in self.signedcocs:
            coc.active = False
        params = BugTaskSearchParams(self, assignee=self)
        for bug_task in self.searchTasks(params):
            # If the bugtask has a conjoined primary we don't try to
            # update it, since we will update it correctly when we
            # update its conjoined primary (see bug 193983).
            if bug_task.conjoined_primary is not None:
                continue

            # XXX flacoste 2007-11-26 bug=164635 The comparison using id in
            # the assert below works around a nasty intermittent failure.
            assert (
                bug_task.assignee.id == self.id
            ), "Bugtask %s assignee isn't the one expected: %s != %s" % (
                bug_task.id,
                bug_task.assignee.name,
                self.name,
            )
            bug_task.transitionToAssignee(None, validate=False)

        for spec in Store.of(self).find(Specification, _assignee=self):
            spec.assignee = None

        registry_experts = getUtility(ILaunchpadCelebrities).registry_experts
        for team in IStore(Person).find(Person, teamowner=self):
            team.teamowner = registry_experts
        for pillar_name in self.getAffiliatedPillars(self):
            pillar = pillar_name.pillar
            # XXX flacoste 2007-11-26 bug=164635 The comparison using id below
            # works around a nasty intermittent failure.
            changed = False
            if pillar.owner.id == self.id:
                pillar.owner = registry_experts
                changed = True
            if pillar.driver is not None and pillar.driver.id == self.id:
                pillar.driver = None
                changed = True

            # Products need to change the bug supervisor as well.
            if IProduct.providedBy(pillar):
                if (
                    pillar.bug_supervisor is not None
                    and pillar.bug_supervisor.id == self.id
                ):
                    pillar.bug_supervisor = None
                    changed = True

            if not changed:
                # Since we removed the person from all teams, something is
                # seriously broken here.
                raise AssertionError(
                    "%s was expected to be owner or driver of %s"
                    % (self.name, pillar.name)
                )

        # Nuke all subscriptions of this person.
        removals = [
            ("BranchSubscription", "person"),
            ("GitSubscription", "person"),
            ("BugSubscription", "person"),
            ("QuestionSubscription", "person"),
            ("SpecificationSubscription", "person"),
            ("AnswerContact", "person"),
            ("LatestPersonSourcePackageReleaseCache", "creator"),
            ("LatestPersonSourcePackageReleaseCache", "maintainer"),
        ]
        cur = cursor()
        for table, person_id_column in removals:
            cur.execute(
                "DELETE FROM %s WHERE %s=%d"
                % (table, person_id_column, self.id)
            )

        # Update the person's name.
        base_new_name = self.name + "-deactivatedaccount"
        self.name = self._ensureNewName(base_new_name)

    def _ensureNewName(self, base_new_name):
        """Return a unique name."""
        new_name = base_new_name
        count = 1
        while not IStore(Person).find(Person, name=new_name).is_empty():
            new_name = base_new_name + str(count)
            count += 1
        return new_name

    @property
    def private(self):
        """See `IPerson`."""
        if not self.is_team:
            return False
        elif self.visibility == PersonVisibility.PUBLIC:
            return False
        else:
            return True

    def isMergePending(self):
        """See `IPublicPerson`."""
        return (
            not getUtility(IPersonMergeJobSource)
            .find(from_person=self)
            .is_empty()
        )

    def visibilityConsistencyWarning(self, new_value):
        """Warning used when changing the team's visibility.

        A private-membership team cannot be connected to other
        objects, since it may be possible to infer the membership.
        """
        if self._visibility_warning_cache_key == new_value:
            return self._visibility_warning_cache

        cur = cursor()
        references = list(
            postgresql.listReferences(cur, "person", "id", indirect=False)
        )
        # These tables will be skipped since they do not risk leaking
        # team membership information.
        # Note all of the table names and columns must be all lowercase.
        skip = {
            ("accessartifactgrant", "grantee"),
            ("accessartifactgrant", "grantor"),
            ("accesspolicy", "person"),
            ("accesspolicygrant", "grantee"),
            ("accesspolicygrant", "grantor"),
            ("archive", "owner"),
            ("archivesubscriber", "subscriber"),
            ("branch", "owner"),
            ("branchsubscription", "person"),
            ("bugnotificationrecipient", "person"),
            ("bugsubscription", "person"),
            ("bugsummary", "viewed_by"),
            ("bugtask", "assignee"),
            ("codereviewvote", "reviewer"),
            ("emailaddress", "person"),
            ("gitactivity", "changee"),
            ("gitactivity", "changer"),
            ("gitrepository", "owner"),
            ("gitrule", "creator"),
            ("gitrulegrant", "grantee"),
            ("gitrulegrant", "grantor"),
            ("gitsubscription", "person"),
            ("gpgkey", "owner"),
            ("ircid", "person"),
            ("jabberid", "person"),
            ("karma", "person"),
            ("karmacache", "person"),
            ("karmatotalcache", "person"),
            ("livefs", "owner"),
            ("logintoken", "requester"),
            ("personlanguage", "person"),
            ("personlocation", "person"),
            ("personsettings", "person"),
            ("persontransferjob", "minor_person"),
            ("persontransferjob", "major_person"),
            ("product", "bug_supervisor"),
            ("product", "driver"),
            ("product", "owner"),
            ("productseries", "driver"),
            ("productseries", "owner"),
            ("sharingjob", "grantee"),
            ("signedcodeofconduct", "owner"),
            ("snapbuild", "requester"),
            ("socialaccount", "person"),
            ("specificationsubscription", "person"),
            ("sshkey", "person"),
            ("structuralsubscription", "subscriber"),
            ("teammembership", "acknowledged_by"),
            ("teammembership", "proposed_by"),
            ("teammembership", "reviewed_by"),
            ("teammembership", "team"),
            ("teamparticipation", "person"),
            ("teamparticipation", "team"),
            ("usertouseremail", "recipient"),
            # Skip mailing lists because if the mailing list is purged, it's
            # not a problem.  Do this check separately below.
            ("mailinglist", "team"),
            # The following is denormalised reporting data only loaded if the
            # user already has access to the team.
            ("latestpersonsourcepackagereleasecache", "creator"),
            ("latestpersonsourcepackagereleasecache", "maintainer"),
        }

        warnings = set()
        ref_query = []
        for (
            src_tab,
            src_col,
            _ref_tab,
            _ref_col,
            _updact,
            _delact,
        ) in references:
            if (src_tab, src_col) in skip:
                continue
            query = (
                "SELECT '%(table)s' AS table FROM %(table)s "
                "WHERE %(col)s = %(person_id)d"
                % {"col": src_col, "table": src_tab, "person_id": self.id}
            )
            if (src_tab, src_col) == ("teammembership", "person"):
                # XXX: this should be converted to the Storm query compiler
                # rather than doing fragile string assembly.
                query += (
                    " AND status != %(deactivated)d AND status != %(expired)d"
                    % {
                        "deactivated": TeamMembershipStatus.DEACTIVATED.value,
                        "expired": TeamMembershipStatus.EXPIRED.value,
                    }
                )
            ref_query.append(query)
        if ref_query:
            cur.execute(" UNION ".join(ref_query))
            for src_tab in cur.fetchall():
                table_name = (
                    src_tab[0] if isinstance(src_tab, tuple) else src_tab
                )
                if table_name[0] in "aeiou":
                    article = "an"
                else:
                    article = "a"
                warnings.add("%s %s" % (article, table_name))

        # Non-purged mailing list check for transitioning to or from PUBLIC.
        if PersonVisibility.PUBLIC in [self.visibility, new_value]:
            mailing_list = getUtility(IMailingListSet).get(self.name)
            if (
                mailing_list is not None
                and mailing_list.status != MailingListStatus.PURGED
            ):
                warnings.add("a mailing list")

        # Compose warning string.
        warnings = sorted(warnings)

        if len(warnings) == 0:
            self._visibility_warning_cache = None
            self._visibility_warning_cache_key = new_value
        else:
            if len(warnings) == 1:
                message = warnings[0]
            else:
                message = "%s and %s" % (
                    ", ".join(warnings[:-1]),
                    warnings[-1],
                )
            self._visibility_warning_cache = (
                "This team cannot be converted to %s since it is "
                "referenced by %s." % (new_value, message)
            )
            self._visibility_warning_cache_key = new_value
        return self._visibility_warning_cache

    @property
    def member_memberships(self):
        """See `IPerson`."""
        return self._getMembershipsByStatuses(ACTIVE_STATES)

    def getInactiveMemberships(self):
        """See `IPerson`."""
        return self._getMembershipsByStatuses(
            [TeamMembershipStatus.EXPIRED, TeamMembershipStatus.DEACTIVATED]
        )

    def getInvitedMemberships(self):
        """See `IPerson`."""
        return self._getMembershipsByStatuses([TeamMembershipStatus.INVITED])

    def getProposedMemberships(self):
        """See `IPerson`."""
        return self._getMembershipsByStatuses([TeamMembershipStatus.PROPOSED])

    def _getMembershipsByStatuses(self, statuses):
        """All `ITeamMembership`s in any given status for this team's members.

        :param statuses: A list of `TeamMembershipStatus` items.

        If called on an person rather than a team, this will obviously return
        no memberships at all.
        """
        return DecoratedResultSet(
            IStore(TeamMembership)
            .find(
                (TeamMembership, Person),
                TeamMembership.status.is_in(statuses),
                TeamMembership.person == Person.id,
                TeamMembership.team == self,
            )
            .order_by(Person.sortingColumns),
            result_decorator=itemgetter(0),
        )

    def getLatestApprovedMembershipsForPerson(self, limit=5):
        """See `IPerson`."""
        result = self.team_memberships
        result = result.order_by(
            Desc(TeamMembership.datejoined), Desc(TeamMembership.id)
        )
        return result[:limit]

    def getPathsToTeams(self):
        """See `Iperson`."""
        # Get all of the teams this person participates in.
        teams = list(self.teams_participated_in)

        # For cases where self is a team, we don't need self as a team
        # participated in.
        teams = [team for team in teams if team is not self]

        # Get all of the memberships for any of the teams this person is
        # a participant of. This must be ordered by date and id because
        # because the graph of the results will create needs to contain
        # the oldest path information to be consistent with results from
        # IPerson.findPathToTeam.
        store = Store.of(self)
        all_direct_memberships = store.find(
            TeamMembership,
            TeamMembership.person_id.is_in(
                [team.id for team in teams] + [self.id]
            ),
            TeamMembership.team != self,
            TeamMembership.status.is_in(ACTIVE_STATES),
        ).order_by(Desc(TeamMembership.datejoined), Desc(TeamMembership.id))
        # Cast the results to list now, because they will be iterated over
        # several times.
        all_direct_memberships = list(all_direct_memberships)

        # Pull out the memberships directly used by this person.
        user_memberships = [
            membership
            for membership in all_direct_memberships
            if membership.person == self
        ]

        all_direct_memberships = [
            (membership.team, membership.person)
            for membership in all_direct_memberships
        ]

        # Create a graph from the edges provided by the other data sets.
        graph = dict(all_direct_memberships)

        # Build the teams paths from that graph.
        paths = {}
        for team in teams:
            path = [team]
            step = team
            while path[-1] != self:
                step = graph[step]
                path.append(step)
            paths[team] = path
        return (paths, user_memberships)

    @property
    def teams_participated_in(self):
        """See `IPerson`."""
        return (
            IStore(Person)
            .find(
                Person,
                Person.id == TeamParticipation.team_id,
                TeamParticipation.person == self,
                IsNot(Person.teamowner_id, None),
            )
            .order_by(Person.sortingColumns)
        )

    @property
    def teams_indirectly_participated_in(self):
        """See `IPerson`."""
        store = Store.of(self)
        origin = [
            Person,
            Join(TeamParticipation, Person.id == TeamParticipation.team_id),
            LeftJoin(
                TeamMembership,
                And(
                    TeamMembership.person == self.id,
                    TeamMembership.team_id == TeamParticipation.team_id,
                    TeamMembership.status.is_in(ACTIVE_STATES),
                ),
            ),
        ]
        return store.using(*origin).find(
            Person,
            And(
                TeamParticipation.person == self.id,
                TeamParticipation.person != TeamParticipation.team_id,
                TeamMembership.id == None,
            ),
        )

    @property
    def teams_with_icons(self):
        """See `IPerson`."""
        return (
            IStore(Person)
            .find(
                Person,
                Person.id == TeamParticipation.team_id,
                TeamParticipation.person == self,
                IsNot(Person.teamowner_id, None),
                IsNot(Person.icon_id, None),
                TeamParticipation.team != self,
            )
            .order_by(Person.sortingColumns)
        )

    @property
    def defaultexpirationdate(self):
        """See `IPerson`."""
        days = self.defaultmembershipperiod
        if days:
            return datetime.now(timezone.utc) + timedelta(days)
        else:
            return None

    @property
    def defaultrenewedexpirationdate(self):
        """See `IPerson`."""
        days = self.defaultrenewalperiod
        if days:
            return datetime.now(timezone.utc) + timedelta(days)
        else:
            return None

    def reactivate(self, comment, preferred_email):
        """See `IPersonSpecialRestricted`."""
        self.account.reactivate(comment)
        self.setPreferredEmail(preferred_email)
        if "-deactivatedaccount" in self.name:
            # The name was changed by deactivate(). Restore the name, but we
            # must ensure it does not conflict with a current user.
            name_parts = self.name.split("-deactivatedaccount")
            base_new_name = name_parts[0]
            self.name = self._ensureNewName(base_new_name)

    def validateAndEnsurePreferredEmail(self, email):
        """See `IPerson`."""
        assert not self.is_team, "This method must not be used for teams."
        if not IEmailAddress.providedBy(email):
            raise TypeError(
                "Any person's email address must provide the IEmailAddress "
                "interface. %s doesn't." % email
            )
        assert email.person == self, "Wrong person! %r, %r" % (
            email.person_id,
            self.id,
        )

        # We need the preferred email address. This method is called
        # recursively, however, and the email address may have just been
        # created. So we have to explicitly pull it from the master store
        # until we rewrite this 'icky mess.
        preferred_email = (
            IStore(EmailAddress)
            .find(
                EmailAddress,
                EmailAddress.person == self,
                EmailAddress.status == EmailAddressStatus.PREFERRED,
            )
            .one()
        )

        # This email is already validated and is this person's preferred
        # email, so we have nothing to do.
        if preferred_email == email:
            return

        if preferred_email is None:
            # This branch will be executed only in the first time a person
            # uses Launchpad. Either when creating a new account or when
            # resetting the password of an automatically created one.
            self.setPreferredEmail(email)
        else:
            email.status = EmailAddressStatus.VALIDATED

    def setContactAddress(self, email):
        """See `IPerson`."""
        if not self.is_team:
            raise ValueError("This method must only be used for teams.")

        if email is None:
            self._unsetPreferredEmail()
        else:
            self._setPreferredEmail(email)
        # A team can have up to two addresses, the preferred one and one used
        # by the team mailing list.
        if (
            self.mailing_list is not None
            and self.mailing_list.status != MailingListStatus.PURGED
        ):
            mailing_list_email = getUtility(IEmailAddressSet).getByEmail(
                self.mailing_list.address
            )
        else:
            mailing_list_email = None
        all_addresses = IStore(EmailAddress).find(EmailAddress, person=self)
        for address in all_addresses:
            # Delete all email addresses that are not the preferred email
            # address, or the team's email address. If this method was called
            # with None, and there is no mailing list, then this condition
            # is (None, None), causing all email addresses to be deleted.
            if address not in (email, mailing_list_email):
                address.destroySelf()

    def _unsetPreferredEmail(self):
        """Change the preferred email address to VALIDATED."""
        email_address = (
            IStore(EmailAddress)
            .find(
                EmailAddress,
                person=self,
                status=EmailAddressStatus.PREFERRED,
            )
            .one()
        )
        if email_address is not None:
            email_address.status = EmailAddressStatus.VALIDATED
        del get_property_cache(self).preferredemail

    def setPreferredEmail(self, email):
        """See `IPerson`."""
        assert not self.is_team, "This method must not be used for teams."
        if email is None:
            self._unsetPreferredEmail()
            return
        self._setPreferredEmail(email)

    def _setPreferredEmail(self, email):
        """Set this person's preferred email to the given email address.

        If the person already has an email address, then its status is
        changed to VALIDATED and the given one is made its preferred one.

        The given email address must implement IEmailAddress and be owned by
        this person.
        """
        if not IEmailAddress.providedBy(email):
            raise TypeError(
                "Any person's email address must provide the IEmailAddress "
                "interface. %s doesn't." % email
            )
        assert email.person == self
        existing_preferred_email = (
            IStore(EmailAddress)
            .find(
                EmailAddress,
                person=self,
                status=EmailAddressStatus.PREFERRED,
            )
            .one()
        )
        if existing_preferred_email is not None:
            original_recipients = existing_preferred_email.email
            existing_preferred_email.status = EmailAddressStatus.VALIDATED
        else:
            original_recipients = None

        email = removeSecurityProxy(email)
        email.status = EmailAddressStatus.PREFERRED

        # Now we update our cache of the preferredemail.
        get_property_cache(self).preferredemail = email
        if original_recipients:
            self.security_field_changed(
                "Preferred email address changed on Launchpad.",
                "Your preferred email address is now <%s>." % email.email,
                original_recipients,
            )

    @cachedproperty
    def preferredemail(self):
        """See `IPerson`."""
        emails = self._getEmailsByStatus(EmailAddressStatus.PREFERRED)
        # There can be only one preferred email for a given person at a
        # given time, and this constraint must be ensured in the DB, but
        # it's not a problem if we ensure this constraint here as well.
        emails = shortlist(emails)
        length = len(emails)
        assert length <= 1
        if length:
            return emails[0]
        else:
            return None

    @property
    def safe_email_or_blank(self):
        """See `IPerson`."""
        if self.preferredemail is not None and not self.hide_email_addresses:
            return self.preferredemail.email
        else:
            return ""

    @property
    def validatedemails(self):
        """See `IPerson`."""
        return self._getEmailsByStatus(EmailAddressStatus.VALIDATED)

    @property
    def unvalidatedemails(self):
        """See `IPerson`."""
        return sorted(
            {
                token.email
                for token in IStore(LoginToken).find(
                    LoginToken,
                    LoginToken.requester == self,
                    LoginToken.tokentype.is_in(
                        (
                            LoginTokenType.VALIDATEEMAIL,
                            LoginTokenType.VALIDATETEAMEMAIL,
                        )
                    ),
                    Is(LoginToken.date_consumed, None),
                )
            }
        )

    @property
    def guessedemails(self):
        """See `IPerson`."""
        return self._getEmailsByStatus(EmailAddressStatus.NEW)

    @property
    def pending_gpg_keys(self):
        """See `IPerson`."""
        logintokenset = getUtility(ILoginTokenSet)
        return sorted(
            {
                token.fingerprint
                for token in logintokenset.getPendingGPGKeys(
                    requesterid=self.id
                )
            }
        )

    @property
    def inactive_gpg_keys(self):
        """See `IPerson`."""
        gpgkeyset = getUtility(IGPGKeySet)
        return gpgkeyset.getGPGKeysForPerson(self, active=False)

    @property
    def gpg_keys(self):
        """See `IPerson`."""
        gpgkeyset = getUtility(IGPGKeySet)
        return gpgkeyset.getGPGKeysForPerson(self)

    def hasMaintainedPackages(self):
        """See `IPerson`."""
        return self._hasReleasesQuery()

    def hasUploadedButNotMaintainedPackages(self):
        """See `IPerson`."""
        return self._hasReleasesQuery(uploader_only=True)

    def hasUploadedPPAPackages(self):
        """See `IPerson`."""
        return self._hasReleasesQuery(uploader_only=True, ppa_only=True)

    def getLatestMaintainedPackages(self):
        """See `IPerson`."""
        return self._latestReleasesQuery()

    def getLatestUploadedButNotMaintainedPackages(self):
        """See `IPerson`."""
        return self._latestReleasesQuery(uploader_only=True)

    def getLatestUploadedPPAPackages(self):
        """See `IPerson`."""
        return self._latestReleasesQuery(uploader_only=True, ppa_only=True)

    def _releasesQueryFilter(self, uploader_only=False, ppa_only=False):
        """Return the filter used to find latest published source package
        releases (SPRs) related to this person.

        :param uploader_only: controls if we are interested in SPRs where
            the person in question is only the uploader (creator) and not the
            maintainer (debian-syncs) if the `ppa_only` parameter is also
            False, or, if the flag is False, it returns all SPR maintained
            by this person.

        :param ppa_only: controls if we are interested only in source
            package releases targeted to any PPAs or, if False, sources
            targeted to primary archives.

        Active 'ppa_only' flag is usually associated with active
        'uploader_only' because there shouldn't be any sense of maintainership
        for packages uploaded to PPAs by someone else than the user
        themselves.
        """
        LPSPRC = LatestPersonSourcePackageReleaseCache
        clauses = []
        if uploader_only:
            clauses.append(LPSPRC.creator_id == self.id)
        if ppa_only:
            # Source maintainer is irrelevant for PPA uploads.
            pass
        elif uploader_only:
            lpspr = ClassAlias(LPSPRC, "lpspr")
            upload_distroseries_id = LPSPRC.upload_distroseries_id
            clauses.append(
                Not(
                    Exists(
                        Select(
                            1,
                            where=And(
                                lpspr.sourcepackagename_id
                                == LPSPRC.sourcepackagename_id,
                                lpspr.upload_archive_id
                                == LPSPRC.upload_archive_id,
                                lpspr.upload_distroseries_id
                                == upload_distroseries_id,
                                lpspr.archive_purpose != ArchivePurpose.PPA,
                                lpspr.maintainer_id == self.id,
                            ),
                            tables=lpspr,
                        )
                    )
                )
            )
        else:
            clauses.append(LPSPRC.maintainer_id == self.id)
        if ppa_only:
            clauses.append(LPSPRC.archive_purpose == ArchivePurpose.PPA)
        else:
            clauses.append(LPSPRC.archive_purpose != ArchivePurpose.PPA)
        return clauses

    def _hasReleasesQuery(self, uploader_only=False, ppa_only=False):
        """Are there sourcepackagereleases (SPRs) related to this person.
        See `_releasesQueryFilter` for details on the criteria used.
        """
        clauses = self._releasesQueryFilter(uploader_only, ppa_only)
        rs = (
            Store.of(self)
            .using(LatestPersonSourcePackageReleaseCache)
            .find(
                LatestPersonSourcePackageReleaseCache.publication_id, *clauses
            )
        )
        return not rs.is_empty()

    def _latestReleasesQuery(self, uploader_only=False, ppa_only=False):
        """Return the sourcepackagereleases records related to this person.
        See `_releasesQueryFilter` for details on the criteria used."""

        clauses = self._releasesQueryFilter(uploader_only, ppa_only)
        rs = (
            Store.of(self)
            .find(LatestPersonSourcePackageReleaseCache, *clauses)
            .order_by(Desc(LatestPersonSourcePackageReleaseCache.dateuploaded))
        )

        def load_related_objects(rows):
            if rows and rows[0].maintainer_id:
                list(
                    getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                        set(map(attrgetter("maintainer_id"), rows))
                    )
                )
            bulk.load_related(
                SourcePackageName, rows, ["sourcepackagename_id"]
            )
            bulk.load_related(
                SourcePackageRelease, rows, ["sourcepackagerelease_id"]
            )
            bulk.load_related(Archive, rows, ["upload_archive_id"])

        return DecoratedResultSet(rs, pre_iter_hook=load_related_objects)

    def hasSynchronisedPublishings(self):
        """See `IPerson`."""
        spph = ClassAlias(SourcePackagePublishingHistory, "spph")
        ancestor_spph = ClassAlias(
            SourcePackagePublishingHistory, "ancestor_spph"
        )
        tables = (
            SourcePackageRelease,
            Join(
                spph, spph.sourcepackagerelease_id == SourcePackageRelease.id
            ),
            Join(Archive, Archive.id == spph.archive_id),
            Join(ancestor_spph, ancestor_spph.id == spph.ancestor_id),
        )
        rs = (
            Store.of(self)
            .using(*tables)
            .find(
                spph.id,
                spph.creator_id == self.id,
                ancestor_spph.archive_id != spph.archive_id,
                Archive.purpose == ArchivePurpose.PRIMARY,
            )
        )
        return not rs.is_empty()

    def getLatestSynchronisedPublishings(self):
        """See `IPerson`."""
        spph = ClassAlias(SourcePackagePublishingHistory, "spph")
        ancestor_spph = ClassAlias(
            SourcePackagePublishingHistory, "ancestor_spph"
        )
        rs = (
            Store.of(self)
            .find(
                SourcePackagePublishingHistory,
                SourcePackagePublishingHistory.id.is_in(
                    Select(
                        spph.id,
                        tables=[
                            SourcePackageRelease,
                            Join(
                                spph,
                                spph.sourcepackagerelease_id
                                == SourcePackageRelease.id,
                            ),
                            Join(Archive, Archive.id == spph.archive_id),
                            Join(
                                ancestor_spph,
                                ancestor_spph.id == spph.ancestor_id,
                            ),
                        ],
                        where=And(
                            spph.creator_id == self.id,
                            ancestor_spph.archive_id != spph.archive_id,
                            Archive.purpose == ArchivePurpose.PRIMARY,
                        ),
                        order_by=[
                            spph.distroseries_id,
                            SourcePackageRelease.sourcepackagename_id,
                            Desc(spph.datecreated),
                            Desc(spph.id),
                        ],
                        distinct=(
                            spph.distroseries_id,
                            SourcePackageRelease.sourcepackagename_id,
                        ),
                    )
                ),
            )
            .order_by(
                Desc(SourcePackagePublishingHistory.datecreated),
                Desc(SourcePackagePublishingHistory.id),
            )
        )

        def load_related_objects(rows):
            bulk.load_related(
                SourcePackageRelease, rows, ["sourcepackagerelease_id"]
            )
            bulk.load_related(Archive, rows, ["archive_id"])

        return DecoratedResultSet(rs, pre_iter_hook=load_related_objects)

    def createRecipe(
        self,
        name,
        description,
        recipe_text,
        distroseries,
        registrant,
        daily_build_archive=None,
        build_daily=False,
    ):
        """See `IPerson`."""
        from lp.code.model.sourcepackagerecipe import SourcePackageRecipe

        recipe = SourcePackageRecipe.new(
            registrant,
            self,
            name,
            recipe_text,
            description,
            distroseries,
            daily_build_archive,
            build_daily,
        )
        Store.of(recipe).flush()
        return recipe

    def getRecipe(self, name):
        from lp.code.model.sourcepackagerecipe import SourcePackageRecipe

        return (
            Store.of(self)
            .find(
                SourcePackageRecipe,
                SourcePackageRecipe.owner == self,
                SourcePackageRecipe.name == name,
            )
            .one()
        )

    @cachedproperty
    def is_ubuntu_coc_signer(self):
        """See `IPerson`."""
        # Also assigned to by self._members.
        store = Store.of(self)
        query = And(
            SignedCodeOfConduct.owner_id == self.id,
            Person._is_ubuntu_coc_signer_condition(),
        )
        return not store.find(SignedCodeOfConduct, query).is_empty()

    @staticmethod
    def _is_ubuntu_coc_signer_condition():
        """Generate a Storm Expr for determining the coc signing status."""
        sigset = getUtility(ISignedCodeOfConductSet)
        lastdate = sigset.getLastAcceptedDate()
        return And(
            SignedCodeOfConduct.active == True,
            SignedCodeOfConduct.datecreated >= lastdate,
        )

    @property
    def activesignatures(self):
        """See `IPerson`."""
        sCoC_util = getUtility(ISignedCodeOfConductSet)
        return sCoC_util.searchByUser(self)

    @property
    def inactivesignatures(self):
        """See `IPerson`."""
        sCoC_util = getUtility(ISignedCodeOfConductSet)
        return sCoC_util.searchByUser(self, active=False)

    @cachedproperty
    def archive(self):
        """See `IPerson`."""
        return getUtility(IArchiveSet).getPPAOwnedByPerson(self)

    def getArchiveSubscriptions(self, requester):
        """See `IPerson`."""
        subscriptions = getUtility(IArchiveSubscriberSet).getBySubscriber(
            subscriber=self
        )
        return subscriptions

    def getArchiveSubscriptionURLs(self, requester):
        """See `IPerson`."""
        agent = getUtility(ILaunchpadCelebrities).software_center_agent
        # If the requester isn't asking about themselves, and they aren't the
        # software center agent, deny them
        if requester.id != agent.id:
            if self.id != requester.id:
                raise Unauthorized
        subscriptions = getUtility(
            IArchiveSubscriberSet
        ).getBySubscriberWithActiveToken(subscriber=self)
        return [
            token.archive_url
            for (subscription, token) in subscriptions
            if token is not None
        ]

    def getArchiveSubscriptionURL(self, requester, archive):
        """See `IPerson`."""
        agent = getUtility(ILaunchpadCelebrities).software_center_agent
        # If the requester isn't asking about themselves, and they aren't the
        # software center agent, deny them
        if requester.id != agent.id:
            if self.id != requester.id:
                raise Unauthorized(
                    "Only the context user can call this method"
                )
        # Verify if the user has a valid subscription on the given
        # archive and return None if it doesn't.
        subscription = getUtility(IArchiveSubscriberSet).getBySubscriber(
            subscriber=self, archive=archive
        )
        if subscription.is_empty():
            raise Unauthorized(
                "This person does not have a valid subscription for the "
                "target archive"
            )
        # Find the corresponding authorization token or create a new one.
        token = archive.getAuthToken(self)
        if token is None:
            token = archive.newAuthToken(self)
        return token.archive_url

    @property
    def ppas(self):
        """See `IPerson`."""
        return (
            IStore(Archive)
            .find(Archive, owner=self, purpose=ArchivePurpose.PPA)
            .order_by(Archive.name)
        )

    def getVisiblePPAs(self, user):
        """See `IPerson`."""

        # Avoid circular imports.
        from lp.soyuz.model.archive import get_enabled_archive_filter

        filter = get_enabled_archive_filter(
            user, purpose=ArchivePurpose.PPA, include_public=True
        )
        return (
            Store.of(self)
            .find(
                Archive,
                Archive.owner == self,
                Archive.status.is_in(
                    (ArchiveStatus.ACTIVE, ArchiveStatus.DELETING)
                ),
                filter,
            )
            .order_by(Archive.name)
        )

    def getPPAByName(self, distribution=None, name=None):
        """See `IPerson`."""
        assert name is not None
        if distribution is None:
            distribution = getUtility(ILaunchpadCelebrities).ubuntu
        ppa = getUtility(IArchiveSet).getPPAOwnedByPerson(
            self, distribution=distribution, name=name
        )
        if ppa is None:
            raise NoSuchPPA(name)
        return ppa

    def createPPA(
        self,
        distribution=None,
        name=None,
        displayname=None,
        description=None,
        private=False,
        suppress_subscription_notifications=False,
    ):
        """See `IPerson`."""
        if distribution is None:
            distribution = getUtility(ILaunchpadCelebrities).ubuntu
        if name is None:
            name = "ppa"
        errors = validate_ppa(self, distribution, name, private)
        if errors:
            raise PPACreationError(errors)
        return getUtility(IArchiveSet).new(
            owner=self,
            purpose=ArchivePurpose.PPA,
            distribution=distribution,
            name=name,
            displayname=displayname,
            description=description,
            private=private,
            suppress_subscription_notifications=(
                suppress_subscription_notifications
            ),
        )

    def isBugContributor(self, user=None):
        """See `IPerson`."""
        search_params = BugTaskSearchParams(user=user, assignee=self)
        return not self.searchTasks(search_params).is_empty()

    def isBugContributorInTarget(self, user=None, target=None):
        """See `IPerson`."""
        assert IBugTarget.providedBy(target) or IProjectGroup.providedBy(
            target
        ), ("%s isn't a valid bug target." % target)
        search_params = BugTaskSearchParams(user=user, assignee=self)
        return not target.searchTasks(search_params).is_empty()

    @property
    def structural_subscriptions(self):
        """See `IPerson`."""
        return (
            IStore(self)
            .find(
                StructuralSubscription,
                StructuralSubscription.subscriberID == self.id,
            )
            .order_by(Desc(StructuralSubscription.date_created))
        )

    def autoSubscribeToMailingList(self, mailinglist, requester=None):
        """See `IPerson`."""
        if mailinglist is None or not mailinglist.is_usable:
            return False

        if mailinglist.getSubscription(self):
            # We are already subscribed to the list.
            return False

        if self.preferredemail is None:
            return False

        if requester is None:
            # Assume the current user requested this action themselves.
            requester = self

        policy = self.mailing_list_auto_subscribe_policy

        if policy == MailingListAutoSubscribePolicy.ALWAYS:
            mailinglist.subscribe(self)
            return True
        elif (
            requester is self
            and policy == MailingListAutoSubscribePolicy.ON_REGISTRATION
        ):
            # Assume that we requested to be joined.
            mailinglist.subscribe(self)
            return True
        else:
            # We don't want to subscribe to the list.
            return False

    @property
    def recipes(self):
        """See `IHasRecipes`."""
        from lp.code.model.sourcepackagerecipe import SourcePackageRecipe

        recipes = Store.of(self).find(
            SourcePackageRecipe, SourcePackageRecipe.owner == self
        )
        hook = SourcePackageRecipe.preLoadDataForSourcePackageRecipes
        return DecoratedResultSet(recipes, pre_iter_hook=hook)

    def canAccess(self, obj, attribute):
        """See `IPerson.`"""
        return canAccess(obj, attribute)

    def canWrite(self, obj, attribute):
        """See `IPerson.`"""
        return canWrite(obj, attribute)

    def checkRename(self):
        """See `IPerson.`"""
        reasons = []
        atom = "person"
        has_ppa = (
            getUtility(IArchiveSet).getPPAOwnedByPerson(
                self,
                has_packages=True,
                statuses=[ArchiveStatus.ACTIVE, ArchiveStatus.DELETING],
            )
            is not None
        )
        if ITeam.providedBy(self):
            atom = "team"
        if has_ppa:
            reasons.append("an active PPA with packages published")
        if reasons:
            return _(
                "This %s has %s and may not be renamed."
                % (atom, " and ".join(reasons))
            )
        else:
            return None

    def canCreatePPA(self):
        """See `IPerson.`"""
        return self.membership_policy in EXCLUSIVE_TEAM_POLICY

    def checkAllowVisibility(self):
        role = IPersonRoles(self)
        if (
            role.in_commercial_admin
            or role.in_admin
            or self.hasCurrentCommercialSubscription()
        ):
            return True
        else:
            return False

    def security_field_changed(
        self, subject, change_description, recipient_emails=None
    ):
        """See `IPerson`."""
        tpl_substitutions = dict(
            field_changed=change_description,
        )
        template = get_email_template(
            "person-details-change.txt", app="registry"
        )
        body = template % tpl_substitutions
        from_addr = config.canonical.bounce_address
        if not recipient_emails:
            to_addrs = self.preferredemail.email
        else:
            to_addrs = recipient_emails
        simple_sendmail(from_addr, to_addrs, subject, body)

    def transitionVisibility(self, visibility, user):
        if self.visibility == visibility:
            return
        validate_person_visibility(self, "visibility", visibility)
        if not user.checkAllowVisibility():
            raise ImmutableVisibilityError()
        self.visibility = visibility
        self._ensurePolicies()

    def _ensurePolicies(self):
        # Ensure that private teams have an access policy grant enabling them
        # to see any private +junk branches.
        if self.visibility == PersonVisibility.PUBLIC or not self.is_team:
            return
        aps = getUtility(IAccessPolicySource)
        existing_policy = list(aps.findByTeam([self]))
        if existing_policy:
            return
        # Create the personal access policy.
        [policy] = getUtility(IAccessPolicySource).createForTeams([self])
        # Create the required access policy grant.
        grants = [(policy, self, self)]
        getUtility(IAccessPolicyGrantSource).grant(grants)


@implementer(IPersonSet)
class PersonSet:
    """The set of persons."""

    def __init__(self):
        self.title = "People registered with Launchpad"

    def isNameBlocklisted(self, name, user=None):
        """See `IPersonSet`."""
        if user is None:
            user_id = 0
        else:
            user_id = user.id
        cur = cursor()
        cur.execute(
            "SELECT is_blocklisted_name(%(name)s, %(user_id)s)",
            {"name": name, "user_id": user_id},
        )
        return bool(cur.fetchone()[0])

    def getTopContributors(self, limit=50):
        """See `IPersonSet`."""
        # The odd ordering here is to ensure we hit the PostgreSQL
        # indexes. It will not make any real difference outside of tests.
        top_people = shortlist(
            IStore(Person).find(
                Person,
                Person.id.is_in(
                    Select(
                        KarmaTotalCache.person_id,
                        order_by=(
                            Desc(KarmaTotalCache.karma_total),
                            Desc(KarmaTotalCache.person_id),
                        ),
                        limit=limit,
                    )
                ),
            )
        )
        return sorted(
            top_people,
            key=lambda obj: (obj.karma, obj.displayname, obj.id),
            reverse=True,
        )

    def getByOpenIDIdentifier(self, identifier):
        """See `IPersonSet`."""
        # We accept a full OpenID identifier URL from either the
        # Launchpad- or Ubuntu-branded OpenID services. But we only
        # store the unique suffix of the identifier, so we need to strip
        # the rest of the URL.
        # + is reserved, so is not allowed to be reencoded in transit, so
        # should never appear as its percent-encoded equivalent.
        identifier_suffix = None
        for root in CurrentOpenIDEndPoint.getAllRootURLs():
            base = "%s+id/" % root
            if identifier.startswith(base):
                identifier_suffix = identifier.replace(base, "", 1)
                break
        if identifier_suffix is None:
            return None

        try:
            account = getUtility(IAccountSet).getByOpenIDIdentifier(
                identifier_suffix
            )
        except LookupError:
            return None
        return IPerson(account)

    def getOrCreateByOpenIDIdentifier(
        self,
        openid_identifier,
        email_address,
        full_name,
        creation_rationale,
        comment,
        trust_email=True,
    ):
        """See `IPersonSet`."""
        # trust_email is an internal flag used by
        # getOrCreateSoftwareCenterCustomer. We don't want SCA to be
        # able to use the API to associate arbitrary OpenID identifiers
        # and email addresses when that could compromise existing
        # accounts.
        #
        # To that end, if trust_email is not set then the given
        # email address will only be used to create an account. It will
        # never be used to look up an account, nor will it be added to
        # an existing account. This causes two additional cases to be
        # rejected: unknown OpenID identifier but known email address,
        # and deactivated account.
        #
        # Exempting account creation and activation from this rule opens
        # us to a potential account fixation attack, but the risk is
        # minimal.

        assert email_address is not None and full_name is not None, (
            "Both email address and full name are required to create an "
            "account."
        )
        db_updated = False

        assert isinstance(openid_identifier, str)
        assert openid_identifier != "", "OpenID identifier must not be empty."

        # Load the EmailAddress, Account and OpenIdIdentifier records
        # from the primary (if they exist). We use the primary to avoid
        # possible replication lag issues but this might actually be
        # unnecessary.
        with PrimaryDatabasePolicy():
            identifier = (
                IStore(OpenIdIdentifier)
                .find(OpenIdIdentifier, identifier=openid_identifier)
                .one()
            )
            email = getUtility(IEmailAddressSet).getByEmail(email_address)

            if identifier is None:
                # We don't know about the OpenID identifier yet, so try
                # to match a person by email address, or as a last
                # resort create a new one.
                if email is None:
                    person_set = getUtility(IPersonSet)
                    person, email = person_set.createPersonAndEmail(
                        email_address,
                        creation_rationale,
                        comment=comment,
                        displayname=full_name,
                    )
                elif trust_email:
                    person = email.person
                else:
                    # The email address originated from a source that's
                    # not completely trustworth (eg. SCA), so we can't
                    # use it to link the OpenID identifier to an
                    # existing person.
                    raise EmailAddressAlreadyTaken()

                # It's possible that the email address is owned by a
                # team. Reject the login attempt, and wait for the user
                # to change their address.
                if person.is_team:
                    raise TeamEmailAddressError()

                # Some autocreated Persons won't have a corresponding
                # Account yet.
                if not person.account:
                    removeSecurityProxy(email.person).account = getUtility(
                        IAccountSet
                    ).new(
                        AccountCreationRationale.OWNER_CREATED_LAUNCHPAD,
                        full_name,
                    )

                # Create the identifier, and link it.
                identifier = OpenIdIdentifier()
                identifier.account = person.account
                identifier.identifier = openid_identifier
                IStore(OpenIdIdentifier).add(identifier)
                db_updated = True

            person = IPerson(identifier.account, None)
            assert person is not None, "Received a personless account."

            status = person.account.status
            if status == AccountStatus.ACTIVE:
                # Account is active, so nothing to do.
                pass
            elif status == AccountStatus.SUSPENDED:
                raise AccountSuspendedError(
                    "The account matching the identifier is suspended."
                )
            elif status == AccountStatus.DECEASED:
                raise AccountDeceasedError(
                    "The account matching the identifier belongs to a "
                    "deceased user."
                )
            elif not trust_email and status != AccountStatus.NOACCOUNT:
                # If the email address is not completely trustworthy
                # (ie. it comes from SCA) and the account has already
                # been used, then we don't want to proceed as we might
                # end up adding a malicious OpenID identifier to an
                # existing account.
                raise NameAlreadyTaken(
                    "The account matching the identifier is inactive."
                )
            elif person.account.status in [
                AccountStatus.DEACTIVATED,
                AccountStatus.NOACCOUNT,
                AccountStatus.PLACEHOLDER,
            ]:
                if person.account.status == AccountStatus.PLACEHOLDER:
                    # Placeholder accounts were never visible to anyone
                    # before, so make them appear fresh to the user.
                    removeSecurityProxy(person).display_name = full_name
                    removeSecurityProxy(person).datecreated = UTC_NOW
                removeSecurityProxy(person.account).reactivate(comment)
                if email is None:
                    email = getUtility(IEmailAddressSet).new(
                        email_address, person
                    )
                removeSecurityProxy(person).setPreferredEmail(email)
                db_updated = True
            else:
                raise AssertionError(
                    "Unhandled account status: %r" % person.account.status
                )

            return person, db_updated

    def getOrCreateSoftwareCenterCustomer(
        self, user, openid_identifier, email_address, display_name
    ):
        """See `IPersonSet`."""
        if user != getUtility(ILaunchpadCelebrities).software_center_agent:
            raise Unauthorized()
        person, _ = getUtility(IPersonSet).getOrCreateByOpenIDIdentifier(
            openid_identifier,
            email_address,
            display_name,
            PersonCreationRationale.SOFTWARE_CENTER_PURCHASE,
            "when purchasing an application via Software Center.",
            trust_email=False,
        )
        return person

    def getUsernameForSSO(self, user, openid_identifier):
        """See `IPersonSet`."""
        if user != getUtility(ILaunchpadCelebrities).ubuntu_sso:
            raise Unauthorized()
        try:
            account = getUtility(IAccountSet).getByOpenIDIdentifier(
                openid_identifier
            )
        except LookupError:
            return None
        return IPerson(account).name

    def setUsernameFromSSO(self, user, openid_identifier, name, dry_run=False):
        """See `IPersonSet`."""
        if user != getUtility(ILaunchpadCelebrities).ubuntu_sso:
            raise Unauthorized()
        self._validateName(name)
        try:
            account = getUtility(IAccountSet).getByOpenIDIdentifier(
                openid_identifier
            )
        except LookupError:
            if not dry_run:
                person = self.createPlaceholderPerson(openid_identifier, name)
        else:
            if account.status != AccountStatus.PLACEHOLDER:
                raise NotPlaceholderAccount(
                    "An account for that OpenID identifier already exists."
                )
            if not dry_run:
                account = removeSecurityProxy(account)
                person = IPerson(account)
                person.name = person.display_name = account.displayname = name

    def getSSHKeysForSSO(self, user, openid_identifier):
        """See `IPersonSet`"""
        if user != getUtility(ILaunchpadCelebrities).ubuntu_sso:
            raise Unauthorized()
        try:
            account = getUtility(IAccountSet).getByOpenIDIdentifier(
                openid_identifier
            )
        except LookupError:
            return None
        return [s.getFullKeyText() for s in IPerson(account).sshkeys]

    def addSSHKeyFromSSO(
        self, user, openid_identifier, key_text, dry_run=False
    ):
        """See `IPersonSet`"""
        if user != getUtility(ILaunchpadCelebrities).ubuntu_sso:
            raise Unauthorized()
        try:
            account = getUtility(IAccountSet).getByOpenIDIdentifier(
                openid_identifier
            )
        except LookupError:
            raise NoSuchAccount(
                "No account found for openid identifier '%s'"
                % openid_identifier
            )
        getUtility(ISSHKeySet).new(
            IPerson(account),
            key_text,
            send_notification=False,
            dry_run=dry_run,
        )

    def deleteSSHKeyFromSSO(
        self, user, openid_identifier, key_text, dry_run=False
    ):
        """See `IPersonSet`."""
        if user != getUtility(ILaunchpadCelebrities).ubuntu_sso:
            raise Unauthorized()
        try:
            account = getUtility(IAccountSet).getByOpenIDIdentifier(
                openid_identifier
            )
        except LookupError:
            raise NoSuchAccount(
                "No account found for openid identifier '%s'"
                % openid_identifier
            )
        keys = getUtility(ISSHKeySet).getByPersonAndKeyText(
            IPerson(account), key_text
        )
        if not dry_run:
            # ISSHKeySet does not restrict the same SSH key being added
            # multiple times, so make sure we delete them all:
            for key in keys:
                key.destroySelf(send_notification=False)

    def newTeam(
        self,
        teamowner,
        name,
        display_name,
        teamdescription=None,
        membership_policy=TeamMembershipPolicy.MODERATED,
        defaultmembershipperiod=None,
        defaultrenewalperiod=None,
        subscription_policy=None,
    ):
        """See `IPersonSet`."""
        assert teamowner
        if self.getByName(name, ignore_merged=False) is not None:
            raise NameAlreadyTaken("The name '%s' is already taken." % name)
        if subscription_policy is not None:
            # Support 1.0 API.
            membership_policy = subscription_policy
        team = Person(
            teamowner=teamowner,
            name=name,
            display_name=display_name,
            description=teamdescription,
            defaultmembershipperiod=defaultmembershipperiod,
            defaultrenewalperiod=defaultrenewalperiod,
            membership_policy=membership_policy,
        )
        store = IStore(Person)
        store.add(team)
        store.flush()
        notify(ObjectCreatedEvent(team))
        # Here we add the owner as a team admin manually because we know what
        # we're doing (so we don't need to do any sanity checks) and we don't
        # want any email notifications to be sent.
        TeamMembershipSet().new(
            teamowner, team, TeamMembershipStatus.ADMIN, teamowner
        )
        return team

    def createPersonAndEmail(
        self,
        email,
        rationale,
        comment=None,
        name=None,
        displayname=None,
        hide_email_addresses=False,
        registrant=None,
    ):
        """See `IPersonSet`."""

        # This check is also done in EmailAddressSet.new() and also
        # generate_nick(). We repeat it here as some call sites want
        # InvalidEmailAddress rather than NicknameGenerationError that
        # generate_nick() will raise.
        if not valid_email(email):
            raise InvalidEmailAddress(
                "%s is not a valid email address." % email
            )

        if name is None:
            name = generate_nick(email)

        if not displayname:
            displayname = name.capitalize()

        # Convert the PersonCreationRationale to an AccountCreationRationale
        account_rationale = getattr(AccountCreationRationale, rationale.name)

        account = getUtility(IAccountSet).new(account_rationale, displayname)

        person = self._newPerson(
            name,
            displayname,
            hide_email_addresses,
            rationale=rationale,
            comment=comment,
            registrant=registrant,
            account=account,
        )
        email = getUtility(IEmailAddressSet).new(email, person)

        return person, email

    def createPersonWithoutEmail(
        self, name, rationale, comment=None, displayname=None, registrant=None
    ):
        """Create and return a new Person without using an email address.

        See `IPersonSet`.
        """
        return self._newPerson(
            name,
            displayname,
            hide_email_addresses=True,
            rationale=rationale,
            comment=comment,
            registrant=registrant,
        )

    def createPlaceholderPerson(self, openid_identifier, name):
        """See `IPersonSet`."""
        account = getUtility(IAccountSet).new(
            AccountCreationRationale.USERNAME_PLACEHOLDER,
            name,
            openid_identifier=openid_identifier,
            status=AccountStatus.PLACEHOLDER,
        )
        return self._newPerson(
            name,
            name,
            True,
            rationale=PersonCreationRationale.USERNAME_PLACEHOLDER,
            comment="when setting a username in SSO",
            account=account,
        )

    def _validateName(self, name):
        if not valid_name(name):
            raise InvalidName("%s is not a valid name for a person." % name)
        else:
            # The name should be okay, move on...
            pass
        if self.getByName(name, ignore_merged=False) is not None:
            raise NameAlreadyTaken("The name '%s' is already taken." % name)

    def _newPerson(
        self,
        name,
        displayname,
        hide_email_addresses,
        rationale,
        comment=None,
        registrant=None,
        account=None,
    ):
        """Create and return a new Person with the given attributes."""
        self._validateName(name)

        if not displayname:
            displayname = name.capitalize()

        person = Person(
            name=name,
            display_name=displayname,
            account=account,
            creation_rationale=rationale,
            creation_comment=comment,
            hide_email_addresses=hide_email_addresses,
            registrant=registrant,
        )
        store = IStore(Person)
        store.add(person)
        store.flush()

        notify(ObjectCreatedEvent(person))

        return person

    def ensurePerson(
        self,
        email,
        displayname,
        rationale,
        comment=None,
        registrant=None,
        name=None,
    ):
        """See `IPersonSet`."""
        person = getUtility(IPersonSet).getByEmail(email, filter_status=False)
        if person is None:
            person, email_address = self.createPersonAndEmail(
                email,
                rationale,
                name=name,
                comment=comment,
                displayname=displayname,
                registrant=registrant,
                hide_email_addresses=True,
            )
        return person

    def getByName(self, name, ignore_merged=True):
        """See `IPersonSet`."""
        clauses = [Person.name == name]
        if ignore_merged:
            clauses.append(Is(Person.merged_id, None))
        return IStore(Person).find(Person, *clauses).one()

    def getByAccount(self, account):
        """See `IPersonSet`."""
        return IStore(Person).find(Person, account_id=account.id).one()

    def updateStatistics(self):
        """See `IPersonSet`."""
        stats = getUtility(ILaunchpadStatisticSet)
        people_count = (
            IStore(Person)
            .find(
                Person,
                Is(Person.teamowner_id, None),
                Is(Person.merged_id, None),
            )
            .count()
        )
        stats.update("people_count", people_count)
        transaction.commit()
        teams_count = (
            IStore(Person)
            .find(
                Person,
                IsNot(Person.teamowner_id, None),
                Is(Person.merged_id, None),
            )
            .count()
        )
        stats.update("teams_count", teams_count)
        transaction.commit()

    def peopleCount(self):
        """See `IPersonSet`."""
        return getUtility(ILaunchpadStatisticSet).value("people_count")

    def teamsCount(self):
        """See `IPersonSet`."""
        return getUtility(ILaunchpadStatisticSet).value("teams_count")

    def _teamEmailQuery(self, text):
        """Product the query for team email addresses."""
        team_email_query = And(
            get_person_visibility_terms(getUtility(ILaunchBag).user),
            Person.teamowner != None,
            Person.merged == None,
            EmailAddress.person == Person.id,
            EmailAddress.email.lower().startswith(text),
        )
        return team_email_query

    def _teamNameQuery(self, text):
        """Produce the query for team names."""
        team_name_query = And(
            get_person_visibility_terms(getUtility(ILaunchBag).user),
            Person.teamowner != None,
            Person.merged == None,
            fti_search(Person, text),
        )
        return team_name_query

    def find(self, text=""):
        """See `IPersonSet`."""
        if not text:
            # Return an empty result set.
            return EmptyResultSet()

        orderBy = Person._sortingColumnsForSetOperations
        lower_case_text = text.lower()
        # Teams may not have email addresses, so we need to either use a LEFT
        # OUTER JOIN or do a UNION between four queries. Using a UNION makes
        # it a lot faster than with a LEFT OUTER JOIN.
        person_email_query = And(
            Person.teamowner == None,
            Person.merged == None,
            EmailAddress.person == Person.id,
            Person.account == Account.id,
            Not(Account.status.is_in(INACTIVE_ACCOUNT_STATUSES)),
            EmailAddress.email.lower().startswith(lower_case_text),
        )

        store = IStore(Person)

        # The call to order_by() is necessary to avoid having the default
        # ordering applied.  Since no value is passed the effect is to remove
        # the generation of an 'ORDER BY' clause on the intermediate results.
        # Otherwise the default ordering is taken from the ordering
        # declaration on the class.  The final result set will have the
        # appropriate ordering set.
        results = store.find(Person, person_email_query).order_by()

        person_name_query = And(
            Person.teamowner == None,
            Person.merged == None,
            Person.account == Account.id,
            Not(Account.status.is_in(INACTIVE_ACCOUNT_STATUSES)),
            fti_search(Person, text),
        )

        results = results.union(
            store.find(Person, person_name_query)
        ).order_by()
        team_email_query = self._teamEmailQuery(lower_case_text)
        results = results.union(
            store.find(Person, team_email_query)
        ).order_by()
        team_name_query = self._teamNameQuery(text)
        results = results.union(store.find(Person, team_name_query)).order_by()

        return results.order_by(orderBy)

    def findPerson(
        self,
        text="",
        exclude_inactive_accounts=True,
        must_have_email=False,
        created_after=None,
        created_before=None,
    ):
        """See `IPersonSet`."""
        orderBy = Person._sortingColumnsForSetOperations
        store = IStore(Person)
        base_query = And(Person.teamowner == None, Person.merged == None)

        clause_tables = []

        if exclude_inactive_accounts:
            clause_tables.append("Account")
            base_query = And(
                base_query,
                Person.account == Account.id,
                Not(Account.status.is_in(INACTIVE_ACCOUNT_STATUSES)),
            )
        email_clause_tables = clause_tables + ["EmailAddress"]
        if must_have_email:
            clause_tables = email_clause_tables
            base_query = And(base_query, EmailAddress.person == Person.id)
        if created_after is not None:
            base_query = And(base_query, Person.datecreated > created_after)
        if created_before is not None:
            base_query = And(base_query, Person.datecreated < created_before)

        # Short circuit for returning all users in order
        if not text:
            results = store.find(Person, base_query)
            return results.order_by(Person.sortingColumns)

        # We use a UNION here because this makes things *a lot* faster
        # than if we did a single SELECT with the two following clauses
        # ORed.
        email_query = And(
            base_query,
            EmailAddress.person == Person.id,
            EmailAddress.email.lower().startswith(text.lower()),
        )

        name_query = And(base_query, fti_search(Person, text))
        email_results = store.find(Person, email_query).order_by()
        name_results = store.find(Person, name_query).order_by()
        combined_results = email_results.union(name_results)
        return combined_results.order_by(orderBy)

    def findTeam(self, text="", preload_for_api=False):
        """See `IPersonSet`."""
        orderBy = Person._sortingColumnsForSetOperations
        # Teams may not have email addresses, so we need to either use a LEFT
        # OUTER JOIN or do a UNION between two queries. Using a UNION makes
        # it a lot faster than with a LEFT OUTER JOIN.
        email_query = self._teamEmailQuery(text.lower())
        store = IStore(Person)
        email_results = store.find(Person, email_query).order_by()
        name_query = self._teamNameQuery(text)
        name_results = store.find(Person, name_query).order_by()
        result = email_results.union(name_results).order_by(orderBy)
        if preload_for_api:

            def preload(people):
                list(
                    self.getPrecachedPersonsFromIDs(
                        [person.id for person in people], need_api=True
                    )
                )

            result = DecoratedResultSet(result, pre_iter_hook=preload)
        return result

    def get(self, personid):
        """See `IPersonSet`."""
        return IStore(Person).get(Person, personid)

    def getByEmail(self, email, filter_status=True):
        """See `IPersonSet`."""
        address = self.getByEmails([email], filter_status=filter_status).one()
        if address:
            return address[1]

    def getByEmails(self, emails, include_hidden=True, filter_status=True):
        """See `IPersonSet`."""
        if not emails:
            return EmptyResultSet()
        addresses = [
            six.ensure_text(address).lower().strip() for address in emails
        ]
        hidden_query = True
        filter_query = True
        if not include_hidden:
            hidden_query = Person.hide_email_addresses == False
        if filter_status:
            filter_query = EmailAddress.status.is_in(VALID_EMAIL_STATUSES)
        return (
            IStore(Person)
            .using(
                Person, Join(EmailAddress, EmailAddress.person_id == Person.id)
            )
            .find(
                (EmailAddress, Person),
                EmailAddress.email.lower().is_in(addresses),
                filter_query,
                hidden_query,
            )
        )

    def mergeAsync(
        self, from_person, to_person, requester, reviewer=None, delete=False
    ):
        """See `IPersonSet`."""
        return getUtility(IPersonMergeJobSource).create(
            from_person=from_person,
            to_person=to_person,
            requester=requester,
            reviewer=reviewer,
            delete=delete,
        )

    def getValidPersons(self, persons):
        """See `IPersonSet.`"""
        person_ids = [person.id for person in persons]
        if len(person_ids) == 0:
            return []

        # This has the side effect of sucking in the ValidPersonCache
        # items into the cache, allowing Person.is_valid_person calls to
        # not hit the DB.
        valid_person_ids = {
            person_id.id
            for person_id in IStore(ValidPersonCache).find(
                ValidPersonCache, ValidPersonCache.id.is_in(person_ids)
            )
        }
        return [person for person in persons if person.id in valid_person_ids]

    def getPeopleWithBranches(self, product=None):
        """See `IPersonSet`."""
        # Circular import.
        from lp.code.model.branch import Branch

        branch_params = {}
        if product is not None:
            branch_params["where"] = Branch.product == product
        return IStore(Person).find(
            Person, Person.id.is_in(Select(Branch.owner_id, **branch_params))
        )

    def updatePersonalStandings(self):
        """See `IPersonSet`."""
        cur = cursor()
        cur.execute(
            """
        UPDATE Person
        SET personal_standing = %s
        WHERE personal_standing = %s
        AND id IN (
            SELECT posted_by
            FROM MessageApproval
            WHERE status = %s
            GROUP BY posted_by
            HAVING COUNT(DISTINCT mailing_list) >= %s
            )
        """
            % sqlvalues(
                PersonalStanding.GOOD,
                PersonalStanding.UNKNOWN,
                PostedMessageStatus.APPROVED,
                config.standingupdater.approvals_needed,
            )
        )

    def cacheBrandingForPeople(self, people):
        """See `IPersonSet`."""
        aliases = []
        aliases.extend(
            person.icon_id for person in people if person.icon_id is not None
        )
        aliases.extend(
            person.logo_id for person in people if person.logo_id is not None
        )
        aliases.extend(
            person.mugshot_id
            for person in people
            if person.mugshot_id is not None
        )
        if not aliases:
            return
        # Listify, since this is a pure cache.
        list(
            IStore(LibraryFileAlias).find(
                (LibraryFileAlias, LibraryFileContent),
                LibraryFileAlias.id.is_in(aliases),
                LibraryFileAlias.content == LibraryFileContent.id,
            )
        )

    def getPrecachedPersonsFromIDs(
        self,
        person_ids,
        need_api=False,
        need_karma=False,
        need_ubuntu_coc=False,
        need_teamowner=False,
        need_location=False,
        need_archive=False,
        need_preferred_email=False,
        need_validity=False,
        need_icon=False,
    ):
        """See `IPersonSet`."""
        person_ids = set(person_ids)
        person_ids.discard(None)
        if not person_ids:
            return EmptyResultSet()
        origin = [Person]
        conditions = [Person.id.is_in(person_ids)]
        return self._getPrecachedPersons(
            origin,
            conditions,
            need_api=need_api,
            need_karma=need_karma,
            need_ubuntu_coc=need_ubuntu_coc,
            need_teamowner=need_teamowner,
            need_location=need_location,
            need_archive=need_archive,
            need_preferred_email=need_preferred_email,
            need_validity=need_validity,
            need_icon=need_icon,
        )

    def _getPrecachedPersons(
        self,
        origin,
        conditions,
        store=None,
        need_api=False,
        need_karma=False,
        need_ubuntu_coc=False,
        need_teamowner=False,
        need_location=False,
        need_archive=False,
        need_preferred_email=False,
        need_validity=False,
        need_icon=False,
    ):
        """Lookup all members of the team with optional precaching.

        :param store: Provide ability to specify the store.
        :param origin: List of storm tables and joins. The Person table is
            required.
        :param conditions: Storm conditions for tables in origin.
        :param need_karma: The karma attribute will be cached.
        :param need_ubuntu_coc: The is_ubuntu_coc_signer attribute will be
            cached.
        :param need_location: The location attribute will be cached.
        :param need_archive: The archive attribute will be cached.
        :param need_preferred_email: The preferred email attribute will be
            cached.
        :param need_validity: The is_valid attribute will be cached.
        :param need_icon: Cache the persons' icons so that their URLs can
            be generated without further reference to the database.
        """
        if store is None:
            store = IStore(Person)
        # The conditions we've been given are almost certainly more
        # selective that anything PostgreSQL might guess at.  Use a CTE to
        # ensure that it looks at them first.
        store = store.with_(
            WithMaterialized(
                "RelevantPerson",
                store,
                Select(Person.id, where=conditions, tables=origin),
            )
        )
        relevant_person = Table("RelevantPerson")
        origin = [
            Person,
            Join(relevant_person, Column("id", relevant_person) == Person.id),
        ]
        columns = [Person]
        decorators = []
        if need_karma or need_api:
            # New people have no karmatotalcache rows.
            origin.append(
                LeftJoin(KarmaTotalCache, KarmaTotalCache.person == Person.id)
            )
            columns.append(KarmaTotalCache)
        if need_ubuntu_coc or need_api:
            columns.append(
                Alias(
                    Exists(
                        Select(
                            SignedCodeOfConduct,
                            tables=[SignedCodeOfConduct],
                            where=And(
                                Person._is_ubuntu_coc_signer_condition(),
                                SignedCodeOfConduct.owner_id == Person.id,
                            ),
                        )
                    ),
                    name="is_ubuntu_coc_signer",
                )
            )
        if need_location or need_api:
            # New people have no location rows
            origin.append(
                LeftJoin(PersonLocation, PersonLocation.person == Person.id)
            )
            columns.append(PersonLocation)
        if need_archive or need_api:
            # Not everyone has PPAs.
            # It would be nice to cleanly expose the soyuz rules for this to
            # avoid duplicating the relationships.
            archive_conditions = Or(
                Archive.id == None,
                And(
                    Archive.owner == Person.id,
                    Archive.id
                    == Select(
                        tables=Archive,
                        columns=Min(Archive.id),
                        where=And(
                            Archive.purpose == ArchivePurpose.PPA,
                            Archive.owner == Person.id,
                        ),
                    ),
                ),
            )
            origin.append(LeftJoin(Archive, archive_conditions))
            columns.append(Archive)

        # Checking validity requires having a preferred email.
        if not need_api and need_preferred_email and not need_validity:
            # Teams don't have email, so a left join
            origin.append(
                LeftJoin(
                    EmailAddress,
                    And(
                        EmailAddress.person == Person.id,
                        EmailAddress.status == EmailAddressStatus.PREFERRED,
                    ),
                )
            )
            columns.append(EmailAddress)
        if need_validity or need_api:
            valid_stuff = Person._validity_queries()
            origin.extend(valid_stuff["joins"])
            columns.extend(valid_stuff["tables"])
            decorators.extend(valid_stuff["decorators"])
        if need_icon:
            IconAlias = ClassAlias(LibraryFileAlias, "IconAlias")
            origin.append(LeftJoin(IconAlias, Person.icon == IconAlias.id))
            columns.append(IconAlias)
        if need_api:
            LogoAlias = ClassAlias(LibraryFileAlias, "LogoAlias")
            MugshotAlias = ClassAlias(LibraryFileAlias, "MugshotAlias")
            origin.extend(
                [
                    LeftJoin(LogoAlias, Person.logo == LogoAlias.id),
                    LeftJoin(MugshotAlias, Person.mugshot == MugshotAlias.id),
                ]
            )
            columns.extend([LogoAlias, MugshotAlias])
        if len(columns) == 1:
            column = columns[0]
            # Return a simple ResultSet
            return store.using(*origin).find(column)
        # Adapt the result into a cached Person.
        columns = tuple(columns)
        raw_result = store.using(*origin).find(columns)
        if need_api:
            # Collections exported on the API need to be sorted in order to
            # provide stable batching.  In some ways Person.name might make
            # more sense, but we use the same ordering as in web UI views to
            # minimise confusion.
            raw_result = raw_result.order_by(Person.sortingColumns)

        def preload_for_people(rows):
            if need_teamowner or need_api:
                bulk.load(Person, [row[0].teamowner_id for row in rows])

        def prepopulate_person(row):
            result = row[0]
            cache = get_property_cache(result)
            index = 1
            # -- karma caching
            if need_karma or need_api:
                karma = row[index]
                index += 1
                if karma is None:
                    karma_total = 0
                else:
                    karma_total = karma.karma_total
                cache.karma = karma_total
            # -- ubuntu code of conduct signer status caching.
            if need_ubuntu_coc or need_api:
                signed = row[index]
                index += 1
                cache.is_ubuntu_coc_signer = signed
            # -- location caching
            if need_location or need_api:
                location = row[index]
                index += 1
                cache.location = location
            # -- archive caching
            if need_archive or need_api:
                archive = row[index]
                index += 1
                cache.archive = archive
            # -- preferred email caching
            if not need_api and need_preferred_email and not need_validity:
                email = row[index]
                index += 1
                cache.preferredemail = email
            for decorator in decorators:
                column = row[index]
                index += 1
                decorator(result, column)
            return result

        return DecoratedResultSet(
            raw_result,
            pre_iter_hook=preload_for_people,
            result_decorator=prepopulate_person,
        )

    def getUserData(self, email):
        """See `IPersonSet`."""
        email_results = self.getByEmails(
            [email], include_hidden=True, filter_status=False
        )

        # We should only have one result
        if email_results.count() > 1:
            raise ValueError(f"Multiple records for {email}")

        # If we don't have any results at all, we have no data!
        if email_results.is_empty():
            return {"status": "no data held"}

        account = email_results.one()[1]
        return_data = {}
        return_data["person"] = canonical_url(account)
        # Get the data behind the overview screen
        overview = self.getUserOverview(account)
        return_data.update(overview)

        # bzr branches
        branches_url = self._checkForBranchData(account)
        if branches_url:
            return_data["branches"] = branches_url
        # Git repositories
        git_url = self._checkForGitRepositoryData(account)
        if git_url:
            return_data["git-repositories"] = git_url
        # bugs
        bug_url = self._checkForBugs(account)
        if bug_url:
            return_data["bugs"] = bug_url
        # blueprints
        blueprints_url = self._checkForBlueprints(account)
        if blueprints_url:
            return_data["blueprints"] = blueprints_url
        # translations
        translations_url = self._checkForTranslations(account)
        if translations_url:
            return_data["translations"] = translations_url
        # questions
        questions_url = self._checkForAnswers(account)
        if questions_url:
            return_data["answers"] = questions_url
        # ssh keys
        if not account.sshkeys.is_empty():
            return_data["sshkeys"] = canonical_url(
                account, view_name="+sshkeys"
            )
        gpg_keys_urls = self._checkForGPGKeys(account)
        if gpg_keys_urls:
            return_data["openpgp-keys"] = gpg_keys_urls
        # This is only an 'account' in terms of the end user view,
        # it does not refer to an `IAccount`.
        if len(return_data.keys()) > 1:
            return_data["status"] = "account with data"
        else:
            return_data["status"] = "account only; no other data"
        return return_data

    def _checkForBranchData(self, account):
        """Check if bzr branches exist for a given person."""
        branches = account.getBranches()
        if branches.is_empty():
            return None
        search_url = canonical_url(
            account, rootsite="code", view_name="+branches"
        )
        req = PreparedRequest()
        req.prepare_url(
            search_url,
            {
                "field.category": "OWNED",
                "field.category-empty-marker": "1",
                "field.lifecycle": "ALL",
                "field.lifecycle-empty-marker": "1",
                "field.sort_by": "most recently changed first",
                "field.sort_by-empty-marker": "1",
            },
        )
        return req.url

    def _checkForGitRepositoryData(self, account):
        """Check if git repositories exist for a given person."""
        repositories = IGitCollection(account).visibleByUser(account)
        if repositories.is_empty():
            return None
        return canonical_url(account, rootsite="code", view_name="+git")

    def _checkForBugs(self, account):
        """Check if related bug data exists for a given person."""
        params = BugTaskSearchParams(user=account)

        # Build related searches, based on BugTaskSearchListingView
        subscriber_params = copy.copy(params)
        subscriber_params.subscriber = account
        assignee_params = copy.copy(params)
        assignee_params.assignee = account
        owner_params = copy.copy(params)
        owner_params.owner = account
        owner_params.bug_reporter = account
        commenter_params = copy.copy(params)
        commenter_params.bug_commenter = account

        results = account.searchTasks(
            assignee_params, subscriber_params, owner_params, commenter_params
        )
        if results.is_empty():
            return None
        req = PreparedRequest()
        bugs_url = canonical_url(account, rootsite="bugs")
        query_arguments = {
            "field.searchtext": "",
            "orderby": "-importance",
            "field.omit_dupes.used": "",
        }
        status_list = [status.token for status in BugTaskStatusSearch]
        query_arguments["field.status:list"] = status_list
        req.prepare_url(bugs_url, query_arguments)
        return req.url

    def _checkForBlueprints(self, account):
        """Check if related blueprints exist for a given person"""
        specifications = account.specifications(account)
        if specifications.is_empty():
            return None
        return canonical_url(account, rootsite="blueprints")

    def _checkForTranslations(self, account):
        """Check for translation activity for a given user"""
        translation_person = ITranslationsPerson(account)
        # Has performed translations
        if translation_person.hasTranslated():
            return canonical_url(
                account, rootsite="translations", view_name="+activity"
            )
        return None

    def _checkForAnswers(self, account):
        """Check if related questions and answers exist for a given person."""
        question_person = IQuestionsPerson(account)
        questions = question_person.searchQuestions(
            status=QuestionStatus.items
        )
        if questions.is_empty():
            return None
        req = PreparedRequest()
        answers_url = canonical_url(
            account, rootsite="answers", view_name="+questions"
        )
        query_arguments = {
            "field.search_text": "",
            "field.sort": "RELEVANCY",
            "field.sort-empty-marker": "1",
            "field.actions.search": "Search",
            "field.status-empty-marker": "1",
            "field.status": [x.token for x in QuestionStatus],
        }
        req.prepare_url(answers_url, query_arguments)
        return req.url

    def _checkForGPGKeys(self, account):
        """Check if we have GPG keys for the given Person."""
        # We return the keyserver url, not an LP url
        urls = []
        keyserver_url = "https://keyserver.ubuntu.com/pks/lookup"
        keys = account.gpg_keys + account.inactive_gpg_keys
        for key in keys:
            req = PreparedRequest()
            query_arguments = {
                "fingerprint": "on",
                "op": "index",
                "search": f"0x{key.fingerprint}",
            }
            req.prepare_url(keyserver_url, query_arguments)
            urls.append(req.url)
        return urls

    def getUserOverview(self, person):
        """See `IPersonSet`."""
        overview = {}
        maintained_packages = person.hasMaintainedPackages()
        uploaded_packages = person.hasUploadedButNotMaintainedPackages()
        ppa_packages = person.hasUploadedPPAPackages()
        synchronised_packages = person.hasSynchronisedPublishings()

        # Related packages is a general overview, if we have any of them
        # the related packages exist
        if any(
            [
                maintained_packages,
                uploaded_packages,
                ppa_packages,
                synchronised_packages,
            ]
        ):
            overview["related-packages"] = canonical_url(
                person, view_name="+related-packages"
            )

        if maintained_packages:
            overview["maintained-packages"] = canonical_url(
                person, view_name="+maintained-packages"
            )
        if uploaded_packages:
            overview["uploaded-packages"] = canonical_url(
                person, view_name="+uploaded-packages"
            )
        if ppa_packages:
            overview["ppa-packages"] = canonical_url(
                person, view_name="+ppa-packages"
            )
        if synchronised_packages:
            overview["synchronised-packages"] = canonical_url(
                person, view_name="+synchronised-packages"
            )

        related_projects = not person.getAffiliatedPillars(person).is_empty()
        if related_projects:
            overview["related-projects"] = canonical_url(
                person, view_name="+related-projects"
            )
        owned_teams = not person.getOwnedTeams(person).is_empty()
        if owned_teams:
            overview["owned-teams"] = canonical_url(
                person, view_name="+owned-teams"
            )
        return overview


# Provide a storm alias from Person to Owner. This is useful in queries on
# objects that have more than just an owner associated with them.
Owner = ClassAlias(Person, "Owner")


class PersonLanguage(StormBase):
    __storm_table__ = "PersonLanguage"
    id = Int(primary=True)
    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")

    language_id = Int(name="language", allow_none=False)
    language = Reference(language_id, "Language.id")

    def __init__(self, person, language):
        self.person = person
        self.language = language

    def delete(self):
        Store.of(self).remove(self)


@implementer(ISSHKey)
class SSHKey(StormBase):
    __storm_table__ = "SSHKey"
    __storm_order__ = ["person", "keytype", "keytext"]

    id = Int(primary=True)
    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")
    keytype = DBEnum(name="keytype", allow_none=False, enum=SSHKeyType)
    keytext = Unicode(name="keytext", allow_none=False)
    comment = Unicode(name="comment", allow_none=False)

    def __init__(self, person, keytype, keytext, comment):
        super().__init__()
        self.person = person
        self.keytype = keytype
        self.keytext = keytext
        self.comment = comment

    def destroySelf(self, send_notification=True):
        if send_notification:
            # For security reasons we want to notify the preferred email
            # address that this sshkey has been removed.
            if self.comment:
                change_description = (
                    "The SSH key %s was removed from your account."
                ) % (self.comment)
            else:
                change_description = (
                    "An SSH key of type '%s' was removed from your "
                    "account." % self.keytype.title
                )
            self.person.security_field_changed(
                "SSH key removed from your Launchpad account.",
                change_description,
            )
        IStore(self).remove(self)

    def getFullKeyText(self):
        try:
            key_blob = base64.b64decode(self.keytext.encode("UTF-8"))
            ssh_keytype = getNS(key_blob)[0].decode("ascii")
        except Exception:
            # We didn't always validate keys, so there might be some that
            # can't be loaded this way.
            if self.keytype == SSHKeyType.RSA:
                ssh_keytype = "ssh-rsa"
            elif self.keytype == SSHKeyType.DSA:
                ssh_keytype = "ssh-dss"
            else:
                # There's no single ssh_keytype for ECDSA keys (it depends
                # on the curve), and we've always validated these so this
                # shouldn't happen.
                raise ValueError(
                    "SSH key of type %s has invalid data '%s'"
                    % (self.keytype, self.keytext)
                )
        key_text = "%s %s" % (ssh_keytype, self.keytext)
        if self.comment:
            key_text = "%s %s" % (key_text, self.comment)
        return key_text


@implementer(ISSHKeySet)
class SSHKeySet:
    def new(
        self,
        person,
        sshkey,
        check_key=True,
        send_notification=True,
        dry_run=False,
    ):
        kind, keytype, keytext, comment = self._extract_ssh_key_components(
            sshkey
        )

        if check_key:
            try:
                key = Key.fromString(sshkey.encode("UTF-8"))
            except Exception as e:
                raise SSHKeyAdditionError(key=sshkey, exception=e)
            keydatatype = six.ensure_str(key.sshType())
            if kind != keydatatype:
                raise SSHKeyAdditionError(type_mismatch=(kind, keydatatype))

        if send_notification:
            if comment:
                change_description = (
                    "The SSH key '%s' has been added to your account."
                ) % comment
            else:
                change_description = (
                    "An SSH key of type '%s' has been added "
                    "to your account." % keytype.title
                )
            person.security_field_changed(
                "New SSH key added to your account.", change_description
            )

        if not dry_run:
            ssh_key = SSHKey(
                person=person,
                keytype=keytype,
                keytext=keytext,
                comment=comment,
            )
            IStore(ssh_key).flush()
            return ssh_key

    def getByID(self, id, default=None):
        return IStore(SSHKey).get(SSHKey, int(id))

    def getByPeople(self, people):
        """See `ISSHKeySet`"""
        return IStore(SSHKey).find(
            SSHKey, SSHKey.person_id.is_in(person.id for person in people)
        )

    def getByPersonAndKeyText(self, person, sshkey):
        _, keytype, keytext, comment = self._extract_ssh_key_components(sshkey)
        return IStore(SSHKey).find(
            SSHKey,
            person=person,
            keytype=keytype,
            keytext=keytext,
            comment=comment,
        )

    def _extract_ssh_key_components(self, sshkey):
        try:
            ssh_key_components = sshkey.split(" ", 2)
        except AttributeError:
            raise SSHKeyAdditionError(key=sshkey)

        if len(ssh_key_components) < 2:
            raise SSHKeyAdditionError(key=sshkey)
        elif len(ssh_key_components) == 2:
            kind, keytext = ssh_key_components
            comment = ""
        else:
            kind, keytext, comment = ssh_key_components

        keytype = SSH_TEXT_TO_KEY_TYPE.get(kind)
        if keytype is None:
            raise SSHKeyAdditionError(kind=kind)
        return kind, keytype, keytext, comment


@implementer(IWikiName)
class WikiName(StormBase, HasOwnerMixin):
    __storm_table__ = "WikiName"

    id = Int(primary=True)
    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")
    wiki = Unicode(name="wiki", allow_none=False)
    wikiname = Unicode(name="wikiname", allow_none=False)

    def __init__(self, person, wiki, wikiname):
        super().__init__()
        self.person = person
        self.wiki = wiki
        self.wikiname = wikiname

    @property
    def url(self):
        return self.wiki + self.wikiname

    def destroySelf(self):
        IStore(self).remove(self)


@implementer(IWikiNameSet)
class WikiNameSet:
    def get(self, id):
        """See `IWikiNameSet`."""
        return IStore(WikiName).get(WikiName, int(id))

    def new(self, person, wiki, wikiname):
        """See `IWikiNameSet`."""
        wiki_name = WikiName(person=person, wiki=wiki, wikiname=wikiname)
        IStore(wiki_name).flush()
        return wiki_name


@implementer(ISocialAccount)
class SocialAccount(StormBase, HasOwnerMixin):
    __storm_table__ = "SocialAccount"

    id = Int(primary=True)
    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")
    platform = DBEnum(
        name="platform",
        allow_none=False,
        enum=SocialPlatformType,
    )
    identity = JSON(
        name="identity",
        allow_none=False,
        validator=validate_social_account_identity,
    )

    def __init__(self, person, platform, identity):
        super().__init__()
        self.person = person
        self.platform = platform
        self.identity = identity

    def getSocialPlatform(self):
        return SOCIAL_PLATFORM_TYPES_MAP.get(self.platform)

    def destroySelf(self):
        IStore(self).remove(self)


@implementer(ISocialAccountSet)
class SocialAccountSet:
    def get(self, id):
        """See `ISocialAccountSet`."""
        return IStore(SocialAccount).get(SocialAccount, int(id))

    def getByPerson(self, person):
        """See `ISocialAccountSet`."""
        return (
            IStore(SocialAccount)
            .find(SocialAccount, person=person)
            .group_by(SocialAccount.platform)
        )

    def getByPersonAndSocialPlatform(self, person, social_platform):
        """See `ISocialAccountSet`."""
        return IStore(SocialAccount).find(
            SocialAccount, person=person, platform=social_platform
        )

    def new(self, person, platform, identity):
        """See `ISocialAccountSet`."""
        social_account = SocialAccount(
            person=person, platform=platform, identity=identity
        )
        IStore(social_account).flush()
        return social_account


@implementer(IJabberID)
class JabberID(StormBase, HasOwnerMixin):
    __storm_table__ = "JabberID"
    __storm_order__ = ["jabberid"]

    id = Int(primary=True)
    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")
    jabberid = Unicode(name="jabberid", allow_none=False)

    def __init__(self, person, jabberid):
        super().__init__()
        self.person = person
        self.jabberid = jabberid

    def destroySelf(self):
        IStore(self).remove(self)


@implementer(IJabberIDSet)
class JabberIDSet:
    def new(self, person, jabberid):
        """See `IJabberIDSet`"""
        return JabberID(person=person, jabberid=jabberid)

    def getByJabberID(self, jabberid):
        """See `IJabberIDSet`"""
        return IStore(JabberID).find(JabberID, jabberid=jabberid).one()

    def getByPerson(self, person):
        """See `IJabberIDSet`"""
        return IStore(JabberID).find(JabberID, person=person)


@implementer(IIrcID)
class IrcID(StormBase, HasOwnerMixin):
    """See `IIrcID`"""

    __storm_table__ = "IrcID"

    id = Int(primary=True)
    person_id = Int(name="person", allow_none=False)
    person = Reference(person_id, "Person.id")
    network = Unicode(name="network", allow_none=False)
    nickname = Unicode(name="nickname", allow_none=False)

    def __init__(self, person, network, nickname):
        super().__init__()
        self.person = person
        self.network = network
        self.nickname = nickname

    def destroySelf(self):
        IStore(self).remove(self)


@implementer(IIrcIDSet)
class IrcIDSet:
    """See `IIrcIDSet`"""

    def get(self, id):
        """See `IIrcIDSet`"""
        return IStore(IrcID).get(IrcID, int(id))

    def new(self, person, network, nickname):
        """See `IIrcIDSet`"""
        irc_id = IrcID(person=person, network=network, nickname=nickname)
        IStore(irc_id).flush()
        return irc_id


class NicknameGenerationError(Exception):
    """I get raised when something went wrong generating a nickname."""


def _is_nick_registered(nick):
    """Answer the question: is this nick registered?"""
    return PersonSet().getByName(nick) is not None


def generate_nick(email_addr, is_registered=_is_nick_registered):
    """Generate a LaunchPad nick from the email address provided.

    See lp.app.validators.name for the definition of a
    valid nick.

    It is technically possible for this function to raise a
    NicknameGenerationError, but this will only occur if an operator
    has majorly screwed up the name blocklist.
    """
    email_addr = email_addr.strip().lower()

    if not valid_email(email_addr):
        raise NicknameGenerationError(
            "%s is not a valid email address" % email_addr
        )

    user = re.match(r"^(\S+)@(?:\S+)$", email_addr).groups()[0]
    user = user.replace(".", "-").replace("_", "-")

    person_set = PersonSet()

    def _valid_nick(nick):
        if not valid_name(nick):
            return False
        elif is_registered(nick):
            return False
        elif person_set.isNameBlocklisted(nick):
            return False
        else:
            return True

    generated_nick = sanitize_name(user)
    if _valid_nick(generated_nick):
        return generated_nick

    # We seed the random number generator so we get consistent results,
    # making the algorithm repeatable and thus testable.
    random_state = random.getstate()
    random.seed(sum(ord(letter) for letter in email_addr))
    try:
        attempts = 0
        prefix = ""
        suffix = ""
        mutated_nick = [letter for letter in generated_nick]
        chars = "abcdefghijklmnopqrstuvwxyz0123456789"
        while attempts < 1000:
            attempts += 1

            # Prefer a nickname with a suffix
            suffix += random.choice(chars)
            if _valid_nick(generated_nick + "-" + suffix):
                return generated_nick + "-" + suffix

            # Next a prefix
            prefix += random.choice(chars)
            if _valid_nick(prefix + "-" + generated_nick):
                return prefix + "-" + generated_nick

            # Or a mutated character
            index = random.randint(0, len(mutated_nick) - 1)
            mutated_nick[index] = random.choice(chars)
            if _valid_nick("".join(mutated_nick)):
                return "".join(mutated_nick)

            # Or a prefix + generated + suffix
            if _valid_nick(prefix + "-" + generated_nick + "-" + suffix):
                return prefix + "-" + generated_nick + "-" + suffix

            # Or a prefix + mutated + suffix
            if _valid_nick(
                prefix + "-" + "".join(mutated_nick) + "-" + suffix
            ):
                return prefix + "-" + "".join(mutated_nick) + "-" + suffix

        raise NicknameGenerationError(
            "No nickname could be generated. "
            "This should be impossible to trigger unless some twonk has "
            "registered a match everything regexp in the blocklist."
        )

    finally:
        random.setstate(random_state)


@adapter(IAccount)
@implementer(IPerson)
def person_from_account(account):
    """Adapt an `IAccount` into an `IPerson`.

    If there is a current browser request, we cache the looked up Person in
    the request's annotations so that we don't have to hit the DB once again
    when further adaptation is needed.  We know this cache may cross
    transaction boundaries, but this should not be a problem as the Person ->
    Account link can't be changed.

    This cache is necessary because our security adapters may need to adapt
    the Account representing the logged in user into an IPerson multiple
    times.
    """
    request = get_current_browser_request()
    person = None
    # First we try to get the person from the cache, but only if there is a
    # browser request.
    if request is not None:
        cache = request.annotations.setdefault(
            "launchpad.person_to_account_cache", weakref.WeakKeyDictionary()
        )
        person = cache.get(account)

    # If it's not in the cache, then we get it from the database, and in that
    # case, if there is a browser request, we also store that person in the
    # cache.
    if person is None:
        person = IStore(Person).find(Person, account=account).one()
        if request is not None:
            cache[account] = person

    if person is None:
        raise ComponentLookupError()
    return person


@ProxyFactory
def get_recipients(person):
    """Return a set of people who receive email for this Person (person/team).

    If <person> has a preferred email, then if <person> is a team or has an
    active account the set will contain only that person, otherwise the set
    will be empty.  If <person> doesn't have a preferred email but is a
    team, the set will contain the preferred email address of each member of
    <person>, including indirect members, that has an active account and a
    preferred (active) address.

    Finally, if <person> doesn't have a preferred email and is not a team,
    the set will be empty.
    """
    if removeSecurityProxy(person).preferredemail:
        if person.is_team or person.account_status == AccountStatus.ACTIVE:
            return [person]
        else:
            return []
    elif person.is_team:
        # Get transitive members of a team that does not itself have a
        # preferred email.
        return _get_recipients_for_team(person)
    else:
        return []


def _get_recipients_for_team(team):
    """Given a team without a preferred email, return recipients.

    Helper for get_recipients, divided out simply to make get_recipients
    easier to understand in broad brush."""
    store = IStore(Person)
    source = store.using(
        TeamMembership,
        Join(Person, TeamMembership.person_id == Person.id),
        LeftJoin(
            EmailAddress,
            And(
                EmailAddress.person == Person.id,
                EmailAddress.status == EmailAddressStatus.PREFERRED,
            ),
        ),
        LeftJoin(Account, Person.account_id == Account.id),
    )
    pending_team_ids = [team.id]
    recipient_ids = set()
    seen = set()
    while pending_team_ids:
        # Find Persons that have a preferred email address and an active
        # account, or are a team, or both.
        intermediate_transitive_results = source.find(
            (TeamMembership.person_id, EmailAddress.person_id),
            TeamMembership.status.is_in(ACTIVE_STATES),
            TeamMembership.team_id.is_in(pending_team_ids),
            Or(
                And(
                    EmailAddress.person != None,
                    Account.status == AccountStatus.ACTIVE,
                ),
                IsNot(Person.teamowner_id, None),
            ),
        ).config(distinct=True)
        next_ids = []
        for (
            person_id,
            preferred_email_marker,
        ) in intermediate_transitive_results:
            if preferred_email_marker is None:
                # This is a team without a preferred email address.
                if person_id not in seen:
                    next_ids.append(person_id)
                    seen.add(person_id)
            else:
                recipient_ids.add(person_id)
        pending_team_ids = next_ids
    return getUtility(IPersonSet).getPrecachedPersonsFromIDs(
        recipient_ids, need_validity=True, need_preferred_email=True
    )
