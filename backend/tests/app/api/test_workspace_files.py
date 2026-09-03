"""Thread Workspace 文件上传、列表和下载接口测试。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import router
from app.domain.threads import Thread
from app.infrastructure import database
from app.repositories.thread_repository import ThreadRepository


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@pytest.fixture()
def workspace_client(tmp_path, monkeypatch):
    """创建隔离数据库和真实临时 Workspace，不运行应用 lifespan。"""
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "files_api.db")
    database.initialize_database()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    thread = Thread(
        id="files-thread",
        user_id="alice",
        workspace_path=str(workspace),
        title="Files test",
    )
    ThreadRepository().create(thread)

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, workspace


def test_upload_list_and_download_workspace_file(workspace_client):
    client, workspace = workspace_client

    uploaded = client.post(
        "/api/threads/files-thread/files",
        params={"user_id": "alice"},
        files={"file": ("report.txt", b"deer mini report", "text/plain")},
    )
    listing = client.get(
        "/api/threads/files-thread/files",
        params={"user_id": "alice"},
    )
    downloaded = client.get(
        "/api/threads/files-thread/files/report.txt",
        params={"user_id": "alice"},
    )

    assert uploaded.status_code == 201
    assert uploaded.json()["relative_path"] == "report.txt"
    assert (workspace / "report.txt").read_bytes() == b"deer mini report"
    assert [item["relative_path"] for item in listing.json()] == ["report.txt"]
    assert downloaded.status_code == 200
    assert downloaded.content == b"deer mini report"


def test_list_and_download_nested_agent_output(workspace_client):
    client, workspace = workspace_client
    output = workspace / "outputs" / "result.txt"
    output.parent.mkdir()
    output.write_text("agent result", encoding="utf-8")

    listing = client.get(
        "/api/threads/files-thread/files",
        params={"user_id": "alice"},
    )
    downloaded = client.get(
        "/api/threads/files-thread/files/outputs/result.txt",
        params={"user_id": "alice"},
    )

    assert listing.status_code == 200
    assert [item["relative_path"] for item in listing.json()] == [
        "outputs/result.txt"
    ]
    assert downloaded.status_code == 200
    assert downloaded.text == "agent result"


def test_file_endpoints_check_thread_ownership(workspace_client):
    client, _ = workspace_client

    response = client.get(
        "/api/threads/files-thread/files",
        params={"user_id": "bob"},
    )

    assert response.status_code == 404


def test_upload_rejects_path_traversal_filename(workspace_client, tmp_path):
    client, workspace = workspace_client

    response = client.post(
        "/api/threads/files-thread/files",
        params={"user_id": "alice"},
        files={"file": ("../escape.txt", b"secret", "text/plain")},
    )

    assert response.status_code == 400
    assert not (workspace / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_download_rejects_symlink_that_escapes_workspace(workspace_client, tmp_path):
    client, workspace = workspace_client
    outside = tmp_path / "outside.txt"
    outside.write_text("must stay private", encoding="utf-8")
    (workspace / "outside-link.txt").symlink_to(outside)

    listing = client.get(
        "/api/threads/files-thread/files",
        params={"user_id": "alice"},
    )
    downloaded = client.get(
        "/api/threads/files-thread/files/outside-link.txt",
        params={"user_id": "alice"},
    )

    assert listing.status_code == 200
    assert listing.json() == []
    assert downloaded.status_code == 400
    assert b"must stay private" not in downloaded.content


def test_upload_too_large_returns_413_without_partial_file(workspace_client):
    client, workspace = workspace_client

    response = client.post(
        "/api/threads/files-thread/files",
        params={"user_id": "alice"},
        files={
            "file": (
                "large.bin",
                b"x" * (MAX_UPLOAD_BYTES + 1),
                "application/octet-stream",
            )
        },
    )

    assert response.status_code == 413
    assert not (workspace / "large.bin").exists()
    assert list(workspace.glob(".upload-*.part")) == []
