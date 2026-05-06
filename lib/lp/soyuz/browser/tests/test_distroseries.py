from datetime import datetime, timezone
from xml.etree import ElementTree

import transaction
from debian.deb822 import Changes
from zope.publisher.interfaces import NotFound
from zope.security.proxy import removeSecurityProxy

from lp.registry.interfaces.distroseries import IDistroSeries
from lp.services.feeds.browser import NewPackageUploadsFeedLink
from lp.services.webapp import canonical_url
from lp.services.webapp.url import urlappend
from lp.soyuz.enums import PackageUploadStatus
from lp.soyuz.interfaces.queue import IPackageUpload
from lp.testing import TestCaseWithFactory, admin_logged_in, login
from lp.testing.layers import LaunchpadFunctionalLayer
from lp.testing.sampledata import USER_EMAIL

ATOM_NAMESPACE = "{http://www.w3.org/2005/Atom}"
CHANGES_FILE_CONTENT = {
    "Format": "1.7",
    "Changed-By": "Test User <test@example.com>",
}


class TestNewPackageUploadsFeedLink(TestCaseWithFactory):
    """Tests for the NewPackageUploadsFeedLink feed-link helper."""

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login(USER_EMAIL)

    def test_feed_is_provided_for_distroseries(self):
        self.assertIs(NewPackageUploadsFeedLink.usedfor, IDistroSeries)

    def test_title_includes_distroseries_displayname(self):
        distroseries = self.factory.makeDistroSeries()
        link = NewPackageUploadsFeedLink(distroseries)
        self.assertIn(distroseries.displayname, link.title)

    def test_href_ends_with_new_package_uploads_atom(self):
        distroseries = self.factory.makeDistroSeries()
        link = NewPackageUploadsFeedLink(distroseries)
        self.assertTrue(link.href.endswith("new-package-uploads.atom"))

    def test_href_contains_distroseries_feeds_url(self):
        distribution = self.factory.makeDistribution(name="dist-1")
        distroseries = self.factory.makeDistroSeries(
            distribution, name="series-999"
        )

        link = NewPackageUploadsFeedLink(distroseries)
        expected_href = (
            "http://feeds.launchpad.test/dist-1/series-999/"
            ""
            "new-package-uploads.atom"
        )

        self.assertEqual(expected_href, link.href)

    def test_constructor_only_accepts_distroseries(self):
        non_series = self.factory.makeDistribution()
        self.assertRaises(
            AssertionError, NewPackageUploadsFeedLink, non_series
        )


class TestNewPackageUploadsFeedEndpoint(TestCaseWithFactory):
    """Tests for the new-package-uploads feed endpoint.

    These tests exercise the full HTTP stack to verify that the feed URL
    returns well-formed XML and honours observable behaviour such as
    entry ordering and 404 responses for unknown series.
    """

    layer = LaunchpadFunctionalLayer

    def setUp(self):
        super().setUp()
        login(USER_EMAIL)

    def _make_signed_upload(
        self,
        distroseries,
        archive=None,
        date_created=None,
        status=PackageUploadStatus.DONE,
        pocket=None,
        changes_file_content=CHANGES_FILE_CONTENT,
    ) -> IPackageUpload:
        spr = self.factory.makeSourcePackageRelease(
            distroseries=distroseries,
            changelog_entry=changes_file_content.get("Changes")
            or "some changes",
        )
        upload = self.factory.makePackageUpload(
            distroseries=distroseries,
            archive=archive or distroseries.main_archive,
            status=status,
            pocket=pocket,
            changes_file_content=Changes(changes_file_content)
            .dump()
            .encode("UTF-8"),
        )
        with admin_logged_in():  # admin access needed to add source to upload
            upload.addSource(spr)
        if date_created is not None:
            removeSecurityProxy(upload).date_created = date_created
        # Mark upload as signed so the auto-sync filter does not discard it
        naked = removeSecurityProxy(upload)
        naked.signing_key_fingerprint = "A" * 40
        naked.signing_key_owner = self.factory.makePerson()
        return upload

    def _feed_url(self, distroseries):
        return urlappend(
            canonical_url(distroseries, rootsite="feeds"),
            "new-package-uploads.atom",
        )

    def test_endpoint_gives_syntactically_valid_xml_with_uploads(self):
        distroseries = self.factory.makeDistroSeries(name="focal")
        self._make_signed_upload(distroseries)
        transaction.commit()

        browser = self.getUserBrowser(url=self._feed_url(distroseries))
        root = ElementTree.fromstring(browser.contents)

        self.assertEqual(f"{ATOM_NAMESPACE}feed", root.tag)
        self.assertIn("<entry>", browser.contents)

    def test_endpoint_gives_syntactically_valid_xml_with_no_uploads(self):
        distroseries = self.factory.makeDistroSeries(name="focal")

        browser = self.getUserBrowser(url=self._feed_url(distroseries))
        root = ElementTree.fromstring(browser.contents)

        self.assertEqual(f"{ATOM_NAMESPACE}feed", root.tag)

    def test_endpoint_returns_entries_newest_first(self):
        distroseries = self.factory.makeDistroSeries(name="focal")
        dates = [
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 3, 1, tzinfo=timezone.utc),
            datetime(2025, 2, 1, tzinfo=timezone.utc),
        ]
        for date in dates:
            self._make_signed_upload(distroseries, date_created=date)
        transaction.commit()

        browser = self.getUserBrowser(url=self._feed_url(distroseries))
        root = ElementTree.fromstring(browser.contents)

        updated_dates = [
            datetime.fromisoformat(entry.find(f"{ATOM_NAMESPACE}updated").text)
            for entry in root.findall(f"{ATOM_NAMESPACE}entry")
        ]
        self.assertEqual(sorted(updated_dates, reverse=True), updated_dates)

    def test_feed_for_non_existent_distroseries_throws_error(self):
        feed_url = (
            "https://feeds.launchpad.test/ubuntu/NOT-A-SERIES"
            "/new-package-uploads.atom"
        )
        self.assertRaises(NotFound, self.getUserBrowser, url=feed_url)
