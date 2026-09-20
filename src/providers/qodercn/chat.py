"""Qoder CN chat client.

Wire format frozen from a live capture of the official desktop client:

* endpoint  : ``POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation
  ?FetchKeys=llm_model_result&AgentId=agent_common``
* body      : plain JSON (the ``Encode=1`` body cipher is *not* required — the
  plaintext path was smoke-tested and returns real streamed content)
* response  : SSE whose每一 ``data:`` 行是一个 envelope
  ``{"headers":…,"body":"<内层 OpenAI chunk 的 JSON 字符串>","statusCodeValue":200}``
  —— 内层 `body` 需要再解码一次；流结束是内层 body 等于字符串 ``"[DONE]"``。

Qoder 的 SSE 已经在语义上就是 OpenAI ``chat.completion.chunk``（含
``reasoning_content`` 思考增量），所以这里做的是"信封剥离 + 字段归一"，
而不是重新拼装内容：把双层 JSON 剥成标准 chunk，覆盖不可信的 ``model``
字段，再按要求补齐 ``finish_reason`` / ``usage`` / ``[DONE]``。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncGenerator

import httpx

from accounts import auth_manager
from storage.http_pool import get_client
from providers import store_common
from providers.model_config import channel_aliases
from providers.retry import retry_delay
from providers.qodercn import cosy, store
from providers.qodercn.constants import (
    ALIASES,
    BUSINESS,
    BUSINESS_PRODUCT,
    BUSINESS_TYPE,
    CHANNEL_ID,
    CHAT_PATH,
    CHAT_QUERY,
    CLIENT_TYPE,
    COSY_VERSION,
    DEFAULT_MODEL,
    GATEWAY_HOST,
    LOGIN_VERSION,
    MACHINE_OS,
    MACHINE_TYPE,
    MODEL_CATALOG,
    PARAMETERS_EXTRA,
    RETRYABLE_STATUS,
    SCENE,
    SESSION_TYPE,
    USER_AGENT,
)
from providers.qodercn.token import refresh_account
from providers.host_override import channel_host
from providers import model_limits
from providers.trae_shared import pick_with_refresh_fallback


# 与 qclaw / qwenwork / traework 的同名单行拷贝收敛：见 store_common.make_translator
translate_model = store_common.make_translator(
    lambda: channel_aliases(CHANNEL_ID, ALIASES), DEFAULT_MODEL
)


def chat_url() -> str:
    return f"{channel_host(CHANNEL_ID, 'gateway', GATEWAY_HOST)}{CHAT_PATH}?{CHAT_QUERY}"


def model_meta(model: str) -> dict:
    """Catalog entry（is_vl / is_reasoning / max_input_tokens）for an upstream key."""
    return MODEL_CATALOG.get(model) or {
        "display_name": model,
        "is_vl": False,
        "is_reasoning": False,
        "max_input_tokens": 180_000,
    }


def _ids(account: dict) -> tuple[str, str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    uid = str(account.get("uid") or extra.get("uid") or "")
    name = str(account.get("nickname") or account.get("name") or extra.get("name") or "")
    email = str(extra.get("email") or "")
    token = str(account.get("access_token") or "")
    return uid, name, email, token


def _split_messages(payload: dict) -> tuple[str, list]:
    system_parts = []
    messages = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or "user"
        if role == "system":
            content = item.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if content:
                system_parts.append(str(content))
            continue
        messages.append(item)
    return "\n\n".join(system_parts), messages


def _last_user_text(messages: list) -> str:
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
    return ""


def build_body(payload: dict) -> tuple[dict, str, str]:
    model = translate_model(str(payload.get("model") or DEFAULT_MODEL))
    request_id = str(payload.get("request_id") or uuid.uuid4())
    session_id = str(payload.get("session_id") or uuid.uuid4())
    system, messages = _split_messages(payload)
    if system:
        messages = [{"role": "system", "content": system}, *messages]
    last_user = _last_user_text(messages)
    meta = model_meta(model)
    parameters = {}
    for key in ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty",
                "context_length"):
        if key in payload and payload[key] is not None:
            parameters[key] = payload[key]
    if "max_tokens" not in parameters:
        parameters["max_tokens"] = model_limits.get_default_max_output_tokens(CHANNEL_ID)
    # 客户端总会带上这两个；`context_length` 默认取该模型自身的窗口上限。
    for key, value in PARAMETERS_EXTRA.items():
        if key == "context_length":
            parameters.setdefault(key, int(meta["max_input_tokens"]))
        else:
            parameters.setdefault(key, value)
    body = {
        "request_id": request_id,
        "request_set_id": str(payload.get("request_set_id") or request_id),
        "chat_record_id": str(payload.get("chat_record_id") or request_id),
        "session_id": session_id,
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {
            "text": last_user,
            "features": [],
            "extra": {
                "context": [],
                "modelConfig": {"key": model, "is_reasoning": bool(meta["is_reasoning"])},
                "originalContent": last_user,
            },
            "chatPrompt": "",
            "imageUrls": None,
        },
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "agent_id": "agent_common",
        "task_id": str(payload.get("task_id") or "common"),
        "session_type": SESSION_TYPE,
        "aliyun_user_type": "",
        "model_config": {
            "key": model,
            "display_name": meta["display_name"],
            "model": "",
            "format": "openai",
            "is_vl": bool(meta["is_vl"]),
            "is_reasoning": bool(meta["is_reasoning"]),
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": int(meta["max_input_tokens"]),
        },
        "system": system,
        "messages": messages,
        "tools": payload.get("tools") or [],
        "parameters": parameters,
        # `business` 是 qfmodel 可路由的必要条件（见 constants.BUSINESS）。
        # 其余模型加不加都一样，故无条件发送。
        "business": dict(BUSINESS),
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw, model


def static_headers(model: str, request_id: str, machine_id: str = "") -> dict[str, str]:
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Cosy-ClientType": CLIENT_TYPE,
        "Cosy-Business-Product": BUSINESS_PRODUCT,
        "Cosy-Business-Type": BUSINESS_TYPE,
        "Cosy-Scene": SCENE,
        "Cosy-Version": COSY_VERSION,
        "Cosy-MachineOS": MACHINE_OS,
        "Cosy-Data-Policy": "agree",
        "Login-Version": LOGIN_VERSION,
        "x-model-key": model,
        "x-model-source": "system",
        "Cache-Control": "no-cache",
    }
    if machine_id:
        headers["Cosy-MachineId"] = machine_id
        headers["Cosy-MachineToken"] = machine_id
        headers["Cosy-MachineType"] = MACHINE_TYPE
    return headers


def _headers_for(account: dict, url: str, body: str, model: str, request_id: str) -> dict[str, str]:
    uid, name, email, token = _ids(account)
    headers = static_headers(model, request_id, store.machine_id())
    headers.update(
        cosy.auth_headers(
            uid=uid,
            name=name,
            email=email,
            access_token=token,
            url=url,
            body=body,
            timestamp=int(time.time()),
            request_id=uuid.uuid4().hex,
        )
    )
    return headers


# ============================================================
# SSE：信封剥离
# ============================================================

def _decode_envelope(raw: str) -> tuple[str, object]:
    """把一行 ``data:`` 载荷解成 (kind, payload)。

    kind:
      ``chunk``  — 内层 OpenAI chunk（dict）
      ``done``   — 内层 body 为字符串 "[DONE]"，流结束
      ``error``  — envelope 的 statusCodeValue >= 400，payload 为
                   {"status": int, "code": str, "message": str}
      ``queued`` — 10605 modelQueued：可重试信号，额外带 ``retry_after``
      ``ignore`` — 心跳 / 无法解析 / event:finish 等
    """
    text = (raw or "").strip()
    if not text:
        return "ignore", None
    try:
        outer = json.loads(text)
    except json.JSONDecodeError:
        return "ignore", None
    if not isinstance(outer, dict) or "statusCodeValue" not in outer:
        return "ignore", None
    status = outer.get("statusCodeValue")
    body = outer.get("body")
    if isinstance(body, dict):
        return "chunk", body
    if not isinstance(body, str):
        return "ignore", None
    # 结束标志：内层 body 就是字面量 "[DONE]"（不是合法 JSON，须先判）
    if body.strip() == "[DONE]":
        return "done", None
    try:
        inner = json.loads(body)
    except json.JSONDecodeError:
        return "ignore", None
    if isinstance(inner, str):
        return ("done", None) if inner == "[DONE]" else ("ignore", None)
    if not isinstance(inner, dict):
        return "ignore", None
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        status_int = 200
    if status_int >= 400:
        code = str(inner.get("code") or status_int)
        message = inner.get("message")
        retry_after = None
        detail = message
        # 上游把队列详情包成 JSON 字符串；10605 时带 retryAfterSeconds。
        if isinstance(message, str) and message.startswith("{"):
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                detail = str(parsed.get("message") or message)
                value = parsed.get("retryAfterSeconds")
                if isinstance(value, (int, float)):
                    retry_after = float(value)
                if str(parsed.get("code") or "") == "10605":
                    code = "10605"
        payload = {"status": status_int, "code": code, "message": str(detail or "")[:240]}
        if code == "10605":
            payload["retry_after"] = retry_after
            return "queued", payload
        return "error", payload
    return "chunk", inner


def _collect(chunk: dict, state: dict) -> tuple[list, dict | None] | None:
    """把内层 chunk 归一成 (choices, usage)（None = 无须下发）。"""
    choices_out = []
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta_src = choice.get("delta") or choice.get("message") or {}
        if not isinstance(delta_src, dict):
            delta_src = {}
        delta_out = {}
        if not state["role_sent"] and delta_src.get("role"):
            delta_out["role"] = delta_src["role"]
            state["role_sent"] = True
        content = delta_src.get("content")
        if content:
            delta_out["content"] = content
            state["content"] = True
        reasoning = delta_src.get("reasoning_content")
        if reasoning:
            delta_out["reasoning_content"] = reasoning
            state["reasoning"] = True
        for call in delta_src.get("tool_calls") or []:
            delta_out.setdefault("tool_calls", []).append(call)
        out_choice = {"index": choice.get("index", 0), "delta": delta_out}
        finish = choice.get("finish_reason")
        if finish is not None:
            out_choice["finish_reason"] = finish
            state["finish"] = finish
        choices_out.append(out_choice)

    usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else None
    if usage is not None:
        state["usage"] = usage

    if not choices_out and usage is None:
        return None
    if choices_out and not any(c["delta"] for c in choices_out) and all(
        c.get("finish_reason") is None for c in choices_out
    ) and usage is None:
        # 纯空 delta（如仅带 index）无须下发
        return None
    return choices_out, usage


def openai_chunk(state: dict, requested_model: str, choices, usage) -> dict:
    """组装标准 OpenAI ``chat.completion.chunk``；``model`` 用请求键覆盖（上游恒发 auto）。"""
    out = {
        "id": state.get("id") or "chatcmpl-qodercn",
        "object": "chat.completion.chunk",
        "created": int(state.get("created") or time.time()),
        "model": requested_model,
        "choices": choices or [],
    }
    if usage is not None:
        out["usage"] = usage
    return out


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    body, raw, upstream_model = build_body(payload)
    model_name = log_model if log_model is not None else payload.get("model", upstream_model)
    url = chat_url()
    request_id = str(body.get("request_id") or uuid.uuid4())

    if client_wants_stream:
        return ("stream", _stream(raw, url, request_id, upstream_model, api_key_info, model_name))

    tried: set[int] = set()
    last_error = None
    for attempt in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        try:
            headers = _headers_for(account, url, raw, upstream_model, request_id)
            state: dict = {"role_sent": False, "content": False, "reasoning": False,
                           "finish": None, "usage": None}
            chunks: list[dict] = []
            error_row = None
            async with get_client().stream("POST", url, headers=headers, content=raw,
                                           timeout=httpx.Timeout(None, connect=10.0, read=None)) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    auth_manager.mark_account_failure(account["id"], response.status_code)
                    last_error = ("error", (response.status_code,
                                            {"error": {"message": text, "type": "server_error"}}))
                    await _log(api_key_info, account, model_name, False, "error",
                               response.status_code, text, t0)
                    if response.status_code not in RETRYABLE_STATUS:
                        return last_error
                    await retry_delay(attempt)
                    continue
                auth_manager.mark_account_success(account["id"])
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    kind, data = _decode_envelope(line[5:])
                    if kind == "done":
                        break
                    if kind == "queued":
                        error_row = ("queued", data)
                        break
                    if kind == "error":
                        error_row = ("error", data)
                        break
                    if kind != "chunk":
                        continue
                    normalized = _collect(data, state)
                    if normalized is None:
                        continue
                    state.setdefault("id", data.get("id"))
                    state.setdefault("created", data.get("created"))
                    choices, usage = normalized
                    chunks.append(openai_chunk(state, upstream_model, choices, usage))
            if error_row is not None:
                kind, data = error_row
                status = 429 if kind == "queued" else int((data or {}).get("status") or 400)
                message = str((data or {}).get("message") or kind)
                last_error = ("error", (status, {"error": {"message": message,
                                                           "type": "upstream_error"}}))
                await _log(api_key_info, account, model_name, False, "error", status, message, t0)
                if status in RETRYABLE_STATUS and attempt < 2:
                    await retry_delay(attempt, retry_after=(data or {}).get("retry_after"))
                    continue
                return last_error
            usage = state.get("usage") or {}
            finish = state.get("finish") or "stop"
            text = "".join(
                (c["choices"][0]["delta"].get("content") or "")
                for c in chunks if c.get("choices")
            )
            reasoning = "".join(
                (c["choices"][0]["delta"].get("reasoning_content") or "")
                for c in chunks if c.get("choices")
            )
            message: dict = {"role": "assistant", "content": text}
            if reasoning:
                message["reasoning_content"] = reasoning
            aggregated = {
                "id": state.get("id") or "chatcmpl-qodercn",
                "object": "chat.completion",
                "created": int(state.get("created") or time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            await _log(
                api_key_info, account, model_name, False, finish, 200, "", t0,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                usage=usage,
            )
            return ("json", aggregated)
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            await retry_delay(attempt)
            continue
    return last_error or (
        "error",
        (503, {
            "error": {
                "message": "No available accounts",
                "type": "channel_unavailable",
                "code": "channel_unavailable",
                "channel": CHANNEL_ID,
            }
        }),
    )


async def _stream(raw: str, url: str, request_id: str, upstream_model: str,
                  api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b'data: {"error":{"message":"No available accounts"}}\n\n'
    last_status = 503
    t0 = time.time()
    # first_token_ms 基线取账号轮换循环之前（= 用户真实等待，含 pick/refresh/退避）
    ft_t0 = time.monotonic()
    first_token_ms: int | None = None
    for attempt in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        output_started = False
        usage = None
        state: dict = {"role_sent": False, "content": False, "reasoning": False,
                       "finish": None, "usage": None}
        try:
            headers = _headers_for(account, url, raw, upstream_model, request_id)
            async with get_client().stream(
                "POST", url, headers=headers, content=raw,
                timeout=httpx.Timeout(None, connect=10.0, read=None),
            ) as response:
                last_status = response.status_code
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    auth_manager.mark_account_failure(account["id"], response.status_code)
                    last_error = f"data: {json.dumps({'error': {'message': text}}, ensure_ascii=False)}\n\n".encode()
                    if response.status_code not in RETRYABLE_STATUS:
                        yield last_error
                        await _log(api_key_info, account, model_name, True, "error",
                                   response.status_code, text, t0)
                        return
                    await retry_delay(attempt)
                    continue
                auth_manager.mark_account_success(account["id"])
                error_row = None
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    kind, data = _decode_envelope(line[5:])
                    if kind == "done":
                        break
                    if kind in ("queued", "error"):
                        error_row = (kind, data)
                        break
                    if kind != "chunk":
                        continue
                    normalized = _collect(data, state)
                    if normalized is None:
                        continue
                    state.setdefault("id", data.get("id"))
                    state.setdefault("created", data.get("created"))
                    choices, chunk_usage = normalized
                    if chunk_usage is not None:
                        usage = chunk_usage
                    if first_token_ms is None and (
                        state["content"] or state["reasoning"]
                    ):
                        first_token_ms = int((time.monotonic() - ft_t0) * 1000)
                    if choices:
                        output_started = True
                    # usage chunk 单独下发（choices 为空），保持 OpenAI 语义
                    payload_out = openai_chunk(state, upstream_model, choices, chunk_usage)
                    yield f"data: {json.dumps(payload_out, ensure_ascii=False)}\n\n".encode("utf-8")
                if error_row is not None:
                    kind, data = error_row
                    status = 429 if kind == "queued" else int((data or {}).get("status") or 400)
                    message = str((data or {}).get("message") or kind)
                    last_status = status
                    last_error = f"data: {json.dumps({'error': {'message': message}}, ensure_ascii=False)}\n\n".encode()
                    if not output_started:
                        if status in RETRYABLE_STATUS and attempt < 2:
                            await retry_delay(attempt, retry_after=(data or {}).get("retry_after"))
                            continue
                        yield last_error
                        await _log(api_key_info, account, model_name, True, "error", status, message, t0)
                        return
                    await _log(api_key_info, account, model_name, True, "error", status, message, t0)
                    return
            if output_started:
                yield b"data: [DONE]\n\n"
            if isinstance(usage, dict):
                await _log(
                    api_key_info, account, model_name, True,
                    "stop", 200, "", t0,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                    usage=usage, first_token_ms=first_token_ms,
                )
            else:
                await _log(api_key_info, account, model_name, True, "stop", 200, "", t0,
                           first_token_ms=first_token_ms)
            return
        except httpx.HTTPError as exc:
            if output_started:
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = f"data: {json.dumps({'error': {'message': str(exc)[:240]}}, ensure_ascii=False)}\n\n".encode()
            last_status = 503
            await retry_delay(attempt)
            continue
    yield last_error
    await _log(api_key_info, None, model_name, True, "error", last_status, "stream failed", t0)


async def fetch_quota(account: dict) -> dict:
    """Query Qoder CN quota via the confirmed /api/v2/quota/usage endpoint."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    access = str(account.get("access_token") or "")
    if access:
        headers["Authorization"] = f"Bearer {access}"
    url = f"{channel_host(CHANNEL_ID, 'gateway', GATEWAY_HOST)}/api/v2/quota/usage"
    try:
        response = await get_client().get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        return {"ok": False, "message": str(exc)[:240]}
    if response.status_code >= 400:
        return {"ok": False, "http_status": response.status_code}
    try:
        data = response.json()
    except ValueError:
        data = {}
    return {"ok": True, "remaining": _quota_remaining(data), "raw": (data or {})}


