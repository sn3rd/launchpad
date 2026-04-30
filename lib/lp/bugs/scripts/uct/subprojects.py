#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import json
from pathlib import Path
from typing import Dict, NamedTuple, Tuple


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
    ppas: Tuple[PPAReference, ...]


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
        value = SubProjectPPAs(
            ubuntu_series=codename.split()[0].lower(),
            ppas=tuple(PPAReference.from_dict(p) for p in entry["ppas"]),
        )
        result[subproject_name] = value
        alias = entry.get("alias")
        if alias:
            result[alias] = value
    return result


def load_subprojects(path: Path) -> Dict[str, SubProjectPPAs]:
    """Load subprojects.json from *path* and return the PPA lookup dict."""
    return load_subprojects_from_str(path.read_text())
