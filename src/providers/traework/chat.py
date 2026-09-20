"""TraeWork chat via remote chat_sessions. Isolated HTTP client."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

import httpx

from accounts import auth_manager
from storage.http_pool import get_client
from storage import database as db
from providers import store_common
from providers.model_config import channel_aliases, channel_model_ids, channel_session_mode
from providers.traework.constants import (
    AGENT_API,
    agent_id_for_mode,
    ALIASES,
    CHANNEL_ID,
    CODE_MODE_FLAG,
    SESSION_MODE,
    SESSION_MODE_CODE,
    SESSIONS_PATH,
    STATIC_MODELS,
)
from providers.traework.token import (
    TraeWorkAuthError,
    adopt_credentials_from_client,
    auth_headers,
    refresh_account,
)
from providers.trae_shared import pick_with_refresh_fallback
from providers.host_override import channel_host

# 后台收尾任务（删会话 / 关连接）的引用集合：持强引用避免被 GC 提前回收，
# 任务完成即通过 done_callback 自动移除（旧实现只留最后一个引用且从不清理）。
_bg_close_tasks: set[asyncio.Task] = set()


def _spawn_bg_close(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_close_tasks.add(task)
    task.add_done_callback(_bg_close_tasks.discard)


# 与 qclaw / qwenwork 的同名单行拷贝收敛：见 store_common.make_translator
translate_model = store_common.make_translator(
    lambda: channel_aliases(CHANNEL_ID, ALIASES), "auto"
)


def accepts_model(inner: str) -> bool:
    value = (inner or "").strip()
    return (
        value in channel_model_ids(CHANNEL_ID, STATIC_MODELS)
        or value in channel_aliases(CHANNEL_ID, ALIASES)
    )


def _parts_to_text(content) -> str:
    """把一条消息的 content 拍平成文本。

    支持 str 与 list[part]：list 里只取文本零件（part.get("text") 或裸 str），
    图片类零件（type=="image_url" / 含 "image"）不在此协议承载，直接忽略但不崩。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                # 图片零件无法放进单轮 query 文本，忽略（不抛）。
                ptype = str(part.get("type") or "")
                if "image" in ptype:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _build_prompt(payload: dict) -> tuple[str, bool]:
    """把 system + 全部历史压平进单条上游 query 文本（R1，入参保真）。

    上游是「会话 + 单 query」模型，不像兄弟通道（qwenwork/qodercn/qclaw/traesolo）
    那样把 messages 数组整体转发；网关每请求新建并删除会话，服务端不持有历史。
    为在模型视角与兄弟通道保持一致（system 与多轮历史都抵达上游），这里把
    它们串行化进唯一的 query 文本，等价于兄弟通道「转发 system + 历史」的语义。

    约定：用户/system/assistant 依次拼接，相邻段之间空行分隔；多条 system 以
    空白行拼接（与 qwenwork._split_messages 一致）。不含任何前缀/标记。

    硬约束：当 payload 无 system 且仅有一条 user 消息时，返回文本须与该 user
    文本逐字节相同（无前缀/后缀/标记，且**不做 strip**）——既保持历史行为，
    也锁定既有测试。

    返回 (prompt, has_user)：has_user 为 False 时调用方应回 400（无 user 轮）。
    has_user 语义与改造前 _last_user_text 一致：只看是否存在**非空 user 消息**，
    只有 assistant/tool 轮的请求同样回 400。
    """
    system_parts: list[str] = []
    turns: list[str] = []
    # 单条 user 轮的原样文本（不 strip），用于满足"逐字节一致"的硬约束。
    sole_user_raw: str | None = None
    user_count = 0
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        raw = _parts_to_text(item.get("content"))
        text = raw.strip()
        if role == "user" and text:
            user_count += 1
            sole_user_raw = raw
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
            continue
        # user / assistant 都作为对话历史压平；其余角色（如 tool）按文本并入。
        turns.append(text)

    # 单一 user 且无 system：原样返回未 strip 的原文，保证逐字节一致。
    if not system_parts and user_count == 1 and len(turns) == 1:
        return (sole_user_raw if sole_user_raw is not None else turns[0]), True
    return "\n\n".join([*system_parts, *turns]), user_count > 0


