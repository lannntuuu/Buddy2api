# 17b — hy4-preview 复现与定位（Phase B）

> 分支：`fix/hy4-preview-usage`
> 执行方式：**只读取证 + 本地复现**（未改动仓库任何文件，未改动**真库**）
> 真库：`data/codebuddy_gateway.db`（只读，禁止写入）
> 工具：`.venv\Scripts\python.exe`

---

## 0. 方法与隔离性（重要）

为避免触碰真库（约束：db 只读），本复现把真库 **整库拷贝到 `%TEMP%/hy4_repro_db/`**（含 `-wal`/`-shm`），再通过
`CB_GATEWAY_DB_PATH` 指向该副本启动网关模块。所有读取（`settings`/`accounts`/`logs`）
与任何副作用（token refresh、请求日志落库）**都落在 `%TEMP%` 副本**，真库零写入。
复现脚本：`%TEMP%/hy4_repro.py`（系统临时区，不在仓库内）。

- **未启动任何长期网关进程**（默认端口 8787 当时无监听：`netstat` 仅见 3000/8000/8080）。
- **未改动代码、未改动真库、未改动 settings**。
- 对照组除了 `hy3-preview-agent`，另加 `wb-m`（当前白名单内唯一 id）与 `auto`。

---

## 1. Part A — `router.bind()` 复现（当前 400 闸口，**确定性结论**）

设置真实回读：`models` setting = **`[{"id": "wb-m"}]`**（即 workbuddy 白名单当前只有 `wb-m` 一个 id）。
`router.bind()` 走 `WorkBuddyProvider.accepts_model` → `inner in {id for m in list_models()}`。

| 请求 model | stream | bind 结果 |
|---|---|---|
| `hy4-preview` | false | **RAISED `UnknownModel`: Unknown model 'hy4-preview'** |
| `hy4-preview` | true | **RAISED `UnknownModel`: Unknown model 'hy4-preview'** |
| `hy3-preview-agent` | false | **RAISED `UnknownModel`: Unknown model 'hy3-preview-agent'** |
| `hy3-preview-agent` | true | **RAISED `UnknownModel`: Unknown model 'hy3-preview-agent'** |
| `wb-m` | false | OK (channel=workbuddy, inner='wb-m') |
| `wb-m` | true | OK (channel=workbuddy, inner='wb-m') |
| `auto` | false | OK (channel=workbuddy, inner='auto') |
| `auto` | true | OK |

**关键修正（相对 Phase A 假设）**：Phase A 推测"当时 `models` 更宽，故 8/29–8/31 的 4×200 能到达上游，而现网已收窄为 `wb-m`"。
本复现确认现网 `models=[{"id":"wb-m"}]` 不仅挡掉 `hy4-preview`，**连代码里 `DEFAULT_MODELS` 记载的 `hy3-preview-agent` 也被挡掉**。
也就是说：当前 workbuddy 白名单**只接受 `wb-m` 和别名命中项**（别名表 `model_aliases={"gpt-5.5":"glm-5.2","auto":"hy3-x"}`，二者与 hy4/hy3 无关）。
历史成功必然发生在白名单更宽（含 hy3/hy4 或整列为 DEFAULT_MODELS）的时期，如今这层闸口对所有非 `wb-m`/非别名模型一律 400。

调用链定位（`src/gateway/router.py:bind` L40 → `WorkBuddyProvider.accepts_model` `src/providers/workbuddy/__init__.py:43` → `list_models` 读 `models` setting = `[{"id":"wb-m"}]`）。
未命中即 `raise UnknownModel` → `bind_http` 转 HTTP **400 `unknown_model`**（`router.py:127`）。**该拒绝发生在任何上游接触之前**，非流/流式皆然。

---

## 2. Part B / C — 实时上游探测（**未能完成，受限说明**）

