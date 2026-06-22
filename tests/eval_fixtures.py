"""Ground-truth eval fixtures for the agent, against BILLABLE_DATA_BY_JOB_DESCRIPTION.

Each case pairs a natural-language `question` with a `ground_truth_sql`. The runner
computes the expected value by executing `ground_truth_sql` directly, then asserts the
agent's returned value matches. The expected values are therefore live ground truth,
not hardcoded guesses.

  ┌──────────────────────────────────────────────────────────────────────────┐
  │ CONFIRM THESE COLUMN NAMES against the real table before running the eval.│
  │ They are inferred from the table name and WILL need editing if wrong.      │
  │ Run `uv run pytest -k print_schema -s -m integration` to list the columns. │
  └──────────────────────────────────────────────────────────────────────────┘
"""

from dataclasses import dataclass

TABLE = "BILLABLE_DATA_BY_JOB_DESCRIPTION"

# --- columns to confirm against the live table ---
COL_AMOUNT = "BILLABLE_AMOUNT"      # a numeric column to SUM / filter on
COL_CATEGORY = "JOB_DESCRIPTION"    # a categorical column to GROUP BY / count distinct


@dataclass(frozen=True)
class EvalCase:
    name: str
    kind: str  # count | distinct | sum | filter | group_by
    question: str
    ground_truth_sql: str


CASES: list[EvalCase] = [
    EvalCase(
        name="row_count",
        kind="count",
        question="How many rows are in the billable data table?",
        ground_truth_sql=f"SELECT COUNT(*) FROM {TABLE}",
    ),
    EvalCase(
        name="distinct_job_descriptions",
        kind="distinct",
        question="How many distinct job descriptions are there in the billable data?",
        ground_truth_sql=f"SELECT COUNT(DISTINCT {COL_CATEGORY}) FROM {TABLE}",
    ),
    EvalCase(
        name="total_billable_amount",
        kind="sum",
        question="What is the total billable amount across all rows?",
        ground_truth_sql=f"SELECT SUM({COL_AMOUNT}) FROM {TABLE}",
    ),
    EvalCase(
        name="rows_with_positive_amount",
        kind="filter",
        question="How many rows have a billable amount greater than zero?",
        ground_truth_sql=f"SELECT COUNT(*) FROM {TABLE} WHERE {COL_AMOUNT} > 0",
    ),
    EvalCase(
        name="top_job_description_by_rows",
        kind="group_by",
        question="Which job description appears in the most rows?",
        ground_truth_sql=(
            f"SELECT {COL_CATEGORY} FROM {TABLE} "
            f"GROUP BY {COL_CATEGORY} ORDER BY COUNT(*) DESC, {COL_CATEGORY} LIMIT 1"
        ),
    ),
    EvalCase(
        name="highest_total_amount_for_one_job",
        kind="group_by",
        question="What is the highest total billable amount for any single job description?",
        ground_truth_sql=(
            f"SELECT SUM({COL_AMOUNT}) AS s FROM {TABLE} "
            f"GROUP BY {COL_CATEGORY} ORDER BY s DESC LIMIT 1"
        ),
    ),
]
