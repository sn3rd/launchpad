# Copyright 2010 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for BugTargets."""

from zope.security.interfaces import ForbiddenAttribute

from lp.bugs.interfaces.bugtarget import ISeriesBugTarget
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer


class BugTargetBugFilingDuplicateSearchAlwaysOn:
    """Base class for tests of bug targets where dupes are always searched."""

    def test_enable_bugfiling_duplicate_search(self):
        # enable_bugfiling_duplicate_search is always True.
        self.assertTrue(self.bugtarget.enable_bugfiling_duplicate_search)

    def test_enable_bugfiling_duplicate_search_is_read_only(self):
        # enable_bugfiling_duplicate_search is a read-only attribute
        with person_logged_in(self.bugtarget.owner):
            self.assertRaises(
                ForbiddenAttribute,
                setattr,
                self.bugtarget,
                "enable_bugfiling_duplicate_search",
                False,
            )


class TestDistribution(
    BugTargetBugFilingDuplicateSearchAlwaysOn, TestCaseWithFactory
):
    """Tests for distributions as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bugtarget = self.factory.makeDistribution()

    def test_bug_target_parent(self):
        self.assertEqual(self.bugtarget, self.bugtarget.bug_target_parent)


class TestDistroSeries(
    BugTargetBugFilingDuplicateSearchAlwaysOn, TestCaseWithFactory
):
    """Tests for distributions as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bugtarget = self.factory.makeDistroSeries()

    def test_bugtarget_parent(self):
        self.assertTrue(ISeriesBugTarget.providedBy(self.bugtarget))
        self.assertEqual(
            self.bugtarget.distribution, self.bugtarget.bugtarget_parent
        )

    def test_bug_target_parent_alias(self):
        self.assertEqual(
            self.bugtarget.distribution,
            self.bugtarget.bug_target_parent,
        )

    def test_series(self):
        self.assertEqual(self.bugtarget, self.bugtarget.series)


class TestProjectGroup(
    BugTargetBugFilingDuplicateSearchAlwaysOn, TestCaseWithFactory
):
    """Tests for distributions as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bugtarget = self.factory.makeProject()


class BugTargetBugFilingDuplicateSearchSettable:
    """A base class for tests of bug targets where dupe search is settable."""

    def test_enable_bugfiling_duplicate_search_default(self):
        # The default value of enable_bugfiling_duplicate_search is True.
        self.assertTrue(self.bugtarget.enable_bugfiling_duplicate_search)

    def test_enable_bugfiling_duplicate_search_is_changeable(self):
        # The bug supervisor can change enable_bugfiling_duplicate_search.
        with person_logged_in(self.bug_supervisor):
            self.bugtarget.enable_bugfiling_duplicate_search = False
        self.assertFalse(self.bugtarget.enable_bugfiling_duplicate_search)


class TestProduct(
    BugTargetBugFilingDuplicateSearchSettable, TestCaseWithFactory
):
    """Tests for products as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug_supervisor = self.factory.makePerson()
        self.bugtarget = self.factory.makeProduct(
            bug_supervisor=self.bug_supervisor
        )

    def test_bug_target_parent(self):
        self.assertEqual(self.bugtarget, self.bugtarget.bug_target_parent)


class TestDistributionSourcePackage(
    BugTargetBugFilingDuplicateSearchSettable, TestCaseWithFactory
):
    """Tests for distributionsourcepackages as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug_supervisor = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            bug_supervisor=self.bug_supervisor
        )
        self.bugtarget = self.factory.makeDistributionSourcePackage(
            distribution=distribution
        )

    def test_bug_target_parent(self):
        self.assertEqual(
            self.bugtarget.distribution,
            self.bugtarget.bug_target_parent,
        )


class BugTargetBugFilingDuplicateSearchInherited:
    """A base class for tests of bug targets where the dupe search policy
    is inherited from a parent object.
    """

    def test_enable_bugfiling_duplicate_search_default(self):
        # The default value of enable_bugfiling_duplicate_search is True.
        self.assertTrue(self.bugtarget.enable_bugfiling_duplicate_search)

    def test_enable_bugfiling_duplicate_search_changed_by_parent_change(self):
        # If enable_bugfiling_duplicate_search is changed for the parent
        # object, it is changed for the bug target too.
        with person_logged_in(self.bug_supervisor):
            parent = self.bugtarget.bugtarget_parent
            parent.enable_bugfiling_duplicate_search = False
        self.assertFalse(self.bugtarget.enable_bugfiling_duplicate_search)


class TestProductSeries(
    BugTargetBugFilingDuplicateSearchInherited, TestCaseWithFactory
):
    """Tests for product series as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug_supervisor = self.factory.makePerson()
        self.bugtarget = self.factory.makeProductSeries(
            product=self.factory.makeProduct(
                bug_supervisor=self.bug_supervisor
            )
        )

    def test_bugtarget_parent(self):
        self.assertTrue(ISeriesBugTarget.providedBy(self.bugtarget))
        self.assertEqual(
            self.bugtarget.product, self.bugtarget.bugtarget_parent
        )

    def test_bug_target_parent_alias(self):
        self.assertEqual(
            self.bugtarget.product,
            self.bugtarget.bug_target_parent,
        )

    def test_series(self):
        self.assertEqual(self.bugtarget, self.bugtarget.series)


class TestSourcePackage(
    BugTargetBugFilingDuplicateSearchInherited, TestCaseWithFactory
):
    """Tests for source packages as bug targets."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.bug_supervisor = self.factory.makePerson()
        distribution = self.factory.makeDistribution(
            bug_supervisor=self.bug_supervisor
        )
        distroseries = self.factory.makeDistroSeries(distribution=distribution)
        self.bugtarget = self.factory.makeSourcePackage(
            distroseries=distroseries
        )

    def test_bugtarget_parent(self):
        self.assertTrue(ISeriesBugTarget.providedBy(self.bugtarget))
        self.assertEqual(
            self.bugtarget.distribution_sourcepackage,
            self.bugtarget.bugtarget_parent,
        )

    def test_bug_target_parent_alias(self):
        self.assertEqual(
            self.bugtarget.distroseries.distribution,
            self.bugtarget.bug_target_parent,
        )

    def test_series(self):
        self.assertEqual(self.bugtarget.distroseries, self.bugtarget.series)
