"""GMI Cloud (https://api.gmi-serving.com/v1) — OpenAI-compatible provider constants.

Endpoints / auth scheme confirmed against the public host (Bearer-only).
"""

from __future__ import annotations

CHANNEL_ID = "gmi"
DISPLAY_NAME = "GMI Cloud"

DEFAULT_BASE_URL = "https://api.gmi-serving.com/v1"

EP_MODELS = "/models"
EP_CHAT = "/chat/completions"

# Static fallback model list (dynamically refreshed from /v1/models on startup;
# these are the IDs we know to exist on the platform as of 2025).
STATIC_MODELS: tuple[str, ...] = (
    "zai-org/GLM-5.3-Flash",
)

DEFAULT_MODEL = STATIC_MODELS[0]

# Translation table: caller sends a friendly alias → resolved to a real upstream id.
ALIASES: dict[str, str] = {
    "auto": DEFAULT_MODEL,
    "gmi": DEFAULT_MODEL,
    "glm-5.2": "zai-org/GLM-5.3-Flash",
    "gmi-flash": "zai-org/GLM-5.3-Flash",
    "gmi/auto": DEFAULT_MODEL,
}

# Models cache TTL (seconds). /v1/models is cheap but we don't want to hit it
# every request.
MODELS_CACHE_TTL = 600.0

# Per-request client cap (one logical account = one API key for OpenAI-compat
# platforms, so we never pool / rotate).
SINGLE_ACCOUNT = True

ENV_API_KEY = "CB_GMI_API_KEY"
ENV_AUTH_DIR = "CB_GMI_AUTH_DIR"