from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memory.store import (
    MAX_CONTENT_BYTES,
    MAX_DESCRIPTION_CHARS,
    MAX_MEMORY_FILE_BYTES,
    MAX_NAME_CHARS,
    MemoryStore,
)


@pytest.fixture
def clock():
    current = [datetime(2026, 10, 4, tzinfo=timezone.utc)]
    return current


@pytest.fixture
def store(tmp_path, clock):
    return MemoryStore(tmp_path / "users", clock=lambda: clock[0])


def save(store, user="alice", **changes):
    fields = dict(kind="user", name="Writing", description="Preferred language",
                  content="Use Chinese.\nPreserve **formatting**.\n", source_thread_id="thread_a",
                  source_run_id="run_a")
    fields.update(changes)
    return store.upsert(user, **fields)


def test_round_trip_metadata_markdown_and_immutable_record(store, tmp_path, clock):
    record = save(store)
    assert record.type == "user"
    assert record.id.startswith("user_")
    assert record.created_at == record.updated_at == clock[0]
    assert store.read("alice", record.id) == record
    assert store.list("alice") == [record]
    body = (tmp_path / "users/alice/memories" / f"{record.id}.md").read_text()
    assert body.startswith("---\n")
    assert record.content in body
    index = (tmp_path / "users/alice/memories/MEMORY.md").read_text()
    for value in (record.id, record.name, record.description, record.source_thread_id,
                  record.source_run_id, record.updated_at.isoformat()):
        assert value in index
    assert '"stable": false' in index
    assert record.content not in index
    with pytest.raises(FrozenInstanceError):
        record.name = "change"


def test_empty_reads_do_not_create_directories(store, tmp_path):
    assert store.list("alice") == []
    assert store.read("alice", "user_" + "a" * 32) is None
    assert not (tmp_path / "users").exists()


def test_same_id_is_private_to_each_user(store):
    record = save(store)
    assert store.read("bob", record.id) is None
    assert store.list("bob") == []
    other = save(store, "bob")
    assert other.id != record.id
    assert store.list("alice") == [record]


@pytest.mark.parametrize("value", ["", ".", "..", "../bob", "/bob", "a/b", "a\\b", "a\x00b", "a\nb", "a" * 201])
def test_illegal_user_paths_rejected(store, value):
    with pytest.raises(ValueError):
        store.list(value)
    with pytest.raises(ValueError):
        save(store, value)


@pytest.mark.parametrize("value", ["", "../MEMORY", "/tmp/x", "user_a/b", "user_a\\b", "MEMORY", "user_" + "a" * 33])
def test_illegal_memory_ids_rejected(store, value):
    with pytest.raises(ValueError):
        store.read("alice", value)
    with pytest.raises(ValueError):
        save(store, memory_id=value)


def test_all_supported_types_and_deterministic_newest_sort(store, clock):
    first = save(store, kind="feedback")
    second = save(store, kind="reference")
    assert [item.id for item in store.list("alice")] == sorted([first.id, second.id])
    clock[0] += timedelta(seconds=1)
    third = save(store)
    assert store.list("alice")[0] == third


def test_stability_exactly_48_hours_and_noop_preserves_age(store, clock, tmp_path):
    record = save(store)
    clock[0] += timedelta(hours=48) - timedelta(microseconds=1)
    assert not record.is_stable(clock[0])
    clock[0] += timedelta(microseconds=1)
    assert record.is_stable(clock[0])
    assert save(store, source_run_id="run_new") == record
    assert save(store, memory_id=record.id, source_run_id="run_new") == record
    assert len(store.list("alice")) == 1
    assert '"stable": true' in (tmp_path / "users/alice/memories/MEMORY.md").read_text()


@pytest.mark.parametrize("changes", [{"content": "Use English"}, {"name": "Tone"}, {"description": "New description"}])
def test_actual_update_keeps_id_and_resets_stability(store, clock, changes):
    record = save(store)
    clock[0] += timedelta(hours=50)
    updated = save(store, memory_id=record.id, **changes)
    assert updated.id == record.id
    assert updated.created_at == record.created_at
    assert updated.updated_at == clock[0]
    assert not updated.is_stable(clock[0])
    assert len(store.list("alice")) == 1


def test_unsupported_kind_unknown_target_and_kind_change_rejected(store):
    with pytest.raises(ValueError):
        save(store, kind="system")
    with pytest.raises(ValueError):
        save(store, memory_id="user_" + "a" * 32)
    record = save(store)
    with pytest.raises(ValueError):
        save(store, kind="feedback", memory_id=record.id)
    assert store.read("alice", record.id) == record


