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
        Path(__file__).resolve().parent.parent.parent.parent / "data" / "codebuddy_gateway.db",
    )
)

_lock = threading.Lock()

CREDENTIAL_FIELDS = ("access_token", "refresh_token", "session_state")

# 连接建立时的容错参数。
# busy_timeout_ms:连接被别的写者占锁时,SQLite 阻塞等待写锁释放的毫秒上限
#   (默认与旧的 timeout=5 相同为 5000;可用环境变量 CB_GATEWAY_DB_BUSY_TIMEOUT_MS 覆盖)。
# 即便如此,WAL 下仍有极小概率在"建连后配置 PRAGMA"这几十毫秒窗口撞上某条
# 长写事务/checkpoint 把持写锁,且 busy handler 因同线程/读升级等场景并未介入,
# 于是 README 里那种 "database is locked" 会直接冒到热鉴权路径变成 500。
# 解法:把这段无锁 PRAGMA 初始化包进有界重试,瞬时锁不再穿透到调用方。
_DB_BUSY_TIMEOUT_MS = int(
    os.environ.get(
        "CB_GATEWAY_DB_BUSY_TIMEOUT_MS",
        "5000",
    )
)


def get_conn() -> sqlite3.Connection:
    """Open a fresh connection and initialize its connection-local pragmas.

    Retries the short, lock-free pragma setup on transient ``database is
    locked`` so a momentary WAL write lock never surfaces to callers
    (the auth/proxy hot path). The pragmas themselves are per-connection
    and don't need the database lock, so retrying them is cheap and safe.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    # Setup pragmas can throw sqlite3.OperationalError "database is locked"
    # if another writer holds the lock at this exact instant. Bounded retry
    # with a backoff; most transient locks clear in well under a second.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            # busy_timeout must be set *before* any statement that could
            # contend, so a busy writer ends up waiting rather than erroring.
            conn.execute(f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT_MS}")
            # WAL 模式下 NORMAL 即可保证一致性（崩溃恢复最多丢一页提交），
            # 每事务省一次 WAL fsync，写日志路径可显著降延迟。
            conn.execute("PRAGMA synchronous=NORMAL")
            break
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == max_attempts:
                conn.close()
                raise
            time.sleep(0.05 * attempt)
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
