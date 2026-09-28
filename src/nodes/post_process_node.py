"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Outer `post_process` (docs/02_design.md §Node Configuration §post_process, §S-3).
# DocGen: assemble the minutes draft (invoke) + build the ALLOWLISTED formatted_output
# envelope. The S-3 envelope allowlist is enforced in _extra_security_gate_output — it runs
# on `result` only (no state), so execute() declares the envelope shape via mode/status and
# the gate selects the allowlist from those declared values (fail-closed, no state dependency).

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import brief as brief_svc
from src.services import envelope as envelope_svc

_EMPTY_BRIEF = {"new": [], "resurfaced": [], "contradictions": [], "completions": []}


class PostProcessNode(FunctionNode):
    """DocGen minutes + allowlisted output envelope + S-3 gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        mode = state.get("mode", "invoke")

        if mode == "invoke":
            formatted = self._format_invoke(state)
        elif mode == "ingest":
            formatted = self._format_ingest(state)
        elif mode == "feedback":
            formatted = self._format_feedback(state)
        else:
            # unknown mode should never reach post_process (route sends only success here);
            # emit an allowlist-incompatible shape so the S-3 gate fails closed.
            formatted = {"mode": mode, "status": "success"}

        if state.get("degradation_reason"):
            formatted["degradation_reason"] = state["degradation_reason"]

        emit_trace_event("output_formatted", {"mode": mode}, state)
        # 🔴 minutes_draft has a SINGLE home: inside formatted_output (redacted by the S-3
        # recursive residue mask in _extra_security_gate_output). Do NOT also return it as a
        # top-level result string: the framework @final S-3 credential scan runs on top-level
        # result strings and would RAISE an unshaped error on a credential that the envelope
        # masker would instead redact — an inconsistent fail path.
        return {"formatted_output": formatted, "status": AgentStatus.SUCCESS.value}

    def _format_invoke(self, state: dict) -> dict:
        minutes = brief_svc.build_minutes_draft(state.get("extracted_items", []) or [], state.get("meeting_meta", {}) or {})
        formatted: dict[str, Any] = {
            "mode": "invoke",
            "status": "success",
            "minutes_draft": minutes,
            "commitment_brief": state.get("commitment_brief") or dict(_EMPTY_BRIEF),
        }
        if state.get("brief_id"):
            formatted["brief_id"] = state["brief_id"]
        return formatted

    @staticmethod
    def _format_ingest(state: dict) -> dict:
        return {
            "mode": "ingest",
            "status": "success",
            "ingest_summary": state.get("ingest_summary") or {"written": 0, "extracted": 0},
        }

    @staticmethod
    def _format_feedback(state: dict) -> dict:
        return {
            "mode": "feedback",
            "status": "success",
            "write_result": state.get("write_result") or {"applied": [], "rejected": []},
        }

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 envelope allowlist (fail-closed) + recursive credential/PII residue mask."""
        try:
            result["formatted_output"] = envelope_svc.enforce_and_mask(result.get("formatted_output"))
        except envelope_svc.EnvelopeViolation as exc:
            raise SecurityViolationError(f"S-3 gate: {exc}") from exc
        return result

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
