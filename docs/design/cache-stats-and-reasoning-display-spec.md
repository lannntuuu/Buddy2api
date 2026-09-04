# Spec：缓存命中统计修复 + 思考档位真实值显示

> 分支：`opt/cache-reasoning-stats`（自 main 切出）
> 执行模型：MiniMax-M3（subagent workflow 委派）
> 状态：待用户确认

---

## 0. 排查结论（根因，均经真实 DB / 代码实证）

### 需求 1：缓存命中 token / 命中率统计不到值

**根因 A（主凶）— `_extract_cache_tokens` 优先级 bug**
`src/upstream/proxy.py` L556-600：提取顺序为 Anthropic → DeepSeek → OpenAI。
但 WorkBuddy 上游（copilot.tencent.com）返回的 usage **同时携带全部三种风格字段**，实测样本：

```json
{
  "prompt_tokens": 71297,
  "prompt_tokens_details": {"cached_tokens": 70848},
  "prompt_cache_hit_tokens": 70848,
  "prompt_cache_miss_tokens": 449,
  "cache_read_input_tokens": 0,        ← 上游置 0 的占位字段！
  "cache_creation_input_tokens": 0
}
```

当前代码第一步读到 `cache_read_input_tokens == 0` 就 `return (0, 0)`，真实命中 70848 被丢弃。
DB 实证：`data/codebuddy_gateway.db` logs 表 1801 条 workbuddy 记录 `SUM(cache_read_tokens)=0`，
但 207 条含 usage_json 的记录里 `prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens`
均为真实命中值（5 万+ token/条）。

**修复**：`_extract_cache_tokens` 改为「**取各风格的最大非零值**」而非「按优先级短路返回」：
- 候选 1：`cache_read_input_tokens`（Anthropic）
- 候选 2：`prompt_cache_hit_tokens`（DeepSeek）
- 候选 3：`prompt_tokens_details.cached_tokens`（OpenAI）
- `cache_read = max(非零候选)`，仍 clamp 到 `[0, prompt_tokens]`；
- `cache_creation`：`cache_creation_input_tokens` 与 `cache_read_input_tokens + prompt_cache_miss_tokens`（仅当后者是唯一信息源时）取可用者，保守取 Anthropic 字段非零值，否则 0。

**根因 B — 命中率字段从未计算**
`src/storage/repos/stats.py` `get_provider_model_usage`：SQL 汇总了 `cache_read_tokens` /
`cache_creation_tokens`，但 `_finalize` / `_new_summary` 从不产出 `cache_hit_ratio`。
前端 `src/web/js/pages/usage.js` 三处 `{{pct(...cache_hit_ratio)}}` 永远拿到 undefined →
`pct()` 显示 `0%`。

**修复**：`_finalize`（及 totals）增加
`cache_hit_ratio = cache_read / prompt_tokens`（cache_read 是 prompt 子集，分母直接用
prompt_tokens；prompt_tokens==0 时置 `None`，前端 `pct(null)` 显示 0%）。

**根因 C — 其余 4 个 provider 从不写缓存字段**
- `providers/gmi/chat.py` `_record()`：有 usage_json 但不写 `cache_read_tokens` 等列；
- `providers/qwenwork/chat.py` `_log()` / `providers/qclaw/chat.py` `_log()`：完全不带缓存字段
  （DB 走 DEFAULT 0）；
- `providers/traework/chat.py` `_log()`：连 token 数都是硬编码 0（上游协议不回 usage，
  **标记为"上游无数据，本次不改"**）；
- `providers/traesolo/chat.py`：已有正确实现（Anthropic 风格直读），无需改动。

**修复**：gmi / qwenwork / qclaw 的 `_log`/`_record` 复用 proxy 的
`_extract_cache_tokens`（提升为公共函数，见 §3 结构），usage 里有什么就记什么。

### 需求 2：思考档位默认值显示 `-` 而非真实值

