# TraeWork「自动退出登录」治理 Spec

- 编号：42
- 状态：**已完成**（自愈 + 启动对齐已实现并验证；见 §7 实现与复核记录）
- 关联文件：`src/providers/traework/{token.py, chat.py, __init__.py}`、`src/providers/trae_shared.py`、`src/gateway/server.py`、`tests/test_traework.py`

## 1. 根因（已实测确证）

TRAE 的 `refresh_token` 是**轮换制（单次票据）**：用一次 `ExchangeToken` 就换发新值，旧值立即作废。

实测证据（2026-09-19）：

```
POST https://api.trae.cn/trae/api/v3/oauth/ExchangeToken
→ 401 {"ResponseMetadata":{"Error":{"Code":"20101",
     "Data":{"__Message.error":"refresh token is invalid"},
     "Message":"Token 无效，refresh token is invalid.","StandardCode":"040012"}}}
```

推论链：

1. 网关与官方客户端**共用同一账号的同一 refresh_token 链**；
2. 任一方刷新，另一方手里的旧 refresh_token 即刻作废；
3. 于是出现「谁后刷新谁赢，另一方下次必掉」的互顶；
4. 网关侧还有一个放大器：**失败即躺平** —— refresh 401 后 `mark_account_failure`
   直接把账号标 `expired`，且请求停在选号阶段（见 §7.2），之后需人工重新导入，不会自愈。

注意：网关 DB 里 `refresh_expires_at` 显示到 2027-03，但这是**假象**——refresh_token 会因轮换而提前作废。

> 更正：本节初稿曾称 traework 存在「24h 预刷新窗口」。经核查**不成立**——
> 24h 预刷新（`needs_pre_refresh` / `REFRESH_SKEW_S`）只属于 **traesolo** 那条产品线；
> traework 走 `pick_with_refresh_fallback`，只有 5 分钟 skew 的「真过期才刷」。

## 2. 用户决策

- 客户端掉线**可接受**（用户明确表示不用管客户端）→ 采用**网关优先**策略；
- 同时要求：**尽量不掉** + **掉了能自动恢复**。

## 3. 实现设计

### 3.1 减少无谓刷新（网关自己别把票刷废）

现状核查结论：traework **本来就没有激进的预刷新**。
`pick_with_refresh_fallback`（`providers/trae_shared.py`）用
`is_token_expired(account, skew_ms=300_000)`（5 分钟 skew）——「真过期才刷」，已经够克制。

（24h 预刷新窗口 `needs_pre_refresh` / `REFRESH_SKEW_S` 只存在于 **traesolo** 那条独立产品线，
不在 traework 链路上；初稿把两者混为一谈，已在 §1 更正。）

因此本项**无需改动**，仅在实现时确认：

- 保持 5 分钟 skew 的「真过期才刷」语义，**不引入 24h 预刷新**；
- `chat.py::_turn` 内的账号重试循环，**同一回合内不重复刷新同一账号**
  （现有 `tried` 集合已保证不重选）；
- 刷新成功后**必须**把新 `refresh_token` 落库（`token.py::refresh_account` 已做）。

> 结论：traework 现状符合「能用就不刷」，**本次主要补 3.2 的自愈**。

### 3.2 失败即自愈：从客户端 storage.json 自动重读（核心）

新增「凭据自救」路径，在 refresh 失败（401/`20101`）时触发：

```
refresh_account 失败
  → 若账号 extra.auth_path 存在且文件可读
    → 调 store.import_discovered(auth_path) 重新解密客户端最新凭据
      → uid 一致且 expires_at/token 有更新？ → 回写 DB，状态置 active，重试
      → 无更新/uid 不一致/解密失败 → 维持 expired（不掩盖真实失效）
```

这是「客户端一登录，网关自动跟上」的关键，可直接消掉「掉线后必须手动重导」。

**最终落点**（实现后修正，详见 §7.2 —— 最初挂的位置走不到，已改到真正的故障路径）：

