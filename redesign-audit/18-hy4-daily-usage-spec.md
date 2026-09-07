# Spec — hy4-preview 日常可用性实测（边测边分析）

> 状态：待执行（workflow 委派 hy3 subagent 执行）
> 分支：`fix/hy4-preview-usage`
> 前置：上一轮已放开 workbuddy 白名单（commit `7e6d5e6`），账户凭据已恢复
> （/health 显示 active_accounts=4, credential_error_accounts=0）。

## 0. 环境事实（主会话已确认）

- 网关已在 **http://127.0.0.1:8787** 运行（v2.2.0），workbuddy 通道 loaded/active。
  8788 是另一实例（键更少），**统一用 8787**。
- settings 每请求实时读 SQLite（`model_config.channel_model_ids`），
  上次写入白名单的 `hy4-preview` 无需重启即生效；待 `/v1/models` 确认。
- API key：从 `data/codebuddy_gateway.db` 只读查出可用 key（api_keys 表），
  仅用于本地测试请求。
- 已知历史失败模式（上一轮取证）：
  1. 400 `11128 "Illegal API invocation from an unapproved channel"`（上游安全策略，特定客户端组合）；
  2. 502 `"The upstream tool call stream had an invalid call id."`（`chat_grammar.py:177` 工具调用流校验）；
  3. 白名单 400（已修复）。
- 长上下文是真实使用场景：历史日志有 194k/262k 输入 token 的 200 记录。

## 1. 目标

回答一个问题：**hy4-preview 能否正常满足日常使用**。
用可重复的测试脚本边测边分析，产出量化结论（成功率/延迟/质量/失败模式），
对照组为 `hy3-preview-agent`（以及必要时 `auto`）。

## 2. 评测维度与通过标准

| # | 维度 | 方法 | 通过标准 |
|---|------|------|----------|
| A | 基础对话 | 非流式 + 流式各 ≥2 例（中/英） | 200，finish=stop，内容非空且切题 |
| B | 流式完整性 | 流式请求：首 token 延迟(TTFT)、chunk 连续性、finish_reason、usage 字段 | 无中断/乱码，TTFT 与 hy3 同量级 |
| C | 工具调用 | 单轮 function call + **连续 ≥3 轮** tool 往返（含多工具并行选择） | 全部 200，tool_calls 参数为合法 JSON，无 502 invalid call id |
| D | 长上下文 | 构造 ~30k-50k 输入（拼接文档+提问），非流式 | 200 且答案引用正确；关注是否触发 11128 |
| E | 稳定性 | 同一请求重复 5 次，记录延迟分布 p50/p95 | 成功率 100%，无偶发 5xx |
| F | 对照 | hy3-preview-agent 跑 A/C/E 同款用例 | hy4 各项不显著劣于 hy3 |
| G | 质量抽查 | 一道代码题 + 一道中文推理题，人工可读评估 | 答案完整、无明显幻觉/截断 |

## 3. 约束

- **只测不改**：不改 db、不改代码、不重启运行中的网关、不切分支。
- 请求预算：总请求 ≤ 45 次；`max_tokens` ≤ 1024（长上下文题 ≤ 512），
  控制配额消耗。
- 测试脚本落盘到 `tests/manual/hy4_daily_probe.py`（可重复执行，
  结果打 JSON 到 stdout + 追加写 `tests/manual/hy4_probe_results.jsonl`）。
- 网关地址/键从命令行参数或 db 只读读取，不硬编码。
- 测试产生的日志记录会进入用户 logs 表——属预期，不做清理。

## 4. 执行步骤（workflow 三阶段，agent 全部 provider=buddy model=hy3）

### Phase 1 — 冒烟与环境确认
1. 读 db（只读）取 key；GET /v1/models 确认含 `hy4-preview`；
   若不含 → 停止并报告（说明 settings 是否被覆盖回退）。
2. 跑维度 A/B（hy4 + hy3 对照），记录 TTFT/延迟/tokens。

### Phase 2 — 能力与稳定性
3. 依据 Phase 1 的结果跑维度 C/D/E/G（hy4），C/E 加 hy3 对照。
4. 任何失败立即保留原始响应（状态码、错误体、SSE 片段）写入结果文件。
5. 若出现 502 invalid call id 或 11128：记录完整请求/响应上下文，
   并追加 2 次复现尝试确认是否必现。

### Phase 3 — 汇总分析
6. 汇总各维度指标表：成功率、延迟 p50/p95、TTFT、tokens/s、失败清单。
7. 写报告 `redesign-audit/18-hy4-daily-usage-report.md`，给出明确结论：
   「可满足日常使用 / 有条件可用（列条件）/ 不可用（列证据）」，
   并给出与 hy3 的对比建议（如"hy4 适合长上下文，hy3 更稳"之类）。
8. git 提交 spec + 脚本 + 结果 + 报告到当前分支（不 push）。

## 5. 验收标准

- [ ] `/v1/models` 确认 hy4-preview 已放行（或有证据说明未放行原因）。
- [ ] 脚本可重复运行，结果 JSONL 落盘。
- [ ] 报告含量化指标表与明确可用性结论。
- [ ] 全部改动只落在 `fix/hy4-preview-usage` 分支。
