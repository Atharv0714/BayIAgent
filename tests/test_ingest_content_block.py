"""Offline tests for the ingest content-block builder (sf_agent.ingest)."""

import base64

import pytest

from sf_agent import ingest
from sf_agent.ingest import IngestError, build_content_block


def test_pdf_becomes_a_document_block():
    raw = b"%PDF-1.4 fake"
    blocks = build_content_block("deck.pdf", raw)
    assert blocks[0]["type"] == "document"
    src = blocks[0]["source"]
    assert src["media_type"] == "application/pdf"
    assert base64.b64decode(src["data"]) == raw
    # A trailing instruction text block is always appended.
    assert blocks[-1]["type"] == "text"
    assert "deck.pdf" in blocks[-1]["text"]


def test_png_becomes_an_image_block():
    blocks = build_content_block("chart.PNG", b"\x89PNG data")
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"


def test_jpg_and_jpeg_map_to_jpeg_media_type():
    for name in ("photo.jpg", "photo.jpeg"):
        blocks = build_content_block(name, b"\xff\xd8\xff data")
        assert blocks[0]["source"]["media_type"] == "image/jpeg"


def test_text_family_is_decoded_inline_with_filename():
    blocks = build_content_block("data.csv", b"client,revenue\nHPE,1800000\n")
    assert blocks[0]["type"] == "text"
    assert "Filename: data.csv" in blocks[0]["text"]
    assert "HPE,1800000" in blocks[0]["text"]


def test_text_decode_is_lossy_not_fatal():
    # Invalid UTF-8 must not raise — errors="replace" keeps ingestion going.
    blocks = build_content_block("notes.txt", b"caf\xe9 \xff\xfe")
    assert blocks[0]["type"] == "text"
    assert "caf" in blocks[0]["text"]


def test_office_file_is_converted_to_a_pdf_block(monkeypatch):
    # LibreOffice is stubbed so the test stays offline; the office branch must route the
    # converted bytes through the PDF path (document block, application/pdf).
    monkeypatch.setattr(ingest, "_office_to_pdf", lambda filename, raw: b"%PDF-1.7 converted")
    blocks = build_content_block("deck.pptx", b"PK\x03\x04")
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(blocks[0]["source"]["data"]) == b"%PDF-1.7 converted"
    # Provenance keeps the original office filename, not a .pdf rename.
    assert "deck.pptx" in blocks[-1]["text"]


def test_office_without_libreoffice_raises_convert_message(monkeypatch):
    monkeypatch.setattr(ingest, "_find_soffice", lambda: None)
    with pytest.raises(IngestError) as exc:
        build_content_block("report.docx", b"PK\x03\x04")
    assert "LibreOffice is not installed" in str(exc.value)


def test_unknown_extension_is_rejected():
    with pytest.raises(IngestError):
        build_content_block("archive.zip", b"PK\x03\x04")
