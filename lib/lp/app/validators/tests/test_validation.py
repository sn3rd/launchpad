# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Unit tests for field validators"""

from zope.component import getUtility

from lp.app.validators import LaunchpadValidationError
from lp.app.validators.validation import (
    validate_oci_branch_name,
    validate_valid_until_config,
)
from lp.registry.interfaces.pocket import PackagePublishingPocket
from lp.registry.model.distroseries import (
    ACTIVE_RELEASED_STATUSES,
    ACTIVE_UNRELEASED_STATUSES,
)
from lp.services.webapp.interfaces import ILaunchBag
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import BaseLayer, ZopelessDatabaseLayer

RELEASE = PackagePublishingPocket.RELEASE
SECURITY = PackagePublishingPocket.SECURITY
UPDATES = PackagePublishingPocket.UPDATES
BACKPORTS = PackagePublishingPocket.BACKPORTS


class TestOCIBranchValidator(TestCase):
    layer = BaseLayer

    def test_validate_oci_branch_name_with_leading_slash(self):
        self.assertFalse(validate_oci_branch_name("/refs/heads/v2.1.0-20.04"))

    def test_validate_oci_branch_name_full(self):
        self.assertTrue(validate_oci_branch_name("refs/heads/v2.1.0-20.04"))

    def test_validate_oci_branch_name_just_branch_name(self):
        self.assertTrue(validate_oci_branch_name("v2.1.0-20.04"))

    def test_validate_oci_branch_name_failure(self):
        self.assertFalse(validate_oci_branch_name("notvalidbranch"))

    def test_validate_oci_branch_name_invalid_ubuntu_version(self):
        self.assertFalse(validate_oci_branch_name("v2.1.0-ubuntu20.04"))

    def test_validate_oci_branch_name_invalid_delimiter(self):
        self.assertFalse(validate_oci_branch_name("v2/1.0-20.04"))

    def test_validate_oci_branch_name_tag(self):
        self.assertTrue(validate_oci_branch_name("refs/tags/v2-1.0-20.04"))

    def test_validate_oci_branch_name_heads_and_tags(self):
        self.assertFalse(
            validate_oci_branch_name("refs/heads/refs/tags/v1.0-20.04")
        )


class TestValidUntilConfigValidation(TestCaseWithFactory):
    """Tests for valid_until_config validation logic."""

    layer = ZopelessDatabaseLayer

    def test_empty_config_is_valid(self):
        """Empty dict or None is valid."""
        self.assertTrue(validate_valid_until_config({}))
        self.assertTrue(validate_valid_until_config(None))

    def test_valid_single_pocket_config(self):
        """Valid configuration with one pocket."""
        config = {SECURITY: {"refresh_threshold": 7, "validity_period": 14}}
        self.assertTrue(validate_valid_until_config(config))

    def test_valid_multiple_pockets_config(self):
        """Valid configuration with multiple pockets."""
        config = {
            SECURITY: {"refresh_threshold": 7, "validity_period": 14},
            UPDATES: {"refresh_threshold": 5, "validity_period": 10},
            BACKPORTS: {"refresh_threshold": 10, "validity_period": 21},
        }
        self.assertTrue(validate_valid_until_config(config))

    def test_refresh_threshold_equals_validity_period(self):
        """refresh_threshold can equal validity_period."""
        config = {SECURITY: {"refresh_threshold": 10, "validity_period": 10}}
        self.assertTrue(validate_valid_until_config(config))

    def test_refresh_threshold_greater_than_validity_period_fails(self):
        """refresh_threshold > validity_period raises error."""
        config = {SECURITY: {"refresh_threshold": 15, "validity_period": 10}}
        self.assertRaises(
            LaunchpadValidationError, validate_valid_until_config, config
        )

    def test_missing_validity_period_fails(self):
        """Missing validity_period key raises error."""
        config = {SECURITY: {"refresh_threshold": 7}}
        self.assertRaises(
            LaunchpadValidationError, validate_valid_until_config, config
        )

    def test_missing_refresh_threshold_fails(self):
        """Missing refresh_threshold key raises error."""
        config = {SECURITY: {"validity_period": 14}}
        self.assertRaises(
            LaunchpadValidationError, validate_valid_until_config, config
        )

    def test_extra_keys_fail(self):
        """Extra keys in config raise error."""
        config = {
            SECURITY: {
                "refresh_threshold": 7,
                "validity_period": 14,
                "extra_key": "value",
            }
        }
        self.assertRaises(
            LaunchpadValidationError, validate_valid_until_config, config
        )

    def test_missing_both_keys_fails(self):
        """Missing both required keys raises error."""
        config = {SECURITY: {}}
        self.assertRaises(
            LaunchpadValidationError, validate_valid_until_config, config
        )

    def test_release_pocket_for_unreleased_series_valid(self):
        """RELEASE pocket is valid for DEVELOPMENT series."""
        distroseries = self.factory.makeDistroSeries()
        getUtility(ILaunchBag).add(distroseries)

        for status in ACTIVE_UNRELEASED_STATUSES:
            distroseries.status = status
            config = {RELEASE: {"refresh_threshold": 3, "validity_period": 7}}
            self.assertTrue(validate_valid_until_config(config))

    def test_release_pocket_for_active_unreleased_series_fails(self):
        """RELEASE pocket is invalid for SUPPORTED series."""
        distroseries = self.factory.makeDistroSeries()
        getUtility(ILaunchBag).add(distroseries)

        for status in ACTIVE_RELEASED_STATUSES:
            distroseries.status = status
            config = {RELEASE: {"refresh_threshold": 3, "validity_period": 7}}
            self.assertRaises(
                LaunchpadValidationError, validate_valid_until_config, config
            )
