import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest

from storage import database as db
import providers
from gateway import router
from gateway import server as tw_server
from providers.protocol import UnknownModel
from providers.traework import chat as tw
from providers.traework import token as tw_token
from providers.traework.chat import _split_event, _text_from_event, extract_assistant_text, translate_model
from providers.traework.crypto import _MAGIC, _SALT, decrypt_tc_b64
from providers.traework.store import parse_credentials, traework_auth_dirs
from providers.traework.token import adopt_credentials_from_client


@pytest.fixture()
def traework_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


@pytest.fixture(autouse=True)
def _clean_global_auth_state():
    """每例前后清掉进程级鉴权全局态，避免泄漏到后续测试文件。

    本模块的 _turn/_run_turn 用例会写 auth_manager._account_failures（失败冷却）
    与 trae_shared 的 refresh 负缓存、sticky 账号；这些是模块级全局，isolated_db
    并不清理。历史症状：跑完本文件后，test_pick_contract 里的账号处于冷却态、
    _pick 直接跳过，导致 "calls == []" 一类假失败（单独运行该文件却全绿）。
    """
    from accounts import auth_manager
    from providers import trae_shared

    def _reset():
        trae_shared.reset_refresh_failures()
        auth_manager._sticky_account_id.clear()
        with auth_manager._failure_lock:
            auth_manager._account_failures.clear()

    _reset()
    yield
    _reset()


def test_traework_in_default_registry(monkeypatch, isolated_db):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    enabled = providers.enabled_provider_ids()
    canonical = {"workbuddy", "qclaw", "qwenwork", "traework", "traesolo"}
    assert canonical.issubset(set(enabled)), (
        f"missing default channels: {canonical - set(enabled)}"
    )
    assert providers.get_provider("traework") is not None
    assert "traework" in providers._LOADED


def test_parse_credentials_official_shape():
    parsed = parse_credentials(
        {
            "token": "jwt-access",
            "refreshToken": "rt-1",
            "userId": "3577",
            "expiredAt": "2026-09-09T08:55:19.325Z",
            "host": "https://api.trae.cn",
            "account": {"username": "书虫"},
            "device_id": "3446",
        }
    )
    assert parsed["provider"] == "traework"
    assert parsed["access_token"] == "jwt-access"
    assert parsed["refresh_token"] == "rt-1"
    assert parsed["uid"] == "3577"
    assert parsed["extra"]["device_id"] == "3446"
    assert parsed["expires_at"] > 10_000_000_000


def test_parse_credentials_requires_token():
    with pytest.raises(ValueError):
        parse_credentials({"account": {"username": "x"}})


def test_bind_traework_when_enabled(traework_enabled, isolated_db):
    bound = router.bind({"model": "auto"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "auto"
    bound = router.bind({"model": "traework/qwen-3.7-plus"}, {"default_channel": "traework"})
    assert bound.inner == "qwen-3.7-plus"
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "traework"})


def test_translate_auto(isolated_db):
    assert translate_model("auto") == "qwen-3.7-plus"


