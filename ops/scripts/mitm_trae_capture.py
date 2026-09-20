"""mitmproxy addon: 抓 TRAE agent 网关 chat_sessions 请求（确认 work/code 模式字段）。

运行:  mitmdump -s mitm_trae_capture.py        (或 mitmweb -s mitm_trae_capture.py)
输出:  脚本同目录 trae_capture.jsonl（追加写入）+ 控制台摘要。
只关注 trae-api-cn.mchost.guru 的:
  - POST /api/remote/v1/chat_sessions          建会话  → 记 mode
  - POST /api/remote/v1/chat_sessions/{sid}/messages  发消息  → 记 agent_id/agent_type/model_name/query
  - 发消息响应(SSE) → 只记 event: 行集合（对比 work/code 事件结构）
凭据类头/字段一律脱敏。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

HOST = "mchost.guru"
CREATE = "/api/remote/v1/chat_sessions"
OUT = Path(__file__).with_name("trae_capture.jsonl")

REDACT_HEADERS = {"authorization", "cloud-ide-jwt", "cookie", "set-cookie"}
REDACT_KEYS = ("token", "jwt", "password", "secret", "authorization", "cookie")


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: "***" if any(s in k.lower() for s in REDACT_KEYS) else _redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _json_body(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw_prefix": text[:500]}


def _hdrs(headers, skip: set[str]) -> dict:
    out = {}
    for k, v in headers.items():
        ks = k.decode() if isinstance(k, bytes) else str(k)
        vs = v.decode() if isinstance(v, bytes) else str(v)
        if ks.lower() not in skip:
            out[ks] = vs
    return out


class TraeCapture:
    def request(self, flow):
        req = flow.request
        if HOST not in req.pretty_host or CREATE not in req.path:
            return
        kind = "create" if req.path.endswith(CREATE) else "send"
        if kind == "send" and not req.path.endswith("/messages"):
            return
        record = {
            "ts": int(time.time()),
            "kind": kind,
            "method": req.method,
            "path": req.path,
            "headers": _hdrs(req.headers, REDACT_HEADERS),
            "body": _redact(_json_body(req.get_text())),
        }
        _write(record)
        mode = (record["body"] or {}).get("mode", "?")
        agent = (record["body"] or {}).get("agent_id", "?")
        print(f"[trae] {kind} mode={mode} agent_id={agent}")

    def response(self, flow):
        req, resp = flow.request, flow.response
        if HOST not in req.pretty_host or resp is None:
            return
        if req.path.endswith(CREATE):
            _write({"ts": int(time.time()), "kind": "create_resp", "path": req.path,
                    "body": _redact(_json_body(resp.get_text()))})
        elif req.path.endswith("/messages"):
            text = (resp.get_text() or "")[:2_000_000]  # ponytail: 截断, 事件类型在头部即出现
            events = sorted({line[6:].strip() for line in text.splitlines()
                             if line.startswith("event:")})
            _write({"ts": int(time.time()), "kind": "send_events", "path": req.path, "events": events})
            print(f"[trae] send events: {events}")


def _write(record: dict) -> None:
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    assert _redact({"mode": "code", "access_token": "x"}) == {"mode": "code", "access_token": "***"}
    assert _redact([{"jwt": "y"}]) == [{"jwt": "***"}]
    print("self-check ok")
