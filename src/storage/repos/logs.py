"""Logs repository: request logs table, search, retention."""
from __future__ import annotations

import math
import os
import sqlite3
import time
from datetime import date, timedelta
from typing import Any, Optional

from storage.repos._common import _lock, connection, get_conn


def migrate_provider(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "provider" not in cols:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN provider TEXT NOT NULL DEFAULT 'workbuddy'"
        )
    migrate_indexes(conn)


def migrate_indexes(conn: sqlite3.Connection) -> None:
    """高频过滤/排序列的索引。

    旧 migrate() 里建索引,但 init_db 从不调用它(拆分重构时漏掉),
    索引实际从未建过;这里接入 init_db 的迁移链路。
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_api_key ON logs(api_key_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_model ON logs(model)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_logs_status "
        "ON logs(status_code, finish_reason)"
    )


def migrate_cache_tokens(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "cache_read_tokens" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN cache_read_tokens INTEGER DEFAULT 0")
    if "cache_creation_tokens" not in cols:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0"
        )
    if "usage_json" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN usage_json TEXT")
    if "credit_source" not in cols:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN credit_source TEXT DEFAULT 'live'"
        )


def migrate_reasoning(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "reasoning_effort" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN reasoning_effort TEXT")


def migrate_client(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "client" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN client TEXT")
    if "client_version" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN client_version TEXT")


def migrate_first_token(conn: sqlite3.Connection):
    """first_token_ms:流式请求从请求起点到首个内容帧的毫秒数(非流式为 NULL)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "first_token_ms" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN first_token_ms INTEGER")


# ============================================================
# Log writes
# ============================================================

