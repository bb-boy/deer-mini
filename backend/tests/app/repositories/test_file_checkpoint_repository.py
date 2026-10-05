"""恢复状态与历史执行隔离的持久化回归。"""
import pytest
from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.services import thread_service
from app.services.thread_service import ThreadService
from app.services.run_service import RunService

@pytest.fixture
def records(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(thread_service, 'DATA_ROOT', tmp_path / 'users')
    database.initialize_database()
    thread = ThreadService().create_thread('alice')
    run = RunService().create_run('alice', thread.id, 'mock')
    return thread, run

def checkpoint(thread, run, step, text, **kwargs):
    return Checkpoint(thread_id=thread.id, run_id=run.id, step=step,
        state=ThreadState(thread_id=thread.id, user_id='alice',
            workspace_path=thread.workspace_path,
            messages=[Message(role='user', content=text)] if text else []), **kwargs)

def test_restore_head_does_not_replace_run_execution(records):
    thread, run = records
    repo = CheckpointRepository()
    initial = repo.save(checkpoint(thread, run, 0, '', kind='turn_start'))
    executed = repo.save(checkpoint(thread, run, 1, 'old run'))
    restored = repo.save(checkpoint(thread, run, 2, '', kind='restore'))
    assert repo.latest(thread.id, 'alice').id == restored.id
    assert repo.latest_for_run(thread.id, run.id, 'alice').id == executed.id
    assert [c.id for c in repo.history(thread.id, 'alice', run.id)] == [executed.id]
    assert repo.revision(thread.id, 'alice') == 3
    assert repo.next_step(thread.id, run.id) == 3
    assert repo.get(initial.id, thread.id, 'bob') is None

def test_background_save_cannot_advance_current_state(records):
    thread, run = records
    repo = CheckpointRepository()
    current = repo.save(checkpoint(thread, run, 1, 'current'))
    repo.save(checkpoint(thread, run, 2, 'background'), advance_head=False)
    assert repo.latest(thread.id, 'alice').id == current.id
    database.initialize_database()
    assert repo.latest(thread.id, 'alice').id == current.id

def test_foreign_state_cannot_be_saved(records):
    thread, run = records
    value = checkpoint(thread, run, 1, 'private')
    value.state.user_id = 'bob'
    with pytest.raises(ValueError):
        CheckpointRepository().save(value)

def test_migration_initializes_old_head_once(records):
    thread, run = records
    repo = CheckpointRepository()
    value = repo.save(checkpoint(thread, run, 1, 'legacy'))
    with database.connect() as conn:
        conn.execute('DELETE FROM thread_heads')
    database.initialize_database()
    assert repo.latest(thread.id, 'alice').id == value.id
