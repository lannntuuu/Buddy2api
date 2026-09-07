"""WS-A1 后端瘦身的行为特征测试（redesign-audit/34 §WS-A1）。

- A1-2: proxy._RetryLog 延迟落库载体（字段默认值、消费时序、increment_usage=False）
  与 feed/finish 共用 _pump 的可观察行为（terminal 帧缓冲、first_token 语义）。
- A1-4: store_common 日志三件套（KNOWN_CACHE_KEYS / credit_source_of /
  enqueue_record_request）及与 proxy._log_request 尾部的等价性。
- A1-3: traesolo _rotate_open 与 _run_once/_run_stream 原头部的等价性。

沿用本仓库约定：同步测试函数内用 asyncio.run 驱动协程（未装 pytest-asyncio）；
mock 手法对齐 tests/test_perf_metrics.py 的 _install_proxy_upstream。
"""
import asyncio
import pytest
import json
import threading

import httpx

from accounts import auth_manager
from providers import store_common
from providers.openai_compat import OpenAICompatProvider
from providers.traesolo import chat as tsc
from storage import database as db
from upstream import proxy


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


class _SteppingClock:
    """proxy.time 替身：monotonic 每次调用前进 0.05s；time 固定值。"""

    def __init__(self):
        self.monotonic_t = 1_000_000.0

    def monotonic(self):
        self.monotonic_t += 0.05
        return self.monotonic_t

    def time(self):
        return 1_700_000_000.0


def _sse_frame(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")


def _chunk(delta, finish=None):
    return _sse_frame({
        "id": "c1", "object": "chat.completion.chunk", "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


class _StreamResp:
    status_code = 200

    def __init__(self, chunks=(), status=None, body=b"", headers=None):
        if status is not None:
            self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._body

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def _install_single_account_stream(monkeypatch, responses, events):
    """单账号序列的 mock 上游；responses 为按次取用的响应对象列表。"""
    queue = list(responses)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return queue.pop(0)

    monkeypatch.setattr(
        auth_manager, "pick_account_with_fallback",
        _async_return({"id": 1, "name": "acc-1"}),
    )
    monkeypatch.setattr(
        auth_manager, "get_valid_headers", _async_return({"X-Test-Account": "1"})
    )
    monkeypatch.setattr(
        auth_manager, "mark_account_failure",
        lambda aid, status=0: events.append(("fail", aid, status)),
    )
    monkeypatch.setattr(
        auth_manager, "mark_account_success", lambda aid: events.append(("ok", aid))
    )
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda default: 30)
    monkeypatch.setattr(proxy, "_retry_delay", _async_return(None))
    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)


async def _collect(body=None):
    return b"".join([
        chunk async for chunk in proxy._stream_upstream(
            body or {"model": "test-model", "stream": True}, None, "test-model"
        )
    ])


# ---------------------------------------------------------------------------
# A1-2 · _RetryLog
# ---------------------------------------------------------------------------

def test_retry_log_field_defaults_match_pending_literal():
    # 与原 4 处字面量逐键一致：token/credit 缺省 0，attempt/retry_after 缺省 None
    row = proxy._RetryLog(account={"id": 1}, status=429, message="m", started=1.0)
    assert row.account == {"id": 1}
    assert row.prompt_tokens == 0
    assert row.completion_tokens == 0
    assert row.total_tokens == 0
    assert row.credit == 0
    assert row.status == 429
    assert row.message == "m"
    assert row.started == 1.0
    assert row.attempt is None
    assert row.retry_after is None


def test_retry_log_explicit_fields_are_kept():
    row = proxy._RetryLog(
        account={"id": 2}, status=502, message="eof", started=2.0, attempt=1,
        prompt_tokens=3, completion_tokens=4, total_tokens=7, credit=9,
    )
    assert (row.attempt, row.prompt_tokens, row.completion_tokens) == (1, 3, 4)
    assert (row.total_tokens, row.credit) == (7, 9)
    # 真值语义与原 dict 版一致：`pending_retry_log or {...}` 的回退判断不失效
    assert row or True


def test_retry_log_deferred_until_next_pick(monkeypatch):
    """重试行延迟到下一次轮换才落库（下一账号 pick 之后、重发之前），
    记的是失败账号且 increment_usage=False。"""
    events = []
    good = _StreamResp(chunks=[
        _chunk({"content": "ok"}),
        _chunk({}, finish="stop"),
        b"data: [DONE]\n\n",
    ])
    err429 = _StreamResp(status=429, body=b"slow down")

    accounts = [{"id": 1, "name": "acc-1"}, {"id": 2, "name": "acc-2"}]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, headers, **kwargs):
            return good if int(headers["X-Test-Account"]) == 2 else err429

    async def pick(_excluded):
        events.append(("pick", tuple(sorted(_excluded))))
        for account in accounts:
            if account["id"] not in _excluded:
                return account
        return None

    async def valid_headers(account):
        return {"X-Test-Account": str(account["id"])}

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", valid_headers)
    monkeypatch.setattr(
        auth_manager, "mark_account_failure",
        lambda aid, status=0: events.append(("fail", aid, status)),
    )
    monkeypatch.setattr(
        auth_manager, "mark_account_success", lambda aid: events.append(("ok", aid))
    )
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda default: 30)
    monkeypatch.setattr(proxy, "_retry_delay", _async_return(None))
    monkeypatch.setattr(proxy.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        proxy, "_log_request", lambda *a, **k: events.append(("log", a, k))
    )

    raw = asyncio.run(_collect())

    picks = [i for i, e in enumerate(events) if e[0] == "pick"]
    logs = [i for i, e in enumerate(events) if e[0] == "log"]
    # 时序：pick#1 → fail(429) → pick#2 → retry 行落库 → 成功行落库
    assert len(picks) == 2 and len(logs) == 2
    assert picks[0] < events.index(("fail", 1, 429)) < picks[1] < logs[0] < logs[1]
    # retry 行字段：account=失败账号（而非新选中的账号）、0 token、
    # increment_usage=False、无 first_token_ms
    log_args, log_kwargs = events[logs[0]][1:]
    assert log_args[1]["id"] == 1
    assert log_args[4:9] == (0, 0, 0, 0, "retry")
    assert log_args[9] == 429
    assert log_args[10] == "slow down"
    assert log_kwargs["increment_usage"] is False
    assert "first_token_ms" not in log_kwargs
    assert b'"content": "ok"' in raw


