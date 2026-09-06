# 性能优化 + 前端交互优化 · 团队讨论方案(31)

> 六角色(架构师/产品经理/高级后端/高级前端/算法工程师/测试工程师)分三轮波次讨论的汇总裁定。
> 波次1 产品经理+高级前端调研立场;波次2 高级后端+算法工程师回应;波次3 架构师仲裁+测试工程师验收设计。
> 本文是方案,不是执行 spec;实施前需按仓库惯例出对应 EXECUTION SPEC。
> 分支:`perf/optimization`。总工作量约 10.5 人日,单人两周含缓冲。

---

## 0. 讨论前提与共识基线

- 上轮后端热路径优化已完成(30 号 spec,commit b50d071..8adf2d8):共享连接池、SSE bytes.find、O(n²) 消除、settings/账号调度 TTL 缓存、get_stats 合并、admin 端点线程池化。
- 产品判断原则(全体共识):本机单进程自部署工具,优化只做「等待变短、坑变少」,拒绝大厂 SaaS 式过度工程。
- 全体永久关闭项:全局锁重构、跨通道 failover(一把 Key 一通道是刻意设计)、Redis、ML 预测、FTS5、OpenTelemetry、credit 精度拟合、构建工具链/npm。

## 1. 讨论发现的关键事实(均已核实到行)

1. **全仓无 first_token_ms 指标**:logs 表只有 duration_ms(grep 证实),流式首字延迟是盲区,PM 判定其为流式优化的头号验收障碍。
2. **泄密面**:`admin.py:1165` `include_secret=True` 使 /admin/api-keys 响应携带 key_secret 明文(架构师查证属实)。
3. **前端真功能 bug**:`channels.js:286-291` Sortable 实例从不 destroy,二次进页 `sortInst.length` 挡住重建,实例挂在旧 DOM 上,拖拽失效。
4. **资源生命周期**:SOLO 登录轮询(2.5s,`_login_import.js:81`)切页不停;api.js 无去重/超时;v-if 切页销毁组件导致回页全量重拉(channels 进页 4 请求)。
5. **dashboard 并无自动轮询**(`dashboard.js:7` 仅挂载加载),高级后端据此否决本轮做 /admin/events SSE。
6. 静态资源无 Cache-Control(server.py:152),vendor 同步加载(index.html:22-23),7 个 woff2 无 preload。
7. /admin/accounts 的真实开销是 list_accounts 全量解密凭据,而非字段过多;`/admin/api-keys` 裁 key_secret 才是必裁点。
8. 6 条基线测试失败中,test_traesolo×3 因 _log 协程化已失去保护力(同步调用 async,StopIteration),必须修。

## 2. 争议点裁决(架构师)

| 争议 | 裁决 | 理由 |
|---|---|---|
| /admin/events SSE 本轮做否 | 不做,立项缓行 | dashboard 无自动轮询,唯一 2.5s 轮询是 SOLO 短流程,负载为内存 dict,收益不抵复杂度;出现「多开管理页+自动刷新」真实需求再启 |
| ?fields= vs 免解密 summary | 免解密 summary | ?fields= 通用机制复杂且易误漏敏感字段;summary 一次收窄解密开销与泄密面 |
| immutable vs max-age | max-age=3600+defer | vendor 无内容哈希,immutable 会发版后缓存死锁;内容哈希入缓行池 |
| 空态向导是否升 P1 | 维持 P2 | 首次跑通堵点已被「错误文案附下一步动作」覆盖 |
| hash 路由 | 做,P2 | S 级,纯 hashchange 监听修复刷新/回退失真 |
| 虚拟滚动 / shallowRef | 砍 | 分页+7 天窗下瓶颈未证实;局部更新落地后重渲染已大减,先量化再优化 |
| PM「不整表闪」与后端「PUT 返回行对象」 | 同阶段配套 | 一体两面,拆开会造成前端空转 |
| quota 批量端点 | 降 P2 | 非瓶颈,后端已有 2s 账号缓存兜底 |

## 3. 优先级矩阵(约 10.5 人日)

### P0(4 条,阶段一)

