# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import hashlib
import os
import shutil
import tempfile

from swiftclient import client as swiftclient
from twisted.internet import defer
from twisted.internet.threads import deferToThread
from twisted.python import log
from twisted.web.static import StaticProducer
from zope.component import getUtility

from lp.services.config import dbconfig
from lp.services.database import write_transaction
from lp.services.database.interfaces import (
    DEFAULT_FLAVOR,
    MAIN_STORE,
    IStoreSelector,
)
from lp.services.database.postgresql import ConnectionString
from lp.services.features import getFeatureFlag
from lp.services.librarianserver import swift

__all__ = [
    "DigestMismatchError",
    "LibrarianStorage",
    "LibraryFileUpload",
    "DuplicateFileIDError",
    "WrongDatabaseError",
    # _relFileLocation needed by other modules in this package.
    # Listed here to keep the import pedant happy
    "_relFileLocation",
]


def fsync_path(path, dir=False):
    fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if dir else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def makedirs_fsync(name, mode=0o777):
    """makedirs_fsync(path [, mode=0o777])

    os.makedirs, but fsyncing on the way up to ensure durability.
    """
    head, tail = os.path.split(name)
    if not tail:
        head, tail = os.path.split(head)
    if head and tail and not os.path.exists(head):
        try:
            makedirs_fsync(head, mode)
        except FileExistsError:
            pass
        if tail == os.curdir:
            return
    os.mkdir(name, mode)
    fsync_path(head, dir=True)


class DigestMismatchError(Exception):
    """The given digest doesn't match the SHA-1 digest of the file."""


class DuplicateFileIDError(Exception):
    """Given File ID already exists."""


class WrongDatabaseError(Exception):
    """The client's database name doesn't match our database."""

    def __init__(self, clientDatabaseName, serverDatabaseName):
        Exception.__init__(self, clientDatabaseName, serverDatabaseName)
        self.clientDatabaseName = clientDatabaseName
        self.serverDatabaseName = serverDatabaseName


class LibrarianStorage:
    """Blob storage.

    This manages the actual storage of files on disk and the record of those
    in the database; it has nothing to do with the network interface to those
    files.
    """

    # Class variables storing some metrics.
    swift_download_attempts = 0
    swift_download_fails = 0

    def __init__(self, directory, library):
        self.directory = directory
        self.library = library
        self.incoming = os.path.join(self.directory, "incoming")
        try:
            os.mkdir(self.incoming)
        except FileExistsError:
            pass

    def hasFile(self, fileid):
        return os.access(self._fileLocation(fileid), os.F_OK)

    CHUNK_SIZE = StaticProducer.bufferSize

    @defer.inlineCallbacks
    def open(self, fileid):
        if getFeatureFlag("librarian.swift.enabled"):
            # Log our attempt.
            self.swift_download_attempts += 1

            if self.swift_download_attempts % 1000 == 0:
                log.msg(
                    "{} Swift download attempts, {} failures".format(
                        self.swift_download_attempts, self.swift_download_fails
                    )
                )

            # First, try and stream the file from Swift.  Try the newest
            # configured instance first.
            swift_download_fail = False
            container, name = swift.swift_location(fileid)
            for connection_pool in reversed(swift.connection_pools):
                swift_connection = connection_pool.get()
                try:
                    headers, chunks = yield deferToThread(
                        swift.quiet_swiftclient,
                        swift_connection.get_object,
                        container,
                        name,
                        resp_chunk_size=self.CHUNK_SIZE,
                    )
                    return TxSwiftStream(
                        connection_pool, swift_connection, chunks
                    )
                except swiftclient.ClientException as x:
                    if x.http_status == 404:
                        connection_pool.put(swift_connection)
                    else:
                        swift_download_fail = True
                        log.err(x)
                except Exception as x:
                    swift_download_fail = True
                    log.err(x)
            if swift_download_fail:
                self.swift_download_fails += 1
            # If Swift failed, for any reason, fall through to try and
            # stream the data from disk. In particular, files cannot be
            # found in Swift until librarian-feed-swift.py has put them
            # in there.

        path = self._fileLocation(fileid)
        if os.path.exists(path):
            return open(path, "rb")

    def _fileLocation(self, fileid):
        return os.path.join(self.directory, _relFileLocation(str(fileid)))

    def startAddFile(self, filename, size):
        return LibraryFileUpload(self, filename, size)

    def getFileAlias(self, aliasid, token, path):
        return self.library.getAlias(aliasid, token, path)


class TxSwiftStream(swift.SwiftStream):
    @defer.inlineCallbacks
    def read(self, size):
        if self.closed:
            raise ValueError("I/O operation on closed file")

        if self._swift_connection is None:
            return b""  # EOF already reached, connection returned.

        if size == 0:
            return b""

        if not self._chunk:
            self._chunk = yield deferToThread(self._next_chunk)
            if not self._chunk:
                # If we have drained the data successfully,
                # the connection can be reused saving on auth
                # handshakes.
                if self._swift_connection is not None:
                    self._connection_pool.put(self._swift_connection)
                    self._swift_connection = None
                self._chunks = None
                return b""
        return_chunk = self._chunk[:size]
        self._chunk = self._chunk[size:]

        self._offset += len(return_chunk)
        return return_chunk


