# Copyright 2009-2020 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "GPGHandler",
    "PymeKey",
    "PymeSignature",
    "PymeUserId",
]

import http.client
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlencode

import gpgme
import requests
from lazr.restful.utils import get_current_browser_request
from zope.component import getUtility
from zope.interface import implementer
from zope.security.proxy import removeSecurityProxy

from lp.app.validators.email import valid_email
from lp.services.config import config
from lp.services.features import getFeatureFlag
from lp.services.gpg.interfaces import (
    GPG_INJECT,
    GPGKeyAlgorithm,
    GPGKeyDoesNotExistOnServer,
    GPGKeyExpired,
    GPGKeyMismatchOnServer,
    GPGKeyNotFoundError,
    GPGKeyRevoked,
    GPGKeyTemporarilyNotFoundError,
    GPGUploadFailure,
    GPGVerificationError,
    IGPGHandler,
    IPymeKey,
    IPymeSignature,
    IPymeUserId,
    MoreThanOneGPGKeyFound,
    SecretGPGKeyImportDetected,
    get_gpg_home_directory,
    get_gpg_path,
    get_gpgme_context,
    gpg_algorithm_letter,
    valid_fingerprint,
)
from lp.services.signing.enums import SigningKeyType
from lp.services.signing.interfaces.signingkey import ISigningKeySet
from lp.services.timeline.requesttimeline import get_request_timeline
from lp.services.timeout import TimeoutError, urlfetch
from lp.services.webapp import errorlog

signing_only_param = """
<GnupgKeyParms format="internal">
  Key-Type: RSA
  Key-Usage: sign
  Key-Length: 4096
  Name-Real: %(name)s
  Expire-Date: 0
</GnupgKeyParms>
"""


@contextmanager
def gpgme_timeline(name, detail):
    request = get_current_browser_request()
    timeline = get_request_timeline(request)
    action = timeline.start("gpgme-%s" % name, detail, allow_nested=True)
    try:
        yield
    finally:
        action.finish()


