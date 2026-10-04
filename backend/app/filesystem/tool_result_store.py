"""当前 Thread 内工具结果的完整保存和字符分页读取。"""

import os
from pathlib import Path
from uuid import uuid4

from app.filesystem.thread_paths import ThreadPaths, VIRTUAL_WORKSPACE


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
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # 创建目录后重新检查，避免已有符号链接被作为结果目录使用。
    target = resolve_result_path(workspace_path, virtual_path)
    temporary = target.with_name("." + uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_chars(workspace_path: str, virtual_path: str, offset: int, limit: int) -> str:
    target = resolve_result_path(workspace_path, virtual_path)
    if not target.is_file():
        raise FileNotFoundError("工具结果文件不存在或不是普通文件")
    with target.open("r", encoding="utf-8", newline="") as file:
        # UTF-8 的字符偏移不能直接用于字节 seek；分块跳过，保持内存有界。
        remaining = offset
        while remaining:
            chunk = file.read(min(8192, remaining))
            if not chunk:
                return ""
            remaining -= len(chunk)
        return file.read(limit)


def preview_header(virtual_path: str, total_chars: int, preview_chars: int) -> str:
    return (
        f"[工具结果全文已保存，共 {total_chars} 字符]\n"
        f"文件：{virtual_path}\n"
        f"使用 read_tool_result(path=\"{virtual_path}\", offset={preview_chars}, limit=2000) 继续读取。\n"
        "以下为内容预览：\n"
    )