| # | 提案 | 负责 | 工作量 | 验收信号 |
|---|---|---|---|---|
| 1 | first_token_ms 四条 SSE 路径打点落 logs + P95(7 天窗) | 后端 | M | 分帧 mock 下 logs.first_token_ms∈[40,120];非流式 NULL;P95 为纯函数可断言 |
| 2 | /admin/api-keys 裁 key_secret + /admin/accounts 免解密 summary | 后端 | S | 响应断言无 key_secret;summary 路径解密调用计数=0 |
| 3 | 503/403/400 错误文案映射(附下一步动作)+ 错误 toast 长驻 + aria-live | 前端 | S | 三类错误注入可见文案;api.js:7 加映射表,独立文件可回滚 |
| 4 | 键盘可达:行/rail 可聚焦回车触发、toast role=status | 前端 | S | 纯 Tab 完成通道启停/进详情 |

first_token_ms 实现要点(后端+算法共识):定义为「首个含 choices 的非 error delta 帧」;打点收敛在各通道日志生成器(workbuddy proxy.py:906/960 的 output_started 翻转,基线取循环前、天然含跨账号重试+退避总时长;openai_compat.py:448 首个 data 行;traesolo chat.py:1172 首 yield;qclaw/qwenwork/traework 首帧);pending_terminal 与 error 帧不计;非流式记 NULL;P95 限 7 天窗 Python 排序,不做全表。

### P1(4 条,阶段二/三)

| # | 提案 | 负责 | 工作量 | 验收信号 |
|---|---|---|---|---|
| 5 | 写操作局部更新 + PUT/POST 返回最终行对象(4 端点:admin.py:854/1199/744/1168) | 前后端 | M | 编辑后 Network 无列表级 GET;保留整表刷新 fallback 开关 |
| 6 | 资源生命周期:Sortable destroy、SOLO 轮询清理、api.js 去重+超时+401 引导 | 前端 | M | 二次进页可拖拽;切页无残留请求;channels 进页请求≤2 |
| 7 | 首屏与缓存:vendor defer、字体 preload、Cache-Control max-age=3600、连接池 64/32、uvicorn keep-alive 30 | 前后端 | S | 静态资源命中缓存;额度页首屏达标 |
| 8 | 策略三件套:同级并列加权随机决胜、退避 equal-jitter(0.5~1.5×)+429 读 Retry-After、refresh 负缓存连续失败×2 封顶 600s 成功清零 | 算法 | S | 429 重试间隔符合 Retry-After;fake clock 下负缓存翻倍/封顶/清零可断言;jitter 注入 rng 后可精确断言 |

### P2(阶段四择取)

9 hash 路由;10 空态向导(dashboard 0 账号/0 Key 时渲染四步 checklist,嵌 _login_import);11 logs 24h 周期 prune + search_logs 默认强制 7 天窗;12 created_at 改入队时携传;13 quota 批量刷新端点(POST /admin/accounts/resources/batch,复用 deps._gather_limited)。

### 砍掉(写明理由)

虚拟滚动/shallowRef(瓶颈未证实,先量化);?fields=(summary 替代);immutable(无内容哈希);/admin/events(收益低);锁重构、跨通道 failover、Redis/ML/FTS5/OTel、credit 精度(共识永久关闭)。

## 4. 分阶段路线图(出口条件可测试)

- **阶段一(1-3 天):P0 全部**。出口:泄密断言、first_token_ms 字段断言、错误文案快照、键盘遍历手测全部通过;压测吞吐与 P95 不劣于上轮基线。
- **阶段二(4-7 天):P1 的 5、6**。出口:四页写操作无整表闪;拖拽回归通过;切页无残留请求。
- **阶段三(8-10 天):P1 的 7、8 + P2 的 11、12 + 捡起 30 号 spec §6.1 eof_error**(仅 not output_started 时 mark_account_failure(502),proxy.py:974 落判据)。出口:429 注入下重试节奏平滑;logs 表体积稳定。
- **阶段四(11-14 天):缓冲 + P2 择取(9、13)**。出口:全量回归 + 手测清单签署。

## 5. 测量与验收方案(测试工程师)

