# 性能与代码异味优化 · 实施 spec (EXECUTION SPEC)

> 本文件是**自包含、可由 subagent 直接执行**的实施规范,subagent 不需要回看主对话。
> 来源:一轮全仓库代码审查(3 个审查视角 + 人工复核,P0/P1 均已核实到行)。
> 分支:`perf/optimization`。
>
> **红线**:
> 1. 中文文案零 em-dash。
> 2. 只允许修改本 spec 分配给你的文件;**禁止**改动 `tests/` 下已存在的测试文件;新增测试一律放到自己的新测试文件。
> 3. **禁止 git commit / git add**,只改文件,由主控统一提交。
> 4. 禁止顺手重排 import、重排格式等与条目无关的 diff。
> 5. 收工前必须跑通完整测试套件(命令见 §5);跑不过就修到过,修不动就把该条目回退并在总结中说明。

---

## 0. 背景与架构速览

- FastAPI 异步网关:`python -m gateway.server`(src/ 布局),`/v1/chat/completions`、`/v1/responses` 两个热入口。
- 请求链路:`gateway/routers/v1.py`(鉴权/配额/绑定)→ `gateway/router.py`(bind)→ `upstream/proxy.py`(workbuddy)或 `providers/openai_compat.py` 及各 provider(qclaw/qwenwork/traesolo/traework)→ 上游 SSE。
- 存储:同步 sqlite3(src/storage/repos/),WAL 模式,全局 `_lock` 串行化写。
- **已存在的复用设施**(直接用,不要重复造):`storage/http_pool.get_client()`(进程级 httpx 连接池,绑定事件循环)、`storage/credit_cache.py`(SWR 结果缓存)、`providers/store_common.py`、`providers/trae_shared.py`。
- 审查确认的既有优点(保持不动):openai_compat 的长连接 `_get_client()`、有界退避重试、8MiB SSE 事件上限、`record_request` 单事务三联写。

### 全局文件归属(并行工作流分区,避免编辑冲突)

| 工作流 | 独占文件 |
|---|---|
| WS-A | `src/upstream/proxy.py`、`src/upstream/responses.py`、新建 `src/upstream/sse.py`、新建 `tests/test_perf_upstream.py` |
| WS-B | `src/providers/qclaw/**`、`src/providers/qwenwork/**`、`src/providers/traework/**`、`src/providers/store_common.py`、`src/providers/trae_shared.py`、新建 `tests/test_perf_providers.py` |
| WS-C | `src/providers/traesolo/**`、`src/providers/openai_compat.py`、新建 `tests/test_perf_traesolo_compat.py` |
| WS-D | `src/storage/**`、`src/gateway/**`、`src/accounts/auth_manager.py`、新建 `tests/test_perf_storage.py` |

跨工作流依赖为零;不得触碰对方文件。`providers/retry.py` 所有工作流都只读。

---

## WS-A · upstream 热路径(P0 一处 + P1 四处 + 异味)

### A1 [P0] 11128 自愈路径 NameError(proxy.py:724-725, 897-898)

`proxy.py` 使用了从未导入的 `_COMPACTION_LOCK`、`_COMPACTION_STATS`(它们定义在 `upstream/compaction.py:24,26`),11128 触发时必抛 NameError,流式路径直接炸断 SSE。`compaction.py:66-68` 已提供等价的 `_record_11128_retry()`。

修法:
1. 在 proxy.py 现有的 `from upstream.compaction import (...)` 块中加入 `_record_11128_retry`。
2. 两处 `with _COMPACTION_LOCK: / _COMPACTION_STATS["retried_11128"] += 1` 整体替换为 `_record_11128_retry()`。

验收:新测试文件中加一条回归测试,直接调用 compaction 的统计入口前后对比 `compaction_stats()["retried_11128"]` 计数 +1;并断言 `from upstream import proxy` 可正常 import(防再犯)。

