# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Archive Domination class.

We call 'domination' the procedure used to identify and supersede all
old versions for a given publication, source or binary, inside a suite
(distroseries + pocket, for instance, gutsy or gutsy-updates).

It also processes the superseded publications and makes the ones with
unnecessary files 'eligible for removal', which will then be considered
for archive removal.  See deathrow.py.

In order to judge if a source is 'eligible for removal' it also checks
if its resulting binaries are not necessary any more in the archive, i.e.,
old binary publications can (and should) hold sources in the archive.

Source version life-cycle example:

  * foo_2.1: currently published, source and binary files live in the archive
             pool and it is listed in the archive indexes.

  * foo_2.0: superseded, it's not listed in archive indexes but one of its
             files is used for foo_2.1 (the orig.tar.gz) or foo_2.1 could
             not build for one or more architectures that foo_2.0 could;

  * foo_1.8: eligible for removal, none of its files are required in the
             archive since foo_2.0 was published (new orig.tar.gz) and none
             of its binaries are published (foo_2.0 was completely built)

  * foo_1.0: removed, it already passed through the quarantine period and its
             files got removed from the archive.

Note that:

  * PUBLISHED and SUPERSEDED are publishing statuses.

  * 'eligible for removal' is a combination of SUPERSEDED or DELETED
    publishing status and a defined (non-empty) 'scheduleddeletiondate'.

  * 'removed' is a combination of 'eligible for removal' and a defined
    (non-empy) 'dateremoved'.

The 'domination' procedure is the 2nd step of the publication pipeline and
it is performed for each suite using:

  * judgeAndDominate(distroseries, pocket)

"""

__all__ = ["Dominator"]

from collections import defaultdict
from datetime import timedelta
from functools import cmp_to_key
from itertools import filterfalse
from operator import attrgetter, itemgetter

import apt_pkg
from storm.expr import And, Count, Desc, Exists, Not, Select
from storm.info import ClassAlias
from zope.component import getUtility

from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.database.bulk import load_related
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import (
    block_implicit_flushes,
    flush_database_updates,
)
from lp.services.database.stormexpr import IsDistinctFrom
from lp.services.orderingcheck import OrderingCheck
from lp.soyuz.adapters.packagelocation import PackageLocation
from lp.soyuz.enums import BinaryPackageFormat, PackagePublishingStatus
from lp.soyuz.interfaces.publishing import (
    IPublishingSet,
    inactive_publishing_status,
)
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.binarypackagename import BinaryPackageName
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
from lp.soyuz.model.distroarchseries import DistroArchSeries
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease

# Days before a package will be removed from disk.
STAY_OF_EXECUTION = 1


# Ugly, but works
apt_pkg.init_system()


def join_spph_spn():
    """Join condition: SourcePackagePublishingHistory/SourcePackageName."""
    SPPH = SourcePackagePublishingHistory
    SPN = SourcePackageName

    return SPN.id == SPPH.sourcepackagename_id


def join_spph_spr():
    """Join condition: SourcePackageRelease/SourcePackagePublishingHistory."""
    SPPH = SourcePackagePublishingHistory
    SPR = SourcePackageRelease

    return SPR.id == SPPH.sourcepackagerelease_id


class SourcePublicationTraits:
    """Basic generalized attributes for `SourcePackagePublishingHistory`.

    Used by `GeneralizedPublication` to hide the differences from
    `BinaryPackagePublishingHistory`.
    """

    @staticmethod
    def getPackageName(spph):
        """Return the name of this publication's source package."""
        return spph.sourcepackagename.name

    @staticmethod
    def getPackageRelease(spph):
        """Return this publication's `SourcePackageRelease`."""
        return spph.sourcepackagerelease


class BinaryPublicationTraits:
    """Basic generalized attributes for `BinaryPackagePublishingHistory`.

    Used by `GeneralizedPublication` to hide the differences from
    `SourcePackagePublishingHistory`.
    """

    @staticmethod
    def getPackageName(bpph):
        """Return the name of this publication's binary package."""
        return bpph.binarypackagename.name

    @staticmethod
    def getPackageRelease(bpph):
        """Return this publication's `BinaryPackageRelease`."""
        return bpph.binarypackagerelease


