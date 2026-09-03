"""GMI 通道存储层测试：parse/upsert 契约 + env 引导（全部本地，不发网络请求）。"""

import pytest

import providers
from storage import database as db
from providers.gmi import store as gstore
from providers.gmi.constants import CHANNEL_ID, DEFAULT_BASE_URL


@pytest.fixture()
def gmi_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,gmi")
    yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CB_GMI_API_KEY", raising=False)


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


# ---------------------------------------------------------------------------
# parse_credentials：三种粘贴形态
# ---------------------------------------------------------------------------


def test_parse_bare_key(gmi_enabled):
    parsed = gstore.parse_credentials({"api_key": "sk-gmi-test-1234567890"})
    assert parsed["access_token"] == "sk-gmi-test-1234567890"
    assert parsed["provider"] == CHANNEL_ID
    assert parsed["uid"] == "gmi-" + "sk-gmi-test-1234567890"[-8:]
    assert parsed["account_type"] == "api_key"
    assert parsed["domain"] == DEFAULT_BASE_URL


def test_parse_bearer_and_json_wrapped(gmi_enabled):
    bearer = gstore.parse_credentials({"api_key": "Bearer sk-abc-def-12345678"})
    assert bearer["access_token"] == "sk-abc-def-12345678"

    wrapped = gstore.parse_credentials({"api_key": '{"api_key": "sk-inner-key-98765432"}'})
    assert wrapped["access_token"] == "sk-inner-key-98765432"


def test_parse_empty_key_raises(gmi_enabled):
    with pytest.raises(ValueError):
        gstore.parse_credentials({"api_key": "   "})
    with pytest.raises(ValueError):
        gstore.parse_credentials({})


def test_parse_custom_base_url(gmi_enabled):
    parsed = gstore.parse_credentials({"api_key": "sk-x-12345678", "base_url": "https://proxy.example.com/v1"})
    assert parsed["domain"] == "https://proxy.example.com/v1"
    assert parsed["extra"]["base_url"] == "https://proxy.example.com/v1"


# ---------------------------------------------------------------------------
# upsert_account：必须返回 {"id", "updated"} 契约（gateway/server.py 依赖）
# ---------------------------------------------------------------------------


def test_upsert_insert_then_update_contract(gmi_enabled, isolated_db):
    parsed = gstore.parse_credentials({"api_key": "sk-contract-key-8888", "nickname": "main"})
    result = gstore.upsert_account(parsed)
    assert isinstance(result, dict)
    assert set(result) >= {"id", "updated"}
    assert result["updated"] is False
    aid = result["id"]
    assert isinstance(aid, int)

    rows = db.list_accounts(provider=CHANNEL_ID)
    assert len(rows) == 1
    assert rows[0]["access_token"] == "sk-contract-key-8888"

    # 换 key（轮换）= 新 uid = 新行；旧 key 行应被置 inactive，避免调度器选中死 key
    rotated = gstore.parse_credentials({"api_key": "sk-rotated-key-99999999", "nickname": "main2"})
    result2 = gstore.upsert_account(rotated)
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


def test_upsert_same_key_is_idempotent(gmi_enabled, isolated_db):
    first = gstore.upsert_account(gstore.parse_credentials({"api_key": "sk-same-key-42424242"}))
    second = gstore.upsert_account(gstore.parse_credentials({"api_key": "sk-same-key-42424242"}))
    assert first["id"] == second["id"]
    assert second["updated"] is True
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_server_add_account_endpoint_contract(gmi_enabled, isolated_db):
    """复现 gateway/server.py POST /admin/accounts 的调用方式。

    回归：旧 gmi upsert 返回 int/裸行，endpoint 取 result["id"] 会 TypeError 500。
    """
    provider = providers.get_provider("gmi")
    parsed = provider.parse_credentials({"api_key": "sk-endpoint-key-7777", "nickname": "via-endpoint"})
    result = provider.upsert_account(parsed)
    # endpoint 原样读取这两个键 —— 不允许 KeyError/TypeError
    payload = {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "gmi"}
    assert payload["updated"] is False