def _finish_answer(tool_call_info, bucket: list[str]) -> None:
    """SOLO agent 的最终回答由 finish 工具调用携带（params.summary 等）。"""
    if not isinstance(tool_call_info, dict):
        return
    if str(tool_call_info.get("name") or "") != "finish":
        return
    params = tool_call_info.get("params")
    if not isinstance(params, dict):
        return
    for key in ("summary", "content", "text"):
        item = params.get(key)
        if isinstance(item, str) and item.strip():
            bucket.append(item.strip())
            return
    for item in params.values():
        if isinstance(item, str) and item.strip():
            bucket.append(item.strip())
            return


def _walk_text(value, answer: list[str], thinking: list[str]) -> None:
    """收集回答文本（answer）与思考文本（thinking），两者分开存放。

    回答来源：finish 工具的 params、text_content/text/markdown/plain_text、
    普通 content 字符串。思考来源：reasoning_content、thought。
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                _walk_text(json.loads(text), answer, thinking)
            except json.JSONDecodeError:
                pass
        return
    if isinstance(value, dict):
        kind = str(value.get("type") or "")
        if kind in {"status", "tool", "tool_call"}:
            return
        _finish_answer(value.get("tool_call_info"), answer)
        for key in ("text_content", "text", "markdown", "plain_text"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                answer.append(item.strip())
        for key in ("reasoning_content", "thought"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                thinking.append(item.strip())
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            if content.startswith("{") or content.startswith("["):
                _walk_text(content, answer, thinking)
            elif content.strip() not in answer:
                answer.append(content.strip())
        elif isinstance(content, (dict, list)):
            _walk_text(content, answer, thinking)
        # 最终消息里 plan_item 还套了一层对象（type=plan_item 的消息体）。
        plan_item = value.get("plan_item")
        if isinstance(plan_item, (dict, list)):
            _walk_text(plan_item, answer, thinking)
        messages = value.get("messages")
        if isinstance(messages, list):
            _walk_text(messages, answer, thinking)
        return
    if isinstance(value, list):
        for item in value:
            _walk_text(item, answer, thinking)


def _join(bucket: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in bucket:
        if chunk not in seen:
            seen.add(chunk)
            ordered.append(chunk)
    return "\n".join(ordered).strip()


_SKIP_EVENTS = {
    "heartbeat",
    "status_changed",
    "platform_timing",
    "timing_events",
    "model_config",
    "project_name_message",
    "session_title_message",
    "session_icon_message",
    "metadata",
}


def _split_event(event: str, payload: dict) -> tuple[str, list[str]]:
    """拆出一个事件的（回答文本, 思考片段列表）。

    思考片段来自 reasoning_content / thought（如 plan_item 事件），
    供流式请求提前转发；回答文本只用于事件兜底拼接。

    token_usage / done 不携带可见文本：前者是用量统计（单独解析后透传到
    usage，绝不并入回答），后者是终止信号，二者都不走 _walk_text，避免
    数值/状态字段被误当正文拼进答案。
    """
    if event in _SKIP_EVENTS or event in ("token_usage", "done"):
        return "", []
    answer: list[str] = []
    thinking: list[str] = []
    _walk_text(payload, answer, thinking)
    return _join(answer), thinking


def _text_from_event(event: str, payload: dict) -> str:
    answer, _thinking = _split_event(event, payload)
    return answer


def _parse_token_usage(payload: dict | None) -> dict | None:
    """解析上游 token_usage 事件（input_tokens / output_tokens）。

    与兄弟通道 traesolo 同族约定：只认 input_tokens / output_tokens 两组键名
    （含 input_token / output_token 简写），其余未知字段丢弃。缺失 / 非整数
    / None 一律按 0 处理；两组都为空则不返回 usage（保持旧语义）。
    """
    if not isinstance(payload, dict):
        return None
    candidates = (
        ("input_tokens", "output_tokens"),
        ("input_token", "output_token"),
    )
    inp = out = None
    for k_in, k_out in candidates:
        if payload.get(k_in) is not None or payload.get(k_out) is not None:
            inp = payload.get(k_in)
            out = payload.get(k_out)
            break
    if inp is None and out is None:
        return None
    try:
        inp_i = int(inp) if isinstance(inp, (int, float, str)) and inp not in ("", None) else 0
    except (TypeError, ValueError):
        inp_i = 0
    try:
        out_i = int(out) if isinstance(out, (int, float, str)) and out not in ("", None) else 0
    except (TypeError, ValueError):
        out_i = 0
    return {
        "prompt_tokens": inp_i,
        "completion_tokens": out_i,
        "total_tokens": inp_i + out_i,
    }


def _finish_reason_from_done(payload: dict | None) -> str | None:
    """上游 done 事件携带 status：完成类映射 stop，失败/取消类映射 error。

    无证据支持 length 这类截断态，故不臆造；status 缺失或非预期值回退 None
    （调用方按既有默认 stop 处理）。
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if status is None:
        return None
    status = str(status).lower()
    if status in ("completed", "complete", "done", "success", "ok", "finished", "succeed"):
        return "stop"
    if status in ("error", "failed", "failure", "cancel", "cancelled", "canceled", "abort", "aborted"):
        return "error"
    return None


