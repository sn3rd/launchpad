Introduction
============

Bugs are some of the more complex objects in Launchpad. They have a
lot of state and many relationships to other objects.


Bugs themselves
---------------

Bugs are indexed by number beneath the top-level collection.

    >>> from lazr.restful.testing.webservice import (
    ...     pprint_collection,
    ...     pprint_entry,
    ... )
    >>> bug_one = webservice.get("/bugs/1").jsonBody()
    >>> pprint_entry(bug_one)
    activity_collection_link: 'http://.../bugs/1/activity'
    attachments_collection_link: 'http://.../bugs/1/attachments'
    bug_tasks_collection_link: 'http://.../bugs/1/bug_tasks'
    bug_watches_collection_link: 'http://.../bugs/1/bug_watches'
    can_expire: False
    cves_collection_link: 'http://.../bugs/1/cves'
    date_created: '2004-01-01T20:58:04.553583+00:00'
    date_last_message: None
    date_last_updated: '2006-05-19T06:37:40.344941+00:00'
    date_made_private: None
    description: 'Firefox needs to support embedded SVG...'
    duplicate_of_link: None
    duplicates_collection_link: 'http://.../bugs/1/duplicates'
    heat: 0
    id: 1
    information_type: 'Public'
    latest_patch_uploaded: None
    linked_branches_collection_link: 'http://.../bugs/1/linked_branches'
    message_count: 2
    messages_collection_link: 'http://.../bugs/1/messages'
    name: None
    number_of_duplicates: 0
    other_users_affected_count_with_dupes: 0
    owner_link: 'http://.../~name12'
    private: False
    resource_type_link: 'http://.../#bug'
    security_related: False
    self_link: 'http://.../bugs/1'
    subscriptions_collection_link: 'http://.../bugs/1/subscriptions'
    tags: []
    title: 'Firefox does not support SVG'
    users_affected_collection_link: 'http://.../bugs/1/users_affected'
    users_affected_count: 0
    users_affected_count_with_dupes: 0
    users_affected_with_dupes_collection_link:
      'http://.../bugs/1/users_affected_with_dupes'
    users_unaffected_collection_link: 'http://.../bugs/1/users_unaffected'
    users_unaffected_count: 0
    vulnerabilities_collection_link: 'http://.../bugs/1/vulnerabilities'
    web_link: 'http://bugs.../bugs/1'
    who_made_private_link: None

Bugs have relationships to other bugs, like "duplicate_of".

    >>> duplicates_of_five = webservice.get("/bugs/5/duplicates").jsonBody()[
    ...     "entries"
    ... ]
    >>> len(duplicates_of_five)
    1
    >>> duplicates_of_five[0]["id"]
    6

    >>> bug_six_url = duplicates_of_five[0]["self_link"]
    >>> bug_six = webservice.get(bug_six_url).jsonBody()
    >>> print(bug_six["duplicate_of_link"])
    http://.../bugs/5

To create a new bug we use the createBug operation. This operation
takes a target parameter which must be a either a Product, a
Distribution or a DistributionSourcePackage.

    >>> project_collection = webservice.get(
    ...     "/projects?ws.op=search&text=firefox"
    ... ).jsonBody()
    >>> firefox = project_collection["entries"][0]
    >>> response = webservice.named_post(
    ...     "/bugs",
    ...     "createBug",
    ...     title="Test bug",
    ...     description="Test bug",
    ...     target=firefox["self_link"],
    ... )
    >>> print(response)
    HTTP/1.1 201 Created
    ...
    Location: http://.../bugs/...
    ...
    >>> new_bug_id = int(response.getHeader("Location").rsplit("/", 1)[-1])

    >>> print(
    ...     webservice.named_post(
    ...         "/bugs",
    ...         "createBug",
    ...         title="Test bug",
    ...         description="Test bug",
    ...         target=webservice.getAbsoluteUrl("/ubuntu"),
    ...     )
    ... )
    HTTP/1.1 201 Created
    ...
    Location: http://.../bugs/...
    ...

    >>> response = webservice.named_post(
    ...     "/bugs",
    ...     "createBug",
    ...     title="Test bug",
    ...     description="Test bug",
    ...     target=webservice.getAbsoluteUrl("/ubuntu/+source/evolution"),
    ... )
    >>> print(response)
    HTTP/1.1 201 Created
    ...
    Location: http://.../bugs/...
    ...

    >>> new_bug = webservice.get(response.getHeader("Location")).jsonBody()

Activity is recorded and notifications are sent for newly created
bugs.

    >>> from lp.bugs.interfaces.bug import IBugSet
    >>> from lp.bugs.model.bugnotification import BugNotification
    >>> from lp.services.database.interfaces import IStore
    >>> from lp.testing import ANONYMOUS, login, logout
    >>> from zope.component import getUtility

    >>> login(ANONYMOUS)
    >>> bug = getUtility(IBugSet).get(new_bug["id"])

    >>> for activity in bug.activity:
    ...     print(
    ...         "%s, %s, %s"
    ...         % (
    ...             activity.whatchanged,
    ...             activity.message,
    ...             activity.person.name,
    ...         )
    ...     )
    ...
    bug, added bug, salgado

    >>> for notification in (
    ...     IStore(BugNotification)
    ...     .find(BugNotification, bug=bug)
    ...     .order_by(BugNotification.id)
    ... ):
    ...     print(
    ...         "%s, %s, %s"
    ...         % (
    ...             notification.message.owner.name,
    ...             notification.is_comment,
    ...             notification.message.text_contents,
    ...         )
    ...     )
    salgado, True, Test bug

    >>> logout()

A ProductSeries can't be the target of a new bug.

    >>> print(
    ...     webservice.named_post(
    ...         "/bugs",
    ...         "createBug",
    ...         title="Test bug",
    ...         description="Test bug",
    ...         target=webservice.getAbsoluteUrl("/firefox/1.0"),
    ...     )
    ... )
    HTTP/1.1 400 Bad Request
    ...
    Can't create a bug on a series. Create it with a non-series
    task instead, and target it to the series afterwards.

That operation will fail if the client doesn't specify the product or
distribution in which the bug exists.

    >>> print(
    ...     webservice.named_post(
    ...         "/bugs", "createBug", title="Test bug", description="Test bug"
    ...     )
    ... )
    HTTP/1.1 400 Bad Request
    ...
    target: Required input is missing.

