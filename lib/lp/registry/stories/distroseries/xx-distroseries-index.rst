Distribution series main page
=============================

The distroseries page presents a "Help translate" link through the
"Get Involved" summary component.

    >>> user_browser.open("http://launchpad.test/ubuntu/hoary")
    >>> user_browser.getLink("Help translate").click()
    >>> print(user_browser.title)
    Hoary (5.04) : Translations : Ubuntu


Registration and summary
------------------------

The distroseries page displays the registrant and registration date at
the top of the page summary, together with the series description.

    >>> anon_browser.open("http://launchpad.test/ubuntu/warty")
    >>> main_content = extract_text(find_main_content(anon_browser.contents))
    >>> "Registered by" in main_content
    True
    >>> "Ubuntu Team" in main_content
    True
    >>> "2006-10-16" in main_content
    True

    >>> print(anon_browser.getLink("Ubuntu Team").url)
    http://launchpad.test/~ubuntu-team


Series details
--------------

The summary section lists the distribution, version, drivers, release
manager, and derivation information for the series.

    >>> def summary_contains(browser, *texts):
    ...     content = extract_text(find_main_content(browser.contents))
    ...     return all(text in content for text in texts)
    ...

    >>> summary_contains(
    ...     anon_browser,
    ...     "Distribution",
    ...     "Ubuntu",
    ...     "Series",
    ...     "Warty",
    ...     "(4.10)",
    ...     "Drivers",
    ...     "Ubuntu Team",
    ...     "Release Manager",
    ...     "Derives from",
    ...     "Not derived from any series",
    ...     "Derived series",
    ...     "No derived series",
    ... )
    True

The summary also shows package and bug counts.

    >>> summary_contains(
    ...     anon_browser,
    ...     "Source packages",
    ...     "Binary packages",
    ...     "Open bugs",
    ...     "Open critical bugs",
    ... )
    True

On series that have no source or binary packages, the counts simply
report zero.

    >>> anon_browser.open("http://launchpad.test/debian/sarge")
    >>> summary_contains(
    ...     anon_browser,
    ...     "Distribution",
    ...     "Debian",
    ...     "Series",
    ...     "Sarge",
    ...     "(3.1)",
    ...     "Drivers",
    ...     "Jeff Waugh",
    ...     "Mark Shuttleworth",
    ...     "Release Manager",
    ...     "Derives from",
    ...     "Not derived from any series",
    ... )
    True

The series' derivation parents are shown when derivation is enabled, as
are the series derived from this series.

    >>> from lp.registry.interfaces.distribution import IDistributionSet
    >>> from lp.testing import celebrity_logged_in
    >>> from zope.component import getUtility

    >>> with celebrity_logged_in("admin"):
    ...     debian = getUtility(IDistributionSet).getByName("debian")
    ...     sarge = debian.getSeries("sarge")
    ...     parents = [
    ...         factory.makeDistroSeries(name="dobby"),
    ...         factory.makeDistroSeries(name="knobby"),
    ...     ]
    ...     distro_series_parents = [
    ...         factory.makeDistroSeriesParent(
    ...             derived_series=sarge, parent_series=parent
    ...         )
    ...         for parent in parents
    ...     ]
    ...     children = [
    ...         factory.makeDistroSeries(name="bobby"),
    ...         factory.makeDistroSeries(name="tables"),
    ...     ]
    ...     distro_series_children = [
    ...         factory.makeDistroSeriesParent(
    ...             derived_series=child, parent_series=sarge
    ...         )
    ...         for child in children
    ...     ]
    ...

    >>> anon_browser.open("http://launchpad.test/debian/sarge")
    >>> summary_contains(
    ...     anon_browser,
    ...     "Derives from",
    ...     "Dobby",
    ...     "Knobby",
    ...     "Derived series",
    ...     "Bobby",
    ...     "Tables",
    ... )
    True


Distribution series bug subscriptions
-------------------------------------

To receive email notifications about bugs pertaining to a distribution
series, we can create structural bug subscriptions.

    >>> admin_browser.open("http://launchpad.test/ubuntu/warty")
    >>> admin_browser.getLink("Subscribe to bug mail").click()
    >>> print(admin_browser.url)
    http://launchpad.test/ubuntu/warty/+subscribe

    >>> print(admin_browser.title)
    Subscribe : Warty (4.10) : Bugs : Ubuntu
