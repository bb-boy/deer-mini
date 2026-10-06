import asyncio
import errno
import os
import stat

import pytest

from app.filesystem import tool_result_store
from app.runtime.errors import StatePersistenceError
from app.services.workspace_file_service import (
    UnsafeWorkspacePathError, UploadTooLargeError, WorkspaceFileService,
)
from app.storage.errors import StorageError


@pytest.fixture
def workspace(tmp_path):
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    return tmp_path / "workspace"


async def chunks():
    yield b"upload"


def test_upload_reports_directory_sync_failure_as_uncertain(workspace, monkeypatch):
    real_sync = os.fsync

    def sync(fd):
        if os.fstat(fd).st_ino == (workspace.parent / "uploads").stat().st_ino:
            raise OSError(errno.EIO, "private details")
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(StorageError) as caught:
        asyncio.run(WorkspaceFileService().save_upload(str(workspace), "a", chunks()))
    assert caught.value.stage == "directory_sync" and caught.value.commit_state == "uncertain"
    assert (workspace.parent / "uploads/a").read_bytes() == b"upload"


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), StatePersistenceError("critical")])
def test_upload_cleans_staging_and_propagates_control_errors(workspace, failure):
    async def interrupted():
        yield b"part"
        raise failure
    with pytest.raises(type(failure)) as caught:
        asyncio.run(WorkspaceFileService().save_upload(str(workspace), "a", interrupted()))
    assert caught.value is failure
    assert list((workspace.parent / "uploads").iterdir()) == []


def test_upload_limit_retains_domain_exception_and_old_file(workspace):
    (workspace.parent / "uploads/a").write_bytes(b"old")
    with pytest.raises(UploadTooLargeError):
        asyncio.run(WorkspaceFileService(max_upload_bytes=2).save_upload(str(workspace), "a", chunks()))
    assert (workspace.parent / "uploads/a").read_bytes() == b"old"
    assert len(list((workspace.parent / "uploads").iterdir())) == 1


def test_upload_does_not_claim_commit_for_chunk_source_failure(workspace):
    failure = OSError(errno.ECONNRESET, "input stream failed")
    async def interrupted():
        yield b"part"
        raise failure
    with pytest.raises(OSError) as caught:
        asyncio.run(WorkspaceFileService().save_upload(str(workspace), "a", interrupted()))
    assert caught.value is failure
    assert list((workspace.parent / "uploads").iterdir()) == []


def test_upload_rejects_hardlink_destination(workspace):
    original = workspace.parent / "private"
    original.write_bytes(b"private")
    os.link(original, workspace.parent / "uploads/a")
    with pytest.raises(UnsafeWorkspacePathError):
        asyncio.run(WorkspaceFileService().save_upload(str(workspace), "a", chunks()))
    assert original.read_bytes() == b"private"


def test_tool_result_read_rejects_hardlink(workspace):
    original = workspace.parent / "private"
    original.write_bytes(b"private")
    results = workspace / ".tool-results"
    results.mkdir()
    os.link(original, results / "a.txt")
    with pytest.raises(ValueError):
        tool_result_store.read_chars(str(workspace), "workspace/.tool-results/a.txt", 0, 20)


def test_tool_result_storage_uses_classified_error(workspace, monkeypatch):
    real_sync = os.fsync
    def fail(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOSPC, "private details")
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(StorageError) as caught:
        tool_result_store.save_text(str(workspace), "workspace/.tool-results/a.txt", "content")
    assert caught.value.stage == "file_sync" and caught.value.commit_state == "not_committed"


def test_listing_and_download_reject_hardlinks(workspace):
    original = workspace.parent / "private"
    original.write_bytes(b"private")
    os.link(original, workspace / "a")
    service = WorkspaceFileService()
    assert service.list_files(str(workspace)) == []
    with pytest.raises(UnsafeWorkspacePathError):
        service.resolve_download(str(workspace), "a")


def test_tool_result_synchronizes_new_directory_entries_before_writing(workspace, monkeypatch):
    synced = set()
    original_sync, original_write = os.fsync, os.write
    def sync(fd):
        synced.add(os.fstat(fd).st_ino)
        original_sync(fd)
    def write(fd, data):
        assert workspace.stat().st_ino in synced
        assert (workspace / ".tool-results").stat().st_ino in synced
        return original_write(fd, data)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "write", write)
    tool_result_store.save_text(str(workspace), "workspace/.tool-results/nested/a.txt", "data")
    assert (workspace / ".tool-results/nested/a.txt").read_text() == "data"


def test_upload_synchronizes_new_uploads_directory_entry(workspace, monkeypatch):
    (workspace.parent / "uploads").rmdir()
    synced = set()
    original_sync, original_write = os.fsync, os.write
    def sync(fd):
        synced.add(os.fstat(fd).st_ino)
        original_sync(fd)
    def write(fd, data):
        assert workspace.parent.stat().st_ino in synced
        return original_write(fd, data)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "write", write)
    asyncio.run(WorkspaceFileService().save_upload(str(workspace), "a", chunks()))
