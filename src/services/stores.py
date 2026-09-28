"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Persistence seams (docs/02_design.md §Ledger scoping, §Staging record contract).
#
# The production seams are the SDK's INTERFACE-ONLY `shared.services.database.BaseDatabase`
# (ledger of record, scoped by WHERE tenant_id/org_id in `params`) and
# `shared.services.vector_store.BaseVectorStore` (semantic recall, scoped by a
# per-(tenant,org) namespace bound at construction). Both concrete impls are STUB in the
# wheel today, so this module ships framework-free, scope-aware in-memory seams for dev /
# CI / STG. A real deployment injects a `BaseDatabase`/`BaseVectorStore`-backed adapter that
# implements the SAME method contract (translating these calls to scoped SQL / a scoped
# collection). Nodes depend only on this contract, never on a concrete backend.
#
# 🔴 Scope is fail-closed: every method requires non-empty tenant_id AND org_id and never
# widens the query. Claim-binding happens upstream (adapter); the store is the last line.

from __future__ import annotations

import copy
import threading
from typing import Any

# staging record status
STAGING_PENDING = "pending"
STAGING_CONSUMED = "consumed"

# feedback promotion reject reasons
FB_ABSENT = "brief_id_absent"
FB_WRONG_OWNER = "brief_id_wrong_owner"
FB_CROSS_SCOPE = "brief_id_cross_scope"
FB_EXPIRED = "brief_id_expired"
FB_CONSUMED = "brief_id_already_consumed"
FB_NOT_STAGED = "item_not_in_candidate_set"


class ScopeError(ValueError):
    """Raised when a store call is made without a complete (tenant_id, org_id) scope."""


def _require_scope(tenant_id: str, org_id: str) -> None:
    if not tenant_id or not org_id:
        raise ScopeError("ledger/recall access requires non-empty tenant_id AND org_id (fail-closed)")


