-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

ALTER TABLE bugtask
    ADD COLUMN archive integer REFERENCES archive;

-- Add archive, packagetype and channel to the assignment checks.
-- Since archive already has relations to distribution, we can hold each of
-- them in mutual-exclusivity within bugtasks similar to the ditribution and
-- distroseries values.
-- But since an ArchiveSourcePackageSeries, would need both an archive and
-- a distroseries, which isn't an attribute of the archive model, we need
-- to allow distroseries and archive to co-exist.
ALTER TABLE bugtask
    DROP CONSTRAINT bugtask_assignment_checks;

ALTER TABLE bugtask
    ADD CONSTRAINT bugtask_assignment_checks CHECK (
        CASE
            WHEN product IS NOT NULL THEN
                productseries IS NULL
                AND distribution IS NULL
                AND distroseries IS NULL
                AND sourcepackagename IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN productseries IS NOT NULL THEN
                distribution IS NULL
                AND distroseries IS NULL
                AND sourcepackagename IS NULL
                AND ociproject IS NULL
                AND ociprojectseries IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN distribution IS NOT NULL THEN
                distroseries IS NULL
                AND archive IS NULL
            WHEN distroseries IS NOT NULL THEN
                ociproject IS NULL
                AND ociprojectseries IS NULL
            WHEN ociproject IS NOT NULL THEN
                ociprojectseries IS NULL
                AND (distribution IS NOT NULL OR product IS NOT NULL)
                AND sourcepackagename IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN ociprojectseries IS NOT NULL THEN
                ociproject IS NULL
                AND (distribution IS NOT NULL OR product IS NOT NULL)
                AND sourcepackagename IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN packagetype IS NOT NULL THEN
                sourcepackagename IS NOT NULL
                AND (distribution IS NOT NULL OR distroseries IS NOT NULL)
                AND product IS NULL
                AND productseries IS NULL
                AND ociproject IS NULL
                AND ociprojectseries IS NULL
                AND archive IS NULL
            WHEN channel IS NOT NULL THEN
                packagetype IS NOT NULL
            WHEN archive IS NOT NULL THEN
                distribution IS NULL
                AND product IS NULL
                AND productseries IS NULL
                AND ociproject IS NULL
                AND ociprojectseries IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
            ELSE false
        END) NOT VALID;

COMMENT ON COLUMN bugtask.archive IS
    'The archive of the bugtask target, for archive-specific source package tasks.';

-- Rename the existing index in the cold patch so the hot-patch that will
-- come after this can create the new index under the original name without
-- conflict.
ALTER INDEX bugtask_distinct_sourcepackage_assignment
    RENAME TO old__bugtask_distinct_sourcepackage_assignment;

ALTER TABLE bugtaskflat
    ADD COLUMN archive integer REFERENCES archive;

ALTER TABLE BugSummary
    ADD COLUMN archive integer REFERENCES archive;

ALTER TABLE BugSummary
    DROP CONSTRAINT bugtask_assignment_checks;

-- Since archive already has relations to distribution, we can hold each of
-- them in mutual-exclusivity within bugtasks similar to the ditribution and
-- distroseries values.
-- But since an ArchiveSourcePackageSeries, would need both an archive and
-- a distroseries, which isn't an attribute of the archive model, we need
-- to allow distroseries and archive to co-exist.
ALTER TABLE BugSummary
    ADD CONSTRAINT bugtask_assignment_checks CHECK (
        CASE
            WHEN product IS NOT NULL THEN
                productseries IS NULL
                AND distribution IS NULL
                AND distroseries IS NULL
                AND sourcepackagename IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN productseries IS NOT NULL THEN
                distribution IS NULL
                AND distroseries IS NULL
                AND sourcepackagename IS NULL
                AND ociproject IS NULL
                AND ociprojectseries IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN distribution IS NOT NULL THEN
                distroseries IS NULL
                AND archive IS NULL
            WHEN distroseries IS NOT NULL THEN
                ociproject IS NULL
                AND ociprojectseries IS NULL
            WHEN ociproject IS NOT NULL THEN
                ociprojectseries IS NULL
                AND (distribution IS NOT NULL OR product IS NOT NULL)
                AND sourcepackagename IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN ociprojectseries IS NOT NULL THEN
                ociproject IS NULL
                AND (distribution IS NOT NULL OR product IS NOT NULL)
                AND sourcepackagename IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
                AND archive IS NULL
            WHEN packagetype IS NOT NULL THEN
                sourcepackagename IS NOT NULL
                AND (distribution IS NOT NULL OR distroseries IS NOT NULL)
                AND product IS NULL
                AND productseries IS NULL
                AND ociproject IS NULL
                AND ociprojectseries IS NULL
                AND archive IS NULL
            WHEN channel IS NOT NULL THEN
                packagetype IS NOT NULL
            WHEN archive IS NOT NULL THEN
                distribution IS NULL
                AND product IS NULL
                AND productseries IS NULL
                AND ociproject IS NULL
                AND ociprojectseries IS NULL
                AND packagetype IS NULL
                AND channel IS NULL
            ELSE false
        END) NOT VALID;

