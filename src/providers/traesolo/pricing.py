"""TRAE official per-token credit formula (derived 2026-09-01 from 51 usage_type=7 sessions).

credits = p_in*input + p_cache*cache_read + p_out*output   (tokens; input EXCLUDES cache_read;
official_money = credits * 0.025 verified on all rows)

Derived prices (official money units per 1M tokens):
  qwen3.7-plus            in=2.00   cache=0.40   out=9.20   (46/51 rows exact within 1%)
  deepseek-v4-flash官方版  in=1.35   cache=0.047  out=3.84
  glm-5.3                 in=2.80   cache=0.70   out=9.80

货币口径（2026-09-01 核对）：官方 API 的 money 单位与官方定价页人民币单价在 2% 内吻合
（Lite ¥49/2000=¥0.0245、Pro ¥99/4000=¥0.0248 vs 内部 0.025），即 1 credit ≈ ¥0.025
（40 credits ≈ ¥1）。真实美元价 ≈ 下列数字 ÷ 7.2（如 qwen input ≈ $0.28/M，与
GMI $0.15/M 同量级）。credit 计算本身与货币无关（分母恒为 0.025）。

A few large sessions bill at deep discounts (promos) and are excluded from derivation;
those rows are genuinely off-formula, so the estimate is an upper reference.
"""
from __future__ import annotations

# official money units (≈ CNY) per 1M tokens, per model (normalized lowercase keys)
TRAE_TOKEN_PRICES: dict[str, tuple[float, float, float]] = {
    # (input, cache_read, output) ≈¥/1M
    "qwen3.7-plus": (2.00, 0.40, 9.20),
    "deepseek-v4-flash": (1.35, 0.047, 3.84),   # matches 官方版 / Official naming
    "glm-5.3": (2.80, 0.70, 9.80),
}
MONEY_PER_CREDIT = 0.025  # 1 credit = 0.025 official money units (≈¥0.025); 40 credits ≈ ¥1
DEFAULT_PRICE = (2.00, 0.40, 9.20)  # fallback: qwen-like tier for unknown models


def _norm(name: str | None) -> str:
    n = (name or "").strip().lower().replace(" ", "")
    n = n.replace("官方版", "").replace("official", "")
    return n.strip("-")  # "DeepSeek-V4-Flash-Official" -> "deepseek-v4-flash-" -> strip 尾连字符


def trae_credit_from_usage(
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    reasoning_tokens: int = 0,
    model: str | None = None,
) -> float | None:
    """Estimate official-style credit from one request's token usage.

    prompt_tokens here is the gateway-side prompt (input INCLUDING cache), so
    input_nc = prompt - cache_read; cache_creation bills at input price.
    Returns None when token counts are unusable (0/absent).
    """
    try:
        prompt = int(prompt_tokens or 0)
        completion = int(completion_tokens or 0)
    except (TypeError, ValueError):
        return None
    if prompt <= 0 and completion <= 0:
        return None
    cache_r = max(0, int(cache_read_tokens or 0))
    cache_r = min(cache_r, prompt)  # cache_read is a subset of prompt
    cache_c = max(0, int(cache_creation_tokens or 0))
    out = max(0, completion)
    p_in, p_cache, p_out = TRAE_TOKEN_PRICES.get(_norm(model), DEFAULT_PRICE)
    money = (
        (prompt - cache_r + cache_c) * p_in
        + cache_r * p_cache
        + out * p_out
    ) / 1e6
    return round(money / MONEY_PER_CREDIT, 6)
