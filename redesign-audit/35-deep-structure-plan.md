# 结构层深潜 · 六角色讨论方案(35)

> 第四轮六角色讨论(架构师/产品经理/高级后端/高级前端/算法工程师/测试工程师)。
> 本轮定位:前三轮覆盖语法层(性能热路径、交互、死代码与复制粘贴),本轮只讨论**机制与结构级**深水区。
> 说明:本轮波次 2/3 因运行环境持续限流,部分立场由主控代笔(标注★),但全部代码证据均为实测。
> 分支:`perf/optimization`。投入上限:**5 人日**。实施前按惯例出 EXECUTION SPEC。

---

## 0. 机制级底稿(主控实测)

1. **provider 协议面**:五家公共方法 14 个(其中 9 个 5/5 齐备),扩展面 ragged(fetch_quota 4/5、upsert 3/5、checkin 2/5、login 流 2/5、动态模型 1/5);`providers/protocol.py:101-125` 已有半成品 `@runtime_checkable Provider(Protocol)`(3 属性+8 方法,仅注解用)。
2. **admin.py**:55 个 handler,accounts 15 + channels 9 + api-keys 5 + provider 专属 9 + 其余 16;最大 handler 跨度 172 行;`admin.py:39` 存在死 `ADMIN_TOKEN` 导入(正是拆分禁令要禁的模式)。
3. **并发面**:全局 `_lock` 25 个写点;锁竞争实测(锁竞争实验,.tmp/slim/lock_contention_probe.py)——写路径 p95 62ms@N1 → 119@4 → 191@8 → 272ms@16,p50 恒 20-43ms;读路径无恶化。
4. **pick 策略副本**:四套实现并存,且 32 号轮的负缓存只覆盖到 chat 层,门面层漏网(见 §2.4)。
5. **流式校验缺口**:`_ChatStreamObserver` 只在 workbuddy 路径挂载(proxy.py:872),qclaw 等 provider 流完全绕过 observer 级校验。

## 1. 本轮六项决议(产品经理定调)

| # | 决议 | 角色 | 优先级 | 要点 |
|---|---|---|---|---|
| 1 | **admin.py 拆分执行**(上一轮"单独一轮"的承诺兑现) | 架构师设计 | P0 | 9 子模块 9 步迁移,路由金样先行 |
| 2 | **chat_grammar.py 语法层统一** | 架构师 | P0 | 堵非 workbuddy 通道绕过 observer 的真实缺口;golden 钉字节等价 |
| 3 | **provider 分层 Protocol + 运行时契约测试** | 架构师/测试 | P0 | 注解用 Protocol,禁运行时 isinstance/ABC/注册表 |
| 4 | **pick 策略四副本收敛** | 算法 | P1 | 上移 accounts/scheduling.py,补 workbuddy/qwenwork 负缓存缺口 |
| 5 | **per-channel 健康视图(只读聚合)** | 算法/前端 | P1 | 五字段,两个固定窗口,零新状态机 |
| 6 | **前端共享 store + API 契约锚** | 前端/测试 | P1/P2 | sharedStore.ensureChannels 单飞;openapi 快照进 CI |

**延续否决(不再重审)**:rotation.py 骨架、responses 桥类化、repos CRUD 抽象、锁重构主体、provider ABC/注册表、测试套件瘦身。

## 2. 各项执行设计

### 2.1 admin.py 拆分(可执行设计,架构师)

包化 `gateway/routers/admin/`,9 个子模块各持自有 router,`__init__.py` 按现路由定义顺序 include(防 shadowing)+ 重导出全部 55 个 handler 与 `router_obj`,`server.py` 零改动:

```
_channels.py        channels 9 个 + 下沉 _public_definition/_probe_models/_apply_definition_with_key/_sync_channel_overrides
_custom_channels.py 4 个
_accounts.py        15 个 + _account_row
_channel_logins.py  qclaw 3 + traesolo 4 + solo 回调(共享 _qclaw/_traesolo_provider_helper)
_api_keys.py        5 个
_model_config.py    unified-models 2 + models 2 + aliases 2
_stats.py           stats/credit 4
_logs.py / _settings.py / _codex.py 各 2
_traework.py        2
```

