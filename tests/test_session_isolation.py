"""One user must not reach another user's chat session or uploaded file.

Conversation history and chat uploads live in process memory keyed by a random id. Nothing
checked that the caller owned that id, so a second user replaying somebody's session_id got
their history back — including warehouse rows the row-access policy had released to THEM,
which is how a conversation replay slips past row-level security. /api/reset was worse than
a read: any holder of a session id could wipe another user's chat memory.

The ids are UUID4 and never appear in a URL, so this was never remotely enumerable. It
matters because isolation rested on a secret token while the app already knew who was
calling, so any leak of an id became a read of someone else's conversation.
"""

import asyncio
import io
import json

import pytest
from fastapi import Request, UploadFile

from sf_agent.config import AuthConfig
from sf_agent.web import (
    STATE,
    AskRequest,
    ExportRequest,
    export,
    reset,
    upload,
)

ALICE = "alice@bayone.com"
BOB = "bob@bayone.com"


@pytest.fixture(autouse=True)
def _clean_state():
    stores = (STATE.uploads, STATE.upload_owners, STATE.conversations, STATE.conversation_owners)
    for store in stores:
        store.clear()
    # Pin the auth settings rather than inheriting whatever .env the developer has: a local
    # DEV_CALLER_IDENTITY would otherwise make every "anonymous" request here signed-in.
    previous = STATE.auth_config
    STATE.auth_config = AuthConfig(dev_caller_identity=None)
    yield
    STATE.auth_config = previous
    for store in stores:
        store.clear()


def _request(identity: str | None = None) -> Request:
    """A request carrying (or lacking) an Easy Auth identity header."""
    headers = [(b"x-ms-client-principal-name", identity.encode())] if identity else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def _pptx_bytes() -> bytes:
    from pptx import Presentation

    buf = io.BytesIO()
    Presentation().save(buf)
    return buf.getvalue()


def _upload_as(identity: str | None, filename="brand.pptx", data: bytes | None = None):
    body = asyncio.run(
        upload(
            _request(identity),
            UploadFile(file=io.BytesIO(data or _pptx_bytes()), filename=filename),
        )
    )
    return json.loads(bytes(body.body))["upload_id"]


def _seed_conversation(session_id: str, owner: str | None, marker="PLATINUM-FALCON"):
    """A conversation as converse() would leave it, owned by `owner`."""
    STATE.conversations[session_id] = [
        {"role": "user", "content": f"Remember the code word {marker}."},
        {"role": "assistant", "content": [{"type": "text", "text": f'{{"answer": "{marker}"}}'}]},
    ]
    STATE.conversation_owners[session_id] = owner


def _export_req(template_id: str) -> ExportRequest:
    return ExportRequest(
        format="pptx",
        question="Make a deck",
        answer={"answer": "x", "values": {"slides": [{"title": "A", "content": ["b"]}]}},
        template_id=template_id,
    )


# --- a signed-in user's resources are theirs alone -------------------------------------

def test_another_user_cannot_reset_a_session_they_do_not_own():
    """Destructive and unauthenticated-by-id: Bob could wipe Alice's chat memory mid-chat."""
    _seed_conversation("alice-session", ALICE)

    resp = reset(AskRequest(question="", session_id="alice-session"), _request(BOB))

    assert resp.status_code == 404
    assert "alice-session" in STATE.conversations, "Bob's reset destroyed Alice's history"


def test_the_owner_can_still_reset_their_own_session():
    _seed_conversation("alice-session", ALICE)

    resp = reset(AskRequest(question="", session_id="alice-session"), _request(ALICE))

    assert resp.status_code == 200
    assert "alice-session" not in STATE.conversations
    assert "alice-session" not in STATE.conversation_owners, "owner entry leaked after reset"


def test_another_user_cannot_use_someone_elses_deck_as_a_template():
    upload_id = _upload_as(ALICE)

    resp = export(_export_req(upload_id), _request(BOB))

    assert resp.status_code == 404


def test_the_owner_can_still_export_onto_their_own_template():
    upload_id = _upload_as(ALICE)

    resp = export(_export_req(upload_id), _request(ALICE))

    assert resp.status_code == 200
    assert bytes(resp.body)[:2] == b"PK"


def test_a_missing_owner_is_indistinguishable_from_a_missing_id():
    """404 rather than 403 on purpose: a 403 confirms the id is real, which tells someone
    holding a leaked id that they found something."""
    _seed_conversation("alice-session", ALICE)

    denied = reset(AskRequest(question="", session_id="alice-session"), _request(BOB))
    unknown = reset(AskRequest(question="", session_id="no-such-session"), _request(BOB))

    assert denied.status_code == 404
    # An unknown id was never an error, and must not become one — that would be a probe.
    assert unknown.status_code == 200


def test_an_anonymous_caller_cannot_reach_a_signed_in_users_session():
    """Dropping the identity header must not read as "unowned"."""
    _seed_conversation("alice-session", ALICE)

    resp = reset(AskRequest(question="", session_id="alice-session"), _request(None))

    assert resp.status_code == 404
    assert "alice-session" in STATE.conversations


# --- nothing changes where there is no identity ----------------------------------------

def test_with_no_identity_resolved_everything_stays_open():
    """ENFORCE_OWNERSHIP is false by default and many deployments front the app with no
    sign-in at all. Unowned resources must behave exactly as they did before."""
    _seed_conversation("shared-session", None)
    upload_id = _upload_as(None)
    assert STATE.upload_owners[upload_id] is None

    assert reset(AskRequest(question="", session_id="shared-session"), _request(None)).status_code == 200
    assert export(_export_req(upload_id), _request(None)).status_code == 200


def test_an_unowned_resource_is_open_even_to_a_signed_in_caller():
    """A session created before sign-in existed must not become unreachable."""
    _seed_conversation("legacy-session", None)

    assert reset(AskRequest(question="", session_id="legacy-session"), _request(ALICE)).status_code == 200


# --- the owner is fixed at creation ------------------------------------------------------

def test_upload_records_its_creator():
    assert STATE.upload_owners[_upload_as(ALICE)] == ALICE
    assert STATE.upload_owners[_upload_as(BOB, "b.pptx")] == BOB


def test_a_later_turn_cannot_re_stamp_an_existing_owner():
    """_remember_conversation uses setdefault, so a session started while signed in never
    becomes claimable — otherwise the second request would simply overwrite the owner."""
    from sf_agent.web import _remember_conversation

    _remember_conversation("s1", [{"role": "user", "content": "hi"}], ALICE)
    _remember_conversation("s1", [{"role": "user", "content": "hi again"}], BOB)

    assert STATE.conversation_owners["s1"] == ALICE


def test_eviction_does_not_leave_an_orphan_owner_behind():
    """The owner map must not outgrow the store it describes, or a recycled id could
    inherit a stale owner."""
    from sf_agent.web import _MAX_CONVERSATIONS, _remember_conversation

    for i in range(_MAX_CONVERSATIONS + 25):
        _remember_conversation(f"s{i}", [{"role": "user", "content": "x"}], ALICE)

    assert len(STATE.conversations) == _MAX_CONVERSATIONS
    assert len(STATE.conversation_owners) == _MAX_CONVERSATIONS
    assert set(STATE.conversation_owners) == set(STATE.conversations)


def test_upload_eviction_also_clears_its_owner():
    from sf_agent.web import _MAX_UPLOADS, _remember_upload

    for i in range(_MAX_UPLOADS + 5):
        _remember_upload(f"u{i}", {"filename": f"{i}.txt", "kind": "context"}, ALICE)

    assert set(STATE.upload_owners) == set(STATE.uploads)
