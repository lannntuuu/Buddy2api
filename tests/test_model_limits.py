"""模型最大输入上下文配置（存储层拆分：gateway_settings.json + DB settings）。

spec 20 / redesign-audit 19c：
- 全局段（max_input_tokens/max_output_tokens/enforce）存 gateway_settings.json；
- 通道级 <channel>.max_input_tokens / 模型级 <channel>.max_input_tokens_by_model
  存 DB settings；
- 旧 model_limits.json 在首次读取时一次性迁移并入新存储并改名留痕。

覆盖点：
- 优先级解析：模型级 > 通道级 > 全局 > 内置默认 1M
- 坏 JSON 容错（不 crash，warn 一次，走内置默认）
- null = 显式不限制（通道级 / 模型级；全局不支持 null 语义）
- enforce=false 整体跳过
- proxy 输入预检 400 / max_tokens 注入与 clamp
- qwenwork 默认输出上限来源（替代原硬编码 32000）
- hy3-preview-agent → hy3-x 保底别名
- admin API GET/PUT 往返、null 重置、非法值 400
- 旧 model_limits.json 迁移用例
"""
import json
import shutil
import uuid
from pathlib import Path

import pytest

from providers import model_limits
from storage import database as db
from storage import gateway_settings as gs
from upstream import proxy


@pytest.fixture()
def settings_file():
    """独立的 gateway_settings.json + 干净的模块缓存，避免污染仓库根真实配置。

    沿用 conftest.isolated_db 的思路：沙箱禁写系统 TEMP（tmp_path 会 WinError 5），
    改用仓库 .tmp 下唯一子目录。
    """
    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"gs-test-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "gateway_settings.json"
    model_limits.reset_migration_state()
    gs.set_settings_path(str(path))
    yield path
    gs.set_settings_path(
        str(Path(__file__).resolve().parent.parent / "gateway_settings.json")
    )
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture()
def fake_settings(monkeypatch):
    """隔离的 DB settings（避免污染真实库）；供 admin API 用例用。"""
    store: dict = {}

    def get_setting(key, default=None):
        return store.get(key, default)

    def set_setting(key, value):
        store[key] = value

    def delete_setting(key):
        store.pop(key, None)

    def setting_exists(key):
        return key in store

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "delete_setting", delete_setting)
    monkeypatch.setattr(db, "setting_exists", setting_exists)
    return store


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------- 优先级解析 ----------

def test_priority_model_over_channel_over_global(settings_file, fake_settings):
    _write(settings_file, {"max_input_tokens": 100000})
    fake_settings["workbuddy.max_input_tokens"] = 200000
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 5000}
    # 模型级 > 通道级 > 全局
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 5000
    assert model_limits.resolve_max_input_tokens("workbuddy", "glm-5.2") == 200000
    assert model_limits.resolve_max_input_tokens("qclaw", "whatever") == 100000


def test_builtin_default_1m_when_no_config(settings_file, fake_settings):
    # 文件不存在 + DB 无配置 → 全部走内置默认 1048576
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 1048576
    assert model_limits.get_default_max_input_tokens() == 1048576


def test_null_means_unlimited(settings_file, fake_settings):
    # 模型级 null = 显式不限制
    fake_settings["workbuddy.max_input_tokens_by_model"] = {
        "hy4-preview": None,
    }
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") is None
    # 通道级 null = 显式不限制
    fake_settings["workbuddy.max_input_tokens"] = None
    assert model_limits.resolve_max_input_tokens("workbuddy", "glm-5.2") is None
    # 全局不支持 null 语义：缺键回退内置默认
    _write(settings_file, {})
    assert model_limits.resolve_max_input_tokens("qclaw", "x") == 1048576