-- Rename the existing index in the cold patch so the hot-patch that will
-- come after this can create the new index under the original name without
-- conflict.
ALTER INDEX bugsummary__unique RENAME TO old__bugsummary__unique;

COMMENT ON COLUMN bugsummary.archive IS
    'The archive for the aggregate, for archive-specific source package summaries.';

ALTER TABLE BugSummaryJournal
    ADD COLUMN archive integer REFERENCES archive;

-- Rename the existing index in the cold patch so the hot-patch that will
-- come after this can create the new index under the original name without
-- conflict.
ALTER INDEX bugsummaryjournal__full__idx
    RENAME TO old__bugsummaryjournal__full__idx;


-- Functions

CREATE OR REPLACE FUNCTION bugtask_flatten(task_id integer, check_only boolean)
    RETURNS boolean
    SET search_path TO 'public'
    LANGUAGE plpgsql
    SECURITY DEFINER
    AS $$
DECLARE
    bug_row Bug%ROWTYPE;
    task_row BugTask%ROWTYPE;
    old_flat_row BugTaskFlat%ROWTYPE;
    new_flat_row BugTaskFlat%ROWTYPE;
    _product_active boolean;
    _access_policies integer[];
    _access_grants integer[];
BEGIN
    -- This is the master function to update BugTaskFlat, but there are
    -- maintenance triggers and jobs on the involved tables that update
    -- it directly. Any changes here probably require a corresponding
    -- change in other trigger functions.

    SELECT * INTO task_row FROM BugTask WHERE id = task_id;
    SELECT * INTO old_flat_row FROM BugTaskFlat WHERE bugtask = task_id;

    -- If the task doesn't exist, ensure that there's no flat row.
    IF task_row.id IS NULL THEN
        IF old_flat_row.bugtask IS NOT NULL THEN
            IF NOT check_only THEN
                DELETE FROM BugTaskFlat WHERE bugtask = task_id;
            END IF;
            RETURN FALSE;
        ELSE
            RETURN TRUE;
        END IF;
    END IF;

    SELECT * INTO bug_row FROM bug WHERE id = task_row.bug;

    -- If it's a product(series) task, we must consult the active flag.
    IF task_row.product IS NOT NULL THEN
        SELECT product.active INTO _product_active
            FROM product WHERE product.id = task_row.product LIMIT 1;
    ELSIF task_row.productseries IS NOT NULL THEN
        SELECT product.active INTO _product_active
            FROM
                product
                JOIN productseries ON productseries.product = product.id
            WHERE productseries.id = task_row.productseries LIMIT 1;
    END IF;

    SELECT policies, grants
        INTO _access_policies, _access_grants
        FROM bug_build_access_cache(bug_row.id, bug_row.information_type)
            AS (policies integer[], grants integer[]);

    -- Compile the new flat row.
    SELECT task_row.id, bug_row.id, task_row.datecreated,
           bug_row.duplicateof, bug_row.owner, bug_row.fti,
           bug_row.information_type, bug_row.date_last_updated,
           bug_row.heat, task_row.product, task_row.productseries,
           task_row.distribution, task_row.distroseries,
           task_row.sourcepackagename, task_row.status,
           task_row.importance, task_row.assignee,
           task_row.milestone, task_row.owner,
           COALESCE(_product_active, TRUE),
           _access_policies,
           _access_grants,
           bug_row.latest_patch_uploaded, task_row.date_closed,
           task_row.ociproject, task_row.ociprojectseries,
           task_row.packagetype, task_row.channel, task_row.archive
           INTO new_flat_row;

    -- Calculate the necessary updates.
    IF old_flat_row.bugtask IS NULL THEN
        IF NOT check_only THEN
            INSERT INTO BugTaskFlat VALUES (new_flat_row.*);
        END IF;
        RETURN FALSE;
    ELSIF new_flat_row != old_flat_row THEN
        IF NOT check_only THEN
            UPDATE BugTaskFlat SET
                bug = new_flat_row.bug,
                datecreated = new_flat_row.datecreated,
                duplicateof = new_flat_row.duplicateof,
                bug_owner = new_flat_row.bug_owner,
                fti = new_flat_row.fti,
                information_type = new_flat_row.information_type,
                date_last_updated = new_flat_row.date_last_updated,
                heat = new_flat_row.heat,
                product = new_flat_row.product,
                productseries = new_flat_row.productseries,
                distribution = new_flat_row.distribution,
                distroseries = new_flat_row.distroseries,
                sourcepackagename = new_flat_row.sourcepackagename,
                status = new_flat_row.status,
                importance = new_flat_row.importance,
                assignee = new_flat_row.assignee,
                milestone = new_flat_row.milestone,
                owner = new_flat_row.owner,
                active = new_flat_row.active,
                access_policies = new_flat_row.access_policies,
                access_grants = new_flat_row.access_grants,
                date_closed = new_flat_row.date_closed,
                latest_patch_uploaded = new_flat_row.latest_patch_uploaded,
                ociproject = new_flat_row.ociproject,
                ociprojectseries = new_flat_row.ociprojectseries,
                packagetype = new_flat_row.packagetype,
                channel = new_flat_row.channel,
                archive = new_flat_row.archive
                WHERE bugtask = new_flat_row.bugtask;
        END IF;
        RETURN FALSE;
    ELSE
        RETURN TRUE;
    END IF;
