"""阿里百炼 Bailian (https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1) — OpenAI-compatible provider constants.

Endpoints / auth scheme confirmed against the MaaS dedicated instance (Bearer-only).
"""

from __future__ import annotations

CHANNEL_ID = "bailian"
DISPLAY_NAME = "阿里百炼 Bailian"

DEFAULT_BASE_URL = "https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

EP_MODELS = "/models"
EP_CHAT = "/chat/completions"

# Static fallback model list. We don't know what models a given dedicated
# instance has deployed, so this is only a placeholder (admin can edit the
# whitelist in "渠道与模型"; dynamic /models will override it).
STATIC_MODELS: tuple[str, ...] = (
    "qwen-plus",
)

DEFAULT_MODEL = STATIC_MODELS[0]

# Translation table: caller sends a friendly alias → resolved to a real upstream id.
ALIASES: dict[str, str] = {
    "auto": DEFAULT_MODEL,
    "bailian": DEFAULT_MODEL,
    "bailian/auto": DEFAULT_MODEL,
}

# Models cache TTL (seconds). /v1/models is cheap but we don't want to hit it
# every request.
MODELS_CACHE_TTL = 600.0

# Per-request client cap (one logical account = one API key for OpenAI-compat
# platforms, so we never pool / rotate).
SINGLE_ACCOUNT = True

ENV_API_KEY = "CB_BAILIAN_API_KEY"
ENV_AUTH_DIR = "CB_BAILIAN_AUTH_DIR"
