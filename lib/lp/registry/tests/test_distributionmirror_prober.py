# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""distributionmirror-prober tests."""

import http.client
import io
import logging
import os
import re
from datetime import datetime
from textwrap import dedent

import responses
import transaction
from fixtures import MockPatchObject
from lazr.uri import URI
from storm.locals import Store
from testtools.matchers import ContainsDict, Equals, MatchesStructure
from testtools.twistedsupport import (
    AsynchronousDeferredRunTest,
    AsynchronousDeferredRunTestForBrokenTwisted,
    assert_fails_with,
)
from twisted.internet import defer, reactor, ssl
from twisted.python.failure import Failure
from twisted.web import server
from twisted.web.client import URI as TwistedWebClientURI
from twisted.web.client import BrowserLikePolicyForHTTPS
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.registry.interfaces.distributionmirror import (
    IDistributionMirrorSet,
    MirrorContent,
    MirrorStatus,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.model.distributionmirror import (
    DistributionMirror,
    MirrorCDImageDistroSeries,
    MirrorDistroArchSeries,
    MirrorDistroSeriesSource,
    MirrorProbeRecord,
)
from lp.registry.scripts import distributionmirror_prober
from lp.registry.scripts.distributionmirror_prober import (
    MAX_REDIRECTS,
    MIN_REQUEST_TIMEOUT_RATIO,
    MIN_REQUESTS_TO_CONSIDER_RATIO,
    ArchiveMirrorProberCallbacks,
    BadContentType,
    BadResponseCode,
    ConnectionSkipped,
    ContentTypeCheckingProberFactory,
    InfiniteLoopDetected,
    InvalidHTTPSCertificate,
    InvalidHTTPSCertificateSkipped,
    LoggingMixin,
    MirrorCDImageProberCallbacks,
    MultiLock,
    ProberFactory,
    ProberTimeout,
    RedirectAwareProberFactory,
    RedirectAwareProberProtocol,
    RedirectToDifferentFile,
    RequestManager,
    ResponseFailure,
    UnknownURLSchemeAfterRedirect,
    _get_cdimage_file_list,
    _parse,
    probe_archive_mirror,
    probe_cdimage_mirror,
    should_skip_host,
)
from lp.registry.tests.distributionmirror_http_server import (
    DistributionMirrorTestHTTPServer,
    DistributionMirrorTestSecureHTTPServer,
)
from lp.services.config import config
from lp.services.daemons.tachandler import TacTestSetup
from lp.services.database.interfaces import IStore
from lp.services.httpproxy.connect_tunneling import TunnelError, TunnelingAgent
from lp.services.timeout import default_timeout
from lp.testing import (
    TestCase,
    TestCaseWithFactory,
    admin_logged_in,
    clean_up_reactor,
)
from lp.testing.layers import (
    LaunchpadZopelessLayer,
    TwistedLayer,
    ZopelessDatabaseLayer,
)
from lp.testing.script import run_script


class HTTPServerTestSetup(TacTestSetup):
    def setUpRoot(self):
        pass

    @property
    def root(self):
        return "/var/tmp"

    @property
    def tacfile(self):
        return os.path.join(
            self.daemon_directory, "distributionmirror_http_server.tac"
        )

    @property
    def pidfile(self):
        return os.path.join(self.root, "distributionmirror_http_server.pid")

    @property
    def logfile(self):
        return os.path.join(self.root, "distributionmirror_http_server.log")


class LocalhostWhitelistedHTTPSPolicy(BrowserLikePolicyForHTTPS):
    """HTTPS policy that bypasses SSL certificate check when doing requests
    to localhost.
    """

    def creatorForNetloc(self, hostname, port):
        # check if the hostname is in the the whitelist,
        # otherwise return the default policy
        if hostname == b"localhost":
            return ssl.CertificateOptions(verify=False)
        return super().creatorForNetloc(hostname, port)


class TestURLParser(TestCase):
    def test_defined_port(self):
        url = "http://foo.com:37/bar"
        self.assertEqual(("http", "foo.com", 37, "/bar"), _parse(url))

    def test_default_port_http(self):
        url = "http://foo.com/bar"
        self.assertEqual(("http", "foo.com", 80, "/bar"), _parse(url))

    def test_default_port_https(self):
        url = "https://foo.com/bar"
        self.assertEqual(("https", "foo.com", 443, "/bar"), _parse(url))

    def test_given_default_port(self):
        url = "https://foo.com/bar"
        self.assertEqual(
            ("https", "foo.com", 99, "/bar"), _parse(url, defaultPort=99)
        )


class TestBadResponseCode(TestCase):
    def test_str_includes_url(self):
        error = BadResponseCode(404, "http://example.com/foo")
        self.assertEqual(
            "Bad response code for http://example.com/foo: 404", str(error)
        )

    def test_str_without_url(self):
        error = BadResponseCode(404)
        self.assertEqual("Bad response code: 404", str(error))


class TestProberHTTPSProtocolAndFactory(TestCase):
    layer = TwistedLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        root = DistributionMirrorTestSecureHTTPServer()
        site = server.Site(root)
        site.displayTracebacks = False
        keys_path = os.path.join(config.root, "configs", "development")
        keys = ssl.DefaultOpenSSLContextFactory(
            os.path.join(keys_path, "launchpad.key"),
            os.path.join(keys_path, "launchpad.crt"),
        )
        self.listening_port = reactor.listenSSL(0, site, keys)

        self.addCleanup(self.listening_port.stopListening)

        # Change the default policy to accept localhost self-signed
        # certificates.
        for factory in (ProberFactory, RedirectAwareProberFactory):
            self.useFixture(
                MockPatchObject(
                    factory,
                    "https_agent_policy",
                    LocalhostWhitelistedHTTPSPolicy,
                )
            )

        self.port = self.listening_port.getHost().port

        self.urls = {
            "timeout": "https://localhost:%s/timeout" % self.port,
            "200": "https://localhost:%s/valid-mirror" % self.port,
            "500": "https://localhost:%s/error" % self.port,
            "404": "https://localhost:%s/invalid-mirror" % self.port,
        }
        self.pushConfig("launchpad", http_proxy=None)

        self.useFixture(
            MockPatchObject(distributionmirror_prober, "host_requests", {})
        )
        self.useFixture(
            MockPatchObject(distributionmirror_prober, "host_timeouts", {})
        )
        self.useFixture(
            MockPatchObject(
                distributionmirror_prober, "invalid_certificate_hosts", set()
            )
        )

    def _createProberAndProbe(self, url):
        prober = ProberFactory(url)
        return prober.probe()

    def test_timeout(self):
        prober = ProberFactory(self.urls["timeout"], timeout=0.5)
        d = prober.probe()
        return assert_fails_with(d, ProberTimeout)

    def test_500(self):
        d = self._createProberAndProbe(self.urls["500"])
        return assert_fails_with(d, BadResponseCode)

    def test_notfound(self):
        d = self._createProberAndProbe(self.urls["404"])
        d = assert_fails_with(d, BadResponseCode)

        def check_url(error):
            self.assertEqual(self.urls["404"], error.url)

        return d.addCallback(check_url)

    def test_multiple_failures(self):
        """Avoid defer.AlreadyCalledError when failWithTimeoutError is still
        scheduled after we already have the HTTP response.
        """
        prober = ProberFactory(self.urls["500"])
        d = prober.probe()
        reactor.callLater(0, prober.failWithTimeoutError)
        return assert_fails_with(d, BadResponseCode)

    def test_config_no_https_proxy(self):
        prober = ProberFactory(self.urls["200"])
        self.assertThat(
            prober,
            MatchesStructure.byEquality(
                request_scheme="https",
                request_host="localhost",
                request_port=self.port,
                request_path="/valid-mirror",
                connect_scheme="https",
                connect_host="localhost",
                connect_port=self.port,
                connect_path="/valid-mirror",
            ),
        )

    def test_RedirectAwareProber_follows_https_redirect(self):
        url = "https://localhost:%s/redirect-to-valid-mirror/file" % self.port
        prober = RedirectAwareProberFactory(url)
        self.assertEqual(prober.url, url)
        deferred = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result.code)
            expected_url = "https://localhost:%s/valid-mirror/file" % self.port
            self.assertEqual(
                expected_url.encode("UTF-8"), result.request.absoluteURI
            )

        return deferred.addCallback(got_result)

    def test_https_fails_on_invalid_certificates(self):
        """Changes set back the default browser-like policy for HTTPS
        request and make sure the request is failing due to invalid
        (self-signed) certificate.
        """
        url = "https://localhost:%s/valid-mirror/file" % self.port
        prober = RedirectAwareProberFactory(url)
        prober.https_agent_policy = BrowserLikePolicyForHTTPS
        self.assertEqual(prober.url, url)
        deferred = prober.probe()

        def on_failure(result):
            self.assertIsInstance(result.value, InvalidHTTPSCertificate)
            self.assertIn(
                ("localhost", self.port),
                distributionmirror_prober.invalid_certificate_hosts,
            )

        def on_success(result):
            if result is not None:
                self.fail(
                    "Should have raised SSL error. Got '%s' instead" % result
                )

        deferred.addErrback(on_failure)
        deferred.addCallback(on_success)
        return deferred

    def test_https_skips_invalid_certificates_hosts(self):
        distributionmirror_prober.invalid_certificate_hosts.add(
            ("localhost", self.port)
        )
        url = "https://localhost:%s/valid-mirror/file" % self.port
        prober = RedirectAwareProberFactory(url)
        prober.https_agent_policy = BrowserLikePolicyForHTTPS
        self.assertEqual(prober.url, url)
        deferred = prober.probe()

        return assert_fails_with(deferred, InvalidHTTPSCertificateSkipped)


