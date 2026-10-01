"""MiniMax Code (``minimax_code``) 通道回归测试：凭证入库 / 目录硬边界 / 方言翻译 /
流式状态机 / 错误分类 / 限流登记 / 端到端（全程离线，httpx.MockTransport）。

协议权威来源：``.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md``
（下文 ``spec:NNN`` = 该文件行号）；实现以 ``src/providers/minimax_code/`` 为准。

钉住的回归点（每条都对应一次真实会踩的坑，不是覆盖率凑数）：
  1. ``store.auth_json_to_account`` —— auth.json 文档 → 账号；凭证原文只准出现在
     ``access_token``/``refresh_token`` 两个字段里，**绝不进** ``extra``/``name``/
     ``uid``/``domain`` 等诊断面（脱敏红线）。
  2. ``store.minimax_auth_dirs`` —— 多实例硬边界（spec:8,669）：只挑 ``prod/cn``，
     排除 ``en/staging/test/dev``；env 覆盖也**不许**突破该边界。
  3. ``translate.build_anthropic_payload`` —— OpenAI → Anthropic Messages：
     system 拆分、tool_calls→tool_use、role:tool→tool_result、input_schema、
     max_tokens 兜底、thinking 只对 M3 发、cache_control 保留但 ttl 剥掉、
     document block 显式拒绝（spec:562）。
  4. 流式状态机 —— 分类**只认** ``data["type"]``，不认 ``event:`` 行
     （``upstream/sse.py:105`` 丢弃 event 行；本用例故意让 event 标签与内部 type
     不一致来证明这一点）。
  5. 截断流（无 ``message_stop``）必须显式判错（spec:591），绝不静默半截回复。
  6. ``stop_reason`` 四条映射 + usage total = 四字段求和（spec:594,619）。
  7. ``_classify_error`` —— 八个业务码 + 内层私有码 2056/2067/1400010161 +
     两套信封（MiniMax / Anthropic）；流内只带 ``error.type`` 时经
     ``ANTHROPIC_ERROR_TYPE_TO_CODE`` 补出业务码（否则对外错误帧缺 code）。
  8. 限流 —— record/预判跳过/全限返回体字段名；两套信封的"将在 … UTC+8 重置"
     都要能解析成解除时刻（按 UTC+8 减 30s 余量）。
  9. ``chat_completions`` 端到端 —— 假 200 与 假 401→刷新→**单次**重放；
     请求头 ``x-api-key: sk-xxx`` + ``Authorization: Bearer``；``/v1`` 净效果只一份；
     绝不回写客户端 auth.json（哨兵文件内容 + mtime 双重校验）。
 10. facade 可用性 —— 9 核心方法齐备 + ``fetch_quota`` 恒 ``unsupported=True``。
 11. **MITM 实测 2026-09-30 回归（文末 §11）** —— 会话 id 形状 / 浏览器直连头 /
     output_config.effort 与 format 共存 / 新默认模型目录 / thinking.display /
     tools[].eager_input_streaming / usage.thinking_tokens 提取且 total 口径不变。

凭证安全：全程只用假值 ``AT-FAKE`` / ``RT-FAKE``；零真实网络（MockTransport 拦截
全部请求），不向 MiniMax 生产 API 发任何包。
"""

import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
import pytest

import upstream.rate_limits as rate_limits
from accounts import auth_manager
from providers.minimax_code import PROVIDER
from providers.minimax_code import chat, store
from providers.minimax_code import translate as T
from providers.minimax_code.constants import (
    ALIASES,
    ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS,
    ANTHROPIC_ERROR_TYPE_TO_CODE,
    API_KEY_PLACEHOLDER,
    CHANNEL_ID,
    CHAT_PATH,
    DEFAULT_MODEL,
    DISPLAY_NAME,
    EAGER_INPUT_STREAMING,
    EFFORT_DEFAULT,
    EFFORT_LEVELS,
    HEADER_ANTHROPIC_BETA,
    HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS,
    LLM_AUTH_ERROR,
    LLM_CLUSTER_OVERLOADED,
    LLM_CREDITS_EXHAUSTED,
    LLM_MIGRATION_ERROR,
    LLM_RATE_LIMITED,
    LLM_TPM_RATE_LIMITED,
    LLM_UPSTREAM_ERROR,
    MAVIS_SESSION_ID_HEX_LEN,
    MAVIS_SESSION_ID_PREFIX,
    STATIC_MODELS,
    THINKING_DISPLAY_FIELD,
    THINKING_DISPLAY_SUMMARIZED,
    TOKEN_PATH,
    UPSTREAM_ERROR_CODES,
    UPSTREAM_STATUS_CODE_MAP,
    USAGE_COMPLETION_TOKENS_DETAILS,
    USAGE_LIMIT_EXCEEDED,
    USAGE_OUTPUT_TOKENS_DETAILS,
    USAGE_REASONING_TOKENS,
    USAGE_THINKING_TOKENS,
)
from storage import database as db

FAKE_AT = "AT-FAKE"
FAKE_RT = "RT-FAKE"
FAKE_AT_GEN2 = "AT-FAKE-GEN2"
FAKE_RT_ROTATED = "RT-FAKE-ROTATED"

# 冻结的 auth.json 样本（spec:287-295 的 schemaVersion=1 / records / 逐字段形状）。
# 键的 ``<account>`` 段是 sha256(authHome\0clientId) 的 base64url（spec:268），
# 本用例只关心前缀 ``com.minimax.mcode.oauth.prod.cn``，故用可读假哈希。
FROZEN_RECORD_KEY = "com.minimax.mcode.oauth.prod.cn\0acct-hash-fake"
FROZEN_AUTH_DOC = {
    "schemaVersion": 1,
    "records": {
        FROZEN_RECORD_KEY: {
            "schemaVersion": 1,
            "accessToken": FAKE_AT,
            "refreshToken": FAKE_RT,
            "tokenType": "Bearer",
            "clientId": "mcode-public",
            "scopes": ["agent.default"],
            "audience": "agent-backend",
            "expiresAtMs": 1_893_427_200_000,  # 2030-01-01 00:00:00 UTC+8
            "generation": 3,
            "subject": "uid-fake-subject",
            "loginEpoch": "epoch-fake-1",
        }
    },
}

# 冻结的 Anthropic SSE 序列（spec:578-593 的事件形状）。
# ⚠️ 每一行的 ``event:`` 标签都**故意**与 data 内层 ``type`` 不一致 —— 回归点：
#    分类只认 data["type"]（仓库唯一的 SSEDecoder 在 upstream/sse.py:105 丢弃 event 行）。
#    若有人把实现改成"读 event 行分派"，本序列会立刻崩成解析错/空流。
FROZEN_ANTHROPIC_SSE = (
    b'event: content_block_delta\n'
    b'data: {"type":"message_start","message":{"id":"msg_frozen_1","model":"MiniMax-M3",'
    b'"usage":{"input_tokens":11,"cache_read_input_tokens":7}}}\n\n'
    b'event: message_start\n'
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"ping"}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_delta","index":0,'
    # 中文用 JSON \uXXXX 转义写（bytes 字面量只能是 ASCII；解析后仍是"先想"）。
    b'"delta":{"type":"thinking_delta","thinking":"\\u5148\\u60f3"}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"pong"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_start","index":1,'
    b'"content_block":{"type":"tool_use","id":"toolu_frozen_1","name":"lookup"}}\n\n'
    b'data: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":"}}\n\n'
    b'data: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"input_json_delta","partial_json":"\\"x\\"}"}}\n\n'
    b'data: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"signature_delta","signature":"sig-fake"}}\n\n'
    b'data: {"type":"content_block_stop","index":1}\n\n'
    b'event: error\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
    b'"usage":{"output_tokens":5,"cache_creation_input_tokens":2}}\n\n'
    b'event: message_start\n'
    b'data: {"type":"message_stop"}\n\n'
)


# ============================================================
# fixtures
# ============================================================

@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    """清进程级全局态 + **焊死出网闸门**。

    清理项：限流表 / 账号失败冷却 / sticky / 刷新负缓存 / 测试 transport。
    照 ``tests/test_rate_limit_failover.py`` 的 autouse 清理写；漏掉任何一项都会让
    用例随执行顺序飘红（例如上一个用例把账号打 expired 后下一个用例选不到号）。

    出网闸门（风控红线）：把 chat 与 token 两条链路的 client 工厂默认换成
    "**拒绝一切真实请求**"的 MockTransport，任何忘记装假 transport 的新用例都会
    立刻炸成断言失败，而不是悄悄向 MiniMax 生产 API 发包。用例里显式
    ``chat.set_transport(...)`` / ``monkeypatch token.get_client`` 会覆盖本兜底。
    """
    from providers import trae_shared
    from providers.minimax_code import token as token_module

    def _offline(request: httpx.Request):
        raise AssertionError(f"测试禁止真实网络请求：{request.method} {request.url}")

    rate_limits.clear()
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    trae_shared.reset_refresh_failures()
    chat.set_transport(httpx.MockTransport(_offline))
    monkeypatch.setattr(
        token_module, "get_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_offline)),
    )
    yield
    chat.set_transport(None)
    rate_limits.clear()
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    trae_shared.reset_refresh_failures()


@pytest.fixture()
def auth_root():
    """仓库内临时目录（不用 pytest 的 ``tmp_path``）。

    同 ``tests/conftest.py`` 的 isolated_db 注释：系统 TEMP 根在沙箱下不可枚举，
    ``tmp_path`` 建目录会慢/失败；仓库 ``.tmp/`` 下自建唯一子目录，测完即删。
    """
    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"minimax-code-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    yield workdir
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture()
def frozen_now(monkeypatch):
    """冻结 ``rate_limits`` 时钟（epoch 秒），返回可推进的 state dict。"""
    state = {"now": 1_800_000_000.0}
    monkeypatch.setattr(rate_limits, "_now", lambda: state["now"])
    return state


@pytest.fixture()
def no_retry_delay(monkeypatch):
    """把退避换成零延迟（本文件只验证语义，不验证睡多久）。"""
    calls: list[tuple] = []

    async def _delay(attempt, retry_after=None):
        calls.append((attempt, retry_after))

    monkeypatch.setattr(chat, "retry_delay", _delay)
    return calls


@pytest.fixture()
def fake_settings(monkeypatch):
    """内存 settings，避免碰真实 DB（同 test_qodercn.py 的 fake_settings）。"""
    store_: dict = {}
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: store_.get(key, default))
    monkeypatch.setattr(db, "set_setting", lambda key, value: store_.__setitem__(key, value))
    monkeypatch.setattr(db, "delete_setting", lambda key: store_.pop(key, None))
    return store_


def _add_account(uid: str = "uid-e2e-1", *, access: str = FAKE_AT, refresh: str = FAKE_RT) -> dict:
    """往隔离 DB 里塞一个 active 账号，返回账号行。"""
    aid = db.add_account({
        "name": f"{CHANNEL_ID}-{uid[:8]}",
        "uid": uid,
        "provider": CHANNEL_ID,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": int(time.time() * 1000) + 3_600_000,
        "status": "active",
        "extra": {"generation": 1},
    })
    return db.get_account(aid)


async def _drain(stream) -> bytes:
    """把 ``chat_completions`` 返回的流式 generator 收干成原始 SSE 字节。"""
    return b"".join([frame async for frame in stream])


def _drain_text(kind_result) -> str:
    """收干流式 generator 并解码成文本（同步包装，供非 async 用例调用）。"""
    _kind, stream = kind_result
    return asyncio.run(_drain(stream)).decode("utf-8")


def _sse_payloads(raw: bytes) -> list[dict]:
    """从对外 SSE 文本里抠出非 [DONE] 的 JSON 帧。"""
    out: list[dict] = []
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:].strip()))
    return out


def _write_auth_sentinel(root: Path) -> Path:
    """在 ``<root>/prod/cn/mcode-public/auth.json`` 写一份哨兵凭证文件（只读快照来源）。"""
    folder = root / "prod" / "cn" / "mcode-public"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / store.CREDENTIALS_FILENAME
    path.write_text(json.dumps(FROZEN_AUTH_DOC, ensure_ascii=False), encoding="utf-8")
    return path


# ============================================================
# 1) store.auth_json_to_account
# ============================================================

