"""持久任务的原子登记、取消、窗口和内容保留。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.domain.threads import Thread
from app.infrastructure.database import connect
from app.repositories.thread_repository import ThreadRepository


def setup_repo():
    from app.repositories.memory_task_repository import MemoryTaskRepository
    now = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
    repo = MemoryTaskRepository(clock=lambda: now[0])
    ThreadRepository().create(Thread(id="thread", user_id="alice", workspace_path="/tmp/workspace"))
    return repo, now


def operation(identity="op"):
    data = {"operation_id": identity, "memory_id": "user_" + "a" * 32,
            "expected_version": None, "result_digest": "digest", "record": {"content": "PRIVATE"}}
    return SimpleNamespace(**data, to_dict=lambda: data)


def test_registration_is_atomic_and_checks_owner():
    repo, now = setup_repo()
    with pytest.raises(ValueError):
        repo.register("bob", "thread", "run", [operation()])
    with connect() as conn:
        conn.execute("CREATE TRIGGER fail_item BEFORE INSERT ON memory_save_items BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception):
        repo.register("alice", "thread", "run", [operation()])
    assert repo.list_tasks("alice") == []


def test_cancel_clears_payload_preserves_success_and_history():
    repo, now = setup_repo()
    task_id = repo.register("alice", "thread", "run", [operation("one"), operation("two")])
    repo.claim("alice", task_id, "one")
    repo.finish("alice", task_id, "one", status="success", phase="complete")
    repo.cancel("alice", task_id)
    task = repo.get("alice", task_id, include_content=True)
    assert [i["status"] for i in task["items"]] == ["success", "cancelled"]
    assert all(i["payload"] is None for i in task["items"])
    assert task["status"] == "partial"
    assert task["closed_at"] == now[0].isoformat()
    assert repo.get("bob", task_id) is None
    now[0] += timedelta(days=29)
    assert repo.cleanup("alice") == 0
    now[0] += timedelta(days=2)
    assert repo.cleanup("alice") == 1


def test_thread_deletion_cancels_and_rejects_late_registration():
    repo, _ = setup_repo()
    task_id = repo.register("alice", "thread", "run", [operation()])
    ThreadRepository().delete("thread", "alice")
    assert repo.get("alice", task_id)["items"][0]["status"] == "cancelled"
    with pytest.raises(ValueError):
        repo.register("alice", "thread", "late", [operation("late")])
    with pytest.raises(ValueError):
        repo.retry("alice", task_id)


def test_manual_retry_resets_window_but_keeps_history_and_rejects_conflict():
    repo, now = setup_repo()
    task_id = repo.register("alice", "thread", "run", [operation()])
    registered = repo.get("alice", task_id)
    assert registered["deadline_at"] == (now[0] + timedelta(hours=24)).isoformat()
    repo.claim("alice", task_id, "op")
    repo.finish("alice", task_id, "op", status="failed", phase="write", error={"category": "no_space"})
    now[0] += timedelta(days=2)
    assert repo.cleanup("alice") == 0
    repo.retry("alice", task_id)
    task = repo.get("alice", task_id)
    assert task["window"] == 2 and len(task["history"]) == 1
    assert task["deadline_at"] == (now[0] + timedelta(hours=24)).isoformat()
    repo.claim("alice", task_id, "op")
    repo.finish("alice", task_id, "op", status="conflict", phase="compare")
    with pytest.raises(ValueError):
        repo.retry("alice", task_id)


def test_cancel_inflight_retains_payload_until_result_is_confirmed():
    repo, _ = setup_repo()
    task_id = repo.register("alice", "thread", "run", [operation()])
    repo.claim("alice", task_id, "op")
    repo.cancel("alice", task_id)
    task = repo.get("alice", task_id, include_content=True)
    assert task["items"][0]["payload"] is not None and task["closed_at"] is None
    repo.finish("alice", task_id, "op", status="success", phase="complete")
    task = repo.get("alice", task_id, include_content=True)
    assert task["items"][0]["status"] == "success" and task["items"][0]["payload"] is None


def test_default_details_redact_payload_and_errors():
    repo, _ = setup_repo()
    task_id = repo.register("alice", "thread", "run", [operation()])
    repo.claim("alice", task_id, "op")
    repo.finish("alice", task_id, "op", status="failed", phase="write",
                error={"category": "io", "message": "PRIVATE"})
    assert "PRIVATE" not in str(repo.get("alice", task_id))
    assert "PRIVATE" in str(repo.get("alice", task_id, include_content=True))


def test_crashed_attempt_is_kept_in_history_when_reclaimed():
    repo, now = setup_repo()
    task = repo.register("alice", "thread", "run", [operation()])
    repo.claim("alice", task, "op")
    now[0] += timedelta(seconds=10)
    repo.recover_inflight()
    repo.claim("alice", task, "op")
    repo.finish("alice", task, "op", status="success", phase="complete")
    history = repo.get("alice", task)["history"]
    assert len(history) == 2
    assert history[0]["status"] == "unconfirmed"
    assert history[0]["attempted_at"] != history[1]["attempted_at"]
