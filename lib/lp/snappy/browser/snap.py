# Copyright 2015-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Snap views."""

__all__ = [
    "SnapAddView",
    "SnapAuthorizeView",
    "SnapContextMenu",
    "SnapDeleteView",
    "SnapEditView",
    "SnapNavigation",
    "SnapNavigationMenu",
    "SnapRequestBuildsView",
    "SnapView",
]

from urllib.parse import urlencode

from lazr.restful.fields import Reference
from lazr.restful.interface import copy_field, use_template
from zope.component import getUtility
from zope.error.interfaces import IErrorReportingUtility
from zope.formlib.widget import CustomWidgetFactory
from zope.interface import Interface, implementer
from zope.schema import Choice, List, TextLine
from zope.security.interfaces import Unauthorized

from lp import _
from lp.app.browser.launchpadform import (
    LaunchpadEditFormView,
    LaunchpadFormView,
    action,
    render_radio_widget_part,
)
from lp.app.browser.lazrjs import InlinePersonEditPickerWidget
from lp.app.browser.tales import format_link
from lp.app.enums import PRIVATE_INFORMATION_TYPES
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.vocabularies import InformationTypeVocabulary
from lp.app.widgets.itemswidgets import (
    LabeledMultiCheckBoxWidget,
    LaunchpadDropdownWidget,
    LaunchpadRadioWidget,
    LaunchpadRadioWidgetWithDescription,
)
from lp.app.widgets.snapbuildchannels import SnapBuildChannelsWidget
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.interfaces.processor import IProcessorSet
from lp.code.browser.widgets.gitref import GitRefWidget
from lp.code.interfaces.branch import IBranch
from lp.code.interfaces.gitref import IGitRef
from lp.registry.enums import VCSType
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.personproduct import IPersonProductFactory
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.product import IProduct
from lp.services.features import getFeatureFlag
from lp.services.propertycache import cachedproperty
from lp.services.scripts import log
from lp.services.utils import seconds_since_epoch
from lp.services.webapp import (
    ContextMenu,
    LaunchpadView,
    Link,
    Navigation,
    NavigationMenu,
    canonical_url,
    enabled_with_permission,
    stepthrough,
    structured,
)
from lp.services.webapp.breadcrumb import Breadcrumb, NameBreadcrumb
from lp.services.webapp.interfaces import ICanonicalUrlData
from lp.services.webapp.url import urlappend
from lp.services.webhooks.browser import WebhookTargetNavigationMixin
from lp.snappy.browser.widgets.snaparchive import SnapArchiveWidget
from lp.snappy.browser.widgets.storechannels import StoreChannelsWidget
from lp.snappy.interfaces.snap import (
    SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG,
    CannotAuthorizeStoreUploads,
    CannotFetchSnapcraftYaml,
    CannotParseSnapcraftYaml,
    ISnap,
    ISnapSet,
    MissingSnapcraftYaml,
    NoSuchSnap,
)
from lp.snappy.interfaces.snapbuild import ISnapBuild, ISnapBuildSet
from lp.snappy.interfaces.snappyseries import (
    ISnappyDistroSeriesSet,
    ISnappySeriesSet,
)
from lp.snappy.interfaces.snapstoreclient import (
    BadRequestPackageUploadResponse,
    SnapNotFoundResponse,
)
from lp.snappy.vocabularies import SyntheticSnappyDistroSeries
from lp.soyuz.browser.archive import EnableProcessorsMixin
from lp.soyuz.browser.build import get_build_by_id_str
from lp.soyuz.interfaces.archive import IArchive


@implementer(ICanonicalUrlData)
class SnapURL:
    """Snap URL creation rules."""

    rootsite = "mainsite"

    def __init__(self, snap):
        self.snap = snap

    @property
    def inside(self):
        owner = self.snap.owner
        project = self.snap.project
        if project is None:
            return owner
        return getUtility(IPersonProductFactory).create(owner, project)

    @property
    def path(self):
        return "+snap/%s" % self.snap.name


class SnapNavigation(WebhookTargetNavigationMixin, Navigation):
    usedfor = ISnap

    @stepthrough("+build-request")
    def traverse_build_request(self, name):
        try:
            job_id = int(name)
        except ValueError:
            return None
        return self.context.getBuildRequest(job_id)

    @stepthrough("+build")
    def traverse_build(self, name):
        build = get_build_by_id_str(ISnapBuildSet, name)
        if build is None or build.snap != self.context:
            return None
        return build

    @stepthrough("+subscription")
    def traverse_subscription(self, name):
        """Traverses to an `ISnapSubscription`."""
        person = getUtility(IPersonSet).getByName(name)
        if person is not None:
            return self.context.getSubscription(person)


