# TraeWork 通道支持「选择 code 模式」Spec

- 编号：41
- 状态：**已完成（实现 + 单测 + 真实账号端到端实测全部通过）**
- 关联文件：`src/providers/traework/{constants.py, chat.py, __init__.py}`、`src/providers/model_config.py`

## 1. 背景与结论

Buddy2api 的 `traework` 通道（读 `%APPDATA%\TRAE SOLO CN`，协议 `POST /api/remote/v1/chat_sessions` 有状态会话）
当前把会话模式**写死**为 Work 办公模式：

- `constants.py`：`SESSION_MODE = "work"`、`AGENT_ID = "solo_work_lite"`；
- `chat.py::_turn` 建会话时发 `{"mode": "work", "auto_create_project": True, "origin": "web"}`，
  发消息时带 `agent_id` / `agent_type`（均取 `AGENT_ID`）。

TRAE Work 是多模式产品线（Work / Code / Design）。用户希望「在反代调用时选择 **code 模式**」。

**结论（已确证）**：code 模式 = `mode:"code"` **且** `agent_id`/`agent_type` 换成
`solo_agent_lite`；只换 `mode` 不换 agent 不完整。证据见 §3（官方客户端静态逆向，三处互证）。

## 2. 上游事实

| 项 | 值 |
|---|---|
| Session 建会话 | `POST {agent_host}/api/remote/v1/chat_sessions`，body `{"mode": "...", "auto_create_project": bool, "origin": "..."}` |
| 发消息 | `POST {session}/messages`，body 带 `agent_id` / `agent_type` / `model_name` / `query` |
| work 模式对 | `mode="work"` + `agent_id`/`agent_type` = `solo_work_lite`（现状，已实测可用） |
| **code 模式对** | **`mode="code"` + `agent_id`/`agent_type` = `solo_agent_lite`**（§3 客户端实证） |
| design 模式对 | `mode="design"` + `solo_design_lite`（未纳入本次实现） |

> Code 模式涉及仓库感知、cloud 沙箱、SubAgents、Git/PR；本网关当前的答案提取
> （`extract_assistant_text` 只认 `finish` 工具 / text 字段，见 `chat.py`）对 code 会话的兼容性
> **尚无真实账号实测**（凭据已失效，见 §3.5）；客户端 code/work 走同一套 SSE 事件
> （metadata / plan_item / done / error），风险中低。

## 3. 前置证据（已完成：客户端静态逆向 + 权威契约）

抓包（MITM）环境未就绪，改用**更权威的客户端静态证据**：直接分析本机官方客户端
`C:\Usr\Compiler\TRAE SOLO CN`（TraeWork CN / runMode=solo-lite，版本 1.107.1）。
结论来自三处互相印证的独立来源，**不是猜测**。

### 3.1 协议契约文件（权威）

`resources\app\node_modules\@byted-icube\solo-lite\dist\410.66aabbc9.mjs` 里有完整 API 表，
与网关 `constants.py` 完全一致：

```js
"chat.createSession": {method:"POST", path:"/api/remote/v1/chat_sessions"}
"chat.deleteSession": {method:"DELETE", path:"/api/remote/v1/chat_sessions/:chat_session_id"}
```

同文件的 agent 清单（模块 69663）：

| id / agent_id | name | type |
|---|---|---|
| `solo_work_lite` | TRAE Work | `SoloWorkLite` |
| **`solo_agent_lite`** | **TRAE Code** | **`SoloAgentLite`** |
| `solo_agent_remote` | TRAE Code | `SoloAgentRemote` |
| `solo_work_remote` | TRAE Work | `SoloWorkRemote` |
| `solo_design_lite` / `solo_design_remote` | TRAE Design | `SoloDesignLite/Remote` |
| `solo_coder` | TRAE Coder | `SoloCoder` |

**mode → agent 映射表（同文件，权威）**：

```js
o = { [Code]:   { [Local]: SoloAgentLite,  [Remote]: SoloAgentRemote  },
      [Work]:   { [Local]: SoloWorkLite,   [Remote]: SoloWorkRemote   },
      [Design]: { [Local]: SoloDesignLite, [Remote]: SoloDesignRemote } }
s = { [Work]:   {type: SoloWorkLite,   name:"TraeWork"},
      [Code]:   {type: SoloAgentLite,  name:"TraeWork"},
      [Design]: {type: SoloDesignLite, name:"TraeWork"} }
// 默认回退 = SoloWorkLite（与网关当前行为一致）
```

### 3.2 UI 代码佐证（多处独立出现同一三元式）

模块 976 / 177 / 771 等均出现：

```js
mode === InputMode.Code ? AgentType.SoloAgentLite : AgentType.SoloWorkLite
```

### 3.3 真实会话快照佐证（客户端 vscdb）

`%APPDATA%\TRAE SOLO CN\User\globalStorage\state.vscdb` 的
`solo-lite-mode-state-map-<uid>` 含真实 code 会话：

```json
"code": { "session": { "mode": "code", "origin": "lite", "session_type": "side_chat", "env": "local" } }
```

对应的 `...:AI.agent.model.session_selected_model` 记录该会话 agent 为
**`solo_agent_lite`**（work 会话记录为 `solo_work_lite`）。

### 3.4 结论

**code 模式 = `mode:"code"` + `agent_id`/`agent_type` 换成 `solo_agent_lite`。**
只换 `mode` 不换 agent 是不完整的（初版实现只做了 mode）。

### 3.5 活体实测（2026-09-19 已完成）

