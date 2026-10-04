"""复用 DeerFlow 的清单提示和过早结束提醒，接到 Mini 自定义 Agent Loop。

功能：模型能看见规划规则；当前 Run 的计划没完成时，最多提醒继续两次。
输入：已注册的工具，以及每轮的父状态、运行上下文和待发送模型的消息。
输出：补有原版系统提示/临时提醒的请求副本，或请求循环再执行一轮。
副作用：只绑定工具和维护内存计数；提醒不写入用户消息、Checkpoint 或 SSE。
"""

from dataclasses import replace

from app.agents.middleware import AgentMiddleware
from app.agents.prompts.todo_list import TODO_SYSTEM_PROMPT, format_completion_reminder
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.tools.write_todos import WriteTodosTool


class TodoMiddleware(AgentMiddleware):
    MAX_COMPLETION_REMINDERS = 2

    def __init__(self, tool: WriteTodosTool) -> None:
        self._tool = tool
        self._reminder_count = 0
        self._pending_reminder: str | None = None

    async def before_agent(self, state: ThreadState, context: RuntimeContext) -> None:
        self._tool.bind(state, context)
        self._reminder_count = 0
        self._pending_reminder = None

    async def prepare_model_messages(
        self, state: ThreadState, context: RuntimeContext, messages: list[Message],
    ) -> list[Message]:
        """只修改本次请求副本；实际用户历史中不会出现伪造的追问。"""
        prepared = list(messages)
        for index, message in enumerate(prepared):
            if message.role == "system":
                prepared[index] = replace(message, content=message.content + "\n\n" + TODO_SYSTEM_PROMPT)
                break
        else:
            prepared.insert(0, Message(role="system", content=TODO_SYSTEM_PROMPT))
        if self._pending_reminder is not None:
            prepared.append(Message(role="user", content=self._pending_reminder))
            self._pending_reminder = None
        return prepared

    async def after_model(
        self, state: ThreadState, context: RuntimeContext, message: Message,
    ) -> bool:
        # 上一轮 Run 的遗留清单仍可查看，但不能让一个新的简单问题被迫继续旧工作。
        if message.role != "assistant" or message.tool_calls or state.todos_run_id != context.run_id:
            return False
        if not state.todos or all(todo.status == "completed" for todo in state.todos):
            return False
        if self._reminder_count >= self.MAX_COMPLETION_REMINDERS:
            return False
        self._pending_reminder = format_completion_reminder([todo.to_dict() for todo in state.todos])
        self._reminder_count += 1
        return True

    async def after_agent(
        self, state: ThreadState, context: RuntimeContext, error: BaseException | None,
    ) -> None:
        self._pending_reminder = None
