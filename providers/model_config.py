"""Per-channel model list / alias configuration (可按平台配置模型列表).

Settings 表（JSON）：
  <channel>.models   -> ["id1", "id2", ...]      自定义模型列表
  <channel>.aliases  -> {"alias": "id", ...}     自定义别名

未配置、为空或格式非法时，回退到该通道的内置默认
（各 constants.py 的 STATIC_MODELS / ALIASES）。

本模块只服务 qclaw / qwenwork / traework 三个 provider。
WorkBuddy 本来就是动态列表，走历史键 `models` / `model_aliases`
（见 control_plane 的通道模型配置端点，四个通道统一入口）。
"""
from __future__ import annotations

from typing import Any

from storage import database as db

# token → credit 估算换算率：多少 token 算 1 credit。
# 仅用于上游不回报 credit 的通道（traesolo / qclaw / qwenwork）做**近似**消耗统计；
# 0 或不配置表示不做估算（保持原行为：credit 记 0）。
DEFAULT_CREDIT_RATE = 1000.0


def channel_credit_rate(channel: str) -> float:
    """每通道 token→credit 换算率（tokens per 1 credit）。<=0 或配置非法返回 0。"""
    raw = db.get_setting(f"{channel}.credit_rate")
    if raw is None:
        return DEFAULT_CREDIT_RATE
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CREDIT_RATE
    if rate <= 0:
        return 0.0
    return rate


def _ids_from_raw(raw: Any) -> list[str]:
    """把设置值规整成模型 id 列表；接受 ["id"] 或 [{"id": "id"}]。"""
    if not isinstance(raw, (list, tuple)):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("id")
        text = str(item or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def _aliases_from_raw(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def channel_model_ids(channel: str, default_ids) -> list[str]:
    """通道当前生效的模型 id 列表。

    按"键存在与否"判断（与自定义空区分）：
      未设置 <channel>.models → 内置默认；
      已设置（哪怕空列表）   → 以自定义为准（空 = 该平台全部 400）。
    """
    try:
        raw = db.get_setting(f"{channel}.models", None)
    except Exception:
        raw = None
    if raw is None:
        return list(default_ids)
    return _ids_from_raw(raw)


def channel_aliases(channel: str, default_aliases) -> dict[str, str]:
    """通道当前生效的别名表。

    按"键存在与否"判断：未设置 <channel>.aliases → 内置默认；
    已设置（哪怕空对象）→ 以自定义为准（空 = 该平台无别名）。
    """
    try:
        raw = db.get_setting(f"{channel}.aliases", None)
    except Exception:
        raw = None
    if raw is None:
        return dict(default_aliases)
    return _aliases_from_raw(raw)


def is_customized(channel: str) -> dict[str, bool]:
    """管理接口用：该通道哪些项设置了自定义值（自定义空也算自定义）。"""
    models_key, aliases_key = _channel_keys(channel)
    return {
        "models": db.get_setting(models_key, None) is not None,
        "aliases": db.get_setting(aliases_key, None) is not None,
    }


def _channel_keys(channel: str) -> tuple[str, str]:
    """统一配置键；workbuddy 保持历史键名。"""
    if channel == "workbuddy":
        return "models", "model_aliases"
    return f"{channel}.models", f"{channel}.aliases"


# ============================================================
# 统一模型（跨平台翻译层）
#
# 设置键 unified_models（JSON 数组）：
#   [{"name": "deepseek-v4-flash",
#     "mappings": {"workbuddy": "deepseek-v4-flash",
#                  "traework": "DeepSeek-V4-Flash-Official"}}]
#
# 纯翻译层：客户端请求统一名 → 按请求通道翻译成该平台内部名 →
# 之后照常走该平台白名单校验（白名单仍是最终闸门）。
# ============================================================

def unified_models() -> dict[str, dict[str, str]]:
    """统一模型表：统一名 -> {通道: 内部模型名}。格式非法的条目静默忽略。"""
    try:
        raw = db.get_setting("unified_models", None)
    except Exception:
        raw = None
    if not isinstance(raw, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        mappings = entry.get("mappings")
        if not name or not isinstance(mappings, dict):
            continue
        mapping = {
            str(k).strip(): str(v).strip()
            for k, v in mappings.items()
            if str(k).strip() and str(v).strip()
        }
        if mapping:
            result[name] = mapping
    return result


def translate_unified(channel: str, inner: str) -> str:
    """统一名 → 通道内部名；不是统一名或该通道无映射时原样返回。"""
    value = (inner or "").strip()
    if not value:
        return value
    mapping = unified_models().get(value)
    if mapping and channel in mapping:
        return mapping[channel]
    return value
