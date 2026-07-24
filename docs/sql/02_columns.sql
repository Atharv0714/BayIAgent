-- ============================================================================
-- BayI Agent — per-owner private-data model · Step 2 of 3: sensitivity columns
-- ----------------------------------------------------------------------------
-- Run as ACCOUNTADMIN, after 01_roles.sql. Idempotent (ADD COLUMN IF NOT EXISTS).
--
-- Replace placeholders:
--   <<DATABASE>>   e.g. BAYONE_INTERNALINFO
--   <<SCHEMA>>     e.g. PUBLIC
--
-- Two new columns per table, matching src/sf_agent/ingest_store.py:
--   SENSITIVITY  'internal' (shared, default) or 'private' (owner-only).
--   INGESTED_BY  the caller's identity (email) at commit time, for private rows.
--
-- The DEFAULT 'internal' means every existing row — and any row ingested before
-- the app starts stamping — stays visible to everyone. That is what makes the
-- rollout safe: turning the policy on (step 3) changes nothing until someone
-- actually ingests a row marked private.
-- ============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE <<DATABASE>>;
USE SCHEMA <<SCHEMA>>;

ALTER TABLE blocks ADD COLUMN IF NOT EXISTS sensitivity VARCHAR DEFAULT 'internal';
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS ingested_by VARCHAR;

ALTER TABLE facts ADD COLUMN IF NOT EXISTS sensitivity VARCHAR DEFAULT 'internal';
ALTER TABLE facts ADD COLUMN IF NOT EXISTS ingested_by VARCHAR;

-- >>> CORRECTION (see deploy/snowflake-governance.sql, which supersedes this file) <<<
-- "Backfill is unnecessary" is WRONG when these columns were first added by the app's
-- ingest_store._ADDITIVE_COLUMNS path, which uses ADD COLUMN *without* a DEFAULT
-- (Snowflake mis-compiles the DEFAULT form on an existing column). Those pre-existing
-- rows have sensitivity = NULL, and the step-3 policy HIDES NULL rows — attaching it
-- without a backfill silently hides every pre-tiering row. Run the explicit backfill
-- (NULL -> 'internal' / 'general') with the policy detached. deploy/snowflake-governance.sql
-- does this correctly; prefer it over running this file verbatim.
