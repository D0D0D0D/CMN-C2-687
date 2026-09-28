# CMN-C2-687 — Unit Tests: main slot (LedgerWorkflowGraphNode / GraphNode)
#
# The main slot is the Cat 2 GraphNode (LedgerWorkflowGraphNode), not the scaffold's simple
# FunctionNode. PB-6 (test_pb_invoke_order.py) excludes GraphNode from its dynamic scan and
# declares its S-2/S-3 delegation + extract_input/merge_output contracts are verified HERE.


from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import LedgerWorkflowGraphNode


class TestLedgerWorkflowGraphNode:
    def setup_method(self):
        self.node = LedgerWorkflowGraphNode()

    def test_is_graphnode_subclass(self):
        assert isinstance(self.node, GraphNode)

    def test_trust_level_declared_verified_external(self):
        # 🔴 GraphNode default is ANONYMOUS — declared VERIFIED_EXTERNAL to match inner nodes' S-1.
        assert LedgerWorkflowGraphNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_error_strategy_is_handle(self):
        # "handle": inner failures become an allowlisted error envelope, not a raised exception.
        assert LedgerWorkflowGraphNode.error_strategy == "handle"

    def test_hitl_not_propagated(self):
        assert LedgerWorkflowGraphNode.propagate_hitl is False

    def test_mandatory_abstract_methods_implemented(self):
        for name in ("get_subgraph", "extract_input", "merge_output"):
            assert name in LedgerWorkflowGraphNode.__dict__, f"missing abstract impl: {name}"

    # ── execute short-circuit ────────────────────────────────────────────────
    def test_execute_short_circuits_on_upstream_error(self):
        # pre_process set status=error (+wrote the envelope). main must NOT run the subgraph.
        delta = self.node.execute({"status": AgentStatus.ERROR.value, "mode": "invoke"})
        assert delta == {}

    # ── extract_input (serializes validated fields only) ─────────────────────
    def test_extract_input_serializes_scope_fields(self):
        import json

        s = self.node.extract_input(
            {"mode": "invoke", "normalized_transcript": "text", "tenant_id": "t1", "org_id": "o1", "as_of_utc": "Z"}
        )
        payload = json.loads(s)
        assert payload["mode"] == "invoke" and payload["tenant_id"] == "t1" and payload["normalized_transcript"] == "text"

    def test_extract_input_does_not_leak_raw_user_input(self):
        s = self.node.extract_input({"mode": "invoke", "user_input": "RAW-UNVALIDATED", "normalized_transcript": "masked"})
        assert "RAW-UNVALIDATED" not in s and "masked" in s

    # ── merge_output (delta-only, mode-scoped, no raw pass-through) ───────────
    def test_merge_output_invoke_maps_declared_keys_only(self):
        sub = {
            "status": AgentStatus.SUCCESS.value,
            "commitment_brief": {"new": []},
            "brief_id": "brief_1",
            "extracted_items": [{"summary": "x"}],
            # inner intermediates must NOT leak to outer state.
            "match_result": {"secret": True},
            "fetched_open_rows": [{"leak": True}],
        }
        delta = self.node.merge_output({"mode": "invoke"}, sub)
        assert delta["commitment_brief"] == {"new": []}
        assert delta["brief_id"] == "brief_1"
        assert delta["extracted_items"] == [{"summary": "x"}]
        assert "match_result" not in delta and "fetched_open_rows" not in delta

    def test_merge_output_scopes_ingest_summary(self):
        sub = {"status": AgentStatus.SUCCESS.value, "ingest_summary": {"written": 2}}
        assert self.node.merge_output({"mode": "ingest"}, sub)["ingest_summary"] == {"written": 2}
        assert "ingest_summary" not in self.node.merge_output({"mode": "invoke"}, sub)

    def test_merge_output_scopes_feedback_result(self):
        sub = {"status": AgentStatus.SUCCESS.value, "write_result": {"applied": ["item_a"], "rejected": []}}
        assert self.node.merge_output({"mode": "feedback"}, sub)["write_result"]["applied"] == ["item_a"]
        assert "write_result" not in self.node.merge_output({"mode": "invoke"}, sub)

    # ── inner-error shaping ──────────────────────────────────────────────────
    def test_on_subgraph_error_returns_allowlisted_envelope(self):
        delta = self.node.on_subgraph_error({"mode": "invoke"}, RuntimeError("boom"))
        assert delta["status"] == AgentStatus.ERROR.value
        env = delta["formatted_output"]
        assert set(env) == {"mode", "status", "error"} and env["error"]["code"] == "subgraph_error"

    def test_error_message_is_fixed_public_string_not_exception_text(self):
        # Sensitive database details in the exception must NOT reach the client message.
        delta = self.node.on_subgraph_error(
            {"mode": "invoke"}, RuntimeError("database DSN leaked sensitive configuration")
        )
        msg = delta["formatted_output"]["error"]["message"]
        assert "database" not in msg and "leaked" not in msg  # fixed public message only
        assert any("database DSN" in e for e in delta["error_log"])  # internal detail kept in error_log (not surfaced)

    def test_classify_inner_error_provenance(self):
        from src.nodes.main_node import _classify_inner_error

        assert _classify_inner_error(["provenance_failed: evidence_span_not_in_transcript"]) == "provenance_failed"
        assert _classify_inner_error(["MatchNode: feedback rejected — brief_id_already_consumed"]) == "brief_id_invalid"
        assert _classify_inner_error(["some other error"]) == "subgraph_error"

    # ── S-2/S-3 delegation (GraphNode deliberate no-op) ──────────────────────
    def test_security_gate_input_is_noop_passthrough(self):
        state = {"validated_input": "x"}
        assert self.node._extra_security_gate_input(state) is state

    def test_security_gate_output_is_noop_passthrough(self):
        result = {"result": {}}
        assert self.node._extra_security_gate_output(result) is result