END;
$$;


CREATE OR REPLACE FUNCTION bug_summary_inc(d bugsummary)
    RETURNS VOID
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Shameless adaption from postgresql manual
    LOOP
        -- first try to update the row
        UPDATE BugSummary SET count = count + d.count
        WHERE
            product IS NOT DISTINCT FROM $1.product
            AND productseries IS NOT DISTINCT FROM $1.productseries
            AND distribution IS NOT DISTINCT FROM $1.distribution
            AND distroseries IS NOT DISTINCT FROM $1.distroseries
            AND sourcepackagename IS NOT DISTINCT FROM $1.sourcepackagename
            AND ociproject IS NOT DISTINCT FROM $1.ociproject
            AND ociprojectseries IS NOT DISTINCT FROM $1.ociprojectseries
            AND packagetype IS NOT DISTINCT FROM $1.packagetype
            AND channel IS NOT DISTINCT FROM $1.channel
            AND archive IS NOT DISTINCT FROM $1.archive
            AND viewed_by IS NOT DISTINCT FROM $1.viewed_by
            AND tag IS NOT DISTINCT FROM $1.tag
            AND status = $1.status
            AND ((milestone IS NULL AND $1.milestone IS NULL)
                OR milestone = $1.milestone)
            AND importance = $1.importance
            AND has_patch = $1.has_patch
            AND access_policy IS NOT DISTINCT FROM $1.access_policy;
        IF found THEN
            RETURN;
        END IF;
        -- not there, so try to insert the key
        -- if someone else inserts the same key concurrently,
        -- we could get a unique-key failure
        BEGIN
            INSERT INTO BugSummary(
                count, product, productseries, distribution,
                distroseries, sourcepackagename,
                ociproject, ociprojectseries, packagetype,
                channel, archive, viewed_by, tag,
                status, milestone, importance, has_patch, access_policy)
            VALUES (
                d.count, d.product, d.productseries, d.distribution,
                d.distroseries, d.sourcepackagename,
                d.ociproject, d.ociprojectseries, d.packagetype,
                d.channel, d.archive, d.viewed_by, d.tag,
                d.status, d.milestone, d.importance, d.has_patch,
                d.access_policy);
            RETURN;
        EXCEPTION WHEN unique_violation THEN
            -- do nothing, and loop to try the UPDATE again
            RAISE NOTICE 'Looping and trying to update again';
        END;
    END LOOP;
