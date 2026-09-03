"""运行总览（/admin/stats + /admin/credit-summary）性能优化回归测试。

覆盖 P0-1（credit-summary 结果级缓存 + SWR + 签到失效）与 P0-2（共享 httpx 连接池）。
全部走 monkeypatch，不触碰真实上游；沿用 tests/conftest.py 的 isolated_db fixture。
采用本仓库约定：同步测试函数内用 asyncio.run 驱动协程（未装 pytest-asyncio）。
"""
import asyncio

import pytest

from storage import database as db  # noqa: F401  (isolated_db 依赖)
from storage import credit_cache
from storage import http_pool
import accounts.control_plane as control_plane
import accounts.auth_manager as auth_manager
import providers.traesolo.chat as tsc
import gateway.server as server


@pytest.fixture(autouse=True)
def _reset_credit_cache():
    credit_cache.invalidate()
    credit_cache.mark_refreshing(False)
    yield
    credit_cache.invalidate()
    credit_cache.mark_refreshing(False)


async def _fake_resources(account, force=False, max_age_seconds=60):  # noqa: ARG001
    return {
        "ok": True,
        "account_id": account.get("id"),
        "stale": False,
        "age_seconds": 0,
        "total_dosage": 100.0,
        "available_total": 100.0,
        "expiring_7d_total": 0.0,
        "expiring_30d_total": 0.0,
        "package_count": 1,
    }


def _stub_workbuddy_only(monkeypatch):
    monkeypatch.setattr(control_plane.providers, "enabled_provider_ids", lambda: ["workbuddy"])

    async def fake_channel_accounts(channel, status="active"):
        return [{"id": 1}]

    monkeypatch.setattr(control_plane, "_channel_accounts", fake_channel_accounts)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", _fake_resources)


# ---------- P0-1 缓存行为 ----------

def test_credit_summary_cache_hit(monkeypatch):
    calls = {"n": 0}

    async def counting(account, force=False, max_age_seconds=60):
        calls["n"] += 1
        return await _fake_resources(account, force=force)

    _stub_workbuddy_only(monkeypatch)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", counting)

    asyncio.run(control_plane.credit_summary())
    assert calls["n"] == 1
    asyncio.run(control_plane.credit_summary())  # TTL 内命中
    assert calls["n"] == 1
    out = credit_cache.get_snapshot(control_plane._CREDIT_SUMMARY_TTL)
    assert out["cache"] == "hit"


def test_credit_summary_force_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    async def counting(account, force=False, max_age_seconds=60):
        calls["n"] += 1
        return await _fake_resources(account, force=force)

    _stub_workbuddy_only(monkeypatch)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", counting)

    asyncio.run(control_plane.credit_summary(force=True))
    asyncio.run(control_plane.credit_summary(force=True))
    assert calls["n"] == 2  # 每次 force 都重建


def test_credit_summary_swr_serves_stale_then_refreshes(monkeypatch):
    calls = {"n": 0}

    async def counting(account, force=False, max_age_seconds=60):
        calls["n"] += 1
        return await _fake_resources(account, force=force)

    _stub_workbuddy_only(monkeypatch)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", counting)

    async def seq():
        await control_plane.credit_summary()
        assert calls["n"] == 1
        credit_cache._SNAPSHOT_TS = 0.0  # 把快照改成过期
        stale = await control_plane.credit_summary()
        assert stale["cache"] == "stale"
        assert calls["n"] == 1  # 过期先返旧值，未同步重建
        for _ in range(20):  # 让后台 SWR 重建任务运行
            await asyncio.sleep(0)
        fresh = await control_plane.credit_summary()
        assert fresh["cache"] == "hit"
        assert calls["n"] == 2  # 后台刷新已重建

    asyncio.run(seq())


def test_credit_summary_swr_does_not_overwrite_invalidate(monkeypatch):
    """SWR 重建期间发生 invalidate（如签到领取成功）：重建结果作废，不得回填旧数据。"""
    calls = {"n": 0}

    async def counting(account, force=False, max_age_seconds=60):
        calls["n"] += 1
        return await _fake_resources(account, force=force)

    _stub_workbuddy_only(monkeypatch)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", counting)

    async def seq():
        await control_plane.credit_summary()
        assert calls["n"] == 1
        credit_cache._SNAPSHOT_TS = 0.0  # 过期 → 调度后台重建
        stale = await control_plane.credit_summary()
        assert stale["cache"] == "stale"
        # 后台重建已调度但尚未运行；此刻签到成功触发 invalidate
        control_plane.invalidate_credit_summary_cache()
        for _ in range(20):
            await asyncio.sleep(0)
        # 重建数据基于领取前的状态，代数已变化 → 不回填
        assert credit_cache.has_snapshot() is False

    asyncio.run(seq())


def test_credit_summary_invalidate_then_rebuild(monkeypatch):
    calls = {"n": 0}

    async def counting(account, force=False, max_age_seconds=60):
        calls["n"] += 1
        return await _fake_resources(account, force=force)

    _stub_workbuddy_only(monkeypatch)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", counting)

    asyncio.run(control_plane.credit_summary())
    assert calls["n"] == 1
    control_plane.invalidate_credit_summary_cache()
    assert credit_cache.has_snapshot() is False
    asyncio.run(control_plane.credit_summary())
    assert calls["n"] == 2


def test_credit_summary_cache_disabled(monkeypatch):
    monkeypatch.setattr(control_plane, "_CREDIT_SUMMARY_TTL", 0.0)
    calls = {"n": 0}

    async def counting(account, force=False, max_age_seconds=60):
        calls["n"] += 1
        return await _fake_resources(account, force=force)

    _stub_workbuddy_only(monkeypatch)
    monkeypatch.setattr(auth_manager, "fetch_account_resources", counting)

    asyncio.run(control_plane.credit_summary())
    asyncio.run(control_plane.credit_summary())
    assert calls["n"] == 2  # TTL=0 关闭缓存，每次重建


def test_credit_summary_contract_keys(monkeypatch):
    # 结构契约：必须含 channels，且 total_balance 恒 null（KD-10）。
    # 全 stub（enabled_provider_ids/_channel_accounts），不触 DB、不发网络。
    _stub_workbuddy_only(monkeypatch)
    out = asyncio.run(control_plane._build_credit_summary(False))
    assert "channels" in out
    assert out.get("total_balance") is None
    assert any(item["id"] == "workbuddy" for item in out["channels"])


# ---------- P0-2 共享连接池 ----------

def test_shared_http_client_singleton():
    a = http_pool.get_client()
    b = http_pool.get_client()
    assert a is b
    assert not a.is_closed


def test_traesolo_quota_client_singleton_and_transport_swap(monkeypatch):
    import httpx

    first = tsc._get_quota_client()
    second = tsc._get_quota_client()
    assert first is second
    # 测试切换 MockTransport 时按 _TRANSPORT 身份重建
    monkeypatch.setattr(tsc, "_TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    rebuilt = tsc._get_quota_client()
    assert rebuilt is not first
    assert getattr(rebuilt, "_transport", None) is tsc._TRANSPORT


# ---------- /admin/stats 契约（P2-1 仍走 threadpool 时结构不变） ----------

def test_admin_stats_returns_compaction_and_today(monkeypatch):
    # 不依赖 isolated_db 临时库（沙箱对临时目录有扫描限制），直接 stub get_stats。
    monkeypatch.setattr(server, "ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(db, "get_stats", lambda: {"total_requests": 1, "today": {}})
    out = asyncio.run(server.admin_stats(authorization="Bearer test-admin-token"))
    assert "compaction" in out
    assert "today" in out
    assert out["total_requests"] == 1
