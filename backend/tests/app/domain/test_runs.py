"""Tests for backend/app/domain/runs.py."""

# 在这里编写 pytest 的 test_* 函数。


from datetime import datetime

from app.domain.runs import Run


def test_new_run_starts_pending():
    run = Run(
        id="run_001",
        thread_id="thread_001",
        user_id="user_001",
    )

    assert run.status == "pending"
    assert run.error is None
    assert run.started_at is None
    assert run.finished_at is None

    created_time = datetime.fromisoformat(run.created_at)
    assert created_time.tzinfo is not None


def test_run_keeps_model_and_thinking_options():
    run = Run(
        id="run_002",
        thread_id="thread_001",
        user_id="user_001",
        model_name="deepseek-chat",
        thinking_enabled=True,
        reasoning_effort="high",
    )

    assert run.model_name == "deepseek-chat"
    assert run.thinking_enabled is True
    assert run.reasoning_effort == "high"