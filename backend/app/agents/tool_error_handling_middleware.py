"""把可反馈给模型的工具异常转成工具消息，保留运行中断信号。"""

from collections.abc import Awaitable, Callable

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError


class ToolErrorHandlingMiddleware(AgentMiddleware):
    async def wrap_tool_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        tool_call: ToolCall,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        try:
            return await call_next()
        except StatePersistenceError:
            # 无法保存状态时，Runtime 必须停止；不能伪装成可恢复的工具错误。
            raise
        except Exception as error:
            # CancelledError 继承 BaseException，不会进入此分支。
            return Message(
                role="tool",
                content=f"执行工具 {tool_call.name} 时出错：{error}。",
                tool_call_id=tool_call.id,
                is_error=True,
            )