def test_corrupt_json_falls_back_to_builtin(settings_file, fake_settings):
    settings_file.write_text("{not valid json", encoding="utf-8")
    # 坏 JSON 不 crash，走内置默认
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 1048576
    # 后续修好文件可恢复（mtime 感知缓存）
    _write(settings_file, {"workbuddy.max_input_tokens_by_model": {}})
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 4096}
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 4096


def test_enforce_false_skips_precheck(settings_file, fake_settings):
    _write(settings_file, {"enforce": False})
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 10}
    monkeypatch_proxy = _monkeypatch_alias()
    with monkeypatch_proxy:
        body = proxy.build_backend_body({
            "model": "hy4-preview",
            "messages": [{"role": "user", "content": "x" * 500}],
        })
    # enforce=false → 预检 / clamp / 注入全部跳过
    assert "max_tokens" not in body


def _monkeypatch_alias():
    import unittest.mock as mock

    m = mock.patch.object(proxy, "resolve_model_alias", lambda m: m)
    return m


# ---------- proxy 链路：预检 / 注入 / clamp ----------

def test_proxy_precheck_rejects_over_limit(settings_file, fake_settings):
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 10}
    with _monkeypatch_alias():
        with pytest.raises(proxy.ModelLimitError) as excinfo:
            proxy.build_backend_body({
                "model": "hy4-preview",
                "messages": [{"role": "user", "content": "x" * 100}],
            })
    detail = excinfo.value.detail
    assert excinfo.value.status == 400
    assert detail["error"]["type"] == "invalid_request_error"
    assert "max input context" in detail["error"]["message"]


def test_proxy_injects_max_tokens_when_missing(settings_file, fake_settings):
    _write(settings_file, {
        "max_output_tokens": 32768,
    })
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 1000}
    with _monkeypatch_alias():
        body = proxy.build_backend_body({
            "model": "hy4-preview",
            "messages": [{"role": "user", "content": "x" * 30}],  # ≈10 tokens
        })
    # 未传 max_tokens → 注入 min(默认输出, max_input-估算输入)
    assert body["max_tokens"] == min(32768, 1000 - 10)


def test_proxy_respects_client_max_tokens_without_clamp(settings_file, fake_settings):
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 100000}
    with _monkeypatch_alias():
        body = proxy.build_backend_body({
            "model": "hy4-preview",
            "messages": [{"role": "user", "content": "x" * 30}],
            "max_tokens": 2048,
        })
    # 显式传值在剩余空间内 → 尊重，不 clamp
    assert body["max_tokens"] == 2048


def test_proxy_clamps_client_max_tokens_over_remaining(settings_file, fake_settings):
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 100}
    with _monkeypatch_alias():
        body = proxy.build_backend_body({
            "model": "hy4-preview",
            "messages": [{"role": "user", "content": "x" * 30}],  # ≈10 tokens
            "max_tokens": 5000,
        })
    # 显式值超剩余空间 → clamp 到剩余
    assert body["max_tokens"] == 90


def test_qwenwork_default_output_comes_from_gateway_settings(settings_file, fake_settings):
    # 原硬编码 32000 → 现读 gateway_settings（内置默认 32768）
    from providers.qwenwork import chat as qw_chat

    assert qw_chat.build_body({"model": "qwork-advanced"})[0] is not None  # 不 crash
    _write(settings_file, {"max_output_tokens": 11111})
    body, _raw, _mid = qw_chat.build_body({"model": "qwork-advanced", "messages": [
        {"role": "user", "content": "hi"}]})
    assert body["parameters"]["max_tokens"] == 11111


def test_hy3x_alias_fallback(fake_settings):
    # 过时 id 保底映射：hy3-preview-agent → hy3-x（未设置自定义别名 → 内置生效）
    assert proxy.resolve_model_alias("hy3-preview-agent") == "hy3-x"
    ids = {m["id"] for m in proxy.DEFAULT_MODELS}
    assert "hy3-x" in ids
    assert "hy3-preview-agent" not in ids


# ---------- estimate_input_tokens ----------