@pytest.mark.parametrize("changes", [{"content": "中" * (MAX_CONTENT_BYTES // 3 + 1)},
                                   {"name": "x" * (MAX_NAME_CHARS + 1)},
                                   {"description": "x" * (MAX_DESCRIPTION_CHARS + 1)},
                                   {"source_run_id": "x" * 201}])
def test_input_size_limits_reject_before_writing(store, tmp_path, changes):
    with pytest.raises(ValueError):
        save(store, **changes)
    assert not (tmp_path / "users").exists()


@pytest.mark.parametrize("target", ["root", "user", "memories"])
def test_directory_symlinks_refused(store, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "users"
    link = {"root": root, "user": root / "alice", "memories": root / "alice/memories"}[target]
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.list("alice")
    with pytest.raises(ValueError):
        save(store)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("target", ["body", "index"])
def test_file_symlinks_refused_without_reading_or_replacing_target(store, tmp_path, target):
    record = save(store)
    directory = tmp_path / "users/alice/memories"
    file = directory / (f"{record.id}.md" if target == "body" else "MEMORY.md")
    file.unlink()
    outside = tmp_path / "private"
    outside.write_text("private secret")
    file.symlink_to(outside)
    with pytest.raises(ValueError):
        store.list("alice")
    with pytest.raises(ValueError):
        save(store, memory_id=record.id, content="Changed")
    assert outside.read_text() == "private secret"
    assert file.is_symlink()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "oversized", "deeply_nested"])
def test_index_rebuilt_from_valid_bodies(store, tmp_path, damage):
    record = save(store)
    index = tmp_path / "users/alice/memories/MEMORY.md"
    if damage == "missing":
        index.unlink()
    else:
        index.write_text("bad" if damage == "corrupt" else
                         "# Memory index\n\n```json\n" + "[" * 20000 + "]" * 20000 + "\n```\n"
                         if damage == "deeply_nested" else "x" * (4 * 1024 * 1024 + 1))
    assert store.list("alice") == [record]
    assert record.id in index.read_text()


def test_corrupt_and_oversized_bodies_ignored_without_deletion(store, tmp_path):
    record = save(store)
    directory = tmp_path / "users/alice/memories"
    bad = directory / ("user_" + "b" * 32 + ".md")
    huge = directory / ("user_" + "c" * 32 + ".md")
    bad.write_text("---\ninvalid: secret\n---\nprivate")
    huge.write_text("x" * (MAX_MEMORY_FILE_BYTES + 1))
    assert store.list("alice") == [record]
    assert store.read("alice", bad.stem) is None
    assert store.read("alice", huge.stem) is None
    assert bad.exists() and huge.exists()


def test_default_root_resolved_lazily_with_current_environment(tmp_path, monkeypatch):
    store = MemoryStore()
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "first"))
    one = save(store)
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "second"))
    assert store.list("alice") == []
    two = save(store)
    assert (tmp_path / "first/alice/memories" / f"{one.id}.md").exists()
    assert (tmp_path / "second/alice/memories" / f"{two.id}.md").exists()


def test_multiple_instances_serialize_dedup_and_index_writes(store, tmp_path, clock):
    stores = [MemoryStore(tmp_path / "users", clock=lambda: clock[0]) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, stores))
    assert len({item.id for item in results}) == 1
    assert store.list("alice") == [results[0]]
    assert not list((tmp_path / "users/alice/memories").glob("*.tmp"))


def test_clock_regression_rejected_without_corrupting_existing_record(store, clock):
    record = save(store)
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(ValueError):
        save(store, memory_id=record.id, content="Changed")
    assert store.read("alice", record.id) == record


def test_hardlinked_body_refused_without_exposing_foreign_file(store, tmp_path):
    record = save(store)
    body = tmp_path / "users/alice/memories" / f"{record.id}.md"
    alias = tmp_path / "foreign_body.md"
    alias.hardlink_to(body)
    with pytest.raises(ValueError):
        store.read("alice", record.id)
    assert alias.exists() and body.exists()


def test_root_ancestor_symlink_refused(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ancestor = tmp_path / "redirect"
    ancestor.symlink_to(outside, target_is_directory=True)
    redirected = MemoryStore(ancestor / "users")
    with pytest.raises(ValueError):
        save(redirected)
    assert list(outside.iterdir()) == []


def test_failed_index_replace_reports_failure_and_body_repairs_on_next_read(store, tmp_path, monkeypatch):
    import os

    original = os.replace

    def fail_index(source, destination, **kwargs):
        if destination == "MEMORY.md":
            raise OSError("simulated disk error")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_index)
    with pytest.raises(OSError, match="simulated disk error"):
        save(store)
    directory = tmp_path / "users/alice/memories"
    assert len(list(directory.glob("user_*.md"))) == 1
    assert not list(directory.glob("*.tmp"))
    monkeypatch.setattr(os, "replace", original)
    records = store.list("alice")
    assert len(records) == 1
    assert records[0].id in (directory / "MEMORY.md").read_text()


