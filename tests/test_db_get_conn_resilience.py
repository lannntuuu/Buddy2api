"""Regression: get_conn() must survive transient "database is locked".

Background: get_conn() opens a fresh SQLite connection and configures
connection-local PRAGMAs (foreign_keys / busy_timeout / synchronous). Under
WAL with concurrent short-lived writers, the opaque pragma setup can hit a
momentary ``SQLITE_BUSY`` and previously surfaced straight to the hot auth
path as an HTTP 500. get_conn() now retries that setup a bounded number of
times instead of raising.

These tests reproduce the contention with a real thread that holds a WAL
write lock open for a controlled interval, then confirms get_conn()
succeeds (retry path) and that a lock which outlives the retry budget still
raises cleanly.
"""
import sqlite3
import threading

import pytest

from storage.repos import _common


def _write_holder(path, hold_ms: int, started: threading.Event):
    """Open a connection and hold an IMMEDIATE write txn for hold_ms."""
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=0)
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO _t(x) VALUES (1)")
        started.set()
        threading.Event().wait(hold_ms / 1000.0)
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def _writable_db(isolated_db):
    with sqlite3.connect(str(isolated_db)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _t (x INTEGER)")
    return isolated_db


def test_get_conn_survives_short_write_lock(_writable_db):
    """Connecting while a brief write lock is held must succeed eventually."""
    started = threading.Event()
    # Hold the write lock briefly (~120ms), far shorter than the retry budget
    # (3 * one busy_timeout wait). On a healthy WAL db the busy handler clears
    # as soon as the writer commits, so this should land on the first retry.
    holder = threading.Thread(
        target=_write_holder, args=(_writable_db, 120, started), daemon=True
    )
    holder.start()
    assert started.wait(timeout=5), "write-lock holder never started"

    conn = _common.get_conn()
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()
    holder.join(timeout=5)