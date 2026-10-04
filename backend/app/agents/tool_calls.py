"""执行一轮模型工具调用，并保证主模型最终能收到逐一对应的工具消息。"""

import asyncio
import logging
from itertools import groupby

from app.agents.middleware import MiddlewareManager
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.todos import todo_result_content
from app.runtime.async_io import finish_inflight
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.executor import ToolExecutor


logger = logging.getLogger(__name__)
INTERRUPTED_RESULT = "工具执行已中断，返回结果尚未确认；可能已经产生部分副作用。"


def repair_interrupted_tool_history(state: ThreadState) -> bool:
    """修补进程中断留下的工具消息空缺，让下一条用户消息可以继续交给模型。

    输入：从 Checkpoint 恢复的完整父消息。输出：是否插入了缺失工具消息。
    副作用：仅修改内存 messages；调用方负责保存，不重新执行任何工具。
    历史 tool_call_id 可能跨 Run 重复，不能据此猜测某个旧子任务的结果。
    """
    repaired: list[Message] = []
    pending: list[ToolCall] = []
    received: set[str] = set()
    changed = False

    def complete_pending() -> None:
        nonlocal changed
        for call in pending:
            if call.id not in received:
                repaired.append(Message(
                    role="tool", content=INTERRUPTED_RESULT, tool_call_id=call.id, is_error=True,
                ))
                changed = True

    for message in state.messages:
        if pending and message.role not in {"tool", "system"}:
            complete_pending()
            pending, received = [], set()
        repaired.append(message)
        if message.role == "assistant" and message.tool_calls:
            pending, received = message.tool_calls, set()
        elif message.role == "tool" and pending and message.tool_call_id is not None:
            received.add(message.tool_call_id)
    complete_pending()
    if changed:
        state.messages = repaired
    return changed


def _known_or_interrupted_result(call: ToolCall, state: ThreadState, context: RuntimeContext) -> Message:
    """收尾时优先使用本 Run 已保存的清单/子任务结果；其余明确标为未确认。"""
    if call.name == "write_todos" and (
        state.user_id, state.thread_id, state.todos_run_id, state.todos_tool_call_id,
    ) == (context.user_id, context.thread_id, context.run_id, call.id):
        return Message(
            role="tool", tool_call_id=call.id,
            content=todo_result_content(state.todos, context.run_id, call.id),
        )
    if call.name == "task":
        matches = [task for task in state.subtasks.values() if (
            task.run_id == context.run_id and task.tool_call_id == call.id
            and task.user_id == context.user_id and task.thread_id == context.thread_id
        )]
        if len(matches) == 1:
            task = matches[0]
            if task.status == "completed" and task.result is not None and task.error is None:
                return Message(role="tool", content=task.result, tool_call_id=call.id)
            if task.status in {"failed", "timed_out", "cancelled"}:
                return Message(
                    role="tool", content=f"Task {task.status}: {task.error or task.status}",
                    tool_call_id=call.id, is_error=True,
                )
    return Message(role="tool", content=INTERRUPTED_RESULT, tool_call_id=call.id, is_error=True)


def _primary_error(error: BaseException) -> BaseException:
    """TaskGroup 可能收集多个异常；把关键状态原始异常继续交给父 Runtime。"""
    if not isinstance(error, BaseExceptionGroup):
        return error
    errors = [_primary_error(item) for item in error.exceptions]
    return next((item for item in errors if isinstance(item, StatePersistenceError)), errors[0])


