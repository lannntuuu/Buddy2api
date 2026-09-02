"""WorkBuddy cache_read 统计补全测试（对应 docs/workbuddy-cache-tracking-plan.md）。

覆盖：
  - _extract_cache_tokens：三种字段风格 / 混合优先级 / 负值 clamp / 越界截断 / 空 None
  - _log_request 带 usage：log_data 含 cache 字段 + usage_json + credit_source 判定
  - Responses 流：input_tokens_details.cached_tokens 透传真值
"""

import asyncio
import json

import pytest

from upstream import proxy
from upstream import responses as responses_mod


# ---------------------------------------------------------------------------
# Part 1：_extract_cache_tokens
# ---------------------------------------------------------------------------

def test_extract_cache_anthropic_style():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 100,
    }
    assert proxy._extract_cache_tokens(usage) == (400, 100)


def test_extract_cache_deepseek_style():
    usage = {
        "prompt_tokens": 1000,
        "prompt_cache_hit_tokens": 350,
        "prompt_cache_miss_tokens": 650,
    }
    # DeepSeek 风格只映射 cache_read，cache_creation 记 0
    assert proxy._extract_cache_tokens(usage) == (350, 0)


def test_extract_cache_openai_style():
    usage = {
        "prompt_tokens": 1000,
        "prompt_tokens_details": {"cached_tokens": 250},
    }
    assert proxy._extract_cache_tokens(usage) == (250, 0)


def test_extract_cache_mixed_prefers_anthropic():
    # 同时带三种风格时，Anthropic 优先
    usage = {
        "prompt_tokens": 1000,
        "cache_read_input_tokens": 400,
        "prompt_cache_hit_tokens": 999,
        "prompt_tokens_details": {"cached_tokens": 888},
    }
    assert proxy._extract_cache_tokens(usage) == (400, 0)


def test_extract_cache_negative_clamped():
    usage = {
        "prompt_tokens": 1000,
        "cache_read_input_tokens": -50,
        "cache_creation_input_tokens": -10,
    }
    assert proxy._extract_cache_tokens(usage) == (0, 0)


def test_extract_cache_read_capped_to_prompt():
    # cache_read 超过 prompt_tokens 时截断（防脏数据）
    usage = {
        "prompt_tokens": 100,
        "cache_read_input_tokens": 999,
    }
    assert proxy._extract_cache_tokens(usage) == (100, 0)


def test_extract_cache_empty_and_none():
    assert proxy._extract_cache_tokens({}) == (0, 0)
    assert proxy._extract_cache_tokens(None) == (0, 0)


# ---------------------------------------------------------------------------
# Part 1：_log_request 落库字段
# ---------------------------------------------------------------------------

def _make_log_data(usage):
    captured = {}

    def fake_record(log_data):
        captured["data"] = log_data

    # 直接调用 _log_request：它内部通过 run_in_executor 调 db.record_request，
    # 需要运行中的事件循环；因此在本协程内 await，让默认线程池执行写入。
    original = proxy.db.record_request
    proxy.db.record_request = fake_record
    try:
        asyncio.run(_call_log_request(
            {"id": 1, "name": "k", "_bind_channel": "workbuddy"},
            {"id": 2, "name": "acc", "provider": "workbuddy"},
            "gpt-x", usage,
        ))
    finally:
        proxy.db.record_request = original
    return captured["data"]


async def _call_log_request(api_key_info, account, model_name, usage):
    proxy._log_request(
        api_key_info, account, model_name,
        True,
        100, 10, 110, 0.5,
        "stop", 200, "", 0.0,
        usage=usage,
    )
    # 等待 fire-and-forget executor 完成
    await asyncio.sleep(0.05)


def test_log_request_with_usage_records_cache_fields():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 5,
    }
    data = _make_log_data(usage)
    assert data["cache_read_tokens"] == 40
    assert data["cache_creation_tokens"] == 5
    assert data["usage_json"] == json.dumps(usage, ensure_ascii=False)
    # 含已知 cache 键 → live
    assert data["credit_source"] == "live"


def test_log_request_without_usage_falls_back():
    data = _make_log_data(None)
    assert data["cache_read_tokens"] == 0
    assert data["cache_creation_tokens"] == 0
    assert data["usage_json"] is None
    assert data["credit_source"] is None


def test_log_request_usage_without_cache_key_not_live():
    # usage 有值但无 cache 键：usage_json 照存证据，credit_source 为 None
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    data = _make_log_data(usage)
    assert data["usage_json"] == json.dumps(usage, ensure_ascii=False)
    assert data["credit_source"] is None
    assert data["cache_read_tokens"] == 0


def test_log_request_usage_json_truncated():
    # 构造 >64KB 的 usage，验证截断保护生效
    big = {"prompt_tokens": 1, "cache_read_input_tokens": 1, "junk": "x" * 70000}
    data = _make_log_data(big)
    assert data["cache_read_tokens"] == 1
    parsed = json.loads(data["usage_json"])
    assert parsed.get("truncated") is True


# ---------------------------------------------------------------------------
# Part 2：Responses API 真值透传
# ---------------------------------------------------------------------------

async def _collect_responses_stream(chunks):
    async def fake_stream():
        for c in chunks:
            yield c

    events = []
    async for ev in responses_mod.chat_stream_to_responses_stream(fake_stream(), "gpt-x"):
        events.append(ev)
    return events


def test_responses_stream_passes_cached_tokens_truth():
    # 末尾 chunk 携带 usage.prompt_tokens_details.cached_tokens
    chunks = [
        'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n',
        'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":10,'
        '"total_tokens":110,"prompt_tokens_details":{"cached_tokens":40}}}\n\n',
        "data: [DONE]\n\n",
    ]
    events = asyncio.run(_collect_responses_stream(chunks))
    # 找 response.completed 事件（usage 非 None）。事件格式为
    # "event: ...\ndata: {...}\n\n"，从 data: 行解析 JSON。
    completed = None
    for ev in events:
        data_line = ""
        for line in ev.splitlines():
            if line.startswith("data: "):
                data_line = line[len("data: "):]
                break
        if not data_line:
            continue
        payload = json.loads(data_line)
        resp = payload.get("response")
        if resp and resp.get("status") == "completed" and resp.get("usage"):
            completed = resp
            break
    assert completed is not None
    assert completed["usage"]["input_tokens_details"]["cached_tokens"] == 40
    assert completed["usage"]["output_tokens_details"]["reasoning_tokens"] == 0
