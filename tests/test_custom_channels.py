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
    with pytest.raises(ValueError, match="已被占用"):
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


def test_validate_rejects_non_http_scheme():
    """非 http(s) 方案（无协议头 / ftp 等）必须拒绝。"""
    with pytest.raises(ValueError, match="需以 http:// 或 https:// 开头"):
        cc.validate_definition(
            {"id": "a", "display_name": "X", "base_url": "ftp://example.com/v1", "models": ["m"]},
            reserved_ids=set(),
        )
    with pytest.raises(ValueError, match="需以 http:// 或 https:// 开头"):
        cc.validate_definition(
            {"id": "a", "display_name": "X", "base_url": "example.com/v1", "models": ["m"]},
            reserved_ids=set(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/v1",
        "http://127.0.0.1:8000/v1",
        "http://localhost/v1",
        "http://localhost:8000/v1",
        "http://192.168.1.10:8000/v1",   # 内网 http(放宽后放行,前端给出明文警告)
        "http://example.com/v1",          # 公网 http 也放行,由管理员自行权衡
        "https://api.example.com/v1",
    ],
)
def test_validate_accepts_http_and_https(url):
    """任意 http:// / https:// 都放行（含内网/公网 http）。"""
    cc.validate_definition(
        {"id": "ok", "display_name": "X", "base_url": url, "models": ["m"]},
        reserved_ids=set(),
    )


def test_validate_rejects_alias_pointing_to_missing_model():
    with pytest.raises(ValueError, match="不在模型白名单中"):
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
        "cb_upper",          # lowercase prefix
        "CB_",               # empty suffix
        "CB_X!",             # illegal character
    ],
)
def test_validate_rejects_bad_env_name(env_name):
    """Spec §3.1: env_api_key 必须匹配 ^CB_[A-Z0-9_-]+$（连字符与通道 id 对齐）。"""
    with pytest.raises(ValueError, match="环境变量名"):
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
    ["CB_X", "CB_BAILIAN_API_KEY", "CB_MY_KEY_2", "CB_QWEN-27B", "CB_GMI-KEY"],
)
def test_validate_accepts_well_formed_env_name(env_name):
    """合法 env_api_key（CB_ + 大写字母/数字/下划线/连字符 1+ 字符）放行；
    含自动填充产物 CB_+id.upper()（id 允许连字符，spec 23 §1.2）。"""
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
    """user 写了 <id>.models → effective model 列表取 user 设置。

    双向同步语义（spec 通道双入口）：模型配置页保存会镜像写回
    definition.models，所以 defaults 与生效列表一致（不再是旧兜底值）；
    reset（传 null）只删覆盖键，definition 自身值重新成为默认。
    """
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
    # 镜像后 definition.models 同步为保存值 → defaults 展示同一份
    assert view["defaults"]["models"] == ["custom-model-1", "custom-model-2"]

    # reset（传 null）只删覆盖键：definition 原值重新成为生效默认
    control_plane.set_channel_models("zchan", models=None, set_models=True)
    view = control_plane.channel_model_view("zchan")
    assert view["models"] == ["custom-model-1", "custom-model-2"]  # 镜像后的定义值兜底
    from storage import database as db
    assert db.get_setting("zchan.models") is None


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


# ---------------------------------------------------------------------------
# spec 23 §1：models / env_api_key 可选化（纯校验层不报错）
# ---------------------------------------------------------------------------


def test_validate_accepts_omitted_models():
    """models 缺省 / None / 空数组都通过纯校验（默认补值放 handler）。"""
    base = {"id": "ok", "display_name": "X", "base_url": "https://x/v1"}
    cc.validate_definition({**base}, reserved_ids=set())
    cc.validate_definition({**base, "models": None}, reserved_ids=set())
    cc.validate_definition({**base, "models": []}, reserved_ids=set())


def test_validate_accepts_omitted_env_name():
    """env_api_key 缺省 / None / 空字符串都通过纯校验。"""
    base = {"id": "ok", "display_name": "X", "base_url": "https://x/v1"}
    cc.validate_definition({**base}, reserved_ids=set())
    cc.validate_definition({**base, "env_api_key": None}, reserved_ids=set())
    cc.validate_definition({**base, "env_api_key": ""}, reserved_ids=set())


