"""Tests for per-channel upstream host override.

After the data-driven migration (custom OpenAI-compat channels) the gmi
and bailian entries moved out of `channel_hosts` and into the seed
definition (`custom_channels` settings key). The whitelist below no longer
lists them — change base URL by editing the channel definition instead.
"""
import pytest
from providers.host_override import channel_host, CHANNEL_HOST_FIELDS
from storage import database as db

def test_default_when_unset(monkeypatch):
    monkeypatch.setattr(db, "get_setting", lambda k, d=None: d)
    # Built-in multi-host channels keep their place in the whitelist.
    assert channel_host("qwenwork", "gateway", "https://default") == "https://default"


def test_override_when_set(monkeypatch):
    monkeypatch.setattr(
        db,
        "get_setting",
        lambda k, d=None: {"qwenwork": {"gateway": "https://mirror.example.com/qw"}},
    )
    assert channel_host("qwenwork", "gateway", "https://default") == "https://mirror.example.com/qw"


def test_phase_b_override(monkeypatch):
    monkeypatch.setattr(
        db,
        "get_setting",
        lambda k, d=None: {"qclaw": {"aizone_base": "https://mirror.example.com/aizone/v1"}},
    )
    assert channel_host("qclaw", "aizone_base", "https://default") == "https://mirror.example.com/aizone/v1"
    assert channel_host("qclaw", "jprx_gateway", "https://default") == "https://default"
    assert channel_host("traesolo", "agent_host", "https://default") == "https://default"
    assert channel_host("traework", "ug_host", "https://default") == "https://default"


def test_channel_host_fields():
    """gmi / bailian were removed: their base URL now lives in the seed
    definition (or admin-edited custom_channels entry), not in the
    `channel_hosts` settings blob.

    minimax_code 是双 host 通道：``llm``（agent.minimax.cn，chat.py 的推理面）与
    ``oauth``（account.minimax.cn，token.py 的换票面，spec:241,88）——字段名必须
    与 ``channel_host(CHANNEL_ID, ...)`` 的调用**逐字一致**，差一个字符覆盖就失效
    （整字典相等断言防的就是这个）。
    """
    assert CHANNEL_HOST_FIELDS == {
        "qwenwork": ("gateway",),
        "qodercn": ("gateway", "openapi"),
        "qclaw": ("jprx_gateway", "aizone_base"),
        "traesolo": ("oauth_host", "console_host", "agent_host"),
        "traework": ("agent_host", "ug_host"),
        "minimax_code": ("llm", "oauth"),
    }


def test_unknown_channel_returns_default(monkeypatch):
    """gmi / bailian no longer have entries in CHANNEL_HOST_FIELDS. Calling
    `channel_host('gmi', ...)` returns the supplied default — base URL for
    these channels lives in the seed definition now."""
    monkeypatch.setattr(db, "get_setting", lambda k, d=None: d)
    assert channel_host("gmi", "base_url", "https://seed-default/v1") == "https://seed-default/v1"
    assert channel_host("bailian", "base_url", "https://seed-default/v1") == "https://seed-default/v1"