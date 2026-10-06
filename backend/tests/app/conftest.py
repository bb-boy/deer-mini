"""旧回归测试默认关闭自动记忆；记忆用例显式启用并使用临时存储。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_storage_defaults(tmp_path, monkeypatch):
    """为旧用例提供独立存储基线；具体测试仍可覆盖路径和环境配置。"""
    from app.infrastructure import database
    from app.services import thread_service

    monkeypatch.delenv("DEER_MINI_DATABASE_PATH", raising=False)
    monkeypatch.delenv("DEER_MINI_DATA_ROOT", raising=False)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "default.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "default-users")
    database.initialize_database()


@pytest.fixture(autouse=True)
def disable_automatic_memory_by_default(monkeypatch):
    """避免旧模型桩意外承接后台提取调用；生产默认配置不受影响。"""
    monkeypatch.setenv("DEER_MINI_MEMORY_ENABLED", "false")
