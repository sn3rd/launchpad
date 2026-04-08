# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for Distribution."""

import json
from datetime import datetime, timedelta, timezone

import soupmatchers
from fixtures import FakeLogger
from lazr.lifecycle.snapshot import Snapshot
from storm.store import Store
from testtools import ExpectedException
from testtools.matchers import (
    ContainsDict,
    Equals,
    Is,
    IsInstance,
    MatchesAll,
    MatchesAny,
    MatchesStructure,
    Not,
)
from zope.component import getUtility
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.enums import (
    FREE_INFORMATION_TYPES,
    PILLAR_INFORMATION_TYPES,
    InformationType,
    ServiceUsage,
)
from lp.app.errors import NotFoundError, ServiceUsageForbidden
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.interfaces.services import IService
from lp.blueprints.model.specification import (
    SPECIFICATION_POLICY_ALLOWED_TYPES,
)
from lp.bugs.enums import VulnerabilityStatus
from lp.bugs.interfaces.bugtarget import BUG_POLICY_ALLOWED_TYPES
from lp.bugs.interfaces.bugtargetparent import IBugTargetParent
from lp.bugs.interfaces.bugtask import BugTaskImportance
from lp.bugs.model.tests.test_vulnerability import (
    grant_access_to_non_public_vulnerability,
)
from lp.code.model.branchnamespace import BRANCH_POLICY_ALLOWED_TYPES
from lp.oci.tests.helpers import OCIConfigHelperMixin
from lp.registry.enums import (
    EXCLUSIVE_TEAM_POLICY,
    INCLUSIVE_TEAM_POLICY,
    BranchSharingPolicy,
    BugSharingPolicy,
    DistributionDefaultTraversalPolicy,
    SpecificationSharingPolicy,
    TeamMembershipPolicy,
)
from lp.registry.errors import (
    CannotChangeInformationType,
    CommercialSubscribersOnly,
    InclusiveTeamLinkageError,
    NoSuchDistroSeries,
    ProprietaryPillar,
)
from lp.registry.interfaces.accesspolicy import (
    IAccessPolicyGrantSource,
    IAccessPolicySource,
)
from lp.registry.interfaces.distribution import IDistribution, IDistributionSet
from lp.registry.interfaces.distributionmirror import MirrorContent
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.interfaces.oopsreferences import IHasOOPSReferences
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.model.distribution import Distribution
from lp.registry.tests.test_distroseries import CurrentSourceReleasesMixin
from lp.services.librarianserver.testing.fake import FakeLibrarian
from lp.services.propertycache import clear_property_cache, get_property_cache
from lp.services.webapp import canonical_url
from lp.services.webapp.interfaces import OAuthPermission
from lp.services.worlddata.interfaces.country import ICountrySet
from lp.soyuz.enums import ArchivePurpose, PackagePublishingStatus
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.interfaces.archivepermission import IArchivePermissionSet
from lp.soyuz.interfaces.distributionsourcepackagerelease import (
    IDistributionSourcePackageRelease,
)
from lp.testing import (
    StormStatementRecorder,
    TestCase,
    TestCaseWithFactory,
    admin_logged_in,
    anonymous_logged_in,
    api_url,
    celebrity_logged_in,
    login,
    login_person,
    person_logged_in,
)
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
    ZopelessDatabaseLayer,
)
from lp.testing.matchers import HasQueryCount, Provides
from lp.testing.pages import webservice_for_person
from lp.testing.views import create_initialized_view
from lp.translations.enums import TranslationPermission

PRIVATE_DISTRIBUTION_TYPES = [InformationType.PROPRIETARY]


