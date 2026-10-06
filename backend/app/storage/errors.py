"""只公开安全技术字段；原异常保留在原因链，不复制路径或业务正文。"""

import errno
import sqlite3
from typing import Literal


CommitState = Literal["not_committed", "committed", "uncertain"]
Recovery = Literal["retry", "wait", "manual", "verify"]


class StorageError(RuntimeError):
    def __init__(self, *, backend: str, operation: str, category: str, stage: str,
                 commit_state: CommitState = "not_committed", recovery: Recovery = "manual") -> None:
        self.backend = backend
        self.operation = operation
        self.category = category
        self.stage = stage
        self.commit_state = commit_state
        self.recovery = recovery
        super().__init__(f"Storage {backend}/{operation} failed: {category} at {stage} ({commit_state})")

    def safe_fields(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in (
            "backend", "operation", "category", "stage", "commit_state", "recovery",
        )}


class UnsafeStoragePathError(StorageError, ValueError):
    """文件名、文件种类或链接不符合安全规则；仍可按 ValueError 处理。"""

    def __init__(self, *, operation: str, stage: str = "open") -> None:
        super().__init__(backend="file", operation=operation, category="invalid_access", stage=stage)


class StorageLimitError(ValueError):
    """读取超限属于输入/领域约束，不应作为瞬时 IO 故障重试。"""


def _recovery(category: str, commit_state: CommitState) -> Recovery:
    if commit_state == "uncertain":
        return "verify"
    if category == "busy":
        return "retry"
    if category in {"permission", "no_space", "io"}:
        return "wait"
    return "manual"


def classify_os_error(error: OSError, *, operation: str, stage: str,
                      commit_state: CommitState = "not_committed") -> StorageError:
    category = {
        errno.ENOENT: "not_found", errno.EACCES: "permission", errno.EPERM: "permission",
        errno.EROFS: "permission", errno.ENOSPC: "no_space", errno.EDQUOT: "no_space",
        errno.EBUSY: "busy", errno.EAGAIN: "busy", errno.EINTR: "busy",
        errno.ELOOP: "invalid_access", errno.ENOTDIR: "invalid_access",
        errno.EISDIR: "invalid_access", errno.EINVAL: "invalid_access",
    }.get(error.errno, "io")
    result = StorageError(backend="file", operation=operation, category=category, stage=stage,
                          commit_state=commit_state, recovery=_recovery(category, commit_state))
    result.__cause__ = error
    return result


def classify_sqlite_error(error: sqlite3.Error, *, operation: str, stage: str,
                          commit_state: CommitState = "not_committed") -> StorageError:
    """依据 SQLite 主错误码分类，包括扩展错误码，不解析可能含私有值的正文。"""
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    category = {
        sqlite3.SQLITE_BUSY: "busy", sqlite3.SQLITE_LOCKED: "busy",
        sqlite3.SQLITE_FULL: "no_space", sqlite3.SQLITE_READONLY: "permission",
        sqlite3.SQLITE_PERM: "permission", sqlite3.SQLITE_AUTH: "permission",
        sqlite3.SQLITE_CORRUPT: "corrupt", sqlite3.SQLITE_NOTADB: "corrupt",
        sqlite3.SQLITE_CONSTRAINT: "conflict", sqlite3.SQLITE_MISUSE: "invalid_access",
        sqlite3.SQLITE_ERROR: "invalid_access",
    }.get(code, "io")
    result = StorageError(backend="sqlite", operation=operation, category=category, stage=stage,
                          commit_state=commit_state, recovery=_recovery(category, commit_state))
    result.__cause__ = error
    return result
