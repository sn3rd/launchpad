# Copyright 2015-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""An `IBuildFarmJobBehaviour` for `SnapBuild`.

Dispatches snap package build jobs to build-farm workers.
"""

__all__ = [
    "SnapBuildBehaviour",
]

from typing import Any, Generator

from twisted.internet import defer
from zope.component import adapter
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.builderproxy import BuilderProxyMixin
from lp.buildmaster.enums import BuildBaseImageType
from lp.buildmaster.interfaces.builder import CannotBuild
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    BuildArgs,
    IBuildFarmJobBehaviour,
)
from lp.buildmaster.model.buildfarmjobbehaviour import (
    BuildFarmJobBehaviourBase,
)
from lp.code.interfaces.codehosting import LAUNCHPAD_SERVICES
from lp.registry.interfaces.series import SeriesStatus
from lp.services.config import config
from lp.services.features import getFeatureFlag
from lp.services.twistedsupport import cancel_on_timeout
from lp.snappy.interfaces.snap import (
    SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG,
    SnapBuildArchiveOwnerMismatch,
)
from lp.snappy.interfaces.snapbuild import ISnapBuild
from lp.soyuz.adapters.archivedependencies import get_sources_list_for_building
from lp.soyuz.interfaces.archive import ArchiveDisabled


def format_as_rfc3339(timestamp):
    """Return a RFC3339 representation of the given timestamp.

    Clear 'microsecond' and 'tzinfo' before returning its '.isoformat()'
    representation appended with 'Z' (https://www.ietf.org/rfc/rfc3339.txt)

    This is how snapd/SAS and snapcraft usually represent timestamps.
    """
    return timestamp.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


@adapter(ISnapBuild)
@implementer(IBuildFarmJobBehaviour)
class SnapBuildBehaviour(BuilderProxyMixin, BuildFarmJobBehaviourBase):
    """Dispatches `SnapBuild` jobs to workers."""

    builder_type = "snap"
    image_types = [BuildBaseImageType.LXD, BuildBaseImageType.CHROOT]

    def getLogFileName(self):
        das = self.build.distro_arch_series

        # Examples:
        #   buildlog_snap_ubuntu_wily_amd64_name_FULLYBUILT.txt
        return "buildlog_snap_%s_%s_%s_%s_%s.txt" % (
            das.distroseries.distribution.name,
            das.distroseries.name,
            das.architecturetag,
            self.build.snap.name,
            self.build.status.name,
        )

    def verifyBuildRequest(self, logger):
        """Assert some pre-build checks.

        The build request is checked:
         * Virtualized builds can't build on a non-virtual builder
         * The source archive may not be disabled
         * If the source archive is private, the snap owner must match the
           archive owner (see `SnapBuildArchiveOwnerMismatch` docstring)
         * Ensure that we have a chroot
        """
        build = self.build
        if build.virtualized and not self._builder.virtualized:
            raise AssertionError(
                "Attempt to build virtual item on a non-virtual builder."
            )

        if not build.archive.enabled:
            raise ArchiveDisabled(build.archive.displayname)
        if build.archive.private and build.snap.owner != build.archive.owner:
            raise SnapBuildArchiveOwnerMismatch()

        chroot = build.distro_arch_series.getChroot(pocket=build.pocket)
        if chroot is None:
            raise CannotBuild(
                "Missing chroot for %s" % build.distro_arch_series.displayname
            )

    def issueMacaroon(self):
        """See `IBuildFarmJobBehaviour`."""
        return cancel_on_timeout(
            self._authserver.callRemote(
                "issueMacaroon", "snap-build", "SnapBuild", self.build.id
            ),
            config.builddmaster.authentication_timeout,
        )

    @defer.inlineCallbacks
    def extraBuildArgs(self, logger=None) -> Generator[Any, Any, BuildArgs]:
        """
        Return the extra arguments required by the worker for the given build.
        """
        build: ISnapBuild = self.build
        args: BuildArgs = yield super().extraBuildArgs(logger=logger)
        yield self.startProxySession(
            args,
            build.snap.allow_internet,
            use_fetch_service=build.snap.use_fetch_service,
            fetch_service_policy=build.snap.fetch_service_policy,
        )
        args["name"] = build.snap.store_name or build.snap.name
        channels = build.channels or {}
        if "snapcraft" not in channels:
            channels["snapcraft"] = (
                getFeatureFlag(SNAP_SNAPCRAFT_CHANNEL_FEATURE_FLAG) or "apt"
            )
        if channels.get("snapcraft") == "apt":
            # XXX cjwatson 2015-08-03: Allow tools_source to be overridden
            # at some more fine-grained level.
            tools_source = config.snappy.tools_source
            tools_fingerprint = config.snappy.tools_fingerprint
        else:
            # We have to remove the security proxy that Zope applies to this
            # dict, since otherwise we'll be unable to serialise it to
            # XML-RPC.
            args["channels"] = removeSecurityProxy(channels)
            tools_source = None
            tools_fingerprint = None
        archive_dependencies = list(build.archive.dependencies)
        if build.snap_base is not None:
            # Private dependencies are only listed for pro-enabled snaps
            archive_dependencies.extend(
                [
                    dependency
                    for dependency in build.snap_base.dependencies
                    if build.snap.pro_enable
                    or not dependency.dependency.private
                ]
            )
        (
            args["archives"],
            args["trusted_keys"],
        ) = yield get_sources_list_for_building(
            self,
            build.distro_arch_series,
            None,
            archive_dependencies=archive_dependencies,
            tools_source=tools_source,
            tools_fingerprint=tools_fingerprint,
            logger=logger,
        )
        if build.snap.branch is not None:
            args["branch"] = build.snap.branch.bzr_identity
        elif build.snap.git_ref is not None:
            if build.snap.git_ref.repository_url is not None:
                args["git_repository"] = build.snap.git_ref.repository_url
            elif build.snap.git_repository.private:
                macaroon_raw = yield self.issueMacaroon()
                url = build.snap.git_repository.getCodebrowseUrl(
                    username=LAUNCHPAD_SERVICES, password=macaroon_raw
                )
                args["git_repository"] = url
            else:
                args["git_repository"] = (
                    build.snap.git_repository.git_https_url
                )
            # "git clone -b" doesn't accept full ref names.  If this becomes
            # a problem then we could change launchpad-buildd to do "git
            # clone" followed by "git checkout" instead.
            if build.snap.git_path != "HEAD":
                args["git_path"] = build.snap.git_ref.name
        else:
            raise CannotBuild(
                "Source branch/repository for ~%s/%s has been deleted."
                % (build.snap.owner.name, build.snap.name)
            )
        args["build_source_tarball"] = build.snap.build_source_tarball
        if build.snap.build_path is not None:
            args["build_path"] = build.snap.build_path
        args["private"] = build.is_private
        build_request = build.build_request
        if build_request is not None:
            args["build_request_id"] = build_request.id
            # RFC3339 format for timestamp
            # (matching snapd, SAS and snapcraft representation)
            timestamp = format_as_rfc3339(build_request.date_requested)
            args["build_request_timestamp"] = timestamp

        args["target_architectures"] = removeSecurityProxy(
            build.target_architectures
        )
        if build.craft_platform:
            args["craft_platform"] = build.craft_platform
        return args

    def verifySuccessfulBuild(self):
        """See `IBuildFarmJobBehaviour`."""
        # The implementation in BuildFarmJobBehaviourBase checks whether the
        # target suite is modifiable in the target archive.  However, a
        # `SnapBuild`'s archive is a source rather than a target, so that
        # check does not make sense.  We do, however, refuse to build for
        # obsolete series.
        assert self.build.distro_series.status != SeriesStatus.OBSOLETE

    @defer.inlineCallbacks
    def _saveBuildSpecificFiles(self, upload_path):
        yield self.endProxySession(upload_path)
