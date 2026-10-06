"""可持久登记的记忆保存项及安全冲突、核实结果。

保存项固定操作身份、目标、旧版本和完整结果；JSON 快照可跨进程重启恢复。
摘要使用规范编码，不依赖文件修改时间或模糊内容匹配。
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.storage.errors import StorageError

if TYPE_CHECKING:
    from app.memory.store import MemoryRecord


class MemoryConflictError(ValueError):
    """目标已被其他有效操作修改，禁止旧保存项覆盖当前内容。"""


class MemoryVerificationError(StorageError):
    """操作可能已生效，但正文或索引仍无法确认为持久结果。"""

    def __init__(self, storage_error: StorageError | None = None) -> None:
        self.storage_error = storage_error
        super().__init__(backend=storage_error.backend if storage_error else "file",
                         operation=storage_error.operation if storage_error else "write",
                         category=storage_error.category if storage_error else "io",
                         stage=storage_error.stage if storage_error else "verify",
                         commit_state="uncertain", recovery="verify")


def record_payload(record: "MemoryRecord") -> dict[str, Any]:
    values = asdict(record)
    values["created_at"] = record.created_at.isoformat()
    values["updated_at"] = record.updated_at.isoformat()
    return values


def _digest(values: dict[str, Any]) -> str:
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_digest(record: "MemoryRecord") -> str:
    """摘要固定语义结果；真正提交时确定的时间戳不参与预期摘要。"""
    values = record_payload(record)
    for field in ("operation_id", "result_digest", "version", "created_at", "updated_at"):
        values.pop(field, None)
    return _digest(values)


def record_version(record: "MemoryRecord") -> str:
    """同时校验实际内容和操作身份；兼容没有版本字段的旧正文。"""
    return _digest(record_payload(record))


@dataclass(frozen=True)
class PreparedMemoryOperation:
    """已完成校验、尚未开始提交的不可变保存项。"""

    operation_id: str
    memory_id: str
    expected_version: str | None
    result_digest: str
    record: "MemoryRecord"

    def to_dict(self) -> dict[str, Any]:
        return dict(operation_id=self.operation_id, memory_id=self.memory_id,
                    expected_version=self.expected_version, result_digest=self.result_digest,
                    record=record_payload(self.record))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PreparedMemoryOperation":
        from app.memory.store import MemoryRecord

        data = dict(value)
        payload = dict(data.pop("record"))
        payload["created_at"] = datetime.fromisoformat(payload["created_at"])
        payload["updated_at"] = datetime.fromisoformat(payload["updated_at"])
        return cls(record=MemoryRecord(**payload), **data)
