"""可信运维环境中的记忆任务管理入口。

输入为必选用户范围、子命令及明确确认标志；输出为 JSON 与退出码。
仅连接已存在且具备任务表的 SQLite，不初始化数据库或启动后台 worker。
写命令只改变任务账本，不直接读写记忆正文；异常不输出私有内容或原因链。
"""

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.infrastructure.database import resolve_database_path
from app.memory.operations import record_payload
from app.memory.store import MemoryStore
from app.repositories.memory_task_repository import MemoryTaskRepository
from app.storage.errors import classify_sqlite_error
from app.storage.sqlite import StorageConnection


class _ArgumentError(ValueError):
    pass


class _SchemaUnavailable(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse 的原始错误可能回显参数值，不能把它写入后台日志。
        raise _ArgumentError


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m app.memory.admin", allow_abbrev=False,
                     description="Manage registered memory saves for one user.")
    parser.add_argument("--user", required=True, help="Required owner user ID.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", allow_abbrev=False, help="List recent task metadata.")
    listing.add_argument("--limit", type=int, default=100, help="Task count, from 1 to 1000 (default: 100).")
    show = commands.add_parser("show", allow_abbrev=False, help="Show task items and attempt history.")
    show.add_argument("task")
    show.add_argument("--include-content", action="store_true", help="Explicitly display retained private payloads.")
    show.add_argument("--include-current", action="store_true", help="Explicitly read current memories using DEER_MINI_DATA_ROOT.")
    for name, description in (
        ("retry", "Open a new 24-hour window for eligible failed items."),
        ("cancel", "Request cancellation of future attempts."),
        ("resolve-conflict", "Keep current memory and cancel the old conflicting item."),
        ("cleanup", "Delete resolved task history closed for at least 30 days."),
    ):
        command = commands.add_parser(name, allow_abbrev=False, help=description)
        if name != "cleanup":
            command.add_argument("task")
        if name == "resolve-conflict":
            command.add_argument("operation")
        command.add_argument("--confirm", action="store_true", help="Confirm this task-record mutation.")
    return parser


@contextmanager
def _connection(path: Path, *, writable: bool) -> Iterator[sqlite3.Connection]:
    """以 SQLite URI 模式连接现有库，检查表后交给 Repository，退出时关闭连接。"""
    mode = "rw" if writable else "ro"
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, factory=StorageConnection)
    except sqlite3.Error as error:
        raise classify_sqlite_error(error, operation="open", stage="open") from error
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("PRAGMA foreign_keys = ON")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"memory_save_tasks", "memory_save_items", "memory_save_attempts"} <= tables:
                raise _SchemaUnavailable
            yield conn
    finally:
        conn.close()


def _error(message: str, code: int = 1) -> int:
    print(json.dumps({"error": message}), file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令并在 Repository 事务确认后输出结果；失败返回非零且不泄漏异常正文。"""
    try:
        args = _parser().parse_args(argv)
        if not args.user.strip():
            raise _ArgumentError
    except _ArgumentError:
        return _error("Invalid command arguments; use --help. --user is required.", 2)

    writable = args.command not in {"list", "show"}
    if writable and not args.confirm:
        return _error("This command requires --confirm.", 2)

    try:
        path = resolve_database_path()
        if not path.is_file():
            return _error("Configured database is unavailable; no database was created.")
        repo = MemoryTaskRepository(connection_factory=lambda: _connection(path, writable=writable))
        result: Any
        if args.command == "list":
            result = repo.list_tasks(args.user, limit=args.limit)
        elif args.command == "show":
            result = repo.get(args.user, args.task, include_content=args.include_content)
            if result is None:
                return _error("Memory task not found for this user.")
            if args.include_current:
                configured_root = os.getenv("DEER_MINI_DATA_ROOT")
                if not configured_root or not Path(configured_root).is_absolute():
                    return _error("--include-current requires an absolute DEER_MINI_DATA_ROOT.")
                store = MemoryStore(data_root=Path(configured_root))
                for item in result["items"]:
                    current = store.read(args.user, item["memory_id"])
                    if current is None:
                        return _error("Current memory is missing or cannot be read safely.")
                    item["current_memory"] = record_payload(current)
        elif args.command == "cleanup":
            result = {"deleted_tasks": repo.cleanup(args.user)}
        else:
            if args.command == "retry":
                repo.retry(args.user, args.task)
            elif args.command == "cancel":
                repo.cancel(args.user, args.task)
            else:
                repo.resolve_conflict(args.user, args.task, args.operation)
            result = {"command": args.command, "task_id": args.task, "accepted": True}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except _SchemaUnavailable:
        return _error("Memory recovery schema is unavailable; database was not initialized.")
    except ValueError:
        return _error("Command rejected; check configuration, user scope and task state.")
    except Exception:
        # CLI 是失败终点：持久化异常必须非零退出，不能输出成功或完整异常信息。
        return _error("Memory task command failed; completion is not confirmed.")


if __name__ == "__main__":
    raise SystemExit(main())