def test_auth_json_to_account_maps_frozen_doc():
    """脱敏 auth.json 样本 → provider/uid/expires_at(整数 ms)/extra 键齐全。"""
    account = store.auth_json_to_account(FROZEN_AUTH_DOC, source="frozen")
    assert account is not None
    assert account["provider"] == CHANNEL_ID
    assert account["uid"] == "uid-fake-subject"
    # 仓库约定：expires_at **一律毫秒**（spec:293 expiresAtMs），且必须是 int。
    assert isinstance(account["expires_at"], int)
    assert account["expires_at"] == 1_893_427_200_000
    assert account["account_type"] == "personal"

    extra = account["extra"]
    for key in ("login_epoch", "generation", "scope", "audience", "build_env", "region",
                "client_id", "auth_path", "token_type", "source", "shared_credential"):
        assert key in extra, f"extra 缺键 {key}"
    assert extra["build_env"] == "prod" and extra["region"] == "cn"  # spec:673 硬边界
    assert extra["client_id"] == "mcode-public"                       # spec:235
    assert extra["audience"] == "agent-backend"                       # spec:237
    assert extra["scope"] == "agent.default"                          # spec:236
    assert extra["generation"] == 3                                   # spec:294
    assert extra["login_epoch"] == "epoch-fake-1"                     # spec:294
    assert extra["shared_credential"] is False                        # 粘贴/文档入口非客户端共用
    assert account["domain"] == "agent.minimax.cn"                    # spec:88


def test_auth_json_to_account_keeps_tokens_out_of_diagnostic_surface():
    """凭证红线：token 原文只准在 ``access_token``/``refresh_token`` 两个键上。

    ``extra`` / ``name`` / ``nickname`` / ``uid`` / ``domain`` 是日志与管理页诊断面，
    一旦把 token 抄进去就等于把凭证写进日志（spec:694 的共用凭证尤其致命）。
    """
    account = store.auth_json_to_account(FROZEN_AUTH_DOC, source="frozen")
    assert account is not None
    assert account["access_token"] == FAKE_AT
    assert account["refresh_token"] == FAKE_RT

    credential_keys = {"access_token", "refresh_token"}
    for key, value in account.items():
        if key in credential_keys:
            continue
        assert FAKE_AT not in json.dumps(value, ensure_ascii=False, default=str), key
        assert FAKE_RT not in json.dumps(value, ensure_ascii=False, default=str), key

    # discover/import 的错误串同样不许含原文（这里走的是解析失败分支）。
    _parsed, reason = store._parse_auth_json({"schemaVersion": 1, "records": {"x": {}}})
    assert FAKE_AT not in reason and FAKE_RT not in reason


def test_auth_json_to_account_rejects_unknown_schema_version():
    """文件级与记录级 schemaVersion 不认识 ⇒ None（spec:290，不猜未来格式）。"""
    assert store.auth_json_to_account({"schemaVersion": 2, "records": {}}) is None
    assert store.auth_json_to_account({"records": {}}) is None  # 缺 schemaVersion
    bad_record = json.loads(json.dumps(FROZEN_AUTH_DOC))
    bad_record["records"][FROZEN_RECORD_KEY]["schemaVersion"] = 99
    assert store.auth_json_to_account(bad_record) is None
    assert store.auth_json_to_account("not-a-dict") is None


def test_auth_json_to_account_rejects_foreign_namespace_record_key():
    """第二道硬边界（spec:267,289）：记录键前缀不是 prod.cn 的记录即使被拷进本目录也拒。"""
    doc = json.loads(json.dumps(FROZEN_AUTH_DOC))
    doc["records"] = {
        "com.minimax.mcode.oauth.staging.cn\0acct-hash-fake": doc["records"][FROZEN_RECORD_KEY]
    }
    assert store.auth_json_to_account(doc) is None


# ============================================================
# 2) store.minimax_auth_dirs —— 多实例硬边界
# ============================================================

def test_minimax_auth_dirs_picks_only_prod_cn(monkeypatch, auth_root):
    """只挑 prod/cn/mcode-public；en/staging/test/dev 目录永不入选（spec:8,669）。"""
    root = auth_root / "auth"
    for build_env in ("prod", "staging", "test", "dev"):
        for region in ("cn", "en"):
            (root / build_env / region / "mcode-public").mkdir(parents=True)
    # 干扰项：prod 下别的 clientId、cn 之外的 region 目录。
    (root / "prod" / "cn" / "some-other-client").mkdir(parents=True)
    (root / "prod" / "us" / "mcode-public").mkdir(parents=True)

    monkeypatch.setenv(store.ENV_AUTH_DIR, str(root))
    dirs = [str(p).replace("\\", "/") for p in store.minimax_auth_dirs()]

    assert len(dirs) == 1, dirs
    assert dirs[0].endswith("/prod/cn/mcode-public"), dirs
    for banned in ("/en/", "/staging/", "/test/", "/dev/", "some-other-client", "/prod/us/"):
        assert not any(banned in item for item in dirs), (banned, dirs)


def test_minimax_auth_dirs_override_cannot_escape_prod_cn(monkeypatch, auth_root):
    """env 覆盖指向末段明写着 staging/en 的凭证目录时必须跳过（硬边界不许被覆盖突破）。"""
    root = auth_root / "auth"
    foreign = root / "staging" / "cn" / "mcode-public"
    foreign.mkdir(parents=True)
    (foreign / store.CREDENTIALS_FILENAME).write_text("{}", encoding="utf-8")

    monkeypatch.setenv(store.ENV_AUTH_DIR, str(foreign))
    assert store.minimax_auth_dirs() == []

    # 同一路径换成 prod/cn：覆盖是"目录本身就是凭证目录"的合法形态，必须放行。
    legit = root / "prod" / "cn" / "mcode-public"
    legit.mkdir(parents=True)
    (legit / store.CREDENTIALS_FILENAME).write_text("{}", encoding="utf-8")
    monkeypatch.setenv(store.ENV_AUTH_DIR, str(legit))
    resolved = [Path(p) for p in store.minimax_auth_dirs()]
    assert resolved == [legit]


def test_minimax_auth_dirs_env_key_name_is_the_documented_one():
    """覆盖键名 = CB_MINIMAX_CODE_AUTH_DIR（运维文档/脚本按此拼，改名即静默失效）。"""
    assert store.ENV_AUTH_DIR == "CB_MINIMAX_CODE_AUTH_DIR"


# ============================================================
# 3) translate.build_anthropic_payload
# ============================================================

def test_build_payload_splits_system_and_maps_tool_calls():
    """system 提到顶层数组；assistant.tool_calls→tool_use；role:tool→tool_result。"""
    payload = T.build_anthropic_payload("MiniMax-M3", {
        "model": "MiniMax-M3",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "帮我查"},
            {"role": "assistant", "content": "好的", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
        ],
        "stream": False,
    })
    # system 是顶层数组（spec:176,691），**不**留在 messages 里。
    assert payload["system"] == [{"type": "text", "text": "你是助手"}]
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]

    assistant_blocks = payload["messages"][1]["content"]
    assert assistant_blocks[0] == {"type": "text", "text": "好的"}
    tool_use = assistant_blocks[1]
    assert tool_use["type"] == "tool_use" and tool_use["id"] == "call_1"
    assert tool_use["name"] == "lookup"
    assert tool_use["input"] == {"q": "x"}  # arguments 字符串被解析成对象（spec:563）

    tool_result = payload["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "call_1"
    assert tool_result["content"] == [{"type": "text", "text": "结果"}]
    assert payload["stream"] is False


def test_build_payload_maps_tools_to_input_schema():
    """tools[] → {name,description,input_schema}；缺 parameters 时补空 object。"""
    payload = T.build_anthropic_payload("MiniMax-M3", {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {
                "name": "lookup", "description": "查",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }},
            {"type": "function", "function": {"name": "noargs"}},
        ],
    })
    tools = payload["tools"]
    assert tools[0]["name"] == "lookup"
    assert tools[0]["description"] == "查"
    assert tools[0]["input_schema"] == {
        "type": "object", "properties": {"q": {"type": "string"}}
    }
    assert tools[1]["input_schema"] == {"type": "object", "properties": {}}


def test_build_payload_max_tokens_default_and_clamp():
    """max_tokens 必填（spec:691）：缺失兜底 DEFAULT_MAX_TOKENS，超上限 clamp。"""
    assert T.DEFAULT_MAX_TOKENS == 32_000  # 网关自择兜底（spec:691 未给默认值）
    base = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}
    assert T.build_anthropic_payload("MiniMax-M3", dict(base))["max_tokens"] == T.DEFAULT_MAX_TOKENS
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, max_tokens=0))["max_tokens"] == T.DEFAULT_MAX_TOKENS
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, max_tokens=-5))["max_tokens"] == T.DEFAULT_MAX_TOKENS
    # max_completion_tokens 是 max_tokens 的同义新名
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, max_completion_tokens=77))["max_tokens"] == 77
    # 超目录 limit.output（M3=128000）被 clamp 而不是原样撞上游 400
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, max_tokens=999_999))["max_tokens"] == 128_000


def test_build_payload_thinking_only_for_m3():
    """M3 族发 thinking{adaptive|disabled}（**带 display 伴生字段**）；M2.7 系不发。

    ⚠️ MITM 实测 2026-09-30（dump-001:67-69）把 thinking 的形状补成
    ``{type, display:"summarized"}`` —— 旧断言只比 ``{"type": ...}`` 已不成立；
    display 的专项回归见文末 §11 的 ``test_thinking_carries_display_field``。
    ⚠️ 实测只覆盖 on（adaptive）；下面 disabled 一侧是 spec 外推（形状对齐 on）。
    """
    base = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, thinking={"type": "adaptive"}))["thinking"] == {
        "type": "adaptive", "display": "summarized"}
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, thinking={"type": "disabled"}))["thinking"] == {
        "type": "disabled", "display": "summarized"}
    # 默认（未给开关）跟随目录 default_enabled=True
    assert T.build_anthropic_payload("MiniMax-M3", dict(base))["thinking"] == {
        "type": "adaptive", "display": "summarized"}

    m27 = {"model": "MiniMax-M2.7", "messages": [{"role": "user", "content": "hi"}]}
    for model in ("MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
        out = T.build_anthropic_payload(model, dict(m27, model=model, thinking={"type": "adaptive"}))
        assert "thinking" not in out, model
    # reasoning_effort 不进 thinking（它是另一条载体）：MITM 实测 2026-09-30 证明
    # 推理路径用 output_config.effort（dump-003:1822-1823），thinking 仍是 on/off。
    out = T.build_anthropic_payload("MiniMax-M3", dict(base, reasoning_effort="high"))
    assert out["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert out["output_config"] == {"effort": "high"}  # 档位走 output_config（旧断言"不发"被推翻）


def test_build_payload_keeps_cache_control_but_drops_ttl():
    """cache_control 保留成 {type:"ephemeral"}，长保留 ttl:'1h' 必须剥掉（spec:564）。"""
    payload = T.build_anthropic_payload("MiniMax-M3", {
        "model": "MiniMax-M3",
        "messages": [
            {"role": "system", "content": [
                {"type": "text", "text": "长提示", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            ]},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }}],
    })
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in json.dumps(payload, ensure_ascii=False)

    # 未知缓存类型（spec 未枚举）宁缺毋滥：整块标记不发。
    unknown = T.build_anthropic_payload("MiniMax-M3", {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "x", "cache_control": {"type": "persistent"}},
        ]}],
    })
    assert "cache_control" not in unknown["messages"][0]["content"][0]


def test_build_payload_rejects_document_block():
    """document/file 块显式拒绝（spec:562：上游没有 document content block）。"""
    for part_type in ("document", "file", "input_file"):
        with pytest.raises(T.PayloadError) as excinfo:
            T.build_anthropic_payload("MiniMax-M3", {
                "model": "MiniMax-M3",
                "messages": [{"role": "user", "content": [{"type": part_type, "source": {}}]}],
            })
        assert "spec:562" in str(excinfo.value)

    # system 里的 document 同样拒绝（不是静默丢内容）
    with pytest.raises(T.PayloadError):
        T.build_anthropic_payload("MiniMax-M3", {
            "model": "MiniMax-M3",
            "messages": [
                {"role": "system", "content": [{"type": "document", "source": {}}]},
                {"role": "user", "content": "hi"},
            ],
        })


