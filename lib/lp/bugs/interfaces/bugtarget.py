# Copyright 2010-2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Interfaces related to bugs."""

__all__ = [
    "IBugTarget",
    "IHasBugs",
    "IHasExpirableBugs",
    "IHasOfficialBugTags",
    "IOfficialBugTag",
    "IOfficialBugTagTarget",
    "IOfficialBugTagTargetPublic",
    "IOfficialBugTagTargetRestricted",
    "ISeriesBugTarget",
    "BUG_POLICY_ALLOWED_TYPES",
    "BUG_POLICY_DEFAULT_TYPES",
    "DISABLE_BUG_WEBHOOKS_FEATURE_FLAG",
]


from lazr.restful.declarations import (
    REQUEST_USER,
    call_with,
    export_read_operation,
    export_write_operation,
    exported,
    exported_as_webservice_entry,
    operation_for_version,
    operation_parameters,
    operation_removed_in_version,
    operation_returns_collection_of,
)
from lazr.restful.fields import Reference
from lazr.restful.interface import copy_field
from zope.interface import Attribute, Interface
from zope.schema import (
    Bool,
    Choice,
    Datetime,
    Dict,
    List,
    Object,
    Text,
    TextLine,
)

from lp import _
from lp.app.enums import (
    FREE_INFORMATION_TYPES,
    NON_EMBARGOED_INFORMATION_TYPES,
    PROPRIETARY_INFORMATION_TYPES,
    InformationType,
)
from lp.app.validators.validation import validate_content_templates
from lp.bugs.interfaces.bugtask import IBugTask
from lp.bugs.interfaces.bugtasksearch import (
    BugBlueprintSearch,
    BugBranchSearch,
    BugTagsSearchCombinator,
    IBugTaskSearch,
)
from lp.registry.enums import BugSharingPolicy
from lp.services.fields import Tag
from lp.services.webservice.apihelpers import (
    patch_plain_parameter_type,
    patch_reference_property,
)

search_tasks_params_common = {
    "order_by": List(
        title=_("List of fields by which the results are ordered."),
        value_type=Text(),
        required=False,
    ),
    "search_text": copy_field(IBugTaskSearch["searchtext"]),
    "status": copy_field(IBugTaskSearch["status"]),
    "importance": copy_field(IBugTaskSearch["importance"]),
    "information_type": copy_field(IBugTaskSearch["information_type"]),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "assignee": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "bug_reporter": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "bug_supervisor": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "bug_commenter": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "bug_subscriber": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "structural_subscriber": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "owner": Reference(schema=Interface),
    # Really IPerson, patched in lp.bugs.interfaces.webservice.
    "affected_user": Reference(schema=Interface),
    "has_patch": copy_field(IBugTaskSearch["has_patch"]),
    "has_cve": copy_field(IBugTaskSearch["has_cve"]),
    "tags": copy_field(IBugTaskSearch["tag"]),
    "tags_combinator": copy_field(IBugTaskSearch["tags_combinator"]),
    "omit_duplicates": copy_field(IBugTaskSearch["omit_dupes"]),
    "status_upstream": copy_field(IBugTaskSearch["status_upstream"]),
    "milestone": copy_field(IBugTaskSearch["milestone"]),
    "component": copy_field(IBugTaskSearch["component"]),
    "nominated_for": Reference(schema=Interface),
    "has_no_package": copy_field(IBugTaskSearch["has_no_package"]),
    "linked_branches": Choice(
        title=_(
            "Search for bugs that are linked to branches or for bugs "
            "that are not linked to branches."
        ),
        vocabulary=BugBranchSearch,
        required=False,
    ),
    "modified_since": Datetime(
        title=_(
            "Search for bugs that have been modified since the given " "date."
        ),
        required=False,
    ),
    "created_since": Datetime(
        title=_(
            "Search for bugs that have been created since the given " "date."
        ),
        required=False,
    ),
    "created_before": Datetime(
        title=_("Search for bugs that were created before the given " "date."),
        required=False,
    ),
}

search_tasks_params_for_api_default = dict(
    search_tasks_params_common,
    omit_targeted=copy_field(IBugTaskSearch["omit_targeted"]),
)

search_tasks_params_for_api_devel = dict(
    search_tasks_params_common,
    omit_targeted=copy_field(IBugTaskSearch["omit_targeted"], default=False),
    linked_blueprints=Choice(
        title=_(
            "Search for bugs that are linked to blueprints or for "
            "bugs that are not linked to blueprints."
        ),
        vocabulary=BugBlueprintSearch,
        required=False,
    ),
)


