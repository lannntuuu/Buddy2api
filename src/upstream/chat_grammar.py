"""upstream.chat_grammar — Chat Completions SSE 的语法层(全仓唯一实现)。

_ChatStreamObserver 自 proxy.py 上移:chunk 解析、choice index/delta 校验、
tool arguments 累积、终态裁决(eof_error)都收敛在此。消费方:
- proxy._stream_upstream(workbuddy 主路径);
- responses 桥(此前自行裸解析,provider 通道的流因此绕过校验——已收敛)。
Responses 事件发射不属于语法层,仍在 responses.py。
"""

from __future__ import annotations

import json


# 单字符连击退化阈值：连续输出同一非空白字符超过该数即判模型跑飞。
# 实测 hy4 系会偶发整段重复同一字符（如 "!!!!..."）直到烧满输出预算；
# 256 个连续同字符已不可能是有意义的正文。
_REPEAT_RUN_LIMIT = 256


class RepeatRunDetector:
    """单字符连击检测：连续 feed 的同一非空白字符超过阈值判退化。

    空白符（含换行/缩进）不重置也不累计——代码块缩进、段落分隔是正常输出；
    其余任意字符切换都会重置连击计数。
    """

    def __init__(self, limit: int = _REPEAT_RUN_LIMIT):
        self._limit = limit
        self._char = ""
        self._length = 0

    def feed(self, text: str) -> bool:
        """喂入一段输出文本。返回 True = 已超过阈值（模型输出退化）。"""
        for ch in text:
            if ch.isspace():
                continue
            if ch == self._char:
                self._length += 1
            else:
                self._char = ch
                self._length = 1
            if self._length >= self._limit:
                return True
        return False


