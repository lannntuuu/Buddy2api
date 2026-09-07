"""WS-1 指标+策略测试（redesign-audit/32 spec §1.1-1.6）。

覆盖：
- 1.1 first_token_ms：logs 列/迁移、P95 纯函数与 stream_p95 统计、
  proxy/openai_compat/traesolo/qclaw/qwenwork/traework 流式打点（分帧 mock）、
  retry 行与错误行保持 NULL、created_at 透传。
- 1.2 eof_error 两分支：未出流 → 降分 + 跨账号重试；出流后 → 不降分不重试透传收尾。
- 1.3 retry_delay equal-jitter（rng 注入）+ Retry-After 透传 + 老签名兼容。
- 1.4 refresh 负缓存自适应间隔（trae_shared + traesolo，fake clock）。
- 1.5 调度同级加权随机决胜（_route_rng 注入）。
- 1.6 池参数 64/32、search_logs 7d 默认窗口、log-prune 24h 循环、
  /static 缓存头与 index no-cache、uvicorn keep-alive 参数。

沿用本仓库约定：同步测试函数内用 asyncio.run 驱动协程（未装 pytest-asyncio）。
"""
import asyncio
import contextlib
import json
import time

import httpx
import pytest

import gateway.server as server_mod
from accounts import auth_manager
from providers import retry as retry_module
from providers import store_common
from providers import trae_shared
from providers.openai_compat import OpenAICompatProvider
from providers.qclaw import chat as qclaw_chat
from providers.qwenwork import chat as qwenwork_chat
from providers.traework import chat as traework_chat
from providers.traesolo import chat as tsc
from storage import database as db
from storage import http_pool
from storage.repos import logs as logs_repo
from upstream import proxy


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _log_row(**over):
    row = {
        "api_key_id": None, "api_key_name": None,
        "account_id": None, "account_name": None,
        "provider": "traesolo", "model": "glm-5.2", "stream": 1,
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "credit": 0.0, "finish_reason": "stop", "duration_ms": 10,
        "status_code": 200, "error_msg": "",
    }
    row.update(over)
    return row


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")


def _chat_chunk(delta: dict, finish=None, created=1, cid="c1"):
    return _sse({
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


class _ProxyClock:
    """proxy.time 替身：monotonic 每次调用前进 0.05s；time 固定值。"""

    def __init__(self):
        self.monotonic_t = 1_000_000.0

    def monotonic(self):
        self.monotonic_t += 0.05
        return self.monotonic_t

    def time(self):
        return 1_700_000_000.0


class _FakeResponse:
    """client.stream(...) 的 200 流式响应替身。"""

    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _ErrResponse:
    """非 200 响应替身：aread + headers。"""

    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return self._body


def _install_proxy_upstream(monkeypatch, responses):
    """按 X-Test-Account 头路由的 FakeAsyncClient + 固定账号序列。

    responses: {account_id: 响应对象}；accounts: 依次返回的账号。
    """
    calls = {"picks": [], "failures": [], "successes": [], "delays": []}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, headers, **kwargs):
            return responses[int(headers["X-Test-Account"])]

    accounts = [
        {"id": 1, "name": "acc-1"},
        {"id": 2, "name": "acc-2"},
    ]

    async def pick_account(_excluded):
        calls["picks"].append(set(_excluded))
        for account in accounts:
            if account["id"] not in _excluded:
                return account
        return None

    async def valid_headers(_account):
        return {"X-Test-Account": str(_account["id"])}

    def mark_failure(aid, status=0):
        calls["failures"].append((aid, status))

    def mark_success(aid):
        calls["successes"].append(aid)

    async def no_delay(attempt, **kwargs):
        calls["delays"].append((attempt, kwargs.get("retry_after")))

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick_account)
    monkeypatch.setattr(auth_manager, "get_valid_headers", valid_headers)
    monkeypatch.setattr(auth_manager, "mark_account_failure", mark_failure)
    monkeypatch.setattr(auth_manager, "mark_account_success", mark_success)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda default: 30)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeAsyncClient)
    return calls


async def _collect_stream(body=None):
    return b"".join([
        chunk
        async for chunk in proxy._stream_upstream(
            body or {"model": "test-model", "stream": True}, None, "test-model"
        )
    ])


# ---------------------------------------------------------------------------
# 1.1 P95 纯函数 + stream_p95 统计 + 落库列
# ---------------------------------------------------------------------------

