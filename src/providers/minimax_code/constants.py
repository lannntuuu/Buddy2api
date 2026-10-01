"""MiniMax Code (受管登录态) protocol constants.

所有值来自本机静态逆向规格（纯 asar 解包 + 源码阅读，零网络）：
`.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md`
每条常量后 `# spec:NNN` = 该规格文件的行号，便于后续逐条审计。
spec 未覆盖的点一律写成"可配置 + TODO 注释"，不编造端点或 header。

一句话协议形状（spec:12,21,154-176）：上游是 **Anthropic Messages 方言**，
不是 OpenAI Chat Completions；请求/响应体是**明文 JSON**，不存在任何编码、
加密、签名层，也没有 `Encode=1` 之类的开关（spec:417-487）—— 所以本文件里
没有签名常量，加了只会失败得更难查（spec:695）。

最容易踩的坑 —— `/v1` 的数量（spec:94-126,689）：
  客户端预置 base = `https://agent.minimax.cn/mavis/api/v1/llm/v1`   ← 末尾带 /v1
  `normalizeProviderBaseUrl()` 对 anthropic-messages **先剥掉**末尾 `/v1`
      → `https://agent.minimax.cn/mavis/api/v1/llm`                    spec:100-111
  再由 `@anthropic-ai/sdk` 拼 `/v1/messages`                            spec:113-125
  净效果：**只保留一个 `/v1`**
      → `https://agent.minimax.cn/mavis/api/v1/llm/v1/messages`         spec:131
故本文件用 `LLM_BASE_PATH`（剥过 /v1 的 base）+ `ANTHROPIC_MESSAGES_PATH`
两条常量拼出最终路径，并在文件末尾 assert 其形状（见 _self_check）。

凭证（spec:184,344,694）：真实凭证只在 `Authorization: Bearer <JWT access_token>`；
`x-api-key` 恒为占位符 `sk-xxx` 且**必须原样发送**（spec:200-202）。
access_token / refresh_token 原文绝不进日志、异常、断言字面量。

Domain（prod/cn，profile 判定见 §profile 段与 spec:667-682）： agent.minimax.cn
账号 OAuth 面：                                              account.minimax.cn
"""

from __future__ import annotations

CHANNEL_ID = "minimax_code"  # 通道 id（本通道在网关内的注册名）
DISPLAY_NAME = "MiniMax Code"  # 展示名（admin UI / /v1/models 通道列表用）

# --- profile：本机安装属于哪个环境（spec §8，决定性证据是 asar 内的 .env.local） ---
BUILD_ENV = "prod"  # spec:673,676  NEXT_PUBLIC_BUILD_ENV="prod"（非 staging/test/internal/inside）
LOCALE = "zh"  # spec:673  NEXT_PUBLIC_LOCALE="zh"
REGION = "cn"  # spec:673,676  落盘目录 .../auth/prod/cn/...；MAVIS_REGION 由 locale 推 cn（spec:679）
LOGIN_ENV_CHOICES = ("prod", "staging", "test", "dev")  # spec:82-90 预置表里的四档 env
# internal/inside 两个构建标志在本实例未置位（spec:675）；internal 的后端环境其实指向
# staging，inside 等同 prod 但有独立更新通道与 userData 目录（spec:682）⇒ 都不适用。
BUILD_INTERNAL_FLAG = "__MAVIS_BUILD_INTERNAL"  # spec:675  仅审计用：本实例不存在该键
BUILD_INSIDE_FLAG = "__MAVIS_BUILD_INSIDE"  # spec:675  同上

# --- 网关 host（按 env × region 的预置 base URL 表，spec:79-92） ---
AGENT_HOST_CN = "https://agent.minimax.cn"  # spec:88,131  cn-prod 唯一 LLM 出口
AGENT_HOST_EN = "https://agent.minimax.io"  # spec:89,132  en-prod
AGENT_HOST_LEGACY = "https://agent.minimaxi.com"  # spec:91,133  旧域名，仍被识别为受管
# 客户端预置表原样（注意：末尾**带** `/v1`，会被 normalizeProviderBaseUrl 剥掉，spec:100-111）。
# 通道实际只用 cn-prod 一行；其余行存在是为了「切环境不改代码」，不要拿去当已验证事实。
PRESET_BASE_URLS: dict[str, str] = {  # spec:81-90
    "cn-test": "https://matrix-test.xaminim.com/mavis/api/v1/llm/v1",  # spec:82
    "cn-dev": "https://matrix-test.xaminim.com/mavis/api/v1/llm/v1",  # spec:83
    "cn-staging": "https://matrix-pre.xaminim.com/mavis/api/v1/llm/v1",  # spec:84
    "en-test": "https://matrix-overseas-test.xaminim.com/mavis/api/v1/llm/v1",  # spec:85
    "en-dev": "https://matrix-overseas-test.xaminim.com/mavis/api/v1/llm/v1",  # spec:86
    "en-staging": "https://matrix-overseas-pre.xaminim.com/mavis/api/v1/llm/v1",  # spec:87
    "cn-prod": AGENT_HOST_CN + "/mavis/api/v1/llm/v1",  # spec:88  ← 本通道默认
    "en-prod": AGENT_HOST_EN + "/mavis/api/v1/llm/v1",  # spec:89
}
LEGACY_MANAGED_BASE_URL = "https://agent.minimaxi.com/mavis/api/v1/llm/v1"  # spec:91

# 账号 OAuth host（spec:241-242）：受管登录走这里，与 LLM 网关是两台机器。
ACCOUNT_HOST_CN_PROD = "https://account.minimax.cn"  # spec:241,673
ACCOUNT_HOST_CN_STAGING = "https://account-pre.xaminim.com"  # spec:241
ACCOUNT_HOST_CN_TEST = "https://account-test.xaminim.com"  # spec:241
ACCOUNT_HOST_EN_PROD = "https://account.minimax.io"  # spec:242
DEFAULT_ACCOUNT_HOST = ACCOUNT_HOST_CN_PROD  # spec:673 NEXT_PUBLIC_LOGIN_API_BASE

# --- 路径拼接（/v1 陷阱的唯一权威写法，spec:94-131） ---
ANTHROPIC_API = "anthropic-messages"  # spec:70 MINIMAX_API_FORMAT；决定走下面这套拼法
LLM_BASE_PATH = "/mavis/api/v1/llm"  # spec:111 已剥掉预置末尾 /v1 的 base path
ANTHROPIC_MESSAGES_PATH = "/v1/messages"  # spec:162,124 SDK 侧追加的 path
CHAT_PATH = LLM_BASE_PATH + ANTHROPIC_MESSAGES_PATH  # spec:131 → /mavis/api/v1/llm/v1/messages
CHAT_METHOD = "POST"  # spec:131
CHAT_URL_CN = AGENT_HOST_CN + CHAT_PATH  # spec:131 主路径完整 URL
CHAT_URL_EN = AGENT_HOST_EN + CHAT_PATH  # spec:132
CHAT_URL_LEGACY = AGENT_HOST_LEGACY + CHAT_PATH  # spec:133
# 预置写法（含末尾 /v1）留一份，只为审计「客户端为什么会发对 URL」：
PRESET_CHAT_BASE_PATH = LLM_BASE_PATH + "/v1"  # spec:82-89 预置 base 的 path 部分（多一个 /v1）

# --- BYOK（自带 MiniMax API key）：与受管是**不同**入口，本通道默认不走这条 ---
# 出处 spec:134,137-146：预置 base = `https://api.minimaxi.com/messages`，同样被
# normalizeProviderBaseUrl 剥 `/messages` 再拼 `/v1/messages`，净效果 `.../v1/messages`。
BYOK_BASE_URL_CN = "https://api.minimaxi.com/messages"  # spec:141（客户端预置形状）
BYOK_BASE_URL_EN = "https://api.minimax.io/messages"  # spec:142
BYOK_CHAT_PATH = "/v1/messages"  # spec:134 剥/拼后的净路径
BYOK_CHAT_URL_CN = "https://api.minimaxi.com/v1/messages"  # spec:134
BYOK_CHAT_URL_EN = "https://api.minimax.io/v1/messages"  # spec:134
# TODO(spec:706)：受管与 BYOK 是否同路径同权（能否用 x-api-key 传真 token）静态无法确认，
# 本通道一律 `Authorization: Bearer` + `x-api-key: sk-xxx`，不要试图合并两条入口。

# --- 多模态 File API（超阈值附件先上传再引用，spec:135,147-152,561） ---
FILE_API_UPLOAD_PATH = "/v1/files/upload"  # spec:150,553
FILES_UPLOAD_PATH = LLM_BASE_PATH + FILE_API_UPLOAD_PATH  # spec:135 → /mavis/api/v1/llm/v1/files/upload
FILES_UPLOAD_URL_CN = AGENT_HOST_CN + FILES_UPLOAD_PATH  # spec:135
FILES_UPLOAD_METHOD = "POST"  # spec:135
# TODO(spec:710)：multipart 字段名与返回的 file-id 引用格式（files_api_ref_scheme 实际取值）
# 静态未确认，本机未触发上传 ⇒ 两者都做成可配置键，缺省值不假装是实测事实。
FILES_API_REF_SCHEME = ""  # spec:561 目录可覆盖 files_api_ref_scheme；空 = 不指定
FILES_API_FILE_ID_TTL_SEC = 3600  # spec:561 files_api_file_id_ttl_sec；TODO: 实测值未知（本地默认）

