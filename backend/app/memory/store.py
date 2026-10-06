"""将用户记忆持久化为 Markdown 正文，并从正文重建元数据索引。

正文是唯一事实来源；索引不含正文，不用于直接渲染模型上下文。
当前单进程内以用户级锁串行读写，通过目录描述符拒绝符号链接与路径穿越。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.storage.errors import StorageError, StorageLimitError, UnsafeStoragePathError, classify_os_error
from app.storage.file_io import atomic_write, bounded_read, confirm_durable
from app.memory.operations import (MemoryConflictError, MemoryVerificationError,
                                   PreparedMemoryOperation, record_digest, record_version)

logger = logging.getLogger(__name__)


MAX_CONTENT_BYTES = 64 * 1024
MAX_MEMORY_FILE_BYTES = 128 * 1024
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 1000
MAX_MEMORIES_PER_USER = 1000
MAX_INDEX_BYTES = 4 * 1024 * 1024
MEMORY_TYPES = frozenset({"user", "feedback", "reference"})
_ID_PATTERN = re.compile(r"(?:user|feedback|reference)_[0-9a-f]{32}\Z")
_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("记忆时间必须包含时区")
    return value


@dataclass(frozen=True)
class MemoryMetadata:
    """索引元数据快照，不携带正文；后端再裁剪为选择模型的候选。"""

    id: str
    type: str
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    source_thread_id: str
    source_run_id: str
    operation_id: str | None = None
    result_digest: str | None = None
    version: str | None = None

    def is_stable(self, now: datetime) -> bool:
        """按索引中的最后修改时间判断是否满 48 小时。"""
        return _aware(now) - _aware(self.updated_at) >= timedelta(hours=48)


@dataclass(frozen=True)
class MemoryRecord:
    """不可变记忆快照；时间带时区，来源标记最后一次有效修改。"""

    id: str
    type: str
    name: str
    description: str
    content: str
    created_at: datetime
    updated_at: datetime
    source_thread_id: str
    source_run_id: str
    operation_id: str | None = None
    result_digest: str | None = None
    version: str | None = None

    def is_stable(self, now: datetime) -> bool:
        """正文最后修改满 48 小时后才视为稳定记忆。"""
        return _aware(now) - _aware(self.updated_at) >= timedelta(hours=48)


class MemoryStore:
    """安全读写 data_root/user_id/memories 下的用户私有记忆。

    输入根目录与可注入时钟；默认根目录在每次操作时读取环境配置。
    list/catalog 可修复已有目录中的索引，读取不会为不存在的用户创建目录。
    upsert 先原子持久化正文再持久化索引；失败向调用者传播以避免假成功。
    """

    def __init__(self, data_root: Path | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._data_root = Path(data_root) if data_root is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _root(self) -> Path:
        if self._data_root is not None:
            root = self._data_root
        else:
            from app.services.thread_service import resolve_data_root

            root = resolve_data_root()
            # resolver 返回规范路径；保留配置路径以便拒绝配置目录的符号链接。
            configured = os.getenv("DEER_MINI_DATA_ROOT")
            if configured:
                root = Path(configured)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("记忆根目录必须是安全绝对路径")
        return root

    @staticmethod
    def _user(user_id: str) -> None:
        if (not isinstance(user_id, str) or not user_id or len(user_id) > 200
                or user_id in {".", ".."} or any(c in user_id for c in "/\\")
                or any(ord(c) < 32 or ord(c) == 127 for c in user_id)):
            raise ValueError("非法 user_id")

    @staticmethod
    def _id(memory_id: str) -> None:
        if not isinstance(memory_id, str) or not _ID_PATTERN.fullmatch(memory_id):
            raise ValueError("非法 memory_id")

    @staticmethod
    def _fields(kind: str, name: str, description: str, content: str,
                source_thread_id: str, source_run_id: str) -> None:
        if kind not in MEMORY_TYPES:
            raise ValueError("不支持的记忆类型")
        for value, limit, required in ((name, MAX_NAME_CHARS, True),
                                       (description, MAX_DESCRIPTION_CHARS, False),
                                       (source_thread_id, 200, True),
                                       (source_run_id, 200, True)):
            if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
                raise ValueError("记忆元数据超限或无效")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("记忆正文不能为空")
        try:
            if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
                raise ValueError("记忆正文超限")
            for value in (name, description, source_thread_id, source_run_id):
                value.encode("utf-8")
        except UnicodeError:
            raise ValueError("记忆包含无效字符") from None

    @staticmethod
    def _lock(root: Path, user_id: str) -> threading.RLock:
        with _LOCKS_GUARD:
            return _LOCKS.setdefault((str(root), user_id), threading.RLock())

    @contextmanager
    def _directory(self, root: Path, user_id: str, *, create: bool,
                   durable: bool = False) -> Iterator[int | None]:
        """逐级安全打开目录，提交时同步父目录，保证新建目录链也已持久化。

        只读且缺失时返回 None；恢复核实允许同步既有目录，但不创建节点。
        描述符固定实际访问对象，不因目录被替换而跟随符号链接。
        """
        descriptors: list[int] = []
        stage = "open"
        missing = False
        try:
            try:
                fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(fd)
                for segment in (*root.parts[1:], user_id, "memories"):
                    stage = "open"
                    try:
                        child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except FileNotFoundError:
                        if not create:
                            missing = True
                            break
                        try:
                            os.mkdir(segment, mode=0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                        child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(child)
                    if create or durable:
                        stage = "directory_sync"
                        os.fsync(fd)
                    fd = child
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafeStoragePathError(operation="open") from error
                raise classify_os_error(error, operation="open", stage=stage,
                    commit_state="uncertain" if stage == "directory_sync" else "not_committed") from error
            yield None if missing else fd
        finally:
            active_error = sys.exception()
            cleanup_error: OSError | None = None
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError as error:
                    cleanup_error = cleanup_error or error
            if cleanup_error is not None and active_error is None:
                raise classify_os_error(cleanup_error, operation="open", stage="cleanup",
                    commit_state="uncertain" if create or durable else "not_committed") from cleanup_error

    @staticmethod
    def _read_bytes(fd: int, name: str, limit: int) -> bytes | None:
        """复用有界安全读取；损坏或超限正文保留并记录安全字段。"""
        try:
            return bounded_read(fd, name, limit)
        except StorageLimitError:
            logger.warning("memory_file_corrupt category=corrupt stage=read")
            return None

    def _decode(self, name: str, data: bytes | None) -> MemoryRecord | None:
        record = self._decode_record(name, data)
        if record is None and data is not None:
            logger.warning("memory_body_corrupt category=corrupt stage=decode")
        return record

    def _decode_record(self, name: str, data: bytes | None) -> MemoryRecord | None:
        if data is None:
            return None
        try:
            text = data.decode("utf-8")
            if not text.startswith("---\n"):
                return None
            header, content = text[4:].split("\n---\n", 1)
            metadata: dict[str, Any] = {}
            for line in header.splitlines():
                key, value = line.split(": ", 1)
                if key in metadata:
                    return None
                metadata[key] = json.loads(value)
            expected = {"id", "type", "name", "description", "created_at", "updated_at",
                        "source_thread_id", "source_run_id"}
            optional = {"operation_id", "result_digest", "version"}
            if (not expected <= metadata.keys() or not metadata.keys() <= expected | optional
                    or any(not isinstance(metadata[k], str) for k in expected)
                    or any(v is not None and not isinstance(v, str)
                           for k, v in metadata.items() if k in optional)):
                return None
            self._id(metadata["id"])
            self._fields(metadata["type"], metadata["name"], metadata["description"], content,
                         metadata["source_thread_id"], metadata["source_run_id"])
            if name != f'{metadata["id"]}.md' or not metadata["id"].startswith(metadata["type"] + "_"):
                return None
            metadata["created_at"] = _aware(datetime.fromisoformat(metadata["created_at"]))
            metadata["updated_at"] = _aware(datetime.fromisoformat(metadata["updated_at"]))
            if metadata["created_at"] > metadata["updated_at"]:
                return None
            return MemoryRecord(content=content, **metadata)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            # 损坏正文不删除，不记录其内容；其他有效正文仍可使用。
            return None

    @staticmethod
    def _stat(fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise classify_os_error(error, operation="read", stage="read") from error

    @staticmethod
    def _listdir(fd: int) -> list[str]:
        try:
            return os.listdir(fd)
        except OSError as error:
            raise classify_os_error(error, operation="read", stage="read") from error

    def _records(self, fd: int) -> list[MemoryRecord]:
        records = []
        for name in sorted(self._listdir(fd)):
            if name == "MEMORY.md" or not name.endswith(".md"):
                continue
            data = self._read_bytes(fd, name, MAX_MEMORY_FILE_BYTES)
            if not _ID_PATTERN.fullmatch(name[:-3]):
                continue
            record = self._decode(name, data)
            if record is not None:
                records.append(record)
            if len(records) > MAX_MEMORIES_PER_USER:
                raise ValueError("用户记忆数量超限")
        return sorted(records, key=lambda record: (-record.updated_at.timestamp(), record.id))

    @staticmethod
    def _atomic_write(fd: int, name: str, data: bytes) -> None:
        atomic_write(fd, name, data)

    @staticmethod
    def _index_bytes(records: list[MemoryRecord] | list[MemoryMetadata], now: datetime) -> bytes:
        metadata = []
        for record in records:
            item = asdict(record)
            item.pop("content", None)
            item["created_at"] = record.created_at.isoformat()
            item["updated_at"] = record.updated_at.isoformat()
            item["stable"] = record.is_stable(now)
            item["filename"] = f"{record.id}.md"
            metadata.append(item)
        data = ("# Memory index\n\n```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2)
                + "\n```\n").encode("utf-8")
        if len(data) > MAX_INDEX_BYTES:
            raise ValueError("记忆索引超限")
        return data

    def _repair_index(self, fd: int, records: list[MemoryRecord] | list[MemoryMetadata],
                      now: datetime) -> None:
        data = self._index_bytes(records, now)
        if self._read_bytes(fd, "MEMORY.md", MAX_INDEX_BYTES) != data:
            self._atomic_write(fd, "MEMORY.md", data)

    def _catalog_from_index(self, fd: int, *, check_bodies: bool = True) -> list[MemoryMetadata] | None:
        """仅打开索引；通过文件集合与修改时间判断是否需要从正文重建。

        健康路径只 stat 正文文件，不读取其内容；链接与特殊节点仍立即拒绝。
        索引损坏返回 None，交由持有同一用户锁的 catalog 重建。
        """
        data = self._read_bytes(fd, "MEMORY.md", MAX_INDEX_BYTES)
        if data is None:
            return None
        try:
            index_stat = self._stat(fd, "MEMORY.md")
            if index_stat is None:
                return None
            text = data.decode("utf-8")
            prefix = "# Memory index\n\n```json\n"
            suffix = "\n```\n"
            if not text.startswith(prefix) or not text.endswith(suffix):
                return None
            items = json.loads(text[len(prefix):-len(suffix)])
            if not isinstance(items, list) or len(items) > MAX_MEMORIES_PER_USER:
                return None
            records: list[MemoryMetadata] = []
            expected = {"id", "type", "name", "description", "created_at", "updated_at",
                        "source_thread_id", "source_run_id", "stable", "filename"}
            for item in items:
                optional = {"operation_id", "result_digest", "version"}
                if (not isinstance(item, dict) or not expected <= item.keys()
                        or not item.keys() <= expected | optional
                        or not isinstance(item["stable"], bool)
                        or any(not isinstance(item[k], str) for k in expected - {"stable"})
                        or any(v is not None and not isinstance(v, str)
                               for k, v in item.items() if k in optional)):
                    return None
                self._id(item["id"])
                self._fields(item["type"], item["name"], item["description"], "index",
                             item["source_thread_id"], item["source_run_id"])
                if (not item["id"].startswith(item["type"] + "_")
                        or item["filename"] != item["id"] + ".md"):
                    return None
                item.pop("filename")
                item.pop("stable")
                item["created_at"] = _aware(datetime.fromisoformat(item["created_at"]))
                item["updated_at"] = _aware(datetime.fromisoformat(item["updated_at"]))
                if item["created_at"] > item["updated_at"]:
                    return None
                records.append(MemoryMetadata(**item))
            ids = {record.id for record in records}
            if len(ids) != len(records):
                return None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        if not check_bodies:
            # 保存项核实时调用者已经完整安全读取正文，允许索引跳过损坏正文。
            return sorted(records, key=lambda record: (-record.updated_at.timestamp(), record.id))
        # 文件安全检查放在解析异常处理之外，危险节点始终向调用者报告。
        body_ids: set[str] = set()
        changed = False
        for filename in self._listdir(fd):
            if filename == "MEMORY.md" or not filename.endswith(".md"):
                continue
            info = self._stat(fd, filename)
            if info is None:
                return None
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("记忆文件必须是无链接的普通文件")
            if _ID_PATTERN.fullmatch(filename[:-3]):
                body_ids.add(filename[:-3])
                changed |= info.st_mtime_ns > index_stat.st_mtime_ns
        if changed or ids != body_ids:
            return None
        return sorted(records, key=lambda record: (-record.updated_at.timestamp(), record.id))

    def catalog(self, user_id: str) -> list[MemoryMetadata]:
        """返回不含正文的索引快照；健康索引直接读取，缺失、损坏或过期才重建。"""
        self._user(user_id)
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=False) as fd:
            if fd is None:
                return []
            now = _aware(self._clock())
            metadata = self._catalog_from_index(fd)
            if metadata is None:
                metadata = []
                for record in self._records(fd):
                    item = asdict(record)
                    item.pop("content")
                    metadata.append(MemoryMetadata(**item))
            self._repair_index(fd, metadata, now)
            return metadata

    def list(self, user_id: str) -> list[MemoryRecord]:
        """返回用户的完整有效快照，按更新时间降序、ID 升序排序并修复索引。"""
        self._user(user_id)
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=False) as fd:
            if fd is None:
                return []
            records = self._records(fd)
            self._repair_index(fd, records, _aware(self._clock()))
            return records

    def read(self, user_id: str, memory_id: str) -> MemoryRecord | None:
        """读取属于该用户的指定记忆；缺失或损坏返回 None，危险路径拒绝。"""
        self._user(user_id)
        self._id(memory_id)
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=False) as fd:
            if fd is None:
                return None
            self._read_bytes(fd, "MEMORY.md", MAX_INDEX_BYTES)
            filename = f"{memory_id}.md"
            return self._decode(filename, self._read_bytes(fd, filename, MAX_MEMORY_FILE_BYTES))

    def snapshot(self, user_id: str) -> list[MemoryRecord]:
        """在用户锁中读取完整版本快照，不创建目录或修复索引。

        抽取模型只看到调用前快照；保存准备阶段再次比较对应目标版本。
        """
        self._user(user_id)
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=False) as fd:
            return self._records(fd) if fd is not None else []

    @staticmethod
    def _body_bytes(record: MemoryRecord) -> bytes:
        metadata = asdict(record)
        metadata.pop("content")
        metadata["created_at"] = record.created_at.isoformat()
        metadata["updated_at"] = record.updated_at.isoformat()
        body = "---\n" + "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()
        ) + "\n---\n" + record.content
        data = body.encode("utf-8")
        if len(data) > MAX_MEMORY_FILE_BYTES:
            raise ValueError("记忆文件超限")
        return data

    def prepare(self, user_id: str, changes: list[dict[str, Any]], *,
                source_thread_id: str, source_run_id: str,
                expected_versions: Mapping[str, str] | None = None) -> list[PreparedMemoryOperation]:
        """校验整批变更并固定身份、旧版本及结果；此过程完全不写文件。

        若提供模型调用前的 expected_versions，更新目标必须仍为原快照。
        等价内容不创建保存项，同批同一目标只允许一次有效修改。
        """
        self._user(user_id)
        if not isinstance(changes, list) or len(changes) > 10:
            raise ValueError("记忆变更数量无效")
        targets: set[str] = set()
        for change in changes:
            if not isinstance(change, dict) or set(change) - {
                "action", "type", "name", "description", "content", "memory_id"
            }:
                raise ValueError("记忆变更字段无效")
            self._fields(change.get("type"), change.get("name"), change.get("description"),
                         change.get("content"), source_thread_id, source_run_id)
            memory_id = change.get("memory_id")
            if change.get("action") == "add":
                if memory_id is not None:
                    raise ValueError("新增记忆不能指定永久 ID")
            elif change.get("action") == "update":
                self._id(memory_id)
                if memory_id in targets:
                    raise ValueError("更新记忆目标重复")
                targets.add(memory_id)
            else:
                raise ValueError("记忆变更动作无效")
        now = _aware(self._clock())
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=False) as fd:
            records = self._records(fd) if fd is not None else []
            operations: list[PreparedMemoryOperation] = []
            for change in changes:
                memory_id = change.get("memory_id")
                previous = next((r for r in records if r.id == memory_id), None)
                if memory_id is not None:
                    if previous is None or previous.type != change["type"]:
                        raise ValueError("更新目标不存在或记忆类型不匹配")
                    if (expected_versions is not None
                            and expected_versions.get(memory_id) != record_version(previous)):
                        raise MemoryConflictError("记忆已在抽取期间修改")
                equal = lambda r: (r.type, r.name, r.description, r.content) == (
                    change["type"], change["name"], change["description"], change["content"])
                if ((previous is not None and equal(previous))
                        or (memory_id is None and any(equal(r) for r in records))):
                    continue
                if previous is not None and now < previous.updated_at:
                    raise ValueError("记忆更新时间不能倒退")
                if previous is None and len(records) >= MAX_MEMORIES_PER_USER:
                    raise ValueError("用户记忆数量超限")
                identity = uuid4().hex
                record = MemoryRecord(
                    id=memory_id or f'{change["type"]}_{uuid4().hex}', type=change["type"],
                    name=change["name"], description=change["description"], content=change["content"],
                    created_at=previous.created_at if previous else now, updated_at=now,
                    source_thread_id=source_thread_id, source_run_id=source_run_id,
                    operation_id=identity, version=identity,
                )
                record = replace(record, result_digest=record_digest(record))
                self._body_bytes(record)
                operations.append(PreparedMemoryOperation(
                    operation_id=identity, memory_id=record.id,
                    expected_version=record_version(previous) if previous else None,
                    result_digest=record.result_digest, record=record,
                ))
                records = [r for r in records if r.id != record.id] + [record]
            records.sort(key=lambda r: (-r.updated_at.timestamp(), r.id))
            self._index_bytes(records, now)
            return operations

    def apply_operation(self, user_id: str, operation: PreparedMemoryOperation, *,
                        allow_write: bool = True) -> MemoryRecord | None:
        """在用户写锁内核实并应用一个已登记项，正文先提交，索引随后提交。

        已应用项必须核对身份和实际摘要并重新同步文件后才能确认成功。
        禁止写入时仅核实既有完整结果，不修复缺失索引或刷新稳定标志。
        """
        self._user(user_id)
        record = operation.record
        self._id(operation.memory_id)
        self._fields(record.type, record.name, record.description, record.content,
                     record.source_thread_id, record.source_run_id)
        if (record.id != operation.memory_id or record.operation_id != operation.operation_id
                or record.version != operation.operation_id
                or record.result_digest != operation.result_digest
                or record_digest(record) != operation.result_digest
                or not re.fullmatch(r"[0-9a-f]{32}", operation.operation_id)
                or (operation.expected_version is not None
                    and not re.fullmatch(r"[0-9a-f]{64}", operation.expected_version))
                or not record.id.startswith(record.type + "_")
                or _aware(record.created_at) > _aware(record.updated_at)):
            raise ValueError("记忆保存项身份或摘要无效")
        body = self._body_bytes(record)
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=allow_write, durable=True) as fd:
            if fd is None:
                return None
            filename = f"{record.id}.md"
            try:
                data = bounded_read(fd, filename, MAX_MEMORY_FILE_BYTES)
            except StorageLimitError as error:
                logger.warning("memory_body_corrupt category=corrupt stage=read")
                raise MemoryVerificationError() from error
            current = self._decode(filename, data)
            if data is not None and current is None:
                raise MemoryVerificationError()
            applied = (current is not None and current.operation_id == operation.operation_id
                       and current.result_digest == operation.result_digest
                       and current.version == operation.operation_id
                       and record_digest(current) == operation.result_digest
                       and (operation.expected_version is None
                            or current.created_at == record.created_at))
            if not applied:
                if ((current is None and operation.expected_version is not None)
                        or (current is not None and record_version(current) != operation.expected_version)):
                    raise MemoryConflictError("当前记忆版本与保存项不一致")
                if not allow_write:
                    return None
            body_applied = applied
            try:
                records = self._records(fd)
                if applied:
                    record = current
                else:
                    if current is None and any(
                        (item.type, item.name, item.description, item.content)
                        == (record.type, record.name, record.description, record.content)
                        for item in records
                    ):
                        # 其他已登记项可能先保存同一内容；不能凭等价内容冒认本项已提交。
                        raise MemoryConflictError("其他操作已保存等价记忆")
                    now = _aware(self._clock())
                    if now < record.updated_at:
                        raise ValueError("记忆更新时间不能倒退")
                    # 真实内容更新时间从第一次正文提交计时，不从排队或重试计时。
                    record = replace(record, created_at=current.created_at if current else now,
                                     updated_at=now)
                    body = self._body_bytes(record)
                    # 索引路径安全性及所有预算必须在正文提交前确认。
                    self._read_bytes(fd, "MEMORY.md", MAX_INDEX_BYTES)
                    if current is None and len(records) >= MAX_MEMORIES_PER_USER:
                        raise ValueError("用户记忆数量超限")
                    records = [r for r in records if r.id != record.id] + [record]
                    records.sort(key=lambda r: (-r.updated_at.timestamp(), r.id))
                    self._index_bytes(records, _aware(self._clock()))
                    self._atomic_write(fd, filename, body)
                    body_applied = True
                # 文件曾存在或 replace 返回并不独立证明持久化；核实同步后才继续。
                confirm_durable(fd, filename)
                if allow_write:
                    self._repair_index(fd, records, _aware(self._clock()))
                else:
                    metadata = self._catalog_from_index(fd, check_bodies=False)
                    expected = []
                    for item in records:
                        values = asdict(item)
                        values.pop("content")
                        expected.append(MemoryMetadata(**values))
                    if metadata != expected:
                        raise MemoryVerificationError()
                confirm_durable(fd, "MEMORY.md")
                return record
            except MemoryVerificationError:
                raise
            except StorageError as error:
                if body_applied:
                    raise MemoryVerificationError(error) from error
                raise


    def upsert(self, user_id: str, *, kind: str, name: str, description: str, content: str,
               source_thread_id: str, source_run_id: str,
               memory_id: str | None = None) -> MemoryRecord:
        """新增或显式更新记忆；等价内容去重，实际修改才重置稳定时间。

        新增 ID 由服务生成；显式更新保持原 ID 与类型，未知目标拒绝。
        正文、索引均持久化后返回；只改来源的重复抽取不改写已有记录。
        """
        self._user(user_id)
        self._fields(kind, name, description, content, source_thread_id, source_run_id)
        if memory_id is not None:
            self._id(memory_id)
        now = _aware(self._clock())
        root = self._root()
        with self._lock(root, user_id), self._directory(root, user_id, create=True) as fd:
            records = self._records(fd)
            # 在正文写入之前检查索引链接，不能覆盖不安全的已有目标。
            self._read_bytes(fd, "MEMORY.md", MAX_INDEX_BYTES)
            previous = next((r for r in records if r.id == memory_id), None)
            if memory_id is not None and (previous is None or previous.type != kind):
                raise ValueError("更新目标不存在或记忆类型不匹配")
            equal = lambda r: (r.type, r.name, r.description, r.content) == (kind, name, description, content)
            duplicate = previous if previous is not None and equal(previous) else None
            if memory_id is None:
                duplicate = next((r for r in records if equal(r)), None)
            if duplicate is not None:
                self._repair_index(fd, records, now)
                return duplicate
            if previous is not None and now < previous.updated_at:
                raise ValueError("记忆更新时间不能倒退")
            if previous is None and len(records) >= MAX_MEMORIES_PER_USER:
                raise ValueError("用户记忆数量超限")
            record = MemoryRecord(
                id=memory_id or f"{kind}_{uuid4().hex}", type=kind, name=name,
                description=description, content=content,
                created_at=previous.created_at if previous else now, updated_at=now,
                source_thread_id=source_thread_id, source_run_id=source_run_id,
            )
            identity = uuid4().hex
            record = replace(record, operation_id=identity, version=identity,
                             result_digest=record_digest(record))
            data = self._body_bytes(record)
            records = [r for r in records if r.id != record.id] + [record]
            records.sort(key=lambda r: (-r.updated_at.timestamp(), r.id))
            self._index_bytes(records, now)  # 超预算必须在写入正文之前拒绝。
            self._atomic_write(fd, f"{record.id}.md", data)
            self._repair_index(fd, records, now)
            return record
