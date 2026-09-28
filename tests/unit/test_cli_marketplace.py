"""Tests for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret
from src.graph.graph import MeetingCommitmentLedgerAgent

_REQUIRED_ENV = {
    "AZURE_OPENAI_API_KEY": "test-api-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
    "ASTAR_TENANT_ID": "tenant-1",
    "ASTAR_ORG_ID": "org-1",
}
_TRANSCRIPT = (
    "Meeting notes 2026-09-01. Send the revised quote to the client by Sept 15. "
    "We agreed to adopt the new pricing tier."
)


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.calls.append(messages)
        return {
            "content": json.dumps(
                {
                    "items": [
                        {
                            "type": "action",
                            "summary": "Send the revised quote to the client",
                            "owner": "",
                            "due": "Sept 15",
                            "evidence_span": "Send the revised quote to the client by Sept 15.",
                        },
                        {
                            "type": "decision",
                            "summary": "Adopt the new pricing tier",
                            "owner": "",
                            "due": "",
                            "evidence_span": "We agreed to adopt the new pricing tier.",
                        },
                    ]
                }
            ),
            "tool_calls": [],
            "model": "fake",
        }

    def stream(self, messages: list[Any]):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list[Any]) -> "_FakeLLM":
        del tools
        return self


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _ctx() -> InvocationContext:
    return InvocationContext(
        session_id="marketplace-session",
        correlation_id="marketplace-correlation",
        caller_id="marketplace-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def test_runner_order_discards_history_calls_llm_and_keeps_ledger_read_only(monkeypatch):
    _set_required_env(monkeypatch)
    fake = _FakeLLM()
    agent = cli.MarketplaceGraph(config={"llm": fake})
    agent.compile()
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())

    result = agent.invoke(
        _TRANSCRIPT,
        ctx=_ctx(),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY_HISTORY"}]},
    )

    assert result["status"] == "success"
    assert result["output"]["commitment_brief"]["new"]
    assert result["output"]["degradation_reason"] == "empty_ledger+staging_unavailable"
    assert "llm_unavailable" not in result["output"]["degradation_reason"]
    assert len(fake.calls) >= 1
    assert "CANARY_HISTORY" not in json.dumps(result, ensure_ascii=False)
    assert agent._ledger._staging == {}


def test_runner_context_is_rebuilt_instead_of_merged(monkeypatch):
    _set_required_env(monkeypatch)
    captured: dict[str, Any] = {}
    captured_ctx: list[InvocationContext | None] = []

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input, session_id
        captured_ctx.append(ctx)
        captured.update(input_context or {})
        return {"status": "success", "output": input_context}

    monkeypatch.setattr(MeetingCommitmentLedgerAgent, "invoke", parent_invoke)
    agent = cli.MarketplaceGraph(config={"llm": _FakeLLM()})
    result = agent.invoke(
        _TRANSCRIPT,
        ctx=_ctx(),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY_HISTORY"}]},
    )

    assert "CANARY_HISTORY" not in json.dumps(result, ensure_ascii=False)
    assert set(captured) == {"mode", "tenant_id", "org_id", "as_of_utc", "meeting_meta"}
    assert captured["mode"] == "invoke"
    assert captured["as_of_utc"].endswith("+00:00")
    assert captured_ctx[0] is not None and captured_ctx[0].hitl_allowed is False


@pytest.mark.parametrize("missing_key", tuple(_REQUIRED_ENV))
def test_provision_secrets_fails_closed_for_each_missing_value(monkeypatch, missing_key):
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_key)
    agent = cli.MarketplaceGraph(config={"llm": _FakeLLM()})
    agent.compile()

    with pytest.raises(MissingSecret):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_env(monkeypatch)
    agent = cli.MarketplaceGraph(config={"llm": _FakeLLM()})
    agent.compile()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


@pytest.mark.parametrize("dirty_value", ("", " leading", "trailing ", "line\nbreak"))
def test_provision_secrets_rejects_unclean_values(monkeypatch, dirty_value):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ASTAR_TENANT_ID", dirty_value)
    agent = cli.MarketplaceGraph(config={"llm": _FakeLLM()})
    agent.compile()

    with pytest.raises(ValueError):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


def test_default_llm_and_read_only_ledger_are_injected():
    agent = cli.MarketplaceGraph()

    assert isinstance(agent._llm, cli._AzureMarketplaceLLM)
    assert isinstance(agent._ledger, cli._ReadOnlyLedgerStore)
    assert vars(agent._llm) == {}


def test_caller_injected_llm_is_preserved():
    fake = _FakeLLM()
    agent = cli.MarketplaceGraph(config={"llm": fake})

    assert agent._llm is fake


def test_pasted_ingest_json_remains_transcript_and_cannot_select_write_mode(monkeypatch):
    _set_required_env(monkeypatch)
    fake = _FakeLLM()
    agent = cli.MarketplaceGraph(config={"llm": fake})
    agent.compile()
    supplied = json.dumps({"mode": "ingest", "transcript": _TRANSCRIPT})

    result = agent.invoke(supplied, ctx=_ctx())

    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"
    assert result["output"]["commitment_brief"]["new"]
    assert agent._ledger._rows == {}
    assert agent._ledger._staging == {}


def test_main_passes_loaded_config_to_marketplace_runner(monkeypatch):
    loaded = {"recall_top_k": 4}
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "load_agent_config", lambda agent_dir: loaded)
    monkeypatch.setattr(cli, "run_agent_marketplace", lambda *args, **kwargs: captured.update(kwargs))

    cli.main()

    assert captured == {"agent_name": "CMN-C2-687", "namespace": "cmn", "config": loaded}


# ── production construction form ────────────────────────────────────────────────
# The tests above build the graph from a hand-written config dict. The runner does
# NOT: main() passes load_agent_config(<repo>) — the real config/config.yaml. A key
# that is present-but-null there makes `setdefault` a silent no-op, so the injection
# must be re-proved against the file the Pod actually loads.
def _production_config() -> dict[str, Any]:
    from pathlib import Path

    return cli.load_agent_config(Path(cli.__file__).resolve().parent)


def test_production_config_still_receives_llm_and_read_only_ledger():
    config = _production_config()
    assert config, "config/config.yaml did not load — the runner would build an unwired agent"
    agent = cli.MarketplaceGraph(config=config)
    assert isinstance(agent._llm, cli._AzureMarketplaceLLM)
    assert isinstance(agent._ledger, cli._ReadOnlyLedgerStore)


def test_production_config_invoke_forces_invoke_mode_and_writes_nothing(monkeypatch):
    _set_required_env(monkeypatch)
    fake = _FakeLLM()
    config = _production_config()
    config["llm"] = fake  # only the LLM is faked; every other key is the real file
    agent = cli.MarketplaceGraph(config=config)
    agent.compile()

    pasted = json.dumps({"mode": "ingest", "tenant_id": "EVIL", "transcript": _TRANSCRIPT})
    result = agent.invoke(
        pasted,
        ctx=_ctx(),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
    )

    assert result["status"] == "success"
    output = result["output"]
    assert output["mode"] == "invoke"
    assert output["degradation_reason"] == "empty_ledger+staging_unavailable"
    assert len(fake.calls) >= 1
    assert agent._ledger._staging == {}
    assert agent._ledger._rows == {}
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    assert "CANARY" not in serialized
    assert "EVIL" not in serialized


def test_explicit_null_llm_and_ledger_are_still_injected():
    """M5: a config.yaml with `llm: null` HAS the key, so `setdefault` would be a
    silent no-op and the Pod would run unwired with every test green. The adapter
    must guard on the value, not on key presence."""
    agent = cli.MarketplaceGraph(config={"llm": None, "ledger": None})
    assert isinstance(agent._llm, cli._AzureMarketplaceLLM)
    assert isinstance(agent._ledger, cli._ReadOnlyLedgerStore)
