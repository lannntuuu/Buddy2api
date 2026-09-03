"""storage.backup — sqlite snapshot helper.

A single, opinionated, callable that writes a consistent copy of the
live `codebuddy_gateway.db` to `data/backup/` under a name that
encodes the reason for the snapshot, then rotates old copies.

Why a dedicated module
----------------------
Pre-migration / dev snapshots used to live next to the live db
(`codebuddy_gateway.db.bak_before_<thing>_<ts>`) or in
`data/backup/` with a flat name. Both shapes are ambiguous when
restoring: you can't tell a manual one-shot from a daily auto
snapshot, and the loose `.bak_…` files drag `-wal`/`-shm` siblings
along that have no meaning outside the live db's WAL session.

This module is the single source of truth for the layout described
in `data/backup/README.md`. Anyone wanting to add a new snapshot
trigger (a script, a startup hook, a test fixture) calls
`storage.backup.snapshot(kind, reason=...)` and gets the file in the
right place with the right name, plus a credentials.key copy so the
restored db is still decryptable.

Why `Connection.backup()` instead of `shutil.copy`
--------------------------------------------------
Copying `codebuddy_gateway.db` while the gateway is running can race
with WAL writes: you get a torn file. `sqlite3.Connection.backup()`
takes a hot, transactional snapshot that's safe to read while writers
are active. Slightly slower than `cp` but correctness > speed here.

What this module does NOT do
----------------------------
- No HTTP / CLI surface. Callers invoke `snapshot()` directly. A
  thin CLI wrapper lives at `ops/scripts/backup-db.py`.
- No background scheduler. Auto snapshots are a separate concern;
  init_db() calls `snapshot("pre-migration", ...)` and the dev team
  adds their own cron / scheduled-task wrapper if they want daily
  rolling backups.
- No remote upload. S3 / rclone / etc. is a one-line follow-up that
  reads the returned path.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from storage.repos._common import DB_PATH, get_conn

logger = logging.getLogger("buddy2api.backup")

# Single, parseable filename format. The README in data/backup/
# documents this; do not change without updating that doc AND any
# downstream tooling that pattern-matches on it.
# Example: codebuddy-gateway__pre-migration__20260903-104500.db
NAME_TEMPLATE = "codebuddy-gateway__{kind}__{timestamp}.db"
CREDENTIALS_NAME = "credentials.key.latest"

# How many of each kind to keep. Anything older is deleted on the
# next snapshot() call. `manual` and the credentials file are
# never auto-rotated — those are user-driven.
ROTATION_KEEP = {
    "auto": 5,
    "manual": None,        # never rotate
    "pre-migration": 10,
    "pre-dev": 3,
}

VALID_KINDS = frozenset(ROTATION_KEEP.keys())


def _backup_dir() -> Path:
    """The directory where snapshots live. Created on first use."""
    p = DB_PATH.parent / "backup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ts() -> str:
    """Local-time stamp in the format used in the filename.

    `datetime.now()` is fine here — these names are for human reading
    and within-rotation sort order, not for cross-machine ordering.
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _name(kind: str, ts: str) -> str:
    if kind not in VALID_KINDS:
        raise ValueError(
            f"unknown backup kind {kind!r}; valid: {sorted(VALID_KINDS)}"
        )
    return NAME_TEMPLATE.format(kind=kind, timestamp=ts)


