# 19c — 限额存储重构实施笔记（spec 20 / Phase 1+2）

> 分支 `fix/hy4-preview-usage`；性质=纯存储层重构，契约/优先级语义保持不变。
> Phase 1 存储层 + Phase 2 契约回归。

## 1. 落点（最终架构）

| 层 | 存储 | 键 |
|----|------|----|
| 全局 | 仓库根 `gateway_settings.json`（新通用容器） | `max_input_tokens` / `max_output_tokens` / `enforce` |
| 通道级 | DB `settings` 表 | `<channel>.max_input_tokens` |
| 模型级 | DB `settings` 表 | `<channel>.max_input_tokens_by_model` |

优先级（与旧版逐字节一致）：模型级 → 通道级 → 全局 → 内置默认 `1048576`。
`enforce`、`max_output_tokens` 只属全局；null = 显式不限制（通道/模型级）；
全局不支持 null 语义（缺键回退内置默认）。

## 2. 模块拆分

- 新增 `src/storage/gateway_settings.py`：通用扁平 JSON 容器。mtime+size 缓存、
  线程安全锁、原子写(.tmp→os.replace)、坏文件/缺失 warn 一次走默认、未知键
  原样保留。提供 `get/set/delete/all/write_all` + 内置默认常量。
- 改造 `src/providers/model_limits.py`：对外五个函数契约不变
  （`resolve_max_input_tokens` / `estimate_input_tokens` / `get_enforce` /
  `get_default_max_output_tokens` / `get_channel_limits`），内部全局段读
  通用容器、通道/模型段读 DB settings。新增旧文件一次性迁移逻辑。
- `src/accounts/control_plane.py`：`_set_model_limits` 改为写 DB settings，
  删除 JSON channels 段写回逻辑。
- `src/gateway/routers/admin/_channels.py`：仅注释更新（契约未动）。
- `.gitignore`：`model_limits.json` → `gateway_settings.json`。
- `model_limits.example.json` 删除 → 新建 `gateway_settings.example.json`(83B)。

## 3. 审查结论

### 3.1 database.py / repos/settings.py（公共存储层）——**改动必要，保留**

新增 `setting_exists(key)`：需区分「未设置=回退内置默认」与「显式 null=不限制」，
而 `get_setting` 对两者都返回 None。这是通道级 null 语义成立的必要条件。
改动最小（单个函数 + `__all__` 导出一条），不影响既有调用方；reset/写路径
缓存失效逻辑复用现有 `_invalidate_path`。**结论：必要，不还原。**

### 3.2 model_limits.py 优先级与迁移逻辑

- 优先级解析：模型级用 `dict` 存在判断、通道级用 `setting_exists` 区分显式 null，
  全局用 `gs.get` + 内置默认兜底。与 spec §2.3 一致。
- 迁移（`_maybe_migrate_legacy`）：进程内单次哨兵；全局键映射
  `default_max_input_tokens→max_input_tokens`、`default_max_output_tokens→
  max_output_tokens`、`enforce→enforce`；通道/模型级迁入 DB；
  旧文件改名 `model_limits.json.migrated` 留痕（不删除）；任何异常不阻断启动。
- **审查修复**：
  1. 迁移时显式 `null` 通道级默认原来被丢弃（丢「显式不限制」语义），
     已补 `elif "default_max_input_tokens" in chan: set_setting(..., None)`。
  2. `_maybe_migrate_legacy` 原来每个请求都取锁，已加快路径
     （`if _migrated: return`，迁移后哨兵恒 True，热路径无锁）。
  3. `get_channel_limits` 的 `model_limits_customized` 原来用 `chan_val is not
     None`，漏了「显式 null 通道级=不限制」仍应算 customized；
     已改用 `setting_exists`。

## 4. 测试

- `tests/test_model_limits.py` 重写为 DB settings + gateway_settings.json 断言，
  并新增：
  - `test_legacy_migration_to_db_and_gateway_settings`：迁移全链（全局键并入 JSON、
    通道/模型级入 DB、显式 null 落 DB、旧文件改名 `.migrated`、视图反映迁移）。
  - `test_gateway_settings_*`：通用容器单测（get/set/all、未知键保留、坏文件容错、
    原子写）。
  - 存储位置断言：API 往返后 gateway_settings.json 无 `channels` 段、
    无通道配置键；通道/模型级只在 DB `settings`。
- 目标套件：`tests/test_core.py test_model_limits.py test_channel_models.py
  test_qwenwork.py` → **182 passed, 3 errors**（3 errors 均为沙箱 tmp_path 已知
  环境 error：`test_encrypt_without_master_key_uses_fernet_key_file` /
  `test_plaintext_legacy_api_key_is_migrated_to_encrypted_storage` /
  `test_qwenwork_auth_dirs_ignore_workbuddy_cb_auth_dir`）。
- 更广回归（custom_channels/control_plane/channel_models/unified_models/web_assets）：
  **128 passed, 1 error**（`test_preview_import_roundtrip` 同为 tmp_path 沙箱 error）。

## 5. Phase 2 契约回归验证

| 验收项 | 结果 |
|--------|------|
| GET `model_limits` / `default_max_input_tokens` / `model_limits_customized` 形状 | 不变 |
| PUT body 字段 + `set_` 标志位模式 | 不变 |
| 前端 `models.js` | 零改动（git 确认 clean） |
| `proxy.py` 调用面 | 零改动（git 确认 clean） |
| 超限 400 / max_tokens 注入 / clamp / enforce=false | 由 test_model_limits 覆盖，全绿 |
| 优先级语义 | 逐字节一致（测试覆盖） |

## 6. 遗留项

- 无代码遗留。主库 `data/codebuddy_gateway.db` 未触碰（schema 未改，仅加键）。
- e2e（8789 临时实例 + db 副本）见 19d 报告。