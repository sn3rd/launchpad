# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "DistributionSourcePackageAnswersMenu",
    "DistributionSourcePackageBreadcrumb",
    "DistributionSourcePackageChangelogView",
    "DistributionSourcePackageEditView",
    "DistributionSourcePackageFacets",
    "DistributionSourcePackageHelpView",
    "DistributionSourcePackageNavigation",
    "DistributionSourcePackageOverviewMenu",
    "DistributionSourcePackagePublishingHistoryView",
    "DistributionSourcePackageURL",
    "DistributionSourcePackageView",
    "PublishingHistoryViewMixin",
]

import itertools
import operator
from functools import cmp_to_key

import apt_pkg
from lazr.delegates import delegate_to
from zope.component import getUtility
from zope.interface import Interface, implementer

from lp.answers.browser.questiontarget import (
    QuestionTargetAnswersMenu,
    QuestionTargetTraversalMixin,
)
from lp.answers.enums import QuestionStatus
from lp.app.browser.launchpadform import LaunchpadEditFormView, action
from lp.app.browser.stringformatter import extract_email_addresses
from lp.app.browser.tales import CustomizableFormatter
from lp.app.enums import ServiceUsage
from lp.app.interfaces.headings import IHeadingBreadcrumb
from lp.app.interfaces.launchpad import IServiceUsage
from lp.bugs.browser.bugtask import BugTargetTraversalMixin
from lp.bugs.browser.structuralsubscription import (
    StructuralSubscriptionMenuMixin,
    StructuralSubscriptionTargetTraversalMixin,
    expose_structural_subscription_data_to_js,
)
from lp.bugs.interfaces.bugtarget import DISABLE_BUG_WEBHOOKS_FEATURE_FLAG
from lp.bugs.interfaces.bugtask import BugTaskStatus
from lp.bugs.interfaces.bugtasksearch import BugTaskSearchParams
from lp.code.browser.vcslisting import TargetDefaultVCSNavigationMixin
from lp.registry.browser import add_subscribe_link
from lp.registry.browser.pillar import BugTargetParentBugsMenu
from lp.registry.enums import DistributionDefaultTraversalPolicy
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.series import SeriesStatus
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.features import getFeatureFlag
from lp.services.helpers import shortlist
from lp.services.propertycache import cachedproperty
from lp.services.webapp import (
    Navigation,
    StandardLaunchpadFacets,
    canonical_url,
    redirection,
)
from lp.services.webapp.batching import BatchNavigator
from lp.services.webapp.breadcrumb import Breadcrumb
from lp.services.webapp.interfaces import (
    ICanonicalUrlData,
    IMultiFacetedBreadcrumb,
)
from lp.services.webapp.menu import (
    ApplicationMenu,
    Link,
    NavigationMenu,
    enabled_with_permission,
)
from lp.services.webapp.publisher import LaunchpadView
from lp.services.webapp.sorting import sorted_dotted_numbers
from lp.services.webhooks.browser import WebhookTargetNavigationMixin
from lp.soyuz.browser.sourcepackagerelease import linkify_changelog
from lp.soyuz.interfaces.archive import IArchiveSet
from lp.soyuz.interfaces.distributionsourcepackagerelease import (
    IDistributionSourcePackageRelease,
)
from lp.soyuz.interfaces.packagediff import IPackageDiffSet
from lp.translations.browser.customlanguagecode import (
    HasCustomLanguageCodesTraversalMixin,
)


@implementer(ICanonicalUrlData)
class DistributionSourcePackageURL:
    """Distribution source package URL creation rules.

    The canonical URL for a distribution source package depends on the
    values of `default_traversal_policy` and `redirect_default_traversal` on
    the context distribution.
    """

    rootsite = None

    def __init__(self, context):
        self.context = context

    @property
    def inside(self):
        return self.context.distribution

    @property
    def path(self):
        policy = self.context.distribution.default_traversal_policy
        if (
            policy == DistributionDefaultTraversalPolicy.SOURCE_PACKAGE
            and not self.context.distribution.redirect_default_traversal
        ):
            return self.context.name
        else:
            return "+source/%s" % self.context.name