def record_request(data: dict):
    """Write a request log and update account/key counters in one transaction."""
    now = int(time.time())
    with _lock:
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO logs
                    (api_key_id, api_key_name, account_id, account_name, model, stream,
                     prompt_tokens, completion_tokens, total_tokens, credit,
                     cache_read_tokens, cache_creation_tokens,
                     usage_json, credit_source,
                     finish_reason, duration_ms, status_code, error_msg, provider, client, client_version,
                     reasoning_effort, created_at, first_token_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data.get("api_key_id"),
                    data.get("api_key_name"),
                    data.get("account_id"),
                    data.get("account_name"),
                    data.get("model", ""),
                    data.get("stream", 0),
                    data.get("prompt_tokens", 0),
                    data.get("completion_tokens", 0),
                    data.get("total_tokens", 0),
                    data.get("credit", 0),
                    data.get("cache_read_tokens", 0),
                    data.get("cache_creation_tokens", 0),
                    data.get("usage_json"),
                    data.get("credit_source"),
                    data.get("finish_reason", ""),
                    data.get("duration_ms", 0),
                    data.get("status_code", 200),
                    data.get("error_msg", ""),
                    data.get("provider") or "workbuddy",
                    data.get("client"),
                    data.get("client_version"),
                    data.get("reasoning_effort"),
                    # 入队方携带的请求起点优先(created_at=int(t0)),缺省落库时刻
                    data.get("created_at") or now,
                    data.get("first_token_ms"),
                ),
            )
            if data.get("account_id") and data.get("increment_usage", True):
                conn.execute(
                    """
                    UPDATE accounts SET
                        total_requests=total_requests + 1,
                        total_tokens=total_tokens + ?,
                        total_credits=total_credits + ?,
                        last_used_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        data.get("total_tokens", 0),
                        data.get("credit", 0),
                        now,
                        now,
                        data["account_id"],
                    ),
                )
            if data.get("api_key_id") and data.get("increment_usage", True):
                conn.execute(
                    """
                    UPDATE api_keys SET
                        total_requests=total_requests + 1,
                        total_tokens=total_tokens + ?,
                        last_used_at=?
                    WHERE id=?
                    """,
                    (data.get("total_tokens", 0), now, data["api_key_id"]),
                )
            conn.commit()


def prune_logs(retention_days: int | None = None) -> int:
    """Delete expired request logs and return the number of removed rows."""
    if retention_days is None:
        try:
            retention_days = int(os.environ.get("CB_GATEWAY_LOG_RETENTION_DAYS", "90"))
        except ValueError:
            retention_days = 90
    retention_days = max(1, retention_days)
    cutoff_ts = int(time.time()) - retention_days * 86400
    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    with _lock:
        with connection() as conn:
            cursor = conn.execute(
                "DELETE FROM logs WHERE created_at < ?", (cutoff_ts,)
            )
            conn.execute(
                "DELETE FROM api_key_daily_usage WHERE usage_date < ?",
                (cutoff_date,),
            )
            conn.commit()
            return max(0, cursor.rowcount)


# ============================================================
# Log reads
# ============================================================

def count_errors_by_provider(seconds: int = 3600) -> dict[str, int]:
    """近 N 秒各通道错误请求数(5xx 或 error 终态);channel-health 观测面用。"""
    cutoff = int(time.time()) - max(1, int(seconds))
    conn = get_conn()
    rows = conn.execute(
        "SELECT provider, COUNT(*) AS c FROM logs "
        "WHERE created_at >= ? AND (status_code >= 500 OR finish_reason = 'error') "
        "GROUP BY provider",
        (cutoff,),
    ).fetchall()
    conn.close()
    return {(r["provider"] or "workbuddy"): int(r["c"]) for r in rows}

def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_logs(filters: Optional[dict] = None) -> dict:
    filters = filters or {}
    limit = max(1, min(500, int(filters.get("limit") or 100)))
    offset = max(0, int(filters.get("offset") or 0))
    where = []
    values: list[Any] = []

    q = str(filters.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append(
            "(api_key_name LIKE ? OR account_name LIKE ? OR model LIKE ? "
            "OR finish_reason LIKE ? OR error_msg LIKE ? OR client LIKE ? "
            "OR client_version LIKE ?)"
        )
        values.extend([like, like, like, like, like, like, like])

    status = str(filters.get("status") or "all").strip()
    if status == "success":
        where.append(
            "status_code BETWEEN 200 AND 299 "
            "AND finish_reason NOT IN ('error', 'content_filter')"
        )
    elif status == "error":
        where.append(
            "(status_code < 200 OR status_code >= 300 OR finish_reason='error')"
        )
    elif status == "filtered":
        where.append("finish_reason='content_filter'")

    for key, col in (("api_key_id", "api_key_id"), ("account_id", "account_id")):
        value = filters.get(key)
        if value not in (None, "", "all"):
            where.append(f"{col}=?")
            values.append(int(value))

    model = str(filters.get("model") or "").strip()
    if model:
        where.append("model=?")
        values.append(model)

    start = filters.get("start")
    has_start = start not in (None, "", "all")
    end = filters.get("end")
    has_end = end not in (None, "", "all")

    # 防大表全扫:请求完全没给时间窗口时强制默认只看近 7 天。
    window_applied = False
    if not has_start and not has_end:
        start = int(time.time()) - 7 * 86400
        has_start = True
        window_applied = True

    if has_start:
        where.append("created_at>=?")
        values.append(int(start))

    if has_end:
        where.append("created_at<=?")
        values.append(int(end))

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM logs{sql_where}", values
    ).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM logs{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, limit, offset],
    ).fetchall()
    model_rows = conn.execute(
        "SELECT DISTINCT model FROM logs "
        "WHERE model IS NOT NULL AND model!='' ORDER BY model LIMIT 200"
    ).fetchall()
    conn.close()
    result = {
        "items": [dict(r) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "models": [r["model"] for r in model_rows],
    }
    if window_applied:
        result["window_applied"] = "7d"
    return result


# ============================================================
# first_token_ms 聚合
# ============================================================

def p95_of(values: list) -> int:
    """纯函数:最近邻秩法 P95。空样本返回 0。

    rank = ceil(0.95 × n),取排序后第 rank 个(1-based);n=1 → 自身,
    n=100 → 第 95 个。测试以此做纯函数断言。
    """
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return int(ordered[min(len(ordered), rank) - 1])


def stream_p95_by_provider(since_days: int = 7, max_samples: int = 5000) -> dict:
    """近 N 天流式请求 first_token_ms 的分通道 P95(毫秒)。

    样本 = stream=1 且 first_token_ms 非空的行,按 id 倒序最多取
    max_samples 条(默认 5000,SQL LIMIT 防大表),Python 端分通道排序取 P95。
    无样本返回 {}。
    """
    cutoff = int(time.time()) - max(1, int(since_days)) * 86400
    conn = get_conn()
    rows = conn.execute(
        "SELECT provider, first_token_ms FROM logs "
        "WHERE stream=1 AND first_token_ms IS NOT NULL AND created_at>=? "
        "ORDER BY id DESC LIMIT ?",
        (cutoff, max(1, int(max_samples))),
    ).fetchall()
    conn.close()
    samples: dict[str, list[int]] = {}
    for row in rows:
        value = row["first_token_ms"]
        if value is None:
            continue
        samples.setdefault(row["provider"] or "workbuddy", []).append(int(value))
    return {provider: p95_of(bucket) for provider, bucket in samples.items()}
