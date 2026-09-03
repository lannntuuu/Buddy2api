"""GMI Cloud chat bridge.

Behaviour:
  - OpenAI Chat Completions in → OpenAI Chat Completions out (we don't translate
    anything; the upstream IS OpenAI-compat). Only model alias resolution
    happens here.
  - Stream pass-through: we forward SSE chunks verbatim so token usage chunks
    (when stream_options.include_usage=true is set) reach the client unchanged.
  - One shared httpx.AsyncClient (keep-alive) per event loop, with a test
    transport escape hatch (parity with traesolo).
  - Usage reporting: fire db.record_request at the end of every request so the
    existing `logs` table + `api_key_daily_usage` aggregation picks up
    per-key token totals for free.

ponytail: this module is intentionally the only place we touch the upstream.
Add new OpenAI-compat platforms by copying this file, not by generalising
further — the protocol surface (headers, error shape, SSE) is what varies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

import httpx

from providers.gmi import store
from providers.gmi.constants import (
    ALIASES,
    CHANNEL_ID,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    EP_CHAT,
    EP_MODELS,
    MODELS_CACHE_TTL,
    STATIC_MODELS,
)
from providers.host_override import channel_host
from providers.model_config import channel_aliases, channel_model_ids
from storage import database as db

logger = logging.getLogger("gmi.chat")

_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

# Module-level client cache (per running loop).
_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None
_models_cache: dict = {"fetched_at": 0.0, "ids": []}

def set_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None:
    """Tests swap the transport; rebuild client lazily."""
    global _TRANSPORT, _client, _client_loop
    _TRANSPORT = transport
    _client = None
    _client_loop = None


def _get_client() -> httpx.AsyncClient:
    """One long-lived httpx client per running loop. Reused for chat, models,
    and quota probes — keeps TLS handshakes to one per process per host."""
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client.is_closed or (loop is not None and _client_loop is not loop):
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), transport=_TRANSPORT)
        _client_loop = loop
    return _client


# ────────────────────────── auth header ──────────────────────────


def _auth_headers(account: dict, stream: bool) -> dict[str, str]:
    token = str(account.get("access_token") or "").strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "buddy2api/gmi",
    }


def _base_url(account: dict) -> str:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    host = str(
        extra.get("base_url")
        or account.get("domain")
        or channel_host(CHANNEL_ID, "base_url", DEFAULT_BASE_URL)
    ).rstrip("/")
    return host or DEFAULT_BASE_URL


# ────────────────────────── model resolution ──────────────────────


def accepts_model(inner: str) -> bool:
    """True iff we recognise this id (alias, dynamic list or static list).

    Whitelist/aliases honour admin customisation (gmi.models / gmi.aliases
    settings), same contract as qclaw / traesolo.
    """
    value = (inner or "").strip()
    if value in channel_aliases(CHANNEL_ID, ALIASES):
        return True
    if value in effective_model_ids():
        return True
    return False


def translate_model(model: str) -> str:
    return channel_aliases(CHANNEL_ID, ALIASES).get(model, model)


def effective_model_ids() -> list[str]:
    """当前生效模型白名单：管理员自定义 > 动态 /v1/models > 内置静态表。"""
    return channel_model_ids(CHANNEL_ID, _effective_default_ids())


def _effective_default_ids() -> list[str]:
    return dynamic_model_ids() or list(STATIC_MODELS)


def dynamic_model_ids() -> list[str]:
    """最近一次 /v1/models 动态拉取结果（无缓存时为空列表）。"""
    return list(_models_cache["ids"])


async def refresh_model_ids(force: bool = False) -> list[str]:
    """Refresh the dynamic model id list from /v1/models. Cached MODELS_CACHE_TTL."""
    now = time.time()
    if not force and _models_cache["ids"] and (now - _models_cache["fetched_at"]) < MODELS_CACHE_TTL:
        return list(_models_cache["ids"])
    accounts = db.get_active_accounts(CHANNEL_ID) or []
    account = accounts[0] if accounts else None
    if not account:
        # Try env-key bootstrap (no admin UI interaction required).
        account = store.ensure_env_account()
    if not account:
        _models_cache.update({"fetched_at": now, "ids": list(STATIC_MODELS)})
        return list(STATIC_MODELS)
    try:
        client = _get_client()
        r = await client.get(f"{_base_url(account)}{EP_MODELS}", headers=_auth_headers(account, stream=False))
        if r.status_code >= 400:
            raise httpx.HTTPStatusError("models fetch failed", request=r.request, response=r)
        data = r.json() if r.content else {}
        ids = []
        for item in (data.get("data") or []):
            mid = item.get("id") if isinstance(item, dict) else None
            if isinstance(mid, str) and mid:
                ids.append(mid)
        if ids:
            _models_cache.update({"fetched_at": now, "ids": ids})
            return ids
    except Exception as exc:
        logger.warning("gmi /v1/models refresh failed: %s", exc)
    _models_cache.update({"fetched_at": now, "ids": list(STATIC_MODELS)})
    return list(STATIC_MODELS)


def cached_model_ids() -> list[str]:
    """Sync accessor used by list_models(): 当前生效白名单（自定义 > 动态 > 静态）。"""
    return effective_model_ids()


# ────────────────────────── account pick ──────────────────────────


async def _pick_account(exclude_ids: set[int] | None = None):
    """Pick the single active GMI account. Single-key platform → no rotation."""
    if store.ensure_env_account() is None and not db.list_accounts(provider=CHANNEL_ID):
        return None
    from accounts import auth_manager

    return auth_manager.pick_account(set(exclude_ids or ()), provider=CHANNEL_ID)


# ────────────────────────── usage logging ─────────────────────────


def _record(api_key_info: dict | None, account: dict | None, *,
            model: str, stream: bool, finish_reason: str, status_code: int,
            prompt_tokens: int, completion_tokens: int, total_tokens: int,
            error_msg: str = "", t0: float = 0.0, usage_payload: dict | None = None):
    """Fire-and-forget log write — mirrors upstream/proxy._log_request."""
    elapsed_ms = int((time.time() - t0) * 1000) if t0 else 0
    payload = {
        "api_key_id": (api_key_info or {}).get("id"),
        "api_key_name": (api_key_info or {}).get("name"),
        "account_id": (account or {}).get("id") if account else None,
        "account_name": (account or {}).get("name") if account else None,
        "provider": CHANNEL_ID,
        "model": model,
        "stream": 1 if stream else 0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "credit": 0,
        "finish_reason": finish_reason,
        "duration_ms": elapsed_ms,
        "status_code": status_code,
        "error_msg": error_msg[:500] if error_msg else "",
        "client": (api_key_info or {}).get("_client_tag"),
        "client_version": (api_key_info or {}).get("_client_version"),
        "usage_json": json.dumps(usage_payload, ensure_ascii=False) if usage_payload else None,
        "credit_source": None,
        "increment_usage": True,
    }
    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, db.record_request, payload)
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    except RuntimeError:
        # No running loop (test harness). Fall back to sync.
        try:
            db.record_request(payload)
        except Exception:
            pass


# ────────────────────────── non-streaming ─────────────────────────


async def _run_non_stream(
    account: dict, payload: dict, api_key_info: dict | None, model: str
) -> tuple:
    """Single request → single JSON. Returns ('json', dict) or ('error', tuple)."""
    t0 = time.time()
    # Ensure usage is reported in non-stream too.
    body = {**payload, "stream": False}
    body.setdefault("stream_options", {"include_usage": True})
    try:
        client = _get_client()
        r = await client.post(
            f"{_base_url(account)}{EP_CHAT}",
            headers=_auth_headers(account, stream=False),
            json=body,
        )
    except httpx.HTTPError as exc:
        _record(api_key_info, account, model=model, stream=False, finish_reason="network_error",
                status_code=502, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg=str(exc), t0=t0)
        return "error", (502, {"error": {"message": f"upstream network error: {exc}", "type": "server_error"}})

    if r.status_code >= 400:
        body_txt = r.text[:500]
        _record(api_key_info, account, model=model, stream=False, finish_reason="error",
                status_code=r.status_code, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg=body_txt, t0=t0)
        # Pass through upstream error envelope if it parses as JSON.
        try:
            upstream_err = r.json()
        except Exception:
            upstream_err = {"error": {"message": body_txt, "type": "upstream_error"}}
        return "error", (r.status_code, upstream_err)

    try:
        data = r.json()
    except json.JSONDecodeError:
        _record(api_key_info, account, model=model, stream=False, finish_reason="error",
                status_code=502, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg="upstream returned non-JSON", t0=t0)
        return "error", (502, {"error": {"message": "upstream returned non-JSON", "type": "server_error"}})

    usage = data.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    tt = int(usage.get("total_tokens") or (pt + ct))
    finish = "stop"
    for choice in (data.get("choices") or []):
        if isinstance(choice, dict) and choice.get("finish_reason"):
            finish = choice["finish_reason"]
            break
    _record(api_key_info, account, model=model, stream=False, finish_reason=finish,
            status_code=200, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            t0=t0, usage_payload=usage)
    return "json", data


# ────────────────────────── streaming ────────────────────────────


def _sse_passthrough_line(parsed_json: dict | None) -> bytes:
    if parsed_json is None:
        return b""
    return f"data: {json.dumps(parsed_json, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


async def _stream_chat(
    account: dict, payload: dict, api_key_info: dict | None, model: str
) -> AsyncGenerator[str, None]:
    """Stream chunks out as SSE strings. Accumulate usage for logging; we do
    not synthesise a final usage chunk — the upstream emits its own (because we
    set stream_options.include_usage)."""
    t0 = time.time()
    body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    usage_payload: dict | None = None
    final_finish = "stop"
    last_status = 200
    error_msg = ""
    emitted = False

    try:
        client = _get_client()
        async with client.stream(
            "POST",
            f"{_base_url(account)}{EP_CHAT}",
            headers=_auth_headers(account, stream=True),
            json=body,
        ) as r:
            last_status = r.status_code
            if r.status_code >= 400:
                body_txt = await r.aread()
                error_msg = body_txt.decode("utf-8", "replace")[:500]
                _record(api_key_info, account, model=model, stream=True, finish_reason="error",
                        status_code=r.status_code, prompt_tokens=0, completion_tokens=0,
                        total_tokens=0, error_msg=error_msg, t0=t0)
                # Emit an OpenAI-shaped error chunk so clients see something.
                err_obj = {"error": {"message": error_msg, "type": "upstream_error"}}
                yield _sse_passthrough_line(err_obj).decode("utf-8")
                yield "data: [DONE]\n\n"
                return

            buffer = b""
            async for line in r.aiter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    raw = line
                    text = raw.decode("utf-8", "replace")
                else:
                    text = line
                    raw = text.encode("utf-8")

                # Forward unchanged to client (the SSE line already includes its
                # trailing \n\n via iter_lines when the upstream flushes them).
                # We split into individual data: lines so the client gets
                # packet-aligned chunks.
                if text.startswith("data:"):
                    emitted = True
                    # Try to grab usage off the last frame for logging.
                    data_part = text[5:].strip()
                    if data_part and data_part != "[DONE]":
                        try:
                            parsed = json.loads(data_part)
                            usage = parsed.get("usage") if isinstance(parsed, dict) else None
                            if isinstance(usage, dict):
                                usage_payload = usage
                                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                                completion_tokens = int(usage.get("completion_tokens") or 0)
                                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                            for ch in (parsed.get("choices") or []) if isinstance(parsed, dict) else []:
                                fr = ch.get("finish_reason") if isinstance(ch, dict) else None
                                if fr:
                                    final_finish = fr
                        except json.JSONDecodeError:
                            pass
                    yield text + "\n\n"
                else:
                    # event: lines, comments, blanks — forward verbatim.
                    yield text + "\n"
    except httpx.HTTPError as exc:
        error_msg = str(exc)
        last_status = 502
        _record(api_key_info, account, model=model, stream=True, finish_reason="network_error",
                status_code=502, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, error_msg=error_msg, t0=t0)
        yield _sse_passthrough_line({"error": {"message": error_msg, "type": "server_error"}}).decode("utf-8")
        yield "data: [DONE]\n\n"
        return

    if not emitted:
        _record(api_key_info, account, model=model, stream=True, finish_reason="empty_response",
                status_code=last_status, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg="no SSE chunks received", t0=t0)
        return
    _record(api_key_info, account, model=model, stream=True, finish_reason=final_finish,
            status_code=last_status, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, t0=t0, usage_payload=usage_payload)


# ────────────────────────── entry point ───────────────────────────


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    """Provider entry: router calls us with model already aliased to upstream id."""
    # Refresh model cache opportunistically (idempotent, cheap).
    try:
        await refresh_model_ids()
    except Exception:
        pass

    account = await _pick_account()
    if not account:
        return "error", (503, {
            "error": {
                "message": "No active GMI account configured (set CB_GMI_API_KEY or import via admin UI)",
                "type": "channel_unavailable",
                "code": "channel_unavailable",
            }
        })

    inner = str(payload.get("model") or DEFAULT_MODEL)
    body = dict(payload)
    body["model"] = inner
    wants_stream = bool(body.get("stream"))

    if wants_stream:
        return "stream", _stream_chat(account, body, api_key_info, inner)
    return await _run_non_stream(account, body, api_key_info, inner)


# ────────────────────────── self-test (manual) ────────────────────


async def test_chat(account: dict, model: str = "auto", prompt: str = "ping") -> dict:
    """Admin-UI 'Test' button entry."""
    inner = translate_model(model)
    payload = {
        "model": inner,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32,
        "stream": False,
    }
    try:
        client = _get_client()
        r = await client.post(
            f"{_base_url(account)}{EP_CHAT}",
            headers=_auth_headers(account, stream=False),
            json=payload,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "message": str(exc), "status_code": 0}
    snippet = r.text[:400]
    return {
        "ok": r.status_code < 400,
        "status_code": r.status_code,
        "model": inner,
        "snippet": snippet,
    }