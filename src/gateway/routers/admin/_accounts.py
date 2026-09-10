"""Admin endpoints: account list/import/scan/CRUD/refresh/test/quota/checkin.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from storage import database as db
from storage.repos.accounts import list_accounts_summary as _list_accounts_summary
import providers
from accounts import auth_manager
from accounts import control_plane
from providers.qclaw.store import upsert_account as upsert_qclaw_account
from upstream import proxy
from gateway.deps import _check_admin, _read_json_object

router = APIRouter()


# ============================================================
# Accounts
# ============================================================

def _account_row(account: dict | None):
    """GET /admin/accounts 行组装(状态摘要 + 管理字段);列表与写操作返回行对象共用。"""
    if account is None:
        return None
    s = auth_manager.get_account_status(account)
    s["phone"] = account.get("phone", "")
    s["account_type"] = account.get("account_type", "")
    s["enterprise_id"] = account.get("enterprise_id", "")
    s["domain"] = account.get("domain", "")
    s["weight"] = int(account.get("weight") or 1)
    s["priority"] = int(account.get("priority") or 0)
    s["credit_limit"] = float(account.get("credit_limit") or 0)
    s["provider"] = account.get("provider") or "workbuddy"
    if account.get("credential_error"):
        s["credential_error"] = account["credential_error"]
    return s


@router.get("/admin/accounts")
async def admin_list_accounts(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    # summary 免凭据解密:状态摘要只消费明文列(repos.accounts.list_accounts_summary);
    # 代价是列表不再产出 credential_error 标记。
    accounts = await run_in_threadpool(_list_accounts_summary)
    return [_account_row(a) for a in accounts]


@router.get("/admin/accounts/pin")
async def admin_get_pins(authorization: str | None = Header(default=None)):
    """返回全量手动锁定映射 {provider: account_id}。"""
    _check_admin(authorization)
    return {"pins": auth_manager.all_manual_pins()}


@router.post("/admin/accounts/pin")
async def admin_set_pin(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """设置/清除某通道手动锁定账号。

    body: {"provider": "workbuddy", "account_id": <int|null|"auto">}。
    account_id 为 null/"auto" 表示取消锁定,改回按权重/优先级调度。
    """
    _check_admin(authorization)
    data = await _read_json_object(request)
    provider = str(data.get("provider") or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    if provider != "workbuddy" and providers.get_provider(provider) is None:
        raise HTTPException(status_code=400, detail=f"Channel '{provider}' is not enabled")
    aid = data.get("account_id")
    if aid is not None and aid != "auto":
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="account_id must be an integer")
        account = db.get_account(aid)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        if str(account.get("provider") or "workbuddy") != provider:
            raise HTTPException(
                status_code=400,
                detail=f"Account {aid} belongs to channel '{account.get('provider')}', not '{provider}'",
            )
    try:
        pins = auth_manager.set_manual_pin(provider, aid)
    except Exception as exc:  # 防御:gateway_settings 写入异常不应 500 成明文
        raise HTTPException(status_code=500, detail=str(exc)[:240]) from exc
    return {"status": "ok", "pins": pins}


@router.get("/admin/accounts/discover")
async def admin_discover_accounts(
    auth_dir: str | None = None,
    channel: str | None = None,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    try:
        return await run_in_threadpool(control_plane.discover, channel, auth_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/accounts/import")
async def admin_import_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    channel = str(data.get("channel") or "workbuddy").strip() or "workbuddy"
    token = str(data.get("preview_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="preview_token is required")
    paths = data.get("paths")
    if paths is not None and (
        not isinstance(paths, list) or not all(isinstance(item, str) for item in paths)
    ):
        raise HTTPException(status_code=400, detail="paths must be an array of strings")
    try:
        result = await run_in_threadpool(
            control_plane.import_channel, channel, token, paths, data.get("auth_dir")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(result, dict) and (result.get("imported") or result.get("updated")):
        control_plane.invalidate_credit_summary_cache()
    return result


@router.post("/admin/accounts/scan")
async def admin_scan_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    auth_dir = data.get("auth_dir") if isinstance(data, dict) else None
    result = await run_in_threadpool(auth_manager.auto_scan_and_import, auth_dir)
    if isinstance(result, dict) and (result.get("imported") or result.get("updated")):
        control_plane.invalidate_credit_summary_cache()
    return result


@router.post("/admin/accounts")
async def admin_add_account(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    provider_id = str(data.get("provider") or data.get("channel") or "").strip()
    if provider_id and provider_id != "workbuddy":
        provider = providers.get_provider(provider_id)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{provider_id}' is not enabled")
        parse_credentials = getattr(provider, "parse_credentials", None)
        if parse_credentials is None:
            raise HTTPException(status_code=400, detail=f"Channel '{provider_id}' does not support pasted credentials")
        try:
            parsed = parse_credentials(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        upsert = getattr(provider, "upsert_account", None)
        if upsert is None and provider_id == "qclaw":
            result = upsert_qclaw_account(parsed)
        elif upsert is None:
            aid = db.add_account({**parsed, "provider": provider_id})
            result = {"id": aid, "updated": False}
        else:
            result = upsert(parsed)
        row = await run_in_threadpool(
            lambda: _account_row(db.get_account(result["id"]))
        )
        return {
            "id": result["id"], "status": "ok", "ok": True,
            "updated": result["updated"], "provider": provider_id, "account": row,
        }
    # Paste raw auth JSON directly
    auth_data = data.get("auth", {})
    account_data = data.get("account", {})
    if not isinstance(auth_data, dict) or not isinstance(account_data, dict):
        raise HTTPException(status_code=400, detail="auth and account must be JSON objects")
    parsed = {
        "name": account_data.get("nickname", data.get("name", "")),
        "uid": account_data.get("uid", ""),
        "nickname": account_data.get("nickname", ""),
        "phone": account_data.get("phoneNumber", ""),
        "account_type": account_data.get("type", "personal"),
        "access_token": auth_data.get("accessToken", ""),
        "refresh_token": auth_data.get("refreshToken", ""),
        "expires_at": auth_data.get("expiresAt", 0),
        "refresh_expires_at": auth_data.get("refreshExpiresAt", 0),
        "domain": auth_data.get("domain", "www.codebuddy.cn"),
        "enterprise_id": account_data.get("enterpriseId", ""),
        "session_state": auth_data.get("sessionState", ""),
    }
    if not parsed["access_token"]:
        raise HTTPException(status_code=400, detail="No accessToken found in auth data")
    # WorkBuddy 手动粘贴时同样固化成 <uid>.info 副本，避免只进 DB、与快照固化导入行为不一致。
    if provider_id == "workbuddy":
        try:
            from accounts import workbuddy_snapshot as _snap

            stored = _snap.write_pasted(str(parsed.get("uid") or ""), parsed)
            parsed.setdefault("extra", {})
            if isinstance(parsed["extra"], dict):
                parsed["extra"]["auth_path"] = str(stored)
                parsed["extra"]["snapshot"] = True
        except Exception:  # noqa: BLE001
            pass
    aid = db.add_account(parsed)
    row = await run_in_threadpool(lambda: _account_row(db.get_account(aid)))
    return {"id": aid, "status": "ok", "ok": True, "account": row}


@router.put("/admin/accounts/{aid}")
async def admin_update_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed = {"name", "nickname", "phone", "status", "weight", "priority", "credit_limit", "credit_baseline"}
    update_data = {k: data[k] for k in allowed if k in data}
    if "status" in update_data and update_data["status"] not in {"active", "inactive", "expired"}:
        raise HTTPException(status_code=400, detail="Invalid account status")
    # 昵称/姓名/电话为纯展示字段,做长度与类型约束,防止脏数据
    for f in ("name", "nickname", "phone"):
        if f in update_data and update_data[f] is not None:
            update_data[f] = str(update_data[f]).strip()[:64]
    if "nickname" in update_data and update_data["nickname"] and "name" not in update_data:
        # 只改昵称时同步 name,使请求日志/分组头展示保持一致
        update_data["name"] = update_data["nickname"]
    if "credit_limit" in update_data and "credit_baseline" not in update_data:
        account = db.get_account(aid)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        update_data["credit_baseline"] = float(account.get("total_credits") or 0)
    for field in ("weight", "priority", "credit_limit", "credit_baseline"):
        if field in update_data:
            try:
                update_data[field] = float(update_data[field]) if field.startswith("credit_") else int(update_data[field])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{field} must be numeric")
    if "weight" in update_data and update_data["weight"] < 1:
        raise HTTPException(status_code=400, detail="weight must be at least 1")
    db.update_account(aid, update_data)
    row = await run_in_threadpool(lambda: _account_row(db.get_account(aid)))
    return {"status": "ok", "ok": True, "account": row}


@router.delete("/admin/accounts/{aid}")
async def admin_delete_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_account(aid)
    control_plane.invalidate_credit_summary_cache()
    return {"status": "ok"}


@router.post("/admin/accounts/resources/batch")
async def admin_resources_batch(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """批量刷新账号额度。body 可选 {"account_ids": [..], "force": bool,
    "max_age_seconds": int};缺省刷全部账号。单账号失败逐条返回,不整体 500。"""
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    ids = data.get("account_ids")
    if ids is not None and (
        not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)
    ):
        raise HTTPException(status_code=400, detail="account_ids must be an array of integers")
    force = bool(data.get("force") or False)
    try:
        max_age_seconds = max(0, int(data.get("max_age_seconds", 60)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_age_seconds must be an integer")
    return await control_plane.fetch_resources_batch(
        ids, force=force, max_age_seconds=max_age_seconds
    )


@router.post("/admin/accounts/{aid}/refresh")
async def admin_refresh_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        refresh = getattr(provider, "refresh", None)
        if refresh is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' does not support refresh")
        try:
            await refresh(account)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:240]) from exc
        return {"status": "ok"}
    ok = await auth_manager.refresh_token(account)
    return {"status": "ok" if ok else "failed"}


@router.post("/admin/accounts/{aid}/test")
async def admin_test_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    data = await _read_json_object(request, allow_empty=True)
    model = data.get("model") if isinstance(data, dict) else None
    prompt = data.get("prompt") if isinstance(data, dict) else None
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        test = getattr(provider, "test_chat", None)
        if test is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' does not support account test")
        default_prompt = "请回复：pong" if channel == "traework" else "ping"
        return await test(account, model or "auto", prompt or default_prompt)
    return await proxy.test_account_chat(account, model or "auto", prompt or "ping")


@router.get("/admin/accounts/{aid}/resources")
async def admin_account_resources(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        fetch_quota = getattr(provider, "fetch_quota", None)
        if fetch_quota is None:
            return {
                "ok": True,
                "unsupported": True,
                "account_id": aid,
                "unit": "unknown",
                "remaining": None,
                "message": "quota API not available",
            }
        snapshot = await fetch_quota(account)
        unit = getattr(snapshot, "unit", "credit") or "credit"
        remaining = getattr(snapshot, "remaining", None)
        unsupported = bool(getattr(snapshot, "unsupported", False)) or unit != "credit"
        credit_remaining = remaining if unit == "credit" and not unsupported else None
        return {
            "ok": bool(getattr(snapshot, "ok", False)),
            "account_id": aid,
            "unit": "credit",
            "remaining": credit_remaining,
            "total_dosage": credit_remaining,
            "unsupported": unsupported or credit_remaining is None,
            "message": getattr(snapshot, "message", "") or ("no credit balance" if credit_remaining is None else ""),
            "packages": [],
        }
    return await auth_manager.fetch_account_resources(account, force=bool(force))


@router.get("/admin/accounts/{aid}/checkin")
async def admin_checkin_status(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        fetch_checkin = getattr(provider, "fetch_checkin", None) if provider else None
        if fetch_checkin is not None:
            return await fetch_checkin(account, force=bool(force))
    return await auth_manager.fetch_checkin_status(account, force=bool(force))


@router.get("/admin/accounts/checkin-status-all")
async def admin_checkin_status_all(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await control_plane.checkin_status_all(force=bool(force))


@router.post("/admin/accounts/{aid}/checkin")
async def admin_claim_checkin(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        claim_checkin = getattr(provider, "claim_checkin", None) if provider else None
        if claim_checkin is not None:
            result = await claim_checkin(account)
            if result.get("ok") or result.get("claimed") or result.get("already_claimed"):
                control_plane.invalidate_credit_summary_cache()
            return result
    result = await auth_manager.claim_daily_checkin(account)
    if result.get("ok"):
        result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
    if result.get("ok") or result.get("claimed") or result.get("already_claimed"):
        control_plane.invalidate_credit_summary_cache()
    return result


@router.post("/admin/accounts/checkin-all")
async def admin_claim_all_checkin(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    channels = data.get("channels") if isinstance(data, dict) else None
    if channels is not None and (
        not isinstance(channels, list) or not all(isinstance(item, str) for item in channels)
    ):
        raise HTTPException(status_code=400, detail="channels must be an array of strings")
    return await control_plane.checkin_all(channels)
