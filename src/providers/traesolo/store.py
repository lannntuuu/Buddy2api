"""SOLO 凭证存储：JSON 凭证解析、文件导入与账号 upsert。

凭证磁盘形态（兼容 Go 版 trae2api-web 的 auths/trae-*.json）：
  嵌套形 {"auth": {...}, "account": {...}}  （登录闭环产出）
  扁平形 {"accessToken": ..., "uid": ...}   （手建）

本通道不依赖 IDE 本地目录，账号来源：
  1. Web 登录闭环（见 login.py）
  2. 管理页粘贴 JSON 凭证
  3. 从 CB_TRAESOLO_AUTH_DIR 目录导入 JSON 凭证文件（Go 版 auths/ 迁移）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from providers.store_common import (
    discover_summary,
    existing_uids,
    imported_file_meta,
    is_relative_to,
    upsert_account as upsert_account_by_uid,
)
from providers.traesolo.constants import (
    CHANNEL_ID,
    DOMAIN,
    ENV_AUTH_DIR,
    OAUTH_HOST,
)


def _first(body: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = body.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _first_int(body: dict, *keys: str) -> int:
    for key in keys:
        value = body.get(key)
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def normalize_expiry_ms(value: int) -> int:
    """上游/凭证文件的过期时间统一为毫秒（>1e12 视为毫秒，否则秒）。"""
    value = int(value or 0)
    if value <= 0:
        return 0
    return value if value > 1_000_000_000_000 else value * 1000


def parse_credentials(body: dict) -> dict:
    """把粘贴/文件里的 SOLO 凭证解析成 add_account 可落库的结构。

    兼容嵌套形（Go 版登录脚本产出）与扁平形（手建/面板粘贴）。
    """
    if not isinstance(body, dict):
        raise ValueError("Trae SOLO credentials must be a JSON object")

    nested_auth = body.get("auth") if isinstance(body.get("auth"), dict) else {}
    nested_account = body.get("account") if isinstance(body.get("account"), dict) else {}

    access = _first(nested_auth, "accessToken") or _first(body, "accessToken", "access_token", "token")
    refresh = _first(nested_auth, "refreshToken") or _first(body, "refreshToken", "refresh_token")
    expires = _first_int(nested_auth, "expiresAt") or _first_int(body, "expiresAt", "expires_at")
    refresh_expires = _first_int(nested_auth, "refreshExpiresAt") or _first_int(body, "refreshExpiresAt", "refresh_expires_at")
    domain = _first(nested_auth, "domain") or _first(body, "domain") or DOMAIN
    api_host = _first(nested_auth, "apiHost") or _first(body, "apiHost", "api_host") or OAUTH_HOST
    machine_id = _first(nested_auth, "machineId") or _first(body, "machineId", "machine_id")
    device_id = _first(nested_auth, "deviceId") or _first(body, "deviceId", "device_id")

    uid = _first(nested_account, "uid") or _first(body, "uid", "userId", "user_id")
    nickname = _first(nested_account, "nickname") or _first(body, "nickname", "name")
    enterprise_id = _first(nested_account, "enterpriseId") or _first(body, "enterpriseId", "enterprise_id", "TenantID")

    if not access and not refresh:
        raise ValueError("Trae SOLO credentials need accessToken or refreshToken")

    return {
        "name": nickname or f"traesolo-{(uid or 'user')[:8]}",
        "uid": uid,
        "nickname": nickname,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": normalize_expiry_ms(expires),
        "refresh_expires_at": normalize_expiry_ms(refresh_expires),
        "domain": domain,
        "enterprise_id": enterprise_id,
        "provider": CHANNEL_ID,
        "status": "active",
        "extra": {
            "machine_id": machine_id,
            "device_id": device_id,
            "api_host": api_host,
            "source": "paste",
        },
    }


def trae_solo_auth_dirs() -> list[Path]:
    """凭证文件目录候选：仅用户显式配置的 CB_TRAESOLO_AUTH_DIR。"""
    override = os.environ.get(ENV_AUTH_DIR, "").strip()
    if not override:
        return []
    return [Path(override).expanduser()]


def import_discovered(path: str) -> dict:
    """导入一个 JSON 凭证文件（嵌套/扁平），返回解析后的账号结构。"""
    target = Path(path).expanduser()
    if not target.is_file():
        raise ValueError(f"path is not a file: {path}")
    allowed = [folder.resolve() for folder in trae_solo_auth_dirs() if folder.exists()]
    resolved = target.resolve()
    if allowed and not any(is_relative_to(resolved, root) for root in allowed):
        raise ValueError(f"path is outside {ENV_AUTH_DIR}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"failed to read credential file: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("credential file must be a JSON object")
    parsed = parse_credentials(payload)
    extra = parsed.setdefault("extra", {})
    extra["source"] = str(resolved)
    extra["auth_path"] = str(resolved)
    return parsed


def discover() -> dict:
    """扫描 CB_TRAESOLO_AUTH_DIR 下的 *.json 凭证文件（不扫描 IDE 目录）。"""
    dirs_info = []
    files: list[dict] = []
    existing = existing_uids(CHANNEL_ID)
    for folder in trae_solo_auth_dirs():
        exists = folder.is_dir()
        dirs_info.append({"path": str(folder), "exists": exists, "file_count": 0})
        if not exists:
            continue
        try:
            candidates = sorted(folder.glob("*.json"))
        except OSError:
            continue
        count = 0
        for path in candidates:
            count += 1
            files.append(_file_meta(path, existing))
        dirs_info[-1]["file_count"] = count
    return discover_summary(CHANNEL_ID, dirs_info, files)


def _file_meta(path: Path, existing: set[str]) -> dict:
    return imported_file_meta(CHANNEL_ID, path, existing, import_discovered)


def upsert_account(parsed: dict) -> dict:
    """按 uid 去重入库；不存在则新增。"""
    return upsert_account_by_uid(
        CHANNEL_ID, parsed, extra_fields=("enterprise_id",), merge_extra=True
    )
