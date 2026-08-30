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
SESSION_MODE = "work"

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
