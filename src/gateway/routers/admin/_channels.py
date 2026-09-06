"""Admin endpoints: channel enablement/order + per-channel model lists.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from accounts import auth_manager
from accounts import control_plane
import providers
from providers import trae_shared
from upstream import compaction
from storage import database as db
from providers import custom_channels
from gateway.deps import _check_admin, _read_json_object

logger = logging.getLogger("buddy2api.admin")

router = APIRouter()


# ============================================================
# Channels & models
# ============================================================

@router.get("/admin/channels")
async def admin_channels(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    env_set = bool((os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip())
    in_container = auth_manager._running_in_container()
    enabled_now = set(providers.enabled_provider_ids())
    ordered_known = providers.get_channel_order() + [
        c for c in providers.known_channel_ids() if c not in providers.get_channel_order()
    ]
    custom_ids = set(custom_channels.reserved_ids()) - set(providers.protocol.KNOWN_CHANNEL_IDS)
    items = []
    for channel in ordered_known:
        provider = providers.get_provider(channel)
        is_custom = channel in custom_ids
        # kind: "builtin" for the protocol-known package providers (workbuddy/qclaw/
        # qwenwork/traework/traesolo); "apikey" for any channel whose provider is the
        # single-API-key OpenAI-compat base (gmi/bailian built-ins + every custom
        # definition). Frontend keys its import-form off this.
        kind = "builtin"
        if is_custom:
            kind = "apikey"
        elif channel in {"gmi", "bailian"}:
            kind = "apikey"
        items.append({
            "id": channel,
            "display_name": getattr(provider, "display_name", channel) if provider else channel,
            "enabled": channel in enabled_now,
            "loaded": provider is not None,
            "checkin_supported": bool(getattr(provider, "checkin_supported", False)) if provider else False,
            "env_locked": env_set,
            "host_auth_limited": bool(in_container and channel in {"qclaw", "qwenwork"}),
            "kind": kind,
            "custom": is_custom,
        })
    return {
        "channels": items,
        "known": list(ordered_known),
        "enabled": providers.enabled_provider_ids(),
        "env_locked": env_set,
    }


@router.put("/admin/channels")
async def admin_update_channels(
    request: Request, authorization: str | None = Header(default=None)
):
    """Update the runtime-enabled channel list and (optionally) the display order.

    Request body: {"enabled": ["workbuddy", "gmi", ...], "order": ["workbuddy", "gmi", ...]}
    `enabled` is the set the admin wants enabled. `order` (optional) is the
    display order used across every page; if omitted, the existing order is
    preserved and new additions are appended. `workbuddy` is forced to the top
    regardless of either input.
    Rejected with 409 if CB_GATEWAY_PROVIDERS is set in the environment.
    """
    _check_admin(authorization)
    if providers.env_locked():
        raise HTTPException(
            status_code=409,
            detail="CB_GATEWAY_PROVIDERS is set in the environment; channel toggles are read-only",
        )
    data = await _read_json_object(request)
    ids = data.get("enabled")
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise HTTPException(status_code=400, detail="enabled must be a list of channel id strings")
    order = data.get("order")
    if order is not None and (
        not isinstance(order, list) or not all(isinstance(x, str) for x in order)
    ):
        raise HTTPException(status_code=400, detail="order must be a list of channel id strings")
    try:
        resolved_enabled, resolved_order = providers.set_enabled_channels(ids, order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"enabled": resolved_enabled, "order": resolved_order, "status": "ok"}


@router.get("/admin/channels/{channel}/models")
async def admin_channel_models(
    channel: str, authorization: str | None = Header(default=None)
):
    """View a channel's effective model list / aliases (built-in default + custom flags)."""
    _check_admin(authorization)
    try:
        return await run_in_threadpool(control_plane.channel_model_view, channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/admin/channels/{channel}/models")
async def admin_set_channel_models(
    channel: str, request: Request, authorization: str | None = Header(default=None)
):
    """Set or reset a channel's model list / aliases / credit rate / per-model reasoning tier.

    Body: {"models": [...]|null, "aliases": {...}|null, "credit_rate": <num>|null,
            "reasoning": {"model_id": "low", "__default__": ""}|null}
    Pass null to reset that field to the built-in default. At least one key
    must be present.
    """
    _check_admin(authorization)
    data = await _read_json_object(request)
    set_models = "models" in data
    set_aliases = "aliases" in data
    set_rate = "credit_rate" in data
    set_reasoning = "reasoning" in data
    try:
        result = await run_in_threadpool(
            control_plane.set_channel_models,
            channel,
            models=data.get("models") if set_models else None,
            aliases=data.get("aliases") if set_aliases else None,
            credit_rate=data.get("credit_rate") if set_rate else None,
            reasoning=data.get("reasoning") if set_reasoning else None,
            set_models=set_models,
            set_aliases=set_aliases,
            set_rate=set_rate,
            set_reasoning=set_reasoning,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 契约:保存后回传生效模型 id 列表,前端据此本地回写,免去整表刷新
    try:
        view = await run_in_threadpool(control_plane.channel_model_view, channel)
        result = dict(result or {})
        result["models"] = view.get("models") or []
    except Exception:
        logger.debug("channel_model_view failed after set", exc_info=True)
    return result


@router.post("/admin/channels/{channel}/models/refresh")
async def admin_refresh_channel_models(
    channel: str, authorization: str | None = Header(default=None)
):
    """Force-refresh a channel's official model list. Only traesolo supports
    dynamic fetching; other channels return a static whitelist."""
    _check_admin(authorization)
    try:
        return await control_plane.refresh_channel_models(channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

# ============================================================
# 通道健康观测面(35号方案 §2.5,只读聚合,零新状态机)
# ============================================================

@router.get("/admin/channel-health")
async def admin_channel_health(authorization: str | None = Header(default=None)):
    """每通道故障状态一屏可见:冷却/负缓存/11128 武装/近 1h 5xx/首字 P95。"""
    _check_admin(authorization)
    accounts = await run_in_threadpool(db.list_accounts_summary)
    provider_by_aid = {
        a["id"]: (a.get("provider") or "workbuddy") for a in accounts
    }
    cooling = await run_in_threadpool(auth_manager.cooling_accounts)
    backoff = trae_shared.refresh_backoff_view()
    armed = compaction.armed_channels()
    errors = await run_in_threadpool(db.count_errors_by_provider)
    p95 = await run_in_threadpool(db.stream_p95_by_provider)

    channels: dict = {}

    def bucket(provider: str) -> dict:
        return channels.setdefault(provider, {
            "cooling_accounts": [], "refresh_backoff": [],
            "armed_11128": False, "errors_5xx_1h": 0, "first_token_p95_ms": None,
        })

    for row in cooling:
        provider = provider_by_aid.get(row["account_id"])
        if provider:
            bucket(provider)["cooling_accounts"].append(row)
    for row in backoff:
        bucket(row["channel"])["refresh_backoff"].append(row)
    for provider in armed:
        bucket(provider)["armed_11128"] = True
    for provider, count in errors.items():
        bucket(provider)["errors_5xx_1h"] = count
    for provider, value in p95.items():
        bucket(provider)["first_token_p95_ms"] = value

    return {"channels": channels, "generated_at": int(time.time())}
