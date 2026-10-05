import copy
import hashlib
import os
import stat
from pathlib import Path

import pytest

from app.filesystem.file_snapshots import FileSnapshotStore, fingerprint, manifest_bytes
from app.filesystem.thread_paths import ThreadPaths


@pytest.fixture
def paths(tmp_path: Path) -> ThreadPaths:
    thread = tmp_path / "thread"
    for name in ("uploads", "workspace", "outputs"):
        (thread / name).mkdir(parents=True)
    return ThreadPaths(thread)


def test_capture_and_materialize_binary_empty_directories_and_modes(paths, tmp_path):
    data = b"\x00\xff\x80binary\r\n"
    (paths.uploads_path / "image.bin").write_bytes(data)
    (paths.workspace_path / "empty").mkdir(mode=0o751)
    (paths.outputs_path / "script").write_bytes(b"hello")
    (paths.outputs_path / "script").chmod(0o6751)
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    assert manifest["schema_version"] == 1
    assert [e["path"] for e in manifest["entries"]] == sorted(
        e["path"] for e in manifest["entries"]
    )
    assert manifest_bytes(manifest) == manifest_bytes(copy.deepcopy(manifest))
    assert fingerprint(manifest) == hashlib.sha256(manifest_bytes(manifest)).hexdigest()
    destination = tmp_path / "restored"
    store.materialize(manifest, destination)
    assert (destination / "uploads/image.bin").read_bytes() == data
    assert list((destination / "workspace/empty").iterdir()) == []
    assert stat.S_IMODE((destination / "workspace/empty").stat().st_mode) == 0o751
    assert stat.S_IMODE((destination / "outputs/script").stat().st_mode) == 0o751
    digest = hashlib.sha256(data).hexdigest()
    assert store.object_path(digest).read_bytes() == data
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.object_path(digest).stat().st_mode) == 0o600
    assert store.object_path(digest).stat().st_ino != (destination / "uploads/image.bin").stat().st_ino


def test_dedup_collect_preserves_remaining_points_and_live_files(paths):
    a = paths.workspace_path / "a"
    b = paths.outputs_path / "b"
    a.write_bytes(b"shared")
    b.write_bytes(b"shared")
    store = FileSnapshotStore(paths)
    first = store.capture()
    assert len(list((store.root / "objects").glob("??/*"))) == 1
    a.write_bytes(b"changed")
    b.unlink()
    second = store.capture()
    store.collect([first, second])
    store.validate(first)
    store.validate(second)
    store.collect([second])
    assert len(list((store.root / "objects").glob("??/*"))) == 1
    assert a.read_bytes() == b"changed"
    assert not b.exists()
    store.validate(second)
    with pytest.raises((ValueError, FileNotFoundError)):
        store.validate(first)


def test_scan_excludes_only_reserved_tool_history_and_preview_has_no_writes(paths):
    history = paths.workspace_path / ".tool-results"
    history.mkdir()
    (history / "old-result").write_bytes(b"history")
    (paths.workspace_path / ".venv").mkdir()
    (paths.workspace_path / ".venv/file").write_bytes(b"ordinary")
    store = FileSnapshotStore(paths)
    preview = store.capture(store_objects=False)
    assert not store.root.exists()
    assert "workspace/.venv/file" in {e["path"] for e in preview["entries"]}
    assert not any(".tool-results" in e["path"] for e in preview["entries"])
    assert store.capture() == preview


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "root_symlink"])
def test_capture_rejects_links_and_nonregular_files(paths, tmp_path, kind):
    source = tmp_path / "outside"
    source.write_bytes(b"outside")
    target = paths.workspace_path / "bad"
    if kind == "symlink":
        target.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        paths.workspace_path.rmdir()
        paths.workspace_path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        FileSnapshotStore(paths).capture()
    assert source.read_bytes() == b"outside"


