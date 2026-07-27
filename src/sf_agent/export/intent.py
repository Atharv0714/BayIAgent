"""Detect when a question is asking the agent to GENERATE an office document.

The query agent already produces a grounded answer (value / summary / values / chart),
and the export renderers turn that into xlsx/docx/pptx/pdf. This closes the loop: when a
user *asks* for a document ("make a PowerPoint of the top clients", "export this to PDF"),
/api/ask returns the detected format so the UI auto-renders the file from that same
grounded answer — no re-query, no extra model call.

Deterministic on purpose (no token cost, predictable): it requires BOTH a format word and
a generation cue, so "how many clients use Excel?" does not trigger an export.
"""

from __future__ import annotations

import re

# fmt -> word-bounded synonym alternation. List order breaks ties when several formats
# are named (earliest mention in the text wins first; order here is the final tie-break).
_FORMAT_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("pptx", r"powerpoint|power\s?point|pptx?|slide\s?deck|slides|presentation|deck"),
    ("xlsx", r"excel|xlsx?|spreadsheet|workbook|worksheet"),
    ("docx", r"word\s+document|word\s+doc\w*|word\s+file|microsoft\s+word|ms\s+word|docx|word\s+format"),
    ("pdf", r"pdf"),
)

# A generation/output intent must accompany the format word. Bare "as/in/to" are too
# common to be verbs, so they only count via _OUTPUT_PREP (directly before the format).
_GEN_VERB = re.compile(
    r"\b(generate|create|make|build|produce|export|download|save|draft|prepare|compile|"
    r"convert|give me|i need|i want|send me|put together|turn (?:this|it|that))\b",
    re.IGNORECASE,
)
_OUTPUT_PREP = r"(?:as|in|to|into)\s+(?:an?\s+)?"


def detect_export_request(question: str | None) -> str | None:
    """Return the office format (``xlsx``/``docx``/``pptx``/``pdf``) the user asked to
    generate, or ``None`` when the question is a normal query.

    Fires only when a format word co-occurs with a generation cue — a verb like
    "make"/"export"/"convert", or an output preposition immediately before the format word
    ("as a PDF", "in Excel", "to a slide deck"). When several formats are named, the one
    mentioned earliest wins.
    """
    if not question:
        return None
    q = question.lower()
    has_verb = bool(_GEN_VERB.search(q))
    best_pos: int | None = None
    best_fmt: str | None = None
    for fmt, syn in _FORMAT_SYNONYMS:
        m = re.search(rf"\b(?:{syn})\b", q)
        if not m:
            continue
        if has_verb or re.search(rf"{_OUTPUT_PREP}(?:{syn})\b", q):
            if best_pos is None or m.start() < best_pos:
                best_pos, best_fmt = m.start(), fmt
    return best_fmt