# --- 鉴权：OAuth2 设备码 + PKCE(S256)（spec:231-259） ---
OAUTH_CLIENT_ID = "mcode-public"  # spec:235,266,694
OAUTH_SCOPES = ("agent.default",)  # spec:236（发请求时以空格 join，spec:253）
OAUTH_AUDIENCE = "agent-backend"  # spec:237,254
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"  # spec:249
REFRESH_GRANT_TYPE = "refresh_token"  # spec:312,694
CODE_CHALLENGE_METHOD = "S256"  # spec:251,254
CODE_VERIFIER_BYTES = 32  # spec:250 randomBytes(32) → base64url
CODE_VERIFIER_ENCODING = "base64url"  # spec:250
CODE_CHALLENGE_HASH = "sha256"  # spec:251 sha256(verifier 的 ascii 字节) → base64url
DEVICE_CODE_PATH = "/oauth2/device/code"  # spec:243
TOKEN_PATH = "/oauth2/token"  # spec:244,312,694
REVOKE_PATH = "/oauth2/revoke"  # spec:245
OAUTH_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"  # spec:257（postForm，全部表单体）
TOKEN_RESPONSE_FIELDS = (  # spec:258
    "access_token",
    "token_type",
    "refresh_token",
    "expires_in",
    "scope",
    "audience",
)
TOKEN_TYPE_BEARER = "Bearer"  # spec:258,291
# access token 是 JWT：客户端读 sub / account_id / scope|scp / exp（spec:258）。
JWT_CLAIM_KEYS = ("sub", "account_id", "scope", "scp", "exp")  # spec:258
# staging 专属：设备码请求带 `X-User-Pre: 1`（spec:259）。**prod 不发**（spec §8=prod）。
STAGING_PRE_HEADER = ("X-User-Pre", "1")  # spec:259 仅 cn-staging/en-staging
# 实机已定论（.tmp/mitm/minimax-code-20260919/ROTATION-VERDICT.md）：access token 实际
# TTL 约 1 小时（spec:317 记的"约 11 天"是误记，已推翻），且 **refresh_token 每次刷新都轮转**
# （被动观测 generation 9→10 时 refresh_token 同步换值）。故刷新策略仍按「到期前留足窗口」
# 实现、不写死 TTL；但"网关先接管磁盘新票"是硬约束（见 store.adopt_credentials_from_client）。
REFRESH_MIN_VALIDITY_MS = 5 * 60 * 1000  # spec:316 AUTH_LEASE_MAX_MIN_VALIDITY_MS（客户端租约口径）
REFRESH_MIN_VALIDITY_S = REFRESH_MIN_VALIDITY_MS // 1000  # spec:312 minValidityMs 同窗口（秒）
LEGACY_COMPATIBILITY_LEASE_MS = 5 * 60 * 1000  # spec:316 legacy 兼容租约（同一量级）

# --- 客户端活性门（网关是否允许**主动** OAuth 刷新；运维配置键，非协议事实） ---
# 共用同一份 OAuth 凭据时任何一方主动刷新都会轮转 refresh_token、作废对方手里的票
# （spec:694 + .tmp/mitm/minimax-code-20260919/ROTATION-VERDICT.md：客户端约每 1h 自刷
# 且每次都换 refresh_token）。故：**客户端进程在 ⇒ 网关不主动刷**（把刷新让给客户端，
# 网关随后从磁盘接管新票，见 store.adopt_credentials_from_client）；客户端不在 ⇒ 网关
# 可以主动刷以维持自身可用。判定实现见 liveness.py；本文件只登记键名与默认值。
ENV_GATEWAY_SELF_REFRESH = "CB_MINIMAX_CODE_GATEWAY_SELF_REFRESH"  # 取值 auto|on|off（缺省/非法 ⇒ auto）
GATEWAY_SELF_REFRESH_MODE_DEFAULT = "auto"  # auto=按进程探测决定；on=总是自刷；off=从不自刷
ENV_CLIENT_PROCESS_MATCH = "CB_MINIMAX_CODE_CLIENT_PROCESS_MATCH"  # 进程名匹配子串（大小写不敏感）
CLIENT_PROCESS_MATCH_DEFAULT = "minimax code"  # 客户端真实进程名是 "MiniMax Code"，探测侧统一 lower() 后比子串

# --- 凭证落盘（spec:261-296,694；本机已验证目录存在，值未读取） ---
DATA_DIR_NAME = ".minimax"  # spec:273（legacy 基名 `.mavis`）
LEGACY_DATA_DIR_NAME = ".mavis"  # spec:273
AUTH_SUBDIR = "auth"  # spec:265 <dataDir>/auth
CREDENTIALS_FILENAME = "auth.json"  # spec:269 **明文 JSON**，非 keychain、非 sqlite（spec:287,296）
AUTH_STATE_FILENAME = "auth-state.json"  # spec:270
AUTH_LOCK_FILENAME = "auth.lock"  # spec:279,315 跨进程文件锁（刷新在锁内串行）
LEGACY_ENCRYPTED_CREDENTIALS_FILENAME = "credentials.enc"  # spec:271 历史落点
CREDENTIAL_SERVICE_TEMPLATE = "com.minimax.mcode.oauth.{build_env}.{region}"  # spec:267
CREDENTIAL_SERVICE = CREDENTIAL_SERVICE_TEMPLATE.format(build_env=BUILD_ENV, region=REGION)  # spec:267,275
CREDENTIAL_ACCOUNT_HASH_INPUT = "{auth_home}" + "\0" + "{client_id}"  # spec:268 sha256→base64url
CREDENTIAL_RECORD_KEY_DELIMITER = "\0"  # spec:289 key = `${service}\0${account}`
# 期望的完整目录（spec:265-275,694）：<home>/.minimax/auth/prod/cn/mcode-public/
CREDENTIAL_DIR_PATH = "{home}" + "/" + DATA_DIR_NAME + "/" + AUTH_SUBDIR + "/" + BUILD_ENV + "/" + REGION + "/" + OAUTH_CLIENT_ID  # spec:275
CREDENTIALS_PATH = CREDENTIAL_DIR_PATH + "/" + CREDENTIALS_FILENAME  # spec:269,694
AUTH_STATE_PATH = CREDENTIAL_DIR_PATH + "/" + AUTH_STATE_FILENAME  # spec:270
# auth.json schema（spec:287-295）：顶层 {schemaVersion, records}；record 键名如下。
CREDENTIAL_SCHEMA_VERSION = 1  # spec:290
CREDENTIAL_RECORD_FIELDS = (  # spec:291-294（值一律不外泄）
    "schemaVersion",
    "accessToken",
    "refreshToken",
    "tokenType",
    "clientId",
    "scopes",
    "audience",
    "expiresAtMs",
    "generation",
    "subject",
    "accountId",
    "loginEpoch",
)
CREDENTIAL_ACCESS_TOKEN_FIELD = "accessToken"  # spec:291,694 读取键名
CREDENTIAL_REFRESH_TOKEN_FIELD = "refreshToken"  # spec:291
# auth-state.json 键结构（spec:281-286，值已脱敏）
AUTH_STATE_SCHEMA_VERSION = 2  # spec:283
AUTH_STATE_STATUS_AUTHENTICATED = "authenticated"  # spec:283
AUTH_STATE_STORE_KIND_FILE = "file"  # spec:283 工厂只返回 FileStore（spec:296）
# 换代语义：每次成功刷新 generation+1；同一登录内 loginEpoch 不变（spec:314）。
GENERATION_STEP = 1  # spec:314
# 共用凭证风险（spec:694）：与运行中的桌面客户端共用同一份会互相顶掉 ⇒
# 本通道按「独立登录态 / 只读快照」实现，写回默认关闭。
READ_ONLY_SNAPSHOT = True  # spec:694 建议项：默认不写回桌面客户端的 auth.json
# 旧 electron-store 落点（spec:299）：顶层键 `tokens`，本机已空 ⇒ 已迁到 OAuth 文件库。
LEGACY_STORE_APP_DIR = "%APPDATA%/MiniMax"  # spec:299
LEGACY_STORE_FILENAME = "minimax-agent-cn-config.json"  # spec:299,677（国内线上命名）
LEGACY_STORE_TOKENS_KEY = "tokens"  # spec:299
# 环境变量注入（受管/CI 场景，spec:300-303，按此优先级取第一个非空）：
ENV_ACCESS_TOKEN_KEYS = ("__MAVIS_PARENT_ACCESS_TOKEN", "MAVIS_ACCESS_TOKEN")  # spec:302
# 子进程另有命名管道租约 broker（spec:319-328）；网关直读文件，不需要这套。
WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\mcode-auth-lease-"  # spec:324 仅审计参考
# 运行时投影是内存态、不额外落盘（spec:304-308）；沙箱模式完全不暴露 token（spec:328）。

