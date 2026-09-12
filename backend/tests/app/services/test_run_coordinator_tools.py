"""RunCoordinator 的 Bash 工具开关与配置测试。"""

import asyncio

import pytest

from app.sandbox.base import CommandResult
from app.sandbox.docker_runner import (
    DockerCommandRunner,
    load_docker_runner_from_env,
)
from app.services.run_coordinator import RunCoordinator
from app.runtime.stream_bridge import MemoryStreamBridge
import app.services.run_coordinator as run_coordinator_module


class RecordingRunner:
    """只用于确认依赖注入，不会真的启动 Docker。"""

    async def run(
        self,
        *,
        command: str,
        workspace_path: str,
        user_id: str,
        thread_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> CommandResult:
        return CommandResult(output="unused", exit_code=0)


def _tool_names(coordinator: RunCoordinator) -> list[str]:
    return [
        definition.name
        for definition in coordinator._build_tool_registry().definitions()
    ]


def test_bash_tool_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEER_MINI_BASH_ENABLED", raising=False)

    coordinator = RunCoordinator(
        MemoryStreamBridge(),
        run_timeout_seconds=10,
    )

    assert _tool_names(coordinator) == ["read_file"]


def test_injected_runner_registers_bash_tool() -> None:
    coordinator = RunCoordinator(
        MemoryStreamBridge(),
        run_timeout_seconds=10,
        bash_runner=RecordingRunner(),
    )

    assert _tool_names(coordinator) == ["read_file", "bash"]


def test_enabled_environment_registers_bash_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEER_MINI_BASH_ENABLED", "true")

    coordinator = RunCoordinator(
        MemoryStreamBridge(),
        run_timeout_seconds=10,
    )

    assert _tool_names(coordinator) == ["read_file", "bash"]


def test_load_docker_runner_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEER_MINI_BASH_ENABLED", "true")
    monkeypatch.setenv("DEER_MINI_BASH_IMAGE", "example/bash:1")
    monkeypatch.setenv("DEER_MINI_BASH_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("DEER_MINI_BASH_MAX_OUTPUT_BYTES", "4096")
    monkeypatch.setenv("DEER_MINI_BASH_MEMORY_LIMIT", "256m")
    monkeypatch.setenv("DEER_MINI_BASH_CPU_LIMIT", "0.5")
    monkeypatch.setenv("DEER_MINI_BASH_PIDS_LIMIT", "32")
    monkeypatch.setenv("DEER_MINI_BASH_NETWORK_ENABLED", "yes")
    monkeypatch.setenv("DEER_MINI_SANDBOX_IDLE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("DEER_MINI_SANDBOX_CHECK_INTERVAL_SECONDS", "5")

    runner = load_docker_runner_from_env()

    assert isinstance(runner, DockerCommandRunner)
    assert runner.config.image == "example/bash:1"
    assert runner.config.timeout_seconds == 12.5
    assert runner.config.max_output_bytes == 4096
    assert runner.config.memory_limit == "256m"
    assert runner.config.cpu_limit == 0.5
    assert runner.config.pids_limit == 32
    assert runner.config.network_enabled is True
    assert runner.config.idle_timeout_seconds == 120
    assert runner.config.idle_check_interval_seconds == 5


def test_disabled_environment_returns_no_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEER_MINI_BASH_ENABLED", "false")

    assert load_docker_runner_from_env() is None


def test_start_cleans_orphaned_containers_even_when_bash_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def cleanup_containers() -> None:
        calls.append("containers")

    async def cleanup_directories() -> None:
        calls.append("directories")

    monkeypatch.setenv("DEER_MINI_BASH_ENABLED", "false")
    monkeypatch.setattr(
        run_coordinator_module,
        "cleanup_orphaned_thread_sandboxes",
        cleanup_containers,
    )
    monkeypatch.setattr(
        run_coordinator_module,
        "cleanup_pending_thread_deletions",
        cleanup_directories,
    )
    coordinator = RunCoordinator(
        MemoryStreamBridge(),
        run_timeout_seconds=10,
    )

    asyncio.run(coordinator.start())

    assert calls == ["directories", "containers"]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DEER_MINI_BASH_ENABLED", "sometimes", "true 或 false"),
        ("DEER_MINI_BASH_TIMEOUT_SECONDS", "fast", "必须是数字"),
        ("DEER_MINI_BASH_MAX_OUTPUT_BYTES", "many", "必须是整数"),
    ],
)
def test_invalid_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv("DEER_MINI_BASH_ENABLED", "true")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        load_docker_runner_from_env()