def test_p95_of_pure_function():
    assert logs_repo.p95_of([]) == 0
    assert logs_repo.p95_of([42]) == 42
    # n=100 → rank=ceil(95)=95 → 第 95 小
    assert logs_repo.p95_of(list(range(1, 101))) == 95
    # n=20 → rank=19 → 第 19 小
    assert logs_repo.p95_of(list(range(1, 21))) == 19
    # 乱序输入等价
    shuffled = [9, 1, 5, 7, 3, 11, 13, 2, 19, 4, 6, 8, 10, 12, 14, 15, 16, 17, 18, 20]
    assert logs_repo.p95_of(shuffled) == 19


def test_stream_p95_by_provider_and_get_stats(isolated_db):
    now = int(time.time())
    for ms in (10, 30, 50, 100, 200):
        db.record_request(_log_row(provider="traesolo", stream=1, first_token_ms=ms, created_at=now))
    db.record_request(_log_row(provider="workbuddy", stream=1, first_token_ms=7, created_at=now))
    # 非流式行与 first_token_ms 为 NULL 的行不计入
    db.record_request(_log_row(provider="traesolo", stream=0, first_token_ms=99999, created_at=now))
    db.record_request(_log_row(provider="traesolo", stream=1, first_token_ms=None, created_at=now))
    # 近 7 天窗口之外的样本不计入
    db.record_request(_log_row(
        provider="traesolo", stream=1, first_token_ms=5000,
        created_at=now - 8 * 86400,
    ))
    assert logs_repo.stream_p95_by_provider() == {"traesolo": 200, "workbuddy": 7}
    stats = db.get_stats()
    assert stats["stream_p95"] == {"traesolo": 200, "workbuddy": 7}


def test_get_stats_stream_p95_empty(isolated_db):
    assert db.get_stats()["stream_p95"] == {}


def test_record_request_first_token_and_created_at_columns(isolated_db):
    db.record_request(_log_row(first_token_ms=123, created_at=1_700_000_000))
    row = db.list_logs(1)[0]
    assert row["first_token_ms"] == 123
    assert row["created_at"] == 1_700_000_000
    # 缺省：first_token NULL，created_at 落库时刻
    db.record_request(_log_row())
    row2 = db.list_logs(1)[0]
    assert row2["first_token_ms"] is None
    assert abs(row2["created_at"] - int(time.time())) <= 5


def test_store_common_log_request_passthrough(isolated_db):
    asyncio.run(store_common.log_request(
        None, None, channel="traesolo", model="glm-5.2", stream=True,
        usage=None, finish_reason="stop", status_code=200, duration_ms=5,
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
        first_token_ms=321, created_at=1_700_000_001,
    ))
    row = db.list_logs(1)[0]
    assert row["first_token_ms"] == 321
    assert row["created_at"] == 1_700_000_001


# ---------------------------------------------------------------------------
# 1.1 proxy._stream_upstream 打点（分帧 mock + 可控时钟）
# ---------------------------------------------------------------------------

def test_proxy_stream_first_token_ms_with_multiframe_chunks(monkeypatch):
    # 内容帧拆成 7 字节小帧喂给 decoder（分帧 mock），首帧时刻即 first_token
    frames = [
        _chat_chunk({"content": "he"}),
        _chat_chunk({"content": "llo"}),
        _chat_chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ]
    payload = b"".join(frames)
    chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]
    monkeypatch.setattr(proxy, "time", _ProxyClock())
    calls = _install_proxy_upstream(monkeypatch, {1: _FakeResponse(chunks)})
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    raw = asyncio.run(_collect_stream())

    assert b'"content": "he"' in raw and b"llo" in raw and b"[DONE]" in raw
    assert calls["failures"] == [] and calls["successes"] == [1]
    success = [k for a, k in logs if a[9] == 200]
    assert len(success) == 1
    # 基线（循环外 request_t0）到首个内容帧恰好一次 0.05s 时钟步进
    assert success[0]["first_token_ms"] == 50
    assert isinstance(success[0]["first_token_ms"], int)


def test_proxy_stream_first_token_not_overwritten_by_later_frames(monkeypatch):
    # 时钟持续步进，若后续帧覆盖 first_token_ms 就不再是 50
    frames = [
        _chat_chunk({"content": "a"}),
        _chat_chunk({"content": "b"}),
        _chat_chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(proxy, "time", _ProxyClock())
    _install_proxy_upstream(monkeypatch, {1: _FakeResponse(frames)})
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))
    asyncio.run(_collect_stream())
    success = [k for a, k in logs if a[9] == 200]
    assert success[0]["first_token_ms"] == 50


