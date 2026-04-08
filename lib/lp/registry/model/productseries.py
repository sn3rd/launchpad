# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Models for `IProductSeries`."""

__all__ = [
    "ProductSeries",
    "ProductSeriesSet",
    "TimelineProductSeries",
]

import datetime
from operator import itemgetter

from lazr.delegates import delegate_to
from storm.expr import Max, Sum
from storm.locals import (
    And,
    DateTime,
    Desc,
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
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
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
from lp.registry.errors import ProprietaryPillar
from lp.registry.interfaces.packaging import IPackagingUtil, PackagingType
from lp.registry.interfaces.person import validate_person
from lp.registry.interfaces.productrelease import IProductReleaseSet
from lp.registry.interfaces.productseries import (
    IProductSeries,
    IProductSeriesSet,
    ITimelineProductSeries,
)
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.model.milestone import HasMilestonesMixin, Milestone
from lp.registry.model.productrelease import ProductRelease
from lp.registry.model.series import SeriesMixin
from lp.services.database.constants import UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.propertycache import cachedproperty
from lp.services.webapp.publisher import canonical_url
from lp.services.webapp.sorting import sorted_dotted_numbers
from lp.services.worlddata.model.language import Language
from lp.translations.interfaces.translations import (
    TranslationsBranchImportMode,
)
from lp.translations.model.hastranslationimports import (
    HasTranslationImportsMixin,
)
from lp.translations.model.hastranslationtemplates import (
    HasTranslationTemplatesMixin,
)
from lp.translations.model.pofile import POFile
from lp.translations.model.potemplate import (
    POTemplate,
    TranslationTemplatesCollection,
)
from lp.translations.model.productserieslanguage import ProductSeriesLanguage

MAX_TIMELINE_MILESTONES = 20


def landmark_key(landmark):
    """Sorts landmarks by date and name."""
    if landmark["date"] is None:
        # Null dates are assumed to be in the future.
        date = "9999-99-99"
    else:
        date = landmark["date"]
    return date + landmark["name"]


@delegate_to(ISpecificationTarget, context="product")
@implementer(IBugSummaryDimension, IProductSeries, ISeriesBugTarget)
class ProductSeries(
    StormBase,
    SeriesMixin,
    BugTargetBase,
    HasMilestonesMixin,
    HasSpecificationsMixin,
    HasTranslationImportsMixin,
    HasTranslationTemplatesMixin,
    StructuralSubscriptionTargetMixin,
):
    """A series of product releases."""

    __storm_table__ = "ProductSeries"

    id = Int(primary=True)
    product_id = Int(name="product", allow_none=False)
    product = Reference(product_id, "Product.id")
    status = DBEnum(
        allow_none=False, enum=SeriesStatus, default=SeriesStatus.DEVELOPMENT
    )
    name = Unicode(allow_none=False)
    datecreated = DateTime(
        allow_none=False, default=UTC_NOW, tzinfo=datetime.timezone.utc
    )
    owner_id = Int(name="owner", validator=validate_person, allow_none=False)
    owner = Reference(owner_id, "Person.id")

    driver_id = Int(
        name="driver", validator=validate_person, allow_none=True, default=None
    )
    driver = Reference(driver_id, "Person.id")
    branch_id = Int(name="branch", default=None)
    branch = Reference(branch_id, "Branch.id")

    def validate_autoimport_mode(self, attr, value):
        # Perform the normal validation for None
        if value is None:
            return value
        if (
            self.product.private
            and value != TranslationsBranchImportMode.NO_IMPORT
        ):
            raise ProprietaryPillar(
                "Translations are disabled for" " proprietary projects."
            )
        return value

    translations_autoimport_mode = DBEnum(
        name="translations_autoimport_mode",
        allow_none=False,
        enum=TranslationsBranchImportMode,
        default=TranslationsBranchImportMode.NO_IMPORT,
        validator=validate_autoimport_mode,
    )
    translations_branch_id = Int(
        name="translations_branch", allow_none=True, default=None
    )
    translations_branch = Reference(translations_branch_id, "Branch.id")
    # where are the tarballs released from this branch placed?
    releasefileglob = Unicode(default=None)
    releaseverstyle = Unicode(default=None)

    packagings = ReferenceSet(
        "id", "Packaging.productseries_id", order_by=Desc("Packaging.id")
    )

    def __init__(
        self, product, name, owner, summary, branch=None, releasefileglob=None
    ):
        super().__init__()
        self.product = product
        self.name = name
        self.owner = owner
        self.summary = summary
        self.branch = branch
        self.releasefileglob = releasefileglob

    @property
    def bug_target_parent(self):
        """See `IBugTarget`."""
        return self.product

    @property
    def pillar(self):
        """See `IBugTarget`."""
        return self.product

    @property
    def series(self):
        """See `ISeriesBugTarget`."""
        return self

    @property
    def answers_usage(self):
        """See `IServiceUsage.`"""
        return self.product.answers_usage

    @property
    def blueprints_usage(self):
        """See `IServiceUsage.`"""
        return self.product.blueprints_usage

    @property
    def translations_usage(self):
        """See `IServiceUsage.`"""
        return self.product.translations_usage

    @property
    def codehosting_usage(self):
        """See `IServiceUsage.`"""
        return self.product.codehosting_usage

    @property
    def bug_tracking_usage(self):
        """See `IServiceUsage.`"""
        return self.product.bug_tracking_usage

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

    def _getMilestoneCondition(self):
        """See `HasMilestonesMixin`."""
        return Milestone.productseries == self

    @property
    def releases(self):
        """See `IProductSeries`."""
        store = Store.of(self)

        # The Milestone is cached too because most uses of a ProductRelease
        # need it. The decorated resultset returns just the ProductRelease.
        result = store.find(
            (ProductRelease, Milestone),
            Milestone.productseries == self,
            ProductRelease.milestone == Milestone.id,
        )
        result = result.order_by(Desc("datereleased"))
        return DecoratedResultSet(result, result_decorator=itemgetter(0))

    @cachedproperty
    def _cached_releases(self):
        return self.releases

    def getCachedReleases(self):
        """See `IProductSeries`."""
        return self._cached_releases

    @property
    def release_files(self):
        """See `IProductSeries`."""
        files = set()
        for release in self.releases:
            files = files.union(release.files)
        return files

    @property
    def displayname(self):
        return self.name

    @property
    def parent(self):
        """See IProductSeries."""
        return self.product

    @property
    def bugtargetdisplayname(self):
        """See IBugTarget."""
        return "%s %s" % (self.product.displayname, self.name)

    @property
    def bugtargetname(self):
        """See IBugTarget."""
        return "%s/%s" % (self.product.name, self.name)

    @property
    def bugtarget_parent(self):
        """See `ISeriesBugTarget`."""
        return self.parent

    def getPOTemplate(self, name):
        """See IProductSeries."""
        return (
            IStore(POTemplate)
            .find(POTemplate, productseries=self, name=name)
            .one()
        )

    @property
    def title(self):
        return "%s %s series" % (self.product.displayname, self.displayname)

    @property
    def bug_reporting_guidelines(self):
        """See `IBugTarget`."""
        return self.product.bug_reporting_guidelines

    @property
    def content_templates(self):
        """See `IBugTarget`."""
        return self.product.content_templates

    @property
    def bug_reported_acknowledgement(self):
        """See `IBugTarget`."""
        return self.product.bug_reported_acknowledgement

    @property
    def enable_bugfiling_duplicate_search(self):
        """See `IBugTarget`."""
        return self.product.enable_bugfiling_duplicate_search

    @property
    def sourcepackages(self):
        """See IProductSeries"""
        from lp.registry.model.sourcepackage import SourcePackage

        ret = self.packagings
        ret = [
            SourcePackage(
                sourcepackagename=r.sourcepackagename,
                distroseries=r.distroseries,
            )
            for r in ret
        ]
        ret.sort(
            key=lambda a: a.distribution.name
            + a.distroseries.version
            + a.sourcepackagename.name
        )
        return ret

    @property
    def is_development_focus(self):
        """See `IProductSeries`."""
        return self == self.product.development_focus

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

        The rules for filtering are that there are three areas where you can
        apply a filter:

          - acceptance, which defaults to ACCEPTED if nothing is said,
          - completeness, which defaults to showing BOTH if nothing is said
          - informational, which defaults to showing BOTH if nothing is said

        """
        base_clauses = [Specification.productseries == self]
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

    @property
    def all_specifications(self):
        return Store.of(self).find(
            Specification, Specification.productseries == self
        )

    def _customizeSearchParams(self, search_params):
        """Customize `search_params` for this product series."""
        search_params.setProductSeries(self)

    def _getOfficialTagClause(self):
        return self.product._getOfficialTagClause()

    @property
    def official_bug_tags(self):
        """See `IHasBugs`."""
        return self.product.official_bug_tags

    def getBugSummaryContextWhereClause(self):
        """See BugTargetBase."""
        # Circular fail.
        from lp.bugs.model.bugsummary import BugSummary

        return And(
            BugSummary.productseries_id == self.id,
            BugSummary.ociproject_id == None,
        )

    def getLatestRelease(self):
        """See `IProductRelease.`"""
        try:
            return self.releases[0]
        except IndexError:
            return None

    def getRelease(self, version):
        return getUtility(IProductReleaseSet).getBySeriesAndVersion(
            self, version
        )

    def getPackage(self, distroseries):
        """See IProductSeries."""
        for pkg in self.sourcepackages:
            if pkg.distroseries == distroseries:
                return pkg
        # XXX sabdfl 2005-06-23: This needs to search through the ancestry of
        # the distroseries to try to find a relevant packaging record
        raise NotFoundError(distroseries)

    def getUbuntuTranslationFocusPackage(self):
        """See `IProductSeries`."""
        ubuntu = getUtility(ILaunchpadCelebrities).ubuntu
        translation_focus = ubuntu.translation_focus
        current_series = ubuntu.currentseries
        candidate = None
        for package in self.sourcepackages:
            if package.distroseries == translation_focus:
                return package
            if package.distroseries == current_series:
                candidate = package
            elif package.distroseries.distribution == ubuntu:
                if candidate is None:
                    candidate = package
        return candidate

    def setPackaging(self, distroseries, sourcepackagename, owner):
        """See IProductSeries."""
        if distroseries.distribution.official_packages:
            source_package = distroseries.getSourcePackage(sourcepackagename)
            if source_package.currentrelease is None:
                raise AssertionError(
                    "The source package is not published in %s."
                    % distroseries.displayname
                )
        for pkg in self.packagings:
            if (
                pkg.distroseries == distroseries
                and pkg.sourcepackagename == sourcepackagename
            ):
                # we have found a matching Packaging record
                # and it has the same source package name
                return pkg

        # ok, we didn't find a packaging record that matches, let's go ahead
        # and create one
        pkg = getUtility(IPackagingUtil).createPackaging(
            distroseries=distroseries,
            sourcepackagename=sourcepackagename,
            productseries=self,
            packaging=PackagingType.PRIME,
            owner=owner,
        )
        IStore(pkg).flush()  # convert UTC_NOW to actual datetime
        return pkg

    def getPackagingInDistribution(self, distribution):
        """See IProductSeries."""
        history = []
        for pkging in self.packagings:
            if pkging.distroseries.distribution == distribution:
                history.append(pkging)
        return history

    def newMilestone(
        self, name, dateexpected=None, summary=None, code_name=None, tags=None
    ):
        """See IProductSeries."""
        milestone = Milestone(
            name=name,
            dateexpected=dateexpected,
            summary=summary,
            product=self.product,
            productseries=self,
            code_name=code_name,
        )
        if tags:
            milestone.setTags(tags.split())
        return milestone

    def getTemplatesCollection(self):
        """See `IHasTranslationTemplates`."""
        return TranslationTemplatesCollection().restrictProductSeries(self)

    def getSharingPartner(self):
        """See `IHasTranslationTemplates`."""
        return self.getUbuntuTranslationFocusPackage()

    @property
    def potemplate_count(self):
        """See `IProductSeries`."""
        return self.getCurrentTranslationTemplates().count()

    @property
    def productserieslanguages(self):
        """See `IProductSeries`."""
        store = Store.of(self)

        english = getUtility(ILaunchpadCelebrities).english

        results = []
        if self.potemplate_count == 1:
            # If there is only one POTemplate in a ProductSeries, fetch
            # Languages and corresponding POFiles with one query, along
            # with their stats, and put them into ProductSeriesLanguage
            # objects.
            origin = [Language, POFile, POTemplate]
            query = store.using(*origin).find(
                (Language, POFile),
                POFile.language == Language.id,
                Language.visible == True,
                POFile.potemplate == POTemplate.id,
                POTemplate.productseries == self,
                POTemplate.iscurrent == True,
                Language.id != english.id,
            )

            ordered_results = query.order_by(Language.englishname)

            for language, pofile in ordered_results:
                psl = ProductSeriesLanguage(self, language, pofile=pofile)
                total = pofile.potemplate.messageCount()
                imported = pofile.currentCount()
                changed = pofile.updatesCount()
                rosetta = pofile.rosettaCount()
                unreviewed = pofile.unreviewedCount()
                translated = imported + rosetta
                new = rosetta - changed
                psl.setCounts(total, translated, new, changed, unreviewed)
                psl.last_changed_date = pofile.date_changed
                results.append(psl)
        else:
            # If there is more than one template, do a single
            # query to count total messages in all templates.
            query = store.find(
                Sum(POTemplate.messagecount),
                POTemplate.productseries == self,
                POTemplate.iscurrent == True,
            )
            (total,) = query
            # And another query to fetch all Languages with translations
            # in this ProductSeries, along with their cumulative stats
            # for imported, changed, rosetta-provided and unreviewed
            # translations.
            query = store.find(
                (
                    Language,
                    Sum(POFile.currentcount),
                    Sum(POFile.updatescount),
                    Sum(POFile.rosettacount),
                    Sum(POFile.unreviewed_count),
                    Max(POFile.date_changed),
                ),
                POFile.language == Language.id,
                Language.visible == True,
                POFile.potemplate == POTemplate.id,
                POTemplate.productseries == self,
                POTemplate.iscurrent == True,
                Language.id != english.id,
            ).group_by(Language)

            ordered_results = query.order_by(Language.englishname)

            for (
                language,
                imported,
                changed,
                rosetta,
                unreviewed,
                last_changed,
            ) in ordered_results:
                psl = ProductSeriesLanguage(self, language)
                translated = imported + rosetta
                new = rosetta - changed
                psl.setCounts(total, translated, new, changed, unreviewed)
                psl.last_changed_date = last_changed
                results.append(psl)

        return results

    def getTimeline(self, include_inactive=False):
        landmarks = []
        for milestone in self.all_milestones_with_releases()[
            :MAX_TIMELINE_MILESTONES
        ]:
            if milestone.product_release is None:
                # Skip inactive milestones, but include releases,
                # even if include_inactive is False.
                if not include_inactive and not milestone.active:
                    continue
                node_type = "milestone"
                date = milestone.dateexpected
                uri = canonical_url(milestone, path_only_if_possible=True)
            else:
                node_type = "release"
                date = milestone.product_release.datereleased
                uri = canonical_url(
                    milestone.product_release, path_only_if_possible=True
                )

            if isinstance(date, datetime.datetime):
                date = date.date().isoformat()
            elif isinstance(date, datetime.date):
                date = date.isoformat()

            entry = dict(
                name=milestone.name,
                code_name=milestone.code_name,
                type=node_type,
                date=date,
                uri=uri,
            )
            landmarks.append(entry)

        landmarks = sorted_dotted_numbers(landmarks, key=landmark_key)
        landmarks.reverse()
        return TimelineProductSeries(
            name=self.name,
            is_development_focus=self.is_development_focus,
            status=self.status,
            uri=canonical_url(self, path_only_if_possible=True),
            landmarks=landmarks,
            product=self.product,
        )

    def getBugTaskWeightFunction(self):
        """Provide a weight function to determine optimal bug task.

        Full weight is given to tasks for this product series.

        If the series isn't found, the product task is better than others.
        """
        series_id = self.id
        product_id = self.product_id

        def weight_function(bugtask):
            if bugtask.productseries_id == series_id:
                return OrderedBugTask(1, bugtask.id, bugtask)
            elif bugtask.product_id == product_id:
                return OrderedBugTask(2, bugtask.id, bugtask)
            else:
                return OrderedBugTask(3, bugtask.id, bugtask)

        return weight_function

    def userCanView(self, user):
        """See `IproductSeriesPublic`."""
        # Deleate the permission check to the parent product.
        return self.product.userCanView(user)


@implementer(ITimelineProductSeries)
class TimelineProductSeries:
    """See `ITimelineProductSeries`."""

    def __init__(
        self, name, status, is_development_focus, uri, landmarks, product
    ):
        self.name = name
        self.status = status
        self.is_development_focus = is_development_focus
        self.uri = uri
        self.landmarks = landmarks
        self.product = product


@implementer(IProductSeriesSet)
class ProductSeriesSet:
    """See IProductSeriesSet."""

    def __getitem__(self, series_id):
        """See IProductSeriesSet."""
        series = self.get(series_id)
        if series is None:
            raise NotFoundError(series_id)
        return series

    def get(self, series_id, default=None):
        """See IProductSeriesSet."""
        series = IStore(ProductSeries).get(ProductSeries, series_id)
        if series is None:
            return default
        return series

    def findByTranslationsImportBranch(
        self, branch, force_translations_upload=False
    ):
        """See IProductSeriesSet."""
        conditions = [ProductSeries.branch == branch]
        if not force_translations_upload:
            import_mode = ProductSeries.translations_autoimport_mode
            conditions.append(
                import_mode != TranslationsBranchImportMode.NO_IMPORT
            )

        return Store.of(branch).find(ProductSeries, And(*conditions))
