# Spec — 排查并修复 hy4-preview 模型使用问题

> 状态：待执行（spec 阶段产出，交由 subagent 用 workflow 编排排查）
> 分支：`fix/hy4-preview-usage`
> 创建：2026-09-05

## 0. 背景与现状（主会话已确认的事实）

- 用户报告「hy4-preview 模型的使用一直有问题」，无具体报错信息。
- **代码里完全没有 `hy4` 字样**（已 grep 全仓 `.py`/配置文件，排除 `.venv`/`data`）。
  现有的只有：
  - `src\upstream\aliases.py` L33 — WorkBuddy 静态模型表 `DEFAULT_MODELS` 里的
    `hy3-preview-agent`；
  - `src\providers\qclaw\constants.py` L39 — QClaw `STATIC_MODELS` 里的 `pool-hy3-preview`。
- 因此 `hy4-preview` 只可能来自**运行时数据**（SQLite `data/codebuddy_gateway.db`
  的 settings 表），候选键：
  - `models` / `model_aliases`（WorkBuddy 自定义模型表与别名，full-replace 语义）；
  - `qclaw.models` / `qclaw.aliases`（QClaw 同理）；
  - `unified_models`（跨通道统一模型翻译表）；
  - `<channel>.reasoning`（按模型思考档位）。
- 请求链路：`gateway/server.py` → `upstream/proxy.py`
  （`resolve_model_alias` → 白名单校验 → `build_backend_body`）→ provider。
- 本机无 `sqlite3` CLI；db 排查需用 Python（`.venv\Scripts\python.exe`）+ `sqlite3` 模块。
- 上游模型路由（DSH subagent 可用）：`buddy/hy3`、`buddy/hy3-x`。

## 1. 目标

1. 查明 `hy4-preview` 在本系统中的真实来源（哪个通道、哪条配置、什么形态）。
2. 复现并定位失败点：是模型名不被白名单接受、别名/统一模型映射缺失、
   上游拒绝（11128/11150 等错误码）、还是流式行为异常。
3. 依据定位结果做出最小修复（代码或数据修正），并验证。

## 2. 约束

- 只在分支 `fix/hy4-preview-usage` 上提交；不得动 `main` / `prod`。
- 数据库只读排查；如需改 settings 一律先备份（拷贝 db 文件，含 `-wal`/`-shm`）。
- 不重启用户正在运行的网关进程；验证用 `test_account_chat` 或直接构造请求，不改线上状态。
- `data/backup/` 与既有 `.bak_*` 文件不动。

## 3. 排查步骤（subagent 执行清单）

### Phase A — 数据取证（只读）
1. 用 `.venv\Scripts\python.exe -c` 打开 `data/codebuddy_gateway.db`
   （只读模式 `file:...?mode=ro`），列出 `settings` 表全部键；
   找出含 `hy4` 的键/值，以及所有模型相关键（`models`, `model_aliases`,
   `qclaw.models`, `qclaw.aliases`, `unified_models`, `*.reasoning`）。
2. 查 usage/request 日志表（如存在）中 model 名含 `hy4` 的最近记录：
   错误码、HTTP 状态、是否 11128 dump、stream/非 stream、时间分布。
3. 结论落盘：hy4-preview 到底配置在哪个通道、以什么身份（直连 id？别名 target？
   unified mapping target？）存在。

### Phase B — 复现与定位
4. 若 Phase A 找到配置：从日志错误码入手对照 `src\upstream\proxy.py` 的
   处理分支（11128 dump、11150 reasoning 拒绝、重试逻辑）定位失败层。
5. 若找不到配置：hy4-preview 大概率是**客户端侧传来的模型名**（如 IDE/CLI
   端配置了 `hy4-preview` 而网关没有别名指向真实模型），此时修法是补
   `model_aliases` 别名映射到真实存在的模型（如 `hy3-preview-agent`），
   或确认上游是否已有 hy4 可用（看 QClaw/WorkBuddy 上游模型列表接口返回）。
6. 对 hy3 系列做一次对照测试：同样请求 hy3 正常、hy4 失败 → 差异即失败点。

### Phase C — 修复与验证
7. 最小修复：
   - 配置缺失 → 改 db settings（先备份）或代码内置别名；
   - 代码 bug → 直接改 `src/` 下相关文件；
   - 上游不支持 → 文档化 + 在网关层返回清晰错误（而不是裸 500/上游错码）。
8. 验证：构造指向 hy4-preview 的 chat 请求（非 stream + stream 各一次），
   确认 200 且有正常 completion；跑 `tests/test_core.py` 中相关用例。

## 4. 执行方式

- 用 workflow 编排 subagent（provider=buddy, model=hy3）执行 Phase A/B/C。
- 每阶段产出写回 `redesign-audit/` 下对应报告文件（沿用现有编号习惯，
  新文件名 `17-hy4-preview-investigation.md` 起）。
- 最终在分支上以独立 commit 提交：spec、报告、修复。

## 5. 验收标准

- [ ] 报告写明 hy4-preview 的来源与失败根因（有日志/数据证据）。
- [ ] 修复后 hy4-preview 请求成功（或给出"上游无此模型"的确凿证据与清晰报错）。
- [ ] `tests/test_core.py` 相关测试通过，无回归。
- [ ] 全部改动只落在 `fix/hy4-preview-usage` 分支。
