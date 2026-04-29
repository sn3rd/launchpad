#  Copyright 2022 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

"""
A copy of `cve_lib` module from `ubuntu-cve-tracker`
(only the code for parsing CVE files).
"""
import codecs
import json
import os
import re
import sys
from functools import cmp_to_key, lru_cache

import yaml

CVE_FILTER_NAME = "cve_filter_name"
CVE_FILTER_ARGS = "cve_filter_args"

GLOBAL_TAGS_KEY = '*'


uct_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
contrib_dir = os.path.dirname(os.path.realpath(__file__))

subprojects_dir = os.path.join(uct_dir, "subprojects")
subprojects_file = os.path.join(contrib_dir, "subprojects.json")

PRODUCT_UBUNTU = "ubuntu"
PRODUCT_ESM = ["esm", "esm-infra", "esm-apps", "esm-infra-legacy", "esm-apps-legacy"]
PRODUCT_FIPS = ["fips", "fips-updates", "fips-preview"]
PRIORITY_REASON_REQUIRED = ["low", "high", "critical"]
PRIORITY_REASON_DATE_START = "2023-07-11"

# common to all scripts
# these get populated by the contents of subprojects defined below
all_releases = []
eol_releases = []
external_releases = []
interim_releases = []
releases = []
devel_release = ""
active_external_subprojects = {}
eol_external_subprojects = {}

# known subprojects which are supported by cve_lib - in general each
# subproject is defined by the combination of a product and series as
# <product/series>.
#
# For each subproject, it is either internal (ie is part of this static
# dict) or external (found dynamically at runtime by
# load_external_subprojects()).
#
# eol specifies whether the subproject is now end-of-life.  packages
# specifies list of files containing the names of supported packages for the
# subproject. alias defines an alternate preferred name for the subproject
# (this is often used to support historical names for projects etc).
with open(subprojects_file, 'r') as f:
    subprojects = json.load(f)

@lru_cache(maxsize=None)
def product_series(rel):
    """Return the product,series tuple for rel."""
    if rel in external_releases:
        product = subprojects[rel]['product']
        series = subprojects[rel]['release']
    else:
        series = ""
        parts = rel.split('/')
        if len(parts) == 3:
            # for example: ros-esm/focal/foxy
            product = parts[0]
            series = parts[1]
        elif len(parts) == 2:
            product = parts[0]
            series = parts[1]
            # handle trusty/esm case
            if product in releases:
                product, series = series, product
        elif parts[0] in releases:
            # by default ubuntu releases have an omitted ubuntu product
            # this avoids cases like snaps
            product = PRODUCT_UBUNTU
            series = parts[0]
        else:
            product = parts[0]
    return product, series


# get the subproject details for rel along with it's canonical name, product and series
@lru_cache(maxsize=None)
def get_subproject_details(rel):
    """Return the canonical name,product,series,details tuple for rel."""
    canon, product, series, details, release = None, None, None, None, None
    if rel in subprojects:
        details = subprojects[rel]
        release = rel
    else:
        for r in subprojects:
            try:
                if subprojects[r]["alias"] == rel \
                  or (rel == "devel" and subprojects[r]["devel"]):
                    details = subprojects[r]
                    release = r
                    break
            except KeyError:
                pass
        else:
            # support subproject versions: if no match was found, and the release name
            # contains a slash, use the token before the slash to
            # look for a subproject with a matching alias
            if isinstance(rel, str) and "/" in rel:
                parent = rel.split("/", 1)[0]  # e.g. from "focal/foxy", get "focal"
                for r in subprojects:
                    if subprojects[r].get("alias") == parent:
                        details = subprojects[r]
                        release = r
                        break

    if release:
        product, series = product_series(release)
        canon = release
    return canon, product, series, details

def release_alias(rel):
    """Return the alias for rel or just rel if no alias is defined."""
    alias = rel
    _, _, _, details = get_subproject_details(rel)
    try:
        alias = details["alias"]
    except (KeyError, TypeError):
        pass
    return alias

def release_parent(rel):
    """Return the parent for rel or None if no parent is defined."""
    parent = None
    _, _, _, details = get_subproject_details(rel)
    try:
        parent = release_alias(details["parent"])
    except (KeyError, TypeError):
        pass
    return parent


def release_progenitor(rel):
    parent = release_parent(rel)
    while release_parent(parent):
        parent = release_parent(parent)

    return parent


def release_version(rel):
    """Return the version for rel or its parent if it doesn't have one."""
    version = 0.0
    _, _, _, details = get_subproject_details(rel)
    if details:
        try:
            version = details["version"]
        except KeyError:
            return release_version(release_progenitor(rel))
    return version