class TestDistribution(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_pillar_category(self):
        # The pillar category is correct.
        distro = self.factory.makeDistribution()
        self.assertEqual("Distribution", distro.pillar_category)

    def test_sharing_policies(self):
        # The sharing policies are PUBLIC.
        distro = self.factory.makeDistribution()
        self.assertEqual(
            BranchSharingPolicy.PUBLIC, distro.branch_sharing_policy
        )
        self.assertEqual(BugSharingPolicy.PUBLIC, distro.bug_sharing_policy)

    def test_owner_cannot_be_open_team(self):
        """Distro owners cannot be open teams."""
        for policy in INCLUSIVE_TEAM_POLICY:
            open_team = self.factory.makeTeam(membership_policy=policy)
            self.assertRaises(
                InclusiveTeamLinkageError,
                self.factory.makeDistribution,
                owner=open_team,
            )

    def test_owner_can_be_closed_team(self):
        """Distro owners can be exclusive teams."""
        for policy in EXCLUSIVE_TEAM_POLICY:
            closed_team = self.factory.makeTeam(membership_policy=policy)
            self.factory.makeDistribution(owner=closed_team)

    def test_distribution_repr_ansii(self):
        # Verify that ANSI displayname is ascii safe.
        distro = self.factory.makeDistribution(
            name="distro", displayname="\xdc-distro"
        )
        ignore, displayname, name = repr(distro).rsplit(" ", 2)
        self.assertEqual("'\\xdc-distro'", displayname)
        self.assertEqual("(distro)>", name)

    def test_distribution_repr_unicode(self):
        # Verify that Unicode displayname is ascii safe.
        distro = self.factory.makeDistribution(
            name="distro", displayname="\u0170-distro"
        )
        ignore, displayname, name = repr(distro).rsplit(" ", 2)
        self.assertEqual("'\\u0170-distro'", displayname)

    def test_guessPublishedSourcePackageName_no_distro_series(self):
        # Distribution without a series raises NotFoundError
        distro = self.factory.makeDistribution()
        with ExpectedException(NotFoundError, ".*has no series.*"):
            distro.guessPublishedSourcePackageName("package")

    def test_guessPublishedSourcePackageName_invalid_name(self):
        # Invalid name raises a NotFoundError
        distro = self.factory.makeDistribution()
        with ExpectedException(NotFoundError, "'Invalid package name.*"):
            distro.guessPublishedSourcePackageName("a*package")

    def test_guessPublishedSourcePackageName_nothing_published(self):
        distroseries = self.factory.makeDistroSeries()
        with ExpectedException(NotFoundError, "'Unknown package:.*"):
            distroseries.distribution.guessPublishedSourcePackageName(
                "a-package"
            )

    def test_guessPublishedSourcePackageName_ignored_removed(self):
        # Removed binary package are ignored.
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeBinaryPackagePublishingHistory(
            archive=distroseries.main_archive,
            binarypackagename="binary-package",
            status=PackagePublishingStatus.SUPERSEDED,
        )
        with ExpectedException(NotFoundError, ".*Binary package.*"):
            distroseries.distribution.guessPublishedSourcePackageName(
                "binary-package"
            )

    def test_guessPublishedSourcePackageName_sourcepackage_name(self):
        distroseries = self.factory.makeDistroSeries()
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries, sourcepackagename="my-package"
        )
        self.assertEqual(
            spph.sourcepackagerelease.sourcepackagename,
            distroseries.distribution.guessPublishedSourcePackageName(
                "my-package"
            ),
        )

    def test_guessPublishedSourcePackageName_binarypackage_name(self):
        distroseries = self.factory.makeDistroSeries()
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries, sourcepackagename="my-package"
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=distroseries.main_archive,
            binarypackagename="binary-package",
            source_package_release=spph.sourcepackagerelease,
        )
        self.assertEqual(
            spph.sourcepackagerelease.sourcepackagename,
            distroseries.distribution.guessPublishedSourcePackageName(
                "binary-package"
            ),
        )

    def test_guessPublishedSourcePackageName_exlude_ppa(self):
        # Package published in PPAs are not considered to be part of the
        # distribution.
        distroseries = self.factory.makeUbuntuDistroSeries()
        ppa_archive = self.factory.makeArchive()
        self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries,
            sourcepackagename="my-package",
            archive=ppa_archive,
        )
        with ExpectedException(NotFoundError, ".*not published in.*"):
            distroseries.distribution.guessPublishedSourcePackageName(
                "my-package"
            )

    def test_guessPublishedSourcePackageName_exlude_other_distro(self):
        # Published source package are only found in the distro
        # in which they were published.
        distroseries1 = self.factory.makeDistroSeries()
        distroseries2 = self.factory.makeDistroSeries()
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries1, sourcepackagename="my-package"
        )
        self.assertEqual(
            spph.sourcepackagerelease.sourcepackagename,
            distroseries1.distribution.guessPublishedSourcePackageName(
                "my-package"
            ),
        )
        with ExpectedException(NotFoundError, ".*not published in.*"):
            distroseries2.distribution.guessPublishedSourcePackageName(
                "my-package"
            )

    def test_guessPublishedSourcePackageName_looks_for_source_first(self):
        # If both a binary and source package name shares the same name,
        # the source package will be returned (and the one from the unrelated
        # binary).
        distroseries = self.factory.makeDistroSeries()
        my_spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=distroseries, sourcepackagename="my-package"
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=distroseries.main_archive,
            binarypackagename="my-package",
            sourcepackagename="other-package",
        )
        self.assertEqual(
            my_spph.sourcepackagerelease.sourcepackagename,
            distroseries.distribution.guessPublishedSourcePackageName(
                "my-package"
            ),
        )

    def test_guessPublishedSourcePackageName_uses_latest(self):
        # If multiple binaries match, it will return the source of the latest
        # one published.
        distroseries = self.factory.makeDistroSeries()
        self.factory.makeBinaryPackagePublishingHistory(
            archive=distroseries.main_archive,
            sourcepackagename="old-source-name",
            binarypackagename="my-package",
        )
        self.factory.makeBinaryPackagePublishingHistory(
            archive=distroseries.main_archive,
            sourcepackagename="new-source-name",
            binarypackagename="my-package",
        )
        self.assertEqual(
            "new-source-name",
            distroseries.distribution.guessPublishedSourcePackageName(
                "my-package"
            ).name,
        )

    def test_guessPublishedSourcePackageName_official_package_branch(self):
        # It consider that a sourcepackage that has an official package
        # branch is published.
        sourcepackage = self.factory.makeSourcePackage(
            sourcepackagename="my-package"
        )
        self.factory.makeRelatedBranchesForSourcePackage(
            sourcepackage=sourcepackage
        )
        self.assertEqual(
            "my-package",
            sourcepackage.distribution.guessPublishedSourcePackageName(
                "my-package"
            ).name,
        )

    def test_derivatives_email(self):
        # Make sure the package_derivatives_email column stores data
        # correctly.
        email = "thingy@foo.com"
        distro = self.factory.makeDistribution()
        with person_logged_in(distro.owner):
            distro.package_derivatives_email = email
        Store.of(distro).flush()
        self.assertEqual(email, distro.package_derivatives_email)

    def test_derivatives_email_permissions(self):
        # package_derivatives_email requires lp.edit to set/change.
        distro = self.factory.makeDistribution()
        self.assertRaises(
            Unauthorized, setattr, distro, "package_derivatives_email", "foo"
        )

    def test_implements_interfaces(self):
        # Distribution fully implements its interfaces.
        distro = removeSecurityProxy(self.factory.makeDistribution())
        expected_interfaces = [
            IHasOOPSReferences,
        ]
        provides_all = MatchesAll(*map(Provides, expected_interfaces))
        self.assertThat(distro, provides_all)

    def test_distribution_creation_creates_accesspolicies(self):
        # Creating a new distribution also creates AccessPolicies for it.
        distro = self.factory.makeDistribution()
        ap = getUtility(IAccessPolicySource).findByPillar((distro,))
        expected = [InformationType.USERDATA, InformationType.PRIVATESECURITY]
        self.assertContentEqual(expected, [policy.type for policy in ap])

    def test_getAllowedBugInformationTypes(self):
        # All distros currently support just the non-proprietary
        # information types.
        self.assertContentEqual(
            [
                InformationType.PUBLIC,
                InformationType.PUBLICSECURITY,
                InformationType.PRIVATESECURITY,
                InformationType.USERDATA,
            ],
            self.factory.makeDistribution().getAllowedBugInformationTypes(),
        )

    def test_getDefaultBugInformationType(self):
        # The default information type for distributions is always PUBLIC.
        self.assertEqual(
            InformationType.PUBLIC,
            self.factory.makeDistribution().getDefaultBugInformationType(),
        )

    def test_getAllowedSpecificationInformationTypes(self):
        # All distros currently support only public specifications.
        distro = self.factory.makeDistribution()
        self.assertContentEqual(
            [InformationType.PUBLIC],
            distro.getAllowedSpecificationInformationTypes(),
        )

    def test_getDefaultSpecificationInformtationType(self):
        # All distros currently support only Public by default
        # specifications.
        distro = self.factory.makeDistribution()
        self.assertEqual(
            InformationType.PUBLIC,
            distro.getDefaultSpecificationInformationType(),
        )

    def test_getExternalPackage(self):
        # Test that we get the ExternalPackage that belongs to the distribution
        # with the proper attributes
        distro = self.factory.makeDistribution()
        sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "my-package"
        )
        channel = ("22.04", "candidate", "staging")
        externalpackage = distro.getExternalPackage(
            name=sourcepackagename,
            packagetype=ExternalPackageType.ROCK,
            channel=channel,
        )
        self.assertEqual(externalpackage.distribution, distro)
        self.assertEqual(externalpackage.name, "my-package")
        self.assertEqual(externalpackage.packagetype, ExternalPackageType.ROCK)
        self.assertEqual(externalpackage.channel, channel)

        # We can have external packages without channel
        externalpackage = distro.getExternalPackage(
            name=sourcepackagename,
            packagetype=ExternalPackageType.SNAP,
            channel=None,
        )
        self.assertEqual(externalpackage.distribution, distro)
        self.assertEqual(externalpackage.name, "my-package")
        self.assertEqual(externalpackage.packagetype, ExternalPackageType.SNAP)
        self.assertEqual(externalpackage.channel, None)

    def test_getOCIProject(self):
        distro = self.factory.makeDistribution()
        first_project = self.factory.makeOCIProject(pillar=distro)
        # make another project to ensure we don't default
        self.factory.makeOCIProject(pillar=distro)
        result = distro.getOCIProject(first_project.name)
        self.assertEqual(first_project, result)

    def test_searchOCIProjects_empty(self):
        distro = self.factory.makeDistribution()
        for _ in range(5):
            self.factory.makeOCIProject(pillar=distro)

        result = distro.searchOCIProjects()
        self.assertEqual(5, result.count())

    def test_searchOCIProjects_by_name(self):
        name = self.factory.getUniqueUnicode()
        distro = self.factory.makeDistribution()
        first_name = self.factory.makeOCIProjectName(name=name)
        first_project = self.factory.makeOCIProject(
            pillar=distro, ociprojectname=first_name
        )
        self.factory.makeOCIProject(pillar=distro)

        result = distro.searchOCIProjects(text=name)
        self.assertEqual(1, result.count())
        self.assertEqual(first_project, result[0])

    def test_searchOCIProjects_by_partial_name(self):
        name = "testpartialname"
        distro = self.factory.makeDistribution()
        first_name = self.factory.makeOCIProjectName(name=name)
        first_project = self.factory.makeOCIProject(
            pillar=distro, ociprojectname=first_name
        )
        self.factory.makeOCIProject(pillar=distro)

        result = distro.searchOCIProjects(text="partial")
        self.assertEqual(1, result.count())
        self.assertEqual(first_project, result[0])

    def test_default_traversal(self):
        # By default, a distribution's default traversal refers to its
        # series.
        distro = self.factory.makeDistribution()
        self.assertEqual(
            DistributionDefaultTraversalPolicy.SERIES,
            distro.default_traversal_policy,
        )
        self.assertFalse(distro.redirect_default_traversal)

    def test_default_traversal_permissions(self):
        # Only distribution owners can change the default traversal
        # behaviour.
        distro = self.factory.makeDistribution()
        with person_logged_in(self.factory.makePerson()):
            self.assertRaises(
                Unauthorized,
                setattr,
                distro,
                "default_traversal_policy",
                DistributionDefaultTraversalPolicy.SERIES,
            )
            self.assertRaises(
                Unauthorized,
                setattr,
                distro,
                "redirect_default_traversal",
                True,
            )
        with person_logged_in(distro.owner):
            distro.default_traversal_policy = (
                DistributionDefaultTraversalPolicy.SERIES
            )
            distro.redirect_default_traversal = True

    def test_creation_grants_maintainer_access(self):
        # Creating a new distribution creates an access grant for the
        # maintainer for all default policies.
        distribution = self.factory.makeDistribution()
        policies = getUtility(IAccessPolicySource).findByPillar(
            (distribution,)
        )
        grants = getUtility(IAccessPolicyGrantSource).findByPolicy(policies)
        expected_grantess = {distribution.owner}
        grantees = {grant.grantee for grant in grants}
        self.assertEqual(expected_grantess, grantees)

    def test_open_creation_sharing_policies(self):
        # Creating a new open (non-proprietary) distribution sets the bug
        # and branch sharing policies to public, and creates policies if
        # required.
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            distribution = self.factory.makeDistribution(owner=owner)
        self.assertEqual(
            BugSharingPolicy.PUBLIC, distribution.bug_sharing_policy
        )
        self.assertEqual(
            BranchSharingPolicy.PUBLIC, distribution.branch_sharing_policy
        )
        self.assertEqual(
            SpecificationSharingPolicy.PUBLIC,
            distribution.specification_sharing_policy,
        )
        aps = getUtility(IAccessPolicySource).findByPillar([distribution])
        expected = [InformationType.USERDATA, InformationType.PRIVATESECURITY]
        self.assertContentEqual(expected, [policy.type for policy in aps])

    def test_proprietary_creation_sharing_policies(self):
        # Creating a new proprietary distribution sets the bug, branch, and
        # specification sharing policies to proprietary.
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            distribution = self.factory.makeDistribution(
                owner=owner, information_type=InformationType.PROPRIETARY
            )
            self.assertEqual(
                BugSharingPolicy.PROPRIETARY, distribution.bug_sharing_policy
            )
            self.assertEqual(
                BranchSharingPolicy.PROPRIETARY,
                distribution.branch_sharing_policy,
            )
            self.assertEqual(
                SpecificationSharingPolicy.PROPRIETARY,
                distribution.specification_sharing_policy,
            )
        aps = getUtility(IAccessPolicySource).findByPillar([distribution])
        expected = [InformationType.PROPRIETARY]
        self.assertContentEqual(expected, [policy.type for policy in aps])

    def test_change_info_type_proprietary_check_artifacts(self):
        # Cannot change distribution information_type if any artifacts are
        # public.
        distribution = self.factory.makeDistribution(
            specification_sharing_policy=(
                SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
            ),
            bug_sharing_policy=BugSharingPolicy.PUBLIC_OR_PROPRIETARY,
            branch_sharing_policy=BranchSharingPolicy.PUBLIC_OR_PROPRIETARY,
        )
        self.useContext(person_logged_in(distribution.owner))
        spec = self.factory.makeSpecification(distribution=distribution)
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            with ExpectedException(
                CannotChangeInformationType, "Some blueprints are public."
            ):
                distribution.information_type = info_type
        spec.transitionToInformationType(
            InformationType.PROPRIETARY, distribution.owner
        )
        dsp = self.factory.makeDistributionSourcePackage(
            distribution=distribution
        )
        bug = self.factory.makeBug(target=dsp)
        for bug_info_type in FREE_INFORMATION_TYPES:
            bug.transitionToInformationType(bug_info_type, distribution.owner)
            for info_type in PRIVATE_DISTRIBUTION_TYPES:
                with ExpectedException(
                    CannotChangeInformationType,
                    "Some bugs are neither proprietary nor embargoed.",
                ):
                    distribution.information_type = info_type
        bug.transitionToInformationType(
            InformationType.PROPRIETARY, distribution.owner
        )
        distroseries = self.factory.makeDistroSeries(distribution=distribution)
        sp = self.factory.makeSourcePackage(distroseries=distroseries)
        branch = self.factory.makeBranch(sourcepackage=sp)
        for branch_info_type in FREE_INFORMATION_TYPES:
            branch.transitionToInformationType(
                branch_info_type, distribution.owner
            )
            for info_type in PRIVATE_DISTRIBUTION_TYPES:
                with ExpectedException(
                    CannotChangeInformationType,
                    "Some branches are neither proprietary nor " "embargoed.",
                ):
                    distribution.information_type = info_type
        branch.transitionToInformationType(
            InformationType.PROPRIETARY, distribution.owner
        )
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            distribution.information_type = info_type

    def test_change_info_type_proprietary_check_translations(self):
        distribution = self.factory.makeDistribution()
        with person_logged_in(distribution.owner):
            for usage in ServiceUsage:
                distribution.information_type = InformationType.PUBLIC
                distribution.translations_usage = usage.value
                for info_type in PRIVATE_DISTRIBUTION_TYPES:
                    if (
                        distribution.translations_usage
                        == ServiceUsage.LAUNCHPAD
                    ):
                        with ExpectedException(
                            CannotChangeInformationType,
                            "Translations are enabled.",
                        ):
                            distribution.information_type = info_type
                    else:
                        distribution.information_type = info_type

    def test_change_info_type_proprietary_sets_policies(self):
        # Changing information type from public to proprietary sets the
        # appropriate policies.
        distribution = self.factory.makeDistribution()
        with person_logged_in(distribution.owner):
            distribution.information_type = InformationType.PROPRIETARY
            self.assertEqual(
                BranchSharingPolicy.PROPRIETARY,
                distribution.branch_sharing_policy,
            )
            self.assertEqual(
                BugSharingPolicy.PROPRIETARY, distribution.bug_sharing_policy
            )
            self.assertEqual(
                SpecificationSharingPolicy.PROPRIETARY,
                distribution.specification_sharing_policy,
            )

    def test_proprietary_to_public_leaves_policies(self):
        # Changing information type from public leaves sharing policies
        # unchanged.
        owner = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            information_type=InformationType.PROPRIETARY, owner=owner
        )
        with person_logged_in(owner):
            distribution.information_type = InformationType.PUBLIC
            # Setting information type to the current type should be a no-op.
            distribution.information_type = InformationType.PUBLIC
        self.assertEqual(
            BranchSharingPolicy.PROPRIETARY, distribution.branch_sharing_policy
        )
        self.assertEqual(
            BugSharingPolicy.PROPRIETARY, distribution.bug_sharing_policy
        )
        self.assertEqual(
            SpecificationSharingPolicy.PROPRIETARY,
            distribution.specification_sharing_policy,
        )

    def test_cacheAccessPolicies(self):
        # Distribution.access_policies is a list caching AccessPolicy.ids
        # for which an AccessPolicyGrant or AccessArtifactGrant gives a
        # principal LimitedView on the Distribution.
        aps = getUtility(IAccessPolicySource)

        # Public distributions don't need a cache.
        distribution = self.factory.makeDistribution()
        naked_distribution = removeSecurityProxy(distribution)
        self.assertContentEqual(
            [InformationType.USERDATA, InformationType.PRIVATESECURITY],
            [p.type for p in aps.findByPillar([distribution])],
        )
        self.assertIsNone(naked_distribution.access_policies)

        # A private distribution normally just allows the Proprietary
        # policy, even if there is still another policy like Private
        # Security.
        naked_distribution.information_type = InformationType.PROPRIETARY
        [prop_policy] = aps.find([(distribution, InformationType.PROPRIETARY)])
        self.assertEqual([prop_policy.id], naked_distribution.access_policies)

        # If we switch it back to public, the cache is no longer
        # required.
        naked_distribution.information_type = InformationType.PUBLIC
        self.assertIsNone(naked_distribution.access_policies)

        # Proprietary distributions can have both Proprietary and Embargoed
        # artifacts, and someone who can see either needs LimitedView on the
        # pillar they're on.  So both policies are permissible if they
        # exist.
        naked_distribution.information_type = InformationType.PROPRIETARY
        naked_distribution.setBugSharingPolicy(
            BugSharingPolicy.EMBARGOED_OR_PROPRIETARY
        )
        [emb_policy] = aps.find([(distribution, InformationType.EMBARGOED)])
        self.assertContentEqual(
            [prop_policy.id, emb_policy.id], naked_distribution.access_policies
        )

    def test_checkInformationType_bug_supervisor(self):
        # Bug supervisors of proprietary distributions must not have
        # inclusive membership policies.
        team = self.factory.makeTeam()
        distribution = self.factory.makeDistribution(bug_supervisor=team)
        for policy in (token.value for token in TeamMembershipPolicy):
            with person_logged_in(team.teamowner):
                team.membership_policy = policy
            for info_type in PRIVATE_DISTRIBUTION_TYPES:
                with person_logged_in(distribution.owner):
                    errors = list(distribution.checkInformationType(info_type))
                if policy in EXCLUSIVE_TEAM_POLICY:
                    self.assertEqual([], errors)
                else:
                    with ExpectedException(
                        CannotChangeInformationType,
                        "Bug supervisor has inclusive membership.",
                    ):
                        raise errors[0]

    def test_checkInformationType_questions(self):
        # Proprietary distributions must not have questions.
        distribution = self.factory.makeDistribution()
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            with person_logged_in(distribution.owner):
                self.assertEqual(
                    [], list(distribution.checkInformationType(info_type))
                )
        self.factory.makeQuestion(target=distribution)
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            with person_logged_in(distribution.owner):
                (error,) = list(distribution.checkInformationType(info_type))
            with ExpectedException(
                CannotChangeInformationType, "This distribution has questions."
            ):
                raise error

    def test_checkInformationType_translations(self):
        # Proprietary distributions must not have translations.
        distroseries = self.factory.makeDistroSeries()
        distribution = distroseries.distribution
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            with person_logged_in(distribution.owner):
                self.assertEqual(
                    [], list(distribution.checkInformationType(info_type))
                )
        self.factory.makePOTemplate(distroseries=distroseries)
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            with person_logged_in(distribution.owner):
                (error,) = list(distribution.checkInformationType(info_type))
            with ExpectedException(
                CannotChangeInformationType,
                "This distribution has translations.",
            ):
                raise error

    def test_checkInformationType_queued_translations(self):
        # Proprietary distributions must not have queued translations.
        self.useFixture(FakeLibrarian())
        distroseries = self.factory.makeDistroSeries()
        distribution = distroseries.distribution
        entry = self.factory.makeTranslationImportQueueEntry(
            distroseries=distroseries
        )
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            with person_logged_in(distribution.owner):
                (error,) = list(distribution.checkInformationType(info_type))
            with ExpectedException(
                CannotChangeInformationType,
                "This distribution has queued translations.",
            ):
                raise error
        Store.of(entry).remove(entry)
        with person_logged_in(distribution.owner):
            for info_type in PRIVATE_DISTRIBUTION_TYPES:
                self.assertContentEqual(
                    [], distribution.checkInformationType(info_type)
                )

    def test_checkInformationType_series_only_bugs(self):
        # A distribution with bugtasks that are only targeted to a series
        # cannot change information type.
        series = self.factory.makeDistroSeries()
        bug = self.factory.makeBug(target=series.distribution)
        with person_logged_in(series.owner):
            bug.addTask(series.owner, series)
            bug.default_bugtask.delete()
            for info_type in PRIVATE_DISTRIBUTION_TYPES:
                (error,) = list(
                    series.distribution.checkInformationType(info_type)
                )
                with ExpectedException(
                    CannotChangeInformationType,
                    "Some bugs are neither proprietary nor embargoed.",
                ):
                    raise error

    def test_private_forbids_translations(self):
        owner = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=owner)
        self.useContext(person_logged_in(owner))
        for info_type in PRIVATE_DISTRIBUTION_TYPES:
            distribution.information_type = info_type
            with ExpectedException(
                ProprietaryPillar,
                "Translations are not supported for proprietary "
                "distributions.",
            ):
                distribution.translations_usage = ServiceUsage.LAUNCHPAD
            for usage in ServiceUsage.items:
                if usage == ServiceUsage.LAUNCHPAD:
                    continue
                distribution.translations_usage = usage

    def createDistribution(self, information_type=None):
        # Convenience method for testing IDistributionSet.new rather than
        # self.factory.makeDistribution.
        owner = self.factory.makePerson()
        members = self.factory.makeTeam(owner=owner)
        kwargs = {}
        if information_type is not None:
            kwargs["information_type"] = information_type
        with person_logged_in(owner):
            return getUtility(IDistributionSet).new(
                name=self.factory.getUniqueUnicode("distro"),
                display_name="Fnord",
                title="Fnord",
                description="test 1",
                summary="test 2",
                domainname="distro.example.org",
                members=members,
                owner=owner,
                registrant=owner,
                **kwargs,
            )

    def test_information_type(self):
        # Distribution is created with specified information_type.
        distribution = self.createDistribution(
            information_type=InformationType.PROPRIETARY
        )
        self.assertEqual(
            InformationType.PROPRIETARY, distribution.information_type
        )
        # The owner can set information_type.
        with person_logged_in(removeSecurityProxy(distribution).owner):
            distribution.information_type = InformationType.PUBLIC
        self.assertEqual(InformationType.PUBLIC, distribution.information_type)
        # The database persists the value of information_type.
        store = Store.of(distribution)
        store.flush()
        store.reset()
        distribution = store.get(Distribution, distribution.id)
        self.assertEqual(InformationType.PUBLIC, distribution.information_type)
        self.assertFalse(distribution.private)

    def test_switching_to_public_does_not_create_policy(self):
        # Creating a Proprietary distribution and switching it to Public
        # does not create a PUBLIC AccessPolicy.
        distribution = self.createDistribution(
            information_type=InformationType.PROPRIETARY
        )
        aps = getUtility(IAccessPolicySource).findByPillar([distribution])
        self.assertContentEqual(
            [InformationType.PROPRIETARY], [ap.type for ap in aps]
        )
        removeSecurityProxy(distribution).information_type = (
            InformationType.PUBLIC
        )
        aps = getUtility(IAccessPolicySource).findByPillar([distribution])
        self.assertContentEqual(
            [InformationType.PROPRIETARY], [ap.type for ap in aps]
        )

    def test_information_type_default(self):
        # The default information_type is PUBLIC.
        distribution = self.createDistribution()
        self.assertEqual(InformationType.PUBLIC, distribution.information_type)
        self.assertFalse(distribution.private)

    invalid_information_types = [
        info_type
        for info_type in InformationType.items
        if info_type not in PILLAR_INFORMATION_TYPES
    ]

    def test_information_type_init_invalid_values(self):
        # Cannot create Distribution.information_type with invalid values.
        for info_type in self.invalid_information_types:
            with ExpectedException(
                CannotChangeInformationType, "Not supported for distributions."
            ):
                self.createDistribution(information_type=info_type)

    def test_information_type_set_invalid_values(self):
        # Cannot set Distribution.information_type to invalid values.
        distribution = self.factory.makeDistribution()
        for info_type in self.invalid_information_types:
            with ExpectedException(
                CannotChangeInformationType, "Not supported for distributions."
            ):
                with person_logged_in(distribution.owner):
                    distribution.information_type = info_type

    def test_set_proprietary_gets_commercial_subscription(self):
        # Changing a Distribution to Proprietary will auto-generate a
        # complimentary subscription just as choosing a proprietary
        # information type at creation time.
        owner = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=owner)
        self.useContext(person_logged_in(owner))
        self.assertIsNone(distribution.commercial_subscription)

        distribution.information_type = InformationType.PROPRIETARY
        self.assertEqual(
            InformationType.PROPRIETARY, distribution.information_type
        )
        self.assertIsNotNone(distribution.commercial_subscription)

    def test_set_proprietary_fails_expired_commercial_subscription(self):
        # Cannot set information type to proprietary with an expired
        # complimentary subscription.
        owner = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            information_type=InformationType.PROPRIETARY, owner=owner
        )
        self.useContext(person_logged_in(owner))

        # The Distribution now has a complimentary commercial subscription.
        new_expires_date = datetime.now(timezone.utc) - timedelta(1)
        naked_subscription = removeSecurityProxy(
            distribution.commercial_subscription
        )
        naked_subscription.date_expires = new_expires_date

        # We can make the distribution PUBLIC.
        distribution.information_type = InformationType.PUBLIC
        self.assertEqual(InformationType.PUBLIC, distribution.information_type)

        # However we can't change it back to Proprietary because our
        # commercial subscription has expired.
        with ExpectedException(
            CommercialSubscribersOnly,
            "A valid commercial subscription is required for private"
            " distributions.",
        ):
            distribution.information_type = InformationType.PROPRIETARY

    def test_no_answers_for_proprietary(self):
        # Enabling Answers is forbidden while information_type is proprietary.
        distribution = self.factory.makeDistribution(
            information_type=InformationType.PROPRIETARY
        )
        with person_logged_in(removeSecurityProxy(distribution).owner):
            self.assertEqual(ServiceUsage.UNKNOWN, distribution.answers_usage)
            for usage in ServiceUsage.items:
                if usage == ServiceUsage.LAUNCHPAD:
                    with ExpectedException(
                        ServiceUsageForbidden,
                        "Answers not allowed for non-public " "distributions.",
                    ):
                        distribution.answers_usage = ServiceUsage.LAUNCHPAD
                else:
                    # All other values are permitted.
                    distribution.answers_usage = usage

    def test_answers_for_public(self):
        # Enabling answers is permitted while information_type is PUBLIC.
        distribution = self.factory.makeDistribution(
            information_type=InformationType.PUBLIC
        )
        self.assertEqual(ServiceUsage.UNKNOWN, distribution.answers_usage)
        with person_logged_in(distribution.owner):
            for usage in ServiceUsage.items:
                # All values are permitted.
                distribution.answers_usage = usage

    def test_no_proprietary_if_answers(self):
        # Information type cannot be set to proprietary while Answers are
        # enabled.
        distribution = self.factory.makeDistribution()
        with person_logged_in(distribution.owner):
            distribution.answers_usage = ServiceUsage.LAUNCHPAD
            with ExpectedException(
                CannotChangeInformationType, "Answers is enabled."
            ):
                distribution.information_type = InformationType.PROPRIETARY

    def test_set_code_admin_permissions(self):
        distribution = self.factory.makeDistribution()
        person = self.factory.makePerson()
        person2 = self.factory.makePerson()
        code_admin_team = self.factory.makeTeam(members=[person])

        with person_logged_in(distribution.owner):
            distribution.code_admin = code_admin_team
            self.assertEqual(code_admin_team, distribution.code_admin)

        with admin_logged_in():
            distribution.code_admin = None
            self.assertIsNone(distribution.code_admin)

        with person_logged_in(person2), ExpectedException(Unauthorized):
            distribution.code_admin = code_admin_team

        with anonymous_logged_in(), ExpectedException(Unauthorized):
            distribution.code_admin = code_admin_team

    def test_bug_target_parent(self):
        distro = self.factory.makeDistribution()
        parent = distro.bug_target_parent

        # The parent should not be None.
        self.assertIsNotNone(parent)

        # The parent should provide the IDistribution interface.
        self.assertTrue(IDistribution.providedBy(parent))

        # The parent should provide the IBugTargetParent interface.
        self.assertTrue(IBugTargetParent.providedBy(parent))

        # The parent should be the distribution itself.
        self.assertEqual(distro, parent)


