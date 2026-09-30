"""MiniMax Code 桌面端凭证的**发现/导入**层——只读，绝不回写客户端文件。

协议事实唯一权威来源：
`.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md`（行号 = 该文件行号）。

风控红线（spec:694）——**只读快照**决策：
桌面客户端把 OAuth 凭证明文写在 `~/.minimax/auth/prod/cn/mcode-public/auth.json`
（spec:269,275,694），本通道与它**共用同一份文件**。客户端每次成功刷新都会
`generation + 1`（spec:314，auth-core.js:509,526），refresh_token 也可能轮转
（spec:694,704），并且客户端与网关都持 `auth.lock` 跨进程锁（spec:279,315）。
⇒ 任何回写都会把正在运行的桌面客户端顶掉（反之亦然），因此本模块：

  * 只读：`read_text()` auth.json（外加同目录 auth-state.json 的 status 做诊断），
    **不提供任何写回函数**（qodercn/qwenwork 的 `write_refreshed_auth()` 在这里
    刻意不存在，不是漏写）；
  * 通道自己刷新出来的新 token 只进我们的 DB（由 refresh 模块负责），
    永不落回客户端文件；
  * discover 预览的每条 meta 里带 `warning`、顶层带 `note`，提示"该凭证与本机
    客户端共享，客户端再登录会使通道凭证失效"。

凭证安全：access_token / refresh_token 原文绝不进日志、异常字符串或断言字面量；
所有诊断信息只包含字段名、枚举值、计数与路径。

命名空间硬边界（spec §8:669、spec:8）：本实例是 **prod / zh / cn**，不是
staging/test/internal/inside，也不是 en。目录层与记录层**两道**都要过，见
`minimax_auth_dirs()` 与 `_parse_auth_json()` 的注释。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

from providers.minimax_code import constants as K
from providers.store_common import (
    dedupe_dirs,
    discover_dirs,
    imported_file_meta,
    is_relative_to,
    iso_to_ms,
    jwt_exp_ms,
    upsert_account as upsert_account_by_uid,
)

logger = logging.getLogger(__name__)


def _const(name: str, default):
    """从契约层 constants 取值，缺失则回退到 spec 的已知值。

    constants.py 是契约层文件、符号名可能随审计调整；这里用 getattr + 兜底，
    保证本模块不因契约层改名而 ImportError。兜底值一律带 spec 行号可审。
    """
    value = getattr(K, name, None)
    return default if value is None or value == "" else value


# --- 契约常量（缺省值出处见括号内 spec 行号） ---
CHANNEL_ID = str(_const("CHANNEL_ID", "minimax-code"))  # 通道注册名（展示名 DISPLAY_NAME 归 constants）
ENV_AUTH_DIR = str(_const("ENV_AUTH_DIR", "CB_MINIMAX_CODE_AUTH_DIR"))  # 本模块的目录覆盖键（非协议事实）

DATA_DIR_NAME = str(_const("DATA_DIR_NAME", ".minimax"))  # spec:273
LEGACY_DATA_DIR_NAME = str(_const("LEGACY_DATA_DIR_NAME", ".mavis"))  # spec:273（legacy 基名）
AUTH_SUBDIR = str(_const("AUTH_SUBDIR", "auth"))  # spec:265
CREDENTIALS_FILENAME = str(_const("CREDENTIALS_FILENAME", "auth.json"))  # spec:269
AUTH_STATE_FILENAME = str(_const("AUTH_STATE_FILENAME", "auth-state.json"))  # spec:270

BUILD_ENV = str(_const("BUILD_ENV", "prod"))  # spec:673,676
REGION = str(_const("REGION", "cn"))  # spec:673,676
OAUTH_CLIENT_ID = str(_const("OAUTH_CLIENT_ID", "mcode-public"))  # spec:235
OAUTH_AUDIENCE = str(_const("OAUTH_AUDIENCE", "agent-backend"))  # spec:237
# 主 scope：契约层给的是元组（spec:236），但也可能是空格分隔串 ⇒ 两种都吃，
# 取第一个非空项。缺 agent.default 的凭证客户端会拒（auth-core.js:615-624）。
_OAUTH_SCOPES_RAW = _const("OAUTH_SCOPES", ("agent.default",))
OAUTH_PRIMARY_SCOPE = str(
    _OAUTH_SCOPES_RAW[0] if isinstance(_OAUTH_SCOPES_RAW, (list, tuple)) and _OAUTH_SCOPES_RAW
    else str(_OAUTH_SCOPES_RAW).split()[0] if str(_OAUTH_SCOPES_RAW).split() else "agent.default"
)
OAUTH_TOKEN_TYPE = "Bearer"  # spec:291（types.js:36 强校验）

CREDENTIAL_SERVICE = str(_const("CREDENTIAL_SERVICE", f"com.minimax.mcode.oauth.{BUILD_ENV}.{REGION}"))  # spec:267
KEY_DELIMITER = str(_const("CREDENTIAL_RECORD_KEY_DELIMITER", "\0"))  # spec:289（file-store.js:6-7）
CREDENTIAL_SCHEMA_VERSION = int(_const("CREDENTIAL_SCHEMA_VERSION", 1))  # spec:290
CREDENTIAL_MIN_SCHEMA_VERSION = int(_const("CREDENTIAL_MIN_SUPPORTED_SCHEMA_VERSION", 1))  # spec:290（types.js:3）

ACCOUNT_TYPE = "personal"  # 本地 OAuth 登录态账号（DB accounts.account_type，默认即 personal）

# --- prod/cn 硬边界的显式表述（spec §8:669,675,682 + spec:82-90,242） ---
ALLOWED_BUILD_ENV = BUILD_ENV  # "prod"
ALLOWED_REGION = REGION  # "cn"
ALLOWED_CLIENT_ID = OAUTH_CLIENT_ID  # "mcode-public"
# 下列目录名**永不**进候选（白名单写法天然排除它们；列出只为把边界写成可审的事实）：
EXCLUDED_BUILD_ENVS = ("staging", "test", "dev", "internal", "inside")  # spec:82-90,675,682
EXCLUDED_REGIONS = ("en",)  # spec:89,132,242

# discover 预览要提示给用户的话（spec:694）
SHARED_CREDENTIAL_WARNING = (
    "该凭证与本机 MiniMax Code 桌面客户端共用同一份 auth.json（spec:694）："
    "客户端再次登录或刷新会换代 generation 并可能轮转 refresh_token，从而使本通道凭证失效。"
    "本通道只读导入、绝不回写客户端文件；通道刷新出的新 token 只存本网关数据库。"
)


# ============================================================
# 目录发现
# ============================================================

def _user_home() -> Path:
    """Windows 用 %USERPROFILE%（spec:275 的 `~`）；非 Windows 退回 Path.home()。"""
    return Path(os.environ.get("USERPROFILE") or str(Path.home()))


def _auth_subpath_parts() -> tuple[str, ...]:
    """期望凭证目录相对 home 的层级。

    优先采用契约层的 `AUTH_SUBPATH`（spec:13,694 = `.minimax/auth/prod/cn/mcode-public`）：
      * 首段是点目录（`.minimax` 这种数据目录基名）⇒ 原样用；
      * 否则视为**相对数据目录**的写法（`auth/prod/cn/mcode-public`）⇒ 自动补 DATA_DIR_NAME，
        避免拼出 `~\\auth\\prod\\cn\\...` 这种指不到客户端的路径。
    契约层没有 AUTH_SUBPATH 时，按 spec:265-275 逐段拼 `<dataDir>/auth/<env>/<region>/<clientId>`。
    """
    explicit = str(_const("AUTH_SUBPATH", "")).replace("\\", "/").strip("/")
    parts = tuple(segment for segment in explicit.split("/") if segment)
    if len(parts) >= 4:
        return parts if parts[0].startswith(".") else (DATA_DIR_NAME,) + parts
    return (DATA_DIR_NAME, AUTH_SUBDIR, BUILD_ENV, REGION, OAUTH_CLIENT_ID)


def _canonical_namespace_dir() -> Path:
    """本机 prod/cn 凭证目录的期望路径（spec:275：`C:\\Users\\<u>\\.minimax\\auth\\prod\\cn\\mcode-public`）。"""
    return _user_home().joinpath(*_auth_subpath_parts())


def _candidate_auth_homes() -> list[Path]:
    """候选 `<dataDir>/auth` 根：当前 `.minimax` + legacy `.mavis`（spec:265,273）。"""
    home = _user_home()
    return [home / base / AUTH_SUBDIR for base in (DATA_DIR_NAME, LEGACY_DATA_DIR_NAME)]


def _iter_dirs(folder: Path) -> list[Path]:
    try:
        return sorted(item for item in folder.iterdir() if item.is_dir())
    except OSError:
        # 权限/枚举失败只当作"没有子目录"，discover 会显示 exists/file_count
        logger.debug("minimax-code: 无法枚举目录 %s", folder)
        return []


def _layer_match(name: str, expected: str) -> bool:
    """命名空间段比较（Windows/NTFS 大小写不敏感 ⇒ casefold）。"""
    return (name or "").casefold() == str(expected).casefold()


def _tail_namespace(path: Path) -> tuple[str, str, str]:
    """把目录末尾三段按 `<buildEnv>/<region>/<clientId>` 形状读出来（spec:266）。

    挂载/改名场景（Docker 把凭证目录挂成 `/auth`）末尾凑不出三层，空位补 ""。
    返回值只用于判断"这里明摆着写着不是 prod/cn"，**不用于放行**。
    """
    tail = [part.casefold() for part in path.parts[-3:]]
    while len(tail) < 3:
        tail.insert(0, "")
    return tail[0], tail[1], tail[2]


def _is_foreign_namespace(path: Path) -> bool:
    """路径末段是否明摆着是别的 env/region（spec §8:669 的多实例硬边界）。"""
    build, region, _client = _tail_namespace(path)
    return build in EXCLUDED_BUILD_ENVS or region in EXCLUDED_REGIONS


def _namespace_dirs(auth_home: Path, *, explicit_root: bool = False) -> list[Path]:
    """在 `<auth_home>/<buildEnv>/<region>/<clientId>` 里只挑 prod/cn/mcode-public。

    逐层白名单（spec:265-266 的 namespaceHome 形状）：
      第 1 层必须是 `prod`          ⇒ 排除 staging/test/dev/internal/inside（spec:82-90,675,682）
      第 2 层必须是 `cn`            ⇒ 排除 en（spec:89,242）
      第 3 层必须是 `mcode-public`  ⇒ 排除别的客户端 id
    auth.json 恒在 clientId 目录下（namespace.js:16 `credentialPath=join(namespaceHome,'auth.json')`），
    所以这里不接受更浅的层级。
    """
    out: list[Path] = []
    if not auth_home.is_dir():
        return out
    if explicit_root and (auth_home / CREDENTIALS_FILENAME).is_file():
        # 显式 env 覆盖时接受"目录本身就是凭证目录"：容器里 `.../prod/cn/mcode-public`
        # 常被挂成 `/auth`，路径层级会丢。但**覆盖不许突破硬边界** ⇒ 末段明摆着是
        # staging/en 之类时照样跳过；只有层级凑不出命名空间名字的（真挂载点）才放行，
        # 交给第二道闸门（记录键前缀，见 _parse_auth_json）把关。
        if _is_foreign_namespace(auth_home):
            logger.warning(
                "minimax-code: 覆盖目录 %s 指向非 prod/cn 命名空间，按多实例硬边界跳过（spec:8）",
                auth_home,
            )
            return out
        out.append(auth_home)
    for build_dir in _iter_dirs(auth_home):
        if not _layer_match(build_dir.name, ALLOWED_BUILD_ENV):
            if build_dir.name.casefold() in EXCLUDED_BUILD_ENVS:
                logger.debug("minimax-code: 跳过非 prod 命名空间目录 %s", build_dir)
            continue
        for region_dir in _iter_dirs(build_dir):
            if not _layer_match(region_dir.name, ALLOWED_REGION):
                if region_dir.name.casefold() in EXCLUDED_REGIONS:
                    logger.debug("minimax-code: 跳过非 cn 命名空间目录 %s", region_dir)
                continue
            for client_dir in _iter_dirs(region_dir):
                if _layer_match(client_dir.name, ALLOWED_CLIENT_ID):
                    out.append(client_dir)
    return out


def minimax_auth_dirs() -> list[Path]:
    """MiniMax Code 凭证目录候选（**只含 prod/cn 命名空间**）。

    默认根 = `%USERPROFILE%\\.minimax\\auth`（spec:275,694），env
    `CB_MINIMAX_CODE_AUTH_DIR` 覆盖（覆盖值是运维显式指定的 auth 根或凭证目录）。

    硬边界两道，缺一不可（多实例互不干扰，spec:8）：
      1. 目录层：逐段白名单 `prod` / `cn` / `mcode-public`（见 `_namespace_dirs`），
         `en`/`staging`/`test`/`internal`/`inside` 目录永不入选。**env 覆盖也照样受这一层
         管**：覆盖指向末段明写着 staging/en 的凭证目录时直接跳过（只记 warning 日志）；
         只有路径层级被挂载/改名抹平（容器里 `/auth` 这种）才按"无命名空间信息"放行，
         交给第 2 道兜底。
      2. 记录层：auth.json 记录键必须落在 `com.minimax.mcode.oauth.prod.cn\\0`
         前缀内（spec:267,289）——即便运维把别的命名空间目录改名/拷进来，
         第 2 道也会拒掉。
    未安装/未登录时期望路径仍然返回，让 discover 显示"不存在"成为可诊断信息
    （比静默空列表更好排查）；结果统一过 `dedupe_dirs`。
    """
    override = os.environ.get(ENV_AUTH_DIR, "").strip()
    dirs: list[Path] = []
    if override:
        root = Path(override).expanduser()
        dirs.extend(_namespace_dirs(root, explicit_root=True))
    else:
        dirs.append(_canonical_namespace_dir())
        for home in _candidate_auth_homes():
            dirs.extend(_namespace_dirs(home))
    return dedupe_dirs(dirs)


# ============================================================
# auth.json 解析
# ============================================================

def _brief(value) -> str:
    """把任意诊断值压成短字符串：**只用字段/枚举名，绝不用于 token**。"""
    return str(value)[:32]


def _as_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_int(value) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _scope_list(value) -> list[str]:
    """scopes 支持数组与空格分隔串两种写法（spec:253 发送时 join(' ')）。"""
    if isinstance(value, list):
        return [_as_str(item) for item in value if _as_str(item)]
    text = _as_str(value)
    return [item for item in text.split() if item]


def _jwt_claims(token: str) -> dict:
    """JWT payload 字典（spec:258：客户端读 `sub / account_id / scope|scp / exp`）。

    解不出返回 {}（与 oauth-client.js:209-219 的 decodeJwtPayload 同语义：只取
    payload，不验签、不联网）。返回值可能含身份信息，**只用于本函数调用点**。
    """
    try:
        segments = (token or "").split(".")
        if len(segments) < 2 or not segments[1]:
            return {}
        payload = segments[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _looks_like_jwt(text: str) -> bool:
    """三段 base64url 且中间段能解成 JSON 对象 ⇒ 判定为裸 JWT（同 oauth-client.js:211）。"""
    value = (text or "").strip()
    if not value or any(char in value for char in "{} \t\r\n"):
        return False
    if len(value.split(".")) != 3:
        return False
    return bool(_jwt_claims(value))


def _llm_domain() -> str:
    """DB `domain` 列（纯元数据：展示 + 部分通道的 Origin 选择），不带 scheme。"""
    url = str(_const("LLM_HOST", "") or _const("AGENT_HOST_CN", "https://agent.minimax.cn"))
    return url.split("://", 1)[-1].split("/", 1)[0]


def _account_dict(
    *,
    uid: str,
    access: str,
    refresh: str,
    expires_at: int,
    source: str,
    auth_path: str = "",
    scope: str = "",
    audience: str = "",
    client_id: str = "",
    token_type: str = OAUTH_TOKEN_TYPE,
    generation: int = 0,
    login_epoch: str = "",
    extra: dict | None = None,
) -> dict:
    """账号结构的唯一出口：字段名与 `storage.database.add_account` 对齐。"""
    label = f"{CHANNEL_ID}-{(uid or 'user')[:8]}"
    account_extra = {
        # 契约要求的核心字段（spec:281-294；值全为身份/元数据，不含 token）
        "login_epoch": login_epoch,  # spec:294 同一次登录内不变；变化 ⇒ 客户端重新登录过
        "generation": generation,  # spec:294,314 每次刷新 +1
        "scope": scope,  # spec:236,292
        "audience": audience or OAUTH_AUDIENCE,  # spec:237,292
        "build_env": BUILD_ENV,  # spec:673 硬边界：恒 prod
        "region": REGION,  # spec:673 硬边界：恒 cn
        "client_id": client_id or OAUTH_CLIENT_ID,  # spec:235,292
        "auth_path": auth_path,  # 来源文件（粘贴入口为空）
        # 本通道自有标记
        "token_type": token_type or OAUTH_TOKEN_TYPE,  # spec:291
        "source": source or "import",
        # spec:694：只有从桌面客户端目录读出来的凭证才与客户端**共用**，
        # 刷新后不得回写（通道刷新逻辑读这个键判定）。
        "shared_credential": bool(auth_path),
    }
    if extra:
        account_extra.update(extra)
    account = {
        "provider": CHANNEL_ID,
        "name": label,
        "nickname": label,  # auth.json 里没有昵称字段（spec:291-294），用派生标签占位，
        # 否则 discover 预览只显示文件名。展示名要真实值需调账号 API（spec:380），本期不做。
        "uid": uid,
        "account_type": ACCOUNT_TYPE,
        "access_token": access,
        "refresh_token": refresh,
        # 仓库约定：expires_at **一律毫秒**。spec:293 的 expiresAtMs 就是
        # `now + expires_in*1000`（auth-core.js:599），本机观测距签发约 11 天（spec:317）。
        "expires_at": int(expires_at or 0),
        # 故意**不写** refresh_expires_at：spec 未记录 refresh token 有效期
        # （§10:704 未确认），写了会经 store_common.upsert_account 把刷新逻辑
        # 拿到的值凭空清成 0。
        "domain": _llm_domain(),
        "status": "active",
        "extra": account_extra,
    }
    if not int(expires_at or 0):
        # 解析不出到期时间时**不发** expires_at 键：store_common 只在 parsed 带该键
        # 时才写列（store_common.py:228-231 的 qclaw 先例），凭空写 0 会把刷新逻辑
        # 算好的到期时间清掉。正常路径走不到这里——客户端强校验 expiresAtMs 必须是
        # 数字（types.js:40），只有畸形/半份粘贴数据才会缺。
        account.pop("expires_at", None)
    return account


def _record_to_account(record: dict, *, source: str = "") -> tuple[dict | None, str]:
    """单条凭证记录 → 账号；不可用返回 (None, 可诊断 reason)。

    校验项对齐客户端 `parseStoredCredential`（spec:291-294 / types.js:21-50），
    差别是本模块**不因缺 refresh_token 就整条拒**（裸 access token 仍可用，
    只是到期无法自刷新，spec:704）。reason 只含字段名与枚举值。
    """
    if not isinstance(record, dict) or not record:
        return None, "记录不是 JSON 对象"

    version = record.get("schemaVersion")
    if version is not None:
        number = _as_int(version)
        if number < CREDENTIAL_MIN_SCHEMA_VERSION or number > CREDENTIAL_SCHEMA_VERSION:
            return None, (
                f"记录 schemaVersion={_brief(version)} 不认识"
                f"（本模块支持 {CREDENTIAL_MIN_SCHEMA_VERSION}-{CREDENTIAL_SCHEMA_VERSION}，spec:290）"
            )

    access = _as_str(record.get("accessToken") or record.get("access_token") or record.get("token"))
    refresh = _as_str(record.get("refreshToken") or record.get("refresh_token"))
    if not access and not refresh:
        return None, "记录里没有 accessToken/refreshToken"

    token_type = _as_str(record.get("tokenType") or record.get("token_type"))
    if token_type and token_type != OAUTH_TOKEN_TYPE:
        return None, f"tokenType={_brief(token_type)} 非 {OAUTH_TOKEN_TYPE}（spec:291）"

    client_id = _as_str(record.get("clientId") or record.get("client_id"))
    if client_id and client_id != OAUTH_CLIENT_ID:
        return None, f"clientId={_brief(client_id)} 非 {OAUTH_CLIENT_ID}（spec:235）"

    audience = _as_str(record.get("audience"))
    if audience and audience != OAUTH_AUDIENCE:
        return None, f"audience={_brief(audience)} 非 {OAUTH_AUDIENCE}（spec:237）"

    scopes = _scope_list(record.get("scopes") if record.get("scopes") is not None else record.get("scope"))
    if scopes and OAUTH_PRIMARY_SCOPE not in scopes:
        # 客户端同样拒（auth-core.js:615-624）：scope 不含 agent.default 的凭证打不通 LLM 网关
        return None, f"scopes={_brief(scopes)} 不含 {OAUTH_PRIMARY_SCOPE}（spec:236）"

    expires_at = iso_to_ms(
        record.get("expiresAtMs") or record.get("expires_at") or record.get("expiresAt")
    )
    if not expires_at:
        # token 响应形状带 expires_in（秒，spec:258）：按客户端算法换算成 ms（auth-core.js:599）
        expires_in = _as_int(record.get("expires_in") or record.get("expiresInSec"))
        if expires_in > 0:
            expires_at = int(time.time() * 1000) + expires_in * 1000
    if not expires_at:
        expires_at = jwt_exp_ms(access)  # 兜底：JWT exp（仓库惯例，traework/qwenwork 同做法）

    claims = _jwt_claims(access)
    uid = (
        _as_str(record.get("subject"))
        or _as_str(record.get("accountId"))
        or _as_str(claims.get("sub"))
        or _as_str(claims.get("account_id"))
    )
    # subject/accountId 是可选键（spec:293，types.js:44-45），来自 JWT 的 sub / account_id
    # claim（oauth-client.js:205-206）；都没有时 uid 为空 ⇒ upsert 无法按 uid 去重，
    # 只会新增行，管理页需人工合并。
    #
    # ⚠️ 实测（2026-09-30 本机真实 auth.json）：这两类键**一次都没出现**——
    # 真实记录只有 accessToken/refreshToken/clientId/expiresAtMs/generation/
    # loginEpoch/audience/scopes/tokenType/schemaVersion，且 `accessToken` 是
    # **60 字符不透明串、只有 1 段、不是 JWT**（base64 解不出 header）⇒ 上面的
    # JWT 兜底恒为空，uid 永远拿不到。后果不是"标签丑"，而是**每次导入都新增一行**
    # （store_common.upsert_account 只在 uid 非空时做匹配，实测连导 2 次得 2 行）。
    # ⇒ 回退到 `loginEpoch`：spec:294 明确"同一次登录内不变"，刷新只轮 generation
    # 不动它（实测 generation=7 而 loginEpoch 稳定），是本机唯一稳定可用的账号标识。
    if not uid:
        login_epoch = _as_str(record.get("loginEpoch") or record.get("login_epoch"))
        if login_epoch:
            uid = f"loginEpoch:{login_epoch}"
    scope_text = " ".join(scopes) if scopes else " ".join(_scope_list(claims.get("scope") or claims.get("scp")))
    return (
        _account_dict(
            uid=uid,
            access=access,
            refresh=refresh,
            expires_at=expires_at,
            source=source,
            scope=scope_text,
            audience=audience,
            client_id=client_id,
            token_type=token_type,
            generation=_as_int(record.get("generation")),
            login_epoch=_as_str(record.get("loginEpoch") or record.get("login_epoch")),
        ),
        "",
    )


def _record_service(key: str) -> str:
    """记录键里的 service 段（`<service>\\0<account>`，spec:289 / file-store.js:6-8）。"""
    return (key or "").split(KEY_DELIMITER, 1)[0] if KEY_DELIMITER in (key or "") else ""


def _parse_auth_json(document, *, source: str = "") -> tuple[dict | None, str]:
    """auth.json 文档 → 账号，附带可诊断 reason（不抛裸异常）。

    硬边界第二道（spec:267,289）：键前缀必须是 `com.minimax.mcode.oauth.prod.cn\\0`，
    于是 en/staging/test 的记录即使被拷进本目录也会被拒。键里的 `<account>` 段是
    `sha256(authHome\\0clientId)` 的 base64url（spec:268，namespace.js:8-10）——同
    一命名空间恒定 ⇒ 正常只有一条记录；出现多条按最晚到期 + 最高 generation 取。
    """
    if not isinstance(document, dict):
        return None, "auth.json 顶层不是 JSON 对象"

    file_version = document.get("schemaVersion")
    if file_version is None:
        return None, "auth.json 缺少 schemaVersion（不是 MiniMax Code 的 OAuth 文件库，spec:290）"
    if _as_int(file_version) != CREDENTIAL_SCHEMA_VERSION:
        return None, (
            f"文件级 schemaVersion={_brief(file_version)} 不认识"
            f"（本模块只支持 {CREDENTIAL_SCHEMA_VERSION}，spec:290 / file-store.js:14）"
        )

    records = document.get("records")
    if not isinstance(records, dict):
        return None, "auth.json 的 records 不是对象（spec:290）"
    if not records:
        return None, "auth.json 的 records 为空（该命名空间下客户端未登录或已登出）"

    candidates: list[tuple[int, int, dict]] = []
    rejections: list[str] = []
    for raw_key, record in records.items():
        key = str(raw_key)
        service = _record_service(key)
        if service and service != CREDENTIAL_SERVICE:
            # 只报 service 段（不含 account 哈希）：这是命名空间信息，不是凭证
            rejections.append(f"记录键命名空间 {_brief(service)} 非 {CREDENTIAL_SERVICE}")
            continue
        parsed, reason = _record_to_account(record if isinstance(record, dict) else {}, source=source)
        if parsed is None:
            rejections.append(reason or "记录不可用")
            continue
        extra = parsed["extra"]
        candidates.append(
            (int(parsed.get("expires_at") or 0), int(extra.get("generation") or 0), parsed)
        )

    if not candidates:
        return None, "; ".join(rejections)[:200] or "records 里没有 prod/cn 可用记录"

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    account = candidates[0][2]
    if len(candidates) > 1:
        # 理论上同命名空间只会有一条；多条 = 手工合并/异常，留计数便于诊断，其余不外泄
        account["extra"]["record_count"] = len(candidates)
    return account, ""


def auth_json_to_account(document, source: str = "") -> dict | None:
    """把整份 auth.json 文档转成账号 dict；不认识的结构返回 None。

    reason 走 `_parse_auth_json()`（discover / import 用它把 reason 显示给用户），
    本函数按契约只返回 dict | None，调用方拿不到 reason 时按"解析失败"处理。
    """
    parsed, _reason = _parse_auth_json(document, source=source)
    return parsed


# ============================================================
# 只读导入
# ============================================================

def _read_document(path: Path) -> dict:
    """读 auth.json（只读，见模块 docstring）。错误串只含定位信息，不含文件内容。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读取 auth.json 失败：{exc.strerror or exc.__class__.__name__}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 只用 exc.msg/lineno：args/doc 里可能带原文（含凭证）
        raise ValueError(f"auth.json 不是合法 JSON（{exc.msg}，line {exc.lineno}）") from exc
    if not isinstance(document, dict):
        raise ValueError("auth.json 顶层不是 JSON 对象")
    return document


