# Copyright 2009-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Webservice unit tests related to Launchpad Bugs."""

from zope.component import getUtility

from lp.app.enums import InformationType
from lp.bugs.interfaces.bugmessage import IBugMessageSet
from lp.bugs.interfaces.bugtask import BugTaskStatus, BugTaskStatusSearch
from lp.registry.interfaces.accesspolicy import IAccessPolicySource
from lp.testing import (
    TestCaseWithFactory,
    login,
    login_celebrity,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer


class TestBugIndexedMessages(TestCaseWithFactory):
    """Test the workings of IBug.indexed_messages."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        login("foo.bar@canonical.com")

        bug_1 = self.factory.makeBug()
        self.bug_2 = self.factory.makeBug()

        message_1 = self.factory.makeMessage()
        message_2 = self.factory.makeMessage()
        message_2.parent = message_1

        bug_1.linkMessage(message_1)
        self.bug_2.linkMessage(message_2)

    def test_indexed_message_null_parents(self):
        # Accessing the parent of an IIndexedMessage will return None if
        # the parent isn't linked to the same bug as the
        # IIndexedMessage.
        for indexed_message in self.bug_2.indexed_messages:
            self.assertIsNone(indexed_message.parent)


class TestUserCanSetCommentVisibility(TestCaseWithFactory):
    """Test whether expected users can toggle bug comment visibility."""

    layer = DatabaseFunctionalLayer

    def test_random_user_cannot_toggle_comment_visibility(self):
        # A random user cannot set bug comment visibility.
        person = self.factory.makePerson()
        bug = self.factory.makeBug()
        self.assertFalse(bug.userCanSetCommentVisibility(person))

    def test_registry_admin_can_toggle_comment_visibility(self):
        # Members of registry experts can set bug comment visibility.
        person = login_celebrity("registry_experts")
        bug = self.factory.makeBug()
        self.assertTrue(bug.userCanSetCommentVisibility(person))

    def test_admin_can_toggle_comment_visibility(self):
        # Admins can set bug comment visibility.
        person = login_celebrity("admin")
        bug = self.factory.makeBug()
        self.assertTrue(bug.userCanSetCommentVisibility(person))

    def test_userdata_grant_can_toggle_comment_visibility(self):
        person = self.factory.makePerson()
        product = self.factory.makeProduct()
        bug = self.factory.makeBug(target=product)
        policy = (
            getUtility(IAccessPolicySource)
            .find([(product, InformationType.USERDATA)])
            .one()
        )
        self.factory.makeAccessPolicyGrant(
            policy=policy, grantor=product.owner, grantee=person
        )
        self.assertTrue(bug.userCanSetCommentVisibility(person))


class TestBugLinkMessageSetsIncompleteStatus(TestCaseWithFactory):
    """Test that Bug.linkMessage updates "Incomplete (without response)" bugs.

    They should transition from "Incomplete (without response)" to
    "Incomplete (with response)".
    """

    layer = DatabaseFunctionalLayer

    def test_new_untouched(self):
        bugtask = self.factory.makeBugTask(status=BugTaskStatus.NEW)
        with person_logged_in(bugtask.owner):
            bugtask.bug.linkMessage(self.factory.makeMessage())
        self.assertEqual(BugTaskStatus.NEW, bugtask.status)

    def test_incomplete_with_response_untouched(self):
        bugtask = self.factory.makeBugTask(
            status=BugTaskStatusSearch.INCOMPLETE_WITH_RESPONSE
        )
        self.assertEqual(BugTaskStatus.INCOMPLETE, bugtask.status)
        self.assertEqual(
            BugTaskStatusSearch.INCOMPLETE_WITH_RESPONSE, bugtask._status
        )
        with person_logged_in(bugtask.owner):
            bugtask.bug.linkMessage(self.factory.makeMessage())
        self.assertEqual(BugTaskStatus.INCOMPLETE, bugtask.status)
        self.assertEqual(
            BugTaskStatusSearch.INCOMPLETE_WITH_RESPONSE, bugtask._status
        )

    def test_incomplete_without_response_updated(self):
        bugtask = self.factory.makeBugTask(status=BugTaskStatus.INCOMPLETE)
        self.assertEqual(BugTaskStatus.INCOMPLETE, bugtask.status)
        self.assertEqual(
            BugTaskStatusSearch.INCOMPLETE_WITHOUT_RESPONSE, bugtask._status
        )
        with person_logged_in(bugtask.owner):
            bugtask.bug.linkMessage(self.factory.makeMessage())
        self.assertEqual(BugTaskStatus.INCOMPLETE, bugtask.status)
        self.assertEqual(
            BugTaskStatusSearch.INCOMPLETE_WITH_RESPONSE, bugtask._status
        )


class TestBugCommentEditing(TestCaseWithFactory):
    """Tests for editing bug comments that have no text content."""

    layer = DatabaseFunctionalLayer

    def test_edit_bug_newmessage_with_no_content(self):
        # A bug comment created without content should be editable.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug()
        with person_logged_in(owner):
            msg = bug.newMessage(owner=owner, subject="Subject", content=None)
        with person_logged_in(owner):
            msg.editContent("new content")
        self.assertEqual("new content", msg.text_contents)

    def test_edit_bugmessageset_create_with_no_content(self):
        # A bug comment created without content should be editable.
        owner = self.factory.makePerson()
        bug = self.factory.makeBug()
        bugmsg = getUtility(IBugMessageSet).createMessage(
            "Subject", bug, owner, content=None
        )
        with person_logged_in(owner):
            bugmsg.editContent("new content")
        self.assertEqual("new content", bugmsg.text_contents)
