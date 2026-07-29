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


# ── How an ATTACHED file should be used ────────────────────────────────────────────────
# An upload used to be classified by extension alone: every .pptx became a deck TEMPLATE and
# was never shown to the model, so "summarize this deck" could not work — the file the user
# was asking about was the one file the model could not see. Intent decides instead, and a
# .pptx can serve BOTH roles in one turn ("summarise this deck and make a new one like it").

# The attachment is wanted as a style/format source.
_TEMPLATE_USE = re.compile(
    r"\b(template|theme|branding|brand|styling|style|format|formatting|look|design|layout|"
    r"master|skin|house\s+style|corporate\s+deck)\b",
    re.IGNORECASE,
)
# The user is asking ABOUT the file's contents.
_READ_USE = re.compile(
    r"\b(summar\w+|read|analy[sz]\w+|review|explain|extract|based on|from (?:this|the) "
    r"(?:file|deck|document|attachment|slide)|what(?:'s| is| does)|tell me about|critique|"
    r"improve|update|fix|translate|compare|key points|takeaways|according to)\b",
    re.IGNORECASE,
)
# "same headers", "keep the titles", "same structure" -> reuse the template's own headings,
# not merely its colours and fonts.
_KEEP_HEADERS = re.compile(
    r"\b(?:same|keep|reuse|retain|preserve|identical|matching)\b[^.]{0,40}?"
    r"\b(header|headers|heading|headings|title|titles|section|sections|structure|outline|"
    r"agenda|skeleton|framework)\b"
    r"|\b(header|headers|heading|headings|title|titles|section|sections|structure|outline)\b"
    r"[^.]{0,30}?\b(?:as|from|of)\s+(?:the\s+)?(?:template|deck|slide|slides|attachment)\b",
    re.IGNORECASE,
)
# "just the theme" / "only the styling" -> explicitly NOT the headers.
_THEME_ONLY = re.compile(
    r"\b(?:just|only|solely|merely)\b[^.]{0,25}?"
    r"\b(theme|styling|style|branding|colors?|colours?|fonts?|look|design)\b",
    re.IGNORECASE,
)

THEME_ONLY = "theme"
THEME_AND_HEADERS = "theme_and_headers"


def detect_template_mode(question: str | None) -> str:
    """How much of an attached deck to reuse: its look only, or its headings too.

    ``theme`` (the default) inherits the template's masters, layouts, fonts and colours while
    the generated content supplies every slide. ``theme_and_headers`` additionally keeps the
    template's own slide titles/sections, for "use the same headers and theme as this deck".
    An explicit "just the theme" wins over a passing mention of structure.
    """
    q = question or ""
    if _THEME_ONLY.search(q):
        return THEME_ONLY
    return THEME_AND_HEADERS if _KEEP_HEADERS.search(q) else THEME_ONLY


def describe_upload_use(question: str | None, is_deck: bool) -> dict[str, object]:
    """Decide what an attached file is FOR on this turn.

    Returns ``{"read": bool, "as_template": bool, "template_mode": str}``.

    * A non-deck attachment (PDF, Word, image, spreadsheet) is always content to read.
    * A deck (.pptx/.potx) is read when the question asks about its contents, and used as a
      template when the question asks for styling or for a new deck. Both can be true.
    * With no signal either way, a deck defaults to template use — the common case — but it
      is still made readable so a follow-up content question is not blocked.
    """
    q = question or ""
    if not is_deck:
        return {"read": True, "as_template": False, "template_mode": THEME_ONLY}

    wants_read = bool(_READ_USE.search(q))
    wants_template = bool(_TEMPLATE_USE.search(q)) or detect_export_request(q) == "pptx"
    if not wants_read and not wants_template:
        wants_template = True  # a bare deck attachment is most often a template
    return {
        "read": wants_read,
        "as_template": wants_template,
        "template_mode": detect_template_mode(q),
    }
