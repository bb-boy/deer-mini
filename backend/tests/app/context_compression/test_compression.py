"""压缩元数据、完整交互和实际 Agent 循环的离线行为验证。"""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware_stack import build_runtime_middlewares
from app.context_compression.budget import request_tokens, text_tokens
from app.context_compression.middleware import ContextBudgetExceeded, ContextCompressionMiddleware, validate_summary
from app.context_compression.policy import CompressionPolicy
from app.context_compression.prompts import SECTIONS
from app.context_compression.state import CompressionState, commit_compression
from app.context_compression.view import active_view, eligible_ids, validate_snip
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.filesystem import tool_result_store
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.snip import SnipTool


def summary(body="已验证旧步骤；当前仍需测试；下一步运行测试。"):
    return '<summary>\n' + '\n'.join(f'{i}. {title}\n{body}' for i, title in enumerate(SECTIONS, 1)) + '\n</summary>'


class Model:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.requests = []
        self.last_prompt_tokens = None

    async def chat(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        reply = self.replies.pop(0) if self.replies else Message(role='assistant', content=summary())
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def close(self):
        pass


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def scope(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    saved, events = [], []
    async def save(state):
        saved.append(deepcopy(state))
    async def event(kind, payload):
        events.append((kind, payload))
    state = ThreadState(user_id='u', thread_id='t', workspace_path=str(workspace))
    context = RuntimeContext('u', 't', 'r', str(workspace), event, save)
    return state, context, saved, events


def interaction(name='read_file', *, call_id='same', error=False, content='result'):
    return [Message(role='assistant', content='', reasoning_content='original reasoning', tool_calls=[ToolCall(call_id, name, {})]),
            Message(role='tool', content=content, tool_call_id=call_id, is_error=error)]


def history():
    return [Message(role='user', content='old request'), *interaction(), Message(role='assistant', content='old answer'), Message(role='user', content='current request')]


def compact_policy(**kwargs):
    return CompressionPolicy(context_window=12_000, compact_remaining=2_000, summary_tokens=1000, **kwargs)


@pytest.mark.parametrize('kwargs', [{'context_window': True}, {'idle_seconds': 0}, {'summary_tokens': 33000}, {'enabled': 'true'}, {'reminder_tokens': -1}])
def test_invalid_policy(kwargs):
    with pytest.raises(ValueError):
        CompressionPolicy(**kwargs)


def test_view_atomic_groups_and_noncontiguous_boundaries():
    messages = [Message(role='system', content='system'), *history()]
    old = messages[1:5]
    validate_snip(messages, {old[0].id, old[3].id}, CompressionState())
    metadata = CompressionState(snipped_ids=[old[0].id, old[3].id])
    original = deepcopy(messages)
    view = active_view(messages, metadata)
    assert len([m for m in view if m.id.startswith('snip-boundary-')]) == 2
    assert messages == original
    assert old[1].reasoning_content == view[2].reasoning_content
    for selected in ({old[1].id}, {'foreign'}, {messages[-1].id}, {messages[0].id}):
        with pytest.raises(ValueError):
            validate_snip(messages, selected, CompressionState())


@pytest.mark.parametrize('name,error', [('task', False), ('write_todos', False), ('unknown', False), ('bash', True)])
def test_irreplaceable_groups_protected(name, error):
    group = interaction(name, error=error)
    messages = [Message(role='user', content='old'), *group, Message(role='user', content='new')]
    assert not ({m.id for m in group} & eligible_ids(messages))


def test_duplicate_call_id_in_later_round_is_not_cross_group():
    first, second = interaction(), interaction()
    messages = [*first, *second, Message(role='user', content='new')]
    validate_snip(messages, {m.id for m in first}, CompressionState())
    assert all(m.id in eligible_ids(messages) for m in first + second)


@pytest.mark.anyio
async def test_compaction_commits_summary_and_preserves_original(scope):
    state, context, saved, events = scope
    state.messages = [Message(role='user', content='old requirements ' * 2400), Message(role='assistant', content='verified change'), Message(role='user', content='current request')]
    original = deepcopy(state.messages)
    model, registry = Model(), ToolRegistry()
    middleware = ContextCompressionMiddleware(model, registry, model_key='m', policy=compact_policy())
    view = await middleware.prepare_model_messages(state, context, state.messages)
    assert state.messages == original
    assert state.compression.summary == summary()
    assert saved[-1].compression == state.compression
    assert state.messages[-1] in view
    assert model.requests and model.requests[0]['tools'] == []
    assert 'current request' in model.requests[-1]['messages'][-1].content
    assert model.requests[0].get('on_text_delta') is None
    assert events[-1][1]['phase'] == 'complete'
    assert state.compression.growth_tokens == 0


@pytest.mark.anyio
async def test_snip_in_normal_loop_below_reminder_threshold(scope):
    state, context, saved, _ = scope
    state.messages = history()
    original = deepcopy(state.messages)
    selected = [m.id for m in state.messages[:4]]
    call = ToolCall('snip1', 'snip', {'message_ids': selected})
    model = Model([Message(role='assistant', content='', tool_calls=[call]), Message(role='assistant', content='done')])
    registry = ToolRegistry(); registry.register(SnipTool())
    middleware = ContextCompressionMiddleware(model, registry, model_key='m')
    agent = LeadAgent(model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([middleware]))
    await agent.run(state, context)
    assert state.messages[:5] == original
    assert state.compression.snipped_ids == selected
    assert len(model.requests) == 2
    assert not any(m.id in selected for m in model.requests[1]['messages'])
    assert any(m.role == 'tool' and '4 条' in m.content for m in state.messages)
    restored = ThreadState.from_dict(saved[-1].to_dict())
    assert restored.compression == state.compression


@pytest.mark.anyio
async def test_reminder_counts_new_messages_and_restart(scope):
    state, context, saved, _ = scope
    state.messages = [Message(role='user', content='a' * 600)]
    registry = ToolRegistry(); registry.register(SnipTool())
    policy = CompressionPolicy(reminder_tokens=400)
    middleware = ContextCompressionMiddleware(Model(), registry, model_key='m', policy=policy)
    await middleware.prepare_model_messages(state, context, state.messages)
    assert 0 < state.compression.growth_tokens < 400
    state = ThreadState.from_dict(saved[-1].to_dict())
    state.messages.append(Message(role='assistant', content='b' * 600))
    view = await middleware.prepare_model_messages(state, context, state.messages)
    assert '新增上下文已达到' in view[0].content
    assert state.compression.growth_tokens == 0
    view = await middleware.prepare_model_messages(state, context, state.messages)
    assert '新增上下文已达到' not in view[0].content
    assert not state.compression.snipped_ids


@pytest.mark.anyio
@pytest.mark.parametrize('elapsed,cleared', [(3599, 0), (3600, 0), (3601, 2)])
async def test_idle_threshold_global_five_and_original_files(scope, elapsed, cleared):
    state, context, saved, _ = scope
    state.messages = [Message(role='user', content='old')]
    for i in range(7):
        state.messages.extend(interaction('Bash' if i % 2 else 'read_file', content=f'original-{i}'))
    state.messages.extend([*interaction('task'), *interaction('bash', error=True), Message(role='user', content='current'), *interaction('write_file')])
    original = deepcopy(state.messages)
    state.compression.last_api_at = 1.0
    middleware = ContextCompressionMiddleware(Model(), ToolRegistry(), model_key='m', clock=lambda: 1 + elapsed)
    view = await middleware.prepare_model_messages(state, context, state.messages)
    # The current result counts among the latest five; it remains protected in addition.
    if cleared:
        cleared = 3
    assert len(state.compression.cleared_results) == cleared
    assert state.messages == original
    for message in state.messages:
        if message.id in state.compression.cleared_results:
            path = state.compression.cleared_results[message.id]
            assert tool_result_store.read_chars(context.workspace_path, path, 0, 50000) == message.content
            assert next(m for m in view if m.id == message.id).content != message.content
    assert all(m.id not in state.compression.cleared_results for m in state.messages[-6:])


@pytest.mark.anyio
async def test_commit_failure_does_not_publish_and_cancel_finishes_inflight(scope):
    state, context, saved, _ = scope
    state.messages = history()
    candidate = CompressionState(snipped_ids=[state.messages[0].id])
    async def fail(snapshot):
        raise OSError('disk unavailable')
    with pytest.raises(StatePersistenceError):
        await commit_compression(state, replace(context, save_checkpoint=fail), candidate)
    assert not state.compression.snipped_ids
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(snapshot):
        entered.set(); await release.wait(); saved.append(deepcopy(snapshot))
    task = asyncio.create_task(commit_compression(state, replace(context, save_checkpoint=slow), candidate))
    await entered.wait(); task.cancel(); await asyncio.sleep(0)
    assert not state.compression.snipped_ids and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.compression == saved[-1].compression == candidate


@pytest.mark.parametrize('message', [Message(role='assistant', content='not summary'), Message(role='assistant', content=summary(), tool_calls=[ToolCall('1','bash',{})]), Message(role='assistant', content=summary(), is_error=True)])
def test_invalid_summaries_rejected(message):
    with pytest.raises(ValueError):
        validate_summary(message, 8000)



def test_request_budget_covers_system_tools_arguments_reasoning_and_model_change():
    from app.context_compression.budget import calibrated_tokens
    from app.domain.tools import ToolDefinition
    plain = [Message(role='user', content='request')]
    messages = [Message(role='system', content='system memory todos ' * 500), *plain,
                Message(role='assistant', content='', reasoning_content='reasoning ' * 300,
                        tool_calls=[ToolCall('id', 'bash', {'command': 'a' * 3000})])]
    tools = [ToolDefinition('bash', 'description ' * 500, {'type': 'object'})]
    full = request_tokens(messages, tools, replay_reasoning=True)
    assert full > request_tokens(messages, tools, replay_reasoning=False)
    assert full > request_tokens(messages, [], replay_reasoning=True)
    assert full > request_tokens(plain, [], replay_reasoning=True) + 3000
    metadata = CompressionState(usage_model='m', last_prompt_tokens=2000, last_request_estimate=1000)
    assert calibrated_tokens(1200, metadata, 'm') == 2400
    assert calibrated_tokens(500, metadata, 'm') == 1000
    assert calibrated_tokens(1200, metadata, 'new-model') == 1200


@pytest.mark.anyio
@pytest.mark.parametrize('tokens,compacts', [(966999, False), (967000, True), (967001, True)])
async def test_default_1m_33k_exact_boundary(scope, tokens, compacts):
    state, context, _, _ = scope
    state.messages = [Message(role='user', content=''), Message(role='user', content='current')]
    base = request_tokens(state.messages, [], replay_reasoning=True)
    state.messages[0].content = 'a' * ((tokens - base) * 3)
    assert request_tokens(state.messages, [], replay_reasoning=True) == tokens
    model = Model()
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m')
    await middleware.prepare_model_messages(state, context, state.messages)
    assert bool(model.requests) is compacts
    assert bool(state.compression.summary) is compacts


@pytest.mark.anyio
async def test_disabled_policy_replays_saved_view_without_new_actions(scope):
    state, context, _, _ = scope
    state.messages = history()
    state.compression = CompressionState(snipped_ids=[state.messages[0].id], summary=summary(), summary_id='summary', summarized_ids=[state.messages[3].id])
    model = Model()
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=CompressionPolicy(enabled=False))
    view = await middleware.prepare_model_messages(state, context, state.messages)
    assert state.messages[0] not in view and state.messages[3] not in view
    assert any(m.id == 'summary' for m in view)
    assert not model.requests


@pytest.mark.anyio
async def test_compaction_inherits_summary_and_every_source_chunk(scope):
    from app.context_compression.middleware import transcript
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='already summarized'), Message(role='user', content='new work \\ \n\t中文🚀 ' * 4000), Message(role='assistant', content='verified latest fix'), Message(role='user', content='continue')]
    state.compression = CompressionState(summary=summary('previous task requirements'), summary_id='old-summary', summarized_ids=[state.messages[0].id])
    source = transcript(active_view(state.messages, state.compression))
    model = Model()
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=compact_policy())
    await middleware.prepare_model_messages(state, context, state.messages)
    blocks = [r['messages'][-1].content.split('\n\n接续历史资料：\n', 1)[1] for r in model.requests]
    assert len(blocks) > 1 and ''.join(blocks) == source
    assert 'previous task requirements' in blocks[0]
    assert all(summary() in r['messages'][-1].content for r in model.requests[1:])
    assert state.compression.summarized_ids == [m.id for m in state.messages[:-1]]


