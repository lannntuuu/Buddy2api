# Spec — 模型最大输入上下文配置 + 通用 max_tokens 配置（JSON 文件）

> 状态：待执行（workflow 委派 hy3 subagent 实施）
> 分支：`fix/hy4-preview-usage`
> 前置事实：hy4-preview 实测报告（redesign-audit/18-*）确认推理模型会吃满
> max_tokens；qwenwork 存在硬编码 `max_tokens = 32000`；`model_details`
> 已有 `context_window: None` 展示位但无配置与强制。

## 0. 用户决策（需求方原话推导，实施不得偏离）

1. `hy3-preview-agent` 的真实 id 就是 `hy3-x` —— 修正过时的模型 id。
2. 【模型配置】-【各平台设置】**每个模型**增加「最大输入上下文」可配置字段；
   输出上下文暂不做每模型配置。
3. 代码里写死的 `max_tokens` 不应写死（未来会有更大上下文的模型），
   上限思想：**默认至少支持 1M（1048576）上下文**。
4. 通用可配置项**存 JSON 文件，不走数据库**（用户明确偏好）。

## 1. 现状确认（主会话已核实）

- 各平台设置链路：`src\gateway\routers\admin\_channels.py`
  （`PUT /admin/channels/{ch}/models`）→ `src\accounts\control_plane.py`
  `set_channel_models`/`channel_model_view` → `src\providers\model_config.py`
  （settings 键 `<channel>.models/aliases/credit_rate/reasoning`）。
- 前端：`src\web\js\pages\models.js` 各平台设置面板，modelRows 表格已有
  id/展示名/倍率/思考档位四类列；保存走整体 PUT。
- `model_details[]` 每模型已带 `context_window` 字段（traesolo 从自身
  cfg 的 `context_window_tokens` 填充），其余通道恒 None。
- 硬编码点：`src\providers\qwenwork\chat.py` L119-120
  `if "max_tokens" not in parameters: parameters["max_tokens"] = 32000`。
- workbuddy 链路（`upstream/proxy.py`）不注入 max_tokens、不做输入长度预检。
- 仓库根已有 `config.toml`（TOML），无 JSON 配置文件先例。

## 2. 方案设计

### 2.1 JSON 配置文件：`model_limits.json`（仓库根，与 config.toml 同级）

```json
{
  "default_max_input_tokens": 1048576,
  "default_max_output_tokens": 32768,
  "channels": {
    "workbuddy": {
      "default_max_input_tokens": null,
      "models": {
        "hy4-preview": { "max_input_tokens": 262144 }
      }
    }
  }
}
```

- 解析优先级（每模型最大输入上下文）：
  `channels[ch].models[id].max_input_tokens`
  → `channels[ch].default_max_input_tokens`
  → 全局 `default_max_input_tokens`（内置默认 1048576）
  → 未配置文件时同内置默认。
- `null` 值 = 显式不限制（跳过预检）。
- 文件不存在 / JSON 非法：warn 一次 + 全部走内置默认，**不得 crash**。
- 文件损坏可被管理端重写（见 2.3）。
- 新模块 `src\providers\model_limits.py`：负责加载/缓存（mtime 感知，
  文件改动即重读）、查询 API、写回 API。线程安全（管理端在线程池跑）。

### 2.2 修正过时模型 id

- `src\upstream\aliases.py` `DEFAULT_MODELS`：`hy3-preview-agent` →
  `hy3-x`（显示名 `HY3-X`）。
- 全仓 grep `hy3-preview-agent` 其余引用（tests/文档）一并修正；
  如担心老客户端兼容，在 `_BUILTIN_ALIASES` 加
  `"hy3-preview-agent": "hy3-x"`（保底映射，代价一行）。

### 2.3 后端：admin API 扩展

- `GET /admin/channels/{ch}/models` 响应新增：
  - `model_limits`: `{ "<model_id>": <int|null>, ... }`（仅显式配置项）；
  - `default_max_input_tokens`: 该通道生效默认值（含优先级解析结果）；
  - `model_limits_customized`: bool（JSON 文件里是否有该通道配置）。
- `PUT /admin/channels/{ch}/models` body 新增可选
  `"model_limits": {"<id>": <int|null>}` 与
  `"default_max_input_tokens": <int|null>`（null=删除该级配置）。
  校验：正整数或 null；模型 id 允许不在当前白名单（预填未来模型）。
