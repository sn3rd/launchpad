# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = ["BugMessage", "BugMessageSet"]

from email.utils import make_msgid

import six
from lazr.delegates import delegate_to
from storm.properties import Int, Unicode
from storm.references import Reference
from storm.store import Store
from zope.interface import implementer

from lp.bugs.interfaces.bugmessage import IBugMessage, IBugMessageSet
from lp.registry.interfaces.person import validate_public_person
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.messages.interfaces.message import IMessage
from lp.services.messages.model.message import Message, MessageChunk


@implementer(IBugMessage)
@delegate_to(IMessage, context="message")
class BugMessage(StormBase):
    """A table linking bugs and messages."""

    __storm_table__ = "BugMessage"

    # db field names
    id = Int(primary=True)

    bug_id = Int(name="bug", allow_none=False)
    bug = Reference(bug_id, "Bug.id")

    message_id = Int(name="message", allow_none=False)
    message = Reference(message_id, "Message.id")

    bugwatch_id = Int(name="bugwatch", allow_none=True, default=None)
    bugwatch = Reference(bugwatch_id, "BugWatch.id")

    remote_comment_id = Unicode(allow_none=True, default=None)
    # -- The index of the message is cached in the DB.
    index = Int(name="index", allow_none=False)
    # -- The owner, cached from the message table using triggers.
    owner_id = Int(
        name="owner", allow_none=False, validator=validate_public_person
    )
    owner = Reference(owner_id, "Person.id")

    def __init__(
        self,
        owner=None,
        index=0,
        message=None,
        bug=None,
        bugwatch=None,
        remote_comment_id=None,
    ):
        # This is maintained by triggers to ensure validity, but we
        # also set it here to ensure it is visible to the transaction
        # creating a BugMessage.
        self.owner = message.owner
        assert (
            self.owner is not None
        ), "BugMessage's Message must have an owner"
        self.index = index
        self.message = message
        self.bug = bug
        self.remote_comment_id = (
            six.ensure_text(remote_comment_id)
            if remote_comment_id is not None
            else None
        )
        self.bugwatch = bugwatch

    def __repr__(self):
        return "<BugMessage message=%s index=%s>" % (self.message, self.index)


@implementer(IBugMessageSet)
class BugMessageSet:
    """See `IBugMessageSet`."""

    def createMessage(self, subject, bug, owner, content=None):
        """See `IBugMessageSet`."""
        msg = Message(
            parent=bug.initial_message,
            owner=owner,
            rfc822msgid=make_msgid("malone"),
            subject=subject,
        )
        if content is not None:
            MessageChunk(message=msg, content=content, sequence=1)
        bugmsg = BugMessage(
            bug=bug, message=msg, index=bug.bug_messages.count()
        )

        # XXX 2008-05-27 jamesh:
        # Ensure that BugMessages get flushed in same order as they
        # are created.
        Store.of(bugmsg).flush()
        return bugmsg

    def get(self, bugmessageid):
        """See `IBugMessageSet`."""
        store = IStore(BugMessage)
        return store.get(BugMessage, bugmessageid)

    def getByBugAndMessage(self, bug, message):
        """See`IBugMessageSet`."""
        store = IStore(BugMessage)
        return store.find(BugMessage, bug=bug, message=message).one()

    def getImportedBugMessages(self, bug):
        """See IBugMessageSet."""
        store = IStore(BugMessage)
        resultset = store.find(
            BugMessage, BugMessage.bug == bug, BugMessage.bugwatch != None
        )
        return resultset.order_by(BugMessage.id)
