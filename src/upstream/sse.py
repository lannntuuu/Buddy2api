"""upstream.sse — 全仓库唯一的 SSE(Server-Sent Events)解析器。

此前同构的行解析逻辑存在三份:proxy._SSEEventDecoder、responses._iter_chat_sse_data
的闭包版、_collect_stream 的手写 aiter_lines 循环,且已各自漂移。本模块把它们收敛为
一个实现:proxy 与 responses 直接消费 _SSEEventDecoder(feed/finish 出完整 data 事件)。
"""

from __future__ import annotations

_MAX_EVENT_BYTES = 8 * 1024 * 1024


class SSEDecoder:
    """Decode complete SSE data fields from arbitrary bytes/str chunks.

    - feed() 接受 bytes / bytearray / str(str 按 UTF-8 编码,兼容 traework 等
      适配层拼好的 SSE 行);每次返回本次累积出的完整 data 事件(bytes 列表)。
    - 事件跨多个 chunk 时先在 _data_lines 里攒,遇到空行(或 finish())才成事件。
    - 单行/单事件超过 8 MiB 置 parser_error 并清空状态,后续 feed 返回空列表;
      调用方负责把 parser_error 转成自己的错误通道。
    """

    def __init__(self):
        self.parser_error: str | None = None
        self._buffer = b""
        self._data_lines: list[bytes] = []
        self._event_bytes = 0

    def feed(self, chunk) -> list[bytes]:
        if self.parser_error:
            return []
        # 不同 provider 的流可能吐 bytes(workbuddy 原始上游)或 str
        # (traework 等适配层拼好的 SSE 行),统一转成 bytes 再解析。
        if isinstance(chunk, str):
            self._buffer += chunk.encode("utf-8")
        elif isinstance(chunk, (bytes, bytearray)):
            self._buffer += bytes(chunk)
        elif chunk:
            raise TypeError("upstream stream chunks must be bytes or str")
        events: list[bytes] = []
        while True:
            line = self._take_line()
            if line is None:
                break
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
            if self.parser_error:
                break
        if not self.parser_error and len(self._buffer) > _MAX_EVENT_BYTES:
            # drain 后 buffer 里只剩一条未完结的行,超限即单行超限
            self._fail("The upstream SSE line exceeded the 8 MiB limit.")
        return events

    def finish(self) -> list[bytes]:
        if self.parser_error:
            return []
        events: list[bytes] = []
        while True:
            line = self._take_line(final=True)
            if line is None:
                break
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
            if self.parser_error:
                return events
        if self._data_lines:
            events.append(b"\n".join(self._data_lines))
            self._data_lines = []
            self._event_bytes = 0
        return events

    def _take_line(self, *, final: bool = False) -> bytes | None:
        buffer = self._buffer
        i_cr = buffer.find(b"\r")
        i_lf = buffer.find(b"\n")
        if i_cr != -1 and (i_lf == -1 or i_cr < i_lf):
            # CR 先出现(可能是 CRLF 前缀,也可能是裸 CR)。
            if i_cr + 1 == len(buffer) and not final:
                return None  # 等下一个 chunk 才能判定是否 CRLF
            end = i_cr + 2 if buffer[i_cr + 1:i_cr + 2] == b"\n" else i_cr + 1
            self._buffer = buffer[end:]
            return buffer[:i_cr]
        if i_lf != -1:
            self._buffer = buffer[i_lf + 1:]
            line = buffer[:i_lf]
            return line[:-1] if line.endswith(b"\r") else line
        if final and buffer:
            self._buffer = b""
            return buffer
        return None

    def _consume_line(self, line: bytes) -> bytes | None:
        if len(line) > _MAX_EVENT_BYTES:
            self._fail("The upstream SSE line exceeded the 8 MiB limit.")
            return None
        if not line:
            if not self._data_lines:
                return None
            event = b"\n".join(self._data_lines)
            self._data_lines = []
            self._event_bytes = 0
            return event
        if not line.startswith(b"data:"):
            return None
        data = line[5:]
        if data.startswith(b" "):
            data = data[1:]
        self._event_bytes += len(data) + 1
        if self._event_bytes > _MAX_EVENT_BYTES:
            self._fail("The upstream SSE event exceeded the 8 MiB limit.")
            return None
        self._data_lines.append(data)
        return None

    def _fail(self, message: str) -> None:
        self.parser_error = message
        self._buffer = b""
        self._data_lines = []
        self._event_bytes = 0
