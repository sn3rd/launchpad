# Copyright 2017-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""WSGI archive authorisation provider.

This is as lightweight as possible, as it runs on PPA frontends.
"""

__all__ = [
    "check_password",
]

import crypt
import sys
import time
from xmlrpc.client import Fault, ServerProxy  # nosec B411

import six
from defusedxml.xmlrpc import monkey_patch as _defused_monkey_patch

from lp.services.config import config
from lp.services.memcache.client import memcache_client_factory

_defused_monkey_patch()


def _log(environ, message, *args):
    """Log a message to the WSGI error stream."""
    # Ideally we might set up a proper logger instead, but that's more
    # effort than is justified by something this small.
    error_stream = environ.get("wsgi.errors", sys.stderr)
    if args:
        message = message % args
    error_stream.write(message + "\n")
    error_stream.flush()


def _get_archive_reference(environ):
    # Reconstruct the relevant part of the URL.  We don't care about where
    # we're installed.
    path = six.ensure_text(environ.get("SCRIPT_NAME") or "/", "ISO-8859-1")
    path_info = six.ensure_text(environ.get("PATH_INFO", ""), "ISO-8859-1")
    path += path_info if path else path_info[1:]
    # Extract the first three segments of the path, and rearrange them to
    # form an archive reference.
    path_parts = path.lstrip("/").split("/")
    if len(path_parts) >= 3:
        return "~%s/%s/%s" % (path_parts[0], path_parts[2], path_parts[1])
    else:
        _log(environ, "No archive reference found in URL '%s'.", path)


_memcache_client = memcache_client_factory(timeline=False)


def check_password(environ, user, password):
    # We have almost no viable ways to set the config instance name.
    # Normally it's set in LPCONFIG in the process environment, but we can't
    # control that for mod_wsgi, and Apache SetEnv directives are
    # intentionally not passed through to the WSGI environment.  However, we
    # *can* control the application group via the application-group option
    # to WSGIAuthUserScript, and overloading that as the config instance
    # name actually makes a certain amount of sense, so use that if it's
    # available.
    application_group = environ.get("mod_wsgi.application_group")
    if application_group:
        config.setInstance(application_group)

    archive_reference = _get_archive_reference(environ)
    if archive_reference is None:
        return None
    memcache_key = "archive-auth:%s:%s" % (archive_reference, user)
    crypted_password = _memcache_client.get(memcache_key)
    if (
        crypted_password
        and crypt.crypt(password, crypted_password) == crypted_password
    ):
        _log(environ, "%s@%s: Authorized (cached).", user, archive_reference)
        return True
    proxy = ServerProxy(config.personalpackagearchive.archive_api_endpoint)
    try:
        proxy.checkArchiveAuthToken(archive_reference, user, password)
        # Cache positive responses for a minute to reduce database load.
        _memcache_client.set(
            memcache_key,
            crypt.crypt(password, crypt.METHOD_SHA256),
            int(time.time()) + 60,
        )
        _log(environ, "%s@%s: Authorized.", user, archive_reference)
        return True
    except Fault as e:
        if e.faultCode == 410:  # Unauthorized
            _log(
                environ,
                "%s@%s: Password does not match.",
                user,
                archive_reference,
            )
            return False
        else:
            # Interpret any other fault as NotFound (320).
            _log(environ, e.faultString)
            return None
