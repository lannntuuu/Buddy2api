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
import tomllib
import types
from pathlib import Path

# Make `python -m gateway.server` work from the project root by prepending
# the src/ directory to sys.path. The src/ layout puts every Python package
# under src/; without this line, the bare module name `gateway` cannot
# be resolved from a cwd outside src/. Tests add src/ via pytest.ini's
# pythonpath, but a CLI invocation of `python -m gateway.server` has
# no such helper. Adding it here keeps the same launch command
# (python -m gateway.server) working with no env-var juggling.
_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from storage import database as db
from accounts import auth_manager
from accounts import control_plane
import providers
from providers.traework.token import adopt_credentials_from_client
from providers.minimax_code import liveness as _mvs_liveness  # 自刷新活性门（只读探测），见下方对齐小节
from providers.minimax_code import store as _store  # 磁盘接管原语（同步），见下方对齐小节
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
        # 停用 traework 通道后必须停止循环：即使账号行仍为 active，
        # sync_traework_usage 也会继续拉官方接口，故按 enabled 决定是否退出。
        if not providers.is_channel_enabled("traework"):
            sys.stderr.write("[traework-sync] traework channel disabled; stopping sync loop\n")
            return
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
        # 若在睡眠期间 traework 被停用，最迟下一个周期开始时退出；不另行实时轮询。


def _align_traework_credentials() -> int:
    """启动时 best-effort 凭据对齐：若客户端 storage.json 的凭据比 DB 新，则采用。

    spec §3.3：网关启动时若拿旧 refresh_token 去刷，会直接把客户端刚刷好的票
    作废——这正是「重启后客户端掉线」的成因。故在 startup_scan 之后、调度 sync
    之前做一次对齐。复用 token.adopt_credentials_from_client 的语义
    （uid 一致 + 路径白名单 + 仅更新时回写 + best-effort），失败必须静默，
    绝不能阻断启动。返回成功接管的账号数。
    """
    try:
        if not providers.is_channel_enabled("traework"):
            return 0
        adopted = 0
        for account in db.list_accounts(provider="traework"):
            try:
                # adopt_credentials_from_client 是异步函数，但本 helper 在
                # main() 的同步启动路径中调用（尚无运行中的事件循环），故每次
                # 单独 asyncio.run。DB 写入是线程锁下的同步操作，无协程冲突。
                # require_newer=True：启动对齐只认 expires_at 更大，
                # 避免把网关已刷新好的新票换成客户端手里的旧票。
                if asyncio.run(adopt_credentials_from_client(account, require_newer=True)):
                    adopted += 1
            except Exception:  # noqa: BLE001 - 单个账号失败不阻断其余账号/启动
                continue
        if adopted:
            sys.stderr.write(f"[startup] traework: aligned {adopted} account credential(s) from client storage\n")
        return adopted
    except Exception as exc:  # noqa: BLE001 - 整体失败也绝不让启动中断
        sys.stderr.write(f"[startup] traework credential align skipped: {exc!r}\n")
        return 0


def _schedule_traework_sync() -> None:
    # 仅当启动时 traework 已启用才调度；运行时停用由循环体内的检查负责退出。
    if not providers.is_channel_enabled("traework"):
        return
    try:
        asyncio.get_running_loop().create_task(_traework_sync_loop())
    except RuntimeError:
        # No running loop (e.g. in tests or non-asyncio contexts); skip.
        pass


