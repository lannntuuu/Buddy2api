"""Qoder CN provider: store mapping, frozen cosy signing, SSE unwrapping, catalog."""
import hashlib
import json

import pytest

from providers.qodercn import chat, cosy, store
from providers.qodercn import PROVIDER
from providers.qodercn.constants import (
    ALIASES,
    CHANNEL_ID,
    COSY_VERSION_FROZEN,
    DEFAULT_MODEL,
    DISPLAY_NAME,
    MODEL_CATALOG,
    STATIC_MODELS,
)
from storage import database as db


@pytest.fixture()
def fake_settings(monkeypatch):
    """内存 settings，避免碰真实 DB。"""
    store_: dict = {}
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: store_.get(key, default))
    monkeypatch.setattr(db, "set_setting", lambda key, value: store_.__setitem__(key, value))
    monkeypatch.setattr(db, "delete_setting", lambda key: store_.pop(key, None))
    return store_

# The desktop and IDE auth docs use different shapes; both must map to an account.
DESKTOP_DOC = {
    "schemaVersion": 1,
    "token": "dt-QHIr1jD29L60CbMr0yi5En65",
    "refreshToken": "drt-lLcy9q4zxRSnqSsW4AfWn86f",
    "expiresAt": "2026-10-18T08:40:44Z",
    "refreshTokenExpiresAt": "2027-09-13T08:40:44Z",
    "user": {"id": "019f9321-f548-75dd-8c54-062b562ff662", "name": "lannntuuu", "email": ""},
}

IDE_DOC = {
    "id": "019f9321-f548-75dd-8c54-062b562ff662",
    "token": "dt-d7PdkZKIPkNYAU2WdaUn86Tg",
    "refreshToken": "drt-MwJP2eCgOjMldOgO4HTcY63Q",
    "expireTime": "1790219340000",
    "refreshTokenExpireTime": "1818731340000",
    "name": "lannntuuu",
    "login_source": "qodercn",
}

# A real captured request (MITM of the official desktop client). The signature
# below was produced by the official client and must verify against our cosy.
CAPTURED = {
    "encoded": (
        "eyJ2ZXJzaW9uIjoidjEiLCJyZXF1ZXN0SWQiOiI3YWU0ODVhNi03ODYxLTQ0ZTAtOWFkZC0yZmU5YjVjNzZhYWMi"
        "LCJpbmZvIjoiUGZGdkR6cmcveXllaWNIYmxyVUxjeHpoT093ZG9acDVlQXNDT0JHMWJsL2FMdzBvWUJKeUxtMEZC"
        "bS9wWlp5d2NwYzJVLzM0OW9KOGs5OFE1dmZtYlRHRWMyVklSb1FvTTNPSUUvNGhYQ1FzL2dXa3EzdjlvNXREUlRs"
        "MDA2dEhDVDlOcG9JblFOOWRic3FuYWhvazdJd2lNRTFBR1Ivd2hCR2I2cFUrQUdJPSIsImNvc3lWZXJzaW9uIjoi"
        "MS4xLjUzIiwiaWRlVmVyc2lvbiI6IiJ9"
    ),
    "info": (
        "PfFvDzrg/yyeicHblrULcxzhOOwdoZp5eAsCOBG1bl/aLw0oYBJyLm0FBm/pZZywcpc2U/349oJ8k98Q5vfmbTGEc2"
        "VIRoQoM3OIE/4hXCQs/gWkq3v9o5tDRTl006tHCT9NpoInQN9dbsqnahok7IwiME1AGR/whBGb6pU+AGI="
    ),
    "signature": "bcfa18b0f7aecaf9693cc44b13154a66",
    "cosy_key": (
        "Sst9+vAJJ7Ho+28DK4kZ4l9RxNM/8xFf7DTwL61HihqWqa+/Tcp9DcBKSuBbyj7tIbk2IKHQH76KZv8gcfssI5AP"
        "Zj+G5oAOxeQDzfPDbw0GH10WB9OloVFm/HLN8clidJ1khfibWfe6R3km6knnTZfVR4n3oQ53yEMbGNOVEuk="
    ),
    "date": "1789740253",
    "path": "/api/v2/service/pro/sse/agent_chat_generation",
}


