# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ExternalPackage."""

from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bugtargetparent import IBugTargetParent
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.model.externalpackage import ExternalPackage
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestExternalPackage(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "mypackage"
        )
        self.channel = "12.81/edge/myfix"
        self.distribution = self.factory.makeDistribution(name="mydistro")

        self.externalpackage = self.distribution.getExternalPackage(
            name=self.sourcepackagename,
            packagetype=ExternalPackageType.SNAP,
            channel=self.channel,
        )
        self.externalpackage_matching = self.distribution.getExternalPackage(
            name=self.sourcepackagename,
            packagetype=ExternalPackageType.SNAP,
            channel=("22", "candidate", "staging"),
        )
        self.externalpackage_maven = self.distribution.getExternalPackage(
            name=self.sourcepackagename,
            packagetype=ExternalPackageType.MAVEN,
            channel=None,
        )
        self.externalpackage_copy = ExternalPackage(
            self.distribution,
            sourcepackagename=self.sourcepackagename,
            packagetype=ExternalPackageType.SNAP,
            channel=self.channel,
        )

    def test_repr(self):
        """Test __repr__ function"""
        self.assertEqual(
            "<ExternalPackage 'mypackage - Snap @12.81/edge/myfix in "
            "Mydistro'>",
            self.externalpackage.__repr__(),
        )
        self.assertEqual(
            "<ExternalPackage 'mypackage - Maven in Mydistro'>",
            self.externalpackage_maven.__repr__(),
        )

    def test_name(self):
        """Test name property"""
        self.assertEqual("mypackage", self.externalpackage.name)
        self.assertEqual("mypackage", self.externalpackage_maven.name)

    def test_display_channel(self):
        """Test display_channel property"""
        self.assertEqual(
            self.externalpackage.display_channel, "12.81/edge/myfix"
        )
        self.assertEqual(self.externalpackage_maven.display_channel, None)

        removeSecurityProxy(self.externalpackage).channel = (
            "12.81",
            "candidate",
            None,
        )
        self.assertEqual(
            "12.81/candidate", self.externalpackage.display_channel
        )

    def test_channel_fields(self):
        """Test channel fields when creating an ExternalPackage"""
        # Channel is not a str, tuple or list
        self.assertRaises(
            ValueError,
            ExternalPackage,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.SNAP,
            {},
        )
        self.assertRaises(
            ValueError,
            ExternalPackage,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.CHARM,
            16,
        )
        # Channel risk is missing
        self.assertRaises(
            ValueError,
            ExternalPackage,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.ROCK,
            "16",
        )
        # Branch name is also risk name
        self.assertRaises(
            ValueError,
            ExternalPackage,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.ROCK,
            "16/stable/stable",
        )
        # Invalid risk name
        self.assertRaises(
            ValueError,
            ExternalPackage,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.ROCK,
            "16/foo/bar",
        )

    def test_display_name(self):
        """Test display_name property without channel"""
        self.assertEqual(
            "mypackage - Maven in Mydistro",
            self.externalpackage_maven.display_name,
        )

    def test_display_name_with_channel(self):
        """Test display_name property with channel"""
        self.assertEqual(
            "mypackage - Snap @12.81/edge/myfix in Mydistro",
            self.externalpackage.display_name,
        )

    def test_compare(self):
        """Test __eq__ and __neq__"""
        self.assertEqual(self.externalpackage, self.externalpackage_copy)
        self.assertNotEqual(
            self.externalpackage, self.externalpackage_matching
        )
        self.assertNotEqual(self.externalpackage, self.externalpackage_maven)

    def test_hash(self):
        """Test __hash__"""
        self.assertEqual(
            removeSecurityProxy(self.externalpackage).__hash__(),
            removeSecurityProxy(self.externalpackage_copy).__hash__(),
        )
        self.assertNotEqual(
            removeSecurityProxy(self.externalpackage).__hash__(),
            removeSecurityProxy(self.externalpackage_maven).__hash__(),
        )

    def test_pillar(self):
        """Test pillar property"""
        self.assertEqual(self.externalpackage.pillar, self.distribution)

    def test_official_bug_tags(self):
        """Test official_bug_tags property"""
        self.assertEqual(
            self.externalpackage.official_bug_tags,
            self.distribution.official_bug_tags,
        )

    def test__getOfficialTagClause(self):
        """Test _getOfficialTagClause"""
        self.assertEqual(
            self.distribution._getOfficialTagClause(),
            self.externalpackage._getOfficialTagClause(),
        )

    def test_drivers_are_distributions(self):
        """Drivers property returns the drivers for the distribution."""
        self.assertNotEqual([], self.distribution.drivers)
        self.assertEqual(
            self.externalpackage.drivers, self.distribution.drivers
        )

    def test_personHasDriverRights(self):
        """A distribution driver has driver permissions on an
        externalpackage."""
        driver = self.distribution.drivers[0]
        self.assertTrue(self.externalpackage.personHasDriverRights(driver))

    def test_bug_target_parent(self):
        externalpackage = self.externalpackage
        parent = externalpackage.bug_target_parent

        # The parent should not be None.
        self.assertIsNotNone(parent)

        # The parent should provide the IDistribution interface.
        self.assertTrue(IDistribution.providedBy(parent))

        # The parent should provide the IBugTargetParent interface.
        self.assertTrue(IBugTargetParent.providedBy(parent))

        # The parent should be the distribution itself.
        self.assertEqual(self.distribution, parent)
