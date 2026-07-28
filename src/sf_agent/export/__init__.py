"""Native document export of the query agent's grounded answers.

The agent already returns a structured, auditable answer — a headline ``value``, a
plain-language ``summary``, supporting ``values`` (lists / tables / key-value objects),
and an optional ``chart`` spec. That structure is exactly what a report needs, so this
package turns one answer into a real, native Office/PDF file (not a rasterized
screenshot): xlsx, docx, pptx, or pdf.

The mapping lives in ``model`` (answer -> ``DocumentModel``); each ``render_*`` reads that
one model, so the four formats stay consistent and a new format only adds a renderer.
"""

from __future__ import annotations

from typing import Callable

from sf_agent.export.intent import detect_export_request
from sf_agent.export.model import DocumentModel, Table, answer_to_document, humanize

# fmt -> (file extension, MIME type). Drives both the renderer dispatch and the
# download response headers.
FORMATS: dict[str, tuple[str, str]] = {
    "xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "pptx": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "pdf": ("pdf", "application/pdf"),
}

SUPPORTED_FORMATS = tuple(FORMATS)


def render_document(model: DocumentModel, fmt: str, template: bytes | None = None) -> bytes:
    """Render ``model`` to the requested format, returning the file's bytes.

    Renderers are imported lazily so the package (and the web app) still load when a
    single format's optional dependency is missing — only that format then fails.

    ``template`` (a .pptx/.potx the user uploaded) is forwarded only to the pptx renderer,
    which builds the deck onto that template's theme and layouts; other formats ignore it.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unsupported export format {fmt!r}")
    renderer = _renderer(fmt)
    if fmt == "pptx":
        return renderer(model, template=template)
    return renderer(model)


def _renderer(fmt: str) -> Callable[[DocumentModel], bytes]:
    if fmt == "xlsx":
        from sf_agent.export.render_xlsx import render as r
    elif fmt == "docx":
        from sf_agent.export.render_docx import render as r
    elif fmt == "pptx":
        from sf_agent.export.render_pptx import render as r
    else:
        from sf_agent.export.render_pdf import render as r
    return r


__all__ = [
    "DocumentModel",
    "Table",
    "answer_to_document",
    "detect_export_request",
    "humanize",
    "render_document",
    "FORMATS",
    "SUPPORTED_FORMATS",
]