def test_extract_assistant_text_from_task():
    items = [
        {"role": "user", "content": "[]"},
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {"type": "text", "text_content": "pong"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assert extract_assistant_text(items) == "pong"


def test_extract_assistant_text_prefers_finish_over_reasoning():
    # 线上真实结构：plan_item 嵌套一层，回答在 finish 工具 params.summary，
    # reasoning_content 是思考文本，不得作为回答返回。
    items = [
        {"role": "user", "content": "[]"},
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {
                            "id": "m1",
                            "type": "plan_item",
                            "plan_item": {
                                "id": "p1",
                                "thought": "",
                                "reasoning_content": (
                                    'The user is asking me to reply with "pong". '
                                    "This is a simple request.\n"
                                ),
                                "tool_call_info": {
                                    "id": "tc1",
                                    "name": "finish",
                                    "params": {"summary": "pong"},
                                    "result": {"status": "success"},
                                },
                                "agent_status": {"status": "completed"},
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assert extract_assistant_text(items) == "pong"


def test_extract_assistant_text_thinking_only_fallback():
    # 没有 finish / 正文时退回思考文本，保持旧的兜底行为。
    items = [
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {
                            "type": "plan_item",
                            "plan_item": {
                                "thought": "",
                                "reasoning_content": "thinking out loud",
                                "tool_call_info": {
                                    "name": "web_search",
                                    "params": {"query": "x"},
                                },
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        }
    ]
    assert extract_assistant_text(items) == "thinking out loud"


def test_event_plan_item_uses_finish_summary_not_reasoning():
    # 事件流里的扁平 plan_item 事件：只取 finish 的回答，不取 reasoning。
    payload = {
        "id": "p1",
        "task_id": "t1",
        "thought": "",
        "reasoning_content": 'The user is asking me to reply with "pong".\n',
        "tool_call_info": {
            "id": "tc1",
            "name": "finish",
            "params": {"summary": "pong"},
            "result": {"status": "success"},
        },
    }
    assert _text_from_event("plan_item", payload) == "pong"


def test_event_without_finish_collects_nothing_from_reasoning():
    payload = {
        "id": "p1",
        "task_id": "t1",
        "thought": "",
        "reasoning_content": "The",
        "tool_call_info": {"id": "tc1", "name": "", "params": None, "result": {}},
    }
    assert _text_from_event("plan_item", payload) == ""


# ---------------------------------------------------------------------------
# 会话模式（work / code）可配置：默认 work；配置 code 改 mode 且 agent 联动
# （mode=code -> solo_agent_lite，其余含 work/未配置 -> solo_work_lite）
# ---------------------------------------------------------------------------


def test_session_mode_default_is_work(isolated_db):
    from providers.model_config import channel_session_mode

    # 未配置 → 默认 work，且行为与硬常量一致
    assert channel_session_mode("traework", "work") == "work"
    assert channel_session_mode("traework", "work") == "work" == tw.SESSION_MODE


def test_session_mode_reads_setting(isolated_db):
    from providers.model_config import channel_session_mode

    db.set_setting("traework.mode", "code")
    assert channel_session_mode("traework", "work") == "code"
    # 非法值回退默认
    db.set_setting("traework.mode", "bogus")
    assert channel_session_mode("traework", "work") == "work"
    # 空串回退
    db.set_setting("traework.mode", "")
    assert channel_session_mode("traework", "work") == "work"


def _post_body(fake, suffix):
    for _url, body in fake.posts:
        if _url.endswith(suffix):
            return body
    raise AssertionError(f"no POST to {suffix}")


def test_turn_creates_session_with_default_mode(isolated_db, monkeypatch):
    """默认（无 mode 设置）建会话 body 与改造前逐字段一致：mode=work，agent 不变。"""
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    text = asyncio.run(tw._turn(account, "hi", "qwen-3.7-plus", timeout=90.0))
    assert text == "pong"
    # 建会话 POST 的 body 与改造前逐字段一致
    create_body = _post_body(fake, "/chat_sessions")
    assert create_body["mode"] == "work"
    assert create_body["auto_create_project"] is True
    assert create_body["origin"] == "web"
    # agent_id / agent_type 在发消息 POST 上，模式切换不改 agent（仍为 solo_work_lite）
    msg_body = _post_body(fake, "/messages")
    assert msg_body["agent_id"] == "solo_work_lite"
    assert msg_body["agent_type"] == "solo_work_lite"


def test_turn_creates_session_with_code_mode(isolated_db, monkeypatch):
    """配置 traework.mode=code：建会话 mode=code，agent_id/agent_type 联动为 solo_agent_lite。"""
    db.set_setting("traework.mode", "code")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    asyncio.run(tw._turn(account, "hi", "qwen-3.7-plus", timeout=90.0))
    create_body = _post_body(fake, "/chat_sessions")
    assert create_body["mode"] == "code"
    msg_body = _post_body(fake, "/messages")
    assert msg_body["agent_id"] == "solo_agent_lite"
    assert msg_body["agent_type"] == "solo_agent_lite"


def test_turn_creates_session_with_work_mode(isolated_db, monkeypatch):
    """配置 traework.mode=work：建会话 mode=work，agent_id/agent_type 仍为 solo_work_lite。"""
    db.set_setting("traework.mode", "work")
    fake = _FakeTraeClient()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    account = {"access_token": "tok", "extra": {}}
    asyncio.run(tw._turn(account, "hi", "qwen-3.7-plus", timeout=90.0))
    create_body = _post_body(fake, "/chat_sessions")
    assert create_body["mode"] == "work"
    msg_body = _post_body(fake, "/messages")
    assert msg_body["agent_id"] == "solo_work_lite"
    assert msg_body["agent_type"] == "solo_work_lite"


def test_traework_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "traework"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text


def test_traework_auth_dirs_ignore_workbuddy_cb_auth_dir(monkeypatch, tmp_path):
    tdir = tmp_path / "trae-auth"
    tdir.mkdir()
    wb = tmp_path / "workbuddy-auth"
    wb.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(tdir))
    monkeypatch.setenv("CB_AUTH_DIR", str(wb))
    dirs = [path.resolve() for path in traework_auth_dirs()]
    assert tdir.resolve() in dirs
    assert wb.resolve() not in dirs


def test_decrypt_tc_roundtrip_rejects_garbage():
    with pytest.raises(Exception):
        decrypt_tc_b64("not-base64-$$$")


# ---------------------------------------------------------------------------
# 流式提前转发思考文本（_stream_chat）
# ---------------------------------------------------------------------------


def test_split_event_separates_thinking_and_answer():
    answer, thinking = _split_event(
        "plan_item",
        {
            "id": "t1",
            "thought": "思考内容",
            "reasoning_content": "推理内容",
            "tool_call_info": {"name": "finish", "params": {"summary": "pong"}},
        },
    )
    assert answer == "pong"
    assert "思考内容" in thinking
    assert "推理内容" in thinking


def test_split_event_skips_noise_events():
    assert _split_event("heartbeat", {"x": 1}) == ("", [])
    assert _split_event("token_usage", {"input": 1}) == ("", [])
    assert _split_event("status_changed", {"new_status": "running"}) == ("", [])


def _collect_stream(monkeypatch, fake_turn, order):
    monkeypatch.setattr(tw, "_run_turn", fake_turn)
    chunks = []

    async def consume():
        async for chunk in tw._stream_chat("请只回复：pong", "qwen-3.7-plus", "auto", None):
            chunks.append(chunk)
            order.append("chunk")

    asyncio.run(consume())
    text = "".join(chunks)
    payloads = [
        json.loads(line[5:].strip())
        for line in text.splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]
    return payloads, text


def test_stream_chat_first_byte_before_turn_finishes(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        await asyncio.sleep(0.05)
        order.append("turn_done")
        return "ok", "pong"

    payloads, text = _collect_stream(monkeypatch, fake_turn, order)
    # 首包（role）必须先于回合完成发出
    assert order[0] == "chunk"
    assert payloads[0]["choices"][0]["delta"].get("role") == "assistant"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    assert "pong" in contents
    assert text.rstrip().endswith("data: [DONE]")


def test_stream_chat_forwards_thinking_early(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("用户要求只回复某个词")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    raw = [c["choices"][0]["delta"].get("content") for c in payloads]
    contents = [x.strip() for x in raw if x]
    # 思考片段先于最终回答出现
    assert contents.index("用户要求只回复某个词") < contents.index("pong")


def test_stream_chat_no_duplicate_answer(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("pong")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    contents = [c["choices"][0]["delta"].get("content") for c in payloads]
    # 答案已包含在转发过的思考文本里，不再重复发
    assert contents.count("pong") == 1


def test_stream_chat_dedups_cumulative_thinking(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        if on_thinking is not None:
            await on_thinking("思考第一步")
            await on_thinking("思考第一步。继续推理")
        await asyncio.sleep(0.02)
        return "ok", "pong"

    payloads, _text = _collect_stream(monkeypatch, fake_turn, order)
    raw = [c["choices"][0]["delta"].get("content") for c in payloads]
    contents = [x.strip() for x in raw if x]
    # 第二段是累计重发，只转发增量；前缀不重复出现
    assert "思考第一步" in contents
    assert "。继续推理" in contents
    assert "".join(contents).count("思考第一步") == 1


def test_stream_chat_error_surfaces_in_band(monkeypatch):
    order = []

    async def fake_turn(prompt, model, client_model, info, stream=False, on_thinking=None, timeout=90.0):
        await asyncio.sleep(0.02)
        return ("error", (503, {"error": {"message": "No available accounts", "type": "channel_unavailable"}}))

    payloads, text = _collect_stream(monkeypatch, fake_turn, order)
    # 流内错误用 OpenAI 兼容的 error 对象承载，不再伪造正常回答
    contents = "".join(
        c["choices"][0]["delta"].get("content") or ""
        for c in payloads if "choices" in c
    )
    assert "上游处理失败" not in contents
    assert not any("choices" in c and c["choices"][0]["finish_reason"] == "stop" for c in payloads)
    assert any("error" in c for c in payloads)
    assert "No available accounts" in text
    assert "data: [DONE]" in text


# ---------------------------------------------------------------------------
# 测试替身：自包含跑完 _turn（建会话 → 发消息 → 收流 → GET /messages）
# ---------------------------------------------------------------------------


class _FakeTraeResponse:
    def __init__(self, *, status_code=200, payload=None, content=True):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content

    def json(self):
        return self._payload


class _FakeTraeStream:
    """模拟 client.stream("GET", .../events) 的异步行迭代器：发一条 done 事件即结束。"""

    def __init__(self, response):
        self._response = response

    def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        yield "event: done"
        yield "data: {}"


class _FakeTraeClient:
    """记录所有 POST 的 (url, json)，并让 _turn 走完标准 happy path。"""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/chat_sessions"):
            return _FakeTraeResponse(payload={"code": 0, "data": {"chat_session_id": "sid-1"}})
        if url.endswith("/messages"):
            return _FakeTraeResponse(payload={"code": 0, "data": {}})
        return _FakeTraeResponse()

    async def get(self, url, *, headers=None, timeout=None):
        if url.endswith("/messages"):
            return _FakeTraeResponse(
                payload={"code": 0, "data": {"items": [
                    {"role": "assistant", "message_type": "task",
                     "content": json.dumps(
                         {"task_id": "t", "messages": [
                             {"type": "text", "text_content": "pong"}]},
                         ensure_ascii=False)},
                ]}}
            )
        return _FakeTraeResponse()

    def stream(self, method, url, *, headers=None, timeout=None):
        return _FakeTraeStream(_FakeTraeResponse())

    async def delete(self, url, *, headers=None, timeout=None):
        return _FakeTraeResponse()


# ---------------------------------------------------------------------------
# TraeWork 凭据自救（spec §3.2）与启动对齐（spec §3.3）测试
# 不使用真实网络 / 真实凭据；storage.json 由测试本地构造并解密。
# 临时目录放在仓库 .tmp 下（沙箱禁止写入系统 TEMP，故不用 pytest tmp_path）。
# ---------------------------------------------------------------------------


def _encrypt_tc_b64(plain_text: str) -> str:
    """构造与 crypto.decrypt_tc_b64 互逆的加密 blob（仅测试用）。"""
    import base64
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    key_mat = os.urandom(32)
    mixed = hashlib.sha512(key_mat).digest() + _SALT
    derived = hashlib.sha512(mixed).digest()
    aes_key, iv = derived[:16], derived[16:32]
    body = plain_text.encode("utf-8")
    plain = hashlib.sha512(body).digest() + body
    padder = PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    blob = _MAGIC + key_mat + cipher
    return base64.b64encode(blob).decode("ascii")


def _write_storage(auth_dir: Path, *, uid, access, refresh, expired_at_iso) -> Path:
    """写一个可被 import_discovered 解密的 storage.json（只读，不写回客户端）。"""
    document = {
        "token": access,
        "refreshToken": refresh,
        "userId": uid,
        "expiredAt": expired_at_iso,
        "host": "https://api.trae.cn",
        "account": {"username": "tester"},
    }
    storage = {"iCubeAuthInfo://icube.cloudide": _encrypt_tc_b64(json.dumps(document))}
    path = auth_dir / "storage.json"
    path.write_text(json.dumps(storage), encoding="utf-8")
    return path


@pytest.fixture()
def client_auth_dir(monkeypatch):
    # 放在仓库 .tmp 下以避免沙箱对系统 TEMP 的写入限制。
    base = Path(__file__).resolve().parent.parent / ".tmp" / f"tw-adopt-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(base))
    yield base
    import shutil

    shutil.rmtree(base, ignore_errors=True)


def _add_traework(uid="3577", **overrides):
    data = {
        "name": "tw-acc",
        "provider": "traework",
        "uid": uid,
        "status": "active",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": 4_000_000_000_000,  # 远未来，避免 pick 触发主动刷新
        "extra": {},
    }
    data.update(overrides)
    return db.add_account(data)


# ---------------------------------------------------------------------------
# §5.1 自愈：refresh 失败 + 客户端更新凭据 → 被采用、状态回 active
# ---------------------------------------------------------------------------


def test_adopt_updated_credentials_from_client(isolated_db, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(
        status="expired",
        extra={"auth_path": str(path)},
    )
    # 初始：expired + 旧凭据
    before = db.get_account(aid)
    assert before["status"] == "expired"
    assert before["access_token"] == "old-access"

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is True
    after = db.get_account(aid)
    assert after["status"] == "active"
    assert after["access_token"] == "new-access"
    assert after["refresh_token"] == "new-refresh"
    # 不应整行覆盖、不应改 uid
    assert after["uid"] == "3577"


# ---------------------------------------------------------------------------
# §5.1 集成：_turn 因鉴权失效失败 → 自愈 → 用新凭据重试成功
# ---------------------------------------------------------------------------


class _FakeTraeClientFailOnce(_FakeTraeClient):
    """首个建会话 POST 返回 401（模拟 refresh 失效），其后恢复正常 happy path。"""

    def __init__(self):
        super().__init__()
        self.fail_next_session = True

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/chat_sessions") and self.fail_next_session:
            self.fail_next_session = False
            return _FakeTraeResponse(status_code=401, payload={})
        if url.endswith("/chat_sessions"):
            return _FakeTraeResponse(payload={"code": 0, "data": {"chat_session_id": "sid-1"}})
        if url.endswith("/messages"):
            return _FakeTraeResponse(payload={"code": 0, "data": {}})
        return _FakeTraeResponse()


def test_run_turn_self_heals_and_retries(isolated_db, monkeypatch, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(extra={"auth_path": str(path)})
    fake = _FakeTraeClientFailOnce()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, text = asyncio.run(
        tw._run_turn("hi", "qwen-3.7-plus", "auto", None, stream=False)
    )

    assert status == "ok", text
    assert text == "pong"
    # 建会话 POST 出现两次：首次失败触发自愈，重试一次成功
    session_posts = [u for u, _ in fake.posts if u.endswith("/chat_sessions")]
    assert len(session_posts) == 2
    after = db.get_account(aid)
    assert after["status"] == "active"
    assert after["access_token"] == "healed-access"


# ---------------------------------------------------------------------------
# §5.2 uid 不一致 → 不接管、维持原状
# ---------------------------------------------------------------------------


def test_adopt_rejects_uid_mismatch(isolated_db, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="9999",  # 与账号 uid 3577 不一致
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    after = db.get_account(aid)
    assert after["status"] == "expired"  # 维持原状
    assert after["access_token"] == "old-access"


# ---------------------------------------------------------------------------
# §5.3 文件不可读 / 解密失败 / 路径越权 → 不接管、不抛异常（best-effort）
# ---------------------------------------------------------------------------


def test_adopt_skip_missing_file(isolated_db, client_auth_dir):
    missing = client_auth_dir / "storage.json"  # 不存在
    aid = _add_traework(status="expired", extra={"auth_path": str(missing)})
    before = db.get_account(aid)

    # 不得抛异常
    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["access_token"] == "old-access"


def test_adopt_skip_corrupt_storage(isolated_db, client_auth_dir):
    path = client_auth_dir / "storage.json"
    path.write_text(json.dumps({"iCubeAuthInfo://icube.cloudide": "not-a-tc-blob"}), encoding="utf-8")
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["access_token"] == "old-access"


def test_adopt_skip_path_outside_whitelist(isolated_db, client_auth_dir, monkeypatch):
    # 把白名单指到别的目录，使 client_auth_dir 下的文件越权 → import_discovered 抛错
    other = client_auth_dir.parent / f"tw-other-{uuid.uuid4().hex[:8]}"
    other.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(other))
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    import shutil

    shutil.rmtree(other, ignore_errors=True)


# ---------------------------------------------------------------------------
# §5.4 客户端凭据无更新 → 不接管（避免无谓轮换）
# ---------------------------------------------------------------------------


def test_adopt_skip_when_no_update(isolated_db, client_auth_dir):
    # 客户端凭据与 DB 完全一致：token 相同，且 expires_at 不大于 DB 的远未来值
    # （DB 的 4e12ms ≈ 2096，这里用更小的 2026，避免 expires_at 触发"更新"判断）。
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="old-access",  # 与 DB 完全相同
        refresh="old-refresh",
        expired_at_iso="2026-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["status"] == "expired"


# ---------------------------------------------------------------------------
# §5.3 / §3.3 启动对齐：客户端更新时接管；失败时不抛、不阻断
# ---------------------------------------------------------------------------


def test_startup_align_adopts_updated(isolated_db, client_auth_dir):
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="aligned-access",
        refresh="aligned-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})

    adopted = tw_server._align_traework_credentials()

    assert adopted >= 1
    after = db.get_account(aid)
    assert after["status"] == "active"
    assert after["access_token"] == "aligned-access"


def test_startup_align_silent_on_bad_path(isolated_db, client_auth_dir):
    missing = client_auth_dir / "storage.json"
    aid = _add_traework(status="expired", extra={"auth_path": str(missing)})

    # 失败必须静默：不抛、不阻断，返回 0
    adopted = tw_server._align_traework_credentials()

    assert adopted == 0
    assert db.get_account(aid)["status"] == "expired"


# ---------------------------------------------------------------------------
# require_newer：启动对齐只认「明确更新」，不得把新票换成客户端旧票
# ---------------------------------------------------------------------------


def test_startup_align_rejects_older_client_credentials(isolated_db, client_auth_dir):
    """客户端 token 与 DB 不同但 expires_at 更旧 → 启动对齐不得接管。

    否则每次重启都会把网关刚刷新好的新票换成客户端的旧票，反而弄坏可用凭据。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="stale-client-access",
        refresh="stale-client-refresh",
        expired_at_iso="2026-01-01T00:00:00Z",  # 远旧于 DB 的 2096
    )
    aid = _add_traework(extra={"auth_path": str(path)})
    before = db.get_account(aid)
    assert before["access_token"] == "old-access"

    # 启动对齐（require_newer=True）
    adopted = tw_server._align_traework_credentials()

    assert adopted == 0
    after = db.get_account(aid)
    assert after["access_token"] == "old-access"  # 未被替换
    assert after["refresh_token"] == "old-refresh"


def test_selfheal_still_accepts_different_token(isolated_db, client_auth_dir):
    """自愈路径（require_newer=False）仍接受「不同即接管」——旧票已判废。

    与上一条对照，确保 require_newer 只收紧了启动对齐、没削弱自愈。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2026-01-01T00:00:00Z",  # 即便更旧
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    adopted = asyncio.run(adopt_credentials_from_client(before))  # require_newer 默认 False

    assert adopted is True
    after = db.get_account(aid)
    assert after["access_token"] == "healed-access"
    assert after["status"] == "active"


# ---------------------------------------------------------------------------
# patch 安全性：空 token 不覆盖、expires_at 解析失败不得把有效值砸成 0
# ---------------------------------------------------------------------------


def test_adopt_does_not_clobber_expires_at_when_client_lacks_it(
    isolated_db, client_auth_dir, monkeypatch
):
    """客户端凭据缺 expires_at 时，不得把 DB 里的有效过期时间覆盖成 0。

    回归点：早期实现无条件写 expires_at=int(parsed.get(...) or 0)，
    会把有效值砸成 0，令 is_token_expired 判定错乱。
    """
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="newer-access",
        refresh="newer-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)
    original_exp = before["expires_at"]
    assert original_exp  # 有值

    # 模拟「客户端凭据解析不出 expires_at」：直接改 adopt 内部读到的 parsed
    real_import = tw_token.import_discovered
    monkeypatch.setattr(
        tw_token, "import_discovered",
        lambda p: {**real_import(p), "expires_at": 0, "refresh_expires_at": 0},
    )

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is True  # token 不同 → 自愈仍接管
    after = db.get_account(aid)
    assert after["expires_at"] == original_exp  # 未被砸成 0
    assert after["access_token"] == "newer-access"


def test_adopt_rejects_credentials_without_any_token(isolated_db, client_auth_dir, monkeypatch):
    """客户端凭据里没有任何 token → 不接管，不得把账号复活成空凭据。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="x",
        refresh="y",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    real_import = tw_token.import_discovered
    monkeypatch.setattr(
        tw_token, "import_discovered",
        lambda p: {**real_import(p), "access_token": "", "refresh_token": ""},
    )

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    after = db.get_account(aid)
    assert after["status"] == "expired"
    assert after["access_token"] == "old-access"


def test_adopt_rejects_empty_client_uid(isolated_db, client_auth_dir, monkeypatch):
    """客户端 uid 解析为空时无法核对身份 → 拒绝接管（不默认放行）。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="new-access",
        refresh="new-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(status="expired", extra={"auth_path": str(path)})
    before = db.get_account(aid)

    real_import = tw_token.import_discovered
    monkeypatch.setattr(
        tw_token, "import_discovered",
        lambda p: {**real_import(p), "uid": ""},
    )

    adopted = asyncio.run(adopt_credentials_from_client(before))

    assert adopted is False
    assert db.get_account(aid)["access_token"] == "old-access"


# ---------------------------------------------------------------------------
# 自救重试的异常收敛：重试时非鉴权异常也必须降级为 503，不得穿透成 500
# ---------------------------------------------------------------------------


class _FakeTraeClientFailThenBoom(_FakeTraeClient):
    """首次建会话 401（触发自愈），重试用新凭据时抛 httpx.HTTPError。"""

    def __init__(self):
        super().__init__()
        self.fail_next_session = True

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/chat_sessions") and self.fail_next_session:
            self.fail_next_session = False
            return _FakeTraeResponse(status_code=401, payload={})
        if url.endswith("/chat_sessions"):
            raise httpx.ConnectError("boom after self-heal")
        return _FakeTraeResponse()


def test_selfheal_retry_network_error_degrades_to_503(
    isolated_db, monkeypatch, client_auth_dir
):
    """自愈成功后的重试若网络异常，必须收敛为 503（不得穿透成 500）。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    _add_traework(extra={"auth_path": str(path)})
    fake = _FakeTraeClientFailThenBoom()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    status, detail = asyncio.run(
        tw._run_turn("hi", "qwen-3.7-plus", "auto", None, stream=False)
    )

    assert status == "error"
    code, payload = detail
    assert code == 503, payload
    assert payload["error"]["type"] == "server_error"


# ---------------------------------------------------------------------------
# 主症状修复：token 过期 → pick 内 refresh 失败 → 就地自救（不必等 _turn）
#
# 这是用户实际遇到的「自动退出登录」路径：账号 token 过期后，pick 里 refresh
# 401，旧实现只进负缓存然后一路 503（需人工重导）。现在应在 pick 阶段就
# 从客户端 storage.json 接管凭据并返回可用账号。
# ---------------------------------------------------------------------------


def test_pick_self_heals_from_client_when_refresh_fails(
    isolated_db, monkeypatch, client_auth_dir
):
    """refresh 失败时，provider 的 pick 路径应自救成功并返回可用账号。"""
    from providers.traework import PROVIDER
    from providers.traework import token as twt

    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="picked-access",
        refresh="picked-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(
        status="expired",
        expires_at=1_000,  # 已过期，会走 refresh 路径
        extra={"auth_path": str(path)},
    )

    async def _boom(_account):
        raise twt.TraeWorkAuthError("refresh token is invalid")

    monkeypatch.setattr(twt, "refresh_account", _boom)
    # provider facade 持有的引用也要换（它 import 的是同名符号）
    monkeypatch.setattr(
        "providers.traework.refresh_account", _boom, raising=False
    )

    picked = asyncio.run(PROVIDER.pick_account_with_fallback())

    assert picked is not None, "refresh 失败后应从客户端 storage.json 自救成功"
    assert picked["access_token"] == "picked-access"
    assert db.get_account(aid)["status"] == "active"


def test_pick_without_adopt_fn_keeps_legacy_semantics(isolated_db, monkeypatch):
    """未传 adopt_fn 时，pick_with_refresh_fallback 行为与改动前一致（返回 None）。

    回归护栏：其余四家 facade 不传 adopt_fn，语义不得被本次改动影响。
    """
    from providers import trae_shared
    from providers.traework import token as twt

    _add_traework(status="expired", expires_at=1_000)

    async def _boom(_account):
        raise twt.TraeWorkAuthError("refresh token is invalid")

    trae_shared.reset_refresh_failures()
    picked = asyncio.run(
        trae_shared.pick_with_refresh_fallback("traework", _boom)
    )

    assert picked is None  # 没有 adopt_fn → 维持旧的"失败即 None"语义


# ---------------------------------------------------------------------------
# 管理页「测试」按钮也必须自愈
#
# test_chat 直接拿账号调 _turn，绕过 _pick/_run_turn；若不自救，凭据被客户端
# 轮换后「测试」会永远失败 —— 而它恰是用户判断通道可用性的主要入口。
# ---------------------------------------------------------------------------


def test_test_chat_self_heals_from_client(isolated_db, monkeypatch, client_auth_dir):
    """凭据失效时，「测试」应自救成功并返回 200 + 回答，而不是一直 503。"""
    path = _write_storage(
        client_auth_dir,
        uid="3577",
        access="healed-access",
        refresh="healed-refresh",
        expired_at_iso="2099-01-01T00:00:00Z",
    )
    aid = _add_traework(extra={"auth_path": str(path)})
    account = db.get_account(aid)

    fake = _FakeTraeClientFailOnce()  # 首次建会话 401，其后 happy path
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    result = asyncio.run(tw.test_chat(account, model="qwen-3.7-plus"))

    assert result["ok"] is True, result
    assert result["status_code"] == 200
    assert result["message"] == "pong"
    # 自救确实发生了：账号被接管为新凭据
    assert db.get_account(aid)["access_token"] == "healed-access"


def test_test_chat_returns_503_when_no_credentials_to_heal(
    isolated_db, monkeypatch, client_auth_dir
):
    """无自救素材（客户端 storage.json 缺主凭据）时，测试仍如实报 503，不谎报成功。"""
    missing = client_auth_dir / "storage.json"  # 不存在
    aid = _add_traework(extra={"auth_path": str(missing)})
    account = db.get_account(aid)

    fake = _FakeTraeClientFailOnce()
    monkeypatch.setattr(tw, "get_client", lambda: fake)

    result = asyncio.run(tw.test_chat(account, model="qwen-3.7-plus"))

    assert result["ok"] is False
    assert result["status_code"] == 503
    # 不得因为自救失败而把账号凭据改动
    assert db.get_account(aid)["access_token"] == "old-access"