- **单测新增**(沿用 isolated_db/asyncio.run 约定):分帧 mock(首帧前 50ms)断言 first_token_ms;EOF 语义两分支;retry_delay 注入 seed;负缓存 fake clock;连接池 limits 断言 64/32;prune 返回删除数;PUT 返回行与无 key_secret 键;summary 解密计数=0;批量端点恰 N 次上游调用;Cache-Control 头断言;search_logs 30 天窗 clamp 到 7 天。
- **基准脚本** 放 tests/bench/,pytest.ini 加 `markers=bench` 且默认 `-m "not bench"`,结果写 .tmp/bench/last.json,P95 劣化>30% 判失败:
  - bench_sse_first_token.py:MockTransport 分帧 0/50/200ms × 200 轮,四条 SSE 路径 P50/P95;
  - bench_dashboard.py:ASGITransport + 1 万行种子 logs,压 /admin/stats、/admin/logs、credit-summary 各 50 次,P95<200ms 软阈值;
  - bench_prune.py:10 万行 seed 测 prune 耗时与主线程阻塞;
  - docs/frontend-checklist.md:手测清单(错误文案、aria-live、写操作无整页重载、Sortable 二次进页、SOLO 切页无残留、hash 刷新还原、空库向导、vendor defer/preload、双击去重),单次 ≤10 分钟,PR 模板引用。
- **6 条基线失败处置**(不许维持现状):test_traesolo×3 立即修(测试侧补 asyncio.run,零风险);test_control_plane credit_rate 先归因(排除 settings TTL 缓存跨用例污染的实现因素),是 bug 修实现、否则修测试;test_bind 与 test_core eof 先 bisect 归因,本轮引入则修,遗留则 xfail(strict=True)+issue。
- **前端测试底线**:不引 node/Playwright;手测 checklist 为主,扩展 tests/test_web_assets.py 做静态 smoke(断言 defer/preload/aria-live/去重 map 存在),零构建秒级防退化。

## 6. 风险与回滚

| 提案 | 风险 | 回滚 |
|---|---|---|
| first_token_ms | 附加计算异常 | try 吞掉不阻主流程;开关关闭,列保留 |
| 裁 key_secret | 外部依赖明文 | 恢复 include_secret=True 紧急发版,同轮补「查看一次」按钮 |
| 错误文案/键盘可达 | 行为变化小 | 映射表/增量属性删除即还原 |
| 局部更新 | 接口返回体差异 | 整表刷新 fallback 开关 |
| 生命周期/去重 | 清理时机 | 删 dispose/AbortController 即回滚 |
| 连接池 64/32 | 上游连接数翻倍 | 配置回退 32/12,监控 SQLite busy |
| jitter/负缓存 | 时序行为变化 | 各自开关,回退旧行为;测试必须注入 rng/fake clock 防 flaky |

## 7. 与 30 号 spec 的衔接

- **捡起**:§6.1 eof_error 判据(阶段三);§6.4 中的 refresh 负缓存自适应(升格为 P1-8)。
- **永久关闭**:§6.2 三件套合并、§6.3 轮换循环/checkin/store 骨架(纯可读性)、§6.4 锁与连接复用主体。
- **缓行池(触发即启)**:/admin/events SSE、内容哈希+immutable、虚拟滚动/shallowRef。

## 附:六角色立场要点索引

- 产品经理:用户画像与感知排序;first_token_ms 缺位是头号盲区;错误自解文案;反过度工程清单;6 项成功指标。
- 高级前端:src/web 全量审计,Sortable 真 bug、SOLO 轮询泄漏、无 hash 路由、api.js 无去重、P0 可访问性;12 条无构建约束内提案。
- 高级后端:first_token_ms 四路径打点收敛方案;否决 /admin/events 与 ?fields=;key_secret 泄密面;连接池参数、prune 周期化、keep-alive 等 6 条补充。
- 算法工程师:first_token_ms 精确定义(首个含 choices 的非 error delta);eof_error 只在未出流时降分;equal-jitter+Retry-After;负缓存×2 封顶;调度维持 sticky,仅并列决胜改加权随机;反 Redis/ML/FTS5。
- 架构师:7 项争议裁决;P0≤4 的优先级矩阵;四阶段路线图;逐条回滚方案。
- 测试工程师:最难测对的 4 点(rng/clock 注入、TTL 与基线失败相互作用、分帧 mock);12 条验收标准;bench 目录约定;6 条基线失败分类处置;零构建前端测试底线。
