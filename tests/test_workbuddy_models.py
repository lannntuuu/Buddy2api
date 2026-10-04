"""WorkBuddy「刷新官方模型表 / 官方模型选择弹窗」单元测试（全部 mock HTTP，不发真实请求）。

风格对齐 tests/test_traesolo.py：用模块级 ``_TRANSPORT`` + ``httpx.MockTransport`` 注入上游
（/v3/config），用例间重置模块级 ``_model_cache``。覆盖 §4 全部要求：

1. ``parse_model_details`` 纯函数（结构 / 原序 / display_name 回退 / rate 解析 / 非正 maxInputTokens 剔除）
2. ``refresh_dynamic_models``（成功 / 无账号 / 非 200 / code!=0 / 空 models / 新鲜缓存跳过 /
   失败负缓存与 force 绕过），用例间重置缓存
3. provider 面钩子（存在性 + 委托正确）
4. ``channel_model_view("workbuddy")`` 缓存命中时 ``model_details`` 带 official rate

隔离要求：每个用例都用 ``isolated_db`` fixture；不碰网络；不发真实请求。
"""

import asyncio
import time

import httpx
import pytest

from accounts import auth_manager
from storage import database as db
from accounts import control_plane
from providers.workbuddy import models
from providers.workbuddy import PROVIDER


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

FAKE_ACCOUNT = {
    "id": 1,
    "name": "wb-1",
    "provider": "workbuddy",
    "uid": "u1",
    "access_token": "jwt-fake",
    "refresh_token": "rt-fake",
    "status": "active",
    "extra": {},
}


@pytest.fixture(autouse=True)
def wb_state_reset():
    """重置 workbuddy 模块级动态模型缓存 + 取号副作用 + MockTransport。"""
    with models._model_cache.lock:
        models._model_cache.details = []
        models._model_cache.ids = []
        models._model_cache.fetched_at = 0.0
        models._model_cache.last_fail_at = 0.0
    auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    models._TRANSPORT = None
    yield
    models._TRANSPORT = None


class WbConfigMock:
    """按 /v3/config 路径分发的 MockTransport handler，自带调用计数。

    与 traesolo 测试里 ``Mock`` 同构：把响应内容在构造时给定，handler 只认
    ``/v3/config`` 这一个端点（其余 404），并在每次命中时自增 ``calls``，
    方便断言「新鲜缓存 / 负缓存期间不再发请求」。
    """

    def __init__(self, models_payload, status=200, code=0):
        self.models_payload = models_payload
        self.status = status
        self.code = code
        self.calls = 0

    def handler(self, request):
        if request.url.path == "/v3/config":
            self.calls += 1
            return httpx.Response(
                self.status,
                json={"code": self.code, "msg": "OK", "data": {"models": self.models_payload}},
            )
        return httpx.Response(404, text="unmocked " + request.url.path)

    def set_success(self, models_payload):
        self.models_payload = models_payload
        self.status = 200
        self.code = 0


async def _fake_headers(account):
    return {"Authorization": "Bearer fake"}


def _wire(mock, monkeypatch, *, account=FAKE_ACCOUNT):
    """注入 MockTransport + 假账号 + 假 chat 指纹头，彻底零网络。"""
    models._TRANSPORT = httpx.MockTransport(mock.handler)
    monkeypatch.setattr(auth_manager, "pick_account", lambda *a, **k: account)
    monkeypatch.setattr(auth_manager, "get_valid_headers", _fake_headers)


# 一组贴近实测目录的原始条目（§1 契约字段用 .get 安全取）。
RAW_NORMAL = [
    {
        "id": "hy3-x",
        "name": "Hunyuan 3 X",
        "maxInputTokens": 200000,
        "credits": "x0.51 credits",
        "supportsToolCall": True,
    },
    {
        # 生图类：maxInputTokens 非正整数 → 应被剔除
        "id": "hunyuan-image-alpha",
        "name": "Hunyuan Image Alpha",
        "maxInputTokens": None,
        "tags": ["text-to-image"],
    },
    {
        "id": "glm-5.3",
        "name": "GLM-5.3",
        "maxInputTokens": 128000,
        # 缺 credits → rate None
    },
    {
        "id": "deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "maxInputTokens": 256000,
        "credits": "N/A",  # 坏格式 → rate None
    },
    {
        # 缺 name → display_name 回退 id
        "id": "space-bunny",
        "maxInputTokens": 64000,
        "credits": "x1.2 credits",
    },
]


