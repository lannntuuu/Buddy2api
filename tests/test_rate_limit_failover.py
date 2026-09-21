"""WorkBuddy 6004 (账号, 模型) 级频率限制：记录 / 预判跳过 / 全限即停。

九项产品裁决（苏格拉底问答定稿）的测试锚点：
  q1 6004 = HTTP 429 + JSON body（流式在响应头阶段，换号物理可行）
  q2 粒度 = (账号, 模型)，同账号其它模型不受连坐
  q3 对外返回保留 code 6004 + 结构化字段
  q4 状态纯内存，重启即失（无持久化用例）
  q5 模型感知预判跳过（seed tried_ids，覆盖 pin/sticky）
  q6 全限判定只看"是否已记录限流"
  q7 任一时刻发现全覆盖即停，限流换号零退避
  q7' key 用别名解析后的上游模型 id
  q9 限流覆盖手动 pin
"""

import asyncio
import json
import time

import pytest

import upstream.rate_limits as rate_limits
from accounts import auth_manager
from storage import database as db
from upstream import proxy


RATE_BODY = {
    "code": 6004,
    "msg": "您的使用量已超出频率限制，将在 2026-09-21 14:19:07 UTC+8 重置，您也可以切换其他模型继续使用。",
    "requestId": "baa312e4faef4947b1a757d1d8ef8710",
}


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    rate_limits.clear()
    # _account_failures / sticky 是进程级内存状态，跨用例必须清掉
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    yield
    rate_limits.clear()
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()


@pytest.fixture()
def frozen_now(monkeypatch):
    """固定时钟（epoch 秒），返回可推进的 state dict。"""
    state = {"now": 1_800_000_000.0}
    monkeypatch.setattr(rate_limits, "_now", lambda: state["now"])
    return state


def _add_account(name: str = "acc") -> dict:
    account_id = db.add_account(
        {
            "name": name,
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9_999_999_999_999,
        }
    )
    return db.get_account(account_id)


def _add_two_accounts():
    return _add_account("acc-1"), _add_account("acc-2")


# ============================================================
# rate_limits 单元
# ============================================================

def test_parse_reset_epoch_from_prod_body():
    """q1：生产 6004 body 解析出正确解除时刻（含 30s 安全余量）。"""
    epoch = rate_limits.parse_reset_epoch(RATE_BODY)
    # 2026-09-21 14:19:07 UTC+8 = 1789971547 - 30 = 1789971517
    assert epoch == 1_789_971_517.0


def test_parse_reset_epoch_unparseable_returns_none():
    assert rate_limits.parse_reset_epoch({"msg": "some other error"}) is None
    assert rate_limits.parse_reset_epoch(b"not json") is None
    assert rate_limits.parse_reset_epoch(None) is None


def test_record_and_query_scoped_per_account_model(frozen_now):
    """q2：记录只影响同一 (账号, 模型)；同账号其它模型、同模型其它账号不受连坐。"""
    rate_limits.record(1, "glm-5.2", frozen_now["now"] + 3600)
    assert rate_limits.is_limited(1, "glm-5.2")
    assert not rate_limits.is_limited(1, "glm-5.1")       # 同账号其它模型
    assert not rate_limits.is_limited(2, "glm-5.2")       # 同模型其它账号


def test_record_fallback_when_unparseable(frozen_now):
    """解析失败 → 60s 兜底，不假装知道解除时间。"""
    rate_limits.record(1, "glm-5.2", None)
    until = rate_limits.limited_until(1, "glm-5.2")
    assert until == frozen_now["now"] + rate_limits.FALLBACK_COOLDOWN_S


def test_record_keeps_later_existing_entry(frozen_now):
    """已有更晚的记录（真实解除时刻）不被兜底值回退。"""
    rate_limits.record(1, "glm-5.2", frozen_now["now"] + 3600)
    rate_limits.record(1, "glm-5.2", frozen_now["now"] + 30)
    assert rate_limits.limited_until(1, "glm-5.2") == frozen_now["now"] + 3600


