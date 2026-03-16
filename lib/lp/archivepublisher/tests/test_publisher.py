# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for publisher class."""

import bz2
import gzip
import hashlib
import lzma
import os
import shutil
import stat
import tempfile
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from functools import partial
from textwrap import dedent
from typing import Optional, Sequence, Tuple
from unittest import mock

import six
import transaction
from debian.deb822 import Release
from fixtures import MonkeyPatch
from testscenarios import WithScenarios, load_tests_apply_scenarios
from testtools.matchers import (
    ContainsAll,
    DirContains,
    Equals,
    FileContains,
    Is,
    LessThan,
    Matcher,
    MatchesDict,
    MatchesSetwise,
    MatchesStructure,
    Not,
    PathExists,
    SamePath,
)
from testtools.twistedsupport import AsynchronousDeferredRunTest
from twisted.internet import defer
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.diskpool import DiskPool
from lp.archivepublisher.interfaces.archivegpgsigningkey import (
    IArchiveGPGSigningKey,
)
from lp.archivepublisher.publishing import (
    BY_HASH_STAY_OF_EXECUTION,
    ByHash,
    ByHashes,
    DirectoryHash,
    I18nIndex,
    Publisher,
    getPublisher,
)
from lp.archivepublisher.tests.artifactory_fixture import (
    FakeArtifactoryFixture,
)
from lp.archivepublisher.tests.test_run_parts import RunPartsMixin
from lp.archivepublisher.utils import RepositoryIndexFile
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket, pocketsuffix
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import flush_database_caches
from lp.services.gpg.interfaces import IGPGHandler
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.services.osutils import open_for_writing
from lp.services.utils import file_exists
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveRepositoryFormat,
    ArchiveStatus,
    BinaryPackageFormat,
    IndexCompressionType,
    PackagePublishingStatus,
    PackageUploadStatus,
)
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.interfaces.archivefile import IArchiveFileSet
from lp.soyuz.tests.test_publishing import TestNativePublishingBase
from lp.testing import TestCaseWithFactory, login_person
from lp.testing.fakemethod import FakeMethod
from lp.testing.gpgkeys import gpgkeysdir
from lp.testing.keyserver import InProcessKeyServerFixture
from lp.testing.layers import LaunchpadZopelessLayer, ZopelessDatabaseLayer
from lp.testing.matchers import FileContainsBytes

RELEASE = PackagePublishingPocket.RELEASE
PROPOSED = PackagePublishingPocket.PROPOSED
BACKPORTS = PackagePublishingPocket.BACKPORTS


class TestPublisherBase(TestNativePublishingBase):
    """Basic setUp for `TestPublisher` classes.

    Extends `TestNativePublishingBase` already.
    """

    def setUp(self):
        """Override cprov PPA distribution to 'ubuntutest'."""
        TestNativePublishingBase.setUp(self)

        # Override cprov's PPA distribution, because we can't publish
        # 'ubuntu' in the current sampledata.
        cprov = getUtility(IPersonSet).getByName("cprov")
        naked_archive = removeSecurityProxy(cprov.archive)
        naked_archive.distribution = self.ubuntutest
        self.ubuntu = getUtility(IDistributionSet)["ubuntu"]


class TestPublisherSeries(TestNativePublishingBase):
    """Test the `Publisher` methods that publish individual series."""

    def setUp(self):
        super().setUp()
        self.publisher = None

    def _createLinkedPublication(self, name, pocket):
        """Return a linked pair of source and binary publications."""
        pub_source = self.getPubSource(
            sourcename=name, filecontent=b"Hello", pocket=pocket
        )

        binaryname = "%s-bin" % name
        pub_bin = self.getPubBinaries(
            binaryname=binaryname,
            filecontent=b"World",
            pub_source=pub_source,
            pocket=pocket,
        )[0]

        return (pub_source, pub_bin)

    def _createDefaultSourcePublications(self):
        """Create and return default source publications.

        See `TestNativePublishingBase.getPubSource` for more information.

        It creates the following publications in breezy-autotest context:

         * a PENDING publication for RELEASE pocket;
         * a PUBLISHED publication for RELEASE pocket;
         * a PENDING publication for UPDATES pocket;

        Returns the respective ISPPH objects as a tuple.
        """
        pub_pending_release = self.getPubSource(
            sourcename="first",
            status=PackagePublishingStatus.PENDING,
            pocket=PackagePublishingPocket.RELEASE,
        )

        pub_published_release = self.getPubSource(
            sourcename="second",
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.RELEASE,
        )

        pub_pending_updates = self.getPubSource(
            sourcename="third",
            status=PackagePublishingStatus.PENDING,
            pocket=PackagePublishingPocket.UPDATES,
        )

        return (
            pub_pending_release,
            pub_published_release,
            pub_pending_updates,
        )

    def _createDefaultBinaryPublications(self):
        """Create and return default binary publications.

        See `TestNativePublishingBase.getPubBinaries` for more information.

        It creates the following publications in breezy-autotest context:

         * a PENDING publication for RELEASE pocket;
         * a PUBLISHED publication for RELEASE pocket;
         * a PENDING publication for UPDATES pocket;

        Returns the respective IBPPH objects as a tuple.
        """
        pub_pending_release = self.getPubBinaries(
            binaryname="first",
            status=PackagePublishingStatus.PENDING,
            pocket=PackagePublishingPocket.RELEASE,
        )[0]

        pub_published_release = self.getPubBinaries(
            binaryname="second",
            status=PackagePublishingStatus.PUBLISHED,
            pocket=PackagePublishingPocket.RELEASE,
        )[0]

        pub_pending_updates = self.getPubBinaries(
            binaryname="third",
            status=PackagePublishingStatus.PENDING,
            pocket=PackagePublishingPocket.UPDATES,
        )[0]

        return (
            pub_pending_release,
            pub_published_release,
            pub_pending_updates,
        )

    def checkLegalPocket(self, status, pocket):
        distroseries = self.factory.makeDistroSeries(
            distribution=self.ubuntutest, status=status
        )
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, distroseries.main_archive
        )
        return publisher.checkLegalPocket(distroseries, pocket, False)

    def test_checkLegalPocket_allows_unstable_release(self):
        """Publishing to RELEASE in a DEVELOPMENT series is allowed."""
        self.assertTrue(
            self.checkLegalPocket(
                SeriesStatus.DEVELOPMENT, PackagePublishingPocket.RELEASE
            )
        )

    def test_checkLegalPocket_allows_unstable_proposed(self):
        """Publishing to PROPOSED in a DEVELOPMENT series is allowed."""
        self.assertTrue(
            self.checkLegalPocket(
                SeriesStatus.DEVELOPMENT, PackagePublishingPocket.PROPOSED
            )
        )

    def test_checkLegalPocket_forbids_unstable_updates(self):
        """Publishing to UPDATES in a DEVELOPMENT series is forbidden."""
        self.assertFalse(
            self.checkLegalPocket(
                SeriesStatus.DEVELOPMENT, PackagePublishingPocket.UPDATES
            )
        )

    def test_checkLegalPocket_forbids_stable_release(self):
        """Publishing to RELEASE in a CURRENT series is forbidden."""
        self.assertFalse(
            self.checkLegalPocket(
                SeriesStatus.CURRENT, PackagePublishingPocket.RELEASE
            )
        )

    def test_checkLegalPocket_allows_stable_proposed(self):
        """Publishing to PROPOSED in a CURRENT series is allowed."""
        self.assertTrue(
            self.checkLegalPocket(
                SeriesStatus.CURRENT, PackagePublishingPocket.PROPOSED
            )
        )

    def test_checkLegalPocket_allows_stable_updates(self):
        """Publishing to UPDATES in a CURRENT series is allowed."""
        self.assertTrue(
            self.checkLegalPocket(
                SeriesStatus.CURRENT, PackagePublishingPocket.UPDATES
            )
        )

    def _ensurePublisher(self):
        """Create self.publisher if needed."""
        if self.publisher is None:
            self.publisher = Publisher(
                self.logger,
                self.config,
                self.disk_pool,
                self.breezy_autotest.main_archive,
            )

    def _publish(self, pocket, is_careful=False):
        """Publish the test IDistroSeries and its IDistroArchSeries."""
        self._ensurePublisher()
        self.publisher.findAndPublishSources(is_careful=is_careful)
        self.publisher.findAndPublishBinaries(is_careful=is_careful)
        self.layer.txn.commit()

    def checkPublicationsAreConsidered(self, pocket):
        """Check if publications are considered for a given pocket.

        Source and Binary publications to the given pocket get PUBLISHED in
        database and on disk.
        """
        pub_source, pub_bin = self._createLinkedPublication(
            name="foo", pocket=pocket
        )
        self._publish(pocket=pocket)

        # source and binary PUBLISHED in database.
        IStore(pub_source).flush()
        IStore(pub_bin).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        self.assertEqual(pub_bin.status, PackagePublishingStatus.PUBLISHED)

        # source and binary PUBLISHED on disk.
        foo_dsc = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        with open(foo_dsc) as foo_dsc_file:
            self.assertEqual(foo_dsc_file.read().strip(), "Hello")
        foo_deb = "%s/main/f/foo/foo-bin_666_all.deb" % self.pool_dir
        with open(foo_deb) as foo_deb_file:
            self.assertEqual(foo_deb_file.read().strip(), "World")

    def checkPublicationsAreIgnored(self, pocket):
        """Check if publications are ignored for a given pocket.

        Source and Binary publications to the given pocket are still PENDING
        in database.
        """
        pub_source, pub_bin = self._createLinkedPublication(
            name="bar", pocket=pocket
        )
        self._publish(pocket=pocket)

        # The publications to pocket were ignored.
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)
        self.assertEqual(pub_bin.status, PackagePublishingStatus.PENDING)

    def checkSourceLookup(self, expected_result, is_careful=False):
        """Check the results of an IDistroSeries publishing lookup."""
        self._ensurePublisher()
        pub_records = self.publisher.getPendingSourcePublications(
            is_careful=is_careful
        )
        pub_records = [
            pub
            for pub in pub_records
            if pub.distroseries == self.breezy_autotest
        ]

        self.assertEqual(len(expected_result), len(pub_records))
        self.assertEqual(
            [item.id for item in expected_result],
            [pub.id for pub in pub_records],
        )

    def checkBinaryLookup(self, expected_result, is_careful=False):
        """Check the results of an IDistroArchSeries publishing lookup."""
        self._ensurePublisher()
        pub_records = self.publisher.getPendingBinaryPublications(
            is_careful=is_careful
        )
        pub_records = [
            pub
            for pub in pub_records
            if pub.distroarchseries == self.breezy_autotest_i386
        ]

        self.assertEqual(len(expected_result), len(pub_records))
        self.assertEqual(
            [item.id for item in expected_result],
            [pub.id for pub in pub_records],
        )

    def testPublishUnstableDistroSeries(self):
        """Top level publication for IDistroSeries in 'unstable' states.

        Publications to RELEASE pocket are considered.
        Publication to UPDATES pocket (post-release pockets) are ignored
        """
        self.assertEqual(
            self.breezy_autotest.status, SeriesStatus.EXPERIMENTAL
        )
        self.assertEqual(self.breezy_autotest.isUnstable(), True)
        self.checkPublicationsAreConsidered(PackagePublishingPocket.RELEASE)
        self.checkPublicationsAreIgnored(PackagePublishingPocket.UPDATES)

    def testPublishStableDistroSeries(self):
        """Top level publication for IDistroSeries in 'stable' states.

        Publications to RELEASE pocket are ignored.
        Publications to UPDATES pocket are considered.
        """
        # Release ubuntu/breezy-autotest.
        self.breezy_autotest.status = SeriesStatus.CURRENT
        self.layer.commit()

        self.assertEqual(self.breezy_autotest.status, SeriesStatus.CURRENT)
        self.assertEqual(self.breezy_autotest.isUnstable(), False)
        self.checkPublicationsAreConsidered(PackagePublishingPocket.UPDATES)
        self.checkPublicationsAreIgnored(PackagePublishingPocket.RELEASE)

    def testPublishFrozenDistroSeries(self):
        """Top level publication for IDistroSeries in FROZEN state.

        Publications to both, RELEASE and UPDATES, pockets are considered.
        """
        # Release ubuntu/breezy-autotest.
        self.breezy_autotest.status = SeriesStatus.FROZEN
        self.layer.commit()

        self.assertEqual(self.breezy_autotest.status, SeriesStatus.FROZEN)
        self.assertEqual(self.breezy_autotest.isUnstable(), True)
        self.checkPublicationsAreConsidered(PackagePublishingPocket.UPDATES)
        self.checkPublicationsAreConsidered(PackagePublishingPocket.RELEASE)

    def testSourcePublicationLookUp(self):
        """Source publishing record lookup.

        Check if Publisher.getPendingSourcePublications() returns only
        pending publications.
        """
        (
            pub_pending_release,
            pub_published_release,
            pub_pending_updates,
        ) = self._createDefaultSourcePublications()

        # Normally, only pending records are considered.
        self.checkSourceLookup(
            expected_result=[pub_pending_release, pub_pending_updates]
        )

        # In careful mode, both pending and published records are
        # considered, ordered by distroseries, pocket, ID.
        self.checkSourceLookup(
            expected_result=[
                pub_published_release,
                pub_pending_release,
                pub_pending_updates,
            ],
            is_careful=True,
        )

    def testBinaryPublicationLookUp(self):
        """Binary publishing record lookup.

        Check if Publisher.getPendingBinaryPublications() returns only
        pending publications.
        """
        (
            pub_pending_release,
            pub_published_release,
            pub_pending_updates,
        ) = self._createDefaultBinaryPublications()
        self.layer.commit()

        # Normally, only pending records are considered.
        self.checkBinaryLookup(
            expected_result=[pub_pending_release, pub_pending_updates]
        )

        # In careful mode, both pending and published records are
        # considered, ordered by distroseries, pocket, architecture tag, ID.
        self.checkBinaryLookup(
            expected_result=[
                pub_published_release,
                pub_pending_release,
                pub_pending_updates,
            ],
            is_careful=True,
        )

    def test_publishing_disabled_distroarchseries(self):
        # Disabled DASes will not receive new publications at all.

        # Make an arch-all source and some builds for it.
        archive = self.factory.makeArchive(
            distribution=self.ubuntutest, virtualized=False
        )
        source = self.getPubSource(archive=archive, architecturehintlist="all")
        [build_i386] = source.createMissingBuilds()
        bin_i386 = self.uploadBinaryForBuild(build_i386, "bin-i386")

        # Now make sure they have a packageupload (but no publishing
        # records).
        changes_file_name = "%s_%s_%s.changes" % (
            bin_i386.name,
            bin_i386.version,
            build_i386.arch_tag,
        )
        pu_i386 = self.addPackageUpload(
            build_i386.archive,
            build_i386.distro_arch_series.distroseries,
            build_i386.pocket,
            changes_file_content=b"anything",
            changes_file_name=changes_file_name,
            upload_status=PackageUploadStatus.ACCEPTED,
        )
        pu_i386.addBuild(build_i386)

        # Now we make hppa a disabled architecture, and then call the
        # publish method on the packageupload.  The arch-all binary
        # should be published only in the i386 arch, not the hppa one.
        hppa = pu_i386.distroseries.getDistroArchSeries("hppa")
        hppa.enabled = False
        for pu_build in pu_i386.builds:
            pu_build.publish()

        publications = archive.getAllPublishedBinaries(name="bin-i386")

        self.assertEqual(1, publications.count())
        self.assertEqual(
            "i386", publications[0].distroarchseries.architecturetag
        )


class ByHashHasContents(Matcher):
    """Matches if a by-hash directory has exactly the specified contents."""

    def __init__(self, contents):
        self.contents = contents
        self.expected_hashes = OrderedDict(
            [
                ("SHA256", "sha256"),
            ]
        )

    def __str__(self):
        return f"ByHashHasContents({self.contents})"

    def match(self, by_hash_path):
        mismatch = DirContains(self.expected_hashes.keys()).match(by_hash_path)
        if mismatch is not None:
            return mismatch
        best_hashname, best_hashattr = list(self.expected_hashes.items())[-1]
        for hashname, hashattr in self.expected_hashes.items():
            digests = {
                getattr(hashlib, hashattr)(content).hexdigest(): content
                for content in self.contents
            }
            path = os.path.join(by_hash_path, hashname)
            mismatch = DirContains(digests.keys()).match(path)
            if mismatch is not None:
                return mismatch
            for digest, content in digests.items():
                full_path = os.path.join(path, digest)
                if hashname == best_hashname:
                    mismatch = FileContainsBytes(content).match(full_path)
                    if mismatch is not None:
                        return mismatch
                else:
                    best_path = os.path.join(
                        by_hash_path,
                        best_hashname,
                        getattr(hashlib, best_hashattr)(content).hexdigest(),
                    )
                    mismatch = SamePath(best_path).match(full_path)
                    if mismatch is not None:
                        return mismatch


class ByHashesHaveContents(Matcher):
    """Matches if only these by-hash directories exist with proper contents."""

    def __init__(self, path_contents):
        self.path_contents = path_contents

    def __str__(self):
        return f"ByHashesHaveContents({self.path_contents})"

    def match(self, root):
        children = set()
        for dirpath, dirnames, _ in os.walk(root):
            if "by-hash" in dirnames:
                children.add(os.path.relpath(dirpath, root))
        mismatch = MatchesSetwise(
            *(Equals(path) for path in self.path_contents)
        ).match(children)
        if mismatch is not None:
            return mismatch
        for path, contents in self.path_contents.items():
            by_hash_path = os.path.join(root, path, "by-hash")
            mismatch = ByHashHasContents(contents).match(by_hash_path)
            if mismatch is not None:
                return mismatch


