from unittest.mock import MagicMock

import transaction
from debian.deb822 import Changes
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.services.feeds.feed import FeedEntry
from lp.services.webapp.servers import LaunchpadTestRequest
from lp.soyuz.enums import PackageUploadCustomFormat, PackageUploadStatus
from lp.soyuz.feed.packageupload import NewPackageUploadsFeed
from lp.soyuz.interfaces.queue import PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR
from lp.testing import (
    ILaunchpadCelebrities,
    TestCaseWithFactory,
    admin_logged_in,
    login,
    record_two_runs,
)
from lp.testing.layers import LaunchpadFunctionalLayer
from lp.testing.matchers import HasQueryCount
from lp.testing.sampledata import USER_EMAIL


class TestNewPackageUploadsFeed(TestCaseWithFactory):
    """Tests for NewPackageUploadsFeed."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login(USER_EMAIL)

    def _make_feed(self, distroseries):
        return NewPackageUploadsFeed(
            distroseries,
            LaunchpadTestRequest(
                SERVER_URL="http://feeds.launchpad.test/ubuntu/%s/%s.atom"
                % (distroseries.name, NewPackageUploadsFeed.feedname)
            ),
        )

    def _make_signed_upload(
        self,
        distroseries,
        archive=None,
        pocket=PackagePublishingPocket.RELEASE,
        status=PackageUploadStatus.DONE,
        changelog_entry="* Fix a bug",
        creator=None,
        signer=None,
        section_name=None,
    ):
        """Create a signed source-only upload that passes all feed filters.

        By default produces a DONE, RELEASE-pocket, main-archive, signed
        source upload
        """
        if creator is None:
            creator = self.factory.makePerson()
        if signer is None:
            signer = creator
        spr = self.factory.makeSourcePackageRelease(
            distroseries=distroseries,
            creator=creator,
            changelog_entry=changelog_entry,
            section_name=section_name,
        )
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=archive or distroseries.main_archive,
            pocket=pocket,
            status=status,
            signing_key=MagicMock(owner=signer, fingerprint="A" * 40),
            changes_file_content=Changes(
                {
                    "Format": "1.7",
                    "Changed-By": "%s <%s>"
                    % (
                        creator.displayname,
                        (
                            creator.preferredemail.email
                            if creator.preferredemail
                            else "noreply@example.com"
                        ),
                    ),
                }
            )
            .dump()
            .encode("UTF-8"),
        )
        with admin_logged_in():  # admin access needed to add source to upload
            upload.addSource(spr)
        return upload

    def test_title_contains_distroseries_name(self):
        distroseries = self.factory.makeDistroSeries(name="focal")
        feed = self._make_feed(distroseries)
        self.assertIn("focal", feed.title)

    def test_getItemsWorker_returns_empty_list_when_no_uploads(self):
        distroseries = self.factory.makeDistroSeries()
        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()
        self.assertEqual(items, [])

    def test_getItemsWorker_respects_quantity_limit(self):
        class LimitedFeed(NewPackageUploadsFeed):
            quantity = 3

        distroseries = self.factory.makeDistroSeries()
        for _ in range(4):
            self._make_signed_upload(distroseries)
        transaction.commit()

        feed = LimitedFeed(
            distroseries,
            LaunchpadTestRequest(
                SERVER_URL="http://feeds.launchpad.test/ubuntu/%s/%s.atom"
                % (distroseries.name, LimitedFeed.feedname)
            ),
        )

        self.assertEqual(3, len(feed._getItemsWorker()))

    def test_itemToFeedEntry_returns_expected_feedentry(self):
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        signer = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries,
            creator=creator,
            signer=signer,
            changelog_entry="some changes",
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertIsInstance(entry, FeedEntry)
        captured_action = (
            entry.title.content.split("(")[-1].rstrip(")").lower()
        )
        expected_action = PACKAGE_UPLOAD_STATUS_MAPPING_TO_STR[
            PackageUploadStatus.ACCEPTED
        ]
        self.assertEqual(captured_action, expected_action)
        self.assertEqual(len(entry.authors), 1)
        self.assertIs(entry.authors[0].name, creator.displayname)
        self.assertIn(upload.displayname, entry.link_alternate)
        self.assertIn(upload.displayversion, entry.link_alternate)
        self.assertIn("some changes", entry.content.content)
        self.assertIn("Changed-By:", entry.content.content)

    def test_entry_content_includes_changelog(self):
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, changelog_entry="* Fixed everything\n* Best app ever"
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertIn("Fixed everything", entry.content.content)
        self.assertIn("Best app ever", entry.content.content)

    def test_entry_content_xml_escaping_changelog_renders_correctly(self):
        """Checks that special chars are properly escaped in the feed."""
        distroseries = self.factory.makeDistroSeries()
        changelog_text = (
            "This & that <test> \"quotes\" 'apostrophes' and what if there is "
            "a 🚀 or let's go even further with '<script>alert(\"x\")</script>"
            " & some text'"
        )

        upload = self._make_signed_upload(
            distroseries, changelog_entry=changelog_text
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        expected_escaped_text = (
            "This &amp; that &lt;test&gt; &quot;quotes&quot; &#x27;apostrophes"
            "&#x27; and what if there is a 🚀 or let&#x27;s go even further wi"
            "th &#x27;&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; "
            "some text&#x27"
        )
        self.assertIn(expected_escaped_text, entry.content.content)

    def test_entry_content_includes_changesfile_link(self):
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertIn("Changes file:", entry.content.content)
        self.assertIn(upload.changes_file_url, entry.content.content)

    def test_entry_content_omits_changesfile_link_when_missing(self):
        """itemToFeedEntry does not crash when changesfile is absent."""
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        removeSecurityProxy(upload).changesfile = None
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertIsInstance(entry, FeedEntry)
        self.assertNotIn("Changes file:", entry.content.content)

    def test_entry_content_includes_email_addresses(self):
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries, creator=creator, signer=creator
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)
        expected = "Changed-By: %s &lt;%s&gt;" % (
            creator.displayname,
            creator.preferredemail.email,
        )

        self.assertIn(expected, entry.content.content)

    def test_entry_content_signed_by_shown_when_different_from_creator(self):
        """Signed-By line appears when signing_key_owner != spr.creator"""
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        signer = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries, creator=creator, signer=signer
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertIn("Signed-By:", entry.content.content)
        self.assertIn(signer.displayname, entry.content.content)

    def test_entry_content_signed_by_omitted_when_same_as_creator(self):
        """Signed-By line is omitted when signing_key_owner == spr.creator"""
        distroseries = self.factory.makeDistroSeries()
        creator = self.factory.makePerson()
        upload = self._make_signed_upload(
            distroseries, creator=creator, signer=creator
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertNotIn("Signed-By:", entry.content.content)

    def test_spr_none_does_not_crash(self):
        # A custom-only upload (spr=None) that passes the filters produces a
        # valid feed entry without crashing.
        distroseries = self.factory.makeDistroSeries()
        signer = self.factory.makePerson()
        # can't use `_make_signed_upload` since it creates an SPR,
        # but we want no SPR here
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(owner=signer, fingerprint="C" * 40),
            changes_file_content=b"Format: 1.7\n\n",
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        entry = feed.itemToFeedEntry(upload)

        self.assertIsInstance(entry, FeedEntry)
        # Falls back to distroseries URL when spr is None.
        self.assertIn(distroseries.name, entry.link_alternate)

    def test_only_main_archive_uploads_are_included(self):
        distroseries = self.factory.makeDistroSeries()

        # Upload to a PPA — should not appear
        ppa = self.factory.makeArchive(distribution=distroseries.distribution)
        self._make_signed_upload(distroseries, archive=ppa)

        # Upload to main — should appear
        main_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(main_upload.displayname, items[0].link_alternate)

    def test_feed_only_contains_accepted_or_done_uploads(self):
        """Only uploads with status ACCEPTED or DONE appear in the feed.

        PackageUploadStatus has five states: NEW, UNAPPROVED, ACCEPTED, DONE,
        and REJECTED.  The feed query is restricted to ACCEPTED and DONE;
        any other status is invisible.  We name each upload after its status
        so we can verify precisely which two packages are returned.
        """
        distroseries = self.factory.makeDistroSeries()
        status_to_name = {
            PackageUploadStatus.NEW: "pkg-new",
            PackageUploadStatus.UNAPPROVED: "pkg-unapproved",
            PackageUploadStatus.ACCEPTED: "pkg-accepted",
            PackageUploadStatus.DONE: "pkg-done",
            PackageUploadStatus.REJECTED: "pkg-rejected",
        }
        for status, pkg_name in status_to_name.items():
            spn = self.factory.makeSourcePackageName(name=pkg_name)
            upload = self._make_signed_upload(distroseries, status=status)
            removeSecurityProxy(
                upload.sourcepackagerelease
            ).sourcepackagename = spn
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(2, len(items))
        titles = {item.title.content.lower() for item in items}
        self.assertTrue(
            any("pkg-accepted" in t for t in titles)
            and any("pkg-done" in t for t in titles),
        )

    def test_backports_excluded(self):
        distroseries = self.factory.makeDistroSeries()
        _ = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.BACKPORTS
        )
        release_upload = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.RELEASE
        )
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(len(items), 1)
        self.assertIn(release_upload.displayname, items[0].link_alternate)

    def test_binary_non_security_upload_is_excluded(self):
        # A source+binary upload in a non-SECURITY pocket is excluded.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.UPDATES
        )
        build = self.factory.makeBinaryPackageBuild()
        with admin_logged_in():  # admin access needed to add build to upload
            upload.addBuild(build)
        transaction.commit()

        feed = self._make_feed(distroseries)

        self.assertEqual(0, len(feed._getItemsWorker()))

    def test_binary_security_upload_with_source_is_included(self):
        # A source+binary upload in the SECURITY pocket is included.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(
            distroseries, pocket=PackagePublishingPocket.SECURITY
        )
        build = self.factory.makeBinaryPackageBuild()
        with admin_logged_in():  # admin access needed to add build to upload
            upload.addBuild(build)
        transaction.commit()

        feed = self._make_feed(distroseries)

        self.assertEqual(1, len(feed._getItemsWorker()))

    def test_binary_only_security_upload_is_excluded(self):
        # A SECURITY upload with binaries but no SPR is excluded.
        # Such uploads have no source package to surface in the feed.
        distroseries = self.factory.makeDistroSeries()
        signer = self.factory.makePerson()
        # can't use `_make_signed_upload` since it creates an SPR,
        # but we want no SPR here
        self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            pocket=PackagePublishingPocket.SECURITY,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(owner=signer, fingerprint="D" * 40),
            changes_file_content=b"Format: 1.7\n\n",
        )
        # a regular signed RELEASE upload that should appear
        release_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(release_upload.displayname, items[0].link_alternate)

    def test_recipe_build_upload_is_excluded(self):
        # Uploads from a source package recipe build are excluded.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        recipe_build = self.factory.makeSourcePackageRecipeBuild()
        removeSecurityProxy(
            upload.sourcepackagerelease
        ).source_package_recipe_build_id = recipe_build.id
        transaction.commit()

        feed = self._make_feed(distroseries)

        self.assertEqual(0, len(feed._getItemsWorker()))

    def test_translations_section_uploads_are_excluded(self):
        # Language-pack uploads (translations section) are excluded.
        distroseries = self.factory.makeDistroSeries()
        self._make_signed_upload(distroseries, section_name="translations")
        main_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(main_upload.displayname, items[0].link_alternate)

    def test_custom_translation_upload_is_excluded(self):
        # Translation-only uploads, which have no SPR or builds, are excluded.
        distroseries = self.factory.makeDistroSeries()
        # can't use `_make_signed_upload` since it creates an SPR,
        # but we want no SPR here
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=distroseries.main_archive,
            status=PackageUploadStatus.DONE,
            signing_key=MagicMock(
                owner=self.factory.makePerson(), fingerprint="A" * 40
            ),
            changes_file_content=b"Format: 1.7\n\n",
        )
        with admin_logged_in():  # admin access needed to add custom to upload
            upload.addCustom(
                self.factory.makeLibraryFileAlias(),
                PackageUploadCustomFormat.DEBIAN_INSTALLER,
            )
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(0, len(items))

    def test_auto_sync_katie_upload_is_excluded(self):
        # Source-only uploads whose creator is the katie auto-sync user are
        # excluded when not targeting the SECURITY pocket.
        distroseries = self.factory.makeDistroSeries()
        katie_upload = self._make_signed_upload(distroseries)
        katie = getUtility(ILaunchpadCelebrities).katie
        removeSecurityProxy(katie_upload.sourcepackagerelease).creator = katie
        manual_upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(manual_upload.displayname, items[0].link_alternate)

    def test_signed_source_upload_is_included(self):
        # A regular signed source upload passes all filters and appears.
        distroseries = self.factory.makeDistroSeries()
        upload = self._make_signed_upload(distroseries)
        transaction.commit()

        feed = self._make_feed(distroseries)
        items = feed._getItemsWorker()

        self.assertEqual(1, len(items))
        self.assertIn(upload.displayname, items[0].link_alternate)

    def test_feed_query_count_is_constant(self):
        # Query count does not grow with the number of uploads
        distroseries = self.factory.makeDistroSeries()
        feed = self._make_feed(distroseries)

        def make_signed_upload():
            self._make_signed_upload(distroseries)

        def render_feed():
            transaction.commit()
            feed._getItemsWorker()

        recorder1, recorder2 = record_two_runs(
            tested_method=render_feed,
            item_creator=make_signed_upload,
            first_round_number=1,
            second_round_number=5,
        )

        self.assertThat(recorder2, HasQueryCount.byEquality(recorder1))