class GeneralizedPublication:
    """Generalize handling of publication records.

    This allows us to write code that can be dealing with either
    `SourcePackagePublishingHistory`s or `BinaryPackagePublishingHistory`s
    without caring which.  Differences are abstracted away in a traits
    class.
    """

    def __init__(self, is_source=True):
        self.is_source = is_source
        if is_source:
            self.traits = SourcePublicationTraits
        else:
            self.traits = BinaryPublicationTraits

    def getPackageName(self, pub):
        """Get the package's name."""
        return self.traits.getPackageName(pub)

    def getPackageVersion(self, pub):
        """Obtain the version string for a publication record."""
        return self.traits.getPackageRelease(pub).version

    def compare(self, pub1, pub2):
        """Compare publications by version.

        If both publications are for the same version, their creation dates
        break the tie.
        """
        version_comparison = apt_pkg.version_compare(
            self.getPackageVersion(pub1), self.getPackageVersion(pub2)
        )

        if version_comparison == 0:
            # Use dates as tie breaker (idiom equivalent to Python 2's cmp).
            return (pub1.datecreated > pub2.datecreated) - (
                pub1.datecreated < pub2.datecreated
            )
        else:
            return version_comparison

    def sortPublications(self, publications):
        """Sort publications from most to least current versions."""
        return sorted(publications, key=cmp_to_key(self.compare), reverse=True)


def make_package_location(pub):
    """Make a `PackageLocation` representing a publication."""
    return PackageLocation(
        archive=pub.archive,
        distribution=pub.distroseries.distribution,
        distroseries=pub.distroseries,
        pocket=pub.pocket,
        channel=pub.channel,
    )


def find_live_source_versions(sorted_pubs):
    """Find versions out of Published publications that should stay live.

    This particular notion of liveness applies to source domination: the
    latest version stays live, and that's it.

    :param sorted_pubs: An iterable of `SourcePackagePublishingHistory`
        sorted by descending package version.
    :return: A list of live versions.
    """
    # Given the required sort order, the latest version is at the head
    # of the list.
    return [sorted_pubs[0].sourcepackagerelease.version]


def get_binary_versions(binary_publications):
    """List versions for sequence of `BinaryPackagePublishingHistory`.

    :param binary_publications: An iterable of
        `BinaryPackagePublishingHistory`.
    :return: A list of the publications' respective versions.
    """
    return [pub.binarypackagerelease.version for pub in binary_publications]


def find_live_binary_versions_pass_1(sorted_pubs):
    """Find versions out of Published `publications` that should stay live.

    This particular notion of liveness applies to first-pass binary
    domination: the latest version stays live, and so do publications of
    binary packages for the "all" architecture.

    :param sorted_pubs: An iterable of `BinaryPackagePublishingHistory`,
        sorted by descending package version.
    :return: A list of live versions.
    """
    sorted_pubs = list(sorted_pubs)
    latest = sorted_pubs.pop(0)
    return get_binary_versions(
        [latest]
        + [pub for pub in sorted_pubs if not pub.architecture_specific]
    )


class ArchSpecificPublicationsCache:
    """Cache to track which releases have arch-specific publications.

    This is used for second-pass binary domination:
    architecture-independent binary publications cannot be superseded as long
    as any architecture-dependent binary publications built from the same
    source package release are still active.  Thus such arch-indep
    publications are reprieved from domination.

    This class looks up whether publications for a release need that
    reprieve.  That only needs to be looked up in the database once per
    (source package release, archive, distroseries, pocket).  Hence this
    cache.
    """

    def __init__(self):
        self.cache = {}

    @staticmethod
    def getKey(bpph):
        """Extract just the relevant bits of information from a bpph."""
        return (
            bpph.binarypackagerelease.build.source_package_release,
            bpph.archive,
            bpph.distroseries,
            bpph.pocket,
        )

    def hasArchSpecificPublications(self, bpph):
        """Does bpph have active, arch-specific publications?

        If so, the dominator will want to reprieve `bpph`.
        """
        assert (
            not bpph.architecture_specific
        ), "Wrongly dominating arch-specific binary pub in pass 2."

        key = self.getKey(bpph)
        if key not in self.cache:
            self.cache[key] = self._lookUp(*key)
        return self.cache[key]

    @staticmethod
    def _lookUp(spr, archive, distroseries, pocket):
        """Look up an answer in the database."""
        query = getUtility(IPublishingSet).getActiveArchSpecificPublications(
            spr, archive, distroseries, pocket
        )
        return not query.is_empty()


