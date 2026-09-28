"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Adapter request contract (docs/02_design.md §Adapter request contract, decisions H1/H2).
# Pure / framework-free: closed-schema validation, per-mode capability authorization, and
# scope claim-binding all run BEFORE State construction and are exhaustively unit-testable.
# PII masking of the transcript is done by the caller (server.py) with the shared detector
# after this returns — this module never touches the raw bytes' content beyond size/shape.

from __future__ import annotations

from typing import Any

from src.services import normalize

DEFAULT_MODE = "invoke"
_VALID_MODES = ("ingest", "invoke", "feedback")

# 🔴 All THREE write paths carry a capability (invoke is not exempt — it writes a staging
# row). The adapter maps the caller's token/claims to allowed capabilities and rejects a
# disallowed mode at ingress (InvocationContext has no capability field — H2).
_MODE_CAPABILITY = {
    "ingest": "ledger:bootstrap",
    "invoke": "ledger:stage",
    "feedback": "ledger:apply",
}

_TOP_LEVEL_KEYS = frozenset(
    {"mode", "transcript", "meeting_meta", "tenant_id", "org_id", "brief_id", "approved_item_ids"}
)
_META_KEYS = frozenset({"meeting_id", "title", "date", "participants"})

# reject reason codes (adapter emits `adapter_request_rejected` with these; PII-free).
R_UNKNOWN_KEY = "schema_unknown_key"
R_WRONG_TYPE = "schema_wrong_type"
R_MODE_INVALID = "mode_invalid"
R_TRANSCRIPT_MISSING = "transcript_missing"
R_TRANSCRIPT_NOT_ALLOWED = "transcript_not_allowed_for_feedback"
R_META_MISSING = "meeting_meta_missing"
R_META_UNKNOWN_KEY = "meeting_meta_unknown_key"
R_MEETING_ID_MISSING = "meeting_id_missing"
R_SCOPE_MISSING = "scope_claim_missing"
R_SCOPE_FORGED = "scope_body_claim_mismatch"
R_CAPABILITY_DENIED = "capability_denied"
R_BRIEF_ID_MISSING = "brief_id_missing"
R_BRIEF_ID_NOT_ALLOWED = "brief_id_not_allowed"
R_APPROVED_IDS_MISSING = "approved_item_ids_missing"


