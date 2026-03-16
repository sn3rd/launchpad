# Copyright 2009 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Generic XML-RPC helpers."""

__all__ = [
    "return_fault",
]

from xmlrpc.client import Fault  # nosec B411

from twisted.python.util import mergeFunctionMetadata


def return_fault(function):
    """Catch any Faults raised by 'function' and return them instead."""

    def decorated(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Fault as fault:
            return fault

    return mergeFunctionMetadata(function, decorated)