def test_failed_body_replace_keeps_original_record(store, monkeypatch):
    import os

    record = save(store)

    def fail_replace(*args, **kwargs):
        raise OSError("simulated disk error")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk error"):
        save(store, memory_id=record.id, content="Changed")
    assert store.read("alice", record.id) == record


def test_healthy_catalog_reads_metadata_without_loading_bodies(store, monkeypatch, clock):
    record = save(store)
    clock[0] += timedelta(hours=48)
    original = store._read_bytes
    opened = []

    def read_bytes(fd, name, limit):
        opened.append(name)
        assert name == "MEMORY.md"
        return original(fd, name, limit)

    monkeypatch.setattr(store, "_read_bytes", read_bytes)
    metadata = store.catalog("alice")
    assert len(metadata) == 1
    assert metadata[0].id == record.id
    assert metadata[0].name == record.name
    assert metadata[0].source_run_id == record.source_run_id
    assert metadata[0].is_stable(clock[0])
    assert not hasattr(metadata[0], "content")
    assert opened and set(opened) == {"MEMORY.md"}


def test_direct_read_opens_only_requested_body(store, monkeypatch):
    record = save(store)
    unrelated = save(store, name="Other", content="Different memory")
    original = store._read_bytes
    opened = []

    def read_bytes(fd, name, limit):
        opened.append(name)
        assert name != f"{unrelated.id}.md"
        return original(fd, name, limit)

    monkeypatch.setattr(store, "_read_bytes", read_bytes)
    assert store.read("alice", record.id) == record
    assert f"{record.id}.md" in opened


@pytest.mark.parametrize("damage", ["missing", "corrupt", "deeply_nested", "changed_body", "removed_body"])
def test_catalog_repairs_missing_corrupt_or_stale_index(store, tmp_path, damage):
    import os

    record = save(store)
    directory = tmp_path / "users/alice/memories"
    index = directory / "MEMORY.md"
    body = directory / f"{record.id}.md"
    if damage == "missing":
        index.unlink()
    elif damage == "corrupt":
        index.write_text("corrupt")
    elif damage == "deeply_nested":
        index.write_text("# Memory index\n\n```json\n" + "[" * 20000 + "]" * 20000 + "\n```\n")
    elif damage == "changed_body":
        modified = index.stat().st_mtime_ns + 1_000_000
        body.write_text(body.read_text().replace('name: "Writing"', 'name: "Tone"'))
        os.utime(body, ns=(modified, modified))
    else:
        body.unlink()
    metadata = store.catalog("alice")
    assert len(metadata) == (0 if damage == "removed_body" else 1)
    if metadata:
        assert metadata[0].name == ("Tone" if damage == "changed_body" else "Writing")
    assert index.exists()


def test_empty_catalog_does_not_create_directory(store, tmp_path):
    assert store.catalog("alice") == []
    assert not (tmp_path / "users").exists()


def test_index_budget_rejection_does_not_write_a_body(store, tmp_path, monkeypatch):
    import app.memory.store as store_module

    monkeypatch.setattr(store_module, "MAX_INDEX_BYTES", 100)
    with pytest.raises(ValueError):
        save(store)
    assert not list((tmp_path / "users/alice/memories").glob("user_*.md"))


def test_deeply_nested_frontmatter_ignored_without_disrupting_valid_body(store, tmp_path):
    record = save(store)
    body = tmp_path / "users/alice/memories" / ("user_" + "d" * 32 + ".md")
    body.write_text("---\nid: " + "[" * 20000 + "]" * 20000 + "\n---\ncontent")
    assert store.list("alice") == [record]
    assert store.read("alice", body.stem) is None
    assert body.exists()


def test_memory_count_limit_refuses_new_records_but_allows_updates(store, monkeypatch):
    import app.memory.store as store_module

    monkeypatch.setattr(store_module, "MAX_MEMORIES_PER_USER", 1)
    record = save(store)
    with pytest.raises(ValueError):
        save(store, name="Second", content="Another memory")
    updated = save(store, memory_id=record.id, content="Updated memory")
    assert store.list("alice") == [updated]
