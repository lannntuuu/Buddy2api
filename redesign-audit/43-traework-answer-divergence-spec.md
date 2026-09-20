# 43. TraeWork 回答与其他通道不一致：根因分析与修复方案

状态：R2/R4/R5/R6 已实施并有单测覆盖；R1（入参保真）、R3（思考分离）待用户决策
范围：`src/providers/traework/**`、`tests/test_traework.py`、`docs/traework-usage.md`
前置：`41-traework-code-mode-spec.md`（code 模式接入）、`42-traework-auto-logout-spec.md`（凭据自愈）

---

## 0. 一句话结论

TraeWork 的"回答不一样"**不是** code 模式没生效，而是四条结构性原因叠加：

1. **入参只发了最后一句用户话**：system、多轮历史、tools、温度等全部丢弃；
2. **code 模式的真正开关没发**：官方客户端靠 `is_in_code_mode` 标记（不是只靠 agent 名），网关从未发送；
3. **思考文本被当成正文 content 推给客户端**：其他通道都走 `reasoning_content`；
4. **上游有 `token_usage` 真值却被丢弃**：`_SKIP_EVENTS` 把它跳过，日志恒记 0。

另发现一个**测试设施缺陷**：`_FakeTraeStream` 契约过期，导致 44 个用例各硬等 90s 超时（表现为"卡死"），已修复。

---

## 1. 证据与安全边界（重要）

### 1.1 安全边界

`traework` 的 `refresh_token` 是**一次性轮换票据**，与官方客户端、prod(:8788) 共用同一条链。任何一次真实
`ExchangeToken` 都会消耗旧票并使其他持有方失效。因此本次分析**全程零网络**。

当前实测状态（只读查库）：

| 实例 | 端口 | `enabled_channels` | `traework.mode` | 结论 |
|---|---|---|---|---|
| dev | 8787 | `["workbuddy"]` | `code` | traework **已停用**，且无 traework 账号 |
| prod | 8788 | `["workbuddy","qodercn","traework"]` | 未设置 → `work` | 唯一保活方，账号 id=17，access 有效期至 2026-10-03 |

**推论**：dev 侧改代码不会碰 8788（prod 从 `Buddy2api-prod` 独立部署、独立 DB）；但**不允许**在 dev 侧对同一
账号发真实请求做验证。

### 1.2 官方客户端契约（反编译 `solo-lite/dist`）

`is_in_code_mode` 注入点（`976.f593cb93.mjs`，两处适配器 HttpTransportAdapter / RustAdapter）：

```js
async applyCodeModeFlagIfNeeded(e){try{let t=`${e.service}.${e.method}`;
  if("chat.sendMessage"!==t&&"chat.createSession"!==t)return;
  await (0,B.f)();let o=(0,B.L)(),r=e.params;
  if(o) if("chat.createSession"===t){let e=r.initial_message??{};e.is_in_code_mode=!0;r.initial_message=e}
       else r.is_in_code_mode=!0
}catch(e){...}}
```

即：**Code 模式下，createSession 把 `is_in_code_mode=true` 放进 `initial_message`，sendMessage 放在顶层。**
网关两处都没发（`chat.py:282-288`、`chat.py:354-361`），所以即便 `mode=code` + `solo_agent_lite` 都对，
上游仍按普通 work 会话处理。

官方 sendMessage 体（`410.66aabbc9.mjs`）还带这些网关没有的字段：

```js
{chat_session_id, content:[], query, model_name, agent_type, agent_id,
 model_selection_strategy, custom_model, common_params, force_new_turn, use_fast_request}
```

agent 注册表确认 mode→agent 映射（网关已正确）：`work→solo_work_lite`、`code→solo_agent_lite`、
`design→solo_design_lite`。

事件枚举（`410.66aabbc9.mjs`）共 30 种，含 `token_usage`、`text_message`、`fee_usage`、
`context_usage`、`context_token_usage` 等。客户端对 `token_usage` 的消费：

```js
case"token_usage":{let e=r;this.recordEvent(i,"tokenUsage",{input:e?.input_tokens,output:e?.output_tokens});break}
case"context_usage":{...contexts?.length...}
```

