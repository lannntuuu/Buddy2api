"""MiniMax Code — OpenAI ⇄ Anthropic Messages 方言双向翻译件（纯函数，无 IO / 无网络）。

协议事实的唯一权威来源：
``.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md``
（下文一律写作 ``spec:NNN`` = 该文件行号）。契约常量在
``providers/minimax_code/constants.py``，本文件只引用、不重复定义。
spec 未覆盖的点一律写成模块级可配置开关 + TODO 注释，禁止编造端点或 header。

职责边界
--------
* 上行：``build_anthropic_payload`` —— 网关收到的 OpenAI Chat Completions 请求体
  → Anthropic Messages 请求体（spec §1.4:154-176 的 body 形状；请求/响应体是
  **明文 JSON**，不存在任何编码/加密/签名层，spec:417-487,695 —— 所以这里绝不加签、
  绝不 base64 封装）。
* 下行：``AnthropicStreamState`` / ``feed_event`` / ``finish_state`` ——
  Anthropic SSE 事件流 → OpenAI ``chat.completion.chunk``（spec §6:569-604）。
  便捷入口 ``feed``（bytes/str → 解析 → feed_event）供 chat.py 直接喂
  ``upstream.sse.SSEDecoder`` 吐出的 data 载荷。
* 下行（非流式）：``to_openai_completion`` —— Anthropic Messages JSON 响应 →
  OpenAI ``chat.completion``（spec:176 的响应孪生形状）。
* 出口收尾：上游 Anthropic **没有** ``[DONE]`` 哨兵（spec:595，结束只看
  ``message_stop``），但我们的网关出口是 OpenAI 方言 —— 由本模块提供
  ``DONE_EVENT``（``data: [DONE]\\n\\n``）在 finish_state 通过后补发。

事件分类的铁律（本通道最容易踩的坑）
------------------------------------
classify 事件**必须用 ``data["type"]``，不能依赖 ``event:`` 行**：仓库唯一的 SSE
解析器 ``upstream/sse.py`` 的 ``SSEDecoder`` 只吐 ``data:`` 字段、**丢弃 ``event:``
行**（sse.py:105 ``if not line.startswith(b"data:")`` 与模块 docstring），而
Anthropic 每条事件的 JSON 内层本来就带同名的 ``type`` 字段（spec:586-588：官方
客户端也是 ``parseJsonWithRepair`` 之后看 ``event.type`` 分派的）。

典型用法（供 chat.py 参照；本模块不调网络）::

    payload = build_anthropic_payload(inner_model, openai_body)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    state = AnthropicStreamState(model=requested_model)
    decoder = SSEDecoder()
    ...
    for data in decoder.feed(chunk_bytes):        # data: bytes（多行 data 已拼好）
        for chunk in feed(state, data):           # 解析 + 按 data["type"] 分类
            yield sse_bytes(chunk)
    usage = finish_state(state)                   # 缺 message_stop ⇒ 抛截断错误（spec:591）
    yield DONE_EVENT                              # 对外 OpenAI 方言收尾补 [DONE]

凭证安全：access_token / refresh_token 原文绝不进日志、异常、断言字面量；
本模块根本不触碰凭证字段，错误消息一律只带上游错误负载的截断摘录（≤240 字符）。
"""

from __future__ import annotations

import json
import time

from providers.minimax_code.constants import (
    ANTHROPIC_ERROR_TYPE_TO_CODE,
    BYOK_MODEL_REF_PREFIX,
    CACHE_CONTROL_TYPE,
    CACHE_LONG_RETENTION_SUPPORTED,
    CHANNEL_ID,
    DEFAULT_MODEL,
    DEFAULT_THINKING_ENABLED,
    DELTA_INPUT_JSON,
    DELTA_SIGNATURE,
    DELTA_TEXT,
    DELTA_THINKING,
    DOCUMENT_BLOCK_TYPE,
    FILES_UPLOAD_PATH,
    MANAGED_MODEL_REF_PREFIX,
    MAX_ATTACHMENTS_COUNT,
    MAX_IMAGE_BYTES_INLINE,
    MAX_REQUEST_BODY_BYTES,
    MAX_VIDEO_BYTES_INLINE,
    MODEL_KEY_SEPARATOR,
    OUTPUT_CONFIG_FORMAT_TYPE_JSON_SCHEMA,
    SSE_EVENT_CONTENT_BLOCK_DELTA,
    SSE_EVENT_CONTENT_BLOCK_START,
    SSE_EVENT_CONTENT_BLOCK_STOP,
    SSE_EVENT_ERROR,
    SSE_EVENT_MESSAGE_DELTA,
    SSE_EVENT_MESSAGE_START,
    SSE_EVENT_MESSAGE_STOP,
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_REFUSAL,
    STOP_REASON_TOOL_USE,
    SUPPORTS_DOCUMENT_BLOCK,
    SUPPORT_JSON_OBJECT_OUTPUT,
    THINKING_CONTROL_ON_OFF,
    THINKING_TYPE_ADAPTIVE,
    THINKING_TYPE_DISABLED,
    USAGE_CACHE_CREATION_INPUT_TOKENS,
    USAGE_CACHE_READ_INPUT_TOKENS,
    USAGE_FIELDS,
    USAGE_INPUT_TOKENS,
    USAGE_OUTPUT_TOKENS,
    model_entry,
)

__all__ = [
    # 任务点名的稳定导出名
    "build_anthropic_payload",
    "AnthropicStreamState",
    "feed_event",
    "finish_state",
    "to_openai_completion",
    "map_stop_reason",
    "normalize_usage",
    # 辅助导出（chat.py / 单测可用）
    "feed",
    "parse_event_data",
    "get_terminal_chunk",
    "sse_bytes",
    "DONE_EVENT",
    "PayloadError",
    "AnthropicStreamError",
    "AnthropicStreamTruncatedError",
    "STABLE_EVENT_TYPES",
]


# ============================================================
# 异常
# ============================================================

class PayloadError(ValueError):
    """上行请求体不合法 / 超出 spec 实证的能力边界。

    由 chat.py 转成对外 4xx；刻意**不静默截断、不静默丢附件**（任务约束：超限
    直接报错 —— 静默丢内容会让模型答非所问且无从诊断）。
    """


class AnthropicStreamError(RuntimeError):
    """Anthropic SSE 流内的 ``error`` 事件（spec:584：客户端行为 = 直接抛错）。

    ``code`` 为上游业务码（内层 ``status_code`` 优先，spec:648-654）；chat.py 用
    constants.UPSTREAM_STATUS_CODE_MAP / ERROR_CODE_TO_HTTP_STATUS 翻成对外 HTTP。
    消息只含上游错误负载的截断摘录，绝不包含本端凭证。
    """

    def __init__(self, message: str, code=None):
        self.code = code
        super().__init__(str(message)[:240])


class AnthropicStreamTruncatedError(AnthropicStreamError):
    """流被截断：没等到 ``message_stop``（spec:591,595）。

    spec:591 原文：官方客户端 ``if (!sawMessageStop) throw new Error("Anthropic
    stream ended before message_stop")`` —— 客户端就是这么做的；我们对外同样
    **显式判错**而不是静默把半截回复当成功返回（chat.py 据此回清晰 5xx）。
    """


# ============================================================
# 可配置项（spec 未覆盖的点一律在此集中，带 TODO；禁止编造端点/字段）
# ============================================================

# max_tokens 缺失时的兜底默认。spec:691 只说「max_tokens:<正整数>」必填，未给默认值
# ⇒ TODO：网关自择 32000，与 model_limits.DEFAULT_MAX_OUTPUT_TOKENS(32768) 同量级，
# 且不超目录里三档共同的 limit.output=128000（spec:503,513,515）。
# 用户显式给的超上限值会被 clamp 到该模型 max_output_tokens（Anthropic 官方约束，
# 超了上游 400；spec 未记录网关行为，clamp 是更贴合客户端预期的兜底选择）。
DEFAULT_MAX_TOKENS = 32_000

