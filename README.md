# Buddy2api 2.2

[English](README_EN.md) | 中文

> 把本机已经登录的消费级 AI 客户端，接成 OpenAI 兼容接口，给 Codex、OpenCode、Cherry Studio、NextChat 等用。默认打开 Work Buddy / CodeBuddy、QClaw、千问办公（QwenWork）、TraeWork、Trae SOLO 五个通道；GMI 与阿里百炼（Bailian）是 opt-in 通道，需要在 `CB_GATEWAY_PROVIDERS` 里启用。管理页下拉选其中一个，一次请求只走一个通道。

当前版本 **2.2.0**。这个项目只适合本机自用，不要公开部署，也不要把登录凭据、API Key、数据库文件发给别人。v2.2 重点变化：管理页不再依赖 CDN（Vue 与 Sortable 全部本地 vendor 化，局域网也能打开）；后端三个巨石模块（`storage/database.py`、`gateway/server.py`、`upstream/proxy.py`）按域拆分；新增 GMI opt-in 通道，阿里百炼（Bailian）opt-in 通道随之上线。完整更新见《v2.2 更新内容》。

## 这是什么？

Buddy2api 在本机提供 `http://127.0.0.1:8787/v1`。你在官方客户端里登录并且还有额度，这个网关把本机登录导入进来，把请求转到对应厂商。普通客户端走 Chat Completions；Codex 走 `/v1/responses`，管理页把 Key 类型选成 Codex 时会做一轮内容清洗。

五个通道默认都开，GMI 和 Bailian 默认关（opt-in）。没装、没登录的通道，通道管理页检测为空，不会自动入库。Trae SOLO 不走本机登录目录，走管理页的「Web 登录」或粘贴回调 URL（见下）。GMI 与 Bailian 不读本机登录目录，靠管理页导入 API Key。

```powershell
python -m src.gateway.server
```

| 通道 | 默认 | 本机登录位置 |
|---|---|---|
| WorkBuddy / CodeBuddy | 开 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | 开 | `%APPDATA%\QClaw` |
| 千问办公 QwenWork | 开 | `%APPDATA%\QwenWorkCN` |
| TraeWork | 开 | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| Trae SOLO | 开 | 无（Web 登录回环 / 凭证 JSON 导入） |
| GMI | 关（opt-in） | Web 配置：通道管理页选 GMI 通道后粘 API Key 即可 |
| Bailian | 关(opt-in) | Web 配置：通道管理页选 Bailian 通道后粘贴 API Key 即可 |

路径不对时可用 `CB_AUTH_DIR`、`CB_QCLAW_AUTH_DIR`、`CB_QWENWORK_AUTH_DIR`、`CB_TRAEWORK_AUTH_DIR` 指定。四个通道的登录文件不要混在同一个目录里。Trae SOLO 的凭证 JSON 可用 `CB_TRAESOLO_AUTH_DIR` 指定扫描目录（可选）。GMI 不读本机登录目录，靠管理页导入 API Key。

## 注意事项

按下面的《安装与启动》即可。这几条是 2.0 里最容易踩空的：

1. **启动后通道管理页是空的，这是正常的。** 默认不再自动入库。到「通道管理」页：选通道 → 重新检测 → 一键导入。四个本地通道都能选；**Trae SOLO 选完后点「发起网页登录」**，在新窗口完成 TRAE 登录，浏览器会自动跳回服务完成入库（远程够不到回调时，把地址栏完整 URL 粘贴到「手动完成」）。
2. **一把 API Key 只打一个通道。** 创建时必须选通道。WorkBuddy 的 Key 可 `auto` / `glm-5.2`；QwenWork 的 Key 可 `auto` 或 `qwork-advanced`；TraeWork 的 Key 可 `auto` 或 `qwen-3.7-plus`；Trae SOLO 的 Key 可 `auto` 或 `glm-5.2`（SOLO 模型表较大，`/v1/models` 里以 `traesolo/` 前缀列出）。通道和模型对不上会 400 或 403，不会帮你转到另一家。
3. **某个通道返回 503 `channel_unavailable`：** 这个通道还没导入可用账号。
4. **QClaw / QwenWork 请在 Windows 上直接跑 `python -m src.gateway.server`。** Linux Docker 读不了这两家用了 DPAPI 加密的本机文件；管理页会写明这一点。WorkBuddy 可以继续用 Docker。
5. 本项目和聊天客户端最好在同一台电脑。客户端如果跑在 Docker 里，Base URL 填 `http://host.docker.internal:8787/v1`，不要填容器自己的 `127.0.0.1`。

