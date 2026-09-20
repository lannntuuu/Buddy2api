"""Backfill: recompute logs.credit from upstream-reported credit (usage_json).

Qoder CN reports the real per-request charge in `usage.credits` + `usage.billable`.
Before store_common.upstream_credit existed, every channel fell back to
`total_tokens / credit_rate`, so `billable=false` free-tier requests
(Qwen3.8-Flash / qfmodel) were booked as consumption -- 51 free requests had
accumulated 4612.70 fake credits.

This script rewrites only rows whose usage_json carries a real upstream value
(or an explicit billable=false), then re-sums accounts.total_credits.

    python ops/scripts/oneoff/backfill-upstream-credit.py [db-path]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")  # 与 tests/pytest.ini 的 pythonpath 同源
from providers.store_common import upstream_credit  # noqa: E402
from storage import database as db  # noqa: E402

TARGET = "qodercn"  # 目前只有该通道在 usage 里回报真值


def main(db_path: str | None = None) -> None:
    if db_path:
        db.DB_PATH = Path(db_path).resolve()  # 镜像进 _common（database.__setattr__ 已处理）
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, account_id, model, credit, usage_json FROM logs "
        "WHERE provider=? AND usage_json IS NOT NULL",
        (TARGET,),
    ).fetchall()
    print(f"rows to inspect: {len(rows)}")

    updates = []
    for r in rows:
        try:
            usage = json.loads(r["usage_json"])
        except (TypeError, ValueError):
            continue
        new_credit = upstream_credit(usage)
        if new_credit is None:
            continue  # 上游没报 → 保留原估算
        if abs(new_credit - float(r["credit"] or 0)) < 1e-9:
            continue
        updates.append((new_credit, r["id"]))

    if updates:
        with db._lock:
            conn.executemany("UPDATE logs SET credit=? WHERE id=?", updates)
            conn.commit()
    print(f"logs.credit updated: {len(updates)} rows")

    for a in conn.execute(
        "SELECT id FROM accounts WHERE provider=?", (TARGET,)
    ).fetchall():
        total = conn.execute(
            "SELECT COALESCE(SUM(credit),0) FROM logs WHERE account_id=? AND provider=?",
            (a["id"], TARGET),
        ).fetchone()[0]
        conn.execute(
            "UPDATE accounts SET total_credits=? WHERE id=?",
            (round(float(total), 6), a["id"]),
        )
        print(f"account {a['id']}: total_credits -> {round(float(total), 4)}")
    conn.commit()

    before = sum(float(r["credit"] or 0) for r in rows)
    after = conn.execute(
        "SELECT COALESCE(SUM(credit),0) FROM logs WHERE provider=?", (TARGET,)
    ).fetchone()[0]
    print(f"\n{TARGET} logs.credit: {round(before, 3)} -> {round(float(after), 3)}")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
