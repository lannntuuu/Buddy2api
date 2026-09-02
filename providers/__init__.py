"""Channel registry. WorkBuddy, QClaw, QwenWork, and TraeWork are enabled by default."""

from __future__ import annotations

import os

from providers.protocol import (
    KNOWN_CHANNEL_IDS,
    KNOWN_CHANNEL_SET,
    ChannelId,
    Provider,
)
from providers.qclaw import PROVIDER as QCLAW_PROVIDER
from providers.qwenwork import PROVIDER as QWENWORK_PROVIDER
from providers.traework import PROVIDER as TRAEWORK_PROVIDER
from providers.traesolo import PROVIDER as TRAESOLO_PROVIDER
from providers.workbuddy import PROVIDER as WORKBUDDY_PROVIDER
from providers.gmi import PROVIDER as GMI_PROVIDER

# Canonical default order when the operator has neither set the env var nor
# saved anything via the Web UI. workbuddy is intentionally first.
DEFAULT_PROVIDER_IDS: tuple[str, ...] = ("workbuddy", "qclaw", "qwenwork", "traework", "traesolo")

# Extra providers that ship with the gateway but are NOT on by default —
# opt in via CB_GATEWAY_PROVIDERS (e.g. "workbuddy,qclaw,gmi"). ponytail:
# keeping these opt-in avoids surprising users who never asked for them.
OPT_IN_PROVIDER_IDS: tuple[str, ...] = ("gmi",)

_LOADED: dict[str, Provider] = {
    "workbuddy": WORKBUDDY_PROVIDER,
    "qclaw": QCLAW_PROVIDER,
    "qwenwork": QWENWORK_PROVIDER,
    "traework": TRAEWORK_PROVIDER,
    "traesolo": TRAESOLO_PROVIDER,
    "gmi": GMI_PROVIDER,
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


def _read_db_list(key: str) -> list[str] | None:
    try:
        from storage import database as _db
        value = _db.get_setting(key, None)
    except Exception:
        return None
    if isinstance(value, list) and value:
        cleaned = [str(x).strip() for x in value if str(x).strip()]
        cleaned = [c for c in cleaned if c in KNOWN_CHANNEL_SET]
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
    if raw:
        parts = [item.strip() for item in raw.split(",") if item.strip()]
        parts = [p for p in parts if p in KNOWN_CHANNEL_SET]
        if not parts:
            return list(DEFAULT_PROVIDER_IDS)
        return _lock_first(parts)

    enabled = _read_db_list(ENABLED_DB_KEY)
    if not enabled:
        # No env, no UI write → alphabetical fallback per the contract: the
        # first time the user touches the order, the saved order takes over.
        return _lock_first(sorted(KNOWN_CHANNEL_SET))

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
    cleaned_ids = [str(x).strip() for x in (ids or []) if str(x).strip()]
    if not cleaned_ids:
        raise ValueError("At least one channel id is required")
    unknown = [c for c in cleaned_ids if c not in KNOWN_CHANNEL_SET]
    if unknown:
        raise ValueError(f"Unknown channel id(s): {', '.join(unknown)}")
    cleaned_ids = [c for c in cleaned_ids if c in KNOWN_CHANNEL_SET]
    if not cleaned_ids:
        raise ValueError("At least one known channel id is required")
    # workbuddy is always enabled; force it into the enabled set.
    if LOCKED_FIRST not in cleaned_ids:
        cleaned_ids.append(LOCKED_FIRST)

    if order:
        cleaned_order = [str(x).strip() for x in order if str(x).strip()]
        cleaned_order = [c for c in cleaned_order if c in KNOWN_CHANNEL_SET]
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
    return channel in KNOWN_CHANNEL_SET


def is_channel_enabled(channel: str) -> bool:
    return channel in enabled_provider_ids()


def get_provider(channel: str) -> Provider | None:
    if channel not in enabled_provider_ids():
        return None
    return _LOADED.get(channel)


def register_provider(provider: Provider) -> None:
    """Test helper to install a stub channel."""
    _LOADED[provider.id] = provider