def test_proxy_stream_retry_row_keeps_first_token_null(monkeypatch):
    # 账号 1 出流前坏流（malformed data）→ retry 行 first_token_ms=NULL，
    # 账号 2 成功 → first_token_ms 从请求起点（含重试）计 50
    bad = _FakeResponse([b"data: {not-json}\n\n"])
    good_frames = [
        _chat_chunk({"content": "ok"}),
        _chat_chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(proxy, "time", _ProxyClock())
    calls = _install_proxy_upstream(
        monkeypatch, {1: bad, 2: _FakeResponse(good_frames)}
    )
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    raw = asyncio.run(_collect_stream())

    assert calls["failures"] == [(1, 502)] and calls["successes"] == [2]
    assert calls["picks"] == [set(), {1}]
    retry_rows = [k for a, k in logs if a[8] == "retry"]
    assert len(retry_rows) == 1
    assert retry_rows[0].get("first_token_ms") is None
    success = [k for a, k in logs if a[9] == 200]
    assert len(success) == 1 and success[0]["first_token_ms"] == 50
    assert b'"content": "ok"' in raw


# ---------------------------------------------------------------------------
# 1.2 eof_error 两分支
# ---------------------------------------------------------------------------

def test_eof_before_output_marks_failure_and_rotates(monkeypatch):
    # 未出流 EOF（只有注释行）→ mark_account_failure + 跨账号重试
    comments = _FakeResponse([b": keepalive\n\n", b": still-alive\n\n"])
    good_frames = [
        _chat_chunk({"content": "second"}),
        _chat_chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ]
    calls = _install_proxy_upstream(
        monkeypatch, {1: comments, 2: _FakeResponse(good_frames)}
    )
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    raw = asyncio.run(_collect_stream())

    assert calls["picks"] == [set(), {1}]
    assert calls["failures"] == [(1, 502)]
    assert calls["successes"] == [2]
    assert b'"content": "second"' in raw
    assert b'"error"' not in raw
    retry_rows = [k for a, k in logs if a[8] == "retry"]
    assert len(retry_rows) == 1
    assert retry_rows[0].get("first_token_ms") is None


def test_eof_after_output_keeps_account_and_passes_error_through(monkeypatch):
    # 已出流（内容帧已发给客户端）后 EOF → 不降分、不跨账号重试，
    # 记 error 日志并注入错误事件收尾
    frames = [_chat_chunk({"content": "partial"}), b"data: [DONE] never\n\n"]
    calls = _install_proxy_upstream(monkeypatch, {1: _FakeResponse(frames)})
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    raw = asyncio.run(_collect_stream())

    # 已转发的内容保留
    assert b'"content": "partial"' in raw
    assert calls["picks"] == [set()]          # 只选过一次账号：未跨账号重试
    assert calls["failures"] == []            # 未降分
    assert calls["successes"] == []
    error_rows = [(a, k) for a, k in logs if a[8] == "error"]
    assert len(error_rows) == 1
    assert error_rows[0][0][9] == 502
    assert error_rows[0][1].get("first_token_ms") is None
    # 错误事件 + [DONE] 收尾
    assert b'"error"' in raw and raw.count(b"data: [DONE]") == 1


# ---------------------------------------------------------------------------
# 1.3 retry_delay equal-jitter + Retry-After
# ---------------------------------------------------------------------------

class _FakeRng:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


@pytest.fixture()
def _sleep_recorder(monkeypatch):
    recorded = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)
    return recorded


def test_retry_delay_equal_jitter_with_injected_rng(_sleep_recorder):
    asyncio.run(retry_module.retry_delay(0, rng=_FakeRng(0.0)))
    asyncio.run(retry_module.retry_delay(0, rng=_FakeRng(1.0)))
    asyncio.run(retry_module.retry_delay(1, rng=_FakeRng(0.5)))
    asyncio.run(retry_module.retry_delay(2, rng=_FakeRng(0.5)))  # 末次尝试不睡
    assert _sleep_recorder[0] == pytest.approx(0.125)   # 0.25 × 0.5
    assert _sleep_recorder[1] == pytest.approx(0.375)   # 0.25 × 1.5
    assert _sleep_recorder[2] == pytest.approx(0.5)     # 0.5 × 1.0
    assert len(_sleep_recorder) == 3


def test_retry_delay_default_rng_still_works(_sleep_recorder):
    # 老调用形态（不传新参）兼容：attempt=1 底数 0.5 × [0.5, 1.5)
    asyncio.run(retry_module.retry_delay(1))
    assert 0.25 <= _sleep_recorder[0] < 0.75
    asyncio.run(retry_module.retry_delay(2))            # 末次：直接返回
    assert len(_sleep_recorder) == 1


