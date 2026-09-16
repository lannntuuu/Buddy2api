# 账号调度与故障转移口径

本文把「一个请求如何选中账号」「账号报错后发生什么」「手动锁定（pin）的确切语义」
三件事钉死。此前这些逻辑只存在于代码与注释里，没有任何文档；而 UI 上那个写着
「锁定」的下拉框很容易被误解成硬绑定，所以本文的首要目的是**纠正这个误解**。

代码位置索引：

| 机制 | 文件 |
|---|---|
| 选号优先级 / 冷却 / sticky | `src/accounts/auth_manager.py` |
| 手动锁定（pin） | `src/accounts/auth_manager.py`（`get_manual_pin` / `set_manual_pin` / `all_manual_pins`） |
| 请求级重试与换号 | `src/upstream/proxy.py`（workbuddy）、`src/providers/*/chat.py`（其余通道） |
| 可重试状态码与退避 | `src/providers/retry.py`、`src/upstream/proxy.py` |
| 预留配额前的空仓判定 | `src/gateway/router.py`（`ensure_usable`） |

---

## 1. 一句话结论

**「锁定账号」是优先偏好，不是硬绑定。** 被锁定的账号一旦不可用
（冷却中 / 被禁用 / token 过期 / 本轮已试过），pin 会**静默失效**并退回常规调度，
请求不会卡死、不会报错。冷却到期后 pin **自动重新生效**，无需人工干预。

---

## 2. 选号优先级（`pick_account`）

```
candidates = 该通道 active 账号
             − 本轮已试过（exclude_ids）
             − 正在冷却（account_is_cooling_down）

1) 手动锁定：pin 且 pin 的账号 ∈ candidates  → 直接命中
2) 最高 priority 的一批里，若有 sticky 账号   → 命中 sticky
3) 否则按调度排序键取最优；
   完全并列时按 weight 加权随机取一（避免永远固定选同一个）
```

排序键 `_route_sort_key`（越小越优）：

```
(-priority, -weight, total_requests / weight, total_requests)
```

即 **优先级 → 权重 → 单位权重请求数 → 累计请求数**。

**关键点**：第 1 步的 pin 是**在已经过滤过的 `candidates` 里查找**的。所以 pin
不生效时是「找不到 → 跳过」，而不是「选中后失败」。这决定了它的失效是静默的。

---

## 3. 账号报错后：冷却（`mark_account_failure`）

任一次成功（`mark_account_success`）会**清零**该账号的失败计数。

| 触发状态码 | 退避基数 | 冷却时长（按连续失败次数） |
|---|---|---|
| `401` / `403` / `429` | 30s | 30 → 60 → 120 → 240 → **300**（此后恒 300） |
| 其它（5xx / 网络错误等） | 5s | 5 → 10 → 20 → 40 → **80**（此后恒 80） |

公式：`cooldown = min(300, base × 2^(min(count−1, 4)))`——
`count` 是**该账号当前连续失败次数**，指数项封顶到 `2^4 = 16`，因此
`401/403/429` 序列是 30/60/120/240/480→**钳到 300**，其它是 5/10/20/40/80。上限恒为 300 秒。

**副作用（容易踩坑，务必注意）**：状态码为 **`401` 或 `403`** 时，除了冷却，
还会把账号行 `status` **直接改写成 `expired`**：

```python
if status_code in {401, 403}:
    db.update_account(aid, {"status": "expired"})
```

这意味着账号会从 `get_active_accounts` 的结果里消失，**不再只是临时冷却**。
界面上会看到该账号状态变成 `expired`。要恢复需重新导入/刷新该账号凭据，
或手工把状态改回 `active`。

冷却状态只存在于**进程内存**（`_account_failures`），**重启网关即全部清空**。
`expired` 则写进数据库，重启后依然存在。

---

## 4. 请求级重试与换号

### 4.1 可重试状态码

```python
# src/upstream/proxy.py
RETRYABLE_STATUS_CODES = RETRYABLE_STATUS | {401, 403}
# RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}   (src/providers/retry.py)
```

`401` / `403` **只参与账号 failover 判定，不参与同账号重试**。

**不可重试的状态码（如 `400` / `404` / `422`）不会换号**，直接把错误返回给客户端。
这是刻意的：换号重试也救不了请求本身的错误（模型名不对、参数非法等）。

### 4.2 尝试次数

最多 **3 次**（首次 + 2 次重试），每次通过 `tried_ids` 排除本请求内已试过的账号，
因此**每次重试都换一个新账号**。

退避 `retry_delay`（`src/providers/retry.py`）：

- 无 `Retry-After`：`base = min(2.0, 0.25 × 2^attempt)`，
  实际 sleep = `base × (0.5 + random())`（equal-jitter，∈ `[0.5×base, 1.5×base]`）；
