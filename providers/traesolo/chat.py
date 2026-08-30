"""SOLO chat：OpenAI ↔ SOLO 协议转换 + 账号轮换 + 冷却状态机。

移植自 Go 版 trae2api-web（internal/upstream/{payload,solosse,client}.go
与 internal/pool/pool.go 的调度语义）：

  payload  单 pass 改写：messages content 字符串→数组、stream 强制 true、
           function=solo_work_lite、model→config_name、tools/tool_choice 归一化
  SSE      SOLO 自定义事件（metadata/output/extra_info/token_usage/done/error）
           → OpenAI SSE chunk（含 reasoning_content / tool_calls / usage）
  调度     1005→12h、429/404→60s、连续 3 错→10min、401→失效、token 24h 预刷新
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
import uuid
from typing import AsyncGenerator, Optional

import httpx

from accounts import auth_manager
from storage import database as db
from providers.model_config import channel_aliases, channel_model_ids, channel_credit_rate
from providers.traesolo.constants import (
    AGENT_HOST,
    CHANNEL_ID,
    DEFAULT_CONFIG,
    DYNAMIC_MODELS_TTL,
    ERR_COOLDOWN_S,
    ERR_THRESHOLD,
    EP_CHAT,
    EP_MODELS,
    FUNCTION,
    MAX_ROTATE,
    MODELS_FAIL_COOLDOWN,
    PLAN_COOLDOWN_S,
    REFRESH_SKEW_S,
    SOFT_COOLDOWN_S,
    STATIC_MODELS,
    ALIASES,
)
from providers.traesolo.token import (
    TraeSoloAuthError,
    needs_pre_refresh,
    refresh_account,
    solo_headers,
)

# 测试注入点：模块级 MockTransport（tests/test_traesolo.py 设置）。
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


def _make_client(timeout: Optional[float], stream: bool = False) -> httpx.AsyncClient:
    if stream:
        # 长流不设总超时/读超时，仅限制首字节（对齐 Go ResponseHeaderTimeout）。
        timeout_obj = httpx.Timeout(None, connect=10.0, read=None)
    else:
        timeout_obj = timeout if timeout else 120.0
    return httpx.AsyncClient(timeout=timeout_obj, transport=_TRANSPORT)


async def _aclose_client(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 错误分类（port of upstream.Classify）
# ---------------------------------------------------------------------------

def classify(status: int, body: str) -> str:
    """按 HTTP 状态码 + body 判定错误类别。"""
    lower = (body or "").lower()
    if '"code":1005' in (body or "") or ("1005" in (body or "") and "plan" in lower):
        return "plan_limit"
    if status == 401:
        return "session_dead"
    if status == 429:
        return "soft_rate"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "server"
    if status >= 400:
        return "client"
    return "none"


class SoloStreamError(RuntimeError):
    """上游 SSE 流内业务错误（event:error）。"""

    def __init__(self, code: int, msg: str):
        self.code = int(code or 0)
        self.msg = msg or ""
        super().__init__(f"solo error code={self.code} msg={self.msg}")

    def kind(self) -> str:
        return "plan_limit" if self.code == 1005 else "client"


# ---------------------------------------------------------------------------
# payload 改写（port of upstream.PrepareBody）
# ---------------------------------------------------------------------------

def normalize_model_name(s: str) -> str:
    """deepseek_v4_pro → DeepSeek-V4-Pro（对齐 Go normalizeModelName）。"""
    parts = [p for p in str(s or "").split("_")]
    return "-".join(p[:1].upper() + p[1:].lower() if p else p for p in parts)


def _known_case_insensitive(value: str, ids: list[str]) -> bool:
    """大小写不敏感回退（Go 注释声明的意图，实现上比 title-case 归一化更宽容）。"""
    lowered = normalize_model_name(value).lower()
    return any(str(item).lower() == lowered for item in ids)


def _normalize_tool_choice(obj: dict) -> None:
    if "tool_choice" not in obj:
        return

    def suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    tc = obj["tool_choice"]
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            suppress()
    elif isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function")
            name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
            if not name:
                name = str(tc.get("name") or "")
            name = name.strip()
            obj["tool_choice"] = name if name else "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)


def _normalize_tools(obj: dict) -> None:
    """OpenAI tools → SOLO 上游格式：parameters 对象序列化为 JSON 字符串。"""
    if "tools" not in obj:
        return
    raw = obj["tools"]
    if not isinstance(raw, list) or not raw:
        return
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        item = dict(item)
        fn = dict(fn)
        params = fn.get("parameters")
        if isinstance(params, dict):
            try:
                fn["parameters"] = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                pass
        item["function"] = fn
        out.append(item)
    if out:
        obj["tools"] = out
    else:
        obj.pop("tools", None)


def prepare_body(payload: dict) -> dict:
    """OpenAI 请求体 → SOLO llm_utils_chat 请求体（单 pass 改写）。"""
    obj = dict(payload or {})
    obj["stream"] = True
    obj["function"] = FUNCTION

    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            new_m = dict(m)
            role = new_m.get("role")
            # assistant 消息回传 tool_calls: OpenAI function → 上游 function_call
            if role == "assistant" and isinstance(new_m.get("tool_calls"), list):
                kept = []
                for tc in new_m["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    tc = dict(tc)
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        tc["function_call"] = dict(fn)
                        tc.pop("function", None)
                    fc = tc.get("function_call")
                    if isinstance(fc, dict) and not str(fc.get("name") or "").strip():
                        continue  # 上游要求 FunctionCall.Name 必填
                    kept.append(tc)
                if kept:
                    new_m["tool_calls"] = kept
                else:
                    new_m.pop("tool_calls", None)
            content = new_m.get("content")
            if isinstance(content, str):
                new_m["content"] = [{"type": "text", "text": content}]
            # 已是数组 → 透传（兼容多模态，保守处理）
            msgs[i] = new_m
        obj["messages"] = msgs

    model = str(obj.get("model") or "").strip() or DEFAULT_CONFIG
    obj["config_name"] = model
    obj["model"] = model
    _normalize_tool_choice(obj)
    _normalize_tools(obj)
    return obj


# ---------------------------------------------------------------------------
# SOLO SSE 解析 / 转换（port of upstream.solosse.go）
# ---------------------------------------------------------------------------

def parse_solo_line(event_name: str, data_line: str) -> Optional[dict]:
    """解析一条 SOLO 事件（event 行值 + data 行值），归一化字段。"""
    ev: dict = {"event": (event_name or "").strip()}
    if not data_line:
        return ev
    try:
        raw = json.loads(data_line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    name = ev["event"]
    if name == "output":
        if isinstance(raw.get("response"), str):
            ev["response"] = raw["response"]
        if isinstance(raw.get("reasoning_content"), str):
            ev["reasoning_content"] = raw["reasoning_content"]
        if "tool_calls" in raw:
            ev["tool_calls"] = raw["tool_calls"]
    elif name == "token_usage":
        ev["usage"] = raw
    elif name == "done":
        if isinstance(raw.get("finish_reason"), str):
            ev["finish_reason"] = raw["finish_reason"]
    elif name == "error":
        ev["error_code"] = raw.get("code")
        ev["error_message"] = raw.get("message")
    return ev


class _SSEState:
    """维护 SSE 一行事件跨行累积（event/data）。"""

    def __init__(self) -> None:
        self.event = ""
        self.data: list[str] = []

    def feed(self, line: str) -> Optional[dict]:
        if line == "":
            if not self.event:
                self._reset()
                return None
            ev = parse_solo_line(self.event, "".join(self.data))
            self._reset()
            return ev
        if line.startswith("event:"):
            self.event = line[6:].strip()
        elif line.startswith("data:"):
            self.data.append(line[5:])
        # 注释行（: 开头）等忽略
        return None

    def _reset(self) -> None:
        self.event = ""
        self.data = []


def _merge_tool_call_delta(merged: dict, delta: dict) -> None:
    """流式 tool_call 片段合并：id/type/name 覆盖，arguments 拼接。

    上游 SOLO 用 function_call 字段，OpenAI 标准用 function，两者都兼容。
    """
    v = delta.get("id")
    if isinstance(v, str) and v:
        merged["id"] = v
    v = delta.get("type")
    if isinstance(v, str) and v:
        merged["type"] = v
    df = delta.get("function")
    if not isinstance(df, dict):
        df = delta.get("function_call")
    if not isinstance(df, dict):
        return
    df = dict(df)
    df.pop("namespace", None)
    df.pop("partial_arguments", None)
    mf = merged.get("function")
    if not isinstance(mf, dict):
        mf = {}
        merged["function"] = mf
    v = df.get("name")
    if isinstance(v, str) and v:
        mf["name"] = v
    v = df.get("arguments")
    if isinstance(v, str) and v:
        prev = mf.get("arguments")
        mf["arguments"] = (prev if isinstance(prev, str) else "") + v


def _merge_tool_calls(tool_calls: dict[int, dict], tool_order: list[int], raw) -> None:
    """把 output.tool_calls（可能 null/对象/数组）按 index 合并进累计对象。"""
    if raw is None:
        return
    if isinstance(raw, dict):
        arr = [raw]
    elif isinstance(raw, list):
        arr = raw
    else:
        return
    for call in arr:
        if not isinstance(call, dict):
            continue
        idx = 0
        v = call.get("index")
        if isinstance(v, (int, float)):
            idx = int(v)
        merged = tool_calls.get(idx)
        if merged is None:
            merged = {"index": idx}
            tool_calls[idx] = merged
            tool_order.append(idx)
        _merge_tool_call_delta(merged, call)


def _clean_tool_calls(raw) -> list[dict] | None:
    """流式 chunk 里的 tool_calls：function_call→function，清理 SOLO 专属字段。"""
    if raw is None:
        return None
    arr = [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else None)
    if arr is None:
        return None
    cleaned = []
    for call in arr:
        if not isinstance(call, dict):
            continue
        call = dict(call)
        fc = call.get("function_call")
        if isinstance(fc, dict):
            call["function"] = dict(fc)
            call.pop("function_call", None)
        fn = call.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            fn.pop("namespace", None)
            fn.pop("partial_arguments", None)
            call["function"] = fn
        cleaned.append(call)
    return cleaned or None


def _new_id() -> str:
    return f"chatcmpl-{int(time.time() * 1_000_000)}-{secrets.token_hex(4)}"


async def aggregate_lines(lines: AsyncGenerator[str, None]) -> tuple[Optional[dict], Optional[SoloStreamError]]:
    """读完整 SOLO SSE，聚合为单个 OpenAI chat.completion（非流式）。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = "stop"
    usage: dict | None = None
    tool_calls: dict[int, dict] = {}
    tool_order: list[int] = []
    upstream_err: SoloStreamError | None = None
    st = _SSEState()
    async for raw_line in lines:
        line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else raw_line
        ev = st.feed(line.rstrip("\r\n"))
        if ev is None:
            continue
        name = ev.get("event")
        if name == "output":
            if ev.get("response"):
                content_parts.append(ev["response"])
            if ev.get("reasoning_content"):
                reasoning_parts.append(ev["reasoning_content"])
            if "tool_calls" in ev:
                _merge_tool_calls(tool_calls, tool_order, ev["tool_calls"])
        elif name == "token_usage":
            usage = ev.get("usage")
        elif name == "done":
            if ev.get("finish_reason"):
                finish_reason = ev["finish_reason"]
        elif name == "error":
            upstream_err = SoloStreamError(ev.get("error_code") or 0, ev.get("error_message") or "")
    if upstream_err is not None:
        return None, upstream_err
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_order:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_order)]
    resp = {
        "id": _new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        resp["usage"] = usage
    return resp, None


async def stream_to_openai(
    lines: AsyncGenerator[str, None],
    chunk_id: str,
    model: str,
    on_error=None,
    usage_sink: dict | None = None,
) -> AsyncGenerator[str, None]:
    """流式转换：SOLO SSE → OpenAI SSE chunk（每 chunk 独立 yield）。

    保证至少一个 [DONE]；上游 event:error 时回调 on_error 并注入 error 事件。
    """
    created = int(time.time())
    st = _SSEState()
    pending_usage: dict | None = None
    saw_done = False

    def chunk_sse(delta: dict, finish: str | None = None) -> str:
        nonlocal pending_usage
        choice: dict = {"index": 0, "delta": delta}
        if finish is not None:
            choice["finish_reason"] = finish
        body = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [choice],
        }
        if pending_usage is not None:
            body["usage"] = pending_usage
            pending_usage = None
        return "data: " + json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n\n"

    async for raw_line in lines:
        line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else raw_line
        ev = st.feed(line.rstrip("\r\n"))
        if ev is None:
            continue
        name = ev.get("event")
        if name == "output":
            delta: dict = {}
            if ev.get("response"):
                delta["content"] = ev["response"]
            if ev.get("reasoning_content"):
                delta["reasoning_content"] = ev["reasoning_content"]
            if "tool_calls" in ev:
                cleaned = _clean_tool_calls(ev["tool_calls"])
                if cleaned:
                    delta["tool_calls"] = cleaned
            if delta:
                yield chunk_sse(delta, None)
        elif name == "token_usage":
            pending_usage = ev.get("usage")
            if usage_sink is not None:
                usage_sink["usage"] = pending_usage
        elif name == "done":
            yield chunk_sse({}, ev.get("finish_reason") or "stop")
            yield "data: [DONE]\n\n"
            saw_done = True
        elif name == "error":
            se = SoloStreamError(ev.get("error_code") or 0, ev.get("error_message") or "")
            if on_error is not None:
                on_error(se)
            msg = json.dumps(f"solo error code={se.code} msg={se.msg}", ensure_ascii=False)
            yield f"event: error\ndata: {msg}\n\n"
            yield "data: [DONE]\n\n"
            saw_done = True
    if not saw_done:
        # 幂等兜底：上游中断（无 done）仍写 [DONE]。
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 冷却状态机（port of Go pool）
# ---------------------------------------------------------------------------

