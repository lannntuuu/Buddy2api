"""共享 pytest fixtures：隔离的临时数据库 + 固定 master key + 干净的 P0 缓存/连接池状态。"""
import pytest

from storage import credential_crypto
from storage import credit_cache
from storage import database as db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    credit_cache.invalidate()          # credit-summary 快照是进程级状态，跨用例必须清掉
    credit_cache.mark_refreshing(False)
    db.init_db()
    yield path
    credential_crypto.reset_cache()
    credit_cache.invalidate()
    credit_cache.mark_refreshing(False)
