# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Functional tests for publish-distro.py script."""

import os
import shutil
import subprocess
from optparse import OptionValueError
from pathlib import Path
from unittest.mock import call

from fixtures import MockPatch
from storm.store import Store
from testscenarios import WithScenarios, load_tests_apply_scenarios
from testtools.matchers import Not, PathExists
from testtools.twistedsupport import AsynchronousDeferredRunTest
from twisted.internet import defer
from zope.component import getUtility
from zope.security.proxy import ProxyFactory, removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    IArchiveGPGSigningKey,
)
from lp.archivepublisher.interfaces.archivepublisherrun import (
    ArchivePublisherRunStatus,
    IArchivePublisherRunSet,
)
from lp.archivepublisher.interfaces.archivepublishinghistory import (
    IArchivePublishingHistorySet,
)
from lp.archivepublisher.interfaces.publisherconfig import IPublisherConfigSet
from lp.archivepublisher.model.archivepublisherrun import ArchivePublisherRun
from lp.archivepublisher.publishing import GLOBAL_PUBLISHER_LOCK, Publisher
from lp.archivepublisher.scripts.publishdistro import (
    ARCHIVEPUBLISHER_HISTORY_ENABLED,
    PUBLISHER_DIRECT_INDEXES,
    PublishDistro,
)
from lp.archivepublisher.tests.artifactory_fixture import (
    FakeArtifactoryFixture,
)
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.features.testing import FeatureFixture
from lp.services.log.logger import BufferLogger
from lp.services.osutils import write_file
from lp.services.scripts.base import LOCK_PATH, LaunchpadScriptFailure
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveRepositoryFormat,
    ArchiveStatus,
    BinaryPackageFileType,
    BinaryPackageFormat,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.model.publishing import SourcePackagePublishingHistory
from lp.soyuz.tests.test_publishing import TestNativePublishingBase
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import switch_dbuser
from lp.testing.fakemethod import FakeMethod
from lp.testing.faketransaction import FakeTransaction
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import ZopelessDatabaseLayer