# ---------------------------------------------------------------------------
# 1. parse_model_details 纯函数
# ---------------------------------------------------------------------------

def test_parse_model_details_structure_and_order(isolated_db):
    out = models.parse_model_details(RAW_NORMAL)
    # 剔除 image-alpha 后剩 4 条，且保持上游原序
    assert [m["id"] for m in out] == [
        "hy3-x",
        "glm-5.3",
        "deepseek-v4.1-flash",
        "space-bunny",
    ]
    # 每项恰好 5 键，且 official=True
    for m in out:
        assert set(m.keys()) == {"id", "display_name", "rate", "context_window", "official"}
        assert m["official"] is True
    # 正常条目映射正确
    hy3 = out[0]
    assert hy3["display_name"] == "Hunyuan 3 X"
    assert hy3["rate"] == 0.51
    assert hy3["context_window"] == 200000  # = maxInputTokens


def test_parse_model_details_display_name_fallback(isolated_db):
    out = models.parse_model_details(RAW_NORMAL)
    bunny = next(m for m in out if m["id"] == "space-bunny")
    assert bunny["display_name"] == "space-bunny"  # 缺 name → 回退 id
    assert bunny["rate"] == 1.2
    assert bunny["context_window"] == 64000


def test_parse_model_details_rate_missing_and_malformed(isolated_db):
    out = models.parse_model_details(RAW_NORMAL)
    by_id = {m["id"]: m for m in out}
    # 缺 credits → None
    assert by_id["glm-5.3"]["rate"] is None
    # 坏格式 "N/A"（正则无数字命中）→ None
    assert by_id["deepseek-v4.1-flash"]["rate"] is None


def test_parse_model_details_drops_non_positive_max_input_tokens(isolated_db):
    raw = [
        {"id": "hunyuan-image-alpha", "name": "A", "maxInputTokens": None, "tags": ["text-to-image"]},
        {"id": "hunyuan-image-alpha-edit", "name": "B", "maxInputTokens": 0, "tags": ["image-to-image"]},
        {"id": "no-field", "name": "C"},  # 缺 maxInputTokens
        {"id": "keep-me", "name": "D", "maxInputTokens": 32000},
    ]
    out = models.parse_model_details(raw)
    ids = [m["id"] for m in out]
    assert "keep-me" in ids
    assert "hunyuan-image-alpha" not in ids
    assert "hunyuan-image-alpha-edit" not in ids
    assert "no-field" not in ids


# ---------------------------------------------------------------------------
# 2. refresh_dynamic_models
# ---------------------------------------------------------------------------

def test_refresh_success_populates_cache(isolated_db, monkeypatch):
    mock = WbConfigMock(RAW_NORMAL)
    _wire(mock, monkeypatch)
    assert asyncio.run(models.refresh_dynamic_models()) is True
    assert mock.calls == 1
    details = models.official_model_details()
    assert [m["id"] for m in details] == [
        "hy3-x", "glm-5.3", "deepseek-v4.1-flash", "space-bunny",
    ]
    assert details[0]["rate"] == 0.51
    assert details[0]["context_window"] == 200000
    assert all(m["official"] is True for m in details)


def test_refresh_no_account_returns_false(isolated_db, monkeypatch):
    mock = WbConfigMock(RAW_NORMAL)
    _wire(mock, monkeypatch, account=None)  # pick_account 返回 None
    assert asyncio.run(models.refresh_dynamic_models()) is False
    assert mock.calls == 0  # 无账号直接负缓存，不发请求
    assert models.official_model_details() == []


def test_refresh_non_200_returns_false(isolated_db, monkeypatch):
    mock = WbConfigMock(RAW_NORMAL, status=500, code=0)
    _wire(mock, monkeypatch)
    assert asyncio.run(models.refresh_dynamic_models()) is False
    assert mock.calls == 1
    assert models.official_model_details() == []


