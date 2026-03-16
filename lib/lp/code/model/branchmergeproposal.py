# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database class for branch merge proposals."""

__all__ = [
    "BranchMergeProposal",
    "BranchMergeProposalGetter",
    "is_valid_transition",
    "PROPOSAL_MERGE_ENABLED_FEATURE_FLAG",
]

import sys
from collections import defaultdict
from datetime import timezone
from email.utils import make_msgid
from operator import attrgetter

from lazr.lifecycle.event import ObjectCreatedEvent, ObjectDeletedEvent
from storm.expr import SQL, And, Desc, Join, LeftJoin, Not, Or, Select
from storm.locals import DateTime, Int, Reference, Unicode
from storm.store import Store
from zope.component import getUtility
from zope.error.interfaces import IErrorReportingUtility
from zope.event import notify
from zope.interface import implementer
from zope.security.interfaces import Unauthorized

from lp.app.enums import PRIVATE_INFORMATION_TYPES
from lp.app.errors import NotFoundError
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.bugs.interfaces.bugtask import IBugTaskSet
from lp.bugs.interfaces.bugtaskfilter import filter_bugtasks_by_context
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.bugs.model.buglinktarget import BugLinkTargetMixin
from lp.code.enums import (
    BranchMergeProposalStatus,
    BranchSubscriptionDiffSize,
    BranchSubscriptionNotificationLevel,
    CodeReviewNotificationLevel,
    CodeReviewVote,
    GitPermissionType,
    MergeProposalCriteria,
    MergeType,
    RevisionStatusResult,
)
from lp.code.errors import (
    BadBranchMergeProposalSearchContext,
    BadStateTransition,
    BranchMergeProposalExists,
    BranchMergeProposalFeatureDisabled,
    BranchMergeProposalMergeFailed,
    BranchMergeProposalNotMergeable,
    DiffNotFound,
    UserNotBranchReviewer,
    WrongBranchMergeProposal,
)
from lp.code.event.branchmergeproposal import (
    BranchMergeProposalNeedsReviewEvent,
    ReviewerNominatedEvent,
)
from lp.code.interfaces.branch import IBranch
from lp.code.interfaces.branchcollection import IAllBranches
from lp.code.interfaces.branchmergeproposal import (
    BRANCH_MERGE_PROPOSAL_FINAL_STATES as FINAL_STATES,
)
from lp.code.interfaces.branchmergeproposal import (
    IBranchMergeProposal,
    IBranchMergeProposalGetter,
)
from lp.code.interfaces.branchtarget import IHasBranchTarget
from lp.code.interfaces.codereviewcomment import ICodeReviewComment
from lp.code.interfaces.codereviewinlinecomment import (
    ICodeReviewInlineCommentSet,
)
from lp.code.interfaces.githosting import IGitHostingClient
from lp.code.interfaces.gitref import IGitRef
from lp.code.mail.branch import RecipientReason
from lp.code.model.branchrevision import BranchRevision
from lp.code.model.codereviewcomment import CodeReviewComment
from lp.code.model.codereviewvote import CodeReviewVoteReference
from lp.code.model.diff import Diff, IncrementalDiff, PreviewDiff
from lp.registry.interfaces.person import (
    IPerson,
    IPersonSet,
    validate_person,
    validate_public_person,
)
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.model.person import Person
from lp.services.config import config
from lp.services.database.bulk import load, load_referencing, load_related
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.sqlbase import quote
from lp.services.database.stormbase import StormBase
from lp.services.features import getFeatureFlag
from lp.services.helpers import shortlist
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import Job
from lp.services.librarian.model import LibraryFileAlias
from lp.services.mail.sendmail import validate_message
from lp.services.messages.model.message import Message, MessageChunk
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.webapp.errorlog import ScriptRequest
from lp.services.xref.interfaces import IXRefSet
from lp.soyuz.enums import re_bug_numbers, re_lp_closes

PROPOSAL_MERGE_ENABLED_FEATURE_FLAG = "merge_proposal.merge.enabled"


def is_valid_transition(proposal, from_state, next_state, user=None):
    """Is it valid for this user to move this proposal to to next_state?

    :param proposal: The merge proposal.
    :param from_state: The previous state
    :param to_state: The new state to change to
    :param user: The user who may change the state
    """
    # Trivial acceptance case.
    if from_state == next_state:
        return True
    if from_state in FINAL_STATES and next_state not in FINAL_STATES:
        dupes = BranchMergeProposalGetter.activeProposalsForBranches(
            proposal.merge_source, proposal.merge_target
        )
        if not dupes.is_empty():
            return False

    [
        wip,
        needs_review,
        code_approved,
        rejected,
        merged,
        merge_failed,
        queued,
        superseded,
    ] = BranchMergeProposalStatus.items

    # Transitioning to code approved, rejected, or failed from work in
    # progress or needs review needs the user to be a valid reviewer, other
    # states are fine.
    valid_reviewer = proposal.merge_target.isPersonTrustedReviewer(user)
    reviewed_ok_states = (code_approved,)
    obsolete_states = (merge_failed, queued)
    if not valid_reviewer:
        # We cannot transition to obsolete states, and we no longer know how
        # to transition away from them either.
        if next_state in obsolete_states or from_state in obsolete_states:
            return False
        # Non-reviewers cannot reject proposals [XXX: what about their own?]
        if next_state == rejected:
            return False
        # Non-reviewers cannot approve proposals, but can otherwise move
        # things around relatively freely.
        elif (
            next_state in reviewed_ok_states
            and from_state not in reviewed_ok_states
        ):
            return False
        else:
            return True
    else:
        return True


class TooManyRelatedBugs(Exception):
    """A source branch has too many related bugs linked from commits."""


