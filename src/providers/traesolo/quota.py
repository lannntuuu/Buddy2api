"""SOLO 官方积分（ide_user_ent_usage）与每日签到（checkin_credits）。"""

from __future__ import annotations

import httpx

from providers.store_common import checkin_row
from providers.protocol import QuotaSnapshot
from providers.traesolo.constants import (
    CHANNEL_ID,
    EP_CHECKIN_CLAIM,
    EP_CHECKIN_STATUS,
    EP_ENT_USAGE,
    UG_HOST,
)
from providers.traesolo.token import ug_headers


def _checkin_row(account: dict, **kwargs) -> dict:
    return checkin_row(account, CHANNEL_ID, **kwargs)


async def _post_json(account: dict, path: str, timeout: float = 30.0):
    """POST 空 JSON 到 ug 端点，返回 (status_code, data, error_message)。"""
    url = f"{UG_HOST}{path}"
    try:
        client = _quota_client()
        response = await client.post(url, headers=ug_headers(account), json={}, timeout=timeout)
    except httpx.HTTPError as exc:
        return 0, {}, str(exc)[:240]
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return response.status_code, data, ""


def _quota_client():
    from providers.traesolo import chat as _chat

    return _chat._get_quota_client()


async def fetch_quota(account: dict) -> QuotaSnapshot:
    """聚合积分（ide_user_ent_usage 的 credits_limit 求和）。

    remain = limit - used（usage.credits_amount 是已用积分，实测）。
    """
    status_code, data, error = await _post_json(account, EP_ENT_USAGE)
    if error:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            message=error,
        )
    if status_code >= 400:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            message=f"HTTP {status_code}",
        )
    limit = 0
    used = 0
    remain = 0
    packs = 0
    for p in data.get("user_entitlement_pack_list") or []:
        if not isinstance(p, dict):
            continue
        base = p.get("entitlement_base_info") if isinstance(p.get("entitlement_base_info"), dict) else {}
        quota = base.get("quota") if isinstance(base.get("quota"), dict) else {}
        try:
            l = int(float(quota.get("credits_limit") or 0))
        except (TypeError, ValueError):
            l = 0
        if l <= 0:
            continue
        usage = p.get("usage") if isinstance(p.get("usage"), dict) else {}
        try:
            u = int(float(usage.get("credits_amount") or 0))
        except (TypeError, ValueError):
            u = 0
        limit += l
        used += u
        remain += l - u
        packs += 1
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=int(account.get("id") or 0),
        unit="credit",
        remaining=float(remain) if packs else None,
        extra={"limit": limit, "used": used, "packs": packs},
        unsupported=packs == 0,
        message="" if packs else "no entitlement packs",
    )


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    status_code, data, error = await _post_json(account, EP_CHECKIN_STATUS, timeout=20.0)
    if error:
        return _checkin_row(account, ok=False, message=error)
    if status_code >= 400:
        return _checkin_row(account, ok=False, status_code=status_code, message=f"HTTP {status_code}")
    checked = bool(data.get("checked_in") or data.get("checkedIn"))
    try:
        credit = float(data.get("credits") or data.get("credit") or 0)
    except (TypeError, ValueError):
        credit = 0.0
    return _checkin_row(
        account,
        ok=True,
        status_code=status_code,
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
    status_code, data, error = await _post_json(account, EP_CHECKIN_CLAIM, timeout=30.0)
    if error:
        return _checkin_row(account, ok=False, message=error)
    if status_code >= 400:
        return _checkin_row(
            account,
            ok=False,
            status_code=status_code,
            message=f"HTTP {status_code}",
        )
    try:
        credit = float(data.get("credits") or data.get("credit") or status.get("credit") or 0)
    except (TypeError, ValueError):
        credit = 0.0
    return _checkin_row(
        account,
        ok=True,
        status_code=status_code,
        claimed=True,
        credit=credit,
        message=str(data.get("message") or "success"),
    )


# --- 账户级权益 / 过期积分（用于补全"历史总消耗"估算）---
ENTITLEMENT_LIST_PATH = "/trae/api/v2/pay/user_current_entitlement_list"
EXPIRED_ENTS_PATH = "/trae/api/v2/pay/expired_ents"


async def fetch_entitlement_list(account: dict) -> dict:
    """当前权益包列表（含 usage_summary.consumed_amount = 当前包已用积分）。"""
    status_code, data, error = await _post_json(account, ENTITLEMENT_LIST_PATH)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "data": data or {}}


async def fetch_expired_ents(account: dict) -> dict:
    """已过期积分包列表（每个只有 credits_limit 上限，无"已用"字段）。"""
    status_code, data, error = await _post_json(account, EXPIRED_ENTS_PATH)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "data": data or {}}
