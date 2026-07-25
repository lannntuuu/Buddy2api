import asyncio
import json
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


def _chat_sse(payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n".encode("utf-8")


def _collect_response_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    async def source():
        for chunk in chunks:
            yield chunk

    async def collect():
        return [
            event
            async for event in responses.chat_stream_to_responses_stream(
                source(),
                "test-model",
            )
        ]

    parsed = []
    for raw_event in asyncio.run(collect()):
        assert raw_event.endswith("\n\n")
        lines = raw_event.strip().splitlines()
        event_name = next(line[7:] for line in lines if line.startswith("event: "))
        data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
        payload = json.loads(data)
        assert payload["type"] == event_name
        parsed.append((event_name, payload))
    return parsed


def _events_of_type(events: list[tuple[str, dict]], event_name: str) -> list[dict]:
    return [payload for name, payload in events if name == event_name]


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
    hourly = db.get_stats()["today"]["hourly"]
    assert len(hourly) == 24
    assert sum(bucket["requests"] for bucket in hourly) == 1
    assert sum(bucket["tokens"] for bucket in hourly) == 7
    assert sum(bucket["credit"] for bucket in hourly) == 0.25


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


def test_responses_stream_reassembles_byte_split_tool_arguments():
    first_args = '{"command":"echo 中'
    second_args = '文"}'
    wire = b"".join([
        _chat_sse({
            "model": "upstream-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_shell",
                    "type": "function",
                    "function": {
                        "name": "shell_command",
                        "arguments": first_args,
                    },
                }]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": second_args},
                }]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }),
        b"data: [DONE]\n\n",
    ])

    events = _collect_response_events([bytes([value]) for value in wire])
    names = [name for name, _ in events]
    deltas = _events_of_type(events, "response.function_call_arguments.delta")
    done = _events_of_type(events, "response.function_call_arguments.done")
    added = _events_of_type(events, "response.output_item.added")
    completed = _events_of_type(events, "response.completed")

    assert [delta["delta"] for delta in deltas] == [first_args, second_args]
    assert not _events_of_type(events, "response.output_text.delta")
    assert len(added) == len(done) == len(completed) == 1
    item_id = added[0]["item"]["id"]
    output_index = added[0]["output_index"]
    assert added[0]["item"]["call_id"] == "call_shell"
    assert added[0]["item"]["name"] == "shell_command"
    assert all(delta["item_id"] == item_id for delta in deltas)
    assert all(delta["output_index"] == output_index for delta in deltas)
    assert done[0]["item_id"] == item_id
    assert json.loads(done[0]["arguments"]) == {"command": "echo 中文"}
    assert completed[0]["response"]["model"] == "upstream-model"
    assert completed[0]["response"]["usage"]["total_tokens"] == 7
    assert names.index("response.function_call_arguments.done") < names.index("response.output_item.done")
    assert names[-1] == "response.completed"
    sequence_numbers = [payload["sequence_number"] for _, payload in events]
    assert sequence_numbers == sorted(sequence_numbers)
    assert len(sequence_numbers) == len(set(sequence_numbers))


def test_responses_stream_keeps_parallel_tool_calls_separate():
    chunks = [
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {
                        "index": 0,
                        "id": "call_one",
                        "function": {"name": "shell", "arguments": '{"command":"one'},
                    },
                    {
                        "index": 1,
                        "id": "call_two",
                        "function": {"name": "read_file", "arguments": '{"path":"a'},
                    },
                ]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 1, "function": {"arguments": '.txt"}'}},
                    {"index": 0, "function": {"arguments": '"}'}},
                ]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }),
        b"data: [DONE]\n\n",
    ]

    events = _collect_response_events(chunks)
    added = _events_of_type(events, "response.output_item.added")
    done = _events_of_type(events, "response.function_call_arguments.done")
    completed = _events_of_type(events, "response.completed")[0]["response"]

    assert len(added) == len(done) == 2
    added_by_call = {event["item"]["call_id"]: event for event in added}
    assert set(added_by_call) == {"call_one", "call_two"}
    assert len({event["item"]["id"] for event in added}) == 2
    assert len({event["output_index"] for event in added}) == 2
    done_by_item = {event["item_id"]: json.loads(event["arguments"]) for event in done}
    assert done_by_item[added_by_call["call_one"]["item"]["id"]] == {"command": "one"}
    assert done_by_item[added_by_call["call_two"]["item"]["id"]] == {"path": "a.txt"}
    output_by_call = {item["call_id"]: item for item in completed["output"]}
    assert json.loads(output_by_call["call_one"]["arguments"]) == {"command": "one"}
    assert json.loads(output_by_call["call_two"]["arguments"]) == {"path": "a.txt"}
    assert all(item["status"] == "completed" for item in completed["output"])


