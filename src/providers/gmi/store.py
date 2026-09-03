"""GMI account storage — single-API-key platform.

A GMI "account" is just the API key itself (no refresh, no expiry, no per-user
quota endpoint we can rely on). We store it as one row in the unified `accounts`
table with provider='gmi', so auth_manager.pick_account / database.record_request
work without any schema change.

Sources:
  1. Admin UI "Import" form: paste the raw JWT key.
  2. Environment variable CB_GMI_API_KEY (auto-imported on first lookup if no
     active account exists). # ponytail: convenience for power users; keeps
     zero-config experience when running as a sidecar proxy.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from providers.gmi.constants import CHANNEL_ID, DEFAULT_BASE_URL, ENV_AUTH_DIR, ENV_API_KEY
from storage import database as db


def _normalize_key(raw: str) -> str:
    """Trim whitespace / accidental JSON wrapping around a raw JWT."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    # If user pasted `{"api_key": "..."}` or `Bearer xxx`, extract the token.
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(obj, dict):
            for key in ("api_key", "apiKey", "key", "token"):
                if obj.get(key):
                    return str(obj[key]).strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def parse_credentials(body: dict) -> dict:
    """Parse the admin-UI paste payload into an `accounts` row."""
    if not isinstance(body, dict):
        raise ValueError("GMI credentials must be a JSON object")
    raw = (
        body.get("api_key")
        or body.get("apiKey")
        or body.get("key")
        or body.get("token")
        or ""
    )
    key = _normalize_key(str(raw))
    if not key:
        raise ValueError("GMI credentials need a non-empty api_key")
    nickname = str(body.get("nickname") or body.get("name") or "gmi").strip() or "gmi"
    base_url = str(body.get("base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    return {
        "name": f"gmi-{nickname[:24]}",
        "uid": f"gmi-{key[-8:]}",
        "nickname": nickname,
        "account_type": "api_key",
        "access_token": key,
        "refresh_token": "",
        "expires_at": 0,
        "refresh_expires_at": 0,
        "domain": base_url,
        "provider": CHANNEL_ID,
        "status": "active",
        "weight": 1,
        "priority": 0,
        "extra": {
            "base_url": base_url,
            "source": "paste",
        },
    }


def discover() -> dict:
    """GMI has no IDE directory. Only an env-var scan."""
    return {
        "channel": CHANNEL_ID,
        "dirs": [],
        "files": [],
        "file_count": 0,
        "valid_count": 0,
        "importable_count": 0,
        "preview_token": "",
    }


def import_path(path: str) -> dict:
    """Import from a file path (txt or json)."""
    target = os.path.expanduser(path)
    if not os.path.isfile(target):
        raise ValueError(f"path is not a file: {path}")
    with open(target, "r", encoding="utf-8") as fh:
        raw = fh.read()
    return upsert_account(parse_credentials({"api_key": raw}))


def upsert_account(parsed: dict) -> dict:
    """Insert or update by (provider, uid). Replaces the key in-place.

    Returns the store_common contract `{"id": aid, "updated": bool}` —
    gateway/server.py `POST /admin/accounts` reads `result["id"]` /
    `result["updated"]`, so a bare row or int here 500s the import even
    though the row landed.
    """
    key = parsed["access_token"]
    base = parsed.get("domain") or DEFAULT_BASE_URL
    existing = None
    for row in db.list_accounts(provider=CHANNEL_ID):
        if row.get("uid") == parsed.get("uid"):
            existing = row
            break
    target_id = existing["id"] if existing else None
    # 单 key 平台（SINGLE_ACCOUNT=True）：同一时刻只允许一个 active 行。
    # 换 key 轮换 / 导回旧 key 时，把其余 active 行全部置 inactive 留档
    #（保留用量历史），避免调度器选中已失效的 key。
    for row in db.list_accounts(provider=CHANNEL_ID):
        if row["id"] != target_id and row.get("status") == "active":
            db.update_account(row["id"], {"status": "inactive"})
    if existing:
        db.update_account(
            existing["id"],
            {
                "access_token": key,
                "domain": base,
                "status": "active",
                "extra": parsed.get("extra") or {},
                "updated_at": int(time.time()),
            },
        )
        return {"id": existing["id"], "updated": True, "row": db.get_account(existing["id"])}
    aid = db.add_account(parsed)
    return {"id": aid, "updated": False, "row": db.get_account(aid)}


def ensure_env_account() -> Optional[dict]:
    """If CB_GMI_API_KEY is set and no active GMI account exists, create one.

    Idempotent: returns the existing active row if present, otherwise inserts
    a fresh row keyed by the env value's last-8-chars and returns the full row.
    """
    env_key = os.environ.get(ENV_API_KEY, "").strip()
    if not env_key:
        return None
    norm = _normalize_key(env_key)
    if not norm:
        return None
    target_uid = f"gmi-{norm[-8:]}"
    for row in db.list_accounts(provider=CHANNEL_ID):
        if row.get("status") == "active":
            return row
    parsed = parse_credentials({"api_key": norm, "nickname": "env"})
    parsed["uid"] = target_uid
    parsed["name"] = "gmi-env"
    parsed["extra"]["source"] = "env"
    result = upsert_account(parsed)
    row = result.get("row")
    if isinstance(row, dict) and row.get("id") == result.get("id"):
        return row
    return db.get_account(result["id"])