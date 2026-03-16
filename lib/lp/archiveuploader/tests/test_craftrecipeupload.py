# Copyright 2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `CraftRecipeUpload`."""

import os
import tarfile
import tempfile

import yaml
from storm.store import Store
from zope.security.proxy import removeSecurityProxy

from lp.archiveuploader.tests.test_uploadprocessor import (
    TestUploadProcessorBase,
)
from lp.archiveuploader.uploadprocessor import UploadHandler, UploadStatusEnum
from lp.buildmaster.enums import BuildStatus
from lp.crafts.interfaces.craftrecipe import CRAFT_RECIPE_ALLOW_CREATE
from lp.services.features.testing import FeatureFixture
from lp.services.osutils import write_file


class TestCraftRecipeUploads(TestUploadProcessorBase):
    """End-to-end tests of craft recipe uploads."""

    def setUp(self):
        super().setUp()
        self.useFixture(FeatureFixture({CRAFT_RECIPE_ALLOW_CREATE: "on"}))

        self.setupBreezy()

        self.switchToAdmin()
        self.build = self.factory.makeCraftRecipeBuild(
            distro_arch_series=self.breezy["i386"]
        )
        self.build.updateStatus(BuildStatus.UPLOADING)
        Store.of(self.build).flush()
        self.switchToUploader()
        self.options.context = "buildd"

        self.uploadprocessor = self.getUploadProcessor(
            self.layer.txn, builds=True
        )

    def _createArchiveWithCrate(
        self, upload_dir, crate_name="test-crate", crate_version="0.1.0"
    ):
        """Helper to create a tar.xz archive containing a crate & metadata."""
        # Create a temporary directory to build our archive
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create metadata.yaml
            metadata = {
                "name": crate_name,
                "version": crate_version,
            }
            metadata_path = os.path.join(tmpdir, "metadata.yaml")
            with open(metadata_path, "w") as f:
                yaml.safe_dump(metadata, f)

            # Create dummy crate file
            crate_path = os.path.join(
                tmpdir, f"{crate_name}-{crate_version}.crate"
            )
            with open(crate_path, "wb") as f:
                f.write(b"dummy crate contents")

            # Create tar.xz archive
            archive_path = os.path.join(upload_dir, "output.tar.xz")
            with tarfile.open(archive_path, "w:xz") as tar:
                tar.add(metadata_path, arcname="metadata.yaml")
                tar.add(crate_path, arcname=os.path.basename(crate_path))

            return archive_path

    def _createArchiveWithoutArtifacts(
        self, upload_dir, filename="output.tar.xz"
    ):
        """Helper to create a tar.xz archive without artifact files."""
        archive_path = os.path.join(upload_dir, filename)
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)

        with tarfile.open(archive_path, "w:xz") as tar:
            # Add a dummy file
            with tempfile.NamedTemporaryFile(mode="w") as tmp:
                tmp.write("test content")
                tmp.flush()
                tar.add(tmp.name, arcname="test.txt")

        return archive_path

    def _createArchiveWithJarAndPom(
        self, upload_dir, jar_name="test-jar", jar_version="0.1.0"
    ):
        """
        Helper to create a tar.xz archive containing a JAR, POM, and metadata.
        """
        # Create a temporary directory to build our archive
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create metadata.yaml
            metadata = {
                "name": jar_name,
                "version": jar_version,
            }
            metadata_path = os.path.join(tmpdir, "metadata.yaml")
            with open(metadata_path, "w") as f:
                yaml.safe_dump(metadata, f)

            # Create dummy JAR file
            jar_path = os.path.join(tmpdir, f"{jar_name}-{jar_version}.jar")
            with open(jar_path, "wb") as f:
                f.write(b"dummy jar contents")

            # Create dummy POM file
            pom_content = (
                "<project>\n"
                "  <modelVersion>4.0.0</modelVersion>\n"
                "  <groupId>com.example</groupId>\n"
                f"  <artifactId>{jar_name}</artifactId>\n"
                f"  <version>{jar_version}</version>\n"
                "</project>"
            )
            pom_path = os.path.join(tmpdir, "pom.xml")
            with open(pom_path, "w") as f:
                f.write(pom_content)

            # Create tar.xz archive
            archive_path = os.path.join(upload_dir, "output.tar.xz")
            with tarfile.open(archive_path, "w:xz") as tar:
                tar.add(metadata_path, arcname="metadata.yaml")
                tar.add(jar_path, arcname=os.path.basename(jar_path))
                tar.add(pom_path, arcname="pom.xml")

            return archive_path

    def test_processes_artifact_from_archive(self):
        """Test that artifacts are properly extracted and processed
        from archives."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create archive with specific crate name and version
        crate_name = "test-crate"
        crate_version = "0.2.0"
        self._createArchiveWithCrate(upload_dir, crate_name, crate_version)

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload succeeded
        self.assertEqual(UploadStatusEnum.ACCEPTED, result)
        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)

        # Verify crate and metadata were stored (not the archive)
        build = removeSecurityProxy(self.build)
        files = list(build.getFiles())
        self.assertEqual(2, len(files))

        filenames = {f[1].filename for f in files}
        expected_files = {
            f"{crate_name}-{crate_version}.crate",
            "metadata.yaml",
        }
        self.assertEqual(expected_files, filenames)

    def test_uploads_archive_without_artifacts(self):
        """Test that the original archive is uploaded when no artifact
        files exist."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create archive without artifact files
        archive_name = "test-output.tar.xz"
        self._createArchiveWithoutArtifacts(upload_dir, archive_name)

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload succeeded
        self.assertEqual(UploadStatusEnum.ACCEPTED, result)
        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)

        # Verify the original archive was stored
        build = removeSecurityProxy(self.build)
        files = list(build.getFiles())
        self.assertEqual(1, len(files))
        stored_file = files[0][1]
        self.assertEqual(archive_name, stored_file.filename)

    def test_rejects_path_traversal_in_archive(self):
        """Unsafe tar members (path traversal) are rejected (CVE-2007-4559)."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)
        archive_path = os.path.join(upload_dir, "malicious.tar.xz")
        with tarfile.open(archive_path, "w:xz") as tar:
            # Add a benign file so the archive is valid
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, suffix=".yaml"
            ) as tmp:
                tmp.write(b"name: x\nversion: 1.0\n")
                tmp.flush()
                tar.add(tmp.name, arcname="metadata.yaml")
            # Add a member that would escape the extraction dir
            info = tarfile.TarInfo(name="../../../etc/escape")
            info.size = 0
            tar.addfile(info)

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        self.assertEqual(UploadStatusEnum.REJECTED, result)
        self.assertIn("Unsafe archive content", self.log.getLogBuffer())
        self.assertIn("outside the destination", self.log.getLogBuffer())

    def test_requires_craft(self):
        """Test that the upload fails if no .tar.xz files are found."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        write_file(os.path.join(upload_dir, "foo_0_all.manifest"), b"manifest")

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        self.assertEqual(UploadStatusEnum.REJECTED, result)
        self.assertIn(
            "ERROR Build did not produce any tar.xz archives.",
            self.log.getLogBuffer(),
        )
        self.assertFalse(self.build.verifySuccessfulUpload())

    def test_processes_all_files(self):
        """Test that all files in subdirectories are processed."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create archive with crate and additional files
        crate_name = "test-crate"
        crate_version = "0.2.0"
        self._createArchiveWithCrate(upload_dir, crate_name, crate_version)

        # Add additional files in subdirectories
        subdir = os.path.join(upload_dir, "subdir")
        os.makedirs(subdir, exist_ok=True)

        # Create manifest file
        manifest_path = os.path.join(subdir, "build.manifest")
        with open(manifest_path, "w") as f:
            f.write('{"type": "test"}')

        # Create log file
        log_path = os.path.join(subdir, "build.log")
        with open(log_path, "w") as f:
            f.write("Build log contents")

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload succeeded
        self.assertEqual(UploadStatusEnum.ACCEPTED, result)
        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)

        # Verify all files were stored
        build = removeSecurityProxy(self.build)
        files = list(build.getFiles())

        # Should have: crate, metadata.yaml, build.manifest, build.log
        self.assertEqual(4, len(files))

        filenames = {f[1].filename for f in files}
        expected_files = {
            f"{crate_name}-{crate_version}.crate",
            "metadata.yaml",
            "build.manifest",
            "build.log",
        }
        self.assertEqual(expected_files, filenames)

    def test_processes_all_files_without_crate(self):
        """Test that all files are processed when no artifact is present."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create archive without crate
        archive_name = "test-output.tar.xz"
        self._createArchiveWithoutArtifacts(upload_dir, archive_name)

        # Add additional files in subdirectories
        subdir = os.path.join(upload_dir, "subdir")
        os.makedirs(subdir, exist_ok=True)

        # Create manifest file
        manifest_path = os.path.join(subdir, "build.manifest")
        with open(manifest_path, "w") as f:
            f.write('{"type": "test"}')

        # Create log file
        log_path = os.path.join(subdir, "build.log")
        with open(log_path, "w") as f:
            f.write("Build log contents")

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload succeeded
        self.assertEqual(UploadStatusEnum.ACCEPTED, result)
        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)

        # Verify all files were stored
        build = removeSecurityProxy(self.build)
        files = list(build.getFiles())

        # Should have: archive, build.manifest, build.log
        self.assertEqual(3, len(files))

        filenames = {f[1].filename for f in files}
        expected_files = {
            archive_name,
            "build.manifest",
            "build.log",
        }
        self.assertEqual(expected_files, filenames)

    def test_processes_jar_and_pom_from_archive(self):
        """Test that JAR and POM files are properly extracted and processed
        from archives."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create archive with specific jar name and version
        jar_name = "test-jar"
        jar_version = "0.2.0"
        self._createArchiveWithJarAndPom(upload_dir, jar_name, jar_version)

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload succeeded
        self.assertEqual(UploadStatusEnum.ACCEPTED, result)
        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)

        # Verify JAR, POM, and metadata were stored (not the archive)
        build = removeSecurityProxy(self.build)
        files = list(build.getFiles())
        self.assertEqual(3, len(files))

        filenames = {f[1].filename for f in files}
        expected_files = {
            f"{jar_name}-{jar_version}.jar",
            "pom.xml",
            "metadata.yaml",
        }
        self.assertEqual(expected_files, filenames)

    def test_processes_on_multiple_jars(self):
        """Test that multiple JARs and POM files are properly extracted and
        processed from archives."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create a temporary directory to build our archive
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create metadata.yaml
            metadata = {
                "name": "test-artifact",
                "version": "0.1.0",
            }
            metadata_path = os.path.join(tmpdir, "metadata.yaml")
            with open(metadata_path, "w") as f:
                yaml.safe_dump(metadata, f)

            # Create multiple JAR files
            jar_path1 = os.path.join(tmpdir, "test-artifact-0.1.0.jar")
            with open(jar_path1, "wb") as f:
                f.write(b"dummy jar contents")

            jar_path2 = os.path.join(tmpdir, "another-jar-0.2.0.jar")
            with open(jar_path2, "wb") as f:
                f.write(b"another jar contents")

            # Create POM file
            pom_path = os.path.join(tmpdir, "pom.xml")
            with open(pom_path, "w") as f:
                f.write("<project></project>")

            # Create tar.xz archive
            archive_path = os.path.join(upload_dir, "output.tar.xz")
            with tarfile.open(archive_path, "w:xz") as tar:
                tar.add(metadata_path, arcname="metadata.yaml")
                tar.add(jar_path1, arcname=os.path.basename(jar_path1))
                tar.add(jar_path2, arcname=os.path.basename(jar_path2))
                tar.add(pom_path, arcname="pom.xml")

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload succeeded
        self.assertEqual(UploadStatusEnum.ACCEPTED, result)
        self.assertEqual(BuildStatus.FULLYBUILT, self.build.status)

        self.assertIn(
            "Uploading 2 JAR file(s)",
            self.log.getLogBuffer(),
        )

        # Verify JARs, POM, and metadata were stored (not the archive)
        build = removeSecurityProxy(self.build)
        files = list(build.getFiles())
        self.assertEqual(4, len(files))

        filenames = {f[1].filename for f in files}
        expected_files = {
            "test-artifact-0.1.0.jar",
            "another-jar-0.2.0.jar",
            "pom.xml",
            "metadata.yaml",
        }
        self.assertEqual(expected_files, filenames)

    def test_errors_on_multiple_artifact_types(self):
        """Test that an error is raised when both crate and Maven artifacts
        are present in the same archive."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create a temporary directory to build our archive
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create metadata.yaml
            metadata = {
                "name": "test-artifact",
                "version": "0.1.0",
            }
            metadata_path = os.path.join(tmpdir, "metadata.yaml")
            with open(metadata_path, "w") as f:
                yaml.safe_dump(metadata, f)

            # Create dummy crate file
            crate_path = os.path.join(tmpdir, "test-artifact-0.1.0.crate")
            with open(crate_path, "wb") as f:
                f.write(b"dummy crate contents")

            # Create dummy JAR file
            jar_path = os.path.join(tmpdir, "test-artifact-0.1.0.jar")
            with open(jar_path, "wb") as f:
                f.write(b"dummy jar contents")

            # Create dummy POM file
            pom_path = os.path.join(tmpdir, "pom.xml")
            with open(pom_path, "w") as f:
                f.write("<project></project>")

            # Create tar.xz archive
            archive_path = os.path.join(upload_dir, "output.tar.xz")
            with tarfile.open(archive_path, "w:xz") as tar:
                tar.add(metadata_path, arcname="metadata.yaml")
                tar.add(crate_path, arcname=os.path.basename(crate_path))
                tar.add(jar_path, arcname=os.path.basename(jar_path))
                tar.add(pom_path, arcname="pom.xml")

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload was rejected
        self.assertEqual(UploadStatusEnum.REJECTED, result)
        self.assertIn(
            "ERROR Archive contains both Rust and Java artifacts",
            self.log.getLogBuffer(),
        )
        self.assertFalse(self.build.verifySuccessfulUpload())

    def test_errors_on_multiple_crates(self):
        """Test that an error is raised when multiple crates are present."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create a temporary directory to build our archive
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create metadata.yaml
            metadata = {
                "name": "test-artifact",
                "version": "0.1.0",
            }
            metadata_path = os.path.join(tmpdir, "metadata.yaml")
            with open(metadata_path, "w") as f:
                yaml.safe_dump(metadata, f)

            # Create multiple crate files
            crate_path1 = os.path.join(tmpdir, "test-artifact-0.1.0.crate")
            with open(crate_path1, "wb") as f:
                f.write(b"dummy crate contents")

            crate_path2 = os.path.join(tmpdir, "another-crate-0.2.0.crate")
            with open(crate_path2, "wb") as f:
                f.write(b"another crate contents")

            # Create tar.xz archive
            archive_path = os.path.join(upload_dir, "output.tar.xz")
            with tarfile.open(archive_path, "w:xz") as tar:
                tar.add(metadata_path, arcname="metadata.yaml")
                tar.add(crate_path1, arcname=os.path.basename(crate_path1))
                tar.add(crate_path2, arcname=os.path.basename(crate_path2))

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload was rejected
        self.assertEqual(UploadStatusEnum.REJECTED, result)
        self.assertIn(
            "ERROR Archive contains multiple Rust crate files",
            self.log.getLogBuffer(),
        )
        self.assertFalse(self.build.verifySuccessfulUpload())

    def test_errors_on_multiple_poms(self):
        """Test that an error is raised when multiple POM files are present."""
        upload_dir = os.path.join(
            self.incoming_folder, "test", str(self.build.id)
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Create a temporary directory to build our archive
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create metadata.yaml
            metadata = {
                "name": "test-artifact",
                "version": "0.1.0",
            }
            metadata_path = os.path.join(tmpdir, "metadata.yaml")
            with open(metadata_path, "w") as f:
                yaml.safe_dump(metadata, f)

            # Create JAR file
            jar_path = os.path.join(tmpdir, "test-artifact-0.1.0.jar")
            with open(jar_path, "wb") as f:
                f.write(b"dummy jar contents")

            # Create multiple POM files
            pom_path1 = os.path.join(tmpdir, "pom.xml")
            with open(pom_path1, "w") as f:
                f.write("<project></project>")

            pom_path2 = os.path.join(tmpdir, "subdir/pom.xml")
            os.makedirs(os.path.join(tmpdir, "subdir"), exist_ok=True)
            with open(pom_path2, "w") as f:
                f.write("<project>another</project>")

            # Create tar.xz archive
            archive_path = os.path.join(upload_dir, "output.tar.xz")
            with tarfile.open(archive_path, "w:xz") as tar:
                tar.add(metadata_path, arcname="metadata.yaml")
                tar.add(jar_path, arcname=os.path.basename(jar_path))
                tar.add(pom_path1, arcname="pom.xml")
                tar.add(pom_path2, arcname="subdir/pom.xml")

        handler = UploadHandler.forProcessor(
            self.uploadprocessor, self.incoming_folder, "test", self.build
        )
        result = handler.processCraftRecipe(self.log)

        # Verify upload was rejected
        self.assertEqual(UploadStatusEnum.REJECTED, result)
        self.assertIn(
            "ERROR Archive contains multiple pom.xml files",
            self.log.getLogBuffer(),
        )
        self.assertFalse(self.build.verifySuccessfulUpload())
