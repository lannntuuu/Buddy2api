"""Per-channel upstream host override, read from the settings table.

Admin can point a channel at a mirror / internal proxy without touching
provider constants. Unset fields fall back to the provider's default.

Note: gmi and bailian used to live here; they moved into the data-driven
custom-channel definitions (`custom_channels` settings key) — change base
URL by editing the channel definition, not via `channel_hosts`.
"""
from __future__ import annotations
from storage import database as db

# Field whitelist per channel. Only these keys are accepted in the
# `channel_hosts` settings blob; anything else is rejected by the admin
# route. Phase A covers single-host channels; Phase B adds multi-host
# channels (qclaw, traesolo, traework).
CHANNEL_HOST_FIELDS: dict[str, tuple[str, ...]] = {
    "qwenwork": ("gateway",),
    "qclaw": ("jprx_gateway", "aizone_base"),
    "traesolo": ("oauth_host", "console_host", "agent_host"),
    "traework": ("agent_host", "ug_host"),
}

def channel_host(channel_id: str, field: str, default: str) -> str:
    """Resolve an upstream host for a channel, honouring admin override.

    field must be one of CHANNEL_HOST_FIELDS[channel_id]. Returns the
    admin-configured value when set, otherwise `default`.
    """
    raw = db.get_setting("channel_hosts", {}) or {}
    mapping = raw.get(channel_id) if isinstance(raw, dict) else None
    if isinstance(mapping, dict):
        val = str(mapping.get(field) or "").strip().rstrip("/")
        if val:
            return val
    return default