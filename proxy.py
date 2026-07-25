"""
proxy.py — 请求代理转发

功能：
  - 转发到 copilot.tencent.com/v2/chat/completions
  - 流式 SSE 原样转发
  - 非流式 SSE 聚合为单个 JSON
  - tool_calls 分片合并
  - usage 统计
  - 账号故障自动切换
"""

import asyncio
import json
import os
import time
from typing import AsyncGenerator, Optional

import httpx

import database as db
import auth_manager

BACKEND = "https://copilot.tencent.com"
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

# 腾讯内容审核拦截时返回的固定话术特征（HTTP 200 + 正文是这段话）
_AUDIT_PHRASES = (
    "系统检测到",
    "敏感内容",
    "无法响应您的请求",
    "请检查后重新输入",
    "内容违规",
    "违规内容",
    "不能提供相关",
)


def _looks_like_audit_block(text: str) -> bool:
    if not text:
        return False
    return any(p in text for p in _AUDIT_PHRASES)


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

DEFAULT_MODELS = [
    {"id": "glm-5.2", "name": "GLM-5.2"},
    {"id": "glm-5.1", "name": "GLM-5.1"},
    {"id": "glm-5v-turbo", "name": "GLM-5V Turbo"},
    {"id": "kimi-k2.7", "name": "Kimi K2.7"},
    {"id": "kimi-k2.6", "name": "Kimi K2.6"},
    {"id": "kimi-k2.5", "name": "Kimi K2.5"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
    {"id": "minimax-m3-pay", "name": "MiniMax M3"},
    {"id": "hy3-preview-agent", "name": "HY3 Preview Agent"},
    {"id": "auto", "name": "Auto (auto routing)"},
]

# Built-in model aliases: alias_id -> backend_model_id
# Extended by user-defined aliases from database settings "model_aliases".
_BUILTIN_ALIASES = {
    # GPT-5.x 系列 → 映射到后端可用模型
    "gpt-5.5": "glm-5.2",
    "gpt-5.5-mini": "glm-5.1",
    "gpt-5.4": "glm-5.2",
    "gpt-5.4-mini": "glm-5.1",
    "gpt-5.4-codex": "glm-5.2",
    "gpt-5.1": "glm-5.2",
    "gpt-5.1-codex": "glm-5.2",
    "gpt-5": "glm-5.2",
    "gpt-5-mini": "glm-5.1",
    # GPT-4.x 系列
    "gpt-4o": "glm-5.2",
    "gpt-4o-mini": "glm-5.1",
    "gpt-4-turbo": "glm-5.2",
    "gpt-4": "glm-5.2",
    "gpt-4.1": "glm-5.2",
    "gpt-4.1-mini": "glm-5.1",
    "gpt-3.5-turbo": "glm-5.1",
    # o 系列推理模型
    "o3": "deepseek-v4-pro",
    "o3-mini": "deepseek-v4-flash",
    "o4-mini": "deepseek-v4-pro",
    "o1": "deepseek-v4-pro",
    "o1-mini": "deepseek-v4-flash",
    # Claude 系列
    "claude-3.5-sonnet": "deepseek-v4-pro",
    "claude-3-haiku": "deepseek-v4-flash",
    "claude-sonnet-4": "deepseek-v4-pro",
    "claude-opus-4": "deepseek-v4-pro",
    # DeepSeek
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-coder": "deepseek-v4-pro",
    "deepseek-r1": "deepseek-v4-pro",
    # Moonshot
    "moonshot-v1-128k": "kimi-k2.7",
    "moonshot-v1-32k": "kimi-k2.6",
}


def resolve_model_alias(model: str) -> str:
    """Resolve an alias to its real backend model ID. Returns original if no match."""
    aliases = db.get_setting("model_aliases", {})
    merged = {**_BUILTIN_ALIASES, **aliases}
    return merged.get(model, model)


def build_backend_body(payload: dict) -> dict:
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    # Resolve model alias before forwarding
    raw_model = body.get("model", "auto")
    body["model"] = resolve_model_alias(raw_model)
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def get_all_aliases() -> dict:
    """Return merged aliases (built-in + user-defined)."""
    user_aliases = db.get_setting("model_aliases", {})
    return {**_BUILTIN_ALIASES, **user_aliases}


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
                        if name is not None and (not isinstance(name, str) or not name):
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
                try:
                    arguments = json.loads(state["arguments"])
                except (json.JSONDecodeError, RecursionError, TypeError):
                    return "The upstream tool call stream ended with incomplete JSON arguments."
                if not isinstance(arguments, dict):
                    return "The upstream tool call arguments were not a JSON object."
        for choice_index, reason in self.finish_reasons.items():
            if not reason and choice_index not in self.content_choices and choice_index not in self.tool_call_choices:
                return "The upstream stream ended before the choice produced complete content."
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


def _log_request(api_key_info, account, model_name, stream,
                  prompt_t, completion_t, total_t, credit,
                  finish_reason, status_code, error_msg, t0,
                  increment_usage: bool = True):
    elapsed_ms = int((time.time() - t0) * 1000)
    log_data = {
        "api_key_id": api_key_info["id"] if api_key_info else None,
        "api_key_name": api_key_info["name"] if api_key_info else None,
        "account_id": account["id"] if account else None,
        "account_name": account.get("name") if account else None,
        "model": model_name,
        "stream": 1 if stream else 0,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "credit": credit,
        "finish_reason": finish_reason,
        "duration_ms": elapsed_ms,
        "status_code": status_code,
        "error_msg": error_msg,
        "increment_usage": increment_usage,
    }
    try:
        db.record_request(log_data)
    except Exception:
        pass


async def proxy_chat_completions(
    payload: dict,
    api_key_info: Optional[dict] = None,
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
    model_name = payload.get("model", "auto")

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
            auth_manager.mark_account_success(account["id"])
            return result

        last_error = result
        err_status = result[1][0]
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
    last_error_event: dict | None = None
    last_status = 503
    last_account = None
    last_started = time.time()
    pending_retry_log: dict | None = None

    for attempt in range(3):
        account = await auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            break
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
                        last_error = raw_error
                        last_error_event = None
                        last_status = response.status_code
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
        log_finish = "content_filter" if audit_blocked else (finish_reason or "stop")
        log_error = ("[audit blocked] " + full_text[:300]) if audit_blocked else ""
        _log_request(
            api_key_info, account, model_name, True,
            observer.usage.get("prompt_tokens", 0),
            observer.usage.get("completion_tokens", 0),
            observer.usage.get("total_tokens", 0),
            observer.usage.get("credit", 0),
            log_finish, 200, log_error, t0,
        )
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
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

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
    _log_request(
        api_key_info, account, model_name, False,
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
        u.get("total_tokens", 0),
        u.get("credit", 0),
        finish_reason or "stop", 200, "", t0,
    )
    return ("json", result)
