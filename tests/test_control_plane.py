import asyncio

import pytest

from accounts import control_plane
from storage import database as db
from accounts import auth_manager


def test_create_key_requires_channel(isolated_db):
    from fastapi import HTTPException
    from gateway import server

    with pytest.raises(HTTPException) as err:
        server._validate_key_channel("")
    assert err.value.status_code == 400


def test_preview_import_roundtrip(isolated_db, tmp_path, monkeypatch):
    info = tmp_path / "a.info"
    info.write_text(
        '{"account":{"uid":"u-1","nickname":"n"},"auth":{"accessToken":"tok","refreshToken":"ref"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(auth_manager, "candidate_auth_dirs", lambda auth_dir=None: [tmp_path])
    monkeypatch.setattr(auth_manager, "find_auth_files", lambda auth_dir=None: [info])
    monkeypatch.setattr(
        auth_manager,
        "discover_auth_files",
        lambda auth_dir=None: {
            "dirs": [{"path": str(tmp_path), "exists": True, "file_count": 1}],
            "files": [
                {
                    "path": str(info),
                    "valid": True,
                    "already_imported": False,
                    "account_name": "n",
                    "uid_masked": "u-1",
                }
            ],
            "file_count": 1,
            "valid_count": 1,
        },
    )
    preview = control_plane.discover("workbuddy")
    assert preview["preview_token"]
    result = control_plane.import_channel("workbuddy", preview["preview_token"], [str(info)])
    assert result["imported"] == 1
    rows = db.list_accounts(provider="workbuddy")
    assert len(rows) == 1
    assert rows[0]["access_token"] == "tok"

    info.write_text(
        '{"account":{"uid":"u-1","nickname":"n"},"auth":{"accessToken":"tok2","refreshToken":"ref2"}}',
        encoding="utf-8",
    )
    db.update_account(rows[0]["id"], {"weight": 7, "priority": 3})
    preview = control_plane.discover("workbuddy")
    result = control_plane.import_channel("workbuddy", preview["preview_token"], [str(info)])
    assert result["updated"] == 1
    row = db.get_account(rows[0]["id"])
    assert row["access_token"] == "tok2"
    assert row["weight"] == 7
    assert row["priority"] == 3


def test_discover_marks_docker_host_auth_limited(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_DOCKER", "1")
    qclaw = control_plane.discover("qclaw")
    assert qclaw["runtime"]["container"] is True
    assert qclaw["runtime"]["host_auth_limited"] is True
    workbuddy = control_plane.discover("workbuddy")
    assert workbuddy["runtime"]["container"] is True
    assert workbuddy["runtime"]["host_auth_limited"] is False


def test_credit_summary_has_null_total(isolated_db):
    payload = asyncio.run(control_plane.credit_summary())
    assert payload["total_balance"] is None
    assert "channels" in payload
    assert any(item["id"] == "workbuddy" for item in payload["channels"])


def test_credit_summary_qclaw_omits_token_cap(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw")
    db.add_account(
        {
            "name": "qc",
            "uid": "q1",
            "provider": "qclaw",
            "access_token": "sk",
            "status": "active",
        }
    )
    payload = asyncio.run(control_plane.credit_summary())
    qclaw = next(item for item in payload["channels"] if item["id"] == "qclaw")
    assert qclaw["unit"] == "credit"
    assert qclaw["remaining"] is None
    assert qclaw["unsupported"] is True


def test_startup_does_not_import_by_default(isolated_db, monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_AUTO_IMPORT", raising=False)
    called = []

    def fake_discover(channel=None, auth_dir=None):
        called.append(channel or "workbuddy")
        return {"files": [], "dirs": [], "preview_token": "x"}

    monkeypatch.setattr(control_plane, "discover", fake_discover)
    monkeypatch.setattr(
        control_plane,
        "import_channel",
        lambda *args, **kwargs: called.append("imported") or {"imported": 1},
    )
    summary = control_plane.startup_scan()
    assert summary["auto_import"] is False


def test_credit_rate_default_and_override(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traesolo")
    # 默认换算率
    view = control_plane.channel_model_view("traesolo")
    assert view["credit_rate"] == 1000.0
    assert view["credit_rate_default"] == 1000.0
    assert view["credit_rate_customized"] is False

    # 自定义换算率
    control_plane.set_channel_models("traesolo", credit_rate=250.0, set_rate=True)
    view = control_plane.channel_model_view("traesolo")
    assert view["credit_rate"] == 250.0
    assert view["credit_rate_customized"] is True

    # 重置回默认
    control_plane.set_channel_models("traesolo", credit_rate=None, set_rate=True)
    view = control_plane.channel_model_view("traesolo")
    assert view["credit_rate"] == 1000.0
    assert view["credit_rate_customized"] is False


def test_credit_rate_validation(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traesolo")
    with pytest.raises(ValueError):
        control_plane.set_channel_models("traesolo", credit_rate=-1, set_rate=True)
    with pytest.raises(ValueError):
        control_plane.set_channel_models("traesolo", credit_rate="abc", set_rate=True)


def test_credit_rate_only_no_models(isolated_db, monkeypatch):
    """只传 credit_rate 也应能保存（不影响 models/aliases）。"""
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traesolo")
    view = control_plane.set_channel_models("traesolo", credit_rate=500.0, set_rate=True)
    assert view["credit_rate"] == 500.0
