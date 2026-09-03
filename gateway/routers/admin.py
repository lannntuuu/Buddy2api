"""Admin endpoints: channels, accounts, api-keys, logs, settings, codex, etc."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from storage import database as db
import providers
from accounts import auth_manager
from accounts import control_plane
from gateway import router
from gateway.deps import (
    _atomic_write,
    _check_admin,
    _check_usage_rate_limit,
    _qclaw_provider_helper,
    _read_json,
    _read_json_object,
    _solo_callback_base,
    _traesolo_provider_helper,
    _usage_date_bounds,
    _validate_key_channel,
    ADMIN_TOKEN,
)
from providers.model_config import unified_models
from providers.protocol import KNOWN_CHANNEL_SET
from providers.qclaw.store import default_guid, upsert_account as upsert_qclaw_account
from upstream import proxy

router_obj = APIRouter()


# ============================================================
# Channels & models
# ============================================================

@router_obj.get("/admin/channels")
async def admin_channels(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    env_set = bool((os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip())
    in_container = auth_manager._running_in_container()
    enabled_now = set(providers.enabled_provider_ids())
    ordered_known = providers.get_channel_order() + [
        c for c in KNOWN_CHANNEL_SET if c not in providers.get_channel_order()
    ]
    items = []
    for channel in ordered_known:
        provider = providers.get_provider(channel)
        items.append({
            "id": channel,
            "display_name": getattr(provider, "display_name", channel) if provider else channel,
            "enabled": channel in enabled_now,
            "loaded": provider is not None,
            "checkin_supported": bool(getattr(provider, "checkin_supported", False)) if provider else False,
            "env_locked": env_set,
            "host_auth_limited": bool(in_container and channel in {"qclaw", "qwenwork"}),
        })
    return {
        "channels": items,
        "known": list(ordered_known),
        "enabled": providers.enabled_provider_ids(),
        "env_locked": env_set,
    }


@router_obj.put("/admin/channels")
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


@router_obj.get("/admin/channels/{channel}/models")
async def admin_channel_models(
    channel: str, authorization: str | None = Header(default=None)
):
    """View a channel's effective model list / aliases (built-in default + custom flags)."""
    _check_admin(authorization)
    try:
        return await run_in_threadpool(control_plane.channel_model_view, channel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router_obj.put("/admin/channels/{channel}/models")
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
        return await run_in_threadpool(
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


@router_obj.post("/admin/channels/{channel}/models/refresh")
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


@router_obj.post("/admin/traework/sync-usage")
async def admin_traework_sync_usage(
    authorization: str | None = Header(default=None),
):
    """Manually trigger the TraeWork official consumption-truth sync
    (the background loop also runs once per hour)."""
    _check_admin(authorization)
    try:
        result = await control_plane.sync_traework_usage(days=90)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:240]) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "sync failed"))
    return result


@router_obj.get("/admin/credit-overview")
async def admin_credit_overview(authorization: str | None = Header(default=None)):
    """Account-level historical total cost estimate (current used + expired credits, assuming expired are spent)."""
    _check_admin(authorization)
    try:
        result = await control_plane.account_credit_overview()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:240]) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "failed"))
    return result


@router_obj.get("/admin/traework/usage")
async def admin_traework_usage(authorization: str | None = Header(default=None)):
    """View synced TraeWork official consumption truth (per day)."""
    _check_admin(authorization)
    by_day = db.get_traework_daily_credit(days=90)
    return {
        "by_day": by_day,
        "total_credits": db.get_traework_total_credit(),
        "last_sync_at": db.latest_traework_sync_at(),
    }


@router_obj.get("/admin/unified-models")
async def admin_get_unified_models(authorization: str | None = Header(default=None)):
    """Unified model (cross-platform translation layer) current config."""
    _check_admin(authorization)
    return await run_in_threadpool(control_plane.unified_model_view)


