# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test Archive as a bug target."""

from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.bugs.interfaces.bug import CreateBugParams
from lp.bugs.interfaces.bugtask import IBugTaskSet, IllegalTarget
from lp.bugs.model.bugtask import bug_target_to_key
from lp.registry.model.archivesourcepackage import ArchiveSourcePackage
from lp.soyuz.enums import ArchivePurpose
from lp.testing import TestCaseWithFactory, person_logged_in
from lp.testing.layers import DatabaseFunctionalLayer


class TestArchiveBugTarget(TestCaseWithFactory):
    """Test Archive implementation of IBugTarget."""

    layer = DatabaseFunctionalLayer

    def test_bugtargetdisplayname(self):
        # Archive has a proper bug target display name
        ppa = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA,
            displayname="Test PPA",
        )
        expected = f"Test PPA from {ppa.owner.displayname}"
        self.assertEqual(expected, ppa.bugtargetdisplayname)

    def test_bugtargetname(self):
        # Archive bug target name is the reference
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        self.assertEqual(ppa.reference, ppa.bugtargetname)

    def test_getBugSummaryContextWhereClause(self):
        # Archive can generate bug summary context where clause
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        clause = removeSecurityProxy(ppa).getBugSummaryContextWhereClause()
        # Basic check that we get a clause
        self.assertIsNotNone(clause)

    def test_create_bug_on_ppa(self):
        # Can create a bug directly on a PPA
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        owner = ppa.owner
        with person_logged_in(owner):
            params = CreateBugParams(
                owner=owner,
                title="Test bug on PPA",
                comment="This is a test bug for the PPA",
            )
            bug = ppa.createBug(params)
            self.assertEqual(1, len(bug.bugtasks))
            bugtask = bug.bugtasks[0]
            self.assertEqual(ppa, bugtask.target)
            self.assertEqual(ppa, removeSecurityProxy(bugtask).archive)
            # Verify package-specific and other target columns are not set
            self.assertIsNone(bugtask.distribution)
            self.assertIsNone(bugtask.product)
            self.assertIsNone(bugtask.productseries)
            self.assertIsNone(removeSecurityProxy(bugtask).distroseries)
            self.assertIsNone(removeSecurityProxy(bugtask).sourcepackagename)
            self.assertIsNone(removeSecurityProxy(bugtask).ociproject)

    def test_cannot_create_bug_on_primary_archive(self):
        # Cannot create bug tasks on primary archives
        distro = self.factory.makeDistribution()
        primary_archive = distro.main_archive
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            params = CreateBugParams(
                owner=owner,
                title="Test bug on primary archive",
                comment="This should fail",
            )
            self.assertRaises(IllegalTarget, primary_archive.createBug, params)

    def test_cannot_create_bug_on_partner_archive(self):
        # Cannot create bug tasks on partner archives
        distro = self.factory.makeDistribution()
        partner_archive = self.factory.makeArchive(
            distribution=distro,
            purpose=ArchivePurpose.PARTNER,
        )
        owner = self.factory.makePerson()
        with person_logged_in(owner):
            params = CreateBugParams(
                owner=owner,
                title="Test bug on partner archive",
                comment="This should fail",
            )
            self.assertRaises(IllegalTarget, partner_archive.createBug, params)


class TestBugTaskArchiveTarget(TestCaseWithFactory):
    """Test BugTask with Archive targets."""

    layer = DatabaseFunctionalLayer

    def test_bug_target_to_key_for_archive(self):
        # bug_target_to_key returns archive + distribution for Archive targets
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        key = bug_target_to_key(ppa)

        # Archive and distribution should be set
        self.assertEqual(ppa, key["archive"])
        # Other columns should be None
        self.assertIsNone(key["distribution"])
        self.assertIsNone(key["product"])
        self.assertIsNone(key["productseries"])
        self.assertIsNone(key["distroseries"])
        self.assertIsNone(key["sourcepackagename"])
        self.assertIsNone(key["ociproject"])
        self.assertIsNone(key["packagetype"])
        self.assertIsNone(key["channel"])

    def test_create_bugtask_on_archive(self):
        # Can create a bugtask directly on an archive
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        bug = self.factory.makeBug()
        owner = ppa.owner

        with person_logged_in(owner):
            bugtask_set = getUtility(IBugTaskSet)
            bugtask = bugtask_set.createTask(
                bug=bug,
                owner=owner,
                target=ppa,
            )

            self.assertEqual(ppa, bugtask.target)
            self.assertEqual(ppa, removeSecurityProxy(bugtask).archive)
            # Verify package-specific columns are not set
            self.assertIsNone(bugtask.distribution)
            self.assertIsNone(bugtask.product)
            self.assertIsNone(bugtask.productseries)
            self.assertIsNone(removeSecurityProxy(bugtask).distroseries)
            self.assertIsNone(removeSecurityProxy(bugtask).sourcepackagename)
            self.assertIsNone(removeSecurityProxy(bugtask).ociproject)

    def test_bug_target_to_key_for_archive_source_package(self):
        # bug_target_to_key returns archive + distribution + sourcepackagename
        # for ArchiveSourcePackage targets
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        spn = self.factory.makeSourcePackageName()
        asp = ArchiveSourcePackage(ppa, spn)

        key = bug_target_to_key(asp)

        # Archive, distribution, and sourcepackagename should be set
        self.assertEqual(ppa, key["archive"])
        self.assertEqual(spn, key["sourcepackagename"])
        # Other columns should be None
        self.assertIsNone(key["distribution"])
        self.assertIsNone(key["product"])
        self.assertIsNone(key["productseries"])
        self.assertIsNone(key["distroseries"])
        self.assertIsNone(key["ociproject"])
        self.assertIsNone(key["packagetype"])
        self.assertIsNone(key["channel"])

    def test_create_bugtask_on_archive_source_package(self):
        # Can create a bugtask on an archive source package
        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        spn = self.factory.makeSourcePackageName()
        # Create publishing history so the package is actually in the PPA
        self.factory.makeSourcePackagePublishingHistory(
            archive=ppa, sourcepackagename=spn
        )
        asp = ArchiveSourcePackage(ppa, spn)
        bug = self.factory.makeBug()
        owner = ppa.owner

        with person_logged_in(owner):
            bugtask_set = getUtility(IBugTaskSet)
            bugtask = bugtask_set.createTask(
                bug=bug,
                owner=owner,
                target=asp,
            )

            self.assertEqual(asp, bugtask.target)
            self.assertEqual(ppa, removeSecurityProxy(bugtask).archive)
            self.assertEqual(
                spn, removeSecurityProxy(bugtask).sourcepackagename
            )
            # Verify other target columns are not set
            self.assertIsNone(bugtask.distribution)
            self.assertIsNone(bugtask.product)
            self.assertIsNone(bugtask.productseries)
            self.assertIsNone(removeSecurityProxy(bugtask).distroseries)
            self.assertIsNone(removeSecurityProxy(bugtask).ociproject)
