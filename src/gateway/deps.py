"""Shared dependencies and helpers for the gateway routers.

This module centralises:
- Environment/config state (admin token, rate limits, CORS)
- Auth helpers (`_check_admin`, `_check_client_auth`, quota reservation)
- Request utilities (`_read_json`, `_read_json_object`)
- Client identification helpers (`_detect_client`, `_client_version`)
- Misc helpers used by both the V1 and admin routers

`gateway/server.py` re-exports the symbols used by tests so existing
imports (`server._check_client_auth`, `server.admin_provider_model_usage`,
etc.) keep working after the P2 split.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from collections import deque
from datetime import date, timedelta
from typing import Optional

from fastapi import Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from storage import database as db
import providers
from providers.protocol import KNOWN_CHANNEL_SET  # noqa: F401  (kept for legacy callers)
from gateway.version import VERSION


# ============================================================
# Environment helpers
# ============================================================

def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cors_origins() -> list[str]:
    value = os.environ.get(
        "CB_GATEWAY_CORS_ORIGINS",
        "http://127.0.0.1:8787,http://localhost:8787",
    )
    return [origin.strip() for origin in value.split(",") if origin.strip()]


# ============================================================
# State (mutated by server.main before uvicorn starts)
# ============================================================

ADMIN_TOKEN: str = ""
ALLOW_NO_ADMIN_AUTH = False
ALLOW_UNAUTHENTICATED_API = _env_flag("CB_GATEWAY_ALLOW_UNAUTHENTICATED_API", False)
ADMIN_COOKIE_NAME = "cb_gw_admin_token"
MAX_BODY_BYTES = max(1024, _env_int("CB_GATEWAY_MAX_BODY_BYTES", 10 * 1024 * 1024))
_CURRENT_REQUEST: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "current_request", default=None
)


# User-Agent version extraction. Tries <name>/<semver> first (may carry
# pre-release suffixes like -rc/-beta/-alpha), then common version forms.
# Pre-release suffixes are preserved so we can match GitHub tags (e.g. 0.1.1-rc.2).
_UA_VERSION_PATTERNS = (
    re.compile(r"^[^/\s]+/([0-9]+(?:\.[0-9]+){1,3}(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)"),
    re.compile(r"(?:^|[^0-9])([0-9]+(?:\.[0-9]+){1,3}(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)"),
    re.compile(r"(?:^|\W)v([0-9]+(?:\.[0-9]+){1,3}(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)"),
)
_UA_FALLBACK_MAX = 32


# ============================================================
# Client identification (for log enrichment only, never auth)
# ============================================================

def _detect_client(request: Request, api_key_info: dict | None) -> str | None:
    """Identify the caller for log analysis (DSH vs other clients).

    Priority order: explicit harness headers, then API key client_type,
    finally a User-Agent prefix.
    """
    headers = request.headers
    if headers.get("x-deepseek-harness-user-id") or headers.get(
        "x-deepseek-harness-session-id"
    ):
        return "dsh"
    if headers.get("x-openai-client"):
        return str(headers.get("x-openai-client"))[:60]
    if api_key_info and api_key_info.get("client_type") == "codex":
        return "codex"
    ua = headers.get("user-agent") or ""
    ua = ua.strip()
    if not ua:
        return None
    low = ua.lower()
    if "deepseek-harness" in low:
        return "dsh"
    if "zcode" in low:
        return "zcode"
    if low.startswith("openai") or "openai" in low and "python" in low:
        return "openai-sdk"
    if "curl" in low:
        return "curl"
    if "python" in low:
        return "python"
    first = ua.split("/")[0].strip()
    if first:
        return first[:32]
    return ua[:32]


def _client_version(request: Request) -> str | None:
    """Extract the caller's version from User-Agent for log diagnostics.

    Tries `<name>/2.5.1` then any `x.y.z` then `v2.5.1`. Falls back to a
    short prefix of the first UA token. Only short versions are stored,
    not the whole UA.
    """
    ua = request.headers.get("user-agent")
    if not ua:
        return None
    ua = ua.strip()
    if not ua:
        return None
    for pattern in _UA_VERSION_PATTERNS:
        match = pattern.search(ua)
        if match and match.group(1):
            return match.group(1)[:24]
    first = ua.split()[0] if ua.split() else ua
    first = first.strip("/").strip()
    return first[:_UA_FALLBACK_MAX] or None


def _stamp_client_info(request: Request, api_key_info: dict | None) -> None:
    """Write the caller label and version into api_key_info for log enrichment.

    Record-only; never participates in auth, routing, or content handling.
    `client` is a short human-readable tag, `client_version` is the UA
    extracted version (unrelated to error info).
    """
    if not api_key_info:
        return
    api_key_info["_client_tag"] = _detect_client(request, api_key_info)
    api_key_info["_client_version"] = _client_version(request)


# ============================================================
# Auth helpers
# ============================================================

def _check_admin(authorization: str | None):
    if ALLOW_NO_ADMIN_AUTH:
        return
    candidates = []
    if authorization:
        parts = authorization.split(" ", 1)
        candidates.append(parts[1] if len(parts) == 2 else parts[0])

    request = _CURRENT_REQUEST.get()
    if request:
        candidates.append(request.cookies.get(ADMIN_COOKIE_NAME, ""))

    # compare_digest on non-ASCII str raises TypeError (turns into 500); only
    # compare bytes that are guaranteed to be ASCII.
    if not any(
        t and secrets.compare_digest(t.encode("utf-8"), ADMIN_TOKEN.encode("utf-8"))
        for t in candidates
    ):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_client_auth(
    authorization: str | None,
    x_api_key: str | None,
    *,
    consume_quota: bool = True,
):
    """Validate a client API key and atomically reserve its daily quota."""
    if not db.has_api_keys():
        if ALLOW_UNAUTHENTICATED_API:
            return None
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "No API keys configured", "type": "server_error"}},
        )

    token = ""
    if x_api_key:
        token = x_api_key
    elif authorization:
        parts = authorization.split(" ", 1)
        token = parts[1] if len(parts) == 2 else parts[0]

    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "API key required", "type": "invalid_request_error"}},
        )

    key_info = db.get_api_key_by_key(token)
    if not key_info:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
        )

    daily_limit = int(key_info.get("daily_limit") or 0)
    if consume_quota and not db.reserve_api_key_request(key_info["id"], daily_limit):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
        )
    return key_info


def _reserve_client_quota(key_info: dict | None):
    if not key_info:
        return
    daily_limit = int(key_info.get("daily_limit") or 0)
    if not db.reserve_api_key_request(key_info["id"], daily_limit):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
        )


def _release_client_quota(key_info: dict | None):
    if not key_info:
        return
    db.release_api_key_request(key_info["id"])


def _validate_key_channel(channel: str) -> str:
    value = str(channel or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="default_channel is required")
    if not providers.is_known_channel(value):
        raise HTTPException(status_code=400, detail=f"Unknown channel '{value}'")
    if not providers.is_channel_enabled(value) or providers.get_provider(value) is None:
        raise HTTPException(status_code=400, detail=f"Channel '{value}' is not enabled")
    return value


def _check_model_access(api_key_info: dict | None, original: str, inner: str, channel: str):
    if not api_key_info or not api_key_info.get("allowed_models"):
        return
    provider = providers.get_provider(channel)
    translated = provider.translate_model(inner) if provider else inner
    allowed = set(api_key_info["allowed_models"])
    candidates = {original, inner, translated, f"{channel}/{inner}"}
    if allowed.isdisjoint(candidates):
        raise HTTPException(
            status_code=403,
            detail={"error": {"message": f"Model '{original}' not allowed for this API key", "type": "invalid_request_error"}},
        )


# ============================================================
# Request body helpers
# ============================================================

async def _read_json(request: Request, *, allow_empty: bool = False):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw and allow_empty:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")


async def _read_json_object(request: Request, *, allow_empty: bool = False) -> dict:
    data = await _read_json(request, allow_empty=allow_empty)
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data


async def _gather_limited(accounts: list[dict], operation, limit: int = 4) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(account: dict):
        async with semaphore:
            return await operation(account)

    return list(await asyncio.gather(*(run(account) for account in accounts)))


# ============================================================
# Rate limiting
# ============================================================

# /admin/provider-model-usage sliding window. Buckets are keyed by admin
# credential (Cookie preferred, then Authorization header, then client IP).
# CB_GATEWAY_USAGE_RATE_LIMIT=0 disables.
_USAGE_RATE_LIMIT = max(0, _env_int("CB_GATEWAY_USAGE_RATE_LIMIT", 30))
_USAGE_RATE_WINDOW_S = 60.0
_usage_rate_bucket: dict[str, deque[float]] = {}


def _usage_rate_key() -> str:
    request = _CURRENT_REQUEST.get()
    if request is None:
        return "global"
    for source in (
        request.cookies.get(ADMIN_COOKIE_NAME, ""),
        request.headers.get("authorization"),
    ):
        if source:
            parts = source.split(" ", 1)
            token = parts[1] if len(parts) == 2 else parts[0]
            if token:
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
                return f"token:{digest}"
    client = request.client.host if request.client else "global"
    return f"ip:{client}"


async def _check_usage_rate_limit() -> None:
    if _USAGE_RATE_LIMIT <= 0:
        return
    now = time.monotonic()
    hits = _usage_rate_bucket.setdefault(_usage_rate_key(), deque())
    while hits and now - hits[0] >= _USAGE_RATE_WINDOW_S:
        hits.popleft()
    if len(hits) >= _USAGE_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    hits.append(now)


# --- Admin login rate limiting: sliding window per client IP, 10 fails / 5 min
_LOGIN_FAIL_WINDOW_S = 300.0
_LOGIN_FAIL_LIMIT = 10
_login_failures: dict[str, deque[float]] = {}


def _record_login_failure(request: Request) -> None:
    now = time.monotonic()
    key = request.client.host if request.client else "unknown"
    fails = _login_failures.setdefault(key, deque())
    while fails and now - fails[0] >= _LOGIN_FAIL_WINDOW_S:
        fails.popleft()
    fails.append(now)


def _check_login_rate(request: Request) -> None:
    now = time.monotonic()
    key = request.client.host if request.client else "unknown"
    fails = _login_failures.get(key)
    if not fails:
        return
    while fails and now - fails[0] >= _LOGIN_FAIL_WINDOW_S:
        fails.popleft()
    if len(fails) >= _LOGIN_FAIL_LIMIT:
        raise HTTPException(status_code=429, detail="Too many failed logins, try again later")


# ============================================================
# File / IO helpers
# ============================================================

def _atomic_write(path, content: str | bytes, mode: int = 0o600):
    """Write atomically via a sibling tmp file, then chmod and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8") if isinstance(content, str) else content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# ============================================================
