"""WS-C 性能条目回归测试（traesolo + openai_compat）。

覆盖 redesign-audit/30-perf-optimization-spec.md 的 C1-C5：
  C1  traesolo _log 经 asyncio.to_thread 落库（协程化）
  C2  _pick 过期账号 refresh 失败的 60s 负缓存（失败不重放）
  C3  fetch_models 删除 / token.py 类型化 httpx / 签到走 ug_host 覆盖
  C4  openai_compat SSE 透传的 usage/finish_reason 预过滤（普通帧不 json.loads）
  C5  refresh_model_ids stale-while-revalidate + ensure_env_account 实例级 memo

全部 mock HTTP，不发真实请求；不触碰既有测试文件。
"""

import asyncio
import inspect
import time
import types
import uuid
from pathlib import Path

import httpx
import pytest

from accounts import auth_manager
from storage import database as db
import providers.openai_compat as oc
from providers.openai_compat import OpenAICompatProvider
from providers.traesolo import chat as tsc
from providers.traesolo import quota as tquota
from providers.traesolo import token as ttoken
from providers.traesolo.constants import CHANNEL_ID


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _solo_state_reset():
    """隔离 traesolo 进程级状态（模式同 tests/test_traesolo.py 的 solo_state_reset）。"""
    with tsc.pool._lock:
        tsc.pool._state.clear()
    with tsc._model_cache.lock:
        tsc._model_cache.ids = []
        tsc._model_cache.details = []
        tsc._model_cache.fetched_at = 0.0
        tsc._model_cache.last_fail_at = 0.0
    tsc._reset_refresh_fail_cache()
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    tsc._TRANSPORT = None
    yield
    tsc._reset_refresh_fail_cache()
    tsc._TRANSPORT = None


def _add_solo_account(**over):
    base = {
        "name": over.get("name", "solo-perf"),
        "uid": over.get("uid", "90001"),
        "nickname": "solo-perf",
        "access_token": over.get("access_token", "jwt-old"),
        "refresh_token": over.get("refresh_token", "rt-perf"),
        "expires_at": over.get("expires_at", int(time.time() * 1000) + 30 * 86400 * 1000),
        "domain": "trae.cn",
        "provider": CHANNEL_ID,
        "status": over.get("status", "active"),
        "extra": {
            "machine_id": "a" * 32,
            "device_id": "b" * 32,
            "api_host": "https://api.trae.com.cn",
        },
    }
    base.update(over)
    return db.add_account(base)


def _make_compat_provider(**over):
    definition = {
        "id": over.get("id", "perfchan"),
        "display_name": "Perf Channel",
        "base_url": over.get("base_url", "http://mock.test/v1"),
        "models": over.get("models", ["m1"]),
        "aliases": over.get("aliases", {"auto": "m1"}),
        "env_api_key": over.get("env_api_key", ""),
    }
    return OpenAICompatProvider(definition)


# ---------------------------------------------------------------------------
# C1：_log 协程化 + to_thread 落库
# ---------------------------------------------------------------------------