- 鉴权/读体全走 deps 调用期读取(现状已满足);**删除 admin.py:39 死 ADMIN_TOKEN 导入**(前提二的活体样本)。
- **迁移顺序(每步独立提交)**:①路由金样测试先行 → ②`_api_keys.py`(test_perf_admin_api 直调 6 处,网最密)→ ③logs/settings/codex → ④_model_config → ⑤stats+traework → ⑥custom_channels → ⑦channels → ⑧channel_logins → ⑨accounts(最大最后)。
- 每步验收:608+ 全绿 + openapi 快照逐字节相等 + `import gateway.server` 冒烟。
- 预期形态:1577 → 约 350 行包壳 + 9 个 100-300 行子模块,纯搬运可评审。

### 2.2 chat_grammar.py 语法层统一(架构师,验证过的缺口)

- 统一的是"chunk 语法+累积+终态裁决"层:`_ChatStreamObserver`(proxy.py:255)抽为 `upstream/chat_grammar.py`,responses.py:707-755 的重复解析(:822-876 的 tool arguments 累积、:908-945 终态裁决)改用之。
- **Responses 事件发射层保持独立**——那是真翻译不是重复。
- 关键收益:provider 边界流(qclaw/chat.py:183 等)获得 observer 级校验,堵住缺口。
- 红线:golden SSE 字节哈希逐字节不变;observer 错误文案被测试断言不可改写。[净减 150-200 行]

### 2.3 provider 分层 Protocol(架构师+测试)

- protocol.py 补全:`CoreProvider`(9 个 5/5 方法)+ 能力 Protocol:`QuotaCapable`/`UpsertCapable`/`CheckinCapable`/`LoginCapable`(qclaw 异步双函数与 traesolo 同步三函数分别声明)/`DynamicModelsCapable`。
- 仅注解用;新增 tests/test_provider_contract.py 遍历已加载 provider,inspect 校验核心方法签名兼容 + 能力集与声明一致。
- [净增约 100 行,换 20 方法矩阵 CI 强制 | 零运行时行为变化]

### 2.4 pick 策略四副本收敛(算法,★主控代笔实测差分)

| 维度 | 正典 workbuddy | qwenwork `__init__` | qclaw `__init__` | traework `__init__` | traesolo |
|---|---|---|---|---|---|
| 选中账号过期处理 | 直接返回 | **主动刷新** | trae_shared | 主动刷新 | chat._pick |
| refresh 负缓存 | **无** | **无** | 有(JprxError 域) | **无** | 有(本地) |
| 异常域 | bool 返回 | 裸吞 Exception | 仅 JprxError | 裸吞 | 分类 |

- **这是 32 号轮的真实漏网**:chat 层三家已接负缓存,门面层(pick_account_with_fallback,被 ensure_usable 每请求调用)的 qwenwork/traework 仍是无负缓存旧副本,workbuddy 正典同样没有。
- 收敛设计:共享函数上移 `accounts/scheduling.py`(避开 providers→accounts 循环导入),`trae_shared.pick_with_refresh_fallback` 变薄壳再导出;签名增可选 `proactive_refresh: bool`(保留 qwenwork/traework 的选中账号主动刷新语义)与既有 `refresh_errors` 异常域参数(qclaw 的 JprxError 域保留)。
- 先行:四家各一条差分特征测试;[净减约 70 行 + 补两通道负缓存缺口 | 风险中]

### 2.5 per-channel 健康视图(算法+前端,★主控代笔)

