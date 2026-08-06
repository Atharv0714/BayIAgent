# BayI Intelligence Agent — Azure runbook

Operating notes for the App Service deployment. Assumes `az` and a login with
Contributor on the `BayIAgent` resource group.

## What is deployed

| | |
|---|---|
| URL | https://bayiinternalagent-dxeba0ftgqgpb6ed.centralus-01.azurewebsites.net |
| Web app | `BayIInternalAgent` |
| Resource group | `BayIAgent` (Central US) |
| Plan | `ASP-BayIAgent-ba56`, P1v3 |
| Key Vault | `kv-bayi-agent` |
| Subscription | `068a6714-1eee-423b-b018-add1c766c033` |
| Tenant | `5c073500-0974-4b6d-8bbe-70ba8fc4c610` (bayone.com) |
| Runtime | Python 3.11, gunicorn + uvicorn worker, 1 worker |

The app, plan and resource group pre-existed and were reused; the plan was
already billing at P1v3 with nothing deployed on it. Nothing here resizes it.
`az appservice plan update --sku B1 -n ASP-BayIAgent-ba56 -g BayIAgent` drops it
to roughly $13/month if you want that — the app does not need P1v3.

## How a request flows

A browser hits the App Service front end. Easy Auth intercepts, requires an Entra
sign-in, and injects `X-MS-CLIENT-PRINCIPAL-NAME` (the caller's UPN) and
`X-MS-CLIENT-PRINCIPAL` (base64 JSON of every claim, including group object IDs).
The browser cannot forge either — the platform strips them from inbound requests.
`startup.sh` has already decoded the Snowflake private key out of Key Vault onto
`/home/site/rsa_key.p8`, so the app opens two Snowflake connections: a read
connection as `BAYI_READ` and a write connection as `BAYI_INGEST_WRITE`.

Two things about that worth remembering. The app holds **one** shared Snowflake
connection behind a module-level lock, which is why gunicorn runs a single
worker — each extra worker opens its own warehouse session and multiplies credit
burn without adding throughput. And `/home` is the persisted Azure Files mount,
so the materialized key and the `.ingest_drafts/` directory survive restarts;
`/tmp` does not.

## Per-request isolation and the shared session

The row-access policy filters private/protected rows using session variables
(`BAYI_CALLER`, `BAYI_PROTECTED`) that the app re-`SET`s per request on that one
shared connection. Snowflake's `GETVARIABLE` docs warn the function result-caches
within a session "including the body of policy objects", which raised the question:
does a re-`SET` for the next caller invalidate the cache, or does the policy keep
evaluating the previous caller and leak their private rows?

**Settled empirically on 2026-07-24: the cache invalidates on `SET` — the shared
session is safe.** `scripts/gov_session_probe.py` reproduces it on a scratch table
via the app's own `SnowflakeConnection`: bind Alice → sees only Alice's; re-bind Bob
on the *same* session → Alice's private row is gone. `tests/test_governance_isolation.py`
is the `integration`-marked regression guard. Re-run either if the connection model
or the policy body ever changes.

This is why no per-request/per-caller connection was introduced. It does **not**
unlock raising `--workers` above 1: that limit is about warehouse credit (N workers =
N sessions) and per-process in-memory state (`STATE.conversations` for follow-ups,
`STATE.pending_ingests` for drafts), neither of which the isolation result changes.
Leave it at 1 unless you add a shared store + sticky routing and review the credit cost.

## Secrets

Five secrets live in `kv-bayi-agent`, wired into app settings as Key Vault
references (`@Microsoft.KeyVault(SecretUri=...)`), so the values are never in
plaintext on the app:

`anthropic-api-key`, `snowflake-account`, `snowflake-user`, `snowflake-pat`,
`snowflake-private-key-pem-b64`.

The private key is stored base64-encoded because a multi-line PEM does not
round-trip through an app setting reliably; `startup.sh` decodes it back to a
file at container start. Everything else in app settings — warehouse, database,
schema, role names, model tuning, header names — is non-sensitive by design and
visible to anyone with Reader on the app.

The web app's managed identity holds **Key Vault Secrets User** on the vault:
read secret values, nothing else. It cannot write a secret or enumerate keys and
certificates. Operators who need to set secrets hold **Key Vault Secrets
Officer** separately; `load-secrets.sh` grants that to whoever runs it.

### Rotating a secret

```bash
# 1. Update the value (from a directory holding the new material)
az keyvault secret set --vault-name kv-bayi-agent --name anthropic-api-key --file ./newkey.txt

# 2. App settings hold a versionless SecretUri, so the app picks up the new
#    version on its next restart — Key Vault references are cached, not live.
az webapp restart -n BayIInternalAgent -g BayIAgent
```

To rotate the Snowflake key pair, re-run `deploy/load-secrets.sh` with the new
`.env` and `.p8`; it re-encodes, re-uploads, and verifies the stored copy decodes
byte-for-byte back to the source file before finishing.

## Routine operations

```bash
# Live log stream (this is the first thing to reach for on any startup problem)
az webapp log tail -n BayIInternalAgent -g BayIAgent

# Redeploy code only — skips all infrastructure steps
./deploy/deploy.sh --code-only

# Full re-run: idempotent, repairs drift in settings, identity, RBAC, platform config
./deploy/deploy.sh

# Inspect settings (values for Key Vault refs show as the reference, not the secret)
az webapp config appsettings list -n BayIInternalAgent -g BayIAgent -o table

# Confirm every Key Vault reference actually resolved
az webapp config appsettings list -n BayIInternalAgent -g BayIAgent \
    --query "[?contains(value,'KeyVault')].name" -o tsv
```

## Troubleshooting

**App returns 503 or hangs after deploy.** Read the log stream first. The most
common cause is `startup.sh` exiting: it deliberately fails loudly and early
rather than starting gunicorn with a broken key, and prints the reason with a
`[startup]` prefix.

**`SNOWFLAKE_PRIVATE_KEY_PEM_B64 is empty` in the logs.** The Key Vault reference
did not resolve. Almost always the managed identity's role assignment has not
propagated, or was lost. Re-run `deploy.sh` (it re-grants and waits), or check:

```bash
az role assignment list --assignee "$(az webapp identity show -n BayIInternalAgent -g BayIAgent --query principalId -o tsv)" \
    --scope "$(az keyvault show -n kv-bayi-agent --query id -o tsv)" -o table
```

**`JWT_TOKEN_INVALID` from Snowflake.** The private key in the vault does not
match the public key registered on the `ASHARMA3` Snowflake user, or a passphrase
is set against an unencrypted key. `load-secrets.sh` guards both directions, so
this points at the key registered in Snowflake having changed. Verify with
`DESC USER ASHARMA3` and compare `RSA_PUBLIC_KEY_FP`.

**Snowflake `Insufficient privileges` on a query.** Expected if `deploy/snowflake-roles.sql`
has not been run — the app now connects as `BAYI_READ`, not `ACCOUNTADMIN`. Run
it, or temporarily set `SNOWFLAKE_ROLE` back and understand you are handing
model-generated SQL an admin role.

**Cortex route fails while plain SQL works.** `BAYI_READ` is missing SELECT on
the semantic view, or the `SNOWFLAKE.CORTEX_USER` database role. Both are in
`snowflake-roles.sql` under "CORRECTION 1".

**Ingest commit fails on `ALTER TABLE`.** `BAYI_INGEST_WRITE` does not own
`blocks`/`facts`. Snowflake has no grantable ALTER privilege — altering requires
OWNERSHIP. See "CORRECTION 2" in `snowflake-roles.sql`.

**Sign-in loops or 401s everything.** Check Easy Auth's unauthenticated action
and that the app registration's redirect URI is exactly
`https://bayiinternalagent-dxeba0ftgqgpb6ed.centralus-01.azurewebsites.net/.auth/login/aad/callback`.

## Group claims — read this before enabling the protected tier

App Service Easy Auth has **no `X-MS-CLIENT-GROUPS` header**. That header does
not exist in the product. Easy Auth injects `X-MS-CLIENT-PRINCIPAL`,
`-PRINCIPAL-ID`, `-PRINCIPAL-NAME` and `-PRINCIPAL-IDP`, and group membership
arrives only as `groups` claims inside the base64 JSON of `X-MS-CLIENT-PRINCIPAL`.
So `GROUPS_HEADER` is set to `X-MS-CLIENT-PRINCIPAL` and `sf_agent.web._principal_groups`
decodes it. A different front end that maps the claim onto a plain
comma-separated header still works — the code selects the format by header name.

Two consequences that bite:

Entra emits group **object IDs**, not display names. `PROTECTED_GROUP` must hold
a GUID; the literal string `SG-BayI-Sensitive` would never match a claim. That
group does not currently exist in the tenant, so `PROTECTED_GROUP` is set to an
all-zeros GUID — no caller ever matches through the group path. That is
fail-closed and deliberate.

**`PROTECTED_USERS` is the mechanism actually in use.** Standing up the group needs
a Groups Administrator to create it *and* the app registration configured to emit
the groups claim. `PROTECTED_USERS` is a comma-separated allowlist of UPNs, OR'd
with the group check in `web._caller_is_protected`, and needs only the identity
sign-in already delivers. Use the exact string `/api/whoami` reports as `identity`
while signed in — that is `X-MS-CLIENT-PRINCIPAL-NAME`, which is not always the
mail address you would guess. The comparison lower-cases both sides, because Entra
does not guarantee UPN casing between tokens.

Moving to a real Entra group later is a config change, not a code change: create
the group, put its object ID in `PROTECTED_GROUP_OBJECT_ID`, re-run `deploy.sh`.
Both paths keep working, so it can be done without a cutover.

Past roughly 200 group memberships Entra drops the groups claim entirely and
substitutes `_claim_names`/`_claim_sources` pointing at Microsoft Graph. The
decoder treats that as *membership unknown* and returns no groups, logging a
warning — an over-grouped admin is denied rather than silently granted. Resolving
overage properly needs a Graph call, which this app does not make. If you have
users in that many groups, either use a Graph lookup or scope the groups claim to
assigned groups only in the app registration's Token configuration.

## Governance model — applied 2026-07-24

The Snowflake side is now provisioned (this account: `BAYONE_INTERNALINFO.PUBLIC`):

1. `deploy/snowflake-roles.sql` — `BAYI_READ` / `BAYI_ADMIN_READ` / `BAYI_INGEST_WRITE`
   created; `blocks`/`facts` **ownership** moved to `BAYI_INGEST_WRITE`.
2. `deploy/snowflake-governance-test.sql` (or `scripts/gov_session_probe.py`) — passed.
3. `deploy/snowflake-governance.sql` — sensitivity columns backfilled and the
   `rap_ownership` row-access policy attached to `blocks` and `facts`.

Two corrections in `snowflake-governance.sql` that are easy to miss when re-running
this elsewhere (both are documented in that file):

- **The backfill is not optional.** Pre-tiering rows had `sensitivity = NULL` (the
  app's `_ADDITIVE_COLUMNS` path adds the column without a `DEFAULT`), and the policy
  hides `NULL` rows — attaching without backfilling silently hides every old row.
  Measured here: 35/115 blocks and 58/74 facts were `NULL`; they are now `internal`.
  The backfill must run with the policy **detached** (the owner role can't see `NULL`
  rows through the policy), so the script order is detach → backfill → attach.
- **`ACCOUNTADMIN` attaches via `APPLY ROW ACCESS POLICY`, not ownership.** Since
  `blocks`/`facts` are owned by `BAYI_INGEST_WRITE`, the script grants
  `APPLY ROW ACCESS POLICY ON ACCOUNT` to `ACCOUNTADMIN` so it can set/unset the policy
  on tables it no longer owns.

### Enabling ownership enforcement

`ENFORCE_OWNERSHIP` is now `true` in `deploy/config.sh`, so `deploy.sh` ships it. Set it
there, **not** by `az webapp config appsettings set` alone: deploy.sh writes this value on
every run, so a CLI-only flip is silently reverted by the next deploy.

Before flipping it, prove an identity actually arrives. Once enforcement is on, `/api/ask`
returns **401 to everyone** if none does. Deploy with it still `false`, sign in, and open
`/api/whoami`:

```json
{"ok":true,"enforce":false,"identity":"you@bayone.com","is_protected_member":false,...}
```

`identity: null` means Easy Auth is enforcing sign-in but not injecting
`X-MS-CLIENT-PRINCIPAL-NAME` — stop and fix that first. A real string is also the value to
copy verbatim into `PROTECTED_USERS`.

To turn it off in a hurry (the app keeps working; only per-user scoping stops):

```bash
az webapp config appsettings set -n BayIInternalAgent -g BayIAgent \
    --settings ENFORCE_OWNERSHIP=false
az webapp restart -n BayIInternalAgent -g BayIAgent
```

Set it back in `config.sh` too, or the next deploy re-enables it.

### Verifying isolation

`scripts/gov_e2e_check.py` is the check (safe to run against production — it inserts one
labelled private row, proves Alice sees it while Bob and an unbound caller do not, then
deletes it):

```bash
.venv/bin/python scripts/gov_e2e_check.py   # exit 0 = isolated
```

### Rolling back

Enforcement and the policy come off independently, both reversible:

```bash
# 1. Stop binding identities (app behaves as before the feature)
az webapp config appsettings set -n BayIInternalAgent -g BayIAgent \
    --settings ENFORCE_OWNERSHIP=false
az webapp restart -n BayIInternalAgent -g BayIAgent
```
```sql
-- 2. Detach the policy entirely (rows all become visible again to BAYI_READ).
--    As ACCOUNTADMIN (holds APPLY ROW ACCESS POLICY):
ALTER TABLE blocks DROP ROW ACCESS POLICY rap_ownership;
ALTER TABLE facts  DROP ROW ACCESS POLICY rap_ownership;
```

## Decisions left for a human

- **`ACCOUNTADMIN` break-glass bypass.** `rap_ownership` ends with
  `current_role() IN ('BAYI_ADMIN_READ', 'ACCOUNTADMIN')` — a permanent, unlogged read
  of every private/protected row by anyone who can `USE ROLE ACCOUNTADMIN`. It is
  tolerable only because the app connects as `BAYI_READ`. **Proposed:** grant
  `BAYI_ADMIN_READ` to a *named* human for break-glass (audited via `query_history`),
  then drop `ACCOUNTADMIN` from the policy's admin branch. Not done unilaterally — it
  removes the only admin path back into private data if `BAYI_ADMIN_READ` is misplaced.
- **Protected tier / `SG-BayI-Sensitive`.** Still does not exist in the tenant;
  `PROTECTED_GROUP` is an all-zeros GUID (fail-closed). To enable: a Groups Administrator
  creates the Entra security group, then set `PROTECTED_GROUP` to its **object-ID GUID**
  (not the display name) and confirm the groups claim is emitted (see "Group claims").
- **Worker count.** Stays at 1 (see "Per-request isolation"); raising it is a separate
  credit + shared-state decision.

## Outstanding

- The protected tier is inert until `SG-BayI-Sensitive` exists and `PROTECTED_GROUP`
  holds its GUID (above).
- The Snowflake service user is `ASHARMA3`, a named human account, not a service
  account. Key-pair auth on a personal identity means offboarding that user breaks
  the app.
- The plan is P1v3 for a workload that fits B1.