class TestProberHTTPSProxy(TestCase):
    def test_https_prober_uses_proxy_and_sends_sni(self):
        proxy_port = 6654
        self.pushConfig(
            "launchpad", http_proxy=f"http://localhost:{proxy_port}"
        )

        url = "https://mirror.example.com/valid-mirror/file"
        prober = RedirectAwareProberFactory(url, timeout=0.5)
        self.assertEqual(prober.url, url)

        # We just want to check that it did the request using the correct
        # Agent, pointing to the correct proxy config, configured with the
        # correct SNI hostname.
        agent = prober.getHttpsClient()._agent
        self.assertIsInstance(agent, TunnelingAgent)
        self.assertEqual(("localhost", proxy_port, None), agent._proxyConf)
        endpoint = agent._getEndpoint(
            TwistedWebClientURI.fromBytes(url.encode())
        )
        sni_hostname = endpoint._contextFactory._hostname
        self.assertEqual("mirror.example.com", sni_hostname)


class TestProberProtocolAndFactory(TestCase):
    layer = TwistedLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        root = DistributionMirrorTestHTTPServer()
        site = server.Site(root)
        site.displayTracebacks = False
        self.listening_port = reactor.listenTCP(0, site)
        self.addCleanup(self.listening_port.stopListening)
        self.port = self.listening_port.getHost().port
        self.urls = {
            "timeout": "http://localhost:%s/timeout" % self.port,
            "200": "http://localhost:%s/valid-mirror" % self.port,
            "500": "http://localhost:%s/error" % self.port,
            "404": "http://localhost:%s/invalid-mirror" % self.port,
        }
        self.pushConfig("launchpad", http_proxy=None)

    def _createProberAndProbe(self, url):
        prober = ProberFactory(url)
        return prober.probe()

    def test_config_no_http_proxy(self):
        prober = ProberFactory(self.urls["200"])
        self.assertThat(
            prober,
            MatchesStructure.byEquality(
                request_scheme="http",
                request_host="localhost",
                request_port=self.port,
                request_path="/valid-mirror",
                connect_scheme="http",
                connect_host="localhost",
                connect_port=self.port,
                connect_path="/valid-mirror",
            ),
        )

    def test_config_http_proxy(self):
        self.pushConfig("launchpad", http_proxy="http://squid.internal:3128")
        prober = ProberFactory(self.urls["200"])
        self.assertThat(
            prober,
            MatchesStructure.byEquality(
                request_scheme="http",
                request_host="localhost",
                request_port=self.port,
                request_path="/valid-mirror",
                connect_scheme="http",
                connect_host="squid.internal",
                connect_port=3128,
                connect_path=self.urls["200"],
            ),
        )

    def test_connect_cancels_existing_timeout_call(self):
        prober = ProberFactory(self.urls["200"])
        prober.timeoutCall = reactor.callLater(30, prober.failWithTimeoutError)
        old_timeout_call = prober.timeoutCall
        self.assertTrue(old_timeout_call.active())
        prober.connect()
        self.assertFalse(old_timeout_call.active())
        self.assertTrue(prober.timeoutCall.active())
        return prober._deferred

    def _test_connect_to_host(self, url, host):
        """Check that a ProberFactory created with the given url will actually
        connect to the given host.
        """
        prober = ProberFactory(url)

        def fakeConnect(host, port, factory):
            factory.connecting_to = host
            factory.succeeded("200")

        prober.connecting_to = None
        orig_connect = reactor.connectTCP
        reactor.connectTCP = fakeConnect

        def restore_connect(result, orig_connect):
            self.assertEqual(prober.connecting_to, host)
            reactor.connectTCP = orig_connect
            return None

        deferred = prober.probe()
        return deferred.addCallback(restore_connect, orig_connect)

    def test_connect_to_proxy_when_http_proxy_exists(self):
        self.pushConfig("launchpad", http_proxy="http://squid.internal:3128")
        self._test_connect_to_host(self.urls["200"], "squid.internal")

    def test_connect_to_host_when_http_proxy_does_not_exist(self):
        self._test_connect_to_host(self.urls["200"], "localhost")

    def test_probe_sets_up_timeout_call(self):
        prober = ProberFactory(self.urls["200"])
        self.assertIsNone(getattr(prober, "timeoutCall", None))
        deferred = prober.probe()
        self.assertIsNotNone(getattr(prober, "timeoutCall", None))
        return deferred

    def test_RedirectAwareProber_follows_http_redirect(self):
        url = "http://localhost:%s/redirect-to-valid-mirror/file" % self.port
        prober = RedirectAwareProberFactory(url)
        self.assertTrue(prober.redirection_count == 0)
        self.assertTrue(prober.url == url)
        deferred = prober.probe()

        def got_result(result):
            self.assertEqual(1, prober.redirection_count)
            new_url = "http://localhost:%s/valid-mirror/file" % self.port
            self.assertEqual(new_url, prober.url)
            self.assertEqual(http.client.OK, result)

        return deferred.addBoth(got_result)

    def test_redirectawareprober_detects_infinite_loop(self):
        prober = RedirectAwareProberFactory(
            "http://localhost:%s/redirect-infinite-loop" % self.port
        )
        deferred = prober.probe()
        return assert_fails_with(deferred, InfiniteLoopDetected)

    def test_redirectawareprober_fail_on_unknown_scheme(self):
        prober = RedirectAwareProberFactory(
            "http://localhost:%s/redirect-unknown-url-scheme" % self.port
        )
        deferred = prober.probe()
        return assert_fails_with(deferred, UnknownURLSchemeAfterRedirect)

    def test_200(self):
        d = self._createProberAndProbe(self.urls["200"])

        def got_result(result):
            self.assertEqual(
                http.client.OK,
                result,
                "Expected a '200' status but got %r" % result,
            )

        return d.addCallback(got_result)

    def test_success_cancel_timeout_call(self):
        prober = ProberFactory(self.urls["200"])
        deferred = prober.probe()
        self.assertTrue(prober.timeoutCall.active())

        def check_timeout_call(result):
            self.assertFalse(prober.timeoutCall.active())

        return deferred.addCallback(check_timeout_call)

    def test_failure_cancel_timeout_call(self):
        prober = ProberFactory(self.urls["500"])
        deferred = prober.probe()
        self.assertTrue(prober.timeoutCall.active())

        def check_timeout_call(result):
            self.assertFalse(prober.timeoutCall.active())

        return deferred.addErrback(check_timeout_call)

    def test_notfound(self):
        d = self._createProberAndProbe(self.urls["404"])
        d = assert_fails_with(d, BadResponseCode)

        def check_url(error):
            self.assertEqual(self.urls["404"], error.url)

        return d.addCallback(check_url)

    def test_500(self):
        d = self._createProberAndProbe(self.urls["500"])
        return assert_fails_with(d, BadResponseCode)

    def test_timeout(self):
        d = self._createProberAndProbe(self.urls["timeout"])
        return assert_fails_with(d, ProberTimeout)

    def test_prober_user_agent(self):
        protocol = RedirectAwareProberProtocol()

        orig_sendHeader = protocol.sendHeader
        headers = {}

        def mySendHeader(header, value):
            orig_sendHeader(header, value)
            headers[header] = value

        protocol.sendHeader = mySendHeader

        protocol.factory = FakeFactory("http://foo.bar/")
        protocol.makeConnection(FakeTransport())
        self.assertEqual(
            "Launchpad Mirror Prober ( https://launchpad.net/ )",
            headers["User-Agent"],
        )


