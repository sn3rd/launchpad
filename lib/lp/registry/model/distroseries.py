# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database classes for a distribution series."""

__all__ = [
    "ACTIVE_RELEASED_STATUSES",
    "ACTIVE_UNRELEASED_STATUSES",
    "DistroSeries",
    "DistroSeriesSet",
]

import collections
from datetime import timezone
from io import BytesIO
from operator import itemgetter
from typing import List

import apt_pkg
from lazr.delegates import delegate_to
from storm.expr import SQL, And, Column, Desc, Is, Join, Not, Or, Select, Table
from storm.locals import (
    JSON,
    Bool,
    DateTime,
    Int,
    Reference,
    ReferenceSet,
    Unicode,
)
from storm.store import Store
from zope.component import getUtility
from zope.interface import implementer

from lp.app.enums import service_uses_launchpad
from lp.app.errors import NotFoundError
from lp.blueprints.interfaces.specificationtarget import ISpecificationTarget
from lp.blueprints.model.specification import (
    HasSpecificationsMixin,
    Specification,
)
from lp.blueprints.model.specificationsearch import search_specifications
from lp.bugs.interfaces.bugsummary import IBugSummaryDimension
from lp.bugs.interfaces.bugtarget import ISeriesBugTarget
from lp.bugs.interfaces.bugtaskfilter import OrderedBugTask
from lp.bugs.model.bugtarget import BugTargetBase
from lp.bugs.model.structuralsubscription import (
    StructuralSubscriptionTargetMixin,
)
from lp.buildmaster.model.processor import Processor
from lp.registry.errors import NoSuchDistroSeries
from lp.registry.interfaces.distroseries import (
    DerivationError,
    DistroSeriesTranslationTemplateStatistics,
    IDistroSeries,
    IDistroSeriesSet,
)
from lp.registry.interfaces.distroseriesdifference import (
    IDistroSeriesDifferenceSource,
)
from lp.registry.interfaces.distroseriesdifferencecomment import (
    IDistroSeriesDifferenceCommentSource,
)
from lp.registry.interfaces.person import validate_public_person
from lp.registry.interfaces.pocket import PackagePublishingPocket, pocketsuffix
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackage import ISourcePackageFactory
from lp.registry.interfaces.sourcepackagename import (
    ISourcePackageName,
    ISourcePackageNameSet,
)
from lp.registry.model.archivesourcepackageseries import (
    ArchiveSourcePackageSeries,
)
from lp.registry.model.externalpackageseries import ExternalPackageSeries
from lp.registry.model.milestone import HasMilestonesMixin, Milestone
from lp.registry.model.packaging import Packaging
from lp.registry.model.person import Person
from lp.registry.model.series import SeriesMixin
from lp.registry.model.sourcepackage import SourcePackage
from lp.registry.model.sourcepackagename import SourcePackageName
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.sqlbase import sqlvalues
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import WithMaterialized, fti_search
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.librarian.model import LibraryFileAlias
from lp.services.mail.signedmessage import signed_message_from_bytes
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.worlddata.model.language import Language
from lp.soyuz.enums import (
    ArchivePurpose,
    IndexCompressionType,
    PackagePublishingStatus,
    PackageUploadStatus,
)
from lp.soyuz.interfaces.binarypackagebuild import IBinaryPackageBuildSet
from lp.soyuz.interfaces.binarypackagename import (
    IBinaryPackageName,
    IBinaryPackageNameSet,
)
from lp.soyuz.interfaces.distributionjob import (
    IInitializeDistroSeriesJobSource,
)
from lp.soyuz.interfaces.publishing import active_publishing_status
from lp.soyuz.interfaces.queue import IHasQueueItems, IPackageUploadSet
from lp.soyuz.interfaces.sourcepackageformat import (
    ISourcePackageFormatSelectionSet,
)
from lp.soyuz.model.binarypackagename import BinaryPackageName
from lp.soyuz.model.component import Component, ComponentSelection
from lp.soyuz.model.distributionsourcepackagerelease import (
    DistributionSourcePackageRelease,
)
from lp.soyuz.model.distroarchseries import DistroArchSeries, PocketChroot
from lp.soyuz.model.distroseriesbinarypackage import DistroSeriesBinaryPackage
from lp.soyuz.model.distroseriespackagecache import DistroSeriesPackageCache
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
    get_current_source_releases,
)
from lp.soyuz.model.queue import (
    PackageUpload,
    PackageUploadQueue,
    PackageUploadSource,
)
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease
from lp.translations.enums import LanguagePackType
from lp.translations.model.distroserieslanguage import (
    DistroSeriesLanguage,
    EmptyDistroSeriesLanguage,
)
from lp.translations.model.hastranslationimports import (
    HasTranslationImportsMixin,
)
from lp.translations.model.hastranslationtemplates import (
    HasTranslationTemplatesMixin,
)
from lp.translations.model.languagepack import LanguagePack
from lp.translations.model.pofile import POFile
from lp.translations.model.potemplate import (
    POTemplate,
    TranslationTemplatesCollection,
)

ACTIVE_RELEASED_STATUSES = [
    SeriesStatus.CURRENT,
    SeriesStatus.SUPPORTED,
]


ACTIVE_UNRELEASED_STATUSES = [
    SeriesStatus.EXPERIMENTAL,
    SeriesStatus.DEVELOPMENT,
    SeriesStatus.FROZEN,
]


DEFAULT_INDEX_COMPRESSORS = [
    IndexCompressionType.GZIP,
    IndexCompressionType.BZIP2,
]


