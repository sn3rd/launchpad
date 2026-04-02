# Copyright 2009-2023 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Browser views for distributions."""

__all__ = [
    "DistributionAddView",
    "DistributionAdminView",
    "DistributionArchiveMirrorsRSSView",
    "DistributionArchiveMirrorsView",
    "DistributionArchivesView",
    "DistributionChangeCodeAdminView",
    "DistributionChangeMembersView",
    "DistributionChangeMirrorAdminView",
    "DistributionChangeOCIProjectAdminView",
    "DistributionChangeSecurityAdminView",
    "DistributionCountryArchiveMirrorsView",
    "DistributionDisabledMirrorsView",
    "DistributionEditView",
    "DistributionFacets",
    "DistributionNavigation",
    "DistributionPPASearchView",
    "DistributionPackageSearchView",
    "DistributionPendingReviewMirrorsView",
    "DistributionPublisherConfigView",
    "DistributionReassignmentView",
    "DistributionSeriesView",
    "DistributionDerivativesView",
    "DistributionSeriesMirrorsRSSView",
    "DistributionSeriesMirrorsView",
    "DistributionSetActionNavigationMenu",
    "DistributionSetBreadcrumb",
    "DistributionSetContextMenu",
    "DistributionSetNavigation",
    "DistributionSetView",
    "DistributionSpecificationsMenu",
    "DistributionUnofficialMirrorsView",
    "DistributionView",
]

import datetime
from collections import defaultdict

from lazr.restful.utils import smartquote
from zope.component import getUtility
from zope.event import notify
from zope.formlib import form
from zope.formlib.boolwidgets import CheckBoxWidget
from zope.formlib.widget import CustomWidgetFactory
from zope.interface import implementer
from zope.lifecycleevent import ObjectCreatedEvent
from zope.schema import Bool
from zope.security.checker import canWrite
from zope.security.interfaces import Unauthorized

from lp.answers.browser.faqtarget import FAQTargetNavigationMixin
from lp.answers.browser.questiontarget import QuestionTargetTraversalMixin
from lp.app.browser.launchpadform import (
    LaunchpadEditFormView,
    LaunchpadFormView,
    action,
)
from lp.app.browser.lazrjs import InlinePersonEditPickerWidget
from lp.app.browser.tales import format_link
from lp.app.enums import PILLAR_INFORMATION_TYPES
from lp.app.errors import NotFoundError
from lp.app.vocabularies import InformationTypeVocabulary
from lp.app.widgets.image import ImageChangeWidget
from lp.app.widgets.itemswidgets import (
    LabeledMultiCheckBoxWidget,
    LaunchpadRadioWidgetWithDescription,
)
from lp.archivepublisher.interfaces.publisherconfig import (
    IPublisherConfig,
    IPublisherConfigSet,
)
from lp.blueprints.browser.specificationtarget import (
    HasSpecificationsMenuMixin,
)
from lp.bugs.browser.bugtask import BugTargetTraversalMixin
from lp.bugs.browser.structuralsubscription import (
    StructuralSubscriptionMenuMixin,
    StructuralSubscriptionTargetTraversalMixin,
    expose_structural_subscription_data_to_js,
)
from lp.bugs.interfaces.bugtarget import DISABLE_BUG_WEBHOOKS_FEATURE_FLAG
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.browser.vcslisting import TargetDefaultVCSNavigationMixin
from lp.registry.browser import RegistryEditFormView, add_subscribe_link
from lp.registry.browser.announcement import (
    HasAnnouncementsView,
    current_user_can_announce,
)
from lp.registry.browser.menu import (
    IRegistryCollectionNavigationMenu,
    RegistryCollectionActionMenuBase,
)
from lp.registry.browser.objectreassignment import ObjectReassignmentView
from lp.registry.browser.pillar import (
    BugTargetParentBugsMenu,
    PillarNavigationMixin,
    PillarViewMixin,
)
from lp.registry.browser.widgets.ocicredentialswidget import (
    OCICredentialsWidget,
)
from lp.registry.enums import DistributionDefaultTraversalPolicy
from lp.registry.interfaces.distribution import (
    IDistribution,
    IDistributionMirrorMenuMarker,
    IDistributionSet,
)
from lp.registry.interfaces.distributionmirror import (
    MirrorContent,
    MirrorSpeed,
)
from lp.registry.interfaces.externalpackage import ExternalPackageType
from lp.registry.interfaces.ociproject import (
    OCI_PROJECT_ALLOW_CREATE,
    IOCIProjectSet,
)
from lp.registry.interfaces.series import SeriesStatus
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.features import getFeatureFlag
from lp.services.feeds.browser import FeedsMixin
from lp.services.geoip.helpers import ipaddress_from_request, request_country
from lp.services.helpers import english_list
from lp.services.propertycache import cachedproperty
from lp.services.webapp import (
    ApplicationMenu,
    ContextMenu,
    LaunchpadView,
    Link,
    Navigation,
    NavigationMenu,
    StandardLaunchpadFacets,
    canonical_url,
    enabled_with_permission,
    redirection,
    stepthrough,
)
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.batching import BatchNavigator
from lp.services.webapp.breadcrumb import Breadcrumb
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webhooks.browser import WebhookTargetNavigationMixin
from lp.soyuz.browser.archive import EnableProcessorsMixin
from lp.soyuz.browser.packagesearch import PackageSearchViewBase
from lp.soyuz.enums import ArchivePurpose
from lp.soyuz.interfaces.archive import IArchiveSet


