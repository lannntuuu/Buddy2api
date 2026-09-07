# 核心/边缘功能划分 · 小而美改造方案(37)

> 第五轮六角色讨论(架构师/产品经理/高级后端/高级前端/算法工程师/测试工程师)。
> 议题:哪些是核心功能、哪些是边缘功能、如果要精简成小而美的项目应该如何下手。
> 本文是决策方案,不直接实施;§5 给出可执行的分批切割清单。
> 关键输入:**这台部署近 30 天真实用量**(实测,探针数据已清理)——
> workbuddy 8005 次、traesolo 1409 次、traework 45 次、**qclaw 0(零账号)、qwenwork 0(零账号)**、bailian 3 次;5 个 custom 类型 Key 在用。

---

## 1. 核心功能地图(T0/T1/T2,产品经理定义 + 依赖边修正)

**判定法**:删掉后"导入账号 → 发一条流式请求 → 拿到合法响应"是否仍完整走通。

### T0 核心循环(产品存在的理由,一行不能少)

| 模块 | 规模 | 理由 |
|---|---|---|
| v1 路由 + SSE 流式(/v1/chat/completions、/v1/responses) | upstream/ 全部 | 产品出口 |
| auth_manager 调度(pick/粘住/冷却/fallback)+ trae_shared 策略 | ~1200 行 | 稳定性本钱(架构师特别提醒:auth_manager.py:27 已引用 pick_with_refresh_fallback,trae_shared 属核心不可随 trae 系切) |
| credential_crypto + fingerprint | ~500 行 | 凭据加密 + **官方指纹伪装头——workbuddy 主力通道的风控前提**(架构师:产出四组出站头) |
| 各保留通道的 chat/token/store 实现 | 按保留通道 | 协议本体 |
| model_config + aliases | 258+117 | 白名单/别名/倍率,全通道依赖;aliases 也是 codex 清洗的幸存依赖 |
| chat_grammar + sse + retry + http_pool | ~600 | 四轮优化的语法层与连接资产 |
| API Key 鉴权 + 每日配额 | api_keys 全部 | 入口治理 |

### T1 管理台与运维(个人用户不读日志、不改库就能过日子)

| 模块 | 规模 | 备注 |
|---|---|---|
| admin 包(channels/accounts/api_keys/stats/logs/settings/model_config) | ~1400 | 55 handler |
| 前端五页:dashboard/channels/keys/logs/settings | ~880 | 小而美控制台形态 |
| backup.py | 231 | 迁移前快照,自部署敢升级的底气 |
| 签到(checkin) | 三层 ~500,牵连 14 文件 | **额度续命机制**:traework/traesolo 的订阅额度靠它维持;与额度缓存共用数据结构 |
| 登录向导(SOLO/qclaw)+ setup 页 | ~490 | 补号便捷途径;**因 traesolo 是主力通道,缓删**(等替代方案确认) |
| 健康观测(/admin/channel-health + /health) | ~200 | 新增资产,排障用 |

### T2 边缘(可砍/可插拔)

| 模块 | 规模 | 实测使用 | 处置 |
|---|---|---|---|
| **qclaw 通道全包**(chat/store/jprx/oauth/sign/constants + 登录端点) | ~1090,12 文件 | **0 请求 0 账号** | **第一刀,整删** |
| **qwenwork 通道全包** | ~1240,9 文件 | **0 请求 0 账号** | **第一刀,整删** |
| codex 客户端特化 | ~270,7 文件 | 5 个 Key 均为 custom 类型 | 第二刀,低风险 |
| traework 报表链(sync_traework_usage + 端点) | ~130,5 文件 | 45 次/30天 | 第三刀,chat/token/quota 本体不动 |
| moderation/compaction(工具停顿/11128 自愈) | 421 | workbuddy 路径 | **保留**(主力通道的稳定性自愈;PM"挂开关后删"被架构师与后端否决:proxy 实调且是长流稳定性本钱) |
| pricing(traesolo credit 估算) | 69 | traesolo 在用 | 保留(随通道走,69 行不值得动) |
| custom_channels + 自定义通道子系统 | ~800 | 3 次请求/5 Key | **保留但标记为可插拔示范**(后端反对全删:5 个 custom Key 在用) |

## 2. 争议仲裁记录

