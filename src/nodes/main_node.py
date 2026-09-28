"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Outer `main` slot — LedgerWorkflowGraphNode (docs/02_design.md §main（GraphNode）, decision H8).
# Wraps the inner LedgerWorkflowGraph (src/graph/domain_workflow_graph.py).
#
# 🔴 execute() is fully overridden (verified against wheel graph_node.py):
#   - pre_process -> main is an UNCONDITIONAL edge and route() runs AFTER main, so a
#     status="error" from pre_process would still run the subgraph unless main short-circuits.
#   - the stock GraphNode.execute() routes an inner returned status="error" to
#     on_subgraph_error (NOT merge_output), so to give the inner reject a distinct error code
#     (provenance_failed vs brief_id_invalid vs subgraph_error) we shape it here. Both the
#     raised-exception and returned-error inner paths converge on an allowlisted error
#     envelope, so an inner failure never leaves formatted_output unset.

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class LedgerWorkflowGraphNode(GraphNode):
    """Delegates to the inner LedgerWorkflowGraph; shapes errors into an allowlisted envelope."""

    # 🔴 GraphNode default is ANONYMOUS — declare VERIFIED_EXTERNAL to match the inner nodes' S-1.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    # error_strategy="handle": we convert an inner failure into an envelope rather than raising.
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False

    # inner keys the schema boundary surfaces (mode-scoped) — NO raw pass-through.
    _INVOKE_KEYS = ("commitment_brief", "brief_id")

    def __init__(self, config: dict | None = None, llm: Any = None, ledger: Any = None, recall: Any = None) -> None:
        super().__init__()
        self._config = config or {}
        self._llm = llm
        self._ledger = ledger
        self._recall = recall
        self._subgraph = None

    def get_subgraph(self):
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import LedgerWorkflowGraph

            subgraph = LedgerWorkflowGraph(
                config=self._config, llm=self._llm, ledger=self._ledger, recall=self._recall
            )
            subgraph.compile()
            self._subgraph = subgraph
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        """Serialize the validated scope/mode fields for the inner graph (a string + ctx).
        Uses only validated flat fields (never the raw user_input) — pre_process has run on
        the success path (main short-circuits otherwise)."""
        payload = {
            "mode": state.get("mode"),
            "normalized_transcript": state.get("normalized_transcript", ""),
            "meeting_meta": state.get("meeting_meta", {}),
            "tenant_id": state.get("tenant_id", ""),
            "org_id": state.get("org_id", ""),
            "as_of_utc": state.get("as_of_utc", ""),
            "brief_id": state.get("brief_id"),
            "approved_item_ids": state.get("approved_item_ids"),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            # upstream (pre_process) already set error + wrote the envelope — do not run inner.
            emit_trace_event("main_short_circuited", {"mode": state.get("mode")}, state)
            return {}

        # 🔴 wrap the ENTIRE delegation (ctx build, compile, serialize, invoke). Any exception
        # anywhere converges on the allowlisted error envelope, so formatted_output is never
        # left unset (a bare BaseNode.__call__ catch would return an envelope-less error).
        try:
            ctx = replace(InvocationContext.from_state(state), hitl_allowed=False)
            subgraph = self.get_subgraph()
            user_input = self.extract_input(state)
            sub_result = subgraph.invoke(user_input, session_id=ctx.session_id, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 — infra/subgraph exception -> shaped envelope
            return self.on_subgraph_error(state, exc)

        if not isinstance(sub_result, dict):
            return self._error_envelope(state, "subgraph_error", detail="inner returned a non-dict result")

        if sub_result.get("status") == AgentStatus.ERROR.value:
            code = _classify_inner_error(sub_result.get("error_log", []))
            return self._error_envelope(state, code, detail=_first_message(sub_result.get("error_log", [])))
        return self.merge_output(state, sub_result)

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict:
        return self._error_envelope(state, "subgraph_error", detail=str(error)[:200])

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map ONLY declared inner keys into outer state (mode-scoped, no raw pass-through)."""
        delta: dict[str, Any] = {"status": sub_result.get("status", AgentStatus.SUCCESS.value)}
        if sub_result.get("degradation_reason"):
            delta["degradation_reason"] = sub_result["degradation_reason"]
        # extracted_items is surfaced so the outer post_process can build the minutes draft.
        if "extracted_items" in sub_result:
            delta["extracted_items"] = sub_result["extracted_items"]

        mode = state.get("mode")
        if mode == "invoke":
            for key in self._INVOKE_KEYS:
                if key in sub_result:
                    delta[key] = sub_result[key]
        elif mode == "ingest" and "ingest_summary" in sub_result:
            delta["ingest_summary"] = sub_result["ingest_summary"]
        elif mode == "feedback" and "write_result" in sub_result:
            delta["write_result"] = sub_result["write_result"]
        return delta

    def _error_envelope(self, state: AgentState, code: str, detail: str = "") -> dict:
        """Allowlisted error envelope. The client-facing message is a FIXED public string per
        code (never str(exception)/error_log text — those can carry a connection string, prompt
        fragment, or PII; docs/02 §Error routing). The internal detail stays in error_log,
        which get_output() does NOT surface to the client."""
        emit_trace_event("main_short_circuited", {"mode": state.get("mode"), "code": code}, state)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"main: {code}" + (f" | {detail}" if detail else "")],
            "formatted_output": {
                "mode": state.get("mode"),
                "status": "error",
                "error": {"code": code, "message": _PUBLIC_MESSAGE.get(code, _PUBLIC_MESSAGE["subgraph_error"])},
            },
        }

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        return result


# fixed, PII-free client-facing messages per error code (docs/02 §Error routing).
_PUBLIC_MESSAGE = {
    "subgraph_error": "the inner workflow could not complete",
    "provenance_failed": "evidence provenance verification failed; no brief was produced",
    "brief_id_invalid": "the feedback reference is invalid, expired, or already used",
}


def _classify_inner_error(error_log: list) -> str:
    joined = " ".join(str(e) for e in (error_log or [])).lower()
    if "provenance_failed" in joined:
        return "provenance_failed"
    if "feedback rejected" in joined or "brief_id" in joined:
        return "brief_id_invalid"
    return "subgraph_error"


def _first_message(error_log: list) -> str:
    if error_log:
        return str(error_log[0])[:200]
    return "inner workflow error"