_AUTH_STATE_STATUSES = ("authenticated", "anonymous", "expired", "error", "logout_pending")  # spec:283 / auth-core.js


def _auth_state_status(namespace_dir: Path) -> str:
    """同目录 auth-state.json 的 status，仅作诊断（spec:281-286，不含凭证）。

    客户端登出/换代后 status 会变；把它带进 extra，通道 401 时能区分
    "我们的库过期了" 与 "用户在本机客户端登出了"。解析失败返回 ""。
    """
    state_path = namespace_dir / AUTH_STATE_FILENAME
    if not state_path.is_file():
        return ""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    status = _as_str(state.get("status")) if isinstance(state, dict) else ""
    return status if status in _AUTH_STATE_STATUSES else ""


def import_discovered(path: str) -> dict:
    """导入 discover 列出的 auth.json（只读）。失败抛 ValueError(reason)。"""
    target = Path(str(path or "")).expanduser()
    allowed = [folder.resolve() for folder in minimax_auth_dirs() if folder.is_dir()]
    resolved = target.resolve()
    if allowed and not any(is_relative_to(resolved, root) for root in allowed):
        raise ValueError(f"路径不在 {ENV_AUTH_DIR} / ~\\{DATA_DIR_NAME}\\auth\\{BUILD_ENV}\\{REGION} 之内")
    if resolved.name != CREDENTIALS_FILENAME:
        raise ValueError(f"只支持导入 {CREDENTIALS_FILENAME}（spec:269）")

    parsed, reason = _parse_auth_json(_read_document(resolved), source=str(resolved))
    if parsed is None:
        raise ValueError(reason or "auth.json 里没有 prod/cn 可用凭证记录")
    extra = parsed["extra"]
    extra["auth_path"] = str(resolved)
    extra["source"] = str(resolved)
    extra["shared_credential"] = True  # spec:694 共用凭证 ⇒ 通道刷新结果永不回写此文件
    status = _auth_state_status(resolved.parent)
    if status:
        extra["auth_state_status"] = status
    return parsed


