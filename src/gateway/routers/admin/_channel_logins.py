"""Admin endpoints: per-channel login flows (QClaw + Trae SOLO) and the SOLO
browser callback.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter. The two
provider accessors (`_qclaw_provider_helper` / `_traesolo_provider_helper`)
stay in `gateway.deps` (server.py re-export chain depends on them).
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from providers.qclaw.store import default_guid, upsert_account as upsert_qclaw_account
from gateway.deps import (
    _check_admin,
    _qclaw_provider_helper,
    _read_json_object,
    _solo_callback_base,
    _traesolo_provider_helper,
)

router = APIRouter()


@router.post("/admin/qclaw/import-path")
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


@router.post("/admin/qclaw/login/start")
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


@router.post("/admin/qclaw/login/complete")
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


# ============================================================
# Trae SOLO login flow
# ============================================================

@router.get("/authorize", include_in_schema=False)
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


@router.post("/admin/traesolo/login/start")
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


@router.get("/admin/traesolo/login/result")
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


@router.post("/admin/traesolo/login/cancel")
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


@router.post("/admin/traesolo/login/complete")
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