END;
$$;


CREATE OR REPLACE FUNCTION bug_summary_dec(bugsummary)
    RETURNS VOID
    LANGUAGE sql
    AS $$
    -- We own the row reference, so in the absence of bugs this cannot
    -- fail - just decrement the row.
    UPDATE BugSummary SET count = count + $1.count
    WHERE
        ((product IS NULL AND $1.product IS NULL)
            OR product = $1.product)
        AND ((productseries IS NULL AND $1.productseries IS NULL)
            OR productseries = $1.productseries)
        AND ((distribution IS NULL AND $1.distribution IS NULL)
            OR distribution = $1.distribution)
        AND ((distroseries IS NULL AND $1.distroseries IS NULL)
            OR distroseries = $1.distroseries)
        AND ((sourcepackagename IS NULL AND $1.sourcepackagename IS NULL)
            OR sourcepackagename = $1.sourcepackagename)
        AND ((packagetype IS NULL AND $1.packagetype IS NULL)
            OR packagetype = $1.packagetype)
        AND ((channel IS NULL AND $1.channel IS NULL)
            OR channel = $1.channel)
        AND ((archive IS NULL AND $1.archive IS NULL)
            OR archive = $1.archive)
        AND ((viewed_by IS NULL AND $1.viewed_by IS NULL)
            OR viewed_by = $1.viewed_by)
        AND ((tag IS NULL AND $1.tag IS NULL)
            OR tag = $1.tag)
        AND status = $1.status
        AND ((milestone IS NULL AND $1.milestone IS NULL)
            OR milestone = $1.milestone)
        AND importance = $1.importance
        AND has_patch = $1.has_patch
        AND access_policy IS NOT DISTINCT FROM $1.access_policy;
$$;

CREATE OR REPLACE FUNCTION bugsummary_rollup_journal(batchsize integer DEFAULT NULL::integer)
    RETURNS VOID
    SET search_path TO 'public'
    LANGUAGE plpgsql
    SECURITY DEFINER
    AS $$
DECLARE
    d bugsummary%ROWTYPE;
    max_id integer;
BEGIN
    -- Lock so we don't content with other invocations of this
    -- function. We can happily lock the BugSummary table for writes
    -- as this function is the only thing that updates that table.
    -- BugSummaryJournal remains unlocked so nothing should be blocked.
    LOCK TABLE BugSummary IN ROW EXCLUSIVE MODE;

    IF batchsize IS NULL THEN
        SELECT MAX(id) INTO max_id FROM BugSummaryJournal;
    ELSE
        SELECT MAX(id) INTO max_id FROM (
            SELECT id FROM BugSummaryJournal ORDER BY id LIMIT batchsize
            ) AS Whatever;
    END IF;

    FOR d IN
        SELECT
            NULL as id,
            SUM(count),
            product,
            productseries,
            distribution,
            distroseries,
            sourcepackagename,
            viewed_by,
            tag,
            status,
            milestone,
            importance,
            has_patch,
            access_policy,
            ociproject,
            ociprojectseries,
            packagetype,
            channel,
            archive
        FROM BugSummaryJournal
        WHERE id <= max_id
        GROUP BY
            product, productseries, distribution, distroseries,
            sourcepackagename, ociproject, ociprojectseries, packagetype,
            channel, archive, viewed_by, tag, status, milestone,
            importance, has_patch, access_policy
        HAVING sum(count) <> 0
    LOOP
        IF d.count < 0 THEN
            PERFORM bug_summary_dec(d);
        ELSIF d.count > 0 THEN
            PERFORM bug_summary_inc(d);
        END IF;
    END LOOP;

    -- Clean out any counts we reduced to 0.
    DELETE FROM BugSummary WHERE count=0;
    -- Clean out the journal entries we have handled.
    DELETE FROM BugSummaryJournal WHERE id <= max_id;
