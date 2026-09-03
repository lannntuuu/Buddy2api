"""进程级共享 httpx 连接池（keep-alive 复用，降低每次上游调用的 TCP+TLS 建连成本）。

- 惰性创建；按"当前事件循环"绑定：httpx 连接池状态属于首次使用的 loop，
  生产服务是单 loop（复用生效），测试里每个 asyncio.run 是新 loop（自动重建，避免跨 loop 复用）。
- 默认沿用 httpx 的环境代理语义（trust_env 默认 True），行为与原本每次新建 client 一致，
  不引入额外的代理/网络行为变化。
- 需要时显式调用 aclose_http_client() 释放连接。
"""
from __future__ import annotations

import asyncio
import threading

import httpx

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def get_client() -> httpx.AsyncClient:
    global _client, _client_loop
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # 非 async 上下文调用：沿用现有 client
    if _client is None or _client.is_closed or (loop is not None and _client_loop is not loop):
        with _lock:
            if _client is None or _client.is_closed or (loop is not None and _client_loop is not loop):
                _client = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=32,
                        max_keepalive_connections=12,
                        keepalive_expiry=60.0,
                    ),
                    # 每次请求仍可按需传入 timeout= 覆盖该默认上限
                    timeout=httpx.Timeout(60.0),
                )
                _client_loop = loop
    return _client


async def aclose_http_client() -> None:
    global _client, _client_loop
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        finally:
            _client = None
            _client_loop = None