def test_validate_alias_may_point_to_default_model_when_models_omitted():
    """models 缺省时用默认模型做别名校验;指向默认模型的别名放行(§5)。"""
    cc.validate_definition(
        {
            "id": "ok",
            "display_name": "X",
            "base_url": "https://x/v1",
            "aliases": {"auto": "DeepSeek-V4-Flash"},
        },
        reserved_ids=set(),
    )


def test_validate_rejects_alias_pointing_to_unknown_model():
    """别名指向既非用户模型也非默认模型的 id 仍报错。"""
    with pytest.raises(ValueError, match="不在模型白名单中"):
        cc.validate_definition(
            {
                "id": "ok",
                "display_name": "X",
                "base_url": "https://x/v1",
                "aliases": {"auto": "does-not-exist"},
            },
            reserved_ids=set(),
        )


# ---------------------------------------------------------------------------
# spec 23 §1 handler 级：POST /admin/channels/custom 不带 models/env → 补默认
# ---------------------------------------------------------------------------


def _make_request(body: dict):
    """构造一个最小 Starlette Request, body 走 _read_json_object 的 stream()。"""
    import json
    from starlette.requests import Request

    raw = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/admin/channels/custom",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    return Request(scope, receive=receive)


def test_post_custom_fills_default_models_and_generated_env(isolated_db, monkeypatch):
    """POST 不带 models / env_api_key(但带 api_key 走完整链路)→
    definition.models == ['DeepSeek-V4-Flash'] 且 env_api_key == 'CB_<ID 大写>'。"""
    import asyncio
    import providers
    from gateway import deps as _deps
    from gateway.routers import admin as _admin

    # 关掉管理鉴权,让 handler 直接跑
    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)
    # 显式移除 env 锁定,保证「默认启用」断言确定(见下方)
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)

    cid = "siliconflow"

    async def run():
        req = _make_request(
            {
                "id": cid,
                "display_name": "硅基流动",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "sk-test-siliconflow-1234567890",
            }
        )
        return await _admin.admin_create_custom_channel(req, authorization=None)

    out = asyncio.run(run())
    assert out["id"] == cid
    assert out["models"] == ["DeepSeek-V4-Flash"]
    assert out["env_api_key"] == "CB_" + cid.upper()

    # 持久化定义也确认
    stored = cc.get_definition(cid)
    assert stored["models"] == ["DeepSeek-V4-Flash"]
    assert stored["env_api_key"] == "CB_" + cid.upper()

    # 新建通道默认启用(全新 DB 无 enabled_channels 键,回退全量 known 集合
    # 已含新 cid,属幂等 no-write,但可读性上必须可调度)
    assert cid in providers.enabled_provider_ids()
    assert providers.is_channel_enabled(cid) is True


def test_post_custom_explicit_models_and_env_passthrough(isolated_db, monkeypatch):
    """显式传 models / env_api_key 仍原样保存(不覆盖)。"""
    import asyncio
    from gateway import deps as _deps
    from gateway.routers import admin as _admin

    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    cid = "deepseekx"

    async def run():
        req = _make_request(
            {
                "id": cid,
                "display_name": "DSX",
                "base_url": "https://api.dsx/v1",
                "models": ["m1", "m2"],
                "env_api_key": "CB_DSX_KEY",
                "api_key": "sk-dsx-test-1234567890",
            }
        )
        return await _admin.admin_create_custom_channel(req, authorization=None)

    out = asyncio.run(run())
    assert out["models"] == ["m1", "m2"]
    assert out["env_api_key"] == "CB_DSX_KEY"

    # 新建通道默认启用(同 test_post_custom_fills_default_models_and_generated_env)
    import providers

    assert cid in providers.enabled_provider_ids()
    assert providers.is_channel_enabled(cid) is True


