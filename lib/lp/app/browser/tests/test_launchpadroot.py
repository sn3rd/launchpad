# Copyright 2010-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests related to ILaunchpadRoot."""

from fixtures import FakeLogger
from zope.component import getUtility
from zope.security.checker import selectChecker

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pillar import IPillarNameSet
from lp.services.beautifulsoup import BeautifulSoup, SoupStrainer
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.interfaces import ILaunchpadRoot
from lp.testing import (
    TestCaseWithFactory,
    login_admin,
    login_person,
    record_two_runs,
)
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.publication import test_traverse
from lp.testing.views import create_initialized_view, create_view


class LaunchpadRootPermissionTest(TestCaseWithFactory):
    """Test for the ILaunchpadRoot permission"""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        TestCaseWithFactory.setUp(self)
        self.root = getUtility(ILaunchpadRoot)
        self.admin = getUtility(IPersonSet).getByEmail("foo.bar@canonical.com")
        # Use a FakeLogger fixture to prevent Memcached warnings to be
        # printed to stdout while browsing pages.
        self.useFixture(FakeLogger())

    def setUpRegistryExpert(self):
        """Create a registry expert and logs in as them."""
        login_person(self.admin)
        self.expert = self.factory.makePerson()
        getUtility(ILaunchpadCelebrities).registry_experts.addMember(
            self.expert, self.admin
        )
        login_person(self.expert)

    def test_anonymous_cannot_edit(self):
        self.assertFalse(
            check_permission("launchpad.Edit", self.root),
            "Anonymous user shouldn't have launchpad.Edit on ILaunchpadRoot",
        )

    def test_regular_user_cannot_edit(self):
        login_person(self.factory.makePerson())
        self.assertFalse(
            check_permission("launchpad.Edit", self.root),
            "Regular users shouldn't have launchpad.Edit on ILaunchpadRoot",
        )

    def test_registry_expert_can_edit(self):
        self.setUpRegistryExpert()
        self.assertTrue(
            check_permission("launchpad.Edit", self.root),
            "Registry experts should have launchpad.Edit on ILaunchpadRoot",
        )

    def test_admins_can_edit(self):
        login_person(self.admin)
        self.assertTrue(
            check_permission("launchpad.Edit", self.root),
            "Admins should have launchpad.Edit on ILaunchpadRoot",
        )

    def test_featured_projects_view_requires_edit(self):
        view = create_view(self.root, "+featuredprojects")
        checker = selectChecker(view)
        self.assertEqual("launchpad.Edit", checker.permission_id("__call__"))

    def test_featured_projects_manage_link_requires_edit(self):
        self.setUpRegistryExpert()
        view = create_initialized_view(
            self.root, "index.html", principal=self.expert
        )
        content = BeautifulSoup(view(), parse_only=SoupStrainer("a"))
        self.assertTrue(
            content.find("a", href="+featuredprojects"),
            "Cannot find the +featuredprojects link on the first page",
        )


class TestLaunchpadRootNavigation(TestCaseWithFactory):
    """Test for the LaunchpadRootNavigation."""

    layer = DatabaseFunctionalLayer

    def test_support(self):
        # The /support link redirects to answers.
        context, view, request = test_traverse("http://launchpad.test/support")
        view()
        self.assertEqual(301, request.response.getStatus())
        self.assertEqual(
            "http://answers.launchpad.test/launchpad",
            request.response.getHeader("location"),
        )

    def test_feedback(self):
        # The /feedback link redirects to the help site.
        context, view, request = test_traverse(
            "http://launchpad.test/feedback"
        )
        view()
        self.assertEqual(301, request.response.getStatus())
        self.assertEqual(
            (
                "https://documentation.ubuntu.com/launchpad/en/latest/user/"
                "reference/launchpad-and-community/feedback-on-launchpad/"
            ),
            request.response.getHeader("location"),
        )


class LaunchpadRootIndexViewTestCase(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        # Use a FakeLogger fixture to prevent Memcached warnings to be
        # printed to stdout while browsing pages.
        self.useFixture(FakeLogger())

    def test_has_logo_without_watermark(self):
        root = getUtility(ILaunchpadRoot)
        user = self.factory.makePerson()
        login_person(user)
        view = create_initialized_view(root, "index.html", principal=user)
        markup = BeautifulSoup(view(), parse_only=SoupStrainer(id="document"))
        self.assertIs(False, view.has_watermark)
        self.assertIs(None, markup.find(True, id="watermark"))
        logo = markup.find(True, id="launchpad-logo-and-name")
        self.assertIsNot(None, logo)
        self.assertEqual("/@@/launchpad-logo-and-name.svg", logo["src"])

    def test_featured_projects_query_count(self):
        def add_featured_projects():
            product = self.factory.makeProduct()
            project = self.factory.makeProject()
            distribution = self.factory.makeDistribution()
            for pillar in product, project, distribution:
                pillar.icon = self.factory.makeLibraryFileAlias(db_only=True)
                getUtility(IPillarNameSet).add_featured_project(pillar)

        root = getUtility(ILaunchpadRoot)
        user = self.factory.makePerson()
        recorder1, recorder2 = record_two_runs(
            lambda: create_initialized_view(
                root, "index.html", principal=user
            )(),
            add_featured_projects,
            5,
            login_method=login_admin,
        )
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))
