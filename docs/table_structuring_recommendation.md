# Structuring Tables in the Ingest Pipeline — Recommendation

*Scope: how the structuring step (`structure_upload` → `ingest_prompt.md` → `blocks`/`facts`) should represent tabular content. Optimized for a balance of RAG retrieval and SQL analytics.*

## The current design, in one paragraph

Every uploaded file is passed to a single Claude call that emits one JSON object mapping into two fixed Snowflake tables. Tabular content lands in **two places at once**: (1) a `blocks` row with `content_type="table"` that stores *both* `table_html` and `table_markdown` plus a required caption in `text_content`, and (2) one `facts` row per atomic numeric cell in long-form EAV shape (`entity_name`, `attribute`, `period`, `value_num`, `unit`, `raw_value`, …). `blocks` feeds retrieval/RAG; `facts` feeds SQL analytics. This split — a human-readable lane and a computable lane — is the right high-level architecture, and the research below supports keeping it. The recommendations refine *how* each lane represents a table, not whether to have two lanes.

## What the research says

**Serialization format matters, but no single format wins outright.** Across 2025 benchmarks, Markdown and HTML are consistently the top tier for LLM table comprehension; CSV and JSONL are consistently the worst. One 11-format study put Markdown-style key/value at 60.7% accuracy vs HTML 53.6% and JSON 52.3%, while other benchmarks found HTML/LaTeX a couple of F1 points *ahead* of Markdown. The honest summary: Markdown and HTML are both good, the gap between them is small and model-dependent, and the format is not the dominant driver of end-to-end quality.

**Token cost cuts against HTML.** HTML uses roughly 3× the tokens of CSV and materially more than Markdown for the same table. Markdown gives ~top-tier comprehension at a fraction of HTML's token count.

**But Markdown cannot represent structurally complex tables.** Markdown pipe tables have no way to express `rowspan`/`colspan`, multi-level/spanning headers, or nested tables — exactly the structures that table-extraction benchmarks (TEDS-scored) identify as the hardest and most meaning-critical. Flattening a merged-cell or two-row-header table into Markdown silently destroys the parent/child relationship between headers and values. HTML preserves it.

**Numbers embed poorly for retrieval.** A grid of numbers has little semantic signal for a vector search, so a table chunk is often un-findable by the embedding of its own body. The fix every RAG-table guide converges on: attach out-of-chunk context to the chunk — the header path, a caption, a one-line semantic summary, the surrounding section title — so the table is retrievable by what it's *about*. Tables should also never be split mid-structure, and when a table spans pages its header must be repeated on each piece.

**EAV is flexible to land but expensive to query.** The `facts` table is Entity-Attribute-Value. EAV is the correct choice when the set of attributes is large and unpredictable — which is precisely this ingest (arbitrary documents, open `attribute` vocabulary). But EAV is a known anti-pattern for analytics *at query time*: it forces self-joins and pivots, invites fan-out errors, and degrades on OLAP-scale data. Concretely, one 2026 benchmark measured text-to-SQL at **32.7% accuracy on raw/unnormalized tables vs 72.7–100% against a properly modeled semantic layer**. The lesson is not "abandon EAV" — it's "don't point the query LLM directly at raw EAV."

## Recommendations

### 1. Make Markdown the canonical LLM-facing serialization; keep HTML conditional, not automatic
Today the prompt asks for *both* `table_html` and `table_markdown` on every table. Instead:

- **Always** populate `table_markdown` — it is the default form the retrieval lane and the query agent should read.
- **Only** populate `table_html` when the table's structure cannot survive Markdown: merged/spanning cells, multi-row or hierarchical headers, or nested tables. For a plain rectangular grid, leave `table_html` empty.

This keeps the format that comprehension studies favor and that is token-cheap, while retaining HTML exactly where it earns its 3× token cost — as the loss-free structural fallback. Add a flag (e.g. reuse `notes`/`block_status` or a short marker in `text_content`) indicating *why* HTML was emitted, so downstream readers know the Markdown is a lossy view of that specific table.