**即上游确实回报 token 真值**——这与代码注释"TraeWork 上游不回报 token"矛盾。

### 1.3 入参保真度实测（零网络差分）

脚本 `.tmp/probe/diff_context_loss.py`，同一份 OpenAI 请求分别走 traework 与 qwenwork：

| 入参 | traework | qwenwork（兄弟通道） |
|---|---|---|
| system prompt | **丢弃** | 保留（`body["system"]`） |
| 多轮历史 | **丢弃**（只留最后一条 user） | 保留（`body["messages"]`，3 条） |
| 图片 part | **丢弃** | 保留 |
| tools | **丢弃** | 保留 |
| temperature/top_p/max_tokens | **丢弃** | 保留（`body["parameters"]`） |

traework `_last_user_text()` 只挑 `role=="user"` 的最后一条，然后把它塞进
`query=[{"type":"text","data":{"content":prompt}}]`，`content:[]`。

---

## 2. 根因清单（按影响排序）

| # | 根因 | 位置 | 影响 |
|---|---|---|---|
| R1 | 只发最后一条 user 文本；system/历史/tools/参数全丢；每请求新建并删除会话 | `chat.py:_last_user_text` / `_turn` | 无上下文、不可控、多轮失忆 |
| R2 | 未发送 `is_in_code_mode` | `chat.py:282-288`、`354-361` | code 模式形同虚设 |
| R3 | 思考文本以 `content` 推给客户端 | `chat.py:576-580,613,635` | 正文混入思维链，先"自言自语"再给答案 |
| R4 | `token_usage` 在 `_SKIP_EVENTS` 中被丢 | `chat.py:159-170` | 用量恒 0，credit 无法估算 |
| R5 | `finish_reason` 恒 `stop`；非流式无 `reasoning_content` | `chat.py:205-219` | 客户端无法感知截断/工具终止 |
| R6 | `_new_piece` 仅按前缀增量，可能重复或吞字 | `chat.py:534-542` | 极端情况下正文重复/缺失 |

---

## 3. 修复方案

### 3.1 R2：补发 `is_in_code_mode`（最高优先级，改动最小）

- `constants.py` 增加 `CODE_MODE_FLAG = "is_in_code_mode"`；
- 发消息时，`mode == "code"` 则在 body 顶层带 `is_in_code_mode: True`；
- work 模式**不发送**该字段（与官方一致，避免改变现有行为）。

> **更正（见 §7）**：早期实现还往 `createSession` 里塞了
> `initial_message: {"is_in_code_mode": True}`，**这是错的，已移除**。
> 官方 `initial_message` 是 `buildSendMessageRequest()` 的完整产物，
> `applyCodeModeFlagIfNeeded` 只是往已存在的对象里补键；本网关首轮走独立
> sendMessage，桩对象会被上游当作"待发消息"解析而报 500。

### 3.2 R3：思考走 `reasoning_content` —— **已实施（按用户指示对齐其它通道）**

用户指示：这两个问题（R3/R1）"看下其他在用的通道就好了"，即以兄弟通道行为为准。

参考实现（实测读取）：

| 通道 | 做法 | 位置 |
|---|---|---|
| qodercn | `delta_out["reasoning_content"] = reasoning` | `chat.py:329-332` |
| traesolo | `delta["reasoning_content"] = ev["reasoning_content"]` | `chat.py:507-508` |
| qwenwork | 收集后 `message["reasoning_content"]` | `chat.py:291-292,306-307` |
| workbuddy (proxy) | 收集后 `message["reasoning_content"]` | `proxy.py:1011-1012,1071-1072` |
| qclaw | 仅当 content 为空时把思考**兜底提升**为 content | `chat.py:41-54` |

**结论：标准做法是思考走独立字段**；只有 qclaw 因上游可能不返回正文才做兜底提升。

实施：
- 流式：`on_thinking` 转发改为 `sse({"reasoning_content": piece})`，最终答案仍走 `content`；
- 非流式：`_turn` 累计思考并返回，`_openai_json` 在 `reasoning != text` 时写
  `message["reasoning_content"]`（正文来自思考兜底时不重复填）。

