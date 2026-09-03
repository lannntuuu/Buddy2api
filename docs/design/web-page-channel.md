# 反向代理「豆包 / DeepSeek / ChatGPT 网页」通道：技术评估

> 范围：评估在本项目（`Buddy2api`）里**新增一个"基于本机浏览器 Cookie 的网页反向代理通道"**的可行性、所需改动、风险与维护成本。
> **本文件只做评估与设计，不写实现代码。** 实际动手请参考文末「落地清单」按步骤执行。

---

## 0. TL;DR

| 项 | 结论 |
|---|---|
| 能不能做 | 能。三家都有**未公开但可观察到的内部 HTTP 接口**，带本机浏览器 Cookie 即可调用 |
| 应不应该做 | **谨慎**。三家 ToS 都禁止非官方客户端访问；DeepSeek/豆包 维护成本高（每月可能失效 1~N 次） |
| 推荐档位 | **先按方案 B（DeepSeek 优先）做最小通道**；ChatGPT 通道在 DeepSeek 稳了之后再加；**豆包通道不做** |
| 是否需要环境变量 | **不需要**。沿用现有 `gmi` 的做法，加进 `OPT_IN_PROVIDER_IDS` 即可由管理页 `enabled_channels` 开关管控 |
| 主要工程量 | 1 个新 provider 包（4~6 个文件）+ 1 处协议层枚举 + 1 处注册 = 约 600~900 行（含单测占位） |
| 最大风险点 | 前端签名/路径变更导致接口失效；Cookie 过期/被风控后无提示 → 必须有清晰的错误回传 |

---

## 1. 目标与边界

### 1.1 用户故事（自用场景）

- 用户在**本机 Chrome** 已登录 `chat.deepseek.com`（同理适用于 `chatgpt.com`、`www.doubao.com`）。
- 用户从 DevTools → Application → Cookies 复制一份该站点的 Cookie JSON，或通过浏览器扩展导出一份。
- 在 Buddy2api 管理页「账号」页选「Web 反向代理」通道 → 粘贴 Cookie → 一键导入。
- 创建 API Key 时绑这个通道。
- 客户端（Cherry Studio / NextChat / OpenCode 等）走 `http://127.0.0.1:8787/v1` 调用，体验与现有 5 通道一致。

### 1.2 非目标（明确不做）

1. **不做公网部署**。任何"把 8787 暴露到 0.0.0.0 让别人用"的形态都立即作废，账号共享还会让所有人的 Cookie 一起进风控黑名单。
2. **不做凭据自动抓取**（不读 Chrome 本地 `Cookies` SQLite）。只接受**用户主动粘贴**的 Cookie 字符串或 JSON。原因：
   - Chrome ≥ 127 的 Cookie DB 是 `App-Bound Encryption`，普通进程读不到明文；
   - 即便能读也是 ToS 高风险行为；
   - 让用户手动粘贴等于让用户**显式同意**。
4. **不做多账号轮询/共享**。每个 Cookie 都是单账号绑定，**多账号轮询会触发风控的关联检测**。最多允许"个人备号列表 + 轮询开关默认关"。

### 1.3 与现有架构的对齐

| 现状 | 对齐方式 |
|---|---|
| `providers/protocol.py` 里 `KNOWN_CHANNEL_IDS` 是 `Literal[...]` | 在字面量里**追加 `webdeepseek` / `webchatgpt`**，不加 `webdoubao` |
| 5 个默认通道由 `providers/__init__.py` 的 `DEFAULT_PROVIDER_IDS` 控制 | 新通道加进 `OPT_IN_PROVIDER_IDS`，默认不开启 |
| 管理页 `/admin/channels` 用 `providers.enabled_provider_ids()` 取启用列表 | 不用改，新通道自动出现在 UI 的"未启用"区域 |
| `LOCKED_FIRST = "workbuddy"` | 保持不动 |
| `traesolo` 走"凭证 JSON 导入"模式 | **完全照抄**这个模式，因为它就是"网页登录闭环 + JSON 凭证"，最接近新通道形态 |

---

## 2. 设计：把网页端当成"特殊账号源"

