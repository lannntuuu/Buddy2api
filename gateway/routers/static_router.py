"""Static web UI mount + index page + admin login/logout."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from gateway.deps import (
    _check_login_rate,
    _record_login_failure,
    _set_admin_cookie,
    ADMIN_TOKEN,
    ALLOW_NO_ADMIN_AUTH,
    ADMIN_COOKIE_NAME,
)
from starlette.concurrency import run_in_threadpool

router_obj = APIRouter()

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


@router_obj.get("/")
async def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@router_obj.post("/admin/login")
async def admin_login(request: Request):
    """Validate the Admin Token and set an HttpOnly cookie (for the Web UI).

    GET / no longer auto-issues a cookie: anyone who can reach the port
    must first prove they know the token to get admin credentials. Repeated
    failures trigger a per-IP rate limit.
    """
    if ALLOW_NO_ADMIN_AUTH:
        return JSONResponse({"status": "ok"})
    _check_login_rate(request)
    from gateway.deps import _read_json_object
    data = await _read_json_object(request, allow_empty=True)
    token = str((data or {}).get("token") or "")
    if not ADMIN_TOKEN or not token or not secrets.compare_digest(
        token.encode("utf-8"), ADMIN_TOKEN.encode("utf-8")
    ):
        _record_login_failure(request)
        raise HTTPException(status_code=401, detail="Invalid admin token")
    _login_failures.pop(request.client.host if request.client else "unknown", None)
    response = JSONResponse({"status": "ok"})
    _set_admin_cookie(request, response)
    return response


@router_obj.post("/admin/logout")
async def admin_logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response
