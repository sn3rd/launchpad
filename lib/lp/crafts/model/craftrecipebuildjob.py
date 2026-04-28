# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Craft recipe build jobs."""

__all__ = [
    "CraftPublishingJob",
    "CraftRecipeBuildJob",
    "CraftRecipeBuildJobType",
]

import glob
import json
import os
import subprocess
import tarfile
import tempfile
from configparser import NoSectionError
from urllib.parse import urlparse

import requests
import transaction
import yaml
from artifactory import ArtifactoryPath
from lazr.delegates import delegate_to
from lazr.enum import DBEnumeratedType, DBItem
from storm.databases.postgres import JSON
from storm.locals import Int, Reference
from zope.interface import implementer, provider

from lp.app.errors import NotFoundError
from lp.archiveuploader.utils import SafeTarExtractError, safe_extract_tar
from lp.crafts.interfaces.craftrecipebuildjob import (
    ICraftPublishingJob,
    ICraftPublishingJobSource,
    ICraftRecipeBuildJob,
)
from lp.registry.interfaces.distributionsourcepackage import (
    IDistributionSourcePackage,
)
from lp.services.config import config
from lp.services.database.enumcol import DBEnum
from lp.services.database.interfaces import IPrimaryStore, IStore
from lp.services.database.stormbase import StormBase
from lp.services.job.model.job import EnumeratedSubclass, Job
from lp.services.job.runner import BaseRunnableJob
from lp.services.scripts import log

# Timeout in seconds for HTTP requests made through Cargo.
# Prevents builds from hanging if network connectivity issues occur.
CARGO_HTTP_TIMEOUT = 60

# Memory limits for different job types
MAVEN_JOB_MEMORY_LIMIT = 8 * (1024**3)  # 8GB
DEFAULT_JOB_MEMORY_LIMIT = 2 * (1024**3)  # 2GB


class CraftRecipeBuildJobType(DBEnumeratedType):
    """Values that `ICraftRecipeBuildJob.job_type` can take."""

    PUBLISH_ARTIFACTS = DBItem(
        0,
        "Publish artifacts to external repositories",
    )


@implementer(ICraftRecipeBuildJob)
class CraftRecipeBuildJob(StormBase):
    """See `ICraftRecipeBuildJob`."""

    __storm_table__ = "CraftRecipeBuildJob"

    job_id = Int(name="job", primary=True, allow_none=False)
    job = Reference(job_id, "Job.id")

    build_id = Int(name="build", allow_none=False)
    build = Reference(build_id, "CraftRecipeBuild.id")

    job_type = DBEnum(enum=CraftRecipeBuildJobType, allow_none=False)

    metadata = JSON("json_data", allow_none=False)

    def __init__(self, build, job_type, metadata, **job_args):
        super().__init__()
        self.job = Job(**job_args)
        self.build = build
        self.job_type = job_type
        self.metadata = metadata

    def makeDerived(self):
        return CraftRecipeBuildJobDerived.makeSubclass(self)


