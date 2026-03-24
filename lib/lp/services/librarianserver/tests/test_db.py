# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import transaction
from fixtures import MockPatchObject
from pymacaroons import Macaroon
from testtools.testcase import ExpectedException
from testtools.twistedsupport import AsynchronousDeferredRunTest
from twisted.internet import defer
from twisted.internet.threads import deferToThread
from zope.interface import implementer

from lp.services.authserver.testing import InProcessAuthServerFixture
from lp.services.database.interfaces import IStore
from lp.services.librarian.interfaces import ILibraryFileAlias
from lp.services.librarian.model import LibraryFileContent
from lp.services.librarianserver import db
from lp.services.macaroons.interfaces import (
    NO_USER,
    BadMacaroonContext,
    IMacaroonIssuer,
)
from lp.services.macaroons.model import MacaroonIssuerBase
from lp.testing import TestCase
from lp.testing.dbuser import switch_dbuser
from lp.testing.fixture import ZopeUtilityFixture
from lp.testing.layers import LaunchpadZopelessLayer


class DBTestCase(TestCase):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        switch_dbuser("librarian")

    def test_lookupByDigest(self):
        # Create library
        library = db.Library()

        # Initially it should be empty
        self.assertEqual([], library.lookupBySHA1("deadbeef"))

        # Add a file, check it is found by lookupBySHA1
        content = library.add("deadbeef", 1234, "abababab", "babababa", "ab")
        self.assertEqual([content.id], library.lookupBySHA1("deadbeef"))

        # Add a new file with the same digest
        new_content = library.add(
            "deadbeef", 1234, "abababab", "babababa", "ab"
        )
        # Check it gets a new ID anyway
        self.assertNotEqual(content.id, new_content.id)
        # Check it is found by lookupBySHA1
        self.assertEqual(
            sorted([content.id, new_content.id]),
            sorted(library.lookupBySHA1("deadbeef")),
        )

        alias = library.addAlias(content, "file1", "text/unknown")
        self.assertEqual("file1", alias.filename)
        self.assertEqual("text/unknown", alias.mimetype)


@implementer(IMacaroonIssuer)
class FakeMacaroonIssuer(MacaroonIssuerBase):
    identifier = "test"
    _root_secret = "test"
    _verified_user = NO_USER

    def checkIssuingContext(self, context, **kwargs):
        """See `MacaroonIssuerBase`."""
        if not ILibraryFileAlias.providedBy(context):
            raise BadMacaroonContext(context)
        return context.id

    def checkVerificationContext(self, context, **kwargs):
        """See `IMacaroonIssuerBase`."""
        if not ILibraryFileAlias.providedBy(context):
            raise BadMacaroonContext(context)
        return context

    def verifyPrimaryCaveat(self, verified, caveat_value, context, **kwargs):
        """See `MacaroonIssuerBase`."""
        ok = caveat_value == str(context.id)
        if ok:
            verified.user = self._verified_user
        return ok


