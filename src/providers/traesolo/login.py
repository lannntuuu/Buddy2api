"""SOLO Web 登录闭环（移植自 Go 版 internal/server/login.go）。

流程：
  start_login(callback_base)
    → 生成随机 machine_id/device_id（hex32）+ pending 态
    → 返回 TRAE 登录 URL（浏览器打开完成登录）
  本地部署：登录完成后 TRAE 302 回 {callback_base}/authorize?...
    → complete_from_callback() 解析 → ExchangeToken → GetUserInfo → 落库
  远程部署：浏览器够不到回调地址时，用户从浏览器地址栏复制完整回调 URL，
    走同一 complete_from_callback()（手动闭环，等价 Go 版"粘贴回调链接"导入）。

pending 态只在内存（TTL 10 分钟，重启丢失），符合"登录态瞬时"语义。
TRAE 回调不回传 machine_id/device_id，只回传 loginTraceID（= 派生 trace），
故用 loginTraceID 反查 pending 拿回登录时生成的 id 对（对齐 Go 实现）。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Optional

from providers.traesolo.constants import (
    AUTHORIZE_PATH,
    CHANNEL_ID,
    DOMAIN,
    OAUTH_HOST,
    PENDING_TTL_S,
)
from providers.traesolo import store
from providers.host_override import channel_host
from providers.traesolo.token import (
    TraeSoloAuthError,
    build_login_url,
    exchange,
    get_user_info,
    machine_trace_id,
    parse_callback,
)

_lock = threading.Lock()
_logins: dict[str, dict] = {}


def _purge() -> None:
    now = time.time()
    for pid in [k for k, v in _logins.items() if now - v["created"] > PENDING_TTL_S]:
        _logins.pop(pid, None)


def start_login(callback_base: str) -> dict:
    """生成登录 URL + pending 态。callback_base 如 http://127.0.0.1:8787。"""
    base = str(callback_base or "").strip().rstrip("/")
    if not base:
        raise ValueError("callback_base is required")
    machine_id = secrets.token_hex(16)
    device_id = secrets.token_hex(16)
    pending_id = secrets.token_hex(8)
    callback_url = f"{base}{AUTHORIZE_PATH}"
    with _lock:
        _purge()
        _logins[pending_id] = {
            "state": "pending",
            "machine_id": machine_id,
            "device_id": device_id,
            "callback_url": callback_url,
            "trace_id": machine_trace_id(machine_id, device_id),
            "created": time.time(),
            "uid": "",
            "nickname": "",
            "error": "",
        }
    return {
        "login_url": build_login_url(machine_id, device_id, callback_url),
        "pending_id": pending_id,
        "callback_url": callback_url,
    }


def get_pending(pending_id: str) -> Optional[dict]:
    with _lock:
        _purge()
        pl = _logins.get((pending_id or "").strip())
        return dict(pl) if pl else None


def result(pending_id: str) -> dict:
    pl = get_pending(pending_id)
    if pl is None:
        return {"found": False, "pending_id": pending_id}
    out = {"found": True, "pending_id": pending_id, "state": pl["state"]}
    if pl["state"] == "success":
        out["uid"] = pl["uid"]
        out["nickname"] = pl["nickname"]
    elif pl["state"] == "failed":
        out["error"] = pl["error"]
    return out


def cancel(pending_id: str) -> dict:
    with _lock:
        _purge()
        pl = _logins.get((pending_id or "").strip())
        if pl is not None:
            if pl["state"] == "pending":
                pl["state"] = "canceled"
            _logins.pop(pending_id, None)
        return {"pending_id": pending_id, "canceled": pl is not None}


def _find_by_trace(trace_id: str) -> Optional[dict]:
    """用 loginTraceID 反查 pending（回调只带 trace，不带 machine/device）。"""
    with _lock:
        _purge()
        for pl in _logins.values():
            if trace_id and pl["trace_id"] == trace_id:
                return pl
    return None


def _mark_pending(trace_id: str, state: str, uid: str = "", nickname: str = "", error: str = "") -> None:
    pl = _find_by_trace(trace_id)
    if pl is None:
        return
    with _lock:
        pl["state"] = state
        if uid:
            pl["uid"] = uid
        if nickname:
            pl["nickname"] = nickname
        if error:
            pl["error"] = error[:400]


