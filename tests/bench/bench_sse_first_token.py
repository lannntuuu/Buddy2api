"""bench: workbuddy SSE 流端到端延迟基准(31 号方案 §5;默认跳过,-m bench 显式运行)。

Mock 假上游按 0/50/200ms 三档首帧延迟各 200 轮,经 proxy._stream_upstream 全链路
(账号选择 -> SSE 解析 -> 观察者聚合)测量端到端墙钟 P50/P95,结果追加写 .tmp/bench/last.json。

说明:
- 其余三条 SSE 路径(openai_compat/traesolo/traework)的首包语义由 tests/test_perf_metrics.py
  单测覆盖;本基准以最热的 workbuddy 路径为代表做趋势回归。
- 墙钟含生成器收尾,不代表纯 first_token_ms;护栏只防重大退化(0ms 档 P95 < 1s)。
"""
import asyncio
import json
import statistics
import time
from pathlib import Path

import pytest

import upstream.proxy as proxy
from accounts import auth_manager


class _FakeResponse:
    status_code = 200

    def __init__(self, first_delay_ms):
        self.first_delay_ms = first_delay_ms

    async def aread(self):
        return b""

    async def aiter_bytes(self):
        body = (
            b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,'
            b'"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
            b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,'
            b'"delta":{},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        await asyncio.sleep(self.first_delay_ms / 1000.0)
        yield body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    first_delay_ms = 0.0

    def __init__(self, *args, **kwargs):
        pass

    def stream(self, *args, **kwargs):
        return _FakeResponse(self.first_delay_ms)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


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
def test_bench_sse_stream_latency(isolated_db, monkeypatch):
    account = {"id": 1, "name": "bench-account", "provider": "workbuddy"}
    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", _async_return(account))
    monkeypatch.setattr(auth_manager, "get_valid_headers", _async_return({"Authorization": "Bearer t"}))
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_: None)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda d: 30)
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: None)
    monkeypatch.setattr(proxy, "_get_client", lambda: _FakeClient())

    body = {
        "model": "bench-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    info = {"id": 9, "name": "bench-key", "_log_model": "bench-model", "_bind_channel": "workbuddy"}

    async def collect():
        chunks = []
        async for chunk in proxy._stream_upstream(body, info, "bench-model"):
            chunks.append(chunk)
        return b"".join(chunks)

    samples = {}
    for label, delay_ms in (("0ms", 0.0), ("50ms", 50.0), ("200ms", 200.0)):
        _FakeClient.first_delay_ms = delay_ms
        runs = []
        for _ in range(200):
            t0 = time.perf_counter()
            asyncio.run(collect())
            runs.append((time.perf_counter() - t0) * 1000.0)
        samples[label] = runs

    result = {
        "bench": "sse_stream_latency",
        "ts": time.time(),
        "profiles": {
            label: {
                "p50_ms": round(statistics.median(runs), 2),
                "p95_ms": round(sorted(runs)[int(len(runs) * 0.95)], 2),
            }
            for label, runs in samples.items()
        },
    }
    _write_bench_result(result)

    # 宽松护栏:mock 上游 0ms 档端到端 P95 不应超过 1s(防重大退化)
    assert result["profiles"]["0ms"]["p95_ms"] < 1000
