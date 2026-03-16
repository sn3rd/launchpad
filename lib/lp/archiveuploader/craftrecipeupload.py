# Copyright 2024 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Process a craft recipe upload."""

__all__ = [
    "CraftRecipeUpload",
]

import os
import tarfile
import tempfile
from pathlib import Path

import yaml
from zope.component import getUtility

from lp.archiveuploader.utils import (
    SafeTarExtractError,
    UploadError,
    safe_extract_tar,
)
from lp.buildmaster.enums import BuildStatus
from lp.services.helpers import filenameToContentType
from lp.services.librarian.interfaces import ILibraryFileAliasSet


class CraftRecipeUpload:
    # XXX: ruinedyourlife 03-10-2024
    # This could be refactored into a single generic class used for all
    # recipe uploaders.
    """A craft recipe upload."""

    def __init__(self, upload_path, logger):
        """Create a `CraftRecipeUpload`.

        :param upload_path: A directory containing files to upload.
        :param logger: The logger to be used.
        """
        self.upload_path = upload_path
        self.logger = logger

        self.librarian = getUtility(ILibraryFileAliasSet)

    def process(self, build):
        """Process this upload, loading it into the database."""
        self.logger.debug("Beginning processing.")

        # Find all files in subdirectories
        upload_path = Path(self.upload_path)
        found_tar = False
        craft_paths = []
        other_files = []

        # Collect all files, separating tar.xz from others
        for path in upload_path.rglob("*"):
            if path.parent == upload_path:
                # Skip files directly in upload_path
                continue
            if path.is_file():
                if path.name.endswith(".tar.xz"):
                    found_tar = True
                    craft_paths.append(path)
                else:
                    other_files.append(path)

        if not found_tar:
            raise UploadError("Build did not produce any tar.xz archives.")

        # Upload all non-tar.xz files first
        for path in sorted(other_files):
            with open(path, "rb") as file:
                libraryfile = self.librarian.create(
                    path.name,
                    os.stat(path).st_size,
                    file,
                    filenameToContentType(str(path)),
                    restricted=build.is_private,
                )
            build.addFile(libraryfile)

        # Process tar.xz files (safe extraction: no path traversal or symlinks)
        for craft_path in sorted(craft_paths):
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    with tarfile.open(craft_path, "r:xz") as tar:
                        safe_extract_tar(tar, tmpdir)
                except SafeTarExtractError as e:
                    raise UploadError("Unsafe archive content: %s" % e) from e

                # Look for .crate files, .jar files, pom.xml, and metadata.yaml
                crate_files = list(Path(tmpdir).rglob("*.crate"))
                jar_files = list(Path(tmpdir).rglob("*.jar"))
                pom_files = list(Path(tmpdir).rglob("pom.xml"))
                metadata_path = Path(tmpdir) / "metadata.yaml"

                # Check for multiple artifact types
                has_crate = bool(crate_files)
                has_maven = bool(jar_files and pom_files)

                if has_crate and has_maven:
                    raise UploadError(
                        "Archive contains both Rust and Java artifacts. "
                        "Only one artifact type is allowed per build."
                    )

                # Check for multiple artifacts of the same type
                if len(crate_files) > 1:
                    raise UploadError(
                        "Archive contains multiple Rust crate files. "
                        "Only one .crate file is allowed per build."
                    )

                if len(pom_files) > 1:
                    raise UploadError(
                        "Archive contains multiple pom.xml files. "
                        "Only one pom.xml file is allowed per build."
                    )

                if crate_files and metadata_path.exists():
                    try:
                        metadata = yaml.safe_load(metadata_path.read_text())
                        crate_name = metadata.get("name")
                        crate_version = metadata.get("version")
                        self.logger.debug(
                            "Found crate %s version %s",
                            crate_name,
                            crate_version,
                        )
                    except Exception as e:
                        self.logger.warning(
                            "Failed to parse metadata.yaml: %s", e
                        )

                    # Upload the crate file
                    crate_path = crate_files[0]
                    with open(crate_path, "rb") as file:
                        libraryfile = self.librarian.create(
                            os.path.basename(str(crate_path)),
                            os.stat(crate_path).st_size,
                            file,
                            filenameToContentType(str(crate_path)),
                            restricted=build.is_private,
                        )
                    build.addFile(libraryfile)

                    # Upload metadata.yaml
                    with open(metadata_path, "rb") as file:
                        libraryfile = self.librarian.create(
                            "metadata.yaml",
                            os.stat(metadata_path).st_size,
                            file,
                            filenameToContentType(str(metadata_path)),
                            restricted=build.is_private,
                        )
                    build.addFile(libraryfile)
                elif jar_files and pom_files and metadata_path.exists():
                    try:
                        metadata = yaml.safe_load(metadata_path.read_text())
                        jar_name = metadata.get("name")
                        jar_version = metadata.get("version")
                        self.logger.debug(
                            "Found jar %s version %s",
                            jar_name,
                            jar_version,
                        )
                    except Exception as e:
                        self.logger.warning(
                            "Failed to parse metadata.yaml: %s", e
                        )

                    # Upload all JAR files
                    self.logger.debug(
                        "Uploading %d JAR file(s)", len(jar_files)
                    )
                    for jar_path in sorted(jar_files):
                        with open(jar_path, "rb") as file:
                            libraryfile = self.librarian.create(
                                os.path.basename(str(jar_path)),
                                os.stat(jar_path).st_size,
                                file,
                                filenameToContentType(str(jar_path)),
                                restricted=build.is_private,
                            )
                        build.addFile(libraryfile)

                    # Upload the POM file
                    pom_path = pom_files[0]
                    with open(pom_path, "rb") as file:
                        libraryfile = self.librarian.create(
                            "pom.xml",
                            os.stat(pom_path).st_size,
                            file,
                            filenameToContentType(str(pom_path)),
                            restricted=build.is_private,
                        )
                    build.addFile(libraryfile)

                    # Upload metadata.yaml
                    with open(metadata_path, "rb") as file:
                        libraryfile = self.librarian.create(
                            "metadata.yaml",
                            os.stat(metadata_path).st_size,
                            file,
                            filenameToContentType(str(metadata_path)),
                            restricted=build.is_private,
                        )
                    build.addFile(libraryfile)
                else:
                    # If no crate or jar+pom files found
                    # upload the original archive
                    self.logger.debug(
                        "No crate or jar+pom found in archive, "
                        "uploading tar.xz"
                    )
                    with open(craft_path, "rb") as file:
                        libraryfile = self.librarian.create(
                            os.path.basename(str(craft_path)),
                            os.stat(craft_path).st_size,
                            file,
                            filenameToContentType(str(craft_path)),
                            restricted=build.is_private,
                        )
                    build.addFile(libraryfile)

        # The master verifies the status to confirm successful upload.
        self.logger.debug("Updating %s" % build.title)
        build.updateStatus(BuildStatus.FULLYBUILT)
        self.logger.debug("Finished upload.")
