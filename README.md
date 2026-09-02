# Buddy2api 2.2

[English](README_EN.md) | 中文

> 把本机已经登录的消费级 AI 客户端，接成 OpenAI 兼容接口，给 Codex、OpenCode、Cherry Studio、NextChat 等用。默认打开 Work Buddy / CodeBuddy、QClaw、千问办公（QwenWork）、TraeWork、Trae SOLO 五个通道；管理页下拉选其中一个。一次请求只走一个通道。

当前版本 **2.2.0**。这个项目只适合本机自用，不要公开部署，也不要把登录凭据、API Key、数据库文件发给别人。

## 这是什么？

Buddy2api 在本机提供 `http://127.0.0.1:8787/v1`。你在官方客户端里登录并且还有额度，这个网关把本机登录导入进来，把请求转到对应厂商。普通客户端走 Chat Completions；Codex 走 `/v1/responses`，管理页把 Key 类型选成 Codex 时会做一轮内容清洗。

五个通道默认都开。没装、没登录的通道，账号页检测为空，不会自动入库。Trae SOLO 不走本机登录目录，走管理页「Web 登录」或粘贴回调 URL（见下）。

```powershell
python -m gateway.server
```

| 通道 | 默认 | 本机登录位置 |
|---|---|---|
| WorkBuddy / CodeBuddy | 开 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | 开 | `%APPDATA%\QClaw` |
| 千问办公 QwenWork | 开 | `%APPDATA%\QwenWorkCN` |
| TraeWork | 开 | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| Trae SOLO | 开 | 无（Web 登录闭环 / 凭证 JSON 导入） |

路径不对时可用 `CB_AUTH_DIR`、`CB_QCLAW_AUTH_DIR`、`CB_QWENWORK_AUTH_DIR`、`CB_TRAEWORK_AUTH_DIR` 指定。四个通道的登录文件不要混在同一个目录。Trae SOLO 的凭证 JSON 可用 `CB_TRAESOLO_AUTH_DIR` 指定扫描目录（可选）。

## 注意事项

按下面「安装与启动」即可。这几条是 2.0 里最容易踩空的：

1. **启动后账号页是空的，这是正常的。** 默认不再自动入库。到「账号」页：选通道 → 重新检测 → 一键导入。四个本地通道都能选；**Trae SOLO 选完后点「发起网页登录」**，在新窗口完成 TRAE 登录，浏览器会自动跳回服务完成入库（远程够不到回调时，把地址栏完整 URL 粘贴到「手动完成」）。
2. **一把 API Key 只打一个通道。** 创建时必须选通道。WorkBuddy 的 Key 发 `auto` / `glm-5.2`；QwenWork 的 Key 发 `auto` 或 `qwork-advanced`；TraeWork 的 Key 发 `auto` 或 `qwen-3.7-plus`；Trae SOLO 的 Key 发 `auto` 或 `glm-5.2`（SOLO 模型表较大，`/v1/models` 里以 `traesolo/` 前缀列出）。通道和模型对不上会 400 或 403，不会帮你转到另一家。
3. **某个通道返回 503 `channel_unavailable`：** 这个通道还没导入可用账号。
4. **QClaw / QwenWork 请在 Windows 上直接跑 `python -m gateway.server`。** Linux Docker 读不了这两家用 DPAPI 加密的本机文件；管理页会写明这一点。WorkBuddy 可以继续用 Docker。
5. 本项目和聊天客户端最好在同一台电脑。客户端如果跑在 Docker 里，Base URL 填 `http://host.docker.internal:8787/v1`，不要填容器自己的 `127.0.0.1`。

## 安装与启动

还没装环境时按这几步走。已经有虚拟环境的，装完 `ops/requirements/base.txt` 后执行 `python -m gateway.server` 即可。

### 1. 安装工具

1. [Git](https://git-scm.com/downloads)，Windows 保持默认选项
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/)，推荐 Python 3.12
3. 先打开并登录你要用的官方客户端（至少 Work Buddy / CodeBuddy）

装完后**重新打开** PowerShell、Windows Terminal 或 Anaconda Prompt：

```powershell
git --version
conda --version
```

找不到 `conda` 时，用开始菜单里的 **Anaconda Prompt / Miniconda Prompt**。也可以在那里执行 `conda init powershell`，关掉窗口再开。

### 2. 克隆项目

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
Get-ChildItem README.md, ops, gateway
```

后面的命令都要在这个目录里执行。

### 3. 用 Conda 启动（推荐）

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r ops/requirements/base.txt
python -m gateway.server
```

