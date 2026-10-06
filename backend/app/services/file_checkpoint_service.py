"""整轮文件恢复：去重备份、保留策略及可重放的目录交换日志。

输入只接受已验证归属的 Thread 和恢复点编号；所有磁盘工作由调用方在线程执行。
成功必须同时确认文件目标和 SQLite 当前指针，失败保留恢复前备份。
"""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from copy import deepcopy

from app.domain.checkpoints import Checkpoint
from app.domain.runs import Run
from app.domain.threads import Thread, ThreadState
from app.filesystem.file_snapshots import FileSnapshotStore, manifest_bytes, fingerprint
from app.filesystem.thread_paths import ThreadPaths
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.file_checkpoint_repository import FileCheckpointRepository
from app.repositories.run_repository import RunRepository
from app.runtime.errors import StatePersistenceError
from app.storage.errors import StorageError

AREAS = ('uploads','workspace','outputs')


def _limit(name: str, default: int) -> int:
    try:
        value = int(os.getenv(f'DEER_MINI_CHECKPOINT_MAX_{name}',str(default)))
    except ValueError as error:
        raise ValueError(f'CHECKPOINT_MAX_{name} 必须是正整数') from error
    if value <= 0:
        raise ValueError(f'CHECKPOINT_MAX_{name} 必须是正整数')
    return value


