"""Qoder CN (Qoder® 中国版) protocol constants.

Frozen from a real captured chat request (MITM of the official desktop client
`com.qodercn.app.stable`, 2026-09) plus the decrypted client model catalog
(`%USERPROFILE%\\.qoder-cn\\.models\\<uid>\\catalog-v6`, AES-256-GCM, key = uid).

Three verification anchors:

* **cosy signing** — reproduced a captured `Authorization` byte-for-byte:
  ``md5(f"{encoded}\\n{cosy_key}\\n{cosy_date}\\n{body}\\n{path}")`` where
  ``path`` drops the ``/algo`` prefix and the query.
* **plaintext body** — a smoke request with a plain JSON body (no ``Encode=1``)
  returned HTTP 200 with real streamed content, so the 65-char body cipher is
  not required for this channel.
* **model catalog** — authoritative model keys/display names/limits decrypted
  from the client cache; every key in ``STATIC_MODELS`` was then confirmed by a
  live request (see ``MODEL_CATALOG``).

Domain                 : qoder.com.cn
Chat gateway           : gateway.qoder.com.cn  (QwenWork uses gateway.qwenwork.cn)
Account login cache    : %APPDATA%\\QoderCN (IDE) /
                         %APPDATA%\\com.qodercn.app.stable (desktop)
Machine fingerprint    : %USERPROFILE%\\.qoder-cn\\.auth\\machine_id
"""

from __future__ import annotations

CHANNEL_ID = "qodercn"
DISPLAY_NAME = "Qoder"

# OAuth / openapi host.
OPENAPI_HOST = "https://openapi.qoder.com.cn"
# Chat gateway host.
GATEWAY_HOST = "https://gateway.qoder.com.cn"

# Chat endpoint family. The plaintext body path works for every model except
# `qfmodel`, which additionally requires the top-level `business` block below.
# `Encode=1` (the client's custom-base64 body) is NOT needed — see BUSINESS.
CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common"

# Quota endpoint (confirmed present in the official desktop bundle).
QUOTA_PATH = "/api/v2/quota/usage"

# Cosy identifiers, all taken from the captured chat request headers.
CLIENT_TYPE = "10"
SCENE = "app"
BUSINESS_PRODUCT = "app"
BUSINESS_TYPE = "agent"
MACHINE_TYPE = "5"
MACHINE_OS = "x86_64_win32"
COSY_VERSION = "1.1.53"
LOGIN_VERSION = "v2"
# `session_type` in the chat body; the desktop worker sends "qoderclicn".
SESSION_TYPE = "qoderclicn"
# The desktop worker issues chat over undici, so this is its real wire UA.
USER_AGENT = "undici"
# Client version reported in account metadata (desktop app version).
IDE_VERSION = "1.25.1"

# --- `business` block (required for qfmodel) ---------------------------------
# The desktop client always sends a top-level `business` object alongside
# `parameters`. Its **presence** is what makes `qfmodel` (Qwen3.8-Flash) routable:
# without it the gateway answers 400
# `[FAIL]node:oa_qwen-plus-main msg:Execution failed: null` — for that model only.
# Verified live: adding `business` turns qfmodel green (3/3 stable), and removing
# any *single* key from it still works, so only the object's presence matters.
# Every other model answers identically with or without it, so sending it
# unconditionally is safe.
# Values mirror the captured request:
#   "business":{"product":"app","version":"1.1.53","type":"agent","id":…,
#               "name":"你正在为 Qoder","begin_at":…,"stage":"start",
#               "sub_task":"chat_recap_generation"}
BUSINESS: dict[str, object] = {
    "product": BUSINESS_PRODUCT,
    "version": COSY_VERSION,
    "type": BUSINESS_TYPE,
    "stage": "start",
    "sub_task": "chat_recap_generation",
}

# `parameters` extras the client pairs with the business block. `context_length`
# is the model's own window (qfmodel advertises 200K/400K/1M tiers; the client
# sent 1M). Kept faithful to the capture.
PARAMETERS_EXTRA: dict[str, object] = {
    "enable_thinking": True,
    "context_length": 1_000_000,
}

# The cosy RSA public key is identical to the qoder_work family; frozen after
# extraction from the official Qoder CN desktop asar.
RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

# Frozen: the plaintext chat path was smoke-tested end to end (real streamed
# content from gateway.qoder.com.cn).
COSY_VERSION_FROZEN = True

# --- Model catalog (decrypted from the client cache, each key live-verified) ---
# is_vl / is_reasoning / max_input_tokens drive the upstream `model_config`
# block; display_name is what the admin UI and API list show.
MODEL_CATALOG: dict[str, dict] = {
    "auto": {"display_name": "Auto", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "qmodel_38max": {"display_name": "Qwen3.8-Max", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "qfmodel": {"display_name": "Qwen3.8-Flash", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "qmodel_latest": {"display_name": "Qwen3.7-Max", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "qmodel": {"display_name": "Qwen3.7-Plus", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "q37fmodel": {"display_name": "Qwen3.7-Flash", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "dmodel": {"display_name": "DeepSeek-V4-Pro", "is_vl": True, "is_reasoning": True, "max_input_tokens": 96_000},
    "dfmodel": {"display_name": "DeepSeek-Flash", "is_vl": True, "is_reasoning": False, "max_input_tokens": 180_000},
    "gmodel": {"display_name": "GLM-5.3", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "gfmodel": {"display_name": "GLM-5.3-Flash", "is_vl": True, "is_reasoning": True, "max_input_tokens": 1_000_000},
    "gm51model": {"display_name": "GLM-5.2", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "kmodel_latest": {"display_name": "Kimi-K3", "is_vl": True, "is_reasoning": False, "max_input_tokens": 180_000},
    "kmodel": {"display_name": "Kimi-K2.8-Preview", "is_vl": True, "is_reasoning": True, "max_input_tokens": 180_000},
    "mmodel": {"display_name": "MiniMax-M2.7", "is_vl": False, "is_reasoning": False, "max_input_tokens": 180_000},
}

# Keys the gateway answers with real content, all smoke-tested live.
# `qmodel_preview` is absent from the official catalog and answered 400, so it is
# not listed at all. `qfmodel` **is** listed: it needed the `business` block
# (see BUSINESS) — with it the gateway serves it reliably (verified 3/3).
STATIC_MODELS = (
    "auto",
    "qmodel_38max",
    "qfmodel",
    "qmodel_latest",
    "qmodel",
    "q37fmodel",
    "dmodel",
    "dfmodel",
    "gmodel",
    "gfmodel",
    "gm51model",
    "kmodel_latest",
    "kmodel",
    "mmodel",
)

DEFAULT_MODEL = "auto"

# "auto" is itself a routable upstream key, so it must not be rewritten.
ALIASES: dict[str, str] = {}

from providers.retry import RETRYABLE_STATUS  # noqa: E402  (统一重试常量)
