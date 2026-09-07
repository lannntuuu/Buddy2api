# 冒烟阶段报告 — hy4-preview 日常可用性实测（Phase 1 复算）

> 分支：`fix/hy4-preview-usage`（未切换、未改代码、未改 db、未重启网关）
> 网关：`http://127.0.0.1:8787`（v2.2.0，workbuddy active）
> 报告作者：实测 subagent（冒烟复算，不发任何新模型请求）
> 关联 spec：`redesign-audit/18-hy4-daily-usage-spec.md`

## 0. 范围与方法

- 仅复算上一轮 Phase 1 已落盘的冒烟结果，**未发送任何新的模型请求**（遵守 ≤28 次请求预算与"不发新请求"约束）。
- 结果文件：`tests/manual/hy4_probe_results.jsonl`（UTF-8）。
- 解析工具：`tmp_pytest_tmp/smoke_analyze.py`，用 `C:\Usr\Code\etc\Buddy2api\.venv\Scripts\python.exe` 只读运行，**未用 PowerShell**（避免控制台乱码）；结果落盘到 `tmp_pytest_tmp/smoke_analysis.json`，**无任何 key 写入**。
- 模型白名单与控制模型确认：用 `file:...?mode=ro` 只读读取 `data/codebuddy_gateway.db` 的 `settings` 表（`models` / `model_aliases` / `unified_models`），仅读取 api_keys 的 `name/status/client_type/default_channel` 元数据，**未读 key_secret**。

## 1. JSONL 去重与「双写」核查

### 1.1 统计（按 (suite, case, model, ts) 去重前/后）

| 指标 | 值 |
|------|----|
| 原始行数（raw_line_count） | 12 |
| JSON 解析失败行 | 0 |
| 字节级完全相同的重复行（exact_duplicate_lines） | **0** |
| (suite,case,model,ts) 主键重复（dedup_key_duplicates） | **0** |
| 去重后唯一记录数 | **12** |

### 1.2 双写结论

- 当前 jsonl **没有重复行**，也无主键冲突。Phase 1 应跑的用例数恰好为 12（basic：4 case × 2 model = 8；stream：2 case × 2 model = 4），与文件记录数完全吻合，**未出现双写**。
- 代码核查：`tests/manual/hy4_daily_probe.py` 的 `log()`（约 502–510 行）对每条记录只做 **一次** `f.write(...)` 追加到 jsonl（另一次是 `print` 到 stdout，属于不同 sink，不算双写）。各 `run_*` 函数对每个 case 也只调用一次 `log()`（如 `run_tools` 的 `multi_round_3` 在 `if tc2 / else` 二选一触发，不会双发）。
- **结论：脚本不存在双写 bug，本文件无需修代码。** （"疑似双写"在上一轮只是猜测，实测数据不支持。为保持只读纪律，未改动测试脚本。）

> 备注：若后续仍希望加一道防御，可在 `log()` 内用 `(suite,case,model,ts)` 做进程内幂等集合去重，但当前非必需，且任务要求"不改代码"，故保留现状。

## 2. 模型列表确认（控制模型选择）

### 2.1 实测 whitelist（来自 `settings.models`，workbuddy 通道）

| id | 在列 |
|----|------|
| `wb-m` | ✅ |
| `hy4-preview` | ✅（上一轮白名单修复已生效，Phase 1 全部 200 印证） |
| `hy3-x` | ✅ |
| `deepseek-v4-flash` | ✅ |

- `unified_models`：`["deepseek-v4-flash"]`（仅一个跨平台翻译项）。
- `model_aliases`：`{"gpt-5.5": "glm-5.2", "auto": "hy3-x"}` → 即 `auto` 会落到 `hy3-x`。

### 2.2 控制模型：为何数据里是 `hy3-x` 而非 spec 的 `hy3-preview-agent`

- spec 第 1 节 F/G 维度写对照组为 `hy3-preview-agent`（必要时 `auto`）。
- **但 `hy3-preview-agent` 不在本网关白名单中**（`settings.models` 未含，也不在 `unified_models`/aliases 里）。若直接请求 `hy3-preview-agent` 会触发白名单 400。
- 数据中 Phase 1 实际用的是 **`hy3-x`**，它是白名单内唯一可用的 hy3 家族模型；且 `auto` 别名也指向它。
- **后续沿用决定**：Phase 2/3 的 C/E/G 对照统一使用 **`hy3-x`**（必要时可用 `auto` 作为同义别名），不再尝试 `hy3-preview-agent`，以免产生白名单 400。原因写入本报告，避免后续 agent 重复踩坑。

