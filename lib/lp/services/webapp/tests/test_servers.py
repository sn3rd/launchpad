# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import unittest
from datetime import datetime, timedelta, timezone
from doctest import ELLIPSIS, NORMALIZE_WHITESPACE, DocTestSuite
from textwrap import dedent

import transaction
from lazr.restful.interfaces import (
    IServiceRootResource,
    IWebServiceConfiguration,
)
from lazr.restful.simple import RootResource
from lazr.restful.testing.webservice import (
    IGenericCollection,
    IGenericEntry,
    WebServiceTestCase,
)
from talisker.context import Context
from talisker.logs import logging_context
from testtools.matchers import ContainsDict, Equals
from zope.component import getGlobalSiteManager, getUtility
from zope.interface import Interface, implementer
from zope.publisher.http import HTTPInputStream
from zope.publisher.publish import publish
from zope.security.interfaces import Unauthorized
from zope.security.management import newInteraction
from zope.security.proxy import removeSecurityProxy

from lp.app import versioninfo
from lp.services.auth.enums import AccessTokenScope
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.oauth.interfaces import TokenException
from lp.services.webapp.interaction import get_interaction_extras
from lp.services.webapp.servers import (
    ApplicationServerSettingRequestFactory,
    FeedsBrowserRequest,
    LaunchpadBrowserRequest,
    LaunchpadBrowserResponse,
    LaunchpadTestRequest,
    PrivateXMLRPCRequest,
    ProtocolErrorPublication,
    VHostWebServiceRequestPublicationFactory,
    VirtualHostRequestPublicationFactory,
    WebServiceClientRequest,
    WebServicePublication,
    WebServiceRequestPublicationFactory,
    WebServiceTestRequest,
    web_service_request_to_browser_request,
)
from lp.testing import TestCase, TestCaseWithFactory, logout
from lp.testing.layers import DatabaseFunctionalLayer, FunctionalLayer
from lp.testing.publication import get_request_and_publication


class SetInWSGIEnvironmentTestCase(TestCase):
    def test_set(self):
        # Test that setInWSGIEnvironment() can set keys in the WSGI
        # environment.
        data = io.BytesIO(b"foo")
        env = {}
        request = LaunchpadBrowserRequest(data, env)
        request.setInWSGIEnvironment("key", "value")
        self.assertEqual(request._orig_env["key"], "value")

    def test_set_fails_for_existing_key(self):
        # Test that setInWSGIEnvironment() fails if the user tries to
        # set a key that existed in the WSGI environment.
        data = io.BytesIO(b"foo")
        env = {"key": "old value"}
        request = LaunchpadBrowserRequest(data, env)
        self.assertRaises(
            KeyError, request.setInWSGIEnvironment, "key", "new value"
        )
        self.assertEqual(request._orig_env["key"], "old value")

    def test_set_twice(self):
        # Test that setInWSGIEnvironment() can change the value of
        # keys in the WSGI environment that it had previously set.
        data = io.BytesIO(b"foo")
        env = {}
        request = LaunchpadBrowserRequest(data, env)
        request.setInWSGIEnvironment("key", "first value")
        request.setInWSGIEnvironment("key", "second value")
        self.assertEqual(request._orig_env["key"], "second value")

    def test_set_after_retry(self):
        # Test that setInWSGIEnvironment() a key in the environment
        # can be set twice over a request retry.
        data = io.BytesIO(b"foo")
        env = {}
        request = LaunchpadBrowserRequest(data, env)
        request.setInWSGIEnvironment("key", "first value")
        new_request = request.retry()
        new_request.setInWSGIEnvironment("key", "second value")
        self.assertEqual(new_request._orig_env["key"], "second value")


class TestApplicationServerSettingRequestFactory(TestCase):
    """Tests for the ApplicationServerSettingRequestFactory."""

    def test___call___should_set_HTTPS_env_on(self):
        # Ensure that the factory sets the HTTPS variable in the request
        # when the protocol is https.
        factory = ApplicationServerSettingRequestFactory(
            LaunchpadBrowserRequest, "launchpad.test", "https", 443
        )
        request = factory(io.BytesIO(), {"HTTP_HOST": "launchpad.test"})
        self.assertEqual(
            request.get("HTTPS"), "on", "factory didn't set the HTTPS env"
        )
        # This is a sanity check ensuring that effect of this works as
        # expected with the Zope request implementation.
        self.assertEqual(request.getURL(), "https://launchpad.test")

    def test___call___should_not_set_HTTPS(self):
        # Ensure that the factory doesn't put an HTTPS variable in the
        # request when the protocol is http.
        factory = ApplicationServerSettingRequestFactory(
            LaunchpadBrowserRequest, "launchpad.test", "http", 80
        )
        request = factory(io.BytesIO(), {})
        self.assertEqual(
            request.get("HTTPS"), None, "factory should not have set HTTPS env"
        )


