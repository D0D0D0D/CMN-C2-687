"""Framework-free unit tests for src.services.request_contract (adapter schema/claim/capability)."""

from __future__ import annotations

from src.services import request_contract as rc

_AS_OF = "2026-08-05T12:00:00Z"
_CLAIMS = {"tenant": "t1", "org": "o1", "capabilities": ["ledger:bootstrap", "ledger:stage", "ledger:apply"], "caller_id": "c1"}


def _reject_reason(body, claims=None):
    try:
        rc.validate_request(body, claims or _CLAIMS, _AS_OF)
    except rc.Rejection as r:
        return r.reason
    return None


def test_invoke_valid_default_mode_injected():
    v = rc.validate_request(
        {"transcript": "meeting text long enough", "meeting_meta": {"meeting_id": "M1"}}, _CLAIMS, _AS_OF
    )
    assert v["mode"] == "invoke"  # absent mode -> default invoke
    assert v["tenant_id"] == "t1" and v["org_id"] == "o1"
    assert v["meeting_meta"]["date"] == "2026-08-05"  # default from as_of
    assert v["meeting_meta"]["title"] == "" and v["meeting_meta"]["participants"] == []


def test_unknown_top_level_key_rejected():
    assert _reject_reason({"mode": "invoke", "transcript": "x", "meeting_meta": {"meeting_id": "M1"}, "junk": 1}) == rc.R_UNKNOWN_KEY


def test_unknown_mode_rejected():
    assert _reject_reason({"mode": "delete_all", "transcript": "x"}) == rc.R_MODE_INVALID


def test_missing_transcript_rejected():
    assert _reject_reason({"mode": "invoke", "meeting_meta": {"meeting_id": "M1"}}) == rc.R_TRANSCRIPT_MISSING


def test_missing_meeting_id_rejected():
    assert _reject_reason({"mode": "invoke", "transcript": "x", "meeting_meta": {"title": "t"}}) == rc.R_MEETING_ID_MISSING


def test_meeting_meta_unknown_key_rejected():
    assert (
        _reject_reason({"mode": "invoke", "transcript": "x", "meeting_meta": {"meeting_id": "M1", "evil": 1}})
        == rc.R_META_UNKNOWN_KEY
    )


def test_scope_claim_missing_rejected():
    claims = dict(_CLAIMS, tenant=None)
    assert _reject_reason({"mode": "invoke", "transcript": "x", "meeting_meta": {"meeting_id": "M1"}}, claims) == rc.R_SCOPE_MISSING


def test_body_scope_forgery_rejected():
    body = {"mode": "invoke", "transcript": "x", "meeting_meta": {"meeting_id": "M1"}, "tenant_id": "t2"}
    assert _reject_reason(body) == rc.R_SCOPE_FORGED


def test_body_scope_matching_claim_ok():
    body = {"mode": "invoke", "transcript": "x", "meeting_meta": {"meeting_id": "M1"}, "tenant_id": "t1", "org_id": "o1"}
    v = rc.validate_request(body, _CLAIMS, _AS_OF)
    assert v["tenant_id"] == "t1"


def test_capability_denied_rejected():
    claims = dict(_CLAIMS, capabilities=["ledger:stage"])  # lacks bootstrap
    assert _reject_reason({"mode": "ingest", "transcript": "x", "meeting_meta": {"meeting_id": "M1"}}, claims) == rc.R_CAPABILITY_DENIED


def test_feedback_valid():
    v = rc.validate_request({"mode": "feedback", "brief_id": "brief_1", "approved_item_ids": ["item_a"]}, _CLAIMS, _AS_OF)
    assert v["mode"] == "feedback" and v["brief_id"] == "brief_1" and v["approved_item_ids"] == ["item_a"]


def test_feedback_empty_approved_ids_ok():
    v = rc.validate_request({"mode": "feedback", "brief_id": "brief_1", "approved_item_ids": []}, _CLAIMS, _AS_OF)
    assert v["approved_item_ids"] == []


def test_feedback_missing_brief_id_rejected():
    assert _reject_reason({"mode": "feedback", "approved_item_ids": []}) == rc.R_BRIEF_ID_MISSING


def test_feedback_transcript_not_allowed():
    assert (
        _reject_reason({"mode": "feedback", "brief_id": "b", "approved_item_ids": [], "transcript": "x"})
        == rc.R_TRANSCRIPT_NOT_ALLOWED
    )


def test_feedback_approved_wrong_type_rejected():
    assert _reject_reason({"mode": "feedback", "brief_id": "b", "approved_item_ids": [1, 2]}) == rc.R_APPROVED_IDS_MISSING


def test_invoke_with_feedback_only_fields_rejected():
    # Review round 2: closed schema — feedback-only fields must not appear for ingest/invoke (silently
    # ignoring them would be a fail-open acceptance).
    assert (
        _reject_reason({"mode": "invoke", "transcript": "x" * 20, "meeting_meta": {"meeting_id": "M1"}, "brief_id": "b"})
        == rc.R_BRIEF_ID_NOT_ALLOWED
    )
    assert (
        _reject_reason({"mode": "invoke", "transcript": "x" * 20, "meeting_meta": {"meeting_id": "M1"}, "approved_item_ids": []})
        == rc.R_BRIEF_ID_NOT_ALLOWED
    )


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK request_contract: {len(_fns)} tests")