- 只读聚合五字段:cooling 账号数与最早恢复时刻、refresh 负缓存剩余、11128 武装状态、近 1h 5xx 计数(查 logs)、first_token_ms 7 天 P95(已有)。窗口固定 1h+7d。
- 呈现:channels 页行徽标(绿/黄/红)+ 悬浮卡五字段;P0 只有徽标。
- 端点:`GET /admin/channel-health`(只读聚合,零新状态机);**反对**:ML、滑动平滑、跨进程健康分。
- 附带:credential_error 账号数透出 /health。

### 2.6 前端共享 store + 契约锚(前端/测试,★主控代笔)

- **sharedStore**:app.js 挂 `reactive({channels:null})` + `ensureChannels()` 单飞去重(复用 api.js INFLIGHT),消除四页 channels 下拉的三种不一致形状(usage 不过滤/keys 过滤 enabled/models 双源);[净减 ~30 行]
- **契约锚**:app.openapi() 快照测试(method+path+200 关键字段)进 tests/,金样落 tests/fixtures/——admin 拆分的验收从主控手工升级为 CI 常驻,前端零改动自保。
- **反对**:Pinia 式全家桶、为 10 个页面上状态机。

## 3. 并发与流式资源裁决(高级后端,★主控代笔,附实验数据)

- **锁竞争实验**(tests/bench 可转正):写路径 p95 62ms@N1 → 119@4 → 191@8 → 272ms@16,p50 恒 20-43ms;读路径无恶化。
- **裁决:维持现状**,记录升级条件——常驻并发流 >8 且 reserve p95 >200ms 时,做 DB_PATH 感知写队列化(单写线程+queue,isolated_db 切队列键)。数据不支持现在动。
- **故障架构**:三套内存状态机(账号冷却 5s×2^n、refresh 负缓存 60-600s、11128 武装)作用域各异属可接受现状,缺的是观测面——§2.5 健康视图即最小改进。
- **流式资源治理**:8MiB 上限+连接池 64 已是边界,慢客户端反压由 asyncio/uvicorn 天然承担;缺并发流数仪表(并入 §2.5)。

## 4. 安全网与验收(测试工程师,★主控代笔)

三层新安全网,全部先于对应重构落地:
1. **路由金样**:app.openapi() 快照测试(admin 拆分第 ① 步即建,拆分全程逐字节相等);
2. **协议契约测试**:五家 9 方法签名 + 能力集声明(Protocol 落地的同门验收);
3. **策略差分特征测试**:pick 四家各一条(收敛前后等价)+ checkin 已有的两家特征测试延续。

红线延续 33/34 号:golden SSE 哈希、credit 口径禁碰清单、测试断言零改动(新安全网文件除外)、单命令 `pytest -q` 全绿为最终门禁。

## 5. 实施排期建议(5 人日)

- **D1**:路由金样 + Protocol 契约测试(两道网先立)。
- **D2-D3**:admin.py 九步拆分(每步独立提交+快照验证)。
- **D4**:chat_grammar.py 抽取(golden 钉住)→ pick 收敛(差分测试先行)。
- **D5**:健康视图(后端聚合 + 前端徽标)→ sharedStore/契约锚 → 终验四件套(路由 diff/bench 双跑/全量/手测清单)。

## 附:六角色立场来源

- 架构师(代理):协议面重裁决、admin 拆分执行设计、chat_grammar 判断、自加深水区 pick 四副本。
- 高级后端(★主控代笔,含实测锁竞争实验):并发裁决、故障架构时序、流式资源治理、架构师四项逐条表态。
- 算法工程师(★主控代笔,四家实现逐一实读):pick 差分表与收敛设计、健康建模、延迟信号边界(不做,攒两周数据)。
- 高级前端(★主控代笔,grep 实测):状态复制面、sharedStore 方案、契约锚、健康视图消费、channels.js 拆分建议(本轮做收尾)。
- 测试工程师(★主控代笔):三层安全网、红线延续。
- 产品经理(★主控代笔):5 人日上限、六项决议、"上一轮单独一轮"的承诺兑现为本轮 admin 拆分。
