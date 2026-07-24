-- ============================================================================
-- BayI Agent — per-owner private-data model · Step 3 of 3: row-access policy
-- ----------------------------------------------------------------------------
-- Run as ACCOUNTADMIN, after 02_columns.sql. Idempotent.
--
-- >>> CORRECTION (see deploy/snowflake-governance.sql, which supersedes this file) <<<
-- If 01_roles.sql transferred OWNERSHIP of blocks/facts to BAYI_INGEST_WRITE, then
-- ACCOUNTADMIN can no longer ALTER those tables to attach a policy. Grant
-- `APPLY ROW ACCESS POLICY ON ACCOUNT TO ROLE ACCOUNTADMIN` first (centralised policy
-- admin — attach/detach without table ownership). Also backfill NULL sensitivity BEFORE
-- attaching (see 02_columns.sql correction), or the policy hides every pre-tiering row.
--
-- Replace placeholders:
--   <<DATABASE>>   e.g. BAYONE_INTERNALINFO
--   <<SCHEMA>>     e.g. PUBLIC
--
-- The policy is the real enforcement boundary: even a bare `SELECT * FROM blocks`
-- has private/protected rows dropped by Snowflake itself, not by the app. A row is
-- returned when ANY of these holds:
--   1. it is shared           (sensitivity = 'internal'), OR
--   2. the caller owns it      (sensitivity = 'private' AND ingested_by = the identity
--                               the app bound this session), OR
--   3. it is group-protected   (sensitivity = 'protected' AND the caller is in the one
--                               privileged group — the app bound BAYI_PROTECTED='true'), OR
--   4. the caller is admin     (break-glass role, or ACCOUNTADMIN during transition).
--
-- Three data tiers: 'internal' (everyone), 'private' (only the ingester), 'protected'
-- (only members of the single privileged Entra/M365 group — the old SG-BayI-Sensitive).
--
-- Both the caller identity and the protected-group flag are carried in SESSION
-- VARIABLES the app sets per request (see SnowflakeConnection.bind_session + web.py
-- /api/ask). The variable NAMES must match CALLER_SESSION_VAR (default: BAYI_CALLER)
-- and PROTECTED_SESSION_VAR (default: BAYI_PROTECTED) in the app config.
--
-- >>> BUILD-TIME DETAIL TO CONFIRM <<<
-- This uses getvariable('BAYI_CALLER') / getvariable('BAYI_PROTECTED') inside the
-- policy body. Confirm your Snowflake edition/version evaluates session variables
-- inside a row-access policy. If it does not, the fallback is a small session-scoped
-- mapping table or per-user Snowflake identities (CURRENT_USER()) plus a group-role
-- membership check; the app-side binding is unchanged.
-- ============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE <<DATABASE>>;
USE SCHEMA <<SCHEMA>>;

-- Detach first so CREATE OR REPLACE can update the policy body (Snowflake refuses to
-- replace a policy that is still attached to a table). These two DROP statements error
-- the very first time (nothing attached yet) — that is expected; run under your client's
-- "continue on error" mode, or skip them on first install.
ALTER TABLE blocks DROP ROW ACCESS POLICY rap_ownership;
ALTER TABLE facts DROP ROW ACCESS POLICY rap_ownership;

-- CREATE OR REPLACE (not IF NOT EXISTS) so re-running this script updates the body when
-- the tier logic changes — e.g. adding the 'protected' branch below.
CREATE OR REPLACE ROW ACCESS POLICY rap_ownership
    AS (sensitivity VARCHAR, ingested_by VARCHAR) RETURNS BOOLEAN ->
        sensitivity = 'internal'
        OR (sensitivity = 'private' AND ingested_by = getvariable('BAYI_CALLER'))
        OR (sensitivity = 'protected' AND getvariable('BAYI_PROTECTED') = 'true')
        OR current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN');

-- Re-attach to both tables on the (sensitivity, ingested_by) column pair.
ALTER TABLE blocks ADD ROW ACCESS POLICY rap_ownership ON (sensitivity, ingested_by);
ALTER TABLE facts ADD ROW ACCESS POLICY rap_ownership ON (sensitivity, ingested_by);
