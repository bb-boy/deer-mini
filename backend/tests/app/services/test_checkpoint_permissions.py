"""真实普通用户权限回归：解释器加载模块后降权，避免 root 掩盖目录权限错误。"""
import subprocess
import sys
import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="部署环境是 Linux")
def test_restore_and_cleanup_under_unprivileged_uid():
    result = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


PROBE = r'''
"""在已加载应用模块后降权，全部数据由 nobody 在新临时目录创建。"""
import os
import tempfile
from pathlib import Path
from app.infrastructure import database
from app.services.thread_service import ThreadService
from app.services.run_service import RunService
from app.services.file_checkpoint_service import FileCheckpointService
from app.domain.threads import ThreadState
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
root=Path(tempfile.mkdtemp(prefix='checkpoint-permission-probe-'))
os.environ['DEER_MINI_DATABASE_PATH']=str(root/'db.sqlite')
os.environ['DEER_MINI_DATA_ROOT']=str(root/'users')
database.initialize_database()
thread=ThreadService().create_thread('alice')
run=RunService().create_run('alice',thread.id,'mock')
state=ThreadState(thread.id,'alice',workspace_path=thread.workspace_path)
service=FileCheckpointService()
workspace=Path(thread.workspace_path)
(workspace/'file').write_text('before')
workspace.chmod(0o500)
point=service.capture_turn(thread,run,state,'test')
workspace.chmod(0o700)
(workspace/'file').write_text('after')
history=workspace/'.tool-results'
history.mkdir()
(history/'result').write_text('retained')
readonly=workspace/'readonly'
readonly.mkdir()
(readonly/'x').write_text('x')
readonly.chmod(0o500)
preview=service.preview(thread,point['id'])
op=service.restore(thread,'nonroot',point['id'],preview['revision'],preview['fingerprint'])
assert op['status']=='committed' and op['cleaned'],op
assert (workspace/'file').read_text()=='before'
assert workspace.stat().st_mode&0o777==0o500
assert (history/'result').read_text()=='retained'
assert service.recover_pending()==[]
print('Unprivileged permission and cleanup verification passed')

service._remove(root)
'''
