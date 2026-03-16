# Copyright 2009-2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "BuilderInteractor",
    "BuilderWorker",
    "extract_vitals_from_db",
]

import logging
import os.path
import sys
import traceback
from collections import OrderedDict, namedtuple
from urllib.parse import urlparse

import six
import transaction
from ampoule.pool import ProcessPool
from twisted.internet import defer
from twisted.internet import reactor as default_reactor
from twisted.internet.interfaces import IReactorCore
from twisted.web import xmlrpc
from twisted.web.client import HTTPConnectionPool
from zope.security.proxy import isinstance as zope_isinstance
from zope.security.proxy import removeSecurityProxy

from lp.buildmaster.downloader import DownloadCommand, RequestProcess
from lp.buildmaster.enums import BuilderCleanStatus, BuilderResetProtocol
from lp.buildmaster.interfaces.builder import (
    BuildDaemonError,
    BuildDaemonIsolationError,
    CannotFetchFile,
    CannotResumeHost,
)
from lp.buildmaster.interfaces.buildfarmjobbehaviour import (
    IBuildFarmJobBehaviour,
)
from lp.services.config import config
from lp.services.job.runner import QuietAMPConnector, VirtualEnvProcessStarter
from lp.services.twistedsupport import cancel_on_timeout, gatherResults
from lp.services.twistedsupport.processmonitor import ProcessWithTimeout
from lp.services.webapp import urlappend


class QuietQueryFactory(xmlrpc._QueryFactory):
    """XMLRPC client factory that doesn't splatter the log with junk."""

    noisy = False


_default_pool = None
_default_process_pool = None
_default_process_pool_shutdown = None


def default_pool(reactor=None):
    global _default_pool
    if reactor is None:
        reactor = default_reactor
    if _default_pool is None:
        _default_pool = HTTPConnectionPool(reactor)
    return _default_pool


def make_download_process_pool(**kwargs):
    """Make a pool of processes for downloading files."""
    env = {"PATH": os.environ["PATH"]}
    if "LPCONFIG" in os.environ:
        env["LPCONFIG"] = os.environ["LPCONFIG"]
    starter = VirtualEnvProcessStarter(env=env)
    starter.connectorFactory = QuietAMPConnector
    kwargs = dict(kwargs)
    kwargs.setdefault("max", config.builddmaster.download_connections)
    # ampoule defaults to stopping child processes after they've been idle
    # for 20 seconds, which is a bit eager since that's close to our scan
    # interval.  Bump this to five minutes so that we have less unnecessary
    # process stop/start activity.  This isn't essential tuning so isn't
    # currently configurable, but if we find we need to tweak it further
    # then we should add a configuration setting for it.
    kwargs.setdefault("maxIdle", 300)
    return ProcessPool(RequestProcess, starter=starter, **kwargs)


def default_process_pool(reactor=None):
    global _default_process_pool, _default_process_pool_shutdown
    if reactor is None:
        reactor = default_reactor
    if _default_process_pool is None:
        _default_process_pool = make_download_process_pool()
        _default_process_pool.start()
        if IReactorCore.providedBy(reactor):
            shutdown_id = reactor.addSystemEventTrigger(
                "during", "shutdown", _default_process_pool.stop
            )
            _default_process_pool_shutdown = (reactor, shutdown_id)
    return _default_process_pool


@defer.inlineCallbacks
def shut_down_default_process_pool():
    """Shut down the default process pool.  Used in test cleanup."""
    global _default_process_pool, _default_process_pool_shutdown
    if _default_process_pool is not None:
        yield _default_process_pool.stop()
        _default_process_pool = None
    if _default_process_pool_shutdown is not None:
        reactor, shutdown_id = _default_process_pool_shutdown
        reactor.removeSystemEventTrigger(shutdown_id)
        _default_process_pool_shutdown = None