| 位置 | 作用 |
|---|---|
| `providers/traework/token.py::adopt_credentials_from_client(account, *, require_newer=False)` | 自救本体：读 `extra.auth_path` → `store.import_discovered` 解密 → uid 校验 → 更新判定 → 仅回写 token 字段 + `status=active` |
| `providers/trae_shared.py::pick_with_refresh_fallback(..., adopt_fn=None)` | **真正的接入点**：refresh 失败 / 负缓存命中时先试 `adopt_fn`，成功即就地返回可用账号 |
| `providers/traework/__init__.py` | facade 传入 `adopt_fn=adopt_credentials_from_client`（其余四家不传，语义不变） |
| `providers/traework/chat.py::_run_turn` + `_adopt_and_retry` | 次要点：`_turn` 鉴权失败时也试一次自救并重试本回合（限一次，防死循环） |
| `providers/traework/chat.py::test_chat` | **管理页「测试」入口**：直调账号、绕过 `_pick`，必须单独接一次自救（见 §7.4） |
| `gateway/server.py::_align_traework_credentials()` | 启动对齐（`require_newer=True`，只认 `expires_at` 更大） |


设计要点（安全与正确性）：

- **uid 必须一致**才允许接管，防止读到别的账号的凭据；
- 只在**旧凭据确实失效**时走这条路，不主动轮换（避免自己把票刷废）；
- 回写字段沿用 `_TOKEN_FIELDS` 白名单语义，不整行覆盖；
- 全程 best-effort：任何异常都只降级为「维持 expired」，不抛出打断请求链路；
- 记录一行 stderr 日志（如 `[traework-adopt] adopted refreshed credentials from client storage`），
  便于排查。

### 3.3 启动时同步一次（避免拿旧票去刷）

`server.py::main()` 在 `startup_scan()` 之后、调度 sync 之前，
对 traework 做一次 best-effort 的凭据对齐：若客户端 storage.json 的凭据比 DB 新（`expires_at` 更大）
且 uid 一致，则采用客户端版本。**失败静默**，不阻断启动。

> 理由：网关启动时若拿旧 refresh_token 去刷，会直接把客户端刚刷好的票作废——
> 这正是「重启后客户端掉线」的成因。

### 3.4 管理页可见性（最小）

账号行的「Token 有效期」已存在；补充一个可观测点即可（可选，非必须）：

- `/admin/channel-health` 或账号详情能看出「上次 self-heal 时间/结果」。

> 若成本高可延后——本次以 3.2 + 3.3 为主，3.4 视实现量决定。

## 4. 明确做不到的（写清边界，避免误导）

- 若用户**仍在官方客户端使用同一账号**，客户端刷新仍会作废网关的票 → 网关仍可能掉；
  但有 3.2/3.3 后**能自动恢复**，不再需要人工重导。
- 若 refresh_token 已被服务端彻底吊销（如异地风控、账号被封），任何自愈都无效，
  必须人工重新登录。这是上游策略，网关无法绕过。

## 5. 验证清单

1. 单测：`_turn` 遇到 401 → 触发 self-heal → uid 一致的更新凭据被采用并重试成功；
2. 单测：uid 不一致 / 文件不可读 / 凭据无更新 → 维持 expired，不误接管；
3. 单测：启动对齐只在「客户端更新」时接管，且失败不阻断启动；
4. 回归：`tests/test_traework.py`、`tests/test_perf_providers.py`、`tests/test_pick_contract.py` 全绿；
5. 真机（凭据有效时）：故意把 DB 里的 refresh_token 改坏 → 发一次请求 → 观察自动恢复。

## 6. 风险

- 自愈会读取客户端目录 → 必须严守 uid 一致 + 路径校验（沿用 `import_discovered` 的目录白名单）；
- 回写凭据涉及加密字段 → 复用现有 `db.update_account`，不自行拼 SQL；
- 不得引入对客户端的**写**操作（绝不回写 storage.json，避免干扰官方客户端）。

## 7. 实现与复核记录（2026-09-19）

实现经 workflow 委派 `buddy/hy3` 子代理完成；因独立验证子代理两次返回失败（空结果），
由主控**逐行复核并自行修复**，随后补测试。复核中修掉 4 个真实缺陷：

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 1 | 自愈后的重试只捕获 `TraeWorkAuthError` | 重试遇网络异常会穿透成 **500**（改动前是 503 降级）→ 行为回归 | 收敛为 `except Exception` → 503 |
| 2 | 启动对齐用「token 不同即接管」 | 会把网关刚刷好的新票换成客户端**更旧**的票 → 反而弄坏可用凭据 | 新增 `require_newer=True`：启动对齐只认 `expires_at` 更大 |
| 3 | `expires_at` 无条件写 `int(parsed.get(...) or 0)` | 客户端凭据缺该字段时，把 DB 有效值**砸成 0** → `is_token_expired` 判定错乱 | 仅非零才写入（对齐 `store_common` 既有约定） |
| 4 | uid 校验写成「两边都有值才比对」 | 客户端 uid 解析为空时**默认放行**，弱化身份防护 | 为空即拒绝；并新增「无任何 token 不接管」 |