class FakeTimeOutCall:
    resetCalled = False

    def reset(self, seconds):
        self.resetCalled = True


class FakeTransport:
    disconnecting = False

    def loseConnection(self):
        self.disconnecting = True

    def write(self, text):
        pass

    def writeSequence(self, text):
        pass


class FakeFactory(RedirectAwareProberFactory):
    redirectedTo = None

    def redirect(self, url):
        self.redirectedTo = url


class TestProberFactoryRequestTimeoutRatioWithoutTwisted(TestCase):
    """Tests to ensure we stop issuing requests on a given host if the
    requests/timeouts ratio on that host is too low.

    The tests here will stub the prober's connect() method, so that we can
    easily check whether it was called or not without actually issuing any
    connections.
    """

    host = "foo.bar"

    def setUp(self):
        super().setUp()
        self.orig_host_requests = dict(distributionmirror_prober.host_requests)
        self.orig_host_timeouts = dict(distributionmirror_prober.host_timeouts)

    def tearDown(self):
        # Restore the globals that our tests fiddle with.
        distributionmirror_prober.host_requests = self.orig_host_requests
        distributionmirror_prober.host_timeouts = self.orig_host_timeouts
        # We need to remove any DelayedCalls that didn't actually get called.
        clean_up_reactor()
        super().tearDown()

    def _createProberStubConnectAndProbe(self, requests, timeouts):
        """Create a ProberFactory object with a URL inside self.host and call
        its probe() method.

        Before the prober.probe() method is called, we stub the connect
        method, because all we want is to check whether that method was called
        or not --we don't want to actually connect.
        """

        def connect():
            prober.connectCalled = True

        distributionmirror_prober.host_requests = {self.host: requests}
        distributionmirror_prober.host_timeouts = {self.host: timeouts}
        prober = ProberFactory("http://%s/baz" % self.host)
        prober.connectCalled = False
        prober.failed = lambda error: None
        prober.connect = connect
        prober.probe()
        return prober

    def test_connect_is_called_if_not_enough_requests(self):
        """Test that only a small ratio is not enough to cause a host to be
        skipped; we also need to have a considerable number of requests.
        """
        requests = MIN_REQUESTS_TO_CONSIDER_RATIO - 1
        timeouts = requests
        prober = self._createProberStubConnectAndProbe(requests, timeouts)
        self.assertTrue(prober.connectCalled)
        # Ensure the number of requests and timeouts we're using should
        # _NOT_ cause a given host to be skipped.
        self.assertFalse(should_skip_host(self.host))

    def test_connect_is_not_called_after_too_many_timeouts(self):
        """If we get a small requests/timeouts ratio on a given host, we'll
        stop issuing requests on that host.
        """
        # If the ratio is small enough and we have a considerable number of
        # requests, we won't issue more connections on that host.
        requests = MIN_REQUESTS_TO_CONSIDER_RATIO
        timeouts = (
            MIN_REQUESTS_TO_CONSIDER_RATIO / MIN_REQUEST_TIMEOUT_RATIO
        ) + 2
        prober = self._createProberStubConnectAndProbe(requests, timeouts)
        self.assertFalse(prober.connectCalled)
        # Ensure the number of requests and timeouts we're using should
        # actually cause a given host to be skipped.
        self.assertTrue(should_skip_host(self.host))

    def test_connect_is_called_if_not_many_timeouts(self):
        # If the ratio is not too small we consider it's safe to keep
        # issuing connections on that host.
        requests = MIN_REQUESTS_TO_CONSIDER_RATIO
        timeouts = (
            MIN_REQUESTS_TO_CONSIDER_RATIO / MIN_REQUEST_TIMEOUT_RATIO
        ) - 2
        prober = self._createProberStubConnectAndProbe(requests, timeouts)
        self.assertTrue(prober.connectCalled)
        # Ensure the number of requests and timeouts we're using should
        # _NOT_ cause a given host to be skipped.
        self.assertFalse(should_skip_host(self.host))