class TestVhostWebserviceFactory(WebServiceTestCase):
    class VHostTestBrowserRequest(LaunchpadBrowserRequest):
        pass

    class VHostTestPublication(LaunchpadBrowserRequest):
        pass

    def setUp(self):
        super().setUp()
        # XXX We have to use a real hostname.
        self.factory = VHostWebServiceRequestPublicationFactory(
            "bugs", self.VHostTestBrowserRequest, self.VHostTestPublication
        )

    def wsgi_env(self, path, method="GET"):
        """Simulate a WSGI application environment."""
        return {
            "PATH_INFO": path,
            "HTTP_HOST": "bugs.launchpad.test",
            "REQUEST_METHOD": method,
        }

    @property
    def api_path(self):
        """Requests to this path should be treated as API requests."""
        return "/" + getUtility(IWebServiceConfiguration).path_override

    @property
    def non_api_path(self):
        """Requests to this path should not be treated as API requests."""
        return "/foo"

    def test_factory_produces_webservice_objects(self):
        """The factory should produce WebService request and publication
        objects for requests to the /api root URL.
        """
        env = self.wsgi_env(self.api_path)

        # Necessary preamble and sanity check.  We need to call
        # the factory's canHandle() method with an appropriate
        # WSGI environment before it can produce a request object for us.
        self.assertTrue(
            self.factory.canHandle(env),
            "Sanity check: The factory should be able to handle requests.",
        )

        wrapped_factory, publication_factory = self.factory()

        # We need to unwrap the real request factory.
        request_factory = wrapped_factory.requestfactory

        self.assertEqual(
            request_factory,
            WebServiceClientRequest,
            "Requests to the /api path should return a WebService "
            "request object.",
        )
        self.assertEqual(
            publication_factory,
            WebServicePublication,
            "Requests to the /api path should return a WebService "
            "publication object.",
        )

    def test_factory_produces_normal_request_objects(self):
        """The factory should return the request and publication factories
        specified in it's constructor if the request is not bound for the
        web service.
        """
        env = self.wsgi_env(self.non_api_path)
        self.assertTrue(
            self.factory.canHandle(env),
            "Sanity check: The factory should be able to handle requests.",
        )

        wrapped_factory, publication_factory = self.factory()

        # We need to unwrap the real request factory.
        request_factory = wrapped_factory.requestfactory

        self.assertEqual(
            request_factory,
            self.VHostTestBrowserRequest,
            "Requests to normal paths should return a VHostTest "
            "request object.",
        )
        self.assertEqual(
            publication_factory,
            self.VHostTestPublication,
            "Requests to normal paths should return a VHostTest "
            "publication object.",
        )

    def test_factory_processes_webservice_http_methods(self):
        """The factory should accept the HTTP methods for requests that
        should be processed by the web service.
        """
        allowed_methods = WebServiceRequestPublicationFactory.default_methods

        for method in allowed_methods:
            env = self.wsgi_env(self.api_path, method)
            self.assertTrue(self.factory.canHandle(env), "Sanity check")
            # Returns a tuple of (request_factory, publication_factory).
            rfactory, pfactory = self.factory.checkRequest(env)
            self.assertIsNone(
                rfactory,
                "The '%s' HTTP method should be handled by the factory."
                % method,
            )

    def test_factory_rejects_normal_http_methods(self):
        """The factory should reject some HTTP methods for requests that
        are *not* bound for the web service.

        This includes methods like 'PUT' and 'PATCH'.
        """
        vhost_methods = VirtualHostRequestPublicationFactory.default_methods
        ws_methods = WebServiceRequestPublicationFactory.default_methods

        denied_methods = set(ws_methods) - set(vhost_methods)

        for method in denied_methods:
            env = self.wsgi_env(self.non_api_path, method)
            self.assertTrue(self.factory.canHandle(env), "Sanity check")
            # Returns a tuple of (request_factory, publication_factory).
            rfactory, pfactory = self.factory.checkRequest(env)
            self.assertIsNotNone(
                rfactory,
                "The '%s' HTTP method should be rejected by the factory."
                % method,
            )

    def test_factory_understands_webservice_paths(self):
        """The factory should know if a path is directed at a web service
        resource path.
        """
        # This is a sanity check, so I can write '/api/foo' instead
        # of PATH_OVERRIDE + '/foo' in my tests.  The former's
        # intention is clearer.
        self.assertEqual(
            getUtility(IWebServiceConfiguration).path_override,
            "api",
            "Sanity check: The web service path override should be 'api'.",
        )

        self.assertTrue(
            self.factory.isWebServicePath("/api"),
            "The factory should handle URLs that start with /api.",
        )

        self.assertTrue(
            self.factory.isWebServicePath("/api/foo"),
            "The factory should handle URLs that start with /api.",
        )

        self.assertFalse(
            self.factory.isWebServicePath("/foo"),
            "The factory should not handle URLs that do not start with "
            "/api.",
        )

        self.assertFalse(
            self.factory.isWebServicePath("/"),
            "The factory should not handle URLs that do not start with "
            "/api.",
        )

        self.assertFalse(
            self.factory.isWebServicePath("/apifoo"),
            "The factory should not handle URLs that do not start with "
            "/api.",
        )

        self.assertFalse(
            self.factory.isWebServicePath("/foo/api"),
            "The factory should not handle URLs that do not start with "
            "/api.",
        )


class TestWebServiceRequestTraversal(WebServiceTestCase):
    testmodule_objects = [IGenericEntry, IGenericCollection]

    def setUp(self):
        super().setUp()

        # For this test we need to make the URL "/foo" resolve to a
        # resource.  To this end, we'll define a top-level collection
        # named 'foo'.
        @implementer(IGenericCollection)
        class GenericCollection:
            pass

        class MyRootResource(RootResource):
            def _build_top_level_objects(self):
                return ({"foo": (IGenericEntry, GenericCollection())}, {})

        getGlobalSiteManager().registerUtility(
            MyRootResource(), IServiceRootResource
        )

    def test_traversal_of_api_path_urls(self):
        """Requests that have /api at the root of their path should trim
        the 'api' name from the traversal stack.
        """
        # First, we need to forge a request to the API.
        data = ""
        config = getUtility(IWebServiceConfiguration)
        api_url = "/" + config.path_override + "/" + "1.0" + "/" + "foo"
        env = {"PATH_INFO": api_url}
        request = config.createRequest(data, env)

        stack = request.getTraversalStack()
        self.assertTrue(
            config.path_override in stack,
            "Sanity check: the API path should show up in the request's "
            "traversal stack: %r" % stack,
        )

        request.traverse(None)

        stack = request.getTraversalStack()
        self.assertFalse(
            config.path_override in stack,
            "Web service paths should be dropped from the webservice "
            "request traversal stack: %r" % stack,
        )


