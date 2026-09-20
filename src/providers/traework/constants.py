"""TraeWork CN (TRAE SOLO CN) protocol constants.

Hosts, client id, and app version were read from the official
0.1.56 product.json and live requests on this machine.
"""

from __future__ import annotations

from providers.trae_shared import (
    AGENT_HOST as AGENT_API,
    APP_ID,
    CHECKIN_CLAIM_PATH,
    CHECKIN_STATUS_PATH,
    CLIENT_ID,
    ENT_USAGE_PATH as USAGE_PATH,
    UG_HOST as UG_API,
)

CHANNEL_ID = "traework"
DISPLAY_NAME = "TraeWork"

IDE_VERSION = "0.1.56"
PLATFORM_CODE = "SOLO_PC"
PRODUCT_CODE = "SOLO_Lite"
REQ_SOURCE = 2
EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"
GET_USER_PATH = "/cloudide/api/v3/trae/GetUserInfo"
MODELS_PATH = "/api/remote/v1/models"
SESSIONS_PATH = "/api/remote/v1/chat_sessions"

AUTH_STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"
AUTH_DEVICE_PREFIX = "iCubeAuthInfo://icube-dc:"
STORAGE_FILENAME = "storage.json"

AGENT_ID = "solo_work_lite"
# code 模式联动的 agent（官方 client 映射 mode=Code -> SoloAgentLite / solo_agent_lite）
AGENT_ID_CODE = "solo_agent_lite"
SESSION_MODE = "work"
SESSION_MODE_CODE = "code"

# code 模式标记（官方 client 在 code 模式下注入；见 976.f593cb93.mjs
# applyCodeModeFlagIfNeeded）：对 chat.sendMessage 是 body 顶层字段。
# 对 chat.createSession 官方是塞进 initial_message —— 但那是**完整的发消息对象**，
# 本网关首轮走独立的 sendMessage、不发送 initial_message，故这里只用于 sendMessage。
# work 模式不发送该字段。
CODE_MODE_FLAG = "is_in_code_mode"


def agent_id_for_mode(mode: str) -> str:
    """随 mode 联动的 agent：code -> solo_agent_lite，其余（含 work/未配置）-> solo_work_lite。"""
    return AGENT_ID_CODE if mode == SESSION_MODE_CODE else AGENT_ID

STATIC_MODELS = (
    "qwen-3.7-plus",
    "Doubao-Seed-2.1-Turbo",
    "DeepSeek-V4-Flash-Official",
    "qwen-3.5",
    "glm-5",
    "glm-5.1",
    "kimi-k2.5",
    "Doubao-Seed-2.0-Code",
)

ALIASES = {
    "auto": "qwen-3.7-plus",
}

USER_AGENT = "TRAE-SOLO-CN/0.1.56"