def _json_sse_event(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _repair_json_arguments(raw: str) -> str:
    """尝试修复上游截断的工具调用 arguments（hy3 长时间流式偶发）。

    只做尾部补全：从后往前尝试补上缺失的 `}` / `]` / `"`，直到能解析成
    JSON 对象。修不动就原样返回（调用方会按不完整报错）。
    """
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
        return raw if isinstance(parsed, dict) else raw
    except (json.JSONDecodeError, RecursionError, TypeError):
        pass
    # 从尾部逐步补闭合符，最多尝试补 16 个（避免死循环/过度猜测）
    for extra in range(1, 17):
        candidate = raw + "}" * extra
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        if isinstance(parsed, dict):
            return candidate
    # 再试补 ] 和 " 组合（嵌套数组/字符串未闭合的场景）
    for tail in ("]", "]", "}", "\"}", "\"]", "}}", "]}", "\"}"):
        candidate = raw + tail
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        if isinstance(parsed, dict):
            return candidate
    return raw




class ChatStreamObserver:
    """Track completion state while Chat Completions SSE is normalized."""

    def __init__(self, fallback_model: str, expected_choices: int = 1):
        self.fallback_model = fallback_model
        if not isinstance(expected_choices, int) or isinstance(expected_choices, bool):
            expected_choices = 1
        self.expected_choice_indices = set(range(expected_choices if 1 <= expected_choices <= 128 else 1))
        self.seen_done = False
        self.saw_chat_chunk = False
        self.upstream_error = False
        self.upstream_error_event: dict | None = None
        self.finish_reasons: dict[int, str | None] = {}
        self.closed_choices: set[int] = set()
        self.content_choices: set[int] = set()
        self.tool_call_choices: set[int] = set()
        self.tool_calls: dict[tuple[int, int], dict] = {}
        self.malformed_data_event = False
        self.parser_error: str | None = None
        self.usage: dict = {}
        self.content_parts: list[str] = []
        self.metadata: dict = {}
        # 单字符连击退化检测（hy4 系偶发输出跑飞，如整段 "!!!!"）
        self.repeat_run = RepeatRunDetector()

    def observe_event(self, data: bytes) -> dict | None:
        if data.strip() == b"[DONE]":
            self.seen_done = True
            return None
        if self.seen_done:
            self.parser_error = "The upstream sent data after the [DONE] event."
            return None
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.malformed_data_event = True
            return None
        if not isinstance(obj, dict):
            self.malformed_data_event = True
            return None

        if "error" in obj and obj["error"] is not None:
            self.upstream_error = True
            self.upstream_error_event = obj
            return None

        choices = obj.get("choices")
        is_chat_chunk = obj.get("object") == "chat.completion.chunk" or "choices" in obj
        if is_chat_chunk and not isinstance(choices, list):
            self.parser_error = "The upstream Chat Completions chunk had an invalid choices field."
            return None
        if is_chat_chunk:
            self.saw_chat_chunk = True
            for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
                if key in obj:
                    self.metadata[key] = obj[key]

        event_usage = obj.get("usage")
        if event_usage is not None and not isinstance(event_usage, dict):
            self.parser_error = "The upstream Chat Completions chunk had invalid usage data."
            return None
        if isinstance(event_usage, dict):
            self.usage.update(event_usage)
        if not is_chat_chunk:
            self.parser_error = "The upstream SSE event was not a Chat Completions chunk."
            return None

        validated_choices: list[tuple[int, dict, str | None]] = []
        event_choice_indices: set[int] = set()
        for choice in choices:
            if not isinstance(choice, dict):
                self.parser_error = "The upstream Chat Completions chunk contained an invalid choice."
                return None
            index = choice.get("index", 0)
            if not isinstance(index, int) or isinstance(index, bool):
                self.parser_error = "The upstream Chat Completions choice had an invalid index."
                return None
            if index not in self.expected_choice_indices:
                self.parser_error = "The upstream Chat Completions choice index was not requested."
                return None
            if index in event_choice_indices:
                self.parser_error = "The upstream Chat Completions chunk repeated a choice index."
                return None
            event_choice_indices.add(index)
            if index in self.closed_choices:
                self.parser_error = "The upstream sent another delta after a choice had finished."
                return None
            reason = choice.get("finish_reason")
            if reason == "":
                reason = None
                choice["finish_reason"] = None
            elif reason is not None and not isinstance(reason, str):
                self.parser_error = "The upstream Chat Completions choice had an invalid finish reason."
                return None
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                self.parser_error = "The upstream Chat Completions choice had an invalid delta."
                return None
            for content_field in ("content", "reasoning_content"):
                content = delta.get(content_field)
                if content is not None and not isinstance(content, str):
                    self.parser_error = (
                        f"The upstream Chat Completions choice had invalid {content_field}."
                    )
                    return None
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is not None and not isinstance(tool_deltas, list):
                self.parser_error = "The upstream Chat Completions choice had invalid tool calls."
                return None
            if isinstance(tool_deltas, list):
                for position, tool_delta in enumerate(tool_deltas):
                    if not isinstance(tool_delta, dict):
                        self.parser_error = "The upstream tool call stream contained an invalid delta."
                        return None
                    tool_index = tool_delta.get("index", position)
                    if (
                        not isinstance(tool_index, int)
                        or isinstance(tool_index, bool)
                        or tool_index < 0
                    ):
                        self.parser_error = "The upstream tool call stream had an invalid index."
                        return None
                    call_id = tool_delta.get("id")
                    if call_id is not None and not isinstance(call_id, str):
                        self.parser_error = "The upstream tool call stream had an invalid call id."
                        return None
                    # 兼容：部分上游（hy4 系实测）会发 id="" 的 delta 帧
                    # （id 稍后帧才带上，甚至整条流不带）。空 id 视作"未携带"：
                    # 此处不报错也不记录，累积阶段 `if call_id:` 同样跳过；
                    # eof 阶段仍要求最终 id 存在（缺失则合成占位），
                    # 所以不会放过真正不完整的工具调用。
                    call_type = tool_delta.get("type")
                    if call_type is not None and call_type != "function":
                        self.parser_error = "The upstream tool call stream had an invalid call type."
                        return None
                    function = tool_delta.get("function")
                    if function is not None and not isinstance(function, dict):
                        self.parser_error = "The upstream tool call stream had an invalid function."
                        return None
                    if isinstance(function, dict):
                        name = function.get("name")
                        if name == "":
                            function.pop("name", None)
                            name = None
                        elif name is not None and not isinstance(name, str):
                            self.parser_error = "The upstream tool call stream had an invalid function name."
                            return None
                        arguments = function.get("arguments")
                        if arguments is not None and not isinstance(arguments, str):
                            self.parser_error = "The upstream tool call stream had invalid arguments."
                            return None
            validated_choices.append((index, delta, reason))

        for index, delta, reason in validated_choices:
            self.finish_reasons.setdefault(index, None)
            if reason:
                self.finish_reasons[index] = reason
                self.closed_choices.add(index)
            content = delta.get("content")
            if content:
                self.content_parts.append(content)
                self.content_choices.add(index)
                if self.repeat_run.feed(content):
                    self.parser_error = (
                        "The upstream output degenerated into a repeated character run."
                    )
                    return None
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is None:
                continue
            if tool_deltas:
                self.tool_call_choices.add(index)
            for position, tool_delta in enumerate(tool_deltas):
                tool_index = tool_delta.get("index", position)
                state = self.tool_calls.setdefault(
                    (index, tool_index),
                    {"id": None, "name": None, "arguments": ""},
                )
                call_id = tool_delta.get("id")
                if call_id:
                    if state["id"] not in (None, call_id):
                        self.parser_error = "The upstream tool call stream changed a call id."
                        return None
                    state["id"] = call_id
                function = tool_delta.get("function")
                if function is None:
                    continue
                name = function.get("name")
                if name:
                    if state["name"] not in (None, name):
                        self.parser_error = "The upstream tool call stream changed a function name."
                        return None
                    state["name"] = name
                arguments = function.get("arguments")
                if arguments is None:
                    continue
                state["arguments"] += arguments
        return obj

    def missing_finish_choices(self) -> list[int]:
        return sorted(index for index, reason in self.finish_reasons.items() if not reason)

    def eof_error(self) -> str | None:
        if self.parser_error:
            return self.parser_error
        if self.malformed_data_event:
            return "The upstream stream ended with a malformed SSE JSON event."
        if self.upstream_error:
            return "The upstream returned an error event in an HTTP 200 stream."
        if not self.saw_chat_chunk:
            return "The upstream stream ended without a Chat Completions chunk."
        missing_choices = self.expected_choice_indices.difference(self.finish_reasons)
        if missing_choices:
            return "The upstream stream ended before all requested choices were received."
        for choice_index, reason in self.finish_reasons.items():
            if reason == "tool_calls" and choice_index not in self.tool_call_choices:
                return "The upstream ended with tool_calls but did not provide a tool call."
            if choice_index in self.tool_call_choices and reason not in {
                None,
                "tool_calls",
                "length",
                "content_filter",
            }:
                return "The upstream tool call stream ended with an inconsistent finish reason."
            if not reason and choice_index not in self.tool_call_choices:
                return "The upstream stream ended before the choice received a finish reason."
        for choice_index in self.tool_call_choices:
            calls = [
                state
                for (current_choice, _), state in self.tool_calls.items()
                if current_choice == choice_index
            ]
            if not calls:
                return "The upstream tool call stream ended before the tool call was identified."
            for tool_pos, state in enumerate(calls):
                if self.finish_reasons.get(choice_index) in {"length", "content_filter"}:
                    continue
                if not state["name"]:
                    return "The upstream tool call stream ended before the tool call was complete."
                if not state["id"]:
                    # 上游始终未携带 id（hy4 系偶发）：按位置合成占位 id。
                    # 客户端回传 tool 结果依赖 id 匹配，空 id 会让协议无效；
                    # 合成比让整个回合 502 更可用。
                    state["id"] = f"call_{choice_index}_{tool_pos}"
                repaired = _repair_json_arguments(state["arguments"])
                try:
                    arguments = json.loads(repaired)
                except (json.JSONDecodeError, RecursionError, TypeError):
                    return "The upstream tool call stream ended with incomplete JSON arguments."
                if not isinstance(arguments, dict):
                    return "The upstream tool call arguments were not a JSON object."
                if repaired != state["arguments"]:
                    # 上游把 arguments 尾部截断了（hy3 长时间流式偶发）：
                    # 修复后按修复值透传，避免整个回合失败。
                    state["arguments"] = repaired
        for choice_index, reason in self.finish_reasons.items():
            if (
                reason not in {"length", "content_filter"}
                and choice_index not in self.content_choices
                and choice_index not in self.tool_call_choices
            ):
                return "The upstream choice ended without content or a tool call."
        return None

    def terminal_event(self, choice_indices: list[int]) -> bytes:
        payload = {
            "id": self.metadata.get("id") or "chatcmpl-" + os.urandom(12).hex(),
            "object": "chat.completion.chunk",
            "created": self.metadata.get("created") or int(time.time()),
            "model": self.metadata.get("model") or self.fallback_model,
            "choices": [
                {
                    "index": index,
                    "delta": {},
                    "finish_reason": "tool_calls" if index in self.tool_call_choices else "stop",
                }
                for index in choice_indices
            ],
        }
        for key in ("system_fingerprint", "service_tier"):
            if key in self.metadata:
                payload[key] = self.metadata[key]
        return _json_sse_event(payload)


# SSE 解析统一走 upstream.sse.SSEDecoder(兼容旧名)。