def test_build_payload_rejects_empty_messages_and_bad_tool_call():
    """Anthropic 要求非空 messages；tool_calls 缺 name 直接报错（不静默丢工具）。"""
    with pytest.raises(T.PayloadError):
        T.build_anthropic_payload("MiniMax-M3", {"model": "MiniMax-M3", "messages": []})
    with pytest.raises(T.PayloadError):
        T.build_anthropic_payload("MiniMax-M3", {
            "model": "MiniMax-M3",
            "messages": [{"role": "assistant", "tool_calls": [{"id": "c", "function": {}}]}],
        })


def test_build_payload_strips_managed_model_prefix():
    """``minimax/MiniMax-M3`` 这类客户端 model-ref 前缀要被剥成上游裸 id（spec:518）。"""
    payload = T.build_anthropic_payload("minimax/MiniMax-M3", {
        "model": "minimax/MiniMax-M3",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert payload["model"] == "MiniMax-M3"


# ============================================================
# 4) 流式状态机：冻结 SSE → OpenAI chunk + usage
# ============================================================

def _pump_frozen_sse() -> tuple[T.AnthropicStreamState, list[dict]]:
    """用仓库唯一的 SSEDecoder 解冻 FROZEN_ANTHROPIC_SSE，喂进状态机。"""
    from upstream.sse import SSEDecoder

    decoder = SSEDecoder()
    payloads = decoder.feed(FROZEN_ANTHROPIC_SSE) + decoder.finish()
    assert not decoder.parser_error, decoder.parser_error

    state = T.AnthropicStreamState(model="MiniMax-M3", chunk_id="chatcmpl-test", created=1)
    chunks: list[dict] = []
    for data in payloads:
        chunks.extend(T.feed(state, data))
    return state, chunks


def test_stream_classifies_by_inner_type_not_event_line():
    """只认 ``data["type"]``：event 标签与内部 type 故意不一致也照样正确分派。"""
    state, chunks = _pump_frozen_sse()

    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    # 首帧 role；随后 thinking→reasoning_content；再 text→content。
    assert deltas[0] == {"role": "assistant"}
    assert {"reasoning_content": "先想"} in deltas
    assert {"content": "pong"} in deltas
    assert state.saw_message_start and state.saw_message_stop
    assert state.text_parts == ["pong"]
    assert state.reasoning_parts == ["先想"]

    # 上游 message id 覆盖出口 chunk id（request 名做 model 覆盖）。
    assert {chunk["id"] for chunk in chunks} == {"msg_frozen_1"}
    assert {chunk["model"] for chunk in chunks} == {"MiniMax-M3"}
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)


def test_stream_accumulates_input_json_delta_into_tool_arguments():
    """input_json_delta 拼进 tool_calls.arguments；signature_delta 丢弃。"""
    state, chunks = _pump_frozen_sse()

    tool_frames = [
        delta["tool_calls"][0]
        for delta in (chunk["choices"][0]["delta"] for chunk in chunks)
        if "tool_calls" in delta
    ]
    # 第一帧报 id/name，后两帧只带 arguments 增量。
    assert tool_frames[0]["id"] == "toolu_frozen_1"
    assert tool_frames[0]["function"]["name"] == "lookup"
    assert tool_frames[0]["index"] == 0
    assert "".join(frame["function"]["arguments"] for frame in tool_frames[1:]) == '{"q":"x"}'

    assert state.tool_calls == [{"id": "toolu_frozen_1", "name": "lookup", "arguments": '{"q":"x"}'}]
    assert state.saw_tool is True
    # signature_delta 在 OpenAI 方言无槽位 ⇒ 显式丢弃，不进任何帧。
    assert "sig-fake" not in json.dumps(chunks, ensure_ascii=False)


def test_stream_finish_state_emits_terminal_chunk_with_usage():
    """终结块（finish_reason + usage 合体）只发一次；usage total = 四字段求和。"""
    state, chunks = _pump_frozen_sse()
    assert len(chunks) == 6  # role / thinking / text / tool_use / 2×input_json

    usage = T.finish_state(state)
    assert usage == {
        "prompt_tokens": 20,           # 11 input + 7 cache_read + 2 cache_creation
        "completion_tokens": 5,
        "total_tokens": 25,            # spec:619 四项之和
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 7, "cache_creation_tokens": 2},
    }
    terminal = T.get_terminal_chunk(state)
    assert terminal["choices"][0]["finish_reason"] == "tool_calls"  # spec:594
    assert terminal["choices"][0]["delta"] == {}
    assert terminal["usage"] == usage
    # 终结块只取一次（重复取返回 None，防止 chat.py 误调两次把结束帧吞掉）。
    assert T.get_terminal_chunk(state) is None


def test_stream_usage_falls_back_to_message_start_when_delta_omits_input_tokens():
    """spec:596：message_delta 可不带 input_tokens，用 message_start 的值兜底。"""
    state = T.AnthropicStreamState(model="MiniMax-M3", created=1)
    T.feed_event(state, {"type": "message_start", "message": {
        "id": "m", "usage": {"input_tokens": 9, "cache_read_input_tokens": 1}}})
    T.feed_event(state, {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                         "usage": {"output_tokens": 4}})
    T.feed_event(state, {"type": "message_stop"})
    usage = T.finish_state(state)
    assert usage["prompt_tokens"] == 10  # 9 + 1（cache 只算一次）
    assert usage["total_tokens"] == 14


def test_parse_event_data_ignores_done_and_garbage():
    """Anthropic 侧**没有** [DONE] 哨兵（spec:595）：字面量与坏 JSON 都按"无事件"处理。"""
    assert T.parse_event_data(b"[DONE]") is None
    assert T.parse_event_data("[DONE]") is None
    assert T.parse_event_data(b"") is None
    assert T.parse_event_data(b"{not json") is None
    assert T.parse_event_data(None) is None
    assert T.parse_event_data(b'{"type":"ping"}') == {"type": "ping"}


# ============================================================
# 5) 截断流必须显式判错（spec:591）
# ============================================================

def test_truncated_stream_raises_explicit_error():
    """没有 message_stop ⇒ AnthropicStreamTruncatedError，绝不静默半截回复。"""
    state = T.AnthropicStreamState(model="MiniMax-M3", created=1)
    T.feed_event(state, {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 1}}})
    T.feed_event(state, {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": "half"}})
    with pytest.raises(T.AnthropicStreamTruncatedError) as excinfo:
        T.finish_state(state)
    assert "message_stop" in str(excinfo.value)  # 诊断可定位（spec:591 原文措辞）
    assert state.text_parts == ["half"]          # 半截内容仍在 state 里，供日志诊断

    # 只有 message_stop、没有 message_start 同样不可诊断（spec:587）。
    only_stop = T.AnthropicStreamState(model="MiniMax-M3", created=1)
    T.feed_event(only_stop, {"type": "message_stop"})
    with pytest.raises(T.AnthropicStreamTruncatedError):
        T.finish_state(only_stop)


def test_truncated_stream_end_to_end_never_emits_done(isolated_db, no_retry_delay):
    """端到端：上游 200 但流被截断 ⇒ 成帧报错，且**不**补 [DONE]（那不是成功结束）。"""
    _add_account("uid-truncated")
    truncated = (
        b'data: {"type":"message_start","message":{"id":"m","usage":{"input_tokens":1}}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"half"}}\n\n'
    )
    chat.set_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, content=truncated, headers={"content-type": "text/event-stream"})))

    kind, stream = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        None))
    assert kind == "stream"
    text = _drain_text((kind, stream))
    assert '"error"' in text
    assert "[DONE]" not in text
    assert "message_stop" in text  # 错误文案带 spec:591 的可定位措辞


# ============================================================
# 6) stop_reason 映射 + usage 归一
# ============================================================

def test_stop_reason_mapping_four_values():
    """spec:594 四条映射；None 透传 None；未知词按 stop 兜底（表非封闭集合）。"""
    assert T.map_stop_reason("end_turn") == "stop"
    assert T.map_stop_reason("max_tokens") == "length"
    assert T.map_stop_reason("tool_use") == "tool_calls"
    assert T.map_stop_reason("refusal") == "content_filter"
    assert T.map_stop_reason(None) is None
    assert T.map_stop_reason("gateway_private_value") == "stop"


def test_usage_total_is_sum_of_four_without_double_counting_cache():
    """total = input + output + cache_read + cache_creation；cache 不重复计数。"""
    usage = T.normalize_usage({
        "input_tokens": 11, "output_tokens": 5,
        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 2,
    })
    assert usage["total_tokens"] == 11 + 5 + 7 + 2 == 25
    # prompt 含 cache（OpenAI 不变量 prompt+completion==total），且 cache 只算一次：
    # 若把 cache_read 重复计入，total 会是 32；若 input_tokens 已含 cache，则会是 18。
    assert usage["prompt_tokens"] == 20
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["total_tokens"] not in (18, 32)

    # 脏值/负数/布尔一律归零，不炸也不虚增。
    dirty = T.normalize_usage({"input_tokens": "x", "output_tokens": -3,
                               "cache_read_input_tokens": True, "cache_creation_input_tokens": None})
    assert dirty["total_tokens"] == 0
    assert dirty["prompt_tokens_details"] == {} if "prompt_tokens_details" in dirty else True


def test_usage_normalize_keeps_native_cache_keys_for_credit_source():
    """保留 Anthropic 原生 cache 键：store_common.credit_source_of 靠它判 'live'。"""
    from providers.store_common import credit_source_of

    usage = T.normalize_usage({"input_tokens": 3, "output_tokens": 1,
                               "cache_read_input_tokens": 4})
    assert usage["cache_read_input_tokens"] == 4
    assert usage["cache_creation_input_tokens"] == 0
    assert credit_source_of(usage) == "live"


