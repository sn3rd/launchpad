# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Definition of the internet servers that Launchpad uses."""

import tempfile
import threading
import xmlrpc.client  # nosec B411
from io import BytesIO
from urllib.parse import parse_qs

import six
import transaction
from lazr.restful.interfaces import (
    ICollectionResource,
    IWebServiceConfiguration,
    IWebServiceVersion,
)
from lazr.restful.publisher import (
    WebServicePublicationMixin,
    WebServiceRequestTraversal,
)
from lazr.restful.utils import get_current_browser_request
from lazr.uri import URI
from talisker.logs import logging_context
from transaction.interfaces import ISynchronizer
from zope.app.publication.httpfactory import HTTPPublicationRequestFactory
from zope.app.publication.interfaces import IRequestPublicationFactory
from zope.app.publication.requestpublicationregistry import (
    factoryRegistry as publisher_factory_registry,
)
from zope.component import getUtility
from zope.formlib.itemswidgets import MultiDataHelper
from zope.formlib.widget import SimpleInputWidget
from zope.interface import alsoProvides, implementer
from zope.publisher.browser import BrowserRequest, BrowserResponse, TestRequest
from zope.publisher.http import HTTPInputStream
from zope.publisher.interfaces import NotFound
from zope.publisher.xmlrpc import XMLRPCRequest, XMLRPCResponse
from zope.security.interfaces import IParticipation, Unauthorized
from zope.security.proxy import isinstance as zope_isinstance
from zope.security.proxy import removeSecurityProxy

import lp.layers
from lp.app import versioninfo
from lp.app.errors import UnexpectedFormData
from lp.services.auth.interfaces import (
    IAccessTokenSet,
    IAccessTokenVerifiedRequest,
)
from lp.services.config import config
from lp.services.encoding import wsgi_native_string
from lp.services.features import get_relevant_feature_controller
from lp.services.features.flags import NullFeatureController
from lp.services.feeds.interfaces.application import IFeedsApplication
from lp.services.feeds.interfaces.feed import IFeed
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.oauth.interfaces import (
    IOAuthConsumerSet,
    IOAuthSignedRequest,
    TokenException,
)
from lp.services.propertycache import cachedproperty
from lp.services.statsd.interfaces.statsd_client import IStatsdClient
from lp.services.webapp.authentication import (
    check_oauth_signature,
    get_oauth_authorization,
)
from lp.services.webapp.authorization import (
    LAUNCHPAD_SECURITY_POLICY_CACHE_KEY,
    LAUNCHPAD_SECURITY_POLICY_CACHE_UNAUTH_KEY,
)
from lp.services.webapp.errorlog import ErrorReportRequest
from lp.services.webapp.escaping import html_escape
from lp.services.webapp.interaction import get_interaction_extras
from lp.services.webapp.interfaces import (
    IBasicLaunchpadRequest,
    IBrowserFormNG,
    IFavicon,
    ILaunchpadBrowserApplicationRequest,
    ILaunchpadProtocolError,
    INotificationRequest,
    INotificationResponse,
    IPlacelessAuthUtility,
    IPlacelessLoginSource,
    ISession,
    OAuthPermission,
)
from lp.services.webapp.notifications import (
    NotificationList,
    NotificationRequest,
    NotificationResponse,
)
from lp.services.webapp.opstats import OpStats
from lp.services.webapp.publication import LaunchpadBrowserPublication
from lp.services.webapp.publisher import RedirectionView, canonical_url
from lp.services.webapp.vhosts import allvhosts
from lp.services.webservice.interfaces import IWebServiceApplication
from lp.testopenid.interfaces.server import ITestOpenIDApplication
from lp.xmlrpc.interfaces import IPrivateApplication


class StepsToGo:
    """

    >>> class FakeRequest:
    ...     def __init__(self, traversed, stack):
    ...         self._traversed_names = traversed
    ...         self.stack = stack
    ...
    ...     def getTraversalStack(self):
    ...         return self.stack
    ...
    ...     def setTraversalStack(self, stack):
    ...         self.stack = stack
    ...

    >>> request = FakeRequest([], ["baz", "bar", "foo"])
    >>> stepstogo = StepsToGo(request)
    >>> stepstogo.startswith()
    True
    >>> stepstogo.startswith("foo")
    True
    >>> stepstogo.startswith("foo", "bar")
    True
    >>> stepstogo.startswith("foo", "baz")
    False
    >>> len(stepstogo)
    3
    >>> print(stepstogo.consume())
    foo
    >>> request._traversed_names
    ['foo']
    >>> request.stack
    ['baz', 'bar']
    >>> print(stepstogo.consume())
    bar
    >>> bool(stepstogo)
    True
    >>> print(stepstogo.consume())
    baz
    >>> print(stepstogo.consume())
    None
    >>> bool(stepstogo)
    False

    >>> request = FakeRequest([], ["baz", "bar", "foo"])
    >>> list(StepsToGo(request))
    ['foo', 'bar', 'baz']

    """

    @property
    def _stack(self):
        return self.request.getTraversalStack()

    def __init__(self, request):
        self.request = request

    def __iter__(self):
        return self

    def consume(self):
        """Remove the next path step and return it.

        Returns None if there are no path steps left.
        """
        stack = self.request.getTraversalStack()
        try:
            nextstep = stack.pop()
        except IndexError:
            return None
        self.request._traversed_names.append(nextstep)
        self.request.setTraversalStack(stack)
        return nextstep

    def peek(self):
        """Return the next path step without removing it.

        Returns None if there are no path steps left.
        """
        stack = self.request.getTraversalStack()
        try:
            return stack[-1]
        except IndexError:
            return None

    def __next__(self):
        value = self.consume()
        if value is None:
            raise StopIteration
        return value

    def startswith(self, *args):
        """Return whether the steps to go start with the names given."""
        if not args:
            return True
        return self._stack[-len(args) :] == list(reversed(args))

    def __len__(self):
        return len(self._stack)

    def __bool__(self):
        return bool(self._stack)


class ApplicationServerSettingRequestFactory:
    """Create a request and call its setApplicationServer method.

    Due to the factory-fanatical design of this part of Zope3, we need
    to have a kind of proxying factory here so that we can create an
    appropriate request and call its setApplicationServer method before it
    is used.
    """

    def __init__(self, requestfactory, host, protocol, port):
        self.requestfactory = requestfactory
        self.host = host
        self.protocol = protocol
        self.port = port

    def __call__(self, body_instream, environ, response=None):
        """Equivalent to the request's __init__ method."""
        # Make sure that HTTPS variable is set so that request.getURL() is
        # sane
        if self.protocol == "https":
            environ["HTTPS"] = "on"
        request = self.requestfactory(body_instream, environ, response)
        request.setApplicationServer(self.host, self.protocol, self.port)
        return request


