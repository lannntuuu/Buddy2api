"""WorkBuddy provider facade. Implementation stays in proxy.py / auth_manager.py."""

from __future__ import annotations

import sqlite3
from typing import Optional

from accounts import auth_manager
from storage import database as db
from upstream import proxy
from providers.protocol import ChannelId
from providers.workbuddy import models


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
        """返回当前生效白名单里每个模型的明细（含官方消耗倍率 rate）。

        上游 /v3/config 提供官方 credits（x 系数），优先用缓存的官方明细
        （official=True）；缓存未命中（无可用账号/未拉过）时回退到当前白名单
        （official=False，rate 仍为 None，与旧行为一致）。

        大小写不敏感官方 lookup：官方 id 可能是全小写（如 deepseek-v4.1-flash），
        白名单里常是混合大小写，因此用 lower() 建索引再以 lower() 查询。
        """
        effective = self.list_models()
        # 大小写不敏感映射：官方 id 统一以 lower() 做 key
        details = {d["id"].lower(): d for d in models.official_model_details()}
        out: list[dict] = []
        for m in effective:
            mid = str(m.get("id") or "")
            if not mid:
                continue
            d = details.get(mid.lower())
            if d is not None and d.get("official"):
                out.append({
                    "id": mid,
                    "display_name": d.get("display_name") or mid,
                    "rate": d.get("rate"),
                    "context_window": d.get("context_window"),
                    "official": True,
                })
            else:
                out.append({
                    "id": mid,
                    "display_name": mid,
                    "rate": None,
                    "context_window": None,
                    "official": False,
                })
        return out

    async def refresh_dynamic_models(self, force: bool = False) -> bool:
        """强制重新拉取官方模型表（/v3/config），成功缓存 1h。"""
        return await models.refresh_dynamic_models(force=force)

    def official_model_details(self) -> list[dict]:
        """完整官方可见模型明细（不含白名单过滤），供模型选择弹窗使用。

        返回 models.official_model_details()——即按 §1 过滤后（剔除生图类等
        maxInputTokens 非正整数条目）的官方可见模型列表，每项含
        id / display_name / rate / context_window / official=True。
        TTL 外（未拉过或缓存过期）返回空 list。control_plane 通过
        getattr(provider, "official_model_details", None) 探测调用。
        """
        return models.official_model_details()

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