def test_to_openai_completion_maps_non_stream_body():
    """非流式 Anthropic JSON → OpenAI chat.completion（响应孪生形状，spec:176）。"""
    completion = T.to_openai_completion({
        "id": "msg_ns", "model": "MiniMax-M3",
        "content": [
            {"type": "thinking", "thinking": "想一下"},
            {"type": "text", "text": "答案"},
            {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": 1}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 5},
    }, "MiniMax-M3")
    message = completion["choices"][0]["message"]
    assert message["content"] == "答案"
    assert message["reasoning_content"] == "想一下"
    assert message["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"q": 1}'}
    assert completion["choices"][0]["finish_reason"] == "tool_calls"
    assert completion["usage"]["total_tokens"] == 2 + 3 + 5


# ============================================================
# 7) _classify_error：业务码 / 内层私有码 / 两套信封
# ============================================================

@pytest.mark.parametrize(
    ("label", "status", "payload", "code", "http", "name"),
    [
        # --- 八个业务码（spec:632-639）逐个走一遍 ---
        ("usage_limit", 500, {"status_code": USAGE_LIMIT_EXCEEDED, "status_msg": "用量到顶"},
         USAGE_LIMIT_EXCEEDED, 429, "USAGE_LIMIT_EXCEEDED"),
        ("credits_exhausted", 402, {"error": {"code": LLM_CREDITS_EXHAUSTED, "message": "余额耗尽"}},
         LLM_CREDITS_EXHAUSTED, 402, "LLM_CREDITS_EXHAUSTED"),
        ("rate_limited", 429, {"status_code": LLM_RATE_LIMITED, "status_msg": "限流"},
         LLM_RATE_LIMITED, 429, "LLM_RATE_LIMITED"),
        ("auth_error", 401, {"error": {"type": "authentication_error", "message": "token 失效"}},
         LLM_AUTH_ERROR, 401, "LLM_AUTH_ERROR"),
        ("upstream_error", 503, {"status_code": LLM_UPSTREAM_ERROR, "status_msg": "上游抖动"},
         LLM_UPSTREAM_ERROR, 502, "LLM_UPSTREAM_ERROR"),
        ("migration_error", 500, {"status_code": LLM_MIGRATION_ERROR, "status_msg": "迁移中"},
         LLM_MIGRATION_ERROR, 500, "LLM_MIGRATION_ERROR"),
        ("tpm_limited", 429, {"statusInfo": {"code": LLM_TPM_RATE_LIMITED, "message": "TPM 超限"}},
         LLM_TPM_RATE_LIMITED, 429, "LLM_TPM_RATE_LIMITED"),
        ("cluster_overloaded", 529, {"error": {"type": "overloaded_error", "message": "集群过载"}},
         LLM_CLUSTER_OVERLOADED, 529, "LLM_CLUSTER_OVERLOADED"),
    ],
)
def test_classify_error_business_codes(label, status, payload, code, http, name):
    """业务码逐个钉住：code/对外 HTTP/语义名三者一致（spec:632-639,692）。"""
    classified = chat._classify_error(status, payload)
    assert classified["code"] == code, label
    assert classified["http"] == http, label
    assert classified["name"] == name, label
    assert classified["message"]  # 一定有可诊断文案（绝不空消息）


@pytest.mark.parametrize(
    ("private_code", "business"),
    sorted(UPSTREAM_STATUS_CODE_MAP.items()),
)
def test_classify_error_inner_private_codes_take_priority(private_code, business):
    """内层 MiniMax 私有码压过传输级 HTTP（spec:642-654），并双写原始码。"""
    classified = chat._classify_error(500, {
        "status_code": private_code, "status_msg": "私有码",
        "base_resp": {"status_code": private_code},
    })
    assert classified["code"] == business
    assert classified["upstream_code"] == private_code
    body = chat._error_body(classified)
    assert body["error"]["code"] == business
    assert body["error"]["status_code"] == private_code  # spec:692 双写
    assert body["error"]["provider"] == CHANNEL_ID


def test_classify_error_inner_code_beats_outer_http():
    """决定性顺序：内层业务码 > Anthropic error.type > HTTP 状态（spec:648-654）。"""
    # 外层 HTTP 500（会兜底成 50113），内层 1400010161 必须压过它 → 402/50110。
    classified = chat._classify_error(500, {"responseBody": '{"status_code": 1400010161}'})
    assert classified["code"] == LLM_CREDITS_EXHAUSTED
    assert classified["http"] == 402

    # Anthropic error.type 也要压过 HTTP：403 + permission_error → 50112/401。
    classified = chat._classify_error(403, {"type": "error", "error": {
        "type": "permission_error", "message": "无权限"}})
    assert classified["code"] == LLM_AUTH_ERROR
    assert classified["auth"] is True


def test_classify_error_two_envelopes_each():
    """两套信封各一例：MiniMax base_resp 平铺 + Anthropic error 对象。"""
    minimax = chat._classify_error(429, {
        "base_resp": {"status_code": 50111, "status_msg": "将在 2030-01-01 00:00:00 UTC+8 重置"}})
    assert minimax["code"] == LLM_RATE_LIMITED
    assert minimax["rate_limited"] is True and minimax["quota"] is False
    assert "重置" in minimax["message"]

    anthropic = chat._classify_error(429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": "将在 2030-01-01 00:00:00 UTC+8 重置"}})
    assert anthropic["code"] == LLM_RATE_LIMITED
    assert anthropic["type"] == "rate_limit_error"
    assert anthropic["upstream_code"] is None  # Anthropic 信封只有字符串类型，无数字码


def test_classify_error_quota_family_does_not_switch_account():
    """额度族（42212/50110）登记但**本请求不换号重放**（spec:660 Do NOT retry）。"""
    for code in (USAGE_LIMIT_EXCEEDED, LLM_CREDITS_EXHAUSTED):
        classified = chat._classify_error(429, {"status_code": code, "status_msg": "x"})
        assert classified["quota"] is True
        assert classified["rate_limited"] is True   # 两族都进 rate_limits 登记
        assert classified["retryable"] is False     # constants 逐码注明 retryable
    assert chat.QUOTA_SWITCH_ACCOUNT is False


def test_classify_error_http_fallback_and_plain_text():
    """认不出任何信封时退回 HTTP 状态分类；纯文本体不炸。"""
    assert chat._classify_error(401, b"")["code"] == LLM_AUTH_ERROR
    assert chat._classify_error(402, b"")["code"] == LLM_CREDITS_EXHAUSTED
    assert chat._classify_error(529, b"")["code"] == LLM_CLUSTER_OVERLOADED
    plain = chat._classify_error(503, "gateway down")
    assert plain["code"] is None and plain["http"] == 503 and plain["message"] == "gateway down"
    assert plain["type"] == "server_error"
    # 400 invalid_request 不硬套业务码：留给 http 原样透传。
    bad = chat._classify_error(400, {"error": {"type": "invalid_request_error", "message": "bad"}})
    assert bad["code"] is None and bad["http"] == 400
    assert bad["type"] == "invalid_request_error"


def test_classify_error_maps_anthropic_error_type_only_stream_events():
    """回归缺口：流内 error 事件常**只带** ``error.type``（无数字码）。

    ``ANTHROPIC_ERROR_TYPE_TO_CODE`` 必须把它翻成业务码，否则对外错误帧缺 ``code``，
    客户端与观测面都无法按业务码分类。overloaded_error → 50151（spec:639 的 529 语义）。
    """
    assert ANTHROPIC_ERROR_TYPE_TO_CODE["overloaded_error"] == LLM_CLUSTER_OVERLOADED

    state = T.AnthropicStreamState(model="MiniMax-M3", created=1)
    T.feed_event(state, {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 1}}})
    with pytest.raises(T.AnthropicStreamError) as excinfo:
        T.feed_event(state, {"type": "error", "error": {
            "type": "overloaded_error", "message": "cluster busy"}})
    exc = excinfo.value
    assert exc.code == LLM_CLUSTER_OVERLOADED == 50151
    assert "cluster busy" in str(exc)

    # 只带 type 的错误负载经 _classify_error 后同样带出业务码（对外成帧路径）。
    classified = chat._classify_error(0, {"error": {"type": "overloaded_error", "message": "busy"}})
    assert classified["code"] == LLM_CLUSTER_OVERLOADED
    assert classified["upstream_code"] is None

    # 词表整体回归：``ANTHROPIC_ERROR_TYPE_TO_CODE`` 的**归属方**是 translate
    # （流内 error 事件的 _describe_upstream_error），每个条目都要能补出业务码。
    for etype, expected in ANTHROPIC_ERROR_TYPE_TO_CODE.items():
        message, code = T._describe_upstream_error({"error": {"type": etype, "message": "m"}})
        assert code == expected, etype
        assert message == "m"

    # chat._classify_error 自己还有一张更窄的表（_ANTHROPIC_ERROR_TYPES）：
    # 只认这几个语义类型，invalid_request_error 显式映射成 0（无业务码、对外 400）。
    for etype, expected in {
        "authentication_error": LLM_AUTH_ERROR,
        "permission_error": LLM_AUTH_ERROR,
        "rate_limit_error": LLM_RATE_LIMITED,
        "overloaded_error": LLM_CLUSTER_OVERLOADED,
        "api_error": LLM_UPSTREAM_ERROR,
    }.items():
        assert chat._classify_error(0, {"error": {"type": etype, "message": "m"}})["code"] == expected, etype

    code_less = chat._classify_error(400, {"error": {"type": "invalid_request_error", "message": "m"}})
    assert code_less["code"] is None          # 客户端请求问题：不硬套业务码
    assert code_less["http"] == 400 and code_less["type"] == "invalid_request_error"
    # 流内路径没有上游 HTTP 状态（已 200 成帧）⇒ 传 0 时走 502 兜底，但 type 仍归位。
    stream_side = chat._classify_error(0, {"error": {"type": "invalid_request_error", "message": "m"}})
    assert stream_side["code"] is None and stream_side["http"] == 502
    assert stream_side["type"] == "invalid_request_error"


def test_stream_error_event_frames_business_code_end_to_end(isolated_db, no_retry_delay):
    """端到端：流内 error 事件 → 对外 SSE 错误帧带 50151（只带 error.type 的路径）。"""
    _add_account("uid-stream-error")
    body = (
        b'data: {"type":"message_start","message":{"id":"m3","usage":{"input_tokens":1}}}\n\n'
        b'data: {"type":"error","error":{"type":"overloaded_error","message":"cluster busy"}}\n\n'
    )
    chat.set_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"})))

    kind, stream = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        None))
    assert kind == "stream"
    text = _drain_text((kind, stream))
    assert '"code": 50151' in text
    assert "cluster busy" in text
    assert FAKE_AT not in text and FAKE_RT not in text  # 凭证不外泄


# ============================================================
# 8) 限流：记录 / 预判跳过 / 全限返回体 / 解除时刻解析
# ============================================================

def test_rate_limit_record_and_preemptive_skip(frozen_now):
    """(账号, 模型) 级登记；预判跳过使该模型在该账号上不再被选中（零上游请求）。"""
    until = rate_limits.record(11, "MiniMax-M3", frozen_now["now"] + 3600)
    assert until == frozen_now["now"] + 3600
    assert rate_limits.is_limited(11, "MiniMax-M3") is True
    assert rate_limits.is_limited(11, "MiniMax-M2.7") is False   # 同账号其它模型不连坐
    assert rate_limits.is_limited(12, "MiniMax-M3") is False     # 同模型其它账号不受影响
    assert rate_limits.limited_account_ids("MiniMax-M3") == {11}

    # 到期惰性清理（无需后台线程）
    frozen_now["now"] += 3601
    assert rate_limits.is_limited(11, "MiniMax-M3") is False
    assert rate_limits.snapshot() == {}


def test_rate_limit_unparseable_reset_falls_back_without_pretending(frozen_now):
    """解析不出解除时刻 ⇒ 落 FALLBACK_COOLDOWN_S，**不假装知道**（spec:711）。"""
    until = chat._record_limit({"id": 22}, "MiniMax-M3", None, {"error": {"message": "too many"}})
    assert until == frozen_now["now"] + rate_limits.FALLBACK_COOLDOWN_S


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        # MiniMax 信封：平铺 status_msg
        ("minimax_status_msg", {"status_code": 50111,
                                "status_msg": "将在 2030-01-01 00:00:00 UTC+8 重置"}),
        # Anthropic 信封：error.message（_reset_text 必须下钻到 error 里）
        ("anthropic_error_message", {"type": "error", "error": {
            "type": "rate_limit_error", "message": "将在 2030-01-01 00:00:00 UTC+8 重置"}}),
    ],
)
def test_reset_epoch_parsed_from_both_envelopes(label, payload):
    """两套信封的"将在 … UTC+8 重置"都要解析成解除时刻（按 UTC+8 减 30s 余量）。

    回归缺口：共享的 ``rate_limits.parse_reset_epoch`` 只认 workbuddy 的 ``msg`` 键，
    不提取本通道两种信封就会把上游给的墙钟解除时刻丢掉，退化成 60s 兜底冷却
    （表现为限流窗口被无谓拉长）。
    """
    expected = 1_893_427_200.0 - rate_limits.RESET_SAFETY_MARGIN_S  # 2030-01-01T00:00:00+08:00
    epoch = chat._reset_epoch(None, payload)
    assert epoch == expected, label
    assert rate_limits.RESET_SAFETY_MARGIN_S == 30

    # 解析不出时返回 None（由 record 落兜底），绝不编造时刻。
    assert chat._reset_epoch(None, {"error": {"message": "too many"}}) is None
    # Retry-After 数字秒是第二来源（spec:711：有就尊重，没有就算）。
    header = httpx.Response(429, headers={"retry-after": "42"})
    assert abs(chat._reset_epoch(header, {}) - (time.time() + 42)) < 5


def test_all_accounts_limited_returns_documented_body_fields(isolated_db, frozen_now, no_retry_delay):
    """全账号受限 ⇒ 429 + 受限视图，字段名逐一对齐 proxy._rate_limit_exhausted_error。"""
    account = _add_account("uid-limited")
    # 入口预判即全限：上游零请求。
    calls: list[str] = []
    chat.set_transport(httpx.MockTransport(
        lambda request: calls.append(str(request.url)) or httpx.Response(200, json={})))

    rate_limits.record(account["id"], "MiniMax-M3", frozen_now["now"] + 3600)
    kind, (status, body) = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        None))

    assert kind == "error" and status == 429
    assert calls == []  # 零上游请求（预判跳过生效）
    error = body["error"]
    for key in ("message", "type", "code", "reset_at", "reset_at_iso",
                "limited_accounts", "tried_account_ids", "provider"):
        assert key in error, f"全限返回体缺字段 {key}"
    assert error["type"] == rate_limits.RATE_LIMIT_TYPE
    assert error["code"] == LLM_RATE_LIMITED
    assert error["provider"] == CHANNEL_ID
    assert error["limited_accounts"][0]["account_id"] == account["id"]
    assert error["reset_at"] == frozen_now["now"] + 3600
    assert error["reset_at_iso"].endswith("UTC+8")


