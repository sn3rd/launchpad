# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from email.header import Header
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import transaction
from testtools.matchers import (
    ContainsDict,
    EndsWith,
    Equals,
    Is,
    MatchesStructure,
)
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bugmessage import IBugMessage
from lp.services.compat import message_as_bytes
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import get_transaction_timestamp
from lp.services.messages.model.message import MessageChunk, MessageSet
from lp.services.messages.tests.scenarios import MessageTypeScenariosMixin
from lp.services.webapp.interfaces import OAuthPermission
from lp.testing import (
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login,
    person_logged_in,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.pages import webservice_for_person


class TestMessageSet(TestCaseWithFactory):
    """Test the methods of `MessageSet`."""

    layer = LaunchpadFunctionalLayer

    # Stick to printable non-whitespace characters from ISO-8859-1 to avoid
    # confusion.  (In particular, '\x85' and '\xa0' are whitespace
    # characters according to Unicode but not according to ASCII, and this
    # would otherwise result in different test output between Python 2 and
    # 3.)
    high_characters = b"".join(bytes((c,)) for c in range(161, 256))

    def setUp(self):
        super().setUp()
        # Testing behaviour, not permissions here.
        login("foo.bar@canonical.com")

    def createTestMessages(self):
        """Create some test messages."""
        message1 = self.factory.makeMessage()
        message2 = self.factory.makeMessage(parent=message1)
        message3 = self.factory.makeMessage(parent=message1)
        message4 = self.factory.makeMessage(parent=message2)
        return (message1, message2, message3, message4)

    def _makeMessageWithAttachment(self, filename="review.diff"):
        sender = self.factory.makePerson()
        msg = MIMEMultipart()
        msg["Message-Id"] = make_msgid("launchpad")
        msg["Date"] = formatdate()
        msg["To"] = "to@example.com"
        msg["From"] = sender.preferredemail.email
        msg["Subject"] = "Sample"
        msg.attach(MIMEText("This is the body of the email."))
        attachment = Message()
        attachment.set_payload("This is the diff, honest.")
        attachment["Content-Type"] = "text/x-diff"
        attachment["Content-Disposition"] = (
            'attachment; filename="%s"' % filename
        )
        msg.attach(attachment)
        return msg

    def test_fromEmail_keeps_attachments(self):
        """Test that the parsing of the email keeps the attachments."""
        # Build a simple multipart message with a plain text first part
        # and an text/x-diff attachment.
        msg = self._makeMessageWithAttachment()
        # Now create the message from the MessageSet.
        message = MessageSet().fromEmail(message_as_bytes(msg))
        text, diff = message.chunks
        self.assertEqual("This is the body of the email.", text.content)
        self.assertEqual("review.diff", diff.blob.filename)
        self.assertEqual("text/x-diff", diff.blob.mimetype)
        # Need to commit in order to read back out of the librarian.
        transaction.commit()
        self.assertEqual(b"This is the diff, honest.", diff.blob.read())

    def test_fromEmail_strips_attachment_paths(self):
        # Build a simple multipart message with a plain text first part
        # and an text/x-diff attachment.
        msg = self._makeMessageWithAttachment(filename="/tmp/foo/review.diff")
        # Now create the message from the MessageSet.
        message = MessageSet().fromEmail(message_as_bytes(msg))
        text, diff = message.chunks
        self.assertEqual("This is the body of the email.", text.content)
        self.assertEqual("review.diff", diff.blob.filename)
        self.assertEqual("text/x-diff", diff.blob.mimetype)
        # Need to commit in order to read back out of the librarian.
        transaction.commit()
        self.assertEqual(b"This is the diff, honest.", diff.blob.read())

    def test_fromEmail_always_creates(self):
        """Even when messages are identical, fromEmail creates a new one."""
        email = self.factory.makeEmailMessage()
        orig_message = MessageSet().fromEmail(message_as_bytes(email))
        transaction.commit()
        dupe_message = MessageSet().fromEmail(message_as_bytes(email))
        self.assertNotEqual(orig_message.id, dupe_message.id)

    def test_fromEmail_restricted_reuploads(self):
        """fromEmail will re-upload the email to the restricted librarian if
        restricted is True."""
        filealias = self.factory.makeLibraryFileAlias()
        transaction.commit()
        email = self.factory.makeEmailMessage()
        message = MessageSet().fromEmail(
            message_as_bytes(email), filealias=filealias, restricted=True
        )
        self.assertTrue(message.raw.restricted)
        self.assertNotEqual(message.raw.id, filealias.id)

    def test_fromEmail_restricted_attachments(self):
        """fromEmail creates restricted attachments correctly."""
        msg = self._makeMessageWithAttachment()
        message = MessageSet().fromEmail(
            message_as_bytes(msg), restricted=True
        )
        text, diff = message.chunks
        self.assertEqual("review.diff", diff.blob.filename)
        self.assertTrue("review.diff", diff.blob.restricted)

    def makeEncodedEmail(self, encoding_name, actual_encoding):
        email = self.factory.makeEmailMessage(body=self.high_characters)
        email.set_type("text/plain")
        email.set_charset(encoding_name)
        macroman = Header(self.high_characters, actual_encoding).encode()
        new_subject = macroman.replace(actual_encoding, encoding_name)
        email.replace_header("Subject", new_subject)
        return email

    def test_fromEmail_decodes_macintosh_encoding(self):
        """ "macintosh encoding is equivalent to MacRoman."""
        high_decoded = self.high_characters.decode("macroman")
        email = self.makeEncodedEmail("macintosh", "macroman")
        message = MessageSet().fromEmail(message_as_bytes(email))
        self.assertEqual(high_decoded, message.subject)
        self.assertEqual(high_decoded, message.text_contents)

    def test_fromEmail_decodes_booga_encoding(self):
        """ "'booga' encoding is decoded as latin-1."""
        high_decoded = self.high_characters.decode("latin-1")
        email = self.makeEncodedEmail("booga", "latin-1")
        message = MessageSet().fromEmail(message_as_bytes(email))
        self.assertEqual(high_decoded, message.subject)
        self.assertEqual(high_decoded, message.text_contents)

    def test_decode_utf8(self):
        """Test decode with a known encoding."""
        result = MessageSet.decode("\u1234".encode(), "utf-8")
        self.assertEqual("\u1234", result)

    def test_decode_macintosh(self):
        """Test decode with macintosh encoding."""
        result = MessageSet.decode(self.high_characters, "macintosh")
        self.assertEqual(self.high_characters.decode("macroman"), result)

    def test_decode_unknown_ascii(self):
        """Test decode with ascii characters in an unknown encoding."""
        result = MessageSet.decode(b"abcde", "booga")
        self.assertEqual("abcde", result)

    def test_decode_unknown_high_characters(self):
        """Test decode with non-ascii characters in an unknown encoding."""
        with self.expectedLog('Treating unknown encoding "booga" as latin-1.'):
            result = MessageSet.decode(self.high_characters, "booga")
        self.assertEqual(self.high_characters.decode("latin-1"), result)


class TestMessageEditing(MessageTypeScenariosMixin, TestCaseWithFactory):
    """Test editing scenarios for Message objects."""

    layer = DatabaseFunctionalLayer

    def assertIsMessageHistory(
        self, msg_history, msg, rev, created_at, content, deleted_at=None
    ):
        """Asserts that `msg_history` is a message history of
        `msg` with the given extra info.
        """
        self.assertThat(
            msg_history,
            MatchesStructure(
                content=Equals(content),
                revision=Equals(rev),
                message=Equals(removeSecurityProxy(msg).message),
                date_created=Equals(created_at),
                date_deleted=Equals(deleted_at),
            ),
        )

    def test_non_owner_cannot_edit_message(self):
        msg = self.makeMessage()
        someone_else = self.factory.makePerson()
        with person_logged_in(someone_else):
            self.assertRaises(Unauthorized, getattr, msg, "editContent")

    def test_msg_owner_can_edit(self):
        owner = self.factory.makePerson()
        msg = self.makeMessage(owner=owner, content="initial content")
        with person_logged_in(owner):
            msg.editContent("This is the new content")
        self.assertEqual("This is the new content", msg.text_contents)
        self.assertEqual(1, len(msg.revisions))
        self.assertIsMessageHistory(
            msg.revisions[0],
            msg,
            rev=1,
            created_at=msg.datecreated,
            content="initial content",
        )

    def test_multiple_edits_revisions(self):
        owner = self.factory.makePerson()
        msg = self.makeMessage(owner=owner, content="initial content")
        with person_logged_in(owner):
            msg.editContent("first edit")
            first_edit_date = msg.date_last_edited
        self.assertEqual("first edit", msg.text_contents)
        self.assertEqual(1, len(msg.revisions))
        self.assertIsMessageHistory(
            msg.revisions[0],
            msg,
            rev=1,
            content="initial content",
            created_at=msg.datecreated,
        )

        with person_logged_in(owner):
            msg.editContent("final form")
        self.assertEqual("final form", msg.text_contents)
        self.assertEqual(2, len(msg.revisions))

        self.assertIsMessageHistory(
            msg.revisions[0],
            msg,
            rev=1,
            content="initial content",
            created_at=msg.datecreated,
        )
        self.assertIsMessageHistory(
            msg.revisions[1],
            msg,
            rev=2,
            content="first edit",
            created_at=first_edit_date,
        )

    def test_edit_message_with_blobs(self):
        # Messages with blobs should keep the blobs untouched when the
        # content is edited.
        owner = self.factory.makePerson()
        msg = self.makeMessage(owner=owner, content="initial content")
        # The IMessage object (not the delegate one).
        raw_msg = removeSecurityProxy(msg).message

        files = [
            self.factory.makeLibraryFileAlias(db_only=True) for _ in range(2)
        ]
        store = IStore(msg)
        for seq, blob in enumerate(files):
            store.add(
                MessageChunk(message=raw_msg, sequence=seq + 2, blob=blob)
            )

        with person_logged_in(owner):
            msg.editContent("final form")
        self.assertThat(
            msg.revisions[0],
            MatchesStructure(
                content=Equals("initial content"),
                revision=Equals(1),
                message=Equals(raw_msg),
                date_created=Equals(msg.datecreated),
                date_deleted=Is(None),
            ),
        )

        # Check that current message chunks are 3: the 2 old blobs, and the
        # new text message.
        self.assertEqual(3, len(msg.chunks))
        # Make sure we avoid gaps in sequence.
        self.assertEqual([1, 2, 3], sorted(i.sequence for i in msg.chunks))
        self.assertThat(
            msg.chunks[0],
            MatchesStructure(
                content=Equals("final form"),
                sequence=Equals(1),
            ),
        )
        self.assertEqual(files, [i.blob for i in msg.chunks[1:]])

        # Check revision chunks. It should be the old text message.
        rev_chunks = msg.revisions[0].chunks
        self.assertEqual(1, len(rev_chunks))
        self.assertThat(
            rev_chunks[0],
            MatchesStructure(
                sequence=Equals(1), content=Equals("initial content")
            ),
        )

    def test_edit_message_with_no_text_content(self):
        # A message created with no text content should be editable.
        owner = self.factory.makePerson()
        msg = self.makeMessage(owner=owner, content=None)
        with person_logged_in(owner):
            msg.editContent("edit text")
        self.assertEqual("edit text", msg.text_contents)
        self.assertEqual(1, len(msg.revisions))

    def test_non_owner_cannot_delete_message(self):
        owner = self.factory.makePerson()
        msg = self.makeMessage(owner=owner, content="initial content")
        someone_else = self.factory.makePerson()
        with person_logged_in(someone_else):
            self.assertRaises(Unauthorized, getattr, msg, "deleteContent")

    def test_delete_message(self):
        owner = self.factory.makePerson()
        msg = self.makeMessage(owner=owner, content="initial content")
        with person_logged_in(owner):
            msg.editContent("new content")
        with person_logged_in(owner):
            msg.deleteContent()
        self.assertEqual("", msg.text_contents)
        self.assertEqual(0, len(msg.chunks))
        self.assertEqual(
            get_transaction_timestamp(IStore(msg)), msg.date_deleted
        )
        self.assertEqual(0, len(msg.revisions))


class TestMessageEditingAPI(MessageTypeScenariosMixin, TestCaseWithFactory):
    """Test editing scenarios for Message editing API."""

    layer = DatabaseFunctionalLayer

    def getWebservice(self, person):
        return webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

    def getMessageAPIURL(self, msg):
        with admin_logged_in():
            if IBugMessage.providedBy(msg):
                # BugMessage has a special URL mapping that uses the
                # IMessage object itself.
                return api_url(msg.message)
            else:
                return api_url(msg)

    def test_api_get_basic_structure(self):
        msg = self.makeMessage(content="some content")
        ws = self.getWebservice(self.person)
        url = self.getMessageAPIURL(msg)
        obj = ws.get(url).jsonBody()
        self.assertThat(
            obj,
            ContainsDict(
                dict(
                    revisions_collection_link=EndsWith("/revisions"),
                    date_last_edited=Is(None),
                    date_deleted=Is(None),
                    content=Equals("some content"),
                )
            ),
        )

    def test_edit_message(self):
        msg = self.makeMessage(content="initial content")
        ws = self.getWebservice(self.person)
        url = self.getMessageAPIURL(msg)
        response = ws.named_post(
            url, "editContent", new_content="the new content"
        )
        self.assertEqual(200, response.status)

        edited_obj = ws.get(url).jsonBody()
        self.assertEqual("the new content", edited_obj["content"])
        self.assertIsNone(edited_obj["date_deleted"])
        self.assertIsNotNone(edited_obj["date_last_edited"])

    def assertPermissionDeniedEditMessage(self, caller_person):
        msg = self.makeMessage(content="initial content")
        ws = self.getWebservice(caller_person)
        url = self.getMessageAPIURL(msg)
        response = ws.named_post(
            url, "editContent", new_content="the new content"
        )
        self.assertEqual(401, response.status)

        edited_obj = ws.get(url).jsonBody()
        self.assertEqual("initial content", edited_obj["content"])
        self.assertIsNone(edited_obj["date_deleted"])
        self.assertIsNone(edited_obj["date_last_edited"])

    def test_edit_message_permission_denied_for_non_owner(self):
        self.assertPermissionDeniedEditMessage(self.factory.makePerson())

    def test_edit_message_permission_denied_for_admin(self):
        self.assertPermissionDeniedEditMessage(
            self.factory.makeAdministrator()
        )

    def test_delete_message(self):
        msg = self.makeMessage(content="initial content")
        ws = self.getWebservice(self.person)
        url = self.getMessageAPIURL(msg)

        response = ws.named_post(url, "deleteContent")
        self.assertEqual(200, response.status)

        deleted_obj = ws.get(url).jsonBody()
        self.assertEqual("", deleted_obj["content"])
        self.assertIsNotNone(deleted_obj["date_deleted"])

    def test_delete_message_permission_denied_for_non_owner(self):
        msg = self.makeMessage(content="initial content")
        ws = self.getWebservice(self.factory.makePerson())
        url = self.getMessageAPIURL(msg)

        response = ws.named_post(url, "deleteContent")
        self.assertEqual(401, response.status)

        obj = ws.get(url).jsonBody()
        self.assertEqual("initial content", obj["content"])
        self.assertIsNone(obj["date_deleted"])
