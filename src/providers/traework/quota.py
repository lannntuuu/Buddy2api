"""TraeWork check-in and official credit usage."""

from __future__ import annotations

import httpx

from providers.protocol import QuotaSnapshot
from providers.store_common import checkin_row
from providers.traework.constants import (
    CHANNEL_ID,
    CHECKIN_CLAIM_PATH,
    CHECKIN_STATUS_PATH,
    REQ_SOURCE,
    UG_API,
    USAGE_PATH,
)
from providers.traework.token import auth_headers, extra_of
from storage.http_pool import get_client


def _host(account: dict) -> str:
    extra = extra_of(account)
    return str(extra.get("host") or UG_API).rstrip("/") or UG_API


def _checkin_row(account: dict, **kwargs) -> dict:
    return checkin_row(account, CHANNEL_ID, **kwargs)


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    url = f"{_host(account)}{CHECKIN_STATUS_PATH}"
    try:
        client = get_client()
        response = await client.post(url, headers=auth_headers(account), json={}, timeout=20.0)
    except httpx.HTTPError as exc:
        return _checkin_row(account, ok=False, message=str(exc)[:240])
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        return _checkin_row(
            account,
            ok=False,
            status_code=response.status_code,
            message=str((data or {}).get("message") or f"HTTP {response.status_code}")[:240],
        )
    checked = bool(data.get("checked_in") or data.get("checkedIn"))
    credit = float(data.get("credits") or data.get("credit") or 0)
    return _checkin_row(
        account,
        ok=True,
        status_code=response.status_code,
        already_claimed=checked,
        today_checked_in=checked,
        credit=credit,
        message=str(data.get("message") or "success"),
        extra={"enable": bool(data.get("enable", True))},
    )


async def claim_checkin(account: dict) -> dict:
    status = await fetch_checkin(account, force=True)
    if not status.get("ok"):
        return status
    if status.get("already_claimed") or status.get("today_checked_in"):
        status["already_claimed"] = True
        status["message"] = "今日已领取"
        return status
    url = f"{_host(account)}{CHECKIN_CLAIM_PATH}"
    try:
        client = get_client()
        response = await client.post(url, headers=auth_headers(account), json={}, timeout=30.0)
    except httpx.HTTPError as exc:
        return _checkin_row(account, ok=False, message=str(exc)[:240])
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        return _checkin_row(
            account,
            ok=False,
            status_code=response.status_code,
            message=str((data or {}).get("message") or f"HTTP {response.status_code}")[:240],
        )
    credit = float(data.get("credits") or data.get("credit") or status.get("credit") or 0)
    return _checkin_row(
        account,
        ok=True,
        status_code=response.status_code,
        claimed=True,
        credit=credit,
        message=str(data.get("message") or "success"),
    )


async def fetch_quota(account: dict) -> QuotaSnapshot:
    url = f"{_host(account)}{USAGE_PATH}"
    body = {"require_usage": True, "req_source": REQ_SOURCE}
    try:
        client = get_client()
        response = await client.post(url, headers=auth_headers(account), json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            message=str(exc)[:240],
        )
    if response.status_code >= 400:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            message=f"HTTP {response.status_code}",
        )
    try:
        data = response.json()
    except ValueError:
        data = {}
    usage = data.get("usage_summary") if isinstance(data.get("usage_summary"), dict) else {}
    total = usage.get("total_amount")
    consumed = usage.get("consumed_amount")
    remaining = None
    if isinstance(total, (int, float)):
        remaining = float(total) - float(consumed or 0)
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=int(account.get("id") or 0),
        unit="credit",
        remaining=remaining,
        extra={"consumed": consumed, "total": total},
        unsupported=remaining is None,
        message="" if remaining is not None else "quota unit unknown",
    )


# --- 官方消耗真值（按 session 明细，usage_type=7 = TraeWork 专属）---
# 与 logs.credit（估算）无关：这里直接拉官方 session 接口的 credits_float 真值。
SESSION_USAGE_PATH = "/trae/api/v1/pay/query_user_usage_group_by_session"


async def fetch_session_usage(
    account: dict, *, days: int = 90, usage_type: int = 7
) -> list[dict]:
    """拉取 TraeWork 按 session 的真实消耗明细。

    返回每条 session 的 {session_id, model_name, credits_float, usage_time, usage_source}。
    page_size 不能 > 50（实测 100 返回空），需翻页。usage_type=7 为 TraeWork 专属真值。
    """
    from providers.traesolo.constants import UG_HOST  # 复用共享积分 host
    import time as _time

    now = int(_time.time())
    start = now - days * 86400
    headers = auth_headers(account)
    out: list[dict] = []
    page = 1
    while True:
        body = {
            "start_time": start,
            "end_time": now,
            "page_size": 50,
            "page_num": page,
            "usage_type": [usage_type],
        }
        try:
            client = get_client()
            resp = await client.post(
                f"{UG_HOST}{SESSION_USAGE_PATH}", headers=headers, json=body, timeout=30.0
            )
        except httpx.HTTPError as exc:
            break
        try:
            data = resp.json()
        except ValueError:
            break
        sessions = data.get("user_usage_group_by_sessions") or []
        if not sessions:
            break
        for s in sessions:
            out.append({
                "session_id": s.get("session_id"),
                "model_name": s.get("model_name") or "",
                "credits_float": float(s.get("credits_float") or 0),
                "usage_time": int(s.get("usage_time") or 0),
            })
        if len(sessions) < 50:
            break
        page += 1
    return out