**现状链路**：`proxy.py build_backend_body` L111-114 —— 客户端没传 `reasoning_effort` 且没配
`<channel>.reasoning` 时**不注入**，`effective_reasoning = body.get("reasoning_effort")` 为
None → logs.reasoning_effort 存 NULL → 前端 logs.js 显示 `-`。

但"跟随上游默认"不等于"没有档位"。上游（copilot.tencent.com）对档位的真实行为有实测结论
（`docs/design/per-model-reasoning-effort.md` §2 探针实验）：
- deepseek-v4-flash / glm-5.2 / auto：默认**不思考**（0 reasoning tokens）→ 真实默认 = `none`
- kimi-k2.7：默认**轻思考** → 真实默认 = `minimal`

**修复方案（推断显示，标注来源）**：
1. proxy.py 新增模块级映射表 `_UPSTREAM_DEFAULT_REASONING = {
     "deepseek-v4-pro": "none", "deepseek-v4-flash": "none",
     "glm-5.2": "none", "auto": "none", "kimi-k2.7": "minimal" }`
   （来源：per-model-reasoning-effort.md §2 实测；未知模型映射为 `"upstream"`）；
2. `_log_request` 的 `reasoning_effort` 参数为 None 时，落库值改为
   `effective_reasoning or _UPSTREAM_DEFAULT_REASONING.get(model, "upstream")`；
   客户端显式传参/配置注入的值不受影响（保持精确记录）；
3. 前端 `logs.js` 思考列：值为 `upstream` 时显示 `上游默认`（title 注明"未显式注入，跟随上游默认档位"），
   其他值照旧显示。DB 历史 NULL 行不改（前端对 NULL 仍显示 `-`，或统一兜底为"上游默认"——见 §5 决策点）。

> 备注：严格意义上 `none` 是"不思考"而非档位枚举值，但实测探针确认上游接受 `none`
> 且行为与不传一致，用 `none` 表示"默认档"最贴近真实语义；`upstream` 仅用于未知模型。

---

## 2. 改动文件清单

| 文件 | 改动 | 对应需求 |
|---|---|---|
| `src/upstream/proxy.py` | ① `_extract_cache_tokens` 改 max-of-candidates 逻辑并重命名为公共函数（`providers/store_common.py` 或新 util），proxy 内引用不动调用方；② 新增 `_UPSTREAM_DEFAULT_REASONING` 表；③ `_log_request` 内 reasoning 落库兜底 | 1A / 2 |
| `src/providers/gmi/chat.py` | `_record()` 增加 `cache_read_tokens` / `cache_creation_tokens` / `credit_source`（复用公共提取函数） | 1C |
| `src/providers/qwenwork/chat.py` | `_log()` 同上 | 1C |
| `src/providers/qclaw/chat.py` | `_log()` 同上 | 1C |
| `src/storage/repos/stats.py` | `get_provider_model_usage` 的 `_finalize` / `_new_summary` / totals 补 `cache_hit_ratio` | 1B |
| `src/web/js/pages/usage.js` | 无需大改（字段补上即活）；命中率副标题文案改为 "cache_read / prompt_tokens" | 1B |
| `src/web/js/pages/logs.js` | 思考列：`upstream` → 显示"上游默认"，NULL → 显示"上游默认"（兜底） | 2 |
| `tests/` | 新增/更新用例（见 §6） | 全部 |

`traework` 不动（上游协议无 usage 数据，硬编码 0 是如实记录）。

---

## 3. 实现细节约定

1. **提取函数公共化**：把 `_extract_cache_tokens` 移到 `src/providers/store_common.py`
   （各 provider 已依赖该模块），签名不变 `-> tuple[int, int]`；proxy.py 改为
   `from providers.store_common import extract_cache_tokens`（保留旧名 alias 一个版本亦可）。
   max-of-candidates 语义：
   ```python
   candidates = [
       usage.get("cache_read_input_tokens"),
       usage.get("prompt_cache_hit_tokens"),
       (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
           if isinstance(usage.get("prompt_tokens_details"), dict) else None,
   ]
   nonzero = [int(v) for v in candidates if v is not None and int(v) > 0]
   cache_read = max(nonzero) if nonzero else 0
   cache_read = max(0, min(cache_read, int(usage.get("prompt_tokens", 0) or 0)))
   cache_creation = max(0, int(usage.get("cache_creation_input_tokens") or 0))
   ```
