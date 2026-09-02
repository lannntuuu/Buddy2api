"""请求日志搜索支持按 client / client_version 过滤。"""
from datetime import datetime, timedelta, date

import pytest

from storage import database as db


def _add(code: str, client: str, version: str, created_at=None):
    data = {
        "api_key_id": None,
        "api_key_name": "k-" + code,
        "account_id": None,
        "account_name": "a-" + code,
        "provider": "workbuddy",
        "model": "m-" + code,
        "stream": 0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "credit": 0.1,
        "finish_reason": "stop",
        "duration_ms": 1000,
        "status_code": 200,
        "error_msg": "",
        "client": client,
        "client_version": version,
    }
    if created_at is not None:
        data["created_at"] = created_at
    db.record_request(data)


def test_log_search_by_client(isolated_db):
    _add("1", "codex", "1.2.3")
    _add("2", "cursor", "0.9.1")

    hit = db.search_logs({"q": "codex"})
    assert hit["total"] == 1
    assert hit["items"][0]["client"] == "codex"


def test_log_search_by_client_version(isolated_db):
    _add("1", "codex", "1.2.3")
    _add("2", "cursor", "0.9.1")

    hit = db.search_logs({"q": "0.9.1"})
    assert hit["total"] == 1
    assert hit["items"][0]["client"] == "cursor"


def test_log_search_excludes_non_matching_client(isolated_db):
    _add("1", "codex", "1.2.3")
    _add("2", "cursor", "0.9.1")

    hit = db.search_logs({"q": "claude"})
    assert hit["total"] == 0
