"""TraeWork chat via remote chat_sessions. Isolated HTTP client."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

import httpx

from accounts import auth_manager
from storage import database as db
from providers.model_config import channel_aliases, channel_model_ids
from providers.traework.constants import (
    AGENT_API,
    AGENT_ID,
    ALIASES,
    CHANNEL_ID,
    SESSION_MODE,
    SESSIONS_PATH,
    STATIC_MODELS,
)
from providers.traework.token import TraeWorkAuthError, auth_headers, is_token_expired, refresh_account

# 持有最近一次后台清理（删会话 / 关连接）的任务引用，避免被 GC 提前回收。
_bg_close: asyncio.Task | None = None


def translate_model(model: str) -> str:
    inner = (model or "auto").strip() or "auto"
    return channel_aliases(CHANNEL_ID, ALIASES).get(inner, inner)


def accepts_model(inner: str) -> bool:
    value = (inner or "").strip()
    return (
        value in channel_model_ids(CHANNEL_ID, STATIC_MODELS)
        or value in channel_aliases(CHANNEL_ID, ALIASES)
    )


def _last_user_text(payload: dict) -> str:
    for item in reversed(payload.get("messages") or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    parts.append(str(part["text"]))
                elif isinstance(part, str):
                    parts.append(part)
            text = "".join(parts).strip()
            if text:
                return text
    return ""


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
    "token_usage",
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
    """
    if event in _SKIP_EVENTS:
        return "", []
    answer: list[str] = []
    thinking: list[str] = []
    _walk_text(payload, answer, thinking)
    return _join(answer), thinking


def _text_from_event(event: str, payload: dict) -> str:
    answer, _thinking = _split_event(event, payload)
    return answer


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


def _openai_json(model: str, text: str, finish: str = "stop") -> dict:
    return {
        "id": f"traework-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _pick(tried: set[int]) -> dict | None:
    account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
    if account:
        if is_token_expired(account):
            try:
                return await refresh_account(account)
            except Exception:
                pass
        else:
            return account
    expired = [
        row
        for row in db.list_accounts(provider=CHANNEL_ID)
        if row.get("status") == "expired" and row.get("id") not in tried
    ]
    for row in expired:
        try:
            return await refresh_account(row)
        except Exception:
            continue
    return None


def _log(api_key_info, account, model_name, stream, finish, status, error, t0):
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
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "credit": 0,
                "finish_reason": finish,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status,
                "error_msg": error,
                "increment_usage": True,
                "client": (api_key_info or {}).get("_client_tag"),
                "client_version": (api_key_info or {}).get("_client_version"),
            }
        )
    except Exception:
        pass


