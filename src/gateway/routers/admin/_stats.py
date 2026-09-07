"""Admin endpoints: stats dashboard, provider×model usage, credit summary/overview.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from storage import database as db
import providers
from accounts import control_plane
from upstream import proxy
from gateway.deps import _check_admin, _check_usage_rate_limit, _usage_date_bounds

router = APIRouter()


@router.get("/admin/credit-overview")
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


@router.get("/admin/stats")
async def admin_stats(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    # get_stats 聚合 logs 全表多个维度,不能在事件循环上同步跑
    stats = await run_in_threadpool(db.get_stats)
    stats["compaction"] = proxy.compaction_stats()
    return stats


@router.get("/admin/provider-model-usage")
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


@router.get("/admin/credit-summary")
async def admin_credit_summary(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await control_plane.credit_summary(force=bool(force))
