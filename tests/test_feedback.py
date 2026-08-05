"""Answer ratings: the CSV store, its guards, and the /api/feedback endpoint."""

import asyncio
import csv

import pytest
from fastapi import Request

from sf_agent import feedback
from sf_agent.web import FeedbackRequest, STATE, feedback as feedback_endpoint


@pytest.fixture()
def ratings(tmp_path):
    return tmp_path / "ratings.csv"


def _rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# --- the store ------------------------------------------------------------------------

def test_record_writes_a_header_once_then_one_row_per_rating(ratings):
    feedback.record(5, "s1", 0, route="database", path=ratings)
    feedback.record(2, "s1", 2, route="followup", path=ratings)

    text = ratings.read_text(encoding="utf-8")
    assert text.count("rated_at,score") == 1, "header must be written exactly once"
    rows = _rows(ratings)
    assert [r["score"] for r in rows] == ["5", "2"]
    assert [r["route"] for r in rows] == ["database", "followup"]
    # The emoji is presentation; only the number is stored.
    assert "😀" not in text and "😞" not in text


def test_record_stores_context_that_makes_a_score_actionable(ratings):
    feedback.record(
        1, "s1", 4, route="web", elapsed_ms=8123.7, cost_usd=0.002059,
        rated_by="rep@bayone.com", question="How many clients?", path=ratings,
    )
    row = _rows(ratings)[0]
    assert row["turn"] == "4" and row["rated_by"] == "rep@bayone.com"
    assert row["elapsed_ms"] == "8124"  # rounded: sub-millisecond precision is noise
    assert row["cost_usd"] == "0.002059"
    assert row["question"] == "How many clients?"
    assert row["rated_at"].startswith("20") and "T" in row["rated_at"]


@pytest.mark.parametrize("bad", [0, 6, -1, 42, "high", None, 2.7, True, ""])
def test_record_rejects_a_score_outside_one_to_five(bad, ratings):
    """The file is meant to be read as numbers 1-5; a bad client must not put junk in it."""
    with pytest.raises(ValueError):
        feedback.record(bad, "s1", 0, path=ratings)
    assert not ratings.exists()


def test_record_accepts_a_numeric_string_score(ratings):
    feedback.record("4", "s1", 0, path=ratings)
    assert _rows(ratings)[0]["score"] == "4"


def test_question_text_cannot_smuggle_a_formula_into_excel(ratings):
    """This file exists to be opened in a spreadsheet, and a leading =, +, - or @ makes
    free text execute as a formula there. It must land as inert, visible text."""
    feedback.record(3, "s1", 0, question="=cmd|'/c calc'!A1", path=ratings)
    row = _rows(ratings)[0]
    assert row["question"].startswith("'="), row["question"]
    assert "cmd" in row["question"], "the text itself must survive, just defused"


def test_newlines_in_a_question_cannot_break_row_alignment(ratings):
    feedback.record(3, "s1", 0, question="line one\nline two\r\nthree", path=ratings)
    feedback.record(4, "s1", 1, path=ratings)
    rows = _rows(ratings)
    assert len(rows) == 2, "an embedded newline must not split into extra rows"
    assert "\n" not in rows[0]["question"]


def test_a_long_question_is_truncated(ratings):
    feedback.record(3, "s1", 0, question="x" * 5000, path=ratings)
    assert len(_rows(ratings)[0]["question"]) == 200


# --- summary --------------------------------------------------------------------------

def test_summary_counts_each_answer_once_however_often_it_was_re_rated(ratings):
    """Re-rating appends rather than edits, so a naive count would let one answer vote
    as many times as the user changed their mind."""
    feedback.record(1, "s1", 0, route="database", path=ratings)
    feedback.record(3, "s1", 0, route="database", path=ratings)
    feedback.record(5, "s1", 0, route="database", path=ratings)  # final word on turn 0
    feedback.record(4, "s1", 2, route="followup", path=ratings)

    s = feedback.summarize(path=ratings)
    assert s["rated_answers"] == 2
    assert s["average"] == 4.5  # (5 + 4) / 2, not an average over all four rows
    assert s["distribution"] == {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}
    assert s["by_route"]["database"] == {"count": 1, "average": 5.0}