def snapshot(
    kind: str,
    *,
    reason: str = "",
) -> Optional[Path]:
    """Take a consistent snapshot of the live db.

    Returns the destination path, or `None` if the live db does not
    exist yet (first-run case: nothing to back up). Callers should
    treat `None` as "no-op" and not raise.

    The snapshot is a self-contained sqlite db — no `-wal`/`-shm`
    siblings. credentials.key is copied next to it under a fixed
    name so a restore grabs both files together.

    `reason` is recorded in the server log but is NOT encoded in the
    filename (the kind field already disambiguates). It's a free-form
    string for human readers when grepping logs.
    """
    if kind not in VALID_KINDS:
        raise ValueError(
            f"unknown backup kind {kind!r}; valid: {sorted(VALID_KINDS)}"
        )
    if not DB_PATH.exists():
        logger.info("backup: skip kind=%s reason=%s (no live db)", kind, reason)
        return None

    out_dir = _backup_dir()
    ts = _ts()
    target = out_dir / _name(kind, ts)

    # sqlite3 Connection.backup() is the only safe way to copy a hot
    # sqlite db; shutil.copy is not transactional.
    src = get_conn()
    try:
        # `target` must not exist for backup(); if it does, the call
        # raises. We've just generated a fresh timestamp so collisions
        # only happen on a sub-second double-snapshot, which is fine
        # to surface rather than silently overwrite.
        with sqlite3.connect(str(target)) as dst:
            src.backup(dst)
    finally:
        # On Windows, src.close() alone is not always enough to release
        # the file lock for the source db's WAL/SHM siblings, which then
        # blocks unlink() of the freshly-written destination db. Force
        # the connection (and its C-level handle) to drop before we
        # return, so callers — including our own rotation pass — can
        # actually delete old snapshots.
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        src.close()
        del src
        import gc
        gc.collect()

    # Always copy credentials.key alongside. The encryption key is
    # shared across the whole install, so we keep only the latest
    # copy under a stable name (no timestamp). If the user later
    # rotates the key (separate concern), a fresh copy overwrites.
    cred_src = DB_PATH.parent / "codebuddy_gateway.db.credentials.key"
    cred_dst = out_dir / CREDENTIALS_NAME
    if cred_src.exists():
        shutil.copy2(cred_src, cred_dst)
    else:
        logger.warning(
            "backup: no credentials.key at %s — restored db will be unable to "
            "decrypt access_token / refresh_token fields",
            cred_src,
        )

    logger.info("backup: wrote %s (kind=%s reason=%s)", target, kind, reason)
    _rotate(kind, out_dir)
    return target


def _rotate(kind: str, out_dir: Path) -> None:
    """Delete the oldest snapshots of this kind past the keep limit.

    Manual snapshots and the credentials file are never rotated.
    Rotation is by filename sort order, which works because the
    timestamp format is lexicographically monotonic.
    """
    keep = ROTATION_KEEP.get(kind)
    if keep is None:
        return
    pattern = NAME_TEMPLATE.format(kind=kind, timestamp="*")
    matches = sorted(out_dir.glob(pattern))
    excess = len(matches) - keep
    if excess <= 0:
        return
    for old in matches[:excess]:
        # On Windows the destination of a just-completed
        # Connection.backup() can leave a -wal / -shm shadow next to the
        # main db that keeps the directory entry locked even after the
        # connection closes. Clean those up alongside the main file, and
        # retry with backoff for the brief lock-hold interval.
        siblings = [old, old.with_suffix(old.suffix + "-wal"), old.with_suffix(old.suffix + "-shm")]
        last_exc: Optional[OSError] = None
        for attempt in range(10):
            try:
                for path in siblings:
                    if path.exists():
                        os.unlink(path)
                logger.info("backup: rotated %s", old)
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                _time.sleep(0.1 * (attempt + 1))
        if last_exc is not None:
            logger.warning("backup: failed to rotate %s: %s", old, last_exc)


def list_snapshots() -> list[Tuple[str, Path, int]]:
    """List all snapshots, newest first, as (kind, path, size_bytes).

    Used by the README examples and any future admin UI.
    """
    out_dir = _backup_dir()
    if not out_dir.exists():
        return []
    rows: list[Tuple[str, Path, int]] = []
    for f in out_dir.glob("codebuddy-gateway__*.db"):
        try:
            kind = f.stem.split("__")[1]
        except IndexError:
            continue
        rows.append((kind, f, f.stat().st_size))
    rows.sort(key=lambda r: r[1].name, reverse=True)
    return rows
