# Template Design Specification — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent

> This specification defines the implemented scope and approval workflow. The SDK contract was
> confirmed against the installed
> `agenticstar-agentcore==1.0.0` wheel (spike verified: `framework/nodes/graph_node.py`,
> `framework/nodes/function_node.py`, `framework/graph/agent_base_graph.py`,
> `shared/services/database/base_db.py`, `shared/services/vector_store/base_vector_store.py`).

## Position in AgentCore Architecture

- **Agent Class**: `MeetingCommitmentLedgerAgent`
- **L1 Base**: **AgentBaseGraph** (L1-direct). *(AutonomousBaseGraph not used — fixed pipeline, not a think→act loop.)*
- **Conceptual pattern**: DocGenerationAgent (structured brief/minutes generation). This is a
  *pattern reference*, not an inheritance target — the class inherits `AgentBaseGraph` directly.
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()`; `main` composes an inner subgraph via `GraphNode`)

## Modes (dispatched by the input `mode` field)

| Mode | Purpose | Ledger effect | Notes |
|------|---------|---------------|-------|
| `ingest` | Bootstrap the ledger from past minutes | **Bulk write of active rows** (bootstrap load) | Extract-and-store; no brief/contradiction output |
| `invoke` | Match a new meeting → **present** a commitment brief | **No active-commitment write**; writes only a scoped, expiring *staging* record (proposed candidates) keyed by `brief_id` | Default mode. Produces minutes draft + brief + `brief_id` |
| `feedback` | Promote human-approved staged candidates → active ledger rows | **The only path that creates per-decision active commitments** | Takes `brief_id` + `approved_item_ids`; stateless |

> "Only write path" is precise here: **`feedback` is the only path that creates per-decision
> active commitments**; `ingest` performs a bulk bootstrap write; `invoke` writes only a
> short-lived, scoped staging record (never an active commitment). The `brief_id` staging
> record is how a stateless `feedback` call proves an approved item was actually presented —
> there is no checkpointer (mode separation), so the presented candidate set is persisted as
> scoped staging rows rather than held in a suspended session.

### Adapter request contract (`src/api/server.py`)

The API adapter validates and normalizes the request **before** State construction. The request
schema is closed (`additionalProperties: false`, top-level **and** nested); unknown keys and
wrong types are rejected (HTTP 4xx) before any node runs.

| Field | Type | ingest | invoke | feedback | Absent behaviour |
|-------|------|--------|--------|----------|------------------|
| `mode` | enum `{ingest,invoke,feedback}` | optional | optional | optional | **optional with default**: adapter injects `default_mode` (`invoke`) when absent; any value outside the enum → reject |
| `transcript` | str (text/CSV) | required | required | — (rejected if present) | reject for ingest/invoke |
| `meeting_meta` | obj (closed): `meeting_id:str` (required, non-empty), `title:str`, `date:str(YYYY-MM-DD)`, `participants:list[str]` | required | required | — | `meeting_id` required, non-empty (reject if absent/empty); `title=""`,`participants=[]`,`date=`(date part of `as_of_utc`); unknown nested key → reject |
| `tenant_id` | str | claim-bound | claim-bound | claim-bound | **body-optional, claim-injected**: if present in body it must equal the caller's authenticated `tenant` claim (mismatch → reject); if absent, adapter injects the claim value; claim absent/empty → reject (fail-closed) |
| `org_id` | str | claim-bound | claim-bound | claim-bound | **body-optional, claim-injected**: if present in body it must equal the caller's authenticated `org` claim (mismatch → reject); if absent, adapter injects the claim value; claim absent/empty → reject (fail-closed) |
| `brief_id` | str | — | — | required | reject for feedback |
| `approved_item_ids` | list[str] | — | — | required (may be empty) | empty list = "approve nothing" (valid) |

**How the adapter seeds graph State (SDK reality).** `BaseGraph.invoke(user_input, session_id,
ctx, input_context)` seeds only `user_input`, `input_context`, and identity/trust fields
(verified in wheel `base_graph.py`). So the adapter:
- passes the **PII-masked transcript as `user_input`** (see S-2) — this is one of the fields the
  framework default S-2 scans (`user_input`/`validated_input`/`llm_response`), so the transcript
  is re-scanned automatically at each node's `__call__`;
- passes request metadata (`mode`, `meeting_meta`, `tenant_id`, `org_id`, `brief_id`,
  `approved_item_ids`, `as_of_utc`) via **`input_context`**.

`pre_process` reads `user_input` + `input_context`, derives `normalized_transcript`, and copies
the validated scope/mode fields onto flat State. Raw text is never passed to `invoke()` — only
the masked text.

- **PII scanning happens at the adapter** (see S-2 below) — the raw `transcript` is masked with
  the shared `detect_pii` at ingress, so raw utterances never enter State; the framework S-2 then
  re-scans the masked text carried in `user_input`.
- **`as_of_utc` is resolved exactly once, here, at the adapter** as an RFC3339 UTC timestamp
  (`YYYY-MM-DDThh:mm:ssZ`) and passed via `input_context`; `meeting_meta.date` defaults to its
  `YYYY-MM-DD` component. Nodes only *read* `as_of_utc`; they never call the clock. This keeps
  overdue/deadline computation host-timezone-independent and stable within a request.
  - **Clock seam.** The adapter reads the wall clock through an **injectable server-side `UtcClock`
    seam**, never client-supplied: the request schema has **no `as_of_utc` field** (a client value
    would be a forgeable overdue basis, so it is not accepted). The seam exists so the implementation
    determinism test can inject a **fixed test clock** and assert the same `as_of_utc` (hence the
    same overdue set) across host timezones. A retry is a new request and legitimately re-reads the
    clock; the `expires_at` freshness in the staging contract is likewise computed from this
    `as_of_utc`, so staging validity is on the same server-clock basis.

## Architecture Overview

The AGENTIC STAR Marketplace adapter (`cli.py`) exposes only `invoke`, discards runner-supplied context,
and injects a read-only ledger seam; leaving the ledger unwired would degrade `MatchNode` and empty the commitment brief.

### Node Configuration (outer graph)

| Node | Responsibility | Reads | Writes | Inherits/Overrides |
|------|---------------|-------|--------|-------------------|
| initialize | default | — | — | InitializeNode (default) |
| pre_process | Input quality gate (S-2 quarantine), normalize, validate `mode`, confirm `tenant_id`/`org_id` present, read `as_of_utc` | `user_input` (masked transcript), `input_context` (`mode`,`meeting_meta`,`tenant_id`,`org_id`,`as_of_utc`,`brief_id`,`approved_item_ids`) | `mode`,`normalized_transcript`,`tenant_id`,`org_id`,`as_of_utc`,`meeting_meta`,`status` | **FunctionNode** (override `execute` + `_extra_security_gate_input`) |
| main | Compose the inner ledger workflow subgraph. **Override `execute()` to short-circuit** (`return {}`) when upstream `status=="error"`, else delegate to `GraphNode.execute()` | `mode`,`normalized_transcript`,`tenant_id`,`org_id`,`as_of_utc` | `extracted_items`,`match_result`,`commitment_brief`,`brief_id`,`write_result` (feedback) | **GraphNode** (override `execute`/`get_subgraph`/`extract_input`/`merge_output`) |
| post_process | DocGen: assemble minutes draft + brief (Markdown); build the allowlisted `formatted_output` envelope; S-3 envelope allowlist runs here | `extracted_items`,`match_result`,`commitment_brief`,`brief_id`,`write_result` (feedback),`mode` | `minutes_draft`,`formatted_output`,`status` | **FunctionNode** (override `execute` + `_extra_security_gate_output`) |
| finalize | default | — | — | FinalizeNode (default) |

> **`main` short-circuit is a required `execute()` override (SDK reality).** `pre→main` is an
> unconditional edge and `route()` runs *after* `main`; the stock `GraphNode.execute()` always
> obtains and invokes the subgraph. So a `status="error"` set in `pre_process` would still run
> the subgraph unless `main.execute()` checks `state.get("status")=="error"` first and returns
> `{}`. This override is mandatory (verified against wheel `graph_node.py`/`agent_base_graph.py`).

> **Error routing + error envelope (explicit).** `AgentBaseGraph.route()` reads `state["status"]`
> after `main`: `success`→`post_process`, `retry`→`pre_process`, **anything else (incl. `error`)→
> `finalize`** (verified against wheel `agent_base_graph.py`). So on any
> reject / upstream-error short-circuit, **`post_process` is skipped and does not build the
> envelope**. To keep the output shape consistent, **whichever node sets `status="error"`
> (`pre_process` quality-gate reject, inner provenance reject surfaced by `main`, or `main`'s
> short-circuit) also writes a minimal allowlisted `formatted_output = {mode, status: "error",
> error: {code, message}}`** where `code` is a fixed reason enum (`quality_gate`, `mode_invalid`,
> `scope_missing`, `provenance_failed`, `brief_id_invalid`, `subgraph_error`) and `message` is a
> fixed reason string that carries **no transcript content and no PII**. `finalize` surfaces
> `state["formatted_output"]` unchanged. `error` is an allowlisted envelope key (see S-3). This
> makes the reject/short-circuit path produce a deterministic, envelope-shaped result rather than
> an unset `formatted_output`.

### Inner subgraph — `LedgerWorkflowGraph` (also `AgentBaseGraph`, composed by `main`'s `GraphNode`)

The inner pipeline is fixed (extract → match → brief); **mode selects behaviour inside the
nodes**, not the node set:

| Inner node | ingest | invoke | feedback | Security |
|------|--------|--------|----------|----------|
| pre_process (`extract`) | LLM structured extraction of decisions/actions (owner/due/verbatim `evidence_span`) + structured `params` | same | *(feedback does not re-extract — see below)* | FunctionNode; scope-bound |
| main (`match`/`apply`) | write extracted rows as **active** (bootstrap) | DB `fetch_many`(open+active, scoped) + KB `search` → classify contradiction/resurface/completion; write staged candidates | load staged candidates by `brief_id`, verify ownership+freshness, **promote approved → active** | FunctionNode; `required_trust_level=VERIFIED_EXTERNAL` |
| post_process (`brief`) | minimal load summary | build fixed-schema brief with `evidence_ids`; run S-3 provenance (below) | build write-result summary | FunctionNode; `_extra_security_gate_output` = S-3 provenance |

> **`feedback` path.** `feedback` is a distinct stateless call. Its `match`/`apply` node loads
> the staged candidate set persisted under `brief_id` during the prior `invoke`, verifies the
> `brief_id` is owned by the same `tenant_id`/`org_id`/actor and is within `staging_ttl_seconds`,
> then promotes only `approved_item_ids` to active ledger rows (and applies close-outs). IDs not
> present in the staged set → reject (an ID cannot be "approved" if it was never presented).

### Data Flow

```
START → initialize → pre_process → main(GraphNode) → {route} → post_process → finalize → END
                                        │                          ↓ (retry, max 3)
                                        │                       pre_process
              inner (mode-parameterized): extract → match/apply → brief