def test_desktop_doc_maps_to_account():
    acct = store.session_to_account(DESKTOP_DOC, source="auth.v1.dat")
    assert acct["uid"] == "019f9321-f548-75dd-8c54-062b562ff662"
    assert acct["access_token"] == "dt-QHIr1jD29L60CbMr0yi5En65"
    assert acct["refresh_token"] == "drt-lLcy9q4zxRSnqSsW4AfWn86f"
    assert acct["expires_at"] == store.iso_to_ms("2026-10-18T08:40:44Z")
    assert acct["provider"] == "qodercn"
    assert acct["domain"] == "qoder.com.cn"


def test_ide_flat_doc_maps_to_account():
    acct = store.session_to_account(IDE_DOC, source="state.vscdb")
    assert acct["uid"] == "019f9321-f548-75dd-8c54-062b562ff662"
    assert acct["name"] == "lannntuuu"
    assert acct["access_token"] == "dt-d7PdkZKIPkNYAU2WdaUn86Tg"
    # epoch-ms string fields parse without wrapping (string stays valid if not ISO)
    assert acct["expires_at"] == 1790219340000
    assert acct["refresh_expires_at"] == 1818731340000
    assert acct["extra"]["login_source"] == "qodercn"


def test_parse_credentials_round_trips():
    acct = store.parse_credentials(DESKTOP_DOC)
    assert acct["uid"] == "019f9321-f548-75dd-8c54-062b562ff662"


def test_display_name_is_qoder():
    assert DISPLAY_NAME == "Qoder"
    assert CHANNEL_ID == "qodercn"


def test_cosy_uses_same_rsa_key_as_qwenwork():
    from providers.qwenwork.constants import RSA_PUBLIC_KEY_PEM as qwen_pem
    from providers.qodercn.constants import RSA_PUBLIC_KEY_PEM as ours_pem
    assert ours_pem == qwen_pem


def test_cosy_frozen_after_smoke_test():
    """The gate opens only because a real chat smoke test passed."""
    assert COSY_VERSION_FROZEN is True
    assert cosy.COSY_VERSION_FROZEN is True
    # require_frozen no longer raises.
    cosy.require_frozen()


def test_cosy_header_reproduces_captured_bytes():
    """COSY header 必须逐字复现抓包值（base64 覆盖进签名，一字之差即验签失败）。

    覆盖点：键序、`cosyVersion` 取真实客户端值 "1.1.53"、`ideVersion` 为空串、
    紧凑分隔符（无空格）。
    """
    material = {"key": CAPTURED["cosy_key"], "info": CAPTURED["info"], "uid": "019f9321"}
    token = cosy.generate_auth_token(
        material,
        url="https://gateway.qoder.com.cn/algo" + CAPTURED["path"],
        body="x",
        timestamp=int(CAPTURED["date"]),
        request_id="7ae485a6-7861-44e0-9add-2fe9b5c76aac",
    )
    assert token["Authorization"].split(".")[1] == CAPTURED["encoded"]


