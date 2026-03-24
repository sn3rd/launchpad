# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from zope.interface import Interface


class IBugTargetParent(Interface):
    """Interface for objects that act as a bug target parent."""


def bug_target_parent_sort_key(pillar):
    """A sort key for a set of bug target parents. We want:

    - products first, alphabetically
    - distributions, with ubuntu first and the rest alphabetically
    """
    from lp.registry.interfaces.distribution import IDistribution
    from lp.registry.interfaces.product import IProduct

    product_name = ""
    distribution_name = ""
    if IProduct.providedBy(pillar):
        product_name = pillar.name
    elif IDistribution.providedBy(pillar):
        distribution_name = pillar.name
    # Move ubuntu to the top.
    if distribution_name == "ubuntu":
        distribution_name = "-"

    return (distribution_name, product_name)
