"""Custom OpenAI-compat channels (definition-driven; data in settings).

Settings key `custom_channels`: JSON array of definitions, one per channel.
Keys (`api_key`) do NOT live here — they go through the normal `accounts` table
(provider=<channel id>). The module:

  * persists definitions:  list_definitions() / save_definitions() / get_definition(id)
  * validates input:       validate_definition() (3.1: slug, base_url, models, aliases, env name)
  * builds Provider instances from definitions (one cache slot per id)
  * invalidates the cache after a CRUD write so the next request rebuilds (D3)

Built-in channels (workbuddy/qclaw/qwenwork/traework/traesolo/gmi/bailian) are
registered through `providers._LOADED`; they coexist with custom definitions
and take priority in `providers.get_provider()`. The seed migration in
gateway/server.py lifespan writes gmi/bailian into the settings key on first
boot so the built-in set and the data-driven set converge.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from providers.openai_compat import OpenAICompatProvider
from storage import database as db

SETTINGS_KEY = "custom_channels"

# When the admin omits `models` (or sends an empty list), the channel falls
# back to this default whitelist. Slug char set [a-z0-9_-] uppercases cleanly.
DEFAULT_MODELS = ("DeepSeek-V4-Flash",)

# Definition validation ------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# 与通道 id 的 slug 字符集对齐：id 允许 [a-z0-9_-]，自动填充 "CB_"+id.upper()
# 可能含连字符，此处必须同样放行（spec 23 §1.2）。Windows 环境变量名含 '-' 合法。
_ENV_NAME_RE = re.compile(r"^CB_[A-Z0-9_-]+$")


def validate_definition(
    definition: dict,
    *,
    reserved_ids: set[str],
    exclude_id: str | None = None,
) -> None:
    """Raise ValueError when definition violates spec 3.1.

    `reserved_ids` MUST be the set of channel ids that the new definition
    must not collide with (built-in ids + ids already used by other custom
    definitions). When editing an existing definition, pass its id in
    `exclude_id` so the self-collision is ignored.
    """
    # 文案约定:校验错误直接面向用户(前端第一道拦截后这里是兜底,API 调用方
    # 也会看到),一律中文人话,不暴露裸正则。
    if not isinstance(definition, dict):
        raise ValueError("通道定义必须是 JSON 对象")

    cid = str(definition.get("id") or "").strip()
    if not cid:
        raise ValueError("通道 ID 必填")
    if not _SLUG_RE.match(cid):
        raise ValueError(
            "通道 ID 需以小写字母开头,只含小写字母/数字/下划线/连字符,最长 32 字符"
        )
    if cid != exclude_id and cid in reserved_ids:
        raise ValueError(f"通道 ID「{cid}」已被占用,请换一个")

    display_name = str(definition.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("显示名称必填")
    if len(display_name) > 40:
        raise ValueError("显示名称不得超过 40 字符")

    base_url = str(definition.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("Base URL 必填")
    # 放行任意 http:// / https://（含内网 http）：API key 走明文有泄密风险，
    # 前端在输入框下方给出「仅建议内网」警告，由管理员自行权衡。
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("Base URL 需以 http:// 或 https:// 开头")

    models = definition.get("models")
    # `models` is OPTIONAL. Omitted / None / empty list all mean "use the
    # default whitelist" — the handler fills DEFAULT_MODELS before persisting.
    # Validation only checks the values the admin actually provided
    # (spec 23 §1). Keep this function pure.
    cleaned_models: list[str] = []
    if models:
        if not isinstance(models, list):
            raise ValueError("模型白名单必须是模型 ID 的字符串列表")
        for m in models:
            if not isinstance(m, str):
                raise ValueError("模型白名单必须是字符串列表")
            text = m.strip()
            if not text:
                raise ValueError("模型白名单不能包含空字符串")
            cleaned_models.append(text)

    aliases = definition.get("aliases")
    if aliases is None:
        aliases = {}
    if not isinstance(aliases, dict):
        raise ValueError("别名必须是「别名 → 模型 ID」的映射对象")
    # Effective model set for alias validation: the admin-provided list, or the
    # default whitelist when models is omitted (spec 23 §5: an alias value must
    # point at the default model or a user-supplied model id).
    _effective_models = cleaned_models or list(DEFAULT_MODELS)
    cleaned_aliases: dict[str, str] = {}
    for k, v in aliases.items():
        ak = str(k).strip()
        av = str(v).strip()
        if not ak:
            raise ValueError("别名不能为空")
        if not av:
            raise ValueError("别名对应的模型 ID 不能为空")
        if av not in _effective_models:
            raise ValueError(f"别名「{ak}」指向的模型「{av}」不在模型白名单中")
        cleaned_aliases[ak] = av

    env_api_key = definition.get("env_api_key")
    if env_api_key is not None:
        env_api_key = str(env_api_key).strip()
        if env_api_key and not _ENV_NAME_RE.match(env_api_key):
            raise ValueError(
                "环境变量名需以 CB_ 开头,其余只允许大写字母/数字/下划线/连字符(如 CB_MY_KEY)"
            )


# Persistence ---------------------------------------------------------------


def list_definitions() -> list[dict]:
    """All custom-channel definitions currently persisted. Each entry is the
    stored dict; UI/ admin routes should NOT echo `api_key` (it isn't kept
    here, by design — keys live in `accounts`)."""
    raw = db.get_setting(SETTINGS_KEY, None)
    if isinstance(raw, list):
        out: list[dict] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
        return out
    return []


def get_definition(channel_id: str) -> Optional[dict]:
    cid = str(channel_id or "").strip()
    if not cid:
        return None
    for entry in list_definitions():
        if str(entry.get("id") or "").strip() == cid:
            return entry
    return None


def save_definitions(definitions: list[dict]) -> None:
    """Persist the whole list. Validation is the caller's responsibility
    (admin routes validate per-definition before calling this)."""
    db.set_setting(SETTINGS_KEY, list(definitions or []))


def upsert_definition(definition: dict) -> dict:
    """Insert (or replace if id exists) and invalidate the provider cache.

    Returns the stored definition (with created_at filled in for new ids).
    Caller is responsible for validate_definition() first.
    """
    definitions = list_definitions()
    cid = str(definition.get("id") or "").strip()
    now = int(time.time())
    new_entry = dict(definition)
    replaced = False
    for i, existing in enumerate(definitions):
        if str(existing.get("id") or "").strip() == cid:
            # Preserve created_at from the original definition.
            new_entry["created_at"] = existing.get("created_at") or now
            new_entry["updated_at"] = now
            definitions[i] = new_entry
            replaced = True
            break
    if not replaced:
        new_entry["created_at"] = definition.get("created_at") or now
        new_entry["updated_at"] = now
        definitions.append(new_entry)
    save_definitions(definitions)
    invalidate_cache(cid)
    return new_entry


def delete_definition(channel_id: str) -> bool:
    """Remove a definition, purge every per-channel settings residual, and
    invalidate the cache. Returns False if not present (caller → 404).

    Purged residuals (so no page/dropdown keeps referencing a dead channel):
      * <cid>.models / <cid>.aliases   — 模型配置页覆盖键
      * <cid>.credit_rate / <cid>.reasoning — 同页倍率/思考档位
      * unified_models entries mapping to this channel
      * enabled_channels / channel_order membership
    """
    cid = str(channel_id or "").strip()
    definitions = list_definitions()
    out = [d for d in definitions if str(d.get("id") or "").strip() != cid]
    if len(out) == len(definitions):
        return False
    save_definitions(out)
    _purge_channel_settings(cid)
    invalidate_cache(cid)
    return True


def _purge_channel_settings(cid: str) -> None:
    """Delete every settings residual tied to a custom channel id (see
    delete_definition). Best-effort per key: a missing key is not an error."""
    cid = str(cid or "").strip()
    if not cid:
        return
    # 1) per-channel overrides written by the 模型配置 page (workbuddy keeps
    #    legacy key names, but workbuddy is a builtin and never hits this).
    for key in (f"{cid}.models", f"{cid}.aliases", f"{cid}.credit_rate", f"{cid}.reasoning"):
        try:
            db.delete_setting(key)
        except Exception:
            pass
    # 2) unified-model mappings referencing this channel: drop the channel
    #    from each entry; remove entries left with no mapping at all.
    try:
        raw = db.get_setting("unified_models", None)
        if isinstance(raw, list):
            changed = False
            cleaned: list = []
            for entry in raw:
                if not isinstance(entry, dict):
                    cleaned.append(entry)
                    continue
                mappings = entry.get("mappings")
                if isinstance(mappings, dict) and cid in mappings:
                    entry["mappings"] = {k: v for k, v in mappings.items() if k != cid}
                    changed = True
                    if not entry["mappings"]:
                        continue  # entry no longer maps anywhere → drop it
                cleaned.append(entry)
            if changed:
                db.set_setting("unified_models", cleaned)
    except Exception:
        pass
    # 3) enabled/order membership so the channel disappears from the UI order.
    try:
        enabled = db.get_setting("enabled_channels", None)
        if isinstance(enabled, list) and cid in enabled:
            db.set_setting("enabled_channels", [x for x in enabled if x != cid])
        order = db.get_setting("channel_order", None)
        if isinstance(order, list) and cid in order:
            db.set_setting("channel_order", [x for x in order if x != cid])
    except Exception:
        pass


def reserved_ids(exclude_id: str | None = None) -> set[str]:
    """Set of channel ids the new definition must not collide with.

    Includes built-in ids from providers.protocol.KNOWN_CHANNEL_IDS and ids
    of other custom definitions (excluding the one being edited, when given).
    """
    from providers.protocol import KNOWN_CHANNEL_IDS

    ids = {str(c) for c in KNOWN_CHANNEL_IDS}
    for entry in list_definitions():
        cid = str(entry.get("id") or "").strip()
        if not cid:
            continue
        if cid == exclude_id:
            continue
        ids.add(cid)
    return ids


# Provider cache (built per definition; invalidated on CRUD) -----------------

_custom_cache: dict[str, OpenAICompatProvider] = {}


def build_provider(definition: dict) -> OpenAICompatProvider:
    """Materialise an OpenAICompatProvider for a definition (no side effects)."""
    return OpenAICompatProvider(definition)


def get_provider(channel_id: str) -> Optional[OpenAICompatProvider]:
    """Return the cached provider for a custom channel id, or None if not
    defined. Re-uses the in-memory cache; rebuilt on invalidate_cache()."""
    cid = str(channel_id or "").strip()
    if not cid:
        return None
    cached = _custom_cache.get(cid)
    if cached is not None:
        return cached
    definition = get_definition(cid)
    if definition is None:
        return None
    provider = build_provider(definition)
    _custom_cache[cid] = provider
    return provider


def invalidate_cache(channel_id: str | None = None) -> None:
    """Clear one or all cached providers.

    Called from save / delete paths so that the next request rebuilds the
    Provider from the freshly persisted definition (D3: zero-restart, hot
    reload). Passing None clears everything.
    """
    if channel_id is None:
        _custom_cache.clear()
        return
    cid = str(channel_id or "").strip()
    _custom_cache.pop(cid, None)


# Seed migration (D5) --------------------------------------------------------
# gmi / bailian used to ship as full provider packages. They are now data
# definitions; the seed below recreates them on first boot so existing
# accounts / enabled_channels / model whitelists keep working without code.
# Idempotent: runs only when the `custom_channels` settings key has NEVER been
# written (None). Subsequent boots short-circuit because the key now exists.

_SEED_BASE_URL_OVERRIDES: dict[str, str] = {
    # key here matches the `channel_hosts` field names in the legacy
    # host_override whitelist. Only `base_url` ever applied to gmi/bailian.
    "gmi": "base_url",
    "bailian": "base_url",
}


def _absent() -> bool:
    """True iff the custom_channels settings key has never been written."""
    return db.get_setting(SETTINGS_KEY, None) is None


def seed_initial_definitions() -> bool:
    """Write the gmi / bailian seed definitions when the settings key is
    absent. Returns True iff a write happened.

    If `channel_hosts` contains `gmi.base_url` / `bailian.base_url` overrides
    (legacy per-channel host overrides from before this migration), they are
    folded into the seed `base_url` and the override entry is cleared.

    Idempotent: re-runs detect that the settings key now exists and exit.
    """
    if not _absent():
        return False

    # Pull legacy channel_hosts overrides so admins who pinned a mirror keep
    # that mirror after the migration.
    raw_hosts = db.get_setting("channel_hosts", {}) or {}
    host_overrides: dict[str, str] = {}
    if isinstance(raw_hosts, dict):
        for cid, field in _SEED_BASE_URL_OVERRIDES.items():
            entry = raw_hosts.get(cid)
            if isinstance(entry, dict):
                value = str(entry.get(field) or "").strip()
                if value:
                    host_overrides[cid] = value.rstrip("/")

    seeds: list[dict] = []
    for cid in ("gmi", "bailian"):
        if cid == "gmi":
            base_url = host_overrides.get("gmi", "https://api.gmi-serving.com/v1")
            seeds.append({
                "id": "gmi",
                "display_name": "GMI Cloud",
                "base_url": base_url,
                "models": ["zai-org/GLM-5.3-Flash"],
                "aliases": {
                    "auto": "zai-org/GLM-5.3-Flash",
                    "gmi": "zai-org/GLM-5.3-Flash",
                    "glm-5.2": "zai-org/GLM-5.3-Flash",
                    "gmi-flash": "zai-org/GLM-5.3-Flash",
                    "gmi/auto": "zai-org/GLM-5.3-Flash",
                },
                "env_api_key": "CB_GMI_API_KEY",
                "source": "seed",
            })
        elif cid == "bailian":
            base_url = host_overrides.get(
                "bailian",
                "https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            )
            seeds.append({
                "id": "bailian",
                "display_name": "阿里百炼 Bailian",
                "base_url": base_url,
                "models": ["qwen-plus"],
                "aliases": {
                    "auto": "qwen-plus",
                    "bailian": "qwen-plus",
                    "bailian/auto": "qwen-plus",
                },
                "env_api_key": "CB_BAILIAN_API_KEY",
                "source": "seed",
            })

    now = int(time.time())
    for entry in seeds:
        entry["created_at"] = now
        entry["updated_at"] = now
    save_definitions(seeds)

    # Strip the legacy overrides so a future settings save doesn't override
    # the seed base_url.
    if host_overrides and isinstance(raw_hosts, dict):
        for cid in list(raw_hosts.keys()):
            if cid in {"gmi", "bailian"}:
                raw_hosts.pop(cid, None)
        if raw_hosts:
            db.set_setting("channel_hosts", raw_hosts)
        else:
            db.delete_setting("channel_hosts")

    invalidate_cache(None)
    return True