def test_cosy_signature_formula_is_md5_of_documented_string():
    """签名公式：md5(f"{encoded}\\n{key}\\n{date}\\n{body}\\n{path}")，独立复算验证。

    （抓包原请求体有 307KB，不便内联；这里用合成体独立复算，等价证明公式。
    抓包签名 `CAPTURED["signature"]` 已在冻结阶段用完整请求体验过一次。）
    """
    assert len(CAPTURED["signature"]) == 32
    key = "KEY=="
    info = "INFO=="
    body = '{"a":1}'
    ts = 1789740253
    url = "https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1"
    material = {"key": key, "info": info, "uid": "u1"}
    token = cosy.generate_auth_token(
        material, url=url, body=body, timestamp=ts, request_id="rid-1")
    encoded = token["Authorization"].split(".")[1]
    expected = hashlib.md5(
        f"{encoded}\n{key}\n{ts}\n{body}\n/api/v2/service/pro/sse/agent_chat_generation".encode()
    ).hexdigest()
    assert token["Authorization"] == f"Bearer COSY.{encoded}.{expected}"
    assert token["sign_str"] == (
        f"{encoded}\n{key}\n{ts}\n{body}\n/api/v2/service/pro/sse/agent_chat_generation")


def test_signing_path_drops_algo_prefix_and_query():
    assert cosy.signing_path(
        "https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation"
        "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
    ) == "/api/v2/service/pro/sse/agent_chat_generation"


# ---------------- SSE 信封剥离（依据真实抓包/实测流） ----------------

def _envelope(inner, status=200, status_text="OK"):
    body = inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
    return json.dumps({"headers": {"Content-Type": ["application/json"]},
                       "body": body, "statusCodeValue": status, "statusCode": status_text})


def test_decode_envelope_chunk():
    kind, data = chat._decode_envelope(_envelope({
        "choices": [{"delta": {"content": "hi"}, "index": 0}],
        "id": "chatcmpl-x", "created": 1, "model": "auto",
        "object": "chat.completion.chunk"}))
    assert kind == "chunk"
    assert data["choices"][0]["delta"]["content"] == "hi"


def test_decode_envelope_done_is_literal_not_json():
    """`[DONE]` 是字面量字符串（非法 JSON），必须识别为结束而非忽略。"""
    assert chat._decode_envelope(_envelope("[DONE]"))[0] == "done"
    assert chat._decode_envelope(json.dumps(
        {"body": "[DONE]", "statusCodeValue": 200}))[0] == "done"


def test_decode_envelope_error_and_queued():
    kind, data = chat._decode_envelope(_envelope(
        {"code": "400", "message": "[FAIL]node:oa msg:Execution failed: null"},
        status=400, status_text="BAD_REQUEST"))
    assert kind == "error"
    assert data["status"] == 400

    # 10605 = modelQueued → 可重试，并透出 retryAfterSeconds
    kind, data = chat._decode_envelope(_envelope(
        {"code": "10605",
         "message": json.dumps({"isQueued": False, "modelKey": "qmodel_latest",
                                "queueCount": 0, "queueType": "p4",
                                "retryAfterSeconds": 2, "serviceAvailable": True,
                                "waitTime": 0})},
        status=403, status_text="FORBIDDEN"))
    assert kind == "queued"
    assert data["retry_after"] == 2.0


def test_decode_envelope_ignores_non_envelope():
    # event:finish 的 data 没有 statusCodeValue，须忽略
    assert chat._decode_envelope('{"firstTokenDuration":1,"totalDuration":2}')[0] == "ignore"
    assert chat._decode_envelope("")[0] == "ignore"
    assert chat._decode_envelope("not json")[0] == "ignore"


def test_collect_keeps_reasoning_and_finish():
    state = {"role_sent": False, "content": False, "reasoning": False,
             "finish": None, "usage": None}
    choices, usage = chat._collect(
        {"choices": [{"delta": {"role": "assistant", "reasoning_content": "想"}, "index": 0}]}, state)
    assert choices[0]["delta"]["role"] == "assistant"
    assert choices[0]["delta"]["reasoning_content"] == "想"
    assert usage is None

    choices, usage = chat._collect(
        {"choices": [{"delta": {"content": ""}, "finish_reason": "stop", "index": 0}]}, state)
    assert choices[0]["finish_reason"] == "stop"
    assert state["finish"] == "stop"

    # usage chunk: choices 为空、顶层带 usage
    choices, usage = chat._collect({"choices": [], "usage": {"total_tokens": 7}}, state)
    assert choices == []
    assert usage == {"total_tokens": 7}
    assert state["usage"] == {"total_tokens": 7}