@pytest.mark.parametrize("bad_path", ["/workspace/a", "workspace/../a", "workspace//a", "workspace/./a", "workspace/a/", "workspace\\a", "workspace/\x00a", "workspace/.checkpoints/a", "workspace/.tool-results", "workspace/.tool-results/a", "unknown/a"])
def test_validate_rejects_unsafe_manifest_paths(paths, bad_path):
    store = FileSnapshotStore(paths)
    manifest = store.capture(store_objects=False)
    manifest["entries"].append({"path": bad_path, "kind": "directory", "mode": 0o700})
    manifest["entries"].sort(key=lambda entry: entry["path"])
    with pytest.raises(ValueError):
        store.validate(manifest, verify_objects=False)


@pytest.mark.parametrize("change", ["duplicate", "missing_parent", "root_file", "missing_root", "unsorted", "negative_mode", "special_mode", "bool_size", "bad_digest", "extra_field", "wrong_version"])
def test_validate_rejects_malformed_manifest(paths, change):
    (paths.workspace_path / "a").write_bytes(b"a")
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    entry = next(e for e in manifest["entries"] if e["kind"] == "file")
    if change == "duplicate":
        manifest["entries"].append(copy.deepcopy(entry))
    elif change == "missing_parent":
        entry["path"] = "workspace/missing/a"
    elif change == "root_file":
        manifest["entries"] = [e for e in manifest["entries"] if e["path"] != "workspace"]
        entry["path"] = "workspace"
    elif change == "missing_root":
        manifest["entries"] = [e for e in manifest["entries"] if e["path"] != "uploads"]
    elif change == "unsorted":
        manifest["entries"].reverse()
    elif change == "negative_mode":
        entry["mode"] = -1
    elif change == "special_mode":
        entry["mode"] = 0o4755
    elif change == "bool_size":
        entry["size"] = True
    elif change == "bad_digest":
        entry["sha256"] = "../escape"
    elif change == "extra_field":
        entry["unexpected"] = 1
    else:
        manifest["schema_version"] = True
    with pytest.raises(ValueError):
        store.validate(manifest)


def test_tampered_object_is_rejected_before_materializing(paths, tmp_path):
    (paths.workspace_path / "a").write_bytes(b"original")
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    digest = next(e["sha256"] for e in manifest["entries"] if e["kind"] == "file")
    store.object_path(digest).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        store.validate(manifest)
    destination = tmp_path / "restore"
    with pytest.raises(ValueError):
        store.materialize(manifest, destination)
    assert not destination.exists()
    with pytest.raises(ValueError):
        store.capture()


@pytest.mark.parametrize("limit", ["entries", "manifest", "bytes"])
def test_capture_enforces_capacity(paths, limit):
    (paths.workspace_path / "a").write_bytes(b"data")
    options = {"entries": {"max_entries": 3}, "manifest": {"max_manifest_bytes": 16}, "bytes": {"max_bytes": 3}}
    with pytest.raises(ValueError):
        FileSnapshotStore(paths, **options[limit]).capture()


def test_materialize_rejects_existing_nonempty_or_linked_destination(paths, tmp_path):
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "keep").write_bytes(b"keep")
    with pytest.raises(ValueError):
        store.materialize(manifest, destination)
    assert (destination / "keep").read_bytes() == b"keep"
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        store.materialize(manifest, link)


def test_collect_refuses_unsafe_object_tree_without_touching_user_files(paths, tmp_path):
    store = FileSnapshotStore(paths)
    store.capture()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    (store.root / "objects" / "aa").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        store.collect([])
    assert (outside / "keep").read_bytes() == b"keep"


def test_capture_detects_same_size_rewrite_even_when_mtime_is_restored(paths, monkeypatch):
    from app.filesystem import file_snapshots

    source = paths.workspace_path / "a"
    source.write_bytes(b"original")
    original = source.stat()
    real_read = file_snapshots.os.read
    changed = False

    def racing_read(descriptor, size):
        nonlocal changed
        result = real_read(descriptor, size)
        if result and not changed:
            changed = True
            source.write_bytes(b"modified")
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        return result

    monkeypatch.setattr(file_snapshots.os, "read", racing_read)
    with pytest.raises(ValueError, match="变化"):
        FileSnapshotStore(paths).capture()
    assert not list((paths.thread_dir / ".checkpoints/objects").glob(".snapshot-*.tmp"))


