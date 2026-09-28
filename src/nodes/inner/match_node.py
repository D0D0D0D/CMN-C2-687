"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Inner `match`/`apply` node (docs/02_design.md §Inner subgraph). Mode selects behaviour:
#   ingest   -> bulk write extracted rows as ACTIVE (bootstrap)
#   invoke   -> fetch open/active (scoped) + KB recall -> classify -> stash match_result
#               (the brief node writes the staging record AFTER provenance passes)
#   feedback -> consume-once CAS: promote approved staged candidates -> active
#
# 🔴 required_trust_level = VERIFIED_EXTERNAL (write path). Scope is fail-closed: the store
# refuses an empty (tenant_id, org_id).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import brief as brief_svc
from src.services import stores

_DEFAULT_SCORE_THRESHOLD = 0.70
_DEFAULT_TOP_K = 8


class MatchNode(FunctionNode):
    """Ledger match / write node (mode-parameterized)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, ledger: Any = None, recall: Any = None, config: dict | None = None) -> None:
        super().__init__()
        self._ledger = ledger
        self._recall = recall
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        mode = state.get("mode", "invoke")
        tenant_id = state.get("tenant_id", "")
        org_id = state.get("org_id", "")

        if not tenant_id or not org_id:
            emit_trace_event("scope_rejected", {"mode": mode, "reason": "scope_missing"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["MatchNode: tenant_id/org_id missing (fail-closed)"]}

        if self._ledger is None:
            # unwired ledger seam -> deterministic degradation (dev/CI), not error.
            emit_trace_event("degraded", {"mode": mode, "reason": "ledger_unwired"}, state)
            return self._degraded(mode)

        try:
            if mode == "ingest":
                return self._do_ingest(state, tenant_id, org_id)
            if mode == "feedback":
                return self._do_feedback(state, tenant_id, org_id)
            return self._do_invoke(state, tenant_id, org_id)
        except stores.ScopeError as exc:
            emit_trace_event("scope_rejected", {"mode": mode, "reason": "scope_error"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"MatchNode: {exc}"]}

    # ── ingest ───────────────────────────────────────────────────────────────
    def _do_ingest(self, state: dict, tenant_id: str, org_id: str) -> dict:
        items = state.get("extracted_items", []) or []
        meeting_id = str((state.get("meeting_meta") or {}).get("meeting_id", ""))
        rows = brief_svc.build_active_rows_from_extraction(items, meeting_id, state.get("as_of_utc", ""))
        written = self._ledger.write_active(tenant_id, org_id, rows)
        emit_trace_event("ledger_write_applied", {"mode": "ingest", "written": written}, state)
        return {
            "ingest_summary": {"written": written, "extracted": len(items)},
            "status": AgentStatus.SUCCESS.value,
        }

    # ── invoke ───────────────────────────────────────────────────────────────
    def _do_invoke(self, state: dict, tenant_id: str, org_id: str) -> dict:
        items = state.get("extracted_items", []) or []
        as_of = state.get("as_of_utc", "")
        open_rows = self._ledger.fetch_open_commitments(tenant_id, org_id)

        # advisory semantic recall (widens signal; authoritative rows come from the ledger).
        if self._recall is not None and items:
            query = " ".join(str(i.get("summary", "")) for i in items)
            try:
                recalled = self._recall.search(tenant_id, org_id, query, int(self._config.get("recall_top_k", _DEFAULT_TOP_K)))
            except stores.ScopeError:
                recalled = []
            emit_trace_event("ledger_matched", {"open_rows": len(open_rows), "recalled": len(recalled)}, state)
        else:
            emit_trace_event("ledger_matched", {"open_rows": len(open_rows), "recalled": 0}, state)

        threshold = float(self._config.get("recall_score_threshold", _DEFAULT_SCORE_THRESHOLD))
        match_result = brief_svc.classify(items, open_rows, as_of, threshold)

        out: dict[str, Any] = {
            "match_result": match_result,
            "fetched_open_rows": open_rows,
            "status": AgentStatus.SUCCESS.value,
        }
        if not open_rows:
            out["degradation_reason"] = "empty_ledger"
            emit_trace_event("degraded", {"mode": "invoke", "reason": "empty_ledger"}, state)
        return out

    # ── feedback (consume-once CAS) ──────────────────────────────────────────
    def _do_feedback(self, state: dict, tenant_id: str, org_id: str) -> dict:
        brief_id = state.get("brief_id", "")
        approved = state.get("approved_item_ids", []) or []
        created_by = state.get("caller_id", "")
        as_of = state.get("as_of_utc", "")
        builder = brief_svc.active_row_from_candidate(tenant_id, org_id)
        promotion = self._ledger.consume_and_promote(
            brief_id, tenant_id, org_id, created_by, as_of, approved, builder
        )
        if promotion.get("reason"):
            emit_trace_event("feedback_id_rejected", {"reason": promotion["reason"]}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"MatchNode: feedback rejected — {promotion['reason']}"],
            }
        emit_trace_event(
            "ledger_write_applied",
            {"mode": "feedback", "applied": len(promotion.get("applied", []))},
            state,
        )
        return {
            "write_result": {"applied": promotion.get("applied", []), "rejected": promotion.get("rejected", [])},
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _degraded(mode: str) -> dict:
        out: dict[str, Any] = {"degradation_reason": "ledger_unwired", "status": AgentStatus.SUCCESS.value}
        if mode == "invoke":
            out["match_result"] = {"new": [], "resurfaced": [], "contradictions": [], "completions": []}
            out["fetched_open_rows"] = []
        elif mode == "ingest":
            out["ingest_summary"] = {"written": 0, "extracted": 0}
        else:
            out["write_result"] = {"applied": [], "rejected": []}
        return out

    def _extra_security_gate_output(self, result: dict) -> dict:
        return result