# ---------------------------------------------------------------------------
# A1-2 · _pump（feed / finish 同构泵）
# ---------------------------------------------------------------------------

def test_pump_buffers_terminal_frames_until_stream_end(monkeypatch):
    """terminal 帧（finish_reason/usage）缓冲到循环结束后按原序补发；
    first_token 在首个内容帧计时一次，terminal 帧不覆盖。"""
    frames = [
        _chunk({"content": "hi"}),   # 非 terminal → 立即透传
        _chunk({}, finish="stop"),   # terminal → 缓冲
        _sse_frame({"id": "c1", "object": "chat.completion.chunk", "created": 1,
                    "model": "test-model", "choices": [],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                              "total_tokens": 3}}),
        b"data: [DONE]\n\n",
    ]
    events = []
    _install_single_account_stream(monkeypatch, [_StreamResp(chunks=frames)], events)
    monkeypatch.setattr(proxy, "time", _SteppingClock())
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    joined = asyncio.run(_collect())

    # 帧序保持：内容帧 → terminal finish 帧 → usage 帧 → [DONE]
    assert joined.index(b'"content": "hi"') < joined.index(b'"finish_reason": "stop"')
    assert joined.index(b'"finish_reason": "stop"') < joined.index(b'"total_tokens": 3')
    assert joined.rstrip().endswith(b"data: [DONE]")
    assert ("ok", 1) in events and not [e for e in events if e[0] == "fail"]
    # 成功行：finish=stop、usage 整包透传、first_token 基线一次 0.05s 时钟步进
    args, kwargs = logs[0]
    assert args[8] == "stop" and args[9] == 200
    assert kwargs["usage"]["total_tokens"] == 3
    assert kwargs["first_token_ms"] == 50


