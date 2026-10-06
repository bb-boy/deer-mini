"""文件恢复使用独立数据库与工作目录，故障注入不访问模型和真实用户文件。"""
from copy import deepcopy
from pathlib import Path
import importlib
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
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(thread_service, 'DATA_ROOT', tmp_path / 'users')
    database.initialize_database()
    thread = ThreadService().create_thread('alice')
    run = RunService().create_run('alice', thread.id, 'mock')
    state = ThreadState(thread_id=thread.id, user_id='alice', workspace_path=thread.workspace_path)
    module = importlib.import_module('app.services.file_checkpoint_service')
    return module.FileCheckpointService(), thread, run, state

def setup_point(env):
    service, thread, run, state = env
    workspace = Path(thread.workspace_path)
    (workspace / 'a.txt').write_text('before')
    (workspace.parent / 'uploads' / 'old.bin').write_bytes(b'\x00\xff')
    point = service.capture_turn(thread, run, state, 'change files')
    state.messages.append(Message(role='user', content='change files'))
    CheckpointRepository().save(Checkpoint(thread.id,run.id,1,deepcopy(state)))
    (workspace / 'a.txt').write_text('after')
    (workspace / 'new.txt').write_text('new')
    return point

def test_restore_first_turn_and_continue_from_current_head(env):
    service, thread, run, state = env
    point = setup_point(env)
    preview = service.preview(thread, point['id'])
    assert preview['modified'] == ['workspace/a.txt']
    assert preview['deleted'] == ['workspace/new.txt']
    assert preview['removed_messages'] == 1
    result = service.restore(thread, 'operation-one', point['id'], preview['revision'], preview['fingerprint'])
    assert result['status'] == 'committed'
    assert Path(thread.workspace_path, 'a.txt').read_text() == 'before'
    assert not Path(thread.workspace_path, 'new.txt').exists()
    repo = CheckpointRepository()
    assert repo.latest(thread.id,'alice').state.messages == []
    assert len(repo.latest_for_run(thread.id,run.id,'alice').state.messages) == 1
    again = service.restore(thread, 'operation-one', point['id'], preview['revision'], preview['fingerprint'])
    assert again['operation_id'] == result['operation_id']
    recovery = service.preview(thread,result['recovery_point_id'])
    service.restore(thread,'undo-restore',result['recovery_point_id'],recovery['revision'],recovery['fingerprint'])
    assert Path(thread.workspace_path,'a.txt').read_text() == 'after'

def test_stale_preview_does_not_overwrite_later_upload(env):
    service, thread, _, _ = env
    point = setup_point(env)
    preview = service.preview(thread,point['id'])
    upload = Path(thread.workspace_path).parent / 'uploads' / 'later.txt'
    upload.write_text('keep me')
    with pytest.raises(RuntimeError, match='预览'):
        service.restore(thread,'stale',point['id'],preview['revision'],preview['fingerprint'])
    assert upload.read_text() == 'keep me'

def test_history_tool_results_survive_restore(env):
    service, thread, _, _ = env
    point = setup_point(env)
    history = Path(thread.workspace_path,'.tool-results')
    history.mkdir()
    (history/'old.txt').write_text('historical result')
    preview = service.preview(thread,point['id'])
    service.restore(thread,'history',point['id'],preview['revision'],preview['fingerprint'])
    assert (history/'old.txt').read_text() == 'historical result'

@pytest.mark.parametrize('phase', ['old_uploads','new_uploads','old_workspace','new_workspace','old_outputs','new_outputs','before_commit'])
def test_failed_directory_swap_rolls_back(env, monkeypatch, phase):
    service, thread, _, _ = env
    point = setup_point(env)
    preview = service.preview(thread,point['id'])
    def fail(stage):
        if stage == phase:
            raise OSError('injected')
    monkeypatch.setattr(service,'_fault',fail)
    result = service.restore(thread,'failure',point['id'],preview['revision'],preview['fingerprint'])
    assert result['status'] == 'rolled_back'
    assert Path(thread.workspace_path,'a.txt').read_text() == 'after'
    assert Path(thread.workspace_path,'new.txt').read_text() == 'new'
    assert len(CheckpointRepository().latest(thread.id,'alice').state.messages) == 1

