# Snowflake migration — per-owner private-data model

These scripts provision the Snowflake side of the private-data feature described in
`docs/security-model.html`. They are **not applied automatically** — run them yourself
as `ACCOUNTADMIN` when you're ready to move off the current single-`ACCOUNTADMIN` posture.

## Run order (as ACCOUNTADMIN)

1. `01_roles.sql` — creates `BAYI_READ`, `BAYI_ADMIN_READ`, `BAYI_INGEST_WRITE` and grants.
2. `02_columns.sql` — adds `sensitivity` (default `internal`) + `ingested_by` to `blocks`/`facts`.
3. `03_row_access_policy.sql` — creates and attaches `rap_ownership`.

Before running, replace the `<<PLACEHOLDER>>` tokens (`<<WAREHOUSE>>`, `<<DATABASE>>`,
`<<SCHEMA>>`, `<<SERVICE_USER>>`) with your real object names — the same values that are
in your `.env` (`SNOWFLAKE_INGEST_DATABASE`, `SNOWFLAKE_INGEST_SCHEMA`, etc.).

> First run of `03`: the two `DROP ROW ACCESS POLICY` lines error because nothing is
> attached yet. That's expected — skip them the first time (or run in continue-on-error
> mode). Every later run is clean.

## After the scripts succeed — flip the app on

Update `.env`:

```
SNOWFLAKE_ROLE=BAYI_READ            # query path moves off ACCOUNTADMIN
SNOWFLAKE_INGEST_ROLE=BAYI_INGEST_WRITE
ENFORCE_OWNERSHIP=true              # app binds caller identity per request
CALLER_SESSION_VAR=BAYI_CALLER      # must match the policy body in 03
```

When `ENFORCE_OWNERSHIP=true`, `/api/ask` requires a caller identity (the Azure Easy
Auth header `X-MS-CLIENT-PRINCIPAL-NAME`, or `DEV_CALLER_IDENTITY` locally) and returns
401 without one. With it `false` (the default), the app behaves exactly as before.

## Verifying isolation

1. Ingest a document with the **Private** toggle as `alice@bayone.com`.
2. Ask a question that would surface that content as `bob@bayone.com`
   (set `DEV_CALLER_IDENTITY=bob@bayone.com`) → the private rows are **not** returned.
3. Re-ask as `alice@bayone.com` → the rows **are** returned.
4. A break-glass holder using `BAYI_ADMIN_READ` sees all rows (log this use).