def get_external_subproject_cve_dir(subproject):
    """Get the directory where CVE files are stored for the subproject.

    Get the directory where CVE files are stored for a subproject. In
    general this is within the higher level project directory, not within
    the specific subdirectory for the particular series that defines this
    subproject.

    """
    rel, product, _, _ = get_subproject_details(subproject)
    if rel not in external_releases:
        raise ValueError("%s is not an external subproject" % rel)
    # CVEs live in the product dir
    return os.path.join(subprojects_dir, product)

def get_external_subproject_dir(subproject):
    """Get the directory for the given external subproject."""
    rel, _, _, _ = get_subproject_details(subproject)
    if rel not in external_releases:
        raise ValueError("%s is not an external subproject" % rel)
    return os.path.join(subprojects_dir, rel)

def read_external_subproject_config(subproject_dir):
    """Read and return the configuration for the given subproject directory."""
    config_yaml = os.path.join(subproject_dir, "config.yml")
    with open(config_yaml) as cfg:
        return yaml.safe_load(cfg)

def read_external_subproject_details(subproject):
    """Read and return the project details for the given subproject."""
    sp_dir = get_external_subproject_dir(subproject)
    # project.yml is located in the top level folder for the subproject
    project_dir = sp_dir[:sp_dir.rfind("/")]
    project_yaml = os.path.join(project_dir, "project.yml")
    if os.path.isfile(project_yaml):
        with open(project_yaml) as cfg:
            return yaml.safe_load(cfg)

def find_files_recursive(path, name):
    """Return a list of all files under path with name."""
    matches = []
    for root, _, files in os.walk(path, followlinks=True):
        for f in files:
            if f == name:
                filepath = os.path.join(root, f)
                matches.append(filepath)
    return matches

def find_external_subproject_cves(cve, realpath=False):
    """Return the list of external subproject CVE snippets for the given CVE."""
    cves = []
    # Use the cache if it's not empty
    if subproject_dir_cache_cves:
        if cve not in subproject_dir_cache_cves:
            return cves
        for entry in subproject_dir_cache_dirs:
            path = os.path.join(entry, cve)
            if os.path.exists(path):
                if realpath:
                    path = os.path.realpath(path)
                if path not in cves:
                    cves.append(path)
    else:
        for rel in external_releases:
            # fallback to the series specific subdir rather than just the
            # top-level project directory even though this is preferred
            for path in [get_external_subproject_dir(rel),
                         get_external_subproject_cve_dir(rel)]:
                path = os.path.join(path, cve)
                if os.path.exists(path):
                    if realpath:
                        path = os.path.realpath(path)
                    if path not in cves:
                        cves.append(path)
                    break

    return cves

# Keys in config.yml for a external subproject
# should follow the same as any other subproject
# except for the extra 'product' and 'release' keys.
MANDATORY_EXTERNAL_SUBPROJECT_KEYS = ['cve_triage', 'cve_patching', 'cve_notification', 'security_updates_notification', 'binary_copies_only', 'seg_support', 'owners', 'subprojects']
MANDATORY_EXTERNAL_SUBPROJECT_PPA_KEYS = ['ppas', 'data_formats', 'product', 'release', 'supported_packages']
OPTIONAL_EXTERNAL_SUBPROJECT_PPA_KEYS =  ['parent', 'name', 'codename', 'description', 'aliases', 'archs', 'lp_distribution', 'staging_updates_ppa', 'staging_lp_distribution', 'build_ppa', 'build_lp_distribution']

