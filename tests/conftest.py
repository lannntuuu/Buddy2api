"""共享 pytest fixtures：隔离的临时数据库 + 固定 master key + 干净的 P0 缓存/连接池状态。"""
import shutil
import uuid
from pathlib import Path

import pytest

from storage import credential_crypto
from storage import credit_cache
from storage import database as db


@pytest.fixture()
def isolated_db(monkeypatch):
    """Isolated temp DB under the repo .tmp dir.

    Uses a fixed workspace path instead of pytest `tmp_path` because the
    sandbox blocks writes / directory enumeration under the system TEMP
    root (e.g. `%LOCALAPPDATA%\\Temp\\dsh-*`), which would fail `tmp_path`
    setup here. A unique subdir + cleanup keeps tests isolated.
    """
    workdir = Path(__file__).resolve().parent.parent / ".tmp" / f"shared-test-{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "DB_PATH", workdir / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    credit_cache.invalidate()          # credit-summary 快照是进程级状态，跨用例必须清掉
    credit_cache.mark_refreshing(False)
    db.init_db()
    yield workdir / "gateway.db"
    credential_crypto.reset_cache()
    credit_cache.invalidate()
    credit_cache.mark_refreshing(False)
    shutil.rmtree(workdir, ignore_errors=True)
