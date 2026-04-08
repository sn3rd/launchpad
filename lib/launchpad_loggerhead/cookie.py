# Copyright 2026 Canonical
# Copyright 2007 Pallets
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""
Modified version of cookie.py from the secure-cookie library.
"""

import base64
import hashlib
import hmac
import pickle
import urllib.parse


class LaunchpadSecureCookie(dict):
    """A dict-based signed cookie session."""

    hash_method = hashlib.sha256

    def __init__(self, data=None, secret_key=None):
        super().__init__(data or {})
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8", "replace")
        self.secret_key = secret_key

    @classmethod
    def _quote(cls, value):
        return base64.b64encode(pickle.dumps(value, protocol=2))

    @classmethod
    def _unquote(cls, value):
        return pickle.loads(base64.b64decode(value))

    def serialize(self):
        if self.secret_key is None:
            raise RuntimeError("no secret key defined")

        result = []
        mac = hmac.new(self.secret_key, None, self.hash_method)

        for key, value in sorted(self.items()):
            item = "{}={}".format(
                urllib.parse.quote_plus(key),
                self._quote(value).decode("ascii"),
            ).encode("ascii")
            result.append(item)
            mac.update(b"|" + item)

        return b"?".join(
            [base64.b64encode(mac.digest()).strip(), b"&".join(result)]
        ).decode("ascii")

    @classmethod
    def unserialize(cls, string, secret_key):
        if isinstance(string, str):
            string = string.encode("utf-8", "replace")
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8", "replace")

        try:
            base64_hash, data = string.split(b"?", 1)
        except (ValueError, IndexError):
            items = ()
        else:
            items = {}
            mac = hmac.new(secret_key, None, cls.hash_method)

            for item in data.split(b"&"):
                mac.update(b"|" + item)

                if b"=" not in item:
                    items = None
                    break

                key, value = item.split(b"=", 1)
                key = urllib.parse.unquote_plus(key.decode("ascii"))
                items[key] = value

            try:
                client_hash = base64.b64decode(base64_hash)
            except Exception:
                items = client_hash = None

            if items is not None and hmac.compare_digest(
                client_hash, mac.digest()
            ):
                try:
                    for key, value in items.items():
                        items[key] = cls._unquote(value)
                except Exception:
                    items = ()
            else:
                items = ()

        return cls(items, secret_key)