class BuilderWorker:
    """Add in a few useful methods for the XMLRPC worker.

    :ivar url: The URL of the actual builder. The XML-RPC resource and
        the filecache live beneath this.
    """

    # WARNING: If you change the API for this, you should also change the APIs
    # of the mocks in soyuzbuilderhelpers to match. Otherwise, you will have
    # many false positives in your test run and will most likely break
    # production.

    def __init__(
        self,
        proxy,
        builder_url,
        vm_host,
        timeout,
        reactor,
        pool=None,
        process_pool=None,
    ):
        """Initialize a BuilderWorker.

        :param proxy: An XML-RPC proxy, implementing 'callRemote'. It must
            support passing and returning None objects.
        :param builder_url: The URL of the builder.
        :param vm_host: The VM host to use when resuming.
        """
        self.url = builder_url
        self._vm_host = vm_host
        self._file_cache_url = urlappend(builder_url, "filecache")
        self._server = proxy
        self.timeout = timeout
        if reactor is None:
            reactor = default_reactor
        self.reactor = reactor
        if pool is None:
            pool = default_pool(reactor=reactor)
        self.pool = pool
        if process_pool is None:
            process_pool = default_process_pool(reactor=reactor)
        self.process_pool = process_pool

    @classmethod
    def makeBuilderWorker(
        cls,
        builder_url,
        vm_host,
        timeout,
        reactor=None,
        proxy=None,
        pool=None,
        process_pool=None,
    ):
        """Create and return a `BuilderWorker`.

        :param builder_url: The URL of the worker buildd machine,
            e.g. http://localhost:8221
        :param vm_host: If the worker is virtual, specify its host machine
            here.
        :param reactor: Used by tests to override the Twisted reactor.
        :param proxy: Used By tests to override the xmlrpc.Proxy.
        :param pool: Used by tests to override the HTTPConnectionPool.
        :param process_pool: Used by tests to override the ProcessPool.
        """
        rpc_url = urlappend(builder_url, "rpc")
        if proxy is None:
            server_proxy = xmlrpc.Proxy(
                rpc_url.encode("UTF-8"), allowNone=True, connectTimeout=timeout
            )
            server_proxy.queryFactory = QuietQueryFactory
        else:
            server_proxy = proxy
        return cls(
            server_proxy,
            builder_url,
            vm_host,
            timeout,
            reactor,
            pool=pool,
            process_pool=process_pool,
        )

    def _with_timeout(self, d, timeout=None):
        return cancel_on_timeout(d, timeout or self.timeout, self.reactor)

    def abort(self):
        """Abort the current build."""
        return self._with_timeout(self._server.callRemote("abort"))

    def clean(self):
        """Clean up the waiting files and reset the worker's internal state."""
        return self._with_timeout(self._server.callRemote("clean"))

    def echo(self, *args):
        """Echo the arguments back."""
        return self._with_timeout(self._server.callRemote("echo", *args))

    def proxy_info(self):
        """Return the details for the proxy used by the manager."""
        return self._with_timeout(self._server.callRemote("proxy_info"))

    def info(self):
        """Return the protocol version and the builder methods supported."""
        return self._with_timeout(self._server.callRemote("info"))

    def status(self):
        """Return the status of the build daemon."""
        return self._with_timeout(self._server.callRemote("status"))

    def ensurepresent(self, sha1sum, url, username, password):
        """Attempt to ensure the given file is present."""
        # XXX: Nothing external calls this. Make it private.
        # Use a larger timeout than other calls, as this synchronously
        # downloads large files.
        return self._with_timeout(
            self._server.callRemote(
                "ensurepresent", sha1sum, url, username, password
            ),
            self.timeout * 20,
        )

    def getURL(self, sha1):
        """Get the URL for a file on the builder with a given SHA-1."""
        return urlappend(self._file_cache_url, sha1)

    @defer.inlineCallbacks
    def getFile(self, sha_sum, path_to_write, logger=None):
        """Fetch a file from the builder.

        :param sha_sum: The sha of the file (which is also its name on the
            builder)
        :param path_to_write: A file name to write the file to
        :param logger: An optional logger.
        :return: A Deferred that calls back when the download is done, or
            errback with the error string.
        """
        file_url = self.getURL(sha_sum)
        for attempt in range(config.builddmaster.download_attempts):
            try:
                # Download the file in a subprocess.  We used to download it
                # asynchronously in Twisted, but in practice this only
                # worked well up to a bit over a hundred builders; beyond
                # that it struggled to keep up with incoming packets in time
                # to avoid TCP timeouts (perhaps because of too much
                # synchronous work being done on the reactor thread).
                yield self.process_pool.doWork(
                    DownloadCommand,
                    file_url=file_url,
                    path_to_write=path_to_write,
                    timeout=self.timeout,
                )
                if logger is not None:
                    logger.info("Grabbed %s" % file_url)
                break
            except Exception as e:
                if logger is not None:
                    logger.info(
                        "Failed to grab %s: %s\n%s"
                        % (
                            file_url,
                            e,
                            " ".join(
                                traceback.format_exception(*sys.exc_info())
                            ),
                        )
                    )
                if attempt == config.builddmaster.download_attempts - 1:
                    raise

    def getFiles(self, files, logger=None):
        """Fetch many files from the builder.

        :param files: A sequence of pairs of the builder file name to
            retrieve and the file name to write the file to.
        :param logger: An optional logger.

        :return: A DeferredList that calls back when the download is done.
        """
        dl = gatherResults(
            [
                self.getFile(builder_file, local_file, logger=logger)
                for builder_file, local_file in files
            ]
        )
        return dl

    def resume(self, clock=None):
        """Resume the builder in an asynchronous fashion.

        We use the builddmaster configuration 'socket_timeout' as
        the process timeout.

        :param clock: An optional twisted.internet.task.Clock to override
                      the default clock.  For use in tests.

        :return: a Deferred that returns a
            (stdout, stderr, subprocess exitcode) triple
        """
        url_components = urlparse(self.url)
        buildd_name = url_components.hostname.split(".")[0]
        resume_command = config.builddmaster.vm_resume_command % {
            "vm_host": self._vm_host,
            "buildd_name": buildd_name,
        }
        # Twisted API requires string but the configuration provides unicode.
        resume_argv = [term.encode("utf-8") for term in resume_command.split()]
        d = defer.Deferred()
        p = ProcessWithTimeout(d, self.timeout, clock=clock)
        p.spawnProcess(resume_argv[0], tuple(resume_argv))
        return d

    @defer.inlineCallbacks
    def sendFileToWorker(
        self, sha1, url, username="", password="", logger=None
    ):
        """Helper to send the file at 'url' with 'sha1' to this builder."""
        if logger is not None:
            logger.info(
                "Asking %s to ensure it has %s (%s%s)",
                self.url,
                sha1,
                url,
                " with auth" if username or password else "",
            )
        try:
            present, info = yield self.ensurepresent(
                sha1, url, username, password
            )
        except Exception as e:
            if logger is not None:
                logger.exception(
                    "Failed to ensure %s has %s (%s%s): %s",
                    self.url,
                    sha1,
                    url,
                    " with auth" if username or password else "",
                    str(e),
                )
            raise CannotFetchFile(url, str(e)) from e
        if not present:
            if logger is not None:
                logger.error(
                    "Failed to fetch %s (%s%s) on %s: %s",
                    sha1,
                    url,
                    " with auth" if username or password else "",
                    self.url,
                    info,
                )
            raise CannotFetchFile(url, info)

    def build(self, buildid, builder_type, chroot_sha1, filemap, args):
        """Build a thing on this build worker.

        :param buildid: A string identifying this build.
        :param builder_type: The type of builder needed.
        :param chroot_sha1: XXX
        :param filemap: A dictionary mapping from paths to SHA-1 hashes of
            the file contents.
        :param args: A dictionary of extra arguments. The contents depend on
            the build job type.
        """
        if isinstance(filemap, OrderedDict):
            filemap = dict(filemap)
        return self._with_timeout(
            self._server.callRemote(
                "build", buildid, builder_type, chroot_sha1, filemap, args
            )
        )


