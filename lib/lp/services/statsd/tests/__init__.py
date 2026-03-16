# Copyright 2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Utility mixins for testing statsd handling"""

__all__ = ["StatsMixin"]

from unittest import mock

from fixtures import MockPatchObject
from zope.component import getUtility

from lp.services.statsd.interfaces.statsd_client import IStatsdClient


class StatsMixin:
    def setUpStats(self):
        # Install a mock statsd client so we can assert against the call
        # counts and args.
        self.pushConfig("statsd", environment="test")
        statsd_client = getUtility(IStatsdClient)
        self.stats_client = mock.Mock()
        self.useFixture(
            MockPatchObject(statsd_client, "_client", self.stats_client)
        )

    def filterStatsdCallsByName(self, call_name: str) -> list:
        """
        Filter for a specific type of call when testing.
        Returns only calls that start with the given call_name.
        """
        return [
            call[0][0]
            for call in self.stats_client.incr.call_args_list
            if call[0][0].startswith(call_name)
        ]
