# 各平台设置：按模型配置思考档位（reasoning effort）设计

> 状态：待评审 · 目标版本：v2.3.0 · 前置分析：2026-02-25 实测探针（`.tmp/probe_effort*.py`）

## 1. 背景与问题

v1.4.10 引入了 `CB_GATEWAY_DEFAULT_REASONING_EFFORT` 环境变量，为 DeepSeek V4 系列注入默认思考档位；
`ops/docker-compose.yml` 把 fallback 写成了 `high`，导致 Docker 部署下所有 DeepSeek 请求都强制高档思考。

现状痛点：

1. **环境变量是全局的**：所有模型一个档位，改一次要重启服务，也无法按模型区分；
2. **只覆盖 DeepSeek**：`_REASONING_DEFAULT_MODEL_IDS` 写死 `deepseek-v4-pro` / `deepseek-v4-flash`；
3. **compose 默认 `high`**：用户不改环境变量就吃高档延迟；
4. `/v1/responses` 路径（Codex 类客户端）丢弃客户端自带的 `reasoning.effort`，无法自救。

需求：去掉环境变量控制，在 Web 管理页「通道与模型 → 各平台设置」里**按模型**设置思考档位。

## 2. 实测结论：上游原生档位（本次探针获取）

上游 `copilot.tencent.com/v2/chat/completions`（WorkBuddy 通道）**没有档位枚举接口**，
但通过对运行中网关直发显式 `reasoning_effort` 的探针实验（显式值在网关白名单内原样透传），
实测拿到原生接受值集合：

| 显式传入值 | 上游行为 | 证据 |
|---|---|---|
| `minimal` | ✅ 接受，轻思考 | deepseek 两次实测 reasoning_tokens 18 / 39 |
| `low` | ✅ 接受 | deepseek 16；glm 154 |
| `medium` | ✅ 接受 | deepseek 25（注意：网关旧枚举只有 low/high/max，这是网关侧限制不是上游的） |
| `high` | ✅ 接受 | deepseek 15；glm 245 |
| `max` | ✅ 接受，重思考 | deepseek 55 / 41 |
| `none` | ✅ 接受，≈轻思考（**不完全关闭**） | deepseek 仍产生 36 / 16 reasoning tokens |
| `off` | ❌ 上游 400 `11150 invalid_reasoning_effort` | 两次复现 |

**按模型行为差异**（关键发现，决定 UI 默认值设计）：

| 模型 | 不传 effort（上游默认） | 传 low | 传 high |
|---|---|---|---|
| deepseek-v4-flash | **不思考**（0 reasoning tokens） | 思考 | 思考 |
| glm-5.2 | **不思考** | 思考（154 tok） | 思考（245 tok） |
| kimi-k2.7 | **默认轻思考**（44 tok） | 更少（15） | 少量（19） |
| auto | **不思考** | 思考（81 tok） | 思考（60 tok） |

即：上游把 `reasoning_effort` 当作**通用思考开关+档位**，不限于 DeepSeek；
glm/auto 这类默认不思考的模型，一旦注入档位反而**开启**思考。

> 探针附注：连续请求会触发账号临时冷却（503 No usable accounts），复测需间隔；
> `none`/`max` 在第一轮的 503 是冷却所致，重试后均 200。数据以 200 行为准。

## 3. 设计目标 / 非目标

**目标**

- G1 按模型配置思考档位，存数据库，Web UI 即时可改、即时生效（无需重启）；
- G2 优先级：客户端显式参数 > 按模型配置 > 通道默认 > 不注入；
- G3 档位枚举采用实测原生集：`minimal` / `low` / `medium` / `high` / `max` / `none`，留空 = 跟随上游默认；
- G4 废弃环境变量路径，compose 不再默认 `high`。

**非目标**

- 不做跨通道统一档位（各通道上游协议不同）；
- 不改 traework / traesolo / qwenwork 的请求体结构（它们的思考由各自 agent 协议决定，
  `qwenwork` 有 `is_reasoning` 标志、traework 是 agent 管线，另行立项）；
- 不做"思考预算 token 数"这类上游不支持的参数。

## 4. 方案总览

```
客户端请求
   │
   ├─ body 带 reasoning_effort？ ──→ 原样透传（现状保留，最高优先级）
   │
   └─ 无 ──→ 查 <channel>.reasoning 配置
              ├─ 有该模型条目（如 "deepseek-v4-flash": "low"）→ 注入
              ├─ 有 "__default__" 条目 → 注入该值
              └─ 都没有 → 不注入（跟随上游默认）
```

- 作用域 v1：**workbuddy 通道**（`upstream/proxy.py`，即原 env 生效处）；
- 键匹配用**别名解析后的后端模型 ID**（与 `body["model"]` 一致；`o3` → `deepseek-v4-pro` 后按后者匹配）；
- provider 协议加能力位 `supports_reasoning_effort`，只有 WorkBuddy 为 True；
  其他通道 UI 显示「该通道不支持按模型思考档位」。

## 5. 数据模型

新增 settings 键（JSON，沿用 `<channel>.xxx` 既有命名法）：

```
workbuddy.reasoning = {
    "__default__": "",                    // 通道默认；"" = 不注入
    "deepseek-v4-flash": "low",
    "glm-5.2": "",                        // 留空 = 该模型不注入
    ...
}
```

- 值域校验：`{"", "minimal", "low", "medium", "high", "max", "none"}`，非法值 400；
- 键存在即自定义（与 `credit_rate` 同语义）；整体替换，与模型白名单保存方式一致；
- **键不存在（未设置过）= 功能关闭**，行为与今天"env 未设"完全一致，零迁移风险。

## 6. 后端改动点