def extract_assistant_text(items: list) -> str:
    answer: list[str] = []
    thinking: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("role") not in {"assistant", "system"} and item.get("message_type") != "task":
            continue
        _walk_text(item.get("content"), answer, thinking)
    # 有正文用正文；只有思考时退回思考文本（保持旧的兜底行为）。
    return _join(answer) or _join(thinking)


def _openai_json(
    model: str,
    text: str,
    finish: str = "stop",
    usage: dict | None = None,
    reasoning: str = "",
) -> dict:
    # 上游 token_usage 事件拿到真值则回填，否则保持 0（向后兼容）。
    usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    message: dict = {"role": "assistant", "content": text}
    # R3：思考文本走独立的 reasoning_content 字段（与 qwenwork/qodercn/traesolo
    # 一致），不混入 content。当正文来自思考兜底时，answer 与 reasoning 是同一段，
    # 此时不重复填 reasoning_content，避免同一字符串既当答案又当思考。
    if reasoning and reasoning != text:
        message["reasoning_content"] = reasoning
    return {
        "id": f"traework-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": usage,
    }


async def _pick(tried: set[int]) -> dict | None:
    # 兜底收敛到共享实现（含 refresh 失败 60s 负缓存），与 qwenwork 同源
    return await pick_with_refresh_fallback(CHANNEL_ID, refresh_account, exclude_ids=tried)


async def _log(api_key_info, account, model_name, stream, finish, status, error, t0,
               first_token_ms=None, usage=None):
    # 落库线程化 + 语义收敛：见 store_common.log_request（三家 _log 的一份实现）。
    # usage 仅在拿到上游 token_usage 真值时传真实 dict，否则传 None（tokens/credit 记 0）。
    #
    # 注意：log_request 的 prompt_tokens / completion_tokens / total_tokens 三列读的是
    # 独立 kwargs，**不会**从 usage 里推导；credit 又只按 total_tokens 计算。只传 usage
    # 的话 usage_json 有真值而三列与 credit 仍恒 0（与 qclaw/qwenwork 的 _log 不一致）。
    # 故拿到真值时显式把 usage 拆成三个 kwargs 一并透传；没拿到时**一个都不传**，
    # 保持与改造前完全一致的行为（log_request 侧默认取 0）。
    token_kwargs = {}
    if isinstance(usage, dict) and usage:
        token_kwargs = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
    await store_common.log_request(
        api_key_info, account,
        channel=CHANNEL_ID, model=model_name, stream=stream, usage=usage,
        finish_reason=finish, status_code=status,
        duration_ms=int((time.time() - t0) * 1000), error_msg=error,
        created_at=int(t0), first_token_ms=first_token_ms,
        **token_kwargs,
    )