### A2 [P1] workbuddy 链路每请求/每重试新建 AsyncClient(proxy.py:885 附近 `_stream_upstream`、proxy.py:1144 附近 `_collect_stream`)

现状 `async with httpx.AsyncClient(timeout=timeout) as client:` 每次新建,放弃 keep-alive,同文件 openai_compat 早已复用。修法:改用 `storage.http_pool.get_client()`,**不要**再用 `async with` 包 client(否则会关掉共享池),per-request 超时通过 `client.stream("POST", url, ..., timeout=timeout)` / `client.post(..., timeout=...)` 传入,保持现有 `httpx.Timeout(connect=10, read=..., write=30, pool=10)` 语义不变。`_collect_stream` 的 `timeout=auth_manager.request_timeout(300)` 同样改为 per-request 传参。

注意:先 `grep -n "AsyncClient" tests/test_core.py tests/test_workbuddy_cache.py`,若现有测试 monkeypatch 了 `httpx.AsyncClient`,以不破坏测试为准(必要时给 proxy 留一个 `_client_factory` 模块级可 patch 点,默认返回 http_pool.get_client())。

### A3 [P1] SSE 行扫描逐字节循环(proxy.py `_SSEEventDecoder._take_line`,约 509-525)

现状 `for index, value in enumerate(self._buffer): if value == 0x0A: ...` 是全仓库最热循环。修法:用 `bytes.find()` 定位:

```python
def _take_line(self, *, final: bool = False) -> bytes | None:
    buffer = self._buffer
    i_lf = buffer.find(b"\n")
    i_cr = buffer.find(b"\r")
    # 选取先出现的分隔符;两者都缺失时按 final 语义返回
```

语义必须与旧实现**逐位一致**,特别保留这些边界:
- 先遇到 LF:取 `buffer[:i]`,消费到 i+1,行尾 `\r` 剥掉;
- 先遇到 CR:若 CR 是 buffer 最后一个字节且 `final=False` → 返回 None(等待下一个 chunk 判断是否 CRLF);否则消费 CR 以及紧随的 LF(若有),返回 `buffer[:i]`;
- `final=True` 且无分隔符:返回整个 buffer 并清空。
为新测试文件补齐表驱动单测:LF、CRLF、 lone CR 结尾(final 真/假)、多行同 buffer、final flush、8MiB 超限路径不受影响。

### A4 [P1] responses.py 每 delta 全量重建 O(n²)(约 846-852, 918-919)

现状:`state["text"] += text` 后立即 `state["item"]["content"] = [{"type": "output_text", "text": state["text"], ...}]`,每个文本/参数 delta 都把累积全文重新拷贝进 item。修法:per-delta 只做 `state["text"] += text`;`item["content"]` / `item["arguments"]` 的全文回写只在 `output_item.added`(首帧)与该 item 的 `done`/关闭事件(末帧)时各做一次。先读清上下文(约 780-1013)确认哪些出站事件携带 item 全文,改动后出站字节流语义不变(以 tests/test_core.py 的 responses 桥测试为准)。

### A5 [P1] SSE 解析器三份并存 → 收敛为 `src/upstream/sse.py`

`proxy._SSEEventDecoder`(约 462-554)、`responses._iter_chat_sse_data`(约 558-638)、`_collect_stream` 的手写 aiter_lines 循环(约 1130-1237)是三份同构实现,已发生漂移。修法:把 `_SSEEventDecoder`(含 A3 的 find 优化)提为 `upstream/sse.py` 的唯一实现,提供 bytes/str 混合输入的 `feed()`/`finish()` 入口;`responses.py` 改用它并删除 `_iter_chat_sse_data`;`_collect_stream` 改为复用同一 decoder(保留其聚合组装逻辑)。
**此项排最后做**:A1-A4 全部绿灯、完整测试通过之后再动;若重构中发现行为差异无法在测试内对齐,允许保留 `_collect_stream` 现状并在总结中说明,但 `_iter_chat_sse_data` 必须收敛。

