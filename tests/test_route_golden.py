"""路由表金样测试(35号方案 admin 拆分的安全网,先于一切搬运落地)。

冻结 app.openapi() 的 method+path 全集:admin 拆分/任何路由改动导致集合
变化时立即红灯。重录方式:确认 diff 属预期后删除 GOLDEN_MARKER 重新生成。
"""
import sys

from fastapi.testclient import TestClient  # noqa: F401  (仅为 import 冒烟语义)

import gateway.server as server

GOLDEN_MARKER = "FROZEN"

EXPECTED_ROUTES = None  # 由 collect_routes() 首跑填充


def collect_routes() -> list[str]:
    schema = server.app.openapi()
    return sorted(
        f"{method.upper()} {path}"
        for path, methods in schema["paths"].items()
        for method in methods
    )


def _expected() -> list[str]:
    global EXPECTED_ROUTES
    if EXPECTED_ROUTES is None:
        EXPECTED_ROUTES = [
            "DELETE /admin/accounts/{aid}",
            "DELETE /admin/api-keys/{kid}",
            "DELETE /admin/channels/custom/{cid}",
            "GET /",
            "GET /admin/accounts",
            "GET /admin/accounts/checkin-status-all",
            "GET /admin/accounts/discover",
            "GET /admin/accounts/{aid}/checkin",
            "GET /admin/accounts/{aid}/resources",
            "GET /admin/aliases",
            "GET /admin/api-keys",
            "GET /admin/api-keys/{kid}/reveal",
            "GET /admin/channels",
            "GET /admin/channel-health",
            "GET /admin/channels/custom",
            "GET /admin/channels/{channel}/models",
            "GET /admin/codex/status",
            "GET /admin/credit-overview",
            "GET /admin/credit-summary",
            "GET /admin/logs",
            "GET /admin/logs/search",
            "GET /admin/meta",
            "GET /admin/models",
            "GET /admin/provider-model-usage",
            "GET /admin/settings",
            "GET /admin/stats",
            "GET /admin/traesolo/login/result",
            "GET /admin/traework/usage",
            "GET /admin/unified-models",
            "GET /health",
            "GET /v1/models",
            "POST /admin/accounts",
            "POST /admin/accounts/checkin-all",
            "POST /admin/accounts/import",
            "POST /admin/accounts/resources/batch",
            "POST /admin/accounts/scan",
            "POST /admin/accounts/{aid}/checkin",
            "POST /admin/accounts/{aid}/refresh",
            "POST /admin/accounts/{aid}/test",
            "POST /admin/api-keys",
            "POST /admin/channels/custom",
            "POST /admin/channels/{channel}/models/refresh",
            "POST /admin/codex/setup",
            "POST /admin/login",
            "POST /admin/logout",
            "POST /admin/qclaw/import-path",
            "POST /admin/qclaw/login/complete",
            "POST /admin/qclaw/login/start",
            "POST /admin/traesolo/login/cancel",
            "POST /admin/traesolo/login/complete",
            "POST /admin/traesolo/login/start",
            "POST /admin/traework/sync-usage",
            "POST /v1/chat/completions",
            "POST /v1/responses",
            "PUT /admin/accounts/{aid}",
            "PUT /admin/aliases",
            "PUT /admin/api-keys/{kid}",
            "PUT /admin/channels",
            "PUT /admin/channels/custom/{cid}",
            "PUT /admin/channels/{channel}/models",
            "PUT /admin/models",
            "PUT /admin/settings",
            "PUT /admin/unified-models",
    ]

    return EXPECTED_ROUTES


def test_route_golden():
    routes = collect_routes()
    if GOLDEN_MARKER == "REGENERATE":  # 首次记录模式
        print("\nROUTES_BEGIN")
        for r in routes:
            print(r)
        print("ROUTES_END")
    assert set(routes) == set(_expected()), (
        "路由集合变化:" 
        f" 新增={sorted(set(routes) - set(_expected()))}"
        f" 消失={sorted(set(_expected()) - set(routes))}"
    )


def test_import_gateway_server_smoke():
    import gateway.server  # noqa: F401  拆分后包导入必须仍然成立
    from gateway.routers import admin  # noqa: F401  handler 重导出面必须完整

    assert hasattr(admin, "router_obj")
