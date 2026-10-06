"""当前 Thread 内工具结果的完整保存和字符分页读取。"""

import os
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from app.filesystem.thread_paths import ThreadPaths, VIRTUAL_WORKSPACE
from app.storage.errors import StorageError, classify_os_error
from app.storage.file_io import atomic_write, child_directory_fd, directory_fd, open_regular_file


RESULT_DIRECTORY = ".tool-results"


def new_result_path() -> str:
    return f"{VIRTUAL_WORKSPACE}/{RESULT_DIRECTORY}/{uuid4().hex}.txt"


def resolve_result_path(workspace_path: str, virtual_path: str) -> Path:
    paths = ThreadPaths(Path(workspace_path).parent)
    target = paths.resolve_agent_path(virtual_path)
    root = paths.workspace_path / RESULT_DIRECTORY
    if not target.is_relative_to(root) or target == root:
        raise ValueError("只能读取当前对话的 .tool-results 文件")
    return target


def save_text(workspace_path: str, virtual_path: str, text: str) -> None:
    target = resolve_result_path(workspace_path, virtual_path)
    workspace = ThreadPaths(Path(workspace_path).parent).workspace_path
    with ExitStack() as directories:
        fd = directories.enter_context(directory_fd(workspace))
        for part in target.parent.relative_to(workspace).parts:
            fd = directories.enter_context(child_directory_fd(fd, part, create=True))
        atomic_write(fd, target.name, text.encode("utf-8"))


def read_chars(workspace_path: str, virtual_path: str, offset: int, limit: int) -> str:
    if offset < 0 or not 0 <= limit <= 50_000:
        raise ValueError("工具结果分页范围不合法")
    target = resolve_result_path(workspace_path, virtual_path)
    try:
        with directory_fd(target.parent) as directory, open_regular_file(directory, target.name) as fd:
            with os.fdopen(fd, "r", encoding="utf-8", newline="", closefd=False) as file:
                # UTF-8 的字符偏移不能直接用于字节 seek；分块跳过，保持内存有界。
                remaining = offset
                while remaining:
                    chunk = file.read(min(8192, remaining))
                    if not chunk:
                        return ""
                    remaining -= len(chunk)
                return file.read(limit)
    except StorageError as error:
        if error.category == "not_found":
            raise FileNotFoundError("工具结果文件不存在") from error
        raise
    except OSError as error:
        raise classify_os_error(error, operation="read", stage="read") from error


def preview_header(virtual_path: str, total_chars: int, preview_chars: int) -> str:
    return (
        f"[工具结果全文已保存，共 {total_chars} 字符]\n"
        f"文件：{virtual_path}\n"
        f"使用 read_tool_result(path=\"{virtual_path}\", offset={preview_chars}, limit=2000) 继续读取。\n"
        "以下为内容预览：\n"
    )
