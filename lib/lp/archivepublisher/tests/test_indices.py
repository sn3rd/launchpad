# Copyright 2009-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test native archive index generation for Soyuz."""

import os
import tempfile
import unittest

import apt_pkg

from lp.archivepublisher.indices import (
    IndexStanzaFields,
    build_binary_stanza_fields,
    build_source_stanza_fields,
    generate_packages_index,
    generate_sources_index,
    read_extra_overrides,
)
from lp.services.database.interfaces import IStore
from lp.soyuz.enums import (
    BinaryPackageFormat,
    PackagePublishingPriority,
    PackagePublishingStatus,
)
from lp.soyuz.model.publishing import (
    BinaryPackagePublishingHistory,
    SourcePackagePublishingHistory,
)
from lp.soyuz.tests.test_publishing import TestNativePublishingBase


def build_bpph_stanza(bpph, include_sha512=False):
    return build_binary_stanza_fields(
        bpph.binarypackagerelease,
        bpph.component,
        bpph.section,
        bpph.priority,
        bpph.phased_update_percentage,
        False,
        include_sha512=include_sha512,
    )


def build_spph_stanza(spph, include_sha512=False):
    return build_source_stanza_fields(
        spph.sourcepackagerelease,
        spph.component,
        spph.section,
        include_sha512=include_sha512,
    )


def get_field(stanza_fields, name):
    return dict(stanza_fields.fields).get(name)


