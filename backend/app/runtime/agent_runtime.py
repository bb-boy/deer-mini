





from typing import Callable
from app.domain.messages import Message
from app.domain.checkpoints import Checkpoint
from app.domain.runs import Run
from app.domain.threads import Thread, ThreadState
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.agent import Agent
from app.runtime.context import RuntimeContext
from app.runtime.event_recorder import EventRecorder
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_service import RunService


class AgentRuntime:
    """
    协调一次完整 Run 的执行。

    它不负责让 LLM 思考；
    它只负责把状态、事件、Run 生命周期和 Agent 串起来。
    """

    def __init__(
        self,
        stream_bridge: MemoryStreamBridge,
        checkpoint_repository: CheckpointRepository | None = None,
        thread_repository: ThreadRepository | None = None,
        run_repository: RunRepository | None = None,
        run_service: RunService | None = None,
    ) -> None:

        self._stream_bridge = stream_bridge
        self._checkpoint_repository = checkpoint_repository or CheckpointRepository()
        self._thread_repository = thread_repository or ThreadRepository()
        self._run_repository = run_repository or RunRepository()
        self._run_service = run_service or RunService()


    #检查用户是否用哟这个对话，以及这个这个run是否存在或者是否属于这个对话
    def _get_owned_thread_and_run(self, user_id: str, thread_id: str, run_id: str) -> tuple[Thread, Run]:
        """
        获取用户拥有的 Thread 和 Run。
        如果用户没有权限访问该 Thread 或 Run，则抛出异常。
        """
        thread = self._thread_repository.get(thread_id, user_id)
        if thread is None:
            raise ValueError(f"Thread with id {thread_id} and user_id {user_id} does not exist.")

        run = self._run_repository.get(run_id, user_id)
        if run is None or run.thread_id != thread_id:
            raise ValueError("Run 不存在，或不属于当前 Thread")

        return thread, run


    #恢复这个 Thread 的最新对话状态。
    #第一次对话没有 Checkpoint，就创建空状态；
    #后续对话则从最新 Checkpoint 恢复历史消息。返回checkpoint.state字段这是一个ThreadState对象，包含所有的messages
    def _load_state(self,thread: Thread,) -> ThreadState:
        """
        恢复这个 Thread 的最新对话状态。

        第一次对话没有 Checkpoint，就创建空状态；
        后续对话则从最新 Checkpoint 恢复历史消息。
        """

        #从数据库中读取某个用户的某个对话的最新 Checkpoint，如果有，就恢复 ThreadState；如果没有，就创建一个新的 ThreadState
        checkpoint = self._checkpoint_repository.latest(thread.id, thread.user_id)
        if checkpoint is not None:
            return checkpoint.state
        else:
            return ThreadState(
            thread_id=thread.id,
            user_id=thread.user_id,
            messages=[],
            workspace_path=thread.workspace_path,
        )  # 返回一个新的 ThreadState



    def _create_checkpoint_saver(self,
        user_id: str,
        thread: Thread,
        run: Run) -> Callable[[ThreadState], Checkpoint]:


        """
        为一次 Run 创建专属的“保存状态按钮”。

        返回的函数每被调用一次，
        都会保存一份 Checkpoint，并自动增加 step。
        """


        history = self._checkpoint_repository.history(thread.id, user_id, run.id)



        if history:
            next_step = history[-1].step +1
        else:
            next_step = 1

        def save_checkpoint(state: ThreadState) -> Checkpoint:
            nonlocal next_step
            checkpoint = Checkpoint(
                thread_id=thread.id,
                run_id=run.id,
                step=next_step,
                state=state,
            )
            saved_checkpoint = self._checkpoint_repository.save(checkpoint)
            next_step += 1
            return saved_checkpoint


        return save_checkpoint




    async def run(self,
        user_id: str,
        thread_id: str,
        run_id: str,
        user_message: str,
        agent: Agent) -> ThreadState:

        """
        用户发送一条消息后，系统保存这条消息、
        启动 Run、实时显示“任务开始”，
        把任务交给 Agent；Agent
        成功后保存结果并结束任务，
        出错则记录错误并结束直播。
        """

        #1 检查用户是否有权限访问这个对话和这个run
        thread, run = self._get_owned_thread_and_run(user_id, thread_id, run_id)
        if thread is None or run is None:
            raise ValueError("Thread 或 Run 不存在，或不属于当前用户")




        #2 创建一个专属的“保存状态按钮”，每次调用都会保存一份 Checkpoint，并自动增加 step
        save_checkpoint = self._create_checkpoint_saver(user_id, thread, run)

        #3 创建一个event recorder，记录事件到数据库，并实时推送到前端
        recorder = EventRecorder(
            user_id=user_id,
            thread_id=thread.id,
            run_id=run.id,
            stream_bridge=self._stream_bridge,
        )

        started =  False

        try:

            #4 恢复这个对话的最新状态
            state = self._load_state(thread)

            #5 用户消息加入到状态中
            state.messages.append(
                Message(role="user", content=user_message)
            )

            #保存用户消息的 Checkpoint
            save_checkpoint(state)



            # 6. 只有 Run 成功从 pending 变成 running，
            # 才允许继续调用 Agent。
            if not self._run_service.start_run(run.id, user_id):
                raise RuntimeError(
                    "Run 无法从 pending 状态启动"
                )

            # 这个标记用于 except：
            # 后续发生异常时，Runtime 才能安全把 Run 结束为 error。
            started = True

            #7 保存并实时推送
            await recorder.record_event("run.start",
                {"model_name": run.model_name,
                 "thinking_enabled": run.thinking_enabled,
                 "reasoning_effort": run.reasoning_effort})


            #8 发布一个meta事件，告诉前端当前的run已经开始，但是不保存为runevent
            await self._stream_bridge.publish(run.id, "metadata", {
                "run_id": run.id,
                "thread_id": thread.id,
            })


            #9 创建一个运行时上下文，传给Agent
            context = RuntimeContext(
                user_id=user_id,
                thread_id=thread_id,
                run_id=run_id,
                workspace_path=thread.workspace_path,
                record_event=recorder.record_event,
                save_checkpoint=save_checkpoint,
            )

            #10 把上下文传给Agent，让Agent去处理用户消息
            final_state = await agent.run(state, context)



            # 防止错误 Agent 返回其他用户或其他 Thread 的状态。
            if (
                final_state.thread_id != thread_id
                or final_state.user_id != user_id
            ):
                raise ValueError("Agent 返回了不属于当前 Thread 的状态")

            # 保存 Agent 最终得到的完整状态。
            save_checkpoint(final_state)

            #  记录结束事件，再结束 Run。
            await recorder.record_event(
                "run.end",
                {"status": "success"},
            )

            if not self._run_service.finish_run(
                run_id,
                user_id,
                "success",
            ):
                raise RuntimeError("Run 无法结束为 success")

            return final_state

        except Exception as error:
            # Agent 或 Runtime 出错时，记录错误并更新 Run 状态。
            if started:
                error_message = str(error) or error.__class__.__name__

                await recorder.record_event(
                    "run.error",
                    {
                        "error_type": error.__class__.__name__,
                        "message": error_message,
                    },
                )

                self._run_service.finish_run(
                    run_id,
                    user_id,
                    "error",
                    error_message,
                )

            # 不吞掉错误，让未来 API 知道本次请求失败。
            raise

        finally:
            # 成功、失败都必须结束直播；否则浏览器会一直等待。
            await self._stream_bridge.publish_end(run_id)