class TestWebServiceRequest(WebServiceTestCase):
    def test_application_url(self):
        """Requests to the /api path should return the original request's
        host, not api.launchpad.net.
        """
        # Simulate a request to bugs.launchpad.net/api
        server_url = "http://bugs.launchpad.test"
        env = {
            "PATH_INFO": "/api/devel",
            "SERVER_URL": server_url,
            "HTTP_HOST": "bugs.launchpad.test",
        }

        # WebServiceTestRequest will suffice, as it too should conform to
        # the Same Origin web browser policy.
        request = WebServiceTestRequest(environ=env, version="1.0")
        self.assertEqual(request.getApplicationURL(), server_url)

    def test_response_should_vary_based_on_content_type(self):
        request = WebServiceClientRequest(io.BytesIO(b""), {})
        self.assertEqual(request.response.getHeader("Vary"), "Accept")


class TestProtocolErrorPublication(TestCase):
    layer = DatabaseFunctionalLayer

    def test_405_options_does_not_raise(self):
        """OPTIONS 405 errors should not raise."""
        publication = ProtocolErrorPublication(405, {"Allow": "GET, POST"})

        env = {"REQUEST_METHOD": "OPTIONS"}
        request = LaunchpadBrowserRequest(io.BytesIO(b""), env)
        request.setPublication(publication)
        request = publish(request, handle_errors=False)

        self.assertEqual(hasattr(request, "oops"), False)
        self.assertEqual(request.response.getStatus(), 405)
        self.assertEqual(request.response.getHeader("Allow"), "GET, POST")


