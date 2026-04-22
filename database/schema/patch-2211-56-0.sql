-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- Needs to be run concurrently on production
CREATE INDEX binarypackagepublishinghistory__archive_distroarchseries_pocket_component_status__idx ON binarypackagepublishinghistory USING btree (archive, distroarchseries, pocket, component, status);

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 56, 0);
