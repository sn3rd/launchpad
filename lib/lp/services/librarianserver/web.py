# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import time
from datetime import datetime
from urllib.parse import urlparse

import six
from pymacaroons import Macaroon
from storm.exceptions import DisconnectionError
from twisted.internet import abstract, defer, reactor
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.interfaces import IPushProducer
from twisted.internet.protocol import Protocol
from twisted.internet.threads import deferToThread
from twisted.python import log
from twisted.web import http, proxy, resource, server, static, util
from twisted.web.client import ProxyAgent
from twisted.web.http_headers import Headers
from twisted.web.server import NOT_DONE_YET
from zope.interface import implementer

from lp.services.config import config
from lp.services.database import read_transaction, write_transaction
from lp.services.librarian.client import url_path_quote
from lp.services.librarian.utils import guess_librarian_encoding

defaultResource = static.Data(
    b"""
        <html>
        <body>
        <h1>Launchpad Librarian</h1>
        <p>
        http://librarian.launchpad.net/ is a
        file repository used by
        <a href="https://launchpad.net/">Launchpad</a>.
        </p>
        <p><small>Copyright 2004 Canonical Ltd.</small></p>
        <!-- kthxbye. -->
        </body></html>
        """,
    type="text/html",
)
fourOhFour = resource.NoResource("No such resource")


class NotFound(Exception):
    pass


class LibraryFileResource(resource.Resource):
    def __init__(self, storage, upstreamHost, upstreamPort):
        resource.Resource.__init__(self)
        self.storage = storage
        self.upstreamHost = upstreamHost
        self.upstreamPort = upstreamPort

    def getChild(self, name, request):
        if name == b"":
            # Root resource
            return defaultResource
        try:
            aliasID = int(name)
        except ValueError:
            log.msg("404: alias is not an int: %r" % (name,))
            return fourOhFour

        return LibraryFileAliasResource(
            self.storage, aliasID, self.upstreamHost, self.upstreamPort
        )


class _StreamingReceiver(Protocol):
    def __init__(self, request):
        self.request = request

    def dataReceived(self, data):
        self.request.write(data)

    def connectionLost(self, reason):
        self.request.finish()


class ProxiedReverseProxyResource(proxy.ReverseProxyResource):
    """
    Subclass of Twisted's ReverseProxyResource that streams responses
    using a TunnelingAgent.
    """

    def __init__(self, host, port, path, reactor, tunnelingAgent):
        super().__init__(host, port, path, reactor)
        self.tunnelingAgent = tunnelingAgent

    def render(self, request):
        """
        Override to use TunnelingAgent and stream the response.
        """
        full_path = self.path
        if request.uri.find(b"?") != -1:
            full_path += b"?" + request.uri.split(b"?", 1)[1]

        url = b"http://%s:%d%s" % (self.host.encode(), self.port, full_path)

        raw_headers = request.getAllHeaders()
        headers = Headers({k: [v] for k, v in raw_headers.items()})

        # override the host header to upstream's host
        headers.setRawHeaders(
            b"host", [f"{self.host}:{self.port}".encode("ascii")]
        )

        d = self.tunnelingAgent.request(
            request.method,
            url,
            headers,
            None,
        )

        def on_response(resp):
            request.setResponseCode(resp.code)
            for header, values in resp.headers.getAllRawHeaders():
                request.responseHeaders.setRawHeaders(
                    header.decode(), [v.decode() for v in values]
                )
            resp.deliverBody(_StreamingReceiver(request))
            return NOT_DONE_YET

        def on_error(err):
            log.err(err, "Error proxying request to upstream server")
            request.setResponseCode(502)
            request.finish()

        d.addCallback(on_response)
        d.addErrback(on_error)

        return NOT_DONE_YET


