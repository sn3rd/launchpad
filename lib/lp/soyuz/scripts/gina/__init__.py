# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import signal
from subprocess import PIPE, STDOUT, Popen

from lp.services.scripts import log


class ExecutionError(Exception):
    """The command executed in a call() returned a non-zero status"""


def subprocess_setup():
    # Python installs a SIGPIPE handler by default. This is usually not what
    # non-Python subprocesses expect.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def call(cmd, stdout_file=None):
    """Run a command, raising an ExecutionError if the command failed.

    :param cmd: Command as a list of arguments (no shell interpretation).
    :param stdout_file: Optional file object to redirect stdout to.
    """
    log.debug("Running %s" % cmd)
    p = Popen(
        cmd,
        stdin=PIPE,
        stdout=stdout_file or PIPE,
        stderr=PIPE if stdout_file else STDOUT,
        preexec_fn=subprocess_setup,
    )
    out, err = p.communicate()
    if out:
        for line in out.splitlines():
            log.debug("> %s" % line)
    if p.returncode != 0:
        raise ExecutionError("Error %d running %s" % (p.returncode, cmd))
    return p.returncode