class DistributionNavigation(
    Navigation,
    BugTargetTraversalMixin,
    QuestionTargetTraversalMixin,
    FAQTargetNavigationMixin,
    StructuralSubscriptionTargetTraversalMixin,
    PillarNavigationMixin,
    TargetDefaultVCSNavigationMixin,
    WebhookTargetNavigationMixin,
):
    usedfor = IDistribution

    @redirection("+source", status=301)
    def redirect_source(self):
        return canonical_url(self.context)

    @stepthrough("+mirror")
    def traverse_mirrors(self, name):
        return self.context.getMirrorByName(name)

    @stepthrough("+source")
    def traverse_sources(self, name):
        dsp = self.context.getSourcePackage(name)
        policy = self.context.default_traversal_policy
        if (
            policy == DistributionDefaultTraversalPolicy.SOURCE_PACKAGE
            and not self.context.redirect_default_traversal
        ):
            return self.redirectSubTree(
                canonical_url(dsp, request=self.request), status=303
            )
        else:
            return dsp

    @stepthrough("+unknown")
    def traverse_unknown(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.UNKNOWN, None
        )

    @stepthrough("+generic")
    def traverse_generic(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.GENERIC, None
        )

    @stepthrough("+snap")
    def traverse_snap(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.SNAP, None
        )

    @stepthrough("+charm")
    def traverse_charm(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.CHARM, None
        )

    @stepthrough("+rock")
    def traverse_rock(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.ROCK, None
        )

    @stepthrough("+python")
    def traverse_python(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.PYTHON, None
        )

    @stepthrough("+conda")
    def traverse_conda(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.CONDA, None
        )

    @stepthrough("+cargo")
    def traverse_cargo(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.CARGO, None
        )

    @stepthrough("+maven")
    def traverse_maven(self, name):
        return self.context.getExternalPackage(
            name, ExternalPackageType.MAVEN, None
        )

    @stepthrough("+oci")
    def traverse_oci(self, name):
        oci_project = self.context.getOCIProject(name)
        policy = self.context.default_traversal_policy
        if (
            policy == DistributionDefaultTraversalPolicy.OCI_PROJECT
            and not self.context.redirect_default_traversal
        ):
            return self.redirectSubTree(
                canonical_url(oci_project, request=self.request), status=303
            )
        else:
            return oci_project

    @stepthrough("+milestone")
    def traverse_milestone(self, name):
        return self.context.getMilestone(name)

    @stepthrough("+announcement")
    def traverse_announcement(self, name):
        return self.context.getAnnouncement(name)

    @stepthrough("+spec")
    def traverse_spec(self, name):
        return self.context.getSpecification(name)

    @stepthrough("+archive")
    def traverse_archive(self, name):
        return self.context.getArchive(name)

    @stepthrough("+commercialsubscription")
    def traverse_commercialsubscription(self, name):
        return self.context.commercial_subscription

    def _resolveSeries(self, name):
        try:
            return self.context[name], False
        except NotFoundError:
            resolved = self.context.resolveSeriesAlias(name)
            return resolved, True

    @stepthrough("+series")
    def traverse_series(self, name):
        series, redirect = self._resolveSeries(name)
        if not redirect:
            policy = self.context.default_traversal_policy
            if (
                policy == DistributionDefaultTraversalPolicy.SERIES
                and not self.context.redirect_default_traversal
            ):
                redirect = True
        if redirect:
            return self.redirectSubTree(
                canonical_url(series, request=self.request), status=303
            )
        else:
            return series

    @stepthrough("+vulnerability")
    def traverse_vulnerability(self, id):
        try:
            id = int(id)
        except ValueError:
            # Not a number.
            return None

        return self.context.getVulnerability(id)

    def traverse(self, name):
        policy = self.context.default_traversal_policy
        if policy == DistributionDefaultTraversalPolicy.SERIES:
            obj, redirect = self._resolveSeries(name)
        elif policy == DistributionDefaultTraversalPolicy.SOURCE_PACKAGE:
            obj = self.context.getSourcePackage(name)
            redirect = False
        elif policy == DistributionDefaultTraversalPolicy.OCI_PROJECT:
            obj = self.context.getOCIProject(name)
            redirect = False
        else:
            raise AssertionError(
                "Unknown default traversal policy %r" % policy
            )
        if obj is None:
            return None
        if redirect or self.context.redirect_default_traversal:
            return self.redirectSubTree(
                canonical_url(obj, request=self.request), status=303
            )
        else:
            return obj


class DistributionSetNavigation(Navigation):
    usedfor = IDistributionSet

    def traverse(self, name):
        # Raise a 404 on an invalid distribution name
        distribution = self.context.getByName(name)
        if distribution is None:
            raise NotFoundError(name)
        return self.redirectSubTree(canonical_url(distribution))


class DistributionFacets(StandardLaunchpadFacets):
    usedfor = IDistribution


class DistributionSetBreadcrumb(Breadcrumb):
    """Builds a breadcrumb for an `IDistributionSet`."""

    text = "Distributions"


class DistributionSetContextMenu(ContextMenu):
    usedfor = IDistributionSet
    links = ["products", "distributions", "people", "meetings"]

    def distributions(self):
        return Link("/distros/", "View distributions")

    def products(self):
        return Link("/projects/", "View projects")

    def people(self):
        return Link("/people/", "View people")

    def meetings(self):
        return Link("/sprints/", "View meetings")


class DistributionMirrorsNavigationMenu(NavigationMenu):
    usedfor = IDistributionMirrorMenuMarker
    facet = "overview"
    links = (
        "cdimage_mirrors",
        "archive_mirrors",
        "disabled_mirrors",
        "pending_review_mirrors",
        "unofficial_mirrors",
    )

    @property
    def distribution(self):
        """Helper method to return the distribution object.

        self.context is the view, so return *its* context.
        """
        return self.context.context

    def cdimage_mirrors(self):
        text = "CD mirrors"
        return Link("+cdmirrors", text, icon="info")

    def archive_mirrors(self):
        text = "Archive mirrors"
        return Link("+archivemirrors", text, icon="info")

    def newmirror(self):
        text = "Register mirror"
        return Link("+newmirror", text, icon="add")

    def _userCanSeeNonPublicMirrorListings(self):
        """Does the user have rights to see non-public mirrors listings?"""
        user = getUtility(ILaunchBag).user
        return (
            self.distribution.supports_mirrors
            and user is not None
            and user.inTeam(self.distribution.mirror_admin)
        )

    def disabled_mirrors(self):
        text = "Disabled mirrors"
        enabled = self._userCanSeeNonPublicMirrorListings()
        return Link("+disabledmirrors", text, enabled=enabled, icon="info")

    def pending_review_mirrors(self):
        text = "Pending-review mirrors"
        enabled = self._userCanSeeNonPublicMirrorListings()
        return Link(
            "+pendingreviewmirrors", text, enabled=enabled, icon="info"
        )

    def unofficial_mirrors(self):
        text = "Unofficial mirrors"
        enabled = self._userCanSeeNonPublicMirrorListings()
        return Link("+unofficialmirrors", text, enabled=enabled, icon="info")