class _Pool:
    """内存冷却状态机（重启清空，与项目其他通道的失败追踪一致）。

    plan   1005 权益不足 → 12h 硬冷却
    soft   429/404      → 60s 短冷却
    err    连续 N 次错误 → 10min 冷却（计数清零）
    session dead 401    → 账号标记 expired（可经刷新复活）
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[int, dict] = {}

    def cooling(self, aid: int) -> bool:
        with self._lock:
            state = self._state.get(aid)
            if not state:
                return False
            if state["until"] <= time.monotonic():
                self._state.pop(aid, None)
                return False
            return True

    def cooldown(self, aid: int, kind: str, seconds: float, reason: str = "") -> None:
        with self._lock:
            self._state[aid] = {
                "kind": kind,
                "until": time.monotonic() + seconds,
                "reason": reason,
                "err_count": 0,
            }

    def note_error(self, aid: int) -> bool:
        """累计一次错误；达到阈值返回 True（调用方无需再处理）。"""
        with self._lock:
            state = self._state.get(aid) or {
                "kind": "err",
                "until": 0.0,
                "reason": "",
                "err_count": 0,
            }
            state["err_count"] = int(state.get("err_count") or 0) + 1
            if state["err_count"] >= ERR_THRESHOLD:
                state["until"] = time.monotonic() + ERR_COOLDOWN_S
                state["reason"] = state.get("reason") or f"{ERR_THRESHOLD} consecutive errors"
                state["err_count"] = 0
                self._state[aid] = state
                return True
            self._state[aid] = state
            return False

    def note_success(self, aid: int) -> None:
        with self._lock:
            self._state.pop(aid, None)

    def disable(self, aid: int, reason: str = "") -> None:
        """session dead：标记账号 expired 并进入长冷却。"""
        with self._lock:
            self._state[aid] = {
                "kind": "session_dead",
                "until": time.monotonic() + PLAN_COOLDOWN_S,
                "reason": reason,
                "err_count": 0,
            }
        try:
            db.update_account(aid, {"status": "expired"})
        except Exception:
            pass

    def info(self, aid: int) -> Optional[dict]:
        with self._lock:
            state = self._state.get(aid)
            if not state:
                return None
            remaining = max(0.0, state["until"] - time.monotonic())
            return {
                "kind": state.get("kind"),
                "reason": state.get("reason") or "",
                "remaining_s": int(remaining),
                "cooling": remaining > 0,
            }


pool = _Pool()


def _handle_kind(aid: int, kind: str, reason: str = "") -> None:
    """把分类结果落到冷却状态机 + 项目通用失败追踪。"""
    if kind == "plan_limit":
        pool.cooldown(aid, "plan", PLAN_COOLDOWN_S, reason or "plan 权益不足")
    elif kind in ("soft_rate", "not_found"):
        pool.cooldown(aid, "soft", SOFT_COOLDOWN_S, reason or f"upstream {kind}")
        auth_manager.mark_account_failure(aid, 429 if kind == "soft_rate" else 404)
    elif kind == "session_dead":
        pool.disable(aid, reason or "session dead")
        auth_manager.mark_account_failure(aid, 401)
    else:
        if pool.note_error(aid):
            auth_manager.mark_account_failure(aid, 502)


async def _pick(tried: set[int]) -> dict | None:
    """选账号：项目路由 + SOLO 冷却过滤 + 过期账号刷新兜底。"""
    for _ in range(8):  # 上限保护，防无限循环
        account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
        if account is None:
            break
        aid = int(account["id"])
        if not pool.cooling(aid):
            return account
        tried.add(aid)
    expired = [
        row
        for row in db.list_accounts(provider=CHANNEL_ID)
        if row.get("status") == "expired"
        and int(row.get("id") or 0) not in tried
        and not pool.cooling(int(row.get("id") or 0))
    ]
    for row in expired:
        try:
            return await refresh_account(row)
        except Exception:
            continue
    return None


async def _pre_refresh(account: dict) -> tuple[bool, str, Optional[Exception]]:
    """token 24h 预刷新。返回 (refreshed, err_kind, error)。"""
    if not needs_pre_refresh(account):
        return False, "", None
    try:
        fresh = await refresh_account(account)
    except TraeSoloAuthError as exc:
        kind = "session_dead" if exc.kind == "session_dead" else "client"
        return False, kind, exc
    except httpx.HTTPError as exc:
        # 刷新是真实网络请求；超时/断连按客户端错误降级，不能穿透成 500
        return False, "client", exc
    for key in ("access_token", "refresh_token", "expires_at", "refresh_expires_at"):
        value = fresh.get(key)
        if value not in (None, ""):
            account[key] = value
    return True, "", None


# ---------------------------------------------------------------------------
# 动态模型表（port of handler.fetchDynamicModels）
# ---------------------------------------------------------------------------

class _ModelCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ids: list[str] = []
        self.fetched_at: float = 0.0
        self.last_fail_at: float = 0.0


_model_cache = _ModelCache()


async def fetch_models(account: dict) -> list[str]:
    """拉 SOLO 模型表（get_detail_param），返回 config_name 列表。"""
    body = {
        "function": FUNCTION,
        "config_names": None,
        "need_prompt": False,
        "current_config_info": None,
        "poly_prompt": True,
        "mode_type": None,
        "agent_type": None,
    }
    client = _make_client(30.0)
    try:
        response = await client.post(
            f"{AGENT_HOST}{EP_MODELS}", headers=solo_headers(account, stream=False), json=body
        )
    finally:
        await _aclose_client(client)
    if response.status_code >= 400:
        raise TraeSoloAuthError(f"models HTTP {response.status_code}", status=response.status_code)
    try:
        data = response.json()
    except ValueError as exc:
        raise TraeSoloAuthError("models returned non-JSON") from exc
    out: list[str] = []
    for cfg in data.get("config_info_list") or []:
        if not isinstance(cfg, dict):
            continue
        name = str(cfg.get("config_name") or "").strip()
        if name and name not in out:
            out.append(name)
    if not out:
        raise TraeSoloAuthError("models api returned empty list")
    return out


async def refresh_dynamic_models(force: bool = False) -> bool:
    """动态拉模型（任一可用账号），成功缓存 1h / 失败负缓存 5min。best-effort。"""
    now = time.time()
    with _model_cache.lock:
        if not force and _model_cache.ids and now - _model_cache.fetched_at < DYNAMIC_MODELS_TTL:
            return True
        if _model_cache.last_fail_at and now - _model_cache.last_fail_at < MODELS_FAIL_COOLDOWN:
            return False
    account = auth_manager.pick_account(None, provider=CHANNEL_ID)
    if account is None:
        with _model_cache.lock:
            _model_cache.last_fail_at = now
        return False
    try:
        ids = await fetch_models(account)
    except Exception:
        with _model_cache.lock:
            _model_cache.last_fail_at = now
        return False
    if not ids:
        with _model_cache.lock:
            _model_cache.last_fail_at = now
        return False
    with _model_cache.lock:
        _model_cache.ids = ids
        _model_cache.fetched_at = now
        _model_cache.last_fail_at = 0.0
    return True


def dynamic_model_ids() -> list[str]:
    with _model_cache.lock:
        if _model_cache.ids and time.time() - _model_cache.fetched_at < DYNAMIC_MODELS_TTL:
            return list(_model_cache.ids)
    return []


_dynamic_task: Optional[asyncio.Task] = None
_dynamic_task_guard = threading.Lock()


def kick_dynamic_models() -> Optional[asyncio.Task]:
    """best-effort 触发动态模型表刷新（TTL 内只触发一次，非阻塞）。

    对齐 Go 版「首次请求时拉模型表」的行为；失败静默回退静态表。
    """
    global _dynamic_task
    with _dynamic_task_guard:
        if _dynamic_task is not None and not _dynamic_task.done():
            return _dynamic_task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(refresh_dynamic_models())
        _dynamic_task = task

        def _done(_t: asyncio.Task) -> None:
            with _dynamic_task_guard:
                try:
                    _t.exception()
                except asyncio.CancelledError:
                    pass

        task.add_done_callback(_done)
        return task


# ---------------------------------------------------------------------------
# 模型映射（port of handler.mapModel / knownModel）
# ---------------------------------------------------------------------------

def _effective_default_ids() -> list[str]:
    return dynamic_model_ids() or list(STATIC_MODELS)


def effective_model_ids() -> list[str]:
    """当前生效模型白名单：用户自定义 > 动态表 > 内置静态表。"""
    return channel_model_ids(CHANNEL_ID, _effective_default_ids())


def translate_model(model: str) -> str:
    inner = (model or "auto").strip() or "auto"
    return channel_aliases(CHANNEL_ID, ALIASES).get(inner, inner)


def accepts_model(inner: str) -> bool:
    value = (inner or "").strip()
    if not value or value == "auto":
        return True
    if value in channel_aliases(CHANNEL_ID, ALIASES):
        return True
    base = value.split("__", 1)[0]  # 去内部名后缀（__dev / __max 等）
    ids = effective_model_ids()
    if base in ids:
        return True
    if normalize_model_name(base) in ids:
        return True
    return _known_case_insensitive(base, ids)


# ---------------------------------------------------------------------------
# 上游调用
# ---------------------------------------------------------------------------

async def _open(account: dict, body: dict) -> tuple[httpx.AsyncClient, httpx.Response]:
    """打开 llm_utils_chat SSE 流。返回 (client, resp)；调用方负责关闭。"""
    client = _make_client(None, stream=True)
    headers = solo_headers(account, stream=True)
    content = json.dumps(body, ensure_ascii=False)
    request = client.build_request("POST", f"{AGENT_HOST}{EP_CHAT}", headers=headers, content=content)
    response = await client.send(request, stream=True)
    return client, response


async def _close(client: httpx.AsyncClient, response: httpx.Response) -> None:
    try:
        await response.aclose()
    except Exception:
        pass
    await _aclose_client(client)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _log(
    api_key_info: dict | None,
    account: dict | None,
    model_name: str,
    stream: bool,
    finish: str,
    status: int,
    error: str,
    t0: float,
    usage: dict | None = None,
) -> None:
    prompt = completion = total = 0
    if isinstance(usage, dict):
        try:
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or 0) or (prompt + completion)
        except (TypeError, ValueError):
            prompt = completion = total = 0
    # 上游不回报 credit，用 token→credit 换算率做**近似**消耗统计（可配，0=不估算）。
    rate = channel_credit_rate(CHANNEL_ID)
    credit = round(total / rate, 6) if rate else 0
    try:
        db.record_request(
            {
                "api_key_id": api_key_info["id"] if api_key_info else None,
                "api_key_name": api_key_info["name"] if api_key_info else None,
                "account_id": account["id"] if account else None,
                "account_name": account.get("name") if account else None,
                "provider": CHANNEL_ID,
                "model": model_name,
                "stream": 1 if stream else 0,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "credit": credit,
                "finish_reason": finish,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status,
                "error_msg": error,
                "increment_usage": True,
            }
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 非流式
# ---------------------------------------------------------------------------

async def _run_once(
    payload: dict, model: str, client_model: str, api_key_info: dict | None
) -> tuple:
    """账号重试循环（非流式）。返回 ("json", body) 或 ("error", (status, detail))。"""
    tried: set[int] = set()
    last_error = "No available accounts"
    for _ in range(MAX_ROTATE):
        account = await _pick(tried)
        if account is None:
            break
        aid = int(account["id"])
        tried.add(aid)
        t0 = time.time()

        _, rkind, rerr = await _pre_refresh(account)
        if rerr is not None:
            _handle_kind(aid, rkind, f"refresh: {rerr}")
            last_error = str(rerr)
            _log(api_key_info, account, client_model, False, "error", 503, str(rerr)[:240], t0)
            continue

        body = prepare_body(payload)
        try:
            client, response = await _open(account, body)
        except httpx.HTTPError as exc:
            pool.note_error(aid)
            last_error = str(exc)
            _log(api_key_info, account, client_model, False, "error", 502, str(exc)[:240], t0)
            continue

        if response.status_code >= 400:
            raw = await response.aread()
            await _close(client, response)
            text = raw.decode("utf-8", "replace")
            kind = classify(response.status_code, text)
            _handle_kind(aid, kind)
            last_error = f"upstream {response.status_code} ({kind})"
            _log(api_key_info, account, client_model, False, "error", response.status_code, text[:240], t0)
            continue

        try:
            data, stream_err = await aggregate_lines(response.aiter_lines())
        except httpx.HTTPError as exc:
            # 上游读超时/断连：关闭连接后换号重试，不能让异常穿透 500 或泄漏连接
            await _close(client, response)
            pool.note_error(aid)
            last_error = str(exc)
            _log(api_key_info, account, client_model, False, "error", 502, str(exc)[:240], t0)
            continue
        await _close(client, response)
        if stream_err is not None:
            _handle_kind(aid, stream_err.kind())
            last_error = str(stream_err)
            _log(api_key_info, account, client_model, False, "error", 502, str(stream_err)[:240], t0)
            continue

        data["model"] = client_model
        finish = str((data.get("choices") or [{}])[0].get("finish_reason") or "stop")
        auth_manager.mark_account_success(aid)
        pool.note_success(aid)
        _log(api_key_info, account, client_model, False, finish, 200, "", t0, data.get("usage"))
        return "json", data

    status, detail = _no_accounts_error(last_error)
    return "error", (status, detail)


def _no_accounts_error(last_error: str) -> tuple[int, dict]:
    return 503, {
        "error": {
            "message": f"No available accounts: {str(last_error)[:240]}",
            "type": "channel_unavailable",
            "code": "channel_unavailable",
            "channel": CHANNEL_ID,
        }
    }


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------

async def _run_stream(
    payload: dict, model: str, client_model: str, api_key_info: dict | None
) -> AsyncGenerator[str, None]:
    """账号重试循环（流式）。上游 2xx 前可轮转；流内错误注入 SSE 并冷却账号。"""
    chunk_id = f"traesolo-{uuid.uuid4().hex[:12]}"
    tried: set[int] = set()
    last_error = "No available accounts"
    for _ in range(MAX_ROTATE):
        account = await _pick(tried)
        if account is None:
            break
        aid = int(account["id"])
        tried.add(aid)
        t0 = time.time()

        _, rkind, rerr = await _pre_refresh(account)
        if rerr is not None:
            _handle_kind(aid, rkind, f"refresh: {rerr}")
            last_error = str(rerr)
            _log(api_key_info, account, client_model, True, "error", 503, str(rerr)[:240], t0)
            continue

        body = prepare_body(payload)
        try:
            client, response = await _open(account, body)
        except httpx.HTTPError as exc:
            pool.note_error(aid)
            last_error = str(exc)
            _log(api_key_info, account, client_model, True, "error", 502, str(exc)[:240], t0)
            continue

        if response.status_code >= 400:
            raw = await response.aread()
            await _close(client, response)
            text = raw.decode("utf-8", "replace")
            kind = classify(response.status_code, text)
            _handle_kind(aid, kind)
            last_error = f"upstream {response.status_code} ({kind})"
            _log(api_key_info, account, client_model, True, "error", response.status_code, text[:240], t0)
            continue

        # 2xx → 锁定该账号，开始转换流
        auth_manager.mark_account_success(aid)
        pool.note_success(aid)
        usage_sink: dict = {}

        def on_error(se: SoloStreamError) -> None:
            _handle_kind(aid, se.kind())

        errored = False
        try:
            async for out in stream_to_openai(
                response.aiter_lines(), chunk_id, client_model, on_error, usage_sink
            ):
                if out.startswith("event: error"):
                    errored = True
                yield out
        except httpx.HTTPError as exc:
            errored = True
            yield "event: error\n"
            yield "data: " + json.dumps(f"upstream stream interrupted: {exc}", ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            await _close(client, response)
        _log(
            api_key_info,
            account,
            client_model,
            True,
            "error" if errored else "stop",
            502 if errored else 200,
            "upstream stream error" if errored else "",
            t0,
            usage_sink.get("usage"),
        )
        return

    # 所有账号在流开始前都失败：以 SSE 错误事件收尾（HTTP 状态已定 200）
    yield "event: error\n"
    yield "data: " + json.dumps(str(last_error)[:300], ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    kick_dynamic_models()
    model = translate_model(str(payload.get("model") or "auto"))
    if model in ("", "auto"):
        model = DEFAULT_CONFIG
    if not accepts_model(model):
        return (
            "error",
            (
                400,
                {
                    "error": {
                        "message": f"unknown model '{model}'",
                        "type": "invalid_request_error",
                        "code": "unknown_model",
                    }
                },
            ),
        )
    client_model = str(payload.get("model") or model)
    if payload.get("stream"):
        return "stream", _run_stream(payload, model, client_model, api_key_info)
    return await _run_once(payload, model, client_model, api_key_info)


async def test_chat(account: dict, model: str = DEFAULT_CONFIG, prompt: str = "ping") -> dict:
    """单账号链路测试：发一个非流式短请求。"""
    t0 = time.time()
    name = translate_model(model or "auto")
    if name in ("", "auto"):
        name = DEFAULT_CONFIG
    body = prepare_body(
        {"model": name, "messages": [{"role": "user", "content": prompt or "ping"}], "stream": False}
    )
    client = None
    response = None
    try:
        client, response = await _open(account, body)
        if response.status_code >= 400:
            raw = await response.aread()
            text = raw.decode("utf-8", "replace")
            return {
                "ok": False,
                "status_code": response.status_code,
                "duration_ms": int((time.time() - t0) * 1000),
                "message": text[:400],
            }
        data, stream_err = await aggregate_lines(response.aiter_lines())
        if stream_err is not None:
            return {
                "ok": False,
                "status_code": 502,
                "duration_ms": int((time.time() - t0) * 1000),
                "message": str(stream_err)[:400],
            }
        text = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
        return {
            "ok": True,
            "status_code": 200,
            "duration_ms": int((time.time() - t0) * 1000),
            "message": text[:400],
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "duration_ms": int((time.time() - t0) * 1000),
            "message": str(exc)[:400],
        }
    finally:
        if client is not None and response is not None:
            await _close(client, response)
