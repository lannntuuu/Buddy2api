"""Channel registry. WorkBuddy, QClaw, QwenWork, TraeWork, and Trae SOLO are
enabled by default. GMI and Bailian ship as opt-in data-driven custom
channels: their definitions live in the `custom_channels` settings key
(seeded by `providers.custom_channels.seed_initial_definitions()` on first
boot). User-defined custom channels use the same machinery."""

from __future__ import annotations

import os

from providers.protocol import (
    KNOWN_CHANNEL_IDS,
    ChannelId,
    Provider,
)
from providers.qclaw import PROVIDER as QCLAW_PROVIDER
from providers.qwenwork import PROVIDER as QWENWORK_PROVIDER
from providers.traework import PROVIDER as TRAEWORK_PROVIDER
from providers.traesolo import PROVIDER as TRAESOLO_PROVIDER
from providers.workbuddy import PROVIDER as WORKBUDDY_PROVIDER

# Canonical default order when the operator has neither set the env var nor
# saved anything via the Web UI. workbuddy is intentionally first.
DEFAULT_PROVIDER_IDS: tuple[str, ...] = ("workbuddy", "qclaw", "qwenwork", "traework", "traesolo")

# Opt-in ids (gmi / bailian seed definitions + future custom channels). They
# ship with the gateway but are NEVER auto-enabled: the admin must opt in via
# CB_GATEWAY_PROVIDERS or the admin UI toggle.
OPT_IN_PROVIDER_IDS: tuple[str, ...] = ("gmi", "bailian")

_LOADED: dict[str, Provider] = {
    "workbuddy": WORKBUDDY_PROVIDER,
    "qclaw": QCLAW_PROVIDER,
    "qwenwork": QWENWORK_PROVIDER,
    "traework": TRAEWORK_PROVIDER,
    "traesolo": TRAESOLO_PROVIDER,
}

# Settings-table keys for UI-driven overrides.
ENABLED_DB_KEY = "enabled_channels"
ORDER_DB_KEY = "channel_order"

# Locked-first channel: never movable in the user's ordering. The DB can hold
# whatever; this is the only post-condition this module enforces.
LOCKED_FIRST = "workbuddy"


def _lock_first(ordered: list[str]) -> list[str]:
    """Re-order so LOCKED_FIRST sits at index 0. Stable on the rest."""
    if not ordered:
        return []
    if ordered[0] == LOCKED_FIRST:
        return list(ordered)
    head = [LOCKED_FIRST] if LOCKED_FIRST in ordered else []
    rest = [c for c in ordered if c != LOCKED_FIRST]
    return head + rest


def _custom_definition_ids() -> list[str]:
    """Cached lookup of custom-channel ids currently in the settings store."""
    try:
        from providers import custom_channels
        return [str(d.get("id") or "") for d in custom_channels.list_definitions() if d.get("id")]
    except Exception:
        # settings table may not be initialised yet (very early boot)
        return []


def known_channel_ids() -> tuple[str, ...]:
    """All channel ids known to the gateway: built-ins union custom.

    Called on every read; cheap because the settings lookup is a single
    sqlite row. The result is the runtime source of truth for "which ids
    are addressable" — built-in `KNOWN_CHANNEL_IDS` in protocol.py stays
    as documentation only.
    """
    seen: list[str] = list(KNOWN_CHANNEL_IDS)
    for cid in _custom_definition_ids():
        if cid not in seen:
            seen.append(cid)
    return tuple(seen)


def _known_set() -> frozenset[str]:
    return frozenset(known_channel_ids())


def _read_db_list(key: str) -> list[str] | None:
    try:
        from storage import database as _db
        value = _db.get_setting(key, None)
    except Exception:
        return None
    if isinstance(value, list) and value:
        valid = _known_set()
        cleaned = [str(x).strip() for x in value if str(x).strip()]
        cleaned = [c for c in cleaned if c in valid]
        return cleaned or None
    return None


