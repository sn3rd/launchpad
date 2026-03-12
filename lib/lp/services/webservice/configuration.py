# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A configuration class describing the Launchpad web service."""

__all__ = [
    "LaunchpadWebServiceConfiguration",
]

import six
from lazr.restful.simple import BaseWebServiceConfiguration
from zope.component import getUtility
from zope.security.interfaces import Unauthorized

from lp.app import versioninfo
from lp.services.config import config
from lp.services.database.sqlbase import block_implicit_flushes
from lp.services.webapp.canonicalurl import nearest_adapter
from lp.services.webapp.interaction import get_interaction_extras
from lp.services.webapp.interfaces import ILaunchBag, ILaunchpadContainer
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.servers import (
    WebServiceClientRequest,
    WebServicePublication,
)


class LaunchpadWebServiceConfiguration(BaseWebServiceConfiguration):
    path_override = "api"
    active_versions = ["beta", "1.0", "devel"]
    last_version_with_mutator_named_operations = "beta"
    first_version_with_total_size_link = "devel"
    view_permission = "launchpad.LimitedView"
    require_explicit_versions = True
    compensate_for_mod_compress_etag_modification = True
    enable_server_side_representation_cache = False

    service_description = (
        "The Launchpad web service allows automated"
        " clients to access most of the functionality available on the"
        " Launchpad web site. For help getting started, see"
        ' <a href="https://documentation.ubuntu.com/launchpad/user/'
        'how-to/launchpad-api/">the help wiki.</a>'
    )

    version_descriptions = {
        "beta": """This is the first version of the web service ever
        published. Its end-of-life date is April 2011, the same as the
        Ubuntu release "Karmic Koala".""",
        "1.0": """This version of the web service removes unnecessary
        named operations. It was introduced in March 2010, and its
        end-of-life date is April 2015, the same as the server version
        of the Ubuntu release "Lucid Lynx".""",
        "devel": """This version of the web service reflects the most
        recent changes made. It may abruptly change without
        warning. Periodically, these changes are bundled up and given a
        permanent version number.""",
    }

    @property
    def use_https(self):
        return config.vhosts.use_https

    @property
    def code_revision(self):
        return str(versioninfo.revision)

    def createRequest(self, body_instream, environ):
        """See `IWebServiceConfiguration`."""
        # The request is going to try to decode the 'PATH_INFO' using utf-8,
        # so if it is currently unicode, encode it.
        if "PATH_INFO" in environ:
            environ["PATH_INFO"] = six.ensure_binary(environ["PATH_INFO"])
        request = WebServiceClientRequest(body_instream, environ)
        request.setPublication(WebServicePublication(None))
        return request

    @property
    def default_batch_size(self):
        return config.launchpad.default_batch_size

    @property
    def max_batch_size(self):
        return config.launchpad.max_batch_size

    @property
    def show_tracebacks(self):
        """See `IWebServiceConfiguration`.

        People who aren't developers shouldn't be shown any
        information about the exception that caused an internal server
        error. It might contain private information.
        """
        is_developer = getUtility(ILaunchBag).developer
        return is_developer or config.canonical.show_tracebacks

    def get_request_user(self):
        """See `IWebServiceConfiguration`."""
        return getUtility(ILaunchBag).user

    @block_implicit_flushes
    def checkRequest(self, context, required_scopes):
        """See `IWebServiceConfiguration`."""
        access_token = get_interaction_extras().access_token
        if access_token is None:
            return

        # The access token must be for a target that either exactly matches
        # or contains the context object.
        if access_token.target == context:
            pass
        else:
            container = nearest_adapter(context, ILaunchpadContainer)
            if not container.isWithin(
                canonical_url(access_token.target, force_local_path=True)
            ):
                raise Unauthorized(
                    "Current authentication does not allow access to this "
                    "object."
                )

        if not required_scopes:
            raise Unauthorized(
                "Current authentication only allows calling scoped methods."
            )
        elif not any(
            scope.title in required_scopes for scope in access_token.scopes
        ):
            raise Unauthorized(
                "Current authentication does not allow calling this method "
                "(one of these scopes is required: %s)."
                % ", ".join("'%s'" % scope for scope in required_scopes)
            )