@implementer(IBranchMergeProposal, IHasBranchTarget)
class BranchMergeProposal(StormBase, BugLinkTargetMixin):
    """A relationship between a person and a branch."""

    __storm_table__ = "BranchMergeProposal"
    __storm_order__ = ("-date_created", "id")

    id = Int(primary=True)

    registrant_id = Int(
        name="registrant", validator=validate_public_person, allow_none=False
    )
    registrant = Reference(registrant_id, "Person.id")

    source_branch_id = Int(name="source_branch", allow_none=True)
    source_branch = Reference(source_branch_id, "Branch.id")
    source_git_repository_id = Int(
        name="source_git_repository", allow_none=True
    )
    source_git_repository = Reference(
        source_git_repository_id, "GitRepository.id"
    )
    source_git_path = Unicode(
        name="source_git_path", default=None, allow_none=True
    )
    source_git_commit_sha1 = Unicode(
        name="source_git_commit_sha1", default=None, allow_none=True
    )

    target_branch_id = Int(name="target_branch", allow_none=True)
    target_branch = Reference(target_branch_id, "Branch.id")
    target_git_repository_id = Int(
        name="target_git_repository", allow_none=True
    )
    target_git_repository = Reference(
        target_git_repository_id, "GitRepository.id"
    )
    target_git_path = Unicode(
        name="target_git_path", default=None, allow_none=True
    )
    target_git_commit_sha1 = Unicode(
        name="target_git_commit_sha1", default=None, allow_none=True
    )

    prerequisite_branch_id = Int(name="dependent_branch", allow_none=True)
    prerequisite_branch = Reference(prerequisite_branch_id, "Branch.id")
    prerequisite_git_repository_id = Int(
        name="dependent_git_repository", allow_none=True
    )
    prerequisite_git_repository = Reference(
        prerequisite_git_repository_id, "GitRepository.id"
    )
    prerequisite_git_path = Unicode(
        name="dependent_git_path", default=None, allow_none=True
    )
    prerequisite_git_commit_sha1 = Unicode(
        name="dependent_git_commit_sha1", default=None, allow_none=True
    )

    merge_type = DBEnum(
        enum=MergeType, allow_none=False, default=MergeType.UNKNOWN
    )

    @property
    def source_git_ref(self):
        if self.source_git_repository is None:
            return None
        return self.source_git_repository.makeFrozenRef(
            self.source_git_path,
            self.source_git_commit_sha1,
        )

    @source_git_ref.setter
    def source_git_ref(self, value):
        self.source_git_repository = value.repository
        self.source_git_path = value.path
        self.source_git_commit_sha1 = value.commit_sha1

    @property
    def target_git_ref(self):
        if self.target_git_repository is None:
            return None
        return self.target_git_repository.makeFrozenRef(
            self.target_git_path,
            self.target_git_commit_sha1,
        )

    @target_git_ref.setter
    def target_git_ref(self, value):
        self.target_git_repository = value.repository
        self.target_git_path = value.path
        self.target_git_commit_sha1 = value.commit_sha1

    @property
    def prerequisite_git_ref(self):
        if self.prerequisite_git_repository is None:
            return None
        return self.prerequisite_git_repository.makeFrozenRef(
            self.prerequisite_git_path,
            self.prerequisite_git_commit_sha1,
        )

    @prerequisite_git_ref.setter
    def prerequisite_git_ref(self, value):
        self.prerequisite_git_repository = value.repository
        self.prerequisite_git_path = value.path
        self.prerequisite_git_commit_sha1 = value.commit_sha1

    @property
    def merge_source(self):
        if self.source_branch is not None:
            return self.source_branch
        else:
            return self.source_git_ref

    @property
    def merge_target(self):
        if self.target_branch is not None:
            return self.target_branch
        else:
            return self.target_git_ref

    @property
    def merge_prerequisite(self):
        if self.prerequisite_branch is not None:
            return self.prerequisite_branch
        else:
            return self.prerequisite_git_ref

    @property
    def parent(self):
        if self.source_branch is not None:
            return self.source_branch
        else:
            return self.source_git_repository

    description = Unicode(default=None)

    whiteboard = Unicode(default=None)

    queue_status = DBEnum(
        enum=BranchMergeProposalStatus,
        allow_none=False,
        default=BranchMergeProposalStatus.WORK_IN_PROGRESS,
    )

    @property
    def private(self):
        objects = [
            self.merge_source,
            self.merge_target,
            self.merge_prerequisite,
        ]
        return any(
            obj is not None
            and obj.information_type in PRIVATE_INFORMATION_TYPES
            for obj in objects
        )

    reviewer_id = Int(
        name="reviewer",
        validator=validate_person,
        allow_none=True,
        default=None,
    )
    reviewer = Reference(reviewer_id, "Person.id")

    @property
    def next_preview_diff_job(self):
        # circular dependencies
        from lp.code.model.branchmergeproposaljob import (
            BranchMergeProposalJob,
            BranchMergeProposalJobType,
        )

        jobs = Store.of(self).find(
            BranchMergeProposalJob,
            BranchMergeProposalJob.branch_merge_proposal == self,
            BranchMergeProposalJob.job_type
            == BranchMergeProposalJobType.UPDATE_PREVIEW_DIFF,
            BranchMergeProposalJob.job == Job.id,
            Job._status.is_in([JobStatus.WAITING, JobStatus.RUNNING]),
        )
        job = jobs.order_by(Job.scheduled_start, Job.date_created).first()
        if job is not None:
            return job.makeDerived()
        else:
            return None

    reviewed_revision_id = Unicode(default=None)

    commit_message = Unicode(default=None)

    date_merged = DateTime(default=None, tzinfo=timezone.utc)
    merged_revno = Int(default=None)
    merged_revision_id = Unicode(default=None)

    @property
    def merged_revision(self):
        """Return the merged revision identifier."""
        if self.target_branch is not None:
            return self.merged_revno
        else:
            return self.merged_revision_id

    merge_reporter_id = Int(
        name="merge_reporter",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    merge_reporter = Reference(merge_reporter_id, "Person.id")

    def __init__(
        self,
        registrant,
        source_branch=None,
        source_git_repository=None,
        source_git_path=None,
        source_git_commit_sha1=None,
        target_branch=None,
        target_git_repository=None,
        target_git_path=None,
        target_git_commit_sha1=None,
        prerequisite_branch=None,
        prerequisite_git_repository=None,
        prerequisite_git_path=None,
        prerequisite_git_commit_sha1=None,
        description=None,
        whiteboard=None,
        queue_status=DEFAULT,
        commit_message=None,
        date_created=DEFAULT,
        date_review_requested=None,
    ):
        super().__init__()
        self.registrant = registrant
        self.source_branch = source_branch
        self.source_git_repository = source_git_repository
        self.source_git_path = source_git_path
        self.source_git_commit_sha1 = source_git_commit_sha1
        self.target_branch = target_branch
        self.target_git_repository = target_git_repository
        self.target_git_path = target_git_path
        self.target_git_commit_sha1 = target_git_commit_sha1
        self.prerequisite_branch = prerequisite_branch
        self.prerequisite_git_repository = prerequisite_git_repository
        self.prerequisite_git_path = prerequisite_git_path
        self.prerequisite_git_commit_sha1 = prerequisite_git_commit_sha1
        self.description = description
        self.whiteboard = whiteboard
        self.queue_status = queue_status
        self.commit_message = commit_message
        self.date_created = date_created
        self.date_review_requested = date_review_requested

    @property
    def bugs(self):
        from lp.bugs.model.bug import Bug

        if self.source_branch is not None:
            # For Bazaar, we currently only store bug/branch links.
            bugs = self.source_branch.linked_bugs
        else:
            bug_ids = [
                int(id)
                for _, id in getUtility(IXRefSet).findFrom(
                    ("merge_proposal", str(self.id)), types=["bug"]
                )
            ]
            bugs = load(Bug, bug_ids)
        return list(sorted(bugs, key=attrgetter("id")))

    def getRelatedBugTasks(self, user):
        """Bug tasks which are linked to the source but not the target.

        Implies that these would be fixed, in the target, by the merge.
        """
        if self.source_branch is not None:
            source_tasks = self.source_branch.getLinkedBugTasks(user)
            target_tasks = self.target_branch.getLinkedBugTasks(user)
            return [
                bugtask
                for bugtask in source_tasks
                if bugtask not in target_tasks
            ]
        else:
            params = BugTaskSearchParams(
                user=user, linked_merge_proposals=self.id
            )
            tasks = shortlist(getUtility(IBugTaskSet).search(params), 1000)
            return filter_bugtasks_by_context(
                self.source_git_repository.target, tasks
            )

    def createBugLink(self, bug, props=None):
        """See `BugLinkTargetMixin`."""
        if props is None:
            props = {}
        # XXX cjwatson 2016-06-11: Should set creator.
        getUtility(IXRefSet).create(
            {("merge_proposal", str(self.id)): {("bug", str(bug.id)): props}}
        )

    def deleteBugLink(self, bug):
        """See `BugLinkTargetMixin`."""
        getUtility(IXRefSet).delete(
            {("merge_proposal", str(self.id)): [("bug", str(bug.id))]}
        )

    def linkBug(self, bug, user=None, check_permissions=True, props=None):
        """See `BugLinkTargetMixin`."""
        if self.source_branch is not None:
            # For Bazaar, we currently only store bug/branch links.
            return self.source_branch.linkBug(bug, user)
        else:
            # Otherwise, link the bug to the merge proposal directly.
            return super().linkBug(
                bug,
                user=user,
                check_permissions=check_permissions,
                props=props,
            )

    def unlinkBug(self, bug, user=None, check_permissions=True):
        """See `BugLinkTargetMixin`."""
        if self.source_branch is not None:
            # For Bazaar, we currently only store bug/branch links.
            # XXX cjwatson 2016-06-11: This may behave strangely in some
            # cases: if a branch is the source for multiple merge proposals,
            # then unlinking a bug from one will unlink them all.  Fixing
            # this would require a complicated data migration.
            return self.source_branch.unlinkBug(bug, user)
        else:
            # Otherwise, unlink the bug from the merge proposal directly.
            return super().unlinkBug(
                bug, user=user, check_permissions=check_permissions
            )

    def _reportTooManyRelatedBugs(self):
        properties = [
            (
                "error-explanation",
                (
                    "Number of related bugs exceeds limit %d."
                    % config.codehosting.related_bugs_from_source_limit
                ),
            ),
            ("source", self.merge_source.identity),
            ("target", self.merge_target.identity),
        ]
        if self.merge_prerequisite is not None:
            properties.append(
                ("prerequisite", self.merge_prerequisite.identity)
            )
        request = ScriptRequest(properties)
        getUtility(IErrorReportingUtility).raising(sys.exc_info(), request)

    def _fetchRelatedBugIDsFromSource(self):
        """Fetch related bug IDs from the source branch."""
        from lp.bugs.model.bug import Bug

        # Only currently used for Git.
        assert self.source_git_ref is not None
        # XXX cjwatson 2016-06-11: This may return too many bugs in the case
        # where a prerequisite branch fixes a bug which is not fixed by
        # further commits on the source branch.  To fix this, we need
        # turnip's log API to be able to take multiple stop parameters.
        commits = self.getUnlandedSourceBranchRevisions()
        bug_ids = set()
        limit = config.codehosting.related_bugs_from_source_limit
        try:
            for commit in commits:
                if "commit_message" in commit:
                    for match in re_lp_closes.finditer(
                        commit["commit_message"]
                    ):
                        for bug_num in re_bug_numbers.findall(match.group(0)):
                            bug_id = int(bug_num)
                            if bug_id not in bug_ids and len(bug_ids) == limit:
                                raise TooManyRelatedBugs()
                            bug_ids.add(bug_id)
        except TooManyRelatedBugs:
            self._reportTooManyRelatedBugs()
        # Only return bug IDs that exist.
        return set(IStore(Bug).find(Bug.id, Bug.id.is_in(bug_ids)))

    def updateRelatedBugsFromSource(self):
        """See `IBranchMergeProposal`."""
        from lp.bugs.model.bug import Bug

        # Only currently used for Git.
        assert self.source_git_ref is not None
        current_bug_ids_from_source = {
            int(id): (props["metadata"] or {}).get("from_source", False)
            for (_, id), props in getUtility(IXRefSet)
            .findFrom(("merge_proposal", str(self.id)), types=["bug"])
            .items()
        }
        current_bug_ids = set(current_bug_ids_from_source)
        new_bug_ids = self._fetchRelatedBugIDsFromSource()
        # Only remove links marked as originating in the source branch.
        remove_bugs = load(
            Bug,
            {
                bug_id
                for bug_id in current_bug_ids - new_bug_ids
                if current_bug_ids_from_source[bug_id]
            },
        )
        add_bugs = load(Bug, new_bug_ids - current_bug_ids)
        if remove_bugs or add_bugs:
            janitor = getUtility(ILaunchpadCelebrities).janitor
            for bug in remove_bugs:
                self.unlinkBug(bug, user=janitor, check_permissions=False)
            # XXX cjwatson 2016-06-11: We could perhaps set creator and
            # date_created based on commit information, but then we'd have
            # to work out what to do in the case of multiple commits
            # referring to the same bug, updating properties if more such
            # commits arrive later, etc.  This is simple and does the job
            # for now.
            for bug in add_bugs:
                self.linkBug(
                    bug,
                    user=janitor,
                    check_permissions=False,
                    props={"metadata": {"from_source": True}},
                )

    @property
    def address(self):
        return "mp+%d@%s" % (self.id, config.launchpad.code_domain)

    superseded_by_id = Int(name="superseded_by", allow_none=True, default=None)
    superseded_by = Reference(superseded_by_id, "BranchMergeProposal.id")

    _supersedes = Reference("id", "superseded_by_id", on_remote=True)

    @cachedproperty
    def supersedes(self):
        return self._supersedes

    date_created = DateTime(
        allow_none=False, default=DEFAULT, tzinfo=timezone.utc
    )
    date_review_requested = DateTime(
        allow_none=True, default=None, tzinfo=timezone.utc
    )
    date_reviewed = DateTime(
        allow_none=True, default=None, tzinfo=timezone.utc
    )

    @property
    def target(self):
        """See `IHasBranchTarget`."""
        if self.source_branch is not None:
            return self.source_branch.target
        else:
            # XXX cjwatson 2015-04-12: This is not an IBranchTarget for Git,
            # although it has similar semantics.
            return self.source_git_repository.namespace

    root_message_id = Unicode(default=None)

    @property
    def title(self):
        """See `IBranchMergeProposal`."""
        return "[Merge] %(source)s into %(target)s" % {
            "source": self.merge_source.identity,
            "target": self.merge_target.identity,
        }

    @property
    def all_comments(self):
        """See `IBranchMergeProposal`."""
        return IStore(CodeReviewComment).find(
            CodeReviewComment, branch_merge_proposal=self
        )

    def getComment(self, id):
        """See `IBranchMergeProposal`."""
        comment = IStore(CodeReviewComment).get(CodeReviewComment, id)
        if comment is None:
            raise NotFoundError(id)
        if comment.branch_merge_proposal != self:
            raise WrongBranchMergeProposal
        return comment

    def userCanSetCommentVisibility(self, user):
        """See `IBranchMergeProposal`."""
        if user is None:
            return False
        roles = IPersonRoles(user)
        return roles.in_admin or roles.in_registry_experts

    def setCommentVisibility(self, user, comment_number, visible):
        """See `IBranchMergeProposal`."""
        comment = IStore(CodeReviewComment).get(
            CodeReviewComment, comment_number
        )
        if comment is None:
            raise NotFoundError(comment_number)
        if comment.branch_merge_proposal != self:
            raise WrongBranchMergeProposal
        if not comment.userCanSetCommentVisibility(user):
            raise Unauthorized(
                "User %s cannot hide or show code review comments."
                % (user.name if user is not None else "<anonymous>")
            )
        comment.message.setVisible(visible)

    def getVoteReference(self, id):
        """See `IBranchMergeProposal`.

        This function can raise WrongBranchMergeProposal."""
        vote = IStore(CodeReviewVoteReference).get(CodeReviewVoteReference, id)
        if vote is None:
            raise NotFoundError(id)
        if vote.branch_merge_proposal != self:
            raise WrongBranchMergeProposal
        return vote

    @property
    def _preview_diffs(self):
        return (
            Store.of(self)
            .find(PreviewDiff, PreviewDiff.branch_merge_proposal_id == self.id)
            .order_by(PreviewDiff.date_created)
        )

    @cachedproperty
    def preview_diffs(self):
        return list(self._preview_diffs)

    @cachedproperty
    def preview_diff(self):
        return self._preview_diffs.last()

    @cachedproperty
    def votes(self):
        return list(
            Store.of(self).find(
                CodeReviewVoteReference,
                CodeReviewVoteReference.branch_merge_proposal == self,
            )
        )

    def getNotificationRecipients(self, min_level):
        """See IBranchMergeProposal.getNotificationRecipients"""
        recipients = {}
        branch_identity_cache = {
            self.merge_source: self.merge_source.identity,
            self.merge_target: self.merge_target.identity,
        }
        branches = [self.merge_source, self.merge_target]
        if self.merge_prerequisite is not None:
            branches.append(self.merge_prerequisite)
        for branch in branches:
            branch_recipients = branch.getNotificationRecipients()
            for recipient in branch_recipients:
                # If the recipient cannot see either of the branches, skip
                # them.
                if not self.merge_source.visibleByUser(
                    recipient
                ) or not self.merge_target.visibleByUser(recipient):
                    continue
                subscription, rationale = branch_recipients.getReason(
                    recipient
                )
                if subscription.review_level < min_level:
                    continue
                recipients[recipient] = RecipientReason.forBranchSubscriber(
                    subscription,
                    branch,
                    recipient,
                    rationale,
                    self,
                    branch_identity_cache=branch_identity_cache,
                )
        # Add in all the individuals that have been asked for a review,
        # or who have reviewed.  These people get added to the recipients
        # with the rationale of "Reviewer".
        # Don't add a team reviewer to the recipients as they are only going
        # to get emails normally if they are subscribed to one of the
        # branches, and if they are subscribed, they'll be getting this email
        # already.
        for review in self.votes:
            reviewer = review.reviewer
            pending = review.comment is None
            recipients[reviewer] = RecipientReason.forReviewer(
                self,
                pending,
                reviewer,
                branch_identity_cache=branch_identity_cache,
            )
        # If the registrant of the proposal is getting emails, update the
        # rationale to say that they registered it.  Don't however send them
        # emails if they aren't asking for any.
        if self.registrant in recipients:
            recipients[self.registrant] = RecipientReason.forRegistrant(
                self, branch_identity_cache=branch_identity_cache
            )
        # If the owner of the source branch is getting emails, override the
        # rationale to say they are the owner of the source branch.
        source_owner = self.merge_source.owner
        if source_owner in recipients:
            reason = RecipientReason.forSourceOwner(
                self, branch_identity_cache=branch_identity_cache
            )
            if reason is not None:
                recipients[source_owner] = reason

        return recipients

    def isValidTransition(self, next_state, user=None):
        """See `IBranchMergeProposal`."""
        return is_valid_transition(self, self.queue_status, next_state, user)

    def _transitionToState(self, next_state, user=None):
        """Update the queue_status of the proposal.

        Raise an error if the proposal is in a final state.
        """
        if not self.isValidTransition(next_state, user):
            raise BadStateTransition(
                "Invalid state transition for merge proposal: %s -> %s"
                % (self.queue_status.title, next_state.title)
            )
        # Transition to the same state occur in two particular
        # situations:
        #  * stale posts
        #  * approving a later revision
        # In both these cases, there is no real reason to disallow
        # transitioning to the same state.
        self.queue_status = next_state

    def setStatus(self, status, user=None, revision_id=None):
        """See `IBranchMergeProposal`."""
        # XXX - rockstar - 9 Oct 2008 - jml suggested in a review that this
        # would be better as a dict mapping.
        # See bug #281060.
        if status == BranchMergeProposalStatus.WORK_IN_PROGRESS:
            self.setAsWorkInProgress()
        elif status == BranchMergeProposalStatus.NEEDS_REVIEW:
            self.requestReview()
        elif status == BranchMergeProposalStatus.CODE_APPROVED:
            self.approveBranch(user, revision_id)
        elif status == BranchMergeProposalStatus.REJECTED:
            self.rejectBranch(user, revision_id)
        elif status == BranchMergeProposalStatus.MERGED:
            self.markAsMerged(
                merge_reporter=user, merged_revision_id=revision_id
            )
        else:
            raise AssertionError("Unexpected queue status: %s" % status)

    def setAsWorkInProgress(self):
        """See `IBranchMergeProposal`."""
        self._transitionToState(BranchMergeProposalStatus.WORK_IN_PROGRESS)
        self._mark_unreviewed()

    def _mark_unreviewed(self):
        """Clear metadata about a previous review."""
        self.reviewer = None
        self.date_reviewed = None
        self.reviewed_revision_id = None

    def requestReview(self, _date_requested=None):
        """See `IBranchMergeProposal`.

        :param _date_requested: used only for testing purposes to override
            the normal UTC_NOW for when the review was requested.
        """
        # Don't reset the date_review_requested if we are already in the
        # review state.
        if _date_requested is None:
            _date_requested = UTC_NOW
        # If we are going from work in progress to needs review, then reset
        # the root message id and trigger a job to send out the email.
        if self.queue_status == BranchMergeProposalStatus.WORK_IN_PROGRESS:
            self.root_message_id = None
            notify(BranchMergeProposalNeedsReviewEvent(self))
        if self.queue_status != BranchMergeProposalStatus.NEEDS_REVIEW:
            self._transitionToState(BranchMergeProposalStatus.NEEDS_REVIEW)
            self.date_review_requested = _date_requested
            # Clear out any reviewed values.
            self._mark_unreviewed()

    def isInProgress(self):
        """See `IBranchMergeProposal`."""
        # As long as the source branch has not been merged, rejected
        # or superseded, then it is valid to be merged.
        return self.queue_status not in FINAL_STATES

    def isApproved(self):
        """See `IBranchMergeProposal`."""

        # TODO ines-almeida 2025-05-09: the criteria for a merge proposal to
        # be 'approved' should eventually be defined by the repository owners.
        # For now we enforce that we need at least one approval from a trusted
        # reviewer

        for vote in self.votes:
            if (
                vote.comment
                and vote.comment.vote == CodeReviewVote.APPROVE
                and self.merge_target.isPersonTrustedReviewer(vote.reviewer)
            ):
                return True
        return False

    def CIChecksPassed(self):
        """See `IBranchMergeProposal`."""

        # We don't run CI checks on bazaar repos
        if self.source_branch is not None:
            return True

        reports = self.getStatusReports(self.source_git_commit_sha1)
        if reports.is_empty():
            return True

        return reports.last().result == RevisionStatusResult.SUCCEEDED

    def hasNoConflicts(self):
        """See `IBranchMergeProposal`."""

        if self.preview_diff is None:
            return True

        return (
            self.preview_diff.conflicts is None
            or self.preview_diff.conflicts == ""
        )

    def hasNoPendingPrerequisite(self):
        """See `IBranchMergeProposal`."""

        if self.merge_prerequisite is None:
            return True

        # Bazaar - not implemented
        if self.target_branch:
            return None

        # Check if prerequisite has been merged into the target branch
        # The response returns a list of commits that were merged, meaning
        # that if the list is empty, the merge hasn't occured
        hosting_client = getUtility(IGitHostingClient)
        response = hosting_client.detectMerges(
            self.target_git_repository_id,
            self.target_git_commit_sha1,
            [self.prerequisite_git_commit_sha1],
        )
        return bool(response)

    def diffIsUpToDate(self):
        """See `IBranchMergeProposal`."""

        return self.next_preview_diff_job is None

    def getMergeCriteria(self):
        """See `IBranchMergeProposal`."""
        can_be_merged, can_be_force_merged, criteria = (
            self.checkMergeCriteria()
        )

        return {
            "can_be_merged": can_be_merged,
            "can_be_force_merged": can_be_force_merged,
            "criteria": criteria,
        }

    def checkMergeCriteria(self):
        """See `IBranchMergeProposal`."""

        # TODO ines-almeida 2025-05-05: the mergeability criteria should be
        # to some extent defined by the repository owner. While that
        # capability is not in place, we will set a few hard rules for one
        # to merge a MP from Launchpad.

        # List MP criteria: method and error message
        MERGE_CRITERIA = {
            MergeProposalCriteria.IS_IN_PROGRESS: (
                self.isInProgress,
                "Invalid status for merging",
                False,
            ),
            MergeProposalCriteria.HAS_NO_CONFLICTS: (
                self.hasNoConflicts,
                "Diff contains conflicts",
                True,
            ),
            MergeProposalCriteria.DIFF_IS_UP_TO_DATE: (
                self.diffIsUpToDate,
                "New changes were pushed too recently",
                False,
            ),
            MergeProposalCriteria.HAS_NO_PENDING_PREREQUISITE: (
                self.hasNoPendingPrerequisite,
                "Prerequisite branch not merged",
                False,
            ),
            MergeProposalCriteria.IS_APPROVED: (
                self.isApproved,
                "Proposal has not been approved",
                False,
            ),
            MergeProposalCriteria.CI_CHECKS_PASSED: (
                self.CIChecksPassed,
                "CI checks failed",
                False,
            ),
        }

        can_be_merged = True
        can_be_force_merged = True
        status = {}
        for key, criteria in MERGE_CRITERIA.items():
            check, error, is_required = criteria
            passed = check()

            key = str(key)
            status[key] = {"passed": passed, "is_required": is_required}
            if passed is None:
                status[key]["error"] = "Not implemented"
            elif not passed:
                status[key]["error"] = error
                can_be_merged = False
                if is_required:
                    can_be_force_merged = False

        return can_be_merged, can_be_force_merged, status

    def canIMerge(self, person):
        """See `IBranchMergeProposal`."""
        return self.personCanMerge(person)

    def personCanMerge(self, person):
        """See `IBranchMergeProposal`."""

        if self.source_branch is not None:
            raise NotImplementedError(
                "Merging Bazaar branches is not supported"
            )

        permissions = self.target_git_repository.checkRefPermissions(
            person, [self.target_git_path]
        )
        return GitPermissionType.CAN_PUSH in permissions[self.target_git_path]

    def requestMerge(self, person, commit_message=None, force=False):
        """See `IBranchMergeProposal`."""

        if not getFeatureFlag(PROPOSAL_MERGE_ENABLED_FEATURE_FLAG):
            raise BranchMergeProposalFeatureDisabled(
                "Merge API is currently disabled"
            )

        if self.source_branch is not None:
            raise NotImplementedError(
                "Merging Bazaar branches is not supported"
            )

        if not self.personCanMerge(person):
            raise Unauthorized()

        can_be_merged, can_be_force_merged, criteria = (
            self.checkMergeCriteria()
        )
        if not can_be_merged and not (force and can_be_force_merged):
            failed_checks = [
                item["error"]
                for item in criteria.values()
                if item["passed"] is False
            ]
            raise BranchMergeProposalNotMergeable(
                "Merge proposal cannot be merged in its current state: %s"
                % ", ".join(failed_checks)
            )

        if commit_message is None:
            commit_message = self.commit_message

        hosting_client = getUtility(IGitHostingClient)
        response = hosting_client.requestMerge(
            self.target_git_repository_id,
            self.target_git_ref.name,
            self.target_git_commit_sha1,
            self.source_git_ref.name,
            self.source_git_commit_sha1,
            person,
            commit_message,
            source_repo=self.source_git_repository_id,
        )

        # Expected response when merge is queued:
        #    {"queued": True, "already_merged": False}
        # If the merge had previously been merged, then expected response is:
        #    {"queued": False, "already_merged": True}

        if "queued" not in response:
            raise BranchMergeProposalMergeFailed(
                "Request to merge this proposal failed."
            )

        if response["queued"]:
            return "Merge successfully queued"

        if not response["already_merged"]:
            raise BranchMergeProposalMergeFailed(
                "Merge proposal could not be queued for merging."
            )

        # If we reach here, then the merge had already been previously merged
        # successfully, but Launchpad didn't mark the proposal as merged.
        # Force a rescan in that case.
        self.target_git_repository.rescan()
        return "Proposal already merged, waiting for rescan"

    def merge(self, person, commit_message=None, force=False):
        """See `IBranchMergeProposal`.
        TODO ines-almeida 2025-07-11: we want to remove this method in favor
        of the requestMerge() one. Keeping it for now while we migrate.
        """

        if not getFeatureFlag(PROPOSAL_MERGE_ENABLED_FEATURE_FLAG):
            raise BranchMergeProposalFeatureDisabled(
                "Merge API is currently disabled"
            )

        if self.source_branch is not None:
            raise NotImplementedError(
                "Merging Bazaar branches is not supported"
            )

        if not self.personCanMerge(person):
            raise Unauthorized()

        can_be_merged, can_be_force_merged, criteria = (
            self.checkMergeCriteria()
        )
        if not can_be_merged and not (force and can_be_force_merged):
            failed_checks = [
                item["error"]
                for item in criteria.values()
                if item["passed"] is False
            ]
            raise BranchMergeProposalNotMergeable(
                "Merge proposal cannot be merged in its current state: %s"
                % ", ".join(failed_checks)
            )

        if commit_message is None:
            commit_message = self.commit_message

        hosting_client = getUtility(IGitHostingClient)
        response = hosting_client.merge(
            self.target_git_repository_id,
            self.target_git_ref.name,
            self.target_git_commit_sha1,
            self.source_git_ref.name,
            self.source_git_commit_sha1,
            person,
            commit_message,
            source_repo=self.source_git_repository_id,
        )

        # It shouldn't be possible for the `merge_commit` to not exist in the
        # response given the request didn't raise, but we do a check anyways.
        if "merge_commit" not in response:
            raise BranchMergeProposalMergeFailed(
                "Failed to merge the proposal."
            )

        # Schedule a rescan (similarly to what happens when a push is done to
        # turnip). This will prevent Launchpad from getting out of date with
        # turnip's repository
        self.target_git_repository.rescan()

        merge_commit = response["merge_commit"]
        previously_merged = response.get("previously_merged", False)

        if previously_merged:
            merge_type = (
                self.merge_type if self.merge_type else MergeType.UNKNOWN
            )

            # If there was already a merge commit (odd case that someone
            # changes the status of an old proposal from 'merged' back to
            # another state and then tries merging again) don't overwrite it
            if self.merged_revision_id:
                merge_commit = self.merged_revision_id
        else:
            merge_type = MergeType.REGULAR_MERGE
            self.commit_message = commit_message

        self.markAsMerged(
            merge_reporter=person,
            merged_revision_id=merge_commit,
            date_merged=UTC_NOW,
            merge_type=merge_type,
        )

    def _reviewProposal(
        self, reviewer, next_state, revision_id, _date_reviewed=None
    ):
        """Set the proposal to next_state."""
        # Check the reviewer can review the code for the target branch.
        if not self.merge_target.isPersonTrustedReviewer(reviewer):
            raise UserNotBranchReviewer
        # Check the current state of the proposal.
        self._transitionToState(next_state, reviewer)
        # Record the reviewer
        self.reviewer = reviewer
        if _date_reviewed is None:
            _date_reviewed = UTC_NOW
        self.date_reviewed = _date_reviewed
        # Record the reviewed revision id
        self.reviewed_revision_id = revision_id

    def approveBranch(self, reviewer, revision_id, _date_reviewed=None):
        """See `IBranchMergeProposal`."""
        self._reviewProposal(
            reviewer,
            BranchMergeProposalStatus.CODE_APPROVED,
            revision_id,
            _date_reviewed,
        )

    def rejectBranch(self, reviewer, revision_id, _date_reviewed=None):
        """See `IBranchMergeProposal`."""
        self._reviewProposal(
            reviewer,
            BranchMergeProposalStatus.REJECTED,
            revision_id,
            _date_reviewed,
        )

    def markAsMerged(
        self,
        merged_revno=None,
        merged_revision_id=None,
        date_merged=None,
        merge_reporter=None,
        merge_type=MergeType.UNKNOWN,
    ):
        """See `IBranchMergeProposal`."""
        old_state = self.queue_status
        self._transitionToState(
            BranchMergeProposalStatus.MERGED, merge_reporter
        )
        self.merged_revno = merged_revno
        self.merged_revision_id = merged_revision_id
        self.merge_reporter = merge_reporter
        self.merge_type = merge_type

        # The reviewer of a merged proposal is assumed to have approved, if
        # they rejected it remove the review metadata to avoid confusion.
        if old_state == BranchMergeProposalStatus.REJECTED:
            self._mark_unreviewed()

        if self.target_branch is not None and merged_revno is not None:
            branch_revision = (
                Store.of(self)
                .find(
                    BranchRevision,
                    BranchRevision.branch == self.target_branch,
                    BranchRevision.sequence == merged_revno,
                )
                .one()
            )
            if branch_revision is not None:
                date_merged = branch_revision.revision.revision_date
        # XXX cjwatson 2015-04-12: Handle the Git case.

        if date_merged is None:
            date_merged = UTC_NOW
        self.date_merged = date_merged

    def resubmit(
        self,
        registrant,
        merge_source=None,
        merge_target=None,
        merge_prerequisite=DEFAULT,
        commit_message=None,
        description=None,
        break_link=False,
    ):
        """See `IBranchMergeProposal`."""
        if merge_source is None:
            merge_source = self.merge_source
        if merge_target is None:
            merge_target = self.merge_target
        # DEFAULT instead of None, because None is a valid value.
        proposals = BranchMergeProposalGetter.activeProposalsForBranches(
            merge_source, merge_target
        )
        for proposal in proposals:
            if proposal is not self:
                raise BranchMergeProposalExists(proposal)
        if merge_prerequisite is DEFAULT:
            merge_prerequisite = self.merge_prerequisite
        if commit_message is None:
            commit_message = self.commit_message
        if description is None:
            description = self.description
        # You can transition from REJECTED to SUPERSEDED, but
        # not from MERGED or SUPERSEDED.
        self._transitionToState(
            BranchMergeProposalStatus.SUPERSEDED, registrant
        )
        # This flush is needed as the add landing target does a database
        # query to identify if there are any active proposals with the same
        # source and target branches.
        Store.of(self).flush()
        review_requests = list(
            {(vote.reviewer, vote.review_type) for vote in self.votes}
        )
        proposal = merge_source.addLandingTarget(
            registrant=registrant,
            merge_target=merge_target,
            merge_prerequisite=merge_prerequisite,
            commit_message=commit_message,
            description=description,
            needs_review=True,
            review_requests=review_requests,
        )
        if not break_link:
            self.superseded_by = proposal
        # This flush is needed to ensure that the transitive properties of
        # supersedes and superseded_by are visible to the old and the new
        # proposal.
        Store.of(self).flush()
        return proposal

    def _normalizeReviewType(self, review_type):
        """Normalse the review type.

        If review_type is None, it stays None.  Otherwise the review_type is
        converted to lower case, and if the string is empty is gets changed to
        None.
        """
        if review_type is not None:
            review_type = review_type.strip()
            if review_type == "":
                review_type = None
            else:
                review_type = review_type.lower()
        return review_type

    def _subscribeUserToStackedBranch(
        self, branch, user, checked_branches=None
    ):
        """Subscribe the user to the branch and those it is stacked on."""
        if checked_branches is None:
            checked_branches = []
        branch.subscribe(
            user,
            BranchSubscriptionNotificationLevel.NOEMAIL,
            BranchSubscriptionDiffSize.NODIFF,
            CodeReviewNotificationLevel.FULL,
            user,
        )
        if IBranch.providedBy(branch) and branch.stacked_on is not None:
            checked_branches.append(branch)
            if branch.stacked_on not in checked_branches:
                self._subscribeUserToStackedBranch(
                    branch.stacked_on, user, checked_branches
                )

    def _acceptable_to_give_visibility(self, branch, reviewer):
        # If the branch is private, only exclusive teams can be subscribed to
        # prevent leaks.
        if (
            branch.information_type in PRIVATE_INFORMATION_TYPES
            and reviewer.is_team
            and reviewer.anyone_can_join()
        ):
            return False
        return True

    def _ensureAssociatedBranchesVisibleToReviewer(self, reviewer):
        """A reviewer must be able to see the source and target branches.

        Currently, we ensure the required visibility by subscribing the user
        to the branch and those on which it is stacked. We do not subscribe
        the reviewer if the branch is private and the reviewer is an open
        team.
        """
        source = self.merge_source
        if not source.visibleByUser(
            reviewer
        ) and self._acceptable_to_give_visibility(source, reviewer):
            self._subscribeUserToStackedBranch(source, reviewer)
        target = self.merge_target
        if not target.visibleByUser(
            reviewer
        ) and self._acceptable_to_give_visibility(source, reviewer):
            self._subscribeUserToStackedBranch(target, reviewer)

    def nominateReviewer(
        self,
        reviewer,
        registrant,
        review_type=None,
        _date_created=DEFAULT,
        _notify_listeners=True,
    ):
        """See `IBranchMergeProposal`."""
        # Return the existing vote reference or create a new one.
        # Lower case the review type.
        review_type = self._normalizeReviewType(review_type)
        vote_reference = self.getUsersVoteReference(reviewer, review_type)
        # If there is no existing review for the reviewer, then create a new
        # one.  If the reviewer is a team, then we don't care if there is
        # already an existing pending review, as some projects expect multiple
        # reviews from a team.
        if vote_reference is None or reviewer.is_team:
            vote_reference = CodeReviewVoteReference(
                branch_merge_proposal=self,
                registrant=registrant,
                reviewer=reviewer,
                date_created=_date_created,
            )
            self._ensureAssociatedBranchesVisibleToReviewer(reviewer)
        vote_reference.review_type = review_type
        Store.of(vote_reference).flush()
        if _notify_listeners:
            notify(ReviewerNominatedEvent(vote_reference))
        return vote_reference

    def deleteProposal(self):
        """See `IBranchMergeProposal`."""
        notify(ObjectDeletedEvent(self))
        # Delete this proposal, but keep the superseded chain linked.
        if self.supersedes is not None:
            self.supersedes.superseded_by = self.superseded_by
        # Delete the related CodeReviewVoteReferences.
        for vote in self.votes:
            vote.destroySelf()
        # Delete published and draft inline comments related to this MP.
        getUtility(ICodeReviewInlineCommentSet).removeFromDiffs(
            [pd.id for pd in self._preview_diffs]
        )
        # Delete the related CodeReviewComments.
        for comment in self.all_comments:
            comment.destroySelf()
        # Delete all jobs referring to the BranchMergeProposal, whether
        # or not they have completed.
        from lp.code.model.branchmergeproposaljob import BranchMergeProposalJob

        IStore(BranchMergeProposalJob).find(
            BranchMergeProposalJob, branch_merge_proposal=self
        ).remove()
        self._preview_diffs.remove()
        Store.of(self).remove(self)

    def getUnlandedSourceBranchRevisions(self):
        """See `IBranchMergeProposal`."""
        if self.source_branch is not None:
            store = Store.of(self)
            source = SQL(
                """
                source AS (
                    SELECT
                        BranchRevision.branch, BranchRevision.revision,
                        Branchrevision.sequence
                    FROM BranchRevision
                    WHERE
                        BranchRevision.branch = %s
                        AND BranchRevision.sequence IS NOT NULL
                    ORDER BY
                        BranchRevision.branch DESC,
                        BranchRevision.sequence DESC
                    LIMIT 10)"""
                % self.source_branch.id
            )
            where = SQL(
                """
                BranchRevision.revision NOT IN (
                    SELECT revision
                    FROM BranchRevision AS target
                    WHERE
                        target.branch = %s
                        AND BranchRevision.revision = target.revision)"""
                % self.target_branch.id
            )
            using = SQL("""source AS BranchRevision""")
            revisions = (
                store.with_(source).using(using).find(BranchRevision, where)
            )
            return list(
                revisions.order_by(Desc(BranchRevision.sequence)).config(
                    limit=10
                )
            )
        else:
            return self.source_git_ref.getCommits(
                self.source_git_commit_sha1,
                limit=10,
                stop=self.target_git_commit_sha1,
                union_repository=self.target_git_repository,
            )

    def createComment(
        self,
        owner,
        subject=None,
        content=None,
        vote=None,
        review_type=None,
        parent=None,
        # The date the message was created.  Provided only for testing
        # purposes, as it can break BranchMergeProposal.root_message.
        _date_created=DEFAULT,
        previewdiff_id=None,
        inline_comments=None,
        _notify_listeners=True,
    ):
        """See `IBranchMergeProposal`."""
        review_type = self._normalizeReviewType(review_type)
        assert owner is not None, "Merge proposal messages need a sender"
        parent_message = None
        if parent is not None:
            assert (
                parent.branch_merge_proposal == self
            ), "Replies must use the same merge proposal as their parent"
            parent_message = parent.message
        if not subject:
            # Get the subject from the parent if there is one, or use a nice
            # default.
            if parent is None:
                subject = self.title
            else:
                subject = parent.message.subject
            if not subject.startswith("Re: "):
                subject = "Re: " + subject

        msgid = make_msgid("codereview")
        message = Message(
            parent=parent_message,
            owner=owner,
            rfc822msgid=msgid,
            subject=subject,
            datecreated=_date_created,
        )
        if content is not None:
            MessageChunk(message=message, content=content, sequence=1)
        comment = self.createCommentFromMessage(
            message,
            vote,
            review_type,
            original_email=None,
            _notify_listeners=_notify_listeners,
            _validate=False,
        )

        if inline_comments:
            assert previewdiff_id is not None, (
                "Inline comments must be associated with a " "previewdiff ID."
            )
            previewdiff = self.getPreviewDiff(previewdiff_id)
            getUtility(ICodeReviewInlineCommentSet).ensureDraft(
                previewdiff, owner, inline_comments
            )
            getUtility(ICodeReviewInlineCommentSet).publishDraft(
                previewdiff, owner, comment
            )

        return comment

    def getUsersVoteReference(self, user, review_type=None):
        """Get the existing vote reference for the given user."""
        # Lower case the review type.
        review_type = self._normalizeReviewType(review_type)
        if user is None:
            return None
        if user.is_team:
            query = And(
                CodeReviewVoteReference.reviewer == user,
                CodeReviewVoteReference.review_type == review_type,
            )
        else:
            query = CodeReviewVoteReference.reviewer == user
        return (
            Store.of(self)
            .find(
                CodeReviewVoteReference,
                CodeReviewVoteReference.branch_merge_proposal == self,
                query,
            )
            .order_by(CodeReviewVoteReference.date_created)
            .first()
        )

    def _getTeamVoteReference(self, user, review_type):
        """Get a vote reference where the user is in the review team.

        Only return those reviews where the review_type matches.
        """
        refs = Store.of(self).find(
            CodeReviewVoteReference,
            CodeReviewVoteReference.branch_merge_proposal == self,
            CodeReviewVoteReference.review_type == review_type,
            CodeReviewVoteReference.comment == None,
        )
        for ref in refs.order_by(CodeReviewVoteReference.date_created):
            if user.inTeam(ref.reviewer):
                return ref
        return None

    def _getVoteReference(self, user, review_type):
        """Get the vote reference for the user.

        The returned vote reference will either:
          * the existing vote reference for the user
          * a vote reference of the same type that has been requested of a
            team that the user is a member of
          * a new vote reference for the user
        """
        # Firstly look for a vote reference for the user.
        ref = self.getUsersVoteReference(user)
        if ref is not None:
            return ref
        # Get all the unclaimed CodeReviewVoteReferences with the review_type
        # specified.
        team_ref = self._getTeamVoteReference(user, review_type)
        if team_ref is not None:
            return team_ref
        # If the review_type is not None, check to see if there is an
        # outstanding team review requested with no specified type.
        if review_type is not None:
            team_ref = self._getTeamVoteReference(user, None)
            if team_ref is not None:
                return team_ref
        # Create a new reference.
        vote_reference = CodeReviewVoteReference(
            branch_merge_proposal=self,
            registrant=user,
            reviewer=user,
            review_type=review_type,
        )
        Store.of(vote_reference).flush()
        return vote_reference

    def createCommentFromMessage(
        self,
        message,
        vote,
        review_type,
        original_email,
        _notify_listeners=True,
        _validate=True,
    ):
        """See `IBranchMergeProposal`."""
        if _validate:
            validate_message(original_email)
        review_type = self._normalizeReviewType(review_type)
        code_review_message = CodeReviewComment(
            branch_merge_proposal=self,
            message=message,
            vote=vote,
            vote_tag=review_type,
        )
        # Get the appropriate CodeReviewVoteReference for the reviewer.
        # If there isn't one, then create one, otherwise set the comment
        # reference.
        if vote is not None:
            vote_reference = self._getVoteReference(message.owner, review_type)
            # Just set the reviewer and review type again on the off chance
            # that the user has edited the review_type or claimed a team
            # review.
            vote_reference.reviewer = message.owner
            vote_reference.review_type = review_type
            vote_reference.comment = code_review_message
        Store.of(code_review_message).flush()
        if _notify_listeners:
            notify(ObjectCreatedEvent(code_review_message))
        return code_review_message

    def getInlineComments(self, previewdiff_id):
        """See `IBranchMergeProposal`."""
        previewdiff = self.getPreviewDiff(previewdiff_id)
        return getUtility(ICodeReviewInlineCommentSet).getPublished(
            previewdiff
        )

    def getDraftInlineComments(self, previewdiff_id, person):
        """See `IBranchMergeProposal`."""
        previewdiff = self.getPreviewDiff(previewdiff_id)
        return getUtility(ICodeReviewInlineCommentSet).getDraft(
            previewdiff, person
        )

    def getPreviewDiff(self, id):
        """See `IBranchMergeProposal`."""
        previewdiff = IStore(self).get(PreviewDiff, id)
        if previewdiff is None:
            raise DiffNotFound
        if previewdiff.branch_merge_proposal != self:
            raise WrongBranchMergeProposal
        return previewdiff

    def saveDraftInlineComment(self, previewdiff_id, person, comments):
        """See `IBranchMergeProposal`."""
        previewdiff = self.getPreviewDiff(previewdiff_id)
        getUtility(ICodeReviewInlineCommentSet).ensureDraft(
            previewdiff, person, comments
        )

    def updatePreviewDiff(
        self,
        diff_content,
        source_revision_id,
        target_revision_id,
        prerequisite_revision_id=None,
        conflicts=None,
    ):
        """See `IBranchMergeProposal`."""
        return PreviewDiff.create(
            self,
            diff_content,
            source_revision_id,
            target_revision_id,
            prerequisite_revision_id,
            conflicts,
        )

    def getIncrementalDiffRanges(self):
        groups = self.getRevisionsSinceReviewStart()
        return [
            (group[0].revision.getLefthandParent(), group[-1].revision)
            for group in groups
        ]

    def generateIncrementalDiff(self, old_revision, new_revision, diff=None):
        """See `IBranchMergeProposal`."""
        if self.source_branch is None:
            # XXX cjwatson 2015-04-16: Implement for Git.
            return
        if diff is None:
            source_branch = self.source_branch.getBzrBranch()
            ignore_branches = [self.target_branch.getBzrBranch()]
            if self.prerequisite_branch is not None:
                ignore_branches.append(self.prerequisite_branch.getBzrBranch())
            diff = Diff.generateIncrementalDiff(
                old_revision, new_revision, source_branch, ignore_branches
            )
        incremental_diff = IncrementalDiff()
        incremental_diff.diff = diff
        incremental_diff.branch_merge_proposal = self
        incremental_diff.old_revision = old_revision
        incremental_diff.new_revision = new_revision
        IPrimaryStore(IncrementalDiff).add(incremental_diff)
        return incremental_diff

    def getIncrementalDiffs(self, revision_list):
        """See `IBranchMergeProposal`."""
        diffs = Store.of(self).find(
            IncrementalDiff,
            IncrementalDiff.branch_merge_proposal_id == self.id,
        )
        diff_dict = {
            (diff.old_revision, diff.new_revision): diff for diff in diffs
        }
        return [diff_dict.get(revisions) for revisions in revision_list]

    def scheduleDiffUpdates(self, return_jobs=True):
        """See `IBranchMergeProposal`."""
        from lp.code.model.branchmergeproposaljob import (
            GenerateIncrementalDiffJob,
            UpdatePreviewDiffJob,
        )

        jobs = []
        if (
            self.target_branch is None
            or self.target_branch.last_scanned_id is not None
        ):
            jobs.append(UpdatePreviewDiffJob.create(self))
            if self.target_branch is not None:
                for old, new in self.getMissingIncrementalDiffs():
                    jobs.append(
                        GenerateIncrementalDiffJob.create(
                            self, old.revision_id, new.revision_id
                        )
                    )
        if return_jobs:
            return jobs

    def getLatestDiffUpdateJob(self):
        """See `IBranchMergeProposal`."""
        from lp.code.model.branchmergeproposaljob import (
            BranchMergeProposalJob,
            UpdatePreviewDiffJob,
        )

        diff_type = UpdatePreviewDiffJob.class_job_type
        latest_preview = (
            IStore(BranchMergeProposalJob)
            .find(
                BranchMergeProposalJob,
                BranchMergeProposalJob.branch_merge_proposal == self,
                BranchMergeProposalJob.job_type == diff_type,
                BranchMergeProposalJob.job == Job.id,
            )
            .order_by(Desc(Job.date_finished))
            .first()
        )
        if latest_preview is not None:
            latest_preview = latest_preview.makeDerived()
        return latest_preview

    @property
    def revision_end_date(self):
        """The cutoff date for showing revisions.

        If the proposal has been merged, then we stop at the merged date. If
        it is rejected, we stop at the reviewed date. For superseded
        proposals, it should ideally use the non-existent date_last_modified,
        but could use the last comment date.
        """
        status = self.queue_status
        if status == BranchMergeProposalStatus.MERGED:
            return self.date_merged
        if status == BranchMergeProposalStatus.REJECTED:
            return self.date_reviewed
        # Otherwise return None representing an open end date.
        return None

    def _getNewerRevisions(self):
        start_date = self.date_review_requested
        if start_date is None:
            start_date = self.date_created
        if self.source_branch is not None:
            revisions = self.source_branch.getMainlineBranchRevisions(
                start_date, self.revision_end_date, oldest_first=True
            )
            return [
                (
                    (revision.date_created, branch_revision.sequence),
                    branch_revision,
                )
                for branch_revision, revision in revisions
            ]
        else:
            commits = reversed(
                self.source_git_ref.getCommits(
                    self.source_git_commit_sha1,
                    stop=self.target_git_commit_sha1,
                    union_repository=self.target_git_repository,
                    start_date=start_date,
                    end_date=self.revision_end_date,
                )
            )
            return [
                ((commit["author_date"], count), commit)
                for count, commit in enumerate(commits)
            ]

    def getRevisionsSinceReviewStart(self):
        """Get the grouped revisions since the review started."""
        all_comments = list(self.all_comments)
        entries = [
            ((comment.date_created, i - len(all_comments)), comment)
            for i, comment in enumerate(all_comments)
        ]
        entries.extend(self._getNewerRevisions())
        entries.sort()
        current_group = []
        for _, entry in entries:
            if ICodeReviewComment.providedBy(entry):
                if current_group != []:
                    yield current_group
                    current_group = []
            else:
                current_group.append(entry)
        if current_group != []:
            yield current_group

    def getMissingIncrementalDiffs(self):
        ranges = self.getIncrementalDiffRanges()
        diffs = self.getIncrementalDiffs(ranges)
        return [range_ for range_, diff in zip(ranges, diffs) if diff is None]

    @staticmethod
    def preloadDataForBMPs(branch_merge_proposals, user, include_votes=True):
        # Utility to load the data related to a list of bmps.
        # Circular imports.
        from lp.code.model.branch import Branch
        from lp.code.model.branchcollection import GenericBranchCollection
        from lp.code.model.gitcollection import GenericGitCollection
        from lp.code.model.gitref import GitRef
        from lp.code.model.gitrepository import GitRepository

        ids = set()
        source_branch_ids = set()
        git_ref_keys = set()
        person_ids = set()
        for mp in branch_merge_proposals:
            ids.add(mp.id)
            if mp.source_branch_id is not None:
                source_branch_ids.add(mp.source_branch_id)
            if mp.source_git_repository_id is not None:
                git_ref_keys.add(
                    (mp.source_git_repository_id, mp.source_git_path)
                )
                git_ref_keys.add(
                    (mp.target_git_repository_id, mp.target_git_path)
                )
                if mp.prerequisite_git_repository_id is not None:
                    git_ref_keys.add(
                        (
                            mp.prerequisite_git_repository_id,
                            mp.prerequisite_git_path,
                        )
                    )
            person_ids.add(mp.registrant_id)
            person_ids.add(mp.merge_reporter_id)
        git_repository_ids = {
            repository_id for repository_id, _ in git_ref_keys
        }

        branches = load_related(
            Branch,
            branch_merge_proposals,
            ("target_branch_id", "prerequisite_branch_id", "source_branch_id"),
        )
        repositories = load_related(
            GitRepository,
            branch_merge_proposals,
            (
                "target_git_repository_id",
                "prerequisite_git_repository_id",
                "source_git_repository_id",
            ),
        )
        load(GitRef, git_ref_keys)
        # The stacked on branches are used to check branch visibility.
        GenericBranchCollection.preloadVisibleStackedOnBranches(branches, user)
        GenericGitCollection.preloadVisibleRepositories(repositories, user)

        if len(branches) == 0 and len(repositories) == 0:
            return

        # Pre-load PreviewDiffs and Diffs.
        preview_diffs = (
            IStore(BranchMergeProposal)
            .find(PreviewDiff, PreviewDiff.branch_merge_proposal_id.is_in(ids))
            .order_by(
                PreviewDiff.branch_merge_proposal_id,
                Desc(PreviewDiff.date_created),
            )
            .config(distinct=[PreviewDiff.branch_merge_proposal_id])
        )
        diffs = load_related(Diff, preview_diffs, ["diff_id"])
        preview_diff_map = {}
        for previewdiff in preview_diffs:
            preview_diff_map[previewdiff.branch_merge_proposal_id] = (
                previewdiff
            )
        for mp in branch_merge_proposals:
            get_property_cache(mp).preview_diff = preview_diff_map.get(mp.id)

        # Preload other merge proposals that supersede these.
        supersedes_map = {}
        for other_mp in load_referencing(
            BranchMergeProposal, branch_merge_proposals, ["superseded_by_id"]
        ):
            supersedes_map[other_mp.superseded_by_id] = other_mp
        for mp in branch_merge_proposals:
            get_property_cache(mp).supersedes = supersedes_map.get(mp.id)

        # Add source branch/repository owners' to the list of pre-loaded
        # persons.  We need the target repository owner as well; unlike
        # branches, repository unique names aren't trigger-maintained.
        person_ids.update(
            branch.owner_id
            for branch in branches
            if branch.id in source_branch_ids
        )
        person_ids.update(
            repository.owner_id
            for repository in repositories
            if repository.id in git_repository_ids
        )

        # Pre-load branches'/repositories' data.
        if branches:
            GenericBranchCollection.preloadDataForBranches(branches)
        if repositories:
            GenericGitCollection.preloadDataForRepositories(repositories)

        # if we need to include a vote summary, we should precache
        # that data too
        if include_votes:
            votes = load_referencing(
                CodeReviewVoteReference,
                branch_merge_proposals,
                ["branch_merge_proposal_id"],
            )
            votes_map = defaultdict(list)
            for vote in votes:
                votes_map[vote.branch_merge_proposal_id].append(vote)
            for mp in branch_merge_proposals:
                get_property_cache(mp).votes = votes_map[mp.id]
            comments = load_related(CodeReviewComment, votes, ["comment_id"])
            load_related(Message, comments, ["message_id"])
            person_ids.update(vote.reviewer_id for vote in votes)

            # we also provide a summary of diffs, so load them
            load_related(LibraryFileAlias, diffs, ["diff_text_id"])

        # Pre-load Person and ValidPersonCache.
        list(
            getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                person_ids, need_validity=True
            )
        )

    def getStatusReports(self, commit_sha1):
        return self.source_git_repository.getStatusReports(commit_sha1)


