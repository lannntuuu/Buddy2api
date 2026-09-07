"""jprx.m.qq.com business calls. Isolated from the WorkBuddy outbound stack."""

from __future__ import annotations

import json

from storage import database as db
from storage.http_pool import get_client

from providers.qclaw.constants import (
    CHANNEL_ID,
    CMD_CREATE_API_KEY,
    CMD_REFRESH_CHANNEL,
    CMD_TIME_SYNC,
    CMD_USER_INFO,
    CMD_WX_LOGIN,
    CMD_WX_LOGIN_STATE,
    JPRX_GATEWAY,
    WEB_VERSION,
)
from providers.host_override import channel_host
from providers.qclaw.sign import jprx_ctx


class JprxError(RuntimeError):
    def __init__(self, message: str, *, payload: dict | None = None, status_code: int = 0):
        super().__init__(message)
        self.payload = payload or {}
        self.status_code = status_code


def unwrap(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise JprxError("jprx response is not an object")
    ret = payload.get("ret")
    if ret not in (0, None):
        raise JprxError(str(payload.get("msg") or payload.get("message") or f"jprx ret={ret}"), payload=payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    resp = data.get("resp") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        resp = payload.get("resp") if isinstance(payload.get("resp"), dict) else data
    if not isinstance(resp, dict):
        return {}
    common = resp.get("common") if isinstance(resp.get("common"), dict) else {}
    code = common.get("code")
    if code not in (0, None):
        raise JprxError(str(common.get("message") or common.get("msg") or f"jprx code={code}"), payload=payload)
    inner = resp.get("data")
    if isinstance(inner, dict):
        return inner
    return resp


def _account_ids(account: dict) -> tuple[str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    guid = str(extra.get("guid") or account.get("guid") or "") or "1"
    user_id = str(account.get("uid") or extra.get("user_id") or "") or "1"
    jwt = str(account.get("refresh_token") or extra.get("jwt") or "")
    return guid, user_id, jwt


def build_headers(account: dict, body: str) -> dict[str, str]:
    guid, user_id, jwt = _account_ids(account)
    headers = {
        "Content-Type": "application/json",
        "X-Version": "1",
        "X-Token": jwt,
        "X-Guid": guid,
        "X-Account": user_id,
        "X-Session": "",
        "X-Qclaw-DeviceToken": guid if guid != "1" else "",
        "JPrx-Ctx": jprx_ctx(body, guid),
    }
    if jwt:
        headers["X-OpenClaw-Token"] = jwt
    return headers


def business_body(extra: dict | None = None) -> dict:
    payload = {"web_version": WEB_VERSION, "web_env": "release"}
    if extra:
        payload.update(extra)
    return payload


async def post_cmd(
    cmd: str,
    account: dict,
    extra: dict | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[dict, str | None]:
    payload = business_body(extra)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    url = f"{channel_host(CHANNEL_ID, 'jprx_gateway', JPRX_GATEWAY)}/data/{cmd}/forward"
    headers = build_headers(account, body)
    client = get_client()
    response = await client.post(url, headers=headers, content=body, timeout=timeout)
    new_token = response.headers.get("X-New-Token")
    try:
        parsed = response.json()
    except ValueError as exc:
        raise JprxError(f"jprx HTTP {response.status_code} non-json", status_code=response.status_code) from exc
    if response.status_code >= 400:
        raise JprxError(f"jprx HTTP {response.status_code}", payload=parsed if isinstance(parsed, dict) else {}, status_code=response.status_code)
    return unwrap(parsed if isinstance(parsed, dict) else {}), new_token


def apply_new_token(account: dict, new_token: str | None) -> dict:
    if not new_token:
        return account

    aid = account.get("id")
    if aid:
        db.update_account(int(aid), {"refresh_token": new_token})
        fresh = db.get_account(int(aid))
        if fresh:
            return fresh
    updated = dict(account)
    updated["refresh_token"] = new_token
    return updated


async def refresh_channel(account: dict) -> dict:
    data, token = await post_cmd(CMD_REFRESH_CHANNEL, account)
    account = apply_new_token(account, token)
    extra = dict(account.get("extra") or {})
    channel_token = data.get("openclaw_channel_token")
    if channel_token:
        extra["openclaw_channel_token"] = channel_token
        aid = account.get("id")
        if aid:
            db.update_account(int(aid), {"extra": extra})
    return data


async def create_api_key(account: dict) -> dict:
    data, token = await post_cmd(CMD_CREATE_API_KEY, account)
    apply_new_token(account, token)
    return data


async def get_user_info(account: dict, extra: dict | None = None) -> dict:
    data, token = await post_cmd(CMD_USER_INFO, account, extra)
    apply_new_token(account, token)
    return data


async def wx_login_state(account: dict, extra: dict | None = None) -> dict:
    data, token = await post_cmd(CMD_WX_LOGIN_STATE, account, extra)
    apply_new_token(account, token)
    return data


async def wx_login(account: dict, extra: dict) -> dict:
    data, token = await post_cmd(CMD_WX_LOGIN, account, extra)
    apply_new_token(account, token)
    return data
