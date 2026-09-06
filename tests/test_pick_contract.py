"""WS-D pick 策略收敛的差分契约测试(35号方案 §2.4)。

四家 facade 的 pick_account_with_fallback 收敛到 trae_shared 共享实现,
本文件钉住各家的语义差异面:
- workbuddy:bool 型 refresh_token 包装为异常域;刷新成功设 sticky;失败进负缓存。
- qwenwork/traework:选中过期账号先原地刷新(proactive);异常域为宽 Exception。
- qclaw:仅 JprxError 算刷新失败(其余异常上抛,不入负缓存)。
"""
import asyncio
import time

import pytest

from accounts import auth_manager
from providers import trae_shared
from storage import database as db


@pytest.fixture(autouse=True)
def _clean_negative_cache():
    trae_shared.reset_refresh_failures()
    auth_manager._sticky_account_id.clear()
    yield
    trae_shared.reset_refresh_failures()
    auth_manager._sticky_account_id.clear()


def _seed_expired(provider, name="acc-1"):
    """status=active 且 token 已过期:pick 会选中,然后走刷新路径。"""
    return db.add_account({
        "name": name, "provider": provider, "status": "active",
        "access_token": "tok", "refresh_token": "ref",
        "expires_at": int(time.time() * 1000) - 10_000,
    })


def _seed_active(provider, name="acc-ok"):
    return db.add_account({
        "name": name, "provider": provider, "status": "active",
        "access_token": "tok", "refresh_token": "ref",
        "expires_at": int(time.time() * 1000) + 3_600_000,
    })


# ---------- workbuddy 正典 ----------

def test_workbuddy_fallback_refresh_success_sets_sticky(isolated_db, monkeypatch):
    aid = _seed_expired("workbuddy")
    calls = []

    async def fake_refresh(account):
        calls.append(account["id"])
        db.update_account(aid, {"status": "active"})
        return True

    monkeypatch.setattr(auth_manager, "refresh_token", fake_refresh)

    fresh = asyncio.run(auth_manager.pick_account_with_fallback(provider="workbuddy"))
    assert fresh is not None and fresh["id"] == aid
    assert calls == [aid]
    # 刷新成功后 sticky 指向该账号(正典语义)
    assert auth_manager._sticky_account_id.get("workbuddy") == aid


def test_workbuddy_fallback_failure_enters_negative_cache(isolated_db, monkeypatch):
    _seed_expired("workbuddy")
    calls = []

    async def failing_refresh(account):
        calls.append(account["id"])
        return False

    monkeypatch.setattr(auth_manager, "refresh_token", failing_refresh)

    first = asyncio.run(auth_manager.pick_account_with_fallback(provider="workbuddy"))
    assert first is None
    second = asyncio.run(auth_manager.pick_account_with_fallback(provider="workbuddy"))
    assert second is None
    assert calls == [aid for aid in calls], "负缓存窗口内不得重放刷新"
    assert len(calls) == 1, "两次 fallback 只应真正刷新一次"


# ---------- qwenwork / traework facade ----------

@pytest.mark.parametrize("module_name,provider", [
    ("providers.qwenwork.__init__", "qwenwork"),
    ("providers.traework.__init__", "traework"),
])
def test_facade_proactive_refresh_success(isolated_db, monkeypatch, module_name, provider):
    import importlib
    mod = importlib.import_module(module_name)
    aid = _seed_expired(provider)

    async def fake_refresh(account):
        db.update_account(aid, {"status": "active"})
        return db.get_account(aid)

    monkeypatch.setattr(mod, "refresh_account", fake_refresh)

    fresh = asyncio.run(mod.PROVIDER.pick_account_with_fallback())
    assert fresh is not None and fresh["id"] == aid


@pytest.mark.parametrize("module_name,provider", [
    ("providers.qwenwork.__init__", "qwenwork"),
    ("providers.traework.__init__", "traework"),
])
def test_facade_refresh_failure_negative_cache(isolated_db, monkeypatch, module_name, provider):
    import importlib
    mod = importlib.import_module(module_name)
    _seed_expired(provider)
    calls = []

    async def failing(account):
        calls.append(account["id"])
        raise RuntimeError("upstream down")

    monkeypatch.setattr(mod, "refresh_account", failing)

    assert asyncio.run(mod.PROVIDER.pick_account_with_fallback()) is None
    assert asyncio.run(mod.PROVIDER.pick_account_with_fallback()) is None
    assert len(calls) == 1, "宽异常域也必须进负缓存,窗口内不重放"


# ---------- qclaw 异常域 ----------

def test_qclaw_non_jprx_error_propagates(isolated_db, monkeypatch):
    from providers.qclaw import jprx

    mod = importlib.import_module("providers.qclaw.__init__") if False else None
    import providers.qclaw as qclaw_pkg
    _seed_expired("qclaw")

    async def boom(row):
        raise ValueError("not a jprx failure")

    monkeypatch.setattr(jprx, "refresh_channel", boom)
    with pytest.raises(ValueError):
        asyncio.run(qclaw_pkg.PROVIDER.pick_account_with_fallback())


def test_qclaw_jprx_error_enters_negative_cache(isolated_db, monkeypatch):
    from providers.qclaw import jprx
    import providers.qclaw as qclaw_pkg

    _seed_expired("qclaw")
    calls = []

    async def jprx_fail(row):
        calls.append(row["id"])
        raise jprx.JprxError("refresh rejected")

    monkeypatch.setattr(jprx, "refresh_channel", jprx_fail)

    assert asyncio.run(qclaw_pkg.PROVIDER.pick_account_with_fallback()) is None
    assert asyncio.run(qclaw_pkg.PROVIDER.pick_account_with_fallback()) is None
    assert len(calls) == 1, "JprxError 入负缓存,窗口内不重放"