# ============================================================
# MiniMax Code credential align (startup + lightweight timer)
# ============================================================
# 实机验证（.tmp/mitm/minimax-code-20260919/ROTATION-VERDICT.md；spec:694,704）：
# MiniMax Code 桌面客户端每约 1h 自刷新一次，且**每次自刷新都会轮转 refresh_token**
# （access token 实测 TTL 约 1h，spec:317 记的 11 天已推翻）⇒ 客户端与网关并用时，
# 网关库内的 refresh_token 是死票，网关下次 OAuth refresh 被拒（invalid_grant）、
# 账号被判 expired，即网关周期性失效。选定保守策略：网关**先**从磁盘接管客户端落在
# auth.json 里更新后的凭据（不轮转、不顶掉对方），只有磁盘没有更新凭据时才由调用方
# 回退原有 OAuth refresh（token.py），保留"网关是唯一持有者"时的自刷新能力。
# 风控说明：接管（adopt_credentials_from_client）全程只做**本地文件读取 + DB 写入**，
# auth.json / auth-state.json **只读**、绝不回写（spec:694），**不发任何**对 MiniMax
# 生产的网络请求 ⇒ 启动对齐与定时器都不构成额外的上游流量/风控面。

async def _minimax_code_align_loop() -> None:
    await asyncio.sleep(60)  # delay the first run so startup stays snappy
    while True:
        # 停用 minimax_code 通道后必须停止循环：账号行仍为 active 时循环也会白跑，
        # 故按 enabled 决定是否退出（与 _traework_sync_loop 同款处置）。
        if not providers.is_channel_enabled("minimax_code"):
            sys.stderr.write("[minimax-code-align] minimax_code channel disabled; stopping align loop\n")
            return
        try:
            # 同步函数（明文 auth.json，无解密 await）⇒ 直接调，别 await。
            _align_minimax_code_credentials()
        except Exception as exc:  # noqa: BLE001 - 整轮兜底：异常只记 stderr，不中断循环
            sys.stderr.write(f"[minimax-code-align] error: {exc!r}\n")
        # 间隔可调（默认 300s）；下限 30s 防误配 0/负值把定时器打成忙轮询。
        await asyncio.sleep(max(30, _env_int("CB_MINIMAX_CODE_ALIGN_INTERVAL_S", 300)))
        # 若在睡眠期间 minimax_code 被停用，最迟下一个周期开始时退出；不另行实时轮询。


def _align_minimax_code_credentials() -> int:
    """启动/定时 best-effort 凭据对齐：客户端 auth.json 的票比 DB 新就接管进来。

    镜像 _align_traework_credentials（spec §3.3 的同一坑：拿旧 refresh_token 去刷会
    把客户端刚刷好的票作废）。复用 store.adopt_credentials_from_client 的语义
    （uid 一致 + 路径硬边界 + 只写非空值 + best-effort，见 store.py:765-841；
    极性对齐 traework/token.py:140-254）。require_newer=True：**只认 expires_at 更大**
    的凭据，绝不把网关刚刷新好的新票降级成客户端手里的旧票。
    与 traework 版唯一实现差异：本通道的接管函数是**同步**的，直接调用即可，
    不需要 asyncio.run。失败必须静默（逐账号 + 整体两层兜底），绝不阻断启动。
    返回成功接管的账号数。
    """
    try:
        if not providers.is_channel_enabled("minimax_code"):
            return 0
        adopted = 0
        for account in db.list_accounts(provider="minimax_code"):
            try:
                if _store.adopt_credentials_from_client(account, require_newer=True):
                    adopted += 1
            except Exception:  # noqa: BLE001 - 单个账号失败不阻断其余账号/启动
                continue
        if adopted:
            sys.stderr.write(
                f"[startup] minimax_code: aligned {adopted} credential(s) from client auth.json\n"
            )
        return adopted
    except Exception as exc:  # noqa: BLE001 - 整体失败也绝不让启动中断
        sys.stderr.write(f"[startup] minimax_code credential align skipped: {exc!r}\n")
        return 0


def _schedule_minimax_code_align() -> None:
    # 仅当启动时 minimax_code 已启用才调度；运行时停用由循环体内的检查负责退出。
    if not providers.is_channel_enabled("minimax_code"):
        return
    try:
        asyncio.get_running_loop().create_task(_minimax_code_align_loop())
    except RuntimeError:
        # No running loop (e.g. in tests or non-asyncio contexts); skip.
        pass