class TestByHash(TestCaseWithFactory):
    """Unit tests for details of handling a single by-hash directory tree."""

    layer = LaunchpadZopelessLayer

    def test_add(self):
        root = self.makeTemporaryDirectory()
        contents = [b"abc\n", b"def\n"]
        lfas = [
            self.factory.makeLibraryFileAlias(content=content)
            for content in contents
        ]
        transaction.commit()
        by_hash = ByHash(root, "dists/foo/main/source", DevNullLogger())
        for lfa in lfas:
            by_hash.add("Sources", lfa)
        by_hash_path = os.path.join(root, "dists/foo/main/source/by-hash")
        self.assertThat(by_hash_path, ByHashHasContents(contents))

    def test_add_copy_from_path(self):
        root = self.makeTemporaryDirectory()
        content = b"abc\n"
        sources_path = "dists/foo/main/source/Sources"
        with open_for_writing(
            os.path.join(root, sources_path), "wb"
        ) as sources:
            sources.write(content)
        lfa = self.factory.makeLibraryFileAlias(content=content, db_only=True)
        by_hash = ByHash(root, "dists/foo/main/source", DevNullLogger())
        by_hash.add("Sources", lfa, copy_from_path=sources_path)
        by_hash_path = os.path.join(root, "dists/foo/main/source/by-hash")
        self.assertThat(by_hash_path, ByHashHasContents([content]))

    def test_add_existing(self):
        root = self.makeTemporaryDirectory()
        content = b"abc\n"
        lfa = self.factory.makeLibraryFileAlias(content=content)
        by_hash_path = os.path.join(root, "dists/foo/main/source/by-hash")
        sha256_digest = hashlib.sha256(content).hexdigest()
        with open_for_writing(
            os.path.join(by_hash_path, "SHA256", sha256_digest), "wb"
        ) as f:
            f.write(content)
        by_hash = ByHash(root, "dists/foo/main/source", DevNullLogger())
        self.assertThat(by_hash_path, ByHashHasContents([content]))
        by_hash.add("Sources", lfa)
        self.assertThat(by_hash_path, ByHashHasContents([content]))

    def test_known(self):
        root = self.makeTemporaryDirectory()
        content = b"abc\n"
        with open_for_writing(os.path.join(root, "abc"), "wb") as f:
            f.write(content)
        lfa = self.factory.makeLibraryFileAlias(content=content, db_only=True)
        by_hash = ByHash(root, "", DevNullLogger())
        md5 = hashlib.md5(content).hexdigest()
        sha1 = hashlib.sha1(content).hexdigest()
        sha256 = hashlib.sha256(content).hexdigest()
        self.assertFalse(by_hash.known("abc", "MD5Sum", md5))
        self.assertFalse(by_hash.known("abc", "SHA1", sha1))
        self.assertFalse(by_hash.known("abc", "SHA256", sha256))
        by_hash.add("abc", lfa, copy_from_path="abc")
        self.assertFalse(by_hash.known("abc", "MD5Sum", md5))
        self.assertFalse(by_hash.known("abc", "SHA1", sha1))
        self.assertTrue(by_hash.known("abc", "SHA256", sha256))
        self.assertFalse(by_hash.known("def", "SHA256", sha256))
        by_hash.add("def", lfa, copy_from_path="abc")
        self.assertTrue(by_hash.known("def", "SHA256", sha256))

    def test_prune(self):
        root = self.makeTemporaryDirectory()
        content = b"abc\n"
        sources_path = "dists/foo/main/source/Sources"
        with open_for_writing(os.path.join(root, sources_path), "wb") as f:
            f.write(content)
        lfa = self.factory.makeLibraryFileAlias(content=content, db_only=True)
        by_hash = ByHash(root, "dists/foo/main/source", DevNullLogger())
        by_hash.add("Sources", lfa, copy_from_path=sources_path)
        by_hash_path = os.path.join(root, "dists/foo/main/source/by-hash")
        with open_for_writing(os.path.join(by_hash_path, "SHA256/0"), "w"):
            pass
        self.assertThat(by_hash_path, Not(ByHashHasContents([content])))
        by_hash.prune()
        self.assertThat(by_hash_path, ByHashHasContents([content]))

    def test_prune_empty(self):
        root = self.makeTemporaryDirectory()
        by_hash = ByHash(root, "dists/foo/main/source", DevNullLogger())
        by_hash_path = os.path.join(root, "dists/foo/main/source/by-hash")
        with open_for_writing(os.path.join(by_hash_path, "SHA256/0"), "w"):
            pass
        self.assertThat(by_hash_path, PathExists())
        by_hash.prune()
        self.assertThat(by_hash_path, Not(PathExists()))

    def test_prune_old_hashes(self):
        # The initial implementation of by-hash included MD5Sum and SHA1,
        # but we since decided that this was unnecessary cruft.  If they
        # exist on disk, they are pruned in their entirety.
        root = self.makeTemporaryDirectory()
        content = b"abc\n"
        lfa = self.factory.makeLibraryFileAlias(content=content)
        by_hash_path = os.path.join(root, "dists/foo/main/source/by-hash")
        sha256_digest = hashlib.sha256(content).hexdigest()
        with open_for_writing(
            os.path.join(by_hash_path, "SHA256", sha256_digest), "wb"
        ) as f:
            f.write(content)
        for hashname, hashattr in (("MD5Sum", "md5"), ("SHA1", "sha1")):
            digest = getattr(hashlib, hashattr)(content).hexdigest()
            os.makedirs(os.path.join(by_hash_path, hashname))
            os.symlink(
                os.path.join(os.pardir, "SHA256", sha256_digest),
                os.path.join(by_hash_path, hashname, digest),
            )
        by_hash = ByHash(root, "dists/foo/main/source", DevNullLogger())
        by_hash.add("Sources", lfa)
        by_hash.prune()
        self.assertThat(by_hash_path, ByHashHasContents([content]))


class TestByHashes(TestCaseWithFactory):
    """Unit tests for details of handling a set of by-hash directory trees."""

    layer = LaunchpadZopelessLayer

    def test_add(self):
        root = self.makeTemporaryDirectory()
        self.assertThat(root, ByHashesHaveContents({}))
        path_contents = {
            "dists/foo/main/source": {"Sources": b"abc\n"},
            "dists/foo/main/binary-amd64": {
                "Packages.gz": b"def\n",
                "Packages.xz": b"ghi\n",
            },
        }
        by_hashes = ByHashes(root, DevNullLogger())
        for dirpath, contents in path_contents.items():
            for name, content in contents.items():
                path = os.path.join(dirpath, name)
                with open_for_writing(os.path.join(root, path), "wb") as f:
                    f.write(content)
                lfa = self.factory.makeLibraryFileAlias(
                    content=content, db_only=True
                )
                by_hashes.add(path, lfa, copy_from_path=path)
        self.assertThat(
            root,
            ByHashesHaveContents(
                {
                    path: contents.values()
                    for path, contents in path_contents.items()
                }
            ),
        )

    def test_known(self):
        root = self.makeTemporaryDirectory()
        content = b"abc\n"
        sources_path = "dists/foo/main/source/Sources"
        with open_for_writing(os.path.join(root, sources_path), "wb") as f:
            f.write(content)
        lfa = self.factory.makeLibraryFileAlias(content=content, db_only=True)
        by_hashes = ByHashes(root, DevNullLogger())
        md5 = hashlib.md5(content).hexdigest()
        sha1 = hashlib.sha1(content).hexdigest()
        sha256 = hashlib.sha256(content).hexdigest()
        self.assertFalse(by_hashes.known(sources_path, "MD5Sum", md5))
        self.assertFalse(by_hashes.known(sources_path, "SHA1", sha1))
        self.assertFalse(by_hashes.known(sources_path, "SHA256", sha256))
        by_hashes.add(sources_path, lfa, copy_from_path=sources_path)
        self.assertFalse(by_hashes.known(sources_path, "MD5Sum", md5))
        self.assertFalse(by_hashes.known(sources_path, "SHA1", sha1))
        self.assertTrue(by_hashes.known(sources_path, "SHA256", sha256))

    def test_prune(self):
        root = self.makeTemporaryDirectory()
        path_contents = {
            "dists/foo/main/source": {"Sources": b"abc\n"},
            "dists/foo/main/binary-amd64": {
                "Packages.gz": b"def\n",
                "Packages.xz": b"ghi\n",
            },
        }
        by_hashes = ByHashes(root, DevNullLogger())
        for dirpath, contents in path_contents.items():
            for name, content in contents.items():
                path = os.path.join(dirpath, name)
                with open_for_writing(os.path.join(root, path), "wb") as f:
                    f.write(content)
                lfa = self.factory.makeLibraryFileAlias(
                    content=content, db_only=True
                )
                by_hashes.add(path, lfa, copy_from_path=path)
        strays = [
            "dists/foo/main/source/by-hash/SHA256/0",
            "dists/foo/main/binary-amd64/by-hash/SHA256/0",
        ]
        for stray in strays:
            with open_for_writing(os.path.join(root, stray), "w"):
                pass
        matcher = ByHashesHaveContents(
            {
                path: contents.values()
                for path, contents in path_contents.items()
            }
        )
        self.assertThat(root, Not(matcher))
        by_hashes.prune()
        self.assertThat(root, matcher)


