"""没有 Docker CLI 时只允许已确认不存在遗留容器的部署继续启动。"""
import importlib
import pytest
from app.infrastructure import database

@pytest.fixture
def safety(tmp_path,monkeypatch):
    monkeypatch.setattr(database,'DATABASE_PATH',tmp_path/'sandbox.db')
    database.initialize_database()
    module=importlib.import_module('app.repositories.sandbox_safety_repository')
    return module.SandboxSafetyRepository()

def test_never_enabled_bash_does_not_require_docker(safety):
    safety.require_clear_without_docker()

def test_crashed_bash_session_requires_cleanup(safety):
    safety.mark_active()
    with pytest.raises(RuntimeError,match='Docker'):
        safety.require_clear_without_docker()
    safety.mark_clear()
    safety.require_clear_without_docker()

def test_startup_without_cli_checks_durable_guard(safety,monkeypatch):
    import asyncio
    import app.sandbox.manager as module
    safety.mark_active()
    monkeypatch.setattr(module.shutil,'which',lambda binary: None)
    with pytest.raises(RuntimeError,match='Docker'):
        asyncio.run(module.cleanup_orphaned_thread_sandboxes())


def test_failed_startup_shutdown_cannot_clear_orphan_guard(safety):
    import asyncio
    from app.services.run_coordinator import RunCoordinator
    from app.runtime.stream_bridge import MemoryStreamBridge
    class Manager:
        async def start(self): raise RuntimeError('orphan sweep failed')
        async def close(self): pass
    async def scenario():
        bridge=MemoryStreamBridge()
        coordinator=RunCoordinator(bridge,bash_runner=None)
        coordinator._sandbox_manager=Manager()
        with pytest.raises(RuntimeError,match='orphan sweep'):
            await coordinator.start()
        await coordinator.shutdown()
        with pytest.raises(RuntimeError,match='Docker'):
            safety.require_clear_without_docker()
        await bridge.close()
    asyncio.run(scenario())
