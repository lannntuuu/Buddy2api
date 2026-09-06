# 代码瘦身 · 六角色讨论方案(33)

> 六角色(架构师/产品经理/高级后端/高级前端/算法工程师/测试工程师)三轮波次讨论的汇总裁定。
> 主题:代码瘦身(可引入设计模式)。行为零变化,576 条测试保持全绿。
> 本文是方案,不是执行 spec;实施前按仓库惯例出对应 EXECUTION SPEC。
> 分支:`perf/optimization`。投入上限:**3 人日**(含验证)。

---

## 0. 量化底稿(主控实测)

- src 约 19.8k 行:providers 8949、gateway 3073、upstream 2899、storage 2832、accounts 2079;前端 js 1872 + css 800;tests 10431。
- 单文件 TOP:admin.py 1577、traesolo/chat.py 1312、proxy.py 1232、auth_manager.py 1068、responses.py 1013、control_plane.py 1011、openai_compat.py 824。
- >80 行函数 24 个,最大:responses.chat_stream_to_responses_stream 376、proxy._stream_upstream 330、stats.get_stats 237。
- 四家 provider chat.py 同名函数成串(_log/_pick/chat_completions/test_chat/_headers_for/_ids/_last_user_text/_stream);30 号 spec §6 暂缓项为讨论素材。

## 1. 收敛定调(产品经理裁决)

- **用户价值**:行数是代理指标,交付物是三件事:下次改功能的 diff 更小(评审成本)、排查故障要读的代码更少(可维护性)、能误删错改的入口更少(缺陷面)。
- **净减总目标:450 行(区间 400-500)**,投入上限 3 人日。架构师的 800 行只有靠 admin.py 拆分才够得着,那是下一轮的承诺。
- **三项否决(记录理由)**:
  1. rotation.py 四 callable 骨架(架构师 P3,-280):否决。热路径 async generator 再加驱动层;eof output_started 分支、pending_retry_log 落库次序、httpx.HTTPError 边界(proxy.py:958)三处时序语义会被打平;retry.py:1-5 自己写明各家策略不通用;上一轮刚升级重试/调度,此刻回归面最大。采纳最小抽取。
  2. responses 桥 StreamTranslator 类化(架构师 P2):不做。1013 行纯函数翻译模块,类化零净减、纯风格偏好。
  3. admin.py 拆分(架构师 P0):单独一轮。1577 行纯搬运 diff 与删除项混在一个分支,评审者无法区分"搬"与"改"。届时两个硬前提(见 §5)。
- 架构师"150 行以上函数清零"目标不采纳:与最小抽取结论冲突,_stream_upstream 抽后仍约 275 行,这是接受的风险而非目标。

## 2. 实施清单

### 必做包(净减约 405 行)

| # | 项 | 内容 | 净减 | 关键锚点/前提 |
|---|---|---|---|---|
| A | 死代码删除 | import_auth_file(auth_manager.py:304)、check_all_accounts(:1065)、cached_model_ids(openai_compat.py:240)、_dpapi_encrypt(credential_crypto.py:85,解密侧在用保留)、qclaw/jprx.py 的 time_sync/list_remote_models/today_tokens | ~95 | 逐个 grep 复核零引用;**jprx_ctx 被 test_qclaw.py:13/52 锚定不可删;backup.list_snapshots 被 ops/scripts/backup-db.py:54 与 README 引用,保留**;invalidate_accounts_cache/settings_cache_clear 是 32 号轮约定的失效口,保留 |
| B | proxy._stream_upstream 最小抽取 | _RetryLog 收敛 4 处 pending_retry_log 字面量(proxy.py:900/964/1019/1098)+ feed/finish 同构 pump 循环提局部闭包;**函数签名不变**(tests 直呼 _stream_upstream x8) | ~55 | 先补 SSE 全字节 golden 特征测试(§4)再动手 |
| C | traesolo 轮换头部合并 | _run_once/_run_stream 同构头部提 _rotate_open(chat.py:1072-1127 与 1158-1190);两函数保留各自聚合/SSE 体 | ~55 | 失败记账/_handle_kind/锁账号语义不可合(差异表见 §6) |
| D | 日志三件套进 store_common | KNOWN_CACHE_KEYS、credit_source_of(usage)、enqueue_record_request(含 openai 无 loop 同步兜底);proxy._log_request 与 openai_compat._record **入口名保留**(27 处 monkeypatch);只并尾部三件,不全量合并(proxy 侧 64KB 截断与 reasoning_effort 差异保留) | ~30 | tests 直呼 proxy._log_request x2 |
| E | 前端删除与微去重 | ①CSS 死块 ~97 行(.chk-*/.ch-panel 整块、legacy 柱状图、.health-kpi 等,.health-dot 在用保留;一次性脚本找死块,不进 CI);②channels.js scan/solo 死码 ~60 行(模板零引用,导入 UI 已迁 login-import;**保留 discover() 预热**);③icons.js users/chevron 2 行;④withBusy→api.js、copyText/格式化共享 ~12 行 | ~170 | **必须同步修订 test_web_assets.py:186-194 的 channels.js 文件元组断言(唯二允许的断言改动之一)**;先做微去重后删 CSS |

### 可选包(时间富余才做,按性价比排序)

