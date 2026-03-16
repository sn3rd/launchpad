# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "DirectEmailAuthorization",
    "Message",
    "MessageChunk",
    "MessageSet",
    "UserToUserEmail",
]

import email
import logging
import os.path
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import make_msgid, mktime_tz, parseaddr, parsedate_tz
from io import BytesIO
from operator import attrgetter

import six
from lazr.config import as_timedelta
from storm.locals import (
    And,
    Bool,
    DateTime,
    Int,
    Max,
    Reference,
    ReferenceSet,
    Store,
    Unicode,
)
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import isinstance as zisinstance

from lp.app.errors import NotFoundError
from lp.registry.interfaces.person import (
    IPersonSet,
    PersonCreationRationale,
    validate_public_person,
)
from lp.services.config import config
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.messages.interfaces.message import (
    IDirectEmailAuthorization,
    IMessage,
    IMessageChunk,
    IMessageSet,
    InvalidEmailMessage,
    IUserToUserEmail,
    UnknownSender,
)
from lp.services.messages.model.messagerevision import (
    MessageRevision,
    MessageRevisionChunk,
)
from lp.services.propertycache import cachedproperty, get_property_cache


def utcdatetime_from_field(field_value):
    """Turn an RFC 2822 Date: header value into a Python datetime (UTC).

    :param field_value: The value of the Date: header
    :type field_value: string
    :return: The corresponding datetime (UTC)
    :rtype: `datetime.datetime`
    :raise `InvalidEmailMessage`: when the date string cannot be converted.
    """
    try:
        date_tuple = parsedate_tz(field_value)
        timestamp = mktime_tz(date_tuple)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise InvalidEmailMessage("Invalid date %s" % field_value)


