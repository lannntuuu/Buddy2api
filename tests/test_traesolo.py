"""Trae SOLO 通道单元测试（全部 mock HTTP，不发真实请求）。"""

import asyncio
import json
import time
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest

from accounts import auth_manager
from storage import database as db
import providers
from gateway import router
from providers.protocol import UnknownModel
from providers.traesolo import chat as tsc
from providers.traesolo import login as tlogin
from providers.traesolo import quota as tquota
from providers.traesolo import store as tstore
from providers.traesolo import token as ttoken
from providers.traesolo.constants import CHANNEL_ID, OAUTH_HOST


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def solo_state_reset():
    tlogin.reset()
    with tsc.pool._lock:
        tsc.pool._state.clear()
    with tsc._model_cache.lock:
        tsc._model_cache.ids = []
        tsc._model_cache.fetched_at = 0.0
        tsc._model_cache.last_fail_at = 0.0
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    tsc._TRANSPORT = None
    yield
    tsc._TRANSPORT = None


@pytest.fixture()
def traesolo_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traesolo")
    yield


def add_solo_account(**over):
    base = {
        "name": over.get("name", "solo-1"),
        "uid": over.get("uid", "10001"),
        "nickname": over.get("nickname", "solo-1"),
        "access_token": over.get("access_token", "jwt-old"),
        "refresh_token": over.get("refresh_token", "rt-1"),
        "expires_at": int(time.time() * 1000) + 30 * 86400 * 1000,
        "domain": "trae.cn",
        "provider": CHANNEL_ID,
        "status": "active",
        "priority": over.get("priority", 0),
        "extra": {
            "machine_id": "a" * 32,
            "device_id": "b" * 32,
            "api_host": OAUTH_HOST,
        },
    }
    base.update(over)  # 显式覆盖默认值（token/过期时间等）
    return db.add_account(base)


SSE_OK = (
    "id:1\nevent:metadata\ndata:{\"model\":\"\",\"session_id\":\"s1\",\"prompt_completion_id\":0}\n\n"
    "id:2\nevent:timing_cost\ndata:{\"name\":\"llm_raw_chat_v2\"}\n\n"
    "event:output\ndata:{\"response\":\"Hello\",\"reasoning_content\":\"\"}\n\n"
    "event:output\ndata:{\"response\":\" world\",\"reasoning_content\":\"thinking...\"}\n\n"
    "event:output\ndata:{\"response\":\"\",\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\","
    "\"function_call\":{\"name\":\"finish\",\"arguments\":\"{\\\"su\"}}]}\n\n"
    "event:output\ndata:{\"tool_calls\":[{\"index\":0,\"function_call\":{\"arguments\":\"mmary\\\":\\\"pong\\\"}\"}}]}\n\n"
    "event:token_usage\ndata:{\"prompt_tokens\":21,\"completion_tokens\":142,\"total_tokens\":163,\"reasoning_tokens\":135}\n\n"
    "event:done\ndata:{\"finish_reason\":\"stop\"}\n\n"
).encode("utf-8")

SSE_ERR = 'event:error\ndata:{"code":1005,"message":"plan quota exceeded"}\n\n'.encode("utf-8")


