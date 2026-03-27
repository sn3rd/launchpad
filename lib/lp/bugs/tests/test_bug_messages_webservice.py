# Copyright 2011-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Webservice unit tests related to Launchpad Bug messages."""
import transaction
from testtools.matchers import HasLength
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import InformationType
from lp.bugs.interfaces.bugmessage import IBugMessageSet
from lp.bugs.model.bugnotification import BugNotification
from lp.registry.interfaces.accesspolicy import IAccessPolicySource
from lp.registry.interfaces.person import IPersonSet
from lp.services.database.interfaces import IStore
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login_celebrity,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestMessageTraversal(TestCaseWithFactory):
    """Tests safe traversal of bugs.

    See bug 607438."""

    layer = LaunchpadFunctionalLayer

    def test_message_with_attachments(self):
        bug = self.factory.makeBug()
        # Traversal over bug messages attachments has no errors.
        expected_messages = []
        with person_logged_in(bug.owner):
            for _ in range(3):
                att = self.factory.makeBugAttachment(bug)
                expected_messages.append(att.message.subject)
        bug_url = api_url(bug)

        webservice = webservice_for_person(self.factory.makePerson())
        ws_bug = self.getWebserviceJSON(webservice, bug_url)
        ws_bug_attachments = self.getWebserviceJSON(
            webservice, ws_bug["attachments_collection_link"]
        )
        messages = [
            self.getWebserviceJSON(webservice, attachment["message_link"])[
                "subject"
            ]
            for attachment in ws_bug_attachments["entries"]
            if attachment["message_link"] is not None
        ]
        self.assertContentEqual(messages, expected_messages)

    def test_message_with_parent(self):
        # The API exposes the parent attribute IMessage that is hidden by
        # IIndexedMessage. The representation cannot make a link to the
        # parent message because it might switch to another context
        # object that is not exposed or the user may not have access to.
        message_1 = self.factory.makeMessage()
        message_2 = self.factory.makeMessage()
        message_2.parent = message_1
        bug = self.factory.makeBug()
        with person_logged_in(bug.owner):
            bug.linkMessage(message_2)
        bug_url = api_url(bug)
        message_2_url = api_url(message_2)
        user = self.factory.makePerson()
        webservice = webservice_for_person(user)
        ws_bug = self.getWebserviceJSON(webservice, bug_url)
        ws_bug_messages = self.getWebserviceJSON(
            webservice, ws_bug["messages_collection_link"]
        )
        for ws_message in ws_bug_messages["entries"]:
            # An IIndexedMessage's representation.
            self.assertIsNone(ws_message["parent_link"])
        # An IMessage's representation.
        ws_message = self.getWebserviceJSON(webservice, message_2_url)
        self.assertIsNone(ws_message["parent_link"])


