# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Git repository interfaces."""

__all__ = [
    "ContributorGitIdentity",
    "GitIdentityMixin",
    "GIT_REPOSITORY_NAME_VALIDATION_ERROR_MESSAGE",
    "git_repository_name_validator",
    "IGitRepository",
    "IGitRepositoryDelta",
    "IGitRepositoryExpensiveRequest",
    "IGitRepositorySet",
    "IHasGitRepositoryURL",
    "user_has_special_git_repository_access",
]

import re
from textwrap import dedent

from lazr.lifecycle.snapshot import doNotSnapshot
from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    collection_default_content,
    export_destructor_operation,
    export_factory_operation,
    export_operation_as,
    export_read_operation,
    export_write_operation,
    exported,
    exported_as_webservice_collection,
    exported_as_webservice_entry,
    mutator_for,
    operation_for_version,
    operation_parameters,
    operation_returns_collection_of,
    operation_returns_entry,
    scoped,
)
from lazr.restful.fields import CollectionField, Reference
from lazr.restful.interface import copy_field
from zope.component import getUtility
from zope.interface import Attribute, Interface
from zope.schema import (
    Bool,
    Choice,
    Datetime,
    Int,
    List,
    Text,
    TextLine,
    Tuple,
)

from lp import _
from lp.app.enums import InformationType
from lp.app.validators import LaunchpadValidationError
from lp.code.enums import (
    BranchMergeProposalStatus,
    BranchSubscriptionDiffSize,
    BranchSubscriptionNotificationLevel,
    CodeReviewNotificationLevel,
    GitListingSort,
    GitRepositoryStatus,
    GitRepositoryType,
)
from lp.code.interfaces.defaultgit import ICanHasDefaultGitRepository
from lp.code.interfaces.hasgitrepositories import IHasGitRepositories
from lp.code.interfaces.hasrecipes import IHasRecipes
from lp.code.interfaces.revisionstatus import IRevisionStatusReport
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.ociproject import IOCIProject
from lp.registry.interfaces.person import IPerson
from lp.registry.interfaces.persondistributionsourcepackage import (
    IPersonDistributionSourcePackageFactory,
)
from lp.registry.interfaces.personociproject import IPersonOCIProjectFactory
from lp.registry.interfaces.personproduct import IPersonProductFactory
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.role import IPersonRoles
from lp.services.auth.enums import AccessTokenScope
from lp.services.auth.interfaces import (
    IAccessTokenTarget,
    IAccessTokenTargetEdit,
)
from lp.services.fields import InlineObject, PersonChoice, PublicPersonChoice
from lp.services.webhooks.interfaces import IWebhookTarget

GIT_REPOSITORY_NAME_VALIDATION_ERROR_MESSAGE = _(
    "Git repository names must start with a number or letter.  The characters "
    "+, -, _, . and @ are also allowed after the first character.  Repository "
    'names must not end with ".git".'
)


# This is a copy of the pattern in database/schema/patch-2209-61-0.sql.
# Don't change it without changing that.
valid_git_repository_name_pattern = re.compile(
    r"(?i)^[a-z0-9][a-z0-9+\.\-@_]*\Z"
)


def valid_git_repository_name(name):
    """Return True iff the name is valid as a Git repository name.

    The rules for what is a valid Git repository name are described in
    GIT_REPOSITORY_NAME_VALIDATION_ERROR_MESSAGE.
    """
    if not name.endswith(".git") and valid_git_repository_name_pattern.match(
        name
    ):
        return True
    return False


def git_repository_name_validator(name):
    """Return True if the name is valid, or raise LaunchpadValidationError."""
    if not valid_git_repository_name(name):
        raise LaunchpadValidationError(
            _(
                "Invalid Git repository name '${name}'. ${message}",
                mapping={
                    "name": name,
                    "message": GIT_REPOSITORY_NAME_VALIDATION_ERROR_MESSAGE,
                },
            )
        )
    return True


