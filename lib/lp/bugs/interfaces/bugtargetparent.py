# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from zope.interface import Interface


class IBugTargetParent(Interface):
    """Interface for objects that act as a bug target parent."""

    def getDefaultBugInformationType():
        """Return the default information type for bugs."""

    def getAllowedBugInformationTypes():
        """Return information types that are valid for bugs on this target."""


def bug_target_parent_sort_key(bug_target_parent):
    """A sort key for a set of bug target parents. We want:

    - products first, alphabetically
    - distributions, with ubuntu first and the rest alphabetically
    """
    from lp.registry.interfaces.distribution import IDistribution
    from lp.registry.interfaces.product import IProduct

    product_name = ""
    distribution_name = ""
    if IProduct.providedBy(bug_target_parent):
        product_name = bug_target_parent.name
    elif IDistribution.providedBy(bug_target_parent):
        distribution_name = bug_target_parent.name
    # Move ubuntu to the top.
    if distribution_name == "ubuntu":
        distribution_name = "-"

    return (distribution_name, product_name)
