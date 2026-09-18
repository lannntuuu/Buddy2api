# qoderwork × 本机 Qoder CN：区别与「改名复用」可行性调研

作者：research session
日期：2026-09
目标分支 worktree：`wt-qoderwork-research`（基于 `bb2f831`）

---

## 1. 结论速览（TL;DR）

1. **Buddy2api 仓库里的 `qoderwork` 只是一个「占位 ChannelId」，没有任何实现。**
   `src/providers/protocol.py` 把 `qoderwork` 列进了 `KNOWN_CHANNEL_IDS`，但仓库里**没有** `providers/qoderwork/` 目录、没有 `_LOADED` 注册、没有 chat 实现。设计文档明确写着「`qoderwork` 仅占位：2.0.0 默认不加载模块」「PR6（QoderWork）非 2.0.0 必做」「Encode/UMID 事实未补齐前不实现 `encode.py`」。
   所以**没有现成的 `qoderwork` 可「改名」——它是个空壳**。

2. **本机同时装了两种不同的 Qoder 产品，是两家不同的服务端 + 两个不同账号：**

   | 维度 | **QoderWork**（国际/Work） | **Qoder CN**（中国版） |
   |---|---|---|
   | Roaming 目录 | `%APPDATA%\QoderWork` | `%APPDATA%\QoderCN`（IDE）/ `%APPDATA%\com.qodercn.app.stable`（桌面）/ `C:\Usr\Compiler\QoderCN`（安装） |
   | 服务端域名 | `qoder.com` / `gateway.qoder.com` | `qoder.com.cn` / `gateway.qoder.com.cn` |
   | 账号 uid | `019e1f12-db14-78dd-b832-df27ff95e84f`（email lannntuuu@163.com, Teams） | `019f9321-f548-75dd-8c54-062b562ff662`（phone 18693132709, 无 email） |
   | 认证文件 | `auth-v2.dat` / `auth.dat` | IDE：`state.vscdb` 的 `secret://aicoding.auth.*`；桌面：`com.qodercn.app.stable\auth.v1.dat` |
   | 加密 | Chromium `v10` + DPAPI os_crypt | 同 `v10` + 各自 Local State os_crypt |
   | 版本 | desktop `0.9.9`、CLI `0.1.x` | `C:\Usr\Compiler\QoderCN\QoderCN.exe` = 1.25.1（IDE 风） |

3. **用户现有凭据可解密、可用，两个都能读到 dt-/drt- token。**
   - QoderWork `auth-v2.dat` → `dt-*` token，随 `%APPDATA%\QoderWork\Local State` 可解密。
   - Qoder CN `auth.v1.dat` → `dt-QHIr…` / `drt-lLcy…`，`expiresAt=2026-10-18`（当前有效），随 `com.qodercn.app.stable\Local State` 可解。已用 Buddy2api 自带的 `providers/store_common.decrypt_chromium_v10` 成功解开并验证。

## 2. 你不能直接把「qoderwork」改名成「qoder cn」就用

关键点：**仓库里的 `qoderwork` 没有实现，而现有 `qwenwork` provider 对接的也不是 Qoder CN。**

- `src/providers/qwenwork/` 的 `CHANNEL_ID="qwenwork"`，但内部 `BUSINESS_PRODUCT="qoder_work"`、`USER_AGENT="qoderwork/0.1.8"`、`session_type="qoder_work"`、`Cosy-ClientType=6`。它读 **`%APPDATA%\QwenWorkCN\auth-v2.dat`**（千问办公 = Qoder Work 系产品）。**本机没有 `QwenWorkCN` 目录**，只有 `QoderWork`。
- Qoder CN 协议文档 URL 是 `gateway.qoder.com.cn`，与 QwenWork/QoderWork 的 `gateway.qwenwork.cn` 是**不同 COSY 版本**：Appendix A 记录 Qoder CN `Cosy-ClientType=5`（QwenWork 为 6）、chat query 带 `Encode=1`（与 QwenWork 明文相反）、HTTP/1.1。
- 也就是说 Qoder CN 用的是**另一套 COSY 常量 + 可能需要 Encode 的请求体**，不能套用 QwenWork provider。

因此「把 qoderwork 改成 qoder cn 然后用起来」在语义上更准确的落地方式是：**在 Buddy2api 里新增一个真正的 `qodercn` 通道 provider**（或复占 `qoderwork` 这个 id 改成 `qodercn`），从旧的纯占位升级为可用。

## 3. 社区已经在做且证明可行

