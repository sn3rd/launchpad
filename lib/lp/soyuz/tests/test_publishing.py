# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test native publication workflow for Soyuz. """

import hashlib
import io
import operator
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest import mock

import transaction
from storm.store import Store
from testtools.matchers import Equals
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NotFoundError
from lp.archivepublisher.artifactory import ArtifactoryPool
from lp.archivepublisher.config import getPubConfig
from lp.archivepublisher.diskpool import DiskPool
from lp.archivepublisher.indices import (
    build_binary_stanza_fields,
    build_source_stanza_fields,
)
from lp.archivepublisher.tests.artifactory_fixture import (
    FakeArtifactoryFixture,
)
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.registry.interfaces.distribution import IDistributionSet
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackage import (
    SourcePackageType,
    SourcePackageUrgency,
)
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.services.channels import channel_string_to_list
from lp.services.config import config
from lp.services.database.constants import UTC_NOW
from lp.services.database.interfaces import IStore
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.log.logger import BufferLogger, DevNullLogger
from lp.soyuz.enums import (
    ArchivePublishingMethod,
    ArchivePurpose,
    ArchiveRepositoryFormat,
    BinaryPackageFormat,
    DistroArchSeriesFilterSense,
    PackageUploadStatus,
)
from lp.soyuz.interfaces.binarypackagename import IBinaryPackageNameSet
from lp.soyuz.interfaces.component import IComponentSet
from lp.soyuz.interfaces.publishing import (
    DeletionError,
    IBinaryPackagePublishingHistory,
    IPublishingSet,
    OverrideError,
    PackagePublishingPriority,
    PackagePublishingStatus,
    active_publishing_status,
)
from lp.soyuz.interfaces.section import ISectionSet
from lp.soyuz.model.distributionsourcepackagecache import (
    DistributionSourcePackageCache,
)
from lp.soyuz.model.distroseriesdifferencejob import find_waiting_jobs
from lp.soyuz.model.distroseriespackagecache import DistroSeriesPackageCache
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)
from lp.testing import (
    StormStatementRecorder,
    TestCaseWithFactory,
    login_admin,
    person_logged_in,
    record_two_runs,
)
from lp.testing.dbuser import dbuser, lp_dbuser, switch_dbuser
from lp.testing.factory import LaunchpadObjectFactory
from lp.testing.layers import (
    DatabaseFunctionalLayer,
    LaunchpadFunctionalLayer,
    LaunchpadZopelessLayer,
    ZopelessDatabaseLayer,
)
from lp.testing.matchers import HasQueryCount