@implementer(IRequestPublicationFactory)
class VirtualHostRequestPublicationFactory:
    """An `IRequestPublicationFactory` handling request to a Launchpad vhost.

    This factory will accepts requests to a particular Launchpad virtual host
    that matches a particular port and set of HTTP methods.
    """

    default_methods = ["GET", "HEAD", "POST"]

    def __init__(
        self,
        vhost_name,
        request_factory,
        publication_factory,
        port=None,
        methods=None,
        handle_default_host=False,
    ):
        """Creates a new factory.

        :param vhost_name: The config section defining the virtual host
             handled by this factory.
        :param request_factory: The request factory to use for this virtual
             host's requests.
        :param publication_factory: The publication factory to use for this
            virtual host's requests.
        :param port: The port which is handled by this factory. If
            this is None, this factory will handle requests that
            originate on any port.
        :param methods: A sequence of HTTP methods that this factory handles.
        :param handle_default_host: Whether or not this factory is
            capable of handling requests that specify no hostname.
        """

        self.vhost_name = vhost_name
        self.request_factory = request_factory
        self.publication_factory = publication_factory
        self.port = port
        if methods is None:
            methods = self.default_methods
        self.methods = methods
        self.handle_default_host = handle_default_host

        self.vhost_config = allvhosts.configs[self.vhost_name]
        self.all_hostnames = set(
            self.vhost_config.althostnames + [self.vhost_config.hostname]
        )
        self._thread_local = threading.local()
        self._thread_local.environment = None

    def canHandle(self, environment):
        """See `IRequestPublicationFactory`.

        Returns true if the HTTP host and port of the incoming request
        match the ones this factory is equipped to handle.
        """
        # We look at the wsgi environment to get the port this request
        # is coming in over.  The port number can be in one of two
        # places; either it's on the SERVER_PORT environment variable
        # or, as is the case with the test suite, it's on the
        # HTTP_HOST variable after a colon.
        # The former takes precedence, the port from the host variable is
        # only checked because the test suite doesn't set SERVER_PORT.
        host = environment.get("HTTP_HOST", "")
        port = environment.get("SERVER_PORT")
        if ":" in host:
            assert (
                len(host.split(":")) == 2
            ), "Having a ':' in the host name isn't allowed."
            host, new_port = host.split(":")
            if port is None:
                port = new_port

        if host == "":
            if not self.handle_default_host:
                return False
        elif host not in self.all_hostnames:
            return False
        else:
            # This factory handles this host.
            pass

        if self.port is not None:
            if port is not None:
                try:
                    port = int(port)
                except ValueError:
                    port = None
            if self.port != port:
                return False

        self._thread_local.environment = environment
        self._thread_local.host = host
        return True

    def __call__(self):
        """See `IRequestPublicationFactory`.

        We know that this factory is the right one for the given host
        and port. But there might be something else wrong with the
        request.  For instance, it might have the wrong HTTP method.
        """
        environment = self._thread_local.environment
        if environment is None:
            raise AssertionError("This factory declined the request.")

        root_url = URI(self.vhost_config.rooturl)

        real_request_factory, publication_factory = self.checkRequest(
            environment
        )

        if not real_request_factory:
            (
                real_request_factory,
                publication_factory,
            ) = self.getRequestAndPublicationFactories(environment)

        host = environment.get("HTTP_HOST", "").split(":")[0]
        if host in ["", "localhost"]:
            # Sometimes requests come in to the default or local host.
            # If we set the application server for these requests,
            # they'll be handled as launchpad.net requests, and
            # responses will go out containing launchpad.net URLs.
            # That's a little unelegant, so we don't set the application
            # server for these requests.
            request_factory = real_request_factory
        else:
            request_factory = ApplicationServerSettingRequestFactory(
                real_request_factory,
                root_url.host,
                root_url.scheme,
                root_url.port,
            )

        self._thread_local.environment = None
        return (request_factory, publication_factory)

    def getRequestAndPublicationFactories(self, environment):
        """Return the request and publication factories to use.

        You can override this method if the request and publication can
        vary based on the environment.
        """
        return self.request_factory, self.publication_factory

    def getAcceptableMethods(self, environment):
        """Return the HTTP methods acceptable in this environment."""
        return self.methods

    def checkRequest(self, environment):
        """Makes sure that the incoming HTTP request is of an expected type.

        This is different from canHandle() because we know the request
        went to the right place. It's just that it might be an invalid
        request for this handler.

        :return: An appropriate ProtocolErrorPublicationFactory if the
            HTTP request doesn't comply with the expected protocol. If
            the request does comply, (None, None).
        """
        method = environment.get("REQUEST_METHOD")

        if method in self.getAcceptableMethods(environment):
            factories = (None, None)
        else:
            request_factory = ProtocolErrorRequest
            publication_factory = ProtocolErrorPublicationFactory(
                405, headers={"Allow": ", ".join(self.methods)}
            )
            factories = (request_factory, publication_factory)

        return factories


class XMLRPCRequestPublicationFactory(VirtualHostRequestPublicationFactory):
    """A VirtualHostRequestPublicationFactory for XML-RPC.

    This factory only accepts XML-RPC method calls.
    """

    def __init__(
        self, vhost_name, request_factory, publication_factory, port=None
    ):
        super().__init__(
            vhost_name, request_factory, publication_factory, port, ["POST"]
        )

    def checkRequest(self, environment):
        """See `VirtualHostRequestPublicationFactory`.

        Accept only requests where the MIME type is text/xml.
        """
        request_factory, publication_factory = super().checkRequest(
            environment
        )
        if request_factory is None:
            mime_type = environment.get("CONTENT_TYPE")
            if mime_type.split(";")[0].strip() != "text/xml":
                request_factory = ProtocolErrorRequest
                # 415 - Unsupported Media Type
                publication_factory = ProtocolErrorPublicationFactory(415)
        return request_factory, publication_factory


class WebServiceRequestPublicationFactory(
    VirtualHostRequestPublicationFactory
):
    """A VirtualHostRequestPublicationFactory for requests against
    resources published through a web service.
    """

    default_methods = [
        "GET",
        "HEAD",
        "POST",
        "PATCH",
        "PUT",
        "DELETE",
        "OPTIONS",
    ]

    def __init__(
        self, vhost_name, request_factory, publication_factory, port=None
    ):
        """This factory accepts requests that use all major HTTP methods."""
        super().__init__(
            vhost_name, request_factory, publication_factory, port
        )


class VHostWebServiceRequestPublicationFactory(
    VirtualHostRequestPublicationFactory
):
    """An `IRequestPublicationFactory` handling requests to vhosts.

    It also handles requests to the launchpad web service, if the
    request's path points to a web service resource.
    """

    def getAcceptableMethods(self, environment):
        """See `VirtualHostRequestPublicationFactory`.

        If this is a request for a webservice path, returns the appropriate
        methods.
        """
        if self.isWebServicePath(environment.get("PATH_INFO", "")):
            return WebServiceRequestPublicationFactory.default_methods
        else:
            return super().getAcceptableMethods(environment)

    def getRequestAndPublicationFactories(self, environment):
        """See `VirtualHostRequestPublicationFactory`.

        If this is a request for a webservice path, returns the appropriate
        factories.
        """
        if self.isWebServicePath(environment.get("PATH_INFO", "")):
            return WebServiceClientRequest, WebServicePublication
        else:
            return super().getRequestAndPublicationFactories(environment)

    def isWebServicePath(self, path):
        """Does the path refer to a web service resource?"""
        # Add a trailing slash, if it is missing.
        if not path.endswith("/"):
            path = path + "/"
        ws_config = getUtility(IWebServiceConfiguration)
        return path.startswith("/%s/" % ws_config.path_override)