def test_retry_delay_retry_after_cap(_sleep_recorder):
    asyncio.run(retry_module.retry_delay(0, retry_after=5))
    asyncio.run(retry_module.retry_delay(0, retry_after="0.4"))
    asyncio.run(retry_module.retry_delay(0, retry_after="not-a-number", rng=_FakeRng(0.5)))
    asyncio.run(retry_module.retry_delay(2, retry_after=5))  # 末次：仍不睡
    assert _sleep_recorder[0] == 2.0
    assert _sleep_recorder[1] == pytest.approx(0.4)
    assert _sleep_recorder[2] == pytest.approx(0.25)
    assert len(_sleep_recorder) == 3


def test_parse_retry_after_pure_numbers_only():
    assert proxy._parse_retry_after("5") == 5.0
    assert proxy._parse_retry_after(" 2.5 ") == 2.5
    assert proxy._parse_retry_after("") is None
    assert proxy._parse_retry_after(None) is None
    assert proxy._parse_retry_after("abc") is None
    assert proxy._parse_retry_after("-1") is None
    assert proxy._parse_retry_after("inf") is None
    assert proxy._parse_retry_after("nan") is None


def test_proxy_stream_429_reads_retry_after_header(monkeypatch):
    err = _ErrResponse(429, b"slow down", {"retry-after": "7"})
    good_frames = [
        _chat_chunk({"content": "ok"}),
        _chat_chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ]
    calls = _install_proxy_upstream(monkeypatch, {1: err, 2: _FakeResponse(good_frames)})
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    raw = asyncio.run(_collect_stream())

    # retry_after 透传给 retry_delay
    assert calls["delays"] == [(0, 7.0)]
    assert calls["failures"] == [(1, 429)] and calls["successes"] == [2]
    retry_rows = [k for a, k in logs if a[8] == "retry"]
    assert len(retry_rows) == 1
    assert retry_rows[0].get("first_token_ms") is None
    assert b'"content": "ok"' in raw


def test_proxy_stream_retryable_without_header_keeps_legacy_call_shape(monkeypatch):
    err = _ErrResponse(429, b"slow down")
    good_frames = [
        _chat_chunk({"content": "ok"}),
        _chat_chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ]
    calls = _install_proxy_upstream(monkeypatch, {1: err, 2: _FakeResponse(good_frames)})
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: None)

    asyncio.run(_collect_stream())
    # 无 Retry-After 头：不携带新关键字（兼容既有测试替身的单参签名）
    assert calls["delays"] == [(0, None)]


# ---------------------------------------------------------------------------
# 1.4 refresh 负缓存自适应（trae_shared + traesolo，fake clock）
# ---------------------------------------------------------------------------

@pytest.fixture()
def _reset_refresh_state():
    trae_shared.reset_refresh_failures()
    tsc._reset_refresh_fail_cache()
    yield
    trae_shared.reset_refresh_failures()
    tsc._reset_refresh_fail_cache()


def test_trae_shared_negative_cache_adaptive_interval(monkeypatch, _reset_refresh_state):
    now = {"t": 1000.0}
    monkeypatch.setattr(trae_shared, "_now", lambda: now["t"])

    trae_shared._mark_refresh_failure("qclaw", 1, now["t"])
    # 第 1 次失败 → 60s
    assert trae_shared._recently_failed("qclaw", 1, 1059.9) is True
    assert trae_shared._recently_failed("qclaw", 1, 1060.1) is False
    # 第 2 次连续失败 → 120s
    trae_shared._mark_refresh_failure("qclaw", 1, 1005.0)
    assert trae_shared._recently_failed("qclaw", 1, 1124.0) is True
    assert trae_shared._recently_failed("qclaw", 1, 1126.0) is False
    # 连续多次失败 → 封顶 600s（前面已记 2 次）
    for _ in range(10):
        trae_shared._mark_refresh_failure("qclaw", 1, 1100.0)
    count, next_try = trae_shared._refresh_failed_at[("qclaw", 1)]
    assert count == 12
    assert next_try - 1100.0 == 600.0
    # 成功清零
    trae_shared._mark_refresh_success("qclaw", 1)
    assert trae_shared._recently_failed("qclaw", 1, 1100.0) is False
    assert ("qclaw", 1) not in trae_shared._refresh_failed_at


