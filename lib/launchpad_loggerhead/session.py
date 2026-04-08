# Copyright 2009-2010 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Simple session manager tuned for the needs of launchpad-loggerhead."""

from werkzeug.http import dump_cookie, parse_cookie

from launchpad_loggerhead.cookie import LaunchpadSecureCookie
from lp.services.config import config


class SessionHandler:
    """Middleware that provides a cookie-based session.

    The session dict is stored, pickled (and HMACed), in a cookie, so don't
    store very much in the session!
    """

    def __init__(self, application, session_var, secret=None):
        """Initialize a SessionHandler instance.

        :param application: This is the wrapped application which will have
            access to the ``environ[session_var]`` dictionary managed by this
            middleware.
        :param session_var: The key under which to store the session
            dictionary in the environment.
        :param secret: A secret value used for signing the cookie.  If not
            supplied, a new secret will be used for each instantiation of the
            SessionHandler.
        """
        self.application = application
        self.session_var = session_var
        self._secret = secret
        self.cookie_name = "%s.lh" % config.launchpad_session.cookie

    def __call__(self, environ, start_response):
        """Process a request."""
        cookie = parse_cookie(environ).get(self.cookie_name, "")
        session = LaunchpadSecureCookie.unserialize(cookie, self._secret)
        existed = bool(session)
        environ[self.session_var] = session

        def response_hook(status, response_headers, exc_info=None):
            session = environ.pop(self.session_var)
            cookie_kwargs = {
                "path": "/",
                "httponly": True,
                "secure": environ["wsgi.url_scheme"] == "https",
            }
            if session:
                cookie = dump_cookie(
                    self.cookie_name, session.serialize(), **cookie_kwargs
                )
                response_headers.append(("Set-Cookie", cookie))
            elif existed:
                # Delete the cookie.
                cookie = dump_cookie(
                    self.cookie_name, "", expires=0, **cookie_kwargs
                )
                response_headers.append(("Set-Cookie", cookie))
            return start_response(status, response_headers, exc_info)

        return self.application(environ, response_hook)