@implementer(IMessage)
class Message(StormBase):
    """A message. This is an RFC822-style message, typically it would be
    coming into the bug system, or coming in from a mailing list.
    """

    __storm_table__ = "Message"
    __storm_order__ = "-id"
    id = Int(primary=True)
    datecreated = DateTime(
        allow_none=False, default=UTC_NOW, tzinfo=timezone.utc
    )
    date_deleted = DateTime(allow_none=True, default=None, tzinfo=timezone.utc)
    date_last_edited = DateTime(
        allow_none=True, default=None, tzinfo=timezone.utc
    )
    subject = Unicode(allow_none=True, default=None)
    owner_id = Int(
        name="owner", validator=validate_public_person, allow_none=True
    )
    owner = Reference(owner_id, "Person.id")
    parent_id = Int(name="parent", allow_none=True, default=None)
    parent = Reference(parent_id, "Message.id")
    rfc822msgid = Unicode(allow_none=False)
    bugs = ReferenceSet(
        "id", "BugMessage.message_id", "BugMessage.bug_id", "Bug.id"
    )
    _chunks = ReferenceSet("id", "MessageChunk.message_id")

    @cachedproperty
    def chunks(self):
        return list(self._chunks)

    raw_id = Int(name="raw", default=None)
    raw = Reference(raw_id, "LibraryFileAlias.id")
    _bugattachments = ReferenceSet("id", "BugAttachment._message_id")

    @cachedproperty
    def bugattachments(self):
        return list(self._bugattachments)

    visible = Bool(allow_none=False, default=True)

    def __init__(
        self,
        rfc822msgid,
        datecreated=DEFAULT,
        subject=None,
        owner=None,
        parent=None,
        raw=None,
    ):
        super().__init__()
        self.rfc822msgid = rfc822msgid
        self.datecreated = datecreated
        self.subject = subject
        self.owner = owner
        self.parent = parent
        self.raw = raw

    def __repr__(self):
        return "<Message id=%s>" % self.id

    def __iter__(self):
        """See IMessage.__iter__"""
        return iter(self.chunks)

    def setVisible(self, visible):
        self.visible = visible

    @property
    def title(self):
        """See IMessage."""
        return self.subject

    @property
    def sender(self):
        """See IMessage."""
        return self.owner

    @cachedproperty
    def text_contents(self):
        """See IMessage."""
        return Message.chunks_text(self.chunks)

    @classmethod
    def chunks_text(cls, chunks):
        bits = [str(chunk) for chunk in chunks if chunk.content]
        return "\n\n".join(bits)

    # XXX flacoste 2006-09-08: Bogus attribute only present so that
    # verifyObject doesn't fail. That attribute is part of the
    # interface because it is used as a UI field in MessageAddView
    content = None

    def getAPIParent(self):
        """See `IMessage`."""
        return None

    @property
    def _revisions(self):
        return (
            Store.of(self)
            .find(MessageRevision, MessageRevision.message == self)
            .order_by(MessageRevision.revision)
        )

    @cachedproperty
    def revisions(self):
        """See `IMessage`."""
        return list(self._revisions)

    def getRevisionByNumber(self, revision_number):
        return self._revisions.find(revision=revision_number).one()

    def editContent(self, new_content):
        """See `IMessage`."""
        store = Store.of(self)

        # Move the old content to a new revision.
        date_created = (
            self.date_last_edited
            if self.date_last_edited is not None
            else self.datecreated
        )
        current_rev_num = store.find(
            Max(MessageRevision.revision), MessageRevision.message == self
        ).one()
        rev_num = (current_rev_num or 0) + 1
        rev = MessageRevision(
            message=self, revision=rev_num, date_created=date_created
        )
        self.date_last_edited = UTC_NOW
        store.add(rev)

        # Move the current text content to the recently created revision.
        used_seq_numbers = set()
        for chunk in self._chunks:
            if chunk.blob is None:
                if chunk.content is not None:
                    revision_chunk = MessageRevisionChunk(
                        rev, chunk.sequence, chunk.content
                    )
                    store.add(revision_chunk)
                store.remove(chunk)
            else:
                used_seq_numbers.add(chunk.sequence)

        # Spot sequence number gaps.
        # If there is a gap in sequence numbers, use it. Otherwise, use the
        # max sequence number + 1.
        min_gap = None
        for i in range(1, len(used_seq_numbers) + 1):
            if i not in used_seq_numbers:
                min_gap = i
                break
        if min_gap is None:
            new_seq = max(used_seq_numbers) + 1 if len(used_seq_numbers) else 1
        else:
            new_seq = min_gap

        # Create the new content.
        new_chunk = MessageChunk(
            message=self, sequence=new_seq, content=new_content
        )
        store.add(new_chunk)

        store.flush()

        # Clean up caches.
        del get_property_cache(self).text_contents
        del get_property_cache(self).chunks
        del get_property_cache(self).revisions

    def deleteContent(self):
        """See `IMessage`."""
        store = Store.of(self)
        store.find(MessageChunk, MessageChunk.message == self).remove()
        revs = [i.id for i in self.revisions]
        store.find(
            MessageRevisionChunk,
            MessageRevisionChunk.message_revision_id.is_in(revs),
        ).remove()
        store.find(MessageRevision, MessageRevision.message == self).remove()
        del get_property_cache(self).text_contents
        del get_property_cache(self).chunks
        del get_property_cache(self).revisions
        self.date_deleted = UTC_NOW


def get_parent_msgids(parsed_message):
    """Returns a list of message ids the mail was a reply to.

    >>> get_parent_msgids({"In-Reply-To": "<msgid1>"})
    ['<msgid1>']

    >>> get_parent_msgids({"References": "<msgid1> <msgid2>"})
    ['<msgid1>', '<msgid2>']

    >>> get_parent_msgids({"In-Reply-To": "<msgid1> <msgid2>"})
    ['<msgid1>', '<msgid2>']

    >>> get_parent_msgids({"In-Reply-To": "", "References": ""})
    []

    >>> get_parent_msgids({})
    []
    """
    for name in ["In-Reply-To", "References"]:
        if name in parsed_message:
            return parsed_message.get(name).split()

    return []


