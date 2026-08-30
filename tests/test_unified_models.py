"""统一模型（跨平台翻译层）：统一名 -> 各平台内部名，白名单仍是最终闸门。"""
import pytest

from accounts import control_plane
from storage import database as db
from gateway import router
from providers.model_config import translate_unified, unified_models
from providers.protocol import InvalidModel


@pytest.fixture()
def fake_settings(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(db, "set_setting", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(db, "delete_setting", lambda key: store.pop(key, None))
    return store


DEEPSEEK = {
    "name": "deepseek-v4-flash",
    "mappings": {
        "traework": "DeepSeek-V4-Flash-Official",
        "workbuddy": "deepseek-v4-flash",
    },
}


# ---------- 翻译函数 ----------

def test_translate_unified_identity_without_setting(fake_settings):
    assert translate_unified("traework", "anything") == "anything"
    assert unified_models() == {}


def test_translate_unified_maps_only_mapped_channel(fake_settings):
    fake_settings["unified_models"] = [DEEPSEEK]
    assert translate_unified("traework", "deepseek-v4-flash") == "DeepSeek-V4-Flash-Official"
    assert translate_unified("workbuddy", "deepseek-v4-flash") == "deepseek-v4-flash"
    # 该通道无映射 → 原样返回
    assert translate_unified("qclaw", "deepseek-v4-flash") == "deepseek-v4-flash"
    # 不是统一名 → 原样返回
    assert translate_unified("traework", "qwen-3.7-plus") == "qwen-3.7-plus"


@pytest.mark.parametrize("bad", ["garbage", 123, [123], [{"name": "", "mappings": {"traework": "x"}}], [{"name": "x", "mappings": {}}], [{"name": "x", "mappings": "nope"}]])
def test_invalid_unified_setting_ignored(fake_settings, bad):
    fake_settings["unified_models"] = bad
    assert unified_models() == {}


# ---------- router.bind：翻译发生在白名单校验之前 ----------

def test_router_bind_translates_unified_before_whitelist(fake_settings):
    fake_settings["traework.models"] = ["DeepSeek-V4-Flash-Official"]
    fake_settings["unified_models"] = [DEEPSEEK]

    bound = router.bind({"model": "deepseek-v4-flash"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "DeepSeek-V4-Flash-Official"
    assert bound.original == "deepseek-v4-flash"

    # 带前缀请求统一名同样被翻译
    bound2 = router.bind({"model": "traework/deepseek-v4-flash"}, {"default_channel": "traework"})
    assert bound2.inner == "DeepSeek-V4-Flash-Official"
    assert bound2.original == "traework/deepseek-v4-flash"


def test_router_bind_unified_whitelist_still_gates(fake_settings):
    # 白名单里没有翻译出的内部名 → 必须拒绝
    fake_settings["traework.models"] = ["glm-5"]
    fake_settings["unified_models"] = [DEEPSEEK]
    with pytest.raises(InvalidModel):
        router.bind({"model": "deepseek-v4-flash"}, {"default_channel": "traework"})


def test_router_bind_direct_inner_name_still_works(fake_settings):
    # 直接用平台内部名（不走统一名）不受影响
    fake_settings["traework.models"] = ["DeepSeek-V4-Flash-Official"]
    bound = router.bind({"model": "traework/DeepSeek-V4-Flash-Official"}, {"default_channel": "traework"})
    assert bound.inner == "DeepSeek-V4-Flash-Official"


# ---------- control_plane：view / set / 校验 ----------

def test_set_unified_models_roundtrip_and_clear(fake_settings):
    view = control_plane.set_unified_models([DEEPSEEK])
    assert view["models"] == [DEEPSEEK]
    assert fake_settings["unified_models"] == [DEEPSEEK]
    assert view["channels"]

    cleared = control_plane.set_unified_models([])
    assert cleared["models"] == []
    assert fake_settings["unified_models"] == []


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        [{"name": "", "mappings": {"traework": "x"}}],
        [{"name": "x", "mappings": "nope"}],
        [{"name": "x", "mappings": {"traework": ""}}],
        [{"name": "x", "mappings": {"notachannel": "x"}}],
        [{"name": "x", "mappings": {}}, {"name": "y", "mappings": {"traework": "x"}}],
        [{"name": "x", "mappings": {"traework": "x"}}, {"name": "x", "mappings": {"traework": "y"}}],
    ],
)
def test_set_unified_models_validation(fake_settings, bad):
    with pytest.raises(ValueError):
        control_plane.set_unified_models(bad)
