"""Custom OpenAI-compat channels — definition validation, cache, seed,
<id>.models settings override. Keep coverage tight: every assertion in
this file maps 1:1 to a contract line in spec §3.1 / §3.2 / §3.3 / §5."""

from __future__ import annotations

import pytest

from providers import custom_channels as cc


# ---------------------------------------------------------------------------
# validate_definition 全规则（spec §3.1）
# ---------------------------------------------------------------------------


def test_validate_accepts_well_formed_definition():
    """合法 slug / https / 非空 models / aliases 全部命中 models / 合法 env。"""
    cc.validate_definition(
        {
            "id": "mychan",
            "display_name": "My Channel",
            "base_url": "https://x.example.com/v1",
            "models": ["m1", "m2"],
            "aliases": {"auto": "m1", "fast": "m2"},
            "env_api_key": "CB_MY_KEY",
        },
        reserved_ids={"workbuddy"},
    )


@pytest.mark.parametrize(
    "bad_id",
    [
        "",          # empty
        "1abc",      # starts with digit
        "ABC",       # uppercase
        "a-b-c-too-long-aaaaaaaaaaaaaaaa-x",  # > 32 chars
        "has space", # forbidden character
        "-leading",  # starts with hyphen
    ],
)
def test_validate_rejects_bad_slug(bad_id):
    with pytest.raises(ValueError):
        cc.validate_definition(
            {
                "id": bad_id,
                "display_name": "X",
                "base_url": "https://x/v1",
                "models": ["m"],
            },
            reserved_ids=set(),
        )


def test_validate_rejects_duplicate_id():
    with pytest.raises(ValueError, match="already in use"):
        cc.validate_definition(
            {"id": "dup", "display_name": "X", "base_url": "https://x/v1", "models": ["m"]},
            reserved_ids={"dup"},
        )


def test_validate_allows_duplicate_id_when_excluded():
    """Edit-existing-id path: caller passes exclude_id=self, so the self
    collision is ignored and validation succeeds."""
    cc.validate_definition(
        {"id": "dup", "display_name": "X2", "base_url": "https://x/v1", "models": ["m"]},
        reserved_ids={"dup"},
        exclude_id="dup",
    )


def test_validate_rejects_http_non_loopback():
    """非本机的 http:// 必须拒绝（D7 只放行本机）。"""
    with pytest.raises(ValueError, match="https"):
        cc.validate_definition(
            {"id": "a", "display_name": "X", "base_url": "http://example.com/v1", "models": ["m"]},
            reserved_ids=set(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/v1",
        "http://127.0.0.1:8000/v1",
        "http://localhost/v1",
        "http://localhost:8000/v1",
        "https://api.example.com/v1",
    ],
)
def test_validate_accepts_loopback_http_or_https(url):
    """本机 http（127.0.0.1 / localhost）以及任何 https 都放行。"""
    cc.validate_definition(
        {"id": "ok", "display_name": "X", "base_url": url, "models": ["m"]},
        reserved_ids=set(),
    )


def test_validate_rejects_alias_pointing_to_missing_model():
    with pytest.raises(ValueError, match="not in the models list"):
        cc.validate_definition(
            {
                "id": "ok",
                "display_name": "X",
                "base_url": "https://x/v1",
                "models": ["m1"],
                "aliases": {"auto": "m-does-not-exist"},
            },
            reserved_ids=set(),
        )


@pytest.mark.parametrize(
    "env_name",
    [
        "MY_KEY",            # missing CB_ prefix
        "CB_lower",          # lowercase
        "CB-WRONG-DASH",     # hyphen
        "cb_upper",          # lowercase prefix
        "CB_",               # empty suffix
        "CB_X-DASH",         # hyphen inside
    ],
)
def test_validate_rejects_bad_env_name(env_name):
    """Spec §3.1: env_api_key 必须匹配 ^CB_[A-Z0-9_]+$."""
    with pytest.raises(ValueError, match="env_api_key"):
        cc.validate_definition(
            {
                "id": "ok",
                "display_name": "X",
                "base_url": "https://x/v1",
                "models": ["m"],
                "env_api_key": env_name,
            },
            reserved_ids=set(),
        )


@pytest.mark.parametrize(
    "env_name",
    ["CB_X", "CB_BAILIAN_API_KEY", "CB_MY_KEY_2"],
)
def test_validate_accepts_well_formed_env_name(env_name):
    """合法 env_api_key（含 CB_ + 大写字母数字下划线 1+ 字符）放行。"""
    cc.validate_definition(
        {
            "id": "ok",
            "display_name": "X",
            "base_url": "https://x/v1",
            "models": ["m"],
            "env_api_key": env_name,
        },
        reserved_ids=set(),
    )


def test_validate_treats_empty_env_name_as_unset():
    """空字符串 / None 都视为「不设 env」，合法放行（spec §3.1：env_api_key 可选）。"""
    cc.validate_definition(
        {
            "id": "ok",
            "display_name": "X",
            "base_url": "https://x/v1",
            "models": ["m"],
            "env_api_key": "",
        },
        reserved_ids=set(),
    )
    cc.validate_definition(
        {
            "id": "ok",
            "display_name": "X",
            "base_url": "https://x/v1",
            "models": ["m"],
        },
        reserved_ids=set(),
    )