def load_external_subprojects(strict=False):
    """Search for and load subprojects into the global subprojects dict.

    Search for and load subprojects into the global subprojects dict.

    A subproject is defined as a directory which resides within
    subprojects_dir and references a supported.txt file and a PPA.
    This information is stored in config.yml, which contains all the
    information in regards the subproject. It can also contain
    a project.yml file which specifies metadata for the project as well
    as snippet CVE files. By convention, a subproject is usually defined
    as the combination of a product and series, ie:

    esm-apps/focal

    as such in this case there would expect to be within subprojects_dir a
    directory called esm-apps/ and within that, in the config.yml, an entry
    of type 'esm-apps/focal'. Inside this entry, a reference to the designated
    supported.txt file, which would list the packages which are supported by
    the esm-apps/focal subproject. By convention, snippet CVE files should
    reside within the esm-apps/ project directory.

    The strict argument determines whether to continue processing if
    there are any missing components to the subproject or not.
    """
    for config_yaml in find_files_recursive(subprojects_dir, "config.yml"):
        subproject_path = config_yaml[:-len("config.yml")-1]
        # use config to populate other parts of the
        # subproject settings
        main_config = read_external_subproject_config(subproject_path)

        for key in MANDATORY_EXTERNAL_SUBPROJECT_KEYS:
            if key not in main_config:
                error_msg = '%s missing "%s" field.' % (subproject_path, key)
                if strict:
                    raise ValueError(error_msg)
                else:
                    print(error_msg, file=sys.stderr)

        for subproject in main_config['subprojects']:
            config = main_config['subprojects'][subproject]
            if 'product' not in config or 'release' not in config:
                error_msg = '%s: missing "product" or "release".' % (subproject_path)
                if strict:
                    raise ValueError(error_msg)
                else:
                    print(error_msg, file=sys.stderr)

            external_releases.append(subproject)
            subprojects.setdefault(subproject, {"packages": []})
            # an external subproject can append to an internal one
            subprojects[subproject]["packages"].append(\
                os.path.join(subproject_path, config['supported_packages']))

            # check if aliases for packages exist
            if 'aliases' in config:
                subprojects[subproject].setdefault("aliases", \
                    os.path.join(subproject_path, config['aliases']))

            for key in MANDATORY_EXTERNAL_SUBPROJECT_PPA_KEYS + OPTIONAL_EXTERNAL_SUBPROJECT_PPA_KEYS:
                if key in config:
                    subprojects[subproject].setdefault(key, config[key])
                elif key in OPTIONAL_EXTERNAL_SUBPROJECT_PPA_KEYS:
                    _, _, _, original_release_details = get_subproject_details(config['release'])
                    if original_release_details and key in original_release_details:
                        subprojects[subproject].setdefault(key, original_release_details[key])
                else:
                    error_msg = '%s missing "%s" field.' % (subproject_path, key)
                    del subprojects[subproject]
                    external_releases.remove(subproject)
                    if strict:
                        raise ValueError(error_msg)
                    else:
                        print(error_msg, file=sys.stderr)

            subprojects[subproject].update({
                key: value for key, value in main_config.items() if key != "subprojects"
            })

            # Introducing a new "eol" tag for subprojects, earlier the eol tag was marked
            # as "False" for all subprojects and introducing a new "eol" tag will add the
            # details to "support_metadata" but not that actual subprojects[subproject]["eol"]
            # field, so this assignment will make subprojects[subproject]["eol"] in sync
            # with "eol" tag marked in subproject config
            subprojects[subproject]["eol"] = config.get("eol", False)

            # populate `eol_external_subprojects` and `active_external_subprojects`
            (eol_external_subprojects if subprojects[subproject]["eol"] else active_external_subprojects)[subproject] = subprojects[subproject]

            project = read_external_subproject_details(subproject)
            if project:
                subprojects[subproject].setdefault("customer", project)

    # now ensure they are consistent
    global devel_release
    for release in subprojects:
        details = subprojects[release]
        rel = release_alias(release)
        # prefer the alias name
        all_releases.append(rel)
        if details["eol"]:
            eol_releases.append(rel)
        if "devel" in details and details["devel"]:
            if devel_release != "" and devel_release != rel:
                raise ValueError("there can be only one ⚔ devel")
            devel_release = rel
        if (
            "description" in details
            and details["description"] == "Interim Release"
            and rel not in external_releases
        ):
            interim_releases.append(rel)
        # ubuntu specific releases
        product, _ = product_series(release)
        if product == PRODUCT_UBUNTU:
            releases.append(rel)

load_external_subprojects()

def compare_releases(a, b):
    v1 = release_version(a)
    v2 = release_version(b)
    if v1 != v2:
        return v1-v2

    # same version, get order by product
    product_order = [PRODUCT_UBUNTU] + PRODUCT_ESM + PRODUCT_FIPS
    order = {product: i for i, product in enumerate(product_order)}

    p1, _ = product_series(a)
    p2, _ = product_series(b)

    # if product is not in the order list it gets the higher index
    # (like realtime/, bluefield/ or any other subproject)
    i1 = order.get(p1, len(product_order))
    i2 = order.get(p2, len(product_order))

    if i1 != i2:
        return i1 - i2

    # finally, fallback to alphabetical order, to preserve stability
    # for random inputs.
    return (a > b) - (a < b)

def release_sort(release_list):
    '''takes a list of release names and sorts them in release order

    This is not a strict ordering based on when the release was made but a logic
    ordering used for human consumption.
    '''

    # turn list into a tuples of (name, version) - we want sub-releases to sort
    # later than their parent, so introduce a hack to add one month to their
    # release version so they sort after their parent
    return sorted(release_list, key=cmp_to_key(compare_releases))

