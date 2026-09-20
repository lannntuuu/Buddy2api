"""TraeWork token refresh via ExchangeToken. Isolated from WorkBuddy."""

from __future__ import annotations

import os
import sys
import time
from typing import Optional
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from providers.trae_shared import extra_of, is_token_expired
from providers.traework.constants import (
    CHANNEL_ID,
    CLIENT_ID,
    EXCHANGE_PATH,
    IDE_VERSION,
    PLATFORM_CODE,
    UG_API,
)
from providers.traework.store import import_discovered, iso_to_ms
from providers.host_override import channel_host
from storage.http_pool import get_client


class TraeWorkAuthError(RuntimeError):
    pass


def auth_headers(account: dict) -> dict[str, str]:
    extra = extra_of(account)
    token = str(account.get("access_token") or "")
    device_id = str(extra.get("device_id") or "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "User-Agent": f"TRAE-SOLO-CN/{IDE_VERSION}",
    }
    if device_id:
        headers["x-device-id"] = device_id
    return headers


def oauth_headers(token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "x-cloudide-token": token}


def _host(account: dict) -> str:
    extra = extra_of(account)
    host = str(extra.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API)).rstrip("/")
    return host or UG_API


def _device_info(account: dict) -> dict:
    extra = extra_of(account)
    return {
        "DeviceID": str(extra.get("device_id") or ""),
        "MachineID": str(extra.get("machine_id") or ""),
        "PlatformCode": PLATFORM_CODE,
        "DeviceType": "PC",
        "DeviceName": os.environ.get("COMPUTERNAME") or os.environ.get("USERNAME") or "PC",
        "ClientVersion": IDE_VERSION,
        "DevicePublicKey": str(extra.get("public_key_pem") or ""),
        "OSInfo": "windows",
    }


