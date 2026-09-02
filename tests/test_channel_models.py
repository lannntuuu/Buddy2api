"""Per-channel model list / alias configuration (可按平台配置模型列表)."""
import pytest

from accounts import control_plane
from storage import database as db
from gateway import router
from providers.qclaw import PROVIDER as QCLAW
from providers.qwenwork import PROVIDER as QWENWORK
from providers.traework import PROVIDER as TRAEWORK
from providers.traework.constants import ALIASES as TRAEWORK_DEFAULT_ALIASES
from providers.traework.constants import STATIC_MODELS as TRAEWORK_DEFAULT_MODELS
from providers.workbuddy import PROVIDER as WORKBUDDY


@pytest.fixture()
def fake_settings(monkeypatch):
    """In-memory settings store; avoids touching the real DB / tmp dirs."""
    store: dict = {}
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(db, "set_setting", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(db, "delete_setting", lambda key: store.pop(key, None))
    return store


# ---------- provider level: defaults vs custom ----------

def test_traework_defaults_without_custom(fake_settings):
    assert TRAEWORK.list_models() == [{"id": m} for m in TRAEWORK_DEFAULT_MODELS]
    assert TRAEWORK.alias_map() == dict(TRAEWORK_DEFAULT_ALIASES)
    assert TRAEWORK.accepts_model("qwen-3.7-plus") is True
    assert TRAEWORK.accepts_model("glm-5.2") is False
    assert TRAEWORK.translate_model("auto") == "qwen-3.7-plus"


def test_traework_custom_models(fake_settings):
    fake_settings["traework.models"] = ["foo", {"id": "bar"}]
    assert [m["id"] for m in TRAEWORK.list_models()] == ["foo", "bar"]
    assert TRAEWORK.accepts_model("foo") is True
    assert TRAEWORK.accepts_model("bar") is True
    assert TRAEWORK.accepts_model("qwen-3.7-plus") is False
    # 别名仍按内置生效
    assert TRAEWORK.accepts_model("auto") is True
    assert TRAEWORK.translate_model("auto") == "qwen-3.7-plus"


def test_traework_custom_aliases(fake_settings):
    fake_settings["traework.aliases"] = {"auto": "foo", "fast": "qwen-3.5"}
    assert TRAEWORK.translate_model("auto") == "foo"
    assert TRAEWORK.translate_model("fast") == "qwen-3.5"
    assert TRAEWORK.accepts_model("auto") is True
    assert TRAEWORK.accepts_model("fast") is True


def test_present_custom_wins_even_when_empty_or_invalid(fake_settings):
    """键存在（哪怕空或非法）→ 以自定义为准，不回退内置默认。"""
    for bad in ("garbage", 123, [], {"models": "x"}):
        fake_settings["traework.models"] = bad
        assert [m["id"] for m in TRAEWORK.list_models()] == []
        assert TRAEWORK.accepts_model("qwen-3.7-plus") is False
    for bad in ("garbage", [], ["a"]):
        fake_settings["traework.aliases"] = bad
        assert TRAEWORK.alias_map() == {}
        assert TRAEWORK.translate_model("auto") == "auto"  # 无别名可翻译


def test_absent_key_still_uses_defaults(fake_settings):
    fake_settings["traework.models"] = []
    assert [m["id"] for m in TRAEWORK.list_models()] == []
    del fake_settings["traework.models"]
    assert TRAEWORK.list_models() == [{"id": m} for m in TRAEWORK_DEFAULT_MODELS]
    assert TRAEWORK.alias_map() == dict(TRAEWORK_DEFAULT_ALIASES)


def test_empty_custom_via_set_roundtrip(fake_settings):
    view = control_plane.set_channel_models(
        "traework", models=[], aliases={}, set_models=True, set_aliases=True,
    )
    assert view["models"] == []
    assert view["aliases"] == {}
    assert view["customized"] == {"models": True, "aliases": True}
    assert TRAEWORK.accepts_model("qwen-3.7-plus") is False
    assert TRAEWORK.translate_model("auto") == "auto"

    # qclaw 自定义（含空白名单）后，pool-* 前缀不再旁路闸门
    view2 = control_plane.set_channel_models("qclaw", models=[], set_models=True)
    assert view2["models"] == []
    assert QCLAW.accepts_model("pool-whatever") is False
    assert QCLAW.accepts_model("default") is False


def test_qclaw_pool_prefix_kept_with_custom(fake_settings):
    # 默认配置下 pool-* 前缀放行（上游 pool 模型名更新快于静态表）
    assert QCLAW.accepts_model("pool-whatever") is True
    fake_settings["qclaw.models"] = ["x-new"]
    assert QCLAW.accepts_model("x-new") is True
    # 自定义后以前缀命中的模型也要过白名单
    assert QCLAW.accepts_model("pool-zzz") is False
    assert QCLAW.accepts_model("default") is False  # 不在自定义列表/别名里


def test_qwenwork_custom(fake_settings):
    fake_settings["qwenwork.models"] = ["qw-new"]
    assert [m["id"] for m in QWENWORK.list_models()] == ["qw-new"]
    assert QWENWORK.accepts_model("qw-new") is True
    assert QWENWORK.accepts_model("qwork-advanced") is False
    assert QWENWORK.translate_model("auto") == "qwork-advanced"


def test_workbuddy_legacy_keys_untouched(fake_settings):
    fake_settings["models"] = [{"id": "custom-m", "name": "Custom M"}]
    assert WORKBUDDY.list_models() == [{"id": "custom-m", "name": "Custom M"}]
    assert WORKBUDDY.accepts_model("custom-m") is True
    fake_settings["model_aliases"] = {"my-alias": "custom-m"}
    assert WORKBUDDY.alias_map()["my-alias"] == "custom-m"


def test_workbuddy_aliases_replacement_semantics(fake_settings):
    """workbuddy 别名整体替换：自定义（含空）生效后内置别名全部失效。"""
    from upstream import proxy

    # 未设置 → 内置默认
    assert "gpt-4o" in WORKBUDDY.alias_map()
    # 自定义非空 → 只有自定义，内置失效
    fake_settings["model_aliases"] = {"my-alias": "glm-5.2"}
    assert WORKBUDDY.alias_map() == {"my-alias": "glm-5.2"}
    assert WORKBUDDY.accepts_model("gpt-4o") is False
    assert proxy.resolve_model_alias("gpt-4o") == "gpt-4o"  # 运行时不再翻译
    assert proxy.resolve_model_alias("my-alias") == "glm-5.2"
    # 自定义空 → 无任何别名
    fake_settings["model_aliases"] = {}
    assert WORKBUDDY.alias_map() == {}
    assert WORKBUDDY.accepts_model("gpt-4o") is False
    assert proxy.get_all_aliases() == {}


def test_workbuddy_empty_models_list(fake_settings):
    fake_settings["models"] = []
    assert WORKBUDDY.list_models() == []
    assert WORKBUDDY.accepts_model("glm-5.2") is False
    # 别名表未设置时仍为内置（两个键独立）
    assert "gpt-4o" in WORKBUDDY.alias_map()


# ---------- control plane: view / set / reset / validation ----------

def test_channel_model_view_defaults(fake_settings):
    view = control_plane.channel_model_view("traework")
    assert view["models"] == list(TRAEWORK_DEFAULT_MODELS)
    assert view["aliases"] == dict(TRAEWORK_DEFAULT_ALIASES)
    assert view["customized"] == {"models": False, "aliases": False}
    assert view["defaults"]["models"] == list(TRAEWORK_DEFAULT_MODELS)


def test_set_channel_models_roundtrip_and_reset(fake_settings):
    view = control_plane.set_channel_models(
        "traework", models=["a", "b"], aliases={"auto": "a"},
        set_models=True, set_aliases=True,
    )
    assert view["models"] == ["a", "b"]
    assert view["aliases"] == {"auto": "a"}
    assert view["customized"] == {"models": True, "aliases": True}
    assert fake_settings["traework.models"] == ["a", "b"]
    assert fake_settings["traework.aliases"] == {"auto": "a"}
    assert TRAEWORK.accepts_model("a") is True
    assert TRAEWORK.translate_model("auto") == "a"

    reset = control_plane.set_channel_models(
        "traework", models=None, aliases=None, set_models=True, set_aliases=True,
    )
    assert reset["models"] == list(TRAEWORK_DEFAULT_MODELS)
    assert reset["customized"] == {"models": False, "aliases": False}
    assert "traework.models" not in fake_settings
    assert "traework.aliases" not in fake_settings


def test_set_channel_models_workbuddy_writes_legacy_keys(fake_settings):
    view = control_plane.set_channel_models(
        "workbuddy", models=["w1", "w2"], set_models=True,
    )
    assert view["models"] == ["w1", "w2"]
    assert fake_settings["models"] == [{"id": "w1"}, {"id": "w2"}]
    assert WORKBUDDY.accepts_model("w1") is True

    reset = control_plane.set_channel_models("workbuddy", models=None, set_models=True)
    assert "models" not in fake_settings
    assert reset["customized"]["models"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        # 注意：models=[] / aliases={} 现在是合法的"自定义空"，不再报错
        dict(models="not-a-list", set_models=True),
        dict(models=[{"id": ""}], set_models=True),
        dict(models=["", " "], set_models=True),
        dict(aliases={"x": ""}, set_aliases=True),
        dict(aliases=["x"], set_aliases=True),
        dict(aliases="nope", set_aliases=True),
        dict(),
    ],
)
def test_set_channel_models_validation(fake_settings, kwargs):
    with pytest.raises(ValueError):
        control_plane.set_channel_models("traework", **kwargs)


