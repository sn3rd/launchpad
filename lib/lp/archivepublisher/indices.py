# Copyright 2009-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "IndexStanzaFields",
    "build_binary_stanza_fields",
    "build_source_stanza_fields",
    "build_translations_stanza_fields",
    "generate_packages_index",
    "generate_sources_index",
    "get_overrides_for_arch",
    "read_extra_overrides",
]

import hashlib
import json
import os.path
import re
from collections import OrderedDict

from lp.soyuz.enums import PackagePublishingPriority, PackagePublishingStatus
from lp.soyuz.model.publishing import makePoolPath


class IndexStanzaFields:
    """Store and format ordered Index Stanza fields."""

    def __init__(self):
        self._names_lower = set()
        self.fields = []

    def append(self, name, value):
        """Append an (field, value) tuple to the internal list.

        Then we can use the FIFO-like behaviour in makeOutput().
        """
        if name.lower() in self._names_lower:
            return
        self._names_lower.add(name.lower())
        self.fields.append((name, value))

    def extend(self, entries):
        """Extend the internal list with the key-value pairs in entries."""
        for name, value in entries:
            self.append(name, value)

    def makeOutput(self):
        """Return a line-by-line aggregation of appended fields.

        Empty fields values will cause the exclusion of the field.
        The output order will preserve the insertion order, FIFO.
        """
        output_lines = []
        for name, value in self.fields:
            if not value:
                continue

            # do not add separation space for the special file list fields.
            if name not in (
                "Files",
                "Checksums-Sha1",
                "Checksums-Sha256",
                "Checksums-Sha512",
            ):
                value = " %s" % value

            # XXX Michael Nelson 20090930 bug=436182. We have an issue
            # in the upload parser that has
            #   1. introduced '\n' at the end of multiple-line-spanning
            #      fields, such as dsc_binaries, but potentially others,
            #   2. stripped the leading space from each subsequent line
            #      of dsc_binaries values that span multiple lines.
            # This is causing *incorrect* Source indexes to be created.
            # This work-around can be removed once the fix for bug 436182
            # is in place and the tainted data has been cleaned.
            # First, remove any trailing \n or spaces.
            value = value.rstrip()

            # Second, as we have corrupt data where subsequent lines
            # of values spanning multiple lines are not preceded by a
            # space, we ensure that any \n in the value that is *not*
            # followed by a white-space character has a space inserted.
            value = re.sub(r"\n(\S)", r"\n \1", value)

            output_lines.append("%s:%s" % (name, value))

        return "\n".join(output_lines)


def format_file_list(lst):
    return "".join("\n %s %s %s" % ((h,) + f) for (h, f) in lst)


def format_description(summary, description):
    # description field in index is an association of summary and
    # description or the summary only if include_long_descriptions
    # is false, as:
    #
    # Description: <SUMMARY>\n
    #  <DESCRIPTION L1>
    #  ...
    #  <DESCRIPTION LN>
    descr_lines = [line.lstrip() for line in description.splitlines()]
    bin_description = "%s\n %s" % (summary, "\n ".join(descr_lines))
    return bin_description


