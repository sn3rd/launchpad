# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""XML-RPC API to the application roots."""

__all__ = [
    "ISelfTest",
    "PrivateApplication",
    "SelfTest",
]

import xmlrpc.client  # nosec B411

from zope.component import getUtility
from zope.interface import Interface, implementer

from lp.bugs.interfaces.malone import IPrivateMaloneApplication
from lp.code.interfaces.codehosting import ICodehostingApplication
from lp.code.interfaces.codeimportscheduler import (
    ICodeImportSchedulerApplication,
)
from lp.code.interfaces.gitapi import IGitApplication
from lp.registry.interfaces.mailinglist import IMailingListApplication
from lp.registry.interfaces.person import ICanonicalSSOApplication
from lp.services.authserver.interfaces import IAuthServerApplication
from lp.services.features.xmlrpc import IFeatureFlagApplication
from lp.services.webapp import LaunchpadXMLRPCView
from lp.services.webapp.interfaces import ILaunchBag
from lp.soyuz.interfaces.archiveapi import IArchiveApplication
from lp.xmlrpc.interfaces import IPrivateApplication


# NOTE: If you add a traversal here, you should update
# the regular expression in lp:lp-dev-utils page-performance-report.ini.
@implementer(IPrivateApplication)
class PrivateApplication:
    @property
    def mailinglists(self):
        """See `IPrivateApplication`."""
        return getUtility(IMailingListApplication)

    @property
    def archive(self):
        """See `IPrivateApplication`."""
        return getUtility(IArchiveApplication)

    @property
    def authserver(self):
        """See `IPrivateApplication`."""
        return getUtility(IAuthServerApplication)

    @property
    def codehosting(self):
        """See `IPrivateApplication`."""
        return getUtility(ICodehostingApplication)

    @property
    def codeimportscheduler(self):
        """See `IPrivateApplication`."""
        return getUtility(ICodeImportSchedulerApplication)

    @property
    def bugs(self):
        """See `IPrivateApplication`."""
        return getUtility(IPrivateMaloneApplication)

    @property
    def canonicalsso(self):
        """See `IPrivateApplication`."""
        return getUtility(ICanonicalSSOApplication)

    @property
    def featureflags(self):
        """See `IPrivateApplication`."""
        return getUtility(IFeatureFlagApplication)

    @property
    def git(self):
        """See `IPrivateApplication`."""
        return getUtility(IGitApplication)


class ISelfTest(Interface):
    """XMLRPC external interface for testing the XMLRPC external interface."""

    def make_fault():
        """Returns an xmlrpc fault."""

    def concatenate(string1, string2):
        """Return the concatenation of the two given strings."""

    def hello():
        """Return a greeting to the one calling the method."""

    def raise_exception():
        """Raise an exception."""


@implementer(ISelfTest)
class SelfTest(LaunchpadXMLRPCView):
    def make_fault(self):
        """Returns an xmlrpc fault."""
        return xmlrpc.client.Fault(666, "Yoghurt and spanners.")

    def concatenate(self, string1, string2):
        """Return the concatenation of the two given strings."""
        return "%s %s" % (string1, string2)

    def hello(self):
        """Return a greeting to the logged in user."""
        caller = getUtility(ILaunchBag).user
        if caller is not None:
            caller_name = caller.displayname
        else:
            caller_name = "Anonymous"
        return "Hello %s." % caller_name

    def raise_exception(self):
        raise RuntimeError("selftest exception")
