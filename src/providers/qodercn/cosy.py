"""Qoder CN COSY headers.

Byte-identical to the QwenWork / qoder_work family (verified against the
official Qoder CN desktop asar `out/main/index.js`): same aes-128-cbc
user-info envelope, same 1024-bit PKCS#1 v1.5 RSA key, same
`Bearer COSY.{o}.{md5}` signed Authorization. Only the identifiers differ.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from providers.qodercn.constants import (
    COSY_VERSION,
    COSY_VERSION_FROZEN,
    RSA_PUBLIC_KEY_PEM,
)

_PUBLIC_KEY = serialization.load_pem_public_key(RSA_PUBLIC_KEY_PEM.encode("ascii"))


class CosyNotFrozen(RuntimeError):
    """Adapter refuses outbound chat until COSY_VERSION_FROZEN is set."""


def require_frozen() -> None:
    if not COSY_VERSION_FROZEN:
        raise CosyNotFrozen("QoderCN COSY is not frozen; refusing outbound request")


def new_request_id() -> str:
    return uuid.uuid4().hex


def new_aes_key() -> str:
    # Official: randomUUID().replace('-','').substring(0, 16) → 16 ASCII hex chars.
    return uuid.uuid4().hex[:16]


def _aes_cbc_encrypt(plaintext: str, key_chars: str) -> str:
    key = key_chars.encode("utf-8")
    iv = key[:16]
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def rsa_encrypt_key(key_chars: str) -> str:
    cipher = _PUBLIC_KEY.encrypt(key_chars.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def signing_path(url: str) -> str:
    try:
        parsed = urlparse(url)
        path = parsed.path or url
    except ValueError:
        path = url
    if "?" in path:
        path = path.split("?", 1)[0]
    if path.startswith("/algo"):
        path = path[5:]
    return path


def encrypt_user_info(
    *,
    uid: str,
    name: str,
    email: str,
    access_token: str,
    aes_key: str | None = None,
    rsa_cipher_b64: str | None = None,
) -> dict:
    key_chars = aes_key if aes_key is not None else new_aes_key()
    payload = {
        "uid": uid or "",
        "aid": "",
        "name": name or "",
        "email": email or "",
        "security_oauth_token": access_token or "",
    }
    info = _aes_cbc_encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), key_chars)
    cosy_key = rsa_cipher_b64 if rsa_cipher_b64 is not None else rsa_encrypt_key(key_chars)
    return {"key": cosy_key, "info": info, "uid": uid or "", "aes_key": key_chars}


def generate_auth_token(material: dict, *, url: str, body: str, timestamp: int,
                        request_id: str | None = None, ide_version: str = "") -> dict:
    """Build the `Bearer COSY.{encoded}.{md5}` Authorization.

    The header is serialized with the same key order and compact separators as
    the official client, because the signature covers the base64 of that exact
    JSON text. Shape frozen from a captured request:

        {"version":"v1","requestId":…,"info":…,"cosyVersion":"1.1.53","ideVersion":""}
    """
    require_frozen()
    header = {
        "version": "v1",
        "requestId": request_id or new_request_id(),
        "info": material["info"],
        "cosyVersion": COSY_VERSION,
        "ideVersion": ide_version,
    }
    encoded = base64.b64encode(
        json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    path = signing_path(url)
    sign_str = f"{encoded}\n{material['key']}\n{timestamp}\n{body}\n{path}"
    digest = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
    return {
        "Authorization": f"Bearer COSY.{encoded}.{digest}",
        "Cosy-Key": material["key"],
        "Cosy-User": material["uid"],
        "Cosy-Date": str(timestamp),
        "request_id": header["requestId"],
        "sign_str": sign_str,
    }


def auth_headers(
    *,
    uid: str,
    name: str,
    email: str,
    access_token: str,
    url: str,
    body: str,
    timestamp: int,
    request_id: str | None = None,
    aes_key: str | None = None,
    rsa_cipher_b64: str | None = None,
) -> dict[str, str]:
    material = encrypt_user_info(
        uid=uid,
        name=name,
        email=email,
        access_token=access_token,
        aes_key=aes_key,
        rsa_cipher_b64=rsa_cipher_b64,
    )
    token = generate_auth_token(
        material, url=url, body=body, timestamp=timestamp, request_id=request_id
    )
    return {
        "Authorization": token["Authorization"],
        "Cosy-Key": token["Cosy-Key"],
        "Cosy-User": token["Cosy-User"],
        "Cosy-Date": token["Cosy-Date"],
    }