def test_refresh_code_not_zero_returns_false(isolated_db, monkeypatch):
    mock = WbConfigMock(RAW_NORMAL, status=200, code=1005)
    _wire(mock, monkeypatch)
    assert asyncio.run(models.refresh_dynamic_models()) is False
    assert mock.calls == 1
    assert models.official_model_details() == []


def test_refresh_empty_models_returns_false(isolated_db, monkeypatch):
    mock = WbConfigMock([], status=200, code=0)
    _wire(mock, monkeypatch)
    assert asyncio.run(models.refresh_dynamic_models()) is False
    assert mock.calls == 1
    assert models.official_model_details() == []


def test_refresh_fresh_cache_skips_request(isolated_db, monkeypatch):
    mock = WbConfigMock(RAW_NORMAL)
    _wire(mock, monkeypatch)
    assert asyncio.run(models.refresh_dynamic_models()) is True
    assert mock.calls == 1
    # 缓存新鲜（TTL 内）且 force=False → 不再发请求，直接 True
    assert asyncio.run(models.refresh_dynamic_models(force=False)) is True
    assert mock.calls == 1


def test_refresh_negative_cache_then_force_bypass(isolated_db, monkeypatch):
    # 首次失败：code!=0 写入 5min 负缓存
    mock = WbConfigMock(RAW_NORMAL, status=200, code=1)
    _wire(mock, monkeypatch)
    assert asyncio.run(models.refresh_dynamic_models()) is False
    assert mock.calls == 1
    # 负缓存 5min 内 force=False 直接 False，不再请求
    assert asyncio.run(models.refresh_dynamic_models(force=False)) is False
    assert mock.calls == 1
    # 上游已恢复，force=True 必须绕过负缓存真正重拉一次
    mock.set_success(RAW_NORMAL)
    assert asyncio.run(models.refresh_dynamic_models(force=True)) is True
    assert mock.calls == 2
    assert models.official_model_details()[0]["id"] == "hy3-x"


# ---------------------------------------------------------------------------
# 3. provider 面钩子
# ---------------------------------------------------------------------------

def test_provider_exposes_dynamic_hooks(isolated_db):
    assert hasattr(PROVIDER, "refresh_dynamic_models")
    assert hasattr(PROVIDER, "official_model_details")
    assert callable(PROVIDER.refresh_dynamic_models)
    assert callable(PROVIDER.official_model_details)


def test_provider_refresh_delegates_to_models(isolated_db, monkeypatch):
    state = {"called": False, "force": None}

    async def fake_refresh(force=False):
        state["called"] = True
        state["force"] = force
        return True

    monkeypatch.setattr(models, "refresh_dynamic_models", fake_refresh)
    assert asyncio.run(PROVIDER.refresh_dynamic_models(force=True)) is True
    assert state["called"] is True
    assert state["force"] is True


def test_provider_official_details_delegates_to_models(isolated_db, monkeypatch):
    data = [{"id": "x", "display_name": "X", "rate": None, "context_window": None, "official": True}]
    monkeypatch.setattr(models, "official_model_details", lambda: data)
    assert PROVIDER.official_model_details() == data


# ---------------------------------------------------------------------------
# 4. channel_model_view 带 official rate
# ---------------------------------------------------------------------------

def test_channel_model_view_carries_official_rate(isolated_db, monkeypatch):
    # 注入官方明细到模块级缓存（大小写一致，不做大小写不敏感假设以降低耦合）
    official = {
        "id": "glm-5.3",
        "display_name": "GLM-5.3 Official",
        "rate": 0.40,
        "context_window": 128000,
        "official": True,
    }
    with models._model_cache.lock:
        models._model_cache.details = [official]
        models._model_cache.ids = ["glm-5.3"]
        models._model_cache.fetched_at = time.time()  # TTL 内

    # 白名单含该官方 id，让 fetch_model_rates 能命中官方段
    db.set_setting("models", [{"id": "glm-5.3"}])

    view = control_plane.channel_model_view("workbuddy")
    row = next(r for r in view["model_details"] if r["id"] == "glm-5.3")
    assert row["official"] is True
    assert row["rate"] == 0.40
    assert row["display_name"] == "GLM-5.3 Official"
    assert row["context_window"] == 128000
