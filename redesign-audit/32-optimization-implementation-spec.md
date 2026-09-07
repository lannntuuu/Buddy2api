# 性能+前端交互优化 · 实施 spec (EXECUTION SPEC)

> 依据 31 号方案(redesign-audit/31-perf-frontend-optimization-plan.md)的实施规范,自包含,subagent 不需要回看主对话。
> 分支:`perf/optimization`。基线:`.venv/Scripts/python.exe -m pytest tests -q` = 512 passed / 6 failed(既有)。
> **红线**:
> 1. 中文零 em-dash;禁止 git commit/add;禁止无关格式重排(包括行尾风格)。
> 2. 只改本工作流归属文件;既有测试文件只有 WS-4 可以改;各工作流新增测试放自己的新文件。
> 3. 收工前跑全量套件,除「基线失败处置」条款外零新增失败。
> 4. 遇与 spec 冲突的代码事实:以代码为准,在报告中说明偏离。

---

## 0. 跨工作流 API 契约(WS-2 与 WS-3 并行的对齐面,不得偏离)

1. `GET /admin/api-keys`:**默认不再返回 key/key_secret 字段**(列表行其余字段不变,含 today_requests)。新增 `GET /admin/api-keys/{kid}/reveal` → `{"ok": true, "key": "<明文>"}`(admin 鉴权,仅此一处返回明文)。
2. `PUT /admin/api-keys/{kid}` → `{"ok": true, "key": <行对象>}`,行对象字段与 GET 列表行完全一致。
3. `PUT /admin/accounts/{aid}` → `{"ok": true, "account": <行对象>}`;`POST /admin/accounts` → `{"ok": true, "account": <行对象>}`。行对象 = GET /admin/accounts 列表项(get_account_status 产物 + phone/account_type/enterprise_id/domain/weight/priority/credit_limit/provider/credential_error)。
4. `PUT /admin/channels/{channel}/models` → 在现有响应上追加 `"models": [<生效模型 id 列表>]`。
5. 新增 `POST /admin/accounts/resources/batch`,body `{"account_ids": [..]}`(缺省=全部 active 账号)→ `{"ok": true, "results": [{"account_id": .., "ok": bool, ...fetch_account_resources 载荷}]}`,内部复用 deps._gather_limited 限并发 4。
6. 前端约定:写操作优先用响应里的行对象本地回写;**响应缺行对象或形状不符时回退整表 load()**(保底开关)。

## 1. 文件归属(零重叠)

