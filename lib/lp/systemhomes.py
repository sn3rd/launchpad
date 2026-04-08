# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Content classes for the 'home pages' of the subsystems of Launchpad."""

__all__ = [
    "BazaarApplication",
    "CodeImportSchedulerApplication",
    "FeedsApplication",
    "MailingListApplication",
    "MaloneApplication",
    "PrivateMaloneApplication",
    "RosettaApplication",
    "TestOpenIDApplication",
]

import codecs
import os

from lazr.restful import ServiceRootResource
from storm.expr import Max
from zope.component import getUtility
from zope.interface import implementer

from lp.app.enums import PRIVATE_INFORMATION_TYPES
from lp.bugs.adapters.bug import convert_to_information_type
from lp.bugs.interfaces.bug import CreateBugParams, IBugSet
from lp.bugs.interfaces.bugtask import IBugTaskSet
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.bugs.interfaces.bugtracker import IBugTrackerSet
from lp.bugs.interfaces.bugwatch import IBugWatchSet
from lp.bugs.interfaces.malone import (
    IMaloneApplication,
    IPrivateMaloneApplication,
)
from lp.bugs.model.bug import Bug
from lp.bugs.model.bugtarget import HasBugsBase
from lp.code.interfaces.codehosting import (
    IBazaarApplication,
    ICodehostingApplication,
)
from lp.code.interfaces.codeimportscheduler import (
    ICodeImportSchedulerApplication,
)
from lp.code.interfaces.gitapi import IGitApplication
from lp.registry.interfaces.distroseries import IDistroSeriesSet
from lp.registry.interfaces.mailinglist import IMailingListApplication
from lp.registry.interfaces.product import IProductSet
from lp.services.config import config
from lp.services.database.interfaces import IStore
from lp.services.feeds.interfaces.application import IFeedsApplication
from lp.services.statistics.interfaces.statistic import ILaunchpadStatisticSet
from lp.services.webapp.interfaces import ICanonicalUrlData, ILaunchBag
from lp.services.webapp.publisher import canonical_url
from lp.services.webservice.interfaces import IWebServiceApplication
from lp.services.worlddata.interfaces.language import ILanguageSet
from lp.soyuz.interfaces.archiveapi import IArchiveApplication
from lp.testopenid.interfaces.server import ITestOpenIDApplication
from lp.translations.interfaces.translationgroup import ITranslationGroupSet
from lp.translations.interfaces.translations import IRosettaApplication
from lp.translations.interfaces.translationsoverview import (
    ITranslationsOverview,
)


@implementer(IArchiveApplication)
class ArchiveApplication:
    title = "Archive API"


@implementer(ICodehostingApplication)
class CodehostingApplication:
    """Codehosting End-Point."""

    title = "Codehosting API"


@implementer(ICodeImportSchedulerApplication)
class CodeImportSchedulerApplication:
    """CodeImportScheduler End-Point."""

    title = "Code Import Scheduler"


@implementer(IGitApplication)
class GitApplication:
    title = "Git API"


@implementer(IPrivateMaloneApplication)
class PrivateMaloneApplication:
    """ExternalBugTracker authentication token end-point."""

    title = "Launchpad Bugs."


@implementer(IMailingListApplication)
class MailingListApplication:
    pass


@implementer(IFeedsApplication)
class FeedsApplication:
    pass


@implementer(IMaloneApplication)
class MaloneApplication(HasBugsBase):
    def __init__(self):
        self.title = "Malone: the Launchpad bug tracker"

    def _customizeSearchParams(self, search_params):
        """See `HasBugsBase`."""
        pass

    def getBugSummaryContextWhereClause(self):
        """See `HasBugsBase`."""
        return True

    def getBugData(self, user, bug_id, related_bug=None):
        """See `IMaloneApplication`."""
        search_params = BugTaskSearchParams(user, bug=bug_id)
        bugtasks = getUtility(IBugTaskSet).search(search_params)
        if not bugtasks:
            return []
        bugs = [task.bug for task in bugtasks]
        data = []
        for bug in bugs:
            bugtask = bug.default_bugtask

            different_parents = (
                related_bug
                and (
                    set(bug.affected_bug_target_parents)
                    != set(related_bug.affected_bug_target_parents)
                )
                or False
            )
            data.append(
                {
                    "id": bug_id,
                    "information_type": bug.information_type.title,
                    "is_private": bug.information_type
                    in PRIVATE_INFORMATION_TYPES,
                    "importance": bugtask.importance.title,
                    "importance_class": "importance" + bugtask.importance.name,
                    "status": bugtask.status.title,
                    "status_class": "status" + bugtask.status.name,
                    "bug_summary": bug.title,
                    "description": bug.description,
                    "bug_url": canonical_url(bugtask),
                    "different_bug_target_parents": different_parents,
                    # Pillars in bugs are deprecated. We are leaving this here
                    # for API compatibility even though it returns the same.
                    "different_pillars": different_parents,
                }
            )
        return data

    def createBug(
        self,
        owner,
        title,
        description,
        target,
        information_type=None,
        tags=None,
        security_related=None,
        private=None,
    ):
        """See `IMaloneApplication`."""
        if information_type is None and (
            security_related is not None or private is not None
        ):
            # Adapt the deprecated args to information_type.
            information_type = convert_to_information_type(
                private, security_related
            )
        params = CreateBugParams(
            title=title,
            comment=description,
            owner=owner,
            information_type=information_type,
            tags=tags,
            target=target,
        )
        return getUtility(IBugSet).createBug(params)

    @property
    def bug_count(self):
        return IStore(Bug).find(Max(Bug.id)).one()

    @property
    def bugwatch_count(self):
        return getUtility(IBugWatchSet).search().count()

    @property
    def bugtask_count(self):
        user = getUtility(ILaunchBag).user
        search_params = BugTaskSearchParams(user=user)
        return getUtility(IBugTaskSet).search(search_params).count()

    @property
    def bugtracker_count(self):
        return getUtility(IBugTrackerSet).count

    @property
    def projects_with_bugs_count(self):
        return getUtility(ILaunchpadStatisticSet).value("projects_with_bugs")

    @property
    def shared_bug_count(self):
        return getUtility(ILaunchpadStatisticSet).value("shared_bug_count")

    @property
    def top_bugtrackers(self):
        return getUtility(IBugTrackerSet).getMostActiveBugTrackers(limit=5)

    def empty_list(self):
        """See `IMaloneApplication`."""
        return []