class TestPublishDistro(WithScenarios, TestNativePublishingBase):
    """Test the publish-distro.py script works properly."""

    scenarios = [
        ("direct_indexes", {"use_direct_indexes": True}),
        ("ftp_archive", {"use_direct_indexes": False}),
    ]

    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        if self.use_direct_indexes:
            self.useFixture(FeatureFixture({PUBLISHER_DIRECT_INDEXES: "on"}))

    def runPublishDistro(self, extra_args=None, distribution="ubuntutest"):
        """Run publish-distro without invoking the script.

        This method hooks into the publishdistro module to run the
        publish-distro script without the overhead of using Popen.
        """
        args = ["-d", distribution]
        if extra_args is not None:
            args.extend(extra_args)
        publish_distro = PublishDistro(test_args=args)
        self.logger = BufferLogger()
        publish_distro.logger = self.logger
        publish_distro.txn = self.layer.txn
        switch_dbuser(config.archivepublisher.dbuser)
        publish_distro.main()
        switch_dbuser("launchpad")

    def runPublishDistroScript(self):
        """Run publish-distro.py, returning the result and output."""
        script = os.path.join(config.root, "scripts", "publish-distro.py")
        args = [script, "-v", "-d", "ubuntutest"]
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
        return (process.returncode, stdout, stderr)

    def loadPubSource(self, spph_id):
        """Load a source package publishing history row from the DB.

        `PublishDistro.processArchive` resets the store for performance
        reasons, so tests need to reload database objects from scratch after
        calling it.
        """
        return ProxyFactory(
            IStore(SourcePackagePublishingHistory).get(
                SourcePackagePublishingHistory, spph_id
            )
        )

    def testPublishDistroRun(self):
        """Try a simple publish-distro run.

        Expect database publishing record to be updated to PUBLISHED and
        the file to be written in disk.

        This method also ensures the publish-distro.py script is runnable.
        """
        pub_source_id = self.getPubSource(filecontent=b"foo").id
        self.layer.txn.commit()

        rc, out, err = self.runPublishDistroScript()

        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(0, rc, "Publisher failed with:\n%s\n%s" % (out, err))
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)

        foo_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        with open(foo_path) as foo:
            self.assertEqual(foo.read().strip(), "foo")

    def testDirtySuiteProcessing(self):
        """Test dirty suite processing.

        Make a DELETED source to see if the dirty suite processing works for
        deletions.
        """
        pub_source_id = self.getPubSource(filecontent=b"foo").id
        self.layer.txn.commit()
        self.runPublishDistro()
        pub_source = self.loadPubSource(pub_source_id)

        random_person = getUtility(IPersonSet).getByName("name16")
        pub_source.requestDeletion(random_person)
        self.layer.txn.commit()
        self.assertTrue(
            pub_source.scheduleddeletiondate is None,
            "pub_source.scheduleddeletiondate should not be set, and it is.",
        )
        self.runPublishDistro()
        pub_source = self.loadPubSource(pub_source_id)
        self.assertTrue(
            pub_source.scheduleddeletiondate is not None,
            "pub_source.scheduleddeletiondate should be set, and it's not.",
        )

    def assertExists(self, path):
        """Assert if the given path exists."""
        self.assertTrue(os.path.exists(path), "Not Found: '%s'" % path)

    def assertNotExists(self, path):
        """Assert if the given path does not exist."""
        self.assertFalse(os.path.exists(path), "Found: '%s'" % path)

    def testRunWithSuite(self):
        """Try to run publish-distro with restricted suite option.

        Expect only update and disk writing only in the publishing record
        targeted to the specified suite, other records should be untouched
        and not present in disk.
        """
        pub_source_id = self.getPubSource(filecontent=b"foo").id
        pub_source2_id = self.getPubSource(
            sourcename="baz",
            filecontent=b"baz",
            distroseries=self.ubuntutest["hoary-test"],
        ).id
        self.layer.txn.commit()

        self.runPublishDistro(["-s", "hoary-test"])

        pub_source = self.loadPubSource(pub_source_id)
        pub_source2 = self.loadPubSource(pub_source2_id)
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PUBLISHED)

        foo_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        self.assertNotExists(foo_path)

        baz_path = "%s/main/b/baz/baz_666.dsc" % self.pool_dir
        with open(baz_path) as baz:
            self.assertEqual("baz", baz.read().strip())

    def publishToArchiveWithOverriddenDistsroot(self, archive):
        """Publish a test package to the specified archive.

        Publishes a test package but overrides the distsroot.
        :return: A tuple of the path to the overridden distsroot and the
                 configured distsroot, in that order.
        """
        self.getPubSource(filecontent=b"flangetrousers", archive=archive)
        self.layer.txn.commit()
        pubconf = getPubConfig(archive)
        tmp_path = os.path.join(pubconf.archiveroot, "tmpdistroot")
        if os.path.exists(tmp_path):
            shutil.rmtree(tmp_path)
        os.makedirs(tmp_path)
        myargs = ["-R", tmp_path]
        if archive.purpose == ArchivePurpose.PARTNER:
            myargs.append("--partner")
        self.runPublishDistro(myargs)
        return tmp_path, pubconf.distsroot

    def testDistsrootOverridePrimaryArchive(self):
        """Test the -R option to publish-distro.

        Make sure that -R works with the primary archive.
        """
        main_archive = getUtility(IDistributionSet)["ubuntutest"].main_archive
        tmp_path, distsroot = self.publishToArchiveWithOverriddenDistsroot(
            main_archive
        )
        distroseries = "breezy-autotest"
        self.assertExists(os.path.join(tmp_path, distroseries, "Release"))
        self.assertNotExists(
            os.path.join("%s" % distsroot, distroseries, "Release")
        )
        shutil.rmtree(tmp_path)

    def testDistsrootOverridePartnerArchive(self):
        """Test the -R option to publish-distro.

        Make sure the -R option affects the partner archive.
        """
        ubuntu = getUtility(IDistributionSet)["ubuntutest"]
        partner_archive = ubuntu.getArchiveByComponent("partner")
        tmp_path, distsroot = self.publishToArchiveWithOverriddenDistsroot(
            partner_archive
        )
        distroseries = "breezy-autotest"
        self.assertExists(os.path.join(tmp_path, distroseries, "Release"))
        self.assertNotExists(
            os.path.join("%s" % distsroot, distroseries, "Release")
        )
        shutil.rmtree(tmp_path)

    def setUpRequireSigningKeys(self):
        config.push(
            "ppa-require-signing-keys",
            """
            [personalpackagearchive]
            require_signing_keys: true
            """,
        )
        self.addCleanup(config.pop, "ppa-require-signing-keys")

    def testForPPAWithoutSigningKey(self):
        """publish-distro skips PPAs that do not yet have a signing key."""
        self.setUpRequireSigningKeys()
        cprov = getUtility(IPersonSet).getByName("cprov")
        pub_source_id = self.getPubSource(archive=cprov.archive).id
        removeSecurityProxy(cprov.archive).distribution = self.ubuntutest
        self.layer.txn.commit()
        self.runPublishDistro(["--ppa"])
        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(PackagePublishingStatus.PENDING, pub_source.status)

    def setUpOVALDataRsync(self):
        self.oval_data_root = self.makeTemporaryDirectory()
        self.pushConfig(
            "archivepublisher",
            oval_data_rsync_endpoint="oval.internal::oval/",
            oval_data_root=self.oval_data_root,
            oval_data_rsync_timeout=90,
        )

    def testPublishDistroOVALDataRsyncEndpointNotConfigured(self):
        """
        Test what happens when the OVAL data rsync endpoint is not configured.
        """
        mock_subprocess_check_call = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        ).mock
        self.runPublishDistro()
        mock_subprocess_check_call.assert_not_called()
        expected_log_line = (
            "INFO Skipping the OVAL data sync as no rsync endpoint has been "
            "configured."
        )
        self.assertTrue(expected_log_line in self.logger.getLogBuffer())

    def testPublishDistroOVALDataRsyncEndpointConfigured(self):
        """
        Test the OVAL data rsync command.

        When the endpoint is configured, verify that the command is run.
        """
        self.setUpOVALDataRsync()
        mock_subprocess_check_call = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        ).mock
        self.runPublishDistro()
        call_args = [
            "/usr/bin/rsync",
            "-a",
            "-q",
            "--timeout=90",
            "--delete",
            "--delete-after",
            "oval.internal::oval/",
            self.oval_data_root + "/",
        ]
        mock_subprocess_check_call.assert_called_once_with(call_args)

    def testPublishDistroOVALDataRsyncErrorsOut(self):
        self.setUpOVALDataRsync()
        mock_subprocess_check_call = self.useFixture(
            MockPatch(
                "lp.archivepublisher.scripts.publishdistro.check_call",
                side_effect=subprocess.CalledProcessError(
                    cmd="foo", returncode=5
                ),
            )
        ).mock
        self.assertRaises(subprocess.CalledProcessError, self.runPublishDistro)
        call_args = [
            "/usr/bin/rsync",
            "-a",
            "-q",
            "--timeout=90",
            "--delete",
            "--delete-after",
            "oval.internal::oval/",
            self.oval_data_root + "/",
        ]
        mock_subprocess_check_call.assert_called_once_with(call_args)
        expected_log_line = (
            "ERROR Failed to rsync OVAL data: "
            "['/usr/bin/rsync', '-a', '-q', '--timeout=90', '--delete', "
            "'--delete-after', 'oval.internal::oval/', "
            f"'{self.oval_data_root}/']"
        )
        self.assertTrue(expected_log_line in self.logger.getLogBuffer())

    def testPublishDistroOVALDataRsyncForExcludedArchives(self):
        """
        Test publisher skips excluded archives specified via --exclude
        during OVALData rsync.
        """
        self.setUpOVALDataRsync()
        ppa1 = self.factory.makeArchive(private=True)
        ppa2 = self.factory.makeArchive()
        non_existing_ppa_reference = "~foo/bar/ppa"
        self.factory.makeArchive()

        mock_subprocess_check_call = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        ).mock

        exclude_options = [
            "--exclude",
            ppa1.reference,
            "--exclude",
            ppa2.reference,
            "--exclude",
            non_existing_ppa_reference,
        ]
        call_args = [
            "/usr/bin/rsync",
            "-a",
            "-q",
            "--timeout=90",
            "--delete",
            "--delete-after",
        ]
        call_args.extend(exclude_options)

        call_args.extend(
            [
                "oval.internal::oval/",
                self.oval_data_root + "/",
            ]
        )
        self.runPublishDistro(
            extra_args=exclude_options,
        )
        mock_subprocess_check_call.assert_called_once_with(call_args)

    def testPublishDistroOVALDataRsyncForSpecificArchives(self):
        """
        Test publisher only runs for archives specified via --archive
        during OVALData rsync.
        """
        self.setUpOVALDataRsync()
        ppa1 = self.factory.makeArchive(private=True)
        ppa2 = self.factory.makeArchive()
        self.factory.makeArchive()

        mock_subprocess_check_call = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        ).mock

        call_args = [
            call(
                [
                    "/usr/bin/rsync",
                    "-a",
                    "-q",
                    "--timeout=90",
                    "--delete",
                    "--delete-after",
                    "-R",
                    "--ignore-missing-args",
                    os.path.join("oval.internal::oval/", ppa.reference, ""),
                    self.oval_data_root + "/",
                ]
            )
            for ppa in [ppa1, ppa2]
        ]

        self.runPublishDistro(
            extra_args=[
                "--archive",
                ppa1.reference,
                "--archive",
                ppa2.reference,
            ]
        )

        assert mock_subprocess_check_call.call_args_list == call_args

    def test_checkForUpdatedOVALData_new(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # Disable normal publication so that dirty_suites isn't cleared.
        archive.publish = False
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        write_file(str(incoming_dir), b"test")
        self.runPublishDistro(
            extra_args=["--ppa"], distribution=archive.distribution.name
        )
        self.assertEqual(["breezy-autotest"], archive.dirty_suites)

    def test_checkForUpdatedOVALData_unchanged(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # Disable normal publication so that dirty_suites isn't cleared.
        archive.publish = False
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        shutil.copytree(str(incoming_dir), str(published_dir))
        self.runPublishDistro(
            extra_args=["--ppa"], distribution=archive.distribution.name
        )
        self.assertIsNone(archive.dirty_suites)

    def test_checkForUpdatedOVALData_updated(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # Disable normal publication so that dirty_suites isn't cleared.
        archive.publish = False
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"old")
        mtime = (published_dir / "foo.oval.xml.bz2").stat().st_mtime
        os.utime(
            str(published_dir / "foo.oval.xml.bz2"), (mtime - 1, mtime - 1)
        )
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"new")
        self.runPublishDistro(
            extra_args=["--ppa"], distribution=archive.distribution.name
        )
        self.assertEqual(["breezy-autotest"], archive.dirty_suites)

    def test_checkForUpdatedOVALData_new_files(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # Disable normal publication so that dirty_suites isn't cleared.
        archive.publish = False
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        shutil.copytree(str(incoming_dir), str(published_dir))
        write_file(str(incoming_dir / "bar.oval.xml.bz2"), b"test")
        self.runPublishDistro(
            extra_args=["--ppa"], distribution=archive.distribution.name
        )
        self.assertEqual(["breezy-autotest"], archive.dirty_suites)

    def test_checkForUpdatedOVALData_nonexistent_archive(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        incoming_dir = (
            Path(self.oval_data_root)
            / "~nonexistent"
            / "ubuntutest"
            / "archive"
            / "breezy-autotest"
            / "main"
        )
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        self.runPublishDistro(extra_args=["--ppa"], distribution="ubuntutest")
        self.assertIn(
            "INFO Skipping OVAL data for '~nonexistent/ubuntutest/archive' "
            "(no such archive).",
            self.logger.getLogBuffer(),
        )

    def test_checkForUpdatedOVALData_nonexistent_suite(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # Disable normal publication so that dirty_suites isn't cleared.
        archive.publish = False
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "nonexistent"
            / "main"
        )
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        self.runPublishDistro(
            extra_args=["--ppa"], distribution=archive.distribution.name
        )
        self.assertIn(
            "INFO Skipping OVAL data for '%s:nonexistent' (no such suite)."
            % archive.reference,
            self.logger.getLogBuffer(),
        )
        self.assertIsNone(archive.dirty_suites)

    def test_checkForUpdatedOVALData_skips_excluded_ppas(self):
        """
        Skip excluded PPAs in checkForUpdatedOVALData
        """
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        ppa1 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa2 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa3 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # Disable normal publication so that dirty_suites isn't cleared.
        ppa1.publish = False
        ppa2.publish = False
        ppa3.publish = False

        for archive in [ppa1, ppa2, ppa3]:
            incoming_dir = (
                Path(self.oval_data_root)
                / archive.reference
                / "breezy-autotest"
                / "main"
            )
            write_file(str(incoming_dir), b"test")

        self.runPublishDistro(
            extra_args=[
                "--ppa",
                "--exclude",
                ppa2.reference,
                "--exclude",
                ppa3.reference,
            ],
            distribution="ubuntu",
        )

        self.assertEqual(["breezy-autotest"], ppa1.dirty_suites)
        self.assertIsNone(ppa2.dirty_suites)
        self.assertIsNone(ppa3.dirty_suites)

    def test_checkForUpdatedOVALData_for_specific_archive(self):
        """
        checkForUpdatedOVALData should only run for specific archives
        if "archive" option is specified.
        """

        distribution = "ubuntu"
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )

        ppa1 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa2 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa3 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        # another ppa with different distribution
        ppa4 = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA,
            distribution=getUtility(IDistributionSet)["ubuntutest"],
        )

        non_existing_ppa_reference = "~foo/bar/ppa"

        # Disable normal publication so that dirty_suites isn't cleared.
        ppa1.publish = False
        ppa2.publish = False
        ppa3.publish = False

        for archive in [ppa1, ppa2, ppa3, ppa4]:
            incoming_dir = (
                Path(self.oval_data_root)
                / archive.reference
                / "breezy-autotest"
                / "main"
            )
            write_file(str(incoming_dir), b"test")

        # test for code paths when there is an non-existent archive dir
        # present in oval_data_root
        nonexistent_archive_dir = (
            Path(self.oval_data_root)
            / "~nonexistent"
            / distribution
            / "archive"
            / "breezy-autotest"
            / "main"
        )
        write_file(str(nonexistent_archive_dir / "foo.oval.xml.bz2"), b"test")

        # test for code paths when there is an non-existent suite dir
        # present in oval_data_root
        nonexistent_suite_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "nonexistent"
            / "main"
        )

        write_file(str(nonexistent_suite_dir / "foo.oval.xml.bz2"), b"test")
        self.runPublishDistro(
            extra_args=[
                "--archive",
                ppa1.reference,
                "--archive",
                ppa2.reference,
                "--archive",
                ppa4.reference,
                "--archive",
                non_existing_ppa_reference,
            ],
            distribution=distribution,
        )

        self.assertEqual(["breezy-autotest"], ppa1.dirty_suites)
        self.assertEqual(["breezy-autotest"], ppa2.dirty_suites)
        self.assertIsNone(ppa3.dirty_suites)
        # ppa4 has different distribution than the target distribution so
        # it should be skipped.
        self.assertIsNone(ppa4.dirty_suites)

        # Further logs should not have any reference to other PPAs
        # as we skip them when --archive option is set.
        self.assertNotIn(
            ppa3.reference,
            self.logger.getLogBuffer(),
        )

    @defer.inlineCallbacks
    def testForPPA(self):
        """Try to run publish-distro in PPA mode.

        It should deal only with PPA publications.
        """
        pub_source_id = self.getPubSource(filecontent=b"foo").id

        cprov = getUtility(IPersonSet).getByName("cprov")
        pub_source2_id = self.getPubSource(
            sourcename="baz", filecontent=b"baz", archive=cprov.archive
        ).id

        ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        name16 = getUtility(IPersonSet).getByName("name16")
        getUtility(IArchiveSet).new(
            purpose=ArchivePurpose.PPA, owner=name16, distribution=ubuntutest
        )
        pub_source3_id = self.getPubSource(
            sourcename="bar", filecontent=b"bar", archive=name16.archive
        ).id

        # Override PPAs distributions
        naked_archive = removeSecurityProxy(cprov.archive)
        naked_archive.distribution = self.ubuntutest
        naked_archive = removeSecurityProxy(name16.archive)
        naked_archive.distribution = self.ubuntutest

        self.setUpRequireSigningKeys()
        yield self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(cprov.archive).setSigningKey(
            key_path, async_keyserver=True
        )
        name16.archive.signing_key_owner = cprov.archive.signing_key_owner
        name16.archive.signing_key_fingerprint = (
            cprov.archive.signing_key_fingerprint
        )

        self.layer.txn.commit()

        self.runPublishDistro(["--ppa"])

        pub_source = self.loadPubSource(pub_source_id)
        pub_source2 = self.loadPubSource(pub_source2_id)
        pub_source3 = self.loadPubSource(pub_source3_id)
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PUBLISHED)
        self.assertEqual(pub_source3.status, PackagePublishingStatus.PUBLISHED)

        foo_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        self.assertEqual(False, os.path.exists(foo_path))

        baz_path = os.path.join(
            config.personalpackagearchive.root,
            "cprov",
            "ppa/ubuntutest/pool/main/b/baz/baz_666.dsc",
        )
        with open(baz_path) as baz:
            self.assertEqual("baz", baz.read().strip())

        bar_path = os.path.join(
            config.personalpackagearchive.root,
            "name16",
            "ppa/ubuntutest/pool/main/b/bar/bar_666.dsc",
        )
        with open(bar_path) as bar:
            self.assertEqual("bar", bar.read().strip())

    @defer.inlineCallbacks
    def testForPrivatePPA(self):
        """Run publish-distro in private PPA mode.

        It should only publish private PPAs.
        """
        # First, we'll make a private PPA and populate it with a
        # publishing record.
        ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        private_ppa = self.factory.makeArchive(
            private=True, distribution=ubuntutest
        )

        # Publish something to the private PPA:
        pub_source_id = self.getPubSource(
            sourcename="baz", filecontent=b"baz", archive=private_ppa
        ).id
        self.layer.txn.commit()

        self.setUpRequireSigningKeys()
        yield self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(private_ppa).setSigningKey(
            key_path, async_keyserver=True
        )

        # Try a plain PPA run, to ensure the private one is NOT published.
        self.runPublishDistro(["--ppa"])

        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)

        # Now publish the private PPAs and make sure they are really
        # published.
        self.runPublishDistro(["--private-ppa"])

        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)

    def testPublishCopyArchiveWithoutSigningKey(self):
        """publish-distro skips copy archives without signing keys."""
        self.setUpRequireSigningKeys()
        ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        cprov = getUtility(IPersonSet).getByName("cprov")
        copy_archive_name = "test-copy-publish"
        copy_archive = getUtility(IArchiveSet).new(
            distribution=ubuntutest,
            owner=cprov,
            name=copy_archive_name,
            purpose=ArchivePurpose.COPY,
            enabled=True,
        )
        removeSecurityProxy(copy_archive).publish = True
        pub_source_id = self.getPubSource(archive=copy_archive).id
        self.runPublishDistro(["--copy-archive"])
        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(PackagePublishingStatus.PENDING, pub_source.status)

    def testPublishCopyArchive(self):
        """Run publish-distro in copy archive mode.

        It should only publish copy archives.
        """
        ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        cprov = getUtility(IPersonSet).getByName("cprov")
        copy_archive_name = "test-copy-publish"

        # The COPY repository path is not created yet.
        root_dir = (
            getUtility(IPublisherConfigSet)
            .getByDistribution(ubuntutest)
            .absolute_root_dir
        )
        repo_path = os.path.join(
            root_dir,
            ubuntutest.name + "-" + copy_archive_name,
            ubuntutest.name,
        )
        self.assertNotExists(repo_path)

        copy_archive = getUtility(IArchiveSet).new(
            distribution=ubuntutest,
            owner=cprov,
            name=copy_archive_name,
            purpose=ArchivePurpose.COPY,
            enabled=True,
        )
        # Save some test CPU cycles by avoiding logging in as the user
        # necessary to alter the publish flag.
        removeSecurityProxy(copy_archive).publish = True

        # Set up signing key.
        self.setUpRequireSigningKeys()
        yield self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(copy_archive).setSigningKey(
            key_path, async_keyserver=True
        )

        # Publish something.
        pub_source_id = self.getPubSource(
            sourcename="baz", filecontent=b"baz", archive=copy_archive
        ).id

        # Try a plain PPA run, to ensure the copy archive is not published.
        self.runPublishDistro(["--ppa"])

        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)

        # Now publish the copy archives and make sure they are really
        # published.
        self.runPublishDistro(["--copy-archive"])

        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)

        # Make sure that the files were published in the right place.
        pool_path = os.path.join(repo_path, "pool/main/b/baz/baz_666.dsc")
        self.assertExists(pool_path)

    @defer.inlineCallbacks
    def testForSingleArchive(self):
        """Run publish-distro over a single archive specified by reference."""
        ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        name16 = getUtility(IPersonSet).getByName("name16")
        archives = [
            getUtility(IArchiveSet).new(
                purpose=ArchivePurpose.PPA,
                owner=name16,
                name=name,
                distribution=ubuntutest,
            )
            for name in (
                self.factory.getUniqueUnicode(),
                self.factory.getUniqueUnicode(),
            )
        ]
        archive_references = [archive.reference for archive in archives]
        pub_source_ids = [
            self.getPubSource(archive=archive).id for archive in archives
        ]

        self.setUpRequireSigningKeys()
        yield self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        for archive in archives:
            yield IArchiveGPGSigningKey(archive).setSigningKey(
                key_path, async_keyserver=True
            )

        self.layer.txn.commit()

        self.assertEqual(
            [PackagePublishingStatus.PENDING, PackagePublishingStatus.PENDING],
            [
                self.loadPubSource(pub_source_id).status
                for pub_source_id in pub_source_ids
            ],
        )

        self.runPublishDistro(["--archive", archive_references[0]])

        self.assertEqual(
            [
                PackagePublishingStatus.PUBLISHED,
                PackagePublishingStatus.PENDING,
            ],
            [
                self.loadPubSource(pub_source_id).status
                for pub_source_id in pub_source_ids
            ],
        )

        self.runPublishDistro(["--archive", archive_references[1]])

        self.assertEqual(
            [
                PackagePublishingStatus.PUBLISHED,
                PackagePublishingStatus.PUBLISHED,
            ],
            [
                self.loadPubSource(pub_source_id).status
                for pub_source_id in pub_source_ids
            ],
        )

    def testPublishToArtifactory(self):
        """Publishing to Artifactory doesn't require generated signing keys."""
        self.setUpRequireSigningKeys()
        switch_dbuser("launchpad")
        base_url = "https://foo.example.com/artifactory"
        self.pushConfig("artifactory", base_url=base_url)
        archive = self.factory.makeArchive(
            distribution=self.ubuntutest,
            publishing_method=ArchivePublishingMethod.ARTIFACTORY,
            repository_format=ArchiveRepositoryFormat.PYTHON,
        )
        das = self.ubuntutest.currentseries.architectures[0]
        self.useFixture(FakeArtifactoryFixture(base_url, archive.name))
        ci_build = self.factory.makeCIBuild(distro_arch_series=das)
        bpn = self.factory.makeBinaryPackageName()
        bpr = self.factory.makeBinaryPackageRelease(
            binarypackagename=bpn,
            version="1.0",
            ci_build=ci_build,
            binpackageformat=BinaryPackageFormat.WHL,
        )
        self.factory.makeBinaryPackageFile(
            binarypackagerelease=bpr, filetype=BinaryPackageFileType.WHL
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            archive=archive,
            distroarchseries=das,
            pocket=PackagePublishingPocket.RELEASE,
            architecturespecific=True,
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)
        self.layer.txn.commit()

        self.runPublishDistro(["--ppa"])

        self.assertEqual(PackagePublishingStatus.PUBLISHED, bpph.status)

    def testRunWithEmptySuites(self):
        """Try a publish-distro run on empty suites in careful_apt mode

        Expect it to create all indexes, including current 'Release' file
        for the empty suites specified.
        """
        self.runPublishDistro(
            ["-A", "-s", "hoary-test-updates", "-s", "hoary-test-backports"]
        )

        # Check "Release" files
        release_path = "%s/hoary-test-updates/Release" % self.config.distsroot
        self.assertExists(release_path)

        release_path = (
            "%s/hoary-test-backports/Release" % self.config.distsroot
        )
        self.assertExists(release_path)

        release_path = "%s/hoary-test/Release" % self.config.distsroot
        self.assertNotExists(release_path)

        # Check some index files
        index_path = (
            "%s/hoary-test-updates/main/binary-i386/Packages.gz"
            % self.config.distsroot
        )
        self.assertExists(index_path)

        index_path = (
            "%s/hoary-test-backports/main/binary-i386/Packages.gz"
            % self.config.distsroot
        )
        self.assertExists(index_path)

        index_path = (
            "%s/hoary-test/main/binary-i386/Packages.gz"
            % self.config.distsroot
        )
        self.assertNotExists(index_path)

    @defer.inlineCallbacks
    def testCarefulRelease(self):
        """publish-distro can be asked to just rewrite Release files."""
        archive = self.factory.makeArchive(distribution=self.ubuntutest)
        archive_id = archive.id
        pub_source_id = self.getPubSource(
            filecontent=b"foo", archive=archive
        ).id

        self.setUpRequireSigningKeys()
        yield self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(archive).setSigningKey(
            key_path, async_keyserver=True
        )

        self.layer.txn.commit()

        self.runPublishDistro(["--ppa"])

        pub_source = self.loadPubSource(pub_source_id)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)

        archive = getUtility(IArchiveSet).get(archive_id)
        dists_path = getPubConfig(archive).distsroot
        hoary_inrelease_path = os.path.join(
            dists_path, "hoary-test", "InRelease"
        )
        breezy_inrelease_path = os.path.join(
            dists_path, "breezy-autotest", "InRelease"
        )
        self.assertThat(hoary_inrelease_path, Not(PathExists()))
        os.unlink(breezy_inrelease_path)

        self.runPublishDistro(["--ppa", "--careful-release"])
        self.assertThat(hoary_inrelease_path, Not(PathExists()))
        self.assertThat(breezy_inrelease_path, Not(PathExists()))

        self.runPublishDistro(
            [
                "--ppa",
                "--careful-release",
                "--include-non-pending",
                "--disable-publishing",
                "--disable-domination",
                "--disable-apt",
            ]
        )
        # hoary-test never had indexes created, so is untouched.
        self.assertThat(hoary_inrelease_path, Not(PathExists()))
        # breezy-autotest has its Release files rewritten.
        self.assertThat(breezy_inrelease_path, PathExists())

    def testDirtySuites(self):
        """publish-distro can be told to publish specific suites."""
        archive_id = self.factory.makeArchive(distribution=self.ubuntutest).id
        self.layer.txn.commit()

        # publish-distro has nothing to publish.
        self.runPublishDistro(["--ppa"])
        archive = getUtility(IArchiveSet).get(archive_id)
        breezy_release_path = os.path.join(
            getPubConfig(archive).distsroot, "breezy-autotest", "Release"
        )
        self.assertThat(breezy_release_path, Not(PathExists()))

        # ... but it will publish a suite anyway if it is marked as dirty.
        archive.markSuiteDirty(
            archive.distribution.getSeries("breezy-autotest"),
            PackagePublishingPocket.RELEASE,
        )
        self.runPublishDistro(["--ppa"])
        self.assertThat(breezy_release_path, PathExists())


