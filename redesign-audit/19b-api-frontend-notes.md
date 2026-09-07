# 19b — 模型限额 Phase 2 admin API + 前端笔记

> spec §2.3/§2.5。前置：Phase 1 见 19a-backend-notes.md。

## 后端 API（§2.3）

| 位置 | 改动 |
|---|---|
| `src/accounts/control_plane.py` | `channel_model_view` 追加 `model_limits`（仅显式配置 int|null）/ `default_max_input_tokens`（生效默认解析值）/ `model_limits_customized`（bool），来自 `model_limits.get_channel_limits`；`set_channel_models` 新增可选 `model_limits` / `default_max_input_tokens` + `set_model_limits` / `set_default_max_input` 标志位（沿用 credit_rate 的 set_ 模式） |
| `control_plane._set_model_limits` / `_validate_limit_value` | 读改写 model_limits.json（不进 DB）：校验正整数或 null；模型 id 允许不在白名单（预填未来模型）；null=删除该级配置；整体替换语义（同 aliases，{}=清空每模型配置）；删空后收敛通道节点，不留空壳；坏文件当 {} 起步，管理端重写即修复 |
| `src/gateway/routers/admin/_channels.py` | PUT `/admin/channels/{ch}/models` 透传 `model_limits` / `default_max_input_tokens`（"key in body" 判定 set_ 标志位，与既有四字段一致），ValueError → 400 |

GET 响应新增字段示例：

```json
{
  "model_limits": {"hy4-preview": 5000},
  "default_max_input_tokens": 262144,
  "model_limits_customized": true
}
```

## 前端（§2.5）

`src/web/js/pages/models.js`（无构建流程确认：`src/web` 只有 css/fonts/js/vendor + index.html，无 package.json / vite / 任何构建脚本，Vue 为 vendor 全局直出的纯静态 ESM——**改完即生效，无需重建**）：

- 模型表格新增「最大输入上下文」列：number 输入，placeholder 显示通道生效默认（如 `默认 1048576`），空 = 未配置。
- 通道级默认输入框放在思考档位默认同区域（通道支持思考档位时并排；不支持的通道单独一块，同样可配）。
- 保存随现有整体 PUT：`model_limits` 仅提交显式填了正整数的行（空/null 行不提交），`default_max_input_tokens` 空串提交 null（删除通道级默认）。
- 重置：`model_limits: {}`（整体替换语义清空）+ `default_max_input_tokens: null`，随现有 reset PUT 一并传。
- 「已自定义上下文限额」tag 依据 `model_limits_customized`。

## 单测（tests/test_model_limits.py API 部分）

- GET 视图：已配置通道回显 model_limits/生效默认/customized=true；未配置通道 `{}` + 内置默认 1048576 + false。
- PUT 往返：写入后磁盘 JSON 与视图一致；非白名单 id 可预填；null 条目=删除不落盘。
- null 重置：通道级默认删除、每模型条目删除、通道节点删空后收敛、customized 归 false。
- 非法值 400（ValueError）：`"x"`、`0`、`-1`、`1.5`、`{}`、`[]`，每模型与通道默认两路均校验。

## 测试结果

```
tests/test_model_limits.py tests/test_channel_models.py → 53 passed
tests/test_core.py tests/test_model_limits.py → 133 passed, 2 errors（已知沙箱 tmp_path 问题，见 19a）
```

## 手动 curl 往返

见 19-model-limits-report.md Phase 3 e2e（临时实例 8789 实测 GET/PUT 往返 + 400/200）。
