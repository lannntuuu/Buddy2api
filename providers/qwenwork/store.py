"""Read official QwenWorkCN auth-v2.dat. Never scans WorkBuddy CB_AUTH_DIR."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from storage.credential_crypto import CredentialCryptoError
from providers.qwenwork.constants import CHANNEL_ID, IDE_VERSION
from providers.store_common import (
    chromium_os_crypt_key,
    decrypt_chromium_v10,
    discover_summary,
    existing_uids,
    imported_file_meta,
    is_relative_to,
    iso_to_ms,
    jwt_exp_ms,
    upsert_account as upsert_account_by_uid,
)


def qwenwork_user_data_dir() -> Path:
    override = os.environ.get("CB_QWENWORK_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "QwenWorkCN"


def qwenwork_auth_dirs() -> list[Path]:
    dirs: list[Path] = []
    explicit = os.environ.get("CB_QWENWORK_AUTH_DIR", "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    dirs.append(qwenwork_user_data_dir())
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _chromium_os_crypt_key(local_state: dict) -> bytes:
    return chromium_os_crypt_key(local_state, label="QwenWork")


def decrypt_v10_blob(blob: bytes, aes_key: bytes) -> bytes:
    return decrypt_chromium_v10(blob, aes_key, label="QwenWork auth-v2.dat")


def encrypt_v10_blob(plain: bytes, aes_key: bytes) -> bytes:
    nonce = os.urandom(12)
    return b"v10" + nonce + AESGCM(aes_key).encrypt(nonce, plain, None)


def _os_crypt_key_for(folder: Path) -> bytes:
    state_path = folder / "Local State"
    if not state_path.is_file():
        raise CredentialCryptoError("QwenWork Local State is missing")
    local_state = json.loads(state_path.read_text(encoding="utf-8"))
    return _chromium_os_crypt_key(local_state)


def decrypt_auth_file(path: Path) -> dict:
    folder = path.parent
    raw = path.read_bytes()
    if raw.startswith(b"{"):
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise CredentialCryptoError("QwenWork auth JSON is not an object")
        return payload
    aes_key = _os_crypt_key_for(folder)
    plain = decrypt_v10_blob(raw, aes_key)
    payload = json.loads(plain.decode("utf-8"))
    if not isinstance(payload, dict):
        raise CredentialCryptoError("QwenWork auth-v2.dat JSON is not an object")
    return payload


def _jwt_exp_ms(token: str) -> int:
    return jwt_exp_ms(token)


def session_to_account(document: dict, source: str = "") -> dict:
    user = document.get("user") if isinstance(document.get("user"), dict) else {}
    uid = str(user.get("id") or user.get("uid") or document.get("uid") or "")
    name = str(user.get("name") or user.get("username") or "")
    email = str(user.get("email") or "")
    access = str(document.get("token") or document.get("access_token") or document.get("device_token") or "")
    refresh = str(document.get("refreshToken") or document.get("refresh_token") or "")
    extra = {
        "email": email,
        "login_device_id": str(document.get("loginDeviceId") or document.get("machine_id") or ""),
        "login_method": str(document.get("loginMethod") or ""),
        "refresh_strategy": str(document.get("refreshStrategy") or "device_token"),
        "source": source or "import",
        "client_version": IDE_VERSION,
    }
    expires_at = iso_to_ms(document.get("expiresAt") or document.get("expires_at")) or _jwt_exp_ms(access)
    refresh_expires_at = iso_to_ms(
        document.get("refreshTokenExpiresAt") or document.get("refresh_expires_at")
    )
    return {
        "name": name or f"qwenwork-{(uid or 'user')[:8]}",
        "uid": uid,
        "nickname": name,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "provider": CHANNEL_ID,
        "domain": "gateway.qwenwork.cn",
        "extra": extra,
        "status": "active",
    }


def parse_credentials(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("QwenWork credentials must be a JSON object")
    nested_user = body.get("user") if isinstance(body.get("user"), dict) else {}
    nested_auth = body.get("auth") if isinstance(body.get("auth"), dict) else {}
    document = {
        "token": (
            body.get("token")
            or body.get("access_token")
            or body.get("device_token")
            or nested_auth.get("accessToken")
            or nested_auth.get("token")
            or ""
        ),
        "refreshToken": (
            body.get("refreshToken")
            or body.get("refresh_token")
            or nested_auth.get("refreshToken")
            or ""
        ),
        "expiresAt": body.get("expiresAt") or body.get("expires_at") or nested_auth.get("expiresAt"),
        "refreshTokenExpiresAt": (
            body.get("refreshTokenExpiresAt")
            or body.get("refresh_expires_at")
            or nested_auth.get("refreshExpiresAt")
        ),
        "loginDeviceId": body.get("loginDeviceId") or body.get("machine_id") or "",
        "loginMethod": body.get("loginMethod") or "",
        "refreshStrategy": body.get("refreshStrategy") or "device_token",
        "user": {
            "id": (
                nested_user.get("id")
                or nested_user.get("uid")
                or body.get("uid")
                or body.get("user_id")
                or ""
            ),
            "name": nested_user.get("name") or body.get("nickname") or body.get("name") or "",
            "email": nested_user.get("email") or body.get("email") or "",
        },
    }
    parsed = session_to_account(document, source="paste")
    if not parsed["access_token"] and not parsed["refresh_token"]:
        raise ValueError("QwenWork credentials need token / refreshToken")
    if not parsed["access_token"] and parsed["refresh_token"]:
        parsed["access_token"] = parsed["refresh_token"]
    return parsed


def read_official_session(path: Path | None = None) -> dict | None:
    target = path or (qwenwork_user_data_dir() / "auth-v2.dat")
    if not target.is_file():
        return None
    document = decrypt_auth_file(target)
    parsed = session_to_account(document, source=str(target))
    if not parsed["access_token"] and not parsed["refresh_token"]:
        return None
    return parsed


def write_refreshed_auth(path: Path, patch: dict) -> None:
    if not path.is_file():
        return
    document = decrypt_auth_file(path)
    if "access_token" in patch:
        document["token"] = patch["access_token"]
    if "refresh_token" in patch:
        document["refreshToken"] = patch["refresh_token"]
    if "expires_at" in patch and patch["expires_at"]:
        document["expiresAt"] = datetime.fromtimestamp(
            int(patch["expires_at"]) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "refresh_expires_at" in patch and patch["refresh_expires_at"]:
        document["refreshTokenExpiresAt"] = datetime.fromtimestamp(
            int(patch["refresh_expires_at"]) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup = path.with_name(path.name + ".buddy2api.bak")
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    aes_key = _os_crypt_key_for(path.parent)
    blob = encrypt_v10_blob(json.dumps(document, ensure_ascii=False).encode("utf-8"), aes_key)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def discover() -> dict:
    dirs_info = []
    files: list[dict] = []
    existing = existing_uids(CHANNEL_ID)
    for folder in qwenwork_auth_dirs():
        exists = folder.is_dir()
        dirs_info.append({"path": str(folder), "exists": exists, "file_count": 0})
        if not exists:
            continue
        count = 0
        for name in ("auth-v2.dat", "auth.dat"):
            path = folder / name
            if not path.is_file():
                continue
            count += 1
            files.append(_file_meta(path, existing))
        json_fallback = folder / "auth-v2.dat.json"
        if json_fallback.is_file():
            count += 1
            files.append(_file_meta(json_fallback, existing))
        dirs_info[-1]["file_count"] = count
    return discover_summary(CHANNEL_ID, dirs_info, files)


def _file_meta(path: Path, existing: set[str]) -> dict:
    return imported_file_meta(CHANNEL_ID, path, existing, import_discovered)


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed_roots = [folder.resolve() for folder in qwenwork_auth_dirs() if folder.exists()]
    resolved = target.resolve()
    if allowed_roots and not any(is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError("path is outside CB_QWENWORK_AUTH_DIR / official QwenWork data dir")
    if target.suffix.lower() == ".json":
        payload = json.loads(target.read_text(encoding="utf-8"))
        return parse_credentials(payload if isinstance(payload, dict) else {})
    document = decrypt_auth_file(target)
    parsed = session_to_account(document, source=str(target))
    if not parsed.get("access_token") and not parsed.get("refresh_token"):
        raise ValueError("failed to decrypt official QwenWork auth-v2.dat")
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = str(resolved)
    parsed["extra"] = extra
    return parsed


def upsert_account(parsed: dict) -> dict:
    return upsert_account_by_uid(CHANNEL_ID, parsed)
