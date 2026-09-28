"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Standalone HTTP entry point (adapter only — no business logic here). The adapter validates
# and normalizes the request BEFORE State construction (docs/02_design.md §Adapter request
# contract): closed schema + per-mode capability + scope claim-binding, then PII-masks the
# transcript (S-2 at ingress) and resolves as_of_utc exactly once via an injectable clock.
# The platform path (AgentGateway) relies on its own AuthMiddleware to populate the same
# claims; this file is the standalone equivalent.

from __future__ import annotations

import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.graph.graph import MeetingCommitmentLedgerAgent
from src.services import request_contract
from src.services.clock import UtcClock, resolve_as_of_utc

app = FastAPI(title="MeetingCommitmentLedgerAgent")

_NAMESPACE = "cmn"
_AGENT_NAME = "cmn-c2-687"

# Injectable server-side clock seam (determinism test replaces app.state.clock with a FixedClock).
app.state.clock = UtcClock()

# Dev/CI seams (in-memory ledger/recall; llm=None -> deterministic degradation). A real
# deployment injects BaseDatabase/BaseVectorStore/BaseLLM-backed seams here.
agent = MeetingCommitmentLedgerAgent(config={"max_retry": 3})
agent.compile()
agent.provision_secrets(secrets_factory(namespace=_NAMESPACE, agent_name=_AGENT_NAME))


