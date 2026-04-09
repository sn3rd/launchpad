# Copyright 2015-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test snap package views."""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import responses
import soupmatchers
import transaction
from fixtures import FakeLogger
from pymacaroons import Macaroon
from testtools.matchers import (
    AfterPreprocessing,
    Equals,
    Is,
    MatchesDict,
    MatchesListwise,
    MatchesSetwise,
    MatchesStructure,
    Not,
)
from zope.component import getUtility
from zope.publisher.interfaces import NotFound
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy
from zope.testbrowser.browser import LinkNotFoundError

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.widgets.tests.test_snapbuildchannels import (
    TestSnapBuildChannelsWidget,
)
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.errors import BranchHostingFault, GitRepositoryScanFault
from lp.code.tests.helpers import BranchHostingFixture, GitHostingFixture
from lp.registry.enums import (
    BranchSharingPolicy,
    PersonVisibility,
    TeamMembershipPolicy,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.propertycache import get_property_cache
from lp.services.webapp import canonical_url
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.snappy.browser.snap import (
    HintedSnapBuildChannelsWidget,
    SnapAdminView,
    SnapEditView,
    SnapView,
)
from lp.snappy.interfaces.snap import (
    SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG,
    CannotModifySnapProcessor,
    ISnapSet,
    SnapBuildRequestStatus,
)
from lp.snappy.interfaces.snappyseries import ISnappyDistroSeriesSet
from lp.snappy.interfaces.snapstoreclient import ISnapStoreClient
from lp.snappy.model.snap import Snap
from lp.testing import (
    BrowserTestCase,
    TestCaseWithFactory,
    admin_logged_in,
    api_url,
    login,
    login_admin,
    login_person,
    person_logged_in,
    time_counter,
)
from lp.testing.fakemethod import FakeMethod
from lp.testing.fixture import ZopeUtilityFixture
from lp.testing.layers import DatabaseFunctionalLayer, LaunchpadFunctionalLayer
from lp.testing.matchers import MatchesPickerText, MatchesTagText
from lp.testing.pages import (
    extract_text,
    find_main_content,
    find_tag_by_id,
    find_tags_by_class,
    get_feedback_messages,
    webservice_for_person,
)
from lp.testing.publication import test_traverse
from lp.testing.views import create_initialized_view, create_view


class TestSnapNavigation(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_canonical_url(self):
        owner = self.factory.makePerson(name="person")
        snap = self.factory.makeSnap(
            registrant=owner, owner=owner, name="snap"
        )
        self.assertEqual(
            "http://launchpad.test/~person/+snap/snap", canonical_url(snap)
        )

    def test_snap(self):
        snap = self.factory.makeSnap()
        obj, _, _ = test_traverse(
            "http://launchpad.test/~%s/+snap/%s" % (snap.owner.name, snap.name)
        )
        self.assertEqual(snap, obj)


class BaseTestSnapView(BrowserTestCase):
    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FakeLogger())
        self.snap_store_client = FakeMethod()
        self.snap_store_client.requestPackageUploadPermission = getUtility(
            ISnapStoreClient
        ).requestPackageUploadPermission
        self.useFixture(
            ZopeUtilityFixture(self.snap_store_client, ISnapStoreClient)
        )
        self.person = self.factory.makePerson(
            name="test-person", displayname="Test Person"
        )


class TestSnapAddView(BaseTestSnapView):
    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeUbuntuDistroSeries(
            version="13.10"
        )
        with admin_logged_in():
            self.snappyseries = self.factory.makeSnappySeries(
                preferred_distro_series=self.distroseries
            )

    def setUpDistroSeries(self):
        """Set up a distroseries with some available processors."""
        distroseries = self.factory.makeUbuntuDistroSeries()
        processor_names = ["386", "amd64", "hppa"]
        for name in processor_names:
            processor = getUtility(IProcessorSet).getByName(name)
            self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=name,
                processor=processor,
            )
        with admin_logged_in():
            self.factory.makeSnappySeries(preferred_distro_series=distroseries)
        return distroseries

    def assertProcessorControls(self, processors_control, enabled, disabled):
        matchers = [
            MatchesStructure.byEquality(optionValue=name, disabled=False)
            for name in enabled
        ]
        matchers.extend(
            [
                MatchesStructure.byEquality(optionValue=name, disabled=True)
                for name in disabled
            ]
        )
        self.assertThat(processors_control.controls, MatchesSetwise(*matchers))

    def test_initial_store_distro_series(self):
        # The initial store_distro_series uses the preferred distribution
        # series for the latest snappy series.
        self.useFixture(BranchHostingFixture(blob=b""))
        lts = self.factory.makeUbuntuDistroSeries(
            version="16.04", status=SeriesStatus.CURRENT
        )
        current = self.factory.makeUbuntuDistroSeries(
            version="16.10", status=SeriesStatus.CURRENT
        )
        with admin_logged_in():
            self.factory.makeSnappySeries(usable_distro_series=[lts, current])
            newest = self.factory.makeSnappySeries(
                preferred_distro_series=lts,
                usable_distro_series=[lts, current],
            )
        branch = self.factory.makeAnyBranch()
        with person_logged_in(self.person):
            view = create_initialized_view(branch, "+new-snap")
        self.assertThat(
            view.initial_values["store_distro_series"],
            MatchesStructure.byEquality(
                snappy_series=newest, distro_series=lts
            ),
        )

    def test_initial_store_distro_series_can_infer_distro_series(self):
        # If the latest snappy series supports inferring the distro series
        # from snapcraft.yaml, then we default to that.
        self.useFixture(BranchHostingFixture(blob=b""))
        lts = self.factory.makeUbuntuDistroSeries(
            version="16.04", status=SeriesStatus.CURRENT
        )
        with admin_logged_in():
            self.factory.makeSnappySeries(usable_distro_series=[lts])
            newest = self.factory.makeSnappySeries(
                preferred_distro_series=lts, can_infer_distro_series=True
            )
        branch = self.factory.makeAnyBranch()
        with person_logged_in(self.person):
            view = create_initialized_view(branch, "+new-snap")
        self.assertThat(
            view.initial_values["store_distro_series"],
            MatchesStructure(
                snappy_series=Equals(newest), distro_series=Is(None)
            ),
        )

    def test_create_new_snap_not_logged_in(self):
        branch = self.factory.makeAnyBranch()
        self.assertRaises(
            Unauthorized,
            self.getViewBrowser,
            branch,
            view_name="+new-snap",
            no_login=True,
        )

    def test_create_new_snap_bzr(self):
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        source_display = branch.display_name
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("snap-name", extract_text(content.h1))
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "Distribution series:\n%s\nEdit snap package"
            % self.distroseries.fullseriesname,
            MatchesTagText(content, "distro_series"),
        )
        self.assertThat(
            "Source:\n%s\nEdit snap package" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build source tarball:\nNo\nEdit snap package",
            MatchesTagText(content, "build_source_tarball"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt on request\nEdit snap package\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source archive for automatic builds:\n\nEdit snap package\n",
            MatchesTagText(content, "auto_build_archive"),
        )
        self.assertThat(
            "Pocket for automatic builds:\n\nEdit snap package",
            MatchesTagText(content, "auto_build_pocket"),
        )
        self.assertIsNone(find_tag_by_id(content, "auto_build_channels"))
        self.assertThat(
            "Builds of this snap package are not automatically uploaded to "
            "the store.\nEdit snap package",
            MatchesTagText(content, "store_upload"),
        )

    def test_create_new_snap_git(self):
        self.useFixture(GitHostingFixture(blob=b""))
        [git_ref] = self.factory.makeGitRefs()
        source_display = git_ref.display_name
        browser = self.getViewBrowser(
            git_ref, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("snap-name", extract_text(content.h1))
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "Distribution series:\n%s\nEdit snap package"
            % self.distroseries.fullseriesname,
            MatchesTagText(content, "distro_series"),
        )
        self.assertThat(
            "Source:\n%s\nEdit snap package" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build source tarball:\nNo\nEdit snap package",
            MatchesTagText(content, "build_source_tarball"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt on request\nEdit snap package\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source archive for automatic builds:\n\nEdit snap package\n",
            MatchesTagText(content, "auto_build_archive"),
        )
        self.assertThat(
            "Pocket for automatic builds:\n\nEdit snap package",
            MatchesTagText(content, "auto_build_pocket"),
        )
        self.assertIsNone(find_tag_by_id(content, "auto_build_channels"))
        self.assertThat(
            "Builds of this snap package are not automatically uploaded to "
            "the store.\nEdit snap package",
            MatchesTagText(content, "store_upload"),
        )

    def test_create_new_snap_project(self):
        self.useFixture(GitHostingFixture(blob=b""))
        project = self.factory.makeProduct()
        [git_ref] = self.factory.makeGitRefs()
        git_ref_shortened_path = git_ref.repository.shortened_path
        git_ref_path = git_ref.path
        source_display = git_ref.display_name
        browser = self.getViewBrowser(
            project, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl(name="field.vcs").value = "GIT"
        browser.getControl(name="field.git_ref.repository").value = (
            git_ref_shortened_path
        )
        browser.getControl(name="field.git_ref.path").value = git_ref_path
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("snap-name", extract_text(content.h1))
        self.assertThat(
            "Test Person", MatchesPickerText(content, "edit-owner")
        )
        self.assertThat(
            "Distribution series:\n%s\nEdit snap package"
            % self.distroseries.fullseriesname,
            MatchesTagText(content, "distro_series"),
        )
        self.assertThat(
            "Source:\n%s\nEdit snap package" % source_display,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build source tarball:\nNo\nEdit snap package",
            MatchesTagText(content, "build_source_tarball"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt on request\nEdit snap package\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source archive for automatic builds:\n\nEdit snap package\n",
            MatchesTagText(content, "auto_build_archive"),
        )
        self.assertThat(
            "Pocket for automatic builds:\n\nEdit snap package",
            MatchesTagText(content, "auto_build_pocket"),
        )
        self.assertIsNone(find_tag_by_id(content, "auto_build_channels"))
        self.assertThat(
            "Builds of this snap package are not automatically uploaded to "
            "the store.\nEdit snap package",
            MatchesTagText(content, "store_upload"),
        )

    def test_create_new_snap_users_teams_as_owner_options(self):
        # Teams that the user is in are options for the snap package owner.
        self.useFixture(BranchHostingFixture(blob=b""))
        self.factory.makeTeam(
            name="test-team", displayname="Test Team", members=[self.person]
        )
        branch = self.factory.makeAnyBranch()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        options = browser.getControl("Owner").displayOptions
        self.assertEqual(
            ["Test Person (test-person)", "Test Team (test-team)"],
            sorted(str(option) for option in options),
        )

    def test_create_new_snap_public(self):
        # Public owner implies public snap.
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()

        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "public-snap"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("public-snap", extract_text(content.h1))
        self.assertEqual(
            "This snap contains Public information",
            extract_text(find_tag_by_id(browser.contents, "privacy")),
        )

    def test_create_new_snap_private_link(self):
        # A link to create new snaps is displayed even for private content.
        login_person(self.person)
        branch = self.factory.makeAnyBranch(
            owner=self.person, information_type=InformationType.USERDATA
        )
        browser = self.getViewBrowser(branch, user=self.person)
        browser.getLink("Create snap package")

    def test_create_new_snap_private(self):
        # Creates a private snap for a private project.
        login_person(self.person)
        self.factory.makeProduct(
            name="private-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        [git_ref] = self.factory.makeGitRefs()

        browser = self.getViewBrowser(
            git_ref, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "private-snap"
        browser.getControl(name="field.information_type").value = "PROPRIETARY"
        browser.getControl(name="field.project").value = "private-project"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("private-snap", extract_text(content.h1))
        self.assertEqual(
            "This snap contains Private information",
            extract_text(find_tag_by_id(browser.contents, "privacy")),
        )
        login_admin()
        snap = getUtility(ISnapSet).getByName(self.person, "private-snap")
        self.assertEqual(InformationType.PROPRIETARY, snap.information_type)

    def test_create_new_snap_private_without_project_fails(self):
        # It should not be possible to create a private snap with
        # information_type not matching project's branch_sharing_policy.
        login_person(self.person)
        [git_ref] = self.factory.makeGitRefs()

        browser = self.getViewBrowser(
            git_ref, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "private-snap"
        browser.getControl(name="field.information_type").value = "PROPRIETARY"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("Create a new snap package", extract_text(content.h1))
        messages = find_tags_by_class(browser.contents, "message")
        self.assertEqual(2, len(messages))
        top_msg, field_msg = messages
        self.assertEqual("There is 1 error.", extract_text(top_msg))
        self.assertEqual(
            "Private snap recipes must be associated with a project.",
            extract_text(field_msg),
        )
        login_admin()
        snap = IStore(Snap).find(Snap, Snap.name == "private-snap").one()
        self.assertIsNone(snap)

    def test_create_new_snap_private_with_invalid_information_type_fails(self):
        # It should not not be possible to create a private snap without
        # setting a project.
        login_person(self.person)
        # The project is proprietary, with branch policy beign proprietary
        # too. We can only create proprietary snaps.
        self.factory.makeProduct(
            name="private-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        [git_ref] = self.factory.makeGitRefs()

        browser = self.getViewBrowser(
            git_ref, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "private-snap"
        browser.getControl(name="field.information_type").value = "PUBLIC"
        browser.getControl(name="field.project").value = "private-project"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("Create a new snap package", extract_text(content.h1))
        messages = find_tags_by_class(browser.contents, "message")
        self.assertEqual(2, len(messages))
        top_msg, field_msg = messages
        self.assertEqual("There is 1 error.", extract_text(top_msg))
        expected_msg = (
            "Project private-project only accepts the following information "
            "types: Proprietary."
        )
        self.assertEqual(expected_msg, extract_text(field_msg))
        login_admin()
        snap = IStore(Snap).find(Snap, Snap.name == "private-snap").one()
        self.assertIsNone(snap)

    def test_create_new_snap_build_source_tarball(self):
        # We can create a new snap and ask for it to build a source tarball.
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl("Build source tarball").selected = True
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Build source tarball:\nYes\nEdit snap package",
            MatchesTagText(content, "build_source_tarball"),
        )

    def test_create_new_snap_build_path(self):
        # We can create a new snap with a build path set.
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl(name="field.build_path").value = "snap/dir"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Build path:\nsnap/dir\nEdit snap package",
            MatchesTagText(content, "build_path"),
        )
        snap = getUtility(ISnapSet).getByName(self.person, "snap-name")
        self.assertEqual("snap/dir", snap.build_path)

    def test_create_new_snap_auto_build(self):
        # Creating a new snap and asking for it to be automatically built
        # sets all the appropriate fields.
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        archive = self.factory.makeArchive()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl(
            "Automatically build when branch changes"
        ).selected = True
        browser.getControl("PPA").click()
        browser.getControl(name="field.auto_build_archive.ppa").value = (
            archive.reference
        )
        browser.getControl("Pocket for automatic builds").value = ["SECURITY"]
        browser.getControl(name="field.auto_build_channels.core").value = (
            "stable"
        )
        browser.getControl(name="field.auto_build_channels.core18").value = (
            "beta"
        )
        browser.getControl(name="field.auto_build_channels.core20").value = (
            "edge/feature"
        )
        browser.getControl(
            name="field.auto_build_channels.snapcraft"
        ).value = "edge"
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Build schedule:\n(?)\nBuilt automatically\nEdit snap package\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source archive for automatic builds:\n%s\nEdit snap package\n"
            % archive.displayname,
            MatchesTagText(content, "auto_build_archive"),
        )
        self.assertThat(
            "Pocket for automatic builds:\nSecurity\nEdit snap package",
            MatchesTagText(content, "auto_build_pocket"),
        )
        self.assertThat(
            "Source snap channels for automatic builds:\nEdit snap package\n"
            "core\nstable\ncore18\nbeta\n"
            "core20\nedge/feature\nsnapcraft\nedge\n",
            MatchesTagText(content, "auto_build_channels"),
        )

    @responses.activate
    def test_create_new_snap_store_upload(self):
        # Creating a new snap and asking for it to be automatically uploaded
        # to the store sets all the appropriate fields and redirects to SSO
        # for authorization.
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        view_url = canonical_url(branch, view_name="+new-snap")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl("Automatically upload to store").selected = True
        browser.getControl("Registered store package name").value = (
            "store-name"
        )
        self.assertFalse(browser.getControl("Stable").selected)
        browser.getControl(name="field.store_channels.add_track").value = (
            "track"
        )
        browser.getControl(name="field.store_channels.add_risk").value = [
            "edge"
        ]
        root_macaroon = Macaroon()
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc, "", "dummy"
        )
        root_macaroon_raw = root_macaroon.serialize()
        self.pushConfig("snappy", store_url="http://sca.example/")
        responses.add(
            "POST",
            "http://sca.example/dev/api/acl/",
            json={"macaroon": root_macaroon_raw},
        )
        browser.getControl("Create snap package").click()
        login_person(self.person)
        snap = getUtility(ISnapSet).getByName(self.person, "snap-name")
        self.assertThat(
            snap,
            MatchesStructure.byEquality(
                owner=self.person,
                distro_series=self.distroseries,
                name="snap-name",
                source=branch,
                store_upload=True,
                store_series=self.snappyseries,
                store_name="store-name",
                store_secrets={"root": root_macaroon_raw},
                store_channels=["track/edge"],
            ),
        )
        [call] = responses.calls
        self.assertThat(
            call.request,
            MatchesStructure.byEquality(
                url="http://sca.example/dev/api/acl/", method="POST"
            ),
        )
        expected_body = {
            "packages": [
                {
                    "name": "store-name",
                    "series": self.snappyseries.name,
                }
            ],
            "permissions": ["package_upload"],
        }
        self.assertEqual(expected_body, json.loads(call.request.body))
        self.assertEqual(303, browser.responseStatusCode)
        parsed_location = urlsplit(browser.headers["Location"])
        self.assertEqual(
            urlsplit(canonical_url(snap) + "/+authorize/+login")[:3],
            parsed_location[:3],
        )
        expected_args = {
            "discharge_macaroon_action": ["field.actions.complete"],
            "discharge_macaroon_field": ["field.discharge_macaroon"],
            "macaroon_caveat_id": ["dummy"],
        }
        self.assertEqual(expected_args, parse_qs(parsed_location[3]))

    def test_create_new_snap_display_processors(self):
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        self.setUpDistroSeries()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(
            ["Intel 386 (386)", "AMD 64bit (amd64)", "HPPA Processor (hppa)"],
            [extract_text(option) for option in processors.displayOptions],
        )
        self.assertContentEqual(["386", "amd64", "hppa"], processors.options)
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)

    def test_create_new_snap_display_restricted_processors(self):
        # A restricted processor is shown with a disabled (greyed out)
        # checkbox in the UI.
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        distroseries = self.setUpDistroSeries()
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="armhf",
            processor=proc_armhf,
        )
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        processors = browser.getControl(name="field.processors")
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )

    def test_create_new_snap_processors(self):
        self.useFixture(BranchHostingFixture(blob=b""))
        branch = self.factory.makeAnyBranch()
        self.setUpDistroSeries()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        processors = browser.getControl(name="field.processors")
        processors.value = ["386", "amd64"]
        browser.getControl(name="field.name").value = "snap-name"
        browser.getControl("Create snap package").click()
        login_person(self.person)
        snap = getUtility(ISnapSet).getByName(self.person, "snap-name")
        self.assertContentEqual(
            ["386", "amd64"], [proc.name for proc in snap.processors]
        )

    def test_create_new_snap_infer_distro_series(self):
        self.useFixture(BranchHostingFixture(blob=b""))
        with admin_logged_in():
            self.snappyseries.can_infer_distro_series = True
        branch = self.factory.makeAnyBranch()
        browser = self.getViewBrowser(
            branch, view_name="+new-snap", user=self.person
        )
        browser.getControl(name="field.name").value = "snap-name"
        self.assertEqual(
            [self.snappyseries.name],
            browser.getControl(name="field.store_distro_series").value,
        )
        self.assertEqual(
            self.snappyseries.name,
            browser.getControl(name="field.store_distro_series").options[0],
        )
        browser.getControl("Create snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("snap-name", extract_text(content.h1))
        self.assertIsNone(find_tag_by_id(content, "distro_series"))

    def test_initial_name_extraction_bzr_success(self):
        self.useFixture(
            BranchHostingFixture(
                blob=b"name: test-snap",
            )
        )
        branch = self.factory.makeBranch()
        view = create_initialized_view(branch, "+new-snap")
        initial_values = view.initial_values
        self.assertIn("store_name", initial_values)
        self.assertEqual("test-snap", initial_values["store_name"])

    def test_initial_name_extraction_bzr_error(self):
        self.useFixture(BranchHostingFixture()).getBlob = FakeMethod(
            failure=BranchHostingFault
        )
        branch = self.factory.makeBranch()
        view = create_initialized_view(branch, "+new-snap")
        initial_values = view.initial_values
        self.assertIn("store_name", initial_values)
        self.assertIsNone(initial_values["store_name"])

    def test_initial_name_extraction_bzr_no_name(self):
        self.useFixture(BranchHostingFixture(blob=b"some: nonsense"))
        branch = self.factory.makeBranch()
        view = create_initialized_view(branch, "+new-snap")
        initial_values = view.initial_values
        self.assertIn("store_name", initial_values)
        self.assertIsNone(initial_values["store_name"])

    def test_initial_name_extraction_git_success(self):
        self.useFixture(GitHostingFixture(blob=b"name: test-snap"))
        [git_ref] = self.factory.makeGitRefs()
        view = create_initialized_view(git_ref, "+new-snap")
        initial_values = view.initial_values
        self.assertIn("store_name", initial_values)
        self.assertEqual("test-snap", initial_values["store_name"])

    def test_initial_name_extraction_git_error(self):
        self.useFixture(GitHostingFixture()).getBlob = FakeMethod(
            failure=GitRepositoryScanFault
        )
        [git_ref] = self.factory.makeGitRefs()
        view = create_initialized_view(git_ref, "+new-snap")
        initial_values = view.initial_values
        self.assertIn("store_name", initial_values)
        self.assertIsNone(initial_values["store_name"])

    def test_initial_name_extraction_git_no_name(self):
        self.useFixture(GitHostingFixture(blob=b"some: nonsense"))
        [git_ref] = self.factory.makeGitRefs()
        view = create_initialized_view(git_ref, "+new-snap")
        initial_values = view.initial_values
        self.assertIn("store_name", initial_values)
        self.assertIsNone(initial_values["store_name"])


class TestSnapAdminView(BaseTestSnapView):
    def test_unauthorized(self):
        # A non-admin user cannot administer a snap package.
        login_person(self.person)
        snap = self.factory.makeSnap(registrant=self.person)
        snap_url = canonical_url(snap)
        browser = self.getViewBrowser(snap, user=self.person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Administer snap package"
        )
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            snap_url + "/+admin",
            user=self.person,
        )

    def test_admin_snap(self):
        # Admins can change require_virtualized, privacy, and allow_internet.

        login("admin@canonical.com")
        admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).admin]
        )
        login_person(self.person)
        project = self.factory.makeProduct(name="my-project")
        with person_logged_in(project.owner):
            project.information_type = InformationType.PROPRIETARY
        snap = self.factory.makeSnap(registrant=self.person)
        self.assertTrue(snap.require_virtualized)
        self.assertIsNone(snap.project)
        self.assertFalse(snap.private)
        self.assertTrue(snap.allow_internet)
        self.assertFalse(snap.pro_enable)

        self.factory.makeAccessPolicy(
            pillar=project, type=InformationType.PRIVATESECURITY
        )
        private = InformationType.PRIVATESECURITY.name
        browser = self.getViewBrowser(snap, user=admin)
        browser.getLink("Administer snap package").click()
        browser.getControl(name="field.project").value = "my-project"
        browser.getControl("Require virtualized builders").selected = False
        browser.getControl(name="field.information_type").value = private
        browser.getControl("Allow external network access").selected = False
        browser.getControl("Enable Ubuntu Pro").selected = True
        browser.getControl("Update snap package").click()
        login_admin()
        self.assertEqual(project, snap.project)
        self.assertFalse(snap.require_virtualized)
        self.assertTrue(snap.private)
        self.assertFalse(snap.allow_internet)
        self.assertTrue(snap.pro_enable)

    def test_admin_snap_private_without_project(self):
        # Cannot make snap private if it doesn't have a project associated.
        login_person(self.person)
        snap = self.factory.makeSnap(registrant=self.person)
        admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).admin]
        )
        private = InformationType.PRIVATESECURITY.name
        browser = self.getViewBrowser(snap, user=admin)
        browser.getLink("Administer snap package").click()
        browser.getControl(name="field.project").value = None
        browser.getControl(name="field.information_type").value = private
        browser.getControl("Update snap package").click()
        self.assertEqual(
            "Private snap recipes must be associated with a project.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_admin_snap_privacy_mismatch(self):
        # Cannot make snap public if it still contains private information.
        login_person(self.person)
        team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED,
            owner=self.person,
            visibility=PersonVisibility.PRIVATE,
        )
        project = self.factory.makeProduct(
            information_type=InformationType.PUBLIC,
            branch_sharing_policy=BranchSharingPolicy.PUBLIC_OR_PROPRIETARY,
        )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=team,
            project=project,
            information_type=InformationType.PRIVATESECURITY,
        )
        # Note that only LP admins or, in this case, commercial_admins
        # can reach this snap because it's owned by a private team.
        admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).admin]
        )
        public = InformationType.PUBLIC.name
        browser = self.getViewBrowser(snap, user=admin)
        browser.getLink("Administer snap package").click()
        browser.getControl(name="field.information_type").value = public
        browser.getControl("Update snap package").click()
        self.assertEqual(
            "A public snap cannot have a private owner.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_admin_snap_sets_date_last_modified(self):
        # Administering a snap package sets the date_last_modified property.
        login("admin@canonical.com")
        ppa_admin = self.factory.makePerson(
            member_of=[getUtility(ILaunchpadCelebrities).ppa_admin]
        )
        login_person(self.person)
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        snap = self.factory.makeSnap(
            registrant=self.person, date_created=date_created
        )
        login_person(ppa_admin)
        view = SnapAdminView(snap, LaunchpadTestRequest())
        view.initialize()
        view.request_action.success({"require_virtualized": False})
        self.assertSqlAttributeEqualsDate(snap, "date_last_modified", UTC_NOW)


class TestSnapEditView(BaseTestSnapView):
    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeUbuntuDistroSeries(
            version="13.10"
        )
        with admin_logged_in():
            self.snappyseries = self.factory.makeSnappySeries(
                usable_distro_series=[self.distroseries]
            )

    def test_edit_snap(self):
        old_series = self.factory.makeUbuntuDistroSeries()
        old_branch = self.factory.makeAnyBranch()
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=old_series,
            branch=old_branch,
        )
        self.factory.makeTeam(
            name="new-team", displayname="New Team", members=[self.person]
        )
        new_series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            new_snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[new_series]
            )
        [new_git_ref] = self.factory.makeGitRefs()
        new_git_ref_display_name = new_git_ref.display_name
        new_git_ref_identity = new_git_ref.repository.identity
        new_git_ref_path = new_git_ref.path
        archive = self.factory.makeArchive()

        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl("Owner").value = ["new-team"]
        browser.getControl(name="field.name").value = "new-name"
        browser.getControl(name="field.store_distro_series").value = [
            "ubuntu/%s/%s" % (new_series.name, new_snappy_series.name)
        ]
        browser.getControl("Git", index=0).click()
        browser.getControl(name="field.git_ref.repository").value = (
            new_git_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = new_git_ref_path
        browser.getControl("Build source tarball").selected = True
        browser.getControl(
            "Automatically build when branch changes"
        ).selected = True
        browser.getControl("PPA").click()
        browser.getControl(name="field.auto_build_archive.ppa").value = (
            archive.reference
        )
        browser.getControl("Pocket for automatic builds").value = ["SECURITY"]
        browser.getControl(
            name="field.auto_build_channels.snapcraft"
        ).value = "edge"
        browser.getControl("Update snap package").click()

        content = find_main_content(browser.contents)
        self.assertEqual("new-name", extract_text(content.h1))
        self.assertThat("New Team", MatchesPickerText(content, "edit-owner"))
        self.assertThat(
            "Distribution series:\n%s\nEdit snap package"
            % new_series.fullseriesname,
            MatchesTagText(content, "distro_series"),
        )
        self.assertThat(
            "Source:\n%s\nEdit snap package" % new_git_ref_display_name,
            MatchesTagText(content, "source"),
        )
        self.assertThat(
            "Build source tarball:\nYes\nEdit snap package",
            MatchesTagText(content, "build_source_tarball"),
        )
        self.assertThat(
            "Build schedule:\n(?)\nBuilt automatically\nEdit snap package\n",
            MatchesTagText(content, "auto_build"),
        )
        self.assertThat(
            "Source archive for automatic builds:\n%s\nEdit snap package\n"
            % archive.displayname,
            MatchesTagText(content, "auto_build_archive"),
        )
        self.assertThat(
            "Pocket for automatic builds:\nSecurity\nEdit snap package",
            MatchesTagText(content, "auto_build_pocket"),
        )
        self.assertThat(
            "Source snap channels for automatic builds:\nEdit snap package\n"
            "snapcraft\nedge",
            MatchesTagText(content, "auto_build_channels"),
        )
        self.assertThat(
            "Builds of this snap package are not automatically uploaded to "
            "the store.\nEdit snap package",
            MatchesTagText(content, "store_upload"),
        )

    def test_edit_snap_build_path(self):
        # build_path can be set and cleared via the edit form.
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=self.distroseries,
            branch=self.factory.makeAnyBranch(),
        )
        browser = self.getViewBrowser(
            snap, view_name="+edit", user=self.person
        )
        browser.getControl(name="field.build_path").value = "snap/dir"
        browser.getControl("Update snap package").click()

        content = find_main_content(browser.contents)
        self.assertThat(
            "Build path:\nsnap/dir\nEdit snap package",
            MatchesTagText(content, "build_path"),
        )
        with person_logged_in(self.person):
            self.assertEqual("snap/dir", snap.build_path)

        # Clear the build path.
        browser = self.getViewBrowser(
            snap, view_name="+edit", user=self.person
        )
        browser.getControl(name="field.build_path").value = ""
        browser.getControl("Update snap package").click()

        with person_logged_in(self.person):
            self.assertIsNone(snap.build_path)

    def test_edit_snap_built_for_older_store_series(self):
        distro_series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[distro_series],
                status=SeriesStatus.SUPPORTED,
            )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distro_series,
            store_series=snappy_series,
            branch=self.factory.makeAnyBranch(),
        )
        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        browser.getControl(name="field.store_distro_series").value = (
            "ubuntu/%s/%s" % (distro_series.name, snappy_series.name)
        )
        browser.getControl("Update snap package").click()

        self.assertEqual([], find_tags_by_class(browser.contents, "message"))
        login_person(self.person)
        self.assertThat(
            snap,
            MatchesStructure.byEquality(
                distro_series=distro_series, store_series=snappy_series
            ),
        )

    def test_edit_snap_built_for_distro_series_None(self):
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                status=SeriesStatus.CURRENT
            )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=None,
            store_series=snappy_series,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl(name="field.store_distro_series").value = (
            browser.getControl(name="field.store_distro_series")
            .options[0]
            .strip()
        )
        browser.getControl("Update snap package").click()
        self.assertEqual([], find_tags_by_class(browser.contents, "message"))
        login_person(self.person)
        self.assertThat(
            snap,
            MatchesStructure(
                distro_series=Is(None), store_series=Equals(snappy_series)
            ),
        )

    def test_edit_snap_built_for_snappy_series_None(self):
        distro_series = self.factory.makeUbuntuDistroSeries()

        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distro_series,
            store_series=None,
        )

        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        self.assertIn(
            "ubuntu/%s" % distro_series.name,
            browser.getControl(name="field.store_distro_series").options,
        )
        browser.getControl(name="field.store_distro_series").value = (
            "ubuntu/%s" % distro_series.name
        )
        browser.getControl("Update snap package").click()
        self.assertEqual([], find_tags_by_class(browser.contents, "message"))
        login_person(self.person)
        self.assertThat(
            snap,
            MatchesStructure(
                distro_series=Equals(distro_series), store_series=Is(None)
            ),
        )

    def test_edit_snap_built_for_distro_snappy_series_None(self):
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=None,
            store_series=None,
        )

        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        self.assertIn(
            "(unset)",
            browser.getControl(name="field.store_distro_series").options,
        )
        browser.getControl(name="field.store_distro_series").value = "(unset)"
        browser.getControl("Update snap package").click()
        self.assertEqual([], find_tags_by_class(browser.contents, "message"))

        login_person(self.person)
        self.assertThat(
            snap,
            MatchesStructure(distro_series=Is(None), store_series=Is(None)),
        )

    def test_edit_snap_sets_date_last_modified(self):
        # Editing a snap package sets the date_last_modified property.
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        snap = self.factory.makeSnap(
            registrant=self.person, date_created=date_created
        )
        with person_logged_in(self.person):
            view = SnapEditView(snap, LaunchpadTestRequest())
            view.initialize()
            view.request_action.success(
                {
                    "owner": snap.owner,
                    "name": "changed",
                    "distro_series": snap.distro_series,
                }
            )
        self.assertSqlAttributeEqualsDate(snap, "date_last_modified", UTC_NOW)

    def test_edit_snap_already_exists(self):
        snap = self.factory.makeSnap(
            registrant=self.person, owner=self.person, name="one"
        )
        self.factory.makeSnap(
            registrant=self.person, owner=self.person, name="two"
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl(name="field.name").value = "two"
        browser.getControl("Update snap package").click()
        self.assertEqual(
            "There is already a snap package owned by Test Person with this "
            "name.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_snap_project_and_info_type(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        initial_project = self.factory.makeProduct(
            name="initial-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PUBLIC,
            branch_sharing_policy=BranchSharingPolicy.PUBLIC_OR_PROPRIETARY,
        )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            project=initial_project,
            distroseries=series,
            store_series=snappy_series,
            information_type=InformationType.PUBLIC,
        )
        final_project = self.factory.makeProduct(
            name="final-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl(name="field.project").value = "final-project"
        browser.getControl(name="field.information_type").value = "PROPRIETARY"
        browser.getControl("Update snap package").click()
        login_admin()
        self.assertEqual(canonical_url(snap), browser.url)
        snap = IStore(Snap).find(Snap, Snap.name == snap.name).one()
        self.assertEqual(final_project, snap.project)
        self.assertEqual(InformationType.PROPRIETARY, snap.information_type)

    def test_edit_snap_private_without_project(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        private_project = self.factory.makeProduct(
            name="private-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        snap = self.factory.makeSnap(
            name="foo-snap",
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            store_series=snappy_series,
            information_type=InformationType.PROPRIETARY,
            project=private_project,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl(name="field.project").value = ""
        browser.getControl(name="field.information_type").value = "PROPRIETARY"
        browser.getControl("Update snap package").click()

        messages = find_tags_by_class(browser.contents, "message")
        self.assertEqual(2, len(messages))
        top_msg, field_msg = messages
        self.assertEqual("There is 1 error.", extract_text(top_msg))
        self.assertEqual(
            "Private snap recipes must be associated with a project.",
            extract_text(field_msg),
        )

    def test_edit_snap_private_information_type_matches_project(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        private_project = self.factory.makeProduct(
            name="private-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        snap = self.factory.makeSnap(
            name="foo-snap",
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            store_series=snappy_series,
            information_type=InformationType.PROPRIETARY,
            project=private_project,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()

        # Make sure we are only showing valid information type options:
        info_type_selector = browser.getControl(name="field.information_type")
        self.assertEqual(["PROPRIETARY"], info_type_selector.options)

    def test_edit_public_snap_private_owner(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            store_series=snappy_series,
        )
        private_team = self.factory.makeTeam(
            owner=self.person, visibility=PersonVisibility.PRIVATE
        )
        private_team_name = private_team.name
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl("Owner").value = [private_team_name]
        browser.getControl("Update snap package").click()
        self.assertEqual(
            "A public snap cannot have a private owner.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_public_snap_make_private_in_one_go(self):
        # Move a public snap to a private owner and mark it private in one go
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        private_project = self.factory.makeProduct(
            name="private-project",
            owner=self.person,
            registrant=self.person,
            information_type=InformationType.PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
        )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            store_series=snappy_series,
            project=private_project,
        )
        private_team = self.factory.makeTeam(
            owner=self.person, visibility=PersonVisibility.PRIVATE
        )
        private_team_name = private_team.name

        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl("Owner").value = [private_team_name]
        browser.getControl(name="field.information_type").value = "PROPRIETARY"
        browser.getControl("Update snap package").click()

        login_admin()
        self.assertEqual(InformationType.PROPRIETARY, snap.information_type)

    def test_edit_public_snap_private_branch(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            branch=self.factory.makeAnyBranch(),
            store_series=snappy_series,
        )
        private_branch = self.factory.makeAnyBranch(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        private_branch_name = private_branch.unique_name
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl("Bazaar branch").value = private_branch_name
        browser.getControl("Update snap package").click()
        self.assertEqual(
            "A public snap cannot have a private branch.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_public_snap_private_git_ref(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            git_ref=self.factory.makeGitRefs()[0],
            store_series=snappy_series,
        )
        login_person(self.person)
        [private_ref] = self.factory.makeGitRefs(
            owner=self.person, information_type=InformationType.PRIVATESECURITY
        )
        private_ref_identity = private_ref.repository.identity
        private_ref_path = private_ref.path
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl(name="field.git_ref.repository").value = (
            private_ref_identity
        )
        browser.getControl(name="field.git_ref.path").value = private_ref_path
        browser.getControl("Update snap package").click()
        self.assertEqual(
            "A public snap cannot have a private repository.",
            extract_text(find_tags_by_class(browser.contents, "message")[1]),
        )

    def test_edit_snap_git_url(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        old_ref = self.factory.makeGitRefRemote()
        new_ref = self.factory.makeGitRefRemote()
        new_repository_url = new_ref.repository_url
        new_path = new_ref.path
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            git_ref=old_ref,
            store_series=snappy_series,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl(name="field.git_ref.repository").value = (
            new_repository_url
        )
        browser.getControl(name="field.git_ref.path").value = new_path
        browser.getControl("Update snap package").click()
        login_person(self.person)
        content = find_main_content(browser.contents)
        self.assertThat(
            "Source:\n%s\nEdit snap package" % new_ref.display_name,
            MatchesTagText(content, "source"),
        )

    def test_edit_use_fetch_service(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            branch=self.factory.makeAnyBranch(),
            store_series=snappy_series,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        self.assertTrue("Use fetch service" in browser.contents)
        with person_logged_in(self.person):
            self.assertFalse(snap.use_fetch_service)
        browser.getControl("Use fetch service").selected = True
        browser.getControl("Update snap package").click()
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        with person_logged_in(self.person):
            self.assertTrue(snap.use_fetch_service)

    def test_edit_fetch_service_policy(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            branch=self.factory.makeAnyBranch(),
            store_series=snappy_series,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        browser.getControl("Use fetch service").selected = True
        with person_logged_in(self.person):
            self.assertEqual(
                snap.fetch_service_policy, FetchServicePolicy.STRICT
            )
        browser.getControl("Fetch service policy").value = ["PERMISSIVE"]
        browser.getControl("Update snap package").click()

        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        with person_logged_in(self.person):
            self.assertEqual(
                snap.fetch_service_policy, FetchServicePolicy.PERMISSIVE
            )

    def test_fetch_service_policy_when_use_fetch_service_is_false(self):
        series = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            snappy_series = self.factory.makeSnappySeries(
                usable_distro_series=[series]
            )
        login_person(self.person)
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=series,
            branch=self.factory.makeAnyBranch(),
            store_series=snappy_series,
        )
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Edit snap package").click()
        with person_logged_in(self.person):
            self.assertFalse(snap.use_fetch_service)
        browser.getControl("Update snap package").click()
        with person_logged_in(self.person):
            self.assertEqual(
                snap.fetch_service_policy, FetchServicePolicy.STRICT
            )

    def setUpSeries(self):
        """Set up {distro,snappy}series with some available processors."""
        distroseries = self.factory.makeUbuntuDistroSeries()
        processor_names = ["386", "amd64", "hppa"]
        for name in processor_names:
            processor = getUtility(IProcessorSet).getByName(name)
            self.factory.makeDistroArchSeries(
                distroseries=distroseries,
                architecturetag=name,
                processor=processor,
            )
        with admin_logged_in():
            snappyseries = self.factory.makeSnappySeries(
                usable_distro_series=[distroseries]
            )
        return distroseries, snappyseries

    def assertSnapProcessors(self, snap, names):
        self.assertContentEqual(
            names, [processor.name for processor in snap.processors]
        )

    def assertProcessorControls(self, processors_control, enabled, disabled):
        matchers = [
            MatchesStructure.byEquality(optionValue=name, disabled=False)
            for name in enabled
        ]
        matchers.extend(
            [
                MatchesStructure.byEquality(optionValue=name, disabled=True)
                for name in disabled
            ]
        )
        self.assertThat(processors_control.controls, MatchesSetwise(*matchers))

    def test_display_processors(self):
        distroseries, snappyseries = self.setUpSeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distroseries,
            store_series=snappyseries,
        )
        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(
            ["Intel 386 (386)", "AMD 64bit (amd64)", "HPPA Processor (hppa)"],
            [extract_text(option) for option in processors.displayOptions],
        )
        self.assertContentEqual(["386", "amd64", "hppa"], processors.options)

    def test_edit_processors(self):
        distroseries, snappyseries = self.setUpSeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distroseries,
            store_series=snappyseries,
        )
        self.assertSnapProcessors(snap, ["386", "amd64", "hppa"])
        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)
        processors.value = ["386", "amd64"]
        browser.getControl("Update snap package").click()
        login_person(self.person)
        self.assertSnapProcessors(snap, ["386", "amd64"])

    def test_edit_with_invisible_processor(self):
        # It's possible for existing snap packages to have an enabled
        # processor that's no longer usable with the current distroseries,
        # which will mean it's hidden from the UI, but the non-admin
        # Snap.setProcessors isn't allowed to disable it.  Editing the
        # processor list of such a snap package leaves the invisible
        # processor intact.
        proc_386 = getUtility(IProcessorSet).getByName("386")
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        proc_armel = self.factory.makeProcessor(
            name="armel", restricted=True, build_by_default=False
        )
        distroseries, snappyseries = self.setUpSeries()
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distroseries,
            store_series=snappyseries,
        )
        snap.setProcessors([proc_386, proc_amd64, proc_armel])
        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64"], processors.value)
        processors.value = ["amd64"]
        browser.getControl("Update snap package").click()
        login_person(self.person)
        self.assertSnapProcessors(snap, ["amd64", "armel"])

    def test_edit_processors_restricted(self):
        # A restricted processor is shown with a disabled (greyed out)
        # checkbox in the UI, and the processor cannot be enabled.
        distroseries, snappyseries = self.setUpSeries()
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="armhf",
            processor=proc_armhf,
        )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distroseries,
            store_series=snappyseries,
        )
        self.assertSnapProcessors(snap, ["386", "amd64", "hppa"])
        browser = self.getViewBrowser(snap, view_name="+edit", user=snap.owner)
        processors = browser.getControl(name="field.processors")
        self.assertContentEqual(["386", "amd64", "hppa"], processors.value)
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )
        # Even if the user works around the disabled checkbox and forcibly
        # enables it, they can't enable the restricted processor.
        for control in processors.controls:
            if control.optionValue == "armhf":
                del control._control.attrs["disabled"]
        processors.value = ["386", "amd64", "armhf"]
        self.assertRaises(
            CannotModifySnapProcessor,
            browser.getControl("Update snap package").click,
        )

    def test_edit_processors_restricted_already_enabled(self):
        # A restricted processor that is already enabled is shown with a
        # disabled (greyed out) checkbox in the UI.  This causes form
        # submission to omit it, but the validation code fixes that up
        # behind the scenes so that we don't get CannotModifySnapProcessor.
        proc_386 = getUtility(IProcessorSet).getByName("386")
        proc_amd64 = getUtility(IProcessorSet).getByName("amd64")
        proc_armhf = self.factory.makeProcessor(
            name="armhf", restricted=True, build_by_default=False
        )
        distroseries, snappyseries = self.setUpSeries()
        self.factory.makeDistroArchSeries(
            distroseries=distroseries,
            architecturetag="armhf",
            processor=proc_armhf,
        )
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=distroseries,
            store_series=snappyseries,
        )
        snap.setProcessors([proc_386, proc_amd64, proc_armhf])
        self.assertSnapProcessors(snap, ["386", "amd64", "armhf"])
        browser = self.getUserBrowser(
            canonical_url(snap) + "/+edit", user=snap.owner
        )
        processors = browser.getControl(name="field.processors")
        # armhf is checked but disabled.
        self.assertContentEqual(["386", "amd64", "armhf"], processors.value)
        self.assertProcessorControls(
            processors, ["386", "amd64", "hppa"], ["armhf"]
        )
        processors.value = ["386"]
        browser.getControl("Update snap package").click()
        login_person(self.person)
        self.assertSnapProcessors(snap, ["386", "armhf"])

    def assertNeedStoreReauth(self, expected, initial_kwargs, data):
        initial_kwargs.setdefault("store_upload", True)
        initial_kwargs.setdefault("store_series", self.snappyseries)
        initial_kwargs.setdefault("store_name", "one")
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=self.distroseries,
            **initial_kwargs,
        )
        view = create_initialized_view(snap, "+edit", principal=self.person)
        data.setdefault("store_upload", snap.store_upload)
        data.setdefault("store_distro_series", snap.store_distro_series)
        data.setdefault("store_name", snap.store_name)
        self.assertEqual(expected, view._needStoreReauth(data))

    def test__needStoreReauth_no_change(self):
        # If the user didn't change any store settings, no reauthorization
        # is needed.
        self.assertNeedStoreReauth(False, {}, {})

    def test__needStoreReauth_different_series(self):
        # Changing the store series requires reauthorization.
        with admin_logged_in():
            new_snappyseries = self.factory.makeSnappySeries(
                usable_distro_series=[self.distroseries]
            )
        sds = getUtility(ISnappyDistroSeriesSet).getByBothSeries(
            new_snappyseries, self.distroseries
        )
        self.assertNeedStoreReauth(True, {}, {"store_distro_series": sds})

    def test__needStoreReauth_different_name(self):
        # Changing the store name requires reauthorization.
        self.assertNeedStoreReauth(True, {}, {"store_name": "two"})

    def test__needStoreReauth_enable_upload(self):
        # Enabling store upload requires reauthorization.  (This can happen
        # on its own if both store_series and store_name were set to begin
        # with, which is especially plausible for Git-based snap packages,
        # or if this option is disabled and then re-enabled.  In the latter
        # case, we can't tell if store_series or store_name were also
        # changed in between, so reauthorizing is the conservative course.)
        self.assertNeedStoreReauth(
            True, {"store_upload": False}, {"store_upload": True}
        )

    @responses.activate
    def test_edit_store_upload(self):
        # Changing store upload settings on a snap sets all the appropriate
        # fields and redirects to SSO for reauthorization.
        snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=self.distroseries,
            store_upload=True,
            store_series=self.snappyseries,
            store_name="one",
            store_channels=["track/edge"],
        )
        view_url = canonical_url(snap, view_name="+edit")
        browser = self.getNonRedirectingBrowser(url=view_url, user=self.person)
        browser.getControl("Registered store package name").value = "two"
        self.assertEqual(
            "track",
            browser.getControl(name="field.store_channels.track_0").value,
        )
        self.assertEqual(
            ["edge"],
            browser.getControl(name="field.store_channels.risk_0").value,
        )
        browser.getControl(name="field.store_channels.track_0").value = ""
        browser.getControl(name="field.store_channels.risk_0").value = [
            "stable"
        ]
        root_macaroon = Macaroon()
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc, "", "dummy"
        )
        root_macaroon_raw = root_macaroon.serialize()
        self.pushConfig("snappy", store_url="http://sca.example/")
        responses.add(
            "POST",
            "http://sca.example/dev/api/acl/",
            json={"macaroon": root_macaroon_raw},
        )
        browser.getControl("Update snap package").click()
        login_person(self.person)
        self.assertThat(
            snap,
            MatchesStructure.byEquality(
                store_name="two",
                store_secrets={"root": root_macaroon_raw},
                store_channels=["stable"],
            ),
        )
        [call] = responses.calls
        self.assertThat(
            call.request,
            MatchesStructure.byEquality(
                url="http://sca.example/dev/api/acl/", method="POST"
            ),
        )
        expected_body = {
            "packages": [{"name": "two", "series": self.snappyseries.name}],
            "permissions": ["package_upload"],
        }
        self.assertEqual(expected_body, json.loads(call.request.body))
        self.assertEqual(303, browser.responseStatusCode)
        parsed_location = urlsplit(browser.headers["Location"])
        self.assertEqual(
            urlsplit(canonical_url(snap) + "/+authorize/+login")[:3],
            parsed_location[:3],
        )
        expected_args = {
            "discharge_macaroon_action": ["field.actions.complete"],
            "discharge_macaroon_field": ["field.discharge_macaroon"],
            "macaroon_caveat_id": ["dummy"],
        }
        self.assertEqual(expected_args, parse_qs(parsed_location[3]))


