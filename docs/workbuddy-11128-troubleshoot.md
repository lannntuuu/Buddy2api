# workbuddy 11128 拦截排查与解决速查

> 现象：ZCode 等 agent 客户端请求 workbuddy(hy3 等) 返回
> `{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}`（HTTP 400）。

## 一句话结论
本问题已由网关内置的**请求体精简（仅 ZCode Client）**自愈解决，开箱即用、无需手动配置。

**根因**：workbuddy 上游会按「请求体里特定内容」返回 11128，而非简单按长度/条数。实测触发源：
1. **`system` 消息内容里的特定块**（如 git status / commit 历史 / 本机路径）——是**内容特征**，非纯长度。
2. **`tool_calls[].arguments` 被截断成非法 JSON**——网关旧版总量兜底的 bug（把结构 JSON 当普通文本截短）**反而制造了 11128**，已修复为跳过 arguments。

---

## 处理逻辑：仅 ZCode Client 参与精简（关键）
- **只有客户端识别为 `zcode` 的请求**才会被精简。**DSH 及其它 agent / curl / 空客户端一律不精简**，即使同一 workbuddy 通道命中过 11128。
- **默认不预截断**：正常请求零干预；非 ZCode 客户端完全不受影响。
- 某 **ZCode 请求**在 workbuddy 通道**真返回过一次 11128** 后，该 `(通道, 客户端=zcode)` 组合被「武装」（armed）：该 ZCode 请求在转发失败后原地精简重试（自愈）。
- 精简只做**单字段纯头切**（实测 11128 是「内容特征」触发，不是总量——system 截短后 body 700KB 也能通过；总量兜底把 content 无脑压成碎片，已废弃）：
  1. **`system` 单独精简**（阈值 `5000`，`CB_GATEWAY_COMPACT_SYSTEM_CHARS`）：**纯头部截断**——其尾部（git status / commit 历史块）正是 11128 触发源。
  2. **普通消息 content / reasoning_content**（阈值 `3000`，`CB_GATEWAY_COMPACT_ARMED_CHARS`）：超大的纯头切。
  3. **`tools` 定义**里的超大 `description`（阈值同上）：截短描述文本，不碰结构键。
- **`tool_calls[].arguments` 永不截断**——结构化 JSON，截断即破坏语法，反而触发 11128。
- 精简失败后仍 11128 不会空转重试。

### 环境变量
| 环境变量 | 作用 |
|---|---|
| `CB_GATEWAY_COMPACT_ARMED_CHARS` | ZCode 触发 11128 自愈后的单字段阈值，默认 `3000` |
| `CB_GATEWAY_COMPACT_SYSTEM_CHARS` | system 消息阈值（纯头部截断，仅 ZCode），默认 `5000` |
| `CB_GATEWAY_COMPACT_CHARS` | 强制启用精简并指定阈值（仍**仅对 ZCode Client 生效**）。不设则走按 (通道,客户端) 自愈 |
| `CB_GATEWAY_COMPACT_CHARS=0` | 关闭精简（不推荐） |

例：`$env:CB_GATEWAY_COMPACT_CHARS="8000"` 后重启，仅对 ZCode Client 强制精简。

### 如何确认精简在生效（可观测）
- 打开管理页 → 「总览」（`/admin/stats`），末尾新增 `compaction` 字段：
  `compacted_messages`（累计精简条数）、`armed_keys`（已武装的 (通道,客户端) 组合数）、`armed_triggers`（ZCode 触发过 11128 的次数）、`retried_11128`（触发后重试成功的次数）、`enabled_clients`（精简生效的客户端列表，应为 `["zcode"]`）。
- `armed_keys` 从 0 变 1 说明某 ZCode 请求触发过一次 11128 并已激活精简。
- `compacted_messages` 持续增长 = 精简真实在切；若长期为 0，说明 ZCode 正常请求都未超过阈值，无需调参。

---

## 快速处理（如果是同类问题）

### 1. 先确认是否已生效
- 网关版本需含「content 精简」逻辑（`upstream/proxy.py` 的 `_smart_compact_messages`、`_is_11128_error`）。
- 重启网关：`python -m gateway.server`（**无需任何环境变量，自愈默认开启**）。
- ZCode 里**堆积出的长会话**（非新会话）重试一次即可——重试时若上游返回 11128，网关会自动精简并再试。

### 2. 想调阈值 / 临时关闭 / 观察是否生效
参数和开关见上文「现在的处理逻辑」（`CB_GATEWAY_COMPACT_CHARS` / `_ARMED_CHARS` / `_SYSTEM_CHARS`），生效情况看 `/admin/stats` 的 `compaction` 字段。

---

## 症状判定：是不是「请求体过大」触发的 11128
- ✅ 新会话（无大历史）能通，长会话/大历史才 11128 → **是**
- ✅ 同一 body、把超大 content / tools / 参数换成短文本后能通 → **是**
- ✅ 换模型、换客户端、换通道(仍 workbuddy)都一样 → 别瞎改这些，问题在请求体体量
- ❌ 新会话也 11128、或换到其它通道(如 traesolo)也失败 → 不是本问题，另查

真实触发案例（实测 profile）：`333 条消息 / 162 条工具结果 / tools 定义 ~105KB / 单条 reasoning_content 25KB / 请求体 ~600KB`。

---

