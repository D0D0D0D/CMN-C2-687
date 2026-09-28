# CMN-C2-687 — Unit Tests: outer pre_process / post_process nodes

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode


class TestPreProcess:
    def test_success_copies_scope_and_normalizes(self):
        node = PreProcessNode()
        ctx = {
            "mode": "invoke",
            "tenant_id": "t1",
            "org_id": "o1",
            "as_of_utc": "2026-08-05T00:00:00Z",
            "meeting_meta": {"meeting_id": "M1"},
        }
        out = node.execute({"user_input": "十分な長さの正常な議事録テキスト。", "input_context": ctx})
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["mode"] == "invoke" and out["tenant_id"] == "t1"
        assert out["normalized_transcript"]

    def test_scope_missing_error_envelope(self):
        node = PreProcessNode()
        out = node.execute({"user_input": "x", "input_context": {"mode": "invoke"}})
        assert out["status"] == AgentStatus.ERROR.value
        assert out["formatted_output"]["error"]["code"] == "scope_missing"

    def test_mode_invalid_error_envelope(self):
        node = PreProcessNode()
        out = node.execute({"user_input": "x", "input_context": {"mode": "wipe"}})
        assert out["formatted_output"]["error"]["code"] == "mode_invalid"

    def test_quality_gate_rejects_binary_magic(self):
        node = PreProcessNode()
        state = {"user_input": "%PDF-1.7 binary payload padding text here", "input_context": {"mode": "invoke"}}
        out = node._extra_security_gate_input(state)
        assert out["status"] == AgentStatus.ERROR.value
        assert out["formatted_output"]["error"]["code"] == "quality_gate"

    def test_quality_gate_skipped_for_feedback(self):
        node = PreProcessNode()
        state = {"user_input": "", "input_context": {"mode": "feedback"}}
        assert node._extra_security_gate_input(state) is state


class TestPostProcess:
    def test_invoke_envelope_has_minutes_and_brief(self):
        node = PostProcessNode()
        out = node.execute(
            {
                "mode": "invoke",
                "extracted_items": [{"type": "action", "summary": "在庫確認", "owner": "担当A", "due": ""}],
                "meeting_meta": {"title": "定例", "date": "2026-08-05"},
                "commitment_brief": {"new": [], "resurfaced": [], "contradictions": [], "completions": []},
                "brief_id": "brief_1",
            }
        )
        fo = out["formatted_output"]
        assert fo["mode"] == "invoke" and "minutes_draft" in fo and "commitment_brief" in fo and fo["brief_id"] == "brief_1"
        # S-3 gate accepts a well-formed envelope.
        node._extra_security_gate_output(out)

    def test_ingest_envelope(self):
        node = PostProcessNode()
        out = node.execute({"mode": "ingest", "ingest_summary": {"written": 2, "extracted": 2}})
        assert out["formatted_output"]["ingest_summary"]["written"] == 2
        node._extra_security_gate_output(out)

    def test_feedback_envelope(self):
        node = PostProcessNode()
        out = node.execute({"mode": "feedback", "write_result": {"applied": ["item_a"], "rejected": []}})
        assert out["formatted_output"]["write_result"]["applied"] == ["item_a"]
        node._extra_security_gate_output(out)

    def test_s3_gate_rejects_unknown_mode(self):
        node = PostProcessNode()
        with pytest.raises(SecurityViolationError):
            node._extra_security_gate_output({"formatted_output": {"mode": "smuggle", "status": "success"}})
