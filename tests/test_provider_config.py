"""Provider switch: ANTHROPIC_BASE_URL routes the agent to Anthropic or Z.AI GLM.

The loop uses the anthropic SDK against any Anthropic-Messages-compatible endpoint, so
pointing it at Z.AI is a base-URL + model change, no code fork. These pin that the base URL
flows from config into the client, and that the cost estimate tracks the model family.
"""

import pytest

from sf_agent.agent import _ANTHROPIC_PRICE, _GLM_PRICE, SnowflakeAgent, _price_for_model
from sf_agent.config import AgentConfig


@pytest.fixture(autouse=True)
def _clear_base_url_env(monkeypatch):
    # pydantic-settings reads real env vars regardless of _env_file, and this machine
    # exports ANTHROPIC_BASE_URL. Clear it so the tests exercise config values, not the host.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


def _cfg(**overrides) -> AgentConfig:
    # _env_file=None keeps the test hermetic; populate_by_name lets us pass field names.
    return AgentConfig(_env_file=None, **{"api_key": "test-key", **overrides})


def test_base_url_defaults_to_none() -> None:
    assert _cfg().base_url is None


def test_base_url_reads_value() -> None:
    assert _cfg(base_url="https://api.z.ai/api/anthropic").base_url == "https://api.z.ai/api/anthropic"


def test_blank_base_url_is_none() -> None:
    # An empty ANTHROPIC_BASE_URL means "use the default endpoint", not an empty URL.
    assert _cfg(base_url="   ").base_url is None


def test_price_table_by_model_family() -> None:
    assert _price_for_model("glm-5.2") is _GLM_PRICE
    assert _price_for_model("glm-5.2[1m]") is _GLM_PRICE
    assert _price_for_model("GLM-4.6") is _GLM_PRICE
    assert _price_for_model("claude-sonnet-4-6") is _ANTHROPIC_PRICE
    # GLM output is materially cheaper — the whole reason for the switch.
    assert _GLM_PRICE["output"] < _ANTHROPIC_PRICE["output"]


def test_client_routes_to_zai_when_base_url_set() -> None:
    agent = SnowflakeAgent(tools=[], config=_cfg(base_url="https://api.z.ai/api/anthropic", model="glm-5.2"))
    assert "z.ai" in str(agent._client.base_url)


def test_client_defaults_to_anthropic(monkeypatch) -> None:
    # With no base URL configured and none in the environment, the SDK uses Anthropic.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    agent = SnowflakeAgent(tools=[], config=_cfg())
    assert "anthropic.com" in str(agent._client.base_url)


# --- document-vision lane -------------------------------------------------------------
def test_vision_lane_disabled_by_default() -> None:
    cfg = _cfg()
    assert cfg.vision_enabled is False
    assert cfg.vision_api_key is None


def test_vision_lane_enables_with_key() -> None:
    cfg = _cfg(vision_api_key="sk-ant-test", vision_model="claude-sonnet-4-6")
    assert cfg.vision_enabled is True
    assert cfg.vision_base_url is None  # blank -> Anthropic default


def test_blank_vision_key_stays_disabled() -> None:
    assert _cfg(vision_api_key="   ").vision_enabled is False


def test_needs_vision_routes_documents_not_text() -> None:
    from sf_agent.ingest import needs_vision

    # Need provider-side document/image vision.
    for name in ("cs.pdf", "deck.pptx", "report.docx", "chart.png", "scan.JPEG", "old.xls"):
        assert needs_vision(name) is True, name
    # Readable by any model as plain text -> stay on the cheap main provider.
    for name in ("data.csv", "notes.md", "grid.xlsx", "sheet.xlsm", "page.html", "x.json"):
        assert needs_vision(name) is False, name
