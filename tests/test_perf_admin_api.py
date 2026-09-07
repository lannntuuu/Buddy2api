"""WS-2 管理 API 契约回归测试(见 redesign-audit/32-optimization-implementation-spec.md §0)。

覆盖:
- 列表不泄露明文/reveal 独占明文/PUT 返回行对象(api-keys)。
- /admin/accounts 免解密 summary(monkeypatch 解密函数直接抛错证明零调用)。
- POST/PUT /admin/accounts 返回行对象。
- 批量额度端点:3 账号恰 3 次上游调用,单账号失败不整体 500。

沿用本仓库约定:同步测试函数内用 asyncio.run 驱动协程;
鉴权用 monkeypatch deps.ALLOW_NO_ADMIN_AUTH = True(照 test_custom_channels.py)。
"""
import asyncio

import pytest

import gateway.deps as _deps
from storage import database as db


@pytest.fixture(autouse=True)
def _open_admin(monkeypatch):
    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)


# ---------- api-keys ----------

def test_api_key_list_has_no_secret(isolated_db):
    db.add_api_key("sk-cb-test-1", "k1", ["m1"], 0, "custom", "workbuddy")
    rows = asyncio.run(_admin_list_keys())
    assert rows and rows[0]["name"] == "k1"
    for field in ("key", "key_secret", "key_hash"):
        assert field not in rows[0]


async def _admin_list_keys():
    from gateway.routers.admin import admin_list_keys

    return await admin_list_keys(authorization=None)


def test_api_key_reveal_returns_plaintext_once(isolated_db):
    from gateway.routers.admin import admin_reveal_key

    kid = db.add_api_key("sk-cb-reveal-me", "k1", None, 0, "custom", "workbuddy")
    out = asyncio.run(admin_reveal_key(kid, authorization=None))
    assert out["ok"] is True
    assert out["key"] == "sk-cb-reveal-me"

    with pytest.raises(Exception) as excinfo:
        asyncio.run(admin_reveal_key(kid + 999, authorization=None))
    assert "not found" in str(excinfo.value.detail)


def test_api_key_put_returns_row_object(isolated_db):
    from gateway.routers.admin import admin_update_key

    kid = db.add_api_key("sk-cb-put-1", "k1", None, 0, "custom", "workbuddy")

    class FakeRequest:
        async def stream(self):
            yield b'{"status": "inactive", "daily_limit": 5}'

    out = asyncio.run(admin_update_key(kid, FakeRequest(), authorization=None))
    assert out["ok"] is True
    row = out["key"]
    assert row["id"] == kid
    assert row["status"] == "inactive"
    assert row["daily_limit"] == 5
    assert "key_secret" not in row and "key" not in row
    # 落库生效
    assert db.get_api_key_by_key("sk-cb-put-1") is None  # inactive 不再可鉴权


# ---------- accounts: 免解密 summary + 行对象 ----------

def _seed_account(**overrides):
    data = {
        "name": "acc-1", "provider": "workbuddy", "status": "active",
        "access_token": "tok-1", "refresh_token": "ref-1", "session_state": "st-1",
        "expires_at": 0,
    }
    data.update(overrides)
    return db.add_account(data)


def test_account_list_summary_never_decrypts(isolated_db, monkeypatch):
    from gateway.routers.admin import admin_list_accounts
    from storage import credential_crypto

    _seed_account()

    def boom(*_args, **_kwargs):
        raise AssertionError("summary 路径不得解密凭据")

    monkeypatch.setattr(credential_crypto, "decrypt_secret", boom)
    rows = asyncio.run(admin_list_accounts(authorization=None))
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "acc-1"
    assert row["provider"] == "workbuddy"
    # 行对象不携带凭据
    for field in ("access_token", "refresh_token", "session_state"):
        assert field not in row


def test_account_put_returns_row_object(isolated_db):
    from gateway.routers.admin import admin_update_account

    aid = _seed_account()

    class FakeRequest:
        async def stream(self):
            yield b'{"status": "inactive", "weight": 3}'

    out = asyncio.run(admin_update_account(aid, FakeRequest(), authorization=None))
    assert out["ok"] is True
    row = out["account"]
    assert row["id"] == aid
    assert row["status"] == "inactive"
    assert row["weight"] == 3
    assert "access_token" not in row


# ---------- 批量额度 ----------

def test_resources_batch_calls_upstream_once_per_account(isolated_db, monkeypatch):
    import accounts.control_plane as control_plane
    from gateway.routers.admin import admin_resources_batch

    ids = [_seed_account(name=f"a{i}") for i in range(3)]
    calls = []

    async def fake_resources(account, force=False, max_age_seconds=60):
        calls.append(account["id"])
        return {"ok": True, "account_id": account["id"], "total_dosage": 10.0}

    monkeypatch.setattr(control_plane.auth_manager, "fetch_account_resources", fake_resources)

    body = '{"account_ids": [%s]}' % ",".join(str(i) for i in ids)

    class FakeRequest:
        async def stream(self):
            yield body.encode("utf-8")

    out = asyncio.run(admin_resources_batch(FakeRequest(), authorization=None))
    assert out["ok"] is True
    assert sorted(calls) == sorted(ids), "每个账号恰一次上游调用"
    assert len(out["results"]) == 3
    assert all(r["ok"] for r in out["results"])


def test_resources_batch_single_failure_does_not_500(isolated_db, monkeypatch):
    import accounts.control_plane as control_plane
    from gateway.routers.admin import admin_resources_batch

    ids = [_seed_account(name=f"b{i}") for i in range(2)]

    async def flaky(account, force=False, max_age_seconds=60):
        if account["id"] == ids[0]:
            raise RuntimeError("upstream boom")
        return {"ok": True, "account_id": account["id"]}

    monkeypatch.setattr(control_plane.auth_manager, "fetch_account_resources", flaky)

    class FakeRequest:
        async def stream(self):
            yield b"{}"

    out = asyncio.run(admin_resources_batch(FakeRequest(), authorization=None))
    assert out["ok"] is True
    assert len(out["results"]) == 2
