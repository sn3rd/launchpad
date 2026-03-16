# This file modified from Zope3/Makefile
# Licensed under the ZPL, (c) Zope Corporation and contributors.

PYTHON?=python3

WD:=$(shell pwd)
PY=$(WD)/bin/py
PYTHONPATH:=$(WD)/lib:${PYTHONPATH}
VERBOSITY=-vv

DEPENDENCY_REPO ?= https://git.launchpad.net/lp-source-dependencies

# virtualenv and pip fail if setlocale fails, so force a valid locale.
PIP_ENV := LC_ALL=C.UTF-8
# Run with "make PIP_NO_INDEX=" if you want pip to find software
# dependencies *other* than those in our download-cache.  Once you have the
# desired software, commit it to lp:lp-source-dependencies if it is going to
# be reviewed/merged/deployed.
PIP_NO_INDEX := 1
PIP_ENV += PIP_NO_INDEX=$(PIP_NO_INDEX)
PIP_ENV += PIP_FIND_LINKS="file://$(WD)/wheels/ file://$(WD)/download-cache/dist/"

VIRTUALENV := $(PIP_ENV) /usr/bin/virtualenv
PIP := PYTHONPATH= $(PIP_ENV) env/bin/pip --cache-dir=$(WD)/download-cache/

VENV_INSTANCE_NAME := env/instance_name
VENV_PYTHON := env/bin/$(notdir $(PYTHON))

SITE_PACKAGES := \
	$$($(VENV_PYTHON) -c 'from distutils.sysconfig import get_python_lib; print(get_python_lib())')

TESTFLAGS=-p $(VERBOSITY)
TESTOPTS=

SHHH=utilities/shhh.py

LPCONFIG?=development

LISTEN_ADDRESS?=127.0.0.88

ICING=lib/canonical/launchpad/icing
LP_BUILT_JS_ROOT=${ICING}/build

JS_BUILD_DIR := build/js
YARN_VERSION := 1.22.4
YARN_BUILD := $(JS_BUILD_DIR)/yarn
YARN := utilities/yarn
YUI_SYMLINK := $(JS_BUILD_DIR)/yui
LP_JS_BUILD := $(JS_BUILD_DIR)/lp

MINS_TO_SHUTDOWN=15

CODEHOSTING_ROOT=/var/tmp/bazaar.launchpad.test

CONVOY_ROOT?=/srv/launchpad.test/convoy

VERSION_INFO = version-info.py

APIDOC_DIR = lib/canonical/launchpad/apidoc
APIDOC_TMPDIR = $(APIDOC_DIR).tmp/
API_INDEX = $(APIDOC_DIR)/index.html

# It is impossible to get pip to tell us all the files it would build, since
# each package's setup.py doesn't tell us that information.
#
# NB: It's important PIP_BIN only mentions things genuinely produced by pip.
PIP_BIN = \
    $(PY) \
    $(VENV_INSTANCE_NAME) \
    bin/bingtestservice \
    bin/build-twisted-plugin-cache \
    bin/harness \
    bin/iharness \
    bin/ipy \
    bin/jsbuild \
    bin/lpjsmin \
    bin/killservice \
    bin/kill-test-services \
    bin/retest \
    bin/run \
    bin/run-testapp \
    bin/sprite-util \
    bin/start_librarian \
    bin/test \
    bin/twistd \
    bin/watch_jsbuild \
    bin/with-xvfb

# Create archives in labelled directories (e.g.
# <rev-id>/$(PROJECT_NAME).tar.gz)
GIT_BRANCH := $(shell if [ -d .git ]; then git branch --show-current; fi)
TARBALL_REVISION ?= $(shell if [ -d .git ]; then git rev-parse HEAD; fi)
ifeq ($(GIT_BRANCH),db-devel)
TARBALL_SUFFIX := db
else
TARBALL_SUFFIX :=
endif
TARBALL_BUILD_LABEL := $(TARBALL_REVISION)$(if $(TARBALL_SUFFIX),-$(TARBALL_SUFFIX))
TARBALL_FILE_NAME = launchpad.tar.gz
TARBALL_BUILD_DIR = dist/$(TARBALL_BUILD_LABEL)
TARBALL_BUILD_PATH = $(TARBALL_BUILD_DIR)/$(TARBALL_FILE_NAME)