@pytest.mark.anyio
@pytest.mark.parametrize('current_only', [False, True])
async def test_protected_content_cannot_fit_stops_without_dropping_it(scope, current_only):
    state, context, _, _ = scope
    state.messages = ([] if current_only else [Message(role='user', content='old')]) + [Message(role='user', content='current protected ' * 2500)]
    before = deepcopy(state.messages)
    model = Model()
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=compact_policy())
    with pytest.raises(ContextBudgetExceeded):
        await middleware.prepare_model_messages(state, context, state.messages)
    assert state.messages == before and state.compression.summary is None
    assert len(model.requests) < 5  # A bounded pass, never an unbounded re-summary loop.


@pytest.mark.anyio
async def test_summary_validation_and_commit_failure_keep_previous_metadata(scope):
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='old work ' * 5000), Message(role='user', content='new')]
    model = Model([Message(role='assistant', content='invalid output')])
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=compact_policy())
    with pytest.raises(ValueError):
        await middleware.prepare_model_messages(state, context, state.messages)
    assert state.compression.summary is None
    async def reject_summary(snapshot):
        if snapshot.compression.summary:
            raise OSError('no space')
    middleware = ContextCompressionMiddleware(Model(), ToolRegistry(), model_key='m', policy=compact_policy())
    with pytest.raises(StatePersistenceError):
        await middleware.prepare_model_messages(state, replace(context, save_checkpoint=reject_summary), state.messages)
    assert state.compression.summary is None and not state.compression.summarized_ids


