"""Qoder CN provider. Reads the official Qoder CN login cache; chat enabled."""

from __future__ import annotations

from typing import Optional

from accounts import auth_manager
from providers.model_config import channel_aliases, channel_model_ids
from providers.protocol import ChannelId, QuotaSnapshot
from providers.qodercn import chat, store
from providers.qodercn.constants import ALIASES, CHANNEL_ID, DISPLAY_NAME, STATIC_MODELS
from providers.qodercn.token import refresh_account
from providers.trae_shared import pick_with_refresh_fallback


class QoderCnProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = False

    def list_models(self) -> list[dict]:
        return [
            {
                "id": item,
                "display_name": chat.model_meta(item)["display_name"],
                "is_reasoning": bool(chat.model_meta(item)["is_reasoning"]),
                "is_vl": bool(chat.model_meta(item)["is_vl"]),
                "max_input_tokens": int(chat.model_meta(item)["max_input_tokens"]),
            }
            for item in channel_model_ids(CHANNEL_ID, STATIC_MODELS)
        ]

    def fetch_model_rates(self) -> list[dict]:
        return [
            {"id": m["id"], "display_name": m["display_name"], "rate": None,
             "context_window": m["max_input_tokens"], "official": False}
            for m in self.list_models()
        ]

    def alias_map(self) -> dict[str, str]:
        return channel_aliases(CHANNEL_ID, ALIASES)

    def accepts_model(self, inner: str) -> bool:
        value = (inner or "").strip()
        return (
            value in channel_model_ids(CHANNEL_ID, STATIC_MODELS)
            or value in channel_aliases(CHANNEL_ID, ALIASES)
        )

    def translate_model(self, model: str) -> str:
        return chat.translate_model(model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return await pick_with_refresh_fallback(self.id, chat.refresh, exclude_ids=exclude_ids)

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    def accepts_chat(self) -> bool:
        return True

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await chat.chat_completions(payload, api_key_info)

    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body)

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_discovered(path)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        result = await chat.fetch_quota(account)
        if not result.get("ok"):
            return QuotaSnapshot(
                ok=False,
                channel=self.id,
                account_id=int(account.get("id") or 0),
                unit="unknown",
                remaining=None,
                message=str(result.get("message") or result.get("http_status") or ""),
            )
        remaining = result.get("remaining")
        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=int(account.get("id") or 0),
            unit="unknown" if remaining is None else "credit",
            remaining=remaining,
            unsupported=remaining is None,
            message="" if remaining is not None else "quota unit unknown",
        )

    async def test_chat(self, account: dict, model: str = "auto", prompt: str = "请回复：pong") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        return await refresh_account(account)


PROVIDER = QoderCnProvider()