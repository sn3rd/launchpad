# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Backfill SHA-512 hashes for LibraryFileContent."""

__all__ = ["BackfillLibrarianSHA512"]

import hashlib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import transaction
from storm.expr import In

from lp.services.database.interfaces import IPrimaryStore
from lp.services.librarian.model import LibraryFileAlias, LibraryFileContent
from lp.services.scripts.base import LaunchpadCronScript

CHUNK_SIZE = 64 * 1024
DEFAULT_WORKERS = 8


def _hash_url(url):
    """Download content and return its SHA-512 hex digest."""
    digester = hashlib.sha512()
    with urllib.request.urlopen(url) as response:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            digester.update(chunk)
    return digester.hexdigest()


class BackfillLibrarianSHA512(LaunchpadCronScript):
    """Backfill sha512 hashes for LibraryFileContent."""

    def add_my_options(self):
        self.parser.add_option(
            "-b",
            "--batch-size",
            action="store",
            type="int",
            default=100,
            dest="batch_size",
            help="Number of rows to process per batch",
        )
        self.parser.add_option(
            "-s",
            "--start-id",
            action="store",
            type="int",
            default=1,
            dest="start_id",
            help="Start processing from this content id",
        )
        self.parser.add_option(
            "-w",
            "--workers",
            action="store",
            type="int",
            default=DEFAULT_WORKERS,
            dest="workers",
            help="Number of parallel download workers",
        )

    def main(self):
        store = IPrimaryStore(LibraryFileContent)
        batch_size = self.options.batch_size
        start_at = self.options.start_id
        workers = self.options.workers
        total = 0

        while True:
            conditions = [
                LibraryFileContent.id >= start_at,
                LibraryFileContent.sha512 == None,
            ]
            contents = list(
                store.find(LibraryFileContent, *conditions).order_by(
                    LibraryFileContent.id
                )[:batch_size]
            )

            if not contents:
                break

            content_ids = [c.id for c in contents]
            batch_aliases = list(
                store.find(
                    LibraryFileAlias,
                    In(LibraryFileAlias.content_id, content_ids),
                ).order_by(LibraryFileAlias.content_id, LibraryFileAlias.id)
            )

            # Keep only the first alias per content_id
            alias_by_content = {}
            for alias in batch_aliases:
                alias_by_content.setdefault(alias.content_id, alias)

            work_items = []
            for content in contents:
                alias = alias_by_content.get(content.id)
                if alias is not None:
                    url = alias.http_url
                    if url is not None:
                        work_items.append((content.id, url))

            hashes = {}
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_id = {
                    executor.submit(_hash_url, url): content_id
                    for content_id, url in work_items
                }
                for future in as_completed(future_to_id):
                    content_id = future_to_id[future]
                    try:
                        hashes[content_id] = future.result()
                    except Exception:
                        self.logger.debug(
                            "Content %d not available; skipping.", content_id
                        )

            for content in contents:
                if content.id in hashes:
                    content.sha512 = hashes[content.id]
                    total += 1

            start_at = contents[-1].id + 1
            self.logger.info(
                "Processed batch of %d rows (total backfilled: %d).",
                len(contents),
                total,
            )
            transaction.commit()

        self.logger.info("Done. Backfilled %d rows.", total)
