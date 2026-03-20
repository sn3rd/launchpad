-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- Needs to be run concurrently on production
CREATE INDEX libraryfilecontent__sha512__idx ON public.libraryfilecontent USING btree (sha512);

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 54, 0);
