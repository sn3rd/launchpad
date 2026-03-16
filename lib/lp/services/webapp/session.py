# Copyright 2009-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).
#
# Portions from zope.session, which is:
#
# Copyright (c) 2004 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.

"""Support for browser-cookie sessions."""

import base64
import hmac
import random
import time
from email.utils import formatdate
from hashlib import sha1
from http.cookiejar import domain_match
from time import process_time

from lazr.uri import URI
from zope.component import adapter, getUtility
from zope.interface import implementer
from zope.publisher.interfaces import IRequest
from zope.publisher.interfaces.http import IHTTPApplicationRequest

from lp.services.config import config
from lp.services.database.sqlbase import session_store
from lp.services.propertycache import cachedproperty
from lp.services.webapp.interfaces import (
    IClientIdManager,
    ISession,
    ISessionDataContainer,
)

SECONDS = 1
MINUTES = 60 * SECONDS
HOURS = 60 * MINUTES
DAYS = 24 * HOURS
YEARS = 365 * DAYS


transtable = bytes.maketrans(b"+/", b"-.")


def encode_digest(s):
    """Encode SHA digest for cookie."""
    return base64.encodebytes(s)[:-2].translate(transtable)


@implementer(ISession)
@adapter(IRequest)
class Session:
    """Default implementation of `lp.services.webapp.interfaces.ISession`."""

    def __init__(self, request):
        self.client_id = getUtility(IClientIdManager).getClientId(request)

    def get(self, product_id, default=None):
        """Get session data."""
        # The ISessionDataContainer contains two levels:
        # ISessionDataContainer[client_id] == ISessionData
        # ISessionDataContainer[client_id][product_id] == ISessionPkgData
        sdc = getUtility(ISessionDataContainer)
        try:
            sd = sdc[self.client_id]
        except KeyError:
            return default

        return sd.get(product_id, default)

    def __getitem__(self, product_id):
        """Get or create session data."""
        sdc = getUtility(ISessionDataContainer)

        # The ISessionDataContainer contains two levels:
        # ISessionDataContainer[client_id] == ISessionData
        # ISessionDataContainer[client_id][product_id] == ISessionPkgData
        return sdc[self.client_id][product_id]


def get_cookie_domain(request_domain):
    """Return a string suitable for use as the domain parameter of a cookie.

    The returned domain value should allow the cookie to be seen by
    all virtual hosts of the Launchpad instance.  If no matching
    domain is known, None is returned.
    """
    domain = config.vhost.mainsite.hostname
    assert not domain.startswith("."), "domain should not start with '.'"
    dotted_domain = "." + domain
    if domain_match(request_domain, domain) or domain_match(
        request_domain, dotted_domain
    ):
        return dotted_domain
    return None


ANNOTATION_KEY = "lp.services.webapp.session.sid"


@implementer(IClientIdManager)
class LaunchpadCookieClientIdManager:
    def __init__(self):
        self.namespace = config.launchpad_session.cookie

    def getClientId(self, request):
        sid = self.getRequestId(request)
        if sid is None:
            # XXX gary 21-Oct-2008 bug 285803
            # Our session data container (see pgsession.py in the same
            # directory) explicitly calls setRequestId the first time a
            # __setitem__ is called. Therefore, we only generate one here,
            # and do not set it. This keeps the session id out of anonymous
            # sessions.  Unfortunately, it is also Rube-Goldbergian: we should
            # consider switching to our own session/cookie machinery that
            # suits us better.
            sid = request.annotations.get(ANNOTATION_KEY)
            if sid is None:
                sid = self.generateUniqueId()
                request.annotations[ANNOTATION_KEY] = sid
        return sid

    def generateUniqueId(self):
        """Generate a new, random, unique id."""
        data = "%.20f%.20f%.20f" % (
            random.random(),
            time.time(),
            process_time(),
        )
        digest = sha1(data.encode()).digest()  # nosec B324
        s = encode_digest(digest)
        # we store a HMAC of the random value together with it, which makes
        # our session ids unforgeable.
        mac = hmac.new(s, self.secret.encode(), digestmod=sha1).digest()
        return (s + encode_digest(mac)).decode()

    def getRequestId(self, request):
        """Return the browser id encoded in request as a string.

        Return `None` if an id is not set.
        """
        response_cookie = request.response.getCookie(self.namespace)
        if response_cookie:
            sid = response_cookie["value"]
        else:
            request = IHTTPApplicationRequest(request)
            sid = request.getCookies().get(self.namespace, None)

        # If there is an id set on the response, use that but don't trust
        # it.  We need to check the response in case there has already been
        # a new session created during the course of this request.

        if sid is None or len(sid) != 54:
            return None
        s, mac = sid[:27], sid[27:]

        # HMAC is specified to work on byte strings only so make
        # sure to feed it that by encoding
        mac_with_my_secret = hmac.new(
            s.encode(), self.secret.encode(), digestmod=sha1
        ).digest()
        mac_with_my_secret = encode_digest(mac_with_my_secret).decode()

        if mac_with_my_secret != mac:
            return None

        return sid

    @cachedproperty
    def secret(self):
        # Because our CookieClientIdManager is not persistent, we need to
        # pull the secret from some other data store - failing to do this
        # would mean a new secret is generated every time the server is
        # restarted, invalidating all old session information.
        # Secret is looked up here rather than in __init__, because
        # we can't be sure the database connections are setup at that point.
        store = session_store()
        result = store.execute("SELECT secret FROM secret")
        return result.get_one()[0]

    def setRequestId(self, request, id):
        """Set cookie with id on response.

        We force the domain key on the cookie to be set to allow our
        session to be shared between virtual hosts where possible, and
        we set the secure key to stop the session key being sent to
        insecure URLs like the Librarian.

        We also log the referrer url on creation of a new
        requestid so we can track where first time users arrive from.
        """
        response = request.response
        uri = URI(request.getURL())
        options = {}

        # Set the cookie lifetime to something big.  It should be larger
        # than our session expiry time.
        expires = formatdate(
            time.time() + 1 * YEARS, localtime=False, usegmt=True
        )
        options["expires"] = expires

        # Set domain attribute on cookie if vhosting requires it.
        cookie_domain = get_cookie_domain(uri.host)
        if cookie_domain is not None:
            options["domain"] = cookie_domain

        # Forbid browsers from exposing it to JS.
        options["HttpOnly"] = True

        # Set secure flag on cookie.
        if uri.scheme != "http":
            options["secure"] = True
        else:
            options["secure"] = False

        response.setCookie(
            self.namespace,
            id,
            path=request.getApplicationURL(path_only=True),
            **options,
        )

        response.setHeader(
            "Cache-Control", 'no-cache="Set-Cookie,Set-Cookie2"'
        )
        response.setHeader("Pragma", "no-cache")
        response.setHeader("Expires", "Mon, 26 Jul 1997 05:00:00 GMT")


idmanager = LaunchpadCookieClientIdManager()