class DistributionSourcePackageFormatterAPI(CustomizableFormatter):
    """Adapt IDistributionSourcePackage objects to a formatted string."""

    _link_permission = "zope.Public"
    _link_summary_template = "%(displayname)s"

    def _link_summary_values(self):
        displayname = self._context.displayname
        return {"displayname": displayname}


@implementer(IHeadingBreadcrumb, IMultiFacetedBreadcrumb)
class DistributionSourcePackageBreadcrumb(Breadcrumb):
    """Builds a breadcrumb for an `IDistributionSourcePackage`."""

    @property
    def text(self):
        return "%s package" % self.context.sourcepackagename.name


class DistributionSourcePackageFacets(StandardLaunchpadFacets):
    usedfor = IDistributionSourcePackage
    enable_only = [
        "overview",
        "branches",
        "bugs",
        "translations",
        "answers",
    ]


class DistributionSourcePackageLinksMixin:
    def publishinghistory(self):
        return Link("+publishinghistory", "Show publishing history")

    @enabled_with_permission("launchpad.BugSupervisor")
    def edit(self):
        """Edit the details of this source package."""
        # This is titled "Edit bug reporting guidelines" because that
        # is the only editable property of a source package right now.
        return Link("+edit", "Configure bug tracker", icon="edit")

    def new_bugs(self):
        base_path = "+bugs"
        get_data = "?field.status:list=NEW"
        return Link(base_path + get_data, "New bugs", site="bugs")

    def open_questions(self):
        base_path = "+questions"
        get_data = "?field.status=OPEN"
        return Link(base_path + get_data, "Open Questions", site="answers")

    @enabled_with_permission("launchpad.Edit")
    def webhooks(self):
        return Link(
            "+webhooks",
            "Manage webhooks",
            icon="edit",
            enabled=not getFeatureFlag(DISABLE_BUG_WEBHOOKS_FEATURE_FLAG),
        )


class DistributionSourcePackageOverviewMenu(
    ApplicationMenu, DistributionSourcePackageLinksMixin
):
    usedfor = IDistributionSourcePackage
    facet = "overview"
    links = ["new_bugs", "open_questions"]


class DistributionSourcePackageBugsMenu(
    BugTargetParentBugsMenu,
    StructuralSubscriptionMenuMixin,
    DistributionSourcePackageLinksMixin,
):
    usedfor = IDistributionSourcePackage
    facet = "bugs"

    @cachedproperty
    def links(self):
        links = ["filebug"]
        add_subscribe_link(links)
        links.append("webhooks")
        return links


class DistributionSourcePackageAnswersMenu(QuestionTargetAnswersMenu):
    usedfor = IDistributionSourcePackage
    facet = "answers"

    links = QuestionTargetAnswersMenu.links + ["gethelp"]

    def gethelp(self):
        return Link("+gethelp", "Help and support options", icon="info")


class DistributionSourcePackageNavigation(
    Navigation,
    BugTargetTraversalMixin,
    HasCustomLanguageCodesTraversalMixin,
    QuestionTargetTraversalMixin,
    TargetDefaultVCSNavigationMixin,
    StructuralSubscriptionTargetTraversalMixin,
    WebhookTargetNavigationMixin,
):
    usedfor = IDistributionSourcePackage

    @redirection("+editbugcontact")
    def redirect_editbugcontact(self):
        return "+subscribe"

    def traverse(self, name):
        return self.context.getVersion(name)


@delegate_to(IDistributionSourcePackageRelease, context="context")
class DecoratedDistributionSourcePackageRelease:
    """A decorated DistributionSourcePackageRelease.

    The publishing history and package diffs for the release are
    pre-cached.
    """

    def __init__(
        self,
        distributionsourcepackagerelease,
        publishing_history,
        package_diffs,
        person_data,
        user,
    ):
        self.context = distributionsourcepackagerelease
        self._publishing_history = publishing_history
        self._package_diffs = package_diffs
        self._person_data = person_data
        self._user = user

    @property
    def publishing_history(self):
        """See `IDistributionSourcePackageRelease`."""
        return self._publishing_history

    @property
    def package_diffs(self):
        """See `ISourcePackageRelease`."""
        return self._package_diffs

    @property
    def change_summary(self):
        """See `ISourcePackageRelease`."""
        return linkify_changelog(
            self._user, self.context.change_summary, self._person_data
        )


