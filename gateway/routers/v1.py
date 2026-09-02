"""OpenAI-compatible V1 endpoints (chat completions, responses, models, health)."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from storage import database as db
import providers
from accounts import auth_manager
from accounts import control_plane
from gateway import router
from gateway.deps import (
    _check_client_auth,
    _check_model_access,
    _read_json_object,
    _release_client_quota,
    _reserve_client_quota,
    _stamp_client_info,
    ADMIN_TOKEN,
    ALLOW_NO_ADMIN_AUTH,
)
from gateway.version import VERSION
from providers.model_config import unified_models
from upstream import proxy, responses

router_obj = APIRouter()


# --- Health & meta (no auth, no DB) ---

@router_obj.get("/health")
async def health():
    accounts = db.list_accounts()
    keys = db.list_api_keys()
    channels = {}
    for channel in providers.enabled_provider_ids():
        rows = db.list_accounts(provider=channel)
        channels[channel] = {
            "accounts": len(rows),
            "active": sum(1 for account in rows if account.get("status") == "active"),
            "loaded": providers.get_provider(channel) is not None,
        }
    return {
        "status": "ok",
        "version": VERSION,
        "accounts": len(accounts),
        "active_accounts": sum(1 for account in accounts if account.get("status") == "active"),
        "active_keys": sum(1 for key in keys if key.get("status") == "active"),
        "channels": channels,
    }


@router_obj.get("/admin/meta")
async def meta():
    """Lightweight metadata for the admin console. No auth, no DB."""
    return {"title": "Buddy 2 API", "version": VERSION}


# --- V1 models ---

@router_obj.get("/v1/models")
async def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    await run_in_threadpool(
        lambda: _check_client_auth(authorization, x_api_key, consume_quota=False)
    )
    data = []
    workbuddy = providers.get_provider("workbuddy")
    wb_models = workbuddy.list_models() if workbuddy else db.get_setting("models", proxy.DEFAULT_MODELS)
    for item in wb_models:
        mid = item["id"] if isinstance(item, dict) else str(item)
        data.append({
            "id": mid,
            "object": "model",
            "created": 0,
            "owned_by": "buddy2api",
            "channel": "workbuddy",
        })
        data.append({
            "id": f"workbuddy/{mid}",
            "object": "model",
            "created": 0,
            "owned_by": "buddy2api",
            "channel": "workbuddy",
        })
    for channel in providers.enabled_provider_ids():
        if channel == "workbuddy":
            continue
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        for item in provider.list_models():
            mid = item["id"] if isinstance(item, dict) else str(item)
            data.append({
                "id": f"{channel}/{mid}",
                "object": "model",
                "created": 0,
                "owned_by": "buddy2api",
                "channel": channel,
            })
    # Unified model names (cross-platform translation layer). De-dupe against
    # already-listed (id, channel) pairs.
    seen = {(item["id"], item["channel"]) for item in data}
    for name, mapping in unified_models().items():
        for channel in mapping:
            if channel not in providers.enabled_provider_ids():
                continue
            if (name, channel) in seen:
                continue
            seen.add((name, channel))
            data.append({
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "buddy2api",
                "channel": channel,
            })
    return {"object": "list", "data": data}


# --- V1 chat & responses ---

@router_obj.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    api_key_info = await run_in_threadpool(
        lambda: _check_client_auth(authorization, x_api_key, consume_quota=False)
    )
    payload = await _read_json_object(request)

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages or not all(isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})
    if "model" in payload and not isinstance(payload["model"], str):
        raise HTTPException(status_code=400, detail={"error": {"message": "model must be a string", "type": "invalid_request_error"}})
    _stamp_client_info(request, api_key_info)
    # Codex client_type keys: auto-apply content sanitisation + tool filtering
    if api_key_info and api_key_info.get("client_type") == "codex":
        payload = responses.apply_codex_sanitize(payload)

    bound = router.bind_http(payload, api_key_info)
    _check_model_access(api_key_info, bound.original, bound.inner, bound.channel)
    await router.ensure_usable(bound.channel)
    await run_in_threadpool(_reserve_client_quota, api_key_info)

    try:
        result = await router.chat_after_bind(bound, payload, api_key_info)
    except BaseException:
        _release_client_quota(api_key_info)
        raise

    if result[0] == "error":
        # Dispatch sync failure (upstream 4xx/5xx): roll back this slot so
        # retries don't double-charge.
        _release_client_quota(api_key_info)
        status, detail = result[1]
        return JSONResponse(status_code=status, content=detail)
    elif result[0] == "json":
        return JSONResponse(content=result[1])
    elif result[0] == "stream":
        return StreamingResponse(
            result[1],
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


@router_obj.post("/v1/responses")
async def resp_responses(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """OpenAI Responses API compatibility endpoint (Codex wire_api="responses")."""
    api_key_info = await run_in_threadpool(
        lambda: _check_client_auth(authorization, x_api_key, consume_quota=False)
    )
    payload = await _read_json_object(request)
    if "input" not in payload:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "input is required", "type": "invalid_request_error"}},
        )
    if "model" in payload and not isinstance(payload["model"], str):
        raise HTTPException(status_code=400, detail={"error": {"message": "model must be a string", "type": "invalid_request_error"}})
    _stamp_client_info(request, api_key_info)
    bound = router.bind_http(payload, api_key_info)
    _check_model_access(api_key_info, bound.original, bound.inner, bound.channel)
    await router.ensure_usable(bound.channel)
    await run_in_threadpool(_reserve_client_quota, api_key_info)
    dispatch = router.dispatch_payload(payload, bound.inner)
    info = dict(api_key_info or {})
    info["_log_model"] = bound.original
    info["_bind_channel"] = bound.channel

    try:
        result = await responses.proxy_responses(dispatch, info, channel=bound.channel)
        result = await router.echo_original(result, bound.original)
    except Exception:
        _release_client_quota(api_key_info)
        import logging
        logger = logging.getLogger("buddy2api.server")
        logger.exception("[responses] bridge error")
        return JSONResponse(status_code=502, content={"error": {"message": "internal bridge error", "type": "server_error"}})

    if result[0] == "error":
        _release_client_quota(api_key_info)
        status, detail = result[1]
        return JSONResponse(status_code=status, content=detail)
    elif result[0] == "json":
        return JSONResponse(content=result[1])
    elif result[0] == "stream":
        return StreamingResponse(
            result[1],
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
