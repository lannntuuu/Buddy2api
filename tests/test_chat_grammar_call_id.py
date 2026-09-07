"""tool call 流 call id 兼容性测试（hy4 系上游偶发空 id）。

背景：上游（hy4 系实测）在工具调用流中会发 id="" 的 delta 帧
（id 稍后帧才带上，甚至整条流不带）。语法层此前直接判死整条流
→ 502 "invalid call id"。现约定：
- 流中途空 id = 视作"未携带"，不判死；
- 流结束仍无 id = 按位置合成占位 id（call_<choice>_<pos>）；
- 非字符串 id、中途换 id 仍照旧报错。
"""

from __future__ import annotations

import json

from upstream.chat_grammar import ChatStreamObserver


def _chunk(delta: dict, finish_reason=None) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-x",
            "object": "chat.completion.chunk",
            "model": "hy4-preview",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _tool_delta(call_id, name="get_weather", arguments='{"city": "北京"}') -> dict:
    tool = {"index": 0, "type": "function",
            "function": {"name": name, "arguments": arguments}}
    if call_id is not ...:
        tool["id"] = call_id
    return tool


def _finish_chunk() -> bytes:
    return _chunk({}, finish_reason="tool_calls")


def _observe_all(*events: bytes) -> ChatStreamObserver:
    obs = ChatStreamObserver("hy4-preview")
    for event in events:
        obs.observe_event(event)
    return obs


def test_empty_id_delta_tolerated_then_real_id():
    """/空 id 帧不判死，后续真实 id 正常采信。"""
    obs = _observe_all(
        _chunk({"tool_calls": [_tool_delta("")]}),
        # 真实上游的分帧：id 与参数分帧携带，此处后续帧只补 id
        _chunk({"tool_calls": [{"index": 0, "id": "call_real_1"}]}),
        _finish_chunk(),
    )
    assert obs.parser_error is None
    assert obs.eof_error() is None
    state = obs.tool_calls[(0, 0)]
    assert state["id"] == "call_real_1"


def test_missing_id_synthesized_at_eof():
    """整条流都不带 id：eof 按位置合成占位 id，不 502。"""
    obs = _observe_all(
        _chunk({"tool_calls": [_tool_delta(...)]}),
        _finish_chunk(),
    )
    assert obs.parser_error is None
    assert obs.eof_error() is None
    assert obs.tool_calls[(0, 0)]["id"] == "call_0_0"


def test_empty_id_only_still_synthesizes():
    """只有 id="" 的帧且无后续 id：同样走 eof 合成。"""
    obs = _observe_all(
        _chunk({"tool_calls": [_tool_delta("")]}),
        _finish_chunk(),
    )
    assert obs.parser_error is None
    assert obs.eof_error() is None
    assert obs.tool_calls[(0, 0)]["id"] == "call_0_0"


def test_non_string_id_still_rejected():
    """非字符串 id 仍判死（协议破坏，不兼容）。"""
    obs = _observe_all(
        _chunk({"tool_calls": [_tool_delta(123)]}),
    )
    assert obs.parser_error == "The upstream tool call stream had an invalid call id."
    assert obs.eof_error() == "The upstream tool call stream had an invalid call id."


def test_changed_call_id_still_rejected():
    """中途换 id 仍判死（原有行为不变）。"""
    obs = _observe_all(
        _chunk({"tool_calls": [_tool_delta("call_a")]}),
        _chunk({"tool_calls": [_tool_delta("call_b")]}),
    )
    assert obs.parser_error == "The upstream tool call stream changed a call id."
