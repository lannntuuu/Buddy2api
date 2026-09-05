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
    `channel_hosts` settings blob."""
    assert CHANNEL_HOST_FIELDS == {
        "qwenwork": ("gateway",),
        "qclaw": ("jprx_gateway", "aizone_base"),
        "traesolo": ("oauth_host", "console_host", "agent_host"),
        "traework": ("agent_host", "ug_host"),
    }


def test_unknown_channel_returns_default(monkeypatch):
    """gmi / bailian no longer have entries in CHANNEL_HOST_FIELDS. Calling
    `channel_host('gmi', ...)` returns the supplied default — base URL for
    these channels lives in the seed definition now."""
    monkeypatch.setattr(db, "get_setting", lambda k, d=None: d)
    assert channel_host("gmi", "base_url", "https://seed-default/v1") == "https://seed-default/v1"
    assert channel_host("bailian", "base_url", "https://seed-default/v1") == "https://seed-default/v1"