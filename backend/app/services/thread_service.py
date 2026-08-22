"""
每个 Thread 准备 workspace、uploads、outputs 三个目录

"""

from concurrent.futures import thread
import shutil
from pathlib import Path
from app.domain.common import new_id
from app.domain.threads import Thread, ThreadState 
from app.repositories.thread_repository import ThreadRepository


#一个用户目录
DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "users" # resolve()返回绝对路径，parents[2]返回到第三个父目录。__file__是当前文件的路径，PATH把她变为一个PATH对象方便操作，


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
        thread_dir = DATA_ROOT / user_id / "threads" / thread_id #每个用户会话的工作目录

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