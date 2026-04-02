# Copyright 2015-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "get_snap_privacy_filter",
    "Snap",
]

import base64
import typing as t
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from urllib.parse import urlsplit

import six
import yaml
from breezy import urlutils
from lazr.lifecycle.event import ObjectCreatedEvent
from pymacaroons import Macaroon
from storm.expr import (
    SQL,
    And,
    Coalesce,
    Desc,
    Exists,
    Join,
    LeftJoin,
    Not,
    Or,
    Select,
)
from storm.locals import JSON, Bool, DateTime, Int, Reference, Store, Unicode
from zope.component import getAdapter, getUtility
from zope.event import notify
from zope.interface import directlyProvides, implementer
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.browser.tales import ArchiveFormatterAPI, DateTimeFormatterAPI
from lp.app.enums import (
    FREE_INFORMATION_TYPES,
    PRIVATE_INFORMATION_TYPES,
    PUBLIC_INFORMATION_TYPES,
    InformationType,
)
from lp.app.errors import (
    IncompatibleArguments,
    SubscriptionPrivacyViolation,
    UserCannotUnsubscribePerson,
)
from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.app.interfaces.security import IAuthorization
from lp.app.interfaces.services import IService
from lp.buildmaster.builderproxy import FetchServicePolicy
from lp.buildmaster.enums import BuildStatus
from lp.buildmaster.interfaces.buildqueue import IBuildQueueSet
from lp.buildmaster.model.builder import Builder
from lp.buildmaster.model.buildfarmjob import BuildFarmJob
from lp.buildmaster.model.buildqueue import BuildQueue
from lp.buildmaster.model.processor import Processor
from lp.code.errors import (
    BranchFileNotFound,
    BranchHostingFault,
    GitRepositoryBlobNotFound,
    GitRepositoryBlobUnsupportedRemote,
    GitRepositoryScanFault,
)
from lp.code.interfaces.branch import IBranch
from lp.code.interfaces.branchcollection import IAllBranches, IBranchCollection
from lp.code.interfaces.gitcollection import (
    IAllGitRepositories,
    IGitCollection,
)
from lp.code.interfaces.gitref import IGitRef, IGitRefRemoteSet
from lp.code.interfaces.gitrepository import (
    IGitRepository,
    IHasGitRepositoryURL,
)
from lp.code.model.branch import Branch
from lp.code.model.branchcollection import GenericBranchCollection
from lp.code.model.branchnamespace import (
    BRANCH_POLICY_ALLOWED_TYPES,
    BRANCH_POLICY_REQUIRED_GRANTS,
)
from lp.code.model.gitcollection import GenericGitCollection
from lp.code.model.gitrepository import GitRepository
from lp.code.model.reciperegistry import recipe_registry
from lp.registry.errors import PrivatePersonLinkageError
from lp.registry.interfaces.accesspolicy import (
    IAccessArtifactGrantSource,
    IAccessArtifactSource,
)
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.person import (
    IPerson,
    IPersonSet,
    validate_public_person,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.interfaces.product import IProduct
from lp.registry.interfaces.role import IHasOwner, IPersonRoles
from lp.registry.model.accesspolicy import (
    AccessPolicyGrant,
    reconcile_access_for_artifacts,
)
from lp.registry.model.distroseries import DistroSeries
from lp.registry.model.person import Person
from lp.registry.model.series import ACTIVE_STATUSES
from lp.registry.model.teammembership import TeamParticipation
from lp.services.config import config
from lp.services.crypto.interfaces import IEncryptedContainer
from lp.services.crypto.model import NaClEncryptedContainerBase
from lp.services.database.bulk import load_related
from lp.services.database.constants import DEFAULT, UTC_NOW
from lp.services.database.decoratedresultset import DecoratedResultSet
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.database.stormexpr import (
    Array,
    ArrayAgg,
    ArrayIntersects,
    Greatest,
    IsDistinctFrom,
    NullsLast,
)
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.model.job import Job
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.openid.adapters.openid import CurrentOpenIDEndPoint
from lp.services.propertycache import cachedproperty, get_property_cache
from lp.services.webapp.authorization import precache_permission_for_objects
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.publisher import canonical_url
from lp.services.webhooks.interfaces import IWebhookSet
from lp.services.webhooks.model import WebhookTargetMixin
from lp.snappy.adapters.buildarch import determine_architectures_to_build
from lp.snappy.interfaces.snap import (
    BadMacaroon,
    BadSnapSearchContext,
    BadSnapSource,
    CannotAuthorizeStoreUploads,
    CannotFetchSnapcraftYaml,
    CannotModifySnapProcessor,
    CannotParseSnapcraftYaml,
    CannotRequestAutoBuilds,
    DuplicateSnapName,
    ISnap,
    ISnapBuildRequest,
    ISnapSet,
    MissingSnapcraftYaml,
    NoSourceForSnap,
    NoSuchSnap,
    SnapAuthorizationBadGeneratedMacaroon,
    SnapBuildAlreadyPending,
    SnapBuildArchiveOwnerMismatch,
    SnapBuildDisallowedArchitecture,
    SnapBuildRequestStatus,
    SnapNotOwner,
    SnapPrivacyMismatch,
)
from lp.snappy.interfaces.snapbase import ISnapBaseSet, NoSuchSnapBase
from lp.snappy.interfaces.snapbuild import ISnapBuild, ISnapBuildSet
from lp.snappy.interfaces.snapjob import ISnapRequestBuildsJobSource
from lp.snappy.interfaces.snappyseries import ISnappyDistroSeriesSet
from lp.snappy.interfaces.snapstoreclient import ISnapStoreClient
from lp.snappy.model.snapbase import SnapBase
from lp.snappy.model.snapbuild import SnapBuild
from lp.snappy.model.snapjob import SnapJob
from lp.snappy.model.snapsubscription import SnapSubscription
from lp.soyuz.interfaces.archive import ArchiveDisabled
from lp.soyuz.interfaces.distroarchseries import IDistroArchSeries
from lp.soyuz.model.archive import Archive, get_enabled_archive_filter
from lp.soyuz.model.distroarchseries import DistroArchSeries


def snap_modified(snap, event):
    """Update the date_last_modified property when a Snap is modified.

    This method is registered as a subscriber to `IObjectModifiedEvent`
    events on snap packages.
    """
    removeSecurityProxy(snap).date_last_modified = UTC_NOW


def user_has_special_snap_access(user):
    """Admins have special access.

    :param user: An `IPerson` or None.
    """
    if user is None:
        return False
    roles = IPersonRoles(user)
    return roles.in_admin


@implementer(ISnapBuildRequest)
class SnapBuildRequest:
    """See `ISnapBuildRequest`.

    This is not directly backed by a database table; instead, it is a
    webservice-friendly view of an asynchronous build request.
    """

    def __init__(self, snap, id):
        self.snap = snap
        self.id = id

    @classmethod
    def fromJob(cls, job):
        """See `ISnapBuildRequest`."""
        request = cls(job.snap, job.job_id)
        get_property_cache(request)._job = job
        return request

    @cachedproperty
    def _job(self):
        job_source = getUtility(ISnapRequestBuildsJobSource)
        return job_source.getBySnapAndID(self.snap, self.id)

    @property
    def date_requested(self):
        """See `ISnapBuildRequest`."""
        return self._job.date_created

    @property
    def date_finished(self):
        """See `ISnapBuildRequest`."""
        return self._job.date_finished

    @property
    def status(self):
        """See `ISnapBuildRequest`."""
        status_map = {
            JobStatus.WAITING: SnapBuildRequestStatus.PENDING,
            JobStatus.RUNNING: SnapBuildRequestStatus.PENDING,
            JobStatus.COMPLETED: SnapBuildRequestStatus.COMPLETED,
            JobStatus.FAILED: SnapBuildRequestStatus.FAILED,
            JobStatus.SUSPENDED: SnapBuildRequestStatus.PENDING,
        }
        return status_map[self._job.job.status]

    @property
    def error_message(self):
        """See `ISnapBuildRequest`."""
        return self._job.error_message

    @property
    def builds(self):
        """See `ISnapBuildRequest`."""
        return self._job.builds

    @property
    def requester(self):
        """See `ISnapBuildRequest`."""
        return self._job.requester

    @property
    def archive(self):
        """See `ISnapBuildRequest`."""
        return self._job.archive

    @property
    def pocket(self):
        """See `ISnapBuildRequest`."""
        return self._job.pocket

    @property
    def channels(self):
        """See `ISnapBuildRequest`."""
        return self._job.channels

    @property
    def architectures(self):
        """See `ISnapBuildRequest`."""
        return self._job.architectures


@implementer(ISnap, IHasOwner)
class Snap(StormBase, WebhookTargetMixin):
    """See `ISnap`."""

    __storm_table__ = "Snap"

    id = Int(primary=True)

    date_created = DateTime(
        name="date_created", tzinfo=timezone.utc, allow_none=False
    )
    date_last_modified = DateTime(
        name="date_last_modified", tzinfo=timezone.utc, allow_none=False
    )

    registrant_id = Int(name="registrant", allow_none=False)
    registrant = Reference(registrant_id, "Person.id")

    def _validate_owner(self, attr, value):
        if not self.private:
            try:
                validate_public_person(self, attr, value)
            except PrivatePersonLinkageError:
                raise SnapPrivacyMismatch(
                    "A public snap cannot have a private owner."
                )
        return value

    owner_id = Int(name="owner", allow_none=False, validator=_validate_owner)
    owner = Reference(owner_id, "Person.id")

    project_id = Int(name="project", allow_none=True)
    project = Reference(project_id, "Product.id")

    distro_series_id = Int(name="distro_series", allow_none=True)
    distro_series = Reference(distro_series_id, "DistroSeries.id")

    name = Unicode(name="name", allow_none=False)

    description = Unicode(name="description", allow_none=True)

    def _validate_branch(self, attr, value):
        if not self.private and value is not None:
            if IStore(Branch).get(Branch, value).private:
                raise SnapPrivacyMismatch(
                    "A public snap cannot have a private branch."
                )
        return value

    branch_id = Int(name="branch", allow_none=True, validator=_validate_branch)
    branch = Reference(branch_id, "Branch.id")

    def _validate_git_repository(self, attr, value):
        if not self.private and value is not None:
            if IStore(GitRepository).get(GitRepository, value).private:
                raise SnapPrivacyMismatch(
                    "A public snap cannot have a private repository."
                )
        return value

    git_repository_id = Int(
        name="git_repository",
        allow_none=True,
        validator=_validate_git_repository,
    )
    git_repository = Reference(git_repository_id, "GitRepository.id")

    git_repository_url = Unicode(name="git_repository_url", allow_none=True)

    git_path = Unicode(name="git_path", allow_none=True)

    build_path = Unicode(name="build_path", allow_none=True)

    auto_build = Bool(name="auto_build", allow_none=False)

    auto_build_archive_id = Int(name="auto_build_archive", allow_none=True)
    auto_build_archive = Reference(auto_build_archive_id, "Archive.id")

    auto_build_pocket = DBEnum(enum=PackagePublishingPocket, allow_none=True)

    auto_build_channels = JSON("auto_build_channels", allow_none=True)

    is_stale = Bool(name="is_stale", allow_none=False)

    require_virtualized = Bool(name="require_virtualized")

    _private = Bool(name="private")

    def _valid_information_type(self, attr, value):
        if not getUtility(ISnapSet).isValidInformationType(
            value, self.owner, self.branch, self.git_ref
        ):
            raise SnapPrivacyMismatch
        return value

    _information_type = DBEnum(
        enum=InformationType,
        default=InformationType.PUBLIC,
        name="information_type",
        validator=_valid_information_type,
    )

    allow_internet = Bool(name="allow_internet", allow_none=False)

    build_source_tarball = Bool(name="build_source_tarball", allow_none=False)

    store_upload = Bool(name="store_upload", allow_none=False)

    store_series_id = Int(name="store_series", allow_none=True)
    store_series = Reference(store_series_id, "SnappySeries.id")

    store_name = Unicode(name="store_name", allow_none=True)

    store_secrets = JSON("store_secrets", allow_none=True)

    _store_channels = JSON("store_channels", allow_none=True)

    pro_enable = Bool(name="pro_enable", allow_none=False)

    use_fetch_service = Bool(name="use_fetch_service", allow_none=False)

    fetch_service_policy = DBEnum(
        enum=FetchServicePolicy,
        default=FetchServicePolicy.STRICT,
        name="fetch_service_policy",
        allow_none=True,
    )

    def __init__(
        self,
        registrant,
        owner,
        distro_series,
        name,
        description=None,
        branch=None,
        git_ref=None,
        build_path=None,
        auto_build=False,
        auto_build_archive=None,
        auto_build_pocket=None,
        auto_build_channels=None,
        require_virtualized=True,
        date_created=DEFAULT,
        information_type=InformationType.PUBLIC,
        allow_internet=True,
        build_source_tarball=False,
        store_upload=False,
        store_series=None,
        store_name=None,
        store_secrets=None,
        store_channels=None,
        project=None,
        pro_enable=False,
        use_fetch_service=False,
        fetch_service_policy=FetchServicePolicy.STRICT,
    ):
        """Construct a `Snap`."""
        super().__init__()

        # Set the information type first so that other validators can perform
        # suitable privacy checks, but pillar should also be set, since it's
        # mandatory for private snaps.
        # Note that we set self._information_type (not self.information_type)
        # to avoid the call to self._reconcileAccess() while building the
        # Snap instance.
        self.project = project
        self._information_type = information_type

        self.registrant = registrant
        self.owner = owner
        self.distro_series = distro_series
        self.name = name
        self.description = description
        self.branch = branch
        self.git_ref = git_ref
        self.build_path = build_path
        self.auto_build = auto_build
        self.auto_build_archive = auto_build_archive
        self.auto_build_pocket = auto_build_pocket
        self.auto_build_channels = auto_build_channels
        self.require_virtualized = require_virtualized
        self.date_created = date_created
        self.date_last_modified = date_created
        self.allow_internet = allow_internet
        self.build_source_tarball = build_source_tarball
        self.store_upload = store_upload
        self.store_series = store_series
        self.store_name = store_name
        self.store_secrets = store_secrets
        self.store_channels = store_channels
        self.pro_enable = pro_enable
        self.use_fetch_service = use_fetch_service
        self.fetch_service_policy = fetch_service_policy

    def __repr__(self):
        return "<Snap ~%s/+snap/%s>" % (self.owner.name, self.name)

    @property
    def information_type(self):
        if self._information_type is None:
            return (
                InformationType.PROPRIETARY
                if self._private
                else InformationType.PUBLIC
            )
        return self._information_type

    @information_type.setter
    def information_type(self, information_type):
        self._information_type = information_type
        self._reconcileAccess()

    @property
    def private(self):
        return self.information_type not in PUBLIC_INFORMATION_TYPES

    @property
    def valid_webhook_event_types(self):
        return ["snap:build:0.1"]

    @property
    def _api_git_path(self):
        return self.git_path

    @_api_git_path.setter
    def _api_git_path(self, value):
        if self.git_repository is None and self.git_repository_url is None:
            raise BadSnapSource(
                "git_path may only be set on a Git-based snap."
            )
        if value is None:
            raise BadSnapSource("git_path may not be set to None.")
        self.git_path = value

    @property
    def git_ref(self):
        """See `ISnap`."""
        if self.git_repository is not None:
            return self.git_repository.getRefByPath(self.git_path)
        elif self.git_repository_url is not None:
            return getUtility(IGitRefRemoteSet).new(
                self.git_repository_url, self.git_path
            )
        else:
            return None

    @git_ref.setter
    def git_ref(self, value):
        """See `ISnap`."""
        if value is not None:
            self.git_repository = value.repository
            self.git_repository_url = value.repository_url
            self.git_path = value.path
        else:
            self.git_repository = None
            self.git_repository_url = None
            self.git_path = None
        if self.git_repository_url is not None:
            directlyProvides(self, IHasGitRepositoryURL)
        else:
            directlyProvides(self)

    @property
    def source(self):
        if self.branch is not None:
            return self.branch
        elif self.git_ref is not None:
            return self.git_ref
        else:
            return None

    @property
    def pillar(self):
        """See `ISnap`."""
        return self.project

    @pillar.setter
    def pillar(self, pillar):
        if pillar is None:
            self.project = None
        elif IProduct.providedBy(pillar):
            self.project = pillar
        else:
            raise ValueError(
                "The pillar of a Snap must be an IProduct instance."
            )

    @property
    def available_processors(self):
        """See `ISnap`."""
        clauses = [Processor.id == DistroArchSeries.processor_id]
        if self.distro_series is not None:
            clauses.append(
                DistroArchSeries.id.is_in(
                    self.distro_series.enabled_architectures.get_select_expr(
                        DistroArchSeries.id
                    )
                )
            )
        else:
            # We don't know the series until we've looked at snapcraft.yaml
            # to dispatch a build, so enabled architectures for any active
            # series will do.
            clauses.extend(
                [
                    DistroArchSeries.enabled,
                    DistroArchSeries.distroseries == DistroSeries.id,
                    DistroSeries.status.is_in(ACTIVE_STATUSES),
                ]
            )
        return Store.of(self).find(Processor, *clauses).config(distinct=True)

    def _getProcessors(self):
        return list(
            Store.of(self).find(
                Processor,
                Processor.id == SnapArch.processor_id,
                SnapArch.snap == self,
            )
        )

    def setProcessors(self, processors, check_permissions=False, user=None):
        """See `ISnap`."""
        if check_permissions:
            can_modify = None
            if user is not None:
                roles = IPersonRoles(user)
                authz = lambda perm: getAdapter(self, IAuthorization, perm)
                if authz("launchpad.Admin").checkAuthenticated(roles):
                    can_modify = lambda proc: True
                elif authz("launchpad.Edit").checkAuthenticated(roles):
                    can_modify = lambda proc: not proc.restricted
            if can_modify is None:
                raise Unauthorized(
                    "Permission launchpad.Admin or launchpad.Edit required "
                    "on %s." % self
                )
        else:
            can_modify = lambda proc: True

        enablements = dict(
            Store.of(self).find(
                (Processor, SnapArch),
                Processor.id == SnapArch.processor_id,
                SnapArch.snap == self,
            )
        )
        for proc in enablements:
            if proc not in processors:
                if not can_modify(proc):
                    raise CannotModifySnapProcessor(proc)
                Store.of(self).remove(enablements[proc])
        for proc in processors:
            if proc not in self.processors:
                if not can_modify(proc):
                    raise CannotModifySnapProcessor(proc)
                snaparch = SnapArch()
                snaparch.snap = self
                snaparch.processor = proc
                Store.of(self).add(snaparch)

    processors = property(_getProcessors, setProcessors)

    def _isBuildableArchitectureAllowed(self, das, snap_base=None):
        """Check whether we may build for a buildable `DistroArchSeries`.

        The caller is assumed to have already checked that a suitable chroot
        is available (either directly or via
        `DistroSeries.buildable_architectures`).
        """
        return (
            das.enabled
            and das.processor in self.processors
            and (
                das.processor.supports_virtualized
                or not self.require_virtualized
            )
            and (snap_base is None or das.processor in snap_base.processors)
        )

    def _isArchitectureAllowed(self, das, pocket, snap_base=None):
        return das.getChroot(
            pocket=pocket
        ) is not None and self._isBuildableArchitectureAllowed(
            das, snap_base=snap_base
        )

    def getAllowedArchitectures(
        self, distro_series: t.Optional[IDistroSeries] = None, snap_base=None
    ) -> t.List[IDistroArchSeries]:
        """See `ISnap`."""
        if distro_series is None:
            distro_series = self.distro_series
        return [
            das
            for das in distro_series.buildable_architectures
            if self._isBuildableArchitectureAllowed(das, snap_base=snap_base)
        ]

    @property
    def store_distro_series(self):
        if self.store_series is None:
            return None
        return getUtility(ISnappyDistroSeriesSet).getByBothSeries(
            self.store_series, self.distro_series
        )

    @store_distro_series.setter
    def store_distro_series(self, value):
        self.distro_series = value.distro_series
        self.store_series = value.snappy_series

    @property
    def store_channels(self):
        return self._store_channels or []

    @store_channels.setter
    def store_channels(self, value):
        self._store_channels = value or None

    def getAllowedInformationTypes(self, user):
        """See `ISnap`."""
        if user_has_special_snap_access(user):
            # Admins can set any type.
            return set(PUBLIC_INFORMATION_TYPES + PRIVATE_INFORMATION_TYPES)
        if self.pillar is None:
            return set(FREE_INFORMATION_TYPES)
        required_grant = BRANCH_POLICY_REQUIRED_GRANTS[
            self.project.branch_sharing_policy
        ]
        if (
            required_grant is not None
            and not getUtility(IService, "sharing").checkPillarAccess(
                [self.project], required_grant, self.owner
            )
            and (
                user is None
                or not getUtility(IService, "sharing").checkPillarAccess(
                    [self.project], required_grant, user
                )
            )
        ):
            return []
        return BRANCH_POLICY_ALLOWED_TYPES[self.project.branch_sharing_policy]

    @staticmethod
    def extractSSOCaveats(macaroon):
        locations = [
            urlsplit(root).netloc
            for root in CurrentOpenIDEndPoint.getAllRootURLs()
        ]
        return [
            c
            for c in macaroon.third_party_caveats()
            if c.location in locations
        ]

    def beginAuthorization(self):
        """See `ISnap`."""
        if self.store_series is None:
            raise CannotAuthorizeStoreUploads(
                "Cannot authorize uploads of a snap package with no store "
                "series."
            )
        if self.store_name is None:
            raise CannotAuthorizeStoreUploads(
                "Cannot authorize uploads of a snap package with no store "
                "name."
            )
        snap_store_client = getUtility(ISnapStoreClient)
        root_macaroon_raw = snap_store_client.requestPackageUploadPermission(
            self.store_series, self.store_name
        )
        sso_caveats = self.extractSSOCaveats(
            Macaroon.deserialize(root_macaroon_raw)
        )
        # We must have exactly one SSO caveat; more than one should never be
        # required and could be an attempt to substitute weaker caveats.  We
        # might as well OOPS here, even though the cause of this is probably
        # in some other service, since the user can't do anything about it
        # and it should show up in our OOPS reports.
        if not sso_caveats:
            raise SnapAuthorizationBadGeneratedMacaroon(
                "Macaroon has no SSO caveats"
            )
        elif len(sso_caveats) > 1:
            raise SnapAuthorizationBadGeneratedMacaroon(
                "Macaroon has multiple SSO caveats"
            )
        self.store_secrets = {"root": root_macaroon_raw}
        return sso_caveats[0].caveat_id

    def completeAuthorization(
        self, root_macaroon=None, discharge_macaroon=None
    ):
        """See `ISnap`."""
        if root_macaroon is not None:
            try:
                Macaroon.deserialize(root_macaroon)
            except Exception:
                raise BadMacaroon("root_macaroon is invalid.")
            self.store_secrets = {"root": root_macaroon}
        else:
            if self.store_secrets is None or "root" not in self.store_secrets:
                raise CannotAuthorizeStoreUploads(
                    "beginAuthorization must be called before "
                    "completeAuthorization."
                )
        if discharge_macaroon is not None:
            try:
                Macaroon.deserialize(discharge_macaroon)
            except Exception:
                raise BadMacaroon("discharge_macaroon is invalid.")
            container = getUtility(IEncryptedContainer, "snap-store-secrets")
            if container.can_encrypt:
                self.store_secrets["discharge_encrypted"] = (
                    removeSecurityProxy(
                        container.encrypt(discharge_macaroon.encode("UTF-8"))
                    )
                )
                self.store_secrets.pop("discharge", None)
            else:
                self.store_secrets["discharge"] = discharge_macaroon
                self.store_secrets.pop("discharge_encrypted", None)
        else:
            self.store_secrets.pop("discharge", None)
            self.store_secrets.pop("discharge_encrypted", None)

    @property
    def can_upload_to_store(self):
        if (
            config.snappy.store_upload_url is None
            or config.snappy.store_url is None
            or self.store_series is None
            or self.store_name is None
            or self.store_secrets is None
            or "root" not in self.store_secrets
        ):
            return False
        root_macaroon = Macaroon.deserialize(self.store_secrets["root"])
        if (
            self.extractSSOCaveats(root_macaroon)
            and "discharge" not in self.store_secrets
            and "discharge_encrypted" not in self.store_secrets
        ):
            return False
        return True

    def _checkRequestBuild(self, requester, archive):
        """May `requester` request builds of this snap from `archive`?"""
        if not requester.inTeam(self.owner):
            raise SnapNotOwner(
                "%s cannot create snap package builds owned by %s."
                % (requester.displayname, self.owner.displayname)
            )
        if not archive.enabled:
            raise ArchiveDisabled(archive.displayname)
        if archive.private and self.owner != archive.owner:
            # See rationale in `SnapBuildArchiveOwnerMismatch` docstring.
            raise SnapBuildArchiveOwnerMismatch()

    def requestBuild(
        self,
        requester,
        archive,
        distro_arch_series,
        pocket,
        snap_base=None,
        channels=None,
        build_request=None,
        target_architectures: t.Optional[t.List[str]] = None,
        craft_platform: str = None,
    ) -> ISnapBuild:
        """See `ISnap`."""
        self._checkRequestBuild(requester, archive)
        if not self._isArchitectureAllowed(
            distro_arch_series, pocket, snap_base=snap_base
        ):
            raise SnapBuildDisallowedArchitecture(distro_arch_series, pocket)

        if target_architectures:
            target_architectures = sorted(target_architectures)

        if not channels:
            channels_clause = Or(
                SnapBuild.channels == None, SnapBuild.channels == {}
            )
        else:
            channels_clause = SnapBuild.channels == channels
        pending = IStore(self).find(
            SnapBuild,
            SnapBuild.snap_id == self.id,
            SnapBuild.archive_id == archive.id,
            SnapBuild.distro_arch_series_id == distro_arch_series.id,
            SnapBuild.pocket == pocket,
            SnapBuild.target_architectures == target_architectures,
            channels_clause,
            SnapBuild.status == BuildStatus.NEEDSBUILD,
            SnapBuild.craft_platform == craft_platform,
        )
        if pending.any() is not None:
            raise SnapBuildAlreadyPending

        build = getUtility(ISnapBuildSet).new(
            requester,
            self,
            archive,
            distro_arch_series,
            pocket,
            snap_base=snap_base,
            channels=channels,
            build_request=build_request,
            target_architectures=target_architectures,
            craft_platform=craft_platform,
        )
        build.queueBuild()
        notify(ObjectCreatedEvent(build, user=requester))
        return build

    def requestBuilds(
        self, requester, archive, pocket, channels=None, architectures=None
    ):
        """See `ISnap`."""
        self._checkRequestBuild(requester, archive)
        job = getUtility(ISnapRequestBuildsJobSource).create(
            self,
            requester,
            archive,
            pocket,
            channels,
            architectures=architectures,
        )
        return self.getBuildRequest(job.job_id)

    @staticmethod
    def _findBase(
        snapcraft_data: t.Dict[str, t.Any]
    ) -> t.Tuple[SnapBase, t.Optional[str]]:
        """Find a suitable base for a build."""
        base = snapcraft_data.get("base")
        build_base = snapcraft_data.get("build-base")
        name = snapcraft_data.get("name")
        snap_type = snapcraft_data.get("type")

        # Keep this in sync with
        # snapcraft.internal.meta.snap.Snap.get_build_base.
        snap_base_set = getUtility(ISnapBaseSet)
        if build_base is not None:
            snap_base_name = build_base
        elif name is not None and snap_type == "base":
            snap_base_name = name
        else:
            snap_base_name = base

        if isinstance(snap_base_name, bytes):
            snap_base_name = snap_base_name.decode("UTF-8")
        if snap_base_name is not None:
            return snap_base_set.getByName(snap_base_name), snap_base_name
        else:
            return snap_base_set.getDefault(), None

    def _pickDistroSeries(self, snap_base, snap_base_name):
        """Pick a suitable `IDistroSeries` for a build."""
        if snap_base is not None:
            return self.distro_series or snap_base.distro_series
        elif self.distro_series is None:
            # A base is mandatory if there's no configured distro series.
            raise NoSuchSnapBase(
                snap_base_name if snap_base_name is not None else "<default>"
            )
        else:
            return self.distro_series

    def _pickChannels(self, snap_base, channels=None):
        """Pick suitable snap channels for a build."""
        if snap_base is not None:
            new_channels = dict(snap_base.build_channels)
            if channels is not None:
                new_channels.update(channels)
            return new_channels
        else:
            return channels

    def requestBuildsFromJob(
        self,
        requester,
        archive,
        pocket,
        channels=None,
        architectures=None,
        allow_failures=False,
        fetch_snapcraft_yaml=True,
        build_request=None,
        logger=None,
    ):
        """See `ISnap`."""
        if not fetch_snapcraft_yaml and self.distro_series is None:
            # Slightly misleading, but requestAutoBuilds is the only place
            # this can happen right now and it raises a more specific error.
            raise CannotRequestAutoBuilds("distro_series")
        try:
            if fetch_snapcraft_yaml:
                try:
                    snapcraft_data = removeSecurityProxy(
                        getUtility(ISnapSet).getSnapcraftYaml(self)
                    )
                except CannotFetchSnapcraftYaml as e:
                    if not e.unsupported_remote:
                        raise
                    # The only reason we can't fetch the file is because we
                    # don't support fetching from this repository's host.
                    # In this case the best thing is to fall back to
                    # building for all supported architectures.
                    snapcraft_data = {}
            else:
                snapcraft_data = {}

            # Find a suitable SnapBase, and combine it with other
            # configuration to find a suitable distro series and suitable
            # channels.
            snap_base, snap_base_name = self._findBase(snapcraft_data)
            distro_series = self._pickDistroSeries(snap_base, snap_base_name)
            channels = self._pickChannels(snap_base, channels=channels)
            if channels is not None:
                channels_by_arch = channels.pop("_byarch", {})
            else:
                channels_by_arch = {}

            # Sort by Processor.id for determinism.  This is chosen to be
            # the same order as in BinaryPackageBuildSet.createForSource, to
            # minimise confusion.
            supported_arches = OrderedDict(
                (das.architecturetag, das)
                for das in sorted(
                    self.getAllowedArchitectures(
                        distro_series, snap_base=snap_base
                    ),
                    key=attrgetter("processor.id"),
                )
                if (
                    architectures is None
                    or das.architecturetag in architectures
                )
            )
            architectures_to_build = determine_architectures_to_build(
                snap_base,
                snapcraft_data,
                list(supported_arches.keys()),
            )
        except Exception as e:
            if not allow_failures:
                raise
            elif logger is not None:
                logger.exception(" - %s/%s: %s", self.owner.name, self.name, e)
            return []

        builds = []
        for build_instance in architectures_to_build:
            arch = build_instance.architecture
            if channels is not None:
                arch_channels = dict(channels)
                if arch in channels_by_arch:
                    arch_channels.update(channels_by_arch[arch])
            else:
                arch_channels = None
            try:
                build = self.requestBuild(
                    requester,
                    archive,
                    supported_arches[arch],
                    pocket,
                    snap_base=snap_base,
                    channels=arch_channels,
                    build_request=build_request,
                    target_architectures=build_instance.target_architectures,
                    craft_platform=build_instance.platform_name,
                )
                if logger is not None:
                    logger.debug(
                        " - %s/%s/%s/%s: Build requested.",
                        self.owner.name,
                        self.name,
                        arch,
                        build_instance.platform_name,
                    )
                builds.append(build)
            except SnapBuildAlreadyPending:
                pass
            except Exception as e:
                if not allow_failures:
                    raise
                elif logger is not None:
                    logger.exception(
                        " - %s/%s/%s/%s: %s",
                        self.owner.name,
                        self.name,
                        arch,
                        build_instance.platform_name,
                        e,
                    )
        return builds

    def requestAutoBuilds(
        self, allow_failures=False, fetch_snapcraft_yaml=False, logger=None
    ):
        """See `ISnap`."""
        if self.auto_build_archive is None:
            raise CannotRequestAutoBuilds("auto_build_archive")
        if self.auto_build_pocket is None:
            raise CannotRequestAutoBuilds("auto_build_pocket")
        if not fetch_snapcraft_yaml and self.distro_series is None:
            raise IncompatibleArguments(
                "Cannot use requestAutoBuilds for a snap package without "
                "inferring from snapcraft.yaml or distro_series being set.  "
                "Consider using requestBuilds instead."
            )
        self.is_stale = False
        if logger is not None:
            logger.debug(
                "Scheduling builds of snap package %s/%s",
                self.owner.name,
                self.name,
            )
        return self.requestBuildsFromJob(
            self.owner,
            self.auto_build_archive,
            self.auto_build_pocket,
            channels=self.auto_build_channels,
            allow_failures=allow_failures,
            fetch_snapcraft_yaml=fetch_snapcraft_yaml,
            logger=logger,
        )

    def getBuildRequest(self, job_id):
        """See `ISnap`."""
        return SnapBuildRequest(self, job_id)

    @property
    def pending_build_requests(self):
        """See `ISnap`."""
        job_source = getUtility(ISnapRequestBuildsJobSource)
        # The returned jobs are ordered by descending ID.
        jobs = job_source.findBySnap(
            self, statuses=(JobStatus.WAITING, JobStatus.RUNNING)
        )
        return DecoratedResultSet(
            jobs, result_decorator=SnapBuildRequest.fromJob
        )

    @property
    def failed_build_requests(self):
        """See `ISnap`."""
        job_source = getUtility(ISnapRequestBuildsJobSource)
        # The returned jobs are ordered by descending ID.
        jobs = job_source.findBySnap(self, statuses=(JobStatus.FAILED,))
        return DecoratedResultSet(
            jobs, result_decorator=SnapBuildRequest.fromJob
        )

    def _getBuilds(self, filter_term, order_by):
        """The actual query to get the builds."""
        query_args = [
            SnapBuild.snap == self,
            SnapBuild.archive_id == Archive.id,
            Archive._enabled,
            get_enabled_archive_filter(
                getUtility(ILaunchBag).user,
                include_public=True,
                include_subscribed=True,
            ),
        ]
        if filter_term is not None:
            query_args.append(filter_term)
        result = Store.of(self).find(SnapBuild, *query_args)
        result.order_by(order_by)

        def eager_load(rows):
            getUtility(ISnapBuildSet).preloadBuildsData(rows)
            getUtility(IBuildQueueSet).preloadForBuildFarmJobs(rows)
            load_related(Builder, rows, ["builder_id"])

        return DecoratedResultSet(result, pre_iter_hook=eager_load)

    def getBuildSummariesForSnapBuildIds(self, snap_build_ids):
        """See `ISnap`."""
        result = {}
        if snap_build_ids is None:
            return result
        filter_term = SnapBuild.id.is_in(snap_build_ids)
        order_by = Desc(SnapBuild.id)
        builds = self._getBuilds(filter_term, order_by)

        # The user can obviously see this snap, and Snap._getBuilds ensures
        # that they can see the relevant archive for each build as well.
        precache_permission_for_objects(None, "launchpad.View", builds)

        # Prefetch data to keep DB query count constant
        lfas = load_related(LibraryFileAlias, builds, ["log_id"])
        load_related(LibraryFileContent, lfas, ["content_id"])

        for build in builds:
            if build.date is not None:
                when_complete = DateTimeFormatterAPI(build.date).displaydate()
            else:
                when_complete = None

            if build.log:
                build_log_size = build.log.content.filesize
            else:
                build_log_size = None

            result[build.id] = {
                "status": build.status.name,
                "buildstate": build.status.title,
                "when_complete": when_complete,
                "when_complete_estimate": build.estimate,
                "build_log_url": build.log_url,
                "build_log_size": build_log_size,
            }
        return result

    def getBuildSummaries(self, request_ids=None, build_ids=None, user=None):
        """See `ISnap`."""
        all_build_ids = []
        result = {"requests": {}, "builds": {}}

        if request_ids:
            job_source = getUtility(ISnapRequestBuildsJobSource)
            jobs = job_source.findBySnap(self, job_ids=request_ids)
            requests = [SnapBuildRequest.fromJob(job) for job in jobs]
            builds_by_request = job_source.findBuildsForJobs(jobs, user=user)
            for builds in builds_by_request.values():
                # It's safe to remove the proxy here, because the IDs will
                # go through Snap._getBuilds which checks visibility.  This
                # saves an Archive query per build in the security adapter.
                all_build_ids.extend(
                    [removeSecurityProxy(build).id for build in builds]
                )
        else:
            requests = []

        if build_ids:
            all_build_ids.extend(build_ids)

        all_build_summaries = self.getBuildSummariesForSnapBuildIds(
            all_build_ids
        )

        for request in requests:
            build_summaries = []
            for build in sorted(
                builds_by_request[request.id],
                key=attrgetter("id"),
                reverse=True,
            ):
                if build.id in all_build_summaries:
                    # Include enough information for
                    # snap.update_build_statuses.js to populate new build
                    # rows.
                    build_summary = {
                        "self_link": canonical_url(
                            build, path_only_if_possible=True
                        ),
                        "id": build.id,
                        "distro_arch_series_link": canonical_url(
                            build.distro_arch_series,
                            path_only_if_possible=True,
                        ),
                        "architecture_tag": (
                            build.distro_arch_series.architecturetag
                        ),
                        "archive_link": ArchiveFormatterAPI(
                            build.archive
                        ).link(None),
                    }
                    build_summary.update(all_build_summaries[build.id])
                    build_summaries.append(build_summary)
            result["requests"][request.id] = {
                "status": request.status.name,
                "error_message": request.error_message,
                "builds": build_summaries,
            }

        for build_id in build_ids or []:
            if build_id in all_build_summaries:
                result["builds"][build_id] = all_build_summaries[build_id]

        return result

    def getBuildByStoreRevision(self, store_upload_revision, user=None):
        build = (
            Store.of(self)
            .find(
                SnapBuild,
                SnapBuild.snap == self,
                SnapBuild.store_upload_revision == store_upload_revision,
                SnapBuild.archive == Archive.id,
                Archive._enabled,
                get_enabled_archive_filter(
                    user, include_public=True, include_subscribed=True
                ),
            )
            .one()
        )
        return build

    @property
    def builds(self):
        """See `ISnap`."""
        order_by = (
            NullsLast(
                Desc(Greatest(SnapBuild.date_started, SnapBuild.date_finished))
            ),
            Desc(SnapBuild.date_created),
            Desc(SnapBuild.id),
        )
        return self._getBuilds(None, order_by)

    @property
    def _pending_states(self):
        """All the build states we consider pending (non-final)."""
        return [
            BuildStatus.NEEDSBUILD,
            BuildStatus.BUILDING,
            BuildStatus.GATHERING,
            BuildStatus.UPLOADING,
            BuildStatus.CANCELLING,
        ]

    @property
    def completed_builds(self):
        """See `ISnap`."""
        filter_term = Not(SnapBuild.status.is_in(self._pending_states))
        order_by = (
            NullsLast(
                Desc(Greatest(SnapBuild.date_started, SnapBuild.date_finished))
            ),
            Desc(SnapBuild.id),
        )
        return self._getBuilds(filter_term, order_by)

    @property
    def pending_builds(self):
        """See `ISnap`."""
        filter_term = SnapBuild.status.is_in(self._pending_states)
        # We want to order by date_created but this is the same as ordering
        # by id (since id increases monotonically) and is less expensive.
        order_by = Desc(SnapBuild.id)
        return self._getBuilds(filter_term, order_by)

    @property
    def subscriptions(self):
        return Store.of(self).find(
            SnapSubscription, SnapSubscription.snap == self
        )

    @property
    def subscribers(self):
        return Store.of(self).find(
            Person,
            SnapSubscription.person_id == Person.id,
            SnapSubscription.snap == self,
        )

    def visibleByUser(self, user):
        """See `ISnap`."""
        if self.information_type in PUBLIC_INFORMATION_TYPES:
            return True
        if user is None:
            return False
        store = IStore(self)
        return not store.find(
            Snap, Snap.id == self.id, get_snap_privacy_filter(user)
        ).is_empty()

    def hasSubscription(self, person):
        """See `ISnap`."""
        return self.getSubscription(person) is not None

    def getSubscription(self, person):
        """Returns person's subscription to this snap recipe, or None if no
        subscription is available.
        """
        if person is None:
            return None
        return (
            Store.of(self)
            .find(
                SnapSubscription,
                SnapSubscription.person == person,
                SnapSubscription.snap == self,
            )
            .one()
        )

    def userCanBeSubscribed(self, person):
        """Checks if the given person can subscribe to this snap recipe."""
        return not (
            self.private and person.is_team and person.anyone_can_join()
        )

    def subscribe(self, person, subscribed_by, ignore_permissions=False):
        """See `ISnap`."""
        if not self.userCanBeSubscribed(person):
            raise SubscriptionPrivacyViolation(
                "Open and delegated teams cannot be subscribed to private "
                "snap recipes."
            )
        subscription = self.getSubscription(person)
        if subscription is None:
            subscription = SnapSubscription(
                person=person, snap=self, subscribed_by=subscribed_by
            )
            Store.of(subscription).flush()
        service = getUtility(IService, "sharing")
        snaps = service.getVisibleArtifacts(
            person, snaps=[self], ignore_permissions=True
        )["snaps"]
        if not snaps:
            service.ensureAccessGrants(
                [person],
                subscribed_by,
                snaps=[self],
                ignore_permissions=ignore_permissions,
            )

    def unsubscribe(self, person, unsubscribed_by, ignore_permissions=False):
        """See `ISnap`."""
        subscription = self.getSubscription(person)
        if subscription is None:
            return
        if (
            not ignore_permissions
            and not subscription.canBeUnsubscribedByUser(unsubscribed_by)
        ):
            raise UserCannotUnsubscribePerson(
                "%s does not have permission to unsubscribe %s."
                % (unsubscribed_by.displayname, person.displayname)
            )
        artifact = getUtility(IAccessArtifactSource).find([self])
        getUtility(IAccessArtifactGrantSource).revokeByArtifact(
            artifact, [person]
        )
        store = Store.of(subscription)
        store.remove(subscription)
        IStore(self).flush()

    def _reconcileAccess(self):
        """Reconcile the snap's sharing information.

        Takes the privacy and pillar and makes the related AccessArtifact
        and AccessPolicyArtifacts match.
        """
        if self.project is None:
            return
        pillars = [self.project]
        reconcile_access_for_artifacts([self], self.information_type, pillars)

    def setProject(self, project):
        self.project = project
        self._reconcileAccess()

    def _deleteAccessGrants(self):
        """Delete access grants for this snap recipe prior to deleting it."""
        getUtility(IAccessArtifactSource).delete([self])

    def _deleteSnapSubscriptions(self):
        subscriptions = Store.of(self).find(
            SnapSubscription, SnapSubscription.snap == self
        )
        subscriptions.remove()

    def destroySelf(self):
        """See `ISnap`."""
        store = IStore(Snap)
        store.find(SnapArch, SnapArch.snap == self).remove()
        # Remove build jobs.  There won't be many queued builds, so we can
        # afford to do this the safe but slow way via BuildQueue.destroySelf
        # rather than in bulk.
        buildqueue_records = store.find(
            BuildQueue,
            BuildQueue._build_farm_job_id == SnapBuild.build_farm_job_id,
            SnapBuild.snap == self,
        )
        for buildqueue_record in buildqueue_records:
            buildqueue_record.destroySelf()
        build_farm_job_ids = list(
            store.find(SnapBuild.build_farm_job_id, SnapBuild.snap == self)
        )
        # XXX cjwatson 2016-02-27 bug=322972: Requires manual SQL due to
        # lack of support for DELETE FROM ... USING ... in Storm.
        store.execute(
            """
            DELETE FROM SnapFile
            USING SnapBuild
            WHERE
                SnapFile.snapbuild = SnapBuild.id AND
                SnapBuild.snap = ?
            """,
            (self.id,),
        )
        store.execute(
            """
            DELETE FROM SnapBuildJob
            USING SnapBuild
            WHERE
                SnapBuildJob.snapbuild = SnapBuild.id AND
                SnapBuild.snap = ?
            """,
            (self.id,),
        )
        store.find(SnapBuild, SnapBuild.snap == self).remove()
        affected_jobs = Select(
            [SnapJob.job_id], And(SnapJob.job == Job.id, SnapJob.snap == self)
        )
        store.find(Job, Job.id.is_in(affected_jobs)).remove()
        getUtility(IWebhookSet).delete(self.webhooks)
        self._deleteAccessGrants()
        self._deleteSnapSubscriptions()
        store.remove(self)
        store.find(
            BuildFarmJob, BuildFarmJob.id.is_in(build_farm_job_ids)
        ).remove()


class SnapArch(StormBase):
    """Link table to back `Snap.processors`."""

    __storm_table__ = "SnapArch"
    __storm_primary__ = ("snap_id", "processor_id")

    snap_id = Int(name="snap", allow_none=False)
    snap = Reference(snap_id, "Snap.id")

    processor_id = Int(name="processor", allow_none=False)
    processor = Reference(processor_id, "Processor.id")


@recipe_registry.register_recipe_type(
    ISnapSet, "Some snap packages build from this repository."
)
@implementer(ISnapSet)
class SnapSet:
    """See `ISnapSet`."""

    def new(
        self,
        registrant,
        owner,
        distro_series,
        name,
        description=None,
        branch=None,
        git_repository=None,
        git_repository_url=None,
        git_path=None,
        git_ref=None,
        build_path=None,
        auto_build=False,
        auto_build_archive=None,
        auto_build_pocket=None,
        auto_build_channels=None,
        require_virtualized=True,
        processors=None,
        date_created=DEFAULT,
        information_type=InformationType.PUBLIC,
        allow_internet=True,
        build_source_tarball=False,
        store_upload=False,
        store_series=None,
        store_name=None,
        store_secrets=None,
        store_channels=None,
        project=None,
        pro_enable=False,
        use_fetch_service=False,
        fetch_service_policy=FetchServicePolicy.STRICT,
    ):
        """See `ISnapSet`."""
        if not registrant.inTeam(owner):
            if owner.is_team:
                raise SnapNotOwner(
                    "%s is not a member of %s."
                    % (registrant.displayname, owner.displayname)
                )
            else:
                raise SnapNotOwner(
                    "%s cannot create snap packages owned by %s."
                    % (registrant.displayname, owner.displayname)
                )

        if (
            sum(
                [
                    git_repository is not None,
                    git_repository_url is not None,
                    git_ref is not None,
                ]
            )
            > 1
        ):
            raise IncompatibleArguments(
                "You cannot specify more than one of 'git_repository', "
                "'git_repository_url', and 'git_ref'."
            )
        if (git_repository is None and git_repository_url is None) != (
            git_path is None
        ):
            raise IncompatibleArguments(
                "You must specify both or neither of "
                "'git_repository'/'git_repository_url' and 'git_path'."
            )
        if git_repository is not None:
            git_ref = git_repository.getRefByPath(git_path)
        elif git_repository_url is not None:
            git_ref = getUtility(IGitRefRemoteSet).new(
                git_repository_url, git_path
            )
        if branch is None and git_ref is None:
            raise NoSourceForSnap
        if self.exists(owner, name):
            raise DuplicateSnapName

        # The relevant validators will do their own checks as well, but we
        # do a single up-front check here in order to avoid an
        # IntegrityError due to exceptions being raised during object
        # creation and to ensure that everything relevant is in the Storm
        # cache.
        if not self.isValidInformationType(
            information_type, owner, branch, git_ref
        ):
            raise SnapPrivacyMismatch

        store = IPrimaryStore(Snap)
        snap = Snap(
            registrant,
            owner,
            distro_series,
            name,
            description=description,
            branch=branch,
            git_ref=git_ref,
            build_path=build_path,
            auto_build=auto_build,
            auto_build_archive=auto_build_archive,
            auto_build_pocket=auto_build_pocket,
            auto_build_channels=auto_build_channels,
            require_virtualized=require_virtualized,
            date_created=date_created,
            information_type=information_type,
            allow_internet=allow_internet,
            build_source_tarball=build_source_tarball,
            store_upload=store_upload,
            store_series=store_series,
            store_name=store_name,
            store_secrets=store_secrets,
            store_channels=store_channels,
            project=project,
            pro_enable=pro_enable,
            use_fetch_service=use_fetch_service,
            fetch_service_policy=fetch_service_policy,
        )
        store.add(snap)
        snap._reconcileAccess()

        # Automatically subscribe the owner to the Snap.
        snap.subscribe(snap.owner, registrant, ignore_permissions=True)

        if processors is None:
            processors = [
                p for p in snap.available_processors if p.build_by_default
            ]
        snap.setProcessors(processors)

        return snap

    def getPossibleSnapInformationTypes(self, project):
        """See `ISnapSet`."""
        return BRANCH_POLICY_ALLOWED_TYPES[project.branch_sharing_policy]

    def isValidInformationType(
        self, information_type, owner, branch=None, git_ref=None
    ):
        private = information_type not in PUBLIC_INFORMATION_TYPES
        if private:
            return True

        # Public snaps with private sources are not allowed.
        source = branch or git_ref
        if source is not None and source.private:
            return False

        # Public snaps owned by private teams are not allowed.
        if owner is not None and owner.private:
            return False

        return True

    def _getByName(self, owner, name):
        return (
            IStore(Snap)
            .find(Snap, Snap.owner == owner, Snap.name == name)
            .one()
        )

    def exists(self, owner, name):
        """See `ISnapSet`."""
        return self._getByName(owner, name) is not None

    def getByName(self, owner, name):
        """See `ISnapSet`."""
        snap = self._getByName(owner, name)
        if snap is None:
            raise NoSuchSnap(name)
        return snap

    def getByPillarAndName(self, owner, pillar, name):
        conditions = [Snap.owner == owner, Snap.name == name]
        if pillar is None:
            # If we start supporting more pillars, remember to add the
            # conditions here.
            conditions.append(Snap.project == None)
        elif IProduct.providedBy(pillar):
            conditions.append(Snap.project == pillar)
        else:
            raise NotImplementedError("Unknown pillar for snap: %s" % pillar)
        return IStore(Snap).find(Snap, *conditions).one()

    def _getSnapsFromCollection(
        self, collection, owner=None, visible_by_user=None
    ):
        if IBranchCollection.providedBy(collection):
            id_column = Snap.branch_id
            ids = collection.getBranchIds()
        else:
            id_column = Snap.git_repository_id
            ids = collection.getRepositoryIds()
        expressions = [id_column.is_in(ids._get_select())]
        if owner is not None:
            expressions.append(Snap.owner == owner)
        expressions.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *expressions)

    def findByIds(self, snap_ids, visible_by_user=None):
        """See `ISnapSet`."""
        clauses = [Snap.id.is_in(snap_ids)]
        if visible_by_user is not None:
            clauses.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *clauses)

    def findByOwner(self, owner):
        """See `ISnapSet`."""
        return IStore(Snap).find(Snap, Snap.owner == owner)

    def findByPerson(self, person, visible_by_user=None):
        """See `ISnapSet`."""

        def _getSnaps(collection):
            collection = collection.visibleByUser(visible_by_user)
            owned = self._getSnapsFromCollection(
                collection.ownedBy(person), visible_by_user=visible_by_user
            )
            packaged = self._getSnapsFromCollection(
                collection, owner=person, visible_by_user=visible_by_user
            )
            return owned.union(packaged)

        bzr_collection = removeSecurityProxy(getUtility(IAllBranches))
        bzr_snaps = _getSnaps(bzr_collection)
        git_collection = removeSecurityProxy(getUtility(IAllGitRepositories))
        git_snaps = _getSnaps(git_collection)
        git_url_snaps = IStore(Snap).find(
            Snap, Snap.owner == person, Snap.git_repository_url != None
        )
        return bzr_snaps.union(git_snaps).union(git_url_snaps)

    def findByProject(self, project, visible_by_user=None):
        """See `ISnapSet`."""

        def _getSnaps(collection):
            return self._getSnapsFromCollection(
                collection.visibleByUser(visible_by_user),
                visible_by_user=visible_by_user,
            )

        snaps_for_project = IStore(Snap).find(
            Snap,
            Snap.project == project,
            get_snap_privacy_filter(visible_by_user),
        )
        bzr_collection = removeSecurityProxy(IBranchCollection(project))
        git_collection = removeSecurityProxy(IGitCollection(project))
        return snaps_for_project.union(_getSnaps(bzr_collection)).union(
            _getSnaps(git_collection)
        )

    def findByBranch(
        self, branch, visible_by_user=None, check_permissions=True
    ):
        """See `ISnapSet`."""
        clauses = [Snap.branch == branch]
        if check_permissions:
            clauses.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *clauses)

    def findByGitRepository(
        self,
        repository,
        paths=None,
        visible_by_user=None,
        check_permissions=True,
    ):
        """See `ISnapSet`."""
        clauses = [Snap.git_repository == repository]
        if paths is not None:
            clauses.append(Snap.git_path.is_in(paths))
        if check_permissions:
            clauses.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *clauses)

    def findByGitRef(self, ref, visible_by_user=None):
        """See `ISnapSet`."""
        return IStore(Snap).find(
            Snap,
            Snap.git_repository == ref.repository,
            Snap.git_path == ref.path,
            get_snap_privacy_filter(visible_by_user),
        )

    def findByContext(self, context, visible_by_user=None, order_by_date=True):
        if IPerson.providedBy(context):
            snaps = self.findByPerson(context, visible_by_user=visible_by_user)
        elif IProduct.providedBy(context):
            snaps = self.findByProject(
                context, visible_by_user=visible_by_user
            )
        elif IBranch.providedBy(context):
            snaps = self.findByBranch(context, visible_by_user=visible_by_user)
        elif IGitRepository.providedBy(context):
            snaps = self.findByGitRepository(
                context, visible_by_user=visible_by_user
            )
        elif IGitRef.providedBy(context):
            snaps = self.findByGitRef(context, visible_by_user=visible_by_user)
        else:
            raise BadSnapSearchContext(context)
        if order_by_date:
            snaps.order_by(
                Desc(Snap.date_last_modified), Desc(Snap.date_created)
            )
        return snaps

    def findByURL(self, url, owner=None, visible_by_user=None):
        """See `ISnapSet`."""
        clauses = [Snap.git_repository_url == url]
        if owner is not None:
            clauses.append(Snap.owner == owner)
        clauses.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *clauses)

    def findByURLPrefix(self, url_prefix, owner=None, visible_by_user=None):
        """See `ISnapSet`."""
        return self.findByURLPrefixes(
            [url_prefix], owner=owner, visible_by_user=visible_by_user
        )

    def findByURLPrefixes(
        self, url_prefixes, owner=None, visible_by_user=None
    ):
        """See `ISnapSet`."""
        prefix_clauses = [
            Snap.git_repository_url.startswith(url_prefix)
            for url_prefix in url_prefixes
        ]
        clauses = [Or(*prefix_clauses)]
        if owner is not None:
            clauses.append(Snap.owner == owner)
        clauses.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *clauses)

    def findByStoreName(self, store_name, owner=None, visible_by_user=None):
        """See `ISnapSet`."""
        clauses = [Snap.store_name == store_name]
        if owner is not None:
            clauses.append(Snap.owner == owner)
        clauses.append(get_snap_privacy_filter(visible_by_user))
        return IStore(Snap).find(Snap, *clauses)

    def preloadDataForSnaps(self, snaps, user=None):
        """See `ISnapSet`."""
        snaps = [removeSecurityProxy(snap) for snap in snaps]

        person_ids = set()
        for snap in snaps:
            person_ids.add(snap.registrant_id)
            person_ids.add(snap.owner_id)

        branches = load_related(Branch, snaps, ["branch_id"])
        repositories = load_related(
            GitRepository, snaps, ["git_repository_id"]
        )
        if branches:
            GenericBranchCollection.preloadDataForBranches(branches)
        if repositories:
            GenericGitCollection.preloadDataForRepositories(repositories)
        # The stacked-on branches are used to check branch visibility.
        GenericBranchCollection.preloadVisibleStackedOnBranches(branches, user)
        GenericGitCollection.preloadVisibleRepositories(repositories, user)

        # Add branch/repository owners to the list of pre-loaded persons.
        # We need the target repository owner as well; unlike branches,
        # repository unique names aren't trigger-maintained.
        person_ids.update(branch.owner_id for branch in branches)
        person_ids.update(repository.owner_id for repository in repositories)

        list(
            getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                person_ids, need_validity=True
            )
        )

    def getSnapcraftYaml(self, context, logger=None):
        """See `ISnapSet`."""
        build_path = None
        if ISnap.providedBy(context):
            build_path = context.build_path
            context = context.source
        if context is None:
            raise CannotFetchSnapcraftYaml("Snap source is not defined")
        try:
            if build_path is not None:
                paths = ("/".join((build_path, "snapcraft.yaml")),)
            else:
                paths = (
                    "snap/snapcraft.yaml",
                    "build-aux/snap/snapcraft.yaml",
                    "snapcraft.yaml",
                    ".snapcraft.yaml",
                )
            for path in paths:
                try:
                    blob = context.getBlob(path)
                    if (
                        IGitRef.providedBy(context)
                        and context.repository_url is not None
                        and isinstance(blob, bytes)
                        and b":" not in blob
                        and b"\n" not in blob
                    ):
                        # Heuristic.  It seems somewhat common for Git
                        # hosting sites to return the symlink target path
                        # when fetching a blob corresponding to a symlink
                        # committed to a repository: GitHub and GitLab both
                        # have this property.  If it looks like this is what
                        # has happened here, try resolving the symlink (only
                        # to one level).
                        resolved_path = urlutils.join(
                            urlutils.dirname(path), six.ensure_str(blob)
                        ).lstrip("/")
                        blob = context.getBlob(resolved_path)
                    break
                except (BranchFileNotFound, GitRepositoryBlobNotFound):
                    pass
            else:
                if logger is not None:
                    logger.exception(
                        "Cannot find snapcraft.yaml in %s", context.unique_name
                    )
                raise MissingSnapcraftYaml(context.unique_name)
        except GitRepositoryBlobUnsupportedRemote as e:
            raise CannotFetchSnapcraftYaml(str(e), unsupported_remote=True)
        except urlutils.InvalidURLJoin as e:
            raise CannotFetchSnapcraftYaml(str(e))
        except (BranchHostingFault, GitRepositoryScanFault) as e:
            msg = "Failed to get snap manifest from %s"
            if logger is not None:
                logger.exception(msg, context.unique_name)
            raise CannotFetchSnapcraftYaml(
                "%s: %s" % (msg % context.unique_name, e)
            )

        try:
            snapcraft_data = yaml.safe_load(blob)
        except Exception as e:
            # Don't bother logging parsing errors from user-supplied YAML.
            raise CannotParseSnapcraftYaml(
                "Cannot parse snapcraft.yaml from %s: %s"
                % (context.unique_name, e)
            )

        if not isinstance(snapcraft_data, dict):
            raise CannotParseSnapcraftYaml(
                "The top level of snapcraft.yaml from %s is not a mapping"
                % context.unique_name
            )

        return snapcraft_data

    @staticmethod
    def _findStaleSnaps():
        """See `ISnapSet`."""
        threshold_date = datetime.now(timezone.utc) - timedelta(
            minutes=config.snappy.auto_build_frequency
        )
        origin = [
            Snap,
            LeftJoin(
                SnapBuild,
                And(
                    SnapBuild.snap_id == Snap.id,
                    SnapBuild.archive_id == Snap.auto_build_archive_id,
                    SnapBuild.pocket == Snap.auto_build_pocket,
                    Not(
                        IsDistinctFrom(
                            SnapBuild.channels, Snap.auto_build_channels
                        )
                    ),
                    # We only want Snaps that haven't had an automatic
                    # SnapBuild dispatched for them recently.
                    SnapBuild.date_created >= threshold_date,
                ),
            ),
        ]
        return (
            IStore(Snap)
            .using(*origin)
            .find(
                Snap,
                Snap.is_stale,
                Snap.auto_build,
                SnapBuild.date_created == None,
            )
            .config(distinct=True)
        )

    @classmethod
    def makeAutoBuilds(cls, logger=None):
        """See `ISnapSet`."""
        snaps = cls._findStaleSnaps()
        build_requests = []
        for snap in snaps:
            snap.is_stale = False
            if logger is not None:
                logger.debug(
                    "Scheduling builds of snap package %s/%s",
                    snap.owner.name,
                    snap.name,
                )
            try:
                build_request = snap.requestBuilds(
                    snap.owner,
                    snap.auto_build_archive,
                    snap.auto_build_pocket,
                    channels=snap.auto_build_channels,
                )
            except SnapBuildArchiveOwnerMismatch as e:
                if logger is not None:
                    logger.exception(
                        "%s Snap owner: %s, Archive owner: %s"
                        % (
                            e,
                            snap.owner.displayname,
                            snap.auto_build_archive.owner.displayname,
                        )
                    )
                continue
            except Exception as e:
                if logger is not None:
                    logger.exception(e)
                continue
            build_requests.append(build_request)
        return build_requests

    def detachFromBranch(self, branch):
        """See `ISnapSet`."""
        self.findByBranch(branch).set(
            branch_id=None, date_last_modified=UTC_NOW
        )

    def detachFromGitRepository(self, repository):
        """See `ISnapSet`."""
        self.findByGitRepository(repository).set(
            git_repository_id=None, git_path=None, date_last_modified=UTC_NOW
        )

    def empty_list(self):
        """See `ISnapSet`."""
        return []


