# Copyright 2011-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Bug domain vocabularies"""

__all__ = [
    "UsesBugsDistributionVocabulary",
    "BugNominatableDistroSeriesVocabulary",
    "BugNominatableProductSeriesVocabulary",
    "BugNominatableSeriesVocabulary",
    "BugTaskMilestoneVocabulary",
    "BugTrackerVocabulary",
    "BugVocabulary",
    "BugWatchVocabulary",
    "DistributionUsingMaloneVocabulary",
    "project_products_using_malone_vocabulary_factory",
    "UsesBugsDistributionVocabulary",
    "WebBugTrackerVocabulary",
]

from storm.expr import And, Is, Or
from zope.component import getUtility
from zope.interface import implementer
from zope.schema.interfaces import IVocabularyTokenized
from zope.schema.vocabulary import SimpleTerm, SimpleVocabulary
from zope.security.proxy import removeSecurityProxy

from lp.app.browser.stringformatter import FormattersAPI
from lp.app.enums import ServiceUsage
from lp.bugs.interfaces.bugtask import IBugTask, IBugTaskSet
from lp.bugs.interfaces.bugtracker import BugTrackerType
from lp.bugs.model.bug import Bug
from lp.bugs.model.bugtracker import BugTracker
from lp.bugs.model.bugwatch import BugWatch
from lp.registry.interfaces.distribution import IDistribution
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.productseries import IProductSeries
from lp.registry.interfaces.projectgroup import IProjectGroup
from lp.registry.interfaces.series import SeriesStatus
from lp.registry.interfaces.sourcepackage import ISourcePackage
from lp.registry.model.distribution import Distribution
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.milestone import milestone_sort_key
from lp.registry.model.productseries import ProductSeries
from lp.registry.vocabularies import DistributionVocabulary
from lp.services.database.interfaces import IStore
from lp.services.helpers import shortlist
from lp.services.webapp.escaping import html_escape, structured
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.vocabulary import (
    CountableIterator,
    IHugeVocabulary,
    NamedStormVocabulary,
    StormVocabularyBase,
)


class UsesBugsDistributionVocabulary(DistributionVocabulary):
    """Distributions that use Launchpad to track bugs.

    If the context is a distribution, it is always included in the
    vocabulary. Historic data is not invalidated if a distro stops
    using Launchpad to track bugs. This vocabulary offers the correct
    choices of distributions at this moment.
    """

    def __init__(self, context=None):
        super().__init__(context=context)
        self.distribution = IDistribution(self.context, None)

    @property
    def _clauses(self):
        if self.distribution is None:
            distro_id = 0
        else:
            distro_id = self.distribution.id
        return [
            Or(
                Is(self._table.official_malone, True),
                self._table.id == distro_id,
            )
        ]


class BugVocabulary(StormVocabularyBase):
    _table = Bug
    _order_by = "id"


@implementer(IHugeVocabulary)
class BugTrackerVocabulary(StormVocabularyBase):
    """All web and email based external bug trackers."""

    displayname = "Select a bug tracker"
    step_title = "Search"
    _table = BugTracker
    _filter = True
    _order_by = [BugTracker.title]

    def toTerm(self, obj):
        """See `IVocabulary`."""
        return SimpleTerm(obj, obj.name, obj.title)

    def getTermByToken(self, token):
        """See `IVocabularyTokenized`."""
        result = (
            IStore(self._table)
            .find(self._table, self._filter, BugTracker.name == token)
            .one()
        )
        if result is None:
            raise LookupError(token)
        return self.toTerm(result)

    def search(self, query, vocab_filter=None):
        """Search for web bug trackers."""
        query = query.lower()
        results = IStore(self._table).find(
            self._table,
            And(
                self._filter,
                BugTracker.active == True,
                Or(
                    BugTracker.name.contains_string(query),
                    BugTracker.title.contains_string(query),
                    BugTracker.summary.contains_string(query),
                    BugTracker.baseurl.contains_string(query),
                ),
            ),
        )
        results = results.order_by(self._order_by)
        return results

    def searchForTerms(self, query=None, vocab_filter=None):
        """See `IHugeVocabulary`."""
        results = self.search(query, vocab_filter)
        return CountableIterator(results.count(), results, self.toTerm)


class WebBugTrackerVocabulary(BugTrackerVocabulary):
    """All web-based bug tracker types."""

    _filter = BugTracker.bugtrackertype != BugTrackerType.EMAILADDRESS


