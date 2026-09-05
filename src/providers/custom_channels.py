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

# Definition validation ------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENV_NAME_RE = re.compile(r"^CB_[A-Z0-9_]+$")

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_https(url: str) -> bool:
    return url.startswith("https://")


def _is_loopback_http(url: str) -> bool:
    """True iff url is http://127.0.0.1[:port] or http://localhost[:port] (D7
    allows plaintext only for loopback debugging)."""
    if not url.startswith("http://"):
        return False
    rest = url[len("http://"):]
    # Strip path/query.
    host_part = rest.split("/", 1)[0]
    # Strip credentials (not supported, but be defensive).
    host_part = host_part.split("@", 1)[-1]
    host = host_part.split(":", 1)[0].lower()
    return host in _LOOPBACK_HOSTS


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
    if not isinstance(definition, dict):
        raise ValueError("definition must be a JSON object")

    cid = str(definition.get("id") or "").strip()
    if not cid:
        raise ValueError("id is required")
    if not _SLUG_RE.match(cid):
        raise ValueError(
            "id must match ^[a-z][a-z0-9_-]{0,31}$ "
            "(start with letter; lowercase alnum / '-' / '_'; max 32 chars)"
        )
    if cid != exclude_id and cid in reserved_ids:
        raise ValueError(f"channel id '{cid}' is already in use")

    display_name = str(definition.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("display_name is required")
    if len(display_name) > 40:
        raise ValueError("display_name must be ≤ 40 characters")

    base_url = str(definition.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("base_url is required")
    if not (_is_https(base_url) or _is_loopback_http(base_url)):
        raise ValueError(
            "base_url must be https:// or http://127.0.0.1[:port]/http://localhost[:port]"
        )

    models = definition.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("models must be a non-empty array of model id strings")
    cleaned_models: list[str] = []
    for m in models:
        if not isinstance(m, str):
            raise ValueError("models must be an array of strings")
        text = m.strip()
        if not text:
            raise ValueError("models must be an array of non-empty strings")
        cleaned_models.append(text)
    if not cleaned_models:
        raise ValueError("models must be a non-empty array of non-empty strings")

    aliases = definition.get("aliases")
    if aliases is None:
        aliases = {}
    if not isinstance(aliases, dict):
        raise ValueError("aliases must be an object mapping alias -> model id")
    cleaned_aliases: dict[str, str] = {}
    for k, v in aliases.items():
        ak = str(k).strip()
        av = str(v).strip()
        if not ak:
            raise ValueError("alias keys must be non-empty")
        if not av:
            raise ValueError("alias values must be non-empty")
        if av not in cleaned_models:
            raise ValueError(
                f"alias '{ak}' points to '{av}' which is not in the models list"
            )
        cleaned_aliases[ak] = av

    env_api_key = definition.get("env_api_key")
    if env_api_key is not None:
        env_api_key = str(env_api_key).strip()
        if env_api_key and not _ENV_NAME_RE.match(env_api_key):
            raise ValueError(
                "env_api_key must match ^CB_[A-Z0-9_]+$ (e.g. CB_MY_KEY)"
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
    """Remove a definition and invalidate the cache. Returns False if not
    present (after validation caller treats as 404/409)."""
    cid = str(channel_id or "").strip()
    definitions = list_definitions()
    out = [d for d in definitions if str(d.get("id") or "").strip() != cid]
    if len(out) == len(definitions):
        return False
    save_definitions(out)
    invalidate_cache(cid)
    return True


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