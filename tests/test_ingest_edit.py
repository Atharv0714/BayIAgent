"""Offline tests for the post-structuring edit helpers (sf_agent.ingest).

Covers the pure pieces the /api/ingest/edit endpoint relies on:
  * `tier_requirements` — what the caller must prove given the tiers in a payload;
  * `coerce_for_validation` — re-typing browser-supplied string cells so `validate`
    behaves like the model path;
  * `stamp_sensitivity` — the server overwriting every row's tier so model output can't
    decide classification.
"""

from __future__ import annotations

from sf_agent.ingest import (
    SENSITIVITIES,
    StructuredResult,
    coerce_for_validation,
    stamp_sensitivity,
    tier_requirements,
)


def test_sensitivities_are_the_three_tiers() -> None:
    assert SENSITIVITIES == ("internal", "private", "protected")


# ── tier_requirements ────────────────────────────────────────────────────────
def test_all_internal_needs_nothing() -> None:
    assert tier_requirements(["internal", "internal"]) == (False, False)


def test_private_needs_identity_not_group() -> None:
    assert tier_requirements(["internal", "private"]) == (True, False)


def test_protected_needs_identity_and_group() -> None:
    assert tier_requirements(["internal", "protected"]) == (True, True)


def test_empty_needs_nothing() -> None:
    assert tier_requirements([]) == (False, False)


# ── coerce_for_validation ────────────────────────────────────────────────────
def test_coerce_block_ints_and_blank_to_none() -> None:
    blocks = [{"block_index": "2", "section_number": "1", "block_order": "", "text_content": "x"}]
    out_blocks, _ = coerce_for_validation(blocks, [])
    rec = out_blocks[0]
    assert rec["block_index"] == 2 and isinstance(rec["block_index"], int)
    assert rec["section_number"] == 1
    assert rec["block_order"] is None  # blank string collapses to None
    assert rec["text_content"] == "x"  # untouched


def test_coerce_fact_numbers() -> None:
    facts = [{"value_num": "1800000", "confidence": "0.9", "raw_value": "$1.8M"}]
    _, out_facts = coerce_for_validation([], facts)
    rec = out_facts[0]
    assert rec["value_num"] == 1800000
    assert rec["confidence"] == 0.9
    assert rec["raw_value"] == "$1.8M"


def test_coerce_leaves_non_numeric_strings() -> None:
    # A non-numeric value_num stays a string so validate() can reject it downstream.
    _, out_facts = coerce_for_validation([], [{"value_num": "n/a"}])
    assert out_facts[0]["value_num"] == "n/a"


def test_coerce_does_not_mutate_inputs() -> None:
    blocks = [{"block_index": "3"}]
    coerce_for_validation(blocks, [])
    assert blocks[0]["block_index"] == "3"  # original untouched


# ── stamp_sensitivity ────────────────────────────────────────────────────────
def test_stamp_overwrites_every_row() -> None:
    s = StructuredResult(
        blocks=[{"block_index": 0, "sensitivity": "protected"}, {"block_index": 1}],
        facts=[{"fact_id": "f1"}],
    )
    stamp_sensitivity(s, "private")
    assert all(b["sensitivity"] == "private" for b in s.blocks)
    assert all(f["sensitivity"] == "private" for f in s.facts)


def test_stamp_repairs_unknown_tier_to_internal() -> None:
    s = StructuredResult(blocks=[{"block_index": 0}], facts=[])
    stamp_sensitivity(s, "bogus")
    assert s.blocks[0]["sensitivity"] == "internal"
