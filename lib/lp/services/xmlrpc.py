# Copyright 2010-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Generic code for XML-RPC in Launchpad."""

__all__ = [
    "LaunchpadFault",
    "Transport",
]

import socket
import xmlrpc.client  # nosec B411 -- defusedxml monkey_patch() applied below

from defusedxml.xmlrpc import monkey_patch

# Protect against various XML parsing vulnerabilities.
monkey_patch()


class LaunchpadFault(xmlrpc.client.Fault):
    """Base class for a Launchpad XMLRPC fault.

    Subclasses should define a unique error_code and a msg_template,
    which will be interpolated with the given keyword arguments.
    """

    error_code = None
    msg_template = None

    def __init__(self, **kw):
        assert (
            self.error_code is not None
        ), "Subclasses must define error_code."
        assert (
            self.msg_template is not None
        ), "Subclasses must define msg_template."
        msg = self.msg_template % kw
        xmlrpc.client.Fault.__init__(self, self.error_code, msg)

    def __eq__(self, other):
        if not isinstance(other, LaunchpadFault):
            return False
        return (
            self.faultCode == other.faultCode
            and self.faultString == other.faultString
        )

    def __ne__(self, other):
        return not (self == other)


class Transport(xmlrpc.client.Transport):
    """An xmlrpc.client transport that supports a timeout argument.

    Use by passing into the "transport" argument of the
    xmlrpc.client.ServerProxy initialization.
    """

    def __init__(self, use_datetime=0, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        xmlrpc.client.Transport.__init__(self, use_datetime)
        self.timeout = timeout

    def make_connection(self, host):
        conn = xmlrpc.client.Transport.make_connection(self, host)
        conn.timeout = self.timeout
        return conn
