"""GMI 通道(迁移后)等价测试：seed 写入的 custom 通道 + 基类契约。

旧 `tests/test_gmi_store.py` 覆盖了：
  * parse_credentials 三种粘贴形态（裸 key / Bearer / JSON 包裹）
  * upsert_account 契约 {"id","updated","row"} + 换 key 置老行 inactive + 同 key 幂等
  * ensure_env_account 三态（env 未设 / env 首次引导 / env 已存在 active）
  * discover 空壳 + provider 入口端到端契约
  * channel_model_view / set_channel_models 持久化与白名单拦截

迁移后 src/providers/gmi/ 已删除，gmi 变成 `custom_channels` settings
里的一个 seed 定义 + OpenAICompatProvider 实例。本测试在每个用例 fixture
里先调 `custom_channels.seed_initial_definitions()`，把 gmi 写入 settings，
并断言所有原断言依然成立。
"""

import pytest

import providers
from storage import database as db
from providers import custom_channels as cc

CHANNEL_ID = "gmi"
SEED_DEFAULT_BASE_URL = "https://api.gmi-serving.com/v1"


@pytest.fixture()
def gmi_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,gmi")
    yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CB_GMI_API_KEY", raising=False)


@pytest.fixture()
def isolated_db(monkeypatch):
    """本文件专用：等同 conftest.isolated_db，但 DB 放仓库 .tmp 下。"""
    import shutil
    import uuid
    from pathlib import Path

    from storage import credential_crypto
    from storage import credit_cache

    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"gmi-test-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "DB_PATH", workdir / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    credit_cache.invalidate()
    credit_cache.mark_refreshing(False)
    db.init_db()
    yield workdir / "gateway.db"
    credential_crypto.reset_cache()
    credit_cache.invalidate()
    credit_cache.mark_refreshing(False)
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture()
def gmi_seeded(gmi_enabled, isolated_db):
    """Seed gmi into the custom_channels settings key so the channel has
    exactly the data shape a freshly-booted instance would have."""
    cc.seed_initial_definitions()
    cc.invalidate_cache(CHANNEL_ID)
    yield providers.get_provider(CHANNEL_ID)


# ---------------------------------------------------------------------------
# parse_credentials：三种粘贴形态（基类 OpenAICompatProvider.parse_credentials）
# ---------------------------------------------------------------------------


def test_parse_bare_key(gmi_seeded):
    parsed = gmi_seeded.parse_credentials({"api_key": "sk-gmi-test-1234567890"})
    assert parsed["access_token"] == "sk-gmi-test-1234567890"
    assert parsed["provider"] == CHANNEL_ID
    assert parsed["uid"] == "gmi-" + "sk-gmi-test-1234567890"[-8:]
    assert parsed["account_type"] == "api_key"
    assert parsed["domain"] == SEED_DEFAULT_BASE_URL


def test_parse_bearer_and_json_wrapped(gmi_seeded):
    bearer = gmi_seeded.parse_credentials({"api_key": "Bearer sk-abc-def-12345678"})
    assert bearer["access_token"] == "sk-abc-def-12345678"

    wrapped = gmi_seeded.parse_credentials({"api_key": '{"api_key": "sk-inner-key-98765432"}'})
    assert wrapped["access_token"] == "sk-inner-key-98765432"


def test_parse_empty_key_raises(gmi_seeded):
    with pytest.raises(ValueError):
        gmi_seeded.parse_credentials({"api_key": "   "})
    with pytest.raises(ValueError):
        gmi_seeded.parse_credentials({})


def test_parse_custom_base_url(gmi_seeded):
    parsed = gmi_seeded.parse_credentials({"api_key": "sk-x-12345678", "base_url": "https://proxy.example.com/v1"})
    assert parsed["domain"] == "https://proxy.example.com/v1"
    assert parsed["extra"]["base_url"] == "https://proxy.example.com/v1"


# ---------------------------------------------------------------------------
# upsert_account：必须返回 {"id", "updated"} 契约（gateway/server.py 依赖）
# ---------------------------------------------------------------------------


def test_upsert_insert_then_update_contract(gmi_seeded, isolated_db):
    parsed = gmi_seeded.parse_credentials({"api_key": "sk-contract-key-8888", "nickname": "main"})
    result = gmi_seeded.upsert_account(parsed)
    assert isinstance(result, dict)
    assert set(result) >= {"id", "updated"}
    assert result["updated"] is False
    aid = result["id"]
    assert isinstance(aid, int)

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 1
    assert rows[0]["access_token"] == "sk-contract-key-8888"

    # 换 key（轮换）= 新 uid = 新行；旧 key 行应被置 inactive，避免调度器选中死 key
    rotated = gmi_seeded.parse_credentials({"api_key": "sk-rotated-key-99999999", "nickname": "main2"})
    result2 = gmi_seeded.upsert_account(rotated)
    assert result2["updated"] is False
    assert isinstance(result2["id"], int) and result2["id"] != aid

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 2
    active = [r for r in rows if r["status"] == "active"]
    inactive = [r for r in rows if r["status"] == "inactive"]
    assert len(active) == 1 and active[0]["access_token"] == "sk-rotated-key-99999999"
    assert len(inactive) == 1 and inactive[0]["id"] == aid


