# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from testscenarios.testcase import WithScenarios
from zope.security.interfaces import Unauthorized

from lp.app.enums import InformationType
from lp.code.enums import CodeReviewVote
from lp.code.errors import (
    ClaimReviewFailed,
    ReviewNotPending,
    UserHasExistingReview,
)
from lp.code.interfaces.codereviewvote import ICodeReviewVoteReference
from lp.code.tests.helpers import make_merge_proposal_without_reviewers
from lp.services.database.constants import UTC_NOW
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import (
    ANONYMOUS,
    TestCaseWithFactory,
    api_url,
    login,
    login_person,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestCodeReviewVote(WithScenarios, TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    scenarios = [
        ("bazaar", {"cvs": "BAZAAR"}),
        ("git", {"cvs": "GIT"}),
    ]

    def setUp(self):
        super().setUp()
        self.reviewer = self.factory.makePerson()
        self.owner = self.factory.makePerson()

        if self.cvs == "BAZAAR":
            self.for_git = False
            self.bmp = self.factory.makeBranchMergeProposal()
            self.bmp_without_reviewers = make_merge_proposal_without_reviewers(
                self.factory
            )
        elif self.cvs == "GIT":
            self.for_git = True
            self.bmp = self.factory.makeBranchMergeProposalForGit()
            self.bmp_without_reviewers = make_merge_proposal_without_reviewers(
                self.factory, for_git=True
            )

    def test_create_vote(self):
        """CodeReviewVotes can be created"""
        merge_proposal = self.bmp_without_reviewers
        login_person(merge_proposal.registrant)
        vote = merge_proposal.nominateReviewer(
            self.reviewer, merge_proposal.registrant
        )
        self.assertEqual(self.reviewer, vote.reviewer)
        self.assertEqual(merge_proposal.registrant, vote.registrant)
        self.assertEqual(merge_proposal, vote.branch_merge_proposal)
        self.assertEqual([vote], list(merge_proposal.votes))
        self.assertSqlAttributeEqualsDate(vote, "date_created", UTC_NOW)
        self.assertProvides(vote, ICodeReviewVoteReference)

    def test_anonymous_public(self):
        """Anonymous users can see votes on public merge proposals."""
        merge_proposal = self.bmp_without_reviewers
        login_person(merge_proposal.registrant)
        vote = merge_proposal.nominateReviewer(
            self.reviewer, merge_proposal.registrant
        )
        login(ANONYMOUS)
        self.assertTrue(check_permission("launchpad.View", vote))

    def test_anonymous_private(self):
        """Anonymous users cannot see votes on private merge proposals."""
        login_person(self.owner)
        if self.cvs == "BAZAAR":
            target_branch = self.factory.makeBranch(
                owner=self.owner, information_type=InformationType.USERDATA
            )
        elif self.cvs == "GIT":
            [target_branch] = self.factory.makeGitRefs(
                owner=self.owner, information_type=InformationType.USERDATA
            )
        merge_proposal = make_merge_proposal_without_reviewers(
            self.factory,
            for_git=self.for_git,
            target=target_branch,
            registrant=self.owner,
        )
        vote = merge_proposal.nominateReviewer(self.reviewer, self.owner)
        login(ANONYMOUS)
        self.assertFalse(check_permission("launchpad.View", vote))


class TestCodeReviewVoteReferenceClaimReview(
    WithScenarios, TestCaseWithFactory
):
    """Tests for CodeReviewVoteReference.claimReview."""

    layer = DatabaseFunctionalLayer

    scenarios = [
        ("bazaar", {"cvs": "BAZAAR"}),
        ("git", {"cvs": "GIT"}),
    ]

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        # Setup the proposal, claimant and team reviewer.
        self.claimant = self.factory.makePerson(name="eric")
        self.review_team = self.factory.makeTeam()

        if self.cvs == "BAZAAR":
            self.bmp = self.factory.makeBranchMergeProposal()
        elif self.cvs == "GIT":
            self.bmp = self.factory.makeBranchMergeProposalForGit()

    def _addPendingReview(self):
        """Add a pending review for the review_team."""
        login_person(self.bmp.registrant)
        return self.bmp.nominateReviewer(
            reviewer=self.review_team, registrant=self.bmp.registrant
        )

    def _addClaimantToReviewTeam(self):
        """Add the claimant to the review team."""
        login_person(self.review_team.teamowner)
        self.review_team.addMember(
            person=self.claimant, reviewer=self.review_team.teamowner
        )

    def test_personal_completed_review(self):
        # If the claimant has a personal review already, then they can't claim
        # a pending team review.
        login_person(self.claimant)
        # Make sure that the personal review is done before the pending team
        # review, otherwise the pending team review will be claimed by this
        # one.
        self.bmp.createComment(
            self.claimant,
            "Message subject",
            "Message content",
            vote=CodeReviewVote.APPROVE,
        )
        review = self._addPendingReview()
        self._addClaimantToReviewTeam()
        self.assertRaisesWithContent(
            UserHasExistingReview,
            "Eric (eric) has already reviewed this",
            review.claimReview,
            self.claimant,
        )

    def test_personal_pending_review(self):
        # If the claimant has a pending review already, then they can't claim
        # a pending team review.
        review = self._addPendingReview()
        self._addClaimantToReviewTeam()
        login_person(self.bmp.registrant)
        self.bmp.nominateReviewer(
            reviewer=self.claimant, registrant=self.bmp.registrant
        )
        login_person(self.claimant)
        self.assertRaisesWithContent(
            UserHasExistingReview,
            "Eric (eric) has already been asked to review this",
            review.claimReview,
            self.claimant,
        )

    def test_personal_not_in_review_team(self):
        # If the claimant is not in the review team, an error is raised.
        review = self._addPendingReview()
        # Since the claimant isn't in the review team, they don't have
        # launchpad.Edit on the review itself, hence Unauthorized.
        login_person(self.claimant)
        # Actually accessing claimReview triggers the security proxy.
        self.assertRaises(Unauthorized, getattr, review, "claimReview")
        # The merge proposal registrant however does have edit permissions,
        # but isn't in the team, so they get ClaimReviewFailed.
        login_person(self.bmp.registrant)
        self.assertRaises(
            ClaimReviewFailed, review.claimReview, self.bmp.registrant
        )

    def test_success(self):
        # If the claimant is in the review team, and does not have a personal
        # review, pending or completed, then they can claim the team review.
        review = self._addPendingReview()
        self._addClaimantToReviewTeam()
        login_person(self.claimant)
        review.claimReview(self.claimant)
        self.assertEqual(self.claimant, review.reviewer)

    def test_repeat_claim(self):
        # Attempting to claim an already-claimed review works.
        review = self.factory.makeCodeReviewVoteReference()
        login_person(review.reviewer)
        review.claimReview(review.reviewer)


class TestCodeReviewVoteReferenceDelete(WithScenarios, TestCaseWithFactory):
    """Tests for CodeReviewVoteReference.delete."""

    layer = DatabaseFunctionalLayer

    scenarios = [
        ("bazaar", {"cvs": "BAZAAR"}),
        ("git", {"cvs": "GIT"}),
    ]

    def setUp(self):
        super().setUp()
        if self.cvs == "BAZAAR":
            self.bmp = self.factory.makeBranchMergeProposal()
            self.bmp_without_reviewers = make_merge_proposal_without_reviewers(
                self.factory
            )
            self.bmp_without_reviewers_owner = (
                self.bmp_without_reviewers.target_branch.owner
            )
        elif self.cvs == "GIT":
            self.bmp = self.factory.makeBranchMergeProposalForGit()
            self.bmp_without_reviewers = make_merge_proposal_without_reviewers(
                self.factory, for_git=True
            )
            self.bmp_without_reviewers_owner = (
                self.bmp_without_reviewers.target_git_repository.owner
            )

    def test_delete_pending_by_registrant(self):
        # A pending review can be deleted by the person requesting the review.
        reviewer = self.factory.makePerson()
        bmp = self.bmp_without_reviewers
        login_person(bmp.registrant)
        review = bmp.nominateReviewer(
            reviewer=reviewer, registrant=bmp.registrant
        )
        review.delete()
        self.assertEqual([], list(bmp.votes))

    def test_delete_pending_by_reviewer(self):
        # A pending review can be deleted by the person requesting the review.
        reviewer = self.factory.makePerson()
        bmp = self.bmp_without_reviewers
        login_person(bmp.registrant)
        review = bmp.nominateReviewer(
            reviewer=reviewer, registrant=bmp.registrant
        )
        login_person(reviewer)
        review.delete()
        self.assertEqual([], list(bmp.votes))

    def test_delete_pending_by_review_team_member(self):
        # A pending review can be deleted by the person requesting the review.
        review_team = self.factory.makeTeam()
        bmp = self.bmp_without_reviewers
        login_person(bmp.registrant)
        review = bmp.nominateReviewer(
            reviewer=review_team, registrant=bmp.registrant
        )
        login_person(review_team.teamowner)
        review.delete()
        self.assertEqual([], list(bmp.votes))

    def test_delete_pending_by_target_branch_owner(self):
        # A pending review can be deleted by anyone with edit permissions on
        # the target branch.
        reviewer = self.factory.makePerson()
        bmp = self.bmp_without_reviewers
        login_person(bmp.registrant)
        review = bmp.nominateReviewer(
            reviewer=reviewer, registrant=bmp.registrant
        )
        login_person(self.bmp_without_reviewers_owner)
        review.delete()
        self.assertEqual([], list(bmp.votes))

    def test_delete_by_others_unauthorized(self):
        # A pending review can be deleted by the person requesting the review.
        reviewer = self.factory.makePerson()
        bmp = self.bmp
        login_person(bmp.registrant)
        review = bmp.nominateReviewer(
            reviewer=reviewer, registrant=bmp.registrant
        )
        login_person(self.factory.makePerson())
        self.assertRaises(Unauthorized, getattr, review, "delete")

    def test_delete_not_pending(self):
        # A non-pending review reference cannot be deleted.
        reviewer = self.factory.makePerson()
        bmp = self.bmp_without_reviewers
        login_person(reviewer)
        bmp.createComment(
            reviewer,
            "Message subject",
            "Message content",
            vote=CodeReviewVote.APPROVE,
        )
        [review] = list(bmp.votes)
        self.assertRaises(ReviewNotPending, review.delete)


class TestCodeReviewVoteReferenceReassignReview(
    WithScenarios, TestCaseWithFactory
):
    """Tests for CodeReviewVoteReference.reassignReview."""

    layer = DatabaseFunctionalLayer

    scenarios = [
        ("bazaar", {"cvs": "BAZAAR"}),
        ("git", {"cvs": "GIT"}),
    ]

    def setUp(self):
        super().setUp()
        self.for_git = True if self.cvs == "GIT" else False

    def makeMergeProposalWithReview(self, completed=False):
        """Return a new merge proposal with a review."""
        bmp = make_merge_proposal_without_reviewers(
            self.factory, for_git=self.for_git
        )
        reviewer = self.factory.makePerson()
        if completed:
            login_person(reviewer)
            bmp.createComment(
                reviewer,
                "Message subject",
                "Message content",
                vote=CodeReviewVote.APPROVE,
            )
            [review] = list(bmp.votes)
        else:
            login_person(bmp.registrant)
            review = bmp.nominateReviewer(
                reviewer=reviewer, registrant=bmp.registrant
            )
        return bmp, review

    def test_reassign_pending(self):
        # A pending review can be reassigned to someone else.
        bmp, review = self.makeMergeProposalWithReview()
        new_reviewer = self.factory.makePerson()
        review.reassignReview(new_reviewer)
        self.assertEqual(new_reviewer, review.reviewer)

    def test_reassign_completed_review(self):
        # A completed review cannot be reassigned
        bmp, review = self.makeMergeProposalWithReview(completed=True)
        self.assertRaises(
            ReviewNotPending, review.reassignReview, bmp.registrant
        )

    def test_reassign_to_user_existing_pending(self):
        # If a user has an existing pending review, they cannot have another
        # pending review assigned to them.
        bmp, review = self.makeMergeProposalWithReview()
        reviewer = self.factory.makePerson(name="eric")
        bmp.nominateReviewer(reviewer=reviewer, registrant=bmp.registrant)
        self.assertRaisesWithContent(
            UserHasExistingReview,
            "Eric (eric) has already been asked to review this",
            review.reassignReview,
            reviewer,
        )

    def test_reassign_to_user_existing_completed(self):
        # If a user has an existing completed review, they cannot have another
        # pending review assigned to them.
        bmp, review = self.makeMergeProposalWithReview()
        reviewer = self.factory.makePerson(name="eric")
        bmp.createComment(
            reviewer,
            "Message subject",
            "Message content",
            vote=CodeReviewVote.APPROVE,
        )
        self.assertRaisesWithContent(
            UserHasExistingReview,
            "Eric (eric) has already reviewed this",
            review.reassignReview,
            reviewer,
        )

    def test_reassign_to_team_existing(self):
        # If a team has an existing review, they can have another pending
        # review assigned to them.
        bmp, review = self.makeMergeProposalWithReview()
        reviewer_team = self.factory.makeTeam()
        bmp.nominateReviewer(reviewer=reviewer_team, registrant=bmp.registrant)
        review.reassignReview(reviewer_team)
        self.assertEqual(reviewer_team, review.reviewer)


class TestCodeReviewVoteReferenceDeleteWebservice(TestCaseWithFactory):
    """Webservice-level tests for CodeReviewVoteReference.delete."""

    layer = DatabaseFunctionalLayer

    def test_delete_not_pending_returns_bad_request(self):
        # Deleting a cast (non-pending) vote via the webservice returns
        # 400 Bad Request, not 500 Internal Server Error.
        bmp = make_merge_proposal_without_reviewers(self.factory)
        reviewer = self.factory.makePerson()
        with person_logged_in(reviewer):
            bmp.createComment(
                reviewer,
                "Message subject",
                "Message content",
                vote=CodeReviewVote.APPROVE,
            )
        with person_logged_in(bmp.registrant):
            [review] = list(bmp.votes)
            review_url = api_url(review)
        webservice = webservice_for_person(
            reviewer,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.delete(review_url)
        self.assertEqual(400, response.status)
        self.assertIn(b"The review is not pending.", response.body)

    def test_delete_pending_succeeds(self):
        bmp = make_merge_proposal_without_reviewers(self.factory)
        reviewer = self.factory.makePerson()
        with person_logged_in(bmp.registrant):
            registrant = bmp.registrant
            review = bmp.nominateReviewer(
                reviewer=reviewer, registrant=bmp.registrant
            )
            review_url = api_url(review)
        webservice = webservice_for_person(
            reviewer,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )
        response = webservice.delete(review_url)
        self.assertEqual(200, response.status)
        with person_logged_in(registrant):
            self.assertEqual([], list(bmp.votes))