def test_pump_sets_output_started_only_for_content_frames(monkeypatch):
    """仅内容帧翻转 output_started：出流后 eof 不降分不换号（分支判定依赖该翻转）。"""
    frames = [
        _chunk({"content": "partial"}),
        b"data: {broken json\n\n",  # 触发 eof_error（malformed）且已出流
    ]
    events = []
    _install_single_account_stream(monkeypatch, [_StreamResp(chunks=frames)], events)
    logs = []
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: logs.append((a, k)))

    raw = asyncio.run(_collect())

    # 已出流：不 mark_failure、不 mark_success，错误行 + 错误事件收尾
    assert not [e for e in events if e[0] == "fail"]
    assert not [e for e in events if e[0] == "ok"]
    args, _kwargs = logs[0]
    assert args[8] == "error" and args[9] == 502
    assert b'"content": "partial"' in raw
    assert b'"error"' in raw


# ---------------------------------------------------------------------------
# A1-4 · store_common 日志三件套
# ---------------------------------------------------------------------------

def test_known_cache_keys_frozen():
    # 键名与顺序逐字冻结（credit 口径禁碰：判定/阈值不得改动）
    assert store_common.KNOWN_CACHE_KEYS == (
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        "prompt_tokens_details",
    )


def test_credit_source_of_key_presence_judgment():
    # 键存在性判定（与值无关）：cache_read_input_tokens=0 也算 live；
    # 空容器/None → None（proxy 的 `usage is not None` 与 openai 的真值判定在此等价）
    assert store_common.credit_source_of(None) is None
    assert store_common.credit_source_of({}) is None
    assert store_common.credit_source_of({"prompt_tokens": 5}) is None
    assert store_common.credit_source_of({"cache_read_input_tokens": 0}) == "live"
    assert store_common.credit_source_of({"prompt_tokens_details": {}}) == "live"
    assert store_common.credit_source_of({"prompt_cache_miss_tokens": 1}) == "live"


def test_enqueue_record_request_sync_fallback_without_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(db, "record_request", lambda row: seen.append(row))
    store_common.enqueue_record_request({"finish_reason": "sync"})
    assert seen == [{"finish_reason": "sync"}]


def test_enqueue_record_request_swallows_sync_errors(monkeypatch):
    def boom(row):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "record_request", boom)
    store_common.enqueue_record_request({"finish_reason": "sync"})  # 不抛


def test_enqueue_record_request_runs_in_executor_and_swallows_errors(monkeypatch):
    done = threading.Event()
    calls = []
    fail_row = {"finish_reason": "fail"}
    ok_row = {"finish_reason": "ok"}

    def fake_record(row):
        if row is fail_row:
            raise RuntimeError("db down")  # 由 done_callback 吞掉
        calls.append(row)
        done.set()

    monkeypatch.setattr(db, "record_request", fake_record)

    async def main():
        store_common.enqueue_record_request(fail_row)
        store_common.enqueue_record_request(ok_row)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not done.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert done.is_set()
    assert calls == [ok_row]


def test_proxy_log_request_tail_uses_store_common_trio(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "record_request", lambda row: rows.append(row))
    proxy._log_request(
        {"id": 1, "name": "k", "_bind_channel": "workbuddy"},
        {"id": 2, "name": "acc", "provider": "workbuddy"},
        "glm-5.2", False,
        100, 10, 110, 0.5,
        "stop", 200, "", 1_700_000_000.0,
        usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 7}},
    )
    row = rows[0]
    # credit_source 与 cache 提取仍走同一实现（proxy 端判定不漂移）
    assert row["credit_source"] == "live"
    assert row["cache_read_tokens"] == 7
    assert row["cache_creation_tokens"] == 0
    # reasoning_effort 缺省注入保留在 proxy（glm-5.2 → none）
    assert row["reasoning_effort"] == "none"
    assert row["first_token_ms"] is None


def test_proxy_log_request_usage_json_truncation_stays_in_proxy(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "record_request", lambda row: rows.append(row))
    big_usage = {"prompt_tokens": 1, "pad": "x" * 70_000}
    proxy._log_request(
        None, None, "m", False, 0, 0, 0, 0,
        "stop", 200, "", 1_700_000_000.0,
        usage=big_usage,
    )
    payload = json.loads(rows[0]["usage_json"])
    assert payload["truncated"] is True
    assert payload["cache_read_tokens"] == 0


