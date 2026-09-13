"""协调 Run、Agent、关键状态和实时流；辅助日志失败不改变执行结果。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy

from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message
from app.domain.runs import Run
from app.domain.threads import Thread, ThreadState
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.agent import Agent
from app.runtime.async_io import finish_inflight, run_sync
from app.runtime.context import RuntimeContext, SaveCheckpoint
from app.runtime.event_recorder import EventRecorder
from app.runtime.stream_bridge import MemoryStreamBridge
from app.sandbox.base import SandboxLifecycle
from app.services.run_service import RunService


logger = logging.getLogger(__name__)
TERMINAL_EVENT_TYPES = {
    "success": "run.end", "error": "run.error",
    "timeout": "run.timeout", "interrupted": "run.interrupted",
}


class AgentRuntime:
    """一次 Run 的总协调器，Agent 仍通过受控 Context 保存关键状态。"""

    def __init__(
        self, stream_bridge: MemoryStreamBridge,
        checkpoint_repository: CheckpointRepository | None = None,
        thread_repository: ThreadRepository | None = None,
        run_repository: RunRepository | None = None,
        run_service: RunService | None = None,
        sandbox_lifecycle: SandboxLifecycle | None = None,
    ) -> None:
        self._stream_bridge = stream_bridge
        self._checkpoint_repository = checkpoint_repository or CheckpointRepository()
        self._thread_repository = thread_repository or ThreadRepository()
        self._run_repository = run_repository or RunRepository()
        self._run_service = run_service or RunService()
        self._sandbox_lifecycle = sandbox_lifecycle

    def _get_owned_thread_and_run(
        self, user_id: str, thread_id: str, run_id: str,
    ) -> tuple[Thread, Run]:
        thread = self._thread_repository.get(thread_id, user_id)
        if thread is None:
            raise ValueError("Thread 不存在，或不属于当前用户")
        run = self._run_repository.get(run_id, user_id)
        if run is None or run.thread_id != thread_id:
            raise ValueError("Run 不存在，或不属于当前 Thread")
        return thread, run

    def _load_state(self, thread: Thread) -> ThreadState:
        checkpoint = self._checkpoint_repository.latest(thread.id, thread.user_id)
        if checkpoint is not None:
            return checkpoint.state
        return ThreadState(
            thread_id=thread.id, user_id=thread.user_id, messages=[],
            workspace_path=thread.workspace_path,
        )

    async def _create_checkpoint_saver(
        self, user_id: str, thread: Thread, run: Run,
    ) -> SaveCheckpoint:
        """为这个 Run 创建异步保存入口；step 在成功提交后递增。"""
        history = await run_sync(self._checkpoint_repository.history, thread.id, user_id, run.id)
        next_step = history[-1].step + 1 if history else 1
        lock = asyncio.Lock()

        async def save_checkpoint(state: ThreadState) -> Checkpoint:
            nonlocal next_step
            if state.thread_id != thread.id or state.user_id != user_id:
                raise ValueError("Checkpoint 状态不属于当前用户和 Thread")
            async with lock:
                checkpoint = Checkpoint(
                    thread_id=thread.id, run_id=run.id, step=next_step,
                    state=deepcopy(state),
                )

                def persist() -> Checkpoint:
                    nonlocal next_step
                    saved = self._checkpoint_repository.save(checkpoint)
                    # 即使刚好收到取消，提交成功的 step 也不能被重复使用。
                    next_step += 1
                    return saved

                return await run_sync(persist)

        return save_checkpoint

    async def run(
        self, user_id: str, thread_id: str, run_id: str,
        user_message: str, agent: Agent, timeout_seconds: float = 240.0,
    ) -> ThreadState:
        recorder = EventRecorder(user_id, thread_id, run_id, self._stream_bridge)
        thread: Thread | None = None
        run: Run | None = None
        state: ThreadState | None = None
        save_checkpoint: SaveCheckpoint | None = None
        original_error: BaseException | None = None
        sandbox_released = False

        def load_owned_run() -> None:
            nonlocal thread, run
            # 查询期间收到取消，也先保留已验证的归属，之后才能安全收尾。
            thread, run = self._get_owned_thread_and_run(user_id, thread_id, run_id)

        async def release_sandbox() -> None:
            nonlocal sandbox_released
            if sandbox_released or run is None:
                return
            if self._sandbox_lifecycle is not None:
                await self._sandbox_lifecycle.end_run(
                    user_id=user_id, thread_id=thread_id, run_id=run_id,
                )
            sandbox_released = True

        try:
            await run_sync(load_owned_run)
            assert thread is not None and run is not None
            save_checkpoint = await self._create_checkpoint_saver(user_id, thread, run)
            state = await run_sync(self._load_state, thread)
            state.messages.append(Message(role="user", content=user_message))
            await save_checkpoint(state)
            if not await run_sync(self._run_service.start_run, run_id, user_id):
                raise RuntimeError("Run 无法从 pending 状态启动")

            if self._sandbox_lifecycle is not None:
                await self._sandbox_lifecycle.begin_run(
                    user_id=user_id, thread_id=thread_id, run_id=run_id,
                    workspace_path=thread.workspace_path,
                )

            await recorder.record_event("run.start", {
                "model_name": run.model_name, "thinking_enabled": run.thinking_enabled,
                "reasoning_effort": run.reasoning_effort,
            })
            await self._stream_bridge.publish(run_id, "metadata", {
                "run_id": run_id, "thread_id": thread_id,
            })
            context = RuntimeContext(
                user_id=user_id, thread_id=thread_id, run_id=run_id,
                workspace_path=thread.workspace_path,
                record_event=recorder.record_event, save_checkpoint=save_checkpoint,
            )
            async with asyncio.timeout(timeout_seconds):
                final_state = await agent.run(state, context)

            if final_state.thread_id != thread_id or final_state.user_id != user_id:
                raise ValueError("Agent 返回了不属于当前 Thread 的状态")
            await save_checkpoint(final_state)
            # Thread 一旦变回 idle 就能接下一轮；必须先归还本轮执行环境。
            await release_sandbox()
            # 必须先确认关键状态和 Run/Thread 终态，再向浏览器宣告成功。
            if not await run_sync(self._run_service.finish_run, run_id, user_id, "success"):
                raise RuntimeError("Run 无法结束为 success")
            await recorder.record_event("run.end", {"status": "success", "status_confirmed": True})
            return final_state

        except (Exception, asyncio.CancelledError) as error:
            original_error = error
            if run is None:
                # 归属未通过验证，不能修改或关闭传入 id 对应的其他 Run。
                raise
            if isinstance(error, asyncio.CancelledError):
                status = "interrupted"
                details = {"reason": str(error) or "cancelled"}
            elif isinstance(error, TimeoutError):
                status = "timeout"
                details = {
                    "timeout_seconds": timeout_seconds,
                    "message": f"Agent Run exceeded {timeout_seconds:g} seconds",
                }
            else:
                status = "error"
                details = {
                    "error_type": type(error).__name__,
                    "message": str(error) or type(error).__name__,
                }
            try:
                # 第二次取消不能打断第一次取消的收尾，原始异常始终继续向上抛出。
                await finish_inflight(asyncio.create_task(self._finish_failure(
                    user_id, run_id, status, details, state, save_checkpoint, recorder,
                    release_sandbox,
                )))
            except (Exception, asyncio.CancelledError):
                logger.exception("Run %s 收尾发生异常，保留最初的执行异常", run_id)
            raise
        finally:
            async def cleanup() -> None:
                try:
                    await release_sandbox()
                finally:
                    try:
                        await recorder.close()
                    finally:
                        if run is not None:
                            await self._stream_bridge.publish_end(run_id)

            try:
                await finish_inflight(asyncio.create_task(cleanup()))
            except asyncio.CancelledError:
                if original_error is None:
                    raise
            except Exception:
                logger.exception("Run %s 清理失败，Stream 已尝试结束", run_id)
                if original_error is None:
                    raise

    async def _finish_failure(
        self, user_id: str, run_id: str, status: str, details: dict,
        state: ThreadState | None, save_checkpoint: SaveCheckpoint | None,
        recorder: EventRecorder,
        release_sandbox: Callable[[], Awaitable[None]],
    ) -> None:
        """保存真实终态并发送独立通知，日志故障不会掩盖原始错误。"""
        try:
            await release_sandbox()
        except Exception:
            logger.exception("Run %s 的执行环境归还失败，将在最终清理时重试", run_id)
        # 取消可能恰好发生在 success 提交期间；以已经提交的终态为准。
        current = None
        try:
            current = await run_sync(self._run_service.get_run, run_id, user_id)
        except Exception:
            logger.exception("无法读取 Run %s 的状态，将继续尝试保存错误状态", run_id)
        if current is not None and current.status in TERMINAL_EVENT_TYPES:
            await recorder.record_event(TERMINAL_EVENT_TYPES[current.status], {
                "status": current.status, "status_confirmed": True,
                **({"message": current.error} if current.error else {}),
            })
            return

        if status in {"interrupted", "timeout"} and state is not None and save_checkpoint is not None:
            try:
                await save_checkpoint(state)
            except Exception as checkpoint_error:
                logger.exception("Run %s 的关键状态保存失败", run_id)
                status = "error"
                details = {
                    **details, "error_type": type(checkpoint_error).__name__,
                    "message": f"结束时关键状态保存失败：{checkpoint_error}",
                }

        message = str(details.get("message") or details.get("reason") or status)
        confirmed = False
        try:
            if status == "interrupted":
                confirmed = await run_sync(self._run_service.interrupt_run, run_id, user_id, message)
            elif status == "timeout":
                confirmed = await run_sync(self._run_service.finish_run, run_id, user_id, status, message)
            else:
                # 这个原子操作同时支持 pending/running，启动前保存失败也能正确收尾。
                confirmed = await run_sync(self._run_service.recover_orphaned_run, run_id, user_id, message)
            if not confirmed:
                current = await run_sync(self._run_service.get_run, run_id, user_id)
                if current is not None and current.status in TERMINAL_EVENT_TYPES:
                    status, confirmed = current.status, True
                    details = {"message": current.error} if current.error else {}
        except Exception:
            logger.exception("Run %s 的终态未能保存，不能声称数据库状态已确认", run_id)

        await recorder.record_event(
            TERMINAL_EVENT_TYPES[status] if confirmed else "run.error",
            {**details, "status": status if confirmed else "unknown", "status_confirmed": confirmed,
             **({} if confirmed else {"message": f"{message}；最终状态未能保存，请稍后重新查询"})},
        )