@app.post("/invoke")
async def invoke(request: Request):
    trust, token_authenticated = _resolve_trust(request)

    # correlation_id is allocated FIRST so even the earliest rejects (bad JSON / non-object)
    # are audited — no reject is an audit blind spot (docs/02 §S-4 adapter rejects).
    correlation_id = str(uuid4())

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        emit_trace_event("adapter_request_rejected", {"reason": "schema_wrong_type"}, {"correlation_id": correlation_id})
        return _reject_response("schema_wrong_type")
    if not isinstance(body, dict):
        emit_trace_event("adapter_request_rejected", {"reason": "schema_wrong_type"}, {"correlation_id": correlation_id})
        return _reject_response("schema_wrong_type")

    # session_id is a transport field (not part of the closed domain schema); coerce + bound.
    session_id = str(body.pop("session_id", "") or "")[:200]
    as_of_utc = resolve_as_of_utc(app.state.clock)
    claims = _claims(request, token_authenticated=token_authenticated)

    try:
        validated = request_contract.validate_request(body, claims, as_of_utc)
    except request_contract.Rejection as rej:
        emit_trace_event("adapter_request_rejected", {"reason": rej.reason}, {"correlation_id": correlation_id})
        # client message is a FIXED per-reason string — never rej.message, which can echo an
        # untrusted key name / PII from the request body; mode is validated to enum-or-None.
        return _reject_response(rej.reason, mode=body.get("mode"))

    # PII masking at ingress (S-2). The transcript AND the meeting metadata (title/participants
    # ride in input_context, which the framework S-2 does NOT scan) are masked here so no raw
    # personal name enters State or the minutes draft.
    transcript = validated.get("transcript")
    user_input = _mask_pii(transcript) if isinstance(transcript, str) else ""

    input_context = {
        "mode": validated["mode"],
        "meeting_meta": _mask_meta(validated.get("meeting_meta")),
        "tenant_id": validated["tenant_id"],
        "org_id": validated["org_id"],
        "as_of_utc": as_of_utc,
        "brief_id": validated.get("brief_id"),
        "approved_item_ids": validated.get("approved_item_ids"),
    }

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=session_id or str(uuid4()),
            correlation_id=correlation_id,
            caller_trust_level=trust,
            caller_id=claims.get("caller_id", ""),
        )
        return agent.invoke(user_input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health():
    return {"status": "ok", "agent": _AGENT_NAME}


# ── STG provisional scope (staging evidence path only) ──────────────────────────
# 🔴 なぜ要るか: `deploy-stg` の provisional mode は AuthMiddleware を配線せずに
#    `uvicorn src.api.server:app` を起動し、`scripts/stg_invoke_evidence.py` は
#    `Authorization: Bearer` **しか**送らない。よって `request.state` にも
#    `X-Tenant-Id` にも claim が無く、`_bind_scope` が fail-closed で
#    `scope_claim_missing` を返し、evidence が構造的に FAIL する。
#    同系テンプレートの既存解（Shooting Plan Risk Advisory & Incident History Agent / Outsourcing SLA Deviation RCA Rebuttal-Brief Agent / SupplierECNImpactBriefAgent / Professional-Services Engagement Estimation-Phase Agent）と同じく、
#    **entry-token 認証済みの呼び出しに限り** STG default claim を供給する。
#    これは非 HITL の fail-closed エージェントに対する既存の前例に倣う。
#    HITL AG は正規パケットでも `awaiting_human` になるのでこの経路を持たない。
#
# 🔴 env の危険を env で守る循環を避けるため、二重ゲートにする（Professional-Services Engagement Estimation-Phase Agent と同形）:
#    1. `STG_PROVISIONAL_MODE=true` を宣言していなければ値は inert（None を返す）
#    2. startup で相互排他を強制 — 宣言が無いのに STG_DEFAULT_* を持つプロセスは
#       起動を拒否する。これが無いと、本番に INVOKE_AUTH_TOKEN と STG_DEFAULT_* が
#       同居した瞬間に「scope を持たない bearer 呼び出しが既定テナントに到達する」
#       認可バイパスになる（残余リスクではなく実バイパス）。
_STG_PROVISIONAL_ENV = "STG_PROVISIONAL_MODE"
_STG_DEFAULT_TENANT_ENV = "STG_DEFAULT_TENANT_ID"
_STG_DEFAULT_ORG_ENV = "STG_DEFAULT_ORG_ID"
_STG_DEFAULT_CAPS_ENV = "STG_DEFAULT_CAPABILITIES"
_STG_ENVS = (_STG_DEFAULT_TENANT_ENV, _STG_DEFAULT_ORG_ENV, _STG_DEFAULT_CAPS_ENV)


def _provisional_stg_mode() -> bool:
    return os.environ.get(_STG_PROVISIONAL_ENV, "").strip().casefold() == "true"


def assert_scope_env_is_sane() -> None:
    """Refuse to start a non-provisional deployment that carries STG scope env.

    The STG default claim is a provisional-deployment-only path. A production process
    that happens to define both INVOKE_AUTH_TOKEN and STG_DEFAULT_* would otherwise let
    any bearer caller reach the default tenant without a verified claim.
    """
    if _provisional_stg_mode():
        return
    present = [n for n in _STG_ENVS if os.environ.get(n, "").strip()]
    if present:
        raise RuntimeError(
            f"{'/'.join(present)} is set without {_STG_PROVISIONAL_ENV}=true. "
            "The STG default claim is a provisional-deployment-only path and must "
            "never be reachable in production."
        )


def _stg_default_claims() -> dict | None:
    """STG default claims, or None when this deployment did not declare itself provisional.

    Partial configuration is NOT rescued — every key must be present, otherwise the
    caller stays scope-less and the request contract rejects it fail-closed.
    """
    if not _provisional_stg_mode():
        return None
    tenant = os.environ.get(_STG_DEFAULT_TENANT_ENV, "").strip()
    org = os.environ.get(_STG_DEFAULT_ORG_ENV, "").strip()
    raw_caps = os.environ.get(_STG_DEFAULT_CAPS_ENV, "").strip()
    caps = [c.strip() for c in raw_caps.split(",") if c.strip()]
    if not tenant or not org or not caps:
        return None
    return {"tenant": tenant, "org": org, "capabilities": caps, "caller_id": "stg-provisional"}


@app.on_event("startup")
def _startup_scope_env_guard() -> None:
    assert_scope_env_is_sane()


def _resolve_trust(request: Request) -> tuple[TrustLevel, bool]:
    """Resolve caller authentication for standalone and staging deployments. When
    INVOKE_AUTH_TOKEN is set, an otherwise-ANONYMOUS caller must present it as a Bearer token
    and runs at VERIFIED_EXTERNAL. Middleware-established trust is never demoted. The token is
    a deployment-level caller credential read before any InvocationContext exists, never an
    agent secret.

    Returns `(trust, token_authenticated)`. The second value gates the STG default claim:
    only a caller that presented the entry token may fall back to it. Middleware-established
    trust deliberately leaves it False — a middleware-vouched caller has its own claims.
    """
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            return TrustLevel.VERIFIED_EXTERNAL, True
    return trust, False


def _claims(request: Request, token_authenticated: bool = False) -> dict:
    """Caller claims — AUTHENTICATED source only by default.

    🔴 Claims are read from `request.state` (populated by the platform AuthMiddleware from the
    verified token). Client-supplied `X-Tenant-Id`/`X-Org-Id`/`X-Capabilities` headers are
    trusted ONLY when the deployment explicitly opts in via `STG_TRUST_CLAIM_HEADERS=true`
    (standalone/STG/dev harness) — otherwise a token holder could forge another tenant's scope
    or grant itself a capability (review finding, High). In production the flag is unset, so absent
    middleware claims are rejected fail-closed by the request contract."""
    st = request.state
    tenant = getattr(st, "tenant", None)
    org = getattr(st, "org", None)
    caps = getattr(st, "capabilities", None)
    caller_id = getattr(st, "caller_id", "") or ""
    if os.environ.get("STG_TRUST_CLAIM_HEADERS") == "true":
        tenant = tenant or request.headers.get("x-tenant-id")
        org = org or request.headers.get("x-org-id")
        if caps is None:
            raw = request.headers.get("x-capabilities", "")
            caps = [c.strip() for c in raw.split(",") if c.strip()]
        caller_id = caller_id or request.headers.get("x-caller-id", "")
    # STG provisional fallback — entry-token callers only, and only when this deployment
    # declared itself provisional (see _stg_default_claims).
    #
    # 🔴 発火条件は「claim が **一つも無い**」であって「揃っていない」ではない
    #    （敵対レビュー Medium）。後者だと、middleware が本物の tenant だけを
    #    立てて capabilities を落とした不整合な統合で、**実テナントが STG テナントに
    #    黙って差し替わる**（cross-tenant かつ監査上の呼び出し元喪失）。
    #    部分的にでも claim があるなら、それは不整合として request contract に
    #    fail-closed で弾かせるのが正しい。
    if token_authenticated and not (tenant or org or caps or caller_id):
        stg = _stg_default_claims()
        if stg is not None:
            return stg
    return {"tenant": tenant, "org": org, "capabilities": caps or [], "caller_id": caller_id}


def _mask_meta(meta: object) -> dict:
    """Mask personal names in meeting metadata (title/participants) before it enters State."""
    if not isinstance(meta, dict):
        return {}
    out = dict(meta)
    if isinstance(out.get("title"), str):
        out["title"] = _mask_pii(out["title"])
    if isinstance(out.get("participants"), list):
        out["participants"] = [_mask_pii(p) if isinstance(p, str) else p for p in out["participants"]]
    return out


def _mask_pii(text: str) -> str:
    findings = detect_pii(text)
    if not findings:
        return text
    masked = text
    for f in reversed(findings):  # reverse to preserve offsets
        masked = masked[: f["start"]] + "[MASKED]" + masked[f["end"] :]
    return masked


# FIXED, PII-free client-facing messages per adapter reject reason. The client NEVER receives
# request_contract.Rejection.message (it can echo an attacker-chosen key name or PII from the
# body); the internal message stays in the audit event only (docs/02 §S-4).
_ADAPTER_MESSAGE = {
    "schema_wrong_type": "request body is not a valid JSON object",
    "schema_unknown_key": "request contains an unrecognized field",
    "mode_invalid": "mode must be one of ingest/invoke/feedback",
    "transcript_missing": "transcript is required for this mode",
    "transcript_not_allowed_for_feedback": "transcript is not permitted for feedback",
    "meeting_meta_missing": "meeting_meta is required for this mode",
    "meeting_meta_unknown_key": "meeting_meta contains an unrecognized field",
    "meeting_id_missing": "meeting_meta.meeting_id is required",
    "scope_claim_missing": "an authenticated tenant/org scope is required",
    "scope_body_claim_mismatch": "request scope does not match the authenticated caller",
    "capability_denied": "the caller lacks the capability for this mode",
    "brief_id_missing": "brief_id is required for feedback",
    "brief_id_not_allowed": "this field is not permitted for the requested mode",
    "approved_item_ids_missing": "approved_item_ids (list of strings) is required for feedback",
}
_GENERIC_REJECT_MESSAGE = "the request was rejected"


def _safe_mode(mode: Any) -> Any:
    """Reflect the request mode in an error body only when it is a valid enum value, else None
    (an arbitrary body value must never be echoed back — matches the S-3 error-envelope rule)."""
    return mode if isinstance(mode, str) and mode in ("ingest", "invoke", "feedback") else None


def _reject_response(reason: str, mode: Any = None) -> JSONResponse:
    """Adapter-level reject -> HTTP 400 with a PII-free, envelope-shaped body (fixed message)."""
    return JSONResponse(
        status_code=400,
        content={
            "mode": _safe_mode(mode),
            "status": "error",
            "error": {"code": reason, "message": _ADAPTER_MESSAGE.get(reason, _GENERIC_REJECT_MESSAGE)},
        },
    )
