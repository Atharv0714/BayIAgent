-- Semantic view for Snowflake Cortex Analyst.
--
-- Source of truth for the semantic model CortexAnalystTool reasons over. Built live
-- against BAYONE_INTERNALINFO; checking it in keeps the view reproducible from source
-- (the tool itself only *reads* the view at query time).
--
-- Goal: give Cortex Analyst the same analytical reach as the run_sql path by exposing
-- every useful column of the structured tables as a dimension/fact/metric, so the model
-- can answer date trends, rate/margin math, and industry/service-line questions — not
-- just simple counts. Covers three independent analytical tables (no join between them,
-- so each question resolves to one):
--   billable      -> "Billable Data by Job Description"  (placements, rates, dates, clients)
--   skills        -> MASTERSKILLLIST                     (candidate skills, service lines)
--   case_studies  -> V_CASE_STUDIES                      (delivered case studies by industry)
--
-- Two source tables are deliberately NOT modeled here: BAYONE_GENERICOVERVIEW and
-- LAM_ACCOUNTPLAN are PPTX/slide prose (title slides, narrative text, OCR), not
-- aggregatable data — they belong to a retrieval/RAG path, not a Cortex analytics view.
--
-- Apply with a role that owns the target schema (e.g. ACCOUNTADMIN):
--     snowsql -f sql/billable_semantic_view.sql
-- or paste into a Snowsight worksheet.
--
-- Cortex Analyst compiles this DDL into a YAML semantic model with stricter rules than
-- raw SQL — the ones that shape this file:
--   1. No PRIMARY KEY on a physical column whose name has a space ("START ID"): YAML
--      logical names must start with a letter/underscore and contain no spaces. The key
--      is optional for aggregate queries, so it is omitted.
--   2. METRICS must reference LOGICAL names (the aliases below), not physical quoted
--      columns — hence COUNT(DISTINCT billable.client_name), not ..."Client Name".
--   3. Logical names are unique across the WHOLE view, so members of the skills and
--      case_studies tables are prefixed (skill_*, cs_*) to avoid colliding with the
--      billable table's members.

-- ---------------------------------------------------------------------------
-- Cleaning view: CASESTUDIES stores its header as the first data row and names its
-- columns C1..C12. Project them to real names and drop the header row so Cortex sees
-- clean categoricals (industry, service_line, client_tier) instead of "C4".
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW BAYONE_INTERNALINFO.PUBLIC.V_CASE_STUDIES AS
SELECT
  C1  AS CASE_STUDY_ID,
  C2  AS TITLE,
  C3  AS SERVICE_LINE,
  C4  AS INDUSTRY,
  C5  AS CLIENT_TIER,
  C6  AS TECH_STACK,
  C7  AS CLIENT_CONTEXT,
  C8  AS CHALLENGES,
  C9  AS SOLUTION,
  C10 AS OUTCOMES,
  C11 AS SOURCE_DOC,
  C12 AS SOURCE_VERSION
FROM BAYONE_INTERNALINFO.PUBLIC.CASESTUDIES
WHERE C1 <> 'case_study_id';  -- drop the embedded header row