**关键回归修复**：旧代码有一句"最终回答若已包含在转发过的内容里则不重复发"的守卫，是
为**旧行为**（思考混在 `content` 里）防重复用的。思考移到 `reasoning_content` 后二者不再
共用通道，继续沿用该守卫会导致：**不渲染 `reasoning_content` 的客户端完全收不到回答**。
现改为与 qodercn/traesolo 一致——`content` 与 `reasoning_content` 各自独立转发，不跨通道去重
（原先的 `emitted` 列表随之成为死代码，已删除）。

### 3.3 R4：解析 `token_usage` 真值

- 把 `token_usage` 移出 `_SKIP_EVENTS`，单独解析 `input_tokens` / `output_tokens`；
- 透传到非流式 `usage`、流式末帧 `usage`，并传入 `_log(usage=...)`；
- 字段缺失时仍退回 0（保持向后兼容）。

**实现补充（关键）**：`store_common.log_request` 的 `prompt_tokens` / `completion_tokens` /
`total_tokens` 三列读的是**独立 kwargs**，不会从 `usage` 推导，而 `credit` 只由 `total_tokens`
计算。因此 `_log` 必须在拿到真值时**额外**把 usage 拆成这三个 kwargs 透传，否则 `usage_json`
有真值而三列与 `credit` 恒 0（与 qclaw/qwenwork 不一致）。无 usage 时一个都不传，保持旧行为——
该"无 usage 不得多传 kwargs"的约束由既有契约测试
`tests/test_perf_providers.py::test_provider_log_wrappers_route_through_shared` 锁定。

### 3.4 R5/R6：终止语义与去重

- `finish_reason` 依据上游 `done.status` 映射（`completed→stop`、失败→`error`）；
- `_new_piece` 增加"非前缀重叠"保护；
- 非流式在拿到思考时补 `reasoning_content`（已随 3.2 一并实现）。

### 3.5 R1：入参保真 —— **已实施（压平转发，方案 A）**

用户指示以兄弟通道为准。兄弟通道**全部转发 system + 完整历史**：

| 通道 | 做法 | 位置 |
|---|---|---|
| qwenwork | `_split_messages` 取 system + 转发完整 `messages` + `tools` | `chat.py:77-95,113-114,161-163` |
| qodercn | 同上 | `chat.py:91-109,187-189` |
| qclaw | `body = dict(payload)` 整体透传 | `chat.py:100-104` |
| traesolo | 转发完整 `messages`（含 assistant tool_calls） | `chat.py:219-263` |

TraeWork 上游不接受 `messages` 数组，只接受一条单轮 `query` 文本，故采用**压平**：把
system 与全部历史按空行拼接进 `query`，效果上等价于兄弟通道转发 `messages`。

**硬约束（已用测试锁定）**：无 system 且仅一条 user 消息时，发送文本与该 user 原文
**逐字节相同**——不做 strip、不加前缀/标记。实现中发现并修复了两处偏差：

1. 早期实现先 `.strip()` 再返回，导致用户文本首尾有空白时**不再逐字节一致**；
2. `has_user` 早期写成"turns 非空"，于是**只有 assistant/tool 轮的请求不再回 400**
   （校验被放宽）；已恢复为"存在非空 user 消息"，与改造前 `_last_user_text` 语义一致。

仍**未做**方案 B（按会话复用上游 session）。
- **方案 B（彻底）**：按客户端会话 id 复用上游 session（映射 OpenAI 会话 ↔ `chat_session_id`），并转发
  system/历史/tools/参数。改动大、涉及会话池与清理，且需与"每请求独立、无状态"的既有降级语义
  重新对齐，风险高。

**建议先做 A**，把 B 作为独立 spec。

### 3.6 实施结果（本次）

| 项 | 状态 |
|---|---|
| R2 code 模式 flag | ✅ 已实施 + 单测 |
| R3 思考分离 | ⏸️ 未实施（见 §3.2，需先确认客户端能力） |
| R4 token_usage 解析 + 三列/credit | ✅ 已实施 + 单测 |
| R5 finish_reason 映射 | ✅ 已实施 + 单测 |
| R6 `_new_piece` 重叠保护 | ✅ 已实施 + 单测 |
| R1 入参保真（压平转发 system + 历史） | ✅ 已实施 + 单测（方案 B 会话复用仍未做） |
| 测试设施缺陷 | ✅ 已修复 |
| 全量单测 | ✅ `794 passed, 4 deselected` |

