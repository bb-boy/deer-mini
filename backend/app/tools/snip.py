"""模型选择无用旧消息，工具校验并持久化 ID；不删除原始聊天或文件。"""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.context_compression.budget import reset_request_baseline
from app.context_compression.state import commit_compression
from app.context_compression.view import validate_snip
from app.domain.messages import ToolCall
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext


class SnipArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_ids: list[str] = Field(min_length=1)


class SnipTool:
    definition = ToolDefinition(
        name="snip",
        description=("按 message_ids 移除已无用的旧模型上下文，原始聊天保留。无需等待提醒。"
                     "工具调用和结果须全组选中，不可删除当前对话、Task/清单或系统约束。"),
        parameters=SnipArgs.model_json_schema(),
    )

    def __init__(self) -> None:
        self._state: ThreadState | None = None
        self._context: RuntimeContext | None = None

    def bind(self, state: ThreadState, context: RuntimeContext) -> None:
        if self._state is not None:
            raise RuntimeError("Snip 工具不能跨 Agent 复用")
        self._state, self._context = state, context
        self._validate(context)

    def _validate(self, context: RuntimeContext) -> None:
        if self._state is None or self._context is None:
            raise ValueError("Snip 尚未绑定当前上下文")
        state, bound = self._state, self._context
        if (context.user_id, context.thread_id, context.run_id, context.workspace_path) != (
            bound.user_id, bound.thread_id, bound.run_id, bound.workspace_path,
        ) or (state.user_id, state.thread_id) != (context.user_id, context.thread_id):
            raise ValueError("Snip 不属于当前用户、Thread 或 Run")
        if state.workspace_path not in {None, context.workspace_path}:
            raise ValueError("Snip 的工作目录不匹配")

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        self._validate(context)
        assert self._state is not None
        state = self._state
        try:
            args = SnipArgs.model_validate(call.arguments)
        except ValidationError:
            raise ValueError("Snip 参数必须是非空 message_ids 列表") from None
        last = next((m for m in reversed(state.messages) if m.role == "assistant"), None)
        if call.name != "snip" or not call.id or last is None or sum(c == call for c in last.tool_calls) != 1:
            raise ValueError("Snip 必须对应当前模型的工具调用")
        selected = set(args.message_ids)
        validate_snip(state.messages, selected, state.compression)
        candidate = state.compression.model_copy(deep=True)
        previous = set(candidate.snipped_ids)
        candidate.snipped_ids = [m.id for m in state.messages if m.id in previous | selected]
        candidate.growth_tokens = 0
        candidate.growth_anchor_id = state.messages[-1].id
        reset_request_baseline(candidate)
        await commit_compression(state, context, candidate)
        return ToolResult(call.id, "snip", f"已将 {len(selected - previous)} 条旧消息移出后续模型上下文；原始聊天仍保留。")