class FakeArchive:
    """A very simple fake `Archive`."""

    def __init__(self, distribution, purpose=ArchivePurpose.PRIMARY):
        self.publish = True
        self.can_be_published = True
        self.distribution = distribution
        self.purpose = purpose
        self.status = ArchiveStatus.ACTIVE
        self.dirty_suites = []
        self.publishing_method = ArchivePublishingMethod.LOCAL


class FakePublisher:
    """A very simple fake `Publisher`."""

    def __init__(self):
        self.setupArchiveDirs = FakeMethod()
        self.A_publish = FakeMethod()
        self.A2_markPocketsWithDeletionsDirty = FakeMethod()
        self.B_dominate = FakeMethod()
        self.C_doFTPArchive = FakeMethod()
        self.C_writeIndexes = FakeMethod()
        self.C_doDirectIndexes = FakeMethod()
        self.D_writeReleaseFiles = FakeMethod()
        self.createSeriesAliases = FakeMethod()
        self.markSuiteDirty = FakeMethod()


class TestPublishDistroMethods(TestCaseWithFactory):
    """Fine-grained unit tests for `PublishDistro`."""

    layer = ZopelessDatabaseLayer

    def makeDistro(self):
        """Create a distribution."""
        # Set up a temporary directory as publish_root_dir.  Without
        # this, getPublisher will create archives in the current
        # directory.
        return self.factory.makeDistribution(
            publish_root_dir=self.makeTemporaryDirectory()
        )

    def makeScript(self, distribution=None, args=[], all_derived=False):
        """Create a `PublishDistro` for `distribution`."""
        if distribution is None and not all_derived:
            distribution = self.makeDistro()
        distro_args = []
        if distribution is not None:
            distro_args.extend(["--distribution", distribution.name])
        if all_derived:
            distro_args.append("--all-derived")
        full_args = args + distro_args
        script = PublishDistro(test_args=full_args)
        script.distribution = distribution
        self.logger = BufferLogger()
        script.logger = self.logger
        return script

    def test_isCareful_is_false_if_option_not_set(self):
        # isCareful normally returns False for a carefulness option that
        # evaluates to False.
        self.assertFalse(self.makeScript().isCareful(False))

    def test_isCareful_is_true_if_option_is_set(self):
        # isCareful returns True for a carefulness option that evaluates
        # to True.
        self.assertTrue(self.makeScript().isCareful(True))

    def test_isCareful_is_true_if_global_careful_option_is_set(self):
        # isCareful returns True for any option value if the global
        # "careful" option has been set.
        self.assertTrue(self.makeScript(args=["--careful"]).isCareful(False))

    def test_describeCare_reports_non_careful_option(self):
        # describeCare describes the absence of carefulness as "Normal."
        self.assertEqual("Normal", self.makeScript().describeCare(False))

    def test_describeCare_reports_careful_option(self):
        # describeCare describes a carefulness option that's been set to
        # True as "Careful."
        self.assertEqual("Careful", self.makeScript().describeCare(True))

    def test_describeCare_reports_careful_override(self):
        # If a carefulness option is considered to be set regardless of
        # its actual value because the global "careful" option overrides
        # it, describeCare reports that as "Careful (Overridden)."
        self.assertEqual(
            "Careful (Overridden)",
            self.makeScript(args=["--careful"]).describeCare(False),
        )

    def test_countExclusiveOptions_is_zero_if_none_set(self):
        # If none of the exclusive options is set, countExclusiveOptions
        # counts zero.
        self.assertEqual(0, self.makeScript().countExclusiveOptions())

    def test_countExclusiveOptions_counts_partner(self):
        # countExclusiveOptions includes the "partner" option.
        self.assertEqual(
            1, self.makeScript(args=["--partner"]).countExclusiveOptions()
        )

    def test_countExclusiveOptions_counts_ppa(self):
        # countExclusiveOptions includes the "ppa" option.
        self.assertEqual(
            1, self.makeScript(args=["--ppa"]).countExclusiveOptions()
        )

    def test_countExclusiveOptions_counts_private_ppa(self):
        # countExclusiveOptions includes the "private-ppa" option.
        self.assertEqual(
            1, self.makeScript(args=["--private-ppa"]).countExclusiveOptions()
        )

    def test_countExclusiveOptions_counts_copy_archive(self):
        # countExclusiveOptions includes the "copy-archive" option.
        self.assertEqual(
            1, self.makeScript(args=["--copy-archive"]).countExclusiveOptions()
        )

    def test_countExclusiveOptions_counts_archive(self):
        # countExclusiveOptions includes the "archive" option.
        self.assertEqual(
            1, self.makeScript(args=["--archive"]).countExclusiveOptions()
        )

    def test_countExclusiveOptions_detects_conflict(self):
        # If more than one of the exclusive options has been set, that
        # raises the result from countExclusiveOptions above 1.
        script = self.makeScript(args=["--ppa", "--partner"])
        self.assertEqual(2, script.countExclusiveOptions())

    def test_validateOptions_rejects_nonoption_arguments(self):
        # validateOptions disallows non-option command-line arguments.
        script = self.makeScript(args=["please"])
        self.assertRaises(OptionValueError, script.validateOptions)

    def test_validateOptions_rejects_exclusive_option_conflict(self):
        # If more than one of the exclusive options are set,
        # validateOptions raises that as an error.
        script = self.makeScript()
        script.countExclusiveOptions = FakeMethod(2)
        self.assertRaises(OptionValueError, script.validateOptions)

    def test_validateOptions_does_not_accept_distsroot_for_ppa(self):
        # The "distsroot" option is not allowed with the ppa option.
        script = self.makeScript(args=["--ppa", "--distsroot=/tmp"])
        self.assertRaises(OptionValueError, script.validateOptions)

    def test_validateOptions_does_not_accept_distsroot_for_private_ppa(self):
        # The "distsroot" option is not allowed with the private-ppa
        # option.
        script = self.makeScript(args=["--private-ppa", "--distsroot=/tmp"])
        self.assertRaises(OptionValueError, script.validateOptions)

    def test_validateOptions_accepts_all_derived_without_distro(self):
        # If --all-derived is given, the --distribution option is not
        # required.
        PublishDistro(test_args=["--all-derived"]).validateOptions()
        # The test is that we get here without error.
        pass

    def test_validateOptions_does_not_accept_distro_with_all_derived(self):
        # The --all-derived option conflicts with the --distribution
        # option.
        distro = self.makeDistro()
        script = PublishDistro(test_args=["-d", distro.name, "--all-derived"])
        self.assertRaises(OptionValueError, script.validateOptions)

    def test_findDistros_finds_selected_distribution(self):
        # findDistros looks up and returns the distribution named on the
        # command line.
        distro = self.makeDistro()
        self.assertEqual([distro], self.makeScript(distro).findDistros())

    def test_findDistros_finds_ubuntu_by_default(self):
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.assertContentEqual(
            [ubuntu], PublishDistro(test_args=[]).findDistros()
        )

    def test_findDistros_raises_if_selected_distro_not_found(self):
        # If findDistro can't find the distribution, that's an
        # OptionValueError.
        wrong_name = self.factory.getUniqueString()
        self.assertRaises(
            OptionValueError,
            PublishDistro(test_args=["-d", wrong_name]).findDistros,
        )

    def test_findDistros_for_all_derived_distros_may_return_empty(self):
        # If the --all-derived option is given but there are no derived
        # distributions to publish, findDistros returns no distributions
        # (but it does return normally).
        self.assertContentEqual(
            [], self.makeScript(all_derived=True).findDistros()
        )

    def test_findDistros_for_all_derived_finds_derived_distros(self):
        # If --all-derived is given, findDistros finds all derived
        # distributions.
        dsp = self.factory.makeDistroSeriesParent()
        self.assertContentEqual(
            [dsp.derived_series.distribution],
            self.makeScript(all_derived=True).findDistros(),
        )

    def test_findDistros_for_all_derived_ignores_ubuntu(self):
        # The --all-derived option does not include Ubuntu, even if it
        # is itself a derived distribution.
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        self.factory.makeDistroSeriesParent(parent_series=ubuntu.currentseries)
        self.assertNotIn(
            ubuntu, self.makeScript(all_derived=True).findDistros()
        )

    def test_findDistros_for_all_derived_ignores_nonderived_distros(self):
        self.makeDistro()
        self.assertContentEqual(
            [], self.makeScript(all_derived=True).findDistros()
        )

    def test_findArchives_without_distro_filter(self):
        ppa1 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        ppa2 = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        non_existing_ppa_reference = "~foo/ubuntu/bar-ppa"

        archive_references = [
            ppa1.reference,
            ppa2.reference,
            non_existing_ppa_reference,
        ]
        self.assertContentEqual(
            [ppa1, ppa2],
            self.makeScript().findArchives(archive_references),
        )

        self.assertIn(
            "WARNING Cannot find the archive with reference: "
            f"'{non_existing_ppa_reference}'",
            self.logger.getLogBuffer(),
        )

    def test_findArchives_with_distro_filter(self):
        distro1 = self.makeDistro()
        distro2 = self.makeDistro()
        ppa1 = self.factory.makeArchive(distro1, purpose=ArchivePurpose.PPA)
        ppa2 = self.factory.makeArchive(distro1, purpose=ArchivePurpose.PPA)
        ppa3 = self.factory.makeArchive(distro2, purpose=ArchivePurpose.PPA)
        non_existing_ppa_reference = "~foo/ubuntu/bar-ppa"

        archive_references = [
            ppa1.reference,
            ppa2.reference,
            ppa3.reference,
            non_existing_ppa_reference,
        ]
        self.assertContentEqual(
            [ppa1, ppa2],
            self.makeScript().findArchives(archive_references, distro1),
        )
        self.assertContentEqual(
            [ppa3], self.makeScript().findArchives(archive_references, distro2)
        )

        self.assertIn(
            "WARNING Cannot find the archive with reference: "
            f"'{non_existing_ppa_reference}'",
            self.logger.getLogBuffer(),
        )

    def test_findArchives_warns_for_non_ppa_references(self):
        partner = self.factory.makeArchive(purpose=ArchivePurpose.PARTNER)
        copy = self.factory.makeArchive(purpose=ArchivePurpose.COPY)
        primary = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        archive_references = [
            partner.reference,
            copy.reference,
            primary.reference,
        ]
        self.assertContentEqual(
            [],
            self.makeScript().findArchives(archive_references),
        )
        self.assertIn(
            f"WARNING Skipping '{partner.reference}'. Archive reference of "
            "type 'PARTNER' specified. Only PPAs are allowed",
            self.logger.getLogBuffer(),
        )
        self.assertIn(
            f"WARNING Skipping '{copy.reference}'. Archive reference of type "
            "'COPY' specified. Only PPAs are allowed",
            self.logger.getLogBuffer(),
        )
        self.assertIn(
            f"WARNING Skipping '{primary.reference}'. Archive reference of "
            "type 'PRIMARY' specified. Only PPAs are allowed",
            self.logger.getLogBuffer(),
        )

    def test_findSuite_finds_release_pocket(self):
        # Despite its lack of a suffix, a release suite shows up
        # normally in findSuite results.
        series = self.factory.makeDistroSeries()
        distro = series.distribution
        self.assertEqual(
            (series, PackagePublishingPocket.RELEASE),
            self.makeScript(distro).findSuite(distro, series.name),
        )

    def test_findSuite_finds_other_pocket(self):
        # Suites that are not in the release pocket have their pocket
        # name as a suffix.  These show up in findSuite results.
        series = self.factory.makeDistroSeries()
        distro = series.distribution
        script = self.makeScript(distro)
        self.assertEqual(
            (series, PackagePublishingPocket.UPDATES),
            script.findSuite(distro, series.name + "-updates"),
        )

    def test_findSuite_raises_if_not_found(self):
        # If findSuite can't find its suite, that's an OptionValueError.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        self.assertRaises(
            OptionValueError,
            script.findSuite,
            distro,
            self.factory.getUniqueString(),
        )

    def test_findAllowedSuites_finds_nothing_if_no_suites_given(self):
        # If no suites are given, findAllowedSuites returns an empty
        # sequence.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        self.assertContentEqual([], script.findAllowedSuites(distro))

    def test_findAllowedSuites_finds_single(self):
        # findAllowedSuites looks up the requested suite.
        series = self.factory.makeDistroSeries()
        suite = "%s-updates" % series.name
        script = self.makeScript(series.distribution, ["--suite", suite])
        self.assertContentEqual(
            [suite], script.findAllowedSuites(series.distribution)
        )

    def test_findAllowedSuites_finds_multiple(self):
        # Multiple suites may be requested; findAllowedSuites looks them
        # all up.
        series = self.factory.makeDistroSeries()
        script = self.makeScript(
            series.distribution,
            ["--suite", "%s-updates" % series.name, "--suite", series.name],
        )
        expected_suites = ["%s-updates" % series.name, series.name]
        self.assertContentEqual(
            expected_suites, script.findAllowedSuites(series.distribution)
        )

    def test_getCopyArchives_returns_list(self):
        # getCopyArchives returns a list of archives.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        copy_archive = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.COPY
        )
        self.assertEqual([copy_archive], script.getCopyArchives(distro))

    def test_getCopyArchives_raises_if_not_found(self):
        # If the distribution has no copy archives, that's a script
        # failure.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        self.assertRaises(
            LaunchpadScriptFailure, script.getCopyArchives, distro
        )

    def test_getCopyArchives_ignores_other_archive_purposes(self):
        # getCopyArchives won't return archives that aren't copy
        # archives.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        self.factory.makeArchive(distro, purpose=ArchivePurpose.PARTNER)
        self.assertRaises(
            LaunchpadScriptFailure, script.getCopyArchives, distro
        )

    def test_getCopyArchives_ignores_other_distros(self):
        # getCopyArchives won't return an archive for the wrong
        # distribution.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        self.factory.makeArchive(purpose=ArchivePurpose.COPY)
        self.assertRaises(
            LaunchpadScriptFailure, script.getCopyArchives, distro
        )

    def test_getPPAs_gets_pending_distro_PPAs_if_careful(self):
        # In careful mode, getPPAs includes PPAs for the distribution
        # that are pending publication.
        distro = self.makeDistro()
        script = self.makeScript(distro, ["--careful"])
        ppa = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.factory.makeSourcePackagePublishingHistory(archive=ppa)
        self.assertContentEqual([ppa], script.getPPAs(distro))

    def test_getPPAs_gets_nonpending_distro_PPAs_if_careful(self):
        # In careful mode, getPPAs includes PPAs for the distribution
        # that are not pending publication.
        distro = self.makeDistro()
        script = self.makeScript(distro, ["--careful"])
        ppa = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.assertContentEqual([ppa], script.getPPAs(distro))

    def test_getPPAs_gets_nonpending_distro_PPAs_if_requested(self):
        # In --include-non-pending mode, getPPAs includes PPAs for the
        # distribution that are not pending publication.
        distro = self.makeDistro()
        script = self.makeScript(distro, ["--include-non-pending"])
        ppa = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.assertContentEqual([ppa], script.getPPAs(distro))

    def test_getPPAs_gets_pending_distro_PPAs_if_not_careful(self):
        # In non-careful mode, getPPAs includes PPAs that are pending
        # publication.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        ppa = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.factory.makeSourcePackagePublishingHistory(archive=ppa)
        self.assertContentEqual([ppa], script.getPPAs(distro))

    def test_getPPAs_ignores_nonpending_distro_PPAs_if_not_careful(self):
        # In non-careful mode, getPPAs does not include PPAs that are
        # not pending publication.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.assertContentEqual([], script.getPPAs(distro))

    def test_getPPAs_returns_empty_if_careful_and_no_PPAs_found(self):
        # If, in careful mode, getPPAs finds no archives it returns an
        # empty sequence.
        distro = self.makeDistro()
        script = self.makeScript(distro, ["--careful"])
        self.assertContentEqual([], script.getPPAs(distro))

    def test_getPPAs_returns_empty_if_not_careful_and_no_PPAs_found(self):
        # If, in non-careful mode, getPPAs finds no archives it returns
        # an empty sequence.
        distro = self.makeDistro()
        self.assertContentEqual([], self.makeScript(distro).getPPAs(distro))

    def test_getTargetArchives_gets_partner_archive(self):
        # If the selected exclusive option is "partner,"
        # getTargetArchives looks for a partner archive.
        distro = self.makeDistro()
        partner = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PARTNER
        )
        script = self.makeScript(distro, ["--partner"])
        self.assertContentEqual([partner], script.getTargetArchives(distro))

    def test_getTargetArchives_ignores_public_ppas_if_private(self):
        # If the selected exclusive option is "private-ppa,"
        # getTargetArchives looks for PPAs but leaves out public ones.
        distro = self.makeDistro()
        self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=False
        )
        script = self.makeScript(distro, ["--private-ppa"])
        self.assertContentEqual([], script.getTargetArchives(distro))

    def test_getTargetArchives_gets_private_ppas_if_private(self):
        # If the selected exclusive option is "private-ppa,"
        # getTargetArchives looks for private PPAs.
        distro = self.makeDistro()
        ppa = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=True
        )
        script = self.makeScript(distro, ["--private-ppa", "--careful"])
        self.assertContentEqual([ppa], script.getTargetArchives(distro))

    def test_getTargetArchives_gets_public_ppas_if_not_private(self):
        # If the selected exclusive option is "ppa," getTargetArchives
        # looks for public PPAs.
        distro = self.makeDistro()
        ppa = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=False
        )
        script = self.makeScript(distro, ["--ppa", "--careful"])
        self.assertContentEqual([ppa], script.getTargetArchives(distro))

    def test_getTargetArchives_ignores_private_ppas_if_not_private(self):
        # If the selected exclusive option is "ppa," getTargetArchives
        # leaves out private PPAs.
        distro = self.makeDistro()
        self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=True
        )
        script = self.makeScript(distro, ["--ppa"])
        self.assertContentEqual([], script.getTargetArchives(distro))

    def test_getTargetArchives_ignores_excluded_archives_for_ppa(self):
        # If the selected exclusive option is "ppa," getTargetArchives
        # leaves out excluded PPAs.
        distro = self.makeDistro()
        ppa = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        excluded_ppa_1 = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA
        )
        excluded_ppa_2 = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA
        )
        script = self.makeScript(
            distro,
            [
                "--ppa",
                "--careful",
                "--exclude",
                excluded_ppa_1.reference,
                "--exclude",
                excluded_ppa_2.reference,
            ],
        )
        self.assertContentEqual([ppa], script.getTargetArchives(distro))

    def test_getTargetArchives_ignores_excluded_archives_for_private_ppa(self):
        # If the selected exclusive option is "private-ppa," getTargetArchives
        # leaves out excluded PPAs.
        distro = self.makeDistro()
        ppa = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=True
        )
        excluded_ppa_1 = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=True
        )
        excluded_ppa_2 = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PPA, private=True
        )
        script = self.makeScript(
            distro,
            [
                "--private-ppa",
                "--careful",
                "--exclude",
                excluded_ppa_1.reference,
                "--exclude",
                excluded_ppa_2.reference,
            ],
        )
        self.assertContentEqual([ppa], script.getTargetArchives(distro))

    def test_getTargetArchives_gets_copy_archives(self):
        # If the selected exclusive option is "copy-archive,"
        # getTargetArchives looks for a copy archive.
        distro = self.makeDistro()
        copy = self.factory.makeArchive(distro, purpose=ArchivePurpose.COPY)
        script = self.makeScript(distro, ["--copy-archive"])
        self.assertContentEqual([copy], script.getTargetArchives(distro))

    def test_getTargetArchives_gets_specific_archives(self):
        # If the selected exclusive option is "archive,"
        # getTargetArchives uses getArchives to look for the specified archives
        # and return only the ones pending publication.
        distro = self.makeDistro()

        ppa_1 = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.factory.makeSourcePackagePublishingHistory(archive=ppa_1)
        ppa_2 = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)

        # create another random archive in the same distro
        self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)

        script = self.makeScript(
            distro,
            [
                "--archive",
                ppa_1.reference,
                "--archive",
                ppa_2.reference,
            ],
        )
        self.assertContentEqual([ppa_1], script.getTargetArchives(distro))

    def test_getTargetArchives_gets_specific_archives_non_pending(self):
        # If the selected options are "archive, include-non-pending"
        # getTargetArchives uses getArchives to look for the specified archives
        # and return all of them
        distro = self.makeDistro()

        ppa_1 = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.factory.makeSourcePackagePublishingHistory(archive=ppa_1)
        ppa_2 = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)

        # create another random archive in the same distro
        self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)

        script = self.makeScript(
            distro,
            [
                "--include-non-pending",
                "--archive",
                ppa_1.reference,
                "--archive",
                ppa_2.reference,
            ],
        )
        self.assertContentEqual(
            [ppa_1, ppa_2], script.getTargetArchives(distro)
        )

    def test_getTargetArchives_gets_specific_archives_careful(self):
        # If the selected options are "archive, careful"
        # getTargetArchives uses getArchives to look for the specified archives
        # and return all of them
        distro = self.makeDistro()

        ppa_1 = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        self.factory.makeSourcePackagePublishingHistory(archive=ppa_1)
        ppa_2 = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)

        # create another random archive in the same distro
        self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)

        script = self.makeScript(
            distro,
            [
                "--careful",
                "--archive",
                ppa_1.reference,
                "--archive",
                ppa_2.reference,
            ],
        )
        self.assertContentEqual(
            [ppa_1, ppa_2], script.getTargetArchives(distro)
        )

    def test_getPublisher_returns_publisher(self):
        # getPublisher produces a Publisher instance.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        publisher = script.getPublisher(distro, distro.main_archive, None)
        self.assertIsInstance(publisher, Publisher)

    def test_deleteArchive_deletes_ppa(self):
        # If fed a PPA, deleteArchive will properly delete it (and
        # return True to indicate it's done something that needs
        # committing).
        distro = self.makeDistro()
        ppa = self.factory.makeArchive(distro, purpose=ArchivePurpose.PPA)
        script = self.makeScript(distro)
        deletion_done = script.deleteArchive(
            ppa, script.getPublisher(distro, ppa, [])
        )
        self.assertTrue(deletion_done)
        self.assertEqual(ArchiveStatus.DELETED, ppa.status)

    def test_deleteArchive_ignores_non_ppa(self):
        # If fed an archive that's not a PPA, deleteArchive will do
        # nothing and return False to indicate the fact.
        distro = self.makeDistro()
        archive = self.factory.makeArchive(
            distro, purpose=ArchivePurpose.PARTNER
        )
        script = self.makeScript(distro)
        deletion_done = script.deleteArchive(archive, None)
        self.assertFalse(deletion_done)
        self.assertEqual(ArchiveStatus.ACTIVE, archive.status)

    def test_deleteArchive_ignores_non_local(self):
        # If fed an Artifactory PPA, deleteArchive will do nothing and
        # return False to indicate the fact.
        distro = self.makeDistro()
        ppa = self.factory.makeArchive(
            distro,
            purpose=ArchivePurpose.PPA,
            publishing_method=ArchivePublishingMethod.ARTIFACTORY,
        )
        script = self.makeScript(distro)
        deletion_done = script.deleteArchive(ppa, None)
        self.assertFalse(deletion_done)
        self.assertEqual(ArchiveStatus.ACTIVE, ppa.status)

    def test_publishArchive_drives_publisher(self):
        # publishArchive puts a publisher through its paces.  This work
        # ought to be in the publisher itself, so if you find this way
        # of doing things annoys you, that's your cue to help clean up!
        distro = self.makeDistro()
        script = self.makeScript(distro)
        script.txn = FakeTransaction()
        publisher = FakePublisher()
        script.publishArchive(FakeArchive(distro), publisher)
        self.assertEqual(1, publisher.A_publish.call_count)
        self.assertEqual(
            1, publisher.A2_markPocketsWithDeletionsDirty.call_count
        )
        self.assertEqual(1, publisher.B_dominate.call_count)
        self.assertEqual(1, publisher.D_writeReleaseFiles.call_count)

    def test_publishArchive_honours_disable_options(self):
        # The various --disable-* options disable the corresponding
        # publisher steps.
        possible_options = {
            "--disable-publishing": ["A_publish"],
            "--disable-domination": [
                "A2_markPocketsWithDeletionsDirty",
                "B_dominate",
            ],
            "--disable-apt": ["C_doFTPArchive", "createSeriesAliases"],
            "--disable-release": ["D_writeReleaseFiles"],
        }
        for option in possible_options:
            distro = self.makeDistro()
            script = self.makeScript(distro, args=[option])
            script.txn = FakeTransaction()
            publisher = FakePublisher()
            script.publishArchive(FakeArchive(distro), publisher)
            for check_option, steps in possible_options.items():
                for step in steps:
                    publisher_step = getattr(publisher, step)
                    if check_option == option:
                        self.assertEqual(0, publisher_step.call_count)
                    else:
                        self.assertEqual(1, publisher_step.call_count)

    def test_publishArchive_uses_apt_ftparchive_for_main_archive(self):
        # Without the direct-indexes feature flag, publishArchive invokes
        # C_doFTPArchive for PRIMARY/COPY archives.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        script.txn = FakeTransaction()
        publisher = FakePublisher()
        script.publishArchive(FakeArchive(distro), publisher)
        self.assertEqual(1, publisher.C_doFTPArchive.call_count)
        self.assertEqual(0, publisher.C_doDirectIndexes.call_count)
        self.assertEqual(0, publisher.C_writeIndexes.call_count)

    def test_publishArchive_uses_direct_indexes_for_main_archive(self):
        # With the direct-indexes feature flag enabled, publishArchive
        # invokes C_doDirectIndexes for PRIMARY/COPY archives.
        self.useFixture(FeatureFixture({PUBLISHER_DIRECT_INDEXES: "on"}))
        distro = self.makeDistro()
        script = self.makeScript(distro)
        script.txn = FakeTransaction()
        publisher = FakePublisher()
        script.publishArchive(FakeArchive(distro), publisher)
        self.assertEqual(0, publisher.C_doFTPArchive.call_count)
        self.assertEqual(1, publisher.C_doDirectIndexes.call_count)
        self.assertEqual(0, publisher.C_writeIndexes.call_count)

    def test_publishArchive_writes_own_indexes_for_ppa(self):
        # For some types of archive, publishArchive invokes the
        # publisher's C_writeIndexes as an alternative to
        # C_doFTPArchive.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        script.txn = FakeTransaction()
        publisher = FakePublisher()
        script.publishArchive(
            FakeArchive(distro, ArchivePurpose.PPA), publisher
        )
        self.assertEqual(0, publisher.C_doFTPArchive.call_count)
        self.assertEqual(1, publisher.C_writeIndexes.call_count)

    def test_processArchive_resets_store(self):
        # The store is reset after processing each archive, as otherwise a
        # large number of alive objects in the store can cause performance
        # problems.
        distro = self.makeDistro()
        script = self.makeScript(distro)
        script.txn = FakeTransaction()
        archive = self.factory.makeArchive(distribution=distro)
        archive_id = archive.id
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.publishArchive = FakeMethod()
        store = Store.of(archive)
        self.assertNotEqual({}, store._alive)

        publisher_run_set = getUtility(IArchivePublisherRunSet)
        publisher_run = publisher_run_set.new()
        IStore(ArchivePublisherRun).flush()

        script.processArchive(archive_id, publisher_run.id)
        self.assertEqual({}, store._alive)
        [((published_archive, _), _)] = script.publishArchive.calls
        self.assertEqual(archive, published_archive)

    def test_publishes_only_selected_archives(self):
        # The script publishes only the archives returned by
        # getTargetArchives, for the distributions returned by
        # findDistros.
        distro = self.makeDistro()
        # The script gets a distribution and archive of its own, to
        # prove that any distros and archives other than what
        # findDistros and getTargetArchives return are ignored.
        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([distro])
        archive = self.factory.makeArchive(distribution=distro)
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.publishArchive = FakeMethod()
        script.main()
        [(args, kwargs)] = script.getPublisher.calls
        distro_arg, archive_arg = args[:2]
        self.assertEqual(distro, distro_arg)
        self.assertEqual(archive, archive_arg)
        self.assertEqual(
            [((archive, publisher), {})], script.publishArchive.calls
        )

    def setUpOVALDataRsync(self):
        self.oval_data_root = self.makeTemporaryDirectory()
        self.pushConfig(
            "archivepublisher",
            oval_data_rsync_endpoint="oval.internal::oval/",
            oval_data_root=self.oval_data_root,
            oval_data_rsync_timeout=90,
        )
        self.ppa_root = self.makeTemporaryDirectory()
        self.pushConfig(
            "personalpackagearchive",
            root=self.ppa_root,
        )

    def test_syncOVALDataFilesForSuite_oval_data_missing_in_destination(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        mock_copy = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.copy")
        ).mock
        # XXX 2023-04-21 jugmac00: as we create temporary directories anyway,
        # we can avoid to use mocks, and directly assert on the content of the
        # target directory on the filesystem
        # This also applies for similar tests here.
        mock_unlink = self.useFixture(MockPatch("pathlib.Path.unlink")).mock
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        write_file(str(incoming_dir / "test"), b"test")
        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.main()
        mock_copy.assert_has_calls(
            [
                call(
                    str(incoming_dir / "test"),
                    "{}/breezy-autotest/main/oval".format(
                        getPubConfig(archive).distsroot
                    ),
                )
            ]
        )
        mock_unlink.assert_not_called()

    def test_syncOVALDataFilesForSuite_oval_data_missing_in_source(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        mock_copy = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.copy")
        ).mock
        mock_unlink = self.useFixture(MockPatch("os.unlink")).mock
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        incoming_dir.mkdir(parents=True, exist_ok=True)
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(published_dir / "foo2.oval.xml.bz2"), b"test")

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.main()
        mock_copy.assert_not_called()
        mock_unlink.assert_has_calls(
            [
                call(str(published_dir / "foo.oval.xml.bz2")),
                call(str(published_dir / "foo2.oval.xml.bz2")),
            ],
            any_order=True,
        )

    def test_syncOVALDataFilesForSuite_oval_data_unchanged(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        mock_copy = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.copy")
        ).mock
        mock_unlink = self.useFixture(MockPatch("os.unlink")).mock
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        incoming_dir.mkdir(parents=True, exist_ok=True)
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(incoming_dir / "foo2.oval.xml.bz2"), b"test")
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(published_dir / "foo2.oval.xml.bz2"), b"test")

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.main()
        mock_copy.assert_not_called()
        mock_unlink.assert_not_called()

    def test_syncOVALDataFilesForSuite_oval_data_updated(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        mock_copy = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.copy")
        ).mock
        mock_unlink = self.useFixture(MockPatch("os.unlink")).mock
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        incoming_dir.mkdir(parents=True, exist_ok=True)
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test2")
        write_file(str(incoming_dir / "foo2.oval.xml.bz2"), b"test2")
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(published_dir / "foo2.oval.xml.bz2"), b"test")

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.main()
        mock_copy.assert_has_calls(
            [
                call(
                    str(incoming_dir / "foo.oval.xml.bz2"),
                    "{}/breezy-autotest/main/oval".format(
                        getPubConfig(archive).distsroot
                    ),
                ),
                call(
                    str(incoming_dir / "foo2.oval.xml.bz2"),
                    "{}/breezy-autotest/main/oval".format(
                        getPubConfig(archive).distsroot
                    ),
                ),
            ],
            any_order=True,
        )
        mock_unlink.assert_not_called()

    def test_syncOVALDataFilesForSuite_oval_data_new_files(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        mock_copy = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.copy")
        ).mock
        mock_unlink = self.useFixture(MockPatch("os.unlink")).mock
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        incoming_dir.mkdir(parents=True, exist_ok=True)
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(incoming_dir / "foo2.oval.xml.bz2"), b"test")
        write_file(str(incoming_dir / "foo3.oval.xml.bz2"), b"test")
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(published_dir / "foo2.oval.xml.bz2"), b"test")

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.main()
        mock_copy.assert_has_calls(
            [
                call(
                    str(incoming_dir / "foo3.oval.xml.bz2"),
                    "{}/breezy-autotest/main/oval".format(
                        getPubConfig(archive).distsroot
                    ),
                ),
            ]
        )
        mock_unlink.assert_not_called()

    def test_syncOVALDataFilesForSuite_oval_data_some_files_removed(self):
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        mock_copy = self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.copy")
        ).mock
        mock_unlink = self.useFixture(MockPatch("os.unlink")).mock
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        incoming_dir.mkdir(parents=True, exist_ok=True)
        write_file(str(incoming_dir / "foo.oval.xml.bz2"), b"test")
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(published_dir / "foo2.oval.xml.bz2"), b"test")

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)
        script.main()
        mock_copy.assert_not_called()
        mock_unlink.assert_has_calls(
            [
                call(
                    "{}/breezy-autotest/main/oval/foo2.oval.xml.bz2".format(
                        getPubConfig(archive).distsroot
                    ),
                ),
            ]
        )

    def test_syncOVALDataFilesForSuite_skips_by_hash_directory(self):
        """`by-hash` directory is generated by the archive indexing machinery

        It must not be deleted, so we need to skip it."""
        self.setUpOVALDataRsync()
        self.useFixture(
            MockPatch("lp.archivepublisher.scripts.publishdistro.check_call")
        )
        archive = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        incoming_dir = (
            Path(self.oval_data_root)
            / archive.reference
            / "breezy-autotest"
            / "main"
        )
        write_file(str(incoming_dir / "test"), b"test")
        published_dir = (
            Path(getPubConfig(archive).distsroot)
            / "breezy-autotest"
            / "main"
            / "oval"
        )
        # create oval files and the `by-hash` dir with some test files
        write_file(str(published_dir / "foo.oval.xml.bz2"), b"test")
        write_file(str(published_dir / "foo2.oval.xml.bz2"), b"test")
        by_hash_dir = published_dir / "by-hash"
        by_hash_dir.mkdir()
        (by_hash_dir / "a").touch()
        (by_hash_dir / "b").touch()

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        script.main()

        # `by-hash` still exists and is indeed a directory
        self.assertTrue(by_hash_dir.is_dir())
        # and still contains the two test files
        self.assertEqual(2, len(list(by_hash_dir.iterdir())))

    def test_lockfilename_option_overrides_default_lock(self):
        lockfilename = "foo.lock"
        script = self.makeScript(args=["--lockfilename", lockfilename])
        self.assertEqual(
            script.lockfilepath, os.path.join(LOCK_PATH, lockfilename)
        )

    def test_default_lock(self):
        script = self.makeScript()
        self.assertEqual(
            script.lockfilepath, os.path.join(LOCK_PATH, GLOBAL_PUBLISHER_LOCK)
        )

    def test_main_no_history_enabled(self):
        """Test that PublishingHistory and PublisherRun records are not created
        if the feature flag is not enabled."""
        archive1 = self.factory.makeArchive()
        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive1.distribution])
        script.getTargetArchives = FakeMethod([archive1])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        script.main()

        # Check PublishingHistory record was not created
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        histories1 = list(publishing_history_set.getByArchive(archive1))
        self.assertEqual(0, len(histories1))

        # Check PublisherRun record was not created
        store = IStore(ArchivePublisherRun)
        publisher_run = store.find(ArchivePublisherRun).one()
        self.assertIsNone(publisher_run)

    def test_main_creates_publishing_history_records(self):
        """Test that PublishingHistory and PublisherRun records are created."""
        self.useFixture(
            FeatureFixture({ARCHIVEPUBLISHER_HISTORY_ENABLED: "on"})
        )

        archive1 = self.factory.makeArchive()
        archive2 = self.factory.makeArchive()
        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive1.distribution])
        script.getTargetArchives = FakeMethod([archive1, archive2])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        script.main()

        # Check PublishingHistory records were created for both archives
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        histories1 = list(publishing_history_set.getByArchive(archive1))
        histories2 = list(publishing_history_set.getByArchive(archive2))

        self.assertEqual(1, len(histories1))
        self.assertEqual(1, len(histories2))

        # Check they reference the same publisher run
        self.assertIsNotNone(histories1[0].publisher_run)
        self.assertIsNotNone(histories2[0].publisher_run)
        self.assertEqual(
            histories1[0].publisher_run.id, histories2[0].publisher_run.id
        )

        # Check that the status is SUCCEEDED
        self.assertEqual(
            ArchivePublisherRunStatus.SUCCEEDED,
            histories1[0].publisher_run.status,
        )

        # Check that dates are set
        self.assertIsNotNone(histories1[0].publisher_run.date_started)
        self.assertIsNotNone(histories1[0].publisher_run.date_finished)
        self.assertGreater(
            histories1[0].publisher_run.date_finished,
            histories1[0].publisher_run.date_started,
        )

    def test_main_publisher_run_failed(self):
        """Test that main sets the PublisherRun status and timestamps to
        FAILED on exception."""
        self.useFixture(
            FeatureFixture({ARCHIVEPUBLISHER_HISTORY_ENABLED: "on"})
        )

        # Mock processArchive to raise an exception
        def mock_processArchive(
            self, archive_id, publisher_run_id, reset_store=True
        ):
            raise RuntimeError("Test exception")

        self.useFixture(
            MockPatch(
                "lp.archivepublisher.scripts.publishdistro."
                "PublishDistro.processArchive",
                mock_processArchive,
            )
        )
        archive1 = self.factory.makeArchive()
        archive2 = self.factory.makeArchive()
        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive1.distribution])
        script.getTargetArchives = FakeMethod([archive1, archive2])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        try:
            script.main()
        except RuntimeError:
            pass  # Expected exception

        # Check PublishingHistory records were created for both archives
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        histories1 = list(publishing_history_set.getByArchive(archive1))
        histories2 = list(publishing_history_set.getByArchive(archive2))

        # Check that no history record was created for any archive
        self.assertEqual(0, len(histories1))
        self.assertEqual(0, len(histories2))

        store = IStore(ArchivePublisherRun)
        publisher_run = store.find(ArchivePublisherRun).one()

        # Check that the status is FAILED
        self.assertEqual(
            ArchivePublisherRunStatus.FAILED, publisher_run.status
        )

        # Check that dates are set
        self.assertIsNotNone(publisher_run.date_started)
        self.assertIsNotNone(publisher_run.date_finished)
        self.assertGreater(
            publisher_run.date_finished,
            publisher_run.date_started,
        )

    def test_main_no_archives(self):
        """Test that history is None if there are no archives to publish."""
        self.useFixture(
            FeatureFixture({ARCHIVEPUBLISHER_HISTORY_ENABLED: "on"})
        )
        archive = self.factory.makeArchive()

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        script.main()

        # Check that no PublishingHistory records were created
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        histories = list(publishing_history_set.getByArchive(archive))
        self.assertEqual(0, len(histories))

        store = IStore(ArchivePublisherRun)
        publisher_run = store.find(ArchivePublisherRun).one()

        # Check ArchivePublisherRun is None
        self.assertIsNone(publisher_run)

    def test_main_no_work_done_no_history(self):
        """Test that history is not created if work_done=False."""
        # processArchive() checks if the archive was published or deleted,
        # and if neither happened, it does not commmit the transaction. We use
        # this to avoid creating history records when no work is done.
        self.useFixture(
            FeatureFixture({ARCHIVEPUBLISHER_HISTORY_ENABLED: "on"})
        )

        archive = self.factory.makeArchive()
        # Disable publication for this archive: work_done=False
        removeSecurityProxy(archive).publish = False

        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive.distribution])
        script.getTargetArchives = FakeMethod([archive])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        script.main()

        # Check that no PublishingHistory records were created
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        histories = list(publishing_history_set.getByArchive(archive))
        self.assertEqual(0, len(histories))

        store = IStore(ArchivePublisherRun)
        publisher_run = store.find(ArchivePublisherRun).one()

        # Check that PublisherRun is None
        self.assertIsNone(publisher_run)

    def test_main_partial_failure_history(self):
        """Test that if one archive fails, previous ones are committed but run
        is FAILED."""
        self.useFixture(
            FeatureFixture({ARCHIVEPUBLISHER_HISTORY_ENABLED: "on"})
        )

        archive1 = self.factory.makeArchive()
        archive2 = self.factory.makeArchive()
        script = self.makeScript()
        script.txn = FakeTransaction()
        script.findDistros = FakeMethod([archive1.distribution])
        # Ensure archive1 is processed before archive2
        script.getTargetArchives = FakeMethod([archive1, archive2])
        publisher = FakePublisher()
        script.getPublisher = FakeMethod(publisher)

        # Mock processArchive to succeed for archive1 and fail for archive2.
        def mock_processArchive(
            archive_id, publisher_run_id, reset_store=True
        ):
            if archive_id == archive1.id:
                # Simulate success: create history and commit
                script._create_publishing_history(archive_id, publisher_run_id)
                script.txn.commit()
            else:
                raise RuntimeError("Simulated failure")

        script.processArchive = mock_processArchive

        try:
            script.main()
        except RuntimeError:
            pass

        # Check archive1 history exists
        publishing_history_set = getUtility(IArchivePublishingHistorySet)
        histories1 = list(publishing_history_set.getByArchive(archive1))
        self.assertEqual(1, len(histories1))

        # Check archive2 history does not exist
        histories2 = list(publishing_history_set.getByArchive(archive2))
        self.assertEqual(0, len(histories2))

        # Check run status is FAILED
        store = IStore(ArchivePublisherRun)
        publisher_run = store.find(ArchivePublisherRun).one()
        self.assertEqual(
            ArchivePublisherRunStatus.FAILED, publisher_run.status
        )


load_tests = load_tests_apply_scenarios
