# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "LibraryFileAlias",
    "LibraryFileAliasWithParent",
    "LibraryFileAliasSet",
    "LibraryFileContent",
    "LibraryFileDownloadCount",
    "TimeLimitedToken",
]

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

from lazr.delegates import delegate_to
from storm.locals import (
    Bool,
    Date,
    DateTime,
    Desc,
    Int,
    Reference,
    Store,
    Unicode,
)
from zope.component import adapter, getUtility
from zope.interface import Interface, implementer

from lp.app.errors import NotFoundError
from lp.registry.errors import InvalidFilename
from lp.services.config import config
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.sqlbase import session_store
from lp.services.database.stormbase import StormBase
from lp.services.librarian.interfaces import (
    ILibraryFileAlias,
    ILibraryFileAliasSet,
    ILibraryFileAliasWithParent,
    ILibraryFileContent,
    ILibraryFileDownloadCount,
)
from lp.services.librarian.interfaces.client import (
    LIBRARIAN_SERVER_DEFAULT_TIMEOUT,
    DownloadFailed,
    ILibrarianClient,
    IRestrictedLibrarianClient,
)
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.tokens import create_token


@implementer(ILibraryFileContent)
class LibraryFileContent(StormBase):
    """A pointer to file content in the librarian."""

    __storm_table__ = "LibraryFileContent"

    id = Int(primary=True)
    datecreated = DateTime(
        allow_none=False, default=UTC_NOW, tzinfo=timezone.utc
    )
    filesize = Int(allow_none=False)
    sha512 = Unicode()
    sha256 = Unicode()
    sha1 = Unicode(allow_none=False)
    md5 = Unicode(allow_none=False)

    def __init__(self, filesize, md5, sha1, sha256, id=None, sha512=None):
        super().__init__()
        if id is not None:
            self.id = id
        self.filesize = filesize
        self.md5 = md5
        self.sha1 = sha1
        self.sha256 = sha256
        self.sha512 = sha512


