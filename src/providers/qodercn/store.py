"""Read Qoder CN local login credentials.

Qoder CN logs in on two surfaces:

* **Desktop** (`com.qodercn.app.stable`): `auth.v1.dat` + `Local State`.
* **IDE** (`QoderCN`): Chromium storage under `User/globalStorage/state.vscdb`.

Both store the same `v10` + DPAPI os_crypt envelope that `store_common`
already deciphers, so we reuse `decrypt_chromium_v10` / `chromium_os_crypt_key`
and map the decrypted session into the shared account shape.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from storage.credential_crypto import CredentialCryptoError
from providers.qodercn.constants import CHANNEL_ID, IDE_VERSION
from providers.store_common import (
    chromium_os_crypt_key,
    decrypt_chromium_v10,
    dedupe_dirs,
    discover_dirs,
    imported_file_meta,
    is_relative_to,
    iso_to_ms,
    jwt_exp_ms,
    upsert_account as upsert_account_by_uid,
)


def qodercn_data_dirs() -> list[Path]:
    """Candidate user-data roots holding a Qoder CN login cache.

    Env override wins; otherwise we scan the Roaming folders the official
    apps use on Windows (`APPDATA`), mirroring QwenWork/TraeWork conventions.
    """
    dirs: list[Path] = []
    explicit = os.environ.get("CB_QODERCN_AUTH_DIR", "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    dirs.append(Path(appdata) / "com.qodercn.app.stable")
    dirs.append(Path(appdata) / "QoderCN")
    return dedupe_dirs(dirs)


def machine_id() -> str:
    """Per-machine device fingerprint used as `Cosy-MachineId`/`Cosy-MachineToken`.

    The official client keeps it in `%USERPROFILE%\\.qoder-cn\\.auth\\machine_id`;
    it is machine-scoped rather than account-scoped. Outbound chat works without
    it (verified), so a missing/blank file is not an error.
    """
    explicit = os.environ.get("CB_QODERCN_MACHINE_ID", "").strip()
    if explicit:
        return explicit
    home = os.environ.get("USERPROFILE") or str(Path.home())
    path = Path(home) / ".qoder-cn" / ".auth" / "machine_id"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _desktop_auth_file(folder: Path) -> Path | None:
    for name in ("auth.v1.dat", "auth-v2.dat", "auth.dat"):
        p = folder / name
        if p.is_file():
            return p
    return None


def _os_crypt_key_for(folder: Path) -> bytes:
    state_path = folder / "Local State"
    if not state_path.is_file():
        raise CredentialCryptoError("QoderCN Local State is missing")
    local_state = json.loads(state_path.read_text(encoding="utf-8"))
    return chromium_os_crypt_key(local_state, label="QoderCN")


def _decrypt_desktop(path: Path) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"{"):
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    aes_key = _os_crypt_key_for(path.parent)
    plain = decrypt_chromium_v10(raw, aes_key, label="QoderCN auth")
    payload = json.loads(plain.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def session_to_account(document: dict, source: str = "") -> dict:
    user = document.get("user") if isinstance(document.get("user"), dict) else {}
    uid = str(user.get("id") or user.get("uid") or document.get("id") or document.get("uid") or "")
    name = str(user.get("name") or user.get("username") or document.get("name") or "")
    access = str(document.get("token") or document.get("access_token") or document.get("device_token") or "")
    refresh = str(document.get("refreshToken") or document.get("refresh_token") or "")
    # epoch-ms fields used by the IDE flat auth doc (expireTime) and the
    # desktop session (expiresAt ISO); accept both.
    expires_ms = document.get("expireTime") or document.get("expiresAt") or document.get("expires_at")
    refresh_ms = (
        document.get("refreshTokenExpireTime")
        or document.get("refreshTokenExpiresAt")
        or document.get("refresh_expires_at")
    )
    expires_at = expires_ms if _is_epoch_ms(expires_ms) else iso_to_ms(expires_ms)
    refresh_expires_at = refresh_ms if _is_epoch_ms(refresh_ms) else iso_to_ms(refresh_ms)
    if isinstance(expires_at, str) and expires_at.strip().isdigit():
        expires_at = int(expires_at)
    if isinstance(refresh_expires_at, str) and refresh_expires_at.strip().isdigit():
        refresh_expires_at = int(refresh_expires_at)
    if not expires_at and access:
        expires_at = _jwt_exp_ms(access)
    return {
        "name": name or f"qodercn-{(uid or 'user')[:8]}",
        "uid": uid,
        "nickname": name,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "provider": CHANNEL_ID,
        "domain": "qoder.com.cn",
        "extra": {
            "email": str(user.get("email") or document.get("email") or ""),
            "phone": str(user.get("phone") or document.get("phone") or ""),
            "login_device_id": str(
                document.get("loginDeviceId") or document.get("machine_id") or ""
            ),
            "login_source": str(document.get("login_source") or ""),
            "refresh_strategy": str(document.get("refreshStrategy") or "device_token"),
            "source": source or "import",
            "client_version": IDE_VERSION,
        },
        "status": "active",
    }


def _is_epoch_ms(value) -> bool:
    if isinstance(value, (int, float)):
        return value > 10_000_000_000
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) > 10_000_000_000
    return False


def parse_credentials(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("QoderCN credentials must be a JSON object")
    return session_to_account(body, source="paste")


def read_official_session(path: Path | None = None) -> dict | None:
    for folder in qodercn_data_dirs():
        if not folder.is_dir():
            continue
        auth_file = _desktop_auth_file(folder)
        if auth_file is None:
            continue
        parsed = session_to_account(_decrypt_desktop(auth_file), source=str(auth_file))
        if parsed["access_token"] or parsed["refresh_token"]:
            return parsed
    return None


def discover() -> dict:
    return discover_dirs(CHANNEL_ID, qodercn_data_dirs(), _collect_files, _file_meta)


def _collect_files(folder: Path) -> list[Path]:
    out = []
    auth_file = _desktop_auth_file(folder)
    if auth_file is not None:
        out.append(auth_file)
    ide_state = folder / "User" / "globalStorage" / "state.vscdb"
    if ide_state.is_file():
        out.append(ide_state)
    return out


def _file_meta(path: Path, existing: set[str]) -> dict:
    return imported_file_meta(CHANNEL_ID, path, existing, import_discovered)


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed_roots = [f.resolve() for f in qodercn_data_dirs() if f.exists()]
    resolved = target.resolve()
    if allowed_roots and not any(is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError("path is outside CB_QODERCN_AUTH_DIR / official QoderCN data dir")
    if target.name.lower() == "state.vscdb":
        parsed = _read_vscdb(target)
    else:
        parsed = session_to_account(_decrypt_desktop(target), source=str(resolved))
    if not parsed.get("access_token") and not parsed.get("refresh_token"):
        raise ValueError("failed to decrypt official QoderCN login cache")
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = str(resolved)
    parsed["extra"] = extra
    return parsed


def _read_vscdb(state_db: Path) -> dict:
    """Pull `secret://aicoding.auth.userInfo` (and refresh token) from the IDE db."""
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError(f"cannot open QoderCN state.vscdb: {exc}") from exc
    try:
        rows = conn.execute("SELECT key, value FROM ItemTable").fetchall()
    finally:
        conn.close()
    mapping = {str(k): v for k, v in rows}
    raw = mapping.get("secret://aicoding.auth.userInfo")
    if not raw:
        raise ValueError("state.vscdb has no secret://aicoding.auth.userInfo")
    blob = _buffer_payload(raw)
    if not blob or not str(blob[:4], "utf-8", "replace").startswith("v10"):
        raise ValueError("QoderCN IDE auth blob is not a v10 ciphertext")
    # vscdb secrets are encrypted with the same Local State os_crypt key.
    aes_key = _os_crypt_key_for(state_db.parent.parent.parent)
    plain = decrypt_chromium_v10(bytes(blob), aes_key, label="QoderCN IDE auth")
    doc = json.loads(plain.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("QoderCN IDE auth JSON is not an object")
    return session_to_account(doc, source=str(state_db))


def _buffer_payload(raw) -> bytes | None:
    """Normalise a `{"type":"Buffer","data":[...]}` JSON string into raw bytes."""
    if isinstance(raw, dict):
        data = raw.get("data")
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        data = parsed.get("data")
    else:
        return None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, list):
        try:
            return bytes(int(x) for x in data)
        except (TypeError, ValueError):
            return None
    return None


def upsert_account(parsed: dict) -> dict:
    return upsert_account_by_uid(CHANNEL_ID, parsed)


def _encrypt_v10_blob(plain: bytes, aes_key: bytes) -> bytes:
    nonce = os.urandom(12)
    return b"v10" + nonce + AESGCM(aes_key).encrypt(nonce, plain, None)


def write_refreshed_auth(path: Path, patch: dict) -> None:
    if not path.is_file():
        return
    document = _decrypt_desktop(path)
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
    blob = _encrypt_v10_blob(json.dumps(document, ensure_ascii=False).encode("utf-8"), aes_key)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def _jwt_exp_ms(token: str) -> int:
    return jwt_exp_ms(token)