### 3.7 实施过程中额外发现并修复的回归（第二轮）

除 §3.5 记录的 `_build_prompt` 两处偏差外，R3 落地时还发现一处**严重回归**：

- `_stream_chat` 里"最终回答若已包含在转发过的内容里则跳过"的守卫，原本是为**旧行为**
  （思考混在 `content`）防重复用的。思考改走 `reasoning_content` 后，若答案文本恰是思考的
  子串（例如上游思考里出现 "pong"、而最终答案就是 "pong"），该守卫会**把回答整个吞掉**——
  不渲染 `reasoning_content` 的客户端将**完全收不到回答**。
- 已按 qodercn/traesolo 语义改为：`content` 与 `reasoning_content` 独立转发、不跨通道去重；
  随之删除已无读取方的 `emitted` 列表。
- 原测试 `test_stream_chat_no_duplicate_answer` 曾把该错误行为固化为断言
  （`assert "pong" not in contents`），已改写为
  `test_stream_chat_answer_always_sent_as_content_even_if_same_as_thinking`，
  断言答案**必须**出现在 `content` 中。

---

## 4. 顺带修复：测试设施缺陷（已完成）

`tests/test_traework.py` 的 `_FakeTraeStream.__aenter__` 是同步方法且返回 `self`，而生产代码是：

```python
async with client.stream("GET", ...) as response:
    if response.status_code >= 400: ...
    async for line in response.aiter_lines(): ...
```

fake 既不是 awaitable、也没有 `aiter_lines`，`read_events` 抛的是 `TypeError`/`AttributeError`
（**不是** `httpx.HTTPError`，不会被吞），于是 `finished` 永不置位，每个用例都得硬等满 `timeout=90.0`。
44 个用例表现为"整个文件卡死"。

修复：`_FakeTraeStream.__aenter__` 改 `async def` 并返回带 `status_code` 与 `aiter_lines()` 的响应对象。

结果：`44 passed in 2.80s`（原先需 ~37 分钟超时）。

---

## 5. 文档同步

- `docs/traework-usage.md` §4.4 补充：code 模式的实际生效条件（含 `is_in_code_mode`）；
- §4.5 改写为「上下文与思考的呈现方式」：说明 system + 历史已压平转发、思考走
  `reasoning_content`、以及仍然不生效的部分（tools / 采样参数 / 图片 / 跨请求记忆）；
- `docs/credit-and-token-tracking.md` 中"TraeWork 上游不报 token"的结论已按 §3.3 更正。

---

## 6. 验收

1. 单测：`tests/test_traework.py` 覆盖 code 模式带/不带 flag、`token_usage` 解析回填
   （含三列/credit 落库、不污染正文）、`finish_reason` 映射、`_new_piece` 重叠片段、
   R1 压平（逐字节一致 / system+历史 / 多 system / 图片忽略 / 缺 user 轮 400）、
   R3（流式 reasoning_content 与 content 分离、非流式 reasoning_content、兜底不重复）、
   以及 §7 的两条回归（不得发 `initial_message`、`auto` 保留字兜底）。
2. 全量：`pytest tests -q` → **795 passed，4 deselected**。
3. 文档：§4.4 / §4.5 与 credit 文档均已按实现更新。
4. **不做**真实联网验证（见 §1.1 安全边界）；如需实测，由用户在 prod(:8788) 侧自行触发。
5. 既有契约测试未被削弱：`test_provider_log_wrappers_route_through_shared` 明确要求
   "无 usage 时不得多传 token kwargs"，实现据此改为条件透传而非无条件传。

---

## 7. prod(:8788)「测试」失败的根因与修复（第三轮）

用户反馈：prod 分支跑的项目里 traework「测试」通过不了。零网络排查结论如下。

### 7.1 先排除的项（都不是原因）