@implementer(ILibraryFileAlias)
class LibraryFileAlias(StormBase):
    """A filename and mimetype that we can serve some given content with."""

    __storm_table__ = "LibraryFileAlias"

    id = Int(primary=True)
    date_created = DateTime(
        allow_none=True, default=DEFAULT, tzinfo=timezone.utc
    )
    content_id = Int(name="content", allow_none=True)
    content = Reference(content_id, "LibraryFileContent.id")
    filename = Unicode(allow_none=False)
    mimetype = Unicode(allow_none=False)
    expires = DateTime(allow_none=True, default=None, tzinfo=timezone.utc)
    restricted = Bool(allow_none=False, default=False)
    hits = Int(allow_none=False, default=0)

    def __init__(
        self,
        content,
        filename,
        mimetype,
        id=None,
        expires=None,
        restricted=False,
    ):
        super().__init__()
        if id is not None:
            self.id = id
        self.content = content
        self.filename = filename
        self.mimetype = mimetype
        self.expires = expires
        self.restricted = restricted

    @property
    def client(self):
        """Return the librarian client to use to retrieve that file."""
        if self.restricted:
            return getUtility(IRestrictedLibrarianClient)
        else:
            return getUtility(ILibrarianClient)

    @property
    def http_url(self):
        """See ILibraryFileAlias.http_url"""
        return self.client.getURLForAliasObject(self)

    @property
    def https_url(self):
        """See ILibraryFileAlias.https_url"""
        url = self.http_url
        if url is None:
            return url
        return url.replace("http", "https", 1)

    @property
    def private_url(self):
        """See ILibraryFileAlias.https_url"""
        return self.client.getURLForAlias(self.id, secure=True)

    def getURL(self, secure=True, include_token=False):
        """See ILibraryFileAlias.getURL"""
        if not self.restricted:
            if config.librarian.use_https and secure:
                return self.https_url
            else:
                return self.http_url
        else:
            url = self.private_url
            if include_token:
                token = TimeLimitedToken.allocate(url)
                url += "?token=%s" % token
            return url

    _datafile = None

    def open(self, timeout=LIBRARIAN_SERVER_DEFAULT_TIMEOUT):
        """See ILibraryFileAlias."""
        self._datafile = self.client.getFileByAlias(self.id, timeout)
        if self._datafile is None:
            raise DownloadFailed(
                "Unable to retrieve LibraryFileAlias %d" % self.id
            )

    def read(self, chunksize=None, timeout=LIBRARIAN_SERVER_DEFAULT_TIMEOUT):
        """See ILibraryFileAlias."""
        if not self._datafile:
            if chunksize is not None:
                raise RuntimeError("Can't combine autoopen with chunksize")
            self.open(timeout=timeout)
            autoopen = True
        else:
            autoopen = False

        if chunksize is None:
            rv = self._datafile.read()
            if autoopen:
                self.close()
            return rv
        else:
            return self._datafile.read(chunksize)

    def close(self):
        # Don't die with an AttributeError if the '_datafile' property
        # is not set.
        if self._datafile is not None:
            self._datafile.close()
            self._datafile = None

    @cachedproperty
    def last_downloaded(self):
        """See `ILibraryFileAlias`."""
        store = Store.of(self)
        results = store.find(LibraryFileDownloadCount, libraryfilealias=self)
        results.order_by(Desc(LibraryFileDownloadCount.day))
        entry = results.first()
        if entry is None:
            return None
        else:
            return datetime.now(timezone.utc).date() - entry.day

    def updateDownloadCount(self, day, country, count):
        """See ILibraryFileAlias."""
        store = Store.of(self)
        entry = store.find(
            LibraryFileDownloadCount,
            libraryfilealias=self,
            day=day,
            country=country,
        ).one()
        if entry is None:
            entry = LibraryFileDownloadCount(
                libraryfilealias=self, day=day, country=country, count=count
            )
        else:
            entry.count += count
        self.hits += count

    @property
    def deleted(self):
        return self.content_id is None

    def __storm_invalidated__(self):
        """Make sure that the file is closed across transaction boundary."""
        super().__storm_invalidated__()
        self.close()


@adapter(ILibraryFileAlias, Interface)
@implementer(ILibraryFileAliasWithParent)
@delegate_to(ILibraryFileAlias)
class LibraryFileAliasWithParent:
    """A LibraryFileAlias variant that has a parent."""

    def __init__(self, libraryfile, parent):
        self.context = libraryfile
        self.__parent__ = parent

    def createToken(self):
        """See `ILibraryFileAliasWithParent`."""
        return TimeLimitedToken.allocate(self.private_url)