class DistributionLinksMixin(StructuralSubscriptionMenuMixin):
    """A mixin to provide common links to menus."""

    @enabled_with_permission("launchpad.Edit")
    def edit(self):
        text = "Change details"
        return Link("+edit", text, icon="edit")


class DistributionNavigationMenu(NavigationMenu, DistributionLinksMixin):
    """A menu of context actions."""

    usedfor = IDistribution
    facet = "overview"

    links = (
        "edit",
        "admin",
        "pubconf",
        "subscribe_to_bug_mail",
        "edit_bug_mail",
        "sharing",
        "new_oci_project",
        "search_oci_project",
        "webhooks",
    )

    @enabled_with_permission("launchpad.Admin")
    def admin(self):
        text = "Administer"
        return Link("+admin", text, icon="edit")

    @enabled_with_permission("launchpad.Admin")
    def pubconf(self):
        text = "Configure publisher"
        return Link("+pubconf", text, icon="edit")

    @enabled_with_permission("launchpad.Driver")
    def sharing(self):
        return Link("+sharing", "Sharing", icon="edit")

    def new_oci_project(self):
        text = "Create an OCI project"
        link = Link("+new-oci-project", text, icon="add")
        link.enabled = bool(
            getFeatureFlag(OCI_PROJECT_ALLOW_CREATE)
        ) and self.context.canAdministerOCIProjects(self.user)
        return link

    def search_oci_project(self):
        oci_projects = getUtility(IOCIProjectSet).findByPillarAndName(
            self.context, ""
        )
        text = "Search for OCI project"
        link = Link("+search-oci-project", text, icon="info")
        link.enabled = not oci_projects.is_empty()
        return link

    @enabled_with_permission("launchpad.Edit")
    def webhooks(self):
        return Link(
            "+webhooks",
            "Manage webhooks",
            icon="edit",
            enabled=not getFeatureFlag(DISABLE_BUG_WEBHOOKS_FEATURE_FLAG),
        )


