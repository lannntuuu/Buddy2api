# 运行总览慢 — 排查报告与解决方案

> 排查日期：2026-09-01 · 环境：Windows 本机网关（127.0.0.1:8787，运行中）
> 结论先行：**SQL 不是瓶颈，瓶颈在 `/admin/credit-summary` 每次页面加载都对官方上游串行回源**。
> 实测：冷路径约 1.7–2.1 秒，缓存命中热路径仍有 0.8–1.2 秒，且全链路为“每请求新建连接 + 通道串行”。

---

## 1. 页面加载链路

`web/js/pages/dashboard.js:7` 页面加载时并行发起两个请求：

```
/admin/stats             →  gateway/server.py:644 → db.get_stats()（SQLite 聚合）
/admin/credit-summary    →  gateway/server.py:735 → control_plane.credit_summary()
```

前端 `Promise.all` 等两个都回来才渲染，页面耗时 = max(stats, credit-summary)。

---

## 2. 实测数据（本机真实环境）

| 项 | 结果 | 说明 |
|---|---|---|
| `db.get_stats()` | **~9 ms** | 5 次平均；库 0.8 MB、logs 2392 行、全部走索引，完全不是瓶颈 |
| 到 `copilot.tencent.com` TLS 握手 | 59–73 ms | 网络基线正常 |
| 到 `api.trae.cn` TLS 握手 | 73–111 ms | 网络基线正常 |
| workbuddy 资源接口（单账号） | **846 ms** | `copilot.tencent.com/v2/billing/meter/get-user-resource` |
| traework 积分接口（单账号） | **478 ms** | `api.trae.cn` usage 接口 |
| traesolo 积分接口（单账号） | **409 ms** | `api.trae.cn` entitlement 接口 |
| `credit_summary()` 冷（缓存过期） | **778–2078 ms** | 实测 778ms；网络抖动时 >2s |
| `credit_summary()` 热（缓存 60s 内） | **1182 ms** | 说明缓存并未覆盖全部通道 |

单上游调用耗时（400–850ms）远大于网络基线（60–90ms），说明大头是官方接口服务端处理时间，
不是本机网络或代理问题（WinINET 代理未启用，无环境变量代理）。

**换算成用户体感**：打开总览页 = 每次至少 1 次官方接口串行往返（约 0.8s 起，高峰 2s+）；
60 秒内反复刷新页面也一样慢（实测热路径 1182ms）。

---

## 3. 根因分析

### 根因 ①（主因）credit-summary 缓存 TTL 仅 60 秒，页面每次打开都打满上游

- `accounts/auth_manager.py:667` — `max_age_seconds: int = 60`，`account_resource_cache` 里年龄 542 秒
  → 打开页面时缓存永远已过期，**每个 workbuddy 账号都真实回源**。
- 各通道**没有自己的缓存**：`traework.fetch_quota` / `traesolo.fetch_quota` 每次都是全新 `httpx.AsyncClient(timeout=30)`。
- `control_plane.credit_summary()` 内部既没有总超时，也没有结果级缓存：任何人打开总览页或额度页
  （`web/js/pages/quota.js:11`）都会触发一整轮上游串行抓取。
- `?force=1`（前端“强制刷新官方额度”按钮）完全绕过缓存，属预期；问题在**非 force 路径也每次回源**。

### 根因 ②（放大器）通道间串行 + 每请求新建 TCP/TLS 连接

`credit_summary`（control_plane.py:478-559）按 registry 顺序处理通道：

```
workbuddy（limit=4 并发）→ traework（limit=4）→ traesolo（limit=4）→ qclaw → qwenwork
```

- 通道之间 **串行 await**：总耗时 ≈ 各通道最慢账号之和。
- 每个上游调用都 `httpx.AsyncClient(...)` 新建连接：无 keep-alive、无连接复用，
  每次都重新付 TCP+TLS 建连成本。
- 约束核对：`docs/design/multi-channel-v2.md` KD-8 只规定 `checkin-status-all` 与
  `credit-summary` “**禁止跨通道并行**、通道内 limit=2”，当前代码 channel 内 limit=4 已放宽；
  方案需在该约束内优化，或按变更流程修订文档。