class TestBugMessage(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_attachments(self):
        # A bug's attachments and the union of its messages' attachments
        # are the same set.
        with admin_logged_in():
            bug = self.factory.makeBug()
            created_attachment_ids = {
                self.factory.makeBugAttachment(bug).id for i in range(3)
            }
            bug_url = api_url(bug)
        self.assertThat(created_attachment_ids, HasLength(3))

        webservice = webservice_for_person(None)
        bug_attachments = webservice.get(bug_url + "/attachments").jsonBody()[
            "entries"
        ]
        bug_attachment_ids = {
            int(att["self_link"].rsplit("/", 1)[1]) for att in bug_attachments
        }
        self.assertContentEqual(created_attachment_ids, bug_attachment_ids)

        messages = webservice.get(bug_url + "/messages").jsonBody()["entries"]
        message_attachments = []
        for message in messages[1:]:
            attachments_url = message["bug_attachments_collection_link"]
            attachments = webservice.get(attachments_url).jsonBody()["entries"]
            self.assertThat(attachments, HasLength(1))
            message_attachments.append(attachments[0])
        message_attachment_ids = {
            int(att["self_link"].rsplit("/", 1)[1])
            for att in message_attachments
        }
        self.assertContentEqual(bug_attachment_ids, message_attachment_ids)

    def _email_sent(self):
        latest_notification = (
            IStore(BugNotification)
            .find(BugNotification)
            .order_by(BugNotification.id)
            .last()
        )
        return (
            "Test comment on bug" == latest_notification.message.text_contents
        )

    def test_disable_email_on_bug_comment_with_ordinary_user(self):
        person = self.factory.makePerson()
        bug = self.factory.makeBug()
        bug_url = api_url(bug)

        webservice = webservice_for_person(
            person, permission=OAuthPermission.WRITE_PUBLIC
        )

        response = webservice.named_post(
            bug_url,
            "newMessage",
            content="Test comment on bug",
            send_notifications=False,
        )

        self.assertEqual(400, response.status)
        self.assertEqual(
            b"Email notifications can only be disabled by admins, "
            b"commercial admins, registry experts, or bug supervisors.",
            response.body,
        )
        self.assertFalse(self._email_sent())

    def test_disable_email_on_bug_comment_with_admin(self):
        # Admins will be able to disable notifs
        self.person_set = getUtility(IPersonSet)
        admins = self.person_set.getByName("admins")
        self.admin = admins.teamowner
        with admin_logged_in():
            bug = self.factory.makeBug()
            bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.admin, permission=OAuthPermission.WRITE_PUBLIC
        )

        response = webservice.named_post(
            bug_url,
            "newMessage",
            content="Test comment on bug",
            send_notifications=False,
        )

        self.assertEqual(201, response.status)
        message_url = response.getHeader("Location")
        added_message = webservice.get(message_url)
        self.assertEqual(
            "Test comment on bug", added_message.jsonBody()["content"]
        )
        self.assertFalse(self._email_sent())

    def test_disable_email_on_bug_comment_with_commercial_admin(self):
        # Commercial admins will be able to disable notifs
        bug = self.factory.makeBug()
        bug_url = api_url(bug)
        commercial_admin = self.factory.makeCommercialAdmin()
        webservice = webservice_for_person(
            commercial_admin, permission=OAuthPermission.WRITE_PUBLIC
        )

        response = webservice.named_post(
            bug_url,
            "newMessage",
            content="Test comment on bug",
            send_notifications=False,
        )

        self.assertEqual(201, response.status)
        message_url = response.getHeader("Location")
        added_message = webservice.get(message_url)
        self.assertEqual(
            "Test comment on bug", added_message.jsonBody()["content"]
        )
        self.assertFalse(self._email_sent())

    def test_disable_email_on_bug_comment_with_supervisor(self):
        # A bug supervisor on at least one affected_bug_target_parents of the
        # bug should be able to disable email notifications
        self.person_set = getUtility(IPersonSet)
        admins = self.person_set.getByName("admins")
        self.admin = admins.teamowner

        with admin_logged_in():
            product = self.factory.makeProduct(
                owner=self.admin,
                official_malone=True,
                bug_supervisor=self.admin,
            )
            bug = self.factory.makeBug(target=product, owner=self.admin)
            transaction.commit()
            bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.admin, permission=OAuthPermission.WRITE_PUBLIC
        )

        response = webservice.named_post(
            bug_url,
            "newMessage",
            content="Test comment on bug",
            send_notifications=False,
        )

        self.assertEqual(201, response.status)
        message_url = response.getHeader("Location")
        added_message = webservice.get(message_url)
        self.assertEqual(
            "Test comment on bug", added_message.jsonBody()["content"]
        )
        self.assertFalse(self._email_sent())

    def test_disable_email_on_bug_comment_default(self):
        # When send_notifications is not passed in it defaults to True
        # and doesn't require bug supervisor privileges to create the comment
        self.person_set = getUtility(IPersonSet)
        admins = self.person_set.getByName("admins")
        self.admin = admins.teamowner
        with admin_logged_in():
            bug = self.factory.makeBug()
            bug_url = api_url(bug)
        webservice = webservice_for_person(
            self.admin, permission=OAuthPermission.WRITE_PUBLIC
        )

        response = webservice.named_post(
            bug_url,
            "newMessage",
            content="Test comment on bug",
        )

        # the endpoint is still callable and if send_notifications is not
        # passed in. The comment will be created and a notification will be
        # created as it did before introducing the silencing of emails through
        # exposure of send_notifications on the API
        self.assertEqual(201, response.status)
        message_url = response.getHeader("Location")
        added_message = webservice.get(message_url)
        self.assertEqual(
            "Test comment on bug", added_message.jsonBody()["content"]
        )
        self.assertTrue(self._email_sent())


class TestSetCommentVisibility(TestCaseWithFactory):
    """Tests who can successfully set comment visibility."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person_set = getUtility(IPersonSet)
        admins = self.person_set.getByName("admins")
        self.admin = admins.teamowner
        with person_logged_in(self.admin):
            self.bug = self.factory.makeBug()
            self.bug_url = api_url(self.bug)
            self.message = self.factory.makeBugComment(
                bug=self.bug, subject="foo", body="bar"
            )

    def _check_comment_hidden(self):
        bug_msg_set = getUtility(IBugMessageSet)
        with person_logged_in(self.admin):
            bug_message = bug_msg_set.getByBugAndMessage(
                self.bug, self.message
            )
            self.assertFalse(bug_message.message.visible)

    def _test_hide_comment(self, person, should_fail=False):
        webservice = webservice_for_person(
            person, permission=OAuthPermission.WRITE_PUBLIC
        )
        response = webservice.named_post(
            self.bug_url,
            "setCommentVisibility",
            comment_number=1,
            visible=False,
        )
        if should_fail:
            self.assertEqual(401, response.status)
        else:
            self.assertEqual(200, response.status)
            self._check_comment_hidden()

    def test_random_user_cannot_set_visible(self):
        # Logged in users without privs can't set bug comment
        # visibility.
        nopriv = self.person_set.getByName("no-priv")
        self._test_hide_comment(person=nopriv, should_fail=True)

    def test_anon_cannot_set_visible(self):
        # Anonymous users can't set bug comment
        # visibility.
        self._test_hide_comment(person=None, should_fail=True)

    def test_registry_admin_can_set_visible(self):
        # Members of registry experts can set bug comment
        # visibility.
        person = login_celebrity("registry_experts")
        self._test_hide_comment(person)

    def test_admin_can_set_visible(self):
        # Admins can set bug comment
        # visibility.
        person = login_celebrity("admin")
        self._test_hide_comment(person)

    def test_userdata_grantee_can_set_visible(self):
        person = self.factory.makePerson()
        proxiless_bugtask = removeSecurityProxy(self.bug.default_bugtask)
        bug_target_parent = proxiless_bugtask.bug_target_parent
        policy = (
            getUtility(IAccessPolicySource)
            .find([(bug_target_parent, InformationType.USERDATA)])
            .one()
        )
        self.factory.makeAccessPolicyGrant(
            policy=policy, grantor=bug_target_parent.owner, grantee=person
        )
        self._test_hide_comment(person)