def test_set_channel_models_unknown_or_disabled_channel(fake_settings, monkeypatch):
    with pytest.raises(ValueError):
        control_plane.set_channel_models("nope", models=["a"], set_models=True)
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traework")
    with pytest.raises(ValueError):
        control_plane.set_channel_models("qclaw", models=["a"], set_models=True)
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


# ---------- end to end: router binding honors the custom list ----------

def test_router_bind_uses_custom_models(fake_settings):
    from providers.protocol import InvalidModel

    fake_settings["traework.models"] = ["foo"]
    bound = router.bind({"model": "traework/foo"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "foo"
    with pytest.raises(InvalidModel):
        router.bind({"model": "traework/qwen-3.7-plus"}, {"default_channel": "traework"})


# ---------- 按模型思考档位（reasoning effort）----------

def test_channel_model_view_reasoning_defaults(fake_settings):
    view = control_plane.channel_model_view("workbuddy")
    assert view["reasoning_supported"] is True
    assert view["reasoning"] == {}
    assert view["reasoning_default"] == ""
    assert view["reasoning_customized"] is False
    # none 等空串仍属合法选择
    assert "" in view["reasoning_choices"]
    assert "off" not in view["reasoning_choices"]


def test_set_channel_models_reasoning_roundtrip_and_reset(fake_settings):
    view = control_plane.set_channel_models(
        "workbuddy",
        reasoning={"__default__": "medium", "deepseek-v4-flash": "low"},
        set_reasoning=True,
    )
    assert view["reasoning"] == {"__default__": "medium", "deepseek-v4-flash": "low"}
    assert view["reasoning_default"] == "medium"
    assert view["reasoning_customized"] is True
    assert fake_settings["workbuddy.reasoning"] == {"__default__": "medium", "deepseek-v4-flash": "low"}

    reset = control_plane.set_channel_models("workbuddy", reasoning=None, set_reasoning=True)
    assert reset["reasoning"] == {}
    assert "workbuddy.reasoning" not in fake_settings


def test_set_channel_models_reasoning_drops_empty_levels(fake_settings):
    view = control_plane.set_channel_models(
        "workbuddy",
        reasoning={"deepseek-v4-pro": "", "glm-5.2": "high"},
        set_reasoning=True,
    )
    # 空串条目被丢弃，只保留显式档位
    assert view["reasoning"] == {"glm-5.2": "high"}


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-dict",
        {"deepseek-v4-flash": "super-high"},   # 非法档位
        {"deepseek-v4-flash": "off"},           # 上游拒绝的 off
        {"": "low"},                             # 空模型 id
        {"__default__": "turbo"},               # 非法通道默认
    ],
)
def test_set_channel_models_reasoning_validation(fake_settings, bad):
    with pytest.raises(ValueError):
        control_plane.set_channel_models("workbuddy", reasoning=bad, set_reasoning=True)


def test_reasoning_for_model_resolution(fake_settings):
    from providers.model_config import reasoning_for_model

    fake_settings["workbuddy.reasoning"] = {"__default__": "high", "deepseek-v4-flash": "low"}
    # 别名解析后的后端 id 命中：o3 → deepseek-v4-pro（走 __default__）
    assert reasoning_for_model("workbuddy", "deepseek-v4-flash") == "low"
    assert reasoning_for_model("workbuddy", "deepseek-v4-pro") == "high"
    # 未配置模型回退通道默认
    assert reasoning_for_model("workbuddy", "glm-5.2") == "high"
    # 完全没有配置 → None（不注入）
    del fake_settings["workbuddy.reasoning"]
    assert reasoning_for_model("workbuddy", "glm-5.2") is None

