-- Copyright 2011 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

ALTER TABLE LibraryFileContent
    ADD COLUMN sha512 character(128);

COMMENT ON COLUMN libraryfilecontent.sha512 IS 'The SHA-512 digest of the file''s contents';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 53, 0);