# ---------------------------------------------------------------------------
# 实例缓存 + invalidate（spec §3.2 D3）
# ---------------------------------------------------------------------------


def test_provider_cache_rebuilds_after_invalidate(isolated_db):
    """save_definitions →  get_provider 返回的实例字段随下一次调用反映新定义。"""
    cc.save_definitions(
        [
            {
                "id": "xchan",
                "display_name": "v1",
                "base_url": "https://v1.example.com/v1",
                "models": ["a"],
                "aliases": {"auto": "a"},
                "env_api_key": "CB_X",
            }
        ]
    )
    p1 = cc.get_provider("xchan")
    assert p1.display_name == "v1"
    assert p1.default_base_url == "https://v1.example.com/v1"

    # 替换定义并失效缓存 → 重建后字段必须是新值
    cc.save_definitions(
        [
            {
                "id": "xchan",
                "display_name": "v2",
                "base_url": "https://v2.example.com/v1",
                "models": ["b", "c"],
                "aliases": {"auto": "b"},
                "env_api_key": "CB_X",
            }
        ]
    )
    cc.invalidate_cache("xchan")
    p2 = cc.get_provider("xchan")
    assert p2.display_name == "v2"
    assert p2.default_base_url == "https://v2.example.com/v1"
    assert p2.default_model() == "b"


# ---------------------------------------------------------------------------
# seed 迁移（spec §5）— 幂等 + channel_hosts 覆盖合并
# ---------------------------------------------------------------------------


def test_seed_is_idempotent(isolated_db):
    """迁移函数调两次只写一次：第二次 absent() 返回 False → 直接 no-op。"""
    assert cc.seed_initial_definitions() is True
    first_run = cc.list_definitions()
    assert cc.seed_initial_definitions() is False  # idempotent: key now exists
    second_run = cc.list_definitions()
    assert [d["id"] for d in first_run] == [d["id"] for d in second_run]


def test_seed_does_not_overwrite_existing_user_definitions(isolated_db):
    """若 key 存在但被用户改写成空数组，seed 不应反向把 gmi/bailian 写回去。"""
    cc.save_definitions([])  # user explicitly cleared
    assert cc.seed_initial_definitions() is False
    assert cc.list_definitions() == []


def test_seed_merges_channel_hosts_base_url(isolated_db):
    """channel_hosts.gmi.base_url / .bailian.base_url 合并进 seed.base_url,
    然后对应条目从 channel_hosts 里清除。qwenwork 等其他渠道不受影响。"""
    from storage import database as db

    db.set_setting(
        "channel_hosts",
        {
            "gmi": {"base_url": "https://mirror.example.com/gmi/v1"},
            "bailian": {"base_url": "https://mirror.example.com/bailian/v1"},
            "qwenwork": {"gateway": "https://other.example.com/qw"},
        },
    )
    assert cc.seed_initial_definitions() is True

    gmi = cc.get_definition("gmi")
    bailian = cc.get_definition("bailian")
    assert gmi["base_url"] == "https://mirror.example.com/gmi/v1"
    assert bailian["base_url"] == "https://mirror.example.com/bailian/v1"

    # gmi/bailian overrides stripped; qwenwork preserved.
    hosts_after = db.get_setting("channel_hosts", {})
    assert "gmi" not in hosts_after
    assert "bailian" not in hosts_after
    assert hosts_after.get("qwenwork", {}).get("gateway") == "https://other.example.com/qw"


# ---------------------------------------------------------------------------
# <id>.models settings 接管：definition 兜底 vs 用户白名单（spec §3.3 D8）
# ---------------------------------------------------------------------------


def test_id_models_settings_overrides_definition_default(isolated_db):
    """user 写了 <id>.models → effective model 列表取 user 设置（≠ definition.models）。
    definition.models 仍存于 control_plane view 的 defaults 字段作为兜底。"""
    from accounts import control_plane

    cc.save_definitions(
        [
            {
                "id": "zchan",
                "display_name": "Z",
                "base_url": "https://z.example.com/v1",
                "models": ["seed-model-1"],
                "aliases": {"auto": "seed-model-1"},
                "env_api_key": "CB_Z",
            }
        ]
    )
    # user 写了一条 <id>.models 覆盖
    control_plane.set_channel_models("zchan", models=["custom-model-1", "custom-model-2"], set_models=True)

    view = control_plane.channel_model_view("zchan")
    assert view["models"] == ["custom-model-1", "custom-model-2"]
    # definition defaults 仍是兜底原值
    assert view["defaults"]["models"] == ["seed-model-1"]


def test_definition_default_used_when_id_models_unset(isolated_db):
    """user 没写 <id>.models 时，effective model 列表取 definition.models。"""
    from accounts import control_plane

    cc.save_definitions(
        [
            {
                "id": "zchan2",
                "display_name": "Z2",
                "base_url": "https://z2.example.com/v1",
                "models": ["seed-only"],
                "aliases": {"auto": "seed-only"},
                "env_api_key": "",
            }
        ]
    )
    view = control_plane.channel_model_view("zchan2")
    assert view["models"] == ["seed-only"]
    assert view["defaults"]["models"] == ["seed-only"]