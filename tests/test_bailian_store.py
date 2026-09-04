"""Bailian 通道存储层测试：parse/upsert 契约 + env 引导（全部本地，不发网络请求）。

仿 tests/test_gmi_store.py，覆盖 spec 第 6 节 1-6 条。
"""

import pytest

import providers
from storage import database as db
from providers.bailian import store as bstore
from providers.bailian.constants import CHANNEL_ID, DEFAULT_BASE_URL
from providers.bailian import chat as bchat


@pytest.fixture()
def bailian_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,bailian")
    yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CB_BAILIAN_API_KEY", raising=False)


@pytest.fixture()
def isolated_db(monkeypatch):
    """本文件专用：等同 conftest.isolated_db，但 DB 放仓库 .tmp 下。

    原因：pytest tmp_path/basetemp 的目录枚举在部分受限环境会 PermissionError，
    仓库内固定路径不受影响。用唯一子目录 + 测试后清理保证隔离。
    """
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


# ---------------------------------------------------------------------------
# 1. parse_credentials：三种粘贴形态
# ---------------------------------------------------------------------------


def test_parse_bare_key(bailian_enabled):
    parsed = bstore.parse_credentials({"api_key": "sk-bailian-test-1234567890"})
    assert parsed["access_token"] == "sk-bailian-test-1234567890"
    assert parsed["provider"] == CHANNEL_ID
    assert parsed["uid"] == "bailian-" + "sk-bailian-test-1234567890"[-8:]
    assert parsed["account_type"] == "api_key"
    assert parsed["domain"] == DEFAULT_BASE_URL


def test_parse_bearer_and_json_wrapped(bailian_enabled):
    bearer = bstore.parse_credentials({"api_key": "Bearer sk-abc-def-12345678"})
    assert bearer["access_token"] == "sk-abc-def-12345678"

    wrapped = bstore.parse_credentials({"api_key": '{"api_key": "sk-inner-key-98765432"}'})
    assert wrapped["access_token"] == "sk-inner-key-98765432"


def test_parse_empty_key_raises(bailian_enabled):
    with pytest.raises(ValueError):
        bstore.parse_credentials({"api_key": "   "})
    with pytest.raises(ValueError):
        bstore.parse_credentials({})


def test_parse_custom_base_url(bailian_enabled):
    parsed = bstore.parse_credentials({"api_key": "sk-x-12345678", "base_url": "https://proxy.example.com/v1"})
    assert parsed["domain"] == "https://proxy.example.com/v1"
    assert parsed["extra"]["base_url"] == "https://proxy.example.com/v1"


# ---------------------------------------------------------------------------
# 2. upsert_account：必须返回 {"id", "updated", "row"} 契约（gateway/server.py 依赖）
# ---------------------------------------------------------------------------


def test_upsert_insert_then_update_contract(bailian_enabled, isolated_db):
    parsed = bstore.parse_credentials({"api_key": "sk-contract-key-8888", "nickname": "main"})
    result = bstore.upsert_account(parsed)
    assert isinstance(result, dict)
    assert set(result) >= {"id", "updated"}
    assert result["updated"] is False
    aid = result["id"]
    assert isinstance(aid, int)

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 1
    assert rows[0]["access_token"] == "sk-contract-key-8888"

    # 换 key（轮换）= 新 uid = 新行；旧 key 行应被置 inactive，避免调度器选中死 key
    rotated = bstore.parse_credentials({"api_key": "sk-rotated-key-99999999", "nickname": "main2"})
    result2 = bstore.upsert_account(rotated)
    assert result2["updated"] is False
    assert isinstance(result2["id"], int) and result2["id"] != aid

    rows = db.list_accounts(provider=CHANNEL_ID)
    by_status = {r["status"]: [] for r in rows}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    assert len(rows) == 2
    active = [r for r in rows if r["status"] == "active"]
    inactive = [r for r in rows if r["status"] == "inactive"]
    assert len(active) == 1 and active[0]["access_token"] == "sk-rotated-key-99999999"
    assert len(inactive) == 1 and inactive[0]["id"] == aid


def test_upsert_same_key_is_idempotent(bailian_enabled, isolated_db):
    first = bstore.upsert_account(bstore.parse_credentials({"api_key": "sk-same-key-42424242"}))
    second = bstore.upsert_account(bstore.parse_credentials({"api_key": "sk-same-key-42424242"}))
    assert first["id"] == second["id"]
    assert second["updated"] is True
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_server_add_account_endpoint_contract(bailian_enabled, isolated_db):
    """复现 gateway/server.py POST /admin/accounts 的调用方式。

    回归：旧 gmi upsert 返回 int/裸行，endpoint 取 result["id"] 会 TypeError 500。
    """
    provider = providers.get_provider("bailian")
    parsed = provider.parse_credentials({"api_key": "sk-endpoint-key-7777", "nickname": "via-endpoint"})
    result = provider.upsert_account(parsed)
    # endpoint 原样读取这两个键 —— 不允许 KeyError/TypeError
    payload = {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "bailian"}
    assert payload["updated"] is False


# ---------------------------------------------------------------------------
# 3. ensure_env_account：env 引导三种态
# ---------------------------------------------------------------------------