# 思考开关（thinking）只对 M3 实证：spec:526-533 的 isMiniMaxM3ThinkingMode /
# resolveMiniMaxM3ThinkingProtocol 覆盖的是 MiniMax-M3；M2.7 系目录没有
# thinking_config（spec:512-515），spec:708 也承认网关行为静态无法枚举
# ⇒ 任务约束：**非 M3 不发 thinking**。若实测 M2.7 也吃这个字段，
# 往这个元组里加即可，不必动映射逻辑（constants 里 M2.7 的可配置 thinking 段
# 仍保留，两处取交集）。
THINKING_MODEL_IDS = ("MiniMax-M3",)  # spec:500-511,526-533

# output_config.effort 是「通用非 M3 Anthropic 路径」的东西（spec:541-544），
# MiniMax 受管思考是开关不是档位（spec:524-533）⇒ 恒 False；仅留开关防将来实测打脸。
SEND_OUTPUT_CONFIG_EFFORT = False  # TODO(spec:541-544)：受管路径未实证 effort，不发

# 图片远程 URL 引用形态：Anthropic 官方 messages 方言里 image.source 支持
# {type:"url"}，spec:12 声明 MiniMax 网关是 "Anthropic Messages 兼容"，但 spec 的
# 文件面只实证了 base64 内联与 files/upload（spec:135,554-561,710）。
# ⇒ TODO：url 源是否被该网关接受未实测；置 False 即改为显式报错（不算静默截断）。
ALLOW_IMAGE_URL_SOURCE = True

# OpenAI ``stop`` → Anthropic ``stop_sequences``：Anthropic 官方字段，但 spec:176/426
# 的 buildParams 清单未列出（那是客户端构造集，不是网关接受集）。
# ⇒ TODO：如实测被拒，关掉这个开关即可。
PASS_THROUGH_STOP_SEQUENCES = True

# 事件白名单（spec:578-581 的 ANTHROPIC_MESSAGE_EVENTS）+ error（spec:584 单独抛错）
# + ping（显式丢弃）。白名单外的未知 type 静默忽略 —— 与官方客户端一致
# （spec:585；spec:708 承认网关可能有被这样忽略的私有事件）。
STABLE_EVENT_TYPES = frozenset(
    {
        SSE_EVENT_MESSAGE_START,
        SSE_EVENT_MESSAGE_DELTA,
        SSE_EVENT_MESSAGE_STOP,
        SSE_EVENT_CONTENT_BLOCK_START,
        SSE_EVENT_CONTENT_BLOCK_DELTA,
        SSE_EVENT_CONTENT_BLOCK_STOP,
        SSE_EVENT_ERROR,
        "ping",
    }
)


# ============================================================
# stop_reason → finish_reason（spec:594，§6 附近）
# ============================================================

# spec:594 原文：end_turn→stop、max_tokens→length、tool_use→toolUse（pi-ai 内部名）、
# refusal→…（省略）。本通道出口是 OpenAI 方言，故 tool_use 落 "tool_calls"；
# refusal 落 content_filter 是网关侧的 OpenAI 语义归位选择（任务约束；Anthropic 无此词表）。
# stop_sequence / pause_turn 是 Anthropic 官方另有值，spec 未枚举 ⇒ 归入 stop
# （TODO：待实测观察实际取值后再细分，与 spec:708 "网关私有字段" 同类不确定性）。
STOP_REASON_MAP: dict[str, str] = {
    STOP_REASON_END_TURN: "stop",           # spec:594
    STOP_REASON_MAX_TOKENS: "length",       # spec:594
    STOP_REASON_TOOL_USE: "tool_calls",     # spec:594（toolUse → OpenAI tool_calls）
    STOP_REASON_REFUSAL: "content_filter",  # spec:594（refusal → OpenAI content_filter）
    "stop_sequence": "stop",                # Anthropic 官方值，spec 未枚举（见上 TODO）
    "pause_turn": "stop",                   # 同上
}


def map_stop_reason(stop_reason) -> str | None:
    """Anthropic stop_reason → OpenAI finish_reason；None 透传 None（流未终结）。

    未知字符串按 "stop" 兜底：spec:594 的映射表不是封闭集合，网关私有值（spec:708）
    宁可归为正常结束，也不要把未知词原样漏给客户端。
    """
    if stop_reason is None:
        return None
    return STOP_REASON_MAP.get(str(stop_reason), "stop")


# ============================================================
# usage 归一（spec §7.1:610-625；总端口径 spec 第 7 行结论 + §5.2/§5.3 缓存语义）
# ============================================================

def _as_int(value) -> int:
    """宽容取整：None/bool/非法 → 0；负数 → 0（usage 不可能为负）。"""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        try:
            return max(0, int(value))
        except (OverflowError, ValueError):
            return 0
    if isinstance(value, str):
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0
    return 0


def normalize_usage(anthropic_usage) -> dict:
    """Anthropic ``usage`` 四字段 → OpenAI ``usage``。

    spec:623：字段名即 Anthropic 原生 ``input_tokens / output_tokens /
    cache_read_input_tokens / cache_creation_input_tokens``；spec:619：
    ``totalTokens = input + output + cacheRead + cacheWrite`` —— **四者求和**，
    因为 Anthropic 的 ``input_tokens`` **不含** cache 部分（spec 第 7 行"只有
    usage 四个 token 字段"与 §5.2/5.3 缓存语义的联合口径）。

    换算（保持 OpenAI 不变量 prompt+completion==total）：
      prompt_tokens     = input + cache_read + cache_creation
      completion_tokens = output
      total_tokens      = 四项之和（= spec:619 的算法）
    同时保留两个 Anthropic 原生 cache 键与 ``prompt_tokens_details.cached_tokens``，
    供 ``providers.store_common.extract_cache_tokens`` / ``credit_source_of``
    （KNOWN_CACHE_KEYS 检查 cache_read_input_tokens，store_common.py:318-329）按
    真实命中记 live —— 否则 cache 统计会被记 0。
    上游 usage **没有** credit 字段（spec:610,624），额度以错误码回报，本函数不涉及。
    """
    src = anthropic_usage if isinstance(anthropic_usage, dict) else {}
    inp = _as_int(src.get(USAGE_INPUT_TOKENS))
    out = _as_int(src.get(USAGE_OUTPUT_TOKENS))
    cache_read = _as_int(src.get(USAGE_CACHE_READ_INPUT_TOKENS))
    cache_creation = _as_int(src.get(USAGE_CACHE_CREATION_INPUT_TOKENS))
    prompt = inp + cache_read + cache_creation
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": out,
        # spec:619,693 —— 总量恒为四字段之和（客户端就是这么算的）。
        "total_tokens": prompt + out,
        # 保留 Anthropic 原生键：cache 统计链路的键名判定依赖它们（见 docstring）。
        USAGE_CACHE_READ_INPUT_TOKENS: cache_read,
        USAGE_CACHE_CREATION_INPUT_TOKENS: cache_creation,
    }
    details = {}
    if cache_read:
        details["cached_tokens"] = cache_read  # OpenAI 风格（store_common 候选之一）
    if cache_creation:
        # TODO：OpenAI 无官方 cache_creation 明细键，自择命名（仅透传给日志面）。
        details["cache_creation_tokens"] = cache_creation
    if details:
        usage["prompt_tokens_details"] = details
    return usage


# ============================================================
# 上行：OpenAI → Anthropic（纯函数）
# ============================================================

def _normalize_model_ref(value: str) -> str:
    """去掉客户端可能带的 ``minimax/`` / ``minimax_api/`` 前缀（spec:518）。

    客户端 model 引用键 = provider/modelId；constants.ALIASES 已收录带前缀写法，
    这里是最后一道兜底（调用方传裸 id 时是恒等操作）。
    """
    text = str(value or "").strip()
    for prefix in (
        MANAGED_MODEL_REF_PREFIX + MODEL_KEY_SEPARATOR,
        BYOK_MODEL_REF_PREFIX + MODEL_KEY_SEPARATOR,
    ):
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix):]
    return text