def test_estimate_input_tokens_heuristic(settings_file):
    # ÷3 启发式，向上取整
    assert model_limits.estimate_input_tokens(None) == 0
    assert model_limits.estimate_input_tokens([]) == 0
    msgs = [{"role": "user", "content": "x" * 30}]
    assert model_limits.estimate_input_tokens(msgs) == 10
    # CJK 从宽
    assert model_limits.estimate_input_tokens(
        [{"role": "user", "content": "中" * 9}]) == 3
    # 多模态 content 列表取 text 部分
    assert model_limits.estimate_input_tokens(
        [{"role": "user", "content": [{"type": "text", "text": "y" * 6}]}]) == 2


# ---------- gateway_settings 容器单测 ----------

def test_gateway_settings_get_set_all_unknown_keys_preserved(settings_file):
    gs.set("max_input_tokens", 123)
    assert gs.get("max_input_tokens") == 123
    # 未知键原样保留
    gs.set("future_global_key", {"x": 1})
    assert gs.get("future_global_key") == {"x": 1}
    data = gs.all()
    assert data["max_input_tokens"] == 123
    assert data["future_global_key"] == {"x": 1}
    # 改一个键不丢另一个
    gs.set("max_output_tokens", 999)
    assert gs.get("future_global_key") == {"x": 1}
    assert gs.get("max_output_tokens") == 999


def test_gateway_settings_bad_file_falls_back(settings_file):
    settings_file.write_text("{broken", encoding="utf-8")
    # 坏文件不 crash，缺键返回 default
    assert gs.get("max_input_tokens", 1048576) == 1048576
    # 修复后写入可恢复（mtime 感知缓存）
    _write(settings_file, {"max_input_tokens": 222})
    assert gs.get("max_input_tokens") == 222


def test_gateway_settings_atomic_write_no_partial(settings_file):
    # 写中途若被打断也应只有完整文件；此处仅验证写入产物为合法 JSON 且内容完整
    gs.set("enforce", False)
    gs.set("max_input_tokens", 555)
    reloaded = json.loads(settings_file.read_text(encoding="utf-8"))
    assert reloaded == {"enforce": False, "max_input_tokens": 555}


# ---------- admin API（Phase 2）----------

def test_admin_view_reports_model_limits(settings_file, fake_settings):
    from accounts import control_plane

    fake_settings["workbuddy.max_input_tokens"] = 262144
    fake_settings["workbuddy.max_input_tokens_by_model"] = {"hy4-preview": 5000}
    view = control_plane.channel_model_view("workbuddy")
    assert view["model_limits"] == {"hy4-preview": 5000}
    assert view["default_max_input_tokens"] == 262144
    assert view["model_limits_customized"] is True


def test_admin_view_unconfigured_channel(settings_file, fake_settings):
    from accounts import control_plane

    view = control_plane.channel_model_view("traework")
    assert view["model_limits"] == {}
    assert view["model_limits_customized"] is False
    assert view["default_max_input_tokens"] == 1048576  # 内置默认