class IGitRepositoryView(IHasRecipes, IAccessTokenTarget):
    """IGitRepository attributes that require launchpad.View permission."""

    id = exported(Int(title=_("ID"), readonly=True, required=True))

    date_created = exported(
        Datetime(title=_("Date created"), required=True, readonly=True)
    )

    repository_type = exported(
        Choice(
            title=_("Repository type"),
            required=True,
            readonly=True,
            vocabulary=GitRepositoryType,
            description=_(
                "The way this repository is hosted: directly on Launchpad, or "
                "imported from somewhere else."
            ),
        )
    )

    status = Choice(
        title=_("Status of this repository"),
        required=True,
        readonly=True,
        vocabulary=GitRepositoryStatus,
    )

    registrant = exported(
        PublicPersonChoice(
            title=_("Registrant"),
            required=True,
            readonly=True,
            vocabulary="ValidPersonOrTeam",
            description=_("The person who registered this Git repository."),
        )
    )

    owner = exported(
        PersonChoice(
            title=_("Owner"),
            required=True,
            readonly=True,
            vocabulary="AllUserTeamsParticipationPlusSelf",
            description=_(
                "The owner of this Git repository. This controls who can "
                "modify the repository."
            ),
        )
    )

    target = exported(
        Reference(
            title=_("Target"),
            required=True,
            readonly=True,
            schema=IHasGitRepositories,
            description=_("The target of the repository."),
        ),
        as_of="devel",
    )

    namespace = Attribute(
        "The namespace of this repository, as an `IGitNamespace`."
    )

    # XXX cjwatson 2015-01-29: Add some advice about default repository
    # naming.
    name = exported(
        TextLine(
            title=_("Name"),
            required=True,
            readonly=True,
            constraint=git_repository_name_validator,
            description=_(
                "The repository name. Keep very short, unique, and "
                "descriptive, because it will be used in URLs."
            ),
        )
    )

    information_type = exported(
        Choice(
            title=_("Information type"),
            vocabulary=InformationType,
            required=True,
            readonly=True,
            default=InformationType.PUBLIC,
            description=_(
                "The type of information contained in this repository."
            ),
        )
    )

    owner_default = exported(
        Bool(
            title=_("Owner default"),
            required=True,
            readonly=True,
            description=_(
                "Whether this repository is the default for its owner and "
                "target."
            ),
        )
    )

    target_default = exported(
        Bool(
            title=_("Target default"),
            required=True,
            readonly=True,
            description=_(
                "Whether this repository is the default for its target."
            ),
        )
    )

    unique_name = exported(
        Text(
            title=_("Unique name"),
            readonly=True,
            description=_(
                "Unique name of the repository, including the owner and "
                "project names."
            ),
        )
    )

    display_name = exported(
        Text(
            title=_("Display name"),
            readonly=True,
            description=_("Display name of the repository."),
        )
    )

    code_reviewer = Attribute(
        "The reviewer if set, otherwise the owner of the repository."
    )

    shortened_path = Attribute(
        "The shortest reasonable version of the path to this repository."
    )

    pack_count = exported(
        Int(
            title=_("Pack count"),
            readonly=True,
            required=False,
            description=_("The number of packs for this repository."),
        )
    )

    loose_object_count = exported(
        Int(
            title=_("Loose object count"),
            readonly=True,
            required=False,
            description=_("The number of loose objects for this repository."),
        )
    )

    # XXX cjwatson 2021-06-22: This is actually when a repack was last
    # requested on the Launchpad side, not when the hosting service finished
    # the repack.
    date_last_repacked = exported(
        Datetime(
            title=_("Date last repacked"),
            readonly=True,
            required=False,
            description=_("The date that this repository was last repacked."),
        )
    )

    date_last_scanned = exported(
        Datetime(
            title=_("Date last scanned"),
            readonly=True,
            required=False,
            description=_(
                "The date when pack statistics were last updated "
                "for this repository."
            ),
        )
    )

    def getClonedFrom():
        """Returns from which repository the given repo is a clone from."""

    @operation_parameters(
        reviewer=Reference(
            title=_("A person for which the reviewer status is in question."),
            schema=IPerson,
        )
    )
    @export_read_operation()
    @operation_for_version("devel")
    def isPersonTrustedReviewer(reviewer):
        """Return true if the `reviewer` is a trusted reviewer.

        The reviewer is trusted if they either own the repository, or are in
        the team that owns the repository, or they are in the review team
        for the repository.
        """

    git_identity = exported(
        Text(
            title=_("Git identity"),
            readonly=True,
            description=_(
                "If this is the default repository for some target, then this "
                "is 'lp:' plus a shortcut version of the path via that "
                "target.  Otherwise it is simply 'lp:' plus the unique name."
            ),
        )
    )

    identity = Attribute(
        "The identity of this repository: a VCS-independent synonym for "
        "git_identity."
    )

    git_https_url = exported(
        TextLine(
            title=_("HTTPS URL"),
            readonly=True,
            description=_(
                "An HTTPS URL for this repository, or None in the case of "
                "private repositories."
            ),
        )
    )

    git_ssh_url = exported(
        TextLine(
            title=_("SSH URL"),
            readonly=True,
            description=_("A git+ssh:// URL for this repository."),
        )
    )

    refs = exported(
        doNotSnapshot(
            CollectionField(
                title=_("The references present in this repository."),
                readonly=True,
                # Really IGitRef, patched in _schema_circular_imports.py.
                value_type=Reference(Interface),
            )
        )
    )

    branches = exported(
        doNotSnapshot(
            CollectionField(
                title=_("The branch references present in this repository."),
                readonly=True,
                # Really IGitRef, patched in _schema_circular_imports.py.
                value_type=Reference(Interface),
            )
        )
    )

    branches_by_date = Attribute(
        "The branch references present in this repository, ordered by last "
        "commit date."
    )

    subscriptions = exported(
        CollectionField(
            title=_("GitSubscriptions associated with this repository."),
            readonly=True,
            # Really IGitSubscription, patched in _schema_circular_imports.py.
            value_type=Reference(Interface),
        )
    )

    subscribers = exported(
        CollectionField(
            title=_("Persons subscribed to this repository."),
            readonly=True,
            value_type=Reference(IPerson),
        )
    )

    code_import = exported(
        Reference(
            title=_("The associated CodeImport, if any."),
            # Really ICodeImport, patched in _schema_circular_imports.py.
            schema=Interface,
        )
    )

    rules = Attribute("The access rules for this repository.")

    grants = Attribute("The access grants for this repository.")

    @operation_parameters(
        path=TextLine(title=_("A string to look up as a path."))
    )
    # Really IGitRef, patched in _schema_circular_imports.py.
    @operation_returns_entry(Interface)
    @export_read_operation()
    @operation_for_version("devel")
    def getRefByPath(path):
        """Look up a single reference in this repository by path.

        :param path: A string to look up as a path.

        :return: An `IGitRef`, or None.
        """

    def createOrUpdateRefs(refs_info, get_objects=False, logger=None):
        """Create or update a set of references in this repository.

        :param refs_info: A dict mapping ref paths to
            {"sha1": sha1, "type": `GitObjectType`}.
        :param get_objects: Return the created/updated references.
        :param logger: An optional logger.

        :return: A list of the created/updated references if get_objects,
            otherwise None.
        """

    def removeRefs(paths):
        """Remove a set of references in this repository.

        :params paths: An iterable of paths.
        """

    def planRefChanges(hosting_path, logger=None):
        """Plan ref changes based on information from the hosting service.

        :param hosting_path: A path on the hosting service.
        :param logger: An optional logger.

        :return: A dict of refs to create or update as appropriate, mapping
            ref paths to dictionaries of their fields; and a set of ref
            paths to remove.
        """

    def fetchRefCommits(refs, filter_paths=None, logger=None):
        """Fetch commit information from the hosting service for a set of refs.

        :param refs: A dict mapping ref paths to dictionaries of their
            fields; the field dictionaries will be updated with any detailed
            commit information that is available.
        :param filter_paths: If given, only return commits that modify paths
            in this list, and include the contents of the files at those
            paths in the response.
        :param logger: An optional logger.
        """

    def synchroniseRefs(refs_to_upsert, refs_to_remove, logger=None):
        """Synchronise references with those from the hosting service.

        :param refs_to_upsert: A dictionary mapping ref paths to
            dictionaries of their fields; these refs will be created or
            updated as appropriate.
        :param refs_to_remove: A set of ref paths to remove.
        :param logger: An optional logger.
        """

    def setOwnerDefault(value):
        """Set whether this repository is the default for its owner-target.

        This is for internal use; the caller should ensure permission to
        edit the owner, should arrange to remove any existing owner-target
        default, and should check that this repository is attached to the
        desired target.

        :param value: True if this repository should be the owner-target
            default, otherwise False.
        """

    def setTargetDefault(value):
        """Set whether this repository is the default for its target.

        This is for internal use; the caller should ensure permission to
        edit the target, should arrange to remove any existing target
        default, and should check that this repository is attached to the
        desired target.

        :param value: True if this repository should be the target default,
            otherwise False.
        """

    def getCodebrowseUrl(username=None, password=None):
        """Construct a browsing URL for this Git repository.

        :param username: Include the given username in the URL (optional).
        :param password: Include the given password in the URL (optional).
        """

    def getCodebrowseUrlForRevision(commit):
        """The URL to the commit of the merge to the target branch"""

    def getLatestScanJob():
        """Return the last IGitRefScanJobSource for this repository"""

    def visibleByUser(user):
        """Can the specified user see this repository?"""

    def getAllowedInformationTypes(user):
        """Get a list of acceptable `InformationType`s for this repository.

        If the user is a Launchpad admin, any type is acceptable.
        """

    def getInternalPath():
        """Get the internal path to this repository.

        This is used on the storage backend.
        """

    def getRepositoryDefaults():
        """Return a sorted list of `ICanHasDefaultGitRepository` objects.

        There is one result for each related object for which this
        repository is the default.  For example, in the case where a
        repository is the default for a project and is also its owner's
        default repository for that project, the objects for both the
        project and the person-project are returned.

        More important related objects are sorted first.
        """

    # Marker for references to Git URL layouts: ##GITNAMESPACE##
    def getRepositoryIdentities():
        """A list of aliases for a repository.

        Returns a list of tuples of path and context object.  There is at
        least one alias for any repository, and that is the repository
        itself.  For default repositories, the context object is the
        appropriate default object.

        Where a repository is the default for a product, distribution
        source package or OCI project, the repository is available
        through a number of different URLs.
        These URLs are the aliases for the repository.

        For example, a repository which is the default for the 'fooix'
        project and which is also its owner's default repository for that
        project is accessible using:
          fooix - the context object is the project fooix
          ~fooix-owner/fooix - the context object is the person-project
              ~fooix-owner and fooix
          ~fooix-owner/fooix/+git/fooix - the unique name of the repository
              where the context object is the repository itself.
        """

    def userCanBeSubscribed(person):
        """Return True if the `IPerson` can be subscribed to the repository."""

    @operation_parameters(
        person=Reference(title=_("The person to subscribe."), schema=IPerson),
        notification_level=Choice(
            title=_("The level of notification to subscribe to."),
            vocabulary=BranchSubscriptionNotificationLevel,
        ),
        max_diff_lines=Choice(
            title=_("The max number of lines for diff email."),
            vocabulary=BranchSubscriptionDiffSize,
        ),
        code_review_level=Choice(
            title=_("The level of code review notification emails."),
            vocabulary=CodeReviewNotificationLevel,
        ),
    )
    # Really IGitSubscription, patched in _schema_circular_imports.py.
    @operation_returns_entry(Interface)
    @call_with(subscribed_by=REQUEST_USER)
    @export_write_operation()
    @operation_for_version("devel")
    def subscribe(
        person,
        notification_level,
        max_diff_lines,
        code_review_level,
        subscribed_by,
    ):
        """Subscribe this person to the repository.

        :param person: The `Person` to subscribe.
        :param notification_level: The kinds of repository changes that
            cause notification.
        :param max_diff_lines: The maximum number of lines of diff that may
            appear in a notification.
        :param code_review_level: The kinds of code review activity that
            cause notification.
        :param subscribed_by: The person who is subscribing the subscriber.
            Most often the subscriber themselves.
        :return: A new or existing `GitSubscription`.
        """

    @operation_parameters(
        person=Reference(title=_("The person to search for"), schema=IPerson)
    )
    # Really IGitSubscription, patched in _schema_circular_imports.py.
    @operation_returns_entry(Interface)
    @export_read_operation()
    @operation_for_version("devel")
    def getSubscription(person):
        """Return the `GitSubscription` for this person."""

    def hasSubscription(person):
        """Is this person subscribed to the repository?"""

    @operation_parameters(
        person=Reference(title=_("The person to unsubscribe"), schema=IPerson)
    )
    @call_with(unsubscribed_by=REQUEST_USER)
    @export_write_operation()
    @operation_for_version("devel")
    def unsubscribe(person, unsubscribed_by):
        """Remove the person's subscription to this repository.

        :param person: The person or team to unsubscribe from the repository.
        :param unsubscribed_by: The person doing the unsubscribing.
        """

    def getSubscriptionsByLevel(notification_levels):
        """Return the subscriptions that are at the given notification levels.

        :param notification_levels: An iterable of
            `BranchSubscriptionNotificationLevel`s.
        :return: A `ResultSet`.
        """

    def getNotificationRecipients():
        """Return a complete INotificationRecipientSet instance.

        The INotificationRecipientSet instance contains the subscribers
        and their subscriptions.
        """

    landing_targets = Attribute(
        "A collection of the merge proposals where this repository is "
        "the source."
    )
    _api_landing_targets = exported(
        doNotSnapshot(
            CollectionField(
                title=_("Landing targets"),
                description=_(
                    "A collection of the merge proposals where this "
                    "repository is the source."
                ),
                readonly=True,
                # Really IBranchMergeProposal, patched in
                # _schema_circular_imports.py.
                value_type=Reference(Interface),
            )
        ),
        exported_as="landing_targets",
    )
    landing_candidates = Attribute(
        "A collection of the merge proposals where this repository is "
        "the target."
    )
    _api_landing_candidates = exported(
        doNotSnapshot(
            CollectionField(
                title=_("Landing candidates"),
                description=_(
                    "A collection of the merge proposals where this "
                    "repository is the target."
                ),
                readonly=True,
                # Really IBranchMergeProposal, patched in
                # _schema_circular_imports.py.
                value_type=Reference(Interface),
            )
        ),
        exported_as="landing_candidates",
    )
    dependent_landings = exported(
        doNotSnapshot(
            CollectionField(
                title=_("Dependent landings"),
                description=_(
                    "A collection of the merge proposals that are dependent "
                    "on this repository."
                ),
                readonly=True,
                # Really IBranchMergeProposal, patched in
                # _schema_circular_imports.py.
                value_type=Reference(Interface),
            )
        )
    )

    def getPrecachedLandingTargets(user):
        """Return precached landing targets.

        Target and prerequisite repositories are preloaded.
        """

    def getPrecachedLandingCandidates(user):
        """Return precached landing candidates.

        Source and prerequisite repositories are preloaded.
        """

    @operation_parameters(
        status=List(
            title=_("A list of statuses to filter the merge proposals by."),
            value_type=Choice(vocabulary=BranchMergeProposalStatus),
        ),
        merged_revision_ids=List(
            TextLine(title=_("The target revision ID of the merge."))
        ),
    )
    @call_with(visible_by_user=REQUEST_USER)
    # Really IBranchMergeProposal, patched in _schema_circular_imports.py.
    @operation_returns_collection_of(Interface)
    @export_read_operation()
    @operation_for_version("devel")
    def getMergeProposals(
        status=None,
        visible_by_user=None,
        merged_revision_ids=None,
        eager_load=False,
    ):
        """Return matching BranchMergeProposals."""

    def getMergeProposalByID(id):
        """Return this repository's merge proposal with this id, or None."""

    def isRepositoryMergeable(other):
        """Is the other repository mergeable into this one (or vice versa)?"""

    pending_updates = Attribute(
        "Whether there are recent changes in this repository that have not "
        "yet been scanned."
    )

    def updateMergeCommitIDs(paths):
        """Update commit SHA1s of merge proposals for this repository.

        The *_git_commit_sha1 columns of merge proposals are stored
        explicitly in order that merge proposals are still meaningful after
        associated refs have been deleted.  However, active merge proposals
        where the refs in question still exist should have these columns
        kept up to date.
        """

    def updateLandingTargets(paths):
        """Update landing targets (MPs where this repository is the source).

        For each merge proposal, create `UpdatePreviewDiffJob`s.

        :param paths: A list of reference paths.  Any merge proposals whose
            source is this repository and one of these paths will have their
            diffs updated.
        """

    def makeFrozenRef(path, commit_sha1):
        """A frozen Git reference.

        This is like a GitRef, but is frozen at a particular commit, even if
        the real reference has moved on or has been deleted.
        It isn't necessarily backed by a real database object,
        and will retrieve columns from the database when required.
        Use this when you have a repository/path/commit_sha1 that you want
        to pass around as a single object,
        but don't necessarily know that the ref still exists.

        :param path: the repository reference path.
        :param commit_sha1: the commit sha1 for that repository reference path.
        """

    def markRecipesStale(paths):
        """Mark recipes associated with this repository as stale.

        :param paths: A list of reference paths.  Any recipes that include
            an entry that points to this repository and that have a
            `revspec` that is one of these paths will be marked as stale.
        """

    def markSnapsStale(paths):
        """Mark snap packages associated with this repository as stale.

        :param paths: A list of reference paths.  Any snap packages that
            include an entry that points to this repository and that are
            based on one of these paths will be marked as stale.
        """

    def markCharmRecipesStale(paths):
        """Mark charm recipes associated with this repository as stale.

        :param paths: A list of reference paths.  Any charm recipes that
            include an entry that points to this repository and that are
            based on one of these paths will be marked as stale.
        """

    def detectMerges(paths, previous_targets, logger=None):
        """Detect merges of landing candidates.

        :param paths: A list of reference paths.  Any merge proposals whose
            target is this repository and one of these paths will be
            checked.
        :param previous_targets: A dictionary mapping merge proposal IDs to
            their previous target commit IDs, before the current ref scan
            job updated them.
        :param logger: An optional logger.
        """

    def getBlob(filename, rev=None):
        """Get a blob by file name from this repository.

        :param filename: Relative path of a file in the repository.
        :param rev: An optional revision. Defaults to 'HEAD'.
        :return: A binary string with the blob content.
        """

    def getDiff(old, new):
        """Get the diff between two commits in this repository.

        :param old: The OID of the old commit.
        :param new: The OID of the new commit.
        :return: The diff as a binary string.
        """

    def getRule(ref_pattern):
        """Get the access rule for this repository with a given pattern.

        :param ref_pattern: The reference pattern that the rule should have.
        :return: An `IGitRule`, or None.
        """

    def getActivity(changed_after=None):
        """Get activity log entries for this repository.

        :param changed_after: If supplied, only return entries for changes
            made after this date.
        :return: A `ResultSet` of `IGitActivity`.
        """

    def getPrecachedActivity(**kwargs):
        """Activity log entries are preloaded.

        :param changed_after: If supplied, only return entries for changes
            made after this date.
        :return: A `ResultSet` of `IGitActivity`.
        """

    # XXX ines-almeida 2023-09-08: This overwrites the definition in
    # IAccessTokenTarget because we want to generate serialised macaroons in
    # certain cases for git repositories specifically. Once
    # `snapcraft remote-build` stops using the old workflow (see
    # https://github.com/snapcore/snapcraft/pull/4270), this can be removed in
    # favour of the general definition in `IAccessTokenTarget`.
    # Note that `snap info snapcraft` still lists a number of older versions
    # of snapcraft from before that change that are still supported.
    @operation_parameters(
        description=TextLine(
            title=_("A short description of the token."), required=False
        ),
        scopes=List(
            title=_("A list of scopes to be granted by this token."),
            value_type=Choice(vocabulary=AccessTokenScope),
            required=False,
        ),
        date_expires=Datetime(
            title=_("When the token should expire."), required=False
        ),
    )
    @export_write_operation()
    @operation_for_version("devel")
    def issueAccessToken(description=None, scopes=None, date_expires=None):
        """Issue an access token for this repository.

        Access tokens can be used to push to this repository over HTTPS.
        They are only valid for a single repository, and have a short expiry
        period (currently fixed at one week), so at the moment they are only
        suitable in some limited situations.  By default they are currently
        implemented as macaroons.

        If `description` and `scopes` are both given, then issue a personal
        access token instead, either non-expiring or with an expiry time
        given by `date_expires`.  These may be used in webservice API
        requests for certain methods on this repository.

        This interface is experimental, and may be changed or removed
        without notice.

        :return: If `description` and `scopes` are both given, the secret
            for a new personal access token (Launchpad only records the hash
            of this secret and not the secret itself, so the caller must be
            careful to save this; personal access tokens are in development
            and may not entirely work yet).  Otherwise, a serialised
            macaroon.
        """

    @operation_parameters(
        commit_sha1=copy_field(IRevisionStatusReport["commit_sha1"])
    )
    @scoped(AccessTokenScope.REPOSITORY_BUILD_STATUS.title)
    @operation_returns_collection_of(Interface)
    @export_read_operation()
    @operation_for_version("devel")
    def getStatusReports(commit_sha1):
        """Retrieves the list of reports that exist for a commit.

        :param commit_sha1: The commit sha1 for the report.
        """

    @call_with(requester=REQUEST_USER)
    @operation_parameters(
        new_owner=Reference(
            title=_("The person who will own the forked repository."),
            schema=IPerson,
        )
    )
    # Really IGitRepository, patched in lp.code.interfaces.webservice.
    @operation_returns_entry(Interface)
    @export_write_operation()
    @operation_for_version("devel")
    def fork(requester, new_owner):
        """Fork this repository to the given user's account.

        :param requester: The IPerson performing this fork.
        :param new_owner: The IPerson that will own the forked repository.
        :return: The newly created GitRepository."""


