"""单进程 Thread 操作准入；所有调用发生在同一事件循环，无等待锁导致的取消死锁。"""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class _Entry:
    run_id: str | None = None
    writer: str | None = None
    readers: int = 0
    blocked: bool = False


class ThreadOperations:
    def __init__(self) -> None:
        self._entries: dict[tuple[str,str],_Entry] = {}

    def _entry(self, user_id: str, thread_id: str) -> _Entry:
        return self._entries.setdefault((user_id,thread_id),_Entry())

    def block(self, user_id: str, thread_id: str) -> None:
        self._entry(user_id,thread_id).blocked = True

    @contextmanager
    def write(self, user_id: str, thread_id: str, kind: str, *, allow_run: bool = False) -> Iterator[None]:
        entry = self._entry(user_id,thread_id)
        if entry.blocked:
            raise RuntimeError('对话文件状态需要恢复处理，暂不能执行或修改')
        if entry.writer or entry.readers or (entry.run_id and not allow_run):
            raise RuntimeError('对话正在执行、传输文件或恢复，请等待结束后重试')
        entry.writer = kind
        try:
            yield
        finally:
            entry.writer = None

    def register_run(self, user_id: str, thread_id: str, run_id: str) -> None:
        self._entry(user_id,thread_id).run_id = run_id

    def release_run(self, user_id: str, thread_id: str, run_id: str) -> None:
        entry = self._entry(user_id,thread_id)
        if entry.run_id == run_id:
            entry.run_id = None

    def read(self, user_id: str, thread_id: str) -> Callable[[],None]:
        entry = self._entry(user_id,thread_id)
        if entry.blocked or entry.writer:
            raise RuntimeError('对话正在恢复或修改文件，请稍后重试')
        entry.readers += 1
        released = False
        def release() -> None:
            nonlocal released
            if not released:
                released = True
                entry.readers -= 1
        return release