### A6 [P2] 重试常量与退避公式双份

`proxy.py` 的 `RETRYABLE_STATUS_CODES`/`_retry_delay` 与 `providers/retry.py` 的 `RETRYABLE_STATUS`/`retry_delay` 重复(公式同为 0.25s×2^n 封顶 2s),且 proxy 版多含 {401,403}。修法:proxy.py 改为 `from providers.retry import RETRYABLE_STATUS, retry_delay`,`RETRYABLE_STATUS_CODES = RETRYABLE_STATUS | {401, 403}`(401/403 仅参与账号 failover,不参与同账号重试,保持现语义),删除 `_retry_delay`。先 grep tests 确认没有直接引用 `proxy._retry_delay`。

### A7 [P2] proxy.py 死代码与吞异常

- 删除未使用的 `import threading`、`from pathlib import Path`(以 grep 全文件确认无引用为准)。
- `_log_request` 里 `except Exception: pass` 改为 `logger.debug("log enqueue failed", exc_info=True)`(沿用该模块现有 logger)。
- responses.py 约 213-217 的 `if tool_choice and isinstance(tool_choice, str): pass` 死分支:删除该 if,仅保留 `if tool_choice is not None: chat_payload["tool_choice"] = tool_choice`。

### WS-A 明确不做

- `proxy.py:1023-1036` eof_error 触发 `mark_account_failure(502)` 的语义调整(涉及故障分类行为变更,本轮不动,见 §6)。
- `_known_cache_keys`/credit_source/executor 写日志三件套与 openai_compat 的去重(跨 WS 文件,见 §6)。

---

## WS-B · qclaw / qwenwork / traework + 共享模块

### B1 [P1] chat/refresh 热路径每请求新建 AsyncClient

位置:`qclaw/chat.py:155`(及 210 附近第二处)、`qwenwork/chat.py:384`、`traework/chat.py:272`、`qclaw/jprx.py:99`、`qwenwork/token.py:60`、`traework/token.py:129`。全部改走 `storage.http_pool.get_client()`,去掉 `async with client` 外壳,per-request 超时经 `timeout=` 传参保持原值。参考同仓库 `traework/quota.py:34` 已有的正确用法。注意 grep 现有 provider 测试是否 monkeypatch `httpx.AsyncClient`(test_traework.py、test_custom_channels.py 等),不得破坏;必要时保留模块级 `_client_factory` patch 点。

### B2 [P1] async `_log` 里同步写 sqlite 阻塞事件循环

`qclaw/chat.py:94 附近`、`qwenwork/chat.py:69 附近`、`traework/chat.py:231 附近` 的 `_log` 在协程内直接调 `db.record_request`(BEGIN IMMEDIATE 写事务)。修法:`await asyncio.to_thread(db.record_request, row)`。三处的 `_log` 同时提取为 `store_common.log_request(api_key_info, account, *, channel, model, stream, usage, finish_reason, status_code, duration_ms, error_msg, **extra)` 一份实现(逐字对齐 qclaw 版语义,含 extract_cache_tokens 与 usage_json 截断),qclaw/qwenwork/traework 三家改调用;**traework 版现把 token 全部硬编码 0,属漂移 bug,按 qclaw 版语义修复**。`_log` 内成对的 `except Exception: pass` 保留兜底但加 `logger.debug(..., exc_info=True)`。

### B3 [P1] `_pick` 兜底路径无负缓存 + 三份复制

`qclaw/chat.py:148,210`、`qwenwork/chat.py:339-357`、`traework/chat.py:216-226`:expired 账号逐个发 refresh 请求,失败后下个请求原样重放。修法:在 `trae_shared.py` 新增共享的 `pick_with_refresh_fallback(channel_id, refresh_fn, *, exclude_ids=None)`(内部含 refresh 失败的 TTL 负缓存,例如 60s 内不重试同一账号,参考 `traesolo/chat.py:693-694` 的 `last_fail_at` 模式),三家 `_pick` 收敛为调用它。`qwenwork/chat.py:341-344` 的裸 `except Exception: account = None` 补 debug 日志。