class TestDistributionBugInformationTypes(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def makeDistributionWithPolicy(self, bug_sharing_policy):
        distribution = self.factory.makeDistribution()
        self.factory.makeCommercialSubscription(pillar=distribution)
        with person_logged_in(distribution.owner):
            distribution.setBugSharingPolicy(bug_sharing_policy)
        return distribution

    def test_no_policy(self):
        # New distributions can only use the non-proprietary information
        # types.
        distribution = self.factory.makeDistribution()
        self.assertContentEqual(
            FREE_INFORMATION_TYPES,
            distribution.getAllowedBugInformationTypes(),
        )
        self.assertEqual(
            InformationType.PUBLIC, distribution.getDefaultBugInformationType()
        )

    def test_sharing_policy_public_or_proprietary(self):
        # bug_sharing_policy can enable Proprietary.
        distribution = self.makeDistributionWithPolicy(
            BugSharingPolicy.PUBLIC_OR_PROPRIETARY
        )
        self.assertContentEqual(
            FREE_INFORMATION_TYPES + (InformationType.PROPRIETARY,),
            distribution.getAllowedBugInformationTypes(),
        )
        self.assertEqual(
            InformationType.PUBLIC, distribution.getDefaultBugInformationType()
        )

    def test_sharing_policy_proprietary_or_public(self):
        # bug_sharing_policy can enable and default to Proprietary.
        distribution = self.makeDistributionWithPolicy(
            BugSharingPolicy.PROPRIETARY_OR_PUBLIC
        )
        self.assertContentEqual(
            FREE_INFORMATION_TYPES + (InformationType.PROPRIETARY,),
            distribution.getAllowedBugInformationTypes(),
        )
        self.assertEqual(
            InformationType.PROPRIETARY,
            distribution.getDefaultBugInformationType(),
        )

    def test_sharing_policy_proprietary(self):
        # bug_sharing_policy can enable only Proprietary.
        distribution = self.makeDistributionWithPolicy(
            BugSharingPolicy.PROPRIETARY
        )
        self.assertContentEqual(
            [InformationType.PROPRIETARY],
            distribution.getAllowedBugInformationTypes(),
        )
        self.assertEqual(
            InformationType.PROPRIETARY,
            distribution.getDefaultBugInformationType(),
        )


class TestDistributionSpecificationPolicyAndInformationTypes(
    TestCaseWithFactory
):
    layer = DatabaseFunctionalLayer

    def makeDistributionWithPolicy(self, specification_sharing_policy):
        distribution = self.factory.makeDistribution()
        self.factory.makeCommercialSubscription(pillar=distribution)
        with person_logged_in(distribution.owner):
            distribution.setSpecificationSharingPolicy(
                specification_sharing_policy
            )
        return distribution

    def test_no_policy(self):
        # Distributions that have not specified a policy can use the PUBLIC
        # information type.
        distribution = self.factory.makeDistribution()
        self.assertContentEqual(
            [InformationType.PUBLIC],
            distribution.getAllowedSpecificationInformationTypes(),
        )
        self.assertEqual(
            InformationType.PUBLIC,
            distribution.getDefaultSpecificationInformationType(),
        )

    def test_sharing_policy_public(self):
        # Distributions with a purely public policy should use PUBLIC
        # information type.
        distribution = self.makeDistributionWithPolicy(
            SpecificationSharingPolicy.PUBLIC
        )
        self.assertContentEqual(
            [InformationType.PUBLIC],
            distribution.getAllowedSpecificationInformationTypes(),
        )
        self.assertEqual(
            InformationType.PUBLIC,
            distribution.getDefaultSpecificationInformationType(),
        )

    def test_sharing_policy_public_or_proprietary(self):
        # specification_sharing_policy can enable Proprietary.
        distribution = self.makeDistributionWithPolicy(
            SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY
        )
        self.assertContentEqual(
            [InformationType.PUBLIC, InformationType.PROPRIETARY],
            distribution.getAllowedSpecificationInformationTypes(),
        )
        self.assertEqual(
            InformationType.PUBLIC,
            distribution.getDefaultSpecificationInformationType(),
        )

    def test_sharing_policy_proprietary_or_public(self):
        # specification_sharing_policy can enable and default to Proprietary.
        distribution = self.makeDistributionWithPolicy(
            SpecificationSharingPolicy.PROPRIETARY_OR_PUBLIC
        )
        self.assertContentEqual(
            [InformationType.PUBLIC, InformationType.PROPRIETARY],
            distribution.getAllowedSpecificationInformationTypes(),
        )
        self.assertEqual(
            InformationType.PROPRIETARY,
            distribution.getDefaultSpecificationInformationType(),
        )

    def test_sharing_policy_proprietary(self):
        # specification_sharing_policy can enable only Proprietary.
        distribution = self.makeDistributionWithPolicy(
            SpecificationSharingPolicy.PROPRIETARY
        )
        self.assertContentEqual(
            [InformationType.PROPRIETARY],
            distribution.getAllowedSpecificationInformationTypes(),
        )
        self.assertEqual(
            InformationType.PROPRIETARY,
            distribution.getDefaultSpecificationInformationType(),
        )

    def test_sharing_policy_embargoed_or_proprietary(self):
        # specification_sharing_policy can be embargoed and then proprietary.
        distribution = self.makeDistributionWithPolicy(
            SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY
        )
        self.assertContentEqual(
            [InformationType.PROPRIETARY, InformationType.EMBARGOED],
            distribution.getAllowedSpecificationInformationTypes(),
        )
        self.assertEqual(
            InformationType.EMBARGOED,
            distribution.getDefaultSpecificationInformationType(),
        )


class BaseSharingPolicyTests:
    """Common tests for distribution sharing policies."""

    layer = DatabaseFunctionalLayer

    def setSharingPolicy(self, policy, user):
        raise NotImplementedError

    def getSharingPolicy(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.distribution = self.factory.makeDistribution()
        self.commercial_admin = self.factory.makeCommercialAdmin()

    def test_owner_can_set_policy(self):
        # Distribution maintainers can set sharing policies.
        self.setSharingPolicy(self.public_policy, self.distribution.owner)
        self.assertEqual(self.public_policy, self.getSharingPolicy())

    def test_commercial_admin_can_set_policy(self):
        # Commercial admins can set sharing policies for commercial
        # distributions.
        self.factory.makeCommercialSubscription(pillar=self.distribution)
        self.setSharingPolicy(self.public_policy, self.commercial_admin)
        self.assertEqual(self.public_policy, self.getSharingPolicy())

    def test_random_cannot_set_policy(self):
        # An unrelated user can't set sharing policies.
        person = self.factory.makePerson()
        self.assertRaises(
            Unauthorized, self.setSharingPolicy, self.public_policy, person
        )

    def test_anonymous_cannot_set_policy(self):
        # An anonymous user can't set sharing policies.
        self.assertRaises(
            Unauthorized, self.setSharingPolicy, self.public_policy, None
        )

    def test_proprietary_forbidden_without_commercial_sub(self):
        # No policy that allows Proprietary can be configured without a
        # commercial subscription.
        self.setSharingPolicy(self.public_policy, self.distribution.owner)
        self.assertEqual(self.public_policy, self.getSharingPolicy())
        for policy in self.commercial_policies:
            self.assertRaises(
                CommercialSubscribersOnly,
                self.setSharingPolicy,
                policy,
                self.distribution.owner,
            )

    def test_proprietary_allowed_with_commercial_sub(self):
        # All policies are valid when there's a current commercial
        # subscription.
        self.factory.makeCommercialSubscription(pillar=self.distribution)
        for policy in self.enum.items:
            self.setSharingPolicy(policy, self.commercial_admin)
            self.assertEqual(policy, self.getSharingPolicy())

    def test_setting_proprietary_creates_access_policy(self):
        # Setting a policy that allows Proprietary creates a
        # corresponding access policy and shares it with the the
        # maintainer.
        self.factory.makeCommercialSubscription(pillar=self.distribution)
        self.assertEqual(
            [InformationType.PRIVATESECURITY, InformationType.USERDATA],
            [
                policy.type
                for policy in getUtility(IAccessPolicySource).findByPillar(
                    [self.distribution]
                )
            ],
        )
        self.setSharingPolicy(
            self.commercial_policies[0], self.commercial_admin
        )
        self.assertEqual(
            [
                InformationType.PRIVATESECURITY,
                InformationType.USERDATA,
                InformationType.PROPRIETARY,
            ],
            [
                policy.type
                for policy in getUtility(IAccessPolicySource).findByPillar(
                    [self.distribution]
                )
            ],
        )
        self.assertTrue(
            getUtility(IService, "sharing").checkPillarAccess(
                [self.distribution],
                InformationType.PROPRIETARY,
                self.distribution.owner,
            )
        )

    def test_unused_policies_are_pruned(self):
        # When a sharing policy is changed, the allowed information types may
        # become more restricted. If this case, any existing access polices
        # for the now defunct information type(s) should be removed so long as
        # there are no corresponding policy artifacts.

        # We create a distribution with and ensure there's an APA.
        ap_source = getUtility(IAccessPolicySource)
        distribution = self.factory.makeDistribution()
        [ap] = ap_source.find(
            [(distribution, InformationType.PRIVATESECURITY)]
        )
        self.factory.makeAccessPolicyArtifact(policy=ap)

        def getAccessPolicyTypes(pillar):
            return [ap.type for ap in ap_source.findByPillar([pillar])]

        # Now change the sharing policies to PROPRIETARY
        self.factory.makeCommercialSubscription(pillar=distribution)
        with person_logged_in(distribution.owner):
            distribution.setBugSharingPolicy(BugSharingPolicy.PROPRIETARY)
            # Just bug sharing policy has been changed so all previous policy
            # types are still valid.
            self.assertContentEqual(
                [
                    InformationType.PRIVATESECURITY,
                    InformationType.USERDATA,
                    InformationType.PROPRIETARY,
                ],
                getAccessPolicyTypes(distribution),
            )

            distribution.setBranchSharingPolicy(
                BranchSharingPolicy.PROPRIETARY
            )
            # Proprietary is permitted by the sharing policy, and there's a
            # Private Security artifact. But Private isn't in use or allowed
            # by a sharing policy, so it's now gone.
            self.assertContentEqual(
                [InformationType.PRIVATESECURITY, InformationType.PROPRIETARY],
                getAccessPolicyTypes(distribution),
            )

    def test_proprietary_distributions_forbid_public_policies(self):
        # A proprietary distribution forbids any sharing policy that would
        # permit public artifacts.
        owner = self.distribution.owner
        with admin_logged_in():
            self.distribution.information_type = InformationType.PROPRIETARY
        policies_permitting_public = [self.public_policy]
        policies_permitting_public.extend(
            policy
            for policy in self.commercial_policies
            if InformationType.PUBLIC in self.allowed_types[policy]
        )
        for policy in policies_permitting_public:
            with ExpectedException(
                ProprietaryPillar, "The pillar is Proprietary."
            ):
                self.setSharingPolicy(policy, owner)


class TestDistributionBugSharingPolicy(
    BaseSharingPolicyTests, TestCaseWithFactory
):
    """Test Distribution.bug_sharing_policy."""

    layer = DatabaseFunctionalLayer

    enum = BugSharingPolicy
    public_policy = BugSharingPolicy.PUBLIC
    commercial_policies = (
        BugSharingPolicy.PUBLIC_OR_PROPRIETARY,
        BugSharingPolicy.PROPRIETARY_OR_PUBLIC,
        BugSharingPolicy.PROPRIETARY,
    )
    allowed_types = BUG_POLICY_ALLOWED_TYPES

    def setSharingPolicy(self, policy, user):
        with person_logged_in(user):
            result = self.distribution.setBugSharingPolicy(policy)
        return result

    def getSharingPolicy(self):
        return self.distribution.bug_sharing_policy


class TestDistributionBranchSharingPolicy(
    BaseSharingPolicyTests, TestCaseWithFactory
):
    """Test Distribution.branch_sharing_policy."""

    layer = DatabaseFunctionalLayer

    enum = BranchSharingPolicy
    public_policy = BranchSharingPolicy.PUBLIC
    commercial_policies = (
        BranchSharingPolicy.PUBLIC_OR_PROPRIETARY,
        BranchSharingPolicy.PROPRIETARY_OR_PUBLIC,
        BranchSharingPolicy.PROPRIETARY,
        BranchSharingPolicy.EMBARGOED_OR_PROPRIETARY,
    )
    allowed_types = BRANCH_POLICY_ALLOWED_TYPES

    def setSharingPolicy(self, policy, user):
        with person_logged_in(user):
            result = self.distribution.setBranchSharingPolicy(policy)
        return result

    def getSharingPolicy(self):
        return self.distribution.branch_sharing_policy

    def test_setting_embargoed_creates_access_policy(self):
        # Setting a policy that allows Embargoed creates a corresponding
        # access policy and shares it with the maintainer.
        self.factory.makeCommercialSubscription(pillar=self.distribution)
        self.assertEqual(
            [InformationType.PRIVATESECURITY, InformationType.USERDATA],
            [
                policy.type
                for policy in getUtility(IAccessPolicySource).findByPillar(
                    [self.distribution]
                )
            ],
        )
        self.setSharingPolicy(
            self.enum.EMBARGOED_OR_PROPRIETARY, self.commercial_admin
        )
        self.assertEqual(
            [
                InformationType.PRIVATESECURITY,
                InformationType.USERDATA,
                InformationType.PROPRIETARY,
                InformationType.EMBARGOED,
            ],
            [
                policy.type
                for policy in getUtility(IAccessPolicySource).findByPillar(
                    [self.distribution]
                )
            ],
        )
        self.assertTrue(
            getUtility(IService, "sharing").checkPillarAccess(
                [self.distribution],
                InformationType.PROPRIETARY,
                self.distribution.owner,
            )
        )
        self.assertTrue(
            getUtility(IService, "sharing").checkPillarAccess(
                [self.distribution],
                InformationType.EMBARGOED,
                self.distribution.owner,
            )
        )


class TestDistributionSpecificationSharingPolicy(
    BaseSharingPolicyTests, TestCaseWithFactory
):
    """Test Distribution.specification_sharing_policy."""

    layer = DatabaseFunctionalLayer

    enum = SpecificationSharingPolicy
    public_policy = SpecificationSharingPolicy.PUBLIC
    commercial_policies = (
        SpecificationSharingPolicy.PUBLIC_OR_PROPRIETARY,
        SpecificationSharingPolicy.PROPRIETARY_OR_PUBLIC,
        SpecificationSharingPolicy.PROPRIETARY,
        SpecificationSharingPolicy.EMBARGOED_OR_PROPRIETARY,
    )
    allowed_types = SPECIFICATION_POLICY_ALLOWED_TYPES

    def setSharingPolicy(self, policy, user):
        with person_logged_in(user):
            result = self.distribution.setSpecificationSharingPolicy(policy)
        return result

    def getSharingPolicy(self):
        return self.distribution.specification_sharing_policy


class TestDistributionCurrentSourceReleases(
    CurrentSourceReleasesMixin, TestCase
):
    """Test for Distribution.getCurrentSourceReleases().

    This works in the same way as
    DistroSeries.getCurrentSourceReleases() works, except that we look
    for the latest published source across multiple distro series.
    """

    layer = LaunchpadFunctionalLayer
    release_interface = IDistributionSourcePackageRelease

    @property
    def target(self):
        return self.distribution

    def test_which_distroseries_does_not_matter(self):
        # When checking for the current release, we only care about the
        # version numbers. We don't care whether the version is
        # published in a earlier or later series.
        self.current_series = self.factory.makeDistroSeries(
            self.distribution, "1.0", status=SeriesStatus.CURRENT
        )
        self.publisher.getPubSource(
            version="0.9", distroseries=self.current_series
        )
        self.publisher.getPubSource(
            version="1.0", distroseries=self.development_series
        )
        self.assertCurrentVersion("1.0")

        self.publisher.getPubSource(
            version="1.1", distroseries=self.current_series
        )
        self.assertCurrentVersion("1.1")

    def test_distribution_series_cache(self):
        distribution = removeSecurityProxy(
            self.factory.makeDistribution("foo")
        )

        cache = get_property_cache(distribution)

        # Not yet cached.
        self.assertNotIn("series", cache)

        # Now cached.
        series = distribution.series
        self.assertIs(series, cache.series)

        # Cache cleared.
        distribution.newSeries(
            name="bar",
            display_name="Bar",
            title="Bar",
            summary="",
            description="",
            version="1",
            previous_series=None,
            registrant=self.factory.makePerson(),
        )
        self.assertNotIn("series", cache)

        # New cached value.
        series = distribution.series
        self.assertEqual(1, len(series))
        self.assertIs(series, cache.series)


class SeriesByStatusTests(TestCaseWithFactory):
    """Test IDistribution.getSeriesByStatus()."""

    layer = LaunchpadFunctionalLayer

    def test_get_none(self):
        distro = self.factory.makeDistribution()
        self.assertEqual(
            [], list(distro.getSeriesByStatus(SeriesStatus.FROZEN))
        )

    def test_get_current(self):
        distro = self.factory.makeDistribution()
        series = self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.CURRENT
        )
        self.assertEqual(
            [series], list(distro.getSeriesByStatus(SeriesStatus.CURRENT))
        )


class SeriesTests(TestCaseWithFactory):
    """Test IDistribution.getSeries() and friends."""

    layer = LaunchpadFunctionalLayer

    def test_get_none(self):
        distro = self.factory.makeDistribution()
        self.assertRaises(NoSuchDistroSeries, distro.getSeries, "astronomy")

    def test_get_by_name(self):
        distro = self.factory.makeDistribution()
        series = self.factory.makeDistroSeries(
            distribution=distro, name="dappere"
        )
        self.assertEqual(series, distro.getSeries("dappere"))

    def test_get_by_version(self):
        distro = self.factory.makeDistribution()
        series = self.factory.makeDistroSeries(
            distribution=distro, name="dappere", version="42.6"
        )
        self.assertEqual(series, distro.getSeries("42.6"))

    def test_development_series_alias(self):
        distro = self.factory.makeDistribution()
        with person_logged_in(distro.owner):
            distro.development_series_alias = "devel"
        self.assertRaises(
            NoSuchDistroSeries, distro.getSeries, "devel", follow_aliases=True
        )
        series = self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.DEVELOPMENT
        )
        self.assertRaises(NoSuchDistroSeries, distro.getSeries, "devel")
        self.assertEqual(
            series, distro.getSeries("devel", follow_aliases=True)
        )

    def test_getNonObsoleteSeries(self):
        distro = self.factory.makeDistribution()
        self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.OBSOLETE
        )
        current = self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.CURRENT
        )
        development = self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.DEVELOPMENT
        )
        experimental = self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.EXPERIMENTAL
        )
        self.assertContentEqual(
            [current, development, experimental],
            list(distro.getNonObsoleteSeries()),
        )