## 3. TTFT / 延迟对比表（Phase 1：basic + stream，各 200）

> 单位 ms。聚合口径：basic 取 4 case、stream 取 2 case 的均值/p50/p95；TTFT=首 token 延迟；latency=端到端总耗时。

### 3.1 basic 套件（非流式 zh/en + 流式 zh/en）

| 模型 | 请求数 | 状态码 | 完成(finish) | TTFT avg | TTFT p50 | latency avg | latency p50 |
|------|-------|--------|--------------|----------|----------|------------|------------|
| hy4-preview | 4 | 全部 200 | 全部 `stop` | 5717.0 | 8907 | 6773.8 | 8907 |
| hy3-x | 4 | 全部 200 | 3×`stop` + 1×`length` | 3213.8 | 4813 | 3526.2 | 4813 |

- hy4-preview 在 basic 上比 hy3-x **慢约 1.8×**（TTFT）/**约 1.9×**（latency）。
- 质量提示：hy3-x 的 `en_nonstream` 命中 `length`（max_tokens=512 耗尽，答案在单词中间被截断）；hy4-preview 四个 case 均为 `stop`，且答案完整、切题（闭包/REST API/沸点/巴黎均正确）。

### 3.2 stream 套件（流式 zh/en 计算题）

| 模型 | 请求数 | 状态码 | chunk 连续性 | 完成(finish) | TTFT avg | TTFT p50 | latency avg |
|------|-------|--------|--------------|--------------|----------|----------|------------|
| hy4-preview | 2 | 全部 200 | `true` | 全部 `stop` | 2776.5 | 3732 | 6595.5 |
| hy3-x | 2 | 全部 200 | `true` | 全部 `length` | 941.5 | 954 | 5558.0 |

- 流式首 token：hy3-x 明显更快（~941ms vs ~2777ms，约 3×）。
- 但 hy3-x 两个流式 case 均 `length`（512 token 全用于 reasoning，正文答案被截断）；hy4-preview 均 `stop` 且给出完整分步推导（17×23=391、1..5 求和=15）。
- 两个模型 stream chunk 连续性均为 `true`，无乱码/中断。

## 4. 成功率与已知失败模式（Phase 1）

| 维度 | hy4-preview | hy3-x（对照） |
|------|-------------|---------------|
| 成功率（200/total） | 6/6 (100%) | 6/6 (100%) |
| 502 invalid call id | 0 | 0 |
| 400 11128 unapproved channel | 0 | 0 |
| 白名单 400 | 0（hy4-preview 已放行） | 0 |

- Phase 1 完全干净：**无** 502、**无** 11128、**无** 白名单拒绝。说明上一轮的白名单放行（commit `7e6d5e6` 后 `hy4-preview` 进 `settings.models`）在运行中即时生效，符合 spec 预期。
- hy4-preview 作为推理模型：非流式 `content` 可能为 null、主要靠 `reasoning_content`——本批用例中 `content` 均有值且 `finish=stop`，行为正常。

## 5. 结论与后续建议

1. **冒烟通过**：hy4-preview 在基础对话（A）与流式完整性（B）两个维度全部 200、答案切题、SSE 连续无中断；对照 hy3-x 同款用例也全 200。
2. **对照模型**：spec 的 `hy3-preview-agent` 不在白名单，**不可用**；实测与后续均以 **`hy3-x`** 为对照（=`auto` 别名目标）。已在本报告登记原因。
3. **延迟画像**：hy4-preview TTFT/总耗时约为 hy3-x 的 1.8–3×，但产出更完整（hy3-x 在多 case 命中 `length` 截断，hy4-preview 多为 `stop`）。对延迟敏感场景 hy3-x 首 token 更快；对"要完整答案"场景 hy4-preview 更稳。
4. **双写**：jsonl 无重复，脚本无双写 bug，未改代码。
5. **后续（Phase 2/3）沿用**：工具调用(C)/长上下文(D)/稳定性(E)/质量(G) 对照统一用 `hy3-x`；继续关注 502 invalid call id 与 11128 在历史失败模式下的复现；请求预算剩余 ≤28 次、max_tokens≤1024 不变。

---
*生成方式：python 只读解析 jsonl + 只读 db 白名单；未发新请求、未写 key、未改运行代码。*
