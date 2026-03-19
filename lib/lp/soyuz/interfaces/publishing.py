# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Publishing interfaces."""

__all__ = [
    "DeletionError",
    "IArchiveSafePublisher",
    "IBinaryPackagePublishingHistory",
    "IBinaryPackagePublishingHistoryEdit",
    "IBinaryPackagePublishingHistoryPublic",
    "IgnorableArtifactoryPoolFileOverwriteError",
    "IPublishingEdit",
    "IPublishingSet",
    "ISourcePackagePublishingHistory",
    "ISourcePackagePublishingHistoryEdit",
    "ISourcePackagePublishingHistoryPublic",
    "MissingSymlinkInPool",
    "NotInPool",
    "OverrideError",
    "PoolFileOverwriteError",
    "SourceUploadBuildInfo",
    "SourceUploadInfo",
    "active_publishing_status",
    "inactive_publishing_status",
    "name_priority_map",
]

import http.client
from typing import List, TypedDict

from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    error_status,
    export_operation_as,
    export_read_operation,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
    operation_returns_collection_of,
    operation_returns_entry,
)
from lazr.restful.fields import Reference
from zope.interface import Attribute, Interface
from zope.schema import Bool, Choice, Date, Datetime, Int, Text, TextLine

from lp import _
from lp.buildmaster.enums import BuildStatus
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.person import IPerson
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.sourcepackage import SourcePackageType
from lp.soyuz.enums import (
    BinaryPackageFormat,
    PackagePublishingPriority,
    PackagePublishingStatus,
)
from lp.soyuz.interfaces.binarypackagerelease import (
    IBinaryPackageReleaseDownloadCount,
)

#
# Exceptions
#


class NotInPool(Exception):
    """Raised when an attempt is made to remove a non-existent file."""


class PoolFileOverwriteError(Exception):
    """Raised when an attempt is made to overwrite a file in the pool.

    The proposed file has different content as the one in pool.
    This exception is unexpected and when it happens we keep the original
    file in pool and print a warning in the publisher log. It probably
    requires manual intervention in the archive.
    """


class IgnorableArtifactoryPoolFileOverwriteError(Exception):
    """Raised for an ignorable attempt to overwrite a file in Artifactory.

    Artifactory publishing model has known limitations in modelling the
    relationship between a source and a binary package for non-deb
    packages (for example, Python) published on multiple architectures. This
    causes too many exceptions to be raised in each publisher run and often
    floods the OOPS system. Due to this, we need to ignore some of these
    overwrite attempts and not raise an exception. This exception class is
    used solely for that purpose.
    """


class MissingSymlinkInPool(Exception):
    """Raised when there is a missing symlink in pool.

    This condition is ignored, similarly to what we do for `NotInPool`,
    since the pool entry requested to be removed is not there anymore.

    The corresponding record is marked as removed and the process
    continues.
    """


@error_status(http.client.BAD_REQUEST)
class OverrideError(Exception):
    """Raised when an attempt to change an override fails."""


@error_status(http.client.BAD_REQUEST)
class DeletionError(Exception):
    """Raised when an attempt to delete a publication fails."""


name_priority_map = {
    "required": PackagePublishingPriority.REQUIRED,
    "important": PackagePublishingPriority.IMPORTANT,
    "standard": PackagePublishingPriority.STANDARD,
    "optional": PackagePublishingPriority.OPTIONAL,
    "extra": PackagePublishingPriority.EXTRA,
    "": None,
}


#
# Base Interfaces
#


class IArchiveSafePublisher(Interface):
    """Safe Publication methods"""

    def setPublished():
        """Set a publishing record to published.

        Basically set records to PUBLISHED status only when they
        are PENDING and do not update datepublished value of already
        published field when they were checked via 'careful'
        publishing.
        """


class IPublishingView(Interface):
    """Base interface for all Publishing classes"""

    files = Attribute("Files included in this publication.")
    displayname = exported(
        TextLine(
            title=_("Display Name"),
            description=_("Text representation of the current record."),
        ),
        exported_as="display_name",
    )
    age = Attribute("Age of the publishing record.")

    component_name = exported(
        TextLine(title=_("Component Name"), required=False, readonly=True)
    )
    section_name = exported(
        TextLine(title=_("Section Name"), required=False, readonly=True)
    )

    pool_name = TextLine(
        title="Name to use when publishing this record in the pool."
    )
    pool_version = TextLine(
        title="Version to use when publishing this record in the pool."
    )

    def publish(diskpool, log):
        """Publish or ensure contents of this publish record

        Skip records which attempt to overwrite the archive (same file paths
        with different content) and do not update the database.

        If all the files get published correctly update its status properly.
        """

    def requestObsolescence():
        """Make this publication obsolete.

        :return: The obsoleted publishing record, either:
            `ISourcePackagePublishingHistory` or
            `IBinaryPackagePublishingHistory`.
        """


class IPublishingEdit(Interface):
    """Base interface for writeable Publishing classes."""

    def requestDeletion(removed_by, removal_comment=None):
        """Delete this publication.

        :param removed_by: `IPerson` responsible for the removal.
        :param removal_comment: optional text describing the removal reason.
        """

    @call_with(removed_by=REQUEST_USER)
    @operation_parameters(
        removal_comment=TextLine(title=_("Removal comment"), required=False)
    )
    @export_operation_as("requestDeletion")
    @export_write_operation()
    @operation_for_version("beta")
    def api_requestDeletion(removed_by, removal_comment=None):
        """Delete this source and its binaries.

        :param removed_by: `IPerson` responsible for the removal.
        :param removal_comment: optional text describing the removal reason.
        """
        # This is a special API method that allows a different code path
        # to the regular requestDeletion().  In the case of sources
        # getting deleted, it ensures source and binaries are both
        # deleted in tandem.