def test_upsert_same_key_is_idempotent(gmi_seeded, isolated_db):
    first = gmi_seeded.upsert_account(gmi_seeded.parse_credentials({"api_key": "sk-same-key-42424242"}))
    second = gmi_seeded.upsert_account(gmi_seeded.parse_credentials({"api_key": "sk-same-key-42424242"}))
    assert first["id"] == second["id"]
    assert second["updated"] is True
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_server_add_account_endpoint_contract(gmi_seeded, isolated_db):
    """复现 gateway/server.py POST /admin/accounts 的调用方式。"""
    provider = providers.get_provider("gmi")
    parsed = provider.parse_credentials({"api_key": "sk-endpoint-key-7777", "nickname": "via-endpoint"})
    result = provider.upsert_account(parsed)
    # endpoint 原样读取这两个键 —— 不允许 KeyError/TypeError
    payload = {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "gmi"}
    assert payload["updated"] is False


# ---------------------------------------------------------------------------
# ensure_env_account：env 引导语义
# ---------------------------------------------------------------------------


def test_env_import_when_no_active_account(gmi_seeded, isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GMI_API_KEY", "sk-env-boot-key-1357")
    row = gmi_seeded.ensure_env_account()
    assert row is not None
    assert row["name"] == "gmi-env"
    assert row["access_token"] == "sk-env-boot-key-1357"
    # 幂等：再次调用不新建
    again = gmi_seeded.ensure_env_account()
    assert again["id"] == row["id"]
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_skips_when_active_account_exists(gmi_seeded, isolated_db, monkeypatch):
    gmi_seeded.upsert_account(gmi_seeded.parse_credentials({"api_key": "sk-manual-key-2468"}))
    monkeypatch.setenv("CB_GMI_API_KEY", "sk-other-env-key-9753")
    row = gmi_seeded.ensure_env_account()
    assert row is not None
    assert row["access_token"] == "sk-manual-key-2468"  # 不覆盖手动导入
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_unset_returns_none(gmi_seeded, isolated_db):
    assert gmi_seeded.ensure_env_account() is None


def test_discover_is_empty_stub(gmi_seeded):
    d = gmi_seeded.discover()
    assert d["channel"] == CHANNEL_ID
    assert d["files"] == []
    assert d["importable_count"] == 0


# ---------------------------------------------------------------------------
# 通道与模型页回归：seed 定义 + 用户自定义都能正确取到 / 设置
# ---------------------------------------------------------------------------


def test_channel_model_view_gmi(gmi_seeded, isolated_db):
    """复现管理页 GET /admin/channels/gmi/models 的调用路径。"""
    from accounts import control_plane

    view = control_plane.channel_model_view("gmi")
    assert view["channel"] == "gmi"
    assert view["models"]
    assert view["defaults"]["models"]
    assert "auto" in view["defaults"]["aliases"]
    assert "credit_rate" in view


def test_set_channel_models_persists_to_gmi_view(gmi_seeded, isolated_db):
    """复现管理页保存生效：save 返回 200 且数据真正写入 gmi.models / gmi.aliases。"""
    from accounts import control_plane

    view = control_plane.set_channel_models(
        "gmi",
        models=["zai-org/GLM-5.3-Flash", "custom-model-1"],
        aliases={"auto": "custom-model-1", "flash": "zai-org/GLM-5.3-Flash"},
        set_models=True,
        set_aliases=True,
    )
    assert view["models"] == ["zai-org/GLM-5.3-Flash", "custom-model-1"]
    assert view["aliases"] == {"auto": "custom-model-1", "flash": "zai-org/GLM-5.3-Flash"}
    assert view["customized"] == {"models": True, "aliases": True}

    # 读取路径也必须生效（provider.list_models / alias_map / chat 翻译）
    provider = providers.get_provider("gmi")
    assert [m["id"] for m in provider.list_models()] == ["zai-org/GLM-5.3-Flash", "custom-model-1"]
    assert provider.alias_map()["flash"] == "zai-org/GLM-5.3-Flash"
    assert provider.translate_model("flash") == "zai-org/GLM-5.3-Flash"
    assert provider.accepts_model("custom-model-1") is True
    assert provider.accepts_model("not-in-list") is False

    # 重置：删除设置回 seed 默认
    view = control_plane.set_channel_models("gmi", models=None, aliases=None, set_models=True, set_aliases=True)
    # defaults 应回滚到 seed 定义里的 models / aliases
    definition = cc.get_definition("gmi")
    assert view["models"] == list(definition["models"])
    assert view["aliases"] == dict(definition["aliases"])


def test_gmi_whitelist_gates_request_models(gmi_seeded, isolated_db):
    """白名单是最终闸门：自定义后列表外模型必须被拒（与 qclaw 同契约）。"""
    from accounts import control_plane
    from providers import custom_channels as _cc

    control_plane.set_channel_models("gmi", models=["zai-org/GLM-5.3-Flash"], set_models=True)
    # Invalidate so the rebuilt provider sees the persisted <id>.models key.
    _cc.invalidate_cache("gmi")
    provider = providers.get_provider("gmi")
    assert provider.accepts_model("zai-org/GLM-5.3-Flash") is True
    assert provider.accepts_model("some-unknown-model") is False


# ---------------------------------------------------------------------------
# seed 形态断言：seed 写入必须满足 spec §5 描述的所有字段
# ---------------------------------------------------------------------------


def test_seed_definition_full_shape(gmi_enabled, isolated_db):
    cc.seed_initial_definitions()
    definition = cc.get_definition("gmi")
    assert definition is not None
    assert definition["id"] == "gmi"
    assert definition["display_name"] == "GMI Cloud"
    assert definition["base_url"] == SEED_DEFAULT_BASE_URL
    assert definition["models"] == ["zai-org/GLM-5.3-Flash"]
    assert "auto" in definition["aliases"]
    assert definition["aliases"]["auto"] == "zai-org/GLM-5.3-Flash"
    assert definition["env_api_key"] == "CB_GMI_API_KEY"
    assert definition["source"] == "seed"
    assert isinstance(definition["created_at"], int) and definition["created_at"] > 0