# --- 请求头（spec §3.1:334-375 + §9:3:690） ---
ANTHROPIC_VERSION = "2023-06-01"  # spec:342,690 SDK 默认头
API_KEY_PLACEHOLDER = "sk-xxx"  # spec:200,202,343,690
USER_AGENT = "MiniMaxAgent"  # spec:345,358,690 受管专属，覆盖 SDK 默认 UA
MAVIS_AGENT_ID_DEFAULT = "main"  # spec:347,690 readAgentHeaderId 的兜底值
HEADER_ANTHROPIC_VERSION = "anthropic-version"  # spec:342
HEADER_X_API_KEY = "x-api-key"  # spec:343
HEADER_AUTHORIZATION = "Authorization"  # spec:344
HEADER_CONTENT_TYPE = "Content-Type"  # spec:340
HEADER_ACCEPT = "Accept"  # spec:341
HEADER_USER_AGENT = "User-Agent"  # spec:345
HEADER_MAVIS_SESSION_ID = "X-Mavis-Session-Id"  # spec:346 值 = mvs_ + 32 位小写 hex（见下）
HEADER_MAVIS_AGENT_ID = "X-Mavis-Agent-Id"  # spec:347 值 = agent_id 或 "main"
HEADER_MAVIS_TIMEZONE_OFFSET = "X-Mavis-Timezone-Offset"  # spec:348 值 = 秒，东为正
HEADER_ANTHROPIC_BETA = "anthropic-beta"  # spec:349 MiniMax 路径**通常不下发**
# MITM 实测 2026-09-30（dump-003:27）：主请求 `/v1/messages` **必发**该头，值 `true`。
# 出处：`.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-003.json:27`
# 与 `.tmp/mitm/minimax-code-20260919/MITM-VERIFIED-FINDINGS.md` §1B#3 / §3 G01。
# 这是 Anthropic SDK 的**浏览器直连开关**（SDK 在检测到浏览器运行时自动注入，
# 用于让 Anthropic 侧放行 CORS 直连）。实测客户端在 `/v1/messages` 上会发。
# ⚠️ **没有**配套的 `anthropic-beta` 头：实测 `jsonl:8` 的
#    `headerPresence["anthropic-beta"] = null`，且 dump-003 全文无该头 ⇒
#    本头与 anthropic-beta 无关，别因为"看到 dangerouse 字样"就顺手加 beta。
# 对照：两次 `count_tokens`（dump-001/dump-002）**不发**该头 ⇒ 属推理路径专属。
HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS = "anthropic-dangerous-direct-browser-access"
ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS = "true"  # MITM 实测 2026-09-30 dump-003:27
BEARER_PREFIX = "Bearer "  # spec:194,211 真实凭证唯一载体
# MITM 实测 2026-09-30（dump-003:34 / dump-001:31 / dump-002:31）：
#   `x-mavis-session-id: mvs_312d6855b7a74b4990b9faf170aecd4f`
#   = 前缀 `mvs_` + **32 位小写 hex**（无连字符）。旧注释"值 = session UUID
#   （每请求生成）"只对了一半：格式是带前缀的 hex，且三次请求（含两次
#   count_tokens）**复用同一个 id** ⇒ 语义是**会话级**，不是每请求一个。
#   本通道按"一次客户端请求一个会话 id"生成（网关无跨请求会话概念，见 chat.py
#   `new_session_id` 的 docstring）；**格式**严格对齐实测。
MAVIS_SESSION_ID_PREFIX = "mvs_"  # MITM 实测 2026-09-30 dump-003:34
MAVIS_SESSION_ID_HEX_LEN = 32  # MITM 实测 2026-09-30 dump-003:34（uuid4().hex 长度）

# 静态头清单：spec:342-345,690 推理请求里「与环境无关、可写死」的那几项。
# ⚠️ x-api-key 是占位符 **sk-xxx，别删**：Anthropic SDK 由 apiKey 生成它，缺了可能
#    直接触发 SDK "Could not resolve authentication method"；服务端应忽略其值（spec:202,343）。
#    真实凭证只在 Authorization —— 见下 DYNAMIC_HEADER_KEYS，由 chat.py 注入。
REQUEST_STATIC_HEADERS: dict[str, str] = {
    HEADER_ANTHROPIC_VERSION: ANTHROPIC_VERSION,  # spec:342,690
    HEADER_X_API_KEY: API_KEY_PLACEHOLDER,  # spec:343,690 占位符，别删（spec:202）
    HEADER_USER_AGENT: USER_AGENT,  # spec:345,690
    HEADER_MAVIS_AGENT_ID: MAVIS_AGENT_ID_DEFAULT,  # spec:347,690
    # MITM 实测 2026-09-30 dump-003:27：主请求实测有该头，且**无** anthropic-beta
    # 配套（jsonl:8 headerPresence 为 null）。spec §3.1 清单没列它 ⇒ 只照 spec 发
    # 必然漏（MITM-VERIFIED-FINDINGS §3 G01），故在此补齐。
    HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS: ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS,
}
# 由 chat.py 逐请求注入、**不在**静态表里的头（值含凭证/会话/时区，不能写死）：
#   Content-Type: application/json     spec:340,690
#   Accept: application/json           spec:341,690  流式仍是 json（stream:true 在 body）
#   Authorization: Bearer <token>      spec:344,690  **唯一真实凭证**
#   X-Mavis-Session-Id: mvs_<32hex>    spec:346,690 + MITM 实测 2026-09-30 dump-003:34
#   X-Mavis-Timezone-Offset: <秒>      spec:348,690  getTimezoneOffset()*-60（东为正）
DYNAMIC_HEADER_KEYS = (  # spec:340-348,690
    HEADER_CONTENT_TYPE,
    HEADER_ACCEPT,
    HEADER_AUTHORIZATION,
    HEADER_MAVIS_SESSION_ID,
    HEADER_MAVIS_TIMEZONE_OFFSET,
)
CONTENT_TYPE_JSON = "application/json"  # spec:340,341
# prod **不要**发 bedrock-lane：仅 dev/test/staging 生效，且受管路径会先删用户配置里的
# 同名头（spec:350,690）。这里列出两个拼法供 chat.py 做「发出前剔除」的黑名单。
PROD_PROHIBITED_HEADERS = ("bedrock-lane", "bedrock_lane")  # spec:350,690
# 已确认**不属于**推理路径的头（spec §3.2:377-413 账号/业务 API 专用）：
# 签名材料 yy / x-signature / x-timestamp / token(legacy SSO) 只在 `GET /v1/api/user/info`
# 出现；设备指纹、nonce、请求体签名、渠道 ID 头在推理路径**全都没有**（spec:375）。
ACCOUNT_ONLY_SIGN_HEADERS = ("yy", "x-timestamp", "x-signature", "token")  # spec:390-392
# ⚠️ 不要把账号 API 的 md5 签名搬到 LLM 请求上（spec:379-395 是「容易误当成 LLM 签名」的反例）。
ACCOUNT_SIGN_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"  # spec:395 仅账号 API；本通道不使用
ACCOUNT_IDENTITY_PATH = "/v1/api/user/info"  # spec:380,382 账号面，非 LLM 面
# 其他「不命中 MiniMax 网关」的头（spec:351-353）：x-session-affinity（fireworks/CF）、
# HTTP-Referer / X-OpenRouter-*（openrouter.ai）、x-opencode-session + UA:MiniMaxCode
# （opencode.ai/zen/go）。列名只为防止误加。
IRRELEVANT_PROVIDER_HEADERS = (  # spec:351-353
    "x-session-affinity",  # spec:351
    "HTTP-Referer",  # spec:352 openrouter-attribution.ts:1-5（已核对解包副本）
    "X-OpenRouter-Title",  # spec:352 同上 :3
    "X-OpenRouter-Categories",  # spec:352 同上 :4
    "x-opencode-session",  # spec:353 opencode-go-headers.ts:17
    "user-agent",  # spec:353 opencode-go-headers.ts:18（值 'MiniMaxCode'，与受管 UA 不同）
)
# 渲染层业务 API 的共享凭证注入白名单（spec:411）：/mavis/api 前缀属于**账号/业务网关**，
# 不代表 LLM 前缀要签名（spec:413,707 属推断 + 未确认）。
SHARED_AUTH_PATH_PREFIXES = (  # spec:411
    "/mavis/api",
    "/v1/api",
    "/matrix/api",
    "/minimax-cloud/api",
    "/backend",
    "/account",
)

