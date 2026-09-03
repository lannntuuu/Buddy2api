"""平台 × 模型 × 日 用量统计接口测试（/admin/provider-model-usage）。"""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from storage import database as db
from gateway import server


@pytest.fixture()
def admin_env(monkeypatch):
    """有效管理凭证 + 放开限流；返回带鉴权的 endpoint 调用器。"""
    monkeypatch.setattr(server, "ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(server, "_USAGE_RATE_LIMIT", 10_000)
    server._usage_rate_bucket.clear()

    def call(**kwargs):
        kwargs.setdefault("authorization", "Bearer test-admin-token")
        return asyncio.run(server.admin_provider_model_usage(**kwargs))

    return call


def _add_log(provider: str, model: str, created_at: int, *, prompt: int = 100,
             completion: int = 50, credit: float = 0.1, duration_ms: int = 1000):
    db.record_request({
        "api_key_id": None,
        "api_key_name": None,
        "account_id": None,
        "account_name": None,
        "provider": provider,
        "model": model,
        "stream": 0,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "credit": credit,
        "finish_reason": "stop",
        "duration_ms": duration_ms,
        "status_code": 200,
        "error_msg": "",
        "created_at": created_at,
    })


def _ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, 0, 0).timestamp())


# ---------- database 层：聚合 ----------

def test_usage_groups_by_provider_model_day(isolated_db):
    today = date.today()
    yesterday = today - timedelta(days=1)
    _add_log("qclaw", "m1", _ts(today), prompt=10, completion=5, credit=0.5, duration_ms=2000)
    _add_log("qclaw", "m1", _ts(today), prompt=20, completion=10, credit=1.0, duration_ms=1000)
    _add_log("qclaw", "m2", _ts(yesterday), prompt=30, completion=15, duration_ms=3000)
    _add_log("qwenwork", "m1", _ts(yesterday), prompt=40, completion=20)

    result = db.get_provider_model_usage({})

    qclaw = result["providers"]["qclaw"]
    m1_today = [d for d in qclaw["models"]["m1"]["daily"] if d["date"] == today.isoformat()]
    assert len(m1_today) == 1
    assert m1_today[0]["requests"] == 2
    assert m1_today[0]["prompt_tokens"] == 30
    assert m1_today[0]["completion_tokens"] == 15
    assert m1_today[0]["total_tokens"] == 45
    assert m1_today[0]["credit"] == 1.5
    # 加权平均耗时 (2000+1000)/2
    assert m1_today[0]["avg_duration_ms"] == 1500

    # 日期降序
    m1_dates = [d["date"] for d in qclaw["models"]["m1"]["daily"]]
    assert m1_dates == sorted(m1_dates, reverse=True)

    # 模型小计 / 平台汇总 / 全局合计
    assert qclaw["models"]["m1"]["summary"]["requests"] == 2
    assert qclaw["summary"]["requests"] == 3
    assert qclaw["summary"]["total_tokens"] == 45 + 45
    assert result["totals"]["requests"] == 4
    assert result["totals"]["total_tokens"] == 45 + 45 + 60


def test_usage_filters_by_provider_model_and_time(isolated_db):
    today = date.today()
    old = today - timedelta(days=40)
    _add_log("qclaw", "m1", _ts(today))
    _add_log("qwenwork", "m1", _ts(today))
    _add_log("qclaw", "m2", _ts(today))
    _add_log("qclaw", "m1", _ts(old))

    only_qclaw = db.get_provider_model_usage({"provider": "qclaw"})
    assert set(only_qclaw["providers"].keys()) == {"qclaw"}

    only_model = db.get_provider_model_usage({"provider": "qclaw", "model": "m1"})
    assert set(only_model["providers"]["qclaw"]["models"].keys()) == {"m1"}

    start = int(datetime(today.year, today.month, today.day).timestamp())
    in_range = db.get_provider_model_usage({"start": start})
    total_rows = sum(
        len(bucket["daily"])
        for prov in in_range["providers"].values()
        for bucket in prov["models"].values()
    )
    assert total_rows == 3  # 40 天前的被排除


# ---------- server 层：参数校验 / 白名单 / 鉴权 / 限流 ----------

