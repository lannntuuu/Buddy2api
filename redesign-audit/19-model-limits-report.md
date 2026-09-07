# 19 — 模型最大输入上下文配置 + 通用 max_tokens 配置：实施报告

> 分支 `fix/hy4-preview-usage`（未 push）；spec：19-model-limits-spec.md；
> 分阶段笔记：19a-backend-notes.md（Phase 1）、19b-api-frontend-notes.md（Phase 2）。

## 1. 设计落点（与 spec 对照）

| spec 条目 | 落点 | 状态 |
|---|---|---|
| §2.1 model_limits.json（仓库根，不进 DB） | `src/providers/model_limits.py`（加载/mtime+size 缓存/线程锁/写回）+ `model_limits.example.json` + `.gitignore` 条目 | ✅ |
| §2.1 优先级 模型级>通道级>全局>内置 1M | `resolve_max_input_tokens()`；单测覆盖四级 | ✅ |
| §2.1 null=不限制 / 坏文件容错 / enforce 开关 | null→None 跳过预检；坏 JSON warn 一次走默认不 crash；`enforce:false` 整体跳过 | ✅ |
| §2.2 hy3-preview-agent→hy3-x | `src/upstream/aliases.py` DEFAULT_MODELS 正 id + `_BUILTIN_ALIASES` 保底映射；src/ 无残留引用（docs/redesign-audit 历史记录保留原文） | ✅ |
| §2.3 admin GET/PUT | `control_plane.channel_model_view`/`set_channel_models` + `_set_model_limits`；`_channels.py` PUT 透传；写 JSON 不写 DB | ✅ |
| §2.4 输入预检 400 / max_tokens 注入与 clamp | `upstream/proxy.py` `_apply_model_limits`（resolve_model_alias 之后）+ `ModelLimitError` → 400 | ✅ |
| §2.4 qwenwork 32000 硬编码 | `qwenwork/chat.py` → `model_limits.get_default_max_output_tokens("qwenwork")`（默认 32768） | ✅ |
| §2.5 前端 models.js | 「最大输入上下文」列 + 通道级默认输入框（思考档位默认同区域）+ 保存/重置随整体 PUT | ✅ |

与 spec 的偏差（均为澄清而非偏离）：
1. **默认值归属**：spec §2.1 把「内置默认 1048576」写成全局 default 之后的兜底；实现上全局 null / 通道 null / 模型 null 都是"显式不限制"，仅"键缺失"才落 1M 内置默认。
2. **max_tokens 注入下限**：spec 未定义剩余 <1 的行为；实现为移除 max_tokens 字段交上游（避免注入 0/负数）。
3. **PUT model_limits 语义**：spec 未定义部分更新；实现为整体替换（同 aliases 语义，{}=清空），null 条目=删除。
4. **spec §4 说 "agent 全部 provider=buddy model=hy3"**：实际由当前会话 agent 完成，无偏差影响。

## 2. 测试

```
pytest tests/test_core.py tests/test_model_limits.py
  → 133 passed, 2 errors（已知沙箱 tmp_path WinError 5，见下）

pytest tests/test_core.py tests/test_model_limits.py tests/test_channel_models.py tests/test_qwenwork.py
  → 178 passed, 3 errors（第 3 个 test_qwenwork_auth_dirs_ignore_workbuddy_cb_auth_dir
    同样是 tmp_path 沙箱问题，非本次改动引入，单独跑同样 error）
```

新用例（tests/test_model_limits.py，21 个）：优先级解析、内置默认、null 三级、
坏 JSON 容错+恢复、enforce=false、预检 400、注入、显式尊重、clamp、qwenwork
默认值来源、hy3-x 别名保底、estimate 启发式、GET 视图三字段、未配置通道、
PUT 往返/null 重置/节点收敛、非法值 400（x/0/-1/1.5/{}/[]）。

**已知环境性 error（不修）**：`tmp_path` 在 DSH 沙箱下对
`%TEMP%\dsh-*\pytest-of-Admin` WinError 5 拒绝访问——影响任何用 tmp_path
的用例（test_core 2 个 + test_qwenwork 1 个），与代码无关；
redesign-audit/17c 记录过同现象。