class SoyuzTestPublisher:
    """Helper class able to publish coherent source and binaries in Soyuz."""

    def __init__(self):
        self.factory = LaunchpadObjectFactory()
        self.default_package_name = "foo"

    def setUpDefaultDistroSeries(self, distroseries=None):
        """Set up a distroseries that will be used by default.

        This distro series is used to publish packages in, if you don't
        specify any when using the publishing methods.

        It also sets up a person that can act as the default uploader,
        and makes sure that the default package name exists in the
        database.

        :param distroseries: The `IDistroSeries` to use as default. If
            it's None, one will be created.
        :return: The `IDistroSeries` that got set as default.
        """
        if distroseries is None:
            distroseries = self.factory.makeDistroSeries()
        self.distroseries = distroseries
        # Set up a person that has a GPG key.
        self.person = getUtility(IPersonSet).getByName("name16")
        # Make sure the name exists in the database, to make it easier
        # to get packages from distributions and distro series.
        name_set = getUtility(ISourcePackageNameSet)
        name_set.getOrCreateByName(self.default_package_name)
        return self.distroseries

    def prepareBreezyAutotest(self):
        """Prepare ubuntutest/breezy-autotest for publications.

        It's also called during the normal test-case setUp.
        """
        self.ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        self.breezy_autotest = self.ubuntutest["breezy-autotest"]
        self.setUpDefaultDistroSeries(self.breezy_autotest)
        # Only create the DistroArchSeries needed if they do not exist yet.
        # This makes it easier to experiment at the python command line
        # (using "make harness").
        try:
            self.breezy_autotest_i386 = self.breezy_autotest["i386"]
        except NotFoundError:
            self.breezy_autotest_i386 = self.breezy_autotest.newArch(
                "i386",
                getUtility(IProcessorSet).getByName("386"),
                False,
                self.person,
            )
        try:
            self.breezy_autotest_hppa = self.breezy_autotest["hppa"]
        except NotFoundError:
            self.breezy_autotest_hppa = self.breezy_autotest.newArch(
                "hppa",
                getUtility(IProcessorSet).getByName("hppa"),
                False,
                self.person,
            )
        self.breezy_autotest.nominatedarchindep = self.breezy_autotest_i386
        fake_chroot = self.addMockFile("fake_chroot.tar.gz")
        self.breezy_autotest_i386.addOrUpdateChroot(fake_chroot)
        self.breezy_autotest_hppa.addOrUpdateChroot(fake_chroot)

    def addFakeChroots(self, distroseries=None, db_only=False):
        """Add fake chroots for all the architectures in distroseries."""
        if distroseries is None:
            distroseries = self.distroseries
        if db_only:
            fake_chroot = self.factory.makeLibraryFileAlias(
                filename="fake_chroot.tar.gz", db_only=True
            )
        else:
            fake_chroot = self.addMockFile("fake_chroot.tar.gz")
        for arch in distroseries.architectures:
            arch.addOrUpdateChroot(fake_chroot)

    def regetBreezyAutotest(self):
        self.ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
        self.breezy_autotest = self.ubuntutest["breezy-autotest"]
        self.person = getUtility(IPersonSet).getByName("name16")
        self.breezy_autotest_i386 = self.breezy_autotest["i386"]
        self.breezy_autotest_hppa = self.breezy_autotest["hppa"]

    def addMockFile(self, filename, filecontent=b"nothing", restricted=False):
        """Add a mock file in Librarian.

        Returns a ILibraryFileAlias corresponding to the file uploaded.
        """
        library_file = getUtility(ILibraryFileAliasSet).create(
            filename,
            len(filecontent),
            io.BytesIO(filecontent),
            "application/text",
            restricted=restricted,
        )
        return library_file

    def addPackageUpload(
        self,
        archive,
        distroseries,
        pocket=PackagePublishingPocket.RELEASE,
        changes_file_name="foo_666_source.changes",
        changes_file_content=b"fake changes file content",
        upload_status=PackageUploadStatus.DONE,
    ):
        signing_key = self.person.gpg_keys[0]
        package_upload = distroseries.createQueueEntry(
            pocket,
            archive,
            changes_file_name,
            changes_file_content,
            signing_key=signing_key,
        )

        status_to_method = {
            PackageUploadStatus.DONE: "setDone",
            PackageUploadStatus.ACCEPTED: "setAccepted",
        }
        naked_package_upload = removeSecurityProxy(package_upload)
        method = getattr(naked_package_upload, status_to_method[upload_status])
        method()

        return package_upload

    def getPubSource(
        self,
        sourcename=None,
        version=None,
        component="main",
        filename=None,
        section="base",
        filecontent=b"I do not care about sources.",
        changes_file_content=b"Fake: fake changes file content",
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        urgency=SourcePackageUrgency.LOW,
        scheduleddeletiondate=None,
        dateremoved=None,
        distroseries=None,
        archive=None,
        builddepends=None,
        builddependsindep=None,
        architecturehintlist="all",
        dsc_standards_version="3.6.2",
        dsc_format="1.0",
        dsc_binaries="foo-bin",
        build_conflicts=None,
        build_conflicts_indep=None,
        dsc_maintainer_rfc822="Foo Bar <foo@bar.com>",
        maintainer=None,
        creator=None,
        date_uploaded=UTC_NOW,
        spr_only=False,
        user_defined_fields=None,
        format=SourcePackageType.DPKG,
        channel=None,
    ):
        """Return a mock source publishing record.

        if spr_only is specified, the source is not published and the
        sourcepackagerelease object is returned instead.
        """
        if sourcename is None:
            sourcename = self.default_package_name
        if version is None:
            version = "666"
        spn = getUtility(ISourcePackageNameSet).getOrCreateByName(sourcename)

        component = getUtility(IComponentSet)[component]
        section = getUtility(ISectionSet)[section]

        if distroseries is None:
            distroseries = self.distroseries
        if archive is None:
            archive = distroseries.main_archive
        if maintainer is None:
            maintainer = self.person
        if creator is None:
            creator = self.person

        spr = distroseries.createUploadedSourcePackageRelease(
            sourcepackagename=spn,
            format=format,
            maintainer=maintainer,
            creator=creator,
            component=component,
            section=section,
            urgency=urgency,
            version=version,
            builddepends=builddepends,
            builddependsindep=builddependsindep,
            build_conflicts=build_conflicts,
            build_conflicts_indep=build_conflicts_indep,
            architecturehintlist=architecturehintlist,
            changelog=None,
            changelog_entry=None,
            dsc=None,
            copyright="placeholder ...",
            dscsigningkey=self.person.gpg_keys[0],
            dsc_maintainer_rfc822=dsc_maintainer_rfc822,
            dsc_standards_version=dsc_standards_version,
            dsc_format=dsc_format,
            dsc_binaries=dsc_binaries,
            archive=archive,
            dateuploaded=date_uploaded,
            user_defined_fields=user_defined_fields,
        )

        changes_file_name = "%s_%s_source.changes" % (sourcename, version)
        if spr_only:
            upload_status = PackageUploadStatus.ACCEPTED
        else:
            upload_status = PackageUploadStatus.DONE
        package_upload = self.addPackageUpload(
            archive,
            distroseries,
            pocket,
            changes_file_name=changes_file_name,
            changes_file_content=changes_file_content,
            upload_status=upload_status,
        )
        naked_package_upload = removeSecurityProxy(package_upload)
        naked_package_upload.addSource(spr)

        if filename is None:
            filename = "%s_%s.dsc" % (sourcename, version)
        alias = self.addMockFile(
            filename, filecontent, restricted=archive.private
        )
        spr.addFile(alias)

        if spr_only:
            return spr

        if status == PackagePublishingStatus.PUBLISHED:
            datepublished = UTC_NOW
        else:
            datepublished = None
        if channel is not None:
            channel = channel_string_to_list(channel)

        spph = SourcePackagePublishingHistory(
            distroseries=distroseries,
            sourcepackagerelease=spr,
            sourcepackagename=spr.sourcepackagename,
            format=spr.format,
            component=spr.component,
            section=spr.section,
            status=status,
            datecreated=date_uploaded,
            dateremoved=dateremoved,
            datepublished=datepublished,
            scheduleddeletiondate=scheduleddeletiondate,
            pocket=pocket,
            archive=archive,
            creator=creator,
            channel=channel,
        )

        return spph

    def getPubBinaries(
        self,
        binaryname="foo-bin",
        summary="Foo app is great",
        description="Well ...\nit does nothing, though",
        shlibdep=None,
        depends=None,
        recommends=None,
        suggests=None,
        conflicts=None,
        replaces=None,
        provides=None,
        pre_depends=None,
        enhances=None,
        breaks=None,
        built_using=None,
        filecontent=b"bbbiiinnnaaarrryyy",
        changes_file_content=b"Fake: fake changes file",
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        format=BinaryPackageFormat.DEB,
        scheduleddeletiondate=None,
        dateremoved=None,
        distroseries=None,
        archive=None,
        pub_source=None,
        version=None,
        architecturespecific=False,
        builder=None,
        component="main",
        phased_update_percentage=None,
        with_debug=False,
        user_defined_fields=None,
        channel=None,
    ):
        """Return a list of binary publishing records."""
        if distroseries is None:
            distroseries = self.distroseries

        if archive is None:
            archive = distroseries.main_archive

        if pub_source is None:
            sourcename = "%s" % binaryname.split("-")[0]
            if architecturespecific:
                architecturehintlist = "any"
            else:
                architecturehintlist = "all"

            pub_source = self.getPubSource(
                sourcename=sourcename,
                status=status,
                pocket=pocket,
                archive=archive,
                distroseries=distroseries,
                version=version,
                architecturehintlist=architecturehintlist,
                component=component,
            )
        else:
            archive = pub_source.archive

        builds = pub_source.createMissingBuilds()
        published_binaries = []
        for build in builds:
            build.updateStatus(BuildStatus.FULLYBUILT, builder=builder)
            pub_binaries = []
            if with_debug:
                binarypackagerelease_ddeb = self.uploadBinaryForBuild(
                    build,
                    binaryname + "-dbgsym",
                    filecontent,
                    summary,
                    description,
                    shlibdep,
                    depends,
                    recommends,
                    suggests,
                    conflicts,
                    replaces,
                    provides,
                    pre_depends,
                    enhances,
                    breaks,
                    built_using,
                    BinaryPackageFormat.DDEB,
                    version=version,
                )
                pub_binaries += self.publishBinaryInArchive(
                    binarypackagerelease_ddeb,
                    archive,
                    status,
                    pocket,
                    scheduleddeletiondate,
                    dateremoved,
                    phased_update_percentage,
                    channel=channel,
                )
            else:
                binarypackagerelease_ddeb = None

            binarypackagerelease = self.uploadBinaryForBuild(
                build,
                binaryname,
                filecontent,
                summary,
                description,
                shlibdep,
                depends,
                recommends,
                suggests,
                conflicts,
                replaces,
                provides,
                pre_depends,
                enhances,
                breaks,
                built_using,
                format,
                binarypackagerelease_ddeb,
                version=version,
                user_defined_fields=user_defined_fields,
            )
            pub_binaries += self.publishBinaryInArchive(
                binarypackagerelease,
                archive,
                status,
                pocket,
                scheduleddeletiondate,
                dateremoved,
                phased_update_percentage,
                channel=channel,
            )
            published_binaries.extend(pub_binaries)
            package_upload = self.addPackageUpload(
                archive,
                distroseries,
                pocket,
                changes_file_content=changes_file_content,
                changes_file_name="%s_%s_%s.changes"
                % (binaryname, binarypackagerelease.version, build.arch_tag),
            )
            package_upload.addBuild(build)

        return sorted(
            published_binaries, key=operator.attrgetter("id"), reverse=True
        )

    def uploadBinaryForBuild(
        self,
        build,
        binaryname,
        filecontent=b"anything",
        summary="summary",
        description="description",
        shlibdep=None,
        depends=None,
        recommends=None,
        suggests=None,
        conflicts=None,
        replaces=None,
        provides=None,
        pre_depends=None,
        enhances=None,
        breaks=None,
        built_using=None,
        format=BinaryPackageFormat.DEB,
        debug_package=None,
        user_defined_fields=None,
        homepage=None,
        version=None,
    ):
        """Return the corresponding `BinaryPackageRelease`."""
        sourcepackagerelease = build.source_package_release
        distroarchseries = build.distro_arch_series
        architecturespecific = (
            not sourcepackagerelease.architecturehintlist == "all"
        )

        binarypackagename = getUtility(
            IBinaryPackageNameSet
        ).getOrCreateByName(binaryname)

        if version is None:
            version = sourcepackagerelease.version

        binarypackagerelease = build.createBinaryPackageRelease(
            version=version,
            component=sourcepackagerelease.component,
            section=sourcepackagerelease.section,
            binarypackagename=binarypackagename,
            summary=summary,
            description=description,
            shlibdeps=shlibdep,
            depends=depends,
            recommends=recommends,
            suggests=suggests,
            conflicts=conflicts,
            replaces=replaces,
            provides=provides,
            pre_depends=pre_depends,
            enhances=enhances,
            breaks=breaks,
            built_using=built_using,
            essential=False,
            installedsize=100,
            architecturespecific=architecturespecific,
            binpackageformat=format,
            priority=PackagePublishingPriority.STANDARD,
            debug_package=debug_package,
            user_defined_fields=user_defined_fields,
            homepage=homepage,
        )

        # Create the corresponding binary file.
        if architecturespecific:
            filearchtag = distroarchseries.architecturetag
        else:
            filearchtag = "all"
        filename = "%s_%s_%s.%s" % (
            binaryname,
            sourcepackagerelease.version,
            filearchtag,
            format.name.lower(),
        )
        alias = self.addMockFile(
            filename, filecontent=filecontent, restricted=build.archive.private
        )
        binarypackagerelease.addFile(alias)

        # Adjust the build record in way it looks complete.
        date_finished = datetime(2008, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
        date_started = date_finished - timedelta(minutes=5)
        build.updateStatus(
            BuildStatus.BUILDING,
            date_started=date_started,
            force_invalid_transition=True,
        )
        build.updateStatus(BuildStatus.FULLYBUILT, date_finished=date_finished)
        buildlog_filename = "buildlog_%s-%s-%s.%s_%s_%s.txt.gz" % (
            build.distribution.name,
            build.distro_series.name,
            build.distro_arch_series.architecturetag,
            build.source_package_release.name,
            build.source_package_release.version,
            build.status.name,
        )
        if not build.log:
            build.setLog(
                self.addMockFile(
                    buildlog_filename,
                    filecontent=b"Built!",
                    restricted=build.archive.private,
                )
            )

        return binarypackagerelease

    def publishBinaryInArchive(
        self,
        binarypackagerelease,
        archive,
        status=PackagePublishingStatus.PENDING,
        pocket=PackagePublishingPocket.RELEASE,
        scheduleddeletiondate=None,
        dateremoved=None,
        phased_update_percentage=None,
        channel=None,
    ):
        """Return the corresponding BinaryPackagePublishingHistory."""
        distroarchseries = binarypackagerelease.build.distro_arch_series

        # Publish the binary.
        if binarypackagerelease.architecturespecific:
            archs = [distroarchseries]
        else:
            archs = distroarchseries.distroseries.architectures
        if channel is not None:
            channel = channel_string_to_list(channel)

        pub_binaries = []
        for arch in archs:
            pub = BinaryPackagePublishingHistory(
                distroarchseries=arch,
                binarypackagerelease=binarypackagerelease,
                binarypackagename=binarypackagerelease.binarypackagename,
                binarypackageformat=binarypackagerelease.binpackageformat,
                component=binarypackagerelease.component,
                section=binarypackagerelease.section,
                priority=binarypackagerelease.priority,
                status=status,
                scheduleddeletiondate=scheduleddeletiondate,
                dateremoved=dateremoved,
                datecreated=UTC_NOW,
                pocket=pocket,
                archive=archive,
                phased_update_percentage=phased_update_percentage,
                channel=channel,
                sourcepackagename=(
                    binarypackagerelease.build.source_package_name
                ),
            )
            if status == PackagePublishingStatus.PUBLISHED:
                pub.datepublished = UTC_NOW
            pub_binaries.append(pub)

        return pub_binaries

    def _findChangesFile(self, top, name_fragment):
        """File with given name fragment in directory tree starting at top."""
        for root, _, files in os.walk(top, topdown=False):
            for name in files:
                if name.endswith(".changes") and name.find(name_fragment) > -1:
                    return os.path.join(root, name)
        return None

    def createSource(
        self, archive, sourcename, version, distroseries=None, new_version=None
    ):
        """Create source with meaningful '.changes' file."""
        top = "lib/lp/archiveuploader/tests/data/suite"
        name_fragment = "%s_%s" % (sourcename, version)
        changesfile_path = self._findChangesFile(top, name_fragment)

        source = None

        if changesfile_path is not None:
            if new_version is None:
                new_version = version
            changesfile_content = ""
            with open(changesfile_path, "rb") as handle:
                changesfile_content = handle.read()

            source = self.getPubSource(
                sourcename=sourcename,
                archive=archive,
                version=new_version,
                changes_file_content=changesfile_content,
                distroseries=distroseries,
            )

        return source

    def makeSourcePackageSummaryData(self, source_pub=None):
        """Make test data for SourcePackage.summary.

        The distroseries that is returned from this method needs to be
        passed into updatePackageCache() so that SourcePackage.summary can
        be populated.
        """
        if source_pub is None:
            distribution = self.factory.makeDistribution(
                name="youbuntu",
                displayname="Youbuntu",
                owner=self.factory.makePerson(email="owner@youbuntu.com"),
            )
            distroseries = self.factory.makeDistroSeries(
                name="busy", distribution=distribution
            )
            source_package_name = self.factory.makeSourcePackageName(
                name="bonkers"
            )
            self.factory.makeSourcePackage(
                sourcepackagename=source_package_name,
                distroseries=distroseries,
            )
            component = self.factory.makeComponent("multiverse")
            source_pub = self.factory.makeSourcePackagePublishingHistory(
                sourcepackagename=source_package_name,
                distroseries=distroseries,
                component=component,
            )

        das = self.factory.makeDistroArchSeries(
            distroseries=source_pub.distroseries
        )

        for name in ("flubber-bin", "flubber-lib"):
            binary_package_name = self.factory.makeBinaryPackageName(name)
            build = self.factory.makeBinaryPackageBuild(
                source_package_release=source_pub.sourcepackagerelease,
                archive=self.factory.makeArchive(),
                distroarchseries=das,
            )
            bpr = self.factory.makeBinaryPackageRelease(
                binarypackagename=binary_package_name,
                summary="summary for %s" % name,
                build=build,
                component=source_pub.component,
            )
            self.factory.makeBinaryPackagePublishingHistory(
                binarypackagerelease=bpr, distroarchseries=das
            )
        return dict(
            distroseries=source_pub.distroseries,
            source_package=source_pub.meta_sourcepackage,
        )

    def updatePackageCache(self, distroseries):
        with dbuser(config.statistician.dbuser):
            DistributionSourcePackageCache.updateAll(
                distroseries.distribution,
                archive=distroseries.main_archive,
                ztm=transaction,
                log=DevNullLogger(),
            )
            DistroSeriesPackageCache.updateAll(
                distroseries,
                archive=distroseries.main_archive,
                ztm=transaction,
                log=DevNullLogger(),
            )


class TestNativePublishingBase(TestCaseWithFactory, SoyuzTestPublisher):
    layer = LaunchpadZopelessLayer
    dbuser = config.archivepublisher.dbuser

    def __init__(self, methodName="runTest"):
        super().__init__(methodName=methodName)
        SoyuzTestPublisher.__init__(self)

    def setUp(self):
        """Setup a pool dir, the librarian, and instantiate the DiskPool."""
        super().setUp()
        switch_dbuser(config.archivepublisher.dbuser)
        self.prepareBreezyAutotest()
        self.config = getPubConfig(self.ubuntutest.main_archive)
        self.config.setupArchiveDirs()
        self.pool_dir = self.config.poolroot
        self.temp_dir = self.config.temproot
        self.logger = DevNullLogger()
        self.disk_pool = self.config.getDiskPool(self.logger)
        self.disk_pool.logger = self.logger

    def tearDown(self):
        """Tear down blows the pool dirs away."""
        super().tearDown()
        for root in (
            self.config.distroroot,
            config.personalpackagearchive.root,
            config.personalpackagearchive.private_root,
        ):
            if root is not None and os.path.exists(root):
                shutil.rmtree(root)

    def getPubSource(self, *args, **kwargs):
        """Overrides `SoyuzTestPublisher.getPubSource`.

        Commits the transaction before returning, this way the rest of
        the test will immediately notice the just-created records.
        """
        source = SoyuzTestPublisher.getPubSource(self, *args, **kwargs)
        self.layer.commit()
        return source

    def getPubBinaries(self, *args, **kwargs):
        """Overrides `SoyuzTestPublisher.getPubBinaries`.

        Commits the transaction before returning, this way the rest of
        the test will immediately notice the just-created records.
        """
        binaries = SoyuzTestPublisher.getPubBinaries(self, *args, **kwargs)
        self.layer.commit()
        return binaries

    def checkPublication(self, pub, status):
        """Assert the publication has the given status."""
        self.assertEqual(
            status,
            pub.status,
            "%s is not %s (%s)"
            % (pub.displayname, status.name, pub.status.name),
        )

    def checkPublications(self, pubs, status):
        """Assert the given publications have the given status.

        See `checkPublication`.
        """
        for pub in pubs:
            self.checkPublication(pub, status)

    def checkPastDate(self, date, lag=None):
        """Assert given date is older than 'now'.

        Optionally the user can pass a 'lag' which will be added to 'now'
        before comparing.
        """
        limit = datetime.now(timezone.utc)
        if lag is not None:
            limit = limit + lag
        self.assertTrue(date < limit, "%s >= %s" % (date, limit))

    def checkSuperseded(self, pubs, supersededby=None):
        self.checkPublications(pubs, PackagePublishingStatus.SUPERSEDED)
        for pub in pubs:
            self.checkPastDate(pub.datesuperseded)
            if supersededby is not None:
                if isinstance(pub, BinaryPackagePublishingHistory):
                    dominant = supersededby.binarypackagerelease.build
                else:
                    dominant = supersededby.sourcepackagerelease
                self.assertEqual(dominant, pub.supersededby)
            else:
                self.assertIsNone(pub.supersededby)


class TestNativePublishing(TestNativePublishingBase):
    def test_publish_source(self):
        # Source publications result in a PUBLISHED publishing record and
        # the corresponding files are dumped in the disk pool/.
        pub_source = self.getPubSource(filecontent=b"Hello world")
        pub_source.publish(self.disk_pool, self.logger)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_source.status)
        pool_path = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        with open(pool_path) as pool_file:
            self.assertEqual(pool_file.read().strip(), "Hello world")

    @mock.patch.object(ArtifactoryPool, "addFile")
    def test_publisher_skips_conda_source_packages(self, mock):
        root_url = "https://foo.example.com/artifactory/repository"
        archive = self.factory.makeArchive(
            purpose=ArchivePurpose.PPA,
            repository_format=ArchiveRepositoryFormat.CONDA,
        )
        pool = ArtifactoryPool(archive, root_url, BufferLogger())
        # getPubSource returns a SourcePackagePublishingHistory object
        pub_source = self.getPubSource(
            filecontent=b"Hello world",
            archive=archive,
            format=SourcePackageType.CI_BUILD,
            user_defined_fields=[("bogus_field", "instead_of_subdir")],
        )
        pub_source.publish(pool, self.logger)
        self.assertFalse(mock.called)

    def test_publish_binaries(self):
        # Binary publications result in a PUBLISHED publishing record and
        # the corresponding files are dumped in the disk pool/.
        pub_binary = self.getPubBinaries(filecontent=b"Hello world")[0]
        pub_binary.publish(self.disk_pool, self.logger)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, pub_binary.status)
        pool_path = "%s/main/f/foo/foo-bin_666_all.deb" % self.pool_dir
        with open(pool_path) as pool_file:
            self.assertEqual(pool_file.read().strip(), "Hello world")

    def test_publish_isolated_binaries(self):
        # Some binary publications have no associated source publication
        # (e.g. Python wheels in an archive published using Artifactory).
        # In these cases, the binary package name/version is passed to the
        # pool.
        base_url = "https://foo.example.com/artifactory"
        self.pushConfig("artifactory", base_url=base_url)
        archive = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            publishing_method=ArchivePublishingMethod.ARTIFACTORY,
            repository_format=ArchiveRepositoryFormat.PYTHON,
        )
        config = getPubConfig(archive)
        disk_pool = config.getDiskPool(self.logger)
        disk_pool.logger = self.logger
        self.useFixture(FakeArtifactoryFixture(base_url, archive.name))
        with lp_dbuser():
            ci_build = self.factory.makeCIBuild(
                distro_arch_series=self.distroseries.architectures[0]
            )
            bpn = self.factory.makeBinaryPackageName(name="foo")
            bpr = self.factory.makeBinaryPackageRelease(
                binarypackagename=bpn,
                version="0.1",
                ci_build=ci_build,
                binpackageformat=BinaryPackageFormat.WHL,
            )
            lfa = self.addMockFile("foo-0.1.whl", filecontent=b"Hello world")
            bpr.addFile(lfa)
            bpph = self.factory.makeBinaryPackagePublishingHistory(
                binarypackagerelease=bpr,
                archive=archive,
                distroarchseries=self.distroseries.architectures[0],
                pocket=PackagePublishingPocket.RELEASE,
                channel="stable",
            )
        bpph.publish(disk_pool, self.logger)
        self.assertEqual(PackagePublishingStatus.PUBLISHED, bpph.status)
        pool_path = disk_pool.rootpath / "foo" / "0.1" / "foo-0.1.whl"
        with pool_path.open() as pool_file:
            self.assertEqual(b"Hello world", pool_file.read())

    def test_publish_ddeb_when_disabled_is_noop(self):
        # Publishing a DDEB publication when
        # Archive.publish_debug_symbols is false just sets PUBLISHED,
        # without a file in the pool.
        pubs = self.getPubBinaries(
            binaryname="dbg", filecontent=b"Hello world", with_debug=True
        )

        def publish_everything():
            existence_map = {}
            for pub in pubs:
                pub.publish(self.disk_pool, self.logger)
                self.assertEqual(PackagePublishingStatus.PUBLISHED, pub.status)
                filename = pub.files[0].libraryfile.filename
                path = "%s/main/d/dbg/%s" % (self.pool_dir, filename)
                existence_map[filename] = os.path.exists(path)
            return existence_map

        self.assertEqual(
            {"dbg_666_all.deb": True, "dbg-dbgsym_666_all.ddeb": False},
            publish_everything(),
        )

        pubs[0].archive.publish_debug_symbols = True

        self.assertEqual(
            {"dbg_666_all.deb": True, "dbg-dbgsym_666_all.ddeb": True},
            publish_everything(),
        )

    def testPublishingOverwriteFileInPool(self):
        """Test if publishOne refuses to overwrite a file in pool.

        Check if it also keeps the original file content.
        It's done by publishing 'foo' by-hand and ensuring it
        has a special content, then publish 'foo' again, via publisher,
        and finally check one of the 'foo' files content.
        """
        foo_path = os.path.join(self.pool_dir, "main", "f", "foo")
        os.makedirs(foo_path)
        foo_dsc_path = os.path.join(foo_path, "foo_666.dsc")
        with open(foo_dsc_path, "w") as foo_dsc:
            foo_dsc.write("Hello world")

        pub_source = self.getPubSource(filecontent=b"Something")
        pub_source.publish(self.disk_pool, self.logger)

        # An oops should be filed for the error, but we don't include the
        # SQL timeline; it may be very large and tells us nothing that we
        # can't get from the error message.
        self.assertEqual("PoolFileOverwriteError", self.oopses[0]["type"])
        self.assertEqual([], self.oopses[0]["timeline"])

        self.layer.commit()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PENDING)
        with open(foo_dsc_path) as foo_dsc:
            self.assertEqual(foo_dsc.read().strip(), "Hello world")

    def testPublishingDifferentContents(self):
        """Test if publishOne refuses to overwrite its own publication."""
        pub_source = self.getPubSource(filecontent=b"foo is happy")
        pub_source.publish(self.disk_pool, self.logger)
        self.layer.commit()

        foo_name = "%s/main/f/foo/foo_666.dsc" % self.pool_dir
        IStore(pub_source).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        with open(foo_name) as foo:
            self.assertEqual(foo.read().strip(), "foo is happy")

        # try to publish 'foo' again with a different content, it
        # raises internally and keeps the files with the original
        # content.
        pub_source2 = self.getPubSource(filecontent=b"foo is depressing")
        pub_source2.publish(self.disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source2).flush()
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PENDING)
        with open(foo_name) as foo:
            self.assertEqual(foo.read().strip(), "foo is happy")

    def testPublishingAlreadyInPool(self):
        """Test if publishOne works if file is already in Pool.

        It should identify that the file has the same content and
        mark it as PUBLISHED.
        """
        pub_source = self.getPubSource(
            sourcename="bar", filecontent=b"bar is good"
        )
        pub_source.publish(self.disk_pool, self.logger)
        self.layer.commit()
        bar_name = "%s/main/b/bar/bar_666.dsc" % self.pool_dir
        with open(bar_name) as bar:
            self.assertEqual(bar.read().strip(), "bar is good")
        IStore(pub_source).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)

        pub_source2 = self.getPubSource(
            sourcename="bar", filecontent=b"bar is good"
        )
        pub_source2.publish(self.disk_pool, self.logger)
        self.layer.commit()
        IStore(pub_source2).flush()
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PUBLISHED)

    def testPublishingSymlink(self):
        """Test if publishOne moving publication between components.

        After check if the pool file contents as the same, it should
        create a symlink in the new pointing to the original file.
        """
        content = b"am I a file or a symbolic link ?"
        # publish sim.dsc in main and re-publish in universe
        pub_source = self.getPubSource(sourcename="sim", filecontent=content)
        pub_source2 = self.getPubSource(
            sourcename="sim", component="universe", filecontent=content
        )
        pub_source.publish(self.disk_pool, self.logger)
        pub_source2.publish(self.disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source).flush()
        IStore(pub_source2).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        self.assertEqual(pub_source2.status, PackagePublishingStatus.PUBLISHED)

        # check the resulted symbolic link
        sim_universe = "%s/universe/s/sim/sim_666.dsc" % self.pool_dir
        self.assertEqual(
            os.readlink(sim_universe), "../../../main/s/sim/sim_666.dsc"
        )

        # if the contexts don't match it raises, so the publication
        # remains pending.
        pub_source3 = self.getPubSource(
            sourcename="sim",
            component="restricted",
            filecontent=b"It is all my fault",
        )
        pub_source3.publish(self.disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source3).flush()
        self.assertEqual(pub_source3.status, PackagePublishingStatus.PENDING)

    def testPublishInAnotherArchive(self):
        """Publication in another archive

        Basically test if publishing records target to other archive
        than Distribution.main_archive work as expected
        """
        cprov = getUtility(IPersonSet).getByName("cprov")
        test_pool_dir = tempfile.mkdtemp()
        test_temp_dir = tempfile.mkdtemp()
        test_disk_pool = DiskPool(
            cprov.archive, test_pool_dir, test_temp_dir, self.logger
        )

        pub_source = self.getPubSource(
            sourcename="foo",
            filecontent=b"Am I a PPA Record ?",
            archive=cprov.archive,
        )
        pub_source.publish(test_disk_pool, self.logger)
        self.layer.commit()

        IStore(pub_source).flush()
        self.assertEqual(pub_source.status, PackagePublishingStatus.PUBLISHED)
        self.assertEqual(
            pub_source.sourcepackagerelease.upload_archive, cprov.archive
        )
        foo_name = "%s/main/f/foo/foo_666.dsc" % test_pool_dir
        with open(foo_name) as foo:
            self.assertEqual(foo.read().strip(), "Am I a PPA Record ?")

        # Remove locally created dir.
        shutil.rmtree(test_pool_dir)
        shutil.rmtree(test_temp_dir)