class DistributionOverviewMenu(ApplicationMenu, DistributionLinksMixin):
    usedfor = IDistribution
    facet = "overview"
    links = [
        "edit",
        "branding",
        "driver",
        "search",
        "members",
        "mirror_admin",
        "oci_project_admin",
        "security_admin",
        "code_admin",
        "reassign",
        "addseries",
        "series",
        "derivatives",
        "milestones",
        "top_contributors",
        "builds",
        "cdimage_mirrors",
        "archive_mirrors",
        "pending_review_mirrors",
        "disabled_mirrors",
        "unofficial_mirrors",
        "newmirror",
        "announce",
        "announcements",
        "ppas",
        "configure_answers",
        "configure_blueprints",
        "configure_translations",
    ]

    @enabled_with_permission("launchpad.Edit")
    def branding(self):
        text = "Change branding"
        return Link("+branding", text, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def driver(self):
        text = "Appoint driver"
        summary = "Someone with permission to set goals for all series"
        return Link("+driver", text, summary, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def reassign(self):
        text = "Change maintainer"
        return Link("+reassign", text, icon="edit")

    def newmirror(self):
        text = "Register a new mirror"
        enabled = self.context.supports_mirrors
        return Link("+newmirror", text, enabled=enabled, icon="add")

    def top_contributors(self):
        text = "More contributors"
        return Link("+topcontributors", text, icon="info")

    def cdimage_mirrors(self):
        text = "CD mirrors"
        return Link("+cdmirrors", text, icon="info")

    def archive_mirrors(self):
        text = "Archive mirrors"
        return Link("+archivemirrors", text, icon="info")

    def _userCanSeeNonPublicMirrorListings(self):
        """Does the user have rights to see non-public mirrors listings?"""
        user = getUtility(ILaunchBag).user
        return (
            self.context.supports_mirrors
            and user is not None
            and user.inTeam(self.context.mirror_admin)
        )

    def disabled_mirrors(self):
        text = "Disabled mirrors"
        enabled = self._userCanSeeNonPublicMirrorListings()
        return Link("+disabledmirrors", text, enabled=enabled, icon="info")

    def pending_review_mirrors(self):
        text = "Pending-review mirrors"
        enabled = self._userCanSeeNonPublicMirrorListings()
        return Link(
            "+pendingreviewmirrors", text, enabled=enabled, icon="info"
        )

    def unofficial_mirrors(self):
        text = "Unofficial mirrors"
        enabled = self._userCanSeeNonPublicMirrorListings()
        return Link("+unofficialmirrors", text, enabled=enabled, icon="info")

    @enabled_with_permission("launchpad.Edit")
    def members(self):
        text = "Change members team"
        return Link("+selectmemberteam", text, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def mirror_admin(self):
        text = "Change mirror admins"
        enabled = self.context.supports_mirrors
        return Link("+selectmirroradmins", text, enabled=enabled, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def oci_project_admin(self):
        text = "Change OCI project admins"
        return Link("+select-oci-project-admins", text, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def security_admin(self):
        text = "Change security admins"
        return Link("+select-security-admins", text, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def code_admin(self):
        text = "Change code admins"
        return Link("+select-code-admins", text, icon="edit")

    def search(self):
        text = "Search packages"
        return Link("+search", text, icon="search")

    @enabled_with_permission("launchpad.Moderate")
    def addseries(self):
        text = "Add series"
        return Link("+addseries", text, icon="add")

    def series(self):
        text = "All series"
        return Link("+series", text, icon="info")

    def derivatives(self):
        text = "All derivatives"
        return Link("+derivatives", text, icon="info")

    def milestones(self):
        text = "All milestones"
        return Link("+milestones", text, icon="info")

    def announce(self):
        text = "Make announcement"
        summary = "Publish an item of news for this project"
        link = Link("+announce", text, summary, icon="add")
        if not current_user_can_announce(self.context):
            link.enabled = False
        return link

    def announcements(self):
        text = "Read all announcements"
        enabled = bool(self.context.getAnnouncements())
        return Link("+announcements", text, icon="info", enabled=enabled)

    def builds(self):
        text = "Builds"
        return Link("+builds", text, icon="info")

    def ppas(self):
        text = "Personal Package Archives"
        return Link("+ppas", text, icon="info")

    @enabled_with_permission("launchpad.Edit")
    def configure_answers(self):
        text = "Configure support tracker"
        summary = "Allow users to ask questions on this project"
        return Link("+edit", text, summary, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def configure_blueprints(self):
        text = "Configure blueprints"
        summary = "Enable tracking of feature planning."
        return Link("+edit", text, summary, icon="edit")

    @enabled_with_permission("launchpad.TranslationsAdmin")
    def configure_translations(self):
        text = "Configure translations"
        summary = "Allow users to provide translations for this project."
        return Link("+configure-translations", text, summary, icon="edit")


class DistributionBugsMenu(BugTargetParentBugsMenu):
    usedfor = IDistribution
    facet = "bugs"

    @property
    def links(self):
        links = ["bugsupervisor", "cve", "filebug"]
        add_subscribe_link(links)
        links.append("webhooks")
        return links


class DistributionSpecificationsMenu(
    NavigationMenu, HasSpecificationsMenuMixin
):
    usedfor = IDistribution
    facet = "specifications"
    links = ["listall", "doc", "assignments", "new", "register_sprint"]


class DistributionPackageSearchView(PackageSearchViewBase):
    """Customised PackageSearchView for Distribution"""

    def initialize(self):
        """Save the search type if provided."""
        super().initialize()

        # If the distribution contains binary packages, then we'll
        # default to searches on binary names, but allow the user to
        # select.
        if self.context.has_published_binaries:
            self.search_type = self.request.get("search_type", "binary")
        else:
            self.search_type = "source"

    def contextSpecificSearch(self):
        """See `AbstractPackageSearchView`."""

        if self.search_by_binary_name:
            return self.context.searchBinaryPackages(self.text)
        else:
            non_exact_matches = self.context.searchSourcePackageCaches(
                self.text
            )

            # The searchBinaryPackageCaches() method returns tuples, so we
            # use the DecoratedResultSet here to just get the
            # DistributionSourcePackag objects for the template.
            def tuple_to_package_cache(cache_name_tuple):
                return cache_name_tuple[0]

            non_exact_matches = DecoratedResultSet(
                non_exact_matches, tuple_to_package_cache
            )

        return non_exact_matches.config(distinct=True)

    @property
    def page_title(self):
        return smartquote("Search %s's packages" % self.context.displayname)

    @property
    def search_by_binary_name(self):
        """Return whether the search is on binary names.

        By default, we search by binary names, as this produces much
        better results. But the user may decide to search by sources, or
        in the case of other distributions, it will be the only option.
        """
        return self.search_type == "binary"

    @property
    def source_search_url(self):
        """Return the equivalent search on source packages.

        By default, we search by binary names, but also provide a link
        to the equivalent source package search in some circumstances.
        """
        return "%s/+search?search_type=source&%s" % (
            canonical_url(self.context),
            self.request.get("QUERY_STRING"),
        )

    @cachedproperty
    def exact_matches(self):
        return self.context.searchBinaryPackages(
            self.text, exact_match=True
        ).order_by("name")

    @property
    def has_exact_matches(self):
        return not self.exact_matches.is_empty()

    @property
    def has_matches(self):
        return self.matches > 0

    @cachedproperty
    def matching_binary_names(self):
        """Define the matching binary names for each result in the batch."""
        names = {}

        for package_cache in self.batchnav.currentBatch():
            names[package_cache.name] = self._listFirstFiveMatchingNames(
                self.text, package_cache.binpkgnames
            )

        return names

    def _listFirstFiveMatchingNames(self, match_text, space_separated_list):
        """Returns a comma-separated list of the first five matching items"""
        name_list = space_separated_list.split(" ")

        matching_names = [name for name in name_list if match_text in name]

        if len(matching_names) > 5:
            matching_names = matching_names[:5]
            matching_names.append("...")

        return ", ".join(matching_names)

    @cachedproperty
    def distroseries_names(self):
        """Define the distroseries for each package name in exact matches."""
        names = {}
        for package_cache in self.exact_matches:
            package = package_cache.distributionsourcepackage

            # Yay for alphabetical series names.
            distroseries_list = sorted(
                {
                    pubrec.distroseries.name
                    for pubrec in package.current_publishing_records
                    if pubrec.distroseries.active
                }
            )
            names[package.name] = ", ".join(distroseries_list)

        return names

    @property
    def display_exact_matches(self):
        """Return whether exact match results should be displayed."""
        if not self.search_by_binary_name:
            return False

        if self.batchnav.start > 0:
            return False

        return self.has_exact_matches


class DistributionView(PillarViewMixin, HasAnnouncementsView, FeedsMixin):
    """Default Distribution view class."""

    def initialize(self):
        super().initialize()
        expose_structural_subscription_data_to_js(
            self.context, self.request, self.user
        )

    @property
    def page_title(self):
        return "%s in Launchpad" % self.context.displayname

    @property
    def maintainer_widget(self):
        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["owner"],
            format_link(self.context.owner),
            header="Change maintainer",
            edit_view="+reassign",
            step_title="Select a new maintainer",
            show_create_team=True,
        )

    @property
    def driver_widget(self):
        if canWrite(self.context, "driver"):
            empty_value = "Specify a driver"
        else:
            empty_value = "None"
        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["driver"],
            format_link(self.context.driver, empty_value=empty_value),
            header="Change driver",
            edit_view="+driver",
            null_display_value=empty_value,
            step_title="Select a new driver",
            show_create_team=True,
        )

    @property
    def members_widget(self):
        if canWrite(self.context, "members"):
            empty_value = "Specify the members team"
        else:
            empty_value = "None"
        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["members"],
            format_link(self.context.members, empty_value=empty_value),
            header="Change the members team",
            edit_view="+selectmemberteam",
            null_display_value=empty_value,
            step_title="Select a new members team",
        )

    @property
    def mirror_admin_widget(self):
        if canWrite(self.context, "mirror_admin"):
            empty_value = "Specify a mirror administrator"
        else:
            empty_value = "None"
        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["mirror_admin"],
            format_link(self.context.mirror_admin, empty_value=empty_value),
            header="Change the mirror administrator",
            edit_view="+selectmirroradmins",
            null_display_value=empty_value,
            step_title="Select a new mirror administrator",
        )

    @property
    def oci_project_admin_widget(self):
        if canWrite(self.context, "oci_project_admin"):
            empty_value = "Specify an OCI project administrator"
        else:
            empty_value = "None"
        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["oci_project_admin"],
            format_link(
                self.context.oci_project_admin, empty_value=empty_value
            ),
            header="Change the OCI project administrator",
            edit_view="+select-oci-project-admins",
            null_display_value=empty_value,
            step_title="Select a new OCI project administrator",
        )

    @property
    def security_admin_widget(self):
        if canWrite(self.context, "security_admin"):
            empty_value = "Specify a security administrator"
        else:
            empty_value = "None"

        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["security_admin"],
            format_link(
                self.context.security_admin,
                empty_value=empty_value,
            ),
            header="Change the security administrator",
            edit_view="+select-security-admins",
            null_display_value=empty_value,
            step_title="Select a new security administrator",
        )

    @property
    def code_admin_widget(self):
        if canWrite(self.context, "code_admin"):
            empty_value = "Specify a code administrator"
        else:
            empty_value = "None"

        return InlinePersonEditPickerWidget(
            self.context,
            IDistribution["code_admin"],
            format_link(
                self.context.code_admin,
                empty_value=empty_value,
            ),
            header="Change the code administrator",
            edit_view="+select-code-admins",
            null_display_value=empty_value,
            step_title="Select a new code administrator",
        )

    def linkedMilestonesForSeries(self, series):
        """Return a string of linkified milestones in the series."""
        # Listify to remove repeated queries.
        milestones = list(series.milestones)
        if len(milestones) == 0:
            return ""

        linked_milestones = []
        for milestone in milestones:
            linked_milestones.append(
                "<a href=%s>%s</a>"
                % (canonical_url(milestone), milestone.name)
            )

        return english_list(linked_milestones)

    @cachedproperty
    def latest_derivatives(self):
        """The 5 most recent derivatives."""
        return self.context.derivatives[:5]

    @cachedproperty
    def show_commercial_subscription_info(self):
        """Should subscription information be shown?

        Subscription information is only shown to the distribution owners,
        Launchpad admins, and members of the Launchpad commercial team.  The
        first two are allowed via the Launchpad.Edit permission.  The latter
        is allowed via Launchpad.Commercial.
        """
        return check_permission(
            "launchpad.Edit", self.context
        ) or check_permission("launchpad.Commercial", self.context)


class DistributionArchivesView(LaunchpadView):
    @property
    def page_title(self):
        return "%s Copy Archives" % self.context.title

    @property
    def batchnav(self):
        """Return the batch navigator for the archives."""
        return BatchNavigator(self.archive_list, self.request)

    @cachedproperty
    def archive_list(self):
        """Returns the list of archives for the given distribution.

        The context may be an IDistroSeries or a users archives.
        """
        results = getUtility(IArchiveSet).getArchivesForDistribution(
            self.context,
            purposes=[ArchivePurpose.COPY],
            user=self.user,
            exclude_disabled=False,
        )
        return results.order_by("date_created DESC")


class DistributionPPASearchView(LaunchpadView):
    """Search PPAs belonging to the Distribution in question."""

    page_title = "Personal Package Archives"

    def initialize(self):
        self.name_filter = self.request.get("name_filter")
        if isinstance(self.name_filter, list):
            # This happens if someone hand-hacks the URL so that it has
            # more than one name_filter field.  We could do something
            # like form.getOne() so that the request would be rejected,
            # but we can actually do better and join the terms supplied
            # instead.
            self.name_filter = " ".join(self.name_filter)
        self.show_inactive = self.request.get("show_inactive")

    @property
    def label(self):
        return "Personal Package Archives for %s" % self.context.title

    @property
    def search_results(self):
        """Process search form request."""
        if self.name_filter is None:
            return None

        # Preserve self.show_inactive state because it's used in the
        # template and build a boolean field to be passed for
        # searchPPAs.
        show_inactive = self.show_inactive == "on"

        ppas = self.context.searchPPAs(
            text=self.name_filter, show_inactive=show_inactive, user=self.user
        )

        self.batchnav = BatchNavigator(ppas, self.request)
        return self.batchnav.currentBatch()

    @property
    def distribution_has_ppas(self):
        return not self.context.getAllPPAs().is_empty()

    @property
    def latest_ppa_source_publications(self):
        """Return the last 5 sources publication in the context PPAs."""
        archive_set = getUtility(IArchiveSet)
        return archive_set.getLatestPPASourcePublicationsForDistribution(
            distribution=self.context
        )

    @property
    def most_active_ppas(self):
        """Return the last 5 most active PPAs."""
        archive_set = getUtility(IArchiveSet)
        return archive_set.getMostActivePPAsForDistribution(
            distribution=self.context
        )


class DistributionSetActionNavigationMenu(RegistryCollectionActionMenuBase):
    """Action menu for `DistributionSetView`."""

    usedfor = IDistributionSet
    links = [
        "register_team",
        "register_project",
        "register_distribution",
        "create_account",
    ]


@implementer(IRegistryCollectionNavigationMenu)
class DistributionSetView(LaunchpadView):
    """View for /distros top level collection."""

    page_title = "Distributions registered in Launchpad"

    @cachedproperty
    def count(self):
        return self.context.count()


class RequireVirtualizedBuildersMixin:
    """A mixin that provides require_virtualized field support"""

    def createRequireVirtualized(self):
        return form.Fields(
            Bool(
                __name__="require_virtualized",
                title="Require virtualized builders",
                description=(
                    "Only build the distribution's packages on virtual "
                    "builders."
                ),
                required=True,
            )
        )

    def updateRequireVirtualized(self, require_virtualized, archive):
        if archive.require_virtualized != require_virtualized:
            archive.require_virtualized = require_virtualized


class DistributionAddView(
    LaunchpadFormView, RequireVirtualizedBuildersMixin, EnableProcessorsMixin
):
    schema = IDistribution
    label = "Register a new distribution"
    field_names = [
        "name",
        "display_name",
        "summary",
        "description",
        "domainname",
        "members",
        "official_malone",
        "blueprints_usage",
        "translations_usage",
        "answers_usage",
    ]
    custom_widget_require_virtualized = CheckBoxWidget
    custom_widget_processors = LabeledMultiCheckBoxWidget
    next_url = None

    @property
    def page_title(self):
        """The page title."""
        return self.label

    @property
    def initial_values(self):
        return {
            "processors": getUtility(IProcessorSet).getAll(),
            "require_virtualized": False,
        }

    @property
    def cancel_url(self):
        """See `LaunchpadFormView`."""
        return canonical_url(self.context)

    def setUpFields(self):
        """See `LaunchpadFormView`."""
        LaunchpadFormView.setUpFields(self)
        self.form_fields += self.createRequireVirtualized()
        self.form_fields += self.createEnabledProcessors(
            getUtility(IProcessorSet).getAll(),
            "The architectures on which the distribution's main archive can "
            "build.",
        )

    @action("Save", name="save")
    def save_action(self, action, data):
        distribution = getUtility(IDistributionSet).new(
            name=data["name"],
            display_name=data["display_name"],
            title=data["display_name"],
            summary=data["summary"],
            description=data["description"],
            domainname=data["domainname"],
            members=data["members"],
            owner=self.user,
            registrant=self.user,
        )
        archive = distribution.main_archive
        self.updateRequireVirtualized(data["require_virtualized"], archive)
        archive.setProcessors(
            data["processors"], check_permissions=True, user=self.user
        )

        notify(ObjectCreatedEvent(distribution))
        self.next_url = canonical_url(distribution)


class DistributionEditView(
    RegistryEditFormView,
    RequireVirtualizedBuildersMixin,
    EnableProcessorsMixin,
):
    schema = IDistribution
    field_names = [
        "display_name",
        "summary",
        "description",
        "bug_reporting_guidelines",
        "content_templates",
        "bug_reported_acknowledgement",
        "package_derivatives_email",
        "icon",
        "logo",
        "mugshot",
        "official_malone",
        "enable_bug_expiration",
        "blueprints_usage",
        "translations_usage",
        "answers_usage",
        "translation_focus",
        "default_traversal_policy",
        "redirect_default_traversal",
        "oci_registry_credentials",
    ]

    custom_widget_icon = CustomWidgetFactory(
        ImageChangeWidget, ImageChangeWidget.EDIT_STYLE
    )
    custom_widget_logo = CustomWidgetFactory(
        ImageChangeWidget, ImageChangeWidget.EDIT_STYLE
    )
    custom_widget_mugshot = CustomWidgetFactory(
        ImageChangeWidget, ImageChangeWidget.EDIT_STYLE
    )
    custom_widget_require_virtualized = CheckBoxWidget
    custom_widget_processors = LabeledMultiCheckBoxWidget
    custom_widget_oci_registry_credentials = OCICredentialsWidget

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Change %s details" % self.context.displayname

    def setUpFields(self):
        """See `LaunchpadFormView`."""
        RegistryEditFormView.setUpFields(self)
        self.form_fields += self.createRequireVirtualized()
        self.form_fields += self.createEnabledProcessors(
            getUtility(IProcessorSet).getAll(),
            "The architectures on which the distribution's main archive can "
            "build.",
        )

    @property
    def initial_values(self):
        main_archive = self.context.main_archive
        return {
            "require_virtualized": main_archive.require_virtualized,
            "processors": main_archive.processors,
        }

    def validate(self, data):
        """Constrain bug expiration to Launchpad Bugs tracker."""
        # enable_bug_expiration is disabled by JavaScript when official_malone
        # is set False. The constraint is enforced here in case the JavaScript
        # fails to load or activate.
        official_malone = data.get("official_malone", False)
        if not official_malone:
            data["enable_bug_expiration"] = False
        if "processors" in data:
            widget = self.widgets["processors"]
            for processor in self.context.main_archive.processors:
                if processor not in data["processors"]:
                    if processor.name in widget.disabled_items:
                        # This processor is restricted and currently
                        # enabled.  Leave it untouched.
                        data["processors"].append(processor)

    def change_archive_fields(self, data):
        # Update context.main_archive.
        new_require_virtualized = data.get("require_virtualized")
        if new_require_virtualized is not None:
            self.updateRequireVirtualized(
                new_require_virtualized, self.context.main_archive
            )
            del data["require_virtualized"]
        new_processors = data.get("processors")
        if new_processors is not None:
            if set(self.context.main_archive.processors) != set(
                new_processors
            ):
                self.context.main_archive.setProcessors(
                    new_processors, check_permissions=True, user=self.user
                )
            del data["processors"]

    @action("Change", name="change")
    def change_action(self, action, data):
        self.change_archive_fields(data)
        new_credentials = data.pop("oci_registry_credentials", None)
        old_credentials = self.context.oci_registry_credentials
        if self.context.oci_registry_credentials != new_credentials:
            # Remove the old credentials as we're assigning new ones
            # or clearing them
            self.context.oci_registry_credentials = new_credentials
            if old_credentials:
                old_credentials.destroySelf()
        self.updateContextFromData(data)


class DistributionAdminView(LaunchpadEditFormView):
    schema = IDistribution
    field_names = [
        "official_packages",
        "supports_ppas",
        "supports_mirrors",
        "default_traversal_policy",
        "redirect_default_traversal",
        "information_type",
    ]
    custom_widget_information_type = CustomWidgetFactory(
        LaunchpadRadioWidgetWithDescription,
        vocabulary=InformationTypeVocabulary(types=PILLAR_INFORMATION_TYPES),
    )
    next_url = None

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Administer %s" % self.context.displayname

    def validate(self, data):
        super().validate(data)
        information_type = data.get("information_type")
        if information_type:
            errors = [
                str(e)
                for e in self.context.checkInformationType(information_type)
            ]
            if len(errors) > 0:
                self.setFieldError("information_type", " ".join(errors))

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @action("Change", name="change")
    def change_action(self, action, data):
        self.updateContextFromData(data)
        self.next_url = canonical_url(self.context)


class DistributionSeriesBaseView(LaunchpadView):
    """A base view to list distroseries."""

    @cachedproperty
    def styled_series(self):
        """A list of dicts; keys: series, css_class, is_development_focus"""
        all_series = []
        for series in self._displayed_series:
            all_series.append(
                {
                    "series": series,
                    "css_class": self.getCssClass(series),
                }
            )
        return all_series

    def getCssClass(self, series):
        """The highlight, lowlight, or normal CSS class."""
        if series.status == SeriesStatus.DEVELOPMENT:
            return "highlight"
        elif series.status == SeriesStatus.OBSOLETE:
            return "lowlight"
        else:
            # This is normal presentation.
            return ""


class DistributionSeriesView(DistributionSeriesBaseView):
    """A view to list the distribution series."""

    label = "Timeline"
    show_add_series_link = True
    show_milestones_link = True

    @property
    def _displayed_series(self):
        return self.context.series


class DistributionDerivativesView(DistributionSeriesBaseView):
    """A view to list the distribution derivatives."""

    label = "Derivatives"
    show_add_series_link = False
    show_milestones_link = False

    @property
    def _displayed_series(self):
        return self.context.derivatives


class DistributionChangeMirrorAdminView(RegistryEditFormView):
    """A view to change the mirror administrator."""

    schema = IDistribution
    field_names = ["mirror_admin"]

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Change the %s mirror administrator" % self.context.displayname


class DistributionChangeOCIProjectAdminView(RegistryEditFormView):
    """A view to change the OCI project administrator."""

    schema = IDistribution
    field_names = ["oci_project_admin"]

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Change the %s OCI project administrator" % (
            self.context.displayname
        )