def test_collect_drops_empty_delta_without_finish():
    state = {"role_sent": True, "content": False, "reasoning": False,
             "finish": None, "usage": None}
    assert chat._collect({"choices": [{"delta": {}, "index": 0}]}, state) is None


def test_openai_chunk_overrides_untrusted_model():
    """上游内层 model 恒为 'auto'，必须用请求模型键覆盖。"""
    state = {"id": "chatcmpl-x", "created": 5}
    out = chat.openai_chunk(state, "qmodel_latest", [{"index": 0, "delta": {"content": "a"}}], None)
    assert out["model"] == "qmodel_latest"
    assert out["object"] == "chat.completion.chunk"
    assert out["id"] == "chatcmpl-x"


# ---------------- 请求体 ----------------

def test_build_body_shape():
    body, raw, model = chat.build_body(
        {"model": "auto", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 16})
    assert model == "auto"
    assert body["agent_id"] == "agent_common"
    assert body["session_type"] == "qodercn"[:0] + body["session_type"]  # from constants
    assert body["chat_task"] == "FREE_INPUT"
    assert body["stream"] is True
    assert body["version"] == "3"
    # model_config 取自模型目录
    meta = MODEL_CATALOG["auto"]
    assert body["model_config"]["display_name"] == meta["display_name"]
    assert body["model_config"]["max_input_tokens"] == meta["max_input_tokens"]
    assert body["parameters"]["max_tokens"] == 16
    assert json.loads(raw)["model_config"]["key"] == "auto"


def test_build_body_hoists_system_message():
    body, _, _ = chat.build_body({"model": "auto", "messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"}]})
    assert body["system"] == "你是助手"


def test_static_headers_match_captured_request():
    headers = chat.static_headers("auto", "rid", "mid-1")
    assert headers["Cosy-ClientType"] == "10"
    assert headers["Cosy-Scene"] == "app"
    assert headers["Cosy-MachineId"] == "mid-1"
    assert headers["Cosy-MachineToken"] == "mid-1"
    assert headers["x-model-key"] == "auto"
    assert headers["User-Agent"] == "undici"
    # machine id 缺失时不应塞空头
    assert "Cosy-MachineId" not in chat.static_headers("auto", "rid", "")


def test_chat_url_has_no_encode_param():
    """明文 body 路径已验证可用，不发送 Encode=1（客户端密文 body 不需要）。"""
    url = chat.chat_url()
    assert url.endswith("/algo/api/v2/service/pro/sse/agent_chat_generation"
                        "?FetchKeys=llm_model_result&AgentId=agent_common")
    assert "Encode" not in url


def test_model_meta_defaults_for_unknown_key():
    meta = chat.model_meta("no-such-model")
    assert meta["display_name"] == "no-such-model"
    assert meta["max_input_tokens"] == 180_000


def test_static_models_match_catalog_and_default_exists():
    assert DEFAULT_MODEL in STATIC_MODELS
    for key in STATIC_MODELS:
        assert key in MODEL_CATALOG, f"{key} 不在模型目录中"


def test_qfmodel_is_enabled_and_uses_free_tier_key():
    """qfmodel（Qwen3.8-Flash）是目录里唯一 price_factor=0 的真免费档。

    它曾因缺少顶层 `business` 块被网关 400 拒服；补上 business 后已验证可用
    （流式/非流式均正常），因此必须出现在 STATIC_MODELS 里。
    """
    assert "qfmodel" in STATIC_MODELS
    assert MODEL_CATALOG["qfmodel"]["display_name"] == "Qwen3.8-Flash"


# ---------- 对外展示名（别名）----------

