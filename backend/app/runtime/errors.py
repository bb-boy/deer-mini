"""区分必须终止 Run 的关键状态故障和模型可以处理的普通工具错误。"""


class StatePersistenceError(RuntimeError):
    """关键状态未能确认保存，不能把它当作工具文字后继续宣告 Run 成功。

    输入：message 说明哪个关键状态保存失败；原数据库异常保留在 __cause__。
    execution_error 可保留保存失败之前的模型异常或取消原因。
    输出：交给父 Runtime 的异常。副作用：无，不自行写库或发送事件。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.execution_error: BaseException | None = None
