# Copyright 2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for path validators."""

from lp.app.validators import LaunchpadValidationError
from lp.app.validators.path import path_does_not_escape
from lp.testing import TestCase


class TestPathDoesNotEscape(TestCase):
    def test_valid_path(self):
        self.assertTrue(path_does_not_escape("Buildfile"))

    def test_invalid_path_parent(self):
        self.assertRaises(
            LaunchpadValidationError, path_does_not_escape, "../Buildfile"
        )

    def test_invalid_path_elsewhere(self):
        self.assertRaises(
            LaunchpadValidationError,
            path_does_not_escape,
            "/var/foo/Buildfile",
        )

    def test_starts_with_target(self):
        self.assertRaises(
            LaunchpadValidationError,
            path_does_not_escape,
            "/target/../../../Buildfile",
        )

    def test_extra_dot_slash(self):
        self.assertRaises(
            LaunchpadValidationError,
            path_does_not_escape,
            "/foo/./../../bar/./Buildfile",
        )

    def test_starts_with_target_inclusive(self):
        self.assertRaises(
            LaunchpadValidationError,
            path_does_not_escape,
            "/targetfoo/../../../Buildfile",
        )

    def test_just_target(self):
        # /target is an absolute path and must be rejected.  Previously this
        # passed because it happened to equal the fake base path used
        # internally, but that was a false negative.
        self.assertRaises(
            LaunchpadValidationError, path_does_not_escape, "/target"
        )

    def test_absolute_path_starting_with_target(self):
        # An absolute path that starts with the fake base path string used
        # internally (/target) must be rejected.  Previously os.path.join
        # silently discarded the base for absolute inputs, making the
        # commonprefix check pass incorrectly.
        self.assertRaises(
            LaunchpadValidationError,
            path_does_not_escape,
            "/target/foo",
        )