class IDistributionSourcePackageActionMenu(Interface):
    """Marker interface for the action menu."""


class DistributionSourcePackageActionMenu(
    NavigationMenu,
    StructuralSubscriptionMenuMixin,
    DistributionSourcePackageLinksMixin,
):
    """Action menu for distro source packages."""

    usedfor = IDistributionSourcePackageActionMenu
    facet = "overview"
    title = "Actions"

    @cachedproperty
    def links(self):
        links = ["publishing_history", "change_log"]
        add_subscribe_link(links)
        links.extend(["edit", "webhooks"])
        return links

    def publishing_history(self):
        text = "View full publishing history"
        return Link("+publishinghistory", text, icon="info")

    def change_log(self):
        text = "View full change log"
        return Link("+changelog", text, icon="info")


class DistributionSourcePackageBaseView(LaunchpadView):
    """Common features to all `DistributionSourcePackage` views."""

    @property
    def releases(self):
        """All releases for this `IDistributionSourcePackage`."""

        def not_empty(text):
            return (
                text is not None
                and isinstance(text, str)
                and len(text.strip()) > 0
            )

        def decorate(dspr_pubs):
            sprs = [dspr.sourcepackagerelease for (dspr, spphs) in dspr_pubs]
            # Preload email/person data only if user is logged on. In
            # the opposite case the emails in the changelog will be
            # obfuscated anyway and thus cause no database lookups.
            the_changelog = "\n".join(
                [
                    spr.changelog_entry
                    for spr in sprs
                    if not_empty(spr.changelog_entry)
                ]
            )
            if self.user:
                self._person_data = {
                    email.email: person
                    for (email, person) in getUtility(IPersonSet).getByEmails(
                        extract_email_addresses(the_changelog),
                        include_hidden=False,
                    )
                }
            else:
                self._person_data = None
            # Collate diffs for relevant SourcePackageReleases
            pkg_diffs = getUtility(IPackageDiffSet).getDiffsToReleases(
                sprs, preload_for_display=True
            )
            spr_diffs = {}
            for spr, diffs in itertools.groupby(
                pkg_diffs, operator.attrgetter("to_source")
            ):
                spr_diffs[spr] = list(diffs)

            return [
                DecoratedDistributionSourcePackageRelease(
                    dspr,
                    spphs,
                    spr_diffs.get(dspr.sourcepackagerelease, []),
                    self._person_data,
                    self.user,
                )
                for (dspr, spphs) in dspr_pubs
            ]

        return DecoratedResultSet(
            self.context.getReleasesAndPublishingHistory(),
            bulk_decorator=decorate,
        )


