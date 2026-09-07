"""Aizone chat client. Isolated from the WorkBuddy outbound stack."""

from __future__ import annotations

import json
import time
from typing import AsyncGenerator

import httpx

from accounts import auth_manager
from storage.http_pool import get_client
from providers import store_common
from providers.model_config import channel_aliases
from providers.qclaw.constants import AIZONE_BASE, ALIASES, CHANNEL_ID, RETRYABLE_STATUS
from providers.retry import retry_delay
from providers.qclaw.sign import aizone_headers
from providers.host_override import channel_host


# 与 qwenwork / traework 的同名单行拷贝收敛：见 store_common.make_translator
translate_model = store_common.make_translator(
    lambda: channel_aliases(CHANNEL_ID, ALIASES), "default"
)


def _alt_text(value) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            elif item:
                parts.append(str(item))
        return "".join(parts)
    return ""


def fill_empty_content(payload: dict) -> dict:
    """Aizone often fills reasoning_content first; OpenAI clients only render content."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    content = out.get("content")
    if content not in (None, ""):
        return out
    for key in ("reasoning_content", "reasoning"):
        text = _alt_text(out.get(key))
        if text:
            out["content"] = text
            return out
    return out


def _normalize_completion(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    choices = []
    for choice in out.get("choices") or []:
        if not isinstance(choice, dict):
            choices.append(choice)
            continue
        item = dict(choice)
        if isinstance(item.get("message"), dict):
            item["message"] = fill_empty_content(item["message"])
        if isinstance(item.get("delta"), dict):
            item["delta"] = fill_empty_content(item["delta"])
        choices.append(item)
    out["choices"] = choices
    return out


def _ids(account: dict) -> tuple[str, str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    guid = str(extra.get("guid") or "") 
    user_id = str(account.get("uid") or "")
    jwt = str(account.get("refresh_token") or "")
    api_key = str(account.get("access_token") or "")
    return guid, user_id, jwt, api_key


async def _log(api_key_info, account, model_name, stream, prompt_t, completion_t, total_t,
               finish_reason, status_code, error_msg, t0, increment_usage=True, usage=None,
               first_token_ms=None):
    # 落库线程化 + 语义收敛：见 store_common.log_request（三家 _log 的一份实现）
    await store_common.log_request(
        api_key_info, account,
        channel=CHANNEL_ID, model=model_name, stream=stream, usage=usage,
        finish_reason=finish_reason, status_code=status_code,
        duration_ms=int((time.time() - t0) * 1000), error_msg=error_msg,
        prompt_tokens=prompt_t, completion_tokens=completion_t, total_tokens=total_t,
        increment_usage=increment_usage,
        created_at=int(t0), first_token_ms=first_token_ms,
    )


def _build_body(payload: dict) -> tuple[dict, str]:
    body = dict(payload)
    body["model"] = translate_model(str(body.get("model") or "default"))
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw


def _headers_for(account: dict) -> dict[str, str]:
    guid, user_id, jwt, api_key = _ids(account)
    return aizone_headers(api_key=api_key, jwt=jwt, guid=guid, account=user_id)


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    model_name = log_model if log_model is not None else payload.get("model", "default")
    body, raw = _build_body(payload)

    if client_wants_stream:
        return ("stream", _stream(body, raw, api_key_info, model_name))

    tried: set[int] = set()
    last_error = None
    for attempt in range(3):
        account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        try:
            headers = _headers_for(account)
            client = get_client()
            response = await client.post(
                f"{channel_host(CHANNEL_ID, 'aizone_base', AIZONE_BASE)}/chat/completions",
                headers=headers,
                content=raw,
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            await retry_delay(attempt)
            continue
        if response.status_code < 400:
            auth_manager.mark_account_success(account["id"])
            try:
                data = _normalize_completion(response.json())
            except ValueError:
                data = {"id": "qclaw", "object": "chat.completion", "choices": []}
            usage = data.get("usage") or {}
            await _log(
                api_key_info, account, model_name, False,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                ((data.get("choices") or [{}])[0].get("finish_reason") or "stop"),
                response.status_code, "", t0, usage=usage,
            )
            return ("json", data)
        status = response.status_code
        try:
            detail = response.json()
        except ValueError:
            detail = {"error": {"message": response.text[:400], "type": "server_error"}}
        auth_manager.mark_account_failure(account["id"], status)
        last_error = ("error", (status, detail))
        await _log(
            api_key_info, account, model_name, False,
            0, 0, 0, "retry" if status in RETRYABLE_STATUS and attempt < 2 else "error",
            status, str(detail)[:400], t0,
            increment_usage=status not in RETRYABLE_STATUS or attempt == 2,
        )
        if status not in RETRYABLE_STATUS:
            return last_error
        await retry_delay(attempt)
    return last_error or (
        "error",
        (503, {"error": {"message": "No available accounts", "type": "channel_unavailable", "code": "channel_unavailable", "channel": CHANNEL_ID}}),
    )


async def _stream(body: dict, raw: str, api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b"data: {\"error\":{\"message\":\"No available accounts\"}}\n\n"
    last_status = 503
    t0 = time.time()
    # first_token_ms 基线取账号轮换循环之前（= 用户真实等待，含 pick/退避）
    ft_t0 = time.monotonic()
    first_token_ms: int | None = None
    for attempt in range(3):
        account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        output_started = False
        usage = None
        try:
            headers = _headers_for(account)
            client = get_client()
            async with client.stream(
                "POST",
                f"{channel_host(CHANNEL_ID, 'aizone_base', AIZONE_BASE)}/chat/completions",
                headers=headers,
                content=raw,
                timeout=httpx.Timeout(None, connect=10.0, read=None),
            ) as response:
                last_status = response.status_code
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    auth_manager.mark_account_failure(account["id"], response.status_code)
                    last_error = f"data: {json.dumps({'error': {'message': text}}, ensure_ascii=False)}\n\n".encode()
                    if response.status_code not in RETRYABLE_STATUS:
                        yield last_error
                        await _log(api_key_info, account, model_name, True, 0, 0, 0, "error", response.status_code, text, t0)
                        return
                    await retry_delay(attempt)
                    continue
                auth_manager.mark_account_success(account["id"])
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if first_token_ms is None:
                        first_token_ms = int((time.monotonic() - ft_t0) * 1000)
                    if not line.startswith("data:"):
                        output_started = True
                        yield (line + "\n").encode("utf-8")
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        output_started = True
                        yield b"data: [DONE]\n\n"
                        continue
                    try:
                        parsed = _normalize_completion(json.loads(data))
                        payload = json.dumps(parsed, ensure_ascii=False)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                        payload = data
                    if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
                        # 上游最后一个 chunk 常带 usage，用它回填统计
                        usage = parsed["usage"]
                    output_started = True
                    yield f"data: {payload}\n\n".encode("utf-8")
            if isinstance(usage, dict):
                await _log(
                    api_key_info, account, model_name, True,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("total_tokens") or (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)),
                    "stop", 200, "", t0, usage=usage,
                    first_token_ms=first_token_ms,
                )
            else:
                await _log(api_key_info, account, model_name, True, 0, 0, 0, "stop", 200, "", t0,
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
    await _log(api_key_info, None, model_name, True, 0, 0, 0, "error", last_status, "stream failed", t0)


async def test_chat(account: dict, model: str = "default", prompt: str = "ping") -> dict:
    async def send(payload: dict) -> tuple:
        body, raw = _build_body(payload)
        headers = _headers_for(account)
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{channel_host(CHANNEL_ID, 'aizone_base', AIZONE_BASE)}/chat/completions",
                headers=headers,
                content=raw,
            )
        if response.status_code >= 400:
            return response.status_code, response.text[:400], None
        try:
            data = _normalize_completion(response.json())
        except ValueError:
            data = {}
        return response.status_code, None, data

    return await store_common.run_test_chat(model or "default", prompt or "ping", send)
