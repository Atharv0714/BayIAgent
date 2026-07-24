"""The vocabulary-registry read must not inherit a prior caller's identity.

`read_registry` runs on the shared read connection without binding an identity. Once the
row-access policy is live, an unbound read inherits whatever BAYI_CALLER the previous
/api/ask left set — so it would read through that user's private/protected rows. These
tests pin that `_scope_read_internal_only` fails the connection closed to internal-only
(caller UNSET, protected='false') using the configured session-variable names, before the
registry is read. Pure unit test — no live warehouse.
"""

from sf_agent.config import AuthConfig
from sf_agent.web import STATE, _scope_read_internal_only


class _FakeConn:
    """Records bind_session calls so we can assert the fail-closed scoping."""

    def __init__(self) -> None:
        self.binds: list[dict[str, str | None]] = []

    def bind_session(self, variables: dict[str, str | None]) -> None:
        self.binds.append(dict(variables))


def test_scope_binds_caller_unset_and_protected_false(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(STATE, "connection", conn)
    monkeypatch.setattr(
        STATE,
        "auth_config",
        AuthConfig(CALLER_SESSION_VAR="BAYI_CALLER", PROTECTED_SESSION_VAR="BAYI_PROTECTED"),
    )

    _scope_read_internal_only()

    assert conn.binds == [{"BAYI_CALLER": None, "BAYI_PROTECTED": "false"}]


def test_scope_honors_configured_variable_names(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(STATE, "connection", conn)
    monkeypatch.setattr(
        STATE,
        "auth_config",
        AuthConfig(CALLER_SESSION_VAR="CUSTOM_CALLER", PROTECTED_SESSION_VAR="CUSTOM_PROT"),
    )

    _scope_read_internal_only()

    assert conn.binds == [{"CUSTOM_CALLER": None, "CUSTOM_PROT": "false"}]


def test_scope_is_noop_without_connection(monkeypatch):
    monkeypatch.setattr(STATE, "connection", None)
    monkeypatch.setattr(STATE, "auth_config", AuthConfig())
    # Must not raise when there is no connection to scope.
    _scope_read_internal_only()
