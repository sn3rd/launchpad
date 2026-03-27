# Copyright 2012-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import six
import transaction
from lazr.restful.interfaces import IWebBrowserOriginatingRequest
from lazr.restful.utils import get_current_web_service_request
from testscenarios import WithScenarios, load_tests_apply_scenarios
from testtools.matchers import LessThan
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy
from zope.traversing.browser.absoluteurl import absoluteURL

from lp.app.enums import InformationType
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.interfaces.services import IService
from lp.blueprints.interfaces.specification import ISpecification
from lp.bugs.interfaces.bug import IBug
from lp.code.enums import (
    BranchSubscriptionNotificationLevel,
    CodeReviewNotificationLevel,
)
from lp.code.interfaces.branch import IBranch
from lp.code.interfaces.gitrepository import IGitRepository
from lp.oci.tests.helpers import OCIConfigHelperMixin
from lp.registry.enums import (
    BranchSharingPolicy,
    BugSharingPolicy,
    SharingPermission,
    SpecificationSharingPolicy,
)
from lp.registry.interfaces.accesspolicy import (
    IAccessArtifactGrantSource,
    IAccessArtifactSource,
    IAccessPolicyGrantFlatSource,
    IAccessPolicyGrantSource,
    IAccessPolicySource,
)
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.person import TeamMembershipPolicy
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.role import IPersonRoles
from lp.registry.interfaces.sourcepackage import ISourcePackage
from lp.registry.services.sharingservice import SharingService
from lp.services.job.tests import block_on_job
from lp.services.webapp.interaction import ANONYMOUS
from lp.services.webapp.interfaces import ILaunchpadRoot
from lp.services.webapp.publisher import canonical_url
from lp.testing import (
    StormStatementRecorder,
    TestCaseWithFactory,
    WebServiceTestCase,
    admin_logged_in,
    login,
    login_person,
    person_logged_in,
    ws_object,
)
from lp.testing.layers import AppServerLayer, CeleryJobLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.pages import LaunchpadWebServiceCaller


class PillarScenariosMixin(WithScenarios):
    """Scenarios for testing against different pillar types."""

    scenarios = [
        (
            "project",
            {
                "pillar_factory_name": "makeProduct",
                "get_shared_pillars_name": "getSharedProjects",
            },
        ),
        (
            "distribution",
            {
                "pillar_factory_name": "makeDistribution",
                "get_shared_pillars_name": "getSharedDistributions",
            },
        ),
    ]

    def _skipUnlessProduct(self):
        if self.pillar_factory_name != "makeProduct":
            self.skipTest("Only relevant for Product.")

    def isPillarADistribution(self):
        return self.pillar_factory_name == "makeDistribution"

    def _skipUnlessDistribution(self):
        if not self.isPillarADistribution():
            self.skipTest("Only relevant for Distribution.")

    def _makePillar(self, **kwargs):
        return getattr(self.factory, self.pillar_factory_name)(**kwargs)

    def _makeBranch(self, pillar, **kwargs):
        kwargs = dict(kwargs)
        if IProduct.providedBy(pillar):
            kwargs["product"] = pillar
        elif IDistribution.providedBy(pillar):
            distroseries = self.factory.makeDistroSeries(distribution=pillar)
            sourcepackage = self.factory.makeSourcePackage(
                distroseries=distroseries
            )
            kwargs["sourcepackage"] = sourcepackage
        else:
            raise AssertionError("Unknown pillar: %r" % pillar)
        return self.factory.makeBranch(**kwargs)

    def _makeGitRepository(self, pillar, **kwargs):
        kwargs = dict(kwargs)
        if IProduct.providedBy(pillar):
            kwargs["target"] = pillar
        elif IDistribution.providedBy(pillar):
            dsp = self.factory.makeDistributionSourcePackage(
                distribution=pillar
            )
            kwargs["target"] = dsp
        else:
            raise AssertionError("Unknown pillar: %r" % pillar)
        return self.factory.makeGitRepository(**kwargs)

    def _makeSpecification(self, pillar, **kwargs):
        kwargs = dict(kwargs)
        if IProduct.providedBy(pillar):
            kwargs["product"] = pillar
        elif IDistribution.providedBy(pillar):
            kwargs["distribution"] = pillar
        else:
            raise AssertionError("Unknown pillar: %r" % pillar)
        return self.factory.makeSpecification(**kwargs)