class IGitRepositoryModerateAttributes(Interface):
    """IGitRepository attributes that can be edited by more than one
    community."""

    date_last_modified = exported(
        Datetime(title=_("Date last modified"), required=True, readonly=True)
    )

    reviewer = exported(
        PublicPersonChoice(
            title=_("Review Team"),
            required=False,
            readonly=False,
            vocabulary="ValidBranchReviewer",
            description=_(
                "The reviewer of a repository is the person or "
                "exclusive team that is responsible for reviewing "
                "proposals and merging into this repository."
            ),
        )
    )

    description = exported(
        Text(
            title=_("Description"),
            required=False,
            readonly=False,
            description=_("A short description of this repository."),
        )
    )


class IGitRepositoryModerate(Interface):
    """IGitRepository methods that can be called by more than one community."""

    @mutator_for(IGitRepositoryView["information_type"])
    @operation_parameters(
        information_type=copy_field(IGitRepositoryView["information_type"]),
    )
    @call_with(user=REQUEST_USER)
    @export_write_operation()
    @operation_for_version("devel")
    def transitionToInformationType(
        information_type, user, verify_policy=True
    ):
        """Set the information type for this repository.

        :param information_type: The `InformationType` to transition to.
        :param user: The `IPerson` who is making the change.
        :param verify_policy: Check if the new information type complies
            with the `IGitNamespacePolicy`.
        """

    @export_write_operation()
    @operation_for_version("devel")
    def rescan():
        """Force a rescan of this repository as a celery task.

        This may be helpful in cases where a previous scan crashed.
        """


