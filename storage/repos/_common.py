"""Shared connection + helpers used by every repository.

Anything imported by more than one repo lives here so that we don't
trigger import cycles when a repo function needs to call into
another repo (e.g. stats reading traework daily credit).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from storage import credential_crypto

# DB file lives under data/ at the project root (not next to the module) so
# the source layout can change without dragging the user's runtime data along.
# data/ is .gitignored.
DB_PATH = Path(
    os.environ.get(
        "CB_GATEWAY_DB_PATH",
        Path(__file__).resolve().parent.parent.parent / "data" / "codebuddy_gateway.db",
    )
)

_lock = threading.Lock()

CREDENTIAL_FIELDS = ("access_token", "refresh_token", "session_state")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # WAL 模式下 NORMAL 即可保证一致性（崩溃恢复最多丢一页提交），
    # 每事务省一次 WAL fsync，写日志路径可显著降延迟。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def connection():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def today_start_ts() -> int:
    now = time.localtime()
    return int(
        time.mktime(
            (
                now.tm_year,
                now.tm_mon,
                now.tm_mday,
                0,
                0,
                0,
                now.tm_wday,
                now.tm_yday,
                now.tm_isdst,
            )
        )
    )


def load_allowed_models(value: Any) -> Optional[list]:
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