### 2.1 把 Cookie 当成"一种账号类型"

抽象一下：现有 5 通道都是「本机客户端 → 标准 API Key/Token」。**网页反向代理通道本质是"本机浏览器 Cookie → 未公开 HTTP 接口"**。

为了不污染现有 `accounts` 表结构，建议：

- 仍然走 `accounts` 表存储，但 `provider` 字段值是 `"webdeepseek"` 等新通道 ID；
- Cookie 内容存在 `accounts.extra_json` 里（结构见 §3.2），不新增列；
- `accounts.api_key` 字段对网页通道**无意义**，写一个固定占位（`"web-cookie"`）即可。

### 2.2 单通道单账号 vs 多通道多账号

**决策：做"按厂商一个通道"，不做"一个大 web 通道包含三家"。**

理由：
- 三家的 Cookie 结构、接口路径、签名算法、SSE 协议都不同；
- 合并到一个 provider 内部会让文件快速膨胀（>1500 行），回归测试爆炸；
- 用户视角下"切换通道"比"切换子类型"心智负担低；
- 出问题时按通道隔离，用户和开发者都好排查。

### 2.3 推荐落地顺序

1. **先做 `webdeepseek`**（最小风险、需求最明确、社区参考资料最多）
2. `webdeepseek` 跑稳 2~4 周后做 **`webchatgpt`**
3. **不做 `webdoubao`**（除非用户单独要求）。理由见 §6。

---

## 3. 详细设计

### 3.1 目录与文件清单

新增 `providers/webdeepseek/`，结构对标 `providers/gmi/`：

```
providers/webdeepseek/
├── __init__.py        # PROVIDER 实例 + Provider Protocol 适配
├── constants.py       # 通道 ID、显示名、Cookie 必需键、SSE 路径模板
├── store.py           # parse_credentials / import_path / upsert_account
├── chat.py            # chat_completions 主体：OpenAI 请求 → 网页接口 → OpenAI 流
├── sse.py             # DeepSeek SSE 事件 → OpenAI ChatCompletionChunk 转换
└── quota.py           # fetch_quota / test_chat（占位实现 + 真实接口可选）
```

并改 3 个现有文件（极小改动）：

- `providers/protocol.py`：在 `ChannelId` Literal 与 `KNOWN_CHANNEL_IDS` 里加 `"webdeepseek"`（占位）。
- `providers/__init__.py`：在 `OPT_IN_PROVIDER_IDS` 里加 `"webdeepseek"`、在 `_LOADED` 注册。
- `README.md`：在通道表里加一行（备注"需手动导入 Cookie，默认不开启"）。

### 3.2 凭证数据结构

存在 `accounts.extra_json`，schema 草案：

```jsonc
{
  "kind": "webdeepseek",                  // 冗余存储，做 migration 时用
  "cookies": [
    {"name": "sessionid", "domain": ".deepseek.com",
     "value": "...", "httpOnly": true, "secure": true},
    {"name": "__Secure-next-auth.session-token", "domain": "chat.deepseek.com",
     "value": "...", "httpOnly": true, "secure": true}
  ],
  "captured_at": "2026-01-15T10:00:00Z",  // 用户粘贴时间，用于提示"该重新粘贴"
  "user_agent": "Mozilla/5.0 (...)",      // 可选；某些站会校验 UA
  "notes": "imported via admin UI"        // 自由文本，前端展示
}
```

**校验规则**（`store.parse_credentials`）：
- 必须是非空 list；
- 每条必须含 `name` + `domain` + `value`，且 `domain` 命中白名单（`deepseek.com` / `chat.deepseek.com`）；
- 关键键缺失（如 `__Secure-next-auth.session-token`）直接 400 拒绝，并提示"请确认已登录且勾选 Application → Cookies"；
- `captured_at` 自动填充当前时间。

**去重规则**：同一 `sessionid` 的 `value` 已存在则拒绝重复导入。

### 3.3 Cookie 刷新（不实现）

- **不做**自动刷新。Cookie 过期（一般 7~30 天）后用户重新粘贴即可。
- **可选**：在 `/v1/chat/completions` 第一次返回 401/403 时，往 stderr 写一行"cookie expired, re-import"，但不触发自动刷新。

