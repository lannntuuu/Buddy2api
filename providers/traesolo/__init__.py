"""Trae SOLO provider. Isolated solo_work_lite adapter.

Ported from the Go project connectedGraph/trae2api-web (MIT):
将 TRAE SOLO 对话通道（/api/agent/v3/llm_utils_chat）包装为 OpenAI 协议。
与 traework（Work session）是两条独立产品线，凭证与 API 均不复用。
"""

from __future__ import annotations

from typing import Optional

from accounts import auth_manager
from storage import database as db
from providers.model_config import channel_aliases, channel_model_ids
from providers.protocol import ChannelId, QuotaSnapshot
from providers.traesolo import chat, login, quota, store, token
from providers.traesolo.constants import (
    ALIASES,
    CHANNEL_ID,
    DEFAULT_CONFIG,
    DISPLAY_NAME,
    STATIC_MODELS,
)


class TraeSoloProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = True

    def list_models(self) -> list[dict]:
        # 动态表（若有可用账号且缓存新鲜）优先，否则内置 32 个 config_name。
        return [{"id": item} for item in chat.effective_model_ids()]

    def alias_map(self) -> dict[str, str]:
        return channel_aliases(CHANNEL_ID, ALIASES)

    def accepts_model(self, inner: str) -> bool:
        return chat.accepts_model(inner)

    def translate_model(self, model: str) -> str:
        return chat.translate_model(model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        return await chat._pick(set(exclude_ids or ()))

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

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
        return await quota.fetch_quota(account)

    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        return await quota.fetch_checkin(account, force=force)

    async def claim_checkin(self, account: dict) -> dict:
        return await quota.claim_checkin(account)

    async def test_chat(
        self, account: dict, model: str = DEFAULT_CONFIG, prompt: str = "ping"
    ) -> dict:
        return await chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        return await token.refresh_account(account)

    # --- Web 登录闭环（server.py 的 /admin/traesolo/login/* 与 /authorize 调用）---

    def start_login(self, callback_base: str) -> dict:
        return login.start_login(callback_base)

    def login_result(self, pending_id: str) -> dict:
        return login.result(pending_id)

    def cancel_login(self, pending_id: str) -> dict:
        return login.cancel(pending_id)

    async def complete_login_callback(self, raw_url: str, *, require_pending: bool = False) -> dict:
        return await login.complete_from_callback(raw_url, require_pending=require_pending)


PROVIDER = TraeSoloProvider()
