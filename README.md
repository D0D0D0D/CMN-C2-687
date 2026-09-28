# CMN-C2-687 — Meeting Commitment Ledger & Follow-up Recall Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: CMN

## Overview

This agent keeps a durable ledger of what a team actually committed to in its meetings, and
compares each new transcript against it. It extracts decisions and action items, matches them to
open commitments, and returns a brief that separates new commitments from ones resurfacing past
their due date, from statements that contradict an earlier decision, from work that now looks
complete. It also drafts the minutes.

It suits recurring meetings where the risk is not "we forgot to write it down" but "we wrote it
down and it drifted": a programme with a standing action list, an operations review whose
decisions must not silently reverse, a team that needs to see what is overdue without a person
re-reading months of notes.

The division of labour between the language model and the deterministic layer is the point of
the design. The model reads the transcript and drafts prose; it never decides what the ledger
says:

- **Nothing is written to the ledger by the agent's own judgement.** A run in `invoke` mode
  stages candidates and returns a `brief_id`; they become active commitments only through a
  separate `feedback` call carrying the ids a person approved.
- **Every brief item is anchored to the record.** An item cites the evidence it came from and a
  verbatim span from the source. Provenance is checked in both directions — a claim with no
  reference, a reference that does not exist, a span too short to be meaningful, or values that
  do not match the cited ledger row all fail the run rather than shipping a plausible item.
  The failure is whole-brief, not per-item: no partial brief is returned.
- **The overdue basis is fixed once per request, server-side.** The "as of" timestamp comes from
  an injectable server clock at the adapter, never from the caller — a client-supplied value
  would be a forgeable basis for deciding what is late — so the same transcript yields the same
  overdue set regardless of the host's timezone.
- **A staged brief can be approved once.** Approval consumes the `brief_id` through a
  compare-and-set, so a replayed feedback call is rejected deterministically instead of
  double-applying.
- **Missing infrastructure degrades, it does not crash.** With no language model or ledger
  injected, the run still completes and reports a `degradation_reason` rather than raising.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation for transcript extraction and matching — the template names no
  model and bundles no provider client. Without one the run degrades rather than fails;
- a ledger store and a recall index. The shipped `src/services/stores.py` seams are **in-memory,
  for development and CI**. A deployment injects `BaseDatabase`- and `BaseVectorStore`-backed
  implementations with the same method contract;
- caller authentication and, for a multi-tenant deployment, middleware that establishes the
  caller's tenant, organisation and capabilities.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) | Retry budget |
| `timeout_seconds` (`60`) | Execution timeout — extraction and matching are model-bound |
| `budget_usd_advisory` (`2.50`) | Advisory per-invocation cost ceiling. The framework does not enforce it; enforcement, if any, is caller- or client-side |
| `default_mode` (`invoke`) | Mode used when the request does not name one |
| `staging_ttl_seconds` (`604800`) | How long a staged `brief_id` stays approvable — 7 days |
| `recall_top_k` (`8`), `recall_score_threshold` (`0.70`), `match_open_only` (`true`) | Recall and matching tuning |
| `input_min_chars` (`20`), `input_max_chars` (`200000`) | Transcript length bounds |
| `input_max_replacement_char_ratio` (`0.02`), `input_max_control_char_ratio` (`0.02`), `input_min_printable_ratio` (`0.90`) | Input quality gate — a transcript that decodes badly is rejected before it reaches the model |
| `memory_enabled` (`false`), `hitl.enabled` (`false`) | Off by design. The review step is a separate stateless `feedback` call, so no suspend/resume checkpointer is needed |

The template does not join a meeting, transcribe audio, send reminders, or write to a calendar or
issue tracker. Input is text.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform, which supplies the entry adapter, the authentication middleware that establishes tenant/org/capability claims, and the secret provider. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not pretend to work when
its dependencies are absent. The standalone server in `src/api/server.py` does start without the
platform — the manifest declares no required secrets — but it wires the development seams: an
in-memory ledger and recall index, and no language model. A request then completes with a
`degradation_reason` instead of a real brief. It will also **refuse to start** if the process
carries the staging scope-default environment variables without declaring itself provisional,
because that combination would let any bearer caller reach a default tenant without a verified
claim. The test suite runs without a platform connection.

## Input

`POST /invoke` takes a JSON object with a closed schema: `mode`, `transcript`, `meeting_meta`
(`meeting_id`, `title`, `date`, `participants`), `tenant_id`, `org_id`, `brief_id`,
`approved_item_ids`, plus a transport-level `session_id`. An unrecognised field is rejected — the
schema is an allowlist, not a filter.

There are three modes, each requiring its own capability:

