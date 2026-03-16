-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

-- Replacing previously renamed indexes.
-- To be done CONCURRENTLY
CREATE UNIQUE INDEX bugtask_distinct_sourcepackage_assignment
    ON bugtask USING btree (
        bug,
        COALESCE(sourcepackagename, '-1'::integer),
        COALESCE(distroseries, '-1'::integer),
        COALESCE(distribution, '-1'::integer),
        COALESCE(packagetype, '-1'::integer),
        COALESCE(channel, '{}'::jsonb),
        COALESCE(archive, '-1'::integer)
    )
    WHERE (
        product IS NULL
        AND productseries IS NULL
        AND ociproject IS NULL
        AND ociprojectseries IS NULL
    );
DROP INDEX IF EXISTS old__bugtask_distinct_sourcepackage_assignment;

-- To be done CONCURRENTLY
CREATE UNIQUE INDEX bugsummary__unique
    ON bugsummary USING btree (
        COALESCE(product, '-1'::integer),
        COALESCE(productseries, '-1'::integer),
        COALESCE(distribution, '-1'::integer),
        COALESCE(distroseries, '-1'::integer),
        COALESCE(sourcepackagename, '-1'::integer),
        COALESCE(ociproject, '-1'::integer),
        COALESCE(ociprojectseries, '-1'::integer),
        COALESCE(packagetype, '-1'::integer),
        COALESCE(channel, '{}'::jsonb),
        COALESCE(archive, '-1'::integer),
        status,
        importance,
        has_patch,
        COALESCE(tag, ''::text),
        COALESCE(milestone, '-1'::integer),
        COALESCE(viewed_by, '-1'::integer),
        COALESCE(access_policy, '-1'::integer)
);
DROP INDEX IF EXISTS old__bugsummary__unique;

-- To be done CONCURRENTLY
CREATE INDEX bugsummaryjournal__full__idx
    ON bugsummaryjournal USING btree (
        status,
        product,
        productseries,
        distribution,
        distroseries,
        sourcepackagename,
        ociproject,
        ociprojectseries,
        packagetype,
        channel,
        archive,
        viewed_by,
        milestone,
        tag
    );
DROP INDEX IF EXISTS old__bugsummaryjournal__full__idx;

-- We need to add an index since Foreign Key references need to be indexed.
-- To be done CONCURRENTLY
CREATE INDEX bugtask__archive__idx ON bugtask
    USING btree (archive);

-- We need to add an index since Foreign Key references need to be indexed.
-- To be done CONCURRENTLY
CREATE INDEX bugtaskflat__archive__idx ON bugtaskflat
    USING btree (archive);

-- We need to add an index since Foreign Key references need to be indexed.
-- To be done CONCURRENTLY
CREATE INDEX bugsummary__archive__idx ON BugSummary
    USING btree (archive);

-- We need to add an index since Foreign Key references need to be indexed.
-- To be done CONCURRENTLY
CREATE INDEX bugsummaryjournal__archive__idx ON BugSummaryJournal
    USING btree (archive);

-- Adding a new index to an already estabilished relation.
-- To be done CONCURRENTLY
CREATE INDEX bugtask__packagetype__idx
    ON bugtask USING btree ( packagetype );

-- Adding a new index to an already estabilished relation.
-- To be done CONCURRENTLY
CREATE INDEX bugtaskflat__packagetype__idx
    ON bugtaskflat USING btree ( packagetype );

-- Adding a new index to an already estabilished relation.
-- To be done CONCURRENTLY
CREATE INDEX bugsummary__packagetype__idx
    ON bugsummary USING btree ( packagetype );

-- Adding a new index to an already estabilished relation.
-- To be done CONCURRENTLY
CREATE INDEX bugsummaryjournal__packagetype__idx
    ON bugsummaryjournal USING btree ( packagetype );

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 52, 1);