def test_capture_final_tree_check_detects_late_change_to_previously_read_file(paths, monkeypatch):
    from app.filesystem import file_snapshots

    first = paths.uploads_path / "first"
    first.write_bytes(b"first")
    (paths.workspace_path / "last").write_bytes(b"last")
    real_read = file_snapshots.os.read

    def racing_read(descriptor, size):
        result = real_read(descriptor, size)
        if result == b"last":
            first.write_bytes(b"later")
        return result

    monkeypatch.setattr(file_snapshots.os, "read", racing_read)
    with pytest.raises(ValueError, match="变化"):
        FileSnapshotStore(paths).capture()


def test_no_follow_open_rejects_source_replaced_by_link_after_inspection(paths, tmp_path, monkeypatch):
    from app.filesystem import file_snapshots

    source = paths.workspace_path / "victim"
    source.write_bytes(b"original")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    real_open = file_snapshots.os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "victim" and not replaced:
            replaced = True
            source.unlink()
            source.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(file_snapshots.os, "open", racing_open)
    with pytest.raises((ValueError, OSError)):
        FileSnapshotStore(paths).capture()
    assert outside.read_bytes() == b"private"


def test_quota_counts_unique_contents_in_candidate(paths):
    data = b"a" * 2048
    (paths.workspace_path / "a").write_bytes(data)
    (paths.workspace_path / "b").write_bytes(data)
    preview = FileSnapshotStore(paths).capture(store_objects=False)
    limit = len(data) + len(manifest_bytes(preview))
    store = FileSnapshotStore(paths, max_bytes=limit)
    first = store.capture()
    assert store.capture() == first
    (paths.workspace_path / "a").write_bytes(b"b" * 2048)
    with pytest.raises(ValueError, match="容量"):
        store.capture()
    store.validate(first)
    store.collect([first])
    assert len(list((store.root / "objects").glob("??/*"))) == 1


def test_capture_can_replace_contents_before_service_selects_old_points_to_evict(paths):
    source = paths.workspace_path / "a"
    source.write_bytes(b"old" * 1024)
    preview = FileSnapshotStore(paths).capture(store_objects=False)
    limit = source.stat().st_size + len(manifest_bytes(preview))
    store = FileSnapshotStore(paths, max_bytes=limit)
    first = store.capture()
    source.write_bytes(b"new" * 1024)
    second = store.capture()
    assert first != second
    store.validate(first)
    store.validate(second)
    assert len(list((store.root / "objects").glob("??/*"))) == 2


def test_manifest_size_limit_is_exact_and_validated_for_untrusted_manifests(paths):
    manifest = FileSnapshotStore(paths).capture(store_objects=False)
    size = len(manifest_bytes(manifest))
    assert FileSnapshotStore(paths, max_manifest_bytes=size).capture() == manifest
    with pytest.raises(ValueError):
        FileSnapshotStore(paths, max_manifest_bytes=size - 1).validate(manifest, verify_objects=False)


@pytest.mark.parametrize("entry", [".checkpoints", "bad\\name"])
def test_capture_does_not_silently_skip_unrepresentable_entries(paths, entry):
    (paths.workspace_path / entry).mkdir()
    with pytest.raises(ValueError):
        FileSnapshotStore(paths).capture()


@pytest.mark.parametrize("kind", ["file", "link"])
def test_capture_rejects_unsafe_history_directory_entry(paths, tmp_path, kind):
    entry = paths.workspace_path / ".tool-results"
    if kind == "file":
        entry.write_bytes(b"wrong type")
    else:
        entry.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        FileSnapshotStore(paths).capture()