class LibraryFileAliasResource(resource.Resource):
    def __init__(self, storage, aliasID, upstreamHost, upstreamPort):
        resource.Resource.__init__(self)
        self.storage = storage
        self.aliasID = aliasID
        self.upstreamHost = upstreamHost
        self.upstreamPort = upstreamPort

    def getChild(self, filename, request):
        # If we still have another component of the path, then we have
        # an old URL that encodes the content ID. We want to keep supporting
        # these, so we just ignore the content id that is currently in
        # self.aliasID and extract the real one from the URL. Note that
        # tokens do not work with the old URL style: they are URL specific.
        if len(request.postpath) == 1:
            try:
                self.aliasID = int(filename)
            except ValueError:
                log.msg("404 (old URL): alias is not an int: %r" % (filename,))
                return fourOhFour
            filename = request.postpath[0]

        # IFF the request has a .restricted. subdomain, ensure there is a
        # alias id in the right most subdomain, and that it matches
        # self.aliasIDd, And that the host precisely matches what we generate
        # (specifically to stop people putting a good prefix to the left of an
        # attacking one).
        hostname = six.ensure_str(request.getRequestHostname())
        if ".restricted." in hostname:
            # Configs can change without warning: evaluate every time.
            download_url = config.librarian.download_url
            parsed = list(urlparse(download_url))
            netloc = parsed[1]
            # Strip port if present
            if netloc.find(":") > -1:
                netloc = netloc[: netloc.find(":")]
            expected_hostname = "i%d.restricted.%s" % (self.aliasID, netloc)
            if expected_hostname != hostname:
                log.msg(
                    "404: expected_hostname != hostname: %r != %r"
                    % (expected_hostname, hostname)
                )
                return fourOhFour

        token = request.args.get(b"token", [None])[0]
        if token is None:
            if not request.getUser() and request.getPassword():
                try:
                    token = Macaroon.deserialize(request.getPassword())
                # XXX cjwatson 2019-04-23: Restrict exceptions once
                # https://github.com/ecordell/pymacaroons/issues/50 is fixed.
                except Exception:
                    pass
        path = six.ensure_text(request.path)
        deferred = deferToThread(self._getFileAlias, self.aliasID, token, path)
        deferred.addCallback(self._cb_getFileAlias, filename, request)
        deferred.addErrback(self._eb_getFileAlias)
        return util.DeferredResource(deferred)

    @write_transaction
    def _getFileAlias(self, aliasID, token, path):
        try:
            alias = self.storage.getFileAlias(aliasID, token, path)
            return (
                alias.content_id,
                alias.filename,
                alias.mimetype,
                alias.date_created,
                alias.content.filesize,
                alias.restricted,
            )
        except LookupError:
            raise NotFound

    def _eb_getFileAlias(self, failure):
        err = failure.trap(NotFound, DisconnectionError)
        if err == DisconnectionError:
            return resource.ErrorPage(
                503,
                "Database unavailable",
                "A required database is unavailable.\n"
                "See https://ubuntu.social/@launchpadstatus "
                "for maintenance and outage notifications.",
            )
        else:
            return fourOhFour

    @defer.inlineCallbacks
    def _cb_getFileAlias(self, results, filename, request):
        (
            dbcontentID,
            dbfilename,
            mimetype,
            date_created,
            size,
            restricted,
        ) = results
        # Return a 404 if the filename in the URL is incorrect. This offers
        # a crude form of access control (stuff we care about can have
        # unguessable names effectively using the filename as a secret).
        if dbfilename.encode("utf-8") != filename:
            log.msg(
                "404: dbfilename.encode('utf-8') != filename: %r != %r"
                % (dbfilename.encode("utf-8"), filename)
            )
            return fourOhFour

        stream = yield self.storage.open(dbcontentID)
        if stream is not None:
            # XXX: Brad Crittenden 2007-12-05 bug=174204: When encodings are
            # stored as part of a file's metadata this logic will be replaced.
            encoding, mimetype = guess_librarian_encoding(dbfilename, mimetype)
            file = File(mimetype, encoding, date_created, stream, size)
            # Set our caching headers. Public Librarian files can be
            # cached forever, while private ones mustn't be at all.
            request.setHeader(
                "Cache-Control",
                (
                    "max-age=31536000, public"
                    if not restricted
                    else "max-age=0, private"
                ),
            )
            return file
        elif self.upstreamHost is not None:
            proxy_url = config.launchpad.http_proxy
            if proxy_url:
                parsed_proxy_url = urlparse(proxy_url)
                if parsed_proxy_url.port is None:
                    raise ValueError(
                        "HTTP proxy URL must include an explicit port number"
                    )
                endpoint = TCP4ClientEndpoint(
                    reactor,
                    parsed_proxy_url.hostname,
                    parsed_proxy_url.port,
                )
                agent = ProxyAgent(endpoint)
                return ProxiedReverseProxyResource(
                    self.upstreamHost,
                    self.upstreamPort,
                    request.path,
                    reactor,
                    agent,
                )
            return proxy.ReverseProxyResource(
                self.upstreamHost, self.upstreamPort, request.path
            )
        else:
            raise AssertionError(
                "Content %d missing from storage." % dbcontentID
            )

    def render_GET(self, request):
        return defaultResource.render(request)


