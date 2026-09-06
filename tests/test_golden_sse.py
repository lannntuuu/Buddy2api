"""SSE 出站字节流 golden 特征测试(33 号瘦身方案 §4 安全网)。

冻结 proxy._stream_upstream 在固定 mock 上游下的出站字节流:重构(如 _RetryLog/pump
抽取)必须保持哈希不变。首帧携带 id/created/model,terminal 事件不随机化。
重构期间唯一允许的更新方式:确认 diff 仅为预期语义后,重新记录 EXPECTED_SHA 并在
commit message 中声明。
"""
import asyncio
import hashlib

import upstream.proxy as proxy
from accounts import auth_manager

# 固定 mock 上游:首帧带元数据(防 terminal 事件随机化),含 content delta、
# tool_call delta、finish、usage、[DONE]。
UPSTREAM_CHUNKS = [
    b'data: {"id":"chatcmpl-golden","object":"chat.completion.chunk","created":1700000000,'
    b'"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
    b'data: {"id":"chatcmpl-golden","object":"chat.completion.chunk","created":1700000000,'
    b'"model":"test-model","choices":[{"index":0,"delta":{"content":"hello "},"finish_reason":null}]}\n\n',
    b'data: {"id":"chatcmpl-golden","object":"chat.completion.chunk","created":1700000000,'
    b'"model":"test-model","choices":[{"index":0,"delta":{"content":"world"},"finish_reason":null}]}\n\n',
    b'data: {"id":"chatcmpl-golden","object":"chat.completion.chunk","created":1700000000,'
    b'"model":"test-model","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    b'"id":"call_1","type":"function","function":{"name":"shell","arguments":"{\\"cmd\\":\\"ls\\"}"}}]},"finish_reason":null}]}\n\n',
    b'data: {"id":"chatcmpl-golden","object":"chat.completion.chunk","created":1700000000,'
    b'"model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    b'data: {"id":"chatcmpl-golden","object":"chat.completion.chunk","created":1700000000,'
    b'"model":"test-model","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":34,"total_tokens":46}}\n\n',
    b"data: [DONE]\n\n",
]

BODY = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
INFO = {"id": 9, "name": "golden-key", "_log_model": "test-model", "_bind_channel": "workbuddy"}


class _FakeResponse:
    status_code = 200

    async def aread(self):
        return b""

    async def aiter_bytes(self):
        for chunk in UPSTREAM_CHUNKS:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def stream(self, *args, **kwargs):
        return _FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _collect(monkeypatch) -> bytes:
    account = {"id": 1, "name": "golden-account", "provider": "workbuddy"}
    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", _async_return(account))
    monkeypatch.setattr(auth_manager, "get_valid_headers", _async_return({"Authorization": "Bearer t"}))
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_: None)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda d: 30)
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **k: None)
    monkeypatch.setattr(proxy, "_get_client", lambda: _FakeClient())

    async def collect():
        chunks = []
        async for chunk in proxy._stream_upstream(BODY, INFO, "test-model"):
            chunks.append(chunk)
        return b"".join(chunks)

    return asyncio.run(collect())


# 首次记录后冻结;重构导致哈希变化时,必须逐字节核对 diff 属预期语义并显式重录。
EXPECTED_SHA = "7404bd46444aa24ccdf7bf13ef83d5e7b1d525df1c67864197ca0a4de1045b9d"


def test_golden_sse_bytes_stable(monkeypatch):
    raw = _collect(monkeypatch)
    digest = hashlib.sha256(raw).hexdigest()
    if EXPECTED_SHA == "REPLACE_ME":  # 首次记录模式:打印供 spec 作者落盘
        print("\nGOLDEN_SHA=" + digest)
    assert digest == EXPECTED_SHA, (
        "SSE 出站字节流发生变化:逐字节核对 diff,若属预期语义请重录 EXPECTED_SHA 并在 commit 声明"
    )