| 文件 | 改动 |
|---|---|
| `upstream/proxy.py` | ① `build_backend_body`：env 注入逻辑替换为 DB 查询 `reasoning_for_model(channel, body["model"])`（优先级链见 §4）；② 删 `_REASONING_DEFAULT_MODEL_IDS` 限制（实测全模型接受）；③ 删 `CB_GATEWAY_DEFAULT_REASONING_EFFORT` 读取（或保留一个版本作最低优先级 fallback 并标废弃）；④ `_VALID_REASONING_DEFAULTS` 扩为 §5 值域（去掉 `"off"`——上游 11150） |
| `providers/model_config.py` | 新增 `channel_reasoning(channel) -> dict`、`reasoning_for_model(channel, model) -> str \| None`（含 `__default__` 兜底、值域清洗）；`_channel_keys` 不动（workbuddy 键名即 `workbuddy.reasoning`） |
| `accounts/control_plane.py` | `channel_model_view` 返回体加 `"reasoning": {...}, "reasoning_customized": bool, "reasoning_choices": [...]+""`；`set_channel_models` 加 `reasoning=` 参数（整体替换 + 值域校验）；`reset` 支持 `reasoning=None` 删除键 |
| `gateway/server.py` | `GET/PUT /admin/channels/{channel}/models` 透传新字段（端点不变，避免前端多一次请求） |
| `providers/workbuddy/__init__.py` | 类属性 `supports_reasoning_effort = True` |
| `providers/protocol.py` | 基类/协议加 `supports_reasoning_effort = False` 默认值 |
| `ops/docker-compose.yml` | 删除 `CB_GATEWAY_DEFAULT_REASONING_EFFORT` 行（**这是本次慢的根因**） |
| `README.md` / `README_EN.md` | 环境变量表删该行，改为「各平台设置 → 思考档位」说明 + 实测档位表 |

注入实现（`build_backend_body` 内，替换现 L536-539）：

```python
if "reasoning_effort" not in body and not has_explicit_thinking:
    effort = reasoning_for_model("workbuddy", body["model"])  # None = 不注入
    if effort:
        body["reasoning_effort"] = effort
```

## 7. Web UI 改动点

`web/js/pages/channels.js`「各平台设置」卡片：

1. **模型白名单表格加「思考档位」列**：每行一个 `<select>`，
   选项：`默认（不注入）` / `none` / `minimal` / `low` / `medium` / `high` / `max`；
   新数据源 `v.reasoning[modelId]`，未出现在配置里的模型显示"默认"；
2. 白名单表上方加**通道默认档位**下拉（写 `__default__`），旁注
   「客户端显式传参始终优先；留空 = 跟随上游默认」；
3. 列头 tooltip / hint 提示实测结论：
   - deepseek/glm/auto 默认不思考，选档位=开启思考；
   - kimi 默认轻思考，`low` 可减少；
   - 想要快：DeepSeek 选 `low`，或留空不注入；
4. `saveChActive` / `resetChActive` 的 body 带上 `reasoning` 字段；
5. 其他通道（协议位为 False）该列显示「—」并禁用。

## 8. 兼容与迁移

- **零迁移**：新键未设置时行为 = 不注入 = 关闭该功能；旧库升级无感；
- env 变量：compose 删除后 Docker 用户立即恢复上游默认行为（这正是修复目的）；
  代码里 `CB_GATEWAY_DEFAULT_REASONING_EFFORT` 保留一个版本、降为最低优先级并标废弃，下版本删除；
- 显式请求参数优先级不变，现有客户端不受影响；
- `off` 虽在旧测试枚举里（`test_build_backend_body_preserves_explicit_reasoning_effort`），
  但那是网关透传行为不是上游接受性；透传测试保留，UI/配置值域不含 `off`。

## 9. 测试计划

| 用例 | 断言 |
|---|---|
| 未设置 `workbuddy.reasoning` | build_backend_body 不注入（回归现状） |
| 配置 `{"deepseek-v4-flash": "low"}` | 该模型注入 low；glm-5.2 不注入 |
| 配置 `{"__default__": "minimal"}` | 无条目模型注入 minimal；有条目的按条目 |
| 客户端显式 `reasoning_effort=high` + 配置 low | 透传 high |
| 别名 `o3` + 配置 `{"deepseek-v4-pro": "low"}` | 注入（键按解析后 ID 匹配） |
| 值域校验 | `off` / `foo` → 400；`""` 合法（不注入） |
| set/reset 往返 | `reasoning=null` 删除键回到"未设置" |
| 响应链路 | Responses → Chat 翻译后仍按上述规则注入 |

## 10. 分期

- **P1（本方案主体）**：workbuddy 通道按模型档位 + UI 列 + compose 修复（约 6 个文件，后端 ~120 行 / 前端 ~40 行）；
- **P2（可选项，另评）**：
  - `/v1/responses` 透传客户端 `reasoning.effort` → chat `reasoning_effort`（Codex 类客户端自救）；
  - usage 日志记录"实际生效档位"，便于对账 reasoning_tokens；
  - qwenwork `is_reasoning` 标志暴露到 UI。

## 11. 风险与备注

- 上游档位集是**实测归纳**而非官方文档，未来上游可能扩枚举（如真·off）；值域做成常量表，扩了只改一处；
- `none` 不等于关闭思考（实测仍有少量 reasoning tokens），UI 文案避免写"关闭"；
- glm/auto 类模型注入档位会开启思考、变慢——UI 默认值必须是"不注入"，并在 hint 里写明；
- 探针脚本留在 `.tmp/probe_effort*.py`（含 503 冷却重试逻辑），上游行为存疑时可随时复测。
