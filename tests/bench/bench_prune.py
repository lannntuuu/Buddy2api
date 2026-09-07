"""bench: logs 保留清理(prune_logs)耗时基准(默认跳过,-m bench 显式运行)。

种子 10 万行(约 25% 超过 90 天保留窗),测 db.prune_logs() 的耗时与删除行数,
结果写 .tmp/bench/last.json。prune 在生产由 lifespan 的 24h 周期任务调用,
此处测的是同步执行的墙钟(评估是否需要分批)。
"""
import json
import time
from pathlib import Path

import pytest

from storage import database as db

ROWS = 100_000
NOW = int(time.time())


def _write_bench_result(result: dict) -> None:
    out = Path(".tmp") / "bench" / "last.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = []
    if out.exists():
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = []
    data.append(result)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.bench
def test_bench_prune_logs(isolated_db):
    preexisting = _count_rows()
    rows = []
    for i in range(ROWS):
        # 每 4 行有 1 行落在 90 天窗之前(约 2.5 万行待删)
        age = 100 * 86400 if i % 4 == 0 else i % 80 * 86400
        rows.append((NOW - age,))
    conn = db.get_conn()
    conn.executemany("INSERT INTO logs (created_at) VALUES (?)", rows)
    conn.commit()
    conn.close()

    expired = ROWS // 4
    t0 = time.perf_counter()
    removed = db.prune_logs()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    result = {
        "bench": "prune_logs",
        "ts": time.time(),
        "seed_rows": ROWS,
        "removed": removed,
        "elapsed_ms": round(elapsed_ms, 2),
    }
    _write_bench_result(result)
    print(f"[bench] prune_logs: removed={removed} elapsed={result['elapsed_ms']}ms")
    assert removed == expired + preexisting, "删除行数应恰为过期行(含隔离库预置行)"
    assert result["elapsed_ms"] < 30_000, "prune 超过 30s 必须改分批"


def _count_rows() -> int:
    conn = db.get_conn()
    count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    conn.close()
    return count