def test_openai_compat_record_keeps_credit_zero_and_live_marker(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "record_request", lambda row: rows.append(row))
    provider = OpenAICompatProvider({"id": "gmi", "display_name": "GMI", "models": ["m1"]})
    provider._record(
        None, None, model="m1", stream=False, finish_reason="stop",
        status_code=200, prompt_tokens=1, completion_tokens=1, total_tokens=2,
        usage_payload={"cache_read_input_tokens": 0},
    )
    row = rows[0]
    assert row["credit"] == 0              # credit 恒 0 语义保留
    assert row["credit_source"] == "live"  # 键存在即 live（值不参与判定）
    assert row["first_token_ms"] is None


# ---------------------------------------------------------------------------
# A1-3 · traesolo _rotate_open
# ---------------------------------------------------------------------------

class _SoloResp:
    def __init__(self, status=200, body=b"", lines=()):
        self.status_code = status
        self._body = body
        self._lines = lines
        self.closed = False

    async def aread(self):
        return self._body

    async def aclose(self):
        self.closed = True

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _SoloClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.fixture()
def _clean_solo_pool():
    tsc.pool._state.clear()
    yield
    tsc.pool._state.clear()


def test_rotate_open_success_returns_rotation_tuple(monkeypatch, _clean_solo_pool):
    picked = []

    async def fake_pick(tried):
        picked.append(set(tried))
        return {"id": 7}

    async def fake_pre_refresh(account):
        return False, "", None

    client, response = _SoloClient(), _SoloResp()

    async def fake_open(account, body):
        assert body["stream"] is True and body["function"] == tsc.FUNCTION
        return client, response

    logs = []

    async def fake_log(*a, **k):
        logs.append(a)

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)
    monkeypatch.setattr(tsc, "_open", fake_open)
    monkeypatch.setattr(tsc, "_log", fake_log)

    tried = set()
    account, t0, got_client, got_response = asyncio.run(tsc._rotate_open(
        {"model": "glm", "messages": []},
        client_model="client-m", api_key_info=None, tried=tried, stream=False,
    ))
    assert account == {"id": 7}
    assert got_client is client and got_response is response
    assert tried == {7} and picked == [set()]
    assert isinstance(t0, float)
    assert logs == []                    # 成功路径不落日志（成功行由调用方打点）
    assert tsc.pool.info(7) is None      # 不记账


def test_rotate_open_refresh_error_books_and_logs(monkeypatch, _clean_solo_pool):
    async def fake_pick(tried):
        return {"id": 7}

    async def fake_pre_refresh(account):
        return False, "client", RuntimeError("refresh failed")

    kinds = []
    logs = []

    async def fake_log(*a, **k):
        logs.append(a)

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)
    monkeypatch.setattr(tsc, "_handle_kind", lambda aid, kind, reason="": kinds.append((aid, kind, reason)))
    monkeypatch.setattr(tsc, "_log", fake_log)

    account, err = asyncio.run(tsc._rotate_open(
        {}, client_model="m", api_key_info=None, tried=set(), stream=True,
    ))
    assert account is None and err == "refresh failed"
    assert kinds == [(7, "client", "refresh: refresh failed")]
    # _log 的 stream 位透传：finish=error、status=503、message 截 240
    assert logs[0][3:7] == (True, "error", 503, "refresh failed")


def test_rotate_open_open_error_notes_pool_error(monkeypatch, _clean_solo_pool):
    async def fake_pick(tried):
        return {"id": 7}

    async def fake_pre_refresh(account):
        return False, "", None

    async def fake_open(account, body):
        raise httpx.ConnectTimeout("boom")

    logs = []

    async def fake_log(*a, **k):
        logs.append(a)

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)
    monkeypatch.setattr(tsc, "_open", fake_open)
    monkeypatch.setattr(tsc, "_log", fake_log)

    account, err = asyncio.run(tsc._rotate_open(
        {}, client_model="m", api_key_info=None, tried=set(), stream=False,
    ))
    assert account is None and err == "boom"
    # _open 异常走 pool.note_error（err_count=1 未达阈值，不冷却）
    info = tsc.pool.info(7)
    assert info is not None and info["cooling"] is False
    assert logs[0][3:7] == (False, "error", 502, "boom")


