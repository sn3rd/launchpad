# Copyright 2009-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `CustomUploads`."""

import os
import shutil
import tarfile
import tempfile
import unittest

from fixtures import MonkeyPatch
from testtools.matchers import Equals, MatchesDict, Not, PathExists
from testtools.twistedsupport import AsynchronousDeferredRunTest
from twisted.internet import defer
from zope.component import getUtility

from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.customupload import (
    CustomUpload,
    CustomUploadTarballBadFile,
    CustomUploadTarballBadSymLink,
    CustomUploadTarballInvalidFileType,
)
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    IArchiveGPGSigningKey,
)
from lp.archivepublisher.interfaces.publisherconfig import IPublisherConfigSet
from lp.archivepublisher.tests.test_run_parts import RunPartsMixin
from lp.services.gpg.interfaces import IGPGHandler
from lp.services.osutils import write_file
from lp.soyuz.enums import ArchivePurpose
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.fakemethod import FakeMethod
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import LaunchpadZopelessLayer


class TestCustomUpload(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="archive_root_")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def assertEntries(self, entries):
        self.assertEqual(entries, sorted(os.listdir(self.test_dir)))

    def testFixCurrentSymlink(self):
        """Test `CustomUpload.fixCurrentSymlink` behaviour.

        Ensure only 3 entries named as valid versions are kept around and
        the 'current' symbolic link is created (or updated) to point to the
        latests entry.

        Also check if it copes with entries not named as valid versions and
        leave them alone.
        """
        # Setup a bogus `CustomUpload` object with the 'targetdir' pointing
        # to the directory created for the test.
        custom_processor = CustomUpload()
        custom_processor.targetdir = self.test_dir

        # Let's create 4 entries named as valid versions.
        os.mkdir(os.path.join(self.test_dir, "1.0"))
        os.mkdir(os.path.join(self.test_dir, "1.1"))
        os.mkdir(os.path.join(self.test_dir, "1.2"))
        os.mkdir(os.path.join(self.test_dir, "1.3"))
        self.assertEntries(["1.0", "1.1", "1.2", "1.3"])

        # `fixCurrentSymlink` will keep only the latest 3 and create a
        # 'current' symbolic link the highest one.
        custom_processor.fixCurrentSymlink()
        self.assertEntries(["1.1", "1.2", "1.3", "current"])
        self.assertEqual(
            "1.3", os.readlink(os.path.join(self.test_dir, "current"))
        )

        # When there is a invalid version present in the directory it is
        # ignored, since it was probably put there manually. The symbolic
        # link still pointing to the latest version.
        os.mkdir(os.path.join(self.test_dir, "1.4"))
        os.mkdir(os.path.join(self.test_dir, "alpha-5"))
        custom_processor.fixCurrentSymlink()
        self.assertEntries(["1.2", "1.3", "1.4", "alpha-5", "current"])
        self.assertEqual(
            "1.4", os.readlink(os.path.join(self.test_dir, "current"))
        )


