import asyncio
import json
from pathlib import Path

import pytest

from storage import database as db
import providers
from gateway import router
from providers.protocol import UnknownModel
from providers.traework import chat as tw
from providers.traework.chat import _split_event, _text_from_event, extract_assistant_text, translate_model
from providers.traework.crypto import decrypt_tc_b64
from providers.traework.store import parse_credentials, traework_auth_dirs


@pytest.fixture()
def traework_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def test_traework_in_default_registry(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    assert providers.enabled_provider_ids() == ["workbuddy", "qclaw", "qwenwork", "traework", "traesolo"]
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
    raw = [c["choices"][0]["delta"].get("content") for c in payloads]
    contents = [x.strip() for x in raw if x]
    # 思考片段先于最终回答出现
    assert contents.index("用户要求只回复某个词") < contents.index("pong")


def test_stream_chat_no_duplicate_answer(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("pong")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    # 答案已包含在转发过的思考文本里，不再重复发
    assert contents.count("pong") == 1


def test_stream_chat_dedups_cumulative_thinking(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("思考第一步")
            await on_thinking("思考第一步。继续推理")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    raw = [c["choices"][0]["delta"].get("content") for c in payloads]
    contents = [x.strip() for x in raw if x]
    # 第二段是累计重发，只转发增量；前缀不重复出现
    assert "思考第一步" in contents
    assert "。继续推理" in contents
    assert "".join(contents).count("思考第一步") == 1


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
