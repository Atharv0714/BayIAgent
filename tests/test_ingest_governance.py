"""The three data tiers, enforced at the HTTP layer.

General/shared is readable by everyone, private only by the person who ingested it, and
protected only by members of the protected tier — who are also the only people who may
author it. Snowflake's ``rap_ownership`` policy enforces the READ side; these tests cover
the WRITE side and the in-flight preview, which is where the app is the only guard.

Every existing governance test is a unit test of a helper. Nothing exercised the endpoints
themselves, so a signature change or a reordered check could have removed a gate without
failing anything.
"""

import asyncio
import io
import json

import pytest
from fastapi import Request, UploadFile

from sf_agent.config import AuthConfig
from sf_agent.ingest import StructuredResult
from sf_agent.web import (
    STATE,
    CommitRequest,
    EditRequest,
    ingest_commit,
    ingest_draft_delete,
    ingest_draft_get,
    ingest_edit,
    ingest_structure,
    whoami,
)

MEMBER = "member@bayone.com"
OUTSIDER = "outsider@bayone.com"
GROUP_ID = "11111111-2222-3333-4444-555555555555"


def _auth(**overrides) -> AuthConfig:
    """Auth settings pinned for the test, so results never depend on a local .env."""
    base = {
        "enforce_ownership": True,
        "dev_caller_identity": None,
        "dev_caller_groups": None,
        "protected_users": MEMBER,
        "protected_group": GROUP_ID,
    }
    base.update(overrides)
    return AuthConfig(**base)


@pytest.fixture(autouse=True)
def _pinned_state():
    previous = (STATE.auth_config, STATE.anthropic_client, STATE.agent_config)
    STATE.auth_config = _auth()
    # /structure returns 503 before it looks at the tier when no model is configured, so
    # the gates below are unreachable without these. Sentinels are enough: every tier
    # rejection happens before the model is called, and no test here reaches structuring.
    STATE.anthropic_client = STATE.anthropic_client or object()
    STATE.agent_config = STATE.agent_config or object()
    for store in (STATE.pending_ingests, STATE.pending_meta, STATE.pending_owners):
        store.clear()
    yield
    STATE.auth_config, STATE.anthropic_client, STATE.agent_config = previous
    for store in (STATE.pending_ingests, STATE.pending_meta, STATE.pending_owners):
        store.clear()