def build_source_stanza_fields(spr, component, section, include_sha512=False):
    """Build a map of fields to be included in a Sources file.

    :param include_sha512: If True, include Checksums-Sha512. Defaults to
        False because SHA-512 has not been backfilled for all archives (e.g.
        PPAs).
    """
    # Special fields preparation.
    pool_path = makePoolPath(spr.name, component.name)
    files_list = []
    sha1_list = []
    sha256_list = []
    sha512_list = [] if include_sha512 else None
    for spf in spr.files:
        common = (spf.libraryfile.content.filesize, spf.libraryfile.filename)
        files_list.append((spf.libraryfile.content.md5, common))
        sha1_list.append((spf.libraryfile.content.sha1, common))
        sha256_list.append((spf.libraryfile.content.sha256, common))
        if sha512_list is not None:
            sha512_list.append((spf.libraryfile.content.sha512, common))
    user_defined_fields = OrderedDict(
        [(key.lower(), (key, value)) for key, value in spr.user_defined_fields]
    )
    # Filling stanza options.
    fields = IndexStanzaFields()
    fields.append("Package", spr.name)
    fields.append("Binary", spr.dsc_binaries)
    fields.append("Version", spr.version)
    fields.append("Section", section.name)
    fields.append("Maintainer", spr.dsc_maintainer_rfc822)
    fields.append("Build-Depends", spr.builddepends)
    fields.append("Build-Depends-Indep", spr.builddependsindep)
    if "build-depends-arch" in user_defined_fields:
        fields.append(
            "Build-Depends-Arch",
            user_defined_fields.pop("build-depends-arch")[1],
        )
    fields.append("Build-Conflicts", spr.build_conflicts)
    fields.append("Build-Conflicts-Indep", spr.build_conflicts_indep)
    if "build-conflicts-arch" in user_defined_fields:
        fields.append(
            "Build-Conflicts-Arch",
            user_defined_fields.pop("build-conflicts-arch")[1],
        )
    fields.append("Architecture", spr.architecturehintlist)
    fields.append("Standards-Version", spr.dsc_standards_version)
    fields.append("Format", spr.dsc_format)
    fields.append("Directory", pool_path)
    fields.append("Files", format_file_list(files_list))
    fields.append("Checksums-Sha1", format_file_list(sha1_list))
    fields.append("Checksums-Sha256", format_file_list(sha256_list))
    if sha512_list is not None:
        fields.append("Checksums-Sha512", format_file_list(sha512_list))
    fields.append("Homepage", spr.homepage)
    fields.extend(user_defined_fields.values())

    return fields


def build_binary_stanza_fields(
    bpr,
    component,
    section,
    priority,
    phased_update_percentage,
    separate_long_descriptions=False,
    include_sha512=False,
):
    """Build a map of fields to be included in a Packages file.

    :param separate_long_descriptions: if True, the long description will
    be omitted from the stanza and Description-md5 will be included.
    :param include_sha512: If True, include SHA512. Defaults to False
        because SHA-512 has not been backfilled for all archives (e.g. PPAs).
    """
    spr = bpr.build.source_package_release

    # binaries have only one file, the DEB
    bin_file = bpr.files[0]
    bin_filename = bin_file.libraryfile.filename
    bin_size = bin_file.libraryfile.content.filesize
    bin_md5 = bin_file.libraryfile.content.md5
    bin_sha1 = bin_file.libraryfile.content.sha1
    bin_sha256 = bin_file.libraryfile.content.sha256
    bin_sha512 = (
        bin_file.libraryfile.content.sha512 if include_sha512 else None
    )
    bin_filepath = os.path.join(
        makePoolPath(spr.name, component.name), bin_filename
    )
    description = format_description(bpr.summary, bpr.description)
    # Our formatted description isn't \n-terminated, but apt
    # considers the trailing \n to be part of the data to hash.
    bin_description_md5 = hashlib.md5(  # nosec B324
        description.encode("utf-8") + b"\n"
    ).hexdigest()
    if separate_long_descriptions:
        # If distroseries.include_long_descriptions is False, the
        # description should be the summary
        bin_description = bpr.summary
    else:
        bin_description = description

    # Dealing with architecturespecific field.
    # Present 'all' in every archive index for architecture
    # independent binaries.
    if bpr.architecturespecific:
        if bpr.build.distro_arch_series.underlying_architecturetag:
            architecture = (
                bpr.build.distro_arch_series.underlying_architecturetag
            )
        else:
            architecture = bpr.build.distro_arch_series.architecturetag
    else:
        architecture = "all"

    architecture_variant = bpr.getUserDefinedField("Architecture-Variant")

    essential = None
    if bpr.essential:
        essential = "yes"

    source = None
    if bpr.version != spr.version:
        source = "%s (%s)" % (spr.name, spr.version)
    elif bpr.name != spr.name:
        source = spr.name

    fields = IndexStanzaFields()
    fields.append("Package", bpr.name)
    fields.append("Source", source)
    fields.append("Priority", priority.title.lower())
    fields.append("Section", section.name)
    fields.append("Installed-Size", bpr.installedsize)
    fields.append("Maintainer", spr.dsc_maintainer_rfc822)
    fields.append("Architecture", architecture)
    if architecture_variant is not None:
        fields.append("Architecture-Variant", architecture_variant)
    fields.append("Version", bpr.version)
    fields.append("Recommends", bpr.recommends)
    fields.append("Replaces", bpr.replaces)
    fields.append("Suggests", bpr.suggests)
    fields.append("Provides", bpr.provides)
    fields.append("Depends", bpr.depends)
    fields.append("Conflicts", bpr.conflicts)
    fields.append("Pre-Depends", bpr.pre_depends)
    fields.append("Enhances", bpr.enhances)
    fields.append("Breaks", bpr.breaks)
    fields.append("Essential", essential)
    fields.append("Filename", bin_filepath)
    fields.append("Size", bin_size)
    fields.append("MD5sum", bin_md5)
    fields.append("SHA1", bin_sha1)
    fields.append("SHA256", bin_sha256)
    if bin_sha512 is not None:
        fields.append("SHA512", bin_sha512)
    fields.append("Phased-Update-Percentage", phased_update_percentage)
    fields.append("Description", bin_description)
    if separate_long_descriptions:
        fields.append("Description-md5", bin_description_md5)
    if bpr.user_defined_fields:
        fields.extend(bpr.user_defined_fields)

    # XXX cprov 2006-11-03: the extra override fields (Bugs, Origin and
    # Task) included in the template be were not populated.
    # When we have the information this will be the place to fill them.

    return fields