| 假设 | 证据 | 结论 |
|---|---|---|
| 凭据失效 | prod 库里 token 是 Fernet **密文**；用 prod 自己的 `credentials.key` 解密后与客户端 `storage.json` **逐字节一致**，access 有效期至 2026-10-03 | 排除 |
| prod 代码落后 | prod 已含前两轮全部提交（`git merge-base --is-ancestor` 三个提交均为真） | 排除 |
| 别名表内容 | 两侧都含 `qwen-3.7-plus` / `DeepSeek-V4-Flash-Official`，白名单不是瓶颈 | 排除 |
| host override | 两侧 `channel_hosts` 都未设置 | 排除 |

### 7.2 真正原因（两个独立缺陷叠加）

**原因 A：`createSession.initial_message` 桩对象（本 spec §3.1 早期实现的错误）**

上一轮我依据 `applyCodeModeFlagIfNeeded` 写了 `initial_message = {"is_in_code_mode": True}`。
但官方客户端里这个字段是 `buildSendMessageRequest()` 的**完整产物**：

```
R = buildSendMessageRequest(...)     // 含 chat_session_id/content/query/model_name/agent_id/...
createSession(en({..., initial_message: R, ...}))
```

而 `applyCodeModeFlagIfNeeded` 只是往**已存在**的对象里补键
（`e = r.initial_message ?? {}; e.is_in_code_mode = !0`），从不凭空创建。

本网关的架构是「建会话 → 另发一次 sendMessage」，首轮并不走 `initial_message`。
塞一个只有 flag 的桩对象，等于告诉上游"这里有一条待发消息"，而它缺 `query`/`model_name`，
上游按 500 `internal server error` 返回。**这与模型名无关**：prod 唯一一条真实请求
（`logs id=34009`）用的就是合法模型 `DeepSeek-V4-Flash-Official`，照样 503。

时间线印证：代码 21:18 落盘，进程 22:59 导入新代码，23:00 那次请求即 500——此前
18:00 用同样模型是 200。

**修复**：删掉 `initial_message` 注入，code 语义只由 sendMessage 顶层标记表达
（那正是官方 `chat.sendMessage` 分支的做法）。`work` 模式行为不变。

**原因 B：保留字 `auto` 被原样透传给上游（配置 + 代码双因素）**

管理页「测试」按钮**硬编码** `{model:'auto',prompt:'ping'}`（`channels.js:318`）。
而 `channel_aliases()` 的语义是「设置键存在即**整体替换**内置默认」：

```
dev : traework.aliases = {"auto": "qwen-3.7-plus"}                    → translate_model("auto") = "qwen-3.7-plus" ✅
prod: traework.aliases = {"DeepSeek-V4-Flash-Official": "同名"}       → translate_model("auto") = "auto"          ❌
```

`"auto"` 于是被当作 `model_name` 发给上游。官方客户端**从不这样发**：
`getModelRequestSelection` 在 Auto 策略下返回 `modelName:""`（空串），具体模型才给 id。
`"auto"` 是 UI 保留字，不是合法的线上取值。

**修复**：`make_translator(..., reserved=...)` 新增兜底——映射结果仍是保留字时回退到内置
具体模型；管理员显式配了 `auto` 时仍以管理员为准（不改既有优先级）。`reserved` 默认 `None`，
其余通道行为完全不变。

### 7.3 为什么会有"dev 能过、prod 不能"的错觉

dev 的 `enabled_channels = ["workbuddy"]`——**traework 根本没启用**，在 dev 点「测试」会在
`get_provider` 处直接 400（`Channel 'traework' is not enabled`），压根到不了 traework 代码。
所以这不是"dev 过 / prod 不过"，而是两个实例的配置不同。真正相关的差异只有两处：
`enabled_channels` 与 `aliases` 表。

### 7.4 附带发现：测试路径不落日志

`chat.test_chat` 与 `store_common.run_test_chat` **都不写 logs 表**，只有 `_run_turn` 写。
所以「测试」失败在日志页查不到任何痕迹——这正是本次排查一开始缺证据的原因。
（已记录为待办，本轮未改，避免扩大改动面。）