class SoloMock:
    """按路径分发的 MockTransport handler。"""

    def __init__(self):
        self.chat_responses = []  # [(status, bytes), ...] 按调用顺序
        self.chat_call_count = 0
        self.models = ["glm-5.2", "kimi-k3"]  # None → 500
        self.exchange_ok = True
        self.user = {"UserID": "20002", "ScreenName": "mock-user", "EnterpriseID": "ent-1"}

    def handler(self, request):
        path = request.url.path
        if "ExchangeToken" in path:
            if not self.exchange_ok:
                return httpx.Response(401, json={"message": "invalid refresh token"})
            return httpx.Response(
                200,
                json={
                    "Result": {
                        "Token": "jwt-new",
                        "TokenExpireAt": int(time.time() * 1000) + 30 * 86400 * 1000,
                        "TokenExpireDuration": 0,
                        "RefreshToken": "rt-rotated",
                        "RefreshExpireAt": 0,
                    }
                },
            )
        if "GetUserInfo" in path:
            return httpx.Response(200, json={"Result": self.user})
        if path.endswith("/api/agent/v3/llm_utils_chat"):
            self.chat_call_count += 1
            if self.chat_responses:
                status, content = self.chat_responses.pop(0)
            else:
                status, content = 200, SSE_OK
            if status >= 400:
                return httpx.Response(status, content=content)
            return httpx.Response(status, content=content, headers={"content-type": "text/event-stream"})
        if path.endswith("/api/ide/v1/get_detail_param"):
            if self.models is None:
                return httpx.Response(500, text="boom")
            return httpx.Response(
                200, json={"config_info_list": [{"config_name": m} for m in self.models]}
            )
        if path.endswith("/trae/api/v2/pay/ide_user_ent_usage"):
            return httpx.Response(
                200,
                json={
                    "user_entitlement_pack_list": [
                        {
                            "entitlement_base_info": {"quota": {"credits_limit": 1000}},
                            "usage": {"credits_amount": 350},
                        }
                    ]
                },
            )
        if path.endswith("/trae/api/v2/ug/checkin_credits/status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 50, "enable": True})
        if path.endswith("/trae/api/v2/ug/checkin_credits/claim"):
            return httpx.Response(200, json={"credits": 50, "message": "ok"})
        return httpx.Response(404, text="unmocked " + path)


def use_mock(monkeypatch=None, mock=None):
    mock = mock or SoloMock()
    tsc._TRANSPORT = httpx.MockTransport(mock.handler)
    return mock


def _async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 注册 / 路由
# ---------------------------------------------------------------------------

def test_traesolo_in_default_registry(isolated_db, monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    # 隔离真实 DB 里残留的 traesolo 自定义
    db.delete_setting("traesolo.aliases")
    db.delete_setting("traesolo.models")
    assert "traesolo" in providers.enabled_provider_ids()
    p = providers.get_provider("traesolo")
    assert p is not None
    assert p.display_name == "Trae SOLO"
    assert p.checkin_supported is True
    assert len(p.list_models()) == 32
    assert p.alias_map()["auto"] == "glm-5.2"


def test_bind_traesolo(traesolo_enabled, isolated_db):
    bound = router.bind({"model": "traesolo/glm-5.2"}, {"default_channel": "traesolo"})
    assert bound.channel == "traesolo"
    assert bound.inner == "glm-5.2"
    bound = router.bind({"model": "auto"}, {"default_channel": "traesolo"})
    assert bound.inner == "auto"
    with pytest.raises(UnknownModel):
        router.bind({"model": "no-such-model-xyz"}, {"default_channel": "traesolo"})


# ---------------------------------------------------------------------------
# 凭证解析 / 存储
# ---------------------------------------------------------------------------

def test_parse_credentials_nested():
    parsed = tstore.parse_credentials(
        {
            "auth": {
                "accessToken": "jwt-1",
                "refreshToken": "rt-1",
                "expiresAt": 1790000000,
                "domain": "trae.cn",
                "apiHost": "https://api.trae.com.cn",
                "machineId": "m1",
                "deviceId": "d1",
            },
            "account": {"uid": "3577", "enterpriseId": "ent-9", "nickname": "书虫"},
        }
    )
    assert parsed["provider"] == CHANNEL_ID
    assert parsed["access_token"] == "jwt-1"
    assert parsed["refresh_token"] == "rt-1"
    assert parsed["uid"] == "3577"
    assert parsed["expires_at"] == 1790000000 * 1000  # 秒 → 毫秒
    assert parsed["extra"]["machine_id"] == "m1"
    assert parsed["extra"]["api_host"] == "https://api.trae.com.cn"


def test_parse_credentials_flat_ms():
    parsed = tstore.parse_credentials(
        {"accessToken": "a", "refreshToken": "r", "uid": "u1", "expiresAt": 1790000000000, "nickname": "n"}
    )
    assert parsed["access_token"] == "a"
    assert parsed["expires_at"] == 1790000000000  # 已是毫秒
    assert parsed["nickname"] == "n"


def test_parse_credentials_requires_token():
    with pytest.raises(ValueError):
        tstore.parse_credentials({"account": {"uid": "x"}})


def test_upsert_dedupes_by_uid(isolated_db):
    r1 = tstore.upsert_account(tstore.parse_credentials({"accessToken": "a", "uid": "u1"}))
    r2 = tstore.upsert_account(tstore.parse_credentials({"accessToken": "b", "uid": "u1"}))
    assert r1["updated"] is False and r2["updated"] is True
    assert r1["id"] == r2["id"]
    row = db.get_account(r1["id"])
    assert row["access_token"] == "b"


def test_discover_no_dirs(isolated_db, monkeypatch):
    monkeypatch.delenv("CB_TRAESOLO_AUTH_DIR", raising=False)
    d = tstore.discover()
    assert d["file_count"] == 0
    assert d["dirs"] == []


def test_discover_and_import_file(isolated_db, tmp_path, monkeypatch):
    cred = {
        "auth": {
            "accessToken": "jwt-f",
            "refreshToken": "rt-f",
            "expiresAt": 1790000000,
            "domain": "trae.cn",
            "apiHost": OAUTH_HOST,
            "machineId": "mf",
            "deviceId": "df",
        },
        "account": {"uid": "777", "nickname": "file-user"},
    }
    (tmp_path / "trae-777.json").write_text(json.dumps(cred, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CB_TRAESOLO_AUTH_DIR", str(tmp_path))
    d = tstore.discover()
    assert d["file_count"] == 1 and d["valid_count"] == 1
    parsed = tstore.import_discovered(str(tmp_path / "trae-777.json"))
    assert parsed["uid"] == "777"
    assert parsed["access_token"] == "jwt-f"
    with pytest.raises(ValueError):
        outside = tmp_path.parent / "outside.json"
        outside.write_text(json.dumps(cred), encoding="utf-8")
        tstore.import_discovered(str(outside))


# ---------------------------------------------------------------------------
# payload 改写
# ---------------------------------------------------------------------------

def test_prepare_body_basics():
    out = tsc.prepare_body(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": False, "temperature": 0.7}
    )
    assert out["stream"] is True
    assert out["function"] == "solo_work_lite"
    assert out["config_name"] == "glm-5.2"
    assert out["messages"][0]["content"] == [{"type": "text", "text": "hi"}]
    assert out["temperature"] == 0.7


def test_prepare_body_default_model():
    out = tsc.prepare_body({"messages": [{"role": "user", "content": "hi"}]})
    assert out["model"] == "glm-5.2"
    assert out["config_name"] == "glm-5.2"


def test_prepare_body_tools_and_choice():
    out = tsc.prepare_body(
        {
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "description": "",
                        "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "finish"}},
        }
    )
    assert out["tool_choice"] == "finish"
    params = out["tools"][0]["function"]["parameters"]
    assert isinstance(params, str)
    assert json.loads(params)["properties"]["a"]["type"] == "string"


def test_prepare_body_tool_choice_none():
    out = tsc.prepare_body(
        {
            "model": "glm-5.2",
            "messages": [],
            "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}],
            "tool_choice": "none",
        }
    )
    assert "tool_choice" not in out
    assert "tools" not in out
    assert "functions" not in out


def test_prepare_body_assistant_tool_calls():
    out = tsc.prepare_body(
        {
            "model": "glm-5.2",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "1", "type": "function", "function": {"name": "finish", "arguments": "{}"}},
                        {"id": "2", "type": "function", "function": {"name": "", "arguments": "{}"}},
                    ],
                }
            ],
        }
    )
    tc = out["messages"][0]["tool_calls"]
    assert len(tc) == 1  # 无 name 的条目被剔除
    assert tc[0]["function_call"]["name"] == "finish"
    assert "function" not in tc[0]


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

