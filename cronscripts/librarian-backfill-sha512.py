#!/usr/bin/python3 -S
#
# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Backfill SHA-512 hashes for LibraryFileContent."""

import _pythonpath  # noqa: F401

from lp.services.librarian.scripts.backfill_sha512 import (
    BackfillLibrarianSHA512,
)

if __name__ == "__main__":
    script = BackfillLibrarianSHA512(
        "librarian-backfill-sha512", dbuser="librarianbackfill"
    )
    script.lock_and_run()