```

### State Definition (flat TypedDict extending `AgentState`)

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest`/`invoke`/`feedback` (adapter-validated; copied from `input_context`) | Yes |
| `user_input` | `str` | Framework field seeded by the adapter with the PII-masked transcript (scanned by default S-2; transient; never written to ledger) | ingest/invoke |
| `input_context` | `dict` | Framework field carrying request metadata (`meeting_meta`,`tenant_id`,`org_id`,`as_of_utc`,`brief_id`,`approved_item_ids`) | Yes |
| `normalized_transcript` | `str` | Post-quality-gate text used by extraction, derived from `user_input` (transient) | internal |
| `meeting_meta` | `dict` | `{meeting_id, title, date, participants}` (defaults applied at adapter) | ingest/invoke |
| `tenant_id` | `str` | Ledger scope key — fail-closed | Yes |
| `org_id` | `str` | Ledger scope key — fail-closed | Yes |
| `as_of_utc` | `str` | RFC3339 UTC, fixed once at adapter (deadline/overdue basis) | Yes |
| `brief_id` | `str` | invoke: generated staging key (returned); feedback: input key | invoke/feedback |
| `approved_item_ids` | `list[str]` | feedback only: IDs to promote (must exist in the staged set) | feedback |
| `extracted_items` | `list[dict]` | Structured decisions/actions with `params` + verbatim `evidence_span` | internal |
| `match_result` | `dict` | `{new, resurfaced, contradictions, completions}` | internal |
| `commitment_brief` | `dict` | Fixed-schema brief; each item carries **category-discriminated** `evidence_ids` — `new` items: `{meeting_id}` only; `resurfaced`/`contradiction`/`completion` items: `{meeting_id, ledger_entry_id}` (see S-3 provenance) | internal |
| `write_result` | `dict` | feedback only: `{applied: list[str], rejected: list[{item_id, reason}]}`. Written by the inner `apply` node, mapped into parent State by `main.merge_output`, and surfaced in `formatted_output` (S-3 allowlist). Empty-approval → `{applied: [], rejected: []}` | feedback |
| `minutes_draft` | `str` | DocGen minutes draft (Markdown) | internal |
| `formatted_output` | `dict` | Allowlisted output envelope (declared keys only — see S-3) | Yes |
| `degradation_reason` | `str \| None` | Set (with `status="success"`) when ledger/KB empty or a seam is unwired | internal |
| `status` | `str` | `AgentStatus` value (lower-case string) | Yes |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- **No raw transcript / full minutes written to the ledger** — the ledger stores short
  summaries + evidence references only. The transcript in `validated_input`/`normalized_transcript`
  is transient and, because mode separation uses **no checkpointer**, is never checkpointed.
