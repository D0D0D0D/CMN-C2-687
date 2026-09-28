"""Framework-free unit tests for src.services.envelope (S-3 allowlist + residue mask)."""

from __future__ import annotations

from src.services import envelope as env


def _raises(fn):
    try:
        fn()
    except env.EnvelopeViolation:
        return True
    return False


# detector doubles so the mask traversal is testable without the wheel.
def _fake_cred(text):
    return [{"value": "sk-SECRET"}] if "sk-SECRET" in text else []


def _fake_pii(text):
    return [{"value": "田中太郎"}] if "田中太郎" in text else []


_DET = (_fake_cred, _fake_pii)


def test_invoke_valid_passes():
    out = env.enforce_and_mask(
        {"mode": "invoke", "status": "success", "minutes_draft": "m", "commitment_brief": {"new": []}, "brief_id": "b"}
    )
    assert out["mode"] == "invoke"


def test_undeclared_top_level_key_raises():
    assert _raises(lambda: env.enforce_and_mask({"mode": "invoke", "status": "success", "secret": "x"}))


def test_unknown_mode_raises():
    assert _raises(lambda: env.enforce_and_mask({"mode": "smuggle", "status": "success"}))


def test_non_dict_raises():
    assert _raises(lambda: env.enforce_and_mask("not a dict"))


def test_error_envelope_allowlist():
    out = env.enforce_and_mask({"mode": "invoke", "status": "error", "error": {"code": "quality_gate", "message": "bad"}})
    assert out["error"]["code"] == "quality_gate"


def test_error_object_undeclared_key_raises():
    assert _raises(
        lambda: env.enforce_and_mask({"mode": "invoke", "status": "error", "error": {"code": "x", "message": "y", "z": 1}})
    )


def test_brief_item_undeclared_key_dropped():
    out = env.enforce_and_mask(
        {
            "mode": "invoke",
            "status": "success",
            "commitment_brief": {
                "new": [
                    {"summary": "s", "params": {}, "evidence_ids": {"meeting_id": "M1"}, "evidence_span": "xxxxxxxx"},
                    {"summary": "leak", "verdict": "smuggled"},  # undeclared key -> dropped
                ]
            },
        }
    )
    assert len(out["commitment_brief"]["new"]) == 1


def test_recursive_mask_reaches_nested_strings():
    out = env.enforce_and_mask(
        {
            "mode": "invoke",
            "status": "success",
            "minutes_draft": "contact sk-SECRET now",
            "commitment_brief": {
                "new": [
                    {
                        "summary": "田中太郎 が担当",
                        "params": {"owner": "田中太郎"},
                        "evidence_ids": {"meeting_id": "M1"},
                        "evidence_span": "some span text",
                    }
                ]
            },
        },
        detectors=_DET,
    )
    assert "sk-SECRET" not in out["minutes_draft"]
    item = out["commitment_brief"]["new"][0]
    assert "田中太郎" not in item["summary"]
    assert "田中太郎" not in item["params"]["owner"]


def test_ingest_envelope_allowlist():
    out = env.enforce_and_mask({"mode": "ingest", "status": "success", "ingest_summary": {"written": 3}})
    assert out["ingest_summary"]["written"] == 3


def test_feedback_envelope_allowlist():
    out = env.enforce_and_mask({"mode": "feedback", "status": "success", "write_result": {"applied": ["item_a"], "rejected": []}})
    assert out["write_result"]["applied"] == ["item_a"]


# ── fail-closed hardening (review round 1) ──────────────────────────────────────────
def test_scalar_error_value_raises_not_leaks():
    # the exact bypass the review found: a string `error` carrying a credential must RAISE.
    assert _raises(lambda: env.enforce_and_mask({"mode": "invoke", "status": "error", "error": "contact sk-SECRETVALUE1234567890"}))


def test_error_object_required():
    assert _raises(lambda: env.enforce_and_mask({"mode": "invoke", "status": "error"}))


def test_invalid_status_raises():
    assert _raises(lambda: env.enforce_and_mask({"mode": "invoke", "status": "weird"}))


def test_error_object_empty_raises():
    # Review round 2: {code,message} must both be present strings — an empty error object is fail-open.
    assert _raises(lambda: env.enforce_and_mask({"mode": "invoke", "status": "error", "error": {}}))


def test_error_object_missing_message_raises():
    assert _raises(lambda: env.enforce_and_mask({"mode": "invoke", "status": "error", "error": {"code": "x"}}))


def test_untrusted_mode_in_error_envelope_raises():
    assert _raises(lambda: env.enforce_and_mask({"mode": "untrusted", "status": "error", "error": {"code": "x", "message": "y"}}))


def test_error_message_credential_masked():
    out = env.enforce_and_mask({"mode": "invoke", "status": "error", "error": {"code": "c", "message": "田中太郎 called"}}, detectors=_DET)
    assert "田中太郎" not in out["error"]["message"]


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK envelope: {len(_fns)} tests")
