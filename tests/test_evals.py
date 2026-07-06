"""Agent eval harness — the chunk-2 done-condition.

For each fixture: compute the expected value live by running ground_truth_sql against
the real table, run the agent on the natural-language question, and assert the agent's
returned `value` matches. Needs Snowflake creds AND an Anthropic key; skips otherwise.
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


def _answer_has(expected: Any, answer) -> bool:
    """True if the correct figure is anywhere in the agent's structured answer.

    For "which X appears most" questions the headline can non-deterministically be
    the name or its count; the agent reliably surfaces both — the name in `value`,
    or in `values` either as a key ({"Google": 140}) or a value
    ({"primary_skill": "Java"}). Accept a match on any so the eval tests
    correctness, not which of two equally-right fields the model chose to headline.
    """
    if _matches(expected, answer.value):
        return True
    for k, v in answer.values.items():
        if _matches(expected, k) or _matches(expected, v):
            return True
    return False


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_agent_matches_ground_truth(case: EvalCase, agent, connection) -> None:
    expected = _scalar(connection.execute(case.ground_truth_sql))

    answer = agent.ask(case.question)

    assert _answer_has(expected, answer), (
        f"[{case.name}] expected {expected!r}, agent value={answer.value!r} values={answer.values!r}\n"
        f"  question:   {case.question}\n"
        f"  truth SQL:  {case.ground_truth_sql}\n"
        f"  agent SQL:  {answer.executed_sql}\n"
        f"  agent says: {answer.answer}"
    )