async def _turn(
    account: dict,
    prompt: str,
    model: str,
    timeout: float = 90.0,
    on_thinking=None,
) -> tuple:
    """跑完一个上游 agent 回合，返回 (text, usage, finish_reason, reasoning)。

    on_thinking: 可选 async 回调，上游事件流里的思考文本（plan_item 的
    thought/reasoning_content）到达时逐片段调用，供流式请求提前转发。返回值
    reasoning 同样累计这些思考文本（R3），供非流式回填 message.reasoning_content。
    """
    headers = auth_headers(account)
    session_url = f"{channel_host(CHANNEL_ID, 'agent_host', AGENT_API)}{SESSIONS_PATH}"
    sid = ""
    # 复用进程级共享连接池，超时按请求传参；绝不 aclose 共享 client。
    client = get_client()
    task: asyncio.Task | None = None
    closed = False
    # 累计思考文本（R3）：流式经 on_thinking 提前转发，非流式在 _turn 内自行累计，
    # 最终回填到非流式 message.reasoning_content。与流式用同一去重逻辑避免重复。
    thinking_accum: list[str] = []

    async def _close_session() -> None:
        if sid:
            try:
                await client.delete(f"{session_url}/{sid}", headers=headers, timeout=timeout)
            except Exception:
                pass

    async def _close_client() -> None:
        # 幂等：成功/失败/外层兜底可能多次触达，只真正执行一次。
        # client 是共享池的连接，这里只清会话，不做 aclose。
        nonlocal closed
        if closed:
            return
        closed = True
        await _close_session()

    try:
        # 建会话与发消息共用同一个 mode，确保两者取值一致（只解析一次）。
        mode = channel_session_mode(CHANNEL_ID, SESSION_MODE)
        # code 模式：官方 client 在 createSession 把 is_in_code_mode 嵌套进
        # initial_message（976.f593cb93.mjs applyCodeModeFlagIfNeeded）；work 模式
        # 不发送该字段，body 与此前逐字段一致。
        create_json: dict = {
            "mode": mode,
            "auto_create_project": True,
            "origin": "web",
        }
        if mode == SESSION_MODE_CODE:
            init_msg = dict(create_json.get("initial_message") or {})
            init_msg[CODE_MODE_FLAG] = True
            create_json["initial_message"] = init_msg
        created = await client.post(
            session_url,
            headers=headers,
            json=create_json,
            timeout=timeout,
        )
        if created.status_code >= 400:
            raise TraeWorkAuthError(f"create session HTTP {created.status_code}")
        data = created.json() if created.content else {}
        if data.get("code") not in (None, 0):
            raise TraeWorkAuthError(str(data.get("message") or data.get("code")))
        sid = str((data.get("data") or {}).get("chat_session_id") or "")
        if not sid:
            raise TraeWorkAuthError("create session missing chat_session_id")
        pieces: list[str] = []
        usage: dict | None = None  # 上游 token_usage 事件解析出的真实用量（无则 None）
        finish_reason: str | None = None  # 上游 done.status 映射出的终止原因（无则 None）
        finished = asyncio.Event()

        async def read_events() -> None:
            nonlocal usage, finish_reason
            event_name = "message"
            seen_thinking: set[str] = set()
            try:
                async with client.stream(
                    "GET",
                    f"{session_url}/{sid}/events",
                    headers={**headers, "Accept": "text/event-stream"},
                    timeout=timeout,
                ) as response:
                    if response.status_code >= 400:
                        finished.set()
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        try:
                            event_payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        event_payload = event_payload if isinstance(event_payload, dict) else {}
                        # token_usage 单独解析：拿到真值用量，且不并入可见回答文本。
                        if event_name == "token_usage":
                            parsed = _parse_token_usage(event_payload)
                            if parsed is not None:
                                usage = parsed
                            continue
                        if event_name == "done":
                            # 终端事件：读取 status 映射 finish_reason（R5）。
                            mapped = _finish_reason_from_done(event_payload)
                            if mapped is not None:
                                finish_reason = mapped
                            finished.set()
                            return
                        answer_text, thinking_frags = _split_event(event_name, event_payload)
                        if answer_text:
                            pieces.append(answer_text)
                        for frag in thinking_frags:
                            if frag and frag not in seen_thinking:
                                seen_thinking.add(frag)
                                thinking_accum.append(frag)
                                if on_thinking is not None:
                                    await on_thinking(frag)
            except httpx.HTTPError:
                finished.set()

        task = asyncio.create_task(read_events())
        try:
            # 留出 SSE 订阅建立时间；早期事件即使丢失也不影响结果
            # （最终 GET /messages 是兜底数据源）。
            await asyncio.sleep(0.1)
            query = json.dumps(
                [{"type": "text", "data": {"content": prompt}}],
                ensure_ascii=False,
            )
            agent = agent_id_for_mode(mode)
            # code 模式：官方 client 在 sendMessage 把 is_in_code_mode 放在 body 顶层
            # （976.f593cb93.mjs applyCodeModeFlagIfNeeded）；work 模式不发送该字段。
            msg_json: dict = {
                "chat_session_id": sid,
                "content": [],
                "query": query,
                "model_name": model,
                "agent_id": agent,
                "agent_type": agent,
            }
            if mode == SESSION_MODE_CODE:
                msg_json[CODE_MODE_FLAG] = True
            sent = await client.post(
                f"{session_url}/{sid}/messages",
                headers=headers,
                json=msg_json,
                timeout=timeout,
            )
            payload = sent.json() if sent.content else {}
            if sent.status_code >= 400 or payload.get("code") not in (None, 0):
                raise TraeWorkAuthError(str(payload.get("message") or f"HTTP {sent.status_code}"))
            try:
                await asyncio.wait_for(finished.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            messages = await client.get(f"{session_url}/{sid}/messages", headers=headers, timeout=timeout)
            body = messages.json() if messages.content else {}
            items = ((body.get("data") or {}).get("items") or [])
            text = extract_assistant_text(items) or "\n".join(dict.fromkeys(pieces)).strip()
            if not text:
                raise TraeWorkAuthError("TraeWork turn finished without assistant text")
            # 成功路径：读流任务收尾后，会话删除放到后台（连接属共享池无需关闭），
            # 不阻塞对客户端的响应（省 ~150ms 尾延迟）。
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            _spawn_bg_close(_close_client())
            # 透传真实用量与终止原因：usage 仅在拿到 token_usage 真值时非空，
            # finish_reason 仅在 done.status 命中映射时非空（缺省回退 "stop"）。
            # reasoning 为累计思考文本（R3），空串表示无思考。
            return text, usage, finish_reason, "\n".join(dict.fromkeys(thinking_accum)).strip()
        except BaseException:
            # 失败路径：同步清理，会话删除尽量做到，再抛出。
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            await _close_client()
            raise
    except BaseException:
        if task is not None:
            task.cancel()
        await _close_client()
        raise


async def _adopt_and_retry(
    account: dict, prompt: str, model: str, *, timeout: float = 90.0, on_thinking=None
) -> tuple | None:
    """鉴权失效后的「凭据自救 + 重试一次」；返回 (text, usage, finish_reason)，未接管/失败返回 None。

    `_run_turn`（选号链路）与 `test_chat`（管理页「测试」直调账号）共用这一份实现：
    后者绕过 _pick，若不自救则凭据被客户端轮换后「测试」永远失败，而它恰是
    用户判断通道可用性的主要入口。收敛为一份，避免两处漂移。

    注：重试阶段的异常在此吞掉并返回 None —— 调用方本就把该回合记为 503，
    故不影响既有降级语义。
    """
    try:
        adopted = await adopt_credentials_from_client(account)
    except Exception:  # noqa: BLE001 - 自救链路须 best-effort，绝不外抛
        adopted = False
    if not adopted:
        return None
    fresh = db.get_account(int(account.get("id") or 0))
    if not fresh:
        return None
    try:
        result = await _turn(fresh, prompt, model, timeout=timeout, on_thinking=on_thinking)
    except Exception:  # noqa: BLE001 - 重试失败同样收敛为 None（调用方按 503 降级）
        return None
    # 保留 _turn 的完整四元组 (text, usage, finish_reason, reasoning)；兼容旧替身
    # 只返回 (text, usage, finish_reason) 或纯文本的情形。
    if isinstance(result, tuple):
        return result
    return result, None, None, ""


async def _run_turn(
    prompt: str,
    model: str,
    client_model: str,
    api_key_info: dict | None,
    stream: bool = False,
    on_thinking=None,
    timeout: float = 90.0,
) -> tuple:
    """账号重试循环。返回 ("ok", text, usage, finish_reason) 或 ("error", (status, detail))。

    成功路径多带回 usage / finish_reason（供非流式 JSON 与流式末帧共用，二者此前
    取不到上游真值）；失败路径仍是 ("error", (status, detail)) 两元组，调用方按
    原语义解包，签名（入参）保持不变以兼容既有测试替身
    （tests/test_perf_providers.py 的 fake _run_turn）。

    流式路径在 on_thinking 回调上挂 first_token_cell（{"t0": 起点}）：
    思考帧在 _stream_chat 侧打点；回合结束时若还没有内容帧（最终回答
    才出的场景），由这里补记一次，避免漏采。
    """
    first_token_cell = getattr(on_thinking, "first_token_cell", None)
    tried: set[int] = set()
    # 每个账号最多自救重试一次，避免死循环（与失败标 expired 的次数解耦）。
    adopted_once: set[int] = set()
    last_error = None
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(int(account["id"]))
        t0 = time.time()
        try:
            turn_result = await _turn(account, prompt, model, timeout=timeout, on_thinking=on_thinking)
            # _turn 返回 (text, usage, finish_reason, reasoning)；兼容旧替身只返回
            # (text, usage, finish_reason) 或纯文本的情形。
            if isinstance(turn_result, tuple):
                text, turn_usage, turn_finish, *rest = turn_result
                turn_reasoning = rest[0] if rest else ""
            else:
                text, turn_usage, turn_finish, turn_reasoning = turn_result, None, None, ""
            finish = turn_finish or "stop"
            auth_manager.mark_account_success(account["id"])
            if first_token_cell is not None and "ms" not in first_token_cell:
                first_token_cell["ms"] = int(
                    (time.monotonic() - first_token_cell.get("t0", time.monotonic())) * 1000
                )
            await _log(
                api_key_info, account, client_model, stream, finish, 200, "", t0,
                first_token_ms=(first_token_cell or {}).get("ms"),
                usage=turn_usage,
            )
            # 返回 (text, usage, finish_reason, reasoning)；非流式 _openai_json 与流式末帧共用。
            # reasoning 经 *_stream_chat 收集（流式）或 _openai_json 回填（非流式）。
            return "ok", text, turn_usage, finish, turn_reasoning
        except TraeWorkAuthError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            await _log(api_key_info, account, client_model, stream, "error", 503, str(exc)[:240], t0)
            # 鉴权失效：尝试从客户端 storage.json 自救；成功则拿新凭据重试本回合一次。
            account_id = int(account["id"])
            if account_id not in adopted_once:
                retried = await _adopt_and_retry(
                    account, prompt, model, timeout=timeout, on_thinking=on_thinking
                )
                if retried is not None:
                    adopted_once.add(account_id)
                    fresh = db.get_account(account_id) or account
                    auth_manager.mark_account_success(account_id)
                    # 自救重试同样产出 (text, usage, finish_reason[, reasoning])；
                    # 兼容旧替身只返回 (text, usage, finish_reason) 或纯文本的情形。
                    retry_usage: dict | None = None
                    retry_finish = "stop"
                    retry_reasoning = ""
                    if isinstance(retried, tuple):
                        retried, retry_usage, retry_finish, *rest = retried
                        retry_finish = retry_finish or "stop"
                        retry_reasoning = rest[0] if rest else ""
                    if first_token_cell is not None and "ms" not in first_token_cell:
                        first_token_cell["ms"] = int(
                            (time.monotonic() - first_token_cell.get("t0", time.monotonic())) * 1000
                        )
                    await _log(
                        api_key_info, fresh, client_model, stream, retry_finish, 200, "", t0,
                        first_token_ms=(first_token_cell or {}).get("ms"),
                        usage=retry_usage,
                    )
                    return "ok", retried, retry_usage, retry_finish, retry_reasoning
            continue
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            continue
    if last_error is not None:
        return last_error
    return (
        "error",
        (
            503,
            {
                "error": {
                    "message": "No available accounts",
                    "type": "channel_unavailable",
                    "code": "channel_unavailable",
                }
            },
        ),
    )


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    model = translate_model(str(payload.get("model") or "auto"))
    # R1：把 system + 全部历史压平进单条 query（上游是会话 + 单 query 模型）。
    prompt, has_user = _build_prompt(payload)
    if not has_user:
        return (
            "error",
            (400, {"error": {"message": "messages must include a user turn", "type": "invalid_request_error"}}),
        )
    stream = bool(payload.get("stream"))
    client_model = str(payload.get("model") or model)
    if stream:
        # 流式：立即返回生成器。首包马上发出，思考文本随事件提前转发，
        # 回合跑完后再补最终回答，避免客户端干等十几秒。
        return ("stream", _stream_chat(prompt, model, client_model, api_key_info))
    status, *rest = await _run_turn(prompt, model, client_model, api_key_info, stream=False)
    if status == "ok":
        # _run_turn 现返回 ("ok", text, usage, finish_reason, reasoning)；
        # 兼容旧替身只回四元组或更短的情形。
        text = rest[0]
        usage = rest[1] if len(rest) > 1 else None
        finish = rest[2] if len(rest) > 2 else None
        reasoning = rest[3] if len(rest) > 3 else ""
        return ("json", _openai_json(client_model, text, finish or "stop", usage, reasoning))
    status_code, detail = rest[0]
    return ("error", (status_code, detail))


def _new_piece(prev: str, frag: str) -> str:
    """上游 plan_item 常把累计思考整段重发；只转发相对上一段的新增量。

    两种情况保持旧行为：
      - 累积重发（frag 是 prev 的扩展）：返回新增尾部；
      - 收缩（frag 是 prev 的严格前缀）：返回 ""（fragment 已被显示过，无新内容）。
    其余情形（既非扩展也非收缩，例如重叠但并不对齐的两段）：去掉 prev
    尾部与 frag 头部的最大重叠部分再转发，避免把已显示内容重复拼一遍，也不
    因误判为收缩而把真正的新文字吞掉。
    """
    if not prev or not frag:
        return frag
    if frag.startswith(prev):
        return frag[len(prev):]
    if prev.startswith(frag):
        return ""
    # 非前缀重叠：找 prev[-k:] == frag[:k] 的最长 k，剥离这段重复。
    max_k = min(len(prev), len(frag))
    overlap = 0
    for k in range(max_k, 0, -1):
        if prev[-k:] == frag[:k]:
            overlap = k
            break
    return frag[overlap:]


async def _stream_chat(
    prompt: str, model: str, client_model: str, api_key_info: dict | None
) -> AsyncGenerator[str, None]:
    chunk_id = f"traework-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    # first_token_ms：基线 = 生成器启动（含账号轮换/_run_turn 内的建连），
    # 首个内容帧（思考增量或最终回答）打点，经共享 cell 透传给 _run_turn 的落库。
    request_t0 = time.monotonic()
    first_token_cell: dict = {"t0": request_t0}

    def mark_first_token() -> None:
        first_token_cell.setdefault(
            "ms", int((time.monotonic() - first_token_cell["t0"]) * 1000)
        )

    def sse(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
        body = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": client_model,
            # 末帧的 usage 放在 chunk 顶层（与 OpenAI 流式一致），不塞进 delta。
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            body["usage"] = usage
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    # 立即首包：客户端马上有 TTFB，不再是干等 10s+ 毫无输出。
    # （role 帧不含内容，不计入 first_token_ms。）
    yield sse({"role": "assistant"})

    queue: asyncio.Queue = asyncio.Queue()

    async def on_thinking(fragment: str) -> None:
        # 思考片段到达即打点：首个片段必然被转发（首个 piece 永远非空），
        # 且必须赶在 _run_turn 完成并落库之前记录真实的首帧时刻。
        mark_first_token()
        queue.put_nowait(fragment)

    # first_token_cell 挂在 on_thinking 回调上（_run_turn 经 getattr 读取），
    # 不改 _run_turn 签名。
    on_thinking.first_token_cell = first_token_cell  # type: ignore[attr-defined]

    turn_task = asyncio.create_task(
        _run_turn(prompt, model, client_model, api_key_info, stream=True, on_thinking=on_thinking)
    )
    last_full = ""
    try:
        while True:
            if turn_task.done() and queue.empty():
                break
            get_task = asyncio.ensure_future(queue.get())
            try:
                done, _pending = await asyncio.wait(
                    {get_task, turn_task}, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                get_task.cancel()
                turn_task.cancel()
                raise
            if get_task in done:
                frags = [get_task.result()]
                while not queue.empty():
                    frags.append(queue.get_nowait())
                for frag in frags:
                    piece = _new_piece(last_full, frag)
                    last_full = frag
                    if piece:
                        # R3：思考片段走独立的 reasoning_content 字段，不塞进 content
                        # （与 qwenwork/qodercn/traesolo 一致）；最终答案另走 content。
                        yield sse({"reasoning_content": piece})
            else:
                get_task.cancel()
    finally:
        # 客户端断连 / 生成器被关闭：取消进行中的回合，避免白跑一个上游 agent。
        if not turn_task.done():
            turn_task.cancel()

    try:
        res = turn_task.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        res = (
            "error",
            (500, {"error": {"message": f"internal error: {exc}"[:240], "type": "server_error"}}),
        )
    # 容忍测试替身（仅返回 ("ok", text) 两元组），真实 _run_turn 返回五元组。
    status = res[0]
    if status == "ok":
        text = res[1]
        usage = res[2] if len(res) > 2 else None
        finish = res[3] if len(res) > 3 else None
        finish = finish or "stop"
        # 最终回答始终以 content 下发。R3 之后思考走 reasoning_content 这一独立字段，
        # 与 content 不再共用通道，故**不能**再拿思考文本去抑制回答：早期
        # "text in 已转发内容则跳过"的守卫是为旧行为（思考混在 content 里）防重复用的，
        # 沿用会导致不渲染 reasoning_content 的客户端**完全收不到回答**。
        # 与 qodercn/traesolo 一致：content 与 reasoning_content 各自独立转发，不跨通道去重。
        if text:
            mark_first_token()
            yield sse({"content": text})
        # 末帧带 usage（上游 token_usage 真实值，无则省略）与 finish_reason。
        yield sse({}, finish, usage=usage)
        yield "data: [DONE]\n\n"
    else:
        # 流内错误：发 OpenAI 兼容的 error 对象 + [DONE]，不伪造 stop 结束的
        # 正常回答（客户端会把错误文案当答案存下来，也无法感知失败）。
        status_code, detail = res[1]
        msg = str(detail.get("error", {}).get("message", ""))[:300]
        error_payload = {
            "error": {"message": f"upstream failed: {msg}", "type": "server_error", "code": status_code}
        }
        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


async def test_chat(account: dict, model: str = "qwen-3.7-plus", prompt: str = "请回复：pong") -> dict:
    async def send(_payload: dict) -> tuple:
        try:
            result = await _turn(account, prompt or "请回复：pong", translate_model(model or "auto"), timeout=90.0)
        except TraeWorkAuthError as exc:
            # 「测试」按钮直接拿账号调 _turn，绕过了 _pick/_run_turn 的选号链路，
            # 因此自愈必须在这里也接一次：否则凭据被客户端轮换后，测试永远失败，
            # 而这条路径恰是用户判断通道可用性的主要入口。
            healed = await _adopt_and_retry(account, prompt, model)
            if healed is None:
                return 503, str(exc)[:400], None
            # _adopt_and_retry 返回 (text, usage, finish_reason)；测试只取文本。
            healed_text = healed[0] if isinstance(healed, tuple) else healed
            return 200, None, healed_text
        # _turn 返回 (text, usage, finish_reason)；测试只取文本。
        text = result[0] if isinstance(result, tuple) else result
        return 200, None, text

    return await store_common.run_test_chat(model or "auto", prompt or "请回复：pong", send, limit=400)
