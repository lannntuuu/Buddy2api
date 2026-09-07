# Spec — 限额存储重构：全局通用 JSON + 通道/模型级进数据库

> 状态：待执行（workflow 委派 hy3 subagent 实施）
> 分支：`fix/hy4-preview-usage`，基于 commit `f393c6a`
> 性质：**纯存储层重构**——admin API 契约、前端 UI、优先级语义全部保持不变。

## 0. 需求方决策（不得偏离）

1. **全局（不分平台、不分模型）的可配置项** → 单独一个 JSON 文件统一管理，
   且该文件是**通用容器**（未来任何全局配置都可放），不是只管上下文限额。
2. **通道级、模型级**的上下文设置 → 改走**数据库**（settings 表），
   与现有 `<channel>.models/aliases/credit_rate/reasoning` 同模式管理。
3. 通道/模型级继续支持 `null` = 显式不限制（重置为无配置）、
   1M 内置默认、alias 到未知模型 id 也可预配置——这些已实现的行为不变。

## 1. 现状（commit f393c6a，待重构点）

- 三级配置全存仓库根 `model_limits.json`：`src\providers\model_limits.py`
  （`_load_raw`/`_save_raw`/`get_channel_limits`/`_set_model_limits` 由
  `control_plane._set_model_limits` 调用）。
- 优先级：模型级 → 通道级 → 全局 → 内置默认 1M；`enforce` 开关在 JSON 顶层。

## 2. 目标架构

### 2.1 全局通用 JSON：`gateway_settings.json`（仓库根，与 config.toml 同级）

```json
{
  "max_input_tokens": 1048576,
  "max_output_tokens": 32768,
  "enforce": true
}
```

- **通用容器**：以上是首批键；结构 = 任意扁平对象，未知键原样保留
  （读不剥离、写不覆盖其他键），供未来全局配置直接落此文件。
- 内置默认（文件缺该键时）：`max_input_tokens=1048576`、
  `max_output_tokens=32768`、`enforce=true`。
- 文件缺失/非法 → warn 一次 + 走内置默认，不 crash；管理端可写回修复。
- mtime+size 缓存沿用；原子写沿用。

### 2.2 通道级 / 模型级 → settings 表（DB）

- 通道级默认：settings 键 **`<channel>.max_input_tokens`**（值：正 int 或无）。
- 模型级：settings 键 **`<channel>.max_input_tokens_by_model`**
  （值：`{"<model_id>": <int|null>}`；`null` 条目 = 该模型显式不限制；
  `{}` = 自定义空）。
- 读写用 `db.get_setting`/`db.set_setting`/`db.delete_setting`，
  与 `<channel>.reasoning` 完全同模式；写入前校验（正 int / null / dict）。
- **键存在与否语义**照搬现有约定：未设置 = 用内置默认；设置了（哪怕 null）
  = 显式值。

### 2.3 重构后的优先级（解析语义与现在逐字节一致）

```
模型级 <channel>.max_input_tokens_by_model[id]
→ 通道级 <channel>.max_input_tokens
→ 全局 gateway_settings.json "max_input_tokens"
→ 内置默认 1048576
```

`enforce`、`max_output_tokens`（qwenwork 默认输出等）只属于全局 JSON，
**不再有通道/模型级覆盖**（上一版也未实现通/模级 override，语义等价）。

### 2.4 模块拆分

- `src\providers\model_limits.py` **保留**（调用方 `proxy.py` 的
  `resolve_max_input_tokens`/`estimate_input_tokens`/`get_enforce`/
  `get_default_max_output_tokens`/`get_channel_limits` 契约不变），
  内部改造：全局段读 `gateway_settings.json`（新通用模块）；
  通道/模型段读 DB settings。
- 新建 **`src\storage\gateway_settings.py`**：通用全局 JSON 容器
  （`get(key, default)` / `set(key, value)` / `all()`；mtime+size 缓存、
  原子写、坏文件容错、未知键保留）。
- `control_plane._set_model_limits` 改为写 DB settings（删除 JSON 里的
  channels 段逻辑）；`model_limits.example.json` 改名为
  **`gateway_settings.example.json`** 并更新内容；`.gitignore` 把
  `model_limits.json` 换成 `gateway_settings.json`。
- 兼容迁移：启动时/首次读取时，若旧 `model_limits.json` 存在，把其中
  `default_max_input_tokens`、`channels`（通道级/模型级）一次性迁入
  DB settings，全局键并入 `gateway_settings.json`，随后把旧文件改名为
  `model_limits.json.migrated`（不删除，留痕）。

## 3. 保持不变（验收对照）

- admin API 请求/响应形状：`GET` 仍返回 `model_limits`、
  `default_max_input_tokens`、`model_limits_customized`；
  `PUT` body 字段不变（`set_` 标志位模式不变）。
- 前端 `models.js` 零改动。
- `proxy.py` 调用面零改动；超限 400 / 注入 / clamp 行为不变。
- pytest 全绿（存量 2-3 个沙箱 tmp_path error 除外）。

## 4. 约束

- 不重启 8787 运行实例；e2e 用 8789 + db 副本临时实例，用完清理。
- settings 表只加键不改 schema；`data/codebuddy_gateway.db` 主库在测试中
  一律只读或用副本。
- 全部改动落在当前分支，commit message 一行中文。

## 5. 执行步骤（workflow 三阶段，agent 全部 provider=buddy model=hy3）

### Phase 1 — 存储层
1. 新建 `src/storage/gateway_settings.py`（通用 JSON 容器 + 单测：
   get/set/all、未知键保留、坏文件容错、原子写、缓存失效）。
2. 改造 `model_limits.py`：全局段接 gateway_settings；通道/模型段接
   `db.get_setting`；`_load_raw` 的 channels 段逻辑删除；迁移逻辑（§2.4）。
3. `control_plane._set_model_limits` 改写 DB；example/gitignore 更新。
4. 更新 `tests/test_model_limits.py`（存储位置断言改为 DB settings +
   gateway_settings.json；新增迁移用例）。

### Phase 2 — 回归与契约
5. 全量 pytest（test_core + test_model_limits + 相关）；API 形状回归测试
   确认 §3 逐项不变。
6. 笔记 `redesign-audit/19c-storage-refactor-notes.md`。

### Phase 3 — e2e + 汇总
7. 8789 + db 副本 e2e：a) PUT 通道/模型级 → 验证落 DB settings 且
   JSON 文件无 channels 段；b) 超限 400 / 放行 / 注入三连仍符合预期；
   c) 迁移用例：构造旧 model_limits.json → 验证迁入 DB 且旧文件改名；
   d) UI/契约不变确认。
8. 报告 `redesign-audit/19d-storage-refactor-report.md`；git 提交。

## 6. 验收标准

- [ ] `gateway_settings.json` 为通用容器；通道/模型级配置在 DB settings
      （`<channel>.max_input_tokens[_by_model]`），JSON 文件中无 channels 段。
- [ ] 优先级语义、admin API 契约、前端、proxy 行为全部不变。
- [ ] 旧 `model_limits.json` 自动迁移并留痕改名。
- [ ] pytest 全绿（存量环境 error 除外）；e2e 四项通过。
