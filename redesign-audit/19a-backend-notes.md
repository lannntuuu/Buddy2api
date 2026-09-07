# 19a — 模型限额 Phase 1 后端笔记（落点 / 决策 / 测试）

> 分支 `fix/hy4-preview-usage`；spec：redesign-audit/19-model-limits-spec.md §2.1/§2.2/§2.4。

## 落点

| 文件 | 改动 |
|---|---|
| `src/providers/model_limits.py` | 新模块：加载/查询/写回 model_limits.json（mtime+size 缓存、线程锁、坏文件 warn 一次不 crash、enforce 开关、estimate_input_tokens ÷3 启发式、get_channel_limits 供 admin 视图） |
| `model_limits.example.json` | 带注释语义的示例（JSON 不支持注释，故独立 example 文件） |
| `.gitignore` | `model_limits.json` 入忽略（不进 git，不进 DB） |
| `src/upstream/proxy.py` | `_apply_model_limits`：输入预检 400 `ModelLimitError` + max_tokens 注入/clamp；挂在 `build_backend_body` 的 `resolve_model_alias` 之后；`proxy_chat_completions` 捕获转 400 |
| `src/providers/qwenwork/chat.py` | 32000 硬编码 → `model_limits.get_default_max_output_tokens("qwenwork")`（内置默认 32768） |
| `src/upstream/aliases.py` | `hy3-preview-agent` → `hy3-x`（DEFAULT_MODELS 正 id + `_BUILTIN_ALIASES` 保底映射） |
| `tests/test_model_limits.py` | 21 个用例（Phase 1 部分 + Phase 2 API 部分） |

## 决策 / 实施偏差说明

1. **通道归属**：`_apply_model_limits` 用 `payload["_bind_channel"]`（gateway.router/v1 绑定时注入），缺省落 `workbuddy`。核实过 proxy 链路（`proxy_chat_completions`）仅 workbuddy provider 调用，缺省值成立。
2. **模型级限额挂在 resolve_model_alias 之后**：预检对象是后端真实 id（hy3-preview-agent 会先解析成 hy3-x 再查限额），配置必须写真实 id。
3. **缓存签名 mtime+size**：原实现仅 mtime，同一 mtime tick 内连续写入（如 e2e 连续 PUT）会误命中缓存；加 size 后消除。测试中发现并修复。
4. **全局 `default_max_input_tokens: null`**：原实现回退内置默认；按 spec「null = 显式不限制」修正为返回 None（跳过预检）。`get_default_max_input_tokens()`（admin 展示用）仍把 null 折叠为内置默认，避免 UI 显示空。
5. **max_tokens clamp 剩余 <1 时**：移除 `max_tokens` 字段交由上游决定（注入 0/负数会让上游报错）。
6. **qwenwork 通道覆盖**：`get_default_max_output_tokens("qwenwork")` 支持通道级覆盖，example 文件中保留该键作示范。

## 测试结果

```
.venv\Scripts\python.exe -m pytest tests/test_core.py tests/test_model_limits.py -q -p no:cacheprovider
133 passed, 2 errors in 23.31s
```

- 2 个 error 为已知沙箱问题（`tmp_path` 在 DSH 沙箱下 WinError 5 拒绝访问 `%TEMP%\dsh-*\pytest-of-Admin`），与代码无关；redesign-audit/17c 已记录同一现象。
- 本文件用例沿用 conftest.isolated_db 思路：限额文件放仓库 `.tmp/model-limits-test-*` 唯一子目录（沙箱禁写系统 TEMP），用后清理。

## 遗留

- `tmp_pytest_tmp/`、`tmp_pytest_basetemp/`（另有 `docs/pytest_tmp/`、`tests/_run_tmp/`）为历史沙箱锁定目录，无法在本会话删除，待手动清理。
