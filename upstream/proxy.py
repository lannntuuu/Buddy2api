"""
proxy.py — 请求代理转发

功能：
  - 转发到 copilot.tencent.com/v2/chat/completions
  - 流式 SSE 原样转发
  - 非流式 SSE 聚合为单个 JSON
  - tool_calls 分片合并
  - usage 统计
  - 账号故障自动切换
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger("buddy2api.proxy")


def _body_size_profile(body: dict) -> dict:
    """诊断：请求体的构成特征（触发 11128 时定位用）。

    会遍历每条消息，记录单条最大字段（含 content、tool_calls 等）与各类字段总字节，
    用来分辨 11128 是"单条超深"还是"整体超宽"。
    """
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
        """递归找出 value 里最长的字符串字段并更新 max_content*。"""
        nonlocal max_content, max_content_role, max_field_desc
        if isinstance(value, dict):
            for k, v in value.items():
                child = f"{prefix}.{k}"
                if isinstance(v, str):
                    if len(v) > max_content:
                        max_content = len(v)
                        max_content_role = prefix
                        max_field_desc = child
                else:
                    _scan_biggest(v, child)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                _scan_biggest(item, f"{prefix}[{i}]")

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        msg_raw = _json.dumps(m, ensure_ascii=False)
        msg_bytes += len(msg_raw)
        content = m.get("content")
        field_prefix = f"m[{role}]"
        _scan_biggest(m, field_prefix)
        if role == "tool":
            tool_msgs += 1
            if isinstance(content, str):
                tool_content_bytes += len(content)
            elif isinstance(content, list):
                tool_content_bytes += len(_json.dumps(content, ensure_ascii=False))
        # assistant 的工具调用参数
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    assistant_args_bytes += len(fn["arguments"])
    bt = _json.dumps(body, ensure_ascii=False)
    return {
        "messages": len(messages),
        "tool_msgs": tool_msgs,
        "max_content": max_content,
        "max_content_role": max_content_role,
        "max_field": max_field_desc,
        "msg_bytes": msg_bytes,
        "assistant_args_bytes": assistant_args_bytes,
        "tool_content_bytes": tool_content_bytes,
        "tools_len": len(_json.dumps(body.get("tools"), ensure_ascii=False)),
        "body_bytes": len(bt),
    }

def _dump_11128_body(body: dict, channel: str, model: str) -> str:
    """11128 自愈精简后仍失败时，把实际出站 body 写到文件供排查。

    返回文件路径（写失败返回空串）。文件含完整请求体，注意可能含敏感内容，
    排查完记得删除。
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

from storage import database as db
from accounts import auth_manager

BACKEND = "https://copilot.tencent.com"
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

# 腾讯内容审核拦截时返回的固定话术特征（HTTP 200 + 正文是这段话）。
# 仅匹配短拒答，避免正常回答引用审查文案时被误标。
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


# 工具停转（tool stall）检测与修复开关。
# 场景：agent 工具循环回合（请求带 tools 且历史含 role=tool），上游模型却以
# finish_reason=stop + 纯文本（"好的，马上继续跑流程"式确认话术）结束且未调用
# 任何工具 —— 工作流卡死成纯聊天（issue #31）。
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
    """是否为 agent 工具循环回合：声明了 tools 且历史里存在工具结果。"""
    if not isinstance(body.get("tools"), list) or not body["tools"]:
        return False
    return any(
        isinstance(msg, dict) and msg.get("role") == "tool"
        for msg in (body.get("messages") or [])
    )


def _looks_like_stall_text(text: str) -> bool:
    """空内容视为 stall；否则要求短文本且像'知道了，马上继续'式话术，
    排除总结性回答。"""
    text = (text or "").strip()
    if not text:
        return True
    if len(text) > 160:
        return False
    if any(marker in text for marker in _STALL_NEGATIVE_MARKERS):
        return False
    return any(marker in text for marker in _STALL_POSITIVE_MARKERS)


def _is_tool_stall(body: dict, finish_reason, tool_calls: bool, text: str) -> bool:
    """判定一次上游完成是否属于工具停转（stall）。"""
    if not _request_has_tool_loop(body):
        return False
    if tool_calls:
        return False
    if (finish_reason or "stop") not in {"stop", None}:
        return False
    return _looks_like_stall_text(text)


def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS_CODES or status in {401, 403}


async def _retry_delay(attempt: int):
    await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))

PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

_BACKEND_ROLE_ALIASES = {
    "developer": "system",
}