## 安装与启动

还没装环境时按这几步走。已经有虚拟环境的，装完 `ops/requirements/base.txt` 后执行 `python -m src.gateway.server` 即可。

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
python -m src.gateway.server
```

看到监听信息后，浏览器打开：

```text
http://127.0.0.1:8787
```

停止服务：回到终端按 `Ctrl+C`。下次开机后：

```powershell
cd <你的项目路径>\Buddy2api
conda activate buddy2api
python -m src.gateway.server
```

提示符前面应出现 `(buddy2api)`，再执行 `python -m pip`，避免装到系统 Python。

### 其他启动方式

- **脚本：** Windows 安装 Python 时勾选 Add Python to PATH，在项目目录执行 `.\ops\start.bat`。Linux / macOS：`chmod +x ops/start.sh && ./ops/start.sh`。脚本优先用名为 `buddy2api` 的 Conda 环境，没有 Conda 才建 `.venv`。
- **Docker：** `powershell -ExecutionPolicy Bypass -File .\ops\start-docker-win.ps1`。本机没有 WorkBuddy 登录目录时脚本仍会启动。容器下拉里仍七个通道都可见（GMI / Bailian 为 opt-in，需在 `CB_GATEWAY_PROVIDERS` 里启用），但 QClaw / QwenWork 请用上面的 `python -m src.gateway.server`。TraeWork 登录文件不是 DPAPI，本机 `python -m src.gateway.server` 导入后 Docker 也能用库里的 token。Trae SOLO 不读本机目录，登录闭环与 token 都在库里，容器内同样可用。GMI 与 Bailian 走 Web 导入，容器内也直接可用。

### 第一次打开网页之后

管理页不再自动发 Cookie。第一次打开网页后，到「设置」把启动日志里的 Admin Token 粘进「管理页登录」保存一次，之后浏览器凭 HttpOnly Cookie 访问。

1. 打开「账号」。下拉里选 WorkBuddy / QClaw / 千问办公 / TraeWork，点「重新检测」，再点「一键导入本机登录」。**Trae SOLO** 时改用「发起网页登录」：新窗口完成 TRAE 登录后自动跳回入库；远程够不到 `127.0.0.1` 回调时，把浏览器地址栏的完整 URL 粘进「手动完成」。
2. 点该账号的「测试」，能返回一句话就说明这条通道通了。
3. 打开「API Keys」，**先选同一个通道**再创建。给 Codex 用时 Key 类型选 Codex，接口用 `/v1/responses`。创建后可以再显示、复制完整 Key。
4. 在客户端里填：
   - Base URL：`http://127.0.0.1:8787/v1`
   - API Key：刚复制的 Key
   - 模型：WorkBuddy 用 `auto` 即可；QClaw 用 `auto`；千问办公用 `auto` 或 `qwork-advanced`；TraeWork 用 `auto` 或 `qwen-3.7-plus`；Trae SOLO 用 `auto` 或 `glm-5.2`（`auto` 在 SOLO 上落到 `glm-5.2`）。