- 写入目标 = `model_limits.json`（**不写 DB**）；读 = 同文件。
- 每模型响应 `model_details[].context_window` 依旧保留（traesolo 官方值
  优先展示，本配置独立并存，互不覆盖）。

### 2.4 后端：请求链路强制（workbuddy 等走 proxy 的通道）

在 `upstream/proxy.py` 组装 backend body 处（resolve_model_alias 之后）：
1. **输入预检**：估算输入 token（轻量启发式：全部 message 文本字符数
   ÷ 3，CJK 从宽估计；函数独立可替换）。若估算 > 生效 max_input →
   400 `{"error": {"message": "input exceeds <model> max input context
   (≈<est> > <limit>)", "type": "invalid_request_error"}}`。
2. **max_tokens 注入**：客户端未传 max_tokens 时，注入
   `min(default_max_output_tokens, max_input - 估算输入)`（结果 <1 则不注入）；
   客户端显式传了则尊重，但若 `max_tokens + 估算 > max_input` 则 clamp 到
   剩余空间并记 warning 日志。
3. **qwenwork 硬编码替换**：`chat.py` 的 32000 改为读
   `model_limits.default_max_output_tokens`（内置默认保持 32768；
   如实测 qwenwork 语义特殊可保留通道覆盖，见 2.1 的 channels 覆盖）。
4. 关闭开关：`model_limits.json` 顶层 `"enforce": false` 可整体停用
   预检/clamp（默认 true）。

### 2.5 前端：各平台设置（models.js）

- 模型表格新增「最大输入上下文」列：number 输入（placeholder 显示
  通道生效默认），空 = 未配置；旁边小字显示全局默认。
- 通道级默认输入框（模型表格上方，与思考档位默认同区域）。
- 保存/重置随现有整体 PUT 走，reset 时传 null 清除。
- `src\web` 若有构建流程（package.json/vite）则重建产物；若无（纯静态
  ESM 直出）则改完即生效——实施时先确认并如实报告。

## 3. 约束

- 不重启用户运行中的 8787 网关；e2e 验证用**临时实例**（如 8789 端口 +
  `CB_GATEWAY_DB_PATH` 指向 db 副本），验证完必须关闭。
- 不改 `data/codebuddy_gateway.db` 的 schema；模型限额一律不进 DB。
- `model_limits.json` 提交一份带注释示例（JSON 不支持注释 → 用
  `model_limits.example.json`），真实 `model_limits.json` 进 `.gitignore`。
- pytest 全绿；改动最小化，沿用仓库中文注释风格。

## 4. 执行步骤（workflow 三阶段，agent 全部 provider=buddy model=hy3）

### Phase 1 — 后端核心
- 修 `hy3-preview-agent` → `hy3-x`（含别名保底、tests/文档同步）。
- 新建 `src\providers\model_limits.py` + 根目录
  `model_limits.example.json` + `.gitignore` 条目。
- 实现 2.4 请求链路强制 + qwenwork 替换。
- 单测：优先级解析、坏文件容错、预检 400、注入/clamp、qwenwork 默认值。
- 跑 `pytest tests/test_core.py`（及相关）确认无回归。

### Phase 2 — admin API + 前端
- 扩展 GET/PUT 通道模型端点（读写 JSON 文件）+ 单测。
- models.js 加列与通道默认输入框；确认构建方式并按需重建。
- 手动 curl 验证 GET/PUT 往返（临时实例）。

### Phase 3 — 端到端验证 + 汇总
- 临时实例（8789 + db 副本）e2e：
  1. 配置 hy4-preview max_input=5000 → 发 6000 估算输入请求 → 400 清晰报错；
  2. 调回 262144 → 同请求 200；
  3. 客户端不传 max_tokens → 后端注入值合理（log/响应可证）；
  4. UI 改动生效（GET 静态页或构建产物确认）。
- 报告 `redesign-audit/19-model-limits-report.md`（设计落点、决策、
  验证证据、遗留项）。
- git 提交（不 push），message 一行中文。

## 5. 验收标准

- [ ] `hy3-x` 成为正 id，`hy3-preview-agent` 有别名保底，全仓无残留引用。
- [ ] 各平台设置 UI 可按模型配置最大输入上下文，存 `model_limits.json`，不入 DB。
- [ ] 默认上下文 ≥ 1M；`qwenwork` 的 32000 硬编码移除。
- [ ] 超限请求 400 清晰报错；未传 max_tokens 时注入合理值；显式值被尊重/clamp。
- [ ] pytest 全绿；e2e 四项验证通过；全部改动落在当前分支。
