"""Settings repository: key-value table for system configuration.

get_setting 是全仓库最密的读路径(每个请求的模型翻译/白名单/倍率/host 覆盖都会
触发多次),此前每次调用都新开 sqlite 连接。这里加一层进程内 TTL 缓存:
- 缓存 key 含 str(DB_PATH),测试切换隔离库不会串库;
- 写路径(set_setting/delete_setting)立即失效当前库的缓存;
- 命中时 deepcopy 返回,与"每次新读"的对象语义一致(调用方改返回值不影响缓存)。
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from storage.repos import _common
from storage.repos._common import _lock, get_conn

_TTL_SECONDS = 5.0
_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_all_cache: dict[str, tuple[float, dict]] = {}


def _path_key() -> str:
    return str(_common.DB_PATH)


def settings_cache_clear() -> None:
    """清空全部设置缓存(测试与配置变更场景用)。"""
    _cache.clear()
    _all_cache.clear()


def _invalidate_path(path_key: str) -> None:
    for key in [k for k in _cache if k[0] == path_key]:
        _cache.pop(key, None)
    _all_cache.pop(path_key, None)


def get_setting(key: str, default: Any = None) -> Any:
    path_key = _path_key()
    now = time.monotonic()
    hit = _cache.get((path_key, key))
    if hit is not None and now - hit[0] <= _TTL_SECONDS:
        return copy.deepcopy(hit[1])
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        value = default
    else:
        val = row["value"]
        try:
            value = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            value = val
    _cache[(path_key, key)] = (now, value)
    return copy.deepcopy(value)


def set_setting(key: str, value: Any):
    val = json.dumps(value) if not isinstance(value, str) else value
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, val),
        )
        conn.commit()
        conn.close()
    _invalidate_path(_path_key())


def delete_setting(key: str):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()
        conn.close()
    _invalidate_path(_path_key())


def get_all_settings() -> dict:
    path_key = _path_key()
    now = time.monotonic()
    hit = _all_cache.get(path_key)
    if hit is not None and now - hit[0] <= _TTL_SECONDS:
        return copy.deepcopy(hit[1])
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    _all_cache[path_key] = (now, result)
    return copy.deepcopy(result)