def _device_proof(refresh_token: str, private_pem: str) -> dict:
    timestamp = int(time.time())
    nonce = os.urandom(16).hex()
    material = "\n".join(
        ["POST", EXCHANGE_PATH, CLIENT_ID, refresh_token, str(timestamp), nonce]
    )
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TraeWorkAuthError("TraeWork device key is not ECDSA")
    der = key.sign(material.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    import base64

    signature = base64.b64encode(der).decode("ascii")
    return {"Signature": signature, "Timestamp": timestamp, "Nonce": nonce}


def _parse_exchange(result: dict) -> dict:
    token = str(result.get("Token") or result.get("token") or "")
    refresh = str(result.get("RefreshToken") or result.get("refreshToken") or "")
    expired_at = iso_to_ms(result.get("TokenExpireAt") or result.get("expiredAt"))
    if not expired_at and result.get("TokenExpireDuration"):
        expired_at = int(time.time() * 1000) + int(result["TokenExpireDuration"])
    refresh_expired = iso_to_ms(result.get("RefreshExpireAt") or result.get("refreshExpiredAt"))
    return {
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": expired_at,
        "refresh_expires_at": refresh_expired,
    }


async def refresh_account(account: dict) -> dict:
    from storage import database as db

    refresh = str(account.get("refresh_token") or "")
    extra = extra_of(account)
    private_pem = str(extra.get("private_key_pem") or "")
    if not refresh:
        raise TraeWorkAuthError("TraeWork account has no refresh_token")
    if not private_pem:
        raise TraeWorkAuthError("TraeWork account has no device private key")
    access = str(account.get("access_token") or "")
    proof = _device_proof(refresh, private_pem)
    body = {
        "ClientID": CLIENT_ID,
        "ClientSecret": "",
        "RefreshToken": refresh,
        "DeviceInfo": _device_info(account),
        "DeviceProof": proof,
        "IDEVersion": IDE_VERSION,
    }
    url = f"{_host(account)}{EXCHANGE_PATH}"
    client = get_client()
    response = await client.post(url, headers=oauth_headers(access), json=body, timeout=30.0)
    if response.status_code >= 400:
        raise TraeWorkAuthError(f"ExchangeToken failed: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise TraeWorkAuthError("ExchangeToken returned non-JSON") from exc
    result = data.get("Result") if isinstance(data.get("Result"), dict) else data
    parsed = _parse_exchange(result if isinstance(result, dict) else {})
    if not parsed.get("access_token"):
        raise TraeWorkAuthError("ExchangeToken response missing Token")
    patch = {**parsed, "status": "active"}
    if not patch.get("refresh_token"):
        patch["refresh_token"] = refresh
    db.update_account(int(account["id"]), patch)
    fresh = db.get_account(account["id"])
    return fresh or {**account, **patch}


def _client_credentials_updated(account: dict, parsed: dict, *, require_newer: bool = False) -> bool:
    """新凭据是否值得接管。

    require_newer=False（自愈路径）：expires_at 更大，或 access/refresh token 与现有不同。
      此时旧凭据已被上游判废，任何"不同的"客户端凭据都比手里的死票强。
    require_newer=True（启动对齐路径）：**只认 expires_at 更大**。
      启动时并未发生鉴权失败，若仅凭"token 不同"就接管，会把网关刚刷新好的新票
      换成客户端手里的旧票（两边不同但客户端更旧），反而弄坏可用凭据。
    """
    old_exp = int(account.get("expires_at") or 0)
    new_exp = int(parsed.get("expires_at") or 0)
    if new_exp and new_exp > old_exp:
        return True
    if require_newer:
        return False
    for field in ("access_token", "refresh_token"):
        old_v = str(account.get(field) or "")
        new_v = str(parsed.get(field) or "")
        if new_v and new_v != old_v:
            return True
    return False


def _describe_client_storage_error(exc: Exception) -> str:
    """把 import_discovered 的底层异常翻译成用户能直接照着做的中文说明。

    起因：早先统一写 "client storage unreadable"，把「客户端自己没登录」
    和「文件真的读不了」混为一谈，用户会误以为是权限/路径问题去排查文件。
    这里按真实原因分类，并给出下一步动作。
    """
    text = str(exc)
    if "has no iCubeAuthInfo" in text:
        # storage.json 存在但缺主凭据键 —— 客户端处于登出态，是最常见的一种。
        return "客户端当前未登录（storage.json 里没有 iCubeAuthInfo 凭据），请先在 TRAE SOLO CN 客户端登录"
    if "outside CB_TRAEWORK_AUTH_DIR" in text:
        return f"路径不在允许的扫描范围内，已拒绝读取（{text}）"
    if "checksum failed" in text or "padding is invalid" in text or "not tc AES" in text:
        return f"凭据可读到但解密失败（{text}），可能需要重新登录客户端"
    if "not UTF-8" in text or "is not an object" in text:
        return f"凭据内容格式异常（{text}）"
    return f"无法读取客户端凭据文件（{type(exc).__name__}: {text}）"


async def adopt_credentials_from_client(account: dict, *, require_newer: bool = False) -> bool:
    """refresh 失效后的「凭据自救」：从客户端 storage.json 重读最新凭据。

    触发点：refresh_account 抛鉴权失效（401/20101 这类）时由 chat.py::_turn
    的账号重试循环调用。全程 best-effort：任何异常都只降级为「维持原状」，
    不抛出打断请求链路。

    require_newer=True 供启动对齐使用：只接管「明确更新」的凭据，
    避免把网关已有的新票换成客户端手里的旧票（见 _client_credentials_updated）。

    返回 True 表示已接管（调用方应重试本回合），False 表示放弃自救。
    """
    account_id = int(account.get("id") or 0)
    extra = extra_of(account)
    auth_path = str(extra.get("auth_path") or "")
    if not auth_path:
        # 该账号没有记录客户端路径（如纯粘贴导入）→ 本就没有自救素材。
        sys.stderr.write(
            f"[traework-adopt] 放弃：账号 {account_id} 未记录客户端 storage.json 路径，"
            f"无法自救（可到管理页重新导入一次以记录路径）\n"
        )
        return False
    try:
        # 复用 import_discovered：内部含目录白名单校验 + 解密，
        # 任何路径不在白名单 / 文件不可读 / 解密失败都会抛异常 → 这里接住即放弃。
        parsed = import_discovered(auth_path)
    except Exception as exc:  # noqa: BLE001 - 自救必须 best-effort，绝不外抛
        sys.stderr.write(
            f"[traework-adopt] 放弃自救：{_describe_client_storage_error(exc)}（账号 {account_id}）\n"
        )
        return False
    # uid 必须一致才允许接管，防止读到别的账号的凭据。
    # 注意：new_uid 为空时无法核对身份，一律拒绝（不"默认放行"）。
    old_uid = str(account.get("uid") or "")
    new_uid = str(parsed.get("uid") or "")
    if old_uid and (not new_uid or old_uid != new_uid):
        sys.stderr.write(
            f"[traework-adopt] 放弃自救：客户端凭据属于另一个账号"
            f"（客户端 uid={new_uid or '空'}，本账号 uid={old_uid}），拒绝接管；账号 {account_id}\n"
        )
        return False
    if not _client_credentials_updated(account, parsed, require_newer=require_newer):
        # 客户端凭据没有更新 → 不是"旧票被作废"，无需接管，维持原状。
        # 这是正常的"无需动作"分支，不打日志避免刷屏。
        return False
    new_access = str(parsed.get("access_token") or "")
    new_refresh = str(parsed.get("refresh_token") or "")
    # 至少要拿到一个可用 token 才接管，避免把账号"复活"成空凭据。
    if not (new_access or new_refresh):
        sys.stderr.write(
            f"[traework-adopt] 放弃自救：客户端凭据里没有可用的 token（账号 {account_id}）\n"
        )
        return False
    patch = {"status": "active"}
    # 仅在解析出非空值时才覆盖对应字段：expires_at 解析失败会得到 0，
    # 无条件写入会把 DB 里有效的过期时间砸成 0（is_token_expired 判定随之错乱）。
    if new_access:
        patch["access_token"] = new_access
    if new_refresh:
        patch["refresh_token"] = new_refresh
    if int(parsed.get("expires_at") or 0):
        patch["expires_at"] = int(parsed["expires_at"])
    if int(parsed.get("refresh_expires_at") or 0):
        patch["refresh_expires_at"] = int(parsed["refresh_expires_at"])
    from storage import database as db

    db.update_account(account_id, patch)
    sys.stderr.write(
        f"[traework-adopt] 已从客户端 storage.json 接管更新后的凭据（账号 {account_id}），"
        f"状态置回 active\n"
    )
    return True