END;
$$;


CREATE OR REPLACE FUNCTION bugtask_maintain_bugtaskflat_trig()
    RETURNS TRIGGER
    SET search_path TO 'public'
    LANGUAGE plpgsql
    SECURITY DEFINER
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM bugtask_flatten(NEW.id, FALSE);
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.bug != OLD.bug THEN
            RAISE EXCEPTION 'cannot move bugtask to a different bug';
        ELSIF (NEW.product IS DISTINCT FROM OLD.product
            OR NEW.productseries IS DISTINCT FROM OLD.productseries) THEN
            -- product.active may differ. Do a full update.
            PERFORM bugtask_flatten(NEW.id, FALSE);
        ELSIF (
            NEW.datecreated IS DISTINCT FROM OLD.datecreated
            OR NEW.product IS DISTINCT FROM OLD.product
            OR NEW.productseries IS DISTINCT FROM OLD.productseries
            OR NEW.distribution IS DISTINCT FROM OLD.distribution
            OR NEW.distroseries IS DISTINCT FROM OLD.distroseries
            OR NEW.sourcepackagename IS DISTINCT FROM OLD.sourcepackagename
            OR NEW.ociproject IS DISTINCT FROM OLD.ociproject
            OR NEW.ociprojectseries IS DISTINCT FROM OLD.ociprojectseries
            OR NEW.packagetype IS DISTINCT FROM OLD.packagetype
            OR NEW.channel IS DISTINCT FROM OLD.channel
            OR NEW.archive IS DISTINCT FROM OLD.archive
            OR NEW.status IS DISTINCT FROM OLD.status
            OR NEW.importance IS DISTINCT FROM OLD.importance
            OR NEW.assignee IS DISTINCT FROM OLD.assignee
            OR NEW.milestone IS DISTINCT FROM OLD.milestone
            OR NEW.owner IS DISTINCT FROM OLD.owner
            OR NEW.date_closed IS DISTINCT FROM OLD.date_closed) THEN
            -- Otherwise just update the columns from bugtask.
            -- Access policies and grants may have changed due to target
            -- transitions, but an earlier trigger will already have
            -- mirrored them to all relevant flat tasks.
            UPDATE BugTaskFlat SET
                datecreated = NEW.datecreated,
                product = NEW.product,
                productseries = NEW.productseries,
                distribution = NEW.distribution,
                distroseries = NEW.distroseries,
                sourcepackagename = NEW.sourcepackagename,
                ociproject = NEW.ociproject,
                ociprojectseries = NEW.ociprojectseries,
                packagetype = NEW.packagetype,
                channel = NEW.channel,
                archive = NEW.archive,
                status = NEW.status,
                importance = NEW.importance,
                assignee = NEW.assignee,
                milestone = NEW.milestone,
                owner = NEW.owner,
                date_closed = NEW.date_closed
                WHERE bugtask = NEW.id;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM bugtask_flatten(OLD.id, FALSE);
    END IF;
    RETURN NULL;
END;
$$;


-- Review returns TABLE () ordering and value types
-- If we want to target only packages in distroseries we need to modify this
-- We use DROP -> CREATE instead of CREATE OR REPLACE because this modification
-- changes the return table's structure by adding the archive column which
-- isn't allowed when using CREATE OR REPLACE.
DROP FUNCTION bugsummary_targets;
CREATE FUNCTION bugsummary_targets(btf_row bugtaskflat)
    RETURNS TABLE(
        product integer,
        productseries integer,
        distribution integer,
        distroseries integer,
        sourcepackagename integer,
        ociproject integer,
        ociprojectseries integer,
        packagetype integer,
        channel jsonb,
        archive integer
    )
    IMMUTABLE
    LANGUAGE sql
    AS $$
    -- Include a sourcepackagename-free/ociproject(series)-free task if this
    -- one has a sourcepackagename/ociproject(series), so package tasks are
    -- also counted in their distro/series.
    SELECT
        $1.product, $1.productseries, $1.distribution,
        $1.distroseries, $1.sourcepackagename, $1.ociproject,
        $1.ociprojectseries, $1.packagetype, $1.channel, $1.archive
    UNION -- Implicit DISTINCT
    SELECT
        $1.product, $1.productseries, $1.distribution,
        $1.distroseries, NULL, NULL,
        NULL, NULL, NULL, $1.archive;
