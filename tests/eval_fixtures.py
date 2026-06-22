"""Ground-truth eval fixtures for the agent, against the live Billable Data table.

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
        question=f'How many rows are in the table {TABLE}?',
        ground_truth_sql=f"SELECT COUNT(*) FROM {TABLE}",
    ),
    EvalCase(
        name="distinct_clients",
        kind="distinct",
        question=f'In {TABLE}, how many distinct values are in the {COL_CATEGORY} column?',
        ground_truth_sql=f"SELECT COUNT(DISTINCT {COL_CATEGORY}) FROM {TABLE}",
    ),
    EvalCase(
        name="total_bill_rate",
        kind="sum",
        question=f'In {TABLE}, what is the total (sum) of the {COL_AMOUNT} column?',
        ground_truth_sql=f"SELECT SUM({COL_AMOUNT}) FROM {TABLE}",
    ),
    EvalCase(
        name="rows_with_positive_bill_rate",
        kind="filter",
        question=f'In {TABLE}, how many rows have {COL_AMOUNT} greater than zero?',
        ground_truth_sql=f"SELECT COUNT(*) FROM {TABLE} WHERE {COL_AMOUNT} > 0",
    ),
    EvalCase(
        name="top_client_by_rows",
        kind="group_by",
        question=f'In {TABLE}, which {COL_CATEGORY} appears in the most rows?',
        ground_truth_sql=(
            f"SELECT {COL_CATEGORY} FROM {TABLE} "
            f"GROUP BY {COL_CATEGORY} ORDER BY COUNT(*) DESC, {COL_CATEGORY} LIMIT 1"
        ),
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
    ),
]