To mark a bug as private, we patch the `private` attribute of the bug.

    >>> import json
    >>> bug_twelve = webservice.get("/bugs/12").jsonBody()
    >>> bug_twelve["private"]
    False
    >>> print(
    ...     webservice.patch(
    ...         bug_twelve["self_link"],
    ...         "application/json",
    ...         json.dumps(dict(private=True)),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...
    >>> bug_twelve = webservice.get("/bugs/12").jsonBody()
    >>> bug_twelve["private"]
    True
    >>> print(
    ...     webservice.patch(
    ...         bug_twelve["self_link"],
    ...         "application/json",
    ...         json.dumps(dict(private=False)),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...

Similarly, to mark a bug as a duplicate, we patch the `duplicate_of_link`
attribute of the bug.

    >>> print(bug_twelve["duplicate_of_link"])
    None
    >>> print(
    ...     webservice.patch(
    ...         bug_twelve["self_link"],
    ...         "application/json",
    ...         json.dumps(dict(duplicate_of_link=bug_one["self_link"])),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...
    >>> bug_twelve = webservice.get("/bugs/12").jsonBody()
    >>> print(bug_twelve["duplicate_of_link"])
    http://api.launchpad.test/beta/bugs/1

Now set it back to none:

    >>> print(
    ...     webservice.patch(
    ...         bug_twelve["self_link"],
    ...         "application/json",
    ...         json.dumps(dict(duplicate_of_link=None)),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...
    >>> bug_twelve = webservice.get("/bugs/12").jsonBody()
    >>> print(bug_twelve["duplicate_of_link"])
    None

Marking a bug as duplicate follows the same validation rules as available in
the web UI. It is impossible, for example, to create circular relationships.
Due to bug #1088358 the error is escaped as if it was HTML.

    >>> dupe_url = webservice.getAbsoluteUrl("/bugs/%d" % new_bug_id)
    >>> print(
    ...     webservice.patch(
    ...         dupe_url,
    ...         "application/json",
    ...         json.dumps(
    ...             dict(
    ...                 duplicate_of_link=webservice.getAbsoluteUrl("/bugs/5")
    ...             )
    ...         ),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...

    >>> print(
    ...     webservice.patch(
    ...         webservice.getAbsoluteUrl("/bugs/5"),
    ...         "application/json",
    ...         json.dumps(dict(duplicate_of_link=dupe_url)),
    ...     )
    ... )
    HTTP/1.1 400 Bad Request
    ...
    Bug ... is already a duplicate of bug 5. You
    can only mark a bug report as duplicate of one that
    isn&#x27;t a duplicate itself...

    >>> print(
    ...     webservice.patch(
    ...         dupe_url,
    ...         "application/json",
    ...         json.dumps(dict(duplicate_of_link=None)),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...


Bugs as message targets
-----------------------

Each bug has a collection of messages.

    >>> messages = webservice.get("/bugs/5/messages").jsonBody()["entries"]
    >>> pprint_entry(messages[0])
    bug_attachments_collection_link:
     'http://.../firefox/+bug/5/comments/0/bug_attachments'
    content: 'All ways of downloading firefox should provide...'
    date_created: '2005-01-14T17:27:03.702622+00:00'
    date_deleted: None
    date_last_edited: None
    owner_link: 'http://.../~name12'
    parent_link: None
    resource_type_link: 'http://.../#message'
    self_link: 'http://.../firefox/+bug/5/comments/0'
    subject: 'Firefox install instructions should be complete'
    web_link: 'http://bugs.../firefox/+bug/5/comments/0'

The messages are stored beneath the bug-specific collection. Their
URLs are based on their position with respect to the
bug. /firefox/+bug/5/comments/0 is the first message for bug 5, and it's
different from /firefox/+bug/1/comments/0.

    >>> print(messages[0]["self_link"])
    http://.../firefox/+bug/5/comments/0

    >>> message = webservice.get(messages[0]["self_link"]).jsonBody()
    >>> message == messages[0]
    True

There is no top-level collection of messages; they only exist in
relation to some bug.

    >>> webservice.get("/messages").status
    404

Bug messages can be accessed anonymously.

    >>> messages = anon_webservice.get("/bugs/5/messages").jsonBody()[
    ...     "entries"
    ... ]
    >>> print(messages[0]["self_link"])
    http://.../firefox/+bug/5/comments/0

We can add a new message to a bug by calling the newMessage method.

    >>> print(
    ...     webservice.named_post(
    ...         "/bugs/5",
    ...         "newMessage",
    ...         subject="A new message",
    ...         content=(
    ...             "This is a new message added through the webservice API."
    ...         ),
    ...     )
    ... )
    HTTP/1.1 201 Created...
    Content-Length: 0
    ...
    Location: http://api.launchpad.test/beta/firefox/+bug/5/comments/1
    ...

    >>> pprint_entry(webservice.get("/firefox/+bug/5/comments/1").jsonBody())
    bug_attachments_collection_link: ...
    content: 'This is a new message added through the webservice API.'
    ...
    resource_type_link: 'http://api.launchpad.test/beta/#message'
    self_link: 'http://api.launchpad.test/beta/firefox/+bug/5/comments/1'
    subject: 'A new message'
    web_link: '...'

We don't have to submit a subject when we add a new message.

    >>> print(
    ...     webservice.named_post(
    ...         "/bugs/5",
    ...         "newMessage",
    ...         content="This is a new message with no subject.",
    ...     )
    ... )
    HTTP/1.1 201 Created...
    Content-Length: 0
    ...
    Location: http://api.launchpad.test/beta/firefox/+bug/5/comments/2
    ...

    >>> pprint_entry(webservice.get("/firefox/+bug/5/comments/2").jsonBody())
    bug_attachments_collection_link: ...
    content: 'This is a new message with no subject.'
    ...
    self_link: 'http://api.launchpad.test/beta/firefox/+bug/5/comments/2'
    subject: 'Re: Firefox install instructions should be complete'
    web_link: '...'

The "visible" field is exported in the "devel" version of the web service API
and it defaults to True.

    >>> response = webservice.get("/bugs/5/messages", api_version="devel")
    >>> messages = response.jsonBody()["entries"]
    >>> pprint_entry(messages[0])
    bug_attachments_collection_link:
     'http://.../firefox/+bug/5/comments/0/bug_attachments'
    content: 'All ways of downloading firefox should provide...'
    date_created: '2005-01-14T17:27:03.702622+00:00'
    date_deleted: None
    date_last_edited: None
    owner_link: 'http://.../~name12'
    parent_link: None
    resource_type_link: 'http://.../#message'
    revisions_collection_link: 'http://.../firefox/+bug/5/comments/0/revisions'
    self_link: 'http://.../firefox/+bug/5/comments/0'
    subject: 'Firefox install instructions should be complete'
    visible: True
    web_link: 'http://bugs.../firefox/+bug/5/comments/0'

The "visible" field will be False when a comment is hidden.

    >>> response = webservice.named_post(
    ...     "/bugs/5", "setCommentVisibility", comment_number=0, visible=False
    ... )
    >>> response.status
    200
    >>> response = webservice.get("/bugs/5/messages", api_version="devel")
    >>> messages = response.jsonBody()["entries"]
    >>> pprint_entry(messages[0])
    bug_attachments_collection_link:
     'http://.../firefox/+bug/5/comments/0/bug_attachments'
    content: 'All ways of downloading firefox should provide...'
    date_created: '2005-01-14T17:27:03.702622+00:00'
    date_deleted: None
    date_last_edited: None
    owner_link: 'http://.../~name12'
    parent_link: None
    resource_type_link: 'http://.../#message'
    revisions_collection_link: 'http://.../firefox/+bug/5/comments/0/revisions'
    self_link: 'http://.../firefox/+bug/5/comments/0'
    subject: 'Firefox install instructions should be complete'
    visible: False
    web_link: 'http://bugs.../firefox/+bug/5/comments/0'

Bug tasks
---------

Each bug may be associated with one or more bug tasks. Much of the
data in a bug task is derived from the bug.

    >>> from operator import itemgetter
    >>> bug_one_bugtasks_url = bug_one["bug_tasks_collection_link"]
    >>> bug_one_bugtasks = sorted(
    ...     webservice.get(bug_one_bugtasks_url).jsonBody()["entries"],
    ...     key=itemgetter("self_link"),
    ... )
    >>> len(bug_one_bugtasks)
    3

    >>> pprint_entry(bug_one_bugtasks[0])
    assignee_link: None
    bug_link: 'http://.../bugs/1'
    bug_target_display_name: 'mozilla-firefox (Debian)'
    bug_target_name: 'mozilla-firefox (Debian)'
    bug_watch_link: 'http://.../bugs/1/+watch/8'
    date_assigned: '2005-01-04T11:07:20.584746+00:00'
    date_closed: None
    date_confirmed: None
    date_created: '2004-01-04T03:49:22.790240+00:00'
    date_deferred: None
    date_fix_committed: None
    date_fix_released: None
    date_in_progress: None
    date_incomplete: None
    date_left_closed: None
    date_left_new: None
    date_triaged: None
    importance: 'Low'
    is_complete: False
    milestone_link: None
    owner_link: 'http://.../~name12'
    related_tasks_collection_link:
      'http://api.../debian/+source/mozilla-firefox/+bug/1/related_tasks'
    resource_type_link: 'http://.../#bug_task'
    self_link: 'http://api.../debian/+source/mozilla-firefox/+bug/1'
    status: 'Confirmed'
    target_link: 'http://api.../debian/+source/mozilla-firefox'
    title:
      'Bug #1 in mozilla-firefox (Debian): "Firefox does not support SVG"'
    web_link: 'http://bugs.../debian/+source/mozilla-firefox/+bug/1'

The collection of bug tasks is not exposed as a resource:

    >>> webservice.get("/bug_tasks").status
    404

It's possible to change the task's assignee.

    >>> patch = {"assignee_link": webservice.getAbsoluteUrl("/~cprov")}
    >>> bugtask_path = bug_one_bugtasks[0]["self_link"]
    >>> print(
    ...     webservice.patch(
    ...         bugtask_path, "application/json", json.dumps(patch)
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...

    >>> print(webservice.get(bugtask_path).jsonBody()["assignee_link"])
    http://.../~cprov


The task's importance can be modified directly.

    >>> body = webservice.get(bugtask_path).jsonBody()
    >>> print(body["importance"])
    Low

    >>> patch = {"importance": "High"}
    >>> print(
    ...     webservice.patch(
    ...         bugtask_path, "application/json", json.dumps(patch)
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...

    >>> body = webservice.get(bugtask_path).jsonBody()
    >>> print(body["importance"])
    High

Only bug supervisors or people who can otherwise edit the bugtask's
bug_target_parent are authorised to edit the importance.

    >>> print(
    ...     user_webservice.named_post(
    ...         bugtask_path, "transitionToImportance", importance="Low"
    ...     )
    ... )
    HTTP/1.1 401 Unauthorized...

    >>> body = webservice.get(bugtask_path).jsonBody()
    >>> print(body["importance"])
    High

The task's status can also be modified directly.

    >>> print(webservice.get(bugtask_path).jsonBody()["status"])
    Confirmed

    >>> patch = {"status": "Fix Committed"}
    >>> print(
    ...     webservice.patch(
    ...         bugtask_path, "application/json", json.dumps(patch)
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...

    >>> print(webservice.get(bugtask_path).jsonBody()["status"])
    Fix Committed

If an error occurs during a request that sets both 'status' and
'importance', neither one will be set.

    >>> task = webservice.get(bugtask_path).jsonBody()
    >>> print(task["status"])
    Fix Committed
    >>> print(task["importance"])
    High

    >>> patch = {"importance": "High", "status": "No Such Status"}
    >>> print(
    ...     webservice.patch(
    ...         bugtask_path, "application/json", json.dumps(patch)
    ...     )
    ... )
    HTTP/1.1 400 Bad Request...

    >>> task = webservice.get(bugtask_path).jsonBody()
    >>> print(task["status"])
    Fix Committed
    >>> print(task["importance"])
    High

The milestone can only be set by appropriately privileged users.

    >>> print(webservice.get(bugtask_path).jsonBody()["milestone_link"])
    None

    >>> patch = {
    ...     "milestone_link": webservice.getAbsoluteUrl(
    ...         "/debian/+milestone/3.1"
    ...     )
    ... }
    >>> print(
    ...     webservice.patch(
    ...         bugtask_path, "application/json", json.dumps(patch)
    ...     )
    ... )
    HTTP/1.1 209 Content Returned...

    >>> print(webservice.get(bugtask_path).jsonBody()["milestone_link"])
    http://.../debian/+milestone/3.1

We need to ensure the milestone we try and set is different to the current
value because lazr restful now discards attempts to patch an attribute with an
unchanged value.

    >>> patch = {
    ...     "milestone_link": webservice.getAbsoluteUrl(
    ...         "/debian/+milestone/3.1-rc1"
    ...     )
    ... }
    >>> print(
    ...     user_webservice.patch(
    ...         bugtask_path, "application/json", json.dumps(patch)
    ...     )
    ... )
    HTTP/1.1 401 Unauthorized...

    >>> print(webservice.get(bugtask_path).jsonBody()["milestone_link"])
    http://.../debian/+milestone/3.1

We can change the task's target. Here we change the task's target from
the mozilla-firefox package to alsa-utils. Only published packages can
have tasks, so we first add a publication.

    >>> from lp.registry.interfaces.distribution import IDistributionSet
    >>> login("admin@canonical.com")
    >>> debian = getUtility(IDistributionSet).getByName("debian")
    >>> ignored = factory.makeSourcePackagePublishingHistory(
    ...     distroseries=debian.currentseries, sourcepackagename="evolution"
    ... )
    >>> logout()
    >>> print(
    ...     webservice.named_post(
    ...         task["self_link"],
    ...         "transitionToTarget",
    ...         target=webservice.getAbsoluteUrl("/debian/+source/evolution"),
    ...     )
    ... )
    HTTP/1.1 301 Moved Permanently
    ...
    Location: http://api.launchpad.test/beta/debian/+source/evolution/+bug/1
    ...

We can also PATCH the target attribute to accomplish the same thing.

    >>> print(
    ...     webservice.patch(
    ...         task["self_link"].replace("mozilla-firefox", "evolution"),
    ...         "application/json",
    ...         json.dumps(
    ...             {
    ...                 "target_link": webservice.getAbsoluteUrl(
    ...                     "/debian/+source/alsa-utils"
    ...                 )
    ...             }
    ...         ),
    ...     )
    ... )
    HTTP/1.1 301 Moved Permanently
    ...
    Location: http://api.launchpad.test/beta/debian/+source/alsa-utils/+bug/1
    ...

After the operation completed successfully, the task is
now an alsa-utils task.

    >>> task = webservice.get(
    ...     task["self_link"].replace("mozilla-firefox", "alsa-utils")
    ... ).jsonBody()
    >>> print(task["target_link"])
    http://api.../debian/+source/alsa-utils

We can change an upstream task to target a different project.

    >>> product_bugtask = webservice.get(
    ...     webservice.getAbsoluteUrl("/jokosher/+bug/14")
    ... ).jsonBody()
    >>> print(
    ...     webservice.named_post(
    ...         product_bugtask["self_link"],
    ...         "transitionToTarget",
    ...         target=webservice.getAbsoluteUrl("/bzr"),
    ...     )
    ... )
    HTTP/1.1 301 Moved Permanently
    ...
    Location: http://api.launchpad.test/beta/bzr/+bug/14
    ...

If the milestone of a task is on a target other than the new
target, we reset it in order to avoid data inconsistencies.

    >>> firefox_bugtask = webservice.get(
    ...     webservice.getAbsoluteUrl("/firefox/+bug/1")
    ... ).jsonBody()
    >>> patch = {
    ...     "milestone_link": webservice.getAbsoluteUrl(
    ...         "/firefox/+milestone/1.0"
    ...     )
    ... }
    >>> print(
    ...     webservice.patch(
    ...         firefox_bugtask["self_link"],
    ...         "application/json",
    ...         json.dumps(patch),
    ...     )
    ... )
    HTTP/1.1 209 Content Returned
    ...
    <BLANKLINE>
    >>> firefox_bugtask = webservice.get(
    ...     webservice.getAbsoluteUrl("/firefox/+bug/1")
    ... ).jsonBody()
    >>> print(firefox_bugtask["milestone_link"])
    http://api.../firefox/+milestone/1.0
    >>> print(
    ...     webservice.named_post(
    ...         firefox_bugtask["self_link"],
    ...         "transitionToTarget",
    ...         target=webservice.getAbsoluteUrl("/jokosher"),
    ...     )
    ... )
    HTTP/1.1 301 Moved Permanently
    ...
    Location: http://api.launchpad.test/beta/jokosher/+bug/1
    ...
    <BLANKLINE>
    >>> jokosher_bugtask = webservice.get(
    ...     firefox_bugtask["self_link"].replace("firefox", "jokosher")
    ... ).jsonBody()
    >>> print(jokosher_bugtask["milestone_link"])
    None

    >>> print(
    ...     webservice.named_post(
    ...         jokosher_bugtask["self_link"],
    ...         "transitionToTarget",
    ...         target=webservice.getAbsoluteUrl("/firefox"),
    ...     )
    ... )
    HTTP/1.1 301 Moved Permanently
    ...
    Location: http://api.launchpad.test/beta/firefox/+bug/1
    ...

We can change a distribution task to a task with a package from the same
distribution.

    >>> login("foo.bar@canonical.com")
    >>> distro_bugtask = factory.makeBugTask(
    ...     target=getUtility(IDistributionSet).getByName("ubuntu")
    ... )
    >>> distro_bugtask_path = webservice.getAbsoluteUrl(
    ...     canonical_url(distro_bugtask).replace(
    ...         "http://bugs.launchpad.test", ""
    ...     )
    ... )
    >>> logout()

    >>> distro_bugtask = webservice.get(distro_bugtask_path)
    >>> print(
    ...     webservice.named_post(
    ...         distro_bugtask_path,
    ...         "transitionToTarget",
    ...         target=webservice.getAbsoluteUrl(
    ...             "/ubuntu/+source/alsa-utils"
    ...         ),
    ...     )
    ... )
    ... # noqa
    HTTP/1.1 301 Moved Permanently
    ...
    Location: http://api.launchpad.test/beta/ubuntu/+source/alsa-utils/+bug/...
    ...

It's possible to get a list of similar bugs for a bug task by calling
its findSimilarBugs() method. As it happens, there aren't any bugs
similar to bug 1 for Firefox.

    >>> pprint_collection(
    ...     anon_webservice.named_get(
    ...         firefox_bugtask["self_link"], "findSimilarBugs"
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 0
    ---

If we add a new bug that's quite similar to others, findSimilarBugs()
will return something more useful.

    >>> new_bug_response = webservice.named_post(
    ...     "/bugs",
    ...     "createBug",
    ...     title="a",
    ...     description="Test bug",
    ...     target=firefox["self_link"],
    ... )
    >>> new_bug = webservice.get(
    ...     new_bug_response.getHeader("Location")
    ... ).jsonBody()
    >>> new_bug_task = webservice.get(
    ...     webservice.getAbsoluteUrl("/firefox/+bug/%s" % new_bug["id"])
    ... ).jsonBody()

    >>> pprint_collection(
    ...     anon_webservice.named_get(
    ...         new_bug_task["self_link"], "findSimilarBugs"
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 4
    ---
    ...
    id: 1
    ...
    title: 'Firefox does not support SVG'
    ...
    ---
    ...
    id: 4
    ...
    title: 'Reflow problems with complex page layouts'
    ...
    ---
    ...
    id: 5
    ...
    title: 'Firefox install instructions should be complete'
    ...
    ---
    ...
    title: 'Test bug'
    ...


Bug nominations
---------------

A bug may be nominated for any number of distro or product series.
Nominations can be inspected, created, approved and declined through
the webservice.

Eric creates Fooix 0.1 and 0.2.

    >>> login("foo.bar@canonical.com")
    >>> eric = factory.makePerson(name="eric")
    >>> fooix = factory.makeProduct(name="fooix", owner=eric)
    >>> fx01 = fooix.newSeries(eric, "0.1", "The 0.1.x series")
    >>> fx02 = fooix.newSeries(eric, "0.2", "The 0.2.x series")
    >>> debuntu = factory.makeDistribution(name="debuntu", owner=eric)
    >>> debuntu50 = debuntu.newSeries(
    ...     "5.0", "5.0", "5.0", "5.0", "5.0", "5.0", None, eric
    ... )
    >>> bug = factory.makeBug(target=fooix)
    >>> logout()

Initially there are no nominations.

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/bugs/%d" % bug.id, "getNominations"
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 0
    ---

    >>> from zope.component import getUtility
    >>> from zope.security.proxy import removeSecurityProxy
    >>> login("foo.bar@canonical.com")
    >>> john = factory.makePerson(name="john")
    >>> debuntu = removeSecurityProxy(debuntu)
    >>> debuntu.bug_supervisor = john
    >>> fooix = removeSecurityProxy(fooix)
    >>> fooix.bug_supervisor = john
    >>> logout()

    >>> from lp.testing.pages import webservice_for_person
    >>> from lp.services.webapp.interfaces import OAuthPermission

    >>> john_webservice = webservice_for_person(
    ...     john, permission=OAuthPermission.WRITE_PRIVATE
    ... )

But John, an unprivileged user, wants it fixed in Fooix 0.1.1.

    >>> print(
    ...     john_webservice.named_post(
    ...         "/bugs/%d" % bug.id,
    ...         "addNomination",
    ...         target=john_webservice.getAbsoluteUrl("/fooix/0.1"),
    ...     )
    ... )
    HTTP/1.1 201 Created
    ...
    Location: http://.../bugs/.../nominations/...
    ...

    >>> nominations = webservice.named_get(
    ...     "/bugs/%d" % bug.id, "getNominations"
    ... ).jsonBody()
    >>> pprint_collection(nominations)
    start: 0
    total_size: 1
    ---
    bug_link: 'http://.../bugs/...'
    date_created: '...'
    date_decided: None
    decider_link: None
    distroseries_link: None
    owner_link: 'http://.../~john'
    productseries_link: 'http://.../fooix/0.1'
    resource_type_link: 'http://.../#bug_nomination'
    self_link: 'http://.../bugs/.../nominations/...'
    status: 'Nominated'
    target_link: 'http://.../fooix/0.1'
    ---


John cannot approve or decline the nomination.

    >>> nom_url = nominations["entries"][0]["self_link"]

    >>> print(john_webservice.named_get(nom_url, "canApprove").jsonBody())
    False

    >>> print(john_webservice.named_post(nom_url, "approve"))
    HTTP/1.1 401 Unauthorized...

    >>> print(john_webservice.named_post(nom_url, "decline"))
    HTTP/1.1 401 Unauthorized...

    >>> login("foo.bar@canonical.com")
    >>> len(bug.bugtasks)
    1
    >>> logout()

Eric, however, can and does decline the nomination.

    >>> eric_webservice = webservice_for_person(
    ...     eric, permission=OAuthPermission.WRITE_PRIVATE
    ... )
    >>> print(eric_webservice.named_post(nom_url, "decline"))
    HTTP/1.1 200 Ok...

    >>> print(eric_webservice.named_get(nom_url, "canApprove").jsonBody())
    True

    >>> login("foo.bar@canonical.com")
    >>> len(bug.bugtasks)
    1
    >>> logout()

John is disappointed to see that the nomination was declined.

    >>> nominations = john_webservice.named_get(
    ...     "/bugs/%d" % bug.id, "getNominations"
    ... ).jsonBody()
    >>> pprint_collection(nominations)
    start: 0
    total_size: 1
    ---
    bug_link: 'http://.../bugs/...'
    date_created: '...'
    date_decided: '...'
    decider_link: 'http://.../~eric'
    distroseries_link: None
    owner_link: 'http://.../~john'
    productseries_link: 'http://.../fooix/0.1'
    resource_type_link: 'http://.../#bug_nomination'
    self_link: 'http://.../bugs/.../nominations/...'
    status: 'Declined'
    target_link: 'http://.../fooix/0.1'
    ---

Eric changes his mind, and approves the nomination.

    >>> print(eric_webservice.named_post(nom_url, "approve"))
    HTTP/1.1 200 Ok...

This marks the nomination as Approved, and creates a new task.

    >>> nominations = webservice.named_get(
    ...     "/bugs/%d" % bug.id, "getNominations"
    ... ).jsonBody()
    >>> pprint_collection(nominations)
    start: 0
    total_size: 1
    ---
    bug_link: 'http://.../bugs/...'
    date_created: '...'
    date_decided: '...'
    decider_link: 'http://.../~eric'
    distroseries_link: None
    owner_link: 'http://.../~john'
    productseries_link: 'http://.../fooix/0.1'
    resource_type_link: 'http://.../#bug_nomination'
    self_link: 'http://.../bugs/.../nominations/...'
    status: 'Approved'
    target_link: 'http://.../fooix/0.1'
    ---

    >>> login("foo.bar@canonical.com")
    >>> len(bug.bugtasks)
    2
    >>> logout()

Eric cannot change his mind and decline the approved task.

    >>> print(eric_webservice.named_post(nom_url, "decline"))
    HTTP/1.1 400 Bad Request
    ...
    Cannot decline an approved nomination.

    >>> login("foo.bar@canonical.com")
    >>> len(bug.bugtasks)
    2
    >>> logout()

While he can approve it again, it's a no-op.

    >>> print(eric_webservice.named_post(nom_url, "approve"))
    HTTP/1.1 200 Ok...

    >>> login("foo.bar@canonical.com")
    >>> len(bug.bugtasks)
    2
    >>> logout()

A bug cannot be nominated for a non-series.

    >>> print(
    ...     john_webservice.named_get(
    ...         "/bugs/%d" % bug.id,
    ...         "canBeNominatedFor",
    ...         target=john_webservice.getAbsoluteUrl("/fooix"),
    ...     ).jsonBody()
    ... )
    False

    >>> print(
    ...     john_webservice.named_post(
    ...         "/bugs/%d" % bug.id,
    ...         "addNomination",
    ...         target=john_webservice.getAbsoluteUrl("/fooix"),
    ...     )
    ... )
    HTTP/1.1 400 Bad Request
    ...
    This bug cannot be nominated for Fooix.

The bug also can't be nominated for Debuntu 5.0, as it has no
Debuntu tasks.

    >>> print(
    ...     john_webservice.named_get(
    ...         "/bugs/%d" % bug.id,
    ...         "canBeNominatedFor",
    ...         target=john_webservice.getAbsoluteUrl("/debuntu/5.0"),
    ...     ).jsonBody()
    ... )
    False

    >>> print(
    ...     john_webservice.named_post(
    ...         "/bugs/%d" % bug.id,
    ...         "addNomination",
    ...         target=john_webservice.getAbsoluteUrl("/debuntu/5.0"),
    ...     )
    ... )
    HTTP/1.1 400 Bad Request
    ...
    This bug cannot be nominated for Debuntu 5.0.

Bug subscriptions
-----------------

We can get the collection of subscriptions to a bug.

    >>> bug_one_subscriptions_url = bug_one["subscriptions_collection_link"]
    >>> subscriptions = webservice.get(bug_one_subscriptions_url).jsonBody()
    >>> subscription_entries = sorted(
    ...     subscriptions["entries"], key=itemgetter("self_link")
    ... )
    >>> for entry in subscription_entries:
    ...     pprint_entry(entry)
    ...     print()
    ...
    bug_link: 'http://.../bugs/1'
    date_created: '2006-10-16T18:31:43.156104+00:00'
    person_link: 'http://.../~name12'
    resource_type_link: 'http://.../#bug_subscription'
    self_link: 'http://.../bugs/1/+subscription/name12'
    subscribed_by_link: 'http://.../~janitor'
    <BLANKLINE>
    bug_link: 'http://.../bugs/1'
    date_created: '2006-10-16T18:31:43.154816+00:00'
    person_link: 'http://.../~stevea'
    resource_type_link: 'http://.../#bug_subscription'
    self_link: 'http://.../bugs/1/+subscription/stevea'
    subscribed_by_link: 'http://.../~janitor'
    <BLANKLINE>

Each subscription can be accessed individually.

    >>> subscription = webservice.get(
    ...     subscription_entries[1]["self_link"]
    ... ).jsonBody()
    >>> pprint_entry(subscription)
    bug_link: 'http://.../bugs/1'
    date_created: '2006-10-16T18:31:43.154816+00:00'
    person_link: 'http://.../~stevea'
    resource_type_link: 'http://.../#bug_subscription'
    self_link: 'http://.../bugs/1/+subscription/stevea'
    subscribed_by_link: 'http://.../~janitor'

Subscriptions can also be accessed anonymously.

    >>> subscriptions = anon_webservice.get(
    ...     bug_one_subscriptions_url
    ... ).jsonBody()
    >>> print(subscriptions["entries"][0]["self_link"])
    http://.../bugs/1/+subscription/stevea

We can also create new subscriptions.

    >>> new_subscription = webservice.named_post(
    ...     bug_one["self_link"],
    ...     "subscribe",
    ...     person=webservice.getAbsoluteUrl("/~cprov"),
    ... ).jsonBody()
    >>> pprint_entry(new_subscription)
    bug_link: ...
    self_link: 'http://.../bugs/1/+subscription/cprov'
    ...

An individual can only unsubscribe themselves.  If the person argument is
not provided, the web service uses the calling user.

    >>> print(webservice.named_post(bug_one["self_link"], "unsubscribe"))
    HTTP/1.1 200 Ok...

Using the devel api, an individual can subscribe themself at a given
BugNotificationLevel.

    >>> bug_one_devel = webservice.get(
    ...     "/bugs/1", api_version="devel"
    ... ).jsonBody()
    >>> new_subscription = webservice.named_post(
    ...     bug_one_devel["self_link"],
    ...     "subscribe",
    ...     person=webservice.getAbsoluteUrl("/~salgado"),
    ...     level="Details",
    ...     api_version="devel",
    ... ).jsonBody()
    >>> pprint_entry(new_subscription)
    bug_link: '.../bugs/1'
    bug_notification_level: 'Details'
    date_created: '...'
    person_link: '...'
    resource_type_link: '...'
    self_link: '...'
    subscribed_by_link: '...'

They can also update the subscription's bug_notification_level directly.

    >>> patch = {"bug_notification_level": "Lifecycle"}
    >>> pprint_entry(
    ...     webservice.patch(
    ...         new_subscription["self_link"],
    ...         "application/json",
    ...         json.dumps(patch),
    ...         api_version="devel",
    ...     ).jsonBody()
    ... )
    bug_link: '.../bugs/1'
    bug_notification_level: 'Lifecycle'...

If one person tries to unsubscribe another individual, the web
service will return an unauthorized error.

    >>> print(
    ...     user_webservice.named_post(
    ...         bug_one["self_link"],
    ...         "unsubscribe",
    ...         person=webservice.getAbsoluteUrl("/~mark"),
    ...     )
    ... )
    HTTP/1.1 401 Unauthorized...

An individual can, however, unsubscribe a team to which they belong.

For this example, we need a member of the ubuntu-team group,
any member will do.

    >>> from lp.registry.interfaces.person import IPersonSet

    >>> login(ANONYMOUS)
    >>> ubuntu_team_member = (
    ...     getUtility(IPersonSet).getByName("ubuntu-team").activemembers[0]
    ... )
    >>> logout()

Once we have a member, a web service must be created for that user.
Then, the user can unsubsribe the group from the bug.

    >>> member_webservice = webservice_for_person(
    ...     ubuntu_team_member, permission=OAuthPermission.WRITE_PRIVATE
    ... )

    >>> print(
    ...     member_webservice.named_post(
    ...         bug_one["self_link"],
    ...         "unsubscribe",
    ...         person=webservice.getAbsoluteUrl("/~ubuntu-team"),
    ...     )
    ... )
    HTTP/1.1 200 Ok...

If someone who is not a member tries to unsubscribe the group,
the web service will raise an unauthorized error.  To demonstrate
this, the group must first be re-subscribed.

    >>> print(
    ...     webservice.named_post(
    ...         bug_one["self_link"],
    ...         "subscribe",
    ...         person=webservice.getAbsoluteUrl("/~ubuntu-team"),
    ...     )
    ... )
    HTTP/1.1 200 Ok...

    >>> print(
    ...     user_webservice.named_post(
    ...         bug_one["self_link"],
    ...         "unsubscribe",
    ...         person=webservice.getAbsoluteUrl("/~ubuntu-team"),
    ...     )
    ... )
    HTTP/1.1 401 Unauthorized...

To determine if a user can unsubscribe a person or team,
use the bug subscription's canBeUnsubscribedByUser method.
This method checks that the requesting user can unsubscribe
the person of the subscription.

This example uses a subscription of SteveA.

    >>> print(subscription["person_link"])
    http://.../~stevea

Salgado is the webservice user who performed the original subscription and so
can unsubscribe SteveA.

    >>> print(
    ...     webservice.named_get(
    ...         subscription["self_link"], "canBeUnsubscribedByUser"
    ...     ).jsonBody()
    ... )
    True


Unsubscribing From Duplicates
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If a user is subscribed via a duplicate, the user can unsubscribe from the
main bug and be unsubscribed from the duplicate as well.

bug_six is a duplicate of bug_five.

    >>> bug_five = webservice.get("/bugs/5").jsonBody()
    >>> bug_six["duplicate_of_link"] == bug_five["self_link"]
    True

To demonstrate unsubscribing from duplicates, first subscribe the
web service user himself (Salgado) so he has permission to unsubscribe
himself.

    >>> print(
    ...     webservice.named_post(
    ...         bug_six["self_link"],
    ...         "subscribe",
    ...         person=webservice.getAbsoluteUrl("/~salgado"),
    ...     )
    ... )
    HTTP/1.1 200 Ok...

bug_six now has one subscriber, Salgado.

    >>> bug_six_subscriptions = webservice.get(
    ...     bug_six["subscriptions_collection_link"]
    ... ).jsonBody()
    >>> for entry in bug_six_subscriptions["entries"]:
    ...     print(entry["person_link"])
    ...
    http://.../~salgado

Unsubscribe from bug_five, the primary bug, to unsubscribe from both
it and its duplicate, bug_six.

    >>> print(
    ...     webservice.named_post(
    ...         bug_five["self_link"], "unsubscribeFromDupes"
    ...     )
    ... )
    HTTP/1.1 200 Ok...

Now bug_six has no subscribers.

    >>> bug_six_subscriptions = webservice.get(
    ...     bug_six["subscriptions_collection_link"]
    ... ).jsonBody()
    >>> print(bug_six_subscriptions["total_size"])
    0

Unsubscribing from duplicates is also supported for teams.
To demonstrate, first subscribe Ubuntu Team to bug_six, the duplicate.

    >>> print(
    ...     webservice.named_post(
    ...         bug_six["self_link"],
    ...         "subscribe",
    ...         person=webservice.getAbsoluteUrl("/~ubuntu-team"),
    ...     )
    ... )
    HTTP/1.1 200 Ok...
    >>> bug_six_subscriptions = webservice.get(
    ...     bug_six["subscriptions_collection_link"]
    ... ).jsonBody()
    >>> for entry in bug_six_subscriptions["entries"]:
    ...     print(entry["person_link"])
    ...
    http://.../~ubuntu-team

Now, a team member can unsubscribe from bug_five to be unsubscribed
from both it and the duplicate (bug_six).  Use the previously created
member_webservice, which is for an Ubuntu Team member.

    >>> print(
    ...     member_webservice.named_post(
    ...         bug_five["self_link"],
    ...         "unsubscribeFromDupes",
    ...         person=webservice.getAbsoluteUrl("/~ubuntu-team"),
    ...     )
    ... )
    HTTP/1.1 200 Ok...

Now again, bug_six has no subscribers.

    >>> bug_six_subscriptions = webservice.get(
    ...     bug_six["subscriptions_collection_link"]
    ... ).jsonBody()
    >>> print(bug_six_subscriptions["total_size"])
    0


Bug Watches
-----------

Bugs can have bug watches associated with them. Each bugwatch can also
be optionally associated with one of the bugtasks in a bug, in which
case aspects of the bugtask (like status) are slaved to the remote bug
report described by the bugwatch.

    >>> bug_one_bug_watches = sorted(
    ...     webservice.get(bug_one["bug_watches_collection_link"]).jsonBody()[
    ...         "entries"
    ...     ],
    ...     key=itemgetter("self_link"),
    ... )
    >>> len(bug_one_bug_watches)
    4

    >>> [bug_watch_2000] = [
    ...     bug_watch
    ...     for bug_watch in bug_one_bug_watches
    ...     if bug_watch["remote_bug"] == "2000"
    ... ]

    >>> pprint_entry(bug_watch_2000)
    bug_link: 'http://.../bugs/1'
    bug_tasks_collection_link: 'http://.../bugs/1/+watch/2/bug_tasks'
    bug_tracker_link: 'http://.../bugs/bugtrackers/mozilla.org'
    date_created: '2004-10-04T01:00:00+00:00'
    date_last_changed: '2004-10-04T01:00:00+00:00'
    date_last_checked: '2004-10-04T01:00:00+00:00'
    date_next_checked: None
    last_error_type: None
    owner_link: 'http://.../~mark'
    remote_bug: '2000'
    remote_importance: ''
    remote_status: ''
    resource_type_link: 'http://.../#bug_watch'
    self_link: 'http://.../bugs/1/+watch/2'
    title: 'The Mozilla.org Bug Tracker #2000'
    url: 'https://bugzilla.mozilla.org/show_bug.cgi?id=2000'
    web_link: 'http://bugs.../bugs/1/+watch/2'

    >>> bug_watch = webservice.get(bug_watch_2000["self_link"]).jsonBody()
    >>> bug_watch == bug_watch_2000
    True

The collection of bug watches is not exposed as a resource:

    >>> webservice.get("/bug_watches").status
    404

We can modify the remote bug.

    >>> print(bug_watch["remote_bug"])
    2000

    >>> patch = {"remote_bug": "1234"}
    >>> response = webservice.patch(
    ...     bug_watch_2000["self_link"], "application/json", json.dumps(patch)
    ... )

    >>> bug_watch = webservice.get(bug_watch_2000["self_link"]).jsonBody()
    >>> print(bug_watch["remote_bug"])
    1234

But we can't change other things, like the URL.

    >>> patch = {"url": "http://www.example.com/"}
    >>> response = webservice.patch(
    ...     bug_watch_2000["self_link"], "application/json", json.dumps(patch)
    ... )
    >>> print(response)
    HTTP/1.1 400 Bad Request...
    Content-Length: 47
    ...
    <BLANKLINE>
    url: You tried to modify a read-only attribute.

We can use the factory function `addWatch` to create a new bug watch
associated with a bug.

    >>> response = webservice.named_post(
    ...     bug_one["self_link"],
    ...     "addWatch",
    ...     bug_tracker=webservice.getAbsoluteUrl(
    ...         "/bugs/bugtrackers/mozilla.org"
    ...     ),
    ...     remote_bug="9876",
    ... )
    >>> print(response)
    HTTP/1.1 201 Created...
    Content-Length: 0
    ...
    Location: http://.../bugs/1/+watch/...
    ...

Following the redirect, we can see the new bug watch:

    >>> new_bug_watch_path = response.getHeader("Location")
    >>> new_bug_watch = webservice.get(new_bug_watch_path).jsonBody()
    >>> pprint_entry(new_bug_watch)
    bug_link: 'http://.../bugs/1'
    bug_tasks_collection_link: 'http://.../bugs/1/+watch/.../bug_tasks'
    bug_tracker_link: 'http://.../bugs/bugtrackers/mozilla.org'
    date_created: '...'
    date_last_changed: None
    date_last_checked: None
    date_next_checked: None
    last_error_type: None
    owner_link: 'http://.../~salgado'
    remote_bug: '9876'
    remote_importance: None
    remote_status: None
    resource_type_link: 'http://.../#bug_watch'
    self_link: 'http://.../bugs/1/+watch/...'
    title: 'The Mozilla.org Bug Tracker #9876'
    url: 'https://bugzilla.mozilla.org/show_bug.cgi?id=9876'
    web_link: 'http://bugs.../bugs/1/+watch/...'

Bug Trackers
------------

    >>> bug_tracker = webservice.get(bug_watch["bug_tracker_link"]).jsonBody()

    >>> pprint_entry(bug_tracker)
    active: True
    base_url: 'https://bugzilla.mozilla.org/'
    base_url_aliases: []
    bug_tracker_type: 'Bugzilla'
    contact_details: 'Carrier pigeon only'
    has_lp_plugin: None
    name: 'mozilla.org'
    registrant_link: 'http://.../~name12'
    resource_type_link: 'http://.../#bug_tracker'
    self_link: 'http://.../bugs/bugtrackers/mozilla.org'
    summary: 'The Mozilla.org bug tracker is the grand-daddy of bugzillas...'
    title: 'The Mozilla.org Bug Tracker'
    watches_collection_link: 'http://.../bugs/bugtrackers/mozilla.org/watches'
    web_link: 'http://bugs.../bugs/bugtrackers/mozilla.org'

We can change various aspects of bug trackers.

    >>> patch = {
    ...     "name": "bob",
    ...     "title": "Bob's Tracker",
    ...     "summary": "Where Bob files his bugs.",
    ...     "base_url": "http://bugs.example.com/",
    ...     "base_url_aliases": [
    ...         "http://bugs.example.com/bugs/",
    ...         "http://www.example.com/bugtracker/",
    ...     ],
    ...     "contact_details": "bob@example.com",
    ... }
    >>> response = webservice.patch(
    ...     bug_tracker["self_link"], "application/json", json.dumps(patch)
    ... )
    >>> print(response)
    HTTP/1.1 301 Moved Permanently...
    Content-Length: 0
    ...
    Location: http://.../bugs/bugtrackers/bob
    ...

Note the 301 response above. We changed the name, so the API URL at which
the bug tracker can be found has changed.

Now notice that bug trackers (and bugs too) that are not found generate
a 404 error, but do not generate an OOPS.

    >>> print(webservice.get(bug_tracker["self_link"]))
    HTTP/1.1 404 Not Found...
    Content-Length: ...
    ...
    <BLANKLINE>
    Object: <BugTrackerSet object>, name: 'mozilla.org'

Naturally, if we follow the Location: header then we'll get the
renamed bug tracker.

    >>> bug_tracker_path = response.getHeader("Location")
    >>> bug_tracker = webservice.get(bug_tracker_path).jsonBody()
    >>> pprint_entry(bug_tracker)
    active: True
    base_url: 'http://bugs.example.com/'
    base_url_aliases:
      ['http://bugs.example.com/bugs/', 'http://www.example.com/bugtracker/']
    bug_tracker_type: 'Bugzilla'
    contact_details: 'bob@example.com'
    has_lp_plugin: None
    name: 'bob'
    registrant_link: 'http://.../~name12'
    resource_type_link: 'http://.../#bug_tracker'
    self_link: 'http://.../bugs/bugtrackers/bob'
    summary: 'Where Bob files his bugs.'
    title: "Bob's Tracker"
    watches_collection_link: 'http://.../bugs/bugtrackers/bob/watches'
    web_link: 'http://bugs.../bugs/bugtrackers/bob'

Non-admins can't disable a bugtracker through the API.

    >>> print(
    ...     public_webservice.patch(
    ...         bug_tracker_path,
    ...         "application/json",
    ...         json.dumps(dict(active=False)),
    ...     )
    ... )
    HTTP/1.1 401 Unauthorized
    ...
    (<...BugTracker object>, 'active', 'launchpad.Admin')

Admins can, however.

    >>> bug_tracker = webservice.patch(
    ...     bug_tracker_path,
    ...     "application/json",
    ...     json.dumps(dict(active=False)),
    ... ).jsonBody()
    >>> pprint_entry(bug_tracker)
    active: False...


Bug attachments
---------------

Bug 1 has no attachments:

    >>> attachments = webservice.get(
    ...     bug_one["attachments_collection_link"]
    ... ).jsonBody()
    >>> pprint_collection(attachments)
    resource_type_link: 'http://.../#bug_attachment-page-resource'
    start: 0
    total_size: 0
    ---

An attachment can be added to the bug:

    >>> import io
    >>> response = webservice.named_post(
    ...     bug_one["self_link"],
    ...     "addAttachment",
    ...     data=io.BytesIO(b"12345"),
    ...     filename="numbers.txt",
    ...     url=None,
    ...     content_type="foo/bar",
    ...     comment="The numbers you asked for.",
    ... )
    >>> print(response)
    HTTP/1.1 201 Created...
    Content-Length: 0
    ...
    Location: http://.../bugs/1/+attachment/...
    ...

Now, bug 1 has one attachment:

    >>> attachments = webservice.get(
    ...     bug_one["attachments_collection_link"]
    ... ).jsonBody()
    >>> pprint_collection(attachments)
    resource_type_link: 'http://.../#bug_attachment-page-resource'
    start: 0
    total_size: 1
    ---
    bug_link: 'http://.../bugs/1'
    data_link: 'http://.../bugs/1/+attachment/.../data'
    message_link: 'http://.../firefox/+bug/1/comments/2'
    resource_type_link: 'http://.../#bug_attachment'
    self_link: 'http://.../bugs/1/+attachment/...'
    title: 'numbers.txt'
    type: 'Unspecified'
    url: None
    web_link: 'http://bugs.../bugs/1/+attachment/...'
    ---

The attachment can be fetched directly:

    >>> [attachment] = attachments["entries"]
    >>> pprint_entry(webservice.get(attachment["self_link"]).jsonBody())
    bug_link: 'http://.../bugs/1'
    data_link: 'http://.../bugs/1/+attachment/.../data'
    message_link: 'http://.../firefox/+bug/1/comments/2'
    resource_type_link: 'http://.../#bug_attachment'
    self_link: 'http://.../bugs/1/+attachment/...'
    title: 'numbers.txt'
    type: 'Unspecified'
    url: None
    web_link: 'http://bugs.../bugs/1/+attachment/...'

Fetching the data actually yields a redirect to the Librarian, which
we must follow to download the data.

    >>> data_response = webservice.get(attachment["data_link"])
    >>> print(data_response)
    HTTP/1.1 303 See Other...
    Content-Length: 0
    ...
    Content-Type: text/plain
    Location: http://.../numbers.txt
    ...

    >>> from urllib.request import urlopen

    >>> data = None
    >>> conn = urlopen(data_response.getHeader("Location"))
    >>> try:
    ...     data = conn.read()
    ... finally:
    ...     conn.close()
    ...

    >>> conn.headers["Content-Type"]
    'foo/bar'

    >>> conn.headers["Content-Length"]
    '5'

    >>> six.ensure_str(data)
    '12345'

We can see that a message was created and linked to our
attachment. This is where our comment is recorded.

    >>> message = webservice.get(attachment["message_link"]).jsonBody()
    >>> pprint_entry(message)
    bug_attachments_collection_link:
      'http://.../firefox/+bug/1/comments/2/bug_attachments'
    content: 'The numbers you asked for.'
    date_created: '...'
    date_deleted: None
    date_last_edited: None
    owner_link: 'http://.../~salgado'
    parent_link: None
    resource_type_link: 'http://.../#message'
    self_link: 'http://.../firefox/+bug/1/comments/2'
    subject: 'Re: Firefox does not support SVG'
    web_link: 'http://bugs.../firefox/+bug/1/comments/2'

The message also links back to the attachments that were uploaded at
the same time.

    >>> attachments = webservice.get(
    ...     message["bug_attachments_collection_link"]
    ... ).jsonBody()
    >>> pprint_collection(attachments)
    resource_type_link: 'http://.../#bug_attachment-page-resource'
    start: 0
    total_size: 1
    ...
    ---

Once an attachment is uploaded, it is not possible to change it.

    >>> response = webservice.put(
    ...     attachment["data_link"], "text/text", "abcdefg"
    ... )
    >>> print(response)
    HTTP/1.1 405 Method Not Allowed
    ...

    >>> data_response = webservice.get(attachment["data_link"])
    >>> data = None
    >>> conn = urlopen(data_response.getHeader("Location"))
    >>> try:
    ...     data = conn.read()
    ... finally:
    ...     conn.close()
    ...
    >>> six.ensure_str(data)
    '12345'

But we can remove the attachment altogether.

    >>> response = webservice.named_post(
    ...     attachment["self_link"], "removeFromBug"
    ... )
    >>> print(response)
    HTTP/1.1 200 Ok
    ...

    >>> attachments = webservice.get(
    ...     bug_one["attachments_collection_link"]
    ... ).jsonBody()
    >>> pprint_collection(attachments)
    resource_type_link:
      'http://api.launchpad.test/beta/#bug_attachment-page-resource'
    start: 0
    total_size: 0
    ---


Searching for bugs
------------------

Bug targets expose the searchTasks method, which provides a search interface
for bug tasks, similar to the advanced search form on the web interface.

Calling searchTasks with no arguments just returns a collection of all the
bug tasks for the target.

    >>> pprint_collection(
    ...     webservice.named_get("/firefox", "searchTasks").jsonBody()
    ... )
    start: 0
    total_size: 5
    ---
    ...
    target_link: 'http://api.launchpad.test/beta/firefox'
    ...
    target_link: 'http://api.launchpad.test/beta/firefox'
    ...
    target_link: 'http://api.launchpad.test/beta/firefox'
    ...
    target_link: 'http://api.launchpad.test/beta/firefox'
    ...
    target_link: 'http://api.launchpad.test/beta/firefox'
    ...

Some parameters accept lists of values, just like when searching from
the web interface. The importance and status parameters, for example,
accept many values and return only tasks with these values.

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/firefox", "searchTasks", importance=["Critical", "Low"]
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 2
    ---
    ...
    importance: 'Critical'
    ...
    self_link: 'http://api.launchpad.test/beta/firefox/+bug/5'
    ...
    ---
    ...
    importance: 'Low'
    ...
    self_link: 'http://api.launchpad.test/beta/firefox/+bug/1'
    ...

The tags parameter also accepts a list of values. By default, it
searches for bugs with any of the given tags.

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/ubuntu", "searchTasks", tags=["crash", "dataloss"]
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 3
    ---
    ...
    bug_link: 'http://.../bugs/9'
    ...
    self_link: 'http://.../ubuntu/+source/thunderbird/+bug/9'
    ...
    ---
    ...
    bug_link: 'http://.../bugs/10'
    ...
    self_link: 'http://.../ubuntu/+source/linux-source-2.6.15/+bug/10'
    ...
    ---
    ...
    bug_link: 'http://.../bugs/2'
    ...
    self_link: 'http://.../ubuntu/+bug/2'
    ...

It can be used for searching for bugs with all of the given tags by
setting the tags_combinator parameter to 'All'.

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/ubuntu",
    ...         "searchTasks",
    ...         tags=["crash", "dataloss"],
    ...         tags_combinator="All",
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 0
    ---

It can also be used to find bugs modified since a certain date.

    >>> from datetime import timedelta
    >>> from lp.testing.sampledata import ADMIN_EMAIL
    >>> login(ADMIN_EMAIL)
    >>> target = factory.makeProduct()
    >>> target_name = target.name
    >>> bug = factory.makeBug(target=target)
    >>> bug = removeSecurityProxy(bug)
    >>> date = bug.date_last_updated - timedelta(days=6)
    >>> logout()

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/%s" % target_name, "searchTasks", modified_since="%s" % date
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 1
    ...
    ---

It can also be used to find bug tasks created since a certain date.

    >>> from lp.bugs.interfaces.bugtarget import IBugTarget
    >>> login(ADMIN_EMAIL)
    >>> target = IBugTarget(factory.makeProduct())
    >>> target_name = target.name
    >>> task = factory.makeBugTask(target=target)
    >>> date = task.datecreated - timedelta(days=8)
    >>> logout()

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/%s" % target_name, "searchTasks", created_since="%s" % date
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 1
    ...
    ---

Or for finding bug tasks created before a certain date.

    >>> before_date = task.datecreated + timedelta(days=8)
    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/%s" % target_name,
    ...         "searchTasks",
    ...         created_before="%s" % before_date,
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 1
    ...
    ---

It is possible to search for bugs targeted to a milestone within a
project group.

    >>> from lp.registry.interfaces.milestone import IMilestoneSet
    >>> from lp.registry.interfaces.product import IProductSet
    >>> login("foo.bar@canonical.com")
    >>> product_set = getUtility(IProductSet)
    >>> milestone_set = getUtility(IMilestoneSet)
    >>> firefox = product_set.getByName("firefox")
    >>> firefox_1_0 = milestone_set.getByNameAndProduct(
    ...     product=firefox, name="1.0"
    ... )
    >>> bug = factory.makeBug(target=firefox)
    >>> bug.bugtasks[0].milestone = firefox_1_0
    >>> logout()

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         "/mozilla",
    ...         "searchTasks",
    ...         milestone=webservice.getAbsoluteUrl(
    ...             "/mozilla/+milestone/1.0"
    ...         ),
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 1
    ...
    ---

The same search can be performed directly on the milestone too.

    >>> pprint_collection(
    ...     webservice.named_get(
    ...         webservice.getAbsoluteUrl("/mozilla/+milestone/1.0"),
    ...         "searchTasks",
    ...     ).jsonBody()
    ... )
    start: 0
    total_size: 1
    ...
    ---

Search results can be ordered using the same string values used by
the advanced search interface.

    >>> ordered_bugtasks = webservice.named_get(
    ...     "/ubuntu", "searchTasks", order_by="-datecreated"
    ... ).jsonBody()["entries"]
    >>> dates = [task["date_created"] for task in ordered_bugtasks]
    >>> dates == sorted(dates, reverse=True)
    True


User related bug tasks
~~~~~~~~~~~~~~~~~~~~~~

Calling searchTasks() on a Person object returns a collection of tasks
related to this person.

First create some sample data

    >>> login("foo.bar@canonical.com")
    >>> testuser1 = factory.makePerson(name="testuser1")
    >>> testuser2 = factory.makePerson(name="testuser2")
    >>> testuser3 = factory.makePerson(name="testuser3")
    >>> testbug1 = factory.makeBug(owner=testuser1)
    >>> testbug2 = factory.makeBug(owner=testuser1)
    >>> subscription = testbug2.subscribe(testuser2, testuser2)
    >>> logout()

There are two tasks related to `testuser1`, the initial tasks of both
bugs:

    >>> related = webservice.named_get(
    ...     "/~testuser1", "searchTasks"
    ... ).jsonBody()
    >>> pprint_collection(related)
    start: 0
    total_size: 2
    ---
    ...
    owner_link: 'http://api.launchpad.test/beta/~testuser1'
    ...
    ---
    ...
    owner_link: 'http://api.launchpad.test/beta/~testuser1'
    ...

`testuser2` is subscribed to `testbug2`, so this bug is related to this
user:

    >>> related = webservice.named_get(
    ...     "/~testuser2", "searchTasks"
    ... ).jsonBody()
    >>> len(related["entries"]) == 1
    True
    >>> int(related["entries"][0]["bug_link"].split("/")[-1]) == testbug2.id
    True

`testuser3` is not active, so the collection of related tasks to them is
empty:

    >>> related = webservice.named_get(
    ...     "/~testuser3", "searchTasks"
    ... ).jsonBody()
    >>> pprint_collection(related)
    start: 0
    total_size: 0
    ---

You are not allowed to overwrite all user related parameters in the same
query, because this bug will not be related to the person anymore. In this
case a `400 Bad Request`-Error will be returned.

    >>> name12 = webservice.get("/~name12").jsonBody()
    >>> print(
    ...     webservice.named_get(
    ...         "/~name16",
    ...         "searchTasks",
    ...         assignee=name12["self_link"],
    ...         owner=name12["self_link"],
    ...         bug_subscriber=name12["self_link"],
    ...         bug_commenter=name12["self_link"],
    ...         structural_subscriber=name12["self_link"],
    ...     )
    ... )
    HTTP/1.1 400 Bad Request...


Searching for bugs that are linked to branches
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

We can search for bugs that are linked to branches...

    >>> bugtasks = webservice.named_get(
    ...     "/firefox",
    ...     "searchTasks",
    ...     linked_branches="Show only Bugs with linked Branches",
    ... )
    >>> bugtasks.jsonBody()["total_size"]
    2

...and we can search for bugs that are not linked to branches.

    >>> bugtasks = webservice.named_get(
    ...     "/firefox",
    ...     "searchTasks",
    ...     linked_branches="Show only Bugs without linked Branches",
    ... )
    >>> bugtasks.jsonBody()["total_size"]
    4


Affected users
--------------

It is possible to mark a bug as affecting the user using the web service.

    >>> print(
    ...     webservice.named_post(
    ...         bug_one["self_link"], "isUserAffected"
    ...     ).jsonBody()
    ... )
    None
    >>> webservice.named_post(
    ...     bug_one["self_link"], "markUserAffected", affected=True
    ... ).jsonBody()
    >>> webservice.named_post(
    ...     bug_one["self_link"], "isUserAffected"
    ... ).jsonBody()
    True
    >>> pprint_collection(
    ...     webservice.get(
    ...         webservice.get(bug_one["self_link"]).jsonBody()[
    ...             "users_affected_collection_link"
    ...         ]
    ...     ).jsonBody()
    ... )
    resource_type_link: 'http://api.launchpad.test/beta/#person-page-resource'
    start: 0
    total_size: 1
    ...
    self_link: 'http://api.launchpad.test/beta/~salgado'
    ...

    >>> webservice.named_post(
    ...     bug_one["self_link"], "markUserAffected", affected=False
    ... ).jsonBody()
    >>> webservice.named_post(
    ...     bug_one["self_link"], "isUserAffected"
    ... ).jsonBody()
    False


CVEs
----

CVEs and how they relate to Launchpad bugs can be accessed using the API.

The collection of all CVEs is available at the top level.

    >>> cves = webservice.get("/bugs/cve").jsonBody()
    >>> pprint_collection(cves)
    next_collection_link: 'http://.../bugs/cve?ws.size=5&memo=5&ws.start=5'
    resource_type_link: 'http://.../#cves'
    start: 0
    total_size: 10
    ---
    bugs_collection_link: 'http://.../bugs/cve/2005-2737/bugs'
    date_created: '2005-09-13T14:05:17.043865+00:00'
    date_modified: '2005-09-13T14:05:17.043865+00:00'
    description: 'Cross-site scripting (XSS) vulnerability...'
    display_name: 'CVE-2005-2737'
    resource_type_link: 'http://.../#cve'
    self_link: 'http://.../bugs/cve/2005-2737'
    sequence: '2005-2737'
    status: 'Candidate'
    title: 'CVE-2005-2737 (Candidate)'
    url: 'https://cve.org/CVERecord?id=CVE-2005-2737'
    web_link: 'http://bugs.launchpad.test/bugs/cve/2005-2737'
    ---
    ...
    self_link: 'http://.../bugs/cve/2005-2736'
    ...
    ---
    ...
    self_link: 'http://.../bugs/cve/2005-2735'
    ...
    ---
    ...
    self_link: 'http://.../bugs/cve/2005-2734'
    ...
    ---
    ...
    self_link: 'http://.../bugs/cve/2005-2733'
    ...

And for every bug we can look at the CVEs linked to it.

    >>> bug_one_cves_url = bug_one["cves_collection_link"]
    >>> bug_one_cves = webservice.get(bug_one_cves_url).jsonBody()
    >>> pprint_collection(bug_one_cves)
    resource_type_link: 'http://.../#cve-page-resource'
    start: 0
    total_size: 1
    ---
    bugs_collection_link: 'http://.../bugs/cve/1999-8979/bugs'
    date_created: '2005-09-07T19:00:32.944561+00:00'
    date_modified: '2005-09-13T14:00:03.508959+00:00'
    description: 'Firefox crashes all the time'
    display_name: 'CVE-1999-8979'
    resource_type_link: 'http://.../#cve'
    self_link: 'http://.../bugs/cve/1999-8979'
    sequence: '1999-8979'
    status: 'Entry'
    title: 'CVE-1999-8979 (Entry)'
    url: 'https://cve.org/CVERecord?id=CVE-1999-8979'
    web_link: 'http://bugs.launchpad.test/bugs/cve/1999-8979'
    ---

For every CVE we can also look at the bugs linked to it.

    >>> cve_entry = bug_one_cves["entries"][0]
    >>> bug_links = webservice.get(
    ...     cve_entry["bugs_collection_link"]
    ... ).jsonBody()
    >>> for bug in bug_links["entries"]:
    ...     print(bug["self_link"])
    ...
    http://.../bugs/1

Unlink CVEs from that bug.

    >>> print(
    ...     webservice.named_post(
    ...         bug_one["self_link"],
    ...         "unlinkCVE",
    ...         cve="http://api.launchpad.test/beta/bugs/cve/1999-8979",
    ...     )
    ... )
    HTTP/1.1 200 Ok...
    >>> pprint_collection(webservice.get(bug_one_cves_url).jsonBody())
    resource_type_link: 'http://.../#cve-page-resource'
    start: 0
    total_size: 0
    ---

And link new CVEs to the bug.

    >>> print(
    ...     webservice.named_post(
    ...         bug_one["self_link"],
    ...         "linkCVE",
    ...         cve="http://api.launchpad.test/beta/bugs/cve/2005-2733",
    ...     )
    ... )
    HTTP/1.1 200 Ok...
    >>> pprint_collection(webservice.get(bug_one_cves_url).jsonBody())
    resource_type_link: 'http://.../#cve-page-resource'
    start: 0
    total_size: 1
    ---
    ...
    self_link: 'http://.../bugs/cve/2005-2733'
    ...

Add a new task to the bug.

    >>> bugtasks_url = bug_one["bug_tasks_collection_link"]
    >>> pprint_collection(webservice.get(bugtasks_url).jsonBody())
    resource_type_link: 'http://.../#bug_task-page-resource'
    start: 0
    total_size: 3
    ...

    >>> redfish = webservice.get("/redfish").jsonBody()
    >>> print(
    ...     webservice.named_post(
    ...         bug_one["self_link"], "addTask", target=redfish["self_link"]
    ...     )
    ... )
    HTTP/1.1 201 Created...

    >>> bugtasks_url = bug_one["bug_tasks_collection_link"]
    >>> pprint_collection(webservice.get(bugtasks_url).jsonBody())
    resource_type_link: 'http://.../#bug_task-page-resource'
    start: 0
    total_size: 4
    ...


Bug branches
------------

For every bug we can look at the branches linked to it.

    >>> bug_four = webservice.get("/bugs/4").jsonBody()
    >>> bug_four_branches_url = bug_four["linked_branches_collection_link"]
    >>> bug_four_branches = webservice.get(bug_four_branches_url).jsonBody()
    >>> pprint_collection(bug_four_branches)
    resource_type_link: 'http://.../#bug_branch-page-resource'
    start: 0
    total_size: 2
    ---
    branch_link: 'http://.../~mark/firefox/release-0.9.2'
    bug_link: 'http://.../bugs/4'
    resource_type_link: 'http://.../#bug_branch'
    self_link: 'http://.../~mark/firefox/release-0.9.2/+bug/4'
    ---
    branch_link: 'http://.../~name12/firefox/main'
    bug_link: 'http://.../bugs/4'
    resource_type_link: 'http://.../beta/#bug_branch'
    self_link: 'http://.../~name12/firefox/main/+bug/4'
    ---

For every branch we can also look at the bugs linked to it.

    >>> branch_entry = bug_four_branches["entries"][0]
    >>> bug_link = webservice.get(branch_entry["bug_link"]).jsonBody()
    >>> print(bug_link["self_link"])
    http://.../bugs/4

Bug expiration
--------------

In addition to can_expire bugs have an isExpirable method to which a custom
time period, days_old, can be passed.  This is then used with
findExpirableBugTasks.  This allows projects to create their own janitor using
a different period for bug expiration.

Check to ensure that isExpirable() works without days_old.

    >>> bug_four = webservice.get("/bugs/4").jsonBody()
    >>> print(
    ...     webservice.named_get(
    ...         bug_four["self_link"], "isExpirable"
    ...     ).jsonBody()
    ... )
    False

Pass isExpirable() an integer for days_old.

    >>> bug_four = webservice.get("/bugs/4").jsonBody()
    >>> print(
    ...     webservice.named_get(
    ...         bug_four["self_link"], "isExpirable", days_old="14"
    ...     ).jsonBody()
    ... )
    False

Pass isExpirable() a string for days_old.

    >>> bug_four = webservice.get("/bugs/4").jsonBody()
    >>> print(
    ...     webservice.named_get(
    ...         bug_four["self_link"], "isExpirable", days_old="sixty"
    ...     )
    ... )
    HTTP/1.1 400 Bad Request
    ...
    days_old: got '...', expected int: ...'sixty'

Can expire
----------

can_expire is not exported in the development version of the API.

    >>> bug_four = webservice.get("/bugs/4", api_version="devel").jsonBody()
    >>> bug_four[can_expire]
    Traceback (most recent call last):
    ...
    NameError: name 'can_expire' is not defined


Bug activity
------------

Each bug has a collection of activities that have taken place with it.

    >>> from lazr.restful.testing.webservice import (
    ...     pprint_collection,
    ...     pprint_entry,
    ... )
    >>> activity = anon_webservice.get(
    ...     bug_one["activity_collection_link"]
    ... ).jsonBody()
    >>> pprint_collection(activity)
    next_collection_link:
      'http://.../bugs/1/activity?ws.size=5&memo=5&ws.start=5'
    resource_type_link: 'http://.../#bug_activity-page-resource'
    start: 0
    total_size: 24
    ...
    message: "Decided problem wasn't silly after all"
    ...

    >>> bug_nine_activity = webservice.get("/bugs/9/activity").jsonBody()
    >>> pprint_entry(bug_nine_activity["entries"][1])
    bug_link: 'http://.../bugs/9'
    datechanged: '2006-02-23T16:42:40.288553+00:00'
    message: None
    newvalue: 'Confirmed'
    oldvalue: 'Unconfirmed'
    person_link: 'http://.../~name12'
    resource_type_link: 'http://.../#bug_activity'
    self_link: 'http://.../bugs/9/activity'
    whatchanged: 'thunderbird: status'
