# Copyright 2009-2011 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "can_be_nominated_for_series",
    "valid_cve_sequence",
    "validate_new_team_email",
    "validate_oci_branch_name",
    "validate_content_templates",
    "validate_valid_until_config",
]

import re

from zope.component import getUtility

from lp import _
from lp.app.validators import LaunchpadValidationError
from lp.app.validators.cve import valid_cve
from lp.app.validators.email import valid_email
from lp.services.identity.interfaces.emailaddress import IEmailAddressSet
from lp.services.webapp import canonical_url
from lp.services.webapp.escaping import html_escape, structured
from lp.services.webapp.interfaces import ILaunchBag


def can_be_nominated_for_series(series):
    """Can the bug be nominated for these series?"""
    current_bug = getUtility(ILaunchBag).bug
    unnominatable_series = []
    for s in series:
        if not current_bug.canBeNominatedFor(s):
            unnominatable_series.append(s.name.capitalize())

    if unnominatable_series:
        series_str = ", ".join(unnominatable_series)
        raise LaunchpadValidationError(
            _(
                "This bug has already been nominated for these "
                "series: ${series}",
                mapping={"series": series_str},
            )
        )

    return True


def valid_cve_sequence(value):
    """Check if the given value is a valid CVE otherwise raise an exception."""
    if valid_cve(value):
        return True
    else:
        raise LaunchpadValidationError(
            _("${cve} is not a valid CVE number", mapping={"cve": value})
        )


def _validate_email(email):
    if not valid_email(email):
        raise LaunchpadValidationError(
            _(
                "${email} isn't a valid email address.",
                mapping={"email": email},
            )
        )


def _check_email_availability(email):
    email_address = getUtility(IEmailAddressSet).getByEmail(email)
    if email_address is not None:
        person = email_address.person
        message = _(
            "${email} is already registered in Launchpad and is "
            'associated with <a href="${url}">${person}</a>.',
            mapping={
                "email": html_escape(email),
                "url": html_escape(canonical_url(person)),
                "person": html_escape(person.displayname),
            },
        )
        raise LaunchpadValidationError(structured(message))


def validate_new_team_email(email):
    """Check that the given email is valid and not registered to
    another launchpad account.
    """
    _validate_email(email)
    _check_email_availability(email)
    return True


def validate_oci_branch_name(branch_name):
    """Check that a git ref name matches appversion-ubuntuversion."""
    # Remove components to just get the branch/tag name
    if branch_name.startswith("refs/tags/"):
        branch_name = branch_name[len("refs/tags/") :]
    elif branch_name.startswith("refs/heads/"):
        branch_name = branch_name[len("refs/heads/") :]
    split = branch_name.split("-")
    # if we've not got at least two components
    if len(split) < 2:
        return False
    app_version = split[0:-1]
    ubuntu_version = split[-1]
    # 20.04 format
    ubuntu_match = re.match(r"\d{2}\.\d{2}", ubuntu_version)
    if not ubuntu_match:
        return False
    # disallow risks in app version number
    for risk in ["stable", "candidate", "beta", "edge"]:
        if risk in app_version:
            return False
    # no '/' as they're a delimiter
    for segment in app_version:
        if "/" in segment:
            return False
    return True


# XXX alvarocs 2024-12-13:
# To add merge proposal templates or other templates
# as allowed keys when implemented.
def validate_content_templates(value):
    # Omit validation if None
    if value is None:
        return True
    allowed_keys = {
        "bug_templates",
    }
    for key, inner_dict in value.items():
        # Validate allowed keys
        if key not in allowed_keys:
            raise ValueError(
                f"Invalid key '{key}' in content_templates. "
                "Allowed keys: {allowed_keys}"
            )
        # Validate 'default' key exists
        if "default" not in inner_dict:
            raise ValueError(
                f"The '{key}' dictionary must contain a 'default' key."
            )
    return True


def validate_valid_until_config(value):
    """Validate valid_until_config dictionary structure.

    Validates that each pocket's configuration contains exactly
    'refresh_threshold' and 'validity_period' keys, and that
    refresh_threshold <= validity_period.

    Also validates that Release pocket configuration is only specified for
    DistroSeries with SeriesStatus in EXPERIMENTAL or DEVELOPMENT.
    """
    # Avoid circular imports
    from lp.registry.interfaces.pocket import PackagePublishingPocket
    from lp.registry.model.distroseries import ACTIVE_UNRELEASED_STATUSES

    if not value:
        return True

    errors = []

    for pocket, config in value.items():
        pocket_name = pocket.name

        if pocket == PackagePublishingPocket.RELEASE:
            distroseries = getUtility(ILaunchBag).distroseries
            if distroseries.status not in ACTIVE_UNRELEASED_STATUSES:
                allowed_statuses = ", ".join(
                    status.name for status in ACTIVE_UNRELEASED_STATUSES
                )
                errors.append(
                    f"Release pocket configuration can only be specified for "
                    f"series with status: {allowed_statuses}. "
                    f"Series ({distroseries.name}) has status "
                    f"{distroseries.status.name}."
                )
                continue

        expected_keys = {"refresh_threshold", "validity_period"}
        actual_keys = set(config.keys())

        if actual_keys != expected_keys:
            extra_keys = actual_keys - expected_keys
            missing_keys = expected_keys - actual_keys
            error_parts = []
            if extra_keys:
                error_parts.append(f"unexpected keys: {sorted(extra_keys)}")
            if missing_keys:
                error_parts.append(f"missing keys: {sorted(missing_keys)}")
            errors.append(
                f"Invalid keys for pocket {pocket_name}: "
                f"{', '.join(error_parts)}. "
                f"Expected exactly 'refresh_threshold' and 'validity_period'."
            )
            continue

        # Validate refresh_threshold <= validity_period
        refresh_threshold = config["refresh_threshold"]
        validity_period = config["validity_period"]

        if refresh_threshold > validity_period:
            errors.append(
                f"refresh_threshold ({refresh_threshold}) must be <= "
                f"validity_period ({validity_period}) for pocket "
                f"{pocket_name}."
            )

    if errors:
        raise LaunchpadValidationError(" ".join(errors))

    return True
