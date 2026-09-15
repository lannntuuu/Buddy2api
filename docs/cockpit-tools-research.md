# cockpit-tools 开源项目借鉴分析

- 调研分支：`research/cockpit-tools`
- 调研日期：2026-07（main @ 6e776e4）
- 目标项目：[jlcodes99/cockpit-tools](https://github.com/jlcodes99/cockpit-tools)（约 17.5k stars，1.5k forks，活跃维护）
- 许可证：**CC BY-NC-SA 4.0（非商业）**。本报告只做机制分析与设计层面参考，**不建议拷贝其代码**；若后续落地，均为在本仓库独立实现。

---

## 1. 项目概览

**cockpit-tools 是什么**：Tauri + Rust 桌面应用，统一管理 16+ 款 AI IDE/CLI（Antigravity、Codex、GitHub Copilot、Windsurf、Kiro、Cursor、Grok CLI、CodeBuddy、CodeBuddy CN、Qoder、Trae 全家桶、Zed、ZCode）的账号：多账号导入/切换（把凭据写回客户端）、配额监控、自动唤醒（wake-up）、多开实例管理、18 语言 i18n，并内嵌一个 Go sidecar（`sidecars/cockpit-cliproxy`，CLIProxyAPI v7 fork）为 Codex 账号提供本地 API 服务。

**与 Buddy2api 的关系**：定位互补——它是「桌面账号管理器」（把网关身份写进 IDE 客户端），Buddy2api 是「本地 OpenAI 兼容网关」（把 IDE 身份读出来转发请求）。两者在 **CodeBuddy/WorkBuddy 与 Trae 的凭据理解、token 刷新、配额/签到 API** 上高度重叠，是本项目最直接的参照系。

**架构速览**：
- 后端 `crates/cockpit-core`（Rust，~90 个模块文件）+ `crates/cockpit-cli`（简陋 CLI）
- 前端 React + zustand + i18next（`src/`，单 App.tsx 149KB 巨石 + 每平台一套 store/service）
- 存储：`~/.antigravity_cockpit` 下「索引 JSON + 每账号 JSON 文件」，原子写 + `.bak` 回滚；**凭据明文**（无加密，依赖 OS 用户隔离）
- sidecar 通信：stdout JSON 行事件 + manifest 配置投影 + 配额状态文件轮询 + 父进程监护自毁

---

## 2. TL;DR：最有价值的借鉴点（Top 12）

| # | 借鉴点 | 价值 | 成本 | 详见 |
|---|--------|------|------|------|
| 1 | **随机时窗自动签到调度**（窗口内每账号随机分钟、状态去重、5min→1h 退避、按日合并日志、默认关） | 高 | 低 | §4.1-A1 |
| 2 | **后台定时配额刷新**（按通道间隔、复用现有 credit_cache TTL/SWR、兼作 token 探活） | 高 | 低 | §4.3-C1 |
| 3 | **配额错误与 token 刷新解耦**（配额查询失败不拖垮刷新；`quota_query_last_error/_at` 落盘可见） | 高 | 低 | §4.1-A2 |
| 4 | **持久配额冷却护栏**（「确认的配额耗尽」与瞬时错误分存，运行时重置逻辑不能误放真正耗尽的账号） | 高 | 低 | §4.4-D1 |
| 5 | **全池枯竭探测恢复**（无可用账号时清空可恢复账号的运行时冷却探测一次，仅一次，失败才 503） | 高 | 低 | §4.4-D2 |
| 6 | **上游错误逐字透传 + 错误分类入日志**（429/502/503 与上游错误码不重写；日志记 `error_category`） | 高 | 低 | §4.4-D3 |
| 7 | **JSON 原子写 + `.bak` 自动回滚 + 隔离（quarantine）** | 高 | 低 | §4.5-E1 |
| 8 | **统一配额呈现层**（quotaItems 数据驱动卡片/表格/弹窗，10/30/60 统一色阶阈值） | 高 | 低 | §4.6-F2 |
| 9 | **定时备份 + 保留策略清理 + 可选远端同步（WebDAV）** | 高 | 中 | §4.6-F1 |
| 10 | **refresh_token 驱动的账号导入/导出**（迁移只传 refresh_token 现场换发；逐账号成功/失败上报；两档导出脱敏） | 高 | 中 | §4.5-E2 |
| 11 | **唤醒任务框架**（最小真实请求 + 前后配额快照验证重置 + 结构化失败分类；合规敏感，默认关） | 中高 | 中 | §4.3-C2 |
| 12 | **按模型配额保护 + 预警/自动切号阈值**（`protected_models`、20% 预警 / 5% 切号） | 中 | 中 | §4.3-C4 |

其余中低价值项见 §3 总表。

---

## 3. 借鉴点总表（按域归组）

### A. 账号与配额生命周期
| 项 | 价值 | 一句话 |
|----|------|--------|
| A1 随机时窗自动签到调度 | 高 | 30s 轮询调度器 + 每账号随机 `scheduled_minute` + 状态去重 + 指数退避 + 当日合并日志 |
| A2 配额错误与刷新解耦 + 落盘 | 高 | refresh 失败仅 warn 继续；`quota_query_last_error/_at` 持久化并在状态摘要暴露 |
| A3 dosageNotifyCode 服务端告警 + 10min 冷却 | 中 | 服务端通知码非 `USAGE_NORMAL` 即告警，HashMap 冷却防刷屏 |
| A4 签到响应宽松解析 | 中 | snake/camel 双命名、bool 兼容 0/1/"true"、`active` 缺省 true、仅 `code==0` 成功 |
| A5 重复账号 uid 冲突检测 | 中 | 同 uid 但 email 不一致 → 拒绝合并（SQLite 下只需判重） |
| A6 state.vscdb 直读兜底导入 | 低 | DPAPI+AES-256-GCM 跨平台解密，现有 `.info` 链路失效时才值得做 |

### B. Trae 系
| 项 | 价值 | 一句话 |
|----|------|--------|
| B1 运行中 IDE 会话重读同步 | 中高 | token 失效时从客户端 storage.json 重读 IDE 自己刚刷新的 token（校验 uid + 过期时间） |
| B2 刷新双段 fallback + best-effort 同步 | 中 | v3 ExchangeToken 失败回退旧接口；成功后串调用户态/配额，失败只记日志 |
| B3 登录 pending 持久化 + PKCE authCode | 中 | 登录会话落盘可跨重启；回调凭证升级为 auth_code+PKCE（traesolo `_logins` 现仅内存） |
| B4 签到调度窗 + 重试 + 按日日志 | 中 | 前端时间窗调度策略（失败 5min / 空闲 1h 复检 / 完成跳次日），网关用后台任务实现 |
| B5 导入直接接受原生 storage.json | 中 | 检测 `iCubeAuthInfo://` 键即走解密导入，管理页整文件粘贴可用 |

### C. 配额引擎与唤醒
| 项 | 价值 | 一句话 |
|----|------|--------|
| C1 后台定时配额刷新 | 高 | 每平台 `*_auto_refresh_minutes`（默认 10、-1 禁用）驱动定时刷新，60s 缓存压成本 |
| C2 唤醒任务框架 | 中高 | 定时/重置事件触发最小真实请求，`quota_before/after` 验证，失败分 verification/quota/temporary/generic |
| C3 重置检测的最小刷新间隔约束 | 中 | 保存 quota_reset 任务时服务端把刷新间隔钳制到 ≥2 分钟并回执提示 |
| C4 按模型保护 + 预警/自动切号 | 中 | `protected_models` 只禁单模型；alert 20% / auto_switch 5% 阈值 + 分组范围 |
| C5 forbidden/invalid_grant 状态机 | 低中 | 403→`is_forbidden` 区分封禁与查询失败；disabled 三字段原子重置、刷新成功自动恢复 |

### D. Codex sidecar / 请求路径
| 项 | 价值 | 一句话 |
|----|------|--------|
| D1 持久配额观测 + 独立冷却护栏 | 高 | 配额耗尽状态独立于可重试错误，`ResetAuthState` 不能移除它，`UpdatedAtMS` 新者胜 |
| D2 全池枯竭探测恢复 + 单次重选 | 高 | ctx 标记防环；凭据失效账号不参与探测 |
| D3 上游错误语义透传 + 错误分类 | 高 | `errorCategory()` 产出 quota_or_rate_limit / first_byte_timeout / client_canceled 等入库 |
| D4 流式 open/idle 超时拆分 + 首帧前重开 | 中 | open 10s 独立重试（无退避）、idle 60s 按 chunk 重置、15s `: keep-alive` 注释帧 |
| D5 模型通配规则 + 每 key 路由/优先级 | 中 | 带锚定 `*` 白/黑名单、`preferred` 账号列表按 key 排队首 |
| D6 模型日期快照别名 + context_window | 低 | `gpt-5.1-2025-08-22→gpt-5.1` 归一；/v1/models 补 context_window |
| D7 池级失败诊断明细 | 低 | 503 时附 {候选数, 冷却中(含剩余秒), 逐账号原因}，数据现成 |

### E. 核心基础设施
| 项 | 价值 | 一句话 |
|----|------|--------|
| E1 JSON `.bak` 回滚 + quarantine | 高 | 写前 copy `.bak` → temp+rename；解析失败自动回滚；坏文件改名隔离不删除 |
| E2 refresh_token 导入导出 | 高 | 宽松提取 + 现场换发补全身份 + 身份去重 upsert + 逐账号失败不中断批次 |
| E3 公告 / 远程配置 | 中 | 运行时从 raw.githubusercontent 拉 `announcements.json`（1h 缓存、版本/语言/过期过滤、已读持久化）；无签名校验 |
| E4 配置字段级迁移 + 未知键保留 | 中 | 缺键继承旧键值并写回；`flatten extra` 保留未知字段；解析失败隔离回退默认 |
| E5 日志邮箱脱敏 | 中 | 全局 `mask_email`（本地段 ab\*\*\*z + 域名打码），日志表写库前同样脱敏 |
| E6 OAuth pending 持久化 | 低 | 浏览器授权中途状态落盘、重启恢复/过期清理 |
| E7 CLI | 低 | 其 CLI 完成度低（quota 未实现），收益低；HTTP-first 的网关可只包一层 admin 端点 |

### F. 前端 UX
| 项 | 价值 | 一句话 |
|----|------|--------|
| F1 定时备份 + 保留 + WebDAV | 高 | 24h 到期判 → 导出 → 写盘 → 按 retention 清理 → 可选 WebDAV 上传（远端同样清理） |
| F2 统一配额呈现层 + 色阶 | 高 | `quotaItems{label,pct,valueText,resetText,cls}` 三处展示共用，10/30/60 阈值 |
| F3 事件驱动刷新（SSE） | 中 | 账号变更 emit 事件，视图失效重取；后端可加 `/admin/events` SSE，失败退化手动刷新 |
| F4 账号标签 + 批量操作 + 筛选持久化 | 中 | tags 列 + 多选批量启停/删除，筛选条件 localStorage 恢复 |
| F5 浮动卡片 → 迷你面板 | 低中 | 桌面悬浮窗不适合无壳网关，可降级为管理页 mini 路由页（60s 自刷新） |
| F6 首屏优先 + 延迟批量预取 | 低 | 核心列表先行、健康数据延后错峰 |
| F7 隐私脱敏开关 | 低 | 全局掩码邮箱/UID 列，localStorage 持久 |

---

## 4. 分域要点与落地建议

> 各域完整报告（含 cockpit 函数级引用与缺口清单）见本地 `.tmp/reports/A-codebuddy.md` ~ `F-frontend-ux.md`（`.tmp/` 不入库）。

### 4.1 CodeBuddy / WorkBuddy（域 A）

**机制要点**
- 客户端 token 存 `state.vscdb` ItemTable 的 `secret://...planning-genie.new.accessTokencn`（VSCode safe-storage：DPAPI+AES-256-GCM，`vscode_inject.rs::decrypt_windows_gcm_v10`）；Buddy2api 走 `.info` + DPAPI 链路，两者是同一客户端的两条读取路径。
- 刷新 `POST /v2/plugin/auth/token/refresh`（`X-Auth-Refresh-Source: plugin/ide-main`）；**刷新失败仅 warn，用现有 token 继续**；配额查询失败独立返回，不拖垮刷新（`refresh_payload_for_account_inner` 返回 `(payload, quota_refresh_error)` 二元组）。
- 配额：`get-user-resource`（`ProductCode=p_tcaca`）+ `get-dosage-notify`（`dosageNotifyCode/zh/en`）+ `get-payment-type` 合并进 `quota_raw`；企业版改走 `get-enterprise-user-usage`；`Semaphore(5)` 限并发刷新。
- 签到：`checkin-activity-status`（回退 `checkin-status`）→ `daily-checkin`，**仅 code==0 成功**；`CheckinStatusResponse` 含 `streak_days/next_streak_day/is_streak_day/checkin_dates/streak_bonus_*`，宽松解析且 `active` 缺省 true。
- **自动签到**（`workbuddy_auto_checkin.rs`）：`{enabled, start_time:"06:00", end_time:"12:00"}`，每天每账号在窗口内**随机** `scheduled_minute`（反同步指纹）；30s 轮询 + 启动补漏 + 配置变更 Notify 唤醒；`IS_CHECKIN_RUNNING` 防重入；`!active` 不消耗当日排期整轮标 retry；失败 5min×2 退避至 1h；日志按日合并保留 30 天。
- 重复账号：并查集按 uid/email 归组合并，身份冲突拒绝（`normalize_account_index`/`merge_duplicate_account`）。

**落地建议**
- **A1 自动签到调度**：新增 `src/accounts/checkin_scheduler.py`，asyncio 后台任务按 `gateway_settings.json` 的 `checkin.auto.{enabled,start,end}` 启停；复用 `auth_manager.fetch_checkin_status`（预检）+ `claim_daily_checkin`；退避 5min→1h；当日明细写 SQLite（每账号每日一行）；管理页展示开关与当日结果。Buddy2api 当前注释明确「手动领取、不做自动定时」——若采纳必须**默认关 + 文案免责**（见 §6 合规）。
- **A2 配额错误落盘**：`fetch_account_resources`/`_resource_failure` 路径写 `last_error`/`last_error_at`（resource 缓存行或 accounts 表），`get_account_status` 摘要带出，管理页展示「额度查询失败原因」。纯增量字段。
- **A3/A4/A5**：`get-dosage-notify` 并入现有 60s 资源缓存（`dosage_notify_code` 进 credit-summary 告警维度）；签到响应加 camelCase 回退 + 统一「code==0 才 ok」；`_upsert_workbuddy` 加 uid/email 冲突拒绝。

### 4.2 Trae 套件（域 B）

**机制要点**
- 四客户端统一建模 `TraePlatformKind`（Trae/Solo/Cn/SoloCn），数据目录统一 `%APPDATA%\<产品>\User\globalStorage\storage.json`。
- 加密即 Buddy2api 已实现的 "byteCrypto"：`tc\x05\x10\x00\x00` 魔数 + 自包含密钥的 AES-128-CBC（与 `traework/crypto.py::decrypt_tc_b64` 同源，交叉验证一致）。
- 刷新 `POST /trae/api/v3/oauth/ExchangeToken`（ECDSA P-256 `DeviceProof` 签名 `POST\npath\nclient_id\nrefresh_token\nts\nnonce`），失败回退旧 `/cloudide/api/v3/trae/oauth/ExchangeToken`；成功后 best-effort 串调 GetUserInfo/CheckLogin/pay_status/ent_usage。
- 多区域 origin 白名单路由（grow-normal.trae.ai / growsg / traeapi.us / api.trae.cn…）。
- **运行实例保护**：刷新时对「客户端正在运行」的账号降级为仅额度刷新，并重读该实例 storage.json 同步会话（`merge_runtime_auth_json` 白名单保留运行时字段）——避免网关与 IDE 双向刷新互顶。
- Web 登录：PKCE(S256) + 新 P-256 设备密钥对 + 随机回环端口；`PendingOAuthState` 落盘（600s 过期、重启恢复）；`submit_callback_url` 手动粘贴兜底。
- 签到调度（前端 `traeAutoCheckinService.ts`）：默认 06:00–12:00 窗口 + 每日一次 + 失败 5min 重试/空闲 1h 复检 + 按日日志 30 天保留。

**落地建议**
- **B1 重读运行中 storage**：`traework/token.py::refresh_account` 失败或 401 时，若账号 extra 的 `auth_path` 存在则经 `session_to_account` 重解析，uid 一致且过期时间更新才回写——「重导入即刷新」兜底分支。
- **B2 双段 fallback**：traework v3 刷新失败回退 legacy body（参照 traesolo/token.py `exchange` 风格）；成功后 best-effort `fetch_quota`。traesolo Web 登录时生成设备密钥对存 extra，刷新升级 DeviceProof 新接口。
- **B3 pending 持久化 + authCode**：traesolo `login.py::_logins` 内存态落盘（SQLite 小表或 JSON）并启动恢复；`parse_callback` 加 authCode 分支。
- **B5 storage.json 直导**：`traework/store.py::parse_credentials` 检测 `iCubeAuthInfo://` 键转走 `import_discovered`，管理页可整文件粘贴。

### 4.3 配额引擎 + 唤醒任务（域 C）

**机制要点**
- 配额模型 `QuotaData{models: [ModelQuota{name, percentage, reset_time}], subscription_tier, credits, tier_id, is_forbidden}`；refresh 3 次/1s 重试，403 → `is_forbidden`。
- 自动刷新：`config.rs` 每平台 `*_auto_refresh_minutes`（14 平台字段，默认 10、-1 禁用）驱动前端 `useAutoRefresh` 定时器；磁盘缓存 TTL 60s。
- 阈值体系：每平台 `quota_alert_threshold`（默认 20%）+ `auto_switch_threshold`（默认 5%）+ 分组/账号范围模式；账号 `protected_models: HashSet` 受配额保护禁用的模型。
- 唤醒任务：四种触发（startup/daily-weekly-interval 定时/crontab/**quota_reset 事件驱动**），默认 prompt 'hi' + 最小模型；执行走真实官方通道（Antigravity 用本地 TLS 网关 + 官方 language_server 子进程 + protobuf 凭据注入；Codex 跑官方 CLI）；历史记 `quota_before/quota_after` 验证配额确实重置；失败分类 `verification_required(带 validationUrl)/quota/temporary/generic`；auto|confirm 执行模式。
- 约束：保存 quota_reset 任务时 `ensureMinRefreshInterval(2)` 服务端把刷新间隔强制收紧到 2 分钟。
- 注意：代码中存在 `force_disable_0_8_14` 迁移键（某版本起把唤醒全局开关强制关）——该功能有合规敏感性。

**落地建议**
- **C1 后台定时刷新**：`gateway_settings.json` 加按通道间隔（分钟，0 禁用）；启动时起 asyncio 任务循环各通道，复用 `control_plane` 的 quota 聚合与 `credit_cache`（TTL/SWR/generation 原样沿用）；管理页去掉「必须手动刷新」依赖，只显示 snapshot_age。周期请求兼作 token 探活。
- **C2 唤醒任务**：SQLite 任务表（channel/account_ids/models/prompt/schedule/last_run/next_run + 历史表带 before/after）；「最小真实请求」复用现成接缝（workbuddy 用 `test_account_chat`，trae 系用 provider chat）；`at_reset` 模式只对有 reset_time 语义的通道先做。**默认关 + 免责文案**。
- **C3**：重置检测开关开启时服务端钳制刷新间隔到最小值并回执。
- **C4 按模型保护**：`QuotaSnapshot.extra` 加按模型 remaining（先支持能给细度的通道）；accounts 表加 `protected_models` JSON 列；`pick_account_with_fallback` 把保护账号并入 exclude。trae 系只有 session 级 model_name，先做整账号禁用。
- **C5**：配额行加 `forbidden` 标志与 `quota_error`；注意别把限流 403 误判为封禁（cockpit 只在配额接口上标记）。

### 4.4 Codex sidecar（域 D）

**机制要点**
- 独立 Go sidecar（CLIProxyAPI v7.2.155 fork）：stdout JSON 行事件（`ready/auth_result/usage/...`）+ manifest 配置投影 + 配额状态文件（1s 轮询、sha256 判变、失败保最后好快照）；`parent_monitor` 监护父进程退出即自毁。
- **canonical accounting**：主进程是 token/配额唯一事实源；「已确认的配额观测」写成独立护栏状态，`ResetAuthState`（清瞬时错误）无法移除它；合并按 `UpdatedAtMS` 新者胜。
- 选号：selector 链（session affinity → backup → quota 预留 → 模型排除 → **quotaCooldown** → recording）+ 分层过滤 + 多策略排序（quota_high/low_first、plan_low_first、expiry_soon_first、weighted 轮询）。
- 恢复：全池枯竭 → `maybeAutoRecoverAuthPool()` 对可恢复账号清运行时状态 + 清冷却，**仅重选一次**（ctx 标记防环）。
- 错误：429/502/503 与上游错误码（server_is_overloaded/slow_down/usage_limit_reached）**逐字透传**（有测试断言）；`errorCategory()` 分类入库。
- 流式：open 10s/idle 60s 拆分；open 超时立即重试（默认 2 次无退避）；idle 按 chunk 重置 + 15s `: keep-alive` 注释帧；`responsesSSEFramer` 修复单 data 内拼接多 JSON。
- 模型目录：manifest 推送 + 每 key 可见性（通配 `*` 白/黑、前缀）+ 日期快照别名归一 + 每账号 context_window 覆盖。

**落地建议**
- **D1 持久冷却护栏**：`auth_manager._account_failures` 目前纯内存（重启即失）；accounts 表或 `gateway_settings.json` 加 `quota_cooldown{exhausted, reset_at_ms, updated_at_ms, source}`；provider 观察到 credit=0 / 配额 429 时写入；选号前硬过滤；只有新的「有余额」观测清除。
- **D2 探测恢复**：`pick_with_refresh_fallback` 返回 None 时加 `probe_recover_once()`——剔除手动禁用与 401 过期账号，清空其余冷却重选一次，失败恢复原冷却值；调用点在 `proxy.py` 的 `if not account: break`。
- **D3 错误透传 + 分类**：日志表加 `error_category`；分类函数放 `src/providers/retry.py` 或新 `src/upstream/errors.py`；429 分支保留原始 body 与 Retry-After；channel-health 按类别聚合 429 占比。
- **D4 流式超时拆分**：`_stream_upstream` 首帧等待拆出 open 超时（默认 15s 可配），`output_started=False` 时对同账号重开一次；SSE 泵空闲下发注释帧。
- **D5/D7**：`_check_model_access` 升级通配规则（约 30 行）；最终 503 的 detail 附逐账号原因（`tried_ids`+`_account_failures` 数据现成）。

### 4.5 核心基础设施（域 E）

**机制要点**
- 原子写三件套（`atomic_write.rs`）：`write_string_atomic`（copy `.bak` → 同目录 temp → rename）、`parse_json_with_auto_restore`（解析失败自动回滚 `.bak`）、`quarantine_file`（坏文件改名 `<名>.<原因>.<时间戳>` 并清理旧隔离件）；账号索引/详情、config、公告缓存、OAuth pending 全走它。
- 导入（`import.rs`）：四类来源（旧数据目录/IDE state.vscdb/JSON/VS Code SecretStorage）；`extract_import_entry` 宽松提取（容忍 `token.refresh_token` 嵌套、文件名 `_at_`→`@` 推 email）；现场 `refresh_access_token`+`get_user_info` 补全身份；按身份去重 upsert；失败入 `FileImportFailure` 不中断批次 + 进度事件。
- 配置（`config.rs` 74KB）：单文件 ~100 字段全 `serde(default)` + `flatten extra` 保留未知键；字段级迁移（缺键继承旧键值）；解析失败隔离回退默认；`config.json.lock` 文件锁 + 锁内重读防丢失更新。
- 公告：运行时从 raw.githubusercontent 拉 `announcements.json`（**无签名校验**，信任 GitHub 源），1h 本地缓存、版本/语言/过期过滤、已读持久化；`remote-config.json` 驱动版本更新提示（无自动下载）。
- i18n：17 语言 `include_str!` 编译进二进制，区域回退 + `{{name}}` 插值，前后端共用键空间。
- 安全：凭据**明文** JSON（无加密无 chmod，仅 OS 用户隔离）；日志全局邮箱脱敏。
- CLI（cockpit-cli）：仅 list/switch/quota 三命令，quota 未实现——完成度低。

**落地建议**
- **E1 原子 JSON**：新增 `src/storage/atomic_json.py`（write_json_atomic / read_json_auto_restore / quarantine），先改造 `gateway_settings.py::_write_atomic`（现只有 tmp+replace 无 .bak）与各 provider JSON 落盘点；SQLite 侧已有 `backup.snapshot` 兜底不动。
- **E2 导入导出**：管理页 JSON 导入（`[{email, refresh_token|session_state,...}]`，upsert 去重 + 逐账号失败原因 UI）；导出两档——全量（含密文凭据）与脱敏版（剥 access/refresh/session_state，仅留元数据 + refresh_token 供跨机迁移）。
- **E3 公告**：`data/announcement.json`（Releases 或自托管源）+ 1h 缓存 + 管理页顶部展示；用于通道兼容说明、签到政策变化、弃用预警。
- **E4 配置迁移**：`config.toml` 新增/改名键时「缺键继承旧键值并写回」，放 `_lifespan` 或 `repos/settings.py` 做一次性 seed；`gateway_settings.json` 已保留未知键，保持。
- **E5 邮箱脱敏**：logging 挂全局 Filter/Formatter + `repos/logs.record_request` 写库前对 `error_msg` 脱敏（请求日志表是另一个泄露面）。

### 4.6 前端 UX（域 F）

**机制要点**
- 分层 `view → zustand store（通用工厂 + 每平台薄包装）→ service(Tauri invoke) → Rust`；localStorage 启动水合 + 请求序号丢弃过期响应 + 跨窗口 `emitAccountsChanged` 事件。
- Dashboard：17 平台卡片聚合，首屏只拉核心、6s 后按 1 个/1200ms 延迟预取；主仪表盘**无轮询**（仅手动刷新）。
- 浮动卡片：独立置顶窗口，60s 静默自刷新，监听切号/平台焦点事件跟随，`recommendedAccount` 提示余量最大账号。
- 账号表：控制器/视图分离；compact/list/grid 三视图；搜索 + 套餐/状态多选 + 标签筛选 + 按标签分组；批量工具条（删除/分组/测试）；分页持久化。
- 配额着色：`getQuotaClassByRemainPercent`（≤10 critical / ≤30 low / ≤60 medium / 其余 high）卡片/表格/浮窗共用。
- 自动备份：24h 到期判 → `exportDataTransferJson` → 写盘 → 按 `retention_days` 清理 → 更新水位；WebDAV 启用时上传（远端按保留期清理，返回 uploaded/deleted 明细）。
- 迁移 bundle：`{schema:'cockpit-tools.data-transfer', sections:{accounts, config}}`，config 侧脱敏（剔除密码/目录），账号 ID 间接化为公开标识引用 + 导入时加权评分重匹配（uid 24/email 10/domain 4）。

**落地建议**
- **F1 定时备份**：`gateway_settings.json` 备份块（enabled/间隔/retention_days/可选 WebDAV）；网关进程内定时任务复用现有备份逻辑 + 按保留期清理；`settings.js` 加「备份」区（最近时间/手动/文件列表/恢复）。备份含凭据，远端同步必须显式启用并提示。
- **F2 统一配额呈现层**：新增 `src/web/js/quota_fmt.js`（或扩 `format.js`）：输入 `official_resource` 输出 `{label,pct,valueText,resetText,cls}` 数组 + 10/30/60 色阶，供 channels 账号表、quota 表格/弹窗、dashboard 概览卡共用。
- **F3 事件驱动**：`app.js` 现有 INFLIGHT 去重 + `invalidateChannels` 扩成小事件总线；后端可选 SSE `/admin/events` 推送账号状态变化（断线重连，失败退化手动刷新）。
- **F4 标签/批量**：accounts 表加 tags 列；channels 账号表加标签编辑/筛选/分组 + 多选批量启停/删除；筛选条件 localStorage 持久。
- **F5 迷你面板**：管理页加 mini 路由页（网关健康 + 各通道剩余积分 + 活跃账号，60s 自刷新）；真桌面悬浮窗与无壳纯 web 约束冲突，不做。

---

## 5. 建议实施顺序

**阶段 1 — Quick wins（各项约半天~1 天，纯增量、低风险）**
1. E1 JSON 原子写 + .bak 回滚 + quarantine
2. D3 上游错误透传 + `error_category` 日志分类
3. A2 配额错误与刷新解耦 + `last_error(_at)` 落盘
4. E5 日志邮箱脱敏
5. F2 统一配额呈现层 + 色阶
6. A5 重复账号 uid 冲突检测 / A4 签到宽松解析 / B5 storage.json 直导
7. D7 池级 503 诊断明细 / D5 模型通配规则（视需要）

**阶段 2 — 功能项（每项数天）**
8. A1+B4 自动签到调度器（默认关 + 免责文案 + 按日审计日志）
9. C1 后台定时配额刷新（按通道间隔）
10. D1 持久配额冷却护栏 + D2 全池枯竭探测恢复
11. F1 定时备份 + 保留策略（+ 可选 WebDAV）
12. E2 refresh_token 驱动的导入/导出（两档导出）
13. B1 traework 运行中 storage 重读兜底 / B2 刷新双段 fallback

**阶段 3 — 需产品判断**
14. C2 唤醒任务框架（合规敏感，建议先只做「重置前最小真实请求验证」的最小形态，默认关）
15. C4 按模型配额保护 + 预警阈值（依赖通道能否给出按模型 remaining）
16. B3 traesolo 登录 pending 持久化 + PKCE authCode
17. F3 SSE 事件驱动刷新 / F4 账号标签 + 批量操作
18. A3 dosageNotifyCode 服务端告警

**阶段 4 — 可选/远期**
19. D6 模型日期快照别名 / D4 流式 open/idle 拆分（结合线上超时问题再评估）
20. E3 公告系统 / F5 迷你面板 / F6-F7 前端小优化 / E6-E7（按需）

---

## 6. 风险与合规

1. **许可证**：cockpit-tools 整体 CC BY-NC-SA 4.0（非商业）。只借鉴设计/机制，不拷贝代码；其 Codex sidecar 源自 CLIProxyAPI（MIT）的 fork，引用相关思路时可回溯到该上游。
2. **自动签到/唤醒 = 对消费级账号的自动化操作**，属厂商服务条款灰区。证据：cockpit 代码中存在 `force_disable_0_8_14` 迁移键，某版本起把唤醒功能全局强制关闭。落地建议：**默认关闭、用户显式开启、UI 文案免责、随机时窗（反同步指纹）、严格限频（每账号每日一次）**。
3. **远程公告无签名校验**（信任 GitHub 源）。若借鉴公告机制，托管源必须自持（Releases/自建端点），不要指向他人仓库。
4. **凭据安全勿回退**：cockpit 明文存储 token（无加密/无 0600），Buddy2api 的 Fernet/DPAPI 双加密 + 0600 key 文件更优，保持。
5. 本报告基于 main @ 6e776e4 的代码与 README 交叉验证；个别推断项（如唤醒成功判定的轮询阈值、切号写回的明文结构）在各域报告「缺口」一节有标注，落地前需按目标模块复核。

---

## 7. 附录：研究方法

- 环境约束：本机 shell（git/curl/node/pwsh HTTPS）直连 GitHub 被拦截（schannel SEC_E_NO_CREDENTIALS / ECONNRESET），全部源码经 DSH `web_fetch` 拉取（raw.githubusercontent / GitHub API），大文件经 spill 文件 + grep/read 分段阅读。
- 组织方式：workflow 6 域并行子代理（CodeBuddy/WorkBuddy、Trae 套件、配额+唤醒、Codex sidecar、核心基础设施、前端 UX）；第一轮 A/B 两域因大文件上下文超限失败，以「先 grep 结构、限制抽读窗口」策略重派成功。
- 各域阅读范围与缺口明细：`.tmp/reports/A-codebuddy.md` ~ `F-frontend-ux.md`（§4 各节亦有摘要）。
- 交叉验证：cockpit 的 Trae byteCrypto（`tc\x05\x10\x00\x00` 魔数）与 `src/providers/traework/crypto.py::decrypt_tc_b64` 一致；自动刷新默认值与 README「推荐 5~10 分钟」一致；配额重置唤醒的刷新间隔最小值约束与 README 设置项说明一致。
