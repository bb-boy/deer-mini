"""恢复点要求真正停止容器，并保护消息引用的历史工具结果。"""
import asyncio
from pathlib import Path
import pytest
from app.sandbox.manager import ThreadSandboxManager
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
from app.sandbox.models import SandboxEntry

class Runner:
    config = DockerRunnerConfig()
    def __init__(self): self.removed=[]; self.fail=False
    async def remove_container(self, name):
        if self.fail: raise RuntimeError('stop failed')
        self.removed.append(name)

def test_stop_thread_removes_warm_container_and_keeps_failed_record(tmp_path):
    async def scenario():
        runner = Runner()
        manager = ThreadSandboxManager(runner)
        key=('alice','thread')
        manager._warm_pool[key]=SandboxEntry('alice','thread',str(tmp_path),container_id='old')
        runner.fail=True
        with pytest.raises(RuntimeError): await manager.stop_thread(user_id='alice',thread_id='thread')
        assert key in manager._warm_pool
        runner.fail=False
        await manager.stop_thread(user_id='alice',thread_id='thread')
        assert runner.removed==['old'] and key not in manager._warm_pool
    asyncio.run(scenario())

def test_tool_result_directory_is_a_nested_read_only_mount(tmp_path):
    for area in ('workspace','uploads','outputs'): (tmp_path/area).mkdir()
    runner= DockerCommandRunner(DockerRunnerConfig())
    _,args=runner.build_run_args(command='pwd',workspace_path=str(tmp_path/'workspace'),run_id='r',tool_call_id='t')
    mounts=[args[i+1] for i,v in enumerate(args) if v=='--mount']
    assert any('target=/mnt/user-data/workspace/.tool-results,readonly' in mount for mount in mounts)
    assert not any('.checkpoints' in mount for mount in mounts)

def test_reject_linked_history_directory(tmp_path):
    for area in ('workspace','uploads','outputs'): (tmp_path/area).mkdir()
    (tmp_path/'workspace'/'.tool-results').symlink_to(tmp_path/'uploads')
    runner= DockerCommandRunner(DockerRunnerConfig())
    with pytest.raises(ValueError):
        runner.build_run_args(command='pwd',workspace_path=str(tmp_path/'workspace'),run_id='r',tool_call_id='t')


def test_disabled_bash_startup_does_not_swallow_cleanup_failure(monkeypatch):
    import app.sandbox.manager as module
    monkeypatch.setattr(module.shutil,'which',lambda binary: '/mock/docker')
    async def fail(*args): raise RuntimeError('cleanup failed')
    monkeypatch.setattr(module.DockerCommandRunner,'remove_owned_containers',fail)
    with pytest.raises(RuntimeError,match='cleanup failed'):
        asyncio.run(module.cleanup_orphaned_thread_sandboxes())