新增/补充测试（`tests/test_traework.py`）覆盖：`require_newer` 收紧启动对齐但**不削弱自愈**、
`expires_at` 不被砸成 0、空 token 拒绝、空 uid 拒绝、自愈重试网络异常降级 503。

验证结果：

- `tests/test_traework.py` — 40 passed（补 7.2 的新用例后含 pick 级自救路径）；
- 组合回归（`test_traework` + `test_pick_contract` + `test_perf_providers`）— **75 passed**；
- 全量面（`test_traework` + `test_perf_providers` + `test_pick_contract` +
  `test_perf_metrics` + `test_channel_models` + `test_traesolo`）— **211 passed**。

### 7.2 复核中发现的**关键缺口**：自救最初挂在走不到的路径上

子代理按 spec §3.2 的字面描述，把自救挂在 `chat.py::_run_turn` 的
`except TraeWorkAuthError` 分支里。复核时发现这对**用户实际症状无效**：

```
token 过期 → _pick() 内 refresh 失败 → pick_with_refresh_fallback 返回 None
          → _run_turn 里 `if not account: break` 直接跳出
          → _turn 从未被调用 → 自救分支永不执行 → 一路 503
```

也就是说：**账号一旦 refresh 失效，请求会停在选号阶段，压根走不到 `_turn`**，
而「每次都要人工重导」正是停在这一步。原实现只有在「token 未过期但被上游拒绝」
这种较少见的情况下才生效。

修法：把自救接到**真正的故障路径** `providers/trae_shared.pick_with_refresh_fallback`：

- 新增可选参数 `adopt_fn`（async `(account) -> bool`），默认 `None`；
- refresh 因鉴权失败时先试 `adopt_fn`，成功则用新凭据**就地返回**，且不计入负缓存；
- 负缓存命中（`_recently_failed`）时也试一次自救 —— 客户端可能已重新登录，
  这正是旧实现「必须手动重导」的场景；
- traework facade 传入 `adopt_fn=adopt_credentials_from_client`；
- **其余四家不传** ⇒ 既有语义零变化（有专门的回归护栏用例钉住 `adopt_fn=None` 时行为不变）。

新增用例：`test_pick_self_heals_from_client_when_refresh_fails`（pick 阶段自救成功并返回可用账号）、
`test_pick_without_adopt_fn_keeps_legacy_semantics`（不传 adopt_fn 时维持旧语义）。

### 7.3 用户实测反馈补的缺口：管理页「测试」按钮也不自愈

**现象**（用户实测）：点了管理页账号行的「测试」，**没有任何 `[traework-adopt]` 日志**。

**定位**：`chat.py::test_chat` 的实现是**直接拿账号调 `_turn`**：

```python
text = await _turn(account, prompt, ...)   # 无选号、无刷新、无自救
```

它同时绕过了 §7.2 里挂自愈的两处（`pick_with_refresh_fallback` 与 `_run_turn`），
因此自愈在这条路径上**根本没接线**。而「测试」恰恰是用户判断
「这条通道还活着吗」的主要入口 —— 在这里不自愈，体验上等于"自愈没做"。

**修法**：抽出共用助手 `chat.py::_adopt_and_retry(account, prompt, model, *, timeout, on_thinking)`，
返回回答文本或 `None`；`_run_turn` 与 `test_chat` **共用这一份**（避免两处漂移）。

- `_run_turn`：失败分支改调 `_adopt_and_retry`，成功则记 200 日志并返回；
- `test_chat`：`TraeWorkAuthError` 分支改调它，成功返回 `200 + 回答`，
  失败仍如实返回 `503`（**不谎报成功**——无素材时就是要让用户看到失败）。

新增用例：`test_test_chat_self_heals_from_client`（测试入口自救成功 → 200 + `pong`）、
`test_test_chat_returns_503_when_no_credentials_to_heal`（无素材 → 如实 503 且不动凭据）。