@delegate_to(ISpecificationTarget, context="distribution")
@implementer(
    IBugSummaryDimension,
    IDistroSeries,
    IHasQueueItems,
    ISeriesBugTarget,
)
class DistroSeries(
    StormBase,
    SeriesMixin,
    BugTargetBase,
    HasSpecificationsMixin,
    HasMilestonesMixin,
    HasTranslationImportsMixin,
    HasTranslationTemplatesMixin,
    StructuralSubscriptionTargetMixin,
):
    """A particular series of a distribution."""

    __storm_table__ = "DistroSeries"
    __storm_order__ = ["distribution", "version"]

    id = Int(primary=True)
    distribution_id = Int(name="distribution", allow_none=False)
    distribution = Reference(distribution_id, "Distribution.id")
    name = Unicode()
    display_name = Unicode(name="displayname", allow_none=False)
    title = Unicode(allow_none=False)
    description = Unicode(allow_none=False)
    version = Unicode(allow_none=False)
    status = DBEnum(name="releasestatus", allow_none=False, enum=SeriesStatus)
    date_created = DateTime(
        allow_none=True, default=UTC_NOW, tzinfo=timezone.utc
    )
    datereleased = DateTime(allow_none=True, default=None, tzinfo=timezone.utc)
    previous_series_id = Int(name="parent_series", allow_none=True)
    previous_series = Reference(previous_series_id, "DistroSeries.id")
    registrant_id = Int(
        name="registrant", validator=validate_public_person, allow_none=False
    )
    registrant = Reference(registrant_id, "Person.id")
    driver_id = Int(
        name="driver",
        validator=validate_public_person,
        allow_none=True,
        default=None,
    )
    driver = Reference(driver_id, "Person.id")
    changeslist = Unicode(allow_none=True, default=None)
    nominatedarchindep_id = Int(
        name="nominatedarchindep", allow_none=True, default=None
    )
    nominatedarchindep = Reference(
        nominatedarchindep_id, "DistroArchSeries.id"
    )
    messagecount = Int(allow_none=False, default=0)
    binarycount = Int(allow_none=False, default=DEFAULT)
    sourcecount = Int(allow_none=False, default=DEFAULT)
    defer_translation_imports = Bool(allow_none=False, default=True)
    hide_all_translations = Bool(allow_none=False, default=True)
    language_pack_base_id = Int(
        name="language_pack_base", allow_none=True, default=None
    )
    language_pack_base = Reference(language_pack_base_id, "LanguagePack.id")
    language_pack_delta_id = Int(
        name="language_pack_delta", allow_none=True, default=None
    )
    language_pack_delta = Reference(language_pack_delta_id, "LanguagePack.id")
    language_pack_proposed_id = Int(
        name="language_pack_proposed", allow_none=True, default=None
    )
    language_pack_proposed = Reference(
        language_pack_proposed_id, "LanguagePack.id"
    )
    language_pack_full_export_requested = Bool(allow_none=False, default=False)
    publishing_options = JSON("publishing_options")

    language_packs = ReferenceSet(
        "id",
        "LanguagePack.distroseries_id",
        order_by=Desc("LanguagePack.date_exported"),
    )
    sections = ReferenceSet(
        "id",
        "SectionSelection.distroseries_id",
        "SectionSelection.section_id",
        "Section.id",
    )

    def __init__(
        self,
        distribution,
        name,
        display_name,
        title,
        summary,
        description,
        version,
        status,
        registrant,
        previous_series=None,
    ):
        super().__init__()
        self.distribution = distribution
        self.name = name
        self.display_name = display_name
        self.title = title
        self.summary = summary
        self.description = description
        self.version = version
        self.status = status
        self.registrant = registrant
        self.previous_series = previous_series
        self.publishing_options = {
            "backports_not_automatic": False,
            "proposed_not_automatic": False,
            "include_long_descriptions": True,
            "index_compressors": [
                compressor.title for compressor in DEFAULT_INDEX_COMPRESSORS
            ],
            "publish_by_hash": False,
            "advertise_by_hash": False,
            "strict_supported_component_dependencies": True,
            "publish_i18n_index": True,
        }

    @property
    def displayname(self):
        return self.display_name

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        return self.distribution

    @property
    def pillar(self):
        """See `IBugTarget`."""
        return self.distribution

    @property
    def series(self):
        """See `ISeriesBugTarget`."""
        return self

    @property
    def named_version(self):
        return "%s (%s)" % (self.display_name, self.version)

    @property
    def upload_components(self):
        """See `IDistroSeries`."""
        return IStore(Component).find(
            Component,
            ComponentSelection.distroseries == self,
            ComponentSelection.component == Component.id,
        )

    @cachedproperty
    def components(self):
        """See `IDistroSeries`."""
        # XXX julian 2007-06-25
        # This is filtering out the partner component for now, until
        # the second stage of the partner repo arrives in 1.1.8.
        return list(
            IStore(Component).find(
                Component,
                ComponentSelection.distroseries == self,
                ComponentSelection.component == Component.id,
                Component.name != "partner",
            )
        )

    @cachedproperty
    def component_names(self):
        """See `IDistroSeries`."""
        return [component.name for component in self.components]

    @cachedproperty
    def suite_names(self):
        """See `IDistroSeries`."""
        return [str(pocket) for pocket in PackagePublishingPocket.items]

    @property
    def answers_usage(self):
        """See `IServiceUsage.`"""
        return self.distribution.answers_usage

    @property
    def blueprints_usage(self):
        """See `IServiceUsage.`"""
        return self.distribution.blueprints_usage

    @property
    def translations_usage(self):
        """See `IServiceUsage.`"""
        return self.distribution.translations_usage

    @property
    def codehosting_usage(self):
        """See `IServiceUsage.`"""
        return self.distribution.codehosting_usage

    @property
    def bug_tracking_usage(self):
        """See `IServiceUsage.`"""
        return self.distribution.bug_tracking_usage

    @property
    def uses_launchpad(self):
        """See `IServiceUsage.`"""
        return (
            service_uses_launchpad(self.blueprints_usage)
            or service_uses_launchpad(self.translations_usage)
            or service_uses_launchpad(self.answers_usage)
            or service_uses_launchpad(self.codehosting_usage)
            or service_uses_launchpad(self.bug_tracking_usage)
        )

    # DistroArchSeries lookup properties/methods.
    architectures = ReferenceSet(
        "id",
        DistroArchSeries.distroseries_id,
        order_by=DistroArchSeries.architecturetag,
    )

    def __getitem__(self, archtag):
        """See `IDistroSeries`."""
        return self.getDistroArchSeries(archtag)

    def __repr__(self):
        return "<%s %r>" % (self.__class__.__name__, self.name)

    def __str__(self):
        return "%s %s" % (self.distribution.name, self.name)

    def getDistroArchSeries(self, archtag):
        """See `IDistroSeries`."""
        item = (
            IStore(DistroArchSeries)
            .find(DistroArchSeries, distroseries=self, architecturetag=archtag)
            .one()
        )
        if item is None:
            raise NotFoundError(
                "Unknown architecture %s for %s %s"
                % (archtag, self.distribution.name, self.name)
            )
        return item

    def getDistroArchSeriesByProcessor(self, processor):
        """See `IDistroSeries`."""
        return (
            Store.of(self)
            .find(
                DistroArchSeries,
                DistroArchSeries.distroseries == self,
                DistroArchSeries.processor == processor,
            )
            .one()
        )

    @property
    def inherit_overrides_from_parents(self):
        from lp.registry.interfaces.distroseriesparent import (
            IDistroSeriesParentSet,
        )

        return any(
            dsp.inherit_overrides
            for dsp in getUtility(IDistroSeriesParentSet).getByDerivedSeries(
                self
            )
        )

    @inherit_overrides_from_parents.setter
    def inherit_overrides_from_parents(self, value):
        from lp.registry.interfaces.distroseriesparent import (
            IDistroSeriesParentSet,
        )

        dsps = getUtility(IDistroSeriesParentSet).getByDerivedSeries(self)
        for dsp in dsps:
            dsp.inherit_overrides = value

    @property
    def enabled_architectures(self):
        return (
            Store.of(self)
            .find(
                DistroArchSeries,
                DistroArchSeries.distroseries == self,
                Is(DistroArchSeries.enabled, True),
            )
            .order_by(DistroArchSeries.architecturetag)
        )

    @property
    def buildable_architectures(self):
        store = Store.of(self)
        origin = [
            DistroArchSeries,
            Join(
                PocketChroot,
                PocketChroot.distroarchseries == DistroArchSeries.id,
            ),
            Join(LibraryFileAlias, PocketChroot.chroot == LibraryFileAlias.id),
        ]
        results = store.using(*origin).find(
            DistroArchSeries, DistroArchSeries.distroseries == self
        )
        return results.order_by(DistroArchSeries.architecturetag).config(
            distinct=True
        )

    @property
    def virtualized_architectures(self):
        store = Store.of(self)
        results = store.find(
            DistroArchSeries,
            DistroArchSeries.distroseries == self,
            Processor.id == DistroArchSeries.processor_id,
            Processor.supports_virtualized == True,
        )
        return results.order_by(DistroArchSeries.architecturetag)

    # End of DistroArchSeries lookup methods

    @property
    def parent(self):
        """See `IDistroSeries`."""
        return self.distribution

    @property
    def owner(self):
        """See `IDistroSeries`."""
        return self.distribution.owner

    @property
    def sortkey(self):
        """A string to be used for sorting distro seriess.

        This is designed to sort alphabetically by distro and series name,
        except that Ubuntu will be at the top of the listing.
        """
        result = ""
        if self.distribution.name == "ubuntu":
            result += "-"
        result += self.distribution.name + self.name
        return result

    @cachedproperty
    def _all_packagings(self):
        """Get an unordered list of all packagings.

        :return: A ResultSet which can be decorated or tuned further. Use
            DistroSeries._packaging_row_to_packaging to extract the
            packaging objects out.
        """
        # We join to SourcePackageName, ProductSeries, and Product to cache
        # the objects that are implicitly needed to work with a
        # Packaging object.
        # NB: precaching objects like this method tries to do has a very poor
        # hit rate with storm - many queries will still be executed; consider
        # ripping this out and instead allowing explicit inclusion of things
        # like Person._members does - returning a cached object graph.
        # -- RBC 20100810
        # Avoid circular import failures.
        from lp.registry.model.product import Product
        from lp.registry.model.productseries import ProductSeries

        find_spec = (Packaging, SourcePackageName, ProductSeries, Product)
        origin = [
            Packaging,
            Join(
                SourcePackageName,
                Packaging.sourcepackagename == SourcePackageName.id,
            ),
            Join(ProductSeries, Packaging.productseries == ProductSeries.id),
            Join(Product, ProductSeries.product == Product.id),
        ]
        condition = Packaging.distroseries == self.id
        results = IStore(self).using(*origin).find(find_spec, condition)
        return results

    @staticmethod
    def _packaging_row_to_packaging(row):
        # each row has:
        #  (packaging, spn, product_series, product)
        return row[0]

    @property
    def packagings(self):
        """See `IDistroSeries`."""
        results = self._all_packagings
        results = results.order_by(SourcePackageName.name)
        return DecoratedResultSet(
            results, DistroSeries._packaging_row_to_packaging
        )

    def getPrioritizedUnlinkedSourcePackages(self):
        """See `IDistroSeries`.

        The prioritization is a heuristic rule using bug count,
        translatable messages, and the source package release's component.
        """
        find_spec = (
            SourcePackageName,
            SQL(
                """
                coalesce(bug_count * 10, 0) + coalesce(po_messages, 0) +
                CASE WHEN component = 1 THEN 1000 ELSE 0 END AS score"""
            ),
            SQL("coalesce(bug_count, 0) AS bug_count"),
            SQL("coalesce(total_messages, 0) AS total_messages"),
        )
        # This does not use _current_sourcepackage_joins_and_conditions
        # because the two queries are working on different data sets -
        # +needs-packaging was timing out and +packaging wasn't, and
        # destabilising things unnecessarily is not good.
        origin = SQL(
            """
            SourcePackageName, (SELECT
        spr.sourcepackagename,
        spr.component,
        bug_count,
        SUM(POTemplate.messagecount) * %(po_message_weight)s AS po_messages,
        SUM(POTemplate.messagecount) AS total_messages
    FROM
        SourcePackageRelease AS spr
        JOIN SourcePackagePublishingHistory AS spph
            ON spr.id = spph.sourcepackagerelease
        JOIN Archive
            ON spph.archive = Archive.id
        JOIN Section
            ON spph.section = Section.id
        JOIN DistroSeries
            ON spph.distroseries = DistroSeries.id
        LEFT OUTER JOIN DistributionSourcePackage AS dsp
            ON dsp.sourcepackagename = spr.sourcepackagename
                AND dsp.distribution = DistroSeries.distribution
        LEFT OUTER JOIN POTemplate
            ON POTemplate.sourcepackagename = spr.sourcepackagename
                AND POTemplate.distroseries = DistroSeries.id
    WHERE
        DistroSeries.id = %(distroseries)s
        AND spph.status IN %(active_status)s
        AND Archive.purpose = %(primary)s
        AND Section.name <> 'translations'
        AND NOT EXISTS (
            SELECT TRUE FROM Packaging
            WHERE
                Packaging.sourcepackagename = spr.sourcepackagename
                AND Packaging.distroseries = spph.distroseries)
    GROUP BY
        spr.sourcepackagename, spr.component, bug_count
    ) AS spn_info"""
            % sqlvalues(
                po_message_weight=self._current_sourcepackage_po_weight,
                distroseries=self.id,
                active_status=active_publishing_status,
                primary=ArchivePurpose.PRIMARY,
            )
        )
        condition = SQL("sourcepackagename.id = spn_info.sourcepackagename")
        results = IStore(self).using(origin).find(find_spec, condition)
        results = results.order_by("score DESC", SourcePackageName.name)
        results = results.config(distinct=("score", SourcePackageName.name))

        def decorator(row):
            spn, score, bug_count, total_messages = row
            return {
                "package": SourcePackage(
                    sourcepackagename=spn, distroseries=self
                ),
                "bug_count": bug_count,
                "total_messages": total_messages,
            }

        return DecoratedResultSet(results, decorator)

    def getPrioritizedPackagings(self):
        """See `IDistroSeries`.

        The prioritization is a heuristic rule using the branch, bug heat,
        translatable messages, and the source package release's component.
        """
        # We join to SourcePackageName, ProductSeries, and Product to cache
        # the objects that are implicitly needed to work with a
        # Packaging object.
        joins, conditions = self._current_sourcepackage_joins_and_conditions
        # XXX: EdwinGrubbs 2010-07-29 bug=374777
        # Storm doesn't support DISTINCT ON.
        origin = SQL(
            """
            (
            SELECT DISTINCT ON (Packaging.id)
                Packaging.*,
                spr.component AS spr_component,
                SourcePackageName.name AS spn_name,
                bug_count,
                po_messages
            FROM %(joins)s
            WHERE %(conditions)s
                AND packaging.id IS NOT NULL
            ) AS Packaging
            JOIN ProductSeries
                ON Packaging.productseries = ProductSeries.id
            JOIN Product
                ON ProductSeries.product = Product.id
            """
            % dict(joins=joins, conditions=conditions)
        )
        return (
            IStore(self)
            .using(origin)
            .find(Packaging)
            .order_by(
                """
                (CASE WHEN spr_component = 1 THEN 1000 ELSE 0 END
                + CASE WHEN Product.bugtracker IS NULL
                    THEN coalesce(bug_count * 10, 10) ELSE 0 END
                + CASE WHEN ProductSeries.translations_autoimport_mode = 1
                    THEN coalesce(po_messages, 10) ELSE 0 END
                + CASE WHEN ProductSeries.branch IS NULL THEN 500 ELSE 0 END
                ) DESC,
                spn_name ASC
                """
            )
        )

    @property
    def _current_sourcepackage_po_weight(self):
        """See getPrioritized*."""
        # Bugs and PO messages are heuristically scored. These queries
        # can easily timeout so filters and weights are used to create
        # an acceptable prioritization of packages that is fast to execute.
        return 0.5

    @property
    def _current_sourcepackage_joins_and_conditions(self):
        """The SQL joins and conditions to prioritize source packages.

        Used for getPrioritizedPackagings only.
        """
        # Bugs and PO messages are heuristically scored. These queries
        # can easily timeout so filters and weights are used to create
        # an acceptable prioritization of packages that is fast to execute.
        po_message_weight = self._current_sourcepackage_po_weight
        message_score = """
            LEFT JOIN (
                SELECT
                    POTemplate.sourcepackagename,
                    POTemplate.distroseries,
                    SUM(POTemplate.messagecount) * %(po_message_weight)s
                        AS po_messages,
                    SUM(POTemplate.messagecount) AS total_messages
                FROM POTemplate
                WHERE
                    POTemplate.sourcepackagename is not NULL
                    AND POTemplate.distroseries = %(distroseries)s
                GROUP BY
                    POTemplate.sourcepackagename,
                    POTemplate.distroseries
                ) messages
                ON SourcePackageName.id = messages.sourcepackagename
                AND DistroSeries.id = messages.distroseries
            """ % sqlvalues(
            distroseries=self.id, po_message_weight=po_message_weight
        )
        joins = (
            """
            SourcePackageName
            JOIN SourcePackageRelease spr
                ON SourcePackageName.id = spr.sourcepackagename
            JOIN SourcePackagePublishingHistory spph
                ON spr.id = spph.sourcepackagerelease
            JOIN archive
                ON spph.archive = Archive.id
            JOIN section
                ON spph.section = section.id
            JOIN DistroSeries
                ON spph.distroseries = DistroSeries.id
            LEFT JOIN Packaging
                ON SourcePackageName.id = Packaging.sourcepackagename
                AND Packaging.distroseries = DistroSeries.id
            LEFT JOIN DistributionSourcePackage dsp
                ON dsp.sourcepackagename = spr.sourcepackagename
                    AND dsp.distribution = DistroSeries.distribution
            """
            + message_score
        )
        conditions = """
            DistroSeries.id = %(distroseries)s
            AND spph.status IN %(active_status)s
            AND archive.purpose = %(primary)s
            AND section.name != 'translations'
            """ % sqlvalues(
            distroseries=self.id,
            active_status=active_publishing_status,
            primary=ArchivePurpose.PRIMARY,
        )
        return (joins, conditions)

    def getMostRecentlyLinkedPackagings(self):
        """See `IDistroSeries`."""
        results = self._all_packagings
        # Order by creation date with a secondary ordering by sourcepackage
        # name to ensure the ordering for test data where many packagings have
        # identical creation dates.
        results = results.order_by(
            Desc(Packaging.datecreated), SourcePackageName.name
        )[:5]
        return DecoratedResultSet(
            results, DistroSeries._packaging_row_to_packaging
        )

    @property
    def supported(self):
        return self.status in [
            SeriesStatus.CURRENT,
            SeriesStatus.SUPPORTED,
        ]

    @property
    def distroserieslanguages(self):
        return (
            IStore(DistroSeriesLanguage)
            .find(
                DistroSeriesLanguage,
                DistroSeriesLanguage.language == Language.id,
                DistroSeriesLanguage.distroseries == self,
                Is(Language.visible, True),
            )
            .order_by(Language.englishname)
        )

    @property
    def bug_reporting_guidelines(self):
        """See `IBugTarget`."""
        return self.distribution.bug_reporting_guidelines

    @property
    def content_templates(self):
        """See 'IBugTarget'."""
        return self.distribution.content_templates

    @property
    def bug_reported_acknowledgement(self):
        """See `IBugTarget`."""
        return self.distribution.bug_reported_acknowledgement

    def _getMilestoneCondition(self):
        """See `HasMilestonesMixin`."""
        return Milestone.distroseries == self

    def updatePackageCount(self):
        """See `IDistroSeries`."""
        self.sourcecount = (
            IStore(SourcePackagePublishingHistory)
            .find(
                SourcePackagePublishingHistory.sourcepackagename_id,
                SourcePackagePublishingHistory.distroseries == self,
                SourcePackagePublishingHistory.archive_id.is_in(
                    self.distribution.all_distro_archive_ids
                ),
                SourcePackagePublishingHistory.status.is_in(
                    active_publishing_status
                ),
                SourcePackagePublishingHistory.pocket
                == PackagePublishingPocket.RELEASE,
            )
            .config(distinct=True)
            .count()
        )

        self.binarycount = (
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory.binarypackagename_id,
                DistroArchSeries.distroseries == self,
                BinaryPackagePublishingHistory.distroarchseries_id
                == DistroArchSeries.id,
                BinaryPackagePublishingHistory.archive_id.is_in(
                    self.distribution.all_distro_archive_ids
                ),
                BinaryPackagePublishingHistory.status.is_in(
                    active_publishing_status
                ),
                BinaryPackagePublishingHistory.pocket
                == PackagePublishingPocket.RELEASE,
            )
            .config(distinct=True)
            .count()
        )

    @property
    def architecturecount(self):
        """See `IDistroSeries`."""
        return self.architectures.count()

    @property
    def fullseriesname(self):
        return "%s %s" % (
            self.distribution.name.capitalize(),
            self.name.capitalize(),
        )

    @property
    def bugtargetname(self):
        """See IBugTarget."""
        # XXX mpt 2007-07-10 bugs 113258, 113262:
        # The distribution's and series' names should be used instead
        # of fullseriesname.
        return self.fullseriesname

    @property
    def bugtargetdisplayname(self):
        """See IBugTarget."""
        return self.fullseriesname

    @property
    def bugtarget_parent(self):
        """See `ISeriesBugTarget`."""
        return self.parent

    @property
    def last_full_language_pack_exported(self):
        language_packs = IStore(LanguagePack).find(
            LanguagePack, distroseries=self, type=LanguagePackType.FULL
        )
        return language_packs.order_by(LanguagePack.date_exported).last()

    @property
    def last_delta_language_pack_exported(self):
        language_packs = IStore(LanguagePack).find(
            LanguagePack,
            distroseries=self,
            type=LanguagePackType.DELTA,
            updates=self.language_pack_base,
        )
        return language_packs.order_by(LanguagePack.date_exported).last()

    @property
    def backports_not_automatic(self):
        return self.publishing_options.get("backports_not_automatic", False)

    @backports_not_automatic.setter
    def backports_not_automatic(self, value):
        assert isinstance(value, bool)
        self.publishing_options["backports_not_automatic"] = value

    @property
    def proposed_not_automatic(self):
        return self.publishing_options.get("proposed_not_automatic", False)

    @proposed_not_automatic.setter
    def proposed_not_automatic(self, value):
        assert isinstance(value, bool)
        self.publishing_options["proposed_not_automatic"] = value

    @property
    def include_long_descriptions(self):
        return self.publishing_options.get("include_long_descriptions", True)

    @include_long_descriptions.setter
    def include_long_descriptions(self, value):
        assert isinstance(value, bool)
        self.publishing_options["include_long_descriptions"] = value

    @property
    def index_compressors(self):
        if "index_compressors" in self.publishing_options:
            return [
                IndexCompressionType.getTermByToken(name).value
                for name in self.publishing_options["index_compressors"]
            ]
        else:
            return list(DEFAULT_INDEX_COMPRESSORS)

    @index_compressors.setter
    def index_compressors(self, value):
        assert isinstance(value, list)
        self.publishing_options["index_compressors"] = [
            compressor.title for compressor in value
        ]

    @property
    def publish_by_hash(self):
        return self.publishing_options.get("publish_by_hash", False)

    @publish_by_hash.setter
    def publish_by_hash(self, value):
        assert isinstance(value, bool)
        self.publishing_options["publish_by_hash"] = value

    @property
    def advertise_by_hash(self):
        return self.publishing_options.get("advertise_by_hash", False)

    @advertise_by_hash.setter
    def advertise_by_hash(self, value):
        assert isinstance(value, bool)
        self.publishing_options["advertise_by_hash"] = value

    @property
    def strict_supported_component_dependencies(self):
        return self.publishing_options.get(
            "strict_supported_component_dependencies", True
        )

    @strict_supported_component_dependencies.setter
    def strict_supported_component_dependencies(self, value):
        assert isinstance(value, bool)
        self.publishing_options["strict_supported_component_dependencies"] = (
            value
        )

    @property
    def publish_i18n_index(self):
        return self.publishing_options.get("publish_i18n_index", True)

    @publish_i18n_index.setter
    def publish_i18n_index(self, value):
        assert isinstance(value, bool)
        self.publishing_options["publish_i18n_index"] = value

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this distribution series."""
        search_params.setDistroSeries(self)

    def _getOfficialTagClause(self):
        return self.distribution._getOfficialTagClause()

    @property
    def official_bug_tags(self):
        """See `IHasBugs`."""
        return self.distribution.official_bug_tags

    def specifications(
        self,
        user,
        sort=None,
        quantity=None,
        filter=None,
        need_people=True,
        need_branches=True,
        need_workitems=False,
    ):
        """See IHasSpecifications.

        In this case the rules for the default behaviour cover three things:

          - acceptance: if nothing is said, ACCEPTED only
          - completeness: if nothing is said, ANY
          - informationalness: if nothing is said, ANY

        """
        base_clauses = [Specification.distroseries == self]
        return search_specifications(
            self,
            base_clauses,
            user,
            sort,
            quantity,
            filter,
            default_acceptance=True,
            need_people=need_people,
            need_branches=need_branches,
            need_workitems=need_workitems,
        )

    def getDistroSeriesLanguage(self, language):
        """See `IDistroSeries`."""
        return (
            IStore(DistroSeriesLanguage)
            .find(DistroSeriesLanguage, distroseries=self, language=language)
            .one()
        )

    def getDistroSeriesLanguageOrEmpty(self, language):
        """See `IDistroSeries`."""
        drl = self.getDistroSeriesLanguage(language)
        if drl is not None:
            return drl
        return EmptyDistroSeriesLanguage(self, language)

    def updateStatistics(self, ztm):
        """See `IDistroSeries`."""
        # first find the set of all languages for which we have pofiles in
        # the distribution that are visible and not English
        langset = set(
            IStore(Language)
            .find(
                Language,
                Is(Language.visible, True),
                Language.id == POFile.language_id,
                Language.code != "en",
                POFile.potemplate_id == POTemplate.id,
                POTemplate.distroseries == self,
                Is(POTemplate.iscurrent, True),
            )
            .config(distinct=True)
        )

        # now run through the existing DistroSeriesLanguages for the
        # distroseries, and update their stats, and remove them from the
        # list of languages we need to have stats for
        for distroserieslanguage in self.distroserieslanguages:
            distroserieslanguage.updateStatistics(ztm)
            langset.discard(distroserieslanguage.language)
        # now we should have a set of languages for which we NEED
        # to have a DistroSeriesLanguage
        for lang in langset:
            drl = DistroSeriesLanguage(distroseries=self, language=lang)
            drl.updateStatistics(ztm)
        # lastly, we need to update the message count for this distro
        # series itself
        messagecount = 0
        for potemplate in self.getCurrentTranslationTemplates():
            messagecount += potemplate.messageCount()
        self.messagecount = messagecount
        ztm.commit()

    def getSourcePackage(self, name):
        """See `IDistroSeries`."""
        if not ISourcePackageName.providedBy(name):
            name = getUtility(ISourcePackageNameSet).queryByName(name)
            if name is None:
                return None
        return getUtility(ISourcePackageFactory).new(
            sourcepackagename=name, distroseries=self
        )

    def getExternalPackageSeries(self, name, packagetype, channel):
        """See `IDistroSeries`."""
        if ISourcePackageName.providedBy(name):
            sourcepackagename = name
        else:
            sourcepackagename = getUtility(ISourcePackageNameSet).queryByName(
                name
            )
            if sourcepackagename is None:
                return None
        return ExternalPackageSeries(
            self, sourcepackagename, packagetype, channel
        )

    def getArchiveSourcePackageSeries(self, archive, name):
        """See `IDistroSeries`."""
        if archive.distribution != self.distribution:
            return None
        if ISourcePackageName.providedBy(name):
            sourcepackagename = name
        else:
            sourcepackagename = getUtility(ISourcePackageNameSet).queryByName(
                name
            )
            if sourcepackagename is None:
                return None
        return ArchiveSourcePackageSeries(archive, self, sourcepackagename)

    def getBinaryPackage(self, name):
        """See `IDistroSeries`."""
        if not IBinaryPackageName.providedBy(name):
            name = getUtility(IBinaryPackageNameSet).queryByName(name)
            if name is None:
                return None
        return DistroSeriesBinaryPackage(self, name)

    def getCurrentSourceReleases(self, source_package_names):
        """See `IDistroSeries`."""
        return getUtility(IDistroSeriesSet).getCurrentSourceReleases(
            {self: source_package_names}
        )

    def getTranslatableSourcePackages(self):
        """See `IDistroSeries`."""
        result = (
            IStore(SourcePackageName)
            .find(
                SourcePackageName,
                POTemplate.sourcepackagename == SourcePackageName.id,
                Is(POTemplate.iscurrent, True),
                POTemplate.distroseries == self,
            )
            .order_by(SourcePackageName.name)
            .config(distinct=True)
        )
        return [
            SourcePackage(sourcepackagename=spn, distroseries=self)
            for spn in result
        ]

    def getUnlinkedTranslatableSourcePackages(self):
        """See `IDistroSeries`."""
        # Note that both unlinked packages and
        # linked-with-no-productseries packages are considered to be
        # "unlinked translatables".
        unlinked = (
            IStore(SourcePackageName)
            .find(
                SourcePackageName,
                Not(
                    SourcePackageName.id.is_in(
                        Select(
                            Packaging.sourcepackagename_id,
                            where=(Packaging.distroseries == self),
                            distinct=True,
                        )
                    )
                ),
                POTemplate.sourcepackagename == SourcePackageName.id,
                POTemplate.distroseries == self,
            )
            .order_by(SourcePackageName.name)
        )
        linked_but_no_productseries = (
            IStore(SourcePackageName)
            .find(
                SourcePackageName,
                Packaging.sourcepackagename == SourcePackageName.id,
                Is(Packaging.productseries_id, None),
                POTemplate.sourcepackagename == SourcePackageName.id,
                POTemplate.distroseries == self,
            )
            .order_by(SourcePackageName.name)
        )
        result = unlinked.union(linked_but_no_productseries)
        return [
            SourcePackage(sourcepackagename=spn, distroseries=self)
            for spn in result
        ]

    def isUnstable(self):
        """See `IDistroSeries`."""
        return self.status in ACTIVE_UNRELEASED_STATUSES

    def _getAllSources(self):
        """Get all sources ever published in this series' main archives."""
        return (
            IStore(SourcePackagePublishingHistory)
            .find(
                SourcePackagePublishingHistory,
                SourcePackagePublishingHistory.distroseries_id == self.id,
                SourcePackagePublishingHistory.archive_id.is_in(
                    self.distribution.all_distro_archive_ids
                ),
            )
            .order_by(SourcePackagePublishingHistory.id)
        )

    def _getAllBinaries(self):
        """Get all binaries ever published in this series' main archives."""
        return (
            IStore(BinaryPackagePublishingHistory)
            .find(
                BinaryPackagePublishingHistory,
                DistroArchSeries.distroseries == self,
                BinaryPackagePublishingHistory.distroarchseries_id
                == DistroArchSeries.id,
                BinaryPackagePublishingHistory.archive_id.is_in(
                    self.distribution.all_distro_archive_ids
                ),
            )
            .order_by(BinaryPackagePublishingHistory.id)
        )

    def getAllPublishedSources(self):
        """See `IDistroSeries`."""
        # Consider main archives only, and return all sources in
        # the PUBLISHED state.
        return self._getAllSources().find(
            status=PackagePublishingStatus.PUBLISHED
        )

    def getAllPublishedBinaries(self):
        """See `IDistroSeries`."""
        # Consider main archives only, and return all binaries in
        # the PUBLISHED state.
        return self._getAllBinaries().find(
            status=PackagePublishingStatus.PUBLISHED
        )

    def getAllUncondemnedSources(self):
        """See `IDistroSeries`."""
        return self._getAllSources().find(scheduleddeletiondate=None)

    def getAllUncondemnedBinaries(self):
        """See `IDistroSeries`."""
        return self._getAllBinaries().find(scheduleddeletiondate=None)

    def getBuildRecords(
        self,
        build_state=None,
        name=None,
        pocket=None,
        arch_tag=None,
        user=None,
        binary_only=True,
    ):
        """See IHasBuildRecords"""
        # Ignore "user", since it would not make any difference to the
        # records returned here (private builds are only in PPA right
        # now). We also ignore binary_only and always return binaries.
        return getUtility(IBinaryPackageBuildSet).getBuildsForDistro(
            self, build_state, name, pocket, arch_tag
        )

    def createUploadedSourcePackageRelease(
        self,
        sourcepackagename,
        version,
        format,
        architecturehintlist,
        creator,
        archive,
        maintainer=None,
        component=None,
        section=None,
        urgency=None,
        dscsigningkey=None,
        dsc=None,
        copyright=None,
        changelog=None,
        changelog_entry=None,
        builddepends=None,
        builddependsindep=None,
        build_conflicts=None,
        build_conflicts_indep=None,
        dsc_maintainer_rfc822=None,
        dsc_standards_version=None,
        dsc_format=None,
        dsc_binaries=None,
        dateuploaded=DEFAULT,
        source_package_recipe_build=None,
        ci_build=None,
        user_defined_fields=None,
        homepage=None,
        buildinfo=None,
    ):
        """See `IDistroSeries`."""
        return SourcePackageRelease(
            upload_distroseries=self,
            sourcepackagename=sourcepackagename,
            version=version,
            format=format,
            maintainer=maintainer,
            dateuploaded=dateuploaded,
            builddepends=builddepends,
            builddependsindep=builddependsindep,
            architecturehintlist=architecturehintlist,
            component=component,
            creator=creator,
            urgency=urgency,
            changelog=changelog,
            changelog_entry=changelog_entry,
            dsc=dsc,
            signing_key_owner=dscsigningkey.owner if dscsigningkey else None,
            signing_key_fingerprint=(
                dscsigningkey.fingerprint if dscsigningkey else None
            ),
            section=section,
            copyright=copyright,
            upload_archive=archive,
            dsc_maintainer_rfc822=dsc_maintainer_rfc822,
            dsc_standards_version=dsc_standards_version,
            dsc_format=dsc_format,
            dsc_binaries=dsc_binaries,
            build_conflicts=build_conflicts,
            build_conflicts_indep=build_conflicts_indep,
            source_package_recipe_build=source_package_recipe_build,
            ci_build=ci_build,
            user_defined_fields=user_defined_fields,
            homepage=homepage,
            buildinfo=buildinfo,
        )

    def getComponentByName(self, name):
        """See `IDistroSeries`."""
        comp = IStore(Component).find(Component, name=name).one()
        if comp is None:
            raise NotFoundError(name)
        permitted = set(self.components)
        if comp in permitted:
            return comp
        raise NotFoundError(name)

    def searchPackages(self, text):
        """See `IDistroSeries`."""
        find_spec = (
            DistroSeriesPackageCache,
            BinaryPackageName,
            SQL("ts_rank(fti, ftq(?)) AS rank", params=(text,)),
        )
        origin = [
            DistroSeriesPackageCache,
            Join(
                BinaryPackageName,
                DistroSeriesPackageCache.binarypackagename
                == BinaryPackageName.id,
            ),
        ]

        # Note: When attempting to convert the query below into straight
        # Storm expressions, a 'tuple index out-of-range' error was always
        # raised.
        package_caches = (
            IStore(BinaryPackageName)
            .using(*origin)
            .find(
                find_spec,
                DistroSeriesPackageCache.distroseries == self,
                DistroSeriesPackageCache.archive_id.is_in(
                    self.distribution.all_distro_archive_ids
                ),
                Or(
                    fti_search(DistroSeriesPackageCache, text),
                    DistroSeriesPackageCache.name.contains_string(
                        text.lower()
                    ),
                ),
            )
            .config(distinct=True)
        )

        # Create a function that will decorate the results, converting
        # them from the find_spec above into a DSBP:
        def result_to_dsbp(row):
            cache, binary_package_name, rank = row
            return DistroSeriesBinaryPackage(
                distroseries=cache.distroseries,
                binarypackagename=binary_package_name,
                cache=cache,
            )

        # Return the decorated result set so the consumer of these
        # results will only see DSBPs
        return DecoratedResultSet(package_caches, result_to_dsbp)

    def newArch(
        self,
        architecturetag,
        processor,
        official,
        owner,
        enabled=True,
        underlying_architecturetag=None,
    ):
        """See `IDistroSeries`."""
        das = DistroArchSeries(
            architecturetag=architecturetag,
            processor=processor,
            official=official,
            distroseries=self,
            owner=owner,
            enabled=enabled,
            underlying_architecturetag=underlying_architecturetag,
        )
        IStore(das).flush()
        return das

    def newMilestone(
        self, name, dateexpected=None, summary=None, code_name=None, tags=None
    ):
        """See `IDistroSeries`."""
        milestone = Milestone(
            name=name,
            code_name=code_name,
            dateexpected=dateexpected,
            summary=summary,
            distribution=self.distribution,
            distroseries=self,
        )
        if tags:
            milestone.setTags(tags.split())
        return milestone

    def getLatestUploads(self):
        """See `IDistroSeries`."""
        # Without this CTE, PostgreSQL sometimes decides to scan the primary
        # key index on PackageUpload instead, which is very slow.
        RelevantUpload = Table("RelevantUpload")
        relevant_upload_cte = WithMaterialized(
            RelevantUpload.name,
            IStore(PackageUpload),
            Select(
                PackageUpload.id,
                And(
                    PackageUpload.status == PackageUploadStatus.DONE,
                    PackageUpload.distroseries == self,
                    PackageUpload.archive_id.is_in(
                        self.distribution.all_distro_archive_ids
                    ),
                ),
            ),
        )
        clauses = [
            PackageUploadSource.sourcepackagerelease
            == SourcePackageRelease.id,
            SourcePackageRelease.sourcepackagename_id == SourcePackageName.id,
            PackageUploadSource.packageupload == Column("id", RelevantUpload),
        ]

        last_uploads = DecoratedResultSet(
            IStore(SourcePackageRelease)
            .with_(relevant_upload_cte)
            .find((SourcePackageRelease, SourcePackageName), *clauses)
            .order_by(Desc(Column("id", RelevantUpload)))[:5],
            result_decorator=itemgetter(0),
        )

        distro_sprs = [
            self.distribution.getSourcePackageRelease(spr)
            for spr in last_uploads
        ]

        return distro_sprs

    @staticmethod
    def setNewerDistroSeriesVersions(spphs):
        """Set the newer_distroseries_version attribute on the spph entries.

        :param spphs: The SourcePackagePublishingHistory objects to set the
            newer_distroseries_version attribute on.
        """
        # Partition by distro series to use getCurrentSourceReleases
        distro_series = collections.defaultdict(list)
        for spph in spphs:
            distro_series[spph.distroseries].append(spph)
        for series, spphs in distro_series.items():
            packagenames = set()
            for spph in spphs:
                packagenames.add(spph.sourcepackagerelease.sourcepackagename)
            latest_releases = series.getCurrentSourceReleases(packagenames)
            for spph in spphs:
                latest_release = latest_releases.get(spph.meta_sourcepackage)
                if (
                    latest_release is not None
                    and apt_pkg.version_compare(
                        latest_release.version, spph.source_package_version
                    )
                    > 0
                ):
                    version = latest_release
                else:
                    version = None
                get_property_cache(spph).newer_distroseries_version = version

    def createQueueEntry(
        self,
        pocket,
        archive,
        changesfilename=None,
        changesfilecontent=None,
        changes_file_alias=None,
        signing_key=None,
        package_copy_job=None,
    ):
        """See `IDistroSeries`."""
        if (changesfilename is None) != (changesfilecontent is None):
            raise AssertionError(
                "Inconsistent changesfilename and changesfilecontent. "
                "Pass either both, or neither."
            )
        if changes_file_alias is not None and changesfilename is not None:
            raise AssertionError(
                "Conflicting options: "
                "Both changesfilename and changes_file_alias were given."
            )
        have_changes_file = not (
            changesfilename is None and changes_file_alias is None
        )
        if package_copy_job is None and not have_changes_file:
            raise AssertionError(
                "changesfilename and changesfilecontent must be supplied "
                "if there is no package_copy_job"
            )

        if changesfilename is not None:
            # We store the changes file in the librarian to avoid having to
            # deal with broken encodings in these files; this will allow us
            # to regenerate these files as necessary.
            #
            # The use of StringIO here should be safe: we do not encoding of
            # the content in the changes file (as doing so would be guessing
            # at best, causing unpredictable corruption), and simply pass it
            # off to the librarian.

            # The PGP signature is stripped from all changesfiles
            # to avoid replay attacks (see bugs 159304 and 451396).
            signed_message = signed_message_from_bytes(changesfilecontent)
            if signed_message is not None:
                # Overwrite `changesfilecontent` with the text stripped
                # of the PGP signature.
                new_content = signed_message.signedContent
                if new_content is not None:
                    changesfilecontent = signed_message.signedContent

            changes_file_alias = getUtility(ILibraryFileAliasSet).create(
                changesfilename,
                len(changesfilecontent),
                BytesIO(changesfilecontent),
                "text/plain",
                restricted=archive.private,
            )

        return PackageUpload(
            distroseries=self,
            status=PackageUploadStatus.NEW,
            pocket=pocket,
            archive=archive,
            changesfile=changes_file_alias,
            signing_key_owner=signing_key.owner if signing_key else None,
            signing_key_fingerprint=(
                signing_key.fingerprint if signing_key else None
            ),
            package_copy_job=package_copy_job,
        )

    def getPackageUploadQueue(self, state):
        """See `IDistroSeries`."""
        return PackageUploadQueue(self, state)

    def getPackageUploads(
        self,
        status=None,
        created_since_date=None,
        archive=None,
        pocket=None,
        custom_type=None,
        name=None,
        version=None,
        exact_match=False,
    ):
        """See `IDistroSeries`."""
        return getUtility(IPackageUploadSet).getAll(
            self,
            created_since_date,
            status,
            archive,
            pocket,
            custom_type,
            name=name,
            version=version,
            exact_match=exact_match,
        )

    def getBugSummaryContextWhereClause(self):
        """See BugTargetBase."""
        # Circular fail.
        from lp.bugs.model.bugsummary import BugSummary

        return And(
            BugSummary.distroseries_id == self.id,
            BugSummary.sourcepackagename_id == None,
            BugSummary.ociproject_id == None,
        )

    def getPOFileContributorsByLanguage(self, language):
        """See `IDistroSeries`."""
        # Circular import.
        from lp.translations.model.pofiletranslator import POFileTranslator

        contributors = IStore(Person).find(
            Person,
            POFileTranslator.person_id == Person.id,
            POFile.id == POFileTranslator.pofile_id,
            POFile.language == language,
            POTemplate.id == POFile.potemplate_id,
            POTemplate.distroseries == self,
            POTemplate.iscurrent == True,
        )
        contributors = contributors.order_by(Person._separated_sortingColumns)
        contributors = contributors.config(distinct=True)
        return contributors

    @property
    def main_archive(self):
        return self.distribution.main_archive

    def getTemplatesCollection(self):
        """See `IHasTranslationTemplates`."""
        return TranslationTemplatesCollection().restrictDistroSeries(self)

    def getSharingPartner(self):
        """See `IHasTranslationTemplates`."""
        # No sharing partner is defined for DistroSeries.
        return None

    def getSuite(self, pocket):
        """See `IDistroSeries`."""
        if pocket == PackagePublishingPocket.RELEASE:
            return self.name
        else:
            return "%s%s" % (self.name, pocketsuffix[pocket])

    def isSourcePackageFormatPermitted(self, format):
        return (
            getUtility(ISourcePackageFormatSelectionSet).getBySeriesAndFormat(
                self, format
            )
            is not None
        )

    def initDerivedDistroSeries(
        self,
        user,
        parents,
        architectures=(),
        archindep_archtag=None,
        packagesets=(),
        rebuild=False,
        overlays=(),
        overlay_pockets=(),
        overlay_components=(),
    ):
        """See `IDistroSeries`."""
        from lp.soyuz.scripts.initialize_distroseries import (
            InitializationError,
            InitializeDistroSeries,
        )

        if self.isDerivedSeries():
            raise DerivationError(
                "DistroSeries %s already has parent series." % self.name
            )
        initialize_series = InitializeDistroSeries(
            self,
            parents,
            architectures,
            archindep_archtag,
            packagesets,
            rebuild,
            overlays,
            overlay_pockets,
            overlay_components,
        )
        try:
            initialize_series.check()
        except InitializationError as e:
            raise DerivationError(e)
        getUtility(IInitializeDistroSeriesJobSource).create(
            self,
            parents,
            architectures,
            archindep_archtag,
            packagesets,
            rebuild,
            overlays,
            overlay_pockets,
            overlay_components,
        )

    def getParentSeries(self):
        """See `IDistroSeriesPublic`."""
        # Circular imports.
        from lp.registry.interfaces.distroseriesparent import (
            IDistroSeriesParentSet,
        )

        dsp_set = getUtility(IDistroSeriesParentSet)
        dsps = dsp_set.getByDerivedSeries(self).order_by("ordering")
        return [dsp.parent_series for dsp in dsps]

    def getDerivedSeries(self):
        """See `IDistroSeriesPublic`."""
        # Circular imports.
        from lp.registry.interfaces.distroseriesparent import (
            IDistroSeriesParentSet,
        )

        dsps = getUtility(IDistroSeriesParentSet).getByParentSeries(self)
        return [dsp.derived_series for dsp in dsps]

    def getBugTaskWeightFunction(self):
        """Provide a weight function to determine optimal bug task.

        Full weight is given to tasks for this distro series.

        If the series isn't found, the distribution task is better than
        others.
        """
        series_id = self.id
        distribution_id = self.distribution_id

        def weight_function(bugtask):
            if bugtask.distroseries_id == series_id:
                return OrderedBugTask(1, bugtask.id, bugtask)
            elif bugtask.distribution_id == distribution_id:
                return OrderedBugTask(2, bugtask.id, bugtask)
            else:
                return OrderedBugTask(3, bugtask.id, bugtask)

        return weight_function

    def getDifferencesTo(
        self,
        parent_series=None,
        difference_type=None,
        source_package_name_filter=None,
        status=None,
        child_version_higher=False,
    ):
        """See `IDistroSeries`."""
        return getUtility(IDistroSeriesDifferenceSource).getForDistroSeries(
            self,
            difference_type=difference_type,
            name_filter=source_package_name_filter,
            status=status,
            child_version_higher=child_version_higher,
        )

    def isDerivedSeries(self):
        """See `IDistroSeries`."""
        return not self.getParentSeries() == []

    def isInitializing(self):
        """See `IDistroSeries`."""
        job = self.getInitializationJob()
        return job is not None and job.is_pending

    def isInitialized(self):
        """See `IDistroSeries`."""
        published = self.main_archive.getPublishedSources(distroseries=self)
        return not published.is_empty()

    def getInitializationJob(self):
        """See `IDistroSeries`."""
        return getUtility(IInitializeDistroSeriesJobSource).get(self)

    def getDifferenceComments(self, since=None, source_package_name=None):
        """See `IDistroSeries`."""
        comment_source = getUtility(IDistroSeriesDifferenceCommentSource)
        return comment_source.getForDistroSeries(
            self, since=since, source_package_name=source_package_name
        )

    def getTranslationTemplateStatistics(
        self,
    ) -> List[DistroSeriesTranslationTemplateStatistics]:
        """See `IDistroSeries`."""
        rows = (
            IStore(POTemplate)
            .find(
                (
                    SourcePackageName.name,
                    POTemplate.translation_domain,
                    POTemplate.name,
                    POTemplate.messagecount,
                    POTemplate.iscurrent,
                    POTemplate.languagepack,
                    POTemplate.priority,
                    POTemplate.date_last_updated,
                ),
                POTemplate.distroseries == self,
                POTemplate.sourcepackagename == SourcePackageName.id,
            )
            .order_by(SourcePackageName.name, POTemplate.name)
        )
        return [
            {
                "sourcepackage": row[0],
                "translation_domain": row[1],
                "template_name": row[2],
                "total": row[3],
                "enabled": row[4],
                "languagepack": row[5],
                "priority": row[6],
                "date_last_updated": row[7],
            }
            for row in rows
        ]