def project_products_using_malone_vocabulary_factory(context):
    """Return a vocabulary containing a project's products using Malone."""
    project = IProjectGroup(context)
    return SimpleVocabulary(
        [
            SimpleTerm(product, product.name, title=product.displayname)
            for product in project.products
            if product.bug_tracking_usage == ServiceUsage.LAUNCHPAD
        ]
    )


class BugWatchVocabulary(StormVocabularyBase):
    _table = BugWatch

    def __iter__(self):
        assert IBugTask.providedBy(
            self.context
        ), "BugWatchVocabulary expects its context to be an IBugTask."
        bug = self.context.bug

        for watch in bug.watches:
            yield self.toTerm(watch)

    def toTerm(self, watch):
        if watch.url.startswith("mailto:"):
            user = getUtility(ILaunchBag).user
            if user is None:
                title = html_escape(
                    FormattersAPI(watch.bugtracker.title).obfuscate_email()
                )
            else:
                url = watch.url
                if url in watch.bugtracker.title:
                    title = html_escape(watch.bugtracker.title).replace(
                        html_escape(url),
                        structured(
                            '<a href="%s">%s</a>', url, url
                        ).escapedtext,
                    )
                else:
                    title = structured(
                        '%s &lt;<a href="%s">%s</a>&gt;',
                        watch.bugtracker.title,
                        url,
                        url[7:],
                    ).escapedtext
        else:
            title = structured(
                '%s <a href="%s">#%s</a>',
                watch.bugtracker.title,
                watch.url,
                watch.remotebug,
            ).escapedtext

        # title is already HTML-escaped.
        return SimpleTerm(watch, watch.id, title)


@implementer(IVocabularyTokenized)
class DistributionUsingMaloneVocabulary:
    """All the distributions that uses Malone officially."""

    _order_by = Distribution.display_name

    def __init__(self, context=None):
        self.context = context

    def __iter__(self):
        """Return an iterator which provides the terms from the vocabulary."""
        distributions_using_malone = (
            IStore(Distribution)
            .find(Distribution, official_malone=True)
            .order_by(self._order_by)
        )
        for distribution in distributions_using_malone:
            yield self.getTerm(distribution)

    def __len__(self):
        return (
            IStore(Distribution)
            .find(Distribution, official_malone=True)
            .count()
        )

    def __contains__(self, obj):
        return (
            IDistribution.providedBy(obj)
            and obj.bug_tracking_usage == ServiceUsage.LAUNCHPAD
        )

    def getTerm(self, obj):
        if obj not in self:
            raise LookupError(obj)
        return SimpleTerm(obj, obj.name, obj.displayname)

    def getTermByToken(self, token):
        found_dist = (
            IStore(Distribution)
            .find(Distribution, name=token, official_malone=True)
            .one()
        )
        if found_dist is None:
            raise LookupError(token)
        return self.getTerm(found_dist)


def BugNominatableSeriesVocabulary(context=None):
    """Return a nominatable series vocabulary."""
    # Check if we're in an archive context
    if getUtility(ILaunchBag).bugtask.archive:
        return BugNominatableDistroSeriesVocabulary(
            context, getUtility(ILaunchBag).bugtask.archive.distribution
        )

    if getUtility(ILaunchBag).distribution:
        return BugNominatableDistroSeriesVocabulary(
            context, getUtility(ILaunchBag).distribution
        )
    else:
        assert getUtility(ILaunchBag).product
        return BugNominatableProductSeriesVocabulary(
            context, getUtility(ILaunchBag).product
        )


class BugNominatableSeriesVocabularyBase(NamedStormVocabulary):
    """Base vocabulary class for series for which a bug can be nominated."""

    def __iter__(self):
        bug = self.context.bug

        for series in self._getNominatableObjects():
            if bug.canBeNominatedFor(series):
                yield self.toTerm(series)

    def __contains__(self, obj):
        # NamedStormVocabulary implements this using a database query, but
        # we need to go through __iter__ so that we filter the available
        # series properly.
        for term in self:
            if term.value == obj:
                return True
        return False

    def toTerm(self, obj):
        return SimpleTerm(obj, obj.name, obj.name.capitalize())

    def getTermByToken(self, token):
        obj = self._queryNominatableObjectByName(token)
        if obj is None:
            raise LookupError(token)

        return self.toTerm(obj)

    def _getNominatableObjects(self):
        """Return the series objects that the bug can be nominated for."""
        raise NotImplementedError

    def _queryNominatableObjectByName(self, name):
        """Return the series object with the given name."""
        raise NotImplementedError