def build_translations_stanza_fields(bpr, packages):
    """Build a map of fields to be included in a Translation-en file.

    :param packages: a set of (Package, Description-md5) tuples used to
        determine if a package has already been added to the translation
        file. The (Package, Description-md5) tuple will be added if it
        doesn't already exist.
    """
    bin_description = format_description(bpr.summary, bpr.description)
    # Our formatted description isn't \n-terminated, but apt
    # considers the trailing \n to be part of the data to hash.
    bin_description_md5 = hashlib.md5(  # nosec B324
        bin_description.encode("utf-8") + b"\n"
    ).hexdigest()
    if (bpr.name, bin_description_md5) not in packages:
        fields = IndexStanzaFields()
        fields.append("Package", bpr.name)
        fields.append("Description-md5", bin_description_md5)
        fields.append("Description-en", bin_description)
        packages.add((bpr.name, bin_description_md5))

        return fields
    else:
        return None


def generate_sources_index(
    store, archive_id, distroseries_id, pocket, component_id, overrides=None
):
    """Generate a Sources index file directly from the database.

    Queries the database without going through the Storm ORM to avoid
    per-row object loading overhead.

    :param store: A Storm Store.
    :param archive_id: The archive ID to generate for.
    :param distroseries_id: The distroseries ID to generate for.
    :param pocket: The pocket integer value.
    :param component_id: The component ID to generate for.
    :param overrides: Optional dict from read_extra_overrides. When
        provided, extra fields (e.g. Task, Build-Essential) are appended to
        each matching stanza before serialisation.
    :return: bytes content of the Sources index file.
    """
    rows = store.execute(
        """
        -- Pre-aggregate the highest binary priority per source package
        WITH src_priorities AS (
            SELECT
                bpb2.source_package_release AS sprelease_id,
                MAX(bpph2.priority)         AS max_priority
            FROM binarypackagepublishinghistory bpph2
            JOIN binarypackagerelease bpr2
                ON bpph2.binarypackagerelease = bpr2.id
            JOIN binarypackagebuild bpb2
                ON bpr2.build = bpb2.id
            JOIN distroarchseries das2
                ON bpph2.distroarchseries = das2.id
            WHERE bpph2.archive = %s
                AND bpph2.pocket = %s
                AND das2.distroseries = %s
                AND bpph2.status = %s
            GROUP BY bpb2.source_package_release
        )
        SELECT
            spn.name,
            spr.dsc_binaries,
            spr.version,
            s.name AS section,
            spr.dsc_maintainer_rfc822,
            spr.builddepends,
            spr.builddependsindep,
            spr.build_conflicts,
            spr.build_conflicts_indep,
            spr.architecturehintlist,
            spr.dsc_standards_version,
            spr.dsc_format,
            c.name AS component,
            spr.homepage,
            spr.user_defined_fields,
            -- Aggregate all source files into a
            -- single JSON array so we can emit Files/Checksums-* without
            -- issuing a separate query per source package
            (
                SELECT json_agg(
                    json_build_object(
                        'md5', lfc2.md5,
                        'sha1', lfc2.sha1,
                        'sha256', lfc2.sha256,
                        'sha512', lfc2.sha512,
                        'size', lfc2.filesize,
                        'filename', lfa2.filename
                    ) ORDER BY sprf2.libraryfile
                )
                FROM sourcepackagereleasefile sprf2
                JOIN libraryfilealias lfa2 ON sprf2.libraryfile = lfa2.id
                JOIN libraryfilecontent lfc2 ON lfa2.content = lfc2.id
                WHERE sprf2.sourcepackagerelease = spr.id
            ) AS files,
            -- Priority derived from the CTE above via LEFT JOIN.
            -- Defaults to 'extra' when no published binaries exist.
            CASE COALESCE(sp.max_priority, 10)
                WHEN 50 THEN 'required'
                WHEN 40 THEN 'important'
                WHEN 30 THEN 'standard'
                WHEN 20 THEN 'optional'
                ELSE 'extra'
            END AS priority
        FROM sourcepackagepublishinghistory spph
        JOIN sourcepackagerelease spr ON spph.sourcepackagerelease = spr.id
        JOIN sourcepackagename spn ON spr.sourcepackagename = spn.id
        JOIN section s ON spph.section = s.id
        JOIN component c ON spph.component = c.id
        LEFT JOIN src_priorities sp ON sp.sprelease_id = spr.id
        WHERE
            spph.archive = %s
            AND spph.distroseries = %s
            AND spph.pocket = %s
            AND spph.component = %s
            AND spph.status = %s
        ORDER BY spn.name, spr.version
        """,
        (
            archive_id,
            pocket,
            distroseries_id,
            PackagePublishingStatus.PUBLISHED.value,
            archive_id,
            distroseries_id,
            pocket,
            component_id,
            PackagePublishingStatus.PUBLISHED.value,
        ),
    ).get_all()

    if not rows:
        return b""

    stanzas = []
    for row in rows:
        (
            name,
            dsc_binaries,
            version,
            section,
            dsc_maintainer_rfc822,
            builddepends,
            builddependsindep,
            build_conflicts,
            build_conflicts_indep,
            architecturehintlist,
            dsc_standards_version,
            dsc_format,
            component,
            homepage,
            user_defined_fields_json,
            files_json,
            priority,
        ) = row

        raw_udf = (
            json.loads(user_defined_fields_json)
            if user_defined_fields_json
            else []
        )
        udf_ordered = OrderedDict(
            [(key.lower(), (key, value)) for key, value in raw_udf]
        )

        # Files are aggregated into a JSON array by the SQL subquery.
        # Each entry becomes four (hash, (size, filename)) tuples
        # for the MD5, SHA1, SHA-256 and SHA-512 hahes.
        raw_files = files_json or []
        files_list = []
        sha1_list = []
        sha256_list = []
        sha512_list = []
        for f in raw_files:
            common = (f["size"], f["filename"])
            files_list.append((f["md5"], common))
            sha1_list.append((f["sha1"], common))
            sha256_list.append((f["sha256"], common))
            if f["sha512"] is not None:
                sha512_list.append((f["sha512"], common))

        pool_path = makePoolPath(name, component)

        fields = IndexStanzaFields()
        fields.append("Package", name)
        fields.append("Binary", dsc_binaries)
        fields.append("Version", version)
        fields.append("Priority", priority)
        fields.append("Section", section)
        fields.append("Maintainer", dsc_maintainer_rfc822)
        fields.append("Build-Depends", builddepends)
        fields.append("Build-Depends-Indep", builddependsindep)
        if "build-depends-arch" in udf_ordered:
            fields.append(
                "Build-Depends-Arch",
                udf_ordered.pop("build-depends-arch")[1],
            )
        fields.append("Build-Conflicts", build_conflicts)
        fields.append("Build-Conflicts-Indep", build_conflicts_indep)
        if "build-conflicts-arch" in udf_ordered:
            fields.append(
                "Build-Conflicts-Arch",
                udf_ordered.pop("build-conflicts-arch")[1],
            )
        fields.append("Architecture", architecturehintlist)
        fields.append("Standards-Version", dsc_standards_version)
        fields.append("Format", dsc_format)
        fields.append("Directory", pool_path)
        fields.append("Files", format_file_list(files_list))
        fields.append("Checksums-Sha1", format_file_list(sha1_list))
        fields.append("Checksums-Sha256", format_file_list(sha256_list))
        if len(sha512_list) == len(raw_files):
            fields.append("Checksums-Sha512", format_file_list(sha512_list))
        fields.append("Homepage", homepage)
        fields.extend(udf_ordered.values())
        if overrides:
            for header, value in overrides.get(name, {}).items():
                fields.append(header, value)

        stanzas.append(fields.makeOutput())

    return ("\n\n".join(stanzas) + "\n\n").encode("utf-8")


