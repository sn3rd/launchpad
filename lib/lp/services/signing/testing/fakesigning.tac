# Copyright 2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Twisted application configuration file for a fake signing service."""

import os

from twisted.application import service, strports
from twisted.web import server

from lp.services.daemons import readyservice
from lp.services.signing.testing.fakesigning import SigningServiceResource

application = service.Application("fakesigning")
svc = service.IServiceCollection(application)

# Service that announces when the daemon is ready.
readyservice.ReadyService().setServiceParent(svc)

site = server.Site(SigningServiceResource())
site.displayTracebacks = False

port = "tcp:%s" % os.environ["FAKE_SIGNING_PORT"]
strports.service(port, site).setServiceParent(svc)