def test_responses_stream_completes_on_terminal_finish_reason_at_eof():
    tool_event = _chat_sse({
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_eof",
                "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
            }]},
            "finish_reason": None,
        }],
    })
    finish_event = _chat_sse({
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }).rstrip(b"\n")

    events = _collect_response_events([tool_event, finish_event])
    assert len(_events_of_type(events, "response.function_call_arguments.done")) == 1
    assert len(_events_of_type(events, "response.completed")) == 1
    assert not _events_of_type(events, "response.failed")


def test_responses_stream_fails_on_unexpected_eof_with_partial_tool_call():
    events = _collect_response_events([_chat_sse({
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_partial",
                "function": {"name": "shell", "arguments": '{"command":"git'},
            }]},
            "finish_reason": None,
        }],
    })])

    failed = _events_of_type(events, "response.failed")
    output_done = _events_of_type(events, "response.output_item.done")
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "upstream_stream_ended"
    assert output_done[0]["item"]["status"] == "incomplete"
    assert not _events_of_type(events, "response.function_call_arguments.done")
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_propagates_upstream_error_as_failed():
    events = _collect_response_events([
        _chat_sse({"error": {
            "message": "rate limited",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }}),
        b"data: [DONE]\n\n",
    ])

    failed = _events_of_type(events, "response.failed")
    assert len(failed) == 1
    assert failed[0]["response"]["status"] == "failed"
    assert failed[0]["response"]["error"] == {
        "code": "rate_limit_exceeded",
        "message": "rate limited",
    }
    assert not _events_of_type(events, "response.completed")


@pytest.mark.parametrize(
    ("finish_reason", "incomplete_reason"),
    [("length", "max_output_tokens"), ("content_filter", "content_filter")],
)
def test_responses_stream_marks_truncated_tool_call_incomplete(
    finish_reason,
    incomplete_reason,
):
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_truncated",
                    "function": {"name": "shell", "arguments": '{"command":"git'},
                }]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }),
        b"data: [DONE]\n\n",
    ])

    incomplete = _events_of_type(events, "response.incomplete")
    output_done = _events_of_type(events, "response.output_item.done")
    assert len(incomplete) == 1
    assert incomplete[0]["response"]["incomplete_details"] == {
        "reason": incomplete_reason,
    }
    assert output_done[0]["item"]["status"] == "incomplete"
    assert not _events_of_type(events, "response.function_call_arguments.done")
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_emits_complete_text_lifecycle():
    events = _collect_response_events([
        _chat_sse({
            "choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}],
        }),
        b"data: [DO",
        b"NE]\n\n",
    ])

    names = [name for name, _ in events]
    assert names.count("response.output_text.done") == 1
    assert names.count("response.content_part.done") == 1
    assert names.count("response.output_item.done") == 1
    assert names.count("response.completed") == 1
    assert names[-1] == "response.completed"
    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert completed["output"][0]["content"][0]["text"] == "hello"


def test_responses_stream_fails_when_only_one_choice_finishes_before_eof():
    events = _collect_response_events([
        _chat_sse({
            "choices": [
                {"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"},
                {"index": 1, "delta": {"content": "partial"}, "finish_reason": None},
            ],
        }),
    ])

    failed = _events_of_type(events, "response.failed")
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "upstream_stream_ended"
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_supports_multiline_data_and_cr_line_endings():
    payload = json.dumps({
        "choices": [{
            "index": 0,
            "delta": {"content": "hello"},
            "finish_reason": "stop",
        }],
    }, separators=(",", ":"))
    first, second = payload[:1], payload[1:]
    wire = (
        ": keepalive\r\n"
        "event: message\r\n"
        f"data: {first}\r\n"
        f"data: {second}\r\n"
        "\r\n"
        "data: [DONE]\r\r"
    ).encode("utf-8")

    events = _collect_response_events([bytes([value]) for value in wire])
    completed = _events_of_type(events, "response.completed")
    assert len(completed) == 1
    assert completed[0]["response"]["output"][0]["content"][0]["text"] == "hello"
    assert not _events_of_type(events, "response.failed")


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