class IGitRepositoryEditableAttributes(Interface):
    """IGitRepository attributes that can be edited.

    These attributes need launchpad.View to see, and launchpad.Edit to change.
    """

    default_branch = exported(
        TextLine(
            title=_("Default branch"),
            required=False,
            readonly=False,
            description=_(
                "The full path to the default branch for this repository, "
                "e.g. refs/heads/master."
            ),
        )
    )


class IGitRepositoryExpensiveRequest(Interface):
    """IGitRepository methods that require
    launchpad.ExpensiveRequest permission.
    """

    @export_write_operation()
    @operation_for_version("devel")
    def repackRepository():
        """Trigger a repack repository operation.

        Raises Unauthorized if the repack was attempted by a person
        that is not an admin or a registry expert."""

    @export_write_operation()
    @operation_for_version("devel")
    def collectGarbage():
        """Trigger a gc run for a given git repository.

        Raises Unauthorized if the repack was attempted by a person
        that is not an admin or a registry expert."""


class IGitRepositoryEdit(IWebhookTarget, IAccessTokenTargetEdit):
    """IGitRepository methods that require launchpad.Edit permission."""

    @mutator_for(IGitRepositoryView["name"])
    @call_with(user=REQUEST_USER)
    @operation_parameters(
        new_name=TextLine(title=_("The new name of the repository."))
    )
    @export_write_operation()
    @operation_for_version("devel")
    def setName(new_name, user):
        """Set the name of the repository to be `new_name`."""

    @mutator_for(IGitRepositoryView["owner"])
    @call_with(user=REQUEST_USER)
    @operation_parameters(
        new_owner=Reference(
            title=_("The new owner of the repository."), schema=IPerson
        )
    )
    @export_write_operation()
    @operation_for_version("devel")
    def setOwner(new_owner, user):
        """Set the owner of the repository to be `new_owner`."""

    @mutator_for(IGitRepositoryView["target"])
    @call_with(user=REQUEST_USER)
    @operation_parameters(
        target=Reference(
            title=_(
                "The project, distribution source package, or person the "
                "repository belongs to."
            ),
            schema=IHasGitRepositories,
            required=True,
        )
    )
    @export_write_operation()
    @operation_for_version("devel")
    def setTarget(target, user):
        """Set the target of the repository."""

    def scan(log=None):
        """
        Executes a synchronous scan of this repository.

        :return: A tuple with (upserted_refs, deleted_refs).
        """

    def addRule(ref_pattern, creator, position=None):
        """Add an access rule to this repository.

        :param ref_pattern: The reference pattern that the new rule should
            match.
        :param creator: The `IPerson` who is adding the rule.
        :param position: The list position at which to insert the rule, or
            None to append it.
        """

    def moveRule(rule, position, user):
        """Move a rule to a new position in its repository's rule order.

        :param rule: The `IGitRule` to move.
        :param position: The new position.  For example, 0 puts the rule at
            the start, while `len(repository.rules)` puts the rule at the
            end.  If the new position is before the end of the list, then
            other rules are shifted to later positions to make room.
        :param user: The `IPerson` who is moving the rule.
        """

    def findRuleGrantsByGrantee(
        grantee, include_transitive=True, ref_pattern=None
    ):
        """Find the grants for a grantee applied to this repository.

        :param grantee: The `IPerson` to search for, or an item of
            `GitGranteeType` other than `GitGranteeType.PERSON` to search
            for some other kind of entity.
        :param include_transitive: If False, match `grantee` exactly; if
            True (the default), also accept teams of which `grantee` is a
            member.
        :param ref_pattern: If not None, only return grants for rules with
            this ref_pattern.
        """

    @export_read_operation()
    @operation_for_version("devel")
    def getRules():
        """Get the access rules for this repository."""

    @operation_parameters(
        rules=List(
            title=_("Rules"),
            # Really IGitNascentRule, patched in
            # _schema_circular_imports.py.
            value_type=InlineObject(schema=Interface),
            description=_(
                dedent(
                    """\
                The new list of rules for this repository.

                For example::

                    [
                        {
                            "ref_pattern": "refs/heads/*",
                            "grants": [
                                {
                                    "grantee_type": "Repository owner",
                                    "can_create": true,
                                    "can_push": true,
                                    "can_force_push": true
                                }
                            ]
                        },
                        {
                            "ref_pattern": "refs/heads/stable/*",
                            "grants": [
                                {
                                    "grantee_type": "Person",
                                    "grantee_link": "/~example-stable-team",
                                    "can_create": true,
                                    "can_push": true
                                }
                            ]
                        }
                    ]"""
                )
            ),
        )
    )
    @call_with(user=REQUEST_USER)
    @export_write_operation()
    @operation_for_version("devel")
    def setRules(rules, user):
        """Set the access rules for this repository."""

    def checkRefPermissions(person, ref_paths):
        """Check a person's permissions on some references in this repository.

        :param person: An `IPerson` to check, or
            `GitGranteeType.REPOSITORY_OWNER` to check an anonymous
            repository owner.
        :param ref_paths: An iterable of reference paths (each of which may
            be either bytes or text).
        :return: A dict mapping reference paths to sets of
            `GitPermissionType`, corresponding to the requested person's
            effective permissions on each of the requested references.
        """

    def setRepackData(loose_object_count, pack_count):
        """Sets the repack parameters received from Turnip.

        :param loose_object_count: The number of loose objects that
            this repository currently has.
        :param pack_count: The number of packs that
            this repository currently has.
        """

    @operation_parameters(
        person=Reference(title=_("Person to check"), schema=IPerson),
        paths=List(title=_("Reference paths"), value_type=TextLine()),
    )
    @export_operation_as("checkRefPermissions")
    @export_read_operation()
    @operation_for_version("devel")
    def api_checkRefPermissions(person, paths):
        """Check a person's permissions on some references in this repository.

        :param person: An `IPerson` to check.
        :param paths: An iterable of reference paths.
        :return: A dict mapping reference paths to lists of zero or more of
            "create", "push", and "force-push", indicating the requested
            person's effective permissions on each of the requested
            references.
        """

    @export_read_operation()
    @operation_for_version("devel")
    def canBeDeleted():
        """Can this repository be deleted in its current state?

        A repository is considered deletable if it is not linked to any
        merge proposals.
        """

    def getDeletionRequirements(eager_load=False):
        """Determine what is required to delete this branch.

        :param eager_load: If True, preload related information needed to
            display the deletion requirements.
        :return: a dict of {object: (operation, reason)}, where object is the
            object that must be deleted or altered, operation is either
            "delete" or "alter", and reason is a string explaining why the
            object needs to be touched.
        """

    @call_with(break_references=True)
    @export_destructor_operation()
    @operation_for_version("devel")
    def destroySelf(break_references=False):
        """Delete the specified repository.

        :param break_references: If supplied, break any references to this
            repository by deleting items with mandatory references and
            NULLing other references.
        :raise: CannotDeleteGitRepository if the repository cannot be deleted.
        """

    @operation_parameters(
        title=copy_field(IRevisionStatusReport["title"]),
        commit_sha1=copy_field(IRevisionStatusReport["commit_sha1"]),
        url=copy_field(IRevisionStatusReport["url"]),
        result_summary=copy_field(IRevisionStatusReport["result_summary"]),
        result=copy_field(IRevisionStatusReport["result"]),
    )
    @scoped(AccessTokenScope.REPOSITORY_BUILD_STATUS.title)
    @call_with(user=REQUEST_USER)
    @export_factory_operation(IRevisionStatusReport, [])
    @operation_for_version("devel")
    def newStatusReport(title, commit_sha1, url, result_summary, result, user):
        """Create a new status report.

        :param title: The name of the new report.
        :param commit_sha1: The commit sha1 for the report.
        :param url: The external link of the status report.
        :param result_summary: The description of the new report.
        :param result: The result of the new report.
        """


