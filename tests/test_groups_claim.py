"""Tests for the Easy Auth groups-claim decoder added for the Azure deployment.

App Service Easy Auth does not emit a comma-separated groups header; it emits
X-MS-CLIENT-PRINCIPAL, base64 JSON holding every claim. These tests pin the
decode, the regression path for a plain comma-separated header, and — most
importantly — that an unreadable claim fails CLOSED.
"""

import base64
import json

import pytest
from starlette.requests import Request

from sf_agent.config import AuthConfig
from sf_agent.web import STATE, _caller_groups, _caller_is_protected, _principal_groups

GROUP_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"


def _principal(claims: list[dict]) -> str:
    """Encode claims the way Easy Auth does."""
    payload = {"auth_typ": "aad", "claims": claims, "name_typ": "name", "role_typ": "roles"}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


@pytest.fixture(autouse=True)
def _auth_config(monkeypatch):
    """Point the app at the principal header, with the protected group as an object ID."""
    cfg = AuthConfig(
        ENFORCE_OWNERSHIP=True,
        GROUPS_HEADER="X-MS-CLIENT-PRINCIPAL",
        PROTECTED_GROUP=GROUP_ID,
    )
    monkeypatch.setattr(STATE, "auth_config", cfg)
    return cfg


def test_extracts_group_object_ids():
    header = _principal([
        {"typ": "groups", "val": GROUP_ID},
        {"typ": "groups", "val": OTHER_ID},
        {"typ": "name", "val": "atharv@example.com"},
    ])
    assert _principal_groups(header) == [GROUP_ID, OTHER_ID]


def test_accepts_full_uri_claim_type():
    """Some token configurations emit the long-form claim URI."""
    header = _principal([
        {"typ": "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups", "val": GROUP_ID},
    ])
    assert _principal_groups(header) == [GROUP_ID]


def test_unpadded_base64_still_decodes():
    """Easy Auth strips '=' padding in some runtimes."""
    header = _principal([{"typ": "groups", "val": GROUP_ID}]).rstrip("=")
    assert _principal_groups(header) == [GROUP_ID]


@pytest.mark.parametrize("overage_claim", ["_claim_names", "_claim_sources", "hasgroups"])
def test_overage_raises(overage_claim):
    """Too many groups -> Entra drops the claim. Membership is UNKNOWN, not empty."""
    header = _principal([
        {"typ": overage_claim, "val": '{"groups":"src1"}'},
        {"typ": "name", "val": "atharv@example.com"},
    ])
    with pytest.raises(ValueError, match="overage"):
        _principal_groups(header)


def test_overage_fails_closed_at_request_level(caplog):
    """The whole point: an over-grouped admin must NOT be handed protected data."""
    header = _principal([{"typ": "_claim_names", "val": '{"groups":"src1"}'}])
    request = _request({"X-MS-CLIENT-PRINCIPAL": header})
    assert _caller_groups(request) == []
    assert _caller_is_protected(request) is False
    assert "could not read group claims" in caplog.text


def test_malformed_header_fails_closed():
    request = _request({"X-MS-CLIENT-PRINCIPAL": "not-valid-base64-!!!"})
    assert _caller_groups(request) == []
    assert _caller_is_protected(request) is False


def test_protected_membership_matches_object_id():
    header = _principal([{"typ": "groups", "val": GROUP_ID}])
    assert _caller_is_protected(_request({"X-MS-CLIENT-PRINCIPAL": header})) is True


def test_non_member_is_not_protected():
    header = _principal([{"typ": "groups", "val": OTHER_ID}])
    assert _caller_is_protected(_request({"X-MS-CLIENT-PRINCIPAL": header})) is False


def test_plain_comma_header_still_works(monkeypatch):
    """Regression: the SharePoint/M365 contract (plain list) must be unchanged."""
    monkeypatch.setattr(
        STATE,
        "auth_config",
        AuthConfig(GROUPS_HEADER="X-MS-CLIENT-GROUPS", PROTECTED_GROUP="SG-BayI-Sensitive"),
    )
    request = _request({"X-MS-CLIENT-GROUPS": "SG-Other, SG-BayI-Sensitive "})
    assert _caller_groups(request) == ["SG-Other", "SG-BayI-Sensitive"]
    assert _caller_is_protected(request) is True


def test_absent_header_falls_back_to_dev_list(monkeypatch):
    monkeypatch.setattr(
        STATE,
        "auth_config",
        AuthConfig(GROUPS_HEADER="X-MS-CLIENT-PRINCIPAL", DEV_CALLER_GROUPS="SG-Local"),
    )
    assert _caller_groups(_request({})) == ["SG-Local"]
