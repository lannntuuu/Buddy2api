"""模型最大上下文配置（存储层拆分：spec 20 / redesign-audit 19c）。

- **全局段**（max_input_tokens / max_output_tokens / enforce）统一存通用容器
  `gateway_settings.json`（src/storage/gateway_settings.py），不再有通道/模型级
  覆盖（上一版也未实现通/模级 override，语义等价）。
- **通道级 / 模型级** 配置走数据库 settings 表，键形如：
    <channel>.max_input_tokens              -> 正 int 或无（未设置=内置默认）
    <channel>.max_input_tokens_by_model     -> {"<model_id>": <int|null>}
  null 条目 = 该模型显式不限制；{} = 自定义空。与 `<channel>.reasoning` 同模式。
- 旧仓库根 `model_limits.json` 在首次读取时一次性迁移（§2.4）：全局键并入
  gateway_settings.json、通道/模型级迁入 DB settings，旧文件改名为
  `model_limits.json.migrated` 留痕（不删除）。

本模块只负责查询 / 写回（写回经 control_plane._set_model_limits 落到 DB），
并提供单次请求的输入估算函数。对外契约函数签名与语义保持不变。

优先级（每模型最大输入上下文）：
  <channel>.max_input_tokens_by_model[id]
  → <channel>.max_input_tokens
  → 全局 gateway_settings "max_input_tokens"
  → 内置默认 1048576
null 值 = 显式不限制（调用方跳过预检 / 不 clamp）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from storage import gateway_settings as gs

logger = logging.getLogger("buddy2api.model_limits")

# ---- 内置默认 ----
DEFAULT_MAX_INPUT_TOKENS = 1048576  # 默认至少支持 1M 上下文
DEFAULT_MAX_OUTPUT_TOKENS = 32768  # qwenwork 等默认输出上限（替代原硬编码 32000）

# 旧文件 model_limits.json 路径（与 config.toml 同级）：src/providers → 上溯两级
_LEGACY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "model_limits.json",
)

_lock = threading.RLock()
_migrated = False  # 一个进程内只迁移一次


def _legacy_path() -> str:
    return _LEGACY_PATH


# ---- 测试钩子（不进生产路径，仅供单测控制迁移）----
def set_legacy_path(path: str) -> None:
    """覆盖旧 model_limits.json 路径（迁移用例用）。"""
    global _LEGACY_PATH, _migrated
    _LEGACY_PATH = path
    _migrated = False


def reset_migration_state() -> None:
    """重置迁移哨兵，使下次读取重新触发迁移（测试隔离用）。"""
    global _migrated
    _migrated = False


def _channel_max_input_key(channel: str) -> str:
    return f"{channel}.max_input_tokens"


def _channel_by_model_key(channel: str) -> str:
    return f"{channel}.max_input_tokens_by_model"


def _maybe_migrate_legacy() -> None:
    """首次读取时把旧 model_limits.json 一次性迁入新存储（§2.4）。

    仅执行一次（进程内）；旧文件改名为 .migrated 留痕。任何异常都不影响正常运行。
    """
    global _migrated
    # 快路径：已迁移过则直接返回，避免每个请求都取锁（迁移后哨兵恒为 True）。
    if _migrated:
        return
    with _lock:
        if _migrated:
            return
        _migrated = True
    path = _LEGACY_PATH
    try:
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("旧 model_limits.json 读取失败，跳过迁移：%s", exc)
            return
        if not isinstance(data, dict):
            logger.warning("旧 model_limits.json 根不是对象，跳过迁移")
            return

        migrated_any = False
        # 1) 全局键并入 gateway_settings.json
        global_map = {
            "enforce": "enforce",
            "default_max_input_tokens": "max_input_tokens",
            "default_max_output_tokens": "max_output_tokens",
        }
        cur = dict(gs.all())
        for old_key, new_key in global_map.items():
            if old_key in data and new_key not in cur:
                cur[new_key] = data[old_key]
                migrated_any = True
        if migrated_any:
            # 一次性原子写回（合并多个全局键，避免逐键写多次）
            gs.write_all(cur)

        # 2) 通道/模型级迁入 DB settings
        from storage import database as db

        channels = data.get("channels")
        if isinstance(channels, dict):
            for ch, chan in channels.items():
                if not isinstance(chan, dict):
                    continue
                d = chan.get("default_max_input_tokens")
                if d is not None:
                    try:
                        db.set_setting(_channel_max_input_key(ch), int(d))
                        migrated_any = True
                    except (TypeError, ValueError):
                        pass
                elif "default_max_input_tokens" in chan:
                    # 显式 null 通道级默认 = 显式不限制，照搬原语义也落 DB（键存在 != 值 null）
                    try:
                        db.set_setting(_channel_max_input_key(ch), None)
                        migrated_any = True
                    except Exception:
                        pass
                models = chan.get("models")
                by_model: dict[str, Any] = {}
                if isinstance(models, dict):
                    for mid, entry in models.items():
                        if not isinstance(entry, dict):
                            continue
                        if "max_input_tokens" in entry:
                            v = entry["max_input_tokens"]
                            by_model[str(mid)] = None if v is None else int(v)
                            migrated_any = True
                if by_model:
                    db.set_setting(_channel_by_model_key(ch), by_model)

        # 3) 改名留痕（不删除）
        if migrated_any:
            try:
                os.replace(path, f"{path}.migrated")
                logger.info("旧 model_limits.json 已迁移并改名 %s.migrated 留痕", path)
            except OSError as exc:
                logger.warning("旧 model_limits.json 改名留痕失败（已迁移）：%s", exc)
    except Exception as exc:  # 兜底：迁移失败绝不阻断启动
        logger.warning("model_limits 迁移异常，跳过：%s", exc)


def get_enforce() -> bool:
    """顶层强制开关；默认 true。false 时预检 / clamp / 注入全部跳过。"""
    _maybe_migrate_legacy()
    return bool(gs.get("enforce", gs.DEFAULT_ENFORCE))


def get_default_max_input_tokens() -> int:
    """全局默认最大输入上下文（内置默认 1048576）。"""
    _maybe_migrate_legacy()
    g = gs.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
    if g is None:
        return DEFAULT_MAX_INPUT_TOKENS
    try:
        return int(g)
    except (TypeError, ValueError):
        return DEFAULT_MAX_INPUT_TOKENS


def get_default_max_output_tokens(channel: str | None = None) -> int:
    """默认最大输出 token；仅全局配置（不支持通道级覆盖）。

    优先级：全局 max_output_tokens（内置默认 32768）。
    """
    _maybe_migrate_legacy()
    # 旧字段名 default_max_output_tokens 兼容（迁移后已在 gateway_settings）
    g = gs.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if g is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        return int(g)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT_TOKENS


def resolve_max_input_tokens(channel: str, model: str) -> int | None:
    """解析某通道某模型生效的最大输入上下文。

    优先级：<channel>.max_input_tokens_by_model[id]
            → <channel>.max_input_tokens
            → 全局 max_input_tokens（内置默认 1048576）
    null 值 = 显式不限制，返回 None（调用方跳过预检 / 不 clamp）。通道/模型级
    未设置 = 用内置默认（与「显式 null = 不限制」严格区分）。
    """
    _maybe_migrate_legacy()
    from storage import database as db

    # 1) 模型级
    by_model = db.get_setting(_channel_by_model_key(channel), None)
    if isinstance(by_model, dict) and model in by_model:
        v = by_model[model]
        if v is None:
            return None  # 显式不限制
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    # 2) 通道级（键存在与否语义：设置了哪怕 null = 显式不限制；未设置 = 回退全局）
    if db.setting_exists(_channel_max_input_key(channel)):
        chan_val = db.get_setting(_channel_max_input_key(channel), None)
        if chan_val is None:
            return None  # 显式不限制
        try:
            return int(chan_val)
        except (TypeError, ValueError):
            pass
    # 3) 全局
    g = gs.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
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


def get_channel_limits(channel: str) -> dict:
    """返回某通道生效限额摘要（admin 端点用，Phase 2 接入）。

    - default_max_input_tokens：该通道生效默认值（含优先级解析结果）
    - model_limits：仅显式配置的每模型上限（<id>: <int|null>）
    - model_limits_customized：DB settings 里是否有该通道配置（通道级或任一模型级）
    """
    _maybe_migrate_legacy()
    from storage import database as db

    chan_customized = db.setting_exists(_channel_max_input_key(channel))
    chan_val = db.get_setting(_channel_max_input_key(channel), None)
    by_model = db.get_setting(_channel_by_model_key(channel), None)
    explicit: dict[str, int | None] = {}
    if isinstance(by_model, dict):
        for mid, v in by_model.items():
            explicit[str(mid)] = None if v is None else int(v)
    customized = chan_customized or (isinstance(by_model, dict) and bool(by_model))
    return {
        "default_max_input_tokens": resolve_max_input_tokens(channel, "__sentinel__"),
        "model_limits": explicit,
        "model_limits_customized": bool(customized),
    }