class TestProberFactoryRequestTimeoutRatioWithTwisted(TestCase):
    """Tests to ensure we stop issuing requests on a given host if the
    requests/timeouts ratio on that host is too low.

    The tests here will check that we'll record a timeout whenever we get a
    ProberTimeout from twisted, as well as checking that twisted raises
    ConnectionSkipped when it finds a URL that should not be probed. This
    means that we need a test HTTP server as well as the twisted magic to
    actually connect to the server.
    """

    layer = TwistedLayer
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        orig_host_requests = dict(distributionmirror_prober.host_requests)
        orig_host_timeouts = dict(distributionmirror_prober.host_timeouts)
        distributionmirror_prober.host_requests = {}
        distributionmirror_prober.host_timeouts = {}

        def restore_prober_globals():
            # Restore the globals that our tests fiddle with.
            distributionmirror_prober.host_requests = orig_host_requests
            distributionmirror_prober.host_timeouts = orig_host_timeouts

        self.addCleanup(restore_prober_globals)

        root = DistributionMirrorTestHTTPServer()
        site = server.Site(root)
        site.displayTracebacks = False
        self.listening_port = reactor.listenTCP(0, site)
        self.addCleanup(self.listening_port.stopListening)
        self.port = self.listening_port.getHost().port
        self.pushConfig("launchpad", http_proxy=None)

    def _createProberAndProbe(self, url):
        prober = ProberFactory(url)
        return prober.probe()

    def test_timeout_is_recorded(self):
        host = "localhost"
        d = self._createProberAndProbe(
            "http://%s:%s/timeout" % (host, self.port)
        )

        def got_error(error):
            self.assertEqual(
                {host: 1}, distributionmirror_prober.host_requests
            )
            self.assertEqual(
                {host: 1}, distributionmirror_prober.host_timeouts
            )

        return d.addErrback(got_error)

    def test_non_timeout_is_recorded(self):
        host = "localhost"
        d = self._createProberAndProbe(
            "http://%s:%s/valid-mirror" % (host, self.port)
        )

        def got_result(result):
            self.assertEqual(
                {host: 1}, distributionmirror_prober.host_requests
            )
            self.assertEqual(
                {host: 0}, distributionmirror_prober.host_timeouts
            )

        return d.addCallback(got_result)

    def test_failure_after_too_many_timeouts(self):
        host = "foo.bar"
        requests = MIN_REQUESTS_TO_CONSIDER_RATIO
        timeouts = (
            MIN_REQUESTS_TO_CONSIDER_RATIO / MIN_REQUEST_TIMEOUT_RATIO
        ) + 2
        distributionmirror_prober.host_requests = {host: requests}
        distributionmirror_prober.host_timeouts = {host: timeouts}
        # Ensure the number of requests and timeouts we're using should
        # cause a given host to be skipped.
        self.assertTrue(should_skip_host(host))

        d = self._createProberAndProbe(
            "http://%s:%s/timeout" % (host, self.port)
        )
        return assert_fails_with(d, ConnectionSkipped)


class TestMultiLock(TestCase):
    def setUp(self):
        super().setUp()
        self.lock_one = defer.DeferredLock()
        self.lock_two = defer.DeferredLock()
        self.multi_lock = MultiLock(self.lock_one, self.lock_two)
        self.count = 0

    def callback(self):
        self.count += 1

    def test_run_does_not_wait_when_there_is_no_need_to(self):
        """Multilock.run will run any given task if it's not locked and
        there's no task currently running.
        """
        self.multi_lock.run(self.callback)
        self.assertEqual(self.count, 1, "self.callback should have run.")

        self.multi_lock.run(self.callback)
        self.assertEqual(self.count, 2, "self.callback should have run twice.")

    def test_run_waits_for_first_lock(self):
        """MultiLock.run acquires the first lock before running a function."""
        # Keep lock_one busy.
        deferred = defer.Deferred()
        self.lock_one.run(lambda: deferred)

        # Run self.callback when self.multi_lock is acquired.
        self.multi_lock.run(self.callback)
        self.assertEqual(
            self.count, 0, "self.callback should not have run yet."
        )

        # Release lock_one.
        deferred.callback(None)

        # multi_lock will now have been able to acquire both semaphores, and
        # so it will have run its task.
        self.assertEqual(self.count, 1, "self.callback should have run.")

    def test_run_waits_for_second_lock(self):
        """MultiLock.run acquires the second lock before running functions."""
        # Keep lock_two busy.
        deferred = defer.Deferred()
        self.lock_two.run(lambda: deferred)

        # Run self.callback when self.multi_lock is acquired.
        self.multi_lock.run(self.callback)
        self.assertEqual(
            self.count, 0, "self.callback should not have run yet."
        )

        # Release lock_two.
        deferred.callback(None)

        # multi_lock will now have been able to acquire both semaphores, and
        # so it will have run its task.
        self.assertEqual(self.count, 1, "self.callback should have run.")

    def test_run_waits_for_current_task(self):
        """MultiLock.run waits the end of the current task before running the
        next.
        """
        # Keep multi_lock busy.
        deferred = defer.Deferred()
        self.multi_lock.run(lambda: deferred)

        # Run self.callback when self.multi_lock is acquired.
        self.multi_lock.run(self.callback)
        self.assertEqual(
            self.count, 0, "self.callback should not have run yet."
        )

        # Release lock_one.
        deferred.callback(None)

        # multi_lock will now have been able to acquire both semaphores, and
        # so it will have run its task.
        self.assertEqual(self.count, 1, "self.callback should have run.")