def test_classify():
    assert tsc.classify(200, '{"code":1005,"message":"plan"}') == "plan_limit"
    assert tsc.classify(401, "unauthorized") == "session_dead"
    assert tsc.classify(429, "too many requests") == "soft_rate"
    assert tsc.classify(404, "nf") == "not_found"
    assert tsc.classify(502, "bad gateway") == "server"
    assert tsc.classify(400, "bad request") == "client"
    assert tsc.classify(200, "ok") == "none"


# ---------------------------------------------------------------------------
# SSE 聚合 / 流式转换
# ---------------------------------------------------------------------------

def _line_gen(sse: bytes):
    async def gen():
        for line in sse.decode("utf-8").split("\n"):
            yield line
    return gen()


def test_aggregate_sse_full():
    data, err = _async(tsc.aggregate_lines(_line_gen(SSE_OK)))
    assert err is None
    msg = data["choices"][0]["message"]
    assert msg["content"] == "Hello world"
    assert msg["reasoning_content"] == "thinking..."
    assert msg["tool_calls"][0]["function"]["name"] == "finish"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"summary":"pong"}'
    assert data["usage"]["total_tokens"] == 163
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["object"] == "chat.completion"


def test_aggregate_sse_error():
    data, err = _async(tsc.aggregate_lines(_line_gen(SSE_ERR)))
    assert data is None
    assert isinstance(err, tsc.SoloStreamError)
    assert err.code == 1005
    assert err.kind() == "plan_limit"