# Misc helpers used by /admin/provider-model-usage
# ============================================================

def _usage_date_bounds(days: int | None, start_date: str | None, end_date: str | None):
    """Translate days / start_date+end_date to a unix-second window.

    `days` and `start_date` are mutually exclusive. With only `end_date`,
    the start defaults to 89 days back (aligned to the default log
    retention window).
    """
    import datetime as _dt

    def _parse(text: str, field: str) -> _dt.date:
        try:
            return _dt.date.fromisoformat(text.strip())
        except (AttributeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be an ISO date (YYYY-MM-DD)",
            ) from exc

    if days is not None and start_date is not None:
        raise HTTPException(
            status_code=400, detail="days and start_date are mutually exclusive"
        )

    today = _dt.date.today()
    if days is not None:
        if days <= 0:
            raise HTTPException(status_code=400, detail="days must be a positive integer")
        start = today - _dt.timedelta(days=days - 1)
        end = today
    elif start_date is not None:
        start = _parse(start_date, "start_date")
        end = _parse(end_date, "end_date") if end_date is not None else today
        if end < start:
            raise HTTPException(
                status_code=400, detail="end_date must not be before start_date"
            )
    elif end_date is not None:
        end = _parse(end_date, "end_date")
        start = end - _dt.timedelta(days=89)
    else:
        return None, None

    start_ts = int(_dt.datetime.combine(start, _dt.time.min).timestamp())
    end_ts = int(_dt.datetime.combine(end, _dt.time.max).timestamp())
    return start_ts, end_ts