class TestNativeArchiveIndexes(TestNativePublishingBase):
    deb_md5 = "008409e7feb1c24a6ccab9f6a62d24c5"
    deb_sha1 = "30b7b4e583fa380772c5a40e428434628faef8cf"
    deb_sha256 = (
        "006ca0f356f54b1916c24c282e6fd19961f4356441401f4b0966f2a00bb3e945"
    )
    dsc_md5 = "5913c3ad52c14a62e6ae7eef51f9ef42"
    dsc_sha1 = "e35e29b2ea94bbaa831882e11d1f456690f04e69"
    dsc_sha256 = (
        "ac512102db9724bee18f26945efeeb82fdab89819e64e120fbfda755ca50c2c6"
    )

    def setUp(self):
        """Setup global attributes."""
        TestNativePublishingBase.setUp(self)
        apt_pkg.init_system()

    def testSourceStanza(self):
        """Check just-created source publication Index stanza.

        The so-called 'stanza' method return a chunk of text which
        corresponds to the APT index reference.

        It contains specific package attributes, like: name of the source,
        maintainer identification, DSC format and standards version, etc

        Also contains the paths and checksums for the files included in
        the package in question.
        """
        pub_source = self.getPubSource(
            builddepends="fooish",
            builddependsindep="pyfoo",
            build_conflicts="bar",
            build_conflicts_indep="pybar",
            user_defined_fields=[
                ("Build-Depends-Arch", "libfoo-dev"),
                ("Build-Conflicts-Arch", "libbar-dev"),
            ],
        )

        self.assertEqual(
            [
                "Package: foo",
                "Binary: foo-bin",
                "Version: 666",
                "Section: base",
                "Maintainer: Foo Bar <foo@bar.com>",
                "Build-Depends: fooish",
                "Build-Depends-Indep: pyfoo",
                "Build-Depends-Arch: libfoo-dev",
                "Build-Conflicts: bar",
                "Build-Conflicts-Indep: pybar",
                "Build-Conflicts-Arch: libbar-dev",
                "Architecture: all",
                "Standards-Version: 3.6.2",
                "Format: 1.0",
                "Directory: pool/main/f/foo",
                "Files:",
                " %s 28 foo_666.dsc" % self.dsc_md5,
                "Checksums-Sha1:",
                " %s 28 foo_666.dsc" % self.dsc_sha1,
                "Checksums-Sha256:",
                " %s 28 foo_666.dsc" % self.dsc_sha256,
            ],
            build_spph_stanza(pub_source).makeOutput().splitlines(),
        )

    def testSourceStanzaCustomFields(self):
        """Check just-created source publication Index stanza
        with custom fields (Python-Version).

        A field is excluded if its key case-insensitively matches one that's
        already there. This mostly affects sources that were uploaded before
        Homepage, Checksums-Sha1 or Checksums-Sha256 were excluded.
        """
        pub_source = self.getPubSource(
            builddepends="fooish",
            builddependsindep="pyfoo",
            build_conflicts="bar",
            build_conflicts_indep="pybar",
            user_defined_fields=[
                ("Python-Version", "< 1.5"),
                ("CHECKSUMS-SHA1", "BLAH"),
                ("Build-Depends-Arch", "libfoo-dev"),
                ("Build-Conflicts-Arch", "libbar-dev"),
            ],
        )

        self.assertEqual(
            [
                "Package: foo",
                "Binary: foo-bin",
                "Version: 666",
                "Section: base",
                "Maintainer: Foo Bar <foo@bar.com>",
                "Build-Depends: fooish",
                "Build-Depends-Indep: pyfoo",
                "Build-Depends-Arch: libfoo-dev",
                "Build-Conflicts: bar",
                "Build-Conflicts-Indep: pybar",
                "Build-Conflicts-Arch: libbar-dev",
                "Architecture: all",
                "Standards-Version: 3.6.2",
                "Format: 1.0",
                "Directory: pool/main/f/foo",
                "Files:",
                " %s 28 foo_666.dsc" % self.dsc_md5,
                "Checksums-Sha1:",
                " %s 28 foo_666.dsc" % self.dsc_sha1,
                "Checksums-Sha256:",
                " %s 28 foo_666.dsc" % self.dsc_sha256,
                "Python-Version: < 1.5",
            ],
            build_spph_stanza(pub_source).makeOutput().splitlines(),
        )

    def testBinaryStanza(self):
        """Check just-created binary publication Index stanza.

        See also testSourceStanza, it must present something similar for
        binary packages.
        """
        pub_binaries = self.getPubBinaries(
            depends="biscuit",
            recommends="foo-dev",
            suggests="pyfoo",
            conflicts="old-foo",
            replaces="old-foo",
            provides="foo-master",
            pre_depends="master-foo",
            enhances="foo-super",
            breaks="old-foo",
            phased_update_percentage=50,
        )
        pub_binary = pub_binaries[0]
        self.assertEqual(
            [
                "Package: foo-bin",
                "Source: foo",
                "Priority: standard",
                "Section: base",
                "Installed-Size: 100",
                "Maintainer: Foo Bar <foo@bar.com>",
                "Architecture: all",
                "Version: 666",
                "Recommends: foo-dev",
                "Replaces: old-foo",
                "Suggests: pyfoo",
                "Provides: foo-master",
                "Depends: biscuit",
                "Conflicts: old-foo",
                "Pre-Depends: master-foo",
                "Enhances: foo-super",
                "Breaks: old-foo",
                "Filename: pool/main/f/foo/foo-bin_666_all.deb",
                "Size: 18",
                "MD5sum: " + self.deb_md5,
                "SHA1: " + self.deb_sha1,
                "SHA256: " + self.deb_sha256,
                "Phased-Update-Percentage: 50",
                "Description: Foo app is great",
                " Well ...",
                " it does nothing, though",
            ],
            build_bpph_stanza(pub_binary).makeOutput().splitlines(),
        )

    def testBinaryStanzaArchSpecific(self):
        self.factory.makeBuildableDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="mips",
            add_proc_to_archive_processors=True,
        )
        pub_binaries = self.getPubBinaries(
            architecturespecific=True,
        )
        index_architectures = {
            get_field(build_bpph_stanza(pub_binary), "Architecture")
            for pub_binary in pub_binaries
        }
        self.assertIn("mips", index_architectures)

    def testBinaryStanzaVariant(self):
        for das in self.distroseries.architectures:
            das.enabled = False
        self.factory.makeBuildableDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="amd64",
            add_proc_to_archive_processors=True,
        )

        pub_source = self.getPubSource(architecturehintlist="any")

        [amd64_binary] = self.getPubBinaries(
            pub_source=pub_source,
        )

        self.factory.makeBuildableDistroArchSeries(
            distroseries=self.distroseries,
            architecturetag="amd64v3",
            underlying_architecturetag="amd64",
            add_proc_to_archive_processors=True,
        )

        [amd64v3_binary] = self.getPubBinaries(
            pub_source=pub_source,
            user_defined_fields=[("Architecture-Variant", "amd64v3")],
        )

        architecture_fields = {
            pub_binary.distroarchseries.architecturetag: get_field(
                build_bpph_stanza(pub_binary), "Architecture"
            )
            for pub_binary in [amd64_binary, amd64v3_binary]
        }
        architecture_variant_fields = {
            pub_binary.distroarchseries.architecturetag: get_field(
                build_bpph_stanza(pub_binary), "Architecture-Variant"
            )
            for pub_binary in [amd64_binary, amd64v3_binary]
        }
        self.assertEqual(
            {"amd64": "amd64", "amd64v3": "amd64"},
            architecture_fields,
        )
        self.assertEqual(
            {"amd64": None, "amd64v3": "amd64v3"},
            architecture_variant_fields,
        )

    def testBinaryStanzaWithCustomFields(self):
        """Check just-created binary publication Index stanza with
        custom fields (Python-Version).

        """
        pub_binaries = self.getPubBinaries(
            depends="biscuit",
            recommends="foo-dev",
            suggests="pyfoo",
            conflicts="old-foo",
            replaces="old-foo",
            provides="foo-master",
            pre_depends="master-foo",
            enhances="foo-super",
            breaks="old-foo",
            user_defined_fields=[("Python-Version", ">= 2.4")],
        )
        pub_binary = pub_binaries[0]
        self.assertEqual(
            [
                "Package: foo-bin",
                "Source: foo",
                "Priority: standard",
                "Section: base",
                "Installed-Size: 100",
                "Maintainer: Foo Bar <foo@bar.com>",
                "Architecture: all",
                "Version: 666",
                "Recommends: foo-dev",
                "Replaces: old-foo",
                "Suggests: pyfoo",
                "Provides: foo-master",
                "Depends: biscuit",
                "Conflicts: old-foo",
                "Pre-Depends: master-foo",
                "Enhances: foo-super",
                "Breaks: old-foo",
                "Filename: pool/main/f/foo/foo-bin_666_all.deb",
                "Size: 18",
                "MD5sum: " + self.deb_md5,
                "SHA1: " + self.deb_sha1,
                "SHA256: " + self.deb_sha256,
                "Description: Foo app is great",
                " Well ...",
                " it does nothing, though",
                "Python-Version: >= 2.4",
            ],
            build_bpph_stanza(pub_binary).makeOutput().splitlines(),
        )

    def testBinaryStanzaDescription(self):
        """Check the description field.

        The description field should be formatted as:

        Description: <single line synopsis>
         <extended description over several lines>

        The extended description should allow the following formatting
        actions supported by the dpkg-friend tools:

         * lines to be wrapped should start with a space.
         * lines to be preserved empty should start with single space followed
           by a single full stop (DOT).
         * lines to be presented in Verbatim should start with two or
           more spaces.

        We just want to check if the original description uploaded and stored
        in the system is preserved when we build the archive index.
        """
        description = "Normal\nNormal" "\n.\n.\n." "\n %s" % ("x" * 100)
        pub_binary = self.getPubBinaries(description=description)[0]

        self.assertEqual(
            [
                "Package: foo-bin",
                "Source: foo",
                "Priority: standard",
                "Section: base",
                "Installed-Size: 100",
                "Maintainer: Foo Bar <foo@bar.com>",
                "Architecture: all",
                "Version: 666",
                "Filename: pool/main/f/foo/foo-bin_666_all.deb",
                "Size: 18",
                "MD5sum: " + self.deb_md5,
                "SHA1: " + self.deb_sha1,
                "SHA256: " + self.deb_sha256,
                "Description: Foo app is great",
                " Normal",
                " Normal",
                " .",
                " .",
                " .",
                " %s" % ("x" * 100),
            ],
            build_bpph_stanza(pub_binary).makeOutput().splitlines(),
        )

    def testBinaryStanzaWithNonAscii(self):
        """Check how will be a stanza with non-ascii content

        Only 'Maintainer' (IPerson.displayname) and 'Description'
        (IBinaryPackageRelease.{summary, description}) can possibly
        contain non-ascii stuff.
        The encoding should be preserved and able to be encoded in
        'utf-8' for disk writing.
        """
        description = "Using non-ascii as: \xe7\xe3\xe9\xf3"
        pub_binary = self.getPubBinaries(description=description)[0]

        self.assertEqual(
            [
                "Package: foo-bin",
                "Source: foo",
                "Priority: standard",
                "Section: base",
                "Installed-Size: 100",
                "Maintainer: Foo Bar <foo@bar.com>",
                "Architecture: all",
                "Version: 666",
                "Filename: pool/main/f/foo/foo-bin_666_all.deb",
                "Size: 18",
                "MD5sum: " + self.deb_md5,
                "SHA1: " + self.deb_sha1,
                "SHA256: " + self.deb_sha256,
                "Description: Foo app is great",
                " Using non-ascii as: \xe7\xe3\xe9\xf3",
            ],
            build_bpph_stanza(pub_binary).makeOutput().splitlines(),
        )

    def testBinaryOmitsIdenticalSourceName(self):
        # Binaries omit the Source field if it identical to Package.
        pub_source = self.getPubSource(sourcename="foo")
        pub_binary = self.getPubBinaries(
            binaryname="foo", pub_source=pub_source
        )[0]
        self.assertIs(None, get_field(build_bpph_stanza(pub_binary), "Source"))

    def testBinaryIncludesDifferingSourceName(self):
        # Binaries include a Source field if their name differs.
        pub_source = self.getPubSource(sourcename="foo")
        pub_binary = self.getPubBinaries(
            binaryname="foo-bin", pub_source=pub_source
        )[0]
        self.assertEqual(
            "foo", get_field(build_bpph_stanza(pub_binary), "Source")
        )

    def testBinaryIncludesDifferingSourceVersion(self):
        # Binaries also include a Source field if their versions differ.
        pub_source = self.getPubSource(sourcename="foo", version="666")
        pub_binary = self.getPubBinaries(
            binaryname="foo", version="999", pub_source=pub_source
        )[0]
        self.assertEqual(
            "foo (666)", get_field(build_bpph_stanza(pub_binary), "Source")
        )