class SnapFormMixin:
    def validateVCSWidgets(self, cls, data):
        """Validates if VCS sub-widgets."""
        # Set widgets as required or optional depending on the vcs field.
        vcs = data.get("vcs")
        if vcs == VCSType.BZR:
            self.widgets["branch"].context.required = True
            self.widgets["git_ref"].context.required = False
        elif vcs == VCSType.GIT:
            self.widgets["branch"].context.required = False
            self.widgets["git_ref"].context.required = True
        else:
            raise AssertionError("Unknown branch type %s" % vcs)

    def setUpVCSWidgets(self):
        widget = self.widgets.get("vcs")
        if widget is not None:
            current_value = widget._getFormValue()
            self.vcs_bzr_radio, self.vcs_git_radio = (
                render_radio_widget_part(widget, value, current_value)
                for value in (VCSType.BZR, VCSType.GIT)
            )


class SnapInformationTypeMixin:
    def getPossibleInformationTypes(self, snap, user):
        """Get the information types to display on the edit form.

        We display a customised set of information types: anything allowed
        by the repository's model, plus the current type.
        """
        allowed_types = set(snap.getAllowedInformationTypes(user))
        allowed_types.add(snap.information_type)
        return allowed_types

    def validateInformationType(self, data, snap=None):
        """Validates the information_type and project on data dictionary.

        The possible information types are defined by the given `snap`.
        When creating a new snap, `snap` should be None and the possible
        information types will be calculated based on the project.
        """
        info_type = data.get("information_type")
        if IProduct.providedBy(self.context):
            project = self.context
        else:
            project = data.get("project")
        if info_type is None and project is None:
            # Nothing to validate here. Move on.
            return
        if project is None and info_type in PRIVATE_INFORMATION_TYPES:
            self.setFieldError(
                "information_type",
                "Private snap recipes must be associated with a project.",
            )
        elif project is not None:
            if snap is None:
                snap_set = getUtility(ISnapSet)
                possible_types = snap_set.getPossibleSnapInformationTypes(
                    project
                )
            else:
                possible_types = self.getPossibleInformationTypes(
                    snap, self.user
                )
            if info_type not in possible_types:
                msg = (
                    "Project %s only accepts the following information "
                    "types: %s."
                )
                msg %= (
                    project.name,
                    ", ".join(i.title for i in possible_types),
                )
                self.setFieldError("information_type", msg)


class SnapBreadcrumb(NameBreadcrumb):
    @property
    def inside(self):
        return Breadcrumb(
            self.context.owner,
            url=canonical_url(self.context.owner, view_name="+snaps"),
            text="Snap packages",
            inside=self.context.owner,
        )


