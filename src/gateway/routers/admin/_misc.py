"""Admin endpoints: logs, settings, Codex one-click setup.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request

from storage import database as db
from gateway.deps import _atomic_write, _check_admin, _read_json_object

router = APIRouter()


# ============================================================
# Logs
# ============================================================

@router.get("/admin/logs")
async def admin_logs(
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return db.list_logs(max(1, min(500, limit)), max(0, offset))


@router.get("/admin/logs/search")
async def admin_logs_search(
    q: str | None = None,
    status: str = "all",
    account_id: str | None = None,
    api_key_id: str | None = None,
    model: str | None = None,
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    if account_id not in (None, "", "all") and not str(account_id).isdigit():
        raise HTTPException(status_code=400, detail="account_id must be numeric")
    if api_key_id not in (None, "", "all") and not str(api_key_id).isdigit():
        raise HTTPException(status_code=400, detail="api_key_id must be numeric")
    return db.search_logs({
        "q": q or "",
        "status": status,
        "account_id": account_id,
        "api_key_id": api_key_id,
        "model": model or "",
        "limit": limit,
        "offset": offset,
    })


# ============================================================
# Settings
# ============================================================

@router.get("/admin/settings")
async def admin_get_settings(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_all_settings()


@router.put("/admin/settings")
async def admin_update_settings(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed_settings = {"backend_url", "default_domain", "timeout", "channel_hosts"}
    unknown = set(data) - allowed_settings
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported settings: {', '.join(sorted(unknown))}")
    if "timeout" in data:
        try:
            data["timeout"] = max(5, min(600, int(data["timeout"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timeout must be an integer between 5 and 600")
    if "backend_url" in data:
        backend_url = str(data["backend_url"]).strip().rstrip("/")
        if not backend_url.startswith("https://"):
            raise HTTPException(status_code=400, detail="backend_url must use HTTPS")
        data["backend_url"] = backend_url
    if "channel_hosts" in data:
        ch = data["channel_hosts"]
        if not isinstance(ch, dict):
            raise HTTPException(status_code=400, detail="channel_hosts must be an object")
        from providers.host_override import CHANNEL_HOST_FIELDS
        for cid, fields in ch.items():
            if cid not in CHANNEL_HOST_FIELDS:
                raise HTTPException(status_code=400, detail=f"Unsupported channel: {cid}")
            if not isinstance(fields, dict):
                raise HTTPException(status_code=400, detail=f"channel_hosts[{cid}] must be an object")
            unknown_fields = set(fields) - set(CHANNEL_HOST_FIELDS[cid])
            if unknown_fields:
                raise HTTPException(status_code=400, detail=f"Unsupported host fields: {', '.join(sorted(unknown_fields))}")
            for f, v in fields.items():
                if not v:
                    continue
                val = str(v).strip().rstrip("/")
                if not val.startswith("https://"):
                    raise HTTPException(status_code=400, detail=f"{cid}.{f} must use HTTPS")
                fields[f] = val
    for k, v in data.items():
        db.set_setting(k, v)
    return {"status": "ok"}


# ============================================================
# Codex one-click setup
# ============================================================

@router.post("/admin/codex/setup")
async def admin_codex_setup(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """One-click Codex setup: write config.toml and auth.json."""
    _check_admin(authorization)
    data = await _read_json_object(request)
    api_key = str(data.get("api_key", "")).strip()
    if not api_key.startswith("sk-cb-") or len(api_key) > 256:
        raise HTTPException(status_code=400, detail="api_key is required")

    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    results = {"backed_up": [], "written": [], "config_path": str(config_path), "auth_path": str(auth_path)}
    for p in [config_path, auth_path]:
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            _atomic_write(bak, p.read_bytes())
            results["backed_up"].append(str(bak))

    existing_config = ""
    if config_path.exists():
        existing_config = config_path.read_text(encoding="utf-8")

    new_lines = []
    in_skip_section = False
    skip_section_prefixes = ["[model_providers", "model ", "model=", "model_provider"]

    for line in existing_config.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in ["model =", "model=", "model_provider"]):
            continue
        if stripped.startswith("[model_providers"):
            in_skip_section = True
            continue
        if in_skip_section:
            if stripped.startswith("[") and not stripped.startswith("[model_providers"):
                in_skip_section = False
                new_lines.append(line)
            else:
                continue
        else:
            new_lines.append(line)

    codex_config = '''model = "auto"
model_provider = "buddy2api"

[model_providers.buddy2api]
name = "Buddy2api"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"

'''
    preserved = "\n".join(new_lines).strip()
    final_config = codex_config + ("\n" + preserved if preserved else "")
    _atomic_write(config_path, final_config)
    results["written"].append(str(config_path))

    import json as _json
    auth_content = _json.dumps({"OPENAI_API_KEY": api_key}, indent=2)
    _atomic_write(auth_path, auth_content)
    results["written"].append(str(auth_path))

    # Keep the key in this process's env; only auth.json holds the persistent
    # credential, and only with restricted permissions.
    os.environ["OPENAI_API_KEY"] = api_key

    results["status"] = "ok"
    results["message"] = "Codex 配置已写入。请完全关闭 Codex 后重新打开。"
    return results


@router.get("/admin/codex/status")
async def admin_codex_status(authorization: str | None = Header(default=None)):
    """Check Codex configuration status."""
    _check_admin(authorization)
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    result = {
        "codex_dir_exists": codex_dir.exists(),
        "config_exists": config_path.exists(),
        "auth_exists": auth_path.exists(),
        "config_has_buddy2api": False,
        "config_wire_api": None,
        "config_model": None,
        "auth_has_key": False,
    }

    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        result["config_has_buddy2api"] = "buddy2api" in content
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("wire_api"):
                result["config_wire_api"] = s.split("=", 1)[1].strip().strip('"')
            elif s.startswith("model ") or s.startswith("model="):
                result["config_model"] = s.split("=", 1)[1].strip().strip('"')

    if auth_path.exists():
        try:
            import json as _json
            auth = _json.loads(auth_path.read_text(encoding="utf-8"))
            result["auth_has_key"] = bool(auth.get("OPENAI_API_KEY"))
        except Exception:
            pass

    return result
