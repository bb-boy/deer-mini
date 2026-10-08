"""RunCoordinator 的工具开关与配置测试。"""

import asyncio

import pytest

from app.sandbox.base import CommandResult
from app.sandbox.docker_runner import (
    DockerCommandRunner,
    load_docker_runner_from_env,
)
from app.services.run_coordinator import RunCoordinator
from app.runtime.stream_bridge import MemoryStreamBridge
from app.agents.prompts.builder import apply_prompt_template
import app.services.run_coordinator as run_coordinator_module


@pytest.fixture(autouse=True)
def isolate_search_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试不受开发机 .env 中真实密钥影响；启用场景单独设置虚拟值。
    monkeypatch.setenv("TAVILY_API_KEY", "")


def test_configured_tavily_registers_web_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    coordinator = RunCoordinator(MemoryStreamBridge(), bash_runner=None)
    registry = coordinator._build_tool_registry()
    assert [tool.name for tool in registry.definitions()] == ["read_file", "glob", "grep", "edit_file", "write_file", "read_tool_result", "web_search", "web_fetch", "task", "write_todos", "snip"]
    assert "test-search-key" not in repr(registry.definitions())


def test_blank_tavily_key_does_not_register_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "  ")
    coordinator = RunCoordinator(MemoryStreamBridge(), bash_runner=None)
    assert coordinator._build_tool_registry().get("web_search") is None
    assert coordinator._build_tool_registry().get("web_fetch") is None


@pytest.mark.parametrize("enabled", [False, True])
def test_citations_only_appear_when_search_is_available(enabled: bool) -> None:
    prompt = apply_prompt_template(web_search_enabled=enabled)
    assert ("<citations>" in prompt) is enabled
    assert ("web_fetch" in prompt) is enabled
    if enabled:
        assert "Extract {title, url, snippet} from results" in prompt
        assert "{{title" not in prompt


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

    assert _tool_names(coordinator) == ["read_file", "glob", "grep", "edit_file", "write_file", "read_tool_result", "task", "write_todos", "snip"]


def test_injected_runner_registers_bash_tool() -> None:
    coordinator = RunCoordinator(
        MemoryStreamBridge(),
        run_timeout_seconds=10,
        bash_runner=RecordingRunner(),
    )

    assert _tool_names(coordinator) == ["read_file", "glob", "grep", "edit_file", "write_file", "read_tool_result", "bash", "task", "write_todos", "snip"]


def test_enabled_environment_registers_bash_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEER_MINI_BASH_ENABLED", "true")

    coordinator = RunCoordinator(
        MemoryStreamBridge(),
        run_timeout_seconds=10,
    )

    assert _tool_names(coordinator) == ["read_file", "glob", "grep", "edit_file", "write_file", "read_tool_result", "bash", "task", "write_todos", "snip"]


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
