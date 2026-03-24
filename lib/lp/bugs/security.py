# Copyright 2010-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Security adapters for the bugs module."""

__all__ = [
    "PublicToAllOrPrivateToExplicitSubscribersForBugTask",
]

from lp.app.security import (
    AnonymousAuthorization,
    AuthorizationBase,
    DelegatedAuthorization,
)
from lp.bugs.enums import BugLockStatus
from lp.bugs.interfaces.bug import IBug
from lp.bugs.interfaces.bugactivity import IBugActivity
from lp.bugs.interfaces.bugattachment import IBugAttachment
from lp.bugs.interfaces.bugnomination import IBugNomination
from lp.bugs.interfaces.bugpresence import IBugPresence
from lp.bugs.interfaces.bugsubscription import IBugSubscription
from lp.bugs.interfaces.bugsubscriptionfilter import IBugSubscriptionFilter
from lp.bugs.interfaces.bugsupervisor import IHasBugSupervisor
from lp.bugs.interfaces.bugtarget import IOfficialBugTagTargetRestricted
from lp.bugs.interfaces.bugtask import IBugTaskDelete
from lp.bugs.interfaces.bugtracker import IBugTracker
from lp.bugs.interfaces.bugwatch import IBugWatch
from lp.bugs.interfaces.hasbug import IHasBug
from lp.bugs.interfaces.structuralsubscription import IStructuralSubscription
from lp.bugs.interfaces.vulnerability import IVulnerability
from lp.registry.interfaces.role import IHasAppointedDriver, IHasOwner
from lp.services.messages.interfaces.message import IMessage


class EditBugNominationStatus(AuthorizationBase):
    permission = "launchpad.Driver"
    usedfor = IBugNomination

    def checkAuthenticated(self, user):
        return self.obj.canApprove(user.person)


class AppendBugTask(DelegatedAuthorization):
    """Security adapter for appending to bug tasks.

    This has the same semantics as `AppendBug`, but can be used where the
    context is a bug task rather than a bug.
    """

    permission = "launchpad.Append"
    usedfor = IHasBug

    def __init__(self, obj):
        super().__init__(obj, obj.bug)


class EditBugTask(DelegatedAuthorization):
    """Permission checker for editing objects linked to a bug.

    Allow people who can edit a bug to edit the tasks linked to it.
    """

    permission = "launchpad.Edit"
    usedfor = IHasBug

    def __init__(self, obj):
        super().__init__(obj, obj.bug)


class DeleteBugTask(AuthorizationBase):
    permission = "launchpad.Delete"
    usedfor = IBugTaskDelete

    def checkAuthenticated(self, user):
        """Check that a user may delete a bugtask.

        A user may delete a bugtask if:
         - project maintainer
         - task creator
         - bug supervisor
        """
        if user is None:
            return False

        # Admins can always delete bugtasks.
        if user.in_admin:
            return True

        bugtask = self.obj
        owner = None
        if IHasOwner.providedBy(bugtask.bug_target_parent):
            owner = bugtask.bug_target_parent.owner
        bugsupervisor = None
        if IHasBugSupervisor.providedBy(bugtask.bug_target_parent):
            bugsupervisor = bugtask.bug_target_parent.bug_supervisor
        return (
            user.inTeam(owner)
            or user.inTeam(bugsupervisor)
            or user.inTeam(bugtask.owner)
        )


class AdminDeleteBugTask(DeleteBugTask):
    """Launchpad admins can also delete bug tasks."""

    permission = "launchpad.Admin"


