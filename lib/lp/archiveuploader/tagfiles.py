# Copyright 2009-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Utility classes for parsing Debian tag files."""

__all__ = ["TagFileParseError", "parse_tagfile", "parse_tagfile_content"]


import tempfile
from typing import Dict, Optional

from debian import deb822

from lp.services.mail.signedmessage import strip_pgp_signature


class TagFileParseError(Exception):
    """This exception is raised if parse_changes encounters nastiness"""

    pass


def parse_tagfile_content(
    content: bytes, filename: Optional[str] = None
) -> Dict[str, bytes]:
    """Parses a tag file and returns a dictionary where each field is a key.

    The mandatory first argument is the contents of the tag file as a
    string.

    An OpenPGP cleartext signature will be stripped before parsing if
    one is present.

    Header values are always returned as bytes.
    """

    with tempfile.TemporaryFile() as f:
        f.write(strip_pgp_signature(content))
        f.seek(0)
        try:
            stanzas = list(deb822.Deb822.iter_paragraphs(f))
        except Exception as e:
            raise TagFileParseError("%s: %s" % (filename, e)) from e

    if len(stanzas) != 1:
        raise TagFileParseError(
            "%s: multiple stanzas where only one is expected" % filename
        )

    [stanza] = stanzas

    # Convert to dict with bytes values (deb822 returns strings).
    return {k: v.strip().encode("utf-8") for k, v in stanza.items()}


def parse_tagfile(filename: str) -> Dict[str, bytes]:
    """Parses a tag file and returns a dictionary where each field is a key.

    The mandatory first argument is the filename of the tag file, and
    the contents of that file is passed on to parse_tagfile_content.

    Header values are always returned as bytes.
    """
    with open(filename, "rb") as changes_in:
        content = changes_in.read()
    if not content:
        raise TagFileParseError("%s: empty file" % filename)
    return parse_tagfile_content(content, filename=filename)
