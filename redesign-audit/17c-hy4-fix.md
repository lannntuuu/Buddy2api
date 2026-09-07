# 17c — hy4-preview 修复与验证（Phase C）

> 分支：`fix/hy4-preview-usage`
> 依赖：Phase A（`17a-hy4-forensics.md`）+ Phase B（`17b-hy4-repro.md`）
> 工具：`.venv\Scripts\python.exe` + 内置 `sqlite3`（db 读写前已备份）

---

## 0. 根因（与 Phase B 一致，不再复现）

`hy4-preview` 只来自客户端请求体 `model` 字段，无白名单/别名/统一映射。
当前 workbuddy `models` 设置被收窄为 `[{"id":"wb-m"}]`，`router.bind()` 在接触上游前
即因 `WorkBuddyProvider.accepts_model` 不匹配而抛 `UnknownModel` → 网关返回
**HTTP 400 `unknown_model`**（非流、流式一致）。Phase A 的 4×200 证明上游曾真实提供
`hy4-preview`；现网请求根本到不了上游，这正是「一直有问题」的近况主因。

补充：当前配置下**连代码 `DEFAULT_MODELS` 里的 `hy3-preview-agent` 也被同样 400 拒掉**，
说明白名单已严重收窄——历史成功发生在配置更宽时期。

---

## 1. 修复内容

### 1.1 数据修复（首选，最小且对症；Phase B 建议 A）

前提：改动真库前先整库备份（见 §2）。

把 `hy4-preview` 与同源的 `hy3-preview-agent` 加回 workbuddy `models` 白名单，使
`bind()` 放行（上游曾证可用，放行即恢复 200）。未动别名的 full-replace 语义，未碰
`unified_models` / `model_aliases`。

```text
改动前：models = [{"id": "wb-m"}]
改动后：models = [{"id": "wb-m"}, {"id": "hy4-preview"}, {"id": "hy3-preview-agent"}]
```

> 注：同时恢复 `hy3-preview-agent` 是因为它被当前白名单一并误伤（Phase B 复现确认），
> 仅加 hy4 会遗漏这个同属代码内置模型列表却同样被挡的 id。**最终应暴露的模型清单以产品
> 确认为准**；若产品认定 workbuddy 应保持更宽列表，可进一步扩展（本次仅补齐被实证需要的两项）。

### 1.2 代码修复 B（11128 安全语义不再误走超长自愈；Phase B 建议 B）

`src/upstream/compaction.py`：

- 新增 `_COMPACT_11128_SECURITY_MARKERS = ("unapproved channel", "Illegal API invocation from an unapproved channel")`。
- `_is_11128_error` 在命中上述安全语义文案时**直接返回 False**，不再当作「超长请求」
  触发 `_smart_compact_messages` 精简重试。理由：
  - 该 11128 语义是「通道/客户端未授权」安全策略拦截（日志 id=1346，zcode 客户端），与请求体大小无关；
  - 精简 `messages`/`tools` 内容既无效、又会无谓改写请求体并武装 `(channel,client)` 自愈状态，徒增噪声；
  - 不影响真正的超长 11128（`"Illegal API invocation"` 但不含 `unapproved channel` 文案）继续走自愈。

### 1.3 已确认无需改动 / 选择不做的项

- **清晰报错（建议 D）**：白名单拒绝已是结构化 **400 `unknown_model`**（`src/gateway/router.py:127`），
  不裸透传上游错误，满足「清晰报错」要求，无需额外改动。
- **502 invalid call id（建议 C，可选）**：`src/upstream/chat_grammar.py:177` 对上游 tool-call 帧
  `id` 非空字符串判为畸形并整条 502。属上游偶发 + 校验过严，但**无法在本环境复现/验证**
  （实时上游探测因账户 `refresh_token` 为空全部 401/503，见 Phase B §2），改动风险大于收益，
  故**本次不做**，留作后续在可联网环境单独评估。
- **文档化 / `/v1/models`**：白名单已放开，模型会自然出现在对外列表；若产品决定长期不支持，再补文档。

---

## 2. 数据备份（约束：db 读写前先备份，含 -wal/-shm）

无网关进程持有写锁（已用 `BEGIN IMMEDIATE` 探活：可正常取到写锁 ⇒ 无活跃写入者），
端口 8787 无监听进程 ⇒ 未触碰运行中网关。

备份命令（脚本法，未开长驻进程）：

```powershell
# 拷贝真库（含 -wal/-shm）到 data/backup/
$ ts = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item data\codebuddy_gateway.db data\backup\bak_before_hy4fix_$ts.db
```