def test_put_custom_blank_env_generates_by_path_param(isolated_db, monkeypatch):
    """edit 模式 PUT,body 不带 env_api_key → 按路径参数 cid 生成 CB_<大写>。"""
    import asyncio
    import json
    from starlette.requests import Request
    from gateway import deps as _deps
    from gateway.routers import admin as _admin

    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)
    cid = "editchan"
    cc.save_definitions(
        [
            {
                "id": cid,
                "display_name": "Edit",
                "base_url": "https://edit.example.com/v1",
                "models": ["a"],
                "env_api_key": "CB_OLD",
            }
        ]
    )

    async def receive():
        raw = json.dumps({"display_name": "Edit2", "models": []}).encode("utf-8")
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "PUT",
        "path": f"/admin/channels/custom/{cid}",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }

    async def run():
        req = Request(scope, receive=receive)
        return await _admin.admin_update_custom_channel(cid, req, authorization=None)

    out = asyncio.run(run())
    assert out["env_api_key"] == "CB_" + cid.upper()
    # 未传的 models 仍落默认
    assert out["models"] == ["DeepSeek-V4-Flash"]


# ---------------------------------------------------------------------------
# 删除自定义通道 = 真删账号行(不再留 inactive 孤儿)+ settings 残留全清
# ---------------------------------------------------------------------------


def test_delete_custom_channel_removes_accounts_and_residues(isolated_db, monkeypatch):
    """DELETE /admin/channels/custom/{cid}:定义消失;该 provider 的账号行
    (active + inactive)全部真删,响应计数键为 accounts_deleted;<cid>.models /
    <cid>.max_input_tokens 等 settings 残留一并清掉;其他通道账号不受影响。"""
    import asyncio
    import providers

    from fastapi import HTTPException
    from storage import database as db
    from gateway import deps as _deps
    from gateway.routers import admin as _admin

    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)

    cid = "delchan"
    cc.upsert_definition(
        {
            "id": cid,
            "display_name": "待删",
            "base_url": "https://del.example.com/v1",
            "models": ["m1"],
            "aliases": {},
            "env_api_key": "CB_DEL",
        }
    )
    # 通道级 settings 残留(含历史上漏清的 max_input_tokens 两键)
    db.set_setting(f"{cid}.models", ["m1"])
    db.set_setting(f"{cid}.max_input_tokens", 8192)
    db.set_setting(f"{cid}.max_input_tokens_by_model", {"m1": 4096})
    # 该通道的账号行:active + inactive 各一
    a_active = db.add_account(
        {"name": "k-active", "uid": "del-1", "provider": cid, "status": "active"}
    )
    a_inactive = db.add_account(
        {"name": "k-inactive", "uid": "del-2", "provider": cid, "status": "inactive"}
    )
    # 其他通道账号(不应被误删)
    a_wb = db.add_account(
        {"name": "wb", "uid": "wb-1", "provider": "workbuddy", "status": "active"}
    )

    async def run():
        return await _admin.admin_delete_custom_channel(cid, authorization=None)

    out = asyncio.run(run())
    assert out == {"status": "ok", "id": cid, "accounts_deleted": 2}

    # 定义消失,known 集合收敛
    assert cc.get_definition(cid) is None
    assert cid not in providers.known_channel_ids()
    # 账号行真删(不再留 inactive 孤儿)
    assert db.list_accounts(provider=cid) == []
    assert db.get_account(a_active) is None
    assert db.get_account(a_inactive) is None
    # settings 残留清干净(含 max_input_tokens 两键)
    assert db.get_setting(f"{cid}.models") is None
    assert db.get_setting(f"{cid}.max_input_tokens") is None
    assert db.get_setting(f"{cid}.max_input_tokens_by_model") is None
    # 其他通道账号不受影响
    assert db.get_account(a_wb) is not None

    # 再删一次 → 404
    async def run_404():
        return await _admin.admin_delete_custom_channel(cid, authorization=None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(run_404())
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 新建自定义通道默认启用(保序 + env 锁定跳过)
# ---------------------------------------------------------------------------


def test_post_custom_channel_enabled_by_default_preserves_order(isolated_db, monkeypatch):
    """DB 已有 enabled_channels 键(用户曾在 UI 勾过开关)时,新建通道仍默认
    启用,且原有通道相对顺序不变、workbuddy 仍居首、新 cid 追加在末尾。"""
    import asyncio
    import providers
    from gateway import deps as _deps
    from gateway.routers import admin as _admin

    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)

    # 模拟用户已在 UI 勾过开关/拖过序:DB 已有 enabled_channels 键
    providers.set_enabled_channels(
        ids=["workbuddy", "qclaw", "qwenwork"],
        order=["workbuddy", "qclaw", "qwenwork", "traework", "traesolo"],
    )
    before = providers.get_channel_order()
    assert before == ["workbuddy", "qclaw", "qwenwork"]

    cid = "autoflow"

    async def run():
        req = _make_request(
            {
                "id": cid,
                "display_name": "Auto",
                "base_url": "https://auto.example.com/v1",
            }
        )
        return await _admin.admin_create_custom_channel(req, authorization=None)

    out = asyncio.run(run())
    assert out["id"] == cid
    assert out["status"] == "ok"

    # 新通道默认启用
    assert cid in providers.enabled_provider_ids()
    assert providers.is_channel_enabled(cid) is True
    # 原有通道相对顺序不变,workbuddy 仍居首,新 cid 追加在末尾
    after = providers.get_channel_order()
    assert after[0] == "workbuddy"
    assert [c for c in after if c in before] == before
    assert after[-1] == cid


