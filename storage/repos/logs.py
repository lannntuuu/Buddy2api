"""Logs repository: request logs table, search, retention."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, timedelta
from typing import Any, Optional

from storage.repos._common import _lock, connection, get_conn


def migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "provider" not in cols:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN provider TEXT NOT NULL DEFAULT 'workbuddy'"
        )
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
    if "client" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN client TEXT")
    if "client_version" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN client_version TEXT")
    if "reasoning_effort" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN reasoning_effort TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_api_key ON logs(api_key_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_model ON logs(model)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_logs_status "
        "ON logs(status_code, finish_reason)"
    )


def migrate_provider(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "provider" not in cols:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN provider TEXT NOT NULL DEFAULT 'workbuddy'"
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


# ============================================================
# Log writes
# ============================================================

def add_log(data: dict):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO logs
                (api_key_id, api_key_name, account_id, account_name, model, stream,
                 prompt_tokens, completion_tokens, total_tokens, credit,
                 finish_reason, duration_ms, status_code, error_msg, provider, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                data.get("finish_reason", ""),
                data.get("duration_ms", 0),
                data.get("status_code", 200),
                data.get("error_msg", ""),
                data.get("provider") or "workbuddy",
                now,
            ),
        )
        conn.commit()
        conn.close()


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
                     reasoning_effort, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    now,
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
    if start not in (None, "", "all"):
        where.append("created_at>=?")
        values.append(int(start))

    end = filters.get("end")
    if end not in (None, "", "all"):
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
    return {
        "items": [dict(r) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "models": [r["model"] for r in model_rows],
    }
