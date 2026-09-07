# Phase 2 能力/稳定性报告 — hy4-preview 日常可用性实测

> 分支：`fix/hy4-preview-usage`（未切换、未改代码、未改 db、未重启网关）
> 网关：`http://127.0.0.1:8787`（v2.2.0，workbuddy active，凭据正常）
> 关联 spec：`redesign-audit/18-hy4-daily-usage-spec.md`
> 前置冒烟：`redesign-audit/18a-hy4-smoke.md`
> 报告作者：实测 subagent（Phase 2）
> 对照模型：**hy3-x**（spec 写的 `hy3-preview-agent` 不在白名单，沿用 18a 决定）

## 0. 范围与约束遵守

- 行请求预算：Phase 2 实际发 **21** 次模型请求（≤28 预算），明细见 §6。
- `max_tokens`：普通/工具/稳定/质量 ≤512（`longctx` 512，`quality` 脚本默认 512）；
  额外两次 `quality` 复检用 1024 / 2048（仅为定位失败根因，非 spec 必跑，已计入预算）。
- 只读纪律：key 经 db `mode=ro` + gateway `decrypt_secret` 在进程内还原到内存，**未落盘、未写进任何提交文件**；分析结果用 python 读 UTF-8 jsonl（未用 PowerShell 控制台，避免乱码）。
- 未改测试脚本、未改 db、未重启网关、未切分支。

## 1. 结果文件完整性（复算）

| 指标 | 值 |
|------|----|
| jsonl 总行数 | 31（Phase1 12 + Phase2 19） |
| JSON 解析失败 | 0 |
| 字节级重复行 | 0 |
| (suite,case,model,ts) 主键重复 | 0 |

→ 数据可信，无双写。

## 2. 维度 C — 工具调用（单轮 + 连续 3 轮往返）

> 失败模式关注：502 `invalid call id`、参数 JSON 合法性。

| 模型 | case | 状态 | 单轮 tool_called | 参数合法 JSON | 多轮 r2 | 多轮 r3 | r2 工具 | 备注 |
|------|------|------|------------------|--------------|---------|---------|--------|------|
| hy4-preview | single_round | 200 | ✅ get_weather | ✅ | — | — | — | finish=tool_calls |
| hy4-preview | multi_round_3 | (r1/r2/r3 均 200) | — | — | 200 | 200 | calculator | r3 给出总结 |
| hy3-x | single_round | 200 | ✅ get_weather | ✅ | — | — | — | finish=tool_calls |
| hy3-x | multi_round_3 | (r1/r2/r3 均 200) | — | — | 200 | 200 | calculator | r3 给出总结 |

- **多轮闭环验证通过**：单轮返回 `get_weather` → 回传模拟结果 → r2 调用 `calculator` → 回传 → r3 综合总结，3 轮全 200。
- **参数 JSON 合法**：`tool_args_valid_json=true`（单轮 `get_weather` 的 `arguments` 可被 `json.loads` 解析）。
- **502 invalid call id：0 次**（hy4 与 hy3-x 均无）。工具流在此网关/通道下当前版本表现正常。
- 注：脚本 `multi_round_3` 的 `status` 字段为 null（仅记录 r2/r3 子状态），但 r2_status/r3_status 均为 200，证明三轮链路完整。

## 3. 维度 D — 长上下文（约 73k 字符 / 33k prompt tokens）

| 模型 | case | 状态 | 输入字符 | prompt_tokens | finish | 延迟(ms) | 触发 11128 |
|------|------|------|---------|---------------|--------|---------|-----------|
| hy4-preview | 30k_input | 200 | 73,530 | 33,034 | length | 12,838 | 否 |

- 无 11128、无白名单拒绝，长输入正常被接受并产出。
- **但 hy4 命中 `length`**：`completion_tokens_details.reasoning_tokens=512`（全部 max_tokens 被用于推理），正文一句总结未输出（`answer_summary` 停在"We need answe…"）。
- 结论：长上下文**能通**，但**推理模型会先把配额吃满在 reasoning 上**，非流式长文问答需调大 `max_tokens` 才能拿到正文本。这与质量维度（§5）表现同源。

## 4. 维度 E — 稳定性（同请求 ×5）

> 同一问题「用一句话说明 2+2 等于几」重复 5 次。

| 模型 | 状态码 | 成功率 | 延迟 p50(ms) | 延迟 p95(ms) | 偶发 5xx |
|------|--------|--------|-------------|-------------|----------|
| hy4-preview | [200×5] | 5/5 (100%) | 6,279 | 7,325 | 0 |
| hy3-x | [200×5] | 5/5 (100%) | 1,752 | 2,128 | 0 |

