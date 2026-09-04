"""缓存命中统计修复 + 思考档位默认值真实化（spec: docs/design/cache-stats-and-reasoning-display-spec.md）。"""
import asyncio

from storage import database as db
from upstream import proxy


# ---------------------------------------------------------------------------
# Part 1：extract_cache_tokens（max-of-candidates 语义）
# ---------------------------------------------------------------------------

def test_extract_three_styles_coexist_takes_max():
    # WorkBuddy 上游实测样本形态：Anthropic 占位 0，DeepSeek/OpenAI 携带真实命中
    usage = {
        "prompt_tokens": 71297,
        "prompt_tokens_details": {"cached_tokens": 70848},
        "prompt_cache_hit_tokens": 70848,
        "prompt_cache_miss_tokens": 449,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assert proxy._extract_cache_tokens(usage) == (70848, 0)


def test_extract_single_style_unchanged():
    # 仅 Anthropic 风格：含 creation
    assert proxy._extract_cache_tokens({
        "prompt_tokens": 1000,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 100,
    }) == (400, 100)
    # 仅 DeepSeek 风格
    assert proxy._extract_cache_tokens(
        {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 350}
    ) == (350, 0)
    # 仅 OpenAI 风格
    assert proxy._extract_cache_tokens(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 250}}
    ) == (250, 0)


def test_extract_clamp_and_degenerate():
    # 越界截断
    assert proxy._extract_cache_tokens(
        {"prompt_tokens": 100, "prompt_cache_hit_tokens": 999}
    ) == (100, 0)
    # None / 空 / 无 cache 键
    assert proxy._extract_cache_tokens(None) == (0, 0)
    assert proxy._extract_cache_tokens({}) == (0, 0)
    assert proxy._extract_cache_tokens({"prompt_tokens": 100}) == (0, 0)


# ---------------------------------------------------------------------------
# Part 2：思考档位兜底（_UPSTREAM_DEFAULT_REASONING）
# ---------------------------------------------------------------------------

def _make_log_data(model_name, reasoning_effort=None):
    captured = {}

    def fake_record(log_data):
        captured["data"] = log_data

    original = proxy.db.record_request
    proxy.db.record_request = fake_record
    try:
        async def call():
            proxy._log_request(
                {"id": 1, "name": "k", "_bind_channel": "workbuddy"},
                {"id": 2, "name": "acc", "provider": "workbuddy"},
                model_name, True,
                100, 10, 110, 0.5,
                "stop", 200, "", 0.0,
                reasoning_effort=reasoning_effort,
            )
            await asyncio.sleep(0.05)

        asyncio.run(call())
    finally:
        proxy.db.record_request = original
    return captured["data"]


def test_reasoning_fallback_known_model():
    assert _make_log_data("deepseek-v4-flash", None)["reasoning_effort"] == "none"
    assert _make_log_data("kimi-k2.7", None)["reasoning_effort"] == "minimal"


def test_reasoning_fallback_unknown_model():
    assert _make_log_data("gpt-x", None)["reasoning_effort"] == "upstream"


def test_reasoning_explicit_value_preserved():
    assert _make_log_data("deepseek-v4-flash", "high")["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# Part 3：stats cache_hit_ratio
# ---------------------------------------------------------------------------

def _add_log(provider, model, *, prompt, cache_read):
    db.record_request({
        "api_key_id": None, "api_key_name": None,
        "account_id": None, "account_name": None,
        "provider": provider, "model": model, "stream": 0,
        "prompt_tokens": prompt, "completion_tokens": 10,
        "total_tokens": prompt + 10,
        "cache_read_tokens": cache_read, "cache_creation_tokens": 0,
        "credit": 0.1, "finish_reason": "stop", "duration_ms": 1000,
        "status_code": 200, "error_msg": "",
    })


def test_stats_cache_hit_ratio(isolated_db):
    _add_log("workbuddy", "glm-5.3", prompt=1000, cache_read=600)
    result = db.get_provider_model_usage({})
    assert result["totals"]["cache_hit_ratio"] == 0.6
    daily = result["providers"]["workbuddy"]["models"]["glm-5.3"]["daily"][0]
    assert daily["cache_hit_ratio"] == 0.6


def test_stats_cache_hit_ratio_zero_prompt(isolated_db):
    _add_log("traework", "m1", prompt=0, cache_read=0)
    result = db.get_provider_model_usage({})
    assert result["totals"]["cache_hit_ratio"] is None