SWIFT_CONTAINER_NAME ?= launchpad-builds
# This must match the object path used by fetch_payload in the ols charm
# layer.
SWIFT_OBJECT_PATH = \
       launchpad-builds/$(TARBALL_BUILD_LABEL)/$(TARBALL_FILE_NAME)


# DO NOT ALTER : this should just build by default
.PHONY: default
default: inplace

.PHONY: schema
schema: compile
	$(MAKE) -C database/schema
	$(RM) -r /var/tmp/fatsam

.PHONY: newsampledata
newsampledata:
	$(MAKE) -C database/schema newsampledata

.PHONY: hosted_branches
hosted_branches: $(PY)
	$(PY) ./utilities/make-dummy-hosted-branches

$(API_INDEX): $(VERSION_INFO) $(PY)
	$(RM) -r $(APIDOC_DIR) $(APIDOC_DIR).tmp
	mkdir -p $(APIDOC_DIR).tmp
	LPCONFIG=$(LPCONFIG) ./utilities/create-lp-wadl-and-apidoc.py \
	    --force "$(APIDOC_TMPDIR)"
	mv $(APIDOC_TMPDIR) $(APIDOC_DIR)

.PHONY: apidoc
ifdef LP_MAKE_NO_WADL
apidoc:
	@echo "Skipping WADL generation."
else
apidoc: compile $(API_INDEX)
endif

# Run by PQM.
.PHONY: check_config
check_config: build
	bin/test -m lp.services.config.tests -vvt test_config

logs:
	mkdir logs

.PHONY: codehosting-dir
codehosting-dir:
	mkdir -p $(CODEHOSTING_ROOT)
	mkdir -p $(CODEHOSTING_ROOT)/mirrors
	mkdir -p $(CODEHOSTING_ROOT)/config
	mkdir -p /var/tmp/bzrsync
	touch $(CODEHOSTING_ROOT)/rewrite.log
	chmod 777 $(CODEHOSTING_ROOT)/rewrite.log
	touch $(CODEHOSTING_ROOT)/config/launchpad-lookup.txt
ifneq ($(SUDO_UID),)
	if [ "$$(id -u)" = 0 ]; then \
		chown -R $(SUDO_UID):$(SUDO_GID) $(CODEHOSTING_ROOT); \
	fi
endif

.PHONY: inplace
inplace: build logs clean_logs codehosting-dir
	if [ -d /srv/launchpad.test ]; then \
		ln -sfn $(WD)/build/js $(CONVOY_ROOT); \
	fi

.PHONY: build
build: compile apidoc jsbuild css_combine

# Bootstrap download-cache.  Useful for CI jobs that want to set this up
# from scratch.
.PHONY: bootstrap
bootstrap:
	if [ -d download-cache/.git ]; then \
		git -C download-cache pull; \
	else \
		git clone --depth=1 $(DEPENDENCY_REPO) download-cache; \
	fi

# LP_PROJECT_ROOT/LP_SOURCEDEPS_DIR points to the parent directory where the
# download-cache and env directories are.  We reuse the variables that are
# used for the rocketfuel-get script.
download-cache:
ifneq (,$(LP_PROJECT_ROOT)$(LP_SOURCEDEPS_DIR))
	utilities/link-external-sourcecode $(LP_PROJECT_ROOT)/$(LP_SOURCEDEPS_DIR)
else
	@echo "Missing ./download-cache."
	@echo "Developers: please run utilities/link-external-sourcecode."
	@exit 1
endif

.PHONY: css_combine
css_combine: jsbuild_widget_css
	${SHHH} bin/sprite-util create-image
	${SHHH} bin/sprite-util create-css
	ln -sfn ../../../../yarn/node_modules/yui $(ICING)/yui
	# Compile the base.css file separately for tests
	$(YARN) run sass --load-path $(WD)/$(ICING) $(WD)/$(ICING)/css/base.scss $(WD)/$(ICING)/base.css
	# Compile the combo.css for the main site
	# XXX 2020-06-12 twom This should have `--style=compressed`. Removed for debugging purposes
	$(YARN) run sass --load-path $(WD)/$(ICING) $(WD)/$(ICING)/combo.scss $(WD)/$(ICING)/combo.css
	$(YARN) run sass --load-path $(WD)/$(ICING) $(WD)/$(ICING)/vanilla/styles.scss $(WD)/$(ICING)/vanilla/styles.css --style=compressed
	$(YARN) run sass $(WD)/$(ICING)/vanilla/pages/:$(WD)/$(ICING)/vanilla/pages/ --style=compressed