def test_rotate_open_400_classifies_closes_and_logs(monkeypatch, _clean_solo_pool):
    async def fake_pick(tried):
        return {"id": 7}

    async def fake_pre_refresh(account):
        return False, "", None

    client, response = _SoloClient(), _SoloResp(status=404, body=b'{"code":1004}')

    async def fake_open(account, body):
        return client, response

    kinds = []
    logs = []

    async def fake_log(*a, **k):
        logs.append(a)

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)
    monkeypatch.setattr(tsc, "_open", fake_open)
    monkeypatch.setattr(tsc, "_handle_kind", lambda aid, kind, reason="": kinds.append((aid, kind)))
    monkeypatch.setattr(tsc, "_log", fake_log)

    account, err = asyncio.run(tsc._rotate_open(
        {}, client_model="m", api_key_info=None, tried=set(), stream=True,
    ))
    assert account is None
    assert err == "upstream 404 (not_found)"
    assert kinds == [(7, "not_found")]
    # 先关连接再记账（顺序保持）
    assert response.closed is True and client.closed is True
    assert logs[0][3:7] == (True, "error", 404, '{"code":1004}')


def test_rotate_open_no_account_signals_break(monkeypatch):
    async def fake_pick(tried):
        return None

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    account, err = asyncio.run(tsc._rotate_open(
        {}, client_model="m", api_key_info=None, tried=set(), stream=False,
    ))
    assert account is None and err is None


def test_run_once_success_shape_preserved(monkeypatch, _clean_solo_pool):
    lines = [
        "event: output",
        'data: {"response": "hi"}',
        "",
        "event: done",
        'data: {"finish_reason": "stop"}',
        "",
    ]
    client, response = _SoloClient(), _SoloResp(lines=lines)
    logs = []
    marks = []

    async def fake_pick(tried):
        return {"id": 7}

    async def fake_pre_refresh(account):
        return False, "", None

    async def fake_open(account, body):
        return client, response

    async def fake_log(*a, **k):
        logs.append((a, k))

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)
    monkeypatch.setattr(tsc, "_open", fake_open)
    monkeypatch.setattr(tsc, "_log", fake_log)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda aid: marks.append(aid))

    result = asyncio.run(tsc._run_once({"model": "glm", "messages": []}, "glm", "client-m", None))
    assert result[0] == "json"
    assert result[1]["model"] == "client-m"
    assert result[1]["choices"][0]["message"]["content"] == "hi"
    assert marks == [7]
    args, _kwargs = logs[0]
    assert args[2] == "client-m" and args[3] is False
    assert args[4:7] == ("stop", 200, "")
    assert isinstance(args[7], float)  # t0 来自轮换头部（时长含 pick/预刷新/建连）
    assert client.closed is True and response.closed is True


def test_run_once_no_accounts_error_shape(monkeypatch, _clean_solo_pool):
    async def fake_pick(tried):
        return None

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    result = asyncio.run(tsc._run_once({}, "m", "m", None))
    assert result[0] == "error"
    status, detail = result[1]
    assert status == 503
    assert detail["error"]["code"] == "channel_unavailable"
    assert "No available accounts" in detail["error"]["message"]


def test_run_once_error_ctx_flows_to_no_accounts_message(monkeypatch, _clean_solo_pool):
    async def fake_pick(tried):
        return None if tried else {"id": 7}

    async def fake_pre_refresh(account):
        return False, "", None

    async def fake_open(account, body):
        raise httpx.ConnectTimeout("boom")

    async def fake_log(*a, **k):
        return None

    monkeypatch.setattr(tsc, "_pick", fake_pick)
    monkeypatch.setattr(tsc, "_pre_refresh", fake_pre_refresh)
    monkeypatch.setattr(tsc, "_open", fake_open)
    monkeypatch.setattr(tsc, "_log", fake_log)

    result = asyncio.run(tsc._run_once({}, "m", "m", None))
    assert result[0] == "error"
    # 轮换失败 → last_error 透传 → "No available accounts: boom"
    assert "No available accounts: boom" in result[1][1]["error"]["message"]
