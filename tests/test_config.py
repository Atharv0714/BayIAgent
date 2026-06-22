import pytest
from pydantic import ValidationError

from sf_agent.config import SnowflakeConfig

BASE = dict(
    account="acct",
    user="usr",
    warehouse="wh",
    database="db",
    schema_name="sch",
    role="ro_role",
)


def _make(**overrides) -> SnowflakeConfig:
    # _env_file=None keeps the test hermetic (ignore any real .env on disk).
    return SnowflakeConfig(_env_file=None, **{**BASE, **overrides})


def test_password_only_is_valid() -> None:
    cfg = _make(password="secret")
    assert cfg.password == "secret"
    assert cfg.private_key_path is None


def test_key_pair_only_is_valid() -> None:
    cfg = _make(private_key_path="/keys/rsa_key.p8")
    assert cfg.private_key_path == "/keys/rsa_key.p8"
    assert cfg.password is None


def test_no_auth_method_raises() -> None:
    with pytest.raises(ValidationError):
        _make()


def test_both_auth_methods_raise() -> None:
    with pytest.raises(ValidationError):
        _make(password="secret", private_key_path="/keys/rsa_key.p8")


def test_blank_auth_values_count_as_unset() -> None:
    # Blank password + real key path -> key-pair, not "both set".
    cfg = _make(password="   ", private_key_path="/keys/rsa_key.p8")
    assert cfg.password is None
    assert cfg.private_key_path == "/keys/rsa_key.p8"
