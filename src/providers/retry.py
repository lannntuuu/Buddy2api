"""跨通道共享的重试常量与退避 helper。

qclaw / qwenwork 使用同一套"固定 3 次 + 指数退避"策略；
workbuddy（upstream/proxy.py）有独立的旧实现；traework / traesolo
分别是有状态会话与冷却状态机，不适用统一策略，保持现状。
"""

from __future__ import annotations

import asyncio

# 上游瞬时错误：可换号/重试
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 固定尝试次数（首次 + 2 次重试）
MAX_ATTEMPTS = 3


async def retry_delay(attempt: int, max_attempts: int = MAX_ATTEMPTS) -> None:
    """第 attempt 次重试前的退避：0.25s → 0.5s → 1s，封顶 2s。

    最后一次尝试的失败后面没有重试了，直接返回不再空等。
    """
    if attempt >= max_attempts - 1:
        return
    await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))