class PublicToAllOrPrivateToExplicitSubscribersForBugTask(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IHasBug

    def checkAuthenticated(self, user):
        return self.obj.bug.userCanView(user.person)

    def checkUnauthenticated(self):
        """Allow anonymous users to see non-private bugs only."""
        return not self.obj.bug.private


class AppendBug(AuthorizationBase):
    """Security adapter for appending to bugs.

    This is used for operations that anyone who can see the bug can perform.
    """

    permission = "launchpad.Append"
    usedfor = IBug

    def checkAuthenticated(self, user):
        """Allow any logged in user to append to a public bug, and only
        explicit subscribers to append to private bugs. Any bug that can be
        seen can be appended to.
        """
        return self.obj.userCanView(user)

    def checkUnauthenticated(self):
        """Never allow unauthenticated users to append to a bug."""
        return False


def _has_any_bug_role(user, tasks):
    """Return True if the user has any role on any of these bug tasks."""
    targets = [task.bug_target_parent for task in tasks]
    bug_target_roles = {}
    for target in targets:
        roles = []
        if IHasOwner.providedBy(target):
            roles.append("owner")
        if IHasAppointedDriver.providedBy(target):
            roles.append("driver")
        if IHasBugSupervisor.providedBy(target):
            roles.append("bug_supervisor")
        bug_target_roles[target] = roles

    teams = []

    for bug_target, roles in bug_target_roles.items():
        for role_ in roles:
            role = getattr(bug_target, role_, None)
            if role:
                teams.append(role)
    return user.inAnyTeam(teams)


class EditBug(AuthorizationBase):
    """Security adapter for editing bugs.

    This is used for operations that are potentially destructive in some
    way.  They aren't heavily locked down, but only users who appear to be
    legitimate can perform them.
    """

    permission = "launchpad.Edit"
    usedfor = IBug

    def checkAuthenticated(self, user):
        """Allow sufficiently-trusted users to edit bugs.

        Only users who can append to the bug can edit it; in addition, only
        users who seem to be generally legitimate or who have a relevant
        role on one of the targets of the bug can edit the bug.
        """
        if not self.forwardCheckAuthenticated(
            user, permission="launchpad.Append"
        ):
            # The user cannot even see the bug.
            return False

        def in_allowed_roles():
            return (
                # Users with relevant roles can edit the bug.
                user.in_admin
                or user.in_commercial_admin
                or user.in_registry_experts
                or _has_any_bug_role(user, self.obj.bugtasks)
            )

        if self.obj.lock_status == BugLockStatus.COMMENT_ONLY:
            return in_allowed_roles()

        return (
            # If the bug is private, then we don't need more elaborate
            # checks as they must have been explicitly subscribed.
            self.obj.private
            or
            # If the user seems generally legitimate, let them through.
            self.forwardCheckAuthenticated(
                user, permission="launchpad.AnyLegitimatePerson"
            )
            or
            # The bug reporter can edit their own bug if it is unlocked.
            user.inTeam(self.obj.owner)
            or in_allowed_roles()
        )

    def checkUnauthenticated(self):
        """Never allow unauthenticated users to edit a bug."""
        return False


class ModerateBug(AuthorizationBase):
    """Security adapter for moderating bugs.

    This is used for operations like locking and unlocking a bug to the
    relevant roles.
    """

    permission = "launchpad.Moderate"
    usedfor = IBug

    def checkAuthenticated(self, user):
        if not self.forwardCheckAuthenticated(
            user, permission="launchpad.Append"
        ):
            # The user cannot even see the bug.
            return False

        return (
            user.in_admin
            or user.in_commercial_admin
            or user.in_registry_experts
            or _has_any_bug_role(user, self.obj.bugtasks)
        )


class ModerateBugTask(DelegatedAuthorization):
    """
    Security adapter for moderating bug tasks.

    This has the same semantics as `ModerateBug`, but can be used where
    the context is a bug task rather than a bug.
    """

    permission = "launchpad.Moderate"
    usedfor = IHasBug

    def __init__(self, obj):
        super().__init__(obj, obj.bug)


class PublicToAllOrPrivateToExplicitSubscribersForBug(AuthorizationBase):
    permission = "launchpad.View"
    usedfor = IBug

    def checkAuthenticated(self, user):
        """Allow any user to see non-private bugs, but only explicit
        subscribers to see private bugs.
        """
        return self.obj.userCanView(user.person)

    def checkUnauthenticated(self):
        """Allow anonymous users to see non-private bugs only."""
        return not self.obj.private


class ViewBugAttachment(DelegatedAuthorization):
    """Security adapter for viewing a bug attachment.

    If the user is authorized to view the bug, they're allowed to view the
    attachment.
    """

    permission = "launchpad.View"
    usedfor = IBugAttachment

    def __init__(self, bugattachment):
        super().__init__(bugattachment, bugattachment.bug)


class EditBugAttachment(AuthorizationBase):
    """Security adapter for editing a bug attachment.

    If the user is authorized to view the bug, they're allowed to edit the
    attachment.
    """

    permission = "launchpad.Edit"
    usedfor = IBugAttachment

    def checkAuthenticated(self, user):
        return (
            user.in_admin
            or user.in_registry_experts
            or user.inTeam(self.obj.message.owner)
            or _has_any_bug_role(user, self.obj.bug.bugtasks)
        )

    def checkUnauthenticated(self):
        return False


class ViewBugPresence(DelegatedAuthorization):
    """Security adapter for viewing a bug presence.

    Anyone with permission to see the bug, can see its presence
    """

    permission = "launchpad.View"
    usedfor = IBugPresence

    def __init__(self, bugpresence):
        super().__init__(bugpresence, bugpresence.bug, "launchpad.View")


class DeleteBugPresence(DelegatedAuthorization):
    """Security adapter for deleting a bug presence."""

    permission = "launchpad.Delete"
    usedfor = IBugPresence

    def checkAuthenticated(self, user):
        return user.in_admin

    def checkUnauthenticated(self):
        return False


class ViewBugActivity(DelegatedAuthorization):
    """Security adapter for viewing a bug activity record.

    If the user is authorized to view the bug, they're allowed to view the
    activity.
    """

    permission = "launchpad.View"
    usedfor = IBugActivity

    def __init__(self, bugactivity):
        super().__init__(bugactivity, bugactivity.bug)


class ViewBugSubscription(AnonymousAuthorization):
    usedfor = IBugSubscription


class EditBugSubscription(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IBugSubscription

    def checkAuthenticated(self, user):
        """Check that a user may edit a subscription.

        A user may edit a subscription if:
         - They are the owner of the subscription.
         - They are the owner of the team that owns the subscription.
         - They are an admin of the team that owns the subscription.
        """
        if self.obj.person.is_team:
            return (
                self.obj.person.teamowner == user.person
                or user.person in self.obj.person.adminmembers
            )
        else:
            return user.person == self.obj.person


class ViewBugMessage(AnonymousAuthorization):
    usedfor = IMessage


class ViewBugTracker(AnonymousAuthorization):
    """Anyone can view a bug tracker."""

    usedfor = IBugTracker


class EditBugTracker(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IBugTracker

    def checkAuthenticated(self, user):
        """Any logged-in user can edit a bug tracker."""
        return True


class AdminBugTracker(AuthorizationBase):
    permission = "launchpad.Admin"
    usedfor = IBugTracker

    def checkAuthenticated(self, user):
        return user.in_janitor or user.in_admin or user.in_registry_experts


class AdminBugWatch(AuthorizationBase):
    permission = "launchpad.Admin"
    usedfor = IBugWatch

    def checkAuthenticated(self, user):
        return user.in_admin or user.in_registry_experts


class EditStructuralSubscription(AuthorizationBase):
    permission = "launchpad.Edit"
    usedfor = IStructuralSubscription

    def checkAuthenticated(self, user):
        """Who can edit StructuralSubscriptions."""

        assert self.obj.target

        # Removal of a target cascades removals to StructuralSubscriptions,
        # so we need to allow editing to those who can edit the target itself.
        can_edit_target = self.forwardCheckAuthenticated(user, self.obj.target)

        # Who is actually allowed to edit a subscription is determined by
        # a helper method on the model.
        can_edit_subscription = self.obj.target.userCanAlterSubscription(
            self.obj.subscriber, user.person
        )

        return can_edit_target or can_edit_subscription


class EditBugSubscriptionFilter(AuthorizationBase):
    """Bug subscription filters may only be modified by the subscriber."""

    permission = "launchpad.Edit"
    usedfor = IBugSubscriptionFilter

    def checkAuthenticated(self, user):
        return user.inTeam(self.obj.structural_subscription.subscriber)


class ViewVulnerability(AnonymousAuthorization):
    """Anyone can view public vulnerabilities, but only subscribers
    can view private ones.
    """

    permission = "launchpad.View"
    usedfor = IVulnerability

    def checkUnauthenticated(self):
        return self.obj.visibleByUser(None)

    def checkAuthenticated(self, user):
        return self.obj.visibleByUser(user.person)


class EditVulnerability(DelegatedAuthorization):
    """The security admins of a distribution should be able to edit
    vulnerabilities in that distribution."""

    permission = "launchpad.Edit"
    usedfor = IVulnerability

    def __init__(self, obj):
        super().__init__(obj, obj.distribution, "launchpad.SecurityAdmin")


class BugTargetOwnerOrBugSupervisorOrAdmins(AuthorizationBase):
    """Product's owner and bug supervisor can set official bug tags."""

    permission = "launchpad.BugSupervisor"
    usedfor = IOfficialBugTagTargetRestricted

    def checkAuthenticated(self, user):
        return (
            user.inTeam(self.obj.bug_supervisor)
            or user.inTeam(self.obj.owner)
            or user.in_admin
        )
