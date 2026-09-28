"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Commitment-brief construction, cross-meeting classification, and the minutes DocGen
# (docs/02_design.md §Inner subgraph, §post_process). Framework-free / deterministic so it
# is unit-testable and reproducible. The brief is ADVISORY — it presents candidates with
# evidence for a human to approve via `feedback`; it never decides on its own.

from __future__ import annotations

from src.services import identifiers, normalize, provenance

# classification markers (deterministic, transparent — the AG only PRESENTS; the human
# decides via feedback, so heuristic edges are surfaced, never auto-applied).
_COMPLETION_MARKERS = ("完了", "済", "解決", "クローズ", "done", "closed", "finished", "resolved")
_CONTRADICTION_MARKERS = ("撤回", "取り消", "取消", "反対", "中止", "覆", "白紙", "reverse", "cancel", "overturn", "rescind")

# active-row fields copied into a ledger-cited brief item's params (faithful by construction).
_LEDGER_PARAM_FIELDS = ("owner", "due", "status")


def classify(extracted_items: list[dict], open_rows: list[dict], as_of_utc: str, score_threshold: float) -> dict:
    """Classify each extracted item against the scoped open/active ledger rows.

    Returns ``{"new": [...], "resurfaced": [...], "contradictions": [...],
    "completions": [...]}`` where each entry is a pre-brief record. Each ledger row is
    claimed by AT MOST ONE item (1:1), so provenance source->claim coverage holds.
    """
    result: dict[str, list[dict]] = {"new": [], "resurfaced": [], "contradictions": [], "completions": []}
    claimed: set[str] = set()

    for item in extracted_items:
        row = _best_match(item, open_rows, score_threshold, claimed)
        text = f"{item.get('summary', '')} {item.get('evidence_span', '')}"
        if row is None:
            result["new"].append(_pre_brief_new(item))
            continue
        claimed.add(row["ledger_entry_id"])
        if _has_marker(text, _COMPLETION_MARKERS):
            result["completions"].append(_pre_brief_ledger(item, row, provenance.COMPLETION))
        elif _has_marker(text, _CONTRADICTION_MARKERS):
            result["contradictions"].append(_pre_brief_ledger(item, row, provenance.CONTRADICTION))
        elif row.get("status") in ("open", "active") and normalize.is_overdue(str(row.get("due", "")), as_of_utc):
            result["resurfaced"].append(_pre_brief_ledger(item, row, provenance.RESURFACED))
        else:
            # matched but not overdue / no marker -> re-affirmation; present as a new candidate.
            result["new"].append(_pre_brief_new(item))
    return result


def build_commitment_brief(match_result: dict, meeting_id: str, as_of_utc: str) -> list[dict]:
    """Build the canonical brief items (docs/02 §S-3 provenance shape). Each item:
    ``{category, summary, params, evidence_ids, evidence_span}``."""
    items: list[dict] = []
    for pre in match_result.get("new", []):
        items.append(_brief_item_new(pre, meeting_id, as_of_utc))
    for category in ("resurfaced", "contradictions", "completions"):
        for pre in match_result.get(category, []):
            items.append(_brief_item_ledger(pre, meeting_id, as_of_utc))
    return items


def expected_ledger_ids(match_result: dict) -> set[str]:
    """Ledger ids the match step surfaced — each MUST appear once in the brief (coverage)."""
    ids: set[str] = set()
    for category in ("resurfaced", "contradictions", "completions"):
        for pre in match_result.get(category, []):
            lid = pre.get("row", {}).get("ledger_entry_id")
            if lid:
                ids.add(lid)
    return ids


def fetched_rows_by_id(open_rows: list[dict]) -> dict[str, dict]:
    return {r["ledger_entry_id"]: r for r in open_rows if r.get("ledger_entry_id")}


# ── bootstrap (ingest) active rows ──────────────────────────────────────────────
def build_active_rows_from_extraction(extracted_items: list[dict], meeting_id: str, as_of_utc: str) -> list[dict]:
    """Ingest bootstrap: turn extracted items into active ledger rows (deterministic ids)."""
    rows: list[dict] = []
    for item in extracted_items:
        iid = identifiers.item_id(
            meeting_id, item.get("summary", ""), item.get("evidence_span", ""), item.get("owner", ""), item.get("due", "")
        )
        lid = identifiers.active_commitment_id("", "", iid)  # scope filled by the store on write
        due_iso = normalize.to_utc_iso(item.get("due", "")) or ""
        rows.append(
            {
                "ledger_entry_id": lid,
                "meeting_id": meeting_id,
                "owner": item.get("owner", ""),
                "due": due_iso or item.get("due", ""),
                "status": "active",
                "summary": item.get("summary", ""),
                "params": {},
            }
        )
    return rows


# ── staging candidate set (invoke) ──────────────────────────────────────────────
def item_id_for(item: dict, meeting_id: str) -> str:
    """Deterministic id of a presented brief item — identical in the presented envelope and
    the staged candidate set, so the human approves by an id they can actually see."""
    params = item.get("params", {}) or {}
    return identifiers.item_id(
        meeting_id,
        str(item.get("summary", "")),
        item.get("evidence_span", ""),
        str(params.get("owner", "")),
        str(params.get("due", "")),
    )


