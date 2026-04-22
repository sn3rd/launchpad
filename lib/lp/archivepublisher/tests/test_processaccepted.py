# Copyright 2010-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test process-accepted.py"""

import os
from optparse import OptionValueError

import transaction
from testtools.matchers import EndsWith, LessThan, MatchesListwise

from lp.archivepublisher.publishing import GLOBAL_PUBLISHER_LOCK
from lp.archivepublisher.scripts.processaccepted import ProcessAccepted
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.log.logger import BufferLogger
from lp.services.scripts.base import LOCK_PATH
from lp.soyuz.enums import (
    ArchivePurpose,
    PackagePublishingStatus,
    PackageUploadStatus,
)
from lp.soyuz.model.queue import PackageUpload
from lp.soyuz.tests.test_publishing import SoyuzTestPublisher
from lp.testing import StormStatementRecorder, TestCaseWithFactory
from lp.testing.dbuser import switch_dbuser
from lp.testing.layers import LaunchpadZopelessLayer


class TestProcessAccepted(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer
    dbuser = config.uploadqueue.dbuser

    def setUp(self):
        """Create the Soyuz test publisher."""
        TestCaseWithFactory.setUp(self)
        self.stp = SoyuzTestPublisher()
        self.stp.prepareBreezyAutotest()
        self.test_package_name = "accept-test"
        self.distro = self.factory.makeDistribution()

    def getScript(self, test_args=None):
        """Return a ProcessAccepted instance."""
        if test_args is None:
            test_args = []
        test_args.extend(["-d", self.distro.name])
        script = ProcessAccepted("process accepted", test_args=test_args)
        script.logger = BufferLogger()
        script.txn = self.layer.txn
        return script

    def createWaitingAcceptancePackage(
        self, distroseries, archive=None, sourcename=None
    ):
        """Create some pending publications."""
        if archive is None:
            archive = self.distro.main_archive
        if sourcename is None:
            sourcename = self.test_package_name
        return self.stp.getPubSource(
            archive=archive,
            sourcename=sourcename,
            distroseries=distroseries,
            spr_only=True,
        )

    def test_robustness(self):
        """Test that a broken package doesn't block the publication of other
        packages."""
        # Attempt to upload one source to a supported series.
        # The record is created first and then the status of the series
        # is changed from DEVELOPMENT to SUPPORTED, otherwise it's impossible
        # to create the record.
        distroseries = self.factory.makeDistroSeries(distribution=self.distro)
        # This creates a broken publication.
        self.createWaitingAcceptancePackage(
            distroseries=distroseries, sourcename="notaccepted"
        )
        distroseries.status = SeriesStatus.SUPPORTED
        # Also upload some other things.
        other_distroseries = self.factory.makeDistroSeries(
            distribution=self.distro
        )
        self.createWaitingAcceptancePackage(distroseries=other_distroseries)
        script = self.getScript([])
        switch_dbuser(self.dbuser)
        script.main()

        # The other source should be published now.
        published_main = self.distro.main_archive.getPublishedSources(
            name=self.test_package_name
        )
        self.assertEqual(published_main.count(), 1)

        # And an oops should be filed for the first.
        self.assertEqual(1, len(self.oopses))
        error_report = self.oopses[0]
        expected_error = "Failure processing queue_item"
        self.assertStartsWith(
            error_report["req_vars"]["error-explanation"], expected_error
        )

    def test_skips_obsolete_series(self):
        distroseries = self.factory.makeDistroSeries(distribution=self.distro)
        self.createWaitingAcceptancePackage(distroseries=distroseries)
        distroseries.status = SeriesStatus.OBSOLETE
        script = self.getScript([])
        switch_dbuser(self.dbuser)
        script.main()

        published_main = self.distro.main_archive.getPublishedSources(
            name=self.test_package_name
        )
        self.assertTrue(published_main.is_empty())

    def test_accept_copy_archives(self):
        """Test that publications in a copy archive are accepted properly."""
        # Upload some pending packages in a copy archive.
        distroseries = self.factory.makeDistroSeries(distribution=self.distro)
        copy_archive = self.factory.makeArchive(
            distribution=self.distro, purpose=ArchivePurpose.COPY
        )
        copy_archive.publish = True
        copy_source = self.createWaitingAcceptancePackage(
            archive=copy_archive, distroseries=distroseries
        )
        # Also upload some stuff in the main archive.
        self.createWaitingAcceptancePackage(distroseries=distroseries)

        # Before accepting, the package should not be published at all.
        published_copy = copy_archive.getPublishedSources(
            name=self.test_package_name
        )
        self.assertTrue(published_copy.is_empty())

        # Accept the packages.
        script = self.getScript(["--copy-archives"])
        switch_dbuser(self.dbuser)
        script.main()

        # Packages in main archive should not be accepted and published.
        published_main = self.distro.main_archive.getPublishedSources(
            name=self.test_package_name
        )
        self.assertTrue(published_main.is_empty())

        # Check the copy archive source was accepted.
        published_copy = copy_archive.getPublishedSources(
            name=self.test_package_name
        ).one()
        self.assertEqual(
            published_copy.status, PackagePublishingStatus.PENDING
        )
        self.assertEqual(copy_source, published_copy.sourcepackagerelease)

    def test_skips_non_publishing_copy_archives(self):
        # The script skips copy archives that have the publish flag disabled.
        distroseries = self.factory.makeDistroSeries(distribution=self.distro)
        copy_archive = self.factory.makeArchive(
            distribution=self.distro, purpose=ArchivePurpose.COPY
        )
        self.createWaitingAcceptancePackage(
            archive=copy_archive, distroseries=distroseries
        )
        published_copy = copy_archive.getPublishedSources(
            name=self.test_package_name
        )
        self.assertTrue(published_copy.is_empty())

        script = self.getScript(["--copy-archives"])
        switch_dbuser(self.dbuser)
        script.main()

        published_copy = copy_archive.getPublishedSources(
            name=self.test_package_name
        )
        self.assertTrue(published_copy.is_empty())

    def test_commits_after_each_item(self):
        # Test that the script commits after each item, not just at the end.
        uploads = [
            self.createWaitingAcceptancePackage(
                distroseries=self.factory.makeDistroSeries(
                    distribution=self.distro
                ),
                sourcename="source%d" % i,
            )
            for i in range(3)
        ]

        class UploadCheckingSynchronizer:
            commit_count = 0

            def beforeCompletion(inner_self, txn):
                self.done_count = len(
                    [
                        upload
                        for upload in uploads
                        if upload.package_upload.status
                        == PackageUploadStatus.DONE
                    ]
                )

            def afterCompletion(inner_self, txn):
                try:
                    if txn.status != "Committed":
                        return
                    inner_self.commit_count += 1
                    # We actually commit twice for each upload: once for the
                    # queue item itself, and again to close its bugs.
                    self.assertIn(
                        min(len(uploads) * 2, inner_self.commit_count),
                        (self.done_count * 2, (self.done_count * 2) - 1),
                    )
                finally:
                    self.done_count = None

        script = self.getScript([])
        switch_dbuser(self.dbuser)
        synch = UploadCheckingSynchronizer()
        transaction.manager.registerSynch(synch)
        self.addCleanup(transaction.manager.unregisterSynch, synch)
        script.main()
        self.assertThat(len(uploads), LessThan(synch.commit_count))

    def test_commits_work(self):
        upload = self.factory.makeSourcePackageUpload(
            distroseries=self.factory.makeDistroSeries(
                distribution=self.distro
            )
        )
        upload_id = upload.id
        self.getScript([]).main()
        self.layer.txn.abort()
        self.assertEqual(
            upload, IStore(PackageUpload).get(PackageUpload, upload_id)
        )

    def test_validateArguments_requires_no_distro_for_derived_run(self):
        ProcessAccepted(test_args=["--all-derived"]).validateArguments()
        # The test is that this does not raise an exception.
        pass

    def test_validateArguments_does_not_accept_distro_for_derived_run(self):
        distro = self.factory.makeDistribution()
        script = ProcessAccepted(
            test_args=["--all-derived", "-d", distro.name]
        )
        self.assertRaises(OptionValueError, script.validateArguments)

    def test_processes_in_batches(self):
        distroseries = self.factory.makeDistroSeries(distribution=self.distro)
        uploads = [
            self.createWaitingAcceptancePackage(
                distroseries=distroseries, sourcename="source%d" % i
            )
            for i in range(5)
        ]

        script = self.getScript([])
        script.batch_size = 2
        switch_dbuser(self.dbuser)
        with StormStatementRecorder() as recorder:
            script.main()

        for upload in uploads:
            published = self.distro.main_archive.getPublishedSources(
                name=upload.name
            ).one()
            self.assertEqual(PackagePublishingStatus.PENDING, published.status)
        # The script made three queries for PackageUploads, each of which
        # was limited to the batch size.
        self.assertThat(
            [
                stmt
                for stmt in recorder.statements
                if "SELECT DISTINCT PackageUpload" in stmt
            ],
            MatchesListwise(
                [
                    EndsWith("LIMIT 2"),
                    EndsWith("LIMIT 2"),
                    EndsWith("LIMIT 2"),
                ]
            ),
        )

    def test_countExclusiveOptions_is_zero_if_none_set(self):
        # If none of the exclusive options is set, countExclusiveOptions
        # counts zero.
        self.assertEqual(0, self.getScript().countExclusiveOptions())

    def test_countExclusiveOptions_counts_ppa(self):
        # countExclusiveOptions includes the "ppa" option.
        self.assertEqual(
            1, self.getScript(test_args=["--ppa"]).countExclusiveOptions()
        )

    def test_countExclusiveOptions_counts_copy_archives(self):
        # countExclusiveOptions includes the "copy-archive" option.
        self.assertEqual(
            1,
            self.getScript(
                test_args=["--copy-archives"]
            ).countExclusiveOptions(),
        )

    def test_countExclusiveOptions_counts_archive(self):
        # countExclusiveOptions includes the "archive" option.
        self.assertEqual(
            1, self.getScript(test_args=["--archive"]).countExclusiveOptions()
        )

    def test_lockfilename_option_overrides_default_lock(self):
        lockfilename = "foo.lock"
        script = self.getScript(test_args=["--lockfilename", lockfilename])
        self.assertEqual(
            script.lockfilepath, os.path.join(LOCK_PATH, lockfilename)
        )

    def test_default_lock(self):
        script = self.getScript()
        self.assertEqual(
            script.lockfilepath, os.path.join(LOCK_PATH, GLOBAL_PUBLISHER_LOCK)
        )

    def test_getTargetArchives_ignores_excluded_archives_for_ppa(self):
        # If the selected exclusive option is "ppa," getTargetArchives
        # leaves out excluded PPAs.
        distroseries = self.factory.makeDistroSeries(distribution=self.distro)

        ppa = self.factory.makeArchive(self.distro, purpose=ArchivePurpose.PPA)
        excluded_ppa_1 = self.factory.makeArchive(
            self.distro, purpose=ArchivePurpose.PPA
        )
        excluded_ppa_2 = self.factory.makeArchive(
            self.distro, purpose=ArchivePurpose.PPA, private=True
        )

        for archive in [ppa, excluded_ppa_1, excluded_ppa_2]:
            self.createWaitingAcceptancePackage(
                archive=archive, distroseries=distroseries
            )
        script = self.getScript(
            test_args=[
                "--ppa",
                "--exclude",
                excluded_ppa_1.reference,
                "--exclude",
                excluded_ppa_2.reference,
            ],
        )
        self.assertContentEqual([ppa], script.getTargetArchives(self.distro))

    def test_getTargetArchives_gets_specific_archives(self):
        # If the selected exclusive option is "archive,"
        # getTargetArchives looks for the specified archives.

        distroseries = self.factory.makeDistroSeries(distribution=self.distro)

        ppa1 = self.factory.makeArchive(
            self.distro, purpose=ArchivePurpose.PPA
        )
        ppa2 = self.factory.makeArchive(
            self.distro, purpose=ArchivePurpose.PPA, private=True
        )
        ppa3 = self.factory.makeArchive(
            self.distro, purpose=ArchivePurpose.PPA
        )
        ppa4 = self.factory.makeArchive(
            self.distro, purpose=ArchivePurpose.PPA, private=True
        )

        # create another random archive in the same distro
        self.factory.makeArchive(self.distro, purpose=ArchivePurpose.PPA)

        for archive in [ppa1, ppa2, ppa3, ppa4]:
            self.createWaitingAcceptancePackage(
                archive=archive, distroseries=distroseries
            )

        script = self.getScript(
            test_args=[
                "--archive",
                ppa1.reference,
                "--archive",
                ppa2.reference,
            ],
        )
        self.assertContentEqual(
            [ppa1, ppa2], script.getTargetArchives(self.distro)
        )