class NotFoundRequestPublicationFactory:
    """An IRequestPublicationFactory which always yields a 404."""

    def canHandle(self, environment):
        """See `IRequestPublicationFactory`."""
        return True

    def __call__(self):
        """See `IRequestPublicationFactory`.

        Unlike other publication factories, this one doesn't wrap its
        request factory in an ApplicationServerSettingRequestFactory.
        That's because it's only triggered when there's no valid hostname.
        """
        return (ProtocolErrorRequest, ProtocolErrorPublicationFactory(404))


def get_query_string_params(request):
    """Return a dict of the decoded query string params for a request.

    The parameter values will be decoded as unicodes, exactly as
    `BrowserRequest` would do to build the request form.

    Defined here so that it can be used in both BasicLaunchpadRequest and
    the LaunchpadTestRequest (which doesn't inherit from
    BasicLaunchpadRequest).
    """
    query_string = request.get("QUERY_STRING", "")

    # Just in case QUERY_STRING is in the environment explicitly as
    # None (Some tests seem to do this, but not sure if it can ever
    # happen outside of tests.)
    if query_string is None:
        query_string = ""

    return parse_qs(
        query_string,
        keep_blank_values=True,
        encoding="UTF-8",
        errors="replace",
    )


def safe_form_values(form: dict) -> dict:
    """Return a copy of the form with values escaped for use in JavaScript."""
    safe_form = {}
    for key, value in form.items():
        if isinstance(value, str):
            value = html_escape(value)
        elif isinstance(value, list):
            value = []
            for item in value:
                if isinstance(item, str):
                    item = html_escape(item)
                value.append(item)
        safe_form[key] = value
    return safe_form


class LaunchpadBrowserRequestMixin:
    """Provides methods used for both API and web browser requests."""

    def getRootURL(self, rootsite):
        """See IBasicLaunchpadRequest."""
        if rootsite is not None:
            assert (
                rootsite in allvhosts.configs
            ), "rootsite is %s.  Must be in %r." % (
                rootsite,
                sorted(allvhosts.configs.keys()),
            )
            root_url = allvhosts.configs[rootsite].rooturl
        else:
            root_url = self.getApplicationURL() + "/"
        return root_url

    @property
    def is_ajax(self):
        """See `IBasicLaunchpadRequest`."""
        return "XMLHttpRequest" == self.getHeader("HTTP_X_REQUESTED_WITH")

    def getURL(self, level=0, path_only=False, include_query=False):
        """See `IBasicLaunchpadRequest`."""
        url = super().getURL(level, path_only)
        if include_query:
            query_string = self.get("QUERY_STRING")
            if query_string is not None and len(query_string) > 0:
                url = "%s?%s" % (url, query_string)
        return url


@implementer(IBasicLaunchpadRequest)
class BasicLaunchpadRequest(LaunchpadBrowserRequestMixin):
    """Mixin request class to provide stepstogo."""

    strict_transport_security = True

    def __init__(self, body_instream, environ, response=None):
        self.traversed_objects = []
        self._wsgi_keys = set()
        if "PATH_INFO" in environ:
            # Zope's sane_environment (called by the superclass's __init__)
            # takes PATH_INFO, which according to WSGI must be a native
            # string containing only code points representable in
            # ISO-8859-1, and recodes it to UTF-8.  However, we don't want
            # it to raise UnicodeDecodeError before OOPS error handling is
            # available, so replace problems with U+FFFD before it has a
            # chance to recode anything.  This change will convert a 400
            # error to a 404, because traversal will raise NotFound when it
            # encounters a non-ASCII path part.
            pi = environ["PATH_INFO"]
            if isinstance(pi, bytes):
                pi = pi.decode("utf-8", "replace")
            environ["PATH_INFO"] = pi.encode("utf-8")
        super().__init__(body_instream, environ, response)
        # Now replace PATH_INFO with the version decoded by sane_environment.
        if "PATH_INFO" in self._environ:
            environ["PATH_INFO"] = self._environ["PATH_INFO"]

        # Our response always vary based on authentication.
        self.response.setHeader("Vary", "Cookie, Authorization")

        # Prevent clickjacking and content sniffing attacks.
        self.response.setHeader(
            "Content-Security-Policy", "frame-ancestors 'self';"
        )
        self.response.setHeader("X-Frame-Options", "SAMEORIGIN")
        self.response.setHeader("X-Content-Type-Options", "nosniff")
        self.response.setHeader("X-XSS-Protection", "1; mode=block")

        if self.strict_transport_security:
            # And tell browsers that we always use SSL unless we're on
            # an insecure vhost.
            # 15552000 = 180 days in seconds
            self.response.setHeader(
                "Strict-Transport-Security", "max-age=15552000"
            )

        # Publish revision information.
        self.response.setHeader("X-Launchpad-Revision", versioninfo.revision)

        # Talisker doesn't normally bother logging the Host: header, but
        # since we have a number of different virtual hosts it's useful to
        # have it do so.  Log the scheme as well so that log parsers can
        # reconstruct the full URL.
        extra = {}
        if "HTTP_HOST" in environ:
            extra["host"] = environ["HTTP_HOST"]
        if environ.get("HTTPS", "").lower() == "on":
            extra["scheme"] = "https"
        else:
            extra["scheme"] = "http"
        logging_context.push(**extra)

    @property
    def stepstogo(self):
        return StepsToGo(self)

    def retry(self):
        """See IPublisherRequest."""
        new_request = super().retry()
        # Propagate the list of keys we have set in the WSGI environment.
        new_request._wsgi_keys = self._wsgi_keys
        return new_request

    def getNearest(self, *some_interfaces):
        """See ILaunchpadBrowserApplicationRequest.getNearest()"""
        for context in reversed(self.traversed_objects):
            for iface in some_interfaces:
                if iface.providedBy(context):
                    return context, iface
        return None, None

    def setInWSGIEnvironment(self, key, value):
        """Set a key-value pair in the WSGI environment of this request.

        Raises KeyError if the key is already present in the environment
        but not set with setInWSGIEnvironment().
        """
        # This method expects the BasicLaunchpadRequest mixin to be used
        # with a base that provides self._orig_env.
        if key not in self._wsgi_keys and key in self._orig_env:
            raise KeyError("'%s' already present in wsgi environment." % key)
        self._orig_env[key] = value
        self._wsgi_keys.add(key)

    @cachedproperty
    def query_string_params(self):
        """See ILaunchpadBrowserApplicationRequest."""
        return get_query_string_params(self)


