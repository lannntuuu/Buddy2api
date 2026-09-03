"""upstream.aliases — model name translation, default model list, reasoning defaults.

This module owns the static model list and the alias table (built-in plus
user-overridden via the settings row `model_aliases`). Pulled out of
proxy.py so the canonical definitions live in one place and the proxy
module can stay focused on the request pipeline.

Public surface (re-exported by upstream.proxy for backwards compat):
- DEFAULT_MODELS
- _BUILTIN_ALIASES
- effective_builtin_aliases()
- resolve_model_alias(model)
- _configured_reasoning_default(model)
- build_backend_body(payload)
"""
from __future__ import annotations

import os

from storage import database as db


DEFAULT_MODELS = [
    {"id": "glm-5.2", "name": "GLM-5.2"},
    {"id": "glm-5.1", "name": "GLM-5.1"},
    {"id": "glm-5v-turbo", "name": "GLM-5V Turbo"},
    {"id": "kimi-k2.7", "name": "Kimi K2.7"},
    {"id": "kimi-k2.6", "name": "Kimi K2.6"},
    {"id": "kimi-k2.5", "name": "Kimi K2.5"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
    {"id": "minimax-m3-pay", "name": "MiniMax M3"},
    {"id": "hy3-preview-agent", "name": "HY3 Preview Agent"},
    {"id": "auto", "name": "Auto (auto routing)"},
]

# Built-in model aliases: alias_id -> backend_model_id
# Extended by user-defined aliases from database settings "model_aliases".
_BUILTIN_ALIASES = {
    # GPT-5.x series
    "gpt-5.5": "glm-5.2",
    "gpt-5.5-mini": "glm-5.1",
    "gpt-5.4": "glm-5.2",
    "gpt-5.4-mini": "glm-5.1",
    "gpt-5.4-codex": "glm-5.2",
    "gpt-5.1": "glm-5.2",
    "gpt-5.1-codex": "glm-5.2",
    "gpt-5": "glm-5.2",
    "gpt-5-mini": "glm-5.1",
    # GPT-4.x series
    "gpt-4o": "glm-5.2",
    "gpt-4o-mini": "glm-5.1",
    "gpt-4-turbo": "glm-5.2",
    "gpt-4": "glm-5.2",
    "gpt-4.1": "glm-5.2",
    "gpt-4.1-mini": "glm-5.1",
    "gpt-3.5-turbo": "glm-5.1",
    # o-series reasoning models
    "o3": "deepseek-v4-pro",
    "o3-mini": "deepseek-v4-flash",
    "o4-mini": "deepseek-v4-pro",
    "o1": "deepseek-v4-pro",
    "o1-mini": "deepseek-v4-flash",
    # Claude series
    "claude-3.5-sonnet": "deepseek-v4-pro",
    "claude-3-haiku": "deepseek-v4-flash",
    "claude-sonnet-4": "deepseek-v4-pro",
    "claude-opus-4": "deepseek-v4-pro",
    # DeepSeek
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-coder": "deepseek-v4-pro",
    "deepseek-r1": "deepseek-v4-pro",
    # Moonshot
    "moonshot-v1-128k": "kimi-k2.7",
    "moonshot-v1-32k": "kimi-k2.6",
}


def effective_builtin_aliases() -> dict:
    """Active alias table for WorkBuddy (full-replace semantics, like other channels).

    `model_aliases` setting unset -> built-in default aliases;
    `model_aliases` set (even to {}) -> user values only, all built-ins disabled.
    """
    raw = db.get_setting("model_aliases", None)
    if raw is None:
        return dict(_BUILTIN_ALIASES)
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def resolve_model_alias(model: str) -> str:
    """Resolve an alias to its real backend model ID. Returns the original if no match."""
    return effective_builtin_aliases().get(model, model)


def _configured_reasoning_default(model: str) -> str | None:
    """Resolve the effective reasoning tier for a model (replaces the old env var).

    Priority: client explicit param > per-model config > channel default > not injected.
    Only the WorkBuddy channel is confirmed upstream to accept reasoning_effort.
    """
    from providers.model_config import reasoning_for_model

    return reasoning_for_model("workbuddy", model)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