@pytest.mark.parametrize("location", ["root", "objects", "prefix", "object"])
def test_capture_rejects_links_in_own_backup_paths(paths, tmp_path, location):
    (paths.workspace_path / "a").write_bytes(b"content")
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    digest = next(e["sha256"] for e in manifest["entries"] if e["kind"] == "file")
    targets = {"root": store.root, "objects": store.root / "objects", "prefix": store.object_path(digest).parent, "object": store.object_path(digest)}
    target = targets[location]
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=location != "object")
    with pytest.raises((ValueError, OSError)):
        store.capture()


def test_collect_removes_only_snapshot_temps_and_unreferenced_objects(paths):
    (paths.workspace_path / "a").write_bytes(b"live")
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    temporary = store.root / "objects" / (".snapshot-" + "0" * 32 + ".tmp")
    temporary.write_bytes(b"interrupted write")
    temporary.chmod(0o600)
    staging = store.root / "staging" / "active-operation"
    staging.mkdir(parents=True)
    (staging / "keep").write_bytes(b"recovery")
    store.collect([manifest])
    assert not temporary.exists()
    assert (staging / "keep").read_bytes() == b"recovery"
    store.collect([])
    assert not list((store.root / "objects").glob("??/*"))
    assert (paths.workspace_path / "a").read_bytes() == b"live"


def test_fsync_failure_never_installs_object_or_leaks_temporary(paths, monkeypatch):
    from app.filesystem import file_snapshots

    (paths.workspace_path / "a").write_bytes(b"new object")
    real_fsync = file_snapshots.os.fsync

    def failing_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("simulated disk failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(file_snapshots.os, "fsync", failing_fsync)
    store = FileSnapshotStore(paths)
    with pytest.raises(OSError, match="simulated disk failure"):
        store.capture()
    assert not list((store.root / "objects").glob("??/*"))
    assert not list((store.root / "objects").glob(".snapshot-*.tmp"))


def test_materialize_rechecks_objects_after_initial_validation(paths, tmp_path, monkeypatch):
    (paths.workspace_path / "a").write_bytes(b"original")
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    digest = next(e["sha256"] for e in manifest["entries"] if e["kind"] == "file")
    original_validate = store.validate

    def validate_then_tamper(value, **kwargs):
        original_validate(value, **kwargs)
        store.object_path(digest).write_bytes(b"tampered")

    monkeypatch.setattr(store, "validate", validate_then_tamper)
    with pytest.raises(ValueError):
        store.materialize(manifest, tmp_path / "restore")
    assert (paths.workspace_path / "a").read_bytes() == b"original"


def test_materialize_rejects_symlink_in_destination_ancestor(paths, tmp_path):
    store = FileSnapshotStore(paths)
    manifest = store.capture()
    real = tmp_path / "outside"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        store.materialize(manifest, linked / "restore")
    assert list(real.iterdir()) == []


def test_capture_entry_limit_is_checked_before_reading_excess_files(paths, monkeypatch):
    from app.filesystem import file_snapshots

    (paths.workspace_path / "a").write_bytes(b"must not read")
    reads = []
    real_read = file_snapshots.os.read

    def counting_read(descriptor, size):
        reads.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(file_snapshots.os, "read", counting_read)
    with pytest.raises(ValueError):
        FileSnapshotStore(paths, max_entries=3).capture()
    assert reads == []


def test_capture_preflights_temporary_disk_space(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.filesystem.file_snapshots import FileSnapshotStore
    from app.filesystem.thread_paths import ThreadPaths
    import app.filesystem.file_snapshots as module
    for area in ('workspace','uploads','outputs'):
        (tmp_path/area).mkdir()
    (tmp_path/'workspace'/'file').write_bytes(b'bytes')
    monkeypatch.setattr(module.os,'fstatvfs',lambda descriptor: SimpleNamespace(f_bavail=0,f_frsize=4096))
    with pytest.raises(OSError,match='空间'):
        FileSnapshotStore(ThreadPaths(tmp_path)).capture()
