"""Read official QClaw Electron credentials. Never scans WorkBuddy CB_AUTH_DIR."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from providers.qclaw.constants import CHANNEL_ID, CLIENT_VERSION
from providers.store_common import (
    file_meta,
    chromium_os_crypt_key,
    decrypt_chromium_v10,
    discover_summary,
    existing_uids,
    imported_file_meta,
    is_relative_to,
    upsert_account as upsert_account_by_uid,
)


def qclaw_user_data_dir() -> Path:
    override = os.environ.get("CB_QCLAW_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "QClaw"


def qclaw_auth_dirs() -> list[Path]:
    dirs: list[Path] = []
    explicit = os.environ.get("CB_QCLAW_AUTH_DIR", "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    dirs.append(qclaw_user_data_dir())
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _chromium_os_crypt_key(local_state: dict) -> bytes:
    return chromium_os_crypt_key(local_state, label="QClaw")


def decrypt_electron_blob(cipher_b64: str, aes_key: bytes) -> bytes:
    return decrypt_chromium_v10(base64.b64decode(cipher_b64), aes_key, label="QClaw")


def read_official_session(user_dir: Path | None = None) -> dict | None:
    root = user_dir or qclaw_user_data_dir()
    store_path = root / "app-store.json"
    state_path = root / "Local State"
    if not store_path.is_file() or not state_path.is_file():
        return None
    store = json.loads(store_path.read_text(encoding="utf-8"))
    local_state = json.loads(state_path.read_text(encoding="utf-8"))
    aes_key = _chromium_os_crypt_key(local_state)

    def _field(key: str) -> str:
        entry = store.get(key) or {}
        cipher = entry.get("cipherText") if isinstance(entry, dict) else None
        if not cipher:
            return ""
        return decrypt_electron_blob(cipher, aes_key).decode("utf-8")

    jwt = _field("secure.jwtToken")
    api_key = _field("authGateway.providers.qclaw.apiKey")
    user_raw = _field("secure.userInfo")
    user: dict = {}
    if user_raw:
        try:
            parsed = json.loads(user_raw)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            user = parsed
    guid = str(user.get("guid") or "")
    device_id_path = root / "device-id"
    if not guid and device_id_path.is_file():
        guid = device_id_path.read_text(encoding="utf-8").strip()
    if not api_key and not jwt:
        return None
    user_id = str(user.get("userId") or user.get("user_id") or "")
    return {
        "provider": CHANNEL_ID,
        "uid": user_id,
        "nickname": str(user.get("nickname") or ""),
        "access_token": api_key,
        "refresh_token": jwt,
        "guid": guid,
        "user": user,
        "client_version": CLIENT_VERSION,
        "source": str(store_path),
    }


def session_to_account(session: dict) -> dict:
    extra = {
        "guid": session.get("guid") or "",
        "client_version": session.get("client_version") or CLIENT_VERSION,
        "source": session.get("source") or "import",
    }
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    if user:
        extra["openid"] = user.get("openid") or ""
    channel_token = session.get("openclaw_channel_token") or session.get("channel_token")
    if channel_token:
        extra["openclaw_channel_token"] = channel_token
    return {
        "name": session.get("nickname") or f"qclaw-{session.get('uid') or 'user'}",
        "uid": str(session.get("uid") or ""),
        "nickname": session.get("nickname") or "",
        "access_token": session.get("access_token") or session.get("sk_api_key") or session.get("api_key") or "",
        "refresh_token": session.get("refresh_token") or session.get("jwt") or "",
        "provider": CHANNEL_ID,
        "domain": "qclaw.qq.com",
        "extra": extra,
        "status": "active",
    }


def parse_credentials(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("QClaw credentials must be a JSON object")
    nested_account = body.get("account") if isinstance(body.get("account"), dict) else {}
    nested_auth = body.get("auth") if isinstance(body.get("auth"), dict) else {}
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    session = {
        "uid": (
            body.get("uid")
            or body.get("user_id")
            or nested_account.get("uid")
            or nested_account.get("user_id")
            or user.get("userId")
            or user.get("user_id")
            or ""
        ),
        "nickname": body.get("nickname") or nested_account.get("nickname") or user.get("nickname") or "",
        "access_token": (
            body.get("access_token")
            or body.get("sk_api_key")
            or body.get("api_key")
            or nested_auth.get("accessToken")
            or nested_auth.get("sk_api_key")
            or ""
        ),
        "refresh_token": (
            body.get("refresh_token")
            or body.get("jwt")
            or nested_auth.get("refreshToken")
            or nested_auth.get("jwt")
            or ""
        ),
        "guid": body.get("guid") or nested_account.get("guid") or user.get("guid") or "",
        "user": user,
        "openclaw_channel_token": (
            body.get("openclaw_channel_token")
            or body.get("channel_token")
            or nested_auth.get("openclaw_channel_token")
            or ""
        ),
        "source": "paste",
    }
    parsed = session_to_account(session)
    if not parsed["access_token"]:
        raise ValueError("QClaw credentials need sk_api_key / access_token")
    return parsed


def discover() -> dict:
    dirs_info = []
    files: list[dict] = []
    existing = existing_uids(CHANNEL_ID)
    for folder in qclaw_auth_dirs():
        exists = folder.is_dir()
        dirs_info.append({"path": str(folder), "exists": exists, "file_count": 0})
        if not exists:
            continue
        store = folder / "app-store.json"
        json_files = []
        if store.is_file():
            json_files.append(store)
        json_files.extend(sorted(folder.glob("*.json")))
        seen_paths: set[str] = set()
        count = 0
        for path in json_files:
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            count += 1
            meta = _file_meta(path, existing)
            files.append(meta)
        dirs_info[-1]["file_count"] = count
    return discover_summary(CHANNEL_ID, dirs_info, files)


def _file_meta(path: Path, existing: set[str]) -> dict:
    if path.name == "app-store.json":
        reason = ""
        valid = False
        uid = ""
        name = path.name
        try:
            session = read_official_session(path.parent)
            if session and session.get("access_token"):
                valid = True
                uid = str(session.get("uid") or "")
                name = session.get("nickname") or name
            else:
                reason = "official store missing api key"
        except Exception as exc:
            reason = str(exc)[:160]
            valid = False
        return file_meta(
            CHANNEL_ID, path, valid=valid, reason=reason, uid=uid, name=name, existing=existing
        )
    return imported_file_meta(
        CHANNEL_ID, path, existing, import_discovered, token_fields=("access_token",)
    )


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed_roots = [folder.resolve() for folder in qclaw_auth_dirs() if folder.exists()]
    resolved = target.resolve()
    if allowed_roots and not any(is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError("path is outside CB_QCLAW_AUTH_DIR / official QClaw data dir")
    if target.name == "app-store.json":
        session = read_official_session(target.parent)
        if not session:
            raise ValueError("failed to decrypt official QClaw store")
        return session_to_account(session)
    payload = json.loads(target.read_text(encoding="utf-8"))
    return parse_credentials(payload)


def default_guid() -> str:
    path = qclaw_user_data_dir() / "device-id"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def upsert_account(parsed: dict) -> dict:
    return upsert_account_by_uid(CHANNEL_ID, parsed)
