# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Implementations of the XML-RPC APIs for Git."""

__all__ = [
    "GitAPI",
]

import logging
import sys
import uuid
import xmlrpc.client  # nosec B411
from urllib.parse import quote

import six
import transaction
from pymacaroons import Macaroon
from zope.component import getUtility
from zope.error.interfaces import IErrorReportingUtility
from zope.interface import implementer
from zope.interface.interfaces import ComponentLookupError
from zope.security.interfaces import Unauthorized
from zope.security.proxy import removeSecurityProxy

from lp.app.errors import NameLookupFailed
from lp.app.validators import LaunchpadValidationError
from lp.code.enums import (
    GitGranteeType,
    GitPermissionType,
    GitRepositoryStatus,
    GitRepositoryType,
)
from lp.code.errors import (
    GitRepositoryCreationException,
    GitRepositoryCreationFault,
    GitRepositoryCreationForbidden,
    GitRepositoryExists,
    InvalidNamespace,
)
from lp.code.interfaces.branchmergeproposal import IBranchMergeProposalGetter
from lp.code.interfaces.codehosting import (
    LAUNCHPAD_ANONYMOUS,
    LAUNCHPAD_SERVICES,
)
from lp.code.interfaces.gitapi import IGitAPI
from lp.code.interfaces.gitjob import IGitRefScanJobSource
from lp.code.interfaces.gitlookup import IGitLookup, IGitTraverser
from lp.code.interfaces.gitnamespace import (
    get_git_namespace,
    split_git_unique_name,
)
from lp.code.interfaces.gitrepository import IGitRepositorySet
from lp.code.xmlrpc.codehosting import run_with_login
from lp.registry.errors import InvalidName, NoSuchSourcePackageName
from lp.registry.interfaces.person import IPersonSet, NoSuchPerson
from lp.registry.interfaces.product import InvalidProductName, NoSuchProduct
from lp.registry.interfaces.sourcepackagename import ISourcePackageNameSet
from lp.services.auth.enums import AccessTokenScope
from lp.services.auth.interfaces import IAccessTokenSet
from lp.services.features import getFeatureFlag
from lp.services.identity.interfaces.account import AccountStatus
from lp.services.macaroons.interfaces import (
    NO_USER,
    IMacaroonIssuer,
    IMacaroonVerificationResult,
)
from lp.services.webapp import LaunchpadXMLRPCView, canonical_url
from lp.services.webapp.authorization import check_permission
from lp.services.webapp.errorlog import ScriptRequest
from lp.xmlrpc import faults
from lp.xmlrpc.helpers import return_fault

GIT_ASYNC_CREATE_REPO = "git.codehosting.async-create.enabled"


def _get_requester_id(auth_params):
    """Get the requester ID from authentication parameters.

    The pack frontend layer authenticates using either the authserver (SSH)
    or `GitAPI.authenticateWithPassword` (HTTP), and then sends a
    corresponding dictionary of authentication parameters to other methods.
    For a real user, it sends a "uid" item with the person's ID; for
    internal services, it sends "user": "+launchpad-services"; for anonymous
    requests, it sends neither.
    """
    requester_id = auth_params.get("uid")
    if requester_id is not None:
        return requester_id
    # We never need to identify other users by name, so limit the "user"
    # item to just internal services.
    if auth_params.get("user") == LAUNCHPAD_SERVICES:
        return LAUNCHPAD_SERVICES
    else:
        return LAUNCHPAD_ANONYMOUS


def _can_internal_issuer_write(verified):
    """Does this internal-only issuer have write access?

    Some macaroons used by internal services are intended for writing to the
    repository; others only allow read access.

    :param verified: An `IMacaroonVerificationResult`.
    """
    return verified.issuer_name == "code-import-job"


class GitLoggerAdapter(logging.LoggerAdapter):
    """A logger adapter that adds request-id information."""

    def process(self, msg, kwargs):
        if self.extra is not None and self.extra.get("request-id"):
            msg = "[request-id=%s] %s" % (self.extra["request-id"], msg)
        return msg, kwargs


@implementer(IMacaroonVerificationResult)
class AccessTokenVerificationResult:
    def __init__(self, token):
        self.token = token

    @property
    def issuer_name(self):
        return None

    @property
    def user(self):
        return self.token.owner

    @property
    def can_pull(self):
        return AccessTokenScope.REPOSITORY_PULL in self.token.scopes

    @property
    def can_push(self):
        return AccessTokenScope.REPOSITORY_PUSH in self.token.scopes