async def execute_tool_calls(
    *, state: ThreadState, context: RuntimeContext, calls: list[ToolCall],
    round_number: int, executor: ToolExecutor, middleware: MiddlewareManager,
) -> None:
    """连续 task 并发，普通工具顺序执行，结果按模型原始调用顺序保存。

    输入：state 保存父对话；context 提供事件/Checkpoint 入口；calls 是这一轮
    模型的工具决定；round_number 用于展示；executor 统一查表调用真实工具；
    middleware 在结果加入父消息后运行钩子。
    输出：把每个工具结果加入 state.messages。副作用：调用工具、发布事件、
    保存完整状态；失败或取消时等待子任务全部退出，再保存已知结果与中断记录。
    """
    outcomes: list[Message | None] = [None] * len(calls)
    appended = [False] * len(calls)
    ended = [False] * len(calls)
    started = [False] * len(calls)
    finalized = False

    async def finalize_results() -> bool:
        nonlocal finalized
        if finalized:
            return False
        # 失败的收尾不重试，避免重复运行具有副作用的中间件。
        finalized = True
        messages = [message for message in outcomes if message is not None]
        return await middleware.finalize_tool_results(state, context, calls, messages)

    async def invoke(index: int) -> None:
        call = calls[index]
        await context.record_event("tool.start", {
            "round": round_number, "tool_call_id": call.id,
            "tool_name": call.name, "arguments": call.arguments,
        })
        started[index] = True
        # task 也走统一 ToolExecutor，没有绕过注册表另开一条执行通道。
        outcomes[index] = await middleware.wrap_tool_call(
            state, context, call, lambda: executor.execute(call, context),
        )

    async def publish_end(index: int, *, interrupted: bool = False) -> None:
        call, message = calls[index], outcomes[index]
        assert message is not None
        await context.record_event("tool.end", {
            "round": round_number, "tool_call_id": call.id,
            "tool_name": call.name, "content": message.content,
            "is_error": message.is_error,
            "tool_result_file": message.tool_result_file,
            "tool_result_chars": message.tool_result_chars,
            **({"interrupted": True} if interrupted else {}),
        })
        ended[index] = True

    async def commit(index: int) -> None:
        message = outcomes[index]
        assert message is not None
        state.messages.append(message)
        appended[index] = True
        await middleware.after_tool(state, context, calls[index], message)
        await context.save_checkpoint(state)

    async def finish_aborted() -> None:
        # 此时 TaskGroup 已经等待所有兄弟任务退出，不会再有子任务写入父快照。
        for index, call in enumerate(calls):
            if not appended[index]:
                if outcomes[index] is None:
                    outcomes[index] = (
                        _known_or_interrupted_result(call, state, context)
                        if started[index] else Message(
                            role="tool", content="本次执行已结束，此工具尚未执行。",
                            tool_call_id=call.id, is_error=True,
                        )
                    )
                state.messages.append(outcomes[index])
                appended[index] = True
        await finalize_results()
        await context.save_checkpoint(state)
        for index in range(len(calls)):
            if not ended[index]:
                try:
                    await publish_end(index, interrupted=True)
                except Exception:
                    logger.warning("工具收尾通知失败，保留已保存的结果", exc_info=True)

    try:
        for is_task_batch, items in groupby(range(len(calls)), key=lambda i: calls[i].name == "task"):
            indices = list(items)
            if is_task_batch:
                async with asyncio.TaskGroup() as group:
                    workers = [group.create_task(invoke(index), name=f"tool-task-{calls[index].id}") for index in indices]
                if any(worker.cancelled() for worker in workers):
                    raise asyncio.CancelledError("task tool execution was cancelled")
                # 整批结束后才加入父工具消息，避免它与子快照保存互相覆盖。
                for index in indices:
                    await commit(index)
            else:
                for index in indices:
                    await invoke(index)
                    await commit(index)
        if await finalize_results():
            await context.save_checkpoint(state)
        # 最后一个 after_tool 可能将本轮前面的较大结果落盘。
        # 等整轮结果定稿，再按调用顺序发布，避免 SSE 仍展示旧全文。
        for index in range(len(calls)):
            await publish_end(index)
    except BaseException as error:
        original = _primary_error(error)
        try:
            await finish_inflight(asyncio.create_task(finish_aborted()))
        except StatePersistenceError as persistence_error:
            if not isinstance(original, StatePersistenceError):
                persistence_error.execution_error = original
                raise persistence_error from original
            logger.exception("工具中断后的状态收尾失败，保留最初的持久化异常")
        except (Exception, asyncio.CancelledError):
            logger.exception("工具中断后的状态收尾失败，保留最初的执行异常")
        if original is not error:
            raise original
        raise
