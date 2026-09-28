# CMN-C2-687 — Unit Tests: inner extract / match / brief nodes

import json

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.brief_node import BriefNode
from src.nodes.inner.extract_node import ExtractNode
from src.nodes.inner.match_node import MatchNode
from src.services import brief as brief_svc
from src.services import stores

_ENVELOPE = json.dumps(
    {"mode": "invoke", "normalized_transcript": "担当Aが在庫を確認する。承認者が予算を確定する。", "meeting_meta": {"meeting_id": "M1"}},
    ensure_ascii=False,
)


class TestExtract:
    def test_feedback_does_not_extract(self):
        out = ExtractNode(llm=None).execute({"user_input": "{}", "mode": "feedback"})
        assert out["extracted_items"] == [] and out["status"] == AgentStatus.SUCCESS.value

    def test_invoke_no_llm_degrades(self):
        out = ExtractNode(llm=None).execute({"user_input": _ENVELOPE})
        assert out["extracted_items"] == [] and "degradation_reason" in out
        assert out["status"] == AgentStatus.SUCCESS.value

    def test_invoke_with_llm_extracts(self, fake_llm):
        out = ExtractNode(llm=fake_llm).execute({"user_input": _ENVELOPE})
        assert len(out["extracted_items"]) >= 1
        assert all("evidence_span" in it for it in out["extracted_items"])


class TestMatch:
    def test_ingest_writes_active_rows(self):
        led = stores.InMemoryLedgerStore()
        items = [{"type": "action", "summary": "在庫確認", "owner": "担当A", "due": "", "evidence_span": "担当Aが在庫確認", "params": {}}]
        out = MatchNode(ledger=led).execute(
            {"mode": "ingest", "extracted_items": items, "tenant_id": "t1", "org_id": "o1", "meeting_meta": {"meeting_id": "M1"}, "as_of_utc": "2026-08-05T00:00:00Z"}
        )
        assert out["ingest_summary"]["written"] == 1
        assert len(led.fetch_open_commitments("t1", "o1")) == 1

    def test_scope_missing_is_error(self):
        out = MatchNode(ledger=stores.InMemoryLedgerStore()).execute({"mode": "invoke", "tenant_id": "", "org_id": ""})
        assert out["status"] == AgentStatus.ERROR.value

    def test_invoke_empty_ledger_degrades(self):
        out = MatchNode(ledger=stores.InMemoryLedgerStore()).execute(
            {"mode": "invoke", "extracted_items": [], "tenant_id": "t1", "org_id": "o1", "as_of_utc": "2026-08-05T00:00:00Z"}
        )
        assert out["status"] == AgentStatus.SUCCESS.value and out.get("degradation_reason") == "empty_ledger"

    def test_feedback_promotes_staged(self):
        led = stores.InMemoryLedgerStore()
        led.stage(
            {
                "brief_id": "brief_1",
                "tenant_id": "t1",
                "org_id": "o1",
                "created_by": "caller-1",
                "candidate_set": [{"item_id": "item_a", "meeting_id": "M1", "summary": "s", "owner": "担当A", "due": "", "status": "active"}],
                "issued_at": "2026-08-05T00:00:00Z",
                "expires_at": "2026-08-12T00:00:00Z",
                "status": stores.STAGING_PENDING,
                "consumed_at": None,
            }
        )
        out = MatchNode(ledger=led).execute(
            {"mode": "feedback", "brief_id": "brief_1", "approved_item_ids": ["item_a"], "tenant_id": "t1", "org_id": "o1", "caller_id": "caller-1", "as_of_utc": "2026-08-06T00:00:00Z"}
        )
        assert out["status"] == AgentStatus.SUCCESS.value and out["write_result"]["applied"] == ["item_a"]

    def test_feedback_replay_rejected(self):
        led = stores.InMemoryLedgerStore()
        led.stage(
            {
                "brief_id": "brief_1", "tenant_id": "t1", "org_id": "o1", "created_by": "caller-1",
                "candidate_set": [{"item_id": "item_a", "meeting_id": "M1", "summary": "s", "owner": "", "due": "", "status": "active"}],
                "issued_at": "2026-08-05T00:00:00Z", "expires_at": "2026-08-12T00:00:00Z", "status": stores.STAGING_PENDING, "consumed_at": None,
            }
        )
        common = {"mode": "feedback", "brief_id": "brief_1", "approved_item_ids": ["item_a"], "tenant_id": "t1", "org_id": "o1", "caller_id": "caller-1", "as_of_utc": "2026-08-06T00:00:00Z"}
        MatchNode(ledger=led).execute(dict(common))
        out = MatchNode(ledger=led).execute(dict(common))  # replay
        assert out["status"] == AgentStatus.ERROR.value


class TestBrief:
    def _invoke_state(self, transcript, match_result):
        return {
            "mode": "invoke",
            "match_result": match_result,
            "fetched_open_rows": [],
            "meeting_meta": {"meeting_id": "M1"},
            "normalized_transcript": transcript,
            "as_of_utc": "2026-08-05T00:00:00Z",
            "tenant_id": "t1",
            "org_id": "o1",
            "correlation_id": "corr-1",
        }

    def test_invoke_builds_brief_and_stages(self):
        led = stores.InMemoryLedgerStore()
        transcript = "承認者が来週金曜までに予算を確定する。"
        items = [{"type": "action", "summary": "予算を確定", "owner": "", "due": "", "evidence_span": "承認者が来週金曜までに予算を確定する", "params": {}}]
        mr = brief_svc.classify(items, [], "2026-08-05T00:00:00Z", 0.7)
        out = BriefNode(ledger=led).execute(self._invoke_state(transcript, mr))
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["brief_id"].startswith("brief_")
        assert out["commitment_brief"]["new"]  # a presented candidate
        # staging persisted under the brief_id.
        assert led.get_staging(out["brief_id"]) is not None

    def test_invoke_provenance_fail_on_forged_span(self):
        led = stores.InMemoryLedgerStore()
        transcript = "本文には無い内容。"
        # span that does NOT appear in the transcript -> provenance fail-closed.
        items = [{"type": "action", "summary": "捏造", "owner": "", "due": "", "evidence_span": "議事録に存在しない長い証拠スパン", "params": {}}]
        mr = brief_svc.classify(items, [], "2026-08-05T00:00:00Z", 0.7)
        out = BriefNode(ledger=led).execute(self._invoke_state(transcript, mr))
        assert out["status"] == AgentStatus.ERROR.value
        assert any("provenance_failed" in e for e in out["error_log"])