def test_entries_expire_lazily(frozen_now):
    rate_limits.record(1, "glm-5.2", frozen_now["now"] + 100)
    assert rate_limits.is_limited(1, "glm-5.2")
    frozen_now["now"] += 101
    assert not rate_limits.is_limited(1, "glm-5.2")
    assert rate_limits.snapshot() == {}


def test_model_view_earliest_and_details(frozen_now):
    """q8：最早恢复时间 + 各账号明细（按解除时刻升序）。"""
    rate_limits.record(2, "glm-5.2", frozen_now["now"] + 2000)
    rate_limits.record(1, "glm-5.2", frozen_now["now"] + 500)
    view = rate_limits.model_view("glm-5.2")
    assert [item["account_id"] for item in view["limited_accounts"]] == [1, 2]
    assert view["earliest_reset"] == frozen_now["now"] + 500


# ============================================================
# proxy：非流式
# ============================================================

def _patch_non_stream_fakes(monkeypatch, accounts, responses_by_account):
    """按账号注入非流式 _collect_stream 结果；responses_by_account: {id: tuple}。"""
    calls = {"statuses": [], "delays": []}

    async def pick(exclude):
        return next((a for a in accounts if a["id"] not in exclude), None)

    async def headers(_account):
        return {"Authorization": "Bearer test"}

    async def collect(_url, _headers, _body, account, *_args, **_kwargs):
        result = responses_by_account[account["id"]]
        if result[0] == "error":
            calls["statuses"].append((account["id"], result[1][0]))
        return result

    async def delay(_attempt):
        calls["delays"].append(_attempt)

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(proxy, "_collect_stream", collect)
    monkeypatch.setattr(proxy, "_retry_delay", delay)
    monkeypatch.setattr(proxy, "_log_request", lambda *_a, **_k: None)
    return calls


def test_non_stream_switches_account_on_6004_and_succeeds(isolated_db, monkeypatch):
    """q5/q7：账号 A 吃 6004 → 记录 + 零退避换号 → B 成功。"""
    acc1, acc2 = _add_two_accounts()
    ok = ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})
    calls = _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ("error", (429, RATE_BODY)), acc2["id"]: ok},
    )
    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert result == ok
    # A 只被打一次；B 成功；全程零退避
    assert [aid for aid, _ in calls["statuses"]] == [acc1["id"]]
    assert calls["delays"] == []
    # (A, glm-5.2) 已记录
    assert rate_limits.is_limited(acc1["id"], "glm-5.2")


def test_non_stream_all_limited_returns_project_level_6004(isolated_db, monkeypatch):
    """q3/q6/q7：两账号都吃 6004 → 零上游第三次请求，项目层 429 + code 6004。"""
    acc1, acc2 = _add_two_accounts()
    calls = _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ("error", (429, RATE_BODY)), acc2["id"]: ("error", (429, RATE_BODY))},
    )
    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert result[0] == "error"
    status, body = result[1]
    assert status == 429
    assert body["code"] == 6004
    error = body["error"]
    assert error["type"] == "rate_limit_error"
    assert error["reset_at"] == 1_789_971_517.0
    assert error["retry_after"] > 0
    assert {item["account_id"] for item in error["limited_accounts"]} == {acc1["id"], acc2["id"]}
    # 上游原始文案保留（含 requestId 的 msg 原文）
    assert "频率限制" in body["msg"]
    # 两个 429 之后再无上游请求；零退避
    assert len(calls["statuses"]) == 2
    assert calls["delays"] == []


def test_non_stream_preemptive_skip_returns_without_upstream_call(isolated_db, monkeypatch):
    """q5：入口预判即全限 → 零上游请求直接项目层返回。"""
    acc1, acc2 = _add_two_accounts()
    rate_limits.record(acc1["id"], "glm-5.2", time.time() + 3600)
    rate_limits.record(acc2["id"], "glm-5.2", time.time() + 7200)
    calls = _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ("json", {"id": "x"}), acc2["id"]: ("json", {"id": "x"})},
    )
    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert result[0] == "error"
    assert result[1][0] == 429 and result[1][1]["code"] == 6004
    assert calls["statuses"] == []  # 上游零调用