### B4 [P2] 异步上下文中的同步磁盘加密写

`qwenwork/token.py:90`(以及 traework/token.py 中同型的 `write_refreshed_auth` 调用,若在协程内):改 `await asyncio.to_thread(write_refreshed_auth, ...)`。

### B5 [P2] traework 后台任务隐患

`traework/chat.py:514` 的 `asyncio.get_event_loop().create_task(...)` 改 `asyncio.create_task(...)`;模块级 `_bg_close`(chat.py:29, 390-391)只保留最后一个任务引用,改为模块级 `set` + `task.add_done_callback(tasks.discard)`。

### B6 [P2] 重复工具函数收敛(仅限 WS-B 三家)

- `is_token_expired`:`qwenwork/token.py:42-46` ↔ `traework/token.py:27-31` 逐字相同 → 迁入 `trae_shared.py`,两处改 import。
- `extra_of`:`traework/token.py:34-36` → `trae_shared.py`(traesolo 的同名函数归 WS-C,不在本轮合并)。
- `translate_model` 四份单行拷贝中的三份(qclaw/chat.py:20-22、qwenwork/chat.py:40-42、traework/chat.py:32-34):若签名一致则提为 `store_common.make_translator(aliases_fn, default_model)`,不一致就保持现状,不强扭。
- `qclaw/jprx.py:93` `import json as json_lib` 删除,直接用模块顶层的 `json`;各文件函数体内重复 import 同名库一律清理。

### B7 store 骨架去重(可选,绿灯才做)

`store_common.py` docstring 自认 discover 骨架"结构完全一致"却未收敛:`qclaw/store.py:38-45` ↔ `qwenwork/store.py:41-48` ↔ `traework/store.py:39-52` 的 `_auth_dirs` 去重块。提取 `store_common.dedupe_dirs(dirs)` 并让三家调用。discover 循环骨架四份(qclaw:177-202 等)涉及行为差异,本轮只做无风险的去重块,骨架收敛不做。

---

## WS-C · traesolo + openai_compat

### C1 [P1] `_log` 同步写 sqlite

`traesolo/chat.py:975 附近` `_log` 内 `db.record_request` 改 `await asyncio.to_thread(...)`。traesolo 的 `_log` 保持本地实现(共享版归 WS-B,避免跨工作流文件依赖),仅做线程化。

### C2 [P2] `_pick` expired-refresh 负缓存

`traesolo/chat.py:650-662`:仿同文件 `_model_cache.last_fail_at` 模式,给 refresh 失败的账号加 60s TTL 负缓存,避免失败重放。

### C3 [P2] 小修一组

- `traesolo/chat.py:767-769` `fetch_models` 全仓库零调用,删除。
- `traesolo/token.py:154` 注解引用未导入的 `httpx`:文件头加 `from typing import TYPE_CHECKING` + `if TYPE_CHECKING: import httpx`(运行时零开销)。
- `traesolo/quota.py:25` 签到请求绕过 host 覆盖:`url = f"{UG_HOST}{path}"` 改为 `url = f"{channel_host(CHANNEL_ID, 'ug_host', UG_HOST)}{path}"`(对齐 `traework/quota.py:24` 的正确写法,注意 import 路径)。
- `traesolo/chat.py:969` `import json as _json` 等函数体内重复 import 清理,用模块顶层 `json`。

### C4 [P1] openai_compat SSE 热路径

`openai_compat.py:398-431`:
- 删除从未使用的 `buffer = b""` 与 str 分支 `raw = text.encode("utf-8")`(每行白做一次编码)。
- 每 `data:` 行全量 `json.loads` 改为廉价预过滤:仅当行内含 `"usage"` 或 `"finish_reason"` 子串才解析;其余行直接透传。保持 usage/finish_reason 提取语义不变(tests/test_workbuddy_cache.py、test_custom_channels.py 有覆盖)。
- 同文件约 47 行 `SINGLE_ACCOUNT = True` 若确无引用,删除。