async def _finish_login(info: dict, machine_id: str, device_id: str) -> dict:
    """解析后的回调凭证 → ExchangeToken → GetUserInfo → 落库。

    返回 {ok, uid, nickname, account_id, updated, error}。
    """
    machine_id = machine_id or secrets.token_hex(16)
    device_id = device_id or secrets.token_hex(16)
    access = str(info.get("access_token") or "")
    refresh = str(info.get("refresh_token") or "")
    expires_ms = int(info.get("expires_at") or 0)
    refresh_expires_ms = 0

    # 有 refreshToken → ExchangeToken 换新 access + 轮换 refreshToken
    if refresh:
        access, refresh, expires_ms, refresh_expires_ms = await exchange(refresh, host=channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST))
    if not access:
        # parse_callback 已保证 access 或 refresh 至少有一个；走到这里说明
        # exchange 没返回 token。
        raise ValueError("no access token after exchange")

    uid = str(info.get("uid") or "")
    nickname = str(info.get("nickname") or "")
    enterprise_id = str(info.get("enterprise_id") or "")

    # GetUserInfo 补全 uid/nickname（失败不阻断）
    temp_account = {
        "access_token": access,
        "refresh_token": refresh,
        "uid": uid,
        "extra": {"machine_id": machine_id, "device_id": device_id, "api_host": channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST)},
    }
    try:
        g_uid, g_nick, g_ent = await get_user_info(temp_account)
        if g_uid:
            uid = g_uid
        if g_nick and not nickname:
            nickname = g_nick
        if g_ent and not enterprise_id:
            enterprise_id = g_ent
    except TraeSoloAuthError:
        pass

    if not uid:
        raise ValueError("cannot determine uid from callback or GetUserInfo")

    parsed = store.parse_credentials(
        {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires_ms,
            "domain": DOMAIN,
            "apiHost": channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST),
            "machineId": machine_id,
            "deviceId": device_id,
            "uid": uid,
            "nickname": nickname,
            "enterpriseId": enterprise_id,
        }
    )
    parsed["refresh_expires_at"] = refresh_expires_ms
    parsed["extra"] = {
        "machine_id": machine_id,
        "device_id": device_id,
        "api_host": channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST),
        "source": "web_login",
    }
    res = store.upsert_account(parsed)
    return {
        "ok": True,
        "uid": uid,
        "nickname": nickname,
        "account_id": res.get("id"),
        "updated": res.get("updated", False),
        "error": "",
    }


async def complete_from_callback(raw_url: str, *, require_pending: bool = False) -> dict:
    """回调落点统一入口：解析 → 换 token → 落库 → 标记 pending 成功。

    require_pending=True 时（无鉴权的 /authorize 落点），回调必须匹配
    start_login 发起的 pending 会话才继续，否则拒绝——防止第三方凭
    伪造/自有的回调 URL 向网关注入账号。admin 鉴权的手动粘贴闭环
    （POST /admin/traesolo/login/complete）不受此限制。
    """
    try:
        info = parse_callback(raw_url)
    except ValueError as exc:
        return {"ok": False, "error": f"callback parse failed: {exc}"}
    trace_id = str(info.get("login_trace_id") or "")

    # 用 loginTraceID 反查 pending 拿回登录时生成的 machine/device 对
    machine_id = ""
    device_id = ""
    pl = _find_by_trace(trace_id)
    if pl is not None:
        machine_id = pl["machine_id"]
        device_id = pl["device_id"]
    elif require_pending:
        return {
            "ok": False,
            "error": "callback does not match any pending web login; start a new login from the admin page",
        }

    try:
        done = await _finish_login(info, machine_id, device_id)
    except (ValueError, TraeSoloAuthError) as exc:
        _mark_pending(trace_id, "failed", error=str(exc))
        return {"ok": False, "error": str(exc)[:400]}
    _mark_pending(trace_id, "success", uid=done["uid"], nickname=done["nickname"])
    return done


def reset() -> None:
    """测试辅助：清空 pending 态。"""
    with _lock:
        _logins.clear()
