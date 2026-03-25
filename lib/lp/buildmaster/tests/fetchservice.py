# Copyright 2015-2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Fixtures for dealing with the build time Fetch Service http proxy."""

import json
import uuid
from textwrap import dedent
from urllib.parse import urlsplit

import fixtures
from testtools.matchers import Equals, HasLength, MatchesStructure
from twisted.internet import defer, endpoints, reactor
from twisted.web import resource, server

from lp.services.config import config


class FetchServiceControlEndpoint(resource.Resource):
    """A fake fetch service control endpoints API."""

    isLeaf = True

    def __init__(self):
        resource.Resource.__init__(self)
        self.requests = []
        self.responses = []
        self.session_id = "2ea9c9f759044f9b9aff469fe90429a5"

    def render_POST(self, request):
        """We make a POST request to start a fetch service session"""
        content = json.loads(request.content.read())
        self.requests.append(
            {
                "method": request.method,
                "uri": request.uri,
                "headers": dict(request.requestHeaders.getAllRawHeaders()),
                "json": content,
            }
        )
        response = {
            "id": self.session_id,
            "token": uuid.uuid4().hex,
        }
        self.responses.append(response)
        return json.dumps(response).encode("UTF-8")

    def render_GET(self, request):
        """We make a GET request to get a fetch service session metadata"""
        self.requests.append(
            {
                "method": request.method,
                "uri": request.uri,
                "headers": dict(request.requestHeaders.getAllRawHeaders()),
            }
        )

        request.setHeader(b"Content-Type", b"application/json")
        response = {
            "session-id": self.session_id,
            "start-time": "2024-04-17T16:25:02.631557582Z",
            "end-time": "2024-04-17T16:26:23.505219343Z",
            "inspectors": [
                "pip.simple-index",
                "pip.wheel",
                "deb",
                "apt.release",
                "apt.packages",
                "default",
            ],
            "spool-path": (
                f"/var/snap/fetch-service/common/spool/{self.session_id}"
            ),
            "policy": "test",
            "processed-requests": 0,
            "processed-artefacts": 0,
            "rejected-requests": 0,
            "rejected-artefacts": 0,
            "artefacts": [],
        }
        self.responses.append(response)
        return json.dumps(response).encode("UTF-8")

    def render_DELETE(self, request):
        """We make a DELETE request to end a fetch service session"""
        self.requests.append(
            {
                "method": request.method,
                "uri": request.uri,
                "headers": dict(request.requestHeaders.getAllRawHeaders()),
            }
        )
        response = {}
        self.responses.append(response)
        return json.dumps(response).encode("UTF-8")


class InProcessFetchServiceAuthAPIFixture(fixtures.Fixture):
    """A fixture that mocks the Fetch Service authentication API.

    Users of this fixture must call the `start` method, which returns a
    `Deferred`, and arrange for that to get back to the reactor.  This is
    necessary because the basic fixture API does not allow `setUp` to return
    anything.  For example:

        class TestSomething(TestCase):

            run_tests_with = AsynchronousDeferredRunTest.make_factory(
                timeout=30)

            @defer.inlineCallbacks
            def setUp(self):
                super().setUp()
                yield self.useFixture(
                    InProcessFetchServiceAuthAPIFixture()
                ).start()
    """

    @defer.inlineCallbacks
    def start(self):
        root = resource.Resource()
        self.sessions = FetchServiceControlEndpoint()
        root.putChild(b"sessions", self.sessions)
        endpoint = endpoints.serverFromString(reactor, "tcp:0")
        site = server.Site(self.sessions)
        self.addCleanup(site.stopFactory)
        port = yield endpoint.listen(site)
        self.addCleanup(port.stopListening)
        configs = dedent(
            """
            [builddmaster]
            fetch_service_control_admin_secret: admin-secret
            fetch_service_control_admin_username: admin-launchpad.test
            fetch_service_control_endpoint: http://{host}:{port}
            fetch_service_host: {host}
            fetch_service_port: {port}
            fetch_service_mitm_certificate: fake-cert
            """
        ).format(host=port.getHost().host, port=port.getHost().port)
        config.push("in-process-fetch-service-api-fixture", configs)
        self.addCleanup(config.pop, "in-process-fetch-service-api-fixture")


# This class is not used yet but it could be
# useful for future tests
class FetchServiceURLMatcher(MatchesStructure):
    """Check that a string is a valid url for fetch service."""

    def __init__(self):
        super().__init__(
            scheme=Equals("http"),
            id=Equals(1),
            token=HasLength(32),
            hostname=Equals(config.builddmaster.fetch_service_host),
            port=Equals(config.builddmaster.fetch_service_port),
            path=Equals(""),
        )

    def match(self, matchee):
        super().match(urlsplit(matchee))


class RevocationEndpointMatcher(Equals):
    """Check that a string is a valid endpoint for fetch
    service session revocation.
    """

    def __init__(self, session_id):
        super().__init__(
            "{endpoint}/session/{session_id}/token".format(
                endpoint=config.builddmaster.fetch_service_control_endpoint,
                session_id=session_id,
            )
        )