class TestSharingService(
    PillarScenariosMixin, OCIConfigHelperMixin, TestCaseWithFactory
):
    """Tests for the SharingService."""

    layer = CeleryJobLayer

    def setUp(self):
        super().setUp()
        self.service = getUtility(IService, "sharing")
        self.setConfig(
            feature_flags={
                "jobs.celery.enabled_classes": "RemoveArtifactSubscriptionsJob"
            }
        )

    def _makeGranteeData(
        self, grantee, policy_permissions, shared_artifact_types
    ):
        # Unpack a grantee into its attributes and add in permissions.
        request = get_current_web_service_request()
        sprite_css = "sprite " + ("team" if grantee.is_team else "person")
        if grantee.icon:
            icon_url = grantee.icon.getURL()
        else:
            icon_url = None
        grantee_data = {
            "name": grantee.name,
            "icon_url": icon_url,
            "sprite_css": sprite_css,
            "display_name": grantee.displayname,
            "self_link": absoluteURL(grantee, request),
            "permissions": {},
        }
        browser_request = IWebBrowserOriginatingRequest(request)
        grantee_data["web_link"] = absoluteURL(grantee, browser_request)
        shared_items_exist = False
        permissions = {}
        for policy, permission in policy_permissions:
            permissions[policy.name] = six.ensure_text(permission.name)
            if permission == SharingPermission.SOME:
                shared_items_exist = True
        grantee_data["shared_items_exist"] = shared_items_exist
        grantee_data["shared_artifact_types"] = [
            info_type.name for info_type in shared_artifact_types
        ]
        grantee_data["permissions"] = permissions
        return grantee_data

    def test_getSharingPermissions(self):
        # test_getSharingPermissions returns permissions in the right order.
        permissions = self.service.getSharingPermissions()
        expected_permissions = [
            SharingPermission.ALL,
            SharingPermission.SOME,
            SharingPermission.NOTHING,
        ]
        for x, permission in enumerate(expected_permissions):
            self.assertEqual(permissions[x]["value"], permission.name)

    def _assert_enumData(self, expected_enums, enum_data):
        expected_data = []
        for x, enum in enumerate(expected_enums):
            item = dict(
                index=x,
                value=enum.name,
                title=enum.title,
                description=enum.description,
            )
            expected_data.append(item)
        self.assertContentEqual(expected_data, enum_data)

    def _assert_getAllowedInformationTypes(self, pillar, expected_policies):
        policy_data = self.service.getAllowedInformationTypes(pillar)
        self._assert_enumData(expected_policies, policy_data)

    def test_getInformationTypes(self):
        pillar = self._makePillar()
        self._assert_getAllowedInformationTypes(
            pillar, [InformationType.PRIVATESECURITY, InformationType.USERDATA]
        )

    def test_getInformationTypes_expired_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        self._assert_getAllowedInformationTypes(
            pillar, [InformationType.PRIVATESECURITY, InformationType.USERDATA]
        )

    def test_getInformationTypes_commercial(self):
        pillar = self._makePillar(
            branch_sharing_policy=BranchSharingPolicy.PROPRIETARY,
            bug_sharing_policy=BugSharingPolicy.PROPRIETARY,
        )
        self._assert_getAllowedInformationTypes(
            pillar, [InformationType.PROPRIETARY]
        )

    def test_getInformationTypes_with_embargoed(self):
        pillar = self._makePillar(
            branch_sharing_policy=BranchSharingPolicy.EMBARGOED_OR_PROPRIETARY,
            bug_sharing_policy=BugSharingPolicy.PROPRIETARY,
        )
        self._assert_getAllowedInformationTypes(
            pillar, [InformationType.PROPRIETARY, InformationType.EMBARGOED]
        )

    def _assert_getBranchSharingPolicies(self, pillar, expected_policies):
        policy_data = self.service.getBranchSharingPolicies(pillar)
        self._assert_enumData(expected_policies, policy_data)

    def test_getBranchSharingPolicies(self):
        pillar = self._makePillar()
        self._assert_getBranchSharingPolicies(
            pillar, [BranchSharingPolicy.PUBLIC]
        )

    def test_getBranchSharingPolicies_expired_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        self._assert_getBranchSharingPolicies(
            pillar, [BranchSharingPolicy.PUBLIC]
        )

    def test_getBranchSharingPolicies_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar)
        self._assert_getBranchSharingPolicies(
            pillar,
            [
                BranchSharingPolicy.PUBLIC,
                BranchSharingPolicy.PUBLIC_OR_PROPRIETARY,
                BranchSharingPolicy.PROPRIETARY_OR_PUBLIC,
                BranchSharingPolicy.PROPRIETARY,
            ],
        )

    def test_getBranchSharingPolicies_non_public(self):
        # When the pillar is non-public the policy options are limited to
        # only proprietary or embargoed/proprietary.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            information_type=InformationType.PROPRIETARY, owner=owner
        )
        with person_logged_in(owner):
            self._assert_getBranchSharingPolicies(
                pillar,
                [
                    BranchSharingPolicy.EMBARGOED_OR_PROPRIETARY,
                    BranchSharingPolicy.PROPRIETARY,
                ],
            )

    def test_getBranchSharingPolicies_disallowed_policy(self):
        # getBranchSharingPolicies includes a pillar's current policy even if
        # it is nominally not allowed.
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        with person_logged_in(pillar.owner):
            pillar.setBranchSharingPolicy(BranchSharingPolicy.FORBIDDEN)
        self._assert_getBranchSharingPolicies(
            pillar, [BranchSharingPolicy.PUBLIC, BranchSharingPolicy.FORBIDDEN]
        )

    def test_getBranchSharingPolicies_with_embargoed(self):
        # If the current sharing policy is embargoed, it can still be made
        # proprietary.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            information_type=InformationType.PROPRIETARY,
            owner=owner,
            branch_sharing_policy=BranchSharingPolicy.EMBARGOED_OR_PROPRIETARY,
        )
        with person_logged_in(owner):
            self._assert_getBranchSharingPolicies(
                pillar,
                [
                    BranchSharingPolicy.EMBARGOED_OR_PROPRIETARY,
                    BranchSharingPolicy.PROPRIETARY,
                ],
            )

    def _assert_getSpecificationSharingPolicies(
        self, pillar, expected_policies
    ):
        policy_data = self.service.getSpecificationSharingPolicies(pillar)
        self._assert_enumData(expected_policies, policy_data)

    def test_getSpecificationSharingPolicies(self):
        pillar = self._makePillar()
        self._assert_getSpecificationSharingPolicies(
            pillar, [SpecificationSharingPolicy.PUBLIC]
        )

    def test_getSpecificationSharingPolicies_expired_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        self._assert_getSpecificationSharingPolicies(
            pillar, [SpecificationSharingPolicy.PUBLIC]
        )

    def test_getSpecificationSharingPolicies_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar)
        self._assert_getSpecificationSharingPolicies(
            pillar,
            [
                SpecificationSharingPolicy.PUBLIC,
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY,
                SpecificationSharingPolicy.PROPRIETARY_OR_PUBLIC,
                SpecificationSharingPolicy.PROPRIETARY,
            ],
        )

    def test_getSpecificationSharingPolicies_non_public(self):
        # When the pillar is non-public the policy options are limited to
        # only proprietary or embargoed/proprietary.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            information_type=InformationType.PROPRIETARY,
            owner=owner,
        )
        with person_logged_in(owner):
            self._assert_getSpecificationSharingPolicies(
                pillar,
                [
                    SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY,
                    SpecificationSharingPolicy.PROPRIETARY,
                ],
            )

    def test_getSpecificationSharingPolicies_with_embargoed(self):
        # The sharing policies will contain the pillar's sharing policy even
        # if it is not in the nominally allowed policy list.
        pillar = self._makePillar(
            specification_sharing_policy=(
                SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY
            )
        )
        self._assert_getSpecificationSharingPolicies(
            pillar,
            [
                SpecificationSharingPolicy.PUBLIC,
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY,
                SpecificationSharingPolicy.PROPRIETARY_OR_PUBLIC,
                SpecificationSharingPolicy.PROPRIETARY,
                SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY,
            ],
        )

    def _assert_getBugSharingPolicies(self, pillar, expected_policies):
        policy_data = self.service.getBugSharingPolicies(pillar)
        self._assert_enumData(expected_policies, policy_data)

    def test_getBugSharingPolicies(self):
        pillar = self._makePillar()
        self._assert_getBugSharingPolicies(pillar, [BugSharingPolicy.PUBLIC])

    def test_getBugSharingPolicies_expired_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        self._assert_getBugSharingPolicies(pillar, [BugSharingPolicy.PUBLIC])

    def test_getBugSharingPolicies_commercial(self):
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar)
        self._assert_getBugSharingPolicies(
            pillar,
            [
                BugSharingPolicy.PUBLIC,
                BugSharingPolicy.PUBLIC_OR_PROPRIETARY,
                BugSharingPolicy.PROPRIETARY_OR_PUBLIC,
                BugSharingPolicy.PROPRIETARY,
            ],
        )

    def test_getBugSharingPolicies_non_public(self):
        # When the pillar is non-public the policy options are limited to
        # only proprietary or embargoed/proprietary.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            information_type=InformationType.PROPRIETARY,
            owner=owner,
        )
        with person_logged_in(owner):
            self._assert_getBugSharingPolicies(
                pillar,
                [
                    BugSharingPolicy.EMBARGOED_OR_PROPRIETARY,
                    BugSharingPolicy.PROPRIETARY,
                ],
            )

    def test_getBugSharingPolicies_disallowed_policy(self):
        # getBugSharingPolicies includes a pillar's current policy even if it
        # is nominally not allowed.
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        with person_logged_in(pillar.owner):
            pillar.setBugSharingPolicy(BugSharingPolicy.FORBIDDEN)
        self._assert_getBugSharingPolicies(
            pillar, [BugSharingPolicy.PUBLIC, BugSharingPolicy.FORBIDDEN]
        )

    def test_jsonGranteeData_with_Some(self):
        # jsonGranteeData returns the expected data for a grantee with
        # permissions which include SOME.
        pillar = self._makePillar()
        [policy1, policy2] = getUtility(IAccessPolicySource).findByPillar(
            [pillar]
        )
        grantee = self.factory.makePerson()
        grantees = self.service.jsonGranteeData(
            [
                (
                    grantee,
                    {
                        policy1: SharingPermission.ALL,
                        policy2: SharingPermission.SOME,
                    },
                    [policy1.type, policy2.type],
                )
            ]
        )
        expected_data = self._makeGranteeData(
            grantee,
            [
                (policy1.type, SharingPermission.ALL),
                (policy2.type, SharingPermission.SOME),
            ],
            [policy1.type, policy2.type],
        )
        self.assertContentEqual([expected_data], grantees)

    def test_jsonGranteeData_without_Some(self):
        # jsonGranteeData returns the expected data for a grantee with only ALL
        # permissions.
        pillar = self._makePillar()
        [policy1, policy2] = getUtility(IAccessPolicySource).findByPillar(
            [pillar]
        )
        grantee = self.factory.makePerson()
        grantees = self.service.jsonGranteeData(
            [(grantee, {policy1: SharingPermission.ALL}, [])]
        )
        expected_data = self._makeGranteeData(
            grantee, [(policy1.type, SharingPermission.ALL)], []
        )
        self.assertContentEqual([expected_data], grantees)

    def test_jsonGranteeData_with_icon(self):
        # jsonGranteeData returns the expected data for a grantee with has an
        # icon.
        pillar = self._makePillar()
        [policy1, policy2] = getUtility(IAccessPolicySource).findByPillar(
            [pillar]
        )
        icon = self.factory.makeLibraryFileAlias(
            filename="smurf.png", content_type="image/png"
        )
        grantee = self.factory.makeTeam(icon=icon)
        grantees = self.service.jsonGranteeData(
            [(grantee, {policy1: SharingPermission.ALL}, [])]
        )
        expected_data = self._makeGranteeData(
            grantee, [(policy1.type, SharingPermission.ALL)], []
        )
        self.assertContentEqual([expected_data], grantees)

    def _assert_getPillarGranteeData(self, pillar):
        # getPillarGranteeData returns the expected data.
        access_policy = self.factory.makeAccessPolicy(
            pillar=pillar, type=InformationType.PROPRIETARY
        )
        grantee = self.factory.makePerson()
        # Make access policy grant so that 'All' is returned.
        self.factory.makeAccessPolicyGrant(access_policy, grantee)
        # Make access artifact grants so that 'Some' is returned.
        artifact_grant = self.factory.makeAccessArtifactGrant()
        self.factory.makeAccessPolicyArtifact(
            artifact=artifact_grant.abstract_artifact, policy=access_policy
        )

        grantees = self.service.getPillarGranteeData(pillar)
        expected_grantees = [
            self._makeGranteeData(
                grantee,
                [(InformationType.PROPRIETARY, SharingPermission.ALL)],
                [],
            ),
            self._makeGranteeData(
                artifact_grant.grantee,
                [(InformationType.PROPRIETARY, SharingPermission.SOME)],
                [InformationType.PROPRIETARY],
            ),
            self._makeGranteeData(
                pillar.owner,
                [
                    (InformationType.USERDATA, SharingPermission.ALL),
                    (InformationType.PRIVATESECURITY, SharingPermission.ALL),
                ],
                [],
            ),
        ]
        self.assertContentEqual(expected_grantees, grantees)

    def test_getPillarGranteeData(self):
        # Users with launchpad.Driver can view grantees.
        driver = self.factory.makePerson()
        pillar = self._makePillar(driver=driver)
        login_person(driver)
        self._assert_getPillarGranteeData(pillar)

    def _assert_QueryCount(self, func, count):
        """getPillarGrantees[Data] only should use 3 queries.

        1. load access policies for pillar
        2. load grantees
        3. load permissions for grantee

        Steps 2 and 3 are split out to allow batching on persons.
        """
        driver = self.factory.makePerson()
        pillar = self._makePillar(driver=driver)
        login_person(driver)
        access_policy = self.factory.makeAccessPolicy(
            pillar=pillar, type=InformationType.PROPRIETARY
        )

        def makeGrants():
            grantee = self.factory.makePerson()
            # Make access policy grant so that 'All' is returned.
            self.factory.makeAccessPolicyGrant(access_policy, grantee)
            # Make access artifact grants so that 'Some' is returned.
            artifact_grant = self.factory.makeAccessArtifactGrant()
            self.factory.makeAccessPolicyArtifact(
                artifact=artifact_grant.abstract_artifact, policy=access_policy
            )

        # Make some grants and check the count.
        for _ in range(5):
            makeGrants()
        with StormStatementRecorder() as recorder:
            grantees = list(func(pillar))
        self.assertEqual(11, len(grantees))
        self.assertThat(recorder, HasQueryCount(LessThan(count)))
        # Make some more grants and check again.
        for _ in range(5):
            makeGrants()
        with StormStatementRecorder() as recorder:
            grantees = list(func(pillar))
        self.assertEqual(21, len(grantees))
        self.assertThat(recorder, HasQueryCount(LessThan(count)))

    def test_getPillarGranteesQueryCount(self):
        self._assert_QueryCount(self.service.getPillarGrantees, 4)

    def test_getPillarGranteeDataQueryCount(self):
        self._assert_QueryCount(self.service.getPillarGranteeData, 6)

    def _assert_getPillarGranteeDataUnauthorized(self, pillar):
        # getPillarGranteeData raises an Unauthorized exception if the user is
        # not permitted to do so.
        access_policy = self.factory.makeAccessPolicy(pillar=pillar)
        grantee = self.factory.makePerson()
        self.factory.makeAccessPolicyGrant(access_policy, grantee)
        self.assertRaises(
            Unauthorized, self.service.getPillarGranteeData, pillar
        )

    def test_getPillarGranteeDataAnonymous(self):
        # Anonymous users are not allowed.
        pillar = self._makePillar()
        login(ANONYMOUS)
        self._assert_getPillarGranteeDataUnauthorized(pillar)

    def test_getPillarGranteeDataAnyone(self):
        # Unauthorized users are not allowed.
        pillar = self._makePillar()
        login_person(self.factory.makePerson())
        self._assert_getPillarGranteeDataUnauthorized(pillar)

    def _assert_getPillarGrantees(self, pillar):
        # getPillarGrantees returns the expected data.
        access_policy = self.factory.makeAccessPolicy(
            pillar=pillar, type=InformationType.PROPRIETARY
        )
        grantee = self.factory.makePerson()
        # Make access policy grant so that 'All' is returned.
        self.factory.makeAccessPolicyGrant(access_policy, grantee)
        # Make access artifact grants so that 'Some' is returned.
        artifact_grant = self.factory.makeAccessArtifactGrant()
        self.factory.makeAccessPolicyArtifact(
            artifact=artifact_grant.abstract_artifact, policy=access_policy
        )

        grantees = self.service.getPillarGrantees(pillar)
        policies = getUtility(IAccessPolicySource).findByPillar([pillar])
        policies = [
            policy
            for policy in policies
            if policy.type != InformationType.PROPRIETARY
        ]
        expected_grantees = [
            (grantee, {access_policy: SharingPermission.ALL}, []),
            (
                artifact_grant.grantee,
                {access_policy: SharingPermission.SOME},
                [access_policy.type],
            ),
            (pillar.owner, dict.fromkeys(policies, SharingPermission.ALL), []),
        ]
        self.assertContentEqual(expected_grantees, grantees)

    def test_getPillarGrantees(self):
        # Users with launchpad.Driver can view grantees.
        driver = self.factory.makePerson()
        pillar = self._makePillar(driver=driver)
        login_person(driver)
        self._assert_getPillarGrantees(pillar)

    def _assert_getPillarGranteesUnauthorized(self, pillar):
        # getPillarGrantees raises an Unauthorized exception if the user is
        # not permitted to do so.
        access_policy = self.factory.makeAccessPolicy(pillar=pillar)
        grantee = self.factory.makePerson()
        self.factory.makeAccessPolicyGrant(access_policy, grantee)
        self.assertRaises(Unauthorized, self.service.getPillarGrantees, pillar)

    def test_getPillarGranteesAnonymous(self):
        # Anonymous users are not allowed.
        pillar = self._makePillar()
        login(ANONYMOUS)
        self._assert_getPillarGranteesUnauthorized(pillar)

    def test_getPillarGranteesAnyone(self):
        # Unauthorized users are not allowed.
        pillar = self._makePillar()
        login_person(self.factory.makePerson())
        self._assert_getPillarGranteesUnauthorized(pillar)

    def _assert_grantee_data(self, expected, actual):
        # Assert that the actual and expected grantee data is equal.
        # Grantee data is a list of (grantee, permissions, info_types) tuples.
        expected_list = list(expected)
        actual_list = list(actual)
        self.assertEqual(len(expected_list), len(list(actual_list)))

        expected_grantee_map = {}
        for data in expected_list:
            expected_grantee_map[data[0]] = data[1:]
        actual_grantee_map = {}
        for data in actual_list:
            actual_grantee_map[data[0]] = data[1:]

        for grantee, expected_permissions, expected_info_types in expected:
            actual_permissions, actual_info_types = actual_grantee_map[grantee]
            self.assertContentEqual(expected_permissions, actual_permissions)
            self.assertContentEqual(expected_info_types, actual_info_types)

    def _assert_sharePillarInformation(self, pillar):
        """sharePillarInformations works and returns the expected data."""
        grantee = self.factory.makePerson()
        grantor = self.factory.makePerson()

        # Make existing grants to ensure sharePillarInformation handles those
        # cases correctly.
        # First, a grant that is in the add set - it will be retained.
        es_policy = getUtility(IAccessPolicySource).find(
            ((pillar, InformationType.PRIVATESECURITY),)
        )[0]
        ud_policy = getUtility(IAccessPolicySource).find(
            ((pillar, InformationType.USERDATA),)
        )[0]
        self.factory.makeAccessPolicyGrant(
            es_policy, grantee=grantee, grantor=grantor
        )
        # Second, grants that are not in the all set - they will be deleted.
        p_policy = self.factory.makeAccessPolicy(
            pillar=pillar, type=InformationType.PROPRIETARY
        )
        self.factory.makeAccessPolicyGrant(
            p_policy, grantee=grantee, grantor=grantor
        )
        self.factory.makeAccessPolicyGrant(
            ud_policy, grantee=grantee, grantor=grantor
        )

        # We also make some artifact grants.
        # First, a grant which will be retained.
        artifact = self.factory.makeAccessArtifact()
        self.factory.makeAccessArtifactGrant(artifact, grantee)
        self.factory.makeAccessPolicyArtifact(
            artifact=artifact, policy=es_policy
        )
        # Second, grants which will be deleted because their policies have
        # information types in the 'some' or 'nothing' category.
        artifact = self.factory.makeAccessArtifact()
        self.factory.makeAccessArtifactGrant(artifact, grantee)
        self.factory.makeAccessPolicyArtifact(
            artifact=artifact, policy=p_policy
        )
        artifact = self.factory.makeAccessArtifact()
        self.factory.makeAccessArtifactGrant(artifact, grantee)
        self.factory.makeAccessPolicyArtifact(
            artifact=artifact, policy=ud_policy
        )

        # Now call sharePillarInformation will the grants we want.
        permissions = {
            InformationType.PRIVATESECURITY: SharingPermission.ALL,
            InformationType.USERDATA: SharingPermission.SOME,
            InformationType.PROPRIETARY: SharingPermission.NOTHING,
        }
        grantee_data = self.service.sharePillarInformation(
            pillar, grantee, grantor, permissions
        )
        policies = getUtility(IAccessPolicySource).findByPillar([pillar])
        policy_grant_source = getUtility(IAccessPolicyGrantSource)
        grants = policy_grant_source.findByPolicy(policies)

        # Filter out the owner's grants if they exist. They're automatic and
        # already tested.
        [grant] = [g for g in grants if g.grantee != pillar.owner]
        self.assertEqual(grantor, grant.grantor)
        self.assertEqual(grantee, grant.grantee)
        expected_permissions = [
            (InformationType.PRIVATESECURITY, SharingPermission.ALL),
            (InformationType.USERDATA, SharingPermission.SOME),
        ]
        expected_grantee_data = self._makeGranteeData(
            grantee,
            expected_permissions,
            [InformationType.PRIVATESECURITY, InformationType.USERDATA],
        )
        self.assertContentEqual(
            expected_grantee_data, grantee_data["grantee_entry"]
        )
        # Check that getPillarGrantees returns what we expect.
        expected_grantee_grants = [
            (
                grantee,
                {
                    ud_policy: SharingPermission.SOME,
                    es_policy: SharingPermission.ALL,
                },
                [InformationType.PRIVATESECURITY, InformationType.USERDATA],
            ),
        ]

        grantee_grants = list(self.service.getPillarGrantees(pillar))
        # Again, filter out the owner, if one exists.
        grantee_grants = [s for s in grantee_grants if s[0] != pillar.owner]
        self.assertContentEqual(expected_grantee_grants, grantee_grants)

    def test_updateProjectGroupGrantee_not_allowed(self):
        # We cannot add grantees to ProjectGroups.
        owner = self.factory.makePerson()
        project_group = self.factory.makeProject(owner=owner)
        grantee = self.factory.makePerson()
        login_person(owner)
        self.assertRaises(
            AssertionError,
            self.service.sharePillarInformation,
            project_group,
            grantee,
            owner,
            {InformationType.USERDATA: SharingPermission.ALL},
        )

    def test_updatePillarGrantee(self):
        # Users with launchpad.Edit can add grantees.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        self._assert_sharePillarInformation(pillar)

    def test_updatePillarGrantee_no_access_grants_remain(self):
        # When a pillar grantee has it's only access policy permission changed
        # to Some, test that None is returned.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        grantee = self.factory.makePerson()
        grant = self.factory.makeAccessPolicyGrant(grantee=grantee)

        permissions = {grant.policy.type: SharingPermission.SOME}
        grantee_data = self.service.sharePillarInformation(
            pillar, grantee, self.factory.makePerson(), permissions
        )
        self.assertIsNone(grantee_data["grantee_entry"])

    def test_granteePillarInformationInvisibleInformationTypes(self):
        # Sharing with a user returns data containing the resulting invisible
        # information types.
        pillar = self._makePillar()
        grantee = self.factory.makePerson()
        with admin_logged_in():
            self.service.deletePillarGrantee(
                pillar, pillar.owner, pillar.owner
            )
            result_data = self.service.sharePillarInformation(
                pillar,
                grantee,
                pillar.owner,
                {InformationType.USERDATA: SharingPermission.ALL},
            )
        # The owner is granted access on pillar creation. So we need to allow
        # for that in the check below.
        self.assertContentEqual(
            ["Private Security"], result_data["invisible_information_types"]
        )

    def _assert_sharePillarInformationUnauthorized(self, pillar):
        # sharePillarInformation raises an Unauthorized exception if the user
        # is not permitted to do so.
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        self.assertRaises(
            Unauthorized,
            self.service.sharePillarInformation,
            pillar,
            grantee,
            user,
            {InformationType.USERDATA: SharingPermission.ALL},
        )

    def test_sharePillarInformationAnonymous(self):
        # Anonymous users are not allowed.
        pillar = self._makePillar()
        login(ANONYMOUS)
        self._assert_sharePillarInformationUnauthorized(pillar)

    def test_sharePillarInformationAnyone(self):
        # Unauthorized users are not allowed.
        pillar = self._makePillar()
        login_person(self.factory.makePerson())
        self._assert_sharePillarInformationUnauthorized(pillar)

    def _assert_deletePillarGrantee(self, pillar, types_to_delete=None):
        access_policies = getUtility(IAccessPolicySource).findByPillar(
            (pillar,)
        )
        information_types = [ap.type for ap in access_policies]
        grantee = self.factory.makePerson()
        # Make some access policy grants for our grantee.
        for access_policy in access_policies:
            self.factory.makeAccessPolicyGrant(access_policy, grantee)
        # Make some artifact grants for our grantee.
        artifact = self.factory.makeAccessArtifact()
        self.factory.makeAccessArtifactGrant(artifact, grantee)
        # Make some access policy grants for another grantee.
        another = self.factory.makePerson()
        self.factory.makeAccessPolicyGrant(access_policies[0], another)
        # Make some artifact grants for our yet another grantee.
        yet_another = self.factory.makePerson()
        self.factory.makeAccessArtifactGrant(artifact, yet_another)
        for access_policy in access_policies:
            self.factory.makeAccessPolicyArtifact(
                artifact=artifact, policy=access_policy
            )
        # Delete data for a specific information type.
        self.service.deletePillarGrantee(
            pillar, grantee, pillar.owner, types_to_delete
        )
        # Assemble the expected data for the remaining access grants for
        # grantee.
        expected_data = []
        if types_to_delete is not None:
            expected_information_types = set(information_types).difference(
                types_to_delete
            )
            expected_policies = [
                access_policy
                for access_policy in access_policies
                if access_policy.type in expected_information_types
            ]
            expected_data = [
                (grantee, {policy: SharingPermission.ALL}, [])
                for policy in expected_policies
            ]
        # Add the expected data for the other grantees.
        another_person_data = (
            another,
            {access_policies[0]: SharingPermission.ALL},
            [],
        )
        expected_data.append(another_person_data)
        policy_permissions = {
            policy: SharingPermission.SOME for policy in access_policies
        }
        yet_another_person_data = (
            yet_another,
            policy_permissions,
            [InformationType.PRIVATESECURITY, InformationType.USERDATA],
        )
        expected_data.append(yet_another_person_data)
        policy_permissions = {
            policy: SharingPermission.ALL for policy in access_policies
        }
        owner_data = (pillar.owner, policy_permissions, [])
        expected_data.append(owner_data)
        self._assert_grantee_data(
            expected_data, self.service.getPillarGrantees(pillar)
        )

    def test_deletePillarGranteeAll(self):
        # Users with launchpad.Edit can delete all access for a grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        self._assert_deletePillarGrantee(pillar)

    def test_deletePillarGranteeSelectedPolicies(self):
        # Users with launchpad.Edit can delete selected policy access for an
        # grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        self._assert_deletePillarGrantee(pillar, [InformationType.USERDATA])

    def test_deletePillarGranteeInvisibleInformationTypes(self):
        # Deleting a pillar grantee returns the resulting invisible info types.
        pillar = self._makePillar()
        with admin_logged_in():
            invisible_information_types = self.service.deletePillarGrantee(
                pillar, pillar.owner, pillar.owner
            )
        self.assertContentEqual(
            ["Private", "Private Security"], invisible_information_types
        )

    def _assert_deletePillarGranteeUnauthorized(self, pillar):
        # deletePillarGrantee raises an Unauthorized exception if the user
        # is not permitted to do so.
        self.assertRaises(
            Unauthorized,
            self.service.deletePillarGrantee,
            pillar,
            pillar.owner,
            pillar.owner,
            [InformationType.USERDATA],
        )

    def test_deletePillarGranteeAnonymous(self):
        # Anonymous users are not allowed.
        pillar = self._makePillar()
        login(ANONYMOUS)
        self._assert_deletePillarGranteeUnauthorized(pillar)

    def test_deletePillarGranteeAnyone(self):
        # Unauthorized users are not allowed.
        pillar = self._makePillar()
        login_person(self.factory.makePerson())
        self._assert_deletePillarGranteeUnauthorized(pillar)

    def _assert_deleteGranteeRemoveSubscriptions(self, types_to_delete=None):
        pillar = self._makePillar()
        access_policies = getUtility(IAccessPolicySource).findByPillar(
            (pillar,)
        )
        information_types = [ap.type for ap in access_policies]
        grantee = self.factory.makePerson()
        # Make some access policy grants for our grantee.
        for access_policy in access_policies:
            self.factory.makeAccessPolicyGrant(access_policy, grantee)

        login_person(pillar.owner)
        # Make some bug artifact grants for our grantee.
        # Branches will be done when information_type attribute is supported.
        bugs = []
        for access_policy in access_policies:
            bug = self.factory.makeBug(
                target=pillar,
                owner=pillar.owner,
                information_type=access_policy.type,
            )
            bugs.append(bug)
            artifact = self.factory.makeAccessArtifact(concrete=bug)
            self.factory.makeAccessArtifactGrant(artifact, grantee)

        # Make some access policy grants for another grantee.
        another = self.factory.makePerson()
        self.factory.makeAccessPolicyGrant(access_policies[0], another)

        # Subscribe the grantee and other person to the artifacts.
        for person in [grantee, another]:
            for bug in bugs:
                bug.subscribe(person, pillar.owner)

        # Delete data for specified information types or all.
        self.service.deletePillarGrantee(
            pillar, grantee, pillar.owner, types_to_delete
        )
        with block_on_job(self):
            transaction.commit()

        expected_information_types = []
        if types_to_delete is not None:
            expected_information_types = set(information_types).difference(
                types_to_delete
            )
        # Check that grantee is unsubscribed.
        login_person(pillar.owner)
        for bug in bugs:
            if bug.information_type in expected_information_types:
                self.assertIn(grantee, bug.getDirectSubscribers())
            else:
                self.assertNotIn(grantee, bug.getDirectSubscribers())
            self.assertIn(another, bug.getDirectSubscribers())

    def test_granteeUnsubscribedWhenDeleted(self):
        # The grantee is unsubscribed from any inaccessible artifacts when
        # their access is revoked.
        self._assert_deleteGranteeRemoveSubscriptions()

    def test_granteeUnsubscribedWhenDeletedSelectedPolicies(self):
        # The grantee is unsubscribed from any inaccessible artifacts when
        # their access to selected policies is revoked.
        self._assert_deleteGranteeRemoveSubscriptions(
            [InformationType.USERDATA]
        )

    def _assert_revokeAccessGrants(
        self, pillar, bugs, branches, gitrepositories, specifications
    ):
        artifacts = []
        if bugs:
            artifacts.extend(bugs)
        if branches:
            artifacts.extend(branches)
        if gitrepositories:
            artifacts.extend(gitrepositories)
        if specifications:
            artifacts.extend(specifications)
        policy = self.factory.makeAccessPolicy(
            pillar=pillar, check_existing=True
        )
        # Grant access to a grantee and another person.
        grantee = self.factory.makePerson()
        someone = self.factory.makePerson()
        access_artifacts = []
        for artifact in artifacts:
            access_artifact = self.factory.makeAccessArtifact(
                concrete=artifact
            )
            access_artifacts.append(access_artifact)
            self.factory.makeAccessPolicyArtifact(
                artifact=access_artifact, policy=policy
            )
            for person in [grantee, someone]:
                self.factory.makeAccessArtifactGrant(
                    artifact=access_artifact,
                    grantee=person,
                    grantor=pillar.owner,
                )

        # Subscribe the grantee and other person to the artifacts.
        for person in [grantee, someone]:
            for bug in bugs or []:
                bug.subscribe(person, pillar.owner)
            for branch in branches or []:
                branch.subscribe(
                    person,
                    BranchSubscriptionNotificationLevel.NOEMAIL,
                    None,
                    CodeReviewNotificationLevel.NOEMAIL,
                    pillar.owner,
                )
            # XXX cjwatson 2015-02-05: subscribe to Git repositories when
            # implemented
            for spec in specifications or []:
                spec.subscribe(person)

        # Check that grantee has expected access grants.
        accessartifact_grant_source = getUtility(IAccessArtifactGrantSource)
        grants = accessartifact_grant_source.findByArtifact(
            access_artifacts, [grantee]
        )
        apgfs = getUtility(IAccessPolicyGrantFlatSource)
        self.assertEqual(1, grants.count())

        self.service.revokeAccessGrants(
            pillar,
            grantee,
            pillar.owner,
            bugs=bugs,
            branches=branches,
            gitrepositories=gitrepositories,
            specifications=specifications,
        )
        with block_on_job(self):
            transaction.commit()

        # The grantee now has no access to anything.
        permission_info = apgfs.findGranteePermissionsByPolicy(
            [policy], [grantee]
        )
        self.assertEqual(0, permission_info.count())

        # Check that the grantee's subscriptions have been removed.
        for bug in bugs or []:
            self.assertNotIn(grantee, bug.getDirectSubscribers())
        for branch in branches or []:
            self.assertNotIn(grantee, branch.subscribers)
        # XXX cjwatson 2015-02-05: check revocation of subscription to Git
        # repositories when implemented
        for spec in specifications or []:
            self.assertNotIn(grantee, spec.subscribers)

        # Someone else still has access to the artifacts.
        grants = accessartifact_grant_source.findByArtifact(
            access_artifacts, [someone]
        )
        self.assertEqual(1, grants.count())
        # Someone else still has subscriptions to the artifacts.
        for bug in bugs or []:
            self.assertIn(someone, bug.getDirectSubscribers())
        for branch in branches or []:
            self.assertIn(someone, branch.subscribers)
        # XXX cjwatson 2015-02-05: check subscription to Git repositories
        # when implemented
        for spec in specifications or []:
            self.assertIn(someone, spec.subscribers)

    def test_revokeAccessGrantsBugs(self):
        # Users with launchpad.Edit can delete all access for a grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        bug = self.factory.makeBug(
            target=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_revokeAccessGrants(pillar, [bug], None, None, None)

    def test_revokeAccessGrantsBranches(self):
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        branch = self._makeBranch(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_revokeAccessGrants(pillar, None, [branch], None, None)

    def test_revokeAccessGrantsGitRepositories(self):
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        gitrepository = self._makeGitRepository(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_revokeAccessGrants(
            pillar, None, None, [gitrepository], None
        )

    def test_revokeAccessGrantsSpecifications(self):
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        specification = self._makeSpecification(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.EMBARGOED,
        )
        self._assert_revokeAccessGrants(
            pillar, None, None, None, [specification]
        )

    def _assert_revokeTeamAccessGrants(
        self, pillar, bugs, branches, gitrepositories, specifications
    ):
        artifacts = []
        if bugs:
            artifacts.extend(bugs)
        if branches:
            artifacts.extend(branches)
        if gitrepositories:
            artifacts.extend(gitrepositories)
        if specifications:
            artifacts.extend(specifications)
        policy = self.factory.makeAccessPolicy(
            pillar=pillar, check_existing=True
        )

        person_grantee = self.factory.makePerson()
        team_owner = self.factory.makePerson()
        team_grantee = self.factory.makeTeam(
            owner=team_owner,
            membership_policy=TeamMembershipPolicy.RESTRICTED,
            members=[person_grantee],
            email="team@example.org",
        )

        # Subscribe the team and person grantees to the artifacts.
        for person in [team_grantee, person_grantee]:
            for bug in bugs or []:
                bug.subscribe(person, pillar.owner)
                # XXX 2012-06-12 wallyworld bug=1002596
                # No need to revoke AAG with triggers removed.
                if person == person_grantee:
                    accessartifact_source = getUtility(IAccessArtifactSource)
                    getUtility(IAccessArtifactGrantSource).revokeByArtifact(
                        accessartifact_source.find([bug]), [person_grantee]
                    )
            for branch in branches or []:
                branch.subscribe(
                    person,
                    BranchSubscriptionNotificationLevel.NOEMAIL,
                    None,
                    CodeReviewNotificationLevel.NOEMAIL,
                    pillar.owner,
                )
            # XXX cjwatson 2015-02-05: subscribe to Git repositories when
            # implemented
            # Subscribing somebody to a specification does not yet imply
            # granting access to this person.
            if specifications:
                self.service.ensureAccessGrants(
                    [person], pillar.owner, specifications=specifications
                )
            for spec in specifications or []:
                spec.subscribe(person)

        # Check that grantees have expected access grants and subscriptions.
        for person in [team_grantee, person_grantee]:
            artifacts = self.service.getVisibleArtifacts(
                person,
                bugs=bugs,
                branches=branches,
                gitrepositories=gitrepositories,
                specifications=specifications,
            )
            visible_bugs = artifacts["bugs"]
            visible_branches = artifacts["branches"]
            visible_specs = artifacts["specifications"]

            self.assertContentEqual(bugs or [], visible_bugs)
            self.assertContentEqual(branches or [], visible_branches)
            # XXX cjwatson 2015-02-05: check Git repositories when
            # subscription is implemented
            self.assertContentEqual(specifications or [], visible_specs)
        for person in [team_grantee, person_grantee]:
            for bug in bugs or []:
                self.assertIn(person, bug.getDirectSubscribers())

        self.service.revokeAccessGrants(
            pillar,
            team_grantee,
            pillar.owner,
            bugs=bugs,
            branches=branches,
            gitrepositories=gitrepositories,
            specifications=specifications,
        )
        with block_on_job(self):
            transaction.commit()

        # The grantees now have no access to anything.
        apgfs = getUtility(IAccessPolicyGrantFlatSource)
        permission_info = apgfs.findGranteePermissionsByPolicy(
            [policy], [team_grantee, person_grantee]
        )
        self.assertEqual(0, permission_info.count())

        # Check that the grantee's subscriptions have been removed.
        # Branches will be done once they have the information_type attribute.
        for person in [team_grantee, person_grantee]:
            for bug in bugs or []:
                self.assertNotIn(person, bug.getDirectSubscribers())
            artifacts = self.service.getVisibleArtifacts(
                person,
                bugs=bugs,
                branches=branches,
                gitrepositories=gitrepositories,
            )
            visible_bugs = artifacts["bugs"]
            visible_branches = artifacts["branches"]
            visible_gitrepositories = artifacts["gitrepositories"]
            visible_specs = artifacts["specifications"]

            self.assertContentEqual([], visible_bugs)
            self.assertContentEqual([], visible_branches)
            self.assertContentEqual([], visible_gitrepositories)
            self.assertContentEqual([], visible_specs)

    def test_revokeTeamAccessGrantsBugs(self):
        # Users with launchpad.Edit can delete all access for a grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        bug = self.factory.makeBug(
            target=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_revokeTeamAccessGrants(pillar, [bug], None, None, None)

    def test_revokeTeamAccessGrantsBranches(self):
        # Users with launchpad.Edit can delete all access for a grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        branch = self._makeBranch(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_revokeTeamAccessGrants(pillar, None, [branch], None, None)

    def test_revokeTeamAccessGrantsGitRepositories(self):
        # Users with launchpad.Edit can delete all access for a grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        gitrepository = self._makeGitRepository(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_revokeTeamAccessGrants(
            pillar, None, None, [gitrepository], None
        )

    def test_revokeTeamAccessGrantsSpecifications(self):
        # Users with launchpad.Edit can delete all access for a grantee.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        specification = self._makeSpecification(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.EMBARGOED,
        )
        self._assert_revokeTeamAccessGrants(
            pillar, None, None, None, [specification]
        )

    def _assert_revokeAccessGrantsUnauthorized(self):
        # revokeAccessGrants raises an Unauthorized exception if the user
        # is not permitted to do so.
        pillar = self._makePillar()
        bug = self.factory.makeBug(
            target=pillar, information_type=InformationType.USERDATA
        )
        grantee = self.factory.makePerson()
        self.assertRaises(
            Unauthorized,
            self.service.revokeAccessGrants,
            pillar,
            grantee,
            pillar.owner,
            bugs=[bug],
        )

    def test_revokeAccessGrantsAnonymous(self):
        # Anonymous users are not allowed.
        login(ANONYMOUS)
        self._assert_revokeAccessGrantsUnauthorized()

    def test_revokeAccessGrantsAnyone(self):
        # Unauthorized users are not allowed.
        login_person(self.factory.makePerson())
        self._assert_revokeAccessGrantsUnauthorized()

    def test_revokeAccessGrants_without_artifacts(self):
        # The revokeAccessGrants method raises a ValueError if called without
        # specifying any artifacts.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        grantee = self.factory.makePerson()
        login_person(owner)
        self.assertRaises(
            ValueError,
            self.service.revokeAccessGrants,
            pillar,
            grantee,
            pillar.owner,
        )

    def _assert_ensureAccessGrants(
        self,
        user,
        bugs,
        branches,
        gitrepositories,
        specifications,
        grantee=None,
    ):
        # Creating access grants works as expected.
        if not grantee:
            grantee = self.factory.makePerson()
        self.service.ensureAccessGrants(
            [grantee],
            user,
            bugs=bugs,
            branches=branches,
            gitrepositories=gitrepositories,
            specifications=specifications,
        )

        # Check that grantee has expected access grants.
        shared_bugs = []
        shared_branches = []
        shared_gitrepositories = []
        shared_specifications = []
        all_pillars = []
        for bug in bugs or []:
            all_pillars.extend(bug.affected_bug_target_parents)
        for branch in branches or []:
            context = branch.target.context
            if ISourcePackage.providedBy(context):
                context = context.distribution
            all_pillars.append(context)
        for gitrepository in gitrepositories or []:
            context = gitrepository.target
            if IDistributionSourcePackage.providedBy(context):
                context = context.distribution
            all_pillars.append(context)
        for specification in specifications or []:
            all_pillars.append(specification.target)
        policies = getUtility(IAccessPolicySource).findByPillar(all_pillars)

        apgfs = getUtility(IAccessPolicyGrantFlatSource)
        access_artifacts = apgfs.findArtifactsByGrantee(grantee, policies)
        for a in access_artifacts:
            if IBug.providedBy(a.concrete_artifact):
                shared_bugs.append(a.concrete_artifact)
            elif IBranch.providedBy(a.concrete_artifact):
                shared_branches.append(a.concrete_artifact)
            elif IGitRepository.providedBy(a.concrete_artifact):
                shared_gitrepositories.append(a.concrete_artifact)
            elif ISpecification.providedBy(a.concrete_artifact):
                shared_specifications.append(a.concrete_artifact)
        self.assertContentEqual(bugs or [], shared_bugs)
        self.assertContentEqual(branches or [], shared_branches)
        self.assertContentEqual(gitrepositories or [], shared_gitrepositories)
        self.assertContentEqual(specifications or [], shared_specifications)

    def test_ensureAccessGrantsBugs(self):
        # Access grants can be created for bugs.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        bug = self.factory.makeBug(
            target=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_ensureAccessGrants(owner, [bug], None, None, None)

    def test_ensureAccessGrantsBranches(self):
        # Access grants can be created for branches.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        branch = self._makeBranch(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_ensureAccessGrants(owner, None, [branch], None, None)

    def test_ensureAccessGrantsGitRepositories(self):
        # Access grants can be created for Git repositories.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        gitrepository = self._makeGitRepository(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        self._assert_ensureAccessGrants(
            owner, None, None, [gitrepository], None
        )

    def test_ensureAccessGrantsSpecifications(self):
        # Access grants can be created for branches.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        specification = self._makeSpecification(pillar=pillar, owner=owner)
        removeSecurityProxy(specification.target)._ensurePolicies(
            [InformationType.PROPRIETARY]
        )
        with person_logged_in(owner):
            specification.transitionToInformationType(
                InformationType.PROPRIETARY, owner
            )
        self._assert_ensureAccessGrants(
            owner, None, None, None, [specification]
        )

    def test_ensureAccessGrantsExisting(self):
        # Any existing access grants are retained and new ones created.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        login_person(owner)
        bug = self.factory.makeBug(
            target=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        bug2 = self.factory.makeBug(
            target=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        # Create an existing access grant.
        grantee = self.factory.makePerson()
        self.service.ensureAccessGrants([grantee], owner, bugs=[bug])
        # Test with a new bug as well as the one for which access is already
        # granted.
        self._assert_ensureAccessGrants(
            owner, [bug, bug2], None, None, None, grantee
        )

    def _assert_ensureAccessGrantsUnauthorized(self, user):
        # ensureAccessGrants raises an Unauthorized exception if the user
        # is not permitted to do so.
        pillar = self._makePillar()
        bug = self.factory.makeBug(
            target=pillar, information_type=InformationType.USERDATA
        )
        grantee = self.factory.makePerson()
        self.assertRaises(
            Unauthorized,
            self.service.ensureAccessGrants,
            [grantee],
            user,
            bugs=[bug],
        )

    def test_ensureAccessGrantsAnonymous(self):
        # Anonymous users are not allowed.
        login(ANONYMOUS)
        self._assert_ensureAccessGrantsUnauthorized(ANONYMOUS)

    def test_ensureAccessGrantsAnyone(self):
        # Unauthorized users are not allowed.
        anyone = self.factory.makePerson()
        login_person(anyone)
        self._assert_ensureAccessGrantsUnauthorized(anyone)

    def test_updatePillarBugSharingPolicy(self):
        # updatePillarSharingPolicies works for bugs.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        self.factory.makeCommercialSubscription(pillar)
        login_person(owner)
        self.service.updatePillarSharingPolicies(
            pillar, bug_sharing_policy=BugSharingPolicy.PROPRIETARY
        )
        self.assertEqual(
            BugSharingPolicy.PROPRIETARY, pillar.bug_sharing_policy
        )

    def test_updatePillarBranchSharingPolicy(self):
        # updatePillarSharingPolicies works for branches.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        self.factory.makeCommercialSubscription(pillar)
        login_person(owner)
        self.service.updatePillarSharingPolicies(
            pillar, branch_sharing_policy=BranchSharingPolicy.PROPRIETARY
        )
        self.assertEqual(
            BranchSharingPolicy.PROPRIETARY, pillar.branch_sharing_policy
        )

    def test_updatePillarSpecificationSharingPolicy(self):
        # updatePillarSharingPolicies works for specifications.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        self.factory.makeCommercialSubscription(pillar)
        login_person(owner)
        self.service.updatePillarSharingPolicies(
            pillar,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PROPRIETARY
            ),
        )
        self.assertEqual(
            SpecificationSharingPolicy.PROPRIETARY,
            pillar.specification_sharing_policy,
        )

    def _assert_updatePillarSharingPoliciesUnauthorized(self, user):
        # updatePillarSharingPolicies raises an Unauthorized exception if the
        # user is not permitted to do so.
        pillar = self._makePillar()
        self.assertRaises(
            Unauthorized,
            self.service.updatePillarSharingPolicies,
            pillar,
            BranchSharingPolicy.PUBLIC,
            BugSharingPolicy.PUBLIC,
            SpecificationSharingPolicy.PUBLIC,
        )

    def test_updatePillarSharingPoliciesAnonymous(self):
        # Anonymous users are not allowed.
        login(ANONYMOUS)
        self._assert_updatePillarSharingPoliciesUnauthorized(ANONYMOUS)

    def test_updatePillarSharingPoliciesAnyone(self):
        # Unauthorized users are not allowed.
        anyone = self.factory.makePerson()
        login_person(anyone)
        self._assert_updatePillarSharingPoliciesUnauthorized(anyone)

    def create_shared_artifacts(self, pillar, grantee, user):
        # Create some shared bugs, branches, Git repositories, snaps,
        # specifications, ocirecipes, and vulnerabilities.
        bugs = []
        bug_tasks = []
        for _ in range(0, 10):
            bug = self.factory.makeBug(
                target=pillar,
                owner=pillar.owner,
                information_type=InformationType.USERDATA,
            )
            bugs.append(bug)
            bug_tasks.append(bug.default_bugtask)
        branches = []
        for _ in range(0, 10):
            branch = self._makeBranch(
                pillar=pillar,
                owner=pillar.owner,
                information_type=InformationType.USERDATA,
            )
            branches.append(branch)
        gitrepositories = []
        for _ in range(0, 10):
            gitrepository = self._makeGitRepository(
                pillar=pillar,
                owner=pillar.owner,
                information_type=InformationType.USERDATA,
            )
            gitrepositories.append(gitrepository)
        snaps = []
        if IProduct.providedBy(pillar):
            for _ in range(0, 10):
                snap = self.factory.makeSnap(
                    project=pillar,
                    owner=pillar.owner,
                    registrant=pillar.owner,
                    information_type=InformationType.USERDATA,
                )
                snaps.append(snap)
        specs = []
        for _ in range(0, 10):
            spec = self._makeSpecification(
                pillar=pillar,
                owner=pillar.owner,
                information_type=InformationType.PROPRIETARY,
            )
            specs.append(spec)
        ocirecipes = []
        for _ in range(0, 10):
            ociproject = self.factory.makeOCIProject(
                pillar=pillar, registrant=pillar.owner
            )
            ocirecipe = self.factory.makeOCIRecipe(
                oci_project=ociproject,
                owner=pillar.owner,
                registrant=pillar.owner,
                information_type=InformationType.USERDATA,
            )
            ocirecipes.append(ocirecipe)
        vulnerabilities = []
        if self.isPillarADistribution():
            for _ in range(10):
                vulnerability = self.factory.makeVulnerability(
                    distribution=pillar,
                    information_type=InformationType.PROPRIETARY,
                )
                vulnerabilities.append(vulnerability)

        # Grant access to grantee as well as the person who will be doing the
        # query. The person who will be doing the query is not granted access
        # to the last bug/branch so those won't be in the result.
        def grant_access(artifact, grantee_only):
            access_artifact = self.factory.makeAccessArtifact(
                concrete=artifact
            )
            self.factory.makeAccessArtifactGrant(
                artifact=access_artifact, grantee=grantee, grantor=pillar.owner
            )
            if not grantee_only:
                self.factory.makeAccessArtifactGrant(
                    artifact=access_artifact,
                    grantee=user,
                    grantor=pillar.owner,
                )

        for i, bug in enumerate(bugs):
            grant_access(bug, i == 9)
        for i, branch in enumerate(branches):
            grant_access(branch, i == 9)
        for i, gitrepository in enumerate(gitrepositories):
            grant_access(gitrepository, i == 9)
        if snaps:
            getUtility(IService, "sharing").ensureAccessGrants(
                [grantee], pillar.owner, snaps=snaps[:9]
            )
        getUtility(IService, "sharing").ensureAccessGrants(
            [grantee], pillar.owner, specifications=specs[:9]
        )
        getUtility(IService, "sharing").ensureAccessGrants(
            [grantee], pillar.owner, ocirecipes=ocirecipes[:9]
        )
        if vulnerabilities:
            getUtility(IService, "sharing").ensureAccessGrants(
                [grantee], pillar.owner, vulnerabilities=vulnerabilities[:9]
            )
        return (
            bug_tasks,
            branches,
            gitrepositories,
            snaps,
            specs,
            ocirecipes,
            vulnerabilities,
        )

    def test_getSharedArtifacts(self):
        # Test the getSharedArtifacts method.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        (
            bug_tasks,
            branches,
            gitrepositories,
            snaps,
            specs,
            ocirecipes,
            vulnerabilities,
        ) = self.create_shared_artifacts(pillar, grantee, user)

        # Check the results.
        artifacts = self.service.getSharedArtifacts(pillar, grantee, user)
        shared_bugtasks = artifacts["bugtasks"]
        shared_branches = artifacts["branches"]
        shared_gitrepositories = artifacts["gitrepositories"]
        shared_snaps = artifacts["snaps"]
        shared_specs = artifacts["specifications"]
        shared_ocirecipes = artifacts["ocirecipes"]
        shared_vulnerabilities = artifacts["vulnerabilities"]

        self.assertContentEqual(bug_tasks[:9], shared_bugtasks)
        self.assertContentEqual(branches[:9], shared_branches)
        self.assertContentEqual(gitrepositories[:9], shared_gitrepositories)
        self.assertContentEqual(snaps[:9], shared_snaps)
        self.assertContentEqual(specs[:9], shared_specs)
        self.assertContentEqual(ocirecipes[:9], shared_ocirecipes)
        if self.isPillarADistribution():
            self.assertContentEqual(
                vulnerabilities[:9], shared_vulnerabilities
            )

    def _assert_getSharedPillars(self, pillar, who=None):
        # Test that 'who' can query the shared pillars for a grantee.

        # Make a pillar not related to 'who' which will be shared.
        unrelated_pillar = self._makePillar()
        # Make an unshared pillar.
        self._makePillar()
        person = self.factory.makePerson()
        # Include more than one permission to ensure distinct works.
        permissions = {
            InformationType.PRIVATESECURITY: SharingPermission.ALL,
            InformationType.USERDATA: SharingPermission.ALL,
        }
        with person_logged_in(pillar.owner):
            self.service.sharePillarInformation(
                pillar, person, pillar.owner, permissions
            )
        with person_logged_in(unrelated_pillar.owner):
            self.service.sharePillarInformation(
                unrelated_pillar, person, unrelated_pillar.owner, permissions
            )
        shared = getattr(self.service, self.get_shared_pillars_name)(
            person, who
        )
        expected = []
        if who:
            expected = [pillar]
            if IPersonRoles(who).in_admin:
                expected.append(unrelated_pillar)
        self.assertContentEqual(expected, shared)

    def test_getSharedPillars_anonymous(self):
        # Anonymous users don't get to see any shared pillars.
        pillar = self._makePillar()
        self._assert_getSharedPillars(pillar)

    def test_getSharedPillars_admin(self):
        # Admins can see all shared pillars.
        admin = getUtility(ILaunchpadCelebrities).admin.teamowner
        pillar = self._makePillar()
        self._assert_getSharedPillars(pillar, admin)

    def test_getSharedPillars_commercial_admin_current(self):
        # Commercial admins can see all current commercial pillars.
        admin = getUtility(ILaunchpadCelebrities).commercial_admin.teamowner
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar)
        self._assert_getSharedPillars(pillar, admin)

    def test_getSharedPillars_commercial_admin_expired(self):
        # Commercial admins can see all expired commercial pillars.
        admin = getUtility(ILaunchpadCelebrities).commercial_admin.teamowner
        pillar = self._makePillar()
        self.factory.makeCommercialSubscription(pillar, expired=True)
        self._assert_getSharedPillars(pillar, admin)

    def test_getSharedPillars_commercial_admin_owner(self):
        # Commercial admins can see pillars they own.
        admin = getUtility(ILaunchpadCelebrities).commercial_admin
        pillar = self._makePillar(owner=admin)
        self._assert_getSharedPillars(pillar, admin.teamowner)

    def test_getSharedPillars_commercial_admin_driver(self):
        # Commercial admins can see pillars they are the driver for.
        admin = getUtility(ILaunchpadCelebrities).commercial_admin
        pillar = self._makePillar(driver=admin)
        self._assert_getSharedPillars(pillar, admin.teamowner)

    def test_getSharedPillars_owner(self):
        # Users only see shared pillars they own.
        owner_team = self.factory.makeTeam(
            membership_policy=TeamMembershipPolicy.MODERATED
        )
        pillar = self._makePillar(owner=owner_team)
        self._assert_getSharedPillars(pillar, owner_team.teamowner)

    def test_getSharedPillars_driver(self):
        # Users only see shared pillars they are the driver for.
        driver_team = self.factory.makeTeam()
        pillar = self._makePillar(driver=driver_team)
        self._assert_getSharedPillars(pillar, driver_team.teamowner)

    def test_getSharedBugs(self):
        # Test the getSharedBugs method.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        bug_tasks, _, _, _, _, _, _ = self.create_shared_artifacts(
            pillar, grantee, user
        )

        # Check the results.
        shared_bugtasks = self.service.getSharedBugs(pillar, grantee, user)
        self.assertContentEqual(bug_tasks[:9], shared_bugtasks)

    def test_getSharedBranches(self):
        # Test the getSharedBranches method.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        _, branches, _, _, _, _, _ = self.create_shared_artifacts(
            pillar, grantee, user
        )

        # Check the results.
        shared_branches = self.service.getSharedBranches(pillar, grantee, user)
        self.assertContentEqual(branches[:9], shared_branches)

    def test_getSharedGitRepositories(self):
        # Test the getSharedGitRepositories method.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        _, _, gitrepositories, _, _, _, _ = self.create_shared_artifacts(
            pillar, grantee, user
        )

        # Check the results.
        shared_gitrepositories = self.service.getSharedGitRepositories(
            pillar, grantee, user
        )
        self.assertContentEqual(gitrepositories[:9], shared_gitrepositories)

    def test_getSharedSnaps(self):
        # Test the getSharedSnaps method.
        self._skipUnlessProduct()
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        _, _, _, snaps, _, _, _ = self.create_shared_artifacts(
            pillar, grantee, user
        )

        # Check the results.
        shared_snaps = self.service.getSharedSnaps(pillar, grantee, user)
        self.assertContentEqual(snaps[:9], shared_snaps)

    def test_getSharedSpecifications(self):
        # Test the getSharedSpecifications method.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        _, _, _, _, specifications, _, _ = self.create_shared_artifacts(
            pillar, grantee, user
        )

        # Check the results.
        shared_specifications = self.service.getSharedSpecifications(
            pillar, grantee, user
        )
        self.assertContentEqual(specifications[:9], shared_specifications)

    def test_getSharedOCIRecipes(self):
        # Test the getSharedSnaps method.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        (
            _,
            _,
            _,
            _,
            _,
            ocirecipes,
            _,
        ) = self.create_shared_artifacts(pillar, grantee, user)

        # Check the results.
        shared_ocirecipes = self.service.getSharedOCIRecipes(
            pillar, grantee, user
        )
        self.assertContentEqual(ocirecipes[:9], shared_ocirecipes)

    def test_getSharedVulnerabilities(self):
        # Test the getSharedVulnerabilities method.
        self._skipUnlessDistribution()
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        login_person(owner)
        grantee = self.factory.makePerson()
        user = self.factory.makePerson()
        (
            _,
            _,
            _,
            _,
            _,
            _,
            vulnerabilities,
        ) = self.create_shared_artifacts(pillar, grantee, user)

        # Check the results.
        shared_vulnerabilities = self.service.getSharedVulnerabilities(
            pillar, grantee, user
        )
        self.assertContentEqual(vulnerabilities[:9], shared_vulnerabilities)

    def test_getPeopleWithAccessBugs(self):
        # Test the getPeopleWithoutAccess method with bugs.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        bug = self.factory.makeBug(
            target=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        login_person(owner)
        self._assert_getPeopleWithoutAccess(pillar, bug)

    def test_getPeopleWithAccessBranches(self):
        # Test the getPeopleWithoutAccess method with branches.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        branch = self._makeBranch(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        login_person(owner)
        self._assert_getPeopleWithoutAccess(pillar, branch)

    def test_getPeopleWithAccessGitRepositories(self):
        # Test the getPeopleWithoutAccess method with Git repositories.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        gitrepository = self._makeGitRepository(
            pillar=pillar,
            owner=owner,
            information_type=InformationType.USERDATA,
        )
        login_person(owner)
        self._assert_getPeopleWithoutAccess(pillar, gitrepository)

    def _assert_getPeopleWithoutAccess(self, pillar, artifact):
        access_artifact = self.factory.makeAccessArtifact(concrete=artifact)
        # Make some people to check. people[:5] will not have access.
        people = []
        # Make a team with access.
        member_with_access = self.factory.makePerson()
        team_with_access = self.factory.makeTeam(members=[member_with_access])
        # Make a team without access.
        team_without_access = self.factory.makeTeam(
            members=[member_with_access]
        )
        people.append(team_without_access)
        for _ in range(0, 10):
            person = self.factory.makePerson()
            people.append(person)
        people.append(team_with_access)
        people.append(member_with_access)

        # Create some access policy grants.
        [policy] = getUtility(IAccessPolicySource).find(
            [(pillar, InformationType.USERDATA)]
        )
        for person in people[5:7]:
            self.factory.makeAccessPolicyGrant(
                policy=policy, grantee=person, grantor=pillar.owner
            )
        # And some access artifact grants.
        for person in people[7:]:
            self.factory.makeAccessArtifactGrant(
                artifact=access_artifact, grantee=person, grantor=pillar.owner
            )

        # Check the results.
        without_access = self.service.getPeopleWithoutAccess(artifact, people)
        self.assertContentEqual(people[:5], without_access)

    def _make_Artifacts(self):
        # Make artifacts for test (in)visible artifact methods.
        owner = self.factory.makePerson()
        pillar = self._makePillar(
            owner=owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        grantee = self.factory.makePerson()
        login_person(owner)

        bugs = []
        for _ in range(0, 10):
            bug = self.factory.makeBug(
                target=pillar,
                owner=owner,
                information_type=InformationType.USERDATA,
            )
            bugs.append(bug)
        branches = []
        for _ in range(0, 10):
            branch = self._makeBranch(
                pillar=pillar,
                owner=owner,
                information_type=InformationType.USERDATA,
            )
            branches.append(branch)
        gitrepositories = []
        for _ in range(0, 10):
            gitrepository = self._makeGitRepository(
                pillar=pillar,
                owner=owner,
                information_type=InformationType.USERDATA,
            )
            gitrepositories.append(gitrepository)
        specifications = []
        for _ in range(0, 10):
            spec = self._makeSpecification(
                pillar=pillar,
                owner=owner,
                information_type=InformationType.PROPRIETARY,
            )
            specifications.append(spec)

        def grant_access(artifact):
            access_artifact = self.factory.makeAccessArtifact(
                concrete=artifact
            )
            self.factory.makeAccessArtifactGrant(
                artifact=access_artifact, grantee=grantee, grantor=owner
            )
            return access_artifact

        # Grant access to some of the artifacts.
        for bug in bugs[:5]:
            grant_access(bug)
        for branch in branches[:5]:
            grant_access(branch)
        for gitrepository in gitrepositories[:5]:
            grant_access(gitrepository)
        for spec in specifications[:5]:
            grant_access(spec)
        return grantee, owner, bugs, branches, gitrepositories, specifications

    def test_getVisibleArtifacts(self):
        # Test the getVisibleArtifacts method.
        (
            grantee,
            ignore,
            bugs,
            branches,
            gitrepositories,
            specs,
        ) = self._make_Artifacts()
        # Check the results.
        artifacts = self.service.getVisibleArtifacts(
            grantee,
            bugs=bugs,
            branches=branches,
            gitrepositories=gitrepositories,
            specifications=specs,
        )
        shared_bugs = artifacts["bugs"]
        shared_branches = artifacts["branches"]
        shared_gitrepositories = artifacts["gitrepositories"]
        shared_specs = artifacts["specifications"]

        self.assertContentEqual(bugs[:5], shared_bugs)
        self.assertContentEqual(branches[:5], shared_branches)
        self.assertContentEqual(gitrepositories[:5], shared_gitrepositories)
        self.assertContentEqual(specs[:5], shared_specs)

    def test_getVisibleArtifacts_grant_on_pillar(self):
        # getVisibleArtifacts() returns private specifications if
        # user has a policy grant for the pillar of the specification.
        (
            _,
            owner,
            bugs,
            branches,
            gitrepositories,
            specs,
        ) = self._make_Artifacts()
        artifacts = self.service.getVisibleArtifacts(
            owner,
            bugs=bugs,
            branches=branches,
            gitrepositories=gitrepositories,
            specifications=specs,
        )
        shared_bugs = artifacts["bugs"]
        shared_branches = artifacts["branches"]
        shared_gitrepositories = artifacts["gitrepositories"]
        shared_specs = artifacts["specifications"]

        self.assertContentEqual(bugs, shared_bugs)
        self.assertContentEqual(branches, shared_branches)
        self.assertContentEqual(gitrepositories, shared_gitrepositories)
        self.assertContentEqual(specs, shared_specs)

    def test_getInvisibleArtifacts(self):
        # Test the getInvisibleArtifacts method.
        (
            grantee,
            ignore,
            bugs,
            branches,
            gitrepositories,
            specs,
        ) = self._make_Artifacts()
        # Check the results.
        (
            not_shared_bugs,
            not_shared_branches,
            not_shared_gitrepositories,
        ) = self.service.getInvisibleArtifacts(
            grantee,
            bugs=bugs,
            branches=branches,
            gitrepositories=gitrepositories,
        )
        self.assertContentEqual(bugs[5:], not_shared_bugs)
        self.assertContentEqual(branches[5:], not_shared_branches)
        self.assertContentEqual(
            gitrepositories[5:], not_shared_gitrepositories
        )

    def _assert_getVisibleArtifacts_bug_change(self, change_callback):
        # Test the getVisibleArtifacts method excludes bugs after a change of
        # information_type or bugtask re-targetting.
        owner = self.factory.makePerson()
        pillar = self._makePillar(owner=owner)
        grantee = self.factory.makePerson()
        login_person(owner)

        [policy] = getUtility(IAccessPolicySource).find(
            [(pillar, InformationType.USERDATA)]
        )
        self.factory.makeAccessPolicyGrant(
            policy, grantee=grantee, grantor=owner
        )

        bugs = []
        for _ in range(0, 10):
            bug = self.factory.makeBug(
                target=pillar,
                owner=owner,
                information_type=InformationType.USERDATA,
            )
            bugs.append(bug)

        artifacts = self.service.getVisibleArtifacts(grantee, bugs=bugs)
        shared_bugs = artifacts["bugs"]
        self.assertContentEqual(bugs, shared_bugs)

        # Change some bugs.
        for x in range(0, 5):
            change_callback(bugs[x], owner)
        # Check the results.
        artifacts = self.service.getVisibleArtifacts(grantee, bugs=bugs)
        self.assertContentEqual(bugs[5:], artifacts["bugs"])

    def test_getVisibleArtifacts_bug_policy_change(self):
        # getVisibleArtifacts excludes bugs after change of information type.
        def change_info_type(bug, owner):
            bug.transitionToInformationType(
                InformationType.PRIVATESECURITY, owner
            )

        self._assert_getVisibleArtifacts_bug_change(change_info_type)

    def test_getVisibleArtifacts_bugtask_retarget(self):
        # Test the getVisibleArtifacts method excludes items after a bugtask
        # is re-targetted to a new pillar.
        another_pillar = self._makePillar()

        def retarget_bugtask(bug, owner):
            bug.default_bugtask.transitionToTarget(another_pillar, owner)

        self._assert_getVisibleArtifacts_bug_change(retarget_bugtask)

    def test_checkPillarAccess(self):
        # checkPillarAccess checks whether the user has full access to
        # an information type.
        pillar = self._makePillar()
        right_person = self.factory.makePerson()
        right_team = self.factory.makeTeam(members=[right_person])
        wrong_person = self.factory.makePerson()
        with admin_logged_in():
            self.service.sharePillarInformation(
                pillar,
                right_team,
                pillar.owner,
                {InformationType.USERDATA: SharingPermission.ALL},
            )
            self.service.sharePillarInformation(
                pillar,
                wrong_person,
                pillar.owner,
                {InformationType.PRIVATESECURITY: SharingPermission.ALL},
            )
        self.assertFalse(
            self.service.checkPillarAccess(
                [pillar], InformationType.USERDATA, wrong_person
            )
        )
        self.assertTrue(
            self.service.checkPillarAccess(
                [pillar], InformationType.USERDATA, right_person
            )
        )

    def test_checkPillarAccess_no_policy(self):
        # checkPillarAccess returns False if there's no policy.
        self.assertFalse(
            self.service.checkPillarAccess(
                [self._makePillar()],
                InformationType.PUBLIC,
                self.factory.makePerson(),
            )
        )

    def test_getAccessPolicyGrantCounts(self):
        # checkPillarAccess checks whether the user has full access to
        # an information type.
        pillar = self._makePillar()
        grantee = self.factory.makePerson()
        with admin_logged_in():
            self.service.sharePillarInformation(
                pillar,
                grantee,
                pillar.owner,
                {InformationType.USERDATA: SharingPermission.ALL},
            )
        # The owner is granted access on pillar creation. So we need to allow
        # for that in the check below.
        self.assertContentEqual(
            [
                (InformationType.PRIVATESECURITY, 1),
                (InformationType.USERDATA, 2),
            ],
            self.service.getAccessPolicyGrantCounts(pillar),
        )

    def test_getAccessPolicyGrantCountsZero(self):
        # checkPillarAccess checks whether the user has full access to
        # an information type.
        pillar = self._makePillar()
        with admin_logged_in():
            self.service.deletePillarGrantee(
                pillar, pillar.owner, pillar.owner
            )
        self.assertContentEqual(
            [
                (InformationType.PRIVATESECURITY, 0),
                (InformationType.USERDATA, 0),
            ],
            self.service.getAccessPolicyGrantCounts(pillar),
        )


class ApiTestMixin(PillarScenariosMixin):
    """Common tests for launchpadlib and webservice."""

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson(name="thundercat")
        self.pillar = self._makePillar(
            owner=self.owner,
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
        )
        self.grantee = self.factory.makePerson(name="grantee")
        self.grantor = self.factory.makePerson()
        self.grantee_uri = canonical_url(self.grantee, force_local_path=True)
        self.grantor_uri = canonical_url(self.grantor, force_local_path=True)
        self.bug = self.factory.makeBug(
            owner=self.owner,
            target=self.pillar,
            information_type=InformationType.PRIVATESECURITY,
        )
        self.branch = self._makeBranch(
            owner=self.owner,
            pillar=self.pillar,
            information_type=InformationType.PRIVATESECURITY,
        )
        self.gitrepository = self._makeGitRepository(
            owner=self.owner,
            pillar=self.pillar,
            information_type=InformationType.PRIVATESECURITY,
        )
        self.spec = self._makeSpecification(
            pillar=self.pillar,
            owner=self.owner,
            information_type=InformationType.PROPRIETARY,
        )
        login_person(self.owner)
        self.bug.subscribe(self.grantee, self.owner)
        self.branch.subscribe(
            self.grantee,
            BranchSubscriptionNotificationLevel.NOEMAIL,
            None,
            CodeReviewNotificationLevel.NOEMAIL,
            self.owner,
        )
        # XXX cjwatson 2015-02-05: subscribe to Git repository when implemented
        getUtility(IService, "sharing").ensureAccessGrants(
            [self.grantee], self.grantor, gitrepositories=[self.gitrepository]
        )
        getUtility(IService, "sharing").ensureAccessGrants(
            [self.grantee], self.grantor, specifications=[self.spec]
        )
        transaction.commit()

    def test_getPillarGranteeData(self):
        # Test the getPillarGranteeData method.
        json_data = self._getPillarGranteeData()
        [grantee_data] = [d for d in json_data if d["name"] != "thundercat"]
        self.assertEqual("grantee", grantee_data["name"])
        self.assertEqual(
            {
                InformationType.USERDATA.name: SharingPermission.ALL.name,
                InformationType.PRIVATESECURITY.name: (
                    SharingPermission.SOME.name
                ),
                InformationType.PROPRIETARY.name: SharingPermission.SOME.name,
            },
            grantee_data["permissions"],
        )


class TestWebService(ApiTestMixin, WebServiceTestCase):
    """Test the web service interface for the Sharing Service."""

    def setUp(self):
        super().setUp()
        self.webservice = LaunchpadWebServiceCaller(
            "launchpad-library", "salgado-change-anything"
        )
        self._sharePillarInformation(self.pillar)

    def test_url(self):
        # Test that the url for the service is correct.
        service = SharingService()
        root_app = getUtility(ILaunchpadRoot)
        self.assertEqual(
            "%s+services/sharing" % canonical_url(root_app),
            canonical_url(service),
        )

    def _named_get(self, api_method, **kwargs):
        return self.webservice.named_get(
            "/+services/sharing", api_method, api_version="devel", **kwargs
        ).jsonBody()

    def _named_post(self, api_method, **kwargs):
        return self.webservice.named_post(
            "/+services/sharing", api_method, api_version="devel", **kwargs
        ).jsonBody()

    def _getPillarGranteeData(self):
        pillar_uri = canonical_url(
            removeSecurityProxy(self.pillar), force_local_path=True
        )
        return self._named_get("getPillarGranteeData", pillar=pillar_uri)

    def _sharePillarInformation(self, pillar):
        pillar_uri = canonical_url(
            removeSecurityProxy(pillar), force_local_path=True
        )
        return self._named_post(
            "sharePillarInformation",
            pillar=pillar_uri,
            grantee=self.grantee_uri,
            user=self.grantor_uri,
            permissions={
                InformationType.USERDATA.title: SharingPermission.ALL.title
            },
        )


class TestLaunchpadlib(ApiTestMixin, TestCaseWithFactory):
    """Test launchpadlib access for the Sharing Service."""

    layer = AppServerLayer

    def setUp(self):
        super().setUp()
        self.launchpad = self.factory.makeLaunchpadService(person=self.owner)
        self.service = self.launchpad.load("+services/sharing")
        transaction.commit()
        self._sharePillarInformation(self.pillar)

    def _getPillarGranteeData(self):
        ws_pillar = ws_object(self.launchpad, self.pillar)
        return self.service.getPillarGranteeData(pillar=ws_pillar)

    def _sharePillarInformation(self, pillar):
        ws_pillar = ws_object(self.launchpad, pillar)
        ws_grantee = ws_object(self.launchpad, self.grantee)
        return self.service.sharePillarInformation(
            pillar=ws_pillar,
            grantee=ws_grantee,
            permissions={
                InformationType.USERDATA.title: SharingPermission.ALL.title
            },
        )

    def test_getSharedPillars(self):
        # Test the exported getSharedProjects() or getSharedDistributions()
        # method (depending on the test scenario).
        ws_grantee = ws_object(self.launchpad, self.grantee)
        pillars = getattr(self.service, self.get_shared_pillars_name)(
            person=ws_grantee
        )
        self.assertEqual(1, len(pillars))
        self.assertEqual(pillars[0].name, self.pillar.name)

    def test_getSharedBugs(self):
        # Test the exported getSharedBugs() method.
        ws_pillar = ws_object(self.launchpad, self.pillar)
        ws_grantee = ws_object(self.launchpad, self.grantee)
        bugtasks = self.service.getSharedBugs(
            pillar=ws_pillar, person=ws_grantee
        )
        self.assertEqual(1, len(bugtasks))
        self.assertEqual(bugtasks[0].title, self.bug.default_bugtask.title)

    def test_getSharedBranches(self):
        # Test the exported getSharedBranches() method.
        ws_pillar = ws_object(self.launchpad, self.pillar)
        ws_grantee = ws_object(self.launchpad, self.grantee)
        branches = self.service.getSharedBranches(
            pillar=ws_pillar, person=ws_grantee
        )
        self.assertEqual(1, len(branches))
        self.assertEqual(branches[0].unique_name, self.branch.unique_name)

    def test_getSharedGitRepositories(self):
        # Test the exported getSharedGitRepositories() method.
        ws_pillar = ws_object(self.launchpad, self.pillar)
        ws_grantee = ws_object(self.launchpad, self.grantee)
        gitrepositories = self.service.getSharedGitRepositories(
            pillar=ws_pillar, person=ws_grantee
        )
        self.assertEqual(1, len(gitrepositories))
        self.assertEqual(
            gitrepositories[0].unique_name, self.gitrepository.unique_name
        )

    def test_getSharedSpecifications(self):
        # Test the exported getSharedSpecifications() method.
        ws_pillar = ws_object(self.launchpad, self.pillar)
        ws_grantee = ws_object(self.launchpad, self.grantee)
        specifications = self.service.getSharedSpecifications(
            pillar=ws_pillar, person=ws_grantee
        )
        self.assertEqual(1, len(specifications))
        self.assertEqual(specifications[0].name, self.spec.name)


load_tests = load_tests_apply_scenarios
