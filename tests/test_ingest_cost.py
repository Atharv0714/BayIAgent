"""Regression: structure_upload must meter its own cost without a live model call.

The cost line in structure_upload calls agent._estimate_cost(usage, model). When that helper
gained its `model` argument (provider-aware pricing) this call site was a separate module and
was missed, breaking every ingest at runtime. This stubs the streaming client so the whole
structure_upload path — including the cost metering — runs offline and would fail loudly if the
signature drifts again.
"""

from types import SimpleNamespace

from sf_agent.agent import _estimate_cost
from sf_agent.config import AgentConfig
from sf_agent.ingest import structure_upload

_JSON = '{"manifest": {"source_file": "x.txt", "block_count": 0, "fact_count": 0}, "blocks": [], "facts": []}'


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._resp


def _fake_client(usage):
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=_JSON)],
        stop_reason="end_turn",
        usage=usage,
    )
    return SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _FakeStream(resp)))


def _cfg(model="claude-sonnet-4-6") -> AgentConfig:
    return AgentConfig(_env_file=None, api_key="test-key", model=model)


def test_structure_upload_meters_cost_offline():
    usage = SimpleNamespace(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    result = structure_upload(_fake_client(usage), _cfg(), "x.txt", b"hello")
    # Exercises the cost line: it must run and match the model-aware estimate.
    expected = _estimate_cost({"input": 1_000_000, "output": 1_000_000}, "claude-sonnet-4-6")
    assert result.cost_usd == expected == round(3.0 + 15.0, 6)
    assert result.tokens["total"] == 2_000_000


def test_structure_upload_cost_tracks_model_family():
    usage = SimpleNamespace(
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    glm = structure_upload(_fake_client(usage), _cfg(model="glm-5.2"), "x.txt", b"hello")
    # GLM is cheaper — the ingest cost readout must follow the configured provider.
    assert glm.cost_usd == round(0.60 + 2.20, 6)