def build_candidate_set(brief_items: list[dict], meeting_id: str) -> list[dict]:
    """The exact item ids + summaries an invoke presented — persisted as the staging
    candidate_set so a stateless feedback call can prove "was presented"."""
    candidates: list[dict] = []
    for item in brief_items:
        params = item.get("params", {})
        iid = item_id_for(item, meeting_id)
        candidates.append(
            {
                "item_id": iid,
                "meeting_id": meeting_id,
                "category": item.get("category"),
                "summary": item.get("summary", ""),
                "owner": params.get("owner", ""),
                "due": params.get("due_iso", params.get("due", "")),
                "status": "active",
            }
        )
    return candidates


def active_row_from_candidate(tenant_id: str, org_id: str):
    """Return a builder(candidate)->active_row bound to the scope (used by feedback CAS).
    The origin meeting_id travels inside each staged candidate, so a stateless feedback call
    needs no meeting_meta."""

    def _builder(candidate: dict) -> dict:
        return {
            "ledger_entry_id": identifiers.active_commitment_id(tenant_id, org_id, candidate["item_id"]),
            "meeting_id": candidate.get("meeting_id", ""),
            "owner": candidate.get("owner", ""),
            "due": candidate.get("due", ""),
            "status": "active",
            "summary": candidate.get("summary", ""),
            "params": {},
        }

    return _builder


# ── DocGen: minutes draft ───────────────────────────────────────────────────────
def build_minutes_draft(extracted_items: list[dict], meeting_meta: dict) -> str:
    """Assemble a Markdown minutes draft (entry-level DocGen). Roles only, no names —
    people are referred to by role in the stored/rendered summary (A-5)."""
    title = str((meeting_meta or {}).get("title", "")) or "Meeting"
    date = str((meeting_meta or {}).get("date", ""))
    lines = [f"# {title}", ""]
    if date:
        lines.append(f"_Date: {date}_")
        lines.append("")
    decisions = [i for i in extracted_items if i.get("type") == "decision"]
    actions = [i for i in extracted_items if i.get("type") == "action"]
    lines.append("## Decisions")
    if decisions:
        for d in decisions:
            lines.append(f"- {d.get('summary', '')}")
    else:
        lines.append("- (none captured)")
    lines.append("")
    lines.append("## Action Items")
    if actions:
        for a in actions:
            owner = a.get("owner", "")
            due = normalize.to_utc_iso(a.get("due", "")) or a.get("due", "")
            suffix = f" — owner: {owner}" if owner else ""
            suffix += f", due: {due}" if due else ""
            lines.append(f"- {a.get('summary', '')}{suffix}")
    else:
        lines.append("- (none captured)")
    lines.append("")
    return "\n".join(lines)


# ── internals ───────────────────────────────────────────────────────────────────
def _best_match(item: dict, open_rows: list[dict], threshold: float, claimed: set[str]) -> dict | None:
    text = f"{item.get('summary', '')} {item.get('evidence_span', '')}"
    q = normalize.signature_tokens(text)
    best: dict | None = None
    best_score = 0.0
    for row in open_rows:
        lid = row.get("ledger_entry_id")
        if not lid or lid in claimed:
            continue
        d = normalize.signature_tokens(str(row.get("summary", "")))
        score = normalize.jaccard(q, d)
        if score > best_score:
            best_score = score
            best = row
    return best if best_score >= threshold else None


def _pre_brief_new(item: dict) -> dict:
    return {"item": item, "row": None}


def _pre_brief_ledger(item: dict, row: dict, category: str) -> dict:
    return {"item": item, "row": row, "category": category}


def _brief_item_new(pre: dict, meeting_id: str, as_of_utc: str) -> dict:
    item = pre["item"]
    due_raw = item.get("due", "")
    due_iso = normalize.to_utc_iso(due_raw) or ""
    params: dict = {"owner": item.get("owner", ""), "due": due_raw}
    if due_iso:
        params["due_iso"] = due_iso
        params["overdue"] = normalize.is_overdue(due_iso, as_of_utc)
    return {
        "category": provenance.NEW,
        "summary": item.get("summary", ""),
        "params": params,
        "evidence_ids": {"meeting_id": meeting_id},
        "evidence_span": item.get("evidence_span", ""),
    }


def _brief_item_ledger(pre: dict, meeting_id: str, as_of_utc: str) -> dict:
    item = pre["item"]
    row = pre["row"]
    category = pre["category"]
    # params AND summary rendered FROM the cited ledger row (faithful by construction). The
    # LLM only decides WHICH row this evidence maps to; the presented summary is the ORIGINAL
    # committed text, never an LLM re-phrasing that could drift from what was committed
    # (a substring-verified span proves a quote exists, not that a free-text summary
    # is faithful). The current-meeting evidence is still shown via evidence_span.
    params: dict = {f: row.get(f, "") for f in _LEDGER_PARAM_FIELDS}
    params["overdue"] = normalize.is_overdue(str(row.get("due", "")), as_of_utc)
    return {
        "category": category,
        "summary": row.get("summary", ""),
        "params": params,
        "evidence_ids": {"meeting_id": meeting_id, "ledger_entry_id": row["ledger_entry_id"]},
        "evidence_span": item.get("evidence_span", ""),
    }


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    low = normalize.nfkc(text).lower()
    return any(m.lower() in low for m in markers)
