"""生产 runtime 的文件恢复点必须先于任何模型或工具写入。"""
import asyncio
from pathlib import Path
import pytest
from app.infrastructure import database
from app.services import thread_service
from app.services.thread_service import ThreadService
from app.services.run_service import RunService
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.file_checkpoint_service import FileCheckpointService

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database,'DATABASE_PATH',tmp_path/'runtime.db')
    monkeypatch.setattr(thread_service,'DATA_ROOT',tmp_path/'users')
    database.initialize_database()
    thread = ThreadService().create_thread('alice')
    run = RunService().create_run('alice',thread.id,'mock')
    return thread,run

def test_turn_start_precedes_user_message_and_model(env):
    thread, run = env
    service = FileCheckpointService()
    class Agent:
        async def run(self,state,context):
            points = service.list_points(thread)
            assert len(points)==1 and points[0]['available']
            preview = service.preview(thread,points[0]['id'])
            assert preview['target_messages']==0
            assert len(state.messages)==1
            Path(thread.workspace_path,'made.txt').write_text('new')
            return state
    async def execute():
        bridge = MemoryStreamBridge()
        try:
            await AgentRuntime(bridge,file_checkpoints=service).run('alice',thread.id,run.id,'write',Agent())
        finally: await bridge.close()
    asyncio.run(execute())

def test_snapshot_failure_never_executes_model(env):
    thread, run = env
    Path(thread.workspace_path,'bad').symlink_to('/tmp')
    class Agent:
        async def run(self,state,context): raise AssertionError('must not execute')
    async def execute():
        bridge = MemoryStreamBridge()
        try:
            with pytest.raises(ValueError):
                await AgentRuntime(bridge,file_checkpoints=FileCheckpointService()).run('alice',thread.id,run.id,'write',Agent())
        finally: await bridge.close()
    asyncio.run(execute())
    assert RunService().get_run(run.id,'alice').status=='error'