@implementer(IGPGHandler)
class GPGHandler:
    """See IGPGHandler."""

    def sanitizeFingerprint(self, fingerprint):
        """See IGPGHandler."""
        return sanitize_fingerprint(fingerprint)

    def resetLocalState(self):
        """See IGPGHandler."""
        # Remove the public keyring, private keyring and the trust DB.
        home = get_gpg_home_directory()
        for filename in (
            "pubring.gpg",
            "pubring.kbx",
            "secring.gpg",
            "private-keys-v1.d",
            "trustdb.gpg",
        ):
            filename = os.path.join(home, filename)
            if os.path.exists(filename):
                if os.path.isdir(filename):
                    shutil.rmtree(filename)
                else:
                    os.remove(filename)
        # Kill any running gpg-agent for GnuPG 2
        if shutil.which("gpgconf"):
            # XXX cjwatson 2023-04-16: Ubuntu 16.04's gpgconf doesn't
            # support --homedir, so we have to set an environment variable.
            # This can be simplified once we require Ubuntu 20.04.
            env = dict(os.environ)
            env["GNUPGHOME"] = home
            subprocess.check_call(["gpgconf", "--kill", "gpg-agent"], env=env)

    def getVerifiedSignatureResilient(self, content, signature=None):
        """See IGPGHandler."""
        stored_errors = []

        for _ in range(3):
            try:
                signature = self.getVerifiedSignature(content, signature)
            except GPGKeyNotFoundError as info:
                stored_errors.append(str(info))
            else:
                return signature

        raise GPGVerificationError(
            "Verification failed 3 times: %s " % stored_errors
        )

    def _rawVerifySignature(self, ctx, content, signature=None):
        """Internals of `getVerifiedSignature`.

        This is called twice during a typical verification: once to work out
        the correct fingerprint, and once after retrieving the corresponding
        key from the keyserver.
        """
        # from `info gpgme` about gpgme_op_verify(SIG, SIGNED_TEXT, PLAIN):
        #
        # If SIG is a detached signature, then the signed text should be
        # provided in SIGNED_TEXT and PLAIN should be a null pointer.
        # Otherwise, if SIG is a normal (or cleartext) signature,
        # SIGNED_TEXT should be a null pointer and PLAIN should be a
        # writable data object that will contain the plaintext after
        # successful verification.

        if signature:
            # store detach-sig
            sig = BytesIO(signature)
            # store the content
            plain = BytesIO(content)
            args = (sig, plain, None)
            timeline_detail = "detached signature"
        else:
            # store clearsigned signature
            sig = BytesIO(content)
            # writeable content
            plain = BytesIO()
            args = (sig, None, plain)
            timeline_detail = "clear signature"

        # process it
        try:
            with gpgme_timeline("verify", timeline_detail):
                signatures = ctx.verify(*args)
        except gpgme.GpgmeError as e:
            error = GPGVerificationError(e.strerror)
            for attr in ("args", "code", "signatures", "source"):
                if hasattr(e, attr):
                    value = getattr(e, attr)
                    setattr(error, attr, value)
            raise error

        # XXX jamesh 2006-01-31:
        # We raise an exception if we don't get exactly one signature.
        # If we are verifying a clear signed document, multiple signatures
        # may indicate two differently signed sections concatenated
        # together.
        # Multiple signatures for the same signed block of data is possible,
        # but uncommon.  If people complain, we'll need to examine the issue
        # again.

        # if no signatures were found, raise an error:
        if len(signatures) == 0:
            raise GPGVerificationError("No signatures found")
        # we only expect a single signature:
        if len(signatures) > 1:
            raise GPGVerificationError(
                "Single signature expected, " "found multiple signatures"
            )

        return plain, signatures[0]

    def getVerifiedSignature(self, content, signature=None):
        """See IGPGHandler."""

        assert isinstance(content, bytes)
        assert signature is None or isinstance(signature, bytes)

        ctx = get_gpgme_context()

        # We may not yet have the public key, so find out the fingerprint we
        # need to fetch.
        _, sig = self._rawVerifySignature(ctx, content, signature=signature)

        # Fetch the full key from the keyserver now that we know its
        # fingerprint, and then verify the signature again.  (This also lets
        # us support subkeys by using the master key fingerprint.)
        # XXX cjwatson 2019-03-12: Before GnuPG 2.2.7 and GPGME 1.11.0,
        # sig.fpr is a 64-bit key ID in the case where the key isn't in the
        # local keyring yet.  I haven't yet heard of 64-bit key ID
        # collisions in the wild, but even if they happen here,
        # importPublicKey will raise MoreThanOneGPGKeyFound, so the worst
        # consequence is a denial of service for the owner of an affected
        # key.  If we do run into this, then the correct fix is to upgrade
        # GnuPG and GPGME.
        key = self.retrieveKey(sig.fpr)
        plain, sig = self._rawVerifySignature(
            ctx, content, signature=signature
        )

        expired = False
        # sig.status == 0 means "Ok"
        if sig.status is not None:
            if sig.status.code == gpgme.ERR_KEY_EXPIRED:
                expired = True
            else:
                raise GPGVerificationError(sig.status.args)

        if expired:
            # This should already be set, but let's make sure.
            key.expired = True
            raise GPGKeyExpired(key)

        # return the signature container
        return PymeSignature(
            fingerprint=key.fingerprint,
            plain_data=plain.getvalue(),
            timestamp=sig.timestamp,
        )

    def importPublicKey(self, content):
        """See IGPGHandler."""
        assert isinstance(content, bytes)
        context = get_gpgme_context()

        newkey = BytesIO(content)
        with gpgme_timeline("import", "new public key"):
            result = context.import_(newkey)

        if len(result.imports) == 0:
            raise GPGKeyNotFoundError(content)

        # Check the status of all imported keys to see if any of them is
        # a secret key.  We can't rely on result.secret_imported here
        # because if there's a secret key which is already imported,
        # result.secret_imported will be 0.
        for fingerprint, _, status in result.imports:
            if status & gpgme.IMPORT_SECRET != 0:
                raise SecretGPGKeyImportDetected(
                    "GPG key '%s' is a secret key." % fingerprint
                )

        if len(result.imports) > 1:
            raise MoreThanOneGPGKeyFound(
                "Found %d GPG keys when importing %s"
                % (len(result.imports), content)
            )

        fingerprint, res, status = result.imports[0]
        key = PymeKey(fingerprint)
        assert key.exists_in_local_keyring
        return key

    def importSecretKey(self, content):
        """See `IGPGHandler`."""
        assert isinstance(content, bytes)

        # Make sure that gpg-agent doesn't interfere.
        if "GPG_AGENT_INFO" in os.environ:
            del os.environ["GPG_AGENT_INFO"]

        context = get_gpgme_context()

        def passphrase_cb(uid_hint, passphrase_info, prev_was_bad, fd):
            os.write(fd, b"\n")

        context.passphrase_cb = passphrase_cb
        newkey = BytesIO(content)
        with gpgme_timeline("import", "new secret key"):
            import_result = context.import_(newkey)

        secret_imports = [
            fingerprint
            for fingerprint, result, status in import_result.imports
            if status & gpgme.IMPORT_SECRET
        ]
        if len(secret_imports) != 1:
            raise MoreThanOneGPGKeyFound(
                "Found %d secret GPG keys when importing %s"
                % (len(secret_imports), content)
            )

        fingerprint, result, status = import_result.imports[0]
        try:
            key = context.get_key(fingerprint, True)
        except gpgme.GpgmeError:
            return None

        key = PymeKey.newFromGpgmeKey(key)
        assert key.exists_in_local_keyring
        return key

    def _injectKeyPair(self, key):
        """Inject a key pair into the signing service."""
        secret_key = key.export()
        public_key = self.retrieveKey(key.fingerprint).export()
        now = datetime.now().replace(tzinfo=timezone.utc)
        getUtility(ISigningKeySet).inject(
            SigningKeyType.OPENPGP,
            secret_key,
            public_key,
            key.uids[0].name,
            now,
        )

    def generateKey(self, name, logger=None):
        """See `IGPGHandler`."""
        context = get_gpgme_context()

        # Make sure that gpg-agent doesn't interfere.
        if "GPG_AGENT_INFO" in os.environ:
            del os.environ["GPG_AGENT_INFO"]

        def passphrase_cb(uid_hint, passphrase_info, prev_was_bad, fd):
            os.write(fd, b"\n")

        context.passphrase_cb = passphrase_cb

        # Only 'utf-8' encoding is supported by gpgme.
        # See more information at:
        # http://pyme.sourceforge.net/doc/gpgme/Generating-Keys.html
        with gpgme_timeline("genkey", name):
            result = context.genkey(signing_only_param % {"name": name})

        # Right, it might seem paranoid to have this many assertions,
        # but we have to take key generation very seriously.
        assert result.primary, "Secret key generation failed."
        assert (
            not result.sub
        ), "Only sign-only RSA keys are safe to be generated"

        secret_keys = list(self.localKeys(result.fpr, secret=True))

        assert len(secret_keys) == 1, "Found %d secret GPG keys for %s" % (
            len(secret_keys),
            result.fpr,
        )

        key = secret_keys[0]

        assert (
            key.fingerprint == result.fpr
        ), "The key in the local keyring does not match the one generated."
        assert (
            key.exists_in_local_keyring
        ), "The key does not seem to exist in the local keyring."

        if getFeatureFlag(GPG_INJECT):
            if logger is not None:
                logger.info(
                    "Injecting key_type %s '%s' into signing service",
                    SigningKeyType.OPENPGP,
                    name,
                )
            try:
                self._injectKeyPair(key)
            except Exception:
                with gpgme_timeline("delete", key.fingerprint):
                    # For clarity this should be allow_secret=True, but
                    # pygpgme doesn't allow it to be passed as a keyword
                    # argument.
                    context.delete(key.key, True)
                raise

        return key

    def encryptContent(self, content, key):
        """See IGPGHandler."""
        if not isinstance(content, bytes):
            raise TypeError("Content must be bytes.")

        ctx = get_gpgme_context()

        # setup containers
        plain = BytesIO(content)
        cipher = BytesIO()

        if key.key is None:
            return None

        if not key.can_encrypt:
            raise ValueError(
                "key %s can not be used for encryption" % key.fingerprint
            )

        # encrypt content
        with gpgme_timeline("encrypt", key.fingerprint):
            ctx.encrypt(
                [removeSecurityProxy(key.key)],
                gpgme.ENCRYPT_ALWAYS_TRUST,
                plain,
                cipher,
            )

        return cipher.getvalue()

    def signContent(self, content, key, password="", mode=None):
        """See IGPGHandler."""
        if not isinstance(content, bytes):
            raise TypeError("Content must be bytes.")

        if mode is None:
            mode = gpgme.SIG_MODE_CLEAR

        # Find the key and make it the only one allowed to sign content
        # during this session.
        context = get_gpgme_context()
        context.signers = [removeSecurityProxy(key.key)]

        # Set up containers.
        plaintext = BytesIO(content)
        signature = BytesIO()

        # Make sure that gpg-agent doesn't interfere.
        if "GPG_AGENT_INFO" in os.environ:
            del os.environ["GPG_AGENT_INFO"]

        def passphrase_cb(uid_hint, passphrase_info, prev_was_bad, fd):
            os.write(fd, ("%s\n" % password).encode())

        context.passphrase_cb = passphrase_cb

        # Sign the text.
        try:
            with gpgme_timeline("sign", key.fingerprint):
                context.sign(plaintext, signature, mode)
        except gpgme.GpgmeError:
            return None

        return signature.getvalue()

    def localKeys(self, filter=None, secret=False):
        """Get an iterator of the keys this gpg handler
        already knows about.
        """
        ctx = get_gpgme_context()

        # XXX michaeln 2010-05-07 bug=576405
        # Currently gpgme.Context().keylist fails if passed a unicode
        # string even though that's what is returned for fingerprints.
        if isinstance(filter, str):
            filter = filter.encode("utf-8")

        with gpgme_timeline(
            "keylist", "filter: %r, secret: %r" % (filter, secret)
        ):
            for key in ctx.keylist(filter, secret):
                yield PymeKey.newFromGpgmeKey(key)

    def retrieveKey(self, fingerprint):
        """See IGPGHandler."""
        # XXX cprov 2005-07-05:
        # Integrate it with the future proposal related
        # synchronization of the local key ring with the
        # global one. It should basically consists of be
        # aware of a revoked flag coming from the global
        # key ring, but it needs "speccing"
        key = PymeKey(fingerprint)
        if not key.exists_in_local_keyring:
            pubkey = self._getPubKey(fingerprint)
            key = self.importPublicKey(pubkey)
            if not key.matches(fingerprint):
                ctx = get_gpgme_context()
                with gpgme_timeline("delete", key.fingerprint):
                    ctx.delete(key.key)
                raise GPGKeyMismatchOnServer(fingerprint, key.fingerprint)
        return key

    def retrieveActiveKey(self, fingerprint):
        """See `IGPGHandler`."""
        key = self.retrieveKey(fingerprint)
        if key.revoked:
            raise GPGKeyRevoked(key)
        if key.expired:
            raise GPGKeyExpired(key)
        return key

    def submitKey(self, content):
        """See `IGPGHandler`."""
        keyserver_http_url = "%s:%s" % (
            config.gpghandler.host,
            config.gpghandler.port,
        )

        conn = http.client.HTTPConnection(keyserver_http_url)
        params = urlencode({"keytext": content})
        headers = {
            "Content-type": "application/x-www-form-urlencoded",
            "Accept": "text/plain",
        }

        try:
            conn.request("POST", "/pks/add", params, headers)
        except OSError as err:
            raise GPGUploadFailure(
                "Could not reach keyserver at http://%s %s"
                % (keyserver_http_url, str(err))
            )

        assert (
            conn.getresponse().status == http.client.OK
        ), "Keyserver POST failed"

        conn.close()

    def uploadPublicKey(self, fingerprint, logger=None):
        """See IGPGHandler"""
        if not config.gpghandler.upload_keys:
            if logger is not None:
                logger.info(
                    "Not submitting key to keyserver "
                    "(disabled in configuration)."
                )
            return

        pub_key = self.retrieveKey(fingerprint)
        self.submitKey(pub_key.export())

    def getURLForKeyInServer(self, fingerprint, action="index", public=False):
        """See IGPGHandler"""
        params = {
            "op": action,
            "search": "0x%s" % fingerprint,
            "fingerprint": "on",
        }
        if public:
            host = config.gpghandler.public_host
        else:
            host = config.gpghandler.host
        if public and config.gpghandler.public_https:
            base = "https://%s" % host
        else:
            base = "http://%s:%s" % (host, config.gpghandler.port)
        return "%s/pks/lookup?%s" % (base, urlencode(sorted(params.items())))

    def _getPubKey(self, fingerprint):
        """See IGPGHandler for further information."""
        request = get_current_browser_request()
        timeline = get_request_timeline(request)
        action = timeline.start(
            "retrieving GPG key",
            "Fingerprint: %s" % fingerprint,
            allow_nested=True,
        )
        try:
            return self._grabPage("get", fingerprint)
        # We record an OOPS for most errors: If the keyserver does not
        # respond, callsites should show users an error message like
        # "sorry, the keyserver is not responding, try again in a few
        # minutes." The details of the error do not matter for users
        # (and for the code in callsites), but we should be able to see
        # if this problem occurs too often.
        except requests.HTTPError as exc:
            # Old versions of SKS return a 500 error when queried for a
            # non-existent key. Production was upgraded in 2013/01, but
            # let's leave this here for a while.
            #
            # We can extract the fact that the key is unknown by looking
            # into the response's content.
            if exc.response.status_code in (404, 500):
                no_key_message = b"No results found: No keys found"
                if exc.response.content.find(no_key_message) >= 0:
                    raise GPGKeyDoesNotExistOnServer(fingerprint)
                errorlog.globalErrorUtility.raising(sys.exc_info(), request)
                raise GPGKeyTemporarilyNotFoundError(fingerprint)
            raise
        except (TimeoutError, requests.RequestException):
            errorlog.globalErrorUtility.raising(sys.exc_info(), request)
            raise GPGKeyTemporarilyNotFoundError(fingerprint)
        finally:
            action.finish()

    def _grabPage(self, action, fingerprint):
        """Wrapper to collect KeyServer Pages."""
        url = self.getURLForKeyInServer(fingerprint, action)
        kwargs = {}
        if config.gpghandler.http_proxy:
            kwargs["proxies"] = {
                "http": config.gpghandler.http_proxy,
                "https": config.gpghandler.http_proxy,
            }
        with urlfetch(url, **kwargs) as response:
            return response.content


