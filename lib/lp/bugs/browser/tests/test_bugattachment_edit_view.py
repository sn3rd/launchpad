# Copyright 2010-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import transaction
from zope.component import getUtility

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.testing import (
    TestCaseWithFactory,
    login_admin,
    login_person,
    person_logged_in,
)
from lp.testing.layers import LaunchpadFunctionalLayer
from lp.testing.views import create_initialized_view


class TestBugAttachmentEditView(TestCaseWithFactory):
    """Tests of traversal to and access of files of bug attachments."""

    layer = LaunchpadFunctionalLayer

    CHANGE_FORM_DATA = {
        "field.title": "new description",
        "field.patch": "on",
        "field.contenttype": "application/whatever",
        "field.actions.change": "Change",
    }

    def setUp(self):
        super().setUp()
        self.bug_owner = self.factory.makePerson()
        self.registry_expert = self.factory.makePerson()
        registry = getUtility(ILaunchpadCelebrities).registry_experts
        with person_logged_in(registry.teamowner):
            registry.addMember(self.registry_expert, registry.teamowner)

        login_person(self.bug_owner)
        self.bug = self.factory.makeBug(owner=self.bug_owner)
        # Reassign the bug's default bug task to a target such that the
        # bug_target_parent is different from the immediate target.  This can
        # make a difference for some security checks.
        self.bug.default_bugtask.transitionToTarget(
            self.factory.makeDistributionSourcePackage(), self.bug_owner
        )
        self.bugattachment = self.factory.makeBugAttachment(
            bug=self.bug,
            filename="foo.diff",
            data=b"file content",
            description="attachment description",
            content_type="text/plain",
            is_patch=False,
        )
        # The Librarian server should know about the new file before
        # we start the tests.
        transaction.commit()

    def test_user_changes_their_own_attachment(self):
        login_person(self.bugattachment.message.owner)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.CHANGE_FORM_DATA
        )
        self.assertEqual("new description", self.bugattachment.title)
        self.assertTrue(self.bugattachment.is_patch)
        self.assertEqual(
            "application/whatever", self.bugattachment.libraryfile.mimetype
        )

    def test_admin_changes_any_attachment(self):
        login_admin()
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.CHANGE_FORM_DATA
        )
        self.assertEqual("new description", self.bugattachment.title)
        self.assertTrue(self.bugattachment.is_patch)
        self.assertEqual(
            "application/whatever", self.bugattachment.libraryfile.mimetype
        )

    def test_registry_expert_changes_any_attachment(self):
        login_person(self.registry_expert)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.CHANGE_FORM_DATA
        )
        self.assertEqual("new description", self.bugattachment.title)
        self.assertTrue(self.bugattachment.is_patch)
        self.assertEqual(
            "application/whatever", self.bugattachment.libraryfile.mimetype
        )

    def test_bug_target_parent_bug_supervisor_changes_any_attachment(self):
        login_admin()
        bug_supervisor = self.factory.makePerson()
        parent = self.bug.default_bugtask.bug_target_parent
        parent.bug_supervisor = bug_supervisor
        login_person(bug_supervisor)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.CHANGE_FORM_DATA
        )
        self.assertEqual("new description", self.bugattachment.title)
        self.assertTrue(self.bugattachment.is_patch)
        self.assertEqual(
            "application/whatever", self.bugattachment.libraryfile.mimetype
        )

    def test_other_user_changes_attachment_fails(self):
        random_user = self.factory.makePerson()
        login_person(random_user)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.CHANGE_FORM_DATA
        )
        self.assertEqual("attachment description", self.bugattachment.title)
        self.assertFalse(self.bugattachment.is_patch)
        self.assertEqual("text/plain", self.bugattachment.libraryfile.mimetype)

    DELETE_FORM_DATA = {
        "field.actions.delete": "Delete Attachment",
    }

    def test_delete_cannot_be_performed_by_other_users(self):
        user = self.factory.makePerson()
        login_person(user)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.DELETE_FORM_DATA
        )
        self.assertEqual(1, self.bug.attachments.count())

    def test_admin_can_delete_any_attachment(self):
        login_admin()
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.DELETE_FORM_DATA
        )
        self.assertEqual(0, self.bug.attachments.count())

    def test_registry_expert_can_delete_any_attachment(self):
        login_person(self.registry_expert)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.DELETE_FORM_DATA
        )
        self.assertEqual(0, self.bug.attachments.count())

    def test_bug_target_parent_bug_supervisor_can_delete_any_attachment(self):
        login_admin()
        bug_supervisor = self.factory.makePerson()
        parent = self.bug.default_bugtask.bug_target_parent
        parent.bug_supervisor = bug_supervisor
        login_person(bug_supervisor)
        create_initialized_view(
            self.bugattachment, name="+edit", form=self.DELETE_FORM_DATA
        )
        self.assertEqual(0, self.bug.attachments.count())

    def test_attachment_owner_can_delete_their_own_attachment(self):
        bug = self.factory.makeBug(owner=self.bug_owner)
        another_user = self.factory.makePerson()
        attachment = self.factory.makeBugAttachment(
            bug=bug,
            owner=another_user,
            filename="foo.diff",
            data=b"the file content",
            description="some file",
            content_type="text/plain",
            is_patch=False,
        )

        login_person(another_user)
        create_initialized_view(
            attachment, name="+edit", form=self.DELETE_FORM_DATA
        )
        self.assertEqual(0, bug.attachments.count())
