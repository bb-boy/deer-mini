"""
每个 Thread 准备 workspace、uploads、outputs 三个目录

"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from app.domain.common import new_id
from app.domain.threads import Thread
from app.repositories.thread_repository import ThreadRepository


logger = logging.getLogger(__name__)
_DELETING_PREFIX = ".deleting-"

#一个用户目录
DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "users" # resolve()返回绝对路径，parents[2]返回到第三个父目录。__file__是当前文件的路径，PATH把她变为一个PATH对象方便操作，


def resolve_data_root() -> Path:
    """返回 Thread 文件根目录；环境变量只接受绝对路径。"""
    configured = os.getenv("DEER_MINI_DATA_ROOT")
    if not configured:
        return DATA_ROOT
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("DEER_MINI_DATA_ROOT 必须是绝对路径")
    return path.resolve()


class ThreadService:

    def __init__(self,threadrepo: ThreadRepository | None = None) -> None:
        self._threadrepo = threadrepo or ThreadRepository() #

    def create_thread(self, user_id: str, title: str | None = None) -> Thread:
        """
        创建一个新的 Thread 对象，并在数据库中存储
        :param user_id: 用户 ID
        :param title: 线程标题
        :return: 创建的 Thread 对象
        """

        thread_id = new_id()     #生成一个会话id
        self._validate_path_segment(user_id,"user_id")   #检查下是否是非法用户id
        thread_dir = resolve_data_root() / user_id / "threads" / thread_id #每个用户会话的工作目录

        workspace_path = thread_dir / "workspace"
        uploads_path = thread_dir / "uploads"
        outputs_path = thread_dir / "outputs"

        # 创建目录
        for path in [workspace_path, uploads_path, outputs_path]:
            path.mkdir(parents=True, exist_ok=False) #parents=True表示如果父目录不存在就创建，exist_ok=False要创建的文件就报错
        thread = Thread(
            id=thread_id,
            user_id=user_id,
            workspace_path=str(workspace_path),
            title=title,
        )

        try:
            self._threadrepo.create(thread)  #将thread对象存储到数据库中
        except Exception:
         # 如果数据库操作失败，删除已创建的目录
            shutil.rmtree(thread_dir)
            raise

        return thread

    def rename_thread(self, thread_id: str, user_id: str, title: str) -> Thread:
        """更新 Thread 标题。"""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Thread 标题不能为空")
        if len(clean_title) > 200:
            raise ValueError("Thread 标题不能超过 200 个字符")
        existing = self._threadrepo.get(thread_id, user_id)
        if existing is None:
            raise ValueError("Thread 不存在")
        if existing.status == "running":
            raise RuntimeError("运行中的 Thread 不能重命名")
        thread = self._threadrepo.update_title(thread_id, user_id, clean_title)
        if thread is None:
            raise ValueError("Thread 不存在")
        return thread

    async def delete_thread(self, thread_id: str, user_id: str) -> Thread:
        """删除 Thread 记录，并在工作线程中清理它的文件目录。"""
        self._validate_path_segment(user_id, "user_id")
        existing = self._threadrepo.get(thread_id, user_id)
        if existing is None:
            raise ValueError("Thread 不存在")
        if existing.status == "running":
            raise RuntimeError("运行中的 Thread 不能删除")
        thread_dir = Path(existing.workspace_path).parent
        if thread_dir.is_symlink():
            raise ValueError("Thread 工作目录不能是符号链接")
        thread_root = (resolve_data_root() / user_id / "threads").resolve()
        resolved_dir = thread_dir.resolve()
        if thread_root not in resolved_dir.parents:
            raise ValueError("Thread 工作目录不在受管理的路径内")

        # 先在同一文件系统内原子改名：数据库删除失败时可以把目录恢复；
        # 数据库删除成功后，外部请求也不会再看到半删除的 Workspace。
        # Thread ID 由 new_id() 生成，不包含句点；句点用于让启动恢复可靠地
        # 从右侧分开 Thread ID 与本次删除操作 ID。
        deleting_dir = thread_root / f"{_DELETING_PREFIX}{thread_id}.{new_id()}"
        if thread_dir.exists():
            thread_dir.replace(deleting_dir)
        try:
            thread = self._threadrepo.delete(thread_id, user_id)
            if thread is None:
                raise ValueError("Thread 不存在")
        except BaseException:
            if deleting_dir.exists() and not thread_dir.exists():
                deleting_dir.replace(thread_dir)
            raise

        if deleting_dir.exists():
            try:
                # 大目录删除可能很慢，不能阻塞 FastAPI 的事件循环。
                await asyncio.to_thread(shutil.rmtree, deleting_dir)
            except OSError:
                # Thread 已完成语义上的删除；保留带标记的目录供下次启动重试，
                # 避免向客户端报告一个实际上已经成功的删除操作。
                logger.exception("Thread 目录暂未清理，将在下次启动重试：%s", deleting_dir)
        return thread



    def _validate_path_segment(self, value: str, field_name: str) -> None:
        """
        验证路径段是否合法，防止目录遍历攻击
        :param value: 要检查的内容
        :param field_name: 要检查的字段，用于错误提示
        data/users/../threads/...这种就是不对的
        :return: None
        """
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError(f"{field_name} 不能包含路径分隔符或 '..'")


async def cleanup_pending_thread_deletions(
    threadrepo: ThreadRepository | None = None,
) -> None:
    """恢复中断的删除操作：有数据库记录就还原，否则清理目录。"""
    data_root = resolve_data_root()
    if not data_root.exists():
        return
    repository = threadrepo or ThreadRepository()
    pending = [
        path
        for path in data_root.glob(f"*/threads/{_DELETING_PREFIX}*")
        if path.is_dir() and not path.is_symlink()
    ]
    for path in pending:
        payload = path.name.removeprefix(_DELETING_PREFIX)
        thread_id, separator, operation_id = payload.rpartition(".")
        if not separator or not thread_id or not operation_id:
            # 旧版或损坏的标记无法安全对应数据库记录，宁可保留也不能误删。
            logger.warning("跳过无法识别的 Thread 删除标记：%s", path)
            continue
        user_id = path.parent.parent.name
        try:
            existing = repository.get(thread_id, user_id)
        except Exception:
            logger.exception("无法确认删除标记对应的 Thread，暂时保留：%s", path)
            continue

        if existing is not None:
            expected_dir = path.parent / thread_id
            recorded_dir = Path(existing.workspace_path).parent
            if recorded_dir.resolve() != expected_dir.resolve():
                logger.error("Thread 记录的 Workspace 与删除标记不匹配：%s", path)
                continue
            if expected_dir.exists():
                logger.error("Thread 原目录与删除标记同时存在，暂不处理：%s", path)
                continue
            path.replace(expected_dir)
            logger.warning("已恢复中断删除的 Thread 目录：%s", expected_dir)
            continue

        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except OSError:
            logger.exception("无法清理遗留的 Thread 目录：%s", path)
