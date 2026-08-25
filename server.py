"""
server.py — Buddy 2 API 主服务

FastAPI 应用，包含：
  - /v1/chat/completions  代理端点（OpenAI 兼容）
  - /v1/models            模型列表
  - /health               健康检查
  - /admin/*              管理 API
  - /                     Web UI
"""

import argparse
import asyncio
import contextvars
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from starlette.concurrency import run_in_threadpool

import database as db
import auth_manager
import proxy
import responses
import providers
import router
from providers.protocol import KNOWN_CHANNEL_SET
from version import VERSION


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cors_origins() -> list[str]:
    value = os.environ.get(
        "CB_GATEWAY_CORS_ORIGINS",
        "http://127.0.0.1:8787,http://localhost:8787",
    )
    return [origin.strip() for origin in value.split(",") if origin.strip()]


app = FastAPI(title="Buddy 2 API", version=VERSION)
_CORS_ORIGINS = _cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials="*" not in _CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Api-Key"],
)

WEB_DIR = Path(__file__).parent / "web"


# ============================================================
# 中间件：管理 API 鉴权
# ============================================================

ADMIN_TOKEN: str = ""
ALLOW_NO_ADMIN_AUTH = False
ALLOW_UNAUTHENTICATED_API = _env_flag("CB_GATEWAY_ALLOW_UNAUTHENTICATED_API", False)
ADMIN_COOKIE_NAME = "cb_gw_admin_token"
MAX_BODY_BYTES = max(1024, _env_int("CB_GATEWAY_MAX_BODY_BYTES", 10 * 1024 * 1024))
_CURRENT_REQUEST: contextvars.ContextVar[Request | None] = contextvars.ContextVar("current_request", default=None)


def _atomic_write(path: Path, content: str | bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            payload = content.encode("utf-8") if isinstance(content, str) else content
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@app.middleware("http")
async def _request_context(request: Request, call_next):
    token = _CURRENT_REQUEST.set(request)
    try:
        return await call_next(request)
    finally:
        _CURRENT_REQUEST.reset(token)


def _check_admin(authorization: str | None):
    if ALLOW_NO_ADMIN_AUTH:
        return
    candidates = []
    if authorization:
        parts = authorization.split(" ", 1)
        candidates.append(parts[1] if len(parts) == 2 else parts[0])

    request = _CURRENT_REQUEST.get()
    if request:
        candidates.append(request.cookies.get(ADMIN_COOKIE_NAME, ""))

    if not any(t and secrets.compare_digest(t, ADMIN_TOKEN) for t in candidates):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_client_auth(
    authorization: str | None,
    x_api_key: str | None,
    *,
    consume_quota: bool = True,
):
    """Validate a client API key and atomically reserve its daily quota."""
    keys = db.list_api_keys()
    if not keys:
        if ALLOW_UNAUTHENTICATED_API:
            return None
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "No API keys configured", "type": "server_error"}},
        )

    token = ""
    if x_api_key:
        token = x_api_key
    elif authorization:
        parts = authorization.split(" ", 1)
        token = parts[1] if len(parts) == 2 else parts[0]

    if not token:
        raise HTTPException(status_code=401, detail={"error": {"message": "API key required", "type": "invalid_request_error"}})

    key_info = db.get_api_key_by_key(token)
    if not key_info:
        raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}})

    daily_limit = int(key_info.get("daily_limit") or 0)
    if consume_quota and not db.reserve_api_key_request(key_info["id"], daily_limit):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
        )
    return key_info


def _reserve_client_quota(key_info: dict | None):
    if not key_info:
        return
    daily_limit = int(key_info.get("daily_limit") or 0)
    if not db.reserve_api_key_request(key_info["id"], daily_limit):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
        )


