# MiniMax Code 通道使用说明（Buddy2API）

本文档说明 **MiniMax Code 通道**（通道 id `minimax_code`）的定位、协议形状、能力边界与已知未验证项。

> 协议权威来源：`.tmp/mitm/minimax-code-20260919/MINIMAX-CODE-LLM-PROTOCOL-SPEC.md`（本机静态逆向规格，
> 纯 asar 解包 + 源码阅读，**零网络流量**）。文中 `spec:NNN` 均指该文件行号，便于逐条审计。
> 实现代码在 `src/providers/minimax_code/`（`constants.py` 存协议事实、`translate.py` 存方言翻译、
> `store.py` 存凭据发现、`token.py` 存 OAuth2 换票、`chat.py` 存装配调度）。
>
> **2026-09-30 追加**：本机做了一次**受控 MITM 抓包**（1 条真实推理消息 + 2 次 `count_tokens`，**零重放**），
> 已把静态推断里被推翻/补全的部分固化成 §10「MITM 实测（2026-09-30）」。
> 抓包已结束且环境已还原；本文档不触发任何新的抓包，也不向生产 API 发任何真实请求。

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
- **模型**：`MiniMax-M3.1-Flash-Preview`（默认，MITM 实测 2026-09-30 dump-003:53）、
  `MiniMax-M3`、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed`（后三档来自内置目录 spec:499-516，**保留**）。
  别名 `auto` 翻到默认模型；客户端 model-ref 写法 `minimax/MiniMax-M3.1-Flash-Preview`、
  `minimax/MiniMax-M3` 等也可直接当别名用（spec:518 + 实测）。
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
  > MITM 实测 2026-09-30 已证实：真实响应的 `event:` 行全集 = `content_block_delta` /
  > `content_block_start` / `content_block_stop` / `message_delta` / `message_start` /
  > `message_stop` / `ping`，与 `data[].type` 全集**完全一致**，`unexpected_events` 为空，
  > `message_stop` 出现 1 次，无 `[DONE]`（详见 §10）。
- `usage` 只有 Anthropic 原生四字段：`input_tokens` / `output_tokens` /
  `cache_read_input_tokens` / `cache_creation_input_tokens`（spec:610-623）；
  总量 = 四项相加（spec:619,693）。`message_delta` 可能不带 `input_tokens`，
  此时以 `message_start` 的值兜底（spec:596）。
  实测响应还带一个**嵌套**字段 `output_tokens_details.thinking_tokens`（思考 token，
  是 `output_tokens` 的**子集**）：本通道会提取它并同时给出 OpenAI 风格
  `completion_tokens_details.reasoning_tokens`，但**不**把它加进 `total_tokens`
  （口径不变，详见 §10）。

### 明文 JSON 结论（重要）

请求/响应体是**明文 JSON**，**不存在任何编码、加密、签名层**，也没有 `Encode=1` 之类的开关
（spec:417-487,695）。MITM 实测 2026-09-30 已**证实**：主请求 body 就是可读明文 JSON
（dump-003:51），无任何包装层。因此：

- 不要给这个通道加请求体签名、base64 包装或自定义编码——源端根本没有这些，
  加了只会失败得更难查（spec:695）。
- 唯一真实凭证载体是 `Authorization: Bearer <token>`（spec:194,211,344）。
- `x-api-key: sk-xxx` 是**占位符且必须原样发送**（spec:200-202,343）：Anthropic SDK 由
  `apiKey` 生成该头，缺了可能直接触发 SDK "Could not resolve authentication method"。
  服务端应忽略其值。实测该头确实与 `Authorization` **双头并存**（dump-003:29,32）。
- 静态头（与环境无关、可写死，spec:342-345,690）：`anthropic-version: 2023-06-01`、
  `x-api-key: sk-xxx`、`User-Agent: MiniMaxAgent`、`X-Mavis-Agent-Id: main`，
  以及 MITM 实测补上的 `anthropic-dangerous-direct-browser-access: true`（dump-003:27）。
  逐请求注入的动态头（spec:340-348,690）：`Content-Type`、`Accept`、
  `Authorization`、`X-Mavis-Session-Id`（`mvs_` + 32 位小写 hex）、
  `X-Mavis-Timezone-Offset`（秒，东为正）。
- prod **不要发** `bedrock-lane`（spec:350,690），**也不要**发 `anthropic-beta`：
  实测主请求 `headerPresence["anthropic-beta"] = null`（`capture.jsonl:8`）且 dump 全文无该头。

---

## 3. thinking 是 on/off 开关；推理路径的档位走 `output_config.effort`

Anthropic 方言下 MiniMax 的思考控制是**二值开关**（spec:524-533,691）：

| 语义 | 下发值 |
|---|---|
| 思考 **on** | `thinking: {"type": "adaptive", "display": "summarized"}`（spec:531 + 实测） |
| 思考 **off** | `thinking: {"type": "disabled", "display": "summarized"}`（spec:531,508 + 实测） |

- 合法取值只有 `on` / `off`（`THINKING_MODE_VALUES`，spec:528）。
- `display` 是**伴生字段**：MITM 实测 2026-09-30 在 `count_tokens` 请求里观测到
  `thinking: {"type":"adaptive","display":"summarized"}`（dump-001:67-69 / dump-002:1783-1785），
  on/off 都带。注意目录里的 `options.reasoningSummary:"auto"` 与实测字面 `"summarized"`
  **不是同一个值**，别混用。
- **档位不是 thinking 的取值**：`low|medium|high|xhigh|max` 是 effort 档位，
  经 `output_config.effort` 下发（spec:541-542）。
  > ⚠️ 静态规格当时判断"受管 M3 路径不下发 effort"——**已被 MITM 实测推翻**：
  > 推理请求实测 `output_config: {"effort":"default"}`（dump-003:1822-1823），
  > 且**完全不发** `thinking`（dump-003 全文 0 处该键）。
  > ⇒ 分工是：**推理路径用 `output_config.effort`，`thinking`（带 `display`）只在
  > `count_tokens` 出现**。本通道按实测补齐：没有 `response_format` 时也产出
  > `output_config == {"effort": "default"}`；调用方给了合法档位则透传，非法值/"关思考"词
  > （`none`/`minimal`/…）回退 `default`，绝不原样撞上游。
- 本通道**没有** `supports_reasoning_effort` 能力位，所以管理页「模型配置」里该通道
  显示为「不适用」（README 的思考档位说明只对 WorkBuddy 上游成立）。
  注意：这不等于"上游不吃 effort"——只是管理页不提供该通道的档位下拉；
  客户端显式传 `reasoning_effort` 时本通道仍会映射进 `output_config.effort`。
- M3 目录声明 `thinking_config.mode=switchable`、默认 `true`（spec:507）。
  M2.7 系目录只声明 `reasoning:true`，**没有** `thinking_config`/`variants`（spec:512-515）——
  实现按 M3 的同一套 on/off 映射处理（spec:531 那条映射对 anthropic-messages 通用），
  属**可配置 + 未实测**，见 §8。
- 工具调用方面：推理路径的每个 `tools[]` 元素都带 `eager_input_streaming: true`
  （实测 27/27，dump-003:83），本通道照发；该字段**不依赖** `anthropic-beta` 头
  （实测该头为 null），也只在推理路径出现（`count_tokens` 的同一批工具 0/27 带）。

---

## 4. 额度（credit）：没有查询 API，只能 token 估算

- **没有额度查询接口**。规格全篇未发现任何「把 LLM 用量上报回服务端」或「查询剩余额度」的
  LLM 面接口（spec:663），LLM 响应里也**没有** credit / credits_used / reasoning_tokens 字段
  （spec:610,624）。客户端侧单位成本硬编码为 0（spec:625）。
  > MITM 实测 2026-09-30 已证实：响应头**没有任何 `x-ratelimit-*`**
  > （只有 `x-request-id` / `x-trace-id` / `x-mavis-session-id`，`capture.jsonl:11`）
  > ⇒ 限流解除时刻只能靠错误文案尽力解析，不能指望响应头。
- 因此本通道的 `fetch_quota` **恒返回 `unsupported=True`**（spec:663），
  且 `remaining=None`。注意：跨通道求和会把「不知道」当 0，看板必须**分列**展示。
- credit 统计走**网关侧 token 估算**：`upstream_credit` 取不到值时退回
  `channel_credit_rate("minimax_code")` 的 token 口径（`chat.py` 的日志路径），
  `credit_source` 记为 `estimate`。这是本通道唯一可行的近似，**不是官方锚点**。
- 思考 token 可观测：实测响应 `usage.output_tokens_details.thinking_tokens`
  （样本 `57`，占 `output_tokens` 的 `85` 中约 67%）会被提取成
  `completion_tokens_details.reasoning_tokens`，供日志/看板统一消费。
  它是 `output_tokens` 的**子集**，**不**计入 `total_tokens`（见 §10）。
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

## 8. 已知未验证项（静态逆向无法确认）

以下均出自 spec:703-709（及 710-711），**静态逆向无法确认**，实现按「可配置 + 不编造」处理。
**2026-09-30 的 MITM 抓包回答了其中一部分**，下表逐条标注「已实测」或「仍未验证」
（实测细节与证据行号见 §10）：

| 项 | 状态 | 依据 / 为什么仍不确定 |
|---|---|---|
| 网关是否返回额外的 MiniMax 私有 SSE 事件（被 `ANTHROPIC_MESSAGE_EVENTS` 静默忽略） | **已实测（本次样本内无）** | 实测 `data[].type` 全集 = `event:` 行全集 = 白名单 6 项 + `ping`，`unexpected_events` 为空（`capture.jsonl:11`）。但样本只 1 条消息 ⇒ 只证明"这条路径没冒新事件"，不排除其它特性下冒 |
| 响应是否带 `x-ratelimit-*` 头 | **已实测：无** | 响应头只有 `x-request-id` / `x-trace-id` / `x-mavis-session-id`（`capture.jsonl:11`）⇒ 退避/解除时刻只能靠错误文案解析（`RETRY_AFTER_UNKNOWN`） |
| 请求体是否有编码/加密/签名层（明文可通） | **已实测：明文可通** | 主请求 body 就是可读明文 JSON，无包装层（dump-003:51） |
| 响应是否发 OpenAI 的 `[DONE]` 哨兵 | **已实测：无** | `saw_done_sentinel=false`、`message_stop_count=1`（`capture.jsonl:11`）⇒ 硬结束只看 `message_stop`，`[DONE]` 由本网关出口自行补 |
| `anthropic-dangerous-direct-browser-access` 是否会被发送 | **已实测：会（推理路径必发）** | `dump-003:27` = `true`；两次 `count_tokens` **不发**（属推理路径专属）。本通道已照发 |
| 推理路径的思考档位载体 | **已实测：`output_config.effort`** | 推理请求实测 `output_config:{"effort":"default"}`（dump-003:1822-1823）且**不发** `thinking`；`thinking`（带 `display:"summarized"`）只在 `count_tokens` 出现（dump-001:67-69） |
| 服务端**必填**头/字段（`X-Mavis-*`、`anthropic-version` 是否强校验） | 仍未验证 | 只能看出客户端会发且 200，看不出服务端拒不拒（spec:703） |
| `x-api-key: sk-xxx` 占位符是否被网关忽略 | 仍未验证 | 客户端一定发、且实测与 `Authorization` 双头并存成功（dump-003:29,32），但"服务端是否校验其值"抓包证明不了（spec:705） |
| `Authorization` 之外用 `x-api-key` 传真 token 是否同权（受管 vs BYOK 是否同路径同权） | 仍未验证 | 源码里 `minimax` 与 `minimax_api` 是两个分开的入口（域名/路径不同，spec:706） |
| `/mavis/api/*` 网关是否要求 `yy` / `x-signature`（账号签名是否外溢到 LLM 前缀） | 仍未验证（实测未发且成功） | 实测主请求 24 个头里**没有** `yy`/`x-signature`/`x-timestamp` ⇒ 至少默认路径不需要；但代码只把签名用在 `/v1/api/user/info`，`proxy.ipc.js` 注释暗示签名属 matrix 网关，**属推断**（spec:707） |
| 是否存在 UA / 客户端版本的 **WAF 级校验** | 仍未验证 | 属反爬层而非协议层（spec:709）；单次抓包不足以证伪 |
| access token 真实 TTL 与 refresh token 是否轮转 | 仍未验证 | `expiresAtMs` 是服务端签发值，静态只见客户端读取（spec:704）；本次无 401/刷新样本 |
| `files/upload` 的 multipart 字段名与 file-id 引用格式 | 仍未验证 | 目录可配置，本机未触发上传（spec:710） |
| 429/529 的退避参数与 `retry-after` 头 | 仍未验证 | 仅见本地退避表（spec:711）；本次 3 个响应**全是 200**，无错误样本 |
| 401 / 402 响应体是否含剩余额度 | 仍未验证 | 同上，无错误样本 |
| `/v1/messages` 是否接受 `stream:false` | 仍未验证 | 本次推理请求恒 `stream:true`；`stream:false` 只在 `count_tokens` 观测到（spec:703-708 的未确认项） |
| `output_config.effort` 的**合法值域** | 部分验证 | 只观测到 `"default"` 一个值 ⇒ `low/medium/high/xhigh/max` 是否被接受**未证实**；本通道只白名单透传，非法值绝不原样撞上游 |
| `thinking.display` 在**推理路径**是否被接受 | 仍未验证 | 只在 `count_tokens` 观测到；推理路径根本没发 `thinking`（本通道仍按调用方意图支持它） |
| `tools[].eager_input_streaming` 是否**强校验** | 仍未验证 | 客户端发了且成功；缺该键时工具参数流式行为是否变化未实验 |
| 我方多发的 `temperature`/`top_p`/`stop_sequences`/`metadata`/`tool_choice` 是否被接受 | 仍未验证 | 客户端本次一个都没发 ⇒ 零覆盖 |
| HTTP/2 与 Brotli 是否**必需** | 仍未验证 | 客户端用 h2 + `content-encoding: br` 成功；本通道 h1.1 + gzip 未实验 |
| 其它特性下 `anthropic-beta` 是否会发 | 仍未验证 | 本次默认路径未发，只覆盖"默认不发"这一面 |

**结论**：`X-Mavis-*` 三头 + `anthropic-dangerous-direct-browser-access` 本通道照客户端原样发送
（最坏情况是被忽略，不是被拒），`yy`/`x-signature` **一律不发**（无证据支持，乱发更可能触发风控）。
上表「仍未验证」的条目若要定论，只能再走受控 MITM 抓包，
**不得用真实生产 API 试探/重放/压测**。

---

## 9. 接入步骤（离线可做的部分）

1. 在桌面客户端完成登录，确认 `%USERPROFILE%\.minimax\auth\prod\cn\mcode-public\auth.json` 存在。
2. 启用通道：`CB_GATEWAY_PROVIDERS=workbuddy,...,minimax_code`，或管理页点「启用」。
3. 管理页「通道管理」→ 选 MiniMax Code → 「重新检测」→「一键导入」；
   或直接粘贴 `auth.json` 内容 / 裸 JWT 建号（`parse_credentials` 会做形状适配）。
4. 点该账号的「测试」：返回一句话即说明上游链路通。
5. 模型配置页确认白名单（默认 `MiniMax-M3.1-Flash-Preview` + 原 M3/M2.7 三档）；
   该页的「思考档位」列对本通道不适用（§3）。

> **风控红线**：本通道的全部验证都在离线完成——测试用 `httpx.MockTransport` 假传输
> （`PROVIDER.set_transport(...)`），**严禁向任何 MiniMax 生产 API 发真实请求**
> （不重放、不试探、不压测、不轮询）。

---

## 10. MITM 实测（2026-09-30）

本节记录**一次已结束的受控抓包**的确定结论。抓包规模：**1 条真实推理消息 + 2 次 `count_tokens`，
零重放**。环境已还原（注册表代理回原值、CA 按指纹删除、mitmdump 已停），本节不触发任何新抓包。

> 证据文件（**均在本机 `.tmp/` 下，不随仓库分发**）：
> `.tmp/mitm/minimax-code-20260919/dumps/req-20260930-172559-00{1,2,3}.json`
> （`003` = `POST /v1/messages` 主请求；`001`/`002` = `POST /v1/messages/count_tokens`）
> 与 `.tmp/mitm/minimax-code-20260919/capture.jsonl`（结构化普查记录）。
> 完整逐条对照见同目录的 `MITM-VERIFIED-FINDINGS.md`。
> **dump 里 `authorization` / `x-api-key` 已脱敏为 `***`；本文档与代码、测试均不出现任何真实凭证。**

### 10.1 确定结论

| # | 结论 | 证据 |
|---|---|---|
| 1 | 主请求 = `POST https://agent.minimax.cn/mavis/api/v1/llm/v1/messages`，HTTP/2 + TLSv1.3 | `dump-003:4,5,6,13-15` |
| 2 | 另存在 `POST .../v1/messages/count_tokens`（**仅登记，本通道不实现**） | `dump-001:4`、`dump-002:4` |
| 3 | 主请求必发 `anthropic-dangerous-direct-browser-access: true`，且**无** `anthropic-beta` 配套 | `dump-003:27`、`jsonl:8`（`headerPresence["anthropic-beta"]=null`） |
| 4 | `x-mavis-session-id` 值形如 `mvs_` + 32 位小写 hex，**会话级复用**（三次请求同值） | `dump-003:34`、`dump-001:31`、`dump-002:31` |
| 5 | 主请求**无** `cookie`、**无** `bedrock-lane` 等非 prod 泳道头 | `jsonl:8`（`headerNames` / `nonProdHeaders={}`） |
| 6 | 客户端实际在用模型 = `MiniMax-M3.1-Flash-Preview`，`max_tokens: 128000` | `dump-003:53,68` |
| 7 | 推理路径发 `output_config: {"effort":"default"}`；**不发** `thinking` | `dump-003:1822-1823`；全文 0 处 `"thinking"` |
| 8 | `count_tokens` 反过来：发 `thinking: {"type":"adaptive","display":"summarized"}`，**不发** `output_config`，`stream:false`，无 `max_tokens` | `dump-001:57,67-69`、`dump-002:57,1783-1785` |
| 9 | 推理路径 27 个 `tools[]` **全部**带 `eager_input_streaming: true`；`count_tokens` 同一批 **0/27 带** ⇒ 推理路径专属，且不依赖 `anthropic-beta` | `dump-003:83`（27 处）、`dump-002:67+` |
| 10 | `tools[].input_schema` 形状与本通道映射一致（`type:"object"` + `properties`/`required`）；末位工具挂 `cache_control:{type:"ephemeral"}`（无 `ttl`） | `dump-003`（`input_schema` ×27）、`dump-003:1817-1819` |
| 11 | 请求体是**明文 JSON**，无编码/加密/签名层 ⇒ **明文可通已证实** | `dump-003:51` |
| 12 | 响应 200 + `content-type: text/event-stream; charset=utf-8` + `content-encoding: br` | `jsonl:11` |
| 13 | SSE `event:` 行全集 = `content_block_delta`/`content_block_start`/`content_block_stop`/`message_delta`/`message_start`/`message_stop`/`ping`，与 `data[].type` 全集**完全一致**，`unexpected_events=[]` ⇒ **白名单与实现完全一致** | `jsonl:11` |
| 14 | `message_stop` 出现 1 次，**无 `[DONE]` 哨兵** | `jsonl:11`（`message_stop_count=1`、`saw_done_sentinel=false`） |
| 15 | `stop_reason` = `end_turn` | `jsonl:11` |
| 16 | `usage` 带嵌套 `output_tokens_details.thinking_tokens`（样本 `57` / `output_tokens` `85`）⇒ 本通道已提取，**不计入** `total_tokens` | `jsonl:11` |
| 17 | 响应头**无任何 `x-ratelimit-*`**（只有 `x-request-id`/`x-trace-id`/`x-mavis-session-id`） | `jsonl:11` |
| 18 | `x-stainless-*` / `sec-fetch-*` / `priority` 属 SDK/浏览器运行时元数据，**不必照抄** | `dump-003:36-48` |

### 10.2 本次实测修正的实现点（附回归测试）

| 缺口 | 修正 | 回归用例（`tests/test_minimax_code.py` §11） |
|---|---|---|
| 会话 id 形状（旧值裸 `uuid4()`，带连字符无前缀） | `mvs_` + `uuid4().hex` | `test_session_id_shape_is_mvs_plus_32_hex` |
| 漏发浏览器直连头 | 静态头补 `anthropic-dangerous-direct-browser-access: true` | `test_request_headers_send_dangerous_direct_browser_access_without_beta` |
| 推理路径不发 `effort`；值域缺 `"default"` | 恒发 `output_config.effort`（默认 `"default"`），`format` 与 `effort` **共存不互相覆盖** | `test_build_payload_always_sends_output_config_effort`、`test_build_payload_output_config_format_and_effort_coexist` |
| 实测模型不在目录 | `MiniMax-M3.1-Flash-Preview` 进目录并成为 `DEFAULT_MODEL`，原三档保留 | `test_model_catalog_gains_m3_1_flash_preview_as_default` |
| `thinking` 缺 `display` | on/off 都带 `display: "summarized"` | `test_thinking_carries_display_field` |
| `tools[]` 故意不发 `eager_input_streaming` | 每个 tool 都发（`True`） | `test_tools_all_carry_eager_input_streaming` |
| `thinking_tokens` 被丢弃 | 提取为原生键 + OpenAI 风格 `reasoning_tokens`，**total 口径不变** | `test_usage_extracts_thinking_tokens_without_changing_total` |

### 10.3 残余不确定性（**不得**当成已验证）

1. **样本极小**：1 条推理消息、1 个模型（`MiniMax-M3.1-Flash-Preview`）、零重放。
   「M3 是否也这样发」「其它模型是否同样吃 `effort`」**都未证实**。
2. **只证明"发了且成功"，不证明"不发会被拒"**：`anthropic-dangerous-direct-browser-access`、
   `eager_input_streaming`、`mvs_` 前缀格式、`x-api-key` 占位符值是否被**强校验**，抓包无法反推。
3. **`output_config.effort` 只观测到 `"default"`**：`low/medium/high/xhigh/max` 是否被接受未知
   ⇒ 实现只白名单透传，非法值回退 `default`。
4. **零错误样本**：本次 3 个响应全是 200 ⇒ 429/529/401/402 的真实信封、`Retry-After`、
   额度字段仍未知（错误码映射仍是静态 + 兜底口径，见 §7）。
5. **`thinking.display` 只在 `count_tokens` 出现**：推理路径发 `thinking` 是否合法未证。
6. **HTTP/2 + Brotli 是否必需**未证（本通道未宣告 br，httpx 自动解压）。

> 上述任何一条要定论，**只能再走受控 MITM 抓包**，绝不用真实生产 API 试探/重放/压测。