# ============================================================
# Logs retention sweep (24h)
# ============================================================

async def _log_prune_loop() -> None:
    """每日一次调用 db.prune_logs()（保留窗口由 CB_GATEWAY_LOG_RETENTION_DAYS 控制）。

    仿 _traework_sync_loop 模式：异常吞掉写 stderr，循环常驻。
    """
    await asyncio.sleep(60)  # delay the first run so startup stays snappy
    while True:
        try:
            removed = await asyncio.to_thread(db.prune_logs)
            if removed:
                sys.stderr.write(f"[log-prune] removed {removed} expired rows\n")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[log-prune] error: {exc!r}\n")
        await asyncio.sleep(24 * 3600)


def _schedule_log_prune() -> None:
    try:
        asyncio.get_running_loop().create_task(_log_prune_loop())
    except RuntimeError:
        # No running loop (e.g. in tests or non-asyncio contexts); skip.
        pass


# ============================================================
# Orphan account rows — startup self-heal
# ============================================================

def purge_orphan_accounts() -> int:
    """启动自愈:清扫「孤儿」账号行——provider 已不在 known_channel_ids()
    (内置通道 ∪ 现存自定义定义)且 status 非 active 的行。

    必须晚于 custom_channels.seed_initial_definitions() 运行:否则
    fresh-install 时 gmi/bailian 定义尚未落库,其账号行会被误判孤儿。
    active 孤儿行保留(用户可见可手动删,且它们本就无法被路由)。
    返回清扫条数;任何异常吞掉只告警,不阻断启动。
    """
    try:
        # 先读一次自定义定义:settings 表不可用时会抛异常 → 直接放弃清扫,
        # 避免 known 集合退化成仅内置通道、把全部自定义通道误判孤儿。
        from providers import custom_channels as _cc

        _cc.list_definitions()
        known = set(providers.known_channel_ids())
        removed = 0
        for row in db.list_accounts():
            if row.get("provider") in known:
                continue
            if row.get("status") == "active":
                continue
            db.delete_account(row["id"])
            removed += 1
        if removed:
            logger.warning(
                "startup: purged %d orphan account row(s) (unknown provider, non-active)",
                removed,
            )
        return removed
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: orphan account purge failed: %s", exc)
        return 0


# ============================================================
# FastAPI app assembly
# ============================================================

import contextlib


@contextlib.asynccontextmanager
async def _lifespan(_app):
    """Boot-time hooks: the custom-channel seed migration (idempotent, runs
    whenever the settings key is absent), then the orphan-account sweep —
    the sweep MUST run after the seed (see purge_orphan_accounts)."""
    # Defer imports: gateway deps / DB / custom_channels all touch the same
    # module graph; touching them at import time creates a cycle.
    from providers import custom_channels as _cc

    try:
        _cc.seed_initial_definitions()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[startup] custom-channels seed migration failed: {exc}\n")
    purge_orphan_accounts()
    _schedule_log_prune()
    yield


app = FastAPI(title="Buddy 2 API", version=VERSION, lifespan=_lifespan)
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


class _CacheableStaticFiles(StaticFiles):
    """给 /static 响应加一小时的 public 缓存（index 等动态路由不受影响）。"""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        try:
            response.headers["Cache-Control"] = "public, max-age=3600"
        except Exception:  # noqa: BLE001 无 headers 的异常响应按原样抛出
            pass
        return response


# Static assets (css/js/vendor modules); the index page itself is served by
# the static router at GET / (kept no-cache there).
app.mount("/static", _CacheableStaticFiles(directory=WEB_DIR), name="static")


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

