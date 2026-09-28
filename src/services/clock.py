"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Injectable server-side UTC clock seam (docs/02_design.md §Clock seam / decision H5).
#
# 🔴 `as_of_utc` is resolved EXACTLY ONCE, at the adapter, through this seam — never
# client-supplied (a client value would be a forgeable overdue basis) and never read by
# nodes (nodes only read the pre-resolved `as_of_utc` string). The seam exists so the
# determinism test can inject a FixedClock and assert the same `as_of_utc` (hence the same
# overdue set) across host timezones. A retry is a new request and legitimately re-reads
# the clock; `expires_at` freshness in the staging contract is computed from this same
# `as_of_utc`, so staging validity is on one server-clock basis.

from __future__ import annotations

from datetime import datetime, timezone


class UtcClock:
    """Server-side wall-clock seam. Returns an aware UTC datetime."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(UtcClock):
    """Test/deterministic clock — always returns the injected instant (aware UTC)."""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._moment = moment.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._moment


def resolve_as_of_utc(clock: UtcClock | None = None) -> str:
    """Resolve the request's overdue/deadline basis as an RFC3339 UTC timestamp.

    Format: ``YYYY-MM-DDThh:mm:ssZ`` (second precision, always ``Z``). Host-timezone
    independent: the underlying instant is converted to UTC before formatting.
    """
    clock = clock or UtcClock()
    moment = clock.now().astimezone(timezone.utc).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
