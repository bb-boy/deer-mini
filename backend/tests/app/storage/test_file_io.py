import asyncio
import errno
import os
import stat

import pytest

from app.runtime.errors import StatePersistenceError


@pytest.fixture
def directory(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def test_read_is_bounded_and_missing_is_distinct(tmp_path, directory):
    from app.storage.file_io import bounded_read
    from app.storage.errors import StorageLimitError
    assert bounded_read(directory, "missing", 3) is None
    (tmp_path / "data").write_bytes(b"1234")
    with pytest.raises(StorageLimitError):
        bounded_read(directory, "data", 3)
    assert bounded_read(directory, "data", 4) == b"1234"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_read_and_replace_reject_unsafe_existing_files(tmp_path, directory, kind):
    from app.storage.file_io import bounded_read, atomic_write
    from app.storage.errors import UnsafeStoragePathError
    original = tmp_path / "original"
    original.write_bytes(b"private")
    target = tmp_path / "target"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    for operation in (lambda: bounded_read(directory, "target", 20),
                      lambda: atomic_write(directory, "target", b"new")):
        with pytest.raises(UnsafeStoragePathError):
            operation()
    assert original.read_bytes() == b"private"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "nested/name", "a\\b", "", ".", "..", "a\x00b"])
def test_names_cannot_escape_directory(directory, name):
    from app.storage.file_io import atomic_write
    from app.storage.errors import UnsafeStoragePathError
    with pytest.raises(UnsafeStoragePathError):
        atomic_write(directory, name, b"x")


@pytest.mark.parametrize("failure_stage", ["write", "file_sync", "replace", "directory_sync"])
def test_atomic_failure_reports_commit_boundary_and_cleans_staging(tmp_path, directory, monkeypatch, failure_stage):
    from app.storage import file_io
    from app.storage.errors import StorageError
    (tmp_path / "target").write_bytes(b"old")
    original_names = sorted(path.name for path in tmp_path.iterdir())
    original_fsync = os.fsync
    source = OSError(errno.ENOSPC, "private details")

    def fail(*args, **kwargs):
        raise source

    def sync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_directory == (failure_stage == "directory_sync"):
            raise source
        original_fsync(fd)

    if failure_stage == "write":
        monkeypatch.setattr(file_io.os, "write", fail)
    elif failure_stage == "replace":
        monkeypatch.setattr(file_io.os, "replace", fail)
    else:
        monkeypatch.setattr(file_io.os, "fsync", sync)
    with pytest.raises(StorageError) as caught:
        file_io.atomic_write(directory, "target", b"new")
    assert caught.value.stage == failure_stage
    assert caught.value.__cause__ is source
    assert caught.value.commit_state == ("uncertain" if failure_stage == "directory_sync" else "not_committed")
    assert (tmp_path / "target").read_bytes() == (b"new" if failure_stage == "directory_sync" else b"old")
    assert sorted(path.name for path in tmp_path.iterdir()) == original_names


def test_confirmation_syncs_existing_result_without_replacing(tmp_path, directory, monkeypatch):
    from app.storage.file_io import atomic_write, confirm_durable
    atomic_write(directory, "target", b"new")
    before = (tmp_path / "target").stat()
    sync_modes = []
    real_sync = os.fsync

    def sync(fd):
        sync_modes.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", sync)
    confirm_durable(directory, "target")
    after = (tmp_path / "target").stat()
    assert sync_modes == [False, True]
    assert before.st_ino == after.st_ino and before.st_mtime_ns == after.st_mtime_ns


@pytest.mark.parametrize("exception", [asyncio.CancelledError(), StatePersistenceError("critical"), RuntimeError("domain error")])
def test_stream_cleanup_preserves_body_exception(tmp_path, directory, exception):
    from app.storage.file_io import AtomicWriter
    original_names = sorted(path.name for path in tmp_path.iterdir())
    with pytest.raises(type(exception)) as caught:
        with AtomicWriter(directory, "target") as writer:
            writer.write(b"part")
            raise exception
    assert caught.value is exception
    assert sorted(path.name for path in tmp_path.iterdir()) == original_names


def test_open_directory_rejects_symlink_component(tmp_path):
    from app.storage.file_io import directory_fd
    from app.storage.errors import UnsafeStoragePathError
    (tmp_path / "actual").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(UnsafeStoragePathError):
        with directory_fd(tmp_path / "link"):
            pytest.fail("unsafe directory opened")


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("failure", [asyncio.CancelledError(), StatePersistenceError("critical")])
def test_descriptor_cleanup_cannot_replace_control_error(tmp_path, directory, monkeypatch, kind, failure):
    from app.storage.file_io import directory_fd, open_regular_file
    (tmp_path / "data").write_bytes(b"data")
    context = directory_fd(tmp_path) if kind == "directory" else open_regular_file(directory, "data")
    real_close = os.close
    with pytest.raises(type(failure)) as caught:
        with monkeypatch.context() as patch:
            with context as opened:
                def close(fd):
                    real_close(fd)
                    if fd == opened:
                        raise OSError(errno.EIO, "close failed")
                patch.setattr(os, "close", close)
                raise failure
    assert caught.value is failure


def test_read_failure_is_not_returned_as_missing(directory, tmp_path, monkeypatch):
    from app.storage.file_io import bounded_read
    from app.storage.errors import StorageError
    (tmp_path / "data").write_bytes(b"data")
    def fail(*args):
        raise OSError(errno.EIO, "read failed")
    monkeypatch.setattr(os, "read", fail)
    with pytest.raises(StorageError) as caught:
        bounded_read(directory, "data", 10)
    assert caught.value.category == "io" and caught.value.stage == "read"


def test_replace_rechecks_destination_after_streaming(directory, tmp_path):
    from app.storage.file_io import AtomicWriter
    from app.storage.errors import UnsafeStoragePathError
    (tmp_path / "private").write_bytes(b"private")
    with pytest.raises(UnsafeStoragePathError):
        with AtomicWriter(directory, "target") as writer:
            writer.write(b"new")
            (tmp_path / "target").symlink_to(tmp_path / "private")
            writer.commit()
    assert (tmp_path / "private").read_bytes() == b"private"


def test_stream_handles_short_writes_without_losing_data(directory, tmp_path, monkeypatch):
    from app.storage.file_io import atomic_write
    real_write = os.write
    def short_write(fd, data):
        return real_write(fd, data[:2])
    monkeypatch.setattr(os, "write", short_write)
    atomic_write(directory, "target", b"1234567")
    assert (tmp_path / "target").read_bytes() == b"1234567"


def test_created_child_directory_is_synced_again_after_uncertain_creation(directory, tmp_path, monkeypatch):
    from app.storage.file_io import child_directory_fd
    from app.storage.errors import StorageError
    real_sync = os.fsync
    def fail(fd):
        raise OSError(errno.EIO, "sync failed")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(StorageError) as caught:
            with child_directory_fd(directory, "new", create=True):
                pytest.fail("creation was not confirmed")
    assert caught.value.stage == "directory_sync"
    assert caught.value.commit_state == "uncertain"
    assert (tmp_path / "new").is_dir()
    synced = []
    def sync(fd):
        synced.append(fd)
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", sync)
    with child_directory_fd(directory, "new", create=True):
        assert synced == [directory]


def test_child_directory_creation_refuses_symlink(directory, tmp_path):
    from app.storage.file_io import child_directory_fd
    from app.storage.errors import UnsafeStoragePathError
    (tmp_path / "actual").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(UnsafeStoragePathError):
        with child_directory_fd(directory, "link", create=True):
            pytest.fail("followed unsafe directory")