def _quota_remaining(data: dict) -> float | None:
    """Prefer Qoder's nested `addOnQuota`/`userQuota` remaining over bare keys."""
    if not isinstance(data, dict):
        return None
    for group in ("addOnQuota", "userQuota"):
        bucket = data.get(group)
        if isinstance(bucket, dict):
            value = bucket.get("remaining")
            if isinstance(value, (int, float)):
                return float(value)
    for key in ("remaining", "remain", "available", "balance", "total_dosage"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


async def test_chat(account: dict, model: str = DEFAULT_MODEL, prompt: str = "请回复：pong") -> dict:
    async def send(payload: dict) -> tuple:
        body, raw, upstream_model = build_body(payload)
        url = chat_url()
        headers = _headers_for(account, url, raw, upstream_model, str(body["request_id"]))
        state: dict = {"role_sent": False, "content": False, "reasoning": False,
                       "finish": None, "usage": None}
        chunks: list[dict] = []
        async with httpx.AsyncClient(timeout=45.0) as client:
            async with client.stream("POST", url, headers=headers, content=raw) as response:
                status = response.status_code
                if status >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    return status, text, None
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    kind, data = _decode_envelope(line[5:])
                    if kind == "done":
                        break
                    if kind in ("queued", "error"):
                        return int((data or {}).get("status") or 400), str((data or {}).get("message") or kind), None
                    if kind != "chunk":
                        continue
                    normalized = _collect(data, state)
                    if normalized is None:
                        continue
                    state.setdefault("id", data.get("id"))
                    state.setdefault("created", data.get("created"))
                    choices, usage = normalized
                    chunks.append(openai_chunk(state, upstream_model, choices, usage))
        usage = state.get("usage") or {}
        text = "".join(
            (c["choices"][0]["delta"].get("content") or "")
            for c in chunks if c.get("choices")
        )
        reasoning = "".join(
            (c["choices"][0]["delta"].get("reasoning_content") or "")
            for c in chunks if c.get("choices")
        )
        message: dict = {"role": "assistant", "content": text}
        if reasoning:
            message["reasoning_content"] = reasoning
        return 200, None, {
            "id": state.get("id") or "chatcmpl-qodercn",
            "object": "chat.completion",
            "created": int(state.get("created") or time.time()),
            "model": upstream_model,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": state.get("finish") or "stop"}],
            "usage": usage or {},
        }

    return await store_common.run_test_chat(model or DEFAULT_MODEL, prompt or "请回复：pong", send)


def _pick(tried: set[int]):
    # 兜底收敛到共享实现（含 refresh 失败 60s 负缓存），与 traework 同源
    return pick_with_refresh_fallback(CHANNEL_ID, refresh_account, exclude_ids=tried)


async def refresh(account: dict) -> dict:
    """Refresh hook consumed by `pick_with_refresh_fallback` / the facade.

    Thin re-export of `token.refresh_account` so callers can use
    `chat.refresh` (the same shape qclaw/qwenwork/traework expose).
    """
    return await refresh_account(account)


async def _log(api_key_info, account, model_name, stream, finish_reason, status_code,
               error_msg, t0, increment_usage=True, usage=None, first_token_ms=None,
               prompt_tokens=0, completion_tokens=0, total_tokens=0):
    # 落库线程化 + 语义收敛：见 store_common.log_request（多家 _log 的一份实现）
    await store_common.log_request(
        api_key_info, account,
        channel=CHANNEL_ID, model=model_name, stream=stream, usage=usage,
        finish_reason=finish_reason, status_code=status_code,
        duration_ms=int((time.time() - t0) * 1000), error_msg=error_msg,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=total_tokens, increment_usage=increment_usage,
        created_at=int(t0), first_token_ms=first_token_ms,
    )