all_releases = release_sort(all_releases)
releases = release_sort(releases)

# all of the following are only valid for the Tags field of the CVE file itself
valid_cve_tags = {
        'cisa-kev': 'This vulnerability is listed in the CISA Known Exploited Vulnerabilities Catalog. For more details see https://www.cisa.gov/known-exploited-vulnerabilities-catalog',
        'epss-prioritized': 'This vulnerability has a significant EPSS score and is being prioritized for analysis. For more details on EPSS scoring see https://www.first.org/epss',
        'epss-reviewed': 'This vulnerability has been reviewed/analyzed due to previously being tagged as epss-prioritized.',
}

# all of the following are only valid for a Tags_srcpkg field
valid_package_tags = {
    'universe-binary': 'Binaries built from this source package are in universe and so are supported by the community. For more details see https://wiki.ubuntu.com/SecurityTeam/FAQ#Official_Support',
    'not-ue': 'This package is not directly supported by the Ubuntu Security Team',
    'apparmor': 'This vulnerability is mitigated in part by an AppArmor profile. For more details see https://wiki.ubuntu.com/Security/Features#apparmor',
    'stack-protector': 'This vulnerability is mitigated in part by the use of gcc\'s stack protector in Ubuntu. For more details see https://wiki.ubuntu.com/Security/Features#stack-protector',
    'fortify-source': 'This vulnerability is mitigated in part by the use of -D_FORTIFY_SOURCE=2 in Ubuntu. For more details see https://wiki.ubuntu.com/Security/Features#fortify-source',
    'symlink-restriction': 'This vulnerability is mitigated in part by the use of symlink restrictions in Ubuntu. For more details see https://wiki.ubuntu.com/Security/Features#symlink',
    'hardlink-restriction': 'This vulnerability is mitigated in part by the use of hardlink restrictions in Ubuntu. For more details see https://wiki.ubuntu.com/Security/Features#hardlink',
    'heap-protector': 'This vulnerability is mitigated in part by the use of GNU C Library heap protector in Ubuntu. For more details see https://wiki.ubuntu.com/Security/Features#heap-protector',
    'pie': 'This vulnerability is mitigated in part by the use of Position Independent Executables in Ubuntu. For more details see https://wiki.ubuntu.com/Security/Features#pie',
    'review-break-fix': 'This vulnerability automatically received break-fix commits entries when it was added and needs to be reviewed.',
    'kernel-team-severity-assessed': 'This vulnerability had the priority assessed by a kernel team member',
    'kernel-team-break-fix-verified': 'This vulnerability had the break-fix commits identified by a kernel team member',
    'kernel-team-severity-assessed-agent': 'This vulnerability had the priority assessed by the kernel team agent',
    'kernel-team-break-fix-verified-agent': 'This vulnerability had the break-fix commits identified by the kernel team agent',
}

# Possible CVE priorities
priorities = ['negligible', 'low', 'medium', 'high', 'critical']

NOTE_RE = re.compile(r'^\s+([A-Za-z0-9-]+)([>|]) *(.*)$')

# as per
# https://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-version
# ideally we would use dpkg --validate-version for this but it is much more
# expensive to shell out than match via a regex so even though this is both
# slightly more strict and also less strict that what dpkg --validate-version
# would permit, it should be good enough for our purposes
VERSION_RE = re.compile(r'^([0-9]+:)?([0-9]+[a-zA-Z0-9~.+-]*)$')

def validate_version(version):
    return VERSION_RE.match(version) is not None

EXIT_FAIL = 1
EXIT_OKAY = 0

subproject_dir_cache_cves = set()
subproject_dir_cache_dirs = set()

