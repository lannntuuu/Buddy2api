"""Bailian 通道(迁移后)等价测试：seed 写入的 custom 通道 + 基类契约。

仿 tests/test_custom_channels_gmi.py，覆盖 spec 第 7 节 1-6 条。
"""

import pytest

import providers
from storage import database as db
from providers import custom_channels as cc

CHANNEL_ID = "bailian"
SEED_DEFAULT_BASE_URL = "https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@pytest.fixture()
def bailian_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,bailian")
    yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CB_BAILIAN_API_KEY", raising=False)


@pytest.fixture()
def isolated_db(monkeypatch):
    """本文件专用：等同 conftest.isolated_db，但 DB 放仓库 .tmp 下。"""
    import shutil
    import uuid
    from pathlib import Path

    from storage import credential_crypto
    from storage import credit_cache

    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"bailian-test-{uuid.uuid4().hex[:8]}"
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
def bailian_seeded(bailian_enabled, isolated_db):
    """Seed bailian into the custom_channels settings key so the channel has
    exactly the data shape a freshly-booted instance would have."""
    cc.seed_initial_definitions()
    cc.invalidate_cache(CHANNEL_ID)
    yield providers.get_provider(CHANNEL_ID)


# ---------------------------------------------------------------------------
# parse_credentials：三种粘贴形态（基类 OpenAICompatProvider.parse_credentials）
# ---------------------------------------------------------------------------


def test_parse_bare_key(bailian_seeded):
    parsed = bailian_seeded.parse_credentials({"api_key": "sk-bailian-test-1234567890"})
    assert parsed["access_token"] == "sk-bailian-test-1234567890"
    assert parsed["provider"] == CHANNEL_ID
    assert parsed["uid"] == "bailian-" + "sk-bailian-test-1234567890"[-8:]
    assert parsed["account_type"] == "api_key"
    assert parsed["domain"] == SEED_DEFAULT_BASE_URL


def test_parse_bearer_and_json_wrapped(bailian_seeded):
    bearer = bailian_seeded.parse_credentials({"api_key": "Bearer sk-abc-def-12345678"})
    assert bearer["access_token"] == "sk-abc-def-12345678"

    wrapped = bailian_seeded.parse_credentials({"api_key": '{"api_key": "sk-inner-key-98765432"}'})
    assert wrapped["access_token"] == "sk-inner-key-98765432"


def test_parse_empty_key_raises(bailian_seeded):
    with pytest.raises(ValueError):
        bailian_seeded.parse_credentials({"api_key": "   "})
    with pytest.raises(ValueError):
        bailian_seeded.parse_credentials({})


def test_parse_custom_base_url(bailian_seeded):
    parsed = bailian_seeded.parse_credentials({"api_key": "sk-x-12345678", "base_url": "https://proxy.example.com/v1"})
    assert parsed["domain"] == "https://proxy.example.com/v1"
    assert parsed["extra"]["base_url"] == "https://proxy.example.com/v1"


# ---------------------------------------------------------------------------
# upsert_account：必须返回 {"id", "updated", "row"} 契约（gateway/server.py 依赖）
# ---------------------------------------------------------------------------


def test_upsert_insert_then_update_contract(bailian_seeded, isolated_db):
    parsed = bailian_seeded.parse_credentials({"api_key": "sk-contract-key-8888", "nickname": "main"})
    result = bailian_seeded.upsert_account(parsed)
    assert isinstance(result, dict)
    assert set(result) >= {"id", "updated"}
    assert result["updated"] is False
    aid = result["id"]
    assert isinstance(aid, int)

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 1
    assert rows[0]["access_token"] == "sk-contract-key-8888"

    # 换 key（轮换）= 新 uid = 新行；多 Key 规格下新旧行并存且都 active（spec 21 §3）
    rotated = bailian_seeded.parse_credentials({"api_key": "sk-rotated-key-99999999", "nickname": "main2"})
    result2 = bailian_seeded.upsert_account(rotated)
    assert result2["updated"] is False
    assert isinstance(result2["id"], int) and result2["id"] != aid

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 2
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 2
    assert {r["access_token"] for r in active} == {"sk-contract-key-8888", "sk-rotated-key-99999999"}


def test_upsert_same_key_is_idempotent(bailian_seeded, isolated_db):
    first = bailian_seeded.upsert_account(bailian_seeded.parse_credentials({"api_key": "sk-same-key-42424242"}))
    second = bailian_seeded.upsert_account(bailian_seeded.parse_credentials({"api_key": "sk-same-key-42424242"}))
    assert first["id"] == second["id"]
    assert second["updated"] is True
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_server_add_account_endpoint_contract(bailian_seeded, isolated_db):
    """复现 gateway/server.py POST /admin/accounts 的调用方式。"""
    provider = providers.get_provider("bailian")
    parsed = provider.parse_credentials({"api_key": "sk-endpoint-key-7777", "nickname": "via-endpoint"})
    result = provider.upsert_account(parsed)
    payload = {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "bailian"}
    assert payload["updated"] is False


