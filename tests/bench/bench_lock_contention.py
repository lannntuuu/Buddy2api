"""bench: 全局 _lock 写路径竞争基准(默认跳过,-m bench 显式运行)。

模拟真实每请求写触点(deps.reserve BEGIN IMMEDIATE + 日志 record BEGIN
IMMEDIATE,C 个并发工作协程经线程池调真实仓储函数),测"每请求写段"
(reserve+record 一对)延迟 P50/P95/P99 随并发数 C 的曲线,定位全局
_lock 的拐点。四种模式做成本分解:

- current     : 现状。真实仓储函数,全局 _lock + 每操作新建连接。
- nolock      : 仅去掉全局 _lock(SQLite 层靠 BEGIN IMMEDIATE + busy_timeout
                串行)。隔离"锁本身"的开销与保护作用。
- queue_repo  : 单写线程队列,真实仓储函数(锁无竞争,连接模型不变)。
                对应提案"写连接队列化"的最小改法。
- queue_pool  : 单写线程队列 + DB_PATH 感知的线程本地连接复用 + 无锁。
                对应提案"队列化 + 连接池化"的上限。

DB_PATH 感知连接池原型(_ConnPool)即裁决依据之一:缓存键含 str(DB_PATH),
monkeypatch db.DB_PATH 切库时自动失效,不依赖 import 顺序,测试安全。

运行: python -m pytest tests/bench -m bench -s -k lock_contention
结果追加写 .tmp/bench/last.json(key="lock_contention")。
"""
import asyncio
import json
import os
import queue as _queue
import sqlite3
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

os.environ.setdefault("BUDDY2API_BACKUP_ON_INIT", "0")

import pytest

import storage.repos._common as _common
from storage.repos import api_keys as api_keys_repo
from storage.repos import logs as logs_repo
from storage import database as db

pytestmark = pytest.mark.bench

CONCURRENCY = [1, 2, 4, 8, 16, 32, 64]
PAIRS_PER_LEVEL = 256
WARMUP_PAIRS = 8


# ============================================================
# 模式基础设施
# ============================================================

class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CloseGuard:
    """包装池化连接:close() 变 no-op,其余属性透传(仓储会直接 conn.close())。"""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _ConnPool:
    """线程本地长连连接池;键含 str(DB_PATH),切库自动失效(测试安全)。"""

    def __init__(self):
        self._local = threading.local()

    def _conn(self):
        key = str(_common.DB_PATH)
        cache = getattr(self._local, "cache", None)
        if cache is None or cache[0] != key:
            cache = (key, {})
            self._local.cache = cache
        conn = cache[1].get("rw")
        if conn is None:
            conn = _common.get_conn()
            cache[1]["rw"] = conn
        return conn

    @contextmanager
    def connection(self):
        yield self._conn()

    def get_conn(self):
        return _CloseGuard(self._conn())


_LOCK_HOLDERS = [db, api_keys_repo, logs_repo]


def _patch_write_stack(monkeypatch, pool: _ConnPool | None):
    """替换各仓储模块持有的 _lock/get_conn/connection 副本(by-value 导入,
    必须逐模块 patch 才生效)。pool=None 表示只去锁、连接模型保持现状。"""
    for mod in _LOCK_HOLDERS:
        if hasattr(mod, "_lock"):
            monkeypatch.setattr(mod, "_lock", _NullLock())
    if pool is not None:
        for mod in _LOCK_HOLDERS:
            if hasattr(mod, "get_conn"):
                monkeypatch.setattr(mod, "get_conn", pool.get_conn)
            if hasattr(mod, "connection"):
                monkeypatch.setattr(mod, "connection", pool.connection)


class _WriterQueue:
    """单写线程:写任务串行执行在专属线程上(真实仓储函数原样投递)。"""

    def __init__(self):
        self._q: _queue.Queue = _queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, args, future = item
            try:
                future.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001 - 原样回传给等待方
                future.set_exception(exc)

    async def submit(self, fn, *args):
        import concurrent.futures as _cf

        future = _cf.Future()
        self._q.put((fn, args, future))
        return await asyncio.wrap_future(future)

    def stop(self):
        self._q.put(None)
        self._thread.join(timeout=5)


# ============================================================
# 负载模型:一对写触点 = reserve(BEGIN IMMEDIATE) + record(BEGIN IMMEDIATE)
# ============================================================

