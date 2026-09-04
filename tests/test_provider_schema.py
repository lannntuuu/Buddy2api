import asyncio


from storage import database as db
from accounts import auth_manager


def _fresh_default(monkeypatch):
    """Reset db.get_setting to return defaults so we hit the code-fallback path."""
    monkeypatch.setattr(db, "get_setting", lambda key, default=None: default)


def test_default_registry_first_boot_alphabetical_with_workbuddy_locked(monkeypatch):
    """Fresh boot with no env var and no db write: alphabetical, but locked to workbuddy first."""
    import providers

    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    _fresh_default(monkeypatch)
    got = providers.enabled_provider_ids()
    # workbuddy is forced to the front; the rest follow alphabetical order.
    assert got[0] == "workbuddy"
    tail = got[1:]
    assert sorted(tail) == tail
    # The full set is the known channel set.
    assert set(got) == set(providers.KNOWN_CHANNEL_IDS)


def test_env_var_overrides_db(monkeypatch):
    import providers

    _fresh_default(monkeypatch)
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    assert providers.enabled_provider_ids() == ["workbuddy"]
    assert providers.get_provider("qclaw") is None


def test_set_enabled_channels_persists_and_lock_first(monkeypatch, isolated_db):
    """UI write path: env unset, db.set_setting wins."""
    import providers

    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    _fresh_default(monkeypatch)
    assert providers.env_locked() is False
    saved = providers.set_enabled_channels(["gmi", "qwenwork"])
    # workbuddy forced on, then user's order preserved with gmi / qwenwork.
    # set_enabled_channels returns (enabled_ids, ordered_full); ordered is index 1.
    assert saved[1] == ["workbuddy", "gmi", "qwenwork"]

    import pytest
    with pytest.raises(ValueError):
        providers.set_enabled_channels(["workbuddy", "nope-not-a-channel"])


def test_set_enabled_channels_with_order(monkeypatch, isolated_db):
    """User drags to reorder. The order should be respected; workbuddy still first."""
    import providers

    # Note: do NOT mock db.get_setting to default here — the fresh isolated_db is
    # already empty, and we want the just-persisted order to be readable on the
    # subsequent enabled_provider_ids() call (a get_setting stub would hide it).
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    saved = providers.set_enabled_channels(
        ids=["workbuddy", "qclaw", "qwenwork", "traework", "traesolo"],
        order=["workbuddy", "traesolo", "traework", "qwenwork", "qclaw"],
    )
    # workbuddy first, then the user's drag order. saved is (enabled_ids, ordered_full).
    assert saved[1] == ["workbuddy", "traesolo", "traework", "qwenwork", "qclaw"]
    # And enabled_provider_ids honours that order on subsequent reads.
    assert providers.enabled_provider_ids() == saved[1]
    assert providers.get_channel_order() == saved[1]


def test_workbuddy_is_locked_first_after_drag(monkeypatch, isolated_db):
    """Even if the user puts workbuddy in the middle, the lock_first post-condition
    puts it back at index 0."""
    import providers

    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    _fresh_default(monkeypatch)
    saved = providers.set_enabled_channels(
        ids=["workbuddy", "qclaw", "qwenwork"],
        order=["qclaw", "workbuddy", "qwenwork"],
    )
    assert saved[1][0] == "workbuddy"


def test_disabled_channels_filtered_out(monkeypatch, isolated_db):
    """A disabled channel must not appear in enabled_provider_ids — this is the
    single source of truth for 'what the rest of the system sees'."""
    import providers

    # Fresh isolated_db is already empty; don't stub get_setting here or the
    # just-persisted enabled_channels would be hidden and the fallback (full
    # alphabetical set, now including bailian) would be returned instead.
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    providers.set_enabled_channels(
        ids=["workbuddy", "qclaw"],  # qwenwork / traework / traesolo / gmi off
        order=["workbuddy", "qclaw"],
    )
    enabled = providers.enabled_provider_ids()
    assert set(enabled) == {"workbuddy", "qclaw"}
    assert providers.is_channel_enabled("qwenwork") is False
    assert providers.is_channel_enabled("gmi") is False
    assert providers.is_channel_enabled("bailian") is False
    assert providers.get_provider("traesolo") is None
    assert providers.get_provider("bailian") is None


def test_bailian_is_known_and_opt_in(monkeypatch):
    """bailian mirrors gmi: a KNOWN channel that is opt-in, not on by default."""
    import providers

    # Known channel set must include bailian.
    assert "bailian" in providers.KNOWN_CHANNEL_IDS
    # Not in the default ON set (opt-in), but reachable via alphabetical fallback
    # when env is unset and nothing is persisted in the db.
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    _fresh_default(monkeypatch)
    assert "bailian" in providers.enabled_provider_ids()  # alphabetical fallback includes it
    assert "bailian" not in providers.DEFAULT_PROVIDER_IDS  # but NOT on by default
    # Opt-in via env → available and locked-respecting order.
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,bailian")
    assert providers.get_provider("bailian") is not None
    assert providers.is_channel_enabled("bailian") is True


def test_accounts_and_keys_have_channel_columns(isolated_db):
    account_id = db.add_account({"name": "wb", "uid": "u1", "access_token": "a"})
    account = db.get_account(account_id)
    assert account["provider"] == "workbuddy"
    key_id = db.add_api_key("sk-cb-test-channel", "k")
    keys = db.list_api_keys()
    row = next(item for item in keys if item["id"] == key_id)
    assert row["default_channel"] == "workbuddy"


def test_second_import_same_uid_updates_token(isolated_db):
    first = db.add_account(
        {"name": "wb", "uid": "same", "provider": "workbuddy", "access_token": "old", "weight": 3}
    )
    db.update_account(first, {"access_token": "new", "uid": "same"})
    rows = db.list_accounts(provider="workbuddy")
    assert len(rows) == 1
    assert rows[0]["access_token"] == "new"
    assert rows[0]["weight"] == 3


def test_workbuddy_pick_ignores_other_provider_expired(isolated_db, monkeypatch):
    wb = db.add_account(
        {
            "name": "wb",
            "uid": "wb-1",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "ok",
            "expires_at": 9_999_999_999_999,
        }
    )
    db.add_account(
        {
            "name": "qw",
            "uid": "qw-1",
            "provider": "qwenwork",
            "status": "expired",
            "access_token": "qw-token",
            "refresh_token": "qw-refresh",
        }
    )
    refreshed = []

    async def fake_refresh(account):
        refreshed.append(account.get("provider"))
        return True

    monkeypatch.setattr(auth_manager, "refresh_token", fake_refresh)
    picked = auth_manager.pick_account(provider="workbuddy")
    assert picked["id"] == wb
    asyncio.run(auth_manager.pick_account_with_fallback(provider="workbuddy"))
    assert "qwenwork" not in refreshed