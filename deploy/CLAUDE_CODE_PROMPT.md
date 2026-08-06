# Prompt for Claude Code

Run this from `~/code/BayIAgent` on the branch `azure-deploy`. Paste everything
below the line.

---

You are working in `~/code/BayIAgent`, a FastAPI app ("BayI Intelligence Agent")
that answers natural-language questions against a Snowflake warehouse and
generates documents. Branch `azure-deploy`. Python 3.11, managed with `uv`; the
runtime deps are in the `web` and `docs` optional groups. Run tests with
`.venv/bin/python -m pytest`, or `uv run pytest`.

The app is being deployed to Azure App Service behind Entra ID. The deployment
scripts already exist and work — do not redo them. Your job is the **governance
model**: per-owner private data and a group-scoped protected tier, enforced by a
Snowflake row-access policy. It is currently not deployable, for a specific
reason set out below.

## Read these first

- `docs/security-model.html` — the intended model
- `docs/sql/README.md`, `docs/sql/01_roles.sql`, `02_columns.sql`, `03_row_access_policy.sql`
- `deploy/snowflake-roles.sql` — filled-in, corrected version of 01, already run
- `deploy/snowflake-governance.sql` — filled-in, corrected 02+03, **not yet run**
- `deploy/snowflake-governance-test.sql` — the deciding test described below
- `deploy/RUNBOOK.md`
- `src/sf_agent/connection.py` — `SnowflakeConnection`, `bind_session`
- `src/sf_agent/web.py` — `_caller_identity`, `_caller_groups`, `_principal_groups`,
  `_caller_is_protected`, `/api/ask` (the `_LOCK` block), `_DRAFTS_DIR`
- `src/sf_agent/ingest_store.py` — `ensure_tables`, `read_registry`, `BLOCK_COLS`, `FACT_COLS`

## The blocking problem

`03_row_access_policy.sql` filters rows with `getvariable('BAYI_CALLER')`. The app
sets that variable per request via `SnowflakeConnection.bind_session`.

Snowflake's GETVARIABLE documentation states:

> This function uses the result cache for the current session if you call the
> function more than once in the same session. The result cache applies wherever
> you call this function, including the body of policy objects, such as a row
> access policy.

The app holds **one** Snowflake connection — one session — shared by every user
and serialized by a module-level `_LOCK`, re-running `SET BAYI_CALLER` for each
request. That is precisely "call the function more than once in the same
session". If the cache does not invalidate on `SET`, the policy evaluates a stale
caller identity and returns one user's private rows to another user. It fails
silently: queries succeed and look correct.

This must be settled empirically before the policy goes anywhere near `blocks`
and `facts`.

## Task 1 — settle it

Write `scripts/gov_session_probe.py`. It must use the app's own
`SnowflakeConnection` class (not a raw connector call) so the test reflects real
behaviour, and it must do everything **on one connection**:

1. Connect as `BAYI_READ` using the existing `.env` (key-pair; the key is
   unencrypted despite `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` containing a single
   space, which pydantic strips to `None`).
2. Create a scratch table `gov_probe(id, label, sensitivity, ingested_by)` — you
   will need ACCOUNTADMIN for the DDL and the policy; use a second connection for
   setup, then do all *reads* on the single `BAYI_READ` connection.
3. Seed: one `internal` row, one `private` row owned by `alice@bayone.com`, one
   `private` row owned by `bob@bayone.com`, one `protected` row.
4. Attach a policy with the same body as `03_row_access_policy.sql`.
5. On the one `BAYI_READ` connection, in order:
   `bind_session(BAYI_CALLER='alice@…')` → select → assert `{shared, alice}`;
   `bind_session(BAYI_CALLER='bob@…')` → select → assert `{shared, bob}`;
   toggle `BAYI_PROTECTED` true then false → assert the protected row appears then
   disappears; `UNSET BAYI_CALLER` → assert `{shared}` only.
6. Tear the scratch objects down in a `finally`.
7. Exit non-zero with a clear message on any assertion failure.

Report the result explicitly before continuing. **Step 5's second assertion is
the whole question.**

## Task 2 — if the probe shows a leak, fix the session model

Only do this if the probe fails. If it passes, say so and skip to Task 3.

The root cause is one Snowflake session shared across callers. Fix it so a
caller's identity can never be observed by another caller's query.

Requirements:

- Each `/api/ask` request must execute against a session whose `BAYI_CALLER` and
  `BAYI_PROTECTED` were set for **that caller**. A per-request connection is the
  obvious approach; a per-caller cached connection with an idle TTL is acceptable
  if you can show it is safe. Do not keep one global session.
- Preserve the existing reconnect-on-expired-session behaviour
  (`_EXPIRED_SESSION_ERRNOS`) and `client_session_keep_alive`.