@implementer(IMessageSet)
class MessageSet:
    extra_encoding_aliases = {
        "macintosh": "mac_roman",
    }

    def get(self, rfc822msgid):
        messages = list(IStore(Message).find(Message, rfc822msgid=rfc822msgid))
        if len(messages) == 0:
            raise NotFoundError(rfc822msgid)
        return messages

    def fromText(
        self, subject, content, owner=None, datecreated=None, rfc822msgid=None
    ):
        """See IMessageSet."""
        if datecreated is None:
            datecreated = UTC_NOW
        if rfc822msgid is None:
            rfc822msgid = make_msgid("launchpad")

        message = Message(
            subject=subject,
            rfc822msgid=rfc822msgid,
            owner=owner,
            datecreated=datecreated,
        )
        IStore(Message).add(message)
        if content is not None:
            MessageChunk(message=message, sequence=1, content=content)
        # XXX 2008-05-27 jamesh:
        # Ensure that BugMessages get flushed in same order as they
        # are created.
        Store.of(message).flush()
        return message

    @classmethod
    def decode(self, encoded, encoding):
        encoding = self.extra_encoding_aliases.get(encoding, encoding)
        try:
            return encoded.decode(encoding, "replace")
        except LookupError:
            try:
                return encoded.decode("us-ascii")
            except UnicodeDecodeError:
                logging.getLogger().warning(
                    'Treating unknown encoding "%s" as latin-1.' % encoding
                )
                return encoded.decode("latin-1")

    def _decode_header(self, header):
        r"""Decode an RFC 2047 encoded header.

            >>> MessageSet()._decode_header("=?iso-8859-1?q?F=F6=F6_b=E4r?=")
            u'F\xf6\xf6 b\xe4r'

        If the header isn't encoded properly, the characters that can't
        be decoded are replaced with unicode question marks.

            >>> MessageSet()._decode_header("=?utf-8?q?F=F6?=")
            u'F\ufffd'
        """
        # Unfold the header before decoding it.
        header = "".join(header.splitlines())

        bits = decode_header(header)
        # Re-encode the header parts using utf-8, replacing undecodable
        # characters with question marks.
        re_encoded_bits = []
        for word, charset in bits:
            # 2008-09-26 gary:
            # The RFC 2047 encoding names and the Python encoding names are
            # not always the same. A safer and more correct approach would use
            #   word.decode(email.charset.Charset(charset).input_codec,
            #               'replace')
            # or similar, rather than
            #   word.decode(charset, 'replace')
            # That said, this has not bitten us so far, and is only likely to
            # cause problems in unusual encodings that we are hopefully
            # unlikely to encounter in this part of the code.
            decoded = word if charset is None else self.decode(word, charset)
            re_encoded_bits.append((decoded.encode("utf-8"), "utf-8"))

        return str(make_header(re_encoded_bits))

    def fromEmail(
        self,
        email_message,
        owner=None,
        filealias=None,
        parsed_message=None,
        create_missing_persons=False,
        fallback_parent=None,
        date_created=None,
        restricted=False,
    ):
        """See IMessageSet.fromEmail."""
        # It does not make sense to handle Unicode strings, as email
        # messages may contain chunks encoded in differing character sets.
        # Passing Unicode in here indicates a bug.
        if not zisinstance(email_message, bytes):
            raise TypeError(
                "email_message must be a byte string.  Got: %r" % email_message
            )

        # Parse the raw message into an email.message.Message instance,
        # if we haven't been given one already.
        if parsed_message is None:
            parsed_message = email.message_from_bytes(email_message)

        # We could easily generate a default, but a missing message-id
        # almost certainly means a developer is using this method when
        # they shouldn't (by creating emails by hand and passing them here),
        # which is broken because they will almost certainly have Unicode
        # errors.
        rfc822msgid = parsed_message.get("message-id")
        if not rfc822msgid:
            raise InvalidEmailMessage("Missing Message-Id")

        # Over-long messages are checked for at the handle_on_message level.

        # If it's a restricted mail (IE: for a private bug), or it hasn't been
        # uploaded, do so now.
        from lp.services.mail.helpers import save_mail_to_librarian

        if restricted or filealias is None:
            raw_email_message = save_mail_to_librarian(
                email_message, restricted=restricted
            )
        else:
            raw_email_message = filealias

        # Find the message subject
        subject = self._decode_header(parsed_message.get("subject", ""))
        subject = subject.strip()

        if owner is None:
            # Try and determine the owner. We raise a NotFoundError
            # if the sender does not exist, unless we were asked to
            # create_missing_persons.
            person_set = getUtility(IPersonSet)
            from_hdr = self._decode_header(
                parsed_message.get("from", "")
            ).strip()
            replyto_hdr = self._decode_header(
                parsed_message.get("reply-to", "")
            ).strip()
            from_addrs = [from_hdr, replyto_hdr]
            from_addrs = [parseaddr(addr) for addr in from_addrs if addr]
            if len(from_addrs) == 0:
                raise InvalidEmailMessage("No From: or Reply-To: header")
            for from_addr in from_addrs:
                owner = person_set.getByEmail(from_addr[1].lower().strip())
                if owner is not None:
                    break
            if owner is None:
                if not create_missing_persons:
                    raise UnknownSender(from_addrs[0][1])
                # autocreate a person
                sendername = six.ensure_text(from_addrs[0][0].strip())
                senderemail = from_addrs[0][1].lower().strip()
                # XXX: Guilherme Salgado 2006-08-31 bug=62344:
                # It's hard to define what rationale to use here, and to
                # make things worst, it's almost impossible to provide a
                # meaningful comment having only the email message.
                owner = person_set.ensurePerson(
                    senderemail,
                    sendername,
                    PersonCreationRationale.FROMEMAILMESSAGE,
                )
                if owner is None:
                    raise UnknownSender(senderemail)

        # Get the parent of the message, if available in the db. We'll
        # go through all the message's parents until we find one that's
        # in the db.
        parent = None
        for parent_msgid in reversed(get_parent_msgids(parsed_message)):
            try:
                # we assume it's the first matching message
                parent = self.get(parent_msgid)[0]
                break
            except NotFoundError:
                pass
        else:
            parent = fallback_parent

        # Figure out the date of the message.
        if date_created is not None:
            datecreated = date_created
        else:
            datecreated = utcdatetime_from_field(parsed_message["date"])

        # Make sure we don't create an email with a datecreated in the
        # distant past or future.
        now = datetime.now(timezone.utc)
        thedistantpast = datetime(1990, 1, 1, tzinfo=timezone.utc)
        if datecreated < thedistantpast or datecreated > now:
            datecreated = UTC_NOW

        message = Message(
            subject=subject,
            owner=owner,
            rfc822msgid=rfc822msgid,
            parent=parent,
            raw=raw_email_message,
            datecreated=datecreated,
        )

        sequence = 1

        # Don't store the preamble or epilogue -- they are only there
        # to give hints to non-MIME aware clients
        #
        # Determine the encoding to use for non-multipart messages, and the
        # preamble and epilogue of multipart messages. We default to
        # iso-8859-1 as it seems fairly harmless to cope with old, broken
        # mail clients (The RFCs state US-ASCII as the default character
        # set).
        # default_charset = (parsed_message.get_content_charset() or
        #                    'iso-8859-1')
        #
        # XXX: kiko 2005-09-23: Is default_charset only useful here?
        #
        # if getattr(parsed_message, 'preamble', None):
        #     # We strip a leading and trailing newline - the email parser
        #     # seems to arbitrarily add them :-/
        #     preamble = parsed_message.preamble.decode(
        #             default_charset, 'replace')
        #     if preamble.strip():
        #         if preamble[0] == '\n':
        #             preamble = preamble[1:]
        #         if preamble[-1] == '\n':
        #             preamble = preamble[:-1]
        #         MessageChunk(
        #             message=message, sequence=sequence, content=preamble
        #             )
        #         sequence += 1

        for part in parsed_message.walk():
            mime_type = part.get_content_type()

            # Skip the multipart section that walk gives us. This part
            # is the entire message.
            if part.is_multipart():
                continue

            # Decode the content of this part.
            content = part.get_payload(decode=True)

            # Store the part as a MessageChunk
            #
            # We want only the content type text/plain as "main content".
            # Exceptions to this rule:
            # - if the content disposition header explicitly says that
            #   this part is an attachment, text/plain content is stored
            #   as a blob,
            # - if the content-disposition header provides a filename,
            #   text/plain content is stored as a blob.
            content_disposition = part.get("Content-disposition", "").lower()
            no_attachment = not content_disposition.startswith("attachment")
            if (
                mime_type == "text/plain"
                and no_attachment
                and part.get_filename() is None
            ):
                # Get the charset for the message part. If one isn't
                # specified, default to latin-1 to prevent
                # UnicodeDecodeErrors.
                charset = part.get_content_charset()
                if charset is None or str(charset).lower() == "x-unknown":
                    charset = "latin-1"
                content = self.decode(content, charset)

                if content.strip():
                    MessageChunk(
                        message=message, sequence=sequence, content=content
                    )
                    sequence += 1
            else:
                filename = part.get_filename() or "unnamed"
                # Strip off any path information.
                filename = os.path.basename(filename)
                # Note we use the Content-Type header instead of
                # part.get_content_type() here to ensure we keep
                # parameters as sent. If Content-Type is None we default
                # to application/octet-stream.
                if part["content-type"] is None:
                    content_type = "application/octet-stream"
                else:
                    content_type = part["content-type"]

                if len(content) > 0:
                    blob = getUtility(ILibraryFileAliasSet).create(
                        name=filename,
                        size=len(content),
                        file=BytesIO(content),
                        contentType=content_type,
                        restricted=restricted,
                    )
                    MessageChunk(message=message, sequence=sequence, blob=blob)
                    sequence += 1

        # Don't store the epilogue
        # if getattr(parsed_message, 'epilogue', None):
        #     epilogue = parsed_message.epilogue.decode(
        #             default_charset, 'replace')
        #     if epilogue.strip():
        #         if epilogue[0] == '\n':
        #             epilogue = epilogue[1:]
        #         if epilogue[-1] == '\n':
        #             epilogue = epilogue[:-1]
        #         MessageChunk(
        #             message=message, sequence=sequence, content=epilogue
        #             )

        # XXX 2008-05-27 jamesh:
        # Ensure that BugMessages get flushed in same order as they
        # are created.
        Store.of(message).flush()
        return message