BUG_POLICY_ALLOWED_TYPES = {
    BugSharingPolicy.PUBLIC: FREE_INFORMATION_TYPES,
    BugSharingPolicy.PUBLIC_OR_PROPRIETARY: NON_EMBARGOED_INFORMATION_TYPES,
    BugSharingPolicy.PROPRIETARY_OR_PUBLIC: NON_EMBARGOED_INFORMATION_TYPES,
    BugSharingPolicy.PROPRIETARY: (InformationType.PROPRIETARY,),
    BugSharingPolicy.FORBIDDEN: [],
    BugSharingPolicy.EMBARGOED_OR_PROPRIETARY: PROPRIETARY_INFORMATION_TYPES,
}

BUG_POLICY_DEFAULT_TYPES = {
    BugSharingPolicy.PUBLIC: InformationType.PUBLIC,
    BugSharingPolicy.PUBLIC_OR_PROPRIETARY: InformationType.PUBLIC,
    BugSharingPolicy.PROPRIETARY_OR_PUBLIC: InformationType.PROPRIETARY,
    BugSharingPolicy.PROPRIETARY: InformationType.PROPRIETARY,
    BugSharingPolicy.FORBIDDEN: None,
    BugSharingPolicy.EMBARGOED_OR_PROPRIETARY: InformationType.EMBARGOED,
}


DISABLE_BUG_WEBHOOKS_FEATURE_FLAG = "bugs.webhooks.disabled"


@exported_as_webservice_entry(as_of="beta")
class IHasBugs(Interface):
    """An entity which has a collection of bug tasks."""

    # searchTasks devel API declaration.
    @call_with(search_params=None, user=REQUEST_USER)
    @operation_parameters(**search_tasks_params_for_api_devel)
    @operation_returns_collection_of(IBugTask)
    @export_read_operation()
    #
    # Pop the *default* version (decorators are run last to first).
    @operation_removed_in_version("devel")
    #
    # searchTasks default API declaration.
    @call_with(search_params=None, user=REQUEST_USER)
    @operation_parameters(**search_tasks_params_for_api_default)
    @operation_returns_collection_of(IBugTask)
    @export_read_operation()
    @operation_for_version("beta")
    def searchTasks(
        search_params,
        user=None,
        order_by=None,
        search_text=None,
        status=None,
        importance=None,
        assignee=None,
        bug_reporter=None,
        bug_supervisor=None,
        bug_commenter=None,
        bug_subscriber=None,
        owner=None,
        affected_user=None,
        has_patch=None,
        has_cve=None,
        distribution=None,
        tags=None,
        tags_combinator=BugTagsSearchCombinator.ALL,
        omit_duplicates=True,
        omit_targeted=None,
        status_upstream=None,
        milestone=None,
        component=None,
        nominated_for=None,
        sourcepackagename=None,
        has_no_package=None,
        linked_branches=None,
        linked_blueprints=None,
        structural_subscriber=None,
        modified_since=None,
        created_since=None,
        created_before=None,
        information_type=None,
    ):
        """Search the IBugTasks reported on this entity.

        :search_params: a BugTaskSearchParams object

        Return an iterable of matching results.

        Note: milestone is currently ignored for all IBugTargets
        except IProduct.
        """

    def getBugTaskWeightFunction():
        """Return a function that is used to weight the bug tasks.

        The function should take a bug task as a parameter and return
        an OrderedBugTask.

        The ordered bug tasks are used to choose the most relevant bug task
        for any particular context.
        """


class IHasExpirableBugs(Interface):
    """Marker interface for entities supporting querying expirable bugs"""