#
# Source package publishing
#


class ISourcePackagePublishingHistoryPublic(IPublishingView):
    """A source package publishing history record."""

    id = Int(
        title=_("ID"),
        required=True,
        readonly=True,
    )
    sourcepackagename_id = Int(
        title=_("The DB id for the sourcepackagename."),
        required=False,
        readonly=False,
    )
    sourcepackagename = Attribute("The source package name being published")
    sourcepackagerelease_id = Int(
        title=_("The DB id for the sourcepackagerelease."),
        required=False,
        readonly=False,
    )
    sourcepackagerelease = Attribute(
        "The source package release being published"
    )
    format = Choice(
        title=_("Source package format"),
        vocabulary=SourcePackageType,
        required=True,
        readonly=True,
    )
    status = exported(
        Choice(
            title=_("Package Publishing Status"),
            description=_("The status of this publishing record"),
            vocabulary=PackagePublishingStatus,
            required=False,
            readonly=False,
        )
    )
    distroseries_id = Attribute("DB ID for distroseries.")
    distroseries = exported(
        Reference(
            IDistroSeries,
            title=_("The distro series being published into"),
            required=False,
            readonly=False,
        ),
        exported_as="distro_series",
    )
    component = Int(
        title=_("The component being published into"),
        required=False,
        readonly=False,
    )
    section_id = Attribute("DB ID for the section")
    section = Int(
        title=_("The section being published into"),
        required=False,
        readonly=False,
    )
    datepublished = exported(
        Datetime(
            title=_("The date on which this record was published"),
            required=False,
            readonly=False,
        ),
        exported_as="date_published",
    )
    scheduleddeletiondate = exported(
        Datetime(
            title=_(
                "The date on which this record is scheduled for " "deletion"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="scheduled_deletion_date",
    )
    pocket = exported(
        Choice(
            title=_("Pocket"),
            description=_("The pocket into which this entry is published"),
            vocabulary=PackagePublishingPocket,
            required=True,
            readonly=True,
        )
    )
    channel = TextLine(
        title=_("Channel"),
        required=False,
        readonly=False,
        description=_(
            "The channel into which this entry is published "
            "(only for archives published using Artifactory)"
        ),
    )
    archive = exported(
        Reference(
            # Really IArchive, patched in lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Archive ID"),
            required=True,
            readonly=True,
        )
    )
    copied_from_archive = exported(
        Reference(
            # Really IArchive, patched in lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Original archive ID where this package was copied from."),
            required=False,
            readonly=True,
        )
    )
    supersededby = Int(
        title=_("The sourcepackagerelease which superseded this one"),
        required=False,
        readonly=False,
    )
    datesuperseded = exported(
        Datetime(
            title=_("The date on which this record was marked superseded"),
            required=False,
            readonly=False,
        ),
        exported_as="date_superseded",
    )
    datecreated = exported(
        Datetime(
            title=_("The date on which this record was created"),
            required=True,
            readonly=False,
        ),
        exported_as="date_created",
    )
    datemadepending = exported(
        Datetime(
            title=_(
                "The date on which this record was set as pending " "removal"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="date_made_pending",
    )
    dateremoved = exported(
        Datetime(
            title=_(
                "The date on which this record was removed from the "
                "published set"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="date_removed",
    )
    removed_by_id = Attribute("DB ID for removed_by.")
    removed_by = exported(
        Reference(
            IPerson,
            title=_("The IPerson responsible for the removal"),
            required=False,
            readonly=False,
        )
    )
    removal_comment = exported(
        Text(
            title=_("Reason why this publication is going to be removed."),
            required=False,
            readonly=False,
        )
    )

    meta_sourcepackage = Attribute(
        "Return an ISourcePackage meta object correspondent to the "
        "sourcepackagerelease attribute inside a specific distroseries"
    )
    meta_distributionsourcepackagerelease = Attribute(
        "Return an IDistributionSourcePackageRelease meta object "
        "correspondent to the sourcepackagerelease attribute inside "
        "this distribution"
    )

    source_package_name = exported(
        TextLine(title=_("Source Package Name"), required=False, readonly=True)
    )
    source_package_version = exported(
        TextLine(
            title=_("Source Package Version"), required=False, readonly=True
        )
    )

    package_creator = exported(
        Reference(
            IPerson,
            title=_("Package Creator"),
            description=_("The IPerson who created the source package."),
            required=False,
            readonly=True,
        )
    )
    package_maintainer = exported(
        Reference(
            IPerson,
            title=_("Package Maintainer"),
            description=_("The IPerson who maintains the source package."),
            required=False,
            readonly=True,
        )
    )
    package_signer = exported(
        Reference(
            IPerson,
            title=_("Package Signer"),
            description=_("The IPerson who signed the source package."),
            required=False,
            readonly=True,
        )
    )

    newer_distroseries_version = Attribute(
        "An `IDistroSeriosSourcePackageRelease` with a newer version of this "
        "package that has been published in the main distribution series, "
        "if one exists, or None."
    )

    ancestor = Reference(
        # Really ISourcePackagePublishingHistory, patched in
        # lp.soyuz.interfaces.webservice.
        Interface,
        title=_("Ancestor"),
        description=_("The previous release of this source package."),
        required=False,
        readonly=True,
    )

    creator_id = Attribute("DB ID for creator.")
    creator = exported(
        Reference(
            IPerson,
            title=_("Publication Creator"),
            description=_("The IPerson who created this publication."),
            required=False,
            readonly=True,
        )
    )

    sponsor_id = Attribute("DB ID for sponsor.")
    sponsor = exported(
        Reference(
            IPerson,
            title=_("Publication sponsor"),
            description=_(
                "The IPerson who sponsored the creation of "
                "this publication."
            ),
            required=False,
            readonly=True,
        )
    )

    packageupload = exported(
        Reference(
            # Really IPackageUpload, patched in lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Package upload"),
            description=_(
                "The Package Upload that caused the creation of "
                "this publication."
            ),
            required=False,
            readonly=True,
        )
    )

    @operation_parameters(
        active_binaries_only=Bool(
            title=_("Only return active publications"), required=False
        )
    )
    # Really IBinaryPackagePublishingHistory, patched in
    # lp.soyuz.interfaces.webservice.
    @operation_returns_collection_of(Interface)
    @export_read_operation()
    @operation_for_version("beta")
    def getPublishedBinaries(active_binaries_only=True):
        """Return all resulted `IBinaryPackagePublishingHistory`.

        Follow the build record and return every binary publishing record
        for any `DistroArchSeries` in this `DistroSeries` and in the same
        `IArchive` and Pocket, ordered by architecture tag.  If
        `active_binaries_only` is True (the default), then only return
        PUBLISHED or PENDING binary publishing records.

        :param active_binaries_only: If True, only return PUBLISHED or
            PENDING publishing records.
        :return: a list with all corresponding publishing records.
        """

    def getBuiltBinaries():
        """Return all unique binary publications built by this source.

        Follow the build record and return every unique binary publishing
        record in the context `DistroSeries` and in the same `IArchive`
        and Pocket.

        There will be only one entry for architecture independent binary
        publications.

        :return: a list containing all unique
            `IBinaryPackagePublishingHistory`.
        """

    @export_read_operation()
    @operation_for_version("devel")
    def hasRestrictedFiles():
        """Return whether or not a given source files has restricted files."""

    # Really IBinaryPackageBuild, patched in lp.soyuz.interfaces.webservice.
    @operation_returns_collection_of(Interface)
    @export_read_operation()
    @operation_for_version("beta")
    def getBuilds():
        """Return a list of `IBuild` objects in this publishing context.

        The builds are ordered by `DistroArchSeries.architecturetag`.

        :return: a list of `IBuilds`.
        """

    def getFileByName(name):
        """Return the file with the specified name.

        Only supports 'changelog' at present.
        """

    @export_read_operation()
    @operation_for_version("beta")
    def changesFileUrl():
        """The .changes file URL for this source publication.

        :return: the .changes file URL for this source (a string).
        """

    @export_read_operation()
    @operation_for_version("devel")
    def changelogUrl():
        """The URL for this source package release's changelog.

        :return: the changelog file URL for this source (a string).
        """

    def createMissingBuilds(architectures_available=None, logger=None):
        """Create missing Build records for a published source.

        :param architectures_available: options list of `DistroArchSeries`
            that should be considered for build creation; if not given
            it will be calculated in place, all architectures for the
            context distroseries with available chroot.
        :param logger: optional context Logger object (used on DEBUG level).

        :return: a list of `Builds` created for this source publication.
        """

    def getSourceAndBinaryLibraryFiles():
        """Return a list of `LibraryFileAlias` for all source and binaries.

        All the source files and all binary files ever published to the
        same archive context are returned as a list of LibraryFileAlias
        records.

        :return: a list of `ILibraryFileAlias`.
        """

    def supersede(dominant=None, logger=None):
        """Supersede this publication.

        :param dominant: optional `ISourcePackagePublishingHistory` which is
            triggering the domination.
        :param logger: optional object to which debug information will be
            logged.
        """

    def copyTo(distroseries, pocket, archive, overrides=None, creator=None):
        """Copy this publication to another location.

        :param distroseries: The `IDistroSeries` to copy the source
            publication into.
        :param pocket: The `PackagePublishingPocket` to copy into.
        :param archive: The `IArchive` to copy the source publication into.
        :param overrides: A tuple of override data as returned from a
            `IOverridePolicy`.
        :param creator: the `IPerson` to use as the creator for the copied
            publication.
        :param packageupload: The `IPackageUpload` that caused this
            publication to be created.

        :return: a `ISourcePackagePublishingHistory` record representing the
            source in the destination location.
        """

    def getStatusSummaryForBuilds():
        """Return a summary of the build status for the related builds.

        This method augments IBuildSet.getBuildStatusSummaryForBuilds() by
        additionally checking to see if all the builds have been published
        before returning the fully-built status.

        :return: A dict consisting of the build status summary for the
            related builds. For example:
                {
                    'status': PackagePublishingStatus.PENDING,
                    'builds': [build1, build2]
                }
        """

    @export_read_operation()
    @operation_parameters(
        include_meta=Bool(title=_("Include Metadata"), required=False)
    )
    @operation_for_version("beta")
    def sourceFileUrls(include_meta=False):
        """URLs for this source publication's uploaded source files.

        :param include_meta: Return a list of dicts with keys url, size, and
            sha256 for each URL instead of a simple list.
        :return: A collection of URLs for this source.
        """

    @export_read_operation()
    @operation_for_version("beta")
    def binaryFileUrls():
        """URLs for this source publication's binary files.

        :return: A collection of URLs for this source.
        """

    @export_read_operation()
    @operation_parameters(
        to_version=TextLine(title=_("To Version"), required=True)
    )
    @operation_for_version("beta")
    def packageDiffUrl(to_version):
        """URL of the debdiff file between this and the supplied version.

        :param to_version: The version of the source package for which you
            want to get the diff to.
        :return: A URL to the librarian file containing the diff.
        """


class ISourcePackagePublishingHistoryEdit(IPublishingEdit):
    """A writeable source package publishing history record."""

    # Really ISourcePackagePublishingHistory, patched in
    # lp.soyuz.interfaces.webservice.
    @operation_returns_entry(Interface)
    @operation_parameters(
        new_component=TextLine(title="The new component name."),
        new_section=TextLine(title="The new section name."),
    )
    @export_write_operation()
    @call_with(creator=REQUEST_USER)
    @operation_for_version("devel")
    def changeOverride(new_component=None, new_section=None, creator=None):
        """Change the component and/or section of this publication.

        It is changed only if the argument is not None.

        Return the overridden publishing record, a
        `ISourcePackagePublishingHistory`.
        """


@exported_as_webservice_entry(as_of="beta", publish_web_link=False)
class ISourcePackagePublishingHistory(
    ISourcePackagePublishingHistoryPublic, ISourcePackagePublishingHistoryEdit
):
    """A source package publishing history record."""


#
# Binary package publishing
#


class IBinaryPackagePublishingHistoryPublic(IPublishingView):
    """A binary package publishing record."""

    id = Int(title=_("ID"), required=True, readonly=True)
    binarypackagename_id = Int(
        title=_("The DB id for the binarypackagename."),
        required=False,
        readonly=False,
    )
    binarypackagename = Attribute("The binary package name being published")
    binarypackagerelease_id = Int(
        title=_("The DB id for the binarypackagerelease."),
        required=False,
        readonly=False,
    )
    binarypackagerelease = Attribute(
        "The binary package release being published"
    )
    binarypackageformat = Choice(
        title=_("Binary package format"),
        vocabulary=BinaryPackageFormat,
        required=True,
        readonly=True,
    )
    # This and source_package_version are exported here to
    # avoid clients needing to indirectly look this up via a build.
    # This can cause security errors due to the differing levels of access.
    # Exporting here allows the lookup to happen internally.
    source_package_name = exported(
        TextLine(
            title=_("Source Package Name"),
            description=_("The source package name that built this binary."),
            required=False,
            readonly=True,
        )
    )
    source_package_version = exported(
        TextLine(
            title=_("Source Package Version"),
            description=_(
                "The source package version that built this binary."
            ),
            required=False,
            readonly=True,
        )
    )
    distroarchseries_id = Int(
        title=_("The DB id for the distroarchseries."),
        required=False,
        readonly=False,
    )
    distroarchseries = exported(
        Reference(
            # Really IDistroArchSeries, patched in
            # lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Distro Arch Series"),
            description=_("The distroarchseries being published into"),
            required=False,
            readonly=False,
        ),
        exported_as="distro_arch_series",
    )
    distroseries = Attribute("The distroseries being published into")
    component = Int(
        title=_("The component being published into"),
        required=False,
        readonly=False,
    )
    section = Int(
        title=_("The section being published into"),
        required=False,
        readonly=False,
    )
    priority = Int(
        title=_("The priority being published into"),
        required=False,
        readonly=False,
    )
    phased_update_percentage = exported(
        Int(
            title=_(
                "The percentage of users for whom this package should be "
                "recommended, or None to publish the update for everyone"
            ),
            required=False,
            readonly=True,
        )
    )
    datepublished = exported(
        Datetime(
            title=_("Date Published"),
            description=_("The date on which this record was published"),
            required=False,
            readonly=False,
        ),
        exported_as="date_published",
    )
    scheduleddeletiondate = exported(
        Datetime(
            title=_("Scheduled Deletion Date"),
            description=_(
                "The date on which this record is scheduled for " "deletion"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="scheduled_deletion_date",
    )
    status = exported(
        Choice(
            title=_("Status"),
            description=_("The status of this publishing record"),
            vocabulary=PackagePublishingStatus,
            required=False,
            readonly=False,
        )
    )
    pocket = exported(
        Choice(
            title=_("Pocket"),
            description=_("The pocket into which this entry is published"),
            vocabulary=PackagePublishingPocket,
            required=True,
            readonly=True,
        )
    )
    channel = TextLine(
        title=_("Channel"),
        required=False,
        readonly=False,
        description=_(
            "The channel into which this entry is published "
            "(only for archives published using Artifactory)"
        ),
    )
    supersededby = Int(
        title=_("The build which superseded this one"),
        required=False,
        readonly=False,
    )
    creator = exported(
        Reference(
            IPerson,
            title=_("Publication Creator"),
            description=_("The IPerson who created this publication."),
            required=False,
            readonly=True,
        )
    )
    datecreated = exported(
        Datetime(
            title=_("Date Created"),
            description=_("The date on which this record was created"),
            required=True,
            readonly=False,
        ),
        exported_as="date_created",
    )
    datesuperseded = exported(
        Datetime(
            title=_("Date Superseded"),
            description=_(
                "The date on which this record was marked superseded"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="date_superseded",
    )
    datemadepending = exported(
        Datetime(
            title=_("Date Made Pending"),
            description=_(
                "The date on which this record was set as pending removal"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="date_made_pending",
    )
    dateremoved = exported(
        Datetime(
            title=_("Date Removed"),
            description=_(
                "The date on which this record was removed from the "
                "published set"
            ),
            required=False,
            readonly=False,
        ),
        exported_as="date_removed",
    )
    archive = exported(
        Reference(
            # Really IArchive, patched in lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Archive"),
            description=_("The context archive for this publication."),
            required=True,
            readonly=True,
        )
    )
    copied_from_archive = exported(
        Reference(
            # Really IArchive, patched in lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Original archive ID where this package was copied from."),
            required=False,
            readonly=True,
        )
    )
    removed_by = exported(
        Reference(
            IPerson,
            title=_("Removed By"),
            description=_("The Person responsible for the removal"),
            required=False,
            readonly=False,
        )
    )
    removal_comment = exported(
        Text(
            title=_("Removal Comment"),
            description=_(
                "Reason why this publication is going to be removed."
            ),
            required=False,
            readonly=False,
        )
    )

    distroarchseriesbinarypackagerelease = Attribute(
        "The object that "
        "represents this binarypackagerelease in this distroarchseries."
    )

    binary_package_name = exported(
        TextLine(title=_("Binary Package Name"), required=False, readonly=True)
    )
    binary_package_version = exported(
        TextLine(
            title=_("Binary Package Version"), required=False, readonly=True
        )
    )
    build = exported(
        Reference(
            # Really IBinaryPackageBuild, fixed in
            # lp.soyuz.interfaces.webservice.
            Interface,
            title=_("Build"),
            description=_("The build that produced this binary package."),
            required=True,
            readonly=True,
        )
    )
    architecture_specific = exported(
        Bool(title=_("Architecture Specific"), required=False, readonly=True)
    )
    priority_name = exported(
        TextLine(title=_("Priority Name"), required=False, readonly=True)
    )
    is_debug = exported(
        Bool(
            title=_("Debug Package"),
            description=_("Is this a debug package publication?"),
            required=False,
            readonly=True,
        ),
        as_of="devel",
    )
    sourcepackagename = Attribute(
        "The source package name that built this binary."
    )

    def getOtherPublications():
        """Return remaining publications with the same overrides.

        Only considers binary publications in the same archive, distroseries,
        pocket, component, section, priority and phased-update-percentage
        context. These publications are candidates for domination if this is
        an architecture-independent package.

        The override match is critical -- it prevents a publication created
        by new overrides from superseding itself.
        """

    def supersede(dominant=None, logger=None):
        """Supersede this publication.

        :param dominant: optional `IBinaryPackagePublishingHistory` which is
            triggering the domination.
        :param logger: optional object to which debug information will be
            logged.
        """

    def copyTo(distroseries, pocket, archive):
        """Copy this publication to another location.

        Architecture independent binary publications are copied to all
        supported architectures in the destination distroseries.

        :return: a list of `IBinaryPackagePublishingHistory` records
            representing the binaries copied to the destination location.
        """

    @export_read_operation()
    @operation_for_version("beta")
    def getDownloadCount():
        """Get the download count of this binary package in this archive.

        This is currently only meaningful for PPAs."""

    @operation_parameters(
        start_date=Date(title=_("Start date"), required=False),
        end_date=Date(title=_("End date"), required=False),
    )
    @operation_returns_collection_of(IBinaryPackageReleaseDownloadCount)
    @export_read_operation()
    @operation_for_version("beta")
    def getDownloadCounts(start_date=None, end_date=None):
        """Get detailed download counts for this binary.

        :param start_date: The optional first date to return.
        :param end_date: The optional last date to return.
        """

    @operation_parameters(
        start_date=Date(title=_("Start date"), required=False),
        end_date=Date(title=_("End date"), required=False),
    )
    @export_read_operation()
    @operation_for_version("beta")
    def getDailyDownloadTotals(start_date=None, end_date=None):
        """Get the daily download counts for this binary.

        :param start_date: The optional first date to return.
        :param end_date: The optional last date to return.
        """

    @export_read_operation()
    @operation_parameters(
        include_meta=Bool(title=_("Include Metadata"), required=False)
    )
    @operation_for_version("devel")
    def binaryFileUrls(include_meta=False):
        """URLs for this binary publication's binary files.

        :param include_meta: Return a list of dicts with keys url, size,
            sha1, and sha256 for each URL instead of a simple list.
        :return: A collection of URLs for this binary.
        """


class IBinaryPackagePublishingHistoryEdit(IPublishingEdit):
    """A writeable binary package publishing record."""

    # Really IBinaryPackagePublishingHistory, patched in
    # lp.soyuz.interfaces.webservice.
    @operation_returns_entry(Interface)
    @operation_parameters(
        new_component=TextLine(title="The new component name."),
        new_section=TextLine(title="The new section name."),
        # XXX cjwatson 20120619: It would be nice to use copy_field here to
        # save manually looking up the priority name, but it doesn't work in
        # this case: the title is wrong, and tests fail when a string value
        # is passed over the webservice.
        new_priority=TextLine(title="The new priority name."),
        new_phased_update_percentage=Int(
            title="The new phased update percentage."
        ),
    )
    @export_write_operation()
    @call_with(creator=REQUEST_USER)
    @operation_for_version("devel")
    def changeOverride(
        new_component=None,
        new_section=None,
        new_priority=None,
        new_phased_update_percentage=None,
        creator=None,
    ):
        """Change the component/section/priority/phase of this publication.

        It is changed only if the argument is not None.

        Passing new_phased_update_percentage=100 has the effect of setting
        the phased update percentage to None (i.e. recommended for all
        users).

        Return the overridden publishing record, a
        `IBinaryPackagePublishingHistory`.
        """


@exported_as_webservice_entry(as_of="beta", publish_web_link=False)
class IBinaryPackagePublishingHistory(
    IBinaryPackagePublishingHistoryPublic, IBinaryPackagePublishingHistoryEdit
):
    """A binary package publishing record."""


class SourceUploadBuildInfo(TypedDict):
    """Build status for one architecture of a source upload."""

    arch_tag: str
    build_status: BuildStatus


class SourceUploadInfo(TypedDict):
    """Data for one row in the recent source uploads list."""

    name: str
    version: str
    pocket_title: str
    builds: List[SourceUploadBuildInfo]


class IPublishingSet(Interface):
    """Auxiliary methods for dealing with sets of publications."""

    def publishBinaries(
        archive, distroseries, pocket, binaries, copied_from_archives=None
    ):
        """Efficiently publish multiple BinaryPackageReleases in an Archive.

        Creates `IBinaryPackagePublishingHistory` records for each
        binary, handling architecture-independent, avoiding creation of
        duplicate publications, and leaving disabled architectures
        alone.

        :param archive: The target `IArchive`.
        :param distroseries: The target `IDistroSeries`.
        :param pocket: The target `PackagePublishingPocket`.
        :param binaries: A dict mapping `BinaryPackageReleases` to their
            desired overrides as (`Component`, `Section`,
            `PackagePublishingPriority`, `phased_update_percentage`) tuples.
        :param copied_from_archives: A dict mapping `BinaryPackageReleases`
            to their original archives (for copy operations).

        :return: A list of new `IBinaryPackagePublishingHistory` records.
        """

    def copyBinaries(archive, distroseries, pocket, bpphs, policy=None):
        """Copy multiple binaries to a given destination.

        Efficiently copies the given `IBinaryPackagePublishingHistory`
        records to a new archive and suite, optionally overriding the
        original publications' component, section and priority using an
        `IOverridePolicy`.

        :param archive: The target `IArchive`.
        :param distroseries: The target `IDistroSeries`.
        :param pocket: The target `PackagePublishingPocket`.
        :param binaries: A list of `IBinaryPackagePublishingHistory`s to copy.
        :param policy: An optional `IOverridePolicy` to apply to the copy.

        :return: A result set of the created `IBinaryPackagePublishingHistory`
            records.
        """

    def newSourcePublication(
        archive,
        sourcepackagerelease,
        distroseries,
        pocket,
        component=None,
        section=None,
        ancestor=None,
        create_dsd_job=True,
        copied_from_archive=None,
        creator=None,
        sponsor=None,
        packageupload=None,
        channel=None,
    ):
        """Create a new `SourcePackagePublishingHistory`.

        :param archive: An `IArchive`
        :param sourcepackagerelease: An `ISourcePackageRelease`
        :param distroseries: An `IDistroSeries`
        :param component: An `IComponent` (optional for SPR.format != DPKG)
        :param section: An `ISection` (optional for SPR.format != DPKG)
        :param pocket: A `PackagePublishingPocket`
        :param ancestor: A `ISourcePackagePublishingHistory` for the previous
            version of this publishing record
        :param create_dsd_job: A boolean indicating whether or not a dsd job
             should be created for the new source publication.
        :param copied_from_archive: For copy operations, this should be the
            source archive (from where this new publication is coming from).
        :param creator: An optional `IPerson`. If this is None, the
            sourcepackagerelease's creator will be used.
        :param sponsor: An optional `IPerson` indicating the sponsor of this
            publication.
        :param packageupload: An optional `IPackageUpload` that caused this
            publication to be created.

        datecreated will be UTC_NOW.
        status will be PackagePublishingStatus.PENDING
        """

    def getByIdAndArchive(id, archive, source=True):
        """Return the publication matching id AND archive.

        :param archive: The context `IArchive`.
        :param source: If true look for source publications, otherwise
            binary publications.
        """

    def getBuildsForSourceIds(source_ids, archive=None, build_states=None):
        """Return all builds related with each given source publication.

        The returned ResultSet contains entries with the wanted `Build`s
        associated with the corresponding source publication and its
        targeted `DistroArchSeries` in a 3-element tuple. This way the extra
        information will be cached and the callsites can group builds in
        any convenient form.

        The optional archive parameter, if provided, will ensure that only
        builds corresponding to the archive will be included in the results.

        The result is ordered by:

         1. Ascending `SourcePackagePublishingHistory.id`,
         2. Ascending `DistroArchSeries.architecturetag`.

        :param source_ids: list of or a single
            `SourcePackagePublishingHistory` object.
        :type source_ids: ``list`` or `SourcePackagePublishingHistory`
        :param archive: An optional archive with which to filter the source
            ids.
        :type archive: `IArchive`
        :param build_states: optional list of build states to which the
            result will be limited. Defaults to all states if omitted.
        :type build_states: ``list`` or None
        :param need_build_farm_job: whether to include the `PackageBuild`
            and `BuildFarmJob` in the result.
        :return: a storm ResultSet containing tuples as
            (`SourcePackagePublishingHistory`, `BinaryPackageBuild`,
            `DistroArchSeries`)
        :rtype: `storm.store.ResultSet`.
        """

    def getBuildsForSources(one_or_more_source_publications):
        """Return all builds related with each given source publication.

        Extracts the source ids from one_or_more_source_publications and
        calls getBuildsForSourceIds.
        """

    def getUnpublishedBuildsForSources(
        one_or_more_source_publications, build_states=None
    ):
        """Return all the unpublished builds for each source.

        :param one_or_more_source_publications: list of, or a single
            `SourcePackagePublishingHistory` object.
        :param build_states: list of build states to which the result should
            be limited. Defaults to BuildStatus.FULLYBUILT if none are
            specified.
        :return: a storm ResultSet containing tuples of
            (`SourcePackagePublishingHistory`, `BinaryPackageBuild`)
        """

    def getBinaryFilesForSources(one_or_more_source_publication):
        """Return binary files related to each given source publication.

        The returned ResultSet contains entries with the wanted
        `LibraryFileAlias`s (binaries only) associated with the
        corresponding source publication and its `LibraryFileContent`
        in a 3-element tuple. This way the extra information will be
        cached and the callsites can group files in any convenient form.

        :param one_or_more_source_publication: list of or a single
            `SourcePackagePublishingHistory` object.

        :return: a storm ResultSet containing triples as follows:
            (`SourcePackagePublishingHistory`, `LibraryFileAlias`,
             `LibraryFileContent`)
        """

    def getFilesForSources(one_or_more_source_publication):
        """Return all files related to each given source publication.

        The returned ResultSet contains entries with the wanted
        `LibraryFileAlias`s (source and binaries) associated with the
        corresponding source publication and its `LibraryFileContent`
        in a 3-element tuple. This way the extra information will be
        cached and the callsites can group files in any convenient form.

        Callsites should order this result after grouping by source,
        because SQL UNION can't be correctly ordered in SQL level.

        :param one_or_more_source_publication: list of or a single
            `SourcePackagePublishingHistory` object.

        :return: an *unordered* storm ResultSet containing tuples as
            (`SourcePackagePublishingHistory`, `LibraryFileAlias`,
             `LibraryFileContent`)
        """

    def getBinaryPublicationsForSources(one_or_more_source_publications):
        """Return all binary publication for the given source publications.

        The returned ResultSet contains entries with the wanted
        `BinaryPackagePublishingHistory`s associated with the corresponding
        source publication and its targeted `DistroArchSeries`,
        `BinaryPackageRelease` and `BinaryPackageName` in a 5-element tuple.
        This way the extra information will be cached and the callsites can
        group binary publications in any convenient form.

        The result is ordered by:

         1. Ascending `SourcePackagePublishingHistory.id`,
         2. Ascending `BinaryPackageName.name`,
         3. Ascending `DistroArchSeries.architecturetag`.
         4. Descending `BinaryPackagePublishingHistory.id`.

        :param one_or_more_source_publication: list of or a single
            `SourcePackagePublishingHistory` object.

        :return: a storm ResultSet containing tuples as
            (`SourcePackagePublishingHistory`,
             `BinaryPackagePublishingHistory`,
             `BinaryPackageRelease`, `BinaryPackageName`, `DistroArchSeries`)
        """

    def getBuiltPackagesSummaryForSourcePublication(source_publication):
        """Return a summary of the built packages for this source publication.

        For each built package from this published source, return a
        dictionary with keys "binarypackagename" and "summary", where
        the binarypackagename is unique (i.e. it ignores the same package
        published in more than one place/architecture.)
        """

    def getActiveArchSpecificPublications(
        sourcepackagerelease, archive, distroseries, pocket
    ):
        """Find architecture-specific binary publications for a source.

        For example, say source package release contains binary packages of:
         * "foo" for i386 (pending in i386)
         * "foo" for amd64 (published in amd64)
         * "foo-common" for the "all" architecture (pending or published in
           various real processor architectures)

        In that case, this search will return foo(i386) and foo(amd64).  The
        dominator uses this when figuring out whether foo-common can be
        superseded: we don't track dependency graphs, but we know that the
        architecture-specific "foo" releases are likely to depend on the
        architecture-independent foo-common release.

        :param sourcepackagerelease: The `SourcePackageRelease`.
        :param archive: The `Archive` to search.
        :param distroseries: The `DistroSeries` to search.
        :param pocket: The `PackagePublishingPocket` to search.
        :return: A Storm result set of active, architecture-specific
            `BinaryPackagePublishingHistory` objects for the source package
            release in the given `archive`, `distroseries`, and `pocket`.
        """

    def getSourcesForPublishing(
        archive, distroseries=None, pocket=None, component=None
    ):
        """Get source publications which are published in a given context.

        :param archive: The `Archive` to search.
        :param distroseries: The `DistroSeries` to search, or None.
        :param pocket: The `PackagePublishingPocket` to search, or None.
        :param component: The `Component` to search, or None.
        :return: A result set of `SourcePackagePublishingHistory` objects in
            the given context and with the `PUBLISHED` status, ordered by
            source package name, with associated publisher-relevant objects
            preloaded.
        """

    def getBinariesForPublishing(
        archive, distroarchseries=None, pocket=None, component=None
    ):
        """Get binary publications which are published in a given context.

        :param archive: The `Archive` to search.
        :param distroarchseries: The `DistroArchSeries` to search, or None.
        :param pocket: The `PackagePublishingPocket` to search, or None.
        :param component: The `Component` to search, or None.
        :return: A result set of `BinaryPackagePublishingHistory` objects in
            the given context and with the `PUBLISHED` status, ordered by
            binary package name, with associated publisher-relevant objects
            preloaded.
        """

    def getChangesFilesForSources(one_or_more_source_publications):
        """Return all changesfiles for each given source publication.

        The returned ResultSet contains entries with the wanted changesfiles
        as `LibraryFileAlias`es associated with the corresponding source
        publication and its corresponding `LibraryFileContent`,
        `PackageUpload` and `SourcePackageRelease` in a 5-element tuple.
        This way the extra information will be cached and the call sites can
        group changesfiles in any convenient form.

        The result is ordered by ascending `SourcePackagePublishingHistory.id`

        :param one_or_more_source_publication: list of or a single
            `SourcePackagePublishingHistory` object.

        :return: a storm ResultSet containing tuples as
            (`SourcePackagePublishingHistory`, `PackageUpload`,
             `SourcePackageRelease`, `LibraryFileAlias`, `LibraryFileContent`)
        """

    def getChangesFileLFA(spr):
        """The changes file for the given `SourcePackageRelease`.

        :param spr: the `SourcePackageRelease` for which to return the
            changes file `LibraryFileAlias`.

        :return: a `LibraryFileAlias` instance or None
        """

    def setMultipleDeleted(
        publication_class, ds, removed_by, removal_comment=None
    ):
        """Mark publications as deleted.

        This is a supporting operation for a deletion request.
        """

    def findCorrespondingDDEBPublications(pubs):
        """Find DDEB publications corresponding to a list of publications."""

    def requestDeletion(pub, removed_by, removal_comment=None):
        """Delete the source and binary publications specified.

        This method deletes the source publications passed via the first
        parameter as well as their associated binary publications, and any
        binary publications passed in.

        :param pubs: list of `SourcePackagePublishingHistory` and
            `BinaryPackagePublishingHistory` objects.
        :param removed_by: `IPerson` responsible for the removal.
        :param removal_comment: optional text describing the removal reason.

        :return: The deleted publishing record, either:
            `ISourcePackagePublishingHistory` or
            `IBinaryPackagePublishingHistory`.
        """

    def getBuildStatusSummariesForSourceIdsAndArchive(source_ids, archive):
        """Return a summary of the build statuses for source publishing ids.

        This method collects all the builds for the provided source package
        publishing history ids, and returns the build status summary for
        the builds associated with each source package.

        See the `getStatusSummaryForBuilds()` method of `IBuildSet`.for
        details of the summary.

        :param source_ids: A list of source publishing history record ids.
        :type source_ids: ``list``
        :param archive: The archive which will be used to filter the source
                        ids.
        :type archive: `IArchive`
        :return: A dict consisting of the overall status summaries for the
            given ids that belong in the archive. For example:
                {
                    18: {'status': 'succeeded'},
                    25: {'status': 'building', 'builds':[building_builds]},
                    35: {'status': 'failed', 'builds': [failed_builds]}
                }
        :rtype: ``dict``.
        """

    def getBuildStatusSummaryForSourcePublication(source_publication):
        """Return a summary of the build statuses for this source
        publication.

        See `ISourcePackagePublishingHistory`.getStatusSummaryForBuilds()
        for details. The call is just proxied here so that it can also be
        used with an ArchiveSourcePublication passed in as
        the source_package_pub, allowing the use of the cached results.
        """

    def getRecentSourceUploads(distroseries, creator=None, offset=0, limit=20):
        """Return recent active source package uploads for a distroseries.

        Returns active (PENDING or PUBLISHED) source package publishing
        records in the distro's primary archives, across all pockets,
        ordered by ``datecreated`` descending.

        :param distroseries: An `IDistroSeries`.
        :param creator: If given, only return uploads where this person
            is the creator (`SourcePackagePublishingHistory.creator`).
        :param offset: Number of records to skip. Defaults to 0.
        :param limit: Maximum number of records to return. Defaults to 20.
        :return: A list of `SourceUploadInfo` ordered by ``datecreated``
            descending. The ``builds`` list is empty for uploads that have
            no builds yet.
        """


active_publishing_status = (
    PackagePublishingStatus.PENDING,
    PackagePublishingStatus.PUBLISHED,
)


inactive_publishing_status = (
    PackagePublishingStatus.SUPERSEDED,
    PackagePublishingStatus.DELETED,
    PackagePublishingStatus.OBSOLETE,
)