实际产物：

```
data/backup/bak_before_hy4fix_20260907_114605.db   (2,166,784 bytes)
```

> 本次 `-wal` / `-shm` 文件均不存在（`PRAGMA synchronous=NORMAL` 下无未提交 WAL 帧），
> 主 `.db` 文件自洽，单独备份主文件即等价于「含 -wal/-shm」的完整一致性快照。
> 既有 `data/backup/*.db` 与 `.bak_*` 文件未动。

待执行 SQL（如日后需回滚，在只读副本或停写后执行）：

```sql
-- 回滚 models 到收窄前（仅 wb-m）状态
UPDATE settings SET value='[{"id":"wb-m"}]' WHERE key='models';
```

---

## 3. 验证

### 3.1 `bind()` 闸门（确定性，复现 Phase B Part A）

方法：把真库整库拷到临时区（`CB_GATEWAY_DB_PATH` 指向副本），`router.bind()` 走真实
`WorkBuddyProvider.accepts_model` → `list_models()` 读回刚改的 `models` 设置。

| model | stream | bind 结果 |
|---|---|---|
| `hy4-preview` | false | **OK** (workbuddy / hy4-preview) |
| `hy4-preview` | true  | **OK** |
| `hy3-preview-agent` | false | **OK** |
| `hy3-preview-agent` | true  | **OK** |
| `wb-m` | false/true | OK（对照，保持放行） |
| `auto` | false/true | OK（别名命中，保持放行） |
| `glm-5.2` | false/true | `UnknownModel`（**正确**：属其它通道，网关给结构化 400，非裸透传） |

**结论**：白名单 400 已解除，`hy4-preview`/`hy3-preview-agent` 非流与流式均 `bind()` 通过。

### 3.2 端到端 200 验证说明（诚实披露）

**本环境无法完成真实的「非 stream + stream 各一次到达上游并 200」验证**，原因同 Phase B §2：
真库 workbuddy 账户 `refresh_token` 为空 → `auth_manager.get_valid_headers` 返回 `None`
→ 所有发起的上游调用在鉴权层即 401/503，与模型名无关（`wb-m` 对照同样失败）。因此：

- 修复正确性以 **§3.1 的 `bind()` 闸门放行** + **Phase A 的 4×200 历史实证（上游曾提供 hy4-preview）**
  为据；放行后请求可达上游，且历史上同路径曾成功返回正常 completion。
- 当前可用性 / 工具调用流 502 / 11128 安全拦截的最终判定，需在有有效 token 的环境或用户侧真实请求确认。

### 3.3 单元测试（无回归）

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_core.py -x -q
```

结果：**139 passed**（含新增 `test_is_11128_error_excludes_unapproved_channel_security_semantic`）。
2 个 ERROR 为 `tmp_path` 夹具在受限沙箱下创建 `tmp_pytest_tmp`/`tests/_run_tmp` 的
**环境权限错误，与本次改动无关**（其失败点是不允许写临时目录，而非 hy4/11128 逻辑）。
新增用例独立运行通过：

```
tests/test_core.py::test_is_11128_error_excludes_unapproved_channel_security_semantic PASSED
```

新增用例覆盖：

- 安全语义 `{"code":11128,"msg":"...unapproved channel"}`（dict / 字符串两种形态）→ `_is_11128_error` 返回 `False`；
- 真实超长语义（含 `Illegal API invocation` 但**不含** `unapproved channel`）→ 仍返回 `True`；
- 已精简过（`_compacted_11128=True`）→ 不再二次自愈（防 busy-loop）；
- 非 400 状态 → 不参与。

---

## 4. 改动文件清单

- 数据（真库，已备份）：`data/codebuddy_gateway.db` 的 `settings.models`
  （`[{"id":"wb-m"}]` → `[{"id":"wb-m"},{"id":"hy4-preview"},{"id":"hy3-preview-agent"}]`）
- 代码（最小）：`src/upstream/compaction.py`（新增安全语义排除 + `_is_11128_error` 短路）
- 测试：`tests/test_core.py`（新增 1 个用例）
- 报告：本文件 `redesign-audit/17c-hy4-fix.md`

---

## 5. 与相关 spec 验收对照

- [x] 写明 hy4-preview 来源与失败根因（Phase A/B + 本文件 §0）
- [x] 修复后 `hy4-preview` 请求放行（§3.1；端到端 200 受环境凭据限制，见 §3.2 诚实披露）
- [x] `tests/test_core.py` 相关用例通过，无回归（§3.3）
- [x] 全部改动落在 `fix/hy4-preview-usage` 分支（见提交）