@implementer(IBranchMergeProposalGetter)
class BranchMergeProposalGetter:
    """See `IBranchMergeProposalGetter`."""

    @staticmethod
    def get(id):
        """See `IBranchMergeProposalGetter`."""
        mp = IStore(BranchMergeProposal).get(BranchMergeProposal, id)
        if mp is None:
            raise NotFoundError(id)
        return mp

    @staticmethod
    def getProposalsForContext(context, status=None, visible_by_user=None):
        """See `IBranchMergeProposalGetter`."""
        collection = getUtility(IAllBranches).visibleByUser(visible_by_user)
        if context is None:
            pass
        elif IProduct.providedBy(context):
            collection = collection.inProduct(context)
        elif IPerson.providedBy(context):
            collection = collection.ownedBy(context)
        else:
            raise BadBranchMergeProposalSearchContext(context)
        return collection.getMergeProposals(status)

    @staticmethod
    def getProposalsForParticipant(
        participant, status=None, visible_by_user=None
    ):
        """See `IBranchMergeProposalGetter`."""
        registrant_select = Select(
            BranchMergeProposal.id,
            BranchMergeProposal.registrant_id == participant.id,
        )

        review_select = Select(
            [CodeReviewVoteReference.branch_merge_proposal_id],
            [CodeReviewVoteReference.reviewer == participant],
        )

        query = Store.of(participant).find(
            BranchMergeProposal,
            BranchMergeProposal.queue_status.is_in(status),
            Or(
                BranchMergeProposal.id.is_in(registrant_select),
                BranchMergeProposal.id.is_in(review_select),
            ),
        )
        return query

    @staticmethod
    def getVotesForProposals(proposals):
        """See `IBranchMergeProposalGetter`."""
        if len(proposals) == 0:
            return {}
        ids = [proposal.id for proposal in proposals]
        store = Store.of(proposals[0])
        result = {proposal: [] for proposal in proposals}
        # Make sure that the Person and the review comment are loaded in the
        # storm cache as the reviewer is displayed in a title attribute on the
        # merge proposal listings page, and the message is needed to get to
        # the actual vote for that person.
        tables = [
            CodeReviewVoteReference,
            Join(Person, CodeReviewVoteReference.reviewer == Person.id),
            LeftJoin(
                CodeReviewComment,
                CodeReviewVoteReference.comment == CodeReviewComment.id,
            ),
        ]
        results = store.using(*tables).find(
            (CodeReviewVoteReference, Person, CodeReviewComment),
            CodeReviewVoteReference.branch_merge_proposal_id.is_in(ids),
        )
        for reference, _, _ in results:
            result[reference.branch_merge_proposal].append(reference)
        return result

    @staticmethod
    def getVoteSummariesForProposals(proposals):
        """See `IBranchMergeProposalGetter`."""
        if len(proposals) == 0:
            return {}
        ids = quote([proposal.id for proposal in proposals])
        store = Store.of(proposals[0])
        # First get the count of comments.
        query = (
            """
            SELECT bmp.id, count(crm.*)
            FROM BranchMergeProposal bmp, CodeReviewMessage crm,
                 Message m, MessageChunk mc
            WHERE bmp.id IN %s
              AND bmp.id = crm.branch_merge_proposal
              AND crm.message = m.id
              AND mc.message = m.id
              AND mc.content is not NULL
            GROUP BY bmp.id
            """
            % ids
        )
        comment_counts = dict(store.execute(query))
        # Now get the vote counts.
        query = (
            """
            SELECT bmp.id, crm.vote, count(crv.*)
            FROM BranchMergeProposal bmp, CodeReviewVote crv,
                 CodeReviewMessage crm
            WHERE bmp.id IN %s
              AND bmp.id = crv.branch_merge_proposal
              AND crv.vote_message = crm.id
            GROUP BY bmp.id, crm.vote
            """
            % ids
        )
        vote_counts = {}
        for proposal_id, vote_value, count in store.execute(query):
            vote = CodeReviewVote.items[vote_value]
            vote_counts.setdefault(proposal_id, {})[vote] = count
        # Now assemble the resulting dict.
        result = {}
        for proposal in proposals:
            summary = result.setdefault(proposal, {})
            summary["comment_count"] = comment_counts.get(proposal.id, 0)
            summary.update(vote_counts.get(proposal.id, {}))
        return result

    @staticmethod
    def activeProposalsForBranches(source, target=None):
        clauses = [Not(BranchMergeProposal.queue_status.is_in(FINAL_STATES))]
        if IGitRef.providedBy(source):
            clauses.extend(
                [
                    BranchMergeProposal.source_git_repository
                    == source.repository,
                    BranchMergeProposal.source_git_path == source.path,
                ]
            )

            if target is not None:
                clauses.extend(
                    [
                        BranchMergeProposal.target_git_repository
                        == target.repository,
                        BranchMergeProposal.target_git_path == target.path,
                    ]
                )

        else:
            clauses.extend(
                [
                    BranchMergeProposal.source_branch == source,
                ]
            )

            if target is not None:
                clauses.extend(
                    [
                        BranchMergeProposal.target_branch == target,
                    ]
                )
        return IStore(BranchMergeProposal).find(BranchMergeProposal, *clauses)