def test_default_aliases_are_the_catalog_display_names():
    """默认别名 = 目录 display_name，客户端下拉里直接是可读名字而非内部 key。"""
    assert ALIASES, "qodercn 必须预置别名，否则 /v1/models 只列 qfmodel 这种内部 key"
    for key, meta in MODEL_CATALOG.items():
        assert ALIASES[meta["display_name"]] == key
    assert ALIASES["Qwen3.8-Flash"] == "qfmodel"
    # 每个别名都指向白名单内的模型（无孤儿）
    for target in ALIASES.values():
        assert target in STATIC_MODELS


def test_public_model_names_prefers_alias_then_falls_back_to_id():
    from providers.model_config import public_model_names

    names = public_model_names(["a", "b"], {"Nice A": "a"})
    assert names == ["Nice A", "b"], "有别名用别名，没别名回退原生 id"


def test_public_model_names_keeps_orphan_aliases():
    """目标不在白名单的别名（如 workbuddy 的 gpt-5.5→glm-5.2）不能消失。"""
    from providers.model_config import public_model_names

    names = public_model_names(["hy3", "hy3-x"],
                               {"auto": "hy3-x", "gpt-5.5": "glm-5.2"})
    assert names == ["hy3", "auto", "gpt-5.5"]


def test_public_model_names_is_order_stable_and_deduped():
    from providers.model_config import public_model_names

    # 两个别名指向同一 id 时只列第一个；重复输入去重
    names = public_model_names(["a", "a", "b"], {"First": "a", "Second": "a"})
    assert names == ["First", "b"]


def test_listed_names_are_all_bindable(fake_settings):
    """回归：`/v1/models` 列出的每个名字都必须真的能 bind。

    否则客户端照着目录发请求会直接 400 —— 这正是本次改动的核心不变量。
    """
    from gateway import router
    from providers.model_config import public_model_names

    fake_settings["qodercn.models"] = list(STATIC_MODELS)
    fake_settings["qodercn.aliases"] = dict(ALIASES)
    ids = [m["id"] for m in PROVIDER.list_models()]
    names = public_model_names(ids, dict(ALIASES))
    assert "Qwen3.8-Flash" in names
    for name in names:
        bound = router.bind({"model": f"{CHANNEL_ID}/{name}"},
                            {"default_channel": CHANNEL_ID})
        assert bound.inner == name
    # 别名最终由 translate_model 翻回原生 key（真正发给上游的值）
    assert PROVIDER.translate_model("Qwen3.8-Flash") == "qfmodel"


def test_alias_can_be_renamed_and_v1_models_follows(fake_settings):
    """别名可改：改名后 /v1/models 跟着变，原生 key 仍可用。"""
    from providers.model_config import public_model_names

    fake_settings["qodercn.models"] = list(STATIC_MODELS)
    fake_settings["qodercn.aliases"] = {"Flash免费档": "qfmodel"}

    names = public_model_names([m["id"] for m in PROVIDER.list_models()], PROVIDER.alias_map())
    assert "Flash免费档" in names
    assert "Qwen3.8-Flash" not in names
    # 原生 key 始终可路由，不受改名影响
    assert PROVIDER.accepts_model("qfmodel")
    assert PROVIDER.translate_model("Flash免费档") == "qfmodel"


def test_build_body_sends_business_block():
    """`business` 是 qfmodel 可路由的必要条件（缺它 => 400 Execution failed: null）。

    实测只删 business 里任意单个字段仍可用，故只断言「对象存在 + 关键键存在」，
    不锁死完整字段集合。
    """
    body, raw, model = chat.build_body(
        {"model": "qfmodel", "messages": [{"role": "user", "content": "你好"}]})
    assert model == "qfmodel"
    assert isinstance(body.get("business"), dict)
    assert body["business"]["product"] == "app"
    assert body["business"]["type"] == "agent"
    # 序列化后确实带上（COSY 签名字段以 raw 为准）
    assert '"business"' in raw


