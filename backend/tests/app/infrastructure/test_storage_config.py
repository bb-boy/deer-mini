"""SQLite 与 Thread Workspace 环境配置测试。"""

from pathlib import Path

from app.infrastructure import database
from app.services import thread_service


def test_database_path_can_be_overridden_by_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "isolated" / "e2e.db"
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(configured))

    assert database.resolve_database_path() == configured.resolve()


def test_data_root_can_be_overridden_by_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "isolated-users"
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(configured))

    assert thread_service.resolve_data_root() == configured.resolve()


def test_relative_storage_paths_are_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", "relative/e2e.db")
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", "relative/users")

    for resolver in (
        database.resolve_database_path,
        thread_service.resolve_data_root,
    ):
        try:
            resolver()
            assert False, "相对存储路径应被拒绝"
        except ValueError as error:
            assert "必须是绝对路径" in str(error)