@implementer(IDistroSeriesSet)
class DistroSeriesSet:
    def get(self, distroseriesid):
        """See `IDistroSeriesSet`."""
        return IStore(DistroSeries).get(DistroSeries, distroseriesid)

    def translatables(self):
        """See `IDistroSeriesSet`."""
        # Join POTemplate distinctly to only get entries with available
        # translations.
        return (
            IStore(DistroSeries)
            .using((DistroSeries, POTemplate))
            .find(
                DistroSeries,
                DistroSeries.hide_all_translations == False,
                DistroSeries.id == POTemplate.distroseries_id,
            )
            .config(distinct=True)
        )

    def queryByName(self, distribution, name, follow_aliases=False):
        """See `IDistroSeriesSet`."""
        series = (
            IStore(DistroSeries)
            .find(DistroSeries, distribution=distribution, name=name)
            .one()
        )
        if series is not None:
            return series
        if follow_aliases:
            try:
                return distribution.resolveSeriesAlias(name)
            except NoSuchDistroSeries:
                pass
        return None

    def queryByVersion(self, distribution, version):
        """See `IDistroSeriesSet`."""
        return (
            IStore(DistroSeries)
            .find(DistroSeries, distribution=distribution, version=version)
            .one()
        )

    def _parseSuite(self, suite):
        """Parse 'suite' into a series name and a pocket."""
        tokens = suite.rsplit("-", 1)
        if len(tokens) == 1:
            return suite, PackagePublishingPocket.RELEASE
        series, pocket = tokens
        try:
            pocket = PackagePublishingPocket.items[pocket.upper()]
        except KeyError:
            # No such pocket. Probably trying to get a hyphenated series name.
            return suite, PackagePublishingPocket.RELEASE
        else:
            return series, pocket

    def fromSuite(self, distribution, suite):
        """See `IDistroSeriesSet`."""
        series_name, pocket = self._parseSuite(suite)
        series = distribution.getSeries(series_name)
        return series, pocket

    def getCurrentSourceReleases(self, distro_series_source_packagenames):
        """See `IDistroSeriesSet`."""
        releases = get_current_source_releases(
            distro_series_source_packagenames,
            lambda series: series.distribution.all_distro_archive_ids,
            (
                lambda series: SourcePackagePublishingHistory.distroseries
                == series
            ),
            [],
            SourcePackagePublishingHistory.distroseries_id,
        )
        result = {}
        for spr, series_id in releases:
            series = getUtility(IDistroSeriesSet).get(series_id)
            result[series.getSourcePackage(spr.sourcepackagename)] = (
                DistributionSourcePackageRelease(series.distribution, spr)
            )
        return result

    def search(self, distribution=None, isreleased=None, orderBy=None):
        """See `IDistroSeriesSet`."""
        clauses = []
        if distribution is not None:
            clauses.append(DistroSeries.distribution == distribution)
        if isreleased is not None:
            if isreleased:
                # The query is filtered on released releases.
                clauses.append(
                    DistroSeries.status.is_in(ACTIVE_RELEASED_STATUSES)
                )
            else:
                # The query is filtered on unreleased releases.
                clauses.append(
                    DistroSeries.status.is_in(ACTIVE_UNRELEASED_STATUSES)
                )
        rows = IStore(DistroSeries).find(DistroSeries, *clauses)
        if orderBy is not None:
            rows = rows.order_by(orderBy)
        return rows