def test_rate_limit_switch_account_on_429(isolated_db, no_retry_delay):
    """限流族命中 ⇒ 登记该账号 + 零延迟换号（不叠加账号级连坐冷却）。

    选号顺序由 auth_manager 的优先级/权重/粘住语义决定（不保证 1 号先中），
    故断言"第一次被选中的账号吃 429、第二次换成**另一个**账号成功"，而不写死 id。
    """
    first = _add_account("uid-rl-1", access="AT-FAKE-RL-1")
    second = _add_account("uid-rl-2", access="AT-FAKE-RL-2")
    by_token = {f"Bearer {first['access_token']}": first, f"Bearer {second['access_token']}": second}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        seen.append(auth)
        if len(seen) == 1:
            return httpx.Response(429, json={"status_code": 50111, "status_msg": "限流"})
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "MiniMax-M3",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    chat.set_transport(httpx.MockTransport(handler))
    kind, completion = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        None))

    assert kind == "json" and completion["choices"][0]["message"]["content"] == "ok"
    # 两次请求打到**不同**账号；零延迟换号（限流族不进退避）。
    assert len(seen) == 2 and seen[0] != seen[1], seen
    assert all(item in by_token for item in seen), seen
    assert no_retry_delay == []
    limited_account = by_token[seen[0]]
    other_account = by_token[seen[1]]
    assert rate_limits.is_limited(limited_account["id"], "MiniMax-M3") is True
    assert rate_limits.is_limited(other_account["id"], "MiniMax-M3") is False
    assert auth_manager.account_is_cooling_down(limited_account["id"]) is False  # 无账号级连坐


# ============================================================
# 9) chat_completions 端到端（httpx.MockTransport，零真实网络）
# ============================================================

def test_chat_completions_non_stream_headers_url_and_no_writeback(
    isolated_db, monkeypatch, auth_root
):
    """假 200 端到端：请求头/URL 形状正确，且**绝不回写**客户端 auth.json。"""
    sentinel = _write_auth_sentinel(auth_root / "auth")
    monkeypatch.setenv(store.ENV_AUTH_DIR, str(sentinel.parent))
    before_bytes = sentinel.read_bytes()
    before_mtime = sentinel.stat().st_mtime_ns

    # 账号由哨兵文件只读导入（证明整条链路只读它）。
    parsed = store.import_discovered(str(sentinel))
    assert parsed["access_token"] == FAKE_AT
    aid = db.add_account(parsed)
    account = db.get_account(aid)
    assert account["extra"]["shared_credential"] is True  # spec:694 与客户端共用

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "msg_e2e", "type": "message", "role": "assistant", "model": "MiniMax-M3",
            "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2}})

    chat.set_transport(httpx.MockTransport(handler))
    kind, completion = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        None))

    assert kind == "json"
    assert completion["choices"][0]["message"]["content"] == "pong"
    assert completion["usage"]["total_tokens"] == 5

    assert len(requests) == 1
    request = requests[0]
    # --- 请求头（spec §3.1:334-375 + §9:3:690）---
    assert request.headers["x-api-key"] == API_KEY_PLACEHOLDER == "sk-xxx"  # 占位符别删
    assert request.headers["Authorization"] == f"Bearer {FAKE_AT}"          # 唯一真实凭证
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["Accept"] == "application/json"                  # 流式也是 json
    assert request.headers["X-Mavis-Agent-Id"] == "main"
    assert "bedrock-lane" not in request.headers and "bedrock_lane" not in request.headers

    # --- URL：/v1 净效果只一份（spec:111-131,689 的头号坑）---
    url = str(request.url)
    assert url == f"https://agent.minimax.cn{CHAT_PATH}"
    assert url.endswith("/mavis/api/v1/llm/v1/messages")
    assert "/v1/v1/" not in url                      # 预置末尾 /v1 已被剥掉，不是叠加
    assert url.count("/v1") == 2                     # 网关前缀 /mavis/api/v1/llm + SDK 的 /v1/messages
    assert CHAT_PATH == "/mavis/api/v1/llm/v1/messages"

    # --- 明文 JSON body（无编码/加密/签名层，spec:417-487）---
    body = json.loads(request.content.decode("utf-8"))
    assert body["model"] == "MiniMax-M3"
    assert body["max_tokens"] > 0 and body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    # --- 只读快照：哨兵文件内容与 mtime 一字不变（spec:694 共存红线）---
    from providers.minimax_code import token as token_module

    assert token_module.WRITEBACK_TO_CLIENT_AUTH_JSON is False
    assert sentinel.read_bytes() == before_bytes
    assert sentinel.stat().st_mtime_ns == before_mtime
    assert not (sentinel.parent / store.AUTH_STATE_FILENAME).exists()
    assert not (sentinel.parent / "auth.lock").exists()
    assert store.minimax_auth_dirs() == [sentinel.parent]


def test_chat_completions_401_refreshes_then_replays_once(isolated_db, monkeypatch, no_retry_delay):
    """假 401 → OAuth 刷新 → **单次**重放（spec:313）；刷新结果只进本网关 DB。"""
    account = _add_account("uid-401")
    upstream_calls: list[str] = []
    token_calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request.headers.get("Authorization", ""))
        if len(upstream_calls) == 1:
            return httpx.Response(401, json={"type": "error", "error": {
                "type": "authentication_error", "message": "token expired"}})
        return httpx.Response(200, json={
            "id": "msg_replay", "type": "message", "role": "assistant", "model": "MiniMax-M3",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        token_calls.append(str(request.url))
        return httpx.Response(200, json={
            "access_token": FAKE_AT_GEN2, "token_type": "Bearer",
            "refresh_token": FAKE_RT_ROTATED, "expires_in": 3600,
            "scope": "agent.default", "audience": "agent-backend"})

    # OAuth 刷新走 storage.http_pool 全局池 ⇒ 只装 chat transport 截不到，必须
    # monkeypatch providers.minimax_code.token.get_client（token.py:57 的共享池入口）。
    from providers.minimax_code import token as token_module

    oauth_client = httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint))
    monkeypatch.setattr(token_module, "get_client", lambda: oauth_client)
    chat.set_transport(httpx.MockTransport(upstream))

    kind, completion = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        None))

    assert kind == "json" and completion["choices"][0]["message"]["content"] == "ok"
    # 恰好两次上游请求：第一次旧 token 401，第二次换新 token 重放（不放大）。
    assert upstream_calls == [f"Bearer {FAKE_AT}", f"Bearer {FAKE_AT_GEN2}"]
    assert len(token_calls) == 1
    assert token_calls[0] == f"https://account.minimax.cn{TOKEN_PATH}"
    # 换代只落本网关 DB（generation+1），且零退避（401 不走通用重试）。
    fresh = db.get_account(account["id"])
    assert fresh["access_token"] == FAKE_AT_GEN2
    assert fresh["refresh_token"] == FAKE_RT_ROTATED
    assert fresh["extra"]["generation"] == 2
    assert fresh["status"] == "active"
    assert no_retry_delay == []


def test_chat_completions_401_without_refresh_material_marks_expired(isolated_db, no_retry_delay):
    """裸 JWT 导入（无 refresh_token）吃 401 ⇒ 标 expired 换号，不做无谓刷新。"""
    account = _add_account("uid-no-rt", refresh="")
    chat.set_transport(httpx.MockTransport(
        lambda request: httpx.Response(401, json={"type": "error", "error": {
            "type": "authentication_error", "message": "expired"}})))

    kind, (status, body) = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        None))

    assert kind == "error" and status == 401
    assert body["error"]["type"] == "authentication_error"
    assert db.get_account(account["id"])["status"] == "expired"
    assert FAKE_AT not in json.dumps(body, ensure_ascii=False)


def test_chat_completions_payload_error_is_local_400(isolated_db, no_retry_delay):
    """请求体不合法（document 块）⇒ 本地 400，**不打上游**、不烧额度。"""
    _add_account("uid-400")
    calls: list[str] = []
    chat.set_transport(httpx.MockTransport(
        lambda request: calls.append(str(request.url)) or httpx.Response(200, json={})))

    kind, (status, body) = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3",
         "messages": [{"role": "user", "content": [{"type": "document", "source": {}}]}],
         "stream": False}, None))

    assert kind == "error" and status == 400
    assert body["error"]["type"] == "invalid_request_error"
    assert calls == []


def test_chat_completions_stream_end_to_end_emits_done(isolated_db, no_retry_delay):
    """流式端到端：增量 → 终结块 → 对外补 [DONE]（上游无此哨兵，spec:595）。"""
    _add_account("uid-stream-ok")
    chat.set_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, content=FROZEN_ANTHROPIC_SSE, headers={"content-type": "text/event-stream"})))

    kind, stream = asyncio.run(chat.chat_completions(
        {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        None))
    assert kind == "stream"
    text = _drain_text((kind, stream))

    assert text.rstrip().endswith("data: [DONE]")
    frames = _sse_payloads(text.encode("utf-8"))
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert frames[-1]["usage"]["total_tokens"] == 25
    assert '"content": "pong"' in text
    assert '"reasoning_content": "先想"' in text
    assert FAKE_AT not in text and FAKE_RT not in text


# ============================================================
# 10) facade 可用性
# ============================================================

CORE_METHODS = (
    "list_models", "alias_map", "accepts_model", "translate_model",
    "pick_account", "pick_account_with_fallback", "has_usable_account",
    "chat_completions", "fetch_model_rates",
)


def test_facade_exposes_nine_core_methods():
    """9 个核心方法 + 3 个属性齐备；异步方法必须是 coroutine function。"""
    import inspect

    for name in CORE_METHODS:
        method = getattr(PROVIDER, name, None)
        assert callable(method), f"facade 缺核心方法 {name}"
    for name in ("pick_account_with_fallback", "has_usable_account", "chat_completions"):
        assert inspect.iscoroutinefunction(getattr(PROVIDER, name)), f"{name} 应为 async"
    assert PROVIDER.id == CHANNEL_ID == "minimax_code"  # 注册 id 用下划线
    assert PROVIDER.display_name == DISPLAY_NAME
    assert PROVIDER.checkin_supported is False          # 无签到面


def test_facade_fetch_quota_is_honestly_unsupported():
    """``fetch_quota`` 恒 unsupported=True 且**零探测请求**（spec:663 无额度接口）。"""
    calls: list[str] = []
    chat.set_transport(httpx.MockTransport(
        lambda request: calls.append(str(request.url)) or httpx.Response(200, json={})))

    snapshot = asyncio.run(PROVIDER.fetch_quota({"id": 7}))
    assert snapshot.ok is False
    assert snapshot.unsupported is True
    assert snapshot.remaining is None      # KD-10：跨通道求和不得把"不知道"当 0
    assert snapshot.channel == CHANNEL_ID
    assert snapshot.account_id == 7
    assert snapshot.unit == "unknown"
    assert snapshot.message == "no quota API"
    assert calls == []


def test_facade_models_aliases_and_translation():
    """facade 的模型面：目录四档、别名翻回原生 id、目录外 id 宽松兜底。

    ⚠️ MITM 实测 2026-09-30（dump-003:53）：默认模型已改为
    ``MiniMax-M3.1-Flash-Preview``（旧断言写死 ``MiniMax-M3`` 已不成立），
    目录随之多出该档；模型目录的专项回归见文末 §11。
    """
    assert [item["id"] for item in PROVIDER.list_models()] == list(STATIC_MODELS)
    for item in PROVIDER.list_models():
        assert item["display_name"] and item["max_input_tokens"] > 0
    assert PROVIDER.alias_map() == dict(ALIASES)
    assert PROVIDER.accepts_model("auto") is True
    assert PROVIDER.translate_model("auto") == DEFAULT_MODEL == "MiniMax-M3.1-Flash-Preview"
    assert PROVIDER.translate_model("minimax/MiniMax-M2.7") == "MiniMax-M2.7"
    # 目录外 id 走宽松兜底（spec:520,710 的远端目录），不编造能力。
    assert PROVIDER.accepts_model("MiniMax-M3.1") is False
    assert chat.model_meta("MiniMax-M3.1")["display_name"] == "MiniMax-M3.1"


def test_facade_fetch_model_rates_is_honest_about_missing_pricing():
    """没有官方倍率表 ⇒ rate=None + official=False（spec 全篇无 pricing 接口）。"""
    rates = PROVIDER.fetch_model_rates()
    assert [item["id"] for item in rates] == list(STATIC_MODELS)
    for item in rates:
        assert item["rate"] is None
        assert item["official"] is False
        assert item["context_window"] > 0
        assert item["max_output_tokens"] > 0


def test_facade_parse_credentials_adapts_paste_wrapping():
    """管理页把粘贴内容平铺进请求体（{"api_key": "<jwt>"}）时门面要能取出原文。"""
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": "uid-pasted", "exp": 1_893_427_200, "scope": "agent.default",
    }).encode("utf-8")).decode("ascii").rstrip("=")
    jwt = f"header.{payload}.signature"

    wrapped = PROVIDER.parse_credentials({"api_key": jwt})
    assert wrapped["uid"] == "uid-pasted"
    assert wrapped["access_token"] == jwt
    assert wrapped["refresh_token"] == ""          # 裸 JWT 无换票素材（store 已注明）
    assert wrapped["extra"]["import_shape"] == "bare_jwt"
    assert wrapped["extra"]["can_refresh"] is False
    assert wrapped["expires_at"] == 1_893_427_200_000

    # 已是凭证文档形状时原样交给 store（不被包装键嗅探打偏）。
    direct = PROVIDER.parse_credentials(json.dumps(FROZEN_AUTH_DOC))
    assert direct["uid"] == "uid-fake-subject"


