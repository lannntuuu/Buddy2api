"""Read official TraeWork storage.json. Never scans WorkBuddy CB_AUTH_DIR."""

from __future__ import annotations

import json
import os
from pathlib import Path

from storage.credential_crypto import CredentialCryptoError
from providers.store_common import (
    discover_summary,
    existing_uids,
    imported_file_meta,
    is_relative_to,
    iso_to_ms,
    jwt_exp_ms,
    upsert_account as upsert_account_by_uid,
)
from providers.traework.constants import (
    AUTH_DEVICE_PREFIX,
    AUTH_STORAGE_KEY,
    CHANNEL_ID,
    IDE_VERSION,
    STORAGE_FILENAME,
    UG_API,
)
from providers.traework.crypto import decrypt_tc_b64
from providers.host_override import channel_host


def traework_user_data_dir() -> Path:
    override = os.environ.get("CB_TRAEWORK_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "TRAE SOLO CN"


def traework_auth_dirs() -> list[Path]:
    dirs: list[Path] = []
    explicit = os.environ.get("CB_TRAEWORK_AUTH_DIR", "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    dirs.append(traework_user_data_dir() / "User" / "globalStorage")
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _read_storage(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise CredentialCryptoError("TraeWork storage.json is not an object")
    return payload


def _device_keys(storage: dict) -> tuple[str, str, str, str]:
    public_pem = ""
    private_pem = ""
    device_id = ""
    for key, value in storage.items():
        if not str(key).startswith(AUTH_DEVICE_PREFIX):
            continue
        device_id = str(key)[len(AUTH_DEVICE_PREFIX) :]
        if not isinstance(value, str):
            continue
        try:
            blob = json.loads(decrypt_tc_b64(value))
        except Exception:
            continue
        if isinstance(blob, dict):
            public_pem = str(blob.get("publicKeyPEM") or "")
            private_pem = str(blob.get("privateKeyPEM") or "")
            break
    machine = str(storage.get("telemetry.machineId") or "")
    return device_id, public_pem, private_pem, machine


def session_to_account(document: dict, storage: dict | None = None, source: str = "") -> dict:
    account = document.get("account") if isinstance(document.get("account"), dict) else {}
    uid = str(document.get("userId") or account.get("userId") or "")
    name = str(account.get("username") or document.get("username") or "")
    access = str(document.get("token") or document.get("access_token") or "")
    refresh = str(document.get("refreshToken") or document.get("refresh_token") or "")
    host = str(document.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API))
    device_id = public_pem = private_pem = machine_id = ""
    if storage:
        device_id, public_pem, private_pem, machine_id = _device_keys(storage)
    extra = {
        "host": host,
        "device_id": device_id,
        "machine_id": machine_id,
        "public_key_pem": public_pem,
        "private_key_pem": private_pem,
        "user_tag": str(account.get("userTag") or ""),
        "store_region": str(account.get("storeRegion") or ""),
        "source": source or "import",
        "client_version": IDE_VERSION,
    }
    return {
        "name": name or f"traework-{(uid or 'user')[:8]}",
        "uid": uid,
        "nickname": name,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": iso_to_ms(document.get("expiredAt")) or jwt_exp_ms(access),
        "refresh_expires_at": iso_to_ms(document.get("refreshExpiredAt")),
        "provider": CHANNEL_ID,
        "domain": host.replace("https://", "").replace("http://", ""),
        "extra": extra,
        "status": "active",
    }


def parse_credentials(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("TraeWork credentials must be a JSON object")
    nested = body.get("account") if isinstance(body.get("account"), dict) else {}
    document = {
        "token": body.get("token") or body.get("access_token") or "",
        "refreshToken": body.get("refreshToken") or body.get("refresh_token") or "",
        "expiredAt": body.get("expiredAt") or body.get("expires_at"),
        "refreshExpiredAt": body.get("refreshExpiredAt") or body.get("refresh_expires_at"),
        "userId": body.get("userId") or body.get("uid") or nested.get("userId") or "",
        "host": body.get("host") or channel_host(CHANNEL_ID, "ug_host", UG_API),
        "account": {
            "username": nested.get("username") or body.get("nickname") or body.get("name") or "",
            "userTag": nested.get("userTag") or "",
            "storeRegion": nested.get("storeRegion") or "",
        },
    }
    parsed = session_to_account(document, source="paste")
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["device_id"] = str(body.get("device_id") or extra.get("device_id") or "")
    extra["machine_id"] = str(body.get("machine_id") or extra.get("machine_id") or "")
    extra["public_key_pem"] = str(body.get("public_key_pem") or extra.get("public_key_pem") or "")
    extra["private_key_pem"] = str(body.get("private_key_pem") or extra.get("private_key_pem") or "")
    parsed["extra"] = extra
    if not parsed["access_token"] and not parsed["refresh_token"]:
        raise ValueError("TraeWork credentials need token / refreshToken")
    return parsed


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed = [folder.resolve() for folder in traework_auth_dirs() if folder.exists()]
    resolved = target.resolve()
    if allowed and not any(is_relative_to(resolved, root) for root in allowed):
        raise ValueError("path is outside CB_TRAEWORK_AUTH_DIR / official TraeWork data dir")
    if target.suffix.lower() == ".json" and target.name != STORAGE_FILENAME:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return parse_credentials(payload if isinstance(payload, dict) else {})
    storage = _read_storage(target)
    blob = storage.get(AUTH_STORAGE_KEY)
    if not isinstance(blob, str) or not blob:
        raise ValueError("TraeWork storage.json has no iCubeAuthInfo")
    document = json.loads(decrypt_tc_b64(blob))
    if not isinstance(document, dict):
        raise ValueError("TraeWork auth JSON is not an object")
    parsed = session_to_account(document, storage=storage, source=str(resolved))
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = str(resolved)
    parsed["extra"] = extra
    if not parsed.get("access_token") and not parsed.get("refresh_token"):
        raise ValueError("failed to decrypt official TraeWork storage.json")
    return parsed


def discover() -> dict:
    dirs_info = []
    files: list[dict] = []
    existing = existing_uids(CHANNEL_ID)
    for folder in traework_auth_dirs():
        exists = folder.is_dir()
        dirs_info.append({"path": str(folder), "exists": exists, "file_count": 0})
        if not exists:
            continue
        count = 0
        path = folder / STORAGE_FILENAME
        if path.is_file():
            count += 1
            files.append(_file_meta(path, existing))
        dirs_info[-1]["file_count"] = count
    return discover_summary(CHANNEL_ID, dirs_info, files)


def _file_meta(path: Path, existing: set[str]) -> dict:
    return imported_file_meta(CHANNEL_ID, path, existing, import_discovered)


def upsert_account(parsed: dict) -> dict:
    return upsert_account_by_uid(CHANNEL_ID, parsed)
