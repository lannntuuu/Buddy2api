"""WorkBuddy 官方模型表（/v3/config）动态拉取 + 缓存。

与 traesolo/chat.py 的模型表部分同构（缓存 + 锁 + TTL + 负缓存），但只实现
workbuddy 所需：上游 /v3/config 官方目录（chat 原生指纹身份，即
auth_manager.get_valid_headers），解析为弹窗条目
（id / display_name / rate / context_window / official=True），按上游顺序保留，
剔除 maxInputTokens 非正整数的条目（实测命中的生图类 hunyuan-image-*）。

详见实施规格 §1/§2.1。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

import httpx

from accounts import auth_manager

logger = logging.getLogger(__name__)

DYNAMIC_MODELS_TTL = 3600      # 成功缓存 1h
MODELS_FAIL_COOLDOWN = 300     # 失败负缓存 5min
CONFIG_PATH = "/v3/config"

# 测试注入点：模块级 MockTransport（tests/test_workbuddy_models.py 设置）。
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

_CREDITS_RE = re.compile(r"x([0-9]*\.?[0-9]+)")


class _ModelCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ids: list[str] = []
        self.details: list[dict] = []
        self.fetched_at: float = 0.0
        self.last_fail_at: float = 0.0


_model_cache = _ModelCache()


def parse_model_details(raw_models: list) -> list[dict]:
    r"""纯函数：按 §1 过滤 + 映射，返回弹窗条目列表（official=True，保持上游顺序）。

    - maxInputTokens 非正整数（缺省/None/0/负数/非数值）的条目丢弃；
    - credits 用正则 x([0-9]*\.?[0-9]+) 取 float，缺失/解析失败 → None；
    - context_window = maxInputTokens（int）；
    - display_name 取 name，缺省回退 id；
    - 输出 5 键：id / display_name / rate / context_window / official。
    """
    out: list[dict] = []
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        mit = m.get("maxInputTokens")
        if isinstance(mit, str):
            try:
                mit = int(mit)
            except ValueError:
                mit = None
        if not isinstance(mit, int) or isinstance(mit, bool) or mit <= 0:
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or any(o["id"] == mid for o in out):
            continue
        rate = None
        credits = m.get("credits")
        if isinstance(credits, str):
            mt = _CREDITS_RE.search(credits)
            if mt:
                try:
                    rate = float(mt.group(1))
                except ValueError:
                    rate = None
        out.append({
            "id": mid,
            "display_name": m.get("name") or mid,
            "rate": rate,
            "context_window": int(mit),
            "official": True,
        })
    return out


def _log_models_refresh_failure(
    force: bool, message: str, *args, exc_info: bool = False
) -> None:
    """模型表刷新失败留痕。

    前台（管理页「刷新官方模型表」，force=True）记 warning——用户在等结果；
    后台 kick（force=False）记 debug——否则上游故障时会周期性刷屏。
    """
    logger.log(logging.WARNING if force else logging.DEBUG, message, *args, exc_info=exc_info)


def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_TRANSPORT)


async def _aclose_client(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception:
        pass


async def refresh_dynamic_models(force: bool = False) -> bool:
    """动态拉模型（任一可用 workbuddy 账号），成功缓存 1h / 失败负缓存 5min。best-effort。

    force=True（管理页「刷新官方模型表」）忽略成功缓存与失败负缓存，真正重试一次；
    force=False（请求前 kick）两者都保留，避免上游故障时每个请求都重放。

    取号走 auth_manager.pick_account（provider="workbuddy"）：账号被标记 expired
    时仍可能拿不到表——与 traesolo 早期实现一致（规格指定此路径，逻辑未改）。
    """
    now = time.time()
    with _model_cache.lock:
        if not force:
            if _model_cache.ids and now - _model_cache.fetched_at < DYNAMIC_MODELS_TTL:
                return True
            if _model_cache.last_fail_at and now - _model_cache.last_fail_at < MODELS_FAIL_COOLDOWN:
                return False
    account = auth_manager.pick_account(provider="workbuddy")
    if account is None:
        with _model_cache.lock:
            _model_cache.last_fail_at = now
        _log_models_refresh_failure(force, "workbuddy 刷新模型表失败：无可用账号")
        return False
    try:
        headers = await auth_manager.get_valid_headers(account)
        if not headers:
            with _model_cache.lock:
                _model_cache.last_fail_at = now
            _log_models_refresh_failure(
                force, "workbuddy 刷新模型表失败（account=%s）：无法获取 chat 指纹 headers",
                account.get("id"),
            )
            return False
        timeout = float(auth_manager.request_timeout(25))
        client = _make_client(timeout)
        try:
            response = await client.get(
                f"{auth_manager.backend_url()}{CONFIG_PATH}", headers=headers
            )
        finally:
            await _aclose_client(client)
        if response.status_code != 200:
            with _model_cache.lock:
                _model_cache.last_fail_at = now
            _log_models_refresh_failure(
                force, "workbuddy 刷新模型表失败（account=%s）：HTTP %s",
                account.get("id"), response.status_code,
            )
            return False
        try:
            payload = response.json()
        except ValueError:
            with _model_cache.lock:
                _model_cache.last_fail_at = now
            _log_models_refresh_failure(
                force, "workbuddy 刷新模型表失败（account=%s）：非 JSON 响应",
                account.get("id"),
            )
            return False
        if payload.get("code") != 0:
            with _model_cache.lock:
                _model_cache.last_fail_at = now
            _log_models_refresh_failure(
                force, "workbuddy 刷新模型表失败（account=%s）：code=%s",
                account.get("id"), payload.get("code"),
            )
            return False
        raw_models = (payload.get("data") or {}).get("models") or []
        details = parse_model_details(raw_models)
        if not details:
            with _model_cache.lock:
                _model_cache.last_fail_at = now
            _log_models_refresh_failure(
                force, "workbuddy 刷新模型表失败：官方返回空模型列表（account=%s）",
                account.get("id"),
            )
            return False
        with _model_cache.lock:
            _model_cache.details = details
            _model_cache.ids = [d["id"] for d in details]
            _model_cache.fetched_at = now
            _model_cache.last_fail_at = 0.0
        return True
    except Exception as exc:
        with _model_cache.lock:
            _model_cache.last_fail_at = now
        _log_models_refresh_failure(
            force, "workbuddy 刷新模型表失败（account=%s）：%s",
            account.get("id"), exc, exc_info=True,
        )
        return False


def official_model_details() -> list[dict]:
    """返回缓存的官方模型明细（含 rate 等）。TTL 外返回空。

    每项含 id / display_name / rate / context_window / official=True。
    control_plane 通过 getattr(provider, "official_model_details", None) 探测调用。
    """
    with _model_cache.lock:
        if _model_cache.details and time.time() - _model_cache.fetched_at < DYNAMIC_MODELS_TTL:
            return list(_model_cache.details)
    return []