@delegate_to(ICraftRecipeBuildJob)
class CraftRecipeBuildJobDerived(
    BaseRunnableJob, metaclass=EnumeratedSubclass
):
    """See `ICraftRecipeBuildJob`."""

    def __init__(self, craft_recipe_build_job):
        self.context = craft_recipe_build_job

    def __repr__(self):
        """An informative representation of the job."""
        recipe = self.build.recipe
        return (
            f"<{self.__class__.__name__} for "
            f"~{recipe.owner.name}/{recipe.project.name}/+craft/{recipe.name}/"
            f"+build/{self.build.id}>"
        )

    @classmethod
    def get(cls, job_id):
        craft_recipe_build_job = IStore(CraftRecipeBuildJob).get(
            CraftRecipeBuildJob, job_id
        )
        if craft_recipe_build_job.job_type != cls.class_job_type:
            raise NotFoundError(
                f"No object found with id {job_id} "
                f"and type {cls.class_job_type.title}"
            )
        return cls(craft_recipe_build_job)

    @classmethod
    def iterReady(cls):
        """See `IJobSource`."""
        jobs = IPrimaryStore(CraftRecipeBuildJob).find(
            CraftRecipeBuildJob,
            CraftRecipeBuildJob.job_type == cls.class_job_type,
            CraftRecipeBuildJob.job == Job.id,
            Job.id.is_in(Job.ready_jobs),
        )
        return (cls(job) for job in jobs)

    def getOopsVars(self):
        """See `IRunnableJob`."""
        oops_vars = super().getOopsVars()
        recipe = self.context.build.recipe
        oops_vars.extend(
            [
                ("job_id", self.context.job.id),
                ("job_type", self.context.job_type.title),
                ("build_id", self.context.build.id),
                ("recipe_owner_name", recipe.owner.name),
                ("recipe_project_name", recipe.project.name),
                ("recipe_name", recipe.name),
            ]
        )
        return oops_vars