# ============================================================
# 11) MITM 实测 2026-09-30 回归（抓包已结束；全部离线，零真实请求）
# ------------------------------------------------------------
# 证据来源：.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-00{1,2,3}.json
#   * 003 = POST /v1/messages（主请求，HTTP/2 + TLSv1.3，1 条真实消息、零重放）
#   * 001/002 = POST /v1/messages/count_tokens（对照，本通道不实现）
# 每个用例都对应一条"实测把静态推断推翻/补全"的缺口，不是覆盖率凑数。
# 全程只用假凭证；凭证在 dump 里已脱敏为 ***，本文件不还原、不落任何 token。
# ============================================================

# 实测会话 id（dump-003:34）：mvs_ + 32 位小写 hex。
# 这里只用它的**形状**做正则，不把抓包里的 id 当测试输入。
SESSION_ID_RE = re.compile(r"^mvs_[0-9a-f]{32}$")


def test_session_id_shape_is_mvs_plus_32_hex():
    """缺口 1 回归：``new_session_id()`` 恒为 ``mvs_`` + 32 位小写 hex。

    MITM 实测 2026-09-30（dump-003:34 / dump-001:31 / dump-002:31）：
    ``x-mavis-session-id: mvs_312d6855b7a74b4990b9faf170aecd4f``。
    旧实现是 ``str(uuid.uuid4())``（带连字符、无前缀）⇒ 形状不符。
    多次调用都断言（防止"只是恰好第一次对"的假修复），并覆盖两条出网路径：
    ``request_headers`` 缺省生成、以及显式传入的 id 被原样保留。
    """
    assert MAVIS_SESSION_ID_PREFIX == "mvs_"
    assert MAVIS_SESSION_ID_HEX_LEN == 32

    seen: set[str] = set()
    for _ in range(64):
        session_id = chat.new_session_id()
        assert SESSION_ID_RE.match(session_id), session_id
        assert "-" not in session_id           # 旧值 uuid4 带连字符 ⇒ 这条直接钉死回归
        assert session_id == session_id.lower()  # 实测是小写 hex
        seen.add(session_id)
    assert len(seen) == 64                     # 每请求一个，不复用（实测的会话级语义见 docstring）

    # 请求头里的实际取值同样满足形状（两条路径共用同一个生成点）。
    headers = chat.request_headers({"access_token": FAKE_AT})
    assert SESSION_ID_RE.match(headers["X-Mavis-Session-Id"]), headers["X-Mavis-Session-Id"]
    # 显式传入的 session_id 原样透传（401 重放沿用同一个 id，不新开会话）。
    pinned = chat.request_headers({"access_token": FAKE_AT}, session_id="mvs_" + "0" * 32)
    assert pinned["X-Mavis-Session-Id"] == "mvs_" + "0" * 32


def test_request_headers_send_dangerous_direct_browser_access_without_beta(
    isolated_db, no_retry_delay
):
    """缺口 2 回归：实测必发 ``anthropic-dangerous-direct-browser-access: true``，且**无** beta。

    MITM 实测 2026-09-30（dump-003:27）：主请求 24 个头里有该头；而
    ``capture.jsonl:8`` 的 ``headerPresence["anthropic-beta"] = null`` 且 dump-003
    全文无 ``anthropic-beta`` ⇒ 两者**不是**配套关系，别顺手加 beta。
    反面断言（``anthropic-beta`` 必须缺席）是本节的重点：只断言"有前者"会漏掉
    "有人为了保险把 beta 一起加上"这种回归。
    """
    headers = chat.request_headers({"access_token": FAKE_AT})
    assert HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS == "anthropic-dangerous-direct-browser-access"
    assert ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS == "true"
    assert headers[HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS] == "true"
    # 大小写不敏感比对（httpx 头名大小写不敏感，避免用错拼法写出假绿）。
    lowered = {name.lower(): value for name, value in headers.items()}
    assert lowered["anthropic-dangerous-direct-browser-access"] == "true"
    assert HEADER_ANTHROPIC_BETA not in lowered
    assert "anthropic-beta" not in lowered
    assert not any("beta" in name for name in lowered)

    # 端到端：假 200 请求真的把这套头发上去了（头清单不是只活在常量表里）。
    _add_account("uid-mitm-headers")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "id": "msg_h", "type": "message", "role": "assistant", "model": DEFAULT_MODEL,
            "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    chat.set_transport(httpx.MockTransport(handler))
    kind, _completion = asyncio.run(chat.chat_completions(
        {"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": False},
        None))
    assert kind == "json"
    assert len(requests) == 1
    sent = {name.lower(): value for name, value in requests[0].headers.items()}
    assert sent["anthropic-dangerous-direct-browser-access"] == "true"
    assert "anthropic-beta" not in sent
    assert SESSION_ID_RE.match(sent["x-mavis-session-id"]), sent["x-mavis-session-id"]


def test_build_payload_always_sends_output_config_effort():
    """缺口 3 回归：无 ``response_format`` 时也产出 ``output_config == {"effort":"default"}``。

    MITM 实测 2026-09-30（dump-003:1822-1823）：推理路径实测
    ``output_config: {"effort":"default"}`` ⇒ 旧实现 ``SEND_OUTPUT_CONFIG_EFFORT=False``
    恒不发 effort 已被推翻（客户端把"未显式选档"也**显式下发**）。
    映射规则：合法档位透传；"关思考"词（none/minimal/…）与非法值回退 ``default``。
    """
    assert T.SEND_OUTPUT_CONFIG_EFFORT is True
    assert EFFORT_DEFAULT == "default"
    base = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}

    # 没给 reasoning_effort ⇒ 仍然显式发 default（这就是实测形状）。
    assert T.build_anthropic_payload("MiniMax-M3", dict(base))["output_config"] == {"effort": "default"}
    # 合法档位逐个透传（含实测基线 default 与 spec:544 的通用档位）。
    for effort in (EFFORT_DEFAULT,) + EFFORT_LEVELS:
        out = T.build_anthropic_payload("MiniMax-M3", dict(base, reasoning_effort=effort))
        assert out["output_config"] == {"effort": effort}, effort
    # 大小写/空白归一后仍命中白名单。
    assert T.build_anthropic_payload("MiniMax-M3", dict(base, reasoning_effort=" HIGH "))["output_config"] == {
        "effort": "high"}
    # "关思考"词与非法值**绝不原样撞上游**（残余不确定 R03：只白名单透传）。
    for bogus in ("none", "minimal", "disabled", "off", "ultra", "", "   ", 42, True, None, ["high"]):
        out = T.build_anthropic_payload("MiniMax-M3", dict(base, reasoning_effort=bogus))
        assert out["output_config"] == {"effort": "default"}, bogus


def test_build_payload_output_config_format_and_effort_coexist():
    """缺口 3 重点：``response_format=json_schema`` 时 **format 与 effort 共存**、互不覆盖。

    实现里两者合并进同一个 ``output_config``（``setdefault``）—— 最危险的回归是
    其中一条把另一条**整个 dict 覆盖掉**（历史 bug 形状：只有 format、或只有 effort）。
    故这里同时断言：两个键都在、format 内容一字不差、effort 跟随 reasoning_effort。
    """
    base = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    rf = {"response_format": {"type": "json_schema", "json_schema": {"name": "lookup", "schema": schema}}}

    only_format = T.build_anthropic_payload("MiniMax-M3", dict(base, **rf))["output_config"]
    assert only_format["format"] == {"type": "json_schema", "schema": schema}
    assert only_format["effort"] == "default"          # format 存在时 effort 也不能丢

    both = T.build_anthropic_payload("MiniMax-M3", dict(base, **rf, reasoning_effort="low"))["output_config"]
    assert both == {"format": {"type": "json_schema", "schema": schema}, "effort": "low"}
    # 键集合恰好两个：既没丢，也没被塞进未实测的第三个键。
    assert set(both) == {"format", "effort"}

    # 非法 effort 与 format 共存时只把 effort 打回 default，format 一字不变。
    degraded = T.build_anthropic_payload("MiniMax-M3", dict(base, **rf, reasoning_effort="none"))["output_config"]
    assert degraded == {"format": {"type": "json_schema", "schema": schema}, "effort": "default"}

    # json_object 仍显式拒绝（目录未声明 support_json_object_output，spec:565），不因 effort 改动而放宽。
    with pytest.raises(T.PayloadError):
        T.build_anthropic_payload("MiniMax-M3", dict(base, response_format={"type": "json_object"}))


def test_model_catalog_gains_m3_1_flash_preview_as_default():
    """缺口 4 回归：``MiniMax-M3.1-Flash-Preview`` 进目录且是默认；原三档仍在。

    MITM 实测 2026-09-30（dump-003:53 / dump-001:42 / dump-002:42 三处一致）：
    客户端在用的模型就是这一档，而旧 catalog 没有它 ⇒ 请求会落到宽松兜底（能力位全靠猜）。
    同时钉住 ``max_tokens=128000``（dump-003:68）与 ``ALIASES["auto"]`` 的指向。
    """
    assert "MiniMax-M3.1-Flash-Preview" in STATIC_MODELS
    assert DEFAULT_MODEL == "MiniMax-M3.1-Flash-Preview"
    assert STATIC_MODELS[0] == DEFAULT_MODEL                    # 目录首位 = 默认档
    assert ALIASES["auto"] == DEFAULT_MODEL                     # auto 别名指向它
    assert ALIASES["minimax/MiniMax-M3.1-Flash-Preview"] == DEFAULT_MODEL
    assert PROVIDER.translate_model("auto") == DEFAULT_MODEL

    # 原三档一个都没被删（仍是可路由目录项）。
    for legacy in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
        assert legacy in STATIC_MODELS, legacy
        assert ALIASES[f"minimax/{legacy}"] == legacy
        assert PROVIDER.accepts_model(legacy) is True

    entry = chat.model_meta(DEFAULT_MODEL)
    assert entry["id"] == DEFAULT_MODEL and entry["display_name"] == DEFAULT_MODEL
    assert entry["max_output_tokens"] == 128_000                # dump-003:68 max_tokens=128000
    assert entry["tool_call"] is True                           # dump-003 带 27 个 tools
    # 目录外 id 仍走宽松兜底、且**不**进目录（无后缀的 M3.1 实测未见，禁止擅自加）。
    assert PROVIDER.accepts_model("MiniMax-M3.1") is False
    assert "MiniMax-M3.1" not in STATIC_MODELS