class TestPublisher(TestPublisherBase):
    """Testing `Publisher` behaviour."""

    def assertReleaseContentsMatch(self, release, filename, contents):
        for hash_name, hash_func in (
            ("md5sum", hashlib.md5),
            ("sha1", hashlib.sha1),
            ("sha256", hashlib.sha256),
        ):
            self.assertTrue(hash_name in release)
            entries = [
                entry
                for entry in release[hash_name]
                if entry["name"] == filename
            ]
            self.assertEqual(1, len(entries))
            self.assertEqual(
                hash_func(contents).hexdigest(), entries[0][hash_name]
            )
            self.assertEqual(str(len(contents)), entries[0]["size"])

    def parseRelease(self, release_path):
        with open(release_path) as release_file:
            return Release(release_file)

    def parseI18nIndex(self, i18n_index_path):
        with open(i18n_index_path) as i18n_index_file:
            return I18nIndex(i18n_index_file)

    def testInstantiate(self):
        """Publisher should be instantiatable"""
        Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

    def testPublishing(self):
        """Test the non-careful publishing procedure.

        With one PENDING record, respective pocket *dirtied*.
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        pub_source = self.getPubSource(filecontent=b"Hello world")

        publisher.A_publish(False)
        self.layer.txn.commit()

        IStore(pub_source).flush()
        self.assertEqual({"breezy-autotest"}, publisher.dirty_suites)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)

        # file got published
        foo_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        with open(foo_path) as foo_file:
            self.assertEqual("Hello world", foo_file.read().strip())

    def testDeletingPPA(self):
        """Test deleting a PPA"""
        ubuntu_team = getUtility(IPersonSet).getByName("ubuntu-team")
        test_archive = getUtility(IArchiveSet).new(
            distribution=self.ubuntu,
            owner=ubuntu_team,
            purpose=ArchivePurpose.PPA,
            name="testing",
        )

        # Create some source and binary publications, including an
        # orphaned NBS binary.
        spph = self.factory.makeSourcePackagePublishingHistory(
            archive=test_archive
        )
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            archive=test_archive
        )
        orphaned_bpph = self.factory.makeBinaryPackagePublishingHistory(
            archive=test_archive
        )
        bpb = orphaned_bpph.binarypackagerelease.build
        bpb.current_source_publication.supersede()
        dead_spph = self.factory.makeSourcePackagePublishingHistory(
            archive=test_archive
        )
        dead_spph.supersede()
        dead_bpph = self.factory.makeBinaryPackagePublishingHistory(
            archive=test_archive
        )
        dead_bpph.supersede()

        publisher = getPublisher(test_archive, None, self.logger)
        publisher.setupArchiveDirs()

        self.assertTrue(os.path.exists(publisher._config.archiveroot))

        # Create a file inside archiveroot to ensure we're recursive.
        open(
            os.path.join(publisher._config.archiveroot, "test_file"), "w"
        ).close()
        # And a meta file
        os.makedirs(publisher._config.metaroot)
        open(os.path.join(publisher._config.metaroot, "test"), "w").close()

        root_dir = publisher._config.archiveroot
        self.assertTrue(os.path.exists(root_dir))
        publisher.deleteArchive()
        self.assertFalse(os.path.exists(root_dir))
        self.assertFalse(os.path.exists(publisher._config.metaroot))
        self.assertEqual(ArchiveStatus.DELETED, test_archive.status)
        self.assertEqual(False, test_archive.publish)
        self.assertEqual("testing-deletedppa", test_archive.name)

        # All of the archive's active publications have been marked
        # DELETED, and dateremoved has been set early because they've
        # already been removed from disk.
        for pub in (spph, bpph, orphaned_bpph):
            self.assertEqual(PackagePublishingStatus.DELETED, pub.status)
            self.assertEqual("janitor", pub.removed_by.name)
            self.assertIsNot(None, pub.dateremoved)

        # The SUPERSEDED publications now have dateremoved set, even
        # though p-d-r hasn't run over them.
        for pub in (dead_spph, dead_bpph):
            self.assertIs(None, pub.scheduleddeletiondate)
            self.assertIsNot(None, pub.dateremoved)

        # Trying to delete it again won't fail, in the corner case where
        # some admin manually deleted the repo.
        publisher.deleteArchive()

    def testDeletingPPAWithoutMetaData(self):
        ubuntu_team = getUtility(IPersonSet).getByName("ubuntu-team")
        test_archive = getUtility(IArchiveSet).new(
            distribution=self.ubuntu,
            owner=ubuntu_team,
            purpose=ArchivePurpose.PPA,
        )
        logger = BufferLogger()
        publisher = getPublisher(test_archive, None, logger)
        publisher.setupArchiveDirs()

        self.assertTrue(os.path.exists(publisher._config.archiveroot))

        # Create a file inside archiveroot to ensure we're recursive.
        open(
            os.path.join(publisher._config.archiveroot, "test_file"), "w"
        ).close()

        root_dir = publisher._config.archiveroot
        self.assertTrue(os.path.exists(root_dir))
        publisher.deleteArchive()
        self.assertFalse(os.path.exists(root_dir))
        self.assertNotIn("WARNING", logger.getLogBuffer())
        self.assertNotIn("ERROR", logger.getLogBuffer())

    def testDeletingPPAThatCannotHaveMetaData(self):
        # Due to conflicts in the directory structure only Ubuntu PPAs
        # have a metadata directory. PPAs with the same name for
        # different distros can coexist, and only deleting the Ubuntu
        # one will remove the metadata.
        ubuntu_team = getUtility(IPersonSet).getByName("ubuntu-team")
        ubuntu_ppa = getUtility(IArchiveSet).new(
            distribution=self.ubuntu,
            owner=ubuntu_team,
            purpose=ArchivePurpose.PPA,
            name="ppa",
        )
        test_ppa = getUtility(IArchiveSet).new(
            distribution=self.ubuntutest,
            owner=ubuntu_team,
            purpose=ArchivePurpose.PPA,
            name="ppa",
        )
        logger = BufferLogger()
        ubuntu_publisher = getPublisher(ubuntu_ppa, None, logger)
        ubuntu_publisher.setupArchiveDirs()
        test_publisher = getPublisher(test_ppa, None, logger)
        test_publisher.setupArchiveDirs()

        self.assertTrue(os.path.exists(ubuntu_publisher._config.archiveroot))
        self.assertTrue(os.path.exists(test_publisher._config.archiveroot))

        open(
            os.path.join(ubuntu_publisher._config.archiveroot, "test_file"),
            "w",
        ).close()
        open(
            os.path.join(test_publisher._config.archiveroot, "test_file"), "w"
        ).close()

        # Add a meta file for the Ubuntu PPA
        os.makedirs(ubuntu_publisher._config.metaroot)
        open(
            os.path.join(ubuntu_publisher._config.metaroot, "test"), "w"
        ).close()
        self.assertIs(None, test_publisher._config.metaroot)

        test_publisher.deleteArchive()
        self.assertFalse(os.path.exists(test_publisher._config.archiveroot))
        self.assertTrue(os.path.exists(ubuntu_publisher._config.metaroot))
        # XXX wgrant 2014-07-07 bug=1338439: deleteArchive() currently
        # kills all PPAs with the same name and owner.
        # self.assertTrue(os.path.exists(ubuntu_publisher._config.archiveroot))

        ubuntu_publisher.deleteArchive()
        self.assertFalse(os.path.exists(ubuntu_publisher._config.metaroot))
        self.assertFalse(os.path.exists(ubuntu_publisher._config.archiveroot))

        self.assertNotIn("WARNING", logger.getLogBuffer())
        self.assertNotIn("ERROR", logger.getLogBuffer())

    def testDeletingPPARename(self):
        a1 = self.factory.makeArchive(purpose=ArchivePurpose.PPA, name="test")
        getPublisher(a1, None, self.logger).deleteArchive()
        self.assertEqual("test-deletedppa", a1.name)
        a2 = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, name="test", owner=a1.owner
        )
        getPublisher(a2, None, self.logger).deleteArchive()
        self.assertEqual("test-deletedppa1", a2.name)

    def testPublishPartner(self):
        """Test that a partner package is published to the right place."""
        archive = self.ubuntutest.getArchiveByComponent("partner")
        pub_config = getPubConfig(archive)
        pub_config.setupArchiveDirs()
        disk_pool = DiskPool(
            archive, pub_config.poolroot, pub_config.temproot, self.logger
        )
        publisher = Publisher(self.logger, pub_config, disk_pool, archive)
        self.getPubSource(archive=archive, filecontent=b"I am partner")

        publisher.A_publish(False)

        # Did the file get published in the right place?
        self.assertEqual(
            "/var/tmp/archive/ubuntutest-partner/pool", pub_config.poolroot
        )
        foo_path = "%s/main/f/foo/foo_666.dsc" % pub_config.poolroot
        with open(foo_path) as foo_file:
            self.assertEqual("I am partner", foo_file.read().strip())

        # Check that the index is in the right place.
        publisher.C_writeIndexes(False)
        self.assertEqual(
            "/var/tmp/archive/ubuntutest-partner/dists", pub_config.distsroot
        )
        index_path = os.path.join(
            pub_config.distsroot,
            "breezy-autotest",
            "partner",
            "source",
            "Sources.gz",
        )
        with open(index_path) as index_file:
            self.assertTrue(index_file)

        # Check the release file is in the right place.
        publisher.D_writeReleaseFiles(False)
        release_path = os.path.join(
            pub_config.distsroot, "breezy-autotest", "Release"
        )
        with open(release_path) as release_file:
            self.assertTrue(release_file)

    def testPartnerReleasePocketPublishing(self):
        """Test partner package RELEASE pocket publishing.

        Publishing partner packages to the RELEASE pocket in a stable
        distroseries is always allowed, so check for that here.
        """
        archive = self.ubuntutest.getArchiveByComponent("partner")
        self.ubuntutest["breezy-autotest"].status = SeriesStatus.CURRENT
        pub_config = getPubConfig(archive)
        pub_config.setupArchiveDirs()
        disk_pool = DiskPool(
            archive, pub_config.poolroot, pub_config.temproot, self.logger
        )
        publisher = Publisher(self.logger, pub_config, disk_pool, archive)
        self.getPubSource(
            archive=archive,
            filecontent=b"I am partner",
            status=PackagePublishingStatus.PENDING,
        )

        publisher.A_publish(force_publishing=False)

        # The pocket was dirtied:
        self.assertEqual({"breezy-autotest"}, publisher.dirty_suites)
        # The file was published:
        foo_path = "%s/main/f/foo/foo_666.dsc" % pub_config.poolroot
        with open(foo_path) as foo_file:
            self.assertEqual("I am partner", foo_file.read().strip())

        # Nothing to test from these two calls other than that they don't blow
        # up as there is an assertion in the code to make sure it's not
        # publishing out of a release pocket in a stable distroseries,
        # excepting PPA and partner which are allowed to do that.
        publisher.C_writeIndexes(is_careful=False)
        publisher.D_writeReleaseFiles(is_careful=False)

    def testPublishingSpecificDistroSeries(self):
        """Test the publishing procedure with the suite argument.

        To publish a specific distroseries.
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
            allowed_suites=["hoary-test"],
        )

        pub_source = self.getPubSource(filecontent=b"foo")
        pub_source2 = self.getPubSource(
            sourcename="baz",
            filecontent=b"baz",
            distroseries=self.ubuntutest["hoary-test"],
        )

        publisher.A_publish(force_publishing=False)
        self.layer.txn.commit()

        IStore(pub_source).flush()
        IStore(pub_source2).flush()
        self.assertEqual({"hoary-test"}, publisher.dirty_suites)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source2.status)
        self.assertEqual(PackagePublishingStatus.PENDING, pub_source.status)

    def testPublishingSpecificPocket(self):
        """Test the publishing procedure with the suite argument.

        To publish a specific pocket.
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
            allowed_suites=["breezy-autotest-updates"],
        )

        self.ubuntutest["breezy-autotest"].status = SeriesStatus.CURRENT

        pub_source = self.getPubSource(
            filecontent=b"foo", pocket=PackagePublishingPocket.UPDATES
        )

        pub_source2 = self.getPubSource(
            sourcename="baz",
            filecontent=b"baz",
            pocket=PackagePublishingPocket.BACKPORTS,
        )

        publisher.A_publish(force_publishing=False)
        self.layer.txn.commit()

        IStore(pub_source).flush()
        IStore(pub_source2).flush()
        self.assertEqual({"breezy-autotest-updates"}, publisher.dirty_suites)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)
        self.assertEqual(PackagePublishingStatus.PENDING, pub_source2.status)

    def testNonCarefulPublishing(self):
        """Test the non-careful publishing procedure.

        With one PUBLISHED record, no pockets *dirtied*.
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        self.getPubSource(status=PackagePublishingStatus.PUBLISHED)

        # a new non-careful publisher won't find anything to publish, thus
        # no pockets will be *dirtied*.
        publisher.A_publish(False)

        self.assertEqual(set(), publisher.dirty_suites)
        # nothing got published
        foo_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        self.assertEqual(False, os.path.exists(foo_path))

    def testCarefulPublishing(self):
        """Test the careful publishing procedure.

        With one PUBLISHED record, pocket gets *dirtied*.
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        self.getPubSource(
            filecontent=b"Hello world",
            status=PackagePublishingStatus.PUBLISHED,
        )

        # Make everything other than breezy-autotest OBSOLETE so that they
        # aren't republished.
        for series in self.ubuntutest.series:
            if series.name != "breezy-autotest":
                series.status = SeriesStatus.OBSOLETE

        # A careful publisher run will re-publish the PUBLISHED records,
        # then we will have a corresponding dirty_pocket entry.
        publisher.A_publish(True)

        self.assertEqual({"breezy-autotest"}, publisher.dirty_suites)
        # file got published
        foo_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        with open(foo_path) as foo_file:
            self.assertEqual("Hello world", foo_file.read().strip())

    def testPublishingOnlyConsidersOneArchive(self):
        """Publisher procedure should only consider the target archive.

        Ignore pending publishing records targeted to another archive.
        Nothing gets published, no pockets get *dirty*
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        ubuntu_team = getUtility(IPersonSet).getByName("ubuntu-team")
        test_archive = getUtility(IArchiveSet).new(
            owner=ubuntu_team, purpose=ArchivePurpose.PPA
        )

        pub_source = self.getPubSource(
            sourcename="foo",
            filename="foo_1.dsc",
            filecontent=b"Hello world",
            status=PackagePublishingStatus.PENDING,
            archive=test_archive,
        )

        publisher.A_publish(False)
        self.layer.txn.commit()

        self.assertEqual(set(), publisher.dirty_suites)
        self.assertEqual(PackagePublishingStatus.PENDING, pub_source.status)

        # nothing got published
        foo_path = "%s/main/f/foo/foo_1.dsc" % self.pool_dir
        self.assertEqual(False, os.path.exists(foo_path))

    def testPublishingWorksForOtherArchives(self):
        """Publisher also works as expected for another archives."""
        ubuntu_team = getUtility(IPersonSet).getByName("ubuntu-team")
        test_archive = getUtility(IArchiveSet).new(
            distribution=self.ubuntutest,
            owner=ubuntu_team,
            purpose=ArchivePurpose.PPA,
        )

        test_pool_dir = tempfile.mkdtemp()
        test_temp_dir = tempfile.mkdtemp()
        test_disk_pool = DiskPool(
            test_archive, test_pool_dir, test_temp_dir, self.logger
        )

        publisher = Publisher(
            self.logger, self.config, test_disk_pool, test_archive
        )

        pub_source = self.getPubSource(
            sourcename="foo",
            filename="foo_1.dsc",
            filecontent=b"I am supposed to be a embargoed archive",
            status=PackagePublishingStatus.PENDING,
            archive=test_archive,
        )

        publisher.A_publish(False)
        self.layer.txn.commit()

        IStore(pub_source).flush()
        self.assertEqual({"breezy-autotest"}, publisher.dirty_suites)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)

        # nothing got published
        foo_path = "%s/main/f/foo/foo_1.dsc" % test_pool_dir
        with open(foo_path) as foo_file:
            self.assertEqual(
                "I am supposed to be a embargoed archive",
                foo_file.read().strip(),
            )

        # remove locally created dir
        shutil.rmtree(test_pool_dir)

    def testPublishingSkipsObsoleteFuturePrimarySeries(self):
        """Publisher skips OBSOLETE/FUTURE series in PRIMARY archives."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        # Remove security proxy so that the publisher can call our fake
        # method.
        publisher.distro = removeSecurityProxy(publisher.distro)
        pub_source = self.getPubSource(distroseries=self.breezy_autotest)
        self.getPubBinaries(
            distroseries=self.breezy_autotest, pub_source=pub_source
        )

        for status in (SeriesStatus.OBSOLETE, SeriesStatus.FUTURE):
            naked_breezy_autotest = publisher.distro["breezy-autotest"]
            naked_breezy_autotest.status = status
            publisher.publishSources = FakeMethod(result=set())
            publisher.publishBinaries = FakeMethod(result=set())

            publisher.A_publish(False)

            self.assertEqual(0, publisher.publishSources.call_count)
            self.assertEqual(0, publisher.publishBinaries.call_count)

    def testPublishingConsidersObsoleteFuturePPASeries(self):
        """Publisher does not skip OBSOLETE/FUTURE series in PPA archives."""
        ubuntu_team = getUtility(IPersonSet).getByName("ubuntu-team")
        test_archive = getUtility(IArchiveSet).new(
            distribution=self.ubuntutest,
            owner=ubuntu_team,
            purpose=ArchivePurpose.PPA,
        )
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, test_archive
        )
        # Remove security proxy so that the publisher can call our fake
        # method.
        publisher.distro = removeSecurityProxy(publisher.distro)
        pub_source = self.getPubSource(
            distroseries=self.breezy_autotest, archive=test_archive
        )
        self.getPubBinaries(
            distroseries=self.breezy_autotest,
            archive=test_archive,
            pub_source=pub_source,
        )

        for status in (SeriesStatus.OBSOLETE, SeriesStatus.FUTURE):
            naked_breezy_autotest = publisher.distro["breezy-autotest"]
            naked_breezy_autotest.status = status
            publisher.publishSources = FakeMethod(result=set())
            publisher.publishBinaries = FakeMethod(result=set())

            publisher.A_publish(False)

            source_args = [
                args[:2] for args in publisher.publishSources.extract_args()
            ]
            self.assertIn((naked_breezy_autotest, RELEASE), source_args)
            binary_args = [
                args[:2] for args in publisher.publishBinaries.extract_args()
            ]
            self.assertIn(
                (naked_breezy_autotest.architectures[0], RELEASE), binary_args
            )

    def testPublisherBuilderFunctions(self):
        """Publisher can be initialized via provided helper function.

        In order to simplify the top-level publication scripts, one for
        'main_archive' publication and other for 'PPA', we have a specific
        helper function: 'getPublisher'
        """
        # Stub parameters.
        allowed_suites = ["breezy-autotest"]

        distro_publisher = getPublisher(
            self.ubuntutest.main_archive, allowed_suites, self.logger
        )

        # check the publisher context, pointing to the 'main_archive'
        self.assertEqual(
            self.ubuntutest.main_archive, distro_publisher.archive
        )
        self.assertEqual(
            "/var/tmp/archive/ubuntutest/dists",
            distro_publisher._config.distsroot,
        )
        self.assertEqual({"breezy-autotest"}, distro_publisher.allowed_suites)

        # Check that the partner archive is built in a different directory
        # to the primary archive.
        partner_archive = getUtility(IArchiveSet).getByDistroPurpose(
            self.ubuntutest, ArchivePurpose.PARTNER
        )
        distro_publisher = getPublisher(
            partner_archive, allowed_suites, self.logger
        )
        self.assertEqual(partner_archive, distro_publisher.archive)
        self.assertEqual(
            "/var/tmp/archive/ubuntutest-partner/dists",
            distro_publisher._config.distsroot,
        )
        self.assertEqual(
            "/var/tmp/archive/ubuntutest-partner/pool",
            distro_publisher._config.poolroot,
        )

        # lets setup an Archive Publisher
        cprov = getUtility(IPersonSet).getByName("cprov")
        archive_publisher = getPublisher(
            cprov.archive, allowed_suites, self.logger
        )

        # check the publisher context, pointing to the given PPA archive
        self.assertEqual(cprov.archive, archive_publisher.archive)
        self.assertEqual(
            "/var/tmp/ppa.test/cprov/ppa/ubuntutest/dists",
            archive_publisher._config.distsroot,
        )
        self.assertEqual({"breezy-autotest"}, archive_publisher.allowed_suites)

    def testPendingArchive(self):
        """Check Pending Archive Lookup.

        IArchiveSet.getPendingPPAs should only return the archives with
        publications in PENDING state.
        """
        archive_set = getUtility(IArchiveSet)
        person_set = getUtility(IPersonSet)
        ubuntu = getUtility(IDistributionSet)["ubuntu"]

        spiv = person_set.getByName("spiv")
        archive_set.new(
            owner=spiv, distribution=ubuntu, purpose=ArchivePurpose.PPA
        )
        name16 = person_set.getByName("name16")
        archive_set.new(
            owner=name16, distribution=ubuntu, purpose=ArchivePurpose.PPA
        )

        self.getPubSource(
            sourcename="foo",
            filename="foo_1.dsc",
            filecontent=b"Hello world",
            status=PackagePublishingStatus.PENDING,
            archive=spiv.archive,
        )

        self.getPubSource(
            sourcename="foo",
            filename="foo_1.dsc",
            filecontent=b"Hello world",
            status=PackagePublishingStatus.PUBLISHED,
            archive=name16.archive,
        )

        self.assertEqual(4, ubuntu.getAllPPAs().count())

        pending_archives = ubuntu.getPendingPublicationPPAs()
        self.assertEqual(1, pending_archives.count())
        pending_archive = pending_archives[0]
        self.assertEqual(spiv.archive.id, pending_archive.id)

    def testDeletingArchive(self):
        # IArchiveSet.getPendingPPAs should include PPAs that have a
        # status of DELETING.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]

        ppa = self.factory.makeArchive(purpose=ArchivePurpose.PPA)
        copy_archive = self.factory.makeArchive(
            distribution=self.ubuntu, purpose=ArchivePurpose.COPY
        )
        self.assertNotIn(ppa, ubuntu.getPendingPublicationPPAs())
        self.assertNotIn(copy_archive, ubuntu.getPendingPublicationPPAs())
        ppa.status = ArchiveStatus.DELETING
        copy_archive.status = ArchiveStatus.DELETING
        self.assertIn(ppa, ubuntu.getPendingPublicationPPAs())
        self.assertNotIn(copy_archive, ubuntu.getPendingPublicationPPAs())

    def testPendingArchiveWithReapableFiles(self):
        # getPendingPublicationPPAs returns archives that have reapable
        # ArchiveFiles.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]
        archive = self.factory.makeArchive()
        self.assertNotIn(archive, ubuntu.getPendingPublicationPPAs())
        archive_file = self.factory.makeArchiveFile(archive=archive)
        self.assertNotIn(archive, ubuntu.getPendingPublicationPPAs())
        now = datetime.now(timezone.utc)
        removeSecurityProxy(archive_file).scheduled_deletion_date = (
            now + timedelta(hours=12)
        )
        self.assertNotIn(archive, ubuntu.getPendingPublicationPPAs())
        removeSecurityProxy(archive_file).scheduled_deletion_date = (
            now - timedelta(hours=12)
        )
        self.assertIn(archive, ubuntu.getPendingPublicationPPAs())
        getUtility(IArchiveFileSet).markDeleted([archive_file])
        self.assertNotIn(archive, ubuntu.getPendingPublicationPPAs())

    def testDirtySuitesArchive(self):
        # getPendingPublicationPPAs returns archives that have dirty_suites
        # set.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]
        archive = self.factory.makeArchive()
        self.assertNotIn(archive, ubuntu.getPendingPublicationPPAs())
        archive.markSuiteDirty(
            ubuntu.currentseries, PackagePublishingPocket.RELEASE
        )
        self.assertIn(archive, ubuntu.getPendingPublicationPPAs())

    def _checkCompressedFiles(
        self, archive_publisher, base_file_path, suffixes
    ):
        """Assert that the various compressed versions of a file are equal.

        Check that the various versions of a compressed file, such as
        Packages.{gz,bz2,xz} and Sources.{gz,bz2,xz} all have identical
        contents.  The file paths are relative to breezy-autotest/main under
        the archive_publisher's configured dist root.  'breezy-autotest' is
        our test distroseries name.

        The contents of the uncompressed file is returned as a list of lines
        in the file.
        """
        index_base_path = os.path.join(
            archive_publisher._config.distsroot,
            "breezy-autotest",
            "main",
            base_file_path,
        )

        all_contents = []
        for suffix in suffixes:
            if suffix == ".gz":
                open_func = gzip.open
            elif suffix == ".bz2":
                open_func = bz2.BZ2File
            elif suffix == ".xz":
                open_func = lzma.LZMAFile
            else:
                open_func = lambda path: open(path, "rb")
            with open_func(index_base_path + suffix) as index_file:
                all_contents.append(index_file.read().splitlines())

        for contents in all_contents[1:]:
            self.assertEqual(all_contents[0], contents)

        return all_contents[0]

    def setupPPAArchiveIndexTest(
        self,
        long_descriptions=True,
        index_compressors=None,
    ):
        # Setup for testPPAArchiveIndex tests
        allowed_suites = []

        cprov = getUtility(IPersonSet).getByName("cprov")
        cprov.archive.publish_debug_symbols = True

        archive_publisher = getPublisher(
            cprov.archive, allowed_suites, self.logger
        )

        # Pending source and binary publications.
        # The binary description explores index formatting properties.
        pub_source = self.getPubSource(
            sourcename="foo",
            filename="foo_1.dsc",
            filecontent=b"Hello world",
            status=PackagePublishingStatus.PENDING,
            archive=cprov.archive,
        )
        self.getPubBinaries(
            pub_source=pub_source,
            description="   My leading spaces are normalised to a single "
            "space but not trailing.  \n    It does nothing, "
            "though",
            with_debug=True,
        )

        # Ignored (deleted) source publication that will not be listed in
        # the index and a pending 'udeb' binary package.
        ignored_source = self.getPubSource(
            status=PackagePublishingStatus.DELETED, archive=cprov.archive
        )
        self.getPubBinaries(
            pub_source=ignored_source,
            binaryname="bingo",
            description="nice udeb",
            format=BinaryPackageFormat.UDEB,
        )[0]

        ds = self.ubuntutest.getSeries("breezy-autotest")
        if not long_descriptions:
            # Make sure that NMAF generates i18n/Translation-en* files.
            ds.include_long_descriptions = False
        if index_compressors is not None:
            ds.index_compressors = index_compressors

        archive_publisher.A_publish(False)
        self.layer.txn.commit()
        archive_publisher.C_writeIndexes(False)
        archive_publisher.D_writeReleaseFiles(False)
        return archive_publisher

    def testPPAArchiveIndex(self):
        """Building Archive Indexes from PPA publications."""
        archive_publisher = self.setupPPAArchiveIndexTest()

        # Various compressed Sources files are written; ensure that they are
        # the same after decompression.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("source", "Sources"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: foo",
            b"Binary: foo-bin",
            b"Version: 666",
            b"Section: base",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Standards-Version: 3.6.2",
            b"Format: 1.0",
            b"Directory: pool/main/f/foo",
            b"Files:",
            b" 3e25960a79dbc69b674cd4ec67a72c62 11 foo_1.dsc",
            b"Checksums-Sha1:",
            b" 7b502c3a1f48c8609ae212cdfb639dee39673f5e 11 foo_1.dsc",
            b"Checksums-Sha256:",
            b" 64ec88ca00b268e5ba1a35678a1b5316d212f4f366b2477232534a8aeca37f"
            b"3c 11 foo_1.dsc",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # Various compressed Packages files are written; ensure that they
        # are the same after decompression.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("binary-i386", "Packages"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: foo-bin",
            b"Source: foo",
            b"Priority: standard",
            b"Section: base",
            b"Installed-Size: 100",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Version: 666",
            b"Filename: pool/main/f/foo/foo-bin_666_all.deb",
            b"Size: 18",
            b"MD5sum: 008409e7feb1c24a6ccab9f6a62d24c5",
            b"SHA1: 30b7b4e583fa380772c5a40e428434628faef8cf",
            b"SHA256: 006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a"
            b"00bb3e945",
            b"Description: Foo app is great",
            b" My leading spaces are normalised to a single space but not "
            b"trailing.  ",
            b" It does nothing, though",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # Various compressed Packages files are written for the
        # 'debian-installer' section for each architecture.  They will list
        # the 'udeb' files.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("debian-installer", "binary-i386", "Packages"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: bingo",
            b"Source: foo",
            b"Priority: standard",
            b"Section: base",
            b"Installed-Size: 100",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Version: 666",
            b"Filename: pool/main/f/foo/bingo_666_all.udeb",
            b"Size: 18",
            b"MD5sum: 008409e7feb1c24a6ccab9f6a62d24c5",
            b"SHA1: 30b7b4e583fa380772c5a40e428434628faef8cf",
            b"SHA256: 006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a"
            b"00bb3e945",
            b"Description: Foo app is great",
            b" nice udeb",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # 'debug' too, when publish_debug_symbols is enabled.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("debug", "binary-i386", "Packages"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: foo-bin-dbgsym",
            b"Source: foo",
            b"Priority: standard",
            b"Section: base",
            b"Installed-Size: 100",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Version: 666",
            b"Filename: pool/main/f/foo/foo-bin-dbgsym_666_all.ddeb",
            b"Size: 18",
            b"MD5sum: 008409e7feb1c24a6ccab9f6a62d24c5",
            b"SHA1: 30b7b4e583fa380772c5a40e428434628faef8cf",
            b"SHA256: 006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a"
            b"00bb3e945",
            b"Description: Foo app is great",
            b" My leading spaces are normalised to a single space but not "
            b"trailing.  ",
            b" It does nothing, though",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # We always regenerate all Releases file for a given suite.
        self.assertIn(
            "breezy-autotest", archive_publisher.release_files_needed
        )

        # Confirm that i18n files are not created
        i18n_path = os.path.join(
            archive_publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "i18n",
        )
        self.assertFalse(
            os.path.exists(os.path.join(i18n_path, "Translation-en"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(i18n_path, "Translation-en.gz"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(i18n_path, "Translation-en.bz2"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(i18n_path, "Translation-en.xz"))
        )

        # remove PPA root
        shutil.rmtree(config.personalpackagearchive.root)

    def testPPAArchiveIndexLongDescriptionsFalse(self):
        # Building Archive Indexes from PPA publications with
        # include_long_descriptions = False.
        archive_publisher = self.setupPPAArchiveIndexTest(
            long_descriptions=False
        )

        # Various compressed Sources files are written; ensure that they are
        # the same after decompression.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("source", "Sources"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: foo",
            b"Binary: foo-bin",
            b"Version: 666",
            b"Section: base",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Standards-Version: 3.6.2",
            b"Format: 1.0",
            b"Directory: pool/main/f/foo",
            b"Files:",
            b" 3e25960a79dbc69b674cd4ec67a72c62 11 foo_1.dsc",
            b"Checksums-Sha1:",
            b" 7b502c3a1f48c8609ae212cdfb639dee39673f5e 11 foo_1.dsc",
            b"Checksums-Sha256:",
            b" 64ec88ca00b268e5ba1a35678a1b5316d212f4f366b2477232534a8aeca37f"
            b"3c 11 foo_1.dsc",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # Various compressed Packages files are written; ensure that they
        # are the same after decompression.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("binary-i386", "Packages"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: foo-bin",
            b"Source: foo",
            b"Priority: standard",
            b"Section: base",
            b"Installed-Size: 100",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Version: 666",
            b"Filename: pool/main/f/foo/foo-bin_666_all.deb",
            b"Size: 18",
            b"MD5sum: 008409e7feb1c24a6ccab9f6a62d24c5",
            b"SHA1: 30b7b4e583fa380772c5a40e428434628faef8cf",
            b"SHA256: 006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a"
            b"00bb3e945",
            b"Description: Foo app is great",
            b"Description-md5: 42d89d502e81dad6d3d4a2f85fdc6c6e",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # Various compressed Packages files are written for the
        # 'debian-installer' section for each architecture.  They will list
        # the 'udeb' files.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("debian-installer", "binary-i386", "Packages"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: bingo",
            b"Source: foo",
            b"Priority: standard",
            b"Section: base",
            b"Installed-Size: 100",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Version: 666",
            b"Filename: pool/main/f/foo/bingo_666_all.udeb",
            b"Size: 18",
            b"MD5sum: 008409e7feb1c24a6ccab9f6a62d24c5",
            b"SHA1: 30b7b4e583fa380772c5a40e428434628faef8cf",
            b"SHA256: 006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a"
            b"00bb3e945",
            b"Description: Foo app is great",
            b"Description-md5: 6fecedf187298acb6bc5f15cc5807fb7",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # 'debug' too, when publish_debug_symbols is enabled.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("debug", "binary-i386", "Packages"),
            [".gz", ".bz2"],
        )
        expected_index_contents = [
            b"Package: foo-bin-dbgsym",
            b"Source: foo",
            b"Priority: standard",
            b"Section: base",
            b"Installed-Size: 100",
            b"Maintainer: Foo Bar <foo@bar.com>",
            b"Architecture: all",
            b"Version: 666",
            b"Filename: pool/main/f/foo/foo-bin-dbgsym_666_all.ddeb",
            b"Size: 18",
            b"MD5sum: 008409e7feb1c24a6ccab9f6a62d24c5",
            b"SHA1: 30b7b4e583fa380772c5a40e428434628faef8cf",
            b"SHA256: 006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a"
            b"00bb3e945",
            b"Description: Foo app is great",
            b"Description-md5: 42d89d502e81dad6d3d4a2f85fdc6c6e",
            b"",
        ]
        self.assertEqual(expected_index_contents, index_contents)

        # We always regenerate all Releases file for a given suite.
        self.assertIn(
            "breezy-autotest", archive_publisher.release_files_needed
        )

        # Various compressed Translation-en files are written; ensure that
        # they are the same after decompression.
        index_contents = self._checkCompressedFiles(
            archive_publisher,
            os.path.join("i18n", "Translation-en"),
            [".gz", ".bz2"],
        )
        self.assertEqual(
            [
                b"Package: bingo",
                b"Description-md5: 6fecedf187298acb6bc5f15cc5807fb7",
                b"Description-en: Foo app is great",
                b" nice udeb",
                b"",
                b"Package: foo-bin",
                b"Description-md5: 42d89d502e81dad6d3d4a2f85fdc6c6e",
                b"Description-en: Foo app is great",
                b" My leading spaces are normalised to a single space but not "
                b"trailing.  ",
                b" It does nothing, though",
                b"",
                b"Package: foo-bin-dbgsym",
                b"Description-md5: 42d89d502e81dad6d3d4a2f85fdc6c6e",
                b"Description-en: Foo app is great",
                b" My leading spaces are normalised to a single space but not "
                b"trailing.  ",
                b" It does nothing, though",
                b"",
            ],
            index_contents,
        )

        series = os.path.join(
            archive_publisher._config.distsroot, "breezy-autotest"
        )
        i18n_index = os.path.join(series, "main", "i18n", "Index")

        # The i18n/Index file has been generated.
        self.assertTrue(os.path.exists(i18n_index))

        # It is listed correctly in Release.
        release_path = os.path.join(series, "Release")
        release = self.parseRelease(release_path)
        with open(i18n_index, "rb") as i18n_index_file:
            self.assertReleaseContentsMatch(
                release, "main/i18n/Index", i18n_index_file.read()
            )

        release_path = os.path.join(series, "Release")
        with open(release_path) as release_file:
            content = release_file.read()
            self.assertIn("main/i18n/Translation-en.bz2", content)
            self.assertIn("main/i18n/Translation-en.gz", content)

        # remove PPA root
        shutil.rmtree(config.personalpackagearchive.root)

    def testPPAArchiveIndexCompressors(self):
        # Archive index generation honours DistroSeries.index_compressors.
        archive_publisher = self.setupPPAArchiveIndexTest(
            long_descriptions=False,
            index_compressors=[
                IndexCompressionType.UNCOMPRESSED,
                IndexCompressionType.XZ,
            ],
        )
        suite_path = os.path.join(
            archive_publisher._config.distsroot, "breezy-autotest", "main"
        )
        for uncompressed_file_path in (
            os.path.join("source", "Sources"),
            os.path.join("binary-i386", "Packages"),
            os.path.join("debian-installer", "binary-i386", "Packages"),
            os.path.join("debug", "binary-i386", "Packages"),
            os.path.join("i18n", "Translation-en"),
        ):
            for suffix in ("bz2", "gz"):
                self.assertFalse(
                    os.path.exists(
                        os.path.join(
                            suite_path,
                            "%s.%s" % (uncompressed_file_path, suffix),
                        )
                    )
                )
            self._checkCompressedFiles(
                archive_publisher, uncompressed_file_path, [".xz"]
            )

    def testDirtyingPocketsWithDeletedPackages(self):
        """Test that dirtying pockets with deleted packages works.

        The publisher run should make dirty pockets where there are
        outstanding deletions, so that the domination process will
        work on the deleted publications.
        """
        allowed_suites = []
        publisher = getPublisher(
            self.ubuntutest.main_archive, allowed_suites, self.logger
        )

        publisher.A2_markPocketsWithDeletionsDirty()
        self.assertEqual(set(), publisher.dirty_suites)

        # Make a published source, a deleted source in the release
        # pocket, a source that's been removed from disk and one that's
        # waiting to be deleted, each in different pockets.  The deleted
        # source in the release pocket should not be processed.  We'll
        # also have a binary waiting to be deleted.
        self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )

        self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )

        self.getPubSource(
            scheduleddeletiondate=UTC_NOW,
            dateremoved=UTC_NOW,
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.DELETED,
        )

        self.getPubSource(
            pocket=PackagePublishingPocket.SECURITY,
            status=PackagePublishingStatus.DELETED,
        )

        self.getPubBinaries(
            pocket=PackagePublishingPocket.BACKPORTS,
            status=PackagePublishingStatus.DELETED,
        )

        # Run the deletion detection.
        publisher.A2_markPocketsWithDeletionsDirty()

        # Only the suites with pending deletions are marked as dirty.
        expected_dirty_suites = {
            "breezy-autotest",
            "breezy-autotest-security",
            "breezy-autotest-backports",
        }
        self.assertEqual(expected_dirty_suites, publisher.dirty_suites)

        # If the distroseries is CURRENT, then the release pocket is not
        # marked as dirty.
        self.ubuntutest["breezy-autotest"].status = SeriesStatus.CURRENT

        publisher.dirty_suites = set()
        publisher.A2_markPocketsWithDeletionsDirty()

        expected_dirty_suites = {
            "breezy-autotest-security",
            "breezy-autotest-backports",
        }
        self.assertEqual(expected_dirty_suites, publisher.dirty_suites)

    def testDeletionDetectionRespectsAllowedSuites(self):
        """Check if the deletion detection mechanism respects allowed_suites.

        The deletion detection should not request publications of pockets
        that were not specified on the command-line ('allowed_suites').

        This issue is reported as bug #241452, when running the publisher
        only for a specific suite, in most of cases an urgent security
        release, only pockets with pending deletion that match the
        specified suites should be marked as dirty.
        """
        allowed_suites = [
            "breezy-autotest-security",
            "breezy-autotest-updates",
        ]
        publisher = getPublisher(
            self.ubuntutest.main_archive, allowed_suites, self.logger
        )

        publisher.A2_markPocketsWithDeletionsDirty()
        self.assertEqual(set(), publisher.dirty_suites)

        # Create pending deletions in RELEASE, BACKPORTS, SECURITY and
        # UPDATES pockets.
        self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )

        self.getPubBinaries(
            pocket=PackagePublishingPocket.BACKPORTS,
            status=PackagePublishingStatus.DELETED,
        )[0]

        self.getPubSource(
            pocket=PackagePublishingPocket.SECURITY,
            status=PackagePublishingStatus.DELETED,
        )

        self.getPubBinaries(
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.DELETED,
        )[0]

        publisher.A2_markPocketsWithDeletionsDirty()
        # Only the suites with pending deletions are marked as dirty.
        self.assertEqual(set(allowed_suites), publisher.dirty_suites)

    def testReleaseFile(self):
        """Test release file writing.

        The release file should contain the MD5, SHA1 and SHA256 for each
        index created for a given distroseries.
        """
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        self.getPubSource(filecontent=b"Hello world")

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)

        self.assertIn("breezy-autotest", publisher.release_files_needed)

        publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(
            os.path.join(self.config.distsroot, "breezy-autotest", "Release")
        )

        # Primary archive distroseries Release 'Origin' contains
        # the distribution displayname.
        self.assertEqual("ubuntutest", release["origin"])

        # The Label: field should be set to the archive displayname
        self.assertEqual("ubuntutest", release["label"])

        arch_release_path = os.path.join(
            self.config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        with open(arch_release_path, "rb") as arch_release_file:
            self.assertReleaseContentsMatch(
                release, "main/source/Release", arch_release_file.read()
            )

        # Primary archive architecture Release files 'Origin' contain the
        # distribution displayname.
        arch_release = self.parseRelease(arch_release_path)
        self.assertEqual("ubuntutest", arch_release["origin"])

    def testReleaseFileForPPA(self):
        """Test release file writing for PPA

        The release file should contain the MD5, SHA1 and SHA256 for each
        index created for a given distroseries.

        Note that the individuals indexes have exactly the same content
        as the ones generated by apt-ftparchive (see previous test), however
        the position in the list is different (earlier) because we do not
        generate/list debian-installer (d-i) indexes in NoMoreAptFtpArchive
        approach.

        Another difference between the primary repositories and PPAs is that
        PPA Release files for the distroseries and its architectures have a
        distinct 'Origin:' value.  The origin is specific to each PPA, using
        the pattern 'LP-PPA-%(owner_name)s'.  This allows proper pinning of
        the PPA packages.
        """
        allowed_suites = []
        cprov = getUtility(IPersonSet).getByName("cprov")
        cprov.archive.displayname = "PPA for Celso Provid\xe8lo"
        archive_publisher = getPublisher(
            cprov.archive, allowed_suites, self.logger
        )

        self.getPubSource(filecontent=b"Hello world", archive=cprov.archive)

        archive_publisher.A_publish(False)
        self.layer.txn.commit()
        archive_publisher.C_writeIndexes(False)
        archive_publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(
            os.path.join(
                archive_publisher._config.distsroot,
                "breezy-autotest",
                "Release",
            )
        )
        self.assertEqual("LP-PPA-cprov", release["origin"])

        # The Label: field should be set to the archive displayname
        self.assertEqual("PPA for Celso Provid\xe8lo", release["label"])

        arch_sources_path = os.path.join(
            archive_publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Sources.gz",
        )
        with gzip.open(arch_sources_path, "rb") as arch_sources_file:
            self.assertReleaseContentsMatch(
                release, "main/source/Sources", arch_sources_file.read()
            )

        arch_release_path = os.path.join(
            archive_publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        with open(arch_release_path, "rb") as arch_release_file:
            self.assertReleaseContentsMatch(
                release, "main/source/Release", arch_release_file.read()
            )

        # Architecture Release files also have a distinct Origin: for PPAs.
        arch_release = self.parseRelease(arch_release_path)
        self.assertEqual("LP-PPA-cprov", arch_release["origin"])

    def testReleaseFileForNamedPPA(self):
        # Named PPA have a distint Origin: field, so packages from it can
        # be pinned if necessary.

        # Create a named-ppa for Celso.
        cprov = getUtility(IPersonSet).getByName("cprov")
        named_ppa = getUtility(IArchiveSet).new(
            owner=cprov,
            name="testing",
            distribution=self.ubuntutest,
            purpose=ArchivePurpose.PPA,
        )

        # Setup the publisher for it and publish its repository.
        allowed_suites = []
        archive_publisher = getPublisher(
            named_ppa, allowed_suites, self.logger
        )
        self.getPubSource(filecontent=b"Hello world", archive=named_ppa)

        archive_publisher.A_publish(False)
        self.layer.txn.commit()
        archive_publisher.C_writeIndexes(False)
        archive_publisher.D_writeReleaseFiles(False)

        # Check the distinct Origin: field content in the main Release file
        # and the component specific one.
        release = self.parseRelease(
            os.path.join(
                archive_publisher._config.distsroot,
                "breezy-autotest",
                "Release",
            )
        )
        self.assertEqual("LP-PPA-cprov-testing", release["origin"])

        arch_release = self.parseRelease(
            os.path.join(
                archive_publisher._config.distsroot,
                "breezy-autotest",
                "main/source/Release",
            )
        )
        self.assertEqual("LP-PPA-cprov-testing", arch_release["origin"])

    def testReleaseFileForPartner(self):
        """Test Release file writing for Partner archives.

        Signed Release files must reference an uncompressed Sources and
        Packages file.
        """
        archive = self.ubuntutest.getArchiveByComponent("partner")
        allowed_suites = []
        publisher = getPublisher(archive, allowed_suites, self.logger)

        self.getPubSource(filecontent=b"Hello world", archive=archive)

        publisher.A_publish(False)
        publisher.C_writeIndexes(False)
        publisher.D_writeReleaseFiles(False)

        # Open the release file that was just published inside the
        # 'breezy-autotest' distroseries.
        release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot, "breezy-autotest", "Release"
            )
        )

        # The Release file must contain lines ending in "Packages",
        # "Packages.gz", "Sources" and "Sources.gz".
        self.assertTrue("md5sum" in release)
        self.assertTrue(
            [
                entry
                for entry in release["md5sum"]
                if entry["name"].endswith("Packages.gz")
            ]
        )
        self.assertTrue(
            [
                entry
                for entry in release["md5sum"]
                if entry["name"].endswith("Packages")
            ]
        )
        self.assertTrue(
            [
                entry
                for entry in release["md5sum"]
                if entry["name"].endswith("Sources.gz")
            ]
        )
        self.assertTrue(
            [
                entry
                for entry in release["md5sum"]
                if entry["name"].endswith("Sources")
            ]
        )

        # Partner archive architecture Release files 'Origin' contain
        # a string
        arch_release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot,
                "breezy-autotest",
                "partner/source/Release",
            )
        )
        self.assertEqual("Canonical", arch_release["origin"])

        # The Label: field should be set to the archive displayname
        self.assertEqual("Partner archive", release["label"])

    def testReleaseFileForNotAutomaticBackports(self):
        # Test Release file writing for series with NotAutomatic backports.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Hello world", pocket=RELEASE)
        self.getPubSource(filecontent=b"Hello world", pocket=BACKPORTS)

        # Make everything other than breezy-autotest OBSOLETE so that they
        # aren't republished.
        for series in self.ubuntutest.series:
            if series.name != "breezy-autotest":
                series.status = SeriesStatus.OBSOLETE

        publisher.A_publish(True)
        publisher.C_writeIndexes(False)

        def get_release(pocket):
            release_path = os.path.join(
                publisher._config.distsroot,
                "breezy-autotest%s" % pocketsuffix[pocket],
                "Release",
            )
            with open(release_path) as release_file:
                return release_file.read().splitlines()

        # When backports_not_automatic is unset, no Release files have
        # NotAutomatic: yes.
        self.assertEqual(False, self.breezy_autotest.backports_not_automatic)
        publisher.D_writeReleaseFiles(False)
        self.assertNotIn("NotAutomatic: yes", get_release(RELEASE))
        self.assertNotIn("NotAutomatic: yes", get_release(BACKPORTS))

        # But with the flag set, -backports Release files gain
        # NotAutomatic: yes and ButAutomaticUpgrades: yes.
        self.breezy_autotest.backports_not_automatic = True
        publisher.D_writeReleaseFiles(False)
        self.assertNotIn("NotAutomatic: yes", get_release(RELEASE))
        self.assertIn("NotAutomatic: yes", get_release(BACKPORTS))
        self.assertIn("ButAutomaticUpgrades: yes", get_release(BACKPORTS))

    def testReleaseFileForNotAutomaticProposed(self):
        # Test Release file writing for series with NotAutomatic -proposed.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Hello world", pocket=RELEASE)
        self.getPubSource(filecontent=b"Hello world", pocket=PROPOSED)

        # Make everything other than breezy-autotest OBSOLETE so that they
        # aren't republished.
        for series in self.ubuntutest.series:
            if series.name != "breezy-autotest":
                series.status = SeriesStatus.OBSOLETE

        publisher.A_publish(True)
        publisher.C_writeIndexes(False)

        def get_release(pocket):
            release_path = os.path.join(
                publisher._config.distsroot,
                "breezy-autotest%s" % pocketsuffix[pocket],
                "Release",
            )
            with open(release_path) as release_file:
                return release_file.read().splitlines()

        # When proposed_not_automatic is unset, no Release files have
        # NotAutomatic: yes.
        self.assertEqual(False, self.breezy_autotest.proposed_not_automatic)
        publisher.D_writeReleaseFiles(False)
        self.assertNotIn("NotAutomatic: yes", get_release(RELEASE))
        self.assertNotIn("NotAutomatic: yes", get_release(PROPOSED))

        # But with the flag set, -proposed Release files gain
        # NotAutomatic: yes and ButAutomaticUpgrades: yes.
        self.breezy_autotest.proposed_not_automatic = True
        publisher.D_writeReleaseFiles(False)
        self.assertNotIn("NotAutomatic: yes", get_release(RELEASE))
        self.assertIn("NotAutomatic: yes", get_release(PROPOSED))
        self.assertIn("ButAutomaticUpgrades: yes", get_release(PROPOSED))

    def testReleaseFileForI18n(self):
        """Test Release file writing for translated package descriptions."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Hello world")

        # Make sure that apt-ftparchive generates i18n/Translation-en* files.
        ds = self.ubuntutest.getSeries("breezy-autotest")
        ds.include_long_descriptions = False

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)
        publisher.D_writeReleaseFiles(False)

        series = os.path.join(self.config.distsroot, "breezy-autotest")
        i18n_index = os.path.join(series, "main", "i18n", "Index")

        # The i18n/Index file has been generated.
        self.assertTrue(os.path.exists(i18n_index))

        # It is listed correctly in Release.
        release = self.parseRelease(os.path.join(series, "Release"))
        with open(i18n_index, "rb") as i18n_index_file:
            self.assertReleaseContentsMatch(
                release, "main/i18n/Index", i18n_index_file.read()
            )

        components = ["main", "universe", "multiverse", "restricted"]
        release_path = os.path.join(series, "Release")
        with open(release_path) as release_file:
            content = release_file.read()
            for component in components:
                self.assertIn(component + "/i18n/Translation-en.bz2", content)
                self.assertIn(component + "/i18n/Translation-en.gz", content)

    def testReleaseFileForContents(self):
        """Test Release file writing for Contents files."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        # Put a Contents file in place, and force the publisher to republish
        # that suite.
        series_path = os.path.join(self.config.distsroot, "breezy-autotest")
        contents_path = os.path.join(series_path, "Contents-i386.gz")
        os.makedirs(os.path.dirname(contents_path))
        with gzip.GzipFile(contents_path, "wb"):
            pass
        publisher.markSuiteDirty(
            self.ubuntutest.getSeries("breezy-autotest"),
            PackagePublishingPocket.RELEASE,
        )

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)
        publisher.D_writeReleaseFiles(False)

        # The Contents file is listed correctly in Release.
        release = self.parseRelease(os.path.join(series_path, "Release"))
        with open(contents_path, "rb") as contents_file:
            self.assertReleaseContentsMatch(
                release, "Contents-i386.gz", contents_file.read()
            )

    def testReleaseFileForOval(self):
        # Test Release file writing for Oval metadata.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        # Put some Oval metadata files in place, and force the publisher
        # to republish that suite.
        series_path = os.path.join(self.config.distsroot, "breezy-autotest")
        oval_path = os.path.join(series_path, "main", "oval")
        oval_names = (
            "data1.oval.xml.gz",
            "data2.oval.xml.bz2",
            "data3.oval.xml",
            "data4.oval.xml.bz2",
        )
        os.makedirs(oval_path)
        for name in oval_names:
            if name.endswith(".gz"):
                with gzip.GzipFile(os.path.join(oval_path, name), "wb") as f:
                    f.write(name.encode())
            elif name.endswith(".bz2"):
                with bz2.open(os.path.join(oval_path, name), "wb") as f:
                    f.write(name.encode())
            else:
                with open(os.path.join(oval_path, name), "wb") as f:
                    f.write(name.encode())

        publisher.markSuiteDirty(
            self.ubuntutest.getSeries("breezy-autotest"),
            PackagePublishingPocket.RELEASE,
        )

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)
        publisher.D_writeReleaseFiles(False)

        # The metadata files are listed correctly in Release.
        release = self.parseRelease(os.path.join(series_path, "Release"))
        for name in oval_names:
            with open(os.path.join(oval_path, name), "rb") as f:
                self.assertReleaseContentsMatch(
                    release, os.path.join("main", "oval", name), f.read()
                )

    def testReleaseFileForDEP11(self):
        # Test Release file writing for DEP-11 metadata.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        # Put some DEP-11 metadata files in place, and force the publisher
        # to republish that suite.
        series_path = os.path.join(self.config.distsroot, "breezy-autotest")
        dep11_path = os.path.join(series_path, "main", "dep11")
        dep11_names = (
            "Components-amd64.yml.gz",
            "Components-i386.yml.gz",
            "icons-64x64.tar.gz",
            "icons-128x128.tar.gz",
        )
        os.makedirs(dep11_path)
        for name in dep11_names:
            with gzip.GzipFile(os.path.join(dep11_path, name), "wb") as f:
                f.write(six.ensure_binary(name))
        publisher.markSuiteDirty(
            self.ubuntutest.getSeries("breezy-autotest"),
            PackagePublishingPocket.RELEASE,
        )

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)
        publisher.D_writeReleaseFiles(False)

        # The metadata files are listed correctly in Release.
        release = self.parseRelease(os.path.join(series_path, "Release"))
        for name in dep11_names:
            with open(os.path.join(dep11_path, name), "rb") as f:
                self.assertReleaseContentsMatch(
                    release, os.path.join("main", "dep11", name), f.read()
                )

    def testReleaseFileForCommandNotFound(self):
        # Test Release file writing for command-not-found metadata.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        # Put some cnf metadata files in place, and force the publisher
        # to republish that suite.
        series_path = os.path.join(self.config.distsroot, "breezy-autotest")
        cnf_path = os.path.join(series_path, "main", "cnf")
        cnf_names = ("Commands-amd64.xz", "Commands-i386.xz")
        os.makedirs(cnf_path)
        for name in cnf_names:
            with lzma.LZMAFile(os.path.join(cnf_path, name), "wb") as f:
                f.write(six.ensure_binary(name))
        publisher.markSuiteDirty(
            self.ubuntutest.getSeries("breezy-autotest"),
            PackagePublishingPocket.RELEASE,
        )

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)
        publisher.D_writeReleaseFiles(False)

        # The metadata files are listed correctly in Release.
        release = self.parseRelease(os.path.join(series_path, "Release"))
        for name in cnf_names:
            with open(os.path.join(cnf_path, name), "rb") as f:
                self.assertReleaseContentsMatch(
                    release, os.path.join("main", "cnf", name), f.read()
                )

    def testReleaseFileTimestamps(self):
        # The timestamps of Release and all its core entries match.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        self.getPubSource(filecontent=b"Hello world")

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)

        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        sources = suite_path("main", "source", "Sources.gz")
        sources_timestamp = os.stat(sources).st_mtime - 60
        os.utime(sources, (sources_timestamp, sources_timestamp))
        dep11_path = suite_path("main", "dep11")
        dep11_names = (
            "Components-amd64.yml.gz",
            "Components-i386.yml.gz",
            "icons-64x64.tar.gz",
            "icons-128x128.tar.gz",
        )
        os.makedirs(dep11_path)
        now = time.time()
        for name in dep11_names:
            with gzip.GzipFile(os.path.join(dep11_path, name), "wb") as f:
                f.write(six.ensure_binary(name))
            os.utime(os.path.join(dep11_path, name), (now - 60, now - 60))

        publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(suite_path("Release"))
        paths = ["Release"] + [entry["name"] for entry in release["md5sum"]]
        timestamps = {
            os.stat(suite_path(path)).st_mtime
            for path in paths
            if "/dep11/" not in path and os.path.exists(suite_path(path))
        }
        self.assertEqual(1, len(timestamps))

        # Non-core files preserve their original timestamps.
        # (Due to https://bugs.python.org/issue12904, there is some loss of
        # accuracy in the test.)
        for name in dep11_names:
            self.assertThat(
                os.stat(os.path.join(dep11_path, name)).st_mtime,
                LessThan(now - 59),
            )

    def testReleaseFileWritingCreatesDirectories(self):
        # Writing Release files creates directories as needed.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        self.getPubSource()
        # Create the top-level Release file so that careful Release
        # republication is allowed.
        release_path = os.path.join(
            self.config.distsroot, "breezy-autotest", "Release"
        )
        with open_for_writing(release_path, "w"):
            pass

        publisher.D_writeReleaseFiles(True)

        source_release = os.path.join(
            self.config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        self.assertTrue(file_exists(source_release))

    def testReleaseFilePPAArchiveMetadataOverridesApplied(self):
        cprov = getUtility(IPersonSet).getByName("cprov")
        cprov.archive.displayname = "PPA for Celso Provid\xe8lo"
        login_person(cprov)
        cprov.archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
                "Label": "test_label",
                "Snapshots": "test_snapshots",
            }
        )
        publisher = getPublisher(cprov.archive, [], self.logger)

        self.getPubSource(filecontent=b"Hello world", archive=cprov.archive)

        publisher.A_publish(False)
        publisher.C_writeIndexes(False)
        publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot,
                "breezy-autotest",
                "Release",
            )
        )
        self.assertEqual("test_origin", release["origin"])
        self.assertEqual("test_label", release["label"])
        self.assertEqual("test_snapshots", release["snapshots"])

        arch_release_path = os.path.join(
            publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        arch_release = self.parseRelease(arch_release_path)
        self.assertEqual("test_origin", arch_release["origin"])
        self.assertEqual("test_label", arch_release["label"])

    def testPPAArchiveGetMetadataOverridesPopulated(self):
        cprov = getUtility(IPersonSet).getByName("cprov")
        cprov.archive.displayname = "PPA for Celso Provid\xe8lo"
        login_person(cprov)
        cprov.archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
            }
        )
        publisher = getPublisher(cprov.archive, [], self.logger)
        metadata_overrides = publisher._getMetadataOverrides()
        self.assertEqual(
            {
                "Origin": "test_origin",
            },
            metadata_overrides,
        )

    def testPPAArchiveGetMetadataOverridesEmpty(self):
        cprov = getUtility(IPersonSet).getByName("cprov")
        cprov.archive.displayname = "PPA for Celso Provid\xe8lo"
        publisher = getPublisher(cprov.archive, [], self.logger)
        metadata_overrides = publisher._getMetadataOverrides()
        self.assertEqual({}, metadata_overrides)

    def testReleaseFilePPAArchiveMetadataOverridesAppliedWithPlaceholder(self):
        cprov = getUtility(IPersonSet).getByName("cprov")
        cprov.archive.displayname = "PPA for Celso Provid\xe8lo"
        login_person(cprov)
        cprov.archive.setMetadataOverrides(
            {
                "Suite": "test_{series}",
            }
        )
        publisher = getPublisher(cprov.archive, [], self.logger)

        self.getPubSource(filecontent=b"Hello world", archive=cprov.archive)

        publisher.A_publish(False)
        publisher.C_writeIndexes(False)
        publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot,
                "breezy-autotest",
                "Release",
            )
        )
        self.assertEqual("test_breezy-autotest", release["suite"])

        arch_release_path = os.path.join(
            publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        arch_release = self.parseRelease(arch_release_path)
        self.assertEqual("test_breezy-autotest", arch_release["archive"])

    def testReleaseFilePrimaryArchiveMetadataOverridesNotApplied(self):
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        self.ubuntutest.main_archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
                "Label": "test_label",
            }
        )

        self.getPubSource(filecontent=b"Hello world")

        publisher.A_publish(False)
        publisher.C_doFTPArchive(False)
        publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(
            os.path.join(self.config.distsroot, "breezy-autotest", "Release")
        )
        self.assertEqual("ubuntutest", release["origin"])
        self.assertNotEqual("test_origin", release["origin"])
        self.assertEqual("ubuntutest", release["label"])
        self.assertNotEqual("test_label", release["label"])

        arch_release_path = os.path.join(
            self.config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        arch_release = self.parseRelease(arch_release_path)
        self.assertEqual("ubuntutest", arch_release["origin"])
        self.assertNotEqual("test_origin", arch_release["origin"])
        self.assertEqual("ubuntutest", arch_release["label"])
        self.assertNotEqual("test_label", arch_release["label"])

    def testPrimaryArchiveGetMetadataOverridesEmpty(self):
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.ubuntutest.main_archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
            }
        )
        metadata_overrides = publisher._getMetadataOverrides()
        self.assertEqual({}, metadata_overrides)

    def testReleaseFilePatnerArchiveMetadataOverridesNotApplied(self):
        partner_archive = self.ubuntutest.getArchiveByComponent("partner")
        publisher = getPublisher(partner_archive, [], self.logger)

        partner_archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
                "Label": "test_label",
            }
        )

        self.getPubSource(filecontent=b"Hello world", archive=partner_archive)

        publisher.A_publish(False)
        publisher.C_writeIndexes(False)
        publisher.D_writeReleaseFiles(False)

        release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot, "breezy-autotest", "Release"
            )
        )
        self.assertEqual("Canonical", release["origin"])
        self.assertNotEqual("test_origin", release["origin"])
        self.assertEqual("Partner archive", release["label"])
        self.assertNotEqual("test_label", release["label"])

        arch_release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot,
                "breezy-autotest",
                "partner/source/Release",
            )
        )
        self.assertEqual("Canonical", arch_release["origin"])
        self.assertNotEqual("test_origin", arch_release["origin"])
        self.assertEqual("Partner archive", release["label"])
        self.assertNotEqual("test_label", release["label"])

    def testPartnerArchiveGetMetadataOverridesEmpty(self):
        partner_archive = self.ubuntutest.getArchiveByComponent("partner")
        publisher = getPublisher(partner_archive, [], self.logger)
        partner_archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
            }
        )
        metadata_overrides = publisher._getMetadataOverrides()
        self.assertEqual({}, metadata_overrides)

    def testReleaseFileCopyArchiveMetadataOverridesNotApplied(self):
        copy_archive = self.factory.makeArchive(
            distribution=self.ubuntu, purpose=ArchivePurpose.COPY
        )
        publisher = getPublisher(copy_archive, [], self.logger)
        # We need a temproot to write indexes
        publisher._config.temproot = self.config.temproot

        copy_archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
                "Label": "test_label",
            }
        )

        self.getPubSource(filecontent=b"Hello world", archive=copy_archive)

        # Careful publishing has to be used for copy archives to be considered
        publisher.A_publish(True)
        publisher.C_writeIndexes(True)
        publisher.D_writeReleaseFiles(True)

        release = self.parseRelease(
            os.path.join(
                publisher._config.distsroot,
                "breezy-autotest",
                "Release",
            )
        )
        self.assertEqual("Ubuntu", release["origin"])
        self.assertNotEqual("test_origin", release["origin"])
        self.assertEqual("Ubuntu", release["label"])
        self.assertNotEqual("test_label", release["label"])

        arch_release_path = os.path.join(
            publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Release",
        )
        arch_release = self.parseRelease(arch_release_path)
        self.assertEqual("Ubuntu", arch_release["origin"])
        self.assertNotEqual("test_origin", arch_release["origin"])
        self.assertEqual("Ubuntu", arch_release["label"])
        self.assertNotEqual("test_label", arch_release["label"])

    def testCopyArchiveGetMetadataOverridesEmpty(self):
        copy_archive = self.factory.makeArchive(
            distribution=self.ubuntu, purpose=ArchivePurpose.COPY
        )
        publisher = getPublisher(copy_archive, [], self.logger)
        copy_archive.setMetadataOverrides(
            {
                "Origin": "test_origin",
            }
        )
        metadata_overrides = publisher._getMetadataOverrides()
        self.assertEqual({}, metadata_overrides)

    def testCreateSeriesAliasesNoAlias(self):
        """createSeriesAliases has nothing to do by default."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        publisher.createSeriesAliases()
        self.assertEqual([], os.listdir(self.config.distsroot))

    def _assertPublishesSeriesAlias(self, publisher, expected):
        publisher.A_publish(False)
        publisher.C_writeIndexes(False)
        publisher.createSeriesAliases()
        self.assertTrue(
            os.path.exists(os.path.join(self.config.distsroot, expected))
        )
        for suffix in pocketsuffix.values():
            path = os.path.join(self.config.distsroot, "devel%s" % suffix)
            expected_path = os.path.join(
                self.config.distsroot, expected + suffix
            )
            # A symlink for the RELEASE pocket exists.  Symlinks for other
            # pockets only exist if the respective targets exist.
            if not suffix or os.path.exists(expected_path):
                self.assertTrue(os.path.islink(path))
                self.assertEqual(expected + suffix, os.readlink(path))
            else:
                self.assertFalse(os.path.islink(path))

    def testCreateSeriesAliasesChangesAlias(self):
        """createSeriesAliases tracks the latest published series."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.ubuntutest.development_series_alias = "devel"
        # Oddly, hoary-test has a higher version than breezy-autotest.
        self.getPubSource(distroseries=self.ubuntutest["breezy-autotest"])
        self._assertPublishesSeriesAlias(publisher, "breezy-autotest")
        hoary_pub = self.getPubSource(
            distroseries=self.ubuntutest["hoary-test"]
        )
        self._assertPublishesSeriesAlias(publisher, "hoary-test")
        hoary_pub.requestDeletion(self.ubuntutest.owner)
        self._assertPublishesSeriesAlias(publisher, "breezy-autotest")

    def testWriteSuiteI18n(self):
        """Test i18n/Index writing."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        i18n_root = os.path.join(
            self.config.distsroot, "breezy-autotest", "main", "i18n"
        )

        # Write compressed versions of a zero-length Translation-en file.
        translation_en_index = RepositoryIndexFile(
            os.path.join(i18n_root, "Translation-en"),
            self.config.temproot,
            self.ubuntutest["breezy-autotest"].index_compressors,
        )
        translation_en_index.close()

        all_files = set()
        publisher._writeSuiteI18n(
            self.ubuntutest["breezy-autotest"],
            PackagePublishingPocket.RELEASE,
            "main",
            all_files,
        )

        # i18n/Index has the correct contents.
        translation_en = os.path.join(i18n_root, "Translation-en.bz2")
        with open(translation_en, "rb") as translation_en_file:
            translation_en_contents = translation_en_file.read()
        i18n_index = self.parseI18nIndex(os.path.join(i18n_root, "Index"))
        self.assertTrue("sha1" in i18n_index)
        self.assertEqual(3, len(i18n_index["sha1"]))
        self.assertEqual(
            hashlib.sha1(translation_en_contents).hexdigest(),
            i18n_index["sha1"][1]["sha1"],
        )
        self.assertEqual(
            str(len(translation_en_contents)), i18n_index["sha1"][1]["size"]
        )
        self.assertContentEqual(
            ["Translation-en", "Translation-en.gz", "Translation-en.bz2"],
            [hash["name"] for hash in i18n_index["sha1"]],
        )

        # i18n/Index and i18n/Translation-en.bz2 are scheduled for inclusion
        # in Release.  Checksums of the uncompressed version are included
        # despite it not actually being written to disk.
        self.assertEqual(4, len(all_files))
        self.assertContentEqual(
            [
                "main/i18n/Index",
                "main/i18n/Translation-en",
                "main/i18n/Translation-en.gz",
                "main/i18n/Translation-en.bz2",
            ],
            all_files,
        )

    def testWriteSuiteI18nMissingDirectory(self):
        """i18n/Index is not generated when the i18n directory is missing."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        i18n_root = os.path.join(
            self.config.distsroot, "breezy-autotest", "main", "i18n"
        )

        publisher._writeSuiteI18n(
            self.ubuntutest["breezy-autotest"],
            PackagePublishingPocket.RELEASE,
            "main",
            set(),
        )

        self.assertFalse(os.path.exists(os.path.join(i18n_root, "Index")))

    def testWriteSuiteI18nEmptyDirectory(self):
        """i18n/Index is not generated when the i18n directory is empty."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        i18n_root = os.path.join(
            self.config.distsroot, "breezy-autotest", "main", "i18n"
        )

        os.makedirs(i18n_root)

        publisher._writeSuiteI18n(
            self.ubuntutest["breezy-autotest"],
            PackagePublishingPocket.RELEASE,
            "main",
            set(),
        )

        self.assertFalse(os.path.exists(os.path.join(i18n_root, "Index")))

    def testWriteSuiteI18nPublishI18nFalse(self):
        """i18n/Index is not generated when publish_i18n_index is False."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        i18n_root = os.path.join(
            self.config.distsroot, "breezy-autotest", "main", "i18n"
        )

        # Write compressed versions of a zero-length Translation-en file.
        translation_en_index = RepositoryIndexFile(
            os.path.join(i18n_root, "Translation-en"),
            self.config.temproot,
            self.ubuntutest["breezy-autotest"].index_compressors,
        )
        translation_en_index.close()

        # First publish i18n/Index (status quo)
        publisher._writeSuiteI18n(
            self.ubuntutest["breezy-autotest"],
            PackagePublishingPocket.RELEASE,
            "main",
            set(),
        )

        self.assertTrue(os.path.exists(os.path.join(i18n_root, "Index")))

        self.ubuntutest["breezy-autotest"].publish_i18n_index = False

        publisher._writeSuiteI18n(
            self.ubuntutest["breezy-autotest"],
            PackagePublishingPocket.RELEASE,
            "main",
            set(),
        )

        # Ensure it is removed, if previously existed
        self.assertFalse(os.path.exists(os.path.join(i18n_root, "Index")))

    def testReadIndexFileHashesCompression(self):
        """Test compressed file handling in _readIndexFileHashes."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        contents = b"test"
        path = os.path.join(
            publisher._config.distsroot, "breezy-autotest", "Test"
        )
        os.makedirs(os.path.dirname(path))
        for suffix, open_func in (
            ("", open),
            (".gz", gzip.open),
            (".bz2", bz2.BZ2File),
            (".xz", partial(lzma.LZMAFile, format=lzma.FORMAT_XZ)),
        ):
            with open_func(path + suffix, mode="wb") as f:
                f.write(contents)
            self.assertEqual(
                {
                    "md5sum": {
                        "md5sum": hashlib.md5(contents).hexdigest(),
                        "name": "Test",
                        "size": len(contents),
                    },
                    "sha1": {
                        "sha1": hashlib.sha1(contents).hexdigest(),
                        "name": "Test",
                        "size": len(contents),
                    },
                    "sha256": {
                        "sha256": hashlib.sha256(contents).hexdigest(),
                        "name": "Test",
                        "size": len(contents),
                    },
                },
                publisher._readIndexFileHashes("breezy-autotest", "Test"),
            )
            os.remove(path + suffix)


