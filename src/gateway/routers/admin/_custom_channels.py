"""Admin endpoints: CRUD over the `custom_channels` settings key.

Split from the former single-module `gateway/routers/admin.py` (WS-A); each
admin submodule owns one resource domain and its own APIRouter.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from storage import database as db
from providers import custom_channels
from gateway.deps import _check_admin, _read_json_object

router = APIRouter()


# ============================================================
# Custom OpenAI-compat channels (CRUD over the `custom_channels` settings key)
# ============================================================

_PROBE_TIMEOUT_S = 10.0


def _public_definition(definition: dict) -> dict:
    """Strip non-persisted / private fields for the API response. Mirrors the
    spec: api_key is never echoed back; uid/key tail is only present when the
    definition itself records it."""
    return {
        "id": definition.get("id"),
        "display_name": definition.get("display_name"),
        "base_url": definition.get("base_url"),
        "models": list(definition.get("models") or []),
        "aliases": dict(definition.get("aliases") or {}),
        "env_api_key": definition.get("env_api_key") or "",
        "source": definition.get("source") or "",
        "created_at": definition.get("created_at") or 0,
        "updated_at": definition.get("updated_at") or 0,
    }


async def _probe_models(base_url: str, api_key: str) -> dict:
    """GET {base}/models with the provided API key. 10s timeout.

    Returns {"ok": bool, "status_code": int, "error": str, "model_count": int}.
    Never raises — failures are reported in-band so the caller can persist the
    definition and surface a warning to the admin (D7: failed probe does not
    block save).
    """
    url = f"{str(base_url).rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            r = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "buddy2api/probe",
                },
            )
        if r.status_code >= 400:
            snippet = r.text[:200] if r.content else ""
            return {
                "ok": False,
                "status_code": r.status_code,
                "error": snippet or f"HTTP {r.status_code}",
                "model_count": 0,
            }
        try:
            data = r.json()
        except Exception:
            return {
                "ok": False,
                "status_code": r.status_code,
                "error": "upstream returned non-JSON",
                "model_count": 0,
            }
        items = data.get("data") if isinstance(data, dict) else None
        count = len(items) if isinstance(items, list) else 0
        return {
            "ok": True,
            "status_code": r.status_code,
            "error": "",
            "model_count": count,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": str(exc)[:240] or "probe failed",
            "model_count": 0,
        }


def _apply_definition_with_key(
    definition: dict,
    api_key: str | None,
    *,
    allow_probe_for_models: bool = False,
) -> dict:
    """Materialise a Provider, optionally probe with the key, and (when key
    is given) upsert the accounts-table row. Returns the probe result dict
    (status_code / ok / error / model_count / stored_uid) so the route can
    echo it back. Runs in the executor because it hits the DB."""
    from providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(definition)
    result: dict = {
        "ok": True,
        "status_code": 0,
        "error": "",
        "model_count": 0,
        "stored_uid": "",
        "stored_account_id": 0,
    }
    if api_key:
        # Single-key semantics: write the row synchronously here so the route
        # can return the account id. The provider's own upsert enforces
        # SINGLE_ACCOUNT=True (old row goes inactive on rotation).
        parsed = provider.parse_credentials({"api_key": api_key})
        upsert = provider.upsert_account(parsed)
        aid = upsert.get("id")
        result["stored_uid"] = parsed.get("uid", "")
        result["stored_account_id"] = aid or 0
    return result


def _sync_channel_overrides(definition: dict) -> None:
    """Mirror a custom-channel definition's models/aliases into the
    `<cid>.models` / `<cid>.aliases` override keys the 模型配置 page reads.

    Two entry points used to write the same two fields (channel form →
    definition, models page → override key) with the override key winning at
    runtime — editing one silently diverged from the other. Writing BOTH on
    every channel-form save keeps them the single same source of truth; the
    models page keeps working unchanged (it overwrites the same keys).
    """
    cid = str(definition.get("id") or "").strip()
    if not cid:
        return
    try:
        models = definition.get("models")
        if isinstance(models, list):
            db.set_setting(f"{cid}.models", [str(m) for m in models if str(m).strip()])
        aliases = definition.get("aliases")
        if isinstance(aliases, dict):
            db.set_setting(
                f"{cid}.aliases",
                {str(k).strip(): str(v).strip() for k, v in aliases.items()
                 if str(k).strip() and str(v).strip()},
            )
    except Exception:
        pass  # sync is best-effort; the definition itself remains authoritative


@router.get("/admin/channels/custom")
async def admin_list_custom_channels(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    defs = [_public_definition(d) for d in custom_channels.list_definitions()]
    return {"channels": defs}


@router.post("/admin/channels/custom")
async def admin_create_custom_channel(
    request: Request, authorization: str | None = Header(default=None)
):
    """Create a custom OpenAI-compat channel.

    Body (required fields first):
        id            : slug, ^[a-z][a-z0-9_-]{0,31}$
        display_name  : ≤ 40 chars
        base_url      : http:// or https:// URL（明文 http 密钥不加密,仅建议内网）
        models        : non-empty list of model id strings
        aliases       : optional dict alias->id (ids must be in models)
        api_key       : optional. When present, written to accounts (uid=id-{tail});
                        used to probe GET {base}/models. Probe failure → HTTP 200
                        + `warning` block; definition is still saved (D7).
        env_api_key   : optional, must match ^CB_[A-Z0-9_-]+$（与通道 id 字符集对齐）
    """
    _check_admin(authorization)
    data = await _read_json_object(request)

    cid = str(data.get("id") or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="通道 ID 必填")
    if custom_channels.get_definition(cid) is not None:
        raise HTTPException(status_code=409, detail=f"通道 ID「{cid}」已存在")

    reserved = custom_channels.reserved_ids()
    try:
        custom_channels.validate_definition(data, reserved_ids=reserved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Defaults (spec 23 §1): `models` optional → fall back to DEFAULT_MODELS;
    # `env_api_key` left blank → auto-generate "CB_<ID uppercase>". Explicit
    # values still pass through validate_definition's checks above.
    if not data.get("models"):
        data["models"] = list(custom_channels.DEFAULT_MODELS)
    if not data.get("env_api_key"):
        data["env_api_key"] = "CB_" + cid.upper()

    api_key = data.get("api_key")
    api_key = str(api_key).strip() if api_key is not None else ""

    probe_result: dict = {"ok": True, "status_code": 0, "error": "", "model_count": 0}
    upsert_result: dict = {}
    if api_key:
        probe_result = await _probe_models(str(data.get("base_url")), api_key)
        upsert_result = await run_in_threadpool(_apply_definition_with_key, data, api_key)
        upsert_result.update({
            "probe_ok": probe_result["ok"],
            "probe_status": probe_result["status_code"],
            "probe_error": probe_result["error"],
            "probe_model_count": probe_result["model_count"],
        })
    else:
        upsert_result = {"probe_ok": True, "probe_status": 0, "probe_error": "", "probe_model_count": 0}

    # Probe failed but we still want to save the definition (D7). When the
    # probe succeeded AND no explicit `models` was sent by the admin, seed the
    # model's list from the probe response so the first user request works.
    definition_to_save = dict(data)
    definition_to_save.pop("api_key", None)
    if (
        api_key
        and probe_result["ok"]
        and probe_result["model_count"] > 0
        and not data.get("models")
    ):
        # Best-effort: re-derive the model list from a probe call shape (data: [...]).
        # We didn't carry the raw model list out, so leave the admin-supplied
        # models in place. (Admin can re-run GET /admin/channels/{id}/models
        # to populate.)
        pass

    stored = custom_channels.upsert_definition(definition_to_save)
    _sync_channel_overrides(stored)
    out = _public_definition(stored)
    out["status"] = "ok"
    out["account"] = {
        "id": upsert_result.get("stored_account_id", 0),
        "uid": upsert_result.get("stored_uid", ""),
        "updated": bool(api_key and upsert_result.get("stored_account_id")),
    }
    if api_key and not probe_result["ok"]:
        out["status"] = "saved_with_warning"
        out["warning"] = {
            "probe_ok": False,
            "probe_status": probe_result["status_code"],
            "probe_error": probe_result["error"],
        }
    elif probe_result.get("model_count"):
        out["probe"] = {
            "ok": True,
            "status_code": probe_result["status_code"],
            "model_count": probe_result["model_count"],
        }
    return out


@router.put("/admin/channels/custom/{cid}")
async def admin_update_custom_channel(
    cid: str, request: Request, authorization: str | None = Header(default=None)
):
    """Update an existing custom channel.

    Body (all optional except `id` is path-bound):
        display_name, base_url, models, aliases, env_api_key, api_key
    Changing base_url re-runs the probe when api_key is given. Passing a new
    api_key rotates the accounts-table row (uid unchanged when last-8 matches
    the prior key; otherwise a new uid line is added and the previous active
    row is set inactive — same contract as the old gmi/bailian upsert).
    """
    _check_admin(authorization)
    cid = str(cid or "").strip()
    existing = custom_channels.get_definition(cid)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"通道「{cid}」不存在")

    data = await _read_json_object(request)
    # Merge patch onto the stored definition so untouched fields remain valid.
    merged: dict = dict(existing)
    for key in ("display_name", "base_url", "models", "aliases", "env_api_key"):
        if key in data:
            merged[key] = data[key]
    # env_api_key=None clears; explicit "" is treated as cleared too.
    if "env_api_key" in data and data["env_api_key"] is None:
        merged["env_api_key"] = ""

    reserved = custom_channels.reserved_ids(exclude_id=cid)
    try:
        custom_channels.validate_definition(merged, reserved_ids=reserved, exclude_id=cid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Defaults (spec 23 §1): `models` optional → fall back to DEFAULT_MODELS;
    # `env_api_key` left blank (or omitted — the frontend omits it when cleared,
    # see §2) → auto-generate "CB_<ID uppercase>". Explicit non-empty values
    # still passed through validate_definition's checks above; an omitted key
    # that happens to be empty is treated as "clear → regenerate".
    if not merged.get("models"):
        merged["models"] = list(custom_channels.DEFAULT_MODELS)
    if "env_api_key" not in data or not str(merged.get("env_api_key") or "").strip():
        merged["env_api_key"] = "CB_" + cid.upper()

    api_key = data.get("api_key")
    api_key_present = api_key is not None
    api_key = str(api_key).strip() if api_key_present else ""

    base_url_changed = (
        "base_url" in data and str(data["base_url"]).strip() != existing.get("base_url")
    )

    upsert_result: dict = {}
    probe_result: dict = {"ok": True, "status_code": 0, "error": "", "model_count": 0}
    if api_key_present and api_key:
        if base_url_changed:
            probe_result = await _probe_models(str(merged["base_url"]), api_key)
        upsert_result = await run_in_threadpool(_apply_definition_with_key, merged, api_key)

    stored = custom_channels.upsert_definition(merged)
    _sync_channel_overrides(stored)
    out = _public_definition(stored)
    out["status"] = "ok"
    if api_key_present and api_key:
        out["account"] = {
            "id": upsert_result.get("stored_account_id", 0),
            "uid": upsert_result.get("stored_uid", ""),
            "updated": bool(upsert_result.get("stored_account_id")),
        }
        if base_url_changed and not probe_result["ok"]:
            out["status"] = "saved_with_warning"
            out["warning"] = {
                "probe_ok": False,
                "probe_status": probe_result["status_code"],
                "probe_error": probe_result["error"],
            }
    return out


@router.delete("/admin/channels/custom/{cid}")
async def admin_delete_custom_channel(
    cid: str, authorization: str | None = Header(default=None)
):
    """Remove a custom-channel definition. Set every account row for this
    provider to status='inactive' so the dispatcher stops using it (D6 —
    keep logs). Seed channels (gmi / bailian) are deletable too: deleting
    leaves the settings key as an empty list, which is distinct from
    "absent", so seed_initial_definitions() will NOT resurrect them on the
    next boot."""
    _check_admin(authorization)
    cid = str(cid or "").strip()
    existing = custom_channels.get_definition(cid)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"通道「{cid}」不存在")

    # Inactive every account row (preserve rows for log forensics, D6).
    inactive_count = 0
    for row in db.list_accounts(provider=cid):
        if row.get("status") == "active":
            db.update_account(row["id"], {"status": "inactive"})
            inactive_count += 1

    custom_channels.delete_definition(cid)
    return {"status": "ok", "id": cid, "inactive_accounts": inactive_count}
