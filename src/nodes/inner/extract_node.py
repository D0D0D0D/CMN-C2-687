"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Inner `extract` node (docs/02_design.md §Inner subgraph). Parses the envelope handed to
# the inner graph, copies the validated scope/mode fields onto flat State, and runs LLM
# structured extraction for invoke/ingest. feedback does NOT re-extract.

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import extraction
from src.services.llm_client import LlmCallError, call_llm

_ENVELOPE_FIELDS = (
    "mode",
    "normalized_transcript",
    "meeting_meta",
    "tenant_id",
    "org_id",
    "as_of_utc",
    "brief_id",
    "approved_item_ids",
)


class ExtractNode(FunctionNode):
    """LLM structured extraction of decisions/action items (owner/due/verbatim span)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        envelope = self._parse_envelope(state.get("user_input", ""))
        # prefer the envelope value, fall back to any flat field already on state (PB-6).
        fields: dict[str, Any] = {f: envelope.get(f, state.get(f)) for f in _ENVELOPE_FIELDS}
        mode = fields.get("mode") or state.get("mode") or "invoke"
        fields["mode"] = mode

        result: dict[str, Any] = {k: v for k, v in fields.items() if v is not None}

        if mode == "feedback":
            # feedback does not re-extract.
            emit_trace_event("items_extracted", {"mode": mode, "count": 0}, state)
            result["extracted_items"] = []
            result["status"] = AgentStatus.SUCCESS.value
            return result

        transcript = fields.get("normalized_transcript") or ""
        meeting_meta = fields.get("meeting_meta") or {}
        if not transcript:
            emit_trace_event("degraded", {"mode": mode, "reason": "empty_transcript"}, state)
            result["extracted_items"] = []
            result["degradation_reason"] = "empty_transcript"
            result["status"] = AgentStatus.SUCCESS.value
            return result

        prompt = extraction.build_extract_prompt(transcript, meeting_meta)
        try:
            raw = call_llm(self._llm, prompt)
            items = extraction.parse_extraction(raw)
            emit_trace_event("items_extracted", {"mode": mode, "count": len(items)}, state)
            result["extracted_items"] = items
        except LlmCallError as exc:
            # un-injected / failing LLM seam -> deterministic degradation (dev/CI), not error.
            emit_trace_event("degraded", {"mode": mode, "reason": "llm_unavailable"}, state)
            result["extracted_items"] = []
            result["degradation_reason"] = f"llm_unavailable: {exc}"
        result["status"] = AgentStatus.SUCCESS.value
        return result

    @staticmethod
    def _parse_envelope(user_input: Any) -> dict:
        if not isinstance(user_input, str) or not user_input.strip():
            return {}
        try:
            data = json.loads(user_input)
        except (json.JSONDecodeError, TypeError, RecursionError):
            return {}
        return data if isinstance(data, dict) else {}

    def _extra_security_gate_input(self, state: dict) -> dict:
        # Framework @final S-2 already re-scans user_input for PII; no extra input gate here.
        return state
