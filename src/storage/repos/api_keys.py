"""API keys repository: api_keys table + daily usage slot reservations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date
from typing import Optional

from storage import credential_crypto
from storage.repos._common import (
    DB_PATH,
    _lock,
    connection,
    get_conn,
    load_allowed_models,
)


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_prefix(key: str) -> str:
    if len(key) <= 16:
        return key[:6] + "..."
    return f"{key[:12]}...{key[-4:]}"


def migrate(conn: sqlite3.Connection) -> None:
    """Keep older plaintext-key databases usable while moving to hash-only storage."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "key" in cols:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
        conn.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy")
        conn.execute(
            """
            CREATE TABLE api_keys (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                key_prefix      TEXT,
                key_hash        TEXT UNIQUE,
                key_secret      TEXT,
                name            TEXT,
                status          TEXT DEFAULT 'active',
                allowed_models  TEXT,
                daily_limit     INTEGER DEFAULT 0,
                client_type     TEXT DEFAULT 'custom',
                default_channel TEXT DEFAULT 'workbuddy',
                total_requests  INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                created_at      INTEGER,
                last_used_at    INTEGER
            )
            """
        )
        for row in rows:
            d = dict(row)
            raw_key = d.get("key") or ""
            conn.execute(
                """
                INSERT INTO api_keys
                    (id, key_prefix, key_hash, key_secret, name, status, allowed_models,
                     daily_limit, client_type, total_requests, total_tokens, created_at,
                     last_used_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    d.get("id"),
                    d.get("key_prefix") or _key_prefix(raw_key),
                    d.get("key_hash") or (_hash_api_key(raw_key) if raw_key else None),
                    d.get("key_secret")
                    or (
                        credential_crypto.encrypt_secret(raw_key, DB_PATH)
                        if raw_key
                        else None
                    ),
                    d.get("name"),
                    d.get("status", "active"),
                    d.get("allowed_models"),
                    d.get("daily_limit", 0),
                    d.get("client_type", "custom"),
                    d.get("total_requests", 0),
                    d.get("total_tokens", 0),
                    d.get("created_at"),
                    d.get("last_used_at"),
                ),
            )
    # 兼容更早的 api_keys 缺 default_channel 列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "default_channel" not in cols:
        conn.execute(
            "ALTER TABLE api_keys ADD COLUMN default_channel TEXT DEFAULT 'workbuddy'"
        )
        conn.execute(
            "UPDATE api_keys SET default_channel='workbuddy' "
            "WHERE default_channel IS NULL OR default_channel=''"
        )


def migrate_daily_usage(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_key_daily_usage (
            api_key_id    INTEGER NOT NULL,
            usage_date    TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(api_key_id, usage_date),
            FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")


# ============================================================
# API key CRUD
# ============================================================

def add_api_key(
    key: str,
    name: str,
    allowed_models: Optional[list] = None,
    daily_limit: Optional[int] = None,
    client_type: str = "custom",
    default_channel: str = "workbuddy",
) -> int:
    now = int(time.time())
    models_json = json.dumps(allowed_models) if allowed_models else None
    limit = int(daily_limit or 0)
    channel = str(default_channel or "workbuddy").strip() or "workbuddy"
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """
            INSERT INTO api_keys
                (key_prefix, key_hash, key_secret, name, status, allowed_models,
                 daily_limit, client_type, default_channel, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _key_prefix(key),
                _hash_api_key(key),
                credential_crypto.encrypt_secret(key, DB_PATH),
                name,
                "active",
                models_json,
                limit,
                client_type,
                channel,
                now,
            ),
        )
        kid = cur.lastrowid
        conn.commit()
        conn.close()
        return kid


def update_api_key(kid: int, data: dict):
    fields = []
    values = []
    for k in [
        "name", "status", "allowed_models", "daily_limit",
        "client_type", "default_channel",
    ]:
        if k in data:
            val = data[k]
            if k == "allowed_models" and isinstance(val, list):
                val = json.dumps(val) if val else None
            fields.append(f"{k}=?")
            values.append(val)
    if not fields:
        return
    values.append(kid)
    with _lock:
        conn = get_conn()
        conn.execute(
            f"UPDATE api_keys SET {','.join(fields)} WHERE id=?", values
        )
        conn.commit()
        conn.close()


def delete_api_key(kid: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM api_keys WHERE id=?", (kid,))
        conn.commit()
        conn.close()


def has_api_keys() -> bool:
    """Lightweight existence check; avoids full-table scan on hot auth path."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone()
        return row is not None
    finally:
        conn.close()


def get_api_key_by_key(key: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash=? AND status='active'",
        (_hash_api_key(key),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d.pop("key_hash", None)
    d.pop("key_secret", None)
    d.pop("key", None)
    d["allowed_models"] = load_allowed_models(d.get("allowed_models"))
    return d


def list_api_keys(*, include_secret: bool = False) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT k.*, COALESCE(u.request_count, 0) AS today_requests
        FROM api_keys AS k
        LEFT JOIN api_key_daily_usage AS u
          ON u.api_key_id=k.id AND u.usage_date=?
        ORDER BY k.id DESC
        """,
        (date.today().isoformat(),),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d.pop("key_hash", None)
        encrypted_key = d.pop("key_secret", None)
        d.pop("key", None)
        if include_secret:
            if not encrypted_key:
                d["key"] = None
            else:
                try:
                    d["key"] = credential_crypto.decrypt_secret(encrypted_key, DB_PATH)
                except credential_crypto.CredentialCryptoError as exc:
                    # 单条 key 解密失败（如 master key 换过）只标记该条，
                    # 不让 /admin/api-keys 整个 500。
                    d["key"] = None
                    d["key_unrecoverable"] = str(exc)[:200]
        d["allowed_models"] = load_allowed_models(d.get("allowed_models"))
        result.append(d)
    return result


def get_api_key_daily_requests(kid: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT request_count AS c FROM api_key_daily_usage "
        "WHERE api_key_id=? AND usage_date=?",
        (kid, date.today().isoformat()),
    ).fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def reserve_api_key_request(kid: int, daily_limit: int) -> bool:
    """Atomically reserve one daily request slot for an API key."""
    today = date.today().isoformat()
    with _lock:
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_count FROM api_key_daily_usage "
                "WHERE api_key_id=? AND usage_date=?",
                (kid, today),
            ).fetchone()
            current = int(row["request_count"] if row else 0)
            if daily_limit > 0 and current >= daily_limit:
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO api_key_daily_usage (api_key_id, usage_date, request_count)
                VALUES (?, ?, 1)
                ON CONFLICT(api_key_id, usage_date) DO UPDATE SET
                    request_count=request_count + 1
                """,
                (kid, today),
            )
            conn.commit()
            return True


def release_api_key_request(kid: int) -> None:
    """Roll back one previously reserved daily request slot."""
    today = date.today().isoformat()
    with _lock:
        with connection() as conn:
            conn.execute(
                """
                UPDATE api_key_daily_usage SET request_count = request_count - 1
                WHERE api_key_id=? AND usage_date=? AND request_count > 0
                """,
                (kid, today),
            )
            conn.commit()


def api_key_increment_usage(kid: int, tokens: int):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            UPDATE api_keys SET
                total_requests = total_requests + 1,
                total_tokens = total_tokens + ?,
                last_used_at = ?
            WHERE id=?
            """,
            (tokens, now, kid),
        )
        conn.commit()
        conn.close()
