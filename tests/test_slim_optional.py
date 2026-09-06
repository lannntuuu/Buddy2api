"""WS-A2 可选包 F1-F5 的行为特征测试（redesign-audit/34 §WS-A2）。

- F1: store_common.run_test_chat 探活外壳的输出契约
  （成功 dict/str 两形态、HTTPError→0、失败透传）。
- F2: traework / traesolo 两家 checkin 的行为特征（合并前先落，
  合并后必须原样保持绿）：traework 的 data.code 业务失败判定、
  traesolo 的纯 HTTP 判定、两家的两段式 claim 流程。
- F4: store_common.discover_dirs 骨架（目录占位/计数回填/收集顺序）。

沿用本仓库约定：同步测试函数内用 asyncio.run 驱动协程（未装 pytest-asyncio）。
"""
import asyncio

import httpx

from providers import store_common
from providers.traesolo import chat as tsc
from providers.traework import quota as twq
from providers.traesolo import quota as tsq


# ---------------------------------------------------------------------------
# F1 · run_test_chat 共享外壳
# ---------------------------------------------------------------------------

def test_run_test_chat_dict_result_shape():
    """dict 结果：choices[0].message 取 content（回退 reasoning_content），
    带 model/usage，status_code 恒 200。"""

    async def send(payload):
        assert payload == {
            "model": "m1",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "max_tokens": 64,
        }
        return 200, None, {
            "model": "upstream-m",
            "choices": [{"message": {"reasoning_content": "thinking", "content": ""}}],
            "usage": {"total_tokens": 9},
        }

    out = asyncio.run(store_common.run_test_chat("m1", "hi", send))
    assert out["ok"] is True
    assert out["status_code"] == 200
    assert out["model"] == "upstream-m"
    assert out["message"] == "thinking"       # content 为空回退 reasoning_content
    assert out["usage"] == {"total_tokens": 9}
    assert isinstance(out["duration_ms"], int)


def test_run_test_chat_error_and_http_error_paths():
    """失败：(status, message, None) 透传；httpx.HTTPError → status 0、截 240。"""

    async def fail(payload):
        return 429, "slow down", None

    out = asyncio.run(store_common.run_test_chat("m", "p", fail))
    assert out == {
        "ok": False, "status_code": 429, "duration_ms": out["duration_ms"],
        "message": "slow down",
    }

    async def boom(payload):
        raise httpx.ConnectTimeout("x" * 300)

    out = asyncio.run(store_common.run_test_chat("m", "p", boom))
    assert out["ok"] is False
    assert out["status_code"] == 0
    assert len(out["message"]) == 240
    assert out["message"] == "x" * 240

    async def short_boom(payload):
        raise httpx.ConnectError("reset")

    out = asyncio.run(store_common.run_test_chat("m", "p", short_boom, limit=400))
    assert out["message"] == "reset"


def test_run_test_chat_str_result_uses_limit():
    """str 结果（traework 回合文本）：直接作为 message，按 limit 截断。"""

    async def send(payload):
        return 200, None, "y" * 500

    out = asyncio.run(store_common.run_test_chat("m", "p", send, limit=400))
    assert out["ok"] is True and out["status_code"] == 200
    assert out["message"] == "y" * 400
    assert "model" not in out and "usage" not in out


# ---------------------------------------------------------------------------
# F2 · 两家 checkin 行为特征（合并前落，合并后保持绿）
# ---------------------------------------------------------------------------

_TW_ACC = {"id": 11, "nickname": "tw-acc", "access_token": "jwt-tw", "extra": {}}
_SOLO_ACC = {"id": 12, "nickname": "solo-acc", "access_token": "jwt-solo", "extra": {}}


