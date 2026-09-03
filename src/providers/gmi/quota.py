"""GMI quota probe.

We don't know of a public "remaining credits" endpoint on api.gmi-serving.com
yet (their /v1/models emits a `pricing` block per-model, but no per-key balance
API). The contract still needs to exist so the Dashboard renders the channel.

ponytail: returns QuotaSnapshot(unsupported=True) instead of fabricating a
balance — the user explicitly chose this provider, so seeing "unsupported" in
the UI tells them the limitation rather than a fake number. Upgrade when gmi
ships a balance endpoint.
"""

from __future__ import annotations

from providers.gmi.constants import CHANNEL_ID
from providers.protocol import QuotaSnapshot


async def fetch_quota(account: dict) -> QuotaSnapshot:
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=int(account.get("id") or 0),
        unit="credit",
        remaining=None,
        unsupported=True,
        message="gmi: per-account credit balance endpoint not available; usage tracked via request logs",
        extra={},
    )


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    return {
        "ok": False,
        "channel": CHANNEL_ID,
        "account_id": int(account.get("id") or 0),
        "enable": False,
        "already_claimed": False,
        "message": "gmi has no check-in endpoint",
    }


async def claim_checkin(account: dict) -> dict:
    return await fetch_checkin(account)