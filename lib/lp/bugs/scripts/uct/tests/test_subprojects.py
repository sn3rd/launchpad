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
                    ppa=PPAReference("ubuntu-esm", "esm", "security"),
                )
            },
            result,
        )

    def test_entry_with_multiple_ppas_prefers_security(self):
        """When both security and updates PPAs exist, security is preferred."""
        content = self._make_json(
            {
                "ubuntu/xenial": {
                    "codename": "Xenial Xerus",
                    "version": 16.04,
                },
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
                },
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual(
            {
                "esm-infra/xenial": SubProjectPPAs(
                    ubuntu_series="xenial",
                    ppa=PPAReference(
                        "ubuntu-esm", "esm-infra-security", "security"
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

    def test_pro_vs_non_pro_for_newer_series(self):
        """For series newer than jammy, prefer pro PPAs."""
        content = self._make_json(
            {
                "ubuntu/noble": {
                    "codename": "Noble Numbat",
                    "version": 24.04,
                },
                "fips-updates/noble": {
                    "codename": "Noble Numbat",
                    "ppas": [
                        {
                            "ppa": "ubuntu-advantage/fips-updates",
                            "pocket": "updates",
                        },
                        {
                            "ppa": "ubuntu-advantage/pro-fips-updates",
                            "pocket": "updates",
                        },
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        # Should prefer pro- prefix for noble (24.04 > 22.04)
        self.assertEqual(
            result["fips-updates/noble"].ppa,
            PPAReference("ubuntu-advantage", "pro-fips-updates", "updates"),
        )

    def test_pro_vs_non_pro_for_jammy_and_older(self):
        """For series jammy and older, prefer non-pro PPAs."""
        content = self._make_json(
            {
                "ubuntu/xenial": {
                    "codename": "Xenial Xerus",
                    "version": 16.04,
                },
                "fips/xenial": {
                    "codename": "Xenial Xerus",
                    "ppas": [
                        {
                            "ppa": "ubuntu-advantage/fips",
                            "pocket": "security",
                        },
                        {
                            "ppa": "ubuntu-advantage/pro-fips",
                            "pocket": "security",
                        },
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        # Should prefer non-pro for xenial (16.04 < 22.04)
        self.assertEqual(
            result["fips/xenial"].ppa,
            PPAReference("ubuntu-advantage", "fips", "security"),
        )

    def test_pro_vs_non_pro_for_jammy_itself(self):
        """For jammy itself, prefer non-pro PPAs."""
        content = self._make_json(
            {
                "ubuntu/jammy": {
                    "codename": "Jammy Jellyfish",
                    "version": 22.04,
                },
                "fips-updates/jammy": {
                    "codename": "Jammy Jellyfish",
                    "ppas": [
                        {
                            "ppa": "ubuntu-advantage/fips-updates",
                            "pocket": "updates",
                        },
                        {
                            "ppa": "ubuntu-advantage/pro-fips-updates",
                            "pocket": "updates",
                        },
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        # Should prefer non-pro for jammy (22.04 <= 22.04)
        self.assertEqual(
            result["fips-updates/jammy"].ppa,
            PPAReference("ubuntu-advantage", "fips-updates", "updates"),
        )

    def test_updates_pocket_when_no_security(self):
        """When only updates pocket exists, it should be selected."""
        content = self._make_json(
            {
                "ubuntu/focal": {
                    "codename": "Focal Fossa",
                    "version": 20.04,
                },
                "test/focal": {
                    "codename": "Focal Fossa",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/test-updates",
                            "pocket": "updates",
                        }
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual(
            result["test/focal"].ppa,
            PPAReference("ubuntu-esm", "test-updates", "updates"),
        )

    def test_security_pocket_when_no_updates(self):
        """When only security pocket exists, it should be selected."""
        content = self._make_json(
            {
                "ubuntu/focal": {
                    "codename": "Focal Fossa",
                    "version": 20.04,
                },
                "test/focal": {
                    "codename": "Focal Fossa",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/test-security",
                            "pocket": "security",
                        }
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual(
            result["test/focal"].ppa,
            PPAReference("ubuntu-esm", "test-security", "security"),
        )

    def test_security_preferred_over_updates(self):
        """When both security and updates pockets exist, security is
        preferred."""
        content = self._make_json(
            {
                "ubuntu/focal": {
                    "codename": "Focal Fossa",
                    "version": 20.04,
                },
                "test/focal": {
                    "codename": "Focal Fossa",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/test-security",
                            "pocket": "security",
                        },
                        {
                            "ppa": "ubuntu-esm/test-updates",
                            "pocket": "updates",
                        },
                    ],
                },
            }
        )
        result = load_subprojects_from_str(content)
        self.assertEqual(
            result["test/focal"].ppa,
            PPAReference("ubuntu-esm", "test-security", "security"),
        )

    def test_version_not_found_raises_error(self):
        """When version lookup fails, raise KeyError."""
        content = self._make_json(
            {
                # No ubuntu/unknown series entry
                "test/unknown": {
                    "codename": "Unknown Series",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/test-security",
                            "pocket": "security",
                        },
                        {
                            "ppa": "ubuntu-esm/pro-test-security",
                            "pocket": "security",
                        },
                    ],
                },
            }
        )
        # Should raise KeyError when ubuntu/unknown doesn't exist
        # (version lookup is needed when there are multiple PPAs)
        error = self.assertRaises(KeyError, load_subprojects_from_str, content)
        self.assertIn("ubuntu/unknown", str(error))

    def test_no_matching_ppa_raises_error(self):
        """When no suitable PPA can be selected, raise ValueError."""
        content = self._make_json(
            {
                "ubuntu/noble": {
                    "codename": "Noble Numbat",
                    "version": 24.04,
                },
                "test/noble": {
                    "codename": "Noble Numbat",
                    "ppas": [
                        {
                            "ppa": "ubuntu-advantage/test1",
                            "pocket": "security",
                        },
                        {
                            "ppa": "ubuntu-advantage/test2",
                            "pocket": "security",
                        },
                    ],
                },
            }
        )
        # Noble (24.04 > 22.04) expects pro PPAs, but none have pro- prefix
        error = self.assertRaises(
            ValueError, load_subprojects_from_str, content
        )
        self.assertIn(
            "Expected exactly one pro- and one non-pro PPA", str(error)
        )
        self.assertIn("noble", str(error))

    def test_multiple_ppas_without_pro_prefix_for_newer_series(self):
        """When multiple PPAs exist but none match expected pro/non-pro,
        raise error."""
        content = self._make_json(
            {
                "ubuntu/noble": {
                    "codename": "Noble Numbat",
                    "version": 24.04,
                },
                "test/noble": {
                    "codename": "Noble Numbat",
                    "ppas": [
                        {
                            "ppa": "test-owner/test-ppa1",
                            "pocket": "release",
                        },
                        {
                            "ppa": "test-owner/test-ppa2",
                            "pocket": "release",
                        },
                    ],
                },
            }
        )
        # Should raise ValueError since noble needs pro- PPAs but none exist
        error = self.assertRaises(
            ValueError, load_subprojects_from_str, content
        )
        self.assertIn(
            "Expected exactly one pro- and one non-pro PPA", str(error)
        )

    def test_missing_version_field_raises_error(self):
        """When ubuntu/series entry exists but 'version' field is missing,
        raise KeyError."""
        content = self._make_json(
            {
                "ubuntu/focal": {
                    "codename": "Focal Fossa",
                    # Missing "version" field
                },
                "test/focal": {
                    "codename": "Focal Fossa",
                    "ppas": [
                        {
                            "ppa": "ubuntu-esm/test-security",
                            "pocket": "security",
                        },
                        {
                            "ppa": "ubuntu-esm/pro-test-security",
                            "pocket": "security",
                        },
                    ],
                },
            }
        )
        # Should raise KeyError when version field is missing
        # (version is needed when there are multiple security PPAs to decide
        # pro/non-pro)
        error = self.assertRaises(KeyError, load_subprojects_from_str, content)
        self.assertIn("version", str(error))
        self.assertIn("ubuntu/focal", str(error))
