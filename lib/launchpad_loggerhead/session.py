# Copyright 2009-2010 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Simple session manager tuned for the needs of launchpad-loggerhead."""

import base64
import hashlib
import hmac
import pickle
import urllib.parse

from werkzeug.http import dump_cookie, parse_cookie

from lp.services.config import config


class LaunchpadSecureCookie(dict):
    """A dict-based signed cookie session."""

    hash_method = hashlib.sha256

    def __init__(self, data=None, secret_key=None):
        super().__init__(data or {})
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8", "replace")
        self.secret_key = secret_key

    @classmethod
    def _quote(cls, value):
        return base64.b64encode(pickle.dumps(value, protocol=2))

    @classmethod
    def _unquote(cls, value):
        return pickle.loads(base64.b64decode(value))

    def serialize(self):
        if self.secret_key is None:
            raise RuntimeError("no secret key defined")

        result = []
        mac = hmac.new(self.secret_key, None, self.hash_method)

        for key, value in sorted(self.items()):
            item = "{}={}".format(
                urllib.parse.quote_plus(key),
                self._quote(value).decode("ascii"),
            ).encode("ascii")
            result.append(item)
            mac.update(b"|" + item)

        return b"?".join(
            [base64.b64encode(mac.digest()).strip(), b"&".join(result)]
        )

    @classmethod
    def unserialize(cls, string, secret_key):
        if isinstance(string, str):
            string = string.encode("utf-8", "replace")
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8", "replace")

        try:
            base64_hash, data = string.split(b"?", 1)
        except (ValueError, IndexError):
            items = ()
        else:
            items = {}
            mac = hmac.new(secret_key, None, cls.hash_method)

            for item in data.split(b"&"):
                mac.update(b"|" + item)

                if b"=" not in item:
                    items = None
                    break

                key, value = item.split(b"=", 1)
                key = urllib.parse.unquote_plus(key.decode("ascii"))
                items[key] = value

            try:
                client_hash = base64.b64decode(base64_hash)
            except Exception:
                items = client_hash = None

            if items is not None and hmac.compare_digest(
                client_hash, mac.digest()
            ):
                try:
                    for key, value in items.items():
                        items[key] = cls._unquote(value)
                except Exception:
                    items = ()
            else:
                items = ()

        return cls(items, secret_key)


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
