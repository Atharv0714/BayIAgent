"""Offline tests for the ingest validation gates (sf_agent.ingest.validate)."""

from sf_agent.ingest import StructuredResult, validate


def _good_block(**over):
    b = {
        "block_index": 0,
        "section_number": 1,
        "section_title": "Overview",
        "section_theme": "intro",
        "block_order": 0,
        "content_type": "narrative",
        "block_status": "filled",
        "text_content": "Some prose.",
    }
    b.update(over)
    return b


def _good_fact(**over):
    f = {
        "fact_id": "abc",
        "source_file": "deck.pdf",
        "source_locator": "slide 3",
        "entity_type": "client",
        "entity_name": "HPE",
        "attribute": "revenue",
        "period": "CY25",
        "value_num": 1800000,
        "unit": "usd",
        "value_text": "",
        "raw_value": "$1.8M",
        "confidence": 0.9,
        "notes": "",
    }
    f.update(over)
    return f


def _payload(blocks=None, facts=None, coverage_ok=True):
    return {
        "manifest": {"source_file": "deck.pdf", "coverage_ok": coverage_ok, "warnings": []},
        "blocks": blocks if blocks is not None else [_good_block()],
        "facts": facts if facts is not None else [_good_fact()],
    }


def test_clean_payload_has_no_errors():
    warnings, errors = validate(_payload())
    assert errors == []
    assert warnings == []


def test_coverage_not_ok_is_an_error():
    _, errors = validate(_payload(coverage_ok=False))
    assert any("coverage_ok" in e for e in errors)


def test_missing_required_block_field_errors():
    bad = _good_block()
    del bad["section_theme"]
    _, errors = validate(_payload(blocks=[bad]))
    assert any("section_theme" in e for e in errors)


def test_block_field_present_but_null_errors():
    _, errors = validate(_payload(blocks=[_good_block(text_content=None)]))
    assert any("text_content" in e for e in errors)


def test_content_type_outside_closed_set_errors():
    _, errors = validate(_payload(blocks=[_good_block(content_type="paragraph")]))
    assert any("content_type" in e and "paragraph" in e for e in errors)


def test_block_status_outside_closed_set_errors():
    _, errors = validate(_payload(blocks=[_good_block(block_status="done")]))
    assert any("block_status" in e for e in errors)


def test_fact_unit_outside_closed_set_errors():
    _, errors = validate(_payload(facts=[_good_fact(unit="dollars")]))
    assert any("unit" in e for e in errors)


def test_fact_value_num_string_is_rejected():
    _, errors = validate(_payload(facts=[_good_fact(value_num="1800000")]))
    assert any("value_num" in e for e in errors)


def test_fact_value_num_null_is_allowed():
    _, errors = validate(_payload(facts=[_good_fact(value_num=None)]))
    assert errors == []


def test_fact_value_num_bool_is_rejected():
    # True is an int subclass in Python; it must not sneak past the number check.
    _, errors = validate(_payload(facts=[_good_fact(value_num=True)]))
    assert any("value_num" in e for e in errors)


def test_fact_empty_raw_value_errors():
    _, errors = validate(_payload(facts=[_good_fact(raw_value="   ")]))
    assert any("raw_value" in e for e in errors)


def test_manifest_warnings_pass_through():
    payload = _payload()
    payload["manifest"]["warnings"] = ["chart vs table conflict on slide 4"]
    warnings, errors = validate(payload)
    assert warnings == ["chart vs table conflict on slide 4"]
    assert errors == []


def test_missing_manifest_errors():
    _, errors = validate({"blocks": [], "facts": []})
    assert any("manifest" in e for e in errors)


def _good_table(**over):
    # A clean rectangular table: Markdown with a header separator row and a rich caption
    # that names its columns, so the findability warnings stay quiet.
    fields = {
        "content_type": "table",
        "text_content": "Quarterly revenue and gross margin by business unit (CY25).",
        "table_markdown": "| Unit | Revenue | Margin |\n| --- | --- | --- |\n| Cloud | 1.8M | 34% |",
    }
    fields.update(over)
    return _good_block(**fields)


def test_table_missing_markdown_errors():
    # Markdown is the canonical form the readers use, so a table without it is a hard fail.
    _, errors = validate(_payload(blocks=[_good_table(table_markdown="")]))
    assert any("table_markdown" in e for e in errors)


def test_clean_table_has_no_diagnostics():
    warnings, errors = validate(_payload(blocks=[_good_table()]))
    assert errors == []
    assert warnings == []


def test_table_markdown_without_header_warns():
    # Present but headerless Markdown is a soft signal that a split dropped the header row.
    warnings, errors = validate(
        _payload(blocks=[_good_table(table_markdown="Cloud 1.8M 34%\nEdge 0.9M 28%")])
    )
    assert errors == []
    assert any("header" in w for w in warnings)


def test_short_table_caption_warns():
    warnings, _ = validate(_payload(blocks=[_good_table(text_content="Revenue")]))
    assert any("caption" in w for w in warnings)


def test_fact_source_block_index_non_int_errors():
    _, errors = validate(_payload(facts=[_good_fact(source_block_index="3")]))
    assert any("source_block_index" in e for e in errors)


def test_fact_source_block_index_int_is_allowed():
    _, errors = validate(_payload(facts=[_good_fact(source_block_index=3)]))
    assert errors == []


def test_fact_source_block_index_bool_is_rejected():
    # bool is an int subclass; a stray True must not pass as a block reference.
    _, errors = validate(_payload(facts=[_good_fact(source_block_index=True)]))
    assert any("source_block_index" in e for e in errors)


def test_structured_result_committable_property():
    ok = StructuredResult(manifest={"coverage_ok": True}, errors=[])
    assert ok.committable is True
    blocked = StructuredResult(manifest={"coverage_ok": True}, errors=["bad"])
    assert blocked.committable is False
    no_cov = StructuredResult(manifest={"coverage_ok": False}, errors=[])
    assert no_cov.committable is False