看到监听信息后，浏览器打开：

```text
http://127.0.0.1:8787
```

停止服务：回到终端按 `Ctrl+C`。下次开机后：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python -m gateway.server
```

提示符前面应出现 `(buddy2api)`，再执行 `python -m pip`，避免装到系统 Python。

### 其他启动方式

- **脚本：** Windows 安装 Python 时勾选 Add Python to PATH，在项目目录执行 `.\start.bat`。Linux / macOS：`chmod +x ops/start.sh && ./ops/start.sh`。脚本优先用名为 `buddy2api` 的 Conda 环境，没有 Conda 才建 `.venv`。
- **Docker：** `powershell -ExecutionPolicy Bypass -File .\start-docker-win.ps1`。本机没有 WorkBuddy 登录目录时脚本仍会启动。容器下拉里仍有五个通道，但 QClaw / QwenWork 请用上面的 `python -m gateway.server`。TraeWork 登录文件不是 DPAPI，本机 `python -m gateway.server` 导入后 Docker 也能用库里的 token。Trae SOLO 不读本机目录，登录闭环与 token 都在库里，容器内同样可用。

### 第一次打开网页之后

管理页不再自动发 Cookie。第一次打开网页后，到「设置」把启动日志里的 Admin Token 粘进「管理页登录」保存一次，之后浏览器凭 HttpOnly Cookie 访问。

1. 打开「账号」。下拉里选 WorkBuddy / QClaw / 千问办公 / TraeWork，点「重新检测」，再点「一键导入本机登录」。选 **Trae SOLO** 时改用「发起网页登录」：新窗口完成 TRAE 登录后自动跳回入库；远程够不到 `127.0.0.1` 回调时，把浏览器地址栏的完整 URL 粘进「手动完成」。
2. 点该账号的「测试」，能返回一句话就说明这条通道通了。
3. 打开「API Keys」，**先选同一个通道**再创建。给 Codex 用时 Key 类型选 Codex，接口用 `/v1/responses`。创建后可以再显示、复制完整 Key。
4. 在客户端里填：
   - Base URL：`http://127.0.0.1:8787/v1`
   - API Key：刚复制的 Key
   - 模型：WorkBuddy 用 `auto` 即可；QClaw 用 `auto`；千问办公用 `auto` 或 `qwork-advanced`；TraeWork 用 `auto` 或 `qwen-3.7-plus`；Trae SOLO 用 `auto` 或 `glm-5.2`（`auto` 在 SOLO 上落到 `glm-5.2`）

管理页打不开或要远程访问时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
python -m gateway.server
```

### 更新

先 `Ctrl+C` 停掉正在跑的服务：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r ops/requirements/base.txt
python -m gateway.server
```

## 常见问题

- `git` 或 `conda` 不是内部命令：关掉终端重开；Conda 用户改用 Miniconda Prompt。
- `No module named ...`：先 `conda activate buddy2api`，再 `python -m pip install -r ops/requirements/base.txt`。
- 下载依赖很慢：确认能访问 PyPI，不要混用好几个 Python。
- 端口 8787 被占用：关掉旧的 Buddy2api，或 `python -m gateway.server --port 8788`。
- 网页里一个账号都没有：还没导入。选对通道再检测；登录目录不对就设 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`。
- 创建 Key 失败：没选通道。
- 客户端 503 `channel_unavailable`：这个 Key 绑定的通道还没有可用账号。
- 客户端 403 `key_channel_mismatch`：模型带了别的通道前缀，和当前 Key 不一致。
- 客户端 400 `unknown_model`：模型不属于这把 Key 的通道。换 Key，或改成该通道认识的 id。

## 从 1.4.x 升级

启动时会自动改数据库。旧 Key 视为绑在 `workbuddy` 上，原来的 `auto` / `glm-5.2` 还能用。

和 1.4 不同的地方：启动不再自动导入账号；空仓是 503 而不是普通 `server_error`；新建 Key 必须选通道；官方余额只显示积分，不把各厂数字加在一起。

## 客户端接入

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 管理页创建，已绑定通道 |
| 模型 | WorkBuddy：`auto` / `glm-5.2`。QClaw：`auto` 或 `qclaw/default`。QwenWork：`auto` 或 `qwork-advanced`。TraeWork：`auto` 或 `qwen-3.7-plus`。Trae SOLO：`auto` / `glm-5.2` / `traesolo/...`（完整列表见 `/v1/models`） |
| Stream | 建议开 |

接口：`/v1/chat/completions`、`/v1/responses`、`/v1/models`。没加前缀的 `auto` 走这把 Key 绑定的通道。Codex 用 Responses 接口；管理页选 Codex 类型的 Key 会按 Codex 特征 prompt 做清洗（其它客户端借用这把 Key、但没有 Codex 特征时不改写）。

OpenCode 示例（WorkBuddy Key）：

```json
{
  "provider": {
    "workbuddy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-你的key"
      },
      "models": {
        "auto": { "name": "WorkBuddy Auto" },
        "glm-5.2": { "name": "GLM-5.2" }
      }
    }
  }
}
```

```powershell
opencode run -m workbuddy/auto "你好"
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-你的key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