管理页打不开或要远程访问时：

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-请换成足够长的随机值"
python -m src.gateway.server
```

### 更新

按 `Ctrl+C` 停掉正在跑的服务：

```powershell
cd <你的项目路径>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r ops/requirements/base.txt
python -m src.gateway.server
```

## 常见问题

- `git` 或 `conda` 不是内部命令：终端窗口里没继承 PATH，关掉终端重开；Conda 用户改用开始菜单里的 Miniconda Prompt，而不是普通 PowerShell。
- `No module named ...`：依赖没装到当前 Python。先 `conda activate buddy2api`（或启用对应虚拟环境），再 `python -m pip install -r ops/requirements/base.txt`，最后再 `python -m src.gateway.server`。
- 下载依赖很慢：先确认能访问 PyPI；不要同时混用好几个 Python（系统 Python、conda 环境、`.venv` 之间会抢包）。
- 端口 8787 被占用：先关掉旧的 Buddy2api 进程，再启动新的；或换端口 `python -m src.gateway.server --port 8788`。
- 网页里一个账号都没有：还没导入过。先到「账号」页选对通道再点「重新检测」；登录目录不对就设 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`，然后再次检测。
- 创建 Key 失败：创建表单里没选通道——v2.0 起每把 Key 必绑定一个通道。
- 客户端 503 `channel_unavailable`：这把 Key 绑定的通道还没有可用账号，或账号都过期了，到「账号」页先做一次 checkin / refresh。
- 客户端 403 `key_channel_mismatch`：请求里的模型带了别的通道前缀（比如 `qwenwork/...`），和这把 Key 绑定的通道不一致；要么换 Key，要么改用这把 Key 对应通道的模型 id。
- 客户端 400 `unknown_model`：请求里的模型不属于这把 Key 的通道。换 Key，或改成该通道认识的 id（`/v1/models` 里能看到每个通道的可用模型）。

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

QwenWork、QClaw、TraeWork、Trae SOLO 各用自己那把 Key，不要混用。注意 `glm-5.2` 在 WorkBuddy 和 Trae SOLO 两个通道都存在：不带前缀时按 Key 绑定的通道解析，想明确走 SOLO 就用 `traesolo/glm-5.2`。

### 按通道配置模型列表

各通道的模型列表 / 别名可通过管理 API 配置（改完立即生效，无需重启）；不配置时用内置默认。

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

网页管理页「模型配置」页提供图形界面：「统一模型」宽表（一行一个统一模型、每列一平台，
格子填内部名、留空 = 该平台没有）+「各平台设置」可切换列表（每平台的白名单与别名）。

