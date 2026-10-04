"""将用户记忆持久化为 Markdown 正文，并从正文重建元数据索引。

正文是唯一事实来源；索引不含正文，不用于直接渲染模型上下文。
当前单进程内以用户级锁串行读写，通过目录描述符拒绝符号链接与路径穿越。
"""

import errno
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


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
    def _directory(self, root: Path, user_id: str, *, create: bool) -> Iterator[int | None]:
        """逐级以 O_NOFOLLOW 打开目录；后续读写始终相对此描述符。

        不跟随用户目录、记忆目录或根目录祖先的链接；缺失只读目录返回 None。
        即使目录被并发替换，也不会把后续操作重定向到替换后的目录。
        """
        fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for segment in (*root.parts[1:], user_id, "memories"):
                try:
                    child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    try:
                        os.mkdir(segment, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("记忆目录不允许链接或非目录节点") from None
            raise
        finally:
            os.close(fd)

    @staticmethod
    def _read_bytes(fd: int, name: str, limit: int) -> bytes | None:
        """拒绝链接与特殊文件，仅从普通文件有界读取；超限视为无效文件。"""
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("记忆文件不允许符号链接") from None
            raise
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("记忆文件必须是无硬链接的普通文件")
            if info.st_size > limit:
                return None
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                part = os.read(file_fd, min(remaining, 65536))
                if not part:
                    break
                chunks.append(part)
                remaining -= len(part)
            data = b"".join(chunks)
            return data if len(data) <= limit else None
        finally:
            os.close(file_fd)

    def _decode(self, name: str, data: bytes | None) -> MemoryRecord | None:
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
            if metadata.keys() != expected or any(not isinstance(v, str) for v in metadata.values()):
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

    def _records(self, fd: int) -> list[MemoryRecord]:
        records = []
        for name in sorted(os.listdir(fd)):
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
        """先 fsync 私有临时文件，再替换目标并 fsync 目录；异常继续传播。"""
        try:
            target = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            target = None
        if target is not None and not stat.S_ISREG(target.st_mode):
            raise ValueError("记忆目标必须是普通文件")
        temporary = f".{uuid4().hex}.tmp"
        file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          mode=0o600, dir_fd=fd)
        try:
            with os.fdopen(file_fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass

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

    def _catalog_from_index(self, fd: int) -> list[MemoryMetadata] | None:
        """仅打开索引；通过文件集合与修改时间判断是否需要从正文重建。

        健康路径只 stat 正文文件，不读取其内容；链接与特殊节点仍立即拒绝。
        索引损坏返回 None，交由持有同一用户锁的 catalog 重建。
        """
        data = self._read_bytes(fd, "MEMORY.md", MAX_INDEX_BYTES)
        if data is None:
            return None
        try:
            index_stat = os.stat("MEMORY.md", dir_fd=fd, follow_symlinks=False)
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
                if (not isinstance(item, dict) or item.keys() != expected
                        or not isinstance(item["stable"], bool)
                        or any(not isinstance(v, str) for k, v in item.items() if k != "stable")):
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
        # 文件安全检查放在解析异常处理之外，危险节点始终向调用者报告。
        body_ids: set[str] = set()
        changed = False
        for filename in os.listdir(fd):
            if filename == "MEMORY.md" or not filename.endswith(".md"):
                continue
            info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
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
            records = [r for r in records if r.id != record.id] + [record]
            records.sort(key=lambda r: (-r.updated_at.timestamp(), r.id))
            self._index_bytes(records, now)  # 超预算必须在写入正文之前拒绝。
            self._atomic_write(fd, f"{record.id}.md", data)
            self._repair_index(fd, records, now)
            return record