class SnapNavigationMenu(NavigationMenu):
    """Navigation menu for snap packages."""

    usedfor = ISnap

    facet = "overview"

    links = ("admin", "edit", "webhooks", "authorize", "delete")

    @enabled_with_permission("launchpad.Admin")
    def admin(self):
        return Link("+admin", "Administer snap package", icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def edit(self):
        return Link("+edit", "Edit snap package", icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def webhooks(self):
        return Link("+webhooks", "Manage webhooks", icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def authorize(self):
        if self.context.store_secrets:
            text = "Reauthorize store uploads"
        else:
            text = "Authorize store uploads"
        return Link("+authorize", text, icon="edit")

    @enabled_with_permission("launchpad.Edit")
    def delete(self):
        return Link("+delete", "Delete snap package", icon="trash-icon")


class SnapContextMenu(ContextMenu):
    """Context menu for snap packages."""

    usedfor = ISnap

    facet = "overview"

    links = ("request_builds", "add_subscriber", "subscription")

    @enabled_with_permission("launchpad.Edit")
    def request_builds(self):
        return Link("+request-builds", "Request builds", icon="add")

    @enabled_with_permission("launchpad.AnyPerson")
    def subscription(self):
        if self.context.hasSubscription(self.user):
            url = "+subscription/%s" % self.user.name
            text = "Edit your subscription"
            icon = "edit"
        else:
            url = "+subscribe"
            text = "Subscribe yourself"
            icon = "add"
        return Link(url, text, icon=icon)

    @enabled_with_permission("launchpad.AnyPerson")
    def add_subscriber(self):
        text = "Subscribe someone else"
        return Link("+addsubscriber", text, icon="add")


class SnapView(LaunchpadView):
    """Default view of a Snap."""

    @cachedproperty
    def builds_and_requests(self):
        return builds_and_requests_for_snap(self.context)

    @property
    def person_picker(self):
        field = copy_field(
            ISnap["owner"],
            vocabularyName="AllUserTeamsParticipationPlusSelfSimpleDisplay",
        )
        return InlinePersonEditPickerWidget(
            self.context,
            field,
            format_link(self.context.owner),
            header="Change owner",
            step_title="Select a new owner",
        )

    @property
    def build_frequency(self):
        if self.context.auto_build:
            return "Built automatically"
        else:
            return "Built on request"

    @property
    def sorted_auto_build_channels_items(self):
        if self.context.auto_build_channels is None:
            return []
        return sorted(self.context.auto_build_channels.items())

    @property
    def store_channels(self):
        return ", ".join(self.context.store_channels)

    @property
    def user_can_see_source(self):
        try:
            return self.context.source.visibleByUser(self.user)
        except Unauthorized:
            return False


def builds_and_requests_for_snap(snap):
    """A list of interesting builds and build requests.

    All pending builds and pending build requests are shown, as well as up
    to 10 recent builds and recent failed build requests.  Pending items are
    ordered by the date they were created; recent items are ordered by the
    date they finished (if available) or the date they started (if the date
    they finished is not set due to an error).  This allows started but
    unfinished builds to show up in the view but be discarded as more recent
    builds become available.

    Builds that the user does not have permission to see are excluded (by
    the model code).
    """

    # We need to interleave items of different types, so SQL can't do all
    # the sorting for us.
    def make_sort_key(*date_attrs):
        def _sort_key(item):
            for date_attr in date_attrs:
                if getattr(item, date_attr, None) is not None:
                    return -seconds_since_epoch(getattr(item, date_attr))
            return 0

        return _sort_key

    items = sorted(
        list(snap.pending_builds) + list(snap.pending_build_requests),
        key=make_sort_key("date_created", "date_requested"),
    )
    if len(items) < 10:
        # We need to interleave two unbounded result sets, but we only need
        # enough items from them to make the total count up to 10.  It's
        # simplest to just fetch the upper bound from each set and do our
        # own sorting.
        recent_items = sorted(
            list(snap.completed_builds[: 10 - len(items)])
            + list(snap.failed_build_requests[: 10 - len(items)]),
            key=make_sort_key(
                "date_finished",
                "date_started",
                "date_created",
                "date_requested",
            ),
        )
        items.extend(recent_items[: 10 - len(items)])
    return items


class HintedSnapBuildChannelsWidget(SnapBuildChannelsWidget):
    """A variant of SnapBuildChannelsWidget with appropriate hints."""

    def __init__(self, context, request):
        super().__init__(context, request)
        self.hint = (
            "The channels to use for build tools when building the snap "
            "package.\n"
        )
        default_snapcraft_channel = (
            getFeatureFlag(SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG) or "apt"
        )
        if default_snapcraft_channel == "apt":
            self.hint += (
                'If unset, or if the channel for snapcraft is set to "apt", '
                "the default is to install snapcraft from the source archive "
                "using apt."
            )
        else:
            self.hint += (
                'If unset, the default is to install snapcraft from the "%s" '
                'channel.  Setting the channel for snapcraft to "apt" causes '
                "snapcraft to be installed from the source archive using "
                "apt." % default_snapcraft_channel
            )


class SnapRequestBuildsView(LaunchpadFormView):
    """A view for requesting builds of a snap package."""

    next_url = None

    @property
    def label(self):
        return "Request builds for %s" % self.context.name

    page_title = "Request builds"

    class schema(Interface):
        """Schema for requesting a build."""

        archive = Reference(IArchive, title="Source archive", required=True)
        distro_arch_series = List(
            Choice(vocabulary="SnapDistroArchSeries"),
            title="Architectures",
            required=True,
            description=(
                "If you do not explicitly select any architectures, then the "
                "snap package will be built for all architectures allowed by "
                "its configuration."
            ),
        )
        pocket = copy_field(
            ISnapBuild["pocket"], title="Pocket", readonly=False
        )
        channels = copy_field(
            ISnap["auto_build_channels"],
            __name__="channels",
            title="Source snap channels",
            required=True,
        )

    custom_widget_archive = SnapArchiveWidget
    custom_widget_distro_arch_series = LabeledMultiCheckBoxWidget
    custom_widget_pocket = LaunchpadDropdownWidget
    custom_widget_channels = HintedSnapBuildChannelsWidget

    help_links = {
        "pocket": "/+help-snappy/snap-build-pocket.html",
    }

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @property
    def initial_values(self):
        """See `LaunchpadFormView`."""
        return {
            "archive": (
                # XXX cjwatson 2019-02-04: In order to support non-Ubuntu
                # bases, we'd need to store this as None and infer it based
                # on the inferred distro series; but this will do for now.
                getUtility(ILaunchpadCelebrities).ubuntu.main_archive
                if self.context.distro_series is None
                else self.context.distro_series.main_archive
            ),
            "distro_arch_series": [],
            "pocket": PackagePublishingPocket.UPDATES,
            "channels": self.context.auto_build_channels,
        }

    @action("Request builds", name="request")
    def request_action(self, action, data):
        if data.get("distro_arch_series", []):
            architectures = [
                arch.architecturetag for arch in data["distro_arch_series"]
            ]
        else:
            architectures = None
        self.context.requestBuilds(
            self.user,
            data["archive"],
            data["pocket"],
            architectures=architectures,
            channels=data["channels"],
        )
        self.request.response.addNotification(
            _("Builds will be dispatched soon.")
        )
        self.next_url = self.cancel_url


class ISnapEditSchema(Interface):
    """Schema for adding or editing a snap package."""

    use_template(
        ISnap,
        include=[
            "owner",
            "name",
            "information_type",
            "project",
            "require_virtualized",
            "allow_internet",
            "build_source_tarball",
            "build_path",
            "auto_build",
            "auto_build_channels",
            "store_upload",
            "pro_enable",
            "use_fetch_service",
            "fetch_service_policy",
        ],
    )

    store_distro_series = Choice(
        vocabulary="SnappyDistroSeries", required=True, title="Series"
    )
    vcs = Choice(vocabulary=VCSType, required=True, title="VCS")

    # Each of these is only required if vcs has an appropriate value.  Later
    # validation takes care of adjusting the required attribute.
    branch = copy_field(ISnap["branch"], required=True)
    git_ref = copy_field(ISnap["git_ref"], required=True)

    # These are only required if auto_build is True.  Later validation takes
    # care of adjusting the required attribute.
    auto_build_archive = copy_field(ISnap["auto_build_archive"], required=True)
    auto_build_pocket = copy_field(ISnap["auto_build_pocket"], required=True)

    # This is only required if store_upload is True.  Later validation takes
    # care of adjusting the required attribute.
    store_name = copy_field(ISnap["store_name"], required=True)
    store_channels = copy_field(ISnap["store_channels"], required=True)

    use_fetch_service = copy_field(ISnap["use_fetch_service"], required=True)
    fetch_service_policy = copy_field(
        ISnap["fetch_service_policy"], required=True
    )


def log_oops(error, request):
    """Log an oops report without raising an error."""
    info = (error.__class__, error, None)
    getUtility(IErrorReportingUtility).raising(info, request)


class SnapAuthorizeMixin:
    next_url = None

    def requestAuthorization(self, snap):
        try:
            self.next_url = SnapAuthorizeView.requestAuthorization(
                snap, self.request
            )
        except BadRequestPackageUploadResponse as e:
            self.setFieldError(
                "store_upload",
                "Cannot get permission from the store to upload this package.",
            )
            log_oops(e, self.request)


class SnapAddView(
    SnapAuthorizeMixin,
    EnableProcessorsMixin,
    SnapInformationTypeMixin,
    SnapFormMixin,
    LaunchpadFormView,
):
    """View for creating snap packages."""

    page_title = label = "Create a new snap package"

    schema = ISnapEditSchema
    next_url = None

    custom_widget_vcs = LaunchpadRadioWidget
    custom_widget_git_ref = CustomWidgetFactory(
        GitRefWidget, allow_external=True
    )
    custom_widget_store_distro_series = LaunchpadRadioWidget
    custom_widget_auto_build_archive = SnapArchiveWidget
    custom_widget_auto_build_pocket = LaunchpadDropdownWidget
    custom_widget_auto_build_channels = HintedSnapBuildChannelsWidget
    custom_widget_store_channels = StoreChannelsWidget

    help_links = {
        "auto_build_pocket": "/+help-snappy/snap-build-pocket.html",
    }

    @property
    def field_names(self):
        fields = ["owner", "name"]
        if self.is_project_context:
            fields += ["vcs", "branch", "git_ref"]
        else:
            fields += ["project"]
        return fields + [
            "information_type",
            "store_distro_series",
            "build_source_tarball",
            "build_path",
            "auto_build",
            "auto_build_archive",
            "auto_build_pocket",
            "auto_build_channels",
            "store_upload",
            "store_name",
            "store_channels",
        ]

    @property
    def is_project_context(self):
        return IProduct.providedBy(self.context)

    def setUpFields(self):
        """See `LaunchpadFormView`."""
        super().setUpFields()
        self.form_fields += self.createEnabledProcessors(
            getUtility(IProcessorSet).getAll(),
            "The architectures that this snap package builds for. Some "
            "architectures are restricted and may only be enabled or "
            "disabled by administrators.",
        )

    def setUpWidgets(self):
        """See `LaunchpadFormView`."""
        super().setUpWidgets()
        self.widgets["processors"].widget_class = "processors"
        if self.is_project_context:
            # If we are on Project:+new-snap page, we know which information
            # types the project supports. Let's filter out the ones that are
            # not supported.
            types = getUtility(ISnapSet).getPossibleSnapInformationTypes(
                self.context
            )
            info_type_widget = self.widgets["information_type"]
            info_type_widget.vocabulary = InformationTypeVocabulary(types)
            self.setUpVCSWidgets()

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @property
    def initial_values(self):
        store_name = None
        if self.has_snappy_distro_series and not self.is_project_context:
            # Try to extract Snap store name from snapcraft.yaml file.
            try:
                snapcraft_data = getUtility(ISnapSet).getSnapcraftYaml(
                    self.context, logger=log
                )
            except (
                MissingSnapcraftYaml,
                CannotFetchSnapcraftYaml,
                CannotParseSnapcraftYaml,
            ):
                pass
            else:
                store_name = snapcraft_data.get("name")

        store_series = getUtility(ISnappySeriesSet).getAll().first()
        if store_series.can_infer_distro_series:
            distro_series = None
        elif store_series.preferred_distro_series is not None:
            distro_series = store_series.preferred_distro_series
        else:
            distro_series = store_series.usable_distro_series.first()
        sds_set = getUtility(ISnappyDistroSeriesSet)
        store_distro_series = sds_set.getByBothSeries(
            store_series, distro_series
        )

        return {
            "store_name": store_name,
            "owner": self.user,
            "store_distro_series": store_distro_series,
            "processors": [
                p
                for p in getUtility(IProcessorSet).getAll()
                if p.build_by_default
            ],
            "auto_build_archive": (
                # XXX cjwatson 2019-02-04: In order to support non-Ubuntu
                # bases, we'd need to store this as None and infer it based
                # on the inferred distro series; but this will do for now.
                getUtility(ILaunchpadCelebrities).ubuntu.main_archive
                if distro_series is None
                else distro_series.main_archive
            ),
            "auto_build_pocket": PackagePublishingPocket.UPDATES,
        }

    @property
    def has_snappy_distro_series(self):
        return not getUtility(ISnappyDistroSeriesSet).getAll().is_empty()

    def validate_widgets(self, data, names=None):
        """See `LaunchpadFormView`."""
        if self.widgets.get("vcs") is not None:
            super().validate_widgets(data, ["vcs"])
            self.validateVCSWidgets(SnapAddView, data)
        if self.widgets.get("auto_build") is not None:
            # Set widgets as required or optional depending on the
            # auto_build field.
            super().validate_widgets(data, ["auto_build"])
            auto_build = data.get("auto_build", False)
            self.widgets["auto_build_archive"].context.required = auto_build
            self.widgets["auto_build_pocket"].context.required = auto_build
        if self.widgets.get("store_upload") is not None:
            # Set widgets as required or optional depending on the
            # store_upload field.
            super().validate_widgets(data, ["store_upload"])
            store_upload = data.get("store_upload", False)
            self.widgets["store_name"].context.required = store_upload
            self.widgets["store_channels"].context.required = store_upload
        super().validate_widgets(data, names=names)

    @action("Create snap package", name="create")
    def create_action(self, action, data):
        if IGitRef.providedBy(self.context):
            kwargs = {"git_ref": self.context, "project": data["project"]}
        elif IBranch.providedBy(self.context):
            kwargs = {"branch": self.context, "project": data["project"]}
        elif self.is_project_context:
            if data["vcs"] == VCSType.GIT:
                kwargs = {"git_ref": data["git_ref"]}
            else:
                kwargs = {"branch": data["branch"]}
            kwargs["project"] = self.context
        else:
            raise NotImplementedError("Unknown context for snap creation.")
        if not data.get("auto_build", False):
            data["auto_build_archive"] = None
            data["auto_build_pocket"] = None
        snap = getUtility(ISnapSet).new(
            self.user,
            data["owner"],
            data["store_distro_series"].distro_series,
            data["name"],
            auto_build=data["auto_build"],
            auto_build_archive=data["auto_build_archive"],
            auto_build_pocket=data["auto_build_pocket"],
            auto_build_channels=data["auto_build_channels"],
            information_type=data["information_type"],
            processors=data["processors"],
            build_source_tarball=data["build_source_tarball"],
            build_path=data.get("build_path"),
            store_upload=data["store_upload"],
            store_series=data["store_distro_series"].snappy_series,
            store_name=data["store_name"],
            store_channels=data.get("store_channels"),
            **kwargs,
        )
        if data["store_upload"]:
            self.requestAuthorization(snap)
        else:
            self.next_url = canonical_url(snap)

    def validate(self, data):
        super().validate(data)
        owner = data.get("owner", None)
        name = data.get("name", None)
        if owner and name:
            if getUtility(ISnapSet).exists(owner, name):
                self.setFieldError(
                    "name",
                    "There is already a snap package owned by %s with this "
                    "name." % owner.displayname,
                )
        self.validateInformationType(data)


class BaseSnapEditView(
    SnapAuthorizeMixin,
    SnapInformationTypeMixin,
    SnapFormMixin,
    LaunchpadEditFormView,
):
    schema = ISnapEditSchema
    next_url = None

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    def setUpWidgets(self, context=None):
        """See `LaunchpadFormView`."""
        super().setUpWidgets()
        self.setUpVCSWidgets()

    @property
    def has_snappy_distro_series(self):
        return not getUtility(ISnappyDistroSeriesSet).getAll().is_empty()

    def validate_widgets(self, data, names=None):
        """See `LaunchpadFormView`."""
        if self.widgets.get("vcs") is not None:
            super().validate_widgets(data, ["vcs"])
            self.validateVCSWidgets(BaseSnapEditView, data)
        if self.widgets.get("auto_build") is not None:
            # Set widgets as required or optional depending on the
            # auto_build field.
            super().validate_widgets(data, ["auto_build"])
            auto_build = data.get("auto_build", False)
            self.widgets["auto_build_archive"].context.required = auto_build
            self.widgets["auto_build_pocket"].context.required = auto_build
        if self.widgets.get("store_upload") is not None:
            # Set widgets as required or optional depending on the
            # store_upload field.
            super().validate_widgets(data, ["store_upload"])
            store_upload = data.get("store_upload", False)
            self.widgets["store_name"].context.required = store_upload
            self.widgets["store_channels"].context.required = store_upload
        super().validate_widgets(data, names=names)

    def validate(self, data):
        super().validate(data)
        info_type = data.get("information_type", self.context.information_type)
        editing_info_type = "information_type" in data
        private = info_type in PRIVATE_INFORMATION_TYPES
        if private is False:
            # These are the requirements for public snaps.
            if "information_type" in data or "owner" in data:
                owner = data.get("owner", self.context.owner)
                if owner is not None and owner.private:
                    self.setFieldError(
                        "information_type" if editing_info_type else "owner",
                        "A public snap cannot have a private owner.",
                    )
            if "information_type" in data or "branch" in data:
                branch = data.get("branch", self.context.branch)
                if branch is not None and branch.private:
                    self.setFieldError(
                        "information_type" if editing_info_type else "branch",
                        "A public snap cannot have a private branch.",
                    )
            if "information_type" in data or "git_ref" in data:
                ref = data.get("git_ref", self.context.git_ref)
                if ref is not None and ref.private:
                    self.setFieldError(
                        "information_type" if editing_info_type else "git_ref",
                        "A public snap cannot have a private repository.",
                    )
        self.validateInformationType(data, snap=self.context)

    def _needStoreReauth(self, data):
        """Does this change require reauthorizing to the store?"""
        store_upload = data.get("store_upload", False)
        store_distro_series = data.get("store_distro_series")
        store_name = data.get("store_name")
        if (
            not store_upload
            or store_distro_series is None
            or store_name is None
        ):
            return False
        if not self.context.store_upload:
            return True
        if store_distro_series.snappy_series != self.context.store_series:
            return True
        if store_name != self.context.store_name:
            return True
        return False

    @action("Update snap package", name="update")
    def request_action(self, action, data):
        vcs = data.pop("vcs", None)
        if vcs == VCSType.BZR:
            data["git_ref"] = None
        elif vcs == VCSType.GIT:
            data["branch"] = None
        new_processors = data.get("processors")
        if new_processors is not None:
            if set(self.context.processors) != set(new_processors):
                self.context.setProcessors(
                    new_processors, check_permissions=True, user=self.user
                )
            del data["processors"]
        if not data.get("auto_build", False):
            if "auto_build_archive" in data:
                del data["auto_build_archive"]
            if "auto_build_pocket" in data:
                del data["auto_build_pocket"]
            if "auto_build_channels" in data:
                del data["auto_build_channels"]
        store_upload = data.get("store_upload", False)
        if not store_upload:
            if "store_name" in data:
                del data["store_name"]
            if "store_channels" in data:
                del data["store_channels"]
        need_store_reauth = self._needStoreReauth(data)
        info_type = data.get("information_type")
        if info_type and info_type != self.context.information_type:
            self.context.information_type = info_type
            del data["information_type"]
        self.updateContextFromData(data)
        if need_store_reauth:
            self.requestAuthorization(self.context)
        else:
            self.next_url = canonical_url(self.context)

    @property
    def adapters(self):
        """See `LaunchpadFormView`."""
        return {ISnapEditSchema: self.context}


class SnapAdminView(BaseSnapEditView):
    """View for administering snap packages."""

    @property
    def label(self):
        return "Administer %s snap package" % self.context.name

    page_title = "Administer"

    # XXX pappacena 2021-02-19: Once we have the whole privacy work in
    # place, we should move "project" and "information_type" from +admin
    # page to +edit, to allow common users to edit this.
    @property
    def field_names(self):
        fields = [
            "project",
            "information_type",
            "require_virtualized",
            "allow_internet",
            "pro_enable",
        ]

        return fields

    @property
    def initial_values(self):
        """Set initial values for the form."""
        # XXX pappacena 2021-02-12: Until we back fill information_type
        # database column, it will be NULL, but snap.information_type
        # property has a fallback to check "private" property. This should
        # be removed once we back fill snap.information_type.
        return {"information_type": self.context.information_type}

    def updateContextFromData(self, data, context=None, notify_modified=True):
        if "project" in data:
            project = data.pop("project")
            self.context.setProject(project)
        super().updateContextFromData(data, context, notify_modified)


class SnapEditView(BaseSnapEditView, EnableProcessorsMixin):
    """View for editing snap packages."""

    @property
    def label(self):
        return "Edit %s snap package" % self.context.name

    page_title = "Edit"

    field_names = [
        "owner",
        "name",
        "project",
        "information_type",
        "store_distro_series",
        "vcs",
        "branch",
        "git_ref",
        "build_source_tarball",
        "build_path",
        "auto_build",
        "auto_build_archive",
        "auto_build_pocket",
        "auto_build_channels",
        "store_upload",
        "store_name",
        "store_channels",
        "use_fetch_service",
        "fetch_service_policy",
    ]
    custom_widget_store_distro_series = LaunchpadRadioWidget
    custom_widget_vcs = LaunchpadRadioWidget
    custom_widget_git_ref = CustomWidgetFactory(
        GitRefWidget, allow_external=True
    )
    custom_widget_auto_build_archive = SnapArchiveWidget
    custom_widget_auto_build_pocket = LaunchpadDropdownWidget
    custom_widget_auto_build_channels = HintedSnapBuildChannelsWidget
    custom_widget_store_channels = StoreChannelsWidget
    # See `setUpWidgets` method.
    custom_widget_information_type = CustomWidgetFactory(
        LaunchpadRadioWidgetWithDescription,
        vocabulary=InformationTypeVocabulary(types=[]),
    )

    help_links = {
        "auto_build_pocket": "/+help-snappy/snap-build-pocket.html",
    }

    def setUpFields(self):
        """See `LaunchpadFormView`."""
        super().setUpFields()
        self.form_fields += self.createEnabledProcessors(
            self.context.available_processors,
            "The architectures that this snap package builds for. Some "
            "architectures are restricted and may only be enabled or "
            "disabled by administrators.",
        )

    def setUpWidgets(self, context=None):
        super().setUpWidgets(context)
        info_type_widget = self.widgets["information_type"]
        info_type_widget.vocabulary = InformationTypeVocabulary(
            types=self.getPossibleInformationTypes(self.context, self.user)
        )

    @property
    def initial_values(self):
        initial_values = {}
        if self.context.git_ref is not None:
            initial_values["vcs"] = VCSType.GIT
        else:
            initial_values["vcs"] = VCSType.BZR
        if self.context.auto_build_pocket is None:
            initial_values["auto_build_pocket"] = (
                PackagePublishingPocket.UPDATES
            )
        # XXX pappacena 2021-02-12: Until we back fill information_type
        # database column, it will be NULL, but snap.information_type
        # property has a fallback to check "private" property. This should
        # be removed once we back fill snap.information_type.
        initial_values["information_type"] = self.context.information_type
        if self.context.store_distro_series is None:
            initial_values["store_distro_series"] = SyntheticSnappyDistroSeries(
                None, self.context.distro_series
            )
        return initial_values

    def validate_widgets(self, data, names=None):
        if (
            self.widgets.get("use_fetch_service") is not None
            and self.widgets.get("fetch_service_policy") is not None
        ):
            super().validate_widgets(data, ["use_fetch_service"])
            self.widgets["fetch_service_policy"].context.required = data.get(
                "use_fetch_service"
            )
        super().validate_widgets(data, names=names)

    def validate(self, data):
        super().validate(data)
        owner = data.get("owner", None)
        name = data.get("name", None)
        if owner and name:
            try:
                snap = getUtility(ISnapSet).getByName(owner, name)
                if snap != self.context:
                    self.setFieldError(
                        "name",
                        "There is already a snap package owned by %s with "
                        "this name." % owner.displayname,
                    )
            except NoSuchSnap:
                pass
        if "processors" in data:
            available_processors = set(self.context.available_processors)
            widget = self.widgets["processors"]
            for processor in self.context.processors:
                if processor not in data["processors"]:
                    if processor not in available_processors:
                        # This processor is not currently available for
                        # selection, but is enabled.  Leave it untouched.
                        data["processors"].append(processor)
                    elif processor.name in widget.disabled_items:
                        # This processor is restricted and currently
                        # enabled. Leave it untouched.
                        data["processors"].append(processor)

    def updateContextFromData(self, data, context=None, notify_modified=True):
        if not data.get("use_fetch_service"):
            data["fetch_service_policy"] = FetchServicePolicy.STRICT

        if "project" in data:
            project = data.pop("project")
            self.context.setProject(project)
        super().updateContextFromData(data, context, notify_modified)


class SnapAuthorizeView(LaunchpadEditFormView):
    """View for authorizing snap package uploads to the store."""

    @property
    def label(self):
        return "Authorize store uploads of %s" % self.context.name

    page_title = "Authorize store uploads"

    class schema(Interface):
        """Schema for authorizing snap package uploads to the store."""

        discharge_macaroon = TextLine(
            title="Serialized discharge macaroon", required=True
        )

    render_context = False

    focusedElementScript = None

    @property
    def cancel_url(self):
        return canonical_url(self.context)

    @classmethod
    def requestAuthorization(cls, snap, request):
        """Begin the process of authorizing uploads of a snap package."""
        try:
            sso_caveat_id = snap.beginAuthorization()
        except CannotAuthorizeStoreUploads as e:
            request.response.addInfoNotification(str(e))
            request.response.redirect(canonical_url(snap))
            return
        except SnapNotFoundResponse:
            request.response.addInfoNotification(
                structured(
                    _(
                        "The requested snap name '%(name)s' is not registered "
                        "in the snap store. You can register it at "
                        '<a href="%(register_url)s" target="_blank">'
                        "%(register_url)s</a>"
                    ),
                    name=snap.store_name,
                    register_url="https://snapcraft.io/register-snap",
                )
            )
            request.response.redirect(canonical_url(snap))
            return
        base_url = canonical_url(snap, view_name="+authorize")
        login_url = urlappend(base_url, "+login")
        login_url += "?%s" % urlencode(
            [
                ("macaroon_caveat_id", sso_caveat_id),
                ("discharge_macaroon_action", "field.actions.complete"),
                ("discharge_macaroon_field", "field.discharge_macaroon"),
            ]
        )
        return login_url

    @action("Begin authorization", name="begin")
    def begin_action(self, action, data):
        login_url = self.requestAuthorization(self.context, self.request)
        if login_url is not None:
            self.request.response.redirect(login_url)

    @action("Complete authorization", name="complete")
    def complete_action(self, action, data):
        if not data.get("discharge_macaroon"):
            self.addError(
                structured(
                    _("Uploads of %(snap)s to the store were not authorized."),
                    snap=self.context.name,
                )
            )
            return
        self.context.completeAuthorization(
            discharge_macaroon=data["discharge_macaroon"]
        )
        self.request.response.addInfoNotification(
            structured(
                _("Uploads of %(snap)s to the store are now authorized."),
                snap=self.context.name,
            )
        )
        self.request.response.redirect(canonical_url(self.context))

    @property
    def adapters(self):
        """See `LaunchpadFormView`."""
        return {self.schema: self.context}


class SnapDeleteView(BaseSnapEditView):
    """View for deleting snap packages."""

    @property
    def label(self):
        return "Delete %s snap package" % self.context.name

    page_title = "Delete"

    field_names = []
    next_url = None

    @action("Delete snap package", name="delete")
    def delete_action(self, action, data):
        owner = self.context.owner
        self.context.destroySelf()
        self.next_url = canonical_url(owner, view_name="+snaps")
