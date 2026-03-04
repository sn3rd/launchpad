A common error of traversal methods is that they raise a KeyError,
IndexError, LookupError NotFoundError etc. instead of
zope.interfaces.NotFound. This means they generate a System Error page
instead of a correct 404 page. So we test them to ensure the correct
HTTP status is returned.

    >>> def check_not_found(url, host="launchpad.test"):
    ...     output = http("GET %s HTTP/1.1\nHost: %s" % (url, host))
    ...     status = output.getStatus()
    ...     if status != 404:
    ...         raise Exception(
    ...             "%s returned status %s instead of 404\n\n%s"
    ...             % (url, status, str(output))
    ...         )
    ...

    >>> def check_redirect(
    ...     url, auth=False, host="launchpad.test", status=303
    ... ):
    ...     get_cmd = """
    ... GET %s HTTP/1.1
    ... Host: %s
    ... """
    ...     if auth:
    ...         get_cmd += (
    ...             "Authorization: "
    ...             "Basic Zm9vLmJhckBjYW5vbmljYWwuY29tOnRlc3Q=\n"
    ...         )
    ...     response = http(get_cmd % (url, host))
    ...     rc = response.getStatus()
    ...     if rc != status:
    ...         raise Exception(
    ...             "%s returned status %s instead of %d" % (url, rc, status)
    ...         )
    ...     print(response.getHeader("Location"))

    >>> check_redirect("/legal", status=301)
    https://documentation.ubuntu.com/launchpad/user/reference/launchpad-and-
    community/legal/launchpad-policies/
    >>> check_redirect("/faq", status=301)
    https://answers.launchpad.net/launchpad-project/+faqs
    >>> check_redirect("/feedback", status=301)
    https://documentation.ubuntu.com/launchpad/.../feedback-on-launchpad/
    >>> check_redirect("/support/", status=301)
    http://answers.launchpad.test/launchpad

    >>> check_redirect("/", host="feeds.launchpad.test", status=301)
    https://help.launchpad.net/Feeds
    >>> check_redirect("/+index", host="feeds.launchpad.test", status=301)
    https://help.launchpad.net/Feeds

