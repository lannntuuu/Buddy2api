"""WorkBuddy 账号快照固化。

WorkBuddy 桌面端在切换登录账号时会**覆盖**唯一的
``workbuddy-desktop.info``（以及写带时间戳的历史快照，可能随时被清理）。
为了让 Buddy2api 同时持有多个账号、且不被桌面端的覆盖/清理冲掉，导入
时把每个账号的 ``.info`` 内容**原子复制**到 Buddy2api 自己管理的目录
（默认 ``data/workbuddy_snapshots/<uid>.info``，可用 config.toml 的
``[workbuddy] snapshot_dir`` 或环境变量 ``CB_WORKBUDDY_SNAPSHOT_DIR`` 配置）。
之后 DB 的 ``extra.auth_path`` 指向固化副本；刷新成功后也回写该副本，
使副本始终保持最新、可独立复用于重装/迁移。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from storage import database as db

_SNAPSHOT_DIR_ENV = "CB_WORKBUDDY_SNAPSHOT_DIR"
_DEFAULT_SUBDIR = "workbuddy_snapshots"

# 刷新回写副本时的字段映射：DB 字段 → workbuddy-desktop.info 里的 auth 键
_REFRESH_FIELD_MAP = {
    "access_token": "accessToken",
    "refresh_token": "refreshToken",
    "expires_at": "expiresAt",
    "refresh_expires_at": "refreshExpiresAt",
    "session_state": "sessionState",
    "domain": "domain",
}


def snapshot_dir() -> Path:
    """返回固化目录（已确保存在）。config.toml 的 ``[workbuddy] snapshot_dir``
    经 server.py 导出为 ``CB_WORKBUDDY_SNAPSHOT_DIR``；缺省 ``data/workbuddy_snapshots``。"""
    env = (os.environ.get(_SNAPSHOT_DIR_ENV) or "").strip()
    if env:
        p = Path(env).expanduser()
    else:
        # src/accounts → 上溯两级到仓库根，再进 data/
        root = Path(__file__).resolve().parent.parent.parent
        p = root / "data" / _DEFAULT_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def snapshot_path(uid: str) -> Path:
    """给定 uid 返回其固化副本的完整路径。"""
    safe = _safe_uid(uid)
    return snapshot_dir() / f"{safe}.info"


def _safe_uid(uid: str) -> str:
    """把 uid 规整成文件名安全的字符串。"""
    uid = (uid or "unknown").strip()
    bad = ':*?"<>|/\\'
    return "".join("_" if c in bad else c for c in uid) or "unknown"


def ensure_snapshot(uid: str, src_file: str | Path) -> Path:
    """把 ``src_file`` 原子复制到 ``<uid>.info`` 固化副本，返回副本路径。

    复制时剥离源文件路径信息，只保留账号内容；副本是 Buddy2api 自己的资产，
    不再依赖桌面端那个会被覆盖/清理的活文件。"""
    src = Path(src_file)
    dest = snapshot_path(uid)
    if src.resolve() == dest.resolve():
        return dest
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    return dest


def write_pasted(uid: str, parsed: dict) -> Path:
    """把手动粘贴的账号内容固化成 ``<uid>.info`` 副本。

    ``parsed`` 为 _account 风格 dict（含 account/auth 嵌套或平铺字段）。
    返回副本路径；调用方据此把 ``auth_path`` 写入 DB。"""
    dest = snapshot_path(uid)
    payload = _to_info_document(uid, parsed)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def _to_info_document(uid: str, parsed: dict) -> dict:
    """把 parsed（_account 平铺格式）拼回 workbuddy-desktop.info 的
    ``{account, auth}`` 两层结构。"""
    account = parsed.get("account") if isinstance(parsed.get("account"), dict) else {}
    auth = parsed.get("auth") if isinstance(parsed.get("auth"), dict) else {}
    account_src = {
        "uid": parsed.get("uid") or account.get("uid") or uid,
        "nickname": parsed.get("nickname") or account.get("nickname") or "",
        "phoneNumber": parsed.get("phone") or account.get("phoneNumber") or "",
        "type": parsed.get("account_type") or account.get("type") or "personal",
        "enterpriseId": parsed.get("enterprise_id") or account.get("enterpriseId") or "",
    }
    auth_src = {
        "accessToken": parsed.get("access_token") or auth.get("accessToken") or "",
        "refreshToken": parsed.get("refresh_token") or auth.get("refreshToken") or "",
        "expiresAt": parsed.get("expires_at") or auth.get("expiresAt") or 0,
        "refreshExpiresAt": parsed.get("refresh_expires_at") or auth.get("refreshExpiresAt") or 0,
        "domain": parsed.get("domain") or auth.get("domain") or "www.codebuddy.cn",
        "sessionState": parsed.get("session_state") or auth.get("sessionState") or "",
    }
    return {"account": account_src, "auth": auth_src}


def write_refreshed_auth(uid: str, patch: dict) -> bool:
    """刷新成功后把新令牌回写到 ``<uid>.info`` 固化副本。

    副本不存在则跳过（说明该账号不是通过快照固化导入的，保持现状）。
    返回是否成功回写。任何异常都吞掉——回写失败不应影响主刷新流程。"""
    dest = snapshot_path(uid)
    if not dest.is_file():
        return False
    try:
        document = json.loads(dest.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return False
        auth = document.get("auth") if isinstance(document.get("auth"), dict) else {}
        for db_field, info_key in _REFRESH_FIELD_MAP.items():
            if db_field in patch and patch[db_field] is not None:
                value = patch[db_field]
                if db_field in ("expires_at", "refresh_expires_at"):
                    value = _ms_to_iso(value)
                auth[info_key] = value
        document["auth"] = auth
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, dest)
        return True
    except Exception:  # noqa: BLE001
        return False


def _ms_to_iso(ms) -> str:
    try:
        ts = int(ms) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def recent_snapshots() -> list[Path]:
    """返回当前固化目录里的全部副本（调试/迁移用）。"""
    try:
        return sorted(snapshot_dir().glob("*.info"))
    except OSError:
        return []


__all__ = [
    "snapshot_dir",
    "snapshot_path",
    "ensure_snapshot",
    "write_pasted",
    "write_refreshed_auth",
    "recent_snapshots",
    "CB_WORKBUDDY_SNAPSHOT_DIR",
]

# 供 server.py 显式导出的环境变量名（保持引用，避免被误删）
CB_WORKBUDDY_SNAPSHOT_DIR = _SNAPSHOT_DIR_ENV