class DerivativesTests(TestCaseWithFactory):
    """Test IDistribution.derivatives."""

    layer = LaunchpadFunctionalLayer

    def test_derivatives(self):
        distro1 = self.factory.makeDistribution()
        distro2 = self.factory.makeDistribution()
        previous_series = self.factory.makeDistroSeries(distribution=distro1)
        series = self.factory.makeDistroSeries(
            distribution=distro2, previous_series=previous_series
        )
        self.assertContentEqual([series], distro1.derivatives)


class DistroSnapshotTestCase(TestCaseWithFactory):
    """A TestCase for distribution snapshots."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distribution = self.factory.makeDistribution(name="boobuntu")

    def test_snapshot(self):
        """Snapshots of distributions should not include marked attributes.

        Wrap an export with 'doNotSnapshot' to force the snapshot to not
        include that attribute.
        """
        snapshot = Snapshot(self.distribution, providing=IDistribution)
        omitted = [
            "archive_mirrors",
            "cdimage_mirrors",
            "series",
            "all_distro_archives",
        ]
        for attribute in omitted:
            self.assertFalse(
                hasattr(snapshot, attribute),
                "Snapshot should not include %s." % attribute,
            )


class TestDistributionPage(TestCaseWithFactory):
    """A TestCase for the distribution page."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp("foo.bar@canonical.com")
        self.distro = self.factory.makeDistribution(
            name="distro", displayname="distro"
        )
        self.admin = getUtility(IPersonSet).getByEmail("admin@canonical.com")
        self.simple_user = self.factory.makePerson()
        # Use a FakeLogger fixture to prevent Memcached warnings to be
        # printed to stdout while browsing pages.
        self.useFixture(FakeLogger())

    def test_distributionpage_addseries_link(self):
        """Verify that an admin sees the +addseries link."""
        login_person(self.admin)
        view = create_initialized_view(
            self.distro, "+index", principal=self.admin
        )
        series_matches = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "link to add a series",
                "a",
                attrs={
                    "href": canonical_url(self.distro, view_name="+addseries")
                },
                text="Add series",
            ),
            soupmatchers.Tag(
                "Active series and milestones widget",
                "h2",
                text="Active series and milestones",
            ),
        )
        self.assertThat(view.render(), series_matches)

    def test_distributionpage_addseries_link_noadmin(self):
        """Verify that a non-admin does not see the +addseries link
        nor the series header (since there is no series yet).
        """
        login_person(self.simple_user)
        view = create_initialized_view(
            self.distro, "+index", principal=self.simple_user
        )
        add_series_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "link to add a series",
                "a",
                attrs={
                    "href": canonical_url(self.distro, view_name="+addseries")
                },
                text="Add series",
            )
        )
        series_header_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Active series and milestones widget",
                "h2",
                text="Active series and milestones",
            )
        )
        self.assertThat(
            view.render(),
            Not(MatchesAny(add_series_match, series_header_match)),
        )

    def test_distributionpage_series_list_noadmin(self):
        """Verify that a non-admin does see the series list
        when there is a series.
        """
        self.factory.makeDistroSeries(
            distribution=self.distro, status=SeriesStatus.CURRENT
        )
        login_person(self.simple_user)
        view = create_initialized_view(
            self.distro, "+index", principal=self.simple_user
        )
        add_series_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "link to add a series",
                "a",
                attrs={
                    "href": canonical_url(self.distro, view_name="+addseries")
                },
                text="Add series",
            )
        )
        series_header_match = soupmatchers.HTMLContains(
            soupmatchers.Tag(
                "Active series and milestones widget",
                "h2",
                text="Active series and milestones",
            )
        )
        self.assertThat(view.render(), series_header_match)
        self.assertThat(view.render(), Not(add_series_match))


