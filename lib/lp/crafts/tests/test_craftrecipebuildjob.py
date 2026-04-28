# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import io
import json
import logging
import os
import tarfile
import tempfile
from pathlib import Path
from resource import RLIMIT_AS  # Maximum memory that can be taken by a process

from artifactory import ArtifactoryPath
from fixtures import FakeLogger
from testtools.matchers import Equals, Is, MatchesStructure
from zope.component import getUtility
from zope.security.proxy import removeSecurityProxy

from lp.archivepublisher.tests.artifactory_fixture import (
    FakeArtifactoryFixture,
)
from lp.crafts.interfaces.craftrecipe import CRAFT_RECIPE_ALLOW_CREATE
from lp.crafts.interfaces.craftrecipebuildjob import (
    ICraftPublishingJob,
    ICraftPublishingJobSource,
    ICraftRecipeBuildJob,
)
from lp.crafts.model.craftrecipebuildjob import (
    CraftPublishingJob,
    CraftRecipeBuildJob,
    CraftRecipeBuildJobType,
)
from lp.services.features.testing import FeatureFixture
from lp.services.job.interfaces.job import JobStatus
from lp.services.job.runner import JobRunner
from lp.services.librarian.interfaces import ILibraryFileAliasSet
from lp.services.librarian.utils import copy_and_close
from lp.services.osutils import preserve_rlimit
from lp.testing import TestCaseWithFactory
from lp.testing.layers import CeleryJobLayer, ZopelessDatabaseLayer


class TestCraftRecipeBuildJob(TestCaseWithFactory):

    layer = ZopelessDatabaseLayer

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CRAFT_RECIPE_ALLOW_CREATE: "on"}))

    def test_provides_interface(self):
        # CraftRecipeBuildJob provides ICraftRecipeBuildJob.
        build = self.factory.makeCraftRecipeBuild()
        self.assertProvides(
            CraftRecipeBuildJob(
                build,
                CraftRecipeBuildJobType.PUBLISH_ARTIFACTS,
                {},
            ),
            ICraftRecipeBuildJob,
        )


class MockBytesIO(io.BytesIO):
    """A mock BytesIO class to simulate an artifact file."""

    def open(self):
        pass


