Launchpad front pages
---------------------

Visit our home page with the typical configuration:

    >>> browser.open("http://launchpad.test/")
    >>> browser.url
    'http://launchpad.test/'

It contains a string which our IS uses to determine whether or not
Launchpad is alive:

    >>> print(browser.contents)
    <!DOCTYPE...
    ...
    <!-- Is your project registered yet? -->
    ...

And links to the important applications and facets:

    >>> code_link = browser.getLink(url="code")
    >>> code_link.url
    'http://code.launchpad.test/'

It also includes a search form...

    >>> print(browser.getControl("Search Launchpad").value)
    Search Launchpad

...and lists of featured projects and marketing material.

    >>> featured = find_tag_by_id(browser.contents, "homepage-featured")
    >>> print(extract_text(featured.decode_contents()))
    Featured projects
    ...
    Ubuntu
    ...

The footer doesn't contain the links that are already present on the page.

    >>> print(find_tags_by_class(browser.contents, "lp-arcana"))
    []

The homepage looks different when the user is logged in:

    >>> user_browser = setupBrowser(auth="Basic test@canonical.com:test")
    >>> user_browser.open("http://launchpad.test/")

Now there are links to create projects and teams:

    >>> project_link = user_browser.getLink(url="/projects")
    >>> project_link.url
    'http://launchpad.test/projects/+new'

    >>> people_link = user_browser.getLink(url="/people")
    >>> people_link.url
    'http://launchpad.test/people/+newteam'


The front pages for the other applications, however, do have
application tabs. On these pages, the tab normally named "Overview"
is labelled "Launchpad Home" to make clear where it goes.

    >>> anon_browser.open("http://code.launchpad.test/")
    >>> print_location_apps(anon_browser.contents)
    * Launchpad Home - http://launchpad.test/
    * Code (selected) - http://code.launchpad.test/
    * Bugs - http://bugs.launchpad.test/
    * Blueprints - http://blueprints.launchpad.test/
    * Translations - http://translations.launchpad.test/
    * Answers - http://answers.launchpad.test/

    >>> user_browser.open("http://answers.launchpad.test/")
    >>> print_location_apps(user_browser.contents)
    * Launchpad Home - http://launchpad.test/
    * Code - http://code.launchpad.test/
    * Bugs - http://bugs.launchpad.test/
    * Blueprints - http://blueprints.launchpad.test/
    * Translations - http://translations.launchpad.test/
    * Answers (selected) - http://answers.launchpad.test/

The footer of those pages contains the link to the front page, tour
and guide:

    >>> print(
    ...     extract_text(
    ...         find_tags_by_class(user_browser.contents, "lp-arcana")[0]
    ...     )
    ... )
    • Take the tour • Read the guide