class PublishingSetTests(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.archive = self.factory.makeArchive(
            distribution=self.distroseries.distribution
        )
        self.publishing = self.factory.makeSourcePackagePublishingHistory(
            distroseries=self.distroseries, archive=self.archive
        )
        self.publishing_set = getUtility(IPublishingSet)

    def test_getByIdAndArchive_finds_record(self):
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, self.archive
        )
        self.assertEqual(self.publishing, record)

    def test_getByIdAndArchive_finds_record_explicit_source(self):
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, self.archive, source=True
        )
        self.assertEqual(self.publishing, record)

    def test_getByIdAndArchive_wrong_archive(self):
        wrong_archive = self.factory.makeArchive()
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, wrong_archive
        )
        self.assertEqual(None, record)

    def makeBinaryPublishing(self):
        distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries
        )
        binary_publishing = self.factory.makeBinaryPackagePublishingHistory(
            archive=self.archive, distroarchseries=distroarchseries
        )
        return binary_publishing

    def test_getByIdAndArchive_wrong_type(self):
        self.makeBinaryPublishing()
        record = self.publishing_set.getByIdAndArchive(
            self.publishing.id, self.archive, source=False
        )
        if record is not None:
            self.assertTrue(IBinaryPackagePublishingHistory.providedBy(record))

    def test_getByIdAndArchive_finds_binary(self):
        binary_publishing = self.makeBinaryPublishing()
        record = self.publishing_set.getByIdAndArchive(
            binary_publishing.id, self.archive, source=False
        )
        self.assertEqual(binary_publishing, record)

    def test_getByIdAndArchive_binary_wrong_archive(self):
        binary_publishing = self.makeBinaryPublishing()
        wrong_archive = self.factory.makeArchive()
        record = self.publishing_set.getByIdAndArchive(
            binary_publishing.id, wrong_archive, source=False
        )
        self.assertEqual(None, record)