-- ---------------------------------------------------------------------------
-- Semantic view
-- ---------------------------------------------------------------------------
CREATE OR REPLACE SEMANTIC VIEW BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC
  TABLES (
    billable AS BAYONE_INTERNALINFO.PUBLIC."Billable Data by Job Description"
      WITH SYNONYMS=('billable data', 'placements', 'billable table', 'staffing', 'consultants placed', 'engagements')
      COMMENT='One row per billable placement / consultant engagement at a client.',
    skills AS BAYONE_INTERNALINFO.PUBLIC.MASTERSKILLLIST
      WITH SYNONYMS=('master skill list', 'skill list', 'candidate skills', 'skills data', 'talent', 'bench')
      COMMENT='One row per candidate skill record.',
    case_studies AS BAYONE_INTERNALINFO.PUBLIC.V_CASE_STUDIES
      WITH SYNONYMS=('case studies', 'delivered work', 'projects', 'engagements delivered', 'proof points', 'success stories')
      COMMENT='One row per delivered BayOne case study, tagged by industry and service line.'
  )
  FACTS (
    -- billable measures
    billable.bill_rate AS billable."Bill Rate" COMMENT='Bill rate charged for the placement.',
    billable.gm AS billable."GM" COMMENT='Gross margin on the placement.',
    -- skills measures
    skills.agreed_bill_rate AS skills.AGREEDBILLRATE COMMENT='Agreed bill rate for the candidate.',
    skills.skill_gm AS skills.GM COMMENT='Gross margin for the candidate record.'
  )
  DIMENSIONS (
    -- billable dimensions
    billable.client_name AS billable."Client Name"
      WITH SYNONYMS=('client', 'customer', 'account', 'company') COMMENT='Client company the consultant is placed at.',
    billable.candidate_full_name AS billable."CANDIDATE FULL NAME"
      WITH SYNONYMS=('candidate', 'consultant', 'contractor', 'placed candidate', 'person') COMMENT='Full name of the placed consultant.',
    billable.job_title AS billable."JOB TITLE"
      WITH SYNONYMS=('role', 'title', 'position') COMMENT='Placement job title.',
    billable.placement_status AS billable."PLACEMENT STATUS"
      WITH SYNONYMS=('status', 'placement state') COMMENT='Placement status (e.g. Approved).',
    billable.start_date AS billable."START DATE"
      WITH SYNONYMS=('start', 'placement start', 'engagement start', 'began', 'started on') COMMENT='Date the placement started.',
    billable.end_date AS billable."END DATE"
      WITH SYNONYMS=('end', 'placement end', 'engagement end', 'finished', 'ended on', 'roll off') COMMENT='Date the placement ends / ended.',
    -- skills dimensions
    skills.candidate_name AS skills.CANDIDATENAME
      WITH SYNONYMS=('candidate', 'consultant', 'person') COMMENT='Candidate name.',
    skills.primary_skill AS skills.PRIMARYSKILL
      WITH SYNONYMS=('skill', 'primary skill', 'top skill', 'main skill') COMMENT='Primary skill of the candidate.',
    skills.secondary_skill AS skills.SECONDARYSKILL
      WITH SYNONYMS=('secondary skill') COMMENT='Secondary skill of the candidate.',
    skills.other_skills AS skills.OTHERSKILLS
      WITH SYNONYMS=('additional skills', 'more skills', 'extra skills') COMMENT='Other skills of the candidate.',
    skills.service_line AS skills.SERVICELINE
      WITH SYNONYMS=('service line', 'practice', 'business line') COMMENT='Service line / practice area for the candidate.',
    skills.country AS skills.COUNTRY
      WITH SYNONYMS=('country', 'location', 'geography') COMMENT='Candidate country.',
    skills.job_company AS skills.JOBCOMPANY
      WITH SYNONYMS=('company', 'employer', 'hiring company') COMMENT='Company on the skill record.',
    skills.skill_job_title AS skills.JOBTITLE
      WITH SYNONYMS=('candidate role', 'candidate title') COMMENT='Job title on the skill record.',
    skills.skill_placement_status AS skills.PLACEMENTSTATUS
      WITH SYNONYMS=('candidate placement status') COMMENT='Placement status on the skill record.',
    skills.skill_status AS skills.STATUS
      WITH SYNONYMS=('candidate status', 'active status') COMMENT='Status on the skill record (Active/Inactive/Offered).',
    -- case study dimensions
    case_studies.cs_id AS case_studies.CASE_STUDY_ID
      WITH SYNONYMS=('case study id', 'case study number') COMMENT='Case study identifier.',
    case_studies.cs_title AS case_studies.TITLE
      WITH SYNONYMS=('case study title', 'project title', 'engagement title') COMMENT='Case study title.',
    case_studies.cs_service_line AS case_studies.SERVICE_LINE
      WITH SYNONYMS=('service line', 'practice', 'capability', 'offering') COMMENT='Service line the case study delivered.',
    case_studies.cs_industry AS case_studies.INDUSTRY
      WITH SYNONYMS=('industry', 'vertical', 'sector', 'market') COMMENT='Client industry / vertical (e.g. Automotive, Retail, Technology).',
    case_studies.cs_client_tier AS case_studies.CLIENT_TIER
      WITH SYNONYMS=('client tier', 'account tier', 'client size') COMMENT='Client tier (e.g. Fortune 500, Enterprise).',
    case_studies.cs_tech_stack AS case_studies.TECH_STACK
      WITH SYNONYMS=('tech stack', 'technologies', 'tools', 'technology used') COMMENT='Technologies used, pipe-delimited.'
  )
  METRICS (
    -- billable metrics
    billable.row_count AS COUNT(*) COMMENT='Number of placement rows.',
    billable.distinct_client_count AS COUNT(DISTINCT billable.client_name) COMMENT='Number of distinct clients.',
    billable.distinct_placed_candidate_count AS COUNT(DISTINCT billable.candidate_full_name) COMMENT='Number of distinct placed consultants.',
    billable.distinct_job_title_count AS COUNT(DISTINCT billable.job_title) COMMENT='Number of distinct placement job titles.',
    billable.total_bill_rate AS SUM(billable.bill_rate) COMMENT='Total bill rate across placements.',
    billable.avg_bill_rate AS AVG(billable.bill_rate) COMMENT='Average bill rate across placements.',
    billable.total_gm AS SUM(billable.gm) COMMENT='Total gross margin across placements.',
    billable.avg_gm AS AVG(billable.gm) COMMENT='Average gross margin across placements.',
    -- skills metrics
    skills.skill_row_count AS COUNT(*) COMMENT='Number of candidate skill records.',
    skills.distinct_candidate_count AS COUNT(DISTINCT skills.candidate_name) COMMENT='Number of distinct candidates.',
    skills.distinct_primary_skill_count AS COUNT(DISTINCT skills.primary_skill) COMMENT='Number of distinct primary skills.',
    skills.distinct_service_line_count AS COUNT(DISTINCT skills.service_line) COMMENT='Number of distinct service lines in the skill list.',
    skills.total_agreed_bill_rate AS SUM(skills.agreed_bill_rate) COMMENT='Total agreed bill rate across candidates.',
    skills.avg_agreed_bill_rate AS AVG(skills.agreed_bill_rate) COMMENT='Average agreed bill rate.',
    skills.total_skill_gm AS SUM(skills.skill_gm) COMMENT='Total gross margin across candidate records.',
    -- case study metrics
    case_studies.case_study_count AS COUNT(*) COMMENT='Number of case studies.',
    case_studies.distinct_cs_industry_count AS COUNT(DISTINCT case_studies.cs_industry) COMMENT='Number of distinct industries with case studies.',
    case_studies.distinct_cs_service_line_count AS COUNT(DISTINCT case_studies.cs_service_line) COMMENT='Number of distinct service lines with case studies.'
  )
  COMMENT='Semantic view over BayOne billable placements, master skill list, and delivered case studies for Cortex Analyst.';

