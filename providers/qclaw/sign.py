"""QClaw request signing. JPrx-Ctx is MD5; aizone LLM headers are a separate set."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

from providers.qclaw.constants import (
    CLIENT_VERSION,
    JPRX_RND_CHARS,
    JPRX_RND_LEN,
    JPRX_SIGNATURE_KEY,
)


def random_rnd(length: int = JPRX_RND_LEN) -> str:
    return "".join(secrets.choice(JPRX_RND_CHARS) for _ in range(length))


def jprx_ctx(body: str, gid: str, *, rnd: str | None = None, date: str | None = None) -> str:
    """Official 0.2.36.629 JPrx-Ctx: rnd / date / gid / md5(body+key+rnd+date+gid)."""
    token_gid = gid or "1"
    nonce = rnd if rnd is not None else random_rnd()
    ts = date if date is not None else str(int(time.time()))
    material = f"{body}{JPRX_SIGNATURE_KEY}{nonce}{ts}{token_gid}"
    digest = hashlib.md5(material.encode("utf-8")).hexdigest()
    return f"rnd={nonce}; date={ts}; gid={token_gid}; sg={digest}"


def llm_signature(server_ts: str, nonce: str, body: str) -> str:
    """Best-effort HMAC for aizone. Official algorithm lives in V8 bytecode."""
    message = f"{server_ts}\n{nonce}\n{body}".encode("utf-8")
    return hmac.new(JPRX_SIGNATURE_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def aizone_headers(
    *,
    api_key: str,
    jwt: str,
    guid: str,
    account: str,
    body: str,
    server_ts: str,
    trace_id: str | None = None,
) -> dict[str, str]:
    nonce = uuid.uuid4().hex
    client_ts = str(int(time.time() * 1000))
    signature = llm_signature(server_ts, nonce, body)
    conv = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"QClaw/{CLIENT_VERSION}",
        "X-Trace-Id": trace_id or str(uuid.uuid4()),
        "X-Guid": guid or "1",
        "X-Account": account or "1",
        "X-Qclaw-DeviceToken": guid or "",
        "x-server-timestamp": str(server_ts),
        "x-client-timestamp": client_ts,
        "x-nonce": nonce,
        "x-qclaw-version": CLIENT_VERSION,
        "x-auth-version": "0.0.1",
        "x-conversation-message-id": conv,
        "x-media-attachment": "0",
        "x-signature": signature,
        "x-sign-signature": signature,
    }
    if jwt:
        headers["X-OpenClaw-Token"] = jwt
        headers["x-token"] = jwt
    return headers
