"""WS-B 性能优化条目的回归测试。

覆盖：
- B1  qclaw / qwenwork / traework chat·refresh 热路径复用 storage.http_pool 连接池
- B2  store_common.log_request 收敛实现（worker 线程落库、cache 提取、usage_json 截断）
- B3  trae_shared.pick_with_refresh_fallback（refresh 失败 60s 负缓存）
- B4  qwenwork write_refreshed_auth 落盘移出事件循环
- B5  traework 后台收尾任务 set 引用 + done_callback 自动清理
- B6  is_token_expired / extra_of / make_translator 收敛
- B7  store_common.dedupe_dirs
"""
import asyncio
import json
import threading
import time

import httpx
import pytest

from accounts import auth_manager
from providers import store_common, trae_shared
from providers.qclaw import chat as qclaw_chat
from providers.qclaw import jprx
from providers.qwenwork import chat as qwenwork_chat
from providers.qwenwork import token as qwenwork_token
from providers.traework import chat as traework_chat
from providers.traework import token as traework_token
from storage import database as db


@pytest.fixture(autouse=True)
def _clean_refresh_negative_cache():
    trae_shared.reset_refresh_failures()
    yield
    trae_shared.reset_refresh_failures()


# ---------------------------------------------------------------------------
# 通用 fake 基建
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, lines=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self._lines = lines or []
        self.text = text
        # 模拟上游"有 body"的语义（业务代码用 content 判断是否解析 json）
        self.content = b"{}" if json_data is not None else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    async def aread(self):
        return self.text.encode("utf-8")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakePoolClient:
    """http_pool.get_client() 返回的共享 client 替身（记录调用，绝不新建连接）。"""

    def __init__(self, post_response=None, stream_response=None, get_response=None):
        self.posts = []
        self.gets = []
        self.deletes = []
        self.streams = []
        self.aclose_calls = 0
        self._post_response = post_response
        self._stream_response = stream_response
        self._get_response = get_response

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self._post_response

    async def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self._get_response

    async def delete(self, url, **kwargs):
        self.deletes.append((url, kwargs))
        return FakeResponse(200, {})

    def stream(self, method, url, **kwargs):
        self.streams.append((method, url, kwargs))
        return _FakeStreamCM(self._stream_response)

    async def aclose(self):  # 共享池 client 不允许被业务代码关闭
        self.aclose_calls += 1


def _forbid_fresh_async_client(monkeypatch, *modules):
    """守卫：被测热路径若再退回新建 httpx.AsyncClient 就立刻失败。"""
    def _boom(*args, **kwargs):
        raise AssertionError("hot path must reuse http_pool, not construct httpx.AsyncClient")

    for module in modules:
        if hasattr(module, "httpx"):
            monkeypatch.setattr(module.httpx, "AsyncClient", _boom)


def _add_account(**overrides):
    data = {
        "name": "acc",
        "uid": "u1",
        "access_token": "ak",
        "refresh_token": "rt",
        "provider": "qclaw",
        "status": "active",
    }
    data.update(overrides)
    return db.add_account(data)


# ---------------------------------------------------------------------------
# B1 共享连接池
# ---------------------------------------------------------------------------


