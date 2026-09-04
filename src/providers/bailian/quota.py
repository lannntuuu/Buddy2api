"""Bailian quota probe.

The Bailian compatible-mode (MaaS dedicated instance) exposes no public
per-key balance endpoint we can rely on. The contract still needs to exist so
the Dashboard renders the channel.

ponytail: returns QuotaSnapshot(unsupported=True) instead of fabricating a
balance — the user explicitly chose this provider, so seeing "unsupported" in
the UI tells them the limitation rather than a fake number. Upgrade when Bailian
ships a balance endpoint.
"""

from __future__ import annotations

from providers.bailian.constants import CHANNEL_ID
from providers.protocol import QuotaSnapshot


async def fetch_quota(account: dict) -> QuotaSnapshot:
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=int(account.get("id") or 0),
        unit="credit",
        remaining=None,
        unsupported=True,
        message="bailian: per-account credit balance endpoint not available; usage tracked via request logs",
        extra={},
    )


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    return {
        "ok": False,
        "channel": CHANNEL_ID,
        "account_id": int(account.get("id") or 0),
        "enable": False,
        "already_claimed": False,
        "message": "bailian has no check-in endpoint",
    }


async def claim_checkin(account: dict) -> dict:
    return await fetch_checkin(account)
