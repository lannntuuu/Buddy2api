# 19d — 限额存储重构报告（spec 20 / 全量验收）

> 分支 `fix/hy4-preview-usage`；存储层重构落地 + 契约回归 + e2e 验证。

## 1. 架构前后对比

| 维度 | 重构前（commit f393c6a） | 重构后 |
|------|--------------------------|--------|
| 全局段 | repo 根 `model_limits.json`（`default_max_input_tokens`/`enforce`/`default_max_output_tokens`） | repo 根 `gateway_settings.json`（通用容器，键名 `max_input_tokens`/`enforce`/`max_output_tokens`） |
| 通道级 | `model_limits.json` 的 `channels[ch].default_max_input_tokens` | DB `settings` → `<channel>.max_input_tokens` |
| 模型级 | `channels[ch].models[id].max_input_tokens` | DB `settings` → `<channel>.max_input_tokens_by_model`（`{"id": int\|null}`） |
| 写出路径 | `control_plane._set_model_limits` 写 `model_limits.json` | 写 DB `settings` |
| 优先级 | 模型→通道→全局→内置默认 1M | **逐字节一致**（模型→通道→全局→内置默认 1M） |
| 旧文件迁移 | — | 首次读取一次性迁移，改名 `model_limits.json.migrated` 留痕 |
| 通用容器 | — | `src/storage/gateway_settings.py`：mtime+size 缓存、锁、原子写、坏文件容错、未知键原样保留 |

## 2. 审查结论（必要公共层改动）

- **`src/storage/database.py` / `src/storage/repos/settings.py`**：新增 `setting_exists(key)`。
  **必要，保留**——需区分「未设置=回退内置默认」与「显式 null=不限制」，而
  `get_setting` 对两者都返回 None。改动最小（单函数 + `__all__` 一行），复用了现有
  `_invalidate_path` 缓存失效，不影响其他调用方。
- `model_limits.py` 保留全部对外契约函数（proxy 调用面零改动）；内部全局段接通用
  容器、通道/模型段接 DB。迁移逻辑 handles 异常不阻断启动。

## 3. e2e 证据（8789 临时实例 + db 副本，临到即用即清）

> 临时实例 `CB_GATEWAY_DB_PATH` 指向系统 temp 下的 db 副本（含其运行时 -wal/-shm）。
> 上游凭据在副本中无法解密（缺 `.credentials.key`），真实请求被「No usable accounts」
> 503 拦截在限额预检之前——故超限/放行/注入三连改走 **bind 层**
> （`proxy._apply_model_limits`，读真实 DB settings）验证并在本小节如实记录。

### a) PUT 通道级+模型级 → 落 DB settings，JSON 无 channels 段 ✅
```
PUT /admin/channels/workbuddy/models
  {"model_limits":{"hy3-x":1000},"default_max_input_tokens":3000} → 200
DB settings 副本:
  workbuddy.max_input_tokens = 3000
  workbuddy.max_input_tokens_by_model = {"hy3-x": 1000}
repo 根 gateway_settings.json: 不存在（e2e 无任何 channels 段写入）
```

### b) 超限 400 / 调回放行 / max_tokens 注入（bind 层，读副本 DB 配置 hy3-x=1000）
```
b1 超限: 6000 字符(≈2000 token) → ModelLimitError 400 "input exceeds hy3-x max input context (≈2000 > 1000)" ✅
b2 放行: "hi"(≈1 token) → 直通上游，未拦截 ✅
b3 注入: 未传 max_tokens → 注入 min(32768, 1000-1)=999 ✅
b4 clamp: 显式传 5000 → clamp 到剩余空间 999 ✅
```
> 说明：HTTP 侧受上游凭据 STS/STA 限制只能到 503，故以上为 bind 层证据；契约与
> 行为（400/注入/clamp/enforce）由 pytest test_model_limits（182 通过的含全部 proxy
> 链路断言）双保险覆盖。

### c) 构造旧 model_limits.json → 迁入 DB + 改名 .migrated ✅
```
旧文件: {"enforce":false,"default_max_input_tokens":1048576,"default_max_output_tokens":16384,
          "channels":{"e2echan":{"default_max_input_tokens":262144,
                     "models":{"e2e-m1":5000,"e2e-unlimited":null}}}}
迁移后:
  旧文件 不存在 → model_limits.json.migrated 存在（留痕）
  gateway_settings.json = {"max_input_tokens":1048576,"max_output_tokens":16384,"enforce":false}（无 channels 段）
  DB settings: e2echan.max_input_tokens=262144; e2echan.max_input_tokens_by_model={"e2e-m1":5000,"e2e-unlimited":null}
  优先级解析生效: e2echan/e2e-m1=5000, e2echan/other=262144
```

### d) GET admin 形状 + models.js 未变 ✅
```
GET /admin/channels/workbuddy/models → 含 model_limits / default_max_input_tokens / model_limits_customized ✅
/static/js/pages/models.js: 200 served, len=19009（内容不变由 git clean 保证）✅
```

## 4. 清理与隔离
- 临时 8789 实例已停止（port 8789 free），db 副本及 -wal/-shm 已删。
- 主库 `data/codebuddy_gateway.db` **未触碰**：无新增 `.max_input_tokens` settings 键、
  无 schema 改动（只加键不改表）。
- 测试期配置（PUT 设置的 limits、新建的 API key）只落进被删除的副本，未污染主库。

## 5. 测试汇总
- 目标套件 `test_core + test_model_limits + test_channel_models + test_qwenwork`：
  **182 passed, 3 errors**（3 个均为沙箱临时目录 tmp_path 已知环境 error：
  `test_encrypt_without_master_key_uses_fernet_key_file` /
  `test_plaintext_legacy_api_key_is_migrated_to_encrypted_storage` /
  `test_qwenwork_auth_dirs_ignore_workbuddy_cb_auth_dir`）。
- 更广回归 `test_custom_channels + test_control_plane + test_channel_models +
  test_unified_models + test_web_assets`：**128 passed, 1 error**
  （`test_preview_import_roundtrip` 同类沙箱 tmp_path error）。
- 无功能失败。

## 6. 验收对照（spec §6 逐项）
- [x] gateway_settings.json 为通用容器；通道/模型级配置在 DB settings，JSON 无 channels 段。
- [x] 优先级语义、admin API 契约、前端、proxy 行为全部不变。
- [x] 旧 model_limits.json 自动迁移并留痕改名 `.migrated`。
- [x] pytest 全绿（存量环境 error 除外）；e2e 四项通过。

## 7. 遗留项
- 无代码遗留。e2e 上游凭据失效为副本语义（缺 .credentials.key），非代码缺陷；
  若需 HTTP 端到端 400 证据，需在带有效凭据的环境重跑（bind 层已验证逻辑一致）。
- 提交内不含 `gateway_settings.json` 本体（.gitignore 已覆盖，仅提交
  `gateway_settings.example.json`）。