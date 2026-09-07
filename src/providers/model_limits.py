"""模型最大上下文配置（model_limits.json，仓库根）。

与 config.toml 同级；**不走数据库**（用户明确偏好：模型限额一律不进 DB）。
本模块只负责加载 / 查询 / 写回该 JSON 文件，并提供单次请求的输入估算函数。

设计要点：
- mtime 感知缓存：文件改动即重读，未改动则直接返回缓存。
- 线程安全：读 / 写都在锁内（管理端在线程池跑写回）。
- 文件缺失 / JSON 非法：warn 一次 + 全部走内置默认，**不 crash**。
- 输入估算函数独立可替换（见 estimate_input_tokens），未来可换 tokenizer。

优先级（每模型最大输入上下文）：
  channels[ch].models[id].max_input_tokens
  → channels[ch].default_max_input_tokens
  → 全局 default_max_input_tokens（内置默认 1048576）
null 值 = 显式不限制（调用方跳过预检 / 不 clamp）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger("buddy2api.model_limits")

# ---- 内置默认 ----
DEFAULT_MAX_INPUT_TOKENS = 1048576  # 默认至少支持 1M 上下文
DEFAULT_MAX_OUTPUT_TOKENS = 32768  # qwenwork 等默认输出上限（替代原硬编码 32000）

# 仓库根 model_limits.json 路径（与 config.toml 同级）：src/providers → 上溯两级
_LIMITS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "model_limits.json",
)

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None
_cache_size: int | None = None  # 缓存签名含 size：同一 mtime tick 内连续写也能感知
_warned_bad_file = False


def set_limits_path(path: str) -> None:
    """测试 / 管理端覆盖文件路径；调用后下次读取强制重读。"""
    global _LIMITS_PATH, _cache, _cache_mtime, _cache_size, _warned_bad_file
    with _lock:
        _LIMITS_PATH = path
        _cache = None
        _cache_mtime = None
        _cache_size = None
        _warned_bad_file = False


def _load_raw() -> dict[str, Any]:
    """读 model_limits.json（mtime+size 缓存）；缺失 / 非法返回 {} 并 warn 一次。"""
    global _cache, _cache_mtime, _cache_size, _warned_bad_file
    path = _LIMITS_PATH
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
                    logger.warning("model_limits.json 损坏，全部走内置默认：%s", exc)
                    _warned_bad_file = True
                _cache = {}
                _cache_mtime = mtime
                _cache_size = size
            return {}
        if not isinstance(data, dict):
            with _lock:
                if not _warned_bad_file:
                    logger.warning("model_limits.json 根不是对象，全部走内置默认")
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
        logger.warning("读取 model_limits.json 失败，走内置默认：%s", exc)
        return {}


def get_enforce() -> bool:
    """顶层强制开关；默认 true。false 时预检 / clamp / 注入全部跳过。"""
    return bool(_load_raw().get("enforce", True))


def get_default_max_input_tokens() -> int:
    """全局默认最大输入上下文（内置默认 1048576）。"""
    g = _load_raw().get("default_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
    if g is None:
        return DEFAULT_MAX_INPUT_TOKENS
    try:
        return int(g)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INPUT_TOKENS


def get_default_max_output_tokens(channel: str | None = None) -> int:
    """默认最大输出 token；支持通道级覆盖（如 qwenwork 语义特殊）。

    优先级：channels[ch].default_max_output_tokens
            → 全局 default_max_output_tokens（内置默认 32768）。
    """
    data = _load_raw()
    if channel:
        channels = data.get("channels")
        if isinstance(channels, dict):
            c = channels.get(channel)
            if isinstance(c, dict) and "default_max_output_tokens" in c:
                v = c["default_max_output_tokens"]
                if isinstance(v, int) and v > 0:
                    return v
    g = data.get("default_max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if g is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        return int(g)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT_TOKENS


def resolve_max_input_tokens(channel: str, model: str) -> int | None:
    """解析某通道某模型生效的最大输入上下文。

    优先级：channels[ch].models[id].max_input_tokens
            → channels[ch].default_max_input_tokens
            → 全局 default_max_input_tokens（内置默认 1048576）
    null 值 = 显式不限制，返回 None（调用方跳过预检 / 不 clamp）。
    """
    data = _load_raw()
    channels = data.get("channels")
    if not isinstance(channels, dict):
        channels = {}
    chan = channels.get(channel)
    if isinstance(chan, dict):
        models = chan.get("models")
        if isinstance(models, dict):
            entry = models.get(model)
            if isinstance(entry, dict) and "max_input_tokens" in entry:
                v = entry["max_input_tokens"]
                if v is None:
                    return None  # 显式不限制
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        if "default_max_input_tokens" in chan:
            d = chan["default_max_input_tokens"]
            if d is None:
                return None  # 显式不限制
            try:
                return int(d)
            except (TypeError, ValueError):
                pass
    g = data.get("default_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
    if g is None:
        return None  # 显式不限制
    try:
        return int(g)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INPUT_TOKENS


def estimate_input_tokens(messages: Any) -> int:
    """轻量启发式：全部 message 文本字符数 ÷ 3（CJK 从宽估计）。

    独立可替换：未来可换成 tiktoken / 上游 tokenizer，不影响调用方。
    从宽向上取整（÷3 启发式，CJK 单字 ≈ 多 token）。
    """
    chars = 0
    if not isinstance(messages, list):
        return 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chars += len(text)
                elif isinstance(part, str):
                    chars += len(part)
    return (chars + 2) // 3


def write_limits(data: dict) -> None:
    """原子写回 model_limits.json（管理端在线程池调用，不进 DB）。

    先写 .tmp 再 os.replace，避免半截文件；写后清缓存，下次读取重读。
    """
    path = _LIMITS_PATH
    tmp = f"{path}.tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _cache = None
        _cache_mtime = None


def get_channel_limits(channel: str) -> dict:
    """返回某通道生效限额摘要（admin 端点用，Phase 2 接入）。

    - default_max_input_tokens：该通道生效默认值（含优先级解析结果）
    - model_limits：仅显式配置的每模型上限（<id>: <int|null>）
    - model_limits_customized：JSON 文件里是否有该通道配置
    """
    data = _load_raw()
    channels = data.get("channels")
    if not isinstance(channels, dict):
        channels = {}
    chan = channels.get(channel)
    if not isinstance(chan, dict):
        return {
            "default_max_input_tokens": get_default_max_input_tokens(),
            "model_limits": {},
            "model_limits_customized": False,
        }
    models = chan.get("models")
    explicit = {}
    if isinstance(models, dict):
        for mid, entry in models.items():
            if isinstance(entry, dict) and "max_input_tokens" in entry:
                explicit[mid] = entry["max_input_tokens"]
    return {
        "default_max_input_tokens": resolve_max_input_tokens(channel, "__sentinel__"),
        "model_limits": explicit,
        "model_limits_customized": True,
    }
