"""Offline tests for app-side document-metadata stamping (sf_agent.ingest).

`_apply_document_metadata` is the token-saving normalization: the model no longer repeats
document-level provenance on every row, so the app back-fills it authoritatively and defaults
the optional empties. These tests pin that contract without a warehouse or a model call.
"""

from datetime import date

from sf_agent.ingest import (
    _OPTIONAL_BLOCK_STRINGS,
    _apply_document_metadata,
    _source_parser_for,
)


def test_source_parser_maps_transport_not_original_extension():
    # Word/PowerPoint + PDF reach the model as rendered PDF pages -> pdf_vision.
    assert _source_parser_for("deck.pptx") == "pdf_vision"
    assert _source_parser_for("report.PDF") == "pdf_vision"
    # Modern Excel is serialized to per-sheet CSV text (a PDF render would paginate a
    # wide sheet and destroy positional grids like org charts); legacy .xls still
    # goes through LibreOffice -> PDF because openpyxl cannot read it.
    assert _source_parser_for("sheet.xlsx") == "text"
    assert _source_parser_for("sheet.xls") == "pdf_vision"
    # Images go through vision/OCR; text-family stays inline text.
    assert _source_parser_for("chart.png") == "ocr"
    assert _source_parser_for("notes.md") == "text"
    assert _source_parser_for("data.csv") == "text"


def test_blocks_get_app_authoritative_provenance_overwriting_model_values():
    # The model emitted a WRONG filename/parser/date; the app overwrites all three.
    blocks = [
        {"block_index": 0, "text_content": "a", "source_file": "typo.pdf",
         "source_parser": "pptx_chart", "extracted_at": "1999-01-01"},
        {"block_index": 1, "text_content": "b"},
    ]
    _apply_document_metadata({}, blocks, [], "Coherent Case Study.pptx")
    today = date.today().isoformat()
    for b in blocks:
        assert b["source_file"] == "Coherent Case Study.pptx"
        assert b["source_parser"] == "pdf_vision"
        assert b["extracted_at"] == today


def test_optional_empty_strings_are_defaulted_so_shape_is_stable():
    # A lean block omits every optional field; the app fills them with "" for the loader/UI.
    block = {"block_index": 0, "text_content": "prose"}
    _apply_document_metadata({}, [block], [], "deck.pdf")
    for f in _OPTIONAL_BLOCK_STRINGS:
        assert block[f] == ""


def test_present_optional_values_are_preserved():
    block = {
        "block_index": 0,
        "content_type": "table",
        "text_content": "caption",
        "table_markdown": "| a |\n| - |\n| 1 |",
    }
    _apply_document_metadata({}, [block], [], "deck.pdf")
    assert block["table_markdown"] == "| a |\n| - |\n| 1 |"
    # The untouched optionals still get a stable empty default.
    assert block["table_html"] == ""


def test_source_modified_at_comes_from_manifest_but_row_wins():
    manifest = {"source_modified_at": "2025-03-01"}
    blocks = [
        {"block_index": 0, "text_content": "a"},                              # omits -> manifest
        {"block_index": 1, "text_content": "b", "source_modified_at": "2024-12-25"},  # own value wins
    ]
    _apply_document_metadata(manifest, blocks, [], "deck.pdf")
    assert blocks[0]["source_modified_at"] == "2025-03-01"
    assert blocks[1]["source_modified_at"] == "2024-12-25"


def test_missing_manifest_modified_at_leaves_rows_without_it():
    block = {"block_index": 0, "text_content": "a"}
    _apply_document_metadata({}, [block], [], "deck.pdf")
    assert block.get("source_modified_at") in (None, "")


def test_facts_get_source_file_stamped():
    facts = [{"fact_id": "x", "raw_value": "$1.8M"}]
    _apply_document_metadata({}, [], facts, "deck.pdf")
    assert facts[0]["source_file"] == "deck.pdf"


def test_filename_is_preferred_over_manifest_source_file():
    # The manifest may carry a model-reported name; the real upload filename is authoritative.
    block = {"block_index": 0, "text_content": "a"}
    _apply_document_metadata({"source_file": "model-guess.pdf"}, [block], [], "real.pdf")
    assert block["source_file"] == "real.pdf"
