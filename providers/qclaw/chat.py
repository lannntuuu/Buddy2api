"""Aizone chat client. Isolated from the WorkBuddy outbound stack."""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncGenerator

import httpx

import auth_manager
import database as db
from providers.qclaw.constants import AIZONE_BASE, ALIASES, CHANNEL_ID, RETRYABLE_STATUS
from providers.qclaw.jprx import JprxError, time_sync
from providers.qclaw.sign import aizone_headers


def translate_model(model: str) -> str:
    inner = (model or "default").strip() or "default"
    return ALIASES.get(inner, inner)


def _ids(account: dict) -> tuple[str, str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    guid = str(extra.get("guid") or "") 
    user_id = str(account.get("uid") or "")
    jwt = str(account.get("refresh_token") or "")
    api_key = str(account.get("access_token") or "")
    return guid, user_id, jwt, api_key


def _log(api_key_info, account, model_name, stream, prompt_t, completion_t, total_t,
         finish_reason, status_code, error_msg, t0, increment_usage=True):
    try:
        db.record_request(
            {
                "api_key_id": api_key_info["id"] if api_key_info else None,
                "api_key_name": api_key_info["name"] if api_key_info else None,
                "account_id": account["id"] if account else None,
                "account_name": account.get("name") if account else None,
                "provider": CHANNEL_ID,
                "model": model_name,
                "stream": 1 if stream else 0,
                "prompt_tokens": prompt_t,
                "completion_tokens": completion_t,
                "total_tokens": total_t,
                "credit": 0,
                "finish_reason": finish_reason,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status_code,
                "error_msg": error_msg,
                "increment_usage": increment_usage,
            }
        )
    except Exception:
        pass


def _build_body(payload: dict) -> tuple[dict, str]:
    body = dict(payload)
    body["model"] = translate_model(str(body.get("model") or "default"))
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw


async def _headers_for(account: dict, raw_body: str) -> dict[str, str]:
    guid, user_id, jwt, api_key = _ids(account)
    try:
        server_ts = await time_sync(account)
    except JprxError:
        server_ts = str(int(time.time()))
    return aizone_headers(
        api_key=api_key,
        jwt=jwt,
        guid=guid,
        account=user_id,
        body=raw_body,
        server_ts=server_ts,
        trace_id=str(uuid.uuid4()),
    )


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
            headers = await _headers_for(account, raw)
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{AIZONE_BASE}/chat/completions",
                    headers=headers,
                    content=raw,
                )
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            continue
        if response.status_code < 400:
            auth_manager.mark_account_success(account["id"])
            try:
                data = response.json()
            except ValueError:
                data = {"id": "qclaw", "object": "chat.completion", "choices": []}
            usage = data.get("usage") or {}
            _log(
                api_key_info, account, model_name, False,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                ((data.get("choices") or [{}])[0].get("finish_reason") or "stop"),
                response.status_code, "", t0,
            )
            return ("json", data)
        status = response.status_code
        try:
            detail = response.json()
        except ValueError:
            detail = {"error": {"message": response.text[:400], "type": "server_error"}}
        auth_manager.mark_account_failure(account["id"], status)
        last_error = ("error", (status, detail))
        _log(
            api_key_info, account, model_name, False,
            0, 0, 0, "retry" if status in RETRYABLE_STATUS and attempt < 2 else "error",
            status, str(detail)[:400], t0,
            increment_usage=status not in RETRYABLE_STATUS or attempt == 2,
        )
        if status not in RETRYABLE_STATUS:
            return last_error
    return last_error or (
        "error",
        (503, {"error": {"message": "No available accounts", "type": "channel_unavailable", "code": "channel_unavailable", "channel": CHANNEL_ID}}),
    )


async def _stream(body: dict, raw: str, api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b"data: {\"error\":{\"message\":\"No available accounts\"}}\n\n"
    last_status = 503
    for attempt in range(3):
        account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        output_started = False
        try:
            headers = await _headers_for(account, raw)
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{AIZONE_BASE}/chat/completions",
                    headers=headers,
                    content=raw,
                ) as response:
                    last_status = response.status_code
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = f"data: {json.dumps({'error': {'message': text}}, ensure_ascii=False)}\n\n".encode()
                        if response.status_code not in RETRYABLE_STATUS:
                            yield last_error
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", response.status_code, text, t0)
                            return
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        output_started = True
                        yield chunk
            _log(api_key_info, account, model_name, True, 0, 0, 0, "stop", 200, "", t0)
            return
        except httpx.HTTPError as exc:
            if output_started:
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = f"data: {json.dumps({'error': {'message': str(exc)[:240]}}, ensure_ascii=False)}\n\n".encode()
            last_status = 503
            continue
    yield last_error
    _log(api_key_info, None, model_name, True, 0, 0, 0, "error", last_status, "stream failed", time.time())


async def test_chat(account: dict, model: str = "default", prompt: str = "ping") -> dict:
    payload = {
        "model": model or "default",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": False,
        "max_tokens": 16,
    }
    t0 = time.time()
    body, raw = _build_body(payload)
    try:
        headers = await _headers_for(account, raw)
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{AIZONE_BASE}/chat/completions",
                headers=headers,
                content=raw,
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "message": str(exc)[:240]}
    duration_ms = int((time.time() - t0) * 1000)
    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "message": response.text[:400],
        }
    data = response.json()
    message = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return {
        "ok": True,
        "status_code": 200,
        "duration_ms": duration_ms,
        "model": data.get("model"),
        "message": str(message)[:240],
        "usage": data.get("usage") or {},
    }