def test_log_is_coroutine_and_writes_row(isolated_db):
    aid = _add_solo_account()
    acc = db.get_account(aid)
    assert inspect.iscoroutinefunction(tsc._log)
    asyncio.run(
        tsc._log(
            {"id": 1, "name": "k"},
            acc,
            "glm-5.2",
            True,
            "stop",
            200,
            "",
            time.time() - 0.05,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
    )
    logs = db.list_logs(10)
    row = next(r for r in logs if r.get("provider") == CHANNEL_ID)
    assert row["total_tokens"] == 30
    assert row["finish_reason"] == "stop"
    assert row["status_code"] == 200
    # 累计到账号（record_request 的 increment_usage 分支）
    fresh = db.get_account(aid)
    assert fresh["total_requests"] == 1


# ---------------------------------------------------------------------------
# C2：_pick 过期账号 refresh 失败负缓存
# ---------------------------------------------------------------------------


class _ExchangeMock:
    def __init__(self, exchange_ok: bool = True):
        self.exchange_calls = 0
        self.exchange_ok = exchange_ok

    def handler(self, request):
        if "ExchangeToken" in request.url.path:
            self.exchange_calls += 1
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
        return httpx.Response(404, text="unmocked " + request.url.path)


def test_pick_expired_refresh_negative_cache(isolated_db):
    mock = _ExchangeMock(exchange_ok=False)
    tsc._TRANSPORT = httpx.MockTransport(mock.handler)
    _add_solo_account(status="expired", uid="90002", name="expired-1")

    # 第一次：refresh 尝试一次后失败
    assert asyncio.run(tsc._pick(set())) is None
    assert mock.exchange_calls == 1
    # 第二次：60s 负缓存内不重放 refresh 请求
    assert asyncio.run(tsc._pick(set())) is None
    assert mock.exchange_calls == 1
    # 重置负缓存后允许重试
    tsc._reset_refresh_fail_cache()
    assert asyncio.run(tsc._pick(set())) is None
    assert mock.exchange_calls == 2


def test_pick_refresh_success_clears_negative_cache(isolated_db):
    mock = _ExchangeMock(exchange_ok=False)
    tsc._TRANSPORT = httpx.MockTransport(mock.handler)
    aid = _add_solo_account(status="expired", uid="90003", name="expired-2")

    assert asyncio.run(tsc._pick(set())) is None
    assert tsc._refresh_fail_at.get(aid)  # 已记负缓存

    mock.exchange_ok = True
    tsc._reset_refresh_fail_cache()
    fresh = asyncio.run(tsc._pick(set()))
    assert isinstance(fresh, dict)
    assert fresh["access_token"] == "jwt-new"
    # 成功后负缓存必须清空（下次账号再过期可立即重试刷新）
    assert tsc._refresh_fail_at == {}


# ---------------------------------------------------------------------------
# C3：小修一组
# ---------------------------------------------------------------------------


def test_fetch_models_removed():
    """fetch_models 全仓库零调用，spec C3 要求删除。"""
    assert not hasattr(tsc, "fetch_models")
    assert hasattr(tsc, "fetch_model_details")


def test_token_httpx_is_type_only_import():
    """token.py 的 httpx 仅存在于 TYPE_CHECKING 分支，运行时零开销。"""
    assert not hasattr(ttoken, "httpx")
    assert ttoken._http_json_post.__annotations__["return"] == "httpx.Response"


def test_checkin_post_honors_ug_host_override(isolated_db):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"checked_in": False, "credits": 5, "enable": True})

    tsc._TRANSPORT = httpx.MockTransport(handler)
    db.set_setting("channel_hosts", {"traesolo": {"ug_host": "https://ug-mirror.example.com"}})
    acc = db.get_account(_add_solo_account())
    st = asyncio.run(tquota.fetch_checkin(acc))
    assert st["ok"] is True
    assert seen, "checkin request must be captured by mock transport"
    assert seen[0].startswith("https://ug-mirror.example.com/trae/api/v2/ug/checkin_credits/status")


def test_checkin_post_falls_back_to_default_ug_host(isolated_db):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"checked_in": False, "credits": 5, "enable": True})

    tsc._TRANSPORT = httpx.MockTransport(handler)
    acc = db.get_account(_add_solo_account())
    st = asyncio.run(tquota.fetch_checkin(acc))
    assert st["ok"] is True
    assert seen[0].startswith("https://api.trae.cn/trae/api/v2/ug/checkin_credits/status")


def test_import_path_reads_plain_key_file(isolated_db):
    """import_path 清理函数体内重复 import 后仍走通（读文件 + upsert 契约）。"""
    p = _make_compat_provider()
    keyfile = Path(__file__).resolve().parent.parent / ".tmp" / f"perf-import-{uuid.uuid4().hex[:8]}.txt"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text("sk-imported-key-99887766", encoding="utf-8")
    try:
        result = p.import_path(str(keyfile))
        assert result["updated"] is False
        assert result["row"]["access_token"] == "sk-imported-key-99887766"
        rows = db.list_accounts(provider="perfchan")
        assert len(rows) == 1
        assert rows[0]["access_token"] == "sk-imported-key-99887766"
    finally:
        keyfile.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# C4：SSE 透传预过滤
# ---------------------------------------------------------------------------


SSE_STREAM = (
    'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
    'data: {"id":"c3","object":"chat.completion.chunk","choices":[{"index":0,"delta":{}}],'
    '"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
    "data: [DONE]\n\n"
).encode("utf-8")


def _compat_sse_mock():
    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, content=SSE_STREAM, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404, text="unmocked " + request.url.path)

    return httpx.MockTransport(handler)