# ============================================================
# Web UI helpers
# ============================================================

def _set_admin_cookie(request: Request, response) -> None:
    if not ADMIN_TOKEN or ALLOW_NO_ADMIN_AUTH:
        return
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        ADMIN_TOKEN,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https"
        or _env_flag("CB_GATEWAY_SECURE_COOKIE"),
        max_age=30 * 24 * 3600,
    )


# ============================================================
# Provider accessors (used by per-channel login/route handlers)
# ============================================================

def _qclaw_provider_helper():
    """Return the QClaw provider or raise a 400 if it is not enabled."""
    provider = providers.get_provider("qclaw")
    if provider is None:
        raise HTTPException(status_code=400, detail="Channel 'qclaw' is not enabled")
    return provider


def _traesolo_provider_helper():
    """Return the Trae SOLO provider or raise a 400 if it is not enabled."""
    provider = providers.get_provider("traesolo")
    if provider is None:
        raise HTTPException(status_code=400, detail="Channel 'traesolo' is not enabled")
    return provider


def _solo_callback_base(request: Request, explicit: str | None) -> str:
    """Resolve the SOLO login callback base URL.

    Order: explicit arg > CB_TRAESOLO_CALLBACK_BASE env > current request URL.
    Local deployments use the request URL (anyone who can reach the console
    can also reach the callback). Remote deployments should set
    CB_TRAESOLO_CALLBACK_BASE to a browser-reachable address.
    """
    value = (explicit or os.environ.get("CB_TRAESOLO_CALLBACK_BASE") or "").strip().rstrip("/")
    if value:
        return value
    return str(request.base_url).rstrip("/")
