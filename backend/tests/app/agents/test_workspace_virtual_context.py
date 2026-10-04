"""模型上下文使用虚拟路径；后端保留真实路径，并替换旧运行上下文。"""
import asyncio

import pytest

from app.agents.workspace_context_middleware import RUNTIME_CONTEXT_PREFIX, WorkspaceContextMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition
from app.runtime.context import RuntimeContext
from app.tools.registry import ToolRegistry


@pytest.mark.parametrize('has_bash', [False, True])
def test_system_context_hides_host_path_and_replaces_legacy_context(tmp_path, has_bash):
    workspace = tmp_path / 'private-server-directory' / 'workspace'
    workspace.mkdir(parents=True)
    paths_before = str(workspace)
    context = RuntimeContext('user', 'thread', 'run', paths_before, None, None)
    old_context = Message(role='system', content=f'{RUNTIME_CONTEXT_PREFIX}\n服务端工作目录：{paths_before}\n')
    question = Message(role='user', content='读取 test-large.txt')
    state = ThreadState('thread', 'user', messages=[old_context, question], workspace_path=paths_before)
    registry = ToolRegistry()
    if has_bash:
        class Bash:
            definition = ToolDefinition('bash', '测试目录提示', {})
        registry.register(Bash())
    asyncio.run(WorkspaceContextMiddleware(registry).before_agent(state, context))
    system = state.messages[0].content
    assert paths_before not in system
    assert 'private-server-directory' not in system
    assert '/mnt/user-data/workspace' in system
    assert '/mnt/user-data/uploads' in system
    assert '/mnt/user-data/outputs' in system
    assert 'read_file' in system and 'test-large.txt' in system
    if has_bash:
        assert 'bash 默认从 /mnt/user-data/workspace' in system
        assert 'workspace/test-large.txt' in system
    assert state.messages[1] is question
    assert len(state.messages) == 2
    assert context.workspace_path == paths_before
    assert state.workspace_path == paths_before