def test_stream_chat_passthrough_and_prefilter(isolated_db, monkeypatch):
    p = _make_compat_provider()
    p.set_transport(_compat_sse_mock())
    aid = db.add_account(
        {
            "name": "perfchan-1",
            "uid": "u-perf-1",
            "provider": "perfchan",
            "access_token": "sk-test",
            "status": "active",
            "domain": "http://mock.test/v1",
        }
    )
    account = db.get_account(aid)

    # 统计 json.loads 调用次数（应只有携带 usage / finish_reason 的两帧被解析）
    loads_calls = {"n": 0}
    real_loads = oc.json.loads
    real_dumps = oc.json.dumps

    def counting_loads(*args, **kwargs):
        loads_calls["n"] += 1
        return real_loads(*args, **kwargs)

    stub = types.SimpleNamespace(
        loads=counting_loads, dumps=real_dumps, JSONDecodeError=oc.json.JSONDecodeError
    )
    monkeypatch.setattr(oc, "json", stub)

    record_calls = []

    def fake_record(*args, **kwargs):
        record_calls.append(kwargs)

    monkeypatch.setattr(p, "_record", fake_record)

    async def run():
        out = []
        async for chunk in p._stream_chat(account, {"model": "m1", "messages": []}, None, "m1"):
            out.append(chunk)
        return out

    out = asyncio.run(run())

    # 透传保真：三条 data 行 + [DONE]，逐行原样（含 \n\n 结尾）
    assert out == [
        'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n',
        'data: {"id":"c2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n',
        'data: {"id":"c3","object":"chat.completion.chunk","choices":[{"index":0,"delta":{}}],'
        '"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
        "data: [DONE]\n\n",
    ]
    # 预过滤：普通内容帧不触发 json.loads
    assert loads_calls["n"] == 2
    # usage / finish_reason 提取语义不变
    assert len(record_calls) == 1
    kwargs = record_calls[0]
    assert kwargs["finish_reason"] == "length"
    assert kwargs["usage_payload"]["total_tokens"] == 15
    assert kwargs["prompt_tokens"] == 10
    assert kwargs["completion_tokens"] == 5
    assert kwargs["total_tokens"] == 15
    assert kwargs["status_code"] == 200


# ---------------------------------------------------------------------------
# C5：refresh_model_ids stale-while-revalidate
# ---------------------------------------------------------------------------


class _ModelsMock:
    def __init__(self):
        self.hits = 0
        self.payload = {"data": [{"id": "m-a"}, {"id": "m-b"}]}

    def handler(self, request):
        if request.url.path.endswith("/models"):
            self.hits += 1
            return httpx.Response(200, json=self.payload)
        return httpx.Response(404, text="unmocked " + request.url.path)


def _add_compat_account(provider_id="perfchan"):
    aid = db.add_account(
        {
            "name": f"{provider_id}-1",
            "uid": f"u-{provider_id}",
            "provider": provider_id,
            "access_token": "sk-test",
            "status": "active",
            "domain": "http://mock.test/v1",
        }
    )
    return db.get_account(aid)


def _expire_models_cache(p):
    p._models_cache["fetched_at"] = time.time() - p._models_cache_ttl - 1.0


def test_refresh_model_ids_cold_start_is_sync(isolated_db):
    mock = _ModelsMock()
    p = _make_compat_provider()
    p.set_transport(httpx.MockTransport(mock.handler))
    _add_compat_account()

    ids = asyncio.run(p.refresh_model_ids())
    assert ids == ["m-a", "m-b"]
    assert mock.hits == 1  # 冷启动同步拉取，首个请求即拿到真实模型表


def test_refresh_model_ids_swr_serves_stale_then_refreshes(isolated_db):
    mock = _ModelsMock()
    p = _make_compat_provider()
    p.set_transport(httpx.MockTransport(mock.handler))
    _add_compat_account()
    asyncio.run(p.refresh_model_ids())
    assert mock.hits == 1

    mock.payload = {"data": [{"id": "m-c"}]}
    _expire_models_cache(p)

    async def run():
        stale = await p.refresh_model_ids()
        assert stale == ["m-a", "m-b"]  # 立即返回旧表，不等上游
        for _ in range(500):
            if not p._refreshing:
                break
            await asyncio.sleep(0.01)
        return p._models_cache["ids"]

    ids = asyncio.run(run())
    assert ids == ["m-c"]  # 后台刷新完成后缓存更新
    assert mock.hits == 2
    assert p._refreshing is False


