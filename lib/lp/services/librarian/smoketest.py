#! /usr/bin/python3 -S
#
# Copyright 2010-2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Perform simple librarian operations to verify the current configuration.
"""

import io
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

import transaction
from zope.component import getUtility

from lp.app.validators.url import validate_url
from lp.services.librarian.interfaces import ILibraryFileAliasSet

FILE_SIZE = 1024
FILE_DATA = b"x" * FILE_SIZE
FILE_LIFETIME = timedelta(hours=1)


def store_file(client):
    expiry_date = datetime.now(timezone.utc) + FILE_LIFETIME
    file_id = client.addFile(
        "smoke-test-file",
        FILE_SIZE,
        io.BytesIO(FILE_DATA),
        "text/plain",
        expires=expiry_date,
    )
    # To be able to retrieve the file, we must commit the current transaction.
    transaction.commit()
    alias = getUtility(ILibraryFileAliasSet)[file_id]
    return (file_id, alias.http_url)


def read_file(url):
    # Validate URL scheme to prevent file:// and other unexpected schemes
    if not validate_url(url, ["http", "https"]):
        return None
    try:
        data = urlopen(url).read()  # nosec B310 (scheme validated above)
    except MemoryError:
        # Re-raise catastrophic errors.
        raise
    except Exception:
        # An error is represented by returning None, which won't match when
        # comapred against FILE_DATA.
        return None

    return data


def upload_and_check(client, output):
    id, url = store_file(client)
    output.write("retrieving file from http_url (%s)\n" % (url,))
    if read_file(url) != FILE_DATA:
        return False
    output.write("retrieving file from client\n")
    if client.getFileByAlias(id).read() != FILE_DATA:
        return False
    return True


def do_smoketest(restricted_client, regular_client, output=None):
    if output is None:
        output = sys.stdout
    output.write("adding a private file to the librarian...\n")
    if not upload_and_check(restricted_client, output):
        output.write("ERROR: data fetched does not match data written\n")
        return 1

    output.write("adding a public file to the librarian...\n")
    if not upload_and_check(regular_client, output):
        output.write("ERROR: data fetched does not match data written\n")
        return 1

    return 0
