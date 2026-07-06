"""Cortex Analyst eval harness — the chunk-3a done-condition.

Same fixtures and same live-computed expected values as test_evals.py, but the agent
is wired with ONLY the CortexAnalystTool and asked the business-phrased `nl_question`.
Cortex Analyst turns the question into SQL over the semantic view; the tool executes
that SQL and the agent reports the value. Proves the tool swap works behind the
unchanged agent loop, grounded in the same numbers run_sql produces.

Needs Snowflake creds, an Anthropic key, AND Cortex config (SNOWFLAKE_PAT +
CORTEX_SEMANTIC_VIEW); skips otherwise.
"""

from typing import Any

import pytest

from eval_fixtures import CASES, EvalCase

pytestmark = pytest.mark.integration

_REL_TOL = 1e-6


def _scalar(query_result) -> Any:
    assert query_result.row_count >= 1, "ground_truth_sql returned no rows"
    return query_result.rows[0][0]


def _matches(expected: Any, actual: Any) -> bool:
    if actual is None:
        return False
    try:
        e, a = float(expected), float(actual)
        return abs(e - a) <= _REL_TOL * max(1.0, abs(e))
    except (TypeError, ValueError):
        return str(expected).strip().casefold() == str(actual).strip().casefold()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_cortex_matches_ground_truth(case: EvalCase, cortex_agent, connection) -> None:
    expected = _scalar(connection.execute(case.ground_truth_sql))

    answer = cortex_agent.ask(case.nl_question)

    assert _matches(expected, answer.value), (
        f"[{case.name}] expected {expected!r}, agent returned {answer.value!r}\n"
        f"  nl_question: {case.nl_question}\n"
        f"  truth SQL:   {case.ground_truth_sql}\n"
        f"  agent SQL:   {answer.executed_sql}\n"
        f"  agent says:  {answer.answer}"
    )
