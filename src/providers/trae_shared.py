"""Trae 家族（TraeWork / Trae SOLO）共享的协议常量。

SOLO 与 TraeWork 是两条产品线，但共用同一套 TRAE 身份体系
（Cloud-IDE-JWT + refreshToken、同一 client/app id、同一批积分端点）。
这些值曾各自复制一份并已出现漂移风险，统一收敛到这里。

均为逆向测得的固定值，不要随手修改。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# --- Client fingerprint ---
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"

# --- Hosts ---
UG_HOST = "https://api.trae.cn"                  # 签到 / 积分
AGENT_HOST = "https://trae-api-cn.mchost.guru"   # agent 网关

# --- Endpoints（积分/签到/用量）---
CHECKIN_STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CHECKIN_CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
ENT_USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"


# ============================================================
# 账号 token / pick 兜底（traework / qwenwork / qclaw 共用）
# ============================================================

def is_token_expired(account: dict, skew_ms: int = 300_000) -> bool:
    """expires_at（毫秒）减 5 分钟 skew 后是否已过期；无过期时间视为不过期。"""
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - skew_ms


def extra_of(account: dict) -> dict:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    return extra


# --- refresh 失败负缓存（60s 内不重试同一账号）---

_REFRESH_FAIL_TTL_SECONDS = 60.0
_refresh_failed_at: dict[tuple[str, int], float] = {}


def _recently_failed(channel_id: str, account_id: int, now: float) -> bool:
    fail_at = _refresh_failed_at.get((channel_id, account_id))
    return fail_at is not None and (now - fail_at) < _REFRESH_FAIL_TTL_SECONDS


def _mark_refresh_failure(channel_id: str, account_id: int, now: float) -> None:
    _refresh_failed_at[(channel_id, account_id)] = now
    # 顺手清掉同通道已过期的负缓存条目，避免长期运行下无界增长。
    for key, fail_at in list(_refresh_failed_at.items()):
        if key[0] == channel_id and now - fail_at >= _REFRESH_FAIL_TTL_SECONDS:
            _refresh_failed_at.pop(key, None)


def reset_refresh_failures() -> None:
    """清空 refresh 失败负缓存（测试与账号变更场景使用）。"""
    _refresh_failed_at.clear()


async def pick_with_refresh_fallback(
    channel_id: str,
    refresh_fn,
    *,
    exclude_ids=None,
    refresh_errors: type[BaseException] | tuple = Exception,
) -> dict | None:
    """pick_account + expired 账号逐个 refresh 兜底（qwenwork / traework / qclaw 共用）。

    refresh 失败的账号记 60s 负缓存：期间不再对同一账号重复发 refresh 请求，
    避免上游故障时每个请求都原样重放失败的刷新。
    refresh_errors 指定哪些异常按"刷新失败"处理（负缓存 + 尝试下一个），
    其余异常照常向上抛（qclaw 只把 JprxError 当刷新失败）。
    """
    from accounts import auth_manager
    from storage import database as db

    exclude = exclude_ids or set()
    now = time.time()
    account = auth_manager.pick_account(exclude, provider=channel_id)
    if account:
        if is_token_expired(account):
            account_id = int(account.get("id") or 0)
            if _recently_failed(channel_id, account_id, now):
                logger.debug(
                    "skip refresh for %s account %s: failed within TTL",
                    channel_id, account_id,
                )
            else:
                try:
                    return await refresh_fn(account)
                except refresh_errors:
                    logger.debug(
                        "refresh failed for %s account %s", channel_id, account_id, exc_info=True
                    )
                    _mark_refresh_failure(channel_id, account_id, time.time())
        else:
            return account
    expired = [
        row
        for row in db.list_accounts(provider=channel_id)
        if row.get("status") == "expired"
        and row.get("id") not in exclude
        and not _recently_failed(channel_id, int(row.get("id") or 0), now)
    ]
    for row in expired:
        try:
            return await refresh_fn(row)
        except refresh_errors:
            logger.debug(
                "refresh failed for %s account %s",
                channel_id, row.get("id"), exc_info=True,
            )
            _mark_refresh_failure(channel_id, int(row.get("id") or 0), time.time())
            continue
    return None
