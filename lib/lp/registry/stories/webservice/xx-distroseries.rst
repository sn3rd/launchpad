Distribution Series
===================

We can get a distroseries object via a distribution object in several ways:

    >>> distros = webservice.get("/distros").jsonBody()
    >>> ubuntu = distros["entries"][0]
    >>> print(ubuntu["self_link"])
    http://.../ubuntu

Via all the available series:

    >>> all_series = webservice.get(
    ...     ubuntu["series_collection_link"]
    ... ).jsonBody()
    >>> for entry in all_series["entries"]:
    ...     print(entry["self_link"])
    ...
    http://.../ubuntu/breezy-autotest
    http://.../ubuntu/grumpy
    http://.../ubuntu/hoary
    http://.../ubuntu/warty

The series are available to the anonymous API user too:

    >>> all_series = anon_webservice.get(
    ...     ubuntu["series_collection_link"]
    ... ).jsonBody()
    >>> for entry in all_series["entries"]:
    ...     print(entry["self_link"])
    ...
    http://.../ubuntu/breezy-autotest
    http://.../ubuntu/grumpy
    http://.../ubuntu/hoary
    http://.../ubuntu/warty

Via the current series:

    >>> current_series = webservice.get(
    ...     ubuntu["current_series_link"]
    ... ).jsonBody()
    >>> print(current_series["self_link"])
    http://.../ubuntu/hoary

Via the collection of development series:

    >>> dev_series = webservice.named_get(
    ...     ubuntu["self_link"], "getDevelopmentSeries"
    ... ).jsonBody()
    >>> for entry in sorted(dev_series["entries"]):
    ...     print(entry["self_link"])
    ...
    http://.../ubuntu/hoary

And via a direct query of a named series:

    >>> series = webservice.named_get(
    ...     ubuntu["self_link"], "getSeries", name_or_version="hoary"
    ... ).jsonBody()
    >>> print(series["self_link"])
    http://.../ubuntu/hoary

For distroseries we publish a subset of its attributes.

    >>> from lazr.restful.testing.webservice import pprint_entry
    >>> pprint_entry(current_series)
    active: True
    active_milestones_collection_link:
        'http://.../ubuntu/hoary/active_milestones'
    advertise_by_hash: False
    all_milestones_collection_link: 'http://.../ubuntu/hoary/all_milestones'
    architectures_collection_link: 'http://.../ubuntu/hoary/architectures'
    backports_not_automatic: False
    bug_reported_acknowledgement: None
    bug_reporting_guidelines: None
    changeslist: 'hoary-changes@ubuntu.com'
    component_names: ['main', 'restricted']
    content_templates: None
    date_created: '2006-10-16T18:31:43.483559+00:00'
    datereleased: None
    description: 'Hoary is the ...
    displayname: 'Hoary'
    distribution_link: 'http://.../ubuntu'
    driver_link: None
    drivers_collection_link: 'http://.../ubuntu/hoary/drivers'
    fullseriesname: 'Ubuntu Hoary'
    include_long_descriptions: True
    index_compressors: ['gzip', 'bzip2']
    language_pack_full_export_requested: False
    main_archive_link: 'http://.../ubuntu/+archive/primary'
    name: 'hoary'
    nominatedarchindep_link: 'http://.../ubuntu/hoary/i386'
    official_bug_tags: []
    owner_link: 'http://.../~ubuntu-team'
    parent_series_link: 'http://.../ubuntu/warty'
    proposed_not_automatic: False
    publish_by_hash: False
    publish_i18n_index: True
    registrant_link: 'http://.../~mark'
    resource_type_link: ...
    self_link: 'http://.../ubuntu/hoary'
    status: 'Active Development'
    suite_names: ['Release', 'Security', 'Updates', 'Proposed', 'Backports']
    summary: 'Hoary is the ...
    supported: False
    title: 'The Hoary Hedgehog Release'
    valid_until_config: {}
    version: '5.04'
    web_link: 'http://launchpad.../ubuntu/hoary'


Getting the previous series
---------------------------

In the beta version of the API the previous series is obtained via
parent_series_link:

    >>> current_series_beta = webservice.get(
    ...     "/ubuntu/hoary", api_version="beta"
    ... ).jsonBody()
    >>> print(current_series_beta["parent_series_link"])
    http://.../ubuntu/warty

In the 1.0 version of the API the previous series is obtained via
parent_series_link:

    >>> current_series_1_0 = webservice.get(
    ...     "/ubuntu/hoary", api_version="1.0"
    ... ).jsonBody()
    >>> print(current_series_1_0["parent_series_link"])
    http://.../ubuntu/warty

In the devel version of the API the previous series is obtained via
parent_series_link:

    >>> current_series_devel = webservice.get(
    ...     "/ubuntu/hoary", api_version="devel"
    ... ).jsonBody()
    >>> print(current_series_devel["previous_series_link"])
    http://.../ubuntu/warty


Creating a milestone on the distroseries
----------------------------------------

    >>> response = webservice.named_post(
    ...     current_series["self_link"],
    ...     "newMilestone",
    ...     {},
    ...     name="alpha1",
    ...     code_name="wombat",
    ...     date_targeted="2009-09-06",
    ...     summary="summary.",
    ... )
    >>> print(response)
    HTTP/1.1 201 Created
    ...
    Location: http://.../ubuntu/+milestone/alpha1
    ...