class TestArchiveIndices(TestPublisherBase):
    """Tests for the native publisher's index generation.

    Verifies that all Packages/Sources/Release files are generated when
    appropriate.
    """

    def runStepC(self, publisher):
        """Run the index generation step of the publisher."""
        publisher.C_writeIndexes(False)

    def assertIndices(self, publisher, suites, present=(), absent=()):
        """Assert that the given suites have correct indices."""
        for series, pocket in suites:
            self.assertIndicesForSuite(
                publisher, series, pocket, present, absent
            )

    def assertIndicesForSuite(
        self, publisher, series, pocket, present=(), absent=()
    ):
        """Assert that the suite has correct indices.

        Checks that the architecture tags in 'present' have Packages and
        Release files and are in the series' Release file, and confirms
        that those in 'absent' are not.
        """

        self.assertIn(series.getSuite(pocket), publisher.release_files_needed)

        arch_template = os.path.join(
            publisher._config.distsroot, series.getSuite(pocket), "%s/%s"
        )

        release_template = os.path.join(arch_template, "Release")
        packages_template = os.path.join(arch_template, "Packages.gz")
        sources_template = os.path.join(arch_template, "Sources.gz")
        release_path = os.path.join(
            publisher._config.distsroot, series.getSuite(pocket), "Release"
        )
        with open(release_path) as release_file:
            release_content = release_file.read()

        for comp in ("main", "restricted", "universe", "multiverse"):
            # Check that source indices are present.
            for path in (release_template, sources_template):
                self.assertTrue(os.path.exists(path % (comp, "source")))

            # Check that wanted binary indices are present.
            for arch_tag in present:
                arch = "binary-" + arch_tag
                for path in (release_template, packages_template):
                    self.assertTrue(os.path.exists(path % (comp, arch)))
                self.assertTrue(arch in release_content)

            # Check that unwanted binary indices are absent.
            for arch_tag in absent:
                arch = "binary-" + arch_tag
                self.assertFalse(os.path.exists(arch_template % (comp, arch)))
                self.assertFalse(arch in release_content)

    def testAllIndicesArePublished(self):
        """Test that indices are created for all components and archs."""
        # Dirty breezy-autotest with a source. Even though there are no
        # new binaries in the suite, all its indices will still be published.
        self.getPubSource()
        self.getPubSource(pocket=PackagePublishingPocket.PROPOSED)

        # Override the series status to FROZEN, which allows publication
        # of all pockets.
        self.ubuntutest.getSeries("breezy-autotest").status = (
            SeriesStatus.FROZEN
        )

        self.config = getPubConfig(self.ubuntutest.main_archive)
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        publisher.A_publish(False)
        self.runStepC(publisher)
        publisher.D_writeReleaseFiles(False)

        self.assertIndices(
            publisher,
            [
                (self.breezy_autotest, PackagePublishingPocket.RELEASE),
                (self.breezy_autotest, PackagePublishingPocket.PROPOSED),
            ],
            present=["hppa", "i386"],
        )

    def testNoIndicesForDisabledArchitectures(self):
        """Test that no indices are created for disabled archs."""
        self.getPubBinaries()

        ds = self.ubuntutest.getSeries("breezy-autotest")
        ds.getDistroArchSeries("i386").enabled = False
        self.config = getPubConfig(self.ubuntutest.main_archive)

        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        publisher.A_publish(False)
        self.runStepC(publisher)
        publisher.D_writeReleaseFiles(False)

        self.assertIndicesForSuite(
            publisher,
            self.breezy_autotest,
            PackagePublishingPocket.RELEASE,
            present=["hppa"],
            absent=["i386"],
        )

    def testWorldAndGroupReadablePackagesAndSources(self):
        """Test Packages.gz and Sources.gz files are world readable."""
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
            allowed_suites=[],
        )

        self.getPubSource(filecontent=b"Hello world")
        publisher.A_publish(False)
        self.runStepC(publisher)

        # Find a Sources.gz and Packages.gz that were just published
        # in the breezy-autotest distroseries.
        sourcesgz_file = os.path.join(
            publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "source",
            "Sources.gz",
        )
        packagesgz_file = os.path.join(
            publisher._config.distsroot,
            "breezy-autotest",
            "main",
            "binary-i386",
            "Packages.gz",
        )

        # What permissions are set on those files?
        for file in (sourcesgz_file, packagesgz_file):
            mode = stat.S_IMODE(os.stat(file).st_mode)
            self.assertEqual(
                (stat.S_IROTH | stat.S_IRGRP),
                (mode & (stat.S_IROTH | stat.S_IRGRP)),
                "%s is not world/group readable." % file,
            )