class TestSnapAuthorizeView(BaseTestSnapView):
    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeUbuntuDistroSeries()
        with admin_logged_in():
            self.snappyseries = self.factory.makeSnappySeries(
                usable_distro_series=[self.distroseries]
            )
        self.snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=self.distroseries,
            store_upload=True,
            store_series=self.snappyseries,
            store_name=self.factory.getUniqueUnicode(),
        )

    def test_unauthorized(self):
        # A user without edit access cannot authorize snap package uploads.
        other_person = self.factory.makePerson()
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            canonical_url(self.snap) + "/+authorize",
            user=other_person,
        )

    @responses.activate
    def test_begin_authorization(self):
        # With no special form actions, we return a form inviting the user
        # to begin authorization.  This allows (re-)authorizing uploads of
        # an existing snap package without having to edit it.
        snap_url = canonical_url(self.snap)
        owner = self.snap.owner
        root_macaroon = Macaroon()
        root_macaroon.add_third_party_caveat(
            urlsplit(config.launchpad.openid_provider_root).netloc, "", "dummy"
        )
        root_macaroon_raw = root_macaroon.serialize()
        self.pushConfig("snappy", store_url="http://sca.example/")
        responses.add(
            "POST",
            "http://sca.example/dev/api/acl/",
            json={"macaroon": root_macaroon_raw},
        )
        browser = self.getNonRedirectingBrowser(
            url=snap_url + "/+authorize", user=self.snap.owner
        )
        browser.getControl("Begin authorization").click()
        [call] = responses.calls
        self.assertThat(
            call.request,
            MatchesStructure.byEquality(
                url="http://sca.example/dev/api/acl/", method="POST"
            ),
        )
        with person_logged_in(owner):
            expected_body = {
                "packages": [
                    {
                        "name": self.snap.store_name,
                        "series": self.snap.store_series.name,
                    }
                ],
                "permissions": ["package_upload"],
            }
            self.assertEqual(expected_body, json.loads(call.request.body))
            self.assertEqual(
                {"root": root_macaroon_raw}, self.snap.store_secrets
            )
        self.assertEqual(303, browser.responseStatusCode)
        self.assertEqual(
            snap_url + "/+authorize/+login?macaroon_caveat_id=dummy&"
            "discharge_macaroon_action=field.actions.complete&"
            "discharge_macaroon_field=field.discharge_macaroon",
            browser.headers["Location"],
        )

    @responses.activate
    def test_begin_authorization__snap_not_registered(self):
        snap_url = canonical_url(self.snap)
        self.pushConfig("snappy", store_url="http://sca.example/")
        responses.add("POST", "http://sca.example/dev/api/acl/", status=404)
        browser = self.getUserBrowser(
            url=snap_url + "/+authorize", user=self.snap.owner
        )
        browser.getControl("Begin authorization").click()
        self.assertEqual(snap_url, browser.url)
        messages = find_tags_by_class(
            browser.contents, "informational message"
        )
        self.assertEqual(1, len(messages))
        self.assertStartsWith(
            extract_text(messages[0]),
            "The requested snap name '{}' is not registered in the "
            "snap store".format(removeSecurityProxy(self.snap).store_name),
        )

    def test_complete_authorization_missing_discharge_macaroon(self):
        # If the form does not include a discharge macaroon, the "complete"
        # action fails.
        with person_logged_in(self.snap.owner):
            self.snap.store_secrets = {"root": Macaroon().serialize()}
            transaction.commit()
            form = {"field.actions.complete": "1"}
            view = create_initialized_view(
                self.snap,
                "+authorize",
                form=form,
                method="POST",
                principal=self.snap.owner,
            )
            html = view()
            self.assertEqual(
                "Uploads of %s to the store were not authorized."
                % self.snap.name,
                get_feedback_messages(html)[1],
            )
            self.assertNotIn("discharge", self.snap.store_secrets)

    def test_complete_authorization(self):
        # If the form includes a discharge macaroon, the "complete" action
        # succeeds and records the new secrets.
        root_macaroon = Macaroon()
        discharge_macaroon = Macaroon()
        with person_logged_in(self.snap.owner):
            self.snap.store_secrets = {"root": root_macaroon.serialize()}
            transaction.commit()
            form = {
                "field.actions.complete": "1",
                "field.discharge_macaroon": discharge_macaroon.serialize(),
            }
            view = create_initialized_view(
                self.snap,
                "+authorize",
                form=form,
                method="POST",
                principal=self.snap.owner,
            )
            self.assertEqual("", view())
            self.assertEqual(302, view.request.response.getStatus())
            self.assertEqual(
                canonical_url(self.snap),
                view.request.response.getHeader("Location"),
            )
            self.assertEqual(
                "Uploads of %s to the store are now authorized."
                % self.snap.name,
                view.request.response.notifications[0].message,
            )
            self.assertEqual(
                {
                    "root": root_macaroon.serialize(),
                    "discharge": discharge_macaroon.serialize(),
                },
                self.snap.store_secrets,
            )