# ---------------------------------------------------------------------------
# ensure_env_account：env 引导语义
# ---------------------------------------------------------------------------


def test_env_import_when_no_active_account(gmi_enabled, isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GMI_API_KEY", "sk-env-boot-key-1357")
    row = gstore.ensure_env_account()
    assert row is not None
    assert row["name"] == "gmi-env"
    assert row["access_token"] == "sk-env-boot-key-1357"
    # 幂等：再次调用不新建
    again = gstore.ensure_env_account()
    assert again["id"] == row["id"]
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_skips_when_active_account_exists(gmi_enabled, isolated_db, monkeypatch):
    gstore.upsert_account(gstore.parse_credentials({"api_key": "sk-manual-key-2468"}))
    monkeypatch.setenv("CB_GMI_API_KEY", "sk-other-env-key-9753")
    row = gstore.ensure_env_account()
    assert row is not None
    assert row["access_token"] == "sk-manual-key-2468"  # 不覆盖手动导入
    assert len(db.list_accounts(provider=CHANNEL_ID)) == 1


def test_env_unset_returns_none(gmi_enabled, isolated_db):
    assert gstore.ensure_env_account() is None


def test_discover_is_empty_stub(gmi_enabled):
    d = gstore.discover()
    assert d["channel"] == CHANNEL_ID
    assert d["files"] == []
    assert d["importable_count"] == 0


# ---------------------------------------------------------------------------
# UI 一致性回归：accounts.js 的硬编码 fallback 列表必须覆盖 api_key 类通道
# （漏掉 gmi 时下拉框选不到该通道，导入入口不可见 —— 本次修复的起因）
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


# ---------------------------------------------------------------------------
# 通道与模型页回归：control_plane._CHANNEL_DEFAULTS 必须覆盖所有已加载通道
# （漏配时打开「通道与模型」页对应该通道直接 KeyError 500 —— 本次线上报错）
# ---------------------------------------------------------------------------


def test_channel_defaults_cover_all_loaded_providers():
    from accounts import control_plane

    for channel_id in providers._LOADED:
        assert channel_id in control_plane._CHANNEL_DEFAULTS, (
            f"channel '{channel_id}' missing from _CHANNEL_DEFAULTS; "
            "通道与模型页会 KeyError"
        )


def test_channel_model_view_gmi(gmi_enabled, isolated_db):
    """复现管理页 GET /admin/channels/gmi/models 的调用路径。"""
    from accounts import control_plane

    view = control_plane.channel_model_view("gmi")
    assert view["channel"] == "gmi"
    assert view["models"]
    assert view["defaults"]["models"]
    assert "auto" in view["defaults"]["aliases"]
    assert "credit_rate" in view


# ---------------------------------------------------------------------------
# 各平台设置保存生效回归：channel_model_view / accepts_model / translate_model
# 必须反映 gmi.models / gmi.aliases 设置
# （漏接 model_config 时保存返回 200 但数据不变 —— 本次用户报错）
# ---------------------------------------------------------------------------


def test_set_channel_models_persists_to_gmi_view(gmi_enabled, isolated_db):
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

    # 重置：删除设置回内置默认
    view = control_plane.set_channel_models("gmi", models=None, aliases=None, set_models=True, set_aliases=True)
    assert view["models"] == list(control_plane._GMI_DEFAULT_MODELS)
    assert view["aliases"] == dict(control_plane._GMI_DEFAULT_ALIASES)


def test_gmi_whitelist_gates_request_models(gmi_enabled, isolated_db):
    """白名单是最终闸门：自定义后列表外模型必须被拒（与 qclaw 同契约，
    gateway/router.py 依据 accepts_model 拒绝请求）。"""
    from providers.gmi import chat as gchat

    control_plane = __import__("accounts.control_plane", fromlist=["control_plane"])
    control_plane.set_channel_models("gmi", models=["zai-org/GLM-5.3-Flash"], set_models=True)
    assert gchat.accepts_model("zai-org/GLM-5.3-Flash") is True
    assert gchat.accepts_model("some-unknown-model") is False
