"""原始输出导出交接失败或取消时，捕获对象仍须清理临时文件。"""

from pathlib import Path

from app.sandbox.docker_runner import _BoundedCapture


def test_unclaimed_export_is_removed_on_capture_close():
    capture = _BoundedCapture(10, preserve_full_output=True)
    capture.append(b"x" * 100)
    path = Path(capture.persist_full_output())
    assert path.exists()
    capture.close()
    assert not path.exists()


def test_claimed_export_survives_until_consumer_deletes_it():
    capture = _BoundedCapture(10, preserve_full_output=True)
    capture.append(b"x" * 100)
    capture.persist_full_output()
    path = Path(capture.release_full_output())
    capture.close()
    try:
        assert path.read_bytes() == b"x" * 100
    finally:
        path.unlink()
