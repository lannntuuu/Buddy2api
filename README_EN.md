# Buddy2api 2.2

[English](README_EN.md) | [中文](README.md)

> Local consumer AI clients → one OpenAI-compatible API for Codex, OpenCode, Cherry Studio, NextChat, and similar agents. Work Buddy / CodeBuddy, QClaw, QwenWork, TraeWork, and Trae SOLO are on by default; pick one in the UI dropdown. Each request stays on one channel.

Release **2.2.0**. Local use only. Do not expose this on the public internet, and do not share credentials, API keys, or the database.

## What is this?

Buddy2api listens on `http://127.0.0.1:8787/v1`. You stay signed into the official apps; this gateway imports those sessions and forwards chat. Typical clients use Chat Completions. Codex uses `/v1/responses`; create the key as type Codex in the UI to enable Codex prompt sanitization.

All five channels are on by default. A channel with no local login shows empty on Accounts; nothing is imported until you click Import. Trae SOLO has no local login folder — import it via the UI's **web login** (or by pasting the callback URL).

```powershell
python -m gateway.server
```

| Channel | Default | Where logins live |
|---|---|---|
| WorkBuddy / CodeBuddy | on | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | on | `%APPDATA%\QClaw` |
| QwenWork | on | `%APPDATA%\QwenWorkCN` |
| TraeWork | on | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| Trae SOLO | on | none (web login / credential JSON import) |

Narrow with `CB_GATEWAY_PROVIDERS=workbuddy` if you only want one.

## Before you start

1. **An empty Accounts page after startup is expected.** 2.0 does not import on boot. Pick a channel → Detect → Import. All four local channels are in the dropdown; **Trae SOLO** instead offers **Start web login**: finish the TRAE login in the new window and the browser redirects back to the server to import the account (if the redirect cannot reach the server remotely, paste the full address-bar URL into "Manual complete").
2. **One API key is one channel.** Create the key with a channel selected. A WorkBuddy key uses `auto` / `glm-5.2`; a QwenWork key uses `auto` or `qwork-advanced`; a TraeWork key uses `auto` or `qwen-3.7-plus`; a Trae SOLO key uses `auto` or `glm-5.2` (the full SOLO list is published under the `traesolo/` prefix). Mismatched model/key returns 400 or 403 — there is no cross-vendor failover.
3. **HTTP 503 `channel_unavailable`** means that channel has no imported account.
4. **Run QClaw / QwenWork with `python -m gateway.server` on Windows.** A Linux Docker container cannot decrypt those DPAPI files; the UI says so. WorkBuddy can stay on Docker.
5. If the chat client is itself in Docker, Base URL is `http://host.docker.internal:8787/v1`.

## Install

