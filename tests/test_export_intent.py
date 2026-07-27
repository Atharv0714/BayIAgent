"""detect_export_request: does a question ask the agent to generate an office document?"""

import pytest

from sf_agent.export import detect_export_request


@pytest.mark.parametrize(
    "question,expected",
    [
        # explicit generation verb + format
        ("Make a PowerPoint of the top 10 clients by revenue", "pptx"),
        ("generate an excel of every placement", "xlsx"),
        ("create a word document summarizing the pipeline", "docx"),
        ("export this to PDF", "pdf"),
        ("build me a spreadsheet of bill rates", "xlsx"),
        ("put together a slide deck on hiring signals", "pptx"),
        ("convert the results to a pdf", "pdf"),
        ("I need this as a Word doc", "docx"),
        # output preposition immediately before the format word (no explicit verb)
        ("Top clients by revenue, in Excel", "xlsx"),
        ("Give me the summary as a PDF", "pdf"),
        ("the quarterly numbers as a slide deck", "pptx"),
        # case / spacing variants
        ("EXPORT AS XLSX please", "xlsx"),
        ("make a power point", "pptx"),
    ],
)
def test_detects_document_requests(question, expected):
    assert detect_export_request(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "How many clients are there?",
        "What is the total bill rate across all placements?",
        "How many of our clients use Excel internally?",  # 'Excel' but no generation cue
        "Which candidates have PowerPoint on their resume?",  # 'PowerPoint' as a skill, no cue
        "Summarize the pipeline",  # generation-ish verb but no format word
        "Give me the top 5 clients",  # 'give me' but no format word
        "",
        None,
    ],
)
def test_ignores_plain_questions(question):
    assert detect_export_request(question) is None


def test_earliest_mentioned_format_wins():
    # Both named; the earlier one ("excel") is chosen.
    assert detect_export_request("export an excel, or a pdf if easier") == "xlsx"


def test_word_requires_qualified_phrase_not_bare_word():
    # A bare "word" must not trigger docx (too common); needs "word document"/"doc"/"docx".
    assert detect_export_request("make a note of every keyword in the deck") == "pptx"
    assert detect_export_request("create a word doc of the notes") == "docx"