# --- 模型目录（spec:495-520，内置受管 MINIMAX_MODELS 三项，逐个核对） ---
# 字段语义：
#   max_input_tokens  = 上游 limit.context（上下文窗口；M3 另有 1M 档，见 context_window_options）
#   max_output_tokens = 上游 limit.output
#   is_vl             = 是否接受图像/视频输入（= modalities.input 含 image/video，spec:560）
#   is_reasoning      = 目录 `reasoning: true`（spec:501,512-515）
#   thinking          = **思考控制语义**，见下面 THINKING_* 常量
# spec:524-533 的静态结论（**已被 MITM 实测部分推翻，见下 EFFORT_DEFAULT**）：
# Anthropic 方言下 MiniMax 的思考开关是 **on/off 二值**（adaptive / disabled）；
# `{reasoning:{effort:...}}` 只在 openai-responses 方言里出现（spec:530）。
THINKING_MODE_SWITCHABLE = "switchable"  # spec:507 thinking_config.mode
THINKING_CONTROL_ON_OFF = "on_off"  # spec:527-528 isMiniMaxM3ThinkingMode：仅 on|off
THINKING_TYPE_ADAPTIVE = "adaptive"  # spec:531,540,691 thinking **on** 的映射值
THINKING_TYPE_DISABLED = "disabled"  # spec:531,508 thinking **off** 的映射值
# MITM 实测 2026-09-30（dump-001:67-69 / dump-002:1783-1785）：
#   count_tokens 请求实测 `thinking: {"type":"adaptive","display":"summarized"}`
#   ⇒ thinking 还带一个 **display** 伴生字段（旧静态假设只知 `type`）。
# 出处：`.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-001.json:67-69`、
# `req-20260930-172559-002.json:1783-1785`、MITM-VERIFIED-FINDINGS.md §1D / §3 G05。
# ⚠️ 推理路径（/v1/messages）实测**不发 thinking**（dump-003 全文 0 处 `"thinking"`），
#    但本通道仍按调用方意图支持它（见 translate._resolve_thinking 的说明）。
# ⚠️ 注意 constants 里 M3 的 `options.reasoningSummary:"auto"` 与实测字面
#    `"summarized"` **不是同一个值**，别混用。
THINKING_DISPLAY_SUMMARIZED = "summarized"  # MITM 实测 2026-09-30 dump-001:69
THINKING_DISPLAY_FIELD = "display"  # MITM 实测 2026-09-30 dump-001:68
THINKING_DISPLAY_VALUES = (THINKING_DISPLAY_SUMMARIZED,)  # 实测仅见该值（不编造其余档）
THINKING_ON = {
    "thinking": {"type": THINKING_TYPE_ADAPTIVE, THINKING_DISPLAY_FIELD: THINKING_DISPLAY_SUMMARIZED}
}  # spec:441,531 + MITM 实测 2026-09-30 dump-001:67-69（补 display）
THINKING_OFF = {
    "thinking": {"type": THINKING_TYPE_DISABLED, THINKING_DISPLAY_FIELD: THINKING_DISPLAY_SUMMARIZED}
}  # spec:441,531,508 —— ⚠️ `disabled` 与"off 也带 display"**均未实测**：
   # 本次抓包只观测到 `adaptive`（count_tokens），3 个 dump 里 `disabled` 的命中
   # 全是工具描述文本、**不是** thinking 取值 ⇒ 这一行是 spec 外推，不是实测结论。
THINKING_MODE_VALUES = ("on", "off")  # spec:528 唯一合法取值（非 effort 档位）
# effort 档位是「通用非 M3 Anthropic 路径」的东西：low|medium|high|xhigh|max（spec:544），
# 随 output_config.effort 下发（spec:541-542）。
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")  # spec:544
# ⚠️ MITM 实测 2026-09-30 推翻了"受管推理路径不发 effort"这个旧判断：
#   推理请求实测 `output_config: {"effort":"default"}`（dump-003:1822-1823）。
#   ⇒ `"default"` 是**实测到的**取值，**不在** spec:544 那套 low|medium|high|xhigh|max 里
#     （客户端把"未显式选档"也**显式下发**，不是省略字段）。
#   出处：MITM-VERIFIED-FINDINGS.md §1C / §3 G03 / §4。
EFFORT_DEFAULT = "default"  # MITM 实测 2026-09-30 dump-003:1823
# 允许值集合 = 实测的 default + spec:544 的通用档位（两者的并集，缺一不可）。
EFFORT_VALUES = (EFFORT_DEFAULT,) + EFFORT_LEVELS  # MITM 实测 + spec:544
# OpenAI 侧 `reasoning_effort` 的取值（三家口径：none/minimal 属"关思考"，不是档位）。
REASONING_EFFORT_OFF_VALUES = ("none", "minimal", "disabled", "off")  # OpenAI 官方词表
# M2.7 系的思考控制语义：目录只声明 `reasoning: true`，**没有** thinking_config / variants
# （spec:512-515）。⇒ 写成可配置：缺省跟随 M3 的 on/off 开关（同为 Anthropic 方言，
# spec:531 那条映射对 anthropic-messages 通用），若实测不支持，改这两个键即可，不必动 chat.py。
# TODO(spec:512-515,520)：M2.7 无 thinking_config 记录；且 spec:520 的
# disableModelPrefixes=['MiniMax-M2'] 字面会命中 'MiniMax-M2.7' 前缀（内置目录豁免，
# 属远端目录/迁移阶段的行为），实测前不要把 M2.7 当已验证模型对外承诺能力。
DEFAULT_THINKING_ENABLED = True  # spec:507 default_value:'true'（M3 默认开思考）
MODEL_CATALOG: dict[str, dict] = {
    # ⚠️ MITM 实测 2026-09-30：客户端**实际在用**的默认模型就是这一档
    # （dump-003:53 / dump-001:42 / dump-002:42 三处一致）。
    # 出处：`.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-003.json:53`、
    # MITM-VERIFIED-FINDINGS.md §1C / §3 G02。旧 catalog 只有 M3/M2.7 两族 ⇒ 实测
    # 请求会落到 `chat.model_meta` 的宽松兜底（能力位全靠猜）。
    # 字段来源分两类，逐字段标注（**未实测的一律按 M3 同族推断**）：
    #   [实测] max_output_tokens=128000 ← dump-003:68 `max_tokens:128000`（客户端按该模型
    #          的输出上限填的，与 M3 的 limit.output 同值，spec:503）
    #   [推断] 其余能力位（context window 具体值、是否支持 video、attachment 等）
    #          **MITM 未验证** ⇒ 沿用同族 M3 的值，不编造具体数字。
    "MiniMax-M3.1-Flash-Preview": {
        "id": "MiniMax-M3.1-Flash-Preview",  # MITM 实测 2026-09-30 dump-003:53
        "display_name": "MiniMax-M3.1-Flash-Preview",  # MITM 实测 2026-09-30 dump-003:53
        "max_input_tokens": 512_000,  # 按 M3 同族推断，MITM 未验证（spec:503 limit.context）
        "max_output_tokens": 128_000,  # MITM 实测 2026-09-30 dump-003:68（max_tokens=128000）
        "is_vl": True,  # 按 M3 同族推断，MITM 未验证（spec:502,560；实测无 image/video block）
        "is_reasoning": True,  # 按 M3 同族推断，MITM 未验证（spec:501 reasoning:true）
        "modalities": {"input": ["text", "image", "video"], "output": ["text"]},  # 按 M3 同族推断，MITM 未验证
        "thinking": {  # 按 M3 同族推断，MITM 未验证（推理路径实测不发 thinking，dump-003 无该键）
            "mode": THINKING_MODE_SWITCHABLE,  # 按 M3 同族推断，MITM 未验证
            "control": THINKING_CONTROL_ON_OFF,  # 按 M3 同族推断，MITM 未验证
            "default_enabled": True,  # 按 M3 同族推断，MITM 未验证
            "on": THINKING_TYPE_ADAPTIVE,  # 按 M3 同族推断，MITM 未验证（count_tokens 实测为 adaptive，dump-001:68）
            "off": THINKING_TYPE_DISABLED,  # 按 M3 同族推断，MITM 未验证
            # 推理路径实测走 output_config.effort（dump-003:1822-1823），不是 thinking 档位
            # ⇒ 这里与 M3 一样标 False（effort 由 translate 的 output_config 通道承载）。
            "effort": False,
        },
        "attachment": True,  # 按 M3 同族推断，MITM 未验证（spec:501）
        "tool_call": True,  # MITM 实测 2026-09-30：dump-003 带 27 个 tools ⇒ 支持工具调用
        "temperature": True,  # 按 M3 同族推断，MITM 未验证（实测请求未发 temperature）
        "context_window_options": [512_000, 1_000_000],  # 按 M3 同族推断，MITM 未验证
        "context_window_option_hints": {"1000000": "higher_usage"},  # 按 M3 同族推断，MITM 未验证
        "options": {"reasoningSummary": "auto"},  # 按 M3 同族推断，MITM 未验证
        "variants": {"none-thinking": THINKING_OFF, "thinking": THINKING_ON},  # 按 M3 同族推断，MITM 未验证
        "files_api": True,  # 按 M3 同族推断，MITM 未验证（spec:510）
    },
    "MiniMax-M3": {  # spec:500-511
        "id": "MiniMax-M3",  # spec:501 name
        "display_name": "MiniMax-M3",  # spec:501
        "max_input_tokens": 512_000,  # spec:503 limit.context
        "max_output_tokens": 128_000,  # spec:503 limit.output
        "is_vl": True,  # spec:502,560 input 含 image|video
        "is_reasoning": True,  # spec:501 reasoning:true
        "modalities": {"input": ["text", "image", "video"], "output": ["text"]},  # spec:502,560
        "thinking": {  # spec:507-509 开关语义，非档位
            "mode": THINKING_MODE_SWITCHABLE,  # spec:507
            "control": THINKING_CONTROL_ON_OFF,  # spec:527-528
            "default_enabled": True,  # spec:507 default_value:'true'
            "on": THINKING_TYPE_ADAPTIVE,  # spec:509,531
            "off": THINKING_TYPE_DISABLED,  # spec:508,531
            "effort": False,  # spec:524-533 M3 不走 effort 档位
        },
        "attachment": True,  # spec:501
        "tool_call": True,  # spec:501
        "temperature": True,  # spec:501
        "context_window_options": [512_000, 1_000_000],  # spec:504
        "context_window_option_hints": {"1000000": "higher_usage"},  # spec:505
        "options": {"reasoningSummary": "auto"},  # spec:506（默认 summarized，spec:545）
        "variants": {"none-thinking": THINKING_OFF, "thinking": THINKING_ON},  # spec:508-509
        "files_api": True,  # spec:510,552 support_files_api（仅此模型，spec:551-558）
    },
    "MiniMax-M2.7": {  # spec:514-515
        "id": "MiniMax-M2.7",  # spec:514
        "display_name": "MiniMax-M2.7",  # spec:514
        "max_input_tokens": 200_000,  # spec:515 limit.context
        "max_output_tokens": 128_000,  # spec:515 limit.output
        "is_vl": False,  # spec:515,560 仅 text
        "is_reasoning": True,  # spec:515 reasoning:true
        "modalities": {"input": ["text"], "output": ["text"]},  # spec:515,560
        "thinking": {
            "mode": THINKING_MODE_SWITCHABLE,  # 可配置：目录未声明（spec:512-515），见上 TODO
            "control": THINKING_CONTROL_ON_OFF,  # spec:531 anthropic-messages 的通用映射
            "default_enabled": DEFAULT_THINKING_ENABLED,  # 继承 M3 缺省（TODO 同上）
            "on": THINKING_TYPE_ADAPTIVE,  # spec:531
            "off": THINKING_TYPE_DISABLED,  # spec:531
            "effort": False,  # spec:524-533
        },
        "attachment": False,  # spec:515 无附件/无 File API
        "tool_call": True,  # spec:515
        "temperature": True,  # spec:515
        "context_window_options": [200_000],  # 目录未列 options，单值兜底（spec:515）
        "context_window_option_hints": {},  # spec:515
        "options": {},  # spec:515 目录未给 options
        "variants": {},  # spec:515 目录未给 variants
        "files_api": False,  # spec:510 仅 M3 挂 MINIMAX_M3_FILE_API_CAPABILITIES
    },
    "MiniMax-M2.7-highspeed": {  # spec:512-513
        "id": "MiniMax-M2.7-highspeed",  # spec:512
        "display_name": "MiniMax-M2.7-highspeed",  # spec:512
        "max_input_tokens": 200_000,  # spec:513 limit.context
        "max_output_tokens": 128_000,  # spec:513 limit.output
        "is_vl": False,  # spec:513,560
        "is_reasoning": True,  # spec:513
        "modalities": {"input": ["text"], "output": ["text"]},  # spec:513,560
        "thinking": {
            "mode": THINKING_MODE_SWITCHABLE,  # 可配置：同 M2.7（TODO 见上）
            "control": THINKING_CONTROL_ON_OFF,  # spec:531
            "default_enabled": DEFAULT_THINKING_ENABLED,  # TODO 同上
            "on": THINKING_TYPE_ADAPTIVE,  # spec:531
            "off": THINKING_TYPE_DISABLED,  # spec:531
            "effort": False,  # spec:524-533
        },
        "attachment": False,  # spec:513
        "tool_call": True,  # spec:513
        "temperature": True,  # spec:513
        "context_window_options": [200_000],  # spec:513
        "context_window_option_hints": {},  # spec:513
        "options": {},  # spec:513
        "variants": {},  # spec:513
        "files_api": False,  # spec:510
    },
}

