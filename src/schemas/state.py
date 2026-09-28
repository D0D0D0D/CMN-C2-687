"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# ADR-005: State is a flat TypedDict (msgpack-serializable primitives + JSON types only).
# NO Pydantic / dataclass / arbitrary objects / credentials / InvocationContext.
# docs/02_design.md §State Definition.
#
# 🔴 No raw transcript / full minutes are ever written to the ledger — the ledger stores
# short summaries + evidence references only. The transcript in user_input / normalized_
# transcript is transient and, because mode separation uses NO checkpointer, is never
# checkpointed. Personal names are masked at the adapter (S-2) before entering State.

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state (flat TypedDict). Shared fields (user_input, status, session_id,
    node_history, error_log, hitl_*, etc.) are inherited from AgentState. Field
    requiredness is read as "phase x mode" (docs/02)."""

    # ── adapter / pre_process establish ──────────────────────────────────────
    mode: str                      # invoke / ingest / feedback (adapter-validated)
    normalized_transcript: str     # post-quality-gate text used by extraction (transient)
    meeting_meta: dict             # {meeting_id, title, date, participants}
    tenant_id: str                 # ledger scope key — fail-closed
    org_id: str                    # ledger scope key — fail-closed
    as_of_utc: str                 # RFC3339 UTC, fixed once at adapter (overdue basis)
    brief_id: str                  # invoke: generated staging key; feedback: input key
    approved_item_ids: list        # feedback only: item ids to promote

    # ── inner intermediates / outputs ────────────────────────────────────────
    extracted_items: list          # structured decisions/actions with params + verbatim span
    match_result: dict             # {new, resurfaced, contradictions, completions}
    commitment_brief: list         # canonical brief items (category-discriminated evidence_ids)
    write_result: dict             # feedback only: {applied, rejected}
    ingest_summary: dict           # ingest only: {written, ...}

    # ── DocGen / envelope ────────────────────────────────────────────────────
    minutes_draft: str             # DocGen minutes draft (Markdown)
    formatted_output: dict         # allowlisted output envelope (post_process)
    degradation_reason: str        # set (with status=success) when ledger/KB empty or seam unwired