@implementer(
    ILaunchpadBrowserApplicationRequest,
    ISynchronizer,
    lp.layers.LaunchpadLayer,
)
class LaunchpadBrowserRequest(
    BasicLaunchpadRequest,
    BrowserRequest,
    NotificationRequest,
    ErrorReportRequest,
):
    """Integration of launchpad mixin request classes to make an uber
    launchpad request class.
    """

    retry_max_count = 5  # How many times we're willing to retry
    buffer_size = 64 * 1024  # buffer size for the processInputs

    def __init__(self, body_instream, environ, response=None):
        BasicLaunchpadRequest.__init__(self, body_instream, environ, response)
        transaction.manager.registerSynch(self)

    def _createResponse(self):
        """As per zope.publisher.browser.BrowserRequest._createResponse"""
        return LaunchpadBrowserResponse()

    def _decode(self, text):
        text = super()._decode(text)
        if isinstance(text, bytes):
            # BrowserRequest._decode failed to do so with the user-specified
            # charsets, so decode as UTF-8 with replacements, since we always
            # want unicode.
            text = text.decode("utf-8", "replace")
        return text

    @cachedproperty
    def form_ng(self):
        """See ILaunchpadBrowserApplicationRequest."""
        return BrowserFormNG(self.form)

    @cachedproperty
    def safe_form(self):
        """
        use the safe_form when the value will be used in a JavaScript string,
        to avoid injection attacks.
        """
        return safe_form_values(self.form)

    def setPrincipal(self, principal):
        self.clearSecurityPolicyCache()
        BrowserRequest.setPrincipal(self, principal)

    def clearSecurityPolicyCache(self):
        if LAUNCHPAD_SECURITY_POLICY_CACHE_KEY in self.annotations:
            del self.annotations[LAUNCHPAD_SECURITY_POLICY_CACHE_KEY]
        if LAUNCHPAD_SECURITY_POLICY_CACHE_UNAUTH_KEY in self.annotations:
            del self.annotations[LAUNCHPAD_SECURITY_POLICY_CACHE_UNAUTH_KEY]

    def beforeCompletion(self, transaction):
        """See `ISynchronizer`."""
        pass

    def afterCompletion(self, transaction):
        """See `ISynchronizer`.

        We clear the cache of security policy results on commit, as objects
        will be refetched from the database and the security checks may result
        in different answers.
        """
        self.clearSecurityPolicyCache()

    def newTransaction(self, transaction):
        """See `ISynchronizer`."""
        pass

    def _skipParseProcess(self):
        """Skips the parsing process inside processInputs by re-creating and
        re-inserting the input stream"""

        # The cache stream stores all the previous read operation results.
        # getCacheStream() reads the remaining body as well and adds it to the
        # cache stream. A complete re-creation is necessary since these stream
        # classes don't have any "seek()" method we can use to reset.
        new_body_instream = self._body_instream.getCacheStream()

        if isinstance(self._body_instream.stream, HTTPInputStream):
            self._body_instream.stream.cacheStream.close()

        self._body_instream = HTTPInputStream(new_body_instream, self._environ)

    def _createResetableStream(self, environment):
        """Creates a stream to be resetted at the end of the parsing process.

        This method is a copy of the __init__ method of HTTPInputStream class
        in zope.publisher since we use the returned value of this method in
        the same request instances in the same way while being unable to
        properly use that method here.
        """

        size = environment.get("CONTENT_LENGTH")
        # There can be no size in the environment (None) or the size
        # can be an empty string, in which case we treat it as absent.
        if not size:
            size = environment.get("HTTP_CONTENT_LENGTH")

        if not size or int(size) < 65536:
            resetable_stream = BytesIO()
        else:
            resetable_stream = tempfile.TemporaryFile()

        return resetable_stream

    # XXX: ilkeremrekoc 2025-03-12
    # processInputs is a part of Zope's request class, which we override.
    # This addition is because we are about to upgrade multipart package,
    # which in its newer versions is strict in its parsing of requests, only
    # accepting CRLF line-breaks throughout. And since the old versions of
    # apport package, which is the main bug-filing tool in Ubuntu, uses LF
    # line-breaks in its requests, we need backwards compatibility until the
    # older Ubuntu versions' apport packages can implement CRLF SRUs (Stable
    # Release Update). Once they do, and a sufficient section of users and
    # Ubuntu versions make the update, we can get rid of this method here.
    def processInputs(self):
        """See IPublisherRequest'

        Processes inputs before traversal

        This method overrides `BrowserRequest.processInputs()` to parse LF or
        CR line-breaks inside requests if they exist into CRLF line-breaks.
        Calls the parent method afterwards for further processing.
        """

        # Trigger the parsing only when the arriving request is pointed
        # towards apport package's entry point.
        # Note: Aside from apport, the browsers' UI can also reach this
        # page. This shouldn't affect our current process but should be kept
        # in mind for any large scale changes in the future.
        # Note: The third condition is redundant but was added as an extra
        # security since we really don't wish to enter this block if we are
        # not in the jurisdiction of the apport package.
        if (
            self._environ.get("PATH_INFO") == "/+storeblob"
            and self._environ.get("REQUEST_METHOD") == "POST"
            and self._environ.get("CONTENT_TYPE", "").startswith(
                "multipart/form-data"
            )
        ):

            # The request's body can rise to enourmous sizes since this part
            # of the code is for filing of bug reports, which require
            # extensive auto generated information. As a result, we create
            # the instance dynamically for different request sizes in the
            # same way the zope.publisher framework's HTTPInputStream's
            # cacheStream is created.
            parsing_body_stream = self._createResetableStream(self._environ)

            buffer = self._body_instream.read(self.buffer_size)

            # If the parsing happens, the content length will change with the
            # new line-breaks so we store the new one.
            newContentLength = 0

            log_linebreak_type = ""  # Store the linebreak type for logging

            while buffer != b"":

                if b"\r\n" in buffer:

                    # Since we assume there is a single line-break type in the
                    # request, we can skip the rest of the parsing when the
                    # CRLF is found.
                    self._skipParseProcess()
                    parsing_body_stream.close()

                    logging_context.push(linebreak_type="CRLF")
                    super().processInputs()
                    return

                # The only CRLF in the buffer might have been split between
                # the next buffer and the current one
                elif buffer.endswith(b"\r"):
                    nextChar = self._body_instream.read(1)
                    buffer += nextChar

                    if nextChar == b"\n":
                        self._skipParseProcess()
                        parsing_body_stream.close()

                        logging_context.push(linebreak_type="CRLF")
                        super().processInputs()
                        return

                if b"\n" in buffer:
                    buffer = buffer.replace(b"\n", b"\r\n")
                    newContentLength += len(buffer)

                    log_linebreak_type = "LF"
                elif b"\r" in buffer:
                    # Technically, we don't need CR functionality in our
                    # codebase, but in this case, not having it would make
                    # things harder to understand as the question of "what
                    # happens" would be left unknowable since the downstream
                    # doesn't raise errors.

                    buffer = buffer.replace(b"\r", b"\r\n")
                    newContentLength += len(buffer)

                    log_linebreak_type = "CR"
                else:
                    # If there are neither CRLF, LF or CR in a buffer, we
                    # cannot be sure if the Content-Length will change later,
                    # so, we store the buffer length just in case.
                    newContentLength += len(buffer)

                parsing_body_stream.write(buffer)
                buffer = self._body_instream.read(self.buffer_size)

            # Note that we can reach this part only if we parsed the whole
            # request.

            # Updating the content length by anticipating change.
            self._environ["CONTENT_LENGTH"] = newContentLength

            logging_context.push(linebreak_type=log_linebreak_type)

            # Reset the stream before we re-create the original one.
            parsing_body_stream.seek(0)

            deleting_stream = self._body_instream
            self._body_instream = HTTPInputStream(
                parsing_body_stream, self._environ
            )

            if isinstance(deleting_stream.stream, HTTPInputStream):
                deleting_stream.stream.cacheStream.close()
            deleting_stream.cacheStream.close()

        super().processInputs()
        return