@pytest.mark.anyio
@pytest.mark.parametrize('error', [asyncio.CancelledError(), StatePersistenceError('save failed')])
async def test_summary_failure_identity_survives_status_cleanup_failure(scope, error):
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='old ' * 10000), Message(role='user', content='new')]
    async def event(kind, payload):
        if payload.get('phase') == 'complete':
            raise RuntimeError('notification failed')
    middleware = ContextCompressionMiddleware(Model([error]), ToolRegistry(), model_key='m', policy=compact_policy())
    with pytest.raises(type(error)) as caught:
        await middleware.prepare_model_messages(state, replace(context, record_event=event), state.messages)
    assert caught.value is error and state.compression.summary is None


@pytest.mark.anyio
async def test_summary_retry_is_silent_and_isolated_from_main_progress(scope, monkeypatch):
    from app.agents.model_error_handling_middleware import ModelErrorHandlingMiddleware
    from app.model.call_policy import ModelCallPolicy
    from app.model.errors import EmptyModelResponseError, ModelCallProgress, model_call_progress
    import app.context_compression.middleware as module
    async def no_sleep(delay):
        pass
    monkeypatch.setattr(module, 'ModelErrorHandlingMiddleware', lambda: ModelErrorHandlingMiddleware(policy=ModelCallPolicy(), sleep=no_sleep))
    state, context, saved, events = scope
    context.model_retry_budget.empty_response_retry_used = True
    model = Model([EmptyModelResponseError(), Message(role='assistant', content=summary())])
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', clock=iter([10, 20]).__next__)
    progress = ModelCallProgress('main', visible_output=True)
    token = model_call_progress.set(progress)
    try:
        result = await middleware._summary_call(state, context, [Message(role='user', content='source')])
        assert result == summary() and model_call_progress.get() is progress
    finally:
        model_call_progress.reset(token)
    assert [s.compression.last_api_at for s in saved] == [10.0, 20.0]
    assert not events and len(model.requests) == 2
    assert context.model_retry_budget.empty_response_retry_used


