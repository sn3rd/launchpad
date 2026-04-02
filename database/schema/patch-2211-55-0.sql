-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

ALTER TABLE Snap ADD COLUMN build_path text;

COMMENT ON COLUMN Snap.build_path IS 'Subdirectory within the branch containing snapcraft.yaml.';

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 55, 0);
