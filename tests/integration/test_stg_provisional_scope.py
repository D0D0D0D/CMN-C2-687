# CMN-C2-687 — Integration: STG provisional scope fallback (staging evidence path).
#
# 🔴 なぜこの経路が要るか: `deploy-stg` の provisional mode は AuthMiddleware を配線せずに
#    `uvicorn src.api.server:app` を起動し、`scripts/stg_invoke_evidence.py` は
#    `Authorization: Bearer` しか送らない。claim が一つも無いので `_bind_scope` が
#    fail-closed で `scope_claim_missing` を返し、evidence が構造的に FAIL する。
#    非 HITL の fail-closed エージェントに対する既存の前例（Shooting Plan Risk Advisory & Incident History Agent 型）に倣う。
#
# 🔴 テストの立て方: fail-closed の主張は「通る条件」だけでは証明できない。
#    対照実験を対で置く — env なし=拒否 / **部分設定=拒否**（部分設定を救済しない）/
#    全設定=200。加えて、宣言が無いのに STG_DEFAULT_* を持つプロセスは startup で
#    落ちること（本番同居時の認可バイパス防止）を **app の lifespan 経由で**検査する。

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.server import app, assert_scope_env_is_sane

_TOKEN = "stg-smoke-token"
_BODY = {
    "mode": "invoke",
    "transcript": "担当者が来週の定例会議までに次期予算案の初稿を確定すると決定した。",
    "meeting_meta": {"meeting_id": "STG-MTG-001"},
}


def _post_bearer_only(client, body=None):
    """CI の evidence スクリプトと同じ呼び方 — Authorization ヘッダ **だけ**を送る。"""
    return client.post(
        "/invoke", json=dict(body or _BODY), headers={"Authorization": f"Bearer {_TOKEN}"}
    )


def _clear_stg_env(monkeypatch):
    for name in ("STG_PROVISIONAL_MODE", "STG_DEFAULT_TENANT_ID",
                 "STG_DEFAULT_ORG_ID", "STG_DEFAULT_CAPABILITIES",
                 "STG_TRUST_CLAIM_HEADERS"):
        monkeypatch.delenv(name, raising=False)


def _set_full_stg_env(monkeypatch):
    monkeypatch.setenv("STG_PROVISIONAL_MODE", "true")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
    monkeypatch.setenv("STG_DEFAULT_ORG_ID", "stg-org")
    monkeypatch.setenv("STG_DEFAULT_CAPABILITIES", "ledger:stage")


class TestStgProvisionalScope:
    def test_bearer_only_without_stg_env_is_rejected(self, monkeypatch):
        """対照 (1): STG env が無ければ、entry-token を持っていても scope は供給されない。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        with TestClient(app) as client:
            r = _post_bearer_only(client)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "scope_claim_missing"

    @pytest.mark.parametrize(
        "present",
        [
            {"STG_DEFAULT_TENANT_ID": "stg-tenant"},
            {"STG_DEFAULT_ORG_ID": "stg-org"},
            {"STG_DEFAULT_CAPABILITIES": "ledger:stage"},
            {"STG_DEFAULT_TENANT_ID": "stg-tenant", "STG_DEFAULT_ORG_ID": "stg-org"},
        ],
    )
    def test_partial_stg_env_is_not_rescued(self, monkeypatch, present):
        """対照 (2): 部分設定は救済しない。欠けたキーがあれば scope 無しのまま拒否される。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_PROVISIONAL_MODE", "true")
        for k, v in present.items():
            monkeypatch.setenv(k, v)
        with TestClient(app) as client:
            r = _post_bearer_only(client)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "scope_claim_missing"

    def test_values_are_inert_without_the_mode_declaration(self, monkeypatch):
        """二重ゲート (1): 値が揃っていても、モード宣言が無ければ inert。

        env が起動後に注入された等で startup guard を通っていないプロセスでも、
        リクエスト経路が単独で拒否することを示す（guard は唯一の防壁ではない）。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
        monkeypatch.setenv("STG_DEFAULT_ORG_ID", "stg-org")
        monkeypatch.setenv("STG_DEFAULT_CAPABILITIES", "ledger:stage")
        from src.api import server

        assert server._stg_default_claims() is None
        client = TestClient(app)          # lifespan を起動しない（with を使わない）
        r = _post_bearer_only(client)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "scope_claim_missing"

    def test_partial_middleware_claim_is_not_replaced_by_the_stg_scope(self, monkeypatch):
        """不整合な middleware 統合で、実テナントが STG テナントに差し替わらないこと。

        🔴 敵対レビュー Medium: 発火条件が「揃っていない」だと、本物の tenant だけ
        立って capabilities が落ちた統合で cross-tenant の黙った差し替えが起きる。
        発火条件は「claim が一つも無い」でなければならない。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        from src.api import server

        class _State:
            tenant = "real-customer-tenant"      # 本物の claim（部分的）
            org = None
            capabilities = None
            caller_id = ""

        class _Req:
            state = _State()
            headers: dict = {}

        claims = server._claims(_Req(), token_authenticated=True)
        assert claims["tenant"] == "real-customer-tenant"    # 差し替わっていない
        assert claims["tenant"] != "stg-tenant"
        assert claims["capabilities"] == []                  # 欠けたままで fail-closed に回る

    def test_full_stg_env_supplies_scope_and_invoke_succeeds(self, monkeypatch):
        """対照 (3): 宣言 + 全キーが揃ったときだけ scope が供給され 200/success になる。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        with TestClient(app) as client:
            r = _post_bearer_only(client)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success"

    def test_unauthenticated_caller_never_gets_the_stg_scope(self, monkeypatch):
        """entry-token を出さない呼び出しは、provisional でも既定 scope に到達しない。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        with TestClient(app) as client:
            r = client.post("/invoke", json=dict(_BODY))   # Authorization ヘッダ無し
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "scope_claim_missing"

    def test_capability_is_still_enforced_under_the_stg_scope(self, monkeypatch):
        """STG scope は capability を素通りさせない — mode に対応する capability が要る。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)                       # ledger:stage = invoke 用のみ
        with TestClient(app) as client:
            r = _post_bearer_only(client, {**_BODY, "mode": "ingest"})   # ledger:bootstrap が要る
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "capability_denied"


class TestStartupGuard:
    def test_app_startup_actually_runs_the_guard(self, monkeypatch):
        """🔴 guard が **app に配線されている**ことを検査する（レビュー Low）。

        `assert_scope_env_is_sane()` を直接呼ぶテストだけだと、`@app.on_event("startup")`
        の配線を消しても緑のままになる。lifespan を起動して落ちることを見る。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
        with pytest.raises(RuntimeError, match="STG_PROVISIONAL_MODE"):
            with TestClient(app):        # lifespan を起動 → startup hook が走る
                pass

    def test_startup_refuses_stg_env_without_the_mode_declaration(self, monkeypatch):
        """本番同居の認可バイパスを起動時に止める（宣言なし + STG_DEFAULT_* → 起動拒否）。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", "stg-tenant")
        with pytest.raises(RuntimeError, match="STG_PROVISIONAL_MODE"):
            assert_scope_env_is_sane()

    def test_startup_allows_the_declared_provisional_deployment(self, monkeypatch):
        _clear_stg_env(monkeypatch)
        _set_full_stg_env(monkeypatch)
        assert_scope_env_is_sane()        # raises しなければ合格

    def test_startup_allows_a_clean_production_process(self, monkeypatch):
        _clear_stg_env(monkeypatch)
        assert_scope_env_is_sane()