@implementer(IEncryptedContainer)
class SnapStoreSecretsEncryptedContainer(NaClEncryptedContainerBase):
    @property
    def public_key_bytes(self):
        if config.snappy.store_secrets_public_key is not None:
            return base64.b64decode(
                config.snappy.store_secrets_public_key.encode("UTF-8")
            )
        else:
            return None

    @property
    def private_key_bytes(self):
        if config.snappy.store_secrets_private_key is not None:
            return base64.b64decode(
                config.snappy.store_secrets_private_key.encode("UTF-8")
            )
        else:
            return None


def get_snap_privacy_filter(user):
    """Returns the filter for all Snaps that the given user has access to,
    including private snaps where the user has proper permission.

    :param user: An IPerson, or a class attribute that references an IPerson
                 in the database.
    :return: A storm condition.
    """
    # XXX pappacena 2021-02-12: Once we do the migration to back fill
    # information_type, we should be able to change this.
    private_snap = SQL(
        "COALESCE(information_type NOT IN ?, private)",
        params=[tuple(i.value for i in PUBLIC_INFORMATION_TYPES)],
    )
    if user is None:
        return private_snap == False

    artifact_grant_query = Coalesce(
        ArrayIntersects(
            SQL("%s.access_grants" % Snap.__storm_table__),
            Select(
                ArrayAgg(TeamParticipation.team_id),
                tables=TeamParticipation,
                where=(TeamParticipation.person == user),
            ),
        ),
        False,
    )

    policy_grant_query = Coalesce(
        ArrayIntersects(
            Array(SQL("%s.access_policy" % Snap.__storm_table__)),
            Select(
                ArrayAgg(AccessPolicyGrant.policy_id),
                tables=(
                    AccessPolicyGrant,
                    Join(
                        TeamParticipation,
                        TeamParticipation.team_id
                        == AccessPolicyGrant.grantee_id,
                    ),
                ),
                where=(TeamParticipation.person == user),
            ),
        ),
        False,
    )

    admin_team = getUtility(ILaunchpadCelebrities).admin
    user_is_admin = Exists(
        Select(
            TeamParticipation.person_id,
            tables=[TeamParticipation],
            where=And(
                TeamParticipation.team == admin_team,
                TeamParticipation.person == user,
            ),
        )
    )
    return Or(
        private_snap == False,
        artifact_grant_query,
        policy_grant_query,
        user_is_admin,
    )
