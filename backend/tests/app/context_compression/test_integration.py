"""真实主子 Agent、SQLite 重启、完整请求及失败收尾的组合验证。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware_stack import build_runtime_middlewares
from app.context_compression.middleware import ContextCompressionMiddleware
from app.context_compression.policy import CompressionPolicy
from app.context_compression.state import CompressionState
from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition, ToolResult
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.services.thread_service import ThreadService
from app.services.run_service import RunService
from app.subagents.executor import SubagentExecutor
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.snip import SnipTool


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def scope(tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    state = ThreadState(user_id='u', thread_id='t', workspace_path=str(workspace), messages=[Message(role='user', content='parent')])
    saved, events = [], []
    async def save(snapshot):
        saved.append(deepcopy(snapshot))
    async def event(kind, payload):
        events.append((kind, payload))
    return state, RuntimeContext('u', 't', 'r', str(workspace), event, save), saved, events


def test_sqlite_roundtrip_and_legacy_payload_without_compression(tmp_path, monkeypatch):
    monkeypatch.setenv('DEER_MINI_DATABASE_PATH', str(tmp_path / 'state.db'))
    monkeypatch.setenv('DEER_MINI_DATA_ROOT', str(tmp_path / 'users'))
    database.initialize_database()
    thread = ThreadService().create_thread('u', 'test')
    run = RunService().create_run('u', thread.id, 'offline')
    state = ThreadState(user_id='u', thread_id=thread.id, workspace_path=thread.workspace_path, messages=[Message(role='user', content='old'), Message(role='user', content='new')])
    child = SubagentTask(user_id='u', thread_id=thread.id, run_id=run.id, tool_call_id='call', description='child', prompt='child', messages=[Message(role='user', content='child')])
    state.subtasks[child.task_id] = child
    state.compression = CompressionState(snipped_ids=[state.messages[0].id], growth_anchor_id=state.messages[-1].id, growth_tokens=15, last_api_at=123.0, usage_model='old-model', last_prompt_tokens=321, last_request_estimate=300)
    child.compression = CompressionState(growth_tokens=12, last_api_at=124.0)
    repo = CheckpointRepository()
    checkpoint = repo.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=1, state=state))
    restored = CheckpointRepository().latest(thread.id, 'u').state
    assert restored.to_dict() == state.to_dict()
    assert restored.compression is not restored.subtasks[child.task_id].compression
    legacy = state.to_dict(); legacy.pop('compression'); legacy['subtasks'][child.task_id].pop('compression')
    with database.connect() as connection:
        connection.execute('UPDATE checkpoints SET state_json = ? WHERE id = ?', (json.dumps(legacy), checkpoint.id))
    restored = CheckpointRepository().latest(thread.id, 'u').state
    assert restored.compression == restored.subtasks[child.task_id].compression == CompressionState()
    assert restored.messages == state.messages


@pytest.mark.anyio
async def test_concurrent_children_cannot_snip_parent_and_keep_own_checkpoints(scope):
    parent, context, saved, _ = scope
    registry = ToolRegistry(); parent_snip = SnipTool(); registry.register(parent_snip)
    parent_snip.bind(parent, context)
    gate = asyncio.Event(); entered = []; models = []
    class ChildModel:
        last_prompt_tokens = 500
        def __init__(self):
            self.calls = 0
        async def chat(self, messages, tools, **kwargs):
            self.calls += 1
            assert 'snip' in [t.name for t in tools]
            if self.calls == 1:
                entered.append(self)
                if len(entered) == 2:
                    gate.set()
                await gate.wait()
                return Message(role='assistant', content='', tool_calls=[ToolCall('snip', 'snip', {'message_ids': [parent.messages[0].id]})])
            assert messages[-1].is_error and '未知' in messages[-1].content
            return Message(role='assistant', content='child done')
        async def close(self):
            pass
    def factory():
        model = ChildModel(); models.append(model); return model
    tasks = [SubagentTask(user_id='u', thread_id='t', run_id='r', tool_call_id=f'call-{i}', description=f'child-{i}', prompt=f'child-{i}') for i in range(2)]
    parent.subtasks = {t.task_id: t for t in tasks}
    executor = SubagentExecutor(parent_state=parent, context=context, tool_registry=registry, model_factory=factory)
    results = await asyncio.wait_for(asyncio.gather(*(executor.execute(t) for t in tasks)), 5)
    assert all(not r.is_error and r.content == 'child done' for r in results)
    assert not parent.compression.snipped_ids and parent.compression.last_api_at is None
    for task in tasks:
        assert task.compression.last_prompt_tokens == 500 and task.compression.last_api_at is not None
        assert not task.compression.snipped_ids
        assert saved[-1].subtasks[task.task_id].compression == task.compression
    assert tasks[0].compression is not tasks[1].compression
    assert all(model.calls == 2 for model in models)


@pytest.mark.anyio
@pytest.mark.parametrize('policy', [CompressionPolicy(enabled=False), CompressionPolicy(snip_enabled=False)])
async def test_child_respects_switch_even_with_parent_snip_instance(scope, policy):
    parent, context, _, _ = scope
    registry = ToolRegistry(); registry.register(SnipTool())
    task = SubagentTask(user_id='u', thread_id='t', run_id='r', tool_call_id='call', description='child', prompt='child')
    parent.subtasks[task.task_id] = task
    class Model:
        async def chat(self, messages, tools, **kwargs):
            assert 'snip' not in [t.name for t in tools]
            assert '可用 snip' not in '\n'.join(m.content for m in messages)
            return Message(role='assistant', content='done')
        async def close(self):
            pass
    executor = SubagentExecutor(parent_state=parent, context=context, tool_registry=registry, model_factory=Model, compression_policy=policy)
    result = await executor.execute(task)
    assert not result.is_error and task.status == 'completed'


@pytest.mark.anyio
async def test_stream_failure_does_not_replay_executed_tool_with_compression(scope):
    from app.model.errors import IncompleteModelResponseError, ModelCallError
    parent, context, _, events = scope
    executions = []
    class Tool:
        definition = ToolDefinition('bash', 'offline fake side effect', {})
        async def execute(self, call, context):
            executions.append(call.id)
            return ToolResult(call.id, 'bash', 'already executed')
    class Model:
        calls = 0
        async def chat(self, messages, tools, on_text_delta, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Message(role='assistant', content='', tool_calls=[ToolCall('run-once', 'bash', {})])
            await on_text_delta('partial answer')
            raise IncompleteModelResponseError()
        async def close(self):
            pass
    model = Model(); registry = ToolRegistry(); registry.register(Tool())
    compression = ContextCompressionMiddleware(model, registry, model_key='m')
    agent = LeadAgent(model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([compression]))
    with pytest.raises(ModelCallError, match='已收到的片段'):
        await agent.run(parent, context)
    assert executions == ['run-once'] and model.calls == 2
    assert parent.messages[-1].role == 'tool' and parent.messages[-1].content == 'already executed'
    assert any(kind == 'model.interrupted' for kind, _ in events)


@pytest.mark.anyio
@pytest.mark.parametrize('large_memory', [False, True])
async def test_coordinator_budgets_final_memory_todo_and_workspace_request(tmp_path, monkeypatch, large_memory):
    from app.agents.memory_context_middleware import MemoryContextMiddleware
    from app.context_compression.budget import request_tokens
    from app.context_compression.middleware import ContextBudgetExceeded
    from app.model.factory import ModelFactory
    from app.repositories.run_repository import RunRepository
    from app.runtime.stream_bridge import MemoryStreamBridge
    from app.services.run_coordinator import RunCoordinator
    monkeypatch.setenv('DEER_MINI_DATABASE_PATH', str(tmp_path / 'integration.db'))
    monkeypatch.setenv('DEER_MINI_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setenv('TAVILY_API_KEY', '')
    database.initialize_database()
    thread = ThreadService().create_thread('u', 'test')
    class Selector:
        async def select(self, state, context):
            return 'memory note ' * (6000 if large_memory else 5)
    class Memory:
        enabled = True
        def middleware(self, factory):
            return MemoryContextMiddleware(Selector())
        def schedule(self, *args):
            pass
        def stop_accepting(self):
            pass
        async def shutdown(self):
            pass
    class Model:
        calls = 0
        last_prompt_tokens = None
        async def chat(self, messages, tools, **kwargs):
            self.calls += 1
            assert '<selected_memories>' in '\n'.join(m.content for m in messages)
            assert 'write_todos' in messages[0].content
            assert thread.workspace_path not in messages[0].content  # Workspace injection exposes virtual paths.
            self.last_prompt_tokens = request_tokens(messages, tools, replay_reasoning=True)
            return Message(role='assistant', content='done')
        async def close(self):
            pass
    model = Model(); monkeypatch.setattr(ModelFactory, 'create_chat_model', lambda self: model)
    bridge = MemoryStreamBridge()
    coordinator = RunCoordinator(bridge, bash_runner=None, memory_service=Memory(), compression_policy=CompressionPolicy(context_window=12000, compact_remaining=2000, summary_tokens=1000))
    try:
        run = await coordinator.create_and_start_run(user_id='u', thread_id=thread.id, message='current request', model_name='offline', thinking_enabled=False, reasoning_effort=None)
        task = coordinator._tasks[run.id]
        if large_memory:
            with pytest.raises(ContextBudgetExceeded):
                await task
            assert model.calls == 0
            assert RunRepository().get(run.id, 'u').status == 'error'
        else:
            state = await task
            assert state.compression.last_request_estimate == model.last_prompt_tokens
            assert state.compression.last_prompt_tokens == model.last_prompt_tokens
            assert model.calls == 1
        restored = CheckpointRepository().latest(thread.id, 'u').state
        assert all('<selected_memories>' not in m.content for m in restored.messages)
        assert [m.content for m in restored.messages if m.role == 'user'] == ['current request']
    finally:
        await coordinator.shutdown(); await bridge.close()


@pytest.mark.anyio
async def test_sqlite_legacy_calibration_reset_blocks_same_model_but_not_new_model(tmp_path, monkeypatch):
    from app.context_compression.budget import calibrated_tokens, reset_request_baseline
    from app.context_compression.middleware import ContextBudgetExceeded
    monkeypatch.setenv('DEER_MINI_DATABASE_PATH', str(tmp_path / 'calibration.db'))
    monkeypatch.setenv('DEER_MINI_DATA_ROOT', str(tmp_path / 'users'))
    database.initialize_database()
    thread = ThreadService().create_thread('u', 'calibration')
    run = RunService().create_run('u', thread.id, 'old-model')
    state = ThreadState(user_id='u', thread_id=thread.id, workspace_path=thread.workspace_path, messages=[Message(role='user', content='x' * 18000)])
    state.compression = CompressionState(usage_model='old-model', last_request_estimate=4500, last_prompt_tokens=9000)
    repo = CheckpointRepository()
    checkpoint = repo.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=1, state=state))
    legacy = state.to_dict()
    legacy['compression'].pop('calibration_model')
    legacy['compression'].pop('calibration_ratio')
    with database.connect() as connection:
        connection.execute('UPDATE checkpoints SET state_json = ? WHERE id = ?', (json.dumps(legacy), checkpoint.id))
    state = CheckpointRepository().latest(thread.id, 'u').state
    assert calibrated_tokens(6000, state.compression, 'old-model') == 12000
    reset_request_baseline(state.compression)
    repo.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=2, state=state))
    state = CheckpointRepository().latest(thread.id, 'u').state
    assert state.compression.last_prompt_tokens is None
    assert calibrated_tokens(6000, state.compression, 'old-model') == 12000
    assert calibrated_tokens(6000, state.compression, 'new-model') == 6000
    class Model:
        calls = 0
        async def chat(self, **kwargs):
            self.calls += 1
            return Message(role='assistant', content='done')
        async def close(self):
            pass
    step = 2
    async def save(snapshot):
        nonlocal step
        step += 1
        return repo.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=step, state=snapshot))
    async def event(*args):
        pass
    context = RuntimeContext('u', thread.id, run.id, thread.workspace_path, event, save)
    policy = CompressionPolicy(context_window=12000, compact_remaining=2000, summary_tokens=1000)
    model = Model(); registry = ToolRegistry()
    old_middleware = ContextCompressionMiddleware(model, registry, model_key='old-model', policy=policy)
    with pytest.raises(ContextBudgetExceeded):
        await LeadAgent(model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([old_middleware])).run(state, context)
    assert model.calls == 0
    new_middleware = ContextCompressionMiddleware(model, registry, model_key='new-model', policy=policy)
    await LeadAgent(model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([new_middleware])).run(state, context)
    assert model.calls == 1 and state.messages[-1].content == 'done'
    assert CheckpointRepository().latest(thread.id, 'u').state.messages[-1].content == 'done'