$$;


CREATE OR REPLACE FUNCTION bugsummary_locations(btf_row bugtaskflat, tags text[])
    RETURNS SETOF bugsummaryjournal
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF btf_row.duplicateof IS NOT NULL THEN
        RETURN;
    END IF;
    RETURN QUERY
        SELECT
            CAST(NULL AS integer) AS id,
            CAST(1 AS integer) AS count,
            bug_targets.product, bug_targets.productseries,
            bug_targets.distribution, bug_targets.distroseries,
            bug_targets.sourcepackagename,
            bug_viewers.viewed_by, bug_tags.tag, btf_row.status,
            btf_row.milestone, btf_row.importance,
            btf_row.latest_patch_uploaded IS NOT NULL AS has_patch,
            bug_viewers.access_policy,
            bug_targets.ociproject, bug_targets.ociprojectseries,
            bug_targets.packagetype, bug_targets.channel,
            bug_targets.archive
        FROM
            bugsummary_targets(btf_row) AS bug_targets,
            unnest(tags) AS bug_tags (tag),
            bugsummary_viewers(btf_row) AS bug_viewers;
END;
$$;


CREATE OR REPLACE FUNCTION bugsummary_insert_journals(journals bugsummaryjournal[])
    RETURNS VOID
    LANGUAGE sql
    AS $$
    -- We sum the rows here to minimise the number of inserts into the
    -- journal, as in the case of UPDATE statement we may have -1s and +1s
    -- cancelling each other out.
    INSERT INTO BugSummaryJournal(
            count, product, productseries, distribution, distroseries,
            sourcepackagename, ociproject, ociprojectseries, packagetype,
            channel, archive, viewed_by, tag, status, milestone, importance, has_patch,
            access_policy)
        SELECT
            SUM(count), product, productseries, distribution, distroseries,
            sourcepackagename, ociproject, ociprojectseries, packagetype,
            channel, archive, viewed_by, tag, status, milestone, importance, has_patch,
            access_policy
        FROM unnest(journals)
        GROUP BY
            product, productseries, distribution, distroseries,
            sourcepackagename, ociproject, ociprojectseries, packagetype,
            channel, archive, viewed_by, tag, status, milestone, importance, has_patch,
            access_policy
        HAVING SUM(count) != 0;
$$;


-- Views

-- Combined view so we don't have to manually collate rows from both tables.
-- Note that we flip the sign of the id column of BugSummaryJournal to avoid
-- clashes. This is enough to keep Storm happy as it never needs to update
-- this table, and there are no other suitable primary keys.
-- We don't SUM() rows here to ensure PostgreSQL has the most hope of
-- generating good query plans when we query this view.
CREATE OR REPLACE VIEW CombinedBugSummary AS (
SELECT id,
       count,
       product,
       productseries,
       distribution,
       distroseries,
       sourcepackagename,
       viewed_by,
       tag,
       status,
       milestone,
       importance,
       has_patch,
       access_policy,
       ociproject,
       ociprojectseries,
       packagetype,
       channel,
       archive
FROM bugsummary
UNION ALL
SELECT -id AS id,
       count,
       product,
       productseries,
       distribution,
       distroseries,
       sourcepackagename,
       viewed_by,
       tag,
       status,
       milestone,
       importance,
       has_patch,
       access_policy,
       ociproject,
       ociprojectseries,
       packagetype,
       channel,
       archive
FROM bugsummaryjournal
);

INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 52, 0);