| WS | 独占文件 |
|---|---|
| WS-1 指标+策略 | src/upstream/proxy.py、src/providers/{openai_compat.py,retry.py,store_common.py,trae_shared.py}、src/providers/{qclaw,qwenwork,traework}/chat.py、src/providers/traesolo/chat.py、src/storage/repos/logs.py、src/storage/http_pool.py、src/accounts/auth_manager.py、src/gateway/server.py、新增 tests/test_perf_metrics.py |
| WS-2 管理 API | src/gateway/routers/admin.py、src/storage/repos/{api_keys.py,accounts.py}、src/accounts/control_plane.py、新增 tests/test_perf_admin_api.py |
| WS-3 前端 | src/web/**、tests/test_web_assets.py(存在则扩展)、新增 docs/frontend-checklist.md |
| WS-4 测试卫生+基准 | tests/{test_traesolo.py,test_control_plane.py,test_bind.py,test_core.py}、tests/bench/*(新建)、pytest.ini(新建) |

---

## WS-1 · first_token_ms + 策略三件套 + 服务端杂项

### 1.1 [P0] first_token_ms 落库与统计

- `logs` 表加列 `first_token_ms INTEGER`(可空),迁移仿 logs.py 现有 migrate_* 模式接入 init_db 链路;record_request 增列写入(data.get("first_token_ms"),缺省 None)。
- 定义:**首个含 choices(或 output 内容)的非 error delta 帧**与请求起点之差(毫秒)。pending_terminal 帧、error 帧、`[DONE]` 不计;**非流式一律 NULL**;重试/换账号不重置起点(=用户真实等待)。
- 打点位置(先读代码核实行号,允许±20 行漂移):
  a) workbuddy `proxy.py` `_stream_upstream`:循环外取 `request_t0 = time.monotonic()`(天然含 pick/refresh/重试/退避);`output_started` 翻转的两处各记一次 `first_token_ms`(首次赋值后不再覆盖);结束时随 _log_request 透传。retry 行(pending_retry_log)与 eof/错误行记 NULL。
  b) openai_compat 流式路径:首个 `data:` 行(非 [DONE])时刻,基线=进入读循环前。
  c) traesolo `_run_stream`:首个 yield 帧,基线=账号轮换循环前。
  d) qclaw/qwenwork/traework 流式路径:各自首个内容帧。
- 透传链:`_log_request(..., first_token_ms=None)` → store_common.log_request(first_token_ms=...) → row;traesolo 本地 _log 同步加参。
- 统计:`stats.get_stats` 增加 `"stream_p95": {provider: 毫秒}`,样本=近 7 天流式请求的 first_token_ms 非空值,Python 排序取 P95(无样本则 {});样本上限 5000 条/查询(SQL LIMIT)防大表。

### 1.2 [P0→捡起 §6.1] eof_error 分类

`proxy.py` eof_error 处理(现无条件 `mark_account_failure(id, 502)` + 重试):**output_started 后**的 eof_error:不 mark_account_failure、不跨账号重试,按现状记 error 日志并把已收内容/错误事件透传收尾;**未出流**的 eof_error:维持 mark + 重试。补单测两分支。

### 1.3 [P1] 退避 equal-jitter + Retry-After

`providers/retry.py::retry_delay(attempt, max_attempts=MAX_ATTEMPTS, *, rng=None, retry_after=None)`:rng 缺省模块级 `random.Random()`;无 retry_after:底数=min(2.0, 0.25×2^attempt),sleep=底数×(0.5+rng())∈[0.5×,1.5×];有 retry_after(秒):sleep=min(retry_after, 2.0)。proxy.py 429 分支读 `response.headers.get("retry-after")`(支持纯数字秒)存入 pending_retry_log 并透传。调用方签名兼容(全部关键字默认)。测试注入固定 rng。

### 1.4 [P1] refresh 负缓存自适应

`trae_shared.py`:失败计数结构改 `(连续失败次数, 下次可试时刻)`,间隔=60×2^(n-1) 封顶 600s,成功清零;提供 `_now` 可注入(测试 fake clock)。traesolo/chat.py 本地负缓存(C2 引入)同步改同一语义(允许直接改用 trae_shared 的实现)。

### 1.5 [P1] 调度同级加权随机决胜

`auth_manager.py::_route_sort_key` 排序键最后一级 id 改为:完全并列(优先级、total_requests/weight 比值、total_requests 全相等)时从并列集加权随机取一(模块级可注入 `_route_rng`);非并列行为逐位不变。先 grep tests 确认无确定性依赖,有则改测试为新契约并说明。

### 1.6 [P1] 服务端杂项

- `http_pool.py` 与 proxy.py `_get_client`:max_connections 32→64,max_keepalive 12→32(两处同步改)。
- `server.py`:lifespan 内新增 24h 周期任务调 db.prune_logs()(仿 _traework_sync_loop 模式,异常吞掉写 stderr);uvicorn.run 加 `timeout_keep_alive=30`;StaticFiles 换子类对 /static 响应加 `Cache-Control: public, max-age=3600`,index 路由(static_router.py:31 的 GET /)保持 no-cache —— static_router.py 归 WS-1 本条临时使用(仅 no-cache 一处,若 WS-3 未动该文件)。
- `logs.py`:record_request 的 created_at 改 `data.get("created_at") or now`;proxy.py/_log 链路入队时携带 `created_at=int(t0)`;search_logs 当请求未提供 start/end 时强制默认 start=now-7d(响应加 `"window_applied": "7d"` 字段)。
- store_common.log_request 增加 created_at/first_token_ms 透传。

---

## WS-2 · 管理 API(泄密面、行对象、summary、批量)

1. [P0] 契约 1:admin.py 的 api-keys 列表去掉 include_secret 透传(列表永不返回明文);新增 reveal 端点(复用 _check_admin;decrypt 失败返回 400 带 key_unrecoverable)。
2. [P0] 契约 2/3:四个端点返回行对象。helper:accounts 用现有 get_account_status(a) 组装;api_keys.py 新增 `get_api_key_by_id(kid)`(SELECT 同 list_api_keys 单行,含 today_requests,无明文)。
3. [P0] /admin/accounts 免解密 summary:`repos/accounts.py` 新增 `list_accounts_summary()`,SQL 只取状态计算所需列;**先核实** get_account_status 的过期判定实际读取哪些凭据字段,只解密最小集合(通常仅 access_token);响应形状与现列表逐字段一致,不破坏前端。
4. [P2] 契约 5 批量端点(control_plane.py 新增 `fetch_resources_batch(account_ids, force, max_age_seconds)`,复用 _gather_limited(4);单账号失败逐条 ok=false 带错误,不整体 500)。
5. 新增测试 tests/test_perf_admin_api.py:无 key_secret 键、reveal 独占明文、PUT 返回行对象、summary 解密调用计数对比、批量 3 账号恰 3 次上游调用。

## WS-3 · 前端(无构建硬约束)

1. [P0] api.js 错误文案映射(503/403/400/401,含下一步动作;401 额外引导跳设置页),错误 toast 长驻(8s+关闭按钮)+ 容器 aria-live/role=status;成功保持 2.5s。
2. [P0] 键盘可达:通道行/账号行/rail 导航 tabindex=0 + Enter/Space 触发与 click 同义;焦点样式复用 focus-visible。
3. [P1] 局部更新(按契约 6):keys.js 增删改、channels.js 启停/删除、models.js 保存白名单、quota.js 批量刷新,成功后本地回写,失败/形状不符回退 load();删除 channels.js 里只为下拉而连带的重复请求。
4. [P1] 生命周期:Sortable onUnmounted destroy 修二次进页失效;SOLO 轮询 onUnmounted 清理 + AbortController;api.js GET in-flight 去重 + 默认 15s 超时。
5. [P1] 首屏:index.html vendor script 加 defer(注意模块加载顺序不破坏)、7 个 woff2 preload+font-display 确认。
6. [P2] hash 路由:go() 同步 location.hash,hashchange 驱动渲染,启动 hash 优先(空 hash 回退现 localStorage 行为)。
7. [P0 配套] keys.js 明文展示改为掩码 + 「查看」按钮调 reveal 端点(若现 UI 本就内联显示明文)。
8. docs/frontend-checklist.md:31 号方案 §5 的手测清单落成文档。
9. tests/test_web_assets.py 扩展静态 smoke:错误映射表存在、api.js 去重/超时存在、index.html defer/preload/aria-live 存在、Sortable 幂等守卫。

## WS-4 · 测试卫生 + 基准

1. [P0] test_traesolo.py 3 条:调用侧补 asyncio.run(照 test_perf_traesolo_compat.py:99 模式),恢复保护力。
2. [P0] test_control_plane credit_rate:先归因(注释说明结论):若 settings TTL 缓存跨用例污染为根因则该条移交 WS-1 报告,否则修测试(set_setting 走 db 层)。
3. [P1] test_bind、test_core eof:按 WS-1 §1.2 的新契约断言(mock 上游:未出流 EOF→重试+降分;出流后 EOF→透传不降分);若归因为既有遗留缺陷且修复超范围,xfail(strict=True)+说明。
4. [P1] tests/bench/:bench_sse_first_token.py(MockTransport 分帧 0/50/200ms×200 轮,四路径 P50/P95)、bench_dashboard.py(ASGITransport+1 万行种子,三端点各 50 次,P95<200ms 软阈值)、bench_prune.py(10 万行 seed);pytest.ini 新建(`[pytest] addopts = -m "not bench"` + `markers = bench`);结果写 .tmp/bench/last.json。

---

## 验证命令

```
cd C:\Usr\Code\etc\Buddy2api
.venv/Scripts/python.exe -m pytest tests -q          # 除被修复的基线失败外零新增失败
.venv/Scripts/python.exe -m pytest tests/bench -m bench -s   # 可跑通并输出 last.json
```

## 暂缓(本轮不做)

空态向导(31 方案 P2-10)、/admin/events SSE、immutable 缓存、虚拟滚动/shallowRef、内容哈希。
