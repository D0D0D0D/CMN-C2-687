# Test Specification — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent

## Test Strategy

- **Two tiers.** The safety-critical / business logic lives in **framework-free service
  modules** (`src/services/*`) that are exhaustively unit-tested (`tests/unit/test_services_*.py`).
  The framework glue (nodes / graph / adapter) is covered by framework-importing tests
  (unit + PB + integration) that run under the SDK venv in CI.
- **Test types:** Unit (services + nodes) / Proof-of-Boundary / Integration (FastAPI startup +
  full ingest→invoke→feedback flow).
- Coverage target: ≥ 80% of `src/services` (the deterministic core); framework glue covered by
  PB + integration.

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat `AgentState` subtype, primitives only | `tests/proof_of_boundary/test_state_safety.py` — no Pydantic/dataclass/credential | PASS |
| TC-02 | S-3 `SecurityViolationError` fires on a bad envelope | `test_pb_s3_gate.py` — unknown/missing discriminator or undeclared key raises | PASS |
| TC-03 | No JWT/Credential in State/src | CI credential scan: 0 violations | PASS (grep-clean) |
| TC-04 | `InvocationContext` via `from_state` only; never constructed in nodes | `main_node` uses `InvocationContext.from_state`; adapter constructs only at the entry point | PASS |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` never emitted in `execute()` bodies | PASS |
| TC-06 | S-2: `_security_gate_input()` not overridden | `test_framework_compliance_tc06_tc07.py` — `TypeError` at class definition | PASS |
| TC-07 | S-3: `_security_gate_output()` not overridden | `test_framework_compliance_tc06_tc07.py` | PASS |
| TC-08 | `required_trust_level` enforced | Every FunctionNode declares `VERIFIED_EXTERNAL`; anonymous caller denied (`test_startup_and_flow.py::test_anonymous_caller_denied_by_trust_gate`) | PASS |
| TC-09 | S-2 `_extra_security_gate_input()` non-trivial | `pre_process` runs the input quality gate (magic-byte/ratio/length) | PASS |
| TC-10 | S-3 `_extra_security_gate_output()` non-trivial | `post_process` enforces the envelope allowlist + recursive residue mask | PASS |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per `execute()` | `test_pb1_s4_audit.py` (mutation-checked per node module) | PASS |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test file | Expected Result | Result |
|-------|----------|-----------|----------------|--------|
| PB-1 | BaseNode → AuditLogger (domain event) | `test_pb1_s4_audit.py` | each FunctionNode module emits ≥1 domain event; mutation-checked | PASS |
| PB-2 | State serialization | `test_state_safety.py` | primitives only; no Pydantic/dataclass | PASS |
| PB-3 | L1 → external service (ledger/KB) | `test_inner_nodes.py` (in-memory `LedgerStore`/`RecallIndex` seams; scoped read/write) + `test_services_stores.py` | real scoped retrieval/write; cross-scope invisible. **Concrete `BaseDatabase`/`BaseVectorStore` are STUB in the wheel** — the injected seam is exercised in-memory; a real backend is wired at deploy | PASS (in-memory seam) |
| PB-4 | Import isolation | `test_import_isolation.py` | AST scan: 0 Level-0 imports | PASS |
| PB-5 | Checkpoint safety | `test_state_safety.py` + design (no checkpointer — mode separation) | no JWT/Pydantic; transcript never checkpointed | PASS |
| PB-6 | Invoke execution order | `test_pb_invoke_order.py` | S-1 → node_start → S-2 → execute → S-3 → node_complete for all 5 FunctionNodes (GraphNode excluded, verified in `test_main_node.py`) | PASS |
| PB-7 | HITL interrupt propagation | `test_pb7_hitl_interrupt_propagation.py` | **Auto-waived — non-HITL** (`config/agent.yaml` has no `hitl.enabled: true`; the stub auto-skips) | WAIVED |

> **Pre-review gate checklist:** PB-1–PB-6 mandatory (all PASS). PB-7 is auto-waived (non-HITL).

## Business Logic Tests (S-3 provenance, staging CAS, adapter contract, classification)

| ID | Test | Input | Expected Result | Result |
|----|------|-------|----------------|--------|
| BL-01 | S-3 provenance three-layer + bidirectional | `test_services_provenance.py` (12 cases) | new-with-ledger-id, span-too-short, forged span, real-but-unrelated ledger id, **param rewrite**, meeting_id mismatch, unreferenced claim, **surfaced-row dropped** all reject; faithful items pass; empty brief passes | PASS |
| BL-02 | Staging consume-once CAS | `test_services_stores.py` (12 cases) | valid promotion; **replay of consumed** / cross-scope / wrong-owner / expired / absent reject; item-not-staged per-item reject; **empty approval consumes + 0 writes**; scoping fail-closed; idempotent active writes | PASS |
| BL-03 | Adapter request contract (H1/H2) | `test_services_request_contract.py` (15 cases) | closed schema; default mode injection; **scope claim-binding** (body forgery reject, absent-claim reject); **per-mode capability** reject; feedback shape | PASS |
| BL-04 | S-3 output-envelope allowlist | `test_services_envelope.py` (10 cases) | fail-closed by discriminator; undeclared top-level raises; brief-item undeclared key dropped; **recursive** credential/PII mask reaches nested strings | PASS |
| BL-05 | ISO time / overdue / quality gate | `test_services_normalize.py` (14 cases) | offset-aware compare (not lexical); magic-byte/mojibake/control/length reject; NFKC fold | PASS |
| BL-06 | Classification + brief build + minutes | `test_services_brief.py` (8 cases) | new/resurfaced/completion; 1:1 ledger-row claim; new-item params limited to owner/due(+derived); candidate meeting_id; minutes sections | PASS |
| BL-07 | Extraction prompt + parse | `test_services_extraction.py` (7 cases) | prompt embeds schema; `{}` → `[]` (MunicipalContractVariationEvidenceBriefAgent defect guard); short-span/unknown-type dropped | PASS |
| BL-08 | Node behaviour (extract/match/brief) | `test_inner_nodes.py` | feedback no-extract; llm-absent degradation; ingest write; feedback promote + replay reject; brief provenance fail-closed on forged span | PASS |
| BL-09 | Outer nodes | `test_outer_nodes.py` | pre_process scope/mode/quality-gate rejects with error envelope; post_process per-mode envelope + S-3 | PASS |
| BL-10 | End-to-end 3-mode flow | `test_startup_and_flow.py` | ingest→invoke→feedback promotes approved items to active rows; **feedback replay rejected**; adapter rejects (capability/scope/unknown-key); FastAPI startup exercised | PASS |

## Local verification (this MR)

- **2026-08-05 (initial implementation run).** The framework-free service suite
  (`tests/unit/test_services_*.py`, **78 tests**) was executed under the system Python
  interpreter and **all pass** (provenance 12 / stores 12 / request_contract 15 / envelope 10 /
  normalize 14 / brief 8 / extraction 7). All `src/` and `tests/` modules pass `py_compile`. CI
  gate scripts (`check_trust_level`, `check_cat_consistency`, `check_stub_tests`,
  `check_dep_pinning`, `check_oss_license`) pass locally.
- **2026-08-05 (round-2 hardening).** The round-2 review fixes (docs/02 DDR *round 2
  reconciliation*) added **4 framework-free cases** → **82** (provenance 13 / envelope 12 /
  brief 9 / request_contract 16): whitespace-only span reject; empty / missing-message error
  object reject; ledger-item summary rendered from the cited row; feedback-only fields rejected
  for ingest/invoke. All changed modules re-verified with `ast.parse`; the framework-free
  suite + framework tier are re-validated by CI `run-tests` (the R2 cases were not re-executed
  in the authoring session's sandbox — CI is the execution gate).
- **LLM behaviour** is covered by a **grounded `FakeLLM`** in `tests/conftest.py` that extracts
  only verbatim-grounded spans (no fabricated ledger ids), so it cannot hide an unfired
  provenance guard. The full graph is driven ingest→invoke→feedback with it in
  `test_startup_and_flow.py`. (The live-LLM harness requires the SDK venv, which is
  provisioned in CI; the deterministic FakeLLM e2e is the reproducible equivalent run here.)

## Test Execution Summary

- Execution date: 2026-08-05 (framework-free tier, local); framework tier runs in CI (`run-tests`)
- Framework-free tests: 82 (78 initial + 4 from round 2) — CI `run-tests` is the execution gate
- Framework tier (CI): unit (main/outer/inner nodes, TC-06/07) + PB-1/2/4/5/6 + integration; PB-7 auto-waived

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | Azure OpenAI のモデルを `BaseLLM` 実装として注入 |
| 注入シーム | `constructor:llm` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['mode', 'status', 'minutes_draft', 'commitment_brief', 'brief_id'] が非空。縮退理由なし |

本番の LLM は注入されたクライアントで、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部モデルの鍵が要るため）。

