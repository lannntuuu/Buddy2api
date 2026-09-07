"""
proxy.py — 请求代理转发

P3 split: this module keeps the request pipeline (proxy_chat_completions,
the streaming + collector, log_request, the SSE observer/decoder) and
re-exports a stable surface for everything that was extracted.

Pipeline layers (each lives in its own module under upstream/):
  - aliases.py      — model alias table, default model list, reasoning defaults
  - moderation.py   — content-audit and tool-stall detection helpers
  - compaction.py   — request-body compaction policy and 11128 self-heal state

The aliases / moderation / compaction helpers are imported here under
their original private names so existing call sites in this file
(e.g. `if _is_tool_stall(...)`) keep working without further edits.
External code that imports from `upstream.proxy` also keeps working
because of the re-exports below.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger("buddy2api.proxy")

# ---- Backwards-compatible re-exports ----
# These modules are the canonical home for each name; we re-export
# here so callers (`from upstream import proxy; proxy._is_tool_stall(...)`,
# `proxy.DEFAULT_MODELS`, etc.) keep working after the split.
from upstream.aliases import (  # noqa: E402,F401
    DEFAULT_MODELS,
    _BUILTIN_ALIASES,
    effective_builtin_aliases,
    resolve_model_alias,
    _configured_reasoning_default,
    _env_int,
)
from upstream.moderation import (  # noqa: E402,F401
    _body_size_profile,
    _dump_11128_body,
    _looks_like_audit_block,
    _request_has_tool_loop,
    _looks_like_stall_text,
    _is_tool_stall,
    TOOL_STALL_RETRY,
    TOOL_STALL_FAIL_STREAM,
)
from upstream.compaction import (  # noqa: E402,F401
    compaction_stats,
    _is_11128_error,
    _arm_channel,
    _channel_armed,
    _record_11128_retry,
    _smart_compact_messages,
    _compact_text,
    _compact_tools,
    _compact_schema_descriptions,
)

from storage import database as db
from accounts import auth_manager
from providers import model_limits
from providers.store_common import (
    credit_source_of,
    enqueue_record_request,
    extract_cache_tokens,
)


class ModelLimitError(Exception):
    """输入预检超限时由 _apply_model_limits 抛出，proxy 转换为 400 错误响应。"""

    def __init__(self, status: int, detail: dict):
        super().__init__(detail)
        self.status = status
        self.detail = detail

BACKEND = "https://copilot.tencent.com"
# 共享 retry.py 的瞬时错误集合;401/403 仅参与账号 failover 判定(_is_retryable_status),
# 不参与同账号重试。
from providers.retry import RETRYABLE_STATUS, retry_delay as _retry_delay  # noqa: E402

RETRYABLE_STATUS_CODES = RETRYABLE_STATUS | {401, 403}

def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS_CODES


def _parse_retry_after(value) -> float | None:
    """Retry-After 头的纯数字秒解析；缺失/非数字/非法值返回 None。

    只接受纯数字秒（HTTP-date 不支持）；NaN/负数/inf 一律视为缺失。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if not (seconds >= 0) or seconds == float("inf"):
        return None
    return seconds

# 进程级长寿命上游客户端(keep-alive 复用,降低每次转发的 TCP+TLS 建连成本)。
# 与 openai_compat._get_client / storage.http_pool 同模式:按"当前事件循环"绑定,
# 单 loop 生产环境全程复用;测试里每个 asyncio.run 是新 loop,自动重建,
# 从而每条用例的 httpx.AsyncClient 全局 fake 都能被重新拾取,不跨用例串味。
# 不直接复用 storage.http_pool:该池的 is_closed 探测对测试注入的无 is_closed
# fake 会 AttributeError,且会把 fake 缓存进全局池。
_upstream_client: httpx.AsyncClient | None = None
_upstream_client_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> httpx.AsyncClient:
    global _upstream_client, _upstream_client_loop
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if (
        _upstream_client is None
        or getattr(_upstream_client, "is_closed", False)
        or (loop is not None and _upstream_client_loop is not loop)
    ):
        _upstream_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=64,
                max_keepalive_connections=32,
                keepalive_expiry=60.0,
            ),
            # 每次请求经 client.stream(..., timeout=...) 传入具体超时
            timeout=httpx.Timeout(60.0),
        )
        _upstream_client_loop = loop
    return _upstream_client


@asynccontextmanager
async def _shared_client_cm():
    """Yield the shared long-lived upstream client without closing it.

    Replaces the old per-request `async with httpx.AsyncClient(...)` so
    connections are reused across requests and retry attempts. Per-request
    timeouts are passed at the `client.stream(...)` call sites instead.
    """
    yield _get_client()

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


