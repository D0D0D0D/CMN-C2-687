"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Inner `brief` node (docs/02_design.md §Inner subgraph, §S-3 provenance).
#   invoke   -> build the fixed-schema brief, run S-3 provenance (three-layer + bidirectional).
#               On PASS, write the scoped/expiring staging record (so a rejected brief never
#               leaves a staging record — DDR: staging is written here, after provenance).
#   ingest   -> minimal load summary (pass through ingest_summary).
#   feedback -> write-result summary (pass through write_result).
#
# 🔴 S-3 provenance runs in execute() (it needs `state` — transcript + fetched rows), which
# `_extra_security_gate_output(result)` cannot see. Provenance failure is FAIL-CLOSED: the
# whole item set is withheld (no partial brief).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import brief as brief_svc
from src.services import identifiers, normalize, provenance
from src.services import stores

_DEFAULT_STAGING_TTL = 604800  # 7 days


class BriefNode(FunctionNode):
    """Brief construction + S-3 provenance + staging write (invoke)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, ledger: Any = None, config: dict | None = None) -> None:
        super().__init__()
        self._ledger = ledger
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        mode = state.get("mode", "invoke")
        # upstream inner error already set — pass through (route skips downstream on error).
        if state.get("status") == AgentStatus.ERROR.value:
            # S-4: every execute() emits >=1 domain event, incl. the short-circuit path.
            emit_trace_event("brief_short_circuited", {"mode": mode}, state)
            return {}

        if mode == "ingest":
            emit_trace_event("brief_built", {"mode": "ingest"}, state)
            return {"status": AgentStatus.SUCCESS.value}
        if mode == "feedback":
            emit_trace_event("brief_built", {"mode": "feedback"}, state)
            return {"status": AgentStatus.SUCCESS.value}

        # ── invoke ───────────────────────────────────────────────────────────
        match_result = state.get("match_result", {}) or {}
        open_rows = state.get("fetched_open_rows", []) or []
        meeting_id = str((state.get("meeting_meta") or {}).get("meeting_id", ""))
        transcript = state.get("normalized_transcript", "") or ""
        as_of = state.get("as_of_utc", "")

        brief_items = brief_svc.build_commitment_brief(match_result, meeting_id, as_of)
        prov = provenance.verify_provenance(
            brief_items,
            brief_svc.fetched_rows_by_id(open_rows),
            brief_svc.expected_ledger_ids(match_result),
            meeting_id,
            transcript,
            as_of,
        )
        if not prov.ok:
            emit_trace_event("provenance_rejected", {"reason": prov.reason}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [prov.as_error_log()]}

        # provenance passed -> persist the exact presented candidate set under a stable brief_id.
        brief_id = identifiers.brief_id(state.get("correlation_id", ""), state.get("tenant_id", ""), state.get("org_id", ""), meeting_id)
        result: dict[str, Any] = {
            "commitment_brief": self._shape_brief(brief_items, meeting_id),
            "brief_id": brief_id,
            "status": AgentStatus.SUCCESS.value,
        }
        wrote = self._write_staging(state, brief_id, brief_items, meeting_id, as_of)
        if not wrote:
            # staging seam refused (fail-closed scope / unwired) -> degrade, still present.
            result["degradation_reason"] = _join(state.get("degradation_reason"), "staging_unavailable")
        emit_trace_event("brief_built", {"mode": "invoke", "items": len(brief_items)}, state)
        return result

    def _write_staging(self, state: dict, brief_id: str, brief_items: list, meeting_id: str, as_of: str) -> bool:
        if self._ledger is None:
            emit_trace_event("degraded", {"mode": "invoke", "reason": "staging_unwired"}, state)
            return False
        ttl = int(self._config.get("staging_ttl_seconds", _DEFAULT_STAGING_TTL))
        candidate_set = brief_svc.build_candidate_set(brief_items, meeting_id)
        record = {
            "brief_id": brief_id,
            "tenant_id": state.get("tenant_id", ""),
            "org_id": state.get("org_id", ""),
            "created_by": state.get("caller_id", ""),
            "candidate_set": candidate_set,
            "issued_at": as_of,
            "expires_at": normalize.add_seconds_iso(as_of, ttl),
            "status": stores.STAGING_PENDING,
            "consumed_at": None,
        }
        try:
            self._ledger.stage(record)
        except stores.ScopeError:
            emit_trace_event("scope_rejected", {"mode": "invoke", "reason": "staging_scope"}, state)
            return False
        emit_trace_event("staged_candidates_written", {"brief_id": brief_id, "count": len(candidate_set)}, state)
        return True

    @staticmethod
    def _shape_brief(brief_items: list, meeting_id: str) -> dict:
        """Group the flat brief items into the category-keyed envelope object. Each presented
        item carries its item_id so the human can approve it by id in the feedback call."""
        shaped: dict[str, list] = {"new": [], "resurfaced": [], "contradictions": [], "completions": []}
        key_by_category = {
            provenance.NEW: "new",
            provenance.RESURFACED: "resurfaced",
            provenance.CONTRADICTION: "contradictions",
            provenance.COMPLETION: "completions",
        }
        for item in brief_items:
            bucket = key_by_category.get(item.get("category"))
            if bucket is None:
                continue
            shaped[bucket].append(
                {
                    "item_id": brief_svc.item_id_for(item, meeting_id),
                    "summary": item.get("summary", ""),
                    "params": item.get("params", {}),
                    "evidence_ids": item.get("evidence_ids", {}),
                    "evidence_span": item.get("evidence_span", ""),
                }
            )
        return shaped

    def _extra_security_gate_output(self, result: dict) -> dict:
        # Residual credential/PII scan is enforced by the outer post_process S-3 envelope
        # allowlist (recursive). The framework @final S-3 already credential-scans top-level
        # strings here; nothing extra needed at the inner boundary.
        return result


def _join(existing: Any, reason: str) -> str:
    if existing:
        return f"{existing}+{reason}"
    return reason
