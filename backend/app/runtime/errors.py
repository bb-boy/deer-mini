"""区分必须终止 Run 的关键状态故障和模型可以处理的普通工具错误。"""


import logging
import sys

from app.storage.errors import StorageError


class StatePersistenceError(RuntimeError):
    """关键状态未能确认保存，不能把它当作工具文字后继续宣告 Run 成功。

    输入：message 说明哪个关键状态保存失败；原数据库异常保留在 __cause__。
    execution_error 可保留保存失败之前的模型异常或取消原因。
    输出：交给父 Runtime 的异常。副作用：无，不自行写库或发送事件。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.execution_error: BaseException | None = None


def log_runtime_exception(logger: logging.Logger, message: str, *args: object,
                          error: BaseException | None = None,
                          level: int = logging.ERROR) -> None:
    """存储错误只记录安全字段，其他错误保留既有 traceback 诊断。

    输入日志上下文及异常；未提供异常时使用当前 except 的异常。关键状态包装
    或随后发生的清理错误可能通过 cause/context 携带存储错误。仅含存储错误
    的链条脱敏；没有存储错误的普通异常保留 traceback，不修改或消费原异常。
    """
    error = error if error is not None else sys.exception()
    storage_error: StorageError | None = None
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, StorageError):
            storage_error = current
            break
        current = (current.__cause__ if current.__cause__ is not None
                   else current.__context__ if not current.__suppress_context__ else None)
    if storage_error is not None:
        logger.log(level, message + " storage=%s", *args, storage_error.safe_fields())
    else:
        logger.log(level, message, *args,
                   exc_info=(type(error), error, error.__traceback__) if error is not None else None)