@implementer(IPymeSignature)
class PymeSignature:
    """See IPymeSignature."""

    def __init__(self, fingerprint=None, plain_data=None, timestamp=None):
        """Initialized a signature container."""
        self.fingerprint = fingerprint
        self.plain_data = plain_data
        self.timestamp = timestamp


@implementer(IPymeKey)
class PymeKey:
    """See IPymeKey."""

    fingerprint = None
    exists_in_local_keyring = False

    def __init__(self, fingerprint):
        """Initialize a key container."""
        if fingerprint:
            self._buildFromFingerprint(fingerprint)

    @classmethod
    def newFromGpgmeKey(cls, key):
        """Initialize a PymeKey from a gpgme_key_t instance."""
        self = cls(None)
        self._buildFromGpgmeKey(key)
        return self

    def _buildFromFingerprint(self, fingerprint):
        """Build key information from a fingerprint."""
        context = get_gpgme_context()
        # retrieve additional key information
        try:
            with gpgme_timeline("get-key", fingerprint):
                key = context.get_key(fingerprint, False)
        except gpgme.GpgmeError:
            key = None

        if key and valid_fingerprint(key.subkeys[0].fpr):
            self._buildFromGpgmeKey(key)

    def _buildFromGpgmeKey(self, key):
        self.exists_in_local_keyring = True
        self.key = key
        subkey = key.subkeys[0]
        self.fingerprint = subkey.fpr
        self.revoked = subkey.revoked
        self.keysize = subkey.length

        self.algorithm = GPGKeyAlgorithm.items[subkey.pubkey_algo]
        self.keyid = self.fingerprint[-8:]
        self.expired = key.expired
        self.secret = key.secret
        self.owner_trust = key.owner_trust
        self.can_encrypt = key.can_encrypt
        self.can_sign = key.can_sign
        self.can_certify = key.can_certify
        self.can_authenticate = key.can_authenticate

        self.uids = [PymeUserId(uid) for uid in key.uids]

        # Non-revoked valid email addresses associated with this key
        self.emails = [
            uid.email
            for uid in self.uids
            if valid_email(uid.email) and not uid.revoked
        ]

    @property
    def displayname(self):
        return "%s%s/%s" % (
            self.keysize,
            gpg_algorithm_letter(self.algorithm),
            self.fingerprint,
        )

    def export(self, secret_passphrase=""):
        """See `IPymeKey`."""
        if self.secret:
            # XXX cprov 20081014: gpgme_op_export() only supports public keys.
            # See http://www.fifi.org/cgi-bin/info2www?(gpgme)Exporting+Keys
            return subprocess.run(
                [
                    get_gpg_path(),
                    "--homedir",
                    get_gpg_home_directory(),
                    "--export-secret-keys",
                    "-a",
                    "--passphrase-fd",
                    "0",
                    self.fingerprint,
                ],
                input=secret_passphrase.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            ).stdout

        context = get_gpgme_context()
        keydata = BytesIO()
        with gpgme_timeline("export", self.fingerprint):
            context.export(self.fingerprint.encode("ascii"), keydata)

        return keydata.getvalue()

    def matches(self, fingerprint):
        """See `IPymeKey`."""
        for subkey in self.key.subkeys:
            if fingerprint == subkey.fpr:
                return True
            # XXX cjwatson 2019-03-13: Remove affordance for 64-bit key IDs
            # once we're on GnuPG 2.2.7 and GPGME 1.11.0.  See comment in
            # getVerifiedSignature.
            if len(fingerprint) == 16 and subkey.fpr.endswith(fingerprint):
                return True
        return False


@implementer(IPymeUserId)
class PymeUserId:
    """See IPymeUserId"""

    def __init__(self, uid):
        self.revoked = uid.revoked
        self.invalid = uid.invalid
        self.validity = uid.validity
        self.uid = uid.uid
        self.name = uid.name
        self.email = uid.email
        self.comment = uid.comment


def sanitize_fingerprint(fingerprint):
    """Sanitize a GPG fingerprint.

    This is the ultimate implementation of IGPGHandler.sanitizeFingerprint.
    """
    # remove whitespaces, truncate to max of 40 (as per v4 keys) and
    # convert to upper case
    fingerprint = fingerprint.replace(" ", "")
    fingerprint = fingerprint[:40].upper()

    if not valid_fingerprint(fingerprint):
        return None

    return fingerprint
