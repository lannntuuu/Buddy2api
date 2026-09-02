# WorkBuddy cache_read 统计补全方案

> 状态：**已实施（Part 1–4）**；Part 5 历史反填保持默认不做，待实测证据
> 关联：`docs/credit-and-token-tracking.md` §10.7（traesolo 已落地，本方案补齐 workbuddy）
> 排查日期：2026-08-28（基于重构后代码结构：`upstream/` / `storage/` / `gateway/` / `accounts/` / `providers/`）
> 实施日期：2026-08-28

## 八、实施记录

- **Part 1**（`upstream/proxy.py`）：新增模块级纯函数 `_extract_cache_tokens(usage)`，兼容 Anthropic / DeepSeek / OpenAI 三种字段风格，负值 clamp、cache_read 不超过 prompt_tokens；`_log_request` 新增 keyword-only 参数 `usage: dict | None = None`，log_data 补充 `cache_read_tokens` / `cache_creation_tokens` / `usage_json`（>64KB 截断） / `credit_source`（usage 含任意已知 cache 键标 `'live'`）；成功路径（流式 `_log_request` 成功、eof_error）与 `_collect_stream` 非流式成功路径均传入 `observer.usage` / `u`。
- **Part 2**（`upstream/responses.py`）：`response_usage()` 的 `input_tokens_details.cached_tokens` 与 `output_tokens_details.reasoning_tokens` 改为取真值（Anthropic / OpenAI 风格 + reasoning），不再硬编码 0。
- **Part 3**（`web/js/pages/usage.js`）：前端「用量统计」页**已**具备缓存命中 Token 列、缓存命中率列与顶部卡片副行（实现早于本方案文档，直接消费聚合字段，无需改动）。
- **Part 4**（`tests/test_workbuddy_cache.py`，新增，12 用例全绿）：覆盖 `_extract_cache_tokens` 三种风格/混合优先级/负值/越界/空 None；`_log_request` 带 usage 落库字段 + usage_json + credit_source 判定 + 截断保护；Responses 流 `cached_tokens` 透传。
- **验证**：临时库端到端跑通 `record_request`（带 cache 键）→ `get_provider_model_usage` 聚合出 `cache_read_tokens` / `cache_creation_tokens` / `cache_hit_ratio` 正确。
- **回归**：全量 pytest 中 13 例失败均为**存量**（与本次改动无关——stash 掉 `proxy.py` / `responses.py` 后失败集合不变）；新增 12 例全通过。
- **遗留**：Part 5 历史 workbuddy 行反填默认不做；部署后实测上游字段名（见第三节）前，不触发反填。

## 一、背景与证据

WorkBuddy 平台的请求日志完全没有统计 cache_read（缓存命中 token）的量。traesolo 已在 v2.1 落地完整的三档 cache 追踪（`cache_read_tokens` / `cache_creation_tokens` / `usage_json` / `credit_source`），workbuddy 路径从未跟进。

### 1.1 实锤数据（`data/codebuddy_gateway.db`）

| provider | 日志行数 | cache_read>0 的行 | usage_json 非空 |
|---|---|---|---|
| traesolo | 1420 | 1389 ✅ | 3 |
| **workbuddy** | 1769 | **0** ❌ | **0** |
| traework | 45 | 0 | 0 |

### 1.2 根因

`upstream/proxy.py` 的 `_log_request()`（workbuddy 的日志写入路径）只提取了 usage 的 4 个字段：

```python
# 现状（成功路径三个调用点均如此）
observer.usage.get("prompt_tokens", 0),
observer.usage.get("completion_tokens", 0),
observer.usage.get("total_tokens", 0),
observer.usage.get("credit", 0),
```

而 SSE observer 本身**已经把上游 usage 事件整体收进了 `self.usage`**（`upstream/proxy.py` `_ChatStreamObserver.observe_event` 内 `self.usage.update(event_usage)`），数据在手却被丢弃。log_data 构造时 `cache_read_tokens` / `cache_creation_tokens` / `usage_json` / `credit_source` 四个键都没传给 `record_request`。

### 1.3 基础设施现状（均已就绪，无需改动）