class TestFtparchiveIndices(TestArchiveIndices):
    """Tests for the apt-ftparchive publisher's index generation."""

    def runStepC(self, publisher):
        """Run the apt-ftparchive index generation step of the publisher."""
        publisher.C_doFTPArchive(False)


class TestUpdateByHash(TestPublisherBase):
    """Tests for handling of by-hash files."""

    def setUpMockTime(self):
        """Start simulating the advance of time in the publisher."""
        self.times = [datetime.now(timezone.utc)]
        mock_datetime = mock.patch("lp.archivepublisher.publishing.datetime")
        mocked_datetime = mock_datetime.start()
        self.addCleanup(mock_datetime.stop)
        mocked_datetime.utcnow = lambda: self.times[-1].replace(tzinfo=None)
        self.useFixture(
            MonkeyPatch(
                "lp.soyuz.model.archivefile._now", lambda: self.times[-1]
            )
        )
        self.useFixture(
            MonkeyPatch(
                "lp.archivepublisher.publishing.get_transaction_timestamp",
                lambda _: self.times[-1],
            )
        )

    def advanceTime(self, delta=None, absolute=None):
        if delta is not None:
            self.times.append(self.times[-1] + delta)
        else:
            self.times.append(absolute)

    def runSteps(
        self,
        publisher,
        step_a=False,
        step_a2=False,
        step_c=False,
        step_d=False,
    ):
        """Run publisher steps."""
        if step_a:
            publisher.A_publish(False)
        if step_a2:
            publisher.A2_markPocketsWithDeletionsDirty()
        if step_c:
            publisher.C_doFTPArchive(False)
        if step_d:
            publisher.D_writeReleaseFiles(False)

    @classmethod
    def _makeScheduledDeletionDateMatcher(cls, superseded_at):
        if superseded_at is None:
            return Is(None)
        else:
            return Equals(
                superseded_at + timedelta(days=BY_HASH_STAY_OF_EXECUTION)
            )

    def assertHasSuiteFiles(
        self,
        patterns: Sequence[str],
        *properties: Tuple[str, Optional[int], Optional[int], Optional[int]],
    ) -> None:
        """Assert that the database records certain archive files.

        :param patterns: A sequence of `fnmatch` patterns for files under
            `dists/breezy-autotest/` that we're interested in.
        :param properties: A sequence of (`path`, `created_at`,
            `superseded_at`, `removed_at`) tuples.  Each must match one of
            the `ArchiveFile` rows matching `patterns`; `path` is relative
            to `dists/breezy-autotest/`, while the `*_at` properties are
            either None (indicating that the corresponding `date_*` column
            should be None) or an index into `self.times` (indicating that
            the corresponding `date_*` column should equal that time).
        """

        def is_interesting(path):
            return any(
                fnmatch(path, "dists/breezy-autotest/%s" % pattern)
                for pattern in patterns
            )

        files = [
            archive_file
            for archive_file in getUtility(IArchiveFileSet).getByArchive(
                self.ubuntutest.main_archive
            )
            if is_interesting(archive_file.path)
        ]
        matchers = []
        for path, created_at, superseded_at, removed_at in properties:
            created_at = None if created_at is None else self.times[created_at]
            superseded_at = (
                None if superseded_at is None else self.times[superseded_at]
            )
            removed_at = None if removed_at is None else self.times[removed_at]
            scheduled_deletion_date_matcher = (
                self._makeScheduledDeletionDateMatcher(superseded_at)
            )
            matchers.append(
                MatchesStructure(
                    path=Equals("dists/breezy-autotest/%s" % path),
                    date_created=Equals(created_at),
                    date_superseded=Equals(superseded_at),
                    scheduled_deletion_date=scheduled_deletion_date_matcher,
                    date_removed=Equals(removed_at),
                )
            )
        self.assertThat(files, MatchesSetwise(*matchers))

    def test_disabled(self):
        # The publisher does not create by-hash directories if it is
        # disabled in the series configuration.
        self.assertFalse(self.breezy_autotest.publish_by_hash)
        self.assertFalse(self.breezy_autotest.advertise_by_hash)
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Source: foo\n")
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)

        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        self.assertThat(
            suite_path("main", "source", "by-hash"), Not(PathExists())
        )
        with open(suite_path("Release")) as release_file:
            release = Release(release_file)
        self.assertNotIn("Acquire-By-Hash", release)

    def test_unadvertised(self):
        # If the series configuration sets publish_by_hash but not
        # advertise_by_hash, then by-hash directories are created but not
        # advertised in Release.  This is useful for testing.
        self.breezy_autotest.publish_by_hash = True
        self.assertFalse(self.breezy_autotest.advertise_by_hash)
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Source: foo\n")
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)

        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        self.assertThat(suite_path("main", "source", "by-hash"), PathExists())
        with open(suite_path("Release")) as release_file:
            release = Release(release_file)
        self.assertNotIn("Acquire-By-Hash", release)

    def test_initial(self):
        # An initial publisher run populates by-hash directories and leaves
        # no archive files scheduled for deletion.
        self.breezy_autotest.publish_by_hash = True
        self.breezy_autotest.advertise_by_hash = True
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Source: foo\n")
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        flush_database_caches()

        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        top_contents = set()
        with open(suite_path("Release"), "rb") as f:
            top_contents.add(f.read())
        main_contents = set()
        for name in ("Release", "Sources.gz", "Sources.bz2"):
            with open(suite_path("main", "source", name), "rb") as f:
                main_contents.add(f.read())

        self.assertThat(suite_path("by-hash"), ByHashHasContents(top_contents))
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )

        archive_files = getUtility(IArchiveFileSet).getByArchive(
            self.ubuntutest.main_archive
        )
        self.assertNotEqual([], archive_files)
        self.assertEqual(
            [],
            [
                archive_file
                for archive_file in archive_files
                if archive_file.scheduled_deletion_date is not None
            ],
        )

    def test_subsequent(self):
        # A subsequent publisher run updates by-hash directories where
        # necessary, and marks inactive index files for later deletion.
        self.breezy_autotest.publish_by_hash = True
        self.breezy_autotest.advertise_by_hash = True
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.getPubSource(filecontent=b"Source: foo\n")
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)

        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        top_contents = set()
        main_contents = set()
        universe_contents = set()
        with open(suite_path("Release"), "rb") as f:
            top_contents.add(f.read())
        for name in ("Release", "Sources.gz", "Sources.bz2"):
            with open(suite_path("main", "source", name), "rb") as f:
                main_contents.add(f.read())
            with open(suite_path("universe", "source", name), "rb") as f:
                universe_contents.add(f.read())

        self.getPubSource(sourcename="baz", filecontent=b"Source: baz\n")
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        flush_database_caches()

        with open(suite_path("Release"), "rb") as f:
            top_contents.add(f.read())
        for name in ("Release", "Sources.gz", "Sources.bz2"):
            with open(suite_path("main", "source", name), "rb") as f:
                main_contents.add(f.read())

        self.assertThat(suite_path("by-hash"), ByHashHasContents(top_contents))
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )
        self.assertThat(
            suite_path("universe", "source", "by-hash"),
            ByHashHasContents(universe_contents),
        )

        archive_files = getUtility(IArchiveFileSet).getByArchive(
            self.ubuntutest.main_archive
        )
        self.assertContentEqual(
            [
                "dists/breezy-autotest/Release",
                "dists/breezy-autotest/main/source/Sources.bz2",
                "dists/breezy-autotest/main/source/Sources.gz",
            ],
            [
                archive_file.path
                for archive_file in archive_files
                if archive_file.scheduled_deletion_date is not None
            ],
        )

    def test_identical_files(self):
        # Multiple identical files in the same directory receive multiple
        # ArchiveFile rows, even though they share a by-hash entry.
        self.breezy_autotest.publish_by_hash = True
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        self.setUpMockTime()

        def get_release_contents():
            with open(suite_path("Release"), "rb") as f:
                return f.read()

        # Create the first file.
        with open_for_writing(suite_path("Contents-i386"), "w") as f:
            f.write("A Contents file\n")
        publisher.markSuiteDirty(
            self.breezy_autotest, PackagePublishingPocket.RELEASE
        )
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        flush_database_caches()
        self.assertHasSuiteFiles(
            ("Contents-*", "Release"),
            ("Contents-i386", 0, None, None),
            ("Release", 0, None, None),
        )
        releases = [get_release_contents()]
        self.assertThat(
            suite_path("by-hash"),
            ByHashHasContents([b"A Contents file\n"] + releases),
        )

        # Add a second identical file.
        with open_for_writing(suite_path("Contents-hppa"), "w") as f:
            f.write("A Contents file\n")
        self.advanceTime(delta=timedelta(hours=1))
        self.runSteps(publisher, step_d=True)
        flush_database_caches()
        self.assertHasSuiteFiles(
            ("Contents-*", "Release"),
            ("Contents-i386", 0, None, None),
            ("Contents-hppa", 1, None, None),
            ("Release", 0, 1, None),
            ("Release", 1, None, None),
        )
        releases.append(get_release_contents())
        self.assertThat(
            suite_path("by-hash"),
            ByHashHasContents([b"A Contents file\n"] + releases),
        )

        # Delete the first file, but allow it its stay of execution.
        os.unlink(suite_path("Contents-i386"))
        self.advanceTime(delta=timedelta(hours=1))
        self.runSteps(publisher, step_d=True)
        flush_database_caches()
        self.assertHasSuiteFiles(
            ("Contents-*", "Release"),
            ("Contents-i386", 0, 2, None),
            ("Contents-hppa", 1, None, None),
            ("Release", 0, 1, None),
            ("Release", 1, 2, None),
            ("Release", 2, None, None),
        )
        releases.append(get_release_contents())
        self.assertThat(
            suite_path("by-hash"),
            ByHashHasContents([b"A Contents file\n"] + releases),
        )

        # A no-op run leaves the scheduled deletion date intact.
        self.advanceTime(delta=timedelta(hours=1))
        self.runSteps(publisher, step_d=True)
        flush_database_caches()
        self.assertHasSuiteFiles(
            ("Contents-*", "Release"),
            ("Contents-i386", 0, 2, None),
            ("Contents-hppa", 1, None, None),
            ("Release", 0, 1, None),
            ("Release", 1, 2, None),
            ("Release", 2, 3, None),
            ("Release", 3, None, None),
        )
        releases.append(get_release_contents())
        self.assertThat(
            suite_path("by-hash"),
            ByHashHasContents([b"A Contents file\n"] + releases),
        )

        # Arrange for the first file to be pruned, and delete the second
        # file.  This also puts us past the stay of execution of the first
        # two Release files.
        i386_file = (
            getUtility(IArchiveFileSet)
            .getByArchive(
                self.ubuntutest.main_archive,
                path="dists/breezy-autotest/Contents-i386",
            )
            .one()
        )
        self.advanceTime(
            absolute=i386_file.scheduled_deletion_date + timedelta(minutes=5)
        )
        os.unlink(suite_path("Contents-hppa"))
        self.runSteps(publisher, step_d=True)
        flush_database_caches()
        self.assertHasSuiteFiles(
            ("Contents-*", "Release"),
            ("Contents-i386", 0, 2, 4),
            ("Contents-hppa", 1, 4, None),
            ("Release", 0, 1, 4),
            ("Release", 1, 2, 4),
            ("Release", 2, 3, None),
            ("Release", 3, 4, None),
            ("Release", 4, None, None),
        )
        releases.append(get_release_contents())
        self.assertThat(
            suite_path("by-hash"),
            ByHashHasContents([b"A Contents file\n"] + releases[2:]),
        )

        # Arrange for the second file to be pruned.  This also puts us past
        # the stay of execution of the first two remaining Release files.
        hppa_file = (
            getUtility(IArchiveFileSet)
            .getByArchive(
                self.ubuntutest.main_archive,
                path="dists/breezy-autotest/Contents-hppa",
            )
            .one()
        )
        self.advanceTime(
            absolute=hppa_file.scheduled_deletion_date + timedelta(minutes=5)
        )
        self.runSteps(publisher, step_d=True)
        flush_database_caches()
        self.assertHasSuiteFiles(
            ("Contents-*", "Release"),
            ("Contents-i386", 0, 2, 4),
            ("Contents-hppa", 1, 4, 5),
            ("Release", 0, 1, 4),
            ("Release", 1, 2, 4),
            ("Release", 2, 3, 5),
            ("Release", 3, 4, 5),
            ("Release", 4, 5, None),
            ("Release", 5, None, None),
        )
        releases.append(get_release_contents())
        self.assertThat(suite_path("by-hash"), ByHashHasContents(releases[4:]))

    def test_reprieve(self):
        # If a newly-modified index file is identical to a
        # previously-condemned one, then it is reprieved and not pruned.
        self.breezy_autotest.publish_by_hash = True
        # Enable uncompressed index files to avoid relying on stable output
        # from compressors in this test.
        self.breezy_autotest.index_compressors = [
            IndexCompressionType.UNCOMPRESSED
        ]
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.setUpMockTime()

        # Publish empty index files.
        publisher.markSuiteDirty(
            self.breezy_autotest, PackagePublishingPocket.RELEASE
        )
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        main_contents = set()
        for name in ("Release", "Sources"):
            with open(suite_path("main", "source", name), "rb") as f:
                main_contents.add(f.read())
        self.assertHasSuiteFiles(
            ("main/source/Sources",),
            ("main/source/Sources", 0, None, None),
        )

        # Add a source package so that Sources is non-empty.
        pub_source = self.getPubSource(filecontent=b"Source: foo\n")
        self.advanceTime(delta=timedelta(hours=1))
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        transaction.commit()
        with open(suite_path("main", "source", "Sources"), "rb") as f:
            main_contents.add(f.read())
        self.assertEqual(3, len(main_contents))
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )
        self.assertHasSuiteFiles(
            ("main/source/Sources",),
            ("main/source/Sources", 0, 1, None),
            ("main/source/Sources", 1, None, None),
        )

        # Delete the source package so that Sources is empty again.  The
        # empty file is reprieved (by creating a new ArchiveFile referring
        # to it) and the non-empty one is condemned.
        pub_source.requestDeletion(self.ubuntutest.owner)
        self.advanceTime(delta=timedelta(hours=1))
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        transaction.commit()
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )
        self.assertHasSuiteFiles(
            ("main/source/Sources",),
            ("main/source/Sources", 0, 1, None),
            ("main/source/Sources", 1, 2, None),
            ("main/source/Sources", 2, None, None),
        )

        # Make the first empty Sources file ready to prune.  This doesn't
        # change the set of files on disk, because there's still a newer
        # reference to the empty file.
        self.advanceTime(
            absolute=self.times[1]
            + timedelta(days=BY_HASH_STAY_OF_EXECUTION, minutes=30)
        )
        self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
        transaction.commit()
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )
        self.assertHasSuiteFiles(
            ("main/source/Sources",),
            ("main/source/Sources", 0, 1, 3),
            ("main/source/Sources", 1, 2, None),
            ("main/source/Sources", 2, None, None),
        )

    def setUpPruneableSuite(self):
        self.setUpMockTime()
        self.breezy_autotest.publish_by_hash = True
        self.breezy_autotest.advertise_by_hash = True
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )

        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        top_contents = []
        main_contents = []
        for sourcename in ("foo", "bar", "baz"):
            self.getPubSource(
                sourcename=sourcename,
                filecontent=("Source: %s\n" % sourcename).encode(),
            )
            self.runSteps(publisher, step_a=True, step_c=True, step_d=True)
            with open(suite_path("Release"), "rb") as f:
                top_contents.append(f.read())
            for name in ("Release", "Sources.gz", "Sources.bz2"):
                with open(suite_path("main", "source", name), "rb") as f:
                    main_contents.append(f.read())
            # Advance time between each publisher run.  We don't advance
            # time after the last one, since we'll do that below.
            if sourcename != "baz":
                self.advanceTime(delta=timedelta(hours=6))
        transaction.commit()

        # We have two condemned sets of index files and one uncondemned set.
        # main/source/Release contains a small enough amount of information
        # that it doesn't change.
        self.assertHasSuiteFiles(
            ("main/source/*", "Release"),
            ("main/source/Sources.gz", 0, 1, None),
            ("main/source/Sources.gz", 1, 2, None),
            ("main/source/Sources.gz", 2, None, None),
            ("main/source/Sources.bz2", 0, 1, None),
            ("main/source/Sources.bz2", 1, 2, None),
            ("main/source/Sources.bz2", 2, None, None),
            ("main/source/Release", 0, None, None),
            ("Release", 0, 1, None),
            ("Release", 1, 2, None),
            ("Release", 2, None, None),
        )
        self.assertThat(suite_path("by-hash"), ByHashHasContents(top_contents))
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )

        # Advance time to the point where the first condemned set of index
        # files is scheduled for deletion.
        self.advanceTime(
            absolute=self.times[1]
            + timedelta(days=BY_HASH_STAY_OF_EXECUTION, hours=1)
        )
        del top_contents[0]
        del main_contents[:3]

        return top_contents, main_contents

    def test_prune(self):
        # The publisher prunes files from by-hash that were condemned more
        # than a day ago.
        top_contents, main_contents = self.setUpPruneableSuite()
        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )

        # Use a fresh Publisher instance to ensure that it doesn't have
        # dirty-pocket state left over from the last run.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.runSteps(publisher, step_a2=True, step_c=True, step_d=True)
        transaction.commit()
        self.assertEqual(set(), publisher.dirty_suites)
        # The condemned index files are removed, and no new Release file is
        # generated.
        self.assertHasSuiteFiles(
            ("main/source/*", "Release"),
            ("main/source/Sources.gz", 0, 1, 3),
            ("main/source/Sources.gz", 1, 2, None),
            ("main/source/Sources.gz", 2, None, None),
            ("main/source/Sources.bz2", 0, 1, 3),
            ("main/source/Sources.bz2", 1, 2, None),
            ("main/source/Sources.bz2", 2, None, None),
            ("main/source/Release", 0, None, None),
            ("Release", 0, 1, 3),
            ("Release", 1, 2, None),
            ("Release", 2, None, None),
        )
        self.assertThat(suite_path("by-hash"), ByHashHasContents(top_contents))
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )

    def test_prune_immutable(self):
        # The publisher prunes by-hash files from immutable suites, but
        # doesn't regenerate the Release file in that case.
        top_contents, main_contents = self.setUpPruneableSuite()
        suite_path = partial(
            os.path.join, self.config.distsroot, "breezy-autotest"
        )
        release_path = suite_path("Release")
        release_mtime = os.stat(release_path).st_mtime

        self.breezy_autotest.status = SeriesStatus.CURRENT
        # Use a fresh Publisher instance to ensure that it doesn't have
        # dirty-pocket state left over from the last run.
        publisher = Publisher(
            self.logger,
            self.config,
            self.disk_pool,
            self.ubuntutest.main_archive,
        )
        self.runSteps(publisher, step_a2=True, step_c=True, step_d=True)
        transaction.commit()
        self.assertEqual(set(), publisher.dirty_suites)
        self.assertEqual(release_mtime, os.stat(release_path).st_mtime)
        # The condemned index files are removed, and no new Release file is
        # generated.
        self.assertHasSuiteFiles(
            ("main/source/*", "Release"),
            ("main/source/Sources.gz", 0, 1, 3),
            ("main/source/Sources.gz", 1, 2, None),
            ("main/source/Sources.gz", 2, None, None),
            ("main/source/Sources.bz2", 0, 1, 3),
            ("main/source/Sources.bz2", 1, 2, None),
            ("main/source/Sources.bz2", 2, None, None),
            ("main/source/Release", 0, None, None),
            ("Release", 0, 1, 3),
            ("Release", 1, 2, None),
            ("Release", 2, None, None),
        )
        self.assertThat(suite_path("by-hash"), ByHashHasContents(top_contents))
        self.assertThat(
            suite_path("main", "source", "by-hash"),
            ByHashHasContents(main_contents),
        )