class DistroRegistrantTestCase(TestCaseWithFactory):
    """A TestCase for registrants and owners of a distribution.

    The registrant is the creator of the distribution (read-only field).
    The owner is really the maintainer.
    """

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.owner = self.factory.makePerson()
        self.registrant = self.factory.makePerson()

    def test_distro_registrant_owner_differ(self):
        distribution = self.factory.makeDistribution(
            name="boobuntu", owner=self.owner, registrant=self.registrant
        )
        self.assertNotEqual(distribution.owner, distribution.registrant)
        self.assertEqual(distribution.owner, self.owner)
        self.assertEqual(distribution.registrant, self.registrant)


class DistributionSet(TestCaseWithFactory):
    """Test case for `IDistributionSet`."""

    layer = ZopelessDatabaseLayer

    def test_implements_interface(self):
        self.assertThat(
            getUtility(IDistributionSet), Provides(IDistributionSet)
        )

    def test_getDerivedDistributions_finds_derived_distro(self):
        dsp = self.factory.makeDistroSeriesParent()
        derived_distro = dsp.derived_series.distribution
        distroset = getUtility(IDistributionSet)
        self.assertIn(derived_distro, distroset.getDerivedDistributions())

    def test_getDerivedDistributions_ignores_nonderived_distros(self):
        distroset = getUtility(IDistributionSet)
        nonderived_distro = self.factory.makeDistribution()
        self.assertNotIn(
            nonderived_distro, distroset.getDerivedDistributions()
        )

    def test_getDerivedDistributions_ignores_ubuntu_even_if_derived(self):
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.factory.makeDistroSeriesParent(
            derived_series=ubuntu.currentseries
        )
        distroset = getUtility(IDistributionSet)
        self.assertNotIn(ubuntu, distroset.getDerivedDistributions())

    def test_getDerivedDistribution_finds_each_distro_just_once(self):
        # Derived distros are not duplicated in the output of
        # getDerivedDistributions, even if they have multiple parents and
        # multiple derived series.
        dsp = self.factory.makeDistroSeriesParent()
        distro = dsp.derived_series.distribution
        other_series = self.factory.makeDistroSeries(distribution=distro)
        self.factory.makeDistroSeriesParent(derived_series=other_series)
        distroset = getUtility(IDistributionSet)
        self.assertEqual(1, len(list(distroset.getDerivedDistributions())))