def find_live_binary_versions_pass_2(sorted_pubs, cache):
    """Find versions out of Published publications that should stay live.

    This particular notion of liveness applies to second-pass binary
    domination: the latest version stays live, and architecture-specific
    publications stay live (i.e, ones that are not for the "all"
    architecture).

    More importantly, any publication for binary packages of the "all"
    architecture stay live if any of the non-"all" binary packages from
    the same source package release are still active -- even if they are
    for other architectures.

    This is the raison d'etre for the two-pass binary domination algorithm:
    to let us see which architecture-independent binary publications can be
    superseded without rendering any architecture-specific binaries from the
    same source package release uninstallable.

    (Note that here, "active" includes Published publications but also
    Pending ones.  This is standard nomenclature in Soyuz.  Some of the
    domination code confuses matters by using the term "active" to mean only
    Published publications).

    :param sorted_pubs: An iterable of `BinaryPackagePublishingHistory`,
        sorted by descending package version.
    :param cache: An `ArchSpecificPublicationsCache` to reduce the number of
        times we need to look up whether an spr/archive/distroseries/pocket
        has active arch-specific publications.
    :return: A list of live versions.
    """
    sorted_pubs = list(sorted_pubs)
    latest = sorted_pubs.pop(0)
    is_arch_specific = attrgetter("architecture_specific")
    arch_specific_pubs = list(filter(is_arch_specific, sorted_pubs))
    arch_indep_pubs = list(filterfalse(is_arch_specific, sorted_pubs))

    bpbs = load_related(
        BinaryPackageBuild,
        [pub.binarypackagerelease for pub in arch_indep_pubs],
        ["build_id"],
    )
    load_related(SourcePackageRelease, bpbs, ["source_package_release_id"])

    # XXX cjwatson 2022-05-01: Skip the architecture-specific check for
    # publications from CI builds for now, until we figure out how to
    # approximate source package releases for groups of CI builds.  We don't
    # currently expect problematic situations to come up on production; CI
    # builds are currently only expected to be used in situations where
    # either we don't build both architecture-specific and
    # architecture-independent packages, or where tight dependencies between
    # the two aren't customary.
    reprieved_pubs = [
        pub
        for pub in arch_indep_pubs
        if pub.binarypackagerelease.ci_build_id is None
        and cache.hasArchSpecificPublications(pub)
    ]

    return get_binary_versions([latest] + arch_specific_pubs + reprieved_pubs)


def contains_arch_indep(bpphs):
    """Are any of the publications among `bpphs` architecture-independent?"""
    return any(not bpph.architecture_specific for bpph in bpphs)