@implementer(IBrowserFormNG)
class BrowserFormNG:
    """Wrapper that provides IBrowserFormNG around a regular form dict."""

    def __init__(self, form):
        """Create a new BrowserFormNG that wraps a dict of form data."""
        self.form = form

    def __contains__(self, name):
        """See IBrowserFormNG."""
        return name in self.form

    def __iter__(self):
        """See IBrowserFormNG."""
        return iter(self.form)

    def getOne(self, name, default=None):
        """See IBrowserFormNG."""
        value = self.form.get(name, default)
        if zope_isinstance(value, (list, tuple)):
            raise UnexpectedFormData(
                "Expected only one value form field %s: %s" % (name, value)
            )
        return value

    def getAll(self, name, default=None):
        """See IBrowserFormNG."""
        # We don't want a mutable as a default parameter, so we use None as a
        # marker.
        if default is None:
            default = []
        else:
            assert zope_isinstance(default, list), (
                "default should be a list: %s" % default
            )
        value = self.form.get(name, default)
        if not zope_isinstance(value, list):
            value = [value]
        return value


def web_service_request_to_browser_request(webservice_request):
    """Convert a given webservice request into a webapp one.

    Overrides 'SERVER_URL' to the 'mainsite', preserving headers and
    body.  Encodes PATH_INFO because it is unconditionally decoded by
    zope.publisher.http.sane_environment.
    """
    body = webservice_request.bodyStream.getCacheStream().read()
    environ = dict(webservice_request.environment)
    environ["SERVER_URL"] = allvhosts.configs["mainsite"].rooturl
    if "PATH_INFO" in environ:
        environ["PATH_INFO"] = environ["PATH_INFO"].encode("utf-8")
    return LaunchpadBrowserRequest(body, environ)


class Zope3WidgetsUseIBrowserFormNGMonkeyPatch:
    """Make Zope3 widgets use IBrowserFormNG.

    Replace the SimpleInputWidget._getFormInput method with one using
    `IBrowserFormNG`.
    """

    installed = False

    @classmethod
    def install(cls):
        """Install the monkey patch."""
        assert not cls.installed, "Monkey patch is already installed."

        def _getFormInput_single(self):
            """Return the submitted form value.

            :raises UnexpectedFormData: If more than one value is submitted.
            """
            return self.request.form_ng.getOne(self.name)

        def _getFormInput_multi(self):
            """Return the submitted form values."""
            return self.request.form_ng.getAll(self.name)

        # Save the original method and replace it with fixed ones.
        # We don't save MultiDataHelper._getFormInput because it doesn't
        # override the one in SimpleInputWidget.
        cls._original__getFormInput = SimpleInputWidget._getFormInput
        SimpleInputWidget._getFormInput = _getFormInput_single
        MultiDataHelper._getFormInput = _getFormInput_multi
        cls.installed = True

    @classmethod
    def uninstall(cls):
        """Uninstall the monkey patch."""
        assert cls.installed, "Monkey patch is not installed."

        # Restore saved method.
        SimpleInputWidget._getFormInput = cls._original__getFormInput
        del MultiDataHelper._getFormInput
        cls.installed = False


Zope3WidgetsUseIBrowserFormNGMonkeyPatch.install()


class LaunchpadBrowserResponse(NotificationResponse, BrowserResponse):
    # Note that NotificationResponse defines a 'redirect' method which
    # needs to override the 'redirect' method in BrowserResponse
    def __init__(self, header_output=None, http_transaction=None):
        super().__init__()

    def _validateHeader(self, name, value):
        name = str(name)
        if "\n" in name or "\r" in name or ":" in name:
            raise ValueError("CR, LF and colon are illegal in header names.")
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError("CR and LF are illegal in header values.")
        return name, value

    def setHeader(self, name, value, literal=False):
        name, value = self._validateHeader(name, value)
        super().setHeader(name, value, literal=literal)

    def addHeader(self, name, value):
        name, value = self._validateHeader(name, value)
        super().addHeader(name, value)

    def redirect(
        self, location, status=None, trusted=True, temporary_if_possible=False
    ):
        """Do a redirect.

        Unlike Zope's BrowserResponse.redirect(), consider all redirects to be
        trusted. Otherwise we'd have to change all callsites that redirect
        from lp.net to vhost.lp.net to pass trusted=True.

        If temporary_if_possible is True, then do a temporary redirect
        if this is a HEAD or GET, otherwise do a 303.

        See RFC 2616.

        The interface doesn't say that redirect returns anything.
        However, Zope's implementation does return the location given.  This
        is largely useless, as it is just the location given which is often
        relative.  So we won't return anything.
        """
        if temporary_if_possible:
            assert status is None, (
                "Do not set 'status' if also setting "
                "'temporary_if_possible'."
            )
            method = self._request.method
            if method == "GET" or method == "HEAD":
                status = 307
            else:
                status = 303
        super().redirect(str(location), status=status, trusted=trusted)


def adaptResponseToSession(response):
    """Adapt LaunchpadBrowserResponse to ISession"""
    return ISession(response._request)


def adaptRequestToResponse(request):
    """Adapt LaunchpadBrowserRequest to LaunchpadBrowserResponse"""
    return request.response