- **DB 列**：`logs.cache_read_tokens` / `cache_creation_tokens` / `usage_json` / `credit_source` 由 `_migrate_logs_cache_tokens` 迁移早已建好；
- **写入**：`storage/database.py record_request()` 已支持这四个键（`data.get("cache_read_tokens", 0)` 等缺省 0 / None）；
- **聚合**：`get_provider_model_usage()`（本会话早前新增，重构后位于 `storage/database.py`）已聚合 cache 字段并计算 `cache_hit_ratio = cache_read / (prompt − cache_read + cache_read)`；
- **Dashboard**：`get_stats().daily` 已有 `cache_tokens` 与 `cache_status`（accurate / partial / approx / empty）。

即：**数据侧只缺 workbuddy 写入这一环**，补上后下游统计即刻生效。

### 1.4 次要问题

`upstream/responses.py` `response_usage()` 把 Responses API 的 `input_tokens_details.cached_tokens` 与 `output_tokens_details.reasoning_tokens` **硬编码为 0**：

```python
"input_tokens_details": {"cached_tokens": 0},
"output_tokens_details": {"reasoning_tokens": 0},
```

即使上游 usage 带真值也会被抹掉（Codex 等走 Responses API 的客户端永远看不到 cache 命中）。

### 1.5 关键未知（实测前无法确定）

WorkBuddy 上游（copilot.tencent.com `/v2/chat/completions`）SSE usage 载荷是否携带 cache 字段、何种风格。已知三种可能：

| 风格 | 字段 |
|---|---|
| Anthropic（traesolo 上游实测是这种） | `cache_read_input_tokens` / `cache_creation_input_tokens` |
| OpenAI | `prompt_tokens_details.cached_tokens` |
| DeepSeek | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` |

traesolo 一节（关联文档 §10）确认同系上游"token_usage 事件原生携带 cache 字段"，workbuddy 走 copilot.tencent.com 不同端点，不能直接推断。**因此修复策略必须兼容三种风格 + 整包 dump 兜底**。

## 二、设计

### Part 1：usage 提取与落库（核心，`upstream/proxy.py`）

**1.1 新增模块级纯函数**

```python
def _extract_cache_tokens(usage: dict | None) -> tuple[int, int]:
    """从上游 usage 提取 (cache_read, cache_creation)，兼容三种字段风格。"""
```

- 优先级：Anthropic → DeepSeek → OpenAI（`prompt_tokens_details.cached_tokens` 记为 cache_read，cache_creation 记 0）
- 全部缺省返回 `(0, 0)`
- 负值 clamp 到 0；`cache_read` 不超过 `prompt_tokens`（cache_read 是 prompt 的子集，防脏数据）

**1.2 `_log_request` 签名扩展（向后兼容）**

```python
def _log_request(..., increment_usage: bool = True, usage: dict | None = None):
```

log_data 新增键：

| 键 | 值 |
|---|---|
| `cache_read_tokens` / `cache_creation_tokens` | `_extract_cache_tokens(usage)` 结果 |
| `usage_json` | 整包 `json.dumps(usage)`（截断保护：序列化后 >64KB 时只存提取结果并置 `{"truncated": true}`；正常 SSE usage 仅几十字节不会触发） |
| `credit_source` | usage **存在任意已知 cache 键** 时标 `'live'`；否则 `None`（但照存 `usage_json` 留证据） |

> `credit_source='live'` 门槛说明：与 traesolo 的 "`cache_read > 0` 才标 live" 略有差异。这里按"键存在"而非"值>0"判定，是为了与 dashboard `cache_status='accurate'` 语义对齐——只有上游真的发了 cache 字段才算"实测"，避免"上游根本不发 cache 字段"的数据被误标为 accurate。该差异是刻意设计。

**1.3 更新全部调用点**

`_stream_upstream`（成功 / eof_error / 网络错误 / 重试 pending 日志 / final_failure）与 `_collect_stream`（非流式成功）共约 6 处，把 `observer.usage` 或局部 `u` dict 以 `usage=` 传入；错误路径（token=0）传 `None` 或实际 usage，按现场最小改动。

### Part 2：Responses API 透传（`upstream/responses.py`）

`response_usage()` 改为取真值：

```python
cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") \
         or usage.get("cache_read_input_tokens") or 0
reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
```

替换两处硬编码的 0。

### Part 3：前端展示（`web/index.html` usg 组件）

- 「用量统计」明细表增加 **Cache 命中** 列：明细行显示 `tok(cache_read_tokens)`，summary 行显示 `cache_hit_ratio` 百分比
- 顶部汇总卡片副行加 "cache 命中 xx%"（`data.totals.cache_hit_ratio`）

`get_provider_model_usage()` 已返回全部所需字段，纯前端改动。

### Part 4：测试

| 位置 | 用例 |
|---|---|
| `tests/test_core.py`（或新建 `tests/test_workbuddy_cache.py`） | `_extract_cache_tokens`：三种字段风格、混合风格取优先级、负值 clamp、cache_read > prompt 截断、空/None |
| 同上 | `_log_request` 带 usage → log_data 含 cache 字段 + `usage_json` + `credit_source='live'`；不带 usage → 回退 0/None；usage 无 cache 键 → `credit_source=None` 但 `usage_json` 有值 |
| 同上 | Responses 流转换后 `input_tokens_details.cached_tokens` 为真值（构造带 `prompt_tokens_details` 的 chat chunk） |
| `tests/test_provider_model_usage.py` | 聚合含 cache 行时 `cache_read_tokens` / `cache_hit_ratio` 正确 |

测试基建：`_install_chat_account_stream_fakes` 已捕获 `_log_request` 全部参数（`calls["logs"]`，line 285 monkeypatch），断言检查捕获的 kwargs 即可。

## 三、验证（部署后一步完成"实测"）

1. 重启网关 → 走 workbuddy 发一次**多轮对话**（第二轮起才可能命中缓存）；
2. 查最新 workbuddy 行的 `usage_json`：
   - **含 cache 键** → 字段名实锤，`cache_read_tokens` 自然累积，前端即刻可见 ✅
   - **不含** → 确认上游不发，`usage_json` 仍留有完整证据，转入 Part 5 决策；
3. 回归：全量 pytest，确认无新增失败（存量：`tests/test_traesolo.py` 别名断言 2 例失败，与本次无关）。

## 四、Part 5（可选，默认不做）：历史行反填

- 仅当实测确认上游发 cache 字段后才有依据；
- 参照 `_backfill_cache.py` 的 per-model-per-day 比例法（N≥3 用当日比例，否则 model 平均，兜底 70%）；
- **workbuddy 特殊性**：credit 来自上游真值（`observer.usage.credit`），反填只补 `cache_read_tokens` 列、**绝不动 credit**（与 traesolo 反填重算 credit 不同）；
- 独立任务，另行评估。

## 五、风险与兼容

| 风险 | 缓解 |
|---|---|
| `_log_request` 签名变更 | 新参数 keyword-only 带默认值；旧调用点与测试 stub（`lambda *_args, **_kwargs`）不受影响 |
| `usage_json` 体积 | SSE usage 是小对象（几十字节~KB）；64KB 截断保护兜底；写入走既有 fire-and-forget executor，不阻塞事件循环 |
| 口径污染 | cache_read 是 prompt 的子集（三种风格均如此），只做旁路记录，**不改** `total_tokens` / `prompt_tokens` 口径 |
| 下游误算 | `get_provider_model_usage` 的 `cache_hit_ratio` 公式已 clamp（fresh = max(0, prompt − cache_read)），脏数据不影响 |
| 触碰面 | `upstream/proxy.py`、`upstream/responses.py`、`web/index.html`、测试 ×2；不动 DB schema、不动聚合 SQL |

## 六、实施顺序

1. Part 1：`_extract_cache_tokens` + `_log_request` 扩展 + 调用点更新
2. Part 2：`responses.py` 真值透传
3. Part 4：测试补齐
4. Part 3：前端 Cache 命中列
5. 全量回归 + 部署实测（第三节）

## 七、遗留决策点

| # | 决策 | 当前倾向 |
|---|---|---|
| 1 | `credit_source='live'` 门槛：按"usage 含任意已知 cache 键"还是与 traesolo 一致按"cache_read>0" | 前者（见 2.1 门槛说明） |
| 2 | Part 5 历史反填：是否按默认 70% 比例立即反填历史 workbuddy 行 | 不做，等实测证据 |
