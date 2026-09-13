"""DockerCommandRunner 的容器边界与进程生命周期测试。"""

import asyncio
import os
from pathlib import Path
import textwrap

import pytest

from app.sandbox.docker_runner import (
    DockerCommandRunner,
    DockerRunnerConfig,
    SandboxCleanupError,
)


def make_config(**overrides) -> DockerRunnerConfig:
    values = {
        "image": "python:3.12-slim",
        "timeout_seconds": 5.0,
        "max_output_bytes": 20_000,
        "memory_limit": "512m",
        "cpu_limit": 1.0,
        "pids_limit": 64,
        "network_enabled": False,
        "docker_binary": "docker",
    }
    values.update(overrides)
    return DockerRunnerConfig(**values)


def make_fake_docker(tmp_path: Path) -> Path:
    """创建可记录 run/kill 的假 Docker CLI，避免单元测试依赖守护进程。"""
    executable = tmp_path / "fake-docker"
    executable.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import sys
            import time

            log_path = os.environ.get("FAKE_DOCKER_LOG")
            action = sys.argv[1]
            if log_path:
                with open(log_path, "a", encoding="utf-8") as log:
                    log.write(action + "\\n")

            if action == "rm" and os.environ.get("FAKE_DOCKER_RM_FAIL") == "1":
                print("cannot remove container", file=sys.stderr)
                raise SystemExit(1)
            if action == "rm":
                raise SystemExit(0)

            mode = os.environ.get("FAKE_DOCKER_MODE", "success")
            if mode == "large":
                print("A" * 2_000)
            elif mode == "utf8":
                print("中文")
            elif mode == "error":
                print("command failed", file=sys.stderr)
                raise SystemExit(7)
            elif mode == "sleep":
                print("started", flush=True)
                time.sleep(60)
            else:
                print("command output")
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_build_run_args_confines_command_to_current_workspace(tmp_path: Path):
    workspace = tmp_path / "thread-1" / "workspace"
    workspace.mkdir(parents=True)
    runner = DockerCommandRunner(make_config())

    container_name, args = runner.build_run_args(
        command="python analyse.py; printf done",
        workspace_path=str(workspace),
        run_id="run/unsafe value",
        tool_call_id="call:1",
    )

    assert container_name.startswith("deer-mini-")
    assert args[:2] == ["docker", "run"]
    assert "--rm" in args
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--memory") + 1] == str(512 * 1024**2)
    assert args[args.index("--memory-swap") + 1] == str(512 * 1024**2)
    assert args[args.index("--cpus") + 1] == "1.0"
    assert args[args.index("--pids-limit") + 1] == "64"
    assert args[args.index("--workdir") + 1] == "/workspace"
    assert (
        args[args.index("--mount") + 1]
        == f'type=bind,"source={workspace.resolve()}",target=/workspace'
    )
    assert args[-3:] == ["/bin/bash", "-lc", "python analyse.py; printf done"]

    injected_env = [
        args[index + 1]
        for index, value in enumerate(args)
        if value == "--env"
    ]
    assert injected_env[:3] == [
        "HOME=/workspace",
        "LANG=C.UTF-8",
        "PYTHONUNBUFFERED=1",
    ]
    # 除固定环境外只允许时区设置，不能把后端的 Key 等环境变量传入容器。
    assert set(injected_env[3:]) <= {"TZ=:/etc/localtime"}


