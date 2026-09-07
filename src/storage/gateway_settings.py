"""通用全局配置容器（gateway_settings.json，仓库根，与 config.toml 同级）。

职责：存放**不分平台、不分模型**的全局可配置项（首批：max_input_tokens /
max_output_tokens / enforce）。结构是任意扁平对象——**未知键原样保留**（读不
剥离、写不覆盖其他键），未来任何全局配置都可直接落此文件，无需新建存储。

设计要点（沿用 model_limits 的成熟做法）：
- mtime+size 缓存：文件改动即重读，未改动直接返回缓存（size 防止同 mtime tick
  内的连续写漏判）。
- 线程安全：读 / 写都在锁内。
- 文件缺失 / JSON 非法：warn 一次 + 走内置默认（调用方各自 fallback），不 crash。
- 原子写：先写 .tmp 再 os.replace，避免半截文件。
- 坏文件容错只 warn 一次；成功读到合法文件后解除告警锁，下次再坏会重新告警。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger("buddy2api.gateway_settings")

# 仓库根 gateway_settings.json 路径（与 config.toml 同级）：src/storage → 上溯两级
_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gateway_settings.json",
)

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None
_cache_size: int | None = None  # 缓存签名含 size：同一 mtime tick 内连续写也能感知
_warned_bad_file = False

# 内置默认（文件缺该键时由调用方使用）
DEFAULT_MAX_INPUT_TOKENS = 1048576  # 默认至少支持 1M 上下文
DEFAULT_MAX_OUTPUT_TOKENS = 32768  # qwenwork 等默认输出上限（替代原硬编码 32000）
DEFAULT_ENFORCE = True


def set_settings_path(path: str) -> None:
    """测试 / 管理端覆盖文件路径；调用后下次读取强制重读。"""
    global _SETTINGS_PATH, _cache, _cache_mtime, _cache_size, _warned_bad_file
    with _lock:
        _SETTINGS_PATH = path
        _cache = None
        _cache_mtime = None
        _cache_size = None
        _warned_bad_file = False


def _load_raw() -> dict[str, Any]:
    """读 gateway_settings.json（mtime+size 缓存）；缺失 / 非法返回 {} 并 warn 一次。"""
    global _cache, _cache_mtime, _cache_size, _warned_bad_file
    path = _SETTINGS_PATH
    try:
        try:
            stat = os.stat(path)
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            mtime = None
            size = None
        with _lock:
            if (
                _cache is not None
                and _cache_mtime is not None
                and _cache_mtime == mtime
                and _cache_size == size
            ):
                return _cache
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            with _lock:
                _cache = {}
                _cache_mtime = mtime
                _cache_size = size
            return {}
        except (json.JSONDecodeError, ValueError) as exc:
            with _lock:
                if not _warned_bad_file:
                    logger.warning("gateway_settings.json 损坏，走内置默认：%s", exc)
                    _warned_bad_file = True
                _cache = {}
                _cache_mtime = mtime
                _cache_size = size
            return {}
        if not isinstance(data, dict):
            with _lock:
                if not _warned_bad_file:
                    logger.warning("gateway_settings.json 根不是对象，走内置默认")
                    _warned_bad_file = True
                _cache = {}
                _cache_mtime = mtime
                _cache_size = size
            return {}
        with _lock:
            _cache = data
            _cache_mtime = mtime
            _cache_size = size
            # 成功读到合法文件：解除“坏文件”告警锁，下次再坏会重新告警
            _warned_bad_file = False
        return data
    except Exception as exc:  # 兜底：任何异常都不 crash
        logger.warning("读取 gateway_settings.json 失败，走内置默认：%s", exc)
        return {}


def get(key: str, default: Any = None) -> Any:
    """读某个全局键；缺键返回 default（不写回文件）。

    注意：与 DB settings 的「存在即显式值」语义不同，此容器对未知键统一返回
    default，调用方需用各内置默认常量兜底（不支持「显式 null = 不限制」语义）。
    """
    return _load_raw().get(key, default)


def set(key: str, value: Any) -> None:
    """写某个全局键；原子写回，未知键原样保留。

    value 必须可被 json 序列化；非法类型由 json.dump 抛出（调用方保证）。
    """
    data = dict(_load_raw())
    data[key] = value
    _write_atomic(data)


def delete(key: str) -> None:
    """删除某个全局键；不存在则静默无操作。"""
    data = dict(_load_raw())
    if key not in data:
        return
    data.pop(key, None)
    _write_atomic(data)


def all() -> dict[str, Any]:
    """返回当前全部全局配置（深拷贝，调用方改返回值不影响缓存）。"""
    data = _load_raw()
    return dict(data)


def _write_atomic(data: dict[str, Any]) -> None:
    """原子写回 gateway_settings.json；写后清缓存，下次读取重读。"""
    path = _SETTINGS_PATH
    tmp = f"{path}.tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _cache = None
        _cache_mtime = None


def write_all(data: dict[str, Any]) -> None:
    """整体原子写回（迁移等场景一次性合并多键用；未知键原样保留）。"""
    _write_atomic(dict(data))