class TestNativeArchiveIndexesReparsing(TestNativePublishingBase):
    """Tests for ensuring the native archive indexes that we publish
    can be parsed correctly by apt_pkg.TagFile.
    """

    def setUp(self):
        """Setup global attributes."""
        TestNativePublishingBase.setUp(self)
        apt_pkg.init_system()

    def write_stanza_and_reparse(self, stanza):
        """Helper method to return the apt_pkg parser for the stanza."""
        index_filename = tempfile.mktemp()
        with open(index_filename, "wb") as index_file:
            index_file.write(stanza.makeOutput().encode("utf-8"))

        parser = apt_pkg.TagFile(open(index_filename))

        # We're only interested in one stanza, so we'll parse it and remove
        # the tmp file again.
        section = next(parser)
        os.remove(index_filename)

        return section

    def test_binary_stanza(self):
        """Check a binary stanza with APT parser."""
        pub_binary = self.getPubBinaries()[0]

        section = self.write_stanza_and_reparse(build_bpph_stanza(pub_binary))

        self.assertEqual(section.get("Package"), "foo-bin")
        self.assertEqual(
            section.get("Description").splitlines(),
            ["Foo app is great", " Well ...", " it does nothing, though"],
        )

    def test_source_stanza(self):
        """Check a source stanza with APT parser."""
        pub_source = self.getPubSource()

        section = self.write_stanza_and_reparse(build_spph_stanza(pub_source))

        self.assertEqual(section.get("Package"), "foo")
        self.assertEqual(section.get("Maintainer"), "Foo Bar <foo@bar.com>")

    def test_source_with_corrupt_dsc_binaries(self):
        """Ensure corrupt binary fields are written correctly to indexes.

        This is a regression test for bug 436182.

        During upload, our custom parser at:
          lp.archiveuploader.tagfiles.parse_tagfile_lines
        strips leading spaces from subsequent lines of fields with values
        spanning multiple lines, such as the binary field, and in addition
        leaves a trailing '\n' (which results in a blank line after the
        Binary field).

        The second issue causes apt_pkg.TagFile() to error during
        germination when it attempts to parse the generated Sources index.
        But the first issue will also cause apt_pkg.TagFile to skip each
        newline of a multiline field that is not preceded with a space.

        This test ensures that binary fields saved as such will continue
        to be written correctly to index files.

        This test can be removed if the parser is fixed and the corrupt
        data has been cleaned.
        """
        pub_source = self.getPubSource()

        # An example of a corrupt dsc_binaries field. We need to ensure
        # that the corruption is not carried over into the index stanza.
        pub_source.sourcepackagerelease.dsc_binaries = (
            "foo_bin,\nbar_bin,\nzed_bin"
        )

        section = self.write_stanza_and_reparse(build_spph_stanza(pub_source))

        self.assertEqual("foo", section["Package"])

        # Without the fix, this raises a key-error due to apt-pkg not
        # being able to parse the file.
        self.assertEqual(
            "666",
            section["Version"],
            "The Version field should be parsed correctly.",
        )

        # Without the fix, the second binary would not be parsed at all.
        self.assertEqual("foo_bin,\n bar_bin,\n zed_bin", section["Binary"])

    def test_source_with_correct_dsc_binaries(self):
        """Ensure correct binary fields are written correctly to indexes.

        During upload, our custom parser at:
          lp.archiveuploader.tagfiles.parse_tagfile_lines
        strips leading spaces from subsequent lines of fields with values
        spanning multiple lines, such as the binary field, and in addition
        leaves a trailing '\n' (which results in a blank line after the
        Binary field).

        This test ensures that when our parser is updated to store the
        binary field in the same way that apt_pkg.TagFile would, that it
        will continue to be written correctly to index files.
        """
        pub_source = self.getPubSource()

        # An example of a corrupt dsc_binaries field. We need to ensure
        # that the corruption is not carried over into the index stanza.
        pub_source.sourcepackagerelease.dsc_binaries = (
            "foo_bin,\n bar_bin,\n zed_bin"
        )

        section = self.write_stanza_and_reparse(build_spph_stanza(pub_source))

        self.assertEqual("foo", section["Package"])

        # Without the fix, this raises a key-error due to apt-pkg not
        # being able to parse the file.
        self.assertEqual(
            "666",
            section["Version"],
            "The Version field should be parsed correctly.",
        )

        # Without the fix, the second binary would not be parsed at all.
        self.assertEqual("foo_bin,\n bar_bin,\n zed_bin", section["Binary"])