## 3. e2e 证据（临时实例 8789 + db 副本）

实例：`python -m gateway.server --port 8789`，
`CB_GATEWAY_DB_PATH` 指向系统临时目录副本（db+key，未动 8787）。
admin token / API key 均临时创建于副本库。

### a) 限额 5000 → 6000 估算输入 → 400 清晰报错 ✅

```
PUT /admin/channels/workbuddy/models {"model_limits":{"hy4-preview":5000}} → 200
POST /v1/chat/completions  model=hy4-preview, messages=[{"role":"user","content":"x"*18000}]  (est ≈6000)
→ HTTP 400
{"error":{"message":"input exceeds hy4-preview max input context (≈6000 > 5000)","type":"invalid_request_error"}}
```

### b) 调回 262144 → 同请求 200（真实上游）✅

```
PUT {"model_limits":{"hy4-preview":262144}} → 200
同请求 → HTTP 200 (18.4s，真实 workbuddy 凭据，copilot.tencent.com 200)
{"id":"chatcmpl-23fa354d6f4db04eb6b9618cc","object":"chat.completion","model":"hy4-preview",
 "choices":[{"message":{"role":"assistant","content":"It looks like your message is just a long string of **x** characters..."}}],
 "usage":{"prompt_tokens":2271,"completion_tokens":577,...,"completion_thinking_tokens":532,"credit":0}}
```

### c) 不传 max_tokens → 注入值合理 ✅

- b) 响应 completion_tokens=577、reasoning 532（未顶到注入上限即正常结束；
  实测 hy4-preview 推理会吃满 max_tokens，注入上限 min(32768, 262144−est)=32768 生效且被上游接受）。
- 显式 max_tokens=2048 的短请求 → 200，内容正常返回（显式值被尊重，未 clamp）。
- clamp 路径（显式值超剩余空间→clamp+warning）由单测
  `test_proxy_clamps_client_max_tokens_over_remaining` 覆盖（5000→90）。

### d) UI 生效确认 ✅

- 无构建流程确认：src/web 无 package.json/vite（css/fonts/js/vendor 直出，
  Vue 全局 vendor 版），纯静态 ESM——改完即生效。
- `GET /static/js/pages/models.js` → 200，包含「最大输入上下文」列、
  model_limits / default_max_input_tokens / model_limits_customized / defaultMaxInput 全部新逻辑。
- `GET /` → 200。

## 4. 清理

- 临时实例已关闭（job kill 后确认 8789 无监听）。
- db 副本目录（含 -wal/-shm/key 与请求/响应样本文件）已删除。
- `model_limits.json` 已还原为 e2e 前状态（不存在；其内容本就 gitignored，
  `git ls-files` 确认未被跟踪，提交内容不含该文件）。
- 测试期临时 API key 建在副本库上，随副本删除，真库无痕迹。

## 5. 遗留项

1. **沙箱锁定目录待手动清理**：`tmp_pytest_tmp/`、`tmp_pytest_basetemp/`
   （另见 `docs/pytest_tmp/`、`tests/_run_tmp/`，git status 有 Permission denied 告警）——本会话无权限删除。
2. `data/backup/credentials.key.latest`、历史 redesign-audit 06~18 文档未随本提交入库（非本任务范围）。
3. 输入估算为 ÷3 启发式（CJK 从宽），与上游真实 prompt_tokens 有出入
   （e2e：估 6000 vs 上游报 2271）——预检方向偏保守（宁可多拦），未来可换 tokenizer。
4. enforce=false 时注入也一并跳过（spec 未细分；如需"只关预检保留注入"需再拆开关）。

## 6. 提交

- 全部相关文件一次提交（源码/测试/example/.gitignore/redesign-audit 19×3），
  commit message 一行中文；model_limits.json 不在提交内（gitignore 覆盖）。
- 未 push；未动 8787；未改 db schema。
