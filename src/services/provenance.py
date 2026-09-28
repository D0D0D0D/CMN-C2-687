"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# S-3 provenance verification (docs/02_design.md §S-3, decision H6). Three-layer +
# bidirectional, FAIL-CLOSED (any failure rejects the whole item set — no partial brief).
# Framework-free so it is exhaustively unit-testable.
#
# A matched substring only proves a quote EXISTS; it does not prove the item's rendered
# numbers/owner/due are faithful. So every rendered field is bound to a checkable source:
#   - ledger-cited items (resurfaced/contradiction/completion): each params value must EQUAL
#     the field of the cited ledger row (closes "valid ID + unrelated span + rewritten numbers").
#   - new items: owner/due must appear VERBATIM within the cited span (after NFKC), so they
#     are literal tokens of the quoted sentence, not prose the LLM invented.

from __future__ import annotations

from src.services import normalize

NEW = "new"
RESURFACED = "resurfaced"
CONTRADICTION = "contradiction"
COMPLETION = "completion"
_LEDGER_CATEGORIES = (RESURFACED, CONTRADICTION, COMPLETION)
_ALL_CATEGORIES = (NEW,) + _LEDGER_CATEGORIES

MIN_SPAN_CHARS = 8

# reject reason codes (surfaced in error_log as "provenance_failed: <code>").
R_UNKNOWN_CATEGORY = "unknown_category"
R_NEW_HAS_LEDGER_ID = "new_item_carries_ledger_entry_id"
R_MEETING_ID_MISMATCH = "meeting_id_mismatch"
R_LEDGER_ID_MISSING = "ledger_id_missing"
R_LEDGER_ID_NOT_FETCHED = "ledger_id_not_in_fetch_set"
R_SPAN_TOO_SHORT = "evidence_span_too_short"
R_SPAN_NOT_FOUND = "evidence_span_not_in_transcript"
R_PARAM_MISMATCH = "param_not_faithful_to_ledger_row"
R_NEW_FIELD_NOT_IN_SPAN = "new_field_not_verbatim_in_span"
R_NO_EVIDENCE = "item_has_no_evidence"
R_ROW_DROPPED = "surfaced_ledger_row_dropped_from_brief"


class ProvenanceResult:
    """Small result carrier (not State — never checkpointed)."""

    __slots__ = ("ok", "reason", "item_index")

    def __init__(self, ok: bool, reason: str | None = None, item_index: int | None = None) -> None:
        self.ok = ok
        self.reason = reason
        self.item_index = item_index

    def as_error_log(self) -> str:
        loc = "" if self.item_index is None else f" (item {self.item_index})"
        return f"provenance_failed: {self.reason}{loc}"


def verify_provenance(
    brief_items: list[dict],
    fetched_rows_by_id: dict[str, dict],
    expected_ledger_ids: set[str],
    meeting_id: str,
    transcript: str,
    as_of_utc: str,
) -> ProvenanceResult:
    """Verify a brief item set. Returns ok=True or the FIRST failure (fail-closed).

    - ``fetched_rows_by_id``: the scoped ledger rows actually read this request (the only
      real-but-related IDs), keyed by ledger_entry_id.
    - ``expected_ledger_ids``: ledger_entry_ids the match step surfaced (resurfaced /
      contradiction / completion) that MUST each be represented by exactly one brief item
      (source->claim completeness).
    """
    norm_transcript = normalize.nfkc(transcript or "")
    covered_ledger_ids: dict[str, int] = {}

    for idx, item in enumerate(brief_items):
        category = item.get("category")
        if category not in _ALL_CATEGORIES:
            return ProvenanceResult(False, R_UNKNOWN_CATEGORY, idx)

        evidence_ids = item.get("evidence_ids") or {}
        span = item.get("evidence_span", "")
        params = item.get("params") or {}

        # (d)(i) claim->source: an item with no evidence at all -> reject.
        if not isinstance(evidence_ids, dict) or not evidence_ids.get("meeting_id"):
            return ProvenanceResult(False, R_NO_EVIDENCE, idx)
        # min length is measured on the STRIPPED span: an all-whitespace 8-char span would
        # otherwise pass the length check and be a vacuous substring of any spaced transcript
        # Require >= MIN_SPAN_CHARS meaningful (non-edge-whitespace) characters.
        if not isinstance(span, str) or len(span.strip()) < MIN_SPAN_CHARS:
            return ProvenanceResult(False, R_SPAN_TOO_SHORT, idx)

        # (a) typed evidence-ID existence.
        if evidence_ids.get("meeting_id") != meeting_id:
            return ProvenanceResult(False, R_MEETING_ID_MISMATCH, idx)

        if category == NEW:
            if evidence_ids.get("ledger_entry_id"):
                return ProvenanceResult(False, R_NEW_HAS_LEDGER_ID, idx)
        else:
            ledger_id = evidence_ids.get("ledger_entry_id")
            if not ledger_id:
                return ProvenanceResult(False, R_LEDGER_ID_MISSING, idx)
            row = fetched_rows_by_id.get(ledger_id)
            if row is None:
                # well-formed but NOT in the fetch set — the "real-but-unrelated ID" attack.
                return ProvenanceResult(False, R_LEDGER_ID_NOT_FETCHED, idx)
            covered_ledger_ids[ledger_id] = covered_ledger_ids.get(ledger_id, 0) + 1

        # (b) verbatim span match against the (NFKC) transcript.
        if normalize.nfkc(span) not in norm_transcript:
            return ProvenanceResult(False, R_SPAN_NOT_FOUND, idx)

        # (c) claim<->source faithfulness.
        if category == NEW:
            span_nfkc = normalize.nfkc(span)
            for field in ("owner", "due"):
                value = params.get(field)
                if value and normalize.nfkc(str(value)) not in span_nfkc:
                    return ProvenanceResult(False, R_NEW_FIELD_NOT_IN_SPAN, idx)
        else:
            row = fetched_rows_by_id[evidence_ids["ledger_entry_id"]]
            if not _params_faithful(params, row):
                return ProvenanceResult(False, R_PARAM_MISMATCH, idx)

    # (d)(ii) source->claim coverage: the cited-ledger-id set must EQUAL the surfaced set,
    # each exactly once. This rejects both a dropped surfaced row AND a ledger item citing a
    # fetched-but-not-surfaced row (the verifier boundary is tightened to multiset equality).
    for ledger_id in expected_ledger_ids:
        if covered_ledger_ids.get(ledger_id, 0) != 1:
            return ProvenanceResult(False, R_ROW_DROPPED)
    for ledger_id, count in covered_ledger_ids.items():
        if ledger_id not in expected_ledger_ids or count != 1:
            return ProvenanceResult(False, R_ROW_DROPPED)

    return ProvenanceResult(True)


def _params_faithful(params: dict, row: dict) -> bool:
    """Every rendered param that names a ledger field must EQUAL that field of the row.

    Covers owner/due/status and any numeric/string param that also exists on the row.
    A field the row does not carry is not constrained here (it is span-checked upstream for
    new items; ledger items only render from the row, so unknown fields are ignored rather
    than trusted). String comparison is NFKC-folded and whitespace-trimmed.
    """
    for key, value in params.items():
        if key in row:
            if _canon(value) != _canon(row[key]):
                return False
    return True


def _canon(value: object) -> str:
    return normalize.nfkc(str(value)).strip()
