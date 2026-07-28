"""Template-based pptx rendering: build the deck onto an uploaded template's layouts."""

import io

from pptx import Presentation

from sf_agent.export import answer_to_document, render_document
from sf_agent.export.render_pptx import _SLIDE_W

_DECK = {
    "answer": "Retail capabilities deck.",
    "values": {
        "slides": [
            {"title": "Retail Wins", "content": ["Albertsons UI", "Walmart marketing"],
             "speaker_notes": "lead with the wins"},
            {"title": "The Ask", "content": ["Partner with BayOne"]},
        ]
    },
}


def _template_bytes() -> bytes:
    """A stand-in template: python-pptx's built-in deck (has Title + Title-and-Content
    layouts, and its own slide size distinct from our widescreen default)."""
    buf = io.BytesIO()
    Presentation().save(buf)
    return buf.getvalue()


def test_template_deck_inherits_template_size_and_populates_slides():
    tpl = _template_bytes()
    tpl_width = Presentation(io.BytesIO(tpl)).slide_width  # the template's own size

    model = answer_to_document(_DECK, "Costco retail deck")
    data = render_document(model, "pptx", template=tpl)

    prs = Presentation(io.BytesIO(data))
    assert prs.slide_width == tpl_width  # template's size governs, not our widescreen override
    slides = list(prs.slides)
    assert len(slides) == 3  # title + 2 content slides (the template's sample slides removed)

    # First content slide: title + bullets landed in the layout's placeholders.
    texts = [sh.text for sh in slides[1].shapes if sh.has_text_frame]
    assert any("Retail Wins" in t for t in texts)
    assert any("Albertsons UI" in t for t in texts)
    # Speaker notes carried onto the notes page.
    assert slides[1].has_notes_slide
    assert "lead with the wins" in slides[1].notes_slide.notes_text_frame.text


def test_default_path_unchanged_without_template():
    model = answer_to_document(_DECK, "deck")
    data = render_document(model, "pptx")  # no template
    prs = Presentation(io.BytesIO(data))
    assert prs.slide_width == int(_SLIDE_W)  # our widescreen default
    assert len(list(prs.slides)) >= 3


def test_template_render_reopens_and_is_valid():
    model = answer_to_document(_DECK, "deck")
    data = render_document(model, "pptx", template=_template_bytes())
    assert data[:2] == b"PK"
    # re-open must succeed
    assert len(list(Presentation(io.BytesIO(data)).slides)) == 3