class DistributionChangeSecurityAdminView(RegistryEditFormView):
    """A view to change the security administrator."""

    schema = IDistribution
    field_names = ["security_admin"]

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Change the %s security administrator" % (
            self.context.displayname
        )


class DistributionChangeCodeAdminView(RegistryEditFormView):
    """A view to change the code administrator."""

    schema = IDistribution
    field_names = ["code_admin"]

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Change the %s code administrator" % (self.context.displayname)


class DistributionChangeMembersView(RegistryEditFormView):
    """A view to change the members team."""

    schema = IDistribution
    field_names = ["members"]

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Change the %s members team" % self.context.displayname


@implementer(IDistributionMirrorMenuMarker)
class DistributionCountryArchiveMirrorsView(LaunchpadView):
    """A text/plain page that lists the mirrors in the country of the request.

    If there are no mirrors located in the country of the request, we fallback
    to the main Ubuntu repositories.
    """

    def render(self):
        request = self.request
        if not self.context.supports_mirrors:
            request.response.setStatus(404)
            return ""
        ip_address = ipaddress_from_request(request)
        country = request_country(request)
        mirrors = self.context.getBestMirrorsForCountry(
            country, MirrorContent.ARCHIVE
        )
        body = "\n".join(mirror.base_url for mirror in mirrors)
        request.response.setHeader("content-type", "text/plain;charset=utf-8")
        if country is None:
            country_name = "Unknown"
        else:
            country_name = country.name
        request.response.setHeader("X-Generated-For-Country", country_name)
        request.response.setHeader("X-Generated-For-IP", ip_address)
        # XXX: Guilherme Salgado 2008-01-09 bug=173729: These are here only
        # for debugging.
        request.response.setHeader(
            "X-REQUEST-HTTP_X_FORWARDED_FOR",
            request.get("HTTP_X_FORWARDED_FOR"),
        )
        request.response.setHeader(
            "X-REQUEST-REMOTE_ADDR", request.get("REMOTE_ADDR")
        )
        return body.encode("utf-8")


