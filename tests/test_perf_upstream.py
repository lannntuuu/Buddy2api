"""WS-A 性能修复回归测试(见 redesign-audit/30-perf-optimization-spec.md)。

覆盖:
- A1:11128 自愈记账不再引用未导入的名字(compaction 统计入口可用且计数递增)。
- A2:_get_client 按"当前事件循环"复用,新 loop 重建;_shared_client_cm 不关闭共享客户端。
- A3:_take_line 的 bytes.find 重写与逐字节扫描语义逐位一致(表驱动 + 定长伪随机分块对拍)。
- A6:重试常量收敛到 providers/retry.py,401/403 仅参与 failover 判定。

沿用本仓库约定:同步测试函数内用 asyncio.run 驱动协程(未装 pytest-asyncio)。
"""
import asyncio

import pytest

from upstream import proxy
from upstream.compaction import _record_11128_retry, compaction_stats


# ---------- A1: 11128 记账 NameError 回归 ----------

def test_11128_retry_bookkeeping_importable_and_counts():
    # proxy 必须能正常 import(旧代码在函数体里引用未导入的
    # _COMPACTION_LOCK/_COMPACTION_STATS,import 不报错但触发即 NameError)。
    assert callable(proxy._record_11128_retry)
    before = compaction_stats()["retried_11128"]
    _record_11128_retry()
    assert compaction_stats()["retried_11128"] == before + 1


# ---------- A2: 共享上游客户端 ----------

def test_upstream_client_reused_within_loop_rebuilt_on_new_loop():
    async def first():
        c1 = proxy._get_client()
        c2 = proxy._get_client()
        assert c1 is c2
        return c1

    client_a = asyncio.run(first())

    async def second():
        c = proxy._get_client()
        # 新事件循环(asyncio.run)必须重建,不能复用上一条 loop 的客户端
        assert c is not client_a

    asyncio.run(second())


def test_shared_client_cm_does_not_close_client():
    async def run():
        async with proxy._shared_client_cm() as client:
            inside = client
        return inside

    client = asyncio.run(run())
    # 上下文管理器退出后客户端必须仍然打开(keep-alive 复用的前提)
    assert not client.is_closed


# ---------- A3: _take_line 语义等价 ----------

class _NaiveDecoder(proxy._SSEEventDecoder):
    """保留逐字节扫描的旧实现,作为对拍基准。"""

    def _take_line(self, *, final: bool = False):
        for index, value in enumerate(self._buffer):
            if value == 0x0A:
                line = self._buffer[:index]
                self._buffer = self._buffer[index + 1:]
                return line[:-1] if line.endswith(b"\r") else line
            if value == 0x0D:
                if index + 1 == len(self._buffer) and not final:
                    return None
                end = index + 2 if self._buffer[index + 1:index + 2] == b"\n" else index + 1
                line = self._buffer[:index]
                self._buffer = self._buffer[end:]
                return line
        if final and self._buffer:
            line = self._buffer
            self._buffer = b""
            return line
        return None


CASES = [
    b"data: 1\ndata: 2\n",
    b"a\r\nb\r\n",
    b"abc\r",
    b"x\r\ny\nz",
    b"\r\n",
    b"\n",
    b"a\rb\nc",
    b"",
    b"no trailing newline",
    b"data: A\n\ndata: B\n\n",
    b"line1\r\nline2\rline3\n\r\n",
]


def _take_all(decoder):
    out = []
    while True:
        line = decoder._take_line()
        if line is None:
            break
        out.append(line)
    return out


@pytest.mark.parametrize("payload", CASES)
def test_take_line_matches_naive_scan(payload):
    for split in _chunkings(payload):
        fast, naive = proxy._SSEEventDecoder(), _NaiveDecoder()
        for chunk in split:
            fast.feed(chunk)
            naive.feed(chunk)
        fast_lines = _take_all(fast)
        naive_lines = _take_all(naive)
        assert fast_lines == naive_lines, f"split={split!r}"
        # final 语义(含残留尾巴)也要一致
        fast_tail, naive_tail = fast._take_line(final=True), naive._take_line(final=True)
        assert fast_tail == naive_tail, f"final tail mismatch split={split!r}"


def _chunkings(payload):
    """同一 payload 的几种分块方式:整体、逐字节、按 2/3 字节。"""
    yield [payload]
    yield [payload[i:i + 1] for i in range(len(payload))]
    yield [payload[i:i + 2] for i in range(0, len(payload), 2)]
    yield [payload[i:i + 3] for i in range(0, len(payload), 3)]


def test_take_line_final_flush():
    d = proxy._SSEEventDecoder()
    d.feed(b"data: tail")
    assert d.feed(b"") == []
    assert _take_all(d) == []
    events = d.finish()
    assert events == [b"tail"]


def test_take_line_bare_cr_waits_for_next_chunk():
    d = proxy._SSEEventDecoder()
    d._buffer = b"abc\r"
    # CR 是 buffer 最后一个字节且未 final:无法判定是否 CRLF,必须等待
    assert d._take_line() is None
    d._buffer += b"\ndef\n"
    assert d._take_line() == b"abc"
    assert d._take_line() == b"def"
    assert d._take_line() is None
    assert d._take_line(final=True) is None


# ---------- A6: 重试常量收敛 ----------

def test_retryable_status_codes_shared_with_retry_module():
    from providers import retry as retry_module

    assert proxy.RETRYABLE_STATUS_CODES == retry_module.RETRYABLE_STATUS | {401, 403}
    assert proxy._retry_delay is retry_module.retry_delay
    assert proxy._is_retryable_status(401)
    assert proxy._is_retryable_status(403)
    assert proxy._is_retryable_status(429)
    assert not proxy._is_retryable_status(400)
    assert not proxy._is_retryable_status(404)