@implementer(IDistributionSourcePackageActionMenu)
class DistributionSourcePackageView(
    DistributionSourcePackageBaseView, LaunchpadView
):
    """View class for DistributionSourcePackage."""

    def initialize(self):
        super().initialize()
        expose_structural_subscription_data_to_js(
            self.context, self.request, self.user
        )

    @property
    def label(self):
        return self.context.title

    page_title = label

    @property
    def next_url(self):
        """See `LaunchpadFormView`."""
        return canonical_url(self.context)

    @property
    def related_ppa_versions(self):
        """Return a list of the latest 3 ppas with related publishings.

        The list contains dictionaries each with a key of 'archive' and
        'publications'.
        """
        # Grab the related archive publications and limit the result to
        # the first 3.
        # XXX Michael Nelson 2009-06-24 bug=387020
        # Currently we need to find distinct archives here manually because,
        # without a concept of IArchive.rank or similar, the best ordering
        # that orderDistributionSourcePackage.findRelatedArchives() method
        # can provide is on a join (SourcePackageRelease.dateuploaded), but
        # this prohibits a distinct clause.
        # To ensure that we find distinct archives here with only one query,
        # we grab the first 20 results and iterate through to find three
        # distinct archives (20 is a magic number being greater than
        # 3 * number of distroseries).
        related_archives = self.context.findRelatedArchives()
        related_archives.config(limit=20)
        top_three_archives = []
        for archive in related_archives:
            if archive in top_three_archives:
                continue
            else:
                top_three_archives.append(archive)

            if len(top_three_archives) == 3:
                break

        # Now we'll find the relevant publications for the top
        # three archives.
        archive_set = getUtility(IArchiveSet)
        publications = archive_set.getPublicationsInArchives(
            self.context.sourcepackagename,
            top_three_archives,
            distribution=self.context.distribution,
        )

        # Collect the publishings for each archive
        archive_publishings = {}
        for pub in publications:
            archive_publishings.setdefault(pub.archive, []).append(pub)

        # Then construct a list of dicts with the results for easy use in
        # the template, preserving the order of the archives:
        archive_versions = []
        for archive in top_three_archives:
            versions = []

            # For each publication, append something like:
            # 'Jaunty (1.0.1b)' to the versions list.
            for pub in archive_publishings[archive]:
                versions.append(
                    "%s (%s)"
                    % (
                        pub.distroseries.displayname,
                        pub.source_package_version,
                    )
                )
            archive_versions.append(
                {
                    "archive": archive,
                    "versions": ", ".join(versions),
                }
            )

        return archive_versions

    @property
    def further_ppa_versions_url(self):
        """Return the url used to find further PPA versions of this package."""
        return "%s/+ppas?name_filter=%s" % (
            canonical_url(self.context.distribution),
            self.context.name,
        )

    @cachedproperty
    def active_distroseries_packages(self):
        """Cached proxy call to context/get_distroseries_packages."""
        return self.context.get_distroseries_packages()

    @property
    def packages_by_active_distroseries(self):
        """Dict of packages keyed by distroseries."""
        packages_dict = {}
        for package in self.active_distroseries_packages:
            packages_dict[package.distroseries] = package
        return packages_dict

    @cachedproperty
    def active_series(self):
        """Return active distroseries where this package is published.

        Used in the template code that shows the table of versions.
        The returned series are sorted in reverse order of creation.
        """
        series = set()
        for package in self.active_distroseries_packages:
            series.add(package.distroseries)
        result = sorted_dotted_numbers(
            series, key=operator.attrgetter("version")
        )
        result.reverse()
        return result

    def published_by_version(self, sourcepackage):
        """Return a dict of publications keyed by version.

        :param sourcepackage: ISourcePackage
        """
        publications = getUtility(IArchiveSet).getPublicationsInArchives(
            sourcepackage.sourcepackagename,
            sourcepackage.distribution.all_distro_archives,
            distroseries=sourcepackage.distroseries,
        )
        pocket_dict = {}
        for pub in shortlist(publications):
            version = pub.source_package_version
            pocket_dict.setdefault(version, []).append(pub)
        return pocket_dict

    @property
    def latest_sourcepackage(self):
        if len(self.active_series) == 0:
            return None
        return self.active_series[0].getSourcePackage(
            self.context.sourcepackagename
        )

    @property
    def version_table(self):
        """Rows of data for the template to render in the packaging table."""
        rows = []
        packages_by_series = self.packages_by_active_distroseries
        for distroseries in self.active_series:
            # The first row for each series is the "title" row.
            packaging = packages_by_series[distroseries].direct_packaging
            package = packages_by_series[distroseries]
            # Don't show the "Set upstream link" action for older series
            # without packaging info, so the user won't feel required to
            # fill it in.
            show_set_upstream_link = (
                packaging is None
                and distroseries.status
                in (
                    SeriesStatus.CURRENT,
                    SeriesStatus.DEVELOPMENT,
                )
            )
            title_row = {
                "blank_row": False,
                "title_row": True,
                "data_row": False,
                "distroseries": distroseries,
                "series_package": package,
                "packaging": packaging,
                "show_set_upstream_link": show_set_upstream_link,
            }
            rows.append(title_row)

            # After the title row, we list each package version that's
            # currently published, and which pockets it's published in.
            pocket_dict = self.published_by_version(package)
            for version in sorted(
                pocket_dict,
                key=cmp_to_key(apt_pkg.version_compare),
                reverse=True,
            ):
                most_recent_publication = pocket_dict[version][0]
                date_published = most_recent_publication.datepublished
                pockets = ", ".join(
                    [pub.pocket.name for pub in pocket_dict[version]]
                )
                row = {
                    "blank_row": False,
                    "title_row": False,
                    "data_row": True,
                    "version": version,
                    "publication": most_recent_publication,
                    "pockets": pockets,
                    "component": most_recent_publication.component_name,
                    "date_published": date_published,
                }
                rows.append(row)
            # We need a blank row after each section, so the series
            # header row doesn't appear too close to the previous
            # section.
            rows.append(
                {
                    "blank_row": True,
                    "title_row": False,
                    "data_row": False,
                }
            )

        return rows

    @cachedproperty
    def open_questions(self):
        """Return result set containing open questions for this package."""
        return self.context.searchQuestions(status=QuestionStatus.OPEN)

    @cachedproperty
    def bugs_answers_usage(self):
        """Return a dict of uses_bugs, uses_answers, uses_both, uses_either."""
        service_usage = IServiceUsage(self.context)
        uses_bugs = service_usage.bug_tracking_usage == ServiceUsage.LAUNCHPAD
        uses_answers = service_usage.answers_usage == ServiceUsage.LAUNCHPAD
        uses_both = uses_bugs and uses_answers
        uses_either = uses_bugs or uses_answers
        return dict(
            uses_bugs=uses_bugs,
            uses_answers=uses_answers,
            uses_both=uses_both,
            uses_either=uses_either,
        )

    @cachedproperty
    def new_bugtasks_count(self):
        search_params = BugTaskSearchParams(
            self.user, status=BugTaskStatus.NEW, omit_dupes=True
        )
        return self.context.searchTasks(search_params).count()


