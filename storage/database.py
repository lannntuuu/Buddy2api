"""storage.database — thin re-export facade.

The actual data-access code now lives in storage.repos.* (one module
per domain: accounts, api_keys, logs, stats, settings, _common).

This facade preserves every public name so existing callers
(`from storage.database import add_account`, etc.) keep working.
init_db() orchestrates schema creation and migrations across all
repos in a single transaction.
"""
from __future__ import annotations

import os
import sys
import time
import types
from datetime import date, timedelta

from storage.repos._common import (
    DB_PATH,
    _lock,
    connection,
    get_conn,
    load_allowed_models as _load_allowed_models,
    today_start_ts as _today_start_ts,
)

# Tests (and any other caller) write to `storage.database.DB_PATH` to
# point the gateway at a different file. The connection helper
# `storage.repos._common.get_conn` reads `DB_PATH` from its own module
# namespace, so a plain re-export is not enough. Wrap this module in a
# tiny subclass that mirrors DB_PATH writes into `_common`.
class _DatabaseModule(types.ModuleType):
    def __setattr__(self, name: str, value):
        if name == "DB_PATH":
            from storage.repos import _common as _c
            setattr(_c, "DB_PATH", value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _DatabaseModule
from storage.repos.accounts import (
    account_increment_usage,
    add_account,
    delete_account,
    get_account,
    get_account_checkin_cache,
    get_account_resource_cache,
    get_active_accounts,
    get_traework_daily_credit,
    get_traework_total_credit,
    latest_traework_sync_at,
    list_accounts,
    update_account,
    upsert_account_checkin_cache,
    upsert_account_resource_cache,
    upsert_traework_daily_credit,
)
from storage.repos.api_keys import (
    _hash_api_key,
    _key_prefix,
    add_api_key,
    api_key_increment_usage,
    delete_api_key,
    get_api_key_by_key,
    get_api_key_daily_requests,
    has_api_keys,
    list_api_keys,
    release_api_key_request,
    reserve_api_key_request,
    update_api_key,
)
from storage.repos.logs import (
    add_log,
    list_logs,
    prune_logs,
    record_request,
    search_logs,
)
from storage.repos.settings import (
    delete_setting,
    get_all_settings,
    get_setting,
    set_setting,
)
from storage.repos.stats import get_provider_model_usage, get_stats

__all__ = [
    "DB_PATH",
    "get_conn",
    "connection",
    "init_db",
    # accounts
    "add_account",
    "update_account",
    "delete_account",
    "get_account",
    "list_accounts",
    "get_active_accounts",
    "account_increment_usage",
    "upsert_account_resource_cache",
    "get_account_resource_cache",
    "upsert_account_checkin_cache",
    "get_account_checkin_cache",
    "upsert_traework_daily_credit",
    "get_traework_daily_credit",
    "get_traework_total_credit",
    "latest_traework_sync_at",
    # api keys
    "add_api_key",
    "update_api_key",
    "delete_api_key",
    "has_api_keys",
    "get_api_key_by_key",
    "list_api_keys",
    "get_api_key_daily_requests",
    "reserve_api_key_request",
    "release_api_key_request",
    "api_key_increment_usage",
    # logs
    "add_log",
    "record_request",
    "prune_logs",
    "list_logs",
    "search_logs",
    # stats
    "get_stats",
    "get_provider_model_usage",
    # settings
    "get_setting",
    "set_setting",
    "delete_setting",
    "get_all_settings",
]


def init_db():
    """Create tables, run migrations, and prune old logs in one shot."""
    # Snapshot the live db before any schema work runs, so a botched
    # migration has a one-command fallback. Gateable via env so test
    # fixtures (which call init_db() on every test) don't fill the
    # tmp db's backup/ dir with hundreds of identical pre-migration
    # copies. Production: leave it on (default). CI: BUDDY2API_BACKUP_ON_INIT=0.
    if os.environ.get("BUDDY2API_BACKUP_ON_INIT", "1") == "1":
        try:
            from storage import backup as _backup
            _backup.snapshot("pre-migration", reason="init_db")
        except Exception:  # pragma: no cover - never let backup break startup
            import logging
            logging.getLogger("buddy2api.database").exception(
                "pre-migration backup failed; continuing with init_db"
            )
    with _lock:
        conn = get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                uid             TEXT,
                nickname        TEXT,
                phone           TEXT,
                account_type    TEXT DEFAULT 'personal',
                access_token    TEXT,
                refresh_token   TEXT,
                expires_at      INTEGER,
                refresh_expires_at INTEGER,
                domain          TEXT DEFAULT 'www.codebuddy.cn',
                enterprise_id   TEXT,
                session_state   TEXT,
                status          TEXT DEFAULT 'active',
                weight          INTEGER DEFAULT 1,
                priority        INTEGER DEFAULT 0,
                credit_limit    REAL DEFAULT 0,
                credit_baseline REAL DEFAULT 0,
                last_used_at    INTEGER,
                total_requests  INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                total_credits   REAL DEFAULT 0,
                created_at      INTEGER,
                updated_at      INTEGER
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                key_prefix      TEXT,
                key_hash        TEXT UNIQUE,
                key_secret      TEXT,
                name            TEXT,
                status          TEXT DEFAULT 'active',
                allowed_models  TEXT,
                daily_limit     INTEGER DEFAULT 0,
                client_type     TEXT DEFAULT 'custom',
                total_requests  INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                created_at      INTEGER,
                last_used_at    INTEGER
            );

            CREATE TABLE IF NOT EXISTS logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_id      INTEGER,
                api_key_name    TEXT,
                account_id      INTEGER,
                account_name    TEXT,
                model           TEXT,
                stream          INTEGER,
                prompt_tokens   INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                credit          REAL DEFAULT 0,
                finish_reason   TEXT,
                duration_ms     INTEGER,
                status_code     INTEGER,
                error_msg       TEXT,
                created_at      INTEGER
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS account_resource_cache (
                account_id INTEGER PRIMARY KEY,
                payload    TEXT NOT NULL,
                updated_at INTEGER,
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS account_checkin_cache (
                account_id   INTEGER PRIMARY KEY,
                checkin_date TEXT,
                payload      TEXT NOT NULL,
                updated_at   INTEGER,
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_resource_cache_updated
                ON account_resource_cache(updated_at);
            CREATE INDEX IF NOT EXISTS idx_checkin_cache_date
                ON account_checkin_cache(checkin_date);

            CREATE TABLE IF NOT EXISTS traework_daily_credit (
                day          TEXT NOT NULL,
                model_name  TEXT NOT NULL,
                credits     REAL NOT NULL DEFAULT 0,
                sessions    INTEGER NOT NULL DEFAULT 0,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY(day, model_name)
            );
            CREATE INDEX IF NOT EXISTS idx_tw_daily_day
                ON traework_daily_credit(day);
            """
        )
        # Migrations are split per repo but executed here so init_db
        # remains a single transactional entry point.
        from storage.repos import accounts as _accounts_repo
        from storage.repos import api_keys as _api_keys_repo
        from storage.repos import logs as _logs_repo

        _accounts_repo.migrate(conn)
        _api_keys_repo.migrate(conn)
        _logs_repo.migrate_provider(conn)
        _logs_repo.migrate_cache_tokens(conn)
        _logs_repo.migrate_reasoning(conn)
        _logs_repo.migrate_client(conn)
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
        _accounts_repo.migrate_credentials(conn)
        _api_keys_repo.migrate_daily_usage(conn)
        # Backfill today's daily usage from existing logs.
        conn.execute(
            """
            INSERT INTO api_key_daily_usage (api_key_id, usage_date, request_count)
            SELECT api_key_id, date(created_at, 'unixepoch', 'localtime'), COUNT(*)
            FROM logs
            WHERE api_key_id IS NOT NULL AND created_at >= ?
            GROUP BY api_key_id, date(created_at, 'unixepoch', 'localtime')
            ON CONFLICT(api_key_id, usage_date) DO UPDATE SET
                request_count=MAX(api_key_daily_usage.request_count, excluded.request_count)
            """,
            (_today_start_ts(),),
        )
        _prune_logs(conn)
        conn.execute("PRAGMA optimize")
        conn.commit()
        conn.close()


def _prune_logs(conn):
    """Inline retention sweep so init_db is one transactional call."""
    try:
        retention_days = max(
            1, int(os.environ.get("CB_GATEWAY_LOG_RETENTION_DAYS", "90"))
        )
    except ValueError:
        retention_days = 90
    cutoff_ts = int(time.time()) - retention_days * 86400
    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff_ts,))
    conn.execute(
        "DELETE FROM api_key_daily_usage WHERE usage_date < ?", (cutoff_date,)
    )