QwenWork、QClaw、TraeWork、Trae SOLO 各用自己那把 Key，不要混用。注意 `glm-5.2` 在 WorkBuddy 和 Trae SOLO 两个通道都存在：不带前缀时按 Key 绑定的通道解析，想明确指 SOLO 就用 `traesolo/glm-5.2`。

### 按通道配置模型列表

各通道的模型列表/别名可通过管理 API 配置（改完立即生效，无需重启）；不配置时用内置默认。

```bash
# 查看（含生效值、内置默认、是否自定义）
curl -H "Authorization: Bearer <admin-token>" http://127.0.0.1:8787/admin/channels/traework/models

# 修改（models 整体替换；null 重置为默认）
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/channels/traework/models \
  -d '{"models":["qwen-3.7-plus","glm-5"],"aliases":{"auto":"qwen-3.7-plus"}}'
```

规则：`models` 为非空字符串数组（或 `{"id": "..."}` 对象），`aliases` 为 `别名 -> 模型id` 的非空对象；
一次请求至少传一项。自定义列表是白名单，不在列表内的模型对该通道 400（QClaw 的 `pool-*` 前缀除外）。
WorkBuddy 兼容历史设置键 `models` / `model_aliases`；其它通道存 `<channel>.models` / `<channel>.aliases`。

### 统一模型（跨平台翻译层）

同一个模型在不同平台名字不一样时，定义一次统一模型（统一名以 WorkBuddy 命名为准），
客户端只请求统一名，网关按 Key 绑定平台翻译成该平台内部名；之后照旧走白名单校验
（内部名不在白名单仍 400，统一模型不自动进白名单）。

```bash
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/unified-models \
  -d '{"models":[{"name":"deepseek-v4-flash","mappings":{"traework":"DeepSeek-V4-Flash-Official","workbuddy":"deepseek-v4-flash"}}]}'
```

网页管理页「模型配置」页提供图形界面：「统一模型」宽表（一行一个统一模型、每列一个平台，
格子填内部名、留空 = 该平台没有）+「各平台设置」可切换列表（每平台的白名单与别名）。

## 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址，本机用保持这个值 |
| `--port` | `8787` | 端口 |
| `--admin-token` | 自动生成（启动日志打印一次） | 管理 Token；在管理页「设置」粘贴一次即可拿到 Cookie |
| `--no-admin-auth` | 关 | 关掉管理鉴权，只适合本机临时试 |

## 环境变量

> 全部可选，都有合理的默认值；绝大多数场合**什么都不用设**。变量按用途分组，单说明里括号内为该变量的默认值，`*` 表示只在特殊场合用。

