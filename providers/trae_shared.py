"""Trae 家族（TraeWork / Trae SOLO）共享的协议常量。

SOLO 与 TraeWork 是两条产品线，但共用同一套 TRAE 身份体系
（Cloud-IDE-JWT + refreshToken、同一 client/app id、同一批积分端点）。
这些值曾各自复制一份并已出现漂移风险，统一收敛到这里。

均为逆向测得的固定值，不要随手修改。
"""

from __future__ import annotations

# --- Client fingerprint ---
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"

# --- Hosts ---
UG_HOST = "https://api.trae.cn"                  # 签到 / 积分
AGENT_HOST = "https://trae-api-cn.mchost.guru"   # agent 网关

# --- Endpoints（积分/签到/用量）---
CHECKIN_STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CHECKIN_CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
ENT_USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"