### 3.4 模型映射

`constants.STATIC_MODELS` 草案：

```python
STATIC_MODELS = (
    "deepseek-chat",         # V3 / R1 路由由 deepseek 自家切换
    "deepseek-reasoner",     # R1
    "deepseek-coder",        # 若官方仍保留
)
```

`alias_map` 草案：

```python
ALIASES = {
    "auto":          "deepseek-chat",
    "deepseek":      "deepseek-chat",
    "deepseek-v3":   "deepseek-chat",
    "r1":            "deepseek-reasoner",
    "deepseek-r1":   "deepseek-reasoner",
}
```

**为什么不用动态拉取**：DeepSeek 网页端 `/api/models` 经常不返回 V3/R1 的真实 ID，且改名前后口径不一致。**静态白名单更可预测**。

### 3.5 `chat_completions` 数据流

```
┌─────────────────┐
│ /v1/chat/       │
│ completions     │
└────────┬────────┘
         │ OpenAI ChatCompletionRequest
         ▼
   payload translate
   (model, messages → DeepSeek 内部格式)
         │
         ▼
   build headers
   (Cookie 拼接, User-Agent, X-Source, Referer)
         │
         ▼
   POST https://chat.deepseek.com/api/v0/chat/completions
   (SSE, stream=true)
         │
         ▼
   parse SSE 事件:
     {"type": "message_start", "message": {...}}
     {"type": "content_block_start", ...}
     {"type": "content_block_delta", "delta": {"text": "..."}}
     {"type": "message_stop"}
     {"type": "error", "error": {"code": ..., "message": ...}}
         │
         ▼
   yield ChatCompletionChunk
   (id, model, choices[0].delta.content, finish_reason)
```

**关键点**：

1. **`messages` 压缩**：OpenAI 可以传任意长 system prompt，但 DeepSeek 网页端对单条消息长度敏感（实测 ~8KB 上下），超过会被截断或 400。需要在 `chat.py` 入口做"超长 system 折叠成 summary"的预处理。**先不做**，在错误里直接提示用户"system too long, retry with shorter"。
2. **流式优先**：网页端不支持非流式（或返回不完整），所以 `stream=true` 是默认；非流式请求在网关层缓冲流。
3. **非流式**：用 `stream=true` 拉到底再聚合成 `ChatCompletionResponse`，保持与现有 5 通道一致的对外行为。

### 3.6 错误映射

| DeepSeek 返回 | 内部抛出 | HTTP 状态 |
|---|---|---|
| 401 / 403 cookie 失效 | `RuntimeError("cookie_expired")` | 401 |
| 429 速率 | `RuntimeError("rate_limited")` | 429 |
| 5xx 服务端 | `RuntimeError("upstream_5xx")` | 502 |
| challenge / 风控拦截（HTML 而非 JSON） | `RuntimeError("upstream_challenge")` | 502 |
| SSE 中途断流 | `RuntimeError("stream_truncated")` | 502 |

**所有错误必须带 `provider="webdeepseek"` 前缀**（gateway 已经统一加，这里保持即可）。

### 3.7 quota / checkin

- **quota**：默认返回 `unsupported=True`（DeepSeek 网页端无配额查询接口）。
- **checkin**：`checkin_supported = False`。
- `test_chat` 实现：发一条 `ping` 消息，期望 1 秒内拿到响应；否则判定 Cookie 失效。

---

## 4. 管理页 / API 集成

### 4.1 现有 `/admin/channels` 已经够用

新通道默认进 `OPT_IN_PROVIDER_IDS`，所以：

- 不出现在"已启用"列表；
- 出现在 `/admin/channels` 返回的 `channels[]` 里（因为它会被 `_parse_enabled` 的最后 fallback 拼到 `ordered_known`）；
- 管理页前端**理论上**不需要改前端代码（用现有的开关即可），但需要在 UI 标识"需手动导入 Cookie"。

### 4.2 `/admin/channels/{channel}/import` 已经够用

