# Copyright 2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the `BugContextMenu`."""

from zope.component import getUtility

from lp.bugs.browser.bug import BugContextMenu
from lp.bugs.enums import BugNotificationLevel
from lp.services.features import get_relevant_feature_controller
from lp.services.webapp.interfaces import IOpenLaunchBag
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.soyuz.interfaces.archive import ARCHIVE_BUGS_FEATURE_FLAG
from lp.testing import (
    TestCaseWithFactory,
    feature_flags,
    person_logged_in,
    set_feature_flag,
)
from lp.testing.layers import DatabaseFunctionalLayer
from lp.testing.views import create_initialized_view


class TestBugContextMenu(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug = self.factory.makeBug()
        # We need to put the Bug and default BugTask into the LaunchBag
        # because BugContextMenu relies on the LaunchBag to populate its
        # context property
        launchbag = getUtility(IOpenLaunchBag)
        launchbag.add(self.bug)
        launchbag.add(self.bug.default_bugtask)
        self.context_menu = BugContextMenu(self.bug)

    def test_text_for_muted_subscriptions(self):
        # If a user has a mute on a bug it's recorded internally as a
        # type of subscription. However, the subscription text of the
        # BugContextMenu will still read 'Subscribe'.
        person = self.factory.makePerson()
        with feature_flags():
            with person_logged_in(person):
                self.bug.mute(person, person)
                link = self.context_menu.subscription()
                self.assertEqual("Subscribe", link.text)

    def test_mute_subscription_link(self):
        # The mute_subscription() method of BugContextMenu will return a
        # Link whose text will alter depending on whether or not they
        # have a mute on the bug.
        person = self.factory.makePerson()
        with feature_flags():
            with person_logged_in(person):
                # If the user hasn't muted the bug, the link text will
                # reflect this.
                link = self.context_menu.mute_subscription()
                self.assertEqual("Mute bug mail", link.text)
                # Once the user has muted the bug, the link text will
                # change.
                self.bug.mute(person, person)
                link = self.context_menu.mute_subscription()
                self.assertEqual("Unmute bug mail", link.text)

    def test_mute_help_available(self):
        # There is a help link available next to the mute/unmute button.
        person = self.factory.makePerson()
        with feature_flags():
            with person_logged_in(person):
                self.bug.subscribe(
                    person, person, level=BugNotificationLevel.METADATA
                )
                self.bug.mute(person, person)
                request = LaunchpadTestRequest()
                request.features = get_relevant_feature_controller()
                view = create_initialized_view(
                    self.bug, name="+portlet-subscription", request=request
                )
                html = view.render()
        self.assertTrue('class="sprite maybe action-icon mute-help"' in html)

    def test_addppa_link_disabled_without_feature_flag(self):
        # The 'Also affects PPA/package' link is disabled when the
        # archive bugs feature flag is off.
        with feature_flags():
            link = self.context_menu.addppa()
        self.assertFalse(link.enabled)

    def test_addppa_link_enabled_with_feature_flag(self):
        # The 'Also affects PPA/package' link is enabled when the
        # archive bugs feature flag is on (and the user has edit permission).
        with feature_flags():
            set_feature_flag(ARCHIVE_BUGS_FEATURE_FLAG, "on")
            with person_logged_in(self.bug.owner):
                link = self.context_menu.addppa()
        self.assertTrue(link.enabled)
        self.assertEqual("+ppatask", link.target)
