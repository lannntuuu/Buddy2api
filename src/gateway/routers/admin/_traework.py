"""Admin endpoints: TraeWork official consumption-truth sync + view.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from storage import database as db
from accounts import control_plane
from gateway.deps import _check_admin

router = APIRouter()


@router.post("/admin/traework/sync-usage")
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


@router.get("/admin/traework/usage")
async def admin_traework_usage(authorization: str | None = Header(default=None)):
    """View synced TraeWork official consumption truth (per day)."""
    _check_admin(authorization)

    def _load():
        return {
            "by_day": db.get_traework_daily_credit(days=90),
            "total_credits": db.get_traework_total_credit(),
            "last_sync_at": db.latest_traework_sync_at(),
        }

    return await run_in_threadpool(_load)
