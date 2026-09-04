"""各通道 store 层共享的工具函数。

四个通道（qclaw/qwenwork/traework/traesolo）的 discover/_file_meta/upsert
结构完全一致，只有"目录枚举"和"文件解析"两处是通道私有逻辑。本模块
收敛可共享的部分，避免四份拷贝各自漂移。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def chromium_os_crypt_key(local_state: dict, *, label: str) -> bytes:
    """Chromium 系客户端 Local State 里的 os_crypt.encrypted_key（DPAPI）。"""
    from storage.credential_crypto import CredentialCryptoError, _dpapi_decrypt

    b64 = ((local_state.get("os_crypt") or {}).get("encrypted_key")) or ""
    raw = base64.b64decode(b64)
    if not raw.startswith(b"DPAPI"):
        raise CredentialCryptoError(f"{label} Local State encrypted_key is not DPAPI")
    return _dpapi_decrypt(raw[5:])


def decrypt_chromium_v10(blob: bytes, aes_key: bytes, *, label: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from storage.credential_crypto import CredentialCryptoError

    if not blob.startswith(b"v10"):
        raise CredentialCryptoError(f"{label} cipherText is not Chromium v10")
    nonce, rest = blob[3:15], blob[15:]
    return AESGCM(aes_key).decrypt(nonce, rest, None)


def mask_uid(uid: str) -> str:
    uid = uid or ""
    return (uid[:6] + "…") if len(uid) > 6 else uid


def iso_to_ms(value) -> int:
    """ISO 时间串 / 秒或毫秒时间戳 → 毫秒时间戳；解析失败返回 0。"""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def jwt_exp_ms(token: str) -> int:
    """从 JWT 的 exp claim 取过期时间（毫秒）；不可解析返回 0。"""
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return 0
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return iso_to_ms(data.get("exp"))
    except Exception:
        return 0


def existing_uids(channel: str) -> set[str]:
    from storage import database as db

    return {
        str(row.get("uid"))
        for row in db.list_accounts(provider=channel)
        if row.get("uid")
    }


def discover_summary(channel: str, dirs_info: list[dict], files: list[dict]) -> dict:
    return {
        "dirs": dirs_info,
        "files": files,
        "file_count": len(files),
        "valid_count": sum(1 for item in files if item.get("valid")),
        "importable_count": sum(
            1 for item in files if item.get("valid") and not item.get("already_imported")
        ),
        "channel": channel,
    }


def file_meta(
    channel: str,
    path: Path,
    *,
    valid: bool,
    reason: str,
    uid: str,
    name: str,
    existing: set[str],
) -> dict:
    """discover 文件条目的统一形态。"""
    return {
        "channel": channel,
        "path": str(path),
        "valid": valid,
        "reason": reason,
        "account_name": name,
        "uid_masked": mask_uid(uid),
        "already_imported": bool(uid and uid in existing),
    }


def imported_file_meta(
    channel: str,
    path: Path,
    existing: set[str],
    import_fn,
    *,
    token_fields: tuple[str, ...] = ("access_token", "refresh_token"),
) -> dict:
    """`_file_meta` 的通用实现：调用通道的 import_fn 解析并判定有效性。"""
    reason = ""
    valid = False
    uid = ""
    name = path.name
    try:
        parsed = import_fn(str(path))
        valid = any(parsed.get(field) for field in token_fields)
        uid = str(parsed.get("uid") or "")
        name = parsed.get("nickname") or name
        if not valid:
            reason = "missing token"
    except Exception as exc:
        reason = str(exc)[:160]
        valid = False
    return file_meta(channel, path, valid=valid, reason=reason, uid=uid, name=name, existing=existing)


def upsert_account(
    channel: str,
    parsed: dict,
    *,
    extra_fields: tuple[str, ...] = (),
    merge_extra: bool = False,
) -> dict:
    """按 uid 去重入库；不存在则新增。

    extra_fields：除公共字段外额外透传的字段（如 traesolo 的
    enterprise_id），更新时按"新值优先，回退旧值"合并。
    merge_extra：更新时 extra 按键合并旧值（traesolo 语义），
    否则新值非空整体替换。
    """
    from storage import database as db

    uid = str(parsed.get("uid") or "")
    if uid:
        for row in db.list_accounts(provider=channel):
            if str(row.get("uid") or "") == uid:
                if merge_extra:
                    merged = dict(row.get("extra") or {})
                    merged.update(parsed.get("extra") or {})
                    extra = merged
                else:
                    extra = parsed.get("extra") or row.get("extra") or {}
                patch = {
                    # 空 token 不覆盖库里已有的值（避免半份凭证清掉好数据）
                    "access_token": parsed.get("access_token") or row.get("access_token") or "",
                    "refresh_token": parsed.get("refresh_token") or row.get("refresh_token") or "",
                    "nickname": parsed.get("nickname") or row.get("nickname") or "",
                    "name": parsed.get("name") or row.get("name") or "",
                    "extra": extra,
                    "status": "active",
                }
                # 只在凭证解析确实产出过期时间时才写，避免把手工设置/
                # 刷新得到的值凭空清成 0（qclaw 的凭证不含过期时间）
                if "expires_at" in parsed:
                    patch["expires_at"] = parsed.get("expires_at") or 0
                if "refresh_expires_at" in parsed:
                    patch["refresh_expires_at"] = parsed.get("refresh_expires_at") or 0
                for field in extra_fields:
                    patch[field] = parsed.get(field) or row.get(field) or ""
                db.update_account(row["id"], patch)
                return {"id": row["id"], "updated": True}
    aid = db.add_account(parsed)
    return {"id": aid, "updated": False}


def checkin_row(
    account: dict,
    channel: str,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    credit: float = 0,
    today_checked_in: bool | None = None,
    extra: dict | None = None,
) -> dict:
    """签到结果行的统一形态（traework / traesolo 共用）。"""
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": True,
        "today_checked_in": already_claimed if today_checked_in is None else today_checked_in,
        "today_credit": credit,
        "channel": channel,
        **(extra or {}),
    }


def extract_cache_tokens(usage: dict | None) -> tuple[int, int]:
    """从上游 usage 提取 (cache_read, cache_creation)：取三种风格候选字段的最大非零值。

    背景（WorkBuddy 上游实测，2026-09 数据）：copilot.tencent.com 返回的 usage 同时携带
    三种风格字段，其中 cache_read_input_tokens 是恒 0 的占位字段。旧的按优先级短路实现
    第一步就命中 0 值并直接返回 (0, 0)，把 prompt_cache_hit_tokens /
    prompt_tokens_details.cached_tokens 里的真实命中（每条可达 7 万 token）全部丢弃。
    新逻辑：三个候选取最大非零值；cache_read 是 prompt 子集，clamp 到 [0, prompt_tokens]。
    候选字段（均映射到 cache_read）：
      - Anthropic: cache_read_input_tokens
      - DeepSeek:  prompt_cache_hit_tokens
      - OpenAI:    prompt_tokens_details.cached_tokens
    cache_creation 仅 Anthropic 风格提供（其余风格无此概念）。
    """
    if not usage or not isinstance(usage, dict):
        return (0, 0)

    candidates: list[int] = []
    ar = usage.get("cache_read_input_tokens")
    if ar is not None:
        candidates.append(int(ar))
    dh = usage.get("prompt_cache_hit_tokens")
    if dh is not None:
        candidates.append(int(dh))
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
        candidates.append(int(ptd["cached_tokens"]))

    cache_read = max((c for c in candidates if c > 0), default=0)
    ac = usage.get("cache_creation_input_tokens")
    cache_creation = int(ac) if ac is not None else 0
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    return (
        max(0, min(cache_read, prompt_tokens)),
        max(0, cache_creation),
    )