def _collect_files(folder: Path) -> list[Path]:
    """命名空间目录里的凭证文件：只有 `auth.json`（spec:269）。

    `auth-state.json` 只有状态/generation/expiresAtMs、`auth.lock` 是跨进程锁，
    两者都不含 token（spec:270,279,281-286）⇒ 不进候选。
    """
    candidate = folder / CREDENTIALS_FILENAME
    return [candidate] if candidate.is_file() else []


def _file_meta(path: Path, existing: set[str]) -> dict:
    # token_fields 与契约一致：access_token / refresh_token 有其一即算有效凭证
    meta = imported_file_meta(
        CHANNEL_ID, path, existing, import_discovered,
        token_fields=("access_token", "refresh_token"),
    )
    meta["read_only"] = True  # 本通道不回写客户端文件（spec:694）
    meta["warning"] = SHARED_CREDENTIAL_WARNING
    return meta


def discover() -> dict:
    """扫描 prod/cn 命名空间下的 auth.json，产出管理页预览（只读）。"""
    payload = discover_dirs(CHANNEL_ID, minimax_auth_dirs(), _collect_files, _file_meta)
    payload["read_only"] = True  # spec:694：本通道凭证来源是只读快照
    payload["note"] = SHARED_CREDENTIAL_WARNING
    return payload