The +translate page in the main host is obsolete so it's now a redirect
to the translations site. This way, we don't break existing links to it.
Before removing this, you must be completely sure that no supported
Ubuntu release is still pointing to this old URL (see bug #138090).

    >>> check_redirect("/products", status=301)
    http://launchpad.test/projects
    >>> check_redirect("/projects/firefox", status=301)
    http://launchpad.test/firefox
    >>> check_redirect("/ubuntu/+source/evolution/+editbugcontact")
    +subscribe
    >>> check_redirect("/ubuntu/hoary/+latest-full-language-pack")
    http://localhost:.../ubuntu-hoary-translations.tar.gz
    >>> check_redirect("/ubuntu/hoary/+source/mozilla-firefox/+pots")
    http://launchpad.test/.../+pots/../+translations

Viewing a bug in the context of an upstream where the bug has already
been reported (including checking the various pages that hang off that
one.)

    >>> check_redirect("/bugs/assigned", auth=True)
    http://launchpad.test/~name16/+assignedbugs
    >>> check_redirect("/bugs/1")
    http://bugs.launchpad.test/firefox/+bug/1
    >>> check_redirect("/firefox/+bug")
    +bugs

Bug attachments in the context of a bugtask are all redirected to be at
+attachment/<id>. The old attachments/<id> form is deprecated.

    >>> login("test@canonical.com")
    >>> attachment = factory.makeBugAttachment(1)
    >>> atid = attachment.id
    >>> logout()

    >>> check_redirect("/firefox/+bug/1/attachments/%d" % atid, status=301)
    http://bugs.launchpad.test/firefox/+bug/1/+attachment/1
    >>> check_redirect(
    ...     "/devel/firefox/+bug/1/attachments/%d" % atid,
    ...     host="api.launchpad.test",
    ...     status=301,
    ... )
    http://api.launchpad.test/devel/firefox/+bug/1/+attachment/1
    >>> check_redirect(
    ...     "/firefox/+bug/1/attachments/%d/+edit" % atid, status=301
    ... )
    http://bugs.launchpad.test/firefox/+bug/1/+attachment/1/+edit
    >>> check_redirect("/bugs/1/attachments/%d" % atid, status=301)
    http://bugs.launchpad.test/bugs/1/+attachment/1
    >>> check_redirect(
    ...     "/devel/bugs/1/attachments/%d" % atid,
    ...     host="api.launchpad.test",
    ...     status=301,
    ... )
    http://api.launchpad.test/devel/bugs/1/+attachment/1
    >>> check_redirect("/bugs/1/attachments/%d/+edit" % atid, status=301)
    http://bugs.launchpad.test/bugs/1/+attachment/1/+edit

Check a bug is traversable by nickname:

    >>> check_redirect("/bugs/blackhole")
    http://bugs.launchpad.test/tomcat/+bug/2
    >>> check_not_found("/bugs/invalid-nickname")

Note that you should not be able to directly file a bug on a
distroseries or sourcepackage; an IBugTask reported against a
distroseries or sourcepackage is *targeted* to be fixed in that specific
release. Instead, you get redirected to the appropriate distro or
distrosourcepackage filebug page.

    >>> check_redirect("/ubuntu/warty/+filebug", auth=True)
    http://launchpad.test/ubuntu/+filebug
    >>> check_redirect(
    ...     "/ubuntu/warty/+source/mozilla-firefox/+filebug", auth=True
    ... )
    http://launchpad.test/ubuntu/+source/mozilla-firefox/+filebug

The old +filebug-advanced form now redirects to the +filebug form.

    >>> check_redirect("/firefox/+filebug-advanced", auth=True, status=301)
    http://bugs.launchpad.test/firefox/+filebug
    >>> check_redirect("/ubuntu/+filebug-advanced", auth=True, status=301)
    http://bugs.launchpad.test/ubuntu/+filebug
    >>> check_redirect(
    ...     "/ubuntu/+source/mozilla-firefox/+filebug-advanced",
    ...     auth=True,
    ...     status=301,
    ... )
    http://bugs.launchpad.test/ubuntu/+source/mozilla-firefox/+filebug

And this is for a person:

    >>> check_redirect("/~name12/+branch/gnome-terminal/pushed/", status=301)
    http://code.launchpad.test/~name12/gnome-terminal/pushed
    >>> check_redirect(
    ...     "/~name12/+branch/gnome-terminal/pushed/+edit",
    ...     auth=True,
    ...     status=301,
    ... )
    http://code.launchpad.test/~name12/gnome-terminal/pushed/+edit
    >>> check_redirect("/~name16/+packages", status=301)
    http://launchpad.test/~name16/+related-packages
    >>> check_redirect("/~name16/+projects", status=301)
    http://launchpad.test/~name16/+related-projects
    >>> check_redirect("/+builds", status=301)
    /builders/
    >>> check_redirect("/translations/groups/", status=301)
    http://translations.launchpad.test/+groups
    >>> check_redirect("/translations/imports/", status=301)
    http://translations.launchpad.test/+imports

The pillar set is published through the web service, but not through the
website.

    >>> check_not_found("/pillars")
    >>> check_not_found("/sourcepackagenames")
    >>> check_not_found("/binarypackagenames")
    >>> check_not_found("/++resource++error")

Check legacy URL redirects

    >>> check_redirect("/distros/ubuntu", status=301)
    http://launchpad.test/ubuntu
    >>> check_redirect("/products/ubuntu-product", status=301)
    http://launchpad.test/projects/ubuntu-product
    >>> check_redirect("/people/stub", status=301)
    http://launchpad.test/~stub

    # wokeignore:rule=blacklist
    >>> check_redirect("/+nameblacklist", auth=True, status=301)
    +nameblocklist

Check redirects of Unicode URLs works

    >>> check_not_found("/ubuntu/foo%C3%A9")
    >>> check_not_found("/@@")
    >>> check_not_found("//@@")
