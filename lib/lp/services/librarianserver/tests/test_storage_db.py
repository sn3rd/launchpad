# Copyright 2009-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import hashlib
import os.path

import transaction
from fixtures import TempDir
from testtools.testcase import ExpectedException
from testtools.twistedsupport import AsynchronousDeferredRunTest
from twisted.internet import defer

from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import flush_database_updates
from lp.services.features.testing import FeatureFixture
from lp.services.librarian.model import LibraryFileContent
from lp.services.librarianserver import db, swift
from lp.services.librarianserver.storage import (
    DigestMismatchError,
    DuplicateFileIDError,
    LibrarianStorage,
    LibraryFileUpload,
)
from lp.services.log.logger import DevNullLogger
from lp.testing import TestCase
from lp.testing.dbuser import switch_dbuser
from lp.testing.layers import LaunchpadZopelessLayer
from lp.testing.swift.fixture import SwiftFixture


class LibrarianStorageDBTests(TestCase):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        switch_dbuser("librarian")
        self.directory = self.useFixture(TempDir()).path
        self.storage = LibrarianStorage(self.directory, db.Library())

    def test_addFile(self):
        data = b"data " * 50
        digest = hashlib.sha1(data).hexdigest()
        newfile = self.storage.startAddFile("file1", len(data))
        newfile.srcDigest = digest
        newfile.append(data)
        fileid, aliasid = newfile.store()
        self.assertTrue(self.storage.hasFile(fileid))

    def test_addFiles_identical(self):
        # Start adding two files with identical data
        data = b"data " * 5000
        newfile1 = self.storage.startAddFile("file1", len(data))
        newfile2 = self.storage.startAddFile("file2", len(data))
        newfile1.append(data)
        newfile2.append(data)
        id1, alias1 = newfile1.store()
        id2, alias2 = newfile2.store()

        # Make sure we actually got an id
        self.assertNotEqual(None, id1)
        self.assertNotEqual(None, id2)

        # But they are two different ids, because we leave duplicate handling
        # to the garbage collector
        self.assertNotEqual(id1, id2)

    def test_badDigest(self):
        data = b"data " * 50
        digest = "crud"
        newfile = self.storage.startAddFile("file", len(data))
        newfile.srcDigest = digest
        newfile.append(data)
        self.assertRaises(DigestMismatchError, newfile.store)

    def test_alias(self):
        # Add a file (and so also add an alias)
        data = b"data " * 50
        newfile = self.storage.startAddFile("file1", len(data))
        newfile.mimetype = "text/unknown"
        newfile.append(data)
        fileid, aliasid = newfile.store()

        # Check that its alias has the right mimetype
        fa = self.storage.getFileAlias(aliasid, None, "/")
        self.assertEqual("text/unknown", fa.mimetype)

        # Re-add the same file, with the same name and mimetype...
        newfile2 = self.storage.startAddFile("file1", len(data))
        newfile2.mimetype = "text/unknown"
        newfile2.append(data)
        fileid2, aliasid2 = newfile2.store()

        # Verify that we didn't get back the same alias ID
        self.assertNotEqual(
            fa.id, self.storage.getFileAlias(aliasid2, None, "/").id
        )

    def test_clientProvidedDuplicateIDs(self):
        # This test checks the new behaviour specified by LibrarianTransactions
        # spec: don't create IDs in DB, but do check they don't exist.

        # Create a new file
        newfile = LibraryFileUpload(self.storage, "filename", 0)

        # Set a content ID on the file (same as would happen with a
        # client-generated ID) and store it
        newfile.contentID = 666
        newfile.store()

        newfile = LibraryFileUpload(self.storage, "filename", 0)
        newfile.contentID = 666
        self.assertRaises(DuplicateFileIDError, newfile.store)

    def test_clientProvidedDuplicateContent(self):
        # Check the new behaviour specified by LibrarianTransactions
        # spec: allow duplicate content with distinct IDs.

        content = b"some content"

        # Store a file with id 6661
        newfile1 = LibraryFileUpload(self.storage, "filename", 0)
        newfile1.contentID = 6661
        newfile1.append(content)
        fileid1, aliasid1 = newfile1.store()

        # Store second file identical to the first, with id 6662
        newfile2 = LibraryFileUpload(self.storage, "filename", 0)
        newfile2.contentID = 6662
        newfile2.append(content)
        fileid2, aliasid2 = newfile2.store()

        # Create rows in the database for these files.
        store = IStore(LibraryFileContent)
        store.add(
            LibraryFileContent(
                filesize=0,
                sha1="foo",
                md5="xx",
                sha256="xx",
                sha512="xx",
                id=6661,
            )
        )
        store.add(
            LibraryFileContent(
                filesize=0,
                sha1="foo",
                md5="xx",
                sha256="xx",
                sha512="xx",
                id=6662,
            )
        )

        flush_database_updates()
        # And no errors should have been raised!


