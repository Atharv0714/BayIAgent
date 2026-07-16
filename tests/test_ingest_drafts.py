"""Offline tests for the ingest draft store (sf_agent.ingest_drafts).

A temp directory stands in for the on-disk draft dir so save/list/get/delete and
the restart-rehydration round trip are exercised without a server.
"""

from sf_agent import ingest_drafts
from sf_agent.ingest import StructuredResult


def _result(**over):
    data = {
        "manifest": {"coverage_ok": True, "source_file": "deck.pdf"},
        "blocks": [{"block_index": 0, "text_content": "prose"}],
        "facts": [{"fact_id": "a", "raw_value": "$1.8M"}],
        "warnings": [],
        "errors": [],
    }
    data.update(over)
    return StructuredResult(**data)


def test_save_then_get_round_trips(tmp_path):
    created = ingest_drafts.save(tmp_path, "id1", "deck.pdf", _result())
    assert created  # ISO timestamp string
    record = ingest_drafts.get(tmp_path, "id1")
    assert record["source_file"] == "deck.pdf"
    assert record["created_at"] == created
    assert record["result"]["blocks"][0]["text_content"] == "prose"


def test_summaries_report_counts_newest_first(tmp_path):
    ingest_drafts.save(tmp_path, "old", "a.pdf", _result())
    ingest_drafts.save(
        tmp_path, "new", "b.csv", _result(errors=["bad"], facts=[{"fact_id": "x"}, {"fact_id": "y"}])
    )
    rows = ingest_drafts.summaries(tmp_path)
    assert len(rows) == 2
    # Newest first (created_at desc); "new" was written last.
    assert rows[0]["ingest_id"] == "new"
    assert rows[0]["fact_count"] == 2
    assert rows[0]["error_count"] == 1
    assert rows[1]["ingest_id"] == "old"


def test_load_all_rehydrates_structured_results(tmp_path):
    ingest_drafts.save(tmp_path, "id1", "deck.pdf", _result())
    loaded = ingest_drafts.load_all(tmp_path)
    assert set(loaded) == {"id1"}
    assert isinstance(loaded["id1"], StructuredResult)
    assert loaded["id1"].committable is True


def test_delete_removes_the_draft(tmp_path):
    ingest_drafts.save(tmp_path, "id1", "deck.pdf", _result())
    assert ingest_drafts.delete(tmp_path, "id1") is True
    assert ingest_drafts.get(tmp_path, "id1") is None
    assert ingest_drafts.delete(tmp_path, "id1") is False  # already gone


def test_missing_dir_is_empty_not_an_error(tmp_path):
    missing = tmp_path / "nope"
    assert ingest_drafts.summaries(missing) == []
    assert ingest_drafts.load_all(missing) == {}
    assert ingest_drafts.get(missing, "id1") is None


def test_new_records_start_as_draft(tmp_path):
    ingest_drafts.save(tmp_path, "id1", "deck.pdf", _result())
    rows = ingest_drafts.summaries(tmp_path)
    assert rows[0]["status"] == "draft"
    assert rows[0]["committed_at"] is None
    assert ingest_drafts.get(tmp_path, "id1")["status"] == "draft"


def test_mark_committed_flips_status_and_records_counts(tmp_path):
    ingest_drafts.save(tmp_path, "id1", "deck.pdf", _result())
    assert ingest_drafts.mark_committed(tmp_path, "id1", blocks_written=3, facts_written=5) is True
    rec = ingest_drafts.get(tmp_path, "id1")
    assert rec["status"] == "committed"
    assert rec["committed_at"]  # ISO timestamp set
    assert rec["blocks_written"] == 3
    assert rec["facts_written"] == 5
    # Committed records surface their commit counts in the sidebar summary.
    row = ingest_drafts.summaries(tmp_path)[0]
    assert row["status"] == "committed"
    assert row["blocks_written"] == 3
    # A committed record is history, not a resumable draft, so load_all skips it.
    assert ingest_drafts.load_all(tmp_path) == {}


def test_mark_committed_missing_record_is_false(tmp_path):
    assert ingest_drafts.mark_committed(tmp_path, "nope", 1, 1) is False