class TestTarfileVerification(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.tarfile_path = self.makeTemporaryDirectory()
        self.tarfile_name = os.path.join(self.tarfile_path, "test_tarfile.tar")
        self.custom_processor = CustomUpload()
        self.custom_processor.tmpdir = None
        self.custom_processor.tarfile_path = self.tarfile_name

    def createTarfile(self):
        tar_file = tarfile.open(self.tarfile_name, mode="w")
        root_info = tarfile.TarInfo(name="./")
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        tar_file.addfile(root_info)
        return tar_file

    def createSymlinkInfo(self, target, name="i_am_a_symlink"):
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        return info

    def createTarfileWithSymlink(self, target):
        info = self.createSymlinkInfo(target)
        tar_file = self.createTarfile()
        tar_file.addfile(info)
        return tar_file

    def createTarfileWithFile(self, file_type, name="testfile"):
        info = tarfile.TarInfo(name=name)
        info.type = file_type
        tar_file = self.createTarfile()
        tar_file.addfile(info)
        return tar_file

    def assertFails(self, exception, tar_file):
        # Close the tar so all data is flushed to disk before extract().
        tar_file.close()
        self.assertRaises(exception, self.custom_processor.extract)
        if self.custom_processor.tmpdir is not None:
            shutil.rmtree(self.custom_processor.tmpdir, True)

    def assertPasses(self, tar_file):
        # Close the tar so all data is flushed to disk before extract().
        tar_file.close()
        self.custom_processor.extract()
        shutil.rmtree(self.custom_processor.tmpdir, True)

    def testFailsToExtractBadSymlink(self):
        """Fail if a symlink's target is outside the tmp tree."""
        tar_file = self.createTarfileWithSymlink(target="/etc/passwd")
        self.assertFails(CustomUploadTarballBadSymLink, tar_file)

    def testFailsToExtractBadRelativeSymlink(self):
        """Fail if a symlink's relative target is outside the tmp tree."""
        tar_file = self.createTarfileWithSymlink(target="../outside")
        self.assertFails(CustomUploadTarballBadSymLink, tar_file)

    def testFailsToExtractBadFileType(self):
        """Fail if a file in a tarfile is not a regular file or a symlink."""
        tar_file = self.createTarfileWithFile(tarfile.FIFOTYPE)
        self.assertFails(CustomUploadTarballInvalidFileType, tar_file)

    def testFailsToExtractBadFileLocation(self):
        """Fail if the file resolves to a path outside the tmp tree."""
        tar_file = self.createTarfileWithFile(tarfile.REGTYPE, "../outside")
        self.assertFails(CustomUploadTarballBadFile, tar_file)

    def testFailsToExtractBadAbsoluteFileLocation(self):
        """Fail if the file resolves to a path outside the tmp tree."""
        tar_file = self.createTarfileWithFile(tarfile.REGTYPE, "/etc/passwd")
        self.assertFails(CustomUploadTarballBadFile, tar_file)

    def testRegularFileDoesntRaise(self):
        """Adding a normal file should pass inspection."""
        tar_file = self.createTarfileWithFile(tarfile.REGTYPE)
        self.assertPasses(tar_file)

    def testDirectoryDoesntRaise(self):
        """Adding a directory should pass inspection."""
        tar_file = self.createTarfileWithFile(tarfile.DIRTYPE)
        self.assertPasses(tar_file)

    def testSymlinkDoesntRaise(self):
        """Adding a symlink should pass inspection."""
        tar_file = self.createTarfileWithSymlink(target="something/blah")
        self.assertPasses(tar_file)

    def testRelativeSymlinkToRootDoesntRaise(self):
        tar_file = self.createTarfileWithSymlink(target=".")
        self.assertPasses(tar_file)

    def testRelativeSymlinkTargetInsideDirectoryDoesntRaise(self):
        tar_file = self.createTarfileWithFile(tarfile.DIRTYPE, name="testdir")
        info = self.createSymlinkInfo(
            name="testdir/symlink", target="../dummy"
        )
        tar_file.addfile(info)
        self.assertPasses(tar_file)

    def testFailsToExtractSymlinkTraversalAttack(self):
        """
        Fail if a symlink causes a later file to escape the tmp tree.
        """
        tar_file = self.createTarfile()
        symlink_info = self.createSymlinkInfo(target=".", name="a")
        tar_file.addfile(symlink_info)
        evil_info = tarfile.TarInfo(name="a/a/a/a/../../../../etc/cron.d/evil")
        evil_info.type = tarfile.REGTYPE
        tar_file.addfile(evil_info)
        self.assertFails(CustomUploadTarballBadFile, tar_file)


class TestSigning(TestCaseWithFactory, RunPartsMixin):
    layer = LaunchpadZopelessLayer
    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    def setUp(self):
        super().setUp()
        self.temp_dir = self.makeTemporaryDirectory()
        self.distro = self.factory.makeDistribution()
        db_pubconf = getUtility(IPublisherConfigSet).getByDistribution(
            self.distro
        )
        db_pubconf.root_dir = self.temp_dir
        self.archive = self.factory.makeArchive(
            distribution=self.distro, purpose=ArchivePurpose.PRIMARY
        )

    def test_sign_without_signing_key(self):
        filename = os.path.join(getPubConfig(self.archive).archiveroot, "file")
        self.assertIsNone(self.archive.signing_key)
        custom_processor = CustomUpload()
        custom_processor.sign(self.archive, "suite", filename)
        self.assertThat("%s.gpg" % filename, Not(PathExists()))

    @defer.inlineCallbacks
    def test_sign_with_signing_key(self):
        filename = os.path.join(getPubConfig(self.archive).archiveroot, "file")
        write_file(filename, b"contents")
        self.assertIsNone(self.archive.signing_key)
        self.useFixture(InProcessKeyServerFixture()).start()
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(self.archive).setSigningKey(
            key_path, async_keyserver=True
        )
        self.assertIsNotNone(self.archive.signing_key)
        custom_processor = CustomUpload()
        custom_processor.sign(self.archive, "suite", filename)
        with open(filename, "rb") as cleartext_file:
            cleartext = cleartext_file.read()
            with open("%s.gpg" % filename, "rb") as signature_file:
                signature = getUtility(IGPGHandler).getVerifiedSignature(
                    cleartext, signature_file.read()
                )
        self.assertEqual(
            self.archive.signing_key.fingerprint, signature.fingerprint
        )

    def test_sign_with_external_run_parts(self):
        self.enableRunParts(distribution_name=self.distro.name)
        archiveroot = getPubConfig(self.archive).archiveroot
        filename = os.path.join(archiveroot, "file")
        write_file(filename, b"contents")
        self.assertIsNone(self.archive.signing_key)
        run_parts_fixture = self.useFixture(
            MonkeyPatch(
                "lp.archivepublisher.archivegpgsigningkey.run_parts",
                FakeMethod(),
            )
        )
        custom_processor = CustomUpload()
        custom_processor.sign(self.archive, "suite", filename)
        args, kwargs = run_parts_fixture.new_value.calls[0]
        self.assertEqual((self.distro.name, "sign.d"), args)
        self.assertThat(
            kwargs["env"],
            MatchesDict(
                {
                    "ARCHIVEROOT": Equals(archiveroot),
                    "DISTRIBUTION": Equals(self.distro.name),
                    "INPUT_PATH": Equals(filename),
                    "MODE": Equals("detached"),
                    "OUTPUT_PATH": Equals("%s.gpg" % filename),
                    "SITE_NAME": Equals("launchpad.test"),
                    "SUITE": Equals("suite"),
                }
            ),
        )