# 裸 id 列表：从 MODEL_CATALOG 派生（顺序 = 目录声明顺序，实测默认模型首位）。
STATIC_MODELS = tuple(MODEL_CATALOG)  # spec:499-516 + MITM 实测 2026-09-30（M3.1-Flash-Preview 在首位）

# 默认模型。**MITM 实测 2026-09-30 修正**：客户端实际在用的默认模型是
# `MiniMax-M3.1-Flash-Preview`（dump-003:53 / dump-001:42 / dump-002:42 三处一致；
# MITM-VERIFIED-FINDINGS.md §1C / §3 G02），而 spec:518 记录的 `minimax/MiniMax-M3`
# 是**静态规格当时的**客户端 defaultModel ⇒ 实测优先，改指向实测模型。
# 旧 M3 / M2.7 / M2.7-highspeed 三个条目**保留**（不删，仍是可路由目录项）。
DEFAULT_MODEL = "MiniMax-M3.1-Flash-Preview"  # MITM 实测 2026-09-30 dump-003:53（spec:16,518 为旧值）
# provider/modelId 前缀 `minimax` 是受管入口、`minimax_api` 是自带 key（spec:518）。
MANAGED_MODEL_REF_PREFIX = "minimax"  # spec:518
BYOK_MODEL_REF_PREFIX = "minimax_api"  # spec:518
MODEL_KEY_SEPARATOR = "/"  # spec:518 `minimax/MiniMax-M3`

# 别名表：对外公开名 → 上游裸 id（bind 时再翻回原生 id，见 providers/model_config.py）。
# 本目录 display_name == id，故**不**像 qodercn 那样按展示名派生（会是恒等映射）。
# "auto" 是网关侧的虚拟别名（上游无此 id），翻到默认模型；spec 未给任何别名。
ALIASES: dict[str, str] = {
    "auto": DEFAULT_MODEL,  # 默认模型兜底（上游不认识 "auto"，spec:499-516 无此项）
    # MITM 实测 2026-09-30 dump-003:53：客户端 model-ref 的 provider 前缀就是 `minimax`
    # （`minimax/MiniMax-M3.1-Flash-Preview` 形状，spec:518 的 `minimax/MiniMax-M3` 同族）。
    "minimax/MiniMax-M3.1-Flash-Preview": DEFAULT_MODEL,
    "minimax/MiniMax-M3": "MiniMax-M3",  # spec:518 客户端 model-ref 写法可直接当别名用
    "minimax/MiniMax-M2.7": "MiniMax-M2.7",  # spec:514,518 同上（provider/modelId 形状）
    "minimax/MiniMax-M2.7-highspeed": "MiniMax-M2.7-highspeed",  # spec:512,518 同上
}

# 代码里出现但**不在**内置目录的 id（多为远端目录/测试/历史，spec:520）：
# 仅作审计记录，不代表可路由，禁止拿来当默认或别名目标。
# MITM 实测 2026-09-30：`MiniMax-M3.1-Flash-Preview` 已**升格进目录**（见 MODEL_CATALOG
# 首条），故从本表移除；无后缀的 `MiniMax-M3.1` 仍不在目录（实测只见 `-Flash-Preview`，
# 不能推断无后缀 id 也存在 ⇒ 保留登记，不擅自加进目录）。
NON_CATALOG_MODEL_IDS = ("MiniMax-M3.1", "MiniMax-M2.5", "MiniMax-M2", "MiniMax-M1")  # spec:520
DISABLE_MODEL_PREFIXES = ("MiniMax-M2",)  # spec:520 config.ts:1581（远端目录/迁移阶段的禁用位）
# 远端模型目录来源（spec:520）。cn 值如下；en 另有值但 spec 未给出 ⇒ 不猜，留空。
MODELS_DEV_URL_CN = "https://filecdn.minimax.chat/public/models-dev"  # spec:520
MODELS_DEV_URL_EN = ""  # TODO(spec:520)：en 值 spec 未给出，禁止编造

