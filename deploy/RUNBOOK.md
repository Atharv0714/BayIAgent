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
all-zeros GUID — no caller ever matches, and protected rows stay unreadable.
That is fail-closed and deliberate.

Past roughly 200 group memberships Entra drops the groups claim entirely and
substitutes `_claim_names`/`_claim_sources` pointing at Microsoft Graph. The
decoder treats that as *membership unknown* and returns no groups, logging a
warning — an over-grouped admin is denied rather than silently granted. Resolving
overage properly needs a Graph call, which this app does not make. If you have
users in that many groups, either use a Graph lookup or scope the groups claim to
assigned groups only in the app registration's Token configuration.

## Enabling ownership enforcement

`ENFORCE_OWNERSHIP` is `false`. Do not flip it until **both** are true:

1. Entra sign-in is verified end to end — you can see a real UPN reaching the app.
2. `docs/sql/02_columns.sql` and `docs/sql/03_row_access_policy.sql` have been
   applied.

Without (2) the app sets the `BAYI_CALLER` session variable and no row-access
policy reads it. Queries succeed, `/api/ask` starts requiring an identity, and it
looks like per-owner isolation is working — but every row is still visible to
everyone. That failure is silent and convincing, which is what makes it worth
this warning.

```bash
az webapp config appsettings set -n BayIInternalAgent -g BayIAgent \
    --settings ENFORCE_OWNERSHIP=true
az webapp restart -n BayIInternalAgent -g BayIAgent
```

Then verify isolation the way `docs/sql/README.md` describes: ingest a private
document as one user, confirm a second user's question does not surface it, and
confirm the first user's does.

## Outstanding

- `docs/sql/02_columns.sql` and `03_row_access_policy.sql` are not applied, so the
  private and protected tiers are inert.
- `SG-BayI-Sensitive` does not exist in the tenant.
- The Snowflake service user is `ASHARMA3`, a named human account, not a service
  account. Key-pair auth on a personal identity means offboarding that user breaks
  the app.
- The plan is P1v3 for a workload that fits B1.