def _load_config(path: Path, profile: str) -> dict:
    """Read a TOML config file and return the merged dict for one profile.

    Resolution order (later wins):
      [default]  ->  [<profile>]  ->  None if file missing.

    Each top-level table is returned as its own dict. Returns empty dict
    on any read/parse failure so the caller can fall through to defaults.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logging.getLogger("buddy2api.server").warning(
            "config: failed to load %s: %s", path, exc
        )
        return {}
    merged: dict = {}
    default = data.get("default") or {}
    if isinstance(default, dict):
        merged.update(default)
    if profile and profile != "default":
        prof = data.get(profile) or {}
        if isinstance(prof, dict):
            merged.update(prof)
    return merged


def _resolve_config() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    """两段式 CLI/TOML 配置解析。

    第一遍只认 --config/--config-name，据此加载 TOML 段落作为第二遍
    argparse 的默认值；--config 不含路径分隔符时视为 profile 名
    （--config prod → ./config.toml + profile=prod）。config.toml 设置的
    database.path 在此导出为 CB_GATEWAY_DB_PATH（存量环境变量优先）。
    返回 (parser, args)：parser 供 _resolve_admin_token 的 ap.error 使用。
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=os.environ.get("CB_GATEWAY_CONFIG", ""),
                     help="Path to config.toml (default: ./config.toml) or profile name "
                          "if --config-name is also set.")
    pre.add_argument("--config-name", default="",
                     help="Profile inside config.toml: 'dev', 'prod', or 'default'.")
    pre_args, _ = pre.parse_known_args()

    config_path = None
    profile = pre_args.config_name or "default"
    if pre_args.config:
        if "/" in pre_args.config or "\\" in pre_args.config:
            config_path = Path(pre_args.config)
        else:
            profile = pre_args.config
            config_path = Path("config.toml")
    if config_path is None:
        config_path = Path("config.toml")

    cfg = _load_config(config_path, profile)
    server_cfg = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
    admin_cfg = cfg.get("admin") if isinstance(cfg.get("admin"), dict) else {}
    db_cfg = cfg.get("database") if isinstance(cfg.get("database"), dict) else {}
    logging_cfg = cfg.get("logging") if isinstance(cfg.get("logging"), dict) else {}

    ap = argparse.ArgumentParser(description="Buddy 2 API")
    ap.add_argument("--host", default=server_cfg.get("host", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=server_cfg.get("port", 8787))
    ap.add_argument(
        "--admin-token",
        default=admin_cfg.get("token") or os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
        help="Admin API token. Defaults to admin.token in config.toml, "
             "then CB_GATEWAY_ADMIN_TOKEN, then a generated startup token.",
    )
    ap.add_argument(
        "--no-admin-auth",
        action="store_true",
        default=bool(admin_cfg.get("no_auth", False)),
        help="Disable Admin API authentication. Only use on trusted local machines.",
    )
    ap.add_argument(
        "--log-level",
        default=logging_cfg.get("level", "warning"),
        choices=["debug", "info", "warning", "error"],
    )
    args = ap.parse_args()

    # If config.toml set a database.path, export it as an env var so the
    # storage layer picks it up. (CB_GATEWAY_DB_PATH is the canonical name.)
    db_path = db_cfg.get("path")
    if db_path and not os.environ.get("CB_GATEWAY_DB_PATH"):
        os.environ["CB_GATEWAY_DB_PATH"] = str(db_path)

    # [workbuddy] snapshot_dir → CB_WORKBUDDY_SNAPSHOT_DIR（固化副本目录）。
    wb_cfg = cfg.get("workbuddy") if isinstance(cfg.get("workbuddy"), dict) else {}
    wb_snapshot = wb_cfg.get("snapshot_dir")
    if wb_snapshot and not os.environ.get("CB_WORKBUDDY_SNAPSHOT_DIR"):
        os.environ["CB_WORKBUDDY_SNAPSHOT_DIR"] = str(Path(wb_snapshot).expanduser())
    return ap, args


def _resolve_admin_token(args, ap) -> tuple[str, bool]:
    """校验 --no-admin-auth 与 host 组合并落 ADMIN_TOKEN / ALLOW_NO_ADMIN_AUTH。

    赋值走本模块属性（经 _ServerModule.__setattr__ 镜像进 gateway.deps），
    否则路由读 deps 里的空串默认值，每个 /admin/login 都会 401。
    返回 (admin_token, admin_token_generated)。
    """
    if args.no_admin_auth and args.host not in {"127.0.0.1", "localhost", "::1"}:
        ap.error("--no-admin-auth can only be used with a loopback host")

    allow_no_admin_auth = args.no_admin_auth
    admin_token_source = args.admin_token or os.environ.get("CB_GATEWAY_ADMIN_TOKEN", "")
    admin_token_generated = bool(not allow_no_admin_auth and not admin_token_source)
    sys.modules[__name__].ALLOW_NO_ADMIN_AUTH = allow_no_admin_auth
    sys.modules[__name__].ADMIN_TOKEN = "" if allow_no_admin_auth else (admin_token_source or f"cb-admin-{secrets.token_urlsafe(24)}")
    return sys.modules[__name__].ADMIN_TOKEN, admin_token_generated


def _print_banner(host: str, port: int, admin_token: str, admin_token_generated: bool) -> None:
    accounts = db.list_accounts()
    sys.stderr.write(f"\n")
    sys.stderr.write(f"  Buddy 2 API v{VERSION}\n")
    sys.stderr.write(f"  ========================\n")
    sys.stderr.write(f"  监听: http://{host}:{port}\n")
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
    if admin_token:
        if admin_token_generated:
            sys.stderr.write(
                f"  Admin Token: {admin_token}\n"
                f"  （自动生成的管理 Token，浏览器打开管理页后在「设置」里粘贴一次即可登录）\n"
            )
        else:
            sys.stderr.write("  Admin Token: configured (hidden)\n")
    sys.stderr.write(f"  ========================\n\n")


def main():
    ap, args = _resolve_config()
    admin_token, admin_token_generated = _resolve_admin_token(args, ap)

    db.init_db()

    # Let `buddy2api.*` loggers respect --log-level (default warning)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING))

    startup = control_plane.startup_scan()
    sys.stderr.write(f"[startup] discover: {startup}\n")

    # TraeWork 启动凭据对齐：在调度 sync 之前，先把客户端可能已更新的凭据
    # 采用进来，避免拿旧票去刷把客户端刚刷好的票作废（spec §3.3）。
    _align_traework_credentials()

    # TraeWork hourly sync (60s grace before first run)
    _schedule_traework_sync()

    # MiniMax Code 启动凭据对齐：客户端自刷新会轮转 refresh_token（ROTATION-VERDICT.md），
    # 必须在任何一次网关 OAuth 刷新之前，先把磁盘上更新的票接管进 DB；接管只读本地
    # auth.json + 写 DB，不发网络请求。best-effort，绝不阻断启动。
    _align_minimax_code_credentials()

    # 活性门（liveness）策略的运维可见性：启动时打印一行当前自刷模式 + 客户端进程探测结果。
    # startup_note() 只含模式枚举值、进程名匹配子串（配置项本身）与平台名 ⇒ 绝无凭证原文；
    # 探测只读本地进程表，不读写客户端文件、不发任何对 MiniMax 生产的网络请求（spec:694）。
    # best-effort：任何异常都兜成一行 stderr，绝不阻断启动。
    try:
        sys.stderr.write(_mvs_liveness.startup_note() + "\n")
    except Exception as exc:  # noqa: BLE001 - 运维摘要失败不影响网关可用性
        sys.stderr.write(f"[startup] minimax_code liveness note skipped: {exc!r}\n")

    # MiniMax Code 轻量对齐定时器（60s 宽限后按 CB_MINIMAX_CODE_ALIGN_INTERVAL_S 轮询）
    _schedule_minimax_code_align()

    _print_banner(args.host, args.port, admin_token, admin_token_generated)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