class DistributionSourcePackageChangelogView(
    DistributionSourcePackageBaseView, LaunchpadView
):
    """View for presenting change logs for a `DistributionSourcePackage`."""

    page_title = "Change log"

    @property
    def label(self):
        return "Change log for %s" % self.context.title

    @cachedproperty
    def batchnav(self):
        return BatchNavigator(self.releases, self.request)


class PublishingHistoryViewMixin:
    """Mixin for presenting batches of `SourcePackagePublishingHistory`s."""

    def _preload_people(self, pubs):
        ids = set()
        for spph in pubs:
            ids.update((spph.removed_by_id, spph.creator_id, spph.sponsor_id))
        ids.discard(None)
        if ids:
            list(
                getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                    ids, need_validity=True
                )
            )

    @property
    def batchnav(self):
        # No point using StormRangeFactory right now, as the sorted
        # lookup can't be fully indexed (it spans multiple archives).
        return BatchNavigator(
            DecoratedResultSet(
                self.context.publishing_history,
                pre_iter_hook=self._preload_people,
            ),
            self.request,
        )


class DistributionSourcePackagePublishingHistoryView(
    LaunchpadView, PublishingHistoryViewMixin
):
    """View for presenting `DistributionSourcePackage` publishing history."""

    page_title = "Publishing history"

    @property
    def label(self):
        return "Publishing history of %s" % self.context.title


class DistributionSourcePackageEditView(LaunchpadEditFormView):
    """Edit a distribution source package."""

    schema = IDistributionSourcePackage
    field_names = [
        "bug_reporting_guidelines",
        "content_templates",
        "bug_reported_acknowledgement",
        "enable_bugfiling_duplicate_search",
    ]

    @property
    def label(self):
        """The form label."""
        return "Edit %s" % self.context.title

    @property
    def page_title(self):
        """The page title."""
        return self.label

    @action("Change", name="change")
    def change_action(self, action, data):
        self.updateContextFromData(data)

    @property
    def next_url(self):
        return canonical_url(self.context)

    cancel_url = next_url


class DistributionSourcePackageHelpView(LaunchpadView):
    """A View to show Answers help."""

    page_title = "Help and support options"