def _apply_model_limits(body: dict, raw_model: str, payload: dict) -> None:
    """模型上下文限额强制（spec §2.4）。

    在 resolve_model_alias 之后、正式发往上游客户端之前调用：
    1. 输入预检：估算输入 token > 生效 max_input → 抛 ModelLimitError（400）。
    2. max_tokens 注入：客户端未传 → min(default_max_output, max_input-估算)
       （<1 不注入）；客户端显式传 → 尊重，但超剩余空间则 clamp + warning。
    3. enforce=false → 整体跳过。
    """
    if not model_limits.get_enforce():
        return

    resolved_model = body.get("model", raw_model)
    # 请求链路按绑定通道解析：workbuddy 等走 proxy 的通道由调用方经 payload 标注；
    # 这里用通用键（管理端统一前缀），缺省落 workbuddy（proxy 主链路）。
    channel = payload.get("_bind_channel") or "workbuddy"

    max_input = model_limits.resolve_max_input_tokens(channel, resolved_model)
    if max_input is None:
        # 显式不限制：跳过预检，仅按开关对待 max_tokens（不 clamp）
        max_input = float("inf")

    est = model_limits.estimate_input_tokens(body.get("messages"))

    # 1. 输入预检
    if est > max_input:
        raise ModelLimitError(
            400,
            {
                "error": {
                    "message": (
                        f"input exceeds {resolved_model} max input context "
                        f"(≈{est} > {max_input})"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    # 2. max_tokens 注入 / clamp
    max_output = model_limits.get_default_max_output_tokens(channel)
    remaining = max_input - est  # 超限已被上面拦截，这里 remaining >= 0
    if "max_tokens" not in body or body.get("max_tokens") is None:
        injected = min(max_output, remaining)
        if injected >= 1:
            body["max_tokens"] = int(injected)
        # <1 不注入（避免注入 0/负数导致上游报错）
        return

    # 客户端显式传：尊重，但超剩余空间则 clamp + warning
    client_max = body["max_tokens"]
    try:
        client_max = int(client_max)
    except (TypeError, ValueError):
        return
    if client_max > remaining:
        logger.warning(
            "max_tokens=%s 超过 %s 剩余空间(估算输入=%s, max_input=%s)，"
            "clamp 到 %s", client_max, resolved_model, est, max_input, int(remaining),
        )
        if remaining >= 1:
            body["max_tokens"] = int(remaining)
        else:
            # 连 1 个输出 token 都不剩：移除该字段，交由上游决定如何处理
            body.pop("max_tokens", None)


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
    # 模型上下文限额强制（§2.4）：输入预检超限→抛 400；未传 max_tokens 注入合理值；
    # 显式值超剩余空间 clamp+warning。enforce=false 时整体跳过。
    _apply_model_limits(body, raw_model, payload)
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




def _has_terminal_choice(payload: dict) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, dict) and bool(choice.get("finish_reason"))
        for choice in choices
    )


from upstream.sse import SSEDecoder, _MAX_EVENT_BYTES as _MAX_SSE_EVENT_BYTES  # noqa: E402
from upstream.chat_grammar import (  # noqa: E402,F401
    ChatStreamObserver as _ChatStreamObserver,
    _json_sse_event,
    _repair_json_arguments,
)




_SSEEventDecoder = SSEDecoder


_extract_cache_tokens = extract_cache_tokens

# 上游各模型的原生默认思考档位（未显式注入 reasoning_effort 时的真实档位）。
# 来源：docs/design/per-model-reasoning-effort.md §2 探针实测（2026-02-25）：
#   deepseek-v4-flash / glm-5.2 / auto 默认不思考（0 reasoning tokens）→ none；
#   kimi-k2.7 默认轻思考（44 tok）→ minimal；deepseek-v4-pro 与 flash 同系 → none。
# 未知模型 → "upstream"（前端显示"上游默认"）。
_UPSTREAM_DEFAULT_REASONING = {
    "deepseek-v4-pro": "none",
    "deepseek-v4-flash": "none",
    "glm-5.2": "none",
    "auto": "none",
    "kimi-k2.7": "minimal",
}


def _log_request(api_key_info, account, model_name, stream,
                  prompt_t, completion_t, total_t, credit,
                  finish_reason, status_code, error_msg, t0,
                  increment_usage: bool = True,
                  usage: dict | None = None,
                  reasoning_effort: str | None = None,
                  first_token_ms: int | None = None):
    elapsed_ms = int((time.time() - t0) * 1000)
    if not reasoning_effort:
        reasoning_effort = _UPSTREAM_DEFAULT_REASONING.get(model_name, "upstream")
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
        # 请求起点秒级时间戳(入队时携带,落库层缺省用当前时刻)
        "created_at": int(t0),
        # 流式首个内容帧毫秒数;retry / eof / 错误行由调用方保持缺省 None
        "first_token_ms": first_token_ms,
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
    log_data["credit_source"] = credit_source_of(usage)
    # 写日志（含 BEGIN IMMEDIATE 事务 + fsync）不占事件循环：
    # 放进默认线程池 fire-and-forget，日志失败只静默丢弃。
    enqueue_record_request(log_data)


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
    try:
        body = build_backend_body(payload)
    except ModelLimitError as err:
        # 输入预检超限：直接以 400 错误响应（不进入账号选择 / 上游转发）
        return ("error", (err.status, err.detail))
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
            _record_11128_retry()
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


class _RetryLog:
    """流式重试的延迟落库载体（_stream_upstream 私有）。

    收敛 4 处 pending_retry_log 字面量构造，字段与原字面量逐键一致：
    在下一次账号轮换前（或循环收尾）才写 "retry"/"error" 日志行。
    """

    __slots__ = (
        "account", "prompt_tokens", "completion_tokens", "total_tokens",
        "credit", "status", "message", "started", "attempt", "retry_after",
    )

    def __init__(self, account, status, message, started,
                 attempt=None, retry_after=None,
                 prompt_tokens=0, completion_tokens=0, total_tokens=0, credit=0):
        self.account = account
        self.status = status
        self.message = message
        self.started = started
        self.attempt = attempt
        self.retry_after = retry_after
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.credit = credit


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
    # first_token_ms 基线取账号轮换/重试循环之前（= 用户真实等待，含 pick/
    # refresh/退避）；重试或换号不重置起点。
    request_t0 = time.monotonic()
    first_token_ms: int | None = None

    for attempt in range(3):
        account = await auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            break
        channel = account.get("provider") or "workbuddy"
        if pending_retry_log is not None:
            _log_request(
                api_key_info,
                pending_retry_log.account,
                model_name,
                True,
                pending_retry_log.prompt_tokens,
                pending_retry_log.completion_tokens,
                pending_retry_log.total_tokens,
                pending_retry_log.credit,
                "retry",
                pending_retry_log.status,
                pending_retry_log.message,
                pending_retry_log.started,
                increment_usage=False,
                reasoning_effort=effective_reasoning,
            )
            if pending_retry_log.retry_after is not None:
                await _retry_delay(
                    pending_retry_log.attempt, retry_after=pending_retry_log.retry_after
                )
            else:
                await _retry_delay(pending_retry_log.attempt)
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

        # feed / finish 共用的同构事件泵：处理 SSE 事件、缓存 terminal 帧
        # 延后下发、产出非 terminal 帧。output_started / first_token_ms 打点
        # 位置与拆分前逐行一致（出流前才翻转，首个内容帧记一次基线差）。
        async def _pump(events):
            nonlocal output_started, pending_terminal_bytes, first_token_ms
            for data in events:
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
                        if first_token_ms is None:
                            first_token_ms = int((time.monotonic() - request_t0) * 1000)
                        yield encoded

        try:
            timeout = httpx.Timeout(
                connect=10,
                read=auth_manager.request_timeout(300),
                write=30,
                pool=10,
            )
            async with _shared_client_cm() as client:
                async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as response:
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
                            _record_11128_retry()
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
                            pending_retry_log = _RetryLog(
                                account=account,
                                status=response.status_code,
                                message=raw_error.decode("utf-8", "replace")[:500],
                                started=t0,
                                attempt=attempt,
                                # 429 等响应可能带 Retry-After(纯数字秒):
                                # 存入 pending_retry_log,在重试前透传给 retry_delay
                                retry_after=_parse_retry_after(
                                    response.headers.get("retry-after")
                                ),
                            )
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
                        async for encoded in _pump(decoder.feed(chunk)):
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
                pending_retry_log = _RetryLog(
                    account=account,
                    status=502,
                    message=str(exc)[:500],
                    started=t0,
                    attempt=attempt,
                )
                continue
            _log_request(
                api_key_info, account, model_name, True,
                0, 0, 0, 0, "network_error", 502, str(exc)[:500], t0,
                reasoning_effort=effective_reasoning,
            )
            yield _err_sse_event(last_error, 502)
            return

        if not stop_reading:
            async for encoded in _pump(decoder.finish()):
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
            # eof 分类（WS-1 §1.2）：已经向客户端出流后的 eof 不再降分、不再
            # 跨账号重试（换号也无法撤回已发出的增量），按现状记 error 日志并
            # 把已收内容/错误事件透传收尾；未出流的 eof 维持 mark + 重试。
            if not output_started:
                auth_manager.mark_account_failure(account["id"], 502)
                if attempt < 2:
                    pending_retry_log = _RetryLog(
                        account=account,
                        status=502,
                        message=eof_error,
                        started=t0,
                        attempt=attempt,
                        prompt_tokens=observer.usage.get("prompt_tokens", 0),
                        completion_tokens=observer.usage.get("completion_tokens", 0),
                        total_tokens=observer.usage.get("total_tokens", 0),
                        credit=observer.usage.get("credit", 0),
                    )
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
            first_token_ms=first_token_ms,
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

    final_failure = pending_retry_log or _RetryLog(
        account=last_account,
        status=last_status,
        message=last_error.decode("utf-8", "replace")[:500],
        started=last_started,
    )
    _log_request(
        api_key_info, final_failure.account, model_name, True,
        final_failure.prompt_tokens,
        final_failure.completion_tokens,
        final_failure.total_tokens,
        final_failure.credit,
        "error", final_failure.status,
        final_failure.message, final_failure.started,
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
        async with _shared_client_cm() as c:
            async with c.stream("POST", url, headers=headers, json=body, timeout=auth_manager.request_timeout(300)) as r:
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