class Dominator:
    """Manage the process of marking packages as superseded.

    Packages are marked as superseded when they become obsolete.
    """

    def __init__(self, logger, archive):
        """Initialize the dominator.

        This process should be run after the publisher has published
        new stuff into the distribution but before the publisher
        creates the file lists for apt-ftparchive.
        """
        self.logger = logger
        self.archive = archive

    def planPackageDomination(
        self, sorted_pubs, live_versions, generalization
    ):
        """Plan domination of publications for a single package.

        The latest publication for any version in `live_versions` stays
        active.  Any older publications (including older publications for
        live versions with multiple publications) are marked as superseded by
        the respective oldest live releases that are newer than the superseded
        ones.

        Any versions that are newer than anything in `live_versions` are
        marked as deleted.  This should not be possible in Soyuz-native
        archives, but it can happen during archive imports when the
        previous latest version of a package has disappeared from the Sources
        list we import.

        :param sorted_pubs: A list of publications for the same package,
            in the same archive, series, pocket, and channel, all with
            status `PackagePublishingStatus.PUBLISHED`.  They must be sorted
            from most current to least current, as would be the result of
            `generalization.sortPublications`.
        :param live_versions: Iterable of versions that are still considered
            "live" for this package.  For any of these, the latest publication
            among `publications` will remain Published.  Publications for
            older releases, as well as older publications of live versions,
            will be marked as Superseded.  Publications of newer versions than
            are listed in `live_versions` are marked as Deleted.
        :param generalization: A `GeneralizedPublication` helper representing
            the kind of publications these are: source or binary.
        :return: A tuple of `(supersede, keep, delete)`.  `supersede` is a
            list of (superseded publication, dominant publication) pairs of
            publications to be marked as superseded, used to supersede other
            publications associated with the superseded ones.  `keep` is a
            set of publications that have been confirmed as live, used to
            ensure that these live publications are not superseded when
            superseding associated publications.  `delete` is a list of
            publications to delete.
        """
        live_versions = frozenset(live_versions)
        supersede = []
        keep = set()
        delete = []

        self.logger.debug(
            "Package has %d live publication(s).  Live versions: %s",
            len(sorted_pubs),
            live_versions,
        )

        # Verify that the publications are really sorted properly.
        check_order = OrderingCheck(
            key=cmp_to_key(generalization.compare), reverse=True
        )

        current_dominant = None
        dominant_version = None

        for pub in sorted_pubs:
            check_order.check(pub)

            version = generalization.getPackageVersion(pub)
            # There should never be two published releases with the same
            # version.  So it doesn't matter whether this comparison is
            # really a string comparison or a version comparison: if the
            # versions are equal by either measure, they're from the same
            # release.
            if version == dominant_version:
                # This publication is for a live version, but has been
                # superseded by a newer publication of the same version.
                # Supersede it.
                supersede.append((pub, current_dominant))
                self.logger.debug2(
                    "Superseding older publication for version %s.", version
                )
            elif version in live_versions:
                # This publication stays active; if any publications
                # that follow right after this are to be superseded,
                # this is the release that they are superseded by.
                current_dominant = pub
                dominant_version = version
                keep.add(pub)
                self.logger.debug2("Keeping version %s.", version)
            elif current_dominant is None:
                # This publication is no longer live, but there is no
                # newer version to supersede it either.  Therefore it
                # must be deleted.
                delete.append(pub)
                self.logger.debug2("Deleting version %s.", version)
            else:
                # This publication is superseded.  This is what we're
                # here to do.
                supersede.append((pub, current_dominant))
                self.logger.debug2("Superseding version %s.", version)

        return supersede, keep, delete

    def _sortPackages(self, publications, generalization):
        """Partition publications by package name and location, and sort them.

        The publications are sorted from most current to least current,
        as required by `planPackageDomination` etc.  Locations are currently
        (package name, channel).

        :param publications: An iterable of `SourcePackagePublishingHistory`
            or of `BinaryPackagePublishingHistory`.
        :param generalization: A `GeneralizedPublication` helper representing
            the kind of publications these are: source or binary.
        :return: A dict mapping each package location (package name,
            channel) to a sorted list of publications from `publications`.
        """
        # XXX cjwatson 2022-05-19: Traditional suites (distroseries/pocket)
        # are divided up in the loop in Publisher.B_dominate.  However, this
        # doesn't scale to channel-map-style suites (distroseries/channel),
        # since there may be a very large number of channels and we don't
        # want to have to loop over all the possible ones, so we divide
        # those up here instead.
        #
        # This is definitely confusing.  In the longer term, we should
        # probably push the loop down from the publisher to here, and sort
        # and dominate all candidates in a given archive at once: there's no
        # particularly obvious reason not to, and it might perform better as
        # well.

        pubs_by_name_and_location = defaultdict(list)
        for pub in publications:
            name = generalization.getPackageName(pub)
            location = make_package_location(pub)
            pubs_by_name_and_location[(name, location)].append(pub)

        # Sort the publication lists.  This is not an in-place sort, so
        # it involves altering the dict while we iterate it.  Listify
        # the items so that we can be sure that we're not altering the
        # iteration order while iteration is underway.
        for (name, location), pubs in list(pubs_by_name_and_location.items()):
            pubs_by_name_and_location[(name, location)] = (
                generalization.sortPublications(pubs)
            )

        return pubs_by_name_and_location

    def _setScheduledDeletionDate(self, pub_record):
        """Set the scheduleddeletiondate on a publishing record.

        If the status is DELETED we set the date to UTC_NOW, otherwise
        it gets the configured stay of execution period.
        """
        if pub_record.status == PackagePublishingStatus.DELETED:
            pub_record.scheduleddeletiondate = UTC_NOW
        else:
            pub_record.scheduleddeletiondate = UTC_NOW + timedelta(
                days=STAY_OF_EXECUTION
            )

    def _judgeSuperseded(self, source_records, binary_records):
        """Determine whether the superseded packages supplied should
        be moved to death row or not.

        Currently this is done by assuming that any superseded binary
        package should be removed. In the future this should attempt
        to supersede binaries in build-sized chunks only, bug 55030.

        Superseded source packages are considered removable when they
        have no binaries in this distroseries which are published or
        superseded

        When a package is considered for death row it is given a
        'scheduled deletion date' of now plus the defined 'stay of execution'
        time provided in the configuration parameter.
        """
        self.logger.debug("Beginning superseded processing...")

        binary_list = list(binary_records)
        for pub_record in binary_list:
            binpkg_release = pub_record.binarypackagerelease
            self.logger.debug(
                "%s/%s (%s) has been judged eligible for removal",
                binpkg_release.binarypackagename.name,
                binpkg_release.version,
                pub_record.distroarchseries.architecturetag,
            )
            self._setScheduledDeletionDate(pub_record)
            # XXX cprov 20070820: 'datemadepending' is useless, since it's
            # always equals to "scheduleddeletiondate - quarantine".
            pub_record.datemadepending = UTC_NOW
        if binary_list:
            IStore(binary_list[0]).flush()

        source_list = list(source_records)
        if not source_list:
            return

        source_ids = [sr.id for sr in source_list]

        # Find which spph ids have unremoved
        # binaries for the same spr/distroseries/pocket/channel.
        source_ids_with_active_binaries = set(
            IStore(SourcePackagePublishingHistory)
            .find(
                SourcePackagePublishingHistory.id,
                SourcePackagePublishingHistory.id.is_in(source_ids),
                BinaryPackagePublishingHistory.distroarchseries_id
                == DistroArchSeries.id,
                DistroArchSeries.distroseries_id
                == SourcePackagePublishingHistory.distroseries_id,
                BinaryPackagePublishingHistory.scheduleddeletiondate == None,
                BinaryPackagePublishingHistory.dateremoved == None,
                BinaryPackagePublishingHistory.archive == self.archive,
                BinaryPackageBuild.source_package_release_id
                == SourcePackagePublishingHistory.sourcepackagerelease_id,
                BinaryPackagePublishingHistory.binarypackagerelease_id
                == BinaryPackageRelease.id,
                BinaryPackageRelease.build_id == BinaryPackageBuild.id,
                BinaryPackagePublishingHistory.pocket
                == SourcePackagePublishingHistory.pocket,
                Not(
                    IsDistinctFrom(
                        BinaryPackagePublishingHistory._channel,
                        SourcePackagePublishingHistory._channel,
                    )
                ),
            )
            .config(distinct=True)
        )

        # Among those spphs with binaries, find which have at
        # least one PUBLISHED source publication for the same spr.
        if source_ids_with_active_binaries:
            OtherSPPH = ClassAlias(
                SourcePackagePublishingHistory, "other_spph"
            )
            published_subquery = Select(
                1,
                tables=[OtherSPPH],
                where=And(
                    OtherSPPH.sourcepackagerelease_id
                    == SourcePackagePublishingHistory.sourcepackagerelease_id,
                    OtherSPPH.distroseries_id
                    == SourcePackagePublishingHistory.distroseries_id,
                    OtherSPPH.pocket == SourcePackagePublishingHistory.pocket,
                    Not(
                        IsDistinctFrom(
                            OtherSPPH._channel,
                            SourcePackagePublishingHistory._channel,
                        )
                    ),
                    OtherSPPH.status == PackagePublishingStatus.PUBLISHED,
                    OtherSPPH.archive_id == self.archive.id,
                ),
            )
            source_ids_with_published_sibling = set(
                IStore(SourcePackagePublishingHistory).find(
                    SourcePackagePublishingHistory.id,
                    SourcePackagePublishingHistory.id.is_in(
                        source_ids_with_active_binaries
                    ),
                    Exists(published_subquery),
                )
            )
        else:
            source_ids_with_published_sibling = set()

        for pub_record in source_list:
            # Has unremoved binaries and no published SPPH for the
            # same SPR — leave it for consideration next time.
            if (
                pub_record.id in source_ids_with_active_binaries
                and pub_record.id not in source_ids_with_published_sibling
            ):
                continue

            srcpkg_release = pub_record.sourcepackagerelease
            self.logger.debug(
                "%s/%s (%s) source has been judged eligible for removal",
                srcpkg_release.sourcepackagename.name,
                srcpkg_release.version,
                pub_record.id,
            )
            self._setScheduledDeletionDate(pub_record)
            # XXX cprov 20070820: 'datemadepending' is pointless, since it's
            # always equals to "scheduleddeletiondate - quarantine".
            pub_record.datemadepending = UTC_NOW

        IStore(source_list[0]).flush()

    def findBinariesForDomination(self, distroarchseries, pocket):
        """Find binary publications that need dominating.

        This is only for traditional domination, where the latest published
        publication is always kept published.  It will ignore publications
        that have no other publications competing for the same binary package.
        """
        BPPH = BinaryPackagePublishingHistory
        BPR = BinaryPackageRelease

        bpph_location_clauses = [
            BPPH.status == PackagePublishingStatus.PUBLISHED,
            BPPH.distroarchseries == distroarchseries,
            BPPH.archive == self.archive,
            BPPH.pocket == pocket,
        ]
        candidate_binary_names = Select(
            BPPH.binarypackagename_id,
            And(*bpph_location_clauses),
            group_by=(BPPH.binarypackagename_id, BPPH._channel),
            having=(Count() > 1),
        )
        main_clauses = bpph_location_clauses + [
            BPR.id == BPPH.binarypackagerelease_id,
            BPR.binarypackagename_id.is_in(candidate_binary_names),
            BPR.binpackageformat != BinaryPackageFormat.DDEB,
        ]

        # We're going to access the BPRs as well.  Since we make the
        # database look them up anyway, and since there won't be many
        # duplications among them, load them alongside the publications.
        # We'll also want their BinaryPackageNames, but adding those to
        # the join would complicate the query.
        query = IStore(BPPH).find((BPPH, BPR), *main_clauses)
        bpphs = list(DecoratedResultSet(query, itemgetter(0)))
        load_related(BinaryPackageName, bpphs, ["binarypackagename_id"])
        return bpphs

    def dominateBinaries(self, distroseries, pocket):
        """Perform domination on binary package publications.

        Dominates binaries, restricted to `distroseries`, `pocket`, and
        `self.archive`.
        """
        generalization = GeneralizedPublication(is_source=False)

        # Domination happens in two passes.  The first tries to
        # supersede architecture-dependent publications; the second
        # tries to supersede architecture-independent ones.  An
        # architecture-independent pub is kept alive as long as any
        # architecture-dependent pubs from the same source package build
        # are still live for any architecture, because they may depend
        # on the architecture-independent package.
        # Thus we limit the second pass to those packages that have
        # published, architecture-independent publications; anything
        # else will have completed domination in the first pass.
        packages_w_arch_indep = set()
        supersede = []
        keep = set()
        delete = []

        def plan(pubs, live_versions):
            cur_supersede, cur_keep, cur_delete = self.planPackageDomination(
                pubs, live_versions, generalization
            )
            supersede.extend(cur_supersede)
            keep.update(cur_keep)
            delete.extend(cur_delete)

        def execute_plan():
            if supersede:
                self.logger.info("Superseding binaries...")
            for pub, dominant in supersede:
                pub.supersede(dominant, logger=self.logger)
                IStore(pub).flush()
                # If this is architecture-independent, all publications with
                # the same context and overrides should be dominated
                # simultaneously, unless one of the plans decided to keep
                # it.  For this reason, an architecture's plan can't be
                # executed until all architectures have been planned.
                if not pub.architecture_specific:
                    for dominated in pub.getOtherPublications():
                        if dominated != pub and dominated not in keep:
                            dominated.supersede(dominant, logger=self.logger)
                            IStore(dominated).flush()
            if delete:
                self.logger.info("Deleting binaries...")
            for pub in delete:
                pub.requestDeletion(None)
                IStore(pub).flush()

        for distroarchseries in distroseries.architectures:
            self.logger.info(
                "Performing domination across %s/%s (%s)",
                distroarchseries.distroseries.name,
                pocket.title,
                distroarchseries.architecturetag,
            )

            self.logger.info("Finding binaries...")
            bins = self.findBinariesForDomination(distroarchseries, pocket)
            sorted_packages = self._sortPackages(bins, generalization)
            self.logger.info("Planning domination of binaries...")
            for (name, location), pubs in sorted_packages.items():
                self.logger.debug(
                    "Planning domination of %s in %s" % (name, location)
                )
                assert len(pubs) > 0, "Dominating zero binaries!"
                live_versions = find_live_binary_versions_pass_1(pubs)
                plan(pubs, live_versions)
                if contains_arch_indep(pubs):
                    packages_w_arch_indep.add((name, location))

        execute_plan()

        packages_w_arch_indep = frozenset(packages_w_arch_indep)
        supersede = []
        keep = set()
        delete = []

        # The second pass attempts to supersede arch-all publications of
        # older versions, from source package releases that no longer
        # have any active arch-specific publications that might depend
        # on the arch-indep ones.
        # (In maintaining this code, bear in mind that some or all of a
        # source package's binary packages may switch between
        # arch-specific and arch-indep between releases.)
        reprieve_cache = ArchSpecificPublicationsCache()
        for distroarchseries in distroseries.architectures:
            self.logger.info("Finding binaries...(2nd pass)")
            bins = self.findBinariesForDomination(distroarchseries, pocket)
            sorted_packages = self._sortPackages(bins, generalization)
            self.logger.info("Planning domination of binaries...(2nd pass)")
            for name, location in packages_w_arch_indep.intersection(
                sorted_packages
            ):
                pubs = sorted_packages[(name, location)]
                self.logger.debug(
                    "Planning domination of %s in %s" % (name, location)
                )
                assert len(pubs) > 0, "Dominating zero binaries in 2nd pass!"
                live_versions = find_live_binary_versions_pass_2(
                    pubs, reprieve_cache
                )
                plan(pubs, live_versions)

        execute_plan()

    def _composeActiveSourcePubsCondition(self, distroseries, pocket):
        """Compose ORM condition for restricting relevant source pubs."""
        SPPH = SourcePackagePublishingHistory

        return And(
            SPPH.status == PackagePublishingStatus.PUBLISHED,
            SPPH.distroseries == distroseries,
            SPPH.archive == self.archive,
            SPPH.pocket == pocket,
        )

    def findSourcesForDomination(self, distroseries, pocket):
        """Find binary publications that need dominating.

        This is only for traditional domination, where the latest published
        publication is always kept published.  See `find_live_source_versions`
        for this logic.

        To optimize for that logic, `findSourcesForDomination` will ignore
        publications that have no other publications competing for the same
        binary package.  There'd be nothing to do for those cases.
        """
        SPPH = SourcePackagePublishingHistory
        SPR = SourcePackageRelease

        spph_location_clauses = self._composeActiveSourcePubsCondition(
            distroseries, pocket
        )
        candidate_source_names = Select(
            SPPH.sourcepackagename_id,
            And(join_spph_spr(), spph_location_clauses),
            group_by=(SPPH.sourcepackagename_id, SPPH._channel),
            having=(Count() > 1),
        )

        # We'll also access the SourcePackageReleases associated with
        # the publications we find.  Since they're in the join anyway,
        # load them alongside the publications.
        # Actually we'll also want the SourcePackageNames, but adding
        # those to the (outer) query would complicate it, and
        # potentially slow it down.
        query = IStore(SPPH).find(
            (SPPH, SPR),
            join_spph_spr(),
            SPPH.sourcepackagename_id.is_in(candidate_source_names),
            spph_location_clauses,
        )
        spphs = DecoratedResultSet(query, itemgetter(0))
        load_related(SourcePackageName, spphs, ["sourcepackagename_id"])
        return spphs

    def dominateSources(self, distroseries, pocket):
        """Perform domination on source package publications.

        Dominates sources, restricted to `distroseries`, `pocket`, and
        `self.archive`.
        """
        self.logger.debug(
            "Performing domination across %s/%s (Source)",
            distroseries.name,
            pocket.title,
        )

        generalization = GeneralizedPublication(is_source=True)

        self.logger.debug("Finding sources...")
        sources = self.findSourcesForDomination(distroseries, pocket)
        sorted_packages = self._sortPackages(sources, generalization)
        supersede = []
        delete = []

        self.logger.debug("Dominating sources...")
        for (name, location), pubs in sorted_packages.items():
            self.logger.debug("Dominating %s in %s" % (name, location))
            assert len(pubs) > 0, "Dominating zero sources!"
            live_versions = find_live_source_versions(pubs)
            cur_supersede, _, cur_delete = self.planPackageDomination(
                pubs, live_versions, generalization
            )
            supersede.extend(cur_supersede)
            delete.extend(cur_delete)

        for pub, dominant in supersede:
            pub.supersede(dominant, logger=self.logger)
            IStore(pub).flush()
        for pub in delete:
            pub.requestDeletion(None)
            IStore(pub).flush()

    def findPublishedSourcePackageNames(self, distroseries, pocket):
        """Find currently published source packages.

        Returns an iterable of tuples: (name of source package, number of
        publications in Published state).
        """
        looking_for = (
            SourcePackageName.name,
            Count(SourcePackagePublishingHistory.id),
        )
        result = IStore(SourcePackageName).find(
            looking_for,
            join_spph_spr(),
            join_spph_spn(),
            self._composeActiveSourcePubsCondition(distroseries, pocket),
        )
        return result.group_by(SourcePackageName.name)

    def findPublishedSPPHs(self, distroseries, pocket, package_name):
        """Find currently published source publications for given package."""
        SPPH = SourcePackagePublishingHistory
        SPR = SourcePackageRelease

        query = IStore(SourcePackagePublishingHistory).find(
            SPPH,
            join_spph_spr(),
            join_spph_spn(),
            SourcePackageName.name == package_name,
            self._composeActiveSourcePubsCondition(distroseries, pocket),
        )
        # Sort by descending version (SPR.version has type debversion in
        # the database, so this should be a real proper comparison) so
        # that _sortPackage will have slightly less work to do later.
        return query.order_by(Desc(SPR.version), Desc(SPPH.datecreated))

    def dominateSourceVersions(
        self,
        distroseries,
        pocket,
        package_name,
        live_versions,
        immutable_check=True,
    ):
        """Dominate source publications based on a set of "live" versions.

        Active publications for the "live" versions will remain active.  All
        other active publications for the same package (and the same archive,
        distroseries, and pocket) are marked superseded.

        Unlike traditional domination, this allows multiple versions of a
        package to stay active in the same distroseries, archive, and pocket.

        :param distroseries: `DistroSeries` to dominate.
        :param pocket: `PackagePublishingPocket` to dominate.
        :param package_name: Source package name, as text.
        :param live_versions: Iterable of all version strings that are to
            remain active.
        """
        generalization = GeneralizedPublication(is_source=True)
        pubs = self.findPublishedSPPHs(distroseries, pocket, package_name)
        pubs = generalization.sortPublications(pubs)
        supersede, _, delete = self.planPackageDomination(
            pubs, live_versions, generalization
        )
        for pub, dominant in supersede:
            pub.supersede(dominant, logger=self.logger)
            IStore(pub).flush()
        for pub in delete:
            pub.requestDeletion(None, immutable_check=immutable_check)
            IStore(pub).flush()

    def judge(self, distroseries, pocket):
        """Judge superseded sources and binaries."""
        sources = IStore(SourcePackagePublishingHistory).find(
            SourcePackagePublishingHistory,
            SourcePackagePublishingHistory.distroseries == distroseries,
            SourcePackagePublishingHistory.archive == self.archive,
            SourcePackagePublishingHistory.pocket == pocket,
            SourcePackagePublishingHistory.status.is_in(
                inactive_publishing_status
            ),
            SourcePackagePublishingHistory.scheduleddeletiondate == None,
            SourcePackagePublishingHistory.dateremoved == None,
        )

        binaries = IStore(BinaryPackagePublishingHistory).find(
            BinaryPackagePublishingHistory,
            BinaryPackagePublishingHistory.distroarchseries
            == DistroArchSeries.id,
            DistroArchSeries.distroseries == distroseries,
            BinaryPackagePublishingHistory.archive == self.archive,
            BinaryPackagePublishingHistory.pocket == pocket,
            BinaryPackagePublishingHistory.status.is_in(
                inactive_publishing_status
            ),
            BinaryPackagePublishingHistory.scheduleddeletiondate == None,
            BinaryPackagePublishingHistory.dateremoved == None,
        )

        self._judgeSuperseded(sources, binaries)

    # The domination process loads many objects into the Storm cache, many
    # of which contain mutable-value variables, and Store.flush gets
    # substantially slower when the cache contains many mutable-value
    # variables since it has to dump each of them to detect changes.  In the
    # long term we may need a different strategy for mutable-value variables
    # in Storm; in the short term, we can get by with blocking implicit
    # flushes during domination (so that every Store.get doesn't incur a
    # flush) and being careful to flush both at the start of domination and
    # explicitly after changing objects.
    @block_implicit_flushes
    def judgeAndDominate(self, distroseries, pocket):
        """Perform the domination and superseding calculations

        It only works across the distroseries and pocket specified.
        """
        flush_database_updates()

        self.dominateBinaries(distroseries, pocket)
        self.dominateSources(distroseries, pocket)
        self.judge(distroseries, pocket)

        self.logger.debug(
            "Domination for %s/%s finished", distroseries.name, pocket.title
        )
