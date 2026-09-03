"""Tests for per-channel upstream host override."""
import pytest
from providers.host_override import channel_host, CHANNEL_HOST_FIELDS
from storage import database as db

def test_default_when_unset(monkeypatch):
    monkeypatch.setattr(db, "get_setting", lambda k, d=None: d)
    assert channel_host("gmi", "base_url", "https://default") == "https://default"
    assert channel_host("qwenwork", "gateway", "https://default") == "https://default"

def test_override_when_set(monkeypatch):
    monkeypatch.setattr(db, "get_setting",
        lambda k, d=None: {"gmi": {"base_url": "https://mirror.example.com/v1"}})
    assert channel_host("gmi", "base_url", "https://default") == "https://mirror.example.com/v1"
    assert channel_host("qwenwork", "gateway", "https://default") == "https://default"

def test_channel_host_fields_phase_a():
    assert CHANNEL_HOST_FIELDS == {"gmi": ("base_url",), "qwenwork": ("gateway",)}