class TestDistributionTranslations(TestCaseWithFactory):
    """A TestCase for accessing distro translations-related attributes."""

    layer = DatabaseFunctionalLayer

    def test_rosetta_expert(self):
        # Ensure rosetta-experts can set Distribution attributes
        # related to translations.
        distro = self.factory.makeDistribution()
        new_series = self.factory.makeDistroSeries(distribution=distro)
        group = self.factory.makeTranslationGroup()
        with celebrity_logged_in("rosetta_experts"):
            distro.translations_usage = ServiceUsage.LAUNCHPAD
            distro.translation_focus = new_series
            distro.translationgroup = group
            distro.translationpermission = TranslationPermission.CLOSED

    def test_translation_group_owner(self):
        # Ensure TranslationGroup owner for a Distribution can modify
        # all attributes related to distribution translations.
        distro = self.factory.makeDistribution()
        new_series = self.factory.makeDistroSeries(distribution=distro)
        group = self.factory.makeTranslationGroup()
        with celebrity_logged_in("admin"):
            distro.translationgroup = group

        new_group = self.factory.makeTranslationGroup()
        with person_logged_in(group.owner):
            distro.translations_usage = ServiceUsage.LAUNCHPAD
            distro.translation_focus = new_series
            distro.translationgroup = new_group
            distro.translationpermission = TranslationPermission.CLOSED