## 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址，本机用保持这个值 |
| `--port` | `8787` | 端口 |
| `--admin-token` | 自动生成（启动日志打印一次） | 管理 Token；在管理页「设置」粘贴一次即可拿到 Cookie |
| `--no-admin-auth` | 关 | 关掉管理鉴权，只适合本机临时试 |
| `--config` | 读 `config.toml` 的 `[default]` 块；带路径则当作 TOML 文件路径；不带路径则当作 profile 名（参见 [配置文件](#配置文件)） |
| `--config-name` | `default` | TOML 文件内要加载的 profile 表名（`[dev]` / `[prod]` 等） |

## 配置文件

`config.toml` 放在项目根目录，启动时被 `gateway.server` 自动加载。适合"我不想每次记一墙 CLI 参数 / 环境变量"的场景：把 `host` `port` `database.path` `admin.token` 写进文件，bare `python -m src.gateway.server` 就能直接用。

**优先级**（later wins）：

```
代码默认值  →  config.toml [default]  →  config.toml [<profile>]  →  环境变量  →  CLI 参数
```

**两种 profile 加载方式**：

```bash
# 1) profile 写在同一个 config.toml 里
python -m src.gateway.server                          # 用 [default] 块
python -m src.gateway.server --config prod            # 用 [prod] 块
CB_GATEWAY_CONFIG=prod python -m src.gateway.server   # 同上，但通过环境变量

# 2) profile 在独立文件里
python -m src.gateway.server --config config.prod.toml
```

**完整示例**（dev / prod 共享同一份代码、各自一份配置）：

```toml
# config.toml · dev checkout 默认用 8787
[default.server]
host = "127.0.0.1"
port = 8787

[dev.server]
host = "127.0.0.1"
port = 8787
```

```toml
# config.prod.toml · prod checkout 用 8788、自己的 data 目录
[default.server]
host = "127.0.0.1"
port = 8788

[default.database]
path = "/var/lib/buddy2api/codebuddy_gateway.db"

[default.admin]
# 留空就自动生成；填了就固定下来（浏览器 Cookie 跨重启有效）
# token = "cb-admin-xxxxxxxxxxxxxxxxxxxxxxxx"
```

`config.toml` 和 `config.*.toml` 都被 `.gitignore` 排除（per-deploy 配置不进 git），跟踪的只有 `config.example.toml` 模板。

**同时跑 dev + prod**：两个 checkout 各写一份 `config.toml`，端口和 db 路径必须错开（否则 WAL 锁会冲突）：

| checkout | config.toml 端口 | config.toml db 路径 |
|---|---|---|
| `Buddy2api/`（dev） | 8787 | `data/codebuddy_gateway.db`（默认） |
| `Buddy2api-prod/`（worktree 跑实例） | 8788 | `data/codebuddy_gateway.db`（相对 prod 自己的 cwd） |

固定 admin token：编辑 `config.toml` 的 `admin.token = "cb-admin-xxx"`。生成一个：`python -c "import secrets; print('cb-admin-' + secrets.token_urlsafe(24))"`。

## 环境变量

> 全部可选，都有合理的默认值；绝大多数场合**什么都不用设**。变量按用途分组，单说明里括号内为该变量的默认值，`*` 表示只在特殊场合用。

### 核心 / 启动
| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 启用哪些通道，逗号分隔。默认 `workbuddy,qclaw,qwenwork,traework,traesolo`。GMI 与 Bailian 是 opt-in，启用加在末尾：`workbuddy,qclaw,qwenwork,traework,traesolo,gmi` 或 `...,bailian` |
| `CB_BAILIAN_API_KEY` | 阿里百炼 API Key（opt-in 通道：通道管理页粘贴或此环境变量导入；无活跃账号时自动导入） |
| `CB_GATEWAY_AUTO_IMPORT` | 设 `1` 则启动时自动扫描导入账号。默认 `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 一键领取时相邻账号的间隔毫秒（防风控，不可设太小）。默认 `800` |
| `CB_GATEWAY_ADMIN_TOKEN` | 固定管理 Token。默认自动生成（启动日志打印一次，管理页「设置」粘贴一次即可拿 Cookie） |
| `CB_GATEWAY_DB_PATH` | 数据库文件路径。默认项目下 `data/` 里 |
| `CB_GATEWAY_MASTER_KEY` | 跨系统搬数据库时手动指定的加密主密钥。默认每实例自动生成（换机器或删 data 会失效，需迁移时用） |
| `CB_GATEWAY_CREDENTIAL_KEY_FILE` * | 读取加密主密钥的文件路径（Docker 场景注入用）。默认空，即用 `CB_GATEWAY_MASTER_KEY` 或自动生成 |
| `CB_GATEWAY_SECURE_COOKIE` | 设 `1` 强制管理 Cookie 带 Secure（https 或反向代理后）。默认跟随请求协议 |
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

> `CB_HOST_AUTH_DIR` 是 Docker 部署脚本内部使用（挂载的本机 WorkBuddy 目录），`CB_CONTAINER_AUTH_DIR` 是容器内的挂载点（默认 `/auth`），一般不用管。

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
| `CB_GATEWAY_MAX_BODY_BYTES` | 请求体上限字节数。默认 `10MiB` |
| `CB_GATEWAY_USAGE_RATE_LIMIT` | /usage 接口秒级限流，设 `0` 关闭。默认 `30` |
| `CB_GATEWAY_TOOL_STALL_RETRY` | 工具停转时自动用 `tool_choice=required` 重试一次。默认 `1` |
| `CB_GATEWAY_TOOL_STALL_FAIL_STREAM` * | 流式工具停转且重试也失败时，把回合标记为失败而不是返回正文。默认 `0` |

### 推理档位（按模型）

不再用环境变量，改为在管理页「模型配置 → 各平台设置」里**按模型**配置（存数据库，即时生效）：

- 每个模型一个下拉：`默认（不注入）` / `none` / `minimal` / `low` / `medium` / `high` / `max`；另有「通道默认」档位作用于未单独设置的模型。
- 优先级：客户端显式 `reasoning_effort` > 按模型配置 > 通道默认 > 不注入（跟随上游默认）。
- 仅 WorkBuddy 通道上游（`copilot.tencent.com`）确认支持该参数；其它通道在 UI 显示「不适用」。
- 实测原生接受值见 `docs/design/per-model-reasoning-effort.md`。注意：deepseek/glm/auto 默认不推理；选档位会开启推理（会变慢）；想最快可给 DeepSeek 选 `low` 或留空。`off` 上游不接受（11150）。

### content 精简（workbuddy 11128 拦截自愈）
| 变量 | 说明 |
|---|---|
| `CB_GATEWAY_COMPACT_CHARS` | 手动全局开启精简超大请求体，并指定单字段字符阈值。默认 `0`（关闭，走下方的按通道自愈） |
| `CB_GATEWAY_COMPACT_ARMED_CHARS` * | 某通道真触发过一次 11128 后，该通道自动精简的单字段阈值。默认 `3000` |
| `CB_GATEWAY_COMPACT_SYSTEM_CHARS` * | system 消息阈值（纯头部截断，实测其尾部 git/commit 块是 11128 触发源）。默认 `5000` |

> 详见 `docs/workbuddy-11128-troubleshoot.md`：正常请求默认不截断，某通道返回 11128 后自动武装并精简（system 纯头切 5000、超大 content/reasoning 头切、tools 描述精简，结构键与 `tool_calls` 永不切），`/admin/stats` 的 `compaction` 字段可看生效情况。

### 调试
| 变量 | 说明 |
|---|---|
| `CB_DEBUG_DUMP` * | 把 responses 协议的请求 / 响应（脱敏 JSON）dump 到 `upstream/.debug/` 便于排查出站协议。默认关 |
| `CB_DEBUG_DUMP_INCLUDE_CONTENT` * | dump 时连 content 一起写（默认脱敏不写正文）。默认关，仅与 `CB_DEBUG_DUMP` 一起用 |
| `CB_DOCKER` * | 标记运行在 Docker 内（内部判断用）。默认空 |

## Credit 与 Token 统计

各通道的 token / credit 统计行为不一致：

- **WorkBuddy** token 与 credit 都由上游直接报；
- **Trae SOLO / QClaw / QwenWork** token 由上游报、credit 不报；
- **TraeWork** token 与 credit 都不报（SSE 里 `token_usage` 事件被丢）。

自 v2.2.0 起，traesolo/qclaw/qwenwork 三家可启用**网关侧 token→credit 估算**（每通道在
「模型配置 → 各平台设置」里设 `credit_rate`，默认 1000 token / 1 credit）。这是**估算值不是真实扣费**，
只用于看趋势和做内部估算，不要拿它和上游真实余额做差额对账。
TraeWork 想算需要先单独修它的 SSE 解析，未做。详见 `docs/credit-and-token-tracking.md`。

## 数据和安全

- 账号 Token 写入前会加密。Windows 用系统 DPAPI。
- 不要把 `*.db`、登录目录、日志、带 Key 的截图发出去。
- 不要把服务绑到公网。保持 `127.0.0.1`。

## 项目结构

v2.2 把三个巨石模块按域拆分；v2.3 把 6 个源模块统一进 `src/`、`redesign-audit/` 进 `docs/redesign/`。目录布局如下：

```text
Buddy2api/
├─ src/                    # 全部 Python 与前端源
│  ├─ gateway/             # HTTP 入口（FastAPI 应用 + 路由 + 版本号）
│  │  ├─ server.py         # app 工厂、lifespan、StaticFiles 挂载
│  │  ├─ router.py         # 绑定请求到通道、做模型翻译（工具）
│  │  ├─ deps.py           # 共享鉴权依赖
│  │  ├─ routers/
│  │  │  ├─ admin.py        # /admin/* 端点
│  │  │  ├─ v1.py           # /v1/chat/completions、/v1/responses、/v1/models
│  │  │  └─ static_router.py# /admin/meta 等元信息
│  │  └─ version.py
│  ├─ accounts/             # 账号与通道管理
│  │  ├─ auth_manager.py     # 账号选择、token 管理、checkin
│  │  └─ control_plane.py    # 启动扫描、一键领取、模型配置
│  ├─ upstream/             # 上游对接
│  │  ├─ proxy.py           # pipeline 主流程（proxy_chat_completions 等）
│  │  ├─ aliases.py         # 模型别名表、默认模型、推理档位
│  │  ├─ moderation.py      # 内容审核、工具停转检测
│  │  ├─ compaction.py      # 请求体精简、11128 自愈
│  │  └─ responses.py       # OpenAI Responses → Chat Completions 翻译
│  ├─ storage/              # 基础设施层（DB、加密、指纹、缓存）
│  │  ├─ database.py        # 兼容门面（re-export 自 storage.repos）
│  │  ├─ backup.py          # db 快照 / rotation / 凭证同步
│  │  ├─ repos/
│  │  │  ├─ accounts.py     # 账号 CRUD
│  │  │  ├─ api_keys.py     # API Key CRUD
│  │  │  ├─ logs.py         # 请求日志、查询
│  │  │  ├─ settings.py     # 通道配置、KV
│  │  │  ├─ stats.py        # dashboard 聚合
│  │  │  └─ _common.py      # 共享连接 / Schema
│  │  ├─ credit_cache.py    # 各通道 credit 缓存
│  │  ├─ http_pool.py       # 上游 httpx 客户端池
│  │  ├─ credential_crypto.py
│  │  └─ fingerprint.py
│  ├─ providers/            # 通道适配
│  │  ├─ openai_compat.py   # OpenAI 兼容通道基类（单 URL + 单 Key 形态）
│  │  └─ custom_channels.py # 自定义通道定义（settings 键 custom_channels；gmi / bailian 为内置 seed）
│  └─ web/                  # 管理页 UI
│      ├─ index.html
│      ├─ css/app.css
│      ├─ js/
│      │  ├─ app.js         # 入口
│      │  ├─ api.js         # 后台 API 客户端
│      │  ├─ icons.js       # 自绘 SVG 图标
│      │  └─ pages/         # dashboard / accounts / quota / keys / channels / usage / logs / setup / settings
│      └─ vendor/           # Vue 3.4.21 + SortableJS 1.15.6（本地，局域网可用）
├─ docs/                    # 设计与使用文档
│  ├─ *.md                  # credit-and-token-tracking / dashboard-slow-query / provider-model-usage / traesolo-usage / traework-usage / workbuddy-11128 / cache-tracking
│  ├─ design/               # per-model-reasoning-effort 等设计稿
│  ├─ maintenance/          # 维护手册
│  ├─ releases/             # 发布说明
│  └─ redesign/             # v2.2 重构设计文档（00-baseline / 01-audit / 02-strategy / 03-tokens / 04-prod-worktree）
├─ tests/                   # pytest
│  ├─ conftest.py
│  ├─ pytest.ini
│  ├─ test_*.py             # 业务与通道测试
│  └─ test_web_assets.py    # 前端 ESM 解析 + vendor/CDN 守卫（v2.2 新增）
├─ ops/                     # 启动 / 部署 / 构建 / 一次性脚本
│  ├─ start.bat / start.sh             # 本机启动脚本
│  ├─ start-docker-win.ps1 / start-docker-wsl.sh
│  ├─ Dockerfile
│  ├─ docker-compose.yml / docker-compose.windows.yml
│  ├─ docker-entrypoint.sh
│  ├─ requirements/{base.txt, dev.txt}
│  ├─ scripts/backup-db.py             # 手动 db 快照
│  ├─ scripts/copy-dev-to-prod.py      # dev → prod 配置复制
│  └─ scripts/oneoff/                  # 一次性分析与回填脚本（归档；不要 import）
├─ data/                    # 运行时数据（DB + 凭证，gitignore）
├─ config.example.toml      # 配置模板（config.toml 自身被 gitignore）
└─ README.md / README_EN.md / SECURITY.md / LICENSE / .gitignore / .dockerignore / .mailmap
```


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

## v2.2 更新内容

相对 1.4 / 2.0 / 2.1 的主要变化：

- **GMI / Bailian 通道**：内置 seed 通道（可编辑）。二者都是"单 URL + 单 API Key"的 OpenAI 兼容形态，定义存 settings 键 `custom_channels`（首次启动自动 seed，id 不变、老账号与配置无损）；不在默认通道列表里，启用需在 `CB_GATEWAY_PROVIDERS` 末尾追加 `gmi` / `bailian`，或设 `CB_BAILIAN_API_KEY` / 在管理页粘贴 Key。
- **自定义通道**：在管理页「通道管理」点行内「详情」浮窗的「编辑」可直接新增/编辑/删除任意 OpenAI 兼容平台，零代码、热生效；协议实现统一走 `providers/openai_compat.py` 基类。列表中两组各支持 ≡ 拖拽排序（密钥型组固定在登录型组之后）。新增浮窗仅「通道 ID / 显示名 / Base URL / 创建模式 API Key」为必填（`*` 红色）；**模型白名单可选**，留空默认 `["DeepSeek-V4-Flash"]`（保存后可在「模型配置」页调整或用探活拉取）；**环境变量名可选**，留空自动生成 `CB_<通道ID大写>`（显式值仍须匹配 `^CB_[A-Z0-9_]+$`）。编辑模式留空 API Key 表示不轮换旧 Key。
- **管理页 vendor 本地化**：Vue 3.4.21 与 SortableJS 1.15.6 从 jsdelivr CDN 落到 `web/vendor/`，由 FastAPI StaticFiles 直接服务。局域网仍可打开管理页。`tests/test_web_assets.py` 守卫 CDN 引用永不回归。
- **后端三巨石模块拆分**：
  - `storage/database.py` 退化为 re-export 兼容门面，子模块在 `storage/repos/{accounts, api_keys, logs, settings, stats, _common}.py`。
  - `gateway/server.py` 留 app 工厂、lifespan、StaticFiles 挂载；端点按域拆到 `gateway/routers/{admin.py, v1.py, static_router.py}`；共享鉴权依赖收口到 `gateway/deps.py`。
  - `upstream/proxy.py` 留 pipeline 主流程；模型别名、审核、精简、Responses 翻译拆到 `upstream/{aliases.py, moderation.py, compaction.py, responses.py}`。
  - 56 个端点路径、契约、行为全部保持不变；`pytest` 与基线一致（pre-existing 失败不在重构范围）。
- **管理页 Overhaul**：八个 lever（依赖本地化、版本号单一来源、CSS 单一令牌体系重建、组件层重做、图表令牌化、重点页重排、移动端断点收敛、一次性脚本归档）。版本号现在从 `/admin/meta` 拉，不再写死。`em-dash` 全部清理为中文标点。
- **一改一 commits 走完**：每个 lever 一个 commit（`refactor(web): ...` / `refactor(storage): ...` / `refactor(gateway): ...` / `refactor(upstream): ...`），所有 commit 已 push 到 `refactor/web-console-ia`。详细设计见 `docs/redesign/`。
- **配置文件 `config.toml`**：新增。`gateway.server` 启动时自动加载，支持 `[default]` / `[dev]` / `[prod]` profile 与 `--config <profile>` / `CB_GATEWAY_CONFIG=<profile>` 切换。dev / prod 双 checkout 各自一份 `config.toml`（`.gitignore`d，per-deploy 私有），端口与 db 路径已写死，bare `python -m src.gateway.server` 走对的端口。详见 [配置文件](#配置文件) 一节。

## License

MIT