def test_stream_to_openai():
    chunks = []

    async def run():
        sink = {}
        async for c in tsc.stream_to_openai(_line_gen(SSE_OK), "cid-1", "glm-5.2", usage_sink=sink):
            chunks.append(c)
        return sink

    sink = _async(run())
    assert chunks[-1] == "data: [DONE]\n\n"
    data_chunks = [c for c in chunks if c.startswith("data: ") and "[DONE]" not in c]
    first = json.loads(data_chunks[0][6:])
    assert first["object"] == "chat.completion.chunk"
    assert first["model"] == "glm-5.2"
    assert first["choices"][0]["delta"]["content"] == "Hello"
    tc_chunks = [json.loads(c[6:]) for c in data_chunks if "tool_calls" in c]
    assert tc_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "finish"
    last = json.loads(data_chunks[-1][6:])
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"]["total_tokens"] == 163
    assert sink["usage"]["total_tokens"] == 163


def test_stream_to_openai_instream_error():
    on_error = []
    chunks = []

    async def run():
        async for c in tsc.stream_to_openai(
            _line_gen(SSE_ERR), "cid-2", "glm-5.2", on_error=lambda se: on_error.append(se)
        ):
            chunks.append(c)

    _async(run())
    assert len(on_error) == 1 and on_error[0].code == 1005
    assert chunks[-1] == "data: [DONE]\n\n"
    assert any(c.startswith("event: error") for c in chunks)


def test_stream_to_openai_eof_without_done():
    chunks = []

    async def run():
        async def gen():
            yield "event:output"
            yield 'data:{"response":"only"}'
            yield ""

        async for c in tsc.stream_to_openai(gen(), "cid-3", "glm-5.2"):
            chunks.append(c)

    _async(run())
    assert chunks[-1] == "data: [DONE]\n\n"
    assert any("only" in c for c in chunks)


# ---------------------------------------------------------------------------
# 模型映射 / 动态模型表
# ---------------------------------------------------------------------------

def test_translate_and_accepts(isolated_db):
    # 清掉真实 DB 里残留的 traesolo 自定义，让该用例只看内置默认
    db.delete_setting("traesolo.aliases")
    db.delete_setting("traesolo.models")
    assert tsc.translate_model("auto") == "glm-5.2"
    assert tsc.accepts_model("glm-5.2")
    assert tsc.accepts_model("auto")
    assert tsc.accepts_model("DeepSeek-V4-Flash-Official")
    assert tsc.accepts_model("deepseek_v4_flash_official")  # 下划线宽松匹配
    assert not tsc.accepts_model("no-such-model")


def test_dynamic_models_refresh(isolated_db):
    mock = use_mock()
    mock.models = ["glm-5.2", "brand-new-model"]
    add_solo_account()
    assert _async(tsc.refresh_dynamic_models()) is True
    assert "brand-new-model" in tsc.dynamic_model_ids()
    assert tsc.accepts_model("brand-new-model")
    assert "brand-new-model" in [m["id"] for m in providers.get_provider(CHANNEL_ID).list_models()]


def test_dynamic_models_negative_cache(isolated_db):
    mock = use_mock()
    mock.models = None  # 上游 500
    add_solo_account()
    assert _async(tsc.refresh_dynamic_models()) is False
    # 5 分钟负缓存内不重试（即使上游已恢复）
    mock.models = ["glm-5.2"]
    assert _async(tsc.refresh_dynamic_models()) is False
    assert tsc.dynamic_model_ids() == []