class IGitRepositoryAdminAttributes(Interface):
    """`IGitRepository` attributes that can be edited by admins.

    These attributes need launchpad.View to see, and launchpad.Admin to change.
    """

    builder_constraints = exported(
        Tuple(
            title=_("Builder constraints"),
            required=False,
            readonly=False,
            value_type=Choice(vocabulary="BuilderResource"),
            description=_(
                "Builder resource tags required by builds of this repository."
            ),
        ),
        as_of="devel",
    )


# XXX cjwatson 2015-01-19 bug=760849: "beta" is a lie to get WADL
# generation working.  Individual attributes must set their version to
# "devel".
@exported_as_webservice_entry(plural_name="git_repositories", as_of="beta")
class IGitRepository(
    IGitRepositoryView,
    IGitRepositoryModerateAttributes,
    IGitRepositoryModerate,
    IGitRepositoryEditableAttributes,
    IGitRepositoryEdit,
    IGitRepositoryExpensiveRequest,
    IGitRepositoryAdminAttributes,
):
    """A Git repository."""

    private = exported(
        Bool(
            title=_("Private"),
            required=False,
            readonly=True,
            description=_(
                "This repository is visible only to its subscribers."
            ),
        )
    )


@exported_as_webservice_collection(IGitRepository)
class IGitRepositorySet(Interface):
    """Interface representing the set of Git repositories."""

    @call_with(
        repository_type=GitRepositoryType.HOSTED,
        registrant=REQUEST_USER,
        with_hosting=True,
    )
    @operation_parameters(
        information_type=copy_field(
            IGitRepositoryView["information_type"], required=False
        )
    )
    @export_factory_operation(IGitRepository, ["owner", "target", "name"])
    @operation_for_version("devel")
    def new(
        repository_type,
        registrant,
        owner,
        target,
        name,
        information_type=None,
        date_created=None,
        with_hosting=False,
    ):
        """Create a Git repository and return it.

        :param repository_type: The `GitRepositoryType` of the new
            repository.
        :param registrant: The `IPerson` who registered the new repository.
        :param owner: The `IPerson` who owns the new repository.
        :param target: The `IProduct`, `IDistributionSourcePackage`,
            `IOCIProjectName`, or `IPerson` that the new repository is
            associated with.
        :param name: The repository name.
        :param information_type: Set the repository's information type to
            one different from the target's default.  The type must conform
            to the target's code sharing policy.  (optional)
        :param with_hosting: Create the repository on the hosting service.
        """

    @call_with(user=REQUEST_USER)
    @operation_parameters(id=Int(title=_("Repository ID"), required=True))
    @operation_returns_entry(IGitRepository)
    @export_read_operation()
    @operation_for_version("devel")
    def getByID(user, id):
        """Find a repository by its ID.

        Return None if no match was found.
        """

    # Marker for references to Git URL layouts: ##GITNAMESPACE##
    @call_with(user=REQUEST_USER)
    @operation_parameters(
        path=TextLine(title=_("Repository path"), required=True)
    )
    @operation_returns_entry(IGitRepository)
    @export_read_operation()
    @operation_for_version("devel")
    def getByPath(user, path):
        """Find a repository by its path.

        Any of these forms may be used::

            Unique names:
                ~OWNER/PROJECT/+git/NAME
                ~OWNER/DISTRO/+source/SOURCE/+git/NAME
                ~OWNER/+git/NAME
            Owner-target default aliases:
                ~OWNER/PROJECT
                ~OWNER/DISTRO/+source/SOURCE
            Official aliases:
                PROJECT
                DISTRO/+source/SOURCE

        Return None if no match was found.
        """

    @call_with(user=REQUEST_USER)
    @operation_parameters(
        target=Reference(
            title=_("Target"), required=False, schema=IHasGitRepositories
        ),
        order_by=Choice(
            title=_("Sort order"),
            vocabulary=GitListingSort,
            default=GitListingSort.MOST_RECENTLY_CHANGED_FIRST,
            required=False,
        ),
        modified_since_date=Datetime(
            title=_("Modified since date"),
            description=_(
                "Return only repositories whose `date_last_modified` is "
                "greater than or equal to this date."
            ),
        ),
    )
    @operation_returns_collection_of(IGitRepository)
    @export_read_operation()
    @operation_for_version("devel")
    def getRepositories(
        user,
        target=None,
        order_by=GitListingSort.MOST_RECENTLY_CHANGED_FIRST,
        modified_since_date=None,
    ):
        """Get all repositories for a target.

        :param user: An `IPerson`.  Only repositories visible by this user
            will be returned.
        :param target: An `IHasGitRepositories`, or None to get repositories
            for all targets.
        :param order_by: An item from the `GitListingSort` enumeration, or
            None to return an unordered result set.
        :param modified_since_date: If not None, return only repositories
            whose `date_last_modified` is greater than this date.

        :return: A collection of `IGitRepository` objects.
        """

    @export_read_operation()
    @operation_for_version("devel")
    def countRepositoriesForRepack():
        """Get number of repositories qualifying for a repack.

        :return: The number of `IGitRepository` objects qualifying
            for a repack.
        """

    @call_with(user=REQUEST_USER)
    @operation_parameters(
        person=Reference(
            title=_(
                "The person whose repository visibility is being " "checked."
            ),
            schema=IPerson,
        ),
        repository_names=List(
            value_type=Text(),
            title=_("List of repository unique names"),
            required=True,
        ),
    )
    @export_read_operation()
    @operation_for_version("devel")
    def getRepositoryVisibilityInfo(user, person, repository_names):
        """Return the named repositories visible to both user and person.

        Anonymous requesters don't get any information.

        :param user: The user requesting the information. If the user is
            None then we return an empty dict.
        :param person: The person whose repository visibility we wish to
            check.
        :param repository_names: The unique names of the repositories to
            check.

        Return a dict with the following values:
        person_name: the displayname of the person.
        visible_repositories: a list of the unique names of the repositories
        which the requester and specified person can both see.

        This API call is provided for use by the client Javascript.  It is
        not designed to efficiently scale to handle requests for large
        numbers of repositories.
        """

    @operation_parameters(
        target=Reference(
            title=_("Target"), required=True, schema=IHasGitRepositories
        )
    )
    @operation_returns_entry(IGitRepository)
    @export_read_operation()
    @operation_for_version("devel")
    def getDefaultRepository(target):
        """Get the default repository for a target.

        :param target: An `IHasGitRepositories`.

        :raises GitTargetError: if `target` is an `IPerson`.
        :return: An `IGitRepository`, or None.
        """

    @operation_parameters(
        owner=Reference(title=_("Owner"), required=True, schema=IPerson),
        target=Reference(
            title=_("Target"), required=True, schema=IHasGitRepositories
        ),
    )
    @operation_returns_entry(IGitRepository)
    @export_read_operation()
    @operation_for_version("devel")
    def getDefaultRepositoryForOwner(owner, target):
        """Get a person's default repository for a target.

        :param owner: An `IPerson`.
        :param target: An `IHasGitRepositories`.

        :raises GitTargetError: if `target` is an `IPerson`.
        :return: An `IGitRepository`, or None.
        """

    @operation_parameters(
        target=Reference(
            title=_("Target"), required=True, schema=IHasGitRepositories
        ),
        repository=Reference(
            title=_("Git repository"), required=False, schema=IGitRepository
        ),
    )
    @export_write_operation()
    @operation_for_version("devel")
    def setDefaultRepository(target, repository):
        """Set the default repository for a target.

        :param target: An `IHasGitRepositories`.
        :param repository: An `IGitRepository`, or None to unset the default
            repository.

        :raises GitTargetError: if `target` is an `IPerson`.
        """

    @call_with(user=REQUEST_USER)
    @operation_parameters(
        owner=Reference(title=_("Owner"), required=True, schema=IPerson),
        target=Reference(
            title=_("Target"), required=True, schema=IHasGitRepositories
        ),
        repository=Reference(
            title=_("Git repository"), required=False, schema=IGitRepository
        ),
    )
    @export_write_operation()
    @operation_for_version("devel")
    def setDefaultRepositoryForOwner(owner, target, repository, user):
        """Set a person's default repository for a target.

        :param owner: An `IPerson`.
        :param target: An `IHasGitRepositories`.
        :param repository: An `IGitRepository`, or None to unset the default
            repository.
        :param user: The `IPerson` who is making the change.

        :raises GitTargetError: if `target` is an `IPerson`.
        """

    @collection_default_content()
    def empty_list():
        """Return an empty collection of repositories.

        This only exists to keep lazr.restful happy.
        """

    def preloadDefaultRepositoriesForProjects(projects):
        """Get preloaded default repositories for a list of projects.

        :return: A dict mapping project IDs to their default repositories.
            Projects that do not have default repositories are omitted.
        """

    @operation_parameters(limit=Int())
    @export_read_operation()
    @operation_for_version("devel")
    def getRepositoriesForRepack(limit=50):
        """Get the top badly packed repositories.

        :param limit: The number of badly packed repositories
            that the endpoint should return - it is 50 by default.

        :return: A list of the worst badly packed repositories.
        """


