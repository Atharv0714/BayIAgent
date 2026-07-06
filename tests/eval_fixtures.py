"""Ground-truth eval fixtures for the agent, against live tables in the warehouse.

Each case pairs a natural-language `question` with a `ground_truth_sql`. The runner
computes the expected value by executing `ground_truth_sql` directly, then asserts the
agent's returned value matches. The expected values are therefore live ground truth,
not hardcoded guesses.

Identifiers are confirmed against the real table. The table name and several columns
are quoted identifiers (spaces / mixed case), so they must be referenced with double
quotes in SQL; the questions name them explicitly so the agent can resolve them.
"""

from dataclasses import dataclass

TABLE = 'PUBLIC."Billable Data by Job Description"'

COL_AMOUNT = '"Bill Rate"'      # numeric column to SUM / filter on
COL_CATEGORY = '"Client Name"'  # categorical column to GROUP BY / count distinct

# Second table, now modeled in the same semantic view (clean, unquoted identifiers).
SKILL_TABLE = "PUBLIC.MASTERSKILLLIST"
SKILL_CATEGORY = "PRIMARYSKILL"  # categorical column to GROUP BY / count distinct


@dataclass(frozen=True)
class EvalCase:
    name: str
    kind: str  # count | distinct | sum | filter | group_by
    question: str        # raw-SQL phrasing (names the table/columns) for run_sql
    ground_truth_sql: str
    nl_question: str     # business phrasing for the Cortex Analyst semantic layer


CASES: list[EvalCase] = [
    EvalCase(
        name="row_count",
        kind="count",
        question=f'How many rows are in the table {TABLE}?',
        ground_truth_sql=f"SELECT COUNT(*) FROM {TABLE}",
        nl_question="How many placement rows are there in total?",
    ),
    EvalCase(
        name="distinct_clients",
        kind="distinct",
        question=f'In {TABLE}, how many distinct values are in the {COL_CATEGORY} column?',
        ground_truth_sql=f"SELECT COUNT(DISTINCT {COL_CATEGORY}) FROM {TABLE}",
        nl_question="How many distinct clients are there?",
    ),
    EvalCase(
        name="total_bill_rate",
        kind="sum",
        question=f'In {TABLE}, what is the total (sum) of the {COL_AMOUNT} column?',
        ground_truth_sql=f"SELECT SUM({COL_AMOUNT}) FROM {TABLE}",
        nl_question="What is the total bill rate across all placements?",
    ),
    EvalCase(
        name="rows_with_positive_bill_rate",
        kind="filter",
        question=f'In {TABLE}, how many rows have {COL_AMOUNT} greater than zero?',
        ground_truth_sql=f"SELECT COUNT(*) FROM {TABLE} WHERE {COL_AMOUNT} > 0",
        nl_question="How many placements have a bill rate greater than zero?",
    ),
    EvalCase(
        name="top_client_by_rows",
        kind="group_by",
        question=f'In {TABLE}, which {COL_CATEGORY} appears in the most rows?',
        ground_truth_sql=(
            f"SELECT {COL_CATEGORY} FROM {TABLE} "
            f"GROUP BY {COL_CATEGORY} ORDER BY COUNT(*) DESC, {COL_CATEGORY} LIMIT 1"
        ),
        nl_question="Which client has the most placement rows?",
    ),
    EvalCase(
        name="highest_total_bill_rate_for_one_client",
        kind="group_by",
        question=(
            f'In {TABLE}, what is the highest total {COL_AMOUNT} '
            f'for any single {COL_CATEGORY}?'
        ),
        ground_truth_sql=(
            f"SELECT SUM({COL_AMOUNT}) AS s FROM {TABLE} "
            f"GROUP BY {COL_CATEGORY} ORDER BY s DESC LIMIT 1"
        ),
        nl_question="What is the highest total bill rate for any single client?",
    ),
    # ── Master Skill List cases (second table in the same semantic view) ──
    EvalCase(
        name="skill_row_count",
        kind="count",
        question=f"How many rows are in the table {SKILL_TABLE}?",
        ground_truth_sql=f"SELECT COUNT(*) FROM {SKILL_TABLE}",
        nl_question="How many candidate skill records are there in the master skill list?",
    ),
    EvalCase(
        name="distinct_primary_skills",
        kind="distinct",
        question=f"In {SKILL_TABLE}, how many distinct values are in the {SKILL_CATEGORY} column?",
        ground_truth_sql=f"SELECT COUNT(DISTINCT {SKILL_CATEGORY}) FROM {SKILL_TABLE}",
        nl_question="How many distinct primary skills are there?",
    ),
    EvalCase(
        name="top_primary_skill",
        kind="group_by",
        question=f"In {SKILL_TABLE}, which {SKILL_CATEGORY} appears in the most rows?",
        ground_truth_sql=(
            f"SELECT {SKILL_CATEGORY} FROM {SKILL_TABLE} "
            f"GROUP BY {SKILL_CATEGORY} ORDER BY COUNT(*) DESC, {SKILL_CATEGORY} LIMIT 1"
        ),
        nl_question="Which primary skill appears in the most candidate records?",
    ),
]