### 2. Make every table chunk retrievable by enriching the caption
`text_content` is already required and already "captions every table." Tighten what that caption must contain, because it is the only part of a numeric table that embeds well:

- the table's subject in plain language ("Quarterly revenue and gross margin by business unit"),
- the **column/header names** spelled out (so a query mentioning a column matches even when the body is numbers),
- the entities and the period/time coverage,
- one sentence of what the table shows or concludes.

This is a prompt change, not a schema change, and it directly attacks the "numbers don't embed" problem.

### 3. Preserve structure across splits
Reinforce two rules already partially present: never atomize a table across blocks, and when a source table spans pages/slides, **stitch it into one block and repeat the header** on any continuation so no chunk carries headerless rows. The prompt mentions cross-page stitching for PDFs; make it a universal rule and a validation gate.

### 4. Keep EAV for landing — but don't let the query agent hit it raw
The `facts` EAV model is correct for ingestion. Mitigate its query-time cost:

- **Enforce the vocabulary registry** you already have. EAV's failure mode is `revenue` vs `rev` vs `net_revenue` drift; the governed `attribute`/`entity_type` registry passed back to the model is the single most important control. Treat "reused an existing attribute when one fit" as a checked expectation, not a suggestion.
- **Add a semantic/view layer for common metrics.** Rather than asking the query LLM to pivot EAV by hand (the 32.7% path), maintain a handful of curated wide views (e.g. one row per `entity_name` × `period`, common attributes as columns) that the agent queries for the frequent questions, falling back to raw `facts` only for the long tail. This is the 72–100% path.
- **Keep the strict numeric normalization.** `value_num` normalized to a base unit with `unit` and verbatim `raw_value` preserved is exactly right and should not be relaxed — it is what makes `SUM`/`AVG` correct and auditable.

### 5. Add a join key between a fact and the block it came from
Right now a `fact` carries `source_locator` (slide/cell) and a block carries a computed `block_id`, but there is no direct link between the two. Emit a stable reference (e.g. the fact records the `block_index` it was extracted from) so the two lanes cooperate: a number retrieved analytically can surface its source table for context, and a table retrieved for RAG can expose its computable facts. This is the cheap connective tissue that makes "balanced for both" real rather than two parallel silos.

### 6. Consider a third, optional representation for clean tables — but only if needed
For tables that are already a regular grid with a genuine header row, a JSON array-of-objects (`[{col: val, …}, …]`) is the most directly computable form and joins the retrieval and analytics goals for that easy case. This is optional and adds a fourth column of storage; only pursue it if you find the query agent struggling to compute over the Markdown/EAV combination. Do **not** adopt CSV/JSONL as a table format anywhere — they are the worst performers for LLM comprehension in every benchmark reviewed.

## Summary table

| Concern | Current | Recommended |
|---|---|---|
| Default table format for LLM | HTML + Markdown always | Markdown canonical; HTML only for merged/hierarchical/nested |
| Retrievability of numeric tables | caption required | caption required **+ column names + semantic summary** |
| Table split across pages | stitched (PDF) | universal stitch + repeat header, as a gate |
| Analytics store | EAV `facts` | keep EAV to land; add curated wide views/semantic layer for the agent |
| Vocabulary drift | registry passed to model | registry **enforced** as a validation expectation |
| RAG ↔ SQL linkage | none (locator only) | fact carries its source `block_index` |
| Formats to avoid | — | CSV / JSONL as a table representation |

## Net
Keep the two-lane architecture. Change five things: prefer Markdown and make HTML a structural fallback, make captions carry the semantic + header text that makes numeric tables findable, guarantee headers survive page splits, protect EAV analytics with a governed vocabulary plus a wide/semantic view layer for the query agent, and link each fact back to its source block. These are mostly `ingest_prompt.md` and query-side changes; the fixed `blocks`/`facts` schema barely moves (one optional linkage field).