class TestSnapDeleteView(BaseTestSnapView):
    def test_unauthorized(self):
        # A user without edit access cannot delete a snap package.
        snap = self.factory.makeSnap(registrant=self.person, owner=self.person)
        snap_url = canonical_url(snap)
        other_person = self.factory.makePerson()
        browser = self.getViewBrowser(snap, user=other_person)
        self.assertRaises(
            LinkNotFoundError, browser.getLink, "Delete snap package"
        )
        self.assertRaises(
            Unauthorized,
            self.getUserBrowser,
            snap_url + "/+delete",
            user=other_person,
        )

    def test_delete_snap_without_builds(self):
        # A snap package without builds can be deleted.
        snap = self.factory.makeSnap(registrant=self.person, owner=self.person)
        snap_url = canonical_url(snap)
        owner_url = canonical_url(self.person)
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Delete snap package").click()
        browser.getControl("Delete snap package").click()
        self.assertEqual(owner_url + "/+snaps", browser.url)
        self.assertRaises(NotFound, browser.open, snap_url)

    def test_delete_snap_with_builds(self):
        # A snap package with builds can be deleted.
        snap = self.factory.makeSnap(registrant=self.person, owner=self.person)
        build = self.factory.makeSnapBuild(snap=snap)
        self.factory.makeSnapFile(snapbuild=build)
        snap_url = canonical_url(snap)
        owner_url = canonical_url(self.person)
        browser = self.getViewBrowser(snap, user=self.person)
        browser.getLink("Delete snap package").click()
        browser.getControl("Delete snap package").click()
        self.assertEqual(owner_url + "/+snaps", browser.url)
        self.assertRaises(NotFound, browser.open, snap_url)


