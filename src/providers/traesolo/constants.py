"""Trae SOLO (solo_work_lite) protocol constants.

Ported from the Go project connectedGraph/trae2api-web (MIT).
Hosts, client id, and ide version come from the upstream reverse
engineering report (measured values, do not change casually).

SOLO 与 TraeWork 是两条独立产品线：
  - SOLO:   POST /api/agent/v3/llm_utils_chat（无状态单请求 SSE，OpenAI 风格 payload）
  - Work:   /api/remote/v1/chat_sessions（有状态会话轮询）
凭证体系相同（Cloud-IDE-JWT + refreshToken），但本通道只走登录闭环 /
JSON 导入，不读 TRAE SOLO CN 的 IDE 本地存储目录。
"""

from __future__ import annotations

from providers.trae_shared import AGENT_HOST, APP_ID, CLIENT_ID, UG_HOST

CHANNEL_ID = "traesolo"
DISPLAY_NAME = "Trae SOLO"

# --- Hosts ---
OAUTH_HOST = "https://api.trae.com.cn"           # ExchangeToken / GetUserInfo
CONSOLE_HOST = "https://www.trae.cn"             # 登录页
IDE_VERSION = "0.1.52"
IDE_VERSION_CODE = "20260811"
DEVICE_BRAND = "83DG"
OS_VERSION = "Windows 11 Pro"
FUNCTION = "solo_work_lite"
PLUGIN_VERSION = "2.3.62834"
USER_AGENT = f"Trae/{IDE_VERSION}"
DOMAIN = "trae.cn"

# --- Endpoints ---
EP_CHAT = "/api/agent/v3/llm_utils_chat"
EP_MODELS = "/api/ide/v1/get_detail_param"
EP_EXCHANGE = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EP_USER_INFO = "/cloudide/api/v3/trae/GetUserInfo"
from providers.trae_shared import (
    CHECKIN_CLAIM_PATH as EP_CHECKIN_CLAIM,
    CHECKIN_STATUS_PATH as EP_CHECKIN_STATUS,
    ENT_USAGE_PATH as EP_ENT_USAGE,
)

# --- Models ---
# 内置静态模型表（32 个 config_name，来自逆向报告；动态拉取失败时回退）。
STATIC_MODELS = (
    "Doubao-Seed-2.1-Pro",
    "seed-code-pro-0430",
    "Doubao-Seed-2.1-Turbo",
    "Doubao-Seed-2.0-Code",
    "DeepSeek-V4-Flash-Official",
    "browser_use_subagent",
    "glm-5.2",
    "glm-5-turbo",
    "glm-5",
    "DeepSeek-V4-Pro",
    "DeepSeek-V4-Flash",
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "minimax-m3",
    "qwen-3.7-plus",
    "sagitta",
    "aquila",
    "custom_model_gemini",
    "custom_model_placeholder",
    "custom_model_1M_text",
    "custom_model_1M",
    "custom_model_kimi",
    "custom_model_claude",
    "custom_model_gpt-5",
    "custom_model_no-fc",
    "custom_model_deepseek_chat",
    "custom_model_deepseek_reasoner",
    "custom_model_deepseek_v4",
    "explore_sub_agent_v13",
    "explore_sub_agent_v2",
    "summary",
)

DEFAULT_CONFIG = "glm-5.2"  # 默认模型（实测可用）

ALIASES = {
    "auto": DEFAULT_CONFIG,
}

# 官方 consumption_rate.rate（credit 消耗倍率，原值直用；相对基准模型的系数）。
# 来自 get_detail_param 实时解析；此处为无可用账号/缓存未命中时的静态兜底。
# None 表示官方未提供（自定义/占位项）。
MODEL_RATES: dict[str, float | None] = {
    "kimi-k3": 1.65,
    "qwen3.8-max": 1.5,
    "Doubao-Seed-Evolving": 0.77,
    "Doubao-Seed-2.1-Pro": 0.77,
    "seed-code-pro-0430": 0.77,
    "glm-5-turbo": 0.74,
    "DeepSeek-V4-Pro-Official": 0.72,
    "DeepSeek-V4-Pro": 0.72,
    "glm-5": 0.70,
    "kimi-k2.6": 0.69,
    "kimi-k2.7-code": 0.62,
    "glm-5.3": 0.40,
    "glm-5.2": 0.40,
    "sagitta": 0.40,
    "aquila": 0.40,
    "Doubao-Seed-2.1-Turbo": 0.39,
    "Doubao-Seed-2.0-Code": 0.39,
    "minimax-m3": 0.26,
    "qwen-3.7-plus": 0.25,
    "summary": 0.15,
    "DeepSeek-V4-Flash-Official": 0.08,
    "DeepSeek-V4-Flash": 0.08,
    "file_search_agent": 0.03,
    "explore_sub_agent_v2": 0.03,
    "browser_use_subagent": 0.01,
    "custom_model_gemini": None,
    "custom_model_placeholder": None,
    "custom_model_1M_text": None,
    "custom_model_1M": None,
    "custom_model_kimi": None,
    "custom_model_claude": None,
    "custom_model_gpt-5": None,
    "custom_model_no-fc": None,
    "custom_model_deepseek_chat": None,
    "custom_model_deepseek_reasoner": None,
    "custom_model_deepseek_v4": None,
    "claude-opus-4-6": None,
    "GLM-5.2": None,
}

# --- Scheduling（冷却状态机，移植自 Go 版 pool）---
PLAN_COOLDOWN_S = 12 * 3600   # 1005 权益不足 → 硬冷却 12h
SOFT_COOLDOWN_S = 60          # 429 / 404 → 短冷却 60s
ERR_THRESHOLD = 3             # 连续错误阈值
ERR_COOLDOWN_S = 10 * 60      # 连续错误 → 冷却 10min
REFRESH_SKEW_S = 24 * 3600    # token 过期前 24h 预刷新
MAX_ROTATE = 3                # 单请求最多换号次数

# --- Dynamic models ---
DYNAMIC_MODELS_TTL = 3600     # 成功缓存 1h
MODELS_FAIL_COOLDOWN = 300    # 失败负缓存 5min

# --- Login loop ---
PENDING_TTL_S = 600           # pending 登录 10 分钟
AUTHORIZE_PATH = "/authorize"
ENV_CALLBACK_BASE = "CB_TRAESOLO_CALLBACK_BASE"
ENV_AUTH_DIR = "CB_TRAESOLO_AUTH_DIR"
