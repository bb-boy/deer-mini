"""合并原生文件工具后，验证真实副作用、压缩引用和 Snip 在同一循环中的行为。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from app.agents.lead_agent import LeadAgent
from app.agents.middleware_stack import build_runtime_middlewares
from app.agents.workspace_context_middleware import WorkspaceContextMiddleware
from app.context_compression.budget import request_tokens
from app.context_compression.middleware import ContextCompressionMiddleware
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService
from app.tools.executor import ToolExecutor


def test_native_mutations_idle_references_and_snip_share_real_loop(monkeypatch):
    import app.tools.edit_file as edit_module
    import app.tools.write_file as write_module
    monkeypatch.setenv('TAVILY_API_KEY', '')
    mutations = []
    original_write, original_edit = write_module._write, edit_module._edit

    def write(args, context):
        mutations.append('write_file')
        return original_write(args, context)

    def edit(args, context):
        mutations.append('edit_file')
        return original_edit(args, context)

    monkeypatch.setattr(write_module, '_write', write)
    monkeypatch.setattr(edit_module, '_edit', edit)
    thread = ThreadService().create_thread('alice')
    state = ThreadState(thread.id, 'alice', workspace_path=thread.workspace_path,
                        messages=[Message(role='user', content='create and edit report')])
    saved = []

    async def save(snapshot):
        saved.append(deepcopy(snapshot))

    async def event(*args):
        pass

    native_calls = [
        ToolCall('write', 'write_file', {'path': 'outputs/report.txt', 'content': 'draft\n'}),
        ToolCall('read-draft', 'read_file', {'path': 'outputs/report.txt'}),
        ToolCall('edit', 'edit_file', {'path': 'outputs/report.txt', 'old_string': 'draft', 'new_string': 'final'}),
        ToolCall('read-final', 'read_file', {'path': 'outputs/report.txt'}),
        ToolCall('glob-1', 'glob', {'path': 'outputs', 'pattern': '**/*.txt'}),
        ToolCall('grep-1', 'grep', {'path': 'outputs', 'pattern': 'final'}),
        ToolCall('read-again', 'read_file', {'path': 'outputs/report.txt'}),
        ToolCall('glob-2', 'glob', {'path': 'outputs', 'pattern': '**/*.txt'}),
        ToolCall('grep-2', 'grep', {'path': 'outputs', 'pattern': 'final'}),
    ]

    class NativeModel:
        calls = 0
        last_prompt_tokens = None

        async def chat(self, messages, tools, **kwargs):
            self.calls += 1
            self.last_prompt_tokens = 2 * request_tokens(messages, tools, replay_reasoning=True)
            assert {'write_file', 'edit_file', 'read_file', 'glob', 'grep', 'read_tool_result', 'snip'} <= {tool.name for tool in tools}
            if self.calls == 1:
                return Message(role='assistant', content='', reasoning_content='keep native reasoning', tool_calls=native_calls)
            results = [m for m in messages if m.role == 'tool']
            assert len(results) == 9 and all(not m.is_error for m in results)
            assert results[1].content == 'draft\n' and results[3].content == 'final\n'
            return Message(role='assistant', content='file ready')

        async def close(self):
            pass

    async def scenario():
        coordinator = RunCoordinator(MemoryStreamBridge(), bash_runner=None)
        registry = coordinator._build_tool_registry()
        context = RuntimeContext('alice', thread.id, 'run-native', thread.workspace_path, event, save)
        native_model = NativeModel()
        compression = ContextCompressionMiddleware(native_model, registry, model_key='offline', clock=lambda: 100)
        await LeadAgent(native_model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([
            WorkspaceContextMiddleware(registry), compression,
        ])).run(state, context)
        original = deepcopy([m for m in state.messages if m.role != 'system'])
        old_group = next(m for m in state.messages if m.tool_calls)
        old_results = [m for m in state.messages if m.role == 'tool']
        selected = [old_group.id, *[m.id for m in old_results]]
        state.messages.append(Message(role='user', content='retain evidence and remove obsolete context'))
        registry = coordinator._build_tool_registry()
        context = RuntimeContext('alice', thread.id, 'run-compress', thread.workspace_path, event, save)

        class CompressModel:
            calls = 0
            last_prompt_tokens = None

            async def chat(self, messages, tools, **kwargs):
                self.calls += 1
                self.last_prompt_tokens = 2 * request_tokens(messages, tools, replay_reasoning=True)
                if self.calls == 1:
                    assert set(state.compression.cleared_results) == {m.id for m in old_results[:4]}
                    projected = {m.id: m for m in messages}
                    assert all('read_tool_result' in projected[m.id].content for m in old_results[:4])
                    assert all(m.content in projected[m.id].content for m in old_results[4:])
                    assert projected[old_group.id].reasoning_content == 'keep native reasoning'
                    return Message(role='assistant', content='', tool_calls=[
                        ToolCall('recover-edit', 'read_tool_result', {'path': state.compression.cleared_results[old_results[2].id], 'limit': 50000}),
                        ToolCall('snip-native', 'snip', {'message_ids': selected}),
                    ])
                assert not ({m.id for m in messages} & set(selected))
                result = next(m for m in messages if m.tool_call_id == 'recover-edit')
                assert not result.is_error
                diff = json.loads(result.content)['diff']
                assert '-draft' in diff and '+final' in diff
                return Message(role='assistant', content='history preserved')

            async def close(self):
                pass

        model = CompressModel()
        compression = ContextCompressionMiddleware(model, registry, model_key='offline', clock=lambda: 4001)
        await LeadAgent(model, registry, ToolExecutor(registry), middlewares=build_runtime_middlewares([
            WorkspaceContextMiddleware(registry), compression,
        ])).run(state, context)
        assert native_model.calls == model.calls == 2
        assert [m for m in state.messages if m.role != 'system'][:len(original)] == original
        assert state.compression.snipped_ids == selected
        assert state.compression.calibration_ratio == 2
        restored = ThreadState.from_dict(saved[-1].to_dict())
        assert restored.compression == state.compression
        # Native read cannot access internal result storage; only the dedicated reader can.
        internal_path = state.compression.cleared_results[old_results[2].id]
        rejected = await ToolExecutor(registry).execute(ToolCall('internal', 'read_file', {'path': internal_path}), context)
        assert rejected.is_error

    asyncio.run(scenario())
    assert mutations == ['write_file', 'edit_file']
    assert (Path(thread.workspace_path).parent / 'outputs' / 'report.txt').read_text() == 'final\n'
