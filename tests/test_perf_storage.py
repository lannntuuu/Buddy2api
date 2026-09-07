"""WS-D 存储层性能修复回归测试(见 redesign-audit/30-perf-optimization-spec.md)。

覆盖:
- D1:settings TTL 缓存(命中不落库、写失效、按 DB_PATH 隔离、deepcopy 隔离)。
- D2:get_active_accounts TTL 缓存(命中、写失效、按 provider/DB_PATH 隔离)。
- D3:get_stats 条件聚合与旧行语义逐字段等价。
- D4:/health 单查询分组语义。

沿用本仓库约定:同步测试函数内用 asyncio.run 驱动协程(未装 pytest-asyncio)。
"""
import asyncio

from storage import database as db
from storage.repos import _common


# ---------- D1: settings 缓存 ----------

def test_settings_cache_hit_and_invalidation(isolated_db, monkeypatch):
    from storage.repos import settings as settings_repo

    calls = {"n": 0}
    real_get_conn = settings_repo.get_conn

    def counting():
        calls["n"] += 1
        return real_get_conn()

    monkeypatch.setattr(settings_repo, "get_conn", counting)

    assert db.get_setting("missing", "dft") == "dft"
    first = calls["n"]
    assert db.get_setting("missing", "dft") == "dft"
    assert calls["n"] == first, "TTL 内的重复读不应再落库"

    db.set_setting("k", {"a": 1})
    assert db.get_setting("k") == {"a": 1}
    second = calls["n"]
    assert calls["n"] == second
    assert db.get_setting("k") == {"a": 1}
    assert calls["n"] == second, "写路径后首次读已回填缓存,继续命中"

    db.delete_setting("k")
    assert db.get_setting("k", "gone") == "gone"


def test_settings_cached_value_is_isolated_from_caller(isolated_db):
    db.set_setting("k", [1, 2])
    got = db.get_setting("k")
    got.append(3)
    assert db.get_setting("k") == [1, 2]


def test_settings_cache_isolated_per_db_path(isolated_db, monkeypatch):
    db.set_setting("k", "v1")
    other = isolated_db.parent / "other-settings.db"
    monkeypatch.setattr(_common, "DB_PATH", other)
    db.init_db()  # 另一个库要先有 schema
    assert db.get_setting("k", "none") == "none", "不同库不得命中缓存"
    monkeypatch.setattr(_common, "DB_PATH", isolated_db)
    assert db.get_setting("k") == "v1"


# ---------- D2: get_active_accounts 缓存 ----------

def test_active_accounts_cache_hit_and_invalidation(isolated_db, monkeypatch):
    from storage.repos import accounts as accounts_repo

    db.add_account({"name": "a1", "provider": "workbuddy", "status": "active"})

    calls = {"n": 0}
    real_get_conn = accounts_repo.get_conn

    def counting():
        calls["n"] += 1
        return real_get_conn()

    monkeypatch.setattr(accounts_repo, "get_conn", counting)

    first = db.get_active_accounts("workbuddy")
    baseline = calls["n"]
    second = db.get_active_accounts("workbuddy")
    assert calls["n"] == baseline, "TTL 内的调度查询不应再落库"
    assert [a["name"] for a in second] == [a["name"] for a in first]

    db.update_account(first[0]["id"], {"status": "inactive"})
    assert calls["n"] == baseline + 1, "账号更新必须失效缓存"
    assert db.get_active_accounts("workbuddy") == []


def test_active_accounts_cache_keyed_by_provider(isolated_db):
    db.add_account({"name": "wb", "provider": "workbuddy", "status": "active"})
    db.add_account({"name": "qc", "provider": "qclaw", "status": "active"})
    wb = db.get_active_accounts("workbuddy")
    qc = db.get_active_accounts("qclaw")
    assert [a["name"] for a in wb] == ["wb"]
    assert [a["name"] for a in qc] == ["qc"]


def test_active_accounts_returned_list_is_isolated(isolated_db):
    db.add_account({"name": "a1", "provider": "workbuddy", "status": "active"})
    got = db.get_active_accounts("workbuddy")
    got.append({"name": "ghost"})
    got[0]["name"] = "mutated"
    again = db.get_active_accounts("workbuddy")
    assert [a["name"] for a in again] == ["a1"]


# ---------- D3: get_stats 条件聚合语义 ----------

def _seed_log(**overrides):
    row = {
        "api_key_id": None, "api_key_name": None,
        "account_id": None, "account_name": None,
        "provider": "workbuddy", "model": "m1", "stream": 0,
        "prompt_tokens": 10, "completion_tokens": 5,
        "total_tokens": 15, "credit": 0.1,
        "finish_reason": "stop", "duration_ms": 100,
        "status_code": 200, "error_msg": "",
    }
    row.update(overrides)
    db.record_request(row)


def test_get_stats_matches_legacy_row_semantics(isolated_db):
    _seed_log(duration_ms=100)                                            # success
    _seed_log(status_code=500, finish_reason="error", duration_ms=None)   # error
    _seed_log(finish_reason="content_filter")                             # filtered
    _seed_log(status_code=404)                                            # error

    stats = db.get_stats()
    assert stats["total_requests"] == 4
    assert stats["success_requests"] == 1
    assert stats["error_requests"] == 2
    assert stats["filtered_requests"] == 1
    # AVG 忽略 NULL,只对 duration_ms IS NOT NULL 的行取均值
    assert stats["avg_duration_ms"] == 100
    assert stats["success_rate"] == 25.0
    assert stats["total_tokens"] == 60
    assert stats["total_credit"] == 0.4

    today = stats["today"]
    assert today["requests"] == 4
    assert today["success"] == 1
    assert today["errors"] == 2
    assert today["filtered"] == 1


def test_get_stats_empty_logs(isolated_db):
    stats = db.get_stats()
    assert stats["total_requests"] == 0
    assert stats["success_requests"] == 0
    assert stats["error_requests"] == 0
    assert stats["filtered_requests"] == 0
    assert stats["avg_duration_ms"] == 0
    assert stats["success_rate"] == 0


# ---------- D4: /health 分组语义 ----------

def test_health_groups_accounts_by_provider(isolated_db):
    from gateway.routers.v1 import health

    db.add_account({"name": "wb-active", "provider": "workbuddy", "status": "active"})
    db.add_account({"name": "wb-idle", "provider": "workbuddy", "status": "inactive"})

    result = asyncio.run(health())
    assert result["accounts"] == 2
    assert result["active_accounts"] == 1
    bucket = result["channels"].get("workbuddy") or {}
    assert bucket.get("accounts") == 2
    assert bucket.get("active") == 1