@router_obj.put("/admin/unified-models")
async def admin_set_unified_models(
    request: Request, authorization: str | None = Header(default=None)
):
    """Replace the unified model table wholesale:
    {"models": [{"name": "...", "mappings": {"traework": "..."}}]}.
    Pass [] to clear. Unified names are a translation layer only; each
    channel's whitelist remains the final gate."""
    _check_admin(authorization)
    data = await _read_json_object(request)
    try:
        return await run_in_threadpool(
            control_plane.set_unified_models, data.get("models", [])
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router_obj.get("/admin/stats")
async def admin_stats(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    stats = db.get_stats()
    stats["compaction"] = proxy.compaction_stats()
    return stats


@router_obj.get("/admin/provider-model-usage")
async def admin_provider_model_usage(
    provider: str | None = None,
    model: str | None = None,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Token usage aggregated by platform × model × calendar day (admin endpoint).

    - `provider`/`model` optional filters; `model` must be in that platform's
      active whitelist.
    - `days` and `start_date`/`end_date` are translated to a unix-second
      window before the same aggregation query runs.
    """
    _check_admin(authorization)
    await _check_usage_rate_limit()

    provider_name = str(provider or "").strip()
    model_name = str(model or "").strip()
    if provider_name:
        if not providers.is_known_channel(provider_name):
            raise HTTPException(status_code=400, detail=f"Unknown provider '{provider_name}'")
        provider_obj = providers.get_provider(provider_name)
        if provider_obj is None:
            raise HTTPException(status_code=400, detail=f"Provider '{provider_name}' is not enabled")
        if model_name and not provider_obj.accepts_model(model_name):
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model_name}' is not in the whitelist of provider '{provider_name}'",
            )
    elif model_name:
        raise HTTPException(status_code=400, detail="model requires a provider filter")

    start_ts, end_ts = _usage_date_bounds(days, start_date, end_date)
    return await run_in_threadpool(
        db.get_provider_model_usage,
        {
            "provider": provider_name,
            "model": model_name,
            "start": start_ts,
            "end": end_ts,
        },
    )


@router_obj.get("/admin/credit-summary")
async def admin_credit_summary(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await control_plane.credit_summary(force=bool(force))


# ============================================================
# Accounts
# ============================================================

@router_obj.get("/admin/accounts")
async def admin_list_accounts(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    accounts = db.list_accounts()
    result = []
    for a in accounts:
        s = auth_manager.get_account_status(a)
        s["phone"] = a.get("phone", "")
        s["account_type"] = a.get("account_type", "")
        s["enterprise_id"] = a.get("enterprise_id", "")
        s["domain"] = a.get("domain", "")
        s["weight"] = int(a.get("weight") or 1)
        s["priority"] = int(a.get("priority") or 0)
        s["credit_limit"] = float(a.get("credit_limit") or 0)
        s["provider"] = a.get("provider") or "workbuddy"
        if a.get("credential_error"):
            s["credential_error"] = a["credential_error"]
        result.append(s)
    return result


@router_obj.get("/admin/accounts/discover")
async def admin_discover_accounts(
    auth_dir: str | None = None,
    channel: str | None = None,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    try:
        return await run_in_threadpool(control_plane.discover, channel, auth_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router_obj.post("/admin/accounts/import")
async def admin_import_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    channel = str(data.get("channel") or "workbuddy").strip() or "workbuddy"
    token = str(data.get("preview_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="preview_token is required")
    paths = data.get("paths")
    if paths is not None and (
        not isinstance(paths, list) or not all(isinstance(item, str) for item in paths)
    ):
        raise HTTPException(status_code=400, detail="paths must be an array of strings")
    try:
        result = await run_in_threadpool(
            control_plane.import_channel, channel, token, paths, data.get("auth_dir")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(result, dict) and (result.get("imported") or result.get("updated")):
        control_plane.invalidate_credit_summary_cache()
    return result


@router_obj.post("/admin/accounts/scan")
async def admin_scan_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    auth_dir = data.get("auth_dir") if isinstance(data, dict) else None
    result = await run_in_threadpool(auth_manager.auto_scan_and_import, auth_dir)
    if isinstance(result, dict) and (result.get("imported") or result.get("updated")):
        control_plane.invalidate_credit_summary_cache()
    return result


@router_obj.post("/admin/accounts")
async def admin_add_account(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    provider_id = str(data.get("provider") or data.get("channel") or "").strip()
    if provider_id and provider_id != "workbuddy":
        provider = providers.get_provider(provider_id)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{provider_id}' is not enabled")
        parse_credentials = getattr(provider, "parse_credentials", None)
        if parse_credentials is None:
            raise HTTPException(status_code=400, detail=f"Channel '{provider_id}' does not support pasted credentials")
        try:
            parsed = parse_credentials(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        upsert = getattr(provider, "upsert_account", None)
        if upsert is None and provider_id == "qclaw":
            result = upsert_qclaw_account(parsed)
        elif upsert is None:
            aid = db.add_account({**parsed, "provider": provider_id})
            result = {"id": aid, "updated": False}
        else:
            result = upsert(parsed)
        return {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": provider_id}
    # Paste raw auth JSON directly
    auth_data = data.get("auth", {})
    account_data = data.get("account", {})
    if not isinstance(auth_data, dict) or not isinstance(account_data, dict):
        raise HTTPException(status_code=400, detail="auth and account must be JSON objects")
    parsed = {
        "name": account_data.get("nickname", data.get("name", "")),
        "uid": account_data.get("uid", ""),
        "nickname": account_data.get("nickname", ""),
        "phone": account_data.get("phoneNumber", ""),
        "account_type": account_data.get("type", "personal"),
        "access_token": auth_data.get("accessToken", ""),
        "refresh_token": auth_data.get("refreshToken", ""),
        "expires_at": auth_data.get("expiresAt", 0),
        "refresh_expires_at": auth_data.get("refreshExpiresAt", 0),
        "domain": auth_data.get("domain", "www.codebuddy.cn"),
        "enterprise_id": account_data.get("enterpriseId", ""),
        "session_state": auth_data.get("sessionState", ""),
    }
    if not parsed["access_token"]:
        raise HTTPException(status_code=400, detail="No accessToken found in auth data")
    aid = db.add_account(parsed)
    return {"id": aid, "status": "ok"}


@router_obj.post("/admin/qclaw/import-path")
async def admin_qclaw_import_path(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    provider = _qclaw_provider_helper()
    data = await _read_json_object(request)
    path = str(data.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    try:
        parsed = provider.import_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = upsert_qclaw_account(parsed)
    return {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "qclaw"}


@router_obj.post("/admin/qclaw/login/start")
async def admin_qclaw_login_start(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    provider = _qclaw_provider_helper()
    data = await _read_json_object(request, allow_empty=True)
    guid = str((data or {}).get("guid") or default_guid() or "").strip()
    if not guid:
        raise HTTPException(status_code=400, detail="guid is required (or login to official QClaw once)")
    return await provider.start_login(guid)


@router_obj.post("/admin/qclaw/login/complete")
async def admin_qclaw_login_complete(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    provider = _qclaw_provider_helper()
    data = await _read_json_object(request)
    from providers.qclaw.oauth import parse_callback

    guid = str(data.get("guid") or default_guid() or "").strip()
    callback = str(data.get("callback") or data.get("code") or "").strip()
    if not guid or not callback:
        raise HTTPException(status_code=400, detail="guid and callback/code are required")
    parsed_cb = parse_callback(callback)
    state = str(data.get("state") or parsed_cb.get("state") or "")
    try:
        parsed = await provider.complete_login(guid, parsed_cb["code"], state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = upsert_qclaw_account(parsed)
    return {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "qclaw"}


@router_obj.put("/admin/accounts/{aid}")
async def admin_update_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed = {"name", "status", "weight", "priority", "credit_limit", "credit_baseline"}
    update_data = {k: data[k] for k in allowed if k in data}
    if "status" in update_data and update_data["status"] not in {"active", "inactive", "expired"}:
        raise HTTPException(status_code=400, detail="Invalid account status")
    if "credit_limit" in update_data and "credit_baseline" not in update_data:
        account = db.get_account(aid)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        update_data["credit_baseline"] = float(account.get("total_credits") or 0)
    for field in ("weight", "priority", "credit_limit", "credit_baseline"):
        if field in update_data:
            try:
                update_data[field] = float(update_data[field]) if field.startswith("credit_") else int(update_data[field])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{field} must be numeric")
    if "weight" in update_data and update_data["weight"] < 1:
        raise HTTPException(status_code=400, detail="weight must be at least 1")
    db.update_account(aid, update_data)
    return {"status": "ok"}


@router_obj.delete("/admin/accounts/{aid}")
async def admin_delete_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_account(aid)
    control_plane.invalidate_credit_summary_cache()
    return {"status": "ok"}


@router_obj.post("/admin/accounts/{aid}/refresh")
async def admin_refresh_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        refresh = getattr(provider, "refresh", None)
        if refresh is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' does not support refresh")
        try:
            await refresh(account)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:240]) from exc
        return {"status": "ok"}
    ok = await auth_manager.refresh_token(account)
    return {"status": "ok" if ok else "failed"}


@router_obj.post("/admin/accounts/{aid}/test")
async def admin_test_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    data = await _read_json_object(request, allow_empty=True)
    model = data.get("model") if isinstance(data, dict) else None
    prompt = data.get("prompt") if isinstance(data, dict) else None
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        test = getattr(provider, "test_chat", None)
        if test is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' does not support account test")
        default_prompt = "请回复：pong" if channel == "traework" else "ping"
        return await test(account, model or "auto", prompt or default_prompt)
    return await proxy.test_account_chat(account, model or "auto", prompt or "ping")


@router_obj.get("/admin/accounts/{aid}/resources")
async def admin_account_resources(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        fetch_quota = getattr(provider, "fetch_quota", None)
        if fetch_quota is None:
            return {
                "ok": True,
                "unsupported": True,
                "account_id": aid,
                "unit": "unknown",
                "remaining": None,
                "message": "quota API not available",
            }
        snapshot = await fetch_quota(account)
        unit = getattr(snapshot, "unit", "credit") or "credit"
        remaining = getattr(snapshot, "remaining", None)
        unsupported = bool(getattr(snapshot, "unsupported", False)) or unit != "credit"
        credit_remaining = remaining if unit == "credit" and not unsupported else None
        return {
            "ok": bool(getattr(snapshot, "ok", False)),
            "account_id": aid,
            "unit": "credit",
            "remaining": credit_remaining,
            "total_dosage": credit_remaining,
            "unsupported": unsupported or credit_remaining is None,
            "message": getattr(snapshot, "message", "") or ("no credit balance" if credit_remaining is None else ""),
            "packages": [],
        }
    return await auth_manager.fetch_account_resources(account, force=bool(force))


@router_obj.get("/admin/accounts/{aid}/checkin")
async def admin_checkin_status(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        fetch_checkin = getattr(provider, "fetch_checkin", None) if provider else None
        if fetch_checkin is not None:
            return await fetch_checkin(account, force=bool(force))
    return await auth_manager.fetch_checkin_status(account, force=bool(force))


@router_obj.get("/admin/accounts/checkin-status-all")
async def admin_checkin_status_all(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await control_plane.checkin_status_all(force=bool(force))


@router_obj.post("/admin/accounts/{aid}/checkin")
async def admin_claim_checkin(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        claim_checkin = getattr(provider, "claim_checkin", None) if provider else None
        if claim_checkin is not None:
            result = await claim_checkin(account)
            if result.get("ok") or result.get("claimed") or result.get("already_claimed"):
                control_plane.invalidate_credit_summary_cache()
            return result
    result = await auth_manager.claim_daily_checkin(account)
    if result.get("ok"):
        result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
    if result.get("ok") or result.get("claimed") or result.get("already_claimed"):
        control_plane.invalidate_credit_summary_cache()
    return result


@router_obj.post("/admin/accounts/checkin-all")
async def admin_claim_all_checkin(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    channels = data.get("channels") if isinstance(data, dict) else None
    if channels is not None and (
        not isinstance(channels, list) or not all(isinstance(item, str) for item in channels)
    ):
        raise HTTPException(status_code=400, detail="channels must be an array of strings")
    return await control_plane.checkin_all(channels)


# ============================================================
# Trae SOLO login flow
# ============================================================

@router_obj.get("/authorize", include_in_schema=False)
async def solo_authorize_callback(request: Request):
    """TRAE login callback landing (browser 302, no admin auth required).

    Captures the query string, runs ExchangeToken + GetUserInfo, persists the
    account, and renders a result page. Only pending sessions started via
    /admin/traesolo/login/start are accepted (`loginTraceID` is derived from
    a random machine/device, so it cannot be forged) — this stops a third
    party from pushing arbitrary accounts into the gateway through this
    endpoint. Remote deployments fall back to the manual flow: the user
    pastes the full callback URL into POST /admin/traesolo/login/complete.
    """
    provider = _traesolo_provider_helper()
    result = await provider.complete_login_callback(str(request.url), require_pending=True)
    ok = bool(result.get("ok"))
    title = "登录成功" if ok else "登录失败"
    if ok:
        detail = f"账号 {result.get('uid') or ''}（{result.get('nickname') or ''}）已添加，可关闭此窗口返回管理页。"
    else:
        detail = str(result.get("error") or "登录失败")
    import html as _html
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><title>"
        f"{_html.escape(title)}</title></head>"
        "<body style='font-family:system-ui,sans-serif;display:grid;place-items:center;"
        "height:100vh;margin:0;background:#0b1020;color:#e5e7eb'>"
        "<div style='max-width:560px;padding:32px'>"
        f"<h2 style='margin-top:0'>{_html.escape(title)}</h2><p>{_html.escape(detail)}</p></div>"
        "</body></html>"
    )
    return HTMLResponse(html, status_code=200 if ok else 400)


@router_obj.post("/admin/traesolo/login/start")
async def admin_traesolo_login_start(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Start the SOLO web login: returns login_url (open in browser) + pending_id."""
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    provider = _traesolo_provider_helper()
    base = _solo_callback_base(request, str((data or {}).get("callback_base") or ""))
    try:
        return provider.start_login(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router_obj.get("/admin/traesolo/login/result")
async def admin_traesolo_login_result(
    pending_id: str,
    authorization: str | None = Header(default=None),
):
    """Poll the pending login result: pending / success / failed / canceled."""
    _check_admin(authorization)
    provider = _traesolo_provider_helper()
    result = provider.login_result(pending_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="pending login not found (expired or invalid)")
    return result


@router_obj.post("/admin/traesolo/login/cancel")
async def admin_traesolo_login_cancel(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    pending_id = str((data or {}).get("pending_id") or request.query_params.get("pending_id") or "")
    if not pending_id:
        raise HTTPException(status_code=400, detail="pending_id is required")
    provider = _traesolo_provider_helper()
    return provider.cancel_login(pending_id)


@router_obj.post("/admin/traesolo/login/complete")
async def admin_traesolo_login_complete(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Manual close: paste the full callback URL (from the browser address bar)
    to complete token exchange and persistence."""
    _check_admin(authorization)
    data = await _read_json_object(request)
    callback = str(data.get("callback") or data.get("callback_url") or "").strip()
    if not callback:
        raise HTTPException(status_code=400, detail="callback is required")
    provider = _traesolo_provider_helper()
    try:
        return await provider.complete_login_callback(callback)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ============================================================
# API keys
# ============================================================

@router_obj.get("/admin/api-keys")
async def admin_list_keys(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.list_api_keys(include_secret=True)


@router_obj.post("/admin/api-keys")
async def admin_create_key(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    name = str(data.get("name", "")).strip()[:120]
    allowed = data.get("allowed_models")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(model, str) for model in allowed)
    ):
        raise HTTPException(status_code=400, detail="allowed_models must be an array of strings")
    try:
        daily_limit = max(0, int(data.get("daily_limit") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="daily_limit must be a non-negative integer")
    client_type = data.get("client_type", "custom")
    if client_type not in {"custom", "codex"}:
        raise HTTPException(status_code=400, detail="Invalid client_type")
    if "default_channel" not in data or data.get("default_channel") in (None, ""):
        raise HTTPException(status_code=400, detail="default_channel is required")
    default_channel = _validate_key_channel(data.get("default_channel"))
    # Generate a key with the `sk-` prefix
    key = f"sk-cb-{secrets.token_urlsafe(32)}"
    kid = db.add_api_key(
        key, name, allowed, daily_limit, client_type, default_channel=default_channel
    )
    return {"id": kid, "key": key, "status": "ok", "default_channel": default_channel}


@router_obj.put("/admin/api-keys/{kid}")
async def admin_update_key(
    kid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    if "daily_limit" in data:
        try:
            data["daily_limit"] = max(0, int(data["daily_limit"] or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="daily_limit must be a non-negative integer")
    if "status" in data and data["status"] not in {"active", "inactive"}:
        raise HTTPException(status_code=400, detail="Invalid API key status")
    if "client_type" in data and data["client_type"] not in {"custom", "codex"}:
        raise HTTPException(status_code=400, detail="Invalid client_type")
    if "default_channel" in data:
        data["default_channel"] = _validate_key_channel(data.get("default_channel"))
    if "allowed_models" in data and (
        data["allowed_models"] is not None
        and (not isinstance(data["allowed_models"], list) or not all(isinstance(model, str) for model in data["allowed_models"]))
    ):
        raise HTTPException(status_code=400, detail="allowed_models must be an array of strings")
    db.update_api_key(kid, data)
    return {"status": "ok"}


@router_obj.delete("/admin/api-keys/{kid}")
async def admin_delete_key(
    kid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_api_key(kid)
    return {"status": "ok"}


# ============================================================
# Logs
# ============================================================

@router_obj.get("/admin/logs")
async def admin_logs(
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return db.list_logs(max(1, min(500, limit)), max(0, offset))


@router_obj.get("/admin/logs/search")
async def admin_logs_search(
    q: str | None = None,
    status: str = "all",
    account_id: str | None = None,
    api_key_id: str | None = None,
    model: str | None = None,
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    if account_id not in (None, "", "all") and not str(account_id).isdigit():
        raise HTTPException(status_code=400, detail="account_id must be numeric")
    if api_key_id not in (None, "", "all") and not str(api_key_id).isdigit():
        raise HTTPException(status_code=400, detail="api_key_id must be numeric")
    return db.search_logs({
        "q": q or "",
        "status": status,
        "account_id": account_id,
        "api_key_id": api_key_id,
        "model": model or "",
        "limit": limit,
        "offset": offset,
    })


# ============================================================
# Settings
# ============================================================

@router_obj.get("/admin/settings")
async def admin_get_settings(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_all_settings()


@router_obj.put("/admin/settings")
async def admin_update_settings(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed_settings = {"backend_url", "default_domain", "timeout", "channel_hosts"}
    unknown = set(data) - allowed_settings
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported settings: {', '.join(sorted(unknown))}")
    if "timeout" in data:
        try:
            data["timeout"] = max(5, min(600, int(data["timeout"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timeout must be an integer between 5 and 600")
    if "backend_url" in data:
        backend_url = str(data["backend_url"]).strip().rstrip("/")
        if not backend_url.startswith("https://"):
            raise HTTPException(status_code=400, detail="backend_url must use HTTPS")
        data["backend_url"] = backend_url
    if "channel_hosts" in data:
        ch = data["channel_hosts"]
        if not isinstance(ch, dict):
            raise HTTPException(status_code=400, detail="channel_hosts must be an object")
        from providers.host_override import CHANNEL_HOST_FIELDS
        for cid, fields in ch.items():
            if cid not in CHANNEL_HOST_FIELDS:
                raise HTTPException(status_code=400, detail=f"Unsupported channel: {cid}")
            if not isinstance(fields, dict):
                raise HTTPException(status_code=400, detail=f"channel_hosts[{cid}] must be an object")
            unknown_fields = set(fields) - set(CHANNEL_HOST_FIELDS[cid])
            if unknown_fields:
                raise HTTPException(status_code=400, detail=f"Unsupported host fields: {', '.join(sorted(unknown_fields))}")
            for f, v in fields.items():
                if not v:
                    continue
                val = str(v).strip().rstrip("/")
                if not val.startswith("https://"):
                    raise HTTPException(status_code=400, detail=f"{cid}.{f} must use HTTPS")
                fields[f] = val
    for k, v in data.items():
        db.set_setting(k, v)
    return {"status": "ok"}


# ============================================================
# Legacy models (settings row)
# ============================================================

@router_obj.get("/admin/models")
async def admin_get_models(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_setting("models", proxy.DEFAULT_MODELS)


@router_obj.put("/admin/models")
async def admin_update_models(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json(request)
    if not isinstance(data, list) or not all(
        isinstance(model, dict) and isinstance(model.get("id"), str) and model.get("id")
        for model in data
    ):
        raise HTTPException(status_code=400, detail="Models must be an array of objects with an id")
    db.set_setting("models", data)
    return {"status": "ok"}


# ============================================================
# Codex one-click setup
# ============================================================

@router_obj.post("/admin/codex/setup")
async def admin_codex_setup(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """One-click Codex setup: write config.toml and auth.json."""
    _check_admin(authorization)
    data = await _read_json_object(request)
    api_key = str(data.get("api_key", "")).strip()
    if not api_key.startswith("sk-cb-") or len(api_key) > 256:
        raise HTTPException(status_code=400, detail="api_key is required")

    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    results = {"backed_up": [], "written": [], "config_path": str(config_path), "auth_path": str(auth_path)}
    for p in [config_path, auth_path]:
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            _atomic_write(bak, p.read_bytes())
            results["backed_up"].append(str(bak))

    existing_config = ""
    if config_path.exists():
        existing_config = config_path.read_text(encoding="utf-8")

    new_lines = []
    in_skip_section = False
    skip_section_prefixes = ["[model_providers", "model ", "model=", "model_provider"]

    for line in existing_config.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in ["model =", "model=", "model_provider"]):
            continue
        if stripped.startswith("[model_providers"):
            in_skip_section = True
            continue
        if in_skip_section:
            if stripped.startswith("[") and not stripped.startswith("[model_providers"):
                in_skip_section = False
                new_lines.append(line)
            else:
                continue
        else:
            new_lines.append(line)

    codex_config = '''model = "auto"
model_provider = "buddy2api"

[model_providers.buddy2api]
name = "Buddy2api"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"

'''
    preserved = "\n".join(new_lines).strip()
    final_config = codex_config + ("\n" + preserved if preserved else "")
    _atomic_write(config_path, final_config)
    results["written"].append(str(config_path))

    import json as _json
    auth_content = _json.dumps({"OPENAI_API_KEY": api_key}, indent=2)
    _atomic_write(auth_path, auth_content)
    results["written"].append(str(auth_path))

    # Keep the key in this process's env; only auth.json holds the persistent
    # credential, and only with restricted permissions.
    os.environ["OPENAI_API_KEY"] = api_key

    results["status"] = "ok"
    results["message"] = "Codex 配置已写入。请完全关闭 Codex 后重新打开。"
    return results


@router_obj.get("/admin/codex/status")
async def admin_codex_status(authorization: str | None = Header(default=None)):
    """Check Codex configuration status."""
    _check_admin(authorization)
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    result = {
        "codex_dir_exists": codex_dir.exists(),
        "config_exists": config_path.exists(),
        "auth_exists": auth_path.exists(),
        "config_has_buddy2api": False,
        "config_wire_api": None,
        "config_model": None,
        "auth_has_key": False,
    }

    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        result["config_has_buddy2api"] = "buddy2api" in content
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("wire_api"):
                result["config_wire_api"] = s.split("=", 1)[1].strip().strip('"')
            elif s.startswith("model ") or s.startswith("model="):
                result["config_model"] = s.split("=", 1)[1].strip().strip('"')

    if auth_path.exists():
        try:
            import json as _json
            auth = _json.loads(auth_path.read_text(encoding="utf-8"))
            result["auth_has_key"] = bool(auth.get("OPENAI_API_KEY"))
        except Exception:
            pass

    return result


# ============================================================
# Model aliases
# ============================================================

@router_obj.get("/admin/aliases")
async def admin_get_aliases(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return proxy.get_all_aliases()


@router_obj.put("/admin/aliases")
async def admin_update_aliases(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        raise HTTPException(status_code=400, detail="Aliases must map string names to string model IDs")
    # Only store user-defined aliases (not built-in ones)
    user_aliases = {k: v for k, v in data.items() if k not in proxy._BUILTIN_ALIASES}
    db.set_setting("model_aliases", user_aliases)
    return {"status": "ok"}
