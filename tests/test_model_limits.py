"""模型最大输入上下文配置（model_limits.json）+ 请求链路强制（spec 19）。

覆盖点：
- 优先级解析：模型级 > 通道级 > 全局 > 内置默认 1M
- 坏 JSON 容错（不 crash，warn 一次，走内置默认）
- null = 显式不限制（预检 / clamp 跳过）
- enforce=false 整体跳过
- proxy 输入预检 400 / max_tokens 注入与 clamp
- qwenwork 默认输出上限来源（替代原硬编码 32000）
- hy3-preview-agent → hy3-x 保底别名
- admin API GET/PUT 往返、null 重置、非法值 400
"""
import json
import shutil
import uuid
from pathlib import Path

import pytest

from providers import model_limits
from storage import database as db
from upstream import proxy


@pytest.fixture()
def limits_file():
    """独立的 model_limits.json + 干净的模块缓存，避免污染仓库根真实配置。

    沿用 conftest.isolated_db 的思路：沙箱禁写系统 TEMP（tmp_path 会 WinError 5），
    改用仓库 .tmp 下唯一子目录。
    """
    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"model-limits-test-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / "model_limits.json"
    model_limits.set_limits_path(str(path))
    yield path
    model_limits.set_limits_path(
        str(Path(__file__).resolve().parent.parent / "model_limits.json")
    )
    shutil.rmtree(workdir, ignore_errors=True)


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------- 优先级解析 ----------

def test_priority_model_over_channel_over_global(limits_file):
    _write(limits_file, {
        "default_max_input_tokens": 100000,
        "channels": {
            "workbuddy": {
                "default_max_input_tokens": 200000,
                "models": {"hy4-preview": {"max_input_tokens": 5000}},
            },
        },
    })
    # 模型级 > 通道级 > 全局
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 5000
    assert model_limits.resolve_max_input_tokens("workbuddy", "glm-5.2") == 200000
    assert model_limits.resolve_max_input_tokens("qclaw", "whatever") == 100000


def test_builtin_default_1m_when_no_file(limits_file):
    # 文件不存在 → 全部走内置默认 1048576
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 1048576
    assert model_limits.get_default_max_input_tokens() == 1048576


def test_null_means_unlimited(limits_file):
    _write(limits_file, {
        "default_max_input_tokens": None,
        "channels": {
            "workbuddy": {
                "default_max_input_tokens": None,
                "models": {"hy4-preview": {"max_input_tokens": None}},
            },
        },
    })
    # null = 显式不限制（模型级、通道级、全局三级均验证）
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") is None
    _write(limits_file, {"channels": {"workbuddy": {"default_max_input_tokens": None}}})
    assert model_limits.resolve_max_input_tokens("workbuddy", "glm-5.2") is None
    _write(limits_file, {"default_max_input_tokens": None})
    # 通道未配置 → 全局 null 也应表示"不限制"（但当前实现会回退内置默认）
    assert model_limits.resolve_max_input_tokens("qclaw", "x") is None


def test_corrupt_json_falls_back_to_builtin(limits_file):
    limits_file.write_text("{not valid json", encoding="utf-8")
    # 坏 JSON 不 crash，走内置默认
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 1048576
    # 后续修好文件可恢复（mtime 感知缓存）
    _write(limits_file, {"channels": {"workbuddy": {"models": {
        "hy4-preview": {"max_input_tokens": 4096}}}}})
    assert model_limits.resolve_max_input_tokens("workbuddy", "hy4-preview") == 4096


def test_enforce_false_skips_precheck(limits_file, monkeypatch):
    _write(limits_file, {"enforce": False, "channels": {"workbuddy": {"models": {
        "hy4-preview": {"max_input_tokens": 10}}}}})
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda m: m)
    body = proxy.build_backend_body({
        "model": "hy4-preview",
        "messages": [{"role": "user", "content": "x" * 500}],
    })
    # enforce=false → 预检 / clamp / 注入全部跳过
    assert "max_tokens" not in body


# ---------- proxy 链路：预检 / 注入 / clamp ----------

def test_proxy_precheck_rejects_over_limit(limits_file, monkeypatch):
    _write(limits_file, {"channels": {"workbuddy": {"models": {
        "hy4-preview": {"max_input_tokens": 10}}}}})
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda m: m)
    with pytest.raises(proxy.ModelLimitError) as excinfo:
        proxy.build_backend_body({
            "model": "hy4-preview",
            "messages": [{"role": "user", "content": "x" * 100}],
        })
    detail = excinfo.value.detail
    assert excinfo.value.status == 400
    assert detail["error"]["type"] == "invalid_request_error"
    assert "max input context" in detail["error"]["message"]


