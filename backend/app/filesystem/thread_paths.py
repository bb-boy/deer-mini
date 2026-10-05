from pathlib import Path, PurePosixPath


# 模型和容器统一使用的目录名称。
VIRTUAL_ROOT = PurePosixPath("/mnt/user-data")
VIRTUAL_WORKSPACE = str(VIRTUAL_ROOT / "workspace")


class ThreadPaths:
    def __init__(self, thread_dir: Path) -> None:
        if not thread_dir.is_absolute():
            raise ValueError("Thread 目录必须是服务端提供的绝对路径")

        # 在 resolve 消除链接信息之前检查 Thread 本身及其祖先。
        if ".." in thread_dir.parts or any(path.is_symlink() for path in (thread_dir, *thread_dir.parents)):
            raise ValueError("Thread 目录及其祖先不能是符号链接")

        # 真实服务器目录，来自当前 Thread。
        self.thread_dir = thread_dir.resolve()
        self.workspace_path = self.thread_dir / "workspace"
        self.uploads_path = self.thread_dir / "uploads"
        self.outputs_path = self.thread_dir / "outputs"

        self._roots = {
            "workspace": self.workspace_path,
            "uploads": self.uploads_path,
            "outputs": self.outputs_path,
        }

        # 标准目录本身不能被替换成指向其他位置的符号链接。
        for root in self._roots.values():
            if root.resolve() != root:
                raise ValueError("Thread 的标准目录不能是符号链接")

    def mount_pairs(self) -> list[tuple[Path, str]]:
        """生成挂载对应关系；实际挂载由 DockerCommandRunner 完成。"""
        return [
            (host_path, str(VIRTUAL_ROOT / name))
            for name, host_path in self._roots.items()
        ]

    def resolve_agent_path(self, raw_path: str) -> Path:
        """把模型文件路径转换为当前 Thread 内的真实路径。"""
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("文件路径不能为空")

        if "\x00" in raw_path or "\\" in raw_path:
            raise ValueError("文件路径包含不支持的字符")

        # 先按 Linux 路径格式分析模型的地址，不直接访问这个地址。
        requested = PurePosixPath(raw_path)
        if ".." in requested.parts:
            raise ValueError("文件路径超出了当前 Thread 的指定目录：不允许 ..")

        if requested.is_absolute():
            try:
                relative = requested.relative_to(VIRTUAL_ROOT)
            except ValueError as error:
                raise ValueError(
                    "绝对路径必须位于 /mnt/user-data 下"
                ) from error

            # 去掉虚拟根目录后，第一级必须是三个标准目录之一。
            if not relative.parts or relative.parts[0] not in self._roots:
                raise ValueError(
                    "只能访问 workspace、uploads、outputs 三个目录"
                )

            name = relative.parts[0]
            remaining = relative.relative_to(name)
        elif requested.parts and requested.parts[0] in self._roots:
            # uploads/资料.txt 明确指向附件区，不再在多个目录里猜测。
            name = requested.parts[0]
            remaining = requested.relative_to(name)
        else:
            # 兼容原来的 read_file("report.txt")。
            name = "workspace"
            remaining = requested

        base = self._roots[name]

        # 每次使用时重新检查；标准目录和中间目录也不能是符号链接。
        current = base
        for part in ("", *remaining.parts):
            current = current / part
            if current.is_symlink():
                raise ValueError("文件路径超出了当前 Thread 的指定目录：不允许符号链接")

        # 解析 .. 和已有符号链接，再检查最终位置。
        target = (base / remaining).resolve()
        if not target.is_relative_to(base):
            raise ValueError("文件路径超出了当前 Thread 的指定目录")

        return target