@exported_as_webservice_entry(as_of="beta")
class IBugTarget(IHasBugs):
    """An entity on which a bug can be reported.

    Examples include an IDistribution, an IDistroSeries and an
    IProduct.
    """

    # XXX Brad Bollenbach 2006-08-02 bug=54974: This attribute name smells.
    bugtargetdisplayname = Attribute("A display name for this bug target")
    bugtargetname = Attribute("The target as shown in mail notifications.")

    bug_target_parent = Attribute("The parent entity of this bug target.")

    pillar = Attribute("The pillar containing this target.")

    bug_reporting_guidelines = exported(
        Text(
            title=("Helpful guidelines for reporting a bug"),
            description=(
                "These guidelines will be shown to "
                "everyone reporting a bug and should be "
                "text or a bulleted list with your particular "
                "requirements, if any."
            ),
            required=False,
            max_length=50000,
        )
    )

    content_templates = exported(
        Dict(
            title=("Templates to use for reporting a bug"),
            description=(
                "This pre-defined template will be given to the "
                "users to guide them when reporting a bug. "
            ),
            key_type=TextLine(),
            value_type=Dict(
                key_type=TextLine(),
                value_type=Text(
                    required=False,
                    max_length=50000,
                ),
                required=False,
                max_length=50000,
            ),
            required=False,
            max_length=50000,
            constraint=validate_content_templates,
        )
    )

    bug_reported_acknowledgement = exported(
        Text(
            title=("After reporting a bug, I can expect the following."),
            description=(
                "This message of acknowledgement will be displayed "
                "to anyone after reporting a bug."
            ),
            required=False,
            max_length=50000,
        )
    )

    enable_bugfiling_duplicate_search = Bool(
        title="Search for possible duplicate bugs when a new bug is filed",
        description=(
            "If enabled, Launchpad searches the project for bugs which "
            "could match the summary given by the bug reporter. However, "
            "this can lead users to mistake an existing bug as the one "
            "they want to report. This can happen for example for hardware "
            "related bugs where the one symptom can be caused by "
            "completely different hardware and drivers."
        ),
        required=False,
    )

    def createBug(bug_params):
        """Create a new bug on this target.

        bug_params is an instance of `CreateBugParams`.
        """


# We assign the schema for an `IBugTask` attribute here
# in order to avoid circular dependencies.
patch_reference_property(IBugTask, "target", IBugTarget)
patch_plain_parameter_type(
    IBugTask, "transitionToTarget", "target", IBugTarget
)


class IHasOfficialBugTags(Interface):
    """An entity that exposes a set of official bug tags."""

    official_bug_tags = exported(
        List(
            title=_("Official Bug Tags"),
            description=_("The list of bug tags defined as official."),
            value_type=Tag(),
            readonly=True,
        )
    )

    def getUsedBugTagsWithOpenCounts(user, tag_limit=0, include_tags=None):
        """Return name and bug count of tags having open bugs.

        :param user: The user who wants the report.
        :param tag_limit: The number of tags to return (excludes those found
            by matching include_tags). If 0 then all tags are returned. If
            non-zero then the most frequently used tags are returned.
        :param include_tags: A list of string tags to return irrespective of
            usage. Tags in this list that have no open bugs are returned with
            a count of 0. May be None if there are tags to require inclusion
            of.
        :return: A dict from tag -> count.
        """

    def _getOfficialTagClause():
        """Get the storm clause for finding this targets tags."""


class IOfficialBugTagTargetPublic(IHasOfficialBugTags):
    """Public attributes for `IOfficialBugTagTarget`."""

    official_bug_tags = copy_field(
        IHasOfficialBugTags["official_bug_tags"], readonly=False
    )


class IOfficialBugTagTargetRestricted(Interface):
    """Restricted methods for `IOfficialBugTagTarget`."""

    @operation_parameters(tag=Tag(title="The official bug tag", required=True))
    @export_write_operation()
    @operation_for_version("beta")
    def addOfficialBugTag(tag):
        """Add tag to the official bug tags of this target."""

    @operation_parameters(tag=Tag(title="The official bug tag", required=True))
    @export_write_operation()
    @operation_for_version("beta")
    def removeOfficialBugTag(tag):
        """Remove tag from the official bug tags of this target."""


class IOfficialBugTagTarget(
    IOfficialBugTagTargetPublic, IOfficialBugTagTargetRestricted
):
    """An entity for which official bug tags can be defined."""

    # XXX intellectronica 2009-03-16 bug=342413
    # We can start using straight inheritance once it becomes possible
    # to export objects implementing multiple interfaces in the
    # webservice API.


class IOfficialBugTag(Interface):
    """Official bug tags for a product, a project or a distribution."""

    tag = Tag(title="The official bug tag", required=True)

    target = Object(
        title="The target of this bug tag.",
        schema=IOfficialBugTagTarget,
        description=(
            "The distribution or product having this official bug tag."
        ),
    )


class ISeriesBugTarget(Interface):
    """An `IBugTarget` which is a series."""

    series = Attribute(
        "The product or distribution series of this series bug target."
    )
    bugtarget_parent = Attribute(
        "Non-series parent of this series bug target."
    )