@implementer(
    INotificationRequest,
    IBasicLaunchpadRequest,
    IParticipation,
    lp.layers.LaunchpadLayer,
)
class LaunchpadTestRequest(
    LaunchpadBrowserRequestMixin, TestRequest, ErrorReportRequest
):
    """Mock request for use in unit and functional tests.

    >>> request = LaunchpadTestRequest(SERVER_URL="http://127.0.0.1/foo/bar")

    This class subclasses TestRequest - the standard Mock request object
    used in unit tests

    >>> isinstance(request, TestRequest)
    True

    It provides LaunchpadLayer and adds a mock INotificationRequest
    implementation.

    >>> lp.layers.LaunchpadLayer.providedBy(request)
    True
    >>> INotificationRequest.providedBy(request)
    True
    >>> request.uuid == request.response.uuid
    True
    >>> request.notifications is request.response.notifications
    True

    It also provides the form_ng attribute that is available from
    LaunchpadBrowserRequest.

    >>> from zope.interface.verify import verifyObject
    >>> verifyObject(IBrowserFormNG, request.form_ng)
    True

    It also provides the query_string_params dict that is available from
    LaunchpadBrowserRequest.

    >>> request = LaunchpadTestRequest(
    ...     SERVER_URL="http://127.0.0.1/foo/bar", QUERY_STRING="a=1&b=2&c=3"
    ... )
    >>> request.charsets = ["utf-8"]
    >>> request.query_string_params == {"a": ["1"], "b": ["2"], "c": ["3"]}
    True

    If force_fresh_login_for_testing is set to True, the
    ``lp.services.webapp.login.isFreshLogin`` function will always return True.
    This is useful in tests where you want to avoid needing a fresh login when
    exercising views such as ``PersonGPGView``

    """

    # These two attributes satisfy IParticipation.
    principal = None
    interaction = None

    def __init__(
        self,
        body_instream=None,
        environ=None,
        form=None,
        skin=None,
        outstream=None,
        method="GET",
        force_fresh_login_for_testing=False,
        **kw,
    ):
        # PEP 3333 requires environment variables to be native strings that
        # contain only code points representable in ISO-8859-1.  To support
        # porting to Python 3 via an intermediate stage of Unicode literals
        # in Python 2, we enforce this here.
        native_kw = {}
        for key, value in kw.items():
            if value is not None:
                value = wsgi_native_string(value)
            native_kw[key] = value
        super().__init__(
            body_instream=body_instream,
            environ=environ,
            form=form,
            skin=skin,
            outstream=outstream,
            REQUEST_METHOD=wsgi_native_string(method),
            **native_kw,
        )
        self.traversed_objects = []
        # Use an existing feature controller if one exists, otherwise use the
        # null controller.
        self.features = get_relevant_feature_controller()
        if self.features is None:
            self.features = NullFeatureController()
        self.force_fresh_login_for_testing = force_fresh_login_for_testing

    @property
    def uuid(self):
        return self.response.uuid

    @property
    def notifications(self):
        """See INotificationRequest."""
        return self.response.notifications

    @property
    def stepstogo(self):
        """See IBasicLaunchpadRequest."""
        return StepsToGo(self)

    def getNearest(self, *some_interfaces):
        """See IBasicLaunchpadRequest."""
        return None, None

    def setInWSGIEnvironment(self, key, value):
        """See IBasicLaunchpadRequest."""
        self._orig_env[key] = value

    def _createResponse(self):
        """As per zope.publisher.browser.BrowserRequest._createResponse"""
        return LaunchpadTestResponse()

    @property
    def form_ng(self):
        """See ILaunchpadBrowserApplicationRequest."""
        return BrowserFormNG(self.form)

    @property
    def safe_form(self):
        return safe_form_values(self.form)

    @property
    def query_string_params(self):
        """See ILaunchpadBrowserApplicationRequest."""
        return get_query_string_params(self)

    def setPrincipal(self, principal):
        """See `IPublicationRequest`."""
        self.principal = principal

    def clearSecurityPolicyCache(self):
        """See ILaunchpadBrowserApplicationRequest."""
        return


@implementer(INotificationResponse)
class LaunchpadTestResponse(LaunchpadBrowserResponse):
    """Mock response for use in unit and functional tests.

    >>> request = LaunchpadTestRequest()
    >>> response = request.response
    >>> isinstance(response, LaunchpadTestResponse)
    True
    >>> INotificationResponse.providedBy(response)
    True

    >>> response.addWarningNotification("Warning Notification")
    >>> print(request.notifications[0].message)
    Warning Notification
    """

    uuid = "LaunchpadTestResponse"

    _notifications = None

    @property
    def notifications(self):
        if self._notifications is None:
            self._notifications = NotificationList()
        return self._notifications


class DebugLayerRequestFactory(HTTPPublicationRequestFactory):
    """RequestFactory that sets the DebugLayer on a request."""

    def __call__(self, input_stream, env, output_stream=None):
        """See zope.app.publication.interfaces.IPublicationRequestFactory"""
        assert output_stream is None, "output_stream is deprecated in Z3.2"

        # Mark the request with the 'lp.layers.debug' layer
        request = HTTPPublicationRequestFactory.__call__(
            self, input_stream, env
        )
        lp.layers.setFirstLayer(request, lp.layers.DebugLayer)
        return request


# ---- mainsite


class MainLaunchpadPublication(LaunchpadBrowserPublication):
    """The publication used for the main Launchpad site."""


# ---- feeds


class FeedsPublication(LaunchpadBrowserPublication):
    """The publication used for Launchpad feed requests."""

    root_object_interface = IFeedsApplication

    def traverseName(self, request, ob, name):
        """Override traverseName to restrict urls on feeds.launchpad.net.

        Feeds.lp.net should only serve classes that implement the IFeed
        interface or redirect to some other url.
        """
        # LaunchpadImageFolder is imported here to avoid an import loop.
        from lp.app.browser.launchpad import LaunchpadImageFolder

        result = super().traverseName(request, ob, name)
        if len(request.stepstogo) == 0:
            # The url has been fully traversed. Now we can check that
            # the result is a feed, a favicon, an image, or a redirection.
            naked_result = removeSecurityProxy(result)
            if (
                IFeed.providedBy(result)
                or IFavicon.providedBy(result)
                or isinstance(naked_result, LaunchpadImageFolder)
                or getattr(naked_result, "status", None) == 301
            ):
                return result
            else:
                raise NotFound(self, "", request)
        else:
            # There are still url segments to traverse.
            return result

    def getPrincipal(self, request):
        """For feeds always return the anonymous user."""
        auth_utility = getUtility(IPlacelessAuthUtility)
        return auth_utility.unauthenticatedPrincipal()


@implementer(lp.layers.FeedsLayer)
class FeedsBrowserRequest(LaunchpadBrowserRequest):
    """Request type for a launchpad feed."""

    # Feeds is not served over SSL, so don't force SSL.
    strict_transport_security = False


# ---- testopenid


@implementer(lp.layers.TestOpenIDLayer)
class TestOpenIDBrowserRequest(LaunchpadBrowserRequest):
    pass


class TestOpenIDBrowserPublication(LaunchpadBrowserPublication):
    root_object_interface = ITestOpenIDApplication


# ---- web service


