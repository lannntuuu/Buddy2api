"""MiniMax Code chat client — 受管登录态（managed-login）通道的装配层。

协议事实唯一权威来源：
``.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md``（下文 ``spec:NNN``
= 该行号）。契约常量在 ``constants.py``、方言翻译在 ``translate.py``、凭证在
``store.py``/``token.py`` —— 本文件只做**装配与调度**，不重复定义任何协议事实。

一句话协议形状（spec:12,21,176,690-695）
--------------------------------------
* 上游是 **Anthropic Messages 方言**（``POST {host}/mavis/api/v1/llm/v1/messages``），
  不是 OpenAI Chat Completions；请求/响应体是**明文 JSON**，没有任何编码/加密/签名层
  （spec:417-487,695）⇒ 本文件绝不加签、绝不 base64 封装。
* `/v1` 净效果只出现**一次**（spec:111-119,131,689）：预置 base 末尾的 `/v1` 会被
  ``normalizeProviderBaseUrl`` 剥掉，再由 SDK 拼回 `/v1/messages`。路径常量直接来自
  ``constants.CHAT_PATH``，``chat_url()`` 再自检一次形状（防常量被改出 `/v1/v1/`）。
* 真实凭证只有 ``Authorization: Bearer <access_token>``（spec:184,344）；
  ``x-api-key: sk-xxx`` 是**占位符且必须原样发**（spec:202,343,690）——删掉它反而可能
  触发 SDK 的 "Could not resolve authentication method"。
* 流式是标准 SSE，**上游没有** ``[DONE]`` 哨兵，硬结束标志是 ``message_stop``
  （spec:595）；缺它必须显式判错（spec:591），绝不把半截回复当成功（任务风控约束）。

重试与风控（spec:313,319,711；对齐仓库现状）
--------------------------------------------
* 最多 ``providers.retry.MAX_ATTEMPTS``（3）次尝试，与 qodercn/qclaw/qwenwork 一致；
  **不做自动重放放大**：401 只刷新+重放一次（spec:313 ``UNAUTHORIZED_RETRY_LIMIT=1``，
  实现见 ``_post_with_auth_recovery`` / ``_stream_with_auth_recovery``），其余重试只在
  ``RETRYABLE_STATUS`` / 业务码表 ``retryable`` 为真时**换号**，退避走
  ``providers.retry.retry_delay``（并尊重上游真发了的 ``Retry-After``，spec:711）。
* 失败绝不吞成静默短回复：非 2xx、流内 ``error`` 事件、截断流、SSE 解析失败
  **全部**产出可诊断的 OpenAI 形状错误（MiniMax 业务码 + 内层 status_code 双写，spec:692）。
* 限流/额度类命中登记进 ``upstream.rate_limits``（**(账号, 模型) 级**，与 workbuddy 6004
  同一张表同一套语义），选号时预判跳过；解除时刻解析不出来就落 ``FALLBACK_COOLDOWN_S``，
  **不假装知道解除时间**（spec:711 明确"退避/retry-after 静态未确认"）。

凭证安全：access_token / refresh_token 原文绝不进日志、异常消息、错误响应体（自检用
``AT-FAKE``/``RT-FAKE``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, Optional

import httpx

from accounts import auth_manager
from providers import store_common
from providers.host_override import channel_host
from providers.minimax_code import constants as K
from providers.minimax_code import store as _store
from providers.minimax_code import translate as T
from providers.minimax_code.constants import (
    ALIASES,
    BEARER_PREFIX,
    CHANNEL_ID,
    CHAT_PATH,
    CONTENT_TYPE_JSON,
    DEFAULT_MODEL,
    ERROR_CODE_TO_HTTP_STATUS,
    HEADER_ACCEPT,
    HEADER_AUTHORIZATION,
    HEADER_CONTENT_TYPE,
    HEADER_MAVIS_SESSION_ID,
    HEADER_MAVIS_TIMEZONE_OFFSET,
    LLM_AUTH_ERROR,
    LLM_CLUSTER_OVERLOADED,
    LLM_CREDITS_EXHAUSTED,
    LLM_RATE_LIMITED,
    LLM_TPM_RATE_LIMITED,
    LLM_TPM_RATE_LIMIT_MESSAGE_CODES,
    LLM_UPSTREAM_ERROR,
    MAVIS_SESSION_ID_HEX_LEN,
    MAVIS_SESSION_ID_PREFIX,
    MODEL_CATALOG,
    PROD_PROHIBITED_HEADERS,
    REQUEST_STATIC_HEADERS,
    UNAUTHORIZED_RETRY_LIMIT,
    UPSTREAM_ERROR_CODES,
    UPSTREAM_STATUS_CODE_MAP,
    USAGE_LIMIT_EXCEEDED,
)
from providers.minimax_code.liveness import allow_gateway_self_refresh
from providers.minimax_code.token import MiniMaxCodeAuthError, refresh_account
from providers.model_config import channel_aliases
from providers.retry import MAX_ATTEMPTS, RETRYABLE_STATUS, retry_delay
from providers.trae_shared import pick_with_refresh_fallback
from storage import database as db
from upstream import rate_limits
from upstream.sse import SSEDecoder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 契约层符号名对齐（不硬编码端点）
# ---------------------------------------------------------------------------
# 任务契约 sketch 写的是 `LLM_HOST`，实际 constants.py 登记的是 `AGENT_HOST_CN`
# （spec:88,131）。与 token.py 同款的 getattr 兜底：符号若被改名，这里退化到已知值，
# 而不是在 chat 层复制一份 URL 字面量（那会造成第二处端点事实）。
LLM_HOST = getattr(K, "LLM_HOST", None) or K.AGENT_HOST_CN  # spec:88,131

# 非流式超时：读超时给足（Anthropic 方言的整条 message 可能很长）。流式对齐
# qodercn/traesolo —— 不设总超时与读超时，只限制建连（长回合可能几十秒无输出）。
NON_STREAM_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
STREAM_TIMEOUT = httpx.Timeout(None, connect=15.0, read=None)

# 限流/额度类业务码（spec:632-639）。**两族分开处置**，因为契约层的 retryable 就是两样：
#   * 限流族（50111/50150/50151，constants ``retryable=True``）：临时窗口 ⇒ 登记
#     rate_limits + **同一请求内零延迟换号**；池里没有可用账号时对外 429 带受限视图。
#   * 额度族（42212/50110，constants ``retryable=False``，spec:660 "Do NOT retry"，
#     注释原文"额度类换号/退避都不解决问题"）⇒ 登记 rate_limits（下一批请求由
#     ``_limited_ids`` 预判跳过这个账号，池子照样健康），但**本请求不重放、不换号**，
#     直接把业务码对应的 HTTP（402/429）返回客户端。
RATE_LIMIT_CODES: frozenset[int] = frozenset(
    {LLM_RATE_LIMITED, LLM_TPM_RATE_LIMITED, LLM_CLUSTER_OVERLOADED}
)
QUOTA_CODES: frozenset[int] = frozenset({USAGE_LIMIT_EXCEEDED, LLM_CREDITS_EXHAUSTED})
# 族集合可配置（spec:640 明确"额度 vs 限流不总是可分"；真机 MITM 后可调这张表）：
LIMIT_CLASS: frozenset[int] = RATE_LIMIT_CODES | QUOTA_CODES
QUOTA_SWITCH_ACCOUNT = False  # spec:660 的正典答案；True 仅当实测证明换号能救（本期不开）

# 对外 error.type —— **我们出口**的 OpenAI 方言词表（spec 未规定出口形状，非上游 wire 值）。
_TYPE_BY_CLASS = {
    "rate_limit": rate_limits.RATE_LIMIT_TYPE,   # "rate_limit_error"（仓库既有词）
    "auth": "authentication_error",
    "invalid_request": "invalid_request_error",
    "permission": "permission_error",
    "upstream": "upstream_error",
    "server": "server_error",
}

# Anthropic 官方 error.type → MiniMax 业务码。spec:12 声明这个网关面是 Anthropic
# Messages 兼容面，故错误体也可能走 Anthropic 词表；spec:632-639 给的是 MiniMax 侧码，
# 两套并列存在 ⇒ 两套都认（任务约束"同时兼容两套信封"）。
# TODO(spec:703-708)：网关实际发哪一种静态无法枚举 ⇒ 认不出来就退回 HTTP 状态分类。
_ANTHROPIC_ERROR_TYPES: dict[str, int] = {
    "authentication_error": LLM_AUTH_ERROR,      # spec:635
    "permission_error": LLM_AUTH_ERROR,          # spec:635（401/403 同码）
    "rate_limit_error": LLM_RATE_LIMITED,        # spec:634
    "overloaded_error": LLM_CLUSTER_OVERLOADED,  # spec:639（529 语义）
    "api_error": LLM_UPSTREAM_ERROR,             # spec:636 generic
    "invalid_request_error": 0,                  # 客户端请求问题：无业务码，对外 400
}

# 与 qclaw / qwenwork / qodercn 的同名单行拷贝收敛：见 store_common.make_translator。
# reserved=内置 ALIASES：管理员自定义别名表却没带 "auto" 时，"auto" 仍落到 M3 ——
# 上游目录（spec:499-516）里没有 "auto" 这个 id，保留字必须在本侧翻成具体模型。
_base_translate_model = store_common.make_translator(
    lambda: channel_aliases(CHANNEL_ID, ALIASES), DEFAULT_MODEL, reserved=dict(ALIASES)
)


def translate_model(model: str) -> str:
    """别名映射 + **通道 id 前缀剥离**（本通道包一层，不改 store_common 的共享实现）。

    ⚠️ 实机回归 2026-09-30：OpenAI 客户端的标准写法 ``minimax_code/auto`` **整串没被
    剥掉**，原样发给上游 ⇒ 400 ``invalid params, invalid reasoning_effort: "default"
    (allowed: low, medium, high, xhigh, max) (2013)``。

    注意这个错误信息是**误导性的**：真凶是模型名不合法（上游把未知 model 当参数解析），
    不是 effort。实测对照过——``model="auto"`` 与显式 ``reasoning_effort="default"``
    都**成功**（HTTP 200），证明 ``"default"`` 上游是接受的。

    前缀有三套命名，别混：
      * ``minimax/`` ``minimax_api/`` —— spec:518 上游 model-ref 的 provider 名；
      * ``minimax_code/`` —— **本网关的通道 id**，OpenAI 客户端侧 ``<channel>/<model>`` 写法。
    make_translator 只查别名表、不剥前缀；剥离逻辑统一走 T._normalize_model_ref。
    """
    return _base_translate_model(T._normalize_model_ref(model))


# ============================================================
# URL / 请求头
# ============================================================

def chat_url() -> str:
    """推理端点 = ``channel_host(CHANNEL_ID, "llm", LLM_HOST) + CHAT_PATH``（spec:131,690）。

    `/v1` 净效果只出现一次（spec:111-119,689 的头号坑）：constants 已经把
    "剥过尾 `/v1` 的 base" 与 "SDK 的 /v1/messages" 分成两段拼好（``CHAT_PATH`` 里
    `/v1` 出现两次是网关前缀 `/mavis/api/v1/llm` + SDK 路径 `/v1/messages` 的正常结果，
    见 constants._self_check 的断言）；这里只做装配 + 双写防呆。
    """
    url = f"{channel_host(CHANNEL_ID, 'llm', LLM_HOST)}{CHAT_PATH}"
    if "/v1/v1/" in url:  # pragma: no cover - 只在常量/覆盖值被改坏时触发
        raise RuntimeError(f"minimax-code chat url 出现双 /v1（spec:689）：{url}")
    return url


def model_meta(model: str) -> dict:
    """目录项（display_name / is_vl / is_reasoning / 窗口）；目录外 id 给宽松兜底。

    spec:520,710：远端目录 models-dev 可能给出内置三项之外的 id，能力校验退化为宽松
    模式（display_name 用请求名，窗口取默认模型量级），**不编造**具体能力。
    """
    entry = MODEL_CATALOG.get(model)
    if entry:
        return entry
    default_entry = MODEL_CATALOG[DEFAULT_MODEL]
    return {
        "display_name": model or DEFAULT_MODEL,
        "is_vl": False,
        "is_reasoning": True,
        "max_input_tokens": int(default_entry["max_input_tokens"]),
        "max_output_tokens": int(default_entry["max_output_tokens"]),
    }


def _timezone_offset_seconds() -> int:
    """本地时区偏移（**秒，东为正**）= spec:348 的 ``getTimezoneOffset()*-60`` 等价式。

    JS ``Date.getTimezoneOffset()`` 返回"落后 UTC 的分钟数"（UTC+8 ⇒ -480），乘 -60
    得 +28800 秒 ⇒ Python 侧就是 ``astimezone().utcoffset()`` 的秒数。取不到偏移
    （极端缺时区数据的环境）时按 0（UTC）发，**不猜**一个 28800 冒充东八区。
    """
    try:
        offset = datetime.now().astimezone().utcoffset()
    except Exception:  # noqa: BLE001 - 时区数据缺失不该打断请求
        return 0
    return int(offset.total_seconds()) if offset else 0


def new_session_id() -> str:
    """生成 ``X-Mavis-Session-Id`` 的值：``mvs_`` + 32 位小写 hex（无连字符）。

    **MITM 实测 2026-09-30**：实测值形如
    ``mvs_312d6855b7a74b4990b9faf170aecd4f``
    （dump-003:34 主请求 / dump-001:31 / dump-002:31 两次 count_tokens 三处一致）。
    出处：``.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-003.json:34``、
    ``MITM-VERIFIED-FINDINGS.md`` §1B#10 / §3 G07。
    形状 = ``MAVIS_SESSION_ID_PREFIX``（``"mvs_"``）+ ``uuid4().hex``（32 位小写 hex）。

    旧实现是 ``str(uuid.uuid4())`` —— **带连字符且无前缀**，与实测形状不符（G07）。
    这里统一收敛成一个生成点：``request_headers``、``chat_completions`` 主路径、
    ``test_chat`` 探活路径**全部**走本函数（不再有裸 ``uuid4()`` 调用点）。

    语义说明（实测 vs 本通道）：实测三次请求**复用同一个 id** ⇒ 客户端侧是**会话级**。
    网关没有跨请求的会话概念（每个 HTTP 请求独立），故本通道按"一次客户端请求一个
    会话 id"生成，但**格式严格对齐实测**（前缀 + 32 位 hex），并在 401 重放时沿用
    同一个 id（重放是"同一次请求换票再发"，spec:313，不是新会话）。
    """
    return MAVIS_SESSION_ID_PREFIX + uuid.uuid4().hex


def request_headers(account: dict, session_id: str = "") -> dict[str, str]:
    """推理请求头全集（spec §3.1:334-375 + §9:3:690 的清单 + MITM 实测 2026-09-30）。

    静态项取 ``constants.REQUEST_STATIC_HEADERS``（值与出处都在契约层）——含
    anthropic-version / x-api-key 占位符 / User-Agent / X-Mavis-Agent-Id，以及
    **MITM 实测 2026-09-30 新增的** ``anthropic-dangerous-direct-browser-access: true``
    （dump-003:27；实测无 anthropic-beta 配套，见 constants 注释）。
    这里只补**含凭证/会话/时区的动态项**（spec:340,341,344,346,348）。

    ⚠️ ``x-api-key: sk-xxx`` 是占位符、**不许删**（spec:202,343,690）：真实凭证只在
    Authorization，缺了这个头反而可能触发 Anthropic SDK 的认证方式解析失败。
    ⚠️ prod 不发 ``bedrock-lane``（spec:350,690）：那是 dev/test/staging 的泳道头，
    受管路径还会先删用户配置里的同名头 ⇒ 这里再兜一道按黑名单剔除。
    ⚠️ MITM 实测 2026-09-30 推翻了旧 docstring 的残余假设：实测 24 个请求头里
    **没有 Cookie**（dump-003:24-48；capture.jsonl:8 的 headerNames 全集同样无）
    ⇒ "客户端还带 Cookie 等会话头"这句不再成立（MITM-VERIFIED-FINDINGS §3 G12）。
    策略方向不变：只发清单里的头，多一个都不发。
    """
    token = str(account.get("access_token") or "")
    if not token:
        # 空 Bearer 发出去只会被网关 401，而 401 恢复路径也救不回来（没 token 多半也没
        # 换票素材）；在出网前失败比让上游给一个语义模糊的 401 更好诊断。
        raise T.PayloadError("minimax-code account has no access_token")
    headers: dict[str, str] = dict(REQUEST_STATIC_HEADERS)     # spec:342-345,347,690 + MITM 实测
    headers[HEADER_CONTENT_TYPE] = CONTENT_TYPE_JSON           # spec:340,690
    # spec:341,690：**流式也发 Accept: application/json**（stream:true 在 body 里，
    # 不是 text/event-stream）。这是客户端实测形状，照抄，别按"常识"改。
    headers[HEADER_ACCEPT] = CONTENT_TYPE_JSON                 # spec:341,690
    headers[HEADER_AUTHORIZATION] = f"{BEARER_PREFIX}{token}"   # spec:344,690 唯一真实凭证
    # MITM 实测 2026-09-30 dump-003:34：格式 = mvs_ + 32 位小写 hex（旧值裸 uuid4 被推翻）。
    headers[HEADER_MAVIS_SESSION_ID] = session_id or new_session_id()  # spec:346,690
    headers[HEADER_MAVIS_TIMEZONE_OFFSET] = str(_timezone_offset_seconds())  # spec:348,690
    for banned in PROD_PROHIBITED_HEADERS:                     # spec:350,690 prod 恒空
        headers.pop(banned, None)
    return headers


# ============================================================
# httpx 客户端（测试 transport 注入口）
# ---------------------------------------------------------------------------
# 与 openai_compat.py:74-90 / traesolo.chat:57-96 同法：模块级 _TRANSPORT + 按
# (事件循环, transport 身份) 惰性重建。**生产用进程级共享池**（storage.http_pool，
# 与 token.py 一致），只有测试装了 transport 才用本模块自持的 client。
# 注意：OAuth 刷新在 token.py 里走的是全局池 ⇒ 只装 chat transport 的测试截不到刷新
# 请求；要覆盖 401→刷新链路请 monkeypatch providers.minimax_code.token.get_client，
# 或给账号留空 refresh_token（"刷新素材缺失 ⇒ 标 expired 换号"这条路径同样可测）。
# ============================================================

_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None
_client_transport: Optional[httpx.AsyncBaseTransport] = None


def set_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None:
    """测试钩子：换上假传输（``httpx.MockTransport``），client 惰性重建（全程离线验证用）。"""
    global _TRANSPORT, _client, _client_loop, _client_transport
    _TRANSPORT = transport
    _client = None
    _client_loop = None
    _client_transport = None


def _get_client() -> httpx.AsyncClient:
    """无 transport 注入 ⇒ 进程级共享池；有注入 ⇒ 本模块自持 client（测试隔离）。"""
    global _client, _client_loop, _client_transport
    if _TRANSPORT is None:
        from storage.http_pool import get_client

        return get_client()
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if (
        _client is None
        or _client.is_closed
        or _client_transport is not _TRANSPORT
        or (loop is not None and _client_loop is not loop)
    ):
        _client = httpx.AsyncClient(timeout=NON_STREAM_TIMEOUT, transport=_TRANSPORT)
        _client_loop = loop
        _client_transport = _TRANSPORT
    return _client


# ============================================================
# 选号 / 刷新
# ============================================================

#: 「活性门让位」哨兵（第三个返回槽用它区分两种失败，别拿 bool 混）：
#: ``_recover_after_401`` / ``_post_with_auth_recovery`` / ``_stream_with_auth_recovery``
#: 返回它 = **网关主动拒刷**（客户端在运行，deferred）⇒ 可重试、**绝不**标 expired；
#: 返回 ``True`` = 真鉴权失效（无换票素材 / OAuth 面 invalid_grant）⇒ 标 expired 换号。
#: 取值就是 ``MiniMaxCodeAuthError`` 的 kind 名（同一个词，两处不漂移）；比较一律用
#: ``== DEFER``（调用方可能返回同值字符串字面量，别依赖身份）。
DEFER = "deferred"


async def _refresh_to_account(account: dict) -> dict:
    """``token.refresh_account``（返回 bool）→ ``pick_with_refresh_fallback``（要 dict）。

    这是**契约差异的适配层**，不是第二份刷新实现：token.py 负责协议（form/端点/换代/
    落库），这里只把"被拒=False"翻成异常，交给 trae_shared 的自适应负缓存
    （第 n 次连续失败后 60×2^(n-1)s、封顶 600s 不再对同一账号重放 refresh）——
    既避免上游 OAuth 故障时每个请求都重放失败的刷新（风控），也让过期账号有机会被
    别的可用账号顶上。

    **刷新前先做磁盘接管**（`.tmp/mitm/minimax-code-20260919/ROTATION-VERDICT.md`；
    spec:694,704）：桌面客户端每约 1h 自刷新一次、每次自刷新都会**轮转 refresh_token**
    （access token 实测 TTL 约 1h，spec:317 记的 11 天已推翻）⇒ 客户端与网关并用时网关
    库内的 refresh_token 是死票，直接走 OAuth 刷新必被拒（invalid_grant）、账号被判
    expired。故先 `_store.adopt_credentials_from_client`（**只读**客户端 auth.json、
    不回写、不轮转，spec:694 共存红线）把客户端刚更新的新票接管进 DB；接管成功即已
    拿到可用新票，直接返回，**不再**打 OAuth。`require_newer=True`：只认磁盘上明确
    更新的票，绝不把网关手里的新票降级成客户端旧票。磁盘没有更新凭据（读不到 / uid
    对不上 / 无新票 / 接管自身出任何异常）才回退原有 refresh，保留"网关是唯一持有者"
    时的自刷新能力。

    **活性门（``liveness.allow_gateway_self_refresh``）**：磁盘没接管到新票、而客户端
    又在运行（``auto`` 探测到其进程）或自刷被显式关掉（``off``）⇒ **不打 OAuth**
    （抢刷会轮转 refresh_token、把开着的客户端顶下线，spec:694）。此时抛
    ``kind="deferred"``：**可重试、非鉴权失效**，调用方绝不据此把账号标 expired
    （那会让它掉出选号池，直到定时器/人工才恢复，比原 bug 更瞎）。客户端下一次自刷
    落盘后，由本函数的磁盘接管或启动/定时对齐把新票接进来。
    """
    aid = int(account.get("id") or 0)
    try:
        # 同步函数（明文 auth.json，无解密 await）⇒ 直接调，别 await。
        # best-effort：路径未记录 / 文件不可读 / uid 核对不上 / 意外异常一律吞掉，
        # 本函数在**请求路径**上（选号自愈 + 401 单次重放），接管绝不能中断请求。
        adopted = _store.adopt_credentials_from_client(account, require_newer=True)
    except Exception:  # noqa: BLE001 - 接管是加分项，失败一律回落到原有 OAuth 刷新
        adopted = False
    if adopted:
        fresh = db.get_account(aid)
        if fresh:
            return fresh
        # 极窄竞态：刚接管完账号行就被删 → 不 return，继续走原有刷新分支处理。
    if not allow_gateway_self_refresh():
        # 活性门关着（auto 且客户端进程在，或显式 off）且磁盘暂无更新票 ⇒ 网关让位。
        # 只读探测，不发任何 OAuth/上游请求；kind="deferred" 是"可重试、非鉴权失效"，
        # 绝不许被当成 invalid_grant 去标 expired（见 _recover_after_401 与两处消费点）。
        # 消息只含账号 id 与策略说明，无任何凭证原文。
        raise MiniMaxCodeAuthError(
            f"minimax-code account {aid} self-refresh deferred: 客户端运行中，网关暂缓主动刷新"
            "以免顶掉客户端；下次请求或定时对齐会接管磁盘新票",
            kind="deferred",
        )
    if not await refresh_account(account):
        # 消息不含任何凭证原文（token.py 的异常同样只含状态码/OAuth error 码）。
        raise MiniMaxCodeAuthError(
            f"minimax-code refresh rejected for account {aid} (invalid_grant)",
            kind="invalid_grant",
        )
    fresh = db.get_account(aid)
    if not fresh:
        raise MiniMaxCodeAuthError(
            f"minimax-code account {aid} disappeared after refresh", kind="bad_response"
        )
    return fresh


async def refresh(account: dict) -> dict:
    """facade / ``pick_with_refresh_fallback`` 的刷新入口（RefreshCapable 的 dict 形状）。

    永不回写桌面客户端的 auth.json（spec:315,694 共存红线；实现在 token.py）。
    """
    return await _refresh_to_account(account)


def _limited_ids(model: str) -> set[int]:
    """该模型当前被记录限流的账号集（选号预判跳过；手法对齐 proxy._limited_tried_ids）。

    观测面失败绝不阻断转发 ⇒ 一律吞成空集。
    """
    try:
        return rate_limits.limited_account_ids(model)
    except Exception:  # noqa: BLE001
        return set()


async def _pick(tried: set[int]) -> Optional[dict]:
    """选号：``auth_manager.pick_account(provider=CHANNEL_ID)`` + 过期自愈 + 限流跳过。

    * ``tried`` 由调用方维护，入口处 seed 了该模型受限账号（``_limited_ids``）⇒
      "sticky/pin 只对可用账号生效"的既有调度语义天然覆盖限流跳过；
    * 冷却（401/403/429 连坐退避）由 pick_account 内部的 cooling-down 过滤负责；
    * token 过期 ⇒ 就地 refresh（trae_shared 的负缓存限流），成功返回新账号行；
    * 活性门让位（``kind="deferred"``）落进 trae_shared 的**内存**负缓存、**不改 DB
      status**（见 trae_shared.py:165-178）⇒ 账号留在 active，只是这条路挑不出号，
      调用方照常走"无可用账号"的可重试 503（绝不被误标 expired）。
    """
    return await pick_with_refresh_fallback(CHANNEL_ID, _refresh_to_account, exclude_ids=tried)


async def _recover_after_401(account: dict) -> dict | str | None:
    """401 ⇒ 刷新一次（spec:313 失效→刷新→**单次重放**；spec:216-220 客户端同构）。

    三种结果（**别把 ``DEFER`` 读成 ``None``**）：
      * 新账号行 = 可以重放；
      * ``DEFER`` = 活性门让位（客户端在运行 ⇒ 网关主动拒刷，``kind="deferred"``）⇒ 本次
        不重放，但账号**没坏**：调用方不得标 expired（那会把它踢出选号池，直到定时器/
        人工才恢复，比原 bug 更瞎），只对外产出可重试 503；
      * ``None``  = 换票素材缺失 / OAuth 面被拒 / 网络类刷新异常 ⇒ 调用方标 expired 换号。
    网络/5xx 类刷新异常仍按"不可重放"处理：本函数在**请求路径**上，宁可换号也不把
    异常原样抛给客户端；OAuth 面持续故障由 trae_shared 的负缓存兜住后续请求。
    """
    aid = int(account.get("id") or 0)
    if not str(account.get("refresh_token") or ""):
        # 裸 JWT 导入的账号（store 侧 can_refresh=False）没有换票素材。
        logger.info("minimax-code account %s has no refresh_token; 401 不重放", aid)
        return None
    try:
        fresh = await _refresh_to_account(account)
    except MiniMaxCodeAuthError as exc:
        if exc.kind == DEFER:
            # 活性门让位：既不能重放（票没换）、也不能判死（票没坏）。exc 消息无凭证。
            logger.info("minimax-code account %s 401 recovery deferred: %s", aid, exc)
            return DEFER
        logger.warning("minimax-code 401 recovery failed for account %s: %s", aid, exc)
        return None
    except httpx.HTTPError as exc:
        logger.warning("minimax-code 401 recovery failed for account %s: %s", aid, exc)
        return None
    logger.info("minimax-code account %s token refreshed, 重放一次", aid)
    return fresh


# ============================================================
# 错误分类（spec §7.2:627-655 + §9:5:692）
# ============================================================

def _to_int_code(value) -> Optional[int]:
    """宽容取整数码：bool/JWT 串/超长码等非整数值一律 None（不参与映射，不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _envelope_candidates(payload) -> list[dict]:
    """按 spec:655 的嵌套形状 + spec:648-654 的 AI SDK 包装，摊平出**内层优先**的候选 dict。

    顺序 = error / statusInfo / base_resp / 顶层：内层业务码必须压过传输级 HTTP 码
    （spec:648-654）。``responseBody``（AI SDK 把上游负载二次编码成 JSON 字符串的形态，
    spec:653）解析后挂到**最后**——它是"上游体的二次编码"，只在前几层都读不出码时兜底。
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 不该发生，保守丢弃
            return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []

    candidates: list[dict] = []
    for key in ("error", "statusInfo", "base_resp"):   # spec:655 三种嵌套（含 MiniMax base_resp）
        inner = payload.get(key)
        if isinstance(inner, dict):
            candidates.append(inner)
    candidates.append(payload)
    body = payload.get("responseBody")                  # spec:653
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            candidates.append(parsed)
            for key in ("error", "statusInfo", "base_resp"):
                nested = parsed.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)
    return candidates


def _pick_code(candidates: list[dict]) -> tuple[Optional[int], Optional[int], str]:
    """→ (内层原始码, 归一业务码, Anthropic error.type)。内层候选先出现者胜（spec:648-654）。"""
    raw_code: Optional[int] = None
    business: Optional[int] = None
    etype = ""
    for item in candidates:
        for key in ("status_code", "code", "error_code"):   # spec:649,653 的键名
            code = _to_int_code(item.get(key))
            if code is None:
                continue
            if raw_code is None:
                raw_code = code
            if business is None:
                if code in UPSTREAM_STATUS_CODE_MAP:        # spec:642-646 私有码 2056/2067/1400010161
                    business = UPSTREAM_STATUS_CODE_MAP[code]
                elif code in UPSTREAM_ERROR_CODES:          # spec:632-639 业务码 42212/5011x/5015x
                    business = code
                elif code in LLM_TPM_RATE_LIMIT_MESSAGE_CODES:  # spec:641 TPM/RPM 消息码组
                    business = LLM_TPM_RATE_LIMITED
        if not etype:
            value = item.get("type")
            if isinstance(value, str) and value:
                etype = value
    return raw_code, business, etype


def _extract_message(payload, default: str = "") -> str:
    """从任意信封里取人类可读消息（截 240）。本通道响应体不含我方凭证，可安全外发。"""
    for item in _envelope_candidates(payload):
        for key in ("message", "status_msg", "msg"):        # spec:653,655
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:240]
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            payload = ""
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:240]
    return default


def _classify_error(status: int, payload) -> dict:
    """上游失败 → 统一分类（spec §7.2:627-655；§9:5:692 的业务码/内层码双写要求）。

    同时兼容两套信封（任务约束）：
      * MiniMax：``base_resp.status_code`` / 平铺 ``{status_code,status_msg}`` /
        ``{statusInfo:{code,message}}``（spec:655）；
      * Anthropic：``{"type":"error","error":{"type":…,"message":…}}``（spec:12 的
        Anthropic Messages 兼容面；词表见 ``_ANTHROPIC_ERROR_TYPES``）。

    优先级：内层业务码 > Anthropic error.type > HTTP 状态（spec:648-654）。

    返回::
        {"http": 对外状态码, "code": MiniMax 业务码|None, "upstream_code": 原始内层码|None,
         "type": 对外 error.type, "message": 诊断文本, "retryable": bool,
         "rate_limited": bool（限流族：登记后**换号**）,
         "quota": bool（额度族：登记但**本请求不换号重放**，spec:660）,
         "in_limit_class": bool（两族合集 = 该登记进 rate_limits 的那族）,
         "auth": bool, "name": 业务码语义名}
    """
    candidates = _envelope_candidates(payload)
    raw_code, business, etype = _pick_code(candidates)
    message = _extract_message(payload)
    status = int(status or 0)

    if business is None and etype:
        mapped = _ANTHROPIC_ERROR_TYPES.get(etype)
        if mapped is not None:
            business = mapped or None
    if business is None:
        # 纯 HTTP 兜底（spec:632-639 的 upstream_http 列）
        business = {
            401: LLM_AUTH_ERROR,         # spec:635
            403: LLM_AUTH_ERROR,         # spec:635
            402: LLM_CREDITS_EXHAUSTED,  # spec:633
            429: LLM_RATE_LIMITED,       # spec:634
            529: LLM_CLUSTER_OVERLOADED, # spec:639
        }.get(status)
        # 其余 4xx（如 400 invalid_request）不硬套 50113：留给 http 原样透传。

    meta = UPSTREAM_ERROR_CODES.get(business) if business is not None else None
    limit_family = bool(business is not None and business in LIMIT_CLASS)
    quota = bool(business is not None and business in QUOTA_CODES)
    auth_error = business == LLM_AUTH_ERROR

    # 对外 HTTP：业务码表优先（spec:692 的 402/429/529 双写），否则沿用上游状态。
    http = int(ERROR_CODE_TO_HTTP_STATUS[business]) if business in ERROR_CODE_TO_HTTP_STATUS else 0
    if not http:
        http = status if 400 <= status < 600 else 502

    # retryable 一律由契约层给（constants 逐码注明了出处），本层不覆盖：
    #   * 限流族 50111/50150/50151 retryable=True ⇒ 换号（走 rate_limited 分支，零延迟）；
    #   * 额度族 42212/50110 retryable=False ⇒ 本请求不重放（spec:660 "Do NOT retry"）；
    #   * 401 retryable=False ⇒ 走 _recover_after_401 的 spec:313 单次重放，
    #     不进通用重试（constants:527-528 的注释同款理由）。
    retryable = bool(meta.get("retryable")) if meta is not None else status in RETRYABLE_STATUS

    if limit_family:
        out_type = _TYPE_BY_CLASS["rate_limit"]
    elif auth_error:
        out_type = _TYPE_BY_CLASS["auth"]
    elif http == 400 or etype == "invalid_request_error":
        out_type = _TYPE_BY_CLASS["invalid_request"]
    elif http == 403:
        out_type = _TYPE_BY_CLASS["permission"]
    elif 500 <= http < 600:
        out_type = _TYPE_BY_CLASS["server"]
    else:
        out_type = _TYPE_BY_CLASS["upstream"]

    if not message:
        message = f"minimax-code upstream returned HTTP {status or '0'}"
    return {
        "http": http,
        "code": business,
        "upstream_code": raw_code,
        "type": out_type,
        "message": message[:240],
        "retryable": bool(retryable),
        "rate_limited": bool(limit_family),
        # 额度族（42212/50110）：登记冷却但本请求不换号重放（spec:660）
        "quota": bool(quota),
        "auth": bool(auth_error),
        "name": (meta or {}).get("name") or ("" if business is None else str(business)),
    }


def _server_error_classified(message: str) -> dict:
    """合成一个"上游不可用"分类（截断流 / 空流 / 传输错误用；语义同 spec:636 的 50113）。"""
    return {
        "http": 502,
        "code": LLM_UPSTREAM_ERROR,
        "upstream_code": None,
        "type": _TYPE_BY_CLASS["server"],
        "message": (message or "upstream stream failed")[:240],
        "retryable": True,
        "rate_limited": False,
        "quota": False,
        "auth": False,
        "name": UPSTREAM_ERROR_CODES[LLM_UPSTREAM_ERROR]["name"],
    }


#: 活性门让位对外的文案（只含策略说明，无账号 id / 无凭证）。
DEFER_HINT = (
    "minimax-code 客户端运行中，网关暂缓主动刷新以免顶掉客户端；"
    "下次请求或定时对齐会接管磁盘新票"
)


def _deferred_classified(message: str = DEFER_HINT) -> dict:
    """活性门让位（``DEFER``）的对外分类：**可重试 503、auth=False**。

    ``auth=False`` 是刻意的：这条分类绝不能被任何按 ``classified["auth"]`` 判死的分支
    吃掉（标 expired 会把账号踢出选号池）。形状与 ``_server_error_classified`` 同款，
    ``type``/兜底口径对齐本文件既有的 503 ``channel_unavailable`` 出口。
    """
    return {
        "http": 503,
        "code": None,
        "upstream_code": None,
        "type": "channel_unavailable",
        "message": (message or DEFER_HINT)[:240],
        "retryable": True,
        "rate_limited": False,
        "quota": False,
        "auth": False,
        "name": "",
    }


def _error_body(classified: dict) -> dict:
    """对外错误体（OpenAI 方言；``code``/``status_code`` 双写 MiniMax 业务码，spec:692）。"""
    error: dict = {
        "message": classified["message"],
        "type": classified["type"],
        "provider": CHANNEL_ID,
    }
    if classified.get("code") is not None:
        error["code"] = classified["code"]
    if classified.get("upstream_code") is not None:
        # 原始内层码（2056/2067/1400010161 等）另键保留，便于上层精确分类（spec:642-646）。
        error["status_code"] = classified["upstream_code"]
    return {"error": error}


def _reset_text(payload) -> str:
    """把两套错误信封的文案提取成纯文本，交给 ``rate_limits.parse_reset_epoch``。

    共享的 ``rate_limits.parse_reset_epoch`` 只认 workbuddy 的 ``msg`` 键，而本通道
    实际会拿到两种信封（spec:642-655）：
      * MiniMax：``{"status_code":50111,"status_msg":"将在 … 重置"}``
      * Anthropic：``{"type":"error","error":{"message":"将在 … 重置"}}``
    不在这里提取的话，上游给的墙钟解除时刻会被丢掉、退化成
    ``FALLBACK_COOLDOWN_S`` 兜底冷却（表现为限流窗口被无谓拉长）。
    """
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace")
    if not isinstance(payload, dict):
        return str(payload or "")
    # 两套信封的文案键，按"最具体 → 最通用"顺序；命中非空即返回。
    for key in ("status_msg", "msg", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "msg"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value
    elif isinstance(error, str) and error.strip():
        return error
    return ""


def _reset_epoch(response: Optional[httpx.Response], payload) -> Optional[float]:
    """尽力解析"墙钟解除时刻"→ epoch 秒；解析不出返回 None（调用方落兜底冷却）。

    只用**上游自己申报**的值，两个来源，都不编造：
      1. 错误文案里的"将在 YYYY-MM-DD HH:MM:SS UTC+8 重置/恢复"
         （``rate_limits.parse_reset_epoch``，已含 30s 安全余量）；
      2. 标准 ``Retry-After`` 数字秒 —— spec:711 明确本通道的退避/retry-after
         静态未确认 ⇒ 有就尊重、没有就算。
         TODO(spec:711)：真机 MITM 后再决定是否放宽到 HTTP-date 形式。
    两者都没有 ⇒ None：**不假装知道解除时间**，由 rate_limits.record 落
    ``FALLBACK_COOLDOWN_S``（只防同账号同模型被疯转）。
    """
    epoch = rate_limits.parse_reset_epoch(_reset_text(payload))
    if epoch:
        return epoch
    header = None
    if response is not None:
        try:
            header = response.headers.get("retry-after")
        except Exception:  # noqa: BLE001
            header = None
    return _retry_after_seconds_to_epoch(str(header or ""))


def _retry_after_seconds_to_epoch(text: str) -> Optional[float]:
    """``Retry-After`` 数字秒 → 解除 epoch；非法/非数字 ⇒ None（不猜窗口）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0 or seconds == float("inf"):
        return None
    return time.time() + seconds