### 核心 / 启动
| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 启用哪些通道，逗号分隔。默认 `workbuddy,qclaw,qwenwork,traework,traesolo`。只想留一家时改 |
| `CB_GATEWAY_AUTO_IMPORT` | 设 `1` 则启动时自动扫描导入账号。默认 `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 一键领取时相邻账号的间隔毫秒（防风控，不可设太小）。默认 `800` |
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理 Token。默认自动生成（启动日志打印一次，管理页「设置」粘贴一次即可拿 Cookie） |
| `CB_GATEWAY_DB_PATH` | 数据库文件路径。默认项目下 `data/` 内 |
| `CB_GATEWAY_MASTER_KEY` | 跨系统搬数据库时手动指定的加密主密钥。默认每实例自动生成（换机器或删 data 会失效，需迁移时用） |
| `CB_GATEWAY_CREDENTIAL_KEY_FILE` * | 读取加密主密钥的文件路径（Docker 场景注入用）。默认空，即用 `CB_GATEWAY_MASTER_KEY` 或自动生成 |
| `CB_GATEWAY_SECURE_COOKIE` | 设 `1` 强制管理 Cookie 走 Secure（https 或反向代理后）。默认跟随请求协议 |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | 日志保留天数。默认 `90` |

### 各通道登录目录
| 通道 | 变量 | 说明 |
|---|---|---|
| WorkBuddy | `CB_AUTH_DIR` | 本机登录目录 |
| QClaw | `CB_QCLAW_AUTH_DIR` | 本机登录目录 |
| QwenWork | `CB_QWENWORK_AUTH_DIR` | 本机登录目录 |
| TraeWork | `CB_TRAEWORK_AUTH_DIR` | `storage.json` 所在目录 |
| Trae SOLO | `CB_TRAESOLO_CALLBACK_BASE` | 登录回调基地址（远程部署时指向能从外网访问服务的地址，默认用请求自身地址） |
| Trae SOLO | `CB_TRAESOLO_AUTH_DIR` * | 凭证 JSON 扫描目录（可选；该通道默认不扫目录，走 Web 登录） |

> `CB_HOST_AUTH_DIR` 仅 Docker 部署脚本内部使用（挂载的本机 WorkBuddy 目录），`CB_CONTAINER_AUTH_DIR` 是容器内的挂载点（默认 `/auth`），一般不用管。

### WorkBuddy 出站指纹（User-Agent / 版本头）
| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_USER_AGENT` | 整体覆盖整个 User-Agent。默认 `CLI/2.109.2 CodeBuddy/2.109.2`，设 `codebuddy2openai/2.0` 可回退历史 UA。只影响 WorkBuddy 出站 |
| `CB_GATEWAY_IDE_VERSION` | CLI 版本号，驱动 UA 与 X-IDE-Version。默认 `2.109.2` |
| `CB_GATEWAY_STAINLESS_OS` * | 上报的操作系统字符串。默认按当前平台推断 |
| `CB_GATEWAY_STAINLESS_PACKAGE_VERSION` * | `stainless` 包版本。默认 `5.10.1` |
| `CB_GATEWAY_NODE_VERSION` * | Node 运行时版本。默认 `v22.13.1` |

### 请求 / 风险控制
| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_CORS_ORIGINS` | 允许的 CORS 来源，逗号分隔。默认 `http://127.0.0.1:8787,http://localhost:8787` |
| `CB_GATEWAY_ALLOW_UNAUTHENTICATED_API` | 设 `1` 允许无 API key 请求（只适合本机临时测）。默认 `0` |
| `CB_GATEWAY_MAX_BODY_BYTES` | 请求体上限字节。默认 `10MiB` |
| `CB_GATEWAY_USAGE_RATE_LIMIT` | /usage 接口秒级限流，设 `0` 关闭。默认 `30` |
| `CB_GATEWAY_TOOL_STALL_RETRY` | 工具停转时自动用 `tool_choice=required` 重试一次。默认 `1` |
| `CB_GATEWAY_TOOL_STALL_FAIL_STREAM` * | 流式工具停转且重试也失败时，把回合标记为失败而不是返回正文。默认 `0` |

### 思考档位（按模型）

不再用环境变量，改为在管理页「通道与模型 → 各平台设置」里**按模型**配置（存数据库，即时生效）：

- 每个模型一个下拉：`默认（不注入）` / `none` / `minimal` / `low` / `medium` / `high` / `max`；另有「通道默认」档位作用于未单独设置的模型。
- 优先级：客户端显式 `reasoning_effort` > 按模型配置 > 通道默认 > 不注入（跟随上游默认）。
- 仅 WorkBuddy 通道上游（`copilot.tencent.com`）确认支持该参数；其它通道在 UI 显示「—」。
- 实测原生接受值见 `docs/design/per-model-reasoning-effort.md`。注意：deepseek/glm/auto 默认不思考、选档位=开启思考（会变慢）；想最快可给 DeepSeek 选 `low` 或留空。`off` 上游不接受（11150）。