@pytest.mark.parametrize('phase', ['old_uploads','new_uploads','old_workspace','new_workspace','old_outputs','new_outputs','before_commit','after_commit'])
def test_restart_recovers_interrupted_swap(env, monkeypatch, phase):
    service, thread, _, _ = env
    point = setup_point(env)
    preview = service.preview(thread,point['id'])
    class SimulatedCrash(BaseException): pass
    def crash(stage):
        if stage == phase: raise SimulatedCrash()
    monkeypatch.setattr(service,'_fault',crash)
    with pytest.raises(SimulatedCrash):
        service.restore(thread,'crash',point['id'],preview['revision'],preview['fingerprint'])
    clean = type(service)()
    assert clean.recover_pending() == []
    op = clean.operation(thread,'crash')
    expected = 'before' if phase == 'after_commit' else 'after'
    assert Path(thread.workspace_path,'a.txt').read_text() == expected
    assert op['status'] == ('committed' if phase == 'after_commit' else 'rolled_back')

def test_capacity_rejection_keeps_existing_restore_points(env, monkeypatch):
    service, thread, run, state = env
    point = setup_point(env)
    before = service.list_points(thread)
    service.max_bytes = 1
    with pytest.raises(ValueError, match='容量'):
        service.capture_turn(thread,run,state,'too large')
    assert service.list_points(thread) == before
    assert before[0]['id'] == point['id']

@pytest.mark.parametrize('terminal', ['committed','rolled_back'])
def test_restart_after_staging_cleanup_before_cleaned_flag(env, monkeypatch, terminal):
    service, thread, _, _ = env
    point=setup_point(env)
    preview=service.preview(thread,point['id'])
    original=service.repo.update
    class Crash(BaseException): pass
    def update(*args,**kwargs):
        if kwargs.get('cleaned'): raise Crash()
        return original(*args,**kwargs)
    monkeypatch.setattr(service.repo,'update',update)
    if terminal=='rolled_back':
        def fail(phase):
            if phase=='new_workspace': raise OSError('injected')
        monkeypatch.setattr(service,'_fault',fail)
    with pytest.raises(Crash):
        service.restore(thread,'cleanup-crash',point['id'],preview['revision'],preview['fingerprint'])
    clean=type(service)()
    assert clean.recover_pending()==[]
    assert clean.operation(thread,'cleanup-crash')['status']==terminal
    assert clean.operation(thread,'cleanup-crash')['cleaned']


def test_retention_evicts_old_points_and_preserves_shared_content(env):
    service,thread,run,state=env
    service.max_points=2
    workspace=Path(thread.workspace_path)
    (workspace/'same.bin').write_bytes(b'shared')
    points=[]
    for number in range(4):
        (workspace/'version.txt').write_text(str(number))
        points.append(service.capture_turn(thread,run,state,str(number)))
    available=[p for p in service.list_points(thread) if p['available']]
    assert [p['id'] for p in available]==[points[3]['id'],points[2]['id']]
    store=service._store(thread)
    import json
    for point in service.repo.points(thread): store.validate(json.loads(point['manifest_json']))
    assert len(list((store.root/'objects').glob('*/*')))==3


def test_capacity_can_evict_prior_content_after_candidate_is_durable(env):
    service,thread,run,state=env
    workspace=Path(thread.workspace_path)
    service.max_bytes=3000
    (workspace/'big').write_bytes(b'a'*1500)
    first=service.capture_turn(thread,run,state,'first')
    (workspace/'big').write_bytes(b'b'*1500)
    second=service.capture_turn(thread,run,state,'second')
    available=[p for p in service.list_points(thread) if p['available']]
    assert [p['id'] for p in available]==[second['id']]
    assert first['id']!=second['id']


def test_read_only_target_workspace_preserves_history_and_cleans_staging(env):
    service,thread,run,state=env
    workspace=Path(thread.workspace_path)
    (workspace/'old').write_text('old')
    workspace.chmod(0o500)
    point=service.capture_turn(thread,run,state,'before')
    workspace.chmod(0o700)
    history=workspace/'.tool-results'
    history.mkdir()
    (history/'result').write_text('retained')
    preview=service.preview(thread,point['id'])
    result=service.restore(thread,'permissions',point['id'],preview['revision'],preview['fingerprint'])
    assert result['status']=='committed' and result['cleaned']
    assert workspace.stat().st_mode & 0o777==0o500
    assert (history/'result').read_text()=='retained'
    workspace.chmod(0o700)


def test_cleanup_handles_read_only_old_subdirectories(env):
    service,thread,_,_=env
    point=setup_point(env)
    directory=Path(thread.workspace_path,'read-only')
    directory.mkdir()
    (directory/'file').write_text('old file')
    directory.chmod(0o500)
    preview=service.preview(thread,point['id'])
    result=service.restore(thread,'read-only-cleanup',point['id'],preview['revision'],preview['fingerprint'])
    assert result['status']=='committed' and result['cleaned']
    assert not directory.exists()


