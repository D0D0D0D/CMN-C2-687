"""Framework-free unit tests for src.services.stores (scoping + consume-once staging CAS)."""

from __future__ import annotations

from src.services import brief as brief_svc
from src.services import stores


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


def _staging_record(brief_id="brief_1", tenant="t1", org="o1", by="caller1", expires="2026-08-12T00:00:00Z"):
    return {
        "brief_id": brief_id,
        "tenant_id": tenant,
        "org_id": org,
        "created_by": by,
        "candidate_set": [
            {"item_id": "item_a", "meeting_id": "M1", "summary": "s", "owner": "担当A", "due": "", "status": "active"}
        ],
        "issued_at": "2026-08-05T00:00:00Z",
        "expires_at": expires,
        "status": stores.STAGING_PENDING,
        "consumed_at": None,
    }


def test_scope_required_fetch():
    store = stores.InMemoryLedgerStore()
    assert _raises(stores.ScopeError, lambda: store.fetch_open_commitments("", "o1"))
    assert _raises(stores.ScopeError, lambda: store.fetch_open_commitments("t1", ""))


def test_write_active_is_idempotent():
    store = stores.InMemoryLedgerStore()
    row = {"ledger_entry_id": "cmt_1", "summary": "x", "status": "active"}
    store.write_active("t1", "o1", [row])
    store.write_active("t1", "o1", [row])  # retry
    rows = store.fetch_open_commitments("t1", "o1")
    assert len(rows) == 1


def test_cross_scope_is_invisible():
    store = stores.InMemoryLedgerStore()
    store.write_active("t1", "o1", [{"ledger_entry_id": "cmt_1", "status": "active"}])
    assert store.fetch_open_commitments("t2", "o1") == []
    assert store.fetch_open_commitments("t1", "o2") == []


def test_feedback_valid_promotion():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_a"], builder)
    assert res["reason"] is None
    assert res["applied"] == ["item_a"]
    assert len(store.fetch_open_commitments("t1", "o1")) == 1


def test_feedback_replay_of_consumed_is_rejected():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_a"], builder)
    # replay
    res = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_a"], builder)
    assert res["reason"] == stores.FB_CONSUMED


def test_feedback_cross_scope_rejected():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t2", "o1")
    res = store.consume_and_promote("brief_1", "t2", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_a"], builder)
    assert res["reason"] == stores.FB_CROSS_SCOPE


def test_feedback_wrong_owner_rejected():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_1", "t1", "o1", "attacker", "2026-08-06T00:00:00Z", ["item_a"], builder)
    assert res["reason"] == stores.FB_WRONG_OWNER


def test_feedback_expired_rejected():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record(expires="2026-08-05T00:00:00Z"))
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-10T00:00:00Z", ["item_a"], builder)
    assert res["reason"] == stores.FB_EXPIRED


def test_feedback_absent_brief_rejected():
    store = stores.InMemoryLedgerStore()
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_missing", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_a"], builder)
    assert res["reason"] == stores.FB_ABSENT


def test_feedback_item_not_in_candidate_set_rejected_per_item():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_ghost"], builder)
    assert res["reason"] is None
    assert res["applied"] == []
    assert res["rejected"] == [{"item_id": "item_ghost", "reason": stores.FB_NOT_STAGED}]


def test_feedback_empty_approval_consumes_row_zero_writes():
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", [], builder)
    assert res == {"applied": [], "rejected": [], "reason": None}
    assert store.fetch_open_commitments("t1", "o1") == []
    # row is now consumed — a second call rejects.
    res2 = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", [], builder)
    assert res2["reason"] == stores.FB_CONSUMED


def test_duplicate_approved_ids_applied_once():
    # Review round 1: duplicate approved ids must not double-apply.
    store = stores.InMemoryLedgerStore()
    store.stage(_staging_record())
    builder = brief_svc.active_row_from_candidate("t1", "o1")
    res = store.consume_and_promote("brief_1", "t1", "o1", "caller1", "2026-08-06T00:00:00Z", ["item_a", "item_a"], builder)
    assert res["applied"] == ["item_a"]
    assert len(store.fetch_open_commitments("t1", "o1")) == 1


def test_recall_index_scoped():
    idx = stores.InMemoryRecallIndex()
    idx.upsert("t1", "o1", [{"id": "d1", "text": "エンジン 異音 点検"}])
    assert idx.search("t1", "o1", "エンジン 異音", 5)
    assert idx.search("t2", "o1", "エンジン 異音", 5) == []


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK stores: {len(_fns)} tests")