DEFAULT_MODELS = [
    {"id": "glm-5.2", "name": "GLM-5.2"},
    {"id": "glm-5.1", "name": "GLM-5.1"},
    {"id": "glm-5v-turbo", "name": "GLM-5V Turbo"},
    {"id": "kimi-k2.7", "name": "Kimi K2.7"},
    {"id": "kimi-k2.6", "name": "Kimi K2.6"},
    {"id": "kimi-k2.5", "name": "Kimi K2.5"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
    {"id": "minimax-m3-pay", "name": "MiniMax M3"},
    {"id": "hy3-preview-agent", "name": "HY3 Preview Agent"},
    {"id": "auto", "name": "Auto (auto routing)"},
]

# Built-in model aliases: alias_id -> backend_model_id
# Extended by user-defined aliases from database settings "model_aliases".
_BUILTIN_ALIASES = {
    # GPT-5.x 系列 → 映射到后端可用模型
    "gpt-5.5": "glm-5.2",
    "gpt-5.5-mini": "glm-5.1",
    "gpt-5.4": "glm-5.2",
    "gpt-5.4-mini": "glm-5.1",
    "gpt-5.4-codex": "glm-5.2",
    "gpt-5.1": "glm-5.2",
    "gpt-5.1-codex": "glm-5.2",
    "gpt-5": "glm-5.2",
    "gpt-5-mini": "glm-5.1",
    # GPT-4.x 系列
    "gpt-4o": "glm-5.2",
    "gpt-4o-mini": "glm-5.1",
    "gpt-4-turbo": "glm-5.2",
    "gpt-4": "glm-5.2",
    "gpt-4.1": "glm-5.2",
    "gpt-4.1-mini": "glm-5.1",
    "gpt-3.5-turbo": "glm-5.1",
    # o 系列推理模型
    "o3": "deepseek-v4-pro",
    "o3-mini": "deepseek-v4-flash",
    "o4-mini": "deepseek-v4-pro",
    "o1": "deepseek-v4-pro",
    "o1-mini": "deepseek-v4-flash",
    # Claude 系列
    "claude-3.5-sonnet": "deepseek-v4-pro",
    "claude-3-haiku": "deepseek-v4-flash",
    "claude-sonnet-4": "deepseek-v4-pro",
    "claude-opus-4": "deepseek-v4-pro",
    # DeepSeek
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-coder": "deepseek-v4-pro",
    "deepseek-r1": "deepseek-v4-pro",
    # Moonshot
    "moonshot-v1-128k": "kimi-k2.7",
    "moonshot-v1-32k": "kimi-k2.6",
}


def effective_builtin_aliases() -> dict:
    """WorkBuddy 生效别名表（整体替换语义，与其它通道一致）：

    未设置 `model_aliases` → 内置默认别名；
    已设置（哪怕空对象）  → 完全以自定义为准，内置别名全部失效。
    """
    raw = db.get_setting("model_aliases", None)
    if raw is None:
        return dict(_BUILTIN_ALIASES)
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def resolve_model_alias(model: str) -> str:
    """Resolve an alias to its real backend model ID. Returns original if no match."""
    return effective_builtin_aliases().get(model, model)


def _configured_reasoning_default(model: str) -> str | None:
    """按模型解析生效的思考档位（取代旧环境变量机制）。

    优先级：客户端显式参数 > 按模型配置 > 通道默认 > 不注入。
    仅 WorkBuddy 通道上游确认支持 reasoning_effort（见 docs/design/...）。
    """
    from providers.model_config import reasoning_for_model

    return reasoning_for_model("workbuddy", model)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---- content 精简（避免上游 11128 拦截）：按通道+客户端记账的自愈机制 ----
# 只对 ZCode Client 出的请求生效：只有 ZCode 触发过一次 11128 后才精简（armed），
# 或显式设 CB_GATEWAY_COMPACT_CHARS 强制启用；DSH 及其它 agent 一律不精简，
# 即使同一 workbuddy 通道命中过 11128。默认不精简，避免无谓地丢失信息。
_COMPACTION_LOCK = threading.Lock()
_ARMRED_KEYS: set[tuple] = set()
_COMPACTION_STATS = {"armed_triggers": 0, "compacted_messages": 0, "retried_11128": 0}
_COMPACT_11128_MARKERS = ("11128", "Illegal API invocation")
_COMPACT_ENABLED_CLIENTS = ("zcode",)


def _compaction_key(channel: Optional[str], client_tag) -> tuple:
    return (channel or "", client_tag or "")


def _client_allows_compact(client_tag) -> bool:
    """只允许 ZCode Client 参与精简，其它客户端（dsh/curl/python/空）一概不精简。"""
    return client_tag in _COMPACT_ENABLED_CLIENTS


def _channel_armed(channel: Optional[str], client_tag) -> bool:
    if not _client_allows_compact(client_tag):
        return False
    with _COMPACTION_LOCK:
        return _compaction_key(channel, client_tag) in _ARMRED_KEYS


def _arm_channel(channel: Optional[str], client_tag) -> None:
    if not _client_allows_compact(client_tag):
        return
    with _COMPACTION_LOCK:
        _ARMRED_KEYS.add(_compaction_key(channel, client_tag))
        _COMPACTION_STATS["armed_triggers"] += 1


def compaction_stats() -> dict:
    """暴露给 /admin/stats：精简触发/生效的计数，便于判断阈值松紧。"""
    return {
        "compacted_messages": _COMPACTION_STATS["compacted_messages"],
        "armed_keys": len(_ARMRED_KEYS),
        "armed_triggers": _COMPACTION_STATS["armed_triggers"],
        "retried_11128": _COMPACTION_STATS["retried_11128"],
        "enabled_clients": list(_COMPACT_ENABLED_CLIENTS),
    }


def _is_11128_error(status: int, payload, body: dict) -> bool:
    """判定一次上游返回是否 11128 大内容拦截。payload 为 raw bytes 或已解析 dict。"""
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
    # 已深度精简后仍 11128 不值得再自愈，避免空转
    if body.get("_compacted_11128"):
        return False
    return True


def _compact_text(text: str, cap: int):
    """单条文本截短到 cap，保留头部+尾部：tool 结果的报错/summary 常在末尾。"""
    n = len(text)
    if n <= cap:
        return text, False
    tail_budget = max(8, cap // 5)  # 尾部保留 ~20%，最少 8 字符（报错/summary 常在末尾）
    head = cap - tail_budget
    out = text[:head] + f"\n...[省略 {n-head-tail_budget} 字符]..." + (text[-tail_budget:] if tail_budget > 0 else "")
    return out, True


def _compact_tools(tools, description_cap: int):
    """精简 tools 定义里的超大文本字段，压低请求体量（11128 常见触发源）。

    只截短描述性字符串（description / schema 里的 description），
    绝不触碰结构键（name、type、property 名、required、enum 值本身），
    保证工具调用契约不被破坏。返回 (new_tools, changed)。
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


def _compact_schema_descriptions(node, cap):
    """递归精简 JSON Schema 里的 description 字符串，保留结构键。"""
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


def _smart_compact_messages(body: dict, *, channel: Optional[str] = None,
                            client_tag=None) -> bool:
    """按需精简超大请求体，避免上游 11128 拦截。返回是否动过请求。

    实测确认（见 docs/workbuddy-11128-troubleshoot.md）：
      - 11128 由「内容特征」触发，不是总量：system 里的 git/commit 块、
        超大 content 里的特定内容。只要把这些字段纯头部截短到安全阈值，
        即使 body 仍有 700KB 也能通过（真实上游验证 200）。
      - 因此这里只做单字段纯头切，**不做总量兜底**——总量兜底会把
        content 无脑压成碎片（曾把 system 压到 137 字符），既破坏语义
        又降不到预算，反而制造问题。

    只精简纯文本字段（不破坏名称/结构/参数契约）。system 指令用单独的宽松阈值
    截短。启用条件二选一：
      - 该 (通道, 客户端) 已触发过 11128（armed），用激进阈值自愈；仅 ZCode 生效；
      - 显式设 CB_GATEWAY_COMPACT_CHARS>0 强制启用（全局，作用于该通道；
        仍只对 ZCode Client 生效）。
    """
    if not _client_allows_compact(client_tag):
        return False
    forced_cap = _env_int("CB_GATEWAY_COMPACT_CHARS", 0)
    if not _channel_armed(channel, client_tag) and forced_cap <= 0:
        return False
    base_cap = forced_cap if forced_cap > 0 else _env_int("CB_GATEWAY_COMPACT_ARMED_CHARS", 3000)
    if base_cap <= 0:
        return False
    # system 指令单独阈值：默认 5000 字符（实测 4000~5000 都能解除 11128，
    # 6000 仍会触发；比普通消息宽松，尽量保留系统提示语义）。
    system_cap = _env_int("CB_GATEWAY_COMPACT_SYSTEM_CHARS", 5000)
    messages = body.get("messages")
    any_changed = False
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system":
                # system 用纯头部截断：实测其尾部（如 git status / commit 历史块）
                # 是 11128 触发源，头尾保留反而把触发内容留在体内。
                content = m.get("content")
                if isinstance(content, str) and len(content) > system_cap:
                    m["content"] = content[:system_cap]
                    any_changed = True
                continue
            content = m.get("content")
            if isinstance(content, str) and len(content) > base_cap:
                # 普通消息 content 也纯头切：11128 是内容特征触发，
                # 头尾保留可能把触发块留在尾部。
                m["content"] = content[:base_cap]
                any_changed = True
            # reasoning_content 是纯思维链文本，截断安全且常为超大单点（11128 高发）
            rc = m.get("reasoning_content")
            if isinstance(rc, str) and len(rc) > base_cap:
                m["reasoning_content"] = rc[:base_cap]
                any_changed = True
    # tools 定义里的超大描述文本也是 11128 常见触发源，一并精简
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        new_tools, tools_changed = _compact_tools(tools, base_cap)
        if tools_changed:
            any_changed = True
            body["tools"] = new_tools
    if any_changed:
        with _COMPACTION_LOCK:
            _COMPACTION_STATS["compacted_messages"] += 1
    return any_changed


def build_backend_body(payload: dict) -> dict:
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    messages = body.get("messages")
    if isinstance(messages, list):
        body["messages"] = [
            {
                **message,
                "role": _BACKEND_ROLE_ALIASES.get(message.get("role"), message.get("role")),
            }
            if isinstance(message, dict) and message.get("role") in _BACKEND_ROLE_ALIASES
            else message
            for message in messages
        ]
    # 注：content 精简不在此构建期做。11128 自愈精简只在转发失败后的重试路径触发，
    # 那里才拿得到客户端信息（仅 ZCode Client 参与），避免构建期无谓地全量截断。
    has_explicit_thinking = "thinking" in payload
    # Resolve model alias before forwarding
    raw_model = body.get("model", "auto")
    body["model"] = resolve_model_alias(raw_model)
    if "reasoning_effort" not in body and not has_explicit_thinking:
        default_reasoning = _configured_reasoning_default(body["model"])
        if default_reasoning:
            body["reasoning_effort"] = default_reasoning
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def get_all_aliases() -> dict:
    """Return effective aliases (custom replaces built-ins; see effective_builtin_aliases)."""
    return effective_builtin_aliases()


def _safe_err(raw: bytes, status: int) -> dict:
    try:
        detail = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        detail = {"error": {"message": raw.decode("utf-8", "replace")[:500],
                            "type": "upstream_error"}}
    return detail


def _err_sse_event(raw: bytes, status: int) -> bytes:
    msg = raw.decode("utf-8", "replace")[:500]
    payload = json.dumps({"error": {"message": msg, "type": "upstream_error", "code": status}})
    event = f"data: {payload}\n\ndata: [DONE]\n\n"
    return event.encode("utf-8")


def _json_sse_event(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _has_terminal_choice(payload: dict) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, dict) and bool(choice.get("finish_reason"))
        for choice in choices
    )


_MAX_SSE_EVENT_BYTES = 8 * 1024 * 1024


def _repair_json_arguments(raw: str) -> str:
    """尝试修复上游截断的工具调用 arguments（hy3 长时间流式偶发）。

    只做尾部补全：从后往前尝试补上缺失的 `}` / `]` / `"`，直到能解析成
    JSON 对象。修不动就原样返回（调用方会按不完整报错）。
    """
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
        return raw if isinstance(parsed, dict) else raw
    except (json.JSONDecodeError, RecursionError, TypeError):
        pass
    # 从尾部逐步补闭合符，最多尝试补 16 个（避免死循环/过度猜测）
    for extra in range(1, 17):
        candidate = raw + "}" * extra
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        if isinstance(parsed, dict):
            return candidate
    # 再试补 ] 和 " 组合（嵌套数组/字符串未闭合的场景）
    for tail in ("]", "]", "}", "\"}", "\"]", "}}", "]}", "\"}"):
        candidate = raw + tail
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError, TypeError):
            continue
        if isinstance(parsed, dict):
            return candidate
    return raw


class _ChatStreamObserver:
    """Track completion state while Chat Completions SSE is normalized."""

    def __init__(self, fallback_model: str, expected_choices: int = 1):
        self.fallback_model = fallback_model
        if not isinstance(expected_choices, int) or isinstance(expected_choices, bool):
            expected_choices = 1
        self.expected_choice_indices = set(range(expected_choices if 1 <= expected_choices <= 128 else 1))
        self.seen_done = False
        self.saw_chat_chunk = False
        self.upstream_error = False
        self.upstream_error_event: dict | None = None
        self.finish_reasons: dict[int, str | None] = {}
        self.closed_choices: set[int] = set()
        self.content_choices: set[int] = set()
        self.tool_call_choices: set[int] = set()
        self.tool_calls: dict[tuple[int, int], dict] = {}
        self.malformed_data_event = False
        self.parser_error: str | None = None
        self.usage: dict = {}
        self.content_parts: list[str] = []
        self.metadata: dict = {}

    def observe_event(self, data: bytes) -> dict | None:
        if data.strip() == b"[DONE]":
            self.seen_done = True
            return None
        if self.seen_done:
            self.parser_error = "The upstream sent data after the [DONE] event."
            return None
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.malformed_data_event = True
            return None
        if not isinstance(obj, dict):
            self.malformed_data_event = True
            return None

        if "error" in obj and obj["error"] is not None:
            self.upstream_error = True
            self.upstream_error_event = obj
            return None

        choices = obj.get("choices")
        is_chat_chunk = obj.get("object") == "chat.completion.chunk" or "choices" in obj
        if is_chat_chunk and not isinstance(choices, list):
            self.parser_error = "The upstream Chat Completions chunk had an invalid choices field."
            return None
        if is_chat_chunk:
            self.saw_chat_chunk = True
            for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
                if key in obj:
                    self.metadata[key] = obj[key]

        event_usage = obj.get("usage")
        if event_usage is not None and not isinstance(event_usage, dict):
            self.parser_error = "The upstream Chat Completions chunk had invalid usage data."
            return None
        if isinstance(event_usage, dict):
            self.usage.update(event_usage)
        if not is_chat_chunk:
            self.parser_error = "The upstream SSE event was not a Chat Completions chunk."
            return None

        validated_choices: list[tuple[int, dict, str | None]] = []
        event_choice_indices: set[int] = set()
        for choice in choices:
            if not isinstance(choice, dict):
                self.parser_error = "The upstream Chat Completions chunk contained an invalid choice."
                return None
            index = choice.get("index", 0)
            if not isinstance(index, int) or isinstance(index, bool):
                self.parser_error = "The upstream Chat Completions choice had an invalid index."
                return None
            if index not in self.expected_choice_indices:
                self.parser_error = "The upstream Chat Completions choice index was not requested."
                return None
            if index in event_choice_indices:
                self.parser_error = "The upstream Chat Completions chunk repeated a choice index."
                return None
            event_choice_indices.add(index)
            if index in self.closed_choices:
                self.parser_error = "The upstream sent another delta after a choice had finished."
                return None
            reason = choice.get("finish_reason")
            if reason == "":
                reason = None
                choice["finish_reason"] = None
            elif reason is not None and not isinstance(reason, str):
                self.parser_error = "The upstream Chat Completions choice had an invalid finish reason."
                return None
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                self.parser_error = "The upstream Chat Completions choice had an invalid delta."
                return None
            for content_field in ("content", "reasoning_content"):
                content = delta.get(content_field)
                if content is not None and not isinstance(content, str):
                    self.parser_error = (
                        f"The upstream Chat Completions choice had invalid {content_field}."
                    )
                    return None
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is not None and not isinstance(tool_deltas, list):
                self.parser_error = "The upstream Chat Completions choice had invalid tool calls."
                return None
            if isinstance(tool_deltas, list):
                for position, tool_delta in enumerate(tool_deltas):
                    if not isinstance(tool_delta, dict):
                        self.parser_error = "The upstream tool call stream contained an invalid delta."
                        return None
                    tool_index = tool_delta.get("index", position)
                    if (
                        not isinstance(tool_index, int)
                        or isinstance(tool_index, bool)
                        or tool_index < 0
                    ):
                        self.parser_error = "The upstream tool call stream had an invalid index."
                        return None
                    call_id = tool_delta.get("id")
                    if call_id is not None and (not isinstance(call_id, str) or not call_id):
                        self.parser_error = "The upstream tool call stream had an invalid call id."
                        return None
                    call_type = tool_delta.get("type")
                    if call_type is not None and call_type != "function":
                        self.parser_error = "The upstream tool call stream had an invalid call type."
                        return None
                    function = tool_delta.get("function")
                    if function is not None and not isinstance(function, dict):
                        self.parser_error = "The upstream tool call stream had an invalid function."
                        return None
                    if isinstance(function, dict):
                        name = function.get("name")
                        if name == "":
                            function.pop("name", None)
                            name = None
                        elif name is not None and not isinstance(name, str):
                            self.parser_error = "The upstream tool call stream had an invalid function name."
                            return None
                        arguments = function.get("arguments")
                        if arguments is not None and not isinstance(arguments, str):
                            self.parser_error = "The upstream tool call stream had invalid arguments."
                            return None
            validated_choices.append((index, delta, reason))

        for index, delta, reason in validated_choices:
            self.finish_reasons.setdefault(index, None)
            if reason:
                self.finish_reasons[index] = reason
                self.closed_choices.add(index)
            content = delta.get("content")
            if content:
                self.content_parts.append(content)
                self.content_choices.add(index)
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is None:
                continue
            if tool_deltas:
                self.tool_call_choices.add(index)
            for position, tool_delta in enumerate(tool_deltas):
                tool_index = tool_delta.get("index", position)
                state = self.tool_calls.setdefault(
                    (index, tool_index),
                    {"id": None, "name": None, "arguments": ""},
                )
                call_id = tool_delta.get("id")
                if call_id:
                    if state["id"] not in (None, call_id):
                        self.parser_error = "The upstream tool call stream changed a call id."
                        return None
                    state["id"] = call_id
                function = tool_delta.get("function")
                if function is None:
                    continue
                name = function.get("name")
                if name:
                    if state["name"] not in (None, name):
                        self.parser_error = "The upstream tool call stream changed a function name."
                        return None
                    state["name"] = name
                arguments = function.get("arguments")
                if arguments is None:
                    continue
                state["arguments"] += arguments
        return obj

    def missing_finish_choices(self) -> list[int]:
        return sorted(index for index, reason in self.finish_reasons.items() if not reason)

    def eof_error(self) -> str | None:
        if self.parser_error:
            return self.parser_error
        if self.malformed_data_event:
            return "The upstream stream ended with a malformed SSE JSON event."
        if self.upstream_error:
            return "The upstream returned an error event in an HTTP 200 stream."
        if not self.saw_chat_chunk:
            return "The upstream stream ended without a Chat Completions chunk."
        missing_choices = self.expected_choice_indices.difference(self.finish_reasons)
        if missing_choices:
            return "The upstream stream ended before all requested choices were received."
        for choice_index, reason in self.finish_reasons.items():
            if reason == "tool_calls" and choice_index not in self.tool_call_choices:
                return "The upstream ended with tool_calls but did not provide a tool call."
            if choice_index in self.tool_call_choices and reason not in {
                None,
                "tool_calls",
                "length",
                "content_filter",
            }:
                return "The upstream tool call stream ended with an inconsistent finish reason."
            if not reason and choice_index not in self.tool_call_choices:
                return "The upstream stream ended before the choice received a finish reason."
        for choice_index in self.tool_call_choices:
            calls = [
                state
                for (current_choice, _), state in self.tool_calls.items()
                if current_choice == choice_index
            ]
            if not calls:
                return "The upstream tool call stream ended before the tool call was identified."
            for state in calls:
                if self.finish_reasons.get(choice_index) in {"length", "content_filter"}:
                    continue
                if not state["id"] or not state["name"]:
                    return "The upstream tool call stream ended before the tool call was complete."
                repaired = _repair_json_arguments(state["arguments"])
                try:
                    arguments = json.loads(repaired)
                except (json.JSONDecodeError, RecursionError, TypeError):
                    return "The upstream tool call stream ended with incomplete JSON arguments."
                if not isinstance(arguments, dict):
                    return "The upstream tool call arguments were not a JSON object."
                if repaired != state["arguments"]:
                    # 上游把 arguments 尾部截断了（hy3 长时间流式偶发）：
                    # 修复后按修复值透传，避免整个回合失败。
                    state["arguments"] = repaired
        for choice_index, reason in self.finish_reasons.items():
            if (
                reason not in {"length", "content_filter"}
                and choice_index not in self.content_choices
                and choice_index not in self.tool_call_choices
            ):
                return "The upstream choice ended without content or a tool call."
        return None

    def terminal_event(self, choice_indices: list[int]) -> bytes:
        payload = {
            "id": self.metadata.get("id") or "chatcmpl-" + os.urandom(12).hex(),
            "object": "chat.completion.chunk",
            "created": self.metadata.get("created") or int(time.time()),
            "model": self.metadata.get("model") or self.fallback_model,
            "choices": [
                {
                    "index": index,
                    "delta": {},
                    "finish_reason": "tool_calls" if index in self.tool_call_choices else "stop",
                }
                for index in choice_indices
            ],
        }
        for key in ("system_fingerprint", "service_tier"):
            if key in self.metadata:
                payload[key] = self.metadata[key]
        return _json_sse_event(payload)


class _SSEEventDecoder:
    """Decode complete SSE data fields from arbitrary byte chunks."""

    def __init__(self):
        self.parser_error: str | None = None
        self._buffer = b""
        self._data_lines: list[bytes] = []
        self._event_bytes = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        if self.parser_error:
            return []
        self._buffer += chunk
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
        if not self.parser_error and len(self._buffer) > _MAX_SSE_EVENT_BYTES:
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

    def _consume_line(self, line: bytes) -> bytes | None:
        if len(line) > _MAX_SSE_EVENT_BYTES:
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
        if self._event_bytes > _MAX_SSE_EVENT_BYTES:
            self._fail("The upstream SSE event exceeded the 8 MiB limit.")
            return None
        self._data_lines.append(data)
        return None

    def _fail(self, message: str) -> None:
        self.parser_error = message
        self._buffer = b""
        self._data_lines = []
        self._event_bytes = 0


def _extract_cache_tokens(usage: dict | None) -> tuple[int, int]:
    """从上游 usage 提取 (cache_read, cache_creation)，兼容三种字段风格。

    优先级：Anthropic → DeepSeek → OpenAI。
      - Anthropic: cache_read_input_tokens / cache_creation_input_tokens
      - DeepSeek:  prompt_cache_hit_tokens(→cache_read) / prompt_cache_miss_tokens(→creation 不计入)
      - OpenAI:    prompt_tokens_details.cached_tokens(→cache_read)，无 creation 概念
    全部缺省返回 (0, 0)。负值 clamp 到 0；cache_read 不超过 prompt_tokens（cache_read 是 prompt 子集）。
    """
    if not usage or not isinstance(usage, dict):
        return (0, 0)

    cache_read = 0
    cache_creation = 0

    # 1) Anthropic 风格
    ar = usage.get("cache_read_input_tokens")
    ac = usage.get("cache_creation_input_tokens")
    if ar is not None or ac is not None:
        cache_read = int(ar) if ar is not None else 0
        cache_creation = int(ac) if ac is not None else 0
        return (
            max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0))),
            max(0, cache_creation),
        )

    # 2) DeepSeek 风格
    dh = usage.get("prompt_cache_hit_tokens")
    if dh is not None:
        cache_read = int(dh)
        return (
            max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0))),
            0,
        )

    # 3) OpenAI 风格
    ptd = usage.get("prompt_tokens_details")
    if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
        cache_read = int(ptd["cached_tokens"])
        return (
            max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0))),
            0,
        )

    return (0, 0)


def _log_request(api_key_info, account, model_name, stream,
                  prompt_t, completion_t, total_t, credit,
                  finish_reason, status_code, error_msg, t0,
                  increment_usage: bool = True,
                  usage: dict | None = None,
                  reasoning_effort: str | None = None):
    elapsed_ms = int((time.time() - t0) * 1000)
    log_data = {
        "api_key_id": api_key_info["id"] if api_key_info else None,
        "api_key_name": api_key_info["name"] if api_key_info else None,
        "account_id": account["id"] if account else None,
        "account_name": account.get("name") if account else None,
        "provider": (account.get("provider") if account else None)
        or (api_key_info.get("_bind_channel") if api_key_info else None)
        or "workbuddy",
        "model": model_name,
        "stream": 1 if stream else 0,
        "reasoning_effort": reasoning_effort,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "credit": credit,
        "finish_reason": finish_reason,
        "duration_ms": elapsed_ms,
        "status_code": status_code,
        "error_msg": error_msg,
        "increment_usage": increment_usage,
        "client": (api_key_info or {}).get("_client_tag"),
        "client_version": (api_key_info or {}).get("_client_version"),
    }
    # Cache 命中追踪：兼容三种字段风格，整包 dump 留证据。
    cache_read, cache_creation = _extract_cache_tokens(usage)
    log_data["cache_read_tokens"] = cache_read
    log_data["cache_creation_tokens"] = cache_creation
    usage_json = None
    if usage is not None:
        try:
            serialized = json.dumps(usage, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = None
        # 体积保护：序列化后 >64KB 时只留存提取结果，避免超大 usage 污染日志表。
        if serialized is not None and len(serialized.encode("utf-8")) > 65536:
            serialized = json.dumps(
                {"truncated": True, "cache_read_tokens": cache_read,
                 "cache_creation_tokens": cache_creation},
                ensure_ascii=False,
            )
        usage_json = serialized
    log_data["usage_json"] = usage_json
    # credit_source='live' 门槛：usage 含任意已知 cache 键即标 live（实测语义，与 dashboard accurate 对齐）。
    _known_cache_keys = (
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        "prompt_tokens_details",
    )
    log_data["credit_source"] = (
        "live" if usage is not None and any(k in usage for k in _known_cache_keys) else None
    )
    try:
        # 写日志（含 BEGIN IMMEDIATE 事务 + fsync）不占事件循环：
        # 放进默认线程池 fire-and-forget，日志失败只静默丢弃。
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, db.record_request, log_data)
        # fire-and-forget：吞掉 executor 内抛出的异常，避免“异常从未被读取”告警
        fut.add_done_callback(lambda f: f.exception() if f.cancelled() is False else None)
    except Exception:
        pass


async def proxy_chat_completions(
    payload: dict,
    api_key_info: Optional[dict] = None,
    log_model: Optional[str] = None,
) -> tuple:
    """
    主代理函数。

    返回:
      - ("stream", async_generator)  流式响应
      - ("json", dict)               非流式响应
      - ("error", (status_code, detail))  错误
    """
    client_wants_stream = bool(payload.get("stream"))
    body = build_backend_body(payload)
    # 实际发给上游的思考档位（客户端显式或按模型配置注入）：用于请求日志
    effective_reasoning = body.get("reasoning_effort")
    if log_model is None and isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    model_name = log_model if log_model is not None else payload.get("model", "auto")

    if client_wants_stream:
        return (
            "stream",
            _stream_upstream(body, api_key_info, model_name),
        )

    tried_ids: set[int] = set()
    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        account = await auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            break

        tried_ids.add(account["id"])
        headers = await auth_manager.get_valid_headers(account)
        if not headers:
            auth_manager.mark_account_failure(account["id"], 401)
            continue

        url = f"{auth_manager.backend_url()}/v2/chat/completions"
        t0 = time.time()
        result = await _collect_stream(url, headers, body, account, api_key_info, model_name, t0)
        if result[0] == "json":
            # 工具停转修复：agent 回合被上游以 stop+纯文本结束且未调用工具时，
            # 用 tool_choice=required 重试一次；重试产出工具调用则采用重试结果。
            if TOOL_STALL_RETRY:
                choice = (result[1].get("choices") or [{}])[0]
                message = choice.get("message") or {}
                if _is_tool_stall(
                    body,
                    choice.get("finish_reason"),
                    bool(message.get("tool_calls")),
                    message.get("content") or "",
                ):
                    retry_body = {**body, "tool_choice": "required"}
                    retry_t0 = time.time()
                    retry_result = await _collect_stream(
                        url, headers, retry_body, account, api_key_info, model_name, retry_t0
                    )
                    if retry_result[0] == "json":
                        retry_choice = (retry_result[1].get("choices") or [{}])[0]
                        retry_message = retry_choice.get("message") or {}
                        if retry_message.get("tool_calls"):
                            auth_manager.mark_account_success(account["id"])
                            return retry_result
            auth_manager.mark_account_success(account["id"])
            return result

        channel = account.get("provider") or "workbuddy"
        client = (api_key_info or {}).get("_client_tag")
        err_status = result[1][0]
        # 11128 大内容拦截：武装该 (通道,客户端) + 用激进阈值精简后原地重试（自愈）。
        # 仅 ZCode Client 参与精简；DSH 及其它 agent 不精简。
        if _is_11128_error(err_status, result[1][1], body):
            _arm_channel(channel, client)
            _smart_compact_messages(body, channel=channel, client_tag=client)
            body["_compacted_11128"] = True
            with _COMPACTION_LOCK:
                _COMPACTION_STATS["retried_11128"] += 1
            retry_t0 = time.time()
            retry_result = await _collect_stream(
                url, headers, body, account, api_key_info, model_name, retry_t0
            )
            if retry_result[0] == "json":
                auth_manager.mark_account_success(account["id"])
                return retry_result
            # 精简后仍失败：落为普通错误走统一处理（不再尝试切换账号疯转）
            result = retry_result
            err_status = retry_result[1][0]
            dump_path = _dump_11128_body(body, channel, model_name)
            logger.warning(
                "11128 self-heal retry still failed (non-stream) "
                "profile=%s channel=%s model=%s dump=%s",
                _body_size_profile(body),
                channel,
                model_name,
                dump_path,
            )

        last_error = result
        auth_manager.mark_account_failure(account["id"], err_status)
        will_retry = _is_retryable_status(err_status) and attempt < max_retries - 1
        detail = result[1][1]
        error_message = detail
        if isinstance(detail, dict):
            error_data = detail.get("error") if isinstance(detail.get("error"), dict) else detail
            error_message = error_data.get("message", detail) if isinstance(error_data, dict) else detail
        _log_request(
            api_key_info, account, model_name, False,
            0, 0, 0, 0, "retry" if will_retry else "error",
            err_status, str(error_message)[:500], t0,
            increment_usage=not will_retry,
            reasoning_effort=effective_reasoning,
        )
        if not will_retry:
            return result
        await _retry_delay(attempt)

    return last_error or (
        "error",
        (503, {"error": {"message": "No available accounts", "type": "server_error"}}),
    )


async def test_account_chat(account: dict, model: str = "auto", prompt: str = "ping") -> dict:
    """Run a small non-streaming request against one specific account."""
    headers = await auth_manager.get_valid_headers(account)
    if not headers:
        return {
            "ok": False,
            "status_code": 401,
            "duration_ms": 0,
            "message": "token refresh failed or account credentials are invalid",
        }

    body = build_backend_body({
        "model": model or "auto",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": False,
    })
    url = f"{auth_manager.backend_url()}/v2/chat/completions"
    t0 = time.time()
    result = await _collect_stream(url, headers, body, account, None, f"account-test:{model or 'auto'}", t0)
    duration_ms = int((time.time() - t0) * 1000)

    if result[0] == "json":
        data = result[1]
        message = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        return {
            "ok": True,
            "status_code": 200,
            "duration_ms": duration_ms,
            "model": data.get("model"),
            "message": message[:240],
            "usage": usage,
        }

    status, detail = result[1]
    msg = detail
    if isinstance(detail, dict):
        err = detail.get("error") if isinstance(detail.get("error"), dict) else detail
        msg = err.get("message") if isinstance(err, dict) else detail
    return {
        "ok": False,
        "status_code": status,
        "duration_ms": duration_ms,
        "message": str(msg)[:500],
    }


async def _stream_upstream(
    body: dict,
    api_key_info: Optional[dict],
    model_name: str,
) -> AsyncGenerator[bytes, None]:
    """Stream upstream SSE with pre-output account failover and backoff."""
    tried_ids: set[int] = set()
    last_error = b"No available accounts"
    # 实际发给上游的思考档位（客户端显式或按模型配置注入）：用于请求日志
    effective_reasoning = body.get("reasoning_effort")
    last_error_event: dict | None = None
    last_status = 503
    last_account = None
    last_started = time.time()
    pending_retry_log: dict | None = None

    for attempt in range(3):
        account = await auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            break
        channel = account.get("provider") or "workbuddy"
        if pending_retry_log is not None:
            _log_request(
                api_key_info,
                pending_retry_log["account"],
                model_name,
                True,
                pending_retry_log["prompt_tokens"],
                pending_retry_log["completion_tokens"],
                pending_retry_log["total_tokens"],
                pending_retry_log["credit"],
                "retry",
                pending_retry_log["status"],
                pending_retry_log["message"],
                pending_retry_log["started"],
                increment_usage=False,
                reasoning_effort=effective_reasoning,
            )
            await _retry_delay(pending_retry_log["attempt"])
            pending_retry_log = None
        last_account = account
        tried_ids.add(account["id"])
        headers = await auth_manager.get_valid_headers(account)
        if not headers:
            auth_manager.mark_account_failure(account["id"], 401)
            last_error = b"Account credentials are invalid"
            last_error_event = None
            last_status = 401
            continue

        url = f"{auth_manager.backend_url()}/v2/chat/completions"
        t0 = time.time()
        last_started = t0
        observer = _ChatStreamObserver(body.get("model") or model_name, body.get("n", 1))
        decoder = _SSEEventDecoder()
        output_started = False
        pending_terminal_events: list[bytes] = []
        pending_terminal_bytes = 0
        stop_reading = False

        try:
            timeout = httpx.Timeout(
                connect=10,
                read=auth_manager.request_timeout(300),
                write=30,
                pool=10,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        raw_error = await response.aread()
                        # 11128 大内容拦截：武装通道 + 激进精简后原地重试（自愈）。
                        if _is_11128_error(response.status_code, raw_error, body):
                            _arm_channel(channel, (api_key_info or {}).get("_client_tag"))
                            _smart_compact_messages(
                                body, channel=channel,
                                client_tag=(api_key_info or {}).get("_client_tag"),
                            )
                            body["_compacted_11128"] = True
                            with _COMPACTION_LOCK:
                                _COMPACTION_STATS["retried_11128"] += 1
                            # 同一账号重发一次：从 tried 移除以免单账号通道被误判为无可用账号
                            tried_ids.discard(account["id"])
                            attempt -= 1
                            continue
                        last_error = raw_error
                        last_error_event = None
                        last_status = response.status_code
                        if body.get("_compacted_11128"):
                            # 自愈精简后仍失败：记录 body 特征 + 完整出站体，便于定位触发源
                            dump_path = _dump_11128_body(body, channel, model_name)
                            logger.warning(
                                "11128 self-heal retry still failed "
                                "profile=%s channel=%s model=%s dump=%s",
                                _body_size_profile(body),
                                channel,
                                model_name,
                                dump_path,
                            )
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        if _is_retryable_status(response.status_code) and attempt < 2:
                            pending_retry_log = {
                                "account": account,
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                                "credit": 0,
                                "status": response.status_code,
                                "message": raw_error.decode("utf-8", "replace")[:500],
                                "started": t0,
                                "attempt": attempt,
                            }
                            continue
                        _log_request(
                            api_key_info, account, model_name, True,
                            0, 0, 0, 0, "error", response.status_code,
                            raw_error.decode("utf-8", "replace")[:500], t0,
                            reasoning_effort=effective_reasoning,
                        )
                        yield _err_sse_event(raw_error, response.status_code)
                        return

                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        for data in decoder.feed(chunk):
                            obj = observer.observe_event(data)
                            if obj is not None and not obj.get("error"):
                                encoded = _json_sse_event(obj)
                                if pending_terminal_events or _has_terminal_choice(obj):
                                    pending_terminal_events.append(encoded)
                                    pending_terminal_bytes += len(encoded)
                                    if pending_terminal_bytes > _MAX_SSE_EVENT_BYTES:
                                        observer.parser_error = (
                                            "The upstream terminal SSE events exceeded the 8 MiB limit."
                                        )
                                else:
                                    output_started = True
                                    yield encoded
                            if (
                                observer.seen_done
                                or observer.parser_error
                                or observer.malformed_data_event
                                or observer.upstream_error
                            ):
                                stop_reading = True
                                break
                        if decoder.parser_error and not observer.seen_done:
                            observer.parser_error = decoder.parser_error
                            stop_reading = True
                        if stop_reading:
                            break
        except httpx.HTTPError as exc:
            last_error = str(exc).encode("utf-8", "replace")
            last_error_event = None
            last_status = 502
            auth_manager.mark_account_failure(account["id"], 502)
            if not output_started and attempt < 2:
                pending_retry_log = {
                    "account": account,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "credit": 0,
                    "status": 502,
                    "message": str(exc)[:500],
                    "started": t0,
                    "attempt": attempt,
                }
                continue
            _log_request(
                api_key_info, account, model_name, True,
                0, 0, 0, 0, "network_error", 502, str(exc)[:500], t0,
                reasoning_effort=effective_reasoning,
            )
            yield _err_sse_event(last_error, 502)
            return

        if not stop_reading:
            for data in decoder.finish():
                obj = observer.observe_event(data)
                if obj is not None and not obj.get("error"):
                    encoded = _json_sse_event(obj)
                    if pending_terminal_events or _has_terminal_choice(obj):
                        pending_terminal_events.append(encoded)
                        pending_terminal_bytes += len(encoded)
                        if pending_terminal_bytes > _MAX_SSE_EVENT_BYTES:
                            observer.parser_error = (
                                "The upstream terminal SSE events exceeded the 8 MiB limit."
                            )
                    else:
                        output_started = True
                        yield encoded
        if decoder.parser_error and not observer.seen_done:
            observer.parser_error = decoder.parser_error

        eof_error = observer.eof_error()
        if eof_error:
            last_error = (
                json.dumps(observer.upstream_error_event, ensure_ascii=False).encode("utf-8")
                if observer.upstream_error_event is not None
                else eof_error.encode("utf-8")
            )
            last_error_event = observer.upstream_error_event
            last_status = 502
            auth_manager.mark_account_failure(account["id"], 502)
            if not output_started and attempt < 2:
                pending_retry_log = {
                    "account": account,
                    "prompt_tokens": observer.usage.get("prompt_tokens", 0),
                    "completion_tokens": observer.usage.get("completion_tokens", 0),
                    "total_tokens": observer.usage.get("total_tokens", 0),
                    "credit": observer.usage.get("credit", 0),
                    "status": 502,
                    "message": eof_error,
                    "started": t0,
                    "attempt": attempt,
                }
                continue
            _log_request(
                api_key_info, account, model_name, True,
                observer.usage.get("prompt_tokens", 0),
                observer.usage.get("completion_tokens", 0),
                observer.usage.get("total_tokens", 0),
                observer.usage.get("credit", 0),
                "error", 502, eof_error, t0,
                usage=observer.usage,
                reasoning_effort=effective_reasoning,
            )
            if observer.upstream_error_event is not None:
                yield _json_sse_event(observer.upstream_error_event)
                yield b"data: [DONE]\n\n"
            else:
                yield _err_sse_event(eof_error.encode("utf-8"), 502)
            return

        missing_choices = observer.missing_finish_choices()
        synthetic_terminal = None
        if missing_choices:
            synthetic_terminal = observer.terminal_event(missing_choices)
            observer.finish_reasons.update({
                index: "tool_calls" if index in observer.tool_call_choices else "stop"
                for index in missing_choices
            })
        auth_manager.mark_account_success(account["id"])

        full_text = "".join(observer.content_parts)
        audit_blocked = _looks_like_audit_block(full_text)
        finish_reason = next((reason for reason in observer.finish_reasons.values() if reason), None)
        tool_stall = _is_tool_stall(body, finish_reason, bool(observer.tool_call_choices), full_text)
        log_finish = "content_filter" if audit_blocked else ("tool_stall" if tool_stall else (finish_reason or "stop"))
        log_error = (
            ("[audit blocked] " + full_text[:300]) if audit_blocked
            else ("[tool stall] " + full_text[:300]) if tool_stall
            else ""
        )
        _log_request(
            api_key_info, account, model_name, True,
            observer.usage.get("prompt_tokens", 0),
            observer.usage.get("completion_tokens", 0),
            observer.usage.get("total_tokens", 0),
            observer.usage.get("credit", 0),
            log_finish, 200, log_error, t0,
            usage=observer.usage,
            reasoning_effort=effective_reasoning,
        )
        if tool_stall and TOOL_STALL_FAIL_STREAM:
            # 流式已发出文本增量，无法回退重试；把本回合标记为失败，
            # 让有重试机制的客户端（DSH / OpenCode 等）自动重试。
            yield _json_sse_event({
                "error": {
                    "message": "The model finished a tool turn without calling a tool.",
                    "type": "upstream_error",
                    "code": "upstream_tool_stall",
                },
            })
            yield b"data: [DONE]\n\n"
            return
        for event in pending_terminal_events:
            yield event
        if synthetic_terminal is not None:
            yield synthetic_terminal
        yield b"data: [DONE]\n\n"
        return

    final_failure = pending_retry_log or {
        "account": last_account,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "credit": 0,
        "status": last_status,
        "message": last_error.decode("utf-8", "replace")[:500],
        "started": last_started,
    }
    _log_request(
        api_key_info, final_failure["account"], model_name, True,
        final_failure["prompt_tokens"],
        final_failure["completion_tokens"],
        final_failure["total_tokens"],
        final_failure["credit"],
        "error", final_failure["status"],
        final_failure["message"], final_failure["started"],
        reasoning_effort=effective_reasoning,
    )
    if last_error_event is not None:
        yield _json_sse_event(last_error_event)
        yield b"data: [DONE]\n\n"
    else:
        yield _err_sse_event(last_error, last_status)


async def _collect_stream(
    url: str, headers: dict, body: dict,
    account: dict, api_key_info: Optional[dict],
    model_name: str, t0: float,
) -> tuple:
    """聚合 SSE 流为单个非流式 JSON。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    try:
        async with httpx.AsyncClient(timeout=auth_manager.request_timeout(300)) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    detail = _safe_err(raw, r.status_code)
                    return ("error", (r.status_code, detail))

                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    model = chunk.get("model") or model
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning_parts.append(delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
    except httpx.HTTPError as e:
        return ("error", (502, {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}}))

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": _repair_json_arguments(v["arguments"])}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    if (
        not content_parts
        and not tool_calls
        and finish_reason not in {"length", "content_filter"}
    ):
        return (
            "error",
            (502, {
                "error": {
                    "message": "The upstream choice ended without content or a tool call.",
                    "type": "upstream_error",
                },
            }),
        )

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tcs:
        message["tool_calls"] = tcs
    result = {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or model_name,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    u = usage or {}
    effective_reasoning = (body or {}).get("reasoning_effort")
    _log_request(
        api_key_info, account, model_name, False,
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
        u.get("total_tokens", 0),
        u.get("credit", 0),
        finish_reason or "stop", 200, "", t0,
        usage=u,
        reasoning_effort=effective_reasoning,
    )
    return ("json", result)
