# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ExternalPackageSeries."""

from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bugtargetparent import IBugTargetParent
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.model.externalpackage import ExternalPackage
from lp.registry.model.externalpackageseries import ExternalPackageSeries
from lp.testing import TestCaseWithFactory
from lp.testing.layers import DatabaseFunctionalLayer


class TestExternalPackageSeries(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()

        self.sourcepackagename = self.factory.getOrMakeSourcePackageName(
            "mypackage"
        )
        self.channel = "14.90-test/edge/myfix"
        self.distribution = self.factory.makeDistribution(name="mydistro")
        self.distroseries = self.factory.makeDistroSeries(
            distribution=self.distribution, name="mydistroseries"
        )

        self.externalpackageseries = (
            self.distroseries.getExternalPackageSeries(
                name=self.sourcepackagename,
                packagetype=ExternalPackageType.SNAP,
                channel=self.channel,
            )
        )
        self.externalpackageseries_matching = (
            self.distroseries.getExternalPackageSeries(
                name=self.sourcepackagename,
                packagetype=ExternalPackageType.SNAP,
                channel=("22", "candidate", "staging"),
            )
        )
        self.externalpackageseries_maven = (
            self.distroseries.getExternalPackageSeries(
                name=self.sourcepackagename,
                packagetype=ExternalPackageType.MAVEN,
                channel=None,
            )
        )
        self.externalpackageseries_copy = ExternalPackageSeries(
            self.distroseries,
            sourcepackagename=self.sourcepackagename,
            packagetype=ExternalPackageType.SNAP,
            channel=self.channel,
        )

    def test_repr(self):
        """Test __repr__ function"""
        self.assertEqual(
            "<ExternalPackageSeries 'mypackage - Snap @14.90-test/edge/myfix "
            "in Mydistroseries'>",
            self.externalpackageseries.__repr__(),
        )
        self.assertEqual(
            "<ExternalPackageSeries 'mypackage - Maven in Mydistroseries'>",
            self.externalpackageseries_maven.__repr__(),
        )

    def test_name(self):
        """Test name property"""
        self.assertEqual("mypackage", self.externalpackageseries.name)
        self.assertEqual("mypackage", self.externalpackageseries_maven.name)

    def test_distribution(self):
        """Test distribution property"""
        self.assertEqual(
            self.distribution, self.externalpackageseries.distribution
        )
        self.assertEqual(
            self.distribution, self.externalpackageseries_maven.distribution
        )

    def test_series(self):
        """Test series property"""
        self.assertEqual(self.distroseries, self.externalpackageseries.series)
        self.assertEqual(
            self.distroseries, self.externalpackageseries_maven.series
        )

    def test_display_channel(self):
        """Test display_channel property"""
        self.assertEqual(
            self.externalpackageseries.display_channel, "14.90-test/edge/myfix"
        )
        self.assertEqual(
            self.externalpackageseries_maven.display_channel, None
        )

        removeSecurityProxy(self.externalpackageseries).channel = (
            "12.81",
            "candidate",
            None,
        )
        self.assertEqual(
            "12.81/candidate", self.externalpackageseries.display_channel
        )

    def test_channel_fields(self):
        """Test channel fields when creating an ExternalPackageSeries"""
        # Valid channel is str, tuple or list
        self.assertRaises(
            ValueError,
            ExternalPackageSeries,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.SNAP,
            {},
        )
        self.assertRaises(
            ValueError,
            ExternalPackageSeries,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.CHARM,
            16,
        )
        # Channel risk is missing
        self.assertRaises(
            ValueError,
            ExternalPackageSeries,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.ROCK,
            "16",
        )
        # Branch name is also risk name
        self.assertRaises(
            ValueError,
            ExternalPackageSeries,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.ROCK,
            "16/stable/stable",
        )
        # Invalid risk name
        self.assertRaises(
            ValueError,
            ExternalPackageSeries,
            self.distribution,
            self.sourcepackagename,
            ExternalPackageType.ROCK,
            "16/foo/bar",
        )

    def test_display_name(self):
        """Test display_name property without channel"""
        self.assertEqual(
            "mypackage - Maven in Mydistroseries",
            self.externalpackageseries_maven.display_name,
        )

    def test_display_name_with_channel(self):
        """Test display_name property with channel"""
        self.assertEqual(
            "mypackage - Snap @14.90-test/edge/myfix in Mydistroseries",
            self.externalpackageseries.display_name,
        )

    def test_bugtarget_parent(self):
        """The bugtarget parent is an ExternalPackage with the same
        sourcepackagename, packagetype and channel."""
        expected = ExternalPackage(
            distribution=self.externalpackageseries.distribution,
            sourcepackagename=self.externalpackageseries.sourcepackagename,
            packagetype=self.externalpackageseries.packagetype,
            channel=removeSecurityProxy(self.externalpackageseries.channel),
        )
        self.assertEqual(expected, self.externalpackageseries.bugtarget_parent)

    def test_compare(self):
        """Test __eq__ and __neq__"""
        self.assertEqual(
            self.externalpackageseries, self.externalpackageseries_copy
        )
        self.assertNotEqual(
            self.externalpackageseries, self.externalpackageseries_matching
        )
        self.assertNotEqual(
            self.externalpackageseries, self.externalpackageseries_maven
        )

    def test_hash(self):
        """Test __hash__"""
        self.assertEqual(
            removeSecurityProxy(self.externalpackageseries).__hash__(),
            removeSecurityProxy(self.externalpackageseries_copy).__hash__(),
        )
        self.assertNotEqual(
            removeSecurityProxy(self.externalpackageseries).__hash__(),
            removeSecurityProxy(self.externalpackageseries_maven).__hash__(),
        )

    def test_pillar(self):
        """Test pillar property"""
        self.assertEqual(
            self.externalpackageseries.pillar, self.distroseries.pillar
        )

    def test_official_bug_tags(self):
        """Test official_bug_tags property"""
        self.assertEqual(
            self.externalpackageseries.official_bug_tags,
            self.distroseries.official_bug_tags,
        )

    @property
    def test_bug_reporting_guidelines(self):
        """Test bug_reporting_guidelines property"""
        self.assertEqual(
            self.distribution.bug_reporting_guidelines,
            self.externalpackageseries.bug_reporting_guidelines,
        )

    @property
    def test_content_templates(self):
        """Test content_templates property"""
        self.assertEqual(
            self.distribution.content_templates,
            self.externalpackageseries.content_templates,
        )

    @property
    def test_bug_reported_acknowledgement(self):
        """Test bug_reported_acknowledgement property"""
        self.assertEqual(
            self.distribution.bug_reported_acknowledgement,
            self.externalpackageseries.bug_reported_acknowledgement,
        )

    def test__getOfficialTagClause(self):
        """Test _getOfficialTagClause"""
        self.assertEqual(
            self.distroseries._getOfficialTagClause(),
            self.externalpackageseries._getOfficialTagClause(),
        )

    def test_drivers_are_distributions(self):
        """Drivers property returns the drivers for the distribution."""
        self.assertNotEqual([], self.distroseries.drivers)
        self.assertEqual(
            self.externalpackageseries.drivers, self.distroseries.drivers
        )

    def test_personHasDriverRights(self):
        """A distribution driver has driver permissions on an
        externalpackageseries."""
        driver = self.distroseries.drivers[0]
        self.assertTrue(
            self.externalpackageseries.personHasDriverRights(driver)
        )

    def test_bug_target_parent(self):
        externalpackageseries = self.externalpackageseries
        parent = externalpackageseries.bug_target_parent

        # The parent should not be None.
        self.assertIsNotNone(parent)

        # The parent should provide the IDistribution interface.
        self.assertTrue(IDistribution.providedBy(parent))

        # The parent should provide the IBugTargetParent interface.
        self.assertTrue(IBugTargetParent.providedBy(parent))

        # The parent should be the distribution itself.
        self.assertEqual(self.distribution, parent)