def _validate_key_channel(channel: str) -> str:
    value = str(channel or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="default_channel is required")
    if value not in KNOWN_CHANNEL_SET:
        raise HTTPException(status_code=400, detail=f"Unknown channel '{value}'")
    if not providers.is_channel_enabled(value) or providers.get_provider(value) is None:
        raise HTTPException(status_code=400, detail=f"Channel '{value}' is not enabled")
    return value


def _check_model_access(api_key_info: dict | None, original: str, inner: str, channel: str):
    if not api_key_info or not api_key_info.get("allowed_models"):
        return
    provider = providers.get_provider(channel)
    translated = provider.translate_model(inner) if provider else inner
    allowed = set(api_key_info["allowed_models"])
    candidates = {original, inner, translated, f"{channel}/{inner}"}
    if allowed.isdisjoint(candidates):
        raise HTTPException(
            status_code=403,
            detail={"error": {"message": f"Model '{original}' not allowed for this API key", "type": "invalid_request_error"}},
        )


async def _read_json(request: Request, *, allow_empty: bool = False):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw and allow_empty:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")


async def _read_json_object(request: Request, *, allow_empty: bool = False) -> dict:
    data = await _read_json(request, allow_empty=allow_empty)
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data


async def _gather_limited(accounts: list[dict], operation, limit: int = 4) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(account: dict):
        async with semaphore:
            return await operation(account)

    return list(await asyncio.gather(*(run(account) for account in accounts)))


# ============================================================
# OpenAI 兼容端点
# ============================================================

@app.get("/health")
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


@app.get("/v1/models")
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
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
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
    # Codex 类型 Key：自动应用内容清洗 + 工具过滤
    if api_key_info and api_key_info.get("client_type") == "codex":
        payload = responses.apply_codex_sanitize(payload)

    bound = router.bind_http(payload, api_key_info)
    _check_model_access(api_key_info, bound.original, bound.inner, bound.channel)
    await router.ensure_usable(bound.channel)
    await run_in_threadpool(_reserve_client_quota, api_key_info)

    result = await router.chat_after_bind(bound, payload, api_key_info)

    if result[0] == "error":
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


