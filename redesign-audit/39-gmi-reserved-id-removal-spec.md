# 39 号 spec：gmi 删除后仍显示在通道列表（保留 id 残留）

## 0. 事故

用户删除密钥型通道 `apikey-gmi`（即 `gmi`）后，**列表里仍然显示它**。

## 1. 根因

`gmi` 被硬编码在内置保留 id 里，删不掉：

```python
# src/providers/protocol.py L8-L18
ChannelId = Literal["workbuddy","qclaw","qwenwork","qoderwork","traework","traesolo","gmi"]
KNOWN_CHANNEL_IDS = ("workbuddy","qclaw","qwenwork","qoderwork","traework","traesolo","gmi")
```

而 `known_channel_ids()` 是「内置 ∪ 自定义」：

```python
# src/providers/__init__.py L69-L81
def known_channel_ids():
    seen = list(KNOWN_CHANNEL_IDS)          # ← gmi 从这里回来
    for cid in _custom_definition_ids():    # ← 自定义定义（已不含 gmi）
        if cid not in seen: seen.append(cid)
    return tuple(seen)
```

`DELETE /admin/channels/custom/{cid}` 的后端逻辑是**正确且完整的**：
- `custom_channels.delete_definition()` 从 `custom_channels` settings 键移除
- `_purge_channel_settings()` 清理 `<cid>.models/aliases/credit_rate/reasoning`、
  unified_models 映射、enabled_channels / channel_order 成员
- 账号行置 inactive（保留日志）

实测删除后 DB 确实已清理（`custom_channels` 只剩 `bailian`），但：

```
known_channel_ids() = (..., 'gmi', 'bailian')
  -> gmi still present after delete? True
enabled_provider_ids() = ['workbuddy','traework','traesolo','bailian']   # gmi 不在
get_provider("gmi") = None                                                # 已无 provider
```

即 `gmi` 从「自定义定义」里删掉了，却**又从硬编码内置表里冒出来**，
于是 `/admin/channels` 仍返回一行 `gmi`（`enabled:false, loaded:false`）
—— 前端把它渲染成一条删不掉的僵尸行。

### 1.1 历史成因

commit `e43ad20`「允许删除 seed 通道；bailian 移出保留 id 名单」把
`bailian` 从 `ChannelId` / `KNOWN_CHANNEL_IDS` 移除，使其可作为纯数据驱动
自定义通道被删除/重建，并在 commit message 里写明 **「gmi 保留」**。
但 `gmi` 与 `bailian` 一样是 seed 数据通道（`seed_initial_definitions()`
两者都种），同样需要可删除。这是一次**只做了一半的迁移**。

对照证据：`tests/test_provider_schema.py` L128 已有
`assert "bailian" not in providers.KNOWN_CHANNEL_IDS`，
但没有 `gmi` 的对应断言 —— 所以 `gmi` 的残留一直没被拦住。

## 2. 修复方案（与 e43ad20 对 bailian 的做法保持一致）

### 2.1 主修复

`src/providers/protocol.py`：从 `ChannelId` 与 `KNOWN_CHANNEL_IDS` 中移除 `gmi`。

移除后 `gmi` 与 `bailian` 完全同构：仅由 `custom_channels` 定义驱动，
可删除、可重建，`seed_initial_definitions()` 在键存在时不重建。

### 2.2 必须同步核查的点（逐条验证，不可想当然）

1. **`ChannelId` 是 Literal 类型**：移除 `gmi` 后，任何把 `"gmi"` 当
   `ChannelId` 用的静态类型标注/代码路径要能走通。grep 全仓 `"gmi"`
   确认没有依赖其 Literal 成员身份的逻辑（只依赖字符串值是可以的）。
2. **`reserved_ids()`**（custom_channels.py L279）从 `KNOWN_CHANNEL_IDS`
   取内置 id：移除后 `gmi` 不再"保留"，因此**可以新建同名自定义通道**——
   这正是期望行为（与 bailian 一致）。
3. **`_channels.py` L44**：`custom_ids = reserved_ids() - KNOWN_CHANNEL_IDS`。
   移除 `gmi` 后，`gmi` 会落入 `custom_ids` → `kind='apikey'`、`custom=True`。
   需确认这对 `gmi` 是正确的（gmi 本来就应是 apikey 类，与删除前一致）。
4. **`_LOADED` 字典**（providers/__init__.py L31）没有 `gmi` 条目 —— 无影响。
5. **已有测试**：`tests/test_provider_schema.py` L25
   `assert set(got) == set(providers.KNOWN_CHANNEL_IDS)` 等可能受影响的断言
   需要复核；若某断言把 `gmi` 当作内置，按 bailian 的先例改为
   「非保留 id + 定义存在即可达」。

### 2.3 数据迁移注意

若某些部署的 `enabled_channels` / `channel_order` 里存了 `gmi` 且
`gmi` 定义已删除，`_read_db_list()` 会用 `_known_set()` 过滤掉未知 id
（`cleaned = [c for c in cleaned if c in valid]`），因此不会出现幽灵项。
但本次删除 gmi 定义后 `known_channel_ids()` 不再含 gmi → 旧 DB 里的
`gmi` 会被自然过滤。需验证这一路径。

## 3. 验收门禁

1. `pytest tests/ -q` 全绿，且失败集不扩大（与基线一致）。
   基线获取方式：修复前先跑一次完整测试并记录 pass/fail 数。
2. 新增/补强断言：在 `tests/test_provider_schema.py` 增加
   `assert "gmi" not in providers.KNOWN_CHANNEL_IDS`（与 bailian 对称），
   并加一条**回归断言**：删除 gmi 定义后 `known_channel_ids()` 不含 gmi
   （这是本次事故的直接护栏，防止将来再把 seed id 硬编码回内置表）。
3. **变异验证**：把 `gmi` 加回 `KNOWN_CHANNEL_IDS`，新断言必须 FAIL。
4. 端到端手验：删除 gmi → `/admin/channels` 不再返回 gmi 行；
   重新创建 id 为 `gmi` 的自定义通道 → 成功且可用（证明可重建）。

## 4. 风险

- 移除后若某部署仍有 gmi 账号行（status=inactive）且未重建定义，
  这些账号行保留但不可达 —— 与 bailian 行为一致，可接受（D6 保留日志）。
- 若 `seed_initial_definitions()` 曾写过 gmi、用户删了定义但**又重启**，
  键已存在（空列表或只含 bailian）→ 不重建。与 commit message 一致。

## 5. 交付物

- `src/providers/protocol.py`：移除 `gmi`。
- `tests/test_provider_schema.py`：新增 gmi 非保留断言 + 删除后不复活回归断言。
- 变异验证记录 + 修复前后测试基线对比。