def _request(identity: str | None = None, groups: str | None = None) -> Request:
    headers = []
    if identity:
        headers.append((b"x-ms-client-principal-name", identity.encode()))
    if groups:
        headers.append((b"x-ms-client-groups", groups.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


def _structure(identity: str | None, sensitivity: str):
    """Call /api/ingest/structure the way the browser does."""
    return asyncio.run(
        ingest_structure(
            _request(identity),
            file=UploadFile(file=io.BytesIO(b"BayOne retail brief"), filename="brief.txt"),
            save_draft=False,
            sensitivity=sensitivity,
        )
    )


def _seed_pending(ingest_id: str, owner: str | None, tier: str = "protected"):
    """A pending ingest as /structure would leave it, owned by `owner`."""
    STATE.pending_ingests[ingest_id] = StructuredResult(
        manifest={"source_file": "brief.txt"},
        blocks=[{"block_index": 0, "text": "confidential", "sensitivity": tier}],
        facts=[],
    )
    STATE.pending_meta[ingest_id] = {"ingested_by": owner}
    STATE.pending_owners[ingest_id] = owner


# --- who counts as a member -----------------------------------------------------------

def test_the_allowlist_grants_protected_membership():
    assert _body(whoami(_request(MEMBER)))["is_protected_member"] is True


def test_someone_not_on_the_allowlist_is_not_a_member():
    assert _body(whoami(_request(OUTSIDER)))["is_protected_member"] is False


def test_membership_survives_a_difference_in_upn_casing():
    """Entra does not guarantee UPN casing between tokens, and a case difference silently
    costing someone their access would be near-impossible to diagnose from outside.

    Both sides have to be normalised: the identity that arrives on the request AND the
    value someone typed into the app setting. Testing only the first passes even with the
    config side left case-sensitive.
    """
    assert _body(whoami(_request(MEMBER.upper())))["is_protected_member"] is True

    STATE.auth_config = _auth(protected_users="Member@BayOne.Com")
    assert _body(whoami(_request(MEMBER)))["is_protected_member"] is True
    assert _body(whoami(_request(MEMBER.upper())))["is_protected_member"] is True


def test_an_anonymous_caller_is_never_a_member():
    assert _body(whoami(_request(None)))["is_protected_member"] is False


def test_the_entra_group_still_grants_membership_when_configured():
    """The allowlist is additive — moving to a real group later must not need a code change."""
    STATE.auth_config = _auth(protected_users=None, groups_header="X-MS-CLIENT-GROUPS")
    assert _body(whoami(_request(OUTSIDER, groups=GROUP_ID)))["is_protected_member"] is True
    assert _body(whoami(_request(OUTSIDER)))["is_protected_member"] is False


def test_whoami_reports_enforcement_so_the_ui_can_hide_the_tiers():
    """With enforcement off the ingest page hides the whole tier panel, so this flag is
    what makes private/protected reachable at all."""
    assert _body(whoami(_request(MEMBER)))["enforce"] is True
    STATE.auth_config = _auth(enforce_ownership=False)
    assert _body(whoami(_request(MEMBER)))["enforce"] is False


# --- who may AUTHOR protected data ----------------------------------------------------

def test_a_non_member_cannot_ingest_into_the_protected_tier():
    resp = _structure(OUTSIDER, "protected")
    assert resp.status_code == 403
    assert "protected-data group" in _body(resp)["error"]


def test_a_non_member_cannot_ingest_protected_by_reclassifying_rows():
    """The other way in: structure as internal, then set a row to protected via /edit."""
    _seed_pending("i1", OUTSIDER, tier="internal")
    resp = ingest_edit(
        EditRequest(
            ingest_id="i1",
            blocks=[{"block_index": 0, "text": "x", "sensitivity": "protected"}],
            facts=[],
        ),
        _request(OUTSIDER),
    )
    assert resp.status_code == 403


def test_a_confidential_tier_requires_a_signed_in_identity():
    """Private and protected rows must be owned, or the policy has nothing to match on."""
    assert _structure(None, "private").status_code == 401


def test_an_unknown_tier_is_rejected_outright():
    assert _structure(MEMBER, "top-secret").status_code == 400


# --- an in-flight preview belongs to whoever started it --------------------------------
# /edit and /instruct return the full blocks and facts, so without an owner check a second
# signed-in user holding an ingest_id could read another user's PROTECTED payload before it
# ever reaches Snowflake — around the row-access policy entirely.

def test_another_user_cannot_read_a_pending_protected_preview():
    _seed_pending("i1", MEMBER)
    resp = ingest_edit(
        EditRequest(ingest_id="i1", blocks=[{"block_index": 0, "text": "x"}], facts=[]),
        _request(OUTSIDER),
    )
    assert resp.status_code == 404
    assert "confidential" not in bytes(resp.body).decode()


def test_another_user_cannot_commit_someone_elses_pending_ingest():
    _seed_pending("i1", MEMBER)
    resp = ingest_commit(CommitRequest(ingest_id="i1"), _request(OUTSIDER))
    assert resp.status_code == 404
    assert "i1" in STATE.pending_ingests, "the payload was consumed by the wrong user"


def test_another_user_cannot_delete_someone_elses_draft():
    _seed_pending("i1", MEMBER)
    resp = ingest_draft_delete("i1", _request(OUTSIDER))
    assert resp.status_code == 404
    assert "i1" in STATE.pending_ingests


def test_another_user_cannot_fetch_someone_elses_draft():
    _seed_pending("i1", MEMBER)
    assert ingest_draft_get("i1", _request(OUTSIDER)).status_code == 404


def test_the_owner_still_reaches_their_own_pending_ingest():
    _seed_pending("i1", MEMBER)
    # Not 404 — it may fail later for unrelated reasons (no ingest connection), but the
    # ownership gate must not be what stops them.
    assert ingest_commit(CommitRequest(ingest_id="i1"), _request(MEMBER)).status_code != 404
    assert ingest_draft_delete("i1", _request(MEMBER)).status_code == 200
    assert "i1" not in STATE.pending_owners, "owner entry outlived the payload"


def test_an_unowned_pending_ingest_stays_open():
    """Structured with no identity resolved — a deployment with no sign-in must be
    unaffected, exactly as for conversations and uploads."""
    _seed_pending("i1", None, tier="internal")
    assert ingest_draft_delete("i1", _request(None)).status_code == 200


# --- an unresolved identity degrades, and must not inherit the previous caller ---------
# With enforcement on, a request whose identity cannot be resolved is served with the
# shared tier only rather than refused: Easy Auth already blocks anonymous visitors at the
# platform, so this branch means "authenticated, header not arriving" — a misconfiguration
# where a 401 would take the app down for everyone at once.
#
# The danger is the shared read connection. One connection serves every request, so
# skipping the bind would leave the PREVIOUS caller's BAYI_CALLER in place and hand their
# private rows to somebody the app could not even identify. Degrading has to CLEAR the
# variables, not leave them alone.

class _RecordingConn:
    """Stands in for the shared Snowflake connection, recording what gets bound."""

    def __init__(self):
        self.binds: list[dict] = []

    def bind_session(self, variables):
        self.binds.append(dict(variables))


def _ask_binds(identity, monkeypatch):
    """Run /api/ask far enough to capture the session bind, then stop."""
    from sf_agent import web

    conn = _RecordingConn()
    monkeypatch.setattr(web.STATE, "connection", conn)
    # Fail the turn immediately after the bind: the agent is irrelevant to what we assert.
    monkeypatch.setitem(
        web.STATE.agents, "run_sql",
        type("A", (), {"route_and_answer": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop"))})(),
    )
    web.ask(web.AskRequest(question="anything", tool="run_sql"), _request(identity))
    return conn.binds


def test_an_unresolved_identity_clears_the_session_rather_than_inheriting_it(monkeypatch):
    binds = _ask_binds(None, monkeypatch)
    assert binds, "no bind happened at all — the next query would inherit the last caller"
    assert binds[-1] == {"BAYI_CALLER": None, "BAYI_PROTECTED": "false"}


def test_a_resolved_identity_binds_that_caller(monkeypatch):
    assert _ask_binds(OUTSIDER, monkeypatch)[-1] == {
        "BAYI_CALLER": OUTSIDER, "BAYI_PROTECTED": "false"
    }


def test_a_protected_member_binds_the_protected_flag(monkeypatch):
    assert _ask_binds(MEMBER, monkeypatch)[-1] == {
        "BAYI_CALLER": MEMBER, "BAYI_PROTECTED": "true"
    }


def test_an_unresolved_identity_is_never_treated_as_a_protected_member(monkeypatch):
    """Belt and braces: shared-only must not accidentally mean group-scoped rows too."""
    STATE.auth_config = _auth(protected_users=None, dev_caller_groups=GROUP_ID)
    assert _ask_binds(None, monkeypatch)[-1]["BAYI_PROTECTED"] == "false"


# --- the diagnostic that makes a UPN mismatch findable ---------------------------------

def test_whoami_explains_why_someone_is_or_is_not_a_member():
    """A protected tier that silently does not work is a mystery; the likeliest cause is
    PROTECTED_USERS holding a different string than the UPN Easy Auth actually sends."""
    assert _body(whoami(_request(MEMBER)))["protected_via"] == "allowlist"
    assert "did not match" in _body(whoami(_request(OUTSIDER)))["protected_via"]
    assert _body(whoami(_request(None)))["protected_via"] == "no identity resolved"

    STATE.auth_config = _auth(protected_users=None, groups_header="X-MS-CLIENT-GROUPS")
    assert _body(whoami(_request(OUTSIDER, groups=GROUP_ID)))["protected_via"] == "group"


def test_whoami_does_not_echo_the_allowlist():
    """A caller learns their OWN standing, not who else is on the list."""
    body = _body(whoami(_request(OUTSIDER)))
    assert MEMBER not in json.dumps(body)