# --- 多模态与体积上限（spec:549-558，MINIMAX_M3_FILE_API_CAPABILITIES） ---
SUPPORT_FILES_API = True  # spec:552 support_files_api（仅 M3，spec:510）
MAX_IMAGE_BYTES_INLINE = 10_485_760  # spec:554 10 MiB 内可内联 base64
MAX_VIDEO_BYTES_INLINE = 52_428_800  # spec:555 50 MiB
MAX_REQUEST_BODY_BYTES = 67_108_864  # spec:556,466 64 MiB，按**明文 JSON**体积计
MAX_ATTACHMENTS_COUNT = 4  # spec:557
# 超阈值附件走 File API：先 POST .../llm/v1/files/upload 拿 file id，再在 messages 引用
# （spec:561；ref scheme 与 TTL 见上 FILES_API_* 可配置项）。
ATTACHMENT_OVER_LIMIT_TO_FILE_API = True  # spec:561
# PDF：客户端本地 poppler 转图像/文本，上游**没有** document content block（spec:562）。
SUPPORTS_DOCUMENT_BLOCK = False  # spec:562 不要期望上游支持 type:"document"
DOCUMENT_BLOCK_TYPE = "document"  # spec:562 仅用于「显式拒绝」的判定值
# prompt caching：只吃短 ephemeral 标记；长保留 ttl:'1h' 被显式关掉（spec:564）。
CACHE_CONTROL_TYPE = "ephemeral"  # spec:564
CACHE_LONG_RETENTION_SUPPORTED = False  # spec:564 supportsLongCacheRetention:false
CACHE_CONTROL_TTL_1H = "1h"  # spec:564 本通道**不得**下发该 ttl
# 工具调用 = 标准 Anthropic 形状（spec:563）：tools[] 的 name/description/input_schema，
# 末位工具可挂 cache_control；响应 tool_use；流式 input_json_delta 累加后 JSON.parse。
TOOL_INPUT_SCHEMA_TYPE = "object"  # spec:563 input_schema{type:object,...}
# MITM 实测 2026-09-30：推理请求的 **27 个 tool 全部**带 `eager_input_streaming: true`
# （dump-003:83 首个，共 27 处）；而两次 `count_tokens` 的同一批 27 个 tool
# **全部不带** ⇒ 该字段是**推理路径专属**（MITM-VERIFIED-FINDINGS §1C/§1D、§3 G04）。
# 出处：`.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-003.json:83`（27 处）。
# ⚠️ 它**不依赖** `anthropic-beta` 头：实测 `headerPresence["anthropic-beta"] = null`
#    （capture.jsonl:8），却照样发了该字段 ⇒ 旧 TODO"属 beta 细粒度工具流式特性、
#    故不发"这个推论被实测推翻（该推论混淆了 SDK 能力位与 wire 字段）。
EAGER_INPUT_STREAMING = True  # spec:349,563 + MITM 实测 2026-09-30 dump-003:83（27/27）
TOOL_CHOICE_TYPES = ("auto", "any")  # spec:563 目录里明确出现的两个取值（**非**封闭集合）
# 已核对解包副本 pi-ai/dist/providers/anthropic.js:794-799：tool_choice 是**原样透传**
# （字符串 ⇒ `{type: <str>}`，对象 ⇒ 直接塞），客户端根本不枚举取值 ⇒ spec:563 那个省略号
# 是真实行为，不是记录不全。校验时只白名单 auto/any，其余交调用方决定，别自造合法集。
TOOL_CHOICE_TYPES_PARTIAL = True  # spec:563 上游无封闭枚举（透传语义，见上）
# 结构化输出（spec:565）：output_config.format = {type:'json_schema', schema}；
# response_format:{type:'json_object'} 需目录声明 support_json_object_output（目录**未**声明）。
OUTPUT_CONFIG_FORMAT_TYPE_JSON_SCHEMA = "json_schema"  # spec:565
SUPPORT_JSON_OBJECT_OUTPUT = False  # spec:565 内置目录无此声明 ⇒ 不发 json_object
REQUEST_BODY_COMPRESSION = False  # spec:466 SDK 不压请求体（明文 JSON 体积即上限口径）

# --- 请求体契约（spec §4 + §9:4:691） ---
MODEL_FIELD = "model"  # spec:176,691
MESSAGES_FIELD = "messages"  # spec:176
SYSTEM_FIELD = "system"  # spec:176,691 Anthropic 形状：顶层 system **数组**
MAX_TOKENS_FIELD = "max_tokens"  # spec:176,691 必填正整数
STREAM_FIELD = "stream"  # spec:176,691 恒 true（本通道按流式实现）
THINKING_FIELD = "thinking"  # spec:176,691 {type: adaptive|disabled}
OUTPUT_CONFIG_FIELD = "output_config"  # spec:176,542,565 effort / format 载体
TOOLS_FIELD = "tools"  # spec:176
TOOL_CHOICE_FIELD = "tool_choice"  # spec:176,563
RESPONSE_FORMAT_FIELD = "response_format"  # spec:176,565（需 SUPPORT_JSON_OBJECT_OUTPUT）
TEMPERATURE_FIELD = "temperature"  # spec:176 三模型均 temperature:true（spec:501,512-515）
METADATA_USER_ID_FIELD = "metadata"  # spec:176 metadata.user_id
TEXT_BLOCK_TYPE = "text"  # spec:691 system 元素形状 {type:"text",text:...}
BLOCK_TYPE_FIELD = "type"  # spec:691
NO_BODY_ENCODING_LAYER = True  # spec:417-487 明文 JSON：无编码/加密/签名，无 Encode 开关

# --- 流式（spec §6:569-604） ---
SSE_EVENT_MESSAGE_START = "message_start"  # spec:579
SSE_EVENT_MESSAGE_DELTA = "message_delta"  # spec:579
SSE_EVENT_MESSAGE_STOP = "message_stop"  # spec:579,591 **硬结束标志**
SSE_EVENT_CONTENT_BLOCK_START = "content_block_start"  # spec:580
SSE_EVENT_CONTENT_BLOCK_DELTA = "content_block_delta"  # spec:580
SSE_EVENT_CONTENT_BLOCK_STOP = "content_block_stop"  # spec:580
SSE_EVENT_ERROR = "error"  # spec:584 sse.event == error ⇒ 抛错
ANTHROPIC_MESSAGE_EVENTS = frozenset(  # spec:578-581 白名单：其余事件（ping 等）静默忽略
    {
        SSE_EVENT_MESSAGE_START,
        SSE_EVENT_MESSAGE_DELTA,
        SSE_EVENT_MESSAGE_STOP,
        SSE_EVENT_CONTENT_BLOCK_START,
        SSE_EVENT_CONTENT_BLOCK_DELTA,
        SSE_EVENT_CONTENT_BLOCK_STOP,
    }
)
STREAM_HARD_END_EVENT = SSE_EVENT_MESSAGE_STOP  # spec:595 漏发/截断 ⇒ 客户端报错
OPENAI_DONE_SENTINEL = ""  # spec:595 **没有** [DONE] 哨兵（那是 OpenAI 风格）
DELTA_TEXT = "text_delta"  # spec:593
DELTA_THINKING = "thinking_delta"  # spec:593
DELTA_INPUT_JSON = "input_json_delta"  # spec:593,563 累加后 JSON.parse
DELTA_SIGNATURE = "signature_delta"  # spec:593
# stop_reason 映射（spec:594；pi-ai 内部名 → 本网关沿用 Anthropic 原名）
STOP_REASON_END_TURN = "end_turn"  # spec:594
STOP_REASON_MAX_TOKENS = "max_tokens"  # spec:594
STOP_REASON_TOOL_USE = "tool_use"  # spec:594
STOP_REASON_REFUSAL = "refusal"  # spec:594
# usage 只有 Anthropic 原生四个 token 字段，**没有** credit 字段（spec:610-625,693）。
USAGE_INPUT_TOKENS = "input_tokens"  # spec:615,623
USAGE_OUTPUT_TOKENS = "output_tokens"  # spec:616,623
USAGE_CACHE_READ_INPUT_TOKENS = "cache_read_input_tokens"  # spec:617,623
USAGE_CACHE_CREATION_INPUT_TOKENS = "cache_creation_input_tokens"  # spec:618,623
USAGE_FIELDS = (  # spec:623
    USAGE_INPUT_TOKENS,
    USAGE_OUTPUT_TOKENS,
    USAGE_CACHE_READ_INPUT_TOKENS,
    USAGE_CACHE_CREATION_INPUT_TOKENS,
)
# MITM 实测 2026-09-30（capture.jsonl:11 的 usage_snapshots[0]）：
#   响应 usage 实测带**第 5 个嵌套字段**：
#     `output_tokens_details: {"thinking_tokens": 57}`
#   出处：`.tmp/mitm/minimax-code-20260919/capture.jsonl` 第 11 行、
#   MITM-VERIFIED-FINDINGS.md §1E / §3 G06。
# ⚠️ `thinking_tokens` 是 `output_tokens` 的**子集**（实测 57 / 85），
#    **绝不**当成第 5 项加进 total ⇒ `TOTAL_TOKENS_IS_SUM_OF_USAGE_FOUR` 口径不变
#    （见 translate.normalize_usage 的注释与自检断言）。
USAGE_OUTPUT_TOKENS_DETAILS = "output_tokens_details"  # MITM 实测 2026-09-30 capture.jsonl:11
USAGE_THINKING_TOKENS = "thinking_tokens"  # MITM 实测 2026-09-30 capture.jsonl:11
# 转成 OpenAI 风格明细时的槽位（与 upstream/responses.py:624 同键，便于观测面统一读取）：
USAGE_COMPLETION_TOKENS_DETAILS = "completion_tokens_details"  # OpenAI 风格明细键
USAGE_REASONING_TOKENS = "reasoning_tokens"  # OpenAI 风格思考 token 键
TOTAL_TOKENS_IS_SUM_OF_USAGE_FOUR = True  # spec:619,693 总量 = 四项相加（客户端就是这么算）
RESPONSE_HAS_CREDIT_FIELD = False  # spec:610,624 无 reasoning_tokens / credits_used
CLIENT_SIDE_UNIT_COST_IS_ZERO = True  # spec:625 cost 硬编码 0 ⇒ 金额由服务端额度系统决定
# spec:596 网关允许 usage 分批下发：message_delta 可能不带 input_tokens，
# 客户端用 message_start 的值兜底 ⇒ 本通道累计时同样以 message_start 为准做缺省。
USAGE_FALLBACK_FROM_MESSAGE_START = True  # spec:596