@pytest.mark.anyio
async def test_snip_rejects_foreign_context_and_partial_group_in_tool_loop(scope):
    state, context, _, _ = scope
    state.messages = history()
    call = ToolCall('snip', 'snip', {'message_ids': [state.messages[1].id]})
    state.messages.append(Message(role='assistant', content='', tool_calls=[call]))
    tool = SnipTool(); tool.bind(state, context)
    with pytest.raises(ValueError):
        await tool.execute(call, replace(context, run_id='foreign'))
    registry = ToolRegistry(); registry.register(tool)
    # ToolExecutor and its normal error boundary report invalid targets as a tool error.
    from app.agents.middleware import MiddlewareManager
    manager = MiddlewareManager(build_runtime_middlewares())
    result = await manager.wrap_tool_call(state, context, call, lambda: ToolExecutor(registry).execute(call, context))
    assert result.is_error and not state.compression.snipped_ids


@pytest.mark.anyio
async def test_idle_missing_existing_file_never_clears(scope):
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='old')]
    for _ in range(6):
        state.messages.extend(interaction())
    state.messages.append(Message(role='user', content='new'))
    state.messages[2].tool_result_file = tool_result_store.new_result_path()
    state.compression.last_api_at = 1.0
    middleware = ContextCompressionMiddleware(Model(), ToolRegistry(), model_key='m', clock=lambda: 4000)
    with pytest.raises(StatePersistenceError):
        await middleware.prepare_model_messages(state, context, state.messages)
    assert not state.compression.cleared_results