def test_non_stream_no_accounts_no_6004(isolated_db, monkeypatch):
    """q6：没有任何限流记录时，pick 不到账号维持 503 原语义。"""
    acc1, acc2 = _add_two_accounts()
    # 两账号都被"其它原因"排除（headers 取不到 → 401 失败路径）后 pick 为 None
    async def no_headers(_account):
        return None

    calls = _patch_non_stream_fakes(monkeypatch, [acc1, acc2], {})
    monkeypatch.setattr(auth_manager, "get_valid_headers", no_headers)
    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert result[0] == "error"
    assert result[1][0] == 503
    assert result[1][1]["error"]["message"] == "No available accounts"
    assert calls["statuses"] == []


def test_non_stream_record_does_not_trigger_account_cooldown(isolated_db, monkeypatch):
    """q2：6004 不触发账号级连坐冷却——同账号其它模型照常调度。"""
    acc1, acc2 = _add_two_accounts()
    ok = ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})
    _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ("error", (429, RATE_BODY)), acc2["id"]: ok},
    )
    asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert not auth_manager.account_is_cooling_down(acc1["id"])


def test_non_stream_other_429_still_uses_backoff(isolated_db, monkeypatch):
    """非 6004 的 429（无 code 字段）维持原行为：账号冷却 + 退避。"""
    acc1, acc2 = _add_two_accounts()
    ok = ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})
    plain_429 = {"error": {"message": "too many requests"}}
    calls = _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ("error", (429, plain_429)), acc2["id"]: ok},
    )
    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert result == ok
    assert calls["delays"]  # 走了退避
    assert not rate_limits.is_limited(acc1["id"], "glm-5.2")


# ============================================================
# proxy：流式
# ============================================================

def _err_sse_payloads(raw: bytes) -> list[dict]:
    payloads = []
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            payloads.append(json.loads(line[5:].strip()))
    return payloads


class _FakeRateLimitResponse:
    """响应头阶段直接 429 + 6004 body（生产实测形态）。"""

    status_code = 429

    def __init__(self, body: bytes):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._body


class _FakeOkResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield b'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'
        yield b"data: [DONE]\n\n"


def _patch_stream_fakes(monkeypatch, accounts, responses_by_account):
    """按账号注入流式响应（FakeResponse 形态，参照 test_core 惯例）。"""
    calls = {"delays": [], "upstream_called": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, headers=None, **kwargs):
            calls["upstream_called"] += 1
            return responses_by_account[int(headers["X-Test-Account"])]

    async def pick(exclude):
        return next((a for a in accounts if a["id"] not in exclude), None)

    async def headers(account):
        return {"X-Test-Account": str(account["id"])}

    async def delay(_attempt):
        calls["delays"].append(_attempt)

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda _d: 30)
    monkeypatch.setattr(proxy, "_retry_delay", delay)
    monkeypatch.setattr(proxy, "_log_request", lambda *_a, **_k: None)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *a, **k: _Client())
    return calls


def _run_stream(body):
    async def collect():
        return b"".join([chunk async for chunk in proxy._stream_upstream(body, None, "glm-5.2")])
    return asyncio.run(collect())


def test_stream_switches_account_on_6004_and_succeeds(isolated_db, monkeypatch):
    """q1/q5：流式头阶段 429+6004 → 记录 + 零延迟换号 → B 成功出流。"""
    acc1, acc2 = _add_two_accounts()
    body_6004 = json.dumps(RATE_BODY, ensure_ascii=False).encode("utf-8")
    calls = _patch_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: _FakeRateLimitResponse(body_6004), acc2["id"]: _FakeOkResponse()},
    )
    raw = _run_stream({"model": "glm-5.2", "stream": True})
    payloads = _err_sse_payloads(raw)
    assert payloads and "hi" == payloads[0]["choices"][0]["delta"]["content"]
    assert calls["delays"] == []
    assert rate_limits.is_limited(acc1["id"], "glm-5.2")