def test_admin_set_roundtrip_and_null_reset(settings_file, fake_settings):
    from accounts import control_plane

    view = control_plane.set_channel_models(
        "workbuddy",
        model_limits={"hy4-preview": 5000, "future-model": None},
        default_max_input_tokens=131072,
        set_model_limits=True,
        set_default_max_input=True,
    )
    # 视图只回显显式配置（int|null），但 null 条目保留为显式不限制
    assert view["model_limits"] == {"hy4-preview": 5000, "future-model": None}
    assert view["default_max_input_tokens"] == 131072
    # 落 DB settings
    assert db.get_setting("workbuddy.max_input_tokens") == 131072
    assert db.get_setting("workbuddy.max_input_tokens_by_model") == {
        "hy4-preview": 5000, "future-model": None}
    # 存储位置断言：gateway_settings.json 若无则新建也仅会放全局键（写走 DB）；
    # 若已存在，绝无 channels / 通道配置段
    on_disk = {}
    if settings_file.exists():
        on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "channels" not in on_disk
    assert "workbuddy" not in on_disk

    # null 通道级默认 = 删除该级配置（回退内置默认）
    control_plane.set_channel_models(
        "workbuddy",
        default_max_input_tokens=None,
        set_default_max_input=True,
    )
    assert db.get_setting("workbuddy.max_input_tokens", None) is None
    view = control_plane.channel_model_view("workbuddy")
    assert view["default_max_input_tokens"] == 1048576

    # 模型级 {} = 清空该通道全部每模型配置
    control_plane.set_channel_models(
        "workbuddy",
        model_limits={},
        set_model_limits=True,
    )
    assert db.get_setting("workbuddy.max_input_tokens_by_model", None) is None
    view = control_plane.channel_model_view("workbuddy")
    assert view["model_limits"] == {}
    assert view["model_limits_customized"] is False


@pytest.mark.parametrize("bad", ["x", 0, -1, 1.5, {}, []])
def test_admin_set_rejects_invalid_values(settings_file, fake_settings, bad):
    from accounts import control_plane

    with pytest.raises(ValueError):
        control_plane.set_channel_models(
            "workbuddy", model_limits={"hy4-preview": bad},
            set_model_limits=True,
        )
    with pytest.raises(ValueError):
        control_plane.set_channel_models(
            "workbuddy", default_max_input_tokens=bad, set_default_max_input=True,
        )


# ---------- 旧 model_limits.json 迁移 ----------

def test_legacy_migration_to_db_and_gateway_settings(settings_file, fake_settings):
    from accounts import control_plane

    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"legacy-test-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        legacy = workdir / "model_limits.json"
        _write(legacy, {
            "enforce": False,
            "default_max_input_tokens": 1048576,
            "default_max_output_tokens": 32768,
            "channels": {
                "workbuddy": {
                    "default_max_input_tokens": 262144,
                    "models": {
                        "hy4-preview": {"max_input_tokens": 5000},
                        "unlimited-model": {"max_input_tokens": None},
                    },
                },
                "qwenwork": {
                    "default_max_input_tokens": None,
                    "default_max_output_tokens": 32768,
                },
            },
        })
        model_limits.set_legacy_path(str(legacy))
        # 触发迁移：任意一次读取都会执行 _maybe_migrate_legacy
        assert model_limits.get_enforce() is False
        # 1) 全局键并入 gateway_settings.json（旧字段名映射新键名）
        assert gs.get("max_input_tokens") == 1048576
        assert gs.get("max_output_tokens") == 32768
        # 存储位置断言：gateway_settings.json 只存全局键，绝无 channels 段
        on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
        assert "channels" not in on_disk
        # 2) 通道/模型级迁入 DB settings
        assert db.get_setting("workbuddy.max_input_tokens") == 262144
        assert db.get_setting("workbuddy.max_input_tokens_by_model") == {
            "hy4-preview": 5000, "unlimited-model": None}
        # 通道级显式 null（qwenwork）= 显式不限制，也应落 DB
        assert db.setting_exists("qwenwork.max_input_tokens") is True
        assert db.get_setting("qwenwork.max_input_tokens") is None
        # 3) 旧文件改名留痕（不删除）
        assert legacy.exists() is False
        assert (workdir / "model_limits.json.migrated").exists() is True
        # 视图应反映迁移结果
        view = control_plane.channel_model_view("workbuddy")
        assert view["model_limits"] == {"hy4-preview": 5000, "unlimited-model": None}
        assert view["default_max_input_tokens"] == 262144
        assert view["model_limits_customized"] is True
    finally:
        model_limits.set_legacy_path(
            str(Path(__file__).resolve().parent.parent / "model_limits.json")
        )
        model_limits.reset_migration_state()
        shutil.rmtree(workdir, ignore_errors=True)
