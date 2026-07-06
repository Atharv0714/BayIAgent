-- Semantic view for Snowflake Cortex Analyst.
--
-- This is the source of truth for the semantic model CortexAnalystTool reasons over.
-- It was built live against BAYONE_INTERNALINFO; checking it in makes the view
-- reproducible from source (the tool itself only *reads* the view at query time).
--
-- Covers two independent tables (no join relationship between them, so each
-- question resolves to one of them):
--   billable  -> "Billable Data by Job Description"  (placements, bill rates, clients)
--   skills    -> MASTERSKILLLIST                     (candidate skills, service lines)
--
-- Apply with a role that owns the target schema (e.g. ACCOUNTADMIN):
--     snowsql -f sql/billable_semantic_view.sql
-- or paste into a Snowsight worksheet.
--
-- Cortex Analyst compiles this DDL into a YAML semantic model with stricter rules
-- than raw SQL — three of them are encoded here:
--   1. No PRIMARY KEY on a physical column whose name has a space ("START ID"):
--      YAML logical names must start with a letter/underscore and contain no spaces.
--      The primary key is optional for our aggregate queries, so it is omitted.
--   2. METRICS must reference LOGICAL names (the dimension/fact aliases below),
--      not physical quoted columns — hence COUNT(DISTINCT billable.client_name),
--      not COUNT(DISTINCT billable."Client Name").
--   3. Logical names are unique per view, so the skills table's members are
--      prefixed (skill_job_title, skill_gm, skill_row_count, ...) to avoid
--      colliding with the billable table's (job_title, gm, row_count, ...).

CREATE OR REPLACE SEMANTIC VIEW BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC
  TABLES (
    billable AS BAYONE_INTERNALINFO.PUBLIC."Billable Data by Job Description"
      WITH SYNONYMS=('billable data', 'placements', 'billable table')
      COMMENT='One row per billable placement.',
    skills AS BAYONE_INTERNALINFO.PUBLIC.MASTERSKILLLIST
      WITH SYNONYMS=('master skill list', 'skill list', 'candidate skills', 'skills data')
      COMMENT='One row per candidate skill record.'
  )
  FACTS (
    billable.bill_rate AS billable."Bill Rate" COMMENT='Bill rate for the placement.',
    billable.gm AS billable."GM" COMMENT='Gross margin.',
    skills.agreed_bill_rate AS skills.AGREEDBILLRATE COMMENT='Agreed bill rate for the candidate.',
    skills.skill_gm AS skills.GM COMMENT='Gross margin for the candidate record.'
  )
  DIMENSIONS (
    billable.client_name AS billable."Client Name"
      WITH SYNONYMS=('client', 'customer', 'account') COMMENT='Client company name.',
    billable.job_title AS billable."JOB TITLE"
      WITH SYNONYMS=('role', 'title') COMMENT='Placement job title.',
    billable.placement_status AS billable."PLACEMENT STATUS"
      WITH SYNONYMS=('status') COMMENT='Placement status.',
    skills.candidate_name AS skills.CANDIDATENAME
      WITH SYNONYMS=('candidate', 'consultant', 'person') COMMENT='Candidate name.',
    skills.primary_skill AS skills.PRIMARYSKILL
      WITH SYNONYMS=('skill', 'primary skill', 'top skill', 'main skill') COMMENT='Primary skill of the candidate.',
    skills.secondary_skill AS skills.SECONDARYSKILL
      WITH SYNONYMS=('secondary skill') COMMENT='Secondary skill of the candidate.',
    skills.service_line AS skills.SERVICELINE
      WITH SYNONYMS=('service line', 'practice', 'business line') COMMENT='Service line / practice area.',
    skills.country AS skills.COUNTRY
      WITH SYNONYMS=('country', 'location', 'geography') COMMENT='Candidate country.',
    skills.job_company AS skills.JOBCOMPANY
      WITH SYNONYMS=('company', 'employer', 'hiring company') COMMENT='Company for the skill record.',
    skills.skill_job_title AS skills.JOBTITLE
      WITH SYNONYMS=('candidate role', 'candidate title') COMMENT='Job title on the skill record.',
    skills.skill_placement_status AS skills.PLACEMENTSTATUS
      WITH SYNONYMS=('candidate placement status') COMMENT='Placement status on the skill record.',
    skills.skill_status AS skills.STATUS
      WITH SYNONYMS=('candidate status') COMMENT='Status on the skill record.'
  )
  METRICS (
    billable.row_count AS COUNT(*) COMMENT='Number of placement rows.',
    billable.total_bill_rate AS SUM(billable.bill_rate) COMMENT='Total bill rate across placements.',
    billable.distinct_client_count AS COUNT(DISTINCT billable.client_name) COMMENT='Number of distinct clients.',
    skills.skill_row_count AS COUNT(*) COMMENT='Number of candidate skill records.',
    skills.distinct_candidate_count AS COUNT(DISTINCT skills.candidate_name) COMMENT='Number of distinct candidates.',
    skills.distinct_primary_skill_count AS COUNT(DISTINCT skills.primary_skill) COMMENT='Number of distinct primary skills.',
    skills.distinct_service_line_count AS COUNT(DISTINCT skills.service_line) COMMENT='Number of distinct service lines.',
    skills.total_agreed_bill_rate AS SUM(skills.agreed_bill_rate) COMMENT='Total agreed bill rate across candidates.',
    skills.avg_agreed_bill_rate AS AVG(skills.agreed_bill_rate) COMMENT='Average agreed bill rate.',
    skills.total_skill_gm AS SUM(skills.skill_gm) COMMENT='Total gross margin across candidate records.'
  )
  COMMENT='Semantic view over the Billable Data and Master Skill List tables for Cortex Analyst';

-- Grants for the Cortex Analyst PAT's role.
--
-- The PAT runs under a restricted role (Snowflake blocks PATs on ACCOUNTADMIN),
-- here the account-wide PUBLIC role. Cortex Analyst must resolve the database,
-- schema, semantic view, and the underlying tables to compile the model. For a
-- multi-user account, replace PUBLIC with a dedicated read-only role and pin the
-- PAT to it instead of granting to everyone.
--
-- NOTE: CREATE OR REPLACE above drops the view's grants, so the SELECT grant on
-- the semantic view must be re-applied every time the view is rebuilt.
GRANT USAGE ON DATABASE BAYONE_INTERNALINFO TO ROLE PUBLIC;
GRANT USAGE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE PUBLIC;
GRANT SELECT ON TABLE BAYONE_INTERNALINFO.PUBLIC."Billable Data by Job Description" TO ROLE PUBLIC;
GRANT SELECT ON TABLE BAYONE_INTERNALINFO.PUBLIC.MASTERSKILLLIST TO ROLE PUBLIC;
GRANT SELECT ON SEMANTIC VIEW BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC TO ROLE PUBLIC;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE PUBLIC;