class TestIndexStanzaFieldsHelper(unittest.TestCase):
    """Check how this auxiliary class works...

    This class provides simple FIFO API for aggregating fields
    (name & values) in a ordered way.

    Provides an method to format the option in a ready-to-use string.
    """

    def test_simple(self):
        fields = IndexStanzaFields()
        fields.append("breakfast", "coffee")
        fields.append("lunch", "beef")
        fields.append("dinner", "fish")

        self.assertEqual(3, len(fields.fields))
        self.assertTrue(("dinner", "fish") in fields.fields)
        self.assertEqual(
            [
                "breakfast: coffee",
                "lunch: beef",
                "dinner: fish",
            ],
            fields.makeOutput().splitlines(),
        )

    def test_preserves_order(self):
        fields = IndexStanzaFields()
        fields.append("one", "um")
        fields.append("three", "tres")
        fields.append("two", "dois")

        self.assertEqual(
            [
                "one: um",
                "three: tres",
                "two: dois",
            ],
            fields.makeOutput().splitlines(),
        )

    def test_files(self):
        # Special treatment for field named 'Files'
        # do not add a space between <name>:<value>
        # <value> will always start with a new line.
        fields = IndexStanzaFields()
        fields.append("one", "um")
        fields.append("Files", "<no_sep>")

        self.assertEqual(
            ["one: um", "Files:<no_sep>"], fields.makeOutput().splitlines()
        )

    def test_extend(self):
        fields = IndexStanzaFields()
        fields.append("one", "um")
        fields.extend([("three", "tres"), ["four", "five"]])

        self.assertEqual(
            [
                "one: um",
                "three: tres",
                "four: five",
            ],
            fields.makeOutput().splitlines(),
        )


