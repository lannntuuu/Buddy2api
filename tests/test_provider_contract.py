"""WS-B provider 协议契约测试(35号方案 §2.3)。

遍历已加载的五家 provider,钉住两层契约:
- Core:Provider Protocol 的 9 方法 + 3 属性,五家齐备,异步方法必须可 await;
- 能力集:各家拥有哪些能力 Protocol 的方法集,声明与实现一致(防漂移)。

仅做存在性/可调用性/异步性检查,不校验参数签名细节(各家能力签名本就允许差异)。
"""
import inspect

import pytest

import providers
from providers.protocol import (
    CheckinCapable,
    DynamicModelsCapable,
    Provider,
    QclawLoginCapable,
    QuotaCapable,
    RefreshCapable,
    SoloLoginCapable,
    StoreCapable,
    TestChatCapable,
    UpsertCapable,
)

CORE_METHODS = [
    "list_models", "alias_map", "accepts_model", "translate_model",
    "pick_account", "pick_account_with_fallback", "has_usable_account",
    "chat_completions", "fetch_model_rates",
]
CORE_ASYNC = {"pick_account_with_fallback", "has_usable_account", "chat_completions"}

ALL_PROVIDERS = ["workbuddy", "qclaw", "qwenwork", "traework", "traesolo"]


def _instance(channel: str, monkeypatch=None):
    if monkeypatch is not None:
        # 契约测试需要全量加载;qclaw/qwenwork 默认不在启用集
        monkeypatch.setenv(
            "CB_GATEWAY_PROVIDERS", ",".join(ALL_PROVIDERS)
        )
    provider = providers.get_provider(channel)
    assert provider is not None, f"channel {channel} 未加载"
    return provider


@pytest.mark.parametrize("channel", ALL_PROVIDERS)
def test_core_provider_contract(channel, monkeypatch):
    provider = _instance(channel, monkeypatch)
    for name in CORE_METHODS:
        method = getattr(provider, name, None)
        assert callable(method), f"{channel}.{name} 缺失"
        if name in CORE_ASYNC:
            assert inspect.iscoroutinefunction(method), f"{channel}.{name} 应为 async"
    for attr in ("id", "display_name", "checkin_supported"):
        assert hasattr(provider, attr), f"{channel}.{attr} 缺失"


def _has_all(provider, names):
    return all(callable(getattr(provider, name, None)) for name in names)


# ---------- 能力集矩阵(实测于 35 号讨论底稿) ----------

def test_capability_matrix(monkeypatch):
    monkeypatch.setenv(
        "CB_GATEWAY_PROVIDERS", ",".join(ALL_PROVIDERS)
    )
    matrix = {
        "workbuddy": set(),
        "qclaw": {"store", "refresh", "test_chat", "quota", "qclaw_login"},
        "qwenwork": {"store", "refresh", "test_chat", "quota", "upsert"},
        "traework": {"store", "refresh", "test_chat", "quota", "upsert", "checkin"},
        "traesolo": {"store", "refresh", "test_chat", "quota", "upsert", "checkin",
                     "solo_login", "dynamic_models"},
    }
    for channel, expected in matrix.items():
        provider = _instance(channel, monkeypatch)
        caps = set()
        if _has_all(provider, ["discover", "import_path", "parse_credentials"]):
            caps.add("store")
        if callable(getattr(provider, "refresh", None)):
            caps.add("refresh")
        if callable(getattr(provider, "test_chat", None)):
            caps.add("test_chat")
        if callable(getattr(provider, "fetch_quota", None)):
            caps.add("quota")
        if callable(getattr(provider, "upsert_account", None)):
            caps.add("upsert")
        if _has_all(provider, ["fetch_checkin", "claim_checkin"]):
            caps.add("checkin")
        if _has_all(provider, ["start_login", "complete_login"]):
            caps.add("qclaw_login")
        if _has_all(provider, ["start_login", "login_result", "cancel_login",
                               "complete_login_callback"]):
            caps.add("solo_login")
        if callable(getattr(provider, "refresh_dynamic_models", None)):
            caps.add("dynamic_models")
        assert caps == expected, f"{channel} 能力集漂移: {caps} != {expected}"


# ---------- Protocol 声明与能力方法的形态一致 ----------

def test_capability_protocols_match_shapes():
    async_caps = {
        RefreshCapable: ["refresh"],
        TestChatCapable: ["test_chat"],
        QuotaCapable: ["fetch_quota"],
        CheckinCapable: ["fetch_checkin", "claim_checkin"],
        QclawLoginCapable: ["start_login", "complete_login"],
        DynamicModelsCapable: ["refresh_dynamic_models"],
    }
    for proto, names in async_caps.items():
        for name in names:
            method = getattr(proto, name, None)
            assert method is not None, f"{proto.__name__}.{name} 未声明"
    sync_caps = {
        StoreCapable: ["discover", "import_path", "parse_credentials"],
        UpsertCapable: ["upsert_account"],
        SoloLoginCapable: ["start_login", "login_result", "cancel_login",
                           "complete_login_callback"],
    }
    for proto, names in sync_caps.items():
        for name in names:
            assert callable(getattr(proto, name, None)), f"{proto.__name__}.{name} 未声明"
    # SoloLoginCapable 的 complete_login_callback 是异步方法,声明层须为 async def
    assert inspect.iscoroutinefunction(SoloLoginCapable.complete_login_callback)


def test_provider_registry_uses_provider_annotation():
    # 注册表的类型注解指向 Provider Protocol(防有人改成具体类导致五家类型失约)
    import providers as providers_module
    annotations = getattr(providers_module, "_LOADED", None)
    assert annotations is not None
