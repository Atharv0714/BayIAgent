-- ============================================================================
-- BayI Agent — governance model: sensitivity columns + row-access policy
--
-- This is docs/sql/02_columns.sql and 03_row_access_policy.sql, placeholders
-- filled from .env, corrected against the code AND against what actually happens
-- when you run it after deploy/snowflake-roles.sql.
--
-- >>> DO NOT RUN THIS UNTIL deploy/snowflake-governance-test.sql (or
-- >>> scripts/gov_session_probe.py) HAS PASSED. <<<
-- That test decides whether getvariable() in a policy body tracks a re-SET on a
-- shared session. It PASSED on 2026-07-24 (getvariable invalidates on SET), so the
-- app's one-connection model is safe and this policy can be attached. If it had
-- failed, attaching this policy would serve one user another user's private rows.
--
-- Order: snowflake-roles.sql -> gov test -> this file. Run as ACCOUNTADMIN. Idempotent.
--
-- Values used (from .env):
--   DATABASE  BAYONE_INTERNALINFO
--   SCHEMA    PUBLIC
--   WAREHOUSE COMPUTE_WH
--
-- WHY THIS DIFFERS FROM docs/sql/02+03 (three corrections, all load-bearing):
--   (A) snowflake-roles.sql transferred OWNERSHIP of blocks/facts to
--       BAYI_INGEST_WRITE. ACCOUNTADMIN can therefore no longer ALTER those tables
--       (add columns / backfill). So the column + backfill DML runs as the OWNER
--       role, and ACCOUNTADMIN attaches the policy via the account-level
--       APPLY ROW ACCESS POLICY privilege (centralised policy admin — no ownership
--       needed for attach/detach).
--   (B) THE BACKFILL IS NOT OPTIONAL. docs/sql/02 says "backfill unnecessary — the
--       DEFAULT lands 'internal'". That is FALSE for this deployment: the columns
--       were first added by the app's _ADDITIVE_COLUMNS path, which uses
--       ADD COLUMN *without* a DEFAULT (Snowflake mis-compiles the DEFAULT form when
--       the column already exists). So pre-existing rows have sensitivity = NULL, and
--       the policy hides NULL rows (NULL = 'internal' is not TRUE). Attaching the
--       policy without backfilling silently HIDES every pre-tiering row. Measured on
--       2026-07-24: 35 of 115 blocks and 58 of 74 facts were NULL. The UPDATE below
--       lands them 'internal'/'general' (their pre-feature, shared meaning).
--   (C) The backfill must run with the policy DETACHED — the owner role is not in the
--       policy's admin branch, so while the policy is attached it cannot even SEE the
--       NULL rows to update them. Hence: detach -> backfill -> (re)attach.
-- ============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE BAYONE_INTERNALINFO;
USE SCHEMA PUBLIC;
USE WAREHOUSE COMPUTE_WH;

-- ── Step 1: enable central policy administration + detach any existing policy ────
-- blocks/facts are owned by BAYI_INGEST_WRITE (roles.sql). The account-level
-- APPLY ROW ACCESS POLICY privilege lets ACCOUNTADMIN set/unset policies on tables it
-- does not own — the standard separation between the object owner and the policy admin.
GRANT APPLY ROW ACCESS POLICY ON ACCOUNT TO ROLE ACCOUNTADMIN;

-- Detach first so the backfill (below) can see every row, and so CREATE OR REPLACE can
-- update the policy body. FIRST RUN: these two error because nothing is attached yet —
-- that is expected; run under "continue on error" or skip them the first time.
ALTER TABLE blocks DROP ROW ACCESS POLICY rap_ownership;
ALTER TABLE facts  DROP ROW ACCESS POLICY rap_ownership;

-- ── Step 2: sensitivity columns + BACKFILL, as the table OWNER ──────────────────
-- Owner role (only role that may ALTER these tables now) and, with the policy
-- detached, the only way to see and fix the NULL rows.
USE ROLE BAYI_INGEST_WRITE;
USE WAREHOUSE COMPUTE_WH;

