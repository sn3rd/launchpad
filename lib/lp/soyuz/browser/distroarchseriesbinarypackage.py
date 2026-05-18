# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "DistroArchSeriesBinaryPackageNavigation",
    "DistroArchSeriesBinaryPackageView",
]

from lazr.restful.utils import smartquote

from lp.services.database.bulk import load_related
from lp.services.propertycache import cachedproperty
from lp.services.webapp import GetitemNavigation, LaunchpadView
from lp.soyuz.interfaces.distroarchseriesbinarypackage import (
    IDistroArchSeriesBinaryPackage,
)
from lp.soyuz.model.archive import Archive
from lp.soyuz.model.binarypackagebuild import BinaryPackageBuild
from lp.soyuz.model.binarypackagerelease import BinaryPackageRelease
from lp.soyuz.model.sourcepackagerelease import SourcePackageRelease


class DistroArchSeriesBinaryPackageNavigation(GetitemNavigation):
    usedfor = IDistroArchSeriesBinaryPackage


class DistroArchSeriesBinaryPackageView(LaunchpadView):
    @property
    def page_title(self):
        return smartquote(self.context.title)

    @cachedproperty
    def publishing_history(self):
        """Publishing history with related objects prefetched."""
        pubs = self.context.publishing_history
        # Preload related objects to avoid N+1 queries when rendering
        # BinaryPublishingRecordView for each record.
        bprs = load_related(
            BinaryPackageRelease, pubs, ["binarypackagerelease_id"]
        )
        bpbs = load_related(BinaryPackageBuild, bprs, ["build_id"])
        superseded_bpbs = load_related(
            BinaryPackageBuild, pubs, ["supersededby_id"]
        )
        all_bpbs = bpbs + superseded_bpbs
        if all_bpbs:
            load_related(
                SourcePackageRelease,
                all_bpbs,
                ["source_package_release_id"],
            )
            load_related(Archive, all_bpbs, ["archive_id"])
        return pubs
