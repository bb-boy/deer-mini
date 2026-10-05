"""Thread 文件快照对象库。

调用方须先停止写入并取得 Thread 的文件独占锁，再在线程池内调用本模块。
对象按内容寻址；这里只构建恢复目录，不交换工作目录或修改数据库。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import uuid4

from app.filesystem.thread_paths import ThreadPaths


_ROOTS = ("outputs", "uploads", "workspace")
_CHUNK_SIZE = 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PREFIX = re.compile(r"[0-9a-f]{2}\Z")
_TEMPORARY = re.compile(r"\.snapshot-[0-9a-f]{32}\.tmp\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def manifest_bytes(manifest: dict) -> bytes:
    """返回清单的规范 UTF-8 JSON，供持久化、容量计算和指纹使用。"""
    try:
        return json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("快照清单不是有效的 JSON") from error


def fingerprint(manifest: dict) -> str:
    return hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _regular(info: os.stat_result, *, private: bool = False) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("文件快照不支持符号链接、硬链接或特殊文件")
    if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("快照对象的所有者或权限不安全")


@contextmanager
def _child_directory(parent: int, name: str, *, create: bool = False, private: bool = False) -> Iterator[int]:
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        if private:
            if created:
                os.fchmod(descriptor, 0o700)
            elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("快照目录的所有者或权限不安全")
        if created:
            os.fsync(descriptor)
            os.fsync(parent)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _absolute_directory(path: Path) -> Iterator[int]:
    """逐级使用目录描述符打开绝对路径，拒绝任何中间符号链接。"""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("快照目录必须是安全的绝对路径")
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for name in path.parts[1:]:
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _relative_directory(parent: int, parts: tuple[str, ...]) -> Iterator[int]:
    descriptor = os.dup(parent)
    try:
        for name in parts:
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _valid_path(raw_path: object) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError("快照清单包含无效路径")
    parts = tuple(raw_path.split("/"))
    if parts[0] not in _ROOTS or any(part in ("", ".", "..", ".checkpoints") for part in parts):
        raise ValueError("快照清单路径超出文件区域")
    if parts[:2] == ("workspace", ".tool-results"):
        raise ValueError("快照不能覆盖历史工具结果目录")
    try:
        raw_path.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("快照路径不是有效的 UTF-8") from error
    return parts


class FileSnapshotStore:
    def __init__(
        self,
        paths: ThreadPaths,
        *,
        max_entries: int = 50_000,
        max_manifest_bytes: int = 16 * 1024**2,
        max_bytes: int = 1024**3,
    ) -> None:
        for limit in (max_entries, max_manifest_bytes, max_bytes):
            if type(limit) is not int or limit < 0:
                raise ValueError("快照容量限制必须是非负整数")
        self.paths = paths
        self.root = paths.thread_dir / ".checkpoints"
        self.max_entries = max_entries
        self.max_manifest_bytes = max_manifest_bytes
        self.max_bytes = max_bytes

    def object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("快照对象摘要无效")
        return self.root / "objects" / digest[:2] / digest[2:]

    @contextmanager
    def _objects(self, *, create: bool = False) -> Iterator[int]:
        with _absolute_directory(self.paths.thread_dir) as thread:
            with _child_directory(thread, ".checkpoints", create=create, private=True) as backup:
                with _child_directory(backup, "objects", create=create, private=True) as objects:
                    yield objects

    def _inventory(self, objects: int) -> tuple[dict[str, os.stat_result], dict[str, os.stat_result]]:
        inventory: dict[str, os.stat_result] = {}
        temporary: dict[str, os.stat_result] = {}
        for prefix in os.listdir(objects):
            info = os.stat(prefix, dir_fd=objects, follow_symlinks=False)
            if _TEMPORARY.fullmatch(prefix):
                _regular(info, private=True)
                temporary[prefix] = info
                continue
            if not _PREFIX.fullmatch(prefix) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("快照对象库包含不安全的条目")
            with _child_directory(objects, prefix, private=True) as directory:
                for suffix in os.listdir(directory):
                    digest = prefix + suffix
                    if not _DIGEST.fullmatch(digest):
                        raise ValueError("快照对象库包含无效名称")
                    info = os.stat(suffix, dir_fd=directory, follow_symlinks=False)
                    _regular(info, private=True)
                    inventory[digest] = info
        return inventory, temporary

    def _check_capacity(self, object_bytes: int, manifest_size: int) -> None:
        if manifest_size > self.max_manifest_bytes:
            raise ValueError("文件快照清单超过容量限制")
        if object_bytes + manifest_size > self.max_bytes:
            raise ValueError("文件快照超过备份容量限制")

    def _walk(
        self, file_callback: Callable[[int, str, str, os.stat_result], dict] | None
    ) -> tuple[list[dict], dict[str, tuple[int, ...]]]:
        entries: list[dict] = []
        signatures: dict[str, tuple[int, ...]] = {}
        manifest_size = len(manifest_bytes({"schema_version": 1, "entries": []}))

        def record(entry: dict, info: os.stat_result) -> None:
            nonlocal manifest_size
            if len(entries) >= self.max_entries:
                raise ValueError("文件快照条目数量超过限制")
            # 条目的规范编码与外层标点分别累计，避免反复编码整份清单。
            manifest_size += len(manifest_bytes(entry)) + bool(entries)
            if manifest_size > self.max_manifest_bytes:
                raise ValueError("文件快照清单超过容量限制")
            entries.append(entry)
            signatures[entry["path"]] = _signature(info)

        def visit(parent: int, name: str, relative: str) -> None:
            if len(entries) >= self.max_entries:
                raise ValueError("文件快照条目数量超过限制")
            _valid_path(relative)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with _child_directory(parent, name) as directory:
                    if _signature(os.fstat(directory)) != _signature(info):
                        raise ValueError("文件目录在快照扫描期间发生变化")
                    record({"path": relative, "kind": "directory", "mode": info.st_mode & 0o777}, info)
                    for child in sorted(os.listdir(directory)):
                        child_path = relative + "/" + child
                        if child_path == "workspace/.tool-results":
                            # 历史结果不进入清单，但其入口仍不可为文件或链接。
                            with _child_directory(directory, child):
                                pass
                            continue
                        visit(directory, child, child_path)
                    if _signature(os.fstat(directory)) != _signature(info):
                        raise ValueError("文件目录在快照扫描期间发生变化")
            else:
                _regular(info)
                entry = {"path": relative, "kind": "file", "mode": info.st_mode & 0o777}
                if file_callback is not None:
                    entry.update(file_callback(parent, name, relative, info))
                record(entry, info)

        with _absolute_directory(self.paths.thread_dir) as thread:
            for name in _ROOTS:
                visit(thread, name, name)
        entries.sort(key=lambda entry: entry["path"])
        return entries, signatures

    def _read_file(
        self, parent: int, name: str, expected: os.stat_result, write: Callable[[bytes], object] | None = None,
        *, private: bool = False,
    ) -> tuple[str, int]:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            _regular(before, private=private)
            if _signature(before) != _signature(expected):
                raise ValueError("文件在快照读取之前发生变化")
            if before.st_size > self.max_bytes:
                raise ValueError("文件快照超过备份容量限制")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, _CHUNK_SIZE):
                size += len(chunk)
                if size > self.max_bytes:
                    raise ValueError("文件快照超过备份容量限制")
                digest.update(chunk)
                if write is not None:
                    write(chunk)
            after = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if size != before.st_size or _signature(before) != _signature(after) or _signature(before) != _signature(named):
                raise ValueError("文件在快照读取期间发生变化")
            return digest.hexdigest(), size
        finally:
            os.close(descriptor)

    def _verify_object(self, objects: int, digest: str, size: int, write: Callable[[bytes], object] | None = None) -> None:
        with _child_directory(objects, digest[:2], private=True) as prefix:
            info = os.stat(digest[2:], dir_fd=prefix, follow_symlinks=False)
            _regular(info, private=True)
            if info.st_size != size:
                raise ValueError("快照内容对象已损坏")
            actual_digest, actual_size = self._read_file(prefix, digest[2:], info, write, private=True)
            if (actual_digest, actual_size) != (digest, size):
                raise ValueError("快照内容对象已损坏")

    def capture(self, *, store_objects: bool = True) -> dict:
        """完整读取三个区域并返回清单；可选落盘对象，检测并拒绝扫描期间的变化。

        容量只计算本清单及其唯一内容；服务层选定保留点后检查归档总量。
        只读扫描不创建对象库，可用作预览的文件指纹。
        """
        if not store_objects:
            return self._capture(None)
        with self._objects(create=True) as objects:
            return self._capture(objects)

    def _capture(self, objects: int | None) -> dict:
        inventory = self._inventory(objects)[0] if objects is not None else {}
        total_bytes = 0
        current: dict[str, int] = {}

        def capture_file(parent: int, name: str, relative: str, info: os.stat_result) -> dict:
            nonlocal total_bytes
            # ThreadPaths 是路径授权入口；实际读取使用已打开的父目录描述符。
            self.paths.resolve_agent_path(relative)
            temporary = ".snapshot-" + uuid4().hex + ".tmp"
            try:
                if objects is None:
                    digest, size = self._read_file(parent, name, info)
                else:
                    # 即使最终去重，也需先容纳当前文件的临时副本；实际 ENOSPC 仍向上传播。
                    space = os.fstatvfs(objects)
                    if space.f_bavail * space.f_frsize < info.st_size + 1024 * 1024:
                        raise OSError("文件备份临时磁盘空间不足")
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=objects)
                    with os.fdopen(descriptor, "wb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        digest, size = self._read_file(parent, name, info, output.write)
                        output.flush()
                        os.fsync(output.fileno())
                if digest not in current:
                    total_bytes += size
                    self._check_capacity(total_bytes, 0)
                    current[digest] = size
                if objects is not None:
                    if digest in inventory:
                        self._verify_object(objects, digest, size)
                    else:
                        with _child_directory(objects, digest[:2], create=True, private=True) as prefix:
                            # 新对象以 fsync 后的独立文件原子安装，绝不链接工作文件。
                            try:
                                os.stat(digest[2:], dir_fd=prefix, follow_symlinks=False)
                            except FileNotFoundError:
                                os.replace(temporary, digest[2:], src_dir_fd=objects, dst_dir_fd=prefix)
                                os.fsync(prefix)
                                os.fsync(objects)
                            else:
                                self._verify_object(objects, digest, size)
                            inventory[digest] = os.stat(digest[2:], dir_fd=prefix, follow_symlinks=False)
                return {"sha256": digest, "size": size}
            finally:
                if objects is not None:
                    try:
                        os.unlink(temporary, dir_fd=objects)
                    except FileNotFoundError:
                        pass

        entries, signatures = self._walk(capture_file)
        # 再次遍历全部条目，检测先前读取文件的后续变化及新增/删除/替换。
        _, final_signatures = self._walk(None)
        if signatures != final_signatures:
            raise ValueError("文件树在快照扫描期间发生变化")
        manifest = {"schema_version": 1, "entries": entries}
        self._check_capacity(total_bytes, len(manifest_bytes(manifest)))
        self.validate(manifest, verify_objects=False)
        return manifest

    def validate(self, manifest: dict, *, verify_objects: bool = True) -> None:
        """验证清单结构、边界、容量和可选的全部对象哈希；不修复损坏数据。"""
        if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "entries"}:
            raise ValueError("文件快照清单结构无效")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise ValueError("不支持的文件快照版本")
        entries = manifest["entries"]
        if not isinstance(entries, list) or len(entries) > self.max_entries:
            raise ValueError("文件快照条目数量超过限制或结构无效")
        manifest_size = len(manifest_bytes({"schema_version": 1, "entries": []}))
        seen: dict[str, str] = {}
        unique: dict[str, int] = {}
        total_bytes = 0
        previous = ""
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("文件快照条目结构无效")
            parts = _valid_path(entry.get("path"))
            path = entry["path"]
            kind = entry.get("kind")
            required = {"path", "kind", "mode"}
            if kind == "file":
                required |= {"sha256", "size"}
            elif kind != "directory":
                raise ValueError("文件快照条目类型无效")
            if set(entry) != required or type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o777:
                raise ValueError("文件快照条目权限或字段无效")
            if path <= previous:
                raise ValueError("文件快照条目必须唯一且按路径排序")
            if len(parts) == 1:
                if kind != "directory":
                    raise ValueError("文件快照根目录不能被文件覆盖")
            elif seen.get("/".join(parts[:-1])) != "directory":
                raise ValueError("文件快照条目缺少父目录")
            previous = path
            manifest_size += len(manifest_bytes(entry)) + bool(seen)
            seen[path] = kind
            if kind == "file":
                digest, size = entry["sha256"], entry["size"]
                self.object_path(digest)
                if type(size) is not int or size < 0:
                    raise ValueError("文件快照字节数无效")
                if digest in unique and unique[digest] != size:
                    raise ValueError("同一快照对象的字节数不一致")
                if digest not in unique:
                    unique[digest] = size
                    total_bytes += size
            self._check_capacity(total_bytes, manifest_size)
        if not all(seen.get(root) == "directory" for root in _ROOTS):
            raise ValueError("文件快照必须包含三个根目录")
        if verify_objects and unique:
            with self._objects() as objects:
                for digest, size in unique.items():
                    self._verify_object(objects, digest, size)

    def materialize(self, manifest: dict, destination: Path) -> None:
        """将已验证对象复制到安全的空目录，保留普通权限和空目录。

        不触碰现有工作目录。失败时可能留下部分 staging 内容，交由服务层恢复协议处理。
        """
        self.validate(manifest)
        if not destination.is_absolute() or ".." in destination.parts:
            raise ValueError("恢复目标必须是安全的绝对路径")
        for protected in (self.paths.uploads_path, self.paths.workspace_path, self.paths.outputs_path, self.root / "objects"):
            if destination == protected or destination.is_relative_to(protected) or protected.is_relative_to(destination):
                raise ValueError("恢复目标不能覆盖工作目录或对象库")
        with _absolute_directory(destination.parent) as parent:
            with _child_directory(parent, destination.name, create=True) as target:
                if os.listdir(target):
                    raise ValueError("文件快照恢复目标必须为空目录")
                entries = manifest["entries"]
                has_files = any(entry["kind"] == "file" for entry in entries)
                with self._materialize_objects(has_files) as objects:
                    for entry in entries:
                        parts = PurePosixPath(entry["path"]).parts
                        with _relative_directory(target, parts[:-1]) as directory:
                            if entry["kind"] == "directory":
                                os.mkdir(parts[-1], mode=0o700, dir_fd=directory)
                            else:
                                assert objects is not None
                                descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                                with os.fdopen(descriptor, "wb") as output:
                                    self._verify_object(objects, entry["sha256"], entry["size"], output.write)
                                    output.flush()
                                    os.fchmod(output.fileno(), entry["mode"])
                                    os.fsync(output.fileno())
                            os.fsync(directory)
                # 子目录先同步并设置权限；父目录最后设置，允许恢复只读/无权限目录。
                for entry in reversed(entries):
                    if entry["kind"] == "directory":
                        with _relative_directory(target, PurePosixPath(entry["path"]).parts) as directory:
                            os.fchmod(directory, entry["mode"])
                            os.fsync(directory)
                os.fsync(target)
                os.fsync(parent)

    @contextmanager
    def _materialize_objects(self, has_files: bool) -> Iterator[int | None]:
        if has_files:
            with self._objects() as objects:
                yield objects
        else:
            yield None

    def collect(self, manifests: list[dict]) -> None:
        """只回收对象库中未引用的对象和本模块临时文件，保留工作文件与恢复 staging。"""
        keep: set[str] = set()
        for manifest in manifests:
            self.validate(manifest, verify_objects=False)
            keep.update(entry["sha256"] for entry in manifest["entries"] if entry["kind"] == "file")
        try:
            with self._objects() as objects:
                inventory, temporary = self._inventory(objects)
                # 先验证整棵对象树，再开始删除；不递归删除任何任意路径。
                for digest, expected in inventory.items():
                    if digest in keep:
                        continue
                    with _child_directory(objects, digest[:2], private=True) as prefix:
                        actual = os.stat(digest[2:], dir_fd=prefix, follow_symlinks=False)
                        if _signature(actual) != _signature(expected):
                            raise ValueError("快照对象在回收期间发生变化")
                        _regular(actual, private=True)
                        os.unlink(digest[2:], dir_fd=prefix)
                        os.fsync(prefix)
                for name, expected in temporary.items():
                    actual = os.stat(name, dir_fd=objects, follow_symlinks=False)
                    if _signature(actual) != _signature(expected):
                        raise ValueError("快照临时文件在回收期间发生变化")
                    _regular(actual, private=True)
                    os.unlink(name, dir_fd=objects)
                os.fsync(objects)
        except FileNotFoundError:
            # 对象库尚未创建时无需清理；存在库但对象消失则必须报告竞争。
            if self.root.exists() or self.root.is_symlink():
                raise
