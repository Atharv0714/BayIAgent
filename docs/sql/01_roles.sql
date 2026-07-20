-- ============================================================================
-- BayI Agent — per-owner private-data model · Step 1 of 3: roles & grants
-- ----------------------------------------------------------------------------
-- Run as ACCOUNTADMIN, once, in order (01 -> 02 -> 03). Idempotent.
--
-- Before running, replace the placeholders below with your real object names:
--   <<WAREHOUSE>>      e.g. COMPUTE_WH        (SNOWFLAKE_WAREHOUSE)
--   <<DATABASE>>       e.g. BAYONE_INTERNALINFO (SNOWFLAKE_INGEST_DATABASE)
--   <<SCHEMA>>         e.g. PUBLIC            (SNOWFLAKE_INGEST_SCHEMA)
--   <<SERVICE_USER>>   the Snowflake user the app connects as (SNOWFLAKE_USER)
--
-- Three least-privilege roles, replacing the current ACCOUNTADMIN posture:
--   BAYI_READ          — the query path. SELECT on blocks/facts; the row-access
--                        policy (step 3) shows only internal rows + the caller's
--                        own private rows.
--   BAYI_ADMIN_READ    — break-glass. SELECT on everything incl. all private rows
--                        (offboarding / legal). Grant to a named human only, and
--                        expect its use to be logged.
--   BAYI_INGEST_WRITE  — the ingest load path. INSERT + create-table only.
-- ============================================================================

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS BAYI_READ;
CREATE ROLE IF NOT EXISTS BAYI_ADMIN_READ;
CREATE ROLE IF NOT EXISTS BAYI_INGEST_WRITE;

-- Compute access (all three need to run statements).
GRANT USAGE ON WAREHOUSE <<WAREHOUSE>> TO ROLE BAYI_READ;
GRANT USAGE ON WAREHOUSE <<WAREHOUSE>> TO ROLE BAYI_ADMIN_READ;
GRANT USAGE ON WAREHOUSE <<WAREHOUSE>> TO ROLE BAYI_INGEST_WRITE;

-- Database + schema visibility.
GRANT USAGE ON DATABASE <<DATABASE>> TO ROLE BAYI_READ;
GRANT USAGE ON DATABASE <<DATABASE>> TO ROLE BAYI_ADMIN_READ;
GRANT USAGE ON DATABASE <<DATABASE>> TO ROLE BAYI_INGEST_WRITE;
GRANT USAGE ON SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_READ;
GRANT USAGE ON SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_ADMIN_READ;
GRANT USAGE ON SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_INGEST_WRITE;

-- Read roles: SELECT on the two knowledge tables (existing + future).
GRANT SELECT ON ALL TABLES IN SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_READ;
GRANT SELECT ON ALL TABLES IN SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_ADMIN_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_ADMIN_READ;

-- Ingest role: the only write-capable role. Needs to create the two tables (first
-- run) and insert rows. CREATE TABLE covers ensure_tables(); INSERT covers commits.
GRANT CREATE TABLE ON SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_INGEST_WRITE;
GRANT INSERT ON ALL TABLES IN SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_INGEST_WRITE;
GRANT INSERT ON FUTURE TABLES IN SCHEMA <<DATABASE>>.<<SCHEMA>> TO ROLE BAYI_INGEST_WRITE;
-- Ingest also needs to SELECT (ALTER/apply-policy is done here as ACCOUNTADMIN, but
-- the loader reads back nothing; SELECT kept off to preserve least privilege).

-- Bind the roles to the app's service user so the app can activate them via
-- SNOWFLAKE_ROLE / SNOWFLAKE_INGEST_ROLE. Grant BAYI_ADMIN_READ only to a human.
GRANT ROLE BAYI_READ TO USER <<SERVICE_USER>>;
GRANT ROLE BAYI_INGEST_WRITE TO USER <<SERVICE_USER>>;
-- GRANT ROLE BAYI_ADMIN_READ TO USER <<NAMED_ADMIN_USER>>;  -- uncomment for break-glass holder