照搬 `traesolo` 的"粘贴 Cookie JSON → 解析 → 入库"流程。如果当前 `import` 路由假设了"必须传 `path`"（即扫描本地文件），需要扩展为接受 `body: {raw_cookie: "..."}`。

**前置条件**：在写实现前，先确认 `gateway/server.py` 里的 import 路由能不能接受非文件路径的 payload。如果不能，加一个 `POST /admin/channels/{channel}/import-raw`。

### 4.3 模型表 UI

`/admin/channels/{channel}/models` 默认会用 `provider.list_models()` 拉模型。新通道给静态白名单即可，**不用自定义模型表**。

---

## 5. 协议层改动（极小）

### 5.1 `providers/protocol.py`

```python
ChannelId = Literal[
    "workbuddy", "qclaw", "qwenwork", "qoderwork",
    "traework", "traesolo", "gmi",
    "webdeepseek",        # ← 新增
    # "webchatgpt",       # ← 暂不启用
]
```

```python
KNOWN_CHANNEL_IDS: tuple[ChannelId, ...] = (
    "workbuddy", "qclaw", "qwenwork", "qoderwork",
    "traework", "traesolo", "gmi",
    "webdeepseek",        # ← 新增
)
```

`Provider` Protocol **不需要改**（已有的方法都够用）。

### 5.2 `providers/__init__.py`

```python
from providers.webdeepseek import PROVIDER as WEBDEEPSEEK_PROVIDER   # ← 新增

OPT_IN_PROVIDER_IDS: tuple[str, ...] = ("gmi", "webdeepseek")          # ← 新增

_LOADED: dict[str, Provider] = {
    ...,
    "webdeepseek": WEBDEEPSEEK_PROVIDER,                               # ← 新增
}
```

**不需要**改 `DEFAULT_PROVIDER_IDS`。

---

## 6. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| DeepSeek 改签名/路径 → 通道全面失效 | 中（季度级） | 高 | 把 SSE 解析层（`sse.py`）单独抽出来，方便快速 patch |
| Cookie 被风控 → 账号不可用 | 中 | 中 | 检测到 challenge/HTML 响应时返回清晰错误；用户重新登录即可 |
| ToS 变更 → 全面禁止 | 低（年级别） | 高 | 默认关闭、文档明示"仅供学习/自用"、不做公网部署 |
| 客户端拿 Cookie 做坏事 | 低 | 高 | 在 README 与 UI 提示"勿上传 Cookie 到网络" |
| 用户粘贴错站点的 Cookie | 中 | 低 | `domain` 白名单校验；键名校验 |
| 私有信息泄露（Cookie 等于登录态） | 低 | 极高 | 数据库仍走本地 SQLite；README 警告不要公开 buddy2api.db |
| SSE 中途崩溃导致用户体验差 | 中 | 低 | 错误时尽量给一个 finish_reason=length 的部分响应 |

**不建议做的方案（明确记录在此）**：

- ❌ **爬虫式长轮询/无浏览器抓取**：DeepSeek 现在有 bot detection，headless 都过不了；
- ❌ **多账号共享同一 Cookie 池（多用户轮询一个 Cookie）**：触发风控关联检测，**所有账号一起死**；
- ❌ **把网页反向代理做成"通用通道"接受任意 HTTP 请求**：变相把 Buddy2api 变成 HTTP 代理，绕过所有现有鉴权；
- ❌ **做 `webdoubao` 通道**：字节风控极严，签名变动频繁，公开项目几乎全部失效；除非用户单独要求否则不开。

---

## 7. 测试策略

### 7.1 单测（不依赖真实 Cookie）

`tests/providers/webdeepseek/`：

- `test_alias_map.py`：固定映射表正确；
- `test_constants.py`：白名单/必需键名；
- `test_sse_parser.py`：喂入 DeepSeek 历史 capture 的 SSE 字符串，断言解析为 OpenAI chunk 列表；
- `test_store_parse.py`：合法/非法 Cookie JSON 入参。
- `test_payload_translate.py`：OpenAI messages → DeepSeek 内部格式。

### 7.2 集成测试（需要真实 Cookie，标 `@pytest.mark.integration`）

