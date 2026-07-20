"""Generate docs/BayI-SOW.docx — the Statement of Work for the BayI Agent platform.

An internal BayOne SOW covering the platform end-to-end: the delivered query agent +
ingest pipeline + Cortex path, and the planned hosted-deployment / security hardening.
Timeline and pricing are left as [TBD] placeholders. Styling mirrors
docs/build_security_docx.py for visual consistency.

Run:  uv run --with python-docx python docs/build_sow_docx.py
"""
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ── Brand palette (shared with security-model.docx) ─────────────────
ACCENT   = RGBColor(0x7C, 0x3A, 0xED)  # purple
DEEP     = RGBColor(0xC2, 0x1E, 0x73)  # magenta/deep
OK       = RGBColor(0x1A, 0x7F, 0x52)  # green
DANGER   = RGBColor(0xE5, 0x48, 0x4D)  # red
WARN     = RGBColor(0x8A, 0x61, 0x00)
MUTED    = RGBColor(0x6B, 0x64, 0x80)
TEXT     = RGBColor(0x1E, 0x16, 0x30)
TBDCOL   = RGBColor(0x8A, 0x61, 0x00)
PANEL2   = "F4F0FB"
HEADERBG = "EDE4FB"

doc = Document()

normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(10.5)
normal.font.color.rgb = TEXT


def shade(cell, hex_color):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def set_cell_text(cell, text, bold=False, color=None, size=10):
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color