def _make_record_row(kid: int, seq: int) -> dict:
    return {
        "api_key_id": kid,
        "api_key_name": "bench",
        "account_id": None,
        "model": "bench-model",
        "stream": 1,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "credit": 0.05,
        "finish_reason": "stop",
        "duration_ms": 300,
        "status_code": 200,
        "error_msg": "",
        "provider": "workbuddy",
        "created_at": int(time.time()),
        "first_token_ms": 120,
    }


async def _worker(mode, kid, ops, executor, wq, samples, errors):
    loop = asyncio.get_running_loop()
    for seq in range(ops):
        t0 = time.perf_counter_ns()
        try:
            if mode in ("current", "nolock"):
                await loop.run_in_executor(
                    executor, db.reserve_api_key_request, kid, 0
                )
                await loop.run_in_executor(
                    executor, db.record_request, _make_record_row(kid, seq)
                )
            else:  # queue_repo / queue_pool
                await wq.submit(db.reserve_api_key_request, kid, 0)
                await wq.submit(db.record_request, _make_record_row(kid, seq))
        except Exception:
            errors.append(1)
            continue
        samples.append((time.perf_counter_ns() - t0) / 1e6)


def _percentiles(samples):
    ordered = sorted(samples)
    def pct(p):
        rank = max(1, int(len(ordered) * p))
        return round(ordered[min(len(ordered), rank) - 1], 3)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "max_ms": round(ordered[-1], 3),
    }


def _run_level(mode, kid, level, monkeypatch):
    pool = _ConnPool() if mode == "queue_pool" else None
    wq = _WriterQueue() if mode.startswith("queue") else None
    if mode == "nolock":
        _patch_write_stack(monkeypatch, None)
    elif mode == "queue_pool":
        _patch_write_stack(monkeypatch, pool)

    async def drive():
        executor = ThreadPoolExecutor(max_workers=level)
        try:
            for _ in range(WARMUP_PAIRS):
                db.reserve_api_key_request(kid, 0)
                db.record_request(_make_record_row(kid, 0))
            samples, errors = [], []
            tasks = [
                asyncio.create_task(
                    _worker(mode, kid, -(-PAIRS_PER_LEVEL // level), executor,
                            wq, samples, errors)
                )
                for _ in range(level)
            ]
            t0 = time.perf_counter()
            await asyncio.gather(*tasks)
            wall = time.perf_counter() - t0
            return samples, sum(errors), wall
        finally:
            executor.shutdown(wait=False)
            if wq is not None:
                wq.stop()

    samples, errors, wall = asyncio.run(drive())
    result = _percentiles(samples)
    result["throughput_pairs_per_s"] = round(len(samples) / wall, 1)
    result["errors"] = errors
    return result


@pytest.mark.bench
def test_bench_lock_contention(isolated_db, monkeypatch):
    kid = db.add_api_key("bench-lock-key", "bench", None, 0)
    modes = ["current", "nolock", "queue_repo", "queue_pool"]
    table = {}
    for mode in modes:
        table[mode] = {}
        for level in CONCURRENCY:
            table[mode][level] = _run_level(mode, kid, level, monkeypatch)
            row = table[mode][level]
            print(
                f"[bench] {mode:12s} C={level:>2} "
                f"p50={row['p50_ms']:>8}ms p95={row['p95_ms']:>8}ms "
                f"p99={row['p99_ms']:>8}ms tput={row['throughput_pairs_per_s']:>7}/s "
                f"err={row['errors']}"
            )

    base = table["current"][1]["p95_ms"]
    knee = next(
        (c for c in CONCURRENCY
         if table["current"][c]["p95_ms"] >= max(2 * base, 10.0)),
        None,
    )
    print(f"[bench] current 基线 p95(C=1)={base}ms;拐点(2x 或 >10ms)C={knee}")

    out = os.path.join(".tmp", "bench", "last.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    data = []
    if os.path.exists(out):
        try:
            data = json.loads(open(out, encoding="utf-8").read())
        except (json.JSONDecodeError, OSError):
            data = []
    data.append({
        "bench": "lock_contention",
        "ts": time.time(),
        "pairs_per_level": PAIRS_PER_LEVEL,
        "knee_concurrency": knee,
        "table": table,
    })
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
