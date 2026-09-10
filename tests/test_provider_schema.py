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
    from providers import custom_channels

    # Note: do NOT stub db.get_setting to default here (same reason as
    # test_set_enabled_channels_with_order): a stub would hide the seeded
    # gmi definition from `_known_set()`, and the write path below must be
    # able to read it back from the real settings store.
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    assert providers.env_locked() is False
    # gmi is no longer a reserved built-in id (spec 39): it is addressable
    # only while a definition for it exists, exactly like bailian. Seed it so
    # the channel is known to the write path.
    custom_channels.seed_initial_definitions()
    custom_channels.invalidate_cache(None)

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


def test_bailian_is_known_and_opt_in(monkeypatch, isolated_db):
    """bailian mirrors gmi: reachable once its definition exists, opt-in.

    After the data-driven migration, bailian is a plain data-driven custom
    channel: its id is NOT a reserved literal (admins may delete the seed
    definition and recreate it), and reachability comes solely from the
    definition living in the `custom_channels` settings key. The fixture
    calls `seed_initial_definitions()` to make the channel reachable via
    `get_provider`.
    """
    import providers
    from providers import custom_channels

    # Seed bailian (and gmi) so the custom definition exists in the settings key.
    custom_channels.seed_initial_definitions()
    custom_channels.invalidate_cache(None)

    # Not a reserved literal anymore — deletable/recreatable as a normal
    # custom channel id.
    assert "bailian" not in providers.KNOWN_CHANNEL_IDS
    # Not in the default ON set (opt-in).
    assert "bailian" not in providers.DEFAULT_PROVIDER_IDS
    # Opt-in via env → provider available and locked-respecting order.
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,bailian")
    assert providers.get_provider("bailian") is not None
    assert providers.is_channel_enabled("bailian") is True


def test_gmi_is_non_reserved_and_opt_in(monkeypatch, isolated_db):
    """Spec 39: gmi mirrors bailian — non-reserved, reachable via definition, opt-in.

    `gmi` used to be hardcoded in `ChannelId` / `KNOWN_CHANNEL_IDS`, which made
    the seed channel undeletable: `DELETE /admin/channels/custom/gmi` cleared
    the definition correctly, but `known_channel_ids()` (built-ins ∪ custom)
    resurrected the id from the built-in list, so `/admin/channels` kept
    rendering a zombie row. It is now purely data-driven, exactly like bailian.

    This is the symmetric counterpart of `test_bailian_is_known_and_opt_in` —
    the bailian assertion existed, the gmi one did not, which is why the
    residue went unnoticed.
    """
    import providers
    from providers import custom_channels

    # Seed gmi (and bailian) so the custom definition exists in the settings key.
    custom_channels.seed_initial_definitions()
    custom_channels.invalidate_cache(None)

    # Not a reserved literal anymore — deletable/recreatable as a normal
    # custom channel id.
    assert "gmi" not in providers.KNOWN_CHANNEL_IDS
    # Not in the default ON set (opt-in).
    assert "gmi" not in providers.DEFAULT_PROVIDER_IDS
    # Opt-in via env → provider available (reachability comes from the
    # definition, not from a built-in registration).
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,gmi")
    assert providers.get_provider("gmi") is not None
    assert providers.is_channel_enabled("gmi") is True


def test_deleted_gmi_definition_does_not_resurrect_from_builtins(isolated_db):
    """THE spec-39 regression guard: a deleted seed channel must stay deleted.

    `known_channel_ids()` is built-ins ∪ custom definitions. While `gmi` sat in
    the built-in `KNOWN_CHANNEL_IDS`, removing its definition could never remove
    the id, so the admin UI kept showing an undeletable row. This test drives
    the real delete path (`custom_channels.delete_definition`) and asserts the
    id is gone from `known_channel_ids()` — it fails loudly if anyone hardcodes
    a seed id back into the built-in list.
    """
    import providers
    from providers import custom_channels

    # Ensure a gmi definition exists: seed it, or upsert a temp one when the
    # settings key already exists without gmi (e.g. a deployment that already
    # deleted the seed — the exact state this guard protects).
    custom_channels.seed_initial_definitions()
    custom_channels.invalidate_cache(None)
    if custom_channels.get_definition("gmi") is None:
        custom_channels.upsert_definition(
            {
                "id": "gmi",
                "display_name": "GMI Cloud",
                "base_url": "https://api.gmi-serving.com/v1",
                "models": ["zai-org/GLM-5.3-Flash"],
                "aliases": {"auto": "zai-org/GLM-5.3-Flash"},
                "source": "test",
            }
        )
        custom_channels.invalidate_cache(None)

    # Precondition: with a definition present, gmi is addressable.
    assert custom_channels.get_definition("gmi") is not None
    assert "gmi" in providers.known_channel_ids()

    # The admin deletes the seed channel.
    assert custom_channels.delete_definition("gmi") is True

    # The deleted id must NOT come back from the built-in list.
    assert "gmi" not in providers.known_channel_ids()
    assert providers.is_known_channel("gmi") is False
    # And it is no longer resolvable to a provider.
    assert providers.get_provider("gmi") is None

    # A stale `gmi` left in enabled_channels / channel_order (old DB) is
    # filtered out by `_read_db_list`, so no ghost entry leaks into reads.
    db.set_setting("enabled_channels", ["workbuddy", "gmi"])
    db.set_setting("channel_order", ["workbuddy", "gmi"])
    assert "gmi" not in providers.enabled_provider_ids()

    # Not banned, just not reserved: the admin may recreate the same id.
    assert "gmi" not in custom_channels.reserved_ids()
    custom_channels.upsert_definition(
        {
            "id": "gmi",
            "display_name": "GMI Cloud",
            "base_url": "https://api.gmi-serving.com/v1",
            "models": ["zai-org/GLM-5.3-Flash"],
            "aliases": {"auto": "zai-org/GLM-5.3-Flash"},
            "source": "test",
        }
    )
    custom_channels.invalidate_cache(None)
    assert "gmi" in providers.known_channel_ids()

    # Leave no cached provider behind for later tests (the cache is process-global).
    custom_channels.invalidate_cache(None)


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