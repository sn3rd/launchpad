#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import json

from lp.bugs.scripts.uct.subprojects import (
    PPAReference,
    SubProjectPPAs,
    load_subprojects_from_str,
)
from lp.testing import TestCase


class TestLoadSubprojectsFromStr(TestCase):
    def _make_json(self, entries):
        return json.dumps(entries)

    def test_entry_with_single_ppa(self):
        content = self._make_json(
            {
                "esm/precise": {
                    "codename": "Precise Pangolin",
                    "ppas": [{"ppa": "ubuntu-esm/esm", "pocket": "security"}],
                }
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual(
            {
                "esm/precise": SubProjectPPAs(
                    ubuntu_series="precise",
                    ppas=(PPAReference("ubuntu-esm", "esm", "security"),),
                )
            },
            result,
        )

    def test_entry_with_multiple_ppas(self):
        content = self._make_json(
            {
                "esm-infra/xenial": {
                    "codename": "Xenial Xerus",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/esm-infra-security",
                            "pocket": "security",
                        },
                        {
                            "ppa": "ubuntu-esm/esm-infra-updates",
                            "pocket": "updates",
                        },
                    ],
                }
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual(
            {
                "esm-infra/xenial": SubProjectPPAs(
                    ubuntu_series="xenial",
                    ppas=(
                        PPAReference(
                            "ubuntu-esm", "esm-infra-security", "security"
                        ),
                        PPAReference(
                            "ubuntu-esm", "esm-infra-updates", "updates"
                        ),
                    ),
                )
            },
            result,
        )

    def test_alias_maps_to_same_object(self):
        content = self._make_json(
            {
                "esm/precise": {
                    "codename": "Precise Pangolin",
                    "alias": "precise/esm",
                    "ppas": [{"ppa": "ubuntu-esm/esm", "pocket": "security"}],
                }
            }
        )
        result = load_subprojects_from_str(content)
        self.assertIn("esm/precise", result)
        self.assertIn("precise/esm", result)
        self.assertIs(result["esm/precise"], result["precise/esm"])

    def test_codename_first_word_lowercased(self):
        content = self._make_json(
            {
                "esm-infra/focal": {
                    "codename": "Focal Fossa",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/esm-infra-security",
                            "pocket": "security",
                        }
                    ],
                }
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual("focal", result["esm-infra/focal"].ubuntu_series)

    def test_entry_without_ppas_is_skipped(self):
        content = self._make_json(
            {
                "ubuntu/xenial": {
                    "codename": "Xenial Xerus",
                    "alias": "xenial",
                    "name": "Ubuntu 16.04 LTS",
                }
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual({}, result)

    def test_alias_of_skipped_entry_is_also_absent(self):
        content = self._make_json(
            {
                "ubuntu/xenial": {
                    "codename": "Xenial Xerus",
                    "alias": "xenial",
                }
            }
        )
        result = load_subprojects_from_str(content)
        self.assertNotIn("xenial", result)

    def test_mixed_entries(self):
        content = self._make_json(
            {
                "ubuntu/xenial": {
                    "codename": "Xenial Xerus",
                    "alias": "xenial",
                },
                "esm-infra/xenial": {
                    "codename": "Xenial Xerus",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/esm-infra-security",
                            "pocket": "security",
                        }
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        self.assertNotIn("ubuntu/xenial", result)
        self.assertNotIn("xenial", result)
        self.assertIn("esm-infra/xenial", result)

    def test_empty_json(self):
        result = load_subprojects_from_str("{}")
        self.assertEqual({}, result)
