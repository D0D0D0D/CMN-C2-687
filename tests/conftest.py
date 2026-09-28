"""Shared test fixtures for CMN-C2-687.

Loaded by pytest in CI (the SDK venv). The framework-free service tests under tests/unit/
(test_services_*.py) do not depend on these fixtures and run under the system interpreter.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import MeetingCommitmentLedgerAgent
from src.services.stores import InMemoryLedgerStore, InMemoryRecallIndex


class FakeLLM:
    """BaseLLM-compatible fake. Extracts one action item per transcript sentence, with the
    sentence as a VERBATIM evidence_span (owner/due left empty). It never fabricates a
    ledger_entry_id or an ungrounded span, so it cannot hide an unfired provenance guard."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def complete(self, messages: list) -> dict:
        self.calls.append(messages)
        prompt = messages[0].get("content", "") if messages else ""
        transcript = _transcript_from_prompt(prompt)
        items = []
        for sentence in re.split(r"[。\n.]", transcript):
            s = sentence.strip()
            if len(s) >= 8:
                items.append(
                    {"type": "action", "summary": s[:40], "owner": "", "due": "", "evidence_span": s, "params": {}}
                )
        return {"content": json.dumps({"items": items}, ensure_ascii=False), "tool_calls": [], "model": "fake"}

    def stream(self, messages: list):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list) -> "FakeLLM":
        return self


def _transcript_from_prompt(prompt: str) -> str:
    m = re.search(r"<<<\n(.*)\n>>>", prompt, re.DOTALL)
    return m.group(1) if m else ""


def make_ctx(caller_id: str = "caller-1", trust: TrustLevel = TrustLevel.VERIFIED_EXTERNAL) -> InvocationContext:
    return InvocationContext(session_id="test-session", caller_trust_level=trust, caller_id=caller_id)


def build_agent(llm: Any = None, ledger: Any = None, recall: Any = None) -> MeetingCommitmentLedgerAgent:
    agent = MeetingCommitmentLedgerAgent(
        config={"max_retry": 3, "recall_score_threshold": 0.2, "staging_ttl_seconds": 604800},
        llm=llm,
        ledger=ledger if ledger is not None else InMemoryLedgerStore(),
        recall=recall if recall is not None else InMemoryRecallIndex(),
    )
    agent.compile()
    return agent


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def ledger() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def agent(fake_llm: FakeLLM, ledger: InMemoryLedgerStore) -> MeetingCommitmentLedgerAgent:
    return build_agent(llm=fake_llm, ledger=ledger)


def invoke_context(mode: str, tenant_id: str = "t1", org_id: str = "o1", **extra) -> dict:
    ic = {"mode": mode, "tenant_id": tenant_id, "org_id": org_id, "as_of_utc": "2026-08-05T00:00:00Z"}
    ic.update(extra)
    return ic


@pytest.fixture
def agent_config() -> dict:
    # tests/integration/test_startup_and_flow.py:117 の seed 不要な invoke happy path と同じ構築前提。
    return {}


@pytest.fixture
def case_payload() -> str:
    # tests/integration/test_startup_and_flow.py:121 の成功 invoke と同じ会議 transcript。
    return "担当Bが来週金曜までに新しい設計書のレビューを完了すると決定した。"


@pytest.fixture
def live_input_context() -> dict:
    # tests/integration/test_startup_and_flow.py:120 の成功 invoke と同じ input_context。
    return {
        "mode": "invoke",
        "tenant_id": "t1",
        "org_id": "o1",
        "as_of_utc": "2026-08-05T00:00:00Z",
        "meeting_meta": {"meeting_id": "M1"},
    }


@pytest.fixture
def live_allowed_degradation_reasons() -> set[str]:
    """実 LLM 1回転チェック（work/scripts/mrb_live_llm_check.py）で許容する縮退理由。

    蓄積型の ledger は本 repo の統合テストが seed する。実 LLM 1回転チェックは
    未シードの空 ledger に対して回すため、この縮退理由だけは想定内として許可する
    （LLM 経路は実際に呼ばれ、抽出結果に実質的な内容が生成されていることを確認済み）。
    """
    return {"empty_ledger"}
