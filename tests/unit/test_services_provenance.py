"""Framework-free unit tests for src.services.provenance (S-3 three-layer + bidirectional).

Dual-mode: pytest collects the test_* functions in CI; runnable directly with the system
interpreter (`python3 tests/unit/test_services_provenance.py`) because it imports no framework.
"""

from __future__ import annotations

from src.services import provenance

_TRANSCRIPT = "承認者は来週金曜までに予算案を確定すると決定した。担当Aが在庫を確認する。"
_MEETING = "MTG-2026-08-05-001"
_AS_OF = "2026-08-05T00:00:00Z"


def _new_item(span="来週金曜までに予算案を確定", owner="", due=""):
    params = {}
    if owner:
        params["owner"] = owner
    if due:
        params["due"] = due
    return {
        "category": provenance.NEW,
        "summary": "予算案を確定する",
        "params": params,
        "evidence_ids": {"meeting_id": _MEETING},
        "evidence_span": span,
    }


def _ledger_row(lid="cmt_x", owner="担当A", due="2026-07-01T00:00:00Z", status="open"):
    return {"ledger_entry_id": lid, "owner": owner, "due": due, "status": status, "summary": "在庫確認"}


def _ledger_item(row, category=provenance.RESURFACED, span="担当Aが在庫を確認"):
    return {
        "category": category,
        "summary": "在庫確認の再浮上",
        "params": {"owner": row["owner"], "due": row["due"], "status": row["status"]},
        "evidence_ids": {"meeting_id": _MEETING, "ledger_entry_id": row["ledger_entry_id"]},
        "evidence_span": span,
    }


def test_valid_new_item_passes():
    res = provenance.verify_provenance([_new_item()], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert res.ok, res.reason


def test_new_item_with_ledger_id_rejected():
    item = _new_item()
    item["evidence_ids"]["ledger_entry_id"] = "cmt_x"
    res = provenance.verify_provenance([item], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_NEW_HAS_LEDGER_ID


def test_span_too_short_rejected():
    res = provenance.verify_provenance([_new_item(span="短い")], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_SPAN_TOO_SHORT


def test_span_not_in_transcript_rejected():
    res = provenance.verify_provenance(
        [_new_item(span="この文は議事録に存在しない長い文字列")], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF
    )
    assert not res.ok and res.reason == provenance.R_SPAN_NOT_FOUND


def test_meeting_id_mismatch_rejected():
    item = _new_item()
    item["evidence_ids"]["meeting_id"] = "OTHER-MEETING"
    res = provenance.verify_provenance([item], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_MEETING_ID_MISMATCH


def test_new_owner_not_in_span_rejected():
    # owner claims a name that is NOT a verbatim token of the cited span.
    item = _new_item(span="来週金曜までに予算案を確定", owner="幽霊担当者")
    res = provenance.verify_provenance([item], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_NEW_FIELD_NOT_IN_SPAN


def test_ledger_item_real_but_unrelated_id_rejected():
    row = _ledger_row(lid="cmt_real")
    item = _ledger_item(row)
    item["evidence_ids"]["ledger_entry_id"] = "cmt_unrelated_but_wellformed"
    # fetch set contains only the real row, NOT the unrelated id.
    res = provenance.verify_provenance([item], {"cmt_real": row}, {"cmt_real"}, _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_LEDGER_ID_NOT_FETCHED


def test_ledger_item_param_rewrite_rejected():
    # LLM rewrote the due date in params — must NOT be faithful to the cited row.
    row = _ledger_row(lid="cmt_real", due="2026-07-01T00:00:00Z")
    item = _ledger_item(row)
    item["params"]["due"] = "2099-12-31T00:00:00Z"  # tampered
    res = provenance.verify_provenance([item], {"cmt_real": row}, {"cmt_real"}, _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_PARAM_MISMATCH


def test_ledger_item_faithful_passes():
    row = _ledger_row(lid="cmt_real")
    item = _ledger_item(row)
    res = provenance.verify_provenance([item], {"cmt_real": row}, {"cmt_real"}, _MEETING, _TRANSCRIPT, _AS_OF)
    assert res.ok, res.reason


def test_surfaced_row_dropped_from_brief_rejected():
    # match surfaced two rows but the brief only covers one (silent omission).
    row1 = _ledger_row(lid="cmt_1")
    item = _ledger_item(row1)
    res = provenance.verify_provenance(
        [item], {"cmt_1": row1, "cmt_2": _ledger_row(lid="cmt_2")}, {"cmt_1", "cmt_2"}, _MEETING, _TRANSCRIPT, _AS_OF
    )
    assert not res.ok and res.reason == provenance.R_ROW_DROPPED


def test_whitespace_only_span_rejected():
    # Review round 2: 8 whitespace chars satisfy the raw length but are a vacuous substring of any
    # spaced transcript — the min-length check runs on the STRIPPED span, so this rejects.
    res = provenance.verify_provenance([_new_item(span="        ")], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_SPAN_TOO_SHORT


def test_item_no_evidence_rejected():
    item = _new_item()
    item["evidence_ids"] = {}
    res = provenance.verify_provenance([item], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert not res.ok and res.reason == provenance.R_NO_EVIDENCE


def test_empty_brief_with_no_expected_rows_passes():
    res = provenance.verify_provenance([], {}, set(), _MEETING, _TRANSCRIPT, _AS_OF)
    assert res.ok


def test_ledger_item_cited_but_not_surfaced_rejected():
    # multiset-equality boundary (review round 1): a brief item citing a fetched row that was
    # NOT surfaced by match must reject, even if the expected row is also covered.
    row1 = _ledger_row(lid="cmt_1")
    row2 = _ledger_row(lid="cmt_2")
    item1 = _ledger_item(row1, span="担当Aが在庫を確認")
    item2 = _ledger_item(row2, span="来週金曜までに予算案を確定")
    res = provenance.verify_provenance(
        [item1, item2], {"cmt_1": row1, "cmt_2": row2}, {"cmt_1"}, _MEETING, _TRANSCRIPT, _AS_OF
    )
    assert not res.ok and res.reason == provenance.R_ROW_DROPPED


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK provenance: {len(_fns)} tests")