def test_pick_fallback_uses_adaptive_interval(monkeypatch, isolated_db, _reset_refresh_state):
    db.add_account({
        "name": "qw", "uid": "qw-1", "provider": "qwenwork",
        "status": "expired", "access_token": "t", "refresh_token": "r",
        "expires_at": 1,
    })
    monkeypatch.setattr(auth_manager, "pick_account", lambda *a, **k: None)
    now = {"t": 1000.0}
    monkeypatch.setattr(trae_shared, "_now", lambda: now["t"])
    calls = []

    async def refresh_fn(row):
        calls.append(row["id"])
        raise RuntimeError("upstream down")

    def attempt():
        return asyncio.run(trae_shared.pick_with_refresh_fallback("qwenwork", refresh_fn))

    assert attempt() is None and len(calls) == 1
    now["t"] = 1050.0                       # 60s 间隔内：不重放
    assert attempt() is None and len(calls) == 1
    now["t"] = 1061.0                       # 60s 间隔过后：重试（第 2 次失败 → 120s）
    assert attempt() is None and len(calls) == 2
    now["t"] = 1170.0                       # 120s 间隔内：不重放
    assert attempt() is None and len(calls) == 2
    now["t"] = 1182.0                       # 120s 间隔过后：重试
    assert attempt() is None and len(calls) == 3


def test_traesolo_refresh_fail_cache_adaptive_interval(monkeypatch, _reset_refresh_state):
    now = {"t": 500.0}
    monkeypatch.setattr(tsc, "_monotonic_now", lambda: now["t"])

    tsc._note_refresh_failure(9)
    assert tsc._refresh_fail_at[9] == (1, 560.0)
    assert tsc._refresh_recently_failed(9, 559.0) is True
    assert tsc._refresh_recently_failed(9, 561.0) is False
    tsc._note_refresh_failure(9)
    assert tsc._refresh_fail_at[9] == (2, 620.0)
    for _ in range(8):
        tsc._note_refresh_failure(9)
    count, next_try = tsc._refresh_fail_at[9]
    assert count == 10
    assert next_try - now["t"] == 600.0
    tsc._note_refresh_success(9)
    assert tsc._refresh_fail_at == {}


# ---------------------------------------------------------------------------
# 1.5 调度同级加权随机决胜
# ---------------------------------------------------------------------------

class _RecordingRng:
    def __init__(self, values=None):
        self.calls = 0
        self.values = list(values or [])

    def random(self):
        self.calls += 1
        if self.values:
            return self.values.pop(0)
        return 0.0


def _add_wb_account(uid, **over):
    row = {
        "name": f"acc-{uid}", "uid": uid, "provider": "workbuddy",
        "status": "active", "access_token": f"tok-{uid}",
        "priority": 5, "weight": 1,
    }
    row.update(over)
    return db.add_account(row)


def test_route_full_tie_uses_weighted_random(monkeypatch, isolated_db):
    a1 = _add_wb_account("u1")
    a2 = _add_wb_account("u2")
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    rng = _RecordingRng()
    monkeypatch.setattr(auth_manager, "_route_rng", rng)

    picked = auth_manager.pick_account(provider="workbuddy")
    assert rng.calls == 1                      # 完全并列 → 加权随机决胜
    assert picked["id"] == a1                  # rng=0.0 → 并列集第一个

    rng.values = [0.999]
    auth_manager._sticky_account_id.clear()
    picked2 = auth_manager.pick_account(provider="workbuddy")
    assert rng.calls == 2
    assert picked2["id"] == a2                 # rng≈1 → 并列集最后一个


def test_route_no_tie_never_consults_rng(monkeypatch, isolated_db):
    a1 = _add_wb_account("busy")
    a2 = _add_wb_account("fresh")
    # total_requests 只能经用量累加产生：给 a1 记一笔请求 → ratio 1/1 vs 0/1
    db.record_request(_log_row(account_id=a1, total_tokens=10, increment_usage=True))
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    rng = _RecordingRng()
    monkeypatch.setattr(auth_manager, "_route_rng", rng)

    picked = auth_manager.pick_account(provider="workbuddy")
    assert rng.calls == 0                      # 非并列：行为与旧实现逐位一致
    assert picked["id"] == a2                  # ratio 0 < 1


