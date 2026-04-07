# Copyright 2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Validators for paths and path functions."""

__all__ = ["path_does_not_escape"]

import os

from lp.app.validators import LaunchpadValidationError


def path_does_not_escape(path):
    """First-pass validation that a given path does not escape a root.

    This is only intended as a first defence, usage of this will also
    require checking for filesystem escapes (symlinks, etc).
    """
    # os.path.join silently discards the base when the second argument is
    # absolute, so an absolute path like /target/foo would bypass the
    # commonprefix check below.  Reject absolute paths up front.
    if os.path.isabs(path):
        raise LaunchpadValidationError("Path must be relative")
    # We're not working with complete paths, so we need to make them so
    fake_base_path = "/target"
    # Ensure that we start with a common base
    target_path = os.path.join(fake_base_path, path)
    # Resolve symlinks and such
    real_path = os.path.normpath(target_path)
    # If the paths don't have a common start anymore,
    # we are attempting an escape
    if not os.path.commonprefix((real_path, fake_base_path)) == fake_base_path:
        raise LaunchpadValidationError("Path would escape target directory")
    return True
