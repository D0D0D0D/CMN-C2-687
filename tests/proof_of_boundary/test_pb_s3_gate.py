"""PB-9 (domain): S-3 output gate — envelope allowlist fail-closed (kind/mode-declared).

docs/02_design.md §S-3 output gate. The gate operates on `result` only (no state): the
envelope self-declares its shape via status(error)/mode, and the gate selects the allowlist
from those declared values (never key presence -> fail-open). Undeclared top-level key or
unknown discriminator raises SecurityViolationError.
"""

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError

from src.nodes.post_process_node import PostProcessNode


def _gate(formatted: dict) -> dict:
    return PostProcessNode()._extra_security_gate_output({"formatted_output": formatted})["formatted_output"]


def test_missing_mode_discriminator_raises():
    with pytest.raises(SecurityViolationError):
        _gate({"status": "success", "minutes_draft": "x"})


def test_unknown_mode_raises():
    with pytest.raises(SecurityViolationError):
        _gate({"mode": "smuggle", "status": "success"})


def test_undeclared_top_level_key_raises():
    with pytest.raises(SecurityViolationError):
        _gate({"mode": "invoke", "status": "success", "minutes_draft": "x", "assessment": "smuggled"})


def test_non_dict_formatted_output_raises():
    with pytest.raises(SecurityViolationError):
        _gate("not a dict")


def test_valid_invoke_passes():
    out = _gate(
        {
            "mode": "invoke",
            "status": "success",
            "minutes_draft": "m",
            "commitment_brief": {"new": [], "resurfaced": [], "contradictions": [], "completions": []},
            "brief_id": "brief_1",
        }
    )
    assert out["mode"] == "invoke"


def test_brief_item_with_undeclared_key_dropped():
    out = _gate(
        {
            "mode": "invoke",
            "status": "success",
            "minutes_draft": "m",
            "commitment_brief": {
                "new": [
                    {"item_id": "i1", "summary": "s", "params": {}, "evidence_ids": {"meeting_id": "M1"}, "evidence_span": "xxxxxxxx"},
                    {"summary": "leak", "verdict": "smuggled"},
                ]
            },
        }
    )
    assert len(out["commitment_brief"]["new"]) == 1


def test_error_envelope_passes():
    out = _gate({"mode": "invoke", "status": "error", "error": {"code": "provenance_failed", "message": "rejected"}})
    assert out["error"]["code"] == "provenance_failed"