### C5 [P2] openai_compat 每请求 await 模型刷新

`openai_compat.py:464-468`:TTL 过期时每个请求同步等一次上游 `/v1/models` 往返。改为 stale-while-revalidate:首拉仍 await(冷启动);TTL 过期后请求直接用旧缓存,同时 `asyncio.create_task` 后台刷新并防并发(实例级 `_refreshing` 标志)。`ensure_env_account` 同请求内多次全表扫描:加实例级 memo(env 变量名不变即复用,提供 `_reset_env_account_cache()` 供测试与 env 变更时调用)。改动以现有测试全绿为准,若某测试依赖"过期后同步刷新",保持该测试语义(可保留首次冷同步),在总结中说明取舍。

---

## WS-D · storage / gateway / accounts

### D1 [P1] settings 读取无缓存,每请求多次新开连接

`storage/repos/settings.py:10-20`:`get_setting` 每次新开 sqlite 连接;每请求至少 3+ 次调用(workbuddy.list_models/accepts_model、model_config 渠道模型与倍率、host_override)。修法:模块级进程内缓存:

- 缓存 key 为 `(str(DB_PATH), key)`,TTL 5s;
- `set_setting`/`delete_setting` 写路径立即失效当前 DB_PATH 的全部缓存条目;
- 新增 `settings_cache_clear()`(清全部),供测试与 `CB_GATEWAY_MASTER_KEY` 类配置变更场景使用;
- `get_all_settings` 同样走缓存(整表一条目)。

**测试安全**:先 `grep -rn "INSERT INTO settings\|UPDATE settings\|DELETE FROM settings" tests/`,确认没有测试绕过 `set_setting` 直写 SQL 后立刻 `get_setting`;若有,该测试路径必须通过先调用 `db.set_setting` 同键一次或调用 `settings_cache_clear()` 保持正确(不允许改既有测试文件,若冲突无法规避则把 TTL 降到 1s 并在总结中说明)。

### D2 [P1] `get_active_accounts` 每请求全表解密

`accounts/auth_manager.pick_account`(auth_manager.py:941-963)每请求调 `db.get_active_accounts(provider)`,对每行做 Fernet/DPAPI 解密;且每请求至少两次(`ensure_usable` + chat 主路径各一次)。修法(在 `storage/repos/accounts.py` 内做,不散到调用方):`get_active_accounts` 加进程内 TTL 缓存(2s,key 为 `(str(DB_PATH), provider)`),`add_account`/`update_account`/`delete_account` 内直接失效该 DB_PATH 的全部账号缓存,并导出 `invalidate_accounts_cache()`。缓存的是 `_account_dict` 产出的 dict 列表,注意返回给调用方时做浅拷贝列表(`list(cached)`),防止调用方原地修改污染缓存。

### D3 [P1] `get_stats` 全表扫描 8 次 + admin_stats 阻塞事件循环

- `storage/repos/stats.py:158-366`:`total_requests/total_tokens/total_credit/success/error/filtered/avg_duration` 六次全表扫描合并为**一条**条件聚合 SQL(`COUNT(*)`、`SUM(total_tokens)`、`SUM(credit)`、`SUM(CASE WHEN ...)`、`AVG(CASE WHEN duration_ms IS NOT NULL ...)`),today 的四条(199-231)合并为一条,语义逐字段保持(注意 success/error/filtered 的判定条件与现 SQL 完全一致,avg 仍只对非 NULL)。hourly/daily/model_stats/key_stats/account_stats/recent_logs 保持原样。
- `gateway/routers/admin.py:597-600` `admin_stats`:`stats = db.get_stats()` 外包 `await run_in_threadpool(...)`。
- 顺带排查 admin.py 其余 async 端点里未包线程池的重 DB 调用(`admin_list_accounts` 的 `db.list_accounts()`、`admin_credit_overview`、`admin_traework_usage`、`admin_credit_summary` 等),一律 `run_in_threadpool` 包裹;`_check_admin` 等轻调用保持原样。