class TestUpdateByHashOverriddenDistsroot(TestUpdateByHash):
    """Test by-hash handling with an overridden distsroot.

    This exercises the way that the publisher is used by PublishFTPMaster.
    """

    def runSteps(self, publisher, **kwargs):
        """Run publisher steps with an overridden distsroot."""
        original_dists = self.config.distsroot
        temporary_dists = original_dists + ".in-progress"
        if not os.path.exists(original_dists):
            os.makedirs(original_dists)
        os.rename(original_dists, temporary_dists)
        try:
            self.config.distsroot = temporary_dists
            super().runSteps(publisher, **kwargs)
        finally:
            self.config.distsroot = original_dists
            os.rename(temporary_dists, original_dists)


class TestPublisherRepositorySignatures(
    WithScenarios, RunPartsMixin, TestPublisherBase
):
    """Testing `Publisher` signature behaviour."""

    scenarios = [
        ("default distsroot", {"override_distsroot": False}),
        ("overridden distsroot", {"override_distsroot": True}),
    ]

    run_tests_with = AsynchronousDeferredRunTest.make_factory(timeout=30)

    archive_publisher = None

    def tearDown(self):
        """Purge the archive root location."""
        if self.archive_publisher is not None:
            shutil.rmtree(self.archive_publisher._config.distsroot)
        super().tearDown()

    def setupPublisher(self, archive):
        """Setup a `Publisher` instance for the given archive."""
        if self.archive_publisher is None:
            allowed_suites = []
            self.archive_publisher = getPublisher(
                archive, allowed_suites, self.logger
            )
            if self.override_distsroot:
                self.archive_publisher._config.distsroot = (
                    self.makeTemporaryDirectory()
                )

    def _publishArchive(self, archive):
        """Publish a test source in the given archive.

        Publish files in pool, generate archive indexes and release files.
        """
        self.setupPublisher(archive)
        self.getPubSource(archive=archive)

        self.archive_publisher.A_publish(False)
        transaction.commit()
        self.archive_publisher.C_writeIndexes(False)
        self.archive_publisher.D_writeReleaseFiles(False)

    @property
    def suite_path(self):
        return os.path.join(
            self.archive_publisher._config.distsroot, "breezy-autotest"
        )

    @property
    def release_file_path(self):
        return os.path.join(self.suite_path, "Release")

    @property
    def release_file_signature_path(self):
        return os.path.join(self.suite_path, "Release.gpg")

    @property
    def inline_release_file_path(self):
        return os.path.join(self.suite_path, "InRelease")

    @property
    def public_key_path(self):
        return os.path.join(
            self.archive_publisher._config.distsroot, "key.gpg"
        )

    def testRepositorySignatureWithNoSigningKey(self):
        """Check publisher behaviour when signing repositories.

        Repository signing procedure is skipped for archive with no
        'signing_key'.
        """
        cprov = getUtility(IPersonSet).getByName("cprov")
        self.assertTrue(cprov.archive.signing_key is None)

        self.setupPublisher(cprov.archive)
        self.archive_publisher._syncTimestamps = FakeMethod()

        self._publishArchive(cprov.archive)

        # Release file exist but it doesn't have any signature.
        self.assertTrue(os.path.exists(self.release_file_path))
        self.assertFalse(os.path.exists(self.release_file_signature_path))

        # The publisher synchronises the timestamp of the Release file with
        # any other files, but does not do anything to Release.gpg or
        # InRelease.
        self.assertEqual(1, self.archive_publisher._syncTimestamps.call_count)
        sync_args = self.archive_publisher._syncTimestamps.extract_args()[0]
        self.assertEqual(self.distroseries.name, sync_args[0])
        self.assertIn("Release", sync_args[1])
        self.assertNotIn("Release.gpg", sync_args[1])
        self.assertNotIn("InRelease", sync_args[1])

    @defer.inlineCallbacks
    def testRepositorySignatureWithSigningKey(self):
        """Check publisher behaviour when signing repositories.

        When the 'signing_key' is available every modified suite Release
        file gets signed with a detached signature name 'Release.gpg' and
        a clearsigned file name 'InRelease'.
        """
        cprov = getUtility(IPersonSet).getByName("cprov")
        self.assertTrue(cprov.archive.signing_key is None)

        # Start the test keyserver, so the signing_key can be uploaded.
        yield self.useFixture(InProcessKeyServerFixture()).start()

        # Set a signing key for Celso's PPA.
        key_path = os.path.join(gpgkeysdir, "ppa-sample@canonical.com.sec")
        yield IArchiveGPGSigningKey(cprov.archive).setSigningKey(
            key_path, async_keyserver=True
        )
        self.assertTrue(cprov.archive.signing_key is not None)

        self.setupPublisher(cprov.archive)
        self.archive_publisher._syncTimestamps = FakeMethod()

        self._publishArchive(cprov.archive)

        # All of Release, Release.gpg, and InRelease exist.
        self.assertTrue(os.path.exists(self.release_file_path))
        self.assertTrue(os.path.exists(self.release_file_signature_path))
        self.assertTrue(os.path.exists(self.inline_release_file_path))

        # Release file signature is correct and was done by Celso's PPA
        # signing_key.
        with open(self.release_file_path, "rb") as release_file:
            release_content = release_file.read()
            with open(
                self.release_file_signature_path, "rb"
            ) as release_file_sig:
                signature = getUtility(IGPGHandler).getVerifiedSignature(
                    release_content, release_file_sig.read()
                )
        self.assertEqual(
            cprov.archive.signing_key.fingerprint, signature.fingerprint
        )

        # InRelease file signature and content are correct, and the
        # signature was done by Celso's PPA signing_key.
        with open(self.inline_release_file_path, "rb") as inline_release_file:
            inline_signature = getUtility(IGPGHandler).getVerifiedSignature(
                inline_release_file.read()
            )
        self.assertEqual(
            inline_signature.fingerprint, cprov.archive.signing_key.fingerprint
        )
        self.assertEqual(release_content, inline_signature.plain_data)

        # The publisher synchronises the various Release file timestamps.
        self.assertEqual(1, self.archive_publisher._syncTimestamps.call_count)
        sync_args = self.archive_publisher._syncTimestamps.extract_args()[0]
        self.assertEqual(self.distroseries.name, sync_args[0])
        self.assertThat(
            sync_args[1], ContainsAll(["Release", "Release.gpg", "InRelease"])
        )

    def testRepositorySignatureWithExternalRunParts(self):
        """Check publisher behaviour when signing repositories.

        When a 'sign.d' run-parts directory is configured for the archive,
        it is used to sign the Release file.
        """
        cprov = getUtility(IPersonSet).getByName("cprov")
        self.assertIsNone(cprov.archive.signing_key)
        self.enableRunParts(distribution_name=cprov.archive.distribution.name)
        sign_directory = os.path.join(
            self.parts_directory, cprov.archive.distribution.name, "sign.d"
        )
        with open(os.path.join(sign_directory, "10-sign"), "w") as sign_script:
            sign_script.write(
                dedent(
                    """\
                #! /bin/sh
                echo "$MODE signature of $INPUT_PATH" \\
                     "($ARCHIVEROOT, $DISTRIBUTION/$SUITE)" \\
                    >"$OUTPUT_PATH"
                """
                )
            )
            os.fchmod(sign_script.fileno(), 0o755)

        self.setupPublisher(cprov.archive)
        self.archive_publisher._syncTimestamps = FakeMethod()

        self._publishArchive(cprov.archive)

        # Release exists.
        self.assertThat(self.release_file_path, PathExists())

        # Release.gpg and InRelease exist with suitable fake signatures.
        # Note that the signatures are made before Release.new is renamed to
        # Release.
        self.assertThat(
            self.release_file_signature_path,
            FileContains(
                "detached signature of %s.new (%s, %s/breezy-autotest)\n"
                % (
                    self.release_file_path,
                    self.archive_publisher._config.archiveroot,
                    cprov.archive.distribution.name,
                )
            ),
        )
        self.assertThat(
            self.inline_release_file_path,
            FileContains(
                "clear signature of %s.new (%s, %s/breezy-autotest)\n"
                % (
                    self.release_file_path,
                    self.archive_publisher._config.archiveroot,
                    cprov.archive.distribution.name,
                )
            ),
        )

        # The publisher synchronises the various Release file timestamps.
        self.assertEqual(1, self.archive_publisher._syncTimestamps.call_count)
        sync_args = self.archive_publisher._syncTimestamps.extract_args()[0]
        self.assertEqual(self.distroseries.name, sync_args[0])
        self.assertThat(
            sync_args[1], ContainsAll(["Release", "Release.gpg", "InRelease"])
        )

    def testRepositorySignatureWithSelectiveRunParts(self):
        """Check publisher behaviour when partially signing repositories.

        A 'sign.d' run-parts implementation may choose to produce only a
        subset of signatures.
        """
        cprov = getUtility(IPersonSet).getByName("cprov")
        self.assertIsNone(cprov.archive.signing_key)
        self.enableRunParts(distribution_name=cprov.archive.distribution.name)
        sign_directory = os.path.join(
            self.parts_directory, cprov.archive.distribution.name, "sign.d"
        )
        with open(os.path.join(sign_directory, "10-sign"), "w") as sign_script:
            sign_script.write(
                dedent(
                    """\
                #! /bin/sh
                [ "$(basename "$OUTPUT_PATH" .new)" != InRelease ] || exit 0
                echo "$MODE signature of $INPUT_PATH" \\
                     "($ARCHIVEROOT, $DISTRIBUTION/$SUITE)" \\
                    >"$OUTPUT_PATH"
                """
                )
            )
            os.fchmod(sign_script.fileno(), 0o755)

        self.setupPublisher(cprov.archive)
        self.archive_publisher._syncTimestamps = FakeMethod()

        self._publishArchive(cprov.archive)

        # Release exists.
        self.assertThat(self.release_file_path, PathExists())

        # Release.gpg exists with a suitable fake signature.  Note that the
        # signature is made before Release.new is renamed to Release.
        self.assertThat(
            self.release_file_signature_path,
            FileContains(
                "detached signature of %s.new (%s, %s/breezy-autotest)\n"
                % (
                    self.release_file_path,
                    self.archive_publisher._config.archiveroot,
                    cprov.archive.distribution.name,
                )
            ),
        )

        # InRelease does not exist.
        self.assertThat(self.inline_release_file_path, Not(PathExists()))

        # The publisher synchronises the various Release file timestamps.
        self.assertEqual(1, self.archive_publisher._syncTimestamps.call_count)
        sync_args = self.archive_publisher._syncTimestamps.extract_args()[0]
        self.assertEqual(self.distroseries.name, sync_args[0])
        self.assertThat(sync_args[1], ContainsAll(["Release", "Release.gpg"]))
        self.assertNotIn("InRelease", sync_args[1])


