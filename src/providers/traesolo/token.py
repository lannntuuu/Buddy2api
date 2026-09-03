"""SOLO token 刷新 / 请求头 / 登录闭环 helper（移植自 Go 版 trae2api-web）。

三类请求头：
  SOLOHeaders  → llm_utils_chat / get_detail_param（对话 + 模型表）
  UgHeaders    → checkin_credits / ide_user_ent_usage（api.trae.cn）
  OAuthHeaders → ExchangeToken / GetUserInfo（api.trae.com.cn，无签名仅 UA）
"""

from __future__ import annotations

import json
import time
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from providers.traesolo.constants import (
    APP_ID,
    CHANNEL_ID,
    CLIENT_ID,
    CONSOLE_HOST,
    DEVICE_BRAND,
    EP_EXCHANGE,
    EP_USER_INFO,
    IDE_VERSION,
    IDE_VERSION_CODE,
    OAUTH_HOST,
    OS_VERSION,
    PLUGIN_VERSION,
    REFRESH_SKEW_S,
    USER_AGENT,
)
from providers.host_override import channel_host


class TraeSoloAuthError(RuntimeError):
    """SOLO 认证/刷新失败。kind: auth | session_dead。"""

    def __init__(self, message: str, status: int = 0, kind: str = "auth"):
        self.status = status
        self.kind = kind
        super().__init__(message)


def extra_of(account: dict) -> dict:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    return extra


def _oauth_base(account: dict) -> str:
    host = str(extra_of(account).get("api_host") or
               channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST)).rstrip("/")
    return host or OAUTH_HOST


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------

def solo_headers(account: dict, stream: bool = True) -> dict[str, str]:
    """llm_utils_chat / get_detail_param 所需 SOLO 专属头（实测必须）。"""
    token = str(account.get("access_token") or "")
    extra = extra_of(account)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-Cloudide-Token": token,
        "X-Ide-Token": token,
        "X-App-Id": APP_ID,
        "X-App-Version": "default",
        "X-Ide-Version": IDE_VERSION,
        "X-Ide-Version-Code": IDE_VERSION_CODE,
        "X-App-Version-Code": IDE_VERSION_CODE,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "X-OS-Version": OS_VERSION,
        "X-Device-Brand": DEVICE_BRAND,
        "Request-Traffic-Type": "prod",
    }
    uid = str(account.get("uid") or "")
    if uid:
        headers["X-Uid"] = uid
    machine_id = str(extra.get("machine_id") or "")
    if machine_id:
        headers["X-Machine-Id"] = machine_id
    device_id = str(extra.get("device_id") or "")
    if device_id:
        headers["X-Device-Id"] = device_id
    return headers


def ug_headers(account: dict) -> dict[str, str]:
    """签到/积分（api.trae.cn）所需头。"""
    token = str(account.get("access_token") or "")
    extra = extra_of(account)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-User-Region": "CN",
    }
    device_id = str(extra.get("device_id") or "")
    if device_id:
        headers["X-Device-Id"] = device_id
    return headers


