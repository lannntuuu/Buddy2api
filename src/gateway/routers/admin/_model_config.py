"""Admin endpoints: unified model table, legacy models row, model aliases.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from storage import database as db
from accounts import control_plane
from upstream import proxy
from gateway.deps import _check_admin, _read_json, _read_json_object

router = APIRouter()


@router.get("/admin/unified-models")
async def admin_get_unified_models(authorization: str | None = Header(default=None)):
    """Unified model (cross-platform translation layer) current config."""
    _check_admin(authorization)
    return await run_in_threadpool(control_plane.unified_model_view)


@router.put("/admin/unified-models")
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


# ============================================================
# Legacy models (settings row)
# ============================================================

@router.get("/admin/models")
async def admin_get_models(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_setting("models", proxy.DEFAULT_MODELS)


@router.put("/admin/models")
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
# Model aliases
# ============================================================

@router.get("/admin/aliases")
async def admin_get_aliases(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return proxy.get_all_aliases()


@router.put("/admin/aliases")
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
