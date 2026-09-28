# CMN-C2-687 — Integration: FastAPI startup (module import exercised) + adapter rejects +
# full ingest -> invoke -> feedback flow through the compiled graph with a grounded FakeLLM.
#
# 🔴 Importing src.api.server runs the module top-level (agent construction + compile +
# provision_secrets), and TestClient(app) runs the ASGI lifespan — so a startup blocker
# (manifest/config, TrustLevel value, secret wiring) is caught here, not silently skipped by
# `pytest tests/`.

from __future__ import annotations

from fastapi.testclient import TestClient


from src.api.server import app
from src.schemas.state import State  # noqa: F401 — import-exercises the schema module
from tests.conftest import build_agent, make_ctx

_TOKEN = "stg-smoke-token"


def _post(client, body, caps="ledger:stage", tenant="t1", org="o1", token=_TOKEN):
    headers = {"X-Tenant-Id": tenant, "X-Org-Id": org, "X-Capabilities": caps, "X-Caller-Id": "caller-1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/invoke", json=body, headers=headers)


class TestStartupAndAdapter:
    def test_health_ok(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_TRUST_CLAIM_HEADERS", "true")  # standalone: trust claim headers
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_invoke_success_degraded_without_llm(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_TRUST_CLAIM_HEADERS", "true")  # standalone: trust claim headers
        with TestClient(app) as client:
            r = _post(
                client,
                {"mode": "invoke", "transcript": "承認者が来週の定例会議までに予算案を確定すると決定した。", "meeting_meta": {"meeting_id": "M1"}},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            # module agent has llm=None -> deterministic degradation, still status=success.
            assert body["status"] == "success"
            assert body["output"]["mode"] == "invoke"

    def test_capability_denied(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_TRUST_CLAIM_HEADERS", "true")  # standalone: trust claim headers
        with TestClient(app) as client:
            r = _post(client, {"mode": "invoke", "transcript": "x" * 30, "meeting_meta": {"meeting_id": "M1"}}, caps="")
            assert r.status_code == 400 and r.json()["error"]["code"] == "capability_denied"

    def test_scope_missing(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_TRUST_CLAIM_HEADERS", "true")  # standalone: trust claim headers
        with TestClient(app) as client:
            r = _post(client, {"mode": "invoke", "transcript": "x" * 30, "meeting_meta": {"meeting_id": "M1"}}, tenant="")
            assert r.status_code == 400 and r.json()["error"]["code"] == "scope_claim_missing"

    def test_unknown_key_rejected(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_TRUST_CLAIM_HEADERS", "true")  # standalone: trust claim headers
        with TestClient(app) as client:
            r = _post(client, {"mode": "invoke", "transcript": "x" * 30, "meeting_meta": {"meeting_id": "M1"}, "evil": 1})
            assert r.status_code == 400 and r.json()["error"]["code"] == "schema_unknown_key"

    def test_anonymous_caller_denied_by_trust_gate(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_TRUST_CLAIM_HEADERS", "true")  # standalone: trust claim headers
        with TestClient(app) as client:
            # no Bearer token -> stays ANONYMOUS -> S-1 denies at pre_process -> error envelope.
            r = _post(client, {"mode": "invoke", "transcript": "承認者が予算を確定する。", "meeting_meta": {"meeting_id": "M1"}}, token=None)
            assert r.status_code == 200
            assert r.json()["status"] == "error"


class TestEndToEndFlow:
    """ingest -> invoke -> feedback through the compiled graph with a grounded FakeLLM."""

    def test_ingest_invoke_feedback(self, fake_llm, ledger):
        agent = build_agent(llm=fake_llm, ledger=ledger)
        ctx = make_ctx(caller_id="caller-1")

        # 1) ingest bootstrap — extracts + writes active rows.
        ingest_ic = {"mode": "ingest", "tenant_id": "t1", "org_id": "o1", "as_of_utc": "2026-08-05T00:00:00Z", "meeting_meta": {"meeting_id": "M0"}}
        agent.invoke("承認者が予算を確定する。担当Aが在庫を確認する。", ctx=ctx, input_context=ingest_ic)
        assert len(ledger.fetch_open_commitments("t1", "o1")) >= 1

        # 2) invoke — presents a brief with item_ids + a brief_id.
        invoke_ic = {"mode": "invoke", "tenant_id": "t1", "org_id": "o1", "as_of_utc": "2026-08-05T00:00:00Z", "meeting_meta": {"meeting_id": "M1"}}
        out = agent.invoke("担当Bが来週金曜までに新しい設計書のレビューを完了すると決定した。", ctx=ctx, input_context=invoke_ic)
        assert out["status"] == "success"
        fo = out["output"]
        brief_id = fo["brief_id"]
        item_ids = [it["item_id"] for cat in fo["commitment_brief"].values() for it in cat]
        assert brief_id and item_ids  # something was presented and staged

        # 3) feedback — promote the approved items -> active rows.
        before = len(ledger.fetch_open_commitments("t1", "o1"))
        fb_ic = {
            "mode": "feedback",
            "tenant_id": "t1",
            "org_id": "o1",
            "as_of_utc": "2026-08-06T00:00:00Z",
            "brief_id": brief_id,
            "approved_item_ids": item_ids,
        }
        fb = agent.invoke("", ctx=ctx, input_context=fb_ic)
        assert fb["status"] == "success"
        assert fb["output"]["write_result"]["applied"] == item_ids
        assert len(ledger.fetch_open_commitments("t1", "o1")) == before + len(item_ids)

    def test_feedback_replay_is_rejected(self, fake_llm, ledger):
        agent = build_agent(llm=fake_llm, ledger=ledger)
        ctx = make_ctx(caller_id="caller-1")
        invoke_ic = {"mode": "invoke", "tenant_id": "t1", "org_id": "o1", "as_of_utc": "2026-08-05T00:00:00Z", "meeting_meta": {"meeting_id": "M1"}}
        out = agent.invoke("担当Bが来週金曜までに新しい設計書のレビューを完了すると決定した。", ctx=ctx, input_context=invoke_ic)
        brief_id = out["output"]["brief_id"]
        item_ids = [it["item_id"] for cat in out["output"]["commitment_brief"].values() for it in cat]
        fb_ic = {"mode": "feedback", "tenant_id": "t1", "org_id": "o1", "as_of_utc": "2026-08-06T00:00:00Z", "brief_id": brief_id, "approved_item_ids": item_ids}
        agent.invoke("", ctx=ctx, input_context=dict(fb_ic))
        replay = agent.invoke("", ctx=ctx, input_context=dict(fb_ic))
        assert replay["status"] == "error"  # consumed brief_id -> deterministic reject

    def test_cross_tenant_forgery_via_body_rejected(self, monkeypatch):
        # scope forgery is caught at the adapter (request contract), not the graph.
        from src.services import request_contract as rc

        claims = {"tenant": "t1", "org": "o1", "capabilities": ["ledger:stage"], "caller_id": "c1"}
        try:
            rc.validate_request(
                {"mode": "invoke", "transcript": "x" * 30, "meeting_meta": {"meeting_id": "M1"}, "tenant_id": "t2"},
                claims,
                "2026-08-05T00:00:00Z",
            )
            assert False, "expected a scope-forgery rejection"
        except rc.Rejection as r:
            assert r.reason == rc.R_SCOPE_FORGED