- InvocationContext via `config["configurable"]` / `InvocationContext.from_state(state)` only
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

## Framework Utilization

### Shared Components Used
- InvocationContext (`from_state`; `ctx.secrets.require()` for any DB/KB/LLM endpoint creds — never `os.environ`)
- `shared.services.llm.BaseLLM` (injected via `config["llm"]` seam — no model id / concrete client hard-coded)
- `shared.services.database.BaseDatabase` (`fetch_one`/`fetch_many`/`execute`) — ledger of record. **INTERFACE-ONLY today** (concrete impls STUB in the wheel); injected seam
- `shared.services.vector_store.BaseVectorStore` (`search`/`upsert`/`delete`) — semantic recall index. **INTERFACE-ONLY today**; injected seam
- `shared.security.pii_detector.detect_pii` (shared detector; reused, not re-implemented)
- S-2: `pre_process._extra_security_gate_input()` — input quality gate (below)
- S-3: provenance verified in inner `brief.execute(state)` (needs state); inner `brief._extra_security_gate_output()` residual credential/PII scan (result-only) + outer `post_process._extra_security_gate_output()` output-envelope allowlist (result-only)
- S-4: `emit_trace_event()` — ≥1 domain event in every `execute()`, incl. reject / short-circuit / degradation paths

### S-2 (input security gate)

**PII masking is done at the adapter, and the masked text rides in `user_input`.** The framework
default S-2 in `FunctionNode` scans `user_input` / `validated_input` / `llm_response` (verified in
wheel `function_node.py`), and `BaseGraph.invoke()` seeds `user_input` (verified in wheel
`base_graph.py`). So the **API adapter runs `detect_pii` on the raw transcript at ingress** and
passes the masked text as `user_input`; the framework default S-2 then re-scans it at each node's
`__call__` (belt-and-suspenders). No self-built Japanese-name supplement is added (the shared
`detect_pii` has a known JP-name gap; residual mitigation is at the persistence layer —
summary-only + role-substitution, see A-5). Raw personal names never enter the ledger.

`pre_process._extra_security_gate_input` quarantines the input regardless of channel (paste or
upload) — thresholds are in `config/config.yaml`. `transcript` arrives as a **JSON string field**
(schema type `str`), so the adapter first obtains its **UTF-8 bytes**; the checks run in a **fixed
order**, first failure rejects:
1. **Format allowlist (on bytes, before decode):** sniff the **leading bytes** for `%PDF`,
   `PK\x03\x04` (ZIP/OOXML), OLE `\xD0\xCF\x11\xE0` (legacy Excel). Any match → reject (a
   text/CSV export never starts with these). **No parser is shipped** — rejected formats have no
   attack surface.
2. **Strict decode:** decode the bytes as **strict UTF-8**; a decode error → reject (so mojibake
   cannot smuggle bytes past the ratio checks). The `detect_pii` masking then runs on the decoded
   text (adapter, see above); ratios below are computed on the **masked decoded string**.
3. **Text-quality ratios** (denominator = total characters of the decoded string; `0`-length is
   caught by the length rule): reject on U+FFFD ratio > `input_max_replacement_char_ratio`,
   **control-char** ratio > `input_max_control_char_ratio` (control = Unicode C0/C1 **excluding**
   TAB/LF/CR), or printable ratio < `input_min_printable_ratio`.
4. **Length:** reject if character length is outside `[input_min_chars, input_max_chars]`.
5. **Mode / scope validation:** reject unknown `mode`; reject missing/empty `tenant_id`/`org_id`.

### S-3 (output security gate) — provenance in `execute`, allowlist in the hook

> **SDK reality:** `FunctionNode._extra_security_gate_output(self, result)` receives **only the
> `result` dict, not `state`** (verified in wheel `function_node.py`). Provenance verification
> needs the transcript and the ledger `fetch` results, which live in `state` — so it CANNOT run
> in the S-3 hook. It runs inside `execute()`, where `state` is available. The hooks (which see
> only `result`) do the checks that need only the output.

1. **Inner `brief.execute(state)` — provenance (three-layer + bidirectional), before returning:**
   Every failure below sets `status="error"` + `error_log`, emits `provenance_rejected`, and stops
   the item set from being surfaced (fail-closed — no partial brief).
   - **(a) Evidence-ID existence (typed).** `evidence_ids` is a **discriminated schema by item
     category**, not a free `{meeting_id, ledger_entry_id}` pair:
     - `new` (registration candidate) → `{meeting_id}` only; **no `ledger_entry_id`** (nothing to
       reference yet). A `new` item carrying a `ledger_entry_id` → reject.
     - `resurfaced` / `contradiction` / `completion` → `{meeting_id, ledger_entry_id}`; the
       `ledger_entry_id` **must be present in this request's ledger `fetch` result** (the scoped
       rows actually read), and `meeting_id` must equal the request `meeting_meta.meeting_id`.
       A `ledger_entry_id` that is well-formed but was not in the fetch set → reject (the
       "real-but-unrelated ID" attack; mirrors the required PB case).
   - **(b) Verbatim span match.** each item's `evidence_span` must be a substring of the
     `normalized_transcript` with **minimum length 8 characters** (empty/1-char spans make
     `span in source` vacuously true). Below-min or not-found → reject.
   - **(c) Claim↔source faithfulness (the layer a substring check does NOT give you).** A matched
     span only proves *a quote exists*; it does **not** prove the item's `params` are faithful. So
     every **rendered** field is bound to a checkable source, not to free-form LLM prose:
     - `owner`, `due`, `status`, and any numeric `params` for `resurfaced`/`contradiction`/
       `completion` items **render from the cited ledger row** (`fetch` result keyed by
       `ledger_entry_id`), and each such `params` value must **equal** the corresponding field of
       that row. A mismatch (LLM rewrote a due date / owner / amount) → reject. This closes the
       "keep a valid ID + an unrelated 8-char span, but change the numbers in `params`" hole.
     - for `new` items, `owner`/`due` must appear **verbatim within the cited `evidence_span`**
       after NFKC normalization (so `params.due` is a literal token of the quoted sentence, not a
       number the LLM invented in prose). `due` is normalized to ISO-8601; overdue is computed
       against `as_of_utc` deterministically, never parsed from prose.
   - **(d) Bidirectional coverage.** (i) **claim→source:** every brief item must carry at least one
     valid `evidence_ids` entry and a valid `evidence_span`; an item with none → reject (no
     unreferenced claims). (ii) **source→claim:** every ledger row returned in `match_result`
     (`resurfaced`/`contradictions`/`completions`) must be represented by exactly one brief item;
     a returned row silently dropped from the brief → reject (no silent omission of a surfaced
     obligation). This is the completeness half that a claim→source-only check misses.
   - The inner `_extra_security_gate_output(result)` hook then does a residual credential/PII
     scan on the produced item strings (result-only; no state needed).