class TestPublishingSetLite(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.person = self.factory.makePerson()

    def test_requestDeletion_marks_SPPHs_deleted(self):
        spph = self.factory.makeSourcePackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion([spph], self.person)
        self.assertEqual(PackagePublishingStatus.DELETED, spph.status)

    def test_requestDeletion_leaves_other_SPPHs_alone(self):
        spph = self.factory.makeSourcePackagePublishingHistory()
        other_spph = self.factory.makeSourcePackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion([other_spph], self.person)
        self.assertEqual(PackagePublishingStatus.PENDING, spph.status)

    def test_requestDeletion_marks_BPPHs_deleted(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion([bpph], self.person)
        self.assertEqual(PackagePublishingStatus.DELETED, bpph.status)

    def test_requestDeletion_marks_attached_BPPHs_deleted(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory()
        spph = self.factory.makeSPPHForBPPH(bpph)
        getUtility(IPublishingSet).requestDeletion([spph], self.person)
        self.assertEqual(PackagePublishingStatus.DELETED, spph.status)

    def test_requestDeletion_leaves_other_BPPHs_alone(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory()
        unrelated_spph = self.factory.makeSourcePackagePublishingHistory()
        getUtility(IPublishingSet).requestDeletion(
            [unrelated_spph], self.person
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)

    def test_requestDeletion_accepts_empty_sources_list(self):
        getUtility(IPublishingSet).requestDeletion([], self.person)
        # The test is that this does not fail.
        Store.of(self.person).flush()

    def test_requestDeletion_creates_DistroSeriesDifferenceJobs(self):
        dsp = self.factory.makeDistroSeriesParent()
        spph = self.factory.makeSourcePackagePublishingHistory(
            dsp.derived_series, pocket=PackagePublishingPocket.RELEASE
        )
        spn = spph.sourcepackagerelease.sourcepackagename
        getUtility(IPublishingSet).requestDeletion([spph], self.person)
        self.assertEqual(
            1,
            len(find_waiting_jobs(dsp.derived_series, spn, dsp.parent_series)),
        )

    def test_requestDeletion_disallows_unmodifiable_suites(self):
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE
        )
        spph = self.factory.makeSourcePackagePublishingHistory(
            distroseries=bpph.distroseries,
            pocket=PackagePublishingPocket.RELEASE,
        )
        spph.distroseries.status = SeriesStatus.CURRENT
        message = "Cannot delete publications from suite '%s'" % (
            spph.distroseries.getSuite(spph.pocket)
        )
        for pub in spph, bpph:
            self.assertRaisesWithContent(
                DeletionError, message, pub.requestDeletion, self.person
            )
            self.assertRaisesWithContent(
                DeletionError, message, pub.api_requestDeletion, self.person
            )

    def test_requestDeletion_marks_debug_as_deleted(self):
        (
            matching_bpph,
            debug_matching_bpph,
        ) = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        non_match_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE
        )
        non_match_bpr = removeSecurityProxy(
            non_match_bpph.binarypackagerelease
        )
        debug_non_match_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE,
            binpackageformat=BinaryPackageFormat.DDEB,
        )
        debug_non_match_bpr = debug_non_match_bpph.binarypackagerelease
        non_match_bpr.debug_package = debug_non_match_bpr
        getUtility(IPublishingSet).requestDeletion(
            [matching_bpph, non_match_bpph], self.person
        )
        for pub in (matching_bpph, debug_matching_bpph, non_match_bpph):
            self.assertEqual(pub.status, PackagePublishingStatus.DELETED)
        self.assertEqual(
            debug_non_match_bpph.status, PackagePublishingStatus.PENDING
        )

    def test_changeOverride_also_overrides_debug_package(self):
        user = self.factory.makePerson()
        bpph, debug_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        new_section = self.factory.makeSection()
        new_bpph = bpph.changeOverride(new_section=new_section, creator=user)
        publishing_set = getUtility(IPublishingSet)
        [new_debug_bpph] = publishing_set.findCorrespondingDDEBPublications(
            [new_bpph]
        )
        self.assertEqual(new_debug_bpph.creator, user)
        self.assertEqual(new_debug_bpph.section, new_section)

    def test_requestDeletion_forbids_debug_package(self):
        bpph, debug_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        self.assertRaisesWithContent(
            DeletionError,
            "Cannot delete ddeb publications directly; delete "
            "the corresponding deb instead.",
            debug_bpph.requestDeletion,
            self.factory.makePerson(),
        )

    def test_changeOverride_forbids_debug_package(self):
        bpph, debug_bpph = self.factory.makeBinaryPackagePublishingHistory(
            pocket=PackagePublishingPocket.RELEASE, with_debug=True
        )
        self.assertRaisesWithContent(
            OverrideError,
            "Cannot override ddeb publications directly; "
            "override the corresponding deb instead.",
            debug_bpph.changeOverride,
            new_phased_update_percentage=20,
        )

    def makePublishedSourcePackage(self, series, pocket=None, status=None):
        # Make a published source package.
        name = self.factory.getUniqueUnicode()
        sourcepackagename = self.factory.makeSourcePackageName(name)
        component = getUtility(IComponentSet)["universe"]
        spph = self.factory.makeSourcePackagePublishingHistory(
            sourcepackagename=sourcepackagename,
            distroseries=series,
            component=component,
            pocket=pocket,
            status=status,
        )
        source_package = self.factory.makeSourcePackage(
            sourcepackagename=spph.sourcepackagename, distroseries=series
        )
        spr = spph.sourcepackagerelease
        for extension in ("dsc", "tar.gz"):
            filename = "%s_%s.%s" % (spr.name, spr.version, extension)
            spr.addFile(
                self.factory.makeLibraryFileAlias(
                    filename=filename, db_only=True
                )
            )
        return source_package

    def test_getSourcesForPublishing(self):
        # PublisherSet.getSourcesForPublishing returns all the ISPPH records
        # in a given publishing context.  It is used as part of publishing
        # some types of archives.
        # XXX cjwatson 2022-03-28: Detach test from sampledata.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]
        hoary = ubuntu["hoary"]
        component_main = getUtility(IComponentSet)["main"]
        component_multiverse = getUtility(IComponentSet)["multiverse"]
        debian_archive = getUtility(IDistributionSet)["debian"].main_archive
        publishing_set = getUtility(IPublishingSet)

        spphs = publishing_set.getSourcesForPublishing(
            archive=hoary.main_archive,
            distroseries=hoary,
            pocket=PackagePublishingPocket.RELEASE,
            component=component_main,
        )
        self.assertEqual(6, spphs.count())
        self.assertContentEqual(
            [
                "alsa-utils",
                "evolution",
                "libstdc++",
                "linux-source-2.6.15",
                "netapplet",
                "pmount",
            ],
            {spph.sourcepackagerelease.name for spph in spphs},
        )
        self.assertEqual(
            0,
            publishing_set.getSourcesForPublishing(
                archive=hoary.main_archive,
                distroseries=hoary,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_multiverse,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getSourcesForPublishing(
                archive=hoary.main_archive,
                distroseries=hoary,
                pocket=PackagePublishingPocket.BACKPORTS,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getSourcesForPublishing(
                archive=debian_archive,
                distroseries=hoary,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            14,
            publishing_set.getSourcesForPublishing(
                archive=hoary.main_archive
            ).count(),
        )

    def test_getSourcesForPublishing_query_count(self):
        # Check that the number of queries required to publish source
        # packages is constant in the number of source packages.
        series = self.factory.makeDistroSeries()
        archive = series.main_archive
        component_universe = getUtility(IComponentSet)["universe"]

        def get_index_stanzas():
            for spp in getUtility(IPublishingSet).getSourcesForPublishing(
                archive=archive,
                distroseries=series,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_universe,
            ):
                build_source_stanza_fields(
                    spp.sourcepackagerelease, spp.component, spp.section
                )

        recorder1, recorder2 = record_two_runs(
            get_index_stanzas,
            partial(
                self.makePublishedSourcePackage,
                series=series,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            ),
            5,
            5,
        )
        self.assertThat(recorder1, HasQueryCount(Equals(8)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))

    def makePublishedBinaryPackage(self, das, pocket=None, status=None):
        # Make a published binary package.
        source = self.makePublishedSourcePackage(
            das.distroseries, pocket=pocket, status=status
        )
        spr = source.distinctreleases[0]
        binarypackagename = self.factory.makeBinaryPackageName(source.name)
        bpph = self.factory.makeBinaryPackagePublishingHistory(
            binarypackagename=binarypackagename,
            distroarchseries=das,
            component=spr.component,
            section_name=spr.section.name,
            status=status,
            pocket=pocket,
            source_package_release=spr,
        )
        bpr = bpph.binarypackagerelease
        filename = "%s_%s_%s.deb" % (
            bpr.name,
            bpr.version,
            das.architecturetag,
        )
        bpr.addFile(
            self.factory.makeLibraryFileAlias(filename=filename, db_only=True)
        )
        return bpph

    def test_getBinariesForPublishing(self):
        # PublisherSet.getBinariesForPublishing returns all the IBPPH
        # records in a given publishing context.  It is used as part of
        # publishing some types of archives.
        # XXX cjwatson 2022-03-28: Detach test from sampledata.
        ubuntu = getUtility(IDistributionSet)["ubuntu"]
        warty = ubuntu["warty"]
        warty_i386 = warty["i386"]
        warty_another = self.factory.makeDistroArchSeries(distroseries=warty)
        component_main = getUtility(IComponentSet)["main"]
        component_multiverse = getUtility(IComponentSet)["multiverse"]
        debian_archive = getUtility(IDistributionSet)["debian"].main_archive
        publishing_set = getUtility(IPublishingSet)

        bpphs = publishing_set.getBinariesForPublishing(
            archive=warty.main_archive,
            distroarchseries=warty_i386,
            pocket=PackagePublishingPocket.RELEASE,
            component=component_main,
        )
        self.assertEqual(8, bpphs.count())
        self.assertIn(
            "mozilla-firefox",
            {bpph.binarypackagerelease.name for bpph in bpphs},
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive,
                distroarchseries=warty_another,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive,
                distroarchseries=warty_i386,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_multiverse,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive,
                distroarchseries=warty_i386,
                pocket=PackagePublishingPocket.BACKPORTS,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            0,
            publishing_set.getBinariesForPublishing(
                archive=debian_archive,
                distroarchseries=warty_i386,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_main,
            ).count(),
        )
        self.assertEqual(
            12,
            publishing_set.getBinariesForPublishing(
                archive=warty.main_archive
            ).count(),
        )

    def test_getBinariesForPublishing_query_count(self):
        # Check that the number of queries required to publish binary
        # packages is constant in the number of binary packages.
        das = self.factory.makeDistroArchSeries()
        archive = das.main_archive
        component_universe = getUtility(IComponentSet)["universe"]

        def get_index_stanzas():
            for bpp in getUtility(IPublishingSet).getBinariesForPublishing(
                archive=archive,
                distroarchseries=das,
                pocket=PackagePublishingPocket.RELEASE,
                component=component_universe,
            ):
                build_binary_stanza_fields(
                    bpp.binarypackagerelease,
                    bpp.component,
                    bpp.section,
                    bpp.priority,
                    bpp.phased_update_percentage,
                    False,
                )

        recorder1, recorder2 = record_two_runs(
            get_index_stanzas,
            partial(
                self.makePublishedBinaryPackage,
                das=das,
                pocket=PackagePublishingPocket.RELEASE,
                status=PackagePublishingStatus.PUBLISHED,
            ),
            5,
            5,
        )
        self.assertThat(recorder1, HasQueryCount(Equals(11)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestSourceDomination(TestNativePublishingBase):
    """Test SourcePackagePublishingHistory.supersede() operates correctly."""

    def testSupersede(self):
        """Check that supersede() without arguments works."""
        source = self.getPubSource()
        source.supersede()
        self.checkSuperseded([source])

    def testSupersedeWithDominant(self):
        """Check that supersede() with a dominant publication works."""
        source = self.getPubSource()
        super_source = self.getPubSource()
        source.supersede(super_source)
        self.checkSuperseded([source], super_source)

    def testSupersedingSupersededSourceFails(self):
        """Check that supersede() fails with a superseded source.

        Sources should not be superseded twice. If a second attempt is made,
        the Dominator's lookups are buggy.
        """
        source = self.getPubSource()
        super_source = self.getPubSource()
        source.supersede(super_source)
        self.checkSuperseded([source], super_source)

        # Manually set a date in the past, so we can confirm that
        # the second supersede() fails properly.
        source.datesuperseded = datetime(2006, 12, 25, tzinfo=timezone.utc)
        super_date = source.datesuperseded

        self.assertRaises(AssertionError, source.supersede, super_source)
        self.checkSuperseded([source], super_source)
        self.assertEqual(super_date, source.datesuperseded)


class TestBinaryDomination(TestNativePublishingBase):
    """Test BinaryPackagePublishingHistory.supersede() operates correctly."""

    def testSupersede(self):
        """Check that supersede() without arguments works."""
        bins = self.getPubBinaries(architecturespecific=True)
        bins[0].supersede()
        self.checkSuperseded([bins[0]])
        self.checkPublication(bins[1], PackagePublishingStatus.PENDING)

    def testSupersedeWithDominant(self):
        """Check that supersede() with a dominant publication works."""
        bins = self.getPubBinaries(architecturespecific=True)
        super_bins = self.getPubBinaries(architecturespecific=True)
        bins[0].supersede(super_bins[0])
        self.checkSuperseded([bins[0]], super_bins[0])
        self.checkPublication(bins[1], PackagePublishingStatus.PENDING)

    def testSupersedingSupersededArchSpecificBinaryFails(self):
        """Check that supersede() fails with a superseded arch-dep binary.

        Architecture-specific binaries should not normally be superseded
        twice. If a second attempt is made, the Dominator's lookups are buggy.
        """
        bin = self.getPubBinaries(architecturespecific=True)[0]
        super_bin = self.getPubBinaries(architecturespecific=True)[0]
        bin.supersede(super_bin)

        # Manually set a date in the past, so we can confirm that
        # the second supersede() fails properly.
        bin.datesuperseded = datetime(2006, 12, 25, tzinfo=timezone.utc)
        super_date = bin.datesuperseded

        self.assertRaises(AssertionError, bin.supersede, super_bin)
        self.checkSuperseded([bin], super_bin)
        self.assertEqual(super_date, bin.datesuperseded)

    def testSkipsSupersededArchIndependentBinary(self):
        """Check that supersede() skips a superseded arch-indep binary.

        Since all publications of an architecture-independent binary are
        superseded atomically, they may be superseded again later. In that
        case, we skip the domination, leaving the old date unchanged.
        """
        bin = self.getPubBinaries(architecturespecific=False)[0]
        super_bin = self.getPubBinaries(architecturespecific=False)[0]
        bin.supersede(super_bin)
        self.checkSuperseded([bin], super_bin)

        # Manually set a date in the past, so we can confirm that
        # the second supersede() skips properly.
        bin.datesuperseded = datetime(2006, 12, 25, tzinfo=timezone.utc)
        super_date = bin.datesuperseded

        bin.supersede(super_bin)
        self.checkSuperseded([bin], super_bin)
        self.assertEqual(super_date, bin.datesuperseded)

    def testSupersedesCorrespondingDDEB(self):
        """Check that supersede() takes with it any corresponding DDEB.

        DDEB publications should be superseded when their corresponding DEB
        is.
        """
        # Each of these will return (i386 deb, i386 ddeb, hppa deb,
        # hppa ddeb).
        bins = self.getPubBinaries(architecturespecific=True, with_debug=True)
        super_bins = self.getPubBinaries(
            architecturespecific=True, with_debug=True
        )

        bins[0].supersede(super_bins[0])
        self.checkSuperseded(bins[:2], super_bins[0])
        self.checkPublications(bins[2:], PackagePublishingStatus.PENDING)
        self.checkPublications(super_bins, PackagePublishingStatus.PENDING)

        bins[2].supersede(super_bins[2])
        self.checkSuperseded(bins[:2], super_bins[0])
        self.checkSuperseded(bins[2:], super_bins[2])
        self.checkPublications(super_bins, PackagePublishingStatus.PENDING)

    def testDDEBsCannotSupersede(self):
        """Check that DDEBs cannot supersede other publications.

        Since DDEBs are superseded when their DEBs are, there's no need to
        for them supersede anything themselves. Any such attempt is an error.
        """
        # This will return (i386 deb, i386 ddeb, hppa deb, hppa ddeb).
        bins = self.getPubBinaries(architecturespecific=True, with_debug=True)
        self.assertRaises(AssertionError, bins[0].supersede, bins[1])


class TestBinaryGetOtherPublications(TestNativePublishingBase):
    """Test BinaryPackagePublishingHistory.getOtherPublications() works."""

    def checkOtherPublications(self, this, others):
        self.assertContentEqual(
            removeSecurityProxy(this).getOtherPublications(), others
        )

    def testFindsOtherArchIndepPublications(self):
        """Arch-indep publications with the same overrides should be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        self.checkOtherPublications(bins[0], bins)

    def testDoesntFindArchSpecificPublications(self):
        """Arch-dep publications shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=True)
        self.checkOtherPublications(bins[0], [bins[0]])

    def testDoesntFindPublicationsInOtherArchives(self):
        """Publications in other archives shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        foreign_bins = bins[0].copyTo(
            bins[0].distroarchseries.distroseries,
            bins[0].pocket,
            self.factory.makeArchive(
                distribution=(
                    bins[0].distroarchseries.distroseries.distribution
                )
            ),
        )
        self.checkOtherPublications(bins[0], bins)
        self.checkOtherPublications(foreign_bins[0], foreign_bins)

    def testDoesntFindPublicationsWithDifferentOverrides(self):
        """Publications with different overrides shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        universe = getUtility(IComponentSet)["universe"]
        foreign_bin = bins[0].changeOverride(new_component=universe)
        self.checkOtherPublications(bins[0], bins)
        self.checkOtherPublications(foreign_bin, [foreign_bin])

    def testDoesntFindSupersededPublications(self):
        """Superseded publications shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        self.checkOtherPublications(bins[0], bins)
        for bpph in bins:
            bpph.supersede()
        self.checkOtherPublications(bins[0], [])

    def testDoesntFindPublicationsInOtherSeries(self):
        """Publications in other series shouldn't be found."""
        bins = self.getPubBinaries(architecturespecific=False)
        series = self.factory.makeDistroSeries(
            distribution=bins[0].archive.distribution
        )
        self.factory.makeDistroArchSeries(
            distroseries=series, architecturetag="i386"
        )
        foreign_bins = bins[0].copyTo(series, bins[0].pocket, bins[0].archive)
        self.checkOtherPublications(bins[0], bins)
        self.checkOtherPublications(foreign_bins[0], foreign_bins)


class TestGetOtherPublicationsForSameSource(TestNativePublishingBase):
    """Test parts of the BinaryPackagePublishingHistory model.

    See also lib/lp/soyuz/doc/publishing.rst
    """

    layer = LaunchpadZopelessLayer

    def _makeMixedSingleBuildPackage(self, version="1.0"):
        # Set up a source with a build that generated four binaries,
        # two of them an arch-all.
        foo_src_pub = self.getPubSource(
            sourcename="foo",
            version=version,
            architecturehintlist="i386",
            status=PackagePublishingStatus.PUBLISHED,
        )
        [foo_bin_pub] = self.getPubBinaries(
            binaryname="foo-bin",
            status=PackagePublishingStatus.PUBLISHED,
            architecturespecific=True,
            version=version,
            pub_source=foo_src_pub,
        )
        # Now need to grab the build for the source so we can add
        # more binaries to it.
        [build] = foo_src_pub.getBuilds()
        foo_one_common = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo-one-common",
            version=version,
            build=build,
            architecturespecific=False,
        )
        foo_one_common_pubs = self.publishBinaryInArchive(
            foo_one_common,
            self.ubuntutest.main_archive,
            pocket=foo_src_pub.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        foo_two_common = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo-two-common",
            version=version,
            build=build,
            architecturespecific=False,
        )
        foo_two_common_pubs = self.publishBinaryInArchive(
            foo_two_common,
            self.ubuntutest.main_archive,
            pocket=foo_src_pub.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        foo_three = self.factory.makeBinaryPackageRelease(
            binarypackagename="foo-three",
            version=version,
            build=build,
            architecturespecific=True,
        )
        [foo_three_pub] = self.publishBinaryInArchive(
            foo_three,
            self.ubuntutest.main_archive,
            pocket=foo_src_pub.pocket,
            status=PackagePublishingStatus.PUBLISHED,
        )
        # So now we have source foo, which has arch specific binaries
        # foo-bin and foo-three, and arch:all binaries foo-one-common and
        # foo-two-common. The latter two will have multiple publications,
        # one for each DAS in the series.
        return (
            foo_src_pub,
            foo_bin_pub,
            foo_one_common_pubs,
            foo_two_common_pubs,
            foo_three_pub,
        )


class TestGetBuiltBinaries(TestNativePublishingBase):
    """Test SourcePackagePublishingHistory.getBuiltBinaries() works."""

    def test_flat_query_count(self):
        spph = self.getPubSource(architecturehintlist="any")
        store = Store.of(spph)
        store.flush()
        store.invalidate()

        # An initial invocation issues one query for each of the SPPH, BPPHs
        # and BPRs, as well as one query to check whether the SPR came from
        # a CI build.
        with StormStatementRecorder() as recorder:
            bins = spph.getBuiltBinaries()
        self.assertEqual(0, len(bins))
        self.assertThat(recorder, HasQueryCount(Equals(4)))

        self.getPubBinaries(pub_source=spph)
        store.flush()
        store.invalidate()

        # A subsequent invocation with files preloaded queries the SPPH,
        # BPPHs, BPRs, BPFs and LFAs, as well as one query to check whether
        # the SPR came from a CI build.  Checking the filenames of each BPF
        # has no query penalty.
        with StormStatementRecorder() as recorder:
            bins = spph.getBuiltBinaries(want_files=True)
            self.assertEqual(2, len(bins))
            for bpph in bins:
                files = bpph.binarypackagerelease.files
                self.assertEqual(1, len(files))
                for bpf in files:
                    bpf.libraryfile.filename
        self.assertThat(recorder, HasQueryCount(Equals(6)))

    def test_ci_build(self):
        with lp_dbuser():
            distroseries = self.factory.makeDistroSeries()
            dases = [
                self.factory.makeDistroArchSeries(distroseries=distroseries)
                for _ in range(2)
            ]
            archive = self.factory.makeArchive(
                distribution=distroseries.distribution
            )
            repository = self.factory.makeGitRepository()
            commit_sha1 = hashlib.sha1(
                self.factory.getUniqueBytes()
            ).hexdigest()
            builds = [
                self.factory.makeCIBuild(
                    git_repository=repository,
                    commit_sha1=commit_sha1,
                    distro_arch_series=das,
                )
                for das in dases
            ]
            spn = self.factory.makeSourcePackageName()
            spr = builds[0].createSourcePackageRelease(
                distroseries,
                spn,
                "1.0",
                creator=repository.owner,
                archive=archive,
            )
            spph = self.factory.makeSourcePackagePublishingHistory(
                archive=archive,
                sourcepackagerelease=spr,
                format=SourcePackageType.CI_BUILD,
            )
            bpn = self.factory.makeBinaryPackageName()
            bpphs = []
            for build in builds:
                bpr = build.createBinaryPackageRelease(
                    bpn,
                    "1.0",
                    "test summary",
                    "test description",
                    BinaryPackageFormat.WHL,
                    True,
                )
                bpphs.extend(
                    getUtility(IPublishingSet).publishBinaries(
                        archive,
                        distroseries,
                        spph.pocket,
                        {bpr: (None, None, None, None)},
                    )
                )

        self.assertContentEqual(bpphs, spph.getBuiltBinaries())


class TestGetActiveArchSpecificPublications(TestCaseWithFactory):
    layer = ZopelessDatabaseLayer

    def makeSPR(self):
        """Create a `SourcePackageRelease`."""
        # Return an un-proxied SPR.  This test is for script code; it
        # won't get proxied objects in real life.
        return removeSecurityProxy(self.factory.makeSourcePackageRelease())

    def makeBPPHs(self, spr, number=1):
        """Create `BinaryPackagePublishingHistory` object(s).

        Each of the publications will be active and architecture-specific.
        Each will be for the same archive, distroseries, and pocket.

        Since the tests need to create a pocket mismatch, it is guaranteed
        that the BPPHs are for the UPDATES pocket.
        """
        das = self.factory.makeDistroArchSeries()
        distroseries = das.distroseries
        archive = distroseries.main_archive
        pocket = PackagePublishingPocket.UPDATES

        bpbs = [
            self.factory.makeBinaryPackageBuild(
                source_package_release=spr, distroarchseries=das
            )
            for counter in range(number)
        ]
        bprs = [
            self.factory.makeBinaryPackageRelease(
                build=bpb, architecturespecific=True
            )
            for bpb in bpbs
        ]

        return [
            removeSecurityProxy(
                self.factory.makeBinaryPackagePublishingHistory(
                    archive=archive,
                    distroarchseries=das,
                    pocket=pocket,
                    binarypackagerelease=bpr,
                    status=PackagePublishingStatus.PUBLISHED,
                )
            )
            for bpr in bprs
        ]

    def test_getActiveArchSpecificPublications_finds_only_matches(self):
        spr = self.makeSPR()
        bpphs = self.makeBPPHs(spr, 5)

        # This BPPH will match our search.
        match = bpphs[0]

        distroseries = match.distroseries
        distro = distroseries.distribution

        # These BPPHs will not match our search, each because they fail
        # one search parameter.
        bpphs[1].archive = self.factory.makeArchive(
            distribution=distro, purpose=ArchivePurpose.PARTNER
        )
        bpphs[2].distroarchseries = self.factory.makeDistroArchSeries(
            distroseries=self.factory.makeDistroSeries(distribution=distro)
        )
        bpphs[3].pocket = PackagePublishingPocket.SECURITY
        bpphs[4].binarypackagerelease.architecturespecific = False

        self.assertContentEqual(
            [match],
            getUtility(IPublishingSet).getActiveArchSpecificPublications(
                spr, match.archive, match.distroseries, match.pocket
            ),
        )

    def test_getActiveArchSpecificPublications_detects_absence(self):
        spr = self.makeSPR()
        distroseries = spr.upload_distroseries
        result = getUtility(IPublishingSet).getActiveArchSpecificPublications(
            spr,
            distroseries.main_archive,
            distroseries,
            self.factory.getAnyPocket(),
        )
        self.assertFalse(result.any())

    def test_getActiveArchSpecificPublications_filters_status(self):
        spr = self.makeSPR()
        bpphs = self.makeBPPHs(spr, len(PackagePublishingStatus.items))
        for bpph, status in zip(bpphs, PackagePublishingStatus.items):
            bpph.status = status
        by_status = {bpph.status: bpph for bpph in bpphs}
        self.assertContentEqual(
            [by_status[status] for status in active_publishing_status],
            getUtility(IPublishingSet).getActiveArchSpecificPublications(
                spr, bpphs[0].archive, bpphs[0].distroseries, bpphs[0].pocket
            ),
        )


class TestPublishBinaries(TestCaseWithFactory):
    """Test PublishingSet.publishBinaries() works."""

    layer = LaunchpadZopelessLayer

    def makeArgs(self, bprs, distroseries, archive=None, channel=None):
        """Create a dict of arguments for publishBinaries."""
        if archive is None:
            archive = distroseries.main_archive
        args = {
            "archive": archive,
            "distroseries": distroseries,
            "pocket": (
                PackagePublishingPocket.BACKPORTS
                if channel is None
                else PackagePublishingPocket.RELEASE
            ),
            "binaries": {
                bpr: (
                    self.factory.makeComponent(),
                    self.factory.makeSection(),
                    PackagePublishingPriority.REQUIRED,
                    50,
                )
                for bpr in bprs
            },
        }
        if channel is not None:
            args["channel"] = channel
        return args

    def test_architecture_dependent(self):
        # Architecture-dependent binaries get created as PENDING in the
        # corresponding architecture of the destination series and pocket,
        # with the given overrides.
        arch_tag = self.factory.getUniqueString("arch-")
        orig_das = self.factory.makeDistroArchSeries(architecturetag=arch_tag)
        target_das = self.factory.makeDistroArchSeries(
            architecturetag=arch_tag
        )
        build = self.factory.makeBinaryPackageBuild(distroarchseries=orig_das)
        bpr = self.factory.makeBinaryPackageRelease(
            build=build, architecturespecific=True
        )
        args = self.makeArgs([bpr], target_das.distroseries)
        [bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        overrides = args["binaries"][bpr]
        self.assertEqual(bpr, bpph.binarypackagerelease)
        self.assertEqual(
            (args["archive"], target_das, args["pocket"], None),
            (bpph.archive, bpph.distroarchseries, bpph.pocket, bpph.channel),
        )
        self.assertEqual(
            overrides,
            (
                bpph.component,
                bpph.section,
                bpph.priority,
                bpph.phased_update_percentage,
            ),
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)

    def test_architecture_variant(self):
        # When a package is not built for a variant, the binaries for
        # the underlying architecture are published to the variant DAS.
        arch_tag = self.factory.getUniqueString("arch-")
        orig_das = self.factory.makeDistroArchSeries(architecturetag=arch_tag)
        target_das = self.factory.makeDistroArchSeries(
            architecturetag=arch_tag
        )
        target_variant_das = self.factory.makeDistroArchSeries(
            distroseries=target_das.distroseries,
            architecturetag=arch_tag + "v2",
            underlying_architecturetag=arch_tag,
        )
        dasf = self.factory.makeDistroArchSeriesFilter(
            distroarchseries=target_variant_das,
            sense=DistroArchSeriesFilterSense.EXCLUDE,
        )
        build = self.factory.makeBinaryPackageBuild(distroarchseries=orig_das)
        dasf.packageset.add([build.source_package_release.sourcepackagename])
        bpr = self.factory.makeBinaryPackageRelease(
            build=build, architecturespecific=True
        )
        args = self.makeArgs([bpr], target_das.distroseries)
        bpphes = list(getUtility(IPublishingSet).publishBinaries(**args))
        self.assertEqual(len(bpphes), 2)
        actual_target_dases = {bpph.distroarchseries for bpph in bpphes}
        actual_bprs = {bpph.binarypackagerelease for bpph in bpphes}
        self.assertEqual(actual_target_dases, {target_das, target_variant_das})
        self.assertEqual(actual_bprs, {bpr})

    def test_architecture_independent(self):
        # Architecture-independent binaries get published to all enabled
        # DASes in the series.
        bpr = self.factory.makeBinaryPackageRelease(architecturespecific=False)
        # Create 3 architectures. The binary will not be published in
        # the disabled one.
        target_das_a = self.factory.makeDistroArchSeries()
        target_das_b = self.factory.makeDistroArchSeries(
            distroseries=target_das_a.distroseries
        )
        # We don't reference target_das_c so it doesn't get a name.
        self.factory.makeDistroArchSeries(
            distroseries=target_das_a.distroseries, enabled=False
        )
        args = self.makeArgs([bpr], target_das_a.distroseries)
        bpphs = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertEqual(2, len(bpphs))
        self.assertContentEqual(
            (target_das_a, target_das_b),
            [bpph.distroarchseries for bpph in bpphs],
        )

    def test_architecture_disabled(self):
        # An empty list is return if the DistroArchSeries was disabled.
        arch_tag = self.factory.getUniqueString("arch-")
        orig_das = self.factory.makeDistroArchSeries(architecturetag=arch_tag)
        target_das = self.factory.makeDistroArchSeries(
            architecturetag=arch_tag
        )
        build = self.factory.makeBinaryPackageBuild(distroarchseries=orig_das)
        bpr = self.factory.makeBinaryPackageRelease(
            build=build, architecturespecific=True
        )
        target_das.enabled = False
        args = self.makeArgs([bpr], target_das.distroseries)
        results = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertEqual([], results)

    def test_does_not_duplicate(self):
        # An attempt to copy something for a second time is ignored.
        bpr = self.factory.makeBinaryPackageRelease()
        target_das = self.factory.makeDistroArchSeries()
        args = self.makeArgs([bpr], target_das.distroseries)
        [new_bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertContentEqual(
            [], getUtility(IPublishingSet).publishBinaries(**args)
        )

        # But changing the target (eg. to RELEASE instead of BACKPORTS)
        # causes a new publication to be created.
        args["pocket"] = PackagePublishingPocket.RELEASE
        [another_bpph] = getUtility(IPublishingSet).publishBinaries(**args)

    def test_channel(self):
        bpr = self.factory.makeBinaryPackageRelease(
            binpackageformat=BinaryPackageFormat.WHL
        )
        target_das = self.factory.makeDistroArchSeries()
        args = self.makeArgs([bpr], target_das.distroseries, channel="stable")
        [bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertEqual(bpr, bpph.binarypackagerelease)
        self.assertEqual(
            (args["archive"], target_das, args["pocket"], args["channel"]),
            (bpph.archive, bpph.distroarchseries, bpph.pocket, bpph.channel),
        )
        self.assertEqual(PackagePublishingStatus.PENDING, bpph.status)

    def test_does_not_duplicate_by_channel(self):
        bpr = self.factory.makeBinaryPackageRelease(
            binpackageformat=BinaryPackageFormat.WHL
        )
        target_das = self.factory.makeDistroArchSeries()
        args = self.makeArgs([bpr], target_das.distroseries, channel="stable")
        [bpph] = getUtility(IPublishingSet).publishBinaries(**args)
        self.assertContentEqual(
            [], getUtility(IPublishingSet).publishBinaries(**args)
        )
        args["channel"] = "edge"
        [another_bpph] = getUtility(IPublishingSet).publishBinaries(**args)


class TestChangeOverride(TestNativePublishingBase):
    """Test that changing overrides works."""

    def setUpOverride(
        self,
        status=SeriesStatus.DEVELOPMENT,
        pocket=PackagePublishingPocket.RELEASE,
        channel=None,
        binary=False,
        format=None,
        ddeb=False,
        **kwargs,
    ):
        self.distroseries.status = status
        get_pub_kwargs = {"pocket": pocket, "channel": channel}
        if format is not None:
            get_pub_kwargs["format"] = format
        if ddeb:
            pub = self.getPubBinaries(with_debug=True, **get_pub_kwargs)[2]
            self.assertEqual(
                BinaryPackageFormat.DDEB,
                pub.binarypackagerelease.binpackageformat,
            )
        elif binary:
            pub = self.getPubBinaries(**get_pub_kwargs)[0]
        else:
            pub = self.getPubSource(**get_pub_kwargs)
        return pub.changeOverride(**kwargs)

    def assertCanOverride(
        self,
        status=SeriesStatus.DEVELOPMENT,
        pocket=PackagePublishingPocket.RELEASE,
        channel=None,
        **kwargs,
    ):
        new_pub = self.setUpOverride(
            status=status, pocket=pocket, channel=channel, **kwargs
        )
        self.assertEqual(new_pub.status, PackagePublishingStatus.PENDING)
        self.assertEqual(new_pub.pocket, pocket)
        self.assertEqual(new_pub.channel, channel)
        if "new_component" in kwargs:
            self.assertEqual(kwargs["new_component"], new_pub.component.name)
        if "new_section" in kwargs:
            self.assertEqual(kwargs["new_section"], new_pub.section.name)
        if "new_priority" in kwargs:
            self.assertEqual(
                kwargs["new_priority"], new_pub.priority.name.lower()
            )
        if "new_phased_update_percentage" in kwargs:
            self.assertEqual(
                kwargs["new_phased_update_percentage"],
                new_pub.phased_update_percentage,
            )
        return new_pub

    def assertCannotOverride(self, **kwargs):
        self.assertRaises(OverrideError, self.setUpOverride, **kwargs)

    def test_changes_source(self):
        # SPPH.changeOverride changes the properties of source publications.
        self.assertCanOverride(new_component="universe", new_section="misc")

    def test_changes_binary(self):
        # BPPH.changeOverride changes the properties of binary publications.
        self.assertCanOverride(
            binary=True,
            new_component="universe",
            new_section="misc",
            new_priority="extra",
            new_phased_update_percentage=90,
        )

    def test_change_binary_logged_in_user(self):
        person = self.factory.makePerson()
        new_pub = self.assertCanOverride(
            binary=True,
            new_component="universe",
            new_section="misc",
            new_priority="extra",
            new_phased_update_percentage=90,
            creator=person,
        )
        self.assertEqual(person, new_pub.creator)

    def test_change_source_logged_in_user(self):
        person = self.factory.makePerson()
        new_pub = self.assertCanOverride(
            binary=False,
            new_component="universe",
            new_section="misc",
            creator=person,
        )
        self.assertEqual(person, new_pub.creator)

    def test_set_and_clear_phased_update_percentage(self):
        # new_phased_update_percentage=<integer> sets a phased update
        # percentage; new_phased_update_percentage=100 clears it.
        pub = self.assertCanOverride(
            binary=True, new_phased_update_percentage=50
        )
        new_pub = pub.changeOverride(new_phased_update_percentage=100)
        self.assertIsNone(new_pub.phased_update_percentage)

    def test_no_change(self):
        # changeOverride does not create a new publication if the existing
        # publication is already in the desired state.
        self.assertIsNone(
            self.setUpOverride(new_component="main", new_section="base")
        )
        self.assertIsNone(
            self.setUpOverride(
                binary=True,
                new_component="main",
                new_section="base",
                new_priority="standard",
            )
        )

    def test_forbids_stable_RELEASE(self):
        # changeOverride is not allowed in the RELEASE pocket of a stable
        # distroseries.
        self.assertCannotOverride(
            status=SeriesStatus.CURRENT, new_component="universe"
        )
        self.assertCannotOverride(
            status=SeriesStatus.CURRENT, binary=True, new_component="universe"
        )

    def test_allows_development_RELEASE(self):
        # changeOverride is allowed in the RELEASE pocket of a development
        # distroseries.
        self.assertCanOverride(new_component="universe")
        self.assertCanOverride(binary=True, new_component="universe")

    def test_allows_stable_PROPOSED(self):
        # changeOverride is allowed in the PROPOSED pocket of a stable
        # distroseries.
        self.assertCanOverride(
            status=SeriesStatus.CURRENT,
            pocket=PackagePublishingPocket.PROPOSED,
            new_component="universe",
        )
        self.assertCanOverride(
            status=SeriesStatus.CURRENT,
            pocket=PackagePublishingPocket.PROPOSED,
            binary=True,
            new_component="universe",
        )

    def test_forbids_changing_archive(self):
        # changeOverride refuses to make changes that would require changing
        # archive.
        self.assertCannotOverride(new_component="partner")
        self.assertCannotOverride(binary=True, new_component="partner")

    def test_preserves_channel(self):
        self.assertCanOverride(
            binary=True,
            format=BinaryPackageFormat.WHL,
            channel="stable",
            new_component="universe",
            new_section="misc",
            new_priority="extra",
            new_phased_update_percentage=90,
        )


class TestPublishingHistoryView(TestCaseWithFactory):
    layer = LaunchpadFunctionalLayer

    def test_constant_query_counts_on_publishing_history_change_override(self):
        admin = self.factory.makeAdministrator()
        normal_user = self.factory.makePerson()

        with person_logged_in(admin):
            test_publisher = SoyuzTestPublisher()
            test_publisher.prepareBreezyAutotest()

            source_pub = test_publisher.getPubSource(
                "test-history", status=PackagePublishingStatus.PUBLISHED
            )
        url = (
            "http://launchpad.test/ubuntutest/+source/test-history"
            "/+publishinghistory"
        )

        def insert_more_publish_history():
            person1 = self.factory.makePerson()
            new_component = (
                "universe" if source_pub.component.name == "main" else "main"
            )
            source_pub.changeOverride(
                new_component=new_component, creator=person1
            )

            person2 = self.factory.makePerson()
            new_section = (
                "web" if source_pub.section.name == "base" else "base"
            )
            source_pub.changeOverride(new_section=new_section, creator=person2)

        def show_page():
            self.getUserBrowser(url, normal_user)

        # Make sure to have all the history fitting in one page.
        self.pushConfig("launchpad", default_batch_size=50)

        recorder1, recorder2 = record_two_runs(
            show_page,
            insert_more_publish_history,
            1,
            10,
            login_method=login_admin,
        )

        self.assertThat(recorder1, HasQueryCount(Equals(26)))
        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))


class TestGetRecentSourceUploads(TestCaseWithFactory):
    """Tests for IPublishingSet.getRecentSourceUploads."""

    layer = DatabaseFunctionalLayer

    def setUp(self):
        super().setUp()
        self.distroseries = self.factory.makeDistroSeries()
        self.publishing_set = getUtility(IPublishingSet)

    def _makeSpph(self, **kwargs):
        """Create an SPPH in the test distroseries' main archive."""
        kwargs.setdefault("distroseries", self.distroseries)
        kwargs.setdefault("pocket", PackagePublishingPocket.RELEASE)
        if "archive" not in kwargs:
            kwargs["archive"] = self.distroseries.main_archive
        return self.factory.makeSourcePackagePublishingHistory(**kwargs)

    def test_getRecentSourceUploads_empty(self):
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual([], result)

    def test_getRecentSourceUploads_active_only(self):
        self._makeSpph(status=PackagePublishingStatus.PENDING)
        self._makeSpph(status=PackagePublishingStatus.PUBLISHED)
        self._makeSpph(status=PackagePublishingStatus.SUPERSEDED)
        self._makeSpph(status=PackagePublishingStatus.DELETED)
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(2, len(result))

    def test_getRecentSourceUploads_distro_archive_only(self):
        self._makeSpph()
        ppa = self.factory.makeArchive(
            distribution=self.distroseries.distribution,
            purpose=ArchivePurpose.PPA,
        )
        self._makeSpph(archive=ppa)
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))

    def test_getRecentSourceUploads_limit(self):
        for _ in range(25):
            self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(20, len(result))

    def test_getRecentSourceUploads_ordering(self):
        now = datetime.now(timezone.utc)
        spph_old = self._makeSpph(
            date_uploaded=now - timedelta(days=2),
        )
        spph_new = self._makeSpph(
            date_uploaded=now,
        )
        spph_mid = self._makeSpph(
            date_uploaded=now - timedelta(days=1),
        )
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(3, len(result))
        self.assertEqual(
            spph_new.sourcepackagerelease.version, result[0]["version"]
        )
        self.assertEqual(
            spph_mid.sourcepackagerelease.version, result[1]["version"]
        )
        self.assertEqual(
            spph_old.sourcepackagerelease.version, result[2]["version"]
        )

    def test_getRecentSourceUploads_dict_shape(self):
        self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))
        row = result[0]
        self.assertIn("name", row)
        self.assertIn("version", row)
        self.assertIn("pocket_title", row)
        self.assertIn("builds", row)
        self.assertIsInstance(row["builds"], list)

    def test_getRecentSourceUploads_pocket_title(self):
        self._makeSpph(pocket=PackagePublishingPocket.PROPOSED)
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual("Proposed", result[0]["pocket_title"])

    def test_getRecentSourceUploads_no_builds(self):
        self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual([], result[0]["builds"])

    def test_getRecentSourceUploads_with_builds(self):
        spph = self._makeSpph()
        das1 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="amd64"
        )
        das2 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="arm64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das1,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das2,
            archive=spph.archive,
            status=BuildStatus.FAILEDTOBUILD,
        )
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))
        builds = result[0]["builds"]
        self.assertEqual(2, len(builds))
        # Ordered by arch_tag ascending.
        self.assertEqual("amd64", builds[0]["arch_tag"])
        self.assertEqual(BuildStatus.FULLYBUILT, builds[0]["build_status"])
        self.assertEqual("arm64", builds[1]["arch_tag"])
        self.assertEqual(BuildStatus.FAILEDTOBUILD, builds[1]["build_status"])

    def test_getRecentSourceUploads_with_in_progress_builds(self):
        spph = self._makeSpph()
        das_amd64 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="amd64"
        )
        das_arm64 = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="arm64"
        )
        das_riscv = self.factory.makeDistroArchSeries(
            distroseries=self.distroseries, architecturetag="riscv64"
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_amd64,
            archive=spph.archive,
            status=BuildStatus.FULLYBUILT,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_arm64,
            archive=spph.archive,
            status=BuildStatus.BUILDING,
        )
        self.factory.makeBinaryPackageBuild(
            source_package_release=spph.sourcepackagerelease,
            distroarchseries=das_riscv,
            archive=spph.archive,
            status=BuildStatus.NEEDSBUILD,
        )
        result = self.publishing_set.getRecentSourceUploads(self.distroseries)
        self.assertEqual(1, len(result))
        builds = result[0]["builds"]
        self.assertEqual(3, len(builds))
        # Ordered by arch_tag ascending.
        self.assertEqual("amd64", builds[0]["arch_tag"])
        self.assertEqual(BuildStatus.FULLYBUILT, builds[0]["build_status"])
        self.assertEqual("arm64", builds[1]["arch_tag"])
        self.assertEqual(BuildStatus.BUILDING, builds[1]["build_status"])
        self.assertEqual("riscv64", builds[2]["arch_tag"])
        self.assertEqual(BuildStatus.NEEDSBUILD, builds[2]["build_status"])

    def test_getRecentSourceUploads_person_filter(self):
        person = self.factory.makePerson()
        other = self.factory.makePerson()
        self._makeSpph(creator=person)
        self._makeSpph(creator=other)
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, creator=person
        )
        self.assertEqual(1, len(result))

    def test_getRecentSourceUploads_person_no_uploads(self):
        person = self.factory.makePerson()
        self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, creator=person
        )
        self.assertEqual([], result)

    def test_getRecentSourceUploads_person_none_returns_all(self):
        self._makeSpph(creator=self.factory.makePerson())
        self._makeSpph(creator=self.factory.makePerson())
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, creator=None
        )
        self.assertEqual(2, len(result))

    def test_getRecentSourceUploads_offset_skips_records(self):
        now = datetime.now(timezone.utc)
        for i in range(5):
            self._makeSpph(date_uploaded=now - timedelta(days=i))
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=2
        )
        self.assertEqual(3, len(result))

    def test_getRecentSourceUploads_offset_preserves_order(self):
        now = datetime.now(timezone.utc)
        spphs = [
            self._makeSpph(date_uploaded=now - timedelta(days=i))
            for i in range(4)
        ]
        # Full result ordered newest-first: [0, 1, 2, 3]
        # offset=1 should return [1, 2, 3]
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=1
        )
        self.assertEqual(3, len(result))
        self.assertEqual(
            spphs[1].sourcepackagerelease.version, result[0]["version"]
        )
        self.assertEqual(
            spphs[3].sourcepackagerelease.version, result[2]["version"]
        )

    def test_getRecentSourceUploads_offset_beyond_end_returns_empty(self):
        for _ in range(3):
            self._makeSpph()
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=10
        )
        self.assertEqual([], result)

    def test_getRecentSourceUploads_offset_and_limit(self):
        now = datetime.now(timezone.utc)
        spphs = [
            self._makeSpph(date_uploaded=now - timedelta(days=i))
            for i in range(6)
        ]
        # offset=2, limit=2 should return records [2, 3]
        result = self.publishing_set.getRecentSourceUploads(
            self.distroseries, offset=2, limit=2
        )
        self.assertEqual(2, len(result))
        self.assertEqual(
            spphs[2].sourcepackagerelease.version, result[0]["version"]
        )
        self.assertEqual(
            spphs[3].sourcepackagerelease.version, result[1]["version"]
        )