def _part_text(part) -> str:
    """从 OpenAI content part 里抠文本（容忍字符串零件，同 qodercn._split_messages）。"""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        text = part.get("text")
        if isinstance(text, str):
            return text
    return ""


def _cache_control_of(container):
    """安全读取 dict 上的 cache_control（非 dict / 无键 → None）。"""
    if isinstance(container, dict):
        return container.get("cache_control")
    return None


def _sanitize_cache_control(value):
    """保留 ``cache_control:{type:"ephemeral"}``，但 **ttl 一律剥掉**。

    spec:564：prompt caching 只吃短 ephemeral 标记；长保留 ``ttl:'1h'`` 要求
    supportsLongCacheRetention，而 MiniMax 受管被显式关掉
    （``model-ref.ts:656-659`` ⇒ constants.CACHE_LONG_RETENTION_SUPPORTED=False）
    ⇒ 带 ttl 的标记必须降级成纯 ``{type:"ephemeral"}`` 再发。
    """
    if not isinstance(value, dict):
        return None
    if str(value.get("type") or "") != CACHE_CONTROL_TYPE:
        return None  # 未知缓存类型：spec 未枚举，不发（宁缺毋滥）
    if not CACHE_LONG_RETENTION_SUPPORTED:  # spec:564
        return {"type": CACHE_CONTROL_TYPE}
    # 保留分支：若哪天目录/实测支持长保留，ttl 原样透传。
    out = {"type": CACHE_CONTROL_TYPE}
    ttl = value.get("ttl")
    if isinstance(ttl, str) and ttl:
        out["ttl"] = ttl
    return out


def _text_block(text, cache_control=None) -> dict | None:
    body = text if isinstance(text, str) else ""
    if not body:
        return None  # Anthropic 拒绝空 text block（官方约束；spec:691 的块形状非空）
    block = {"type": "text", "text": body}
    cc = _sanitize_cache_control(cache_control)
    if cc:
        block["cache_control"] = cc  # spec:564 只留 {type:"ephemeral"}
    return block


