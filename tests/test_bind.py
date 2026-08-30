import asyncio

import pytest
from fastapi import HTTPException

import providers
from gateway import router
from providers.protocol import (
    InvalidModel,
    KeyChannelMismatch,
    UnknownChannel,
    UnknownModel,
)


class _QwenStub:
    id = "qwenwork"
    display_name = "QwenWork"
    checkin_supported = False

    def list_models(self):
        return [{"id": "auto"}, {"id": "qwork-advanced"}]

    def alias_map(self):
        return {}

    def accepts_model(self, inner):
        return inner in {"auto", "qwork-advanced"}

    def translate_model(self, model):
        return model

    def pick_account(self, exclude_ids=None):
        return None

    async def pick_account_with_fallback(self, exclude_ids=None):
        return None

    async def has_usable_account(self):
        return False

    async def chat_completions(self, payload, api_key_info):
        return ("json", {"model": payload.get("model"), "id": "stub"})


@pytest.fixture()
def qwen_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    stub = _QwenStub()
    previous = providers._LOADED.get("qwenwork")
    providers.register_provider(stub)
    yield stub
    if previous is not None:
        providers._LOADED["qwenwork"] = previous
    else:
        providers._LOADED.pop("qwenwork", None)


def test_unprefixed_auto_on_workbuddy_key():
    bound = router.bind({"model": "auto"}, {"default_channel": "workbuddy"})
    assert bound.channel == "workbuddy"
    assert bound.inner == "auto"
    assert bound.original == "auto"


def test_namespaced_workbuddy_strips_inner():
    bound = router.bind({"model": "workbuddy/glm-5.2"}, {"default_channel": "workbuddy"})
    assert bound.channel == "workbuddy"
    assert bound.inner == "glm-5.2"
    assert bound.original == "workbuddy/glm-5.2"


def test_workbuddy_gpt_alias_accepted():
    bound = router.bind({"model": "workbuddy/gpt-5.5"}, {"default_channel": "workbuddy"})
    assert bound.inner == "gpt-5.5"


def test_bare_qwork_advanced_on_workbuddy_key_rejected():
    with pytest.raises(UnknownModel):
        router.bind({"model": "qwork-advanced"}, {"default_channel": "workbuddy"})


def test_prefix_mismatch_is_403(qwen_enabled):
    with pytest.raises(KeyChannelMismatch):
        router.bind(
            {"model": "workbuddy/auto"},
            {"default_channel": "qwenwork"},
        )


def test_unprefixed_auto_on_qwenwork_key(qwen_enabled):
    bound = router.bind({"model": "auto"}, {"default_channel": "qwenwork"})
    assert bound.channel == "qwenwork"
    assert bound.inner == "auto"


def test_unprefixed_auto_on_disabled_qwenwork_key(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(UnknownChannel):
        router.bind({"model": "auto"}, {"default_channel": "qwenwork"})


def test_glm_on_qwenwork_key_rejected(qwen_enabled):
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "qwenwork"})


def test_qwenwork_flag_off_namespaced(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(UnknownChannel):
        router.bind(
            {"model": "qwenwork/qwork-advanced"},
            {"default_channel": "qwenwork"},
        )


def test_qwenwork_namespaced_ok(qwen_enabled):
    bound = router.bind(
        {"model": "qwenwork/qwork-advanced"},
        {"default_channel": "qwenwork"},
    )
    assert bound.channel == "qwenwork"
    assert bound.inner == "qwork-advanced"


def test_qwenwork_glm_inner_invalid(qwen_enabled):
    with pytest.raises(InvalidModel):
        router.bind(
            {"model": "qwenwork/glm-5.2"},
            {"default_channel": "qwenwork"},
        )


def test_ensure_usable_503_does_not_need_workbuddy(qwen_enabled):
    with pytest.raises(HTTPException) as err:
        asyncio.run(router.ensure_usable("qwenwork"))
    assert err.value.status_code == 503
    assert err.value.detail["error"]["code"] == "channel_unavailable"