def _sync(path: Path) -> None:
    fd = os.open(path,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class FileCheckpointService:
    def __init__(self) -> None:
        self.max_points = _limit('POINTS',100)
        self.max_bytes = _limit('BYTES',1024**3)
        self.max_entries = _limit('ENTRIES',50_000)
        self.max_manifest_bytes = _limit('MANIFEST_BYTES',16*1024**2)
        self.repo = FileCheckpointRepository()
        self.checkpoints = CheckpointRepository()

    def _store(self, thread: Thread) -> FileSnapshotStore:
        return FileSnapshotStore(ThreadPaths(Path(thread.workspace_path).parent),
            max_entries=self.max_entries,max_manifest_bytes=self.max_manifest_bytes,max_bytes=self.max_bytes)

    def _pinned(self, thread: Thread) -> set[str]:
        pins: set[str] = set()
        operations = self.repo.operations(thread)
        for operation in operations:
            if not operation['cleaned']:
                pins.update((operation['restore_point_id'],operation['recovery_point_id']))
        # 最新一次恢复前备份一直可用，下一次备份建立后才能替换它。
        recoveries = [p for p in self.repo.points(thread) if p['kind']=='recovery']
        if recoveries:
            pins.add(recoveries[0]['id'])
        return pins

    @staticmethod
    def _usage(manifests: list[dict]) -> int:
        objects: dict[str,int] = {}
        for manifest in manifests:
            for entry in manifest['entries']:
                if entry['kind']=='file':
                    objects[entry['sha256']] = entry['size']
        return sum(objects.values()) + sum(len(manifest_bytes(m)) for m in manifests)

    def _evictions(self, thread: Thread, manifest: dict, pins: set[str], *, replacing_recovery: bool) -> list[str]:
        points = self.repo.points(thread)
        # 在新备份确实能落盘的同一事务里才解除旧 recovery 的固定。
        protected = self._pinned(thread) | pins
        if replacing_recovery:
            old = next((p for p in points if p['kind']=='recovery'),None)
            operation_pins = {pid for op in self.repo.operations(thread,pending_only=True)
                              for pid in (op['restore_point_id'],op['recovery_point_id'])}
            if old and old['id'] not in pins | operation_pins:
                protected.discard(old['id'])
        remaining = list(reversed(points))
        evicted: list[str] = []
        while (len(remaining)+1 > self.max_points or self._usage(
                [manifest]+[json.loads(p['manifest_json']) for p in remaining]) > self.max_bytes):
            oldest = next((p for p in remaining if p['id'] not in protected),None)
            if oldest is None:
                raise ValueError('恢复点容量不足，请增加保留数量或备份容量上限')
            remaining.remove(oldest)
            evicted.append(oldest['id'])
        return evicted

    def _capture(self, thread: Thread, checkpoint: Checkpoint, message: str, *,
                 kind: str, pins: set[str] | None = None) -> dict:
        store = self._store(thread)
        existing = [json.loads(p['manifest_json']) for p in self.repo.points(thread)]
        store.collect(existing)
        manifest = store.capture()
        evictions = self._evictions(thread,manifest,pins or set(),replacing_recovery=kind=='recovery')
        point = self.repo.add_point(thread,checkpoint,manifest,kind,message,evictions,
                                    advance_head=kind=='turn_start')
        # 删除点的事务完成后才回收对象，不会先破坏仍然有效的备份。
        store.collect([json.loads(p['manifest_json']) for p in self.repo.points(thread)])
        return point

    def capture_turn(self, thread: Thread, run: Run, state: ThreadState, message: str) -> dict:
        history_step = self.checkpoints.next_step(thread.id,run.id,initial=0)
        checkpoint = Checkpoint(thread.id,run.id,history_step,
                                deepcopy(state),kind='turn_start')
        try:
            return self._capture(thread,checkpoint,message,kind='turn_start')
        except (sqlite3.Error, StorageError) as error:
            raise StatePersistenceError('本轮恢复点未能保存') from error

    def list_points(self, thread: Thread) -> list[dict]:
        points = self.repo.points(thread)
        result = [{key:p[key] for key in ('id','run_id','kind','created_at','message')}
                  | {'available':True,'unavailable_reason':None} for p in points]
        # 旧轮次不能补造过去的文件备份，只展示不可用说明。
        represented = {p['run_id'] for p in points if p['run_id']}
        for run in RunRepository().list_for_thread(thread.id,thread.user_id,limit=100,offset=0):
            if run.id not in represented:
                result.append({'id':f'legacy-{run.id}','run_id':run.id,'kind':'turn_start',
                    'created_at':run.created_at,'message':'本轮开始前','available':False,
                    'unavailable_reason':'本轮没有可用文件备份（旧轮次、备份失败或已清理）'})
        return sorted(result,key=lambda p:p['created_at'],reverse=True)

    def preview(self, thread: Thread, point_id: str) -> dict:
        point = self.repo.point(thread,point_id)
        target = json.loads(point['manifest_json'])
        store = self._store(thread)
        store.validate(target)
        current = store.capture(store_objects=False)
        before = {e['path']:e for e in current['entries']}
        after = {e['path']:e for e in target['entries']}
        current_state = self.checkpoints.latest(thread.id,thread.user_id)
        target_state = self.checkpoints.get(point['state_checkpoint_id'],thread.id,thread.user_id)
        if target_state is None:
            raise LookupError('目标消息状态不存在')
        messages = current_state.state.messages if current_state else []
        target_ids = {m.id for m in target_state.state.messages}
        return {'restore_point_id':point_id,
            'revision':self.checkpoints.revision(thread.id,thread.user_id),
            'fingerprint':fingerprint(current),
            'created':sorted(after.keys()-before.keys()),
            'deleted':sorted(before.keys()-after.keys()),
            'modified':sorted(p for p in after.keys() & before.keys() if after[p]!=before[p]),
            'removed_messages':sum(m.id not in target_ids for m in messages),
            'target_messages':len(target_state.state.messages),'current_messages':len(messages)}

    def operation(self, thread: Thread, operation_id: str) -> dict:
        op = self.repo.operation(thread,operation_id)
        if op is None:
            raise LookupError('恢复操作不存在')
        return op

    def _fault(self, phase: str) -> None:
        """故障注入接缝；生产环境无副作用。"""

    def _stage(self, store: FileSnapshotStore, operation_id: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',operation_id):
            raise ValueError('恢复操作编号格式无效')
        base = store.root / 'staging'
        base.mkdir(mode=0o700,exist_ok=True)
        if base.is_symlink() or not base.is_dir():
            raise ValueError('恢复暂存目录不安全')
        return base / operation_id

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_symlink():
            raise ValueError('恢复暂存目录不能包含符号链接')
        if path.exists():
            # 清单允许普通只读权限；仅对待删除的私有 staging 副本临时开放 owner 权限。
            path.chmod(0o700)
            for base, directories, _ in os.walk(path, followlinks=False):
                for name in directories:
                    child = Path(base) / name
                    if child.is_symlink():
                        raise ValueError('恢复暂存目录不能包含符号链接')
                    child.chmod(0o700)
            shutil.rmtree(path)

    def _history(self, thread: Thread, stage: Path) -> Path | None:
        for workspace in (Path(thread.workspace_path),stage/'old'/'workspace',stage/'rollback-old'/'workspace'):
            history = workspace/'.tool-results'
            if history.is_symlink():
                raise ValueError('历史结果目录不能是符号链接')
            if history.exists():
                self._check_history(history)
                return history
        return None

    @staticmethod
    def _check_history(history: Path) -> int:
        size = 0
        for base, dirs, files in os.walk(history,followlinks=False):
            for name in [*dirs,*files]:
                entry = (Path(base)/name).lstat()
                if stat.S_ISDIR(entry.st_mode):
                    continue
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink!=1:
                    raise ValueError('历史结果目录包含不支持的链接或文件')
                size += entry.st_size
        return size

    def _build(self, thread: Thread, store: FileSnapshotStore, manifest: dict,
               destination: Path, stage: Path) -> None:
        history = self._history(thread,stage)
        required = sum(e.get('size',0) for e in manifest['entries'])
        required += self._check_history(history) if history else 0
        if shutil.disk_usage(store.root).free < required + 1024*1024:
            raise OSError('恢复临时磁盘空间不足')
        store.materialize(manifest,destination)
        if history:
            target = destination/'workspace'/'.tool-results'
            workspace = destination/'workspace'
            workspace_mode = workspace.stat().st_mode & 0o777
            workspace.chmod(workspace_mode | 0o700)
            try:
                shutil.copytree(history,target)
            finally:
                workspace.chmod(workspace_mode)
            for base, dirs, files in os.walk(target,topdown=False):
                for name in files:
                    with (Path(base)/name).open('rb') as stream:
                        os.fsync(stream.fileno())
                _sync(Path(base))
            _sync(destination/'workspace')
        _sync(destination)
        _sync(destination.parent)

    @staticmethod
    def _move_directory(source: Path, target: Path) -> None:
        """跨父目录移动只读目录时临时开放 owner 权限，再恢复清单权限。"""
        if source.is_symlink() or not source.is_dir():
            raise ValueError('恢复目标必须是普通目录')
        mode = source.stat().st_mode & 0o777
        source.chmod(mode | 0o700)
        try:
            source.replace(target)
        except BaseException:
            if source.exists():
                source.chmod(mode)
            raise
        target.chmod(mode)
        _sync(target)

    def _apply(self, thread: Thread, stage: Path) -> None:
        parent = Path(thread.workspace_path).parent
        old = stage/'old'
        old.mkdir(mode=0o700)
        _sync(stage)
        for area in AREAS:
            self._move_directory(parent/area,old/area)
            _sync(parent)
            _sync(old)
            self._fault(f'old_{area}')
            self._move_directory(stage/'new'/area,parent/area)
            _sync(parent)
            _sync(stage/'new')
            self._fault(f'new_{area}')

    def _rollback(self, thread: Thread, operation: dict) -> None:
        store = self._store(thread)
        stage = self._stage(store,operation['operation_id'])
        recovery = self.repo.point(thread,operation['recovery_point_id'])
        manifest = json.loads(recovery['manifest_json'])
        store.validate(manifest)
        destination = stage/'rollback-new'
        self._remove(destination)
        self._build(thread,store,manifest,destination,stage)
        parent = Path(thread.workspace_path).parent
        # recovery 对象是所有重放的稳定源。部分回滚后重启仍可重新完整构建。
        discarded = stage/'rollback-old'
        discarded.mkdir(mode=0o700,exist_ok=True)
        for area in AREAS:
            self._remove(discarded/area)
            live = parent/area
            if live.is_symlink():
                raise ValueError('恢复目标目录不安全')
            if live.exists():
                self._move_directory(live,discarded/area)
                _sync(parent)
                _sync(discarded)
            self._move_directory(destination/area,live)
            _sync(parent)
            _sync(destination)
        if fingerprint(store.capture(store_objects=False)) != fingerprint(manifest):
            raise RuntimeError('恢复前文件状态校验失败')
        self.repo.update(thread,operation['operation_id'],'rolled_back',error='恢复未提交，已还原恢复前状态')
        self._cleanup(thread,operation['operation_id'],'rolled_back')

    def _cleanup(self, thread: Thread, operation_id: str, status: str) -> None:
        store = self._store(thread)
        stage = self._stage(store,operation_id)
        self._remove(stage)
        _sync(stage.parent)
        previous = self.operation(thread,operation_id)
        self.repo.update(thread,operation_id,status,error=previous['error'],cleaned=True)

    def restore(self, thread: Thread, operation_id: str, point_id: str,
                revision: int, current_fingerprint: str) -> dict:
        previous = self.repo.operation(thread,operation_id)
        if previous:
            if (previous['restore_point_id'],previous['expected_revision'],previous['fingerprint']) != (
                    point_id,revision,current_fingerprint):
                raise RuntimeError('恢复操作编号已用于不同请求')
            return previous
        store = self._store(thread)
        stage = self._stage(store,operation_id)
        preview = self.preview(thread,point_id)
        if preview['revision'] != revision or preview['fingerprint'] != current_fingerprint:
            raise RuntimeError('预览后消息或文件已变化，请重新预览')
        target = self.repo.point(thread,point_id)
        current = self.checkpoints.latest(thread.id,thread.user_id)
        if current is None:
            raise RuntimeError('当前消息状态不存在')
        recovery_checkpoint = Checkpoint(thread.id,current.run_id,
            self.checkpoints.next_step(thread.id,current.run_id),deepcopy(current.state),kind='restore')
        recovery = self._capture(thread,recovery_checkpoint,'恢复前备份',kind='recovery',pins={point_id})
        # 备份扫描之后再确认，避免未协调的外部写入混入恢复。
        if fingerprint(json.loads(recovery['manifest_json'])) != current_fingerprint:
            raise RuntimeError('预览后文件已变化，请重新预览')
        self._remove(stage)
        stage.mkdir(mode=0o700)
        _sync(stage.parent)
        try:
            self._build(thread,store,json.loads(target['manifest_json']),stage/'new',stage)
            self.repo.prepare(thread,operation_id,point_id,recovery['id'],revision,current_fingerprint)
        except Exception:
            # 尚未登记 prepared，没有工作目录被交换；清理失败也可在启动时识别并回收。
            self._remove(stage)
            _sync(stage.parent)
            raise
        try:
            self.repo.update(thread,operation_id,'applying')
            self._apply(thread,stage)
            if fingerprint(store.capture(store_objects=False)) != fingerprint(json.loads(target['manifest_json'])):
                raise RuntimeError('恢复后的文件校验失败')
            self._fault('before_commit')
            self.repo.commit_restore(thread,operation_id,target,revision)
            self._fault('after_commit')
        except Exception as error:
            operation = self.operation(thread,operation_id)
            if operation['status']=='committed':
                # 提交结果是最终真相，不能把已提交的消息配回旧文件。
                raise StatePersistenceError('恢复已提交，清理尚待确认') from error
            try:
                self._rollback(thread,operation)
            except Exception as recovery_error:
                self.repo.update(thread,operation_id,'needs_recovery',error=type(recovery_error).__name__)
                raise StatePersistenceError('恢复未完成，需要恢复处理') from recovery_error
            if isinstance(error,(StatePersistenceError,sqlite3.Error)):
                raise StatePersistenceError('恢复状态未能提交，已还原文件') from error
            return self.operation(thread,operation_id)
        self._cleanup(thread,operation_id,'committed')
        return self.operation(thread,operation_id)

    def recover_pending(self) -> list[tuple[str,str]]:
        """启动时重放未清理的恢复日志；返回需禁止执行和文件访问的用户/对话。"""
        blocked: list[tuple[str,str]] = []
        for operation in self.repo.operations(pending_only=True):
            thread = Thread(id=operation['thread_id'],user_id=operation['user_id'],workspace_path=operation['workspace_path'])
            try:
                if operation['status'] in {'committed','rolled_back'}:
                    point_id = operation['restore_point_id'] if operation['status']=='committed' else operation['recovery_point_id']
                    point = self.repo.point(thread,point_id)
                    store = self._store(thread)
                    manifest = json.loads(point['manifest_json'])
                    store.validate(manifest)
                    if fingerprint(store.capture(store_objects=False)) != fingerprint(manifest):
                        raise RuntimeError('已完成恢复的文件状态无法确认')
                    self._cleanup(thread,operation['operation_id'],operation['status'])
                else:
                    self._rollback(thread,operation)
            except Exception as error:
                blocked.append((thread.user_id,thread.id))
                # 终态只重试验证与清理，不能因 cleaned 写入失败再次从已删除的 staging 回滚。
                status = operation['status'] if operation['status'] in {'committed','rolled_back'} else 'needs_recovery'
                self.repo.update(thread,operation['operation_id'],status,error=type(error).__name__)
        for thread in self.repo.threads():
            try:
                self._collect_unprepared_staging(thread)
            except Exception:
                if (thread.user_id,thread.id) not in blocked:
                    blocked.append((thread.user_id,thread.id))
        return blocked

    def _collect_unprepared_staging(self, thread: Thread) -> None:
        """只回收数据库没有未清理操作引用的私有 staging，处理 prepared 前进程退出。"""
        store = self._store(thread)
        base = store.root/'staging'
        if store.root.is_symlink() or base.is_symlink():
            raise ValueError('恢复暂存目录不安全')
        if not base.exists():
            return
        referenced = {op['operation_id'] for op in self.repo.operations(thread,pending_only=True)}
        for path in base.iterdir():
            if path.name not in referenced and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',path.name):
                self._remove(path)
        _sync(base)
