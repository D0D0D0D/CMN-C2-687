"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# S-3 output-envelope allowlist + residue masking (docs/02_design.md §S-3 output gate).
# Framework-free (plain EnvelopeViolation; the node re-raises it as SecurityViolationError)
# so the allowlist / masking logic is fully unit-testable with the system interpreter.
#
# 🔴 The gate is FAIL-CLOSED with NO state dependency: the envelope self-declares its shape
# via `status` (error) and `mode`, and the gate selects the allowlist from those declared
# values (never from the presence/absence of a key — that would be fail-open). An unknown /
# missing discriminator or an undeclared top-level key raises. The default framework S-3 only
# credential-scans TOP-LEVEL strings, so this adds a RECURSIVE credential/PII residue mask
# over the nested brief.

from __future__ import annotations

from typing import Any, Callable

# Detector seams — guarded so this module stays importable (and testable) without the wheel.
try:  # pragma: no cover — shared is always vendored in CI
    from shared.security.credential_detector import detect_credentials as _detect_credentials
    from shared.security.pii_detector import detect_pii as _detect_pii
except ImportError:  # pragma: no cover
    def _detect_credentials(text: str) -> list:  # type: ignore[misc]
        return []

    def _detect_pii(text: str) -> list:  # type: ignore[misc]
        return []

_CREDENTIAL_MASK = "[MASKED-CREDENTIAL]"
_PII_MASK = "[MASKED]"

# per-discriminator top-level allowlists.
_ERROR_KEYS = frozenset({"mode", "status", "error"})
_ALLOWLIST_BY_MODE: dict[str, frozenset[str]] = {
    "invoke": frozenset({"mode", "status", "minutes_draft", "commitment_brief", "brief_id", "degradation_reason"}),
    "ingest": frozenset({"mode", "status", "ingest_summary", "degradation_reason"}),
    "feedback": frozenset({"mode", "status", "write_result", "degradation_reason"}),
}
_BRIEF_CATEGORY_KEYS = frozenset({"new", "resurfaced", "contradictions", "completions"})
_BRIEF_ITEM_KEYS = frozenset({"item_id", "category", "summary", "params", "evidence_ids", "evidence_span"})
_ERROR_OBJ_KEYS = frozenset({"code", "message"})


class EnvelopeViolation(Exception):
    """S-3 envelope violation (undeclared key / unknown discriminator). The node re-raises
    this as framework SecurityViolationError."""


_VALID_MODES = frozenset({"invoke", "ingest", "feedback"})


def enforce_and_mask(formatted: Any, detectors: tuple[Callable, Callable] | None = None) -> dict:
    """Validate the envelope against its self-declared allowlist and mask residue (FAIL-CLOSED).

    Raises EnvelopeViolation on: non-dict envelope; a `status` that is not `success`/`error`;
    an unknown/missing mode discriminator; an undeclared top-level key; or an `error` value
    that is not a `{code, message}` dict. Every string anywhere in the returned envelope is
    passed through the credential + PII mask (belt-and-suspenders over the field-specific
    masks), so a stray string (e.g. a scalar `error`) can never leave unscanned.
    """
    if not isinstance(formatted, dict):
        raise EnvelopeViolation("formatted_output is missing or not a dict")

    cred_fn, pii_fn = detectors or (_detect_credentials, _detect_pii)

    status = formatted.get("status")
    if status not in ("success", "error"):
        raise EnvelopeViolation(f"invalid status discriminator: {status!r}")

    # discriminator: status=error -> error keys (mode must still be a valid mode or None);
    # status=success -> per-mode allowlist.
    if status == "error":
        mode = formatted.get("mode")
        if mode is not None and mode not in _VALID_MODES:
            raise EnvelopeViolation(f"invalid mode in error envelope: {mode!r}")
        allowlist = _ERROR_KEYS
        # error object is MANDATORY and must be exactly {code, message}.
        err = formatted.get("error")
        if not isinstance(err, dict):
            raise EnvelopeViolation("error envelope requires an error object {code, message}")
        if [k for k in err if k not in _ERROR_OBJ_KEYS]:
            raise EnvelopeViolation("undeclared key in error object")
        # both code and message must be present strings — an empty/partial error object is
        # fail-OPEN (a client-facing error with no code/message). Require the exact shape.
        if not isinstance(err.get("code"), str) or not isinstance(err.get("message"), str):
            raise EnvelopeViolation("error object must carry string code and message")
    else:
        mode = formatted.get("mode")
        allowlist = _ALLOWLIST_BY_MODE.get(mode) if isinstance(mode, str) else None
        if allowlist is None:
            raise EnvelopeViolation(f"unknown/missing mode discriminator: {mode!r}")

    undeclared = [k for k in formatted if k not in allowlist]
    if undeclared:
        raise EnvelopeViolation(f"undeclared top-level key(s): {sorted(undeclared)}")

    out = dict(formatted)

    # nested error object (already shape-validated above).
    if "error" in out:
        out["error"] = {k: out["error"][k] for k in out["error"] if k in _ERROR_OBJ_KEYS}

    # nested commitment_brief (invoke) — item allowlist + drop undeclared-key items.
    if "commitment_brief" in out:
        out["commitment_brief"] = _validate_brief(out["commitment_brief"])

    # 🔴 final defense: recursively mask EVERY string in the whole envelope, so no scalar or
    # nested value can leave unscanned regardless of which field carried it.
    return _mask_obj(out, cred_fn, pii_fn)


def _validate_brief(brief: Any) -> dict:
    """Structure-validate the brief object (masking is done by the final recursive pass)."""
    if not isinstance(brief, dict):
        raise EnvelopeViolation("commitment_brief is not a dict")
    if [k for k in brief if k not in _BRIEF_CATEGORY_KEYS]:
        raise EnvelopeViolation("undeclared category in commitment_brief")
    out: dict[str, list] = {}
    for category in _BRIEF_CATEGORY_KEYS:
        items = brief.get(category, []) or []
        clean: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if [k for k in item if k not in _BRIEF_ITEM_KEYS]:
                # an item smuggling an undeclared key is dropped (never silently emitted).
                continue
            clean.append(item)
        out[category] = clean
    return out


def _mask_obj(obj: Any, cred_fn: Callable, pii_fn: Callable) -> Any:
    if isinstance(obj, str):
        return _mask_text(obj, cred_fn, pii_fn)
    if isinstance(obj, dict):
        return {k: _mask_obj(v, cred_fn, pii_fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_obj(v, cred_fn, pii_fn) for v in obj]
    return obj


def _mask_text(text: str, cred_fn: Callable, pii_fn: Callable) -> str:
    for detector, mask in ((cred_fn, _CREDENTIAL_MASK), (pii_fn, _PII_MASK)):
        try:
            for finding in detector(text):
                value = finding.get("value") if isinstance(finding, dict) else None
                if value:
                    text = text.replace(str(value), mask)
        except Exception:  # noqa: BLE001 — a detector failure must not crash the gate
            pass
    return text
