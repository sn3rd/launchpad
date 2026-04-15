# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Database classes related to bug nomination.

A bug nomination is a suggestion from a user that a bug be fixed in a
particular distro series or product series. A bug may have zero, one,
or more nominations.
"""

__all__ = ["BugNomination", "BugNominationSet"]

from datetime import datetime, timezone

from storm.properties import DateTime, Int
from storm.references import Reference
from zope.component import getUtility
from zope.interface import implementer

from lp.app.errors import NotFoundError
from lp.bugs.adapters.bugchange import BugTaskAdded
from lp.bugs.interfaces.bugnomination import (
    BugNominationStatus,
    BugNominationStatusError,
    IBugNomination,
    IBugNominationSet,
)
from lp.bugs.interfaces.bugtask import IBugTaskSet
from lp.registry.interfaces.archiveseries import IArchiveSeries
from lp.registry.interfaces.archivesourcepackageseries import (
    IArchiveSourcePackageSeries,
)
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.externalpackageseries import IExternalPackageSeries
from lp.registry.interfaces.person import validate_public_person
from lp.registry.interfaces.productseries import IProductSeries
from lp.registry.interfaces.sourcepackage import ISourcePackage
from lp.services.database.constants import UTC_NOW
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IStore
from lp.services.database.stormbase import StormBase
from lp.services.features import getFeatureFlag


@implementer(IBugNomination)
class BugNomination(StormBase):
    __storm_table__ = "BugNomination"

    id = Int(primary=True)

    owner_id = Int(
        name="owner", allow_none=False, validator=validate_public_person
    )
    owner = Reference(owner_id, "Person.id")

    decider_id = Int(
        name="decider",
        allow_none=True,
        default=None,
        validator=validate_public_person,
    )
    decider = Reference(decider_id, "Person.id")

    date_created = DateTime(
        allow_none=False, default=UTC_NOW, tzinfo=timezone.utc
    )
    date_decided = DateTime(allow_none=True, default=None, tzinfo=timezone.utc)

    distroseries_id = Int(name="distroseries", allow_none=True, default=None)
    distroseries = Reference(distroseries_id, "DistroSeries.id")

    productseries_id = Int(name="productseries", allow_none=True, default=None)
    productseries = Reference(productseries_id, "ProductSeries.id")

    bug_id = Int(name="bug", allow_none=False)
    bug = Reference(bug_id, "Bug.id")

    status = DBEnum(
        name="status",
        allow_none=False,
        enum=BugNominationStatus,
        default=BugNominationStatus.PROPOSED,
    )

    def __init__(
        self,
        bug,
        owner,
        decider=None,
        date_created=UTC_NOW,
        date_decided=None,
        distroseries=None,
        productseries=None,
        status=BugNominationStatus.PROPOSED,
    ):
        self.owner = owner
        self.decider = decider
        self.date_created = date_created
        self.date_decided = date_decided
        self.distroseries = distroseries
        self.productseries = productseries
        self.bug = bug
        self.status = status

    @property
    def target(self):
        """See IBugNomination."""
        return self.distroseries or self.productseries

    def approve(self, approver):
        """See IBugNomination."""
        if self.isApproved():
            # Approving an approved nomination is a no-op.
            return
        self.status = BugNominationStatus.APPROVED
        self.decider = approver
        self.date_decided = datetime.now(timezone.utc)
        targets = []
        if self.distroseries:
            # Figure out which packages are affected in this distro for
            # this bug.
            distribution = self.distroseries.distribution
            distroseries = self.distroseries
            for task in self.bug.bugtasks:

                if task.distribution == distribution:
                    if task.packagetype is not None:
                        targets.append(
                            distroseries.getExternalPackageSeries(
                                task.sourcepackagename,
                                task.packagetype,
                                task.channel,
                            )
                        )
                    elif task.sourcepackagename is not None:
                        targets.append(
                            distroseries.getSourcePackage(
                                task.sourcepackagename
                            )
                        )
                    else:
                        targets.append(distroseries)
                elif (
                    task.archive
                    and task.archive.distribution == distribution
                    and not task.distroseries
                ):
                    if task.sourcepackagename is not None:

                        targets.append(
                            distroseries.getArchiveSourcePackageSeries(
                                task.archive, task.sourcepackagename
                            )
                        )
                    else:
                        targets.append(
                            distroseries.getArchiveSeries(task.archive)
                        )
        else:
            targets.append(self.productseries)
        bugtasks = getUtility(IBugTaskSet).createManyTasks(
            self.bug, approver, targets
        )
        for bug_task in bugtasks:
            self.bug.addChange(BugTaskAdded(UTC_NOW, approver, bug_task))

    def decline(self, decliner):
        """See IBugNomination."""
        if self.isApproved():
            raise BugNominationStatusError(
                "Cannot decline an approved nomination."
            )
        self.status = BugNominationStatus.DECLINED
        self.decider = decliner
        self.date_decided = datetime.now(timezone.utc)

    def isProposed(self):
        """See IBugNomination."""
        return self.status == BugNominationStatus.PROPOSED

    def isDeclined(self):
        """See IBugNomination."""
        return self.status == BugNominationStatus.DECLINED

    def isApproved(self):
        """See IBugNomination."""
        return self.status == BugNominationStatus.APPROVED

    def canApprove(self, person):
        """See IBugNomination."""
        # Use the class method to check permissions because there is not
        # yet a bugtask instance with the this target.
        BugTask = self.bug.bugtasks[0].__class__

        if getFeatureFlag(
            "bugs.nominations.bug_supervisors_can_target"
        ) and BugTask.userHasBugSupervisorPrivilegesContext(
            self.target, person
        ):
            return True

        if BugTask.userHasDriverPrivilegesContext(self.target, person):
            return True

        if self.distroseries is not None:
            distribution = self.distroseries.distribution
            # An uploader to any of the packages can approve the
            # nomination. Compile a list of possibilities, and check
            # them all.
            package_names = []
            for bugtask in self.bug.bugtasks:
                if (
                    bugtask.distribution == distribution
                    and bugtask.sourcepackagename is not None
                ):
                    package_names.append(bugtask.sourcepackagename)
            if len(package_names) == 0:
                # If the bug isn't targeted to a source package, allow
                # any component uploader to approve the nomination, like
                # a new package.
                return (
                    distribution.main_archive.verifyUpload(
                        person, None, None, None, strict_component=False
                    )
                    is None
                )
            for name in package_names:
                component = self.distroseries.getSourcePackage(
                    name
                ).latest_published_component
                if (
                    distribution.main_archive.verifyUpload(
                        person, name, component, self.distroseries
                    )
                    is None
                ):
                    return True
        return False

    def destroySelf(self):
        IStore(self).remove(self)

    def __repr__(self):
        return "<BugNomination bug=%s owner=%s>" % (self.bug_id, self.owner_id)


@implementer(IBugNominationSet)
class BugNominationSet:
    """See IBugNominationSet."""

    def get(self, id):
        """See IBugNominationSet."""
        store = IStore(BugNomination)
        nomination = store.get(BugNomination, id)
        if nomination is None:
            raise NotFoundError(id)
        return nomination

    def getByBugTarget(self, bug, target):
        if IDistroSeries.providedBy(target):
            filter_args = dict(distroseries_id=target.id)
        elif IProductSeries.providedBy(target):
            filter_args = dict(productseries_id=target.id)
        elif ISourcePackage.providedBy(target):
            filter_args = dict(distroseries_id=target.series.id)
        elif IExternalPackageSeries.providedBy(target):
            filter_args = dict(distroseries_id=target.series.id)
        elif IArchiveSourcePackageSeries.providedBy(target):
            filter_args = dict(distroseries_id=target.series.id)
        elif IArchiveSeries.providedBy(target):
            filter_args = dict(distroseries_id=target.series.id)
        else:
            return None
        store = IStore(BugNomination)
        return store.find(BugNomination, bug=bug, **filter_args).one()

    def findByBug(self, bug):
        """See IBugNominationSet."""
        store = IStore(BugNomination)
        return store.find(BugNomination, bug=bug)