# --- count_tokens 端点（**仅登记，不实现**；MITM 实测 2026-09-30）---
# 实测：`POST https://agent.minimax.cn/mavis/api/v1/llm/v1/messages/count_tokens`
# （dump-001:4 / dump-002:4），两次均 200 + `application/json`（非 SSE）。
# 形状对照（MITM-VERIFIED-FINDINGS §1D / §4）：`stream:false`、**无 max_tokens**、
# **无 output_config**、`thinking` 带 `display`、tools[] **不带** eager_input_streaming。
# 任务口径是"不需要实现，仅登记" ⇒ 这里只留端点常量供审计，chat/translate 不使用它。
COUNT_TOKENS_PATH = ANTHROPIC_MESSAGES_PATH + "/count_tokens"  # MITM 实测 2026-09-30 dump-001:4
COUNT_TOKENS_URL_CN = AGENT_HOST_CN + LLM_BASE_PATH + COUNT_TOKENS_PATH  # MITM 实测 2026-09-30 dump-001:4
COUNT_TOKENS_METHOD = "POST"  # MITM 实测 2026-09-30 dump-001:5
COUNT_TOKENS_IMPLEMENTED = False  # 仅登记：本通道不实现该端点（任务口径）

# --- 上游错误码表（spec §7.2:627-655 + §9:5:692）---
# 语义名 → 上游业务码（LLM_ERROR_STATUS_CODES）
USAGE_LIMIT_EXCEEDED = 42212  # spec:632 quota 满 / 用量到顶
LLM_CREDITS_EXHAUSTED = 50110  # spec:633 provider 报余额耗尽 / 上游 HTTP 402
LLM_RATE_LIMITED = 50111  # spec:634 上游 HTTP 429
LLM_AUTH_ERROR = 50112  # spec:635 上游 HTTP 401/403
LLM_UPSTREAM_ERROR = 50113  # spec:636 generic 4xx/5xx
LLM_MIGRATION_ERROR = 50114  # spec:637 目录里有此项（任务未点名，一并登记）
LLM_TPM_RATE_LIMITED = 50150  # spec:638 TPM/RPM 短期限流
LLM_CLUSTER_OVERLOADED = 50151  # spec:639 上游 HTTP 529

# 业务码 → 语义元信息。`upstream_http` 取自 spec:632-639 的注释；None = spec 未给。
UPSTREAM_ERROR_CODES: dict[int, dict] = {
    USAGE_LIMIT_EXCEEDED: {  # spec:632
        "name": "USAGE_LIMIT_EXCEEDED",
        "meaning": "quota 满 / 用量到顶",  # spec:632
        "upstream_http": None,  # TODO(spec:632)：注释只说「用量到顶」，未给 HTTP 状态
        "retryable": False,  # 额度类换号/退避都不解决问题（spec:660 "Do NOT retry"）
    },
    LLM_CREDITS_EXHAUSTED: {  # spec:633
        "name": "LLM_CREDITS_EXHAUSTED",
        "meaning": "provider 余额耗尽",
        "upstream_http": 402,  # spec:633
        "retryable": False,  # spec:660 402 语义 = 不重试
    },
    LLM_RATE_LIMITED: {  # spec:634
        "name": "LLM_RATE_LIMITED",
        "meaning": "通用限流",
        "upstream_http": 429,  # spec:634
        "retryable": True,
    },
    LLM_AUTH_ERROR: {  # spec:635
        "name": "LLM_AUTH_ERROR",
        "meaning": "凭证失效 / 无权限（401 ⇒ 刷新后重放一次）",
        "upstream_http": 401,  # spec:635 原文 401/403
        "upstream_http_alt": 403,  # spec:635
        "retryable": False,  # 由 chat.py 的 401 恢复路径处理，不进通用重试（spec:313）
        "recover_by_refresh": True,  # spec:216,313 401 → recoverToken → 单次重放
    },
    LLM_UPSTREAM_ERROR: {  # spec:636
        "name": "LLM_UPSTREAM_ERROR",
        "meaning": "generic 4xx/5xx",
        "upstream_http": None,  # TODO(spec:636)：兜底类，无固定 HTTP
        "retryable": True,  # 兜底按可重试处理（配合 RETRYABLE_STATUS）
    },
    LLM_MIGRATION_ERROR: {  # spec:637
        "name": "LLM_MIGRATION_ERROR",
        "meaning": "spec 未给语义（仅登记）",
        "upstream_http": None,  # TODO(spec:637)
        "retryable": False,  # TODO(spec:637)：未知，保守不重试
    },
    LLM_TPM_RATE_LIMITED: {  # spec:638
        "name": "LLM_TPM_RATE_LIMITED",
        "meaning": "TPM/RPM 短期限流",
        "upstream_http": None,  # TODO(spec:638)：未给 HTTP（429 是 LLM_RATE_LIMITED 的口径）
        "retryable": True,
    },
    LLM_CLUSTER_OVERLOADED: {  # spec:639
        "name": "LLM_CLUSTER_OVERLOADED",
        "meaning": "集群过载",
        "upstream_http": 529,  # spec:639
        "retryable": True,
    },
}

# 上游内层 status_code（MiniMax 私有码）→ LLM 业务码。**内层优先**：业务级码必须压过
# 传输级 HTTP statusCode（spec:648-654，AI SDK 把上游负载包成 {statusCode:500, responseBody:'{"status_code":...}'}）。
UPSTREAM_STATUS_CODE_MAP: dict[int, int] = {  # spec:642-646
    1400010161: LLM_CREDITS_EXHAUSTED,  # spec:643 MiniMax 内部码：余额不足
    2056: USAGE_LIMIT_EXCEEDED,  # spec:644 MiniMax 内部码：用量超限
    2067: USAGE_LIMIT_EXCEEDED,  # spec:645 Token Plan 已达限且积分自动消耗关闭
}
UPSTREAM_STATUS_CODE_MEANINGS: dict[int, str] = {  # spec:643-645
    1400010161: "MiniMax 内部码：余额不足",
    2056: "MiniMax 内部码：用量超限",
    2067: "Token Plan 已达限且积分自动消耗关闭",
}
# TPM 限流的一组 message codes（spec:641）：命中即按 LLM_TPM_RATE_LIMITED 归类。
LLM_TPM_RATE_LIMIT_MESSAGE_CODES = frozenset({2045, 2046, 2047, 1039, 1041})  # spec:641
# 分类后的对外 HTTP 状态（spec:692「限流/额度耗尽要映射成 402/429/529 + 内层 status_code 双写」）。
# 402/429/529 三个是 spec 明示的；其余为本网关的兜底选择（可配置，见下 TODO）。
ERROR_CODE_TO_HTTP_STATUS: dict[int, int] = {  # spec:632-639,692
    USAGE_LIMIT_EXCEEDED: 429,  # spec:692 用量超限 ⇒ 429（TODO: spec 未直接点名 42212 的 HTTP）
    LLM_CREDITS_EXHAUSTED: 402,  # spec:633,692
    LLM_RATE_LIMITED: 429,  # spec:634,692
    LLM_AUTH_ERROR: 401,  # spec:635
    LLM_UPSTREAM_ERROR: 502,  # TODO(spec:636)：generic 兜底，502 为网关自择（非 spec 事实）
    LLM_MIGRATION_ERROR: 500,  # TODO(spec:637)：语义未给，500 为网关自择（非 spec 事实）
    LLM_TPM_RATE_LIMITED: 429,  # spec:638,692
    LLM_CLUSTER_OVERLOADED: 529,  # spec:639,692
}
# Anthropic 语义错误类型 → 本通道业务码（spec:584 流内 error 事件、spec:648-654 内层码优先）。
# Anthropic 信封常只给 error.type 字符串而无数字码；不映射的话对外错误帧会缺 code，
# 客户端与观测面都无法按业务码分类（对应自检里 overloaded_error 那一例）。
ANTHROPIC_ERROR_TYPE_TO_CODE: dict[str, int] = {
    "overloaded_error": LLM_CLUSTER_OVERLOADED,  # spec:639 上游 529 集群过载
    "rate_limit_error": LLM_RATE_LIMITED,  # spec:634 上游 429
    "authentication_error": LLM_AUTH_ERROR,  # spec:635 上游 401/403
    "permission_error": LLM_AUTH_ERROR,  # spec:635 同属鉴权/权限面
    "invalid_request_error": LLM_UPSTREAM_ERROR,  # spec:636 generic 4xx
    "not_found_error": LLM_UPSTREAM_ERROR,  # spec:636
    "request_too_large": LLM_UPSTREAM_ERROR,  # spec:636（体积超限，见 MAX_REQUEST_BODY_BYTES）
    "api_error": LLM_UPSTREAM_ERROR,  # spec:636 上游 5xx 兜底
    "timeout_error": LLM_UPSTREAM_ERROR,  # spec:636
    "billing_error": LLM_CREDITS_EXHAUSTED,  # spec:633 余额耗尽（Anthropic 侧措辞）
}
# 错误体兼容三种嵌套形状（spec:655），键名登记供 chat.py 逐个试：
UPSTREAM_STATUS_CODE_KEY = "status_code"  # spec:649,653 MiniMax base_resp 内层码
UPSTREAM_STATUS_MSG_KEY = "status_msg"  # spec:649,655
STATUS_INFO_KEY = "statusInfo"  # spec:655 形如 {statusInfo:{code,message}}
ERROR_ENVELOPE_KEY = "error"  # spec:655 形如 {error:{...}}
INNER_CODE_TAKES_PRIORITY = True  # spec:648-654 内层业务码优先于外层 HTTP
# 401 恢复路径：失效→刷新→单次重放；刷新失败或 loginEpoch 变化 → logout（spec:313）。
UNAUTHORIZED_RETRY_LIMIT = 1  # spec:219,313 只重放一次
# 退避参数 / Retry-After 行为静态未确认（spec:711），仅见本地退避表 ⇒ 交给 providers.retry。
RETRY_AFTER_UNKNOWN = True  # spec:711 TODO：429/529 退避与 retry-after 需 MITM 确认