class IGitRepositoryDelta(Interface):
    """The quantitative changes made to a Git repository that was edited or
    altered.
    """

    repository = Attribute("The IGitRepository, after it's been edited.")
    user = Attribute("The IPerson that did the editing.")

    # fields on the repository itself, we provide just the new changed value
    name = Attribute("Old and new names or None.")
    identity = Attribute("Old and new identities or None.")


class GitIdentityMixin:
    """This mixin class determines Git repository paths.

    Used by both the model GitRepository class and the browser repository
    listing item.  This allows the browser code to cache the associated
    context objects which reduces query counts.
    """

    @property
    def shortened_path(self):
        """See `IGitRepository`."""
        path, context = self.getRepositoryIdentities()[0]
        return path

    @property
    def git_identity(self):
        """See `IGitRepository`."""
        return "lp:" + self.shortened_path

    identity = git_identity

    def getRepositoryDefaults(self):
        """See `IGitRepository`."""
        defaults = []
        if self.target_default:
            defaults.append(ICanHasDefaultGitRepository(self.target))
        if self.owner_default:
            if IProduct.providedBy(self.target):
                factory = getUtility(IPersonProductFactory)
                default = factory.create(self.owner, self.target)
            elif IDistributionSourcePackage.providedBy(self.target):
                factory = getUtility(IPersonDistributionSourcePackageFactory)
                default = factory.create(self.owner, self.target)
            elif IOCIProject.providedBy(self.target):
                factory = getUtility(IPersonOCIProjectFactory)
                default = factory.create(self.owner, self.target)
            else:
                # Also enforced by database constraint.
                raise AssertionError(
                    "Only projects, packages, or OCI projects can have "
                    "owner-target default repositories."
                )
            defaults.append(ICanHasDefaultGitRepository(default))
        return sorted(defaults)

    def getRepositoryIdentities(self):
        """See `IGitRepository`."""
        identities = [
            (default.path, default.context)
            for default in self.getRepositoryDefaults()
        ]
        identities.append((self.unique_name, self))
        return identities


class IHasGitRepositoryURL(Interface):
    """Marker interface for objects that have a Git repository URL."""

    git_repository_url = Attribute(
        "The Git repository URL (possibly external)"
    )


def user_has_special_git_repository_access(user, repository=None):
    """Admins have special access.

    :param user: An `IPerson` or None.
    :param repository: An `IGitRepository` or None when checking collection
        access.
    """
    if user is None:
        return False
    roles = IPersonRoles(user)
    if roles.in_admin:
        return True
    if repository is None:
        return False
    code_import = repository.code_import
    if code_import is None:
        return False
    return roles.in_vcs_imports


class ContributorGitIdentity(GitIdentityMixin):
    def __init__(self, owner, target, repository):
        self.target_default = False
        self.owner_default = True
        self.owner = owner
        self.target = target
        self.repository = repository

    def getRepositoryIdentities(self):
        identities = [
            (default.path, default.context)
            for default in self.getRepositoryDefaults()
        ]
        return identities
