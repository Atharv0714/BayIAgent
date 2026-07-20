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

-- Backfill is unnecessary: the DEFAULT already lands 'internal' on existing rows.
