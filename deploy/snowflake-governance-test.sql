-- ============================================================================
-- BayI Agent — DECIDING TEST before enabling the governance model
--
-- Run this ENTIRE script in ONE Snowsight worksheet, top to bottom, without
-- opening a new worksheet partway through. The whole point is that every
-- statement shares a single Snowflake SESSION — which is exactly how the app
-- behaves, since it holds one connection for all users behind a module lock.
--
-- WHAT IS BEING TESTED
-- docs/sql/03_row_access_policy.sql filters rows with getvariable('BAYI_CALLER').
-- Snowflake's GETVARIABLE documentation says:
--
--   "This function uses the result cache for the current session if you call
--    the function more than once in the same session. The result cache applies
--    wherever you call this function, including the body of policy objects,
--    such as a row access policy."
--
-- If that cache does NOT clear when the app re-runs SET for the next user, then
-- on a shared session the policy keeps evaluating the PREVIOUS caller — and one
-- user is served another user's private rows. That is the precise failure the
-- ownership model exists to prevent, and it fails silently: queries succeed and
-- look correct.
--
-- This script settles it on a scratch table. It touches NOTHING in blocks/facts.
-- Prerequisite: deploy/snowflake-roles.sql has been run (BAYI_READ must exist).
-- ============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE BAYONE_INTERNALINFO;
USE SCHEMA PUBLIC;
USE WAREHOUSE COMPUTE_WH;

-- ── Scratch fixture ─────────────────────────────────────────────────────────
CREATE OR REPLACE TABLE gov_test (
    id          NUMBER,
    label       VARCHAR,
    sensitivity VARCHAR,
    ingested_by VARCHAR
);

INSERT INTO gov_test VALUES
    (1, 'shared-row',        'internal',  NULL),
    (2, 'alice-private-row', 'private',   'alice@bayone.com'),
    (3, 'bob-private-row',   'private',   'bob@bayone.com'),
    (4, 'protected-row',     'protected', NULL);

-- Same body as 03_row_access_policy.sql.
CREATE OR REPLACE ROW ACCESS POLICY rap_gov_test
    AS (sensitivity VARCHAR, ingested_by VARCHAR) RETURNS BOOLEAN ->
        sensitivity = 'internal'
        OR (sensitivity = 'private' AND ingested_by = getvariable('BAYI_CALLER'))
        OR (sensitivity = 'protected' AND getvariable('BAYI_PROTECTED') = 'true')
        OR current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN');

ALTER TABLE gov_test ADD ROW ACCESS POLICY rap_gov_test ON (sensitivity, ingested_by);
GRANT SELECT ON TABLE gov_test TO ROLE BAYI_READ;

-- ── The test ────────────────────────────────────────────────────────────────
-- Must run as BAYI_READ. As ACCOUNTADMIN the policy's break-glass branch matches
-- every row and the test would pass vacuously.
USE ROLE BAYI_READ;
USE WAREHOUSE COMPUTE_WH;

-- Caller 1 -------------------------------------------------------------------
SET BAYI_CALLER = 'alice@bayone.com';
SET BAYI_PROTECTED = 'false';

-- EXPECT exactly: shared-row, alice-private-row
SELECT 'query 1 (alice)' AS step, id, label FROM gov_test ORDER BY id;

-- Caller 2 — the same session, a different user. This is the real test. --------
SET BAYI_CALLER = 'bob@bayone.com';

-- EXPECT exactly: shared-row, bob-private-row
--
--   If 'alice-private-row' appears here, GETVARIABLE served a cached value and
--   the shared-session design LEAKS ACROSS USERS. Stop; do not attach the policy
--   to blocks/facts. The app needs a session per caller first.
--
--   If 'bob-private-row' appears and Alice's does not, the cache tracks SET and
--   the design is sound as written.
SELECT 'query 2 (bob)' AS step, id, label FROM gov_test ORDER BY id;

-- Protected tier, same session ------------------------------------------------
SET BAYI_PROTECTED = 'true';
-- EXPECT: shared-row, bob-private-row, protected-row
SELECT 'query 3 (bob, protected)' AS step, id, label FROM gov_test ORDER BY id;

SET BAYI_PROTECTED = 'false';
-- EXPECT: protected-row is GONE again. If it lingers, the protected flag caches
-- too, and group membership would leak across users the same way.
SELECT 'query 4 (bob, not protected)' AS step, id, label FROM gov_test ORDER BY id;

-- Unbound caller --------------------------------------------------------------
UNSET BAYI_CALLER;
-- EXPECT: shared-row only. getvariable returns NULL, the equality is NULL, and
-- no private row matches — fail-closed, which is the correct direction.
SELECT 'query 5 (no identity)' AS step, id, label FROM gov_test ORDER BY id;

-- ── Cleanup ─────────────────────────────────────────────────────────────────
USE ROLE ACCOUNTADMIN;
ALTER TABLE gov_test DROP ROW ACCESS POLICY rap_gov_test;
DROP ROW ACCESS POLICY rap_gov_test;
DROP TABLE gov_test;

-- ============================================================================
-- Paste back the output of queries 1-5 (the step column and the labels) and I
-- will take it from there.
-- ============================================================================