class TestLibrarianStuff(TestCase):
    """Tests for the librarian."""

    layer = LaunchpadZopelessLayer
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        switch_dbuser("librarian")
        self.store = IStore(LibraryFileContent)
        self.content_id = (
            db.Library().add("deadbeef", 1234, "abababab", "ba", "ba").id
        )
        self.file_content = self._getTestFileContent()
        transaction.commit()

    def _getTestFileContent(self):
        """Return the file content object that created."""
        return self.store.find(LibraryFileContent, id=self.content_id).one()

    def test_getAlias(self):
        # Library.getAlias() returns the LibrarayFileAlias for a given
        # LibraryFileAlias ID.
        library = db.Library(restricted=False)
        alias = library.getAlias(1, None, "/")
        self.assertEqual(1, alias.id)

    def test_getAlias_no_such_record(self):
        # Library.getAlias() raises a LookupError, if no record with
        # the given ID exists.
        library = db.Library(restricted=False)
        self.assertRaises(LookupError, library.getAlias, -1, None, "/")

    def test_getAlias_content_is_null(self):
        # Library.getAlias() raises a LookupError, if no content
        # record for the given alias exists.
        library = db.Library(restricted=False)
        alias = library.getAlias(1, None, "/")
        alias.content = None
        self.assertRaises(LookupError, library.getAlias, 1, None, "/")

    def test_getAlias_content_is_none(self):
        # Library.getAlias() raises a LookupError, if the matching
        # record does not reference any LibraryFileContent record.
        library = db.Library(restricted=False)
        alias = library.getAlias(1, None, "/")
        alias.content = None
        self.assertRaises(LookupError, library.getAlias, 1, None, "/")

    def test_getAlias_restricted_library_unrestricted_alias(self):
        # Library.getAlias() allows looking up unrestricted
        # LibraryFileAliases from a restricted library.
        restricted_library = db.Library(restricted=True)
        alias = restricted_library.getAlias(1, None, "/")
        self.assertEqual(1, alias.id)

    def test_getAlias_unrestricted_library_restricted_alias(self):
        # Library.getAlias() raises a LookupError if an unrestricted
        # library looks up a restricted LibraryFileAlias.
        unrestricted_library = db.Library(restricted=False)
        alias = unrestricted_library.getAlias(1, None, "/")
        alias.restricted = True
        self.assertRaises(
            LookupError, unrestricted_library.getAlias, 1, None, "/"
        )

    @defer.inlineCallbacks
    def test_getAlias_with_macaroon(self):
        # Library.getAlias() uses the authserver to verify macaroons.
        issuer = FakeMacaroonIssuer()
        self.useFixture(
            ZopeUtilityFixture(issuer, IMacaroonIssuer, name="test")
        )
        self.useFixture(InProcessAuthServerFixture())
        unrestricted_library = db.Library(restricted=False)
        alias = unrestricted_library.getAlias(1, None, "/")
        alias.restricted = True
        transaction.commit()
        restricted_library = db.Library(restricted=True)
        macaroon = issuer.issueMacaroon(alias)
        alias = yield deferToThread(
            restricted_library.getAlias, 1, macaroon, "/"
        )
        self.assertEqual(1, alias.id)

    @defer.inlineCallbacks
    def test_getAlias_with_wrong_macaroon(self):
        # A macaroon for a different LFA doesn't work.
        issuer = FakeMacaroonIssuer()
        self.useFixture(
            ZopeUtilityFixture(issuer, IMacaroonIssuer, name="test")
        )
        self.useFixture(InProcessAuthServerFixture())
        unrestricted_library = db.Library(restricted=False)
        alias = unrestricted_library.getAlias(1, None, "/")
        alias.restricted = True
        other_alias = unrestricted_library.getAlias(2, None, "/")
        transaction.commit()
        macaroon = issuer.issueMacaroon(other_alias)
        restricted_library = db.Library(restricted=True)
        with ExpectedException(LookupError):
            yield deferToThread(restricted_library.getAlias, 1, macaroon, "/")

    @defer.inlineCallbacks
    def test_getAlias_with_macaroon_timeout(self):
        # The authserver call is cancelled after a timeout period.
        unrestricted_library = db.Library(restricted=False)
        alias = unrestricted_library.getAlias(1, None, "/")
        alias.restricted = True
        transaction.commit()
        macaroon = Macaroon()
        restricted_library = db.Library(restricted=True)
        self.useFixture(
            MockPatchObject(
                restricted_library._authserver,
                "callRemote",
                return_value=defer.Deferred(),
            )
        )
        # XXX cjwatson 2018-11-01: We should use a Clock instead, but I had
        # trouble getting that working in conjunction with deferToThread.
        self.pushConfig("librarian", authentication_timeout=1)
        with ExpectedException(defer.CancelledError):
            yield deferToThread(restricted_library.getAlias, 1, macaroon, "/")

    def test_getAliases(self):
        # Library.getAliases() returns a sequence
        # [(LFA.id, LFA.filename, LFA.mimetype), ...] where LFA are
        # LibrarayFileAlias records having the given LibraryFileContent
        # ID.
        library = db.Library(restricted=False)
        aliases = library.getAliases(1)
        expected_aliases = [
            (1, "netapplet-1.0.0.tar.gz", "application/x-gtar"),
            (2, "netapplet_1.0.0.orig.tar.gz", "application/x-gtar"),
        ]
        self.assertEqual(expected_aliases, aliases)

    def test_getAliases_content_is_none(self):
        # Library.getAliases() does not return records which do not
        # reference any LibraryFileContent record.
        library = db.Library(restricted=False)
        alias = library.getAlias(1, None, "/")
        alias.content = None
        aliases = library.getAliases(1)
        expected_aliases = [
            (2, "netapplet_1.0.0.orig.tar.gz", "application/x-gtar"),
        ]
        self.assertEqual(expected_aliases, aliases)

    def test_getAliases_content_wrong_library(self):
        # Library.getAliases() does not return data from restriceded
        # LibrarayFileAlias records when called from a unrestricted
        # library and vice versa.
        unrestricted_library = db.Library(restricted=False)
        alias = unrestricted_library.getAlias(1, None, "/")
        alias.restricted = True

        aliases = unrestricted_library.getAliases(1)
        expected_aliases = [
            (2, "netapplet_1.0.0.orig.tar.gz", "application/x-gtar"),
        ]
        self.assertEqual(expected_aliases, aliases)

        restricted_library = db.Library(restricted=True)
        aliases = restricted_library.getAliases(1)
        expected_aliases = [
            (1, "netapplet-1.0.0.tar.gz", "application/x-gtar"),
        ]
        self.assertEqual(expected_aliases, aliases)
