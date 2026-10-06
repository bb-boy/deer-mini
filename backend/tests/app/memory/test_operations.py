"""已准备的记忆保存项在部分提交、恢复和并发更新下的契约。"""

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from app.memory.store import MemoryStore


@pytest.fixture
def clock():
    return [datetime(2026, 10, 5, tzinfo=timezone.utc)]


@pytest.fixture
def store(tmp_path, clock):
    return MemoryStore(tmp_path / "users", clock=lambda: clock[0])


def change(**overrides):
    values = dict(action="add", type="user", name="Language", description="Preferred language",
                  content="Use Chinese", memory_id=None)
    values.update(overrides)
    return values


def prepare(store, **overrides):
    return store.prepare("alice", [change(**overrides)], source_thread_id="thread", source_run_id="run")[0]


def test_preparation_is_read_only_and_json_round_trip_keeps_identity(store, tmp_path):
    operation = prepare(store)
    from app.memory.operations import PreparedMemoryOperation
    assert not (tmp_path / "users").exists()
    restored = PreparedMemoryOperation.from_dict(json.loads(json.dumps(operation.to_dict())))
    assert restored == operation
    assert restored.expected_version is None
    assert restored.memory_id == restored.record.id
    with pytest.raises(FrozenInstanceError):
        restored.memory_id = "different"


def test_body_saved_index_failure_recovery_keeps_id_and_timestamp(store, tmp_path, clock, monkeypatch):
    operation = prepare(store)
    original = store._atomic_write
    def fail_index(fd, name, data):
        if name == "MEMORY.md":
            raise OSError("index unavailable")
        return original(fd, name, data)
    monkeypatch.setattr(store, "_atomic_write", fail_index)
    with pytest.raises(OSError):
        store.apply_operation("alice", operation)
    clock[0] += timedelta(days=3)
    monkeypatch.setattr(store, "_atomic_write", original)
    recovered = store.apply_operation("alice", operation)
    assert recovered.updated_at == operation.record.updated_at
    assert recovered.is_stable(clock[0])
    assert store.apply_operation("alice", operation) == recovered
    assert len(list((tmp_path / "users/alice/memories").glob("user_*.md"))) == 1


def test_recovery_must_not_overwrite_a_later_direct_update(store):
    initial = store.apply_operation("alice", prepare(store))
    pending = prepare(store, action="update", memory_id=initial.id, content="Old pending content")
    newer = store.upsert("alice", kind="user", name=initial.name, description=initial.description,
                        content="Newer content", source_thread_id="other", source_run_id="other",
                        memory_id=initial.id)
    from app.memory.operations import MemoryConflictError
    with pytest.raises(MemoryConflictError):
        store.apply_operation("alice", pending)
    assert store.read("alice", initial.id) == newer


def test_expired_verification_does_not_create_or_repair_files(store, tmp_path, monkeypatch):
    operation = prepare(store)
    assert store.apply_operation("alice", operation, allow_write=False) is None
    assert not (tmp_path / "users").exists()
    store.apply_operation("alice", operation)
    index = tmp_path / "users/alice/memories/MEMORY.md"
    index.unlink()
    def forbidden(*args):
        pytest.fail("verification wrote a file")
    monkeypatch.setattr(store, "_atomic_write", forbidden)
    from app.memory.operations import MemoryVerificationError
    with pytest.raises(MemoryVerificationError):
        store.apply_operation("alice", operation, allow_write=False)
    assert not index.exists()


def test_expired_verification_accepts_complete_stable_index_without_refresh(store, clock, monkeypatch):
    operation = prepare(store)
    record = store.apply_operation("alice", operation)
    clock[0] += timedelta(days=3)
    def forbidden(*args):
        pytest.fail("verification refreshed index stability")
    monkeypatch.setattr(store, "_atomic_write", forbidden)
    assert store.apply_operation("alice", operation, allow_write=False) == record


def test_all_prepared_changes_validate_before_any_file_is_written(store, tmp_path):
    with pytest.raises(ValueError):
        store.prepare("alice", [change(), change(type="invalid")],
                      source_thread_id="thread", source_run_id="run")
    assert not (tmp_path / "users").exists()


def test_tampered_body_is_not_mistaken_for_previously_applied_operation(store, tmp_path):
    operation = prepare(store)
    store.apply_operation("alice", operation)
    body = tmp_path / "users/alice/memories" / f"{operation.memory_id}.md"
    body.write_text(body.read_text().replace("Use Chinese", "Use English"))
    from app.memory.operations import MemoryConflictError
    with pytest.raises(MemoryConflictError):
        store.apply_operation("alice", operation)
    assert "Use English" in body.read_text()


def test_corrupt_target_is_preserved_and_not_overwritten(store, tmp_path, caplog):
    operation = prepare(store)
    store.apply_operation("alice", operation)
    body = tmp_path / "users/alice/memories" / f"{operation.memory_id}.md"
    body.write_text("SECRET broken body")
    from app.memory.operations import MemoryVerificationError
    with pytest.raises(MemoryVerificationError):
        store.apply_operation("alice", operation)
    assert body.read_text() == "SECRET broken body"
    assert "SECRET" not in caplog.text
    assert "corrupt" in caplog.text