# ============================================================
# 粘贴入口
# ============================================================

def _account_from_bare_jwt(token: str) -> dict:
    """裸 JWT：只有 access_token，没有 refresh_token。

    expires_at 由 `exp` 推（jwt_exp_ms，仓库惯例），uid 由 `sub`/`account_id` 推
    （spec:258）。到期后无法自刷新，只能重新导入——把这点写进 extra 供 UI/刷新
    逻辑判断；refresh token 有效期 spec 未记录（§10:704），不猜。
    """
    claims = _jwt_claims(token)
    scope = _scope_list(claims.get("scope") or claims.get("scp"))
    return _account_dict(
        uid=_as_str(claims.get("sub")) or _as_str(claims.get("account_id")),
        access=token,
        refresh="",
        expires_at=jwt_exp_ms(token),
        source="paste",
        scope=" ".join(scope),
        audience=_as_str(claims.get("aud")) or OAUTH_AUDIENCE,
        login_epoch="",
        extra={"import_shape": "bare_jwt", "can_refresh": False},
    )


def parse_credentials(text) -> dict:
    """粘贴入口：整段 auth.json / 单条凭证记录 / token 响应 / 裸 JWT 都收。

    返回结构与 auth_json_to_account 一致；失败抛 ValueError，管理页会转成 400
    （`gateway/routers/admin/_accounts.py`）。异常串只含字段名/枚举/JSON 定位，
    **绝不含 token 原文**。
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    if isinstance(text, dict):
        document = text
    elif isinstance(text, str):
        raw = text.strip()
        if not raw:
            raise ValueError("凭证为空")
        if not raw.startswith("{") and _looks_like_jwt(raw):
            return _account_from_bare_jwt(raw)
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"凭证既不是裸 JWT，也不是合法 JSON（{exc.msg}）") from exc
    else:
        raise ValueError("凭证必须是字符串或 JSON 对象")

    if not isinstance(document, dict):
        raise ValueError("凭证必须是 JSON 对象")
    if isinstance(document.get("records"), dict):
        parsed, reason = _parse_auth_json(document, source="paste")
    else:
        parsed, reason = _record_to_account(document, source="paste")
    if parsed is None:
        raise ValueError(reason or "凭证解析失败")
    return parsed


# ============================================================
# 入库
# ============================================================

def upsert_account(parsed: dict) -> dict:
    """按 uid 去重入库；不存在则新增。

    `merge_extra=True`：更新时 extra 按**键**合并旧值（store_common.py:213-216），
    但它是"新值优先"——显式的空串会把旧值抹掉。本通道的 extra 里 `auth_path` /
    `login_epoch` 是**来源信息**：管理页粘贴凭证（parse_credentials 的产物，
    auth_path=""）不该把之前从客户端目录读到的 provenance 清掉，所以下面先剔除
    空串键，让 merge_extra 保持旧值。数值 0（如 generation）不剔除——那是真实值。

    `extra_fields=("account_type",)`：更新时也透传该列。
    共享 store_common 的"空 token 永不覆盖好数据"语义（store_common.py:219-227）
    ——这对本通道尤其重要：只读快照可能读出半份凭证，不能把好数据擦掉。
    """
    payload = dict(parsed)
    extra = payload.get("extra")
    if isinstance(extra, dict):
        payload["extra"] = {
            key: value for key, value in extra.items()
            if not (isinstance(value, str) and value == "")
        }
    return upsert_account_by_uid(
        CHANNEL_ID, payload, extra_fields=("account_type",), merge_extra=True
    )