# New CVE file format for release package field is:
# <product>[/<where or who>]_SOFTWARE[/<modifier>]: <status> [(<when>)]
# <product> is the Canonical product or supporting technology (eg, ‘esm-apps’
# or ‘snap’). ‘ubuntu’ is the implied product when ‘<product>/’ is omitted
# from the ‘<product>[/<where or who>]’ tuple (ie, where we might use
# ‘ubuntu/bionic_DEBSRCPKG’ for consistency, we continue to use
# ‘bionic_DEBSRCPKG’)
# <where or who> indicates where the software lives or in the case of snaps or
# other technologies with a concept of publishers, who the publisher is
# SOFTWARE is the name of the software as dictated by the product (eg, the deb
# source package, the name of the snap or the name of the software project
# <modifier> is an optional key for grouping collections of packages (eg,
# ‘melodic’ for the ROS Melodic release or ‘rocky’ for the OpenStack Rocky
# release)
# <status> indicates the statuses as defined in UCT (eg, needs-triage, needed,
# pending, released, etc)
# <when> indicates ‘when’ the software will be/was fixed when used with the
# ‘pending’ or ‘released’ status (eg, the source package version, snap
# revision, etc)
# e.g.: esm-apps/xenial_jackson-databind: released (2.4.2-3ubuntu0.1~esm2)
# e.g.: git/github.com/gogo/protobuf_gogoprotobuf: needs-triage
# This method should keep supporting existing current format:
# e.g.: bionic_jackson-databind: needs-triage
def parse_cve_release_package_field(cvefile, field, data, value, code, msg, linenum):
    package = ""
    release = ""
    state = ""
    details = ""
    try:
        release, package = field.split('_', 1)
    except ValueError:
        msg += "%s: %d: bad field with '_': '%s'\n" % (cvefile, linenum, field)
        code = EXIT_FAIL
        return False, package, release, state, details, code, msg

    try:
        info = value.split(' ', 1)
    except ValueError:
        msg += "%s: %d: missing state for '%s': '%s'\n" % (cvefile, linenum, field, value)
        code = EXIT_FAIL
        return False, package, release, state, details, code, msg

    state = info[0]
    if state == '':
        state = 'needs-triage'

    if len(info) < 2:
        details = ""
    else:
        details = info[1].strip()

    if details.startswith("["):
        msg += "%s: %d: %s has details that starts with a bracket: '%s'\n" % (cvefile, linenum, field, details)
        code = EXIT_FAIL
        return False, package, release, state, details, code, msg

    if details.startswith('('):
        details = details[1:]
    if details.endswith(')'):
        details = details[:-1]

    # Work-around for old-style of only recording released versions
    if details == '' and state[0] in ('0123456789'):
        details = state
        state = 'released'

    valid_states = ['needs-triage', 'needed', 'in-progress', 'pending', 'released', 'deferred', 'DNE', 'ignored', 'not-affected']
    if state not in valid_states:
        msg += "%s: %d: %s has unknown state: '%s' (valid states are: %s)\n" % (cvefile, linenum, field, state,
                                                                                   ' '.join(valid_states))
        code = EXIT_FAIL
        return False, package, release, state, details, code, msg

    # if the state is released or pending then the details needs to be a valid
    # debian package version number
    if details != "" and state in ['released', 'pending'] and release not in ['upstream', 'snap']:
        if not validate_version(details):
            msg += "%s: %d: %s has invalid version for state %s: '%s'\n" % (cvefile, linenum, field, state, details)
            code = EXIT_FAIL
            return False, package, release, state, details, code, msg

    # Verify "released" kernels have version details
    #if state == 'released' and package in kernel_srcs and details == '':
    #    msg += "%s: %s_%s has state '%s' but lacks version note\n" % (cvefile, package, release, state)
    #    code = EXIT_FAIL

    # Verify "active" states have an Assignee
    if state == 'in-progress' and data['Assigned-to'].strip() == "":
        msg += "%s: %d: %s has state '%s' but lacks 'Assigned-to'\n" % (cvefile, linenum, field, state)
        code = EXIT_FAIL
        return False, package, release, state, details, code, msg

    return True, package, release, state, details, code, msg

class NotesParser(object):
    def __init__(self):
        self.notes = list()
        self.user = None
        self.separator = None
        self.note = None

    def parse_line(self, cve, line, linenum, code):
        msg = ""
        m = NOTE_RE.match(line)
        if m is not None:
            new_user = m.group(1)
            new_sep = m.group(2)
            new_note = m.group(3)
        else:
            # follow up comments should have 2 space indent and
            # an author
            if self.user is None:
                msg += ("%s: %d: Note entry with no author: '%s'\n" %
                        (cve, linenum, line[1:]))
                code = EXIT_FAIL
            if not line.startswith('  '):
                msg += ("%s: %d: Note continuations should be indented by 2 spaces: '%s'.\n" %
                        (cve, linenum, line))
                code = EXIT_FAIL
            new_user = self.user
            new_sep = self.separator
            new_note = line.strip()
        if self.user and self.separator and self.note:
            # if is different user, start a new note
            if new_user != self.user:
                self.notes.append([self.user, self.note])
                self.user = new_user
                self.note = new_note
                self.separator = new_sep
            elif new_sep != self.separator:
                # finish this note and start a new one since this has new
                # semantics
                self.notes.append([self.user, self.note])
                self.separator = new_sep
                self.note = new_note
            else:
                if self.separator == '|':
                    self.note = self.note + " " + new_note
                else:
                    assert(self.separator == '>')
                    self.note = self.note + "\n" + new_note
        else:
            # this is the first note
            self.user = new_user
            self.separator = new_sep
            self.note = new_note
        return code, msg

    def finalize(self):
        if self.user is not None and self.note is not None:
            # add last Note
            self.notes.append([self.user, self.note])
            self.user = None
            self.note = None
        notes = self.notes
        self.user = None
        self.separator = None
        self.notes = None
        return notes