def test_unprepared_build_failure_reclaims_staging(env,monkeypatch):
    service,thread,_,_=env
    point=setup_point(env)
    preview=service.preview(thread,point['id'])
    def fail_build(thread,store,manifest,destination,stage):
        destination.mkdir()
        (destination/'partial').write_bytes(b'partial')
        raise OSError('build failed')
    monkeypatch.setattr(service,'_build',fail_build)
    with pytest.raises(OSError):
        service.restore(thread,'build-failure',point['id'],preview['revision'],preview['fingerprint'])
    assert not (service._store(thread).root/'staging'/'build-failure').exists()
    assert Path(thread.workspace_path,'a.txt').read_text()=='after'


def test_restart_removes_unprepared_staging_after_crash(env,monkeypatch):
    service,thread,_,_=env
    point=setup_point(env)
    preview=service.preview(thread,point['id'])
    class Crash(BaseException): pass
    def crash_build(thread,store,manifest,destination,stage):
        destination.mkdir()
        (destination/'partial').write_bytes(b'partial')
        raise Crash()
    monkeypatch.setattr(service,'_build',crash_build)
    with pytest.raises(Crash):
        service.restore(thread,'build-crash',point['id'],preview['revision'],preview['fingerprint'])
    clean=type(service)()
    assert clean.recover_pending()==[]
    assert not (clean._store(thread).root/'staging'/'build-crash').exists()


def test_restore_cancelled_request_waits_for_persistence_and_releases_gate(env,monkeypatch):
    import asyncio
    import threading
    from app.services.run_coordinator import RunCoordinator
    from app.runtime.stream_bridge import MemoryStreamBridge
    service,thread,_,_=env
    point=setup_point(env)
    RunService().interrupt_run(env[2].id,'alice')
    preview=service.preview(thread,point['id'])
    entered=threading.Event()
    proceed=threading.Event()
    real=service.restore
    def delayed(*args):
        entered.set()
        assert proceed.wait(5)
        return real(*args)
    monkeypatch.setattr(service,'restore',delayed)
    async def scenario():
        bridge=MemoryStreamBridge()
        coordinator=RunCoordinator(bridge,bash_runner=None)
        coordinator.file_checkpoints=service
        task=asyncio.create_task(coordinator.restore_thread(user_id='alice',thread_id=thread.id,
            operation_id='cancel-http',point_id=point['id'],revision=preview['revision'],fingerprint=preview['fingerprint']))
        await asyncio.to_thread(entered.wait,5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(RuntimeError):
            with coordinator.operations.write('alice',thread.id,'upload'): pass
        proceed.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert service.operation(thread,'cancel-http')['status']=='committed'
        with coordinator.operations.write('alice',thread.id,'upload'): pass
        await bridge.close()
    asyncio.run(scenario())


def test_rolled_back_recovery_cleanup_db_failure_keeps_terminal_state(env,monkeypatch):
    service,thread,_,_=env
    point=setup_point(env)
    preview=service.preview(thread,point['id'])
    def fail(phase):
        if phase=='new_workspace': raise OSError('injected')
    monkeypatch.setattr(service,'_fault',fail)
    class Crash(BaseException): pass
    original=service.repo.update
    def crash_after_cleanup(*args,**kwargs):
        if kwargs.get('cleaned'): raise Crash()
        return original(*args,**kwargs)
    monkeypatch.setattr(service.repo,'update',crash_after_cleanup)
    with pytest.raises(Crash):
        service.restore(thread,'terminal-retry',point['id'],preview['revision'],preview['fingerprint'])
    clean=type(service)()
    update=clean.repo.update
    def fail_cleaned_once(*args,**kwargs):
        if kwargs.get('cleaned'): raise OSError('db temporarily unavailable')
        return update(*args,**kwargs)
    monkeypatch.setattr(clean.repo,'update',fail_cleaned_once)
    assert clean.recover_pending()==[('alice',thread.id)]
    assert clean.operation(thread,'terminal-retry')['status']=='rolled_back'
    again=type(service)()
    assert again.recover_pending()==[]
    assert again.operation(thread,'terminal-retry')['cleaned']


def test_capture_wraps_classified_database_failure(env, monkeypatch):
    import sqlite3
    from app.storage.errors import classify_sqlite_error
    from app.runtime.errors import StatePersistenceError
    service, thread, run, state = env
    error = classify_sqlite_error(sqlite3.OperationalError('database is locked'), operation='write', stage='execute')
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(service, '_capture', fail)
    with pytest.raises(StatePersistenceError) as caught:
        service.capture_turn(thread, run, state, 'test')
    assert caught.value.__cause__ is error