def _mock_traework_client(monkeypatch, handler):
    monkeypatch.setattr(
        twq, "get_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_traework_checkin_code_field_is_failure(isolated_db, monkeypatch):
    """traework 特征：HTTP 200 但 data.code != 0 也按失败处理，
    message 取 data.message（截 240）。"""
    _mock_traework_client(monkeypatch, lambda req: httpx.Response(
        200, json={"code": 1, "message": "not today"}
    ))
    row = asyncio.run(twq.fetch_checkin(dict(_TW_ACC)))
    assert row["ok"] is False
    assert row["status_code"] == 200
    assert row["message"] == "not today"


def test_traework_checkin_claim_two_post_flow(isolated_db, monkeypatch):
    """traework 特征：claim 先查 status（未签到）再发 claim 两段请求；
    领取行 claimed=True，credit 取 claim 响应的 credits。"""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": True})
        return httpx.Response(200, json={"code": 0, "message": "ok", "credits": 12.5})

    _mock_traework_client(monkeypatch, handler)
    row = asyncio.run(twq.claim_checkin(dict(_TW_ACC)))
    assert row["ok"] is True and row["claimed"] is True
    assert row["credit"] == 12.5
    assert sum(p.endswith("/checkin_credits/status") for p in seen) == 1
    assert sum(p.endswith("/checkin_credits/claim") for p in seen) == 1


def test_traesolo_checkin_http_error_message_ignores_body(isolated_db):
    """traesolo 特征：HTTP >=400 只看状态码，message=HTTP N，
    不读 body 里的 message（与 traework 的 code_error 判定相反）。"""
    tsc._TRANSPORT = httpx.MockTransport(
        lambda req: httpx.Response(503, json={"code": 9, "message": "upstream detail"})
    )
    try:
        row = asyncio.run(tsq.fetch_checkin(dict(_SOLO_ACC)))
    finally:
        tsc._TRANSPORT = None
    assert row["ok"] is False
    assert row["status_code"] == 503
    assert row["message"] == "HTTP 503"


def test_traesolo_checkin_claim_two_post_flow(isolated_db):
    """traesolo 特征：claim 先查 status（未签到）再发 claim 两段请求；
    领取行 claimed=True，credit 取 claim 响应的 credits。"""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": True})
        return httpx.Response(200, json={"code": 0, "message": "ok", "credits": 3})

    tsc._TRANSPORT = httpx.MockTransport(handler)
    try:
        row = asyncio.run(tsq.claim_checkin(dict(_SOLO_ACC)))
    finally:
        tsc._TRANSPORT = None
    assert row["ok"] is True and row["claimed"] is True
    assert row["credit"] == 3
    assert sum(p.endswith("/checkin_credits/status") for p in seen) == 1
    assert sum(p.endswith("/checkin_credits/claim") for p in seen) == 1


# ---------------------------------------------------------------------------
# F4 · discover_dirs 骨架
# ---------------------------------------------------------------------------

def test_discover_dirs_skeleton(tmp_path, monkeypatch):
    """目录占位/存在位/计数回填/收集顺序与四家原 discover 一致：
    不存在目录只留占位；存在目录按 collect_fn 顺序逐个过 meta_fn。"""
    from pathlib import Path

    missing = tmp_path / "nope"
    existing = tmp_path / "yes"
    existing.mkdir()
    (existing / "a.json").write_text("{}", encoding="utf-8")
    (existing / "b.json").write_text("{}", encoding="utf-8")

    metas = []

    def collect(folder: Path):
        if folder == missing:
            return []
        return [existing / "b.json", existing / "a.json"]

    def meta_fn(path: Path, existing_uids):
        metas.append(path.name)
        return {"path": str(path), "valid": True}

    monkeypatch.setattr(store_common, "existing_uids", lambda channel: set())
    summary = store_common.discover_dirs("chan", [missing, existing], collect, meta_fn)

    assert [d["path"] for d in summary["dirs"]] == [str(missing), str(existing)]
    assert summary["dirs"][0]["exists"] is False and summary["dirs"][0]["file_count"] == 0
    assert summary["dirs"][1]["exists"] is True and summary["dirs"][1]["file_count"] == 2
    assert metas == ["b.json", "a.json"]          # 收集顺序保持
    assert summary["file_count"] == 2 and summary["valid_count"] == 2
    assert summary["importable_count"] == 2 and summary["channel"] == "chan"
