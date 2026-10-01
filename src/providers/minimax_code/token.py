"""MiniMax Code — OAuth2 refresh_token 轮换（grant_type=refresh_token）。

协议事实唯一权威来源：
``.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md``（下文 ``spec:NNN``
= 该行号；spec 未覆盖的点一律写成可配置 + TODO 注释，不编造端点或 header）。

端点（spec:239-246,257,312,694）：
    POST {channel_host(CHANNEL_ID,"oauth",OAUTH_HOST)}/oauth2/token
    Content-Type: application/x-www-form-urlencoded     spec:257（oauth-client.js postForm）
    body: grant_type=refresh_token & refresh_token=<库内值> & client_id=mcode-public
          spec:312（auth-core.js refreshCredential）+ spec:694（落地要点 7）
    响应字段：access_token / token_type / refresh_token / expires_in / scope / audience
          spec:258

刷新语义（spec §2.4:310-317）：
- 主动：调用方给 minValidityMs，``expiresAtMs - now >= minValidityMs`` 不满足则刷新
  （spec:312）。客户端租约口径 5 分钟（spec:316 AUTH_LEASE_MAX_MIN_VALIDITY_MS）。
- 被动：401 → 刷新 → 单次重放；刷新失败 → logout（spec:313）。401 处理链在 chat.py，
  本模块只负责"刷新 + 落库 + 回传布尔"。
- 换代：每次成功刷新 generation+1；loginEpoch 同一登录内不变（spec:314）。generation
  记进 extra 仅作通道侧轮转计数（客户端自己的 generation 序列在它的 auth-state.json 里，
  两边不同步——因为我们不回写，见下）。

⚠️ 共存/风控红线（spec:315,694）—— refresh_token 是一次一轮转的：
    桌面客户端与网关共用同一份凭证时，任何一方先刷新都会把另一方的 refresh_token
    作废（客户端侧还有 auth.lock 串行，网关在锁外，绕不过去）。因此本通道约定：
      1) **只在通道侧刷新**（库内凭证独立成代）；
      2) **永不回写桌面客户端的 auth.json / auth-state.json**——qodercn 那种
         write_refreshed_auth 回写在本通道是主动禁止的行为，不是漏实现；
      3) WRITEBACK_TO_CLIENT_AUTH_JSON 仅为将来"接管客户端登录态"预留的开关，
         **默认 False 且当前没有任何实现路径读取它**。要启用需先解决跨进程文件锁
         与 generation 对齐（spec:261-296 的 FileStore schema），届时新写函数，
         不要顺手在本模块里塞写入逻辑。

凭证安全：access_token / refresh_token 原文绝不进日志、异常消息、断言字面量。
异常消息只带 HTTP 状态与 OAuth error 码，**不回显响应体**（WAF/错误页内容不可控）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional
from urllib.parse import parse_qs

import httpx

# 契约 sketch 里的符号名（OAUTH_HOST/LLM_HOST）与实际 constants.py（ACCOUNT_HOST_CN_PROD/
# AGENT_HOST_CN）有出入；本文件一律以 constants.py 现存符号为准，getattr 兜底防止契约层
# 后续改名把这里 import 打断。禁止在此硬编码端点——默认值全部来自 constants（其值=spec:241）。
from providers.minimax_code import constants as _C
from providers.minimax_code.constants import CHANNEL_ID, TOKEN_PATH  # spec:115,244
from providers.host_override import channel_host
from providers.store_common import jwt_exp_ms  # spec:258（access_token 是 JWT，读 exp 兜底）
from storage.http_pool import get_client  # 共享连接池，不自建常驻 AsyncClient

logger = logging.getLogger(__name__)

OAUTH_HOST = getattr(_C, "OAUTH_HOST", None) or getattr(_C, "ACCOUNT_HOST_CN_PROD", None)  # spec:241
CLIENT_ID = getattr(_C, "OAUTH_CLIENT_ID", None) or getattr(_C, "CLIENT_ID", None)  # spec:235 mcode-public
GRANT_TYPE_REFRESH = getattr(_C, "REFRESH_GRANT_TYPE", None) or "refresh_token"  # spec:312
FORM_CONTENT_TYPE = getattr(_C, "OAUTH_FORM_CONTENT_TYPE", None) or "application/x-www-form-urlencoded"  # spec:257

# ---------------------------------------------------------------------------
# 刷新阈值（可配置）
# ---------------------------------------------------------------------------
# spec:312 只给出 minValidityMs 的**比较语义**（expiresAtMs - now >= minValidityMs 则不刷）；
# 实机验证（ROTATION-VERDICT.md）：access token TTL≈1h、refresh_token 每次刷新轮转
# （spec:317 记的"观测约 11 天"是误记，已推翻）。60_000 这个数值来自任务契约
# （TOKEN_REFRESH_SKEW_MS），非 spec 实证——所以做成常量 + 环境变量覆盖，别当事实引用。
TOKEN_REFRESH_SKEW_MS = 60_000


def _env_int(name: str, default: int) -> int:
    """环境变量覆盖（repo 惯例 CB_* 前缀）；解析失败回退默认值。"""
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value >= 0 else default


# 到期判定的负缓冲：距到期不足该窗口即视为"需要刷新"。
REFRESH_SKEW_MS = _env_int("CB_MINIMAX_CODE_REFRESH_SKEW_MS", TOKEN_REFRESH_SKEW_MS)
# 主动预刷窗口：沿用客户端租约上限 5 分钟（spec:316 AUTH_LEASE_MAX_MIN_VALIDITY_MS；
# constants.REFRESH_MIN_VALIDITY_MS 同值，spec:133）。取两者较大者做阈值。
PROACTIVE_MIN_VALIDITY_MS = max(
    REFRESH_SKEW_MS,
    int(getattr(_C, "REFRESH_MIN_VALIDITY_MS", 0) or 0),
)


class MiniMaxCodeAuthError(RuntimeError):
    """MiniMax Code token 刷新失败（消息绝不含凭证原文）。

    kind:
      invalid_grant —— refresh 被拒（400/401）：换票失败，调用方应把账号打成 expired；
      bad_response  —— 协议异常（非 JSON / 200 但缺 access_token）：不重试也不判死，交调用方；
      network       —— 连接/超时等传输层错误：可重试；
      server        —— 429/5xx 及其余未分类状态：可重试。
    """

    def __init__(self, message: str, status: int = 0, kind: str = "server"):
        self.status = status
        self.kind = kind
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return self.kind in ("network", "server")

    @property
    def invalid_grant(self) -> bool:
        return self.kind == "invalid_grant"


# ---------------------------------------------------------------------------
# 过期判定
# ---------------------------------------------------------------------------

def remaining_validity_ms(account: dict, *, now_ms: Optional[int] = None) -> int:
    """``expiresAtMs - now``（spec:312 的比较左值）；无到期信息返回 0（视作已到期）。"""
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return 0
    return expires_at - (now_ms if now_ms is not None else int(time.time() * 1000))


def is_token_expired(account: dict, skew_ms: int = REFRESH_SKEW_MS, *, now_ms: Optional[int] = None) -> bool:
    """spec:312 语义取反：剩余寿命 < skew 即"过期"（需要刷新）。

    无到期信息（expires_at<=0）按已过期处理（与 auth_manager/traesolo 一致）。
    若上游从不回 expires_in 且 JWT 也不可解，会退化成"逢请求必刷"——由调用方的
    refresh 失败负缓存（trae_shared）兜住，本模块不自行造节流。
    TODO(spec:704)：真实 TTL 需 MITM 确认后才能收紧这里。
    """
    return remaining_validity_ms(account, now_ms=now_ms) < int(skew_ms)


def needs_pre_refresh(account: dict, *, now_ms: Optional[int] = None) -> bool:
    """主动刷新判定：进入客户端同款租约窗口（PROACTIVE_MIN_VALIDITY_MS）即刷。"""
    return is_token_expired(account, skew_ms=PROACTIVE_MIN_VALIDITY_MS, now_ms=now_ms)


# ---------------------------------------------------------------------------
# 回写开关（默认关闭；本模块无实现路径读取它——纯登记，防将来手滑）
# ---------------------------------------------------------------------------
# 打开前提：先解决与桌面客户端的 auth.lock 跨进程互斥与 generation 对齐（spec:315,261-296）。
WRITEBACK_TO_CLIENT_AUTH_JSON = False


# ---------------------------------------------------------------------------
# token endpoint
# ---------------------------------------------------------------------------

def _oauth_base(account: dict) -> str:
    """host 一律走 channel_host；extra.api_host 允许逐账号覆盖（staging/镜像联调用）。"""
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    host = str(extra.get("api_host") or "").strip().rstrip("/")
    if host:
        return host
    # host_override.CHANNEL_HOST_FIELDS 已登记 (CHANNEL_ID, "llm"/"oauth")，所以管理端 UI
    # 能保存这两个键（channel_host 运行时不校验白名单，但 UI 会拒绝未登记的键）。
    return channel_host(CHANNEL_ID, "oauth", OAUTH_HOST)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _oauth_error_code(data: dict) -> str:
    """尽力提取 OAuth2 error 码（spec:258 未定义错误体，仅按 RFC 常见形状读；读不到就空）。"""
    for key in ("error", "status_code", "code"):
        hit = data.get(key)
        if hit is not None and not isinstance(hit, (dict, list)):
            return str(hit)[:64]
    return ""


async def _post_token(client: httpx.AsyncClient, base: str, refresh: str) -> dict:
    """POST form 到 token endpoint。

    被拒（400/401）→ 抛 kind=invalid_grant；网络/5xx/其它 → 抛对应可分类异常。
    refresh_account 在边界把 invalid_grant 翻译成 False。绝不回显响应体原文。
    """
    # spec:257：全表单体；除 Content-Type 外 spec 未给 token endpoint 专属头 ⇒ 不加，
    # 防编造（AUTH_SUBPATH/桌面 UA 之类与本请求无关）。
    headers = {"Content-Type": FORM_CONTENT_TYPE}
    body = {
        "grant_type": GRANT_TYPE_REFRESH,  # spec:312,694
        "refresh_token": refresh,  # spec:694（库内值，可能已被桌面端轮转掉→invalid_grant）
        "client_id": CLIENT_ID,  # spec:235,253 mcode-public
    }
    try:
        response = await client.post(f"{base}{TOKEN_PATH}", headers=headers, data=body, timeout=30.0)
    except httpx.HTTPError as exc:
        # 只报异常类名；httpx 异常字符串可能含 URL（URL 无凭证，但保守起见不透传原文）
        raise MiniMaxCodeAuthError(
            f"minimax-code token endpoint transport error: {type(exc).__name__}",
            kind="network",
        ) from exc

    status = response.status_code
    if status in (400, 401):
        # OAuth2 拒绝：invalid_grant / invalid_client 等。不进负缓存语义由调用方决定；
        # 调用方应把账号置 expired（任务契约 §3）。
        error_code = ""
        try:
            error_code = _oauth_error_code(response.json())
        except Exception:  # noqa: BLE001 - 拒答体不可解析不影响分类
            pass
        raise MiniMaxCodeAuthError(
            f"minimax-code refresh rejected: HTTP {status}" + (f" error={error_code}" if error_code else ""),
            status=status,
            kind="invalid_grant",
        )
    if status == 429 or status >= 500:
        raise MiniMaxCodeAuthError(
            f"minimax-code token endpoint unavailable: HTTP {status}",
            status=status,
            kind="server",
        )
    if status >= 400:
        # 404/405 等：端点被改/被挡，属于配置问题，判死账号是错的——可分类但不重试不判死。
        raise MiniMaxCodeAuthError(
            f"minimax-code token endpoint unexpected status: HTTP {status}",
            status=status,
            kind="bad_response",
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise MiniMaxCodeAuthError(
            f"minimax-code token refresh returned non-JSON: HTTP {status}",
            status=status,
            kind="bad_response",
        ) from exc
    if not isinstance(data, dict):
        raise MiniMaxCodeAuthError(
            f"minimax-code token refresh payload is not an object: HTTP {status}",
            status=status,
            kind="bad_response",
        )
    return data


# ---------------------------------------------------------------------------
# 并发防呆：同账号互斥
# ---------------------------------------------------------------------------
# 网关内两个并发请求对同一账号同时刷新 = 自己顶掉自己刚拿到的新 refresh_token
# （轮转语义，spec:694）。按 (loop, 账号) 建锁；跨事件循环不复用旧锁对象。
_refresh_locks: dict[tuple[int, int], tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _lock_for_account(aid: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), aid)
    entry = _refresh_locks.get(key)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _refresh_locks[key] = (loop, lock)
        return lock
    return entry[1]


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

async def refresh_account(account: dict) -> bool:
    """用库内 refresh_token 换新一代 token 并落库。

    返回 True  = 已轮换并写回（status=active、generation+1 记入 extra）；
    返回 False = refresh 被拒（400/401 invalid_grant）——**调用方应把账号打成 expired**；
    抛 MiniMaxCodeAuthError = 网络/5xx/协议异常（.kind/.retryable 可分类，不写库、不改状态）。

    永不回写桌面客户端 auth.json（spec:694 共存红线，见模块 docstring）。
    """
    from storage import database as db

    aid = int(account.get("id") or 0)
    refresh = str(account.get("refresh_token") or "")
    if not aid:
        raise MiniMaxCodeAuthError("minimax-code refresh_account: account has no id", kind="bad_response")
    if not refresh:
        # 没有换票素材 ≈ 该凭证已不可用；按被拒处理，让调用方走 expired 分支。
        logger.warning("minimax-code account %s has no refresh_token", aid)
        return False

    async with _lock_for_account(aid):
        try:
            data = await _post_token(get_client(), _oauth_base(account), refresh)
        except MiniMaxCodeAuthError as exc:
            if exc.invalid_grant:
                # 凭证安全：只打状态与 error 码（消息里本就没有凭证原文），不打响应体。
                logger.warning("minimax-code refresh rejected (account=%s): %s", aid, exc)
                return False
            raise

        access = str(data.get("access_token") or "")
        if not access:
            raise MiniMaxCodeAuthError(
                "minimax-code token response missing access_token",
                kind="bad_response",
            )
        new_refresh = str(data.get("refresh_token") or "") or refresh  # spec:694 可能轮转也可能不变
        expires_in = _to_int(data.get("expires_in"))
        now_ms = int(time.time() * 1000)
        if expires_in > 0:
            # 任务契约 §1：expires_at = now_ms + expires_in*1000，整数毫秒（spec:312 的 expiresAtMs 口径）
            expires_at = now_ms + expires_in * 1000
        else:
            # spec 未保证一定带 expires_in；access_token 是 JWT（spec:258），退而读 exp。
            # 两者皆无 → 0，is_token_expired 会按过期处理（TODO: 真实 TTL 待 MITM，spec:704）。
            expires_at = jwt_exp_ms(access)

        extra_old = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        extra = dict(extra_old)
        # 换代：每次成功刷新 generation+1（spec:314 auth-core.js:509,526）。这是**通道侧**
        # 序列（我们从不与客户端的 auth-state.json 同步，登录代际由 loginEpoch 表达、本模块不动）。
        extra["generation"] = _to_int(extra.get("generation"), 0) + 1
        # 非凭证元信息原样存档（scope/audience/token_type 见 spec:236,237,258）。
        for src_key, dst_key in (("token_type", "oauth_token_type"),
                                 ("scope", "oauth_scope"),
                                 ("audience", "oauth_audience")):
            value = data.get(src_key)
            if isinstance(value, str) and value:
                extra[dst_key] = value

        patch: dict = {
            "access_token": access,
            "refresh_token": new_refresh,
            "expires_at": int(expires_at),
            "status": "active",
            "extra": extra,
        }
        db.update_account(aid, patch)
        # 任务契约：本通道**不回写**客户端 auth.json（qodercn 的 write_refreshed_auth 在这里
        # 是被禁止的；WRITEBACK_TO_CLIENT_AUTH_JSON 默认 False 且无实现路径读取）。
        return True


# ---------------------------------------------------------------------------
# __main__ 自检：全程离线（httpx.MockTransport + 临时 sqlite），不发任何真实网络包
# ---------------------------------------------------------------------------

def _self_check() -> None:  # pragma: no cover - 自检脚本
    import tempfile
    from pathlib import Path

    from storage import database as db

    tmp = Path(tempfile.mkdtemp(prefix="minimax-code-token-selfcheck-"))
    db.DB_PATH = tmp / "gateway.db"
    db.init_db()

    fake_at, fake_rt = "AT-FAKE", "RT-FAKE"
    new_at, new_rt = "AT-FAKE-GEN2", "RT-FAKE-ROTATED"
    calls: list[httpx.Request] = []

    def _ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": new_at,
                "token_type": "Bearer",
                "refresh_token": new_rt,
                "expires_in": 3600,
                "scope": "agent.default",
                "audience": "agent-backend",
            },
        )

    def _install(handler) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # 与 tests/test_perf_providers.py 同法替换 get_client；但本脚本经 `python -m`
        # 运行时模块 __name__ 是 __main__，`import ...token as tk` 会加载出**第二个实例**、
        # patch 打在没人用的那份上——必须直接改本模块 globals 才生效。
        globals()["get_client"] = lambda: client

    aid = db.add_account({
        "name": "selfcheck", "uid": "uid-selfcheck", "provider": CHANNEL_ID,
        "access_token": fake_at, "refresh_token": fake_rt,
        "expires_at": 0, "status": "expired",
        "extra": {"generation": 1, "note": "keep-me"},
    })

    # --- 1) 成功轮换：请求形状 + 落库 ---
    def _capture_ok(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_handler(request)

    _install(_capture_ok)
    account = db.get_account(aid)
    assert asyncio.run(refresh_account(account)) is True, "成功轮换应返回 True"
    req = calls[-1]
    assert req.url.path == TOKEN_PATH, req.url.path  # spec:244 单段 /oauth2/token
    assert req.url.host == "account.minimax.cn", req.url.host  # spec:241
    assert req.headers.get("content-type", "") == FORM_CONTENT_TYPE  # spec:257 表单体
    form = dict(__import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(req.content.decode("utf-8")))
    assert form.get("grant_type") == [GRANT_TYPE_REFRESH]  # spec:312
    assert form.get("refresh_token") == [fake_rt]  # 假值，仅限本自检内存
    assert form.get("client_id") == [CLIENT_ID]  # spec:235
    fresh = db.get_account(aid)
    assert fresh["access_token"] == new_at and fresh["refresh_token"] == new_rt  # 轮转覆盖
    assert fresh["status"] == "active"
    now_ms = int(time.time() * 1000)
    assert isinstance(fresh["expires_at"], int) and abs(fresh["expires_at"] - (now_ms + 3_600_000)) < 60_000, fresh["expires_at"]
    assert fresh["extra"].get("generation") == 2, fresh["extra"]  # spec:314 +1
    assert fresh["extra"].get("note") == "keep-me"  # 原 extra 合并
    assert fresh["extra"].get("oauth_audience") == "agent-backend" and fresh["extra"].get("oauth_scope") == "agent.default"

    # --- 2) 被拒：400 invalid_grant → False，状态不动 ---
    _install(lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    assert asyncio.run(refresh_account(db.get_account(aid))) is False, "invalid_grant 应返回 False"
    assert db.get_account(aid)["refresh_token"] == new_rt, "被拒不得半更新"
    assert db.get_account(aid)["status"] == "active", "expired 标记是调用方的事"

    # --- 3) 网络/5xx：抛可分类异常，且异常消息不含凭证 ---
    _install(lambda request: httpx.Response(503, text="gateway down"))
    try:
        asyncio.run(refresh_account(db.get_account(aid)))
        raise AssertionError("5xx 必须抛异常而不是返回 False")
    except MiniMaxCodeAuthError as exc:
        assert exc.kind == "server" and exc.retryable and exc.status == 503
        assert new_rt not in str(exc) and new_at not in str(exc), "异常消息禁止含凭证"
        assert "gateway down" not in str(exc), "禁止回显响应体"

    _install(lambda request: (_ for _ in ()).throw(httpx.ConnectError("stub")))
    try:
        asyncio.run(refresh_account(db.get_account(aid)))
        raise AssertionError("传输层错误必须抛异常")
    except MiniMaxCodeAuthError as exc:
        assert exc.kind == "network" and exc.retryable

    # --- 4) 过期判定：spec:312 的 skew 语义 ---
    assert is_token_expired({"expires_at": 0}) is True
    assert is_token_expired({"expires_at": now_ms + 30_000}) is True  # 剩余 30s < 60s skew
    assert is_token_expired({"expires_at": now_ms + 90_000}) is False
    assert needs_pre_refresh({"expires_at": now_ms + 4 * 60 * 1000}) is True  # 5 分钟租约窗口（spec:316）
    assert needs_pre_refresh({"expires_at": now_ms + 6 * 60 * 1000}) is False

    # --- 5) 端点契约不依赖环境：默认 host 走 constants（禁止硬编码在本文件） ---
    assert OAUTH_HOST == "https://account.minimax.cn"  # spec:241（值登记于 constants.py）
    assert _oauth_base({}) == channel_host(CHANNEL_ID, "oauth", OAUTH_HOST)
    assert _oauth_base({"extra": {"api_host": "https://mirror.invalid"}}) == "https://mirror.invalid"

    print("minimax-code token.py self-check OK (offline, MockTransport only)")


if __name__ == "__main__":
    _self_check()