class WebServicePublication(
    WebServicePublicationMixin, LaunchpadBrowserPublication
):
    """The publication used for Launchpad web service requests."""

    root_object_interface = IWebServiceApplication

    def constructPageID(self, view, context):
        """Add the web service named operation (if any) to the page ID.

        See https://web.archive.org/web/20210618184623/https://dev.launchpad.net/Foundations/Webservice  # noqa: E501
        for more information about WebService page IDs.
        """
        pageid = super().constructPageID(view, context)
        if ICollectionResource.providedBy(view):
            # collection_identifier is a way to differentiate between
            # CollectionResource objects. CollectionResource objects are
            # objects that serve a list of Entry resources through the
            # WebService, so by querying the CollectionResource.type_url
            # attribute we're able to find out the resource type the
            # collection holds. See lazr.restful._resource.py to see how
            # the type_url is constructed.
            # We don't need the full URL, just the type of the resource.
            collection_identifier = view.type_url.split("/")[-1]
            if collection_identifier:
                pageid += ":" + collection_identifier
        op = view.request.get("ws.op") or view.request.query_string_params.get(
            "ws.op"
        )
        if op and isinstance(op, str):
            pageid += ":" + op
        return pageid

    def getApplication(self, request):
        """See `zope.publisher.interfaces.IPublication`.

        Always use the web service application to serve web service
        resources, no matter what application is normally used to serve
        the underlying objects.
        """
        return getUtility(IWebServiceApplication)

    def getResource(self, request, ob):
        """Return the resource that can publish the object ob.

        This is done at the end of traversal.  If the published object
        supports the ICollection, or IEntry interface we wrap it into the
        appropriate resource.
        """
        if zope_isinstance(ob, RedirectionView):
            # A redirection should be served as is.
            return ob
        else:
            return super().getResource(request, ob)

    def _getPrincipalFromAccessToken(self, request):
        """Authenticate a request using a personal access token."""
        access_token = removeSecurityProxy(
            getUtility(IAccessTokenSet).getBySecret(
                request._auth[len("Token ") :]
            )
        )
        if access_token is None:
            raise TokenException("Unknown access token.")
        elif access_token.is_expired:
            raise TokenException("Expired access token.")
        elif access_token.owner.account_status != AccountStatus.ACTIVE:
            raise TokenException("Inactive account.")
        access_token.updateLastUsed()
        # GET requests will be rolled back, as will unsuccessful ones.
        # Commit so that the last-used date is updated anyway.
        transaction.commit()
        logging_context.push(
            access_token_id=access_token.id,
            access_token_scopes=" ".join(
                scope.title for scope in access_token.scopes
            ),
        )
        alsoProvides(request, IAccessTokenVerifiedRequest)
        get_interaction_extras().access_token = access_token
        return getUtility(IPlacelessLoginSource).getPrincipal(
            access_token.owner.account_id
        )

    def _getPrincipalFromOAuth(self, request):
        """Authenticate a request using OAuth."""
        # Fetch OAuth authorization information from the request.
        try:
            form = get_oauth_authorization(request)
        except UnicodeDecodeError:
            raise TokenException("Invalid UTF-8.")

        consumer_key = form.get("oauth_consumer_key")
        consumers = getUtility(IOAuthConsumerSet)
        consumer = consumers.getByKey(consumer_key)
        token_key = form.get("oauth_token")
        anonymous_request = not token_key

        if consumer_key is None:
            # Either the client's OAuth implementation is broken, or
            # the user is trying to make an unauthenticated request
            # using wget or another OAuth-ignorant application.
            # Try to retrieve a consumer based on the User-Agent
            # header.
            anonymous_request = True
            consumer_key = six.ensure_text(request.getHeader("User-Agent", ""))
            if consumer_key == "":
                consumer_key = "anonymous client"
            consumer = consumers.getByKey(consumer_key)

        if consumer is None:
            if anonymous_request:
                # Require a consumer key (or user agent) to be present, so
                # that we can apply throttling if necessary.  But webservice
                # GET requests have their transactions rolled back, and at
                # the moment we don't do anything with the consumer in this
                # case, so there's no point dynamically creating a consumer.
                if consumer_key == "" or consumer_key is None:
                    raise TokenException("No consumer key specified.")
            else:
                # An unknown consumer can never make a non-anonymous
                # request, because access tokens are registered with a
                # specific, known consumer.
                raise Unauthorized("Unknown consumer (%s)." % consumer_key)
        if anonymous_request:
            # Skip the OAuth verification step and let the user access the
            # web service as an unauthenticated user.
            #
            # XXX leonardr 2009-12-15 bug=496964: Ideally we'd be
            # auto-creating a token for the anonymous user the first
            # time, passing it through the OAuth verification step,
            # and using it on all subsequent anonymous requests.
            alsoProvides(request, IOAuthSignedRequest)
            auth_utility = getUtility(IPlacelessAuthUtility)
            return auth_utility.unauthenticatedPrincipal()
        token = consumer.getAccessToken(token_key)
        if token is None:
            raise TokenException("Unknown access token (%s)." % token_key)
        if token.permission == OAuthPermission.UNAUTHORIZED:
            raise TokenException("Unauthorized token (%s)." % token.key)
        elif token.is_expired:
            raise TokenException("Expired token (%s)." % token.key)
        elif not check_oauth_signature(request, consumer, token):
            raise TokenException("Invalid signature.")
        elif token.person.account_status != AccountStatus.ACTIVE:
            raise TokenException("Inactive account.")
        else:
            # Everything is fine, let's return the principal.
            pass
        alsoProvides(request, IOAuthSignedRequest)
        if token.context is not None:
            scope_url = canonical_url(token.context, force_local_path=True)
        else:
            scope_url = None
        return getUtility(IPlacelessLoginSource).getPrincipal(
            token.person.account.id,
            access_level=token.permission,
            scope_url=scope_url,
        )

    def getPrincipal(self, request):
        """See `LaunchpadBrowserPublication`.

        Web service requests are authenticated using OAuth or personal
        access tokens, except for the one made using (presumably) JavaScript
        on the /api override path.

        Raises TokenException which has a webservice error status of
        Unauthorized - 401.

        Raises Unauthorized directly in the case where the consumer is None
        for a non-anonymous request as it may represent a server error.
        """
        # Use the regular HTTP authentication, when the request is not
        # on the API virtual host but comes through the path_override on
        # the other regular virtual hosts.
        request_path = request.get("PATH_INFO", "")
        web_service_config = getUtility(IWebServiceConfiguration)
        if request_path.startswith("/%s" % web_service_config.path_override):
            return super().getPrincipal(request)

        if request._auth is not None and request._auth.startswith("Token "):
            return self._getPrincipalFromAccessToken(request)
        else:
            return self._getPrincipalFromOAuth(request)


@implementer(lp.layers.WebServiceLayer)
class LaunchpadWebServiceRequestTraversal(WebServiceRequestTraversal):
    def getRootURL(self, rootsite):
        """See IBasicLaunchpadRequest."""
        # When browsing the web service, we want URLs to point back at the web
        # service, so we basically ignore rootsite.
        return self.getApplicationURL() + "/"


class WebServiceClientRequest(
    LaunchpadWebServiceRequestTraversal, LaunchpadBrowserRequest
):
    """Request type for a resource published through the web service."""

    def __init__(self, body_instream, environ, response=None):
        super().__init__(body_instream, environ, response)
        # Web service requests use content negotiation, so we put
        # 'Accept' in the Vary header. They don't use cookies, so
        # there's no point in putting 'Cookie' in the Vary header, and
        # putting 'Authorization' in the Vary header totally destroys
        # caching because every web service request contains a
        # distinct OAuth nonce in its Authorization header.
        #
        # Because 'Authorization' is not in the Vary header, a client
        # that reuses a single cache for different OAuth credentials
        # could conceivably leak private information to an
        # unprivileged user via the cache. This won't happen for the
        # web service root resource because the service root is the
        # same for everybody. It won't happen for entry resources
        # because if two users have a different representation of an
        # entry, the ETag will also be different and a conditional
        # request will fail.
        #
        # Once lazr.restful starts setting caching directives other
        # than ETag, we may have to revisit this.
        self.response.setHeader("Vary", "Accept")