# --- 非 LLM 的额度面（spec:657-663）：通道若要统计 credit 才需要，均**不是** LLM 响应字段 ---
ENTITLEMENT_FIELDS = (  # spec:659 agent-tools/src/shared/entitlement.ts:56-99
    "entitlement_key",
    "limit",
    "current",
    "suggest_limit",
)
BUSINESS_API_INSUFFICIENT_CREDITS_HTTP = 402  # spec:660 "Do NOT retry"
DAILY_SIGNIN_FIELDS = (  # spec:661 每日签到积分（非 LLM）
    "day_no",
    "points",
    "bonus_points",
    "status",
    "expire_at_ms",
    "claim_id",
)
LLM_USAGE_REPORT_ENDPOINT = ""  # spec:663 **未发现**任何把 LLM 用量上报回服务端的接口 ⇒ 禁止臆造


def model_entry(model: str) -> dict | None:
    """按裸 id 或别名取目录项；未知返回 None（调用方决定回退策略）。"""
    value = (model or "").strip()
    if value in MODEL_CATALOG:
        return MODEL_CATALOG[value]
    target = ALIASES.get(value)
    if target in MODEL_CATALOG:
        return MODEL_CATALOG[target]
    return None


def thinking_payload_for(model: str, mode: str | bool | None) -> dict:
    """把 on/off 思考开关翻成 Anthropic 方言的 body 片段。

    mode 接受 "on"/"off"（spec:528）或 bool。目录未声明 thinking 时按 on/off 开关处理
    （可配置，见 DEFAULT_THINKING_ENABLED 处的 TODO）。返回 `{}` 表示不下发 thinking 字段。
    """
    entry = model_entry(model) or {}
    thinking = entry.get("thinking") or {}
    if thinking.get("control") != THINKING_CONTROL_ON_OFF:
        return {}  # 非 on/off 语义（当前目录没有这种项）⇒ 不猜，交给调用方
    if isinstance(mode, bool):
        enabled = mode
    elif isinstance(mode, str):
        lowered = mode.strip().lower()
        if lowered not in THINKING_MODE_VALUES:
            return {}  # 非法取值：不下发，绝不当成 effort 档位（spec:524-533）
        enabled = lowered == "on"
    else:
        enabled = bool(thinking.get("default_enabled", DEFAULT_THINKING_ENABLED))
    return {"thinking": {
        "type": thinking.get("on" if enabled else "off"),
        # MITM 实测 2026-09-30 dump-001:67-69：display 是 thinking 的伴生字段。
        THINKING_DISPLAY_FIELD: THINKING_DISPLAY_SUMMARIZED,
    }}


def _self_check() -> None:
    """把「/v1 只有一个」和「目录派生一致」钉成导入期不变量（spec:111-119,689）。

    2026-09-30 MITM 实测新增的断言标 `MITM 实测 2026-09-30` 并给出 dump 行号
    （实测优先于旧静态断言，但不放宽成无断言）。
    """
    assert CHAT_PATH == "/mavis/api/v1/llm/v1/messages", CHAT_PATH  # spec:131,689
    assert CHAT_PATH.count("/v1") == 2, CHAT_PATH  # 恰好两处：网关前缀 /mavis/api/v1/llm + SDK 的 /v1/messages
    assert "/v1/v1/" not in CHAT_PATH, CHAT_PATH  # 净效果**不是** /v1/v1/messages（spec:689 的头号坑）
    assert CHAT_URL_CN == "https://agent.minimax.cn/mavis/api/v1/llm/v1/messages", CHAT_URL_CN  # spec:131
    assert FILES_UPLOAD_PATH == "/mavis/api/v1/llm/v1/files/upload", FILES_UPLOAD_PATH  # spec:135
    assert LLM_BASE_PATH == PRESET_CHAT_BASE_PATH.removesuffix("/v1"), LLM_BASE_PATH  # spec:100-111
    assert REQUEST_STATIC_HEADERS["x-api-key"] == "sk-xxx"  # spec:202,343 占位符别删
    assert ALIASES["auto"] in MODEL_CATALOG  # "auto" 必须指向真实目录项
    assert DEFAULT_MODEL in MODEL_CATALOG
    assert set(STATIC_MODELS) == set(MODEL_CATALOG)  # 派生列表与目录同源
    assert {USAGE_LIMIT_EXCEEDED, LLM_CREDITS_EXHAUSTED, LLM_RATE_LIMITED,
            LLM_AUTH_ERROR, LLM_UPSTREAM_ERROR, LLM_TPM_RATE_LIMITED,
            LLM_CLUSTER_OVERLOADED} <= set(UPSTREAM_ERROR_CODES)  # spec:632-639
    assert set(UPSTREAM_STATUS_CODE_MAP) == {1400010161, 2056, 2067}  # spec:642-646
    assert set(UPSTREAM_STATUS_CODE_MAP.values()) <= set(UPSTREAM_ERROR_CODES)

    # --- MITM 实测 2026-09-30 的不变量（dump 行号见各常量注释） ---
    # G01：主请求实测必发该头，且**没有** anthropic-beta 配套（capture.jsonl:8）。
    assert REQUEST_STATIC_HEADERS[
        HEADER_ANTHROPIC_DANGEROUS_DIRECT_BROWSER_ACCESS
    ] == "true"  # MITM 实测 2026-09-30 dump-003:27
    assert HEADER_ANTHROPIC_BETA not in REQUEST_STATIC_HEADERS  # MITM 实测 2026-09-30（未发）
    # G02：实测默认模型进目录且在首位（STATIC_MODELS 顺序 = 目录声明顺序）。
    assert DEFAULT_MODEL == "MiniMax-M3.1-Flash-Preview"  # MITM 实测 2026-09-30 dump-003:53
    assert STATIC_MODELS[0] == DEFAULT_MODEL  # MITM 实测 2026-09-30（放首位）
    assert MODEL_CATALOG[DEFAULT_MODEL]["max_output_tokens"] == 128_000  # MITM 实测 dump-003:68
    for legacy in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
        assert legacy in MODEL_CATALOG, legacy  # 旧条目必须保留（不删）
    # G03：effort 实测值 default 在允许集合里（旧值域 low|medium|high|xhigh|max 漏了它）。
    assert EFFORT_DEFAULT == "default"  # MITM 实测 2026-09-30 dump-003:1823
    assert EFFORT_DEFAULT in EFFORT_VALUES and set(EFFORT_LEVELS) <= set(EFFORT_VALUES)
    # G05：thinking 实测带 display（count_tokens 实测到 `adaptive`）。
    assert THINKING_ON["thinking"][THINKING_DISPLAY_FIELD] == "summarized"  # MITM 实测 dump-001:69
    # ⚠️ off 行的 display 是 spec 外推（`disabled` 未实测，见 THINKING_OFF 注释）：
    # 这里只断言"形状与 on 对齐"，**不得**标成实测结论。
    assert THINKING_OFF["thinking"][THINKING_DISPLAY_FIELD] == "summarized"  # spec 外推，非实测
    assert thinking_payload_for("MiniMax-M3", "on")["thinking"][THINKING_DISPLAY_FIELD] == "summarized"
    # G07：session id 实测 = mvs_ + 32 位 hex。
    assert MAVIS_SESSION_ID_PREFIX == "mvs_" and MAVIS_SESSION_ID_HEX_LEN == 32  # MITM 实测 dump-003:34
    # 次要3：usage 第 5 个嵌套字段登记在案，但**不进** total 的四项口径。
    assert TOTAL_TOKENS_IS_SUM_OF_USAGE_FOUR is True  # MITM 实测（thinking_tokens 是 output 子集）
    assert USAGE_OUTPUT_TOKENS_DETAILS not in USAGE_FIELDS  # 不扩四字段口径
    assert USAGE_THINKING_TOKENS == "thinking_tokens"  # MITM 实测 capture.jsonl:11
    # G09：count_tokens 仅登记（不实现），端点形状与实测一致。
    assert COUNT_TOKENS_PATH == "/v1/messages/count_tokens"  # MITM 实测 dump-001:4
    assert COUNT_TOKENS_IMPLEMENTED is False  # 任务口径：仅登记
    # 次要2：eager_input_streaming 实测为 true 且不依赖 anthropic-beta。
    assert EAGER_INPUT_STREAMING is True  # MITM 实测 2026-09-30 dump-003:83


_self_check()

from providers.retry import RETRYABLE_STATUS  # noqa: E402  (统一重试常量)