2. **Outer `post_process._extra_security_gate_output(result)` — output-envelope allowlist:** the
   `formatted_output` envelope is built in `post_process.execute()`, so the allowlist is enforced
   in this hook (it operates on `result`, which is exactly what it needs). Declared keys (below)
   only; unknown/nested keys dropped. Recursive PII/credential residue scan on string values (the
   framework default S-3 only credential-scans top-level strings, so the recursive check is added
   explicitly).

**`formatted_output` declared keys (allowlist) — selected by a self-declared discriminator
(`status=="error"` → error keys, else `mode`); fail-closed, no state dependency:**
- **invoke:** `mode`, `status`, `minutes_draft` (str), `commitment_brief` (obj: `new[]`,`resurfaced[]`,`contradictions[]`,`completions[]`, each item = `{item_id, summary, params, evidence_ids, evidence_span}`), `brief_id`, `degradation_reason` (nullable)
- **ingest:** `mode`, `status`, `ingest_summary` (`{written, extracted}`), `degradation_reason`
- **feedback:** `mode`, `status`, `write_result` (`{applied[], rejected[]}`), `degradation_reason`
- **error (any mode):** `mode`, `status`, `error` (`{code, message}`, fixed reason enum + PII-free message — see Error routing above)

> **`item_id`:** each presented brief item carries a deterministic `item_id`
> (`identifiers.item_id(meeting_id, span, owner, due)`), identical to the staged
> `candidate_set` id, so the human approves by an id they can actually see in the brief and
> `feedback.approved_item_ids` references it. Undeclared top-level keys **raise** (fail-closed);
> a brief item carrying an undeclared key is **dropped** (never silently emitted).

> S-3 is a product-safety mitigation, not legal-compliance certification. No hand-written
> assertive-tone denylist (allowlist is the main axis).

### S-4 (audit)

Domain events on **all** paths, not only success: `transcript_normalized`, `quality_gate_rejected`
(magic-byte / ratio / length), `mode_rejected`, `scope_rejected` (missing/forged `tenant_id`/`org_id`),
`items_extracted`, `ledger_matched`, `brief_built`, `provenance_rejected`, `main_short_circuited`
(upstream error), `staged_candidates_written`, `ledger_write_applied`, `feedback_id_rejected`,
`degraded` (empty ledger / unwired seam). Each reject event carries a `reason` code and
`correlation_id` and **no transcript content / no PII** in the payload.
`node_start`/`node_complete`/`node_error` are emitted by `__call__()` — not duplicated here.

> **Adapter-level rejects are audited too.** Schema/type reject, unknown-key reject, mode-not-
> permitted (capability) reject, and scope claim-mismatch reject happen **before** any node runs,
> so `emit_trace_event` inside `execute()` cannot see them. The adapter emits the equivalent
> audit event (`adapter_request_rejected` with `reason` + `correlation_id`, PII-free) at ingress
> so the reject is not an audit blind spot.

### Composition Pattern

- **Pattern**: GraphNode (in-process subgraph) — `main` composes `LedgerWorkflowGraph`
- **Composition target**: inner `LedgerWorkflowGraph` (`src/graph/domain_workflow_graph.py`)
- **Error propagation strategy**: **`handle`** (declared explicitly) — an inner exception
  (`SubgraphError`) is caught by `main.on_subgraph_error(state, err)`, which returns the allowlisted
  error envelope `{mode, status:"error", error:{code:"subgraph_error", message}}` (no re-raise, no
  uncaught exception reaching the app boundary). A **provenance reject inside the inner `brief`**
  sets `status="error"` and **returns normally** (the inner graph routes to its own finalize); that
  is a returned status, not an exception, so `main.merge_output(state, sub_result)` detects
  `sub_result["status"]=="error"` and, instead of mapping the normal keys, writes `status="error"`
  + the same allowlisted error envelope (`code:"provenance_failed"`). Both inner-error paths thus
  converge on `status="error"` + a shaped `formatted_output`; outer `route()` then sends to
  `finalize`, which surfaces it (see Error routing above).
- **propagate_hitl**: `False` (mode-separation; no in-graph HITL to propagate — see A-3)
- **`main.execute()` override**: short-circuit on upstream error before delegating (above); the
  inner-error → envelope conversion is done by `on_subgraph_error` (exception) and `merge_output`
  (returned `status="error"`), so an inner failure never leaves `formatted_output` unset.

### Ledger scoping — fail-closed, through the real SDK seams

The scope keys `tenant_id` + `org_id` are **claim-bound at the adapter** (see Authorization model):
body values, if present, must equal the caller's claims; otherwise they are injected from the
claims. After that binding, both must be non-empty — a missing/empty **claim** is a hard reject,
never a broad query, and the nodes never default or widen them. Because the SDK seams differ,
scoping is enforced per seam:
- **DB (`BaseDatabase`)** — every `fetch_many`/`fetch_one`/`execute` includes
  `WHERE tenant_id = :tenant_id AND org_id = :org_id` with both in `params`. `params` is
  supported by the seam (`fetch_many(query, params)`).
- **KB (`BaseVectorStore`)** — `search(query, top_k)` has **no scope argument**, so scoping is
  bound at construction: the injected store is a **per-`(tenant_id, org_id)` namespace/collection**
  produced by a scoped factory. An unbound/global store is rejected. A cross-scope document is
  therefore never reachable by `search`. (This is a design constraint the injection wiring
  must honour; the interface alone cannot enforce it.)

### Staging record contract (`brief_id`) — replay-safe promotion

Reading `owner`/`freshness` alone cannot stop replay; the staging record needs an explicit state
model and a consume-once promotion. The `invoke` staging row (one per `brief_id`) has the schema:

| Field | Meaning |
|-------|---------|
| `brief_id` | primary key; generated by `invoke` (`correlation_id`-seeded, stable per request) |
| `tenant_id`, `org_id`, `created_by` (`ctx.caller_id`) | owner/scope, all fail-closed on `feedback` |
| `candidate_set` | the exact item ids + summaries presented by that `invoke` |
| `issued_at`, `expires_at` (`issued_at + staging_ttl_seconds`) | freshness window (from `as_of_utc`) |
| `status` (`pending` / `consumed`) + `consumed_at` | consume-once marker |