class TestCraftPublishingJob(TestCaseWithFactory):
    """Test the CraftPublishingJob."""

    layer = CeleryJobLayer

    def setUp(self):
        super().setUp()
        self.useFixture(
            FeatureFixture(
                {"jobs.celery.enabled_classes": "CraftPublishingJob"}
            )
        )
        self.useFixture(FakeLogger())
        self.useFixture(FeatureFixture({CRAFT_RECIPE_ALLOW_CREATE: "on"}))
        self.recipe = self.factory.makeCraftRecipe()
        ds = self.factory.makeDistroSeries(name="noble")
        das = self.factory.makeDistroArchSeries(distroseries=ds)
        self.build = self.factory.makeCraftRecipeBuild(
            recipe=self.recipe, distro_arch_series=das
        )

        # Set up the Artifactory fixture
        self.base_url = "https://example.com/artifactory"
        self.repository_name = "repository"
        self.publish_url = f"{self.base_url}/{self.repository_name}/index"

        self.artifactory = self.useFixture(
            FakeArtifactoryFixture(self.base_url, self.repository_name)
        )

    def run_job(self, job):
        """Helper to run a job and return the result."""
        job = getUtility(ICraftPublishingJobSource).create(self.build)

        # Preserve the virtual memory resource limit because runAll changes it
        # which causes the whole test worker process to have limited memory
        with preserve_rlimit(RLIMIT_AS):
            JobRunner([job]).runAll()

        job = removeSecurityProxy(job)
        return job

    def _setup_distribution(self, distribution_name="soss"):
        """Helper to setup a distribution and its source package."""
        distribution = self.factory.makeDistribution(name=distribution_name)
        package = self.factory.makeDistributionSourcePackage(
            distribution=distribution
        )
        git_repository = self.factory.makeGitRepository(target=package)
        removeSecurityProxy(self.recipe).git_repository = git_repository

    def _setup_config(self, with_env_vars=False, with_http_proxy=False):
        """Helper to setup a config with the given values."""
        distribution_name = "soss"

        # Push config with or without env vars
        if not with_env_vars:
            # Just push the distribution section without env vars
            self.pushConfig(f"craftbuild.{distribution_name}")
        else:
            # Push with standard environment variables
            env_vars = {
                "CARGO_PUBLISH_URL": self.publish_url,
                "CARGO_PUBLISH_AUTH": "lp:token123",
                "MAVEN_PUBLISH_URL": self.publish_url,
                "MAVEN_PUBLISH_AUTH": "lp:token123",
            }
            self.pushConfig(
                f"craftbuild.{distribution_name}",
                environment_variables=json.dumps(env_vars),
            )

        if with_http_proxy:
            self.pushConfig("launchpad", http_proxy="http://localhost:8080")

    def _create_crate_file(self, crate_name="test-0.1.0"):
        """Helper to create a crate file."""
        # Create a BytesIO object to hold the tar data
        tar_data = io.BytesIO()

        # Create a tar archive
        with tarfile.open(fileobj=tar_data, mode="w") as tar:
            # Create a directory entry for the crate
            crate_dir_info = tarfile.TarInfo(crate_name)
            crate_dir_info.type = tarfile.DIRTYPE
            crate_dir_info.mode = 0o755
            tar.addfile(crate_dir_info)

            # Add a Cargo.toml file
            cargo_toml = (
                "[package]\n"
                'name = "test"\n'
                'version = "0.1.0"\n'
                'authors = ["Test <test@example.com>"]\n'
                'edition = "2025"\n'
            )
            cargo_toml_info = tarfile.TarInfo(f"{crate_name}/Cargo.toml")
            cargo_toml_bytes = cargo_toml.encode("utf-8")
            cargo_toml_info.size = len(cargo_toml_bytes)
            tar.addfile(cargo_toml_info, io.BytesIO(cargo_toml_bytes))

            # Add a src directory
            src_dir_info = tarfile.TarInfo(f"{crate_name}/src")
            src_dir_info.type = tarfile.DIRTYPE
            tar.addfile(src_dir_info)

            # Add a main.rs file
            main_rs = 'fn main() { println!("Hello, world!"); }'
            main_rs_info = tarfile.TarInfo(f"{crate_name}/src/main.rs")
            main_rs_bytes = main_rs.encode("utf-8")
            main_rs_info.size = len(main_rs_bytes)
            tar.addfile(main_rs_info, io.BytesIO(main_rs_bytes))

        # Get the tar data
        tar_data.seek(0)
        crate_content = tar_data.getvalue()

        # Create a LibraryFileAlias with the crate content
        librarian = getUtility(ILibraryFileAliasSet)
        lfa = librarian.create(
            f"{crate_name}.crate",
            len(crate_content),
            io.BytesIO(crate_content),
            "application/x-tar",
        )

        removeSecurityProxy(self.build).addFile(lfa)

        return lfa

    def _create_invalid_crate_file(self, type):
        """Create an invalid crate file of different types."""
        if type == "no_dirs":
            tar_data = io.BytesIO()
            with tarfile.open(fileobj=tar_data, mode="w") as tar:
                # Add a file at the root level (no directory)
                file_info = tarfile.TarInfo("file.txt")
                file_content = b"This is a file with no directory"
                file_info.size = len(file_content)
                tar.addfile(file_info, io.BytesIO(file_content))
            tar_data.seek(0)
            content = tar_data.getvalue()
            filename = "nodirs-0.1.0.crate"
            mimetype = "application/x-tar"
        elif type == "symlink_escape":
            tar_data = io.BytesIO()
            with tarfile.open(fileobj=tar_data, mode="w") as tar:
                crate_name = "unsafe-0.1.0"
                crate_dir_info = tarfile.TarInfo(crate_name)
                crate_dir_info.type = tarfile.DIRTYPE
                crate_dir_info.mode = 0o755
                tar.addfile(crate_dir_info)

                # A symlink that would resolve outside the extraction root.
                link_info = tarfile.TarInfo(f"{crate_name}/escape")
                link_info.type = tarfile.SYMTYPE
                link_info.linkname = "../../../../tmp/evil-target"
                tar.addfile(link_info)
            tar_data.seek(0)
            content = tar_data.getvalue()
            filename = "symlink-escape-0.1.0.crate"
            mimetype = "application/x-tar"
        elif type == "dummy":
            content = b"test content"
            filename = "test.txt"
            mimetype = "text/plain"
        elif type == "invalid_tar":
            content = b"This is not a valid tar file"
            filename = "invalid-0.1.0.crate"
            mimetype = "application/x-tar"
        else:
            raise ValueError(f"Unknown invalid crate type: {type}")

        librarian = getUtility(ILibraryFileAliasSet)
        lfa = librarian.create(
            filename,
            len(content),
            io.BytesIO(content),
            mimetype,
        )

        removeSecurityProxy(self.build).addFile(lfa)

    def _artifactory_search(self, repo_name, artifact_name):
        """Helper to search for a file in the Artifactory fixture."""

        root_path = ArtifactoryPath(self.base_url)
        artifacts = root_path.aql(
            "items.find",
            {
                "repo": repo_name,
                "name": artifact_name,
            },
            ".include",
            # We don't use "repo", but the AQL documentation says that
            # non-admin users must include all of "name", "repo",
            # and "path" in the include directive.
            ["repo", "path", "name", "property"],
            ".limit(1)",
        )

        if not artifacts:
            return

        artifact = artifacts[0]

        properties = {}
        for prop in artifact.get("properties", {}):
            properties[prop["key"]] = prop.get("value", "")

        artifact["properties"] = properties

        return artifact

    def _artifactory_put(
        self, base_url, middle_path, artifact_name, artifact_file
    ):
        """Helper to put a file into the Artifactory fixture."""

        fd, name = tempfile.mkstemp(prefix="temp-download.")
        f = os.fdopen(fd, "wb")

        targetpath = ArtifactoryPath(base_url, middle_path, artifact_name)
        targetpath.parent.mkdir(parents=True, exist_ok=True)

        try:
            artifact_file.open()
            copy_and_close(artifact_file, f)
            targetpath.deploy_file(name, parameters={"test": ["True"]})
        finally:
            f.close()
            Path(name).unlink()

    def test_provides_interface(self):
        # CraftPublishingJob provides ICraftPublishingJob.
        job = getUtility(ICraftPublishingJobSource).create(self.build)

        # Check that the instance provides the job interface
        self.assertProvides(job, ICraftPublishingJob)

        # Check that the class provides the source interface
        self.assertProvides(CraftPublishingJob, ICraftPublishingJobSource)

    def test_create(self):
        # CraftPublishingJob.create creates a CraftPublishingJob with the
        # correct attributes.
        job = getUtility(ICraftPublishingJobSource).create(self.build)

        job = removeSecurityProxy(job)

        self.assertThat(
            job,
            MatchesStructure(
                class_job_type=Equals(
                    CraftRecipeBuildJobType.PUBLISH_ARTIFACTS
                ),
                build=Equals(self.build),
                error_message=Is(None),
            ),
        )

    def test_run_failure_cannot_determine_distribution(self):
        """Test failure when distribution cannot be determined."""
        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertEqual(
            "Could not determine distribution for build",
            job.error_message,
        )

    def test_run_failure_no_configuration(self):
        """Test failure when no configuration is found for the distribution."""
        self._setup_distribution("nonexistent")

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertEqual(
            "No configuration found for nonexistent",
            job.error_message,
        )

    def test_run_no_http_proxy(self):
        """Test that job runs even when no HTTP proxy is configured."""
        self._setup_distribution("soss")
        self._setup_config(with_env_vars=True)  # With env vars but no proxy

        # Set up a fixture to capture log messages
        logger_fixture = self.useFixture(FakeLogger(level=logging.WARNING))

        # Create a crate file
        self._create_crate_file()

        # Mock the _publish_rust_crate method to avoid actual publishing
        self.patch(
            CraftPublishingJob,
            "_publish_rust_crate",
            lambda self, extract_dir, env_vars, artifact_name: None,
        )

        # Run the job
        job = self.run_job(self.build)

        # Verify the warning was logged
        self.assertIn(
            "No HTTP proxy configured for artifact publishing",
            logger_fixture.output,
        )

        # Job should complete successfully despite no proxy
        self.assertEqual(JobStatus.COMPLETED, job.job.status)

    def test_run_no_publishable_artifacts(self):
        """Test failure when no publishable artifacts are found."""
        self._setup_distribution("soss")
        self._setup_config(with_http_proxy=True)

        self._create_invalid_crate_file(type="dummy")

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertEqual(
            "No publishable artifacts found in build",
            job.error_message,
        )

    def test_run_missing_cargo_config(self):
        """Test failure when a crate is found but Cargo config is missing."""
        self._setup_distribution("soss")
        self._setup_config(with_http_proxy=True)

        self._create_crate_file()

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertEqual(
            "Missing Cargo publishing repository configuration",
            job.error_message,
        )

    def test_run_crate_extraction_failure(self):
        """Test failure when crate extraction fails."""
        self._setup_distribution("soss")
        self._setup_config(with_env_vars=True, with_http_proxy=True)

        self._create_invalid_crate_file(type="invalid_tar")

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertIn(
            "Failed to extract crate",
            job.error_message,
        )

    def test_run_crate_extraction_symlink_escape_failure(self):
        """Test failure when crate contains symlink traversal content."""
        self._setup_distribution("soss")
        self._setup_config(with_env_vars=True, with_http_proxy=True)

        self._create_invalid_crate_file(type="symlink_escape")

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertIn(
            "Failed to extract crate",
            job.error_message,
        )

    def test_run_no_directory_in_crate(self):
        """Test failure when no directory is found in extracted crate."""
        self._setup_distribution("soss")
        self._setup_config(with_env_vars=True, with_http_proxy=True)

        self._create_invalid_crate_file(type="no_dirs")

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertEqual(
            "No directory found in extracted crate",
            job.error_message,
        )

    def test_run_cargo_publish_success(self):
        """Test success when a crate is found and Cargo config is present."""
        self._setup_distribution("soss")
        self._setup_config(with_env_vars=True, with_http_proxy=True)

        lfa = self._create_crate_file()

        # Add a metadata file with license information
        license_value = "Apache-2.0"
        metadata_yaml = f"license: {license_value}\nversion: 0.1.0\n"
        librarian = getUtility(ILibraryFileAliasSet)
        metadata_lfa = librarian.create(
            "metadata.yaml",
            len(metadata_yaml),
            MockBytesIO(metadata_yaml.encode("utf-8")),
            "text/x-yaml",
        )
        removeSecurityProxy(self.build).addFile(metadata_lfa)

        # Set a revision ID for the build
        removeSecurityProxy(self.build).revision_id = "random-revision-id"

        # Create a mock return value for subprocess.run
        mock_completed_process = type(
            "MockCompletedProcess",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

        from lp.crafts.model.craftrecipebuildjob import (
            subprocess as crbj_subprocess,
        )

        # Mock subprocess.run to only mock cargo calls
        subprocess_calls = []
        original_run = crbj_subprocess.run

        def mock_run(*args, **kwargs):
            if args and len(args[0]) > 0 and "cargo" in args[0][0]:
                subprocess_calls.append((args, kwargs))
                return mock_completed_process
            return original_run(*args, **kwargs)

        self.patch(crbj_subprocess, "run", mock_run)

        original_publish_properties = CraftPublishingJob._publish_properties

        def mock_cargo_publish_properties(*args, **kwargs):
            """Mock _publish_properties to deploy the crate to Artifactory
            Fixture before testing.

            We need to do this in a nested function here because we need
            to access the `lfa` variable which is created in the this test
            setup above but the mocked function (and the lfa.open()) can
            only be called inside the job's run method."""

            self._artifactory_put(args[1], "crates/test", args[2], lfa)
            return original_publish_properties(*args, **kwargs)

        self.patch(
            CraftPublishingJob,
            "_publish_properties",
            mock_cargo_publish_properties,
        )

        # Create and run the job
        job = self.run_job(self.build)
        self.assertEqual(JobStatus.COMPLETED, job.job.status)

        # Find the call for cargo publish (should be the second call)
        cargo_call = None
        for call in subprocess_calls:
            args = call[0][0]
            if "cargo" in args[0] and "publish" in args:
                cargo_call = call
                break

        self.assertIsNotNone(cargo_call, "No cargo publish command was called")

        # Extract the command arguments and verify them
        args = cargo_call[0][0]
        self.assertEqual("cargo", args[0])
        self.assertEqual("publish", args[1])
        self.assertIn("--no-verify", args)
        self.assertIn("--allow-dirty", args)
        self.assertIn("--registry", args)

        # Check registry argument
        registry_index = args.index("--registry") + 1
        self.assertEqual("launchpad", args[registry_index])

        # Verify that the correct working directory was used
        kwargs = cargo_call[1]
        self.assertIn("cwd", kwargs)
        cwd = kwargs["cwd"]
        self.assertTrue(
            cwd.endswith("test-0.1.0"),
            f"Expected working directory to end with 'test-0.1.0', got {cwd}",
        )

        # Verify CARGO_HOME was set in the environment
        self.assertIn("env", kwargs)
        env = kwargs["env"]
        self.assertIn("CARGO_HOME", env)

        # Verify that the artifact's metadata were uploaded to Artifactory
        artifact = self._artifactory_search("repository", lfa.filename)

        self.assertIsNotNone(artifact, "Artifact not found in Artifactory")

        self.assertEqual(artifact["repo"], "repository")
        self.assertEqual(artifact["name"], lfa.filename)

        # Our artifactory fixture preserves the "index/" path used during
        # publishing. This is not the case in a real Artifactory
        # instance, so we need to remove the "index/" prefix.
        path_without_index = artifact["path"].replace("index/", "")
        self.assertEqual(path_without_index, "crates/test")

        self.assertEqual(
            artifact["properties"]["soss.commit_id"], "random-revision-id"
        )
        self.assertEqual(
            artifact["properties"]["soss.source_url"],
            removeSecurityProxy(self.recipe).git_repository.git_https_url,
        )
        self.assertEqual(artifact["properties"]["soss.type"], "source")
        self.assertEqual(artifact["properties"]["soss.license"], license_value)
        self.assertEqual(
            artifact["properties"].get("launchpad.channel"),
            "noble:0.1.0/stable",
        )

    def test_run_missing_maven_config(self):
        """
        Test failure when Maven artifacts are found but Maven config is
        missing.
        """
        self._setup_distribution("soss")
        self._setup_config(with_http_proxy=True)

        # Create a dummy jar file
        from io import BytesIO

        dummy_jar_content = b"dummy jar content"

        # Create a LibraryFileAlias with the jar content
        librarian = getUtility(ILibraryFileAliasSet)
        jar_lfa = librarian.create(
            "test-0.1.0.jar",
            len(dummy_jar_content),
            BytesIO(dummy_jar_content),
            "application/java-archive",
        )

        # Create a dummy pom file
        dummy_pom_content = b"<project>...</project>"

        # Create a LibraryFileAlias with the pom content
        pom_lfa = librarian.create(
            "pom.xml",
            len(dummy_pom_content),
            BytesIO(dummy_pom_content),
            "application/xml",
        )

        # Add the files to the build
        removeSecurityProxy(self.build).addFile(jar_lfa)
        removeSecurityProxy(self.build).addFile(pom_lfa)

        # Create and run the job
        job = self.run_job(self.build)

        self.assertEqual(JobStatus.FAILED, job.job.status)
        self.assertEqual(
            "Missing Maven publishing repository configuration",
            job.error_message,
        )

    def test_run_maven_deploy_success(self):
        """Test successful Maven artifact publishing."""
        self._setup_distribution("soss")
        self._setup_config(with_env_vars=True, with_http_proxy=True)

        # Create a dummy jar file
        dummy_jar_content = b"dummy jar content"
        librarian = getUtility(ILibraryFileAliasSet)
        jar_lfa = librarian.create(
            "test-artifact-0.1.0.jar",
            len(dummy_jar_content),
            io.BytesIO(dummy_jar_content),
            "application/java-archive",
        )

        # Create a dummy pom file
        pom_content = """
<project>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>test-artifact</artifactId>
    <version>0.1.0</version>
</project>
"""
        pom_bytes = pom_content.encode("utf-8")
        pom_lfa = librarian.create(
            "pom.xml",
            len(pom_bytes),
            io.BytesIO(pom_bytes),
            "application/xml",
        )

        # Add the files to the build
        removeSecurityProxy(self.build).addFile(jar_lfa)
        removeSecurityProxy(self.build).addFile(pom_lfa)

        # Create a metadata file with license information
        license_value = "Apache-2.0"
        metadata_yaml = f"license: {license_value}\nversion: 0.1.0\n"
        librarian = getUtility(ILibraryFileAliasSet)
        metadata_lfa = librarian.create(
            "metadata.yaml",
            len(metadata_yaml),
            MockBytesIO(metadata_yaml.encode("utf-8")),
            "text/x-yaml",
        )
        removeSecurityProxy(self.build).addFile(metadata_lfa)

        # Set a revision ID for the build
        removeSecurityProxy(self.build).revision_id = "random-revision-id"

        # Create a mock return value for subprocess.run
        mock_completed_process = type(
            "MockCompletedProcess",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

        from lp.crafts.model.craftrecipebuildjob import (
            subprocess as crbj_subprocess,
        )

        # Mock subprocess.run to only mock mvn calls
        subprocess_calls = []
        original_run = crbj_subprocess.run

        def mock_run(*args, **kwargs):
            if args and len(args[0]) > 0 and "mvn" in args[0][0]:
                subprocess_calls.append((args, kwargs))
                return mock_completed_process
            return original_run(*args, **kwargs)

        self.patch(crbj_subprocess, "run", mock_run)

        original_publish_properties = CraftPublishingJob._publish_properties

        def mock_maven_publish_properties(*args, **kwargs):
            """Mock _publish_properties to deploy the crate to Artifactory
            Fixture before testing.

            We need to do this in a nested function here because we need
            to access the `lfa` variable which is created in the this test
            setup above but the mocked function (and the lfa.open()) can
            only be called inside the job's run method."""

            self._artifactory_put(
                args[1], "com/example/test-artifact/0.1.0", args[2], jar_lfa
            )
            return original_publish_properties(*args, **kwargs)

        self.patch(
            CraftPublishingJob,
            "_publish_properties",
            mock_maven_publish_properties,
        )

        job = self.run_job(self.build)

        self.assertEqual(JobStatus.COMPLETED, job.job.status)

        # Find the call for mvn deploy
        mvn_call = None
        for call in subprocess_calls:
            args = call[0][0]
            if "mvn" in args[0] and "deploy:deploy-file" in args:
                mvn_call = call
                break

        self.assertIsNotNone(mvn_call, "No mvn deploy command was called")

        # Extract the command arguments and verify them
        args = mvn_call[0][0]
        self.assertEqual("mvn", args[0])
        self.assertEqual("deploy:deploy-file", args[1])

        # Convert args to a string to make it easier to check for parameters
        args_str = " ".join(args)

        # Check for required Maven arguments
        self.assertIn("-DrepositoryId=artifactory", args_str)
        self.assertIn(
            "-Durl=https://example.com/artifactory/repository", args_str
        )

        # Verify that the POM and JAR files are correctly referenced
        pom_found = False
        jar_found = False
        for arg in args:
            if arg.startswith("-DpomFile=") and "pom.xml" in arg:
                pom_found = True
            if arg.startswith("-Dfile=") and ".jar" in arg:
                jar_found = True

        self.assertTrue(
            pom_found, "POM file parameter not found in Maven command"
        )
        self.assertTrue(
            jar_found, "JAR file parameter not found in Maven command"
        )

        # Verify settings file path is included
        settings_found = False
        for arg in args:
            if arg.startswith("--settings=") and ".m2/settings.xml" in arg:
                settings_found = True

        self.assertTrue(
            settings_found, "Maven settings file path not found in command"
        )

        # Verify working directory was set correctly
        kwargs = mvn_call[1]
        self.assertIn("cwd", kwargs)

        artifact = self._artifactory_search("repository", jar_lfa.filename)

        # Verify that the artifact's metadata were uploaded to Artifactory
        self.assertIsNotNone(artifact, "Artifact not found in Artifactory")
        self.assertEqual(artifact["repo"], "repository")
        self.assertEqual(artifact["name"], jar_lfa.filename)

        # Our artifactory fixture preserves the "index/" path used during
        # publishing. This is not the case in a real Artifactory
        # instance, so we need to remove the "index/" prefix.
        path_without_index = artifact["path"].replace("index/", "")
        self.assertEqual(path_without_index, "com/example/test-artifact/0.1.0")
        self.assertEqual(
            artifact["properties"]["soss.commit_id"], "random-revision-id"
        )
        self.assertEqual(
            artifact["properties"]["soss.source_url"],
            removeSecurityProxy(self.recipe).git_repository.git_https_url,
        )
        self.assertEqual(artifact["properties"]["soss.type"], "source")
        self.assertEqual(artifact["properties"]["soss.license"], license_value)
        self.assertEqual(
            artifact["properties"].get("launchpad.channel"),
            "noble:0.1.0/stable",
        )

    def test__publish_properties_sets_expected_properties(self):
        """Test that _publish_properties sets the correct properties in
        Artifactory."""
        # Arrange
        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)

        # Patch _recipe_git_url and _get_license_metadata for deterministic
        # output
        self.patch(
            CraftPublishingJob,
            "_recipe_git_url",
            lambda self: "https://example.com/repo.git",
        )
        self.patch(
            CraftPublishingJob, "_get_license_metadata", lambda self: "MIT"
        )

        removeSecurityProxy(self.build).revision_id = "random-revision-id"

        self._artifactory_put(
            f"{self.base_url}/{self.repository_name}",
            "middle_folder",
            "artifact.file",
            MockBytesIO(b"dummy content"),
        )
        job._publish_properties(self.publish_url, "artifact.file")
        artifact = self._artifactory_search("repository", "artifact.file")

        self.assertIsNotNone(artifact, "Artifact not found in Artifactory")

        props = artifact["properties"]
        self.assertIn("soss.commit_id", props)
        self.assertIn("soss.source_url", props)
        self.assertIn("soss.type", props)
        self.assertIn("soss.license", props)
        self.assertEqual(props["soss.commit_id"], job.build.revision_id)
        self.assertEqual(
            props["soss.source_url"], "https://example.com/repo.git"
        )
        self.assertEqual(props["soss.type"], "source")
        self.assertEqual(props["soss.license"], "MIT")
        self.assertIn("launchpad.channel", props)
        self.assertEqual(props["launchpad.channel"], "noble:unknown/stable")

    def test__publish_properties_artifact_not_found(self):
        """Test that _publish_properties raises NotFoundError if artifact is
        missing."""
        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)

        from lp.app.errors import NotFoundError

        self.assertRaises(
            NotFoundError,
            job._publish_properties,
            self.publish_url,
            "missing-artifact.file",
        )

    def test__publish_properties_no_metadata_yaml(self):
        """Test that _publish_properties sets license to 'unknown' if no
        metadata.yaml is present."""

        self._artifactory_put(
            f"{self.base_url}/{self.repository_name}",
            "some/path",
            "artifact.file",
            MockBytesIO(b"dummy content"),
        )

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)
        job.run = lambda: job._publish_properties(
            self.publish_url, "artifact.file"
        )

        self.patch(
            CraftPublishingJob,
            "_recipe_git_url",
            lambda self: "https://example.com/repo.git",
        )

        # Preserve the virtual memory resource limit because runAll changes it
        # which causes the whole test worker process to have limited memory
        with preserve_rlimit(RLIMIT_AS):
            JobRunner([job]).runAll()

        artifact = self._artifactory_search("repository", "artifact.file")
        self.assertEqual(artifact["properties"]["soss.license"], "unknown")
        self.assertEqual(
            artifact["properties"].get("launchpad.channel"),
            "noble:unknown/stable",
        )

    def test__publish_properties_no_license_in_metadata_yaml(self):
        """Test that _publish_properties sets license to 'unknown' if no
        license is specified in metadata.yaml."""

        # Create a broken metadata.yaml file with a license
        metadata_yaml = "no_license: True\n"
        librarian = getUtility(ILibraryFileAliasSet)
        metadata_lfa = librarian.create(
            "metadata.yaml",
            len(metadata_yaml),
            MockBytesIO(metadata_yaml.encode("utf-8")),
            "text/x-yaml",
        )
        removeSecurityProxy(self.build).addFile(metadata_lfa)

        self._artifactory_put(
            f"{self.base_url}/{self.repository_name}",
            "some/path",
            "artifact.file",
            MockBytesIO(b"dummy content"),
        )

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)
        job.run = lambda: job._publish_properties(
            self.publish_url, "artifact.file"
        )

        self.patch(
            CraftPublishingJob,
            "_recipe_git_url",
            lambda self: "https://example.com/repo.git",
        )

        # Preserve the virtual memory resource limit because runAll changes it
        # which causes the whole test worker process to have limited memory
        with preserve_rlimit(RLIMIT_AS):
            JobRunner([job]).runAll()

        artifact = self._artifactory_search("repository", "artifact.file")
        self.assertEqual(artifact["properties"]["soss.license"], "unknown")
        self.assertEqual(
            artifact["properties"].get("launchpad.channel"),
            "noble:unknown/stable",
        )

    def test__publish_properties_license_from_metadata_yaml(self):
        """Test that _publish_properties gets license from metadata.yaml
        if present."""

        # Create a metadata.yaml file with a license
        license_value = "Apache-2.0"
        metadata_yaml = f"license: {license_value}\nversion: 0.1.0\n"
        librarian = getUtility(ILibraryFileAliasSet)
        metadata_lfa = librarian.create(
            "metadata.yaml",
            len(metadata_yaml),
            MockBytesIO(metadata_yaml.encode("utf-8")),
            "text/x-yaml",
        )
        removeSecurityProxy(self.build).addFile(metadata_lfa)

        self._artifactory_put(
            f"{self.base_url}/{self.repository_name}",
            "some/path",
            "artifact.file",
            MockBytesIO(b"dummy content"),
        )

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)
        job.run = lambda: job._publish_properties(
            self.publish_url, "artifact.file"
        )

        self.patch(
            CraftPublishingJob,
            "_recipe_git_url",
            lambda self: "https://example.com/repo.git",
        )

        # Preserve the virtual memory resource limit because runAll changes it
        # which causes the whole test worker process to have limited memory
        with preserve_rlimit(RLIMIT_AS):
            JobRunner([job]).runAll()

        artifact = self._artifactory_search("repository", "artifact.file")
        self.assertEqual(artifact["properties"]["soss.license"], license_value)
        self.assertEqual(
            artifact["properties"].get("launchpad.channel"),
            "noble:0.1.0/stable",
        )

    def test__publish_properties_git_repository_source_url(self):
        """Test that _publish_properties gets git_repository as source_url."""

        self._artifactory_put(
            f"{self.base_url}/{self.repository_name}",
            "some/path",
            "artifact.file",
            MockBytesIO(b"dummy content"),
        )

        git_repository = self.factory.makeGitRepository()
        removeSecurityProxy(self.recipe).git_repository = git_repository

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)

        self.patch(
            CraftPublishingJob, "_get_license_metadata", lambda self: "MIT"
        )

        job._publish_properties(self.publish_url, "artifact.file")

        artifact = self._artifactory_search("repository", "artifact.file")
        self.assertEqual(
            artifact["properties"]["soss.source_url"],
            git_repository.git_https_url,
        )

    def test__publish_properties_git_repository_url_source_url(self):
        """Test that _publish_properties gets git_repository_url as
        source_url."""

        self._artifactory_put(
            f"{self.base_url}/{self.repository_name}",
            "some/path",
            "artifact.file",
            MockBytesIO(b"dummy content"),
        )

        git_url_recipe = self.factory.makeCraftRecipe(
            git_ref=self.factory.makeGitRefRemote()
        )
        git_url_build = self.factory.makeCraftRecipeBuild(
            recipe=git_url_recipe
        )

        job = getUtility(ICraftPublishingJobSource).create(git_url_build)
        job = removeSecurityProxy(job)

        self.patch(
            CraftPublishingJob, "_get_license_metadata", lambda self: "MIT"
        )

        job._publish_properties(self.publish_url, "artifact.file")

        artifact = self._artifactory_search("repository", "artifact.file")
        self.assertEqual(
            artifact["properties"]["soss.source_url"],
            git_url_recipe.git_repository_url,
        )

    def test__extract_root_path_no_https(self):
        """Test that the _extract_root_path method return empty string
        when the URL does not contain 'https'."""

        url = "http://example.com/path/to/artifactory/file.txt"

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)

        self.assertEqual(job._extract_root_path(url), "")

    def test__extract_root_path_no_artifactory(self):
        """Test that _extract_root_path returns empty string when 'artifactory'
        is not in the URL."""

        url = "https://example.com/path/to/somewhere/file.txt"

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)

        self.assertEqual(job._extract_root_path(url), "")

    def test__extract_root_path(self):
        """Test that _extract_root_path extracts the correct root path from an
        https:// URL.

        We expect the root path to be everything after the 'https',
        before and including the 'artifactory'."""
        url = "https://example.com/path/to/artifactory/file.txt"
        expected_root = "https://example.com/path/to/artifactory"

        job = getUtility(ICraftPublishingJobSource).create(self.build)
        job = removeSecurityProxy(job)

        root_path = job._extract_root_path(url)
        self.assertEqual(expected_root, root_path)