@implementer(IDistributionMirrorMenuMarker)
class DistributionMirrorsView(LaunchpadView):
    show_freshness = True
    show_mirror_type = False
    description = None
    page_title = "Mirrors"

    @cachedproperty
    def mirror_count(self):
        return len(self.mirrors)

    def _sum_throughput(self, mirrors):
        """Given a list of mirrors, calculate the total bandwidth
        available.
        """
        throughput = 0
        # this would be a wonderful place to have abused DBItem.sort_key ;-)
        for mirror in mirrors:
            if mirror.speed == MirrorSpeed.S128K:
                throughput += 128
            elif mirror.speed == MirrorSpeed.S256K:
                throughput += 256
            elif mirror.speed == MirrorSpeed.S512K:
                throughput += 512
            elif mirror.speed == MirrorSpeed.S1M:
                throughput += 1000
            elif mirror.speed == MirrorSpeed.S2M:
                throughput += 2000
            elif mirror.speed == MirrorSpeed.S10M:
                throughput += 10000
            elif mirror.speed == MirrorSpeed.S45M:
                throughput += 45000
            elif mirror.speed == MirrorSpeed.S100M:
                throughput += 100000
            elif mirror.speed == MirrorSpeed.S1G:
                throughput += 1000000
            elif mirror.speed == MirrorSpeed.S2G:
                throughput += 2000000
            elif mirror.speed == MirrorSpeed.S4G:
                throughput += 4000000
            elif mirror.speed == MirrorSpeed.S10G:
                throughput += 10000000
            elif mirror.speed == MirrorSpeed.S20G:
                throughput += 20000000
            elif mirror.speed == MirrorSpeed.S50G:
                throughput += 50000000
            elif mirror.speed == MirrorSpeed.S100G:
                throughput += 100000000
            else:
                # need to be made aware of new values in
                # interfaces/distributionmirror.py MirrorSpeed
                return "Indeterminate"
        if throughput < 1000:
            return str(throughput) + " Kbps"
        elif throughput < 1000000:
            return str(throughput // 1000) + " Mbps"
        else:
            return str(throughput // 1000000) + " Gbps"

    @cachedproperty
    def total_throughput(self):
        return self._sum_throughput(self.mirrors)

    def getMirrorsGroupedByCountry(self):
        """Given a list of mirrors, create and return list of dictionaries
        containing the country names and the list of mirrors on that country.

        This list is ordered by country name.
        """
        mirrors_by_country = defaultdict(list)
        for mirror in self.mirrors:
            mirrors_by_country[mirror.country.name].append(mirror)

        return [
            dict(
                country=country,
                mirrors=mirrors,
                number=len(mirrors),
                throughput=self._sum_throughput(mirrors),
            )
            for country, mirrors in sorted(mirrors_by_country.items())
        ]


class DistributionArchiveMirrorsView(DistributionMirrorsView):
    heading = "Official Archive Mirrors"
    description = (
        "These mirrors provide repositories and archives of all "
        "software for the distribution."
    )

    @cachedproperty
    def mirrors(self):
        return self.context.archive_mirrors_by_country


class DistributionSeriesMirrorsView(DistributionMirrorsView):
    heading = "Official CD Mirrors"
    description = (
        "These mirrors offer ISO images which you can download "
        "and burn to CD to make installation disks."
    )
    show_freshness = False

    @cachedproperty
    def mirrors(self):
        return self.context.cdimage_mirrors_by_country


class DistributionMirrorsRSSBaseView(LaunchpadView):
    """A base class for RSS feeds of distribution mirrors."""

    def initialize(self):
        self.now = datetime.datetime.utcnow()

    def render(self):
        self.request.response.setHeader(
            "content-type", "text/xml;charset=utf-8"
        )
        body = LaunchpadView.render(self)
        return body.encode("utf-8")


class DistributionArchiveMirrorsRSSView(DistributionMirrorsRSSBaseView):
    """The RSS feed for archive mirrors."""

    heading = "Archive Mirrors"

    @cachedproperty
    def mirrors(self):
        return self.context.archive_mirrors


class DistributionSeriesMirrorsRSSView(DistributionMirrorsRSSBaseView):
    """The RSS feed for series mirrors."""

    heading = "CD Mirrors"

    @cachedproperty
    def mirrors(self):
        return self.context.cdimage_mirrors


class DistributionMirrorsAdminView(DistributionMirrorsView):
    def initialize(self):
        """Raise an Unauthorized exception if the user is not a member of this
        distribution's mirror_admin team.
        """
        # XXX: Guilherme Salgado 2006-06-16:
        # We don't want these pages to be public but we can't protect
        # them with launchpad.Edit because that would mean only people with
        # that permission on a Distribution would be able to see them. That's
        # why we have to do the permission check here.
        if not (self.user and self.user.inTeam(self.context.mirror_admin)):
            raise Unauthorized("Forbidden")


class DistributionUnofficialMirrorsView(DistributionMirrorsAdminView):
    heading = "Unofficial Mirrors"

    @cachedproperty
    def mirrors(self):
        return self.context.unofficial_mirrors


class DistributionPendingReviewMirrorsView(DistributionMirrorsAdminView):
    heading = "Pending-review mirrors"
    show_mirror_type = True
    show_freshness = False

    @cachedproperty
    def mirrors(self):
        return self.context.pending_review_mirrors


class DistributionDisabledMirrorsView(DistributionMirrorsAdminView):
    heading = "Disabled Mirrors"

    @cachedproperty
    def mirrors(self):
        return self.context.disabled_mirrors


class DistributionReassignmentView(ObjectReassignmentView):
    """View class for changing distribution maintainer."""

    ownerOrMaintainerName = "maintainer"


class DistributionPublisherConfigView(LaunchpadFormView):
    """View class for configuring publisher options for a DistroSeries.

    It redirects to the main distroseries page after a successful edit.
    """

    schema = IPublisherConfig
    field_names = ["root_dir", "base_url", "copy_base_url"]
    next_url = None

    @property
    def label(self):
        """See `LaunchpadFormView`."""
        return "Publisher configuration for %s" % self.context.title

    @property
    def page_title(self):
        """The page title."""
        return self.label

    @property
    def cancel_url(self):
        """See `LaunchpadFormView`."""
        return canonical_url(self.context)

    @property
    def initial_values(self):
        """If the config already exists, set up the fields with data."""
        config = getUtility(IPublisherConfigSet).getByDistribution(
            self.context
        )
        values = {}
        if config is not None:
            for name in self.field_names:
                values[name] = getattr(config, name)

        return values

    @action("Save")
    def save_action(self, action, data):
        """Update the context and redirect to its overview page."""
        config = getUtility(IPublisherConfigSet).getByDistribution(
            self.context
        )
        if config is None:
            config = getUtility(IPublisherConfigSet).new(
                distribution=self.context,
                root_dir=data["root_dir"],
                base_url=data["base_url"],
                copy_base_url=data["copy_base_url"],
            )
        else:
            form.applyChanges(config, self.form_fields, data, self.adapters)

        self.request.response.addInfoNotification(
            "Your changes have been applied."
        )
        self.next_url = canonical_url(self.context)