`feedback` promotion is a **single-transaction compare-and-set**: promote only if the row is
`tenant_id`/`org_id`/`created_by`-owned, `status == pending`, and `as_of_utc <= expires_at`; the
same transaction flips `status → consumed` and writes the active rows for `approved_item_ids ∩
candidate_set`. Any of {absent, wrong owner, cross-scope, expired, **already `consumed`**} → reject
(`feedback_id_rejected`). This makes **replay of a consumed `brief_id` reject deterministically**,
and defines the **concurrent-feedback winner** (the first CAS consumes; the loser sees `consumed`
and rejects). Active-row writes are **idempotent on `(brief_id, item_id)`** so a retried winner
does not double-apply. `approved_item_ids` not in `candidate_set` → reject (cannot approve an item
that was never presented); an **empty** `approved_item_ids` consumes the row and writes zero active
rows, returning `write_result = {applied: [], rejected: []}`.

## Executable-Contract Completeness

| Claim | Default value & allowlist | Unknown/invalid handling | Determinism | Write authorization | Test |
|-------|---------------------------|--------------------------|-------------|---------------------|-----------|
| `mode` | adapter injects `invoke`; allowlist `{ingest,invoke,feedback}` | unknown → reject at adapter | from request, not env | — | mode-dispatch: invoke writes no active row, feedback does |
| `meeting_meta.*` optional | title="",participants=[],date=`as_of_utc` date part; `meeting_id` required | missing meeting_id → reject | date from `as_of_utc` (UTC) | — | meta-default + missing-meeting_id reject |
| request schema | closed (`additionalProperties:false`) | unknown key / wrong type → reject at adapter | — | — | unknown-key + wrong-type reject |
| `ingest` writes active rows | bulk bootstrap load | scope keys required | as_of UTC once | S-1 `VERIFIED_EXTERNAL` + `ledger:bootstrap` capability at adapter + scope keys (`tenant_id`/`org_id`) | ingest-write + cross-scope reject |
| `invoke` writes staging only | staged candidates under generated `brief_id` (TTL `staging_ttl_seconds`) | scope keys required | as_of UTC once; `brief_id` from a stable per-request generator (`correlation_id`-seeded) | S-1 `VERIFIED_EXTERNAL` + `ledger:stage` capability at adapter + scope | invoke writes no active commitment; staging row scoped; no-capability `invoke` reject |
| scope keys (`tenant_id`/`org_id`) | bound to caller claims at adapter; non-empty, normalized | body value ≠ caller claim → reject; empty → reject; absent → inject from claim | from claim, not env | both required for any read/write; claim-binding makes cross-scope unreachable per seam (DB params, KB namespace) | request body forging another tenant/org → reject; both DB and KB cross-scope unreachable |
| `feedback` promotes approved | `approved_item_ids` must exist in `candidate_set`; consume-once CAS (see Staging record contract) | id not staged / stale / cross-scope / **already-consumed** `brief_id` → reject | approved set from request; consume-once is deterministic | S-1 `VERIFIED_EXTERNAL` + `ledger:apply` capability at adapter + `brief_id` ownership (`ctx.caller_id`+scope) + `status==pending` + TTL freshness | valid approval; forged/replayed(=consumed)/cross-scope/expired `brief_id` reject; **empty `approved_item_ids` → 0 active writes, row consumed, `write_result={applied:[],rejected:[]}`**; concurrent-feedback: first CAS wins, loser rejects |
| deadlines / overdue | `as_of_utc` fixed once at adapter via injectable server clock | — | UTC, host-TZ-independent; test clock seam makes it reproducible | — | fixed `as_of` (injected clock) → stable overdue set across host TZs |

> **Authorization model (SDK reality).** `VERIFIED_EXTERNAL` is the S-1 floor (framework-enforced
> before `execute()`). `InvocationContext` has **no permissions/capabilities field** — only
> identity/trust (`caller_id`, `caller_trust_level`) (verified in wheel `invocation_context.py`).
> So mode-specific *capability* authorization — **`ledger:bootstrap` for `ingest`, `ledger:stage`
> for `invoke`, `ledger:apply` for `feedback`** (all three write paths carry a capability; `invoke`
> is not exempt just because its write is a short-lived staging row) — is enforced by the **API
> adapter / auth middleware before invocation** (it maps the caller's token/claims to the allowed
> modes and rejects a disallowed mode at ingress).
>
> **Scope keys are bound to the authenticated caller, not trusted from the request body (H1).**
> `tenant_id` + `org_id` are **derived from / validated against the caller's authenticated claims
> at the adapter**: if the request body carries them, they must **exactly equal** the caller's
> token claims (mismatch → reject); if it does not, the adapter injects the claim values. Both are
> required, **non-empty, and normalized** (trimmed; a canonical case/again fail-closed on empty).
> This is the load-bearing point: DB `params` binding and KB scoped-namespace only isolate to *the
> scope the request names* — without claim-binding, a `VERIFIED_EXTERNAL` caller could read/write
> **another tenant's ledger by passing arbitrary scope values**. Claim-binding is what makes
> cross-tenant access unreachable, per seam. (`tenant`-only would still leak across orgs inside one
> company — hence both keys.)
>
> What the write nodes enforce *inside* the graph, using data they do have, is **ownership + scope
> + freshness**: `feedback` requires that the `brief_id` staging record was created by the same
> `tenant_id`/`org_id` and `ctx.caller_id`, and is unconsumed and within `staging_ttl_seconds` (see
> Staging record contract below); a mismatch, consumed, or stale/absent record → reject.

## EU AI Act Art.13 Design-Time Evidence

> The proposal declares **Not in scope** (CMN, internal meeting operations, human-gated
> active writes). Recorded for traceability; advisory until 2026-09-01.

| Evidence item | Design reference / description |
|---------------|--------------------------------|
| Intended purpose and operating context | Internal meeting commitment tracking; human operators (managers/PMO) review all output |
| System capabilities and limitations | Presents candidates with evidence IDs; does not auto-decide; degrades to `success`+`degradation_reason` when ledger/KB empty |
| User-facing transparency information | Every brief item carries evidence IDs + verbatim span; minutes are a draft for human edit |
| Human oversight mechanism | Mode separation — active commitments created only via `feedback` after human approval |

