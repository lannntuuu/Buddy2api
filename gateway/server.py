"""server.py — Buddy 2 API entry point.

P2 split: this file now only assembles the FastAPI app, registers
middleware, mounts static assets, and `include_router`s the per-domain
submodules. The actual endpoint functions live in `gateway.routers.*`
and the shared helpers in `gateway.deps`. The module-level `app` object
stays so existing imports (`from gateway.server import app`) keep working.

For backwards compatibility with the test suite and any other caller
that reaches into `gateway.server` for helpers, every helper used by
the tests is re-exported from `gateway.deps` at the bottom of this
module. New code should import from `gateway.deps` directly.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
import types
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from storage import database as db
from accounts import auth_manager
from accounts import control_plane
import providers
from gateway import router as gateway_router
from gateway.version import VERSION

logger = logging.getLogger("buddy2api.server")


# ============================================================
# Module attribute mirroring
# ============================================================
# Tests (and the `main()` startup) write to attributes on this module
# (`server.ADMIN_TOKEN = "..."`, `server._USAGE_RATE_LIMIT = 10_000`,
# etc.). The endpoint code, however, lives in `gateway.routers.*` and
# reads those values from `gateway.deps`. To keep the two views in sync
# without copying state on every read, we wrap this module in a tiny
# subclass that mirrors writes for the relevant attributes into
# `gateway.deps`. The set is small and explicit so it stays easy to
# audit.
_MIRRORED_ATTRS = (
    "ADMIN_TOKEN",
    "ALLOW_NO_ADMIN_AUTH",
    "ALLOW_UNAUTHENTICATED_API",
    "_USAGE_RATE_LIMIT",
    "_usage_rate_bucket",
    "_login_failures",
    "_LOGIN_FAIL_LIMIT",
    "_LOGIN_FAIL_WINDOW_S",
)


class _ServerModule(types.ModuleType):
    def __setattr__(self, name: str, value):
        if name in _MIRRORED_ATTRS:
            from gateway import deps as _d
            setattr(_d, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _ServerModule


# ============================================================
# TraeWork background sync loop
# ============================================================

async def _traework_sync_loop() -> None:
    await asyncio.sleep(60)  # delay the first run so startup stays snappy
    while True:
        try:
            res = await control_plane.sync_traework_usage(days=90)
            if res.get("ok"):
                sys.stderr.write(
                    f"[traework-sync] ok days={res.get('synced_days')} "
                    f"sessions={res.get('sessions')} credits={res.get('total_credits')}\n"
                )
            else:
                sys.stderr.write(f"[traework-sync] skipped: {res.get('error')}\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[traework-sync] error: {exc!r}\n")
        await asyncio.sleep(3600)


def _schedule_traework_sync() -> None:
    try:
        asyncio.get_running_loop().create_task(_traework_sync_loop())
    except RuntimeError:
        # No running loop (e.g. in tests or non-asyncio contexts); skip.
        pass


# ============================================================
# FastAPI app assembly
# ============================================================

app = FastAPI(title="Buddy 2 API", version=VERSION)
from gateway import deps as _deps  # imported here so we can pass values to CORS
_CORS_ORIGINS = _deps._cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials="*" not in _CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Api-Key"],
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Static assets (css/js/vendor modules); the index page itself is served by
# the static router at GET /.
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def _request_context(request: Request, call_next):
    """Make the active request visible to helper functions for context."""
    from gateway import deps as _d
    token = _d._CURRENT_REQUEST.set(request)
    try:
        return await call_next(request)
    finally:
        _d._CURRENT_REQUEST.reset(token)


# Bring in the per-domain routers. Order doesn't matter for path
# resolution since each route is unique.
from gateway.routers import v1 as _v1_router
from gateway.routers import admin as _admin_router
from gateway.routers import static_router as _static_router

app.include_router(_v1_router.router_obj)
app.include_router(_admin_router.router_obj)
app.include_router(_static_router.router_obj)


# ============================================================
# Backwards-compatible re-exports
# ============================================================
# Tests and a few internal callers reach into `gateway.server` for these
# helpers. Re-export them so existing imports keep working. New code
# should import from `gateway.deps` directly.

from gateway.deps import (  # noqa: E402,F401  (re-exports below)
    ADMIN_TOKEN,
    ALLOW_NO_ADMIN_AUTH,
    ALLOW_UNAUTHENTICATED_API,
    ADMIN_COOKIE_NAME,
    MAX_BODY_BYTES,
    _CURRENT_REQUEST,
    _UA_VERSION_PATTERNS,
    _UA_FALLBACK_MAX,
    _atomic_write,
    _check_admin,
    _check_client_auth,
    _check_login_rate,
    _check_model_access,
    _check_usage_rate_limit,
    _client_version,
    _cors_origins,
    _detect_client,
    _env_flag,
    _env_int,
    _gather_limited,
    _LOGIN_FAIL_LIMIT,
    _LOGIN_FAIL_WINDOW_S,
    _login_failures,
    _qclaw_provider_helper,
    _read_json,
    _read_json_object,
    _record_login_failure,
    _release_client_quota,
    _reserve_client_quota,
    _set_admin_cookie,
    _solo_callback_base,
    _stamp_client_info,
    _traesolo_provider_helper,
    _usage_date_bounds,
    _usage_rate_bucket,
    _USAGE_RATE_LIMIT,
    _USAGE_RATE_WINDOW_S,
    _usage_rate_key,
    _validate_key_channel,
)

# Endpoint aliases for tests that call the implementation directly
# (e.g. test_dashboard_perf.py calls `server.admin_stats(...)`).
from gateway.routers.admin import (  # noqa: E402,F401
    admin_stats,
    admin_provider_model_usage,
)
from gateway.routers.v1 import health, meta  # noqa: E402,F401


# ============================================================
# CLI entry point
# ============================================================

def main():
    global ADMIN_TOKEN, ALLOW_NO_ADMIN_AUTH

    ap = argparse.ArgumentParser(description="Buddy 2 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--admin-token", default=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
                    help="Admin API token. Defaults to CB_GATEWAY_ADMIN_TOKEN or a generated startup token.")
    ap.add_argument("--no-admin-auth", action="store_true",
                    help="Disable Admin API authentication. Only use on trusted local machines.")
    ap.add_argument("--log-level", default="warning", choices=["debug","info","warning","error"],
                    help="Log level")
    args = ap.parse_args()

    if args.no_admin_auth and args.host not in {"127.0.0.1", "localhost", "::1"}:
        ap.error("--no-admin-auth can only be used with a loopback host")

    ALLOW_NO_ADMIN_AUTH = args.no_admin_auth
    admin_token_source = args.admin_token or os.environ.get("CB_GATEWAY_ADMIN_TOKEN", "")
    admin_token_generated = bool(not ALLOW_NO_ADMIN_AUTH and not admin_token_source)
    # Assigning on the module (not as locals) lets _ServerModule.__setattr__
    # mirror the value into gateway.deps. Otherwise the routers read the
    # empty-string default from deps and every /admin/login 401s.
    sys.modules[__name__].ALLOW_NO_ADMIN_AUTH = ALLOW_NO_ADMIN_AUTH
    sys.modules[__name__].ADMIN_TOKEN = "" if ALLOW_NO_ADMIN_AUTH else (admin_token_source or f"cb-admin-{secrets.token_urlsafe(24)}")

    db.init_db()

    # Let `buddy2api.*` loggers respect --log-level (default warning)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING))

    startup = control_plane.startup_scan()
    sys.stderr.write(f"[startup] discover: {startup}\n")

    # TraeWork hourly sync (60s grace before first run)
    _schedule_traework_sync()

    accounts = db.list_accounts()
    sys.stderr.write(f"\n")
    sys.stderr.write(f"  Buddy 2 API v{VERSION}\n")
    sys.stderr.write(f"  ========================\n")
    sys.stderr.write(f"  监听: http://{args.host}:{args.port}\n")
    sys.stderr.write(f"  账号: {len(accounts)} 个 ({sum(1 for a in accounts if a['status']=='active')} active)\n")
    sys.stderr.write(f"  通道: {', '.join(providers.enabled_provider_ids())}\n")
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        ids = [
            (item["id"] if isinstance(item, dict) else str(item))
            for item in provider.list_models()
        ]
        preview = ", ".join(ids[:6]) + ("..." if len(ids) > 6 else "")
        sys.stderr.write(f"  模型[{channel}]: {len(ids)} 个 ({preview})\n")
    sys.stderr.write(
        f"  启动导入: {'on' if control_plane.auto_import_enabled() else 'off (CB_GATEWAY_AUTO_IMPORT=1 可打开)'}\n"
    )
    sys.stderr.write(f"  Admin: {'no auth' if ALLOW_NO_ADMIN_AUTH else 'enabled'}\n")
    if ADMIN_TOKEN:
        if admin_token_generated:
            sys.stderr.write(
                f"  Admin Token: {ADMIN_TOKEN}\n"
                f"  （自动生成的管理 Token，浏览器打开管理页后在「设置」里粘贴一次即可登录）\n"
            )
        else:
            sys.stderr.write("  Admin Token: configured (hidden)\n")
    sys.stderr.write(f"  ========================\n\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