def test_policy_environment_validation(monkeypatch):
    monkeypatch.setenv('DEER_MINI_CONTEXT_ENABLED', 'false')
    monkeypatch.setenv('DEER_MINI_CONTEXT_SNIP_ENABLED', '0')
    monkeypatch.setenv('DEER_MINI_CONTEXT_CONTEXT_WINDOW', '200000')
    assert CompressionPolicy.from_env().context_window == 200000
    assert not CompressionPolicy.from_env().enabled and not CompressionPolicy.from_env().snip_enabled
    monkeypatch.setenv('DEER_MINI_CONTEXT_IDLE_SECONDS', 'no')
    with pytest.raises(ValueError):
        CompressionPolicy.from_env()


@pytest.mark.anyio
async def test_summary_chunks_apply_known_usage_ratio_and_cover_all_input(scope):
    from app.context_compression.middleware import transcript
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='requirements ' * 9000), Message(role='user', content='current')]
    estimate = request_tokens(state.messages, [], replay_reasoning=True)
    state.compression = CompressionState(usage_model='m', last_request_estimate=estimate, last_prompt_tokens=3 * estimate)
    model = Model()
    policy = CompressionPolicy(context_window=20000, compact_remaining=5000, summary_tokens=1000)
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=policy)
    source = transcript(state.messages)
    await middleware.prepare_model_messages(state, context, state.messages)
    assert len(model.requests) > 2
    blocks = [r['messages'][-1].content.split('\n\n接续历史资料：\n', 1)[1] for r in model.requests]
    assert ''.join(blocks) == source
    assert all(request_tokens(r['messages'], [], replay_reasoning=False) * 3 + policy.summary_tokens * 3 + 256 <= policy.context_window for r in model.requests)


def test_summary_rejects_duplicate_empty_and_oversize_sections():
    for value in (summary().replace('1. 主要请求与意图', '1. 主要请求与意图\n1. 主要请求与意图'), summary(''), summary('a' * 10000)):
        with pytest.raises(ValueError):
            validate_summary(Message(role='assistant', content=value), 1000)


@pytest.mark.anyio
async def test_snip_keeps_known_calibration_before_next_real_model_call(scope):
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='old'), Message(role='user', content='x' * 10000)]
    call = ToolCall('snip-short', 'snip', {'message_ids': [state.messages[0].id]})
    class UnderestimatedModel(Model):
        async def chat(self, **kwargs):
            self.last_prompt_tokens = 2 * request_tokens(kwargs['messages'], kwargs['tools'], replay_reasoning=True)
            if not self.requests:
                assert self.last_prompt_tokens < 10000
            return await super().chat(**kwargs)
    model = UnderestimatedModel([Message(role='assistant', content='', reasoning_content='r' * 6000, tool_calls=[call]), Message(role='assistant', content='unsafe request was sent')])
    registry = ToolRegistry(); registry.register(SnipTool())
    middleware = ContextCompressionMiddleware(model, registry, model_key='m', policy=compact_policy())
    agent = LeadAgent(model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([middleware]))
    with pytest.raises(ContextBudgetExceeded):
        await agent.run(state, context)
    assert len(model.requests) == 1
    assert state.compression.snipped_ids == [state.messages[0].id]