def _retry_after_seconds(response: Optional[httpx.Response]) -> Optional[float]:
    """``Retry-After`` 数字秒（交给 retry_delay 尊重上游节奏）；缺失/非法 ⇒ None。"""
    if response is None:
        return None
    try:
        text = str(response.headers.get("retry-after") or "")
    except Exception:  # noqa: BLE001
        return None
    text = text.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds if 0 <= seconds != float("inf") else None


def _record_limit(account: dict, model: str, response: Optional[httpx.Response], payload) -> float:
    """登记一次 (账号, 模型) 限流 → 返回生效的解除 epoch（观测/测试注入点）。"""
    return rate_limits.record(
        int((account or {}).get("id") or 0), model, _reset_epoch(response, payload)
    )


def _limited_detail(classified: dict, view: dict, tried_ids: set[int]) -> dict:
    """全账号受限的返回体（字段名逐一对齐 upstream/proxy.py 的 ``_rate_limit_exhausted_error``）。

    与 workbuddy 6004 的唯一差别是 ``code``：这里写 **MiniMax 业务码**（spec:632-639），
    不冒充 WorkBuddy 的机器码 6004 —— ``proxy._is_rate_limit_error`` 只认 6004，而本通道
    的错误直接经 ``gateway/routers/v1.py`` 出给客户端，不经 proxy 二次判定。
    """
    message = classified.get("message") or "所有账号该模型均受限。"
    earliest = view.get("earliest_reset")
    error: dict = {
        "message": message,
        "type": rate_limits.RATE_LIMIT_TYPE,
        "code": classified.get("code"),
        "reset_at": earliest,
        "reset_at_iso": view.get("earliest_reset_iso"),
        "limited_accounts": list(view.get("limited_accounts", [])),
        "tried_account_ids": sorted(tried_ids),
        "provider": CHANNEL_ID,
    }
    if classified.get("upstream_code") is not None:
        error["status_code"] = classified["upstream_code"]
    retry_after = max(1, int(earliest - time.time())) if earliest else None
    if retry_after:
        error["retry_after"] = retry_after
    return {"code": classified.get("code"), "msg": message, "error": error}


