"""真实 Agent Loop 中的临时提醒、原始提示注入和有限继续行为。"""

import asyncio
from copy import deepcopy

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.agents.prompts.todo_list import TODO_SYSTEM_PROMPT, WRITE_TODOS_DESCRIPTION
from app.agents.todo_middleware import TodoMiddleware
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.todos import TodoItem
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.write_todos import WriteTodosTool


class TodoLoop:
    def __init__(self, replies):
        self.replies, self.requests, self.snapshots, self.events = replies, [], [], []
        self.closed = 0
        self.state = ThreadState(user_id="user", thread_id="thread", messages=[Message(role="user", content="完成工作")])
        self.context = RuntimeContext("user", "thread", "run", "/workspace", self.event, self.save)
        self.tool = WriteTodosTool()
        self.middleware = TodoMiddleware(self.tool)
        self.registry = ToolRegistry()
        self.registry.register(self.tool)

    async def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        return deepcopy(self.replies[len(self.requests) - 1])

    async def close(self):
        self.closed += 1

    async def event(self, kind, payload):
        event = RunEvent("run", "thread", kind, deepcopy(payload))
        self.events.append(event)
        return event

    async def save(self, state):
        self.snapshots.append(deepcopy(state))
        return Checkpoint(thread_id="thread", run_id="run", step=len(self.snapshots), state=deepcopy(state))

    async def run(self, max_tool_rounds=8):
        return await LeadAgent(self, self.registry, ToolExecutor(self.registry),
            middlewares=[self.middleware], max_tool_rounds=max_tool_rounds).run(self.state, self.context)


def decide(todos, call_id="plan"):
    return Message(role="assistant", content="", tool_calls=[ToolCall(call_id, "write_todos", {"todos": todos})])


def test_original_system_prompt_is_added_to_request_copy_only():
    async def scenario():
        loop = TodoLoop([])
        system = Message(role="system", content="已有工作区信息")
        loop.state.messages.insert(0, system)
        before = loop.state.to_dict()
        await loop.middleware.before_agent(loop.state, loop.context)
        for _ in range(3):
            request = await loop.middleware.prepare_model_messages(loop.state, loop.context, loop.state.messages)
            assert request[0] is not system
            assert request[0].content == system.content + "\n\n" + TODO_SYSTEM_PROMPT
            assert request[0].content.count("<todo_list_system>") == 1
        assert loop.state.to_dict() == before
        assert loop.tool.definition.description == WRITE_TODOS_DESCRIPTION

    asyncio.run(scenario())


def test_unfinished_work_gets_two_private_reminders_and_keeps_genuine_status():
    async def scenario():
        loop = TodoLoop([
            decide([{"content": "受阻的工作", "status": "in_progress"}]),
            Message(role="assistant", content="目前无法完成。"),
            Message(role="assistant", content="仍有阻碍。"),
            Message(role="assistant", content="请补充缺少的资料。"),
        ])
        state = await loop.run()
        assert len(loop.requests) == 4 and loop.closed == 1
        reminders = [message for request in loop.requests for message in request if "<system_reminder>" in message.content]
        assert len(reminders) == 2 and all(message.role == "user" for message in reminders)
        assert all("- [in_progress] 受阻的工作" in message.content for message in reminders)
        assert [message.content for message in state.messages if message.role == "user"] == ["完成工作"]
        assert all("<system_reminder>" not in message.content for snapshot in loop.snapshots for message in snapshot.messages)
        assert all("system_reminder" not in str(event.payload) for event in loop.events)
        assert state.todos[0].status == "in_progress"
        assert state.messages[-1].content == "请补充缺少的资料。"

    asyncio.run(scenario())


@pytest.mark.parametrize("finish_todos", [[], [{"content": "工作", "status": "completed"}]])
def test_confirming_completed_or_cleared_plan_allows_final_response(finish_todos):
    async def scenario():
        loop = TodoLoop([
            decide([{"content": "工作", "status": "pending"}]),
            Message(role="assistant", content="已处理。"),
            decide(finish_todos, "finish"),
            Message(role="assistant", content="最终结果。"),
        ])
        state = await loop.run()
        assert state.todos_tool_call_id == "finish"
        assert state.messages[-1].content == "最终结果。"
        assert sum("<system_reminder>" in message.content for request in loop.requests for message in request) == 1
        assert len(loop.requests) == 4

    asyncio.run(scenario())


@pytest.mark.parametrize(("todos", "plan_run"), [
    ([], None), ([TodoItem("已处理", "completed")], "run"),
    ([TodoItem("旧请求未完成", "in_progress")], "earlier-run"),
])
def test_simple_question_and_old_run_plan_do_not_force_continuation(todos, plan_run):
    async def scenario():
        loop = TodoLoop([Message(role="assistant", content="直接回答")])
        loop.state.todos = deepcopy(todos)
        loop.state.todos_run_id = plan_run
        loop.state.todos_tool_call_id = "earlier-call" if plan_run else None
        state = await loop.run()
        assert len(loop.requests) == 1 and state.messages[-1].content == "直接回答"
        assert state.todos_run_id == plan_run

    asyncio.run(scenario())


def test_middleware_continuation_does_not_skip_other_hooks():
    async def scenario():
        trace = []

        class Hook(AgentMiddleware):
            def __init__(self, name, continuation):
                self.name, self.continuation = name, continuation

            async def prepare_model_messages(self, state, context, messages):
                trace.append("prepare-" + self.name)
                return [*messages, Message(role="system", content=self.name)]

            async def after_model(self, state, context, message):
                trace.append("after-" + self.name)
                return self.continuation

        loop = TodoLoop([])
        manager = MiddlewareManager([Hook("a", None), Hook("b", True), Hook("c", False)])
        prepared = await manager.prepare_model_messages(loop.state, loop.context, loop.state.messages)
        assert await manager.after_model(loop.state, loop.context, Message(role="assistant", content="回答"))
        assert trace == ["prepare-a", "prepare-b", "prepare-c", "after-c", "after-b", "after-a"]
        assert [message.content for message in prepared[-3:]] == ["a", "b", "c"]
        assert len(loop.state.messages) == 1

    asyncio.run(scenario())


def test_completion_reminders_still_obey_global_loop_limit():
    async def scenario():
        loop = TodoLoop([
            decide([{"content": "工作", "status": "pending"}]),
            Message(role="assistant", content="还没完成"),
        ])
        with pytest.raises(RuntimeError, match="模型调用上限"):
            await loop.run(max_tool_rounds=2)
        assert len(loop.requests) == 2 and loop.closed == 1
        assert loop.state.todos[0].status == "pending"

    asyncio.run(scenario())