为确认"即便放行后上游是否仍认 hy4-preview"，本应按网关等价路径探测 `copilot.tencent.com/v2/chat/completions`：
- 先走 `auth_manager.get_valid_headers`（网关真实刷新流程），再 `stream=True` POST（网关 `build_backend_body` 恒发 `stream=True`）；
- 对照组 `hy4-preview` / `hy3-preview-agent` / `wb-m`；非流、流式各一次；
- 并直接跑 `upstream.proxy.proxy_chat_completions("hy4-preview", ...)` 复现 502/11128 两层。

**结果：全部在鉴权层失败，未取得上游对模型名的真实响应。**

| 探测 | 结果 | 说明 |
|---|---|---|
| `get_valid_headers` | **None** | 日志：`token 刷新失败 (account=1): 10001:refreshToken is empty` |
| 直连上游（未刷 token） | HTTP **401**（hy4/hy3 均 401） | 用库中明文 `access_token`，上游判过期 |
| `proxy_chat_completions("hy4-preview", non-stream)` | **503** `No available accounts` | 因无有效 token，账号选择失败 |
| `proxy_chat_completions("hy4-preview", stream)` | stream 帧：`503 No available accounts` | 同上 |

**根因（环境限制，非模型证据）**：真库 `accounts` 表 workbuddy 账户 `refresh_token` 为空，`ensure_token_valid` 刷新失败 → 无法拿到 `Authorization`。
这属于复现环境凭据缺失，**不得解读为"上游拒绝 hy4-preview"**。`wb-m`（白名单内对照）同样因无 token 而 401/503，证明是凭据问题而非模型问题。

> 结论：本环境**无法实锤"上游当下是否仍提供 hy4-preview"**。Phase A 的 4×200 真实 completion 仍是最强证据（上游曾于 8/29–8/31 提供 hy4-preview）；当下可用性需在有有效 Token 的环境（或用户侧真实请求）验证。

---

## 3. 失败层定位（综合代码静态 + 实时复现 + 日志）

| 层 | 结论 | 证据 |
|---|---|---|
| ① 客户端模型名解析 / 别名 | **无别名、无统一映射**；`resolve_model_alias("hy4-preview")` 原样返回（确认） | `aliases.py` + `model_config.py`；`model_aliases` 仅 2 条，不含 hy4 |
| ② 白名单 400（**主因，今日**） | `models=[{"id":"wb-m"}]` 不含 hy4/hy3 → `bind` 抛 `UnknownModel` → **400 unknown_model**，流/非流皆然，且早于任何上游接触 | Part A 复现表 + `router.py`/`workbuddy/__init__.py` |
| ③ 上游错误码 11128（历史，偶发） | 上游 400 `{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}` 被网关 `_is_11128_error` 误判为"超长请求"（`_COMPACT_11128_MARKERS=("11128","Illegal API invocation")`），进而对 **zcode 客户端**走 `_smart_compact_messages` 自愈精简——但该错误语义是"未授权通道"安全策略，**精简内容无效** | `compaction.py:27,85,174`；日志 id=1346（client=zcode 3.10.1） |
| ④ 流式异常 502 invalid call id（历史，偶发） | 上游 tool-call 帧 `id` 非字符串/空 → `chat_grammar.py:177` 置 `parser_error`，网关中断整条流返回 502 | 日志 id=691/723；`chat_grammar.py:175-178` |
| ⑤ 上游实时可用性 | 本环境凭据失效，**未能验证** | Part B/C 受限说明 |

---

## 4. 明确根因结论

