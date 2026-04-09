# Copyright 2015-2020 Canonical Ltd.  This software is licensed under the GNU
# Affero General Public License version 3 (see the file LICENSE).

"""Snappy vocabularies."""

__all__ = [
    "SnapDistroArchSeriesVocabulary",
    "SnappyDistroSeriesVocabulary",
    "SnappySeriesVocabulary",
    "SnapStoreChannel",
    "SnapStoreChannelVocabulary",
    "SyntheticSnappyDistroSeries",
]

from lazr.restful.interfaces import IJSONPublishable
from storm.expr import LeftJoin
from storm.locals import Desc, Not
from zope.interface import implementer
from zope.schema.vocabulary import SimpleTerm, SimpleVocabulary
from zope.security.proxy import removeSecurityProxy

from lp.registry.enums import StoreRisk
from lp.registry.model.distribution import Distribution
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.series import ACTIVE_STATUSES
from lp.services.database.interfaces import IStore
from lp.services.database.stormexpr import IsDistinctFrom
from lp.services.utils import seconds_since_epoch
from lp.services.webapp.vocabulary import StormVocabularyBase
from lp.snappy.interfaces.snap import ISnap
from lp.snappy.interfaces.snappyseries import ISnappyDistroSeries
from lp.snappy.model.snappyseries import (
    SnappyDistroSeries,
    SnappyDistroSeriesMixin,
    SnappySeries,
)
from lp.soyuz.model.distroarchseries import DistroArchSeries


class SnapDistroArchSeriesVocabulary(StormVocabularyBase):
    """All architectures of a Snap's distribution series."""

    _table = DistroArchSeries

    def toTerm(self, das):
        return SimpleTerm(das, das.id, das.architecturetag)

    def __iter__(self):
        for obj in self.context.getAllowedArchitectures():
            yield self.toTerm(obj)

    def __len__(self):
        return len(self.context.getAllowedArchitectures())


class SnappySeriesVocabulary(StormVocabularyBase):
    """A vocabulary for searching snappy series."""

    _table = SnappySeries
    _clauses = [SnappySeries.status.is_in(ACTIVE_STATUSES)]
    _order_by = Desc(SnappySeries.date_created)


@implementer(ISnappyDistroSeries)
class SyntheticSnappyDistroSeries(SnappyDistroSeriesMixin):
    def __init__(self, snappy_series, distro_series):
        self.snappy_series = snappy_series
        self.distro_series = distro_series

    preferred = False

    def __eq__(self, other):
        return (
            ISnappyDistroSeries.providedBy(other)
            and self.snappy_series == other.snappy_series
            and self.distro_series == other.distro_series
        )

    def __ne__(self, other):
        return not (self == other)


def sorting_tuple_date_created(element):
    # we negate the conversion to epoch here of
    # the two date_created in order to achieve
    # descending order
    if element.distro_series is not None:
        if element.snappy_series is not None:
            return (
                1,
                element.distro_series.distribution.display_name,
                (-seconds_since_epoch(element.distro_series.date_created)),
                (-seconds_since_epoch(element.snappy_series.date_created)),
            )
        else:
            return (
                1,
                element.distro_series.distribution.display_name,
                (-seconds_since_epoch(element.distro_series.date_created)),
                0,
            )
    else:
        if element.snappy_series is not None:
            return (
                0,
                (-seconds_since_epoch(element.snappy_series.date_created)),
            )
        else:
            return 0, 0