首次尝试时网关唯一的 traework 凭据已失效：JWT `exp` 未到（2026-10-02）但上游
**所有端点**（chat_sessions / models / UG checkin）均返回 `code:1001`，刷新亦 401；
根因是客户端当时处于登出态（`storage.json` 主凭据键 `iCubeAuthInfo://icube.cloudide` 不存在）。

**用户在客户端重新登录后，实测已完成并全部通过**（上游直连 + 网关端到端，见 §6.5/6.6）：

| 项 | 结果 |
|---|---|
| `mode=work` + `solo_work_lite` 直连上游 | 建会话 200 / 发消息 200 / 回答 `pong` |
| `mode=code` + `solo_agent_lite` 直连上游 | 建会话 200 / 发消息 200 / 回答 `pong` |
| 两种模式 SSE 事件集 | 完全一致（metadata / plan_item / done / token_usage / …） |
| 网关 `/v1/chat/completions`（work / code，非流式+流式） | 全部 200，`pong` |
| 网关 `/v1/responses`（code，Codex 路径） | 非流式 `status=completed`；流式 `response.completed` |

> **完整证据留档（含客户端真实运行日志里的真实会话消息）见
> [`41a-traework-code-mode-evidence.md`](41a-traework-code-mode-evidence.md)。**
> 其中最有力的一条：客户端日志里 code 会话的真实消息记
> `agent_type/agent_id = "solo_agent_lite"`、`agent_name = "SOLO Code"`；
> work 会话记 `"solo_work_lite"` / `"SOLO MTC"`；另有 `solo_work_remote`（远端，未纳入）。


## 4. 实现设计（最简 / ponytail）

**模式做成通道级 settings 项**，仿现有 `model_config.py` 的 reasoning/aliases 做法：

- settings 键：`traework.mode`（`"work"` | `"code"` | 空 = 默认 `"work"`，行为与现状完全一致）；
- `model_config.py` 已有 `channel_session_mode(channel, default="work")` 与 `_validate_session_mode`（初版已实现，保留）；
- `constants.py`：`SESSION_MODE = "work"`、`SESSION_MODE_CODE = "code"`（已有），
  **新增 code 的 agent 常量**：`AGENT_ID_CODE = "solo_agent_lite"`（§3 实证）；
- `chat.py::_turn`：
  - 建 session 的 `mode` = `channel_session_mode(CHANNEL_ID, SESSION_MODE)`（初版已实现）；
  - 发消息的 `agent_id` / `agent_type` 必须**随 mode 联动**（本次要补）：
    mode=code → `solo_agent_lite`；否则 → `AGENT_ID`（`solo_work_lite`）。

设计要点（保证默认零变化）：

- 抽一个纯函数 `agent_id_for_mode(mode) -> str`（放 `chat.py` 或 constants 旁），
  `code` 返回 `AGENT_ID_CODE`，其余一律返回 `AGENT_ID`；`agent_id` 与 `agent_type` 用同一值。
- 未配置 `traework.mode` ⇒ mode 默认 `work` ⇒ agent 仍是 `solo_work_lite`，**请求体逐字节不变**。
- 不改动 session/message 的其余字段与结构（无 project/repo 参数需求，§3 未发现 code 专属字段；
  客户端 code 会话同样只带 `mode` + agent，`auto_create_project` / `origin` 保持不变）。

## 5. 管理页接入（可选，最低限度）

- 「模型配置 → 各平台设置」为 `traework` 加一个模式下拉（工作 / 代码 / 默认）；
- 或仅暴露管理 API `GET/PUT /admin/channels/{channel}/models` 的 `mode` 字段（初版已实现，最小）。

决定点：先做 API（已完成），UI 是否跟进看需求。

## 6. 验证清单

1. 默认（未配置 mode）请求 body 与改造前逐字段一致：create `mode="work"`，
   message `agent_id`/`agent_type` = `solo_work_lite`（回归 goldens：`tests/test_traework.py`）；✅
2. 配置 `traework.mode=work` 行为不变；✅
3. 配置 `mode=code`：create `mode="code"` **且** message `agent_id`/`agent_type` = `solo_agent_lite`（单测断言）；✅
4. 管理 API：`mode` 只接受 `work|code|null`，非法 400；✅
5. **真实账号直连上游**（2026-09-19 客户端重新登录后实测）：
   - `mode=work` + `solo_work_lite`：建会话 200、发消息 200、回答 `pong`；✅
   - `mode=code` + `solo_agent_lite`：建会话 200、发消息 200、回答 `pong`；✅
   - 两种模式 SSE 事件集完全一致（`metadata`/`plan_item`/`done`/`token_usage`/…）；✅
6. **网关端到端**（真实账号 + `/v1/chat/completions` + `/v1/responses`）：
   - work：非流式 `pong`、流式 SSE 正常 `[DONE]`；✅
   - code：非流式 `pong`、流式 SSE 正常 `[DONE]`；✅
   - code + `/v1/responses`（Codex）：非流式 `status=completed` + `pong`；流式 `response.completed`；✅

## 7. 风险与依赖

- ~~上游未公开 code 会话合法参数~~ → 已由 §3 客户端静态逆向确证（mode+agent 联动）；✅
- ~~活体实测受阻~~ → **2026-09-19 客户端重新登录后已完成真实账号实测**（见 §6.5/6.6，全部通过）；✅
- code 模式返回事件结构已实测与 work 一致，`extract_assistant_text` 通用；✅
- 本改动默认关闭、零迁移风险：未配置时行为与现状完全一致。✅