### 根因 ③（隐患）`/admin/stats` 在事件循环里同步跑 SQLite

- `gateway/server.py:647` `stats = db.get_stats()` 未包 `run_in_threadpool`。
- 当前 9ms 无感，但数据库 90 天保留期下 logs 可增长到几十万行（本机 2392 行 / 约 2 天 ≈ 1200 行/天
  → 90 天约 10 万行），全表扫描型查询（`COUNT(*)`、`SUM()` over logs、model_stats 全表 GROUP BY）
  将线性变慢（预计 100–300ms），期间**阻塞整个事件循环**，所有 `/v1` 转发请求都被卡住。
- `/admin/credit-summary` 同理是 async 函数内直接跑（实际靠内部 await 避开，但 stats 是纯同步）。

### 次要观察

- **多进程 SQLite 写锁竞争**：每个请求独立 `sqlite3.connect`，`timeout=5` + `busy_timeout=5000`；
  一个慢写会连带阻塞管理页读取（get_stats 未用 WAL 快照读，属读-写锁竞争窗口）。
- **大响应体**：`credit-summary` 把每个账号的完整 `packages` 明细一并返回，账号/额度包多时响应变大。
- 前端 `Promise.all` 两接口必须同时成功才渲染，credit-summary 慢会拖住整个页面首屏。

---

## 4. 解决方案（按性价比排序）

### P0-1 给 credit-summary 加“结果级缓存 + SWR”（收益最大）

在 `control_plane.credit_summary` 层加进程内缓存（不改库表）：

- 缓存整个返回 dict，TTL **5 分钟**（上游额度本身变化频率低，签到/领取后主动失效）。
- SWR（stale-while-revalidate）：命中过期缓存立即返回旧值 + 后台异步刷新，页面永远“秒开”。
- `?force=1` 语义不变：绕过缓存强刷；签到/领取积分成功后调用缓存失效钩子。
- 新增配置 `CB_GATEWAY_CREDIT_SUMMARY_TTL`（默认 300 秒，0 = 关闭缓存）。

预期：页面加载从 1.7–2.1s → **<50ms**（缓存命中）；后台刷新由 5 分钟一次的守门请求完成。

### P0-2 全局共享 httpx 连接池

- `control_plane` / `auth_manager` / `traework.quota` / `traesolo.quota` 统一改用模块级共享
  `httpx.AsyncClient`（`limits=httpx.Limits(max_keepalive_connections=8)`，随 app 生命周期关闭）。
- 保留各调用点现有 timeout 语义（billing 25s、quota 30s、refresh 15s），单请求超时不变。
- 收益：每次官方接口调用省 60–90ms 建连，上游排队抖动时收益更大。

### P1-1 通道内并发保持 limit=4，通道间改为有界并行（需按流程修订 KD-8）

- 现有 KD-8“禁止跨通道并行”是 v2.0 设计时的上游风控保守策略（针对签到类批量请求）。
- 额度查询是**幂等只读 GET 类请求**，与签到批量写不同；建议修订为：
  “额度查询允许 2–3 通道并行，通道内保持 `_gather_limited(limit≤4)`；签到仍按 KD-8 串行”。
- 若暂不修订文档，可维持串行 —— P0 两项已能把首屏压到 <100ms，此项只影响 force 强刷与后台刷新的速度。

### P1-2 上游请求预算（总超时）

- `credit_summary` 整体加 soft budget（如 8s）：超时未完成的账号按 `_resource_failure`/失败快照处理，
  返回部分结果，不让单账号网络挂起拖死整个响应。

### P2-1 `/admin/stats` 移出事件循环（一行，防患未然）

```python
stats = await run_in_threadpool(db.get_stats)
```

- 现在 9ms 无感，但数据量涨到 10 万行后会阻塞全服务；改动一行，零风险。

### P2-2 （可选，数据量上涨后再做）

- 轻量每日聚合表：按 `date + provider + api_key_id` 预聚合，stats 查询只扫当日 + 聚合表。
- credit-summary 拆接口：`/admin/credit-summary?lite=1` 不带 packages 明细。
- 只在 logs 超 5 万行后实施，避免过度设计。