@implementer(IMessageChunk)
class MessageChunk(StormBase):
    """One part of a possibly multipart Message"""

    __storm_table__ = "MessageChunk"
    __storm_order__ = "sequence"

    id = Int(primary=True)

    message_id = Int(name="message", allow_none=False)
    message = Reference(message_id, "Message.id")

    sequence = Int(allow_none=False)

    content = Unicode(allow_none=True, default=None)

    blob_id = Int(name="blob", allow_none=True, default=None)
    blob = Reference(blob_id, "LibraryFileAlias.id")

    def __init__(self, message, sequence, content=None, blob=None):
        super().__init__()
        self.message = message
        self.sequence = sequence
        self.content = content
        self.blob = blob

    def __str__(self):
        """Return a text representation of this chunk.

        This is either the content, or a link to the blob in a format
        suitable for use in a text only environment, such as an email
        """
        if self.content:
            return self.content
        else:
            blob = self.blob
            return (
                "Attachment: %s\n"
                "Type:       %s\n"
                "URL:        %s" % (blob.filename, blob.mimetype, blob.url)
            )


@implementer(IUserToUserEmail)
class UserToUserEmail(StormBase):
    """See `IUserToUserEmail`."""

    __storm_table__ = "UserToUserEmail"

    id = Int(primary=True)

    sender_id = Int(name="sender")
    sender = Reference(sender_id, "Person.id")

    recipient_id = Int(name="recipient")
    recipient = Reference(recipient_id, "Person.id")

    date_sent = DateTime(allow_none=False)

    subject = Unicode(allow_none=False)

    message_id = Unicode(allow_none=False)

    def __init__(self, message):
        """Create a new user-to-user email entry.

        :param message: the message being sent
        :type message: `email.message.Message`
        """
        super().__init__()
        person_set = getUtility(IPersonSet)
        # Find the person who is sending this message.
        realname, address = parseaddr(message["from"])
        assert address, "Message has no From: field"
        sender = person_set.getByEmail(address)
        assert sender is not None, "No person for sender email: %s" % address
        # Find the person who is the recipient.
        realname, address = parseaddr(message["to"])
        assert address, "Message has no To: field"
        recipient = person_set.getByEmail(address)
        assert recipient is not None, (
            "No person for recipient email: %s" % address
        )
        # Convert the date string into a UTC datetime.
        date = message["date"]
        assert date is not None, "Message has no Date: field"
        self.date_sent = utcdatetime_from_field(date)
        # Find the subject and message-id.
        message_id = message["message-id"]
        assert message_id is not None, "Message has no Message-ID: field"
        subject = message["subject"]
        assert subject is not None, "Message has no Subject: field"
        # Initialize.
        self.sender = sender
        self.recipient = recipient
        self.message_id = six.ensure_text(message_id, "ascii")
        self.subject = str(make_header(decode_header(subject)))
        # Add the object to the store of the sender.  Our StormMigrationGuide
        # recommends against this saying "Note that the constructor should not
        # usually add the object to a store -- leave that for a FooSet.new()
        # method, or let it be inferred by a relation."
        #
        # On the other hand, we really don't need a UserToUserEmailSet for any
        # other purpose.  There isn't any other relationship that can be
        # inferred, so in this case I think it makes fine sense for the
        # constructor to add self to the store.
        Store.of(sender).add(self)


