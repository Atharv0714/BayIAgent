# BayOne Snowflake Query Agent

A Python agent that answers natural-language questions against the BayOne Snowflake
warehouse, with numbers **grounded in query results** rather than model recall.

This is a self-contained sub-project, isolated from the Node backend in the repo root.
It does **not** implement custom text-to-SQL — Snowflake Cortex Analyst will own NL→SQL
later. The agent loop (chunk 2) runs over a clean tool abstraction so the `run_sql` tool
can be swapped for a Cortex Analyst tool without touching the loop.

## Status

**Chunk 1:** typed config, key-pair connection, a single `run_sql` tool, integration
tests against `BILLABLE_DATA_BY_JOB_DESCRIPTION`.

**Chunk 2 (this commit):** Anthropic tool-use agent loop over the chunk-1 `Tool`
protocol (`run_sql` is the only registered tool; the cortex_analyst seam is untouched),
a grounding system prompt, a structured auditable `AgentAnswer`, and an eval harness
that asserts the agent's value against live ground truth. Offline loop tests pass with
no creds; the live eval is the done-condition (see below).

## Layout

```
src/sf_agent/
  config.py        SnowflakeConfig + AgentConfig — typed, env-loaded
  connection.py    key-pair connection lifecycle + query execution → QueryResult
  sql_guard.py     read-only guard for untrusted (model-generated) SQL
  prompts.py       grounding system prompt for the agent
  types.py         QueryResult / ToolError / ToolResult / AgentAnswer (Pydantic v2)
  agent.py         SnowflakeAgent — Anthropic tool-use loop
  tools/
    base.py        Tool protocol the agent loop depends on
    run_sql.py     RunSqlTool — wraps the connection, captures SQL + elapsed_ms
tests/
  test_sql_guard.py    unit, no creds
  test_agent_loop.py   unit, no creds (stub Anthropic client + stub tool)
  test_connection.py   integration
  test_run_sql.py      integration
  eval_fixtures.py     ground-truth eval cases (CONFIRM column constants)
  test_evals.py        integration eval harness (the done-condition)
  test_schema.py       integration helper: prints the table's columns
```

## Running the agent eval (done-condition)

Needs Snowflake creds AND `ANTHROPIC_API_KEY` in `.env`. The eval computes each expected
value live from `ground_truth_sql`, runs the agent, and asserts the value matches.

```bash
# 1. confirm the real column names, then edit COL_AMOUNT / COL_CATEGORY in eval_fixtures.py
uv run pytest -k print_schema -s -m integration
# 2. run the eval
uv run pytest -m integration -k evals -v
```

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # fill in account, user, key path, warehouse/db/schema/role
```

## Auth: key-pair only

Single-factor password auth is being deprecated by Snowflake (final enforcement
expected Oct 2026); key-pair is the recommended method for service accounts. Generate
a key, register the public half on the Snowflake user, and point
`SNOWFLAKE_PRIVATE_KEY_PATH` at the PEM private key.

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8       # encrypted
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
# in Snowflake:  ALTER USER <user> SET RSA_PUBLIC_KEY='<contents of rsa_key.pub>';
```

## Security boundary

The **primary** safety boundary is the Snowflake role: the agent must connect as a
dedicated **read-only** user/role with `SELECT` only and no write/DDL/DML grants.
The in-process `sql_guard` (SELECT-only, single-statement, no DML even inside CTEs or
comments) is a secondary defense, not the boundary — never rely on it alone.

## Tests

```bash
uv run pytest                      # all
uv run pytest -m "not integration" # guard unit tests only (no creds needed)
uv run pytest -m integration       # requires a populated .env
```

Integration tests skip automatically when Snowflake creds are absent.