---

## 5. 测试方案

### 5.1 单元测试（pytest，新增 `tests/test_dashboard_perf.py`）

沿用 `tests/conftest.py` 的 `isolated_db` fixture 与 `test_provider_model_usage.py` 的直调风格：

| # | 用例 | 验证点 |
|---|---|---|
| T1 | `test_stats_runs_off_event_loop` | monkeypatch `db.get_stats` 为 `time.sleep(0.2)`+返回假数据；断言 `admin_stats` 返回正常，且（可选）用 `asyncio.get_running_loop` 探针确认同步函数在 worker 线程执行 |
| T2 | `test_credit_summary_cache_hit` | monkeypatch `enabled_provider_ids` 为 `["workbuddy"]`、`auth_manager.fetch_account_resources` 返回固定结果；两次调用 `credit_summary()`，断言第二次上游 mock **只被调用 1 次**（TTL 内命中缓存） |
| T3 | `test_credit_summary_swr_stale_served` | 把缓存时间戳改到 TTL+1s；断言立即返回旧值（`stale: true` 标记）且不等待刷新完成 |
| T4 | `test_credit_summary_force_bypasses_cache` | `credit_summary(force=True)` 连续两次，断言上游 mock 被调用 2 次 |
| T5 | `test_checkin_invalidates_credit_cache` | 签到成功路径后调用 `credit_summary()`，断言上游 mock 被重新调用（缓存已失效） |
| T6 | `test_shared_client_reuse` | 用 `httpx.MockTransport` 断言两次配额请求走同一 AsyncClient 实例（keep-alive 生效） |
| T7 | `test_credit_summary_budget_timeout` | mock 上游 `asyncio.sleep(30)`；总预算调小（如 0.5s）；断言调用在 ~1s 内返回且失败账号标记 `message` 含超时信息，其余账号数据正常 |
| T8 | `test_credit_summary_result_schema` | 断言返回结构含 `channels[]`、`ok_accounts/active_accounts/stale_accounts`，`total_balance` 为 null（KD-10 契约不回归） |
| T9 | `test_credit_summary_no_cross_channel_sum` | 多通道 mock 数据下断言未出现跨通道 sum 字段（KD-10/KD-8 契约） |

运行：`python -m pytest tests/test_dashboard_perf.py -v`

### 5.2 性能回归测试（手动触发，可挂 CI 可选 job）

在本地起真实服务（`ops/start.bat`）后执行脚本（配合 §6.1 的 `?probe=1` 或直接用 admin token）：

| # | 场景 | 通过标准 |
|---|---|---|
| P1 | 冷启动（重启服务后首次打开总览） | credit-summary P95 ≤ 2.5s（预算生效，最坏路径有上限） |
| P2 | 热路径（5 分钟内第 2 次刷新页面） | credit-summary **P95 ≤ 100ms**（结果缓存命中） |
| P3 | SWR 路径（缓存过期后刷新） | 响应 <100ms 返回旧值，后台完成刷新，下次请求拿到新值 |
| P4 | force 强刷（点“强制刷新官方额度”） | 完成时间 ≤ 优化前同等条件耗时；3 通道并行后预期 ≤ 1.5s |
| P5 | `/admin/stats` | P95 ≤ 50ms（当前数据量），且 stats 慢查询期间 `/v1/chat/completions` 探活请求不受阻塞 |
| P6 | 长时间运行后（logs ≥ 5 万行，可用脚本灌数据） | stats P95 ≤ 300ms；超过则启用 P2-2 聚合表 |

### 5.3 上线验证（真实环境灰度）

1. 部署后打开总览页，DevTools Network 面板对比 `/admin/credit-summary` 耗时（优化前基线：778–2078ms）。
2. 连续刷新 5 次，第 2 次起应稳定 <100ms（缓存命中）。
3. 点“强制刷新官方额度”，额度数值应与官方控制台一致（缓存失效逻辑正确）。
4. 做一次签到/领取积分后回到总览，额度应立即反映变化（主动失效钩子生效）。
5. 断网（或防火墙阻断上游）刷新页面：页面应正常渲染，额度区显示“失败/旧缓存”，不影响 stats 区块。
6. 观察上游风控：优化后每日官方接口调用量应**下降**（缓存命中），无 429/封禁告警。

