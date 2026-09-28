"""Framework-free unit tests for src.services.brief (classify + brief build + minutes)."""

from __future__ import annotations

from src.services import brief as b
from src.services import provenance

_AS_OF = "2026-08-05T00:00:00Z"


def _extracted(summary, span, type_="action", owner="", due=""):
    return {"type": type_, "summary": summary, "owner": owner, "due": due, "evidence_span": span, "params": {}}


def test_classify_new_when_no_ledger_match():
    items = [_extracted("在庫を確認する", "担当Aが在庫を確認する")]
    mr = b.classify(items, [], _AS_OF, 0.7)
    assert len(mr["new"]) == 1
    assert not mr["resurfaced"] and not mr["contradictions"] and not mr["completions"]


def test_classify_resurfaced_when_overdue_open_row_matches():
    row = {"ledger_entry_id": "cmt_1", "summary": "在庫確認 担当", "owner": "担当A", "due": "2026-07-01T00:00:00Z", "status": "open"}
    items = [_extracted("在庫確認 担当 の件", "担当Aが在庫確認をまだ実施していない")]
    mr = b.classify(items, [row], _AS_OF, 0.1)
    assert len(mr["resurfaced"]) == 1


def test_classify_completion_marker():
    row = {"ledger_entry_id": "cmt_1", "summary": "在庫確認", "owner": "担当A", "due": "2026-07-01T00:00:00Z", "status": "open"}
    items = [_extracted("在庫確認は完了した", "担当Aが在庫確認を完了した")]
    mr = b.classify(items, [row], _AS_OF, 0.1)
    assert len(mr["completions"]) == 1


def test_classify_one_row_claimed_once():
    # two extracted items match the same row — only one may claim it (1:1 coverage).
    row = {"ledger_entry_id": "cmt_1", "summary": "在庫 確認", "owner": "担当A", "due": "2026-07-01T00:00:00Z", "status": "open"}
    items = [_extracted("在庫 確認 A", "担当Aが在庫確認 その1"), _extracted("在庫 確認 B", "担当Aが在庫確認 その2")]
    mr = b.classify(items, [row], _AS_OF, 0.1)
    claimed = sum(len(mr[k]) for k in ("resurfaced", "contradictions", "completions"))
    assert claimed == 1  # row claimed exactly once; the other becomes new
    assert len(mr["new"]) == 1


def test_build_brief_and_expected_ids_are_consistent():
    row = {"ledger_entry_id": "cmt_1", "summary": "在庫 確認", "owner": "担当A", "due": "2026-07-01T00:00:00Z", "status": "open"}
    items = [_extracted("在庫 確認 の件", "担当Aが在庫確認をまだ")]
    mr = b.classify(items, [row], _AS_OF, 0.1)
    brief_items = b.build_commitment_brief(mr, "M1", _AS_OF)
    expected = b.expected_ledger_ids(mr)
    # every surfaced ledger id appears exactly once in the brief.
    covered = [it["evidence_ids"].get("ledger_entry_id") for it in brief_items if it["category"] != provenance.NEW]
    assert set(covered) == expected


def test_new_item_params_only_owner_due_derived():
    item = _extracted("予算を確定する", "承認者が来週金曜までに予算を確定する", owner="承認者", due="2026-08-14")
    mr = b.classify([item], [], _AS_OF, 0.7)
    brief_items = b.build_commitment_brief(mr, "M1", _AS_OF)
    params = brief_items[0]["params"]
    assert set(params).issubset({"owner", "due", "due_iso", "overdue"})
    assert params["owner"] == "承認者" and params["due"] == "2026-08-14"


def test_candidate_set_carries_meeting_id():
    item = _extracted("予算を確定する", "承認者が来週金曜までに予算を確定する", owner="承認者")
    mr = b.classify([item], [], _AS_OF, 0.7)
    brief_items = b.build_commitment_brief(mr, "M1", _AS_OF)
    candidates = b.build_candidate_set(brief_items, "M1")
    assert candidates and candidates[0]["meeting_id"] == "M1" and candidates[0]["item_id"].startswith("item_")


def test_item_id_distinguishes_same_span_different_summary():
    # Review round 1: two commitments sharing one span/owner/due must NOT collide on item_id.
    x = {"category": provenance.NEW, "summary": "予算を確定", "params": {}, "evidence_ids": {}, "evidence_span": "同じ証拠スパン文字列"}
    y = {"category": provenance.NEW, "summary": "在庫を確認", "params": {}, "evidence_ids": {}, "evidence_span": "同じ証拠スパン文字列"}
    assert b.item_id_for(x, "M1") != b.item_id_for(y, "M1")


def test_ledger_item_summary_from_row_not_llm():
    # Review round 2: a ledger-cited item's summary is the ORIGINAL committed text (row), never the
    # LLM's re-phrasing — faithful by construction (params already render from the row).
    row = {"ledger_entry_id": "cmt_1", "summary": "在庫確認 担当", "owner": "担当A", "due": "2026-07-01T00:00:00Z", "status": "open"}
    items = [_extracted("在庫確認 担当 の件", "担当Aが在庫確認をまだ実施していない")]
    mr = b.classify(items, [row], _AS_OF, 0.1)
    brief_items = b.build_commitment_brief(mr, "M1", _AS_OF)
    ledger = [it for it in brief_items if it["category"] != provenance.NEW]
    assert ledger, "expected a ledger-cited item"
    assert ledger[0]["summary"] == "在庫確認 担当"  # from the cited row, not the LLM item summary


def test_minutes_draft_has_sections():
    items = [_extracted("予算を確定", "予算を確定する決定", type_="decision"), _extracted("在庫確認", "在庫を確認する", owner="担当A")]
    md = b.build_minutes_draft(items, {"title": "定例会議", "date": "2026-08-05"})
    assert "## Decisions" in md and "## Action Items" in md and "定例会議" in md


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK brief: {len(_fns)} tests")
