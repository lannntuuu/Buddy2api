"""Admin endpoints: channels, accounts, api-keys, logs, settings, codex, etc.

WS-A split: the former single-module `gateway/routers/admin.py` is now a
package; each submodule owns one resource domain and its own APIRouter, and
shared helpers (`_check_admin`, `_read_json*`, provider accessors, ...) come
from `gateway.deps` (read at call time — no import-time ADMIN_TOKEN snapshot).

This `__init__` re-assembles the per-domain routers in the original route
definition order (first-appearance order of each domain; the qclaw login
handlers were interleaved inside the accounts block in the old file and now
live in `_channel_logins` — no route-shape overlap exists between domains)
and re-exports every handler name so the legacy import surface keeps working:

- `from gateway.routers.admin import admin_xxx` (server.py re-export chain,
  test_perf_admin_api.py)
- `admin.admin_xxx` attribute access (test_custom_channels.py)
- `admin.router_obj` (server.py include, test_route_golden.py)
"""
from __future__ import annotations

from fastapi import APIRouter

from gateway.routers.admin._channels import router as _channels_router
from gateway.routers.admin._custom_channels import router as _custom_channels_router
from gateway.routers.admin._traework import router as _traework_router
from gateway.routers.admin._stats import router as _stats_router
from gateway.routers.admin._model_config import router as _model_config_router
from gateway.routers.admin._accounts import router as _accounts_router
from gateway.routers.admin._channel_logins import router as _channel_logins_router
from gateway.routers.admin._api_keys import router as _api_keys_router
from gateway.routers.admin._misc import router as _misc_router

router_obj = APIRouter()
router_obj.include_router(_channels_router)
router_obj.include_router(_custom_channels_router)
router_obj.include_router(_traework_router)
router_obj.include_router(_stats_router)
router_obj.include_router(_model_config_router)
router_obj.include_router(_accounts_router)
router_obj.include_router(_channel_logins_router)
router_obj.include_router(_api_keys_router)
router_obj.include_router(_misc_router)

# --- Handler re-exports (legacy surface: gateway.routers.admin.<name>) ---

from gateway.routers.admin._channels import (  # noqa: E402,F401
    admin_channels,
    admin_update_channels,
    admin_channel_health,
    admin_channel_models,
    admin_set_channel_models,
    admin_refresh_channel_models,
)
from gateway.routers.admin._custom_channels import (  # noqa: E402,F401
    admin_list_custom_channels,
    admin_create_custom_channel,
    admin_update_custom_channel,
    admin_delete_custom_channel,
)
from gateway.routers.admin._traework import (  # noqa: E402,F401
    admin_traework_sync_usage,
    admin_traework_usage,
)
from gateway.routers.admin._stats import (  # noqa: E402,F401
    admin_credit_overview,
    admin_stats,
    admin_provider_model_usage,
    admin_credit_summary,
)
from gateway.routers.admin._model_config import (  # noqa: E402,F401
    admin_get_unified_models,
    admin_set_unified_models,
    admin_get_models,
    admin_update_models,
    admin_get_aliases,
    admin_update_aliases,
)
from gateway.routers.admin._accounts import (  # noqa: E402,F401
    admin_list_accounts,
    admin_discover_accounts,
    admin_import_accounts,
    admin_scan_accounts,
    admin_add_account,
    admin_update_account,
    admin_delete_account,
    admin_resources_batch,
    admin_refresh_account,
    admin_test_account,
    admin_account_resources,
    admin_checkin_status,
    admin_checkin_status_all,
    admin_claim_checkin,
    admin_claim_all_checkin,
)
from gateway.routers.admin._channel_logins import (  # noqa: E402,F401
    admin_qclaw_import_path,
    admin_qclaw_login_start,
    admin_qclaw_login_complete,
    solo_authorize_callback,
    admin_traesolo_login_start,
    admin_traesolo_login_result,
    admin_traesolo_login_cancel,
    admin_traesolo_login_complete,
)
from gateway.routers.admin._api_keys import (  # noqa: E402,F401
    admin_list_keys,
    admin_reveal_key,
    admin_create_key,
    admin_update_key,
    admin_delete_key,
)
from gateway.routers.admin._misc import (  # noqa: E402,F401
    admin_logs,
    admin_logs_search,
    admin_get_settings,
    admin_update_settings,
    admin_codex_setup,
    admin_codex_status,
)
