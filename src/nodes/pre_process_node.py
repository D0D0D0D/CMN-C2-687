"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Outer `pre_process` (docs/02_design.md §Node Configuration §pre_process, §S-2).
# Responsibilities:
#   - copy the adapter-validated scope/mode/meta/as_of from input_context onto flat State
#   - derive normalized_transcript from the (framework-S-2-masked) user_input
#   - input quality gate (S-2 quarantine) via _extra_security_gate_input
#   - on any reject, write a minimal ALLOWLISTED error envelope so route()->finalize surfaces
#     a deterministic, envelope-shaped result (post_process is skipped on error).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import normalize

# fixed error-code enum (docs/02 §Error routing) — PII-free reason strings.
ERR_QUALITY_GATE = "quality_gate"
ERR_MODE_INVALID = "mode_invalid"
ERR_SCOPE_MISSING = "scope_missing"

_VALID_MODES = ("ingest", "invoke", "feedback")

# S-2 quality-gate reason -> stable, PII-free message.
_QG_MESSAGE = {
    normalize.QG_DISALLOWED_FORMAT: "input format not allowed (binary document rejected)",
    normalize.QG_INVALID_ENCODING: "input encoding invalid (mojibake)",
    normalize.QG_CONTROL_CHARS: "input contains too many control characters",
    normalize.QG_LOW_PRINTABLE: "input has too few printable characters",
    normalize.QG_TOO_SHORT: "input transcript too short",
    normalize.QG_TOO_LONG: "input transcript too long",
}


class PreProcessNode(FunctionNode):
    """Scope/mode copy, transcript normalization, and S-2 input quality gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        ctx = state.get("input_context", {}) or {}
        mode = ctx.get("mode") or state.get("mode") or "invoke"

        if mode not in _VALID_MODES:
            emit_trace_event("mode_rejected", {"reason": ERR_MODE_INVALID}, state)
            return _reject(mode, ERR_MODE_INVALID, "mode is missing or invalid")

        tenant_id = ctx.get("tenant_id") or state.get("tenant_id") or ""
        org_id = ctx.get("org_id") or state.get("org_id") or ""
        if not tenant_id or not org_id:
            emit_trace_event("scope_rejected", {"reason": ERR_SCOPE_MISSING}, state)
            return _reject(mode, ERR_SCOPE_MISSING, "tenant_id/org_id scope is missing")

        result: dict[str, Any] = {
            "mode": mode,
            "tenant_id": tenant_id,
            "org_id": org_id,
            "as_of_utc": ctx.get("as_of_utc") or state.get("as_of_utc") or "",
            "meeting_meta": ctx.get("meeting_meta") or state.get("meeting_meta") or {},
            "status": AgentStatus.SUCCESS.value,
        }
        if ctx.get("brief_id") is not None:
            result["brief_id"] = ctx["brief_id"]
        if ctx.get("approved_item_ids") is not None:
            result["approved_item_ids"] = ctx["approved_item_ids"]

        if mode in ("invoke", "ingest"):
            # user_input carries the PII-masked transcript (framework S-2 re-scans it).
            result["normalized_transcript"] = normalize.nfkc(state.get("user_input", "") or "")
            emit_trace_event("transcript_normalized", {"mode": mode, "chars": len(result["normalized_transcript"])}, state)
        else:
            emit_trace_event("transcript_normalized", {"mode": mode, "chars": 0}, state)
        return result

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 input quality gate — quarantine before execute() (docs/02 §S-2). feedback has
        no transcript, so the gate is skipped for it."""
        ctx = state.get("input_context", {}) or {}
        mode = ctx.get("mode") or state.get("mode") or "invoke"
        if mode == "feedback":
            return state
        transcript = state.get("user_input", "") or ""
        reason = normalize.quality_gate(transcript, self._config)
        if reason is None:
            return state
        emit_trace_event("quality_gate_rejected", {"mode": mode, "reason": reason}, state)
        state = dict(state)
        envelope = _reject(mode, ERR_QUALITY_GATE, _QG_MESSAGE.get(reason, "input rejected by quality gate"))
        state["status"] = envelope["status"]
        state["error_log"] = [f"PreProcessNode: quality gate rejected — {reason}"]
        state["formatted_output"] = envelope["formatted_output"]
        return state


def _reject(mode: str, code: str, message: str) -> dict:
    """Minimal allowlisted error envelope (docs/02 §Error routing). No transcript / no PII."""
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"PreProcessNode: {code} — {message}"],
        "formatted_output": {"mode": mode, "status": "error", "error": {"code": code, "message": message}},
    }