def test_refresh_model_ids_swr_dedupes_concurrent_refresh(isolated_db):
    mock = _ModelsMock()
    p = _make_compat_provider()
    p.set_transport(httpx.MockTransport(mock.handler))
    _add_compat_account()
    asyncio.run(p.refresh_model_ids())
    assert mock.hits == 1

    _expire_models_cache(p)

    async def run():
        # 模拟已有后台刷新在跑：请求直接用旧表，不再叠加调度
        p._refreshing = True
        stale = await p.refresh_model_ids()
        assert stale == ["m-a", "m-b"]
        assert mock.hits == 1
        assert p._refreshing is True
        p._refreshing = False

    asyncio.run(run())


def test_refresh_model_ids_force_is_sync(isolated_db):
    mock = _ModelsMock()
    p = _make_compat_provider()
    p.set_transport(httpx.MockTransport(mock.handler))
    _add_compat_account()
    asyncio.run(p.refresh_model_ids())

    mock.payload = {"data": [{"id": "m-d"}]}
    _expire_models_cache(p)
    ids = asyncio.run(p.refresh_model_ids(force=True))
    assert ids == ["m-d"]  # force（管理页刷新按钮）保持同步强拉
    assert mock.hits == 2


# ---------------------------------------------------------------------------
# C5：ensure_env_account 实例级 memo
# ---------------------------------------------------------------------------

ENV_NAME = "CB_PERF_TEST_KEY"


def _env_provider():
    return _make_compat_provider(env_api_key=ENV_NAME)


def test_env_account_memo_skips_full_scan(isolated_db, monkeypatch):
    monkeypatch.setenv(ENV_NAME, "sk-env-memo-key-12345678")
    p = _env_provider()

    calls = {"n": 0}
    real_list = db.list_accounts

    def counting_list(*args, **kwargs):
        calls["n"] += 1
        return real_list(*args, **kwargs)

    monkeypatch.setattr(db, "list_accounts", counting_list)

    row1 = p.ensure_env_account()
    assert row1 is not None
    assert row1["name"] == "perfchan-env"
    n1 = calls["n"]
    assert n1 >= 1

    row2 = p.ensure_env_account()
    assert row2["id"] == row1["id"]
    # memo 命中：不再全表扫描（仅单行 get_account 校验）
    assert calls["n"] == n1
    assert len(db.list_accounts(provider="perfchan")) == 1


def test_env_account_memo_rescans_on_env_change(isolated_db, monkeypatch):
    monkeypatch.setenv(ENV_NAME, "sk-env-memo-key-12345678")
    p = _env_provider()
    row1 = p.ensure_env_account()
    assert row1 is not None

    calls = {"n": 0}
    real_list = db.list_accounts

    def counting_list(*args, **kwargs):
        calls["n"] += 1
        return real_list(*args, **kwargs)

    monkeypatch.setattr(db, "list_accounts", counting_list)

    # env 值变化 → memo key 失效 → 重新扫描（语义与旧实现一致：
    # 已有 active 行时不被新 env 值覆盖）
    monkeypatch.setenv(ENV_NAME, "sk-different-key-87654321")
    row2 = p.ensure_env_account()
    assert calls["n"] == 1
    assert row2["id"] == row1["id"]

    # 显式 reset 后同样重扫
    p._reset_env_account_cache()
    row3 = p.ensure_env_account()
    assert calls["n"] == 2
    assert row3["id"] == row1["id"]


def test_env_account_memo_self_heals_when_row_disabled(isolated_db, monkeypatch):
    monkeypatch.setenv(ENV_NAME, "sk-env-heal-key-24681357")
    p = _env_provider()
    row1 = p.ensure_env_account()
    assert row1 is not None

    db.update_account(row1["id"], {"status": "inactive"})
    # memo 命中但行已非 active → 失效并回退全量引导（保留旧实现的自愈语义）
    row2 = p.ensure_env_account()
    assert row2 is not None
    assert row2["id"] == row1["id"]
    assert row2["status"] == "active"
    rows = db.list_accounts(provider="perfchan")
    assert len(rows) == 1
    assert rows[0]["status"] == "active"


def test_env_unset_returns_none_without_memo(isolated_db, monkeypatch):
    monkeypatch.delenv(ENV_NAME, raising=False)
    p = _env_provider()
    assert p.ensure_env_account() is None
    assert p._env_account_memo["key"] is None
    assert p._env_account_memo["row"] is None