def test_run_captures_nonzero_exit_code(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_docker = make_fake_docker(tmp_path)
    monkeypatch.setenv("FAKE_DOCKER_MODE", "error")
    runner = DockerCommandRunner(
        make_config(docker_binary=str(fake_docker))
    )

    result = asyncio.run(
        runner.run(
            command="false",
            workspace_path=str(workspace),
            run_id="run-1",
            tool_call_id="call-1",
        )
    )

    assert result.exit_code == 7
    assert result.timed_out is False
    assert result.output == "command failed\n"


def test_run_drains_but_bounds_large_output(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_docker = make_fake_docker(tmp_path)
    monkeypatch.setenv("FAKE_DOCKER_MODE", "large")
    runner = DockerCommandRunner(
        make_config(
            docker_binary=str(fake_docker),
            max_output_bytes=160,
        )
    )

    result = asyncio.run(
        runner.run(
            command="yes",
            workspace_path=str(workspace),
            run_id="run-1",
            tool_call_id="call-1",
        )
    )

    assert result.exit_code == 0
    assert result.output_truncated is True
    assert len(result.output.encode("utf-8")) <= 160
    assert "output truncated" in result.output


def test_timeout_kills_container_and_returns_timeout(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_docker = make_fake_docker(tmp_path)
    log_path = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log_path))
    runner = DockerCommandRunner(
        make_config(
            docker_binary=str(fake_docker),
            timeout_seconds=0.1,
        )
    )

    result = asyncio.run(
        runner.run(
            command="sleep 60",
            workspace_path=str(workspace),
            run_id="run-1",
            tool_call_id="call-1",
        )
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert "started" in result.output
    assert log_path.read_text(encoding="utf-8").splitlines() == ["run", "rm"]


def test_cancellation_kills_container_and_propagates_cancel(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_docker = make_fake_docker(tmp_path)
    log_path = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log_path))
    runner = DockerCommandRunner(
        make_config(
            docker_binary=str(fake_docker),
            timeout_seconds=10,
        )
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            runner.run(
                command="sleep 60",
                workspace_path=str(workspace),
                run_id="run-1",
                tool_call_id="call-1",
            )
        )
        for _ in range(100):
            if log_path.exists() and "run" in log_path.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert log_path.read_text(encoding="utf-8").splitlines() == ["run", "rm"]


def test_cancel_while_starting_process_still_runs_cleanup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FakeProcess:
        def __init__(self):
            self.pid = os.getpid()
            self.returncode = None
            self.stdout = asyncio.StreamReader()

        async def wait(self):
            await asyncio.Event().wait()

    class StartingRunner(DockerCommandRunner):
        def __init__(self):
            super().__init__(make_config())
            self.cleanup_called = False

        async def _start_process(self, args):
            await asyncio.sleep(0.05)
            return FakeProcess()

        async def _cleanup_cancelled_process(self, process, container_name):
            self.cleanup_called = True
            process.returncode = 0
            process.stdout.feed_eof()

    runner = StartingRunner()

    async def scenario():
        task = asyncio.create_task(
            runner.run(
                command="echo never",
                workspace_path=str(workspace),
                run_id="run-start-cancel",
                tool_call_id="call-start-cancel",
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert runner.cleanup_called is True


def test_cleanup_failure_is_reported(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_docker = make_fake_docker(tmp_path)
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    monkeypatch.setenv("FAKE_DOCKER_RM_FAIL", "1")
    runner = DockerCommandRunner(
        make_config(docker_binary=str(fake_docker), timeout_seconds=0.05)
    )

    with pytest.raises(SandboxCleanupError, match="cannot remove container"):
        asyncio.run(
            runner.run(
                command="sleep 60",
                workspace_path=str(workspace),
                run_id="run-cleanup-fail",
                tool_call_id="call-cleanup-fail",
            )
        )


@pytest.mark.parametrize(
    "memory_limit",
    ["garbage", "0", "1m", "-2g", "10ib", "9" * 400],
)
def test_invalid_memory_limit_is_rejected(memory_limit: str):
    with pytest.raises(ValueError, match="内存限制"):
        DockerRunnerConfig(memory_limit=memory_limit)


def test_workspace_path_with_comma_is_csv_quoted(tmp_path: Path):
    workspace = tmp_path / "workspace,with-comma"
    workspace.mkdir()

    _, args = DockerCommandRunner(make_config()).build_run_args(
        command="pwd",
        workspace_path=str(workspace),
        run_id="run-1",
        tool_call_id="call-1",
    )

    assert args[args.index("--mount") + 1] == (
        f'type=bind,"source={workspace.resolve()}",target=/workspace'
    )


def test_utf8_output_stays_within_byte_limit(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_docker = make_fake_docker(tmp_path)
    monkeypatch.setenv("FAKE_DOCKER_MODE", "utf8")
    runner = DockerCommandRunner(
        make_config(docker_binary=str(fake_docker), max_output_bytes=5)
    )

    result = asyncio.run(
        runner.run(
            command="printf 中文",
            workspace_path=str(workspace),
            run_id="run-utf8",
            tool_call_id="call-utf8",
        )
    )

    assert len(result.output.encode("utf-8")) <= 5