class TestSnapView(BaseTestSnapView):
    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="shiny", displayname="Shiny"
        )
        processor = getUtility(IProcessorSet).getByName("386")
        self.distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="i386",
            processor=processor,
        )
        self.factory.makeBuilder(virtualized=True)

    def makeSnap(self, **kwargs):
        if "distroseries" not in kwargs:
            kwargs["distroseries"] = self.distroseries
        if kwargs.get("branch") is None and kwargs.get("git_ref") is None:
            kwargs["branch"] = self.factory.makeAnyBranch()
        return self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            name="snap-name",
            **kwargs,
        )

    def makeBuild(self, snap=None, archive=None, date_created=None, **kwargs):
        if snap is None:
            snap = self.makeSnap()
        if archive is None:
            archive = self.ubuntu.main_archive
        if date_created is None:
            date_created = datetime.now(timezone.utc) - timedelta(hours=1)
        return self.factory.makeSnapBuild(
            requester=self.person,
            snap=snap,
            archive=archive,
            distroarchseries=self.distroarchseries,
            date_created=date_created,
            **kwargs,
        )

    def test_breadcrumb(self):
        snap = self.makeSnap()
        view = create_view(snap, "+index")
        # To test the breadcrumbs we need a correct traversal stack.
        view.request.traversed_objects = [self.person, snap, view]
        view.initialize()
        breadcrumbs_tag = soupmatchers.Tag(
            "breadcrumbs", "ol", attrs={"class": "breadcrumbs"}
        )
        self.assertThat(
            view(),
            soupmatchers.HTMLContains(
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "snap collection breadcrumb",
                        "a",
                        text="Snap packages",
                        attrs={
                            "href": re.compile(r"/~test-person/\+snaps$"),
                        },
                    ),
                ),
                soupmatchers.Within(
                    breadcrumbs_tag,
                    soupmatchers.Tag(
                        "snap breadcrumb",
                        "li",
                        text=re.compile(r"\ssnap-name\s"),
                    ),
                ),
            ),
        )

    def test_snap_with_project_pillar_url(self):
        project = self.factory.makeProduct()
        snap = self.factory.makeSnap(project=project)
        browser = self.getViewBrowser(snap)
        with admin_logged_in():
            expected_url = "http://launchpad.test/~{}/{}/+snap/{}".format(
                snap.owner.name, project.name, snap.name
            )
        self.assertEqual(expected_url, browser.url)

    def test_index_bzr(self):
        branch = self.factory.makePersonalBranch(
            owner=self.person, name="snap-branch"
        )
        snap = self.makeSnap(branch=branch)
        build = self.makeBuild(
            snap=snap,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Snap packages snap-name
            .*
            Snap package information
            Owner: Test Person
            Distribution series: Ubuntu Shiny
            Source: lp://dev/~test-person/\+junk/snap-branch
            Build source tarball: No
            Build schedule: \(\?\)
            Built on request
            Source archive for automatic builds:
            Pocket for automatic builds:
            Builds of this snap package are not automatically uploaded to
            the store.
            Latest builds
            Status When complete Architecture Archive
            Successfully built 30 minutes ago i386
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(build.snap),
        )

    def test_index_git(self):
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="snap-repository",
            paths=["refs/heads/master"],
        )
        snap = self.makeSnap(git_ref=ref)
        build = self.makeBuild(
            snap=snap,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Snap packages snap-name
            .*
            Snap package information
            Owner: Test Person
            Distribution series: Ubuntu Shiny
            Source: ~test-person/\+git/snap-repository:master
            Build source tarball: No
            Build schedule: \(\?\)
            Built on request
            Source archive for automatic builds:
            Pocket for automatic builds:
            Builds of this snap package are not automatically uploaded to
            the store.
            Latest builds
            Status When complete Architecture Archive
            Successfully built 30 minutes ago i386
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(build.snap),
        )

    def test_index_for_subscriber_without_git_repo_access(self):
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="snap-repository",
            paths=["refs/heads/master"],
            information_type=InformationType.PRIVATESECURITY,
        )
        with person_logged_in(self.person):
            snap = self.makeSnap(git_ref=ref, private=True)
        with admin_logged_in():
            self.makeBuild(
                snap=snap,
                status=BuildStatus.FULLYBUILT,
                duration=timedelta(minutes=30),
            )

        subscriber = self.factory.makePerson()
        with person_logged_in(self.person):
            snap.subscribe(subscriber, self.person)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Snap packages snap-name
            .*
            Snap package information
            Owner: Test Person
            Distribution series: Ubuntu Shiny
            Source: &lt;redacted&gt;
            Build source tarball: No
            Build schedule: \(\?\)
            Built on request
            Source archive for automatic builds:
            Pocket for automatic builds:
            Builds of this snap package are not automatically uploaded to
            the store.
            Latest builds
            Status When complete Architecture Archive
            Successfully built 30 minutes ago i386
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(snap, user=subscriber),
        )

    def test_index_for_subscriber_without_archive_access(self):
        [ref] = self.factory.makeGitRefs(
            owner=self.person,
            target=self.person,
            name="snap-repository",
            paths=["refs/heads/master"],
            information_type=InformationType.PRIVATESECURITY,
        )
        with person_logged_in(self.person):
            snap = self.makeSnap(git_ref=ref, private=True)
        with admin_logged_in():
            archive = self.factory.makeArchive(private=True)
            self.makeBuild(
                snap=snap,
                status=BuildStatus.FULLYBUILT,
                archive=archive,
                duration=timedelta(minutes=30),
            )

        subscriber = self.factory.makePerson()
        with person_logged_in(self.person):
            snap.subscribe(subscriber, self.person)
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Snap packages snap-name
            .*
            Snap package information
            Owner: Test Person
            Distribution series: Ubuntu Shiny
            Source: &lt;redacted&gt;
            Build source tarball: No
            Build schedule: \(\?\)
            Built on request
            Source archive for automatic builds:
            Pocket for automatic builds:
            Builds of this snap package are not automatically uploaded to
            the store.
            Latest builds
            Status When complete Architecture Archive
            This snap package has not been built yet.
            """,
            self.getMainText(snap, user=subscriber),
        )

    def test_index_git_url(self):
        ref = self.factory.makeGitRefRemote(
            repository_url="https://git.example.org/foo",
            path="refs/heads/master",
        )
        snap = self.makeSnap(git_ref=ref)
        build = self.makeBuild(
            snap=snap,
            status=BuildStatus.FULLYBUILT,
            duration=timedelta(minutes=30),
        )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Snap packages snap-name
            .*
            Snap package information
            Owner: Test Person
            Distribution series: Ubuntu Shiny
            Source: https://git.example.org/foo master
            Build source tarball: No
            Build schedule: \(\?\)
            Built on request
            Source archive for automatic builds:
            Pocket for automatic builds:
            Builds of this snap package are not automatically uploaded to
            the store.
            Latest builds
            Status When complete Architecture Archive
            Successfully built 30 minutes ago i386
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(build.snap),
        )

    def test_index_no_distro_series(self):
        # If the snap is configured to infer an appropriate distro series
        # from snapcraft.yaml, then the index page does not show a distro
        # series.
        snap = self.makeSnap(distroseries=None)
        text = self.getMainText(snap)
        self.assertIn("Snap package information", text)
        self.assertNotIn("Distribution series:", text)

    def test_index_build_path(self):
        # The build path is shown when set.
        snap = self.makeSnap(build_path="snap/dir")
        text = self.getMainText(snap)
        self.assertIn("Build path:", text)
        self.assertIn("snap/dir", text)

    def test_index_no_build_path(self):
        # No build path line is shown when not set.
        snap = self.makeSnap()
        text = self.getMainText(snap)
        self.assertNotIn("Build path:", text)

    def test_index_success_with_buildlog(self):
        # The build log is shown if it is there.
        build = self.makeBuild(
            status=BuildStatus.FULLYBUILT, duration=timedelta(minutes=30)
        )
        build.setLog(self.factory.makeLibraryFileAlias())
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Status When complete Architecture Archive
            Successfully built 30 minutes ago buildlog \(.*\) i386
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(build.snap),
        )

    def test_index_hides_builds_into_private_archive(self):
        # The index page hides builds into archives the user can't view.
        archive = self.factory.makeArchive(private=True)
        with person_logged_in(archive.owner):
            snap = self.makeBuild(archive=archive).snap
        self.assertIn(
            "This snap package has not been built yet.", self.getMainText(snap)
        )

    def test_index_no_builds(self):
        # A message is shown when there are no builds.
        snap = self.factory.makeSnap()
        self.assertIn(
            "This snap package has not been built yet.", self.getMainText(snap)
        )

    def test_index_pending_build(self):
        # A pending build is listed as such.
        build = self.makeBuild()
        build.queueBuild()
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Status When complete Architecture Archive
            Needs building in .* \(estimated\) i386
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(build.snap),
        )

    def test_index_pending_build_request(self):
        # A pending build request is listed as such.
        snap = self.makeSnap()
        with person_logged_in(snap.owner):
            snap.requestBuilds(
                snap.owner,
                snap.distro_series.main_archive,
                PackagePublishingPocket.UPDATES,
            )
        self.assertTextMatchesExpressionIgnoreWhitespace(
            """\
            Latest builds
            Status When complete Architecture Archive
            Pending build request
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(snap),
        )

    def test_index_failed_build_request(self):
        # A failed build request is listed as such, with its error message.
        snap = self.makeSnap()
        with person_logged_in(snap.owner):
            request = snap.requestBuilds(
                snap.owner,
                snap.distro_series.main_archive,
                PackagePublishingPocket.UPDATES,
            )
        job = removeSecurityProxy(removeSecurityProxy(request)._job)
        job.job._status = JobStatus.FAILED
        job.job.date_finished = datetime.now(timezone.utc) - timedelta(hours=1)
        job.error_message = "Boom"
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""\
            Latest builds
            Status When complete Architecture Archive
            Failed build request 1 hour ago \(Boom\)
            Primary Archive for Ubuntu Linux
            """,
            self.getMainText(snap),
        )

    def test_index_store_upload(self):
        # If the snap package is to be automatically uploaded to the store,
        # the index page shows details of this.
        with admin_logged_in():
            snappyseries = self.factory.makeSnappySeries(
                usable_distro_series=[self.distroseries]
            )
        snap = self.makeSnap(
            store_upload=True,
            store_series=snappyseries,
            store_name=self.getUniqueString("store-name"),
        )
        view = create_initialized_view(snap, "+index")
        store_upload_tag = soupmatchers.Tag(
            "store upload", "div", attrs={"id": "store_upload"}
        )
        self.assertThat(
            view(),
            soupmatchers.HTMLContains(
                soupmatchers.Tag(
                    "distribution series", "dl", attrs={"id": "distro_series"}
                ),
                soupmatchers.Within(
                    store_upload_tag,
                    soupmatchers.Tag(
                        "store series name", "span", text=snappyseries.title
                    ),
                ),
                soupmatchers.Within(
                    store_upload_tag,
                    soupmatchers.Tag(
                        "store name", "span", text=snap.store_name
                    ),
                ),
            ),
        )

    def test_index_store_upload_no_distro_series(self):
        # If the snap package is to be automatically uploaded to the store
        # and is configured to infer an appropriate distro series from
        # snapcraft.yaml, the index page shows details of this.
        with admin_logged_in():
            snappyseries = self.factory.makeSnappySeries(
                usable_distro_series=[self.distroseries],
                can_infer_distro_series=True,
            )
        snap = self.makeSnap(
            distroseries=None,
            store_upload=True,
            store_series=snappyseries,
            store_name=self.getUniqueString("store-name"),
        )
        view = create_initialized_view(snap, "+index")
        store_upload_tag = soupmatchers.Tag(
            "store upload", "div", attrs={"id": "store_upload"}
        )
        self.assertThat(
            view(),
            soupmatchers.HTMLContains(
                Not(
                    soupmatchers.Tag(
                        "distribution series",
                        "dl",
                        attrs={"id": "distro_series"},
                    )
                ),
                soupmatchers.Within(
                    store_upload_tag,
                    soupmatchers.Tag(
                        "store series name", "span", text=snappyseries.title
                    ),
                ),
                soupmatchers.Within(
                    store_upload_tag,
                    soupmatchers.Tag(
                        "store name", "span", text=snap.store_name
                    ),
                ),
            ),
        )

    def setStatus(self, build, status):
        build.updateStatus(
            BuildStatus.BUILDING, date_started=build.date_created
        )
        build.updateStatus(
            status, date_finished=build.date_started + timedelta(minutes=30)
        )

    def test_builds_and_requests(self):
        # SnapView.builds_and_requests produces reasonable results.
        snap = self.makeSnap()
        # Create oldest builds first so that they sort properly by id.
        date_gen = time_counter(
            datetime(2000, 1, 1, tzinfo=timezone.utc), timedelta(days=1)
        )
        builds = [
            self.makeBuild(snap=snap, date_created=next(date_gen))
            for i in range(11)
        ]
        view = SnapView(snap, None)
        self.assertEqual(list(reversed(builds)), view.builds_and_requests)
        self.setStatus(builds[10], BuildStatus.FULLYBUILT)
        self.setStatus(builds[9], BuildStatus.FAILEDTOBUILD)
        del get_property_cache(view).builds_and_requests
        # When there are >= 9 pending builds, only the most recent of any
        # completed builds is returned.
        self.assertEqual(
            list(reversed(builds[:9])) + [builds[10]], view.builds_and_requests
        )
        for build in builds[:9]:
            self.setStatus(build, BuildStatus.FULLYBUILT)
        del get_property_cache(view).builds_and_requests
        self.assertEqual(list(reversed(builds[1:])), view.builds_and_requests)

    def test_builds_and_requests_shows_build_requests(self):
        # SnapView.builds_and_requests interleaves build requests with
        # builds.
        snap = self.makeSnap()
        date_gen = time_counter(
            datetime(2000, 1, 1, tzinfo=timezone.utc), timedelta(days=1)
        )
        builds = [
            self.makeBuild(snap=snap, date_created=next(date_gen))
            for i in range(3)
        ]
        self.setStatus(builds[2], BuildStatus.FULLYBUILT)
        with person_logged_in(snap.owner):
            request = snap.requestBuilds(
                snap.owner,
                snap.distro_series.main_archive,
                PackagePublishingPocket.UPDATES,
            )
        job = removeSecurityProxy(removeSecurityProxy(request)._job)
        job.job.date_created = next(date_gen)
        view = SnapView(snap, None)
        # The pending build request is interleaved in date order with
        # pending builds, and these are followed by completed builds.
        self.assertThat(
            view.builds_and_requests,
            MatchesListwise(
                [
                    MatchesStructure.byEquality(id=request.id),
                    Equals(builds[1]),
                    Equals(builds[0]),
                    Equals(builds[2]),
                ]
            ),
        )
        transaction.commit()
        builds.append(self.makeBuild(snap=snap))
        del get_property_cache(view).builds_and_requests
        self.assertThat(
            view.builds_and_requests,
            MatchesListwise(
                [
                    Equals(builds[3]),
                    MatchesStructure.byEquality(id=request.id),
                    Equals(builds[1]),
                    Equals(builds[0]),
                    Equals(builds[2]),
                ]
            ),
        )
        # If we pretend that the job failed, it is still listed, but after
        # any pending builds.
        job.job._status = JobStatus.FAILED
        job.job.date_finished = job.date_created + timedelta(minutes=30)
        del get_property_cache(view).builds_and_requests
        self.assertThat(
            view.builds_and_requests,
            MatchesListwise(
                [
                    Equals(builds[3]),
                    Equals(builds[1]),
                    Equals(builds[0]),
                    MatchesStructure.byEquality(id=request.id),
                    Equals(builds[2]),
                ]
            ),
        )

    def test_store_channels_empty(self):
        snap = self.factory.makeSnap()
        view = create_initialized_view(snap, "+index")
        self.assertEqual("", view.store_channels)

    def test_store_channels_display(self):
        snap = self.factory.makeSnap(
            store_channels=["track/stable/fix-123", "track/edge/fix-123"]
        )
        view = create_initialized_view(snap, "+index")
        self.assertEqual(
            "track/stable/fix-123, track/edge/fix-123", view.store_channels
        )

    def test_authorize_navigation_no_store_secrets(self):
        # A snap with no store secrets has an "Authorize store uploads"
        # navigation link.
        owner = self.factory.makePerson()
        snap = self.factory.makeSnap(registrant=owner, owner=owner)
        authorize_url = canonical_url(snap, view_name="+authorize")
        browser = self.getViewBrowser(snap, user=owner)
        authorize_link = browser.getLink("Authorize store uploads")
        self.assertEqual(authorize_url, authorize_link.url)

    def test_authorize_navigation_store_secrets(self):
        # A snap with store secrets has an "Reauthorize store uploads"
        # navigation link.
        owner = self.factory.makePerson()
        snap = self.factory.makeSnap(
            registrant=owner,
            owner=owner,
            store_secrets={"root": Macaroon().serialize()},
        )
        authorize_url = canonical_url(snap, view_name="+authorize")
        browser = self.getViewBrowser(snap, user=owner)
        authorize_link = browser.getLink("Reauthorize store uploads")
        self.assertEqual(authorize_url, authorize_link.url)

    def test_snap_anonymous_view(self):
        registrant = self.factory.makePerson(name="snap-registrant")
        team_owner = self.factory.makeTeam(
            name="snap-creator", members=[registrant]
        )
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        snap = self.factory.makeSnap(
            name="core20",
            registrant=registrant,
            owner=team_owner,
            date_created=date_created,
            auto_build_channels={"snapcraft": "stable/launchpad-buildd"},
        )
        browser = self.getViewBrowser(
            context=snap, no_login=True, user=registrant
        )
        content = find_main_content(browser.contents)
        self.assertThat(
            "Source snap channels for automatic builds:\n"
            "snapcraft\nstable/launchpad-buildd",
            MatchesTagText(content, "auto_build_channels"),
        )

    # Bug #2033382
    def test_snap_anonymous_view_byarch(self):
        registrant = self.factory.makePerson(name="snap-registrant")
        team_owner = self.factory.makeTeam(
            name="snap-creator", members=[registrant]
        )
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        snap = self.factory.makeSnap(
            name="core20",
            registrant=registrant,
            owner=team_owner,
            date_created=date_created,
            auto_build_channels={
                "_byarch": {"riscv64": {"snapcraft": "beta"}}
            },
        )
        browser = self.getViewBrowser(
            context=snap, no_login=True, user=registrant
        )
        content = find_main_content(browser.contents)
        self.assertThat(
            "Source snap channels for automatic builds:\n"
            "_byarch\n{'riscv64': {'snapcraft': 'beta'}}",
            MatchesTagText(content, "auto_build_channels"),
        )

    # Bug #2033382
    def test_snap_anonymous_api_byarch(self):
        registrant = self.factory.makePerson(name="snap-registrant")
        team_owner = self.factory.makeTeam(
            name="snap-creator", members=[registrant]
        )
        date_created = datetime(2000, 1, 1, tzinfo=timezone.utc)
        snap = self.factory.makeSnap(
            name="core20",
            registrant=registrant,
            owner=team_owner,
            date_created=date_created,
            auto_build_channels={
                "_byarch": {"riscv64": {"snapcraft": "beta"}}
            },
        )
        api_base = "http://api.launchpad.test/devel"
        snap_url = api_base + api_url(snap)

        webservice = webservice_for_person(
            None,
            default_api_version="devel",
        )
        response = webservice.get(snap_url)
        self.assertEqual(200, response.status)
        self.assertEqual(
            {"_byarch": {"riscv64": {"snapcraft": "beta"}}},
            response.jsonBody()["auto_build_channels"],
        )


class TestHintedSnapBuildChannelsWidget(TestSnapBuildChannelsWidget):
    def setUp(self):
        super().setUp()
        self.widget = HintedSnapBuildChannelsWidget(self.field, self.request)

    def test_hint_no_feature_flag(self):
        self.assertEqual(
            "The channels to use for build tools when building the snap "
            "package.\n"
            'If unset, or if the channel for snapcraft is set to "apt", '
            "the default is to install snapcraft from the source archive "
            "using apt.",
            self.widget.hint,
        )

    def test_hint_feature_flag_apt(self):
        self.useFixture(
            FeatureFixture({SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG: "apt"})
        )
        widget = HintedSnapBuildChannelsWidget(self.field, self.request)
        self.assertEqual(
            "The channels to use for build tools when building the snap "
            "package.\n"
            'If unset, or if the channel for snapcraft is set to "apt", '
            "the default is to install snapcraft from the source archive "
            "using apt.",
            widget.hint,
        )

    def test_hint_feature_flag_real_channel(self):
        self.useFixture(
            FeatureFixture({SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG: "stable"})
        )
        widget = HintedSnapBuildChannelsWidget(self.field, self.request)
        self.assertEqual(
            "The channels to use for build tools when building the snap "
            "package.\n"
            'If unset, the default is to install snapcraft from the "stable" '
            'channel.  Setting the channel for snapcraft to "apt" causes '
            "snapcraft to be installed from the source archive using "
            "apt.",
            widget.hint,
        )


class TestSnapRequestBuildsView(BaseTestSnapView):
    def setUp(self):
        super().setUp()
        self.ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntu, name="shiny", displayname="Shiny"
        )
        self.architectures = []
        for processor, architecture in ("386", "i386"), ("amd64", "amd64"):
            das = self.factory.makeDistroArchSeries(
                distroseries=self.distroseries,
                architecturetag=architecture,
                processor=getUtility(IProcessorSet).getByName(processor),
            )
            das.addOrUpdateChroot(self.factory.makeLibraryFileAlias())
            self.architectures.append(das)
        self.snap = self.factory.makeSnap(
            registrant=self.person,
            owner=self.person,
            distroseries=self.distroseries,
            name="snap-name",
        )

    def test_request_builds_page(self):
        # The +request-builds page is sane.
        self.assertTextMatchesExpressionIgnoreWhitespace(
            r"""
            Request builds for snap-name
            Snap packages
            snap-name
            Request builds
            Source archive:
            Primary Archive for Ubuntu Linux
            PPA
            \(Find…\)
            Architectures:
            amd64
            i386
            If you do not explicitly select any architectures, then the snap
            package will be built for all architectures allowed by its
            configuration.
            Pocket:
            Release
            Security
            Updates
            Proposed
            Backports
            \(\?\)
            The package stream within the source archive and distribution
            series to use when building the snap package.  If the source
            archive is a PPA, then the PPA's archive dependencies will be
            used to select the pocket in the distribution's primary archive.
            Source snap channels:
            core
            core18
            core20
            core22
            core24
            core26
            snapcraft
            snapd
            The channels to use for build tools when building the snap
            package.
            If unset, or if the channel for snapcraft is set to "apt", the
            default is to install snapcraft from the source archive using
            apt.
            or
            Cancel
            """,
            self.getMainText(self.snap, "+request-builds", user=self.person),
        )

    def test_request_builds_not_owner(self):
        # A user without launchpad.Edit cannot request builds.
        self.assertRaises(
            Unauthorized, self.getViewBrowser, self.snap, "+request-builds"
        )

    def test_request_builds_with_architectures_action(self):
        # Requesting a build with architectures selected creates a pending
        # build request limited to those architectures.
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        browser.getControl("amd64").selected = True
        browser.getControl("i386").selected = True
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertThat(
            removeSecurityProxy(request),
            MatchesStructure(
                snap=Equals(self.snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(list, Equals([])),
                archive=Equals(self.ubuntu.main_archive),
                _job=MatchesStructure(
                    requester=Equals(self.person),
                    pocket=Equals(PackagePublishingPocket.UPDATES),
                    channels=Equals({}),
                    architectures=MatchesSetwise(
                        Equals("amd64"), Equals("i386")
                    ),
                ),
            ),
        )

    def test_request_builds_with_architectures_ppa(self):
        # Selecting a different archive with architectures selected creates
        # a build request targeting that archive and limited to those
        # architectures.
        ppa = self.factory.makeArchive(
            distribution=self.ubuntu, owner=self.person, name="snap-ppa"
        )
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        browser.getControl("PPA").click()
        browser.getControl(name="field.archive.ppa").value = ppa.reference
        browser.getControl("amd64").selected = True
        self.assertFalse(browser.getControl("i386").selected)
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertThat(
            request,
            MatchesStructure(
                archive=Equals(ppa),
                architectures=MatchesSetwise(Equals("amd64")),
            ),
        )

    def test_request_builds_with_architectures_channels(self):
        # Selecting different channels with architectures selected creates a
        # build request using those channels and limited to those
        # architectures.
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        browser.getControl(name="field.channels.core").value = "edge"
        browser.getControl("amd64").selected = True
        self.assertFalse(browser.getControl("i386").selected)
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertThat(
            request,
            MatchesStructure(
                channels=MatchesDict({"core": Equals("edge")}),
                architectures=MatchesSetwise(Equals("amd64")),
            ),
        )

    def test_request_builds_no_architectures_action(self):
        # Requesting a build with no architectures selected creates a
        # pending build request.
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        self.assertFalse(browser.getControl("amd64").selected)
        self.assertFalse(browser.getControl("i386").selected)
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertThat(
            removeSecurityProxy(request),
            MatchesStructure(
                snap=Equals(self.snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(list, Equals([])),
                archive=Equals(self.ubuntu.main_archive),
                _job=MatchesStructure(
                    requester=Equals(self.person),
                    pocket=Equals(PackagePublishingPocket.UPDATES),
                    channels=Equals({}),
                    architectures=Is(None),
                ),
            ),
        )

    def test_request_builds_no_architectures_ppa(self):
        # Selecting a different archive with no architectures selected
        # creates a build request targeting that archive.
        ppa = self.factory.makeArchive(
            distribution=self.ubuntu, owner=self.person, name="snap-ppa"
        )
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        browser.getControl("PPA").click()
        browser.getControl(name="field.archive.ppa").value = ppa.reference
        self.assertFalse(browser.getControl("amd64").selected)
        self.assertFalse(browser.getControl("i386").selected)
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertThat(
            request,
            MatchesStructure(archive=Equals(ppa), architectures=Is(None)),
        )

    def test_request_builds_no_architectures_channels(self):
        # Selecting different channels with no architectures selected
        # creates a build request using those channels.
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        browser.getControl(name="field.channels.core").value = "edge"
        self.assertFalse(browser.getControl("amd64").selected)
        self.assertFalse(browser.getControl("i386").selected)
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertEqual({"core": "edge"}, request.channels)

    def test_request_builds_no_distro_series(self):
        # Requesting builds of a snap configured to infer an appropriate
        # distro series from snapcraft.yaml creates a pending build request.
        login_person(self.person)
        self.snap.distro_series = None
        browser = self.getViewBrowser(
            self.snap, "+request-builds", user=self.person
        )
        browser.getControl("Request builds").click()

        login_person(self.person)
        [request] = self.snap.pending_build_requests
        self.assertThat(
            removeSecurityProxy(request),
            MatchesStructure(
                snap=Equals(self.snap),
                status=Equals(SnapBuildRequestStatus.PENDING),
                error_message=Is(None),
                builds=AfterPreprocessing(list, Equals([])),
                archive=Equals(self.ubuntu.main_archive),
                _job=MatchesStructure(
                    requester=Equals(self.person),
                    pocket=Equals(PackagePublishingPocket.UPDATES),
                    channels=Equals({}),
                ),
            ),
        )