def load_cve(cvefile, strict=False, srcentries=None):
    '''Loads a given CVE into:
       dict( fields...
             'pkgs' -> dict(  pkg -> dict(  release ->  (state, details)   ) )
           )
    '''

    msg = ''
    code = EXIT_OKAY
    required_fields = ['Candidate', 'PublicDate', 'References', 'Description',
                       'Ubuntu-Description', 'Notes', 'Bugs',
                       'Priority', 'Discovered-by', 'Assigned-to', 'CVSS']
    extra_fields = ['CRD', 'PublicDateAtUSN', 'Mitigation', 'Tags']

    data = dict()
    # maps entries in data to their source line - if didn't supply one
    # create a local one to simplify the code
    if srcentries is None:
        srcentries = dict()
    srcentries.setdefault('pkgs', dict())
    srcentries.setdefault('tags', dict())
    data.setdefault('tags', dict())
    srcentries.setdefault('patches', dict())
    data.setdefault('patches', dict())
    affected = dict()
    lastfield = ""
    fields_seen = set()
    if not os.path.exists(cvefile):
        raise ValueError("File does not exist: '%s'" % cvefile)
    linenum = 0
    notes_parser = NotesParser()
    priority_reason = {}
    cvss_entries = []
    with codecs.open(cvefile, encoding="utf-8") as inF:
        lines = inF.readlines()
    for line in lines:
        line = line.rstrip()
        linenum += 1

        # Ignore blank/commented lines
        if len(line) == 0 or line.startswith('#'):
            continue
        if line.startswith(' '):
            try:
                # parse Notes properly
                if lastfield == 'Notes':
                    code, newmsg = notes_parser.parse_line(cvefile, line, linenum, code)
                    if code != EXIT_OKAY:
                        msg += newmsg
                elif lastfield.startswith('Priority'):
                    priority_part = lastfield.split('_')[1] if '_' in lastfield else None
                    if priority_part in priority_reason:
                        priority_reason[priority_part].append(line.strip())
                    else:
                        priority_reason[priority_part] = [line.strip()]
                elif 'Patches_' in lastfield:
                    try:
                        _, pkg = lastfield.split('_', 1)
                        patch_type, entry = line.split(':', 1)
                        patch_type = patch_type.strip()
                        entry = entry.strip()
                        data['patches'][pkg].append((patch_type, entry))
                        srcentries['patches'][pkg].append((cvefile, linenum))
                    except Exception as e:
                        msg += "%s: %d: Failed to parse '%s' entry %s: %s\n" % (cvefile, linenum, lastfield, line, e)
                        code = EXIT_FAIL
                elif lastfield == 'CVSS':
                    try:
                        cvss = dict()
                        result = re.search(r' (.+)\: (\S+)( \[(.*) (.*)\])?', line)
                        if result is None:
                            continue
                        cvss['source'] = result.group(1)
                        cvss['vector'] = result.group(2)
                        if result.group(3):
                            cvss['baseScore'] = result.group(4)
                            cvss['baseSeverity'] = result.group(5)

                        cvss_entries.append(cvss)
                        # CVSS in srcentries will be a tuple since this is the
                        # line where the CVSS block starts - so convert it
                        # to a dict first if needed
                        if type(srcentries["CVSS"]) is tuple:
                            srcentries["CVSS"] = dict()
                        srcentries["CVSS"].setdefault(cvss['source'], (cvefile, linenum))
                    except Exception as e:
                        msg += "%s: %d: Failed to parse CVSS: %s\n" % (cvefile, linenum, e)
                        code = EXIT_FAIL
                else:
                    data[lastfield] += '\n%s' % (line[1:])
            except KeyError as e:
                msg += "%s: %d: bad line '%s' (%s)\n" % (cvefile, linenum, line, e)
                code = EXIT_FAIL
            continue

        try:
            field, value = line.split(':', 1)
        except ValueError as e:
            msg += "%s: %d: bad line '%s' (%s)\n" % (cvefile, linenum, line, e)
            code = EXIT_FAIL
            continue

        lastfield = field = field.strip()
        if field in fields_seen:
            msg += "%s: %d: repeated field '%s'\n" % (cvefile, linenum, field)
            code = EXIT_FAIL
        else:
            fields_seen.add(field)
        value = value.strip()
        if field == 'Candidate':
            data.setdefault(field, value)
            srcentries.setdefault(field, (cvefile, linenum))
            if value != "" and not value.startswith('CVE-') and not value.startswith('UEM-') and not value.startswith('EMB-'):
                msg += "%s: %d: unknown Candidate '%s' (must be /(CVE|UEM|EMB)-/)\n" % (cvefile, linenum, value)
                code = EXIT_FAIL
        elif 'Priority' in field:
            # For now, throw away comments on Priority fields
            if ' ' in value:
                value = value.split()[0]
            if 'Priority_' in field:
                try:
                    _, pkg = field.split('_', 1)
                except ValueError:
                    msg += "%s: %d: bad field with 'Priority_': '%s'\n" % (cvefile, linenum, field)
                    code = EXIT_FAIL
                    continue
            # initially set the priority reason as an empty string - this will
            # be fixed up later with a real value if one is found
            data.setdefault(field, [value, ""])
            srcentries.setdefault(field, (cvefile, linenum))
            if value not in ['untriaged', 'not-for-us'] + priorities:
                msg += "%s: %d: unknown Priority '%s'\n" % (cvefile, linenum, value)
                code = EXIT_FAIL
        elif 'Patches_' in field:
            try:
                _, pkg = field.split('_', 1)
            except ValueError:
                msg += "%s: %d: bad field with 'Patches_': '%s'\n" % (cvefile, linenum, field)
                code = EXIT_FAIL
                continue
            # value should be empty
            if len(value) > 0:
                msg += "%s: %d: '%s' field should have no value\n" % (cvefile, linenum, field)
                code = EXIT_FAIL
                continue
            data['patches'].setdefault(pkg, list())
            srcentries['patches'].setdefault(pkg, list())
        elif 'Tags' in field:
            '''These are processed into the "tags" hash'''
            try:
                _, pkg = field.split('_', 1)
            except ValueError:
                # no package specified - this is the global tags field - use a
                # key of '*' to store it in the package hash
                pkg = GLOBAL_TAGS_KEY
            data['tags'].setdefault(pkg, set())
            srcentries['tags'].setdefault(pkg, (cvefile, linenum))
            for word in value.strip().split(' '):
                if pkg == GLOBAL_TAGS_KEY and word not in valid_cve_tags:
                    msg += "%s: %d: invalid CVE tag '%s': '%s'\n" % (cvefile, linenum, word, field)
                    code = EXIT_FAIL
                    continue
                elif pkg != GLOBAL_TAGS_KEY and word not in valid_package_tags:
                    msg += "%s: %d: invalid package tag '%s': '%s'\n" % (cvefile, linenum, word, field)
                    code = EXIT_FAIL
                    continue
                data['tags'][pkg].add(word)
        elif '_' in field:
            success, pkg, rel, state, details, code, msg = parse_cve_release_package_field(cvefile, field, data, value, code, msg, linenum)
            if not success:
                assert(code == EXIT_FAIL)
                continue
            canon, _, _, _ = get_subproject_details(rel)
            if canon is None and rel not in ['upstream', 'devel']:
                msg += "%s: %d: unknown entry '%s'\n" % (cvefile, linenum, rel)
                code = EXIT_FAIL
                continue
            affected.setdefault(pkg, dict())
            if rel in affected[pkg]:
                msg += ("%s: %d: duplicate entry for '%s': original at %s line %d\n"
                        % (cvefile, linenum, rel, srcentries['pkgs'][pkg][rel][0], srcentries['pkgs'][pkg][rel][1]))
                code = EXIT_FAIL
                continue
            affected[pkg].setdefault(rel, [state, details])
            srcentries['pkgs'].setdefault(pkg, dict())
            srcentries['pkgs'][pkg].setdefault(rel, (cvefile, linenum))
        elif field not in required_fields + extra_fields:
            msg += "%s: %d: unknown field '%s'\n" % (cvefile, linenum, field)
            code = EXIT_FAIL
        else:
            data.setdefault(field, value)
            srcentries.setdefault(field, (cvefile, linenum))

    data['Notes'] = notes_parser.finalize()
    data['CVSS'] = cvss_entries

    # Check for required fields
    for field in required_fields:
        # boilerplate files are special and can (should?) be empty
        nonempty = [] if "boilerplate" in cvefile else ['Candidate']
        if strict:
            nonempty += ['PublicDate']

        if field not in data or field not in fields_seen:
            msg += "%s: %d: missing field '%s'\n" % (cvefile, linenum, field)
            code = EXIT_FAIL
        elif field in nonempty and data[field].strip() == "":
            linenum = srcentries[field][1]
            msg += "%s: %d: required field '%s' is empty\n" % (cvefile, linenum, field)
            code = EXIT_FAIL

    # Fill in defaults for missing fields
    if 'Priority' not in data:
        data.setdefault('Priority', ['untriaged'])
        srcentries.setdefault('Priority', (cvefile, 1))
    # Perform override fields
    if 'PublicDateAtUSN' in data:
        data['PublicDate'] = data['PublicDateAtUSN']
        srcentries['PublicDate'] = srcentries['PublicDateAtUSN']
    if 'CRD' in data and data['CRD'].strip() != '' and data['PublicDate'] != data['CRD']:
        if cvefile.startswith("embargoed"):
            print("%s: %d: adjusting PublicDate to use CRD: %s" % (cvefile, linenum, data['CRD']), file=sys.stderr)
        data['PublicDate'] = data['CRD']
        srcentries['PublicDate'] = srcentries['CRD']

    if data["PublicDate"] > PRIORITY_REASON_DATE_START and \
            data["Priority"][0] in PRIORITY_REASON_REQUIRED and not priority_reason:
        linenum = srcentries["Priority"][1]
        msg += "%s: %d: needs a reason for being '%s'\n" % (cvefile, linenum, data["Priority"][0])
        code = EXIT_FAIL
    for item in priority_reason:
        field = 'Priority' if not item else 'Priority_' + item
        data[field][1] = ' '.join(priority_reason[item])

    # entries need an upstream entry if any entries are from the internal
    # list of subprojects
    for pkg in affected:
        needs_upstream = False
        for rel in affected[pkg]:
            if rel not in external_releases:
                needs_upstream = True
        if needs_upstream and 'upstream' not in affected[pkg]:
            msg += "%s: %d: missing upstream '%s'\n" % (cvefile, linenum, pkg)
            code = EXIT_FAIL

    data['pkgs'] = affected

    if not "boilerplate" in cvefile:
        code, msg = load_external_subproject_cve_data(cvefile, data, srcentries, code, msg)

    if code != EXIT_OKAY:
        raise ValueError(msg.strip())
    return data