def test_dynamic_models_no_account(isolated_db):
    mock = use_mock()
    assert _async(tsc.refresh_dynamic_models()) is False
    # 无账号时静态表兜底
    assert tsc.accepts_model("glm-5.2")


def test_chat_kicks_dynamic_models(isolated_db):
    mock = use_mock()
    mock.models = ["glm-5.2", "kicked-model"]
    add_solo_account()

    async def run():
        kind, _body = await tsc.chat_completions(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None
        )
        # 让后台任务跑完
        task = tsc._dynamic_task
        if task is not None:
            await task
        return kind

    assert _async(run()) == "json"
    assert "kicked-model" in tsc.dynamic_model_ids()


# ---------------------------------------------------------------------------
# 登录闭环
# ---------------------------------------------------------------------------

def test_build_login_url_and_parse_callback_roundtrip():
    m, d = "c" * 32, "e" * 32
    url = ttoken.build_login_url(m, d, "http://127.0.0.1:8787/authorize")
    assert url.startswith("https://www.trae.cn/authorization?")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"][0] == "en1oxy7wnw8j9n"
    assert q["login_trace_id"][0] == ttoken.machine_trace_id(m, d)
    assert q["auth_callback_url"][0] == "http://127.0.0.1:8787/authorize"
    assert q["machine_id"][0] == m

    info = {"UserID": "3577", "ScreenName": "书虫", "TenantID": "ent-9"}
    jwt = {"Token": "jwt-fallback", "RefreshToken": "rt-cb", "TokenExpireAt": 1790000000000}
    cb = (
        "http://127.0.0.1:8787/authorize?refreshToken=rt-cb"
        "&userInfo=" + quote(json.dumps(info, ensure_ascii=False))
        + "&userJwt=" + quote(json.dumps(jwt))
        + "&loginTraceID=" + ttoken.machine_trace_id(m, d)
    )
    parsed = ttoken.parse_callback(cb)
    assert parsed["refresh_token"] == "rt-cb"
    assert parsed["uid"] == "3577"
    assert parsed["nickname"] == "书虫"
    assert parsed["enterprise_id"] == "ent-9"
    assert parsed["login_trace_id"] == ttoken.machine_trace_id(m, d)


def test_parse_callback_userjwt_fallback():
    jwt = {"Token": "jwt-only", "TokenExpireAt": 1790000000000}
    cb = "http://127.0.0.1:8787/authorize?userJwt=" + quote(json.dumps(jwt))
    parsed = ttoken.parse_callback(cb)
    assert parsed["access_token"] == "jwt-only"
    assert parsed["refresh_token"] == ""
    assert parsed["expires_at"] == 1790000000000


def test_parse_callback_query_only():
    cb = "refreshToken=rt-q&userInfo=" + quote(json.dumps({"UserID": "q1"}))
    parsed = ttoken.parse_callback(cb)
    assert parsed["refresh_token"] == "rt-q"
    assert parsed["uid"] == "q1"


def test_parse_callback_empty():
    with pytest.raises(ValueError):
        ttoken.parse_callback("   ")


def test_login_loop_e2e(isolated_db):
    use_mock()
    provider = providers.get_provider(CHANNEL_ID)
    started = provider.start_login("http://127.0.0.1:8787")
    assert started["login_url"].startswith("https://www.trae.cn/authorization?")
    assert started["callback_url"] == "http://127.0.0.1:8787/authorize"
    pending_id = started["pending_id"]
    assert provider.login_result(pending_id)["state"] == "pending"

    q = parse_qs(urlparse(started["login_url"]).query)
    trace = q["login_trace_id"][0]
    cb = (
        "http://127.0.0.1:8787/authorize?refreshToken=rt-cb&userInfo="
        + quote(json.dumps({"UserID": "3577", "ScreenName": "书虫", "TenantID": "ent-9"}))
        + "&loginTraceID=" + trace
    )
    done = _async(provider.complete_login_callback(cb))
    assert done["ok"] is True
    assert done["uid"] == "20002"  # GetUserInfo 覆盖回调里的 uid
    assert done["nickname"] == "书虫"

    res = provider.login_result(pending_id)
    assert res["found"] and res["state"] == "success"
    assert res["uid"] == "20002"

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row["access_token"] == "jwt-new"
    assert row["refresh_token"] == "rt-rotated"
    assert row["extra"]["machine_id"] == q["machine_id"][0]
    assert row["extra"]["source"] == "web_login"