### D4 [P2] /health N+1 与阻塞

`gateway/routers/v1.py:32-51`:先 `db.list_accounts()` 再按 channel 逐个 `db.list_accounts(provider=...)`。改为:一次 `list_accounts()`(经 `run_in_threadpool`),Python 内按 `account.get("provider")` 分组统计,`keys` 的一次 `list_api_keys()` 同样包线程池。返回 JSON 结构保持逐字段一致。

### D5 [P2] 热路径配额释放阻塞 + 限流桶无界

- `v1.py:155,161,206,213` 的 `_release_client_quota(...)` 直接在协程内跑同步 sqlite:改 `await run_in_threadpool(_release_client_quota, ...)`(释放顺序保持在返回响应前,语义不变)。
- `gateway/deps.py` `_usage_rate_bucket`/`_login_failures` 只增不减:各加一个 `_prune_stale(bucket, now)` 帮助函数,在写入路径调用,剔除队首已过期且为空的 key,超过 4096 个 key 时强制全量清扫一次。

### D6 [P2] 死代码

- `storage/repos/logs.py:13-44` 的 `migrate()`:全仓库无调用(init_db 只调 migrate_provider/migrate_cache_tokens/migrate_reasoning/migrate_client),删除。
- `storage/repos/logs.py:89-121` 的 `add_log()`:全仓库无调用(record_request 覆盖),删除;同步删除 `database.py:73-79` 的 import 与 `__all__` 中的 `"add_log"`。删前再 grep 一遍 `add_log\(` 确认(排除 record_request 自身与定义处)。

### D7 新增测试(tests/test_perf_storage.py)

覆盖:settings 缓存命中与写失效、DB_PATH 切换后不串库;`get_active_accounts` 缓存命中与 update_account 失效;合并后 `get_stats` 与旧语义等价(手工种 3-5 行 logs,断言各统计字段与 success/error/filtered 分类)。

---

## 5. 验证命令(每个工作流收工前必跑)

```
cd C:\Usr\Code\etc\Buddy2api
.venv/Scripts/python.exe -m pytest tests -q
```

基线(改前 `perf/optimization` = main HEAD):**448 collected,442 passed,6 failed 为既有基线失败,与本轮无关**:

```
tests/test_bind.py::test_namespaced_workbuddy_strips_inner
tests/test_control_plane.py::test_credit_rate_default_and_override
tests/test_core.py::test_chat_proxy_stream_rejects_invalid_tool_arguments_at_eof
tests/test_traesolo.py::test_log_estimates_credit_from_tokens
tests/test_traesolo.py::test_log_honors_credit_rate_setting
tests/test_traesolo.py::test_log_zero_rate_no_estimate
```

收工标准:除上述 6 条基线失败外**零新增失败**。禁止修复基线失败(避免越权改行为),禁止跳过失败用例,禁止改既有测试来迁就实现。

---

## 6. 本轮明确暂缓(记录债务,不实施)

1. eof_error 不再 `mark_account_failure(502)`(proxy.py:1023-1036):涉及故障分类与计费语义,需要单独的行为决策。
2. `_known_cache_keys`/credit_source/executor 写日志三件套在 proxy.py 与 openai_compat.py 的合并:跨 WS-A/WS-C 文件,留待本轮合并后单独一轮。
3. 各 provider 的 `_run_once`/`_run_stream` 轮换循环合并(traesolo/chat.py:1009-1171)、checkin 流程共享化(traesolo/quota.py ↔ traework/quota.py)、store discover 骨架全量收敛:收益是可读性,风险是行为漂移,等 perf 轮稳定后再排。
4. Python 全局 `_lock` 写串行化与 sqlite 连接复用:当前规模下不是瓶颈,动了反而引入跨测试 DB_PATH 失效复杂度。
