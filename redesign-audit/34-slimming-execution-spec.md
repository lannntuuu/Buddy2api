# 代码瘦身 · 实施 spec (EXECUTION SPEC)

> 依据 33 号方案(redesign-audit/33-code-slimming-plan.md)的实施规范,自包含。
> 分支:`perf/optimization`。当前基线:576 passed / 0 failed / 3 bench deselected;
> **新增安全网 tests/test_golden_sse.py 已冻结**(_stream_upstream 出站字节 SHA256=
> 7404bd46444aa24ccdf7bf13ef83d5e7b1d525df1c67864197ca0a4de1045b9d,该哈希变化即行为变化)。
> 验收基线已抓取:.tmp/slim/routes-baseline.txt(62 条路由)、.tmp/slim/bench-baseline.json。
>
> **红线**:
> 1. 行为零变化;中文零 em-dash;禁止 git commit/add;禁止无关格式重排。
> 2. 只改本工作流归属文件;既有断言只允许 §WS-B-3 声明的一处修订。
> 3. 33 号方案 §3 明确不做清单(rotation.py、responses 桥类化、admin 拆分、repos CRUD 抽象、
>    pricing 合并、测试套件瘦身)继续有效,任何人不得越界。
> 4. 33 号 §5 credit 口径禁碰清单逐字有效:机械搬移可,改判定/阈值/舍入不可。
> 5. 收工跑全量 `python -m pytest -q` 必须全绿(golden 哈希不变)。

---

## 0. 文件归属(零重叠)

| WS | 独占文件 |
|---|---|
| WS-A1 后端必做包 | src/upstream/proxy.py、src/providers/traesolo/chat.py、src/providers/store_common.py、src/providers/openai_compat.py、src/accounts/auth_manager.py、src/storage/credential_crypto.py、src/providers/qclaw/jprx.py、新增 tests/test_slim_backend.py |
| WS-B 前端 | src/web/**、tests/test_web_assets.py、新增 tests/test_slim_frontend.py(可选) |
| WS-A2 可选包 | src/providers/{qclaw,qwenwork,traework,traesolo}/chat.py、四家 store.py 与 quota.py、src/providers/trae_shared.py、src/storage/database.py、src/gateway/server.py、src/accounts/control_plane.py、新增 tests/test_slim_optional.py |
| 主控 | tests/test_golden_sse.py(已冻结)、redesign-audit/*、验证与提交 |

tests/test_golden_sse.py 对所有工作流只读:它必须全程保持绿色。

## WS-A1 · 后端必做包(A/B/C/D,按序执行)

### A1-1 死代码删除(~95 行,先做:最小 diff、独立可提交)

删除前逐一 grep src+tests 确认零引用,发现任何引用立即跳过该项并报告:
- auth_manager.py: `import_auth_file`(:304 附近)、`check_all_accounts`(:1065 附近)
- openai_compat.py: `cached_model_ids`(:240 附近)
- credential_crypto.py: `_dpapi_encrypt`(:85 附近;**解密侧 `_dpapi_decrypt` 与 store_common 的 DPAPI 读路径保留**)
- qclaw/jprx.py: `time_sync`/`list_remote_models`/`today_tokens`(**`jprx_ctx` 被 test_qclaw.py:13/52 锚定,不可删**)
- **保留**:backup.list_snapshots(ops/scripts/backup-db.py:54 与 README 引用)、invalidate_accounts_cache、settings_cache_clear(32 号轮约定的失效口)。

### A1-2 proxy._stream_upstream 最小抽取(~55 行)

约束:**函数签名 `_stream_upstream(body, api_key_info, model_name)` 不变;golden 哈希不变**。
- 提 `_RetryLog` 小类(或 dataclass)收敛 4 处 pending_retry_log 字面量构造(proxy.py:900/964/1019/1098 附近,以实际为准),字段与现字面量逐键一致。
- feed 与 finish 两段同构 pump 循环提模块内局部闭包或私有协程 `_pump(...)`;11128 分支、eof `output_started` 分支(proxy.py:1016)、`_parse_retry_after` 透传、httpx.HTTPError 捕获边界**逐行保持原位**。
- first_token_ms 基线(循环前取 t0)与 output_started 翻转打点位置不动;tests/test_perf_metrics.py 的三锚(:250/276/293)、eof 两分支(:325/:351)、负缓存锚(:488)必须全绿。

### A1-3 traesolo 轮换头部合并(~55 行)

- `_run_once` 与 `_run_stream` 的同构头部提 `_rotate_open(payload, *, client_model, api_key_info, tried, stream)`:
  做 _pick→_pre_refresh→_open→≥400 分类记账分支,返回 (account, client, response) 或 (None, error_ctx);
  两函数保留各自的聚合/SSE 产出体与成功打点时机。
- 语义差异表(33 号 §6)中的"不可合"项(失败记账方式、锁账号、无 11128/tool stall)不得为合并而改变。

### A1-4 日志三件套进 store_common(~30 行)

- `store_common.KNOWN_CACHE_KEYS`、`credit_source_of(usage)`、`enqueue_record_request(row)`
  (run_in_executor + done_callback 吞异常;openai_compat 无运行 loop 时的同步兜底一并收编)。
- proxy 的 usage_json 64KB 截断与 reasoning_effort 透传**留在 proxy**(行为差异不合并);
  openai_compat 的 credit 恒 0 语义保留。
- **`proxy._log_request` 与 `openai_compat._record` 入口名不变**(tests 27 处 monkeypatch)。
- credit_source_of 的判定逻辑逐字搬移,不改任何键名/阈值。

### WS-A1 验收

```
python -m pytest tests/test_golden_sse.py tests/test_perf_metrics.py tests/test_traesolo.py tests/test_perf_traesolo_compat.py tests/test_perf_providers.py tests/test_core.py -q
python -m pytest tests -q          # 全绿
git diff --stat                    # 自查净减行数,预期约 -235(95+55+55+30)
```

## WS-B · 前端(必做包 E,~170 行)

按序:
1. **微去重先行**(test_web_assets 零耦合):`withBusy/busyKey` 三份(channels.js:249/281、keys.js:15-16、quota.js:7-8)收敛到 api.js(或 app.js 暴露的全局 helper);`copyText`+copied 四份(keys.js:62/65、logs.js:16、setup.js:31)共享;`size/credit/age/expireMeta` 各 2 份(channels.js:256-257、_login_import.js:58、dashboard.js:12-13、quota.js:45-46/51)收敛到一处共享模块(建议 src/web/js/format.js 新文件)。
2. **CSS 单向 smoke**(安全网):test_web_assets.py 新增"模板+JS 字符串字面量类名全集 ⊆ app.css 选择器全集"断言(先跑现状确认绿色,动态拼接类名若现状即不满足,把例外列入白名单数组并在注释说明)。
3. **channels.js 死码删除**(~60 行):scan/startSoloLogin/solo/disc 相关(template 零引用,158-196/198-243/348-349 附近);**保留 `discover()` 预热及其调用链**。**同步修订 test_web_assets.py:186-194**:channels.js 移出 solo 断言文件元组,保留 _login_import.js(这是全 spec 唯一允许的既有断言改动)。
4. **CSS 死块删除**(~97 行):.chk-*/.ch-panel/.ch-card 整块、.bars/.bcol/.btip、.health-kpi*(**.health-dot 保留**)、.iconbtn、.quick*、.detect-summary/-actions、.credit-main/-rem、522/526/716 行等(以一次性 grep 脚本复核为准;结果清单写进 commit message)。删除集不得含任何 JS 动态拼接的类名。
5. **icons.js**:删除 users/chevron 两个未引用图标。