@implementer(ILibraryFileAliasSet)
class LibraryFileAliasSet:
    """Create and find LibraryFileAliases."""

    def create(
        self,
        name,
        size,
        file,
        contentType,
        expires=None,
        debugID=None,
        restricted=False,
        allow_zero_length=False,
    ):
        """See `ILibraryFileAliasSet`"""
        if restricted:
            client = getUtility(IRestrictedLibrarianClient)
        else:
            client = getUtility(ILibrarianClient)
        if "/" in name:
            raise InvalidFilename("Filename cannot contain slashes.")
        fid = client.addFile(
            name,
            size,
            file,
            contentType,
            expires=expires,
            debugID=debugID,
            allow_zero_length=allow_zero_length,
        )
        lfa = (
            IPrimaryStore(LibraryFileAlias)
            .find(LibraryFileAlias, LibraryFileAlias.id == fid)
            .one()
        )
        assert lfa is not None, "client.addFile didn't!"
        return lfa

    def __getitem__(self, key):
        """See ILibraryFileAliasSet.__getitem__"""
        lfa = IStore(LibraryFileAlias).get(LibraryFileAlias, key)
        if lfa is None:
            raise NotFoundError(key)
        return lfa

    def findBySHA256(self, sha256):
        """See ILibraryFileAliasSet."""
        return IStore(LibraryFileAlias).find(
            LibraryFileAlias,
            LibraryFileAlias.content == LibraryFileContent.id,
            LibraryFileContent.sha256 == sha256,
        )

    def preloadLastDownloaded(self, lfas):
        """See `ILibraryFileAliasSet`."""
        store = IStore(LibraryFileAlias)
        results = store.find(
            (
                LibraryFileDownloadCount.libraryfilealias_id,
                LibraryFileDownloadCount.day,
            ),
            LibraryFileDownloadCount.libraryfilealias_id.is_in(
                sorted(lfa.id for lfa in lfas)
            ),
        )
        results.order_by(
            # libraryfilealias doesn't need to be descending for
            # correctness, but this allows the index on
            # LibraryFileDownloadCount (libraryfilealias, day, country) to
            # satisfy this query efficiently.
            Desc(LibraryFileDownloadCount.libraryfilealias_id),
            Desc(LibraryFileDownloadCount.day),
        )
        # Request the first row for each LFA, which corresponds to the most
        # recent day due to the above ordering.
        results.config(
            distinct=(LibraryFileDownloadCount.libraryfilealias_id,)
        )
        now = datetime.now(timezone.utc).date()
        lfas_by_id = {lfa.id: lfa for lfa in lfas}
        for lfa_id, day in results:
            get_property_cache(lfas_by_id[lfa_id]).last_downloaded = now - day
            del lfas_by_id[lfa_id]
        for lfa in lfas_by_id.values():
            get_property_cache(lfa).last_downloaded = None


@implementer(ILibraryFileDownloadCount)
class LibraryFileDownloadCount(StormBase):
    """See `ILibraryFileDownloadCount`"""

    __storm_table__ = "LibraryFileDownloadCount"

    id = Int(primary=True)
    libraryfilealias_id = Int(name="libraryfilealias", allow_none=False)
    libraryfilealias = Reference(libraryfilealias_id, "LibraryFileAlias.id")
    day = Date(allow_none=False)
    count = Int(allow_none=False)
    country_id = Int(name="country", allow_none=True)
    country = Reference(country_id, "Country.id")

    def __init__(self, libraryfilealias, day, count, country=None):
        super().__init__()
        self.libraryfilealias = libraryfilealias
        self.day = day
        self.count = count
        self.country = country


class TimeLimitedToken(StormBase):
    """A time limited access token for accessing a private file."""

    __storm_table__ = "TimeLimitedToken"

    created = DateTime(allow_none=False, default=UTC_NOW, tzinfo=timezone.utc)
    path = Unicode(allow_none=False)
    # The hex SHA-256 hash of the token.
    token = Unicode(allow_none=False)

    __storm_primary__ = ("path", "token")

    def __init__(self, path, token, created=None):
        """Create a TimeLimitedToken."""
        if created is not None:
            self.created = created
        self.path = path
        self.token = hashlib.sha256(token).hexdigest()

    @staticmethod
    def allocate(url):
        """Allocate a token for url path in the librarian.

        :param url: A url string. e.g.
            https://i123.restricted.launchpad-librarian.net/123/foo.txt
            Note that the token is generated for 123/foo.txt
        :return: A url fragment token ready to be attached to the url.
            e.g. 'a%20token'
        """
        store = session_store()
        path = TimeLimitedToken.url_to_token_path(url)
        token = create_token(32)
        store.add(TimeLimitedToken(path, token.encode("ascii")))
        # The session isn't part of the main transaction model, and in fact it
        # has autocommit on. The commit here is belts and bracers: after
        # allocation the external librarian must be able to serve the file
        # immediately.
        store.commit()
        return token

    @staticmethod
    def url_to_token_path(url):
        """Return the token path used for authorising access to url."""
        return urlparse(url)[2]