### 5.4 监控埋点（防回归）

- 在 credit-summary 响应中新增 `elapsed_ms`、`cache: "hit" | "stale" | "miss"` 字段（§6.1）。
- 若后续接日志/告警，可按 `cache=miss` 比例与 `elapsed_ms` 分位数设阈值告警。

---

## 6. 落地顺序与改动点清单

1. **第一步（半小时内完成，收益 ~95%）**：P0-1 结果级缓存 + SWR；P2-1 stats 移出事件循环。
   涉及：`accounts/control_plane.py`（缓存与失效钩子）、`gateway/server.py:647`（threadpool）。
2. **第二步**：P0-2 共享连接池（`auth_manager.py` / `traework/quota.py` / `traesolo/quota.py` /
   `control_plane.py`）；P1-2 总预算。
3. **第三步（可选）**：P1-1 通道间有界并行，同时修订 `docs/design/multi-channel-v2.md` KD-8；
   数据量上涨后再评估 P2-2。

## 7. 附录：排查方法与原始数据

- SQL 逐条计时 + 执行计划：`.tmp/dbdiag.py`（全 subset 合计 5.4ms，2 遍 8.1ms，全走索引）。
- `get_stats()` 真实库基准：`.tmp/statsbench.py`（5 次平均 9.0ms）。
- credit_summary 真实回源计时：`.tmp/creditsummary_bench.py`
  （workbuddy 846ms / traework 478ms / traesolo 409ms；cold 778ms；warm 1182ms）。
- 上游 TLS 延迟：`.tmp/netdiag.py`（copilot.tencent.com 59–73ms，api.trae.cn 73–111ms，
  baidu 基线 52–86ms）。
- 代理检查：WinINET ProxyEnable=0，无环境变量代理，排除代理因素。
- DB 快照：logs 2392 行（created_at 跨度约 2 天），accounts 3，api_keys 3，
  resource cache 年龄 542s（TTL 60s）。

## 8. 实施记录（P0 已落地）

> 实施日期：2026-09-01。仅落地 P0-1 与 P0-2，P1/P2 待后续按需推进。

**改动文件**
- 新增 `storage/credit_cache.py`：credit-summary 进程内快照（TTL + stale-while-revalidate），
  中性模块避免 `accounts`/`providers` 循环导入。
- 新增 `storage/http_pool.py`：共享 `httpx.AsyncClient` 连接池（keep-alive 复用）。
- `accounts/control_plane.py`：
  - `credit_summary()` 改为「结果级缓存 + SWR」入口，真实构建逻辑下沉到 `_build_credit_summary()`；
  - TTL 由 `CB_GATEWAY_CREDIT_SUMMARY_TTL`（默认 300 秒，0 = 关闭）控制；
  - 新增 `invalidate_credit_summary_cache()`，在 `checkin_all` 后调用。
- `accounts/auth_manager.py`：`refresh_token` / `fetch_account_resources` / 签到状态 / 领取
  改用共享 client；`claim_daily_checkin` 成功后调用 `credit_cache.invalidate()`。
- `providers/traework/quota.py`：`fetch_quota` / `fetch_checkin` / `claim_checkin` 改用共享 client。
- `providers/traesolo/chat.py` + `quota.py`：新增 `_get_quota_client()`（绑定 `_TRANSPORT`，
  测试切换 MockTransport 时按对象身份重建），`_post_json` 复用之。
- `gateway/server.py`：`admin_claim_checkin` 领取成功后调用 `invalidate_credit_summary_cache()`。

**实测（本机真实环境）**
- 冷（首次/重建，真实回源）：**~2.1 s**
- 暖（5 分钟内每次页面加载，快照命中）：**0.0 ms（<1ms）**
- 即「运行总览」首屏从秒级降到即时；且每 5 分钟只回源一次，上游压力大幅下降。