class LibraryFileUpload:
    """A file upload from a client."""

    srcDigest = None
    mimetype = "unknown/unknown"
    contentID = None
    aliasID = None
    expires = None
    databaseName = None
    debugID = None

    def __init__(self, storage, filename, size):
        self.storage = storage
        self.filename = filename
        self.size = size
        self.debugLog = []

        # Create temporary file
        tmpfile, tmpfilepath = tempfile.mkstemp(dir=self.storage.incoming)
        self.tmpfile = os.fdopen(tmpfile, "wb")
        self.tmpfilepath = tmpfilepath
        self.md5_digester = hashlib.md5()  # nosec B324
        self.sha1_digester = hashlib.sha1()  # nosec B324
        self.sha256_digester = hashlib.sha256()
        self.sha512_digester = hashlib.sha512()

    def append(self, data):
        self.tmpfile.write(data)
        self.md5_digester.update(data)
        self.sha1_digester.update(data)
        self.sha256_digester.update(data)
        self.sha512_digester.update(data)

    @write_transaction
    def store(self):
        self.debugLog.append(
            "storing %r, size %r" % (self.filename, self.size)
        )
        self.tmpfile.close()

        # Verify the digest matches what the client sent us
        dstDigest = self.sha1_digester.hexdigest()
        if self.srcDigest is not None and dstDigest != self.srcDigest:
            # XXX: Andrew Bennetts 2004-09-20: Write test that checks that
            # the file really is removed or renamed, and can't possibly be
            # left in limbo
            os.remove(self.tmpfilepath)
            raise DigestMismatchError(self.srcDigest, dstDigest)

        try:
            # If the client told us the name of the database it's using,
            # check that it matches.
            if self.databaseName is not None:
                # Per Bug #840068, there are two methods of getting the
                # database name (connection string and db
                # introspection), and they can give different results
                # due to pgbouncer database aliases. Lets check both,
                # and succeed if either matches.
                config_dbname = ConnectionString(
                    dbconfig.rw_main_primary
                ).dbname

                store = getUtility(IStoreSelector).get(
                    MAIN_STORE, DEFAULT_FLAVOR
                )
                result = store.execute("SELECT current_database()")
                real_dbname = result.get_one()[0]
                if self.databaseName not in (config_dbname, real_dbname):
                    raise WrongDatabaseError(
                        self.databaseName, (config_dbname, real_dbname)
                    )

            self.debugLog.append("database name %r ok" % (self.databaseName,))
            # If we haven't got a contentID, we need to create one and return
            # it to the client.
            if self.contentID is None:
                content = self.storage.library.add(
                    dstDigest,
                    self.size,
                    self.md5_digester.hexdigest(),
                    self.sha256_digester.hexdigest(),
                    self.sha512_digester.hexdigest(),
                )
                contentID = content.id
                aliasID = self.storage.library.addAlias(
                    content, self.filename, self.mimetype, self.expires
                ).id
                self.debugLog.append(
                    "created contentID: %r, aliasID: %r."
                    % (contentID, aliasID)
                )
            else:
                contentID = self.contentID
                aliasID = None
                self.debugLog.append("received contentID: %r" % (contentID,))

        except Exception:
            # Abort transaction and re-raise
            self.debugLog.append("failed to get contentID/aliasID, aborting")
            raise

        # Move file to final location
        try:
            self._move(contentID)
        except Exception:
            # Abort DB transaction
            self.debugLog.append("failed to move file, aborting")

            # Remove file
            os.remove(self.tmpfilepath)

            # Re-raise
            raise

        # Commit any DB changes
        self.debugLog.append("committed")

        # Return the IDs if we created them, or None otherwise
        return contentID, aliasID

    def _move(self, fileID):
        location = self.storage._fileLocation(fileID)
        if os.path.exists(location):
            raise DuplicateFileIDError(fileID)
        try:
            makedirs_fsync(os.path.dirname(location))
        except FileExistsError:
            # If the directory already exists, that's ok.
            pass
        shutil.move(self.tmpfilepath, location)
        fsync_path(location)
        fsync_path(os.path.dirname(location), dir=True)


def _relFileLocation(file_id):
    """Return the relative location for the given file_id.

    The relative location is obtained by converting file_id into a 8-digit hex
    and then splitting it across four path segments.
    """
    file_id = int(file_id)
    assert (
        file_id <= 4294967295
    ), f"file id {file_id!r} has exceeded filesystem db maximum"
    h = "%08x" % file_id
    return "%s/%s/%s/%s" % (h[:2], h[2:4], h[4:6], h[6:])