def test_thinking_carries_display_field():
    """缺口 5 回归：产出的 thinking 带 ``display: "summarized"`` 伴生字段。

    MITM 实测 2026-09-30（dump-001:67-69 / dump-002:1783-1785，count_tokens）：
    ``thinking: {"type":"adaptive","display":"summarized"}`` —— 旧静态假设只知 ``type``。
    ⚠️ 实测只观测到 **on（adaptive）** 一侧：3 个 dump 里 `disabled` 的命中全是工具
    描述文本、不是 thinking 取值。off 侧带 display 属 **spec 外推**（形状与 on 对齐），
    故 off 的断言只锁定"与 on 形状一致"，不声称它是实测结论。
    """
    assert THINKING_DISPLAY_FIELD == "display"
    assert THINKING_DISPLAY_SUMMARIZED == "summarized"
    base = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}

    on = T.build_anthropic_payload("MiniMax-M3", dict(base, thinking={"type": "adaptive"}))["thinking"]
    off = T.build_anthropic_payload("MiniMax-M3", dict(base, thinking={"type": "disabled"}))["thinking"]
    assert on == {"type": "adaptive", "display": THINKING_DISPLAY_SUMMARIZED}   # 实测侧
    assert off == {"type": "disabled", "display": THINKING_DISPLAY_SUMMARIZED}  # spec 外推侧
    # 值域不编造：实测只见 summarized，别扩成 low/high 之类的臆测档。
    assert on["display"] == "summarized" and off["display"] == "summarized"

    # 目录里两个 M3 族条目的 variants 同样带 display（目录与翻译层不得漂移）。
    from providers.minimax_code.constants import MODEL_CATALOG

    for model in ("MiniMax-M3", "MiniMax-M3.1-Flash-Preview"):
        variants = MODEL_CATALOG[model]["variants"]
        assert variants["thinking"]["thinking"][THINKING_DISPLAY_FIELD] == THINKING_DISPLAY_SUMMARIZED, model
        assert variants["none-thinking"]["thinking"][THINKING_DISPLAY_FIELD] == THINKING_DISPLAY_SUMMARIZED, model


def test_tools_all_carry_eager_input_streaming():
    """缺口 6 回归：每个 tool 都带 ``eager_input_streaming is True``（27/27 实测）。

    MITM 实测 2026-09-30（dump-003:83，共 27 处）：推理路径的 tools 全部带该字段，
    而两次 count_tokens 的同一批 tool **0/27 带** ⇒ 推理路径专属，且**不依赖**
    ``anthropic-beta`` 头（实测该头为 null）。旧 TODO"属 beta 特性故不发"已被推翻。
    """
    assert EAGER_INPUT_STREAMING is True
    payload = T.build_anthropic_payload(DEFAULT_MODEL, {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {
                "name": f"tool_{index}",
                "description": f"第 {index} 个",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }}
            for index in range(27)   # 与实测规模一致（27 个工具）
        ],
    })
    tools = payload["tools"]
    assert len(tools) == 27
    for tool in tools:
        assert tool["eager_input_streaming"] is True, tool["name"]
    # 一个都不能漏：字段出现次数 == 工具数（防"只给第一个加"的假修复）。
    assert json.dumps(payload).count('"eager_input_streaming": true') == 27
    # 该字段不依赖 anthropic-beta：请求头清单里没有它（与缺口 2 的断言互相印证）。
    assert HEADER_ANTHROPIC_BETA not in chat.request_headers({"access_token": FAKE_AT})


def test_usage_extracts_thinking_tokens_without_changing_total():
    """缺口 7 回归：``output_tokens_details.thinking_tokens`` 被提取，**total 口径不变**。

    MITM 实测 2026-09-30（capture.jsonl:11 / dump-003 响应）：
    ``usage: {"input_tokens":21769,"output_tokens":85,"cache_read_input_tokens":2627,
    "output_tokens_details":{"thinking_tokens":57}}``。
    ``thinking_tokens`` 是 ``output_tokens`` 的**子集**（57/85）⇒ 绝不能加进 total
    （total 恒 = input + output + cache_read + cache_creation 四项求和）。
    """
    assert USAGE_OUTPUT_TOKENS_DETAILS == "output_tokens_details"
    assert USAGE_THINKING_TOKENS == "thinking_tokens"

    # 与实测同形的样本（只搬数字形状，不含任何凭证）。
    sample = {
        "input_tokens": 21_769, "output_tokens": 85,
        "cache_read_input_tokens": 2_627,
        "output_tokens_details": {"thinking_tokens": 57},
    }
    usage = T.normalize_usage(sample)
    assert usage[USAGE_OUTPUT_TOKENS_DETAILS] == {USAGE_THINKING_TOKENS: 57}      # 原生实测键保留
    assert usage[USAGE_COMPLETION_TOKENS_DETAILS] == {USAGE_REASONING_TOKENS: 57}  # OpenAI 风格槽位
    # 口径不变：total 仍是四项求和，**不含** thinking_tokens。
    assert usage["total_tokens"] == 21_769 + 85 + 2_627 == 24_481
    assert usage["total_tokens"] != 24_481 + 57
    assert usage["completion_tokens"] == 85          # 不因 thinking 变成 85+57
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    # 无 thinking 时两个明细键都不出现（不虚增、不塞 0）。
    plain = T.normalize_usage({"input_tokens": 3, "output_tokens": 1})
    assert USAGE_OUTPUT_TOKENS_DETAILS not in plain
    assert USAGE_COMPLETION_TOKENS_DETAILS not in plain

    # 流式路径：thinking_tokens 只在 message_start 或只在 message_delta 时都要能合并上来。
    start_only = T.AnthropicStreamState(model=DEFAULT_MODEL, created=1)
    T.feed_event(start_only, {"type": "message_start", "message": {
        "id": "msg_t1", "usage": dict(sample)}})
    T.feed_event(start_only, {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                              "usage": {"output_tokens": 85}})
    T.feed_event(start_only, {"type": "message_stop"})
    merged = T.finish_state(start_only)   # 返回**归一 usage 本体**（不是包一层的 dict）
    assert merged[USAGE_OUTPUT_TOKENS_DETAILS] == {USAGE_THINKING_TOKENS: 57}
    assert merged["total_tokens"] == 24_481

    delta_only = T.AnthropicStreamState(model=DEFAULT_MODEL, created=1)
    T.feed_event(delta_only, {"type": "message_start", "message": {
        "id": "msg_t2", "usage": {"input_tokens": 10}}})
    T.feed_event(delta_only, {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                              "usage": {"output_tokens": 4,
                                        "output_tokens_details": {"thinking_tokens": 2}}})
    T.feed_event(delta_only, {"type": "message_stop"})
    final = T.finish_state(delta_only)
    assert final[USAGE_OUTPUT_TOKENS_DETAILS] == {USAGE_THINKING_TOKENS: 2}
    assert final["total_tokens"] == 10 + 4        # 仍不含 thinking_tokens


def test_opaque_token_falls_back_to_login_epoch_uid(monkeypatch, tmp_path):
    """实机回归：``accessToken`` 是不透明串 ⇒ 回退 ``loginEpoch`` 做 uid 才能去重。

    2026-09-30 本机真实 ``auth.json`` 实测：记录只有 accessToken/refreshToken/
    clientId/expiresAtMs/generation/loginEpoch/audience/scopes/tokenType/schemaVersion，
    **没有** ``subject``/``accountId``，且 ``accessToken`` 是 **60 字符、只有 1 段、
    不是 JWT**（base64 解不出 header ⇒ ``_jwt_claims`` 恒空）⇒ uid 恒为空。

    后果实测过：连导 2 次得到 **2 行**（``store_common.upsert_account`` 只在 uid
    非空时匹配）。修法是回退 ``loginEpoch``（spec:294 同一次登录内不变；刷新只轮
    ``generation`` 不动它）。这里用与真实记录**同形**的合成数据锁定该行为。
    """
    import time
    from providers.minimax_code import store as S

    opaque = "x" * 60   # 与实测同长、同样只有 1 段（不是 JWT）
    doc = {"schemaVersion": 1, "records": {
        "mcode-public": {
            "accessToken": opaque, "refreshToken": "y" * 60,
            "clientId": "mcode-public", "audience": "agent-backend",
            "expiresAtMs": int(time.time() * 1000) + 3_600_000,
            "generation": 7, "loginEpoch": "ef8e9a66-082b-4263-ba0d-571f58610883",
            "scopes": ["agent.default"], "tokenType": "Bearer", "schemaVersion": 1,
        }
    }}
    parsed, reason = S._parse_auth_json(doc, source="test://auth.json")
    assert parsed is not None and reason == "", reason
    # 关键：uid 非空且锚定在 loginEpoch 上（不是空串、也不是 -user 兜底）。
    assert parsed["uid"] == "loginEpoch:ef8e9a66-082b-4263-ba0d-571f58610883"
    # 不透明 token 不得被误当成 JWT 去解（解不出就当没有 claim，不抛异常）。
    assert parsed["access_token"] == opaque
    # 同一 loginEpoch 两次导入 ⇒ 同一 uid ⇒ upsert 走更新而不是新增。
    again, _ = S._parse_auth_json(doc, source="test://auth.json")
    assert again["uid"] == parsed["uid"]


def test_channel_id_prefix_is_stripped():
    """实机回归：``minimax_code/<model>`` 的**通道 id 前缀**必须被剥掉。

    2026-09-30 真机：``model="minimax_code/auto"`` 整串发给上游 ⇒ 400
    ``invalid params, invalid reasoning_effort: "default" (allowed: low, medium,
    high, xhigh, max) (2013)``。

    ⚠️ 这个错误信息是**误导性的**：真凶是模型名不合法（上游把未知 model 当参数解析），
    不是 effort。同一批真机对照证明 ``model="auto"`` 与显式 ``reasoning_effort=
    "default"`` 都返回 200 ⇒ ``"default"`` 上游是接受的，别再去"修" effort。

    三套前缀不是同一层命名，都要剥：``minimax/``、``minimax_api/``（spec:518 上游
    model-ref 的 provider 名）与 ``minimax_code/``（**本网关通道 id**，OpenAI 客户端
    侧 ``<channel>/<model>`` 写法）。剥离后还必须能落到具体模型（带 thinking），
    不能只剥成 ``"auto"`` 就完事。
    """
    from providers.minimax_code import chat as C

    for spelling in ("auto", "minimax_code/auto", "minimax/auto", "minimax_api/auto",
                     "minimax_code/MiniMax-M3.1-Flash-Preview"):
        inner = C.translate_model(spelling)
        assert inner == DEFAULT_MODEL, f"{spelling} -> {inner}"
        built = T.build_anthropic_payload(inner, {
            "model": spelling, "messages": [{"role": "user", "content": "hi"}]})
        # 关键：发给上游的是具体模型 id，绝不能带任何前缀/保留字。
        assert built["model"] == DEFAULT_MODEL, f"{spelling} -> {built['model']}"
        assert "/" not in built["model"]
        # 落到具体模型 ⇒ 该带的方言字段也跟着对（M3 族发 thinking）。
        assert built["thinking"] == {"type": "adaptive", "display": THINKING_DISPLAY_SUMMARIZED}


