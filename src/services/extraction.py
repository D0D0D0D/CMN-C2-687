"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# LLM structured extraction of decisions/action items (docs/02_design.md §Inner subgraph
# `extract`). Framework-free: the prompt builder + response parser are pure so they are
# unit-testable, and the live-LLM check can read them directly.
#
# 🔴 The prompt embeds the FULL response JSON schema. Instructing "output only the schema"
# WITHOUT including the schema makes the LLM return `{}`, which a lenient coercer then turns
# into a valid-but-empty result that passes silently (the MunicipalContractVariationEvidenceBriefAgent defect,
# a mock that returns a completed shape hides an unfired guard). The schema is spelled out below.

from __future__ import annotations

import json
from typing import Any

# minimum verbatim span length — an empty/1-char span makes `span in transcript` vacuously
# true (docs/02 §S-3 (b)); extraction discards anything shorter so the LLM cannot smuggle a
# claim with no real quote.
MIN_SPAN_CHARS = 8

_ITEM_TYPES = ("decision", "action")

_RESPONSE_SCHEMA = """{
  "items": [
    {
      "type": "decision" | "action",
      "summary": "<one-line summary of the decision/action, no personal names — refer to people by role>",
      "owner": "<responsible-party token copied VERBATIM from evidence_span, else empty string>",
      "due": "<deadline token copied VERBATIM from evidence_span (for example: 来週金曜), else empty string>",
      "evidence_span": "<a VERBATIM substring copied from the transcript, at least 8 characters, that supports this item>",
      "params": { "<optional structured fields, e.g. amount/quantity/target>": "<value copied from evidence_span>" }
    }
  ]
}"""


def build_extract_prompt(transcript: str, meeting_meta: dict) -> str:
    """Build the extraction prompt. The transcript is already PII-masked (S-2)."""
    meeting_id = str((meeting_meta or {}).get("meeting_id", ""))
    title = str((meeting_meta or {}).get("title", ""))
    return (
        "You extract the decisions and action items committed in a meeting transcript.\n"
        "Rules:\n"
        "  - Output ONLY a JSON object that conforms EXACTLY to the schema below. No prose.\n"
        "  - `evidence_span` MUST be copied verbatim from the transcript (>= 8 characters).\n"
        "  - `owner` and `due` MUST be exact literal substrings of `evidence_span`.\n"
        "  - NEVER infer, normalize, translate, or convert `owner` or `due`. In particular, "
        "keep a relative deadline such as `来週金曜` verbatim; do NOT convert it to ISO-8601. "
        "If the transcript does not explicitly state either field, use an empty string.\n"
        "  - Refer to people by role (担当A / 承認者), never by personal name.\n"
        "  - If the transcript states no commitments, return {\"items\": []}.\n\n"
        f"Response JSON schema:\n{_RESPONSE_SCHEMA}\n\n"
        f"meeting_id: {meeting_id}\n"
        f"meeting_title: {title}\n\n"
        "Transcript:\n"
        "<<<\n"
        f"{transcript}\n"
        ">>>\n"
    )


def parse_extraction(raw_text: str) -> list[dict]:
    """Parse the LLM response into a list of validated raw items.

    Discards any item that is not a dict, has an unknown `type`, lacks a non-empty
    `summary`, or whose `evidence_span` is shorter than MIN_SPAN_CHARS. Verbatim/params
    faithfulness is verified later in provenance (S-3); this is structural coercion only.
    """
    obj = _loads(raw_text)
    if not isinstance(obj, dict):
        return []
    items = obj.get("items")
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item_type = raw.get("type")
        if item_type not in _ITEM_TYPES:
            continue
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        span = raw.get("evidence_span")
        if not isinstance(span, str) or len(span) < MIN_SPAN_CHARS:
            continue
        params = raw.get("params")
        if not isinstance(params, dict):
            params = {}
        out.append(
            {
                "type": item_type,
                "summary": summary.strip(),
                "owner": _as_str(raw.get("owner")),
                "due": _as_str(raw.get("due")),
                "evidence_span": span,
                "params": {k: v for k, v in params.items() if isinstance(k, str)},
            }
        )
    return out


def _loads(raw_text: str) -> Any:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    text = raw_text.strip()
    # Tolerate a fenced ```json block.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, RecursionError):
        # last resort: slice the outermost object braces
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError, RecursionError):
                return None
        return None


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""