def test_login_loop_exchange_failure(isolated_db):
    mock = use_mock()
    mock.exchange_ok = False
    provider = providers.get_provider(CHANNEL_ID)
    started = provider.start_login("http://127.0.0.1:8787")
    q = parse_qs(urlparse(started["login_url"]).query)
    cb = (
        "http://127.0.0.1:8787/authorize?refreshToken=rt-cb"
        + "&loginTraceID=" + q["login_trace_id"][0]
    )
    done = _async(provider.complete_login_callback(cb))
    assert done["ok"] is False
    assert "401" in done["error"]
    res = provider.login_result(started["pending_id"])
    assert res["state"] == "failed"
    assert db.list_accounts(provider=CHANNEL_ID) == []


def test_manual_callback_without_pending(isolated_db):
    use_mock()
    cb = "http://elsewhere/authorize?refreshToken=rt-x&userInfo=" + quote(
        json.dumps({"UserID": "999", "ScreenName": "manual"})
    )
    done = _async(tlogin.complete_from_callback(cb))
    assert done["ok"] is True
    assert done["uid"] == "20002"
    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 1


def test_login_cancel_and_ttl():
    provider = providers.get_provider(CHANNEL_ID)
    started = provider.start_login("http://127.0.0.1:8787")
    assert provider.cancel_login(started["pending_id"])["canceled"] is True
    assert provider.login_result(started["pending_id"])["found"] is False
    assert provider.cancel_login("deadbeefdeadbeef")["canceled"] is False


# ---------------------------------------------------------------------------
# token 刷新
# ---------------------------------------------------------------------------

def test_refresh_account(isolated_db):
    use_mock()
    aid = add_solo_account(access_token="jwt-old", expires_at=int(time.time() * 1000) + 1000)
    acc = db.get_account(aid)
    fresh = _async(ttoken.refresh_account(acc))
    assert fresh["access_token"] == "jwt-new"
    row = db.get_account(aid)
    assert row["refresh_token"] == "rt-rotated"
    assert row["status"] == "active"


def test_is_token_expired_semantics():
    now_ms = int(time.time() * 1000)
    assert ttoken.is_token_expired({"expires_at": 0}) is True  # 无过期信息 → 过期
    assert ttoken.is_token_expired({"expires_at": now_ms + 3600 * 1000}) is False
    assert ttoken.is_token_expired({"expires_at": now_ms + 1000}) is True
    assert ttoken.needs_pre_refresh({"expires_at": now_ms + 12 * 3600 * 1000}) is True  # 24h 窗口


def test_headers():
    acc = {
        "access_token": "jwt-1",
        "uid": "42",
        "extra": {"machine_id": "m1", "device_id": "d1"},
    }
    h = ttoken.solo_headers(acc, stream=True)
    assert h["Authorization"] == "Cloud-IDE-JWT jwt-1"
    assert h["X-Cloudide-Token"] == "jwt-1"
    assert h["X-Ide-Token"] == "jwt-1"
    assert h["X-Machine-Id"] == "m1"
    assert h["X-Device-Id"] == "d1"
    assert h["X-Uid"] == "42"
    assert h["Accept"] == "text/event-stream"
    h2 = ttoken.solo_headers(acc, stream=False)
    assert h2["Accept"] == "application/json"
    ug = ttoken.ug_headers(acc)
    assert ug["Authorization"] == "Cloud-IDE-JWT jwt-1"
    assert ug["X-User-Region"] == "CN"


# ---------------------------------------------------------------------------
# chat_completions（mock 上游）
# ---------------------------------------------------------------------------