@pytest.mark.anyio
@pytest.mark.parametrize('restore_json', [False, True])
async def test_idle_retains_calibration_and_blocks_known_oversize_next_call(scope, restore_json):
    state, context, saved, _ = scope
    state.messages = [Message(role='user', content='old')]
    for _ in range(6):
        state.messages.extend(interaction(content='small'))
    state.messages.append(Message(role='user', content='x' * 17000))
    state.compression = CompressionState(usage_model='m', last_request_estimate=4500, last_prompt_tokens=9000, last_api_at=1.0)
    model = Model([Message(role='assistant', content='unsafe request was sent')])
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=compact_policy(), clock=lambda: 4000)
    await middleware._clear_idle(state, context)
    assert state.compression.cleared_results
    if restore_json:
        state = ThreadState.from_dict(saved[-1].to_dict())
    # A summary can be attempted, but the protected current input must never reach a normal API call.
    model.replies.clear()
    agent = LeadAgent(model, middleware.registry, ToolExecutor(middleware.registry), middlewares=build_runtime_middlewares([middleware]))
    with pytest.raises(ContextBudgetExceeded):
        await agent.run(state, context)
    assert all('max_output_tokens' in request for request in model.requests)


@pytest.mark.anyio
async def test_compact_reset_keeps_calibration_for_later_request(scope):
    state, context, _, _ = scope
    state.messages = [Message(role='user', content='old work ' * 5000), Message(role='user', content='current')]
    state.compression = CompressionState(usage_model='m', last_request_estimate=4500, last_prompt_tokens=9000)
    model = Model()
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='m', policy=compact_policy())
    await middleware.prepare_model_messages(state, context, state.messages)
    assert state.compression.summary
    state = ThreadState.from_dict(state.to_dict())
    state.messages.append(Message(role='user', content='new current ' * 1500))
    with pytest.raises(ContextBudgetExceeded):
        await middleware.prepare_model_messages(state, context, state.messages)


def test_calibration_survives_missing_usage_and_stays_model_scoped():
    from app.context_compression.budget import calibrated_tokens, record_usage, reset_request_baseline
    metadata = CompressionState.model_validate({'usage_model': 'a', 'last_prompt_tokens': 9000, 'last_request_estimate': 4500})
    reset_request_baseline(metadata)
    assert metadata.last_prompt_tokens is None and metadata.last_request_estimate == 0
    assert calibrated_tokens(6000, metadata, 'a') == 12000
    assert calibrated_tokens(6000, metadata, 'b') == 6000
    record_usage(metadata, 'a', 1000, None)
    assert calibrated_tokens(6000, metadata, 'a') == 12000
    record_usage(metadata, 'a', 1000, 1000)
    assert calibrated_tokens(6000, metadata, 'a') == 12000
    record_usage(metadata, 'b', 1000, 1500)
    assert calibrated_tokens(6000, metadata, 'b') == 9000
    assert calibrated_tokens(6000, metadata, 'a') == 6000
    restored = CompressionState.model_validate_json(metadata.model_dump_json())
    reset_request_baseline(restored)
    assert calibrated_tokens(6000, restored, 'b') == 9000


@pytest.mark.parametrize('ratio', [True, 0.5, float('nan'), float('inf')])
def test_invalid_persisted_calibration_rejected(ratio):
    with pytest.raises(ValueError):
        CompressionState(calibration_model='m', calibration_ratio=ratio)


@pytest.mark.anyio
async def test_summary_new_model_usage_survives_compact_and_later_missing_usage(scope):
    from app.context_compression.budget import calibrated_tokens
    state, context, saved, _ = scope
    state.messages = [Message(role='user', content='old work ' * 5000), Message(role='user', content='current')]
    state.compression = CompressionState(usage_model='old-model', last_request_estimate=1000, last_prompt_tokens=4000)
    class SummaryUsageModel(Model):
        async def chat(self, **kwargs):
            self.last_prompt_tokens = 2 * request_tokens(kwargs['messages'], kwargs['tools'], replay_reasoning=False)
            return await super().chat(**kwargs)
    model = SummaryUsageModel()
    middleware = ContextCompressionMiddleware(model, ToolRegistry(), model_key='new-model', policy=compact_policy())
    await middleware.prepare_model_messages(state, context, state.messages)
    assert state.compression.summary
    assert calibrated_tokens(6000, state.compression, 'new-model') == 12000
    assert state.compression.calibration_model == 'new-model'
    state = ThreadState.from_dict(saved[-1].to_dict())
    model.last_prompt_tokens = None
    async def answer():
        return Message(role='assistant', content='done')
    await middleware.wrap_model_call(state, context, answer)
    assert calibrated_tokens(6000, state.compression, 'new-model') == 12000
    assert all(saved_state.compression.calibration_ratio >= 1 for saved_state in saved)