def oauth_headers() -> dict[str, str]:
    """ExchangeToken / GetUserInfo 所需头（无签名，仅 UA）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


# ---------------------------------------------------------------------------
# 过期判定
# ---------------------------------------------------------------------------

def is_token_expired(account: dict, skew_s: int = 60) -> bool:
    """expires_at 为毫秒；无过期信息视为过期（对齐 Go NeedsRefresh）。"""
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return True
    return time.time() * 1000 >= expires_at - skew_s * 1000


def needs_pre_refresh(account: dict) -> bool:
    """过期前 REFRESH_SKEW_S（24h）窗口内预刷新。"""
    return is_token_expired(account, skew_s=REFRESH_SKEW_S)


# ---------------------------------------------------------------------------
# ExchangeToken
# ---------------------------------------------------------------------------

def _normalize_ms(value: int) -> int:
    value = int(value or 0)
    if value <= 0:
        return 0
    return value if value > 1_000_000_000_000 else value * 1000


def _classify_error(status: int, body: str) -> TraeSoloAuthError:
    text = (body or "")[:200]
    if status == 401:
        return TraeSoloAuthError(f"ExchangeToken HTTP 401: {text}", status=401, kind="session_dead")
    return TraeSoloAuthError(f"ExchangeToken HTTP {status}: {text}", status=status, kind="auth")


async def _http_json_post(url: str, headers: dict, body: dict, timeout: float) -> httpx.Response:
    """短 JSON POST（自动管理 client 生命周期）。"""
    from providers.traesolo import chat as _chat  # 惰性导入避免循环

    client = _chat._make_client(timeout)
    try:
        return await client.post(url, headers=headers, json=body)
    finally:
        await _chat._aclose_client(client)


async def exchange(
    refresh_token: str,
    host: Optional[str] = None,
    timeout: float = 30.0,
) -> tuple[str, str, int, int]:
    """核心刷新：refreshToken 换 accessToken。

    返回 (access_token, refresh_token, expires_at_ms, refresh_expires_at_ms)。
    任何失败路径都不产生半更新（由调用方决定是否落盘）。
    """
    if not str(refresh_token or "").strip():
        raise TraeSoloAuthError("no refreshToken")
    base = (host or channel_host(CHANNEL_ID, "oauth_host", OAUTH_HOST)).rstrip("/")
    body = {
        "ClientID": CLIENT_ID,
        "RefreshToken": refresh_token,
        "ClientSecret": "-",
        "UserID": "",
    }
    response = await _http_json_post(f"{base}{EP_EXCHANGE}", oauth_headers(), body, timeout)
    if response.status_code >= 400:
        raise _classify_error(response.status_code, response.text)
    try:
        data = response.json()
    except ValueError as exc:
        raise TraeSoloAuthError("ExchangeToken returned non-JSON") from exc
    result = data.get("Result") if isinstance(data.get("Result"), dict) else data
    token = str(result.get("Token") or "")
    if not token:
        raise TraeSoloAuthError("refresh_failed: no token in response — re-login required")
    new_refresh = str(result.get("RefreshToken") or "") or refresh_token
    expires_ms = _normalize_ms(int(result.get("TokenExpireAt") or 0))
    if not expires_ms and int(result.get("TokenExpireDuration") or 0) > 0:
        expires_ms = int(time.time() * 1000) + int(result["TokenExpireDuration"]) * 1000
    refresh_expires_ms = _normalize_ms(int(result.get("RefreshExpireAt") or 0))
    return token, new_refresh, expires_ms, refresh_expires_ms


async def refresh_account(account: dict) -> dict:
    """刷新账号 token 并写回数据库，返回最新账号。"""
    from storage import database as db

    refresh = str(account.get("refresh_token") or "")
    token, new_refresh, expires_ms, refresh_expires_ms = await exchange(
        refresh, host=_oauth_base(account)
    )
    patch = {
        "access_token": token,
        "refresh_token": new_refresh,
        "expires_at": expires_ms,
        "refresh_expires_at": refresh_expires_ms,
        "status": "active",
    }
    db.update_account(int(account["id"]), patch)
    fresh = db.get_account(account["id"])
    return fresh or {**account, **patch}


async def get_user_info(account: dict) -> tuple[str, str, str]:
    """查询账号信息（登录闭环补全 uid/nickname）。返回 (uid, nickname, enterprise_id)。"""
    token = str(account.get("access_token") or "")
    body = {"ReqSource": "IDE", "IDEVersion": IDE_VERSION}
    headers = oauth_headers()
    if token:
        headers["X-Cloudide-Token"] = token
    response = await _http_json_post(f"{_oauth_base(account)}{EP_USER_INFO}", headers, body, 30.0)
    if response.status_code >= 400:
        raise TraeSoloAuthError(f"GetUserInfo HTTP {response.status_code}", status=response.status_code)
    try:
        data = response.json()
    except ValueError as exc:
        raise TraeSoloAuthError("GetUserInfo returned non-JSON") from exc
    result = data.get("Result") if isinstance(data.get("Result"), dict) else {}
    return (
        str(result.get("UserID") or ""),
        str(result.get("ScreenName") or ""),
        str(result.get("EnterpriseID") or ""),
    )


# ---------------------------------------------------------------------------
# 登录闭环：URL 构造 / 回调解析
# ---------------------------------------------------------------------------

def machine_trace_id(machine_id: str, device_id: str) -> str:
    """由 machineID+deviceID 派生稳定的 login_trace_id（hex16，对齐 login.sh）。"""
    h = f"{machine_id or ''}{device_id or ''}"
    if len(h) >= 16:
        return h[-16:]
    return "0" * (16 - len(h)) + h


def build_login_url(machine_id: str, device_id: str, callback_url: str) -> str:
    """构造 TRAE 登录 URL（复刻 login.sh 参数集）。"""
    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": CLIENT_ID,
        "redirect": "0",
        "login_trace_id": machine_trace_id(machine_id, device_id),
        "auth_callback_url": callback_url,
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": "PC",
        "x_device_type": "PC",
        "x_os_version": "1.0",
        "x_app_version": IDE_VERSION,
        "x_app_type": "stable",
    }
    # 与 Go url.Values.Encode() 一致：按键名排序
    encoded = urlencode(sorted(params.items()), quote_via=quote)
    return f"{channel_host(CHANNEL_ID, 'console_host', CONSOLE_HOST)}/authorization?{encoded}"


def _parse_json_param(raw: str) -> dict:
    """解回调里 URL 编码的 JSON 参数（容错双层 unquote，复刻 login.sh）。"""
    if not raw:
        return {}
    candidates = [raw]
    try:
        unq = unquote(raw)
        if unq != raw:
            candidates.append(unq)
    except Exception:
        pass
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def _json_str(obj: dict, key: str) -> str:
    value = obj.get(key) if isinstance(obj, dict) else None
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def parse_callback(raw_url: str) -> dict:
    """解析 TRAE 登录回调链接，提取凭证字段。

    回调形如：
      http://127.0.0.1:8787/authorize?refreshToken=...&userInfo={...}&userJwt={...}

    refreshToken 优先；缺失时回退 userJwt.RefreshToken；再缺失时
    直接用 userJwt.Token 作为 accessToken（login.sh 兜底分支）。
    """
    text = (raw_url or "").strip()
    if not text:
        raise ValueError("empty callback url")
    if "://" not in text:
        # 允许只粘贴 query 串
        text = "http://127.0.0.1/authorize?" + text.lstrip("?")
    parsed = urlparse(text)
    query = parse_qs(parsed.query, keep_blank_values=True)

    def q(key: str) -> str:
        return (query.get(key) or [""])[0]

    info: dict = {
        "refresh_token": q("refreshToken"),
        "access_token": "",
        "uid": "",
        "nickname": "",
        "enterprise_id": "",
        "expires_at": 0,
        "login_trace_id": q("loginTraceID") or q("login_trace_id"),
        "machine_id": q("machine_id"),
        "device_id": q("device_id"),
    }

    user_info = _parse_json_param(q("userInfo"))
    info["uid"] = _json_str(user_info, "UserID")
    info["nickname"] = _json_str(user_info, "ScreenName")
    info["enterprise_id"] = _json_str(user_info, "TenantID")

    user_jwt = _parse_json_param(q("userJwt"))
    jwt_token = _json_str(user_jwt, "Token")
    jwt_refresh = _json_str(user_jwt, "RefreshToken")

    if not info["refresh_token"]:
        info["refresh_token"] = jwt_refresh
    if not info["refresh_token"]:
        if not jwt_token:
            raise ValueError("callback missing refreshToken and userJwt.Token")
        info["access_token"] = jwt_token
        expire = 0
        try:
            expire = int(float(_json_str(user_jwt, "TokenExpireAt") or 0))
        except ValueError:
            expire = 0
        info["expires_at"] = _normalize_ms(expire)
    return info
