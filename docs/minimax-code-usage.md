# MiniMax Code 通道使用说明（Buddy2API）

本文档说明 **MiniMax Code 通道**（通道 id `minimax_code`）的定位、协议形状、能力边界与已知未验证项。

> 协议权威来源：`.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md`（本机静态逆向规格，
> 纯 asar 解包 + 源码阅读，**零网络流量**）。文中 `spec:NNN` 均指该文件行号，便于逐条审计。
> 实现代码在 `src/providers/minimax_code/`（`constants.py` 存协议事实、`translate.py` 存方言翻译、
> `store.py` 存凭据发现、`token.py` 存 OAuth2 换票、`chat.py` 存装配调度）。

---

## 1. 通道定位与来源

- **来源**：逆向 **MiniMax Code 桌面客户端**（Electron 应用）的 LLM 调用路径。不是官方 API 文档、
  不是 MiniMax 开放平台的 BYOK 接口（spec:134,141-142）。
- **登录态**：复用桌面客户端已经登录好的**受管登录态**——凭证来自客户端自己落盘的
  `auth.json`（明文 JSON），网关只**读取快照**（见 §5）。
- **默认关闭（opt-in）**：`minimax_code` 注册在 `providers/__init__.py` 的 `OPT_IN_PROVIDER_IDS`
  里，与 `gmi` / `bailian` 同侧。理由：**逆向通道不应默认开启**。启用方式二选一：
  - 环境变量：`CB_GATEWAY_PROVIDERS=workbuddy,qclaw,qwenwork,traework,traesolo,minimax_code`
  - 管理页「通道管理」里对该通道点「启用」（`CB_GATEWAY_PROVIDERS` 一旦设置，UI 开关变只读）。
- **模型**：`MiniMax-M3`（默认，spec:518）、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed`
  （内置目录 spec:499-516）。别名 `auto` 翻到默认模型；客户端 model-ref 写法
  `minimax/MiniMax-M3` 也可直接当别名用（spec:518）。
- **可覆盖 host**：`channel_hosts` 里本通道只认两个字段——`llm`（推理网关）与 `oauth`
  （换票面），与 `providers/minimax_code/chat.py`、`token.py` 里的 `channel_host(CHANNEL_ID, ...)`
  调用逐字一致。默认 `llm` = `https://agent.minimax.cn`（spec:88,131），
  `oauth` = `https://account.minimax.cn`（spec:241）。

---

## 2. 上游方言：Anthropic Messages（不是 OpenAI Chat Completions）

- 请求路径：`POST {origin}/mavis/api/v1/llm/v1/messages`（spec:131,689）。
  注意 `/v1` 的坑：客户端预置 base 末尾**带** `/v1`（spec:88），`normalizeProviderBaseUrl()`
  对 anthropic-messages **先剥掉**末尾 `/v1`（spec:100-111），再由 `@anthropic-ai/sdk` 拼
  `/v1/messages`（spec:113-125）——净效果**只保留一个 `/v1`**，绝不是 `/v1/v1/messages`。
  这条不变量在 `constants.py` 的 `_self_check()` 里以导入期 assert 钉住。
- 请求体最小集（spec:691）：`{model, max_tokens, stream:true, messages:[...], system?:[{type:"text",text:...}]}`。
  `system` 是 Anthropic 形状的**顶层数组**（spec:176,691），不是 OpenAI 的 system 角色消息。
- 流式事件（spec:578-595）：`message_start` / `content_block_start` / `content_block_delta` /
  `content_block_stop` / `message_delta` / `message_stop`。**`message_stop` 是硬结束标志**，
  漏发即视为截断（spec:595）；**没有** OpenAI 风格的 `[DONE]` 哨兵（spec:595）。
  其余事件（`ping` 等）静默忽略。
- `usage` 只有 Anthropic 原生四字段：`input_tokens` / `output_tokens` /
  `cache_read_input_tokens` / `cache_creation_input_tokens`（spec:610-623）；
  总量 = 四项相加（spec:619,693）。`message_delta` 可能不带 `input_tokens`，
  此时以 `message_start` 的值兜底（spec:596）。

### 明文 JSON 结论（重要）

请求/响应体是**明文 JSON**，**不存在任何编码、加密、签名层**，也没有 `Encode=1` 之类的开关
（spec:417-487,695）。因此：

- 不要给这个通道加请求体签名、base64 包装或自定义编码——源端根本没有这些，
  加了只会失败得更难查（spec:695）。