1. [Git](https://git-scm.com/downloads), [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) (Python 3.12), and sign into Work Buddy / CodeBuddy at least once.
2. Reopen the terminal, then:

```powershell
git --version
conda --version
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r ops/requirements/base.txt
python -m gateway.server
```

3. Open http://127.0.0.1:8787 → Accounts → Detect → Import → Test → API Keys (select a channel; pick Codex if the client is Codex) → point your client at `http://127.0.0.1:8787/v1`.

Windows script: `.\start.bat`. Docker helper: `.\start-docker-win.ps1` (WorkBuddy mount; use native Python for QClaw/QwenWork).

Later starts: `conda activate buddy2api` then `python -m gateway.server` in the project directory.

## FAQ

- `conda` not found: use Miniconda Prompt, or `conda init powershell` and reopen the terminal.
- `No module named ...`: activate `buddy2api`, then `python -m pip install -r ops/requirements/base.txt`.
- Port 8787 in use: stop the old process or `python -m gateway.server --port 8788`.
- No accounts in the UI: import has not been run yet.
- Key create fails: the channel dropdown is required.
- 403 `key_channel_mismatch`: the model prefix does not match the key’s channel.
- 400 `unknown_model`: that model does not belong to this key’s channel.

## Upgrade from 1.4.x

The database migrates on startup. Existing keys stay on `workbuddy`. Startup no longer auto-imports; empty channel is 503; new keys must pick a channel; the official-balance column shows credits only.

## Client

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Created in the UI, bound to one channel |
| Model | WorkBuddy `auto`; QClaw `auto`; QwenWork `qwork-advanced`; TraeWork `qwen-3.7-plus`; Trae SOLO `auto` / `glm-5.2` / `traesolo/...` |

Unprefixed `auto` follows the key's channel. Use a separate key per channel. Note `glm-5.2` exists on both WorkBuddy and Trae SOLO; unprefixed it resolves via the key's channel — use `traesolo/glm-5.2` to be explicit.

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-your-key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## Environment

`CB_GATEWAY_PROVIDERS` (default `workbuddy,qclaw,qwenwork,traework,traesolo`), `CB_GATEWAY_AUTO_IMPORT` (default `0`), `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR` / `CB_TRAEWORK_AUTH_DIR`, `CB_TRAESOLO_CALLBACK_BASE` (SOLO login callback base, for remote deployments), `CB_TRAESOLO_AUTH_DIR` (optional SOLO credential-JSON scan dir), `CB_GATEWAY_ADMIN_TOKEN`, `CB_GATEWAY_MASTER_KEY`.

Keep `--host 127.0.0.1`. Do not share the database, auth folders, or key screenshots.

## Credit and token accounting

Channel-level token/credit accounting is inconsistent across providers:

- **WorkBuddy**: both tokens and credit are reported by the upstream SSE `usage` event.
- **Trae SOLO / QClaw / QwenWork**: tokens are reported; credit is not.
- **TraeWork**: neither is reported — the `token_usage` event is dropped from its SSE stream.

Since v2.2.0, traesolo / qclaw / qwenwork can apply a **gateway-side token→credit estimate** by
setting `credit_rate` in "Model config → Per-channel settings" (default 1000 tokens = 1 credit).
This is an **estimate, not a real charge** — useful for trend visualisation only. Don't use it
to reconcile against the upstream balance. TraeWork needs its SSE parser fixed first to participate
(this is not done). See `docs/credit-and-token-tracking.md` for the full story.

## Project layout

The Python source is split into three packages by responsibility; the root only holds entry / deployment / docs:

```text
Buddy2api/
├── gateway/                # HTTP entry (FastAPI app + routes + version)
│   ├── server.py           # All @app.get / @app.post endpoints
│   ├── router.py           # Bind request to channel, model translation
│   └── version.py
├── accounts/               # Account and channel management
│   ├── auth_manager.py     # Account selection, token lifecycle, checkin
│   └── control_plane.py    # Startup scan, one-click claim, model config
├── upstream/               # Upstream adapters
│   ├── proxy.py            # HTTP upstream (proxy_chat_completions)
│   └── responses.py        # OpenAI Responses ↔ Chat Completions translation
├── storage/                # Infrastructure (DB, crypto, fingerprint)
│   ├── database.py         # SQLite CRUD
│   ├── credential_crypto.py
│   └── fingerprint.py
├── providers/              # Channel adapters (workbuddy / qclaw / qwenwork / traework / traesolo)
├── web/                    # Admin UI (Vue 3 via CDN)
├── docs/                   # Design and usage docs
├── tests/                  # pytest (incl. pytest.ini)
├── ops/                    # Launch / deploy / build
│   ├── start.bat / start.sh                       # Native launch scripts
│   ├── start-docker-win.ps1 / start-docker-wsl.sh # Docker launch wrappers
│   ├── Dockerfile
│   ├── docker-compose.yml / docker-compose.windows.yml
│   ├── docker-entrypoint.sh
│   └── requirements/
│       ├── base.txt     # Runtime deps (was requirements.txt)
│       └── dev.txt      # Dev / test deps (was requirements-dev.txt)
├── data/                   # Runtime data (DB + credentials, .gitignored)
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

## License

MIT