-- ---------------------------------------------------------------------------
-- Grants for the Cortex Analyst PAT's role.
--
-- The PAT runs under a restricted role (Snowflake blocks PATs on ACCOUNTADMIN) — here
-- the account-wide PUBLIC role. Cortex Analyst must resolve the database, schema,
-- semantic view, and every underlying table/view to compile the model. For a
-- multi-user account, replace PUBLIC with a dedicated read-only role and pin the PAT
-- to it instead of granting to everyone.
--
-- NOTE: CREATE OR REPLACE above drops the view's grants, so the SELECT grant on the
-- semantic view must be re-applied every time the view is rebuilt.
-- ---------------------------------------------------------------------------
GRANT USAGE ON DATABASE BAYONE_INTERNALINFO TO ROLE PUBLIC;
GRANT USAGE ON SCHEMA BAYONE_INTERNALINFO.PUBLIC TO ROLE PUBLIC;
GRANT SELECT ON TABLE BAYONE_INTERNALINFO.PUBLIC."Billable Data by Job Description" TO ROLE PUBLIC;
GRANT SELECT ON TABLE BAYONE_INTERNALINFO.PUBLIC.MASTERSKILLLIST TO ROLE PUBLIC;
GRANT SELECT ON TABLE BAYONE_INTERNALINFO.PUBLIC.CASESTUDIES TO ROLE PUBLIC;
GRANT SELECT ON VIEW BAYONE_INTERNALINFO.PUBLIC.V_CASE_STUDIES TO ROLE PUBLIC;
GRANT SELECT ON SEMANTIC VIEW BAYONE_INTERNALINFO.PUBLIC.BILLABLE_SEMANTIC TO ROLE PUBLIC;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE PUBLIC;
