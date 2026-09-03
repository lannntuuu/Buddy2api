"""
proxy.py — 请求代理转发

P3 split: this module keeps the request pipeline (proxy_chat_completions,
the streaming + collector, log_request, the SSE observer/decoder) and
re-exports a stable surface for everything that was extracted.

Pipeline layers (each lives in its own module under upstream/):
  - aliases.py      — model alias table, default model list, reasoning defaults
  - moderation.py   — content-audit and tool-stall detection helpers
  - compaction.py   — request-body compaction policy and 11128 self-heal state

The aliases / moderation / compaction helpers are imported here under
their original private names so existing call sites in this file
(e.g. `if _is_tool_stall(...)`) keep working without further edits.
External code that imports from `upstream.proxy` also keeps working
because of the re-exports below.
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger("buddy2api.proxy")

# ---- Backwards-compatible re-exports ----
# These modules are the canonical home for each name; we re-export
# here so callers (`from upstream import proxy; proxy._is_tool_stall(...)`,
# `proxy.DEFAULT_MODELS`, etc.) keep working after the split.
from upstream.aliases import (  # noqa: E402,F401
    DEFAULT_MODELS,
    _BUILTIN_ALIASES,
    effective_builtin_aliases,
    resolve_model_alias,
    _configured_reasoning_default,
    _env_int,
)
from upstream.moderation import (  # noqa: E402,F401
    _body_size_profile,
    _dump_11128_body,
    _looks_like_audit_block,
    _request_has_tool_loop,
    _looks_like_stall_text,
    _is_tool_stall,
    TOOL_STALL_RETRY,
    TOOL_STALL_FAIL_STREAM,
)
from upstream.compaction import (  # noqa: E402,F401
    compaction_stats,
    _is_11128_error,
    _arm_channel,
    _channel_armed,
    _smart_compact_messages,
    _compact_text,
    _compact_tools,
    _compact_schema_descriptions,
)

from storage import database as db
from accounts import auth_manager

BACKEND = "https://copilot.tencent.com"
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS_CODES or status in {401, 403}


async def _retry_delay(attempt: int):
    await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))

PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

_BACKEND_ROLE_ALIASES = {
    "developer": "system",
}


def build_backend_body(payload: dict) -> dict:
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    messages = body.get("messages")
    if isinstance(messages, list):
        body["messages"] = [
            {
                **message,
                "role": _BACKEND_ROLE_ALIASES.get(message.get("role"), message.get("role")),
            }
            if isinstance(message, dict) and message.get("role") in _BACKEND_ROLE_ALIASES
            else message
            for message in messages
        ]
    # 注：content 精简不在此构建期做。11128 自愈精简只在转发失败后的重试路径触发，
    # 那里才拿得到客户端信息（仅 ZCode Client 参与），避免构建期无谓地全量截断。
    has_explicit_thinking = "thinking" in payload
    # Resolve model alias before forwarding
    raw_model = body.get("model", "auto")
    body["model"] = resolve_model_alias(raw_model)
    if "reasoning_effort" not in body and not has_explicit_thinking:
        default_reasoning = _configured_reasoning_default(body["model"])
        if default_reasoning:
            body["reasoning_effort"] = default_reasoning
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def get_all_aliases() -> dict:
    """Return effective aliases (custom replaces built-ins; see effective_builtin_aliases)."""
    return effective_builtin_aliases()


def _safe_err(raw: bytes, status: int) -> dict:
    try:
        detail = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        detail = {"error": {"message": raw.decode("utf-8", "replace")[:500],
                            "type": "upstream_error"}}
    return detail


def _err_sse_event(raw: bytes, status: int) -> bytes:
    msg = raw.decode("utf-8", "replace")[:500]
    payload = json.dumps({"error": {"message": msg, "type": "upstream_error", "code": status}})
    event = f"data: {payload}\n\ndata: [DONE]\n\n"
    return event.encode("utf-8")


def _json_sse_event(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _has_terminal_choice(payload: dict) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, dict) and bool(choice.get("finish_reason"))
        for choice in choices
    )


_MAX_SSE_EVENT_BYTES = 8 * 1024 * 1024


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


class _ChatStreamObserver:
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
                    if call_id is not None and (not isinstance(call_id, str) or not call_id):
                        self.parser_error = "The upstream tool call stream had an invalid call id."
                        return None
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
            for state in calls:
                if self.finish_reasons.get(choice_index) in {"length", "content_filter"}:
                    continue
                if not state["id"] or not state["name"]:
                    return "The upstream tool call stream ended before the tool call was complete."
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


class _SSEEventDecoder:
    """Decode complete SSE data fields from arbitrary byte chunks."""

    def __init__(self):
        self.parser_error: str | None = None
        self._buffer = b""
        self._data_lines: list[bytes] = []
        self._event_bytes = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        if self.parser_error:
            return []
        self._buffer += chunk
        events: list[bytes] = []
        while True:
            line = self._take_line()
            if line is None:
                break
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
            if self.parser_error:
                break
        if not self.parser_error and len(self._buffer) > _MAX_SSE_EVENT_BYTES:
            self._fail("The upstream SSE line exceeded the 8 MiB limit.")
        return events

    def finish(self) -> list[bytes]:
        if self.parser_error:
            return []
        events: list[bytes] = []
        while True:
            line = self._take_line(final=True)
            if line is None:
                break
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
            if self.parser_error:
                return events
        if self._data_lines:
            events.append(b"\n".join(self._data_lines))
            self._data_lines = []
            self._event_bytes = 0
        return events

    def _take_line(self, *, final: bool = False) -> bytes | None:
        for index, value in enumerate(self._buffer):
            if value == 0x0A:
                line = self._buffer[:index]
                self._buffer = self._buffer[index + 1:]
                return line[:-1] if line.endswith(b"\r") else line
            if value == 0x0D:
                if index + 1 == len(self._buffer) and not final:
                    return None
                end = index + 2 if self._buffer[index + 1:index + 2] == b"\n" else index + 1
                line = self._buffer[:index]
                self._buffer = self._buffer[end:]
                return line
        if final and self._buffer:
            line = self._buffer
            self._buffer = b""
            return line
        return None

    def _consume_line(self, line: bytes) -> bytes | None:
        if len(line) > _MAX_SSE_EVENT_BYTES:
            self._fail("The upstream SSE line exceeded the 8 MiB limit.")
            return None
        if not line:
            if not self._data_lines:
                return None
            event = b"\n".join(self._data_lines)
            self._data_lines = []
            self._event_bytes = 0
            return event
        if not line.startswith(b"data:"):
            return None
        data = line[5:]
        if data.startswith(b" "):
            data = data[1:]
        self._event_bytes += len(data) + 1
        if self._event_bytes > _MAX_SSE_EVENT_BYTES:
            self._fail("The upstream SSE event exceeded the 8 MiB limit.")
            return None
        self._data_lines.append(data)
        return None

    def _fail(self, message: str) -> None:
        self.parser_error = message
        self._buffer = b""
        self._data_lines = []
        self._event_bytes = 0


def _extract_cache_tokens(usage: dict | None) -> tuple[int, int]:
    """从上游 usage 提取 (cache_read, cache_creation)，兼容三种字段风格。

    优先级：Anthropic → DeepSeek → OpenAI。
      - Anthropic: cache_read_input_tokens / cache_creation_input_tokens
      - DeepSeek:  prompt_cache_hit_tokens(→cache_read) / prompt_cache_miss_tokens(→creation 不计入)
      - OpenAI:    prompt_tokens_details.cached_tokens(→cache_read)，无 creation 概念
    全部缺省返回 (0, 0)。负值 clamp 到 0；cache_read 不超过 prompt_tokens（cache_read 是 prompt 子集）。
    """
    if not usage or not isinstance(usage, dict):
        return (0, 0)

    cache_read = 0
    cache_creation = 0

    # 1) Anthropic 风格
    ar = usage.get("cache_read_input_tokens")
    ac = usage.get("cache_creation_input_tokens")
    if ar is not None or ac is not None:
        cache_read = int(ar) if ar is not None else 0
        cache_creation = int(ac) if ac is not None else 0
        return (
            max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0))),
            max(0, cache_creation),
        )

    # 2) DeepSeek 风格
    dh = usage.get("prompt_cache_hit_tokens")
    if dh is not None:
        cache_read = int(dh)
        return (
            max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0))),
            0,
        )

    # 3) OpenAI 风格
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
        cache_read = int(ptd["cached_tokens"])
        return (
            max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0))),
            0,
        )

    return (0, 0)


def _log_request(api_key_info, account, model_name, stream,
                  prompt_t, completion_t, total_t, credit,
                  finish_reason, status_code, error_msg, t0,
                  increment_usage: bool = True,
                  usage: dict | None = None,
                  reasoning_effort: str | None = None):
    elapsed_ms = int((time.time() - t0) * 1000)
    log_data = {
        "api_key_id": api_key_info["id"] if api_key_info else None,
        "api_key_name": api_key_info["name"] if api_key_info else None,
        "account_id": account["id"] if account else None,
        "account_name": account.get("name") if account else None,
        "provider": (account.get("provider") if account else None)
        or (api_key_info.get("_bind_channel") if api_key_info else None)
        or "workbuddy",
        "model": model_name,
        "stream": 1 if stream else 0,
        "reasoning_effort": reasoning_effort,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "credit": credit,
        "finish_reason": finish_reason,
        "duration_ms": elapsed_ms,
        "status_code": status_code,
        "error_msg": error_msg,
        "increment_usage": increment_usage,
        "client": (api_key_info or {}).get("_client_tag"),
        "client_version": (api_key_info or {}).get("_client_version"),
    }
    # Cache 命中追踪：兼容三种字段风格，整包 dump 留证据。
    cache_read, cache_creation = _extract_cache_tokens(usage)
    log_data["cache_read_tokens"] = cache_read
    log_data["cache_creation_tokens"] = cache_creation
    usage_json = None
    if usage is not None:
        try:
            serialized = json.dumps(usage, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = None
        # 体积保护：序列化后 >64KB 时只留存提取结果，避免超大 usage 污染日志表。
        if serialized is not None and len(serialized.encode("utf-8")) > 65536:
            serialized = json.dumps(
                {"truncated": True, "cache_read_tokens": cache_read,
                 "cache_creation_tokens": cache_creation},
                ensure_ascii=False,
            )
        usage_json = serialized
    log_data["usage_json"] = usage_json
    # credit_source='live' 门槛：usage 含任意已知 cache 键即标 live（实测语义，与 dashboard accurate 对齐）。
    _known_cache_keys = (
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        "prompt_tokens_details",
    )
    log_data["credit_source"] = (
        "live" if usage is not None and any(k in usage for k in _known_cache_keys) else None
    )
    try:
        # 写日志（含 BEGIN IMMEDIATE 事务 + fsync）不占事件循环：
        # 放进默认线程池 fire-and-forget，日志失败只静默丢弃。
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, db.record_request, log_data)
        # fire-and-forget：吞掉 executor 内抛出的异常，避免“异常从未被读取”告警
        fut.add_done_callback(lambda f: f.exception() if f.cancelled() is False else None)
    except Exception:
        pass


async def proxy_chat_completions(
    payload: dict,
    api_key_info: Optional[dict] = None,
    log_model: Optional[str] = None,
) -> tuple:
    """
    主代理函数。

    返回:
      - ("stream", async_generator)  流式响应
      - ("json", dict)               非流式响应
      - ("error", (status_code, detail))  错误
    """
    client_wants_stream = bool(payload.get("stream"))
    body = build_backend_body(payload)
    # 实际发给上游的思考档位（客户端显式或按模型配置注入）：用于请求日志
    effective_reasoning = body.get("reasoning_effort")
    if log_model is None and isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    model_name = log_model if log_model is not None else payload.get("model", "auto")

    if client_wants_stream:
        return (
            "stream",
            _stream_upstream(body, api_key_info, model_name),
        )

    tried_ids: set[int] = set()
    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        account = await auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            break

        tried_ids.add(account["id"])
        headers = await auth_manager.get_valid_headers(account)
        if not headers:
            auth_manager.mark_account_failure(account["id"], 401)
            continue

        url = f"{auth_manager.backend_url()}/v2/chat/completions"
        t0 = time.time()
        result = await _collect_stream(url, headers, body, account, api_key_info, model_name, t0)
        if result[0] == "json":
            # 工具停转修复：agent 回合被上游以 stop+纯文本结束且未调用工具时，
            # 用 tool_choice=required 重试一次；重试产出工具调用则采用重试结果。
            if TOOL_STALL_RETRY:
                choice = (result[1].get("choices") or [{}])[0]
                message = choice.get("message") or {}
                if _is_tool_stall(
                    body,
                    choice.get("finish_reason"),
                    bool(message.get("tool_calls")),
                    message.get("content") or "",
                ):
                    retry_body = {**body, "tool_choice": "required"}
                    retry_t0 = time.time()
                    retry_result = await _collect_stream(
                        url, headers, retry_body, account, api_key_info, model_name, retry_t0
                    )
                    if retry_result[0] == "json":
                        retry_choice = (retry_result[1].get("choices") or [{}])[0]
                        retry_message = retry_choice.get("message") or {}
                        if retry_message.get("tool_calls"):
                            auth_manager.mark_account_success(account["id"])
                            return retry_result
            auth_manager.mark_account_success(account["id"])
            return result

        channel = account.get("provider") or "workbuddy"
        client = (api_key_info or {}).get("_client_tag")
        err_status = result[1][0]
        # 11128 大内容拦截：武装该 (通道,客户端) + 用激进阈值精简后原地重试（自愈）。
        # 仅 ZCode Client 参与精简；DSH 及其它 agent 不精简。
        if _is_11128_error(err_status, result[1][1], body):
            _arm_channel(channel, client)
            _smart_compact_messages(body, channel=channel, client_tag=client)
            body["_compacted_11128"] = True
            with _COMPACTION_LOCK:
                _COMPACTION_STATS["retried_11128"] += 1
            retry_t0 = time.time()
            retry_result = await _collect_stream(
                url, headers, body, account, api_key_info, model_name, retry_t0
            )
            if retry_result[0] == "json":
                auth_manager.mark_account_success(account["id"])
                return retry_result
            # 精简后仍失败：落为普通错误走统一处理（不再尝试切换账号疯转）
            result = retry_result
            err_status = retry_result[1][0]
            dump_path = _dump_11128_body(body, channel, model_name)
            logger.warning(
                "11128 self-heal retry still failed (non-stream) "
                "profile=%s channel=%s model=%s dump=%s",
                _body_size_profile(body),
                channel,
                model_name,
                dump_path,
            )

        last_error = result
        auth_manager.mark_account_failure(account["id"], err_status)
        will_retry = _is_retryable_status(err_status) and attempt < max_retries - 1
        detail = result[1][1]
        error_message = detail
        if isinstance(detail, dict):
            error_data = detail.get("error") if isinstance(detail.get("error"), dict) else detail
            error_message = error_data.get("message", detail) if isinstance(error_data, dict) else detail
        _log_request(
            api_key_info, account, model_name, False,
            0, 0, 0, 0, "retry" if will_retry else "error",
            err_status, str(error_message)[:500], t0,
            increment_usage=not will_retry,
            reasoning_effort=effective_reasoning,
        )
        if not will_retry:
            return result
        await _retry_delay(attempt)

    return last_error or (
        "error",
        (503, {"error": {"message": "No available accounts", "type": "server_error"}}),
    )


async def test_account_chat(account: dict, model: str = "auto", prompt: str = "ping") -> dict:
    """Run a small non-streaming request against one specific account."""
    headers = await auth_manager.get_valid_headers(account)
    if not headers:
        return {
            "ok": False,
            "status_code": 401,
            "duration_ms": 0,
            "message": "token refresh failed or account credentials are invalid",
        }

    body = build_backend_body({
        "model": model or "auto",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": False,
    })
    url = f"{auth_manager.backend_url()}/v2/chat/completions"
    t0 = time.time()
    result = await _collect_stream(url, headers, body, account, None, f"account-test:{model or 'auto'}", t0)
    duration_ms = int((time.time() - t0) * 1000)

    if result[0] == "json":
        data = result[1]
        message = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        return {
            "ok": True,
            "status_code": 200,
            "duration_ms": duration_ms,
            "model": data.get("model"),
            "message": message[:240],
            "usage": usage,
        }

    status, detail = result[1]
    msg = detail
    if isinstance(detail, dict):
        err = detail.get("error") if isinstance(detail.get("error"), dict) else detail
        msg = err.get("message") if isinstance(err, dict) else detail
    return {
        "ok": False,
        "status_code": status,
        "duration_ms": duration_ms,
        "message": str(msg)[:500],
    }


async def _stream_upstream(
    body: dict,
    api_key_info: Optional[dict],
    model_name: str,
) -> AsyncGenerator[bytes, None]:
    """Stream upstream SSE with pre-output account failover and backoff."""
    tried_ids: set[int] = set()
    last_error = b"No available accounts"
    # 实际发给上游的思考档位（客户端显式或按模型配置注入）：用于请求日志
    effective_reasoning = body.get("reasoning_effort")
    last_error_event: dict | None = None
    last_status = 503
    last_account = None
    last_started = time.time()
    pending_retry_log: dict | None = None

    for attempt in range(3):
        account = await auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            break
        channel = account.get("provider") or "workbuddy"
        if pending_retry_log is not None:
            _log_request(
                api_key_info,
                pending_retry_log["account"],
                model_name,
                True,
                pending_retry_log["prompt_tokens"],
                pending_retry_log["completion_tokens"],
                pending_retry_log["total_tokens"],
                pending_retry_log["credit"],
                "retry",
                pending_retry_log["status"],
                pending_retry_log["message"],
                pending_retry_log["started"],
                increment_usage=False,
                reasoning_effort=effective_reasoning,
            )
            await _retry_delay(pending_retry_log["attempt"])
            pending_retry_log = None
        last_account = account
        tried_ids.add(account["id"])
        headers = await auth_manager.get_valid_headers(account)
        if not headers:
            auth_manager.mark_account_failure(account["id"], 401)
            last_error = b"Account credentials are invalid"
            last_error_event = None
            last_status = 401
            continue

        url = f"{auth_manager.backend_url()}/v2/chat/completions"
        t0 = time.time()
        last_started = t0
        observer = _ChatStreamObserver(body.get("model") or model_name, body.get("n", 1))
        decoder = _SSEEventDecoder()
        output_started = False
        pending_terminal_events: list[bytes] = []
        pending_terminal_bytes = 0
        stop_reading = False

        try:
            timeout = httpx.Timeout(
                connect=10,
                read=auth_manager.request_timeout(300),
                write=30,
                pool=10,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        raw_error = await response.aread()
                        # 11128 大内容拦截：武装通道 + 激进精简后原地重试（自愈）。
                        if _is_11128_error(response.status_code, raw_error, body):
                            _arm_channel(channel, (api_key_info or {}).get("_client_tag"))
                            _smart_compact_messages(
                                body, channel=channel,
                                client_tag=(api_key_info or {}).get("_client_tag"),
                            )
                            body["_compacted_11128"] = True
                            with _COMPACTION_LOCK:
                                _COMPACTION_STATS["retried_11128"] += 1
                            # 同一账号重发一次：从 tried 移除以免单账号通道被误判为无可用账号
                            tried_ids.discard(account["id"])
                            attempt -= 1
                            continue
                        last_error = raw_error
                        last_error_event = None
                        last_status = response.status_code
                        if body.get("_compacted_11128"):
                            # 自愈精简后仍失败：记录 body 特征 + 完整出站体，便于定位触发源
                            dump_path = _dump_11128_body(body, channel, model_name)
                            logger.warning(
                                "11128 self-heal retry still failed "
                                "profile=%s channel=%s model=%s dump=%s",
                                _body_size_profile(body),
                                channel,
                                model_name,
                                dump_path,
                            )
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        if _is_retryable_status(response.status_code) and attempt < 2:
                            pending_retry_log = {
                                "account": account,
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                                "credit": 0,
                                "status": response.status_code,
                                "message": raw_error.decode("utf-8", "replace")[:500],
                                "started": t0,
                                "attempt": attempt,
                            }
                            continue
                        _log_request(
                            api_key_info, account, model_name, True,
                            0, 0, 0, 0, "error", response.status_code,
                            raw_error.decode("utf-8", "replace")[:500], t0,
                            reasoning_effort=effective_reasoning,
                        )
                        yield _err_sse_event(raw_error, response.status_code)
                        return

                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        for data in decoder.feed(chunk):
                            obj = observer.observe_event(data)
                            if obj is not None and not obj.get("error"):
                                encoded = _json_sse_event(obj)
                                if pending_terminal_events or _has_terminal_choice(obj):
                                    pending_terminal_events.append(encoded)
                                    pending_terminal_bytes += len(encoded)
                                    if pending_terminal_bytes > _MAX_SSE_EVENT_BYTES:
                                        observer.parser_error = (
                                            "The upstream terminal SSE events exceeded the 8 MiB limit."
                                        )
                                else:
                                    output_started = True
                                    yield encoded
                            if (
                                observer.seen_done
                                or observer.parser_error
                                or observer.malformed_data_event
                                or observer.upstream_error
                            ):
                                stop_reading = True
                                break
                        if decoder.parser_error and not observer.seen_done:
                            observer.parser_error = decoder.parser_error
                            stop_reading = True
                        if stop_reading:
                            break
        except httpx.HTTPError as exc:
            last_error = str(exc).encode("utf-8", "replace")
            last_error_event = None
            last_status = 502
            auth_manager.mark_account_failure(account["id"], 502)
            if not output_started and attempt < 2:
                pending_retry_log = {
                    "account": account,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "credit": 0,
                    "status": 502,
                    "message": str(exc)[:500],
                    "started": t0,
                    "attempt": attempt,
                }
                continue
            _log_request(
                api_key_info, account, model_name, True,
                0, 0, 0, 0, "network_error", 502, str(exc)[:500], t0,
                reasoning_effort=effective_reasoning,
            )
            yield _err_sse_event(last_error, 502)
            return

        if not stop_reading:
            for data in decoder.finish():
                obj = observer.observe_event(data)
                if obj is not None and not obj.get("error"):
                    encoded = _json_sse_event(obj)
                    if pending_terminal_events or _has_terminal_choice(obj):
                        pending_terminal_events.append(encoded)
                        pending_terminal_bytes += len(encoded)
                        if pending_terminal_bytes > _MAX_SSE_EVENT_BYTES:
                            observer.parser_error = (
                                "The upstream terminal SSE events exceeded the 8 MiB limit."
                            )
                    else:
                        output_started = True
                        yield encoded
        if decoder.parser_error and not observer.seen_done:
            observer.parser_error = decoder.parser_error

        eof_error = observer.eof_error()
        if eof_error:
            last_error = (
                json.dumps(observer.upstream_error_event, ensure_ascii=False).encode("utf-8")
                if observer.upstream_error_event is not None
                else eof_error.encode("utf-8")
            )
            last_error_event = observer.upstream_error_event
            last_status = 502
            auth_manager.mark_account_failure(account["id"], 502)
            if not output_started and attempt < 2:
                pending_retry_log = {
                    "account": account,
                    "prompt_tokens": observer.usage.get("prompt_tokens", 0),
                    "completion_tokens": observer.usage.get("completion_tokens", 0),
                    "total_tokens": observer.usage.get("total_tokens", 0),
                    "credit": observer.usage.get("credit", 0),
                    "status": 502,
                    "message": eof_error,
                    "started": t0,
                    "attempt": attempt,
                }
                continue
            _log_request(
                api_key_info, account, model_name, True,
                observer.usage.get("prompt_tokens", 0),
                observer.usage.get("completion_tokens", 0),
                observer.usage.get("total_tokens", 0),
                observer.usage.get("credit", 0),
                "error", 502, eof_error, t0,
                usage=observer.usage,
                reasoning_effort=effective_reasoning,
            )
            if observer.upstream_error_event is not None:
                yield _json_sse_event(observer.upstream_error_event)
                yield b"data: [DONE]\n\n"
            else:
                yield _err_sse_event(eof_error.encode("utf-8"), 502)
            return

        missing_choices = observer.missing_finish_choices()
        synthetic_terminal = None
        if missing_choices:
            synthetic_terminal = observer.terminal_event(missing_choices)
            observer.finish_reasons.update({
                index: "tool_calls" if index in observer.tool_call_choices else "stop"
                for index in missing_choices
            })
        auth_manager.mark_account_success(account["id"])

        full_text = "".join(observer.content_parts)
        audit_blocked = _looks_like_audit_block(full_text)
        finish_reason = next((reason for reason in observer.finish_reasons.values() if reason), None)
        tool_stall = _is_tool_stall(body, finish_reason, bool(observer.tool_call_choices), full_text)
        log_finish = "content_filter" if audit_blocked else ("tool_stall" if tool_stall else (finish_reason or "stop"))
        log_error = (
            ("[audit blocked] " + full_text[:300]) if audit_blocked
            else ("[tool stall] " + full_text[:300]) if tool_stall
            else ""
        )
        _log_request(
            api_key_info, account, model_name, True,
            observer.usage.get("prompt_tokens", 0),
            observer.usage.get("completion_tokens", 0),
            observer.usage.get("total_tokens", 0),
            observer.usage.get("credit", 0),
            log_finish, 200, log_error, t0,
            usage=observer.usage,
            reasoning_effort=effective_reasoning,
        )
        if tool_stall and TOOL_STALL_FAIL_STREAM:
            # 流式已发出文本增量，无法回退重试；把本回合标记为失败，
            # 让有重试机制的客户端（DSH / OpenCode 等）自动重试。
            yield _json_sse_event({
                "error": {
                    "message": "The model finished a tool turn without calling a tool.",
                    "type": "upstream_error",
                    "code": "upstream_tool_stall",
                },
            })
            yield b"data: [DONE]\n\n"
            return
        for event in pending_terminal_events:
            yield event
        if synthetic_terminal is not None:
            yield synthetic_terminal
        yield b"data: [DONE]\n\n"
        return

    final_failure = pending_retry_log or {
        "account": last_account,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "credit": 0,
        "status": last_status,
        "message": last_error.decode("utf-8", "replace")[:500],
        "started": last_started,
    }
    _log_request(
        api_key_info, final_failure["account"], model_name, True,
        final_failure["prompt_tokens"],
        final_failure["completion_tokens"],
        final_failure["total_tokens"],
        final_failure["credit"],
        "error", final_failure["status"],
        final_failure["message"], final_failure["started"],
        reasoning_effort=effective_reasoning,
    )
    if last_error_event is not None:
        yield _json_sse_event(last_error_event)
        yield b"data: [DONE]\n\n"
    else:
        yield _err_sse_event(last_error, last_status)


async def _collect_stream(
    url: str, headers: dict, body: dict,
    account: dict, api_key_info: Optional[dict],
    model_name: str, t0: float,
) -> tuple:
    """聚合 SSE 流为单个非流式 JSON。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    try:
        async with httpx.AsyncClient(timeout=auth_manager.request_timeout(300)) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    detail = _safe_err(raw, r.status_code)
                    return ("error", (r.status_code, detail))

                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    model = chunk.get("model") or model
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning_parts.append(delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
    except httpx.HTTPError as e:
        return ("error", (502, {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}}))

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": _repair_json_arguments(v["arguments"])}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    if (
        not content_parts
        and not tool_calls
        and finish_reason not in {"length", "content_filter"}
    ):
        return (
            "error",
            (502, {
                "error": {
                    "message": "The upstream choice ended without content or a tool call.",
                    "type": "upstream_error",
                },
            }),
        )

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tcs:
        message["tool_calls"] = tcs
    result = {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or model_name,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    u = usage or {}
    effective_reasoning = (body or {}).get("reasoning_effort")
    _log_request(
        api_key_info, account, model_name, False,
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
        u.get("total_tokens", 0),
        u.get("credit", 0),
        finish_reason or "stop", 200, "", t0,
        usage=u,
        reasoning_effort=effective_reasoning,
    )
    return ("json", result)