def test_weighted_tie_pick_honours_weights(monkeypatch):
    light = {"id": 1, "weight": 1, "priority": 0, "total_requests": 0}
    heavy = {"id": 2, "weight": 3, "priority": 0, "total_requests": 0}
    rng = _RecordingRng()
    monkeypatch.setattr(auth_manager, "_route_rng", rng)

    rng.values = [0.0]
    assert auth_manager._weighted_tie_pick([light, heavy]) is light   # 0×4 < 1
    rng.values = [0.5]
    assert auth_manager._weighted_tie_pick([light, heavy]) is heavy   # 0.5×4=2 ≥ 1
    rng.values = [0.99]
    assert auth_manager._weighted_tie_pick([light, heavy]) is heavy


# ---------------------------------------------------------------------------
# 1.6 服务端杂项
# ---------------------------------------------------------------------------

def test_pool_limits_64_connections_32_keepalive():
    async def run():
        c1 = proxy._get_client()
        c2 = http_pool.get_client()
        return c1, c2

    c1, c2 = asyncio.run(run())
    for client in (c1, c2):
        pool = client._transport._pool
        assert pool._max_connections == 64
        assert pool._max_keepalive_connections == 32


def test_search_logs_default_7d_window(isolated_db):
    now = int(time.time())
    db.record_request(_log_row(provider="workbuddy", model="fresh", created_at=now))
    db.record_request(_log_row(provider="workbuddy", model="stale", created_at=now - 8 * 86400))

    hit = db.search_logs({})
    assert hit["total"] == 1
    assert hit["items"][0]["model"] == "fresh"
    assert hit["window_applied"] == "7d"

    # 显式 start：不强制窗口
    hit2 = db.search_logs({"start": now - 9 * 86400})
    assert hit2["total"] == 2
    assert "window_applied" not in hit2

    # 只给 end（未给 start）也算提供了窗口 → 不应用默认
    hit3 = db.search_logs({"end": now + 60})
    assert hit3["total"] == 2
    assert "window_applied" not in hit3


def test_log_prune_loop_runs_daily_and_swallows_errors(monkeypatch, isolated_db):
    calls = []

    def fake_prune(retention_days=None):
        calls.append(retention_days)
        return 0

    monkeypatch.setattr(db, "prune_logs", fake_prune)
    real_sleep = asyncio.sleep
    ticks = {"n": 0}

    async def fake_sleep(_delay):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(server_mod.asyncio, "sleep", fake_sleep)
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(server_mod._log_prune_loop())
    assert len(calls) == 1                      # 跑了一轮后进入 24h 等待


def test_lifespan_schedules_log_prune(monkeypatch, isolated_db):
    scheduled = []
    monkeypatch.setattr(server_mod, "_schedule_log_prune", lambda: scheduled.append(1))

    async def run():
        async with server_mod._lifespan(server_mod.app):
            pass

    asyncio.run(run())
    assert scheduled == [1]


def test_uvicorn_run_sets_timeout_keep_alive():
    # 配置级契约：uvicorn.run 必须带 timeout_keep_alive=30（源码断言）
    source = open(server_mod.__file__, "r", encoding="utf-8").read()
    assert "timeout_keep_alive=30" in source


def test_static_cache_headers_and_index_no_cache():
    async def run():
        transport = httpx.ASGITransport(app=server_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            static = await client.get("/static/js/api.js")
            index = await client.get("/")
            return static, index

    static, index = asyncio.run(run())
    assert static.status_code == 200
    assert static.headers["cache-control"] == "public, max-age=3600"
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-cache"


# ---------------------------------------------------------------------------
# 1.1 各通道流式打点
# ---------------------------------------------------------------------------

def test_openai_compat_stream_first_token_recorded(monkeypatch, isolated_db):
    provider = OpenAICompatProvider({
        "id": "gmi", "display_name": "GMI", "base_url": "https://gmi.test/v1",
        "models": ["m1"], "aliases": {}, "env_api_key": "", "source": "seed",
        "created_at": 1,
    })
    sse = (
        _chat_chunk({"content": "hi"})
        + _chat_chunk({}, finish="stop")
        + b"data: [DONE]\n\n"
    )
    provider.set_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, content=sse, headers={"content-type": "text/event-stream"}
    )))
    captured = []
    monkeypatch.setattr(provider, "_record", lambda *a, **k: captured.append(k))

    async def run():
        out = []
        async for chunk in provider._stream_chat({"id": 1}, {"model": "m1", "messages": []}, None, "m1"):
            out.append(chunk)
        return out

    out = asyncio.run(run())
    assert any("[DONE]" in c for c in out)
    success = [k for k in captured if k["finish_reason"] == "stop"]
    assert len(success) == 1
    value = success[0]["first_token_ms"]
    assert isinstance(value, int) and value >= 0