1. **用户"hy4-preview 一直有问题"的近况主因 = 白名单 400**：workbuddy `models` 当前被收窄为 `[{"id":"wb-m"}]`，`hy4-preview` 既不在白名单、也无别名/统一映射，`router.bind()` 在接触上游前即返回 **HTTP 400 `unknown_model`**（非流、流式一致）。这层失败覆盖了 Phase A 观察到的"现网请求根本到不了上游"。
2. **对照事实修正**：当前配置下**连 `hy3-preview-agent`（代码 `DEFAULT_MODELS` 里的模型）也被同样 400 拒掉**——说明白名单已严重收窄，历史 4×200 成功发生在白名单更宽（含 hy3/hy4，或尚为 `DEFAULT_MODELS` 默认）的时期。
3. **即便放行，仍有两层历史遗留失败**：上游偶发 502 `invalid call id`（`chat_grammar` 对 tool-call 帧校验过严）与 400 11128 `unapproved channel`（被误纳入 11128 超长自愈路径，精简无效）。
4. **上游是否仍提供 hy4-preview 无法在本环境判定**（账户 `refresh_token` 为空，拿不到有效 token，实时探测全部 401/503）。Phase A 的 4×200 仍是"上游曾提供"的确证；当下可用性需有效凭据验证。

---

## 5. 建议修复（**本阶段未执行，仅建议；Phase C 待办**）

约束：本 Phase B 严禁改文件（报告除外）。以下为 Phase C 候选，均落在 `fix/hy4-preview-usage`：

- **A. 数据修复（最小、首选）**：先把真库备份（含 `-wal`/`-shm`，沿用 `data/*.bak_*` 习惯），再把 `hy4-preview`（以及应恢复可用的 `hy3-preview-agent` 等）加入 `models` 白名单。理由：Phase A 已证上游曾真实提供 hy4-preview，放行即恢复 200。需与产品确认"当前 workbuddy 应暴露的模型清单"——不能只加 hy4 而遗漏 hy3。
- **B. 代码修复（11128 语义错配）**：`proxy.py` 的 11128 自愈分支应在检测到 `"unapproved channel"` / `"Illegal API invocation"` 语义时**停止**走 `_smart_compact_messages`（精简内容对"通道未授权"无效，且会无谓改写请求体）。可让 `_is_11128_error` 排除含 `unapproved channel` 文案的 400，或在 `_smart_compact_messages` 入口对 security 文案短路。
- **C. 代码修复（502 invalid call id，可选）**：`chat_grammar.py:177` 对 `id` 非字符串判为畸形并整条 502；若上游在后续帧补齐合法 `id`，可考虑容错（缓存待补 `id` 的 tool-call 而非直接断流），否则至少在日志标明"上游工具调用帧畸形"，便于区分"网关校验过严"与"上游 bug"。
- **D. 清晰报错**：若决定不支持 hy4，应在 `/v1/models` 与文档明确，避免客户端盲传。当前 400 `unknown_model` 已是结构化错误，可接受。

---

## 6. 复现命令（审计用）

```powershell
# 1) 拷贝真库到临时区（本脚本内部完成，未碰真库）
# 2) 跑复现（脚本已设 CB_GATEWAY_DB_PATH 指向副本）
cd C:\Usr\Code\etc\Buddy2api
$env:CB_GATEWAY_DB_PATH = "$env:TEMP\hy4_repro_db\codebuddy_gateway.db"
.\.venv\Scripts\python.exe "$env:TEMP\hy4_repro.py"
```

输出要点回顾：
- Part A：`hy4-preview`/`hy3-preview-agent` → `UnknownModel`；`wb-m`/`auto` → OK。
- Part B/C：因 `refreshToken is empty` 无法取有效 token，上游探测全 401/503（环境限制，非模型证据）。

---

## 7. 与 Phase A 的差异 / 一致性

- 一致：hy4 不在任何配置（settings/代码）、别名无映射、上游曾 4×200、11128 与 502 两类历史失败、11128 误入超长自愈。
- **修正**：Phase A 假设"现网 `models` 已收窄故 hy4 直接 400"，本复现**进一步证实该收窄也把 `hy3-preview-agent`（代码内置模型）一并挡掉**，即当前白名单极度收窄（仅 `wb-m`），这是比"仅 hy4 缺失"更根本的配置问题。
- **新增限制披露**：本环境凭据（`refresh_token` 空）无法完成实时上游探测，Phase A 的"上游曾提供"无法被"当下仍提供"复现或证伪。