class TestPublisherLite(TestCaseWithFactory):
    """Lightweight unit tests for the publisher."""

    layer = ZopelessDatabaseLayer

    def makePublishableSeries(self, root_dir):
        """Create a `DistroSeries` ready for publishing.

        :param root_dir: A temporary directory for use as an archive root.
        """
        distro = self.factory.makeDistribution(publish_root_dir=root_dir)
        return self.factory.makeDistroSeries(
            distribution=distro, status=SeriesStatus.FROZEN
        )

    def getReleaseFileDir(self, root, distroseries, suite):
        """Locate the directory where a Release file should be.

        :param root: Archive root directory.
        :param distroseries: Published distroseries.
        :param suite: Published suite.
        """
        return os.path.join(
            root, distroseries.distribution.name, "dists", suite
        )

    def makePublishablePackage(self, series):
        """Create a source publication ready for publishing."""
        return self.factory.makeSourcePackagePublishingHistory(
            distroseries=series, status=PackagePublishingStatus.PENDING
        )

    def makePublisher(self, archive_or_series):
        """Create a publisher for a given archive or distroseries."""
        if IDistroSeries.providedBy(archive_or_series):
            archive_or_series = archive_or_series.main_archive
        return getPublisher(archive_or_series, None, DevNullLogger())

    def makeFakeReleaseData(self):
        """Create a fake `debian.deb822.Release`.

        The object's dump method will write arbitrary text.  For testing
        purposes, the fake object will compare equal to a string holding
        this same text, encoded in the requested encoding.
        """

        class FakeReleaseData(str):
            def dump(self, output_file, encoding):
                output_file.write(self.encode(encoding))

        return FakeReleaseData(self.factory.getUniqueUnicode())

    def test_writeReleaseFile_dumps_release_file(self):
        # _writeReleaseFile writes a Release file for a suite.
        root = self.makeTemporaryDirectory()
        series = self.makePublishableSeries(root)
        spph = self.makePublishablePackage(series)
        suite = series.name + pocketsuffix[spph.pocket]
        releases_dir = self.getReleaseFileDir(root, series, suite)
        os.makedirs(releases_dir)
        release_data = self.makeFakeReleaseData()
        release_path = os.path.join(releases_dir, "Release.new")

        self.makePublisher(series)._writeReleaseFile(suite, release_data)

        self.assertTrue(file_exists(release_path))
        with open(release_path) as release_file:
            self.assertEqual(release_data, release_file.read())

    def test_writeReleaseFile_creates_directory_if_necessary(self):
        # If the suite is new and its release directory does not exist
        # yet, _writeReleaseFile will create it.
        root = self.makeTemporaryDirectory()
        series = self.makePublishableSeries(root)
        spph = self.makePublishablePackage(series)
        suite = series.name + pocketsuffix[spph.pocket]
        release_data = self.makeFakeReleaseData()
        release_path = os.path.join(
            self.getReleaseFileDir(root, series, suite), "Release.new"
        )

        self.makePublisher(series)._writeReleaseFile(suite, release_data)

        self.assertTrue(file_exists(release_path))

    def test_syncTimestamps_makes_timestamps_match_latest(self):
        root = self.makeTemporaryDirectory()
        series = self.makePublishableSeries(root)
        location = self.getReleaseFileDir(root, series, series.name)
        os.makedirs(location)
        now = time.time()
        path_times = (("a", now), ("b", now - 1), ("c", now - 2))
        for path, timestamp in path_times:
            with open(os.path.join(location, path), "w"):
                pass
            os.utime(os.path.join(location, path), (timestamp, timestamp))

        paths = [path for path, _ in path_times]
        self.makePublisher(series)._syncTimestamps(series.name, set(paths))

        timestamps = {
            os.stat(os.path.join(location, path)).st_mtime for path in paths
        }
        self.assertEqual(1, len(timestamps))
        # The filesystem may round off subsecond parts of timestamps.
        self.assertEqual(int(now), int(list(timestamps)[0]))

    def test_subcomponents(self):
        primary = self.factory.makeArchive(purpose=ArchivePurpose.PRIMARY)
        self.assertEqual(
            ["debian-installer"], self.makePublisher(primary).subcomponents
        )
        primary.publish_debug_symbols = True
        self.assertEqual(
            ["debian-installer", "debug"],
            self.makePublisher(primary).subcomponents,
        )

        partner = self.factory.makeArchive(purpose=ArchivePurpose.PARTNER)
        self.assertEqual([], self.makePublisher(partner).subcomponents)


class TestDirectoryHashHelpers(TestCaseWithFactory):
    """Helper functions for DirectoryHash testing."""

    def createTestFile(self, path, content):
        with open(path, "wb") as tfd:
            tfd.write(content)
        return hashlib.sha256(content).hexdigest()

    @property
    def all_hash_files(self):
        return ["MD5SUMS", "SHA1SUMS", "SHA256SUMS"]

    @property
    def expected_hash_files(self):
        return ["SHA256SUMS"]

    def fetchSums(self, rootdir):
        result = defaultdict(list)
        for dh_file in self.all_hash_files:
            checksum_file = os.path.join(rootdir, dh_file)
            if os.path.exists(checksum_file):
                with open(checksum_file) as sfd:
                    for line in sfd:
                        result[dh_file].append(line.strip().split(" "))
        return result


