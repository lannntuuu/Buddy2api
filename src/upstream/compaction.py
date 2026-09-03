"""upstream.compaction — request-body compaction to dodge upstream 11128.

Pulled out of proxy.py. The compaction state, helpers, and decision
function live here so the policy is editable in isolation.

Behaviour:
- Only the ZCode Client participates in compaction. Other clients (DSH,
  curl, python, empty) are never compacted, even if the same channel
  has been armed, so unrelated agents don't lose information.
- "Armed" mode kicks in after the first 11128 on a (channel, client)
  pair; `CB_GATEWAY_COMPACT_CHARS>0` also forces compaction on.
- Compaction is *per-field* head truncation, not a body-wide budget.
  The 11128 trigger is content-shape sensitive, so total-byte clamping
  was tried and rejected (it would shred system prompts to 137 chars
  and still fail the budget).
"""
from __future__ import annotations

import os
import threading
from typing import Optional

# --- Per-(channel, client) state ---
_COMPACTION_LOCK = threading.Lock()
_ARMED_KEYS: set[tuple] = set()
_COMPACTION_STATS = {"armed_triggers": 0, "compacted_messages": 0, "retried_11128": 0}
_COMPACT_11128_MARKERS = ("11128", "Illegal API invocation")
_COMPACT_ENABLED_CLIENTS = ("zcode",)


def _compaction_key(channel: Optional[str], client_tag) -> tuple:
    return (channel or "", client_tag or "")


def _client_allows_compact(client_tag) -> bool:
    """Only ZCode Client is eligible for compaction. Everything else is left alone."""
    return client_tag in _COMPACT_ENABLED_CLIENTS


def _channel_armed(channel: Optional[str], client_tag) -> bool:
    if not _client_allows_compact(client_tag):
        return False
    with _COMPACTION_LOCK:
        return _compaction_key(channel, client_tag) in _ARMED_KEYS


def _arm_channel(channel: Optional[str], client_tag) -> None:
    if not _client_allows_compact(client_tag):
        return
    with _COMPACTION_LOCK:
        _ARMED_KEYS.add(_compaction_key(channel, client_tag))
        _COMPACTION_STATS["armed_triggers"] += 1


def compaction_stats() -> dict:
    """Surface counters to /admin/stats: triggers, compactions, 11128 retries."""
    return {
        "compacted_messages": _COMPACTION_STATS["compacted_messages"],
        "armed_keys": len(_ARMED_KEYS),
        "armed_triggers": _COMPACTION_STATS["armed_triggers"],
        "retried_11128": _COMPACTION_STATS["retried_11128"],
        "enabled_clients": list(_COMPACT_ENABLED_CLIENTS),
    }


def _record_11128_retry() -> None:
    with _COMPACTION_LOCK:
        _COMPACTION_STATS["retried_11128"] += 1


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- 11128 detection ---

def _is_11128_error(status: int, payload, body: dict) -> bool:
    """True iff this upstream response is the 11128 oversized-request block.
    `payload` may be raw bytes, a dict, or a string.
    """
    if status != 400:
        return False
    text = ""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", "replace")
    elif isinstance(payload, dict):
        text = str(payload)
    elif isinstance(payload, str):
        text = payload
    if not any(marker in text for marker in _COMPACT_11128_MARKERS):
        return False
    # If we've already deep-compacted and still 11128'd, give up to avoid busy-looping.
    if body.get("_compacted_11128"):
        return False
    return True


# --- Per-field compaction helpers ---

def _compact_text(text: str, cap: int):
    """Truncate a single text to `cap` chars, keeping head + tail.

    Tool results often contain the error / summary at the tail, so we
    keep the last ~20% of the budget (minimum 8 chars).
    """
    n = len(text)
    if n <= cap:
        return text, False
    tail_budget = max(8, cap // 5)
    head = cap - tail_budget
    out = text[:head] + f"\n...[省略 {n-head-tail_budget} 字符]..." + (text[-tail_budget:] if tail_budget > 0 else "")
    return out, True


def _compact_schema_descriptions(node, cap):
    """Recursively trim JSON Schema `description` strings. Structural keys
    (name, type, property names, required, enum values) are left alone.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "description" and isinstance(v, str):
                compacted, c = _compact_text(v, cap)
                out[k] = compacted if c else v
            else:
                out[k] = _compact_schema_descriptions(v, cap)
        return out
    if isinstance(node, list):
        return [_compact_schema_descriptions(item, cap) for item in node]
    return node


def _compact_tools(tools, description_cap: int):
    """Trim large text fields in `tools` definitions to shrink the request
    body (a common 11128 trigger).

    Only description-style strings are touched; structural keys
    (name, type, property names, required, enum values) stay intact so
    the tool-call contract is preserved. Returns (new_tools, changed).
    """
    if not isinstance(tools, list):
        return tools, False
    new_tools = []
    changed = False
    for tool in tools:
        new_tool = tool
        if isinstance(tool, dict):
            new_tool = dict(tool)
            fn = new_tool.get("function")
            if isinstance(fn, dict):
                new_fn = dict(fn)
                desc = new_fn.get("description")
                if isinstance(desc, str):
                    compacted, dc = _compact_text(desc, description_cap)
                    if dc:
                        new_fn["description"] = compacted
                        changed = True
                params = new_fn.get("parameters")
                if isinstance(params, dict):
                    new_fn["parameters"] = _compact_schema_descriptions(params, description_cap)
                new_tool["function"] = new_fn
        new_tools.append(new_tool)
    return (new_tools, changed)


def _smart_compact_messages(body: dict, *, channel: Optional[str] = None,
                            client_tag=None) -> bool:
    """Compacting pass on the request body. Returns True iff the body was modified.

    See module docstring for the rationale (per-field head truncation,
    not a body-wide clamp). Eligibility: ZCode client + (armed OR
    `CB_GATEWAY_COMPACT_CHARS>0`).
    """
    if not _client_allows_compact(client_tag):
        return False
    forced_cap = _env_int("CB_GATEWAY_COMPACT_CHARS", 0)
    if not _channel_armed(channel, client_tag) and forced_cap <= 0:
        return False
    base_cap = forced_cap if forced_cap > 0 else _env_int("CB_GATEWAY_COMPACT_ARMED_CHARS", 3000)
    if base_cap <= 0:
        return False
    # system prompt gets a more generous cap: 4000-5000 reliably defuses
    # 11128, 6000 still trips. Head-only because the 11128 trigger is
    # usually the trailing git/commit block.
    system_cap = _env_int("CB_GATEWAY_COMPACT_SYSTEM_CHARS", 5000)
    messages = body.get("messages")
    any_changed = False
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system":
                content = m.get("content")
                if isinstance(content, str) and len(content) > system_cap:
                    m["content"] = content[:system_cap]
                    any_changed = True
                continue
            content = m.get("content")
            if isinstance(content, str) and len(content) > base_cap:
                # 11128 is content-shape sensitive, so head-truncate only.
                m["content"] = content[:base_cap]
                any_changed = True
            # reasoning_content is a long thinking trace; a 11128 hotspot.
            rc = m.get("reasoning_content")
            if isinstance(rc, str) and len(rc) > base_cap:
                m["reasoning_content"] = rc[:base_cap]
                any_changed = True
    if isinstance(body.get("tools"), list) and body["tools"]:
        new_tools, tools_changed = _compact_tools(body["tools"], base_cap)
        if tools_changed:
            any_changed = True
            body["tools"] = new_tools
    if any_changed:
        with _COMPACTION_LOCK:
            _COMPACTION_STATS["compacted_messages"] += 1
    return any_changed
