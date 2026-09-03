r"""Copy credentials.key + accounts + api_keys + settings from dev db to prod db.

This is the only safe manual migration. Do NOT use shutil.copy on the db file
itself - WAL mode means a hot copy may tear. We do per-table extraction
and INSERT OR REPLACE so dev IDs are preserved.

Usage:
    python ops/scripts/copy-dev-to-prod.py

Env overrides:
    DEV_DB_PATH  default: C:\Usr\Code\etc\Buddy2api\data\codebuddy_gateway.db
    PROD_DB_PATH default: C:\Usr\Code\etc\Buddy2api-prod\data\codebuddy_gateway.db
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

DEV_DB = Path(os.environ.get(
    "DEV_DB_PATH",
    Path(__file__).resolve().parent.parent.parent / "data" / "codebuddy_gateway.db",
))
PROD_DB = Path(os.environ.get(
    "PROD_DB_PATH",
    Path(__file__).resolve().parent.parent.parent.parent / "Buddy2api-prod" / "data" / "codebuddy_gateway.db",
))
DEV_KEY = DEV_DB.parent / "codebuddy_gateway.db.credentials.key"
PROD_KEY = PROD_DB.parent / "codebuddy_gateway.db.credentials.key"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    if not DEV_DB.exists():
        die(f"dev db not found at {DEV_DB}")
    if not DEV_KEY.exists():
        die(f"dev credentials.key not found at {DEV_KEY}")
    if not PROD_DB.exists():
        die(f"prod db not found at {PROD_DB}. Start prod server once so init_db runs.")
    if not PROD_DB.parent.exists():
        die(f"prod data dir not found at {PROD_DB.parent}")

    print(f"dev  db: {DEV_DB}")
    print(f"prod db: {PROD_DB}")
    print()

    # 1. credentials.key
    if PROD_KEY.exists():
        prod_key_bytes = PROD_KEY.read_bytes()
        dev_key_bytes = DEV_KEY.read_bytes()
        if prod_key_bytes == dev_key_bytes:
            print("[1/4] credentials.key: already in sync, no copy needed")
        else:
            print("[1/4] credentials.key: prod has DIFFERENT key, copying dev key")
            # Back up the prod key first so the user can revert if needed
            backup = PROD_KEY.with_suffix(PROD_KEY.suffix + ".pre-copy-bak")
            shutil.copy2(PROD_KEY, backup)
            print(f"        saved existing prod key to {backup}")
            shutil.copy2(DEV_KEY, PROD_KEY)
    else:
        print("[1/4] credentials.key: not yet present in prod, copying dev key")
        shutil.copy2(DEV_KEY, PROD_KEY)

    # 2-4. accounts / api_keys / settings
    # We do this in a single transaction on the prod side; the dev side is
    # read-only. INSERT OR REPLACE preserves dev row ids.
    dev_conn = sqlite3.connect(str(DEV_DB))
    dev_conn.row_factory = sqlite3.Row
    prod_conn = sqlite3.connect(str(PROD_DB))
    prod_conn.row_factory = sqlite3.Row

    try:
        # Capture dev source counts for the report
        src_counts = {
            t: dev_conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("accounts", "api_keys", "settings")
        }
        for t, n in src_counts.items():
            print(f"        dev {t}: {n} rows")

        # Make sure the prod schema has the tables we need. The dev and prod
        # repos are the same version, but defensive CREATE IF NOT EXISTS
        # makes this script resilient to schema drift in future versions.
        for ddl in (
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, uid TEXT,
                nickname TEXT, phone TEXT, account_type TEXT DEFAULT 'personal',
                access_token TEXT, refresh_token TEXT, expires_at INTEGER,
                refresh_expires_at INTEGER, domain TEXT DEFAULT 'www.codebuddy.cn',
                enterprise_id TEXT, session_state TEXT, status TEXT DEFAULT 'active',
                weight INTEGER DEFAULT 1, priority INTEGER DEFAULT 0,
                credit_limit REAL DEFAULT 0, credit_baseline REAL DEFAULT 0,
                last_used_at INTEGER, total_requests INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0, total_credits REAL DEFAULT 0,
                created_at INTEGER, updated_at INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key_prefix TEXT,
                key_hash TEXT UNIQUE, key_secret TEXT, name TEXT,
                status TEXT DEFAULT 'active', allowed_models TEXT,
                daily_limit INTEGER DEFAULT 0, client_type TEXT DEFAULT 'custom',
                total_requests INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
                created_at INTEGER, last_used_at INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            )
            """,
        ):
            prod_conn.execute(ddl)

        for table in ("accounts", "api_keys", "settings"):
            rows = dev_conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"[2/4] {table}: no rows in dev, skipping")
                continue
            cols = rows[0].keys()
            placeholders = ",".join("?" for _ in cols)
            col_list = ",".join(f"`{c}`" for c in cols)
            sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
            prod_conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
            print(f"[2/4] {table}: copied {len(rows)} rows from dev")

        prod_conn.commit()
        print()
        print("done. prod now has the same accounts / api_keys / settings as dev,")
        print("and the same credentials.key. logs / daily_usage / traework_daily_credit")
        print("were intentionally NOT copied (prod keeps its own forward-going log).")
    finally:
        dev_conn.close()
        prod_conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
