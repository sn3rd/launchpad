Translation help
================

Links to Translation help on https://help.launchpad.net/ links are
available on a number of translation pages in Launchpad.

The Translations start page provides a link to official documentation.

    >>> browser.open("http://translations.launchpad.test/")
    >>> browser.getLink(id="link-to-translations-help").url
    'https://help.launchpad.net/Translations'

Links to translation help are provided on a number of translation related
pages.  Namely, on a Distribution and DistroSeries pages:

    >>> browser.open("http://translations.launchpad.test/ubuntu")
    >>> browser.getLink(id="link-to-translations-help").url
    'https://help.launchpad.net/Translations'

    >>> browser.open("http://translations.launchpad.test/ubuntu/hoary")
    >>> browser.getLink(id="link-to-translations-help").url
    'https://help.launchpad.net/Translations'

Product and ProductSeries translations pages also provide direct link to
the translations help.

    >>> browser.open("http://translations.launchpad.test/evolution")
    >>> browser.getLink(id="link-to-translations-help").url
    'https://help.launchpad.net/Translations'

    >>> browser.open("http://translations.launchpad.test/evolution/trunk")
    >>> browser.getLink(id="link-to-translations-help").url
    'https://help.launchpad.net/Translations'

Each PO template overview page also provides a link to documentation for
translation, but this one takes people directly to translators'
documentation.

    >>> browser.open(
    ...     "http://translations.launchpad.test/evolution"
    ...     "/trunk/+pots/evolution-2.2"
    ... )
    >>> browser.getLink(
    ...     id="link-to-translations-help"
    ... ).url  # doctest: +ELLIPSIS
    'https://documentation.ubuntu.com/launchpad/.../prepare-to-translate/'
