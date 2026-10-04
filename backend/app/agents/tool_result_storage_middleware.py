"""单工具 50k、同一模型消息 200k 的完整结果落盘策略。"""

from collections.abc import Awaitable, Callable

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.filesystem import tool_result_store as store
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError


SINGLE_RESULT_CHARS = 50_000
PREVIEW_CHARS = 2_000
MODEL_MESSAGE_RESULT_CHARS = 200_000


def tool_result_groups(messages: list[Message]) -> list[tuple[list[ToolCall], list[Message]]]:
    groups = []
    current = None
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            current = (message.tool_calls, [])
            groups.append(current)
        elif message.role == "tool" and current is not None:
            current[1].append(message)
        elif message.role != "system":
            current = None
    return groups


class ToolResultStorageMiddleware(AgentMiddleware):
    async def _externalize(
        self, message: Message, context: RuntimeContext, *, target_chars: int | None = None,
    ) -> bool:
        original = message.content
        already_stored = message.tool_result_file is not None
        virtual_path = message.tool_result_file or store.new_result_path()
        total_chars = message.tool_result_chars if already_stored else len(original)
        if total_chars is None:
            raise StatePersistenceError("工具结果文件缺少原始字符数")
        preview_chars = min(PREVIEW_CHARS, total_chars)
        if target_chars is not None:
            # 元信息长度也算进预算；必要时只缩短预览，全文保持不变。
            header_bound = len(store.preview_header(virtual_path, total_chars, PREVIEW_CHARS))
            preview_chars = min(preview_chars, max(0, target_chars - header_bound))
        header = store.preview_header(virtual_path, total_chars, preview_chars)
        if target_chars is not None and len(header) + preview_chars >= len(original):
            return False
        try:
            if already_stored:
                preview = await run_sync(store.read_chars, context.workspace_path, virtual_path, 0, preview_chars)
            else:
                await run_sync(store.save_text, context.workspace_path, virtual_path, original)
                preview = original[:preview_chars]
        except (OSError, ValueError, RuntimeError) as error:
            raise StatePersistenceError("工具结果全文未能确认保存或读取，停止本次运行") from error
        # 文件确认保存后才改正文；保留对象身份、消息 ID、调用 ID 和错误标记。
        message.content = header + preview
        message.tool_result_file = virtual_path
        message.tool_result_chars = total_chars
        return True

    async def _compact_group(self, results: list[Message], context: RuntimeContext) -> bool:
        changed = False
        for message in results:
            if message.tool_result_file is None and len(message.content) > SINGLE_RESULT_CHARS:
                changed = await self._externalize(message, context) or changed
        total = sum(len(message.content) for message in results)
        if total <= MODEL_MESSAGE_RESULT_CHARS:
            return changed
        for message in sorted(results, key=lambda item: len(item.content), reverse=True):
            old_size = len(message.content)
            target = old_size - (total - MODEL_MESSAGE_RESULT_CHARS)
            updated = await self._externalize(message, context, target_chars=target)
            if updated:
                total += len(message.content) - old_size
                changed = True
            if total <= MODEL_MESSAGE_RESULT_CHARS:
                return changed
        # 极多的工具调用可能使文件引用本身都超过预算，不能静默丢弃引用。
        raise RuntimeError("本轮工具结果引用仍超过 200000 字符，请减少同一轮的工具调用数量")

    async def wrap_tool_call(
        self, state: ThreadState, context: RuntimeContext, tool_call: ToolCall,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        message = await call_next()
        if message.tool_result_file is None and len(message.content) > SINGLE_RESULT_CHARS:
            await self._externalize(message, context)
        return message

    async def after_tool(
        self, state: ThreadState, context: RuntimeContext, tool_call: ToolCall, tool_message: Message,
    ) -> None:
        groups = tool_result_groups(state.messages)
        if not groups:
            return
        calls, results = groups[-1]
        # 等这一条模型消息的所有结果到齐，再选择其中最大的结果。
        if [result.tool_call_id for result in results] == [call.id for call in calls]:
            await self._compact_group(results, context)

    async def before_model(self, state: ThreadState, context: RuntimeContext) -> None:
        # 覆盖旧 Checkpoint、取消后补齐结果及显式恢复的历史。
        changed = False
        for _, results in tool_result_groups(state.messages):
            changed = await self._compact_group(results, context) or changed
        if changed:
            await context.save_checkpoint(state)

    async def finalize_tool_results(
        self, state: ThreadState, context: RuntimeContext,
        tool_calls: list[ToolCall], tool_messages: list[Message],
    ) -> bool:
        # 中断时补出的工具消息也需要满足同一轮的大小限制。
        return await self._compact_group(tool_messages, context)