def test_model_preparation_reads_snapshot_without_repairing_index(store, tmp_path):
    from app.memory.extractor import MemoryExtractor
    from app.domain.messages import Message
    from app.domain.threads import ThreadState
    original = store.apply_operation("alice", prepare(store))
    index = tmp_path / "users/alice/memories/MEMORY.md"
    index.unlink()
    class Model:
        async def chat(self, **kwargs):
            return Message(role="assistant", content=json.dumps({"changes": [dict(
                change(action="update", memory_id=original.id, content="Use English"),
                evidence="Use English")]}))
        async def close(self):
            pass
    state = ThreadState(user_id="alice", thread_id="thread", workspace_path="/tmp/workspace",
                        messages=[Message(role="user", content="Use English")])
    operations = asyncio.run(MemoryExtractor(store, Model).prepare(state, "new-run"))
    assert len(operations) == 1
    assert not index.exists()
    assert store.read("alice", original.id) == original


def test_update_detects_change_while_model_was_running(store):
    from app.memory.extractor import MemoryExtractor
    from app.domain.messages import Message
    from app.domain.threads import ThreadState
    original = store.apply_operation("alice", prepare(store))
    class Model:
        async def chat(self, **kwargs):
            store.upsert("alice", kind="user", name=original.name, description=original.description,
                         content="Changed concurrently", source_thread_id="other", source_run_id="other",
                         memory_id=original.id)
            return Message(role="assistant", content=json.dumps({"changes": [dict(
                change(action="update", memory_id=original.id, content="Use English"),
                evidence="Use English")]}))
        async def close(self):
            pass
    state = ThreadState(user_id="alice", thread_id="thread", workspace_path="/tmp/workspace",
                        messages=[Message(role="user", content="Use English")])
    from app.memory.operations import MemoryConflictError
    with pytest.raises(MemoryConflictError):
        asyncio.run(MemoryExtractor(store, Model).prepare(state, "new-run"))
    assert store.read("alice", original.id).content == "Changed concurrently"


def test_verification_requires_successful_fsync_even_when_bytes_match(store, monkeypatch):
    import app.memory.store as module
    from app.storage.errors import StorageError
    operation = prepare(store)
    store.apply_operation("alice", operation)
    def fail_sync(fd, name):
        raise StorageError(backend="file", operation="verify", category="io", stage="file_sync",
                           commit_state="uncertain", recovery="verify")
    monkeypatch.setattr(module, "confirm_durable", fail_sync)
    with pytest.raises(StorageError) as error:
        store.apply_operation("alice", operation, allow_write=False)
    assert error.value.commit_state == "uncertain"


def test_legacy_body_without_operation_metadata_can_be_updated(store, tmp_path):
    operation = prepare(store)
    store.apply_operation("alice", operation)
    body = tmp_path / "users/alice/memories" / f"{operation.memory_id}.md"
    body.write_text("\n".join(line for line in body.read_text().split("\n")
                              if not line.startswith(("operation_id:", "result_digest:", "version:"))))
    legacy = store.read("alice", operation.memory_id)
    assert legacy.operation_id is None and legacy.version is None
    update = prepare(store, action="update", memory_id=legacy.id, content="Use English")
    assert update.expected_version
    updated = store.apply_operation("alice", update)
    assert updated.id == legacy.id and updated.created_at == legacy.created_at
    assert updated.operation_id == update.operation_id


def test_overlapping_updates_are_rejected_without_a_partial_preparation(store):
    record = store.apply_operation("alice", prepare(store))
    update = change(action="update", memory_id=record.id, content="Use English")
    with pytest.raises(ValueError):
        store.prepare("alice", [update, update], source_thread_id="thread", source_run_id="run")
    assert store.read("alice", record.id) == record


def test_directory_permission_failure_uses_safe_storage_classification(store, monkeypatch):
    import errno
    import os
    from app.storage.errors import StorageError
    original = os.open
    def denied(path, flags, *args, **kwargs):
        if path == "alice":
            raise PermissionError(errno.EACCES, "SECRET path must not enter logs")
        return original(path, flags, *args, **kwargs)
    operation = prepare(store)
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(StorageError) as error:
        store.apply_operation("alice", operation)
    assert error.value.category == "permission"
    assert error.value.stage == "open"
    assert "SECRET" not in str(error.value)
    assert isinstance(error.value.__cause__, PermissionError)


def test_created_parent_directory_is_synced_before_any_body_write(store, monkeypatch):
    import os
    import stat
    from app.storage.errors import StorageError
    operation = prepare(store)
    original = os.fsync
    def fail_new_parent(fd):
        # 当 users 已创建且 alice 尚未创建时，父目录同步必须在正文之前。
        if stat.S_ISDIR(os.fstat(fd).st_mode) and "users" in os.listdir(fd):
            raise OSError("parent directory sync failure")
        return original(fd)
    monkeypatch.setattr(os, "fsync", fail_new_parent)
    with pytest.raises(StorageError) as error:
        store.apply_operation("alice", operation)
    assert error.value.stage == "directory_sync"
    assert not list(store._root().glob("alice/memories/*.md"))