def _approx_raw_bytes(b64_text: str) -> int:
    """base64 折算原始字节（3/4，粗算即可——上限判定是防弹闸门不是秤）。"""
    return (len(b64_text or "") // 4) * 3


def _data_url_split(url: str) -> tuple[str, str] | None:
    """``data:<media>;base64,<payload>`` → (media_type, payload)；不是 data URL 返回 None。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, rest = url.partition(",")
    if ";base64" not in header:
        return None
    media = header[len("data:"):].split(";")[0].strip() or "application/octet-stream"
    return media, rest


def _image_block_from_part(part, entry) -> dict:
    """OpenAI ``image_url`` part → Anthropic image block（spec:502,560,554）。"""
    model = (entry or {}).get("id", "")
    if entry is not None and not entry.get("is_vl"):
        # spec:515,560：M2.7 系 modalities.input 仅 text，图片直接拒（不静默丢，
        # 丢图作答比拒答更糟）。
        raise PayloadError(
            f"model {model or 'the requested model'} accepts text input only (spec:515,560); "
            "image content is not supported"
        )
    raw = part.get("image_url")
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str) or not url:
        raise PayloadError("image_url part carries no usable url")
    split = _data_url_split(url)
    if split is None:
        if url.startswith("http://") or url.startswith("https://"):
            if not ALLOW_IMAGE_URL_SOURCE:
                raise PayloadError(
                    "remote image url sources are disabled (see ALLOW_IMAGE_URL_SOURCE TODO)"
                )
            # TODO(spec:135,710)：files/upload 的 multipart 字段与 file-id 引用格式
            # 静态未确认 ⇒ 本期远程 URL 先按 Anthropic 官方 url 源原样转发（spec:12
            # 声称 Anthropic 兼容），网关是否接受未实测。
            # ⚠️ 后续实现：先 POST {host}{FILES_UPLOAD_PATH} 换 file id 再引用。
            return {"type": "image", "source": {"type": "url", "url": url}}
        raise PayloadError("image_url must be a data: URL or an http(s) URL")
    media, payload = split
    approx = _approx_raw_bytes(payload)
    if approx > MAX_IMAGE_BYTES_INLINE:
        # spec:554：10 MiB 内可内联 base64；超阈值客户端走 File API（spec:561）。
        # TODO(FILES_UPLOAD_PATH = /mavis/api/v1/llm/v1/files/upload, spec:135,561)：
        # 本期不实现上传 ⇒ 直接报错而不是静默截断。
        raise PayloadError(
            f"inline image of ~{approx} bytes exceeds max_image_bytes_inline="
            f"{MAX_IMAGE_BYTES_INLINE} (spec:554); upload via {FILES_UPLOAD_PATH} "
            "is not implemented this iteration"
        )
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": payload},
    }


def _reject_document_part(part_type: str) -> None:
    """``document``/``file`` 类零件一律显式拒绝（本期不做 PDF 本地转换）。

    spec:562："MiniMax-M3 has no document-block input" ⇒ PDF 由客户端本地 poppler
    转图像/文本，上游**没有** document content block。通道绝不发明这种块。
    TODO：PDF→文本/图像的本地转换属客户端侧能力，本期不在通道实现（任务约束：
    **不发** type:"document"）。
    """
    if not SUPPORTS_DOCUMENT_BLOCK:  # constants.py:406（spec:562）
        raise PayloadError(
            f"{part_type} content blocks are not supported upstream (spec:562: "
            "no document-block input; convert PDF/attachments to text/image client-side)"
        )


def _reject_video_part(part_type: str) -> None:
    """视频：目录声明 M3 可收 video（spec:502,555,560），但 Anthropic 官方方言里
    **没有** video content block，spec 也没给出 MiniMax 的私有形状 ⇒ 不编造
    （任务红线），本期显式报错。
    TODO(spec:555,560,710)：待确认是 {type:"video"} 私有块还是 files/upload 引用；
    max_video_bytes_inline=50MiB（spec:555）届时作为内联阈值复用。"""
    raise PayloadError(
        f"{part_type} content has no confirmed Anthropic-dialect wire shape for this "
        "gateway (spec:560 lists video input, spec:555 caps inline video at "
        f"{MAX_VIDEO_BYTES_INLINE} bytes, but no video block shape is documented); "
        "refusing rather than fabricating"
    )


def _blocks_from_openai_content(content, entry) -> list[dict]:
    """OpenAI message.content（str 或 parts 数组）→ Anthropic content block 数组。

    字符串 → 单个 text block（spec:688 落地要点 1 的 content block 化）。
    """
    if isinstance(content, str):
        block = _text_block(content)
        return [block] if block else []
    if isinstance(content, list):
        blocks: list[dict] = []
        for part in content:
            if isinstance(part, str):
                block = _text_block(part)
                if block:
                    blocks.append(block)
                continue
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "")
            if ptype in ("text", "input_text", ""):
                block = _text_block(_part_text(part), _cache_control_of(part))
                if block:
                    blocks.append(block)
            elif ptype in ("image_url", "input_image"):
                blocks.append(_image_block_from_part(part, entry))
            elif ptype in ("document", "file", "input_file"):
                _reject_document_part(ptype)  # spec:562
            elif ptype in ("video", "video_url", "input_video"):
                _reject_video_part(ptype)
            else:
                raise PayloadError(f"unsupported content part type {ptype!r}")
        return blocks
    if content is None:
        return []
    # 非 str/list 的标量：按文本接住（宽容三方客户端）
    block = _text_block(str(content))
    return [block] if block else []


def _tool_use_block(call) -> dict:
    """OpenAI assistant.tool_calls[i] → Anthropic tool_use block（spec:563）。"""
    if not isinstance(call, dict):
        raise PayloadError("tool_calls entry must be an object")
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name") or call.get("name") or "")
    if not name:
        raise PayloadError("tool_calls entry without function.name")
    raw_args = fn.get("arguments")
    parsed: object = {}
    if isinstance(raw_args, dict):
        parsed = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            # 客户端上一轮给了半截参数：Anthropic 要求 input 是对象 ⇒ 退化成
            # {"_raw": ...}，**不静默丢**，让模型至少看见原文。
            parsed = {"_raw": raw_args}
    if not isinstance(parsed, dict):
        parsed = {"_value": parsed}
    return {"type": "tool_use", "id": str(call.get("id") or name), "name": name, "input": parsed}


def _tool_result_block(item) -> dict:
    """OpenAI role:"tool" 消息 → Anthropic tool_result block（按 tool_call_id 关联，spec:563,688）。

    Anthropic 语义里 tool_result 必须挂在 **user** 消息上（官方方言），调用侧的
    分组由 _translate_messages 完成。
    """
    if not isinstance(item, dict):
        raise PayloadError("tool message must be an object")
    call_id = str(item.get("tool_call_id") or item.get("call_id") or "")
    if not call_id:
        raise PayloadError("role='tool' message lacks tool_call_id (spec:563 tool_result linkage)")
    content = item.get("content")
    blocks = _blocks_from_openai_content(content, None) if content is not None else []
    block: dict = {"type": "tool_result", "tool_use_id": call_id}
    if blocks:
        block["content"] = blocks
    else:
        # 空结果：Anthropic 允许 content 缺省，但显式给空文本更稳（官方行为）。
        block["content"] = [{"type": "text", "text": ""}]
    return block


def _append_message(messages: list[dict], role: str, blocks: list[dict], tool_only: bool = False) -> None:
    """追加消息；**合并相邻同角色**消息。

    Anthropic 官方约束：messages 必须 user/assistant 交替、同角色相邻会 400。
    该约束属"Anthropic Messages 兼容"的题中之义（spec:12），spec 未单列 ⇒ 在此
    做无信息损失的并块，而不是把客户端的多条同角色消息原样送去撞 400。
    tool_result 合并进同一条 user 消息也顺带满足 spec:563 的关联语义。
    """
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
        return
    messages.append({"role": role, "content": list(blocks), "_tool_only": tool_only})


def _translate_messages(payload: dict, entry) -> tuple[list[dict], list[dict]]:
    """OpenAI messages → (system_blocks, anthropic_messages)。

    - role system/developer → 顶层 ``system`` 的 text block 数组（spec:176,691：
      system 是**数组**，元素 {type:"text",text:...}）。system 内出现非文本零件
      会被降级为文本占位或丢弃（Anthropic 官方 system 只支持 text block，spec 未
      记录 MiniMax 扩展；TODO 属未实证点）。
    - assistant 的 tool_calls → 同一 assistant 消息里的 tool_use block。
    - role tool → user 消息里的 tool_result block；**连续**的 tool 消息合并进同一条
      user 消息（Anthropic 允许一条 user 消息携带多个 tool_result，spec:563 的
      tool_result 关联语义）。
    """
    system_blocks: list[dict] = []
    messages: list[dict] = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        if role in ("system", "developer"):
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    ptype = str(part.get("type") or "") if isinstance(part, dict) else ""
                    if isinstance(part, dict) and ptype not in ("text", "input_text", ""):
                        # Anthropic 官方 system 只支持 text block（spec 未记录 MiniMax
                        # 扩展）⇒ document 显式拒绝（spec:562），其余非文本零件不猜形状、
                        # 尽力降级为文本占位，避免整条 system 静默丢内容。
                        if ptype in ("document", "file", "input_file"):
                            _reject_document_part(ptype)
                        continue
                    block = _text_block(_part_text(part), _cache_control_of(part))
                    if block:
                        system_blocks.append(block)
            else:
                text = _part_text(content) if content is not None else ""
                if not text and content is not None and not isinstance(content, (dict, list)):
                    text = str(content)
                block = _text_block(text, _cache_control_of(item))
                if block:
                    system_blocks.append(block)
            continue
        if role == "tool":
            _append_message(messages, "user", [_tool_result_block(item)], tool_only=True)
            continue
        if role == "assistant":
            blocks = _blocks_from_openai_content(item.get("content"), entry)
            for call in item.get("tool_calls") or []:
                blocks.append(_tool_use_block(call))
            if not blocks:
                continue  # 空气泡：Anthropic 会 400，直接丢（无信息损失）
            _append_message(messages, "assistant", blocks)
            continue
        # user（含未知角色一律按 user 处理，宽容接住三方客户端）
        blocks = _blocks_from_openai_content(item.get("content"), entry)
        _append_message(messages, "user", blocks)
    for message in messages:
        message.pop("_tool_only", None)
    return system_blocks, messages


def _map_tools(tools) -> list[dict]:
    """OpenAI tools[] → Anthropic tools[]（{name,description,input_schema}，spec:563）。

    TODO(spec:349,563)：eager_input_streaming（constants.EAGER_INPUT_STREAMING=True
    仅是客户端能力位）属 anthropic-beta 细粒度工具流式特性，spec:349 表明该 beta 头
    在 MiniMax 路径**通常不下发** ⇒ 本期不给 tools[] 加该字段。
    """
    out: list[dict] = []
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(fn.get("name") or "")
        if not name:
            raise PayloadError("tools[] entry lacks function.name")
        schema = fn.get("parameters")
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}}  # Anthropic 要求 input_schema 对象
        tool: dict = {
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": schema,  # spec:563 input_schema{type:object,properties,required}
        }
        cc = _sanitize_cache_control(_cache_control_of(fn) or _cache_control_of(item))
        if cc:
            tool["cache_control"] = cc  # spec:563 末位工具可挂 cache_control（调用方已挂则保留）
        out.append(tool)
    return out


def _map_tool_choice(value, has_tools: bool):
    """OpenAI tool_choice → Anthropic tool_choice（spec:563：{type:'auto'|'any'|...}）。

    上游对 tool_choice 是**原样透传**语义（constants.py:417-420 已核对解包副本
    pi-ai anthropic.js:794-801），spec:563 的省略号不是封闭集合；下面只映射有实证
    的目标，none 用「整体不发 tools」实现。返回 (tool_choice, note)；
    note == "drop_tools" 时调用方须去掉 tools 字段。
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "none":
            # Anthropic 无实证 none 值（spec:563 只列 auto|any）：
            # TODO(spec:703-708)：网关是否吃 {type:"none"} 静态无法确认 ⇒ 本期不发
            # tools 整体实现「禁用工具」语义，实测支持后可改回透传。
            return None, "drop_tools"
        if lowered == "required":
            return ({"type": "any"} if has_tools else None), None  # spec:563 any = 必须调
        if lowered in ("auto", "any"):
            return ({"type": lowered} if has_tools else None), None
        if lowered:
            return ({"type": lowered} if has_tools else None), None  # 透传（与客户端同语义）
        return None, None
    if isinstance(value, dict):
        typ = str(value.get("type") or "").strip().lower()
        if typ == "none":
            return None, "drop_tools"
        if not has_tools:
            return None, None
        if typ in ("auto", "any"):
            return {"type": typ}, None
        if typ == "required":
            return {"type": "any"}, None
        if typ == "function":
            fn = value.get("function") if isinstance(value.get("function"), dict) else {}
            name = str(fn.get("name") or value.get("name") or "")
            if not name:
                raise PayloadError("tool_choice function without name")
            # {"type":"tool","name":...} 是 Anthropic 官方指定函数形状；spec:563 的
            # 省略号未枚举 ⇒ TODO：待实测，但这是官方方言的既定写法，非编造。
            return {"type": "tool", "name": name}, None
        if typ == "tool" and value.get("name"):
            return {"type": "tool", "name": str(value["name"])}, None
        raise PayloadError(f"unsupported tool_choice shape: {typ or value!r}")
    return None, None


def _resolve_thinking(model: str, payload: dict, entry) -> dict | None:
    """OpenAI 侧开关 → Anthropic ``thinking``（spec:441,507-509,526-533,691）。

    二元 on/off ⇒ ``{type:"adaptive"}`` / ``{type:"disabled"}``（spec:441 原文
    ``payload.thinking = { type: selection.enabled ? 'adaptive' : 'disabled' }`）。

    **只有 M3 发**：spec:526-533 的实证只覆盖 MiniMax-M3；M2.7 系目录没有
    thinking_config（spec:512-515）⇒ 见 THINKING_MODEL_IDS 的可配置 TODO。
    且目录思考控制语义必须是 on/off 开关（constants.THINKING_CONTROL_ON_OFF，
    spec:527-528），否则不发（宁缺毋滥）。

    ⚠️ 绝不把 ``reasoning_effort`` 引进这条路径：``reasoning:{effort:...}`` 只在
    openai-responses 方言存在（spec:530），Anthropic 路径的 MiniMax 思考是开关
    不是档位（spec:524-533）。网关上游若注入了 reasoning_effort，本函数**故意无视**
    （TODO：如将来要映射 effort→output_config.effort，那是通用 Anthropic 路径的
    事，见 spec:541-544，与 MiniMax 受管开关无关，开关 SEND_OUTPUT_CONFIG_EFFORT）。
    """
    if model not in THINKING_MODEL_IDS:
        return None
    if entry is not None:
        tcfg = entry.get("thinking") or {}
        if tcfg.get("control") != THINKING_CONTROL_ON_OFF:
            return None  # 非 on/off 语义 ⇒ 不猜（spec:527-528）
    enabled: bool | None = None
    raw = payload.get("thinking")
    if isinstance(raw, dict):
        typ = str(raw.get("type") or "").strip().lower()
        if typ in ("adaptive", "enabled", "on", "true"):
            enabled = True
        elif typ in ("disabled", "off", "false"):
            enabled = False
        if enabled is None and isinstance(raw.get("enabled"), bool):
            enabled = raw["enabled"]
    elif isinstance(raw, bool):
        enabled = raw
    elif isinstance(raw, str):
        typ = raw.strip().lower()
        if typ in ("on", "adaptive", "enabled", "true"):
            enabled = True
        elif typ in ("off", "disabled", "false"):
            enabled = False
    if enabled is None and isinstance(payload.get("thinking_enabled"), bool):
        enabled = payload["thinking_enabled"]  # 网关自有布尔位（可配置入口）
    if enabled is None:
        # 通用 bool 位兜底；reasoning_effort **故意不读**（见 docstring 红线）。
        for key in ("reasoning", "include_reasoning"):
            value = payload.get(key)
            if isinstance(value, bool):
                enabled = value
                break
            if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
                enabled = value["enabled"]
                break
    if enabled is None:
        default = (entry or {}).get("thinking", {}).get("default_enabled")
        enabled = DEFAULT_THINKING_ENABLED if default is None else bool(default)
    return {"type": THINKING_TYPE_ADAPTIVE if enabled else THINKING_TYPE_DISABLED}


def _resolve_max_tokens(payload: dict, entry) -> int:
    """max_tokens 必填（spec:176,691）；缺失兜底 DEFAULT_MAX_TOKENS，超目录上限 clamp。

    兜底理由（任务要求写明注释）：Anthropic 的 max_tokens 是必填项，而 OpenAI 的
    max_tokens 可缺省 —— 缺省时必须造一个正整数。spec:691 只写 ``<正整数>`` 未给
    默认 ⇒ 网关自择 32000（DEFAULT_MAX_TOKENS，可配置），低于所有模型 limit.output
    =128000（spec:503,513,515），对三档模型都合法。
    """
    cap = entry.get("max_output_tokens") if entry else None
    raw = payload.get("max_tokens")
    if raw is None:
        raw = payload.get("max_completion_tokens")  # 新版 OpenAI 字段名（官方方言同义）
    if isinstance(raw, bool):
        raw = None
    value = None
    if isinstance(raw, (int, float, str)):
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = None
    if value is None or value <= 0:
        value = DEFAULT_MAX_TOKENS
    if isinstance(cap, (int, float)) and cap > 0 and value > int(cap):
        # 超过模型输出上限 Anthropic 会 400（官方约束），clamp 比报错更贴合客户端预期。
        value = int(cap)
    return value


def _output_config_from_response_format(payload: dict) -> dict | None:
    """OpenAI ``response_format`` → Anthropic ``output_config.format``（spec:565）。

    spec:565：``payload.output_config.format = {type:'json_schema', schema}``；
    ``json_object`` 需要目录声明 support_json_object_output —— 内置目录**未**声明
    （constants.SUPPORT_JSON_OBJECT_OUTPUT=False）⇒ 显式报错而不是降级成"尽力而为"。
    TODO(spec:544,565)：output_config.effort 属通用 Anthropic 档位路径，受管
    MiniMax 不发（SEND_OUTPUT_CONFIG_EFFORT=False）。
    """
    rf = payload.get("response_format")
    if not isinstance(rf, dict):
        return None
    typ = str(rf.get("type") or "").strip().lower()
    if typ in ("", "text"):
        return None
    if typ == "json_schema":
        inner = rf.get("json_schema") if isinstance(rf.get("json_schema"), dict) else {}
        schema = inner.get("schema") or rf.get("schema")
        if not isinstance(schema, dict):
            raise PayloadError("response_format json_schema without schema object")
        return {"format": {"type": OUTPUT_CONFIG_FORMAT_TYPE_JSON_SCHEMA, "schema": schema}}
    if typ == "json_object":
        if not SUPPORT_JSON_OBJECT_OUTPUT:  # spec:565
            raise PayloadError(
                "response_format json_object requires catalog flag "
                "support_json_object_output which MiniMax managed models do not declare (spec:565)"
            )
        return None  # 若开关打开，本期仍只支持 json_schema 形态（TODO spec:565）
    raise PayloadError(f"unsupported response_format type {typ!r}")


def build_anthropic_payload(inner_model, openai_payload) -> dict:
    """OpenAI Chat Completions 请求体 → MiniMax Code 的 Anthropic Messages 请求体。

    映射逐条出处（任务点名的 §1.4 body 形状 + §4 明文 + §5 thinking/output_config）：
    * body 形状 spec:176,426,691：``model/messages/system(数组)/max_tokens/stream/
      temperature/tools/tool_choice/thinking/output_config/metadata``；
      ``presence_penalty``/``frequency_penalty``/``n``/``seed``/``logprobs`` 在
      Anthropic 方言**不存在** ⇒ 不发（spec:426 的客户端构造集里也没有）。
    * 明文直发：请求体不存在任何编码/加密/签名层（spec:417-487,695）。
    * system 抽取：OpenAI 把系统提示混在 messages 里，Anthropic 要求顶层数组
      （spec:688 落地要点 1；spec:691 元素形状 {type:"text",text:...}）。
    * content block 化 / tool_use / tool_result：spec:563,688。
    * max_tokens 必填 + 兜底：spec:176,691（见 _resolve_max_tokens 注释）。
    * 思考 on/off：spec:441,524-533,691 —— 仅 M3；``reasoning.effort`` 不引
      （spec:530 属 openai-responses 方言）。
    * cache_control 只留 {type:"ephemeral"}、**ttl 剥掉**：spec:564。
    * **不发** document：spec:562（PDF 走本地转换，本期未实现 ⇒ 显式拒绝）。
    * 体积/附件上限 spec:554-557：超限**直接报错**（本期不做 files/upload，
      TODO 见 ``FILES_UPLOAD_PATH`` = spec:135 的 /mavis/api/v1/llm/v1/files/upload）。
    * 目录外模型 id **透传**（网关是 Anthropic 兼容面，spec:12；TODO spec:520,710：
      远端目录 models-dev 可能给出内置三项之外的 id，能力校验退化为宽松模式）。

    ``stream``：与入参一致（客户端要流式则 True）。网关若对外是非流式请求，由
    chat.py 照常以 stream=True 打上游再用状态机聚合 —— 上游是否接受 stream:false
    属 spec:703-708 的未确认项。
    """
    if not isinstance(openai_payload, dict):
        raise PayloadError("openai_payload must be a dict")
    payload = openai_payload

    model = _normalize_model_ref(inner_model if inner_model else payload.get("model"))
    model = model or DEFAULT_MODEL
    entry = model_entry(model)

    system_blocks, messages = _translate_messages(payload, entry)
    if not messages:
        raise PayloadError("no usable messages (Anthropic requires a non-empty messages array)")

    anthropic: dict = {
        "model": model,          # spec:176,691（裸 id，去掉 minimax/ 前缀，spec:518）
        "messages": messages,    # spec:176
        "max_tokens": _resolve_max_tokens(payload, entry),  # spec:176,691 必填正整数
        "stream": bool(payload.get("stream", True)),  # spec:176,691（见 docstring）
    }
    if system_blocks:
        anthropic["system"] = system_blocks  # spec:176,691 顶层 system 数组

    # 采样参数透传（spec:176 temperature；top_p 同族官方字段；spec:501,512-515
    # 三模型均 temperature:true）。Anthropic 的 top_k 不在 OpenAI 请求里，忽略。
    for key in ("temperature", "top_p"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            anthropic[key] = value
    if PASS_THROUGH_STOP_SEQUENCES:
        stop = payload.get("stop")
        if isinstance(stop, str) and stop:
            anthropic["stop_sequences"] = [stop]
        elif isinstance(stop, list):
            seqs = [str(s) for s in stop if s]
            if seqs:
                anthropic["stop_sequences"] = seqs

    tools = _map_tools(payload.get("tools"))
    tool_choice, choice_note = _map_tool_choice(payload.get("tool_choice"), bool(tools))
    if choice_note == "drop_tools":
        tools = []          # none：整体不发工具（见 _map_tool_choice 的 TODO）
        tool_choice = None
    if tools:
        anthropic["tools"] = tools
    if tool_choice:
        anthropic["tool_choice"] = tool_choice

    thinking = _resolve_thinking(model, payload, entry)
    if thinking:
        anthropic["thinking"] = thinking  # spec:441,531,691

    output_config = _output_config_from_response_format(payload)
    if output_config:
        anthropic["output_config"] = output_config  # spec:176,565

    user = payload.get("user")
    if isinstance(user, (str, int)) and str(user):
        anthropic["metadata"] = {"user_id": str(user)}  # spec:176 metadata.user_id

    # ---- 附件数量闸门（spec:557）----
    image_count = sum(
        1
        for message in anthropic["messages"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "image"
    )
    if image_count > MAX_ATTACHMENTS_COUNT:
        # TODO(spec:561,710)：超阈值附件应先 POST {host}{FILES_UPLOAD_PATH} 上传换
        # file id 再引用（multipart 字段名与 files_api_ref_scheme 静态未确认）；
        # 本期不实现 ⇒ 报错而不是静默截断。
        raise PayloadError(
            f"{image_count} inline attachments exceed max_attachments_count="
            f"{MAX_ATTACHMENTS_COUNT} (spec:557); files/upload path {FILES_UPLOAD_PATH} "
            "is not implemented this iteration"
        )

    # ---- 明文 JSON 体积闸门（spec:556,466：64 MiB 按明文 JSON 计，无压缩层）----
    # 内联媒体总量另受 max_image_bytes_inline / max_video_bytes_inline 约束
    # （spec:554-555；video 本身已在 _reject_video_part 处拒绝，这里只兜 image）。
    try:
        serialized = json.dumps(anthropic, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"request body is not JSON-serializable: {exc}") from exc
    if len(serialized.encode("utf-8")) > MAX_REQUEST_BODY_BYTES:
        raise PayloadError(
            f"plaintext request body exceeds max_request_body_bytes="
            f"{MAX_REQUEST_BODY_BYTES} (spec:556); TODO: offload attachments via "
            f"{FILES_UPLOAD_PATH} (spec:135,561), not implemented this iteration"
        )
    return anthropic


# ============================================================
# 下行：Anthropic SSE 事件 → OpenAI chunk（可重入状态机，spec §6）
# ============================================================

class AnthropicStreamState:
    """Anthropic 流式事件的累积状态（形态参考 qodercn.chat._collect 的 state dict）。

    可重入：同一 state 对象可被任意顺序多次 ``feed_event``；``finish_state`` 做
    硬结束校验（spec:591）。字段即诊断面：chat.py 出错时可以把这些标志位原样
    写进日志（不含任何凭证）。
    """

    __slots__ = (
        "model", "chunk_id", "created",
        "saw_message_start", "saw_message_stop", "role_sent", "finish_emitted",
        "finish", "finish_raw",
        "usage_start", "usage_delta", "usage_final",
        "blocks", "next_tool_index",
        "text_parts", "reasoning_parts", "tool_calls",
        "saw_content", "saw_reasoning", "saw_tool",
        # finish_state 产出的终结 chunk（由 get_terminal_chunk 取走）。
        # ⚠️ 用了 __slots__ 就必须把名字登记在这里 —— 漏掉会让 finish_state
        # 赋值时抛 AttributeError（整条流式收尾全崩）。
        "terminal_chunk",
    )

    def __init__(self, model: str, chunk_id: str | None = None, created: int | None = None):
        self.model = str(model or DEFAULT_MODEL)   # 出口 chunk 的 model 用**请求名**覆盖
        self.chunk_id = chunk_id or f"chatcmpl-{CHANNEL_ID}"  # message_start 后会换成上游 id
        self.created = int(created or time.time())
        self.saw_message_start = False   # spec:587
        self.saw_message_stop = False    # spec:588,591 硬结束标志
        self.role_sent = False           # 首块 role（与 qodercn._collect 同语义）
        self.finish_emitted = False      # 终结 chunk 是否已由 finish_state 发出
        self.finish: str | None = None       # 映射后的 finish_reason（spec:594）
        self.finish_raw: str | None = None   # Anthropic 原词（诊断用）
        self.usage_start: dict | None = None    # message_start.message.usage（spec:593）
        self.usage_delta: dict | None = None    # message_delta.usage（可能缺字段，spec:596）
        self.usage_final: dict | None = None    # normalize_usage 的产物（finish_state 落定）
        self.blocks: dict[int, dict] = {}       # content_block index → 块累加状态
        self.next_tool_index = 0                # OpenAI tool_calls[].index 分配器
        self.text_parts: list[str] = []         # 非流式聚合用
        self.reasoning_parts: list[str] = []
        self.tool_calls: list[dict] = []        # {"id","name","arguments"} 累加
        self.saw_content = False
        self.saw_reasoning = False
        self.saw_tool = False
        self.terminal_chunk: dict | None = None  # finish_state 写、get_terminal_chunk 取

    def make_chunk(self, delta: dict | None = None, finish_reason: str | None = None,
                   usage: dict | None = None) -> dict:
        """组装标准 OpenAI ``chat.completion.chunk``（同 qodercn.openai_chunk 形态）。

        model 一律用**请求名**覆盖（spec:594 之后 Anthropic 事件里没有可信 model
        口径；与仓库各家一致的防呆做法）。
        """
        choice: dict = {"index": 0, "delta": delta or {}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        out: dict = {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [choice],
        }
        if usage is not None:
            out["usage"] = usage
        return out

    def merged_usage(self) -> dict:
        """message_delta 缺字段用 message_start 兜底再归一（spec:588 附近,596）。

        spec:596 原文：「message_delta 可能不带 input_tokens，客户端用
        message_start 的值兜底 ⇒ 网关允许 usage 分批下发」。
        逐字段合并：delta 有值取 delta，否则回退 start（多次 message_delta 已在
        feed 里合并更新，这里只处理"缺字段"）。
        """
        merged: dict = {}
        for field in USAGE_FIELDS:
            value = None
            if isinstance(self.usage_delta, dict) and self.usage_delta.get(field) is not None:
                value = self.usage_delta.get(field)
            elif isinstance(self.usage_start, dict) and self.usage_start.get(field) is not None:
                value = self.usage_start.get(field)
            if value is not None:
                merged[field] = value
        self.usage_final = normalize_usage(merged)
        return self.usage_final


def _describe_upstream_error(event: dict) -> tuple[str, object]:
    """从错误负载里抠 (message, code)，兼容三种嵌套（spec:655：
    {status_code,status_msg} / {statusInfo:{code,message}} / {error:{...}}）。

    **内层业务码优先**于传输级码（spec:648-654）：candidates 的排列把
    error/statusInfo/base_resp 内层放在外层之前。
    截断到 240 字符；上游错误体不含本端凭证，仍避免整包原样进异常。
    """
    candidates: list[object] = []
    inner = event.get("error")
    if isinstance(inner, dict):
        candidates.append(inner)
    status_info = event.get("statusInfo")
    if isinstance(status_info, dict):
        candidates.append(status_info)
    if event.get("status_code") is not None or event.get("status_msg") is not None:
        candidates.append(event)  # MiniMax base_resp 平铺形状（spec:653）
    candidates.append(event)
    message = ""
    code = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if code is None:
            code = item.get("code") if item.get("code") is not None else item.get("status_code")
        if not message:
            for key in ("message", "status_msg", "type"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    message = value
                    break
        if message and code is not None:
            break
    if not message:
        try:
            message = json.dumps(event, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            message = str(event)
    # Anthropic 语义错误类型 → MiniMax 业务码（spec:584,648-654 的内层码优先原则）。
    # 流内 error 事件通常只带 ``error.type``（如 overloaded_error/rate_limit_error），
    # 没有数字码；不在这里补映射的话，对外错误帧会缺 ``code``，
    # 客户端与观测面都无法按业务码分类（自检 spec:584 那例即此路径）。
    if code is None:
        code = ANTHROPIC_ERROR_TYPE_TO_CODE.get(str(_error_type_of(event)).strip())
    return message[:240], code


def _error_type_of(event: dict) -> str:
    """取出 Anthropic 错误语义类型（``error.type`` 优先，其次顶层 ``type``）。"""
    inner = event.get("error")
    if isinstance(inner, dict):
        value = inner.get("type")
        if isinstance(value, str) and value:
            return value
    value = event.get("type")
    return value if isinstance(value, str) else ""


def _index_of(event: dict) -> int:
    value = event.get("index")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def feed_event(state: AnthropicStreamState, event) -> list[dict]:
    """喂入**一条已解析的 Anthropic 事件对象**，返回 0..n 条 OpenAI chunk。

    ⚠️ 分类只看 ``data["type"]``：**不能**依赖 ``event:`` 行 —— 仓库的 SSEDecoder
    只吐 data 字段、丢弃 event 行（upstream/sse.py:105 与模块 docstring），而
    Anthropic 每条事件 JSON 内都自带 type（spec:586-588 客户端同样按 event.type
    分派；spec:574 的官方解析器甚至兼容多行 data 与 \\r 分行）。

    支持事件（spec §6:578-593 白名单 + spec:584 的 error）：
      message_start / content_block_start / content_block_delta(text_delta、
      thinking_delta→reasoning_content、input_json_delta→tool_calls.arguments、
      signature_delta 丢弃) / content_block_stop / message_delta(stop_reason+usage) /
      message_stop / ping(丢弃) / error(抛 AnthropicStreamError)。

    **终结 chunk 不在这里发**：message_stop 只置位，终结块（finish_reason +
    合并 usage）统一由 ``finish_state`` 发出 —— 保证「所有增量 → 终结块 →
    DONE_EVENT」的出口顺序，且截断流永远等不到终结块（spec:591 显式判错）。
    """
    if not isinstance(event, dict):
        return []
    etype = event.get("type")  # spec:586 —— 唯一可信分类字段（见上铁律）

    if etype == SSE_EVENT_MESSAGE_START:
        # spec:593：message_start.message 含 id/model/usage.input_tokens。
        first = not state.saw_message_start
        state.saw_message_start = True  # spec:587
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        mid = message.get("id")
        if isinstance(mid, str) and mid:
            state.chunk_id = mid
        usage = message.get("usage")
        if isinstance(usage, dict):
            state.usage_start = usage
        if first and not state.role_sent:
            # 首块：role（与 qodercn._collect 的 role_sent 语义一致）。
            state.role_sent = True
            return [state.make_chunk(delta={"role": "assistant"})]
        return []

    if etype == SSE_EVENT_CONTENT_BLOCK_START:
        block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
        index = _index_of(event)
        btype = str(block.get("type") or "")
        entry = {"type": btype, "json": "", "tool_index": None, "tool": None}
        state.blocks[index] = entry
        if btype == "tool_use":  # spec:563：响应 tool_use block；流式先报 id/name
            tool_index = state.next_tool_index
            state.next_tool_index += 1
            entry["tool_index"] = tool_index
            call = {
                "id": str(block.get("id") or f"call_{tool_index}"),
                "name": str(block.get("name") or ""),
                "arguments": "",
            }
            entry["tool"] = call
            state.tool_calls.append(call)
            state.saw_tool = True
            return [state.make_chunk(delta={
                "tool_calls": [{
                    "index": tool_index,
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"]},
                }]
            })]
        if btype == "text":
            # 某些网关会在 start 块里预填 text（Anthropic 官方为空串），非空则转发，
            # 与后续 text_delta 不重叠（官方协议 start 不携带增量）。
            prefill = block.get("text")
            if isinstance(prefill, str) and prefill:
                state.text_parts.append(prefill)
                state.saw_content = True
                return [state.make_chunk(delta={"content": prefill})]
        if btype == "thinking":
            prefill = block.get("thinking")
            if isinstance(prefill, str) and prefill:
                state.reasoning_parts.append(prefill)
                state.saw_reasoning = True
                return [state.make_chunk(delta={"reasoning_content": prefill})]
        return []

    if etype == SSE_EVENT_CONTENT_BLOCK_DELTA:
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        dtype = str(delta.get("type") or "")
        index = _index_of(event)
        entry = state.blocks.get(index)
        if entry is None:
            # 未见 start 的孤儿 delta：补建条目（不崩；官方顺序是 start→delta）。
            entry = {"type": "", "json": "", "tool_index": None, "tool": None}
            state.blocks[index] = entry
        if dtype == DELTA_TEXT:  # text_delta → content（spec:593）
            text = delta.get("text")
            if isinstance(text, str) and text:
                state.text_parts.append(text)
                state.saw_content = True
                return [state.make_chunk(delta={"content": text})]
            return []
        if dtype == DELTA_THINKING:  # thinking_delta → reasoning_content（spec:593）
            piece = delta.get("thinking")
            if not isinstance(piece, str):
                piece = delta.get("text")
            if isinstance(piece, str) and piece:
                state.reasoning_parts.append(piece)
                state.saw_reasoning = True
                return [state.make_chunk(delta={"reasoning_content": piece})]
            return []
        if dtype == DELTA_INPUT_JSON:  # input_json_delta → tool_calls.arguments（spec:593,563）
            # spec:563：客户端在块末对累加的 partial_json 做 JSON.parse；我们出口
            # 保持**原样透传**（OpenAI 的 arguments 本就是流式字符串片段），
            # 翻译层不越权改写。
            piece = delta.get("partial_json")
            if isinstance(piece, str) and piece:
                entry["json"] += piece
                if entry["tool"] is not None:
                    entry["tool"]["arguments"] += piece
                return [state.make_chunk(delta={
                    "tool_calls": [{
                        "index": entry["tool_index"] if entry["tool_index"] is not None else 0,
                        "function": {"arguments": piece},
                    }]
                })]
            return []
        if dtype == DELTA_SIGNATURE:
            # signature_delta（spec:593）：thinking 块的防篡改签名。OpenAI 方言
            # 没有对应槽位，也不参与内容渲染 ⇒ 丢弃（显式分支只为声明处理过）。
            return []
        return []  # 未知 delta 类型：与 spec:585 的宽容忽略一致

    if etype == SSE_EVENT_CONTENT_BLOCK_STOP:
        entry = state.blocks.get(_index_of(event))
        if entry is not None:
            entry["stopped"] = True
        return []

    if etype == SSE_EVENT_MESSAGE_DELTA:
        # spec:593：含 delta.stop_reason 与 usage 终值；usage 允许分批（spec:596），
        # 这里**累积合并**而不是整体覆盖（多次 message_delta 也正确）。
        usage = event.get("usage")
        if isinstance(usage, dict):
            merged = dict(state.usage_delta or {})
            merged.update(usage)
            state.usage_delta = merged
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        stop_reason = delta.get("stop_reason")
        if stop_reason is not None:
            state.finish_raw = str(stop_reason)
            state.finish = map_stop_reason(stop_reason)  # spec:594
        return []

    if etype == SSE_EVENT_MESSAGE_STOP:
        # spec:588,591：硬结束标志，只置位；终结 chunk 由 finish_state 统一发。
        state.saw_message_stop = True
        return []

    if etype == "ping":
        # spec:585：ping 等心跳被官方客户端忽略，这里同样丢弃。
        return []

    if etype == SSE_EVENT_ERROR or "error" in event:
        # spec:584：客户端行为 = throw new Error(sse.data)。
        message, code = _describe_upstream_error(event)
        raise AnthropicStreamError(f"upstream stream error: {message}", code=code)

    return []  # 白名单外的私有事件静默忽略（spec:585,708）


def parse_event_data(data) -> object | None:
    """把 SSEDecoder 吐出的一条 data 载荷（bytes/str）解析成事件对象。

    spec:586：官方客户端用 ``parseJsonWithRepair``；这里用严格 json.loads ——
    解析失败返回 None（调用方选择忽略或报错），不引入 repair 依赖。
    载荷若是字面量 ``[DONE]``（理论不该出现，Anthropic 侧无此哨兵，spec:595），
    同样返回 None。
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(data, str):
        text = data
    else:
        return None
    text = text.strip()
    if not text or text == "[DONE]":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def feed(state: AnthropicStreamState, data) -> list[dict]:
    """``parse_event_data`` + ``feed_event`` 的便捷组合（chat.py 直接喂 data 载荷）。"""
    event = parse_event_data(data)
    if event is None:
        return []
    return feed_event(state, event)


def finish_state(state: AnthropicStreamState) -> dict:
    """流读完后的硬校验：发出终结 chunk 并返回归一 usage。

    缺 ``message_stop`` ⇒ 抛 :class:`AnthropicStreamTruncatedError`：官方客户端就是
    这么做的（spec:591 "Anthropic stream ended before message_stop"），对外必须给
    清晰 5xx，**绝不**静默把半截回复当成功返回（spec:595）。

    调用序约定（chat.py）：``feed_event``/``feed`` 的返回值全部下发之后才调本函数；
    本函数返回的 chunk 由调用方下发后，出口补 ``DONE_EVENT``（对外 OpenAI 方言的
    ``data: [DONE]`` 由我们加 —— 上游没有这个哨兵，spec:595）。

    返回**同一个 usage dict** 也写进了终结 chunk（finish_reason + usage 合体，
    与各家通道的 usage 终块语义一致）。
    """
    if not state.saw_message_stop:
        raise AnthropicStreamTruncatedError(
            "Anthropic stream ended before message_stop (spec:591,595); "
            f"stage=saw_message_start={state.saw_message_start} "
            f"blocks={len(state.blocks)} finish_raw={state.finish_raw!r}"
        )
    if not state.saw_message_start:
        # spec:587 也跟踪了 message_start；只有 stop 没有 start 同样不可诊断。
        raise AnthropicStreamTruncatedError(
            "Anthropic stream ended before message_start (spec:587)"
        )
    usage = state.merged_usage()  # spec:596：delta 缺 input_tokens 用 start 兜底
    if not state.finish_emitted:
        state.finish_emitted = True
        finish = state.finish
        if finish is None:
            # 网关没发 stop_reason 就 message_stop（spec 未记录该形态）：按已观测
            # 内容推断 —— 有工具调用且无正文 ⇒ tool_calls，否则 stop。
            finish = "tool_calls" if state.saw_tool and not state.saw_content else "stop"
        state.terminal_chunk = state.make_chunk(delta={}, finish_reason=finish, usage=usage)
    # 重复调用**不**清空已挂起的终结 chunk：终结块只由 get_terminal_chunk 取走一次。
    # （若在这里置 None，chat.py 误调两次 finish_state 就会把待发的 finish_reason
    # 终块吞掉 —— 客户端表现为流没有结束块。）
    return usage


def get_terminal_chunk(state: AnthropicStreamState) -> dict | None:
    """取走 finish_state 生成的终结 chunk（重复调用返回 None，保证只发一次）。

    独立函数而非让 finish_state 返回它：保持任务点名的
    ``finish_state(state) -> usage`` 稳定签名；chat.py 用法::

        usage = finish_state(state)
        terminal = get_terminal_chunk(state)
        if terminal: yield sse_bytes(terminal)
        yield DONE_EVENT
    """
    chunk = state.terminal_chunk
    state.terminal_chunk = None
    return chunk


# ============================================================
# 出口 SSE 序列化（供 chat.py 收尾用）
# ============================================================

def sse_bytes(chunk: dict) -> bytes:
    """一条 OpenAI chunk → ``data: <json>\\n\\n``。"""
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


# 对外流式收尾哨兵。注意方向性：**上游 Anthropic 没有** [DONE]（spec:595，结束只看
# message_stop），这个是**我们网关出口**（OpenAI 方言）必须补的收尾行（任务约束 7）。
DONE_EVENT = b"data: [DONE]\n\n"


# ============================================================
# 下行：非流式 Anthropic JSON → OpenAI chat.completion（spec:176 响应孪生）
# ============================================================

def to_openai_completion(anthropic_body, requested_model: str | None = None) -> dict:
    """Anthropic Messages JSON 响应 → OpenAI ``chat.completion``（非流式出口）。

    形状出处 spec:176（请求侧 body 的响应孪生）与 §6:593 的事件内层语义：
      content 数组里 ``text`` 块拼成 content；``thinking`` 块拼成
      ``reasoning_content``（与 qodercn/traework 等通道的思考字段约定一致）；
      ``tool_use`` 块 → tool_calls（arguments 为 input 的 JSON 序列化）；
      ``stop_reason`` → finish_reason（spec:594 的映射）；
      ``usage`` 四字段 → OpenAI usage，total = 四项之和（spec:619）。

    本函数主要服务测试 fixture 与"上游返回非流式 JSON"的兜底路径；受管主路径是
    流式（spec §6）。若 chat.py 走"上游流式 + 本地聚合"路线，请用
    AnthropicStreamState 的 text_parts/reasoning_parts/tool_calls 拼 message，
    而不是本函数。
    """
    body = anthropic_body if isinstance(anthropic_body, dict) else {}
    texts: list[str] = []
    reasonings: list[str] = []
    tool_calls: list[dict] = []
    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
        elif btype == "thinking":
            # spec:593 的 thinking_delta 在流式对应 reasoning_content；非流式同归位。
            thought = block.get("thinking")
            if isinstance(thought, str) and thought:
                reasonings.append(thought)
        elif btype == "redacted_thinking":
            continue  # Anthropic 官方脱敏块，无正文可展示
        elif btype == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
        elif btype == DOCUMENT_BLOCK_TYPE:
            # spec:562：上游不该回这种块；真出现就当文本兜底读出来，不崩。
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)

    message: dict = {"role": "assistant", "content": "".join(texts) or None}
    if reasonings:
        message["reasoning_content"] = "".join(reasonings)
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish = map_stop_reason(body.get("stop_reason"))
    if finish is None:
        finish = "tool_calls" if tool_calls and not texts else "stop"

    created = body.get("created") if body.get("created") is not None else body.get("created_at")
    try:
        created_ts = int(created) if created is not None else int(time.time())
    except (TypeError, ValueError):
        created_ts = int(time.time())

    return {
        "id": str(body.get("id") or f"chatcmpl-{CHANNEL_ID}"),
        "object": "chat.completion",
        "created": created_ts,
        "model": str(requested_model or body.get("model") or DEFAULT_MODEL),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": normalize_usage(body.get("usage")),
    }
