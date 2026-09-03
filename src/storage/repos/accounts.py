"""Accounts repository: accounts table, resource_cache, checkin_cache, traework daily credit."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from typing import Optional

from storage import credential_crypto
from storage.repos._common import (
    CREDENTIAL_FIELDS,
    DB_PATH,
    _lock,
    get_conn,
)


def _protect_account_data(data: dict) -> dict:
    protected = dict(data)
    for field in CREDENTIAL_FIELDS:
        if field in protected:
            protected[field] = credential_crypto.encrypt_secret(
                protected.get(field), DB_PATH
            )
    return protected


def _account_dict(row: sqlite3.Row) -> dict:
    account = dict(row)
    decrypt_errors = []
    for field in CREDENTIAL_FIELDS:
        if field in account:
            try:
                account[field] = credential_crypto.decrypt_secret(
                    account.get(field), DB_PATH
                )
            except credential_crypto.CredentialCryptoError as exc:
                # 单条记录解密失败（如 Windows DPAPI 行拿到 Linux、master key
                # 换过）只降级该账号，不能让整个 list_accounts 崩掉。
                decrypt_errors.append(f"{field}: {exc}")
                account[field] = ""
    if decrypt_errors:
        account["credential_error"] = "; ".join(decrypt_errors)[:400]
    extra = account.get("extra")
    if isinstance(extra, str) and extra:
        try:
            account["extra"] = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            account["extra"] = {}
    elif extra is None:
        account["extra"] = {}
    if not account.get("provider"):
        account["provider"] = "workbuddy"
    return account


def migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "provider" not in cols:
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN provider TEXT NOT NULL DEFAULT 'workbuddy'"
        )
    if "extra" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN extra TEXT")
    conn.execute(
        "UPDATE accounts SET provider='workbuddy' WHERE provider IS NULL OR provider=''"
    )
    _dedupe_accounts_provider_uid(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider_uid
            ON accounts(provider, uid)
            WHERE uid IS NOT NULL AND uid != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_accounts_provider_status
            ON accounts(provider, status, priority, id)
        """
    )
    if "weight" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN weight INTEGER DEFAULT 1")
    if "priority" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER DEFAULT 0")
    if "credit_limit" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN credit_limit REAL DEFAULT 0")
    if "credit_baseline" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN credit_baseline REAL DEFAULT 0")
        conn.execute(
            """
            UPDATE accounts
            SET credit_baseline=COALESCE(total_credits, 0)
            WHERE credit_limit IS NOT NULL AND credit_limit > 0
            """
        )
    conn.execute("UPDATE accounts SET weight=1 WHERE weight IS NULL OR weight < 1")
    conn.execute("UPDATE accounts SET priority=0 WHERE priority IS NULL")
    conn.execute(
        "UPDATE accounts SET credit_limit=0 WHERE credit_limit IS NULL OR credit_limit < 0"
    )
    conn.execute(
        "UPDATE accounts SET credit_baseline=0 WHERE credit_baseline IS NULL OR credit_baseline < 0"
    )


