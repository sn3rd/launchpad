#!/usr/bin/python3
#
# Copyright 2009-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import unittest

import apt_pkg

from lp.archiveuploader.tagfiles import TagFileParseError, parse_tagfile
from lp.archiveuploader.tests import datadir


class Testtagfiles(unittest.TestCase):
    def testCheckParseChangesOkay(self):
        """lp.archiveuploader.tagfiles.parse_tagfile should work on a good
        changes file
        """
        parse_tagfile(datadir("good-signed-changes"))

    def testCheckParseBadChanges(self):
        """Malformed but somewhat readable files do not raise an exception.

        We let apt_pkg make of them what it can, and dpkg-source will
        reject them if it can't understand.
        """
        parsed = parse_tagfile(datadir("bad-multiline-changes"))
        self.assertEqual(b"unstable", parsed["Distribution"])

    def testCheckParseMalformedMultiline(self):
        """Malformed but somewhat readable files do not raise an exception.

        We let apt_pkg make of them what it can, and dpkg-source will
        reject them if it can't understand.
        """
        parsed = parse_tagfile(datadir("bad-multiline-changes"))
        self.assertEqual(b"unstable", parsed["Distribution"])
        self.assertRaises(KeyError, parsed.__getitem__, "Fish")

    def testCheckParseEmptyChangesRaises(self):
        """lp.archiveuploader.tagfiles.parse_chantges should raise
        TagFileParseError on empty
        """
        self.assertRaises(
            TagFileParseError, parse_tagfile, datadir("empty-file")
        )

    def testParseChangesNotVulnerableToArchExploit(self):
        """lp.archiveuploader.tagfiles.parse_tagfile should not be vulnerable
        to tags outside of the signed portion
        """
        tf = parse_tagfile(datadir("changes-with-exploit-top"))
        self.assertRaises(KeyError, tf.__getitem__, "you")
        tf = parse_tagfile(datadir("changes-with-exploit-bottom"))
        self.assertRaises(KeyError, tf.__getitem__, "you")

    def testParseCorruptedTagFiles(self):
        """
        parse_tagfile_content should raise TagFileParseError with
        detailed information when parsing corrupted/random binary data.

        We have seen instances where corrupted upload files in the queue
        was causing segfaults while using apt_pkg for tagfile parsing.

        The deb822 module, being implemented in Python, handles such faulty
        uploads much more gracefully.
        """

        with self.assertRaises(TagFileParseError):
            parse_tagfile(datadir("corrupted-upload-file"))


class TestTagFileDebianPolicyCompat(unittest.TestCase):
    def setUp(self):
        """Parse the test file using apt_pkg for comparison."""

        tagfile_path = datadir("test436182_0.1_source.changes")
        tagfile = open(tagfile_path)
        self.apt_pkg_parsed_version = apt_pkg.TagFile(tagfile, bytes=True)
        self.apt_pkg_parsed_version.step()

        self.parse_tagfile_version = parse_tagfile(tagfile_path)

    def test_parse_tagfile_with_multiline_values(self):
        """parse_tagfile should not leave trailing '\n' on multiline values.

        This is a regression test for bug 436182.
        Previously we,
          1. Stripped leading space/tab from subsequent lines of multiline
             values, and
          2. appended a trailing '\n' to the end of the value.
        """

        expected_bytes = (
            b"test75874 anotherbinary\n"
            b" andanother andonemore\n"
            b"\tlastone"
        )

        self.assertEqual(
            expected_bytes, self.apt_pkg_parsed_version.section["Binary"]
        )

        self.assertEqual(expected_bytes, self.parse_tagfile_version["Binary"])

    def test_parse_tagfile_with_newline_delimited_field(self):
        """parse_tagfile should not leave leading or tailing '\n' when
        parsing newline delimited fields.

        Newline-delimited fields should be parsed to match apt_pkg.TagFile.

        Note: in the past, our parse_tagfile function left the leading
        '\n' in the parsed value, whereas it should not have.

        For an example, see
        https://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-files
        """  # noqa: E501

        expected_bytes = (
            b"f26bb9b29b1108e53139da3584a4dc92 1511 test75874_0.1.tar.gz\n "
            b"29c955ff520cea32ab3e0316306d0ac1 393742 "
            b"pmount_0.9.7.orig.tar.gz\n"
            b" 91a8f46d372c406fadcb57c6ff7016f3 5302 "
            b"pmount_0.9.7-2ubuntu2.diff.gz"
        )

        self.assertEqual(
            expected_bytes, self.apt_pkg_parsed_version.section["Files"]
        )

        self.assertEqual(expected_bytes, self.parse_tagfile_version["Files"])

    def test_parse_description_field(self):
        """Apt-pkg preserves the blank-line indicator and does not strip
        leading spaces.

        See https://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-description
        """  # noqa: E501
        expected_bytes = (
            b"Here's the single-line synopsis.\n"
            b" Then there is the extended description which can\n"
            b" span multiple lines, and even include blank-lines like this:\n"
            b" .\n"
            b" There you go. If a line starts with two or more spaces,\n"
            b" it will be displayed verbatim. Like this one:\n"
            b"  Example verbatim line.\n"
            b"    Another verbatim line.\n"
            b" OK, back to normal."
        )

        self.assertEqual(
            expected_bytes, self.apt_pkg_parsed_version.section["Description"]
        )

        # In the past our parse_tagfile function replaced blank-line
        # indicators in the description (' .\n') with new lines ('\n'),
        # but it is now compatible with ParseTagFiles (and ready to be
        # replaced by ParseTagFiles).
        self.assertEqual(
            expected_bytes, self.parse_tagfile_version["Description"]
        )