### content 精简（workbuddy 11128 拦截自愈）
| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_COMPACT_CHARS` | 手动全局开启精简超大请求体，并指定单字段字符阈值。默认 `0`（关闭，走下方的按通道自愈） |
| `CB_GATEWAY_COMPACT_ARMED_CHARS` * | 某通道真触发过一次 11128 后，该通道自动精简的单字段阈值。默认 `3000` |
| `CB_GATEWAY_COMPACT_SYSTEM_CHARS` * | system 消息阈值（纯头部截断，实测其尾部 git/commit 块是 11128 触发源）。默认 `5000` |

> 详见 `docs/workbuddy-11128-troubleshoot.md`：正常请求默认不截断，某通道返回 11128 后自动武装并精简（system 纯头切 5000、超大 content/reasoning 头切、tools 描述精简，结构键与 `tool_calls` 永不截），`/admin/stats` 的 `compaction` 字段可看生效情况。

### 调试
| 变量 | 说明 |
|---|---|
| `CB_DEBUG_DUMP` * | 把 responses 协议的请求/响应（脱敏 JSON）dump 到 `upstream/.debug/` 便于排查出站协议。默认关 |
| `CB_DEBUG_DUMP_INCLUDE_CONTENT` * | dump 时连 content 一起写（默认脱敏不含正文）。默认关，仅随 `CB_DEBUG_DUMP` 一起用 |
| `CB_DOCKER` * | 标记运行在 Docker 内（内部判断用）。默认空 |

## Credit 与 Token 统计

各通道的 token / credit 统计行为不一致：

- **WorkBuddy** token 与 credit 都由上游直接报；
- **Trae SOLO / QClaw / QwenWork** token 由上游报、credit 不报；
- **TraeWork** token 与 credit 都不报（SSE 里 `token_usage` 事件被丢）。

从 v2.2.0 起，traesolo/qclaw/qwenwork 三家可启用**网关侧 token→credit 估算**（每通道在
「模型配置 → 各平台设置」里设 `credit_rate`，默认 1000 token / 1 credit）。这是**估算值不是真实扣费**，
只用于看趋势和做内部估算，不要拿它和上游真实余额做差额对账。
TraeWork 想算需要先单独修它的 SSE 解析，未做。详见 `docs/credit-and-token-tracking.md`。

## 数据和安全

- 账号 Token 写入前会加密。Windows 用系统 DPAPI。
- 不要把 `*.db`、登录目录、日志、带 Key 的截图发出去。
- 不要把服务绑到公网。保持 `127.0.0.1`。

## 项目结构

按职责把核心 Python 代码分成三个包，根目录只剩入口/部署/文档：

```text
Buddy2api/
├── gateway/                # HTTP 入口（FastAPI 应用 + 路由 + 版本号）
│   ├── server.py           # 所有 @app.get / @app.post 端点
│   ├── router.py           # 绑定请求到通道、做模型翻译
│   └── version.py
├── accounts/               # 账号与通道管理
│   ├── auth_manager.py     # 账号选择、token 管理、checkin
│   └── control_plane.py    # 启动扫描、一键领取、模型配置
├── upstream/               # 上游对接
│   ├── proxy.py            # HTTP 上游转发（proxy_chat_completions）
│   └── responses.py        # OpenAI Responses ↔ Chat Completions 翻译
├── storage/                # 基础设施层（DB、加密、指纹）
│   ├── database.py         # SQLite CRUD
│   ├── credential_crypto.py
│   └── fingerprint.py
├── providers/              # 通道适配（workbuddy / qclaw / qwenwork / traework / traesolo）
├── web/                    # 管理页 UI（Vue 3 CDN）
├── docs/                   # 设计与使用文档
├── tests/                  # pytest（含 pytest.ini）
├── ops/                    # 启动 / 部署 / 构建
│   ├── start.bat / start.sh         # 本机启动脚本
│   ├── start-docker-win.ps1 / start-docker-wsl.sh   # Docker 启动包装
│   ├── Dockerfile
│   ├── docker-compose.yml / docker-compose.windows.yml
│   ├── docker-entrypoint.sh
│   └── requirements/
│       ├── base.txt                 # 运行依赖（原 requirements.txt）
│       └── dev.txt                  # 开发/测试依赖（原 requirements-dev.txt）
├── data/                   # 运行时数据（DB + 凭据，.gitignore）
└── README.md / README_EN.md / SECURITY.md / LICENSE / .gitignore / .dockerignore / .mailmap
```

启动方式：`python -m gateway.server`（从根目录）。

启动脚本：

```powershell
# Windows
.\ops\start.bat
# Linux / macOS
chmod +x ops/start.sh && ./ops/start.sh
```

Docker 启动：

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\ops\start-docker-win.ps1
# WSL
./ops/start-docker-wsl.sh
```

## License

MIT
