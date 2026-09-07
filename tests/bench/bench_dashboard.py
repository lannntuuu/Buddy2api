"""bench: 管理台重端点延迟基准(默认跳过,-m bench 显式运行)。

种子 1 万行 logs,压 /admin/stats、/admin/logs、/admin/traework/usage 各 50 次,
记录 P50/P95 到 .tmp/bench/last.json。P95<200ms 为软阈值:超标只告警不失败
(不同机器性能差异大,阈值仅作趋势观察)。
"""
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx
import pytest

import gateway.deps as deps

ROWS = 10_000
ITERATIONS = 50


def _seed_rows(db, base_ts):
    rows = []
    for i in range(ROWS):
        rows.append((
            None, None, None, None, f"model-{i % 7}", 0,
            100, 50, 150, 0.05,
            10, 0, None, "live",
            "stop", 300 + (i % 900), 200, "",
            "workbuddy", None, None, None,
            base_ts - (i % (30 * 86400)),
        ))
    conn = db.get_conn()
    conn.executemany(
        """
        INSERT INTO logs
            (api_key_id, api_key_name, account_id, account_name, model, stream,
             prompt_tokens, completion_tokens, total_tokens, credit,
             cache_read_tokens, cache_creation_tokens, usage_json, credit_source,
             finish_reason, duration_ms, status_code, error_msg,
             provider, client, client_version, reasoning_effort, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


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
def test_bench_dashboard_endpoints(isolated_db, monkeypatch):
    from gateway.server import app
    from storage import database as db

    monkeypatch.setattr(deps, "ALLOW_NO_ADMIN_AUTH", True)
    _seed_rows(db, int(time.time()))

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            targets = ["/admin/stats", "/admin/logs?limit=100", "/admin/traework/usage"]
            timings = {t: [] for t in targets}
            for _ in range(ITERATIONS):
                for target in targets:
                    t0 = time.perf_counter()
                    resp = await client.get(target)
                    assert resp.status_code == 200, f"{target}: {resp.status_code}"
                    timings[target].append((time.perf_counter() - t0) * 1000.0)
            return timings

    timings = asyncio.run(run())
    profiles = {}
    slow = []
    for target, runs in timings.items():
        p50 = round(statistics.median(runs), 2)
        p95 = round(sorted(runs)[int(len(runs) * 0.95)], 2)
        profiles[target] = {"p50_ms": p50, "p95_ms": p95}
        if p95 > 200:
            slow.append(f"{target} p95={p95}ms")
        print(f"[bench] {target}: p50={p50}ms p95={p95}ms")

    result = {"bench": "dashboard", "ts": time.time(), "rows": ROWS, "profiles": profiles}
    _write_bench_result(result)
    if slow:
        print(f"[bench] WARNING 软阈值(200ms)超标: {', '.join(slow)}")
