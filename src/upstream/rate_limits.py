"""upstream.rate_limits — WorkBuddy (账号, 模型) 级频率限制状态（纯内存）。

WorkBuddy 上游在单账号单模型用量超限时返回 HTTP 429 + body
``{"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-09-21 14:19:07
UTC+8 重置，您也可以切换其他模型继续使用。",...}``。实测语义：

  * 计数维度是 **(账号, 模型)**——同一账号换个模型仍可用（上游文案
    "您也可以切换其他模型继续使用"），换账号打同一模型也可能可用；
  * 解除时间是上游规定的**墙钟时刻**（不是"从现在起 N 秒"）；
  * 别名（如 hy3-preview-agent → hy3-x）指向同一上游模型，共享同一窗口
    ——所以记录必须用**别名解析后的模型 id** 作 key。

因此这里维护一张进程内 ``{(account_id, model_id) -> 记录}`` 表：

  * 记录值为墙钟 epoch 秒（``time.time()`` 域），直接与上游报的解除时刻比；
  * 纯内存、重启即清（产品裁决：丢了可接受，重启后最多多吃一次 429）；
  * 到期条目在读取/写入时惰性清理，无后台线程。

对外三个用途：
  1. ``is_limited``  —— 选号预判跳过（模型感知选号的输入）；
  2. ``record``      —— proxy 收到 6004 时登记（解析 msg 里的解除时刻）；
  3. ``view``        —— 管理端模型列表展示"最早恢复时间 + 各账号明细"。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

# 解除时刻与上游申报值之间的安全余量（秒）：客户端与上游时钟可能有偏差，
# 贴边请求会再吃一次 429 并把记录续期，不如主动多等半分钟。
RESET_SAFETY_MARGIN_S = 30

# 解析失败时的兜底记录时长（秒）：不假装知道解除时间，只防同账号同模型
# 被连续疯转（每个请求都真实打一次上游 429）。
FALLBACK_COOLDOWN_S = 60

# ``将在 2026-09-21 14:19:07 UTC+8 重置``（也兼容"恢复"措辞与缺省的 UTC+8 后缀）
_RESET_RE = re.compile(
    r"将在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?:\s*UTC\+8)?\s*(?:重置|恢复)"
)

# {模型 id: {账号 id: 解除 epoch 秒}}；两把锁分别保护各自的读写。
_limit_lock = threading.Lock()
_limits: dict[str, dict[int, float]] = {}

# 上游 6004 的机器码与人类可读类型（对外返回体/观测面共用）。
RATE_LIMIT_CODE = 6004
RATE_LIMIT_TYPE = "rate_limit_error"


def _now() -> float:
    """时钟注入点：测试用 fake clock 覆盖（monkeypatch rate_limits._now）。"""
    return time.time()


def _prune_locked(model: str, now: float) -> dict[int, float]:
    """按模型清理到期条目后返回剩余映射（调用方需持锁）。"""
    per_account = _limits.get(model)
    if per_account is None:
        return {}
    alive = {aid: until for aid, until in per_account.items() if until > now}
    if alive:
        _limits[model] = alive
    else:
        _limits.pop(model, None)
    return alive


def parse_reset_epoch(raw) -> Optional[float]:
    """从上游 6004 body（dict 或原始文本）解析解除时刻 → epoch 秒。

    时刻按 UTC+8 解读（与 msg 措辞一致），减去安全余量后返回；解析不出
    返回 None（调用方用 FALLBACK_COOLDOWN_S 兜底）。
    """
    if isinstance(raw, dict):
        raw = raw.get("msg") or ""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    text = str(raw or "")
    match = _RESET_RE.search(text)
    if not match:
        return None
    try:
        naive = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    tz8 = timezone(timedelta(hours=8))
    reset_epoch = naive.replace(tzinfo=tz8).timestamp()
    return max(0.0, reset_epoch - RESET_SAFETY_MARGIN_S)


def record(account_id: int, model: str, reset_epoch: Optional[float]) -> float:
    """登记一次 (账号, 模型) 限流，返回生效的解除 epoch。

    reset_epoch 为 None 或已过期时用 FALLBACK_COOLDOWN_S 兜底（防疯转）。
    """
    model = str(model or "").strip()
    if not model or not account_id:
        return 0.0
    now = _now()
    until = float(reset_epoch) if reset_epoch and reset_epoch > now else now + FALLBACK_COOLDOWN_S
    with _limit_lock:
        per_account = _prune_locked(model, now)
        prev = per_account.get(account_id)
        if prev is not None and prev > until:
            until = prev  # 已有更晚的记录（比如兜底后的真实解除时刻）不回退
        per_account[account_id] = until
        _limits[model] = per_account
    return until


def is_limited(account_id: int, model: str) -> bool:
    """该 (账号, 模型) 是否仍在限流窗口内（选号预判跳过用）。"""
    model = str(model or "").strip()
    if not model or not account_id:
        return False
    now = _now()
    with _limit_lock:
        per_account = _prune_locked(model, now)
        return account_id in per_account


def limited_until(account_id: int, model: str) -> Optional[float]:
    """该 (账号, 模型) 的解除 epoch；未受限/已到期返回 None。"""
    model = str(model or "").strip()
    if not model or not account_id:
        return None
    now = _now()
    with _limit_lock:
        per_account = _prune_locked(model, now)
        until = per_account.get(account_id)
    return until if until is not None else None


def limited_account_ids(model: str) -> set[int]:
    """该模型当前处于限流窗口的账号 id 集合（选号预判跳过的输入）。"""
    model = str(model or "").strip()
    if not model:
        return set()
    now = _now()
    with _limit_lock:
        per_account = _prune_locked(model, now)
        return set(per_account)


def _iso_utc8(epoch: Optional[float]) -> Optional[str]:
    """epoch → "YYYY-MM-DD HH:MM:SS UTC+8"（上游申报口径，展示直用）。"""
    if not epoch:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M:%S UTC+8")
    )


def model_view(model: str) -> dict:
    """单模型的限流观测面：最早恢复时间 + 各账号明细（管理端展示用）。

    返回 ``{"limited_accounts": [...], "earliest_reset": float|None,
    "all_limited": bool}``；``all_limited`` 仅表示"已记录的账号数 > 0"，
    是否构成"全账号受限"由调用方结合可用账号集判定（本模块不认识账号池）。
    """
    model = str(model or "").strip()
    if not model:
        return {"limited_accounts": [], "earliest_reset": None, "all_limited": False}
    now = _now()
    with _limit_lock:
        per_account = _prune_locked(model, now)
        entries = sorted(per_account.items(), key=lambda kv: kv[1])
    return {
        "limited_accounts": [
            {"account_id": aid, "reset_at": until, "reset_at_iso": _iso_utc8(until)}
            for aid, until in entries
        ],
        "earliest_reset": entries[0][1] if entries else None,
        "earliest_reset_iso": _iso_utc8(entries[0][1]) if entries else None,
        "all_limited": bool(entries),
    }


def snapshot() -> dict[str, dict[int, float]]:
    """全量快照（测试与调试观测面用；无锁拷贝，调用方只读）。"""
    now = _now()
    with _limit_lock:
        return {
            model: dict(_prune_locked(model, now)) for model in list(_limits)
        }


def clear(model: str | None = None, account_id: int | None = None) -> None:
    """清空记录：全表 / 指定模型 / 指定 (模型, 账号)。测试与运维兜底用。"""
    with _limit_lock:
        if model is None:
            _limits.clear()
            return
        model = str(model or "").strip()
        if account_id is None:
            _limits.pop(model, None)
        else:
            per_account = _limits.get(model)
            if per_account is not None:
                per_account.pop(account_id, None)
                if not per_account:
                    _limits.pop(model, None)
