from typing import List

from storm.expr import SQL
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.app.interfaces.launchpad import ILaunchpadCelebrities
from lp.registry.interfaces.distroseries import IDistroSeries
from lp.registry.interfaces.person import IPersonSet
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.feeds.feed import (
    FeedBase,
    FeedEntry,
    FeedPerson,
    FeedTypedData,
)
from lp.services.librarian.browser import ProxiedLibraryFileAlias
from lp.services.webapp import canonical_url
from lp.soyuz.enums import PackageUploadStatus
from lp.soyuz.interfaces.queue import (
    PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR,
    IPackageUpload,
    IPackageUploadSet,
)
from lp.soyuz.mail.packageupload import calculate_subject

__all__ = [
    "NewPackageUploadsFeed",
]


def _format_person(person):
    """Format a person as 'Name <email>' for the feed content body"""
    if person.safe_email_or_blank:
        return "%s <%s>" % (person.displayname, person.safe_email_or_blank)
    return person.displayname


class NewPackageUploadsFeed(FeedBase):
    usedfor = IDistroSeries
    feedname: str = "new-package-uploads"

    @property
    def title(self) -> str:
        return "New package uploads for %s" % self.context.name

    @property
    def logo(self) -> str:
        return self.site_url + "/@@/ubuntu-icon"

    @property
    def quantity(self) -> int:
        return 100

    def _getItemsWorker(self) -> List[FeedEntry]:
        uploads = list(
            getUtility(IPackageUploadSet).getAll(
                self.context,
                status=[
                    PackageUploadStatus.ACCEPTED,
                    PackageUploadStatus.DONE,
                ],
                archive=self.context.main_archive,
                pocket=[  # skip BACKPORTS
                    PackagePublishingPocket.RELEASE,
                    PackagePublishingPocket.SECURITY,
                    PackagePublishingPocket.UPDATES,
                    PackagePublishingPocket.PROPOSED,
                ],
            )
            # For ordering by date, we're doing the below instead of simply
            # `.order_by(Desc(PackageUpload.date_created))` because the
            # PackageUpload import causes a warning from the
            # lib/lp/scripts/utilities/importpedant.py script.
            .order_by(SQL("PackageUpload.date_created DESC"))
            # fetch extra to account for filtering below
            .config(limit=self.quantity * 2)
        )

        if not uploads:
            return []

        katie_id = getUtility(ILaunchpadCelebrities).katie.id

        # XXX shreyamalviya 2026-05-04: This whole filtering logic should
        # ideally be converted to a SQL query in the future. This was left like
        # this for now to mimic the existing logic in the mailer that
        # determines which uploads to send notifications for, and in the
        # interest of time.
        filtered_uploads = []
        for upload in uploads:
            spr = upload.sourcepackagerelease
            # Skip binary/mixed uploads unless it's a security upload
            if (
                upload.builds
                and upload.pocket != PackagePublishingPocket.SECURITY
            ):
                continue
            # Skip recipe builds
            if (
                spr
                and removeSecurityProxy(spr).source_package_recipe_build_id
                is not None
            ):
                continue
            # Skip translations/language packs
            if spr and spr.section.name == "translations":
                continue
            if spr is None and not upload.builds and upload.customfiles:
                # Translations-only custom uploads have no spr or builds,
                # only customfiles
                continue
            # Skip binary-only security
            if (
                upload.pocket == PackagePublishingPocket.SECURITY
                and spr is None
            ):
                continue
            # Skip auto-syncs: source-only, Changed-By is the Katie user,
            # non-security (mirrors is_auto_sync_upload in the mailer)
            if (
                spr
                and not upload.builds
                and spr.creator_id == katie_id
                and upload.pocket != PackagePublishingPocket.SECURITY
            ):
                continue

            filtered_uploads.append(upload)
            if len(filtered_uploads) >= self.quantity:
                break

        if not filtered_uploads:
            return []

        # Collect all person IDs we'll need ahead and load them in one query
        person_ids = set()
        for upload in filtered_uploads:
            spr = upload.sourcepackagerelease
            if spr:
                if spr.creator_id:
                    person_ids.add(spr.creator_id)
                if spr.maintainer_id:
                    person_ids.add(spr.maintainer_id)
            if removeSecurityProxy(upload).signing_key_owner_id:
                person_ids.add(
                    removeSecurityProxy(upload).signing_key_owner_id
                )
        if person_ids:
            list(
                getUtility(IPersonSet).getPrecachedPersonsFromIDs(
                    person_ids, need_validity=True
                )
            )

        return [self.itemToFeedEntry(upload) for upload in filtered_uploads]

    def itemToFeedEntry(self, upload: IPackageUpload) -> FeedEntry:
        spr = upload.sourcepackagerelease
        distroseries = upload.distroseries

        title = FeedTypedData(
            calculate_subject(
                spr=spr,
                bprs=upload.builds,
                customfiles=upload.customfiles,
                archive=upload.archive,
                distroseries=distroseries,
                pocket=upload.pocket,
                action=PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR[upload.status],
            )
        )

        if spr:
            link_alternate = canonical_url(
                distroseries.distribution.getSourcePackageRelease(spr),
                rootsite="mainsite",
            )
        else:
            link_alternate = canonical_url(upload, rootsite="mainsite")

        # Build content directly from preloaded SPR fields to avoid expensive
        # methods that do librarian downloads and/or extra queries
        lines = []
        if spr and spr.changelog_entry:
            lines.append("%s\n" % spr.changelog_entry)
        if spr:
            lines.append("Date: %s" % spr.dateuploaded)
            if spr.creator:
                lines.append("Changed-By: %s" % _format_person(spr.creator))
            if spr.maintainer:
                lines.append("Maintainer: %s" % _format_person(spr.maintainer))

        # direct signed uploads give the signer; syncs/recipes/copies give None
        # because nobody signed these
        signer = removeSecurityProxy(upload).signing_key_owner
        if signer and (not spr or signer != spr.creator):
            lines.append("Signed-By: %s" % _format_person(signer))

        lines.append(link_alternate)

        if upload.changesfile is not None:
            changes_file_url = ProxiedLibraryFileAlias(
                upload.changesfile, upload, rootsite="mainsite"
            ).http_url

            if changes_file_url:
                lines.append("\nChanges file: %s" % changes_file_url)

        content = FeedTypedData("\n".join(lines))

        return FeedEntry(
            title=title,
            link_alternate=link_alternate,
            date_created=upload.date_created,
            date_updated=upload.date_created,
            date_published=upload.date_created,
            authors=(
                [FeedPerson(spr.creator, rootsite=self.rootsite)]
                if spr and spr.creator
                else []
            ),
            content=content,
            logo=self.logo,
            icon=self.logo,
        )
