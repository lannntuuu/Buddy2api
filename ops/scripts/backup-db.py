"""ops/scripts/backup-db.py — manual gateway db snapshot.

Thin CLI over `storage.backup.snapshot`. Use this before any risky
operation (dev experiments, schema change try-out, manual data fix)
where you want a one-shot snapshot that won't get auto-rotated.

Usage:
    python ops/scripts/backup-db.py manual
    python ops/scripts/backup-db.py pre-dev
    python ops/scripts/backup-db.py                  # default: manual
    python ops/scripts/backup-db.py --list

The snapshot lands in data/backup/ under the standard name; see
data/backup/README.md for the layout and the restore procedure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project root importable when run as `python ops/scripts/backup-db.py`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage import backup  # noqa: E402  (sys.path tweak above)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Take a manual snapshot of the gateway db.",
    )
    parser.add_argument(
        "kind",
        nargs="?",
        default="manual",
        choices=sorted(backup.VALID_KINDS),
        help="Snapshot kind. `manual` and `pre-dev` are the user-driven ones.",
    )
    parser.add_argument(
        "--reason",
        default="",
        help="Free-form note recorded in the log; not part of the filename.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List existing snapshots and exit; do not write a new one.",
    )
    args = parser.parse_args()

    if args.list:
        rows = backup.list_snapshots()
        if not rows:
            print("(no snapshots in data/backup/)")
            return 0
        print(f"{'kind':<14} {'size':>10}  path")
        print("-" * 80)
        for kind, path, size in rows:
            print(f"{kind:<14} {size:>10}  {path}")
        return 0

    path = backup.snapshot(args.kind, reason=args.reason or "cli")
    if path is None:
        print("No live db found at the configured DB_PATH; nothing to back up.")
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