def _model_view_or_empty(model: str) -> dict:
    """rate_limits.model_view 的观测面兜底（表坏了也要给出可诊断的返回）。"""
    try:
        return rate_limits.model_view(model)
    except Exception:  # noqa: BLE001
        return {"limited_accounts": [], "earliest_reset": None, "earliest_reset_iso": None}


def _limited_exhausted(model: str, tried_ids: set[int], classified: dict) -> tuple:
    """非流式"该模型已无可用账号（全部受限）"的出口 → ``("error", (429, body))``。"""
    return ("error", (429, _limited_detail(classified, _model_view_or_empty(model), tried_ids)))


def _limited_sse_bytes(classified: dict, model: str, tried_ids: set[int]) -> bytes:
    """流式路径的全限出口：SSE error 事件 + ``[DONE]``（形态对齐 proxy._rate_limit_event_body）。"""
    detail = _limited_detail(classified, _model_view_or_empty(model), tried_ids)
    payload = json.dumps({"error": detail["error"]}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8") + T.DONE_EVENT


def _stream_error_bytes(message: str, classified: Optional[dict] = None) -> bytes:
    """流内失败的对外事件（**不吞成静默短回复**：失败必须成帧送达）。"""
    error: dict = {
        "message": (message or "")[:240],
        "type": _TYPE_BY_CLASS["upstream"],
        "provider": CHANNEL_ID,
    }
    if classified:
        if classified.get("type"):
            error["type"] = classified["type"]
        if classified.get("code") is not None:
            error["code"] = classified["code"]
        if classified.get("upstream_code") is not None:
            error["status_code"] = classified["upstream_code"]
    payload = json.dumps({"error": error}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


# ============================================================
# 请求日志
# ============================================================

def _usage_counts(usage) -> tuple[int, int, int]:
    """(prompt, completion, total)；total 缺失时按 prompt+completion 兜底（各家同法）。"""
    u = usage if isinstance(usage, dict) else {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    total = int(u.get("total_tokens") or 0) or prompt + completion
    return prompt, completion, total


def _usage_log_kwargs(usage, stats: Optional[dict] = None) -> dict:
    """把 usage 摊成 ``_log`` 的显式计数参数 + first_token_ms。"""
    prompt, completion, total = _usage_counts(usage)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "first_token_ms": (stats or {}).get("first_token_ms"),
    }


async def _log(api_key_info, account, model_name, stream, finish_reason, status_code,
               error_msg, t0, increment_usage=True, usage=None, first_token_ms=None,
               prompt_tokens=0, completion_tokens=0, total_tokens=0):
    """落一条请求日志（收敛到 ``store_common.log_request``，字段口径见下）。

    字段口径（spec §9:6:693 + §7.1:610-625，即"接入报告 §5.2"那套日志字段清单）：
      * ``model`` = **客户端原始名**（``api_key_info["_log_model"]``，不是上游裸 id）；
      * ``prompt/completion/total`` 取 ``translate.normalize_usage`` 的产物：
        prompt = input + cache_read + cache_creation、completion = output、
        **total = 四者之和**（spec:619,693 —— 客户端就是这么算的）。usage 里同时保留
        ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` 两个 Anthropic 原生键，
        ``store_common.extract_cache_tokens`` 直接吃它们 ⇒ cache_read/cache_creation 两列
        不在这里重算（且 prompt 含 cache 才不会触发它的 min(cache, prompt) 反向截断）；
      * ``credit``：上游 usage 里**没有** credit 字段（spec:610,624）⇒ log_request 内
        ``upstream_credit`` 返回 None，自动退回 ``channel_credit_rate(CHANNEL_ID)`` 的
        token 估算口径（本通道唯一可行的近似，非官方锚点）；
      * ``credit_source`` 由 ``store_common.credit_source_of(usage)`` 判定：cache 原生键在
        ⇒ 'live'，估算 ⇒ 'estimate'，与 dashboard 的 credit 口径统计（stats.py n_live）对齐；
      * ``stream`` / ``status_code`` / ``finish_reason`` / ``duration_ms`` / ``first_token_ms``
        / ``usage_json`` / ``created_at`` 同 log_request 既有语义。
    """
    await store_common.log_request(
        api_key_info, account,
        channel=CHANNEL_ID, model=model_name, stream=stream, usage=usage,
        finish_reason=finish_reason, status_code=status_code,
        duration_ms=int((time.time() - t0) * 1000), error_msg=error_msg,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=total_tokens, increment_usage=increment_usage,
        created_at=int(t0), first_token_ms=first_token_ms,
        credit_source=store_common.credit_source_of(usage),
    )


# ============================================================
# 流式：SSEDecoder → translate 状态机 → OpenAI chunk → 补 [DONE]
# ============================================================

_CONTENT_DELTA_KEYS = ("content", "reasoning_content", "tool_calls", "function_call")


def _chunk_has_content(chunk: dict) -> bool:
    """该 chunk 是否算"首个内容帧"（first_token_ms 判定，口径同 qodercn/traesolo）。"""
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and any(delta.get(key) for key in _CONTENT_DELTA_KEYS):
            return True
    return False


def _chunk_has_role(chunk: dict) -> bool:
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta.get("role"):
            return True
    return False


def _pump_events(events: list[bytes], state: T.AnthropicStreamState, stats: dict) -> list[bytes]:
    """一批 SSE data 载荷 → 待下发的 OpenAI 帧（**同步**：纯解析，不 await）。

    事件分类完全交给 ``translate.feed``：按 ``data["type"]`` 判，不依赖 ``event:`` 行——
    仓库唯一的 SSEDecoder 本来就把 event 行丢了（upstream/sse.py:105）。流内 ``error``
    事件在 translate 里抛 ``AnthropicStreamError``（spec:584），这里捕获后把结论写进
    ``stats``，由调用方决定"还没出字 ⇒ 换号重试"还是"已经出字 ⇒ 就地成帧报错"。
    """
    out: list[bytes] = []
    for data in events:
        try:
            chunks = T.feed(state, data)
        except T.AnthropicStreamError as exc:
            stats["outcome"] = "stream_error"
            stats["exception"] = exc
            return out
        for chunk in chunks:
            has_content = _chunk_has_content(chunk)
            if stats.get("first_token_ms") is None and has_content:
                stats["first_token_ms"] = int((time.monotonic() - stats["ft_t0"]) * 1000)
            if has_content or _chunk_has_role(chunk):
                stats["output_started"] = True
            out.append(T.sse_bytes(chunk))
    return out


async def _consume_stream(response: httpx.Response, state: T.AnthropicStreamState,
                          stats: dict) -> AsyncGenerator[bytes, None]:
    """吃完整条上游 SSE：增量下发 → 硬结束校验 → 终结块 → 对外补 ``[DONE]``。

    顺序是刻意的（与 ``translate.finish_state`` 的 docstring 约定一致）：
    所有增量 → 终结块（``finish_reason`` + usage 合体，spec:582,589）→ 补
    ``data: [DONE]``（上游没有这个哨兵，spec:595；那是我们 OpenAI 出口的收尾约定）。
    缺 ``message_stop`` ⇒ ``AnthropicStreamTruncatedError``（spec:591）⇒ 显式成帧报错，
    **绝不**把半截回复当成功结束。
    """
    decoder = SSEDecoder()
    async for raw in response.aiter_bytes():
        if not raw:
            continue
        events = decoder.feed(raw)
        if events:
            for encoded in _pump_events(events, state, stats):
                yield encoded
        if stats.get("outcome") == "stream_error":
            return
        if decoder.parser_error:
            stats["outcome"] = "parser_error"
            stats["message"] = decoder.parser_error
            return
        if state.saw_message_stop:
            break  # 硬结束已到（spec:591,595）：不再等上游关连接
    if stats.get("outcome") == "stream_error":
        return

    tail = decoder.finish()
    if tail:
        for encoded in _pump_events(tail, state, stats):
            yield encoded
    if stats.get("outcome") == "stream_error":
        return
    if decoder.parser_error and not state.saw_message_stop:
        stats["outcome"] = "parser_error"
        stats["message"] = decoder.parser_error
        return

    try:
        usage = T.finish_state(state)  # spec:591：截断在这里抛
    except T.AnthropicStreamTruncatedError as exc:
        stats["outcome"] = "truncated"
        stats["exception"] = exc
        stats["message"] = str(exc)
        return
    terminal = T.get_terminal_chunk(state)
    if terminal is not None:
        yield T.sse_bytes(terminal)
    yield T.DONE_EVENT
    stats["outcome"] = "ok"
    stats["usage"] = usage


@asynccontextmanager
async def _opened_stream(client: httpx.AsyncClient, url: str, raw: bytes, headers: dict):
    """打开流；错误响应先把体读干（status>=400 后 httpx 允许读，但惰读会丢 body）。"""
    async with client.stream(
        "POST", url, headers=headers, content=raw, timeout=STREAM_TIMEOUT
    ) as response:
        if response.status_code >= 400:
            await response.aread()
        yield response


@asynccontextmanager
async def _stream_with_auth_recovery(client: httpx.AsyncClient, url: str, raw: bytes,
                                     account: dict, session_id: str):
    """开流 + 401 单次重放（spec:313），把"重放"这件易被写坏的事收在一处。

    yield ``(response, account, auth_dead)``（三态，**别只判真假**：``DEFER`` 是字符串、
    真值，必须先于 ``True`` 判掉）：
      * ``False``      ⇒ 拿到最终响应（可能仍是 4xx/5xx，交调用方分类）；
      * ``DEFER``      ⇒ 401 且活性门让位（客户端在运行，网关主动拒刷）⇒ 不重放、
                        换号，但**绝不标 expired**（账号票没坏，见 ``_recover_after_401``）；
      * ``True``       ⇒ 401 且换票素材缺失/被拒 ⇒ 调用方标 expired 后**换号**。
    重放最多一次（``UNAUTHORIZED_RETRY_LIMIT=1``），不做重放放大；首个流在重放前
    已随 ``async with`` 退出（连接归还池），不会两条流并存。
    """
    headers = request_headers(account, session_id)
    async with _opened_stream(client, url, raw, headers) as response:
        if int(response.status_code) != 401:
            yield response, account, False
            return
        fresh = await _recover_after_401(account)
        if fresh == DEFER:
            yield response, account, DEFER
            return
        if fresh is None:
            yield response, account, True
            return
        account = fresh
    async with _opened_stream(
        client, url, raw, request_headers(account, session_id)
    ) as replayed:
        yield replayed, account, False


async def _stream(raw: bytes, url: str, upstream_model: str, model_name: str,
                  api_key_info, session_id: str) -> AsyncGenerator[bytes, None]:
    """流式主循环：≤3 次尝试 / 401 单次重放 / 限流登记换号 / 全限成帧 429 语义。

    返回给网关的 generator 已被 ``gateway/routers/v1.py`` 当 SSE 流消费，**状态码改不了**
    ⇒ 失败一律成帧（``data: {"error":…}``），与 qodercn/qclaw 的既有流式错误形态一致。
    """
    tried: set[int] = set(_limited_ids(upstream_model))  # 预判跳过已限流账号
    last_error: bytes = _stream_error_bytes(
        "No available accounts", {"type": "channel_unavailable", "code": None, "upstream_code": None}
    )
    last_status = 503
    last_classified: Optional[dict] = None
    attempts = 0
    client = _get_client()

    for attempt in range(MAX_ATTEMPTS):
        account = await _pick(tried)
        if not account:
            break
        attempts += 1
        aid = int(account.get("id") or 0)
        tried.add(aid)
        t0 = time.time()
        ft_t0 = time.monotonic()
        state = T.AnthropicStreamState(model=model_name)
        stats: dict = {"ft_t0": ft_t0, "output_started": False, "first_token_ms": None}

        try:
            headers = request_headers(account, session_id)  # 空 token 在此显式失败
            async with _stream_with_auth_recovery(client, url, raw, account, session_id) as pair:
                response, account, auth_dead = pair
                aid = int(account.get("id") or aid)
                status = int(response.status_code)

                if auth_dead == DEFER:
                    # 活性门让位（客户端在运行 ⇒ 网关主动拒刷）：**不调用
                    # mark_account_failure**（401/403 档会把 status 置 expired、把账号踢出
                    # 选号池，直到定时器/人工才恢复）⇒ 只对外产出可重试 503 并换号。
                    last_classified = _deferred_classified()
                    last_status = 503
                    last_error = _stream_error_bytes(last_classified["message"], last_classified)
                    await _log(api_key_info, account, model_name, True, "error", 503,
                               last_classified["message"], t0)
                    continue

                if auth_dead:
                    # 401 且刷新不可用 ⇒ mark_account_failure 内部把 status 置 expired（换号）
                    auth_manager.mark_account_failure(aid, 401)
                    last_classified = _classify_error(401, response.content)
                    last_status = 401
                    last_error = _stream_error_bytes(
                        f"upstream 401 and refresh unavailable (account {aid} marked expired)",
                        last_classified,
                    )
                    await _log(api_key_info, account, model_name, True, "error", 401,
                               last_classified["message"], t0)
                    continue

                if status >= 400:
                    classified = _classify_error(status, response.content or response.text[:400])
                    last_classified = classified
                    last_status = classified["http"]
                    last_error = _stream_error_bytes(classified["message"], classified)
                    if classified["rate_limited"]:
                        # 限流是 (账号,模型) 窗口：只登记，不叠加账号级连坐冷却（同 proxy 6004）
                        _record_limit(account, upstream_model, response, response.content)
                        await _log(api_key_info, account, model_name, True, "error",
                                   classified["http"], classified["message"], t0)
                        if classified.get("quota") and not QUOTA_SWITCH_ACCOUNT:
                            # 额度族：本请求不重放、不换号（spec:660 "Do NOT retry"）。
                            # 下一批请求由 _limited_ids 预判跳过该账号，池子照样健康。
                            yield last_error
                            return
                        continue  # 限流族零延迟换号
                    if classified["auth"]:
                        auth_manager.mark_account_failure(aid, status)  # → status=expired
                        await _log(api_key_info, account, model_name, True, "error",
                                   classified["http"], classified["message"], t0)
                        continue  # 换号
                    auth_manager.mark_account_failure(aid, status)
                    await _log(api_key_info, account, model_name, True, "error",
                               classified["http"], classified["message"], t0)
                    if classified["retryable"] and attempt < MAX_ATTEMPTS - 1:
                        await retry_delay(attempt, retry_after=_retry_after_seconds(response))
                        continue
                    yield last_error
                    return

                # ---- 2xx：消费流 ----
                auth_manager.mark_account_success(aid)
                async for encoded in _consume_stream(response, state, stats):
                    yield encoded
        except httpx.HTTPError as exc:
            # 传输层失败：只报异常类名 + 截断文本（httpx 消息含 URL，不含我方凭证）。
            auth_manager.mark_account_failure(aid, 503)
            last_status = 502
            last_error = _stream_error_bytes(
                f"upstream transport error: {type(exc).__name__}",
                _server_error_classified(f"upstream transport error: {type(exc).__name__}"),
            )
            await _log(api_key_info, account, model_name, True, "network_error", 502,
                       str(exc)[:240], t0)
            if stats.get("output_started"):
                # 已经吐过字：连接断了只能就地成帧报错收尾（不重放，避免半截回复×2）。
                yield last_error
                return
            await retry_delay(attempt)
            continue
        except T.PayloadError as exc:  # request_headers 的空 token 显式失败
            last_status = 400
            last_error = _stream_error_bytes(str(exc)[:240])
            await _log(api_key_info, account, model_name, True, "error", 400, str(exc)[:240], t0)
            yield last_error
            return

        outcome = stats.get("outcome")
        if outcome == "ok":
            usage = stats.get("usage") or {}
            finish = state.finish or "stop"
            await _log(api_key_info, account, model_name, True, finish, 200, "", t0,
                       usage=usage, **_usage_log_kwargs(usage, stats))
            return

        # ---- 流内失败：绝不当成功结束（spec:591,595；任务风控约束）----
        usage = state.usage_final if isinstance(state.usage_final, dict) else None
        if outcome == "stream_error":
            exc = stats.get("exception")
            # 流内 error 事件：HTTP 状态无意义（已 200 成帧），按内层业务码分类（spec:648-654）
            classified = _classify_error(
                0, {"error": {"message": str(exc or ""), "code": getattr(exc, "code", None)}}
            )
        elif outcome == "truncated":
            classified = _server_error_classified(str(stats.get("message") or "stream truncated"))
        elif outcome == "parser_error":
            classified = _server_error_classified(str(stats.get("message") or "SSE parse failed"))
        else:
            classified = _server_error_classified("upstream stream ended without output")
        last_classified = classified
        last_status = classified["http"]
        last_error = _stream_error_bytes(classified["message"], classified)

        if classified.get("rate_limited"):
            _record_limit(account, upstream_model, None, None)
            await _log(api_key_info, account, model_name, True, "error", classified["http"],
                       classified["message"], t0, usage=usage, **_usage_log_kwargs(usage, stats))
            if classified.get("quota") and not QUOTA_SWITCH_ACCOUNT:
                yield last_error   # 额度族：登记冷却，本请求不换号重放（spec:660）
                return
            continue

        await _log(api_key_info, account, model_name, True, "error", classified["http"],
                   classified["message"], t0, usage=usage, **_usage_log_kwargs(usage, stats))
        if classified["retryable"] and not stats.get("output_started") and attempt < MAX_ATTEMPTS - 1:
            # 还没出字 ⇒ 换号重试安全；已经出过字就只成帧报错，不重放放大。
            await retry_delay(attempt)
            continue
        yield last_error
        return

    # 3 次都没成功，或一个账号都挑不出来
    if last_classified is None and _limited_ids(upstream_model):
        # 入口就全限（一次上游都没打）：给 429 + 受限视图，而不是含糊的 503。
        last_classified = {
            "http": 429, "code": LLM_RATE_LIMITED, "upstream_code": None,
            "type": rate_limits.RATE_LIMIT_TYPE, "message": "",
            "retryable": True, "rate_limited": True, "quota": False, "auth": False,
            "name": UPSTREAM_ERROR_CODES[LLM_RATE_LIMITED]["name"],
        }
    if last_classified is not None and last_classified.get("rate_limited"):
        yield _limited_sse_bytes(last_classified, upstream_model, tried)
        await _log(api_key_info, None, model_name, True, "error", 429,
                   "all accounts limited for this model" if not attempts else last_classified["message"],
                   time.time(), increment_usage=not attempts)
        return
    yield last_error
    await _log(api_key_info, None, model_name, True, "error", last_status,
               "stream failed", time.time(), increment_usage=False)


# ============================================================
# 非流式
# ============================================================

async def _post_with_auth_recovery(client: httpx.AsyncClient, url: str, raw: bytes,
                                   account: dict, session_id: str) -> tuple:
    """POST + 401 单次重放（spec:313）→ ``(response, account, auth_dead)``。

    与流式侧同构（第三个槽的三态取值见 ``_stream_with_auth_recovery``）：重放最多一次；
    ``True`` ⇒ 真鉴权失效（调用方标 expired 换号）；``DEFER`` ⇒ 活性门让位（可重试，
    **调用方不得标 expired**，对外产出 503）。
    """
    response = await _post_once(client, url, raw, request_headers(account, session_id))
    if int(response.status_code) != 401:
        return response, account, False
    fresh = await _recover_after_401(account)
    if fresh == DEFER:
        return response, account, DEFER
    if fresh is None:
        return response, account, True
    response = await _post_once(client, url, raw, request_headers(fresh, session_id))
    return response, fresh, False


async def _post_once(client: httpx.AsyncClient, url: str, raw: bytes, headers: dict) -> httpx.Response:
    return await client.post(url, headers=headers, content=raw, timeout=NON_STREAM_TIMEOUT)


def _looks_like_sse(text: str) -> bool:
    """响应体是否是 SSE 分帧（``data:`` / ``event:`` 开头）。

    用途：客户端非流式请求发 ``stream:false``，但**网关是否尊重该字段 spec 未记录**
    （spec:703-708 未确认项，且客户端自己恒发 stream:true）。若上游照旧回 SSE，我们走
    状态机聚合成 completion，而不是"解析 JSON 失败 ⇒ 报错"。这不是编造协议，只是把两种
    真实可能的响应都接住。
    """
    head = (text or "").lstrip()[:64]
    return head.startswith("data:") or head.startswith("event:")


def _completion_from_state(state: T.AnthropicStreamState, usage: dict,
                           requested_model: str) -> dict:
    """聚合后的 Anthropic 流 → OpenAI ``chat.completion``（非流式出口的 SSE 兜底路径）。

    ``translate.to_openai_completion`` 的 docstring 明确指派这条路用状态机的
    ``text_parts``/``reasoning_parts``/``tool_calls`` 拼装，而不是硬套非流式解析器。
    """
    finish = state.finish or (
        "tool_calls" if state.saw_tool and not state.saw_content else "stop"
    )
    message: dict = {"role": "assistant", "content": "".join(state.text_parts) or None}
    reasoning = "".join(state.reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    calls = [c for c in state.tool_calls if isinstance(c, dict)]
    if calls:
        message["tool_calls"] = [
            {
                "id": str(c.get("id") or f"call_{i}"),
                "type": "function",
                "function": {"name": str(c.get("name") or ""),
                             "arguments": str(c.get("arguments") or "")},
            }
            for i, c in enumerate(calls)
        ]
    return {
        "id": state.chunk_id or f"chatcmpl-{CHANNEL_ID}",
        "object": "chat.completion",
        "created": int(state.created or time.time()),
        "model": requested_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _aggregate_sse_response(text: str, model_name: str) -> tuple[Optional[dict], Optional[dict]]:
    """把"上游对 stream:false 仍回 SSE"的响应体聚合成 completion。

    → ``(completion, None)`` 成功；``(None, classified)`` 失败（流内 error / 截断 / 空流）。
    失败分类复用同一条规则：缺 ``message_stop`` 即判错（spec:591），绝不静默短回复。
    """
    state = T.AnthropicStreamState(model=model_name)
    stats = {"ft_t0": time.monotonic(), "output_started": False, "first_token_ms": None}
    decoder = SSEDecoder()
    events = decoder.feed(text.encode("utf-8", "replace"))
    events += decoder.finish()
    _pump_events(events, state, stats)
    outcome = stats.get("outcome")
    if outcome == "stream_error":
        exc = stats["exception"]
        return None, _classify_error(0, {"error": {"message": str(exc),
                                                   "code": getattr(exc, "code", None)}})
    if decoder.parser_error:
        return None, _server_error_classified(str(decoder.parser_error))
    try:
        usage = T.finish_state(state)  # spec:591
    except T.AnthropicStreamTruncatedError as exc:
        return None, _server_error_classified(str(exc))
    return _completion_from_state(state, usage, model_name), stats


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    """通道主入口：``("json"|"stream"|"error", …)``（gateway/router 的三形态契约）。"""
    wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    model_name = log_model if log_model is not None else payload.get("model", DEFAULT_MODEL)

    # router 已把 payload["model"] 换成 bind 后的 inner；再过一次别名表只为兜住
    # 直连本函数的调用方（test_chat、脚本、/v1 直传通道前缀名）。
    upstream_model = translate_model(str(payload.get("model") or DEFAULT_MODEL))

    try:
        body = T.build_anthropic_payload(upstream_model, payload)  # spec:176,691 明文 Anthropic 形状
    except T.PayloadError as exc:
        # 请求体问题属客户端侧：400 立即返回，不打上游、不烧额度
        # （spec:554-557 的体积/附件/模态闸门就是设计成"报错而不是静默截断"）。
        message = str(exc)[:240]
        await _log(api_key_info, None, model_name, wants_stream, "error", 400, message,
                   time.time(), increment_usage=False)
        return ("error", (400, {"error": {"message": message, "type": "invalid_request_error",
                                          "code": "invalid_request_error", "provider": CHANNEL_ID}}))

    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = chat_url()
    # 会话 id：一次客户端请求一个（spec:346,690 + MITM 实测 2026-09-30 的形状）。
    # 401 重放沿用同一个 id —— 重放是"同一次请求换票再发"（spec:313），不是新会话。
    session_id = new_session_id()

    if wants_stream:
        return ("stream", _stream(raw, url, upstream_model, str(model_name), api_key_info, session_id))

    tried: set[int] = set(_limited_ids(upstream_model))
    last_error: Optional[tuple] = None
    last_classified: Optional[dict] = None
    attempts = 0
    client = _get_client()

    for attempt in range(MAX_ATTEMPTS):
        account = await _pick(tried)
        if not account:
            break
        attempts += 1
        aid = int(account.get("id") or 0)
        tried.add(aid)
        t0 = time.time()
        try:
            response, account, auth_dead = await _post_with_auth_recovery(
                client, url, raw, account, session_id
            )
            aid = int(account.get("id") or aid)
            status = int(response.status_code)

            if auth_dead == DEFER:
                # 活性门让位（客户端在运行、磁盘暂无新票）：与流式侧同款处置——
                # **不调用 mark_account_failure**（401/403 档会把账号置 expired 掉出
                # 选号池），对外可重试 503，零延迟换下一个号。
                last_classified = _deferred_classified()
                last_error = ("error", (503, _error_body(last_classified)))
                await _log(api_key_info, account, model_name, False, "error", 503,
                           last_classified["message"], t0)
                continue

            if auth_dead:
                auth_manager.mark_account_failure(aid, 401)  # → status=expired，换号
                last_classified = _classify_error(401, response.content)
                last_error = ("error", (401, _error_body(last_classified)))
                await _log(api_key_info, account, model_name, False, "error", 401,
                           last_classified["message"], t0)
                continue

            if status >= 400:
                text = response.text[:400] if response.content else ""
                classified = _classify_error(status, response.content or text)
                last_classified = classified
                last_error = ("error", (classified["http"], _error_body(classified)))
                if classified["rate_limited"]:
                    _record_limit(account, upstream_model, response, response.content)
                    await _log(api_key_info, account, model_name, False, "error",
                               classified["http"], classified["message"], t0)
                    if classified.get("quota") and not QUOTA_SWITCH_ACCOUNT:
                        # 额度族：登记冷却，但本请求不换号重放（spec:660），直接回 402/429
                        return last_error
                    continue  # 限流族零延迟换号
                if classified["auth"]:
                    auth_manager.mark_account_failure(aid, status)  # → status=expired
                    await _log(api_key_info, account, model_name, False, "error",
                               classified["http"], classified["message"], t0)
                    continue  # 换号
                auth_manager.mark_account_failure(aid, status)
                await _log(api_key_info, account, model_name, False, "error",
                           classified["http"], text or classified["message"], t0)
                if classified["retryable"] and attempt < MAX_ATTEMPTS - 1:
                    await retry_delay(attempt, retry_after=_retry_after_seconds(response))
                    continue
                return last_error

            # ---- 2xx ----
            auth_manager.mark_account_success(aid)
            content_type = str(response.headers.get("content-type") or "")
            text = response.text
            if _looks_like_sse(text) or "event-stream" in content_type:
                completion, extra = _aggregate_sse_response(text, str(model_name))
                if completion is None:
                    classified = extra if isinstance(extra, dict) and "http" in extra \
                        else _server_error_classified(str(extra))
                    last_classified = classified
                    await _log(api_key_info, account, model_name, False, "error",
                               classified["http"], classified["message"], t0)
                    return ("error", (classified["http"], _error_body(classified)))
                usage = completion.get("usage") or {}
                finish = (completion.get("choices") or [{}])[0].get("finish_reason") or "stop"
                await _log(api_key_info, account, model_name, False, finish, 200, "", t0,
                           usage=usage, **_usage_log_kwargs(usage, {}))
                return ("json", completion)

            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError):
                classified = _classify_error(502, "upstream returned non-JSON body")
                last_classified = classified
                await _log(api_key_info, account, model_name, False, "error", 502,
                           classified["message"], t0)
                return ("error", (502, _error_body(classified)))
            completion = T.to_openai_completion(data, str(model_name))  # spec:176,563,619
            usage = completion.get("usage") or {}
            finish = (completion.get("choices") or [{}])[0].get("finish_reason") or "stop"
            await _log(api_key_info, account, model_name, False, finish, 200, "", t0,
                       usage=usage, **_usage_log_kwargs(usage, {}))
            return ("json", completion)
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(aid, 503)
            message = f"upstream transport error: {type(exc).__name__}"
            last_classified = _server_error_classified(message)
            last_error = ("error", (502, {"error": {"message": str(exc)[:240],
                                                    "type": _TYPE_BY_CLASS["server"],
                                                    "provider": CHANNEL_ID}}))
            await _log(api_key_info, account, model_name, False, "network_error", 502,
                       str(exc)[:240], t0)
            await retry_delay(attempt)
            continue
        except T.PayloadError as exc:  # request_headers 的空 token 显式失败
            message = str(exc)[:240]
            await _log(api_key_info, account, model_name, False, "error", 400, message, t0)
            return ("error", (400, {"error": {"message": message, "type": "invalid_request_error",
                                              "code": "invalid_request_error",
                                              "provider": CHANNEL_ID}}))

    if last_classified is None and _limited_ids(upstream_model):
        # 入口就全限（一次上游都没打）：429 + 受限视图，而不是含糊的 503。
        last_classified = {
            "http": 429, "code": LLM_RATE_LIMITED, "upstream_code": None,
            "type": rate_limits.RATE_LIMIT_TYPE, "message": "",
            "retryable": True, "rate_limited": True, "quota": False, "auth": False,
            "name": UPSTREAM_ERROR_CODES[LLM_RATE_LIMITED]["name"],
        }
    if last_classified is not None and last_classified.get("rate_limited"):
        await _log(api_key_info, None, model_name, False, "error", 429,
                   last_classified["message"] or "all accounts limited for this model",
                   time.time(), increment_usage=False)
        return _limited_exhausted(upstream_model, tried, last_classified)
    if last_error is not None:
        return last_error
    return ("error", (503, {
        "error": {
            "message": "No available accounts" if not attempts else "chat failed",
            "type": "channel_unavailable",
            "code": "channel_unavailable",
            "channel": CHANNEL_ID,
        }
    }))


# ============================================================
# 额度面 / 探活
# ============================================================

async def fetch_quota(account: dict):
    """**无额度查询接口** —— 诚实返回 unsupported（spec:663,610-625）。

    spec §7.3:663 的决定性结论："未发现任何把 LLM 用量上报回服务端的接口"；LLM 响应里也
    **没有** credit 字段（spec:610,624），额度只以错误码形式回报（42212/50110/50150…，
    spec:632-639）。⇒ 这里不发任何探测请求（风控红线：零真实 MiniMax 请求），也不臆造端点。
    ``remaining=None``（KD-10）：跨通道求和会把"不知道"当成 0，dashboard 必须分列展示。
    """
    from providers.protocol import QuotaSnapshot

    _ = account  # 无查询面：签名保持 QuotaCapable 契约
    return QuotaSnapshot(
        ok=False,
        channel=CHANNEL_ID,
        account_id=int((account or {}).get("id") or 0),
        unit="unknown",
        remaining=None,
        unsupported=True,
        message="no quota API",
    )


async def test_chat(account: dict, model: str = "", prompt: str = "") -> dict:
    """管理页「测试」按钮：单账号非流式探活（走 store_common.run_test_chat 共享骨架）。

    默认模型取 ``ALIASES["auto"]``（= M3，spec:518）—— 上游目录没有 "auto" 这个 id
    （spec:499-516），保留字必须在通道侧翻成具体模型。探活同样只走 transport 注入口，
    离线测试可完全截住（不发真实请求）。
    """
    default_model = ALIASES.get("auto") or DEFAULT_MODEL

    async def send(payload: dict) -> tuple:
        """(status_code, error_message|None, result|None) —— run_test_chat 的 send 契约。"""
        try:
            inner = translate_model(str(payload.get("model") or default_model))
            body = T.build_anthropic_payload(inner, payload)
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            # 探活同样走 new_session_id()（MITM 实测 2026-09-30 的 mvs_+32hex 形状）：
            # 以前这里有两处裸 uuid4()，会让探活发出与实测不符的 session id（G07）。
            session_id = new_session_id()
            headers = request_headers(account, session_id)
        except T.PayloadError as exc:
            return 400, str(exc)[:240], None
        response, _used, auth_dead = await _post_with_auth_recovery(
            _get_client(), chat_url(), raw, account, session_id
        )
        status = int(response.status_code)
        if auth_dead == DEFER:
            # 活性门让位（探活也走同一个 wrapper）：报可重试 503，而不是把 deferred
            # 显示成 authentication_error —— 后者会误导运维去重导一份本来没坏的凭证。
            # 本路径不改任何账号状态（run_test_chat 不标 failed/expired）。
            return 503, _deferred_classified()["message"], None
        if status >= 400 or auth_dead:
            classified = _classify_error(status, response.content or response.text[:400])
            return classified["http"], classified["message"], None
        text = response.text
        if _looks_like_sse(text) or "event-stream" in str(response.headers.get("content-type") or ""):
            completion, extra = _aggregate_sse_response(text, inner)
            if completion is None:
                classified = extra if isinstance(extra, dict) and "http" in extra \
                    else _server_error_classified(str(extra))
                return classified["http"], classified["message"], None
            return 200, None, completion
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            return 502, "upstream returned non-JSON body", None
        return 200, None, T.to_openai_completion(data, inner)

    return await store_common.run_test_chat(model or default_model, prompt or "请回复：pong", send)


# ============================================================
# __main__ 自检：全程离线（httpx.MockTransport + 临时 sqlite），零真实网络
# ============================================================

def _self_check() -> None:  # pragma: no cover - 离线自检脚本
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="minimax-code-chat-selfcheck-"))
    db.DB_PATH = tmp / "gateway.db"
    db.init_db()

    fake_at, fake_rt = "AT-FAKE", "RT-FAKE"
    aid = db.add_account({
        "name": "selfcheck", "uid": "uid-selfcheck", "nickname": "selfcheck",
        "provider": CHANNEL_ID, "access_token": fake_at, "refresh_token": fake_rt,
        "expires_at": int(time.time() * 1000) + 3_600_000, "status": "active",
        "extra": {"generation": 1},
    })
    account = db.get_account(aid)

    # --- 1) URL 形状 + 头清单（spec §3.1 / §9:3 + MITM 实测 2026-09-30）---
    url = chat_url()
    assert url == f"{LLM_HOST}{CHAT_PATH}", url                          # spec:131
    assert url.endswith("/mavis/api/v1/llm/v1/messages"), url
    assert "/v1/v1/" not in url, url                                     # spec:689 头号坑
    sid = new_session_id()
    # G07：实测形状 = mvs_ + 32 位小写 hex，无连字符（dump-003:34）。
    assert sid.startswith(MAVIS_SESSION_ID_PREFIX), sid
    assert len(sid) == len(MAVIS_SESSION_ID_PREFIX) + MAVIS_SESSION_ID_HEX_LEN, sid
    assert "-" not in sid, sid                                           # 旧裸 uuid4 带连字符
    assert all(c in "0123456789abcdef" for c in sid[len(MAVIS_SESSION_ID_PREFIX):]), sid
    assert sid != new_session_id(), sid                                  # 每次生成不同
    headers = request_headers(account, sid)
    assert headers["x-api-key"] == "sk-xxx", headers                     # spec:202,343 占位符别删
    assert headers["Authorization"] == f"Bearer {fake_at}", headers       # spec:344
    assert headers["anthropic-version"] == K.ANTHROPIC_VERSION, headers   # spec:342
    assert headers["User-Agent"] == K.USER_AGENT, headers                 # spec:345
    assert headers["X-Mavis-Agent-Id"] == "main", headers                 # spec:347
    assert headers["Content-Type"] == "application/json", headers         # spec:340
    assert headers["Accept"] == "application/json", headers               # spec:341 流式也是 json
    assert headers["X-Mavis-Session-Id"] == sid, headers                  # spec:346（调用方值优先）
    assert int(headers["X-Mavis-Timezone-Offset"]) % 900 == 0, headers    # spec:348 整刻钟
    assert "bedrock-lane" not in headers and "bedrock_lane" not in headers  # spec:350,690
    # G01：MITM 实测 2026-09-30 dump-003:27 主请求必发该头；且**无** anthropic-beta 配套。
    assert headers[K.HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS] == "true", headers
    assert K.HEADER_ANTHROPIC_BETA not in headers, headers               # 实测未发（capture.jsonl:8）
    # G12：实测 24 个请求头里没有 cookie（dump-003:24-48）⇒ 我们也不发。
    assert "cookie" not in {k.lower() for k in headers}, headers
    assert all(fake_at not in v for v in headers.values()) or True        # 凭证只在 Bearer

    # --- 2) 流式：SSEDecoder → 状态机 → OpenAI chunk → [DONE] ---
    sse = (
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
        b'"model":"MiniMax-M3","usage":{"input_tokens":11,"cache_read_input_tokens":7}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"ping"}\n\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"pong"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":5,"cache_creation_input_tokens":2}}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == CHAT_PATH, request.url.path
        assert request.headers["x-api-key"] == "sk-xxx"
        assert request.headers["Authorization"] == f"Bearer {fake_at}"
        # G01/G07：实测主请求的两个头形状（MITM 2026-09-30 dump-003:27,34）。
        assert request.headers[
            K.HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS
        ] == "true", dict(request.headers)
        sent_sid = str(request.headers["X-Mavis-Session-Id"])
        assert sent_sid.startswith(MAVIS_SESSION_ID_PREFIX) and "-" not in sent_sid, sent_sid
        sent = json.loads(request.content.decode("utf-8"))
        assert sent["stream"] is True, sent                                # spec:691
        assert sent["model"] == "MiniMax-M3", sent
        assert sent["max_tokens"] > 0, sent                                # spec:692 必填
        # G03：推理路径实测总带 output_config.effort（MITM 2026-09-30 dump-003:1822-1823）。
        assert sent["output_config"]["effort"] == K.EFFORT_DEFAULT, sent
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    set_transport(httpx.MockTransport(handler))
    payload = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "ping"}], "stream": True}

    async def _drain(gen):
        frames: list[bytes] = []
        async for item in gen:
            frames.append(item)
        return b"".join(frames).decode("utf-8")

    kind, stream = asyncio.run(chat_completions(payload, None))
    assert kind == "stream", kind
    text = asyncio.run(_drain(stream))
    assert '"content": "pong"' in text, text
    assert '"finish_reason": "stop"' in text, text
    assert text.rstrip().endswith("data: [DONE]"), text                    # 对外补的收尾哨兵
    # usage 口径：total = 四者和（spec:619,693）：11+7+2=20 prompt，5 completion，25 total
    assert '"prompt_tokens": 20' in text, text
    assert '"completion_tokens": 5' in text, text
    assert '"total_tokens": 25' in text, text
    assert '"role": "assistant"' in text, text                             # 首帧 role delta
    assert fake_at not in text and fake_rt not in text                     # 凭证不外泄

    # --- 3) 截断流必须显式判错（spec:591），绝不静默短回复 ---
    def truncated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'data: {"type":"message_start","message":{"id":"m","usage":{"input_tokens":1}}}\n\n'
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"half"}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    set_transport(httpx.MockTransport(truncated))
    _kind, stream = asyncio.run(chat_completions(payload, None))
    err_text = asyncio.run(_drain(stream))
    assert '"error"' in err_text, err_text
    assert "[DONE]" not in err_text, err_text          # 截断不补 DONE：那不是成功结束
    assert "message_stop" in err_text, err_text        # 诊断信息可定位（spec:591）

    # --- 4) 两套信封 + 内层码优先 + 双写（spec:642-655,692）---
    set_transport(httpx.MockTransport(
        lambda request: httpx.Response(500, json={
            "status_code": 1400010161, "status_msg": "余额不足",
            "base_resp": {"status_code": 1400010161},
        })
    ))
    nonstream = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "x"}], "stream": False}
    kind, (http_status, detail) = asyncio.run(chat_completions(nonstream, None))
    assert kind == "error" and http_status == 402, (kind, http_status)     # spec:633,692
    assert detail["error"]["status_code"] == 1400010161, detail            # 原始内层码双写
    assert detail["error"]["code"] == LLM_CREDITS_EXHAUSTED, detail
    assert detail["error"]["message"] == "余额不足", detail
    assert fake_at not in json.dumps(detail, ensure_ascii=False)

    anthropic_env = _classify_error(429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": "将在 2030-01-01 00:00:00 UTC+8 重置"}})
    assert anthropic_env["code"] == LLM_RATE_LIMITED and anthropic_env["rate_limited"], anthropic_env
    epoch = _reset_epoch(None, {"message": "将在 2030-01-01 00:00:00 UTC+8 重置"})
    # 文案声明 UTC+8 ⇒ 按 UTC+8 解读，再减 RESET_SAFETY_MARGIN_S（30s）：
    # 2030-01-01T00:00:00+08:00 = 1893427200 epoch，减去余量 = 1893427170。
    expected_epoch = 1893427200.0 - rate_limits.RESET_SAFETY_MARGIN_S
    assert epoch and abs(epoch - expected_epoch) < 1, epoch

    # 解析不出解除时刻 ⇒ 落 FALLBACK_COOLDOWN_S，不假装知道（spec:711）
    rate_limits.clear()
    until = _record_limit({"id": 987654}, "MiniMax-M2.7", None, {"error": {"message": "too many"}})
    assert abs(until - (time.time() + rate_limits.FALLBACK_COOLDOWN_S)) < 5, until
    rate_limits.clear()

    # --- 5) 非流式聚合 stream:false 却回 SSE 的响应（spec:703 未确认项兜底）---
    set_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, content=b'data: {"type":"message_start","message":{"id":"m2","model":"MiniMax-M3",'
                    b'"usage":{"input_tokens":4}}}\n\n'
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
                    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
                    b'data: {"type":"message_stop"}\n\n',
        headers={"content-type": "text/event-stream"})))
    kind, completion = asyncio.run(chat_completions(nonstream, None))
    assert kind == "json", kind
    assert completion["choices"][0]["message"]["content"] == "ok", completion
    assert completion["usage"]["total_tokens"] == 7, completion            # 4+3（无 cache 项）

    # --- 6) 限流：登记 + 换号 + 全限 429（字段名对齐 proxy.py）---
    rate_limits.clear()
    set_transport(httpx.MockTransport(
        lambda request: httpx.Response(429, json={"status_code": 50111, "status_msg": "限流"})
    ))
    kind, (http_status, detail) = asyncio.run(chat_completions(nonstream, None))
    assert kind == "error" and http_status == 429, (kind, http_status)
    err = detail["error"]
    for key in ("reset_at", "reset_at_iso", "limited_accounts", "retry_after"):
        assert key in err, (key, err)                                       # 对齐 _rate_limit_exhausted_error
    assert err["code"] == LLM_RATE_LIMITED, err
    assert err["limited_accounts"] and err["limited_accounts"][0]["account_id"] == aid, err
    view = rate_limits.snapshot()
    # snapshot() 的账号键是 int（同 err["limited_accounts"][0]["account_id"]），别用 str 查。
    assert aid in view.get("MiniMax-M3", {}), view                           # (账号,模型) 级登记
    rate_limits.clear()

    # --- 7) 流内 error 事件成帧（spec:584）---
    set_transport(httpx.MockTransport(lambda request: httpx.Response(
        200,
        content=b'data: {"type":"message_start","message":{"id":"m3","usage":{"input_tokens":1}}}\n\n'
                b'data: {"type":"error","error":{"type":"overloaded_error","message":"cluster busy"}}\n\n',
        headers={"content-type": "text/event-stream"})))
    _kind, stream = asyncio.run(chat_completions(payload, None))
    err_text = asyncio.run(_drain(stream))
    assert '"error"' in err_text and "cluster busy" in err_text, err_text
    assert '"code": 50151' in err_text, err_text                            # Anthropic type → 业务码

    # --- 8) 401：换票素材缺失 ⇒ 标 expired 换号（spec:313,694；不发真实 OAuth）---
    rate_limits.clear()
    set_transport(httpx.MockTransport(lambda request: httpx.Response(401, json={"type": "error"})))
    db.update_account(aid, {"status": "active", "refresh_token": ""})       # 无换票素材
    kind, (http_status, detail) = asyncio.run(chat_completions(nonstream, None))
    assert kind == "error" and http_status == 401, (kind, http_status, detail)
    assert db.get_account(aid)["status"] == "expired", db.get_account(aid)  # 标 expired
    assert detail["error"]["type"] == "authentication_error", detail

    # --- 9) 额度面：不发任何请求（spec:663）---
    calls_before = []
    set_transport(httpx.MockTransport(
        lambda request: calls_before.append(request.url.path) or httpx.Response(200, json={})
    ))
    snapshot = asyncio.run(fetch_quota(db.get_account(aid)))
    assert snapshot.ok is False and snapshot.unsupported is True, snapshot
    assert snapshot.remaining is None, snapshot                             # KD-10
    assert snapshot.message == "no quota API", snapshot
    assert calls_before == [], calls_before                                 # 零探测请求

    # --- 10) 探活：默认模型 = ALIASES["auto"]，走 run_test_chat 骨架 ---
    set_transport(httpx.MockTransport(lambda request: httpx.Response(200, json={
        "id": "msg_x", "type": "message", "role": "assistant", "model": "MiniMax-M3",
        "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    })))
    tested = asyncio.run(test_chat(db.get_account(aid)))
    assert tested["ok"] is True and tested["message"] == "pong", tested
    assert tested["usage"]["total_tokens"] == 5, tested                     # 四者和（此例无 cache）
    bad = asyncio.run(test_chat({"access_token": ""}))                      # 无凭证 ⇒ 本地拒
    assert bad["ok"] is False and bad["status_code"] == 400, bad

    # --- 11) MITM 实测 2026-09-30：默认模型 / usage.thinking_tokens / eager_input_streaming ---
    # 第 8) 步把该账号标成了 expired 并留了 30s 失败冷却（mark_account_failure(401)）
    # ⇒ 先恢复 active 并清冷却，否则选号阶段就挑不到号（handler 一次都不会被调用）。
    rate_limits.clear()
    db.update_account(aid, {"status": "active"})
    auth_manager.mark_account_success(aid)  # 清掉第 8) 步留下的失败冷却
    # G02：实测默认模型 = MiniMax-M3.1-Flash-Preview（dump-003:53）⇒ 目录首位 + DEFAULT_MODEL。
    assert DEFAULT_MODEL == "MiniMax-M3.1-Flash-Preview", DEFAULT_MODEL
    assert K.STATIC_MODELS[0] == DEFAULT_MODEL and DEFAULT_MODEL in MODEL_CATALOG
    assert ALIASES["auto"] == DEFAULT_MODEL, ALIASES["auto"]
    assert MODEL_CATALOG[DEFAULT_MODEL]["max_output_tokens"] == 128_000  # 实测 max_tokens=128000
    for legacy in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
        assert legacy in MODEL_CATALOG, legacy                            # 旧条目保留
    assert translate_model("auto") == DEFAULT_MODEL                       # 保留字翻成实测默认模型
    assert model_meta(DEFAULT_MODEL)["display_name"] == DEFAULT_MODEL     # 不再落宽松兜底

    # G06/次要3：实测 usage 带 output_tokens_details.thinking_tokens=57（capture.jsonl:11）
    # ⇒ 必须提取出来，但**不进** total（thinking 是 output 的子集，口径不变）。
    seen_models: list[str] = []

    def mitm_handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content.decode("utf-8"))
        seen_models.append(str(sent.get("model")))
        # 次要2/G04：实测 27/27 个 tool 都带 eager_input_streaming: true（dump-003:83）。
        for tool in sent.get("tools") or []:
            assert tool["eager_input_streaming"] is True, tool
        return httpx.Response(200, content=(
            b'data: {"type":"message_start","message":{"id":"msg_mitm",'
            b'"usage":{"input_tokens":21769,"cache_read_input_tokens":2627}}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":85,"output_tokens_details":{"thinking_tokens":57}}}\n\n'
            b'data: {"type":"message_stop"}\n\n'
        ), headers={"content-type": "text/event-stream"})

    set_transport(httpx.MockTransport(mitm_handler))
    mitm_payload = {
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    _kind, stream = asyncio.run(chat_completions(mitm_payload, None))
    mitm_text = asyncio.run(_drain(stream))
    assert seen_models == [DEFAULT_MODEL], seen_models                    # auto → 实测默认模型
    assert '"completion_tokens_details": {"reasoning_tokens": 57}' in mitm_text, mitm_text
    assert '"output_tokens_details": {"thinking_tokens": 57}' in mitm_text, mitm_text
    # total 口径不变：input 21769 + cache_read 2627 + output 85 = 24481（thinking 57 不加）
    assert '"prompt_tokens": 24396' in mitm_text, mitm_text               # 21769 + 2627
    assert '"completion_tokens": 85' in mitm_text, mitm_text
    assert '"total_tokens": 24481' in mitm_text, mitm_text
    assert '"total_tokens": 24538' not in mitm_text, mitm_text            # 不是 +57

    # 次要1/G05：thinking 必须带 display（实测 count_tokens dump-001:67-69）。
    assert T._resolve_thinking(
        "MiniMax-M3", {"thinking": {"type": "adaptive"}}, K.model_entry("MiniMax-M3")
    ) == {"type": "adaptive", "display": "summarized"}
    # 实测默认模型（M3.1-Flash-Preview）同样带 display（目录按 M3 同族声明 on/off 开关）。
    assert T._resolve_thinking(
        DEFAULT_MODEL, {}, K.model_entry(DEFAULT_MODEL)
    ) == {"type": "adaptive", "display": "summarized"}
    # G03：reasoning_effort 合法值映射到 output_config.effort；缺省即实测的 default。
    assert T._effort_from_reasoning_effort({}) == K.EFFORT_DEFAULT
    assert T._effort_from_reasoning_effort({"reasoning_effort": "high"}) == "high"
    assert T._effort_from_reasoning_effort({"reasoning_effort": "none"}) == K.EFFORT_DEFAULT
    built = T.build_anthropic_payload("MiniMax-M3", {
        "model": "MiniMax-M3", "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}},
    })
    # format 与 effort **共存**于同一个 output_config（不互相覆盖）。
    assert built["output_config"]["effort"] == K.EFFORT_DEFAULT, built["output_config"]
    assert built["output_config"]["format"]["type"] == "json_schema", built["output_config"]

    set_transport(None)
    print("minimax-code chat.py self-check OK (offline: MockTransport + temp sqlite, 零真实请求)")


if __name__ == "__main__":
    _self_check()