def test_usage_days_and_start_date_conflict(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(days=7, start_date="2025-01-01")
    assert err.value.status_code == 400


def test_usage_invalid_date_format(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(start_date="2025-13-45")
    assert err.value.status_code == 400
    with pytest.raises(HTTPException) as err:
        admin_env(start_date="not-a-date")
    assert err.value.status_code == 400


def test_usage_end_before_start(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(start_date="2025-01-10", end_date="2025-01-01")
    assert err.value.status_code == 400


def test_usage_non_positive_days(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(days=0)
    assert err.value.status_code == 400
    with pytest.raises(HTTPException) as err:
        admin_env(days=-3)
    assert err.value.status_code == 400


def test_usage_model_requires_provider(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(model="glm-5.2")
    assert err.value.status_code == 400


def test_usage_unknown_provider(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(provider="nope")
    assert err.value.status_code == 400


def test_usage_model_not_in_whitelist(isolated_db, admin_env):
    with pytest.raises(HTTPException) as err:
        admin_env(provider="qclaw", model="gpt-999")
    assert err.value.status_code == 400


def test_usage_model_in_whitelist_passes(isolated_db, admin_env):
    from providers.workbuddy import PROVIDER as WORKBUDDY

    model = WORKBUDDY.list_models()[0]["id"]
    data = admin_env(provider="workbuddy", model=model)
    assert "providers" in data
    assert "totals" in data


def test_usage_returns_aggregated_data(isolated_db, admin_env):
    today = date.today()
    _add_log("qclaw", "auto", _ts(today), prompt=10, completion=5)
    data = admin_env(provider="qclaw", days=7)
    entry = data["providers"]["qclaw"]["models"]["auto"]["daily"][0]
    assert entry["date"] == today.isoformat()
    assert entry["total_tokens"] == 15
    assert data["totals"]["requests"] == 1


def test_usage_days_one_is_today_only(isolated_db, admin_env):
    """days=1 必须只覆盖今天 00:00~23:59:59，排除更早的记录。

    record_request 会强制 created_at=now，无法回填历史；这里用裸 SQL 直接
    写入一条「前天」记录，验证时间区间过滤确实把旧数据排除在外。
    """
    import sqlite3

    today = date.today()
    two_days_ago = today - timedelta(days=2)
    # 今天的记录走正常写入路径
    _add_log("qclaw", "m1", _ts(today))
    # 前天的记录用裸 SQL 回填，绕过 record_request 的 now 覆盖
    with sqlite3.connect(str(isolated_db)) as c:
        c.execute(
            "INSERT INTO logs (provider, model, stream, prompt_tokens, completion_tokens, "
            "total_tokens, credit, finish_reason, duration_ms, status_code, error_msg, created_at) "
            "VALUES ('qclaw','m1',0,1,1,2,0.0,'stop',1000,200,'',?)",
            (int(datetime(two_days_ago.year, two_days_ago.month, two_days_ago.day, 12, 0, 0).timestamp()),),
        )

    data = admin_env(provider="qclaw", days=1)
    daily = data["providers"]["qclaw"]["models"]["m1"]["daily"]
    assert [d["date"] for d in daily] == [today.isoformat()]
    assert data["totals"]["requests"] == 1


def test_usage_days_window_bounds(isolated_db, admin_env):
    start_ts, end_ts = server._usage_date_bounds(7, None, None)
    today = date.today()
    midnight = int(datetime(today.year, today.month, today.day).timestamp())
    assert start_ts < end_ts
    assert end_ts == midnight + 86399
    assert 0 <= (midnight - start_ts) / 86400 - 6 < 1


def test_usage_only_end_date_defaults_start(isolated_db, admin_env):
    start_ts, end_ts = server._usage_date_bounds(None, None, "2025-06-30")
    assert start_ts is not None and end_ts is not None
    assert end_ts - start_ts == 89 * 86400 + 86399


def test_usage_only_start_date_defaults_end_to_today(isolated_db, admin_env):
    today = date.today()
    start_ts, end_ts = server._usage_date_bounds(None, "2025-01-01", None)
    expected_end = int(datetime(today.year, today.month, today.day).timestamp()) + 86399
    assert end_ts == expected_end
    assert start_ts < end_ts


def test_usage_no_auth_rejected(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(server, "_USAGE_RATE_LIMIT", 10_000)
    with pytest.raises(HTTPException) as err:
        asyncio.run(server.admin_provider_model_usage(authorization=None))
    assert err.value.status_code == 401


def test_usage_rate_limit_triggers_429(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_USAGE_RATE_LIMIT", 2)
    server._usage_rate_bucket.clear()

    async def hammer():
        await server._check_usage_rate_limit()
        await server._check_usage_rate_limit()
        with pytest.raises(HTTPException) as err:
            await server._check_usage_rate_limit()
        assert err.value.status_code == 429

    asyncio.run(hammer())
    server._usage_rate_bucket.clear()
