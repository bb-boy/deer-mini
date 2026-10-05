"""单进程准入不等待持有的锁，恢复优先于取消收尾。"""
import importlib
import pytest


def test_restore_blocks_new_writers_but_allows_run_release():
    cls = importlib.import_module('app.services.thread_operations').ThreadOperations
    operations = cls()
    with operations.write('alice','thread','run'):
        operations.register_run('alice','thread','run-one')
    with pytest.raises(RuntimeError):
        with operations.write('alice','thread','upload'): pass
    with operations.write('alice','thread','restore',allow_run=True):
        operations.release_run('alice','thread','run-one')
        with pytest.raises(RuntimeError):
            with operations.write('alice','thread','run'): pass
    with operations.write('alice','thread','upload'): pass


def test_open_download_blocks_restore_until_closed():
    cls = importlib.import_module('app.services.thread_operations').ThreadOperations
    operations = cls()
    release = operations.read('alice','thread')
    with pytest.raises(RuntimeError):
        with operations.write('alice','thread','restore',allow_run=True): pass
    release()
    with operations.write('alice','thread','restore',allow_run=True):
        with pytest.raises(RuntimeError): operations.read('alice','thread')


def test_blocked_thread_cannot_execute_or_upload_or_read():
    cls = importlib.import_module('app.services.thread_operations').ThreadOperations
    operations = cls()
    operations.block('alice','thread')
    for mode in ('run','upload','restore','delete'):
        with pytest.raises(RuntimeError):
            with operations.write('alice','thread',mode): pass
    with pytest.raises(RuntimeError): operations.read('alice','thread')