def _dedupe_accounts_provider_uid(conn: sqlite3.Connection):
    """Keep the lowest-id row per (provider, uid); nullify uid on the rest."""
    rows = conn.execute(
        """
        SELECT id, provider, uid FROM accounts
        WHERE uid IS NOT NULL AND uid != ''
        ORDER BY id
        """
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    dup_ids: list[int] = []
    for r in rows:
        key = (r["provider"], r["uid"])
        if key in seen:
            dup_ids.append(r["id"])
        else:
            seen.add(key)
    if dup_ids:
        placeholders = ",".join("?" for _ in dup_ids)
        conn.execute(
            f"UPDATE accounts SET uid=NULL WHERE id IN ({placeholders})", dup_ids
        )


def migrate_credentials(conn: sqlite3.Connection):
    """Re-encrypt plaintext credentials that predate the on-disk encryption layer.

    Older databases may carry plaintext or DPAPI ciphertext that other
    platforms (Linux, Docker) cannot decrypt. This migration rewrites
    every plaintext credential field in place.
    """
    rows = conn.execute(
        "SELECT id, access_token, refresh_token, session_state FROM accounts"
    ).fetchall()
    for row in rows:
        updates = {}
        for field in CREDENTIAL_FIELDS:
            value = row[field]
            if value and not credential_crypto.is_encrypted(value):
                updates[field] = credential_crypto.encrypt_secret(value, DB_PATH)
        if updates:
            fields = ", ".join(f"{field}=?" for field in updates)
            conn.execute(
                f"UPDATE accounts SET {fields} WHERE id=?",
                [*updates.values(), row["id"]],
            )


# ============================================================
# Account CRUD
# ============================================================

def add_account(data: dict) -> int:
    data = _protect_account_data(data)
    now = int(time.time())
    weight = max(1, int(data.get("weight", 1) or 1))
    priority = int(data.get("priority", 0) or 0)
    credit_limit = max(0.0, float(data.get("credit_limit", 0) or 0))
    credit_baseline = max(0.0, float(data.get("credit_baseline", 0) or 0))
    provider = str(data.get("provider") or "workbuddy").strip() or "workbuddy"
    extra = data.get("extra")
    if isinstance(extra, dict):
        extra_text = json.dumps(extra, ensure_ascii=False)
    elif extra is None:
        extra_text = None
    else:
        extra_text = str(extra)
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """
            INSERT INTO accounts
                (name, uid, nickname, phone, account_type, access_token, refresh_token,
                 expires_at, refresh_expires_at, domain, enterprise_id, session_state,
                 status, weight, priority, credit_limit, credit_baseline, provider, extra,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("name", ""),
                data.get("uid", ""),
                data.get("nickname", ""),
                data.get("phone", ""),
                data.get("account_type", "personal"),
                data.get("access_token", ""),
                data.get("refresh_token", ""),
                data.get("expires_at", 0),
                data.get("refresh_expires_at", 0),
                data.get("domain", "www.codebuddy.cn"),
                data.get("enterprise_id", ""),
                data.get("session_state", ""),
                data.get("status", "active"),
                weight,
                priority,
                credit_limit,
                credit_baseline,
                provider,
                extra_text,
                now,
                now,
            ),
        )
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        return aid


def update_account(aid: int, data: dict):
    data = _protect_account_data(data)
    now = int(time.time())
    fields = []
    values = []
    for k in [
        "name", "uid", "nickname", "phone", "account_type", "access_token",
        "refresh_token", "expires_at", "refresh_expires_at", "domain",
        "enterprise_id", "session_state", "status", "weight", "priority",
        "credit_limit", "credit_baseline", "provider", "extra",
    ]:
        if k in data:
            if k == "weight":
                data[k] = max(1, int(data[k] or 1))
            elif k == "priority":
                data[k] = int(data[k] or 0)
            elif k in {"credit_limit", "credit_baseline"}:
                data[k] = max(0.0, float(data[k] or 0))
            elif k == "provider":
                data[k] = str(data[k] or "workbuddy").strip() or "workbuddy"
            elif k == "extra" and isinstance(data[k], dict):
                data[k] = json.dumps(data[k], ensure_ascii=False)
            fields.append(f"{k}=?")
            values.append(data[k])
    if not fields:
        return
    fields.append("updated_at=?")
    values.append(now)
    values.append(aid)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE accounts SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
        conn.close()


def delete_account(aid: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM account_resource_cache WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM account_checkin_cache WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
        conn.commit()
        conn.close()


def get_account(aid: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    conn.close()
    return _account_dict(row) if row else None


def list_accounts(*, provider: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if provider:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE provider=? ORDER BY id",
            (provider,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [_account_dict(r) for r in rows]


def get_active_accounts(provider: str = "workbuddy") -> list[dict]:
    if not provider:
        raise ValueError("get_active_accounts requires provider")
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE status='active' AND provider=?
        ORDER BY priority DESC,
                 (CAST(total_requests AS REAL) / CASE WHEN weight > 0 THEN weight ELSE 1 END) ASC,
                 total_requests ASC,
                 id ASC
        """,
        (provider,),
    ).fetchall()
    conn.close()
    accounts = [_account_dict(r) for r in rows]
    # 凭据解密失败的账号（credential_error）不能参与调度：拿空 token
    # 打上游只会白吃 401，还可能阻塞通道的可用性判定。
    return [a for a in accounts if not a.get("credential_error")]


