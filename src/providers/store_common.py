"""各通道 store 层共享的工具函数。

四个通道（qclaw/qwenwork/traework/traesolo）的 discover/_file_meta/upsert
结构完全一致，只有"目录枚举"和"文件解析"两处是通道私有逻辑。本模块
收敛可共享的部分，避免四份拷贝各自漂移。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


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


def discover_dirs(
    channel: str,
    dirs: list[Path],
    collect_fn: Callable[[Path], list[Path]],
    meta_fn: Callable[[Path, set[str]], dict],
) -> dict:
    """四家 store discover 的共享骨架：壳同收集异。

    逐目录先占位 {"path", "exists", "file_count": 0}；目录存在时按
    collect_fn(folder) 给出的候选文件顺序逐个过 meta_fn(path, existing)
    回填计数与文件条目。existing（已入库 uid 集合）整轮扫描前取一次。
    """
    dirs_info = []
    files: list[dict] = []
    existing = existing_uids(channel)
    for folder in dirs:
        exists = folder.is_dir()
        dirs_info.append({"path": str(folder), "exists": exists, "file_count": 0})
        if not exists:
            continue
        count = 0
        for path in collect_fn(folder):
            count += 1
            files.append(meta_fn(path, existing))
        dirs_info[-1]["file_count"] = count
    return discover_summary(channel, dirs_info, files)


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


# ============================================================
# 请求日志 · workbuddy proxy 与 openai_compat._record 共享件
# ============================================================

# credit_source='live' 门槛：usage 含任意已知 cache 键即标 live（实测语义，
# 与 dashboard accurate 对齐；原 proxy._log_request / openai_compat._record
# 各自的字面量收敛于此，键名与判定逐字保留）。
KNOWN_CACHE_KEYS = (
    "cache_read_input_tokens", "cache_creation_input_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "prompt_tokens_details",
)


def credit_source_of(usage) -> str | None:
    """usage 含任一已知 cache 键 → 'live'，否则 None（键存在性判定）。"""
    if usage is not None and any(k in usage for k in KNOWN_CACHE_KEYS):
        return "live"
    return None


def enqueue_record_request(row: dict) -> None:
    """把日志行放入默认线程池 fire-and-forget 落库；无运行 loop 时同步兜底。

    record_request（含 BEGIN IMMEDIATE 事务 + fsync）是同步阻塞调用，
    放线程池避免卡事件循环；executor 内抛出的异常经 done_callback 吞掉，
    日志失败只静默丢弃。
    """
    from storage import database as db

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (test harness). Fall back to sync.
        try:
            db.record_request(row)
        except Exception:
            pass
        return
    try:
        fut = loop.run_in_executor(None, db.record_request, row)
    except Exception:
        logger.debug("log enqueue failed", exc_info=True)
        return
    # fire-and-forget：吞掉 executor 内抛出的异常，避免“异常从未被读取”告警
    fut.add_done_callback(lambda f: f.exception() if f.cancelled() is False else None)


# ============================================================
# 请求日志（qclaw / qwenwork / traework 三家 _log 的收敛实现）
# ============================================================

_USAGE_JSON_LIMIT_BYTES = 65536


def _usage_json(usage) -> str | None:
    """usage 整包序列化留证据；>64KB 时只留存提取结果，避免污染日志表。"""
    if usage is None:
        return None
    try:
        serialized = json.dumps(usage, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(serialized.encode("utf-8")) > _USAGE_JSON_LIMIT_BYTES:
        cache_read, cache_creation = extract_cache_tokens(
            usage if isinstance(usage, dict) else None
        )
        serialized = json.dumps(
            {
                "truncated": True,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
            },
            ensure_ascii=False,
        )
    return serialized


async def log_request(
    api_key_info,
    account,
    *,
    channel: str,
    model: str,
    stream: bool,
    usage=None,
    finish_reason: str = "",
    status_code: int = 0,
    duration_ms: int = 0,
    error_msg: str = "",
    **extra,
) -> None:
    """写一条请求日志并更新账号/密钥用量计数，sqlite 写放 worker 线程执行。

    qclaw / qwenwork / traework 三家 chat 的 _log 收敛为这一份实现，
    字段语义逐字对齐 qclaw 版（含 extract_cache_tokens 与 usage_json 截断）；
    credit = round(total_tokens / channel_credit_rate(channel), 6)。
    extra 透传 prompt_tokens / completion_tokens / total_tokens /
    increment_usage 及其他 record_request 覆盖字段；first_token_ms
    （流式首帧毫秒，非流式 None）与 created_at（请求起点秒级时间戳，
    缺省落库时刻）同样经此透传。
    """
    from providers.model_config import channel_credit_rate
    from storage import database as db

    cache_read, cache_creation = extract_cache_tokens(usage if isinstance(usage, dict) else None)
    total_tokens = int(extra.pop("total_tokens", 0) or 0)
    rate = channel_credit_rate(channel)
    credit = round(total_tokens / rate, 6) if rate else 0
    row = {
        "api_key_id": api_key_info["id"] if api_key_info else None,
        "api_key_name": api_key_info["name"] if api_key_info else None,
        "account_id": account["id"] if account else None,
        "account_name": account.get("name") if account else None,
        "provider": channel,
        "model": model,
        "stream": 1 if stream else 0,
        "prompt_tokens": int(extra.pop("prompt_tokens", 0) or 0),
        "completion_tokens": int(extra.pop("completion_tokens", 0) or 0),
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "credit": credit,
        "usage_json": _usage_json(usage),
        "finish_reason": finish_reason,
        "duration_ms": duration_ms,
        "status_code": status_code,
        "error_msg": error_msg,
        "increment_usage": extra.pop("increment_usage", True),
        "client": (api_key_info or {}).get("_client_tag"),
        "client_version": (api_key_info or {}).get("_client_version"),
        "first_token_ms": extra.pop("first_token_ms", None),
        "created_at": extra.pop("created_at", None),
    }
    row.update(extra)
    try:
        # BEGIN IMMEDIATE 写事务是同步阻塞调用，放线程池避免卡事件循环
        await asyncio.to_thread(db.record_request, row)
    except Exception:
        logger.debug("record_request failed", exc_info=True)


# ============================================================
# 其他跨通道小工具
# ============================================================

def make_translator(aliases_fn, default_model: str):
    """生成 translate_model：inner 模型名经通道别名表映射，未命中原样返回。

    aliases_fn 每次调用时求值，保证管理员热更新别名表后立即生效。
    收敛 qclaw / qwenwork / traework 三份逐字相同的单行拷贝。
    """
    def translate_model(model: str) -> str:
        inner = (model or default_model).strip() or default_model
        return aliases_fn().get(inner, inner)
    return translate_model


def dedupe_dirs(dirs: list[Path]) -> list[Path]:
    """按字符串形式去重并保持顺序（三家 store 的 _auth_dirs 收敛块）。"""
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ============================================================
# Admin「测试」按钮探活 · qclaw / qwenwork / traework 三家 test_chat 共享骨架
# ============================================================

async def run_test_chat(
    model: str,
    prompt: str,
    send: Callable[[dict], Awaitable[tuple[int, str | None, object]]],
    *,
    limit: int = 240,
) -> dict:
    """单账号探活的共享外壳：构造非流式短请求，通道私有收发由 send() 承担。

    send(payload) 返回 (status_code, error_message, result)：
      失败：(status, message, None)，status 原样透传进 ok=False 行
        （HTTP >=400、信封错误、鉴权失败均在此列，message 已由各家截断）；
      成功：(status, None, result)，result 为 dict 时按 OpenAI choices 习惯
        提取 message（content 回退 reasoning_content），带 model / usage；
        为 str 时直接作为 message（traework 的 agent 回合文本）。
    httpx.HTTPError 统一映射 status_code=0；limit 控制 HTTPError 与纯文本
    成功消息的截断长度（qclaw/qwenwork 默认 240，traework 传 400）。
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": 64,
    }
    t0 = time.time()
    try:
        status, error_message, result = await send(payload)
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "message": str(exc)[:limit]}
    duration_ms = int((time.time() - t0) * 1000)
    if error_message is not None:
        return {"ok": False, "status_code": status, "duration_ms": duration_ms, "message": error_message}
    if isinstance(result, dict):
        message_obj = ((result.get("choices") or [{}])[0].get("message") or {})
        message = message_obj.get("content") or message_obj.get("reasoning_content") or ""
        return {
            "ok": True,
            "status_code": 200,
            "duration_ms": duration_ms,
            "model": result.get("model"),
            "message": str(message)[:240],
            "usage": result.get("usage") or {},
        }
    return {"ok": True, "status_code": 200, "duration_ms": duration_ms, "message": str(result)[:limit]}