class Rejection(Exception):
    """Adapter-level reject with a PII-free reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def validate_request(body: Any, claims: dict, as_of_utc: str) -> dict:
    """Validate + normalize a request against the closed schema and the caller's claims.

    ``claims`` = ``{"tenant": str, "org": str, "capabilities": list[str], "caller_id": str}``
    (populated by the auth middleware / entry-point). Returns a normalized dict:
    ``{mode, transcript, meeting_meta, tenant_id, org_id, brief_id, approved_item_ids}``.
    Raises ``Rejection`` (never leaks transcript content) on any violation.
    """
    if not isinstance(body, dict):
        raise Rejection(R_WRONG_TYPE, "request body must be a JSON object")

    unknown = [k for k in body if k not in _TOP_LEVEL_KEYS]
    if unknown:
        raise Rejection(R_UNKNOWN_KEY, f"unknown top-level key(s): {sorted(unknown)}")

    # ── mode (optional; default invoke; outside enum -> reject) ──────────────
    mode = body.get("mode", DEFAULT_MODE)
    if not isinstance(mode, str) or mode not in _VALID_MODES:
        raise Rejection(R_MODE_INVALID, f"mode must be one of {list(_VALID_MODES)}")

    # ── scope claim-binding (H1) — fail-closed ───────────────────────────────
    tenant_id = _bind_scope("tenant_id", body, claims.get("tenant"))
    org_id = _bind_scope("org_id", body, claims.get("org"))

    # ── per-mode capability (H2) ─────────────────────────────────────────────
    required_cap = _MODE_CAPABILITY[mode]
    caps = claims.get("capabilities") or []
    if not isinstance(caps, (list, tuple, set)) or required_cap not in caps:
        raise Rejection(R_CAPABILITY_DENIED, f"caller lacks capability {required_cap!r} for mode={mode!r}")

    value: dict[str, Any] = {"mode": mode, "tenant_id": tenant_id, "org_id": org_id}

    if mode in ("ingest", "invoke"):
        # closed schema: feedback-only fields must not appear for ingest/invoke (silently
        # ignoring them would be a fail-open acceptance of an ill-formed request).
        for forbidden in ("brief_id", "approved_item_ids"):
            if forbidden in body:
                raise Rejection(R_BRIEF_ID_NOT_ALLOWED, f"{forbidden} is not permitted for mode={mode!r}")
        transcript = body.get("transcript")
        if not isinstance(transcript, str) or not transcript:
            raise Rejection(R_TRANSCRIPT_MISSING, f"transcript is required for mode={mode!r}")
        value["transcript"] = transcript
        value["meeting_meta"] = _validate_meta(body.get("meeting_meta"), as_of_utc)
        value["brief_id"] = None
        value["approved_item_ids"] = None
    else:  # feedback
        if "transcript" in body:
            raise Rejection(R_TRANSCRIPT_NOT_ALLOWED, "transcript is not permitted for mode='feedback'")
        brief_id = body.get("brief_id")
        if not isinstance(brief_id, str) or not brief_id:
            raise Rejection(R_BRIEF_ID_MISSING, "brief_id is required for mode='feedback'")
        approved = body.get("approved_item_ids")
        if not isinstance(approved, list) or any(not isinstance(x, str) for x in approved):
            raise Rejection(R_APPROVED_IDS_MISSING, "approved_item_ids (list[str], may be empty) is required")
        value["transcript"] = None
        value["meeting_meta"] = None
        value["brief_id"] = brief_id
        value["approved_item_ids"] = approved

    return value


def _bind_scope(field: str, body: dict, claim_value: Any) -> str:
    """Bind a scope key to the caller's authenticated claim (H1). Body value, if present,
    must EXACTLY equal the claim (else reject); absent -> inject the claim. A missing/empty
    claim is a hard reject (never a broad query)."""
    if not isinstance(claim_value, str) or not claim_value.strip():
        raise Rejection(R_SCOPE_MISSING, f"authenticated {field} claim is required (fail-closed)")
    claim_norm = claim_value.strip()
    if field in body:
        supplied = body[field]
        if not isinstance(supplied, str) or supplied.strip() != claim_norm:
            raise Rejection(R_SCOPE_FORGED, f"{field} in body does not match the caller's authenticated claim")
    return claim_norm


def _validate_meta(meta: Any, as_of_utc: str) -> dict:
    if not isinstance(meta, dict):
        raise Rejection(R_META_MISSING, "meeting_meta is required and must be an object")
    unknown = [k for k in meta if k not in _META_KEYS]
    if unknown:
        raise Rejection(R_META_UNKNOWN_KEY, f"unknown meeting_meta key(s): {sorted(unknown)}")
    meeting_id = meta.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id.strip():
        raise Rejection(R_MEETING_ID_MISSING, "meeting_meta.meeting_id is required and must be non-empty")
    title = meta.get("title", "")
    if not isinstance(title, str):
        raise Rejection(R_WRONG_TYPE, "meeting_meta.title must be a string")
    participants = meta.get("participants", [])
    if not isinstance(participants, list) or any(not isinstance(p, str) for p in participants):
        raise Rejection(R_WRONG_TYPE, "meeting_meta.participants must be a list of strings")
    date = meta.get("date")
    if date is not None and not isinstance(date, str):
        raise Rejection(R_WRONG_TYPE, "meeting_meta.date must be a string")
    return {
        "meeting_id": meeting_id.strip(),
        "title": title,
        "date": date or normalize.date_part(as_of_utc),
        "participants": participants,
    }
