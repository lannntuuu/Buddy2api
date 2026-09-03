"""GMI Cloud (api.gmi-serving.com) provider — OpenAI-compatible.

Single-API-key platform; one row in `accounts` per import. Per-key token usage
is logged through the standard db.record_request path so existing Dashboard /
api_key_daily_usage surfaces work without modification.
"""

from __future__ import annotations

from typing import Optional

from providers.gmi import chat as _chat
from providers.gmi import quota, store
from providers.gmi.constants import CHANNEL_ID, DISPLAY_NAME
from providers.protocol import ChannelId, QuotaSnapshot


class GmiProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = False

    # ── model surface ────────────────────────────────────────────
    def list_models(self) -> list[dict]:
        ids = _chat.effective_model_ids()
        return [{"id": mid} for mid in ids]

    def fetch_model_rates(self) -> list[dict]:
        """GMI 上游 /v1/models 不回报倍率，仅返回生效白名单（rate=None, official=False）。"""
        return [
            {"id": m["id"], "display_name": m["id"], "rate": None, "context_window": None, "official": False}
            for m in self.list_models()
        ]

    async def refresh_dynamic_models(self, force: bool = False) -> bool:
        ids = await _chat.refresh_model_ids(force=force)
        return bool(ids)

    def alias_map(self) -> dict[str, str]:
        return _chat.channel_aliases(CHANNEL_ID, _chat.ALIASES)

    def accepts_model(self, inner: str) -> bool:
        return _chat.accepts_model(inner)

    def translate_model(self, model: str) -> str:
        return _chat.translate_model(model)

    # ── account surface (single-key, no rotation) ────────────────
    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        from accounts import auth_manager

        store.ensure_env_account()
        return auth_manager.pick_account(set(exclude_ids or ()), provider=CHANNEL_ID)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        from accounts import auth_manager

        store.ensure_env_account()
        return auth_manager.pick_account_with_fallback(set(exclude_ids or ()), provider=CHANNEL_ID)

    async def has_usable_account(self) -> bool:
        # Lazy env-bootstrap so has_usable_account() works without an admin UI step.
        store.ensure_env_account()
        return await self.pick_account_with_fallback() is not None

    # ── chat entry (the only path the gateway cares about) ───────
    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await _chat.chat_completions(payload, api_key_info)

    # ── admin-UI hooks ───────────────────────────────────────────
    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body)

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_path(path)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        return await quota.fetch_quota(account)

    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        return await quota.fetch_checkin(account, force=force)

    async def claim_checkin(self, account: dict) -> dict:
        return await quota.claim_checkin(account)

    async def test_chat(self, account: dict, model: str = "auto", prompt: str = "ping") -> dict:
        return await _chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        # No refresh path on a single-key OpenAI-compat platform. Re-import if key rotated.
        return {"status": "noop", "message": "gmi uses a static API key; rotate by re-importing"}


PROVIDER = GmiProvider()