## 排查中已确认的触发因素（重点看这些）
- ✅ **`system` 消息内容里的特定块**（git status / commit 历史 / 本机路径等）——靠 system 纯头部截断（5000）
- ✅ **`tools` 定义体积**大（上百个工具、各自较长的 `description`）——靠 tools 描述精简
- ✅ **单条 `reasoning_content`** 超大（assistant 思维链，如 25KB）——靠 reasoning_content 头切
- ✅ **超大 `content`**（tool 结果 / 长文本）——靠 content 头切
- ⚠️ **`tool_calls[].arguments` 被截断成非法 JSON**（旧版总量兜底 bug）——**会制造 11128**，已废弃总量兜底，arguments 永不截断

## 排查中已排除的因素（别再走冤枉路）
- ❌ max_tokens 过大（钳制无效）
- ❌ 客户端、UA、headers、账号被标记（网关转发到 workbuddy 的出站请求对任何客户端是同一套）
- ❌ **纯长度**触发：`'S'*20000` 这种无意义重复字符的 system 不触发 11128（200 通过），说明是**内容特征**而非长度
- ❌ **总量**触发：system 截短后 body 700KB 也能通过（真实上游验证 200），11128 不是总量超限，总量兜底只会把 content 压成碎片

> 早前文档曾断言「system 永不精简」「大 body 能通」「总量兜底必要」，这些在**更长/更膨胀的历史会话**里已被推翻——system 尾部内容块正是 11128 触发源，总量兜底反而制造问题。诊断日志（见下）打印的 `_body_size_profile` 能精确定位触发源。

---

## 诊断：怎么看到触发源
网关在「自动精简后仍失败」时打印一条 WARNING：`11128 self-heal retry still failed profile={...}`。
profile 字段含义：
- `body_bytes` / `msg_bytes` / `tools_len` —— 请求整体、消息、tools 各自字节
- `max_content` / `max_field` —— 单条最长字段及所在位置（如 `m[system].content`）
- `assistant_args_bytes` / `tool_content_bytes` —— 工具参数 / 工具结果的累计字节
- `messages` / `tool_msgs` —— 消息与工具结果条数

看这条日志即可判断哪类字段超阈值，据此调 `CB_GATEWAY_COMPACT_SYSTEM_CHARS` / `CB_GATEWAY_COMPACT_ARMED_CHARS`。

---

## 相关：11134（独立问题，非 11128）
排查中发现 workbuddy 上游还会对**另一类内容**返回 `{"code":11134,"msg":"the model provider is temporarily unavailable"}`（HTTP 500）：
- **触发源**：`tool_calls[].arguments` 里的**复合 shell 命令**（如 `cd "C:/绝对路径" && find ... && cat ...` 这种多命令串联）。
- **已确认**：把 arguments 换成无害内容（`{"command":"echo dummy"}`）→ 200；保留命令仅换路径 → 仍 11134。所以是**命令内容**触发，不是路径。
- **与 11128 无关**：11134 是独立的上游策略拦截，网关当前**不自动处理**（避免误伤正常工具调用）。若频繁出现，需从客户端侧避免在工具参数里发复合 shell 命令。

## 相关：不完整 tool arguments（hy3 长时间流式偶发）
hy3 上游在**长时间流式输出**（30s+）后，偶尔会返回**尾部被截断的工具调用 arguments**（JSON 不完整）。网关旧行为是直接报 502 `The upstream tool call stream ended with incomplete JSON arguments.`——流式已输出内容无法重试，客户端（如 ZCode）会看到「部分输出 + 错误」并报 `Partial assistant output was discarded before a streaming retry`。

- **已修复**：网关在 EOF 校验时用 `_repair_json_arguments` 尝试补全截断的 arguments（补 `}` / `]` / `"`），补全后能解析成合法 JSON 对象就按修复值透传，工具调用继续执行；补不动才报错。流式（`_ChatStreamObserver`）与非流式（`_collect_stream`）两条路径都已覆盖。
- 这是**上游偶发**问题（前后请求都 200），不是网关精简 body 导致；修复是防御性的，正常完整 arguments 不受影响。

---

## 各通道差异（背景参考）
| 通道 | 上游 API | 是否有本问题 |
|---|---|---|
| workbuddy | `copilot.tencent.com/v2/chat/completions` | ✅ 触发 11128 / 11134 |
| traesolo / traework / qwenwork / qclaw | 各自后端(如 SOLO `trae.cn`) | 通常无此问题 |

---

## 涉及文件
- 修复：`upstream/proxy.py` → `_smart_compact_messages`、`_compact_tools`、`_compact_schema_descriptions`、`_repair_json_arguments`、`_is_11128_error`、`_arm_channel`
- 诊断：`upstream/proxy.py` → `_body_size_profile`（打印在 11128 自愈仍失败的 WARNING 里）
- 调用点：`upstream/proxy.py` → `build_backend_body`（强制档）、`proxy_chat_completions` / `_stream_upstream`（自愈重试）
- 统计：`gateway/server.py` → `/admin/stats` 的 `compaction` 字段

## 注意
- 精简只影响**转发给上游的本次出站请求**，不回写会话存储。
- 只截短纯文本字段，**不删消息**、不破坏 `tool_calls`/`tool_call_id` 引用、不改变工具调用契约（名称、参数结构、`required` 等结构键不动）。
- **`tool_calls[].arguments` 永不截断**（结构化 JSON，截断即破坏语法，反而触发 11128）。
- **`system` 消息只在超阈值时纯头部截断**（默认 5000），正常大小不动。
- 精简是**按 (通道, 客户端) 记账 + 仅 ZCode**的：某 ZCode 请求真触发过 11128 才为该组合开启；DSH 及其它客户端一律不精简，避免无谓丢失信息。