class InMemoryLedgerStore:
    """Scope-aware in-memory ledger of record (dev/CI seam).

    Row shape (active commitment):
        {ledger_entry_id, tenant_id, org_id, meeting_id, owner, due, status,
         summary, params}
    ``status`` in {"open", "active", "completed"}; open+active are the match candidates.
    """

    def __init__(self) -> None:
        # active/open rows keyed by (tenant, org) -> {ledger_entry_id: row}
        self._rows: dict[tuple[str, str], dict[str, dict]] = {}
        # staging rows keyed by brief_id -> record
        self._staging: dict[str, dict] = {}
        # 🔴 the check-then-flip in consume_and_promote must be atomic so two concurrent
        # feedback calls cannot both consume one brief_id (reentrant: consume calls write_active).
        self._lock = threading.RLock()

    # ── read (match) ─────────────────────────────────────────────────────────
    def fetch_open_commitments(self, tenant_id: str, org_id: str) -> list[dict]:
        """Return open actions + active decisions for the scope (never widened)."""
        _require_scope(tenant_id, org_id)
        with self._lock:
            scope = self._rows.get((tenant_id, org_id), {})
            return [copy.deepcopy(r) for r in scope.values() if r.get("status") in ("open", "active")]

    # ── write active (ingest bootstrap / feedback promotion) ─────────────────
    def write_active(self, tenant_id: str, org_id: str, rows: list[dict]) -> int:
        """Idempotent bulk write of active rows. Idempotent on ``ledger_entry_id`` so a
        retried write does not double-apply. Returns the count of rows written/updated."""
        _require_scope(tenant_id, org_id)
        with self._lock:
            scope = self._rows.setdefault((tenant_id, org_id), {})
            written = 0
            for row in rows:
                lid = row.get("ledger_entry_id")
                if not lid:
                    continue
                stored = dict(row)
                stored["tenant_id"] = tenant_id
                stored["org_id"] = org_id
                stored.setdefault("status", "active")
                scope[lid] = stored
                written += 1
            return written

    # ── staging (invoke) ─────────────────────────────────────────────────────
    def stage(self, record: dict) -> None:
        """Persist one invoke staging record keyed by ``brief_id`` (idempotent overwrite
        of the same brief_id, since brief_id is correlation_id-seeded and stable)."""
        _require_scope(record.get("tenant_id", ""), record.get("org_id", ""))
        brief = record.get("brief_id")
        if not brief:
            raise ScopeError("staging record requires a brief_id")
        with self._lock:
            existing = self._staging.get(brief)
            # never resurrect a consumed row on retry of the invoke (defensive).
            if existing and existing.get("status") == STAGING_CONSUMED:
                return
            self._staging[brief] = copy.deepcopy(record)

    def get_staging(self, brief_id: str) -> dict | None:
        with self._lock:
            rec = self._staging.get(brief_id)
            return copy.deepcopy(rec) if rec is not None else None

    def consume_and_promote(
        self,
        brief_id: str,
        tenant_id: str,
        org_id: str,
        created_by: str,
        as_of_iso: str,
        approved_item_ids: list[str],
        active_row_builder: Any,
    ) -> dict:
        """Single-transaction compare-and-set promotion (docs/02 §Staging record contract).

        Promote only if the row is owned by (tenant_id, org_id, created_by), is
        ``pending``, and ``as_of_iso <= expires_at``. The same call flips status to
        ``consumed`` and writes active rows for ``approved_item_ids ∩ candidate_set``.
        Any of {absent, wrong owner, cross-scope, expired, already consumed} -> reject.
        ``approved_item_ids`` not in the candidate set -> reject (cannot approve an item
        that was never presented). An empty approved list consumes the row and writes zero
        active rows.

        ``active_row_builder(candidate: dict) -> dict`` maps a staged candidate to the
        active ledger row to write (owner/due/summary/params from the STAGED candidate,
        never from the request).

        Returns ``{"applied": [...], "rejected": [{"item_id", "reason"}], "reason": ...}``.
        ``reason`` (top-level) is set only on a whole-request reject (row-level rejects go
        in ``rejected``).
        """
        _require_scope(tenant_id, org_id)
        from src.services import normalize

        with self._lock:
            rec = self._staging.get(brief_id)
            if rec is None:
                return {"applied": [], "rejected": [], "reason": FB_ABSENT}
            if rec.get("tenant_id") != tenant_id or rec.get("org_id") != org_id:
                return {"applied": [], "rejected": [], "reason": FB_CROSS_SCOPE}
            if rec.get("created_by") != created_by:
                return {"applied": [], "rejected": [], "reason": FB_WRONG_OWNER}
            if rec.get("status") == STAGING_CONSUMED:
                return {"applied": [], "rejected": [], "reason": FB_CONSUMED}
            # freshness: as_of <= expires_at (both offset-aware).
            expires_at = rec.get("expires_at", "")
            a = normalize.parse_iso(as_of_iso)
            e = normalize.parse_iso(expires_at)
            if a is None or e is None or a > e:
                return {"applied": [], "rejected": [], "reason": FB_EXPIRED}

            candidates = {c.get("item_id"): c for c in rec.get("candidate_set", []) if c.get("item_id")}
            applied: list[str] = []
            rejected: list[dict] = []
            active_rows: list[dict] = []
            seen: set[str] = set()
            for iid in approved_item_ids:
                if iid in seen:  # duplicate approved id -> apply once (idempotent)
                    continue
                seen.add(iid)
                cand = candidates.get(iid)
                if cand is None:
                    rejected.append({"item_id": iid, "reason": FB_NOT_STAGED})
                    continue
                active_rows.append(active_row_builder(cand))
                applied.append(iid)

            # CAS commit: flip to consumed FIRST (consume-once winner), then write actives.
            rec["status"] = STAGING_CONSUMED
            rec["consumed_at"] = as_of_iso
            self._staging[brief_id] = rec
            if active_rows:
                self.write_active(tenant_id, org_id, active_rows)
            return {"applied": applied, "rejected": rejected, "reason": None}


class InMemoryRecallIndex:
    """Scope-aware in-memory semantic recall index (dev/CI seam).

    Real deploy injects a `BaseVectorStore` bound to a per-(tenant,org) namespace, so a
    cross-scope document is unreachable by ``search``. Here scope is an explicit key.
    Similarity is a deterministic token-overlap proxy (no embeddings in the dev seam);
    an empty index returns [] and the caller degrades gracefully.
    """

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], list[dict]] = {}

    def upsert(self, tenant_id: str, org_id: str, documents: list[dict]) -> None:
        _require_scope(tenant_id, org_id)
        bucket = self._docs.setdefault((tenant_id, org_id), [])
        bucket.extend(copy.deepcopy(d) for d in documents)

    def search(self, tenant_id: str, org_id: str, query: str, top_k: int = 8) -> list[dict]:
        _require_scope(tenant_id, org_id)
        from src.services import normalize

        bucket = self._docs.get((tenant_id, org_id), [])
        if not bucket or not query:
            return []
        q_tokens = normalize.signature_tokens(query)
        scored: list[tuple[float, dict]] = []
        for doc in bucket:
            d_tokens = normalize.signature_tokens(str(doc.get("text", "")))
            overlap = normalize.jaccard(q_tokens, d_tokens)
            if overlap > 0:
                scored.append((overlap, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [dict(doc, score=round(score, 4)) for score, doc in scored[: max(top_k, 0)]]
