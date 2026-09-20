import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest

from storage import database as db
import providers
from gateway import router
from gateway import server as tw_server
from providers.protocol import UnknownModel
from providers import store_common
from providers.traework import chat as tw
from providers.traework import token as tw_token
from providers.traework.chat import _split_event, _text_from_event, extract_assistant_text, translate_model
from providers.traework.crypto import _MAGIC, _SALT, decrypt_tc_b64
from providers.traework.store import parse_credentials, traework_auth_dirs
from providers.traework.token import adopt_credentials_from_client


@pytest.fixture()
def traework_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


@pytest.fixture(autouse=True)
def _clean_global_auth_state():
    """每例前后清掉进程级鉴权全局态，避免泄漏到后续测试文件。

    本模块的 _turn/_run_turn 用例会写 auth_manager._account_failures（失败冷却）
    与 trae_shared 的 refresh 负缓存、sticky 账号；这些是模块级全局，isolated_db
    并不清理。历史症状：跑完本文件后，test_pick_contract 里的账号处于冷却态、
    _pick 直接跳过，导致 "calls == []" 一类假失败（单独运行该文件却全绿）。
    """
    from accounts import auth_manager
    from providers import trae_shared

    def _reset():
        trae_shared.reset_refresh_failures()
        auth_manager._sticky_account_id.clear()
        with auth_manager._failure_lock:
            auth_manager._account_failures.clear()

    _reset()
    yield
    _reset()


def test_traework_in_default_registry(monkeypatch, isolated_db):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    enabled = providers.enabled_provider_ids()
    canonical = {"workbuddy", "qclaw", "qwenwork", "traework", "traesolo"}
    assert canonical.issubset(set(enabled)), (
        f"missing default channels: {canonical - set(enabled)}"
    )
    assert providers.get_provider("traework") is not None
    assert "traework" in providers._LOADED


def test_parse_credentials_official_shape():
    parsed = parse_credentials(
        {
            "token": "jwt-access",
            "refreshToken": "rt-1",
            "userId": "3577",
            "expiredAt": "2026-09-09T08:55:19.325Z",
            "host": "https://api.trae.cn",
            "account": {"username": "书虫"},
            "device_id": "3446",
        }
    )
    assert parsed["provider"] == "traework"
    assert parsed["access_token"] == "jwt-access"
    assert parsed["refresh_token"] == "rt-1"
    assert parsed["uid"] == "3577"
    assert parsed["extra"]["device_id"] == "3446"
    assert parsed["expires_at"] > 10_000_000_000


def test_parse_credentials_requires_token():
    with pytest.raises(ValueError):
        parse_credentials({"account": {"username": "x"}})


def test_bind_traework_when_enabled(traework_enabled, isolated_db):
    bound = router.bind({"model": "auto"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "auto"
    bound = router.bind({"model": "traework/qwen-3.7-plus"}, {"default_channel": "traework"})
    assert bound.inner == "qwen-3.7-plus"
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "traework"})


def test_translate_auto(isolated_db):
    assert translate_model("auto") == "qwen-3.7-plus"


def test_translate_auto_falls_back_when_admin_aliases_omit_it(isolated_db):
    """回归：管理员自定义别名漏掉 "auto" 时，保留字必须兜底成具体模型。

    背景（prod 实测故障）：`channel_aliases` 的语义是「管理员表存在即整体替换内置
    默认」。prod 的 `traework.aliases` 被设成 {"DeepSeek-V4-Flash-Official": 同名}，
    没有 "auto"，于是 translate_model("auto") 原样返回 "auto" 并**透传给上游**；
    而上游不认识这个保留字，直接 500（`internal server error`）。
    管理页「测试」按钮硬编码 model="auto"，所以只要别名表缺 auto，测试必失败。
    """
    # 模拟 prod：自定义别名存在但没有 auto
    db.set_setting("traework.aliases", {"DeepSeek-V4-Flash-Official": "DeepSeek-V4-Flash-Official"})
    assert translate_model("auto") == "qwen-3.7-plus"
    # 管理员显式配了 auto 时仍以管理员为准（不改变既有优先级）
    db.set_setting("traework.aliases", {"auto": "glm-5.3"})
    assert translate_model("auto") == "glm-5.3"
    # 具体模型名不受兜底影响
    db.set_setting("traework.aliases", {"DeepSeek-V4-Flash-Official": "DeepSeek-V4-Flash-Official"})
    assert translate_model("glm-5.3") == "glm-5.3"


