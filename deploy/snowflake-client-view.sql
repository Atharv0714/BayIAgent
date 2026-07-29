-- ============================================================================
-- BayI Agent — canonical client view (V_CLIENT_PLACEMENTS)
--
-- WHY THIS EXISTS
-- "How many clients does BayOne service?" returned a different number on every run
-- (58, then 60, then 61) because the agent invented its own CASE/REGEXP grouping each
-- time and merged a different subset of name variants. Client identity is a data
-- modelling decision, not something an LLM should re-derive per question, so it lives
-- here in SQL. With this view the count is reproducible.
--
-- WHAT IT ADDS to MASTERSKILLLIST
--   CLIENT_NAME  one canonical name per company. Branch / contract-vehicle / geography
--                suffixes are stripped generically (SOW, India, Costa Rica, CAD,
--                Workday, USA, with '-', '_' or ' ' separators), which covers
--                'Cisco - SOW', 'Cisco - India', 'Cisco - Costa Rica', 'Walmart-SOW',
--                'Rivian SOW', 'eBay - CAD', 'Applied Materials_India' and
--                'Hewlett-Packard Enterprise (HPE) - Workday'. Two names differ by
--                abbreviation or casing rather than a suffix ('HPE' vs the full name,
--                'ebay' vs 'eBay'), so those are mapped explicitly.
--   IS_INTERNAL  TRUE for BayOne's own bench/overhead rows ('BayOne', 'Bayone Bench',
--                'Bayone Solutions Inc') — headcount, but not customers.
--
-- Verified on 2026-07-29: 70 distinct active JOB_COMPANY values collapse to 58 distinct
-- active CLIENT_NAME values once internal rows are excluded, merging six groups
-- (Cisco 4 variants, HPE 3, eBay 2, Rivian 2, Walmart 2, Applied Materials 2).
--
-- Run as ACCOUNTADMIN. Idempotent (CREATE OR REPLACE). Re-run after adding a client
-- whose name carries a new suffix.
-- ============================================================================

USE ROLE ACCOUNTADMIN;
USE DATABASE BAYONE_INTERNALINFO;
USE SCHEMA PUBLIC;

CREATE OR REPLACE VIEW V_CLIENT_PLACEMENTS AS
SELECT
    m.*,
    (JOB_COMPANY ILIKE '%bayone%' OR JOB_COMPANY ILIKE '%bench%') AS IS_INTERNAL,
    CASE
      WHEN JOB_COMPANY ILIKE '%bayone%' OR JOB_COMPANY ILIKE '%bench%'
        THEN 'BayOne (internal)'
      WHEN JOB_COMPANY ILIKE 'hewlett%' OR JOB_COMPANY ILIKE 'hpe%'
        THEN 'Hewlett-Packard Enterprise (HPE)'
      WHEN JOB_COMPANY ILIKE 'ebay%' THEN 'eBay'
      ELSE TRIM(REGEXP_REPLACE(JOB_COMPANY,
             '[ _-]*(-\\s*)?(SOW|India|Costa Rica|CAD|Workday|USA)\\s*$', '', 1, 0, 'i'))
    END AS CLIENT_NAME
FROM MASTERSKILLLIST m;

-- The query agent reads as BAYI_READ.
GRANT SELECT ON VIEW V_CLIENT_PLACEMENTS TO ROLE BAYI_READ;

-- ── Verify ──────────────────────────────────────────────────────────────────
-- Canonical client count (expected: 58 as of 2026-07-29).
SELECT COUNT(DISTINCT CLIENT_NAME) AS active_clients
FROM V_CLIENT_PLACEMENTS
WHERE STATUS = 'Active' AND NOT IS_INTERNAL;

-- Which groups actually merged, so a new suffix showing up is easy to spot.
SELECT CLIENT_NAME, COUNT(*) AS placements, COUNT(DISTINCT JOB_COMPANY) AS variants
FROM V_CLIENT_PLACEMENTS
WHERE STATUS = 'Active' AND NOT IS_INTERNAL
GROUP BY CLIENT_NAME
HAVING COUNT(DISTINCT JOB_COMPANY) > 1
ORDER BY placements DESC;