class BugNominatableProductSeriesVocabulary(
    BugNominatableSeriesVocabularyBase
):
    """The product series for which a bug can be nominated."""

    _table = ProductSeries

    def __init__(self, context, product):
        super().__init__(context)
        self.product = product

    def _getNominatableObjects(self):
        """See BugNominatableSeriesVocabularyBase."""
        return shortlist(self.product.series)

    def _queryNominatableObjectByName(self, name):
        """See BugNominatableSeriesVocabularyBase."""
        return self.product.getSeries(name)


class BugNominatableDistroSeriesVocabulary(BugNominatableSeriesVocabularyBase):
    """The distribution series for which a bug can be nominated."""

    _table = DistroSeries

    def __init__(self, context, distribution):
        super().__init__(context)
        self.distribution = distribution

    def _getNominatableObjects(self):
        """Return all non-obsolete distribution series"""
        return [
            series
            for series in shortlist(self.distribution.series)
            if series.status != SeriesStatus.OBSOLETE
        ]

    def _queryNominatableObjectByName(self, name):
        """See BugNominatableSeriesVocabularyBase."""
        return self.distribution.getSeries(name)


def milestone_matches_bugtask(milestone, bugtask):
    """Return True if the milestone can be set against this bugtask."""
    bug_target = bugtask.target
    naked_milestone = removeSecurityProxy(milestone)

    if IProduct.providedBy(bug_target):
        return bugtask.product == naked_milestone.product
    elif IProductSeries.providedBy(bug_target):
        return bugtask.productseries.product == naked_milestone.product
    elif IDistribution.providedBy(
        bug_target
    ) or IDistributionSourcePackage.providedBy(bug_target):
        return bugtask.distribution == naked_milestone.distribution
    elif IDistroSeries.providedBy(bug_target) or ISourcePackage.providedBy(
        bug_target
    ):
        return bugtask.distroseries == naked_milestone.distroseries
    return False


@implementer(IVocabularyTokenized)
class BugTaskMilestoneVocabulary:
    """Milestones for a set of bugtasks.

    This vocabulary supports the optional preloading and caching of milestones
    in order to avoid repeated database queries.
    """

    def __init__(self, default_bugtask=None, milestones=None):
        assert default_bugtask is None or IBugTask.providedBy(default_bugtask)
        self.default_bugtask = default_bugtask
        self._milestones = None
        if milestones is not None:
            self._milestones = {
                str(milestone.id): milestone for milestone in milestones
            }

    def _load_milestones(self, bugtask):
        # If the milestones have not already been cached, load them for the
        # specified bugtask.
        if self._milestones is None:
            bugtask_set = getUtility(IBugTaskSet)
            milestones = list(
                bugtask_set.getBugTaskTargetMilestones([bugtask])
            )
            self._milestones = {
                str(milestone.id): milestone for milestone in milestones
            }
        return self._milestones

    @property
    def milestones(self):
        return self._load_milestones(self.default_bugtask)

    def visible_milestones(self, bugtask=None):
        return self._get_milestones(bugtask)

    def _get_milestones(self, bugtask=None):
        """All milestones for the specified bugtask."""
        bugtask = bugtask or self.default_bugtask
        if bugtask is None:
            return []

        self._load_milestones(bugtask)
        milestones = [
            milestone
            for milestone in self._milestones.values()
            if milestone_matches_bugtask(milestone, bugtask)
        ]

        if (
            bugtask.milestone is not None
            and bugtask.milestone not in milestones
        ):
            # Even if we deactivate a milestone, a bugtask might still be
            # linked to it. Include such milestones in the vocabulary to
            # ensure that the +editstatus page doesn't break.
            milestones.append(bugtask.milestone)

        def naked_milestone_sort_key(milestone):
            return milestone_sort_key(removeSecurityProxy(milestone))

        return sorted(milestones, key=naked_milestone_sort_key, reverse=True)

    def getTerm(self, value):
        """See `IVocabulary`."""
        if value not in self:
            raise LookupError(value)
        return self.toTerm(value)

    def getTermByToken(self, token):
        """See `IVocabularyTokenized`."""
        try:
            return self.toTerm(self.milestones[str(token)])
        except Exception:
            raise LookupError(token)

    def __len__(self):
        """See `IVocabulary`."""
        return len(self._get_milestones())

    def __iter__(self):
        """See `IVocabulary`."""
        return iter(
            [self.toTerm(milestone) for milestone in self._get_milestones()]
        )

    def toTerm(self, obj):
        """See `IVocabulary`."""
        return SimpleTerm(obj, obj.id, obj.displayname)

    def __contains__(self, obj):
        """See `IVocabulary`."""
        return obj in self._get_milestones()
