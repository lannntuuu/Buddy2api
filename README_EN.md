# Buddy2api 2.2

[English](README_EN.md) | [中文](README.md)

> Turn consumer AI clients you're already signed into locally into one OpenAI-compatible API for Codex, OpenCode, Cherry Studio, NextChat, and similar agents. Work Buddy / CodeBuddy, QClaw, QwenWork, TraeWork, and Trae SOLO are enabled by default; GMI is a new opt-in channel in v2.2 and must be listed in `CB_GATEWAY_PROVIDERS` to show up. Pick one in the admin UI dropdown. Each request stays on one channel.

Current release **2.2.0**. This project is for local, personal use only. Do not deploy it on the public internet, and do not send login credentials, API keys, or the database file to anyone. v2.2 highlights: the admin UI no longer loads anything from a CDN (Vue and Sortable are vendored locally, so the console works offline); the three Python monoliths (`storage/database.py`, `gateway/server.py`, `upstream/proxy.py`) are split by domain; a new GMI opt-in channel is available. See "What's new in v2.2" at the bottom for the full list.

## What is this?

Buddy2api serves `http://127.0.0.1:8787/v1` on your machine. You stay signed into the official clients and still have quota; this gateway imports those local sessions and forwards requests to the matching vendor. Normal clients use Chat Completions; Codex uses `/v1/responses`, and when you set the key type to Codex in the admin UI it runs a round of prompt sanitization.

Five channels are on by default and a sixth (GMI) is opt-in. A channel you haven't installed or signed into shows empty on the Accounts page and is not auto-imported. Trae SOLO does not read a local login directory — import it via the admin UI's **Web login** or by pasting a callback URL (see below). GMI doesn't read a local directory either; paste its API key on the Accounts page.

```powershell
python -m gateway.server
```

| Channel | Default | Where logins live |
|---|---|---|
| WorkBuddy / CodeBuddy | on | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | on | `%APPDATA%\QClaw` |
| QwenWork | on | `%APPDATA%\QwenWorkCN` |
| TraeWork | on | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| Trae SOLO | on | none (web login loop / credential JSON import) |

If the paths are wrong you can point them with `CB_AUTH_DIR`, `CB_QCLAW_AUTH_DIR`, `CB_QWENWORK_AUTH_DIR`, `CB_TRAEWORK_AUTH_DIR`. Don't put the four channels' login files in the same directory. Trae SOLO's credential JSON can be scanned from a directory set via `CB_TRAESOLO_AUTH_DIR` (optional).

## Notes

Just follow "Install and run" below. These are the easiest things to trip over in 2.0:

