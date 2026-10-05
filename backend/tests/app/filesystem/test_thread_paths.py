import pytest
from app.filesystem.thread_paths import ThreadPaths


def test_thread_directory_itself_cannot_be_a_symlink(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        ThreadPaths(linked)
