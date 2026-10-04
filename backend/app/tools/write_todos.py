"""让主 Agent 保存完整任务清单；工具只记进度，不替模型执行清单中的工作。

输入：call.arguments.todos 是本次替换后的全部项目；context 提供当前用户、
对话、Run、工作目录和受控的保存/通知入口。归属编号不能由模型指定。
输出：带原工具调用编号的已保存结果。副作用：写完整 Checkpoint，更新父状态，
随后发布 todos.updated；关键状态失败必须交给 Runtime，不能伪装成普通工具错误。
"""

import asyncio
from copy import deepcopy
import json
import logging

from app.agents.prompts.todo_list import WRITE_TODOS_DESCRIPTION
from app.domain.messages import ToolCall
from app.domain.threads import ThreadState
from app.domain.todos import TodoItem, todo_result_content
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.async_io import finish_inflight
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError


logger = logging.getLogger(__name__)


class WriteTodosTool:
    """每个主 Run 独立创建；Runtime 恢复好状态后由 TodoMiddleware 绑定。"""

    definition = ToolDefinition(
        name="write_todos", description=WRITE_TODOS_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "minLength": 1},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    )

    def __init__(self) -> None:
        self._state: ThreadState | None = None
        self._context: RuntimeContext | None = None
        self._lock = asyncio.Lock()

    def bind(self, state: ThreadState, context: RuntimeContext) -> None:
        """建立与本次父状态的内存联系，不保存清单，也不改变旧清单的归属。"""
        if self._state is not None:
            raise RuntimeError("WriteTodosTool 已绑定 Run，不能跨 Run 复用")
        if (state.user_id, state.thread_id) != (context.user_id, context.thread_id):
            raise ValueError("清单状态与 RuntimeContext 不属于同一用户和 Thread")
        if state.workspace_path not in {None, context.workspace_path}:
            raise ValueError("清单状态与 RuntimeContext 的工作目录不一致")
        if not isinstance(context.run_id, str) or not context.run_id.strip():
            raise ValueError("清单必须属于有效的 Run")
        self._state, self._context = state, context

    def _validate_context(self, context: RuntimeContext) -> None:
        if self._state is None or self._context is None:
            raise RuntimeError("WriteTodosTool 尚未绑定本次 Run")
        if (context.user_id, context.thread_id, context.run_id, context.workspace_path) != (
            self._context.user_id, self._context.thread_id,
            self._context.run_id, self._context.workspace_path,
        ):
            raise ValueError("write_todos 不属于当前用户、Thread、Run 或工作目录")
        if (self._state.user_id, self._state.thread_id) != (context.user_id, context.thread_id):
            raise ValueError("清单状态的用户或 Thread 已改变")
        if self._state.workspace_path not in {None, context.workspace_path}:
            raise ValueError("清单状态的工作目录已改变")

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        """校验完整清单，保存成功后才替换内存；传入空列表表示清空。"""
        self._validate_context(context)
        assert self._state is not None and self._context is not None
        state, bound_context = self._state, self._context
        if call.name != "write_todos" or not isinstance(call.id, str) or not call.id.strip():
            raise ValueError("write_todos 必须有有效的工具调用编号")
        if not isinstance(call.arguments, dict) or set(call.arguments) != {"todos"}:
            raise ValueError("write_todos 只接受 todos 参数")
        if not isinstance(call.arguments["todos"], list):
            raise ValueError("todos 必须是完整的列表")
        todos = [TodoItem.from_dict(item) for item in call.arguments["todos"]]

        async def persist() -> None:
            async with self._lock:
                self._validate_context(context)
                last_assistant = next((item for item in reversed(state.messages) if item.role == "assistant"), None)
                writes = [item for item in last_assistant.tool_calls if item.name == "write_todos"] if last_assistant else []
                # 与 DeerFlow 一样，拒绝同一条模型回复中的多次清单写入。
                # 两份“完整替换”没有可合并的含义，不能让后一次悄悄覆盖前一次。
                if len(writes) != 1 or writes[0] != call:
                    raise ValueError("每条模型回复只能调用一次 write_todos，且必须对应当前工具决定")
                snapshot = deepcopy(state)
                snapshot.todos = deepcopy(todos)
                snapshot.todos_run_id, snapshot.todos_tool_call_id = context.run_id, call.id
                try:
                    await bound_context.save_checkpoint(snapshot)
                except StatePersistenceError:
                    raise
                except Exception as error:
                    raise StatePersistenceError("任务清单的关键状态未能确认保存") from error
                # 这一段没有 await：提交一旦确认，就立即同步内存，取消也不能插进来。
                state.todos = deepcopy(todos)
                state.todos_run_id, state.todos_tool_call_id = context.run_id, call.id

        # 若用户在 SQLite 写入时点停止，先等待在途保存及内存同步，再传递取消。
        await finish_inflight(asyncio.create_task(persist()))
        content = todo_result_content(todos, context.run_id, call.id)
        try:
            await bound_context.record_event("todos.updated", {
                **json.loads(content), "status_confirmed": True,
            })
        except Exception:
            logger.warning("任务清单通知失败，清单已保存，run_id=%s", context.run_id, exc_info=True)
        return ToolResult(tool_call_id=call.id, name="write_todos", content=content)