@implementer(ICraftPublishingJob)
@provider(ICraftPublishingJobSource)
class CraftPublishingJob(CraftRecipeBuildJobDerived):
    """
    A Job that publishes craft recipe build artifacts to external
    repositories.
    """

    class_job_type = CraftRecipeBuildJobType.PUBLISH_ARTIFACTS

    user_error_types = ()
    retry_error_types = ()
    max_retries = 5

    task_queue = "native_publisher_job"
    config = config.ICraftPublishingJobSource

    @property
    def memory_limit(self):
        """Dynamically determine memory limit based on job type.

        Maven jobs need more memory for JVM operations, while Rust/Cargo jobs
        can work with the default limit.

        There is some duplication here with checking the file type in the run
        method, but it's not worth refactoring atm.
        """
        # Check if this job will be processing Maven artifacts
        for _, lfa, _ in self.build.getFiles():
            if lfa.filename.endswith(".jar") or lfa.filename == "pom.xml":
                # Maven job - needs more memory for JVM
                return MAVEN_JOB_MEMORY_LIMIT

        # Non-Maven job (Rust/Cargo) - use default limit
        return DEFAULT_JOB_MEMORY_LIMIT

    @classmethod
    def create(cls, build):
        """See `ICraftPublishingJobSource`."""
        metadata = {
            "build_id": build.id,
        }
        recipe_job = CraftRecipeBuildJob(build, cls.class_job_type, metadata)
        job = cls(recipe_job)
        job.celeryRunOnCommit()
        IStore(CraftRecipeBuildJob).flush()
        return job

    @property
    def error_message(self):
        """See `ICraftPublishingJob`."""
        return self.metadata.get("error_message")

    @error_message.setter
    def error_message(self, message):
        """See `ICraftPublishingJob`."""
        self.metadata["error_message"] = message

    def run(self):
        """See `IRunnableJob`."""
        try:
            # Get the distribution name to access the correct configuration
            distribution_name = None
            git_repo = self.build.recipe.git_repository
            if git_repo is not None:
                if IDistributionSourcePackage.providedBy(git_repo.target):
                    distribution_name = git_repo.target.distribution.name

            if not distribution_name:
                self.error_message = (
                    "Could not determine distribution for build"
                )
                raise Exception(self.error_message)

            # Get environment variables from configuration
            try:
                env_vars_json = config["craftbuild." + distribution_name][
                    "environment_variables"
                ]
                if env_vars_json and env_vars_json.lower() != "none":
                    env_vars = json.loads(env_vars_json)
                    # Replace auth placeholders
                    for key, value in env_vars.items():
                        if (
                            isinstance(value, str)
                            and "%(write_auth)s" in value
                        ):
                            env_vars[key] = value.replace(
                                "%(write_auth)s",
                                config.artifactory.write_credentials,
                            )
                else:
                    env_vars = {}
            except (NoSectionError, KeyError):
                self.error_message = (
                    f"No configuration found for {distribution_name}"
                )
                raise Exception(self.error_message)

            # Check if HTTP proxy is configured - log a warning but continue
            if not config.launchpad.http_proxy:
                log.warning(
                    "No HTTP proxy configured for artifact publishing. "
                    "This may cause connectivity issues with external "
                    "repositories."
                )

            # Check if we have a .crate file or .jar file
            crate_file = None
            jar_file = None
            pom_file = None

            for _, lfa, _ in self.build.getFiles():
                if lfa.filename.endswith(".crate"):
                    crate_file = lfa
                elif lfa.filename.endswith(".jar"):
                    jar_file = lfa
                elif lfa.filename == "pom.xml":
                    pom_file = lfa

            # Process the crate file
            with tempfile.TemporaryDirectory() as tmpdir:
                if crate_file is not None:
                    # Download the crate file
                    crate_path = os.path.join(tmpdir, crate_file.filename)
                    crate_file.open()
                    try:
                        with open(crate_path, "wb") as f:
                            f.write(crate_file.read())
                    finally:
                        crate_file.close()

                    # Create a directory to extract the crate
                    crate_extract_dir = os.path.join(tmpdir, "crate_contents")
                    os.makedirs(crate_extract_dir, exist_ok=True)

                    try:
                        with tarfile.open(crate_path, "r:*") as tar:
                            safe_extract_tar(tar, crate_extract_dir)
                    except (tarfile.TarError, SafeTarExtractError) as e:
                        raise Exception(f"Failed to extract crate: {e}") from e

                    # Find the extracted directory(should be the only one)
                    extracted_dirs = [
                        d
                        for d in os.listdir(crate_extract_dir)
                        if os.path.isdir(os.path.join(crate_extract_dir, d))
                    ]

                    if not extracted_dirs:
                        raise Exception(
                            "No directory found in extracted crate"
                        )

                    # Use the first directory as the crate directory
                    crate_dir = os.path.join(
                        crate_extract_dir, extracted_dirs[0]
                    )

                    # Publish the Rust crate
                    self._publish_rust_crate(
                        crate_dir, env_vars, crate_file.filename
                    )
                elif jar_file is not None and pom_file is not None:
                    # Download the jar file
                    jar_path = os.path.join(tmpdir, jar_file.filename)
                    jar_file.open()
                    try:
                        with open(jar_path, "wb") as f:
                            f.write(jar_file.read())
                    finally:
                        jar_file.close()

                    # Download the pom file
                    pom_path = os.path.join(tmpdir, "pom.xml")
                    pom_file.open()
                    try:
                        with open(pom_path, "wb") as f:
                            f.write(pom_file.read())
                    finally:
                        pom_file.close()

                    # Publish the Maven artifact
                    self._publish_maven_artifact(
                        tmpdir,
                        env_vars,
                        jar_file.filename,
                        jar_path,
                        pom_path,
                    )

                else:
                    raise Exception("No publishable artifacts found in build")

        except Exception as e:
            self.error_message = str(e)
            # The normal job infrastructure will abort the transaction, but
            # we want to commit instead: the only database changes we make
            # are to this job's metadata and should be preserved.
            transaction.commit()
            raise

    def _publish_rust_crate(self, extract_dir, env_vars, artifact_name):
        """Publish Rust crates from the extracted crate directory.

        :param extract_dir: Path to the extracted crate directory
        :param env_vars: Environment variables from configuration
        :raises: Exception if publishing fails
        """
        # Look for specific Cargo publishing repository configuration
        cargo_publish_url = env_vars.get("CARGO_PUBLISH_URL")
        cargo_publish_auth = env_vars.get("CARGO_PUBLISH_AUTH")

        if not cargo_publish_url or not cargo_publish_auth:
            raise Exception(
                "Missing Cargo publishing repository configuration"
            )

        # Extract token from auth string (discard username if present)
        if ":" in cargo_publish_auth:
            _, token = cargo_publish_auth.split(":", 1)
            token = token.strip('"')
        else:
            token = cargo_publish_auth

        # Set up cargo config
        cargo_dir = os.path.join(extract_dir, ".cargo")
        os.makedirs(cargo_dir, exist_ok=True)

        # Create config.toml
        with open(os.path.join(cargo_dir, "config.toml"), "w") as f:
            config_content = (
                "\n"
                "[registry]\n"
                'global-credential-providers = ["cargo:token"]\n'
                "\n"
                "[registries.launchpad]\n"
                f'index = "{cargo_publish_url}"\n'
                "\n"
                "[source.crates-io]\n"
                'replace-with = "launchpad"\n'
            )

            # Only add the HTTP proxy configuration if it's set
            if config.launchpad.http_proxy:
                config_content += (
                    "\n"
                    "[http]\n"
                    f"proxy = '{config.launchpad.http_proxy}'\n"
                    f"timeout = {CARGO_HTTP_TIMEOUT}\n"
                    "multiplexing = false\n"
                )

            f.write(config_content)

        # Create credentials.toml
        with open(os.path.join(cargo_dir, "credentials.toml"), "w") as f:
            f.write(
                "\n" "[registries.launchpad]\n" f'token = "Bearer {token}"\n'
            )

        # Replace any Cargo.toml files with their .orig versions if they exist,
        # as the .orig files contain the original content before build
        # modifications
        orig_files = glob.glob(
            f"{extract_dir}/**/Cargo.toml.orig", recursive=True
        )
        for orig_file in orig_files:
            cargo_toml = orig_file.replace(".orig", "")
            if os.path.exists(cargo_toml):
                os.replace(orig_file, cargo_toml)

        # Run cargo publish from the extracted directory
        result = subprocess.run(
            [
                "cargo",
                "publish",
                "--no-verify",
                "--allow-dirty",
                "--registry",
                "launchpad",
            ],
            capture_output=True,
            cwd=extract_dir,
            env={"CARGO_HOME": cargo_dir},
        )

        if result.returncode != 0:
            raise Exception(f"Failed to publish crate: {result.stderr}")

        self._publish_properties(cargo_publish_url, artifact_name)

    def _publish_maven_artifact(
        self, work_dir, env_vars, artifact_name, jar_path=None, pom_path=None
    ):
        """Publish Maven artifacts.

        :param work_dir: Working directory
        :param env_vars: Environment variables from configuration
        :param jar_path: Path to the JAR file
        :param pom_path: Path to the pom.xml file
        :raises: Exception if publishing fails
        """
        # Look for specific Maven publishing repository configuration
        maven_publish_url = env_vars.get("MAVEN_PUBLISH_URL")
        maven_publish_auth = env_vars.get("MAVEN_PUBLISH_AUTH")

        if not maven_publish_url or not maven_publish_auth:
            raise Exception(
                "Missing Maven publishing repository configuration"
            )

        if jar_path is None or pom_path is None:
            raise Exception("Missing JAR or POM file for Maven publishing")

        # Set up Maven settings
        maven_dir = os.path.join(work_dir, ".m2")
        os.makedirs(maven_dir, exist_ok=True)

        # Extract username and password from auth string
        if ":" in maven_publish_auth:
            username, password = maven_publish_auth.split(":", 1)
        else:
            username = "launchpad"
            password = maven_publish_auth

        # Generate settings.xml content
        settings_xml = self._get_maven_settings_xml(
            username, password, maven_publish_url
        )

        with open(os.path.join(maven_dir, "settings.xml"), "w") as f:
            f.write(settings_xml)

        # We set MAVEN_OPTS to control JVM memory usage.
        # -Xmx4G and -Xms4G: Set the maximum and initial heap size to 4GB.
        #   This is needed because Maven can require significant memory for
        #   dependency resolution and artifact processing, especially for large
        #   projects or when running in containers with limited default memory.
        # -XX:CompressedClassSpaceSize=1G and -XX:MaxMetaspaceSize=1G:
        #   These options limit the class metadata and metaspace to 1GB each,
        #   which helps prevent the JVM from exhausting system memory due to
        #   class loading, but is generous enough for typical Maven builds.
        # These values are chosen to balance between avoiding out-of-memory
        # errors and not over-allocating resources in the publisher.
        env = os.environ.copy()
        env["MAVEN_OPTS"] = (
            "-Xmx4G -Xms4G "
            "-XX:CompressedClassSpaceSize=1G "
            "-XX:MaxMetaspaceSize=1G"
        )
        # Run mvn deploy using the pom file
        result = subprocess.run(
            [
                "mvn",
                "deploy:deploy-file",
                f"-Dfile={jar_path}",
                f"-DpomFile={pom_path}",
                "-DrepositoryId=artifactory",
                f"-Durl={maven_publish_url}",
                "--settings={}".format(
                    os.path.join(maven_dir, "settings.xml")
                ),
            ],
            capture_output=True,
            cwd=work_dir,
            env=env,
        )

        if result.returncode != 0:
            log.error(
                f"[+] Maven publish failed (return code: {result.returncode})"
            )
            log.error(f"[+] Maven stdout:\n{result.stdout}")

            raise Exception("Failed to publish Maven artifact")

        # Only publish properties for release artifacts, not SNAPSHOTs.
        # SNAPSHOT artifacts are timestamped and represent development builds,
        # so their coordinates change with each publish and they are not
        # considered stable releases. We only care about publishing properties
        # for release artifacts, which are stable and have fixed coordinates.
        if "SNAPSHOT" not in artifact_name:
            self._publish_properties(maven_publish_url, artifact_name)

    def _get_maven_settings_xml(self, username, password, repository_url):
        """Generate Maven settings.xml content.

        :param username: Maven repository username
        :param password: Maven repository password
        :param repository_url: Artifactory repository URL
        :return: XML content as string
        """
        # Get proxy settings from config
        proxy_url = config.launchpad.http_proxy
        proxy_host = None
        proxy_port = None

        if proxy_url:
            # Parse the proxy URL using urllib.parse
            parsed_url = urlparse(proxy_url)

            # Extract host and port from the parsed URL
            proxy_host = parsed_url.hostname
            proxy_port = parsed_url.port

            # Log warning if proxy URL doesn't contain both host and port
            if not proxy_host or not proxy_port:
                log.warning(
                    f"Invalid proxy URL format: {proxy_url}. "
                    "Expected format: http://hostname:port. "
                    "Maven proxy configuration will be skipped."
                )
                proxy_host = None
                proxy_port = None

        # Break it into smaller parts to avoid long lines
        header = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"\n'
            '        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        )

        schema = (
            '        xsi:schemaLocation="'
            "http://maven.apache.org/SETTINGS/1.0.0 "
            'http://maven.apache.org/xsd/settings-1.0.0.xsd">\n'
        )

        servers = (
            "    <servers>\n"
            "        <server>\n"
            "            <id>artifactory</id>\n"
            f"            <username>{username}</username>\n"
            f"            <password>{password}</password>\n"
            "        </server>\n"
            "    </servers>\n"
        )

        # Add proxy configuration if proxy is configured
        proxies = ""
        if proxy_host and proxy_port:
            proxies = (
                "    <proxies>\n"
                "        <proxy>\n"
                "            <id>launchpad-proxy</id>\n"
                "            <active>true</active>\n"
                "            <protocol>http</protocol>\n"
                f"            <host>{proxy_host}</host>\n"
                f"            <port>{proxy_port}</port>\n"
                "        </proxy>\n"
                "    </proxies>\n"
            )

        # Profile with repositories and pluginRepositories
        profiles = (
            "    <profiles>\n"
            "        <profile>\n"
            "            <id>artifactory</id>\n"
            "            <repositories>\n"
            "                <repository>\n"
            "                    <id>artifactory</id>\n"
            f"                    <url>{repository_url}</url>\n"
            "                </repository>\n"
            "            </repositories>\n"
            "            <pluginRepositories>\n"
            "                <pluginRepository>\n"
            "                    <id>artifactory</id>\n"
            f"                    <url>{repository_url}</url>\n"
            "                </pluginRepository>\n"
            "            </pluginRepositories>\n"
            "        </profile>\n"
            "    </profiles>\n"
        )

        active_profiles = (
            "    <activeProfiles>\n"
            "        <activeProfile>artifactory</activeProfile>\n"
            "    </activeProfiles>\n"
        )

        # Mirror that redirects everything to Artifactory
        mirrors = (
            "    <mirrors>\n"
            "        <mirror>\n"
            "            <id>artifactory</id>\n"
            "            <name>Maven Repository Manager</name>\n"
            f"            <url>{repository_url}</url>\n"
            "            <mirrorOf>*</mirrorOf>\n"
            "        </mirror>\n"
            "    </mirrors>\n"
        )

        # Combine all parts
        return (
            header
            + schema
            + servers
            + proxies
            + profiles
            + active_profiles
            + mirrors
            + "</settings>"
        )

    def _make_session(self) -> requests.Session:
        """Make a suitable requests session for talking to Artifactory."""

        session = requests.Session()
        session.trust_env = False
        if config.launchpad.http_proxy:
            session.proxies = {
                "http": config.launchpad.http_proxy,
                "https": config.launchpad.http_proxy,
            }
        if config.launchpad.ca_certificates_path is not None:
            session.verify = config.launchpad.ca_certificates_path
        write_creds = config.artifactory.write_credentials

        if write_creds is not None:
            session.headers.update(
                {"Authorization": f"Bearer {write_creds.split(':', 1)[1]}"}
            )
        return session

    def _publish_properties(
        self, publish_url: str, artifact_name: str
    ) -> None:
        """Publish properties to the artifact in Artifactory."""

        new_properties = {}

        new_properties["soss.commit_id"] = (
            [self.build.revision_id] if self.build.revision_id else ["unknown"]
        )
        new_properties["soss.source_url"] = [self._recipe_git_url()]
        new_properties["soss.type"] = ["source"]
        new_properties["soss.license"] = [self._get_license_metadata()]
        version_str = self._get_version_metadata()
        channel_value = self._build_channel_value(version_str)
        new_properties["launchpad.channel"] = [channel_value]

        # Repo name is derived from the URL
        # refer to schema-lazr.conf for more details about the URL structure
        parsed_url = urlparse(publish_url)
        # If publish_url is a fully-qualified URL, use its path;
        # otherwise, use as-is.
        path_to_split = (
            parsed_url.path
            if parsed_url.scheme and parsed_url.netloc
            else publish_url
        )
        url_parts = path_to_split.rstrip("/").split("/")

        if publish_url.endswith("/index") or publish_url.endswith("/index/"):
            # Cargo URL - repository name is second to last part
            repo_name = url_parts[-2]
        else:
            # Maven URL - repository name is the last part
            repo_name = url_parts[-1]

        root_path_str = self._extract_root_path(publish_url)
        if not root_path_str:
            raise NotFoundError(
                f"Could not extract root path from URL: {publish_url}"
            )

        # Search for the artifact in Artifactory using AQL
        root_path = ArtifactoryPath(root_path_str)
        root_path.session = self._make_session()

        artifacts = root_path.aql(
            "items.find",
            {
                "repo": repo_name,
                "name": artifact_name,
            },
            ".include",
            ["repo", "path", "name"],
            ".limit(1)",
        )

        if not artifacts:
            raise NotFoundError(
                f"Artifact '{artifact_name}' not found in repository: "
                + f"'{repo_name}'"
            )

        if len(artifacts) > 1:
            log.info(
                f"Multiple artifacts found for '{artifact_name}'"
                + f"in repository '{repo_name}'. Using the first one."
            )

        # Get the first artifact that matches the name
        artifact = artifacts[0]

        artifact_path = ArtifactoryPath(
            root_path, artifact["repo"], artifact["path"], artifact["name"]
        )
        artifact_path.session = self._make_session()
        artifact_path.set_properties(new_properties)

    def _extract_root_path(self, publish_url: str) -> str:
        """
        Extracts everything from the first occurrence of 'https' up to and
        including 'artifactory'."""
        start_index = publish_url.find("https")
        if start_index == -1:
            return ""

        end_index = publish_url.find("artifactory", start_index)
        if end_index == -1:
            return ""

        return publish_url[start_index : end_index + len("artifactory")]

    def _recipe_git_url(self):
        """Get the recipe git URL."""

        craft_recipe = self.build.recipe
        if craft_recipe.git_repository is not None:
            return craft_recipe.git_repository.getCodebrowseUrl()
        elif craft_recipe.git_repository_url is not None:
            return craft_recipe.git_repository_url
        else:
            log.info(
                f"Recipe {craft_recipe.id} has no git repository URL defined."
            )
            return "unknown"

    def _get_artifact_metadata(self) -> dict:
        """Load and cache metadata from metadata.yaml if present.

        Returns an empty dict when metadata is missing or cannot be parsed.
        """
        if hasattr(self, "_artifact_metadata"):
            return self._artifact_metadata

        for _, lfa, _ in self.build.getFiles():
            if lfa.filename == "metadata.yaml":
                lfa.open()
                try:
                    content = lfa.read().decode("utf-8")
                    metadata = yaml.safe_load(content) or {}
                    self._artifact_metadata = metadata
                    return self._artifact_metadata
                except yaml.YAMLError as e:
                    self.error_message = f"Failed to parse metadata.yaml: {e}"
                    log.info(self.error_message)
                    self._artifact_metadata = {}
                    return self._artifact_metadata
                finally:
                    lfa.close()

        log.info("No metadata.yaml file found in the build files.")
        self._artifact_metadata = {}
        return self._artifact_metadata

    def _get_license_metadata(self) -> str:
        """Get the license metadata from the cached metadata."""
        metadata = self._get_artifact_metadata()
        if "license" not in metadata:
            log.info("No license found in metadata.yaml, returning 'unknown'.")
            return "unknown"
        return metadata.get("license")

    def _get_version_metadata(self) -> str:
        """Get the version metadata from the cached metadata."""
        metadata = self._get_artifact_metadata()
        if "version" not in metadata:
            log.info("No version found in metadata.yaml, returning 'unknown'.")
            return "unknown"
        return str(metadata.get("version"))

    def _get_series_name(self) -> str:
        """Return the distro series name for this build, or 'unknown'.

        We derive the series from the build's `distro_arch_series` to prefix
        the channel value.
        """
        das = getattr(self.build, "distro_arch_series", None)
        if das is not None and getattr(das, "distroseries", None) is not None:
            series_attr = getattr(das.distroseries, "name", None)
            if series_attr:
                return series_attr
        log.warning(
            f"Could not determine distro series for build {self.build.id}; "
            "defaulting to 'unknown'."
        )
        return "unknown"

    def _build_channel_value(self, version_str: str) -> str:
        """Compose the channel value as '<series>:<version>/stable'.

        Falls back to 'unknown' when version or series cannot be resolved.
        """
        series_name = self._get_series_name()
        channel_suffix = (
            f"{version_str}/stable" if version_str else "unknown/stable"
        )
        return f"{series_name}:{channel_suffix}"
