"""Chat file upload: reusable content-block builder, /api/upload classify+store, and
export accepting a template_id. No live warehouse or model needed."""

import asyncio
import io
import json

import pytest
from fastapi import UploadFile

from sf_agent.ingest import IngestError, file_content_block
from sf_agent.web import ExportRequest, STATE, export, upload


@pytest.fixture(autouse=True)
def _clear_uploads():
    STATE.uploads.clear()
    yield
    STATE.uploads.clear()


def _pptx_bytes() -> bytes:
    from pptx import Presentation

    buf = io.BytesIO()
    Presentation().save(buf)
    return buf.getvalue()


def _upload(filename: str, data: bytes):
    return asyncio.run(upload(UploadFile(file=io.BytesIO(data), filename=filename)))


def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


# --- file_content_block (reused by ingest + chat context) ------------------------------

def test_file_content_block_text_returns_text_block():
    block = file_content_block("notes.txt", b"hello world")
    assert block["type"] == "text"
    assert "hello world" in block["text"]


def test_file_content_block_unsupported_raises():
    with pytest.raises(IngestError, match="Unsupported file type"):
        file_content_block("mystery.xyz", b"\x00\x01")


# --- /api/upload classification + storage ---------------------------------------------

def test_upload_pptx_is_template_and_keeps_raw():
    resp = _upload("brand.pptx", _pptx_bytes())
    body = _body(resp)
    assert body["ok"] and body["kind"] == "template"
    rec = STATE.uploads[body["upload_id"]]
    assert rec["kind"] == "template" and isinstance(rec["raw"], bytes) and "block" not in rec


def test_upload_text_is_context_and_stores_block():
    resp = _upload("brief.txt", b"BayOne retail brief")
    body = _body(resp)
    assert body["ok"] and body["kind"] == "context"
    rec = STATE.uploads[body["upload_id"]]
    assert rec["kind"] == "context" and rec["block"]["type"] == "text" and "raw" not in rec


def test_upload_empty_file_400():
    resp = _upload("empty.txt", b"")
    assert resp.status_code == 400


def test_upload_unsupported_type_415():
    resp = _upload("weird.xyz", b"\x00\x01\x02")
    assert resp.status_code == 415


# --- export accepts a template_id ------------------------------------------------------

def test_export_pptx_with_template_id_renders():
    STATE.uploads["tpl1"] = {"filename": "brand.pptx", "kind": "template", "raw": _pptx_bytes()}
    req = ExportRequest(
        format="pptx",
        question="Make a deck",
        answer={"answer": "x", "values": {"slides": [{"title": "A", "content": ["b"]}]}},
        template_id="tpl1",
    )
    resp = export(req)
    assert resp.status_code == 200
    assert bytes(resp.body)[:2] == b"PK"  # a valid pptx/zip


def test_export_missing_template_id_falls_back():
    # A stale/unknown template_id must not error — it falls back to the default deck.
    req = ExportRequest(
        format="pptx",
        question="Make a deck",
        answer={"answer": "x", "values": {"slides": [{"title": "A", "content": ["b"]}]}},
        template_id="does-not-exist",
    )
    resp = export(req)
    assert resp.status_code == 200
    assert bytes(resp.body)[:2] == b"PK"