class TestRedirectAwareProberFactoryAndProtocol(TestCase):
    def tearDown(self):
        # We need to remove any DelayedCalls that didn't actually get called.
        clean_up_reactor()
        super().tearDown()

    def test_redirect_resets_timeout(self):
        prober = RedirectAwareProberFactory("http://foo.bar")
        prober.timeoutCall = FakeTimeOutCall()
        prober.connect = lambda: None
        self.assertFalse(prober.timeoutCall.resetCalled)
        prober.redirect("http://bar.foo")
        self.assertTrue(prober.timeoutCall.resetCalled)

    def _createFactoryAndStubConnectAndTimeoutCall(self, url=None):
        if url is None:
            url = "http://foo.bar"
        prober = RedirectAwareProberFactory(url)
        prober.timeoutCall = FakeTimeOutCall()
        prober.connectCalled = False

        def connect():
            prober.connectCalled = True

        prober.connect = connect
        return prober

    def test_raises_error_if_redirected_to_different_file(self):
        prober = self._createFactoryAndStubConnectAndTimeoutCall(
            "http://foo.bar/baz/boo/package.deb"
        )

        def failed(error):
            prober.has_failed = True

        prober.failed = failed
        prober.redirect("http://foo.bar/baz/boo/notfound?file=package.deb")
        self.assertTrue(prober.has_failed)

    def test_does_not_raise_if_redirected_to_reencoded_file(self):
        prober = self._createFactoryAndStubConnectAndTimeoutCall(
            "http://foo.bar/baz/boo/package+foo.deb"
        )

        def failed(error):
            prober.has_failed = True

        prober.failed = failed
        prober.redirect("http://foo.bar/baz/boo/package%2Bfoo.deb")
        self.assertFalse(hasattr(prober, "has_failed"))

    def test_connect_depends_on_localhost_only_config(self):
        # If localhost_only is True and the host to which we would connect is
        # not localhost, the connect() method is not called.
        localhost_only_conf = """
            [distributionmirrorprober]
            localhost_only: True
            """
        config.push("localhost_only_conf", localhost_only_conf)
        prober = self._createFactoryAndStubConnectAndTimeoutCall()
        self.assertTrue(prober.connect_host != "localhost")
        prober.probe()
        self.assertFalse(prober.connectCalled)
        # Restore the config.
        config.pop("localhost_only_conf")

        # If localhost_only is False, then it doesn't matter the host to which
        # we'll connect to --the connect() method will be called.
        remote_conf = """
            [distributionmirrorprober]
            localhost_only: False
            """
        config.push("remote_conf", remote_conf)
        prober = self._createFactoryAndStubConnectAndTimeoutCall()
        prober.probe()
        self.assertTrue(prober.connectCalled)
        # Restore the config.
        config.pop("remote_conf")

    def test_noconnection_is_made_when_infiniteloop_detected(self):
        prober = self._createFactoryAndStubConnectAndTimeoutCall()
        prober.failed = lambda error: None
        prober.redirection_count = MAX_REDIRECTS
        prober.redirect("http://bar.foo")
        self.assertFalse(prober.connectCalled)

    def test_noconnection_is_made_when_url_scheme_is_not_http_or_ftp(self):
        prober = self._createFactoryAndStubConnectAndTimeoutCall()
        prober.failed = lambda error: None
        prober.redirect("ssh://bar.foo")
        self.assertFalse(prober.connectCalled)

    def test_connection_is_made_on_successful_redirect(self):
        prober = self._createFactoryAndStubConnectAndTimeoutCall()
        prober.redirect("http://bar.foo")
        self.assertTrue(prober.connectCalled)

    def test_connection_is_closed_on_redirect(self):
        protocol = RedirectAwareProberProtocol()
        protocol.factory = FakeFactory("http://foo.bar/")
        protocol.makeConnection(FakeTransport())
        protocol.dataReceived(
            b"HTTP/1.1 301 Moved Permanently\r\n"
            b"Location: http://foo.baz/\r\n"
            b"Length: 0\r\n"
            b"\r\n"
        )
        self.assertEqual(b"http://foo.baz/", protocol.factory.redirectedTo)
        self.assertTrue(protocol.transport.disconnecting)


