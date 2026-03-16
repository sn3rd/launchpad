# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tools for testing XML-RPC services."""

__all__ = [
    "MatchesFault",
    "XMLRPCTestTransport",
]

import http.client as http_client
import io
import xmlrpc.client  # nosec B411

from testtools.matchers import Equals, MatchesStructure
from zope.security.management import endInteraction, queryInteraction

from lp.services.webapp.interaction import (
    get_current_principal,
    setupInteraction,
)
from lp.testing.pages import http
from lp.xmlrpc import faults


class _FakeSocket:
    """Pretend to be a socket that has a makefile method.

    This is used because it is what http.client.HTTPResponse expects.
    """

    def __init__(self, output):
        self._output = output

    def makefile(self, mode="rb", bufsize=0):
        return io.BytesIO(self._output)


class TestHTTPConnection(http_client.HTTPConnection):
    """A HTTPConnection which talks to http() instead of a real server.

    Only the methods called by xmlrpc.client are overridden.
    """

    _data_to_send = b""
    _response = None

    def connect(self):
        """No need to connect."""
        pass

    def send(self, data):
        """Send the request to http()."""
        # We don't send it to http() yet; we store the data and send
        # everything at once when the client requests a response.
        self._data_to_send += data

    def _zope_response(self):
        """Get the response."""
        current_principal = None
        # End and save the current interaction, since http() creates
        # its own interaction.
        if queryInteraction():
            current_principal = get_current_principal()
            endInteraction()
        if self._response is None:
            self._response = http(self._data_to_send)
        # Restore the interaction to what it was before.
        setupInteraction(current_principal)
        return self._response

    def getresponse(self, buffering=False):
        content = self._zope_response().getOutput()
        sock = _FakeSocket(content)
        response = http_client.HTTPResponse(sock)
        response.begin()
        return response


class XMLRPCTestTransport(xmlrpc.client.Transport):
    """An XMLRPC Transport which sends the requests to http()."""

    def make_connection(self, host):
        """Return our custom http() HTTPConnection."""
        host, self._extra_headers, x509 = self.get_host_info(host)
        return TestHTTPConnection(host)


class MatchesFault(MatchesStructure):
    """Match an XML-RPC fault.

    This can be given either::

        - a subclass of LaunchpadFault (matches only the fault code)
        - an instance of Fault (matches the fault code and the fault string
          from this instance exactly)
    """

    def __init__(self, expected_fault):
        fault_matchers = {}
        if isinstance(expected_fault, type) and issubclass(
            expected_fault, faults.LaunchpadFault
        ):
            fault_matchers["faultCode"] = Equals(expected_fault.error_code)
        else:
            fault_matchers["faultCode"] = Equals(expected_fault.faultCode)
            fault_string = expected_fault.faultString
            # XXX cjwatson 2019-09-27: InvalidBranchName.faultString is
            # bytes, so we need this to handle that case.  Should it be?
            if not isinstance(fault_string, str):
                fault_string = fault_string.decode("UTF-8")
            fault_matchers["faultString"] = Equals(fault_string)
        super().__init__(**fault_matchers)