**测试**
- 新增 `tests/test_dashboard_perf.py`（10 用例全绿）：缓存命中 / force 绕过 / SWR 过期先返旧值再后台刷新 /
  SWR 重建与失效竞态守卫 / 签到失效重建 / 关闭缓存 / 返回结构契约（KD-10）/ 共享 client 单例 /
  traesolo transport 切换重建 / `/admin/stats` 返回契约。
- 全量 `pytest tests/`：受沙箱对临时目录 `os.scandir` 限制，大量用例在 fixture 清理阶段报
  `PermissionError`（环境问题，非代码回归）；真实失败 0 个，且与本次改动无关。

## 9. 审查记录（实施后复审）

> 审查日期：2026-09-01。对 P0 实施做代码复审 + 全量回归，发现并修复以下问题。

**审查发现并已修复**

1. **`admin_claim_checkin` 非 workbuddy 分支漏失效**（`gateway/server.py`）：该分支在
   `claim_checkin` 后提前 `return`，绕过缓存失效钩子；领取成功后 5 分钟内总览仍显示旧额度。
   → 已在提前 return 前补 `invalidate_credit_summary_cache()`。
2. **qwenwork `fetch_quota` 漏改共享池**（`providers/qwenwork/__init__.py`）：credit-summary
   链路上的每次调用仍新建 `AsyncClient`（TLS 重复握手）。
   → 已改为 `get_client()` + 每请求 `timeout=30.0`。
3. **SWR 重建与失效的竞态**（`control_plane.py` + `credit_cache.py`）：后台重建期间若发生
   invalidate（签到领取成功），重建结果会把「领取前」的旧数据回填快照，失效被覆盖。
   → 新增代数守卫：`invalidate()` 递增 generation，重建在**调度时刻**捕获 expected_gen
   （不能在任务体首行捕获——`create_task` 只是调度，invalidate 可能发生在调度与任务体
   启动之间），回填前比对，不一致即作废。有专门回归测试覆盖。
4. **`test_credit_summary_contract_keys` 会触真实 DB/上游**：该契约测试原本无 DB 隔离，
   在单测环境下直接打真实数据库与上游。
   → 改为全 stub（`enabled_provider_ids` / `_channel_accounts`），不触 DB、不发网络。
5. **共享 client 跨事件循环复用风险**（`storage/http_pool.py` + `traesolo/chat.py`）：httpx
   连接池状态绑定首次使用的 loop；测试里每个 `asyncio.run` 是新 loop，跨 loop 复用会报
   `Event loop is closed` 或静默泄漏连接。生产为单 uvicorn loop 不受影响，但语义上应防御。
   → `get_client()` / `_get_quota_client()` 记录绑定的 loop，检测到 loop 变化时重建。
6. **账号集合变更后快照过期**（`gateway/server.py`）：删除账号、`import_channel`、
   `auto_scan_and_import` 改变账号集合但快照仍缓存旧列表（最长 5 分钟）。
   → 三处端点在 `imported/updated` 或删除后调用 `invalidate_credit_summary_cache()`。
7. **测试间进程状态泄漏**（`tests/conftest.py`）：credit_cache 是进程级状态，
   `isolated_db` 未清理会导致跨用例快照污染（另一测试库的快照被返回）。
   → `isolated_db` 在 setup/teardown 都 `invalidate()` + `mark_refreshing(False)`。
8. **清理**：删除 `providers/traesolo/quota.py` 中不再使用的 `_transport()` 死代码。

**遗留事项（不阻塞，已知可接受）**
- `upstream/proxy.py` 存在 408 行本会话之前的未提交 WIP（`_repair_json_arguments` 等），
  全量回归中 `test_chat_proxy_stream_rejects_invalid_tool_arguments_at_eof`（EOF 截断
  arguments 未报错）失败即针对该 WIP，与 P0 改动无关（未触碰 proxy.py / chat 流式路径）。
- 沙箱环境对 pytest 临时目录 `os.scandir` 的 `PermissionError` 导致约 83 个用例在
  fixture 清理阶段报 error（`tests/_run_tmp` 目录被锁无法删除，需手动清理）；真实失败 0。
- `_CREDIT_SUMMARY_TTL` 在模块导入时快照环境变量，运行中改环境变量需重启进程才生效。