def test_stream_all_limited_returns_project_level_6004_event(isolated_db, monkeypatch):
    """q3：流式全限 → SSE error 事件保留 code 6004 + 结构化字段。"""
    acc1, acc2 = _add_two_accounts()
    body_6004 = json.dumps(RATE_BODY, ensure_ascii=False).encode("utf-8")
    _patch_stream_fakes(
        monkeypatch, [acc1, acc2],
        {
            acc1["id"]: _FakeRateLimitResponse(body_6004),
            acc2["id"]: _FakeRateLimitResponse(body_6004),
        },
    )
    raw = _run_stream({"model": "glm-5.2", "stream": True})
    payloads = _err_sse_payloads(raw)
    assert len(payloads) == 1
    error = payloads[0]["error"]
    assert error["code"] == 6004
    assert error["type"] == "rate_limit_error"
    assert error["reset_at"] == 1_789_971_517.0
    assert len(error["limited_accounts"]) == 2
    assert "频率限制" in error["message"]


def test_stream_preemptive_skip_zero_upstream(isolated_db, monkeypatch):
    """q5：流式入口预判即全限 → 零上游请求。"""
    acc1, acc2 = _add_two_accounts()
    rate_limits.record(acc1["id"], "glm-5.2", time.time() + 3600)
    rate_limits.record(acc2["id"], "glm-5.2", time.time() + 7200)
    calls = _patch_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: _FakeRateLimitResponse(b"{}"), acc2["id"]: _FakeRateLimitResponse(b"{}")},
    )
    raw = _run_stream({"model": "glm-5.2", "stream": True})
    error = _err_sse_payloads(raw)[0]["error"]
    assert error["code"] == 6004
    assert calls["upstream_called"] == 0


# ============================================================
# key 归一与 pin 覆盖
# ============================================================

def test_alias_resolved_model_key(isolated_db, monkeypatch):
    """q7'：限流 key 用别名解析后的上游模型 id（hy3-preview-agent → hy3-x）。"""
    acc1, acc2 = _add_two_accounts()
    ok = ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})
    alias_6004 = dict(RATE_BODY, msg="您的使用量已超出频率限制，将在 2026-09-21 14:19:07 UTC+8 重置。")
    _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ("error", (429, alias_6004)), acc2["id"]: ok},
    )
    asyncio.run(proxy.proxy_chat_completions(
        {"model": "hy3-preview-agent", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert rate_limits.is_limited(acc1["id"], "hy3-x")
    assert not rate_limits.is_limited(acc1["id"], "hy3-preview-agent")


def test_rate_limit_overrides_manual_pin(isolated_db, monkeypatch):
    """q9：pinned 账号该模型限流 → 视为不可用，自动切常规调度。"""
    acc1, acc2 = _add_two_accounts()
    auth_manager.set_manual_pin("workbuddy", acc1["id"])
    ok = ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})
    _patch_non_stream_fakes(
        monkeypatch, [acc1, acc2],
        {acc1["id"]: ok, acc2["id"]: ok},
    )
    # 预先记录 acc1 限流：pin 不应再命中 acc1
    rate_limits.record(acc1["id"], "glm-5.2", time.time() + 3600)
    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}, None,
    ))
    assert result == ok  # acc2 顶上（否则 acc1 被选中会因 limited 被跳过）


# ============================================================
# 管理端观测面（模型设置页数据源）
# ============================================================

def test_channel_model_view_exposes_rate_limits(isolated_db, monkeypatch):
    """q8：workbuddy 的 channel_model_view 带限流观测面；其它通道为空。"""
    from accounts import control_plane

    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw")
    acc = _add_account("蓝图")
    rate_limits.record(acc["id"], "glm-5.2", time.time() + 3600)

    view = control_plane.channel_model_view("workbuddy")
    assert "rate_limits" in view
    rl = view["rate_limits"].get("glm-5.2")
    assert rl and rl["limited_accounts"][0]["account_id"] == acc["id"]
    assert rl["limited_accounts"][0]["account_name"] == "蓝图"
    assert rl["limited_accounts"][0]["reset_at_iso"].endswith("UTC+8")
    # 其它通道不受影响
    other = control_plane.channel_model_view("qclaw")
    assert other["rate_limits"] == {}