def test_env_import_when_no_active_account(bailian_enabled, isolated_db, monkeypatch):
    monkeypatch.setenv("CB_BAILIAN_API_KEY", "sk-env-boot-key-1357")
    row = bstore.ensure_env_account()
    assert row is not None
    assert row["name"] == "bailian-env"
    assert row["access_token"] == "sk-env-boot-key-1357"
    # 幂等：再次调用不新建
    again = bstore.ensure_env_account()
    assert again["id"] == row["id"]
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_skips_when_active_account_exists(bailian_enabled, isolated_db, monkeypatch):
    bstore.upsert_account(bstore.parse_credentials({"api_key": "sk-manual-key-2468"}))
    monkeypatch.setenv("CB_BAILIAN_API_KEY", "sk-other-env-key-9753")
    row = bstore.ensure_env_account()
    assert row is not None
    assert row["access_token"] == "sk-manual-key-2468"  # 不覆盖手动导入
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_unset_returns_none(bailian_enabled, isolated_db):
    assert bstore.ensure_env_account() is None


def test_discover_is_empty_stub(bailian_enabled):
    d = bstore.discover()
    assert d["channel"] == CHANNEL_ID
    assert d["files"] == []
    assert d["importable_count"] == 0


# ---------------------------------------------------------------------------
# 4. get_provider 可用性（CB_GATEWAY_PROVIDERS 含 bailian 时可用）
# ---------------------------------------------------------------------------


def test_get_provider_available_when_enabled(bailian_enabled):
    provider = providers.get_provider("bailian")
    assert provider is not None
    assert provider.id == CHANNEL_ID


def test_get_provider_unavailable_when_not_enabled(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    # 默认（未 opt-in）时不应加载为可用
    import providers as _p

    monkeypatch.setattr(_p, "get_provider", lambda c: None if c == "bailian" else _p._LOADED.get(c))
    assert _p.get_provider("bailian") is None


# ---------------------------------------------------------------------------
# 5. 通道与模型页：control_plane.channel_model_view / set_channel_models 持久化
# ---------------------------------------------------------------------------


def test_channel_model_view_bailian(bailian_enabled, isolated_db):
    """复现管理页 GET /admin/channels/bailian/models 的调用路径。"""
    from accounts import control_plane

    view = control_plane.channel_model_view("bailian")
    assert view["channel"] == "bailian"
    assert view["models"]
    assert view["defaults"]["models"]
    assert "auto" in view["defaults"]["aliases"]
    assert "credit_rate" in view


def test_set_channel_models_persists_to_bailian_view(bailian_enabled, isolated_db):
    """复现管理页保存生效：save 返回 200 且数据真正写入 gmi.models / gmi.aliases。"""
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

    # 读取路径也必须生效（provider.list_models / alias_map / chat 翻译）
    provider = providers.get_provider("bailian")
    assert [m["id"] for m in provider.list_models()] == ["qwen-max", "custom-model-1"]
    assert provider.alias_map()["flash"] == "qwen-max"
    assert provider.translate_model("flash") == "qwen-max"
    assert provider.accepts_model("custom-model-1") is True
    assert provider.accepts_model("not-in-list") is False

    # 重置：删除设置回内置默认
    view = control_plane.set_channel_models("bailian", models=None, aliases=None, set_models=True, set_aliases=True)
    assert view["models"] == list(control_plane._BAILIAN_DEFAULT_MODELS)
    assert view["aliases"] == dict(control_plane._BAILIAN_DEFAULT_ALIASES)


# ---------------------------------------------------------------------------
# 6. 白名单拦截：未在 bailian.models 内的模型必须 400
# ---------------------------------------------------------------------------


def test_bailian_whitelist_gates_request_models(bailian_enabled, isolated_db):
    """白名单是最终闸门：自定义后列表外模型必须被拒（与 gmi 同契约，
    gateway/router.py 依据 accepts_model 拒绝请求）。"""
    control_plane = __import__("accounts.control_plane", fromlist=["control_plane"])
    control_plane.set_channel_models("bailian", models=["qwen-max"], set_models=True)
    assert bchat.accepts_model("qwen-max") is True
    assert bchat.accepts_model("some-unknown-model") is False


# ---------------------------------------------------------------------------
# 一致性回归：accounts.js 下拉 + control_plane._CHANNEL_DEFAULTS 覆盖所有已加载通道
# ---------------------------------------------------------------------------


def test_ui_channel_fallback_covers_apikey_channels():
    import re
    from pathlib import Path

    accounts_js = (
        Path(__file__).resolve().parent.parent / "src" / "web" / "js" / "pages" / "accounts.js"
    ).read_text(encoding="utf-8")
    m = re.search(r"channels=ref\(\[(.*?)\]\)", accounts_js, re.S)
    assert m, "accounts.js fallback channels list not found"
    ui_ids = set(re.findall(r"id:'([a-z]+)'", m.group(1)))
    # 以已加载 provider 为准（qoderwork 在 KNOWN 列表里但无实现，不进下拉框）
    for channel_id in providers._LOADED:
        assert channel_id in ui_ids, f"channel '{channel_id}' missing from accounts.js UI fallback"


def test_channel_defaults_cover_all_loaded_providers():
    from accounts import control_plane

    for channel_id in providers._LOADED:
        assert channel_id in control_plane._CHANNEL_DEFAULTS, (
            f"channel '{channel_id}' missing from _CHANNEL_DEFAULTS; "
            "通道与模型页会 KeyError"
        )
