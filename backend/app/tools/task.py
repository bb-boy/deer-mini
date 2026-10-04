"""把主模型的 task 调用交给本 Run 的子任务调度器。

功能：模型可以委派一项有明确范围的工作，并收到真实子 Agent 的结果。
输入：call 提供调用编号和 description/prompt/subagent_type；context 提供
后台确认的用户、对话、Run 和工作目录。模型不能自行指定这些后台身份。
输出：与原始 tool_call_id 对应的 ToolResult。
副作用：委托 Dispatcher 调用模型/工具、保存 Checkpoint、发布子任务事件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.messages import ToolCall
from app.domain.subagents import SubagentTask
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext

if TYPE_CHECKING:
    from app.subagents.dispatcher import SubagentDispatcher


# 摘录 DeerFlow tools/builtins/task_tool.py:task_tool 的原始说明。
# 删除尚未提供的专业能力、bash 子类型和自定义 Agent 段落，不另写固定提示词。
TASK_DESCRIPTION = """Delegate a bounded task to a specialized subagent in its own context.

Delegate only when expected benefit clearly exceeds delegation overhead.
Useful benefits are:
- Material wall-clock savings from independent parallel work
- Context isolation for a bounded, unusually context-heavy investigation

Built-in subagent types:
- **general-purpose**: A capable agent for bounded exploration and action.

When to use this tool:
- Independent tasks that materially reduce wall-clock time when run in parallel
- Bounded exploration that would otherwise displace important parent context

When NOT to use this tool:
- Merely because a task is complex, multi-step, verbose, or touches a large repo
- Parallel work with overlapping files, shared mutable state, or external side effects
- Tasks requiring user interaction or clarification

Costs to include in the delegation decision:
- Repeating the same repository discovery in multiple contexts
- Coordination, verification, and synthesis of returned results
- Any task the parent can complete more cheaply with direct tools"""


class TaskTool:
    """统一工具表中的委派入口；每次 Run 使用独立实例，启动时再绑定状态。"""

    definition = ToolDefinition(
        name="task",
        description=TASK_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task for logging/display. ALWAYS PROVIDE THIS PARAMETER FIRST.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task description for the subagent. Be specific and clear about what needs to be done. ALWAYS PROVIDE THIS PARAMETER SECOND.",
                },
                "subagent_type": {
                    "type": "string", "enum": ["general-purpose"],
                    "description": "The type of subagent to use. ALWAYS PROVIDE THIS PARAMETER THIRD.",
                },
            },
            "required": ["description", "prompt", "subagent_type"],
            "additionalProperties": False,
        },
    )

    def __init__(self) -> None:
        self._dispatcher: SubagentDispatcher | None = None

    def bind(self, dispatcher: SubagentDispatcher) -> None:
        """输入已绑定父状态的调度器；只建立内存联系，不启动任何子任务。"""
        if self._dispatcher is not None:
            raise RuntimeError("TaskTool 已绑定 Run，不能跨 Run 复用")
        self._dispatcher = dispatcher

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        """校验模型参数，补齐后台身份，再等待本次委派的结果。"""
        if self._dispatcher is None:
            raise RuntimeError("TaskTool 尚未绑定本次 Run")
        if call.name != "task" or not isinstance(call.arguments, dict):
            raise ValueError("task 调用必须提供参数对象")
        names = ("description", "prompt", "subagent_type")
        if set(call.arguments) != set(names):
            raise ValueError("task 只接受 description、prompt、subagent_type 三个参数")
        for name in names:
            value = call.arguments[name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"task 的 {name} 必须是非空字符串")
        if call.arguments["subagent_type"] != "general-purpose":
            raise ValueError("Unknown subagent type. Available: general-purpose")

        task = SubagentTask(
            tool_call_id=call.id, user_id=context.user_id,
            thread_id=context.thread_id, run_id=context.run_id,
            description=call.arguments["description"], prompt=call.arguments["prompt"],
            subagent_type=call.arguments["subagent_type"],
        )
        return await self._dispatcher.execute(task, context)