- 唯一真实凭证载体是 `Authorization: Bearer <token>`（spec:194,211,344）。
- `x-api-key: sk-xxx` 是**占位符且必须原样发送**（spec:200-202,343）：Anthropic SDK 由
  `apiKey` 生成该头，缺了可能直接触发 SDK "Could not resolve authentication method"。
  服务端应忽略其值。
- 静态头（与环境无关、可写死，spec:342-345,690）：`anthropic-version: 2023-06-01`、
  `x-api-key: sk-xxx`、`User-Agent: MiniMaxAgent`、`X-Mavis-Agent-Id: main`。
  逐请求注入的动态头（spec:340-348,690）：`Content-Type`、`Accept`、
  `Authorization`、`X-Mavis-Session-Id`（每请求 UUID）、`X-Mavis-Timezone-Offset`（秒，东为正）。
- prod **不要发** `bedrock-lane`（spec:350,690）。

---

## 3. thinking 是 on/off 开关，不是 effort 档位

Anthropic 方言下 MiniMax 的思考控制是**二值开关**（spec:524-533,691）：

| 语义 | 下发值 |
|---|---|
| 思考 **on** | `thinking: {"type": "adaptive"}`（spec:531） |
| 思考 **off** | `thinking: {"type": "disabled"}`（spec:531,508） |

- 合法取值只有 `on` / `off`（`THINKING_MODE_VALUES`，spec:528）。
- **不是** `low|medium|high|xhigh|max` 这类 effort 档位。`{reasoning:{effort:...}}` 只在
  openai-responses 方言里出现（spec:530），本通道**不下发**；`output_config.effort`
  （spec:541-542）属于「通用非 M3 Anthropic 路径」，M3 的受管开关路径不使用。
- 本通道**没有** `supports_reasoning_effort` 能力位，所以管理页「模型配置」里该通道
  显示为「不适用」（README 的思考档位说明只对 WorkBuddy 上游成立）。
- M3 目录声明 `thinking_config.mode=switchable`、默认 `true`（spec:507）。
  M2.7 系目录只声明 `reasoning:true`，**没有** `thinking_config`/`variants`（spec:512-515）——
  实现按 M3 的同一套 on/off 映射处理（spec:531 那条映射对 anthropic-messages 通用），
  属**可配置 + 未实测**，见 §7。

---

## 4. 额度（credit）：没有查询 API，只能 token 估算

- **没有额度查询接口**。规格全篇未发现任何「把 LLM 用量上报回服务端」或「查询剩余额度」的
  LLM 面接口（spec:663），LLM 响应里也**没有** credit / credits_used / reasoning_tokens 字段
  （spec:610,624）。客户端侧单位成本硬编码为 0（spec:625）。
- 因此本通道的 `fetch_quota` **恒返回 `unsupported=True`**（spec:663），
  且 `remaining=None`。注意：跨通道求和会把「不知道」当 0，看板必须**分列**展示。
- credit 统计走**网关侧 token 估算**：`upstream_credit` 取不到值时退回
  `channel_credit_rate("minimax_code")` 的 token 口径（`chat.py` 的日志路径），
  `credit_source` 记为 `estimate`。这是本通道唯一可行的近似，**不是官方锚点**。
- 管理页模型倍率列显示「-」：`fetch_model_rates()` 返回 `rate=None`、
  `official=False`，只诚实给出 `context_window`（目录里的 `max_input_tokens`，spec:499-516）。
- 另：每日签到积分（spec:661）、工具/云能力额度对象（spec:659）都是**非 LLM 面**的字段，
  本通道不据此对外承诺额度能力。本通道 `checkin_supported = False`（无签到 API）。

---

## 5. 与桌面客户端共用 `auth.json` 的互斥风险

**结论（spec:694）：本通道只读快照，不回写客户端 `auth.json`。**

- 凭证落点（spec:265-275,694）：`%USERPROFILE%\.minimax\auth\prod\cn\mcode-public\auth.json`，
  **明文 JSON**（不是 keychain、不是 sqlite，spec:287,296）。
  记录键形如 `com.minimax.mcode.oauth.prod.cn\0<account>`，值含
  `accessToken` / `refreshToken` / `expiresAtMs` / `generation` / `loginEpoch` 等（spec:289-294）。
- **风险**：spec:694 明确提示——`generation` 会换代、**refresh token 可能轮转**，
  与运行中的桌面客户端共用同一份凭证会**互相顶掉**（一边刷新，另一边的 refresh token 作废）。
  规格给出的建议就是「独立登录态或**只读快照**」，本通道取后者（`READ_ONLY_SNAPSHOT = True`）。