class LibrarianStorageSwiftTests(TestCase):
    layer = LaunchpadZopelessLayer
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        switch_dbuser("librarian")
        self.swift_fixture = self.useFixture(SwiftFixture())
        self.useFixture(FeatureFixture({"librarian.swift.enabled": True}))
        self.directory = self.useFixture(TempDir()).path
        self.pushConfig("librarian_server", root=self.directory)
        self.storage = LibrarianStorage(self.directory, db.Library())
        transaction.commit()

    def copyToSwift(self, lfc_id, swift_fixture=None):
        # Copy a file to Swift.
        if swift_fixture is None:
            swift_fixture = self.swift_fixture
        path = swift.filesystem_path(lfc_id)
        swift_connection = swift_fixture.connect()
        try:
            swift._to_swift_file(
                DevNullLogger(), swift_connection, lfc_id, path
            )
        finally:
            swift_connection.close()

    def moveToSwift(self, lfc_id, swift_fixture=None):
        # Move a file to Swift so that we know it can't accidentally be
        # retrieved from the local file system.
        self.copyToSwift(lfc_id, swift_fixture=swift_fixture)
        os.unlink(swift.filesystem_path(lfc_id))

    @defer.inlineCallbacks
    def test_completed_fetch_reuses_connection(self):
        # A completed fetch returns the expected data and reuses the Swift
        # connection.
        data = b"x" * (self.storage.CHUNK_SIZE * 4 + 1)
        newfile = self.storage.startAddFile("file", len(data))
        newfile.mimetype = "text/plain"
        newfile.append(data)
        lfc_id, _ = newfile.store()
        self.moveToSwift(lfc_id)
        stream = yield self.storage.open(lfc_id)
        self.assertIsNotNone(stream)
        chunks = []
        while True:
            chunk = yield stream.read(self.storage.CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), data)
        self.assertEqual(1, len(swift.connection_pools[-1]._pool))

    @defer.inlineCallbacks
    def test_partial_fetch_does_not_reuse_connection(self):
        # If only part of a file is fetched, the Swift connection is not
        # reused.
        data = b"x" * self.storage.CHUNK_SIZE * 4
        newfile = self.storage.startAddFile("file", len(data))
        newfile.mimetype = "text/plain"
        newfile.append(data)
        lfc_id, _ = newfile.store()
        self.moveToSwift(lfc_id)
        stream = yield self.storage.open(lfc_id)
        self.assertIsNotNone(stream)
        chunk = yield stream.read(self.storage.CHUNK_SIZE)
        self.assertEqual(b"x" * self.storage.CHUNK_SIZE, chunk)
        stream.close()
        with ExpectedException(ValueError, "I/O operation on closed file"):
            yield stream.read(self.storage.CHUNK_SIZE)
        self.assertEqual(0, len(swift.connection_pools[-1]._pool))

    @defer.inlineCallbacks
    def test_fetch_with_close_at_end_does_not_reuse_connection(self):
        num_chunks = 4
        data = b"x" * self.storage.CHUNK_SIZE * num_chunks
        newfile = self.storage.startAddFile("file", len(data))
        newfile.mimetype = "text/plain"
        newfile.append(data)
        lfc_id, _ = newfile.store()
        self.moveToSwift(lfc_id)
        stream = yield self.storage.open(lfc_id)
        self.assertIsNotNone(stream)
        # Read exactly the number of chunks we expect to make up the file.
        for _ in range(num_chunks):
            chunk = yield stream.read(self.storage.CHUNK_SIZE)
            self.assertEqual(b"x" * self.storage.CHUNK_SIZE, chunk)
        # Start the read that should return an empty byte string (indicating
        # EOF), but close the stream before finishing it.  This exercises
        # the connection-reuse path in TxSwiftStream.read.
        d = stream.read(self.storage.CHUNK_SIZE)
        stream.close()
        chunk = yield d
        self.assertEqual(b"", chunk)
        # In principle we might be able to reuse the connection here, but
        # SwiftStream.close doesn't know that.
        self.assertEqual(0, len(swift.connection_pools[-1]._pool))

    @defer.inlineCallbacks
    def test_multiple_swift_instances(self):
        # If multiple Swift instances are configured, LibrarianStorage tries
        # each in turn until it finds the object.
        old_swift_fixture = self.useFixture(SwiftFixture(old_instance=True))
        # We need to push this again, since setting up SwiftFixture reloads
        # the config.
        self.pushConfig("librarian_server", root=self.directory)

        old_data = b"x" * (self.storage.CHUNK_SIZE * 4 + 1)
        old_file = self.storage.startAddFile("file1", len(old_data))
        old_file.mimetype = "text/plain"
        old_file.append(old_data)
        old_lfc_id, _ = old_file.store()
        self.moveToSwift(old_lfc_id, swift_fixture=old_swift_fixture)

        both_data = b"y" * (self.storage.CHUNK_SIZE * 4 + 1)
        both_file = self.storage.startAddFile("file2", len(both_data))
        both_file.mimetype = "text/plain"
        both_file.append(both_data)
        both_lfc_id, _ = both_file.store()
        self.copyToSwift(both_lfc_id, swift_fixture=old_swift_fixture)
        self.moveToSwift(both_lfc_id)

        new_data = b"z" * (self.storage.CHUNK_SIZE * 4 + 1)
        new_file = self.storage.startAddFile("file3", len(new_data))
        new_file.mimetype = "text/plain"
        new_file.append(new_data)
        new_lfc_id, _ = new_file.store()
        self.moveToSwift(new_lfc_id)

        old_stream = yield self.storage.open(old_lfc_id)
        self.assertIsNotNone(old_stream)
        self.assertEqual(
            swift.connection_pools[0], old_stream._connection_pool
        )
        chunks = []
        while True:
            chunk = yield old_stream.read(self.storage.CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), old_data)

        both_stream = yield self.storage.open(both_lfc_id)
        self.assertIsNotNone(both_stream)
        self.assertEqual(
            swift.connection_pools[1], both_stream._connection_pool
        )
        chunks = []
        while True:
            chunk = yield both_stream.read(self.storage.CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), both_data)

        new_stream = yield self.storage.open(new_lfc_id)
        self.assertIsNotNone(new_stream)
        self.assertEqual(
            swift.connection_pools[1], new_stream._connection_pool
        )
        chunks = []
        while True:
            chunk = yield new_stream.read(self.storage.CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), new_data)