def test_proxy_injects_max_tokens_when_missing(limits_file, monkeypatch):
    _write(limits_file, {
        "default_max_output_tokens": 32768,
        "channels": {"workbuddy": {"models": {
            "hy4-preview": {"max_input_tokens": 1000}}}},
    })
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda m: m)
    body = proxy.build_backend_body({
        "model": "hy4-preview",
        "messages": [{"role": "user", "content": "x" * 30}],  # ≈10 tokens
    })
    # 未传 max_tokens → 注入 min(默认输出, max_input-估算输入)
    assert body["max_tokens"] == min(32768, 1000 - 10)


def test_proxy_respects_client_max_tokens_without_clamp(limits_file, monkeypatch):
    _write(limits_file, {"channels": {"workbuddy": {"models": {
        "hy4-preview": {"max_input_tokens": 100000}}}}})
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda m: m)
    body = proxy.build_backend_body({
        "model": "hy4-preview",
        "messages": [{"role": "user", "content": "x" * 30}],
        "max_tokens": 2048,
    })
    # 显式传值在剩余空间内 → 尊重，不 clamp
    assert body["max_tokens"] == 2048


def test_proxy_clamps_client_max_tokens_over_remaining(limits_file, monkeypatch):
    _write(limits_file, {"channels": {"workbuddy": {"models": {
        "hy4-preview": {"max_input_tokens": 100}}}}})
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda m: m)
    body = proxy.build_backend_body({
        "model": "hy4-preview",
        "messages": [{"role": "user", "content": "x" * 30}],  # ≈10 tokens
        "max_tokens": 5000,
    })
    # 显式值超剩余空间 → clamp 到剩余
    assert body["max_tokens"] == 90


def test_qwenwork_default_output_comes_from_model_limits(limits_file):
    # 原硬编码 32000 → 现读 model_limits（内置默认 32768）
    from providers.qwenwork import chat as qw_chat

    assert qw_chat.build_body({"model": "qwork-advanced"})[0] is not None  # 不 crash
    _write(limits_file, {"channels": {"qwenwork": {"default_max_output_tokens": 11111}}})
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

def test_estimate_input_tokens_heuristic(limits_file):
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


# ---------- admin API（Phase 2）----------

@pytest.fixture()
def fake_settings(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(db, "set_setting", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(db, "delete_setting", lambda key: store.pop(key, None))
    return store


def test_admin_view_reports_model_limits(limits_file, fake_settings):
    from accounts import control_plane

    _write(limits_file, {"channels": {"workbuddy": {
        "default_max_input_tokens": 262144,
        "models": {"hy4-preview": {"max_input_tokens": 5000}}}}})
    view = control_plane.channel_model_view("workbuddy")
    assert view["model_limits"] == {"hy4-preview": 5000}
    assert view["default_max_input_tokens"] == 262144
    assert view["model_limits_customized"] is True


def test_admin_view_unconfigured_channel(limits_file, fake_settings):
    from accounts import control_plane

    view = control_plane.channel_model_view("traework")
    assert view["model_limits"] == {}
    assert view["model_limits_customized"] is False
    assert view["default_max_input_tokens"] == 1048576  # 内置默认


def test_admin_set_roundtrip_and_null_reset(limits_file, fake_settings):
    from accounts import control_plane

    view = control_plane.set_channel_models(
        "workbuddy",
        model_limits={"hy4-preview": 5000, "future-model": None},
        default_max_input_tokens=131072,
        set_model_limits=True,
        set_default_max_input=True,
    )
    # 视图只回显显式配置（int|null），但 null 条目已删、不留在视图里
    assert view["model_limits"] == {"hy4-preview": 5000}
    assert view["default_max_input_tokens"] == 131072
    on_disk = json.loads(limits_file.read_text(encoding="utf-8"))
    assert on_disk["channels"]["workbuddy"]["models"]["hy4-preview"]["max_input_tokens"] == 5000
    assert on_disk["channels"]["workbuddy"]["default_max_input_tokens"] == 131072
    # null 条目 = 删除语义，不应落盘
    assert "future-model" not in on_disk["channels"]["workbuddy"]["models"]

    # null = 删除该级配置
    control_plane.set_channel_models(
        "workbuddy",
        model_limits={"hy4-preview": None},
        default_max_input_tokens=None,
        set_model_limits=True,
        set_default_max_input=True,
    )
    on_disk = json.loads(limits_file.read_text(encoding="utf-8"))
    # 通道节点全空后应收敛删除（不留空壳），customized 归 false
    assert on_disk.get("channels", {}).get("workbuddy") is None
    view = control_plane.channel_model_view("workbuddy")
    assert view["model_limits"] == {}
    assert view["model_limits_customized"] is False
    assert view["default_max_input_tokens"] == 1048576


@pytest.mark.parametrize("bad", ["x", 0, -1, 1.5, {}, []])
def test_admin_set_rejects_invalid_values(limits_file, fake_settings, bad):
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
