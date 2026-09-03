"""upstream.moderation — content moderation and tool-stall detection.

Split out of proxy.py. The full pipeline still calls into these helpers
from proxy.py, but the constants and rule sets live here so they can be
edited, reviewed, and tested in isolation.

This module owns:
- `_looks_like_audit_block(text)` — heuristic for short "已拦截" replies
  from the upstream content moderator.
- `_request_has_tool_loop(body)` / `_looks_like_stall_text(text)` /
  `_is_tool_stall(...)` — agent tool-loop stall detection (issue #31).
- `_body_size_profile(body)` / `_dump_11128_body(body, channel, model)` —
  diagnostics for 11128 (oversize-request) errors.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

# Tencent content-moderation short refusals. HTTP 200 with this kind of
# text means the request was intercepted. Only match short replies so
# legitimate long answers that quote the wording are not misclassified.
_AUDIT_PHRASE_GROUPS = (
    ("系统检测到", "敏感内容", "无法响应"),
    ("无法响应您的请求", "请检查后重新输入"),
    ("内容违规", "请检查后重新输入"),
    ("违规内容", "不能提供相关"),
)
_AUDIT_PREFIXES = (
    "系统检测到",
    "无法响应您的请求",
    "内容违规",
    "违规内容",
    "抱歉，系统检测到",
    "抱歉，无法响应",
)


def _looks_like_audit_block(text: str) -> bool:
    text = " ".join((text or "").split())
    if not text or len(text) > 240:
        return False
    if not text.startswith(_AUDIT_PREFIXES):
        return False
    return any(all(phrase in text for phrase in group) for group in _AUDIT_PHRASE_GROUPS)


# Tool-stall detection: in an agent tool loop the model can answer with
# "OK, I'll keep going" text and finish_reason=stop without calling any
# tool — this dead-ends the workflow (issue #31).
TOOL_STALL_RETRY = (
    os.environ.get("CB_GATEWAY_TOOL_STALL_RETRY", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
TOOL_STALL_FAIL_STREAM = (
    os.environ.get("CB_GATEWAY_TOOL_STALL_FAIL_STREAM", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)

_STALL_POSITIVE_MARKERS = (
    "马上继续", "继续跑", "接下来需要", "请问您接下来",
    "这就去", "马上开始", "我现在就", "这就开始", "稍等",
)
_STALL_NEGATIVE_MARKERS = (
    "总结", "已完成", "结果如下", "以下是", "以上就是", "完成情况",
)


def _request_has_tool_loop(body: dict) -> bool:
    """True if this is an agent tool-loop turn: tools declared + at least one tool result in history."""
    if not isinstance(body.get("tools"), list) or not body["tools"]:
        return False
    return any(
        isinstance(msg, dict) and msg.get("role") == "tool"
        for msg in (body.get("messages") or [])
    )


def _looks_like_stall_text(text: str) -> bool:
    """Empty text is always a stall. Otherwise require short text that sounds
    like an acknowledgement, not a summary."""
    text = (text or "").strip()
    if not text:
        return True
    if len(text) > 160:
        return False
    if any(marker in text for marker in _STALL_NEGATIVE_MARKERS):
        return False
    return any(marker in text for marker in _STALL_POSITIVE_MARKERS)


def _is_tool_stall(body: dict, finish_reason, tool_calls: bool, text: str) -> bool:
    """True iff this upstream completion is a stalled tool-loop turn."""
    if not _request_has_tool_loop(body):
        return False
    if tool_calls:
        return False
    if (finish_reason or "stop") not in {"stop", None}:
        return False
    return _looks_like_stall_text(text)


# --- 11128 (oversize-request) diagnostics ---

def _body_size_profile(body: dict) -> dict:
    """Diagnostic: per-message / per-field sizes so we can tell 11128 apart
    from a global overage (used by tests; harmless to keep in prod)."""
    import json as _json
    messages = body.get("messages")
    if not isinstance(messages, list):
        return {"messages": 0, "tool_msgs": 0, "max_content": 0, "max_content_role": None,
                "msg_bytes": 0, "assistant_args_bytes": 0, "tool_content_bytes": 0,
                "tools_len": 0, "body_bytes": 0}
    max_content = 0
    max_content_role = None
    max_field_desc = None
    tool_msgs = 0
    msg_bytes = 0
    assistant_args_bytes = 0
    tool_content_bytes = 0

    def _scan_biggest(value, prefix: str):
        """Recursively find the longest string field under `value` and
        update the module-level max_content* state."""
        nonlocal max_content, max_content_role, max_field_desc
        if isinstance(value, dict):
            for k, v in value.items():
                _scan_biggest(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                _scan_biggest(item, f"{prefix}[{i}]")
        elif isinstance(value, str):
            n = len(value)
            if n > max_content:
                max_content = n
                max_content_role = max_field_desc
                max_field_desc = prefix

    try:
        bt = _json.dumps(body, ensure_ascii=False).encode("utf-8")
    except Exception:
        bt = b""
    for m in messages:
        if not isinstance(m, dict):
            continue
        try:
            msg_bytes += len(_json.dumps(m, ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass
        if m.get("role") == "tool":
            tool_msgs += 1
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments")
                    if isinstance(args, str):
                        assistant_args_bytes += len(args.encode("utf-8"))
        if m.get("role") == "tool":
            c = m.get("content")
            if isinstance(c, str):
                tool_content_bytes += len(c.encode("utf-8"))
        _scan_biggest(m, "")
    return {
        "messages": len(messages),
        "tool_msgs": tool_msgs,
        "max_content": max_content,
        "max_content_role": max_content_role,
        "msg_bytes": msg_bytes,
        "assistant_args_bytes": assistant_args_bytes,
        "tool_content_bytes": tool_content_bytes,
        "tools_len": len(_json.dumps(body.get("tools"), ensure_ascii=False)),
        "body_bytes": len(bt),
    }


def _dump_11128_body(body: dict, channel: str, model: str) -> str:
    """After a compaction that still 11128'd, dump the outgoing body to a
    file for offline inspection. Returns the path; empty string on failure.

    The file contains the full request body and may include sensitive
    content — delete it once you're done debugging.
    """
    import json as _json
    try:
        d = Path(__file__).parent / ".debug"
        d.mkdir(exist_ok=True)
        ts = int(time.time() * 1000)
        target = d / f"11128_{channel}_{ts}.json"
        target.write_text(
            _json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(target)
    except Exception:
        return ""