class TestDirectSourcesIndex(TestNativePublishingBase):
    """Tests for SQL-based direct Sources index generation."""

    def _generate(self, pub_source, overrides=None):
        """Helper to call generate_sources_index for a given SPPH."""
        store = IStore(SourcePackagePublishingHistory)
        return generate_sources_index(
            store,
            archive_id=pub_source.archive_id,
            distroseries_id=pub_source.distroseries_id,
            pocket=pub_source.pocket.value,
            component_id=pub_source.component_id,
            overrides=overrides,
        )

    def _expected_with_priority(self, pub_source, priority="extra"):
        """Build expected output from ORM path with Priority inserted.

        The ORM path (build_source_stanza_fields) does not emit Priority
        because PPAs don't use it.
        """
        lines = (
            build_spph_stanza(pub_source, include_sha512=True)
            .makeOutput()
            .splitlines()
        )
        for i, line in enumerate(lines):
            if line.startswith("Section:"):
                lines.insert(i, "Priority: %s" % priority)
                break
        return "\n".join(lines) + "\n\n"

    def test_matches_stanza_builder(self):
        """Direct SQL output matches the existing stanza builder."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
            builddepends="fooish",
            builddependsindep="pyfoo",
            build_conflicts="bar",
            build_conflicts_indep="pybar",
            user_defined_fields=[
                ("Build-Depends-Arch", "libfoo-dev"),
                ("Build-Conflicts-Arch", "libbar-dev"),
            ],
        )

        content = self._generate(pub_source)
        expected = self._expected_with_priority(pub_source)
        self.assertEqual(expected, content.decode("utf-8"))

    def test_custom_fields(self):
        """User-defined fields are included and deduplicated."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
            builddepends="fooish",
            builddependsindep="pyfoo",
            build_conflicts="bar",
            build_conflicts_indep="pybar",
            user_defined_fields=[
                ("Python-Version", "< 1.5"),
                ("CHECKSUMS-SHA1", "BLAH"),
                ("Build-Depends-Arch", "libfoo-dev"),
                ("Build-Conflicts-Arch", "libbar-dev"),
            ],
        )

        content = self._generate(pub_source)
        expected = self._expected_with_priority(pub_source)
        self.assertEqual(expected, content.decode("utf-8"))

    def test_empty_index(self):
        """An empty archive produces empty output."""
        self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )
        store = IStore(SourcePackagePublishingHistory)
        content = generate_sources_index(
            store,
            archive_id=-1,
            distroseries_id=1,
            pocket=0,
            component_id=1,
        )
        self.assertEqual(b"", content)

    def test_multiple_packages_ordered(self):
        """Multiple packages appear in name order."""
        pub_a = self.getPubSource(
            sourcename="aaa-pkg",
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubSource(
            sourcename="zzz-pkg",
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubSource(
            sourcename="mmm-pkg",
            status=PackagePublishingStatus.PUBLISHED,
        )

        content = self._generate(pub_a)
        stanzas = content.decode("utf-8").strip().split("\n\n")
        names = []
        for stanza in stanzas:
            for line in stanza.splitlines():
                if line.startswith("Package:"):
                    names.append(line.split(": ", 1)[1])
                    break
        self.assertEqual(["aaa-pkg", "mmm-pkg", "zzz-pkg"], names)

    def test_only_published_status(self):
        """Only PUBLISHED packages are included."""
        self.getPubSource(
            sourcename="pending-pkg",
            status=PackagePublishingStatus.PENDING,
        )
        pub_published = self.getPubSource(
            sourcename="published-pkg",
            status=PackagePublishingStatus.PUBLISHED,
        )

        content = self._generate(pub_published)
        decoded = content.decode("utf-8")
        self.assertIn("Package: published-pkg", decoded)
        self.assertNotIn("Package: pending-pkg", decoded)

    def test_lib_prefix_poolification(self):
        """Source names starting with 'lib' use 4-char pool prefix."""
        pub_source = self.getPubSource(
            sourcename="libfoo",
            status=PackagePublishingStatus.PUBLISHED,
        )

        content = self._generate(pub_source)
        self.assertIn(b"Directory: pool/main/libf/libfoo", content)

    def test_homepage(self):
        """Homepage field is included when present."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )
        pub_source.sourcepackagerelease.homepage = "https://example.com"
        self.layer.commit()

        content = self._generate(pub_source)
        expected = self._expected_with_priority(pub_source)
        self.assertEqual(expected, content.decode("utf-8"))

    def test_priority_from_binaries(self):
        """Priority is derived from the highest-priority published binary."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubBinaries(
            pub_source=pub_source,
            status=PackagePublishingStatus.PUBLISHED,
        )

        content = self._generate(pub_source)
        # getPubBinaries defaults to STANDARD priority.
        self.assertIn(b"Priority: standard", content)

    def test_priority_uses_highest(self):
        """When multiple binaries exist, the highest priority wins."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs = self.getPubBinaries(
            pub_source=pub_source,
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs[0].priority = PackagePublishingPriority.IMPORTANT
        self.layer.commit()

        content = self._generate(pub_source)
        self.assertIn(b"Priority: important", content)

    def test_priority_default_no_binaries(self):
        """Without published binaries, Priority defaults to extra."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )

        content = self._generate(pub_source)
        self.assertIn(b"Priority: extra", content)

    def test_extra_overrides_applied(self):
        """Extra override fields are appended to the matching stanza."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )
        package_name = pub_source.sourcepackagerelease.sourcepackagename.name
        overrides = {package_name: {"Task": "ubuntu-desktop"}}

        content = self._generate(pub_source, overrides=overrides)
        self.assertIn(b"Task: ubuntu-desktop", content)

    def test_extra_overrides_no_duplicate(self):
        """Override headers already in the stanza are not duplicated."""
        pub_source = self.getPubSource(
            status=PackagePublishingStatus.PUBLISHED,
        )
        package_name = pub_source.sourcepackagerelease.sourcepackagename.name
        # "Package" is always present; an attempt to override it must be
        # silently ignored by IndexStanzaFields.
        overrides = {package_name: {"Package": "should-not-replace"}}

        content = self._generate(pub_source, overrides=overrides)
        decoded = content.decode("utf-8")
        self.assertEqual(1, decoded.count("Package:"))
        self.assertIn("Package: %s" % package_name, decoded)


class TestDirectPackagesIndex(TestNativePublishingBase):
    """Tests for SQL-based direct Packages index generation."""

    def _generate(
        self, bpph, separate_long_descriptions=False, overrides=None
    ):
        """Helper to call generate_packages_index for a given BPPH."""
        store = IStore(BinaryPackagePublishingHistory)
        arch = bpph.distroarchseries
        packages_bytes, translations_bytes = generate_packages_index(
            store,
            archive_id=bpph.archive_id,
            distroseries_id=arch.distroseries.id,
            pocket=bpph.pocket.value,
            component_id=bpph.component_id,
            distroarchseries_id=arch.id,
            architecturetag=arch.architecturetag,
            underlying_architecturetag=arch.underlying_architecturetag,
            separate_long_descriptions=separate_long_descriptions,
            overrides=overrides,
        )
        return packages_bytes, translations_bytes

    def test_matches_stanza_builder(self):
        """Direct SQL output matches the existing ORM stanza builder
        (plus SHA512 and Homepage which the ORM path omits).

        Uses an ubuntu.com maintainer address so that both code paths
        agree: the SQL path rewrites non-ubuntu.com addresses, while the
        ORM path does not.
        """
        pub_source = self.getPubSource(
            dsc_maintainer_rfc822="Ubuntu Kernel Team <kernel-team@lists.ubuntu.com>",  # noqa: E501
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs = self.getPubBinaries(
            pub_source=pub_source,
            status=PackagePublishingStatus.PUBLISHED,
        )
        bpph = pubs[0]

        packages_bytes, _ = self._generate(bpph)
        expected = (
            build_bpph_stanza(bpph, include_sha512=True).makeOutput() + "\n\n"
        )
        self.assertEqual(expected, packages_bytes.decode("utf-8"))

    def test_empty_index(self):
        """An empty archive produces empty output."""
        store = IStore(BinaryPackagePublishingHistory)
        packages_bytes, translations_bytes = generate_packages_index(
            store,
            archive_id=-1,
            distroseries_id=1,
            pocket=0,
            component_id=1,
            distroarchseries_id=1,
            architecturetag="i386",
            underlying_architecturetag=None,
        )
        self.assertEqual(b"", packages_bytes)
        self.assertEqual(b"", translations_bytes)

    def test_multiple_packages_ordered(self):
        """Multiple packages appear in name order."""
        pub_a = self.getPubBinaries(
            binaryname="aaa-bin",
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubBinaries(
            binaryname="zzz-bin",
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubBinaries(
            binaryname="mmm-bin",
            status=PackagePublishingStatus.PUBLISHED,
        )

        packages_bytes, _ = self._generate(pub_a[0])
        stanzas = packages_bytes.decode("utf-8").strip().split("\n\n")
        names = []
        for stanza in stanzas:
            for line in stanza.splitlines():
                if line.startswith("Package:"):
                    names.append(line.split(": ", 1)[1])
                    break
        self.assertEqual(["aaa-bin", "mmm-bin", "zzz-bin"], names)

    def test_only_published_status(self):
        """Only PUBLISHED packages are included."""
        self.getPubBinaries(
            binaryname="pending-bin",
            status=PackagePublishingStatus.PENDING,
        )
        pub_published = self.getPubBinaries(
            binaryname="published-bin",
            status=PackagePublishingStatus.PUBLISHED,
        )

        packages_bytes, _ = self._generate(pub_published[0])
        decoded = packages_bytes.decode("utf-8")
        self.assertIn("Package: published-bin", decoded)
        self.assertNotIn("Package: pending-bin", decoded)

    def test_priority_field(self):
        """Priority is included and maps correctly."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )
        # Default priority is STANDARD
        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Priority: standard", packages_bytes)

    def test_priority_optional(self):
        """Priority correctly maps OPTIONAL."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs[0].priority = PackagePublishingPriority.OPTIONAL
        self.layer.commit()

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Priority: optional", packages_bytes)

    def test_source_field_same_name_version(self):
        """Source field is omitted when package and
        source have same name/version."""
        pubs = self.getPubBinaries(
            binaryname="foo-bin",
            status=PackagePublishingStatus.PUBLISHED,
        )
        packages_bytes, _ = self._generate(pubs[0])
        decoded = packages_bytes.decode("utf-8")
        # Source name is 'foo' (derived from 'foo-bin' split on '-'),
        # but binary name is 'foo-bin', so Source field should be present.
        self.assertIn("Source: foo", decoded)

    def test_essential_field(self):
        """Essential field is set to 'yes' when bpr.essential is True."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )
        bpr = pubs[0].binarypackagerelease
        bpr.essential = True
        self.layer.commit()

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Essential: yes", packages_bytes)

    def test_filename_poolified(self):
        """Filename uses correct pool path."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Filename: pool/main/f/foo/", packages_bytes)

    def test_lib_prefix_poolification(self):
        """Source names starting with 'lib' use 4-char pool prefix."""
        pubs = self.getPubBinaries(
            binaryname="libfoo-bin",
            status=PackagePublishingStatus.PUBLISHED,
        )

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Filename: pool/main/libf/libfoo/", packages_bytes)

    def test_homepage_field(self):
        """Homepage field is included when set on BPR."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )
        bpr = pubs[0].binarypackagerelease
        bpr.homepage = "https://example.com"
        self.layer.commit()

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Homepage: https://example.com", packages_bytes)

    def test_user_defined_fields(self):
        """User defined fields are included."""
        pubs = self.getPubBinaries(
            user_defined_fields=[("Multi-Arch", "same")],
            status=PackagePublishingStatus.PUBLISHED,
        )

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"Multi-Arch: same", packages_bytes)

    def test_sha512_included(self):
        """SHA512 hash is included in the output."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )

        packages_bytes, _ = self._generate(pubs[0])
        self.assertIn(b"SHA512:", packages_bytes)

    def test_extra_overrides_applied(self):
        """Extra override fields are appended to the matching stanza."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )
        bpph = pubs[0]
        package_name = bpph.binarypackagerelease.binarypackagename.name
        overrides = {package_name: {"Task": "ubuntu-desktop"}}

        packages_bytes, _ = self._generate(bpph, overrides=overrides)
        self.assertIn(b"Task: ubuntu-desktop", packages_bytes)

    def test_extra_overrides_no_duplicate(self):
        """Override headers already in the stanza are not duplicated."""
        pubs = self.getPubBinaries(
            status=PackagePublishingStatus.PUBLISHED,
        )
        bpph = pubs[0]
        package_name = bpph.binarypackagerelease.binarypackagename.name
        # "Package" is always present; an attempt to override it must be
        # silently ignored by IndexStanzaFields.
        overrides = {package_name: {"Package": "should-not-replace"}}

        packages_bytes, _ = self._generate(bpph, overrides=overrides)
        decoded = packages_bytes.decode("utf-8")
        self.assertEqual(1, decoded.count("Package:"))
        self.assertIn("Package: %s" % package_name, decoded)

    def test_formats_filter_deb_excludes_udeb(self):
        """Passing formats=[DEB] omits UDEB packages."""
        deb_pubs = self.getPubBinaries(
            binaryname="my-deb",
            format=BinaryPackageFormat.DEB,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubBinaries(
            binaryname="my-udeb",
            format=BinaryPackageFormat.UDEB,
            status=PackagePublishingStatus.PUBLISHED,
        )
        store = IStore(BinaryPackagePublishingHistory)
        arch = deb_pubs[0].distroarchseries
        bpph = deb_pubs[0]
        packages_bytes, _ = generate_packages_index(
            store,
            archive_id=bpph.archive_id,
            distroseries_id=arch.distroseries.id,
            pocket=bpph.pocket.value,
            component_id=bpph.component_id,
            distroarchseries_id=arch.id,
            architecturetag=arch.architecturetag,
            underlying_architecturetag=arch.underlying_architecturetag,
            formats=[BinaryPackageFormat.DEB],
        )
        decoded = packages_bytes.decode("utf-8")
        self.assertIn("Package: my-deb", decoded)
        self.assertNotIn("Package: my-udeb", decoded)

    def test_formats_filter_udeb_excludes_deb(self):
        """Passing formats=[UDEB] omits DEB packages."""
        deb_pubs = self.getPubBinaries(
            binaryname="my-deb",
            format=BinaryPackageFormat.DEB,
            status=PackagePublishingStatus.PUBLISHED,
        )
        self.getPubBinaries(
            binaryname="my-udeb",
            format=BinaryPackageFormat.UDEB,
            status=PackagePublishingStatus.PUBLISHED,
        )
        store = IStore(BinaryPackagePublishingHistory)
        arch = deb_pubs[0].distroarchseries
        bpph = deb_pubs[0]
        packages_bytes, _ = generate_packages_index(
            store,
            archive_id=bpph.archive_id,
            distroseries_id=arch.distroseries.id,
            pocket=bpph.pocket.value,
            component_id=bpph.component_id,
            distroarchseries_id=arch.id,
            architecturetag=arch.architecturetag,
            underlying_architecturetag=arch.underlying_architecturetag,
            formats=[BinaryPackageFormat.UDEB],
        )
        decoded = packages_bytes.decode("utf-8")
        self.assertIn("Package: my-udeb", decoded)
        self.assertNotIn("Package: my-deb", decoded)


class TestDirectPackagesIndexTranslations(TestNativePublishingBase):
    """Tests for Translation-en deduplication across architectures."""

    def test_arch_all_translation_deduplicated_across_archs(self):
        """An arch-all package produces only one Translation-en entry
        even when generate_packages_index is called for multiple arches
        with a shared seen_translations set."""
        pubs = self.getPubBinaries(
            architecturespecific=False,
            status=PackagePublishingStatus.PUBLISHED,
        )
        # Check that we indeed have more than one arch
        self.assertGreater(len(pubs), 1)

        store = IStore(BinaryPackagePublishingHistory)
        seen_translations = set()
        all_translation_stanzas = []

        for bpph in pubs:
            arch = bpph.distroarchseries
            _, translations_bytes = generate_packages_index(
                store,
                archive_id=bpph.archive_id,
                distroseries_id=arch.distroseries.id,
                pocket=bpph.pocket.value,
                component_id=bpph.component_id,
                distroarchseries_id=arch.id,
                architecturetag=arch.architecturetag,
                underlying_architecturetag=arch.underlying_architecturetag,
                separate_long_descriptions=True,
                seen_translations=seen_translations,
            )
            if translations_bytes:
                all_translation_stanzas.append(
                    translations_bytes.decode("utf-8")
                )

        combined = "".join(all_translation_stanzas)
        package_name = pubs[0].binarypackagerelease.binarypackagename.name
        self.assertEqual(
            1,
            combined.count("Package: %s" % package_name),
            "arch-all package appeared in Translation-en more than once",
        )


class TestExtraOverrides(TestNativePublishingBase):
    """Tests for the extra override reading and application."""

    def test_read_extra_overrides(self):
        """Override file is parsed into a dict."""
        _, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        with open(path, "w") as f:
            f.write("pkg-a\tTask\tubuntu-desktop\n")
            f.write("pkg-a\tTask\tubuntu-server\n")
            f.write("pkg-b\tBuild-Essential\tyes\n")

        result = read_extra_overrides(path)

        self.assertEqual(
            {
                "pkg-a": {"Task": "ubuntu-desktop, ubuntu-server"},
                "pkg-b": {"Build-Essential": "yes"},
            },
            result,
        )

    def test_read_extra_overrides_missing_file(self):
        """A missing file returns an empty dict."""
        result = read_extra_overrides("/nonexistent/path")
        self.assertEqual({}, result)


class TestDirectPackagesIndexMaintainer(TestNativePublishingBase):
    """Integration tests for Ubuntu Maintainer rewriting in
    generate_packages_index.
    """

    def _generate(self, bpph):
        store = IStore(BinaryPackagePublishingHistory)
        arch = bpph.distroarchseries
        packages_bytes, _ = generate_packages_index(
            store,
            archive_id=bpph.archive_id,
            distroseries_id=arch.distroseries.id,
            pocket=bpph.pocket.value,
            component_id=bpph.component_id,
            distroarchseries_id=arch.id,
            architecturetag=arch.architecturetag,
            underlying_architecturetag=arch.underlying_architecturetag,
        )
        return packages_bytes

    def test_debian_maintainer_rewritten_in_output(self):
        """A Debian source maintainer is replaced with Ubuntu Developers
        in the Packages index output.
        """
        pub_source = self.getPubSource(
            dsc_maintainer_rfc822="Person <person@debian.org>",
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs = self.getPubBinaries(
            pub_source=pub_source,
            status=PackagePublishingStatus.PUBLISHED,
        )
        packages_bytes = self._generate(pubs[0])
        decoded = packages_bytes.decode("utf-8")
        self.assertIn(
            "Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>",  # noqa: E501
            decoded,
        )
        self.assertNotIn("Maintainer: Person", decoded)

    def test_ubuntu_team_maintainer_kept_in_output(self):
        """A source maintainer with an ubuntu.com address is preserved
        verbatim in the Packages index output.
        """
        pub_source = self.getPubSource(
            dsc_maintainer_rfc822="Ubuntu Kernel Team <kernel-team@lists.ubuntu.com>",  # noqa: E501
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs = self.getPubBinaries(
            pub_source=pub_source,
            status=PackagePublishingStatus.PUBLISHED,
        )
        packages_bytes = self._generate(pubs[0])
        self.assertIn(
            b"Maintainer: Ubuntu Kernel Team <kernel-team@lists.ubuntu.com>",
            packages_bytes,
        )

    def test_legacy_maintainer_rewritten_in_output(self):
        """A pre-2009 Ubuntu maintainer address is updated to the
        current canonical address in the Packages index output.
        """
        pub_source = self.getPubSource(
            dsc_maintainer_rfc822="Ubuntu Core Developers <ubuntu-devel@lists.ubuntu.com>",  # noqa: E501
            status=PackagePublishingStatus.PUBLISHED,
        )
        pubs = self.getPubBinaries(
            pub_source=pub_source,
            status=PackagePublishingStatus.PUBLISHED,
        )
        packages_bytes = self._generate(pubs[0])
        decoded = packages_bytes.decode("utf-8")
        self.assertIn(
            "Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>",  # noqa: E501
            decoded,
        )
        self.assertNotIn("Maintainer: Ubuntu Core Developers", decoded)
