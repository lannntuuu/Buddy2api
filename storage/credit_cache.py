"""credit-summary 结果级快照缓存（进程内、TTL + stale-while-revalidate）。

放在 storage 下是因为 accounts/auth_manager 与 accounts/control_plane 都需要调用它，
而 storage 不依赖这两者，可避免循环导入。
"""
from __future__ import annotations

import time

_SNAPSHOT: dict | None = None
_SNAPSHOT_TS: float = 0.0
_REFRESHING: bool = False
_GENERATION: int = 0


def get_snapshot(ttl: float) -> dict | None:
    """返回带 `cache`/`snapshot_age` 标记的快照；无缓存或 TTL 由调用方判断。

    - 命中（age <= ttl）：``cache="hit"``
    - 过期（age > ttl）：``cache="stale"`` 且 ``stale=True``，调用方据 `stale` 决定是否后台刷新
    """
    if _SNAPSHOT is None:
        return None
    age = time.time() - _SNAPSHOT_TS
    out = dict(_SNAPSHOT)
    out["snapshot_age"] = int(age)
    if age <= ttl:
        out["cache"] = "hit"
    else:
        out["cache"] = "stale"
        out["stale"] = True
    return out


def set_snapshot(snap: dict) -> None:
    global _SNAPSHOT, _SNAPSHOT_TS
    _SNAPSHOT = dict(snap)
    _SNAPSHOT_TS = time.time()


def has_snapshot() -> bool:
    return _SNAPSHOT is not None


def invalidate() -> None:
    """签到/领取/强制刷新成功后使快照失效，下次请求会重建（含后台 SWR 重建）。"""
    global _SNAPSHOT, _SNAPSHOT_TS, _GENERATION
    _SNAPSHOT = None
    _SNAPSHOT_TS = 0.0
    _GENERATION += 1


def generation() -> int:
    """失效代数：重建期间发生 invalidate 则代数变化，重建结果不得回填（防竞态覆盖）。"""
    return _GENERATION


def mark_refreshing(value: bool) -> None:
    global _REFRESHING
    _REFRESHING = value


def is_refreshing() -> bool:
    return _REFRESHING