def test_openai_compat_stream_error_row_first_token_null(monkeypatch, isolated_db):
    provider = OpenAICompatProvider({
        "id": "gmi", "display_name": "GMI", "base_url": "https://gmi.test/v1",
        "models": ["m1"], "aliases": {}, "env_api_key": "", "source": "seed",
        "created_at": 1,
    })
    provider.set_transport(httpx.MockTransport(
        lambda request: httpx.Response(500, text="boom")
    ))
    captured = []
    monkeypatch.setattr(provider, "_record", lambda *a, **k: captured.append(k))

    async def run():
        out = []
        async for chunk in provider._stream_chat({"id": 1}, {"model": "m1", "messages": []}, None, "m1"):
            out.append(chunk)
        return out

    asyncio.run(run())
    assert captured and captured[0]["finish_reason"] == "error"
    assert captured[0].get("first_token_ms") is None


def test_traesolo_stream_first_token_recorded(monkeypatch):
    tsc.pool._state.clear()
    captured = []

    async def fake_log(*a, **k):
        captured.append((a, k))

    async def fake_pick(_tried):
        return {"id": 3}

    async def fake_pre_refresh(_account):
        return False, "", None

    class _SoloResponse:
        status_code = 200

        def __init__(self, lines):
            self._lines = lines

        async def aiter_lines(self):
            for line in self._lines:
                yield line

        async def aclose(self):
            pass

    class _SoloClient:
        async def aclose(self):
            pass

    happy_lines = [
        "event: output",
        'data: {"response": "hello"}',
        "",
        "event: done",
        'data: {"finish_reason": "stop"}',
        "",
    ]
    error_lines = ["event: error", 'data: {"code": 403, "message": "no"}', ""]
    monkeypatch.setattr(tsc, "_log", fake_log)
    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)

    async def fake_open(_account, _body):
        return _SoloClient(), _SoloResponse(happy_lines)

    monkeypatch.setattr(tsc, "_open", fake_open)

    async def run():
        out = []
        async for chunk in tsc._run_stream({"model": "m"}, "m", "m", None):
            out.append(chunk)
        return out

    asyncio.run(run())
    success = [k for a, k in captured if a[4] == "stop"]
    assert len(success) == 1
    assert isinstance(success[0]["first_token_ms"], int) and success[0]["first_token_ms"] >= 0

    # 流内错误行：first_token_ms 保持 NULL
    captured.clear()

    async def fake_open_err(_account, _body):
        return _SoloClient(), _SoloResponse(error_lines)

    monkeypatch.setattr(tsc, "_open", fake_open_err)
    asyncio.run(run())
    error_rows = [k for a, k in captured if a[4] == "error"]
    assert len(error_rows) == 1 and error_rows[0]["first_token_ms"] is None
    tsc.pool._state.clear()


def test_traework_stream_first_token_via_thinking_cell(monkeypatch):
    cells = []

    async def fake_run_turn(prompt, model, client_model, info, stream=False,
                            on_thinking=None, timeout=90.0):
        cells.append(getattr(on_thinking, "first_token_cell", None))
        if on_thinking is not None:
            await on_thinking("思考片段")
        return "ok", "思考片段"

    monkeypatch.setattr(traework_chat, "_run_turn", fake_run_turn)

    async def run():
        out = []
        async for chunk in traework_chat._stream_chat("hi", "m", "auto", None):
            out.append(chunk)
        return out

    out = asyncio.run(run())
    assert out[0].startswith("data:") and "assistant" in out[0]
    assert any("content" in c for c in out)
    assert cells and isinstance(cells[0].get("ms"), int)


def test_traework_run_turn_fills_first_token_fallback(monkeypatch, isolated_db):
    logged = []

    async def fake_log(*a, **k):
        logged.append((a, k))

    async def fake_pick(_tried):
        return {"id": 1}

    async def fake_turn(account, prompt, model, timeout=90.0, on_thinking=None):
        return "answer"

    def fake_success(_aid):
        pass

    monkeypatch.setattr(traework_chat, "_log", fake_log)
    monkeypatch.setattr(traework_chat, "_pick", fake_pick)
    monkeypatch.setattr(traework_chat, "_turn", fake_turn)
    monkeypatch.setattr(auth_manager, "mark_account_success", fake_success)

    cell = {"t0": time.monotonic()}

    async def on_thinking(_fragment):
        pass

    on_thinking.first_token_cell = cell
    status, result = asyncio.run(
        traework_chat._run_turn("hi", "m", "auto", None, True, on_thinking)
    )
    assert status == "ok" and result == "answer"
    # 无思考片段：_run_turn 在回合结束时补记首帧时刻
    assert isinstance(cell.get("ms"), int)
    assert logged and logged[0][1]["first_token_ms"] == cell["ms"]