def test_chat_completions_non_stream(isolated_db):
    mock = use_mock()
    add_solo_account()
    payload = {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    kind, body = _async(tsc.chat_completions(payload, {"id": 1, "name": "key"}))
    assert kind == "json"
    assert body["choices"][0]["message"]["content"] == "Hello world"
    assert body["model"] == "glm-5.2"
    assert body["usage"]["total_tokens"] == 163
    assert mock.chat_call_count == 1
    logs = db.list_logs(10)
    solo_logs = [row for row in logs if row.get("provider") == CHANNEL_ID]
    assert len(solo_logs) == 1
    assert solo_logs[0]["total_tokens"] == 163


def test_chat_completions_unknown_model(isolated_db):
    use_mock()
    kind, err = _async(
        tsc.chat_completions({"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]}, None)
    )
    assert kind == "error"
    assert err[0] == 400


def test_chat_rotation_on_429(isolated_db):
    mock = use_mock()
    add_solo_account(name="a", uid="111", priority=1)
    add_solo_account(name="b", uid="222", priority=0)
    mock.chat_responses = [(429, b"too many requests"), (200, SSE_OK)]
    kind, body = _async(
        tsc.chat_completions({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None)
    )
    assert kind == "json"
    assert mock.chat_call_count == 2  # 第一个 429 换号后成功


def test_chat_plan_limit_cooldown(isolated_db):
    mock = use_mock()
    a = add_solo_account(name="a", uid="111")
    mock.chat_responses = [(400, b'{"code":1005,"message":"plan insufficient"}')]
    kind, err = _async(
        tsc.chat_completions({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None)
    )
    assert kind == "error"
    assert err[0] == 503
    assert tsc.pool.cooling(a)
    info = tsc.pool.info(a)
    assert info["kind"] == "plan"
    assert info["remaining_s"] > 11 * 3600


def test_chat_no_accounts(isolated_db):
    use_mock()
    kind, err = _async(
        tsc.chat_completions({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None)
    )
    assert kind == "error"
    assert err[0] == 503
    assert "No available accounts" in err[1]["error"]["message"]


def test_chat_pre_refresh(isolated_db):
    use_mock()
    # token 1 小时后过期 → 落入 24h 预刷新窗口
    add_solo_account(access_token="jwt-old", expires_at=int(time.time() * 1000) + 3600 * 1000)
    kind, _body = _async(
        tsc.chat_completions({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None)
    )
    assert kind == "json"
    row = db.list_accounts(provider=CHANNEL_ID)[0]
    assert row["access_token"] == "jwt-new"  # 对话前已刷新


def test_chat_stream(isolated_db):
    mock = use_mock()
    add_solo_account()

    async def run():
        kind, gen = await tsc.chat_completions(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True}, None
        )
        assert kind == "stream"
        out = []
        async for chunk in gen:
            out.append(chunk)
        return out

    out = _async(run())
    assert out[-1] == "data: [DONE]\n\n"
    joined = "".join(out)
    assert "Hello" in joined
    assert "chat.completion.chunk" in joined
    assert mock.chat_call_count == 1


def test_chat_stream_instream_error(isolated_db):
    mock = use_mock()
    a = add_solo_account()
    mock.chat_responses = [(200, SSE_ERR)]

    async def run():
        kind, gen = await tsc.chat_completions(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True}, None
        )
        return [c async for c in gen]

    out = _async(run())
    assert out[-1] == "data: [DONE]\n\n"
    assert any(c.startswith("event: error") for c in out)
    assert tsc.pool.cooling(a)  # 1005 → 12h plan 冷却


def test_chat_stream_fallback_error_event(isolated_db):
    mock = use_mock()
    # 所有账号在流开始前都失败
    mock.chat_responses = [(500, b"upstream down"), (500, b"upstream down"), (500, b"upstream down")]
    add_solo_account(name="a", uid="111")

    async def run():
        kind, gen = await tsc.chat_completions(
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": True}, None
        )
        return [c async for c in gen]

    out = _async(run())
    assert out[-1] == "data: [DONE]\n\n"
    assert any(c.startswith("event: error") for c in out)


def test_test_chat(isolated_db):
    use_mock()
    acc = db.get_account(add_solo_account())
    r = _async(tsc.test_chat(acc, "glm-5.2", "ping"))
    assert r["ok"] is True
    assert r["status_code"] == 200
    assert "Hello" in r["message"]


def test_test_chat_failure(isolated_db):
    mock = use_mock()
    mock.chat_responses = [(401, b"unauthorized")]
    acc = db.get_account(add_solo_account())
    r = _async(tsc.test_chat(acc, "glm-5.2", "ping"))
    assert r["ok"] is False
    assert r["status_code"] == 401


# ---------------------------------------------------------------------------
# Credit 估算（token → credit 换算率）
# ---------------------------------------------------------------------------

def test_log_estimates_credit_from_tokens(isolated_db):
    use_mock()
    aid = add_solo_account()
    acc = db.get_account(aid)
    # 默认换算率 1000 token / 1 credit
    usage = {"prompt_tokens": 100, "completion_tokens": 400, "total_tokens": 500}
    tsc._log(None, acc, "glm-5.2", False, "stop", 200, "", time.time() - 0.1, usage)
    logs = db.list_logs(5)
    row = next(r for r in logs if r.get("provider") == CHANNEL_ID)
    assert row["total_tokens"] == 500
    assert abs(row["credit"] - 0.5) < 1e-6  # 500 / 1000
    # 累计到账号
    fresh = db.get_account(aid)
    assert abs(fresh["total_credits"] - 0.5) < 1e-6


def test_log_honors_credit_rate_setting(isolated_db):
    use_mock()
    aid = add_solo_account()
    acc = db.get_account(aid)
    # 改换算率到 200 token / 1 credit
    db.set_setting("traesolo.credit_rate", 200.0)
    usage = {"prompt_tokens": 50, "completion_tokens": 150, "total_tokens": 200}
    tsc._log(None, acc, "glm-5.2", False, "stop", 200, "", time.time() - 0.1, usage)
    logs = db.list_logs(5)
    row = next(r for r in logs if r.get("provider") == CHANNEL_ID)
    assert row["total_tokens"] == 200
    assert abs(row["credit"] - 1.0) < 1e-6  # 200 / 200 = 1


def test_log_zero_rate_no_estimate(isolated_db):
    """换算率设为 0 → 不做估算（credit=0），保持原行为。"""
    use_mock()
    aid = add_solo_account()
    acc = db.get_account(aid)
    db.set_setting("traesolo.credit_rate", 0)
    usage = {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}
    tsc._log(None, acc, "glm-5.2", False, "stop", 200, "", time.time() - 0.1, usage)
    logs = db.list_logs(5)
    row = next(r for r in logs if r.get("provider") == CHANNEL_ID)
    assert row["total_tokens"] == 200
    assert row["credit"] == 0


# ---------------------------------------------------------------------------
# 配额 / 签到
# ---------------------------------------------------------------------------

def test_fetch_quota(isolated_db):
    use_mock()
    acc = db.get_account(add_solo_account())
    snap = _async(tquota.fetch_quota(acc))
    assert snap.ok is True
    assert snap.unit == "credit"
    assert snap.remaining == 650.0  # 1000 - 350
    assert snap.extra["packs"] == 1
    assert snap.unsupported is False


def test_fetch_checkin_and_claim(isolated_db):
    use_mock()
    acc = db.get_account(add_solo_account())
    st = _async(tquota.fetch_checkin(acc))
    assert st["ok"] is True
    assert st["already_claimed"] is False
    assert st["credit"] == 50
    assert st["channel"] == CHANNEL_ID
    cl = _async(tquota.claim_checkin(acc))
    assert cl["ok"] is True
    assert cl["claimed"] is True
    assert cl["credit"] == 50


def test_claim_checkin_already_claimed(isolated_db):
    mock = use_mock()

    def handler(request):
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(200, json={"checked_in": True, "credits": 50, "enable": True})
        return httpx.Response(404, text="unmocked")

    tsc._TRANSPORT = httpx.MockTransport(handler)
    acc = db.get_account(add_solo_account())
    cl = _async(tquota.claim_checkin(acc))
    assert cl["ok"] is True
    assert cl["already_claimed"] is True
    assert cl["message"] == "今日已领取"


# ---------------------------------------------------------------------------
# provider 协议集成
# ---------------------------------------------------------------------------

def test_provider_has_usable_account(isolated_db):
    use_mock()
    add_solo_account()
    provider = providers.get_provider(CHANNEL_ID)
    assert _async(provider.has_usable_account()) is True
    db.update_account(db.list_accounts(provider=CHANNEL_ID)[0]["id"], {"status": "inactive"})
    assert _async(provider.has_usable_account()) is False