- Preserve `bind_session`'s re-application of variables after a reconnect.
- The write/ingest connection has the same structural issue but a smaller blast
  radius (the ingest role has no SELECT and `ingested_by` is stamped from
  server-side metadata). Handle it consistently; say what you decided and why.
- Measure and report the added latency per request. Snowflake connect is
  typically 0.5–2s; if that is unacceptable, propose the caching design rather
  than silently accepting it.
- Note in your summary whether this removes the "gunicorn `--workers 1`"
  constraint. That limit exists only because of the single shared connection
  (see `startup.sh` and `deploy/config.sh`). If per-request sessions make it
  safe to raise, say so — but do **not** change the worker count yourself; it
  has warehouse-credit implications the owner should approve.

Add tests that would fail against the current design: at minimum a concurrent
test with two identities interleaved, asserting neither sees the other's private
rows. Mark anything hitting a live warehouse with the existing `integration`
marker so it skips without credentials.

## Task 3 — close the remaining gaps

**`read_registry` runs unbound.** `web.py` calls
`read_registry(STATE.connection)` on the read connection without binding an
identity, so once the policy is live it inherits whatever `BAYI_CALLER` the
previous `/api/ask` left set. It exposes `entity_type`/`attribute` *values*, not
row content, but it is the same root cause. Make it explicitly identity-scoped or
explicitly unbound-and-internal-only; either is fine, decide and document it.

**The ACCOUNTADMIN bypass.** The policy ends with
`current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN')` — a permanent, unlogged
bypass of every private and protected row. Now that the app connects as
`BAYI_READ`, propose whether to drop `ACCOUNTADMIN` from that list, and what
break-glass procedure replaces it. Do not change it unilaterally.

**Protected group.** `PROTECTED_GROUP` in `deploy/config.sh` is
`00000000-0000-0000-0000-000000000000`, a deliberate fail-closed placeholder:
`SG-BayI-Sensitive` does not exist in the bayone.com tenant. Entra emits group
**object IDs**, not display names, so the setting must hold a GUID. Document
exactly what has to happen to switch the protected tier on — creating the group
needs Groups Administrator. Do not create it.

**Groups overage.** `_principal_groups` in `web.py` fails closed when Entra drops
the groups claim past ~200 memberships (`_claim_names`/`_claim_sources`/
`hasgroups`), logging a warning and returning no groups. Confirm that is still
correct after your changes and that the tests still cover it.

## Task 4 — make it deployment-ready

- Verify `deploy/snowflake-governance.sql` is correct against the final code,
  especially that the columns it adds match `BLOCK_COLS`/`FACT_COLS` and
  `_ADDITIVE_COLUMNS` in `ingest_store.py`.
- Run it against Snowflake, then re-run the probe from Task 1 against the **real**
  `blocks`/`facts` tables (read-only; do not insert into them) to confirm the
  policy filters as intended.
- End-to-end check with `ENFORCE_OWNERSHIP=true` locally, using
  `DEV_CALLER_IDENTITY` / `DEV_CALLER_GROUPS` to simulate two users: ingest a
  private document as user A, confirm user B's question does not surface it,
  confirm user A's does. This is the verification `docs/sql/README.md` describes.
- Update `deploy/RUNBOOK.md`: the enabling procedure, the new session model, how
  to verify isolation, and how to roll back (detach the policy, set
  `ENFORCE_OWNERSHIP=false`).
- Update `deploy/config.sh` only if a setting genuinely changed.
- Run the full test suite. Everything must pass.

## Constraints

- **Never commit `.env`, `rsa_key.p8`, or `rsa_key.pub`.** They are gitignored;
  keep it that way. No secret in any committed file, test fixture, or log line.
- Do not weaken any default. `ENFORCE_OWNERSHIP` stays `false` in committed
  config; it is turned on per-environment after verification.
- Do not change auth behaviour beyond what is specified above without proposing
  it first.
- `requirements.txt` is generated —
  `uv export --extra web --extra docs --no-dev --no-hashes --no-emit-project`.
  If you touch dependencies, regenerate it and re-lock.
- Keep changes reviewable: small commits, clear messages explaining *why*.
- If the probe in Task 1 passes and no session redesign is needed, say so plainly
  rather than refactoring for its own sake.

## Definition of done

1. A reproducible probe proving whether session-variable filtering is safe on
   this app's connection model, with its result stated.
2. If unsafe: a session model where one caller's identity cannot affect another
   caller's query, with a test that fails against the old design.
3. `read_registry` no longer inherits a stale caller identity.
4. The governance SQL applied, and the policy verified to filter `blocks` and
   `facts` correctly for a bound identity, a different identity, and no identity.
5. Local end-to-end proof of private-row isolation between two identities.
6. Runbook updated, full suite green, nothing sensitive committed.
7. A short written summary of what you changed, what you decided and why, the
   measured latency cost, and anything you deliberately left for a human —
   specifically the ACCOUNTADMIN bypass, the protected group, and the worker count.