@implementer(IBazaarApplication)
class BazaarApplication:
    def __init__(self):
        self.title = "The Open Source Bazaar"


@implementer(IRosettaApplication)
class RosettaApplication:
    def __init__(self):
        self.title = "Rosetta: Translations in the Launchpad"
        self.name = "Rosetta"

    @property
    def languages(self):
        """See `IRosettaApplication`."""
        return getUtility(ILanguageSet)

    @property
    def language_count(self):
        """See `IRosettaApplication`."""
        stats = getUtility(ILaunchpadStatisticSet)
        return stats.value("language_count")

    @property
    def statsdate(self):
        stats = getUtility(ILaunchpadStatisticSet)
        return stats.dateupdated("potemplate_count")

    @property
    def translation_groups(self):
        """See `IRosettaApplication`."""
        return getUtility(ITranslationGroupSet)

    def translatable_products(self):
        """See `IRosettaApplication`."""
        products = getUtility(IProductSet)
        return products.getTranslatables()

    def featured_products(self):
        """See `IRosettaApplication`."""
        projects = getUtility(ITranslationsOverview)
        for project in projects.getMostTranslatedPillars():
            yield {
                "pillar": project["pillar"],
                "font_size": project["weight"] * 10,
            }

    def translatable_distroseriess(self):
        """See `IRosettaApplication`."""
        distroseriess = getUtility(IDistroSeriesSet)
        return distroseriess.translatables()

    def potemplate_count(self):
        """See `IRosettaApplication`."""
        stats = getUtility(ILaunchpadStatisticSet)
        return stats.value("potemplate_count")

    def pofile_count(self):
        """See `IRosettaApplication`."""
        stats = getUtility(ILaunchpadStatisticSet)
        return stats.value("pofile_count")

    def pomsgid_count(self):
        """See `IRosettaApplication`."""
        stats = getUtility(ILaunchpadStatisticSet)
        return stats.value("pomsgid_count")

    def translator_count(self):
        """See `IRosettaApplication`."""
        stats = getUtility(ILaunchpadStatisticSet)
        return stats.value("translator_count")


@implementer(IWebServiceApplication, ICanonicalUrlData)
class WebServiceApplication(ServiceRootResource):
    """See `IWebServiceApplication`.

    This implementation adds a 'cached_wadl' attribute, which starts
    out as an empty dict and is populated as needed.
    """

    inside = None
    path = ""
    rootsite = None

    cached_wadl = {}

    # This should only be used by devel instances: production serves root
    # WADL (and JSON) from the filesystem.

    @classmethod
    def cachedWADLPath(cls, instance_name, version):
        """Helper method to calculate the path to a cached WADL file."""
        return os.path.join(
            config.root,
            "lib",
            "canonical",
            "launchpad",
            "apidoc",
            version,
            "%s.wadl" % (instance_name,),
        )

    def toWADL(self):
        """See `IWebServiceApplication`.

        Look for a cached WADL file for the request version at the
        location used by the script
        utilities/create-launchpad-wadl.py. If the file is present,
        load the file and cache its contents rather than generating
        new WADL. Otherwise, generate new WADL and cache it.
        """
        version = self.request.version
        if self.__class__.cached_wadl is None:
            # The cache has been disabled for testing
            # purposes. Generate the WADL.
            return super().toWADL()
        if version not in self.__class__.cached_wadl:
            # It's not cached. Look for it on disk.
            _wadl_filename = self.cachedWADLPath(config.instance_name, version)
            _wadl_fd = None
            try:
                _wadl_fd = codecs.open(_wadl_filename, encoding="UTF-8")
                try:
                    wadl = _wadl_fd.read()
                finally:
                    _wadl_fd.close()
            except OSError:
                # It's not on disk; generate it.
                wadl = super().toWADL()
            del _wadl_fd
            self.__class__.cached_wadl[version] = wadl
        return self.__class__.cached_wadl[version]


@implementer(ITestOpenIDApplication)
class TestOpenIDApplication:
    title = "TestOpenIDApplication"