2. **credit_source 判定同步放宽**：`_log_request` 里 `_known_cache_keys` 已含三种键，无需改；
   gmi/qwenwork/qclaw 补写时沿用同样判定。
3. **缓存命中率口径**：`cache_hit_ratio = cache_read_tokens / prompt_tokens`（float, 4 位小数）；
   分母 0 → `None`。前端 `pct()` 对 null 显示 `0%`，可接受。
4. **思考档位兜底只影响展示层落库**：`build_backend_body` 的注入逻辑完全不动；
   新映射表只作用于 `_log_request` 的落库值。
5. **不改 DB schema**：所有字段已存在（logs 表 `cache_read_tokens` / `cache_creation_tokens` /
   `reasoning_effort` / `usage_json` 均已迁移过），零迁移。
6. **向后兼容**：traesolo 现有正确路径不回归；`upstream/proxy.py` 现有单测
   （`test_build_backend_body_preserves_explicit_reasoning_effort` 等）必须全绿。

---

## 4. 验收标准

1. 用 `data/codebuddy_gateway.db` 中任一条历史 workbuddy usage_json 样本喂
   `extract_cache_tokens`，返回 `(70848, 0)` 类真实值（而非 0）；
2. stats API `/admin/provider-model-usage` 返回体中 `totals.cache_hit_ratio` 为 0~1 浮点；
   新请求落库后 usage 页"缓存命中 Token"与"缓存命中率"显示真实数值；
3. 客户端不传 `reasoning_effort` 时，logs 新记录 `reasoning_effort` 为
   `none`/`minimal`/`upstream`（按模型映射），logs 页不再显示 `-`；
   客户端显式传参时仍记录显式值；
4. gmi / qwenwork / qclaw 有 usage 响应时，logs 落库 `cache_read_tokens` 正确；
5. `pytest tests/` 全量通过；前端页面（dashboard/usage/logs/channels）手动加载无 JS 报错。

---

## 5. 决策点（已按最合理取值，用户可改）

| # | 决策 | 取值 | 备选 |
|---|---|---|---|
| D1 | 历史脏数据（cache_read=0 但 usage_json 有真值）是否回填？ | **不回填**，只修增量（简单安全） | 写一次性迁移脚本回填 207 条 |
| D2 | logs 页 NULL reasoning 显示 | **兜底显示"上游默认"** | 保留 `-` |
| D3 | 命中率分母 | `prompt_tokens`（cache_read ⊆ prompt） | `prompt_tokens + cache_creation`（Anthropic 全量口径，本项目上游不适用） |
| D4 | 未知模型 reasoning 兜底值 | `"upstream"`（前端显示"上游默认"） | 存 NULL 照旧显示 `-` |

---

## 6. 测试计划

| 用例 | 断言 |
|---|---|
| `extract_cache_tokens` 三风格并存 | 取 max 非零：`(70848, 0)` |
| 仅 Anthropic 风格 | 行为与旧实现一致（含 creation） |
| 仅 DeepSeek / 仅 OpenAI 风格 | 正确提取，clamp 到 prompt_tokens |
| usage 为 None / 空 / 全零 | `(0, 0)` |
| `_log_request` reasoning=None + 映射表命中 | 落库 `none`/`minimal` |
| `_log_request` reasoning=None + 未知模型 | 落库 `upstream` |
| `_log_request` reasoning="high" | 落库 `high`（不覆盖） |
| stats `_finalize` | prompt>0 时 ratio=cache_read/prompt；prompt=0 → None |
| gmi `_record` 带 cached usage | 落库 cache_read>0 |
| 回归：traesolo / proxy 既有测试 | 全绿 |