class File(resource.Resource):
    isLeaf = True

    def __init__(self, contentType, encoding, modification_time, stream, size):
        resource.Resource.__init__(self)
        # Have to convert the UTC datetime to POSIX timestamp (localtime)
        offset = datetime.utcnow() - datetime.now()
        local_modification_time = modification_time - offset
        self._modification_time = time.mktime(
            local_modification_time.timetuple()
        )
        self.type = contentType
        self.encoding = encoding
        self.stream = stream
        self.size = size

    def _setContentHeaders(self, request):
        request.setHeader(b"content-length", b"%d" % self.size)
        if self.type:
            request.setHeader(
                b"content-type", six.ensure_binary(self.type, "ASCII")
            )
        if self.encoding:
            request.setHeader(
                b"content-encoding", six.ensure_binary(self.encoding, "ASCII")
            )

    def render_GET(self, request):
        """See `Resource`."""
        request.setHeader(b"accept-ranges", b"none")

        if request.setLastModified(self._modification_time) is http.CACHED:
            # `setLastModified` also sets the response code for us, so if
            # the request is cached, we close the file now that we've made
            # sure that the request would otherwise succeed and return an
            # empty body.
            self.stream.close()
            return b""

        if request.method == b"HEAD":
            # Set the content headers here, rather than making a producer.
            self._setContentHeaders(request)
            self.stream.close()
            return b""

        # static.File has HTTP range support, which would be nice to have.
        # Unfortunately, static.File isn't a good match for producing data
        # dynamically by fetching it from Swift. The librarian used to sit
        # behind Squid, which took care of this, but it no longer does. I
        # think we will need to cargo-cult the byte-range support and three
        # Producer implementations from static.File, making the small
        # modifications to cope with self.fileObject.read maybe returning a
        # Deferred, and the static.File.makeProducer method to return the
        # correct producer.
        self._setContentHeaders(request)
        request.setResponseCode(http.OK)
        producer = FileProducer(request, self.stream)
        producer.start()

        return server.NOT_DONE_YET


@implementer(IPushProducer)
class FileProducer:
    buffer_size = abstract.FileDescriptor.bufferSize

    def __init__(self, request, stream):
        self.request = request
        self.stream = stream
        self.producing = True

    def start(self):
        self.request.registerProducer(self, True)
        self.resumeProducing()

    def pauseProducing(self):
        """See `IPushProducer`."""
        self.producing = False

    @defer.inlineCallbacks
    def _produceFromStream(self):
        """Read data from our stream and write it to our consumer."""
        while self.request and self.producing:
            data = yield self.stream.read(self.buffer_size)
            # pauseProducing or stopProducing may have been called while we
            # were waiting.
            if not self.producing:
                return
            if data:
                self.request.write(data)
            else:
                self.request.unregisterProducer()
                self.request.finish()
                self.stopProducing()

    def resumeProducing(self):
        """See `IPushProducer`."""
        self.producing = True
        if self.request:
            reactor.callLater(0, self._produceFromStream)

    def stopProducing(self):
        """See `IProducer`."""
        self.producing = False
        self.stream.close()
        self.request = None


class DigestSearchResource(resource.Resource):
    def __init__(self, storage):
        self.storage = storage
        resource.Resource.__init__(self)

    def render_GET(self, request):
        try:
            digest = six.ensure_text(request.args[b"digest"][0])
        except (LookupError, UnicodeDecodeError):
            return static.Data(b"Bad search", "text/plain").render(request)

        deferred = deferToThread(self._matchingAliases, digest)
        deferred.addCallback(self._cb_matchingAliases, request)
        deferred.addErrback(_eb, request)
        return server.NOT_DONE_YET

    @read_transaction
    def _matchingAliases(self, digest):
        library = self.storage.library
        matches = [
            "%s/%s" % (aID, url_path_quote(aName))
            for fID in library.lookupBySHA1(digest)
            for aID, aName, aType in library.getAliases(fID)
        ]
        return matches

    def _cb_matchingAliases(self, matches, request):
        text = "\n".join([str(len(matches))] + matches)
        response = static.Data(
            text.encode("utf-8"), "text/plain; charset=utf-8"
        ).render(request)
        request.write(response)
        request.finish()


# Ask robots not to index or archive anything in the librarian.
robotsTxt = static.Data(
    b"""
User-agent: *
Disallow: /
""",
    type="text/plain",
)


def _eb(failure, request):
    """Generic errback for failures during a render_GET."""
    request.processingFailed(failure)