_UBUNTU_MAINTAINER = (
    "Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>"
)

# Prior May 2009 these Maintainers were used:
_PREVIOUS_UBUNTU_MAINTAINERS = frozenset(
    [
        "ubuntu core developers <ubuntu-devel@lists.ubuntu.com>",
        "ubuntu core developers <ubuntu-devel-discuss@lists.ubuntu.com>",
        "ubuntu motu developers <ubuntu-motu@lists.ubuntu.com>",
    ]
)


def _rewrite_maintainer_for_ubuntu(maintainer):
    """Apply the Ubuntu Maintainer-field rewriting policy to a binary package.

    Mirrors the logic in ubuntutools.update_maintainer

    * Legacy pre-2009 Ubuntu team addresses are updated to the current
      canonical value.
    * If the address already ends with ubuntu.com> it is a current
      Ubuntu team and is kept verbatim.
    * Everything else is replaced with the canonical Ubuntu Developers address.
    """
    if not maintainer:
        return maintainer
    if maintainer.strip().lower() in _PREVIOUS_UBUNTU_MAINTAINERS:
        return _UBUNTU_MAINTAINER
    if maintainer.strip().endswith("ubuntu.com>"):
        return maintainer
    return _UBUNTU_MAINTAINER


def generate_packages_index(
    store,
    archive_id,
    distroseries_id,
    pocket,
    component_id,
    distroarchseries_id,
    architecturetag,
    underlying_architecturetag,
    separate_long_descriptions=False,
    overrides=None,
    formats=None,
    seen_translations=None,
):
    """Generate a Packages index file directly from the database.

    Queries the database without going through the Storm ORM to avoid
    per-row object loading overhead.

    :param store: A Storm Store.
    :param archive_id: The archive ID to generate for.
    :param distroseries_id: The distroseries ID to generate for.
    :param pocket: The pocket integer value.
    :param component_id: The component ID to generate for.
    :param distroarchseries_id: The DistroArchSeries ID.
    :param architecturetag: The architecture tag (e.g. 'amd64').
    :param underlying_architecturetag: The underlying architecture tag,
        or None.
    :param separate_long_descriptions: If True, omit long descriptions
        from the stanza and include Description-md5.
    :param overrides: Optional dict from read_extra_overrides. When
        provided, extra fields (e.g. Task, Build-Essential) are appended to
        each matching stanza before serialisation.
    :param formats: Optional list of BinaryPackageFormat items to
        restrict to.
    :param seen_translations: Optional set of (package, description_md5)
        tuples shared across multiple calls (e.g. across architectures)
        to deduplicate Translation-en entries for arch-all packages.
        A new set is created internally when not provided.
    :return: A tuple of (packages_bytes, translations_bytes) where
        translations_bytes is non-empty only when separate_long_descriptions
        is True.
    """
    format_list = [f.value for f in formats] if formats is not None else None
    rows = store.execute(
        """
        SELECT
            bpn.name AS package,
            bpr.version,
            bpr.summary,
            bpr.description,
            bpr.architecturespecific,
            bpr.installedsize,
            bpr.recommends,
            bpr.replaces,
            bpr.suggests,
            bpr.provides,
            bpr.depends,
            bpr.conflicts,
            bpr.pre_depends,
            bpr.enhances,
            bpr.breaks,
            bpr.essential,
            bpr.homepage,
            bpr.user_defined_fields AS bpr_udf,
            spr.dsc_maintainer_rfc822,
            spn.name AS source_name,
            spr.version AS source_version,
            bpph.priority,
            bpph.phased_update_percentage,
            c.name AS component,
            s.name AS section,
            lfa.filename,
            lfc.filesize,
            lfc.md5,
            lfc.sha1,
            lfc.sha256,
            lfc.sha512,
            bpr.binpackageformat
        FROM binarypackagepublishinghistory bpph
        JOIN binarypackagerelease bpr
            ON bpph.binarypackagerelease = bpr.id
        JOIN binarypackagename bpn
            ON bpr.binarypackagename = bpn.id
        JOIN binarypackagebuild bpb
            ON bpr.build = bpb.id
        JOIN sourcepackagerelease spr
            ON bpb.source_package_release = spr.id
        JOIN sourcepackagename spn
            ON spr.sourcepackagename = spn.id
        JOIN section s ON bpph.section = s.id
        JOIN component c ON bpph.component = c.id
        JOIN binarypackagefile bpf
            ON bpf.binarypackagerelease = bpr.id
        JOIN libraryfilealias lfa ON bpf.libraryfile = lfa.id
        JOIN libraryfilecontent lfc ON lfa.content = lfc.id
        WHERE
            bpph.archive = %s
            AND bpph.distroarchseries = %s
            AND bpph.pocket = %s
            AND bpph.component = %s
            AND bpph.status = %s
            AND (%s IS NULL OR bpr.binpackageformat = ANY(%s))
        ORDER BY bpn.name, bpr.version
        """,
        (
            archive_id,
            distroarchseries_id,
            pocket,
            component_id,
            PackagePublishingStatus.PUBLISHED.value,
            format_list,
            format_list,
        ),
    ).get_all()

    if not rows:
        return b"", b""

    stanzas = []
    translation_stanzas = []
    if seen_translations is None:
        seen_translations = set()

    for row in rows:
        (
            package,
            version,
            summary,
            description,
            architecturespecific,
            installedsize,
            recommends,
            replaces,
            suggests,
            provides,
            depends,
            conflicts,
            pre_depends,
            enhances,
            breaks,
            essential,
            homepage,
            bpr_udf_json,
            dsc_maintainer_rfc822,
            source_name,
            source_version,
            priority_value,
            phased_update_percentage,
            component,
            section,
            filename,
            filesize,
            md5,
            sha1,
            sha256,
            sha512,
            binpackageformat,
        ) = row

        # Map priority integer to string name
        priority_str = PackagePublishingPriority.items[
            priority_value
        ].title.lower()

        pool_path = makePoolPath(source_name, component)
        bin_filepath = os.path.join(pool_path, filename)

        bin_description = format_description(summary, description)
        bin_description_md5 = hashlib.md5(  # nosec B324
            bin_description.encode("utf-8") + b"\n"
        ).hexdigest()
        if separate_long_descriptions:
            display_description = summary
        else:
            display_description = bin_description

        # Architecture: 'all' for arch-independent, otherwise the
        # (underlying) architecture tag.
        if architecturespecific:
            if underlying_architecturetag:
                architecture = underlying_architecturetag
            else:
                architecture = architecturetag
        else:
            architecture = "all"

        raw_udf = json.loads(bpr_udf_json) if bpr_udf_json else []

        essential_str = "yes" if essential else None

        # Source field get somitted when binary name==source name and
        # versions match
        source = None
        if version != source_version:
            source = "%s (%s)" % (source_name, source_version)
        elif package != source_name:
            source = source_name

        fields = IndexStanzaFields()
        fields.append("Package", package)
        fields.append("Source", source)
        fields.append("Priority", priority_str)
        fields.append("Section", section)
        fields.append("Installed-Size", installedsize)
        fields.append(
            "Maintainer", _rewrite_maintainer_for_ubuntu(dsc_maintainer_rfc822)
        )
        fields.append("Architecture", architecture)
        fields.append("Version", version)
        fields.append("Recommends", recommends)
        fields.append("Replaces", replaces)
        fields.append("Suggests", suggests)
        fields.append("Provides", provides)
        fields.append("Depends", depends)
        fields.append("Conflicts", conflicts)
        fields.append("Pre-Depends", pre_depends)
        fields.append("Enhances", enhances)
        fields.append("Breaks", breaks)
        fields.append("Essential", essential_str)
        fields.append("Filename", bin_filepath)
        fields.append("Size", filesize)
        fields.append("MD5sum", md5)
        fields.append("SHA1", sha1)
        fields.append("SHA256", sha256)
        if sha512 is not None:
            fields.append("SHA512", sha512)
        fields.append("Phased-Update-Percentage", phased_update_percentage)
        fields.append("Homepage", homepage)
        fields.append("Description", display_description)
        if separate_long_descriptions:
            fields.append("Description-md5", bin_description_md5)
        if raw_udf:
            fields.extend(raw_udf)
        if overrides:
            for header, value in overrides.get(package, {}).items():
                fields.append(header, value)

        stanzas.append(fields.makeOutput())

        # Build Translation-en stanza if needed.
        if separate_long_descriptions:
            key = (package, bin_description_md5)
            if key not in seen_translations:
                seen_translations.add(key)
                t_fields = IndexStanzaFields()
                t_fields.append("Package", package)
                t_fields.append("Description-md5", bin_description_md5)
                t_fields.append("Description-en", bin_description)
                translation_stanzas.append(t_fields.makeOutput())

    packages_bytes = (
        ("\n\n".join(stanzas) + "\n\n").encode("utf-8") if stanzas else b""
    )
    translations_bytes = (
        ("\n\n".join(translation_stanzas) + "\n\n").encode("utf-8")
        if translation_stanzas
        else b""
    )
    return packages_bytes, translations_bytes