@implementer(IDirectEmailAuthorization)
class DirectEmailAuthorization:
    """See `IDirectEmailAuthorization`."""

    def __init__(self, sender):
        """Create a `UserContactBy` instance.

        :param sender: The sender we're checking.
        :type sender: `IPerson`
        :param after: The cutoff date for throttling.  Primarily used only for
            testing purposes.
        :type after: `datetime.datetime`
        """
        self.sender = sender

    def _getThrottlers(self, after):
        """Return a result set of entries affecting throttling decisions.

        :param after: Explicit cut off date.
        :type after: `datetime.datetime`
        """
        return Store.of(self.sender).find(
            UserToUserEmail,
            And(
                UserToUserEmail.sender == self.sender,
                UserToUserEmail.date_sent >= after,
            ),
        )

    def _isAllowedAfter(self, after):
        """Like .is_allowed but used with an explicit cutoff date.

        For testing purposes only.

        :param after: Explicit cut off date.
        :type after: `datetime.datetime`
        :return: True if email is allowed
        :rtype: bool
        """
        # Count the number of messages from the sender since the throttle
        # date.
        messages_sent = self._getThrottlers(after).count()
        return messages_sent < config.launchpad.user_to_user_max_messages

    @property
    def is_allowed(self):
        """See `IDirectEmailAuthorization`."""
        # Users are only allowed to send X number of messages in a certain
        # period of time.  Both the number of messages and the time period
        # are configurable.
        now = datetime.now(timezone.utc)
        after = now - as_timedelta(
            config.launchpad.user_to_user_throttle_interval
        )
        return self._isAllowedAfter(after)

    @property
    def throttle_date(self):
        """See `IDirectEmailAuthorization`."""
        now = datetime.now(timezone.utc)
        after = now - as_timedelta(
            config.launchpad.user_to_user_throttle_interval
        )
        throttlers = self._getThrottlers(after)
        # We now have the set of emails that would throttle delivery.  If the
        # configuration variable has changed, this could produce more or less
        # than the now-allowed number of throttlers.  We should never get here
        # if it's less because the contact would have been allowed.
        #
        # If it's more, then we really want to count back from the sorted end,
        # because when /that/ contact record expires, they'll be able to
        # resend.  Here are two examples.
        #
        # affecters = A B C
        # max allowed = 3
        # index = len(affecters) - 3 == 0 == A
        # when A's date < the interval, they can try again
        #
        # affecters = A B C D E F G
        # max allowed (now) = 3
        # index = len(affecters) - 3 = 4 == E (counting from zero)
        # when E's date < than the interval, they can try again
        affecters = sorted(throttlers, key=attrgetter("date_sent"))
        max_throttlers = config.launchpad.user_to_user_max_messages
        expiry = len(affecters) - max_throttlers
        if expiry < 0:
            # There were fewer affecters than are now allowed, so they can
            # retry immediately.  Remember that the caller adds the interval
            # back, so this would give us 'now'.
            return after
        return affecters[expiry].date_sent

    @property
    def message_quota(self):
        """See `IDirectEmailAuthorization`."""
        return config.launchpad.user_to_user_max_messages

    def record(self, message):
        """See `IDirectEmailAuthorization`."""
        contact = UserToUserEmail(message)
        Store.of(self.sender).add(contact)