def _parse_enabled() -> list[str]:
    """Resolve the canonical enabled-channel list, ordered for display.

    Priority order:
      1. env CB_GATEWAY_PROVIDERS (preserves written order; lock first)
      2. db enabled_channels + db channel_order (UI-driven)
      3. DEFAULT_PROVIDER_IDS (fresh install)

    The returned list is also the SINGLE source of truth for "what order do
    channels appear in" across the whole admin UI and `/v1/models`. Every
    endpoint that needs to render channel lists must call this.
    """
    raw = (os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip()
    valid = _known_set()
    if raw:
        parts = [item.strip() for item in raw.split(",") if item.strip()]
        parts = [p for p in parts if p in valid]
        if not parts:
            return list(DEFAULT_PROVIDER_IDS)
        return _lock_first(parts)

    enabled = _read_db_list(ENABLED_DB_KEY)
    if not enabled:
        # No env, no UI write → alphabetical fallback per the contract: the
        # first time the user touches the order, the saved order takes over.
        return _lock_first(sorted(valid))

    # Apply db-persisted ordering if present, otherwise preserve the enabled
    # list as-written.
    order = _read_db_list(ORDER_DB_KEY)
    if not order:
        return _lock_first(enabled)

    rank = {c: i for i, c in enumerate(order)}
    # Stable sort: rank-known first (preserving their db order), then unknown
    # in their enabled-list position.
    return _lock_first(
        sorted(
                enabled,
                key=lambda c: (rank.get(c, len(order)), enabled.index(c)),
            )
    )


def env_locked() -> bool:
    """True iff CB_GATEWAY_PROVIDERS is set, in which case the UI toggle is read-only."""
    return bool((os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip())


def set_enabled_channels(ids: list[str], order: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Persist a new enabled-channel list (and optional order) to the settings table.

    `ids` is the set of channels the admin wants enabled; `order` (optional) is
    the desired display order across the whole admin UI. When `order` is given,
    any channel in `ids` missing from `order` is appended in its `ids` position
    so the two stay consistent. `workbuddy` is forced to the top.

    Returns (enabled_ids, ordered_full) where enabled_ids is the explicit
    enabled set (subset of ordered_full) and ordered_full is the display
    order for every known channel.
    """
    valid = _known_set()
    cleaned_ids = [str(x).strip() for x in (ids or []) if str(x).strip()]
    if not cleaned_ids:
        raise ValueError("At least one channel id is required")
    unknown = [c for c in cleaned_ids if c not in valid]
    if unknown:
        raise ValueError(f"Unknown channel id(s): {', '.join(unknown)}")
    cleaned_ids = [c for c in cleaned_ids if c in valid]
    if not cleaned_ids:
        raise ValueError("At least one known channel id is required")
    # workbuddy is always enabled; force it into the enabled set.
    if LOCKED_FIRST not in cleaned_ids:
        cleaned_ids.append(LOCKED_FIRST)

    if order:
        cleaned_order = [str(x).strip() for x in order if str(x).strip()]
        cleaned_order = [c for c in cleaned_order if c in valid]
    else:
        cleaned_order = list(cleaned_ids)

    # Make sure every enabled channel is present in the order (append unknowns
    # in their ids order), then lock workbuddy to the front.
    for c in cleaned_ids:
        if c not in cleaned_order:
            cleaned_order.append(c)
    ordered = _lock_first(cleaned_order)

    from storage import database as _db  # lazy for import safety

    # enabled_channels is the set the admin explicitly turned on (NOT the
    # display order — that lives in channel_order). Setting `ED == ordered` was
    # the bug that turned "未勾选的也变成勾选了".
    _db.set_setting(ENABLED_DB_KEY, cleaned_ids)
    _db.set_setting(ORDER_DB_KEY, ordered)
    return cleaned_ids, ordered


def get_channel_order() -> list[str]:
    """Public read of the display order (always includes workbuddy first)."""
    return _parse_enabled()


def enabled_provider_ids() -> list[str]:
    return _parse_enabled()


def is_known_channel(channel: str) -> bool:
    return channel in _known_set()


def is_channel_enabled(channel: str) -> bool:
    return channel in enabled_provider_ids()


def get_provider(channel: str) -> Provider | None:
    """Resolve a provider by id. Two-level lookup:

      1. built-in `_LOADED` registry (gmi / bailian / qclaw / ...)
      2. custom OpenAI-compat providers materialised from the
         `custom_channels` settings key (lazy; cached per definition)

    Returns None when the id is unknown or the channel is not currently
    enabled (custom channels are not auto-enabled; the admin must enable
    them via env or DB just like opt-in built-ins).
    """
    if channel not in enabled_provider_ids():
        return None
    built_in = _LOADED.get(channel)
    if built_in is not None:
        return built_in
    try:
        from providers import custom_channels
        return custom_channels.get_provider(channel)
    except Exception:
        return None


def register_provider(provider: Provider) -> None:
    """Test helper to install a stub channel."""
    _LOADED[provider.id] = provider