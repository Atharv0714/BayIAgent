"""Entity-isolation evals — the regression net for cross-entity "bleed".

Every bleed bug found so far was caught by a human reading an answer and then fixed with a
prompt rule. Prompt rules are ADVISORY: the model can quietly stop honouring one when another
rule is added, and nothing fails. These cases turn "someone noticed" into "pytest failed".

Bleed is different from an ordinary wrong answer: the output looks complete and confident while
containing facts belonging to a DIFFERENT entity. Detecting it needs a NEGATIVE assertion — not
just "the right thing is present" but "the neighbouring entity's marker is absent". Each case
therefore carries `must_exclude` markers that only appear if data bled across a boundary.

Real regressions these guard (all observed in production):
  * a Rivian org-structure answer that also described Lam Research's hierarchy;
  * a generated Google deck that credited Macy's "60% code reduction" to Cisco;
  * BayOne's own bench rows counted as client companies.

Marked `integration`: each case runs a live agent turn, so the suite is slow and skips cleanly
without credentials. Run before a deploy or after touching prompts.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class IsolationCase:
    name: str
    question: str
    # Substrings that MUST appear — proves the agent actually found the right entity's data
    # rather than passing by saying nothing.
    must_include: tuple[str, ...]
    # Substrings that must NOT appear anywhere in the answer or its supporting values. These
    # are markers unique to a NEIGHBOURING entity, so their presence means data bled across.
    must_exclude: tuple[str, ...]
    guards: str = ""
    # Optional: SQL whose scalar result the agent's `value` must equal, computed live so the
    # expectation tracks the warehouse instead of going stale.
    ground_truth_sql: str | None = None
    case_insensitive: bool = field(default=True)
    # Where must_exclude is searched. "all" covers the prose and the structured values;
    # "values" covers only the structured data. Use "values" when NAMING the excluded entity
    # in prose is legitimate — e.g. an answer that says "internal bench rows were excluded" is
    # doing the right thing, so scanning its prose for "bench" is a false positive. What
    # matters there is that the entity never appears as a row in the data.
    exclude_scope: str = field(default="all")


CASES: list[IsolationCase] = [
    IsolationCase(
        name="rivian_org_excludes_lam",
        question="What is the org structure at Rivian that our team is currently working with?",
        must_include=("RJ Scaringe",),
        # Lam Research's CEO and COO. One uploaded file once held both charts, and the answer
        # described both companies as a single hierarchy.
        must_exclude=("Tim Archer", "Pat Lord"),
        guards="Rivian and Lam org charts must never merge into one reporting structure.",
    ),
    IsolationCase(
        name="lam_org_excludes_rivian",
        question="Show me the Lam Research org chart.",
        must_include=("Tim Archer",),
        must_exclude=("RJ Scaringe", "Wassym Bensaid"),
        guards="The mirror case: a Lam answer must not pull in Rivian's executives.",
    ),
    IsolationCase(
        name="cisco_work_excludes_macys_metric",
        question="What kind of projects have we delivered for Cisco, including the outcomes?",
        must_include=("Cisco",),
        # "60% code reduction" belongs to the Macy's loyalty-platform modernization. It was
        # credited to Cisco's debug-tool engagement four times in a generated client deck.
        must_exclude=("60% code reduction",),
        guards="An outcome metric must stay attached to the engagement that produced it.",
    ),
    IsolationCase(
        name="client_count_excludes_internal_bench",
        question="How many clients does BayOne currently service?",
        must_include=(),
        # BayOne's own bench/overhead rows are headcount, not customers.
        must_exclude=("Bayone Bench", "Bayone Solutions"),
        guards="BayOne must never appear as one of its own clients.",
        # Naming the exclusion in prose ("internal bench rows excluded") is CORRECT behaviour,
        # so only the structured data is scanned: BayOne must not appear as a client ROW.
        exclude_scope="values",
        # The canonical count lives in SQL (V_CLIENT_PLACEMENTS), so this also pins the
        # non-determinism that once returned 58 on one run and 60 on the next.
        ground_truth_sql=(
            "SELECT COUNT(DISTINCT CLIENT_NAME) FROM V_CLIENT_PLACEMENTS "
            "WHERE STATUS = 'Active' AND NOT IS_INTERNAL"
        ),
    ),
]


def _haystack(payload: dict, scope: str = "all") -> str:
    """Everything the user could read, since a bled fact can hide in a table cell or a
    generated slide rather than in the summary. ``scope="values"`` restricts the search to the
    structured data, excluding the prose."""
    import json

    parts = [] if scope == "values" else [str(payload.get("answer") or "")]
    for key in ("values", "chart"):
        blob = payload.get(key)
        if blob:
            parts.append(json.dumps(blob, ensure_ascii=False, default=str))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def agent_and_conn():
    """A live agent plus a read connection for ground truth; skips without credentials."""
    from sf_agent.agent import SnowflakeAgent
    from sf_agent.config import AgentConfig, SnowflakeConfig
    from sf_agent.connection import SnowflakeConnection
    from sf_agent.tools.run_sql import RunSqlTool

    try:
        sf_config = SnowflakeConfig()  # type: ignore[call-arg]
        agent_config = AgentConfig()  # type: ignore[call-arg]
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"credentials unavailable: {e}")

    try:
        conn = SnowflakeConnection(sf_config).connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"cannot reach Snowflake: {e}")

    agent = SnowflakeAgent(tools=[RunSqlTool(conn)], config=agent_config)
    try:
        yield agent, conn
    finally:
        conn.close()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_entity_isolation(case: IsolationCase, agent_and_conn) -> None:
    agent, conn = agent_and_conn
    answer, _ = agent.route_and_answer([], case.question)
    payload = {"answer": answer.answer, "values": answer.values, "chart": answer.chart}
    text = _haystack(payload)
    probe = text.casefold() if case.case_insensitive else text

    def present(needle: str) -> bool:
        return (needle.casefold() if case.case_insensitive else needle) in probe

    missing = [n for n in case.must_include if not present(n)]
    assert not missing, (
        f"{case.name}: expected marker(s) absent — the agent did not surface this entity's "
        f"own data: {missing}\nGuards: {case.guards}\nAnswer: {answer.answer[:400]}"
    )

    excl_text = _haystack(payload, case.exclude_scope)
    excl_probe = excl_text.casefold() if case.case_insensitive else excl_text
    bled = [
        n for n in case.must_exclude
        if (n.casefold() if case.case_insensitive else n) in excl_probe
    ]
    assert not bled, (
        f"{case.name}: CROSS-ENTITY BLEED — {bled} belongs to a different entity but appeared "
        f"in this answer.\nGuards: {case.guards}\nAnswer: {answer.answer[:400]}"
    )

    if case.ground_truth_sql:
        expected = conn.execute(case.ground_truth_sql, max_rows=1).rows[0][0]
        # The headline value is the auditable figure; accept it in `values` too, since some
        # phrasings put the count there and lead with prose.
        found = str(expected) in f"{answer.value} {text}"
        assert found, (
            f"{case.name}: canonical figure {expected} (from SQL) not in the answer — "
            f"value={answer.value!r}. This is the determinism guard.\nAnswer: {answer.answer[:400]}"
        )
