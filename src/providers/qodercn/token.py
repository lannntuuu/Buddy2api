"""Qoder CN device-token refresh.

Mirrors the QwenWork refresh shape (same `/api/v1/deviceToken/refresh`
endpoint family, `device_token`/`refresh_token` response) against the
Qoder CN openapi host. Body is `{refresh_token}` (no `target` field).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from providers.qodercn.constants import CHANNEL_ID, GATEWAY_HOST, OPENAPI_HOST, USER_AGENT
from providers.host_override import channel_host
from providers.qodercn.store import iso_to_ms, write_refreshed_auth
from storage.http_pool import get_client


class QoderCnAuthError(RuntimeError):
    pass


def openapi_headers(request_id: str = "") -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Request-Id": request_id or f"buddy2api-{int(time.time() * 1000)}",
        "X-Qoder-Remote-Api-Version": "2",
    }


def _openapi_base() -> str:
    return channel_host(CHANNEL_ID, "openapi", OPENAPI_HOST)


def _gateway_base() -> str:
    return channel_host(CHANNEL_ID, "gateway", GATEWAY_HOST)


async def refresh_account(account: dict) -> dict:
    from storage import database as db

    refresh = str(account.get("refresh_token") or "")
    if not refresh:
        raise QoderCnAuthError("QoderCN account has no refresh_token")
    headers = openapi_headers()
    access = str(account.get("access_token") or "")
    if access:
        headers["Authorization"] = f"Bearer {access}"
    body = {"refresh_token": refresh}
    client = get_client()
    response = await client.post(
        f"{_openapi_base()}/api/v1/deviceToken/refresh",
        headers=headers,
        json=body,
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise QoderCnAuthError(f"deviceToken refresh failed: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise QoderCnAuthError("deviceToken refresh returned non-JSON") from exc
    token = data.get("device_token") or data.get("token") or ""
    new_refresh = data.get("refresh_token") or refresh
    if not token:
        raise QoderCnAuthError("refresh response missing device_token")
    expires_at = iso_to_ms(data.get("expires_at")) or data.get("expires_in", 0)
    if isinstance(expires_at, (int, float)) and expires_at < 10_000_000_000:
        expires_at = int((time.time() + float(expires_at)) * 1000)
    elif isinstance(expires_at, (int, float)):
        expires_at = int(expires_at)
    refresh_expires_at = iso_to_ms(data.get("refresh_token_expires_at")) or data.get(
        "refresh_token_expires_in", 0
    )
    if isinstance(refresh_expires_at, (int, float)) and refresh_expires_at < 10_000_000_000:
        refresh_expires_at = int((time.time() + float(refresh_expires_at)) * 1000)
    elif isinstance(refresh_expires_at, (int, float)):
        refresh_expires_at = int(refresh_expires_at)
    patch = {
        "access_token": token,
        "refresh_token": new_refresh,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "status": "active",
    }
    db.update_account(account["id"], patch)
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    auth_path = extra.get("auth_path")
    if auth_path:
        try:
            await asyncio.to_thread(write_refreshed_auth, Path(auth_path), patch)
        except Exception:
            pass
    fresh = db.get_account(account["id"])
    if not fresh:
        raise QoderCnAuthError("account disappeared after refresh")
    return fresh