-- Columns the write path names on every INSERT (sf_agent.ingest_store BLOCK_COLS /
-- FACT_COLS / _ADDITIVE_COLUMNS). Idempotent; NO DEFAULT (see the ingest_store note —
-- ADD COLUMN IF NOT EXISTS ... DEFAULT mis-compiles when the column already exists).
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS sensitivity VARCHAR;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS sensitivity_category VARCHAR;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS ingested_by VARCHAR;
ALTER TABLE facts  ADD COLUMN IF NOT EXISTS source_block_index NUMBER;
ALTER TABLE facts  ADD COLUMN IF NOT EXISTS sensitivity VARCHAR;
ALTER TABLE facts  ADD COLUMN IF NOT EXISTS sensitivity_category VARCHAR;
ALTER TABLE facts  ADD COLUMN IF NOT EXISTS ingested_by VARCHAR;

-- CRITICAL backfill (correction B/C above). Every pre-tiering row is shared, so NULL
-- sensitivity becomes 'internal' / category 'general'. Without this the policy hides them.
UPDATE blocks SET sensitivity = 'internal', sensitivity_category = 'general' WHERE sensitivity IS NULL;
UPDATE facts  SET sensitivity = 'internal', sensitivity_category = 'general' WHERE sensitivity IS NULL;
-- Defensive: a category left NULL on an otherwise-tiered row (mirrors category_for_tier).
UPDATE blocks SET sensitivity_category = CASE WHEN sensitivity = 'internal' THEN 'general' ELSE sensitivity END WHERE sensitivity_category IS NULL;
UPDATE facts  SET sensitivity_category = CASE WHEN sensitivity = 'internal' THEN 'general' ELSE sensitivity END WHERE sensitivity_category IS NULL;

-- Confirm zero NULLs remain before the policy goes on.
SELECT 'blocks' AS tbl, COUNT(*) AS null_sensitivity FROM blocks WHERE sensitivity IS NULL
UNION ALL
SELECT 'facts', COUNT(*) FROM facts WHERE sensitivity IS NULL;

-- ── Step 3: the row-access policy ───────────────────────────────────────────────
-- The real enforcement boundary. Even a bare SELECT * FROM blocks has private and
-- protected rows removed by Snowflake, not by the app, so a prompt injection that talks
-- the agent into a broad query still cannot widen the view.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE COMPUTE_WH;

CREATE OR REPLACE ROW ACCESS POLICY rap_ownership
    AS (sensitivity VARCHAR, ingested_by VARCHAR) RETURNS BOOLEAN ->
        sensitivity = 'internal'
        OR (sensitivity = 'private' AND ingested_by = getvariable('BAYI_CALLER'))
        OR (sensitivity = 'protected' AND getvariable('BAYI_PROTECTED') = 'true')
        OR current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN');

-- The variable NAMES must match the app: CALLER_SESSION_VAR=BAYI_CALLER and
-- PROTECTED_SESSION_VAR=BAYI_PROTECTED. Change one without the other and the policy
-- silently stops matching anyone's private rows.
--
-- ON THE ACCOUNTADMIN BRANCH: this is a permanent, unlogged bypass — anyone who can
-- USE ROLE ACCOUNTADMIN reads every private and protected row. It is tolerable only
-- because the app now connects as BAYI_READ. Once BAYI_ADMIN_READ is granted to a named
-- human for break-glass, drop ACCOUNTADMIN from this list (see RUNBOOK "ACCOUNTADMIN
-- bypass"). If SNOWFLAKE_ROLE ever reverts to ACCOUNTADMIN, this policy stops filtering.

ALTER TABLE blocks ADD ROW ACCESS POLICY rap_ownership ON (sensitivity, ingested_by);
ALTER TABLE facts  ADD ROW ACCESS POLICY rap_ownership ON (sensitivity, ingested_by);

-- ── Verify ──────────────────────────────────────────────────────────────────────
SHOW ROW ACCESS POLICIES LIKE 'rap_ownership';

-- Confirm the policy bites as the app's own role. With no identity bound, only
-- 'internal' rows are visible; after the backfill that is EVERY pre-existing row, so
-- these counts must equal the full table counts — nothing legitimate disappeared.
USE ROLE BAYI_READ;
USE WAREHOUSE COMPUTE_WH;
UNSET BAYI_CALLER;
SELECT sensitivity, COUNT(*) AS visible_rows FROM blocks GROUP BY sensitivity ORDER BY 1;
SELECT sensitivity, COUNT(*) AS visible_rows FROM facts  GROUP BY sensitivity ORDER BY 1;
USE ROLE ACCOUNTADMIN;
