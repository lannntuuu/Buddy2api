"""跨通道共享的重试常量与退避 helper。

qclaw / qwenwork 使用同一套"固定 3 次 + 指数退避 + equal-jitter"策略；
workbuddy（upstream/proxy.py）有独立的旧实现；traework / traesolo
分别是有状态会话与冷却状态机，不适用统一策略，保持现状。
"""

from __future__ import annotations

import asyncio
import random

# 上游瞬时错误：可换号/重试
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 固定尝试次数（首次 + 2 次重试）
MAX_ATTEMPTS = 3

# 模块级随机源：生产共用一个 Random 实例；测试可向 retry_delay 注入
# 固定 rng（任何提供 .random() 的对象）。
_module_rng = random.Random()


async def retry_delay(
    attempt: int,
    max_attempts: int = MAX_ATTEMPTS,
    *,
    rng=None,
    retry_after=None,
) -> None:
    """第 attempt 次重试前的退避。

    无 retry_after：底数 base = min(2.0, 0.25×2^attempt)，实际 sleep =
    base × (0.5 + rng.random()) ∈ [0.5×base, 1.5×base]（equal-jitter，
    避免并发失败后同步重试）。rng 缺省用模块级 random.Random()。

    有 retry_after（秒，来自上游 Retry-After 头的纯数字值）：
    sleep = min(retry_after, 2.0)，尊重上游明确指示的节奏。

    最后一次尝试的失败后面没有重试了，直接返回不再空等。
    调用方兼容：老调用 retry_delay(attempt) / retry_delay(attempt, 3) 不变。
    """
    if attempt >= max_attempts - 1:
        return
    if retry_after is not None:
        try:
            delay = min(float(retry_after), 2.0)
        except (TypeError, ValueError):
            delay = min(2.0, 0.25 * (2 ** attempt))
        await asyncio.sleep(delay)
        return
    base = min(2.0, 0.25 * (2 ** attempt))
    source = rng if rng is not None else _module_rng
    await asyncio.sleep(base * (0.5 + source.random()))
