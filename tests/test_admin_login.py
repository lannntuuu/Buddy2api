"""Smoke test for /admin/login: ensures the start-up admin token (set on
the gateway.server module by main()) actually reaches the static router
and accepts the matching token.

Background: when the gateway was refactored in P2, /admin/login moved
into gateway.routers.static_router and the previous test surface stopped
covering the cookie + bearer path through that router. The startup hook
in gateway.server.main() used to be `ADMIN_TOKEN = "..."` as a local
variable (which silently did nothing for the routers) and static_router
snapshotted `ADMIN_TOKEN` at import time (so the empty-string default
stuck). Together they produced a regression where every real-world
/admin/login 401'd regardless of what token the user typed.

These tests pin the happy path: the post-P2 wiring must accept the
configured token and set the cb_gw_admin_token cookie.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from gateway import server
from gateway.deps import ADMIN_COOKIE_NAME


@pytest.fixture()
def admin_token(monkeypatch):
    """Set the admin token via the mirror-friendly module path.

    The real startup assigns to `sys.modules[__main__]`, which triggers
    `_ServerModule.__setattr__` and mirrors into `gateway.deps`. Here we
    use the same `server` module path that the test suite already uses
    elsewhere, so the mirror is exercised.
    """
    monkeypatch.setattr(server, "ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", False)


def test_static_router_accepts_correct_token_via_bearer(admin_token):
    """The router must accept a valid token even when no cookie is present."""
    from gateway.routers import static_router

    request = _FakeRequest(client_host="127.0.0.1", cookies={})
    request._body = {"token": "test-admin-token"}
    response = asyncio.run(static_router.admin_login(request))
    assert response.status_code == 200
    cookies = response.headers.get("set-cookie", "")
    assert ADMIN_COOKIE_NAME in cookies
    assert "test-admin-token" in cookies


def test_static_router_rejects_wrong_token(admin_token):
    """A wrong token must 401, not 500."""
    from gateway.routers import static_router

    request = _FakeRequest(client_host="127.0.0.2", cookies={})
    request._body = {"token": "nope"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(static_router.admin_login(request))
    assert exc.value.status_code == 401


def test_check_admin_accepts_admin_cookie(admin_token):
    """After login, subsequent /admin/* calls come back with the cookie.

    admin_login only validates the body token, but the cookie is what
    every other admin endpoint reads. This test pins that `_check_admin`
    accepts a valid cookie without an Authorization header.
    """
    from gateway import deps as _deps
    request = _FakeRequest(
        client_host="127.0.0.1",
        cookies={ADMIN_COOKIE_NAME: "test-admin-token"},
    )
    # _check_admin reads from _CURRENT_REQUEST context var. Simulate the
    # middleware by setting it ourselves.
    token = _deps._CURRENT_REQUEST.set(request)
    try:
        _deps._check_admin(None)  # no Authorization header
    finally:
        _deps._CURRENT_REQUEST.reset(token)


class _FakeRequest:
    """Minimal Request stand-in for the static router's body-token flow.

    The static router's admin_login currently reads the body via
    `_read_json_object`, which parses the raw ASGI body. We monkey-patch
    that helper in the test below so we can drive admin_login with a
    plain dict payload.
    """

    def __init__(self, client_host: str = "127.0.0.1", cookies: dict | None = None):
        self.client = type("Client", (), {"host": client_host})()
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}
        self.url = type("URL", (), {"scheme": "http"})()

    async def json(self):
        return getattr(self, "_body", {})

    async def stream(self):
        import json as _json
        body = getattr(self, "_body", {})
        yield _json.dumps(body).encode("utf-8")


@pytest.fixture(autouse=True)
def _patch_read_json(monkeypatch):
    """`admin_login` calls `_read_json_object(request)` which reads the
    ASGI body stream. Bypass it by giving the request a `_body` dict
    and patching the helper to return it.
    """
    from gateway import deps as _deps

    async def _read(_request, *, allow_empty=False):
        body = getattr(_request, "_body", None) or {}
        return body if body else {}

    monkeypatch.setattr(_deps, "_read_json_object", _read)


@pytest.fixture
def set_request_body():
    """Helper to set the body before invoking the router."""
    def _set(request: _FakeRequest, body: dict) -> None:
        request._body = body
    return _set
