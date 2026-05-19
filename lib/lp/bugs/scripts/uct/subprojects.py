#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import json
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple


class PPAReference(NamedTuple):
    owner: str
    archive: str
    pocket: str

    @classmethod
    def from_dict(cls, data: dict) -> "PPAReference":
        owner, archive = data["ppa"].split("/", 1)
        return cls(owner=owner, archive=archive, pocket=data["pocket"])


class SubProjectPPAs(NamedTuple):
    ubuntu_series: str
    ppa: PPAReference


def _get_series_version(ubuntu_series: str, all_data: dict) -> float:
    """Get the version number for an Ubuntu series.

    Args:
        ubuntu_series: Ubuntu series codename (lowercase)
        all_data: Full parsed subprojects.json data

    Returns:
        Version number (e.g., 22.04)

    Raises:
        KeyError: If ubuntu/{series} entry or version field is not found
    """
    ubuntu_key = f"ubuntu/{ubuntu_series}"
    if ubuntu_key not in all_data:
        raise KeyError(
            f"No {ubuntu_key} entry found in subprojects.json for series "
            f"{ubuntu_series}"
        )
    if "version" not in all_data[ubuntu_key]:
        raise KeyError(
            f"{ubuntu_key} entry in subprojects.json is missing 'version' "
            f"field"
        )
    return float(all_data[ubuntu_key]["version"])


def _select_single_ppa(
    ppa_list: List[dict], ubuntu_series: str, all_data: dict
) -> PPAReference:
    """Select a single PPA from a list based on pocket preference and series.

    Selection rules:
    1. If there's only 1 PPA, pick that
    2. If there's exactly one security pocket PPA, pick that
    3. Otherwise, check for 'pro-' prefix in archive name and pick based on
       series version:
       - For series jammy (22.04) and older: prefer non-pro PPAs
       - For series newer than jammy: prefer pro PPAs
    4. Else raise

    Args:
        ppa_list: List of PPA dictionaries from subprojects.json
        ubuntu_series: Ubuntu series codename (lowercase)
        all_data: Full parsed subprojects.json data

    Returns:
        A single PPAReference selected from the list

    Raises:
        ValueError: If no suitable PPA can be selected (e.g., the expected
            pro/non-pro PPA for the series version is not found)
        KeyError: If the version for the ubuntu series cannot be found
    """
    # If there's only 1 PPA, pick that
    if len(ppa_list) == 1:
        return PPAReference.from_dict(ppa_list[0])

    elif len(ppa_list) > 2:
        ppa_names = [p["ppa"] for p in ppa_list]
        raise ValueError(
            f"Expected at most 2 PPAs for series {ubuntu_series}, but found "
            f"{len(ppa_list)}: {ppa_names}"
        )

    # If there's exactly one security pocket PPA, pick that
    security_ppas = [p for p in ppa_list if p.get("pocket") == "security"]
    if len(security_ppas) == 1:
        return PPAReference.from_dict(security_ppas[0])

    # Otherwise, check for -pro prefix and pick based on series version
    pro_ppa = non_pro_ppa = None

    for p in ppa_list:
        if p["ppa"].split("/", 1)[1].startswith("pro-"):
            pro_ppa = p
        else:
            non_pro_ppa = p

    if pro_ppa is None or non_pro_ppa is None:
        ppa_names = [p["ppa"] for p in ppa_list]
        raise ValueError(
            f"Expected exactly one pro- and one non-pro PPA for series "
            f"{ubuntu_series}, but found: {ppa_names}"
        )

    series_version = _get_series_version(ubuntu_series, all_data)

    if series_version <= 22.04:
        return PPAReference.from_dict(non_pro_ppa)
    else:
        return PPAReference.from_dict(pro_ppa)


def load_subprojects_from_str(
    content: str,
) -> Dict[str, SubProjectPPAs]:
    """Parse subprojects.json content into a PPA lookup dict.

    Returns a dict mapping each subproject name (and its alias, if any) to a
    SubProjectPPAs.  Entries without a "ppas" key are skipped.
    """
    data = json.loads(content)
    result: Dict[str, SubProjectPPAs] = {}
    for subproject_name, entry in data.items():
        if "ppas" not in entry:
            continue
        codename = entry.get("codename")
        ubuntu_series = codename.split()[0].lower()
        value = SubProjectPPAs(
            ubuntu_series=ubuntu_series,
            ppa=_select_single_ppa(entry["ppas"], ubuntu_series, data),
        )
        result[subproject_name] = value
        alias = entry.get("alias")
        if alias:
            result[alias] = value
    return result


def load_subprojects(path: Path) -> Dict[str, SubProjectPPAs]:
    """Load subprojects.json from *path* and return the PPA lookup dict."""
    return load_subprojects_from_str(path.read_text())


def create_archive_to_subproject_map(
    subprojects: Dict[str, SubProjectPPAs]
) -> Dict[Tuple[str, str], str]:
    """Create a reverse mapping from (archive_name, series_name) to subproject
    key.

    This is used when exporting from Launchpad to UCT format to map PPA archive
    references back to their original subproject keys.

    Args:
        subprojects: Dict mapping subproject keys to SubProjectPPAs

    Returns:
        Dict mapping (archive_name, series_name) tuples to subproject keys
    """
    archive_to_subproject: Dict[Tuple[str, str], str] = {}
    for subproject_key, subproject_ppa in subprojects.items():
        archive_to_subproject[
            (subproject_ppa.ppa.archive, subproject_ppa.ubuntu_series)
        ] = subproject_key
    return archive_to_subproject