def amend_external_subproject_pkg(cve, data, srcentries, amendments, code, msg):
    linenum = 0
    for line in amendments.splitlines():
        linenum += 1
        if len(line) == 0 or line.startswith('#') or line.startswith(' '):
            continue
        try:
            field, value = line.split(':', 1)
            field = field.strip()
            value = value.strip()
        except ValueError as e:
            msg += "%s: bad line '%s' (%s)\n" % (cve, line, e)
            code = EXIT_FAIL
            return code, msg

        if '_' in field:
            success, pkg, rel, state, details, code, msg = parse_cve_release_package_field(cve, field, data, value, code, msg, linenum)
            if not success:
                return code, msg

            canon, _, _, _ = get_subproject_details(rel)
            if canon is None and rel not in ['upstream', 'devel']:
                msg += "%s: %d: unknown entry '%s'\n" % (cve, linenum, rel)
                code = EXIT_FAIL
                return code, msg
            data.setdefault("pkgs", dict())
            data["pkgs"].setdefault(pkg, dict())
            srcentries["pkgs"].setdefault(pkg, dict())
            if rel in data["pkgs"][pkg]:
                msg += ("%s: %d: duplicate entry for '%s': original at %s line %d (%s)\n"
                        % (cve, linenum, rel, srcentries['pkgs'][pkg][rel][0], srcentries['pkgs'][pkg][rel][1], data["pkgs"][pkg][rel]))
                code = EXIT_FAIL
                return code, msg
            data["pkgs"][pkg][rel] = [state, details]
            srcentries["pkgs"][pkg][rel] = (cve, linenum)

    return code, msg

def load_external_subproject_cve_data(cve, data, srcentries, code, msg):
    cve_id = os.path.basename(cve)
    for f in find_external_subproject_cves(cve_id):
        with codecs.open(f, 'r', encoding="utf-8") as fp:
            amendments = fp.read()
            fp.close()
        code, msg = amend_external_subproject_pkg(f, data, srcentries, amendments, code, msg)

    return code, msg