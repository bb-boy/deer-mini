"""在父 Agent 开始时，把 task 工具接到 Runtime 已恢复的真实状态上。"""

from collections.abc import Callable

from app.agents.middleware import AgentMiddleware
from app.context_compression.policy import CompressionPolicy
from app.domain.threads import ThreadState
from app.model.base import ChatModel
from app.runtime.context import RuntimeContext
from app.subagents.dispatcher import SubagentDispatcher
from app.subagents.limits import DEFAULT_MAX_CONCURRENT_SUBAGENTS, DEFAULT_MAX_TOTAL_SUBAGENTS
from app.tools.registry import ToolRegistry
from app.tools.task import TaskTool


class SubagentMiddleware(AgentMiddleware):
    """建立一次 Run 内的联系，不在这里执行子任务。

    输入：task_tool 是给主模型注册的入口；tool_registry 提供可继承的工具；
    model_factory 按需新建子模型；两个额度控制并发与累计任务数；超时、轮数和
    推理设置交给子执行器。before_agent 接收 Runtime 恢复的 state 和 context。
    输出：task_tool 绑定本 Run 的 Dispatcher。
    副作用：只创建内存对象；此时不会调用模型、写库或发送 Stream。
    """

    def __init__(
        self, task_tool: TaskTool, tool_registry: ToolRegistry,
        model_factory: Callable[[], ChatModel], *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_SUBAGENTS,
        max_total: int = DEFAULT_MAX_TOTAL_SUBAGENTS,
        timeout_seconds: float = 120.0, max_tool_rounds: int = 8,
        thinking_enabled: bool = False, reasoning_effort: str | None = None,
        compression_policy: CompressionPolicy | None = None,
    ) -> None:
        self._task_tool = task_tool
        self._registry = tool_registry
        self._model_factory = model_factory
        self._options = {
            "compression_policy": compression_policy,
            "max_concurrent": max_concurrent, "max_total": max_total,
            "timeout_seconds": timeout_seconds, "max_tool_rounds": max_tool_rounds,
            "thinking_enabled": thinking_enabled, "reasoning_effort": reasoning_effort,
        }

    async def before_agent(self, state: ThreadState, context: RuntimeContext) -> None:
        self._task_tool.bind(SubagentDispatcher(
            parent_state=state, context=context, tool_registry=self._registry,
            model_factory=self._model_factory, **self._options,
        ))