def test_extract_assistant_text_from_task():
    items = [
        {"role": "user", "content": "[]"},
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {"type": "text", "text_content": "pong"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assert extract_assistant_text(items) == "pong"


def test_extract_assistant_text_prefers_finish_over_reasoning():
    # 线上真实结构：plan_item 嵌套一层，回答在 finish 工具 params.summary，
    # reasoning_content 是思考文本，不得作为回答返回。
    items = [
        {"role": "user", "content": "[]"},
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {
                            "id": "m1",
                            "type": "plan_item",
                            "plan_item": {
                                "id": "p1",
                                "thought": "",
                                "reasoning_content": (
                                    'The user is asking me to reply with "pong". '
                                    "This is a simple request.\n"
                                ),
                                "tool_call_info": {
                                    "id": "tc1",
                                    "name": "finish",
                                    "params": {"summary": "pong"},
                                    "result": {"status": "success"},
                                },
                                "agent_status": {"status": "completed"},
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assert extract_assistant_text(items) == "pong"


def test_extract_assistant_text_thinking_only_fallback():
    # 没有 finish / 正文时退回思考文本，保持旧的兜底行为。
    items = [
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {
                            "type": "plan_item",
                            "plan_item": {
                                "thought": "",
                                "reasoning_content": "thinking out loud",
                                "tool_call_info": {
                                    "name": "web_search",
                                    "params": {"query": "x"},
                                },
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        }
    ]
    assert extract_assistant_text(items) == "thinking out loud"


def test_event_plan_item_uses_finish_summary_not_reasoning():
    # 事件流里的扁平 plan_item 事件：只取 finish 的回答，不取 reasoning。
    payload = {
        "id": "p1",
        "task_id": "t1",
        "thought": "",
        "reasoning_content": 'The user is asking me to reply with "pong".\n',
        "tool_call_info": {
            "id": "tc1",
            "name": "finish",
            "params": {"summary": "pong"},
            "result": {"status": "success"},
        },
    }
    assert _text_from_event("plan_item", payload) == "pong"


def test_event_without_finish_collects_nothing_from_reasoning():
    payload = {
        "id": "p1",
        "task_id": "t1",
        "thought": "",
        "reasoning_content": "The",
        "tool_call_info": {"id": "tc1", "name": "", "params": None, "result": {}},
    }
    assert _text_from_event("plan_item", payload) == ""


# ---------------------------------------------------------------------------
# 会话模式（work / code）可配置：默认 work；配置 code 改 mode 且 agent 联动
# （mode=code -> solo_agent_lite，其余含 work/未配置 -> solo_work_lite）
# ---------------------------------------------------------------------------


def test_session_mode_default_is_work(isolated_db):
    from providers.model_config import channel_session_mode

    # 未配置 → 默认 work，且行为与硬常量一致
    assert channel_session_mode("traework", "work") == "work"
    assert channel_session_mode("traework", "work") == "work" == tw.SESSION_MODE


def test_session_mode_reads_setting(isolated_db):
    from providers.model_config import channel_session_mode

    db.set_setting("traework.mode", "code")
    assert channel_session_mode("traework", "work") == "code"
    # 非法值回退默认
    db.set_setting("traework.mode", "bogus")
    assert channel_session_mode("traework", "work") == "work"
    # 空串回退
    db.set_setting("traework.mode", "")
    assert channel_session_mode("traework", "work") == "work"


def _post_body(fake, suffix):
    for _url, body in fake.posts:
        if _url.endswith(suffix):
            return body
    raise AssertionError(f"no POST to {suffix}")


def test_turn_creates_session_with_default_mode(isolated_db, monkeypatch):
    """默认（无 mode 设置）建会话 body 与改造前逐字段一致：mode=work，agent 不变。"""
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    # _turn 现返回 (text, usage, finish_reason, reasoning)
    text, _usage, _finish, _reasoning = asyncio.run(tw._turn(account, "hi", "qwen-3.7-plus", timeout=90.0))
    assert text == "pong"
    # 建会话 POST 的 body 与改造前逐字段一致
    create_body = _post_body(fake, "/chat_sessions")
    assert create_body["mode"] == "work"
    assert create_body["auto_create_project"] is True
    assert create_body["origin"] == "web"
    # agent_id / agent_type 在发消息 POST 上，模式切换不改 agent（仍为 solo_work_lite）
    msg_body = _post_body(fake, "/messages")
    assert msg_body["agent_id"] == "solo_work_lite"
    assert msg_body["agent_type"] == "solo_work_lite"


def test_turn_creates_session_with_code_mode(isolated_db, monkeypatch):
    """配置 traework.mode=code：建会话 mode=code，agent_id/agent_type 联动为 solo_agent_lite。"""
    db.set_setting("traework.mode", "code")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    asyncio.run(tw._turn(account, "hi", "qwen-3.7-plus", timeout=90.0))
    create_body = _post_body(fake, "/chat_sessions")
    assert create_body["mode"] == "code"
    msg_body = _post_body(fake, "/messages")
    assert msg_body["agent_id"] == "solo_agent_lite"
    assert msg_body["agent_type"] == "solo_agent_lite"


def test_turn_creates_session_with_work_mode(isolated_db, monkeypatch):
    """配置 traework.mode=work：建会话 mode=work，agent_id/agent_type 仍为 solo_work_lite。"""
    db.set_setting("traework.mode", "work")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    asyncio.run(tw._turn(account, "hi", "qwen-3.7-plus", timeout=90.0))
    create_body = _post_body(fake, "/chat_sessions")
    assert create_body["mode"] == "work"
    msg_body = _post_body(fake, "/messages")
    assert msg_body["agent_id"] == "solo_work_lite"
    assert msg_body["agent_type"] == "solo_work_lite"


def test_r1_turn_sends_flattened_prompt_to_upstream(isolated_db, monkeypatch):
    """R1：_turn 发出的 query 必须包含 system 与历史（而非只发最后一句 user）。"""
    db.set_setting("traework.mode", "work")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    full_payload = {
        "model": "qwen-3.7-plus",
        "messages": [
            {"role": "system", "content": "system-ctx"},
            {"role": "user", "content": "上一问"},
            {"role": "assistant", "content": "上一答"},
            {"role": "user", "content": "当前问"},
        ],
    }
    prompt, _has_user = tw._build_prompt(full_payload)
    asyncio.run(tw._turn(account, prompt, "qwen-3.7-plus", timeout=90.0))
    msg_body = _post_body(fake, "/messages")
    query = json.loads(msg_body["query"])
    assert query[0]["data"]["content"] == prompt
    assert "system-ctx" in query[0]["data"]["content"]
    assert "上一问" in query[0]["data"]["content"]
    assert "上一答" in query[0]["data"]["content"]
    assert "当前问" in query[0]["data"]["content"]


def test_traework_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "traework"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text


def test_traework_auth_dirs_ignore_workbuddy_cb_auth_dir(monkeypatch, tmp_path):
    tdir = tmp_path / "trae-auth"
    tdir.mkdir()
    wb = tmp_path / "workbuddy-auth"
    wb.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(tdir))
    monkeypatch.setenv("CB_AUTH_DIR", str(wb))
    dirs = [path.resolve() for path in traework_auth_dirs()]
    assert tdir.resolve() in dirs
    assert wb.resolve() not in dirs


def test_decrypt_tc_roundtrip_rejects_garbage():
    with pytest.raises(Exception):
        decrypt_tc_b64("not-base64-$$$")


# ---------------------------------------------------------------------------
# 流式提前转发思考文本（_stream_chat）
# ---------------------------------------------------------------------------


def test_split_event_separates_thinking_and_answer():
    answer, thinking = _split_event(
        "plan_item",
        {
            "id": "t1",
            "thought": "思考内容",
            "reasoning_content": "推理内容",
            "tool_call_info": {"name": "finish", "params": {"summary": "pong"}},
        },
    )
    assert answer == "pong"
    assert "思考内容" in thinking
    assert "推理内容" in thinking


def test_split_event_skips_noise_events():
    assert _split_event("heartbeat", {"x": 1}) == ("", [])
    assert _split_event("token_usage", {"input": 1}) == ("", [])
    assert _split_event("status_changed", {"new_status": "running"}) == ("", [])


def _collect_stream(monkeypatch, fake_turn, order):
    monkeypatch.setattr(tw, "_run_turn", fake_turn)
    chunks = []

    async def consume():
        async for chunk in tw._stream_chat("请只回复：pong", "qwen-3.7-plus", "auto", None):
            chunks.append(chunk)
            order.append("chunk")

    asyncio.run(consume())
    text = "".join(chunks)
    payloads = [
        json.loads(line[5:].strip())
        for line in text.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]
    return payloads, text


def test_stream_chat_first_byte_before_turn_finishes(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        await asyncio.sleep(0.05)
        order.append("turn_done")
        return "ok", "pong"

    payloads, text = _collect_stream(monkeypatch, fake_turn, order)
    # 首包（role）必须先于回合完成发出
    assert order[0] == "chunk"
    assert payloads[0]["choices"][0]["delta"].get("role") == "assistant"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    assert "pong" in contents
    assert text.rstrip().endswith("data: [DONE]")


def test_stream_chat_forwards_thinking_early(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("用户要求只回复某个词")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    # R3：思考片段走 reasoning_content，最终回答走 content；且思考先于回答出现。
    # 在完整 payloads 序列里定位思考帧与最终内容帧的下标，确认思考先于回答。
    reasoning_idx = next(
        i for i, c in enumerate(payloads)
        if c["choices"][0]["delta"].get("reasoning_content", "").strip() == "用户要求只回复某个词"
    )
    content_idx = next(
        i for i, c in enumerate(payloads)
        if c["choices"][0]["delta"].get("content", "").strip() == "pong"
    )
    assert reasoning_idx < content_idx
    # 思考文本不得混入 content
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    assert "用户要求只回复某个词" not in [x for x in contents if x]


def test_stream_chat_answer_always_sent_as_content_even_if_same_as_thinking(monkeypatch):
    """R3：思考走 reasoning_content 后，最终回答仍必须以 content 下发。

    回归点：早期"答案已在转发内容里则不重复发"的守卫是为旧行为（思考混在 content）
    防重复用的。R3 把思考移到 reasoning_content 后二者不再共用通道，沿用该守卫会让
    不渲染 reasoning_content 的客户端**完全收不到回答**。参考 qodercn/traesolo：
    content 与 reasoning_content 独立转发，不做跨通道去重。
    """
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("pong")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    reasoning = [c["choices"][0]["delta"].get("reasoning_content") for c in payloads]
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    assert "pong" in reasoning
    # 关键：答案必须出现在 content 里（不得被思考去重守卫吞掉）
    assert "pong" in [x.strip() for x in contents if x]
    # 且流以正常 finish_reason=stop 收尾
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_chat_dedups_cumulative_thinking(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("思考第一步")
            await on_thinking("思考第一步。继续推理")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    reasoning = [c["choices"][0]["delta"].get("reasoning_content") for c in payloads]
    reasoning = [x.strip() for x in reasoning if x]
    # 第二段是累计重发，只转发增量；前缀不重复出现
    assert "思考第一步" in reasoning
    assert "。继续推理" in reasoning
    assert "".join(reasoning).count("思考第一步") == 1
    # 思考文本不出现在 content 里（R3 分离）
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    assert "思考第一步" not in [x for x in contents if x]


def test_stream_chat_error_surfaces_in_band(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        await asyncio.sleep(0.02)
        return ("error", (503, {"error": {"message": "No available accounts", "type": "channel_unavailable"}}))

    payloads, text = _collect_stream(monkeypatch, fake_turn, order)
    # 流内错误用 OpenAI 兼容的 error 对象承载，不再伪造正常回答
    contents = "".join(
        c["choices"][0]["delta"].get("content") or ""
        for c in payloads if "choices" in c
    )
    assert "上游处理失败" not in contents
    assert not any("choices" in c and c["choices"][0]["finish_reason"] == "stop" for c in payloads)
    assert any("error" in c for c in payloads)
    assert "No available accounts" in text
    assert "data: [DONE]" in text


# ---------------------------------------------------------------------------
# 新增回归：R2 is_in_code_mode / R4 token_usage / R5 finish_reason / R6 _new_piece
# ---------------------------------------------------------------------------


def test_r2_code_mode_sends_flag_on_sendmessage_only(isolated_db, monkeypatch):
    """R2：code 模式只在 sendMessage 顶层带 is_in_code_mode=True。

    **不再发送 createSession.initial_message**：官方 client 的 initial_message 是
    一个完整的发消息对象（buildSendMessageRequest 的产物），applyCodeModeFlagIfNeeded
    只往这个已存在的对象里补键。本网关首轮走独立的 sendMessage，塞一个只有
    {is_in_code_mode:true} 的桩对象属于协议违规（上游按"这里有一条待发消息"解析，
    缺 query/model_name 等必需字段即报 500 internal server error）。
    code 语义由 sendMessage 顶层标记表达，与官方 sendMessage 分支一致。
    """
    db.set_setting("traework.mode", "code")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)
    asyncio.run(tw._turn({"access_token": "tok", "extra": {}}, "hi", "qwen-3.7-plus", timeout=90.0))

    create_body = _post_body(fake, "/chat_sessions")
    # 关键回归：不得再伪造 initial_message（无论是否带 flag）
    assert "initial_message" not in create_body
    assert "is_in_code_mode" not in create_body
    msg_body = _post_body(fake, "/messages")
    assert msg_body["is_in_code_mode"] is True
    assert msg_body["agent_id"] == "solo_agent_lite"


def test_r2_work_mode_sends_flag_nowhere(isolated_db, monkeypatch):
    """R2：work 模式（默认）不发送 is_in_code_mode 字段，body 逐字段与改造前一致。"""
    db.set_setting("traework.mode", "work")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)
    asyncio.run(tw._turn({"access_token": "tok", "extra": {}}, "hi", "qwen-3.7-plus", timeout=90.0))

    create_body = _post_body(fake, "/chat_sessions")
    assert "initial_message" not in create_body
    assert "is_in_code_mode" not in create_body
    msg_body = _post_body(fake, "/messages")
    assert "is_in_code_mode" not in msg_body


def test_r4_token_usage_parsed_into_openai_json(isolated_db, monkeypatch):
    """R4：上游 token_usage 事件被解析进非流式 usage（input→prompt，output→completion，
    total=两者和）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = [
        "event: token_usage",
        'data: {"input_tokens": 12, "output_tokens": 34}',
        "event: done",
        "data: {}",
    ]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, body = asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    assert status == "json", body
    usage = body["usage"]
    assert usage["prompt_tokens"] == 12
    assert usage["completion_tokens"] == 34
    assert usage["total_tokens"] == 46


def test_r4_token_usage_absent_keeps_zeros(isolated_db, monkeypatch):
    """R4：无 token_usage 事件时 usage 仍是全 0（向后兼容，不破坏既有客户端）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = ["event: done", "data: {}"]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, body = asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    assert status == "json"
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_r4_token_usage_logged_real_tokens(isolated_db, monkeypatch):
    """R4：拿到 token_usage 真值时 _log 收到真实 usage（而非 None）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = [
        "event: token_usage",
        'data: {"input_tokens": 7, "output_tokens": 9}',
        "event: done",
        "data: {}",
    ]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    logged = []
    real_log = store_common.log_request

    async def spy_log(*a, **k):
        logged.append(k)
        await real_log(*a, **k)

    # 注意 spy 挂在 store_common.log_request 上（而非 tw._log）：token 三列 kwargs 是在
    # tw._log 内部拆出来再传给 log_request 的，只有在这一层才观察得到。
    monkeypatch.setattr(store_common, "log_request", spy_log)

    asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    assert logged, "应写入一条请求日志"
    # usage 透传进 log_request（非 None，且为真实 token 数）
    usage = logged[0].get("usage")
    assert isinstance(usage, dict)
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 9
    # 关键：token 还要**拆成独立 kwargs** 透传。log_request 的 prompt/completion/total
    # 三列只读 kwargs，credit 又只由 total_tokens 推导；只传 usage 会让 usage_json 有真值
    # 而三列与 credit 恒 0（与 qclaw/qwenwork 行为不一致）。
    assert logged[0].get("prompt_tokens") == 7
    assert logged[0].get("completion_tokens") == 9
    assert logged[0].get("total_tokens") == 16


def test_r4_token_usage_lands_in_db_columns_and_credit(isolated_db, monkeypatch):
    """R4 端到端：上游真值确实落进 logs 表的 token 三列，并据此算出 credit。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = [
        "event: token_usage",
        'data: {"input_tokens": 1000, "output_tokens": 2000}',
        "event: done",
        "data: {}",
    ]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    rows = db.list_recent_logs(limit=5) if hasattr(db, "list_recent_logs") else []
    if not rows:
        import sqlite3

        con = sqlite3.connect(db.DB_PATH)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "select prompt_tokens, completion_tokens, total_tokens, credit, usage_json "
            "from logs order by id desc limit 1"
        )]
        con.close()
    row = rows[0]
    assert row["prompt_tokens"] == 1000
    assert row["completion_tokens"] == 2000
    assert row["total_tokens"] == 3000
    # credit = total_tokens / channel_credit_rate(traework)；traework 默认 1000 token/credit
    assert row["credit"] == 3.0
    assert row["usage_json"]


def test_r4_token_usage_not_in_answer_text(isolated_db, monkeypatch):
    """R4：token_usage 事件不得被当正文拼进回答（含 payload 里的伪装字符串）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = [
        "event: token_usage",
        'data: {"input_tokens": 5, "output_tokens": 6, "note": "LEAK_MARKER"}',
        "event: done",
        'data: {"status": "completed"}',
    ]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    result = asyncio.run(tw._turn({"access_token": "tok", "extra": {}}, "hi", "qwen-3.7-plus", timeout=90.0))
    text = result[0] if isinstance(result, tuple) else result
    assert "LEAK_MARKER" not in text
    # 回答仍来自 GET /messages 的 pong（token_usage 未污染 pieces）
    assert text == "pong"


def test_r5_finish_reason_maps_from_done_status(isolated_db, monkeypatch):
    """R5：done.status 映射 finish_reason（completed→stop，error→error）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()

    captured = {}
    real_log = tw._log  # 捕获原始实现，避免 spy 自递归

    async def spy_log(*a, **k):
        # _log(api_key_info, account, model_name, stream, finish, status, error, t0, ...)
        # finish 是位置参数（第 5 个），不是关键字 finish_reason。
        captured["finish"] = a[4]
        await real_log(*a, **k)

    monkeypatch.setattr(tw, "_log", spy_log)

    for status, expect in (("completed", "stop"), ("error", "error"), ("cancelled", "error")):
        events = ["event: done", f'data: {{"status": "{status}"}}']
        fake = _FakeTraeClientEvents(events)
        monkeypatch.setattr(tw, "get_client", lambda: fake)
        st, body = asyncio.run(
            tw.chat_completions(
                {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                None,
            )
        )
        assert st == "json", body
        assert body["choices"][0]["finish_reason"] == expect, (status, body)
        assert captured["finish"] == expect


def test_r5_finish_reason_default_stop_when_absent(isolated_db, monkeypatch):
    """R5：done 事件缺 status（或不识别值）时回退默认 stop。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = ["event: done", "data: {}"]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    st, body = asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    assert st == "json"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_r6_new_piece_overlapping_fragment(isolated_db):
    """R6：非前缀重叠片段既不被重复拼出，也不被吞掉新文字。

    上游先发完整段 'ABCDEF'，再发 'CDEFGH'（重叠 'CDEF'）：应只转发尾部 'GH'，
    而非重发整段，也不因误判为收缩而丢弃 'GH'。
    """
    # 标准累计扩展：应只返回新增尾部
    assert tw._new_piece("ABC", "ABCDEFG") == "DEFG"
    # 标准收缩：返回空（已显示过）
    assert tw._new_piece("ABCDEFG", "ABC") == ""
    # 非前缀重叠：去掉最大重叠 CDEF 后转发 GH
    assert tw._new_piece("ABCDEF", "CDEFGH") == "GH"
    # 不重复：拼接结果不含两段重叠区
    assert ("ABCDEF" + tw._new_piece("ABCDEF", "CDEFGH")).count("CDEF") == 1


def test_r4_stream_terminal_chunk_carries_usage(isolated_db, monkeypatch):
    """R4：流式末帧在拿到 token_usage 时带 usage（非流式之外也要覆盖流路径）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = [
        "event: token_usage",
        'data: {"input_tokens": 3, "output_tokens": 4}',
        "event: done",
        'data: {"status": "completed"}',
    ]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    async def consume():
        out = []
        async for chunk in tw._stream_chat("hi", "qwen-3.7-plus", "auto", None):
            out.append(chunk)
        return out

    chunks = asyncio.run(consume())
    terminal = None
    for chunk in chunks:
        if chunk.startswith("data:") and chunk[5:].strip() != "[DONE]":
            payload = json.loads(chunk[5:].strip())
            if payload.get("choices", [{}])[0].get("finish_reason"):
                terminal = payload
    assert terminal is not None, "应有带 finish_reason 的末帧"
    assert terminal["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    assert terminal["choices"][0]["finish_reason"] == "stop"



# ---------------------------------------------------------------------------
# 新增回归：R1 入参保真（system + 多轮历史压平进单条 query）
# ---------------------------------------------------------------------------


def _build_query_json(payload: dict) -> str:
    """复刻 _turn 里 query 的构造：单元素 list 包一层 text item。"""
    prompt, _has_user = tw._build_prompt(payload)
    return json.dumps(
        [{"type": "text", "data": {"content": prompt}}],
        ensure_ascii=False,
    )


def test_r1_single_user_no_system_is_byte_identical(isolated_db):
    """R1 硬约束：无 system + 单条 user 消息时，prompt 与用户文本逐字节相同。"""
    payload = {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "只回复：pong"}]}
    prompt, has_user = tw._build_prompt(payload)
    assert has_user is True
    assert prompt == "只回复：pong"
    # query 形状仍是 list-of-one-text-item，且 content 等于原文
    query = _build_query_json(payload)
    parsed = json.loads(query)
    assert parsed == [{"type": "text", "data": {"content": "只回复：pong"}}]


def test_r1_system_and_history_flattened_into_query(isolated_db):
    """R1：system + 多轮历史都进 query；query 仍是合法 list-of-one-text-item。"""
    payload = {
        "model": "qwen-3.7-plus",
        "messages": [
            {"role": "system", "content": "你是一个严谨的助手"},
            {"role": "user", "content": "什么是光年？"},
            {"role": "assistant", "content": "光年是距离单位"},
            {"role": "user", "content": "那速度呢？"},
        ],
    }
    prompt, has_user = tw._build_prompt(payload)
    assert has_user is True
    # system 与历史都被保留（模型视角与兄弟通道转发 messages 数组一致）
    assert "你是一个严谨的助手" in prompt
    assert "什么是光年？" in prompt
    assert "光年是距离单位" in prompt
    assert "那速度呢？" in prompt
    # 仍为单用户轮语义（最后一句仍是当前提问），且 query 形状合法
    query = _build_query_json(payload)
    parsed = json.loads(query)
    assert isinstance(parsed, list) and len(parsed) == 1
    assert parsed[0] == {"type": "text", "data": {"content": prompt}}


def test_r1_multiple_system_joined_by_blank_line(isolated_db):
    """R1：多条 system 以空行拼接（与 qwenwork._split_messages 一致）。"""
    payload = {
        "model": "qwen-3.7-plus",
        "messages": [
            {"role": "system", "content": "规则一"},
            {"role": "system", "content": "规则二"},
            {"role": "user", "content": "开始"},
        ],
    }
    prompt, _has_user = tw._build_prompt(payload)
    assert "规则一" in prompt and "规则二" in prompt
    # 两条 system 以空行分隔（非简单拼接，也不带标记）
    assert "规则一\n\n规则二" in prompt
    assert prompt.endswith("开始")


def test_r1_list_content_parts_text_concatenated_image_ignored(isolated_db):
    """R1：list content 里文本零件拼接、图片零件忽略且不崩。"""
    payload = {
        "model": "qwen-3.7-plus",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图回答："},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                    {"type": "text", "text": "这是什么？"},
                ],
            }
        ],
    }
    prompt, has_user = tw._build_prompt(payload)
    assert has_user is True
    # 文本零件拼接；图片零件被忽略（本协议无法承载）
    assert prompt == "看图回答：这是什么？"


def test_r1_no_user_turn_reports_400(isolated_db):
    """R1：无 user 轮时仍按既有语义回 400（chat_completions 据此拦截）。"""
    payload = {"model": "qwen-3.7-plus", "messages": [{"role": "system", "content": "只有系统提示"}]}
    prompt, has_user = tw._build_prompt(payload)
    # 无 user 轮 → has_user 为 False；chat_completions 据此回 400（system 文本本身
    # 仍会被压平进 prompt，但缺 user 轮不构成合法请求）。
    assert has_user is False


def test_r1_only_assistant_or_tool_still_400(isolated_db):
    """R1 回归：has_user 只看 user 轮，与改造前 _last_user_text 语义一致。

    早期实现把 has_user 写成 "turns 非空"，于是只有 assistant / tool 轮的请求
    不再回 400（校验被放宽）。这里锁死：缺 user 轮一律 False。
    """
    for msgs in (
        [{"role": "assistant", "content": "我先说话"}],
        [{"role": "tool", "content": "工具结果"}],
        [{"role": "assistant", "content": "a"}, {"role": "tool", "content": "b"}],
    ):
        _prompt, has_user = tw._build_prompt({"model": "m", "messages": msgs})
        assert has_user is False, f"缺 user 轮应回 400，但被放行: {msgs}"


def test_r1_byte_identical_keeps_surrounding_whitespace(isolated_db):
    """R1 硬约束：单 user、无 system 时必须逐字节一致，含首尾空白（不得 strip）。"""
    for text in ("hello", "  hello  ", "\nhello\n", "line1\nline2", "  "):
        prompt, has_user = tw._build_prompt(
            {"model": "m", "messages": [{"role": "user", "content": text}]}
        )
        if text.strip():
            assert prompt == text, f"未逐字节保留: {text!r} -> {prompt!r}"
            assert has_user is True
        else:
            # 纯空白 user 文本视为无有效 user 轮（与旧实现"取不到文本"一致）
            assert has_user is False



# ---------------------------------------------------------------------------
# 新增回归：R3 思考走 reasoning_content
# ---------------------------------------------------------------------------


def test_r3_nonstream_reasoning_content_present(isolated_db, monkeypatch):
    """R3：非流式拿到思考时，message.reasoning_content 必须呈现（与兄弟通道一致）。"""
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()
    events = [
        "event: plan_item",
        "data: " + json.dumps(
            {
                "id": "p1",
                "thought": "",
                "reasoning_content": "让我先想想思路",
                "tool_call_info": {"name": "finish", "params": {"summary": "pong"}},
                "result": {"status": "success"},
            },
            ensure_ascii=False,
        ),
        "event: token_usage",
        'data: {"input_tokens": 1, "output_tokens": 2}',
        "event: done",
        'data: {"status": "completed"}',
    ]
    fake = _FakeTraeClientEvents(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, body = asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    assert status == "json", body
    message = body["choices"][0]["message"]
    assert message["content"] == "pong"
    # 思考文本独立成字段，绝不混入 content
    assert message.get("reasoning_content") == "让我先想想思路"


def test_r3_nonstream_fallback_answer_not_duplicated_into_reasoning(isolated_db, monkeypatch):
    """R3：当最终答案来自思考兜底（GET /messages 无 finish/正文，只有思考文本）时，
    同一文本不得同时占 content 与 reasoning_content。

    这里需要 GET /messages 返回的助手消息本身是「只有 reasoning_content、无 finish」
    的 plan_item：extract_assistant_text 会退回思考文本作答案；同时事件流也产出同样的
    思考文本，故 thinking_accum 与最终答案相等，`_openai_json` 的去重守卫应跳过
    reasoning_content（否则同一段既当答案又当思考）。
    """
    db.set_setting("traework.mode", "work")
    _add_one_traework_account()

    class _FakeTraeClientFallback(_FakeTraeClientEvents):
        async def get(self, url, *, headers=None, timeout=None):
            if url.endswith("/messages"):
                return _FakeTraeResponse(
                    payload={"code": 0, "data": {"items": [
                        {"role": "assistant", "message_type": "task",
                         "content": json.dumps(
                             {"task_id": "t", "messages": [
                                 {"type": "plan_item", "plan_item": {
                                     "thought": "",
                                     "reasoning_content": "唯一的回答",
                                     "tool_call_info": {"name": "web_search", "params": {"query": "x"}},
                                 }}],
                             }, ensure_ascii=False)},
                    ]}}
                )
            return _FakeTraeResponse()

    events = [
        "event: plan_item",
        "data: " + json.dumps(
            {
                "id": "p1",
                "thought": "",
                "reasoning_content": "唯一的回答",
                "tool_call_info": {"name": "web_search", "params": {"query": "x"}},
            },
            ensure_ascii=False,
        ),
        "event: done",
        "data: {}",
    ]
    fake = _FakeTraeClientFallback(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, body = asyncio.run(
        tw.chat_completions(
            {"model": "qwen-3.7-plus", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            None,
        )
    )
    assert status == "json", body
    message = body["choices"][0]["message"]
    # 答案来自思考兜底 → content 承载它，reasoning_content 不得重复同一段
    assert message["content"] == "唯一的回答"
    assert "reasoning_content" not in message


def test_r3_stream_reasoning_and_final_content_separated(monkeypatch):
    """R3 流式端到端：思考增量走 reasoning_content，最终答案走 content。"""
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("先规划再回答")
        await asyncio.sleep(0.02)
        return "ok", "pong", None, "stop", "先规划再回答"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    reasoning = [c["choices"][0]["delta"].get("reasoning_content") for c in payloads]
    reasoning = [x.strip() for x in reasoning if x]
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    contents = [x.strip() for x in contents if x]
    # 思考片段走 reasoning_content，最终答案（pong）走 content
    assert "先规划再回答" in reasoning
    assert "pong" in contents
    # content 里不得出现思考文本（R3 分离，绝不混入）
    assert "先规划再回答" not in contents



# ---------------------------------------------------------------------------
# 测试替身：自包含跑完 _turn（建会话 → 发消息 → 收流 → GET /messages）
# ---------------------------------------------------------------------------


class _FakeTraeResponse:
    def __init__(self, *, status_code=200, payload=None, content=True):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content

    def json(self):
        return self._payload

    async def aiter_lines(self):
        """SSE 行迭代器：与 _turn 里 `async for line in response.aiter_lines()` 契约一致。"""
        yield "event: done"
        yield "data: {}"


class _FakeTraeStream:
    """模拟 client.stream("GET", .../events) 的异步上下文管理器：发一条 done 事件即结束。

    契约必须是 `async def __aenter__` 且返回带 `status_code` / `aiter_lines()` 的
    响应对象（对应 `_turn` 中的 `async with client.stream(...) as response`）。
    旧实现把 `__aenter__` 写成同步并返回 self：既不是 awaitable，也没有
    `aiter_lines`，于是 read_events 抛 TypeError/AttributeError（不是
    httpx.HTTPError，不会被吞掉），`finished` 永不置位，每个用例只能硬等满
    90s 超时——整个文件看起来像"卡死"。
    """

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeTraeClient:
    """记录所有 POST 的 (url, json)，并让 _turn 走完标准 happy path。"""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/chat_sessions"):
            return _FakeTraeResponse(payload={"code": 0, "data": {"chat_session_id": "sid-1"}})
        if url.endswith("/messages"):
            return _FakeTraeResponse(payload={"code": 0, "data": {}})
        return _FakeTraeResponse()

    async def get(self, url, *, headers=None, timeout=None):
        if url.endswith("/messages"):
            return _FakeTraeResponse(
                payload={"code": 0, "data": {"items": [
                    {"role": "assistant", "message_type": "task",
                     "content": json.dumps(
                         {"task_id": "t", "messages": [
                             {"type": "text", "text_content": "pong"}]},
                         ensure_ascii=False)},
                ]}}
            )
        return _FakeTraeResponse()

    def stream(self, method, url, *, headers=None, timeout=None):
        return _FakeTraeStream(_FakeTraeResponse())

    async def delete(self, url, *, headers=None, timeout=None):
        return _FakeTraeResponse()


class _FakeTraeResponseEvents(_FakeTraeResponse):
    """SSE 响应：按给定事件行序列迭代（event:/data: 成对出现）。"""

    def __init__(self, events):
        super().__init__()
        self._events = events

    async def aiter_lines(self):
        for ev in self._events:
            yield ev


class _FakeTraeClientEvents(_FakeTraeClient):
    """建会话/发消息走 happy path，SSE 流由传入的 events 决定内容。"""

    def __init__(self, events):
        super().__init__()
        self._events = events

    def stream(self, method, url, *, headers=None, timeout=None):
        return _FakeTraeStream(_FakeTraeResponseEvents(self._events))


class _FakeTraeClientEventsFailOnce(_FakeTraeClientEvents):
    """首个建会话 POST 返回 401（触发凭据自愈），其后按 events 正常返回。

    用于验证自愈重试成功时，usage / finish_reason 仍能透传（见
    test_run_turn_selfheal_retry_keeps_usage_and_finish）。
    """

    def __init__(self, events):
        super().__init__(events)
        self.fail_next_session = True

    async def post(self, url, *, headers=None, json=None, timeout=None):
        if url.endswith("/chat_sessions") and self.fail_next_session:
            self.posts.append((url, json))
            self.fail_next_session = False
            return _FakeTraeResponse(status_code=401, payload={})
        return await super().post(url, headers=headers, json=json, timeout=timeout)


def _add_one_traework_account() -> int:
    return _add_traework()


# ---------------------------------------------------------------------------
# TraeWork 凭据自救（spec §3.2）与启动对齐（spec §3.3）测试
# 不使用真实网络 / 真实凭据；storage.json 由测试本地构造并解密。
# 临时目录放在仓库 .tmp 下（沙箱禁止写入系统 TEMP，故不用 pytest tmp_path）。
# ---------------------------------------------------------------------------


def _encrypt_tc_b64(plain_text: str) -> str:
    """构造与 crypto.decrypt_tc_b64 互逆的加密 blob（仅测试用）。"""
    import base64
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    key_mat = os.urandom(32)
    mixed = hashlib.sha512(key_mat).digest() + _SALT
    derived = hashlib.sha512(mixed).digest()
    aes_key, iv = derived[:16], derived[16:32]
    body = plain_text.encode("utf-8")
    plain = hashlib.sha512(body).digest() + body
    padder = PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    blob = _MAGIC + key_mat + cipher
    return base64.b64encode(blob).decode("ascii")


def _write_storage(auth_dir: Path, *, uid, access, refresh, expired_at_iso) -> Path:
    """写一个可被 import_discovered 解密的 storage.json（只读，不写回客户端）。"""
    document = {
        "token": access,
        "refreshToken": refresh,
        "userId": uid,
        "expiredAt": expired_at_iso,
        "host": "https://api.trae.cn",
        "account": {"username": "tester"},
    }
    storage = {"iCubeAuthInfo://icube.cloudide": _encrypt_tc_b64(json.dumps(document))}
    path = auth_dir / "storage.json"
    path.write_text(json.dumps(storage), encoding="utf-8")
    return path


@pytest.fixture()
def client_auth_dir(monkeypatch):
    # 放在仓库 .tmp 下以避免沙箱对系统 TEMP 的写入限制。
    base = Path(__file__).resolve().parent.parent / ".tmp" / f"tw-adopt-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(base))
    yield base
    import shutil

    shutil.rmtree(base, ignore_errors=True)


def _add_traework(uid="3577", **overrides):
    data = {
        "name": "tw-acc",
        "provider": "traework",
        "uid": uid,
        "status": "active",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": 4_000_000_000_000,  # 远未来，避免 pick 触发主动刷新
        "extra": {},
    }
    data.update(overrides)
    return db.add_account(data)


# ---------------------------------------------------------------------------
# §5.1 自愈：refresh 失败 + 客户端更新凭据 → 被采用、状态回 active
# ---------------------------------------------------------------------------


def test_adopt_updated_credentials_from_client(isolated_db, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(
        status="expired",
        extra={"auth_path": str(path)},
    )
    # 初始：expired + 旧凭据
    before = db.get_account(aid)
    assert before["status"] == "expired"
    assert before["access_token"] == "old-access"

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is True
    after = db.get_account(aid)
    assert after["status"] == "active"
    assert after["access_token"] == "new-access"
    assert after["refresh_token"] == "new-refresh"
    # 不应整行覆盖、不应改 uid
    assert after["uid"] == "3577"


# ---------------------------------------------------------------------------
# §5.1 集成：_turn 因鉴权失效失败 → 自愈 → 用新凭据重试成功
# ---------------------------------------------------------------------------


class _FakeTraeClientFailOnce(_FakeTraeClient):
    """首个建会话 POST 返回 401（模拟 refresh 失效），其后恢复正常 happy path。"""

    def __init__(self):
        super().__init__()
        self.fail_next_session = True

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/chat_sessions") and self.fail_next_session:
            self.fail_next_session = False
            return _FakeTraeResponse(status_code=401, payload={})
        if url.endswith("/chat_sessions"):
            return _FakeTraeResponse(payload={"code": 0, "data": {"chat_session_id": "sid-1"}})
        if url.endswith("/messages"):
            return _FakeTraeResponse(payload={"code": 0, "data": {}})
        return _FakeTraeResponse()


def test_run_turn_self_heals_and_retries(isolated_db, monkeypatch, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(extra={"auth_path": str(path)})
    fake = _FakeTraeClientFailOnce()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    # _run_turn 现返回 ("ok", text, usage, finish_reason, reasoning)
    status, text, _usage, _finish, _reasoning = asyncio.run(
        tw._run_turn("hi", "qwen-3.7-plus", "auto", None, stream=False)
    )

    assert status == "ok", text
    assert text == "pong"
    # 建会话 POST 出现两次：首次失败触发自愈，重试一次成功
    session_posts = [u for u, _ in fake.posts if u.endswith("/chat_sessions")]
    assert len(session_posts) == 2
    after = db.get_account(aid)
    assert after["status"] == "active"
    assert after["access_token"] == "healed-access"


def test_run_turn_selfheal_retry_keeps_usage_and_finish(isolated_db, monkeypatch, client_auth_dir):
    """自愈重试成功时，usage / finish_reason 不得被丢成 (None, stop)。

    回归点：_adopt_and_retry 早期只回传 text，导致「自愈成功」这一分支落库与响应都拿不到
    上游 token 真值。返回三元组后，usage 应一路透传到 _log。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    _add_traework(extra={"auth_path": str(path)})
    events = [
        "event: token_usage",
        'data: {"input_tokens": 11, "output_tokens": 22}',
        "event: done",
        'data: {"status": "completed"}',
    ]
    fake = _FakeTraeClientEventsFailOnce(events)
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    logged = []
    real_log = store_common.log_request

    async def spy_log(*a, **k):
        logged.append(k)
        await real_log(*a, **k)

    monkeypatch.setattr(store_common, "log_request", spy_log)

    status, text, usage, finish, _reasoning = asyncio.run(
        tw._run_turn("hi", "qwen-3.7-plus", "auto", None, stream=False)
    )

    assert status == "ok", text
    assert text == "pong"
    assert usage == {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
    assert finish == "stop"
    # 成功落库那一条必须带真实 token 三列
    ok_rows = [k for k in logged if k.get("status_code") == 200]
    assert ok_rows, "应有一条 200 落库"
    assert ok_rows[-1].get("total_tokens") == 33
    assert ok_rows[-1].get("usage") == usage


# ---------------------------------------------------------------------------
# §5.2 uid 不一致 → 不接管、维持原状
# ---------------------------------------------------------------------------


def test_adopt_rejects_uid_mismatch(isolated_db, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="9999",  # 与账号 uid 3577 不一致
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    after = db.get_account(aid)
    assert after["status"] == "expired"  # 维持原状
    assert after["access_token"] == "old-access"


# ---------------------------------------------------------------------------
# §5.3 文件不可读 / 解密失败 / 路径越权 → 不接管、不抛异常（best-effort）
# ---------------------------------------------------------------------------


def test_adopt_skip_missing_file(isolated_db, client_auth_dir):
    missing = client_auth_dir / "storage.json"  # 不存在
    aid = _add_traework(status="expired", extra={"auth_path": str(missing)})
    before = db.get_account(aid)

    # 不得抛异常
    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["access_token"] == "old-access"


def test_adopt_skip_corrupt_storage(isolated_db, client_auth_dir):
    path = client_auth_dir / "storage.json"
    path.write_text(json.dumps({"iCubeAuthInfo://icube.cloudide": "not-a-tc-blob"}), encoding="utf-8")
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["access_token"] == "old-access"


def test_adopt_skip_path_outside_whitelist(isolated_db, client_auth_dir, monkeypatch):
    # 把白名单指到别的目录，使 client_auth_dir 下的文件越权 → import_discovered 抛错
    other = client_auth_dir.parent / f"tw-other-{uuid.uuid4().hex[:8]}"
    other.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(other))
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    import shutil

    shutil.rmtree(other, ignore_errors=True)


# ---------------------------------------------------------------------------
# §5.4 客户端凭据无更新 → 不接管（避免无谓轮换）
# ---------------------------------------------------------------------------


def test_adopt_skip_when_no_update(isolated_db, client_auth_dir):
    # 客户端凭据与 DB 完全一致：token 相同，且 expires_at 不大于 DB 的远未来值
    # （DB 的 4e12ms ≈ 2096，这里用更小的 2026，避免 expires_at 触发"更新"判断）。
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="old-access",  # 与 DB 完全相同
        refresh="old-refresh",
        expired_at_iso="2026-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["status"] == "expired"


# ---------------------------------------------------------------------------
# §5.3 / §3.3 启动对齐：客户端更新时接管；失败时不抛、不阻断
# ---------------------------------------------------------------------------


def test_startup_align_adopts_updated(isolated_db, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="aligned-access",
        refresh="aligned-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})

    adopted = tw_server._align_traework_credentials()

    assert adopted >= 1
    after = db.get_account(aid)
    assert after["status"] == "active"
    assert after["access_token"] == "aligned-access"


def test_startup_align_silent_on_bad_path(isolated_db, client_auth_dir):
    missing = client_auth_dir / "storage.json"
    aid = _add_traework(status="expired", extra={"auth_path": str(missing)})

    # 失败必须静默：不抛、不阻断，返回 0
    adopted = tw_server._align_traework_credentials()

    assert adopted == 0
    assert db.get_account(aid)["status"] == "expired"


# ---------------------------------------------------------------------------
# require_newer：启动对齐只认「明确更新」，不得把新票换成客户端旧票
# ---------------------------------------------------------------------------


def test_startup_align_rejects_older_client_credentials(isolated_db, client_auth_dir):
    """客户端 token 与 DB 不同但 expires_at 更旧 → 启动对齐不得接管。

    否则每次重启都会把网关刚刷新好的新票换成客户端的旧票，反而弄坏可用凭据。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="stale-client-access",
        refresh="stale-client-refresh",
        expired_at_iso="2026-01-01T00:00:00Z",  # 远旧于 DB 的 2096
    )
    aid = _add_traework(extra={"auth_path": str(path)})
    before = db.get_account(aid)
    assert before["access_token"] == "old-access"

    # 启动对齐（require_newer=True）
    adopted = tw_server._align_traework_credentials()

    assert adopted == 0
    after = db.get_account(aid)
    assert after["access_token"] == "old-access"  # 未被替换
    assert after["refresh_token"] == "old-refresh"


def test_selfheal_still_accepts_different_token(isolated_db, client_auth_dir):
    """自愈路径（require_newer=False）仍接受「不同即接管」——旧票已判废。

    与上一条对照，确保 require_newer 只收紧了启动对齐、没削弱自愈。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2026-01-01T00:00:00Z",  # 即便更旧
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))  # require_newer 默认 False

    assert adopted is True
    after = db.get_account(aid)
    assert after["access_token"] == "healed-access"
    assert after["status"] == "active"


# ---------------------------------------------------------------------------
# patch 安全性：空 token 不覆盖、expires_at 解析失败不得把有效值砸成 0
# ---------------------------------------------------------------------------


def test_adopt_does_not_clobber_expires_at_when_client_lacks_it(
    isolated_db, client_auth_dir, monkeypatch
):
    """客户端凭据缺 expires_at 时，不得把 DB 里的有效过期时间覆盖成 0。

    回归点：早期实现无条件写 expires_at=int(parsed.get(...) or 0)，
    会把有效值砸成 0，令 is_token_expired 判定错乱。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="newer-access",
        refresh="newer-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)
    original_exp = before["expires_at"]
    assert original_exp  # 有值

    # 模拟「客户端凭据解析不出 expires_at」：直接改 adopt 内部读到的 parsed
    real_import = tw_token.import_discovered
    monkeypatch.setattr(
        tw_token, "import_discovered",
        lambda p: {**real_import(p), "expires_at": 0, "refresh_expires_at": 0},
    )

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is True  # token 不同 → 自愈仍接管
    after = db.get_account(aid)
    assert after["expires_at"] == original_exp  # 未被砸成 0
    assert after["access_token"] == "newer-access"


def test_adopt_rejects_credentials_without_any_token(isolated_db, client_auth_dir, monkeypatch):
    """客户端凭据里没有任何 token → 不接管，不得把账号复活成空凭据。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="x",
        refresh="y",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    real_import = tw_token.import_discovered
    monkeypatch.setattr(
        tw_token, "import_discovered",
        lambda p: {**real_import(p), "access_token": "", "refresh_token": ""},
    )

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    after = db.get_account(aid)
    assert after["status"] == "expired"
    assert after["access_token"] == "old-access"


def test_adopt_rejects_empty_client_uid(isolated_db, client_auth_dir, monkeypatch):
    """客户端 uid 解析为空时无法核对身份 → 拒绝接管（不默认放行）。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    real_import = tw_token.import_discovered
    monkeypatch.setattr(
        tw_token, "import_discovered",
        lambda p: {**real_import(p), "uid": ""},
    )

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["access_token"] == "old-access"


# ---------------------------------------------------------------------------
# 自救重试的异常收敛：重试时非鉴权异常也必须降级为 503，不得穿透成 500
# ---------------------------------------------------------------------------


class _FakeTraeClientFailThenBoom(_FakeTraeClient):
    """首次建会话 401（触发自愈），重试用新凭据时抛 httpx.HTTPError。"""

    def __init__(self):
        super().__init__()
        self.fail_next_session = True

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/chat_sessions") and self.fail_next_session:
            self.fail_next_session = False
            return _FakeTraeResponse(status_code=401, payload={})
        if url.endswith("/chat_sessions"):
            raise httpx.ConnectError("boom after self-heal")
        return _FakeTraeResponse()


def test_selfheal_retry_network_error_degrades_to_503(
    isolated_db, monkeypatch, client_auth_dir
):
    """自愈成功后的重试若网络异常，必须收敛为 503（不得穿透成 500）。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    _add_traework(extra={"auth_path": str(path)})
    fake = _FakeTraeClientFailThenBoom()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, detail = asyncio.run(
        tw._run_turn("hi", "qwen-3.7-plus", "auto", None, stream=False)
    )

    assert status == "error"
    code, payload = detail
    assert code == 503, payload
    assert payload["error"]["type"] == "server_error"


# ---------------------------------------------------------------------------
# 主症状修复：token 过期 → pick 内 refresh 失败 → 就地自救（不必等 _turn）
#
# 这是用户实际遇到的「自动退出登录」路径：账号 token 过期后，pick 里 refresh
# 401，旧实现只进负缓存然后一路 503（需人工重导）。现在应在 pick 阶段就
# 从客户端 storage.json 接管凭据并返回可用账号。
# ---------------------------------------------------------------------------


def test_pick_self_heals_from_client_when_refresh_fails(
    isolated_db, monkeypatch, client_auth_dir
):
    """refresh 失败时，provider 的 pick 路径应自救成功并返回可用账号。"""
    from providers.traework import PROVIDER
    from providers.traework import token as twt

    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="picked-access",
        refresh="picked-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(
        status="expired",
        expires_at=1_000,  # 已过期，会走 refresh 路径
        extra={"auth_path": str(path)},
    )

    async def _boom(_account):
        raise twt.TraeWorkAuthError("refresh token is invalid")

    monkeypatch.setattr(twt, "refresh_account", _boom)
    # provider facade 持有的引用也要换（它 import 的是同名符号）
    monkeypatch.setattr(
        "providers.traework.refresh_account", _boom, raising=False
    )

    picked = asyncio.run(PROVIDER.pick_account_with_fallback())

    assert picked is not None, "refresh 失败后应从客户端 storage.json 自救成功"
    assert picked["access_token"] == "picked-access"
    assert db.get_account(aid)["status"] == "active"


def test_pick_without_adopt_fn_keeps_legacy_semantics(isolated_db, monkeypatch):
    """未传 adopt_fn 时，pick_with_refresh_fallback 行为与改动前一致（返回 None）。

    回归护栏：其余四家 facade 不传 adopt_fn，语义不得被本次改动影响。
    """
    from providers import trae_shared
    from providers.traework import token as twt

    _add_traework(status="expired", expires_at=1_000)

    async def _boom(_account):
        raise twt.TraeWorkAuthError("refresh token is invalid")

    trae_shared.reset_refresh_failures()
    picked = asyncio.run(
        trae_shared.pick_with_refresh_fallback("traework", _boom)
    )

    assert picked is None  # 没有 adopt_fn → 维持旧的"失败即 None"语义


# ---------------------------------------------------------------------------
# 管理页「测试」按钮也必须自愈
#
# test_chat 直接拿账号调 _turn，绕过 _pick/_run_turn；若不自救，凭据被客户端
# 轮换后「测试」会永远失败 —— 而它恰是用户判断通道可用性的主要入口。
# ---------------------------------------------------------------------------


def test_test_chat_self_heals_from_client(isolated_db, monkeypatch, client_auth_dir):
    """凭据失效时，「测试」应自救成功并返回 200 + 回答，而不是一直 503。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(extra={"auth_path": str(path)})
    account = db.get_account(aid)

    fake = _FakeTraeClientFailOnce()  # 首次建会话 401，其后 happy path
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    result = asyncio.run(tw.test_chat(account, model="qwen-3.7-plus"))

    assert result["ok"] is True, result
    assert result["status_code"] == 200
    assert result["message"] == "pong"
    # 自救确实发生了：账号被接管为新凭据
    assert db.get_account(aid)["access_token"] == "healed-access"


def test_test_chat_returns_503_when_no_credentials_to_heal(
    isolated_db, monkeypatch, client_auth_dir
):
    """无自救素材（客户端 storage.json 缺主凭据）时，测试仍如实报 503，不谎报成功。"""
    missing = client_auth_dir / "storage.json"  # 不存在
    aid = _add_traework(extra={"auth_path": str(missing)})
    account = db.get_account(aid)

    fake = _FakeTraeClientFailOnce()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    result = asyncio.run(tw.test_chat(account, model="qwen-3.7-plus"))

    assert result["ok"] is False
    assert result["status_code"] == 503
    # 不得因为自救失败而把账号凭据改动
    assert db.get_account(aid)["access_token"] == "old-access"