def account_increment_usage(aid: int, tokens: int, credit: float):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            UPDATE accounts SET
                total_requests = total_requests + 1,
                total_tokens = total_tokens + ?,
                total_credits = total_credits + ?,
                last_used_at = ?,
                updated_at = ?
            WHERE id=?
            """,
            (tokens, credit, now, now, aid),
        )
        conn.commit()
        conn.close()


# ============================================================
# Account resource / checkin cache
# ============================================================

def upsert_account_resource_cache(account_id: int, payload: dict):
    now = int(time.time())
    safe_payload = dict(payload or {})
    safe_payload["cached"] = False
    safe_payload["stale"] = False
    safe_payload["updated_at"] = int(safe_payload.get("updated_at") or now)
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO account_resource_cache (account_id, payload, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                json.dumps(safe_payload, ensure_ascii=False),
                safe_payload["updated_at"],
            ),
        )
        conn.commit()
        conn.close()


def get_account_resource_cache(account_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT payload, updated_at FROM account_resource_cache WHERE account_id=?",
        (account_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    payload["cached"] = True
    payload["cache_updated_at"] = int(
        row["updated_at"] or payload.get("updated_at") or 0
    )
    payload["age_seconds"] = max(
        0, int(time.time()) - int(payload["cache_updated_at"] or 0)
    )
    return payload


def upsert_account_checkin_cache(account_id: int, payload: dict):
    now = int(time.time())
    checkin_date = date.today().isoformat()
    safe_payload = dict(payload or {})
    safe_payload["cached"] = False
    safe_payload["stale"] = False
    safe_payload["updated_at"] = int(safe_payload.get("updated_at") or now)
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO account_checkin_cache (account_id, checkin_date, payload, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                checkin_date=excluded.checkin_date,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                checkin_date,
                json.dumps(safe_payload, ensure_ascii=False),
                safe_payload["updated_at"],
            ),
        )
        conn.commit()
        conn.close()


def get_account_checkin_cache(account_id: int, today_only: bool = True) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT checkin_date, payload, updated_at FROM account_checkin_cache WHERE account_id=?",
        (account_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if today_only and row["checkin_date"] != date.today().isoformat():
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    payload["cached"] = True
    payload["cache_date"] = row["checkin_date"]
    payload["cache_updated_at"] = int(
        row["updated_at"] or payload.get("updated_at") or 0
    )
    payload["age_seconds"] = max(
        0, int(time.time()) - int(payload["cache_updated_at"] or 0)
    )
    return payload


# ============================================================
# TraeWork 官方消耗真值（query_user_usage_group_by_session）
# 按天 + 模型聚合；由定时/手动同步写入，dashboard 直接读取。
# 与 logs.credit（估算）无关——这是官方 session 接口的真值。
# ============================================================

def upsert_traework_daily_credit(rows: list[dict]) -> None:
    """rows: [{day, model_name, credits, sessions}], upsert by (day, model_name)."""
    if not rows:
        return
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.executemany(
            """
            INSERT INTO traework_daily_credit (day, model_name, credits, sessions, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(day, model_name) DO UPDATE SET
                credits=excluded.credits,
                sessions=excluded.sessions,
                updated_at=excluded.updated_at
            """,
            [(r["day"], r["model_name"], r["credits"], r["sessions"], now) for r in rows],
        )
        conn.commit()
        conn.close()


def get_traework_daily_credit(days: int = 30) -> dict:
    """{day: {model_name: credits, ...}, ...} plus per-day aggregates."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT day, model_name, credits, sessions FROM traework_daily_credit ORDER BY day"
    ).fetchall()
    conn.close()
    by_day: dict[str, dict] = {}
    for r in rows:
        d = r["day"]
        by_day.setdefault(d, {"models": {}, "credits": 0.0, "sessions": 0})
        by_day[d]["models"][r["model_name"]] = float(r["credits"])
        by_day[d]["credits"] += float(r["credits"])
        by_day[d]["sessions"] += int(r["sessions"] or 0)
    return by_day


def get_traework_total_credit() -> float:
    conn = get_conn()
    val = conn.execute(
        "SELECT COALESCE(SUM(credits),0) FROM traework_daily_credit"
    ).fetchone()[0]
    conn.close()
    return float(val)


def latest_traework_sync_at() -> int:
    conn = get_conn()
    val = conn.execute(
        "SELECT COALESCE(MAX(updated_at),0) FROM traework_daily_credit"
    ).fetchone()[0]
    conn.close()
    return int(val or 0)
