# Copyright 2009-2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = ["SpecificationMessage", "SpecificationMessageSet"]

from email.utils import make_msgid

from storm.locals import Bool, Int, Reference
from zope.interface import implementer

from lp.blueprints.interfaces.specificationmessage import (
    ISpecificationMessage,
    ISpecificationMessageSet,
)
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.messages.model.message import Message, MessageChunk


@implementer(ISpecificationMessage)
class SpecificationMessage(StormBase):
    """A table linking specifications and messages."""

    __storm_table__ = "SpecificationMessage"

    id = Int(primary=True)

    specification_id = Int(name="specification", allow_none=False)
    specification = Reference(specification_id, "Specification.id")

    message_id = Int(name="message", allow_none=False)
    message = Reference(message_id, "Message.id")

    visible = Bool(allow_none=False, default=True)

    def __init__(self, specification, message):
        super().__init__()
        self.specification = specification
        self.message = message


@implementer(ISpecificationMessageSet)
class SpecificationMessageSet:
    """See ISpecificationMessageSet."""

    def createMessage(self, subject, spec, owner, content=None):
        """See ISpecificationMessageSet."""
        msg = Message(
            owner=owner, rfc822msgid=make_msgid("blueprint"), subject=subject
        )
        if content is not None:
            MessageChunk(message=msg, content=content, sequence=1)
        specmessage = SpecificationMessage(specification=spec, message=msg)
        IStore(SpecificationMessage).flush()
        return specmessage

    def get(self, specmessageid):
        """See ISpecificationMessageSet."""
        return IStore(SpecificationMessage).get(
            SpecificationMessage, specmessageid
        )