def test_qclaw_chat_reuses_shared_pool(monkeypatch, isolated_db):
    _add_account()
    client = FakePoolClient(
        post_response=FakeResponse(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
    )
    monkeypatch.setattr(qclaw_chat, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, qclaw_chat)

    status, data = asyncio.run(
        qclaw_chat.chat_completions({"model": "default", "messages": [{"role": "user", "content": "hi"}]}, None)
    )

    assert status == "json"
    assert data["choices"][0]["message"]["content"] == "pong"
    assert client.posts and client.posts[0][1]["timeout"] == 120.0
    assert client.aclose_calls == 0


def test_qclaw_stream_reuses_shared_pool(monkeypatch, isolated_db):
    _add_account()
    lines = [
        'data: {"choices": [{"index": 0, "delta": {"content": "he"}}]}',
        'data: {"choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}],'
        ' "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}}',
        "data: [DONE]",
    ]
    client = FakePoolClient(stream_response=FakeResponse(200, lines=lines))
    monkeypatch.setattr(qclaw_chat, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, qclaw_chat)

    body, raw = qclaw_chat._build_body({"model": "default", "messages": [{"role": "user", "content": "hi"}]})

    async def consume():
        out = []
        async for chunk in qclaw_chat._stream(body, raw, None, "default"):
            out.append(chunk.decode("utf-8"))
        return out

    chunks = asyncio.run(consume())
    text = "".join(chunks)
    assert '"content": "he"' in text and '"content": "llo"' in text
    assert "data: [DONE]" in text
    assert client.streams and client.streams[0][2]["timeout"] == httpx.Timeout(None, connect=10.0, read=None)
    assert client.aclose_calls == 0


def test_qwenwork_chat_reuses_shared_pool(monkeypatch, isolated_db):
    _add_account(provider="qwenwork")
    lines = [
        'data: {"choices": [{"index": 0, "delta": {"content": "he"}}]}',
        'data: {"choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}],'
        ' "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}}',
    ]
    client = FakePoolClient(stream_response=FakeResponse(200, lines=lines))
    monkeypatch.setattr(qwenwork_chat, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, qwenwork_chat)

    status, data = asyncio.run(
        qwenwork_chat.chat_completions({"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, None)
    )

    assert status == "json"
    assert data["choices"][0]["message"]["content"] == "hello"
    assert client.streams and client.streams[0][2]["timeout"] == 120.0
    assert client.aclose_calls == 0


def test_qwenwork_stream_reuses_shared_pool(monkeypatch, isolated_db):
    _add_account(provider="qwenwork")
    lines = [
        'data: {"choices": [{"index": 0, "delta": {"content": "he"}}]}',
        'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],'
        ' "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}}',
    ]
    client = FakePoolClient(stream_response=FakeResponse(200, lines=lines))
    monkeypatch.setattr(qwenwork_chat, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, qwenwork_chat)

    async def consume():
        out = []
        async for chunk in qwenwork_chat._stream("raw", "http://upstream", "req-1", "auto", None, "auto"):
            out.append(chunk.decode("utf-8"))
        return out

    chunks = asyncio.run(consume())
    text = "".join(chunks)
    assert '"content": "he"' in text
    assert "data: [DONE]" in text
    assert client.streams and client.streams[0][2]["timeout"] == httpx.Timeout(None, connect=10.0, read=None)
    assert client.aclose_calls == 0


def test_traework_turn_reuses_shared_pool(monkeypatch, isolated_db):
    items = {
        "data": {
            "items": [
                {
                    "role": "assistant",
                    "message_type": "task",
                    "content": json.dumps(
                        {"task_id": "t1", "messages": [{"type": "text", "text_content": "pong"}]}
                    ),
                }
            ]
        }
    }
    client = FakePoolClient(
        post_response=FakeResponse(200, {"code": 0, "data": {"chat_session_id": "s1"}}),
        stream_response=FakeResponse(200, lines=["event: done", "data: {}"]),
        get_response=FakeResponse(200, items),
    )
    monkeypatch.setattr(traework_chat, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, traework_chat)

    account = {"id": 1, "access_token": "tk", "extra": {"device_id": "d"}}

    async def scenario():
        text = await traework_chat._turn(account, "hi", "m", timeout=5.0)
        assert text == "pong"
        await asyncio.sleep(0.05)  # 等后台收尾任务跑完
        return text

    assert asyncio.run(scenario()) == "pong"
    # create session / send message 两次 POST 都带 per-request timeout
    assert all(kwargs.get("timeout") == 5.0 for _url, kwargs in client.posts)
    assert client.deletes, "background close should delete the session"
    assert client.aclose_calls == 0, "shared pool client must never be closed by business code"


def test_qclaw_post_cmd_reuses_shared_pool(monkeypatch, isolated_db):
    client = FakePoolClient(
        post_response=FakeResponse(
            200,
            {"ret": 0, "data": {"resp": {"data": {"server_time": "123"}}}},
            headers={"X-New-Token": "nt"},
        )
    )
    monkeypatch.setattr(jprx, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, jprx)

    data, new_token = asyncio.run(jprx.post_cmd(jprx.CMD_TIME_SYNC, {"uid": "1", "refresh_token": "j"}))

    assert data == {"server_time": "123"}
    assert new_token == "nt"
    assert client.posts[0][1]["timeout"] == 30.0
    assert client.aclose_calls == 0


def test_qwenwork_refresh_reuses_shared_pool(monkeypatch, isolated_db):
    aid = _add_account(provider="qwenwork", expires_at=1)
    client = FakePoolClient(
        post_response=FakeResponse(
            200,
            {"device_token": "tok", "refresh_token": "r2", "expires_at": "2099-01-01T00:00:00Z"},
        )
    )
    monkeypatch.setattr(qwenwork_token, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, qwenwork_token)

    fresh = asyncio.run(qwenwork_token.refresh_account(dict(db.get_account(aid))))

    assert fresh["access_token"] == "tok"
    assert client.posts[0][1]["timeout"] == 30.0
    assert client.aclose_calls == 0


def test_traework_refresh_reuses_shared_pool(monkeypatch, isolated_db):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    aid = _add_account(
        provider="traework",
        status="expired",
        extra={"private_key_pem": pem, "device_id": "d", "machine_id": "m"},
    )
    client = FakePoolClient(
        post_response=FakeResponse(
            200,
            {
                "Result": {
                    "Token": "tk",
                    "RefreshToken": "rt2",
                    "TokenExpireAt": "2099-01-01T00:00:00Z",
                    "RefreshExpireAt": "2099-01-02T00:00:00Z",
                }
            },
        )
    )
    monkeypatch.setattr(traework_token, "get_client", lambda: client)
    _forbid_fresh_async_client(monkeypatch, traework_token)

    fresh = asyncio.run(traework_token.refresh_account(dict(db.get_account(aid))))

    assert fresh["access_token"] == "tk"
    assert client.posts[0][1]["timeout"] == 30.0
    assert client.aclose_calls == 0


# ---------------------------------------------------------------------------
# B2 store_common.log_request
# ---------------------------------------------------------------------------


def test_log_request_records_from_worker_thread(monkeypatch, isolated_db):
    captured = {}

    def fake_record(row):
        captured["row"] = dict(row)
        captured["thread"] = threading.get_ident()

    monkeypatch.setattr(db, "record_request", fake_record)
    main_thread = threading.get_ident()
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_cache_hit_tokens": 42,
    }

    asyncio.run(
        store_common.log_request(
            {"id": 5, "name": "k", "_client_tag": "tag", "_client_version": "v1"},
            {"id": 9, "name": "acc"},
            channel="qclaw",
            model="m1",
            stream=True,
            usage=usage,
            finish_reason="stop",
            status_code=200,
            duration_ms=1234,
            error_msg="",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        )
    )

    assert captured["thread"] != main_thread, "sqlite write must run via asyncio.to_thread"
    row = captured["row"]
    assert row["provider"] == "qclaw"
    assert row["model"] == "m1"
    assert row["stream"] == 1
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 50
    assert row["total_tokens"] == 150
    assert row["cache_read_tokens"] == 42
    assert row["cache_creation_tokens"] == 0
    assert row["credit"] == round(150 / 1000.0, 6)
    assert row["increment_usage"] is True
    assert row["client"] == "tag"
    assert row["client_version"] == "v1"
    assert row["api_key_id"] == 5
    assert row["account_id"] == 9
    assert row["duration_ms"] == 1234
    assert json.loads(row["usage_json"]) == usage


def test_log_request_usage_json_truncated(monkeypatch, isolated_db):
    captured = {}

    def fake_record(row):
        captured["row"] = dict(row)

    monkeypatch.setattr(db, "record_request", fake_record)
    usage = {
        "prompt_tokens": 10,
        "total_tokens": 10,
        "prompt_cache_hit_tokens": 3,
        "blob": "x" * 70000,
    }

    asyncio.run(
        store_common.log_request(
            None, None, channel="qwenwork", model="m", stream=False,
            usage=usage, finish_reason="stop", status_code=200, duration_ms=1,
        )
    )

    payload = json.loads(captured["row"]["usage_json"])
    assert payload["truncated"] is True
    assert payload["cache_read_tokens"] == 3
    assert payload["cache_creation_tokens"] == 0
    assert "blob" not in payload


def test_log_request_swallows_db_errors(monkeypatch, isolated_db):
    def boom(row):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "record_request", boom)

    asyncio.run(
        store_common.log_request(
            None, None, channel="traework", model="m", stream=False,
            usage=None, finish_reason="error", status_code=503, duration_ms=1, error_msg="x",
        )
    )
    # 不抛异常即通过（兜底保留，只 debug 记录）


def test_provider_log_wrappers_route_through_shared(monkeypatch):
    calls = []

    async def fake_log_request(api_key_info, account, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(store_common, "log_request", fake_log_request)
    t0 = time.time()

    asyncio.run(qclaw_chat._log(None, None, "m", True, 1, 2, 3, "stop", 200, "", t0, usage={"a": 1}))
    asyncio.run(qwenwork_chat._log(None, None, "m", True, 1, 2, 3, "stop", 200, "", t0, usage={"a": 1}))
    asyncio.run(traework_chat._log(None, None, "m", True, "error", 503, "boom", t0))

    assert calls[0]["channel"] == "qclaw"
    assert calls[0]["prompt_tokens"] == 1 and calls[0]["total_tokens"] == 3
    assert calls[0]["usage"] == {"a": 1}
    assert calls[1]["channel"] == "qwenwork"
    assert calls[2]["channel"] == "traework"
    assert calls[2]["usage"] is None
    assert "prompt_tokens" not in calls[2]
    for call in calls:
        assert call["stream"] is True
        assert call["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# B3 pick_with_refresh_fallback 负缓存
# ---------------------------------------------------------------------------


def test_pick_fallback_returns_active_account_without_refresh(isolated_db):
    aid = _add_account(provider="traework")
    calls = []

    async def refresh_fn(row):
        calls.append(row)
        return row

    result = asyncio.run(trae_shared.pick_with_refresh_fallback("traework", refresh_fn))
    assert result["id"] == aid
    assert not calls


def test_pick_fallback_negative_cache_skips_recent_failures(monkeypatch, isolated_db):
    _add_account(provider="qwenwork", status="expired", expires_at=1)
    monkeypatch.setattr(auth_manager, "pick_account", lambda *a, **k: None)
    calls = []

    async def refresh_fn(row):
        calls.append(row["id"])
        raise RuntimeError("upstream down")

    # 第一次：expired 账号尝试 refresh 失败，负缓存生效
    assert asyncio.run(trae_shared.pick_with_refresh_fallback("qwenwork", refresh_fn)) is None
    assert len(calls) == 1
    # 60s 内的下个请求不再重放失败的 refresh
    assert asyncio.run(trae_shared.pick_with_refresh_fallback("qwenwork", refresh_fn)) is None
    assert len(calls) == 1
    # 清缓存后（模拟 TTL 过期）重新尝试
    trae_shared.reset_refresh_failures()
    assert asyncio.run(trae_shared.pick_with_refresh_fallback("qwenwork", refresh_fn)) is None
    assert len(calls) == 2


def test_pick_fallback_refresh_success_recovers_account(monkeypatch, isolated_db):
    aid = _add_account(provider="traework", status="expired", expires_at=1)
    monkeypatch.setattr(auth_manager, "pick_account", lambda *a, **k: None)

    async def refresh_fn(row):
        db.update_account(row["id"], {"status": "active", "access_token": "fresh"})
        return db.get_account(row["id"])

    result = asyncio.run(trae_shared.pick_with_refresh_fallback("traework", refresh_fn))
    assert result["id"] == aid
    assert result["access_token"] == "fresh"


def test_pick_fallback_refresh_errors_scope(monkeypatch, isolated_db):
    _add_account(provider="traework", status="expired", expires_at=1)
    monkeypatch.setattr(auth_manager, "pick_account", lambda *a, **k: None)

    async def boom(row):
        raise ValueError("not a refresh failure")

    # 默认：所有异常都按刷新失败处理（对齐 qwenwork/traework 旧的裸 except）
    assert asyncio.run(trae_shared.pick_with_refresh_fallback("traework", boom)) is None
    # 收窄后：非指定异常照常向上抛（对齐 qclaw 只认 JprxError 的旧语义）
    trae_shared.reset_refresh_failures()
    with pytest.raises(ValueError):
        asyncio.run(
            trae_shared.pick_with_refresh_fallback("traework", boom, refresh_errors=KeyError)
        )


def test_provider_picks_delegate_to_shared_helper(monkeypatch, isolated_db):
    captured = {}

    async def fake_helper(channel_id, refresh_fn, **kwargs):
        captured["qwenwork"] = (channel_id, kwargs.get("exclude_ids"))
        return "sentinel-qw"

    monkeypatch.setattr(qwenwork_chat, "pick_with_refresh_fallback", fake_helper)
    assert asyncio.run(qwenwork_chat._pick({1, 2})) == "sentinel-qw"
    assert captured["qwenwork"] == ("qwenwork", {1, 2})

    async def fake_helper_tw(channel_id, refresh_fn, **kwargs):
        captured["traework"] = (channel_id, kwargs.get("exclude_ids"))
        return "sentinel-tw"

    monkeypatch.setattr(traework_chat, "pick_with_refresh_fallback", fake_helper_tw)
    assert asyncio.run(traework_chat._pick({3})) == "sentinel-tw"
    assert captured["traework"] == ("traework", {3})


def test_qclaw_fallback_delegates_with_jprx_error_scope(monkeypatch, isolated_db):
    from providers import qclaw as qclaw_pkg
    from providers.qclaw import PROVIDER as qclaw_provider

    captured = {}

    async def fake_helper(channel_id, refresh_fn, **kwargs):
        captured["channel_id"] = channel_id
        captured["refresh_errors"] = kwargs.get("refresh_errors")
        captured["exclude_ids"] = kwargs.get("exclude_ids")
        return None

    monkeypatch.setattr(qclaw_pkg, "pick_with_refresh_fallback", fake_helper)
    assert asyncio.run(qclaw_provider.pick_account_with_fallback({7})) is None
    assert captured["channel_id"] == "qclaw"
    assert captured["refresh_errors"] is jprx.JprxError
    assert captured["exclude_ids"] == {7}


def test_qclaw_fallback_refresh_marks_account_active(monkeypatch, isolated_db):
    from providers.qclaw import PROVIDER as qclaw_provider

    aid = _add_account(provider="qclaw", status="expired")
    calls = []

    async def fake_refresh_channel(row):
        calls.append(row["id"])
        return {"ok": True}

    monkeypatch.setattr(jprx, "refresh_channel", fake_refresh_channel)
    result = asyncio.run(qclaw_provider.pick_account_with_fallback())

    assert result is not None and result["id"] == aid
    assert result["status"] == "active"
    assert db.get_account(aid)["status"] == "active"
    assert calls == [aid]


def test_qclaw_fallback_negative_cache(monkeypatch, isolated_db):
    from providers.qclaw import PROVIDER as qclaw_provider

    aid = _add_account(provider="qclaw", status="expired")
    calls = []

    async def failing_refresh(row):
        calls.append(row["id"])
        raise jprx.JprxError("boom")

    monkeypatch.setattr(jprx, "refresh_channel", failing_refresh)
    # 第一次：refresh 失败进负缓存；60s 内下个请求不重放
    assert asyncio.run(qclaw_provider.pick_account_with_fallback()) is None
    assert asyncio.run(qclaw_provider.pick_account_with_fallback()) is None
    assert calls == [aid]
    # 清缓存（模拟 TTL 过期）后重新尝试
    trae_shared.reset_refresh_failures()
    assert asyncio.run(qclaw_provider.pick_account_with_fallback()) is None
    assert calls == [aid, aid]


# ---------------------------------------------------------------------------
# B4 qwenwork write_refreshed_auth 线程化
# ---------------------------------------------------------------------------


def test_qwenwork_refresh_writes_auth_file_off_event_loop(monkeypatch, isolated_db, tmp_path):
    auth_file = tmp_path / "auth-v2.dat"
    auth_file.write_bytes(b"{}")
    aid = _add_account(provider="qwenwork", expires_at=1, extra={"auth_path": str(auth_file)})
    client = FakePoolClient(
        post_response=FakeResponse(
            200,
            {"device_token": "tok", "refresh_token": "r2", "expires_at": "2099-01-01T00:00:00Z"},
        )
    )
    monkeypatch.setattr(qwenwork_token, "get_client", lambda: client)
    captured = {}

    def fake_write(path, patch):
        captured["thread"] = threading.get_ident()
        captured["path"] = path
        captured["patch"] = patch

    monkeypatch.setattr(qwenwork_token, "write_refreshed_auth", fake_write)
    main_thread = threading.get_ident()

    asyncio.run(qwenwork_token.refresh_account(dict(db.get_account(aid))))

    assert captured["thread"] != main_thread, "encrypted disk write must run via asyncio.to_thread"
    assert captured["path"] == auth_file
    assert captured["patch"]["access_token"] == "tok"


# ---------------------------------------------------------------------------
# B5 traework 后台任务
# ---------------------------------------------------------------------------


def test_traework_bg_close_tasks_tracked_and_discarded(monkeypatch, isolated_db):
    client = FakePoolClient(
        post_response=FakeResponse(200, {"code": 0, "data": {"chat_session_id": "s1"}}),
        stream_response=FakeResponse(200, lines=["event: done", "data: {}"]),
        get_response=FakeResponse(200, {"data": {"items": []}}),
    )
    monkeypatch.setattr(traework_chat, "get_client", lambda: client)

    async def scenario():
        traework_chat._spawn_bg_close(asyncio.sleep(0))
        assert traework_chat._bg_close_tasks, "bg task must be strongly referenced"
        await asyncio.sleep(0.05)
        assert not traework_chat._bg_close_tasks, "done_callback must discard finished tasks"

    asyncio.run(scenario())


def test_traework_stream_chat_creates_turn_via_create_task(monkeypatch):
    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        await asyncio.sleep(0.01)
        return "ok", "pong"

    monkeypatch.setattr(traework_chat, "_run_turn", fake_turn)

    async def consume():
        out = []
        async for chunk in traework_chat._stream_chat("hi", "m", "auto", None):
            out.append(chunk)
        return out

    chunks = asyncio.run(consume())
    assert any('"role": "assistant"' in chunk or "assistant" in chunk for chunk in chunks)
    assert any("stop" in chunk for chunk in chunks)


# ---------------------------------------------------------------------------
# B6 共享工具函数
# ---------------------------------------------------------------------------


def test_shared_is_token_expired_and_extra_of():
    assert qwenwork_token.is_token_expired is trae_shared.is_token_expired
    assert traework_token.is_token_expired is trae_shared.is_token_expired
    assert traework_token.extra_of is trae_shared.extra_of

    assert trae_shared.is_token_expired({"expires_at": 0}) is False
    future_ms = int((time.time() + 3600) * 1000)
    past_ms = int((time.time() - 3600) * 1000)
    assert trae_shared.is_token_expired({"expires_at": future_ms}) is False
    assert trae_shared.is_token_expired({"expires_at": past_ms}) is True
    # 5 分钟 skew：即将过期（< 5min）也算过期
    soon_ms = int((time.time() * 1000) + 60_000)
    assert trae_shared.is_token_expired({"expires_at": soon_ms}) is True

    assert trae_shared.extra_of({"extra": {"a": 1}}) == {"a": 1}
    assert trae_shared.extra_of({}) == {}
    assert trae_shared.extra_of({"extra": "not-a-dict"}) == {}


def test_make_translator_semantics():
    translate = store_common.make_translator(lambda: {"a": "b"}, "fallback")
    assert translate("a") == "b"
    assert translate("zzz") == "zzz"
    assert translate("") == "fallback"
    assert translate(None) == "fallback"


def test_channel_translators_still_resolve_via_alias_table(isolated_db):
    assert qclaw_chat.translate_model("default") == "default"
    assert qwenwork_chat.translate_model("") == "qwork-advanced"
    assert traework_chat.translate_model("auto") == "qwen-3.7-plus"
    assert traework_chat.translate_model("unknown-model") == "unknown-model"


# ---------------------------------------------------------------------------
# B7 dedupe_dirs
# ---------------------------------------------------------------------------


def test_dedupe_dirs_preserves_order(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    assert store_common.dedupe_dirs([a, b, a, a]) == [a, b]
    assert store_common.dedupe_dirs([]) == []
