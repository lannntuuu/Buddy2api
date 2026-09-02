"""QClaw provider. Isolated aizone/jprx adapter."""

from __future__ import annotations

from typing import Optional

from accounts import auth_manager
from storage import database as db
from providers.model_config import channel_aliases, channel_model_ids
from providers.protocol import ChannelId, QuotaSnapshot
from providers.qclaw import chat, jprx, oauth, store
from providers.qclaw.constants import (
    ALIASES,
    CHANNEL_ID,
    DISPLAY_NAME,
    STATIC_MODELS,
)


class QClawProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = False

    def list_models(self) -> list[dict]:
        return [{"id": item} for item in channel_model_ids(CHANNEL_ID, STATIC_MODELS)]

    def fetch_model_rates(self) -> list[dict]:
        """QClaw 上游不回报 per-model 倍率，仅返回生效白名单（rate=None, official=False）。"""
        return [
            {"id": m["id"], "display_name": m["id"], "rate": None, "context_window": None, "official": False}
            for m in self.list_models()
        ]

    def alias_map(self) -> dict[str, str]:
        return channel_aliases(CHANNEL_ID, ALIASES)

    def accepts_model(self, inner: str) -> bool:
        value = (inner or "").strip()
        if value in channel_model_ids(CHANNEL_ID, STATIC_MODELS):
            return True
        if value in channel_aliases(CHANNEL_ID, ALIASES):
            return True
        # pool-* 前缀仅在默认配置下放行（上游 pool 模型名更新快于静态表）；
        # 管理员一旦自定义白名单，就以自定义为准，前缀不再旁路闸门。
        if value.startswith("pool-"):
            try:
                return db.get_setting(f"{CHANNEL_ID}.models", None) is None
            except Exception:
                # 与 model_config 的同类调用一致：DB 不可用时按默认配置处理
                return True
        return False

    def translate_model(self, model: str) -> str:
        return chat.translate_model(model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        account = self.pick_account(exclude_ids)
        if account:
            return account
        expired = [
            row
            for row in db.list_accounts(provider=self.id)
            if row.get("status") == "expired"
            and row.get("id") not in (exclude_ids or set())
        ]
        for row in expired:
            try:
                await jprx.refresh_channel(row)
                fresh = db.get_account(row["id"])
                if fresh:
                    db.update_account(fresh["id"], {"status": "active"})
                    return db.get_account(fresh["id"])
            except jprx.JprxError:
                continue
        return None

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

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        # Official balance column is credit-only. QClaw's daily token cap is not 积分.
        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            unsupported=True,
            message="no credit balance",
        )

    async def test_chat(self, account: dict, model: str = "default", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def start_login(self, guid: str) -> dict:
        return await oauth.start_login(guid)

    async def complete_login(self, guid: str, code: str, state: str) -> dict:
        return await oauth.complete_login(guid, code, state)

    async def refresh(self, account: dict) -> dict:
        await jprx.refresh_channel(account)
        from storage import database as db

        fresh = db.get_account(account["id"])
        if fresh:
            db.update_account(fresh["id"], {"status": "active"})
            return db.get_account(fresh["id"]) or fresh
        return account


PROVIDER = QClawProvider()
