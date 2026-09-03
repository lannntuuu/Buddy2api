"""WorkBuddy provider facade. Implementation stays in proxy.py / auth_manager.py."""

from __future__ import annotations

import sqlite3
from typing import Optional

from accounts import auth_manager
from storage import database as db
from upstream import proxy
from providers.protocol import ChannelId


class WorkBuddyProvider:
    id: ChannelId = "workbuddy"
    display_name = "WorkBuddy / CodeBuddy"
    checkin_supported = True
    # 该通道上游（copilot.tencent.com）支持 reasoning_effort 按模型控制
    supports_reasoning_effort = True

    def list_models(self) -> list[dict]:
        """未设置 `models` → 内置默认；已设置（哪怕空列表）→ 以自定义为准。"""
        try:
            models = db.get_setting("models", None)
        except sqlite3.OperationalError:
            models = None
        if models is None:
            return list(proxy.DEFAULT_MODELS)
        if isinstance(models, list):
            return models
        return []

    def fetch_model_rates(self) -> list[dict]:
        """WorkBuddy 上游直接报 usage.credit，无独立的 per-model 倍率概念；仅返回生效白名单。"""
        return [
            {"id": m["id"], "display_name": m["id"], "rate": None, "context_window": None, "official": False}
            for m in self.list_models()
        ]

    def alias_map(self) -> dict[str, str]:
        return proxy.effective_builtin_aliases()

    def accepts_model(self, inner: str) -> bool:
        ids = {str(item.get("id")) for item in self.list_models() if isinstance(item, dict)}
        return inner in ids or inner in self.alias_map()

    def translate_model(self, model: str) -> str:
        return proxy.resolve_model_alias(model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        return await auth_manager.pick_account_with_fallback(exclude_ids, provider=self.id)

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        log_model = None
        info = api_key_info
        if isinstance(api_key_info, dict) and "_log_model" in api_key_info:
            log_model = api_key_info.get("_log_model")
            info = {k: v for k, v in api_key_info.items() if k != "_log_model"} or None
        return await proxy.proxy_chat_completions(payload, info, log_model=log_model)


PROVIDER = WorkBuddyProvider()