class TestMirrorCDImageProberCallbacks(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def makeMirrorProberCallbacks(self):
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        distroseries = removeSecurityProxy(
            self.factory.makeDistroSeries(distribution=ubuntu)
        )
        mirror = removeSecurityProxy(
            self.factory.makeMirror(distroseries.distribution)
        )
        callbacks = MirrorCDImageProberCallbacks(
            mirror, distroseries, "ubuntu", io.StringIO()
        )
        return callbacks

    def getLogger(self):
        logger = logging.getLogger("distributionmirror-prober")
        logger.errorCalled = False

        def error(msg):
            logger.errorCalled = True

        logger.error = error
        return logger

    def test_mirrorcdimageseries_creation_and_deletion_all_success(self):
        callbacks = self.makeMirrorProberCallbacks()
        all_success = [(defer.SUCCESS, "200"), (defer.SUCCESS, "200")]
        mirror_cdimage_series = callbacks.ensureOrDeleteMirrorCDImageSeries(
            all_success
        )
        self.assertIsNot(
            mirror_cdimage_series,
            None,
            "If the prober gets a list of 200 Okay statuses, a new "
            "MirrorCDImageSeries should be created.",
        )

    def test_mirrorcdimageseries_creation_and_deletion_some_404s(self):
        not_all_success = [
            (defer.FAILURE, Failure(BadResponseCode(http.client.NOT_FOUND))),
            (defer.SUCCESS, "200"),
        ]
        callbacks = self.makeMirrorProberCallbacks()
        all_success = [(defer.SUCCESS, "200"), (defer.SUCCESS, "200")]
        mirror_cdimage_series = callbacks.ensureOrDeleteMirrorCDImageSeries(
            all_success
        )
        callbacks.ensureOrDeleteMirrorCDImageSeries(not_all_success)
        # If the prober gets at least one 404 status, we need to make sure
        # there's no MirrorCDImageSeries for that series and flavour.
        self.assertIsNone(
            Store.of(mirror_cdimage_series).get(
                type(mirror_cdimage_series), mirror_cdimage_series.id
            )
        )

    def test_expected_failures_are_ignored(self):
        # Any errors included in callbacks.expected_failures are simply
        # ignored by ensureOrDeleteMirrorCDImageSeries() because they've been
        # logged by logMissingURL() already and they're expected to happen
        # some times.
        logger = self.getLogger()
        callbacks = self.makeMirrorProberCallbacks()
        self.assertEqual(
            set(callbacks.expected_failures),
            {
                BadContentType,
                BadResponseCode,
                ProberTimeout,
                ConnectionSkipped,
                RedirectToDifferentFile,
                UnknownURLSchemeAfterRedirect,
                InvalidHTTPSCertificate,
                InvalidHTTPSCertificateSkipped,
                TunnelError,
            },
        )
        exceptions = [
            BadContentType("http://localhost/ubuntu.iso", "text/html"),
            BadResponseCode(http.client.NOT_FOUND),
            ProberTimeout("http://localhost/", 5),
            ConnectionSkipped(),
            RedirectToDifferentFile("/foo", "/bar"),
            UnknownURLSchemeAfterRedirect("https://localhost"),
            InvalidHTTPSCertificate("localhost", 443),
            InvalidHTTPSCertificateSkipped("https://localhost/xx"),
            TunnelError("Could not open CONNECT tunnel."),
        ]
        for exception in exceptions:
            failure = callbacks.ensureOrDeleteMirrorCDImageSeries(
                [(defer.FAILURE, Failure(exception))]
            )
            # Twisted callbacks may raise or return a failure; that's why we
            # check the return value.
            self.assertFalse(isinstance(failure, Failure))
            # Also, these failures are not logged to stdout/stderr since
            # they're expected to happen.
            self.assertFalse(logger.errorCalled)

    def test_unexpected_failures_are_logged_but_not_raised(self):
        # Errors which are not expected as logged using the
        # prober's logger to make sure people see it while still allowing
        # other mirrors to be probed.
        logger = self.getLogger()
        callbacks = self.makeMirrorProberCallbacks()
        failure = callbacks.ensureOrDeleteMirrorCDImageSeries(
            [(defer.FAILURE, Failure(ZeroDivisionError()))]
        )
        # Twisted callbacks may raise or return a failure; that's why we
        # check the return value.
        self.assertFalse(isinstance(failure, Failure))
        # Unlike the expected failures, these ones must be logged as errors to
        # stdout/stderr.
        self.assertTrue(logger.errorCalled)


class TestArchiveMirrorProberCallbacks(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def makeMirrorProberCallbacks(self):
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        distroseries = removeSecurityProxy(
            self.factory.makeDistroSeries(distribution=ubuntu)
        )
        mirror = removeSecurityProxy(
            self.factory.makeMirror(distroseries.distribution)
        )
        component = self.factory.makeComponent()
        callbacks = ArchiveMirrorProberCallbacks(
            mirror,
            distroseries,
            PackagePublishingPocket.RELEASE,
            component,
            "foo",
            io.StringIO(),
        )
        return callbacks

    def test_failure_propagation(self):
        # Make sure that deleteMirrorSeries() does not propagate
        # ProberTimeout, BadResponseCode or ConnectionSkipped failures.
        callbacks = self.makeMirrorProberCallbacks()
        try:
            callbacks.deleteMirrorSeries(
                Failure(ProberTimeout("http://localhost/", 5))
            )
        except Exception as e:
            self.fail("A timeout shouldn't be propagated. Got %s" % e)
        try:
            callbacks.deleteMirrorSeries(
                Failure(BadResponseCode(http.client.INTERNAL_SERVER_ERROR))
            )
        except Exception as e:
            self.fail(
                "A bad response code shouldn't be propagated. Got %s" % e
            )
        try:
            callbacks.deleteMirrorSeries(Failure(ConnectionSkipped()))
        except Exception as e:
            self.fail(
                "A ConnectionSkipped exception shouldn't be "
                "propagated. Got %s" % e
            )
        try:
            callbacks.deleteMirrorSeries(
                Failure(ResponseFailure("http://localhost/", "reason"))
            )
        except Exception as e:
            self.fail(
                "A ResponseFailure exception shouldn't be propagated. Got %s"
                % e
            )

        # Make sure that deleteMirrorSeries() propagate any failure that is
        # not a ProberTimeout, a BadResponseCode or a ConnectionSkipped.
        d = defer.Deferred()
        d.addErrback(callbacks.deleteMirrorSeries)
        ok = []

        def got_result(result):
            self.fail(
                "Any failure that's not a timeout/bad-response/skipped "
                "should be propagated."
            )

        def got_failure(failure):
            ok.append(1)

        d.addCallbacks(got_result, got_failure)
        d.errback(Failure(ZeroDivisionError()))
        self.assertEqual([1], ok)

    def test_mirrorseries_creation_and_deletion(self):
        callbacks = self.makeMirrorProberCallbacks()
        mirror_distro_series_source = callbacks.ensureMirrorSeries(
            str(int(http.client.OK))
        )
        self.assertIsNot(
            mirror_distro_series_source,
            None,
            "If the prober gets a 200 Okay status, a new "
            "MirrorDistroSeriesSource/MirrorDistroArchSeries should be "
            "created.",
        )

        callbacks.deleteMirrorSeries(
            Failure(BadResponseCode(http.client.NOT_FOUND))
        )
        # If the prober gets a 404 status, we need to make sure there's no
        # MirrorDistroSeriesSource/MirrorDistroArchSeries referent to
        # that url
        self.assertIsNone(
            Store.of(mirror_distro_series_source).get(
                type(mirror_distro_series_source),
                mirror_distro_series_source.id,
            )
        )


class TestProbeFunctionSemaphores(TestCase):
    """Make sure we use one DeferredSemaphore for each hostname when probing
    mirrors.
    """

    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.logger = None
        # RequestManager uses a mutable class attribute (host_locks) to ensure
        # all of its instances share the same locks. We don't want our tests
        # to interfere with each other, though, so we'll clean
        # RequestManager.host_locks here.
        RequestManager.host_locks.clear()

    def tearDown(self):
        # We need to remove any DelayedCalls that didn't actually get called.
        clean_up_reactor()
        super().tearDown()

    def test_MirrorCDImageSeries_records_are_deleted_before_probing(self):
        mirror = getUtility(IDistributionMirrorSet).getByName(
            "releases-mirror2"
        )
        self.assertNotEqual(0, len(mirror.cdimage_series))
        # Note that calling this function won't actually probe any mirrors; we
        # need to call reactor.run() to actually start the probing.
        with default_timeout(15.0):
            probe_cdimage_mirror(mirror, io.StringIO(), [], logging, 100, 2)
        self.assertEqual(0, len(mirror.cdimage_series))

    def test_archive_mirror_probe_function(self):
        mirror1 = getUtility(IDistributionMirrorSet).getByName(
            "archive-mirror"
        )
        mirror2 = getUtility(IDistributionMirrorSet).getByName(
            "archive-mirror2"
        )
        mirror3 = getUtility(IDistributionMirrorSet).getByName(
            "canonical-archive"
        )
        self._test_one_semaphore_for_each_host(
            mirror1, mirror2, mirror3, probe_archive_mirror
        )

    def test_cdimage_mirror_probe_function(self):
        mirror1 = getUtility(IDistributionMirrorSet).getByName(
            "releases-mirror"
        )
        mirror2 = getUtility(IDistributionMirrorSet).getByName(
            "releases-mirror2"
        )
        mirror3 = getUtility(IDistributionMirrorSet).getByName(
            "canonical-releases"
        )
        with default_timeout(15.0):
            self._test_one_semaphore_for_each_host(
                mirror1, mirror2, mirror3, probe_cdimage_mirror
            )

    def _test_one_semaphore_for_each_host(
        self, mirror1, mirror2, mirror3, probe_function
    ):
        """Check that we create one semaphore per host when probing the given
        mirrors using the given probe_function.

        mirror1.base_url and mirror2.base_url must be on the same host while
        mirror3.base_url must be on a different one.

        The given probe_function must be either probe_cdimage_mirror or
        probe_archive_mirror.
        """
        max_per_host_requests = 2
        max_requests = 100
        request_manager = RequestManager(max_requests, max_per_host_requests)
        mirror1_host = URI(mirror1.base_url).host
        mirror2_host = URI(mirror2.base_url).host
        mirror3_host = URI(mirror3.base_url).host

        probe_function(mirror1, io.StringIO(), [], logging, 100, 2)
        # Since we have a single mirror to probe we need to have a single
        # DeferredSemaphore with a limit of max_per_host_requests, to ensure we
        # don't issue too many simultaneous connections on that host.
        self.assertEqual(len(request_manager.host_locks), 1)
        multi_lock = request_manager.host_locks[mirror1_host]
        self.assertEqual(multi_lock.host_lock.limit, max_per_host_requests)
        # Note that our multi_lock contains another semaphore to control the
        # overall number of requests.
        self.assertEqual(multi_lock.overall_lock.limit, max_requests)

        probe_function(mirror2, io.StringIO(), [], logging, 100, 2)
        # Now we have two mirrors to probe, but they have the same hostname,
        # so we'll still have a single semaphore in host_semaphores.
        self.assertEqual(mirror2_host, mirror1_host)
        self.assertEqual(len(request_manager.host_locks), 1)
        multi_lock = request_manager.host_locks[mirror2_host]
        self.assertEqual(multi_lock.host_lock.limit, max_per_host_requests)

        probe_function(mirror3, io.StringIO(), [], logging, 100, 2)
        # This third mirror is on a separate host, so we'll have a second
        # semaphore added to host_semaphores.
        self.assertTrue(mirror3_host != mirror1_host)
        self.assertEqual(len(request_manager.host_locks), 2)
        multi_lock = request_manager.host_locks[mirror3_host]
        self.assertEqual(multi_lock.host_lock.limit, max_per_host_requests)

        # When using an http_proxy, even though we'll actually connect to the
        # proxy, we'll use the mirror's host as the key to find the semaphore
        # that should be used
        self.pushConfig("launchpad", http_proxy="http://squid.internal:3128/")
        probe_function(mirror3, io.StringIO(), [], logging, 100, 2)
        self.assertEqual(len(request_manager.host_locks), 2)


class TestCDImageFileListFetching(TestCase):
    @responses.activate
    def test_no_cache(self):
        url = "http://releases.ubuntu.com/.manifest"
        self.pushConfig("distributionmirrorprober", cdimage_file_list_url=url)
        responses.add("GET", url)
        with default_timeout(1.0):
            _get_cdimage_file_list()
        self.assertThat(
            responses.calls[0].request.headers,
            ContainsDict(
                {
                    "Pragma": Equals("no-cache"),
                    "Cache-control": Equals("no-cache"),
                }
            ),
        )


class TestLoggingMixin(TestCase):
    def tearDown(self):
        # We need to remove any DelayedCalls that didn't actually get called.
        clean_up_reactor()
        super().tearDown()

    def _fake_gettime(self):
        # Fake the current time.
        fake_time = datetime(2004, 10, 20, 12, 0, 0, 0)
        return fake_time

    def test_logMessage_output(self):
        logger = LoggingMixin()
        logger.log_file = io.StringIO()
        logger._getTime = self._fake_gettime
        logger.logMessage("Ubuntu Warty Released")
        logger.log_file.seek(0)
        message = logger.log_file.read()
        self.assertEqual(
            "Wed Oct 20 12:00:00 2004: Ubuntu Warty Released", message
        )

    def test_logMessage_integration(self):
        logger = LoggingMixin()
        logger.log_file = io.StringIO()
        logger.logMessage("Probing...")
        logger.log_file.seek(0)
        message = logger.log_file.read()
        self.assertNotEqual(None, message)


class TestDistroMirrorProberFunctional(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def setUp(self):
        super().setUp()
        # Makes a clean distro mirror set, with only the mirrors we want.
        self.removeMirrors()

    def removeMirrors(self):
        """Removes all mirror information from database."""
        store = IStore(DistributionMirror)
        store.find(MirrorProbeRecord).remove()
        store.find(MirrorDistroArchSeries).remove()
        store.find(MirrorDistroSeriesSource).remove()
        store.find(MirrorCDImageDistroSeries).remove()
        store.find(DistributionMirror).remove()
        store.flush()

    def makeMirror(self, content_type, distro=None):
        with admin_logged_in():
            if distro is None:
                distro = self.factory.makeDistribution()
                distro.supports_mirrors = True
                self.factory.makeDistroSeries(distribution=distro)
            mirror = self.factory.makeMirror(
                distro, http_url="http://fake-url.invalid"
            )
            mirror.enabled = True
            mirror.status = MirrorStatus.OFFICIAL
            mirror.official_candidate = True
            mirror.content = content_type
        return mirror

    def test_cdimage_prober(self):
        """Checks that CD image prober works fine, end to end."""
        mirror = self.makeMirror(content_type=MirrorContent.RELEASE)
        transaction.commit()

        exit_code, out, err = run_script(
            "cronscripts/distributionmirror-prober.py",
            args=[
                "--no-remote-hosts",
                "--content-type=cdimage",
                "--no-owner-notification",
                "--force",
            ],
        )
        self.assertEqual(0, exit_code, err)

        lock_file = "/var/lock/launchpad-distributionmirror-prober.lock"
        self.assertEqual(
            dedent(
                """\
            INFO    Creating lockfile: %s
            INFO    Probing CD Image Mirrors
            INFO    Probed 1 mirrors.
            INFO    Starting to update mirrors statuses outside reactor now.
            INFO    Done.
            """
            )
            % lock_file,
            err,
        )

        with admin_logged_in():
            record = removeSecurityProxy(mirror.last_probe_record)

        log_lines = record.log_file.read().decode("UTF-8")
        self.assertEqual(4, len(log_lines.split("\n")))
        self.assertIn(
            "Found all ISO images for series The Hoary Hedgehog Release "
            "and flavour kubuntu.",
            log_lines,
        )
        self.assertIn(
            "Found all ISO images for series The Hoary Hedgehog Release "
            "and flavour ubuntu.",
            log_lines,
        )
        self.assertIn(
            "Found all ISO images for series The Warty Warthog Release "
            "and flavour ubuntu.",
            log_lines,
        )

    def test_archive_prober(self):
        """Checks that archive prober works fine, end to end."""
        # Using ubuntu to avoid the need to create all the packages that
        # will be checked by prober.
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        mirror = self.makeMirror(
            content_type=MirrorContent.ARCHIVE, distro=ubuntu
        )
        transaction.commit()

        exit_code, out, err = run_script(
            "cronscripts/distributionmirror-prober.py",
            args=[
                "--no-remote-hosts",
                "--content-type=archive",
                "--no-owner-notification",
                "--force",
            ],
        )
        self.assertEqual(0, exit_code, err)

        lock_file = "/var/lock/launchpad-distributionmirror-prober.lock"
        self.assertEqual(
            dedent(
                """\
            INFO    Creating lockfile: %s
            INFO    Probing Archive Mirrors
            INFO    Probed 1 mirrors.
            INFO    Starting to update mirrors statuses outside reactor now.
            INFO    Done.
            """
            )
            % lock_file,
            err,
        )

        with admin_logged_in():
            record = removeSecurityProxy(mirror.last_probe_record)

        log_lines = record.log_file.read().decode("UTF-8")

        # Make sure that prober output seems reasonable.
        self.assertEqual(85, len(log_lines.split("\n")))
        url = "http://fake-url.invalid/dists/"
        self.assertEqual(
            40,
            len(
                re.findall(
                    (r"Ensuring MirrorDistroSeries of .* with url %s" % url)
                    + r".* exists in the database",
                    log_lines,
                )
            ),
        )
        self.assertEqual(
            40,
            len(
                re.findall(
                    (
                        r"Ensuring MirrorDistroArchSeries of .* with url %s"
                        % url
                    )
                    + r".* exists in the database",
                    log_lines,
                )
            ),
        )
        self.assertEqual(
            1,
            len(
                re.findall(
                    r"Updating MirrorDistroArchSeries of .* freshness to "
                    r"Up to date",
                    log_lines,
                )
            ),
        )
        self.assertEqual(
            3,
            len(
                re.findall(
                    r"Updating MirrorDistroSeries of .* freshness to "
                    r"Up to date",
                    log_lines,
                )
            ),
        )


class TestContentTypeCheckingProberHTTP(TestCase):
    """Tests for ContentTypeCheckingProberFactory over plain HTTP.

    These tests reproduce the bug where a mirror serving .iso files with
    Content-Type: text/html is incorrectly marked as working.  The new
    factory must reject any content type that is not in the allowlist of
    known-good binary types.
    """

    layer = TwistedLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        root = DistributionMirrorTestHTTPServer()
        site = server.Site(root)
        site.displayTracebacks = False
        self.listening_port = reactor.listenTCP(0, site)
        self.addCleanup(self.listening_port.stopListening)
        self.port = self.listening_port.getHost().port
        self.pushConfig("launchpad", http_proxy=None)
        self.useFixture(
            MockPatchObject(distributionmirror_prober, "host_requests", {})
        )
        self.useFixture(
            MockPatchObject(distributionmirror_prober, "host_timeouts", {})
        )

    def _url(self, path):
        return "http://localhost:%s/%s" % (self.port, path)

    def test_text_html_content_type_raises_bad_content_type(self):
        """A 200 OK with Content-Type: text/html must be rejected.

        This is the core bug: a misconfigured mirror proxying .iso requests
        through a web page causes browsers to display binary data as text
        rather than triggering a file download.
        """
        prober = ContentTypeCheckingProberFactory(
            self._url("html-mirror/ubuntu-24.04.iso")
        )
        return assert_fails_with(prober.probe(), BadContentType)

    def test_iso9660_content_type_is_accepted(self):
        """A 200 OK with Content-Type: application/x-iso9660-image succeeds."""
        prober = ContentTypeCheckingProberFactory(
            self._url("iso-mirror/ubuntu-24.04.iso")
        )
        d = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result)

        return d.addCallback(got_result)

    def test_octet_stream_content_type_is_accepted(self):
        """A 200 OK with Content-Type: application/octet-stream succeeds."""
        prober = ContentTypeCheckingProberFactory(
            self._url("octet-stream-mirror/ubuntu-24.04.iso")
        )
        d = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result)

        return d.addCallback(got_result)

    def test_missing_content_type_is_accepted(self):
        """A 200 OK with no Content-Type header is not treated as a failure."""
        prober = ContentTypeCheckingProberFactory(
            self._url("no-content-type-mirror/ubuntu-24.04.iso")
        )
        d = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result)

        return d.addCallback(got_result)