def read_extra_overrides(path):
    """Read a more-extra.override file.

    These files are produced by Germinate (finalize.d/20-germinate)
    and contain per-package field overrides for Task, Build-Essential,
    and Supported.

    File format (tab-separated):

        package_name    Header    value

    Multiple entries for the same (package, Header) are comma-joined,
    matching the behaviour in FTPArchiveHandler.generateOverrideForComponent.

    :param path: Filesystem path to the override file.
    :return: {package_name: {Header: value, ...}, ...}
    """
    overrides = {}
    if not os.path.exists(path):
        return overrides
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            package, header, value = parts
            pkg_overrides = overrides.setdefault(package, {})
            header_values = pkg_overrides.get(header)
            if header_values is not None:
                pkg_overrides[header] = header_values + ", " + value
            else:
                pkg_overrides[header] = value
    return overrides


def get_overrides_for_arch(overrides, archtag):
    """Filter extra overrides for a specific architecture.

    The generated extra override file contains three kinds of keys:

    * Bare package names, for the Origin, Bugs fields.
    * pkg/arch for Phased-Update-Percentage (already in SQL).
    * pkg/arch for Task, Build-Essential (from Germinate).

    This function returns a new dict keyed by bare package name,
    containing only the entries that apply to archtag. Bare name
    entries are included as-is, and pkg/arch entries are included
    only when the arch matches, re-keyed to the bare name.

    :param overrides: dict from read_extra_overrides
    :param archtag: Architecture tag string (e.g. 'amd64').
    :return: {package_name: {Header: value, ...}, ...}
    """
    result = {}
    for key, fields in overrides.items():
        if "/" in key:
            pkg, arch = key.split("/", 1)
            if arch != archtag:
                continue
        else:
            pkg = key
        pkg_entry = result.setdefault(pkg, {})
        for header, value in fields.items():
            if header not in pkg_entry:
                pkg_entry[header] = value
    return result