class SnappyDistroSeriesVocabulary(StormVocabularyBase):
    """A vocabulary for searching snappy/distro series combinations."""

    _table = SnappyDistroSeries
    _origin = [
        SnappyDistroSeries,
        LeftJoin(
            DistroSeries,
            SnappyDistroSeries.distro_series_id == DistroSeries.id,
        ),
        LeftJoin(Distribution, DistroSeries.distribution == Distribution.id),
        SnappySeries,
    ]
    _clauses = [
        SnappyDistroSeries.snappy_series_id == SnappySeries.id,
        SnappySeries.status.is_in(ACTIVE_STATUSES),
    ]

    @property
    def _entries(self):
        entries = list(
            IStore(self._table)
            .using(*self._origin)
            .find(self._table, *self._clauses)
        )

        if ISnap.providedBy(self.context) and not any(
            entry.snappy_series == self.context.store_series
            and entry.distro_series == self.context.distro_series
            for entry in entries
        ):
            entries.append(
                SyntheticSnappyDistroSeries(
                    self.context.store_series, self.context.distro_series
                )
            )
        return sorted(entries, key=sorting_tuple_date_created)

    def toTerm(self, obj):
        """See `IVocabulary`."""
        if obj.snappy_series is not None:
            if obj.distro_series is None:
                token = obj.snappy_series.name
            else:
                token = "%s/%s/%s" % (
                    obj.distro_series.distribution.name,
                    obj.distro_series.name,
                    obj.snappy_series.name,
                )
        else:
            if obj.distro_series is None:
                token = "(unset)"
            else:
                token = "%s/%s" % (
                    obj.distro_series.distribution.name,
                    obj.distro_series.name,
                )
        return SimpleTerm(obj, token, obj.title)

    def __contains__(self, value):
        """See `IVocabulary`."""
        return value in self._entries

    def getTerm(self, value):
        """See `IVocabulary`."""
        if value not in self:
            raise LookupError(value)
        return self.toTerm(value)

    def getTermByToken(self, token):
        """See `IVocabularyTokenized`."""
        if token == "(unset)":
            return self.toTerm(SyntheticSnappyDistroSeries(None, None))
        if "/" in token:
            bits = token.split("/", 2)
            if len(bits) == 2:
                distribution_name, distro_series_name = bits
                snappy_series_name = None
            elif len(bits) == 3:
                (
                    distribution_name,
                    distro_series_name,
                    snappy_series_name,
                ) = bits
            else:
                raise LookupError(token)
        else:
            distribution_name = None
            distro_series_name = None
            snappy_series_name = token
        entry = (
            IStore(self._table)
            .using(*self._origin)
            .find(
                self._table,
                Not(IsDistinctFrom(Distribution.name, distribution_name)),
                Not(IsDistinctFrom(DistroSeries.name, distro_series_name)),
                SnappySeries.name == snappy_series_name,
                *self._clauses,
            )
            .one()
        )

        if entry is None and ISnap.providedBy(self.context):
            if self.context.store_series is None:
                context_store_series_name = None
            else:
                context_store_series_name = self.context.store_series.name
            if self.context.distro_series is not None:
                context_distribution_name = (
                    self.context.distro_series.distribution.name
                )
                context_distro_series_name = self.context.distro_series.name
            else:
                context_distribution_name = None
                context_distro_series_name = None
            if (
                context_distribution_name == distribution_name
                and context_distro_series_name == distro_series_name
                and context_store_series_name == snappy_series_name
            ):
                entry = SyntheticSnappyDistroSeries(
                    self.context.store_series, self.context.distro_series
                )
        if entry is None:
            raise LookupError(token)

        return self.toTerm(entry)


@implementer(IJSONPublishable)
class SnapStoreChannel(SimpleTerm):
    """A store channel."""

    def toDataForJSON(self, media_type):
        """See `IJSONPublishable`."""
        return self.token


class SnapStoreChannelVocabulary(SimpleVocabulary):
    """A vocabulary for searching store channels."""

    def __init__(self, context=None):
        terms = [
            self.createTerm(item.title, item.title, item.description)
            for item in StoreRisk.items
        ]
        if ISnap.providedBy(context):
            # Supplement the vocabulary with any obsolete channels still
            # used by this context.
            context_channels = removeSecurityProxy(context)._store_channels
            if context_channels is not None:
                known_names = {item.title for item in StoreRisk.items}
                for name in context_channels:
                    if name not in known_names:
                        terms.append(self.createTerm(name, name, name))
        super().__init__(terms)

    @classmethod
    def createTerm(cls, *args):
        """See `SimpleVocabulary`."""
        return SnapStoreChannel(*args)