- 有 `Retry-After`（纯数字秒）：`sleep = min(retry_after, 2.0)`；
- 最后一次尝试之后不再空等。

### 4.3 流式的关键差异：出字后不换号

**一旦已经向客户端吐出第一个内容帧（`output_started == True`），就不再换号重试。**
因为已发出的增量无法撤回，换号会导致客户端收到两个账号拼接的内容。此时只记录
错误日志并按现状收尾。

只有**出流前**的失败（HTTP 状态码非 200、eof、网络异常）才会换号重试。

### 4.4 全通道无可用账号

- **预留配额之前**（`router.ensure_usable` dry-pick）发现无可用账号 →
  HTTP **503** `channel_unavailable`，**不消耗** API Key 日限额。
- **预留配额之后**重试耗尽 → HTTP **503**，**消耗**日限额（与 503 空仓历史行为一致）。

两类 503 的配额规则不同，不要混为一谈。

---

## 5. 各通道的实现差异（不要假设完全一致）

pin 与冷却对所有通道**同一套**（都走 `auth_manager.pick_account`），但
**重试与过期兜底**各通道不同：

| 通道 | 每次尝试的取号方式 | 可换号的状态码集合 |
|---|---|---|
| `workbuddy` | `pick_account_with_fallback`（含 expired 账号就地刷新） | `RETRYABLE_STATUS ∪ {401,403}` |
| `qclaw` | `pick_account`（**无** expired 刷新兜底） | `RETRYABLE_STATUS`（**不含** 401/403） |
| `qwenwork` | `pick_with_refresh_fallback` | `RETRYABLE_STATUS`（**不含** 401/403） |
| `traework` / `traesolo` | 各自的会话/冷却状态机 | 见各自 `chat.py` |
| 自定义 OpenAI 兼容通道 | `pick_account_with_fallback` | 见 `openai_compat.py` |

也就是说：**「401 会换号」主要针对 workbuddy**；qclaw / qwenwork 收到 401 不会
把它当作可换号信号（它们会冷却该账号，但当前请求未必重试）。

---

## 6. 手动锁定（pin）的存储与运维含义

- pin 存在 **`src/gateway_settings.json`** 的 `manual_account_pin` 键：

  ```json
  { "manual_account_pin": { "workbuddy": 12, "traework": 2 } }
  ```

- **不在 SQLite 里**。这一点很重要：
  - `data/*.db` 的备份**不包含** pin；
  - 换机器/恢复数据库后，**pin 会丢失**（回到自动调度）；
  - 用 `DB_PATH` 重定向做测试隔离时，**pin 仍会写到真实的 `gateway_settings.json`**——
    除非同时调用 `gateway_settings.set_settings_path()` 重定向。
    （`_SETTINGS_PATH` 是从模块位置推导的进程级全局，不受 `DB_PATH` 影响。）
- 读写接口：`GET /admin/accounts/pin`、`POST /admin/accounts/pin`
  （body `{"provider": ..., "account_id": <int|null|"auto">}`；`null`/`"auto"` 取消锁定）。
- `POST` 会校验：通道必须已启用、账号必须存在、**账号必须属于该通道**，
  否则 400/404。
- **删除通道或账号都不会清理 pin 条目**（已实测）：
  - `custom_channels._purge_channel_settings()` 清理的是 DB `settings` 表的
    `<cid>.models` / `.aliases` / `.credit_rate` / `.reasoning` / `.max_input_tokens*`
    等键，**不碰 `gateway_settings.json` 的 pin**；
  - `all_manual_pins()` 只过滤「非 int」的值，**不会清理指向已消失账号 id 的条目**。
  - 后果：删除后若**重建同名通道**，pin 会**静默继承**；若 pin 指向的账号已不存在，
    则 `pick_account` 找不到该 id，pin 静默失效（行为上是安全的，但配置面上是残留）。

---

## 7. 实测验证记录

以下行为在隔离环境（`set_settings_path` 重定向到副本 + 独立临时 DB）中逐条实测：

| 场景 | 结果 |
|---|---|
| 无 pin，A(priority 5) 与 B(priority 1) | 选 A |
| pin B | 选 B（**pin 压过 priority**） |
| traework 独立 pin | 该通道按自己的 pin 选（pin 是**每通道**独立的） |
| pin B，B 吃 500 | 改选 A（pin 记录仍在，只是暂时不生效） |
| 之后 B 成功一次 | 再选即回到 B（**pin 自动重新生效，无需人工干预**） |
| pin B，B 吃 401 | 改选 A，且 **B 的 `status` 变成 `expired`** |

**验证方法提示**：测试写路径时必须同时重定向 `gateway_settings`，否则会写到
真实配置。这正是本文第 6 节那条注意事项的由来。
