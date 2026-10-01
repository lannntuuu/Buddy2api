"""MiniMax Code provider（通道 id ``minimax_code``）。

**纯门面**：只做"网关契约 ↔ 通道内模块"的名字装配，零业务逻辑、零协议事实——
* 协议事实（端点/头/错误码/能力位）在 ``constants.py``（spec 行号可追溯）；
* 方言翻译（OpenAI ↔ Anthropic Messages、SSE 状态机）在 ``translate.py``；
* 凭据发现/入库在 ``store.py``（只读，spec:694 不回写客户端 auth.json）；
* OAuth2 换票在 ``token.py``；
* 装配与调度（选号/重试/限流/日志）在 ``chat.py``。

模板照 ``providers/qodercn/__init__.py``（113 行）：3 个属性 + 9 个核心方法
（list_models / alias_map / accepts_model / translate_model / pick_account /
pick_account_with_fallback / has_usable_account / chat_completions /
fetch_model_rates）+ 能力 Mixin。

能力面（对应 ``providers/protocol.py`` 的 runtime_checkable Protocol）::

    Provider          ✓  核心 9 方法
    StoreCapable      ✓  discover / import_path / parse_credentials
    UpsertCapable     ✓  upsert_account（粘贴建号/按 uid 更新）
    RefreshCapable    ✓  refresh（OAuth2 refresh_token 换代）
    TestChatCapable   ✓  test_chat（管理页「测试」按钮）
    QuotaCapable      ✓  fetch_quota —— **恒 unsupported**：MiniMax 无额度查询接口
                         （spec:663；LLM 响应里也没有 credit 字段，spec:610,624）
    CheckinCapable    ✗  无每日签到面 ⇒ checkin_supported = False
    LoginCapable      ✗  无扫码/设备码登录流（凭证来自本机客户端 auth.json 快照）

注册（``providers/__init__.py`` 的 ``_LOADED``、``protocol.ChannelId`` 的 Literal、
``host_override.CHANNEL_HOST_FIELDS``、契约测试的能力矩阵）**不在本文件范围内**，
由主会话统一协调——门面本身不注册自己。
"""

from __future__ import annotations

from typing import Optional

from accounts import auth_manager
from providers.minimax_code import chat, store
from providers.minimax_code.constants import (
    ALIASES,
    CHANNEL_ID,
    DISPLAY_NAME,
    MODEL_CATALOG,
    STATIC_MODELS,
)
from providers.model_config import channel_aliases, channel_model_ids
from providers.protocol import QuotaSnapshot
from providers.trae_shared import pick_with_refresh_fallback


class MinimaxCodeProvider:
    """MiniMax Code（受管登录态 · Anthropic Messages 方言上游）。"""

    # 注：``protocol.ChannelId`` 的 Literal 已登记 "minimax_code"（见 ``protocol.KNOWN_CHANNEL_IDS``）。
    # 这里按实际值标注 str，运行期 isinstance(Provider) 只看属性/方法在不在。
    id: str = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = False  # 无签到 API（spec 全篇无该接口）

    # ---------------- 核心方法（Provider Protocol） ----------------

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
        """**没有官方倍率表**（spec 全篇无 pricing/rate 接口）⇒ ``rate=None``。

        与 qodercn 的处置同形：能诚实给出的只有 ``context_window``（目录里的
        ``max_input_tokens``，spec:499-516）。credit 统计走 log 侧的
        ``channel_credit_rate`` token 估算（上游 usage 无 credit 字段，spec:610,624），
        不依赖这里的倍率，所以 ``official=False``。
        """
        return [
            {
                "id": item,
                "display_name": chat.model_meta(item)["display_name"],
                "rate": None,
                "context_window": int(chat.model_meta(item)["max_input_tokens"]),
                "max_output_tokens": int(MODEL_CATALOG.get(item, {}).get("max_output_tokens")
                                         or chat.model_meta(item)["max_output_tokens"]),
                "official": False,
            }
            for item in channel_model_ids(CHANNEL_ID, STATIC_MODELS)
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
        # chat.refresh 返回账号 dict（token.refresh_account 的 bool 在 chat 层适配）。
        return await pick_with_refresh_fallback(self.id, chat.refresh, exclude_ids=exclude_ids)

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    def accepts_chat(self) -> bool:
        return True

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await chat.chat_completions(payload, api_key_info)

    # ---------------- StoreCapable / UpsertCapable ----------------

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_discovered(path)

    #: 管理页把粘贴内容解析后**平铺**进请求体（web/js/pages/channels.js:334
    #: ``api.post('/admin/accounts', {...d, name, provider})``），裸 JWT 这种非 JSON
    #: 文本会被前端包成 ``{"api_key": "<jwt>"}``（同文件 :329）。而 store 的入口吃的是
    #: "凭证原文字符串 / auth.json 文档 / 单条记录"。⇒ 门面做一次**形状适配**（不含逻辑）：
    #: 已是凭证文档就原样交给 store，否则从粘贴包装键里取出原文。
    _PASTE_WRAPPING_KEYS = ("api_key", "apiKey", "key", "credentials", "raw", "auth_json", "text")
    _CREDENTIAL_DOC_KEYS = ("records", "accessToken", "access_token", "refreshToken",
                            "refresh_token", "token")

    def parse_credentials(self, body: dict) -> dict:
        if isinstance(body, dict) and not any(key in body for key in self._CREDENTIAL_DOC_KEYS):
            for key in self._PASTE_WRAPPING_KEYS:
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return store.parse_credentials(value)
        return store.parse_credentials(body)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    # ---------------- RefreshCapable / TestChatCapable / QuotaCapable ----------------

    async def refresh(self, account: dict) -> dict:
        return await chat.refresh(account)

    async def test_chat(self, account: dict, model: str = "auto", prompt: str = "请回复：pong") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        """直接透传：chat 层已返回 QuotaSnapshot（unsupported，spec:663；``remaining=None``
        ⇒ 跨通道求和会把"不知道"当 0，看板必须分列，KD-10）。"""
        return await chat.fetch_quota(account)

    # ---------------- 测试钩子（openai_compat.py:86 同法） ----------------

    def set_transport(self, transport) -> None:
        """换上假传输（``httpx.MockTransport``）做**离线**验证；传 None 复原。

        只影响本通道的推理请求；OAuth 刷新走 storage 全局池（token.py），需要时
        monkeypatch ``providers.minimax_code.token.get_client``。
        """
        chat.set_transport(transport)


PROVIDER = MinimaxCodeProvider()