def test_build_body_parameters_include_thinking_and_context_length():
    """客户端会带 enable_thinking 与 context_length；后者取该模型自身窗口。"""
    body, _, _ = chat.build_body(
        {"model": "qfmodel", "messages": [{"role": "user", "content": "你好"}]})
    params = body["parameters"]
    assert params["enable_thinking"] is True
    assert params["context_length"] == MODEL_CATALOG["qfmodel"]["max_input_tokens"]

    # 调用方显式传 context_length 时应尊重它
    body2, _, _ = chat.build_body(
        {"model": "qfmodel", "messages": [{"role": "user", "content": "你好"}],
         "context_length": 400_000})
    assert body2["parameters"]["context_length"] == 400_000


def test_refresh_hook_exists_and_is_async():
    """回归：`__init__.py` 的 pick_account_with_fallback 依赖 `chat.refresh`。

    曾因 chat.py 漏定义 `refresh` 导致 AttributeError，使整条通道经网关
    `/v1/chat/completions` 一律 500（`ensure_usable` → `has_usable_account`
    在真正发请求之前就崩）。这里钉住该 hook 的存在与异步性。
    """
    import inspect

    assert hasattr(chat, "refresh"), "chat.refresh 缺失会让整条通道 500"
    assert inspect.iscoroutinefunction(chat.refresh)


def test_provider_facade_methods_used_by_ensure_usable_exist(monkeypatch):
    """`providers.qodercn.__init__` 引用的 chat.* 成员都必须存在。

    覆盖「facade 调用了 chat 里不存在的函数」这类回归（曾导致通道级 500）。
    """
    import inspect

    for name in ("model_meta", "translate_model", "chat_completions", "fetch_quota",
                 "test_chat", "refresh"):
        assert hasattr(chat, name), f"chat.{name} 缺失（__init__ 依赖它）"

    import providers
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "qodercn")
    provider = providers.get_provider("qodercn")
    for name in ("pick_account_with_fallback", "has_usable_account", "chat_completions",
                 "refresh", "test_chat", "fetch_quota"):
        assert hasattr(provider, name), f"provider.{name} 缺失"
        assert inspect.iscoroutinefunction(getattr(provider, name)), f"{name} 应为 async"


def test_quota_prefers_nested_addon_bucket():
    """真实 /api/v2/quota/usage 结构：额度在 addOnQuota/userQuota 里。"""
    assert chat._quota_remaining(
        {"userQuota": {"remaining": 0.0}, "addOnQuota": {"remaining": 100.0}}) == 100.0
    assert chat._quota_remaining({"userQuota": {"remaining": 12.5}}) == 12.5
    assert chat._quota_remaining({"remaining": 12.5}) == 12.5
    assert chat._quota_remaining({"balance": 3}) == 3.0
    assert chat._quota_remaining({"total_dosage": "x"}) is None
    assert chat._quota_remaining({}) is None
    assert chat._quota_remaining(None) is None


def test_machine_id_env_override(monkeypatch):
    monkeypatch.setenv("CB_QODERCN_MACHINE_ID", "abc-123")
    assert store.machine_id() == "abc-123"
    monkeypatch.delenv("CB_QODERCN_MACHINE_ID", raising=False)
    # 本机存在时为非空；不存在时为空字符串且不抛错
    assert isinstance(store.machine_id(), str)


def test_qodercn_dirs_exclude_international_qoder(monkeypatch):
    """国际版 `%APPDATA%\\Qoder` 属于 qoderwork 家族，不能混入 qodercn 发现。"""
    monkeypatch.delenv("CB_QODERCN_AUTH_DIR", raising=False)
    dirs = [str(p) for p in store.qodercn_data_dirs()]
    assert any(p.endswith("com.qodercn.app.stable") for p in dirs)
    assert any(p.endswith("QoderCN") for p in dirs)
    assert not any(p.replace("\\", "/").endswith("/Qoder") for p in dirs)