def heading(num, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    r = p.add_run(f"{num}   {text}")
    r.bold = True
    r.font.size = Pt(15)
    r.font.color.rgb = ACCENT
    return p


def subhead(text):
    p = doc.add_paragraph()
    r = p.add_run(text.upper())
    r.bold = True
    r.font.size = Pt(10.5)
    r.font.color.rgb = TEXT
    return p


def lede(text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.color.rgb = MUTED
    r.font.size = Pt(10)
    return p


def body(text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.size = Pt(10.5)
    return p


def bullets(items, sub_color=MUTED):
    """items: list of str, or (lead, detail) tuples."""
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        if isinstance(it, tuple):
            lead, detail = it
            rl = p.add_run(lead)
            rl.bold = True
            rl.font.size = Pt(10.5)
            if detail:
                rd = p.add_run(f" — {detail}")
                rd.font.size = Pt(10.5)
                rd.font.color.rgb = sub_color
        else:
            r = p.add_run(it)
            r.font.size = Pt(10.5)


def note(text, kind="info"):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.15)
    bar = {"info": ACCENT, "ok": OK, "warn": WARN}[kind]
    r = p.add_run("▍ ")
    r.font.color.rgb = bar
    r.bold = True
    r2 = p.add_run(text)
    r2.font.size = Pt(10)
    return p


def styled_table(headers, rows, header_fill=HEADERBG, col_widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = t.rows[0].cells
    for j, h in enumerate(headers):
        set_cell_text(hdr[j], h, bold=True, size=10)
        shade(hdr[j], header_fill)
    for row in rows:
        cells = t.add_row().cells
        for j, val in enumerate(row):
            if isinstance(val, tuple):
                txt, col = val
                set_cell_text(cells[j], txt, bold=(col is not None), color=col, size=10)
            else:
                set_cell_text(cells[j], val, size=10)
    if col_widths:
        for row in t.rows:
            for j, w in enumerate(col_widths):
                row.cells[j].width = Inches(w)
    return t


# status pill helpers for table cells
DELIVERED = ("Delivered", OK)
PLANNED = ("Planned", ACCENT)
PARTIAL = ("Partial", WARN)
TBD = ("[TBD]", TBDCOL)


# ── Title block ─────────────────────────────────────────────────────
title = doc.add_paragraph()
tr = title.add_run("BayI")
tr.bold = True
tr.font.size = Pt(26)
tr.font.color.rgb = DEEP
tr2 = title.add_run("  Agent — Statement of Work")
tr2.bold = True
tr2.font.size = Pt(22)
tr2.font.color.rgb = TEXT

sub = doc.add_paragraph()
sr = sub.add_run(
    "Internal Statement of Work for the BayI Agent platform — a grounded natural-language "
    "query and document-ingest system over the BayOne Snowflake warehouse, and its hosted, "
    "access-controlled rollout to BayOne staff."
)
sr.font.color.rgb = MUTED
sr.font.size = Pt(10.5)

status = doc.add_paragraph()
st = status.add_run("DRAFT FOR REVIEW · v0.1")
st.bold = True
st.font.size = Pt(9)
st.font.color.rgb = WARN

doc.add_paragraph()

# ── Document control ────────────────────────────────────────────────
subhead("Document control")
styled_table(
    ["Field", "Value"],
    [
        ["Project", "BayI Agent — grounded Snowflake query & ingest platform"],
        ["Organization", "BayOne Solutions (internal project)"],
        ["Delivery owner", "Atharv Sharma"],
        ["Document type", "Statement of Work (internal)"],
        ["Version", "0.1 (Draft for review)"],
        ["Effective date", TBD],
        ["Status", ("Draft — pending sign-off", WARN)],
    ],
    col_widths=[1.9, 4.7],
)
note(
    "This is an internal scope-of-record. Timeline dates and pricing are intentionally left "
    "as [TBD] placeholders to be completed at sign-off. The companion security detail lives in "
    "docs/security-model.docx."
)

# ── 1 · Background & purpose ────────────────────────────────────────
heading("1", "Background & purpose")
lede(
    "BayI Agent answers questions about BayOne's data by querying Snowflake and responding from "
    "the actual query results — never from model recall. It supports pre-call intelligence and "
    "internal analysis where every figure must be traceable to the warehouse."
)
bullets([
    ("Grounded answers", "the agent writes SELECT-only SQL, runs it, and answers strictly from "
     "the returned rows, with the executed SQL captured for audit."),
    ("Knowledge ingest", "authorized users upload documents; Claude structures them into two "
     "fixed Snowflake tables (blocks + facts) that become queryable knowledge."),
    ("Three data classes", "ingested content is classified internal (shared, the default), "
     "private (readable only by the ingester), or protected (readable only by one privileged "
     "Entra group); a logged break-glass admin can read all of it."),
])

# ── 2 · Objectives & success criteria ───────────────────────────────
heading("2", "Objectives & success criteria")
bullets([
    ("Accurate, grounded responses", "natural-language questions resolve to correct answers "
     "backed by live Snowflake queries (validated against ground-truth evals)."),
    ("Safe ingest round-trip", "uploaded documents structure, preview, and load into the "
     "knowledge tables without model-authored write SQL."),
    ("Least-privilege data access", "the app connects through purpose-scoped Snowflake roles, "
     "not ACCOUNTADMIN."),
    ("Owner- and group-scoped privacy", "private data is physically unreadable to anyone but its "
     "owner, and protected data only to the privileged group — both enforced in the database."),
    ("Single secured URL", "BayOne staff reach the agent at one authenticated HTTPS endpoint."),
])

# ── 3 · Scope of work ───────────────────────────────────────────────
heading("3", "Scope of work")
subhead("In scope")
bullets([
    "Natural-language query agent (Anthropic tool-use loop) with question routing.",
    "Read-only SQL execution tool with an in-process safety guard.",
    "Optional Snowflake Cortex Analyst path (semantic-view NL→SQL).",
    "Document-ingest pipeline: structure → preview → commit, with ingest history.",
    "Web UI for query and ingest.",
    "Hosted deployment on Azure App Service with Microsoft Entra authentication.",
    "Three-tier data-privacy security model (least-privilege roles + owner- and group-based "
    "row-access policy).",
])
subhead("Out of scope")
bullets([
    "A custom / fine-tuned text-to-SQL model (Cortex Analyst owns NL→SQL where used).",
    "Access for non-BayOne tenants or external/anonymous users.",
    "Data migration or ETL beyond the named knowledge-base tables.",
    "BI dashboards or reporting front-ends outside the agent UI.",
])

# ── 4 · Deliverables ────────────────────────────────────────────────
heading("4", "Deliverables")
lede("Status reflects the codebase today: the core platform is built; the hosted, secured "
     "rollout is designed and paused, pending go-ahead.")
styled_table(
    ["Deliverable", "Key modules", "Status"],
    [
        ["Query agent loop + question routing", "agent.py, router.py, prompts.py", DELIVERED],
        ["Read-only SQL tool + guard", "tools/run_sql.py, sql_guard.py", DELIVERED],
        ["Cortex Analyst tool (optional)", "tools/cortex_analyst.py", DELIVERED],
        ["Grounded structured answers", "types.py, prompts.py", DELIVERED],
        ["Snowflake connection + typed config", "connection.py, config.py", DELIVERED],
        ["Ingest pipeline + history store", "ingest.py, ingest_store.py, ingest_drafts.py", DELIVERED],
        ["Web application + UI", "web.py, static/index.html, static/ingest.html", DELIVERED],
        ["Automated test suite (integration-gated)", "tests/ (17 files)", DELIVERED],
        ["Azure hosting + Entra Easy Auth", "deployment config", PLANNED],
        ["Least-privilege Snowflake roles + SENSITIVITY & INGESTED_BY columns + owner/group row policy",
         "Snowflake DDL", PLANNED],
        ["Three-tier sensitivity toggle (internal / private / protected) + ingest-only enforcement",
         "web.py, static/ingest.html", DELIVERED],
        ["Per-request session-identity + group binding at query time", "web.py, connection.py", DELIVERED],
    ],
    col_widths=[2.7, 2.6, 1.0],
)
note("The sensitivity toggle, the protected-group gating (Entra group claim), and the per-request "
     "identity/group binding are built and tested behind the ENFORCE_OWNERSHIP flag; what remains "
     "for Phase 4 is applying the Snowflake DDL and turning the flag on in the hosted environment.")

# ── 5 · Phased work breakdown ───────────────────────────────────────
heading("5", "Phased work breakdown")
styled_table(
    ["Phase", "Work", "Duration", "Milestone"],
    [
        ["1", "Query agent core — connection, run_sql tool, agent loop, grounded answers",
         TBD, DELIVERED],
        ["2", "Ingest pipeline — structuring, preview, commit, history", TBD, DELIVERED],
        ["3", "Cortex Analyst integration (optional semantic path)", TBD, DELIVERED],
        ["4", "Hardening & hosted rollout (see sequence below)", TBD, PLANNED],
    ],
    col_widths=[0.5, 3.7, 1.0, 1.1],
)
subhead("Phase 4 sequence (locked build order)")
bullets([
    ("4.1 Least-privilege Snowflake roles", "create BAYI_READ / BAYI_ADMIN_READ (break-glass) / "
     "BAYI_INGEST_WRITE, add the SENSITIVITY and INGESTED_BY columns, attach the rap_ownership "
     "row-access policy; retire ACCOUNTADMIN for the app."),
    ("4.2 Easy Auth identity + group wiring", "the app reads the server-injected caller email and "
     "group claim, using them as the owner stamp on ingest and the row filters on query."),
    ("4.3 Three-tier sensitivity toggle", "internal (default) for everyone; private (only me) for "
     "everyone; protected (only my group) gated to members of the privileged Entra group. "
     "Confidential tiers force the ingest-only path (nothing kept locally)."),
    ("4.4 Per-request session-identity + group binding", "before each query the app binds the "
     "caller identity and protected-group flag to the Snowflake session; the database returns "
     "internal rows plus the caller's own private rows plus, for group members, protected rows."),
])

# ── 6 · Technical architecture ──────────────────────────────────────
heading("6", "Technical architecture")
subhead("Application stack")
bullets([
    ("Query engine", "a run_sql tool (model-written SELECT, guarded) plus an optional "
     "cortex_analyst tool, behind one Tool abstraction so tools can be swapped without touching "
     "the loop."),
    ("Agentic loop", "Anthropic tool-use with prompt caching and a pre-classifier that routes "
     "each question (database / follow-up / web)."),
    ("Web UI", "FastAPI serving the query and ingest pages; agent runs serialized on a shared "
     "connector via a thread lock."),
])
subhead("Three security enforcement layers (Phase 4)")
bullets([
    ("Azure Easy Auth", "proves who you are and what groups you belong to — Microsoft Entra, "
     "BayOne single tenant only."),
    ("Application", "binds that caller identity and group flag to every request — the owner stamp "
     "on ingest and the row filters on query, from server-trusted headers the browser cannot set."),
    ("Snowflake row-access policy", "the last line — physically decides which rows you can see "
     "(internal for everyone, private only for its owner, protected only for the privileged group), "
     "even if the model writes SELECT *."),
])
note("Full detail — sequence diagrams, permission matrix, roles, and the row-access policy — is "
     "in the companion document docs/security-model.docx.")

# ── 7 · Security & data handling ────────────────────────────────────
heading("7", "Security & data handling")
bullets([
    ("No training on BayOne data", "Anthropic's commercial terms exclude customer content from "
     "model training."),
    ("Retention / ZDR", "30-day default retention; a Zero-Data-Retention agreement is pursued "
     "for sensitive workloads."),
    ("Confidential uploads are ingest-only", "documents marked private or protected are never "
     "written to local disk; they live solely in Snowflake."),
    ("Owner- and group-scoped access", "private data is readable only by the user who ingested it; "
     "protected data only by members of the privileged Entra group; everyone else sees internal "
     "(shared) rows only. Only that group's members may mark an upload protected. A logged "
     "break-glass admin role can read all data for offboarding/legal."),
])
note("Today both read and write connect as ACCOUNTADMIN and the web app has no authentication "
     "(local-only). Phase 4 retires ACCOUNTADMIN and puts the app behind Entra — the single "
     "biggest risk reduction in this SOW.", "warn")

# ── 8 · Assumptions & dependencies ──────────────────────────────────
heading("8", "Assumptions & dependencies")
bullets([
    "A Snowflake account, warehouse(s), and the target knowledge-base tables are available.",
    "An Anthropic API key is provisioned (and a ZDR agreement is in place for sensitive data).",
    "An Azure subscription with App Service and the BayOne Entra tenant are available.",
    ("Open inputs required before Phase 4 build", "(a) who holds the BAYI_ADMIN_READ break-glass "
     "role and how its use is logged/reviewed; (b) the Snowflake mechanism that carries the caller "
     "identity and group flag into the session for the row-access policy to read; (c) the exact "
     "privileged Entra group whose members may use the protected tier."),
])

# ── 9 · Roles & responsibilities ────────────────────────────────────
heading("9", "Roles & responsibilities")
styled_table(
    ["Role", "Owner"],
    [
        ["Delivery owner (build & integration)", "Atharv Sharma"],
        ["Break-glass admin holder (BAYI_ADMIN_READ)", TBD],
        ["Snowflake administrator (roles, policy, grants)", TBD],
        ["Azure administrator (App Service, Easy Auth)", TBD],
        ["Business sponsor / approver", TBD],
    ],
    col_widths=[4.3, 2.3],
)

# ── 10 · Timeline & milestones ──────────────────────────────────────
heading("10", "Timeline & milestones")
lede("Dates to be set at sign-off. Phases 1–3 are already delivered; the schedule below covers "
     "the remaining Phase 4 rollout.")
styled_table(
    ["Milestone", "Start", "End"],
    [
        ["Phase 4.1 — Least-privilege roles + owner/group row policy", TBD, TBD],
        ["Phase 4.2 — Easy Auth identity + group wiring", TBD, TBD],
        ["Phase 4.3 — Three-tier sensitivity toggle", TBD, TBD],
        ["Phase 4.4 — Per-request session-identity + group binding", TBD, TBD],
        ["Go-live (secured URL for BayOne staff)", TBD, TBD],
    ],
    col_widths=[3.6, 1.5, 1.5],
)

# ── 11 · Pricing ────────────────────────────────────────────────────
heading("11", "Pricing")
styled_table(
    ["Line item", "Basis", "Amount"],
    [
        ["Engineering / delivery effort", "Internal", TBD],
        ["Azure hosting (App Service, storage)", "Monthly", TBD],
        ["Snowflake compute (warehouses)", "Usage", TBD],
        ["Anthropic API usage", "Usage", TBD],
        ["Total", "", TBD],
    ],
    col_widths=[3.3, 1.6, 1.7],
)
note("All figures are placeholders to be completed at sign-off.", "warn")

# ── 12 · Acceptance criteria ────────────────────────────────────────
heading("12", "Acceptance criteria")
bullets([
    ("Grounded-answer eval passes", "the agent's answers match ground-truth SQL on the eval set."),
    ("Ingest round-trips", "a document structures, previews, and commits to the knowledge tables."),
    ("Authentication blocks outsiders", "only authenticated BayOne Entra accounts reach the app."),
    ("Owner- and group-scoped privacy is enforced", "a user's connection cannot return another "
     "user's private rows, nor a non-member's connection any protected rows, even under SELECT * "
     "(row-access policy verified)."),
    ("Least privilege confirmed", "no app path connects as ACCOUNTADMIN."),
])

# ── 13 · Sign-off ───────────────────────────────────────────────────
heading("13", "Sign-off")
styled_table(
    ["Party", "Name", "Signature", "Date"],
    [
        ["Delivery owner", "Atharv Sharma", "", TBD],
        ["Business sponsor", TBD, "", TBD],
    ],
    col_widths=[1.6, 2.0, 1.9, 1.1],
)

out = "/Users/atharvsharma/code/BayIAgent/docs/BayI-SOW.docx"
doc.save(out)
print("Saved", out)