class WebServiceTestRequest(
    LaunchpadWebServiceRequestTraversal, LaunchpadTestRequest
):
    """Test request for the webservice.

    It provides the WebServiceLayer and supports the getResource()
    web publication hook.
    """

    def __init__(self, body_instream=None, environ=None, version=None, **kw):
        test_environ = {
            "SERVER_URL": "http://api.launchpad.test",
            "HTTP_HOST": "api.launchpad.test",
        }
        if environ is not None:
            test_environ.update(environ)
        super().__init__(
            body_instream=body_instream, environ=test_environ, **kw
        )
        if version is None:
            version = getUtility(IWebServiceConfiguration).active_versions[-1]
        self.version = version
        version_marker = getUtility(IWebServiceVersion, name=version)
        alsoProvides(self, version_marker)


# ---- xmlrpc


class PublicXMLRPCPublication(LaunchpadBrowserPublication):
    """The publication used for public XML-RPC requests."""

    def handleException(self, object, request, exc_info, retry_allowed=True):
        LaunchpadBrowserPublication.handleException(
            self, object, request, exc_info, retry_allowed
        )
        OpStats.stats["xml-rpc faults"] += 1
        getUtility(IStatsdClient).incr("errors.xmlrpc")

    def endRequest(self, request, object):
        OpStats.stats["xml-rpc requests"] += 1
        getUtility(IStatsdClient).incr("requests.xmlrpc")
        return LaunchpadBrowserPublication.endRequest(self, request, object)


class PublicXMLRPCRequest(
    BasicLaunchpadRequest, XMLRPCRequest, ErrorReportRequest
):
    """Request type for doing public XML-RPC in Launchpad."""

    def getRootURL(self, rootsite):
        """See IBasicLaunchpadRequest."""
        # XML-RPC requests occasionally need to use canonical_url, for
        # the likes of sending emails. Until these are tracked down and
        # fixed to use mainsite explicitly, replace the XML-RPC root
        # URLs with mainsite's, so that URLs are meaningful.
        if rootsite in (None, "xmlrpc", "xmlrpc_private"):
            rootsite = "mainsite"
        return super().getRootURL(rootsite)

    def _createResponse(self):
        return PublicXMLRPCResponse()


class PublicXMLRPCResponse(XMLRPCResponse):
    """Response type for doing public XML-RPC in Launchpad."""

    def handleException(self, exc_info):
        # If we don't have a proper xmlrpc.client.Fault, and we have
        # logged an OOPS, create a Fault that reports the OOPS ID to
        # the user.
        exc_value = exc_info[1]
        if not isinstance(exc_value, xmlrpc.client.Fault):
            request = get_current_browser_request()
            if request is not None and request.oopsid is not None:
                exc_info = (
                    xmlrpc.client.Fault,
                    xmlrpc.client.Fault(-1, request.oopsid),
                    None,
                )
        XMLRPCResponse.handleException(self, exc_info)


class PrivateXMLRPCPublication(PublicXMLRPCPublication):
    """The publication used for private XML-RPC requests."""

    root_object_interface = IPrivateApplication

    def traverseName(self, request, ob, name):
        """Traverse to an end point or let normal traversal do its thing."""
        assert isinstance(
            request, PrivateXMLRPCRequest
        ), "Not a private XML-RPC request"
        missing = object()
        end_point = getattr(ob, name, missing)
        if end_point is missing:
            return super().traverseName(request, ob, name)
        return end_point


class PrivateXMLRPCRequest(PublicXMLRPCRequest):
    """Request type for doing private XML-RPC in Launchpad."""

    # For now, the same as public requests except that there's no SSL.

    strict_transport_security = False


# ---- Protocol errors


class ProtocolErrorRequest(LaunchpadBrowserRequest):
    """An HTTP request that happened to result in an HTTP error."""

    def traverse(self, object):
        """It's already been determined that there's an error. Return None."""
        return None


class ProtocolErrorPublicationFactory:
    """This class publishes error messages in response to protocol errors."""

    def __init__(self, status, headers=None):
        """Store the headers and status for turning into a parameterized
        publication.
        """
        if not headers:
            headers = {}
        self.status = status
        self.headers = headers

    def __call__(self, db):
        """Create a parameterized publication object."""
        return ProtocolErrorPublication(self.status, self.headers)


class ProtocolErrorPublication(LaunchpadBrowserPublication):
    """Publication used for requests that turn out to be protocol errors."""

    def __init__(self, status, headers):
        """Prepare to construct a ProtocolErrorException

        :param status: The HTTP status to send
        :param headers: Any HTTP headers that should be sent.
        """
        super().__init__(None)
        self.status = status
        self.headers = headers

    def callObject(self, request, object):
        """Raise an appropriate exception for this protocol error."""
        if self.status == 404:
            raise NotFound(self, "", request)
        if self.status == 405 and request.method == "OPTIONS":
            self.startPublicationTiming(request, include_thread_time=False)
            request.response.setStatus(self.status)
            for header, value in self.headers.items():
                request.response.setHeader(header, value)
            return ""
        raise ProtocolErrorException(self.status, self.headers)


@implementer(ILaunchpadProtocolError)
class ProtocolErrorException(Exception):
    """An exception for requests that turn out to be protocol errors."""

    def __init__(self, status, headers):
        """Store status and headers for rendering in the HTTP response."""
        Exception.__init__(self)
        self.status = status
        self.headers = headers

    def __str__(self):
        """A protocol error can be well-represented by its HTTP status code."""
        return "Protocol error: %s" % self.status


def register_launchpad_request_publication_factories():
    """Register our factories with the Zope3 publisher.

    DEATH TO ZCML!
    """
    VHRP = VirtualHostRequestPublicationFactory
    VWSHRP = VHostWebServiceRequestPublicationFactory

    factories = [
        VWSHRP(
            "mainsite",
            LaunchpadBrowserRequest,
            MainLaunchpadPublication,
            handle_default_host=True,
        ),
        VHRP("feeds", FeedsBrowserRequest, FeedsPublication),
        WebServiceRequestPublicationFactory(
            "api", WebServiceClientRequest, WebServicePublication
        ),
        XMLRPCRequestPublicationFactory(
            "xmlrpc", PublicXMLRPCRequest, PublicXMLRPCPublication
        ),
    ]

    if config.launchpad.enable_test_openid_provider:
        factories.append(
            VHRP(
                "testopenid",
                TestOpenIDBrowserRequest,
                TestOpenIDBrowserPublication,
            )
        )

    # We may also have a private XML-RPC server.
    private_port = config.vhost.xmlrpc_private.private_port

    if private_port is not None:
        factories.append(
            XMLRPCRequestPublicationFactory(
                "xmlrpc_private",
                PrivateXMLRPCRequest,
                PrivateXMLRPCPublication,
                port=private_port,
            )
        )

    # Register those factories, in priority order corresponding to
    # their order in the list. This means picking a large number for
    # the first factory and giving each subsequent factory the next
    # lower number. We need to leave one space left over for the
    # catch-all handler defined below, so we start at
    # len(factories)+1.
    for priority, factory in enumerate(factories):
        publisher_factory_registry.register(
            "*",
            "*",
            factory.vhost_name,
            len(factories) - priority + 1,
            factory,
        )

    # Register a catch-all "not found" handler at the lowest priority.
    publisher_factory_registry.register(
        "*", "*", "*", 0, NotFoundRequestPublicationFactory()
    )


register_launchpad_request_publication_factories()