- 两者均 **100% 成功、零偶发 5xx**，稳定性达标。
- hy4 延迟约为 hy3-x 的 **3.0–3.6×**（p50 6279 vs 1752，p95 7325 vs 2128）。

## 5. 维度 G — 质量抽查（hy4-preview）

| case | 状态 | finish | 结果 | 评估 |
|------|------|--------|------|------|
| 代码题（is_palindrome） | 200 | **length** | 仅 reasoning，未输出函数体（512 token 全耗在推理） | ⚠️ 不完整 |
| 中文推理题（小红年龄） | 200 | **stop** | 推导完整、方程正确，结论「小红 13 岁」✅ | ✅ 完整正确 |
| 代码题复检 @1024 | 200 | **length** | content 长度=0（推理耗尽全部 1024） | ⚠️ 仍不完整 |
| 代码题复检 @2048 | 200 | **stop** | 输出完整 `def is_palindrome` 实现（1601 字符）✅ | ✅ 完整正确 |

- **根因定位（重要）**：代码题在 512 / 1024 下失败**不是模型缺陷，而是推理预算耗尽**——hy4-preview 作为推理模型把全部 `max_tokens` 用于 `reasoning_content`，正文没空间。把上限放到 **2048** 即正常 `stop` 并给出可运行代码。
- 中文推理题在标准 512 下即完整正确，说明「答案短」类问题不受影响；受影响的是「需要长正文 + 思考」的代码/长文题。
- **无幻觉**：两道可判题答案均正确。

## 6. 预算与失败模式汇总

### 6.1 请求计数（Phase 2，共 21）

| suite | hy4-preview | hy3-x | 小计 |
|-------|-------------|-------|------|
| tools | 3（单轮+r2+r3） | 3 | 6 |
| longctx | 1 | — | 1 |
| stability | 5 | 5 | 10 |
| quality（标准） | 2 | — | 2 |
| quality 复检 | 2（1024/2048） | — | 2 |
| **合计** | 13 | 8 | **21** |

剩余预算：28 − 21 = **7**。

### 6.2 已知失败模式复现（spec §0）

| 失败模式 | Phase 2 出现次数 | 是否必现 |
|----------|-----------------|----------|
| 502 invalid call id（工具流） | **0** | 未复现（工具 suite 全 200） |
| 400 11128 unapproved channel | **0** | 未复现（含 33k 长上下文也未触发） |
| 白名单 400 | **0** | 未复现 |

- **因 502/11128 在 Phase 2 均未出现，按 spec §4.5 的「出现才追加 2 次复现」规则，未做额外复现请求**；已留作预算缓冲。
- 任何失败响应均已在 jsonl 保留原始 `error_body`（本批无 error_body，因为无错误响应；质量代码题是 `length` 正常 200，非失败，正文通过 `answer_summary` 保留）。

## 7. 结论（Phase 2）

1. **工具调用（C）通过**：hy4-preview 单轮 + 连续 3 轮工具往返全 200，参数 JSON 合法，多轮闭环成立，**无 502 invalid call id**。与 hy3-x 表现一致。
2. **长上下文（D）可用但不"开箱完整"**：33k prompt tokens 正常受理、无 11128；但默认 512 `max_tokens` 下 hy4 把额度全花在推理，正文不完整 → 需给足 `max_tokens`（建议 ≥2048）才能拿到长文答案。
3. **稳定性（E）通过**：hy4 与 hy3-x 各 5/5 成功、零 5xx；hy4 延迟约为 hy3-x 的 3×。
4. **质量（G）有条件达标**：中文短推理题完整正确；代码/长文题在默认 512 下被 reasoning 预算截断（length），**调大 `max_tokens` 至 2048 即完整正确**——属推理模型配额配置问题，非能力缺陷。
5. **失败模式**：502 / 11128 / 白名单 400 在 Phase 2 **全部零出现**，无需追加复现。

> Phase 2 一句话结论：**hy4-preview 能力面（工具/长上下文/稳定/质量）全部可用；唯一注意点是推理模型会优先吃满 max_tokens 做思考，故对"要长正文"的请求需显式给更大的 max_tokens（≥1024，复杂代码题 ≥2048），否则会看到 length 截断而非答案。**

---
*生成方式：python 只读解析 jsonl + db 只读取 key；未发新分支操作、未改代码/库、未重启网关。所有响应原文保留于 `tests/manual/hy4_probe_results.jsonl`。*
