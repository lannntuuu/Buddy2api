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
- `_turn` 建会话时，`mode == "code"` 则在 body 里带 `initial_message: {"is_in_code_mode": True}`；
- 发消息时，`mode == "code"` 则在 body 顶层带 `is_in_code_mode: True`；
- work 模式**不发送**该字段（与官方一致，避免改变现有行为）。

### 3.2 R3：思考走 `reasoning_content`（流式）——**未实施，待决策**

- `on_thinking` 转发时改为 `sse({"reasoning_content": piece})`，不再塞进 `content`；
- 保留"最终回答若已在思考里则不重复发"的判断；
- 严格 OpenAI 客户端由此能把思考与正文分开渲染。

**为何未做**：这是**行为可见的破坏性变更**——现有客户端（含本机在用者）可能已习惯
"思考文本先到"的表现；改成独立字段后，不渲染 `reasoning_content` 的客户端会**看不到任何
中间输出**（首包变成空的 role 帧，之后长时间静默直到最终答案），TTFB 体验反而变差。
需要用户先确认目标客户端是否支持 `reasoning_content`，故本轮不动。

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
- 非流式在拿到思考时补 `reasoning_content`（若 3.2 已实现，则统一由同一处产出）。

### 3.5 R1：入参保真（**需用户决策，本次不实施**）

`query` 的载荷模型是"单轮对话"，**历史上文由服务端会话持有**。网关每请求新建并删除会话，因此要恢复多轮
上下文，只有两条路：

- **方案 A（保守）**：只做 `3.1`–`3.4`，不碰会话生命周期。多轮仍失忆，但 code 模式、用量真值、
  终止语义都能拿到。**本轮即采用 A。**
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
| R1 入参保真 | ⏸️ 未实施（方案 B，待决策） |
| 测试设施缺陷 | ✅ 已修复 |
| 全量单测 | ✅ `782 passed in 39.75s` |

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

结果：`44 passed in 2.80s`（原先需 ~37 分钟超时）；全量 `771 passed in 39.04s`。

---

## 5. 文档同步

- `docs/traework-usage.md` §4.4 补充：code 模式的实际生效条件（含 `is_in_code_mode`）；
- 新增一节说明"多轮上下文/ system 不生效"的既有边界，避免用户误判为故障；
- `docs/credit-and-token-tracking.md` 中"TraeWork 上游不报 token"的结论需按 §3.3 更正。

---

## 6. 验收

1. 单测：`tests/test_traework.py` 覆盖 code 模式带 flag（两处）、work 模式不带 flag（两处）、
   `token_usage` 解析回填（含三列/credit 落库、且不污染正文）、`finish_reason` 映射、
   `_new_piece` 重叠片段。**55 passed**。
2. 全量：`pytest tests -q` → **782 passed，4 deselected**。
3. 文档：`docs/traework-usage.md` §4.4 补充 code 模式生效条件，新增 §4.5 已知限制；
   `docs/credit-and-token-tracking.md` §2 表格与 §6 按实际实现更正。
4. **不做**真实联网验证（见 §1.1 安全边界）；如需实测，由用户在 prod(:8788) 侧自行触发。
5. 既有契约测试未被削弱：`test_provider_log_wrappers_route_through_shared` 明确要求
   "无 usage 时不得多传 token kwargs"，实现据此改为条件透传而非无条件传。
4. **不做**真实联网验证（见 §1.1 安全边界）；如需实测，由用户在 prod(:8788) 侧自行触发。
