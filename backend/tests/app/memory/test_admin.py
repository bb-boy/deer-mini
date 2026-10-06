"""后台管理入口使用真实临时 SQLite，验证用户范围、只读与显式确认。"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.infrastructure import database


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, DEER_MINI_DATABASE_PATH=str(database.resolve_database_path()))
    return subprocess.run(
        [sys.executable, "-m", "app.memory.admin", *args],
        cwd=Path(__file__).resolve().parents[3], env=env,
        capture_output=True, text=True, timeout=10,
    )


def register_task(user_id: str = "alice", status: str = "pending"):
    from app.domain.threads import Thread
    from app.repositories.memory_task_repository import MemoryTaskRepository
    from app.repositories.thread_repository import ThreadRepository

    repo = MemoryTaskRepository()
    thread_id = f"thread-{user_id}"
    ThreadRepository().create(Thread(id=thread_id, user_id=user_id, workspace_path="/unused"))
    payload = {
        "operation_id": f"operation-{user_id}", "memory_id": "user_" + "a" * 32,
        "expected_version": None, "result_digest": "digest",
        "record": {"content": "PRIVATE MEMORY BODY", "name": "PRIVATE MEMORY TITLE"},
    }
    task_id = repo.register(user_id, thread_id, "run", [SimpleNamespace(**payload, to_dict=lambda: payload)])
    if status != "pending":
        repo.claim(user_id, task_id, payload["operation_id"])
        repo.finish(user_id, task_id, payload["operation_id"], status=status, phase="write")
    return repo, task_id, payload["operation_id"]


@pytest.mark.parametrize("args", [
    ["retry", "task"], ["cancel", "task"],
    ["resolve-conflict", "task", "operation"], ["cleanup"],
])
def test_mutations_require_confirmation_before_opening_database(args, monkeypatch, tmp_path):
    missing = tmp_path / "missing" / "private.db"
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(missing))
    result = run_cli("--user", "alice", *args)
    assert result.returncode == 2
    assert "--confirm" in result.stderr
    assert not missing.parent.exists()


def test_user_is_required_and_invalid_arguments_do_not_echo_private_values():
    result = run_cli("list", "PRIVATE INPUT")
    assert result.returncode == 2
    assert "PRIVATE INPUT" not in result.stderr


def test_missing_database_is_not_created(monkeypatch, tmp_path):
    missing = tmp_path / "missing" / "private.db"
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(missing))
    result = run_cli("--user", "alice", "list")
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]
    assert str(missing) not in result.stderr
    assert not missing.parent.exists()


def test_uninitialized_database_is_not_migrated(monkeypatch, tmp_path):
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sentinel (id INTEGER)")
    before = path.read_bytes()
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(path))
    result = run_cli("--user", "alice", "list")
    assert result.returncode == 1
    assert "schema" in json.loads(result.stderr)["error"]
    assert "Traceback" not in result.stderr
    assert path.read_bytes() == before


def test_list_and_show_are_scoped_and_only_explicit_show_displays_content():
    repo, task_id, _ = register_task()
    register_task("bob")
    before = database.resolve_database_path().read_bytes()
    result = run_cli("--user", "alice", "list")
    assert result.returncode == 0, result.stderr
    assert [task["id"] for task in json.loads(result.stdout)] == [task_id]
    assert "PRIVATE" not in result.stdout
    result = run_cli("--user", "alice", "show", task_id)
    assert result.returncode == 0, result.stderr
    assert "PRIVATE" not in result.stdout
    task = json.loads(result.stdout)
    assert task["items"] and "history" in task and "deadline_at" in task
    result = run_cli("--user", "alice", "show", task_id, "--include-content")
    assert result.returncode == 0, result.stderr
    assert "PRIVATE MEMORY BODY" in result.stdout
    result = run_cli("--user", "bob", "show", task_id, "--include-content")
    assert result.returncode == 1
    assert "PRIVATE" not in result.stdout + result.stderr
    assert database.resolve_database_path().read_bytes() == before


def test_retry_reopens_window_and_preserves_history():
    repo, task_id, _ = register_task(status="failed")
    old = repo.get("alice", task_id)
    result = run_cli("--user", "alice", "retry", task_id, "--confirm")
    assert result.returncode == 0, result.stderr
    task = repo.get("alice", task_id)
    assert task["window"] == old["window"] + 1
    assert task["history"] == old["history"]
    assert task["items"][0]["status"] == "pending"
    assert "PRIVATE" not in result.stdout + result.stderr
    remaining = datetime.fromisoformat(task["deadline_at"]) - datetime.now(timezone.utc)
    assert timedelta(hours=23, minutes=59) < remaining <= timedelta(hours=24)


def test_cancel_rejects_other_user_and_clears_only_owned_task_content():
    repo, task_id, _ = register_task()
    result = run_cli("--user", "bob", "cancel", task_id, "--confirm")
    assert result.returncode == 1
    assert repo.get("alice", task_id)["items"][0]["status"] == "pending"
    result = run_cli("--user", "alice", "cancel", task_id, "--confirm")
    assert result.returncode == 0, result.stderr
    task = repo.get("alice", task_id, include_content=True)
    assert task["items"][0]["status"] == "cancelled"
    assert task["items"][0]["payload"] is None


def test_conflict_cannot_be_retried_and_resolution_cancels_old_operation():
    repo, task_id, operation_id = register_task(status="conflict")
    result = run_cli("--user", "alice", "retry", task_id, "--confirm")
    assert result.returncode == 1
    assert repo.get("alice", task_id)["items"][0]["status"] == "conflict"
    result = run_cli("--user", "alice", "resolve-conflict", task_id, operation_id, "--confirm")
    assert result.returncode == 0, result.stderr
    assert repo.get("alice", task_id)["items"][0]["status"] == "cancelled"


def test_cleanup_deletes_only_old_resolved_tasks_for_requested_user():
    repo, alice_id, _ = register_task(status="success")
    _, bob_id, _ = register_task("bob", status="success")
    _, failed_id, _ = register_task("carol", status="failed")
    with database.connect() as conn:
        conn.execute("UPDATE memory_save_tasks SET closed_at=? WHERE id IN (?, ?)",
                     ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), alice_id, bob_id))
    result = run_cli("--user", "alice", "cleanup", "--confirm")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"deleted_tasks": 1}
    assert repo.get("alice", alice_id) is None
    assert repo.get("bob", bob_id) is not None
    assert repo.get("carol", failed_id) is not None


def test_repository_failure_is_reported_without_exception_content(monkeypatch, capsys):
    from app.memory import admin

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE FAILURE AND CREDENTIAL")

    monkeypatch.setattr(admin.MemoryTaskRepository, "list_tasks", fail)
    assert admin.main(["--user", "alice", "list"]) == 1
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert json.loads(output.err)["error"]


def test_commit_failure_never_reports_acceptance(monkeypatch, capsys):
    from app.memory import admin
    from app.storage.errors import StorageError
    from app.storage.sqlite import StorageConnection

    _, task_id, _ = register_task()

    def fail_commit(self):
        raise StorageError(backend="sqlite", operation="commit", category="io",
                           stage="commit", commit_state="uncertain", recovery="verify")

    monkeypatch.setattr(StorageConnection, "commit", fail_commit)
    assert admin.main(["--user", "alice", "cancel", task_id, "--confirm"]) == 1
    output = capsys.readouterr()
    assert "accepted" not in output.out
    assert json.loads(output.err)["error"]
    with sqlite3.connect(database.resolve_database_path()) as conn:
        assert conn.execute("SELECT status FROM memory_save_items WHERE task_id=?", (task_id,)).fetchone()[0] == "pending"


def test_database_removed_after_preflight_is_not_recreated(monkeypatch, capsys, tmp_path):
    from app.memory import admin

    missing = tmp_path / "removed.db"
    monkeypatch.setattr(admin, "resolve_database_path", lambda: missing)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    assert admin.main(["--user", "alice", "cleanup", "--confirm"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]
    assert not missing.exists()


def test_read_command_uses_sqlite_read_only_connection(monkeypatch, capsys):
    from app.memory import admin

    repo, task_id, _ = register_task()

    def accidental_write(self, *args, **kwargs):
        with self._connect() as conn:
            conn.execute("UPDATE memory_save_tasks SET status='failed'")
        return []

    monkeypatch.setattr(admin.MemoryTaskRepository, "list_tasks", accidental_write)
    assert admin.main(["--user", "alice", "list"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]
    assert repo.get("alice", task_id)["status"] == "pending"


def test_conflict_resolution_rejects_force_option():
    repo, task_id, operation_id = register_task(status="conflict")
    result = run_cli("--user", "alice", "resolve-conflict", task_id, operation_id, "--confirm", "--force")
    assert result.returncode == 2
    assert repo.get("alice", task_id)["items"][0]["status"] == "conflict"


def current_memory_task(tmp_path, monkeypatch):
    from app.domain.threads import Thread
    from app.memory.store import MemoryStore
    from app.repositories.memory_task_repository import MemoryTaskRepository
    from app.repositories.thread_repository import ThreadRepository

    root = tmp_path / "users"
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(root))
    store = MemoryStore(root)
    ThreadRepository().create(Thread(id="thread", user_id="alice", workspace_path="/unused"))
    current = store.upsert("alice", kind="user", name="Preference", description="language",
                           content="PRIVATE CURRENT BODY", source_thread_id="thread", source_run_id="run")
    operation = store.prepare("alice", [dict(action="update", type="user", name="Preference",
        description="language", content="PRIVATE PENDING BODY", memory_id=current.id)],
        source_thread_id="thread", source_run_id="run")[0]
    repo = MemoryTaskRepository()
    task_id = repo.register("alice", "thread", "run", [operation])
    return task_id, current, root / "alice" / "memories"


def test_current_memory_requires_explicit_flag_and_does_not_repair_index(tmp_path, monkeypatch):
    task_id, current, directory = current_memory_task(tmp_path, monkeypatch)
    (directory / "MEMORY.md").unlink()
    before = (directory / f"{current.id}.md").read_bytes()
    result = run_cli("--user", "alice", "show", task_id)
    assert result.returncode == 0, result.stderr
    assert "PRIVATE" not in result.stdout
    result = run_cli("--user", "alice", "show", task_id, "--include-content")
    assert result.returncode == 0, result.stderr
    assert "PRIVATE PENDING BODY" in result.stdout and "PRIVATE CURRENT BODY" not in result.stdout
    result = run_cli("--user", "alice", "show", task_id, "--include-current")
    assert result.returncode == 0, result.stderr
    item = json.loads(result.stdout)["items"][0]
    assert item["current_memory"]["content"] == "PRIVATE CURRENT BODY"
    assert "PRIVATE PENDING BODY" not in result.stdout
    assert item["current_memory"]["updated_at"] == current.updated_at.isoformat()
    assert not (directory / "MEMORY.md").exists()
    assert (directory / f"{current.id}.md").read_bytes() == before


def test_current_memory_checks_task_owner_before_any_file_read(tmp_path, monkeypatch, capsys):
    from app.memory import admin
    from app.memory.store import MemoryStore

    task_id, _, _ = current_memory_task(tmp_path, monkeypatch)
    reads = []

    def read(self, *args, **kwargs):
        reads.append(args)
        raise AssertionError("must not read another user's memory")

    monkeypatch.setattr(MemoryStore, "read", read)
    assert admin.main(["--user", "bob", "show", task_id, "--include-current"]) == 1
    assert reads == []
    assert "PRIVATE" not in capsys.readouterr().err


@pytest.mark.parametrize("configured_root", [None, "relative/users"])
def test_current_memory_requires_explicit_absolute_data_root(tmp_path, monkeypatch, configured_root):
    task_id, _, _ = current_memory_task(tmp_path, monkeypatch)
    if configured_root is None:
        monkeypatch.delenv("DEER_MINI_DATA_ROOT")
    else:
        monkeypatch.setenv("DEER_MINI_DATA_ROOT", configured_root)
    result = run_cli("--user", "alice", "show", task_id, "--include-current")
    assert result.returncode == 1
    assert "DEER_MINI_DATA_ROOT" in result.stderr
    assert "PRIVATE" not in result.stdout + result.stderr


def test_current_memory_corruption_fails_without_exposing_or_changing_body(tmp_path, monkeypatch):
    task_id, current, directory = current_memory_task(tmp_path, monkeypatch)
    body = directory / f"{current.id}.md"
    body.write_text("PRIVATE CORRUPT BODY")
    result = run_cli("--user", "alice", "show", task_id, "--include-current")
    assert result.returncode == 1
    assert "PRIVATE" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert body.read_text() == "PRIVATE CORRUPT BODY"