# ============================================================
# 每日签到 · traework / traesolo 两家 checkin 共享实现
# ============================================================

async def run_checkin(
    account: dict,
    *,
    channel: str,
    host_of: Callable[[dict], str],
    headers_of: Callable[[dict], dict],
    client_of: Callable[[], httpx.AsyncClient],
    status_path: str,
    claim_path: str,
    code_error: bool = False,
    claim: bool = False,
) -> dict:
    """签到查询/领取的共享实现（traework / traesolo）。

    两家差异参数化：
      host_of / headers_of / client_of：host（traework 支持账号级
        extra.host 覆盖）、鉴权头与连接池（traework 用全局池，
        traesolo 用 chat 模块的配额短连接池）；
      code_error：traework 上游以 data.code != 0 表达业务失败，命中或
        HTTP >=400 均判失败，message 取 data.message（截 240）；traesolo
        只判 HTTP >=400，message 固定 f"HTTP N"；
      claim：False 查询今日状态；True 先查询，未签到才发领取请求。
    POST 空 JSON body、20s/30s 超时两家一致，收敛于此。
    """
    def row(**kwargs) -> dict:
        return checkin_row(account, channel, **kwargs)

    async def post(path: str, timeout: float) -> tuple[int, dict, str]:
        url = f"{host_of(account)}{path}"
        try:
            response = await client_of().post(url, headers=headers_of(account), json={}, timeout=timeout)
        except httpx.HTTPError as exc:
            return 0, {}, str(exc)[:240]
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return response.status_code, data, ""

    def failed(status_code: int, data: dict) -> dict:
        message = f"HTTP {status_code}"
        if code_error:
            message = str(data.get("message") or message)[:240]
        return row(ok=False, status_code=status_code, message=message)

    status_code, data, error = await post(status_path, timeout=20.0)
    if error:
        return row(ok=False, message=error)
    if status_code >= 400 or (code_error and data.get("code") not in (None, 0)):
        return failed(status_code, data)
    checked = bool(data.get("checked_in") or data.get("checkedIn"))
    try:
        credit = float(data.get("credits") or data.get("credit") or 0)
    except (TypeError, ValueError):
        credit = 0.0
    status_row = row(
        ok=True, status_code=status_code,
        already_claimed=checked, today_checked_in=checked,
        credit=credit, message=str(data.get("message") or "success"),
        extra={"enable": bool(data.get("enable", True))},
    )
    if not claim:
        return status_row
    if checked:
        status_row["already_claimed"] = True
        status_row["message"] = "今日已领取"
        return status_row
    status_code, data, error = await post(claim_path, timeout=30.0)
    if error:
        return row(ok=False, message=error)
    if status_code >= 400 or (code_error and data.get("code") not in (None, 0)):
        return failed(status_code, data)
    try:
        credit = float(data.get("credits") or data.get("credit") or status_row.get("credit") or 0)
    except (TypeError, ValueError):
        credit = 0.0
    return row(
        ok=True, status_code=status_code, claimed=True, credit=credit,
        message=str(data.get("message") or "success"),
    )