### WS-B 验收

```
python -m pytest tests/test_web_assets.py tests/test_golden_sse.py -q    # 全绿
# 手测:channels/keys/quota/dashboard 四页,console 零报错(docs/frontend-checklist.md)
git diff --stat                    # 预期约 -170
```

## WS-A2 · 可选包 F1-F5(WS-A1 完成后启动,同一后端文件集)

- **F1 test_chat 五份共享**(proxy.py:745、qclaw/chat.py:271、qwenwork/chat.py:502、traework/chat.py:589、openai_compat.py:547):提取公共 `store_common.run_test_chat(...)`,各家薄壳传参;差异(各家的探活 URL/响应判定)参数化,**test 侧 monkeypatch 点不消失**。
- **F2 checkin 两家合并**(traesolo/quota.py:100-165 ↔ traework/quota.py:41-107):`store_common.run_checkin(account, *, host_of, headers_of, client_of, status_path, claim_path, code_error)`;参数化差异按后端盘点(traework 账号级 extra.host 覆盖、data.code 失败判定)。先给两家各补一条行为特征测试再合并。
- **F3 两份同构 refresh 负缓存收敛**(trae_shared.py:57-90 ↔ traesolo/chat.py:645-660,上轮已同语义):收敛进 trae_shared,traesolo 改调用。
- **F4 store discover 骨架**:`store_common.discover_dirs(dirs, collect_fn)` 四家接入(壳同收集异)。
- **F5 纯函数化**:database.init_db 提 _create_tables/_run_migrations;server.main 提 _resolve_config/_print_banner/_resolve_admin_token;control_plane._build_credit_summary 提 _credit_channel_entry/_workbuddy_rollup。行数约 0,以可读性为目的,禁止语义改动。

### WS-A2 验收

全量 pytest 全绿 + golden 哈希不变 + 路由快照 diff 为空;F2 需附两家特征测试。

## 终验(主控执行)

1. `python -m pytest -q` 全绿(含 golden);
2. openapi 路由快照与 .tmp/slim/routes-baseline.txt diff 为空;
3. `python -m pytest tests/bench -m bench -s` 对比 .tmp/slim/bench-baseline.json,无劣化趋势;
4. 净减行数核对:`git diff --stat` 汇总,目标 400-500(可选包另计);
5. 手测:docs/frontend-checklist.md 走查(channels/keys/quota/dashboard + setup 导入 + 流式/非流式对话)。

## 暂缓(延续 33 号 §3)

rotation.py、responses 桥类化、admin.py 拆分(下一轮,带 §33-5 两个硬前提)、repos CRUD 抽象、pricing 合并、测试套件瘦身。
