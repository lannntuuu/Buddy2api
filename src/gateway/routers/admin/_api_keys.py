"""Admin endpoints: API key CRUD + one-off secret reveal.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from storage import database as db
from storage.repos.api_keys import (
    get_api_key_by_id as _get_api_key_by_id,
    get_api_key_secret as _get_api_key_secret,
)
from gateway.deps import _check_admin, _read_json_object, _validate_key_channel

router = APIRouter()


# ============================================================
# API keys
# ============================================================

@router.get("/admin/api-keys")
async def admin_list_keys(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    # 列表不再携带明文(此前 include_secret=True 把 key_secret/key 全量回传);
    # 需要 明文 的场景走 GET /admin/api-keys/{kid}/reveal 按需取一次。
    return db.list_api_keys()


@router.get("/admin/api-keys/{kid}/reveal")
async def admin_reveal_key(kid: int, authorization: str | None = Header(default=None)):
    """按需返回单个 Key 明文。全仓库唯一返回明文的端点。"""
    _check_admin(authorization)
    row = await run_in_threadpool(_get_api_key_by_id, kid)
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    key = await run_in_threadpool(_get_api_key_secret, kid)
    if key is None:
        raise HTTPException(
            status_code=400, detail="旧版本只保存了哈希,无法还原原始 Key"
        )
    return {"ok": True, "key": key}


@router.post("/admin/api-keys")
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


@router.put("/admin/api-keys/{kid}")
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
    row = await run_in_threadpool(_get_api_key_by_id, kid)
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "ok", "ok": True, "key": row}


@router.delete("/admin/api-keys/{kid}")
async def admin_delete_key(
    kid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_api_key(kid)
    return {"status": "ok"}