class TestBasicLaunchpadRequest(TestCase):
    """Tests for the base request class"""

    layer = FunctionalLayer

    def test_baserequest_response_should_vary(self):
        """Test that our base response has a proper vary header."""
        request = LaunchpadBrowserRequest(io.BytesIO(b""), {})
        self.assertEqual(
            request.response.getHeader("Vary"), "Cookie, Authorization"
        )

    def test_baserequest_response_should_vary_after_retry(self):
        """Test that our base response has a proper vary header."""
        request = LaunchpadBrowserRequest(io.BytesIO(b""), {})
        retried_request = request.retry()
        self.assertEqual(
            retried_request.response.getHeader("Vary"), "Cookie, Authorization"
        )

    def test_baserequest_security_headers(self):
        response = LaunchpadBrowserRequest(io.BytesIO(b""), {}).response
        self.assertEqual(
            response.getHeader("Content-Security-Policy"),
            "frame-ancestors 'self';",
        )
        self.assertEqual(response.getHeader("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(
            response.getHeader("X-Content-Type-Options"), "nosniff"
        )
        self.assertEqual(
            response.getHeader("X-XSS-Protection"), "1; mode=block"
        )
        self.assertEqual(
            response.getHeader("Strict-Transport-Security"), "max-age=15552000"
        )

    def test_baserequest_revision_header(self):
        response = LaunchpadBrowserRequest(io.BytesIO(b""), {}).response
        self.assertEqual(
            versioninfo.revision, response.getHeader("X-Launchpad-Revision")
        )

    def test_baserequest_recovers_from_bad_path_info_encoding(self):
        # The request object recodes PATH_INFO to ensure sane_environment
        # does not raise a UnicodeDecodeError when LaunchpadBrowserRequest
        # is instantiated.  This is only relevant on Python 2: PATH_INFO is
        # required to be a native string, which on Python 3 is already
        # Unicode, so the recoding issue doesn't arise.
        bad_path = b"fnord/trunk\xE4"
        env = {"PATH_INFO": bad_path}
        request = LaunchpadBrowserRequest(io.BytesIO(b""), env)
        self.assertEqual("fnord/trunk\ufffd", request.getHeader("PATH_INFO"))

    def test_baserequest_preserves_path_info_unicode(self):
        # If the request object receives PATH_INFO as Unicode, it is passed
        # through unchanged.  This is only relevant on Python 3: PATH_INFO
        # is required to be a native string, which on Python 2 is bytes.
        # (As explained in BasicLaunchpadRequest.__init__, non-ASCII
        # characters will be rejected later during traversal.)
        bad_path = "fnord/trunk\xE4"
        env = {"PATH_INFO": bad_path}
        request = LaunchpadBrowserRequest(io.BytesIO(b""), env)
        self.assertEqual("fnord/trunk\xE4", request.getHeader("PATH_INFO"))

    def test_baserequest_logging_context_no_host_header(self):
        Context.new()
        LaunchpadBrowserRequest(io.BytesIO(b""), {})
        self.assertNotIn("host", logging_context.flat)

    def test_baserequest_logging_context_host_header(self):
        Context.new()
        env = {"HTTP_HOST": "launchpad.test"}
        LaunchpadBrowserRequest(io.BytesIO(b""), env)
        self.assertEqual("launchpad.test", logging_context.flat["host"])

    def test_baserequest_logging_context_https(self):
        Context.new()
        LaunchpadBrowserRequest(io.BytesIO(b""), {"HTTPS": "on"})
        self.assertEqual("https", logging_context.flat["scheme"])

    def test_baserequest_logging_context_http(self):
        Context.new()
        LaunchpadBrowserRequest(io.BytesIO(b""), {})
        self.assertEqual("http", logging_context.flat["scheme"])

    def test_request_with_invalid_query_string_recovers(self):
        # When the query string has invalid utf-8, it is decoded with
        # replacement.
        # PEP 3333 requires environment variables to be native strings, so
        # we can't actually get a bytes object in here on Python 3 (both
        # because the WSGI runner will never put it there, and because
        # parse_qs would crash if we did).  Test the next best thing, namely
        # percent-encoded invalid UTF-8.
        env = {"QUERY_STRING": "field.title=subproc%E9s "}
        request = LaunchpadBrowserRequest(io.BytesIO(b""), env)
        self.assertEqual(
            ["subproc\ufffds "], request.query_string_params["field.title"]
        )


class LaunchpadBrowserResponseHeaderInjection(TestCase):
    """Test that LaunchpadBrowserResponse rejects header injection attempts.

    Applications should reject data that they cannot safely serialise, but
    most WSGI containers don't complain when header names or values contain
    CR or LF, so we reject them before they're added.
    """

    def test_setHeader_good(self):
        response = LaunchpadBrowserResponse()
        response.setHeader("Foo", "bar")
        self.assertEqual({"foo": ["bar"]}, response._headers)

    def test_setHeader_bad_name(self):
        response = LaunchpadBrowserResponse()
        self.assertRaises(ValueError, response.setHeader, "Foo\n", "bar")
        self.assertRaises(ValueError, response.setHeader, "Foo\r", "bar")
        self.assertRaises(ValueError, response.setHeader, "Foo:", "bar")
        self.assertEqual({}, response._headers)

    def test_setHeader_bad_value(self):
        response = LaunchpadBrowserResponse()
        self.assertRaises(ValueError, response.setHeader, "Foo", "bar\n")
        self.assertRaises(ValueError, response.setHeader, "Foo", "bar\r")
        self.assertEqual({}, response._headers)

    def test_addHeader_good(self):
        response = LaunchpadBrowserResponse()
        response.addHeader("Foo", "bar")
        self.assertEqual({"Foo": ["bar"]}, response._headers)

    def test_addHeader_bad_name(self):
        response = LaunchpadBrowserResponse()
        self.assertRaises(ValueError, response.addHeader, "Foo\n", "bar")
        self.assertRaises(ValueError, response.addHeader, "Foo\r", "bar")
        self.assertRaises(ValueError, response.addHeader, "Foo:", "bar")
        self.assertEqual({}, response._headers)

    def test_addHeader_bad_value(self):
        response = LaunchpadBrowserResponse()
        self.assertRaises(ValueError, response.addHeader, "Foo", "bar\n")
        self.assertRaises(ValueError, response.addHeader, "Foo", "bar\r")
        self.assertEqual({}, response._headers)


class TestFeedsBrowserRequest(TestCase):
    """Tests for `FeedsBrowserRequest`."""

    def test_not_strict_transport_security(self):
        # Feeds are served over HTTP, so no Strict-Transport-Security
        # header is sent.
        response = FeedsBrowserRequest(io.BytesIO(b""), {}).response
        self.assertIs(None, response.getHeader("Strict-Transport-Security"))


class TestPrivateXMLRPCRequest(TestCase):
    """Tests for `PrivateXMLRPCRequest`."""

    def test_not_strict_transport_security(self):
        # Private XML-RPC is served over HTTP, so no Strict-Transport-Security
        # header is sent.
        response = PrivateXMLRPCRequest(io.BytesIO(b""), {}).response
        self.assertIs(None, response.getHeader("Strict-Transport-Security"))


class TestLaunchpadBrowserRequestMixin:
    """Tests for `LaunchpadBrowserRequestMixin`.

    As `LaunchpadBrowserRequestMixin` is a mixin, it needs to be tested when
    mixed into another class, hence why this does not inherit from `TestCase`.
    """

    request_factory = None  # Specify in subclasses.

    def test_is_ajax_false(self):
        """Normal requests do not define HTTP_X_REQUESTED_WITH."""
        request = self.request_factory(io.BytesIO(b""), {})

        self.assertFalse(request.is_ajax)

    def test_is_ajax_true(self):
        """Requests with HTTP_X_REQUESTED_WITH set are ajax requests."""
        request = self.request_factory(
            io.BytesIO(b""),
            {
                "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
            },
        )

        self.assertTrue(request.is_ajax)

    def test_getURL(self):
        """
        getURL() overrides HTTPRequest.getURL(), but behaves identically by
        default.
        """
        environ = {
            "SERVER_URL": "http://geturl.example.com",
            "SCRIPT_NAME": "/sabbra/cadabra",
            "QUERY_STRING": "tuesday=gone",
        }
        request = self.request_factory(io.BytesIO(b""), environ)
        self.assertEqual(
            "http://geturl.example.com/sabbra/cadabra", request.getURL()
        )
        self.assertEqual(
            "http://geturl.example.com/sabbra", request.getURL(level=1)
        )
        self.assertEqual("/sabbra/cadabra", request.getURL(path_only=True))

    def test_getURL_include_query(self):
        """
        getURL() overrides HTTPRequest.getURL(), but appends the query string
        if include_query=True.
        """
        environ = {
            "SERVER_URL": "http://geturl.example.com",
            "SCRIPT_NAME": "/sabbra/cadabra",
            "QUERY_STRING": "tuesday=gone",
        }
        request = self.request_factory(io.BytesIO(b""), environ)
        self.assertEqual(
            "http://geturl.example.com/sabbra/cadabra?tuesday=gone",
            request.getURL(include_query=True),
        )
        self.assertEqual(
            "http://geturl.example.com/sabbra?tuesday=gone",
            request.getURL(include_query=True, level=1),
        )
        self.assertEqual(
            "/sabbra/cadabra?tuesday=gone",
            request.getURL(include_query=True, path_only=True),
        )


class TestLaunchpadBrowserRequestMixinWithLaunchpadBrowserRequest(
    TestLaunchpadBrowserRequestMixin, TestCase
):
    """
    Tests for `LaunchpadBrowserRequestMixin` as found in
    `LaunchpadBrowserRequest`.
    """

    request_factory = LaunchpadBrowserRequest


class TestLaunchpadBrowserRequestMixinWithLaunchpadTestRequest(
    TestLaunchpadBrowserRequestMixin, TestCase
):
    """
    Tests for `LaunchpadBrowserRequestMixin` as found in
    `LaunchpadTestRequest`.
    """

    request_factory = LaunchpadTestRequest


class IThingSet(Interface):
    """Marker interface for a set of things."""


class IThing(Interface):
    """Marker interface for a thing."""


@implementer(IThing)
class Thing:
    pass


@implementer(IThingSet)
class ThingSet:
    pass


class TestLaunchpadBrowserRequest_getNearest(TestCase):
    def setUp(self):
        super().setUp()
        self.request = LaunchpadBrowserRequest("", {})
        self.thing_set = ThingSet()
        self.thing = Thing()

    def test_return_value(self):
        # .getNearest() returns a two-tuple with the object and the interface
        # that matched. The second item in the tuple is useful when multiple
        # interfaces are passed to getNearest().
        request = self.request
        request.traversed_objects.extend([self.thing_set, self.thing])
        self.assertEqual(request.getNearest(IThing), (self.thing, IThing))
        self.assertEqual(
            request.getNearest(IThingSet), (self.thing_set, IThingSet)
        )

    def test_multiple_traversed_objects_with_common_interface(self):
        # If more than one object of a particular interface type has been
        # traversed, the most recently traversed one is returned.
        thing2 = Thing()
        self.request.traversed_objects.extend(
            [self.thing_set, self.thing, thing2]
        )
        self.assertEqual(self.request.getNearest(IThing), (thing2, IThing))

    def test_interface_not_traversed(self):
        # If a particular interface has not been traversed, the tuple
        # (None, None) is returned.
        self.request.traversed_objects.extend([self.thing_set])
        self.assertEqual(self.request.getNearest(IThing), (None, None))


class TestLaunchpadBrowserRequest(TestCase):
    def prepareRequest(self, form):
        """Return a `LaunchpadBrowserRequest` with the given form.

        Also set the accepted charset to 'utf-8'.
        """
        request = LaunchpadBrowserRequest("", form)
        request.charsets = ["utf-8"]
        return request

    def test_query_string_params_on_get(self):
        """query_string_params is populated from the QUERY_STRING during
        GET requests."""
        request = self.prepareRequest({"QUERY_STRING": "a=1&b=2&c=3"})
        self.assertEqual(
            {"a": ["1"], "b": ["2"], "c": ["3"]},
            request.query_string_params,
            "The query_string_params dict is populated from the "
            "QUERY_STRING during GET requests.",
        )

    def test_query_string_params_on_post(self):
        """query_string_params is populated from the QUERY_STRING during
        POST requests."""
        request = self.prepareRequest(
            {"QUERY_STRING": "a=1&b=2&c=3", "REQUEST_METHOD": "POST"}
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            {"a": ["1"], "b": ["2"], "c": ["3"]},
            request.query_string_params,
            "The query_string_params dict is populated from the "
            "QUERY_STRING during POST requests.",
        )

    def test_query_string_params_empty(self):
        """The query_string_params dict is always empty when QUERY_STRING
        is empty, None or undefined.
        """
        request = self.prepareRequest({"QUERY_STRING": ""})
        self.assertEqual({}, request.query_string_params)
        request = self.prepareRequest({"QUERY_STRING": None})
        self.assertEqual({}, request.query_string_params)
        request = self.prepareRequest({})
        self.assertEqual({}, request.query_string_params)

    def test_query_string_params_multi_value(self):
        """The query_string_params dict can include multiple values
        for a parameter."""
        request = self.prepareRequest({"QUERY_STRING": "a=1&a=2&b=3"})
        self.assertEqual(
            {"a": ["1", "2"], "b": ["3"]},
            request.query_string_params,
            "The query_string_params dict correctly interprets multiple "
            "values for the same key in a query string.",
        )

    def test_query_string_params_unicode(self):
        # Encoded query string parameters are properly decoded.
        request = self.prepareRequest({"QUERY_STRING": "a=%C3%A7"})
        self.assertEqual(
            {"a": ["\xe7"]},
            request.query_string_params,
            "The query_string_params dict correctly interprets encoded "
            "parameters.",
        )


# XXX: ilkeremrekoc 2025-03-12
# This test case is for the overridden processInputs inside
# LaunchpadBrowserRequest. The reason for the overriding is connected to
# the changes in multipart package (which now only accepts CRLF endings) and
# the original state of apport package (which only sent LF endings)
# (see https://bugs.launchpad.net/ubuntu/+source/apport/+bug/2096327). Thus,
# we add the overridden method and this test case temporarily until the apport
# package can be SRU'd in a sufficient percantage of Ubuntu users. Both of
# these can be removed once the prerequisite is fulfilled.
class TestLaunchpadBrowserRequestProcessInputs(TestCase):
    layer = FunctionalLayer

    def setUp(self):
        super().setUp()

        self.body_template_str = dedent(
            """\
            Content-Type: multipart/mixed; \
                boundary="===============4913381966751462219=="
            MIME-Version: 1.0
            \n\
            --===============4913381966751462219==
            Content-Type: text/plain; charset="us-ascii"
            MIME-Version: 1.0
            Content-Transfer-Encoding: 7bit
            Content-Disposition: form-data; name="FORM_SUBMIT"
            \n\
            1
            --===============4913381966751462219==
            Content-Type: application/octet-stream
            MIME-Version: 1.0
            Content-Disposition: form-data; name="field.blob"; \
                filename="x"
            \n\
            %s
            \n\
            --===============4913381966751462219==--
            \n\
            """
        )

        self.standard_blob = dedent(
            """
            This is for a test....
            \n\
            The second line of test
            \n\
            The third
            """
        )

        self.body_str = self.body_template_str % self.standard_blob
        self.body_bytes = self.body_str.encode("ASCII")

        self.environ = {
            "PATH_INFO": "/+storeblob",
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": "multipart/form-data; \
                boundary================4913381966751462219==",
        }

    def prepareRequest_lineBreakTest(self, body_bytes, environ):
        """Return a `LaunchpadBrowserRequest` with the given form.

        Also set the accepted charset to 'utf-8'.
        """

        body_stream = io.BytesIO(body_bytes)
        input_stream = HTTPInputStream(body_stream, environ)

        request = LaunchpadBrowserRequest(input_stream, environ)
        request.charsets = ["utf-8"]
        return request

    def _assessProcessInputsResult(self, request):
        """
        Assesses whether the request has correctly parsed the blob field.

        Passing in this case means the request's form is correctly parsed
        and it only includes CRLF endings.
        """

        self.assertIn("field.blob", request.form)

        result = request.form["field.blob"].read()

        self.assertIn(b"\r\n", result)
        self.assertNotRegex(result, b"[^\r]\n")

    def test_processInputs_with_LF(self):
        """
        processInputs parses request bodies with LF line-breaks into CRLF.
        """

        request = self.prepareRequest_lineBreakTest(
            self.body_bytes, self.environ
        )
        request.processInputs()

        self._assessProcessInputsResult(request)

    def test_processInputs_with_CR(self):
        """
        processInputs parses request bodies with CR line-breaks into CRLF.
        """

        body_bytes_with_CR = self.body_bytes.replace(b"\n", b"\r")

        request = self.prepareRequest_lineBreakTest(
            body_bytes_with_CR, self.environ
        )
        request.processInputs()

        self._assessProcessInputsResult(request)

    def test_processInputs_with_CRLF(self):
        """
        processInputs passes request bodies with CRLF line-breaks.
        """

        body_bytes_with_CRLF = self.body_bytes.replace(b"\n", b"\r\n")

        request = self.prepareRequest_lineBreakTest(
            body_bytes_with_CRLF, self.environ
        )
        request.processInputs()

        self._assessProcessInputsResult(request)

    def test_processInputs_with_multiple_buffer_runs(self):
        """
        processInputs should work even when the message overflows the buffer
        """

        request = self.prepareRequest_lineBreakTest(
            self.body_bytes, self.environ
        )
        request.buffer_size = 4

        request.processInputs()

        self._assessProcessInputsResult(request)

    def test_processInputs_with_cut_CRLF(self):
        """
        processInputs should work even when a CRLF is cut in the middle
        """

        body_bytes_with_CRLF = self.body_bytes.replace(b"\n", b"\r\n")
        CR_index = body_bytes_with_CRLF.index(b"\r")

        request = self.prepareRequest_lineBreakTest(
            body_bytes_with_CRLF, self.environ
        )
        request.buffer_size = len(body_bytes_with_CRLF[: CR_index + 1])

        request.processInputs()

        self._assessProcessInputsResult(request)

    def test_processInputs_different_environ_with_LF(self):
        """
        processInputs shouldn't work outside its domain. If LF line-breaks are
        present outside of the apport package domain, the parsing should fail.
        """

        different_environ = self.environ.copy()
        different_environ["PATH_INFO"] = ""

        request = self.prepareRequest_lineBreakTest(
            self.body_bytes, different_environ
        )

        request.processInputs()

        self.assertNotIn("field.blob", request.form)

    def test_processInputs_different_environ_with_CR(self):
        """
        processInputs shouldn't work outside its domain. If CR line-breaks are
        present outside of the apport package domain, the parsing should fail.
        """

        body_bytes_with_CR = self.body_bytes.replace(b"\n", b"\r")

        different_environ = self.environ.copy()
        different_environ["PATH_INFO"] = ""

        request = self.prepareRequest_lineBreakTest(
            body_bytes_with_CR, different_environ
        )

        request.processInputs()

        self.assertNotIn("field.blob", request.form)

    def test_processInputs_different_environ_with_CRLF(self):
        """
        processInputs should work with CRLF everytime, regardless of domain.
        """

        body_bytes_with_CRLF = self.body_bytes.replace(b"\n", b"\r\n")

        different_environ = self.environ.copy()
        different_environ["PATH_INFO"] = ""

        request = self.prepareRequest_lineBreakTest(
            body_bytes_with_CRLF, different_environ
        )

        request.processInputs()

        self._assessProcessInputsResult(request)

    def test_processInputs_content_length(self):
        """
        processInputs should change the content length in proportion to the
        number of LFs.
        """

        original_body_length = len(self.body_str)
        lf_count = self.body_str.count("\n")

        environ = self.environ.copy()
        environ["CONTENT_LENGTH"] = original_body_length

        body_bytes = self.body_str.encode("ASCII")

        request = self.prepareRequest_lineBreakTest(body_bytes, environ)

        request.processInputs()

        # Note that processInputs change the values within "_environ"
        # internally.
        self.assertEqual(
            original_body_length + lf_count, request._environ["CONTENT_LENGTH"]
        )


class TestLaunchpadBrowserRequestProcessInputsLarge(TestCase):
    # Test case for the large inputs slotted into ProcessInputs method

    layer = FunctionalLayer

    def setUp(self):
        super().setUp()

        self.body_template_str = dedent(
            """\
            Content-Type: multipart/mixed; \
                boundary="===============4913381966751462219=="
            MIME-Version: 1.0
            \n\
            --===============4913381966751462219==
            Content-Type: text/plain; charset="us-ascii"
            MIME-Version: 1.0
            Content-Transfer-Encoding: 7bit
            Content-Disposition: form-data; name="FORM_SUBMIT"
            \n\
            1
            --===============4913381966751462219==
            Content-Type: application/octet-stream
            MIME-Version: 1.0
            Content-Disposition: form-data; name="field.blob"; \
                filename="x"
            \n\
            %s
            \n\
            --===============4913381966751462219==--
            \n\
            """
        )

        self.standard_blob = dedent(
            """
            This is for a test....
            \n\
            The second line of test
            \n\
            The third
            """
        )

        self.body_str = self.body_template_str % self.standard_blob
        self.body_bytes = self.body_str.encode("ASCII")

        self.environ = {
            "PATH_INFO": "/+storeblob",
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": "multipart/form-data; \
                boundary================4913381966751462219==",
        }

    def prepareRequest_lineBreakTest(self, body_bytes, environ):
        """Return a `LaunchpadBrowserRequest` with the given form.

        Also set the accepted charset to 'utf-8'.
        """

        body_stream = io.BytesIO(body_bytes)
        input_stream = HTTPInputStream(body_stream, environ)

        request = LaunchpadBrowserRequest(input_stream, environ)
        request.charsets = ["utf-8"]
        return request

    def _assessProcessInputsResult(self, request):
        """
        Assesses whether the request has correctly parsed the blob field.

        Passing in this case means the request's form is correctly parsed
        and it only includes CRLF endings.
        """

        self.assertIn("field.blob", request.form)

        result = request.form["field.blob"].read()

        self.assertIn(b"\r\n", result)
        self.assertNotRegex(result, b"[^\r]\n")

    def test_processInputs_above_buffer_size_limit(self):
        """
        processInputs should work even when the initial message overflows the
        buffer.
        """

        template_size = len(self.body_template_str) - 2  # Subtracting "%s"
        buffer_size = LaunchpadBrowserRequest.buffer_size

        space_to_fill = buffer_size - template_size
        blob_size = len(self.standard_blob)

        # Make the whole request above the buffer size limit
        multiply_blob = (space_to_fill // blob_size) + 1
        large_blob = self.standard_blob * multiply_blob

        large_body_str = self.body_template_str % large_blob
        large_body_bytes = large_body_str.encode("ASCII")

        lf_count = large_body_str.count("\n")

        environ = self.environ.copy()
        environ["CONTENT_LENGTH"] = len(large_body_str)

        request = self.prepareRequest_lineBreakTest(large_body_bytes, environ)

        old_content_length = request._environ["CONTENT_LENGTH"]

        request.processInputs()

        self.assertEqual(
            old_content_length + lf_count, request._environ["CONTENT_LENGTH"]
        )
        self._assessProcessInputsResult(request)

    def test_processInputs_just_below_buffer_size_limit(self):
        """
        processInputs should work even when the message gets overflowed after
        initiation.
        """

        template_size = len(self.body_template_str) - 2  # Subtracting "%s"
        buffer_size = LaunchpadBrowserRequest.buffer_size

        space_to_fill = buffer_size - template_size
        blob_size = len(self.standard_blob)

        # Make the whole request above the buffer size limit
        multiply_blob = space_to_fill // blob_size
        large_blob = self.standard_blob * multiply_blob

        large_body_str = self.body_template_str % large_blob
        large_body_bytes = large_body_str.encode("ASCII")

        lf_count = large_body_str.count("\n")

        environ = self.environ.copy()
        environ["CONTENT_LENGTH"] = len(large_body_str)

        request = self.prepareRequest_lineBreakTest(large_body_bytes, environ)

        old_content_length = request._environ["CONTENT_LENGTH"]

        request.processInputs()

        self.assertEqual(
            old_content_length + lf_count, request._environ["CONTENT_LENGTH"]
        )
        self._assessProcessInputsResult(request)

    def test_processInputs_blob_only_LF(self):
        """
        processInputs should work even when the whole blob within the message
        is gibberish, or made up of entirely LFs and is extremely large.
        """

        buffer_size = LaunchpadBrowserRequest.buffer_size

        lf_blob = "\n" * buffer_size * 2

        lf_body_str = self.body_template_str % lf_blob
        lf_body_bytes = lf_body_str.encode("ASCII")

        lf_count = lf_body_str.count("\n")

        environ = self.environ.copy()
        environ["CONTENT_LENGTH"] = len(lf_body_str)

        request = self.prepareRequest_lineBreakTest(lf_body_bytes, environ)

        old_content_length = request._environ["CONTENT_LENGTH"]

        request.processInputs()

        self.assertEqual(
            old_content_length + lf_count, request._environ["CONTENT_LENGTH"]
        )
        self._assessProcessInputsResult(request)


class TestWebServiceRequestToBrowserRequest(WebServiceTestCase):
    def test_unicode_path_info(self):
        web_service_request = WebServiceTestRequest(
            PATH_INFO="/api/devel\u1234".encode()
        )
        browser_request = web_service_request_to_browser_request(
            web_service_request
        )
        self.assertEqual(
            web_service_request.get("PATH_INFO"),
            browser_request.get("PATH_INFO"),
        )


class LoggingTransaction:
    def __init__(self):
        self.log = []

    def commit(self):
        self.log.append("COMMIT")

    def abort(self):
        self.log.append("ABORT")


class TestWebServiceAccessTokensBase:
    """Test personal access tokens for the webservice.

    These are bearer tokens with an owner, a context, and some scopes.  We
    can authenticate using one of these, and it will be recorded in the
    interaction extras.
    """

    layer = DatabaseFunctionalLayer

    def test_valid(self):
        owner = self.factory.makePerson()
        secret, token = self.factory.makeAccessToken(
            owner=owner,
            target=self.makeTarget(owner=owner),
            scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
        )
        self.assertIsNone(removeSecurityProxy(token).date_last_used)
        transaction.commit()
        logout()

        request, publication = get_request_and_publication(
            "api.launchpad.test",
            "POST",
            extra_environment={"HTTP_AUTHORIZATION": "Token %s" % secret},
        )
        newInteraction(request)
        principal = publication.getPrincipal(request)
        request.setPrincipal(principal)
        self.assertEqual(owner, principal.person)
        self.assertEqual(token, get_interaction_extras().access_token)
        self.assertIsNotNone(token.date_last_used)
        self.assertThat(
            logging_context.flat,
            ContainsDict(
                {
                    "access_token_id": Equals(removeSecurityProxy(token).id),
                    "access_token_scopes": Equals("repository:build_status"),
                }
            ),
        )

        # token.date_last_used is still up to date even if the transaction
        # is rolled back.
        date_last_used = token.date_last_used
        transaction.abort()
        self.assertEqual(date_last_used, token.date_last_used)

    def test_expired(self):
        owner = self.factory.makePerson()
        secret, token = self.factory.makeAccessToken(
            owner=owner,
            target=self.makeTarget(owner=owner),
            date_expires=datetime.now(timezone.utc) - timedelta(days=1),
        )
        transaction.commit()

        request, publication = get_request_and_publication(
            "api.launchpad.test",
            "POST",
            extra_environment={"HTTP_AUTHORIZATION": "Token %s" % secret},
        )
        self.assertRaisesWithContent(
            TokenException,
            "Expired access token.",
            publication.getPrincipal,
            request,
        )

    def test_unknown(self):
        request, publication = get_request_and_publication(
            "api.launchpad.test",
            "POST",
            extra_environment={"HTTP_AUTHORIZATION": "Token nonexistent"},
        )
        self.assertRaisesWithContent(
            TokenException,
            "Unknown access token.",
            publication.getPrincipal,
            request,
        )

    def test_inactive_account(self):
        owner = self.factory.makePerson(account_status=AccountStatus.SUSPENDED)
        secret, token = self.factory.makeAccessToken(
            owner=owner, target=self.makeTarget(owner=owner)
        )
        transaction.commit()

        request, publication = get_request_and_publication(
            "api.launchpad.test",
            "POST",
            extra_environment={"HTTP_AUTHORIZATION": "Token %s" % secret},
        )
        self.assertRaisesWithContent(
            TokenException,
            "Inactive account.",
            publication.getPrincipal,
            request,
        )

    def _makeAccessTokenVerifiedRequest(self, **kwargs):
        secret, token = self.factory.makeAccessToken(**kwargs)
        transaction.commit()
        logout()

        request, publication = get_request_and_publication(
            "api.launchpad.test",
            "POST",
            extra_environment={"HTTP_AUTHORIZATION": "Token %s" % secret},
        )
        newInteraction(request)
        principal = publication.getPrincipal(request)
        request.setPrincipal(principal)

    def test_checkRequest_valid(self):
        target = self.makeTarget()
        self._makeAccessTokenVerifiedRequest(
            target=target,
            scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
        )
        getUtility(IWebServiceConfiguration).checkRequest(
            target, ["repository:build_status", "repository:another_scope"]
        )

    def test_checkRequest_contains_context(self):
        [ref] = self.factory.makeGitRefs()
        self._makeAccessTokenVerifiedRequest(
            target=ref.repository,
            scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
        )
        getUtility(IWebServiceConfiguration).checkRequest(
            ref, ["repository:build_status", "repository:another_scope"]
        )

    def test_checkRequest_bad_context(self):
        target = self.makeTarget()
        self._makeAccessTokenVerifiedRequest(
            target=target,
            scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
        )
        self.assertRaisesWithContent(
            Unauthorized,
            "Current authentication does not allow access to this object.",
            getUtility(IWebServiceConfiguration).checkRequest,
            self.factory.makeGitRepository(),
            ["repository:build_status"],
        )

    def test_checkRequest_unscoped_method(self):
        target = self.makeTarget()
        self._makeAccessTokenVerifiedRequest(
            target=target,
            scopes=[AccessTokenScope.REPOSITORY_BUILD_STATUS],
        )
        self.assertRaisesWithContent(
            Unauthorized,
            "Current authentication only allows calling scoped methods.",
            getUtility(IWebServiceConfiguration).checkRequest,
            target,
            None,
        )

    def test_checkRequest_wrong_scope(self):
        target = self.makeTarget()
        self._makeAccessTokenVerifiedRequest(
            target=target,
            scopes=[
                AccessTokenScope.REPOSITORY_BUILD_STATUS,
                AccessTokenScope.REPOSITORY_PUSH,
            ],
        )
        self.assertRaisesWithContent(
            Unauthorized,
            "Current authentication does not allow calling this method "
            "(one of these scopes is required: "
            "'repository:scope_1', 'repository:scope_2').",
            getUtility(IWebServiceConfiguration).checkRequest,
            target,
            ["repository:scope_1", "repository:scope_2"],
        )


class TestWebServiceAccessTokensGitRepository(
    TestWebServiceAccessTokensBase, TestCaseWithFactory
):
    def makeTarget(self, owner=None):
        return self.factory.makeGitRepository(owner=owner)


class TestWebServiceAccessTokensProject(
    TestWebServiceAccessTokensBase, TestCaseWithFactory
):
    def makeTarget(self, owner=None):
        return self.factory.makeProduct(owner=owner)


def test_suite():
    suite = unittest.TestSuite()
    suite.addTest(
        DocTestSuite(
            "lp.services.webapp.servers",
            optionflags=NORMALIZE_WHITESPACE | ELLIPSIS,
        )
    )
    suite.addTest(unittest.TestLoader().loadTestsFromName(__name__))
    return suite