- 401 恢复路径（spec:216,313）：失效 → 刷新 → **单次重放**；刷新失败或 `loginEpoch`
  变化即 logout。刷新打 `https://account.minimax.cn/oauth2/token`，
  `grant_type=refresh_token`（spec:241,312,694）。
- 凭据导入面：`discover()` / `import_path()` / `parse_credentials()` / `upsert_account()`
  都在管理页「通道管理」可用；`import_path` 只接受落在 `CB_MINIMAX_CODE_AUTH_DIR`
  或默认 `~\.minimax\auth\prod\cn` 之内的路径。
- **本文档与代码、测试、日志里都不出现真实 token 原文**。测试一律用假值
  （`AT-FAKE` / `RT-FAKE`）。

### 环境变量

| 变量 | 说明 |
|---|---|
| `CB_MINIMAX_CODE_AUTH_DIR` * | 覆盖 auth 根目录或凭证目录本身（默认扫 `%USERPROFILE%\.minimax\auth\prod\cn\mcode-public`）。 |
| `CB_MINIMAX_CODE_REFRESH_SKEW_MS` * | 刷新提前量（毫秒），非 spec 实证值，缺省 `60000`。 |

---

## 6. 多实例硬边界（只认 prod/cn 命名空间）

本机安装属于 **prod / zh / region=cn**（spec:669-682，决定性证据是 asar 内的 `.env.local`：
`NEXT_PUBLIC_BUILD_ENV="prod"`、`NEXT_PUBLIC_LOCALE="zh"`）。据此本通道设了两道**缺一不可**的边界
（`store.py` 的 `minimax_auth_dirs()`）：

1. **目录层**：逐段白名单 `prod` / `cn` / `mcode-public`。
   `en` / `staging` / `test` / `internal` / `inside` 目录**永不入选**（spec:82-90,675,682）。
   **环境变量覆盖也照样受这一层管**：覆盖值指向末段明写着 staging/en 的凭证目录时直接跳过
   （只记 warning 日志）。只有路径层级被挂载/改名抹平（容器里 `/auth` 这种）才按
   「无命名空间信息」放行，交给第 2 道兜底。
2. **记录层**：`auth.json` 记录键必须落在 `com.minimax.mcode.oauth.prod.cn\0` 前缀内
   （spec:267,289）。即便运维把别的命名空间目录改名/拷进来，第 2 道也会拒掉。

另外，本通道**不进** `_WINDOWS_DPAPI_CHANNELS`：`auth.json` 是明文，加进去会让容器挂载场景
失效（容器内本来就读不了 DPAPI 加密的 QClaw / QwenWork 文件，本通道不存在这个问题）。

---

## 7. 错误码表

上游把额度/限流表达成**错误码**，而不是回报字段（spec:627-646）。错误体是**双层信封**，
且**内层业务码优先于外层 HTTP 状态**（spec:648-654）：

```
{status_code, status_msg}          // MiniMax base_resp
{statusInfo:{code,message}}
{error:{...}}
```

### 7.1 业务码（spec:632-639）

| 业务码 | 语义 | 上游 HTTP | 是否重试 |
|---|---|---|---|
| `42212` | `USAGE_LIMIT_EXCEEDED` —— quota 满 / 用量到顶 | spec 未给 | 否（换号/退避都不解决） |
| `50110` | `LLM_CREDITS_EXHAUSTED` —— 余额耗尽 | 402 | 否（402 = Do NOT retry） |
| `50111` | `LLM_RATE_LIMITED` —— 通用限流 | 429 | 是 |
| `50112` | `LLM_AUTH_ERROR` —— 凭证失效 / 无权限 | 401 / 403 | 走 401 恢复路径（刷新+单次重放），不进通用重试 |
| `50113` | `LLM_UPSTREAM_ERROR` —— generic 4xx/5xx | spec 未给 | 是（兜底） |
| `50150` | `LLM_TPM_RATE_LIMITED` —— TPM/RPM 短期限流 | spec 未给 | 是 |
| `50151` | `LLM_CLUSTER_OVERLOADED` —— 集群过载 | 529 | 是 |

> 目录里另有 `50114` `LLM_MIGRATION_ERROR`（spec:637，spec 未给语义，本通道登记为保守不重试）。

### 7.2 上游内层 MiniMax 私有码 → 业务码（spec:642-646）