@implementer(IGitAPI)
class GitAPI(LaunchpadXMLRPCView):
    """See `IGitAPI`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.repository_set = getUtility(IGitRepositorySet)

    def _verifyMacaroon(self, macaroon_raw, repository=None, user=None):
        try:
            macaroon = Macaroon.deserialize(macaroon_raw)
        # XXX cjwatson 2019-04-23: Restrict exceptions once
        # https://github.com/ecordell/pymacaroons/issues/50 is fixed.
        except Exception:
            return False
        try:
            issuer = getUtility(IMacaroonIssuer, macaroon.identifier)
        except ComponentLookupError:
            return False
        verified = issuer.verifyMacaroon(
            macaroon, repository, require_context=False, user=user
        )
        if verified:
            # Double-check user verification to prevent accidents.  Internal
            # macaroons may only be used by internal services, and user
            # macaroons may only be used by the corresponding real user.
            if user is None:
                if verified.user is not NO_USER:
                    raise faults.Unauthorized()
            else:
                if verified.user != user:
                    raise faults.Unauthorized()
        return verified

    def _verifyAccessToken(
        self, user, secret=None, token_id=None, repository=None
    ):
        access_token_set = removeSecurityProxy(getUtility(IAccessTokenSet))
        if secret is not None:
            assert token_id is None
            access_token = access_token_set.getBySecret(secret)
        else:
            assert token_id is not None
            try:
                # turnip sends authentication parameters as strings.
                # Convert this back.
                token_id = int(token_id)
            except ValueError:
                return None
            access_token = access_token_set.getByID(token_id)
        if access_token is None:
            return None
        if (
            access_token.is_expired
            or access_token.owner != user
            or access_token.owner.account_status != AccountStatus.ACTIVE
        ):
            raise faults.Unauthorized()
        if repository is not None and (
            access_token.target != repository
            and access_token.target != repository.target
        ):
            raise faults.Unauthorized()
        access_token.updateLastUsed()
        return AccessTokenVerificationResult(access_token)

    def _verifyAuthParams(self, requester, repository, auth_params):
        """Verify authentication parameters in the context of a repository.

        There are several possibilities:

         * Anonymous authentication with no macaroon or access token.  We do
           no additional checks here.
         * Anonymous authentication with a macaroon or access token.  This
           is forbidden.
         * User authentication with no macaroon or access token.  We can
           only get here if something else has already verified user
           authentication (SSH with a key checked against the authserver, or
           `authenticateWithPassword`); we do no additional checks beyond
           that.
         * User authentication with a macaroon or access token.  As above,
           we can only get here if something else has already verified user
           authentication.  In this case, the macaroon or access token is
           required to match the requester, and constrains their
           permissions.
         * Internal-services authentication with a macaroon.  In this case,
           we require that the macaroon does not identify a user.
         * Internal-services authentication with no macaroon or with an
           access token.  This is forbidden.

        :param requester: The logged-in `IPerson`, `LAUNCHPAD_SERVICES`, or
            None for anonymous access.
        :param repository: The context `IGitRepository`.
        :param auth_params: A dictionary of authentication parameters.
        :return: An `IMacaroonVerificationResult` if macaroon authentication
            was used, otherwise None.
        :raises faults.Unauthorized: if the authentication parameters are
            not sufficient to grant access to the repository for this
            requester.
        """
        macaroon_raw = auth_params.get("macaroon")
        if macaroon_raw is not None:
            if requester is None:
                raise faults.Unauthorized()
            verify_user = (
                None if requester == LAUNCHPAD_SERVICES else requester
            )
            verified = self._verifyMacaroon(
                macaroon_raw, removeSecurityProxy(repository), user=verify_user
            )
            if not verified:
                # Macaroon authentication failed.  Don't fall back to the
                # requester's permissions, since macaroons typically have
                # additional constraints.  Instead, just return
                # "authorisation required", thus preventing probing for the
                # existence of repositories without presenting valid
                # credentials.
                raise faults.Unauthorized()
            # _verifyMacaroon checks that any user indicated by the macaroon
            # matches the requester.
            return verified
        elif requester == LAUNCHPAD_SERVICES:
            # Internal services must authenticate using a macaroon.
            raise faults.Unauthorized()

        access_token_id = auth_params.get("access-token")
        if access_token_id is not None:
            if requester is None:
                raise faults.Unauthorized()
            verified = self._verifyAccessToken(
                requester, token_id=access_token_id, repository=repository
            )
            if verified is None:
                # Access token authentication failed.  Don't fall back to
                # the requester's permission, since an access token is
                # typically supposed to convey additional constraints.
                # Instead, just return "authorisation required", thus
                # preventing probing for the existence of repositories
                # without presenting valid credentials.
                raise faults.Unauthorized()
            # _verifyAccessToken checks that the access token's owner
            # matches the requester.
            return verified

    def _isReadable(self, requester, repository, verified):
        # Most authentication methods allow readability.
        readable = True
        if isinstance(verified, AccessTokenVerificationResult):
            # Access token authentication only allows readability with the
            # "repository:pull" scope.
            readable = verified.can_pull
        return readable

    def _isWritable(self, requester, repository, verified):
        writable = False
        naked_repository = removeSecurityProxy(repository)
        if verified is not None and verified.user is NO_USER:
            # We have verified that the authentication parameters correctly
            # specify internal-services authentication with a suitable
            # macaroon that specifically grants access to this repository.
            # This is only permitted for macaroons not bound to a user.
            writable = _can_internal_issuer_write(verified)
        elif (
            isinstance(verified, AccessTokenVerificationResult)
            and not verified.can_push
        ):
            # The user authenticated with an access token without the
            # "repository:push" scope, so pushing isn't allowed no matter
            # what permissions they might ordinarily have.
            writable = False
        elif repository.repository_type != GitRepositoryType.HOSTED:
            # Normal users can never push to non-hosted repositories.
            writable = False
        else:
            # This isn't an authorised internal service, so perform normal
            # user authorisation.
            writable = check_permission("launchpad.Edit", repository)
            if not writable:
                grants = naked_repository.findRuleGrantsByGrantee(requester)
                if not grants.is_empty():
                    writable = True
        return writable

    def _performLookup(self, requester, path, auth_params):
        """Perform a translation path lookup.

        :return: A tuple with the repository object and a dict with
                 translation information."""
        # Skip permission checks on intermediate URL segments if
        # internal-services authentication was requested, since in that case
        # they'll never succeed for elements of private pillars.  This is
        # safe because internal services can only authenticate using
        # macaroons issued for a specific repository.
        check_permissions = requester != LAUNCHPAD_SERVICES
        repository, extra_path = getUtility(IGitLookup).getByPath(
            path, check_permissions=check_permissions
        )
        if repository is None:
            return None, None

        verified = self._verifyAuthParams(requester, repository, auth_params)
        naked_repository = removeSecurityProxy(repository)

        if verified is not None and verified.user is NO_USER:
            # We have verified that the authentication parameters correctly
            # specify internal-services authentication with a suitable
            # macaroon that specifically grants access to this repository,
            # so we can bypass other checks.  This is only permitted for
            # macaroons not bound to a user.
            hosting_path = naked_repository.getInternalPath()
            private = naked_repository.private
        else:
            # This isn't an authorised internal service, so perform normal
            # user authorisation.
            try:
                hosting_path = repository.getInternalPath()
            except Unauthorized:
                return naked_repository, None
            private = repository.private
        readable = self._isReadable(requester, repository, verified)
        writable = self._isWritable(requester, repository, verified)

        return naked_repository, {
            "path": hosting_path,
            "readable": readable,
            "writable": writable,
            "trailing": extra_path,
            "private": private,
        }

    def _getGitNamespaceExtras(self, path, requester):
        """Get the namespace, repository name, and callback for the path.

        If the path defines a full Git repository path including the owner
        and repository name, then the namespace that is returned is the
        namespace for the owner and the repository target specified.

        If the path uses a shortcut name, then we only allow the requester
        to create a repository if they have permission to make the newly
        created repository the default for the shortcut target.  If there is
        an existing default repository, then GitRepositoryExists is raised.
        The repository name that is used is determined by the namespace as
        the first unused name starting with the leaf part of the namespace
        name.  In this case, the repository owner will be set to the
        namespace owner, and distribution source package namespaces are
        currently disallowed due to the complexities of ownership there.
        """
        try:
            namespace_name, repository_name = split_git_unique_name(path)
        except InvalidNamespace:
            namespace_name = path
            repository_name = None
        owner, target, repository = getUtility(IGitTraverser).traverse_path(
            namespace_name
        )
        # split_git_unique_name should have left us without a repository name.
        assert repository is None
        if owner is None:
            default_namespace = get_git_namespace(target, None)
            if (
                not default_namespace.allow_push_to_set_default
                or default_namespace.default_owner is None
            ):
                raise GitRepositoryCreationForbidden(
                    "Cannot automatically set the default repository for this "
                    "target; push to a named repository instead."
                )
            repository_owner = default_namespace.default_owner
        else:
            repository_owner = owner
        namespace = get_git_namespace(target, repository_owner)
        if repository_name is None and not namespace.has_defaults:
            raise InvalidNamespace(path)
        if repository_name is None:
            repository_name = namespace.findUnusedName(target.name)
            target_default = owner is None
            owner_default = (
                owner is None
                or self.repository_set.getDefaultRepositoryForOwner(
                    repository_owner, target
                )
                is None
            )
            return namespace, repository_name, target_default, owner_default
        else:
            return namespace, repository_name, False, False

    def _reportError(self, path, exception, hosting_path=None):
        properties = [
            ("path", path),
            ("error-explanation", str(exception)),
        ]
        if hosting_path is not None:
            properties.append(("hosting_path", hosting_path))
        request = ScriptRequest(properties)
        getUtility(IErrorReportingUtility).raising(sys.exc_info(), request)
        raise faults.OopsOccurred("creating a Git repository", request.oopsid)

    def _getLogger(self, request_id=None):
        # XXX cjwatson 2019-10-16: Ideally we'd always have a request-id,
        # but since that isn't yet the case, generate one if necessary.
        if request_id is None:
            request_id = uuid.uuid4()
        return GitLoggerAdapter(
            logging.getLogger(__name__), {"request-id": request_id}
        )

    def _createRepository(self, requester, path, clone_from=None):
        try:
            (
                namespace,
                repository_name,
                target_default,
                owner_default,
            ) = self._getGitNamespaceExtras(path, requester)
        except InvalidNamespace:
            raise faults.PermissionDenied(
                "'%s' is not a valid Git repository path." % path
            )
        except NoSuchPerson as e:
            raise faults.NotFound("User/team '%s' does not exist." % e.name)
        except (NoSuchProduct, InvalidProductName) as e:
            raise faults.NotFound("Project '%s' does not exist." % e.name)
        except NoSuchSourcePackageName as e:
            try:
                getUtility(ISourcePackageNameSet).new(e.name)
            except InvalidName:
                raise faults.InvalidSourcePackageName(e.name)
            return self._createRepository(requester, path)
        except NameLookupFailed as e:
            raise faults.NotFound(str(e))
        except GitRepositoryCreationForbidden as e:
            raise faults.PermissionDenied(str(e))

        try:
            try:
                # Creates the repository on the database, but do not create
                # it on hosting service. It should be created by the hosting
                # service as a result of pathTranslate call indicating that
                # the repo was just created in Launchpad.
                if getFeatureFlag(GIT_ASYNC_CREATE_REPO):
                    with_hosting = False
                    status = GitRepositoryStatus.CREATING
                else:
                    with_hosting = True
                    status = GitRepositoryStatus.AVAILABLE
                namespace.createRepository(
                    GitRepositoryType.HOSTED,
                    requester,
                    repository_name,
                    target_default=target_default,
                    owner_default=owner_default,
                    with_hosting=with_hosting,
                    status=status,
                )
            except LaunchpadValidationError as e:
                # Despite the fault name, this just passes through the
                # exception text so there's no need for a new Git-specific
                # fault.
                raise faults.InvalidBranchName(e)
            except GitRepositoryExists as e:
                # We should never get here, as we just tried to translate
                # the path and found nothing (not even an inaccessible
                # private repository).  Log an OOPS for investigation.
                self._reportError(path, e)
            except (GitRepositoryCreationException, Unauthorized) as e:
                raise faults.PermissionDenied(str(e))
            except GitRepositoryCreationFault as e:
                # The hosting service failed.  Log an OOPS for investigation.
                self._reportError(path, e, hosting_path=e.path)
                raise
        except Exception:
            # We don't want to keep the repository we created.
            transaction.abort()
            raise

    @return_fault
    def _translatePath(self, requester, path, permission, auth_params):
        if requester == LAUNCHPAD_ANONYMOUS:
            requester = None
        try:
            repo, result = self._performLookup(requester, path, auth_params)
            if repo and repo.status == GitRepositoryStatus.CREATING:
                raise faults.GitRepositoryBeingCreated(path)

            if (
                result is None
                and requester is not None
                and permission == "write"
            ):
                self._createRepository(requester, path)
                repo, result = self._performLookup(
                    requester, path, auth_params
                )

                # If the recently-created repo is in "CREATING" status,
                # it should be created asynchronously by hosting service
                # receiving this response. So, we must include the extra
                # parameter instructing code hosting service to do so.
                if repo.status == GitRepositoryStatus.CREATING:
                    clone_from = repo.getClonedFrom()
                    result["creation_params"] = {
                        "clone_from": (
                            clone_from.getInternalPath()
                            if clone_from
                            else None
                        )
                    }
            if result is None:
                raise faults.GitRepositoryNotFound(path)
            if permission == "read" and not result["readable"]:
                raise faults.PermissionDenied()
            if permission != "read" and not result["writable"]:
                raise faults.PermissionDenied()
            return result
        except (faults.PermissionDenied, faults.GitRepositoryNotFound):
            # Turn lookup errors for anonymous HTTP requests into
            # "authorisation required", so that the user-agent has a
            # chance to try HTTP basic auth.
            can_authenticate = auth_params.get("can-authenticate", False)
            if can_authenticate and requester is None:
                raise faults.Unauthorized()
            else:
                raise

    def translatePath(self, path, permission, auth_params):
        """See `IGitAPI`."""
        logger = self._getLogger(auth_params.get("request-id"))
        requester_id = _get_requester_id(auth_params)
        logger.info(
            "Request received: translatePath('%s', '%s') for %s",
            path,
            permission,
            requester_id,
        )
        result = run_with_login(
            requester_id,
            self._translatePath,
            six.ensure_text(path).strip("/"),
            permission,
            auth_params,
        )
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("translatePath failed: %r", result)
            else:
                # The results of path translation are not sensitive for
                # logging purposes (they may refer to private artifacts, but
                # contain no credentials).
                logger.info("translatePath succeeded: %s", result)
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    @return_fault
    def _notify(self, translated_path, statistics, auth_params):
        logger = self._getLogger()
        repository = removeSecurityProxy(
            getUtility(IGitLookup).getByHostingPath(translated_path)
        )
        if repository is None:
            fault = faults.NotFound(
                "No repository found for '%s'." % translated_path
            )
            logger.error("notify failed: %r", fault)
            return fault
        if repository is None:
            raise faults.GitRepositoryNotFound(translated_path)
        if statistics:
            removeSecurityProxy(repository).setRepackData(
                statistics.get("loose_object_count"),
                statistics.get("pack_count"),
            )
        getUtility(IGitRefScanJobSource).create(
            removeSecurityProxy(repository)
        )

    def notify(self, translated_path, statistics, auth_params):
        """See `IGitAPI`."""
        # This receives authentication parameters for historical reasons,
        # but ignores them.  We already checked authorization at the start
        # of the operation whose completion we're now being notified about,
        # so we don't do so again here, as it can have weird effects: for
        # example, it should be possible to have a short-duration personal
        # access token that expires between the start and the end of a long
        # push operation.  We have to trust turnip anyway, and the worst
        # thing that any of this can do is spuriously update statistics.
        #
        # If we feel the need to authorize notify calls in future, then it
        # should be done by checking whether a previous operation was
        # authorized, e.g. by generating a single-use token earlier.  At the
        # moment this seems like overkill, though.
        logger = self._getLogger()
        logger.info(
            "Request received: notify('%s', '%d', '%d')",
            translated_path,
            statistics.get("loose_object_count"),
            statistics.get("pack_count"),
        )

        result = self._notify(translated_path, statistics, auth_params)
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("notify failed: %r", result)
            else:
                logger.info("notify succeeded: %s" % result)
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    @return_fault
    def _getMergeProposalURL(
        self, requester, translated_path, branch, auth_params
    ):
        if requester == LAUNCHPAD_ANONYMOUS:
            requester = None
        repository = removeSecurityProxy(
            getUtility(IGitLookup).getByHostingPath(translated_path)
        )
        if repository is None:
            raise faults.GitRepositoryNotFound(translated_path)

        verified = self._verifyAuthParams(requester, repository, auth_params)
        if verified is not None and verified.user is NO_USER:
            # Showing a merge proposal URL may be useful to ordinary users,
            # but it doesn't make sense in the context of an internal service.
            return None

        # We assemble the URL this way here because the ref may not exist yet.
        base_url = canonical_url(repository, rootsite="code")
        mp_url = "%s/+ref/%s/+register-merge" % (base_url, quote(branch))
        return mp_url

    def getMergeProposalURL(self, translated_path, branch, auth_params):
        """See `IGitAPI`."""
        logger = self._getLogger(auth_params.get("request-id"))
        requester_id = _get_requester_id(auth_params)
        logger.info(
            "Request received: getMergeProposalURL('%s, %s') for %s",
            translated_path,
            branch,
            requester_id,
        )
        result = run_with_login(
            requester_id,
            self._getMergeProposalURL,
            translated_path,
            branch,
            auth_params,
        )
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("getMergeProposalURL failed: %r", result)
            else:
                # The result of getMergeProposalURL is not sensitive for
                # logging purposes (it may refer to private artifacts, but
                # contains no credentials, only the merge proposal URL).
                logger.info("getMergeProposalURL succeeded: %s" % result)
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    @return_fault
    def _getMergeProposalInfo(
        self, requester, translated_path, ref, auth_params
    ):
        if requester == LAUNCHPAD_ANONYMOUS:
            requester = None
        repository = removeSecurityProxy(
            getUtility(IGitLookup).getByHostingPath(translated_path)
        )
        if repository is None:
            raise faults.GitRepositoryNotFound(translated_path)

        verified = self._verifyAuthParams(requester, repository, auth_params)
        if verified is not None and verified.user is NO_USER:
            # Showing merge proposal information may be useful to ordinary
            # users, but it doesn't make sense in the context of
            # an internal service.
            return None

        # commit_sha1 isn't used here, but is
        # needed to satisfy the `IGitRef` interface.
        frozen_ref = repository.makeFrozenRef(path=ref, commit_sha1="")
        branch = frozen_ref.name
        merge_proposals = getUtility(
            IBranchMergeProposalGetter
        ).activeProposalsForBranches(source=frozen_ref, target=None)

        merge_proposal = merge_proposals.any()
        if merge_proposal:
            return (
                "Updated existing merge proposal "
                "for '%s' on Launchpad:\n      %s"
                % (
                    quote(branch),
                    canonical_url(merge_proposal, rootsite="code"),
                )
            )
        else:
            base_url = canonical_url(repository, rootsite="code")
            mp_url = "%s/+ref/%s/+register-merge" % (base_url, quote(branch))
            return (
                "Create a merge proposal for '%s' on "
                "Launchpad by visiting:\n      %s" % (branch, mp_url)
            )

    def getMergeProposalInfo(self, translated_path, ref, auth_params):
        """See `IGitAPI`."""
        logger = self._getLogger(auth_params.get("request-id"))
        requester_id = _get_requester_id(auth_params)
        logger.info(
            "Request received: getMergeProposalInfo('%s, %s') for %s",
            translated_path,
            ref,
            requester_id,
        )

        result = run_with_login(
            requester_id,
            self._getMergeProposalInfo,
            translated_path,
            ref,
            auth_params,
        )
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("getMergeProposalInfo failed: %r", result)
            else:
                logger.info("getMergeProposalInfo succeeded: %s" % result)
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    @return_fault
    def _authenticateWithPassword(self, username, password):
        """Authenticate a user by username and password.

        This is a separate method from `authenticateWithPassword` because
        otherwise Zope's XML-RPC publication machinery gets confused by the
        decorator and publishes a method that takes zero arguments.
        """
        user = (
            getUtility(IPersonSet).getByName(username)
            if username and username != LAUNCHPAD_SERVICES
            else None
        )
        verified = self._verifyAccessToken(user, secret=password)
        if verified is not None:
            return {"access-token": verified.token.id, "uid": user.id}
        verified = self._verifyMacaroon(password, user=user)
        if verified:
            auth_params = {"macaroon": password}
            if user is not None:
                auth_params["uid"] = user.id
            else:
                auth_params["user"] = LAUNCHPAD_SERVICES
            return auth_params
        # Only macaroons are supported for password authentication.
        raise faults.Unauthorized()

    def authenticateWithPassword(self, username, password):
        """See `IGitAPI`."""
        logger = self._getLogger()
        logger.info(
            "Request received: authenticateWithPassword('%s')", username
        )
        result = self._authenticateWithPassword(username, password)
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("authenticateWithPassword failed: %r", result)
            else:
                # The results of authentication may be sensitive, but we can
                # at least log the authenticated user.
                logger.info(
                    "authenticateWithPassword succeeded: %s",
                    result.get("uid", result.get("user")),
                )
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    def _renderPermissions(self, set_of_permissions):
        """Render a set of permission strings for XML-RPC output."""
        permissions = []
        if GitPermissionType.CAN_CREATE in set_of_permissions:
            permissions.append("create")
        if GitPermissionType.CAN_PUSH in set_of_permissions:
            permissions.append("push")
        if GitPermissionType.CAN_FORCE_PUSH in set_of_permissions:
            permissions.append("force_push")
        return permissions

    @return_fault
    def _checkRefPermissions(
        self, requester, translated_path, ref_paths, auth_params
    ):
        if requester == LAUNCHPAD_ANONYMOUS:
            requester = None
        repository = removeSecurityProxy(
            getUtility(IGitLookup).getByHostingPath(translated_path)
        )
        if repository is None:
            raise faults.GitRepositoryNotFound(translated_path)

        try:
            verified = self._verifyAuthParams(
                requester, repository, auth_params
            )
            if verified is not None and verified.user is NO_USER:
                if not _can_internal_issuer_write(verified):
                    raise faults.Unauthorized()

                # We have verified that the authentication parameters
                # correctly specify internal-services authentication with a
                # suitable macaroon that specifically grants access to this
                # repository, so we can bypass other checks and grant access
                # as an anonymous repository owner.  This is only permitted
                # for selected macaroon issuers.
                requester = GitGranteeType.REPOSITORY_OWNER
            elif (
                isinstance(verified, AccessTokenVerificationResult)
                and not verified.can_push
            ):
                # The user authenticated with an access token without the
                # "repository:push" scope, so pushing isn't allowed no
                # matter what permissions they might ordinarily have.  (This
                # should already have been checked earlier, but it doesn't
                # hurt to be careful about it here as well.)
                raise faults.Unauthorized()
        except faults.Unauthorized:
            # XXX cjwatson 2019-05-09: It would be simpler to just raise
            # this directly, but turnip won't handle it very gracefully at
            # the moment.  It's possible to reach this by being very unlucky
            # about the timing of a push.
            return [
                (xmlrpc.client.Binary(ref_path.data), [])
                for ref_path in ref_paths
            ]

        # Caller sends paths as bytes; Launchpad returns a list of (path,
        # permissions) tuples.  (XML-RPC doesn't support dict keys being
        # bytes.)
        ref_paths = [ref_path.data for ref_path in ref_paths]
        return [
            (
                xmlrpc.client.Binary(ref_path),
                self._renderPermissions(permissions),
            )
            for ref_path, permissions in repository.checkRefPermissions(
                requester, ref_paths
            ).items()
        ]

    def checkRefPermissions(self, translated_path, ref_paths, auth_params):
        """See `IGitAPI`."""
        logger = self._getLogger(auth_params.get("request-id"))
        requester_id = _get_requester_id(auth_params)
        logger.info(
            "Request received: checkRefPermissions('%s', %s) for %s",
            translated_path,
            [ref_path.data for ref_path in ref_paths],
            requester_id,
        )
        result = run_with_login(
            requester_id,
            self._checkRefPermissions,
            translated_path,
            ref_paths,
            auth_params,
        )
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("checkRefPermissions failed: %r", result)
            else:
                # The results of ref permission checks are not sensitive for
                # logging purposes (they may refer to private artifacts, but
                # contain no credentials).
                logger.info(
                    "checkRefPermissions succeeded: %s",
                    [
                        (ref_path.data, permissions)
                        for ref_path, permissions in result
                    ],
                )
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    def _validateRequesterCanManageRepoCreation(
        self, requester, repository, auth_params
    ):
        """Makes sure the requester has permission to change repository
        creation status."""
        naked_repo = removeSecurityProxy(repository)
        if requester == LAUNCHPAD_ANONYMOUS:
            requester = None

        if naked_repo.status != GitRepositoryStatus.CREATING:
            raise faults.Unauthorized()

        if requester == LAUNCHPAD_SERVICES and "macaroon" not in auth_params:
            # For repo creation management operations, we trust
            # LAUNCHPAD_SERVICES, since it should be just an internal call
            # to confirm/abort repository creation.
            return

        verified = self._verifyAuthParams(requester, repository, auth_params)
        if verified is not None and verified.user is NO_USER:
            # For internal-services authentication, we check if it's using a
            # suitable macaroon that specifically grants access to this
            # repository.  This is only permitted for macaroons not bound to
            # a user.
            if not _can_internal_issuer_write(verified):
                raise faults.Unauthorized()
        elif isinstance(verified, AccessTokenVerificationResult):
            # Access tokens can currently only be issued for an existing
            # repository, so it doesn't make sense to allow using one for
            # creating a repository.
            raise faults.Unauthorized()
        else:
            # This checks `requester` against `repo.registrant` because the
            # requester should be the only user able to confirm/abort
            # repository creation while it's being created.
            if requester != naked_repo.registrant:
                raise faults.Unauthorized()

    def _confirmRepoCreation(self, requester, translated_path, auth_params):
        naked_repo = removeSecurityProxy(
            getUtility(IGitLookup).getByHostingPath(translated_path)
        )
        if naked_repo is None:
            raise faults.GitRepositoryNotFound(translated_path)
        self._validateRequesterCanManageRepoCreation(
            requester, naked_repo, auth_params
        )
        naked_repo.rescan()
        naked_repo.status = GitRepositoryStatus.AVAILABLE

    def confirmRepoCreation(self, translated_path, auth_params):
        """See `IGitAPI`."""
        logger = self._getLogger(auth_params.get("request-id"))
        requester_id = _get_requester_id(auth_params)
        logger.info(
            "Request received: confirmRepoCreation('%s')", translated_path
        )
        try:
            result = run_with_login(
                requester_id,
                self._confirmRepoCreation,
                translated_path,
                auth_params,
            )
        except Exception as e:
            result = e
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("confirmRepoCreation failed: %r", result)
            else:
                logger.info("confirmRepoCreation succeeded: %s" % result)
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    def _abortRepoCreation(self, requester, translated_path, auth_params):
        naked_repo = removeSecurityProxy(
            getUtility(IGitLookup).getByHostingPath(translated_path)
        )
        if naked_repo is None:
            raise faults.GitRepositoryNotFound(translated_path)
        self._validateRequesterCanManageRepoCreation(
            requester, naked_repo, auth_params
        )
        naked_repo.destroySelf(break_references=True)

    def abortRepoCreation(self, translated_path, auth_params):
        """See `IGitAPI`."""
        logger = self._getLogger(auth_params.get("request-id"))
        requester_id = _get_requester_id(auth_params)
        logger.info(
            "Request received: abortRepoCreation('%s')", translated_path
        )
        try:
            result = run_with_login(
                requester_id,
                self._abortRepoCreation,
                translated_path,
                auth_params,
            )
        except Exception as e:
            result = e
        try:
            if isinstance(result, xmlrpc.client.Fault):
                logger.error("abortRepoCreation failed: %r", result)
            else:
                logger.info("abortRepoCreation succeeded: %s" % result)
            return result
        finally:
            # Avoid traceback reference cycles.
            del result

    def updateRepackStats(self, translated_path, statistics):
        """See `IGitAPI`."""
        logger = self._getLogger()
        logger.info(
            "Request received: updateRepackStats('%s', '%d', '%d')",
            translated_path,
            statistics.get("loose_object_count"),
            statistics.get("pack_count"),
        )
        repository = getUtility(IGitLookup).getByHostingPath(translated_path)
        if repository is None:
            fault = faults.GitRepositoryNotFound(translated_path)
            logger.error("updateRepackStats failed: %r", fault)
            return fault
        removeSecurityProxy(repository).setRepackData(
            loose_object_count=statistics.get("loose_object_count"),
            pack_count=statistics.get("pack_count"),
        )
        logger.info(
            "updateRepackStats succeeded for repo id %s with: %s %s "
            % (
                translated_path,
                statistics.get("loose_object_count"),
                statistics.get("pack_count"),
            )
        )
