"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Deterministic, immutable, unique identifiers (docs/02_design.md §Staging record contract,
# §Data Flow). NO random uuid4:
#   - re-runs (backbone retry / timeout re-send) produce the same value -> idempotent writes
#   - the value never changes across hosts / timezones -> reproducible provenance
#   - 🔴 a uuid4 hex fragment can match the framework S-2 PII patterns (phone_jp / credit_card
#     / my_number_jp) and get [MASKED], which would break downstream ref resolution
#     (a UUID reference can itself be masked by the S-2 PII gate). An
#     alphabetic prefix + sha256 hex never becomes a bare digit run, avoiding that.

from __future__ import annotations

import hashlib
import json
from typing import Any

_BRIEF_PREFIX = "brief"
_STAGE_PREFIX = "stg"
_ACTIVE_PREFIX = "cmt"       # active commitment row id
_ITEM_PREFIX = "item"        # per-decision candidate item id


def _canonical(value: Any) -> str:
    """Deterministic string form of any value (dict keys sorted)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(*parts: Any, length: int = 32) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(_canonical(p).encode("utf-8"))
        h.update(b"\x1f")  # field separator (avoid concatenation collisions)
    return h.hexdigest()[:length]


def brief_id(correlation_id: str, tenant_id: str, org_id: str, meeting_id: str) -> str:
    """Stable per-request staging key (correlation_id-seeded, scoped). Same request ->
    same brief_id (idempotent staging write on retry)."""
    return f"{_BRIEF_PREFIX}_{digest(correlation_id, tenant_id, org_id, meeting_id)}"


def stage_row_id(brief_id_value: str, item_id: str) -> str:
    return f"{_STAGE_PREFIX}_{digest(brief_id_value, item_id)}"


def active_commitment_id(tenant_id: str, org_id: str, item_id: str) -> str:
    """Deterministic active-row id — idempotent on (brief_id/item scope, item_id) so a
    retried promotion does not double-apply (docs/02 §Staging record contract)."""
    return f"{_ACTIVE_PREFIX}_{digest(tenant_id, org_id, item_id)}"


def item_id(meeting_id: str, summary: str, evidence_span: str, owner: str, due: str) -> str:
    """Deterministic per-decision candidate id from the decision's identifying content.
    `summary` is included so two commitments that share one evidence span (e.g. extracted from
    the same sentence) with the same owner/due do NOT collapse to one id."""
    return f"{_ITEM_PREFIX}_{digest(meeting_id, summary, evidence_span, owner, due)}"