# ---------------------------------------------------------------------------
# ensure_env_account：env 引导三种态
# ---------------------------------------------------------------------------


def test_env_import_when_no_active_account(bailian_seeded, isolated_db, monkeypatch):
    monkeypatch.setenv("CB_BAILIAN_API_KEY", "sk-env-boot-key-1357")
    row = bailian_seeded.ensure_env_account()
    assert row is not None
    assert row["name"] == "bailian-env"
    assert row["access_token"] == "sk-env-boot-key-1357"
    again = bailian_seeded.ensure_env_account()
    assert again["id"] == row["id"]
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_skips_when_active_account_exists(bailian_seeded, isolated_db, monkeypatch):
    bailian_seeded.upsert_account(bailian_seeded.parse_credentials({"api_key": "sk-manual-key-2468"}))
    monkeypatch.setenv("CB_BAILIAN_API_KEY", "sk-other-env-key-9753")
    row = bailian_seeded.ensure_env_account()
    assert row is not None
    assert row["access_token"] == "sk-manual-key-2468"
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_unset_returns_none(bailian_seeded, isolated_db):
    assert bailian_seeded.ensure_env_account() is None


def test_discover_is_empty_stub(bailian_seeded):
    d = bailian_seeded.discover()
    assert d["channel"] == CHANNEL_ID
    assert d["files"] == []
    assert d["importable_count"] == 0


# ---------------------------------------------------------------------------
# 通道与模型页：control_plane.channel_model_view / set_channel_models 持久化
# ---------------------------------------------------------------------------


def test_channel_model_view_bailian(bailian_seeded, isolated_db):
    from accounts import control_plane

    view = control_plane.channel_model_view("bailian")
    assert view["channel"] == "bailian"
    assert view["models"]
    assert view["defaults"]["models"]
    assert "auto" in view["defaults"]["aliases"]
    assert "credit_rate" in view


def test_set_channel_models_persists_to_bailian_view(bailian_seeded, isolated_db):
    from accounts import control_plane

    view = control_plane.set_channel_models(
        "bailian",
        models=["qwen-max", "custom-model-1"],
        aliases={"auto": "custom-model-1", "flash": "qwen-max"},
        set_models=True,
        set_aliases=True,
    )
    assert view["models"] == ["qwen-max", "custom-model-1"]
    assert view["aliases"] == {"auto": "custom-model-1", "flash": "qwen-max"}
    assert view["customized"] == {"models": True, "aliases": True}

    provider = providers.get_provider("bailian")
    assert [m["id"] for m in provider.list_models()] == ["qwen-max", "custom-model-1"]
    assert provider.alias_map()["flash"] == "qwen-max"
    assert provider.translate_model("flash") == "qwen-max"
    assert provider.accepts_model("custom-model-1") is True
    assert provider.accepts_model("not-in-list") is False

    # 重置：删除设置回 seed 默认
    view = control_plane.set_channel_models("bailian", models=None, aliases=None, set_models=True, set_aliases=True)
    definition = cc.get_definition("bailian")
    assert view["models"] == list(definition["models"])
    assert view["aliases"] == dict(definition["aliases"])


def test_bailian_whitelist_gates_request_models(bailian_seeded, isolated_db):
    """白名单是最终闸门：自定义后列表外模型必须被拒。"""
    from accounts import control_plane
    from providers import custom_channels as _cc

    control_plane.set_channel_models("bailian", models=["qwen-max"], set_models=True)
    _cc.invalidate_cache("bailian")
    provider = providers.get_provider("bailian")
    assert provider.accepts_model("qwen-max") is True
    assert provider.accepts_model("some-unknown-model") is False


# ---------------------------------------------------------------------------
# seed 形态断言
# ---------------------------------------------------------------------------


def test_seed_definition_full_shape(bailian_enabled, isolated_db):
    cc.seed_initial_definitions()
    definition = cc.get_definition("bailian")
    assert definition is not None
    assert definition["id"] == "bailian"
    assert definition["display_name"] == "阿里百炼 Bailian"
    assert definition["base_url"] == SEED_DEFAULT_BASE_URL
    assert definition["models"] == ["qwen-plus"]
    assert "auto" in definition["aliases"]
    assert definition["aliases"]["auto"] == "qwen-plus"
    assert definition["env_api_key"] == "CB_BAILIAN_API_KEY"
    assert definition["source"] == "seed"
    assert isinstance(definition["created_at"], int) and definition["created_at"] > 0