# ============================================================
# 12) 从磁盘接管凭据 + 刷新前置接管（客户端自刷新轮转 refresh_token 的保守对策）
# ------------------------------------------------------------
# 实机依据：``.tmp/mitm/minimax-code-20260919/ROTATION-VERDICT.md`` + spec:694,704。
#   客户端每约 1h 自刷新一次，且每次自刷新都会**轮转 refresh_token**（access token
#   实测 TTL ≈ 1h，spec:317 记的"11 天"已推翻）⇒ 网关库内的 refresh_token 变成
#   死票 ⇒ 网关下次 OAuth refresh 被拒（invalid_grant）、账号被判 expired，即
#   "客户端与网关并用时网关会周期性失效"。
# 选定策略（保守）：网关**先**从磁盘只读接管客户端更新后的凭据（不轮转、不顶掉
#   对方），只有磁盘没有更新凭据时才回退原有 OAuth refresh（保留"网关是唯一持有者"
#   时的自刷新能力）。启动对齐 + 轻量定时器 + 刷新前置接管一律 require_newer=True
#   （只认更新、绝不降级）。
# 被测面：``store.adopt_credentials_from_client``（store.py:765-841，语义逐字对齐
#   traework/token.py:140-254）与 ``chat._refresh_to_account``（chat.py:339-383；
#   启动/定时器骨架见 gateway/server.py:172-230）。
# 红线复核：接管只做本地文件读取 + DB 写入，**零网络**；auth.json **只读**（哨兵
#   字节级 + mtime 双重校验）；下面全部只用合成假值，真实 token 原文绝不出现。
# ============================================================

# 合成假值（绝不代表真实 token 形状/内容）：DB 侧沿用本文件既有 FAKE_AT/FAKE_RT，
# 磁盘侧用明显不同的 AT-FROM-DISK / RT-FROM-DISK-ROT（后者模拟客户端轮转后的新票）。
ADOPT_AT_DISK = "AT-FROM-DISK"
ADOPT_RT_DISK = "RT-FROM-DISK-ROT"
ADOPT_AT_OAUTH = "AT-FROM-OAUTH"
ADOPT_RT_OAUTH = "RT-FROM-OAUTH"
# 到期时间：网关库里那张已被客户端轮转成死票的旧票 vs 客户端刚写盘的新票。
EXP_DB_OLD = 1_800_000_000_000
EXP_DISK_NEW = 1_893_427_200_000
# 真实 auth.json 记录没有 subject/accountId ⇒ uid 回退 loginEpoch（见
# test_opaque_token_falls_back_to_login_epoch_uid），接管核对的也是这个派生 uid。
ADOPT_LOGIN_EPOCH = "xyz-adopt-fake"
ADOPT_UID = f"loginEpoch:{ADOPT_LOGIN_EPOCH}"


def _client_auth_doc(
    *,
    access: str,
    refresh: str,
    expires_at_ms: int,
    login_epoch: str = ADOPT_LOGIN_EPOCH,
    generation: int = 10,
) -> dict:
    """一份与本机实测**同形**的合成 auth.json（generation 递增见 ROTATION-VERDICT）。"""
    return {
        "schemaVersion": 1,
        "records": {
            FROZEN_RECORD_KEY: {
                "schemaVersion": 1,
                "accessToken": access,
                "refreshToken": refresh,
                "tokenType": "Bearer",
                "clientId": "mcode-public",
                "scopes": ["agent.default"],
                "audience": "agent-backend",
                "expiresAtMs": expires_at_ms,
                "generation": generation,
                "loginEpoch": login_epoch,
            }
        },
    }


def _write_client_auth(
    root: Path, *, access: str, refresh: str, expires_at_ms: int,
    login_epoch: str = ADOPT_LOGIN_EPOCH,
) -> Path:
    """在 ``<root>/prod/cn/mcode-public/auth.json`` 落客户端凭据哨兵文件，返回其路径。"""
    folder = root / "prod" / "cn" / "mcode-public"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / store.CREDENTIALS_FILENAME
    path.write_text(
        json.dumps(
            _client_auth_doc(
                access=access, refresh=refresh, expires_at_ms=expires_at_ms,
                login_epoch=login_epoch,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _adopt_account(
    auth_path,
    *,
    uid: str = ADOPT_UID,
    access: str = FAKE_AT,
    refresh: str = FAKE_RT,
    expires_at: int = EXP_DB_OLD,
    status: str = "active",
) -> dict:
    """往隔离 DB 塞一个带（或不带）``extra.auth_path`` 的网关账号，返回账号行。"""
    extra: dict = {"generation": 9}
    if auth_path is not None:
        extra["auth_path"] = str(auth_path)
    aid = db.add_account({
        "name": f"{CHANNEL_ID}-adopt",
        "uid": uid,
        "provider": CHANNEL_ID,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": int(expires_at),
        "status": status,
        "extra": extra,
    })
    return db.get_account(aid)


def test_adopt_credentials_newer_token(isolated_db, monkeypatch, auth_root):
    """磁盘票更新（expires_at 更大）⇒ 接管 True 且只写非空字段；**客户端文件一字未动**。

    本策略的主干：接管 = 读盘 + 写我们自己的 DB，绝不回写 auth.json（spec:694 共存
    红线）。store.py:765-841 的 patch 只写非空 access/refresh/expires_at + status
    active，其余列与 extra 原样保留 —— 不整行覆盖。
    """
    path = _write_client_auth(
        auth_root / "auth",
        access=ADOPT_AT_DISK, refresh=ADOPT_RT_DISK, expires_at_ms=EXP_DISK_NEW,
    )
    monkeypatch.setenv(store.ENV_AUTH_DIR, str(path.parent))  # 只把临时目录纳入白名单
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    account = _adopt_account(path)  # 库内：死票 + 更旧到期
    assert account["access_token"] == FAKE_AT and account["expires_at"] == EXP_DB_OLD

    assert store.adopt_credentials_from_client(account, require_newer=True) is True

    after = db.get_account(account["id"])
    assert after["access_token"] == ADOPT_AT_DISK
    assert after["refresh_token"] == ADOPT_RT_DISK  # 客户端轮转后的新 refresh 也接管进来
    assert after["expires_at"] == EXP_DISK_NEW
    assert after["status"] == "active"
    # 不整行覆盖：uid / provider / extra（含 provenance 的 auth_path）都得留着。
    assert after["uid"] == ADOPT_UID and after["provider"] == CHANNEL_ID
    assert after["extra"]["auth_path"] == str(path)

    # 只读快照红线（哨兵字节级 + mtime 双重校验，与 §9 端到端用例同一手法）。
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert not (path.parent / store.AUTH_STATE_FILENAME).exists()

    # 接管全程不碰网络：autouse 的"拒绝一切真实请求"闸门一次都没被触发。
    # 凭证也不得被抄进诊断面。
    assert ADOPT_AT_DISK not in json.dumps(after["extra"], ensure_ascii=False, default=str)


def test_adopt_never_downgrades(isolated_db, monkeypatch, auth_root):
    """磁盘票更旧 ⇒ require_newer=True 必须 False 且 DB 不变（绝不把好票换成旧票）。

    极性出处：``store._client_credentials_updated``（逐字对齐 traework/token.py:140-160）
    —— 启动对齐/定时器调用时**并未发生鉴权失败**，只凭"token 不同"就接管会把网关
    刚刷好的新票换成客户端手里的旧票。
    """
    path = _write_client_auth(
        auth_root / "auth",
        access=ADOPT_AT_DISK, refresh=ADOPT_RT_DISK, expires_at_ms=EXP_DB_OLD,
    )
    monkeypatch.setenv(store.ENV_AUTH_DIR, str(path.parent))
    account = _adopt_account(path, expires_at=EXP_DISK_NEW)  # 网关手里的票更新

    assert store.adopt_credentials_from_client(account, require_newer=True) is False

    after = db.get_account(account["id"])
    assert after["access_token"] == FAKE_AT
    assert after["refresh_token"] == FAKE_RT
    assert after["expires_at"] == EXP_DISK_NEW  # 没被换成更旧的值

    # 对照组：自愈极性（require_newer=False）在"票不同"时才允许接管——
    # 这条差异就是 require_newer 开关的全部意义（traework 有同款对照用例）。
    assert store.adopt_credentials_from_client(after, require_newer=False) is True
    assert db.get_account(account["id"])["access_token"] == ADOPT_AT_DISK


def test_adopt_rejects_other_uid(isolated_db, monkeypatch, auth_root):
    """磁盘凭据属于另一个账号 ⇒ 拒绝接管（uid 一致是硬前提，防读串号）。"""
    path = _write_client_auth(
        auth_root / "auth",
        access=ADOPT_AT_DISK, refresh=ADOPT_RT_DISK, expires_at_ms=EXP_DISK_NEW,
        login_epoch="someone-else-epoch",
    )
    monkeypatch.setenv(store.ENV_AUTH_DIR, str(path.parent))
    account = _adopt_account(path)  # 本账号 uid 锚在 ADOPT_LOGIN_EPOCH 上

    assert store.adopt_credentials_from_client(account, require_newer=True) is False

    after = db.get_account(account["id"])
    assert after["access_token"] == FAKE_AT and after["refresh_token"] == FAKE_RT
    assert after["expires_at"] == EXP_DB_OLD


def test_adopt_no_auth_path_is_noop(isolated_db):
    """纯粘贴账号（extra 没有 auth_path）⇒ False、不抛异常（best-effort 红线）。

    该分支在请求路径上必须安静：粘贴进来的凭据本就没有"客户端素材"可接管，回退
    原有 OAuth refresh 是调用方的事。
    """
    account = _adopt_account(None)
    assert "auth_path" not in account["extra"]

    assert store.adopt_credentials_from_client(account, require_newer=True) is False

    after = db.get_account(account["id"])
    assert after["access_token"] == FAKE_AT and after["refresh_token"] == FAKE_RT
    assert after["status"] == "active"


def test_refresh_to_account_adopts_before_oauth(isolated_db, monkeypatch):
    """刷新入口**先接管**：接管成功即返回 DB 新账号，绝不触发 OAuth refresh。

    这是"客户端与网关并用时网关周期性失效"的正解（ROTATION-VERDICT 策略 1/3）：
    拿库内那张已被轮转的死票去 POST /oauth2/token 只会 invalid_grant 并把账号判死，
    而磁盘上的新票是零成本、零风控面的。桩 refresh_account 一旦被 await 就抛
    AssertionError ⇒ 任何回退都当场红。
    极性同样钉住：接管必须以 require_newer=True 调用（只认更新、绝不降级）。
    """
    account = _adopt_account(None)
    polarities: list[bool] = []

    def fake_adopt(target: dict, *, require_newer: bool) -> bool:
        polarities.append(require_newer)
        db.update_account(
            int(target["id"]),
            {
                "access_token": ADOPT_AT_DISK,
                "refresh_token": ADOPT_RT_DISK,
                "expires_at": EXP_DISK_NEW,
                "status": "active",
            },
        )
        return True

    async def forbidden_refresh(target: dict) -> bool:
        raise AssertionError("接管成功后不得再打 OAuth refresh（会拿死票换 invalid_grant）")

    monkeypatch.setattr(store, "adopt_credentials_from_client", fake_adopt)
    monkeypatch.setattr(chat, "refresh_account", forbidden_refresh)

    fresh = asyncio.run(chat._refresh_to_account(account))

    assert polarities == [True]
    assert int(fresh["id"]) == int(account["id"])
    assert fresh["access_token"] == ADOPT_AT_DISK
    assert fresh["refresh_token"] == ADOPT_RT_DISK
    assert fresh["expires_at"] == EXP_DISK_NEW


def test_refresh_to_account_falls_back_when_no_update(isolated_db, monkeypatch):
    """磁盘没有更新凭据（接管 False）⇒ 回退原有 OAuth refresh，旧行为不破。

    ROTATION-VERDICT 策略 3：接管是**纯新增**，网关作为唯一持有者（磁盘没有新票 /
    读不到 / uid 对不上）时仍须能自刷新，既有 refresh 语义不能因此退化。
    """
    account = _adopt_account(None)
    calls: list[int] = []

    async def fake_refresh(target: dict) -> bool:
        calls.append(int(target["id"]))
        db.update_account(
            int(target["id"]),
            {
                "access_token": ADOPT_AT_OAUTH,
                "refresh_token": ADOPT_RT_OAUTH,
                "expires_at": EXP_DISK_NEW,
                "status": "active",
            },
        )
        return True

    monkeypatch.setattr(
        store, "adopt_credentials_from_client", lambda target, *, require_newer: False
    )
    monkeypatch.setattr(chat, "refresh_account", fake_refresh)

    fresh = asyncio.run(chat._refresh_to_account(account))

    assert calls == [int(account["id"])]  # 恰好一次，不放大
    assert fresh["access_token"] == ADOPT_AT_OAUTH
    assert fresh["refresh_token"] == ADOPT_RT_OAUTH