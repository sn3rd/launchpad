# Copyright 2009-2021 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "copy_and_close",
    "EncodableLibraryFileAlias",
    "filechunks",
    "guess_librarian_encoding",
    "sha1_from_path",
]


import hashlib

MEGABYTE = 1024 * 1024


def filechunks(file, chunk_size=4 * MEGABYTE):
    """Return an iterator which reads chunks of the given file."""
    return iter(lambda: file.read(chunk_size), b"")


def copy_and_close(from_file, to_file):
    """Copy from_file to to_file and close both.

    It requires both arguments to be opened file-like objects.
    'filechunks' trick is used reduce the buffers memory demanded
    when handling large files.
    It's suitable to copy contents from ILibraryFileAlias instances to the
    local filesystem.
    Both file_descriptors are closed before return.
    """
    for chunk in filechunks(from_file):
        to_file.write(chunk)
    from_file.close()
    to_file.close()


def sha1_from_path(path):
    """Return the hexdigest SHA1 for the contents of the path."""
    with open(path, "rb") as the_file:
        the_hash = hashlib.sha1()  # nosec B324
        for chunk in filechunks(the_file):
            the_hash.update(chunk)

    return the_hash.hexdigest()


def guess_librarian_encoding(filename, mimetype):
    """Return the appropriate encoding for the given filename and mimetype.

    Files with the following extensions will be served as
    'Content-Encoding: gzip' and 'Content-Type: text/plain',
    which indicates to browsers that, after being unzipped,
    their contents can be rendered inline.

    * 'txt.gz': gzipped sources buildlogs;
    * 'diff.gz': gzipped sources diffs;

    :param filename: string containing the filename to be guessed;
    :param mimetype: string containing the stored mimetype;

    :return: a tuple containing the appropriate 'encoding' and 'mimetype'
        that should be used to serve the file.
    """
    if filename.endswith("txt.gz"):
        encoding = "gzip"
        mimetype = "text/plain; charset=utf-8"
    elif filename.endswith("diff.gz"):
        encoding = "gzip"
        mimetype = "text/plain"
    else:
        encoding = None

    return encoding, mimetype


class EncodableLibraryFileAlias:
    """A `LibraryFileAlias` wrapper usable with a `MultipartEncoder`."""

    def __init__(self, lfa):
        self.lfa = lfa
        self.position = 0

    def __len__(self):
        return self.lfa.content.filesize - self.position

    def read(self, length=-1):
        chunksize = None if length == -1 else length
        data = self.lfa.read(chunksize=chunksize)
        if chunksize is None:
            self.position = self.lfa.content.filesize
        else:
            self.position += len(data)
        return data
