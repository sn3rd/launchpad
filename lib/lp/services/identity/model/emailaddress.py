# Copyright 2009-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "EmailAddress",
    "EmailAddressSet",
    "HasOwnerMixin",
    "UndeletableEmailAddress",
]


import hashlib
import operator

from storm.locals import Int, Reference, Unicode
from zope.interface import implementer

from lp.app.validators.email import valid_email
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.identity.interfaces.emailaddress import (
    EmailAddressAlreadyTaken,
    EmailAddressStatus,
    IEmailAddress,
    IEmailAddressSet,
    InvalidEmailAddress,
)


class HasOwnerMixin:
    """A mixing providing an 'owner' property which returns self.person.

    This is to be used on content classes who want to provide IHasOwner but
    have the owner stored in an attribute named 'person' rather than 'owner'.
    """

    owner = property(operator.attrgetter("person"))


@implementer(IEmailAddress)
class EmailAddress(StormBase, HasOwnerMixin):
    __storm_table__ = "EmailAddress"
    __storm_order__ = ["email"]

    id = Int(primary=True)
    email = Unicode(name="email", allow_none=False)
    status = DBEnum(name="status", enum=EmailAddressStatus, allow_none=False)
    person_id = Int(name="person", allow_none=True)
    person = Reference(person_id, "Person.id")

    def __init__(self, email, status, person=None):
        super().__init__()
        self.email = email
        self.status = status
        self.person = person

    def destroySelf(self):
        """See `IEmailAddress`."""
        # Import this here to avoid circular references.
        from lp.registry.interfaces.mailinglist import MailingListStatus
        from lp.registry.model.mailinglist import MailingListSubscription

        if self.status == EmailAddressStatus.PREFERRED:
            raise UndeletableEmailAddress(
                "This is a person's preferred email, so it can't be deleted."
            )
        mailing_list = self.person and self.person.mailing_list
        if (
            mailing_list is not None
            and mailing_list.status != MailingListStatus.PURGED
            and mailing_list.address == self.email
        ):
            raise UndeletableEmailAddress(
                "This is the email address of a team's mailing list, so it "
                "can't be deleted."
            )

        # XXX 2009-05-04 jamesh bug=371567: This function should not
        # be responsible for removing subscriptions, since the SSO
        # server can't write to that table.
        store = IPrimaryStore(MailingListSubscription)
        for subscription in store.find(
            MailingListSubscription, email_address=self
        ):
            store.remove(subscription)
        store.remove(self)

    @property
    def rdf_sha1(self):
        """See `IEmailAddress`."""
        return (
            hashlib.sha1(
                ("mailto:" + self.email).encode("UTF-8")
            )  # nosec B324
            .hexdigest()
            .upper()
        )


@implementer(IEmailAddressSet)
class EmailAddressSet:
    def getByPerson(self, person):
        """See `IEmailAddressSet`."""
        return (
            IStore(EmailAddress)
            .find(EmailAddress, person=person)
            .order_by(EmailAddress.email)
        )

    def getPreferredEmailForPeople(self, people):
        """See `IEmailAddressSet`."""
        return IStore(EmailAddress).find(
            EmailAddress,
            EmailAddress.status == EmailAddressStatus.PREFERRED,
            EmailAddress.person_id.is_in([person.id for person in people]),
        )

    def getByEmail(self, email):
        """See `IEmailAddressSet`."""
        return (
            IStore(EmailAddress)
            .find(
                EmailAddress,
                EmailAddress.email.lower() == email.strip().lower(),
            )
            .one()
        )

    def new(self, email, person=None, status=EmailAddressStatus.NEW):
        """See IEmailAddressSet."""
        email = email.strip()

        if not valid_email(email):
            raise InvalidEmailAddress(
                "%s is not a valid email address." % email
            )

        if self.getByEmail(email) is not None:
            raise EmailAddressAlreadyTaken(
                "The email address '%s' is already registered." % email
            )
        assert status in EmailAddressStatus.items
        assert person
        return EmailAddress(email=email, status=status, person=person)


class UndeletableEmailAddress(Exception):
    """User attempted to delete an email address which can't be deleted."""