class TestContentTypeCheckingProberHTTPS(TestCase):
    """HTTPS equivalent of TestContentTypeCheckingProberHTTP."""

    layer = TwistedLayer
    run_tests_with = AsynchronousDeferredRunTestForBrokenTwisted.make_factory(
        timeout=30
    )

    def setUp(self):
        super().setUp()
        root = DistributionMirrorTestSecureHTTPServer()
        site = server.Site(root)
        site.displayTracebacks = False
        keys_path = os.path.join(config.root, "configs", "development")
        keys = ssl.DefaultOpenSSLContextFactory(
            os.path.join(keys_path, "launchpad.key"),
            os.path.join(keys_path, "launchpad.crt"),
        )
        self.listening_port = reactor.listenSSL(0, site, keys)
        self.addCleanup(self.listening_port.stopListening)
        self.port = self.listening_port.getHost().port
        self.pushConfig("launchpad", http_proxy=None)
        self.useFixture(
            MockPatchObject(
                ContentTypeCheckingProberFactory,
                "https_agent_policy",
                LocalhostWhitelistedHTTPSPolicy,
            )
        )
        self.useFixture(
            MockPatchObject(distributionmirror_prober, "host_requests", {})
        )
        self.useFixture(
            MockPatchObject(distributionmirror_prober, "host_timeouts", {})
        )
        self.useFixture(
            MockPatchObject(
                distributionmirror_prober, "invalid_certificate_hosts", set()
            )
        )

    def _url(self, path):
        return "https://localhost:%s/%s" % (self.port, path)

    def test_text_html_content_type_raises_bad_content_type(self):
        """
        A 200 OK with Content-Type: text/html must be rejected over HTTPS.
        """
        prober = ContentTypeCheckingProberFactory(
            self._url("html-mirror/ubuntu-24.04.iso")
        )
        return assert_fails_with(prober.probe(), BadContentType)

    def test_iso9660_content_type_is_accepted(self):
        """A 200 OK with Content-Type: application/x-iso9660-image succeeds."""
        prober = ContentTypeCheckingProberFactory(
            self._url("iso-mirror/ubuntu-24.04.iso")
        )
        d = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result.code)

        return d.addCallback(got_result)

    def test_octet_stream_content_type_is_accepted(self):
        """A 200 OK with Content-Type: application/octet-stream succeeds."""
        prober = ContentTypeCheckingProberFactory(
            self._url("octet-stream-mirror/ubuntu-24.04.iso")
        )
        d = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result.code)

        return d.addCallback(got_result)

    def test_missing_content_type_is_accepted(self):
        """A 200 OK with no Content-Type header is not treated as a failure."""
        prober = ContentTypeCheckingProberFactory(
            self._url("no-content-type-mirror/ubuntu-24.04.iso")
        )
        d = prober.probe()

        def got_result(result):
            self.assertEqual(http.client.OK, result.code)

        return d.addCallback(got_result)