- [avaritiachaos/qoder-proxy](https://github.com/avaritiachaos/qoder-proxy)（182★）：对接 `qoderclicn`（CN，qoder.com.cn）与 `qodercli`（Global，qoder.com）；CN 端用 **Personal Access Token**（`QODERCN_PERSONAL_ACCESS_TOKEN`，PAT 入口 `https://qoder.com.cn/account/integrations`），Global 端 OAuth 登录。走 CLI 子进程 + 文本适配（受 `SERVER_TOOL_EXECUTION` 限制，非原生消息协议）。
- [Morpheus799/qodercn-gateway](https://github.com/Morpheus799/qodercn-gateway)（从 lingma-proxy 重构，Go）：**远程直连** `gateway.qoder.com.cn`，`internal/remote` 实现 **cosy 签名 + SSE**，凭证从本地 QoderCN CLI 登录缓存读取，也支持 `--remote-auth-file credentials.json`。证明**原生消息协议（cosy 签名）可以绕开 CLI 子进程直连实现**。
- [EchoPing07/Qoder-2API-Go](https://github.com/EchoPing07/Qoder-2API-Go) / [fengyinxia/qoder2api](https://github.com/fengyinxia/qoder2api)（作者参考的 Python 版本）：把 **QoderWork** 桥成 OpenAI 兼容 API，用 PAT + `RSA+AES 混合加密` + `MD5 签名` + `自定义 Base64`。其模型目录反映的是 Qoder Work 系模型（`qmodel_latest` 等）。

> 结论：**路子通——cosy 签名的原生消息协议在社区 Go/Python 里都有可复现实现**，且「远程直连 + 读本地登录缓存」正是 Buddy2api 面向 WorkBuddy/QClaw/QwenWork 的做法。Encode/COSY 细节不再是「完全未知」，而是「有开源参照、需要自行抓包复核 + 写测试向量」。

## 4. 在 Buddy2api 落地需要做的事

以「新增/激活 `qodercn` 通道」为目标的最小工作量（参照 PR6 / PR5 结构）：

1. **凭据导入**：新增 auth 读取器，指向 Qoder CN 的登录缓存。
   - 桌面 `com.qodercn.app.stable\auth.v1.dat`（v10 + 该目录 `Local State` os_crypt）。
   - 或 IDE `%APPDATA%\QoderCN\User\globalStorage\state.vscdb` 的 `secret://aicoding.auth.userInfo`（明文字节也是 `v10…`，用 IDE `Local State` 解）。
   - 字段与 `store.session_to_account` 兼容：`token/auth`（dt-）、`refreshToken`（drt-）、`user.{id,name}`。
2. **COSY 常量**：新建 `providers/qodercn/constants.py`，抄录 qoder.com.cn 域名、chat 路径 `/algo/api/v2/service/pro/sse/agent_chat_generation`、query `FetchKeys=llm_model_result&AgentId=agent_common&Encode=1`、客户端版本/Scene/ClientType 5，以及（如走远程直连）RSA PEM。
3. **cosy/auth**：把 QwenWork `cosy.py` 改造成 qodercn 版本，处理 `Encode=1`（若官方确实要求）与 clienttype 5 差异。先做明文 HTTP 冒烟确认 Encode 是否必需，再写测试向量。
4. **chat/SSE**：复制 `qwenwork/chat.py` 的 SSE 解包/聚合结构，替换常量与签名；HTTP/1.1 保底。
5. **注册**：在 `src/providers/__init__.py` 把新 provider 加进 `_LOADED`（默认关或 opt-in），在 `protocol.py` 把 `qodercn`/`qoderwork` 维护进 `KNOWN_CHANNEL_IDS`，管理页出现「Qoder CN」通道。
6. **命名**：若保留 `qoderwork` id 但改显示名为「Qoder CN」，注意它现在只出现在 docs/协议哨兵里，几乎零迁移成本；更干净的是新增 `qodercn` id，把旧的空壳 `qoderwork` 留在 `KNOWN_CHANNEL_IDS` 作为文档哨兵。

## 5. 风险与未决项

| 风险 | 说明 | 缓解 |
|---|---|---|
| Encode 是否必需 | Appendix A 说 Qoder CN query 带 `Encode=1`；但需用官方客户端抓包验证当前版本是否仍强制 | 先用 `auth.v1.dat` 有效 token 打明文 chat 冒烟，看是否被拒；不行再实现 Encode |
| COSY 字节兼容 | QwerWork 的 `cosy.py` 与 Qoder CN 不保证字节兼容（不同 clienttype/域名） | 独立常量 + 冒烟门闩；参照社区 Go/Python 的 cosy 签名 |
| token 会过期/刷新 | `auth.v1.dat` 现在有效（2026-10-18），但必须要 refresh 逻辑 | 复用 `qwenwork/token.py` 的 refresh 框架，换成 `gateway.qoder.com.cn` 的 refresh 端点 |
| 账号是另一个生态 | Qoder CN 账号（手机号 18693132709）与 QoderWork 账号（email）不是同一个 | 明确告诉用户两个通道对应不同额度账户，Key 要分开 |

## 6. 建议下一步（推荐）

**可行性：高。** 仓库的 `qoderwork` 是空占位，真正的「Qoder CN 通道」需要**新建/激活一个可用的 `qodercn` provider**，而不是字面上改名就能用。

推荐执行顺序：
1. 用现成 `auth.v1.dat` token（已解密验证有效）做一次 `gateway.qoder.com.cn` chat 冒烟 → 确定 Encode 是否必需。
2. 冒烟成功 → 新增 `providers/qodercn/*`（constants / cosy / token / store / chat），默认关闭。
3. 注册进 registry + 管理页 → 通「通道管理 → Qoder CN → 导入本机账号」。
4. 冒烟失败卡在 Encode → 参照社区实现补 Encode 与测试向量。

需要的话，我可以直接在 `wt-qoderwork-research` 分支里把第 2~3 步（新增 `qodercn` provider 骨架 + 注册）做出来。