def test_traework_log_passes_first_token_and_created_at(isolated_db):
    asyncio.run(traework_chat._log(
        None, None, "qwen-3.7-plus", True, "stop", 200, "", 1_700_000_012.5,
        first_token_ms=77,
    ))
    row = db.list_logs(1)[0]
    assert row["first_token_ms"] == 77
    assert row["created_at"] == 1_700_000_012
    assert row["provider"] == "traework"


def test_traesolo_log_passes_first_token_and_created_at(isolated_db):
    asyncio.run(tsc._log(
        None, None, "glm-5.2", True, "stop", 200, "", 1_700_000_013.5, None,
        first_token_ms=88,
    ))
    row = db.list_logs(1)[0]
    assert row["first_token_ms"] == 88
    assert row["created_at"] == 1_700_000_013
    assert row["provider"] == "traesolo"


def test_qclaw_stream_first_token_recorded(monkeypatch):
    acc = {"id": 1}
    captured = []

    async def fake_log(*a, **k):
        captured.append((a, k))

    def fake_pick(tried, provider=None):
        return None if tried else acc

    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
        "data: [DONE]",
    ]

    class _Resp:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return b"err"

        async def aiter_lines(self):
            for row in self._rows:
                yield row

    class _Client:
        def stream(self, *a, **k):
            return _Resp(lines)

    monkeypatch.setattr(qclaw_chat, "_log", fake_log)
    monkeypatch.setattr(qclaw_chat, "get_client", lambda: _Client())
    monkeypatch.setattr(auth_manager, "pick_account", fake_pick)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_: None)

    async def run():
        out = []
        async for chunk in qclaw_chat._stream({}, "raw", None, "default"):
            out.append(chunk)
        return out

    asyncio.run(run())
    success = [k for a, k in captured if a[7] == "stop"]
    assert len(success) == 1
    assert isinstance(success[0]["first_token_ms"], int) and success[0]["first_token_ms"] >= 0


def test_qclaw_stream_error_row_first_token_null(monkeypatch):
    acc = {"id": 1}
    captured = []

    async def fake_log(*a, **k):
        captured.append((a, k))

    def fake_pick(tried, provider=None):
        return None if tried else acc

    class _Resp:
        status_code = 500

        def __init__(self):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return b"boom"

    class _Client:
        def stream(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(qclaw_chat, "_log", fake_log)
    monkeypatch.setattr(qclaw_chat, "get_client", lambda: _Client())
    monkeypatch.setattr(auth_manager, "pick_account", fake_pick)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_: None)

    async def run():
        out = []
        async for chunk in qclaw_chat._stream({}, "raw", None, "default"):
            out.append(chunk)
        return out

    out = asyncio.run(run())
    assert b"boom" in b"".join(out)
    assert captured and captured[0][0][7] == "error"
    assert captured[0][1].get("first_token_ms") is None


def test_qwenwork_stream_first_token_recorded(monkeypatch):
    acc = {"id": 1, "uid": "u", "access_token": "t"}
    captured = []

    async def fake_log(*a, **k):
        captured.append((a, k))

    async def fake_pick(_tried):
        return acc

    lines = [
        'data: {"object":"chat.completion.chunk",'
        '"choices":[{"index":0,"delta":{"content":"hi"}}]}',
        'data: {"object":"chat.completion.chunk","choices":[],'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
    ]

    class _Resp:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return b"err"

        async def aiter_lines(self):
            for row in self._rows:
                yield row

    class _Client:
        def stream(self, *a, **k):
            return _Resp(lines)

    monkeypatch.setattr(qwenwork_chat, "_log", fake_log)
    monkeypatch.setattr(qwenwork_chat, "get_client", lambda: _Client())
    monkeypatch.setattr(qwenwork_chat, "_pick", fake_pick)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_: None)

    async def run():
        out = []
        async for chunk in qwenwork_chat._stream(
            "raw", "https://q.test/chat", "rid", "qwork-advanced", None, "qwork-advanced"
        ):
            out.append(chunk)
        return out

    out = asyncio.run(run())
    assert b'"content":"hi"' in b"".join(out)
    success = [k for a, k in captured if a[7] == "stop"]
    assert len(success) == 1
    assert isinstance(success[0]["first_token_ms"], int) and success[0]["first_token_ms"] >= 0