@app.post("/v1/responses")
async def resp_responses(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """OpenAI Responses API 兼容端点（Codex wire_api="responses" 支持）。"""
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
    bound = router.bind_http(payload, api_key_info)
    _check_model_access(api_key_info, bound.original, bound.inner, bound.channel)
    await router.ensure_usable(bound.channel)
    await run_in_threadpool(_reserve_client_quota, api_key_info)
    dispatch = router.dispatch_payload(payload, bound.inner)
    info = dict(api_key_info or {})
    info["_log_model"] = bound.original
    info["_bind_channel"] = bound.channel

    try:
        result = await responses.proxy_responses(dispatch, info)
        result = await router.echo_original(result, bound.original)
    except Exception as e:
        import traceback
        sys.stderr.write(f"[responses] ERROR: {e}\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return JSONResponse(status_code=502, content={"error": {"message": f"internal bridge error: {e}", "type": "server_error"}})

    if result[0] == "error":
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


# ============================================================
# Admin API
# ============================================================

@app.get("/admin/channels")
async def admin_channels(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    env_set = bool((os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip())
    items = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        items.append({
            "id": channel,
            "display_name": getattr(provider, "display_name", channel),
            "enabled": True,
            "loaded": provider is not None,
            "checkin_supported": bool(getattr(provider, "checkin_supported", False)),
            "env_locked": env_set,
        })
    return {"channels": items, "known": list(KNOWN_CHANNEL_SET)}


@app.get("/admin/stats")
async def admin_stats(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_stats()


@app.get("/admin/credit-summary")
async def admin_credit_summary(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    accounts = db.list_accounts()
    active_accounts = [a for a in accounts if a.get("status") == "active"]
    resources = await _gather_limited(
        active_accounts,
        lambda account: auth_manager.fetch_account_resources(account, force=bool(force)),
    )

    ok_resources = [r for r in resources if r.get("ok")]
    total_balance = round(sum(float(r.get("total_dosage") or r.get("available_total") or 0) for r in ok_resources), 4)
    expiring_7d_total = round(sum(float(r.get("expiring_7d_total") or 0) for r in ok_resources), 4)
    expiring_30d_total = round(sum(float(r.get("expiring_30d_total") or 0) for r in ok_resources), 4)
    package_count = sum(int(r.get("package_count") or 0) for r in ok_resources)
    stale_count = sum(1 for r in resources if r.get("stale"))
    failed_count = sum(1 for r in resources if not r.get("ok"))
    now_ts = int(time.time())
    low_accounts = []
    expiring_accounts = []
    for r in resources:
        balance = float(r.get("total_dosage") or r.get("available_total") or 0)
        row = {
            "account_id": r.get("account_id"),
            "account_name": r.get("account_name"),
            "balance": round(balance, 4),
            "expiring_30d_total": round(float(r.get("expiring_30d_total") or 0), 4),
            "next_expire_time": r.get("next_expire_time") or "",
            "next_expire_days": r.get("next_expire_days"),
            "ok": bool(r.get("ok")),
            "stale": bool(r.get("stale")),
            "age_seconds": int(r.get("age_seconds") or 0),
        }
        if r.get("ok") and balance <= 300:
            low_accounts.append(row)
        if r.get("ok") and float(r.get("expiring_30d_total") or 0) > 0:
            expiring_accounts.append(row)

    low_accounts.sort(key=lambda x: (x["balance"], x["account_id"] or 0))
    expiring_accounts.sort(key=lambda x: (x["next_expire_days"] if x["next_expire_days"] is not None else 9999, -x["expiring_30d_total"]))
    return {
        "ok": failed_count == 0,
        "updated_at": now_ts,
        "active_accounts": len(active_accounts),
        "resource_accounts": len(resources),
        "ok_accounts": len(ok_resources),
        "failed_accounts": failed_count,
        "stale_accounts": stale_count,
        "total_balance": total_balance,
        "expiring_7d_total": expiring_7d_total,
        "expiring_30d_total": expiring_30d_total,
        "package_count": package_count,
        "low_accounts": low_accounts[:8],
        "expiring_accounts": expiring_accounts[:8],
        "accounts": resources,
    }


# --- Accounts ---

@app.get("/admin/accounts")
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
        result.append(s)
    return result


@app.get("/admin/accounts/discover")
async def admin_discover_accounts(
    auth_dir: str | None = None,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await run_in_threadpool(auth_manager.discover_auth_files, auth_dir)


@app.post("/admin/accounts/scan")
async def admin_scan_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    auth_dir = data.get("auth_dir") if isinstance(data, dict) else None
    return await run_in_threadpool(auth_manager.auto_scan_and_import, auth_dir)


@app.post("/admin/accounts")
async def admin_add_account(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    # 直接粘贴 auth JSON
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


@app.put("/admin/accounts/{aid}")
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


@app.delete("/admin/accounts/{aid}")
async def admin_delete_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_account(aid)
    return {"status": "ok"}


@app.post("/admin/accounts/{aid}/refresh")
async def admin_refresh_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    ok = await auth_manager.refresh_token(account)
    return {"status": "ok" if ok else "failed"}


@app.post("/admin/accounts/{aid}/test")
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
    return await proxy.test_account_chat(account, model or "auto", prompt or "ping")


@app.get("/admin/accounts/{aid}/resources")
async def admin_account_resources(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return await auth_manager.fetch_account_resources(account, force=bool(force))


@app.get("/admin/accounts/{aid}/checkin")
async def admin_checkin_status(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return await auth_manager.fetch_checkin_status(account, force=bool(force))


@app.get("/admin/accounts/checkin-status-all")
async def admin_checkin_status_all(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    accounts = [a for a in db.list_accounts() if a.get("status") == "active"]
    results = await _gather_limited(
        accounts,
        lambda account: auth_manager.fetch_checkin_status(account, force=bool(force)),
    )
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "claimed": sum(1 for r in results if r.get("claimed")),
        "already_claimed": sum(1 for r in results if r.get("already_claimed") or r.get("today_checked_in")),
        "available": sum(1 for r in results if r.get("ok") and not (r.get("already_claimed") or r.get("today_checked_in"))),
        "failed": sum(1 for r in results if not r.get("ok")),
        "stale": sum(1 for r in results if r.get("stale")),
        "results": results,
    }


@app.post("/admin/accounts/{aid}/checkin")
async def admin_claim_checkin(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    result = await auth_manager.claim_daily_checkin(account)
    if result.get("ok"):
        result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
    return result


@app.post("/admin/accounts/checkin-all")
async def admin_claim_all_checkin(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    accounts = [a for a in db.list_accounts() if a.get("status") == "active"]
    async def claim(account: dict):
        result = await auth_manager.claim_daily_checkin(account)
        if result.get("ok"):
            result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
        return result

    results = await _gather_limited(accounts, claim)
    return {
        "total": len(results),
        "claimed": sum(1 for r in results if r.get("claimed")),
        "already_claimed": sum(1 for r in results if r.get("already_claimed")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "credit": round(sum(float(r.get("credit") or 0) for r in results if r.get("claimed")), 4),
        "results": results,
    }


# --- API Keys ---

@app.get("/admin/api-keys")
async def admin_list_keys(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.list_api_keys(include_secret=True)


@app.post("/admin/api-keys")
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
    default_channel = _validate_key_channel(data.get("default_channel", "workbuddy"))
    # 生成 sk- 前缀的 key
    key = f"sk-cb-{secrets.token_urlsafe(32)}"
    kid = db.add_api_key(
        key, name, allowed, daily_limit, client_type, default_channel=default_channel
    )
    return {"id": kid, "key": key, "status": "ok", "default_channel": default_channel}


@app.put("/admin/api-keys/{kid}")
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


@app.delete("/admin/api-keys/{kid}")
async def admin_delete_key(
    kid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_api_key(kid)
    return {"status": "ok"}


# --- Logs ---

@app.get("/admin/logs")
async def admin_logs(
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return db.list_logs(max(1, min(500, limit)), max(0, offset))


@app.get("/admin/logs/search")
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


# --- Settings ---

@app.get("/admin/settings")
async def admin_get_settings(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_all_settings()


@app.put("/admin/settings")
async def admin_update_settings(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed_settings = {"backend_url", "default_domain", "timeout"}
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
    for k, v in data.items():
        db.set_setting(k, v)
    return {"status": "ok"}


# --- Models ---

@app.get("/admin/models")
async def admin_get_models(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_setting("models", proxy.DEFAULT_MODELS)


@app.put("/admin/models")
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


# --- Codex 一键配置 ---

@app.post("/admin/codex/setup")
async def admin_codex_setup(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """一键配置 Codex：写入 config.toml 和 auth.json。"""
    _check_admin(authorization)
    data = await _read_json_object(request)
    api_key = str(data.get("api_key", "")).strip()
    if not api_key.startswith("sk-cb-") or len(api_key) > 256:
        raise HTTPException(status_code=400, detail="api_key is required")

    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    # 备份现有文件
    results = {"backed_up": [], "written": [], "config_path": str(config_path), "auth_path": str(auth_path)}
    for p in [config_path, auth_path]:
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            _atomic_write(bak, p.read_bytes())
            results["backed_up"].append(str(bak))

    # 读取现有 config.toml，保留 marketplaces 等非冲突段
    existing_config = ""
    if config_path.exists():
        existing_config = config_path.read_text(encoding="utf-8")

    # 构建新的 config.toml
    # 保留 [marketplaces.*] 和 [projects.*] 和 [desktop] 段，替换/插入顶层和 provider
    new_lines = []
    in_skip_section = False
    skip_section_prefixes = ["[model_providers", "model ", "model=", "model_provider"]

    for line in existing_config.splitlines():
        stripped = line.strip()
        # 跳过旧的 model / model_provider / [model_providers.*] 行
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

    # 在文件开头插入 model 和 provider 配置
    codex_config = f'''model = "auto"
model_provider = "buddy2api"

[model_providers.buddy2api]
name = "Buddy2api"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"

'''
    # 保留原有内容（去掉了旧 model 配置）
    preserved = "\n".join(new_lines).strip()
    final_config = codex_config + ("\n" + preserved if preserved else "")
    _atomic_write(config_path, final_config)
    results["written"].append(str(config_path))

    # 写 auth.json
    import json as _json
    auth_content = _json.dumps({"OPENAI_API_KEY": api_key}, indent=2)
    _atomic_write(auth_path, auth_content)
    results["written"].append(str(auth_path))

    # 当前进程保留变量；持久化凭据只写入权限受限的 auth.json。
    os.environ["OPENAI_API_KEY"] = api_key

    results["status"] = "ok"
    results["message"] = "Codex 配置已写入。请完全关闭 Codex 后重新打开。"
    return results


@app.get("/admin/codex/status")
async def admin_codex_status(authorization: str | None = Header(default=None)):
    """检查 Codex 配置状态。"""
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


# --- Model Aliases ---

@app.get("/admin/aliases")
async def admin_get_aliases(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return proxy.get_all_aliases()


@app.put("/admin/aliases")
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


# ============================================================
# Web UI
# ============================================================

@app.get("/")
async def index(request: Request):
    response = FileResponse(str(WEB_DIR / "index.html"))
    if ADMIN_TOKEN and not ALLOW_NO_ADMIN_AUTH:
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            ADMIN_TOKEN,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https" or _env_flag("CB_GATEWAY_SECURE_COOKIE"),
            max_age=30 * 24 * 3600,
        )
    return response


# ============================================================
# 启动
# ============================================================

def main():
    global ADMIN_TOKEN, ALLOW_NO_ADMIN_AUTH

    ap = argparse.ArgumentParser(description="Buddy 2 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--admin-token", default=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
                    help="Admin API token. Defaults to CB_GATEWAY_ADMIN_TOKEN or a generated startup token.")
    ap.add_argument("--no-admin-auth", action="store_true",
                    help="Disable Admin API authentication. Only use on trusted local machines.")
    ap.add_argument("--log-level", default="warning", choices=["debug","info","warning","error"],
                    help="Log level")
    args = ap.parse_args()

    if args.no_admin_auth and args.host not in {"127.0.0.1", "localhost", "::1"}:
        ap.error("--no-admin-auth can only be used with a loopback host")

    ALLOW_NO_ADMIN_AUTH = args.no_admin_auth
    ADMIN_TOKEN = "" if ALLOW_NO_ADMIN_AUTH else (args.admin_token or f"cb-admin-{secrets.token_urlsafe(24)}")

    db.init_db()

    # 启动时自动扫描
    result = auth_manager.auto_scan_and_import()
    if result["imported"] or result["updated"]:
        sys.stderr.write(f"[startup] Auto-scan: {result}\n")

    accounts = db.list_accounts()
    sys.stderr.write(f"\n")
    sys.stderr.write(f"  Buddy 2 API v{VERSION}\n")
    sys.stderr.write(f"  ========================\n")
    sys.stderr.write(f"  监听: http://{args.host}:{args.port}\n")
    sys.stderr.write(f"  账号: {len(accounts)} 个 ({sum(1 for a in accounts if a['status']=='active')} active)\n")
    sys.stderr.write(f"  Admin: {'no auth' if ALLOW_NO_ADMIN_AUTH else 'enabled'}\n")
    if ADMIN_TOKEN:
        sys.stderr.write("  Admin Token: configured (hidden)\n")
    sys.stderr.write(f"  ========================\n\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