BuilderVitals = namedtuple(
    "BuilderVitals",
    (
        "name",
        "url",
        "processor_names",
        "virtualized",
        "vm_host",
        "vm_reset_protocol",
        "open_resources",
        "restricted_resources",
        "builderok",
        "manual",
        "build_queue",
        "version",
        "clean_status",
        "active",
        "failure_count",
        "region",
    ),
)

_BQ_UNSPECIFIED = object()


def extract_vitals_from_db(builder, build_queue=_BQ_UNSPECIFIED):
    if build_queue == _BQ_UNSPECIFIED:
        build_queue = builder.currentjob
    return BuilderVitals(
        builder.name,
        builder.url,
        [processor.name for processor in builder.processors],
        builder.virtualized,
        builder.vm_host,
        builder.vm_reset_protocol,
        builder.open_resources,
        builder.restricted_resources,
        builder.builderok,
        builder.manual,
        build_queue,
        builder.version,
        builder.clean_status,
        builder.active,
        builder.failure_count,
        builder.region,
    )


class BuilderInteractor:
    @staticmethod
    def makeWorkerFromVitals(vitals):
        if vitals.virtualized:
            timeout = config.builddmaster.virtualized_socket_timeout
        else:
            timeout = config.builddmaster.socket_timeout
        return BuilderWorker.makeBuilderWorker(
            vitals.url, vitals.vm_host, timeout
        )

    @staticmethod
    def getBuildBehaviour(queue_item, builder, worker):
        if queue_item is None:
            return None
        behaviour = IBuildFarmJobBehaviour(queue_item.specific_build)
        behaviour.setBuilder(builder, worker)
        return behaviour

    @classmethod
    def resumeWorkerHost(cls, vitals, worker):
        """Resume the worker host to a known good condition.

        Issues 'builddmaster.vm_resume_command' specified in the configuration
        to resume the worker.

        :raises: CannotResumeHost: if builder is not virtual or if the
            configuration command has failed.

        :return: A Deferred that fires when the resume operation finishes,
            whose value is a (stdout, stderr) tuple for success, or a Failure
            whose value is a CannotResumeHost exception.
        """
        if not vitals.virtualized:
            return defer.fail(CannotResumeHost("Builder is not virtualized."))

        if not vitals.vm_host:
            return defer.fail(CannotResumeHost("Undefined vm_host."))

        logger = cls._getWorkerScannerLogger()
        logger.info("Resuming %s (%s)" % (vitals.name, vitals.url))

        d = worker.resume()

        def got_resume_ok(args):
            stdout, stderr, returncode = args
            return stdout, stderr

        def got_resume_bad(failure):
            stdout, stderr, code = failure.value
            raise CannotResumeHost(
                "Resuming failed:\nOUT:\n%s\nERR:\n%s\n"
                % (six.ensure_str(stdout), six.ensure_str(stderr))
            )

        return d.addCallback(got_resume_ok).addErrback(got_resume_bad)

    @classmethod
    @defer.inlineCallbacks
    def cleanWorker(cls, vitals, worker, builder_factory):
        """Prepare a worker for a new build.

        :return: A Deferred that fires when this stage of the resume
            operations finishes. If the value is True, the worker is now clean.
            If it's False, the clean is still in progress and this must be
            called again later.
        """
        if vitals.virtualized:
            if vitals.vm_reset_protocol == BuilderResetProtocol.PROTO_1_1:
                # In protocol 1.1 the reset trigger is synchronous, so
                # once resumeWorkerHost returns the worker should be
                # running.
                builder_factory[vitals.name].setCleanStatus(
                    BuilderCleanStatus.CLEANING
                )
                transaction.commit()
                yield cls.resumeWorkerHost(vitals, worker)
                # We ping the resumed worker before we try to do anything
                # useful with it. This is to ensure it's accepting
                # packets from the outside world, because testing has
                # shown that the first packet will randomly fail for no
                # apparent reason.  This could be a quirk of the Xen
                # guest, we're not sure. See bug 586359.
                yield worker.echo("ping")
                return True
            elif vitals.vm_reset_protocol == BuilderResetProtocol.PROTO_2_0:
                # In protocol 2.0 the reset trigger is asynchronous.
                # If the trigger succeeds we'll leave the worker in
                # CLEANING, and the non-LP worker management code will
                # set it back to CLEAN later using the webservice.
                if vitals.clean_status == BuilderCleanStatus.DIRTY:
                    yield cls.resumeWorkerHost(vitals, worker)
                    builder_factory[vitals.name].setCleanStatus(
                        BuilderCleanStatus.CLEANING
                    )
                    transaction.commit()
                    logger = cls._getWorkerScannerLogger()
                    logger.info("%s is being cleaned.", vitals.name)
                return False
            raise CannotResumeHost(
                "Invalid vm_reset_protocol: %r" % vitals.vm_reset_protocol
            )
        else:
            worker_status = yield worker.status()
            status = worker_status.get("builder_status", None)
            if status == "BuilderStatus.IDLE":
                # This is as clean as we can get it.
                return True
            elif status == "BuilderStatus.BUILDING":
                # Asynchronously abort() the worker and wait until WAITING.
                yield worker.abort()
                return False
            elif status == "BuilderStatus.ABORTING":
                # Wait it out until WAITING.
                return False
            elif status == "BuilderStatus.WAITING":
                # Just a synchronous clean() call and we'll be idle.
                yield worker.clean()
                return True
            raise BuildDaemonError("Invalid status during clean: %r" % status)

    @classmethod
    @defer.inlineCallbacks
    def _startBuild(
        cls, build_queue_item, vitals, builder, worker, behaviour, logger
    ):
        """Start a build on this builder.

        :param build_queue_item: A BuildQueueItem to build.
        :param logger: A logger to be used to log diagnostic information.

        :return: A Deferred that fires after the dispatch has completed whose
            value is None, or a Failure that contains an exception
            explaining what went wrong.
        """
        behaviour.verifyBuildRequest(logger)

        # Set the build behaviour depending on the provided build queue item.
        if not builder.builderok:
            raise BuildDaemonIsolationError(
                "Attempted to start a build on a known-bad builder."
            )

        if builder.clean_status != BuilderCleanStatus.CLEAN:
            raise BuildDaemonIsolationError(
                "Attempted to start build on a dirty worker."
            )

        builder.setCleanStatus(BuilderCleanStatus.DIRTY)
        transaction.commit()

        yield behaviour.dispatchBuildToWorker(logger)

    @classmethod
    @defer.inlineCallbacks
    def findAndStartJob(cls, vitals, builder, worker, builder_factory):
        """Find a job to run and send it to the buildd worker.

        :return: A Deferred whose value is the `IBuildQueue` instance
            found or None if no job was found.
        """
        logger = cls._getWorkerScannerLogger()
        # Try a few candidates so that we make reasonable progress if we
        # have only a few idle builders but lots of candidates that fail
        # postprocessing due to old source publications or similar.  The
        # chance of a large prefix of the queue being bad candidates is
        # negligible, and we want reasonably bounded per-cycle performance
        # even if the prefix is large.
        for _ in range(10):
            candidate = builder_factory.acquireBuildCandidate(vitals, builder)
            if candidate is not None:
                if candidate.specific_source.postprocessCandidate(
                    candidate, logger
                ):
                    break
        else:
            logger.debug("No build candidates available for builder.")
            return None

        new_behaviour = cls.getBuildBehaviour(candidate, builder, worker)
        needed_bfjb = type(
            removeSecurityProxy(
                IBuildFarmJobBehaviour(candidate.specific_build)
            )
        )
        if not zope_isinstance(new_behaviour, needed_bfjb):
            raise AssertionError(
                "Inappropriate IBuildFarmJobBehaviour: %r is not a %r"
                % (new_behaviour, needed_bfjb)
            )
        yield cls._startBuild(
            candidate, vitals, builder, worker, new_behaviour, logger
        )
        return candidate

    @staticmethod
    def extractLogTail(worker_status):
        """Extract the log tail from a builder status response.

        :param worker_status: build status dict from BuilderWorker.status.
        :return: a text string representing the tail of the build log, or
            None if the log tail is unavailable and should be left
            unchanged.
        """
        builder_status = worker_status["builder_status"]
        if builder_status == "BuilderStatus.ABORTING":
            logtail = "Waiting for worker process to be terminated"
        elif worker_status.get("logtail") is not None:
            # worker_status["logtail"] is an xmlrpc.client.Binary instance,
            # and the contents might include invalid UTF-8 due to being a
            # fixed number of bytes from the tail of the log.  Turn it into
            # Unicode as best we can.
            logtail = worker_status.get("logtail").data.decode(
                "UTF-8", errors="replace"
            )
            # PostgreSQL text columns can't contain \0 characters, and since
            # we only use this for web UI display purposes there's no point
            # in going through contortions to store them.
            logtail = logtail.replace("\0", "")
        else:
            logtail = None
        return logtail

    @classmethod
    @defer.inlineCallbacks
    def updateBuild(
        cls,
        vitals,
        worker,
        worker_status,
        builder_factory,
        behaviour_factory,
        manager,
    ):
        """Verify the current build job status.

        Perform the required actions for each state.

        :return: A Deferred that fires when the worker dialog is finished.
        """
        # IDLE is deliberately not handled here, because it should be
        # impossible to get past the cookie check unless the worker
        # matches the DB, and this method isn't called unless the DB
        # says there's a job.
        builder_status = worker_status["builder_status"]
        if builder_status not in (
            "BuilderStatus.BUILDING",
            "BuilderStatus.ABORTING",
            "BuilderStatus.WAITING",
        ):
            raise AssertionError("Unknown status %s" % builder_status)
        builder = builder_factory[vitals.name]
        behaviour = behaviour_factory(vitals.build_queue, builder, worker)
        if builder_status in (
            "BuilderStatus.BUILDING",
            "BuilderStatus.ABORTING",
        ):
            logtail = cls.extractLogTail(worker_status)
            if logtail is not None:
                manager.addLogTail(vitals.build_queue.id, logtail)
        # Delegate the remaining handling to the build behaviour, which will
        # commit the transaction.
        yield behaviour.handleStatus(vitals.build_queue, worker_status)

    @staticmethod
    def _getWorkerScannerLogger():
        """Return the logger instance from lp.buildmaster.manager."""
        # XXX cprov 20071120: Ideally the Launchpad logging system
        # should be able to configure the root-logger instead of creating
        # a new object, then the logger lookups won't require the specific
        # name argument anymore. See bug 164203.
        logger = logging.getLogger("worker-scanner")
        return logger