1. **An empty Accounts page right after startup is normal.** It no longer auto-imports by default. Go to the **Accounts** page: pick a channel → Re-detect → Import all. All four local channels are available; **for Trae SOLO click "Start web login"** after selecting it, finish the TRAE login in the new window, and the browser redirects back to the server to complete the import (if the remote side can't reach the callback, paste the full address-bar URL into "Manual complete").
2. **One API key hits exactly one channel.** You must pick a channel when creating it. A WorkBuddy key sends `auto` / `glm-5.2`; a QwenWork key sends `auto` or `qwork-advanced`; a TraeWork key sends `auto` or `qwen-3.7-plus`; a Trae SOLO key sends `auto` or `glm-5.2`; a GMI key sends any model the upstream lists (SOLO's model list is large and surfaced under the `traesolo/` prefix in `/v1/models`). A channel/model mismatch returns 400 or 403; the gateway won't forward you to another vendor.
3. **A channel returns 503 `channel_unavailable`:** that channel has no imported, usable account yet.
4. **Run QClaw / QwenWork with `python -m gateway.server` directly on Windows.** A Linux Docker container can't decrypt the DPAPI-encrypted local files those two use; the admin UI says so. WorkBuddy can keep using Docker.
5. This project and the chat client should run on the same machine. If the client runs inside Docker, set Base URL to `http://host.docker.internal:8787/v1`, not the container's own `127.0.0.1`.

## Install and run

Follow these steps if you haven't set up the environment yet. If you already have a virtual environment, just install `ops/requirements/base.txt` and run `python -m gateway.server`.

### 1. Install tooling

1. [Git](https://git-scm.com/downloads) — on Windows keep the default options.
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) — Python 3.12 recommended.
3. Open and sign into the official client(s) you'll use at least once (Work Buddy / CodeBuddy at minimum).

After installing, **reopen** PowerShell, Windows Terminal, or Anaconda Prompt:

```powershell
git --version
conda --version
```

If `conda` isn't found, use **Anaconda Prompt / Miniconda Prompt** from the Start menu, or run `conda init powershell` there, close the window, and reopen it.

### 2. Clone the project

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
Get-ChildItem README.md, ops, gateway
```

All following commands run inside this directory.

### 3. Start with Conda (recommended)

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r ops/requirements/base.txt
python -m gateway.server
```

When you see the listening message, open in your browser:

```text
http://127.0.0.1:8787
```

To stop the server: go back to the terminal and press `Ctrl+C`. After a reboot next time:

```powershell
cd <your project path>\Buddy2api
conda activate buddy2api
python -m gateway.server
```

The prompt should show `(buddy2api)` before you run `python -m pip`, so you don't install into the system Python.

### Other ways to start

- **Script:** On Windows, with Python added to PATH during install, run `.\ops\start.bat` in the project directory. Linux / macOS: `chmod +x ops/start.sh && ./ops/start.sh`. The script prefers a Conda environment named `buddy2api`, and only creates a `.venv` if Conda isn't present.
- **Docker:** `powershell -ExecutionPolicy Bypass -File .\ops\start-docker-win.ps1`. The script still starts even when there's no WorkBuddy login directory on the host. All six channels are still in the dropdown when GMI is enabled via `CB_GATEWAY_PROVIDERS`, but use the `python -m gateway.server` method above for QClaw / QwenWork. TraeWork's login file isn't DPAPI, so once imported via local `python -m gateway.server`, Docker can use the tokens from the database. Trae SOLO doesn't read a local directory — its login loop and tokens live in the database, so it works inside the container too. GMI is Web-imported and works inside the container as well.

### After opening the web UI for the first time

The admin UI no longer auto-issues a cookie. After first opening the page, go to **Settings** and paste the Admin Token from the startup log into "Admin login" and save once; after that the browser uses the HttpOnly cookie.

1. Open **Accounts**. Select WorkBuddy / QClaw / QwenWork / TraeWork from the dropdown, click "Re-detect", then "Import local logins". For **Trae SOLO** use "Start web login" instead: finish the TRAE login in the new window and it redirects back to import; if the remote can't reach the `127.0.0.1` callback, paste the full address-bar URL into "Manual complete".
2. Click "Test" on the account — if it returns a sentence, that channel is working.
3. Open **API Keys**, **pick the same channel first** then create. For Codex, set the key type to Codex and use the `/v1/responses` interface. After creating you can reveal and copy the full key.
4. Fill in your client:
   - Base URL: `http://127.0.0.1:8787/v1`
   - API Key: the key you just copied
   - Model: WorkBuddy `auto`; QClaw `auto`; QwenWork `auto` or `qwork-advanced`; TraeWork `auto` or `qwen-3.7-plus`; Trae SOLO `auto` or `glm-5.2` (`auto` maps to `glm-5.2` on SOLO)

If the admin UI won't open or you need remote access:

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-replace-with-a-long-random-value"
python -m gateway.server
```

### Update

First stop the running server with `Ctrl+C`:

```powershell
cd <your project path>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r ops/requirements/base.txt
python -m gateway.server
```

## FAQ

- `git` or `conda` is not a recognized command: close the terminal and reopen it; Conda users should use Miniconda Prompt.
- `No module named ...`: first `conda activate buddy2api`, then `python -m pip install -r ops/requirements/base.txt`.
- Dependency download is very slow: make sure PyPI is reachable and don't mix several Python installs.
- Port 8787 is occupied: stop the old Buddy2api, or `python -m gateway.server --port 8788`.
- No accounts in the web UI: not imported yet. Pick the right channel and detect; if the login directory is wrong, set `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`.
- Key creation failed: no channel was selected.
- Client 503 `channel_unavailable`: the channel bound to this key has no usable account yet.
- Client 403 `key_channel_mismatch`: the model carries another channel's prefix and doesn't match the current key.
- Client 400 `unknown_model`: the model doesn't belong to this key's channel. Switch keys, or use an id that channel recognizes.

## Upgrade from 1.4.x

The database is migrated automatically on startup. Old keys are treated as bound to `workbuddy`; the original `auto` / `glm-5.2` still work.

Differences from 1.4: startup no longer auto-imports accounts; an empty pool returns 503 instead of a plain `server_error`; new keys must select a channel; the official balance shows credits only and does not add up numbers across vendors.

## Client setup

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Created in the admin UI, bound to a channel |
| Model | WorkBuddy: `auto` / `glm-5.2`. QClaw: `auto` or `qclaw/default`. QwenWork: `auto` or `qwork-advanced`. TraeWork: `auto` or `qwen-3.7-plus`. Trae SOLO: `auto` / `glm-5.2` / `traesolo/...` (full list in `/v1/models`) |
| Stream | Recommended on |

Interfaces: `/v1/chat/completions`, `/v1/responses`, `/v1/models`. An unprefixed `auto` goes to the channel bound to the key. Codex uses the Responses interface; a key set to type Codex in the admin UI is sanitized per Codex prompt characteristics (if another client borrows the key but lacks Codex characteristics, it isn't rewritten).

OpenCode example (WorkBuddy key):

```json
{
  "provider": {
    "workbuddy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-yourkey"
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
opencode run -m workbuddy/auto "hello"
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-yourkey" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

QwenWork, QClaw, TraeWork, and Trae SOLO each use their own key — don't mix them. Note `glm-5.2` exists on both WorkBuddy and Trae SOLO: unprefixed it resolves via the key's bound channel; to point explicitly at SOLO use `traesolo/glm-5.2`.

### Configure the model list per channel

Each channel's model list / aliases can be configured via the admin API (takes effect immediately, no restart); the built-in defaults are used when unset.

```bash
# View (includes effective value, built-in default, and whether customized)
curl -H "Authorization: Bearer <admin-token>" http://127.0.0.1:8787/admin/channels/traework/models

# Update (models replaces the whole list; null resets to default)
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/channels/traework/models \
  -d '{"models":["qwen-3.7-plus","glm-5"],"aliases":{"auto":"qwen-3.7-plus"}}'
```

Rules: `models` is a non-empty array of strings (or `{"id": "..."}` objects); `aliases` is a non-empty object of `alias -> model id`; at least one field per request. The custom list is a whitelist — models not in it return 400 for that channel (except QClaw's `pool-*` prefix). WorkBuddy is compatible with the legacy keys `models` / `model_aliases`; other channels store `<channel>.models` / `<channel>.aliases`.

### Unified models (cross-platform translation layer)

When the same model has different names on different platforms, define a unified model once (the unified name follows WorkBuddy's naming). The client only requests the unified name, and the gateway translates it to that platform's internal name based on the key's bound platform; whitelist validation then proceeds as usual (an internal name not in the whitelist still returns 400 — unified models don't auto-enter the whitelist).

```bash
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/unified-models \
  -d '{"models":[{"name":"deepseek-v4-flash","mappings":{"traework":"DeepSeek-V4-Flash-Official","workbuddy":"deepseek-v4-flash"}}]}'
```

The admin UI's "Model config" page provides a graphical interface: a wide "Unified models" table (one unified model per row, one platform per column; fill the cell with the internal name, empty = that platform doesn't have it) + a "Per-channel settings" toggle list (each platform's whitelist and aliases).

## Launch parameters

| Parameter | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Listen address; keep this value for local use |
| `--port` | `8787` | Port |
| `--admin-token` | auto-generated (printed once in the startup log) | Admin token; paste it once in the admin UI "Settings" to get a cookie |
| `--no-admin-auth` | off | Disable admin auth; local trial only |
| `--config` | read the `[default]` block of `config.toml`; if a path is given, treat it as a TOML file; if not, treat it as a profile name (see [Configuration file](#configuration-file)) |
| `--config-name` | `default` | Profile table name inside the TOML file (e.g. `[dev]`, `[prod]`) |

## Configuration file

Place a `config.toml` at the project root and `gateway.server` will auto-load it on startup. This is the alternative to a wall of CLI flags / env vars: put `host`, `port`, `database.path`, `admin.token` in a file, and a bare `python -m gateway.server` does the right thing.

**Priority order** (later wins):

```
code default  ->  config.toml [default]  ->  config.toml [<profile>]  ->  env var  ->  CLI flag
```

**Two ways to switch profiles**:

```bash
# 1) Profile is a table inside the same config.toml
python -m gateway.server                          # uses [default]
python -m gateway.server --config prod            # uses [prod]
CB_GATEWAY_CONFIG=prod python -m gateway.server   # same, via env

# 2) Profile lives in a separate file
python -m gateway.server --config config.prod.toml
```

**Full example** (dev / prod share the same code, each has its own config):

```toml
# config.toml -- dev checkout defaults to 8787
[default.server]
host = "127.0.0.1"
port = 8787

[dev.server]
host = "127.0.0.1"
port = 8787
```

```toml
# config.prod.toml -- prod runs on 8788 with its own data dir
[default.server]
host = "127.0.0.1"
port = 8788

[default.database]
path = "/var/lib/buddy2api/codebuddy_gateway.db"

[default.admin]
# Leave empty to auto-generate; set a fixed value to keep the
# browser cookie valid across restarts.
# token = "cb-admin-xxxxxxxxxxxxxxxxxxxxxxxx"
```

`config.toml` and `config.*.toml` are `.gitignore`d (per-deploy config never enters the repo); only `config.example.toml` is tracked as a template.

**Running dev and prod side-by-side**: each checkout writes its own `config.toml`, and port / db path must differ (otherwise they race on the same WAL file):

| checkout | config.toml port | config.toml db path |
|---|---|---|
| `Buddy2api/` (dev) | 8787 | `data/codebuddy_gateway.db` (default) |
| `Buddy2api-prod/` (worktree runtime) | 8788 | `data/codebuddy_gateway.db` (relative to prod's own cwd) |

Pin the admin token so the browser cookie survives restarts: edit `config.toml` and set `admin.token = "cb-admin-xxx"`. Generate one with: `python -c "import secrets; print('cb-admin-' + secrets.token_urlsafe(24))"`.

## Environment variables

> All optional, all with sensible defaults; in most cases **you don't need to set anything**. Variables are grouped by purpose; the value in parentheses is the default, and `*` marks special cases.

### Core / startup
| Variable | Description |
|---|---|
| `CB_GATEWAY_PROVIDERS` | Which channels to enable, comma-separated. Default `workbuddy,qclaw,qwenwork,traework,traesolo`. GMI is opt-in; enable by appending `gmi`: `workbuddy,qclaw,qwenwork,traework,traesolo,gmi` |
| `CB_GATEWAY_AUTO_IMPORT` | Set `1` to auto-scan and import accounts on startup. Default `0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | Milliseconds between adjacent accounts during one-click checkin (anti-risk; don't set too small). Default `800` |
| `CB_GATEWAY_ADMIN_TOKEN` | Fixed admin token. Default auto-generated (printed once at startup; paste into admin UI "Settings" once to get a cookie) |
| `CB_GATEWAY_DB_PATH` | Database file path. Default under project `data/` |
| `CB_GATEWAY_MASTER_KEY` | Explicit encryption master key for moving the database across systems. Default auto-generated per instance (breaks if you move machines or delete `data/`; set when migrating) |
| `CB_GATEWAY_CREDENTIAL_KEY_FILE` * | File path to read the encryption master key from (for Docker injection). Default empty, i.e. use `CB_GATEWAY_MASTER_KEY` or auto-generate |
| `CB_GATEWAY_SECURE_COOKIE` | Set `1` to force the admin cookie Secure (behind https or a reverse proxy). Default follows the request scheme |
| `CB_GATEWAY_LOG_RETENTION_DAYS` | Log retention days. Default `90` |

### Per-channel auth folders
| Channel | Variable | Description |
|---|---|---|
| WorkBuddy | `CB_AUTH_DIR` | Local login directory |
| QClaw | `CB_QCLAW_AUTH_DIR` | Local login directory |
| QwenWork | `CB_QWENWORK_AUTH_DIR` | Local login directory |
| TraeWork | `CB_TRAEWORK_AUTH_DIR` | Directory containing `storage.json` |
| Trae SOLO | `CB_TRAESOLO_CALLBACK_BASE` | Login callback base (point to an externally reachable address when deployed remotely; defaults to the request's own address) |
| Trae SOLO | `CB_TRAESOLO_AUTH_DIR` * | Credential-JSON scan directory (optional; this channel doesn't scan by default, uses web login) |

> `CB_HOST_AUTH_DIR` is only used internally by the Docker deploy script (the mounted host WorkBuddy directory); `CB_CONTAINER_AUTH_DIR` is the in-container mount point (default `/auth`) and is usually left alone.

### WorkBuddy outbound fingerprint (User-Agent / version headers)
| Variable | Description |
|---|---|
| `CB_GATEWAY_USER_AGENT` | Override the entire User-Agent. Default `CLI/2.109.2 CodeBuddy/2.109.2`; set `codebuddy2openai/2.0` to fall back to the legacy UA. Affects WorkBuddy outbound only |
| `CB_GATEWAY_IDE_VERSION` | CLI version, drives UA and X-IDE-Version. Default `2.109.2` |
| `CB_GATEWAY_STAINLESS_OS` * | Reported OS string. Default inferred from the current platform |
| `CB_GATEWAY_STAINLESS_PACKAGE_VERSION` * | `stainless` package version. Default `5.10.1` |
| `CB_GATEWAY_NODE_VERSION` * | Node runtime version. Default `v22.13.1` |

### Request / risk control
| Variable | Description |
|---|---|
| `CB_GATEWAY_CORS_ORIGINS` | Allowed CORS origins, comma-separated. Default `http://127.0.0.1:8787,http://localhost:8787` |
| `CB_GATEWAY_ALLOW_UNAUTHENTICATED_API` | Set `1` to allow requests without an API key (local trial only). Default `0` |
| `CB_GATEWAY_MAX_BODY_BYTES` | Request body size limit in bytes. Default `10MiB` |
| `CB_GATEWAY_USAGE_RATE_LIMIT` | Per-second rate limit on the `/usage` interface; set `0` to disable. Default `30` |
| `CB_GATEWAY_TOOL_STALL_RETRY` | Auto-retry once with `tool_choice=required` on tool stall. Default `1` |
| `CB_GATEWAY_TOOL_STALL_FAIL_STREAM` * | On a streaming tool stall where the retry also fails, mark the round as failed instead of returning body text. Default `0` |

### Reasoning effort (per model)

No more environment variable: configure it **per model** on the admin page **Channels & Models → per-platform settings** (stored in DB, takes effect immediately):

- Per-model dropdown: `default (no injection)` / `none` / `minimal` / `low` / `medium` / `high` / `max`, plus a channel-level default that applies to models without an explicit entry.
- Priority: explicit client `reasoning_effort` > per-model config > channel default > no injection (upstream default).
- Only the WorkBuddy channel upstream (`copilot.tencent.com`) is confirmed to support this parameter; other channels show `—` in the UI.
- Native accepted values from live probing: see `docs/design/per-model-reasoning-effort.md`. Note: deepseek/glm/auto default to *no* thinking — selecting a level turns thinking *on* (slower); for fastest, pick `low` or leave empty for DeepSeek. `off` is rejected upstream (11150).

### content compaction (workbuddy 11128 self-heal)
| Variable | Description |
|---|---|
| `CB_GATEWAY_COMPACT_CHARS` | Manually enable global compaction of oversized request bodies and set the per-field char threshold. Default `0` (off; uses the per-channel self-heal below) |
| `CB_GATEWAY_COMPACT_ARMED_CHARS` * | Once a channel actually triggers 11128 once, the per-field threshold it auto-compacts at. Default `3000` |
| `CB_GATEWAY_COMPACT_SYSTEM_CHARS` * | system-message threshold (pure head truncation; in practice its trailing git/commit block is the 11128 trigger). Default `5000` |

> See `docs/workbuddy-11128-troubleshoot.md`: normal requests aren't truncated by default; after a channel returns 11128 it auto-arms and compacts (system pure-head-cut 5000, oversized content/reasoning head-cut, tool descriptions trimmed; structural keys and `tool_calls` are never cut), and the `compaction` field of `/admin/stats` shows what's in effect.

### Debug
| Variable | Description |
|---|---|
| `CB_DEBUG_DUMP` * | Dump responses-protocol requests/responses (redacted JSON) to `upstream/.debug/` for outbound-protocol troubleshooting. Default off |
| `CB_DEBUG_DUMP_INCLUDE_CONTENT` * | Also write content in the dump (default redacts body text). Default off; only used together with `CB_DEBUG_DUMP` |
| `CB_DOCKER` * | Marks the run as inside Docker (internal use). Default empty |

## Credit and token accounting

Channel-level token / credit accounting is inconsistent:

- **WorkBuddy**: both tokens and credit are reported by the upstream directly;
- **Trae SOLO / QClaw / QwenWork**: tokens are reported by the upstream, credit is not;
- **TraeWork**: neither tokens nor credit are reported (the `token_usage` event is dropped from its SSE).

Since v2.2.0, traesolo / qclaw / qwenwork can enable a **gateway-side token→credit estimate** (set `credit_rate` per channel in "Model config → Per-channel settings"; default 1000 tokens / 1 credit). This is an **estimate, not a real charge** — for trend-spotting and internal estimates only; don't reconcile it against the upstream's real balance. TraeWork would need its SSE parser fixed first to participate. See `docs/credit-and-token-tracking.md` for details.

## Data and security

- Account tokens are encrypted before being written. Windows uses system DPAPI.
- Don't send `*.db`, login directories, logs, or keyed screenshots to anyone.
- Don't bind the service to the public internet. Keep `127.0.0.1`.

## Project layout

The core Python code is split into three packages by responsibility; the root holds only entry / deployment / docs:

```text
Buddy2api/
├── gateway/                # HTTP entry (FastAPI app + routes + version)
│   ├── server.py           # app factory, lifespan, StaticFiles mount
│   ├── router.py           # Bind request to channel, model translation (helper)
│   ├── deps.py             # Shared auth dependencies
│   ├── routers/
│   │   ├── admin.py        # /admin/* endpoints
│   │   ├── v1.py           # /v1/chat/completions, /v1/responses, /v1/models
│   │   └── static_router.py# /admin/meta and other metadata
│   └── version.py
├── accounts/               # Account and channel management
│   ├── auth_manager.py     # Account selection, token management, checkin
│   └── control_plane.py    # Startup scan, one-click claim, model config
├── upstream/               # Upstream adapters
│   ├── proxy.py            # Pipeline orchestration (proxy_chat_completions, etc.)
│   ├── aliases.py          # Model alias table, default model list, reasoning effort
│   ├── moderation.py       # Content moderation, tool-stall detection
│   ├── compaction.py       # Request body compaction, 11128 self-heal
│   └── responses.py        # OpenAI Responses ↔ Chat Completions translation
├── storage/                # Infrastructure layer (DB, crypto, fingerprint, cache)
│   ├── database.py         # Compat facade (re-exports from storage.repos)
│   ├── repos/
│   │   ├── accounts.py     # Account CRUD
│   │   ├── api_keys.py     # API key CRUD
│   │   ├── logs.py         # Request log + queries
│   │   ├── settings.py     # Channel config, KV
│   │   ├── stats.py        # Dashboard aggregations
│   │   └── _common.py      # Shared connection / schema
│   ├── credit_cache.py     # Per-channel credit cache
│   ├── http_pool.py        # Upstream httpx client pool
│   ├── credential_crypto.py
│   └── fingerprint.py
├── providers/              # Channel adapters
│   ├── workbuddy/
│   ├── qclaw/
│   ├── qwenwork/
│   ├── traework/
│   ├── traesolo/
│   └── gmi/                # v2.2 new, opt-in
├── web/                    # Admin UI
│   ├── index.html
│   ├── css/app.css
│   ├── js/
│   │   ├── app.js          # Entry
│   │   ├── api.js          # Backend API client
│   │   ├── icons.js        # Inline SVG icons
│   │   └── pages/          # dashboard / accounts / quota / keys / channels / usage / logs / setup / settings
│   └── vendor/             # Vue 3.4.21 + SortableJS 1.15.6 (local, works offline)
├── docs/                   # Design and usage docs
├── tests/                  # pytest
│   ├── test_*.py           # Business and per-channel tests
│   └── test_web_assets.py  # SPA ESM parse + vendor/CDN guards (new in v2.2)
├── ops/                    # Launch / deploy / build / one-off scripts
│   ├── start.bat / start.sh             # Native launch scripts
│   ├── start-docker-win.ps1 / start-docker-wsl.sh
│   ├── Dockerfile
│   ├── docker-compose.yml / docker-compose.windows.yml
│   ├── docker-entrypoint.sh
│   ├── requirements/{base.txt, dev.txt}
│   └── scripts/oneoff/                  # Archived one-off analysis and backfill scripts
├── data/                   # Runtime data (DB + credentials, .gitignore)
├── redesign-audit/         # v2.2 refactor design docs (baseline / audit / strategy / tokens)
└── README.md / README_EN.md / SECURITY.md / LICENSE / .gitignore / .dockerignore / .mailmap
```

Start with `python -m gateway.server` (from the repo root).

Launch scripts:

```powershell
# Windows
.\ops\start.bat
# Linux / macOS
chmod +x ops/start.sh && ./ops/start.sh
```

Docker:

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\ops\start-docker-win.ps1
# WSL
./ops/start-docker-wsl.sh
```

## What's new in v2.2

Compared to 1.4 / 2.0 / 2.1:

- **GMI channel**: new opt-in channel (OpenAI-compatible, Web-imported API key). Off by default; enable by appending `gmi` to `CB_GATEWAY_PROVIDERS`.
- **Admin UI vendoring**: Vue 3.4.21 and SortableJS 1.15.6 moved off jsdelivr CDN into `web/vendor/`, served by the FastAPI StaticFiles mount. The admin UI now works fully offline. `tests/test_web_assets.py` guards against any future CDN reference creeping back in.
- **Backend monolith split**:
  - `storage/database.py` is now a re-export facade; modules live in `storage/repos/{accounts, api_keys, logs, settings, stats, _common}.py`.
  - `gateway/server.py` keeps the app factory, lifespan, and StaticFiles mount; endpoints split by domain into `gateway/routers/{admin.py, v1.py, static_router.py}`; shared auth dependencies live in `gateway/deps.py`.
  - `upstream/proxy.py` keeps the pipeline orchestration; model aliases, moderation, compaction, and the Responses bridge live in `upstream/{aliases.py, moderation.py, compaction.py, responses.py}`.
  - All 56 endpoint paths, contracts, and behavior are unchanged; the test suite shows the same pre-existing pass/fail as the v2.1 baseline (no new regressions).
- **Admin UI overhaul**: eight levers, one commit each (vendor local, version from `/admin/meta`, CSS token rebuild, component layer, chart palette tokenization, key page reflow, responsive breakpoint consolidation, one-off script archive). Version is now fetched from the backend instead of hard-coded. `em-dash` characters were replaced with Chinese punctuation throughout the SPA.
- **One lever, one commit**: every refactor commit is independently reviewable (`refactor(web): ...`, `refactor(storage): ...`, `refactor(gateway): ...`, `refactor(upstream): ...`); all commits are pushed to `refactor/web-console-ia`. See `redesign-audit/` for the design baseline, audit findings, strategy, and token spec.
- **Config file `config.toml`**: added. `gateway.server` auto-loads it on startup, supports `[default]` / `[dev]` / `[prod]` profiles plus `--config <profile>` / `CB_GATEWAY_CONFIG=<profile>` to switch. A dev/prod worktree split keeps two `config.toml` files (`.gitignore`d, per-deploy private) pinning port and db path, so a bare `python -m gateway.server` lands on the right port in each checkout. See [Configuration file](#configuration-file) for details.

## License

MIT