> 边界提醒（同 §4）：**客户端自己登出时没有自救素材**。此时启动日志会打印
> `[traework-adopt] skip: client storage unreadable (TraeWork storage.json has no iCubeAuthInfo)`，
> 这是**正确行为**（不硬接管空凭据），但也意味着必须先在客户端重新登录，
> 网关才有素材可接管。

### 7.4 日志文案修正：把「未登录」和「读不了」分开

**起因**（用户反馈）：启动时看到

```
[traework-adopt] skip: client storage unreadable (TraeWork storage.json has no iCubeAuthInfo)
```

`unreadable` 会让用户以为是权限/路径问题去排查文件，**实际含义是「客户端自己处于登出态」**
（`storage.json` 缺主凭据键）——这是最常见的一种，而处置方式完全不同（去客户端登录）。

**修法**：新增纯函数 `token.py::_describe_client_storage_error(exc)`，按底层异常分类成
中文说明 + 下一步动作；各放弃分支的日志同步改写：

| 场景 | 新文案 |
|---|---|
| 客户端未登录 | `放弃自救：客户端当前未登录（storage.json 里没有 iCubeAuthInfo 凭据），请先在 TRAE SOLO CN 客户端登录` |
| uid 不一致 | `放弃自救：客户端凭据属于另一个账号（客户端 uid=…，本账号 uid=…），拒绝接管` |
| 无可用 token | `放弃自救：客户端凭据里没有可用的 token` |
| 路径越权 | `放弃自救：路径不在允许的扫描范围内，已拒绝读取（…）` |
| 解密失败 | `放弃自救：凭据可读到但解密失败（…），可能需要重新登录客户端` |
| 真读不了 | `放弃自救：无法读取客户端凭据文件（FileNotFoundError: …）` |
| 未记录 auth_path | `放弃：账号 <id> 未记录客户端 storage.json 路径，无法自救（可到管理页重新导入一次以记录路径）` |
| 接管成功 | `已从客户端 storage.json 接管更新后的凭据（账号 <id>），状态置回 active` |

设计要点：

- 「无需动作」的分支（凭据无更新）**仍然不打日志**，避免正常运行时刷屏；
- 文案**必须 GBK 可编码**（本机控制台是 GBK）——为此不用 emoji（此前一个 ✅ 曾导致
  `UnicodeEncodeError` 把脚本崩掉）；
- 未加 `auth_path` 时原先静默返回，现补一条说明（粘贴导入的账号缺路径属常见困惑）。

验证：三场景真机复现文案（未登录走纯函数、uid 不一致与未记录路径走真实调用），
并确认**未触碰真实 storage.json**（size/mtime/主凭据键均未变）。
回归：`test_traework` + `test_pick_contract` + `test_perf_metrics` — 92 passed。

### 7.5 复核中额外发现并修复的测试污染
初次跑组合回归时出现 **13 个与本改动无关的失败**（全在 qclaw/qwenwork 的 pick 契约用例，
报 `calls == []`），但**单独跑那些文件却全绿** —— 典型的用例间状态泄漏。

用 `git stash` 做了基线对照，确认是**本次新增测试引入的污染**，并用二分定位到具体用例：
`test_selfheal_retry_network_error_degrades_to_503` 调 `_run_turn` 时会写
`auth_manager._account_failures`（进程级失败冷却），而 `isolated_db` fixture 只清理
DB 与 credit_cache、**不清理鉴权全局态**。于是后续 `test_pick_contract` 里账号仍处冷却，
`_pick` 直接跳过 → 假失败。

修法（`tests/test_traework.py` 新增 autouse fixture `_clean_global_auth_state`）：
每例前后清理 `trae_shared` refresh 负缓存、`auth_manager._sticky_account_id`
与 `_account_failures`。修后组合回归 75 passed、全量 156 passed。

> 教训：本仓库有若干**进程级全局态**（失败冷却 / sticky / refresh 负缓存）不在
> `isolated_db` 覆盖范围内；任何会触发 `mark_account_failure` 的新用例都应自清，
> 否则会以"别的文件红"的形式暴露，且单跑不复现。

仍**未做**（spec 明确延后）：§3.4 管理页可见性、§3.1（经核查 traework 现状已是"接近真过期才刷"，无需改）。