1. **sign.py 是否跨通道**:PM 称跨通道,后端实测仅 qclaw 包内引用(traework/token 用 cryptography EC 自签)→ 可随 qclaw 删。教训:依赖断言以 grep 实测为准。
2. **trae_shared 是否随 trae 系切**:架构师实测 auth_manager.py:27 引用其 pick_with_refresh_fallback → 属核心,保留(算法工程师在本轮之前把它上移时已确认无循环导入,方向正确)。
3. **签到归层**:PM 判 T1(额度续命),后端给出 -500/14 文件的删除成本 → **归 T1 保留**,但若用户确认不再用 traework/traesolo 签到,可整层删除(与通道删除同批)。
4. **核心包通道集**:架构师"仅 workbuddy"(未见用量数据)vs 实测 workbuddy+traesolo 双主力 → **manifest 参数化**:通道集是配置不是结论(见 §3)。
5. **登录向导**:PM T1 + 后端"中高风险" → **缓删**,等确认补号替代方案。

## 3. 发布形态:唯一推荐"manifest 重放"(架构师方案 C 变体)

三条路线:A 删除式单分支(同步即冲突,弃)、B 能力开关(代码不减,仅运行面收窄,弃)、**C manifest 重放(推荐)**:
- **主分支永远全量**,跟随上游修复;
- slim 发行版由 `build_slim.py` 按 **manifest(数据文件,通道集是参数)** 在干净 worktree 重放"删单 + 守卫补丁"再生,永不手工 merge;
- 守卫仅 L2 三点:providers/__init__ 注册表、admin 包 include、protocol.py ChannelId Literal;
- 升级路径:main 先合上游 → build_slim.py 重放 → 门禁(核心 profile 测试 + 路由金样)红灯即 manifest 过期;上游新文件经 import 闭包检查,仅被已删者引用则入删单。

## 4. 小而美版目标形态(两档)

| 档位 | 通道 | 删除内容 | src 规模 | 路由 |
|---|---|---|---|---|
| **推荐档**(贴合本部署) | workbuddy + traesolo + traework(签到) | qclaw+qwenwork 包、codex 特化、traework 报表链、登录向导(缓)、custom 系(可选包) | ≈20226 → **≈17400(-14%)** | 63 → ≈50 |
| **激进档**(archiver 幻想形态) | 仅 workbuddy | 再砍 traework/traesolo 全包、checkin、quota/usage 页 | ≈12500(-38%) | 50 |

前端:小而美控制台 = dashboard/channels/keys/logs/settings 五页(≈880 行);quota/usage/models 页随功能层决策。

## 5. 分批切割清单(每批独立提交 + 金样/契约验证)

- **批次一(低风险,约 -2600 行)**:qclaw 整删(按 8 步删通道 SOP,后端已给样板清单)→ qwenwork 整删 → 协议面 Literal 收口 → 前端分支与测试整删(qclaw 175 行/qwenwork 152 行测试)。
- **批次二(低风险,约 -400 行)**:codex 特化清洗(7 文件)→ traework 报表链。
- **批次三(缓,等用户确认)**:登录向导与 setup 精简(需确认 traesolo 补号替代);checkin 整层(需确认放弃签到);custom_channels 可选包化。
- 每批验收:路由金样重冻并 diff 审查 + 契约测试矩阵更新 + 全量绿 + 手测 T0 流程(导入→流式→日志)。

## 6. 角色立场索引

- 产品经理:三层判定法(T0/T1/T2)、"三条美"标准、砍单顺序、六个决策输入;反多发行版/插件系统。
- 架构师:L1/L2/L3 机械可行性分级、manifest 重放发布形态、核心包文件树与 -38% 幻想形态、升级路径四步。
- 高级后端:逐项工程清单(qclaw -1090/12 文件、qwenwork -1240/9 文件、checkin -500/14 文件、codex -270/7 文件)、删通道 8 步 SOP、保留项边界收紧、反对清单(fingerprint/store_common/custom 全删)。
- 算法工程师(★主控代笔):机制面(调度/负缓存/退避/健康)全部保留——策略层无肥肉;pricing 随通道走;协议面 Literal 收窄是类型层收益。
- 高级前端(★主控代笔):页面映射(五页 ≈880 行为控制台形态)、_login_import/setup 随向导决策、hash 路由与静态断言同步义务。
- 测试工程师(★主控代笔):631 用例按功能 taxonomy 归属;删除式=删对应测试而非跳过;双 profile CI(全量+核心)+ 双路由金样;断言强度不许降。
- 主控仲裁:以实测使用数据定通道集;manifest 通道集参数化;签到/登录向导缓删等确认。