async def _turn(
    account: dict,
    prompt: str,
    model: str,
    timeout: float = 90.0,
    on_thinking=None,
) -> str:
    """跑完一个上游 agent 回合，返回最终回答文本。

    on_thinking: 可选 async 回调，上游事件流里的思考文本（plan_item 的
    thought/reasoning_content）到达时逐片段调用，供流式请求提前转发。
    """
    headers = auth_headers(account)
    session_url = f"{AGENT_API}{SESSIONS_PATH}"
    sid = ""
    client = httpx.AsyncClient(timeout=timeout)
    task: asyncio.Task | None = None
    closed = False

    async def _close_session() -> None:
        if sid:
            try:
                await client.delete(f"{session_url}/{sid}", headers=headers)
            except Exception:
                pass

    async def _close_client() -> None:
        # 幂等：成功/失败/外层兜底可能多次触达，只真正执行一次。
        nonlocal closed
        if closed:
            return
        closed = True
        await _close_session()
        await client.aclose()

    try:
        created = await client.post(
            session_url,
            headers=headers,
            json={"mode": SESSION_MODE, "auto_create_project": True, "origin": "web"},
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
        finished = asyncio.Event()

        async def read_events() -> None:
            event_name = "message"
            seen_thinking: set[str] = set()
            try:
                async with client.stream(
                    "GET",
                    f"{session_url}/{sid}/events",
                    headers={**headers, "Accept": "text/event-stream"},
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
                        answer_text, thinking_frags = _split_event(
                            event_name,
                            event_payload if isinstance(event_payload, dict) else {},
                        )
                        if answer_text:
                            pieces.append(answer_text)
                        if on_thinking is not None:
                            for frag in thinking_frags:
                                if frag and frag not in seen_thinking:
                                    seen_thinking.add(frag)
                                    await on_thinking(frag)
                        if event_name == "done":
                            finished.set()
                            return
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
            sent = await client.post(
                f"{session_url}/{sid}/messages",
                headers=headers,
                json={
                    "chat_session_id": sid,
                    "content": [],
                    "query": query,
                    "model_name": model,
                    "agent_id": AGENT_ID,
                    "agent_type": AGENT_ID,
                },
            )
            payload = sent.json() if sent.content else {}
            if sent.status_code >= 400 or payload.get("code") not in (None, 0):
                raise TraeWorkAuthError(str(payload.get("message") or f"HTTP {sent.status_code}"))
            try:
                await asyncio.wait_for(finished.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            messages = await client.get(f"{session_url}/{sid}/messages", headers=headers)
            body = messages.json() if messages.content else {}
            items = ((body.get("data") or {}).get("items") or [])
            text = extract_assistant_text(items) or "\n".join(dict.fromkeys(pieces)).strip()
            if not text:
                raise TraeWorkAuthError("TraeWork turn finished without assistant text")
            # 成功路径：读流任务收尾后，会话删除 + 连接关闭放到后台，
            # 不阻塞对客户端的响应（省 ~150ms 尾延迟）。
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            global _bg_close
            _bg_close = asyncio.create_task(_close_client())
            return text
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


async def _run_turn(
    prompt: str,
    model: str,
    client_model: str,
    api_key_info: dict | None,
    stream: bool = False,
    on_thinking=None,
    timeout: float = 90.0,
) -> tuple:
    """账号重试循环。返回 ("ok", text) 或 ("error", (status, detail))。"""
    tried: set[int] = set()
    last_error = None
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(int(account["id"]))
        t0 = time.time()
        try:
            text = await _turn(account, prompt, model, timeout=timeout, on_thinking=on_thinking)
            auth_manager.mark_account_success(account["id"])
            _log(api_key_info, account, client_model, stream, "stop", 200, "", t0)
            return "ok", text
        except TraeWorkAuthError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            _log(api_key_info, account, client_model, stream, "error", 503, str(exc)[:240], t0)
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
    prompt = _last_user_text(payload)
    if not prompt:
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
    status, result = await _run_turn(prompt, model, client_model, api_key_info, stream=False)
    if status == "ok":
        return ("json", _openai_json(client_model, result))
    status_code, detail = result
    return ("error", (status_code, detail))


def _new_piece(prev: str, frag: str) -> str:
    """上游 plan_item 常把累计思考整段重发；只转发相对上一段的新增量。"""
    if not prev or not frag:
        return frag
    if frag.startswith(prev):
        return frag[len(prev):]
    if prev.startswith(frag):
        return ""
    return frag


async def _stream_chat(
    prompt: str, model: str, client_model: str, api_key_info: dict | None
) -> AsyncGenerator[str, None]:
    chunk_id = f"traework-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def sse(delta: dict, finish: str | None = None) -> str:
        body = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": client_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    # 立即首包：客户端马上有 TTFB，不再是干等 10s+ 毫无输出。
    yield sse({"role": "assistant"})

    queue: asyncio.Queue = asyncio.Queue()

    async def on_thinking(fragment: str) -> None:
        queue.put_nowait(fragment)

    turn_task = asyncio.get_event_loop().create_task(
        _run_turn(prompt, model, client_model, api_key_info, stream=True, on_thinking=on_thinking)
    )
    emitted: list[str] = []  # 完整片段（供最终答案去重判断）
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
                    emitted.append(frag)
                    piece = _new_piece(last_full, frag)
                    last_full = frag
                    if piece:
                        yield sse({"content": piece})
            else:
                get_task.cancel()
    finally:
        # 客户端断连 / 生成器被关闭：取消进行中的回合，避免白跑一个上游 agent。
        if not turn_task.done():
            turn_task.cancel()

    try:
        status, result = turn_task.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status, result = (
            "error",
            (500, {"error": {"message": f"internal error: {exc}"[:240], "type": "server_error"}}),
        )
    if status == "ok":
        text = result
        # 最终回答若已包含在转发过的思考文本里就不重复发，避免正文出现两遍。
        if text and text not in "".join(emitted):
            yield sse({"content": ("\n" if emitted else "") + text})
        yield sse({}, "stop")
        yield "data: [DONE]\n\n"
    else:
        # 流内错误：发 OpenAI 兼容的 error 对象 + [DONE]，不伪造 stop 结束的
        # 正常回答（客户端会把错误文案当答案存下来，也无法感知失败）。
        status_code, detail = result
        msg = str(detail.get("error", {}).get("message", ""))[:300]
        error_payload = {
            "error": {"message": f"upstream failed: {msg}", "type": "server_error", "code": status_code}
        }
        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


async def test_chat(account: dict, model: str = "qwen-3.7-plus", prompt: str = "请回复：pong") -> dict:
    t0 = time.time()
    try:
        text = await _turn(account, prompt or "请回复：pong", translate_model(model or "auto"), timeout=90.0)
    except TraeWorkAuthError as exc:
        return {
            "ok": False,
            "status_code": 503,
            "duration_ms": int((time.time() - t0) * 1000),
            "message": str(exc)[:400],
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "duration_ms": int((time.time() - t0) * 1000),
            "message": str(exc)[:400],
        }
    return {
        "ok": True,
        "status_code": 200,
        "duration_ms": int((time.time() - t0) * 1000),
        "message": text[:400],
    }
