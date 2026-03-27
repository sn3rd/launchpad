# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the librarian SHA-512 backfill script."""

import hashlib
import logging

import transaction
from zope.security.proxy import removeSecurityProxy

from lp.services.database.interfaces import IStore
from lp.services.librarian.model import LibraryFileContent
from lp.testing import TestCaseWithFactory
from lp.testing.dbuser import switch_dbuser
from lp.testing.layers import LaunchpadZopelessLayer

from lp.services.librarian.scripts.backfill_sha512 import (
    BackfillLibrarianSHA512,
)


class TestBackfillLibrarianSHA512(TestCaseWithFactory):
    layer = LaunchpadZopelessLayer

    def _runScript(self, *args):
        switch_dbuser("testadmin")
        script = BackfillLibrarianSHA512(
            "test-backfill-sha512", test_args=list(args)
        )
        script.logger = logging.getLogger("test-backfill-sha512")
        script.txn = transaction
        script.main()

    def _getLFC(self, lfc_id):
        switch_dbuser("testadmin")
        return IStore(LibraryFileContent).get(LibraryFileContent, lfc_id)

    def _makeAliasWithoutSHA512(self, data):
        switch_dbuser("testadmin")
        lfa = self.factory.makeLibraryFileAlias(content=data)
        lfc_id = removeSecurityProxy(lfa).content_id
        transaction.commit()
        lfc = self._getLFC(lfc_id)
        lfc.sha512 = None
        transaction.commit()
        return lfc_id, hashlib.sha512(data).hexdigest()

    def test_backfills_sha512(self):
        lfc_id, expected_sha512 = self._makeAliasWithoutSHA512(
            b"some test content for sha512"
        )
        self._runScript("--start-id", str(lfc_id))
        self.assertEqual(expected_sha512, self._getLFC(lfc_id).sha512)

    def test_handles_multiple_rows(self):
        lfc_ids = []
        expected_hashes = []
        for data in (
            b"first file content",
            b"second file content",
            b"third file content",
        ):
            lfc_id, expected = self._makeAliasWithoutSHA512(data)
            lfc_ids.append(lfc_id)
            expected_hashes.append(expected)

        self._runScript("--batch-size", "2", "--start-id", str(lfc_ids[0]))

        switch_dbuser("testadmin")
        for lfc_id, expected in zip(lfc_ids, expected_hashes):
            self.assertEqual(expected, self._getLFC(lfc_id).sha512)

    def test_skips_content_without_alias(self):
        switch_dbuser("testadmin")
        lfc = LibraryFileContent(
            filesize=0,
            md5=hashlib.md5(b"").hexdigest(),
            sha1=hashlib.sha1(b"").hexdigest(),
            sha256=hashlib.sha256(b"").hexdigest(),
            sha512=None,
        )
        IStore(LibraryFileContent).add(lfc)
        IStore(LibraryFileContent).flush()
        lfc_id = lfc.id
        transaction.commit()

        self._runScript("--start-id", str(lfc_id))
        self.assertIsNone(self._getLFC(lfc_id).sha512)
