"""OpenAI-compat channel base class (definition-driven).

Extracted line-for-line from src/providers/gmi/{chat,store,quota,__init__}.py:
the protocol surface (headers, error shape, SSE passthrough, usage logging,
accounts upsert contract) is identical across "one URL + one API key" style
platforms (Bailian, GMI, and future custom channels). Every channel-specific
difference (channel id, display name, default base URL, static model list,
aliases, models cache TTL, env key name) comes from the definition dict that
the provider is constructed with — see providers.custom_channels.

Definition dict shape (as persisted in the `custom_channels` settings key):

    {
        "id": "bailian",
        "display_name": "阿里百炼 Bailian",
        "base_url": "https://llm-....maas.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus"],
        "aliases": {"auto": "qwen-plus"},
        "env_api_key": "CB_BAILIAN_API_KEY",
        "source": "seed",
        "created_at": 1700000000,
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncGenerator, Optional

import httpx

from providers.host_override import channel_host
from providers.model_config import channel_aliases, channel_model_ids
from storage import database as db

logger = logging.getLogger("openai_compat")

EP_MODELS = "/models"
EP_CHAT = "/chat/completions"

# Per-request client cap (one logical account = one API key for OpenAI-compat
# platforms, so we never pool / rotate).
SINGLE_ACCOUNT = True

DEFAULT_MODELS_CACHE_TTL = 600.0


class OpenAICompatProvider:
    """One instance per channel definition. All channel differences live in
    `definition`; the behaviour below is shared verbatim with the former
    gmi/bailian packages."""

    def __init__(self, definition: dict):
        d = definition if isinstance(definition, dict) else {}
        self._def = d
        self.id: str = str(d.get("id") or "").strip()
        self.display_name: str = str(d.get("display_name") or self.id)
        self.checkin_supported = False

        self._base_url_default = str(d.get("base_url") or "").strip().rstrip("/")
        self._static_models: tuple[str, ...] = tuple(
            str(m) for m in (d.get("models") or []) if str(m).strip()
        )
        self._aliases: dict[str, str] = dict(d.get("aliases") or {})
        self._models_cache_ttl = float(
            d.get("models_cache_ttl") or DEFAULT_MODELS_CACHE_TTL
        )
        self._env_api_key = str(d.get("env_api_key") or "").strip()

        # Per-instance transport/client/models cache (the old packages held
        # these at module level; definitions can change at runtime so the
        # cache follows the instance).
        self._transport: Optional[httpx.AsyncBaseTransport] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None
        self._models_cache: dict = {"fetched_at": 0.0, "ids": []}

    # ── test transport escape hatch (parity with traesolo) ───────
    def set_transport(self, transport: Optional[httpx.AsyncBaseTransport]) -> None:
        """Tests swap the transport; rebuild client lazily."""
        self._transport = transport
        self._client = None
        self._client_loop = None

    def _get_client(self) -> httpx.AsyncClient:
        """One long-lived httpx client per running loop. Reused for chat, models,
        and quota probes — keeps TLS handshakes to one per process per host."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if (
            self._client is None
            or self._client.is_closed
            or (loop is not None and self._client_loop is not loop)
        ):
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=15.0), transport=self._transport
            )
            self._client_loop = loop
        return self._client

    # ────────────────────────── auth header ──────────────────────────

    def _auth_headers(self, account: dict, stream: bool) -> dict[str, str]:
        token = str(account.get("access_token") or "").strip()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": f"buddy2api/{self.id}",
        }

    def _base_url(self, account: dict) -> str:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        host = str(
            extra.get("base_url")
            or account.get("domain")
            or channel_host(self.id, "base_url", self._base_url_default)
        ).rstrip("/")
        return host or self._base_url_default

    # ────────────────────────── model resolution ──────────────────────

    def accepts_model(self, inner: str) -> bool:
        """True iff we recognise this id (alias, dynamic list or static list).

        Whitelist/aliases honour admin customisation (<id>.models / <id>.aliases
        settings), same contract as qclaw / traesolo.
        """
        value = (inner or "").strip()
        if value in self._channel_aliases():
            return True
        if value in self.effective_model_ids():
            return True
        return False

    def translate_model(self, model: str) -> str:
        return self._channel_aliases().get(model, model)

    def _channel_aliases(self) -> dict[str, str]:
        return channel_aliases(self.id, self._aliases)

    def effective_model_ids(self) -> list[str]:
        """当前生效模型白名单：管理员自定义 > 动态 /v1/models > 内置静态表。"""
        return channel_model_ids(self.id, self._effective_default_ids())

    def _effective_default_ids(self) -> list[str]:
        return self.dynamic_model_ids() or list(self._static_models)

    def dynamic_model_ids(self) -> list[str]:
        """最近一次 /v1/models 动态拉取结果（无缓存时为空列表）。"""
        return list(self._models_cache["ids"])

    async def refresh_model_ids(self, force: bool = False) -> list[str]:
        """Refresh the dynamic model id list from /v1/models. Cached MODELS_CACHE_TTL."""
        now = time.time()
        if (
            not force
            and self._models_cache["ids"]
            and (now - self._models_cache["fetched_at"]) < self._models_cache_ttl
        ):
            return list(self._models_cache["ids"])
        accounts = db.get_active_accounts(self.id) or []
        account = accounts[0] if accounts else None
        if not account:
            # Try env-key bootstrap (no admin UI interaction required).
            account = self.ensure_env_account()
        if not account:
            self._models_cache.update({"fetched_at": now, "ids": list(self._static_models)})
            return list(self._static_models)
        try:
            client = self._get_client()
            r = await client.get(
                f"{self._base_url(account)}{EP_MODELS}",
                headers=self._auth_headers(account, stream=False),
            )
            if r.status_code >= 400:
                raise httpx.HTTPStatusError("models fetch failed", request=r.request, response=r)
            data = r.json() if r.content else {}
            ids = []
            for item in (data.get("data") or []):
                mid = item.get("id") if isinstance(item, dict) else None
                if isinstance(mid, str) and mid:
                    ids.append(mid)
            if ids:
                self._models_cache.update({"fetched_at": now, "ids": ids})
                return ids
        except Exception as exc:
            logger.warning("%s /v1/models refresh failed: %s", self.id, exc)
        self._models_cache.update({"fetched_at": now, "ids": list(self._static_models)})
        return list(self._static_models)

    def cached_model_ids(self) -> list[str]:
        """Sync accessor used by list_models(): 当前生效白名单（自定义 > 动态 > 静态）。"""
        return self.effective_model_ids()

    # ────────────────────────── account pick ──────────────────────────

    async def _pick_account(self, exclude_ids: set[int] | None = None):
        """Pick the single active account. Single-key platform → no rotation."""
        if self.ensure_env_account() is None and not db.list_accounts(provider=self.id):
            return None
        from accounts import auth_manager

        return auth_manager.pick_account(set(exclude_ids or ()), provider=self.id)

    # ────────────────────────── usage logging ─────────────────────────

    def _record(
        self,
        api_key_info: dict | None,
        account: dict | None,
        *,
        model: str,
        stream: bool,
        finish_reason: str,
        status_code: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        error_msg: str = "",
        t0: float = 0.0,
        usage_payload: dict | None = None,
    ):
        """Fire-and-forget log write — mirrors upstream/proxy._log_request."""
        elapsed_ms = int((time.time() - t0) * 1000) if t0 else 0
        cache_read, cache_creation = 0, 0
        try:
            from providers.store_common import extract_cache_tokens

            cache_read, cache_creation = extract_cache_tokens(usage_payload)
        except Exception:
            pass
        _known_cache_keys = (
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "prompt_tokens_details",
        )
        credit_source = (
            "live" if usage_payload and any(k in usage_payload for k in _known_cache_keys) else None
        )
        payload = {
            "api_key_id": (api_key_info or {}).get("id"),
            "api_key_name": (api_key_info or {}).get("name"),
            "account_id": (account or {}).get("id") if account else None,
            "account_name": (account or {}).get("name") if account else None,
            "provider": self.id,
            "model": model,
            "stream": 1 if stream else 0,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "credit": 0,
            "finish_reason": finish_reason,
            "duration_ms": elapsed_ms,
            "status_code": status_code,
            "error_msg": error_msg[:500] if error_msg else "",
            "client": (api_key_info or {}).get("_client_tag"),
            "client_version": (api_key_info or {}).get("_client_version"),
            "usage_json": json.dumps(usage_payload, ensure_ascii=False) if usage_payload else None,
            "credit_source": credit_source,
            "increment_usage": True,
        }
        try:
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(None, db.record_request, payload)
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        except RuntimeError:
            # No running loop (test harness). Fall back to sync.
            try:
                db.record_request(payload)
            except Exception:
                pass

    # ────────────────────────── non-streaming ─────────────────────────

    async def _run_non_stream(
        self, account: dict, payload: dict, api_key_info: dict | None, model: str
    ) -> tuple:
        """Single request → single JSON. Returns ('json', dict) or ('error', tuple)."""
        t0 = time.time()
        # Ensure usage is reported in non-stream too.
        body = {**payload, "stream": False}
        body.setdefault("stream_options", {"include_usage": True})
        try:
            client = self._get_client()
            r = await client.post(
                f"{self._base_url(account)}{EP_CHAT}",
                headers=self._auth_headers(account, stream=False),
                json=body,
            )
        except httpx.HTTPError as exc:
            self._record(
                api_key_info, account, model=model, stream=False, finish_reason="network_error",
                status_code=502, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg=str(exc), t0=t0,
            )
            return "error", (502, {"error": {"message": f"upstream network error: {exc}", "type": "server_error"}})

        if r.status_code >= 400:
            body_txt = r.text[:500]
            self._record(
                api_key_info, account, model=model, stream=False, finish_reason="error",
                status_code=r.status_code, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg=body_txt, t0=t0,
            )
            # Pass through upstream error envelope if it parses as JSON.
            try:
                upstream_err = r.json()
            except Exception:
                upstream_err = {"error": {"message": body_txt, "type": "upstream_error"}}
            return "error", (r.status_code, upstream_err)

        try:
            data = r.json()
        except json.JSONDecodeError:
            self._record(
                api_key_info, account, model=model, stream=False, finish_reason="error",
                status_code=502, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg="upstream returned non-JSON", t0=t0,
            )
            return "error", (502, {"error": {"message": "upstream returned non-JSON", "type": "server_error"}})

        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        finish = "stop"
        for choice in (data.get("choices") or []):
            if isinstance(choice, dict) and choice.get("finish_reason"):
                finish = choice["finish_reason"]
                break
        self._record(
            api_key_info, account, model=model, stream=False, finish_reason=finish,
            status_code=200, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            t0=t0, usage_payload=usage,
        )
        return "json", data

    # ────────────────────────── streaming ────────────────────────────

    @staticmethod
    def _sse_passthrough_line(parsed_json: dict | None) -> bytes:
        if parsed_json is None:
            return b""
        return f"data: {json.dumps(parsed_json, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

    async def _stream_chat(
        self, account: dict, payload: dict, api_key_info: dict | None, model: str
    ) -> AsyncGenerator[str, None]:
        """Stream chunks out as SSE strings. Accumulate usage for logging; we do
        not synthesise a final usage chunk — the upstream emits its own (because we
        set stream_options.include_usage)."""
        t0 = time.time()
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        usage_payload: dict | None = None
        final_finish = "stop"
        last_status = 200
        error_msg = ""
        emitted = False

        try:
            client = self._get_client()
            async with client.stream(
                "POST",
                f"{self._base_url(account)}{EP_CHAT}",
                headers=self._auth_headers(account, stream=True),
                json=body,
            ) as r:
                last_status = r.status_code
                if r.status_code >= 400:
                    body_txt = await r.aread()
                    error_msg = body_txt.decode("utf-8", "replace")[:500]
                    self._record(
                        api_key_info, account, model=model, stream=True, finish_reason="error",
                        status_code=r.status_code, prompt_tokens=0, completion_tokens=0,
                        total_tokens=0, error_msg=error_msg, t0=t0,
                    )
                    # Emit an OpenAI-shaped error chunk so clients see something.
                    err_obj = {"error": {"message": error_msg, "type": "upstream_error"}}
                    yield self._sse_passthrough_line(err_obj).decode("utf-8")
                    yield "data: [DONE]\n\n"
                    return

                buffer = b""
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        raw = line
                        text = raw.decode("utf-8", "replace")
                    else:
                        text = line
                        raw = text.encode("utf-8")

                    # Forward unchanged to client (the SSE line already includes its
                    # trailing \n\n via iter_lines when the upstream flushes them).
                    # We split into individual data: lines so the client gets
                    # packet-aligned chunks.
                    if text.startswith("data:"):
                        emitted = True
                        # Try to grab usage off the last frame for logging.
                        data_part = text[5:].strip()
                        if data_part and data_part != "[DONE]":
                            try:
                                parsed = json.loads(data_part)
                                usage = parsed.get("usage") if isinstance(parsed, dict) else None
                                if isinstance(usage, dict):
                                    usage_payload = usage
                                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                                    completion_tokens = int(usage.get("completion_tokens") or 0)
                                    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                                for ch in (parsed.get("choices") or []) if isinstance(parsed, dict) else []:
                                    fr = ch.get("finish_reason") if isinstance(ch, dict) else None
                                    if fr:
                                        final_finish = fr
                            except json.JSONDecodeError:
                                pass
                        yield text + "\n\n"
                    else:
                        # event: lines, comments, blanks — forward verbatim.
                        yield text + "\n"
        except httpx.HTTPError as exc:
            error_msg = str(exc)
            last_status = 502
            self._record(
                api_key_info, account, model=model, stream=True, finish_reason="network_error",
                status_code=502, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, error_msg=error_msg, t0=t0,
            )
            yield self._sse_passthrough_line({"error": {"message": error_msg, "type": "server_error"}}).decode("utf-8")
            yield "data: [DONE]\n\n"
            return

        if not emitted:
            self._record(
                api_key_info, account, model=model, stream=True, finish_reason="empty_response",
                status_code=last_status, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                error_msg="no SSE chunks received", t0=t0,
            )
            return
        self._record(
            api_key_info, account, model=model, stream=True, finish_reason=final_finish,
            status_code=last_status, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, t0=t0, usage_payload=usage_payload,
        )

    # ────────────────────────── entry point ───────────────────────────

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        """Provider entry: router calls us with model already aliased to upstream id."""
        # Refresh model cache opportunistically (idempotent, cheap).
        try:
            await self.refresh_model_ids()
        except Exception:
            pass

        account = await self._pick_account()
        if not account:
            env_hint = f" (set {self._env_api_key}" if self._env_api_key else ""
            if env_hint:
                env_hint += " or import via admin UI)"
            else:
                env_hint = " (import via admin UI)"
            return "error", (503, {
                "error": {
                    "message": f"No active {self.display_name} account configured{env_hint}",
                    "type": "channel_unavailable",
                    "code": "channel_unavailable",
                }
            })

        inner = str(payload.get("model") or self.default_model())
        body = dict(payload)
        body["model"] = inner
        wants_stream = bool(body.get("stream"))

        if wants_stream:
            return "stream", self._stream_chat(account, body, api_key_info, inner)
        return await self._run_non_stream(account, body, api_key_info, inner)

    # ────────────────────────── self-test (manual) ────────────────────

    async def test_chat(self, account: dict, model: str = "auto", prompt: str = "ping") -> dict:
        """Admin-UI 'Test' button entry."""
        inner = self.translate_model(model)
        payload = {
            "model": inner,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32,
            "stream": False,
        }
        try:
            client = self._get_client()
            r = await client.post(
                f"{self._base_url(account)}{EP_CHAT}",
                headers=self._auth_headers(account, stream=False),
                json=payload,
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "message": str(exc), "status_code": 0}
        snippet = r.text[:400]
        return {
            "ok": r.status_code < 400,
            "status_code": r.status_code,
            "model": inner,
            "snippet": snippet,
        }

    # ────────────────────────── store: parse/upsert/env ──────────────

    @property
    def default_base_url(self) -> str:
        return self._base_url_default

    def default_model(self) -> str:
        return self._static_models[0] if self._static_models else "auto"

    @staticmethod
    def _normalize_key(raw: str) -> str:
        """Trim whitespace / accidental JSON wrapping around a raw key."""
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

    def parse_credentials(self, body: dict) -> dict:
        """Parse the admin-UI paste payload into an `accounts` row."""
        if not isinstance(body, dict):
            raise ValueError(f"{self.display_name} credentials must be a JSON object")
        raw = (
            body.get("api_key")
            or body.get("apiKey")
            or body.get("key")
            or body.get("token")
            or ""
        )
        key = self._normalize_key(str(raw))
        if not key:
            raise ValueError(f"{self.display_name} credentials need a non-empty api_key")
        nickname = str(body.get("nickname") or body.get("name") or self.id).strip() or self.id
        base_url = str(body.get("base_url") or self._base_url_default).strip() or self._base_url_default
        return {
            "name": f"{self.id}-{nickname[:24]}",
            "uid": f"{self.id}-{key[-8:]}",
            "nickname": nickname,
            "account_type": "api_key",
            "access_token": key,
            "refresh_token": "",
            "expires_at": 0,
            "refresh_expires_at": 0,
            "domain": base_url,
            "provider": self.id,
            "status": "active",
            "weight": 1,
            "priority": 0,
            "extra": {
                "base_url": base_url,
                "source": "paste",
            },
        }

    def discover(self) -> dict:
        """No IDE directory for OpenAI-compat channels. Only an env-var scan."""
        return {
            "channel": self.id,
            "dirs": [],
            "files": [],
            "file_count": 0,
            "valid_count": 0,
            "importable_count": 0,
            "preview_token": "",
        }

    def import_path(self, path: str) -> dict:
        """Import from a file path (txt or json)."""
        import os as _os

        target = _os.path.expanduser(path)
        if not _os.path.isfile(target):
            raise ValueError(f"path is not a file: {path}")
        with open(target, "r", encoding="utf-8") as fh:
            raw = fh.read()
        return self.upsert_account(self.parse_credentials({"api_key": raw}))

    def upsert_account(self, parsed: dict) -> dict:
        """Insert or update by (provider, uid). Replaces the key in-place.

        Same-uid rows are updated (in-place token refresh); different-uid
        rows are appended as separate active rows — the auth_manager scheduler
        picks among all active rows by weight/priority, so the same channel
        can hold multiple keys concurrently (旋转池). Previously this method
        deactivated every other active row on the same provider; that single-
        active constraint was lifted in spec §3 (v2.2.3): keys are not "single-
        use", they coexist by uid, all eligible for scheduling until
        individually disabled.

        Returns the store contract `{"id": aid, "updated": bool, "row": ...}`
        so gateway/server.py POST /admin/accounts reads `result["id"]` /
        `result["updated"]` without a TypeError on import.
        """
        key = parsed["access_token"]
        base = parsed.get("domain") or self._base_url_default
        existing = None
        for row in db.list_accounts(provider=self.id):
            if row.get("uid") == parsed.get("uid"):
                existing = row
                break
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

    def ensure_env_account(self) -> Optional[dict]:
        """If the channel's env key is set and no active account exists, create one.

        Idempotent: returns the existing active row if present, otherwise inserts
        a fresh row keyed by the env value's last-8-chars and returns the full row.
        """
        if not self._env_api_key:
            return None
        env_key = os.environ.get(self._env_api_key, "").strip()
        if not env_key:
            return None
        norm = self._normalize_key(env_key)
        if not norm:
            return None
        target_uid = f"{self.id}-{norm[-8:]}"
        for row in db.list_accounts(provider=self.id):
            if row.get("status") == "active":
                return row
        parsed = self.parse_credentials({"api_key": norm, "nickname": "env"})
        parsed["uid"] = target_uid
        parsed["name"] = f"{self.id}-env"
        parsed["extra"]["source"] = "env"
        result = self.upsert_account(parsed)
        row = result.get("row")
        if isinstance(row, dict) and row.get("id") == result.get("id"):
            return row
        return db.get_account(result["id"])

    # ────────────────────────── quota (unsupported) ──────────────────

    async def fetch_quota(self, account: dict):
        from providers.protocol import QuotaSnapshot

        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            unsupported=True,
            message=(
                f"{self.id}: per-account credit balance endpoint not available; "
                "usage tracked via request logs"
            ),
            extra={},
        )

    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        return {
            "ok": False,
            "channel": self.id,
            "account_id": int(account.get("id") or 0),
            "enable": False,
            "already_claimed": False,
            "message": f"{self.id} has no check-in endpoint",
        }

    async def claim_checkin(self, account: dict) -> dict:
        return await self.fetch_checkin(account)

    # ────────────────────────── model surface ────────────────────────

    def list_models(self) -> list[dict]:
        ids = self.effective_model_ids()
        return [{"id": mid} for mid in ids]

    def fetch_model_rates(self) -> list[dict]:
        """上游 /v1/models 不回报倍率，仅返回生效白名单（rate=None, official=False）。"""
        return [
            {"id": m["id"], "display_name": m["id"], "rate": None, "context_window": None, "official": False}
            for m in self.list_models()
        ]

    async def refresh_dynamic_models(self, force: bool = False) -> bool:
        ids = await self.refresh_model_ids(force=force)
        return bool(ids)

    def alias_map(self) -> dict[str, str]:
        return self._channel_aliases()

    # ────────────────────────── account surface ──────────────────────

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        from accounts import auth_manager

        self.ensure_env_account()
        return auth_manager.pick_account(set(exclude_ids or ()), provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        from accounts import auth_manager

        self.ensure_env_account()
        return await auth_manager.pick_account_with_fallback(set(exclude_ids or ()), provider=self.id)

    async def has_usable_account(self) -> bool:
        # Lazy env-bootstrap so has_usable_account() works without an admin UI step.
        self.ensure_env_account()
        return await self.pick_account_with_fallback() is not None

    async def refresh(self, account: dict) -> dict:
        # No refresh path on a single-key OpenAI-compat platform. Re-import if key rotated.
        return {"status": "noop", "message": f"{self.id} uses a static API key; rotate by re-importing"}
