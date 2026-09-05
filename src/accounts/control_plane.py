"""Cross-channel admin orchestration. Never sends chat; never failovers vendors."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from pathlib import Path

import logging

from storage import database as db
from storage import credit_cache
from upstream import proxy
import providers
from accounts import auth_manager
import providers.model_config as model_config
from providers.model_config import _channel_keys, _ids_from_raw, is_customized, unified_models
from providers.protocol import KNOWN_CHANNEL_SET  # noqa: F401  (kept for legacy callers)
from providers.qclaw.constants import ALIASES as _QCLAW_DEFAULT_ALIASES
from providers.qclaw.constants import STATIC_MODELS as _QCLAW_DEFAULT_MODELS
from providers.qwenwork.constants import ALIASES as _QWENWORK_DEFAULT_ALIASES
from providers.qwenwork.constants import STATIC_MODELS as _QWENWORK_DEFAULT_MODELS
from providers.traework.constants import ALIASES as _TRAEWORK_DEFAULT_ALIASES
from providers.traework.constants import STATIC_MODELS as _TRAEWORK_DEFAULT_MODELS
from providers.traesolo.constants import ALIASES as _TRAESOLO_DEFAULT_ALIASES
from providers.traesolo.constants import STATIC_MODELS as _TRAESOLO_DEFAULT_MODELS

logger = logging.getLogger(__name__)

# credit-summary 结果级快照缓存 TTL（秒）。0 = 关闭缓存。签到/领取后会 invalidate()。
_CREDIT_SUMMARY_TTL = float(os.environ.get("CB_GATEWAY_CREDIT_SUMMARY_TTL", "300"))


async def _refresh_credit_snapshot_bg(expected_gen: int) -> None:
    """过期快照的后台重建（stale-while-revalidate），单飞保证同一时刻只有一次重建。

    expected_gen 在调度时刻捕获（而非任务体首行——create_task 只是调度，
    invalidate 可能发生在调度与任务体启动之间）；重建期间若发生 invalidate
    （如签到领取成功），代数变化，本次结果作废不回填，避免用旧数据覆盖失效。
    """
    try:
        snap = await _build_credit_summary(False)
        if credit_cache.generation() == expected_gen:
            credit_cache.set_snapshot(snap)
    except Exception:  # noqa: BLE001
        logger.warning("credit_summary 后台刷新失败，沿用旧快照", exc_info=True)
    finally:
        credit_cache.mark_refreshing(False)


def _maybe_schedule_credit_refresh() -> None:
    if credit_cache.is_refreshing():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    credit_cache.mark_refreshing(True)
    loop.create_task(_refresh_credit_snapshot_bg(credit_cache.generation()))


def invalidate_credit_summary_cache() -> None:
    """签到/领取/强制刷新成功后调用，使额度快照失效。"""
    credit_cache.invalidate()


async def _gather_limited(accounts: list[dict], operation, limit: int = 2) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(account: dict):
        async with semaphore:
            return await operation(account)

    if not accounts:
        return []
    return list(await asyncio.gather(*(run(account) for account in accounts)))

_PREVIEW_TTL_SEC = 600
_previews: dict[str, dict] = {}
_TOKEN_FIELDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_expires_at",
    "session_state",
    "extra",
    "nickname",
    "name",
    "phone",
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def auto_import_enabled() -> bool:
    return _env_flag("CB_GATEWAY_AUTO_IMPORT", False)


def checkin_gap_seconds() -> float:
    raw = os.environ.get("CB_GATEWAY_CHECKIN_GAP_MS", "800")
    try:
        return max(0, int(raw)) / 1000.0
    except (TypeError, ValueError):
        return 0.8


def path_hash(path: str) -> str:
    return hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()


def _purge_previews():
    now = time.time()
    dead = [key for key, item in _previews.items() if item["expires"] <= now]
    for key in dead:
        _previews.pop(key, None)


def issue_preview(channel: str, files: list[dict]) -> str:
    _purge_previews()
    token = secrets.token_urlsafe(24)
    paths_by_hash = {}
    for item in files:
        path = item.get("path")
        if not path:
            continue
        paths_by_hash[path_hash(path)] = path
    _previews[token] = {
        "channel": channel,
        "hashes": set(paths_by_hash),
        "paths": paths_by_hash,
        "expires": time.time() + _PREVIEW_TTL_SEC,
    }
    return token


def lookup_preview(token: str, channel: str) -> dict:
    _purge_previews()
    item = _previews.get(token or "")
    if not item or item["channel"] != channel:
        raise ValueError("preview_token is invalid or expired")
    return item


_WINDOWS_DPAPI_CHANNELS = frozenset({"qclaw", "qwenwork"})


def _attach_runtime(payload: dict, channel: str) -> dict:
    runtime = dict(payload.get("runtime") or {})
    in_container = auth_manager._running_in_container()
    runtime["container"] = bool(runtime.get("container") or in_container)
    runtime["host_auth_limited"] = bool(
        runtime["container"] and channel in _WINDOWS_DPAPI_CHANNELS
    )
    payload["runtime"] = runtime
    payload["channel"] = channel
    return payload


def workbuddy_discover(auth_dir: str | None = None) -> dict:
    payload = auth_manager.discover_auth_files(auth_dir)
    files = []
    for item in payload.get("files") or []:
        row = dict(item)
        row["channel"] = "workbuddy"
        files.append(row)
    payload["files"] = files
    payload["preview_token"] = issue_preview("workbuddy", files)
    return _attach_runtime(payload, "workbuddy")


def discover(channel: str | None = None, auth_dir: str | None = None) -> dict:
    if not channel or channel == "workbuddy":
        return workbuddy_discover(auth_dir)
    provider = providers.get_provider(channel)
    if provider is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    discover_fn = getattr(provider, "discover", None)
    if discover_fn is None:
        raise ValueError(f"Channel '{channel}' does not support discover")
    payload = discover_fn()
    if not isinstance(payload, dict):
        payload = {"files": [], "dirs": []}
    files = payload.get("files") or []
    payload["preview_token"] = issue_preview(channel, files)
    return _attach_runtime(payload, channel)


def _allowed_roots_workbuddy(auth_dir: str | None) -> list[Path]:
    return [path.resolve() for path in auth_manager.candidate_auth_dirs(auth_dir)]


def _is_under(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _upsert_workbuddy(parsed: dict, auth_path: str) -> str:
    parsed = dict(parsed)
    parsed["provider"] = "workbuddy"
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = auth_path
    parsed["extra"] = extra
    uid = str(parsed.get("uid") or "")
    if uid:
        for row in db.list_accounts(provider="workbuddy"):
            if str(row.get("uid") or "") == uid:
                patch = {key: parsed[key] for key in _TOKEN_FIELDS if key in parsed}
                db.update_account(row["id"], patch)
                return "updated"
    db.add_account(parsed)
    return "imported"


def import_workbuddy(paths: list[str], preview: dict, auth_dir: str | None) -> dict:
    roots = _allowed_roots_workbuddy(auth_dir)
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    hashes = preview.get("hashes") or set()
    for raw in paths:
        path = Path(raw)
        digest = path_hash(str(path))
        if digest not in hashes:
            result["errors"].append({"path": raw, "error": "path is not in the current preview"})
            result["skipped"] += 1
            continue
        if not _is_under(path, roots):
            result["errors"].append({"path": raw, "error": "path is outside WorkBuddy auth dirs"})
            result["skipped"] += 1
            continue
        parsed = auth_manager.parse_auth_file(path)
        if not parsed:
            result["skipped"] += 1
            continue
        action = _upsert_workbuddy(parsed, str(path.resolve()))
        result[action] += 1
    return result


def import_channel(channel: str, preview_token: str, paths: list[str] | None, auth_dir: str | None = None) -> dict:
    preview = lookup_preview(preview_token, channel)
    if not paths:
        paths = list((preview.get("paths") or {}).values())
    if channel == "workbuddy":
        return import_workbuddy(paths, preview, auth_dir)
    provider = providers.get_provider(channel)
    if provider is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    import_path = getattr(provider, "import_path", None)
    parse_credentials = getattr(provider, "parse_credentials", None)
    upsert = getattr(provider, "upsert_account", None)
    if import_path is None:
        raise ValueError(f"Channel '{channel}' does not support import")
    if not paths:
        discovered = discover(channel)
        paths = [item["path"] for item in discovered.get("files") or [] if item.get("valid")]
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    for raw in paths:
        digest = path_hash(raw)
        if digest not in (preview.get("hashes") or set()):
            result["errors"].append({"path": raw, "error": "path is not in the current preview"})
            result["skipped"] += 1
            continue
        try:
            parsed = import_path(raw)
            if upsert:
                info = upsert(parsed)
            elif channel == "qclaw":
                from providers.qclaw.store import upsert_account as qclaw_upsert

                info = qclaw_upsert(parsed)
            else:
                uid = str(parsed.get("uid") or "")
                info = {"updated": False}
                if uid:
                    for row in db.list_accounts(provider=channel):
                        if str(row.get("uid") or "") == uid:
                            patch = {key: parsed[key] for key in _TOKEN_FIELDS if key in parsed}
                            db.update_account(row["id"], patch)
                            info = {"updated": True}
                            break
                if not info.get("updated"):
                    db.add_account({**parsed, "provider": channel})
            if info.get("updated"):
                result["updated"] += 1
            else:
                result["imported"] += 1
        except Exception as exc:
            result["errors"].append({"path": raw, "error": str(exc)[:200]})
            result["skipped"] += 1
    return result


def startup_scan() -> dict:
    summary = {"auto_import": auto_import_enabled(), "channels": []}
    for channel in providers.enabled_provider_ids():
        try:
            preview = discover(channel)
        except Exception as exc:
            summary["channels"].append({"channel": channel, "error": str(exc)[:200]})
            continue
        files = preview.get("files") or []
        valid = sum(1 for item in files if item.get("valid"))
        entry = {
            "channel": channel,
            "dirs": preview.get("dirs") or [],
            "file_count": preview.get("file_count") or len(files),
            "valid_count": preview.get("valid_count") or valid,
        }
        if auto_import_enabled():
            imported = import_channel(channel, preview.get("preview_token") or "", None)
            entry["import"] = imported
        summary["channels"].append(entry)
    return summary


# ============================================================
# 每通道模型列表 / 别名配置（可按平台配置）
#
# 统一存储键（settings 表，JSON）：
#   <channel>.models   -> ["id1", "id2", ...]
#   <channel>.aliases  -> {"alias": "id", ...}
# 未配置时回退各通道内置默认。WorkBuddy 兼容历史键：
#   models（[{"id":...}]）与 model_aliases（叠加在内置别名上）。
# ============================================================

_CHANNEL_DEFAULTS: dict[str, tuple[list[str], dict[str, str]]] = {
    "workbuddy": (
        [str(item["id"]) for item in proxy.DEFAULT_MODELS],
        dict(proxy._BUILTIN_ALIASES),
    ),
    "qclaw": (list(_QCLAW_DEFAULT_MODELS), dict(_QCLAW_DEFAULT_ALIASES)),
    "qwenwork": (list(_QWENWORK_DEFAULT_MODELS), dict(_QWENWORK_DEFAULT_ALIASES)),
    "traework": (list(_TRAEWORK_DEFAULT_MODELS), dict(_TRAEWORK_DEFAULT_ALIASES)),
    "traesolo": (list(_TRAESOLO_DEFAULT_MODELS), dict(_TRAESOLO_DEFAULT_ALIASES)),
}


def _custom_default_models_and_aliases(channel: str) -> tuple[list[str], dict[str, str]] | None:
    """For custom OpenAI-compat channels: pull the built-in defaults straight
    from the persisted definition. Return None when the channel isn't a
    custom definition (so callers can fall back to the built-in dict).

    Editing a definition's models/aliases updates the default here too —
    but only matters to admins who haven't written their own <id>.models
    override (D8). The model surface itself reads from `<id>.models` if set
    via channel_model_ids() (handled by providers.openai_compat).
    """
    try:
        from providers import custom_channels
    except Exception:
        return None
    definition = custom_channels.get_definition(channel)
    if not definition:
        return None
    models = [str(m) for m in (definition.get("models") or []) if str(m).strip()]
    aliases_raw = definition.get("aliases") or {}
    aliases = {
        str(k).strip(): str(v).strip()
        for k, v in aliases_raw.items()
        if str(k).strip() and str(v).strip()
    }
    return models, aliases


def _resolve_default_models_and_aliases(channel: str) -> tuple[list[str], dict[str, str]]:
    """`_CHANNEL_DEFAULTS` for built-ins, definition-derived defaults for
    custom channels. Raises KeyError only for unknown ids — callers must
    pre-check."""
    if channel in _CHANNEL_DEFAULTS:
        return _CHANNEL_DEFAULTS[channel]
    custom = _custom_default_models_and_aliases(channel)
    if custom is not None:
        return custom
    raise KeyError(channel)


def _validate_models(models) -> list[str]:
    """整体替换白名单。[] = 自定义空白名单（该平台所有模型 400）。"""
    if not isinstance(models, list):
        raise ValueError("models must be a list of model ids")
    for item in models:
        if isinstance(item, dict):
            if not str(item.get("id") or "").strip():
                raise ValueError("each models entry must be a non-empty string or {'id': ...}")
        elif not str(item).strip():
            raise ValueError("each models entry must be a non-empty string or {'id': ...}")
    return _ids_from_raw(models)


def _validate_aliases(aliases) -> dict[str, str]:
    """整体替换别名表。{} = 自定义空（该平台无任何别名）。"""
    if not isinstance(aliases, dict):
        raise ValueError("aliases must be an object mapping alias -> model id")
    result: dict[str, str] = {}
    for key, value in aliases.items():
        key_s, value_s = str(key).strip(), str(value).strip()
        if not key_s or not value_s:
            raise ValueError("aliases keys and values must be non-empty strings")
        result[key_s] = value_s
    return result


def channel_model_view(channel: str) -> dict:
    """通道当前生效的模型列表 / 别名，附内置默认与自定义标记。"""
    channel = str(channel or "").strip()
    if not providers.is_known_channel(channel):
        raise ValueError(f"Unknown channel '{channel}'")
    provider = providers.get_provider(channel)
    if provider is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    effective_ids = [
        str(item["id"]) if isinstance(item, dict) else str(item)
        for item in provider.list_models()
    ]
    default_ids, default_aliases = _resolve_default_models_and_aliases(channel)
    # per-model 明细（含官方消耗倍率）。无 fetch_model_rates 钩子的通道回退到白名单。
    rates_fn = getattr(provider, "fetch_model_rates", None)
    if callable(rates_fn):
        model_details = rates_fn()
    else:
        model_details = [
            {"id": mid, "display_name": mid, "rate": None, "context_window": None, "official": False}
            for mid in effective_ids
        ]
    return {
        "channel": channel,
        "models": effective_ids,
        "model_details": model_details,
        "aliases": provider.alias_map(),
        "defaults": {"models": default_ids, "aliases": default_aliases},
        "customized": is_customized(channel),
        "credit_rate": model_config.channel_credit_rate(channel),
        "credit_rate_default": model_config.channel_credit_rate(channel),
        "credit_rate_customized": db.get_setting(f"{channel}.credit_rate") is not None,
        # 按模型思考档位（取代环境变量）
        "reasoning_supported": bool(getattr(provider, "supports_reasoning_effort", False)),
        "reasoning": model_config.channel_reasoning(channel),
        "reasoning_default": model_config.channel_reasoning(channel).get("__default__", ""),
        "reasoning_customized": db.get_setting(f"{channel}.reasoning") is not None,
        "reasoning_choices": list(model_config.REASONING_CHOICES),
    }


async def refresh_channel_models(channel: str) -> dict:
    """强制刷新某通道的官方模型表（仅支持动态拉取的通道，如 traesolo）。

    返回 {"channel", "refreshed": bool, "model_details": [...], "note": str}。
    非动态通道返回 refreshed=false 并附说明。
    """
    channel = str(channel or "").strip()
    if not providers.is_known_channel(channel):
        raise ValueError(f"Unknown channel '{channel}'")
    provider = providers.get_provider(channel)
    if provider is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    # traesolo 有动态刷新能力
    refresh_fn = getattr(provider, "refresh_dynamic_models", None)
    if callable(refresh_fn):
        ok = await refresh_fn(force=True)
        view = channel_model_view(channel)
        return {
            "channel": channel,
            "refreshed": bool(ok),
            "model_details": view.get("model_details", []),
            "note": "" if ok else "刷新失败（可能无可用账号或上游不可达）",
        }
    view = channel_model_view(channel)
    return {
        "channel": channel,
        "refreshed": False,
        "model_details": view.get("model_details", []),
        "note": "该通道无动态模型接口，仅展示静态白名单（上游不提供倍率）",
    }


def set_channel_models(
    channel: str,
    *,
    models=None,
    aliases=None,
    credit_rate=None,
    reasoning=None,
    set_models: bool = False,
    set_aliases: bool = False,
    set_rate: bool = False,
    set_reasoning: bool = False,
) -> dict:
    """设置或重置通道模型列表 / 别名 / credit 换算率 / 按模型思考档位。
    None 表示重置为默认。返回最新视图。"""
    channel = str(channel or "").strip()
    if not providers.is_known_channel(channel):
        raise ValueError(f"Unknown channel '{channel}'")
    if providers.get_provider(channel) is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    if not (set_models or set_aliases or set_rate or set_reasoning):
        raise ValueError(
            "Provide 'models' and/or 'aliases' and/or 'credit_rate' and/or 'reasoning' (null resets)"
        )

    models_key, aliases_key = _channel_keys(channel)
    if set_models:
        if models is None:
            db.delete_setting(models_key)
        else:
            ids = _validate_models(models)
            if channel == "workbuddy":
                db.set_setting(models_key, [{"id": mid} for mid in ids])
            else:
                db.set_setting(models_key, ids)
    if set_aliases:
        if aliases is None:
            db.delete_setting(aliases_key)
        else:
            mapping = _validate_aliases(aliases)
            db.set_setting(aliases_key, mapping)
    if set_rate:
        if credit_rate is None:
            db.delete_setting(f"{channel}.credit_rate")
        else:
            try:
                rate = float(credit_rate)
            except (TypeError, ValueError):
                raise ValueError("credit_rate must be a number")
            if rate < 0:
                raise ValueError("credit_rate must be >= 0")
            db.set_setting(f"{channel}.credit_rate", rate)
    if set_reasoning:
        if reasoning is None:
            db.delete_setting(f"{channel}.reasoning")
        else:
            validated = model_config._validate_reasoning(reasoning)
            db.set_setting(f"{channel}.reasoning", validated)
    return channel_model_view(channel)


# ------------------------------------------------------------
# 统一模型（跨平台翻译层）：统一名 -> {通道: 内部模型名}
# 纯翻译层，白名单仍是各通道的最终闸门。
# ------------------------------------------------------------

def unified_model_view() -> dict:
    """统一模型当前配置 + 可选通道列表（管理页宽表用）。"""
    return {
        "models": [
            {"name": name, "mappings": dict(mapping)}
            for name, mapping in unified_models().items()
        ],
        "channels": providers.enabled_provider_ids(),
    }


def _validate_unified_models(models) -> list[dict]:
    if not isinstance(models, list):
        raise ValueError("models must be a list of {name, mappings}")
    seen: set[str] = set()
    result: list[dict] = []
    for entry in models:
        if not isinstance(entry, dict):
            raise ValueError("each unified model must be an object with name and mappings")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError("unified model name must be a non-empty string")
        if name in seen:
            raise ValueError(f"duplicate unified model '{name}'")
        seen.add(name)
        mappings = entry.get("mappings")
        if not isinstance(mappings, dict):
            raise ValueError(f"unified model '{name}' needs a mappings object")
        cleaned: dict[str, str] = {}
        for channel, inner in mappings.items():
            channel_s = str(channel).strip()
            inner_s = str(inner).strip()
            if not channel_s or not inner_s:
                raise ValueError(f"unified model '{name}' has an empty channel or inner id")
            if not providers.is_known_channel(channel_s):
                raise ValueError(f"unified model '{name}' references unknown channel '{channel_s}'")
            cleaned[channel_s] = inner_s
        if not cleaned:
            raise ValueError(f"unified model '{name}' has no mappings")
        result.append({"name": name, "mappings": cleaned})
    return result


def set_unified_models(models) -> dict:
    """整体替换统一模型表；[] = 清空。返回最新视图。"""
    cleaned = _validate_unified_models(models)
    db.set_setting("unified_models", cleaned)
    return unified_model_view()


async def _channel_accounts(channel: str, status: str = "active") -> list[dict]:
    rows = [
        account
        for account in db.list_accounts(provider=channel)
        if not status or account.get("status") == status
    ]
    rows.sort(key=lambda item: int(item.get("id") or 0))
    return rows


async def credit_summary(force: bool = False) -> dict:
    """结果级缓存 + SWR 的入口；真实构建逻辑在 _build_credit_summary。"""
    if not _CREDIT_SUMMARY_TTL:
        return await _build_credit_summary(force)
    if force:
        # 用户显式强制刷新：绕过账号级缓存，并刷新进程级快照
        snap = await _build_credit_summary(True)
        credit_cache.set_snapshot(snap)
        return snap
    if credit_cache.has_snapshot():
        cached = credit_cache.get_snapshot(_CREDIT_SUMMARY_TTL)
        if cached.get("cache") == "stale":
            _maybe_schedule_credit_refresh()
        return cached
    snap = await _build_credit_summary(False)
    credit_cache.set_snapshot(snap)
    return snap


async def _build_credit_summary(force: bool = False) -> dict:
    channels = []
    workbuddy_resources = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        accounts = await _channel_accounts(channel)
        if channel == "workbuddy":
            resources = []
            if accounts:
                resources = await _gather_limited(
                    accounts,
                    lambda account: auth_manager.fetch_account_resources(account, force=force),
                    limit=4,
                )
            workbuddy_resources = resources
            ok = [row for row in resources if row.get("ok")]
            remaining = round(sum(float(row.get("total_dosage") or row.get("available_total") or 0) for row in ok), 4)
            channels.append(
                {
                    "id": channel,
                    "display_name": getattr(provider, "display_name", channel),
                    "unit": "credit",
                    "remaining": remaining,
                    "ok": True,
                    "accounts": len(accounts),
                    "ok_accounts": len(ok),
                    "unsupported": False,
                    "expiring_7d_total": round(sum(float(row.get("expiring_7d_total") or 0) for row in ok), 4),
                    "expiring_30d_total": round(sum(float(row.get("expiring_30d_total") or 0) for row in ok), 4),
                    "package_count": sum(int(row.get("package_count") or 0) for row in ok),
                }
            )
            continue
        fetch_quota = getattr(provider, "fetch_quota", None) if provider else None
        if fetch_quota is None:
            channels.append(
                {
                    "id": channel,
                    "display_name": getattr(provider, "display_name", channel) if provider else channel,
                    "unit": "unknown",
                    "remaining": None,
                    "ok": True,
                    "accounts": len(accounts),
                    "unsupported": True,
                    "message": "quota API not available",
                }
            )
            continue
        remaining_values = []
        ok_count = 0
        unsupported = False
        message = ""
        snapshots = await _gather_limited(
            accounts, lambda account: fetch_quota(account), limit=4
        )
        for snapshot in snapshots:
            unit = getattr(snapshot, "unit", None) if not isinstance(snapshot, dict) else snapshot.get("unit")
            ok = bool(getattr(snapshot, "ok", None) if not isinstance(snapshot, dict) else snapshot.get("ok"))
            snap_unsupported = bool(
                getattr(snapshot, "unsupported", False) if not isinstance(snapshot, dict) else snapshot.get("unsupported")
            )
            if snap_unsupported:
                unsupported = True
                message = (
                    getattr(snapshot, "message", "") if not isinstance(snapshot, dict) else snapshot.get("message") or ""
                )
            if ok:
                ok_count += 1
            value = getattr(snapshot, "remaining", None) if not isinstance(snapshot, dict) else snapshot.get("remaining")
            if unit == "credit" and value is not None and not snap_unsupported:
                remaining_values.append(float(value))
        remaining = round(sum(remaining_values), 4) if remaining_values else None
        channels.append(
            {
                "id": channel,
                "display_name": getattr(provider, "display_name", channel),
                "unit": "credit",
                "remaining": remaining,
                "ok": True,
                "accounts": len(accounts),
                "ok_accounts": ok_count,
                "unsupported": remaining is None,
                "message": message or ("no credit balance" if remaining is None else ""),
            }
        )
    now_ts = int(time.time())
    ok_resources = [row for row in workbuddy_resources if row.get("ok")]
    stale_count = sum(1 for row in workbuddy_resources if row.get("stale"))
    failed_count = sum(1 for row in workbuddy_resources if not row.get("ok"))
    low_accounts = []
    expiring_accounts = []
    for row in workbuddy_resources:
        balance = float(row.get("total_dosage") or row.get("available_total") or 0)
        item = {
            "account_id": row.get("account_id"),
            "account_name": row.get("account_name"),
            "channel": "workbuddy",
            "balance": round(balance, 4),
            "expiring_30d_total": round(float(row.get("expiring_30d_total") or 0), 4),
            "next_expire_time": row.get("next_expire_time") or "",
            "next_expire_days": row.get("next_expire_days"),
            "ok": bool(row.get("ok")),
            "stale": bool(row.get("stale")),
            "age_seconds": int(row.get("age_seconds") or 0),
        }
        if row.get("ok") and balance <= 300:
            low_accounts.append(item)
        if row.get("ok") and float(row.get("expiring_30d_total") or 0) > 0:
            expiring_accounts.append(item)
    low_accounts.sort(key=lambda item: (item["balance"], item["account_id"] or 0))
    expiring_accounts.sort(
        key=lambda item: (item["next_expire_days"] if item["next_expire_days"] is not None else 9999, -item["expiring_30d_total"])
    )
    active_total = sum(item.get("accounts") or 0 for item in channels)
    return {
        "ok": failed_count == 0,
        "updated_at": now_ts,
        "active_accounts": active_total,
        "resource_accounts": len(workbuddy_resources),
        "ok_accounts": len(ok_resources),
        "failed_accounts": failed_count,
        "stale_accounts": stale_count,
        "total_balance": None,
        "channels": channels,
        "expiring_7d_total": round(sum(float(row.get("expiring_7d_total") or 0) for row in ok_resources), 4),
        "expiring_30d_total": round(sum(float(row.get("expiring_30d_total") or 0) for row in ok_resources), 4),
        "package_count": sum(int(row.get("package_count") or 0) for row in ok_resources),
        "low_accounts": low_accounts[:8],
        "expiring_accounts": expiring_accounts[:8],
        "accounts": workbuddy_resources,
    }


async def checkin_status_all(force: bool = False) -> dict:
    results = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        if provider is None or not getattr(provider, "checkin_supported", False):
            continue
        accounts = await _channel_accounts(channel)
        if not accounts:
            continue
        fetch_checkin = getattr(provider, "fetch_checkin", None)
        if fetch_checkin:
            chunk = await _gather_limited(
                accounts,
                lambda account, fn=fetch_checkin: fn(account, force=force),
                limit=4,
            )
        else:
            chunk = await _gather_limited(
                accounts,
                lambda account: auth_manager.fetch_checkin_status(account, force=force),
                limit=4,
            )
        for item in chunk:
            if isinstance(item, dict):
                item["channel"] = channel
        results.extend(chunk)
    return {
        "total": len(results),
        "ok": sum(1 for row in results if row.get("ok")),
        "claimed": sum(1 for row in results if row.get("claimed")),
        "already_claimed": sum(1 for row in results if row.get("already_claimed") or row.get("today_checked_in")),
        "available": sum(1 for row in results if row.get("ok") and not (row.get("already_claimed") or row.get("today_checked_in"))),
        "failed": sum(1 for row in results if not row.get("ok")),
        "stale": sum(1 for row in results if row.get("stale")),
        "results": results,
    }


async def checkin_all(channel_filter: list[str] | None = None) -> dict:
    wanted = set(channel_filter) if channel_filter else None
    results = []
    gap = checkin_gap_seconds()
    skipped = []
    first = True
    for channel in providers.enabled_provider_ids():
        if wanted is not None and channel not in wanted:
            continue
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        if not getattr(provider, "checkin_supported", False):
            skipped.append({"channel": channel, "reason": "checkin_unsupported"})
            continue
        accounts = await _channel_accounts(channel)
        for account in accounts:
            if not first and gap:
                await asyncio.sleep(gap)
            first = False
            claim_fn = getattr(provider, "claim_checkin", None)
            if claim_fn:
                result = await claim_fn(account)
            else:
                result = await auth_manager.claim_daily_checkin(account)
            if result.get("ok") and not claim_fn:
                result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
            result["channel"] = channel
            results.append(result)
    invalidate_credit_summary_cache()
    wb_credit = round(
        sum(float(row.get("credit") or 0) for row in results if row.get("claimed") and row.get("channel") == "workbuddy"),
        4,
    )
    return {
        "total": len(results),
        "claimed": sum(1 for row in results if row.get("claimed")),
        "already_claimed": sum(1 for row in results if row.get("already_claimed")),
        "failed": sum(1 for row in results if not row.get("ok")),
        "credit": wb_credit,
        "credit_deprecated": True,
        "skipped": skipped,
        "results": results,
    }


# ============================================================
# TraeWork 官方消耗真值同步（usage_type=7 session 明细 -> 按天聚合落库）
# ============================================================

async def sync_traework_usage(days: int = 90) -> dict:
    """拉取 TraeWork 官方 session 消耗明细，按天+模型聚合写入 traework_daily_credit。

    返回 {synced_days, sessions, total_credits}。失败返回 {ok:False, error}。
    注意：这是官方真值，与 logs.credit 的估算无关；dashboard 的 TraeWork 消耗改读此真值。
    """
    from providers.traework import quota as tw_quota

    account = auth_manager.pick_account(None, provider="traework")
    if account is None:
        return {"ok": False, "error": "no traework account"}
    try:
        sessions = await tw_quota.fetch_session_usage(account, days=days)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:240]}

    # 按 (day, model_name) 聚合
    agg: dict[tuple, dict] = {}
    for s in sessions:
        if not s.get("usage_time"):
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(s["usage_time"]))
        key = (day, s.get("model_name") or "?")
        bucket = agg.setdefault(key, {"day": day, "model_name": key[1], "credits": 0.0, "sessions": 0})
        bucket["credits"] += float(s.get("credits_float") or 0)
        bucket["sessions"] += 1
    rows = list(agg.values())
    db.upsert_traework_daily_credit(rows)
    total_credits = round(sum(r["credits"] for r in rows), 4)
    return {
        "ok": True,
        "synced_days": len({r["day"] for r in rows}),
        "sessions": len(sessions),
        "total_credits": total_credits,
    }


# ============================================================
# 账户级历史总消耗估算（当前已用 + 已过期积分，假设过期部分已用完）
# 说明：TRAE 官方不提供"历史总消耗"直读，也不提供 traesolo 单产品真值。
# 这里用 user_current_entitlement_list 的 consumed_amount（当前包已用）
# + expired_ents 的过期包额度上限（假设用完后过期）估算历史总消耗。
# 仅作账户级概览，不可用于按产品/按日拆分。
# ============================================================

async def account_credit_overview() -> dict:
    from providers.traesolo import quota as ts_quota

    account = auth_manager.pick_account(None, provider="traesolo")
    if account is None:
        # 退而求其次用 traework 账号（共享同一 TRAE 用户）
        account = auth_manager.pick_account(None, provider="traework")
    if account is None:
        return {"ok": False, "error": "no trae account"}
    try:
        ent = await ts_quota.fetch_entitlement_list(account)
        exp = await ts_quota.fetch_expired_ents(account)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:240]}

    ent_data = (ent.get("data") or {}) if ent.get("ok") else {}
    exp_data = (exp.get("data") or {}) if exp.get("ok") else {}
    usage_summary = ent_data.get("usage_summary") or {}
    current_consumed = float(usage_summary.get("consumed_amount") or 0)
    expired_list = exp_data.get("expired_ent_list") or []
    expired_total = sum(float(e.get("credits_limit") or 0) for e in expired_list)
    # 假设过期包已用完（官方不回报"过期包实际已用"，只能假设）
    historical_estimate = round(current_consumed + expired_total, 4)
    return {
        "ok": True,
        "current_consumed": round(current_consumed, 4),
        "expired_total": round(expired_total, 4),
        "expired_count": len(expired_list),
        "historical_estimate": historical_estimate,
        "assumed_expired_used": True,
        "note": "历史总消耗=当前包已用+过期包额度上限（假设过期部分已用完）；官方不回报过期包实际用量，故为估算。",
    }
