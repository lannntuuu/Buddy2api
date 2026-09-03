"""Settings repository: key-value table for system configuration."""
from __future__ import annotations

import json
from typing import Any

from storage.repos._common import _lock, get_conn


def get_setting(key: str, default: Any = None) -> Any:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    val = row["value"]
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


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


def delete_setting(key: str):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()
        conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    return result