.PHONY: css_watch
css_watch: jsbuild_widget_css
	${SHHH} bin/sprite-util create-image
	${SHHH} bin/sprite-util create-css
	ln -sfn ../../../../yarn/node_modules/yui $(ICING)/yui
	$(YARN) run sass --load-path $(WD)/$(ICING) $(WD)/$(ICING)/:$(WD)/$(ICING)/ --watch

.PHONY: jsbuild_widget_css
jsbuild_widget_css: bin/jsbuild
	${SHHH} bin/jsbuild \
	    --srcdir lib/lp/app/javascript \
	    --builddir $(LP_BUILT_JS_ROOT)

.PHONY: jsbuild_watch
jsbuild_watch:
	$(PY) bin/watch_jsbuild

$(JS_BUILD_DIR):
	mkdir -p $@

$(YARN_BUILD): | $(JS_BUILD_DIR)
	mkdir -p $@/tmp
	tar -C $@/tmp -xf download-cache/dist/yarn-v$(YARN_VERSION).tar.gz
	mv $@/tmp/yarn-v$(YARN_VERSION)/* $@
	$(RM) -r $@/tmp

$(JS_BUILD_DIR)/.production: yarn/package.json | $(YARN_BUILD)
	$(YARN) install --offline --frozen-lockfile --production
	# We don't use YUI's Flash components and they have a bad security
	# record. Kill them.
	find yarn/node_modules/yui -name '*.swf' -delete
	touch $@

$(YUI_SYMLINK): $(JS_BUILD_DIR)/.production
	ln -sfn ../../yarn/node_modules/yui $@

.PHONY: $(LP_JS_BUILD)
$(LP_JS_BUILD): | $(JS_BUILD_DIR)
	mkdir -p $@/services
	for jsdir in lib/lp/*/javascript lib/lp/services/*/javascript; do \
		app=$$(echo $$jsdir | sed -e 's,lib/lp/\(.*\)/javascript,\1,'); \
		cp -a $$jsdir $@/$$app; \
	done
	find $@ -name 'tests' -type d | xargs rm -rf
	LC_ALL=C.UTF-8 bin/lpjsmin -p $@

.PHONY: jsbuild
jsbuild: $(LP_JS_BUILD) $(YUI_SYMLINK)
	LC_ALL=C.UTF-8 utilities/js-deps -n LP_MODULES -s build/js/lp \
		-x '-min.js' -o build/js/lp/meta.js >/dev/null
	utilities/check-js-deps

requirements/combined.txt: \
		requirements/setup.txt \
		requirements/ztk-versions.cfg \
		requirements/launchpad.txt
	$(PYTHON) utilities/make-requirements.py \
		--exclude requirements/setup.txt \
		--buildout requirements/ztk-versions.cfg \
		--include requirements/launchpad.txt \
		>"$@"

# It doesn't seem to be straightforward to build a wheelhouse of all our
# dependencies without also building a useless wheel of Launchpad itself;
# fortunately that doesn't take too long, and we just remove it afterwards.
.PHONY: build_wheels_only
build_wheels_only: $(PIP_BIN) requirements/combined.txt
	$(RM) -r wheelhouse wheels
	$(SHHH) $(PIP) wheel -w wheels -r requirements/setup.txt
	$(SHHH) $(PIP) wheel \
		-c requirements/setup.txt -c requirements/combined.txt \
		-w wheels .
	$(RM) wheels/lp-[0-9]*.whl

# This target is used by deployment machinery to prepare a build to be
# pushed out to destination machines.  We only want wheels: they are the
# expensive bits, and the other bits might run into problems like bug
# 575037.  This target runs pip, builds a wheelhouse with predictable paths
# that can be used even if the build is pushed to a different path on the
# destination machines, and then removes everything created except for the
# wheels.
.PHONY: build_wheels
build_wheels: build_wheels_only
	$(MAKE) clean_js clean_pip

# Compatibility
.PHONY: build_eggs
build_eggs: build_wheels

# Build a tarball that can be unpacked and built on another machine,
# including all the wheels we need.  This will eventually supersede
# build_wheels.
.PHONY: build-tarball
build-tarball:
	utilities/build-tarball $(TARBALL_BUILD_DIR)

# Publish a buildable tarball to Swift.
.PHONY: publish-tarball
publish-tarball: build-tarball
	[ ! -e ~/.config/swift/launchpad ] || . ~/.config/swift/launchpad; \
	utilities/publish-to-swift --debug \
		$(SWIFT_CONTAINER_NAME) $(SWIFT_OBJECT_PATH) \
		$(TARBALL_BUILD_PATH) $(TARBALL_SUFFIX)

# setuptools won't touch files that would have the same contents, but for
# Make's sake we need them to get fresh timestamps, so we touch them after
# building.
#
# If we listed every target on the left-hand side, a parallel make would try
# multiple copies of this rule to build them all.  Instead, we nominally build
# just $(VENV_INSTANCE_NAME), and everything else is implicitly updated by
# that.
$(VENV_INSTANCE_NAME): download-cache requirements/combined.txt setup.py
	rm -rf env
	mkdir -p env
	$(VIRTUALENV) \
		--python=$(PYTHON) --never-download \
		--extra-search-dir=$(WD)/download-cache/dist/ \
		--extra-search-dir=$(WD)/wheels/ \
		env
	ln -sfn env/bin bin
	$(PIP) install -r requirements/setup.txt
	LPCONFIG=$(LPCONFIG) $(PIP) \
		install \
		-c requirements/setup.txt -c requirements/combined.txt -e . \
		|| { code=$$?; rm -f $@; exit $$code; }
	touch $@

$(subst $(VENV_INSTANCE_NAME),,$(PIP_BIN)): $(VENV_INSTANCE_NAME)

# Explicitly update version-info.py rather than declaring $(VERSION_INFO) as
# a prerequisite, to make sure it's up to date when doing deployments.
.PHONY: compile
compile: $(VENV_INSTANCE_NAME)
	${SHHH} utilities/relocate-virtualenv env
	$(PYTHON) utilities/link-system-packages.py \
		"$(SITE_PACKAGES)" system-packages.txt
	${SHHH} bin/build-twisted-plugin-cache
	[ ! -d .git ] || scripts/update-version-info.sh

.PHONY: test_build
test_build: build
	bin/test $(TESTFLAGS) $(TESTOPTS)

.PHONY: test_inplace
test_inplace: inplace
	bin/test $(TESTFLAGS) $(TESTOPTS)

.PHONY: ftest_build
ftest_build: build
	bin/test -f $(TESTFLAGS) $(TESTOPTS)

.PHONY: ftest_inplace
ftest_inplace: inplace
	bin/test -f $(TESTFLAGS) $(TESTOPTS)

.PHONY: run
run: build inplace stop
	bin/run -r librarian,bing-webservice,memcached,rabbitmq \
	-i $(LPCONFIG)

.PHONY: run-testapp
run-testapp: LPCONFIG=testrunner-appserver
run-testapp: build inplace stop
	LPCONFIG=$(LPCONFIG) INTERACTIVE_TESTS=1 bin/run-testapp \
	-r memcached -i $(LPCONFIG)

run.gdb:
	echo 'run' > run.gdb

.PHONY: start-gdb
start-gdb: build inplace stop support_files run.gdb
	nohup gdb -x run.gdb --args bin/run -i $(LPCONFIG) \
		-r librarian,bing-webservice
		> ${LPCONFIG}-nohup.out 2>&1 &

.PHONY: run_all
run_all: build inplace stop
	bin/run \
	 -r librarian,sftp,codebrowse,bing-webservice,\
	memcached,rabbitmq -i $(LPCONFIG)

.PHONY: run_codebrowse
run_codebrowse: compile
	BRZ_PLUGIN_PATH=brzplugins $(PY) scripts/start-loggerhead.py

.PHONY: start_codebrowse
start_codebrowse: compile
	BRZ_PLUGIN_PATH=$(shell pwd)/brzplugins $(PY) scripts/start-loggerhead.py --daemon

.PHONY: stop_codebrowse
stop_codebrowse:
	$(PY) scripts/stop-loggerhead.py

.PHONY: run_codehosting
run_codehosting: build inplace stop
	bin/run -r librarian,sftp,codebrowse,rabbitmq -i $(LPCONFIG)

.PHONY: start_librarian
start_librarian: compile
	bin/start_librarian

.PHONY: stop_librarian
stop_librarian:
	bin/killservice librarian

$(VERSION_INFO):
	scripts/update-version-info.sh

.PHONY: support_files
support_files: $(API_INDEX) $(VERSION_INFO)

# Intended for use on developer machines
.PHONY: start
start: inplace stop support_files initscript-start

# Run as a daemon - hack using nohup until we move back to using zdaemon
# properly. We also should really wait until services are running before
# exiting, as running 'make stop' too soon after running 'make start'
# will not work as expected. For use on production servers, where
# we know we don't need the extra steps in a full "make start"
# because of how the code is deployed/built.
.PHONY: initscript-start
initscript-start:
	nohup bin/run -i $(LPCONFIG) > ${LPCONFIG}-nohup.out 2>&1 &

# Intended for use on developer machines
.PHONY: stop
stop: build initscript-stop

# Kill launchpad last - other services will probably shutdown with it,
# so killing them after is a race condition. For use on production
# servers, where we know we don't need the extra steps in a full
# "make stop" because of how the code is deployed/built.
.PHONY: initscript-stop
initscript-stop:
	bin/killservice librarian launchpad

.PHONY: shutdown
shutdown: scheduleoutage stop
	$(RM) +maintenancetime.txt

.PHONY: scheduleoutage
scheduleoutage:
	echo Scheduling outage in ${MINS_TO_SHUTDOWN} mins
	date --iso-8601=minutes -u -d +${MINS_TO_SHUTDOWN}mins > +maintenancetime.txt
	echo Sleeping ${MINS_TO_SHUTDOWN} mins
	sleep ${MINS_TO_SHUTDOWN}m

.PHONY: harness
harness: bin/harness
	bin/harness

.PHONY: iharness
iharness: bin/iharness
	bin/iharness

.PHONY: rebuildfti
rebuildfti:
	@echo Rebuilding FTI indexes on launchpad_dev database
	$(PY) database/schema/fti.py -d launchpad_dev --force

.PHONY: clean_js
clean_js:
	$(RM) -r $(JS_BUILD_DIR)
	$(RM) -r yarn/node_modules

.PHONY: clean_pip
clean_pip:
	$(RM) -r build
	if [ -d $(CONVOY_ROOT) ]; then $(RM) -r $(CONVOY_ROOT) ; fi
	$(RM) -r bin
	$(RM) -r env
	$(RM) -r parts
	$(RM) .installed.cfg

# Compatibility.
.PHONY: clean_buildout
clean_buildout: clean_pip

.PHONY: clean_logs
clean_logs:
	$(RM) logs/thread*.request

.PHONY: lxc-clean
lxc-clean: clean_js clean_pip clean_logs
	# XXX: BradCrittenden 2012-05-25 bug=1004514:
	# It is important for parallel tests inside LXC that the
	# $(CODEHOSTING_ROOT) directory not be completely removed.
	# This target removes its contents but not the directory and
	# it does everything expected from a clean target.  When the
	# referenced bug is fixed, this target may be reunited with
	# the 'clean' target.
	$(RM) -r env wheelhouse wheels
	$(RM) requirements/combined.txt
	$(RM) -r $(LP_BUILT_JS_ROOT)/*
	$(RM) -r $(CODEHOSTING_ROOT)/*
	$(RM) -r $(APIDOC_DIR)
	$(RM) -r $(APIDOC_DIR).tmp
	$(RM) -r build
	$(RM) $(VERSION_INFO)
	$(RM) +config-overrides.zcml
	$(RM) -r /var/tmp/builddmaster \
			  /var/tmp/bzrsync \
			  /var/tmp/codehosting.test \
			  /var/tmp/codeimport \
			  /var/tmp/fatsam.test \
			  /var/tmp/lperr \
			  /var/tmp/lperr.test \
			  /var/tmp/ppa \
			  /var/tmp/ppa.test \
			  /var/tmp/testkeyserver
	# /var/tmp/launchpad_mailqueue is created read-only on ec2test
	# instances.
	if [ -w /var/tmp/launchpad_mailqueue ]; then \
		$(RM) -r /var/tmp/launchpad_mailqueue; \
	fi

.PHONY: clean
clean: lxc-clean
	$(RM) -r $(CODEHOSTING_ROOT)

.PHONY: realclean
realclean: clean
	$(RM) TAGS tags

.PHONY: potemplates
potemplates: launchpad.pot

# Generate launchpad.pot by extracting message ids from the source
# XXX cjwatson 2017-09-04: This was previously done using i18nextract from
# z3c.recipe.i18n, but has been broken for some time.  The place to start in
# putting this together again is probably zope.app.locales.
.PHONY: launchpad.pot
launchpad.pot:
	echo "POT generation not currently supported; help us fix this!" >&2
	exit 1

# Called by the rocketfuel-setup script. You probably don't want to run this
# on its own.
.PHONY: install
install: reload-apache

.PHONY: copy-certificates
copy-certificates:
	mkdir -p /etc/apache2/ssl
	cp configs/$(LPCONFIG)/launchpad.crt /etc/apache2/ssl/
	cp configs/$(LPCONFIG)/launchpad.key /etc/apache2/ssl/

.PHONY: copy-apache-config
copy-apache-config: codehosting-dir
	# Byte-compile scripts/_pythonpath.py first, otherwise Apache may do
	# so as root and cause permission problems.
	$(PYTHON) -m py_compile scripts/_pythonpath.py
	# We insert the absolute path to the branch-rewrite script
	# into the Apache config as we copy the file into position.
	set -e; \
	apachever="$$(dpkg-query -W --showformat='$${Version}' apache2)"; \
	if dpkg --compare-versions "$$apachever" ge 2.4.1-1~; then \
		base=local-launchpad.conf; \
	else \
		base=local-launchpad; \
	fi; \
	sed -e 's,%BRANCH_REWRITE%,$(shell pwd)/scripts/branch-rewrite.py,' \
		-e 's,%WSGI_ARCHIVE_AUTH%,$(shell pwd)/scripts/wsgi-archive-auth.py,' \
		-e 's,%LISTEN_ADDRESS%,$(LISTEN_ADDRESS),' \
		configs/$(LPCONFIG)/local-launchpad-apache > \
		/etc/apache2/sites-available/$$base
	if [ ! -d /srv/launchpad.test ]; then \
		mkdir /srv/launchpad.test; \
		chown $(SUDO_UID):$(SUDO_GID) /srv/launchpad.test; \
	fi

.PHONY: enable-apache-launchpad
enable-apache-launchpad: copy-apache-config copy-certificates
	[ ! -e /etc/apache2/mods-available/version.load ] || a2enmod version
	a2ensite local-launchpad

.PHONY: reload-apache
reload-apache: enable-apache-launchpad
	service apache2 restart

.PHONY: TAGS
TAGS: compile
	# emacs tags
	ctags -R -e --languages=-JavaScript --python-kinds=-i -f $@.new \
		$(CURDIR)/lib "$(SITE_PACKAGES)"
	mv $@.new $@

.PHONY: tags
tags: compile
	# vi tags
	ctags -R --languages=-JavaScript --python-kinds=-i -f $@.new \
		$(CURDIR)/lib "$(SITE_PACKAGES)"
	mv $@.new $@

PYDOCTOR = pydoctor
PYDOCTOR_OPTIONS =

.PHONY: pydoctor
pydoctor:
	$(PYDOCTOR) --make-html --html-output=apidocs --add-package=lib/lp \
		--add-package=lib/canonical --project-name=Launchpad \
		--docformat restructuredtext --verbose-about epytext-summary \
		$(PYDOCTOR_OPTIONS)

.PHONY: build-lxd-image
build-lxd-image:
	sudo lxd-imagebuilder --debug build-lxd $(CURDIR)/lpdev-image-file.yaml --type unified
