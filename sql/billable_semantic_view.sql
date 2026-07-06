-- Semantic view for Snowflake Cortex Analyst (chunk 3a).
--
-- This is the source of truth for the semantic model CortexAnalystTool reasons over.
-- It was built live against BAYONE_INTERNALINFO; checking it in makes the view
-- reproducible from source (the tool itself only *reads* the view at query time).
--
-- Apply with a role that owns the target schema (e.g. ACCOUNTADMIN):
--     snowsql -f sql/billable_semantic_view.sql
-- or paste into a Snowsight worksheet.
--
-- Cortex Analyst compiles this DDL into a YAML semantic model with stricter rules
-- than raw SQL — two of them bit us and are encoded here:
--   1. No PRIMARY KEY on a physical column whose name has a space ("START ID"):
--      YAML logical names must start with a letter/underscore and contain no spaces.
--      The primary key is optional for our aggregate queries, so it is omitted.
--   2. METRICS must reference LOGICAL names (the dimension/fact aliases below),
--      not physical quoted columns — hence COUNT(DISTINCT billable.client_name),
--      not COUNT(DISTINCT billable."Client Name").

CREATE OR REPLACE SEMANTIC VIEW BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC
  TABLES (
    billable AS BAYONE_INTERNALINFO.PUBLIC."Billable Data by Job Description"
      WITH SYNONYMS=('billable data', 'placements', 'billable table')
      COMMENT='One row per billable placement.'
  )
  FACTS (
    billable.bill_rate AS billable."Bill Rate" COMMENT='Bill rate for the placement.',
    billable.gm AS billable."GM" COMMENT='Gross margin.'
  )
  DIMENSIONS (
    billable.client_name AS billable."Client Name"
      WITH SYNONYMS=('client', 'customer', 'account') COMMENT='Client company name.',
    billable.job_title AS billable."JOB TITLE"
      WITH SYNONYMS=('role', 'title') COMMENT='Placement job title.',
    billable.placement_status AS billable."PLACEMENT STATUS"
      WITH SYNONYMS=('status') COMMENT='Placement status.'
  )
  METRICS (
    billable.row_count AS COUNT(*) COMMENT='Number of placement rows.',
    billable.total_bill_rate AS SUM(billable.bill_rate) COMMENT='Total bill rate across placements.',
    billable.distinct_client_count AS COUNT(DISTINCT billable.client_name) COMMENT='Number of distinct clients.'
  )
  COMMENT='Minimal semantic view over the Billable Data table for Cortex Analyst';

-- Grants for the Cortex Analyst PAT's role.
--
-- The PAT runs under a restricted role (Snowflake blocks PATs on ACCOUNTADMIN),
-- here the account-wide PUBLIC role. Cortex Analyst must resolve the database,
-- schema, semantic view, and the underlying table to compile the model. For a
-- multi-user account, replace PUBLIC with a dedicated read-only role and pin the
-- PAT to it instead of granting to everyone.
--
-- NOTE: CREATE OR REPLACE above drops the view's grants, so the SELECT grant on
-- the semantic view must be re-applied every time the view is rebuilt.
GRANT USAGE ON DATABASE BAYONE_INTERNALINFO TO ROLE PUBLIC;
GRANT USAGE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE PUBLIC;
GRANT SELECT ON TABLE BAYONE_INTERNALINFO.PUBLIC."Billable Data by Job Description" TO ROLE PUBLIC;
GRANT SELECT ON SEMANTIC VIEW BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC TO ROLE PUBLIC;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE PUBLIC;