def test_summary_of_an_empty_store_is_zeroed_not_an_error(ratings):
    s = feedback.summarize(path=ratings)
    assert s["rated_answers"] == 0 and s["average"] is None
    assert feedback.read_ratings(path=ratings) == []


# --- default location -----------------------------------------------------------------

def test_default_path_prefers_persistent_storage_on_app_service(monkeypatch):
    """Only /home survives on App Service — the deployment is extracted to a temp dir that
    is replaced on restart, so ratings written beside the code would silently vanish."""
    monkeypatch.setenv("WEBSITE_SITE_NAME", "bayiagent")
    monkeypatch.delenv("FEEDBACK_PATH", raising=False)
    assert str(feedback.default_path()).startswith("/home/")

    monkeypatch.delenv("WEBSITE_SITE_NAME")
    assert not str(feedback.default_path()).startswith("/home/")

    monkeypatch.setenv("FEEDBACK_PATH", "/tmp/custom.csv")
    assert str(feedback.default_path()) == "/tmp/custom.csv"


# --- the endpoint ---------------------------------------------------------------------

def _request(headers=None):
    scope = {
        "type": "http", "method": "POST", "path": "/api/feedback",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


def test_endpoint_records_a_rating(ratings, monkeypatch):
    monkeypatch.setattr(feedback, "default_path", lambda: ratings)
    resp = feedback_endpoint(
        FeedbackRequest(score=4, session_id="s1", turn=0, route="database"), _request()
    )
    assert resp.status_code == 200
    assert _rows(ratings)[0]["score"] == "4"


def test_endpoint_rejects_an_out_of_range_score(ratings, monkeypatch):
    monkeypatch.setattr(feedback, "default_path", lambda: ratings)
    resp = feedback_endpoint(FeedbackRequest(score=9, session_id="s1", turn=0), _request())
    assert resp.status_code == 400
    assert not ratings.exists()


def test_endpoint_takes_the_rater_from_the_request_not_the_body(ratings, monkeypatch):
    """A client must not be able to file a rating under somebody else's name."""
    from sf_agent.config import AuthConfig

    monkeypatch.setattr(feedback, "default_path", lambda: ratings)
    prev = STATE.auth_config
    STATE.auth_config = AuthConfig(enforce_ownership=True, dev_caller_identity="dev@bayone.com")
    try:
        feedback_endpoint(FeedbackRequest(score=5, session_id="s1", turn=0), _request())
    finally:
        STATE.auth_config = prev
    assert _rows(ratings)[0]["rated_by"] == "dev@bayone.com"
    # ...and the request model has no field a client could set it with.
    assert "rated_by" not in FeedbackRequest.model_fields


def test_a_failed_write_does_not_raise_into_the_chat(ratings, monkeypatch):
    """Telemetry must never take the app down: a read-only disk returns 503, not a 500."""
    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(feedback, "record", boom)
    resp = feedback_endpoint(FeedbackRequest(score=3, session_id="s1", turn=0), _request())
    assert resp.status_code == 503


def test_concurrent_ratings_all_land_intact(tmp_path):
    """Appends are serialized under a mutex, so parallel clicks cannot interleave into a
    half-written line that would break every later row's columns."""
    import threading

    path = tmp_path / "concurrent.csv"
    errors = []

    def rate(i):
        try:
            feedback.record((i % 5) + 1, f"s{i}", i, question=f"q{i}", path=path)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=rate, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    rows = _rows(path)
    assert len(rows) == 40
    assert all(r["score"].isdigit() and r["session_id"].startswith("s") for r in rows)
