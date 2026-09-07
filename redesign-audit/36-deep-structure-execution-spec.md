# 结构层深潜 · 实施 spec (EXECUTION SPEC)

> 依据 35 号方案(redesign-audit/35-deep-structure-plan.md)。自包含。
> 分支:`perf/optimization`。基线:608 passed / 0 failed / 3 bench deselected;
> golden SSE 哈希 7404bd46444aa24ccdf7bf13ef83d5e7b1d525df1c67864197ca0a4de1045b9d 全程不可变;
> 路由基线 62 条(.tmp/slim/routes-baseline.txt)。
>
> **红线**:行为零变化;中文零 em-dash;禁止 git commit/add;禁止无关格式重排;
> 只改归属文件;既有测试断言零改动(新增测试文件除外);33-35 号方案的否决清单继续有效
> (rotation.py、responses 桥类化、repos CRUD 抽象、锁重构、ABC/注册表、credit 口径)。

---

## 0. 文件归属(零重叠)

| WS | 独占文件 |
|---|---|
| WS-A admin 拆分 | src/gateway/routers/admin.py(→ 包)、新增 tests/test_route_golden.py |
| WS-B Protocol 契约 | src/providers/protocol.py、新增 tests/test_provider_contract.py |
| WS-C chat_grammar | src/upstream/proxy.py、src/upstream/responses.py、新增 src/upstream/chat_grammar.py |
| WS-D pick 收敛 | 新增 src/accounts/scheduling.py、src/accounts/auth_manager.py、src/providers/trae_shared.py、src/providers/{qwenwork,qclaw,traework}/__init__.py、新增 tests/test_pick_contract.py |
| WS-E 健康视图+前端 | src/gateway/routers/admin/(A 完成后的包内新文件)、src/gateway/routers/v1.py、src/web/**、tests/test_web_assets.py |
| 主控 | tests/test_golden_sse.py、验证与提交 |

并行矩阵:WS-A+WS-B(代理) 与 WS-C+WS-D(主控) 四线并行;WS-E 等 A。

---

## WS-A · admin.py 九步拆分(照 35 号 §2.1 执行,这里给验收序)

0. **第①步(先于一切搬运)**:新增 tests/test_route_golden.py——`from gateway import server; server.app.openapi()` 导出 method+path 排序清单与金样断言(金样现抓现冻,62 条);再加一条 `import gateway.server` 冒烟。金样测试先行落地并全绿后才开始搬运。
1. 建 `gateway/routers/admin/` 包:把现 admin.py 改名为包 `__init__.py` 起步(内容暂不动,保证 import 面不变),然后按 35 号 §2.1 的 9 个子模块逐域搬出:
   `_channels.py`(9 handler + _public_definition/_probe_models/_apply_definition_with_key/_sync_channel_overrides)、`_custom_channels.py`(4)、`_accounts.py`(15 + _account_row)、`_channel_logins.py`(qclaw 3 + traesolo 4 + solo 回调 + 两 helper)、`_api_keys.py`(5)、`_model_config.py`(6)、`_stats.py`(4)、`_traework.py`(2)、`_logs.py`/`_settings.py`/`_codex.py`(各 2)。
2. 每子模块各持自有 `APIRouter`,`__init__.py` 按现路由定义顺序 include + **重导出全部 55 个 handler 名与 router_obj**(tests 直调面:server 重导出、test_custom_channels 的 `_admin.admin_*` 属性调用、test_perf_admin_api 的 6 个直 import)。
3. **删除 admin.py:39 的死 `ADMIN_TOKEN` 导入**(前提二活体样本);所有子模块鉴权/读体动态走 deps,禁止 `from gateway.deps import ADMIN_TOKEN` 导入期快照。
4. **每搬一个域跑一次**:`python -m pytest tests -q` 全绿 + 路由金样逐字节相等;九步每步一个逻辑单元(最终由主控统一提交)。
5. 终态:admin 包 `__init__.py` ≤约 350 行(壳+重导出),9 个子模块 100-300 行。

## WS-B · 分层 Protocol + 运行时契约测试

1. protocol.py 现有 `Provider(Protocol)`(:101-125,3 属性+8 方法)补全为分层结构(仅注解用,`@runtime_checkable` 保留,禁止运行时 isinstance 入业务路径):
   `CoreProvider`(9 个 5/5 方法:accepts_model/alias_map/chat_completions/fetch_model_rates/has_usable_account/list_models/pick_account/pick_account_with_fallback/translate_model——注意 discover/test_chat/refresh/import_path/parse_credentials 按实测也是 5/5,归入 Core,以 inspect 实测为准)、
   `QuotaCapable`(fetch_quota)、`UpsertCapable`(upsert_account)、`CheckinCapable`(fetch_checkin/claim_checkin)、`LoginCapable`(qclaw 与 traesolo 分别声明,签名各异不强统)、`DynamicModelsCapable`(refresh_dynamic_models)。
2. 新增 tests/test_provider_contract.py:遍历 providers 已加载实例,inspect 校验每家 Core 方法存在且签名兼容(参数名/默认值不严于声明),能力集合与 Protocol 声明一致;五家逐家断言。
3. 零运行时行为变化;608 全绿。

## WS-C · chat_grammar.py 语法层统一(主控执行)

1. 抽 `upstream/chat_grammar.py`:`_ChatStreamObserver` + `_SSEEventDecoder` 的公共依赖(错误文案、常量)原样搬移;proxy.py 改 import 并保留 `_ChatStreamObserver` 别名(tests 直呼)。
2. responses.py 三处重复解析改用 grammar(:707-755 的 DONE/json.loads/usage/finish_reason、:822-876 的 tool arguments 累积、:908-945 终态裁决与 eof 语义对齐);**Responses 事件发射层不动**。
3. provider 边界流获得 observer 级校验 = 本项核心收益;错误文案逐字保留(tests 断言)。
4. 验收:golden 哈希不变 + tests/test_core.py + test_workbuddy_cache.py 全绿。

## WS-D · pick 四副本收敛(主控执行)

1. **先行差分特征测试** tests/test_pick_contract.py:四家 facade fallback 各一条(mock refresh 成功/失败/负缓存窗口),记录现行为。
2. 新增 `accounts/scheduling.py`:`pick_with_refresh_fallback(channel_id, refresh_fn, *, exclude_ids, refresh_errors, proactive_refresh=False)` 自 trae_shared 上移(负缓存状态随之迁移,60×2^n 封顶 600s 语义不变);trae_shared 改薄壳再导出(qclaw/qwenwork/chat 层调用点零改动)。
3. 接线:qwenwork/__init__.py 与 traework/__init__.py 的手写副本改调 scheduling(**`proactive_refresh=True` 保留其选中账号主动刷新语义**);workbuddy 正典 auth_manager.pick_account_with_fallback 改调 scheduling(其 bool 型 refresh_token 包装进 refresh_fn);qclaw 零改动(已是目标形态)。traesolo 委托链本轮不动。
4. 验收:test_pick_contract.py 四家等价 + 608 全绿。

## WS-E · 健康视图 + 前端(A 完成后)

1. 后端:新端点 `GET /admin/channel-health`(只读聚合五字段:cooling 账号数与最早恢复、refresh 负缓存剩余、11128 武装状态、近 1h 5xx 计数、first_token_ms 7 天 P95;窗口 1h+7d 固定);/health 增加 credential_error 账号数字段。落位:admin 包 `_channels.py`(或独立 `_health.py`)。
2. 前端:app.js 挂 `sharedStore`(reactive channels + ensureChannels 单飞,复用 api.js INFLIGHT),usage/keys/models/channels 四页的下拉拉取改订阅(消除三种不一致过滤);channels 页行徽标(绿/黄/红)消费 channel-health。
3. 验收:全量绿 + test_web_assets 更新 + 手测四页。

## 终验(主控)

1. `python -m pytest -q` 全绿(含 golden/路由金样/契约/pick 四网);
2. openapi 路由快照与基线 diff 为空(62 条,WS-A 全程);
3. bench 双跑无劣化;
4. `import gateway.server` 冒烟;
5. 手测四页 + setup 导入 + 流式/非流式对话。

## 暂缓(延续)

rotation.py、responses 桥类化、repos CRUD 抽象、锁重构、ABC/注册表、credit 口径、测试套件瘦身。
