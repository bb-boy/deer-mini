"""恢复接口归属、版本、幂等与文件准入测试，全部使用临时数据库。"""
from copy import deepcopy
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from app.api.routes import router
from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService
from app.services.run_service import RunService

@pytest.fixture
def case(tmp_path,monkeypatch):
    monkeypatch.setattr(database,'DATABASE_PATH',tmp_path/'api.db')
    monkeypatch.setattr(thread_service,'DATA_ROOT',tmp_path/'users')
    database.initialize_database()
    thread=ThreadService().create_thread('alice')
    run=RunService().create_run('alice',thread.id,'mock')
    state=ThreadState(thread.id,'alice',workspace_path=thread.workspace_path)
    coordinator=RunCoordinator(MemoryStreamBridge(),bash_runner=None)
    point=coordinator.file_checkpoints.capture_turn(thread,run,state,'make file')
    state.messages.append(Message(role='user',content='make file'))
    CheckpointRepository().save(Checkpoint(thread.id,run.id,1,deepcopy(state)))
    RunService().interrupt_run(run.id,'alice')
    Path(thread.workspace_path,'new.txt').write_text('new')
    app=FastAPI()
    app.state.run_coordinator=coordinator
    app.state.thread_operations=coordinator.operations
    app.include_router(router)
    with TestClient(app) as client:
        yield client,thread,point,coordinator

def test_api_restore_and_idempotency(case):
    client,thread,point,_=case
    base=f'/api/threads/{thread.id}'
    listing=client.get(base+'/restore-points',params={'user_id':'alice'})
    assert listing.status_code==200
    assert listing.json()[0]['id']==point['id']
    preview=client.post(base+f'/restore-points/{point["id"]}/preview',params={'user_id':'alice'})
    assert preview.status_code==200
    body={'operation_id':'api-restore','restore_point_id':point['id'],
          'revision':preview.json()['revision'],'fingerprint':preview.json()['fingerprint']}
    restored=client.post(base+'/restore',params={'user_id':'alice'},json=body)
    assert restored.status_code==200,restored.text
    assert restored.json()['status']=='committed'
    assert not Path(thread.workspace_path,'new.txt').exists()
    assert client.post(base+'/restore',params={'user_id':'alice'},json=body).json()==restored.json()
    assert client.get(base+'/restore-operations/api-restore',params={'user_id':'alice'}).json()==restored.json()

@pytest.mark.parametrize('method,suffix', [('get','/restore-points'),('post','/restore-points/point/preview'),('get','/restore-operations/op'),('post','/restore')])
def test_new_endpoints_check_ownership(case,method,suffix):
    client,thread,_,_=case
    body={'operation_id':'owned','restore_point_id':'x','revision':1,'fingerprint':'0'*64}
    response=client.request(method,f'/api/threads/{thread.id}'+suffix,params={'user_id':'bob'},json=body if suffix=='/restore' else None)
    assert response.status_code==404

def test_stale_preview_returns_conflict(case):
    client,thread,point,_=case
    base=f'/api/threads/{thread.id}'
    p=client.post(base+f'/restore-points/{point["id"]}/preview',params={'user_id':'alice'}).json()
    Path(thread.workspace_path,'late.txt').write_text('keep')
    response=client.post(base+'/restore',params={'user_id':'alice'},json={
        'operation_id':'stale','restore_point_id':point['id'],'revision':p['revision'],'fingerprint':p['fingerprint']})
    assert response.status_code==409
    assert Path(thread.workspace_path,'late.txt').read_text()=='keep'

def test_restoring_blocks_file_read_upload_and_new_run(case):
    client,thread,_,coordinator=case
    base=f'/api/threads/{thread.id}'
    with coordinator.operations.write('alice',thread.id,'restore'):
        assert client.get(base+'/files',params={'user_id':'alice'}).status_code==409
        assert client.get(base+'/files/workspace/new.txt',params={'user_id':'alice'}).status_code==409
        assert client.post(base+'/files',params={'user_id':'alice'},files={'file':('x.txt',b'x')}).status_code==409
        assert client.post(base+'/runs',json={'user_id':'alice','message':'x','model_name':'mock'}).status_code==409
