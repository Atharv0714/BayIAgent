-- ============================================================================
-- BayI Agent — least-privilege Snowflake roles for the Azure deployment
--
-- This is docs/sql/01_roles.sql with the <<PLACEHOLDER>> tokens filled in from
-- .env, plus three corrections found by checking the script against the code.
-- Run it as ACCOUNTADMIN in Snowsight, top to bottom. It is idempotent.
--
-- Values used (from .env):
--   WAREHOUSE     COMPUTE_WH
--   DATABASE      BAYONE_INTERNALINFO
--   SCHEMA        PUBLIC
--   SERVICE_USER  ASHARMA3
--
-- WHAT THIS CHANGES: the app stops connecting as ACCOUNTADMIN. The query path
-- (which executes model-generated SQL) becomes SELECT-only, and the ingest path
-- becomes the only write-capable role.
--
-- WHAT THIS DOES NOT DO: the per-owner private/protected tiers. Those need
-- docs/sql/02_columns.sql and 03_row_access_policy.sql. Until those run, keep
-- ENFORCE_OWNERSHIP=false — with no row-access policy attached, the app would
-- set BAYI_CALLER and nothing would read it, which LOOKS like isolation but is
-- not. See the runbook.
-- ============================================================================

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS BAYI_READ;
CREATE ROLE IF NOT EXISTS BAYI_ADMIN_READ;
CREATE ROLE IF NOT EXISTS BAYI_INGEST_WRITE;

-- ── Compute ─────────────────────────────────────────────────────────────────
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE BAYI_READ;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE BAYI_ADMIN_READ;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE BAYI_INGEST_WRITE;

-- ── Database + schema visibility ────────────────────────────────────────────
GRANT USAGE ON DATABASE BAYONE_INTERNALINFO TO ROLE BAYI_READ;
GRANT USAGE ON DATABASE BAYONE_INTERNALINFO TO ROLE BAYI_ADMIN_READ;
GRANT USAGE ON DATABASE BAYONE_INTERNALINFO TO ROLE BAYI_INGEST_WRITE;
GRANT USAGE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;
GRANT USAGE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_ADMIN_READ;
GRANT USAGE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_INGEST_WRITE;

-- ── Read roles: SELECT on tables ────────────────────────────────────────────
GRANT SELECT ON ALL TABLES IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;
GRANT SELECT ON ALL TABLES IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_ADMIN_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_ADMIN_READ;

-- ── CORRECTION 1: views and the Cortex semantic view ────────────────────────
-- 01_roles.sql grants SELECT on TABLES only. CORTEX_SEMANTIC_VIEW is
-- BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC, and the SQL Cortex Analyst
-- generates is executed over the ordinary connection as BAYI_READ. Without
-- these the Cortex route fails with "does not exist or not authorized" while
-- the plain SQL route keeps working — a confusing half-broken state.
GRANT SELECT ON ALL VIEWS IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_ADMIN_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_ADMIN_READ;

-- Semantic views are a distinct object type on current Snowflake versions. If
-- your account predates them this errors with a syntax error — that is safe to
-- ignore, it means the object is covered by the VIEW grants above.
GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;
GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_READ;

-- Cortex Analyst requires one of these database roles on the querying role.
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE BAYI_READ;

-- ── Ingest role: the only write-capable role ────────────────────────────────
GRANT CREATE TABLE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_INGEST_WRITE;
GRANT INSERT ON ALL TABLES IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_INGEST_WRITE;
GRANT INSERT ON FUTURE TABLES IN SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE BAYI_INGEST_WRITE;

-- ── CORRECTION 2: ownership of blocks/facts ─────────────────────────────────
-- sf_agent.ingest_store.ensure_tables() runs, on every commit:
--     ALTER TABLE blocks ADD COLUMN IF NOT EXISTS sensitivity VARCHAR
-- Snowflake has NO grantable ALTER privilege on a table — altering requires
-- OWNERSHIP. blocks/facts already exist and are owned by ACCOUNTADMIN, so with
-- INSERT alone every ingest commit would fail on the first ALTER, even though
-- the statement is a no-op once the column is present.
--
-- COPY CURRENT GRANTS preserves the SELECT grants issued above, so BAYI_READ
-- keeps reading. Tables created later by the ingest role are owned by it
-- automatically, and the FUTURE grants keep BAYI_READ's access intact.
GRANT OWNERSHIP ON TABLE BAYONE_INTERNALINFO.PUBLIC.BLOCKS
    TO ROLE BAYI_INGEST_WRITE COPY CURRENT GRANTS;
GRANT OWNERSHIP ON TABLE BAYONE_INTERNALINFO.PUBLIC.FACTS
    TO ROLE BAYI_INGEST_WRITE COPY CURRENT GRANTS;

-- ── Bind roles to the service user ──────────────────────────────────────────
GRANT ROLE BAYI_READ TO USER ASHARMA3;
GRANT ROLE BAYI_INGEST_WRITE TO USER ASHARMA3;
-- Break-glass: grant to a NAMED HUMAN only, and expect its use to be audited.
-- GRANT ROLE BAYI_ADMIN_READ TO USER <named_admin>;

-- ============================================================================
-- VERIFY — run these and check the output before deploying.
-- ============================================================================

-- 1. The query role can read, and CANNOT write.
USE ROLE BAYI_READ;
USE WAREHOUSE COMPUTE_WH;
SELECT COUNT(*) AS blocks_visible FROM BAYONE_INTERNALINFO.PUBLIC.BLOCKS;
SELECT COUNT(*) AS facts_visible  FROM BAYONE_INTERNALINFO.PUBLIC.FACTS;
-- This MUST fail with "Insufficient privileges". If it succeeds, stop and
-- investigate — the read path is not actually read-only.
-- INSERT INTO BAYONE_INTERNALINFO.PUBLIC.FACTS (fact_id) VALUES ('privilege-test');

-- 2. The ingest role can create/alter and insert.
USE ROLE BAYI_INGEST_WRITE;
USE WAREHOUSE COMPUTE_WH;
ALTER TABLE BAYONE_INTERNALINFO.PUBLIC.BLOCKS ADD COLUMN IF NOT EXISTS sensitivity VARCHAR;
ALTER TABLE BAYONE_INTERNALINFO.PUBLIC.FACTS  ADD COLUMN IF NOT EXISTS sensitivity VARCHAR;

-- 3. Confirm the grants landed.
USE ROLE ACCOUNTADMIN;
SHOW GRANTS TO ROLE BAYI_READ;
SHOW GRANTS TO ROLE BAYI_INGEST_WRITE;