class DistributionOCIProjectAdminPermission(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_check_oci_project_admin_person(self):
        person1 = self.factory.makePerson()
        person2 = self.factory.makePerson()
        distro = self.factory.makeDistribution(oci_project_admin=person1)

        self.assertTrue(distro.canAdministerOCIProjects(person1))
        self.assertFalse(distro.canAdministerOCIProjects(person2))
        self.assertFalse(distro.canAdministerOCIProjects(None))

    def test_check_oci_project_admin_team(self):
        person1 = self.factory.makePerson()
        person2 = self.factory.makePerson()
        person3 = self.factory.makePerson()
        team = self.factory.makeTeam(owner=person1)
        distro = self.factory.makeDistribution(oci_project_admin=team)

        admin = self.factory.makeAdministrator()
        with person_logged_in(admin):
            person2.join(team)

        self.assertTrue(distro.canAdministerOCIProjects(team))
        self.assertTrue(distro.canAdministerOCIProjects(person1))
        self.assertTrue(distro.canAdministerOCIProjects(person2))
        self.assertFalse(distro.canAdministerOCIProjects(person3))
        self.assertFalse(distro.canAdministerOCIProjects(None))

    def test_check_oci_project_admin_without_any_admin(self):
        person1 = self.factory.makePerson()
        distro = self.factory.makeDistribution(oci_project_admin=None)

        self.assertFalse(distro.canAdministerOCIProjects(person1))
        self.assertFalse(distro.canAdministerOCIProjects(None))

    def test_check_oci_project_admin_user_and_distro_owner(self):
        admin = self.factory.makeAdministrator()
        owner = self.factory.makePerson()
        someone = self.factory.makePerson()
        distro = self.factory.makeDistribution(owner=owner)

        self.assertFalse(distro.canAdministerOCIProjects(someone))
        self.assertTrue(distro.canAdministerOCIProjects(owner))
        self.assertTrue(distro.canAdministerOCIProjects(admin))


class TestDistributionWebservice(OCIConfigHelperMixin, TestCaseWithFactory):
    """Test the IDistribution API.

    Some tests already exist in xx-distribution.rst.
    """

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson(displayname="Test Person")
        self.webservice = webservice_for_person(
            self.person,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

    def test_searchOCIProjects(self):
        name = self.factory.getUniqueUnicode("partial-")
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            first_name = self.factory.makeOCIProjectName(name=name)
            first_project = self.factory.makeOCIProject(
                pillar=distro, ociprojectname=first_name
            )
            self.factory.makeOCIProject(pillar=distro)
            distro_url = api_url(distro)

        response = self.webservice.named_get(
            distro_url, "searchOCIProjects", text="partial"
        )
        self.assertEqual(200, response.status, response.body)

        search_body = response.jsonBody()
        self.assertEqual(1, search_body["total_size"])
        self.assertEqual(name, search_body["entries"][0]["name"])
        with person_logged_in(self.person):
            self.assertEqual(
                self.webservice.getAbsoluteUrl(api_url(first_project)),
                search_body["entries"][0]["self_link"],
            )

    def test_oops_references_matching_distro(self):
        # The distro layer provides the context restriction, so we need to
        # check we can access context filtered references - e.g. on question.
        oopsid = "OOPS-abcdef1234"
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution()
            self.factory.makeQuestion(
                title="Crash with %s" % oopsid, target=distro
            )
            distro_url = api_url(distro)

        now = datetime.now(tz=timezone.utc)
        day = timedelta(days=1)

        yesterday_response = self.webservice.named_get(
            distro_url,
            "findReferencedOOPS",
            start_date=(now - day).isoformat(),
            end_date=now.isoformat(),
        )
        self.assertEqual([oopsid], yesterday_response.jsonBody())

        future_response = self.webservice.named_get(
            distro_url,
            "findReferencedOOPS",
            start_date=(now + day).isoformat(),
            end_date=(now + day).isoformat(),
        )
        self.assertEqual([], future_response.jsonBody())

    def test_oops_references_different_distro(self):
        # The distro layer provides the context restriction, so we need to
        # check the filter is tight enough - other contexts should not work.
        oopsid = "OOPS-abcdef1234"
        with person_logged_in(self.person):
            self.factory.makeQuestion(title="Crash with %s" % oopsid)
            distro = self.factory.makeDistribution()
            distro_url = api_url(distro)
        now = datetime.now(tz=timezone.utc)
        day = timedelta(days=1)

        empty_response = self.webservice.named_get(
            distro_url,
            "findReferencedOOPS",
            start_date=(now - day).isoformat(),
            end_date=now.isoformat(),
        )
        self.assertEqual([], empty_response.jsonBody())

    def test_setOCICredentials(self):
        # We can add OCI Credentials to the distribution
        self.setConfig()
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro.oci_project_admin = self.person
            distro_url = api_url(distro)

        resp = self.webservice.named_post(
            distro_url,
            "setOCICredentials",
            registry_url="http://registry.test",
            username="test-username",
            password="test-password",
            region="test-region",
        )

        self.assertEqual(200, resp.status)
        with person_logged_in(self.person):
            self.assertEqual(
                "http://registry.test", distro.oci_registry_credentials.url
            )
            credentials = distro.oci_registry_credentials.getCredentials()
            self.assertDictEqual(
                {
                    "username": "test-username",
                    "password": "test-password",
                    "region": "test-region",
                },
                credentials,
            )

    def test_setOCICredentials_no_oci_admin(self):
        # If there's no oci_project_admin to own the credentials, error
        self.setConfig()
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro_url = api_url(distro)

        resp = self.webservice.named_post(
            distro_url,
            "setOCICredentials",
            registry_url="http://registry.test",
        )

        self.assertEqual(400, resp.status)
        self.assertIn(b"no OCI Project Admin for this distribution", resp.body)

    def test_setOCICredentials_changes_credentials(self):
        # if we have existing credentials, we should change them
        self.setConfig()
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro.oci_project_admin = self.person
            credentials = self.factory.makeOCIRegistryCredentials()
            distro.oci_registry_credentials = credentials
            distro_url = api_url(distro)

        resp = self.webservice.named_post(
            distro_url,
            "setOCICredentials",
            registry_url="http://registry.test",
        )

        self.assertEqual(200, resp.status)
        with person_logged_in(self.person):
            self.assertEqual(
                "http://registry.test", distro.oci_registry_credentials.url
            )

    def test_deleteOCICredentials(self):
        # We can remove existing credentials
        self.setConfig()
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro.oci_project_admin = self.person
            credentials = self.factory.makeOCIRegistryCredentials()
            distro.oci_registry_credentials = credentials
            distro_url = api_url(distro)

        resp = self.webservice.named_post(distro_url, "deleteOCICredentials")

        self.assertEqual(200, resp.status)
        with person_logged_in(self.person):
            self.assertIsNone(distro.oci_registry_credentials)

    def test_getBestMirrorsForCountry_randomizes_results(self):
        """Make sure getBestMirrorsForCountry() randomizes its results."""
        login("foo.bar@canonical.com")
        ubuntu = getUtility(IDistributionSet).getByName("ubuntu")
        with StormStatementRecorder() as recorder:
            ubuntu.getBestMirrorsForCountry(None, MirrorContent.ARCHIVE)
        self.assertIn("ORDER BY random()", recorder.statements[0])

    def test_getBestMirrorsForCountry_appends_main_repo_to_the_end(self):
        """Make sure the main mirror is appended to the list of mirrors for a
        given country.
        """
        login("foo.bar@canonical.com")
        france = getUtility(ICountrySet)["FR"]
        main_mirror = getUtility(ILaunchpadCelebrities).ubuntu_archive_mirror
        mirrors = main_mirror.distribution.getBestMirrorsForCountry(
            france, MirrorContent.ARCHIVE
        )
        self.assertTrue(len(mirrors) > 1, "Not enough mirrors")
        self.assertEqual(main_mirror, mirrors[-1])

        main_mirror = getUtility(ILaunchpadCelebrities).ubuntu_cdimage_mirror
        mirrors = main_mirror.distribution.getBestMirrorsForCountry(
            france, MirrorContent.RELEASE
        )
        self.assertTrue(len(mirrors) > 1, "Not enough mirrors")
        self.assertEqual(main_mirror, mirrors[-1])

    def test_distribution_security_admin_unset(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro_url = api_url(distro)

        response = self.webservice.get(distro_url)
        json_body = response.jsonBody()
        self.assertEqual(200, response.status)
        self.assertIsNone(json_body["security_admin_link"])

    def test_distribution_security_admin_set(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro_url = api_url(distro)
            person = self.factory.makePerson()
            distro.security_admin = person
            person_url = "http://api.launchpad.test/devel" + api_url(person)

        response = self.webservice.get(distro_url)
        json_body = response.jsonBody()
        self.assertEqual(200, response.status)
        self.assertEqual(
            person_url,
            json_body["security_admin_link"],
        )

    def test_admin_can_set_distribution_security_admin(self):
        with admin_logged_in():
            distro = self.factory.makeDistribution()
            person = self.factory.makePerson()
            person_url = "http://api.launchpad.test/devel" + api_url(person)
            self.assertIsNone(distro.security_admin)
            admin_user = getUtility(IPersonSet).getByEmail(
                "admin@canonical.com"
            )
            distro_url = api_url(distro)

        webservice = webservice_for_person(
            admin_user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

        response = webservice.patch(
            distro_url,
            "application/json",
            json.dumps({"security_admin_link": person_url}),
        )
        self.assertEqual(209, response.status)
        with admin_logged_in():
            self.assertEqual(person, distro.security_admin)

    def test_distribution_owner_can_set_security_admin(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            self.assertIsNone(distro.security_admin)
            distro_url = api_url(distro)
            person_url = "http://api.launchpad.test/devel" + api_url(
                self.person
            )

        response = self.webservice.patch(
            distro_url,
            "application/json",
            json.dumps(
                {
                    "security_admin_link": person_url,
                }
            ),
        )
        self.assertEqual(209, response.status)

        with person_logged_in(self.person):
            self.assertEqual(self.person, distro.security_admin)

    def test_others_cannot_set_distribution_security_admin(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution()
            distro_url = api_url(distro)
            person_url = "http://api.launchpad.test/devel" + api_url(
                self.person
            )

        response = self.webservice.patch(
            distro_url,
            "application/json",
            json.dumps(
                {
                    "security_admin_link": person_url,
                }
            ),
        )
        self.assertEqual(401, response.status)

    def test_distribution_code_admin_unset(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro_url = api_url(distro)

        response = self.webservice.get(distro_url)
        json_body = response.jsonBody()
        self.assertEqual(200, response.status)
        self.assertIsNone(json_body["code_admin_link"])

    def test_distribution_code_admin_set(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            distro_url = api_url(distro)
            person = self.factory.makePerson()
            distro.code_admin = person
            person_url = "http://api.launchpad.test/devel" + api_url(person)

        response = self.webservice.get(distro_url)
        json_body = response.jsonBody()
        self.assertEqual(200, response.status)
        self.assertEqual(
            person_url,
            json_body["code_admin_link"],
        )

    def test_admin_can_set_distribution_code_admin(self):
        with admin_logged_in():
            distro = self.factory.makeDistribution()
            person = self.factory.makePerson()
            person_url = "http://api.launchpad.test/devel" + api_url(person)
            self.assertIsNone(distro.code_admin)
            admin_user = getUtility(IPersonSet).getByEmail(
                "admin@canonical.com"
            )
            distro_url = api_url(distro)

        webservice = webservice_for_person(
            admin_user,
            permission=OAuthPermission.WRITE_PUBLIC,
            default_api_version="devel",
        )

        response = webservice.patch(
            distro_url,
            "application/json",
            json.dumps({"code_admin_link": person_url}),
        )
        self.assertEqual(209, response.status)
        with admin_logged_in():
            self.assertEqual(person, distro.code_admin)

    def test_distribution_owner_can_set_code_admin(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution(owner=self.person)
            self.assertIsNone(distro.code_admin)
            distro_url = api_url(distro)
            person_url = "http://api.launchpad.test/devel" + api_url(
                self.person
            )

        response = self.webservice.patch(
            distro_url,
            "application/json",
            json.dumps({"code_admin_link": person_url}),
        )
        self.assertEqual(209, response.status)

        with person_logged_in(self.person):
            self.assertEqual(self.person, distro.code_admin)

    def test_others_cannot_set_distribution_code_admin(self):
        with person_logged_in(self.person):
            distro = self.factory.makeDistribution()
            distro_url = api_url(distro)
            person_url = "http://api.launchpad.test/devel" + api_url(
                self.person
            )

        response = self.webservice.patch(
            distro_url,
            "application/json",
            json.dumps({"code_admin_link": person_url}),
        )
        self.assertEqual(401, response.status)


class TestDistributionVulnerabilities(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def assert_newVulnerability_only_the_required_params(
        self, distribution, creator
    ):
        vulnerability = distribution.newVulnerability(
            status=VulnerabilityStatus.NEEDS_TRIAGE,
            information_type=InformationType.PUBLIC,
            creator=creator,
        )

        self.assertThat(
            vulnerability,
            MatchesStructure.byEquality(
                status=VulnerabilityStatus.NEEDS_TRIAGE,
                creator=creator,
                importance=BugTaskImportance.UNDECIDED,
                information_type=InformationType.PUBLIC,
                cve=None,
                description=None,
                notes=None,
                mitigation=None,
                importance_explanation=None,
                date_made_public=None,
            ),
        )

    def test_vulnerabilities_no_vulnerability_present(self):
        distribution = self.factory.makeDistribution()
        self.assertEqual(0, distribution.vulnerabilities.count())

    def test_vulnerabilities_vulnerabilities_present(self):
        distribution = self.factory.makeDistribution()
        first_vulnerability = self.factory.makeVulnerability(distribution)
        second_vulnerability = self.factory.makeVulnerability(distribution)
        self.assertEqual(
            {first_vulnerability, second_vulnerability},
            set(distribution.vulnerabilities),
        )

    def test_vulnerabilities_some_vulnerabilities_private(self):
        distribution = self.factory.makeDistribution(
            bug_sharing_policy=BugSharingPolicy.PROPRIETARY
        )
        public_vulnerabilities = set()
        for _ in range(5):
            public_vulnerabilities.add(
                self.factory.makeVulnerability(distribution=distribution)
            )

        private_vulnerability = self.factory.makeVulnerability(
            distribution=distribution,
            information_type=InformationType.PROPRIETARY,
        )
        person = self.factory.makePerson()
        person_with_access = self.factory.makePerson()
        grant_access_to_non_public_vulnerability(
            private_vulnerability,
            person_with_access,
        )
        with person_logged_in(person):
            self.assertCountEqual(
                public_vulnerabilities,
                distribution.vulnerabilities,
            )

        with person_logged_in(person_with_access):
            self.assertCountEqual(
                public_vulnerabilities.union([private_vulnerability]),
                distribution.vulnerabilities,
            )

    def test_set_security_admin_permissions(self):
        distribution = self.factory.makeDistribution()
        person = self.factory.makePerson()
        person2 = self.factory.makePerson()
        security_team = self.factory.makeTeam(members=[person])

        with person_logged_in(distribution.owner):
            distribution.security_admin = security_team
            self.assertEqual(security_team, distribution.security_admin)

        with admin_logged_in():
            distribution.security_admin = None
            self.assertIsNone(distribution.security_admin)

        with person_logged_in(person2), ExpectedException(Unauthorized):
            distribution.security_admin = security_team

        with anonymous_logged_in(), ExpectedException(Unauthorized):
            distribution.security_admin = security_team

    def test_newVulnerability_default_arguments(self):
        distribution = self.factory.makeDistribution()
        owner = distribution.owner

        with person_logged_in(owner):
            self.assert_newVulnerability_only_the_required_params(
                distribution, creator=owner
            )

    def test_newVulnerability_all_parameters(self):
        distribution = self.factory.makeDistribution()
        owner = distribution.owner
        cve = self.factory.makeCVE(sequence="2022-1234")
        now = datetime.now(timezone.utc)

        with person_logged_in(owner):
            # The distribution owner can create a new vulnerability in
            # the distribution.
            vulnerability = distribution.newVulnerability(
                status=VulnerabilityStatus.ACTIVE,
                creator=owner,
                importance=BugTaskImportance.CRITICAL,
                information_type=InformationType.PRIVATESECURITY,
                cve=cve,
                description="Vulnerability",
                notes="lgp171188> Foo bar",
                mitigation="Foo bar baz",
                importance_explanation="Foo bar baz",
                date_made_public=now,
            )
            self.assertThat(
                vulnerability,
                MatchesStructure.byEquality(
                    status=VulnerabilityStatus.ACTIVE,
                    creator=owner,
                    importance=BugTaskImportance.CRITICAL,
                    information_type=InformationType.PRIVATESECURITY,
                    cve=cve,
                    description="Vulnerability",
                    notes="lgp171188> Foo bar",
                    mitigation="Foo bar baz",
                    importance_explanation="Foo bar baz",
                    date_made_public=now,
                ),
            )

    def test_newVulnerability_permissions(self):
        person = self.factory.makePerson()
        distribution = self.factory.makeDistribution()
        security_team = self.factory.makeTeam(members=[person])

        # The distribution owner, admin, can create a new vulnerability
        # in the distribution.
        with person_logged_in(distribution.owner):
            self.assert_newVulnerability_only_the_required_params(
                distribution, creator=distribution.owner
            )

        with admin_logged_in():
            self.assert_newVulnerability_only_the_required_params(
                distribution, creator=person
            )
        self.factory.makeCommercialSubscription(pillar=distribution)

        with celebrity_logged_in("commercial_admin"):
            self.assert_newVulnerability_only_the_required_params(
                distribution, creator=person
            )

        with person_logged_in(distribution.owner):
            distribution.security_admin = security_team

        # When the security admin is set for the distribution,
        # users in that team can create a new vulnerability in the
        # distribution.
        with person_logged_in(person):
            self.assert_newVulnerability_only_the_required_params(
                distribution, creator=person
            )

    def test_newVulnerability_cannot_be_called_by_unprivileged_users(self):
        person = self.factory.makePerson()
        distribution = self.factory.makeDistribution()
        with person_logged_in(person), ExpectedException(Unauthorized):
            distribution.newVulnerability(
                status=VulnerabilityStatus.NEEDS_TRIAGE,
                information_type=InformationType.PUBLIC,
                creator=person,
            )

    def test_getVulnerability_non_existent_id(self):
        distribution = self.factory.makeDistribution()
        vulnerability = distribution.getVulnerability(9999999)
        self.assertIsNone(vulnerability)

    def test_getVulnerability(self):
        distribution = self.factory.makeDistribution()
        vulnerability = self.factory.makeVulnerability(distribution)
        self.assertEqual(
            vulnerability,
            distribution.getVulnerability(
                removeSecurityProxy(vulnerability).id
            ),
        )


class TestDistributionVulnerabilitiesWebService(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_vulnerability_api_url_data(self):
        distribution = self.factory.makeDistribution()
        person = distribution.owner
        cve = self.factory.makeCVE("2022-1234")
        now = datetime.now(timezone.utc)
        vulnerability = removeSecurityProxy(
            self.factory.makeVulnerability(
                distribution,
                creator=person,
                cve=cve,
                description="Foo bar baz",
                notes="Foo bar baz",
                mitigation="Foo bar baz",
                importance_explanation="Foo bar baz",
                date_made_public=now,
            )
        )
        vulnerability_url = api_url(vulnerability)

        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        with person_logged_in(person):
            distribution_url = webservice.getAbsoluteUrl(api_url(distribution))
            cve_url = webservice.getAbsoluteUrl(api_url(cve))
            creator_url = webservice.getAbsoluteUrl(api_url(person))

        response = webservice.get(vulnerability_url)
        self.assertEqual(200, response.status)

        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "id": Equals(vulnerability.id),
                    "distribution_link": Equals(distribution_url),
                    "cve_link": Equals(cve_url),
                    "creator_link": Equals(creator_url),
                    "status": Equals("Needs triage"),
                    "description": Equals("Foo bar baz"),
                    "notes": Equals("Foo bar baz"),
                    "mitigation": Equals("Foo bar baz"),
                    "importance": Equals("Undecided"),
                    "importance_explanation": Equals("Foo bar baz"),
                    "information_type": Equals("Public"),
                    "date_made_public": Equals(now.isoformat()),
                }
            ),
        )

    def test_vulnerability_api_url_invalid_id(self):
        person = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=person)
        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        with person_logged_in(person):
            distribution_url = api_url(distribution)
        invalid_vulnerability_url = distribution_url + "/+vulnerability/foo"
        response = webservice.get(invalid_vulnerability_url)
        self.assertEqual(404, response.status)

    def test_vulnerability_api_url_nonexistent_id(self):
        person = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=person)
        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        with person_logged_in(person):
            distribution_url = api_url(distribution)
        vulnerability_url = distribution_url + "/+vulnerability/99999999"
        response = webservice.get(vulnerability_url)
        self.assertEqual(404, response.status)

    def test_vulnerabilities_collection_link(self):
        person = self.factory.makePerson()
        distribution = self.factory.makeDistribution(owner=person)
        cve = self.factory.makeCVE("2022-1234")
        another_cve = self.factory.makeCVE("2022-1235")
        now = datetime.now(timezone.utc)

        first_vulnerability = removeSecurityProxy(
            self.factory.makeVulnerability(
                distribution,
                creator=person,
                cve=cve,
                description="Foo bar baz",
                notes="Foo bar baz",
                mitigation="Foo bar baz",
                importance_explanation="Foo bar baz",
                date_made_public=now,
            )
        )
        second_vulnerability = removeSecurityProxy(
            self.factory.makeVulnerability(
                distribution,
                creator=person,
                cve=another_cve,
                description="A B C",
                notes="A B C",
                mitigation="A B C",
                importance_explanation="A B C",
                date_made_public=now,
            )
        )

        distribution_url = api_url(distribution)
        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        with person_logged_in(person):
            distribution_url = webservice.getAbsoluteUrl(api_url(distribution))
            cve_url = webservice.getAbsoluteUrl(api_url(cve))
            another_cve_url = webservice.getAbsoluteUrl(api_url(another_cve))
            creator_url = webservice.getAbsoluteUrl(api_url(person))

        response = webservice.get(distribution_url)
        response_json = response.jsonBody()
        self.assertEqual(200, response.status)
        self.assertIn("vulnerabilities_collection_link", response_json)

        response = webservice.get(
            response_json["vulnerabilities_collection_link"]
        )
        response_json = response.jsonBody()
        self.assertEqual(200, response.status)
        self.assertEqual(2, len(response_json["entries"]))
        self.assertThat(
            response_json["entries"][0],
            ContainsDict(
                {
                    "id": Equals(first_vulnerability.id),
                    "distribution_link": Equals(distribution_url),
                    "cve_link": Equals(cve_url),
                    "creator_link": Equals(creator_url),
                    "status": Equals("Needs triage"),
                    "description": Equals("Foo bar baz"),
                    "notes": Equals("Foo bar baz"),
                    "mitigation": Equals("Foo bar baz"),
                    "importance": Equals("Undecided"),
                    "importance_explanation": Equals("Foo bar baz"),
                    "information_type": Equals("Public"),
                    "date_made_public": Equals(now.isoformat()),
                }
            ),
        )
        self.assertThat(
            response_json["entries"][1],
            ContainsDict(
                {
                    "id": Equals(second_vulnerability.id),
                    "distribution_link": Equals(distribution_url),
                    "cve_link": Equals(another_cve_url),
                    "creator_link": Equals(creator_url),
                    "status": Equals("Needs triage"),
                    "description": Equals("A B C"),
                    "notes": Equals("A B C"),
                    "mitigation": Equals("A B C"),
                    "importance": Equals("Undecided"),
                    "importance_explanation": Equals("A B C"),
                    "information_type": Equals("Public"),
                    "date_made_public": Equals(now.isoformat()),
                }
            ),
        )

    def test_newVulnerability_required_arguments_missing(self):
        distribution = self.factory.makeDistribution()
        owner = distribution.owner
        distribution_url = api_url(distribution)

        self.assertEqual(0, distribution.vulnerabilities.count())

        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_post(
            distribution_url,
            "newVulnerability",
        )
        self.assertEqual(400, response.status)
        self.assertEqual(
            {
                "status: Required input is missing.",
                "creator: Required input is missing.",
                "information_type: Required input is missing.",
            },
            set(response.body.decode().split("\n")),
        )

    def test_newVulnerability_default_arguments(self):
        distribution = self.factory.makeDistribution()
        owner = distribution.owner
        api_base = "http://api.launchpad.test/devel"
        distribution_url = api_base + api_url(distribution)
        owner_url = api_base + api_url(owner)

        self.assertEqual(0, distribution.vulnerabilities.count())

        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_post(
            distribution_url,
            "newVulnerability",
            status="Needs triage",
            information_type="Public",
            creator=owner_url,
        )
        self.assertEqual(200, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "distribution_link": Equals(distribution_url),
                    "id": IsInstance(int),
                    "cve_link": Is(None),
                    "creator_link": Equals(owner_url),
                    "status": Equals("Needs triage"),
                    "description": Is(None),
                    "notes": Is(None),
                    "mitigation": Is(None),
                    "importance": Equals("Undecided"),
                    "importance_explanation": Is(None),
                    "information_type": Equals("Public"),
                    "date_made_public": Is(None),
                }
            ),
        )

    def test_newVulnerability_security_admin(self):
        distribution = self.factory.makeDistribution()
        person = self.factory.makePerson()
        security_team = self.factory.makeTeam(members=[person])
        api_base = "http://api.launchpad.test/devel"
        distribution_url = api_base + api_url(distribution)
        owner_url = api_base + api_url(person)

        with person_logged_in(distribution.owner):
            distribution.security_admin = security_team

        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_post(
            distribution_url,
            "newVulnerability",
            status="Needs triage",
            information_type="Public",
            creator=owner_url,
        )
        self.assertEqual(200, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "distribution_link": Equals(distribution_url),
                    "id": IsInstance(int),
                    "cve_link": Is(None),
                    "creator_link": Equals(owner_url),
                    "status": Equals("Needs triage"),
                    "description": Is(None),
                    "notes": Is(None),
                    "mitigation": Is(None),
                    "importance": Equals("Undecided"),
                    "importance_explanation": Is(None),
                    "information_type": Equals("Public"),
                    "date_made_public": Is(None),
                }
            ),
        )

    def test_newVulnerability_unauthorized_users(self):
        distribution = self.factory.makeDistribution()
        person = self.factory.makePerson()
        api_base = "http://api.launchpad.test/devel"
        distribution_url = api_base + api_url(distribution)
        owner_url = api_base + api_url(person)

        webservice = webservice_for_person(
            person,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_post(
            distribution_url,
            "newVulnerability",
            status="Needs triage",
            information_type="Public",
            creator=owner_url,
        )
        self.assertEqual(401, response.status)

    def test_newVulnerability_all_parameters(self):
        distribution = self.factory.makeDistribution()
        owner = distribution.owner
        cve = self.factory.makeCVE(sequence="2022-1234")

        api_base = "http://api.launchpad.test/devel"
        distribution_url = api_base + api_url(distribution)
        owner_url = api_base + api_url(owner)
        cve_url = api_base + api_url(cve)
        now = datetime.now(timezone.utc)

        self.assertEqual(0, distribution.vulnerabilities.count())

        webservice = webservice_for_person(
            owner,
            permission=OAuthPermission.WRITE_PRIVATE,
            default_api_version="devel",
        )
        response = webservice.named_post(
            distribution_url,
            "newVulnerability",
            status="Active",
            information_type="Private",
            creator=owner_url,
            importance="Critical",
            cve=cve_url,
            description="Vulnerability Foo",
            notes="lgp171188> Foo bar",
            mitigation="Foo bar baz",
            importance_explanation="Foo bar bazz",
            date_made_public=now.isoformat(),
        )
        self.assertEqual(200, response.status)
        self.assertThat(
            response.jsonBody(),
            ContainsDict(
                {
                    "distribution_link": Equals(distribution_url),
                    "id": IsInstance(int),
                    "cve_link": Equals(cve_url),
                    "creator_link": Equals(owner_url),
                    "status": Equals("Active"),
                    "description": Equals("Vulnerability Foo"),
                    "notes": Equals("lgp171188> Foo bar"),
                    "mitigation": Equals("Foo bar baz"),
                    "importance": Equals("Critical"),
                    "importance_explanation": Equals("Foo bar bazz"),
                    "information_type": Equals("Private"),
                    "date_made_public": Equals(now.isoformat()),
                }
            ),
        )


class TestDistributionPublishedSources(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_has_published_sources_no_sources(self):
        distribution = self.factory.makeDistribution()
        self.assertFalse(distribution.has_published_sources)

    def test_has_published_sources(self):
        ubuntu = getUtility(IDistributionSet).getByName("ubuntu")
        self.assertTrue(ubuntu.all_distro_archives.count() > 0)
        self.assertTrue(ubuntu.has_published_sources)

    def test_has_published_sources_query_count(self):
        distribution = self.factory.makeDistribution()
        self.factory.makeSourcePackagePublishingHistory(
            archive=distribution.main_archive,
        )
        self.assertEqual(1, distribution.all_distro_archives.count())
        with StormStatementRecorder() as recorder1:
            self.assertTrue(distribution.has_published_sources)
        clear_property_cache(distribution)
        partner_archive = self.factory.makeArchive(
            distribution=distribution,
            purpose=ArchivePurpose.PARTNER,
        )
        self.factory.makeSourcePackagePublishingHistory(
            archive=getUtility(IArchiveSet).getByDistroAndName(
                distribution, partner_archive.name
            )
        )
        self.assertEqual(2, distribution.all_distro_archives.count())
        with StormStatementRecorder() as recorder2:
            self.assertTrue(distribution.has_published_sources)

        # Adding one or more archives to the distribution and publishing
        # source packages to them should not affect the number of queries
        # executed here.
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestDistributionSearchPPAs(TestCaseWithFactory):
    # XXX cjwatson 2023-09-11: See also lib/lp/soyuz/doc/distribution.rst
    # and lib/lp/soyuz/doc/distribution.rst for related doctests which
    # haven't yet been turned into unit tests.

    layer = DatabaseFunctionalLayer

    def test_private_invisible_by_unprivileged_user(self):
        distribution = self.factory.makeDistribution()
        archive = self.factory.makeArchive(
            distribution=distribution, private=True
        )
        with person_logged_in(self.factory.makePerson()) as user:
            self.assertNotIn(
                archive, distribution.searchPPAs(user=user, show_inactive=True)
            )

    def test_private_visible_by_owner(self):
        distribution = self.factory.makeDistribution()
        owner = self.factory.makePerson()
        archive = self.factory.makeArchive(
            distribution=distribution, owner=owner, private=True
        )
        with person_logged_in(owner):
            self.assertIn(
                archive,
                distribution.searchPPAs(user=owner, show_inactive=True),
            )

    def test_private_visible_by_uploader(self):
        distribution = self.factory.makeDistribution()
        archive = self.factory.makeArchive(
            distribution=distribution, private=True
        )
        uploader = self.factory.makePerson()
        getUtility(IArchivePermissionSet).newComponentUploader(
            archive, uploader, "main"
        )
        with person_logged_in(uploader):
            self.assertIn(
                archive,
                distribution.searchPPAs(user=uploader, show_inactive=True),
            )

    def test_private_visible_by_commercial_admin(self):
        distribution = self.factory.makeDistribution()
        archive = self.factory.makeArchive(
            distribution=distribution, private=True
        )
        with celebrity_logged_in("commercial_admin") as commercial_admin:
            self.assertIn(
                archive,
                distribution.searchPPAs(
                    user=commercial_admin, show_inactive=True
                ),
            )

    def test_private_visible_by_admin(self):
        distribution = self.factory.makeDistribution()
        archive = self.factory.makeArchive(
            distribution=distribution, private=True
        )
        with celebrity_logged_in("admin") as admin:
            self.assertIn(
                archive,
                distribution.searchPPAs(user=admin, show_inactive=True),
            )