| Mode | Purpose | Capability |
|---|---|---|
| `ingest` | Bootstrap the ledger from past minutes | `ledger:bootstrap` |
| `invoke` | Analyse a transcript and stage candidates | `ledger:stage` |
| `feedback` | Apply the items a person approved | `ledger:apply` |

`invoke` is not exempt from authorization: it writes a staging row, so it carries a capability
like the other two.

Callers must be authenticated, and the tenant/organisation scope comes from the **verified**
caller, never from the request body — a body value that disagrees with the caller's claim is
rejected rather than honoured. When `INVOKE_AUTH_TOKEN` is set on the server environment, a
matching `Authorization: Bearer` header promotes an otherwise-anonymous caller to
`VERIFIED_EXTERNAL`; trust already established by middleware is never demoted. Client-supplied
scope headers are honoured only when a deployment explicitly opts in for a development harness.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "mode": "invoke",
    "session_id": "example-session",
    "transcript": "田中: 来週までに見積を出します。決定: A 案で進める。",
    "meeting_meta": {
      "meeting_id": "weekly-2026-09-01",
      "title": "Weekly review",
      "date": "2026-09-01",
      "participants": ["A", "B"]
    }
  }'
```

Rejections carry a fixed, personal-data-free message per reason — `schema_unknown_key`,
`mode_invalid`, `transcript_missing`, `meeting_id_missing`, `scope_claim_missing`,
`scope_body_claim_mismatch`, `capability_denied`, `brief_id_missing` and the rest. The internal
detail stays in the audit event, because a validation message can otherwise echo an
attacker-chosen field name or personal data straight back to the caller.

Personal names in the transcript **and** in the meeting metadata are masked at the adapter,
before anything enters state — the framework's own input gate does not scan the metadata, so the
adapter does it explicitly.

## Output

The response envelope is allowlisted per mode: an undeclared top-level key is a failure, not a
silently dropped field.

| Mode | Fields |
|---|---|
| `invoke` | `mode`, `status`, `minutes_draft`, `commitment_brief`, `brief_id`, `degradation_reason` |
| `ingest` | `mode`, `status`, `ingest_summary`, `degradation_reason` |
| `feedback` | `mode`, `status`, `write_result`, `degradation_reason` |
| error | `mode`, `status`, `error` (`code` and `message`) |

`commitment_brief` holds four categories — `new`, `resurfaced`, `contradictions`, `completions` —
and each item carries `item_id`, `category`, `summary`, `params`, `evidence_ids` and
`evidence_span`.

```json
{
  "mode": "invoke",
  "status": "success",
  "brief_id": "b-0001",
  "minutes_draft": "## Decisions\n- ...",
  "commitment_brief": {
    "new": [
      {
        "item_id": "i-0001",
        "category": "new",
        "summary": "見積を提出する",
        "params": {"owner": "[MASKED]", "due": "2026-09-08"},
        "evidence_ids": ["e-0001"],
        "evidence_span": "来週までに見積を出します"
      }
    ],
    "resurfaced": [],
    "contradictions": [],
    "completions": []
  }
}
```

Values above are illustrative; real output depends on the transcript and the ledger contents.

## Security and Limitations

Tenant and organisation scope is bound to the authenticated caller and every ledger read and
write is scoped by it, so a caller cannot reach another tenant's commitments by asking. All three
write paths require a capability. The output gate enforces the per-mode allowlist and then masks
credential and personal-data residue recursively through the nested brief — the draft minutes
live only inside that envelope, so they cannot bypass the mask by travelling as a separate
top-level string.

Raw transcript text is never written to the ledger: the ledger holds the extracted commitment,
not the source document.

Known limits:

- **A verbatim span proves a quote exists, not that the summary is faithful to it.** The
  provenance check is strong about attribution and weaker about interpretation.
- **Approving a completion does not yet close the cited row.** Approved candidates are promoted
  as new active commitments; per-category close-out semantics are a scoped follow-up, so a
  completion approval currently leaves the cited row active. This is safe — nothing is lost —
  but it is not the whole story.
- **Recall is advisory.** The ledger is authoritative for matching; the recall index widens the
  audit signal rather than deciding the outcome.
- **The shipped stores are in-memory.** Without injected backends nothing survives a restart.
- **Personal-name masking uses the framework detector.** Names it does not recognise are not
  masked.

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be checked against the source record by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern with an inner
  workflow subgraph
- Generation mode: `llm`
- HITL: not enabled in-graph — review happens between two stateless calls (`invoke` then
  `feedback`), so no checkpointer is required
- Persistence: an injected ledger store and recall index; none bundled
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using the in-memory store seams and a fake language
model. Running the agent itself against a bare `uvicorn src.api.server:app` starts, but answers
from those same development seams.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the adapter request contract, the
provenance rules and the security design, together with the record of the design decisions and
how the implementation resolved each one. `docs/03_test_spec.md` records the test specification
and the verification results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