def test_healthy_operation_can_be_verified_with_an_unrelated_corrupt_body(store, tmp_path):
    operation = prepare(store)
    record = store.apply_operation("alice", operation)
    corrupt = tmp_path / "users/alice/memories" / ("user_" + "f" * 32 + ".md")
    corrupt.write_text("broken unrelated body")
    assert store.list("alice") == [record]
    assert store.apply_operation("alice", operation, allow_write=False) == record
    assert corrupt.read_text() == "broken unrelated body"


def test_directory_listing_io_failure_is_classified_instead_of_missing(store, monkeypatch):
    import errno
    import os
    from app.storage.errors import StorageError
    store.apply_operation("alice", prepare(store))
    def failed_listing(fd):
        raise OSError(errno.EIO, "SECRET read failure")
    monkeypatch.setattr(os, "listdir", failed_listing)
    with pytest.raises(StorageError) as error:
        store.snapshot("alice")
    assert error.value.category == "io"
    assert error.value.operation == "read"
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("updating", [False, True])
def test_delayed_first_save_timestamps_actual_content_change(store, clock, updating):
    original = store.apply_operation("alice", prepare(store)) if updating else None
    operation = prepare(store, **({"action": "update", "memory_id": original.id,
                                 "content": "Use English"} if original else {}))
    clock[0] += timedelta(hours=50)
    saved = store.apply_operation("alice", operation)
    assert saved.updated_at == clock[0]
    assert saved.created_at == (original.created_at if original else clock[0])
    assert not saved.is_stable(clock[0])
    clock[0] += timedelta(hours=50)
    assert store.apply_operation("alice", operation) == saved
    assert store.apply_operation("alice", operation, allow_write=False) == saved
    assert saved.is_stable(clock[0])


def test_catalog_stat_failure_preserves_storage_error_classification(store, monkeypatch):
    import errno
    import os
    from app.storage.errors import StorageError
    store.apply_operation("alice", prepare(store))
    original = os.stat
    def failed_stat(path, *args, **kwargs):
        if path == "MEMORY.md":
            raise PermissionError(errno.EACCES, "SECRET metadata failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", failed_stat)
    with pytest.raises(StorageError) as error:
        store.catalog("alice")
    assert error.value.category == "permission"
    assert error.value.operation == "read"
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("critical", [False, True])
def test_directory_cleanup_preserves_control_errors_and_closes_all_descriptors(store, monkeypatch, critical):
    import os
    from app.runtime.errors import StatePersistenceError
    store.apply_operation("alice", prepare(store))
    original = StatePersistenceError("critical") if critical else asyncio.CancelledError("cancelled")
    real_open, real_close = os.open, os.close
    opened, closed = [], []
    def track_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor
    def failed_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)
        if len(closed) == 1:
            raise OSError("directory close failed")
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", track_open)
        patch.setattr(os, "close", failed_close)
        with pytest.raises(type(original)) as caught:
            with store._directory(store._root(), "alice", create=False):
                raise original
    assert caught.value is original
    assert sorted(opened) == sorted(closed)


@pytest.mark.parametrize("body_committed", [False, True])
def test_storage_failure_distinguishes_unwritten_body_from_partial_commit(store, monkeypatch, body_committed):
    from app.memory.operations import MemoryVerificationError
    from app.storage.errors import StorageError
    operation = prepare(store)
    original = store._atomic_write
    failure = StorageError(backend="file", operation="write", category="no_space", stage="replace",
                           commit_state="not_committed", recovery="wait")
    def fail_at_stage(fd, name, data):
        if (name == "MEMORY.md") == body_committed:
            raise failure
        return original(fd, name, data)
    monkeypatch.setattr(store, "_atomic_write", fail_at_stage)
    with pytest.raises(StorageError) as caught:
        store.apply_operation("alice", operation)
    if body_committed:
        assert isinstance(caught.value, MemoryVerificationError)
        assert caught.value.storage_error is failure
        assert caught.value.__cause__ is failure
        assert caught.value.commit_state == "uncertain"
        assert caught.value.category == "no_space"
        assert caught.value.stage == "replace"
    else:
        assert caught.value is failure
        assert caught.value.commit_state == "not_committed"
        assert store.read("alice", operation.memory_id) is None


def test_prepared_add_cannot_duplicate_content_saved_by_a_later_operation(store):
    from app.memory.operations import MemoryConflictError
    first = prepare(store)
    later = prepare(store)
    assert first.operation_id != later.operation_id
    assert first.memory_id != later.memory_id
    saved = store.apply_operation("alice", later)
    with pytest.raises(MemoryConflictError):
        store.apply_operation("alice", first)
    assert store.list("alice") == [saved]
