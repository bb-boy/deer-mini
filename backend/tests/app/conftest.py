"""旧回归测试默认关闭自动记忆；记忆用例显式启用并使用临时存储。"""

import pytest


@pytest.fixture(autouse=True)
def disable_automatic_memory_by_default(monkeypatch):
    """避免旧模型桩意外承接后台提取调用；生产默认配置不受影响。"""
    monkeypatch.setenv("DEER_MINI_MEMORY_ENABLED", "false")