| # | 项 | 净减 | 风险 |
|---|---|---|---|
| F1 | test_chat 五份共享(proxy:745/qclaw:271/qwenwork:502/traework:589/openai_compat:547),进 store_common | ~90 | 低 |
| F2 | checkin 两家合并(traesolo/quota.py:100-165 ↔ traework/quota.py:41-107),参数化 host/headers/失败判定 | ~55 | 中,需两家行为测试先行 |
| F3 | 两份同构 refresh 负缓存收敛(trae_shared.py:57-90 ↔ traesolo/chat.py:645-660) | ~30 | 低 |
| F4 | store discover 骨架 discover_dirs(collect_fn) 四家接入 | ~25 | 低 |
| F5 | init_db/main/_build_credit_summary 纯函数化(可读性,不追求行数) | ~0 | 低 |

## 3. 明确不做(反过度工程共识)

- provider ABC+插件注册表(providers/protocol.py 模块级函数已是事实接口);
- repos 五文件通用 CRUD 抽象(-120 但回归面最大,sqlite 样板本就便宜);
- pricing.py 与 model_config 合并(官方标价 vs 运营倍率,合则混口径);
- store_common 与 trae_shared 合并(职责清晰,改名零收益);
- 页面脚手架抽象、动态 import()、内联样式工具类化、dashboard/quota 额度卡合并;
- 测试套件瘦身(10431 行不参与:行为冻结期同时改测试会摧毁"576 全绿"这个唯一信号;test_core 的参数化机会留给专门测试重构轮);
- 30 号 §6.4 sqlite 连接复用、§6.1 eof 语义再动。

## 4. 安全网与验收(测试工程师)

**动手前先建两道特征网:**
1. `_stream_upstream` 的 SSE 全字节 SHA256 golden 特征测试(固定 mock 上游,断言出站字节流哈希),进 tests/test_perf_metrics.py;
2. CSS 单向 smoke 进 test_web_assets.py:模板+JS 用到的类名全集 ⊆ app.css 选择器全集(先跑现状生成基线)。

**每项验收:**
- 死代码:全量 `python -m pytest -q` 单命令为最终门禁;
- B/C:tests/test_perf_metrics.py + test_traesolo.py + test_perf_traesolo_compat.py 全绿(golden 哈希不变);
- D:全量 pytest(_log_request/_record 入口 monkeypatch 27 处不失效);
- E:test_web_assets.py 27 条全绿(含两处已声明的断言修订)+ 手测 4 个主页面;
- 全局三验收:①openapi 路由快照 diff 为空(`from gateway import server; server.app.openapi()` 导出 method+path 排序清单,基线 49 paths/62 ops);②tests/bench 双跑,首字延迟与失败率落噪声带;③按 docs/frontend-checklist.md 手工走查用户旅程。

## 5. 红线汇总(union,实施 spec 直接引用)

- **credit 口径禁碰**:traesolo/chat.py:1001-1020 估算与退回旧口径、pricing.py 全文件、credit_source 标记(chat.py:1045、proxy.py:599-607)、openai_compat.py:289-313、auth_manager.py:1055-1058;机械搬移可,改判定/阈值/舍入不可。
- **admin 拆分(下一轮)两个硬前提**:`gateway.routers.admin` 仍可 import 并在 __init__ 重导出全部 handler;新子模块禁止 `from gateway.deps import ADMIN_TOKEN` 导入期快照(必须动态走 deps),否则 11 处 setattr 失效。
- **产品红线**:setup 向导页与 _login_import.js(新用户唯一入口)、README/ops 运维脚本 import 的模块路径、旧库迁移分支(database.py 快照+各 repos migrate、deps legacy 再导出)、admin API 的 URL 与返回字段名。
- **重构期测试纪律**:禁改/删既有断言(唯二例外:channels.js 死码对应的 test_web_assets.py:186-194 修订)、禁删慢测试或加 skip、禁改 conftest 夹具、禁把 bench 改默认跑、禁用 xfail 换绿灯。

## 6. 附:proxy 与 traesolo 轮换循环语义差异表(为什么只做最小抽取)

| 维度 | proxy | traesolo | 处置 |
|---|---|---|---|
| 重试次数 | range(3),11128 可 attempt-=1 回退 | range(MAX_ROTATE) | 可参数化 |
| 失败记账 | mark_failure+延迟落库+退避 | _handle_kind→冷却+立即 _log,无退避 | 不可合 |
| 11128 分支 | 精简+同账号重发 | 无 | 不可合 |
| tool stall | 合成 error 事件/required 重试 | 无 | 不可合 |
| 错误合成 | _err_sse_event+synthetic terminal | 文本 event:error | 可参数化 |
| 2xx 后 eof | 未出流仍换号 | 锁定账号不轮转 | 不可合(核心分歧) |

## 附:六角色立场要点索引

- 架构师:三档处置策略与模式 playbook;P0-P6 依赖序;需各角色输入的四个问题。
- 高级后端:24 个长函数逐个拆分判断(多数"不拆");死代码 97 行清单;净减约 330 行,"不为行数强拆"。
- 高级前端:CSS 死块 97 行与 channels.js 死码 60 行的定量证据;净减 120-170;反对脚手架抽象。
- 算法工程师:否决 rotation.py 的四条理由 + 轮换语义差异表;pricing/repos 禁碰;死代码 API 面复核;credit 禁碰清单。
- 产品经理:3 人日上限与 450 行目标;四项裁决;四条产品红线;三条可执行验收。
- 测试工程师:admin/server 符号导入面统计(6 import + 3 属性 + 11 setattr);golden SSE 哈希与 CSS 单向 smoke 两道特征网;移动式重构防回归命令级方案;测试套件不参与瘦身。