- `test_chat_live.py`：用 fixture 中的 Cookie 发一条真请求，断言 200 + 至少 1 个 chunk；
- 默认 `pytest -m "not integration"` 跳过；用户跑全量时手动 `pytest -m integration`。

### 7.3 灰度发布

1. 在自己机器用 `CB_GATEWAY_PROVIDERS=...`（或纯 UI 启用）启用 `webdeepseek`，先单跑一周；
2. 在 `docs/maintenance/` 下加 `webdeepseek-troubleshoot.md`，记录已知错误模式；
3. 上游一旦变更，**先 patch `sse.py`，再发 release**。

---

## 8. 落地清单（动手前请勾选）

> 这一节是给"决定动手写代码"时用的。**当前 PR 不实现，只勾选规划。**

- [ ] 决策：本次实现 `webdeepseek` 通道（`webchatgpt` 暂缓，`webdoubao` 不做）
- [ ] 决策：手动粘贴 Cookie，不做 Chrome 本地 DB 读取
- [ ] 决策：不依赖环境变量开关，完全走 `OPT_IN_PROVIDER_IDS` + 管理页 `enabled_channels`
- [ ] 决策：先出 `webdeepseek` 一个通道的最小实现，跑稳 2~4 周后再开 `webchatgpt`
- [ ] 阅读参考实现：`providers/traesolo/`（凭证 JSON 模式）和 `providers/gmi/`（Provider 协议样板）
- [ ] 抓一份 DeepSeek 网页端的真实 SSE capture 作为 `test_sse_parser.py` 的 fixture
- [ ] 写 `providers/webdeepseek/constants.py`（模型白名单、Cookie 必需键）
- [ ] 写 `providers/webdeepseek/store.py`（凭证解析）
- [ ] 写 `providers/webdeepseek/sse.py`（事件解析）
- [ ] 写 `providers/webdeepseek/chat.py`（请求拼装 + 流式响应）
- [ ] 写 `providers/webdeepseek/__init__.py`（Provider 适配）
- [ ] 改 `providers/protocol.py` 加 `"webdeepseek"`
- [ ] 改 `providers/__init__.py` 注册 + 加 `OPT_IN_PROVIDER_IDS`
- [ ] 改 `gateway/server.py` 的 import 路由（如需支持 raw cookie）
- [ ] 写 `tests/providers/webdeepseek/` 单测
- [ ] 在 `docs/maintenance/` 加 `webdeepseek-troubleshoot.md`
- [ ] 在 `README.md` 通道表加一行
- [ ] 灰度自测一周后再考虑 `webchatgpt`

---

## 9. 不写实现的理由

> 写到这里特意停下来，再说一下：**为什么这份文档不附代码。**

1. **网页反向代理是"软目标"工程**：签名/路径/UA/风控规则每月都可能变；写死的实现跑起来好看，**真正能用的代码必须能 patch**，不该当 PR 一锤子交付。
2. **本项目的价值是"统一鉴权 + 多通道 + 配额可视化"**：真正长寿命的是协议层（Provider Protocol + `accounts` 表 + 管理页）。**网页反向代理的具体实现应当被当成"插件"对待**，先有文档、再有 fixture、再有 patch。
3. **用户的真实需求是"统一接口"**：现有 5 通道的模型清单里大概率已经覆盖了你想要的"豆包 / DeepSeek"等价物（GLM-4.6 / Doubao-1.5 / Qwen-3-Max / DeepSeek-V3）。**在动手爬网页之前，先去 `/v1/models` 看一眼现有通道的 `auto` 默认模型是什么**，可能根本不需要新增通道。

---

## 附录 A：参考实现（其他项目，不在本仓库）

- `chat2api`：DeepSeek 网页 → OpenAI 兼容（已停更）
- `gpt2api` / `chatgpt-api-server`：ChatGPT 网页 → OpenAI 兼容
- `doubao2openai`：豆包 → OpenAI 兼容（已失效）

注意这些项目的**共同特征**：维护者声明"仅供学习，请勿用于商业用途"，且最近一次 commit 通常在 3~6 个月前。**这本身就是本设计选择"小步、隔离、可 patch"的根据。**