| 内层 `status_code` | 含义 | 归类到 |
|---|---|---|
| `1400010161` | MiniMax 内部码：余额不足 | `50110` `LLM_CREDITS_EXHAUSTED` |
| `2056` | MiniMax 内部码：用量超限 | `42212` `USAGE_LIMIT_EXCEEDED` |
| `2067` | Token Plan 已达限且积分自动消耗关闭 | `42212` `USAGE_LIMIT_EXCEEDED` |

TPM 限流另有一组 message code（spec:641）：`2045 / 2046 / 2047 / 1039 / 1041`，
命中即归 `50150`。

### 7.3 对外 HTTP 状态

`402 / 429 / 529` 三个是 spec 明示的（spec:692：限流/额度耗尽要映射成 402/429/529
**+ 内层 `status_code` 双写**，才能被上层正确分类）。其余为网关兜底选择
（`50113→502`、`50114→500`、`42212→429`），**不是 spec 事实**，可配置。

Anthropic 信封常只给 `error.type` 字符串而无数字码，`ANTHROPIC_ERROR_TYPE_TO_CODE`
把 `overloaded_error`→`50151`、`rate_limit_error`→`50111`、`authentication_error`/
`permission_error`→`50112`、`billing_error`→`50110`、其余→`50113`，避免对外错误帧缺 code。

---

## 8. 已知未验证项（需 MITM 才能定论）

以下均出自 spec:703-709（及 710-711），**静态逆向无法确认**，实现按「可配置 + 不编造」处理：

| 项 | 为什么不确定 |
|---|---|
| 服务端**必填**头/字段（`X-Mavis-*`、`anthropic-version` 是否强校验） | 只能看出客户端会发，看不出服务端拒不拒（spec:703） |
| `x-api-key: sk-xxx` 占位符是否被网关忽略 | 客户端一定会发，但网关行为未知（spec:705） |
| `Authorization` 之外用 `x-api-key` 传真 token 是否同权（受管 vs BYOK 是否同路径同权） | 源码里 `minimax` 与 `minimax_api` 是两个分开的入口（域名/路径不同，spec:706） |
| `/mavis/api/*` 网关是否要求 `yy` / `x-signature`（账号签名是否外溢到 LLM 前缀） | 代码只把签名用在 `/v1/api/user/info`，`proxy.ipc.js` 注释暗示签名属 matrix 网关，**属推断**（spec:707） |
| 网关是否返回额外的 MiniMax 私有 SSE 事件（被 `ANTHROPIC_MESSAGE_EVENTS` 静默忽略） | 客户端忽略未知事件，静态无法枚举（spec:708） |
| 是否存在 UA / 客户端版本的 **WAF 级校验** | 属反爬层而非协议层（spec:709） |
| access token 真实 TTL 与 refresh token 是否轮转 | `expiresAtMs` 是服务端签发值，静态只见客户端读取（spec:704） |
| `files/upload` 的 multipart 字段名与 file-id 引用格式 | 目录可配置，本机未触发上传（spec:710） |
| 429/529 的退避参数与 `retry-after` 头 | 仅见本地退避表（spec:711） |

**结论**：`X-Mavis-*` 三头本通道照客户端原样发送（最坏情况是被忽略，不是被拒），
`yy`/`x-signature` **一律不发**（无静态证据支持，乱发更可能触发风控）。
以上任何一条若要定论，必须走 MITM 抓包，**不得用真实生产 API 试探/重放/压测**。

---

## 9. 接入步骤（离线可做的部分）

1. 在桌面客户端完成登录，确认 `%USERPROFILE%\.minimax\auth\prod\cn\mcode-public\auth.json` 存在。
2. 启用通道：`CB_GATEWAY_PROVIDERS=workbuddy,...,minimax_code`，或管理页点「启用」。
3. 管理页「通道管理」→ 选 MiniMax Code → 「重新检测」→「一键导入」；
   或直接粘贴 `auth.json` 内容 / 裸 JWT 建号（`parse_credentials` 会做形状适配）。
4. 点该账号的「测试」：返回一句话即说明上游链路通。
5. 模型配置页确认白名单（默认三个 M3/M2.7 模型）；该页的「思考档位」列对本通道不适用（§3）。

> **风控红线**：本通道的全部验证都在离线完成——测试用 `httpx.MockTransport` 假传输
> （`PROVIDER.set_transport(...)`），**严禁向任何 MiniMax 生产 API 发真实请求**
> （不重放、不试探、不压测、不轮询）。