class TestDirectoryHash(TestDirectoryHashHelpers):
    """Unit tests for DirectoryHash object."""

    layer = ZopelessDatabaseLayer

    def test_checksum_files_created(self):
        tmpdir = self.makeTemporaryDirectory()
        rootdir = self.makeTemporaryDirectory()

        for dh_file in self.all_hash_files:
            checksum_file = os.path.join(rootdir, dh_file)
            self.assertFalse(os.path.exists(checksum_file))

        with DirectoryHash(rootdir, tmpdir):
            pass

        for dh_file in self.all_hash_files:
            checksum_file = os.path.join(rootdir, dh_file)
            if dh_file in self.expected_hash_files:
                self.assertTrue(os.path.exists(checksum_file))
            else:
                self.assertFalse(os.path.exists(checksum_file))

    def test_basic_file_add(self):
        tmpdir = self.makeTemporaryDirectory()
        rootdir = self.makeTemporaryDirectory()
        test1_file = os.path.join(rootdir, "test1")
        test1_hash = self.createTestFile(test1_file, b"test1")

        test2_file = os.path.join(rootdir, "test2")
        test2_hash = self.createTestFile(test2_file, b"test2")

        os.mkdir(os.path.join(rootdir, "subdir1"))

        test3_file = os.path.join(rootdir, "subdir1", "test3")
        test3_hash = self.createTestFile(test3_file, b"test3")

        with DirectoryHash(rootdir, tmpdir) as dh:
            dh.add(test1_file)
            dh.add(test2_file)
            dh.add(test3_file)

        expected = {
            "SHA256SUMS": MatchesSetwise(
                Equals([test1_hash, "*test1"]),
                Equals([test2_hash, "*test2"]),
                Equals([test3_hash, "*subdir1/test3"]),
            ),
        }
        self.assertThat(self.fetchSums(rootdir), MatchesDict(expected))

    def test_basic_directory_add(self):
        tmpdir = self.makeTemporaryDirectory()
        rootdir = self.makeTemporaryDirectory()
        test1_file = os.path.join(rootdir, "test1")
        test1_hash = self.createTestFile(test1_file, b"test1 dir")

        test2_file = os.path.join(rootdir, "test2")
        test2_hash = self.createTestFile(test2_file, b"test2 dir")

        os.mkdir(os.path.join(rootdir, "subdir1"))

        test3_file = os.path.join(rootdir, "subdir1", "test3")
        test3_hash = self.createTestFile(test3_file, b"test3 dir")

        with DirectoryHash(rootdir, tmpdir) as dh:
            dh.add_dir(rootdir)

        expected = {
            "SHA256SUMS": MatchesSetwise(
                Equals([test1_hash, "*test1"]),
                Equals([test2_hash, "*test2"]),
                Equals([test3_hash, "*subdir1/test3"]),
            ),
        }
        self.assertThat(self.fetchSums(rootdir), MatchesDict(expected))


class TestArtifactoryPublishing(TestPublisherBase):
    """Test publishing to Artifactory."""

    def setUpArtifactory(self, repository_format):
        self.base_url = "https://foo.example.com/artifactory"
        self.pushConfig("artifactory", base_url=self.base_url)
        self.archive = self.factory.makeArchive(
            distribution=self.ubuntutest,
            publishing_method=ArchivePublishingMethod.ARTIFACTORY,
            repository_format=repository_format,
        )
        self.config = getPubConfig(self.archive)
        self.disk_pool = self.config.getDiskPool(self.logger)
        self.disk_pool.logger = self.logger
        self.artifactory = self.useFixture(
            FakeArtifactoryFixture(self.base_url, self.archive.name)
        )

    def test_publish_files(self):
        """The actual publishing of packages' files to Artifactory works."""
        self.setUpArtifactory(ArchiveRepositoryFormat.DEBIAN)
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, self.archive
        )
        pub_source = self.getPubSource(
            sourcename="foo",
            version="666",
            filecontent=b"Hello world",
            archive=self.archive,
        )

        publisher.A_publish(False)

        self.assertEqual({"breezy-autotest"}, publisher.dirty_suites)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)
        path = self.disk_pool.rootpath / "f" / "foo" / "foo_666.dsc"
        with path.open() as f:
            self.assertEqual(b"Hello world", f.read())

    def test_initialize_properties(self):
        """We set initial properties for newly-published files."""
        self.setUpArtifactory(ArchiveRepositoryFormat.DEBIAN)
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, self.archive
        )
        source = self.getPubSource(
            sourcename="hello",
            version="1.0",
            archive=self.archive,
            architecturehintlist="i386",
        )
        binary = self.getPubBinaries(
            binaryname="hello",
            version="1.0",
            archive=self.archive,
            pub_source=source,
        )[0]

        publisher.A_publish(False)
        publisher.C_updateArtifactoryProperties(False)

        source_path = self.disk_pool.rootpath / "h" / "hello" / "hello_1.0.dsc"
        self.assertEqual(
            {
                "deb.component": ["main"],
                "deb.distribution": ["breezy-autotest"],
                "deb.name": ["hello"],
                "deb.version": ["1.0"],
                "launchpad.release-id": [
                    "source:%d" % source.sourcepackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["debian/copyright"],
                "soss.type": ["source"],
            },
            source_path.properties,
        )
        binary_path = (
            self.disk_pool.rootpath
            / "h"
            / "hello"
            / ("hello_1.0_%s.deb" % binary.distroarchseries.architecturetag)
        )
        self.assertEqual(
            {
                "deb.architecture": [binary.distroarchseries.architecturetag],
                "deb.component": ["main"],
                "deb.distribution": ["breezy-autotest"],
                "launchpad.release-id": [
                    "binary:%d" % binary.binarypackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["/usr/share/doc/hello/copyright"],
                "soss.source_url": [
                    "%s/%s/pool/h/hello/hello_1.0.dsc"
                    % (self.base_url, self.archive.name)
                ],
                "soss.type": ["binary"],
            },
            binary_path.properties,
        )

    def test_update_properties(self):
        """We update properties when publishing contexts are updated."""
        self.setUpArtifactory(ArchiveRepositoryFormat.DEBIAN)
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, self.archive
        )
        source = self.getPubSource(
            sourcename="hello",
            version="1.0",
            archive=self.archive,
            architecturehintlist="i386",
        )
        # getPubSource adds a .dsc; add a .tar.xz too.
        source.sourcepackagerelease.addFile(
            self.addMockFile("hello_1.0.tar.xz", b"A tarball")
        )
        binary = self.getPubBinaries(
            binaryname="hello",
            version="1.0",
            archive=self.archive,
            pub_source=source,
        )[0]

        # Do an initial publication so that we have something to update.
        publisher.A_publish(False)
        publisher.C_updateArtifactoryProperties(False)

        dsc_path = self.disk_pool.rootpath / "h" / "hello" / "hello_1.0.dsc"
        self.assertEqual(
            ["breezy-autotest"], dsc_path.properties["deb.distribution"]
        )
        tar_path = self.disk_pool.rootpath / "h" / "hello" / "hello_1.0.tar.xz"
        self.assertEqual(
            ["breezy-autotest"], tar_path.properties["deb.distribution"]
        )
        binary_path = (
            self.disk_pool.rootpath
            / "h"
            / "hello"
            / ("hello_1.0_%s.deb" % binary.distroarchseries.architecturetag)
        )
        self.assertEqual(
            ["breezy-autotest"], binary_path.properties["deb.distribution"]
        )

        # Copy the source and binary to another publishing context, and
        # ensure that we update their Artifactory properties accordingly.
        source.copyTo(
            archive=self.archive,
            distroseries=self.archive.distribution["hoary-test"],
            pocket=PackagePublishingPocket.RELEASE,
        )
        binary.copyTo(
            archive=self.archive,
            distroseries=self.archive.distribution["hoary-test"],
            pocket=PackagePublishingPocket.RELEASE,
        )

        publisher.A_publish(False)
        publisher.C_updateArtifactoryProperties(False)

        self.assertEqual(
            {
                "deb.component": ["main"],
                "deb.distribution": ["breezy-autotest", "hoary-test"],
                "deb.name": ["hello"],
                "deb.version": ["1.0"],
                "launchpad.release-id": [
                    "source:%d" % source.sourcepackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["debian/copyright"],
                "soss.type": ["source"],
            },
            dsc_path.properties,
        )
        self.assertEqual(
            {
                "deb.component": ["main"],
                "deb.distribution": ["breezy-autotest", "hoary-test"],
                "deb.name": ["hello"],
                "deb.version": ["1.0"],
                "launchpad.release-id": [
                    "source:%d" % source.sourcepackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["debian/copyright"],
                "soss.type": ["source"],
            },
            tar_path.properties,
        )
        self.assertEqual(
            {
                "deb.architecture": [binary.distroarchseries.architecturetag],
                "deb.component": ["main"],
                "deb.distribution": ["breezy-autotest", "hoary-test"],
                "launchpad.release-id": [
                    "binary:%d" % binary.binarypackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["/usr/share/doc/hello/copyright"],
                "soss.source_url": [
                    "%s/%s/pool/h/hello/hello_1.0.dsc"
                    % (self.base_url, self.archive.name)
                ],
                "soss.type": ["binary"],
            },
            binary_path.properties,
        )

    def test_update_properties_shared_orig(self):
        """An .orig from a previous Debian revision doesn't confuse matters."""
        self.setUpArtifactory(ArchiveRepositoryFormat.DEBIAN)
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, self.archive
        )
        orig_tar_file = self.addMockFile(
            "hello_1.0.orig.tar.xz", b"An orig tarball"
        )
        source_1 = self.getPubSource(
            sourcename="hello",
            version="1.0-1",
            archive=self.archive,
        )
        source_1.sourcepackagerelease.addFile(
            self.addMockFile("hello_1.0-1.debian.tar.xz", b"A tarball")
        )
        source_1.sourcepackagerelease.addFile(orig_tar_file)
        transaction.commit()

        publisher.A_publish(False)
        publisher.C_updateArtifactoryProperties(False)

        # Publish 1.0-2, remove 1.0-1, and simulate process-death-row.
        source_2 = self.getPubSource(
            sourcename="hello",
            version="1.0-2",
            archive=self.archive,
        )
        source_2.sourcepackagerelease.addFile(
            self.addMockFile("hello_1.0-2.debian.tar.xz", b"A tarball")
        )
        source_2.sourcepackagerelease.addFile(orig_tar_file)
        source_1.requestDeletion(self.archive.owner)
        for pub_file in source_1.files:
            if not pub_file.libraryfile.filename.endswith(".orig.tar.xz"):
                self.disk_pool.removeFile("main", "hello", "1.0-1", pub_file)
        transaction.commit()

        publisher.A_publish(False)
        publisher.C_updateArtifactoryProperties(False)

        for name in "hello_1.0-1.dsc", "hello_1.0-1.debian.tar.xz":
            self.assertFalse(
                (self.disk_pool.rootpath / "h" / "hello" / name).exists()
            )
        for spph, name in (
            (source_2, "hello_1.0-2.dsc"),
            (source_2, "hello_1.0-2.debian.tar.xz"),
            # The shared .orig file ends up still having a
            # launchpad.release-id property associated with version 1.0-1,
            # because ArtifactoryPoolEntry.updateProperties always keeps the
            # release-id from the properties passed to it from Artifactory.
            # This isn't ideal, but is a relatively minor problem, so allow
            # it for now.
            (source_1, "hello_1.0.orig.tar.xz"),
        ):
            self.assertEqual(
                {
                    "deb.component": ["main"],
                    "deb.distribution": ["breezy-autotest"],
                    "deb.name": ["hello"],
                    "deb.version": ["1.0-2"],
                    "launchpad.release-id": [
                        "source:%d" % spph.sourcepackagerelease_id
                    ],
                    "launchpad.source-name": ["hello"],
                    "launchpad.source-version": ["1.0-2"],
                    "soss.license": ["debian/copyright"],
                    "soss.type": ["source"],
                },
                (self.disk_pool.rootpath / "h" / "hello" / name).properties,
            )

    def test_remove_properties(self):
        """We remove properties if a file is no longer published anywhere."""
        self.setUpArtifactory(ArchiveRepositoryFormat.DEBIAN)
        publisher = Publisher(
            self.logger, self.config, self.disk_pool, self.archive
        )
        source = self.getPubSource(
            sourcename="hello",
            version="1.0",
            archive=self.archive,
            architecturehintlist="i386",
        )
        binary = self.getPubBinaries(
            binaryname="hello",
            version="1.0",
            archive=self.archive,
            pub_source=source,
        )[0]

        # Do an initial publication so that we have something to update.
        publisher.A_publish(False)
        publisher.C_updateArtifactoryProperties(False)

        source_path = self.disk_pool.rootpath / "h" / "hello" / "hello_1.0.dsc"
        self.assertEqual(
            ["breezy-autotest"], source_path.properties["deb.distribution"]
        )
        binary_path = (
            self.disk_pool.rootpath
            / "h"
            / "hello"
            / ("hello_1.0_%s.deb" % binary.distroarchseries.architecturetag)
        )
        self.assertEqual(
            ["breezy-autotest"], binary_path.properties["deb.distribution"]
        )

        source.requestDeletion(self.archive.owner)
        binary.requestDeletion(self.archive.owner)

        publisher.A_publish(False)
        publisher.A2_markPocketsWithDeletionsDirty()
        publisher.C_updateArtifactoryProperties(False)

        # The artifacts are still present until process-death-row runs, but
        # they no longer have any properties that would cause them to be
        # included in indexes.
        self.assertEqual(
            {
                "deb.name": ["hello"],
                "deb.version": ["1.0"],
                "launchpad.release-id": [
                    "source:%d" % source.sourcepackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["debian/copyright"],
                "soss.type": ["source"],
            },
            source_path.properties,
        )
        self.assertEqual(
            {
                "launchpad.release-id": [
                    "binary:%d" % binary.binarypackagerelease_id
                ],
                "launchpad.source-name": ["hello"],
                "launchpad.source-version": ["1.0"],
                "soss.license": ["/usr/share/doc/hello/copyright"],
                "soss.source_url": [
                    "%s/%s/pool/h/hello/hello_1.0.dsc"
                    % (self.base_url, self.archive.name)
                ],
                "soss.type": ["binary"],
            },
            binary_path.properties,
        )


class TestPublisherDirtyPockets(TestPublisherBase):
    """Test the A2_markPocketsWithDeletionsDirty method."""

    def setUp(self):
        super().setUp()
        self.allowed_suites = []
        self.publisher = getPublisher(
            self.ubuntutest.main_archive, self.allowed_suites, self.logger
        )

    def test_exclude_spph_if_spr_published_elsewhere(self):
        # Exclude SPPHs where the SourcePackageRelease is also used by
        # other SPPHs that are published.

        spph_deleted = self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )

        spr = spph_deleted.sourcepackagerelease

        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=spr,
            distroseries=self.breezy_autotest,
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.PUBLISHED,
            archive=self.ubuntutest.main_archive,
        )

        self.publisher.A2_markPocketsWithDeletionsDirty()

        # breezy-autotest (RELEASE) should NOT be dirty
        # because the SPR is still published in UPDATES.
        self.assertNotIn("breezy-autotest", self.publisher.dirty_suites)

    def test_mark_dirty_if_spr_published_in_different_ppa(self):
        ppa_one = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, distribution=self.ubuntutest
        )
        ppa_two = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, distribution=self.ubuntutest
        )

        spph_published = self.getPubSource(
            archive=ppa_one,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )
        spr = spph_published.sourcepackagerelease

        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=spr,
            distroseries=self.breezy_autotest,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
            archive=ppa_two,
        )

        ppa_publisher = getPublisher(ppa_two, self.allowed_suites, self.logger)
        ppa_publisher.A2_markPocketsWithDeletionsDirty()

        # breezy-autotest should be dirty, since the published SPR is
        # only in a different PPA archive.
        self.assertIn("breezy-autotest", ppa_publisher.dirty_suites)

    def test_mark_dirty_if_bpr_published_in_different_ppa(self):
        ppa_one = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, distribution=self.ubuntutest
        )
        ppa_two = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA, distribution=self.ubuntutest
        )

        bpph_published = self.getPubBinaries(
            archive=ppa_one,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
        )[0]
        bpr = bpph_published.binarypackagerelease

        self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            distroarchseries=self.breezy_autotest_i386,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
            archive=ppa_two,
        )

        ppa_publisher = getPublisher(ppa_two, self.allowed_suites, self.logger)
        ppa_publisher.A2_markPocketsWithDeletionsDirty()

        # breezy-autotest should be dirty, since the published BPR is
        # only in a different PPA archive.
        self.assertIn("breezy-autotest", ppa_publisher.dirty_suites)

    def test_exclude_spph_if_binary_published(self):
        # Exclude SPPHs where the corresponding
        # binary packages are still published.

        spph_deleted = self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )

        spr = spph_deleted.sourcepackagerelease

        build = self.factory.makeBinaryPackageBuild(
            source_package_release=spr,
            distroarchseries=self.breezy_autotest_i386,
        )

        bpr = self.factory.makeBinaryPackageRelease(build=build)

        self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            distroarchseries=self.breezy_autotest_i386,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.PUBLISHED,
            archive=self.ubuntutest.main_archive,
        )

        self.publisher.A2_markPocketsWithDeletionsDirty()

        # breezy-autotest should NOT be dirty
        # because a binary from the source is still published.
        self.assertNotIn("breezy-autotest", self.publisher.dirty_suites)

    def test_exclude_bpph_if_bpr_published_elsewhere(self):
        # Exclude BPPHs where the BinaryPackageRelease is also used by
        # other BPPHs that are published.

        bpph_deleted = self.getPubBinaries(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )[0]

        bpr = bpph_deleted.binarypackagerelease

        self.factory.makeBinaryPackagePublishingHistory(
            binarypackagerelease=bpr,
            distroarchseries=self.breezy_autotest_i386,
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.PUBLISHED,
            archive=self.ubuntutest.main_archive,
        )

        self.publisher.A2_markPocketsWithDeletionsDirty()

        # breezy-autotest should NOT be dirty
        # because the BPR is still published in UPDATES.
        self.assertNotIn("breezy-autotest", self.publisher.dirty_suites)

    def test_dirty_if_no_dependencies(self):
        # Control test: If no dependencies, it SHOULD be dirty.

        self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )

        self.getPubBinaries(
            pocket=PackagePublishingPocket.UPDATES,
            status=PackagePublishingStatus.DELETED,
        )

        self.publisher.A2_markPocketsWithDeletionsDirty()

        self.assertIn("breezy-autotest", self.publisher.dirty_suites)
        self.assertIn("breezy-autotest-updates", self.publisher.dirty_suites)

    def test_dirty_if_both_spphs_deleted(self):
        # If two SPPHs in two different series
        # point to the same SourcePackageRelease,
        # but BOTH are deleted, BOTH suites are marked as dirty.

        spph1 = self.getPubSource(
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
        )
        spr = spph1.sourcepackagerelease

        hoary = self.ubuntutest["hoary-test"]
        self.factory.makeSourcePackagePublishingHistory(
            sourcepackagerelease=spr,
            distroseries=hoary,
            pocket=PackagePublishingPocket.RELEASE,
            status=PackagePublishingStatus.DELETED,
            archive=self.ubuntutest.main_archive,
        )

        self.publisher.A2_markPocketsWithDeletionsDirty()

        # Both suites should be dirty.
        self.assertIn("breezy-autotest", self.publisher.dirty_suites)
        self.assertIn("hoary-test", self.publisher.dirty_suites)


load_tests = load_tests_apply_scenarios