def test_post_custom_channel_env_locked_skips_enable(isolated_db, monkeypatch):
    """env 锁定(CB_GATEWAY_PROVIDERS)时创建不报错:定义照常保存,但启用态
    由 env 接管 —— 不写 DB,新 cid 不因本端点而启用。"""
    import asyncio
    import providers
    from storage import database as db
    from gateway import deps as _deps
    from gateway.routers import admin as _admin

    monkeypatch.setattr(_deps, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")

    cid = "lockedchan"

    async def run():
        req = _make_request(
            {
                "id": cid,
                "display_name": "Locked",
                "base_url": "https://locked.example.com/v1",
            }
        )
        return await _admin.admin_create_custom_channel(req, authorization=None)

    out = asyncio.run(run())
    assert out["id"] == cid
    assert out["status"] == "ok"
    # 定义已保存
    assert cc.get_definition(cid) is not None
    # env 分支直接 return:DB 启用态完全不被本端点写入
    assert db.get_setting("enabled_channels", None) is None
    assert providers.is_channel_enabled(cid) is False


# ---------------------------------------------------------------------------
# 启动自愈:孤儿账号行清扫(_lifespan 中 seed 之后)
# ---------------------------------------------------------------------------


def test_purge_orphan_accounts_removes_nonactive_unknown_provider_rows(isolated_db):
    """purge_orphan_accounts:provider 不在 known 集合(内置 ∪ 现存自定义
    定义)且非 active 的孤儿行被删;active 孤儿行保留;已知通道的账号行
    (内置 workbuddy / seed gmi)不受影响。"""
    from storage import database as db
    from gateway import server as _server

    # fresh-install 语义:seed 定义先落库(真实 _lifespan 中 purge 晚于 seed)
    cc.seed_initial_definitions()

    orphan_inactive = db.add_account(
        {"name": "ghost", "uid": "g-1", "provider": "ghost-ch", "status": "inactive"}
    )
    orphan_active = db.add_account(
        {"name": "ghost2", "uid": "g-2", "provider": "ghost-ch", "status": "active"}
    )
    gmi_row = db.add_account(
        {"name": "gmi1", "uid": "gmi-1", "provider": "gmi", "status": "inactive"}
    )
    wb_row = db.add_account(
        {"name": "wb", "uid": "wb-1", "provider": "workbuddy", "status": "inactive"}
    )

    removed = _server.purge_orphan_accounts()

    assert removed == 1
    assert db.get_account(orphan_inactive) is None
    # active 孤儿保留(用户可见可手动删,且本就无法被路由)
    assert db.get_account(orphan_active) is not None
    # 已知通道的行不受影响(定义存在 → 不算孤儿)
    assert db.get_account(gmi_row) is not None
    assert db.get_account(wb_row) is not None


def test_lifespan_seeds_then_purges_orphans(isolated_db):
    """_lifespan 全链路:先 seed gmi/bailian 定义,再清 inactive 孤儿行 ——
    顺序颠倒时 gmi/bailian 的行会被误删,本用例守这个插入点。"""
    import asyncio
    from storage import database as db
    from gateway import server as _server

    orphan = db.add_account(
        {"name": "ghost", "uid": "l-1", "provider": "ghost-ch", "status": "inactive"}
    )

    async def run():
        async with _server._lifespan(None):
            pass

    asyncio.run(run())

    assert db.get_account(orphan) is None
    # seed 定义已落库(purge 不误伤的前提)
    assert cc.get_definition("gmi") is not None
    assert cc.get_definition("bailian") is not None