## Import Isolation Confirmation
- Template does not import agenticstar-platform SDK (Level 0)
- Import targets: `framework/` and `shared/` only (no `agents/base/` required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed pipeline (extract→match→brief), not a self-directed think→act loop |
| Composition pattern | GraphNode (subgraph) | Single FunctionNode main | **GraphNode** | Multi-stage inner workflow; keeps stage-level S-2/S-3 boundaries |
| HITL mechanism (A-3) | Mode separation (no D6) | In-graph D6 `interrupt()` | **Mode separation** | In-graph-suspend-necessity test = all No (below) |
| feedback linkage (A-4) | invoke writes scoped staging record + `brief_id`; feedback promotes | invoke read-only; feedback re-submits full items + re-verifies | **Staging + `brief_id`** | Stateless feedback can prove "was presented" without a checkpointer; keeps active-ledger writes human-gated |
| Sensitive-speech handling (A-5) | detect_pii (adapter) + summary-only + role-substitution | + self-built JP-name dictionary | **detect_pii + summary-only + role-substitution** | Decision: no self-built supplement; don't chase 100% machine guarantee |
| Runtime config home | `agent.yaml` `config:` | `config/config.yaml` | **`config/config.yaml`** | Keeps runtime parameters separate from the static registry manifest. |
| Input format | Text/CSV only | + PDF/Excel parsers | **Text/CSV only** | Magic-byte reject; avoids parser/OCR sprawl |
| Cost budget | Framework-enforced ceiling | Advisory param | **Advisory (`budget_usd_advisory`)** | AgentBaseGraph does not enforce `budget_usd` (only AutonomousBaseGraph does) |
| Scope binding (H1) | Trust `tenant_id`/`org_id` from request body | Bind/validate against caller's authenticated claims | **Claim-bound** | Body-only scope lets a `VERIFIED_EXTERNAL` caller read/write another tenant's ledger; DB params + KB namespace only isolate to the *named* scope, so claim-binding is what makes cross-scope unreachable |
| invoke authorization (H2) | `VERIFIED_EXTERNAL` + scope only | + `ledger:stage` capability | **+ `ledger:stage`** | `invoke` performs a staging write; all three write paths should carry a capability (symmetry with `ingest`/`feedback`) |
| Replay safety (H3) | Read owner + freshness only | Staging record `status` + consume-once CAS | **Consume-once CAS** | Reading freshness cannot stop replay of a consumed `brief_id`; a single-transaction compare-and-set also defines the concurrent-feedback winner + idempotency |
| Provenance depth (H6) | ID + verbatim span (two-layer) | + claim↔source faithfulness + bidirectional coverage | **Three-layer + bidirectional** | A substring match proves a quote exists, not that `params` numbers/owner/due are faithful; binding rendered fields to the cited ledger row / span closes the number-rewrite hole |
| Error output (H8) | Leave `formatted_output` unset on error | Minimal allowlisted `error` envelope written by the erroring node | **Minimal `error` envelope** | `route()` sends error→`finalize`, skipping `post_process` (the envelope builder); the erroring node writes `{mode,status,error:{code,message}}` so the shape stays deterministic |

### A-3 — HITL in-graph-suspend-necessity test (four-question test)

| Q | Answer |
|---|--------|
| ① Does the AG itself make an approve/deny judgment? | No — the human approves; the AG only presents |
| ② Can validation-failure terminate fail-closed (normal end)? | Yes — `invoke` ends `success` after presenting; no session held |
| ③ Does the human reply within minutes? | No — post-meeting approval is hours/days (async) |
| ④ Can an async path (`feedback` mode) carry the human involvement? | Yes — `feedback` promotes approved items |

All four → **HITL non-adoption (mode separation)**. `hitl.enabled` stays false; `memory_enabled`
false; **no checkpointer**, no `/resume` path, **PB-7 auto-waived**. This preserves the
three-mode design ("only human-approved items are written to the ledger via feedback"). *Presented to
the user as decision A-3 (recommendation: mode separation).*

### A-4 — feedback→ledger linkage (staging vs stateless re-submit)

Because there is no checkpointer (A-3), a stateless `feedback` call needs a way to prove that
`approved_item_ids` were actually presented by a prior `invoke`. Chosen: `invoke` persists the
presented candidate set as **scoped, expiring staging rows** keyed by a generated `brief_id` and
returns it; `feedback` promotes only staged IDs after verifying `brief_id` ownership
(`tenant_id`/`org_id`/actor) and freshness (`staging_ttl_seconds`). This keeps active-ledger
writes human-gated while making the "was presented" check enforceable. *Presented to the user as
decision A-4 (recommendation: staging + `brief_id`).*

### A-5 — Sensitive-speech (機微発言) granularity

The ledger is accumulation-type (permanent), so speech sensitivity is a key quality concern.
Layered mitigation:
- **Adapter PII masking:** `detect_pii` runs at ingress on the raw transcript (shared detector, reused).
- **Persistence minimization:** the ledger stores short summaries + evidence references only;
  raw utterances / full minutes are never written.
- **KB two-layer split:** KB stores decision/action short summaries only; the retrieval text
  (classification key + summary + concept code) is separate from conclusion/reason.
- **Role-substitution:** the summary-generation prompt refers to people by role (担当A / 承認者)
  rather than personal name in the stored summary — a cheap residual guard for the `detect_pii`
  JP-name gap, without a self-built dictionary.
- **Honest residual:** this does not guarantee zero personal-name leakage into summaries; the
  shared `detect_pii` JP-name gap is a framework matter, not patched here. *Presented to the user
  as decision A-5 (recommendation: keep this default layering).*

### Pre-empt-set check (recurring design-review findings + defaults)

| Item | Applies? | How addressed |
|------|----------|---------------|
| provenance faithfulness (not ID-only) | Yes | S-3 inner `brief` **three-layer + bidirectional**: (a) typed evidence-ID existence incl. real-but-unrelated-ID reject; (b) verbatim span (min 8); (c) **claim↔source faithfulness** — `params` values must equal the cited ledger row / appear verbatim in the cited span (closes "valid ID + unrelated span + rewritten numbers"); (d) bidirectional coverage (unreferenced claim reject + surfaced-row dropped reject) |
| verbatim span min length | Yes | span min length 8; below-min → reject |
| inheritance ≠ composition | Yes | class inherits AgentBaseGraph only; GraphNode/inner graph documented as composition |
| input quality gate | Yes | ordered: magic-byte (bytes) → strict-UTF-8 decode → U+FFFD/control/printable ratio → length; thresholds in `config/config.yaml` |
| S-4 on non-success paths | Yes | domain events on quality-gate/mode/scope reject, short-circuit, degradation; **adapter-level rejects audited via `adapter_request_rejected`** |
| determinism / TZ | Yes | `as_of_utc` fixed once at adapter via **injectable server clock** (test clock seam), UTC; overdue basis stable across hosts |
| authz / scope binding | Yes | scope keys **bound to caller claims** (body-forged tenant/org → reject); all 3 write paths carry a capability (`ledger:bootstrap`/`stage`/`apply`); staging **consume-once CAS** for replay-safety |
| input format default | Yes | text/CSV only; PDF/Excel/ZIP/OLE magic-byte rejected |
| PII default | Yes | shared `detect_pii` (adapter) only; no self-built JP supplement |
| data governance | Yes | DB=source-of-truth + KB=summary; raw text never written to ledger; tenant_id+org_id fail-closed (DB via params, KB via scoped namespace, both claim-bound) |
| HITL default | Yes | mode separation (A-3) — no in-graph interrupt; PB-7 auto-waived |
| customer-data premise | N/A | ledger is AG-owned; ingest bootstraps from the customer's past minutes (premise, not a concern) |
| unwired seams | Yes | DB/KB/LLM are injected seams; concrete impls are STUB in the wheel — to be recorded in the operation guide as unwired-until-real-deploy |

> **Adversarial review, round 1 (design stage, pre-push).** 8 High / 3 Medium / 1 Low were raised and
> all addressed in this revision (none rejected as phantom): scope claim-binding (H1) + per-mode
> capability incl. `invoke:ledger:stage` (H2); staging record contract + consume-once CAS for
> replay/concurrency (H3); empty-approval contract & test (H4); injectable clock seam (H5);
> provenance three-layer + bidirectional faithfulness (H6); `write_result` State field + merge_output
> + allowlist wiring (H7); explicit error routing + `error` envelope (H8); input-gate order/units
> (M1); adapter-level + quarantine/short-circuit audit events (M2); README per-decision qualifier
> (M3); `meeting_id` required-non-empty (L1). Re-reviewed after the revision (section adds, not
> renames — references re-checked).

## Implementation notes (non-normative, for the implementer)

- `main` GraphNode: implement `execute()` (upstream-error short-circuit → `return {}`, else
  `super().execute(state)`), `get_subgraph()` (compile `LedgerWorkflowGraph` with the injected
  `llm`/`db`/`kb` config), `extract_input()` (serialize `{mode, normalized_transcript, meeting_meta,
  tenant_id, org_id, as_of_utc, brief_id, approved_item_ids}` with `json.dumps(..., default=str)`;
  the inner graph receives a string + ctx only), `on_subgraph_error(state, err)` (`error_strategy=
  "handle"`; return `{status:"error", formatted_output:{mode, status:"error",
  error:{code:"subgraph_error", message}}}`), and `merge_output(state, sub_result)`:
  - **success path** — map **only these declared inner keys** into parent state (no raw
    pass-through): `extracted_items`, `match_result`, `commitment_brief`, `brief_id`,
    `write_result` (feedback), `degradation_reason`, `status`; any other inner key is dropped.
  - **inner-error path** — if `sub_result["status"]=="error"`, do **not** map the normal keys;
    write `status="error"` + `formatted_output={mode, status:"error", error:{code, message}}`
    (the allowlisted envelope). `error` is not a top-level State key — it lives inside
    `formatted_output`; `error_log` is the framework `AgentState` reducer field.
- Tests to author (see docs/03_test_spec.md): **adapter valid-request success +
  default-mode injection** (absent `mode` → `invoke`); adapter schema reject cases (unknown key,
  wrong type, missing required, unknown-mode, transcript-present-in-feedback); **capability reject
  per mode** (no `ledger:bootstrap`/`ledger:stage`/`ledger:apply` → reject); **scope claim-binding**
  (request body forging a different `tenant_id`/`org_id` than the caller claim → reject; empty →
  reject; both DB and KB cross-scope unreachable); mode dispatch (write vs no-write per mode);
  **error routing** (quality-gate/mode/scope reject and upstream short-circuit → `status=error`,
  `post_process` skipped, minimal allowlisted `formatted_output.error` envelope surfaced by
  `finalize`); determinism via **injected fixed clock** (same `as_of` → same overdue set across host
  TZs); **S-3 provenance** — ID-absent, span-too-short, span-forged, **real-but-unrelated
  `ledger_entry_id`**, **params-value ≠ cited ledger row (LLM rewrote due/owner/amount)**,
  **unreferenced claim (no evidence)**, **surfaced ledger row dropped from brief (source→claim
  completeness)**; input quality gate order (magic-byte PDF/ZIP/OLE reject, invalid-UTF-8 reject,
  each ratio, length bounds — one accept / one reject each); **feedback authorization + replay**
  (valid; forged / **already-consumed(replay)** / cross-scope / expired `brief_id` reject; **empty
  `approved_item_ids` → 0 active writes, row consumed, `write_result={applied:[],rejected:[]}`**;
  concurrent-feedback first-CAS-wins); PB-3 real-DB ledger match (incl. cross-tenant invisibility);
  PB-1/2/4/5/6 as scaffold. PB-7 auto-waived (non-HITL).

## Implementation Reconciliation (DDR — implementation-confirmed)

> The implementation was matched against the SDK wheel (`agenticstar-agentcore==1.0.0`,
> read from source). The following are the **implementation-confirmed** points where the
> implementation made a concrete decision or corrected a design assumption; all are realized here.

| # | Point | Design text | Implementation (confirmed) | Rationale |
|---|-------|-----------|---------------------------------|-----------|
| R-1 | Inner-error routing | "`merge_output` detects `sub_result['status']=='error'`" | The wheel's `GraphNode.execute()` routes an inner returned `status=="error"` to **`on_subgraph_error`**, NOT `merge_output`. `main.execute()` is therefore **fully overridden** (not `super().execute()`) so it can give the inner reject a distinct code (`provenance_failed` vs `brief_id_invalid` vs `subgraph_error`). Both inner-error paths (raised exception, returned error) converge on the allowlisted `error` envelope; `merge_output` runs on success only. | Verified against `framework/nodes/graph_node.py` (lines 150–158). Keeps `formatted_output` always set. |
| R-2 | Where staging is written | "match writes staged candidates" | The **`brief`** node writes the staging record, **after provenance passes**. | A provenance-rejected brief must never leave a staging record for a brief that was never actually presented. Keeps `candidate_set` == what was presented. |
| R-3 | Presented item id | item = `{summary, params, evidence_ids, evidence_span}` | Added **`item_id`** to each presented item (and to the S-3 item allowlist); identical to the staged candidate id. | `feedback.approved_item_ids` needs ids the human can see in the brief. |
| R-4 | S-3 undeclared key | "unknown/nested keys dropped" | Undeclared **top-level** key → **raise** (fail-closed); undeclared **brief-item** key → drop. | Raising catches a post_process bug/smuggle; dropping keeps per-item robustness. Matches the proven S-3 pattern used elsewhere (kind/mode-declared, no state dependency). |
| R-5 | LLM/DB/KB unwired | injected seams | Un-injected `llm`/`ledger` → **deterministic degradation** (`status=success` + `degradation_reason`), never a crash; concrete `BaseDatabase`/`BaseVectorStore` are STUB in the wheel, so dev/CI use in-memory scoped seams (`src/services/stores.py`) with the same method contract; a real backend is injected at deploy. | Runnable + testable now; production wires real seams. |
| R-6 | Runtime config home | `agent.yaml` `config:` | Runtime parameters live in `config/config.yaml`; node defaults remain defensive, and `server.py` passes an explicit standalone config. | Keeps registry identity separate from operator-tunable runtime settings. |
| R-7 | Provenance fail-closed granularity | "fail-closed — no partial brief" | Confirmed: **any** provenance failure withholds the **whole** brief item set (`status=error`), not a per-item drop. The minutes draft is built in `post_process`, which `route()` skips on error, so a provenance reject also withholds the minutes. | Honors decision H6; the strictness trade-off is surfaced to the reviewer (see review guide). |

### Pre-empt-set check (recurring implementation-review findings)

| Pattern | Applies? | How addressed |
|---------|----------|---------------|
| S-3 back-door (allowlist `.*`, trusted client `payload_ref`, `_extra_security_gate_output` has no `state`, side free-text unmasked) | Yes | Envelope allowlist is discriminator-selected (no `.*`); `_extra_security_gate_output(result)` reads **only** `result` and declares shape via `mode`/`status`; **recursive** residue mask over all nested strings (`test_services_envelope`). |
| checkpoint scrubber / PB vacuous | N/A | No checkpointer (mode separation); PB-5 is the state-safety AST scan + the "transcript never checkpointed" invariant. |
| S-4 PB-1 watches only framework events | Yes | `test_pb1_s4_audit.py` patches each node module's `emit_trace_event` and asserts a **domain** event (mutation-checked). |
| startup never exercised by CI | Yes | `test_startup_and_flow.py` imports `src.api.server` and runs `TestClient(app)` (startup + first invoke). |
| fail-closed test is a bare `raises` | Yes | Scope/capability/replay rejects are asserted with **provisioned** valid controls alongside (`test_services_stores`, `test_services_request_contract`, e2e). |
| numeric guard defeated by JP unit adjacency | N/A | No numeric threshold guard on free text; numbers are bound to the cited ledger row / verbatim span (provenance), NFKC-folded. |
| test-substitution weakens PB | Yes | PB-6/PB-1 use dynamic `pkgutil` scans with a `>= 5` real-node-count assertion. |
| vendored-part import identity | N/A | No vendored `parts/` in this template. |

### Adversarial review, round 2 (post-round-1 hardening) — reconciliation

A second adversarial review was run on the R1-hardened code (round 1 fixed 8 High/3 Med; this
round re-scrutinized the result). Verdicts below are recorded against the actual code.

**Accepted and fixed in this MR:**

| # | Finding | Fix (file) | Test |
|---|---------|-----------|------|
| R2-1 | An all-whitespace 8-char `evidence_span` passed the min-length check (vacuous substring). | `provenance.py` — min length measured on `span.strip()`. | `test_whitespace_only_span_rejected` |
| R2-2 | A ledger-cited brief item's `summary` was LLM-controlled (params were row-faithful, summary was not) → a re-phrased/misleading summary could be presented. | `brief.py` `_brief_item_ledger` — `summary` rendered **from the cited row** (faithful by construction; current-meeting evidence still shown via `evidence_span`). | `test_ledger_item_summary_from_row_not_llm` |
| R2-3 | `minutes_draft` was duplicated as a **top-level** result string, so a credential in it would trip the framework `@final` S-3 top-level scan and RAISE an *unshaped* error, whereas the same string inside `formatted_output` is masked → inconsistent fail path. | `post_process_node.py` — `minutes_draft` has a **single home** inside `formatted_output` (masked by the recursive S-3 residue pass). | existing `test_invoke_envelope_has_minutes_and_brief` |
| R2-4 | Adapter reject bodies reflected the raw request `mode` and `Rejection.message` (which can echo an attacker-chosen key name / PII) unmasked to the client. | `server.py` — client message is a **FIXED per-reason string** (`_ADAPTER_MESSAGE`); `mode` reflected only if a valid enum value (`_safe_mode`); the untrusted internal message stays in the audit event only. | `test_startup_and_flow` adapter cases |
| R2-5 | Invalid-JSON / non-object rejects returned before `correlation_id` + `adapter_request_rejected` were emitted (audit blind spot). | `server.py` — `correlation_id` allocated first; both early-reject paths audited. | — (integration) |
| R2-6 | S-3 error envelope accepted an `error` object missing `code`/`message` (fail-open). | `envelope.py` — require `code` **and** `message` as strings. | `test_error_object_empty_raises`, `test_error_object_missing_message_raises` |
| R2-7 | `brief_id` / `approved_item_ids` present in an `ingest`/`invoke` body were silently ignored (fail-open acceptance of an ill-formed request). | `request_contract.py` — reject feedback-only fields for ingest/invoke (`R_BRIEF_ID_NOT_ALLOWED`). | `test_invoke_with_feedback_only_fields_rejected` |
| R2-8 | The inner `brief` short-circuit path emitted no domain audit event. | `brief_node.py` — emit `brief_short_circuited`. | — |

**Rejected (phantom / conflicts with the tested design):**

| # | Finding | Why rejected |
|---|---------|-------------|
| R2-A | "Reject an ANONYMOUS caller with a shaped error at the adapter before `invoke`." | Conflicts with the **deliberately-tested** design: `test_anonymous_caller_denied_by_trust_gate` asserts an unauthenticated caller reaches the graph and the framework **S-1 trust gate** denies it (HTTP 200, `status="error"`). Pre-rejecting at the adapter would bypass the framework S-1 boundary that PB-6/TC-08 exercise. The S-1 denial is a first-class, audited framework path here — not an adapter concern. |

**Scope decisions (surfaced, not silently changed):**

| # | Point | Decision |
|---|-------|----------|
| R2-D1 | `ingest` writes LLM-extracted rows to the active ledger **without** brief-provenance. | **By design.** `ingest` is a privileged, capability-gated (`ledger:bootstrap`) **bulk bootstrap of the customer's OWN past minutes** (DDR "customer-data premise: N/A"). Brief-provenance guards the *presented* brief a human approves; the ingest corpus is the trusted source being loaded, not a claim about an existing row. Speech-sensitivity is mitigated by adapter PII masking + summary-only persistence + role-substitution (A-5), not by ingest provenance. |
| R2-D2 | `feedback` promotes **every** approved candidate as a new active row; `completion`/`contradiction` candidates do not yet **close/supersede** the cited row (the design's "(and applies close-outs)"). | **Deferred — surfaced as a reviewer DECISION.** The implementation covers new-commitment promotion; per-category close-out semantics (carry the cited `ledger_entry_id` into the candidate + an atomic close/supersede in the CAS) are a scoped follow-up. Current behaviour is safe (no data loss) but a completion approval leaves the cited row active. Surfaced to the reviewer as a decision. |
| R2-D3 | KB (`BaseVectorStore`) recall is fetched (scope-checked + audited) but **advisory-only** — it is not yet fed into classification (the ledger is authoritative), and the dev in-memory seam takes an explicit scope arg while the design's production adapter binds scope at construction (`search(query, top_k)`). | **Documented reconciliation.** Matching is ledger-authoritative; recall widens the audit signal only. Real-deploy wiring injects a per-`(tenant,org)`-namespace `BaseVectorStore` adapter (design §KB) and, if KB-weighted matching is desired, feeds recall into `classify`. (`BaseVectorStore` concrete impls are STUB in the wheel today, so the reuse rule does not apply.) |
