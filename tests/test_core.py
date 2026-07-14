import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import proxy
import responses
import server
import auth_manager


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_account_credentials_are_encrypted_at_rest(isolated_db):
    account_id = db.add_account(
        {
            "name": "test",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "session_state": "session-secret",
        }
    )

    assert db.get_account(account_id)["access_token"] == "access-secret"
    with sqlite3.connect(isolated_db) as conn:
        raw = conn.execute("SELECT access_token, refresh_token FROM accounts WHERE id=?", (account_id,)).fetchone()
    assert raw[0].startswith("enc:v1:")
    assert "access-secret" not in raw[0]
    assert raw[1].startswith("enc:v1:")


def test_plaintext_credentials_are_migrated_on_startup(isolated_db):
    account_id = db.add_account({"name": "test", "access_token": "old", "refresh_token": "old-refresh"})
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE accounts SET access_token='legacy-access', refresh_token='legacy-refresh' WHERE id=?",
            (account_id,),
        )
        conn.commit()
    db.init_db()
    assert db.get_account(account_id)["access_token"] == "legacy-access"
    with sqlite3.connect(isolated_db) as conn:
        raw = conn.execute("SELECT access_token FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    assert raw.startswith("enc:v1:")


def test_daily_limit_reservation_is_atomic(isolated_db):
    key_id = db.add_api_key("sk-cb-test", "test", daily_limit=5)

    def reserve():
        return db.reserve_api_key_request(key_id, 5)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: reserve(), range(24)))

    assert sum(results) == 5
    assert db.get_api_key_daily_requests(key_id) == 5


def test_record_request_updates_log_and_counters_once(isolated_db):
    account_id = db.add_account({"name": "account", "access_token": "a", "refresh_token": "r"})
    key_id = db.add_api_key("sk-cb-test", "key")
    db.record_request(
        {
            "api_key_id": key_id,
            "api_key_name": "key",
            "account_id": account_id,
            "account_name": "account",
            "model": "auto",
            "total_tokens": 7,
            "credit": 0.25,
            "status_code": 200,
            "finish_reason": "stop",
        }
    )
    account = db.get_account(account_id)
    key = db.get_api_key_by_key("sk-cb-test")
    assert account["total_requests"] == 1
    assert account["total_tokens"] == 7
    assert key["total_requests"] == 1
    assert len(db.list_logs()) == 1


def test_api_auth_fails_closed_without_keys(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_UNAUTHENTICATED_API", False)
    with pytest.raises(HTTPException) as error:
        server._check_client_auth(None, None)
    assert error.value.status_code == 503


def test_responses_input_image_string_is_preserved():
    flattened = responses._flatten_content(
        [{"type": "input_image", "image_url": "data:image/png;base64,abc"}]
    )
    assert "data:image/png;base64,abc" in flattened


def test_retryable_statuses_are_explicit():
    assert proxy._is_retryable_status(429)
    assert proxy._is_retryable_status(503)
    assert not proxy._is_retryable_status(400)


def test_non_stream_proxy_fails_over_on_retryable_upstream(isolated_db, monkeypatch):
    account_id = db.add_account(
        {
            "name": "account",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    calls = []

    async def pick(exclude):
        calls.append("pick")
        return account

    async def headers(value):
        return {"Authorization": "Bearer access"}

    async def delay(_attempt):
        return None

    async def collect(*_args):
        if len(calls) == 1:
            return ("error", (503, {"error": {"message": "busy"}}))
        return ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(proxy, "_retry_delay", delay)
    monkeypatch.setattr(proxy, "_collect_stream", collect)
    result = asyncio.run(
        proxy.proxy_chat_completions(
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
            None,
        )
    )
    assert result[0] == "json"
    assert calls.count("pick") == 2


def test_debug_redaction_removes_credentials():
    redacted = responses._redact_debug_value(
        {"api_key": "secret", "messages": [{"content": "private"}]}
    )
    assert redacted["api_key"] == "<redacted>"
    assert redacted["messages"][0]["content"] == "<content redacted>"


def test_valid_headers_is_async_and_uses_decrypted_token(isolated_db):
    account_id = db.add_account(
        {
            "name": "account",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    headers = asyncio.run(__import__("auth_manager").get_valid_headers(account))
    assert headers["Authorization"] == "Bearer access-secret"
