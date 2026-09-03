"""安全读写一个 Thread Workspace 中的普通文件。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile


DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
STAGING_PREFIX = ".upload-"
STAGING_SUFFIX = ".part"


class UnsafeWorkspacePathError(ValueError):
    """用户提供的路径试图越过 Workspace 或指向不安全文件。"""


class UploadTooLargeError(ValueError):
    """上传内容超过服务允许的最大字节数。"""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"上传文件不能超过 {max_bytes} 字节")
        self.max_bytes = max_bytes


@dataclass(frozen=True)
class WorkspaceFile:
    """浏览器展示一个 Workspace 文件所需的元数据。"""

    relative_path: str
    name: str
    size: int
    modified_at: str


class WorkspaceFileService:
    """处理上传、列表和下载路径解析，不依赖 FastAPI。"""

    def __init__(self, max_upload_bytes: int | None = None) -> None:
        self._max_upload_bytes = (
            self._configured_max_upload_bytes()
            if max_upload_bytes is None
            else max_upload_bytes
        )
        if self._max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes 必须大于 0")

    async def save_upload(
        self,
        workspace_path: str,
        filename: str,
        chunks: AsyncIterator[bytes],
    ) -> WorkspaceFile:
        """分块写入上传内容，完整写完后才替换 Workspace 中的目标文件。"""
        workspace = self._resolve_workspace(workspace_path)
        safe_name = self._validate_upload_filename(filename)
        destination = workspace / safe_name
        self._validate_upload_destination(destination)

        file_descriptor, staging_value = tempfile.mkstemp(
            prefix=STAGING_PREFIX,
            suffix=STAGING_SUFFIX,
            dir=workspace,
        )
        staging_path = Path(staging_value)
        total_bytes = 0

        try:
            with os.fdopen(file_descriptor, "wb") as staging_file:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("上传数据块必须是 bytes")
                    total_bytes += len(chunk)
                    if total_bytes > self._max_upload_bytes:
                        raise UploadTooLargeError(self._max_upload_bytes)
                    staging_file.write(chunk)
                staging_file.flush()
                os.fsync(staging_file.fileno())

            # staging 和目标位于同一个目录，os.replace 是一次原子替换。
            os.replace(staging_path, destination)
        except BaseException:
            staging_path.unlink(missing_ok=True)
            raise

        return self._to_workspace_file(workspace, destination)

    def list_files(self, workspace_path: str) -> list[WorkspaceFile]:
        """递归列出 Workspace 内的普通文件，不跟随符号链接。"""
        workspace = self._resolve_workspace(workspace_path)
        files: list[WorkspaceFile] = []
        for path in workspace.rglob("*"):
            if path.name.startswith(STAGING_PREFIX) and path.name.endswith(
                STAGING_SUFFIX
            ):
                continue
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                # Agent 可能正好在列表期间删除文件，跳过即可。
                continue
            if stat.S_ISLNK(path_stat.st_mode):
                continue
            if not stat.S_ISREG(path_stat.st_mode):
                continue
            files.append(self._to_workspace_file(workspace, path, path_stat))

        return sorted(files, key=lambda item: item.relative_path)

    def resolve_download(self, workspace_path: str, relative_path: str) -> Path:
        """返回经过边界检查的下载文件绝对路径。"""
        workspace = self._resolve_workspace(workspace_path)
        if not relative_path or "\x00" in relative_path or "\\" in relative_path:
            raise UnsafeWorkspacePathError("文件路径不安全")

        requested = Path(relative_path)
        if requested.is_absolute():
            raise UnsafeWorkspacePathError("只能下载当前 Workspace 中的文件")

        unresolved = workspace / requested
        if unresolved.is_symlink():
            raise UnsafeWorkspacePathError("不能下载符号链接")

        try:
            target = unresolved.resolve(strict=True)
            target.relative_to(workspace)
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as error:
            raise UnsafeWorkspacePathError(
                "只能下载当前 Workspace 中的文件"
            ) from error

        if not target.is_file():
            raise FileNotFoundError(relative_path)
        return target

    @staticmethod
    def _resolve_workspace(workspace_path: str) -> Path:
        try:
            workspace = Path(workspace_path).resolve(strict=True)
        except (OSError, ValueError) as error:
            raise FileNotFoundError("Thread Workspace 不存在") from error
        if not workspace.is_dir():
            raise FileNotFoundError("Thread Workspace 不存在")
        return workspace

    @staticmethod
    def _validate_upload_filename(filename: str) -> str:
        if (
            not filename
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
            or Path(filename).name != filename
        ):
            raise UnsafeWorkspacePathError("上传文件名不能包含目录路径")
        if len(filename.encode("utf-8")) > 255:
            raise UnsafeWorkspacePathError("上传文件名过长")
        return filename

    @staticmethod
    def _validate_upload_destination(destination: Path) -> None:
        try:
            destination_stat = destination.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(destination_stat.st_mode):
            raise UnsafeWorkspacePathError("上传目标不是普通文件")

    @staticmethod
    def _to_workspace_file(
        workspace: Path,
        path: Path,
        path_stat: os.stat_result | None = None,
    ) -> WorkspaceFile:
        path_stat = path_stat or path.stat()
        return WorkspaceFile(
            relative_path=path.relative_to(workspace).as_posix(),
            name=path.name,
            size=path_stat.st_size,
            modified_at=datetime.fromtimestamp(
                path_stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        )

    @staticmethod
    def _configured_max_upload_bytes() -> int:
        raw_value = os.getenv(
            "DEER_MINI_MAX_UPLOAD_BYTES",
            str(DEFAULT_MAX_UPLOAD_BYTES),
        )
        try:
            return int(raw_value)
        except ValueError as error:
            raise ValueError(
                "DEER_MINI_MAX_UPLOAD_BYTES 必须是整数"
            ) from error
