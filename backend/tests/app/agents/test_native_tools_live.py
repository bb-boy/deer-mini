"""Real model and Docker acceptance for native file tools; skipped by default."""
import asyncio
from datetime import datetime
import json
import os
import re
import traceback
from pathlib import Path
import shutil
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_NATIVE_TOOLS_LIVE_TEST") != "1",
    reason="requires explicit real model and Docker acceptance opt-in",
)

from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.repositories.run_repository import RunRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
from app.sandbox.manager import ThreadSandboxManager
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService

MODEL_NAME = "siliconflow-deepseek-flash"
USER_ID = "native-live-acceptance"
RUN_TIMEOUT_SECONDS = 300
MAX_TOOL_ROUNDS = 8
FIRST_RUN_TOOLS = {"bash", "glob", "grep", "read_file", "edit_file", "write_file"}
SECOND_RUN_TOOLS = {"bash", "read_file", "edit_file", "write_file"}
EVIDENCE_PATH = Path(__file__).resolve().parents[4] / "docs" / "native-tools" / "live-model-evidence.json"


def _append_evidence(record: dict) -> None:
    """Append only redacted status and behavior summaries."""
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if EVIDENCE_PATH.exists():
        try:
            records = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records = []
    for old in records:
        if old.get("result") != "failed":
            continue
        runs = old.get("runs")
        if runs:
            counts = [run.get("model_request_attempt_count") for run in runs]
            if all(type(count) is int for count in counts):
                old["model_request_attempt_count"] = sum(counts)
                old["failure_stage"] = "Failure details are summarized by Run in runs"
            else:
                old["model_request_attempt_count"] = None
                old["failure_stage"] = "Per-Run trace is incomplete; see runs"
        elif old.get("failure_type") == "ValueError" and old.get("run_status") is None:
            old["model_request_attempt_count"] = 0
            old["failure_stage"] = "Run creation; no model request started"
        elif old.get("model_request_attempt_count") is None or "model_request_attempt_count" not in old:
            old["model_request_attempt_count"] = None
            old["failure_stage"] = "Earlier failure; event trace unavailable"
    records.append(record)
    temporary = EVIDENCE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(EVIDENCE_PATH)


def _run_evidence(events: list) -> dict:
    attempts = [e for e in events if e.event_type == "model.status" and e.payload.get("phase") == "attempt"]
    ends = [e for e in events if e.event_type == "tool.end"]
    errors = [e for e in ends if e.payload.get("is_error")]
    tool_error_categories = []
    for event in errors:
        name = event.payload.get("tool_name")
        content = event.payload.get("content", "")
        if name == "bash":
            match = re.search(r"\[Exit Code: (-?\d+)\]", content)
            if match:
                tool_error_categories.append({"tool": name, "category": "exit_code", "exit_code": int(match.group(1))})
            elif "超时" in content:
                tool_error_categories.append({"tool": name, "category": "timeout"})
            else:
                tool_error_categories.append({"tool": name, "category": "execution_error"})
        elif name == "write_file" and "文件已存在" in content:
            tool_error_categories.append({"tool": name, "category": "already_exists"})
        else:
            tool_error_categories.append({"tool": name, "category": "tool_error"})
    return {
        "run_status": "success",
        "model_request_attempt_count": len(attempts),
        "tool_names": sorted({e.payload.get("tool_name") for e in ends}),
        "tool_name_sequence": [e.payload.get("tool_name") for e in ends],
        "tool_end_count": len(ends),
        "ordinary_tool_error_count": len(errors),
        "ordinary_error_tool_names": sorted({e.payload.get("tool_name") for e in errors}),
        "tool_error_categories": tool_error_categories,
        "model_failure_reasons": sorted({
            e.payload.get("reason") for e in events
            if e.event_type == "model.status" and e.payload.get("reason")
        }),
        "reasoning_character_count": sum(
            len(e.payload["message"].get("reasoning_content") or "")
            for e in events if e.event_type == "message.complete"
            and isinstance(e.payload.get("message"), dict)
        ),
    }


@pytest.mark.parametrize("thinking_enabled", [False, True], ids=["non-thinking", "thinking"])
def test_real_model_native_tools_end_to_end(monkeypatch, tmp_path, thinking_enabled):
    """Run two real Agent turns through native tools and isolated Docker Bash."""
    from dotenv import load_dotenv
    from app.model.errors import ModelCallError

    # Fixture initialized database.DATABASE_PATH in tmp_path and clears env overrides.
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(Path(database.DATABASE_PATH).resolve()))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str((tmp_path / "live-test-users").resolve()))
    load_dotenv(dotenv_path="/home/pl/deer_mini/backend/.env", override=False)
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(Path(database.DATABASE_PATH).resolve()))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str((tmp_path / "live-test-users").resolve()))
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    monkeypatch.setenv("DEER_MINI_MODEL_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("DEER_MINI_MODEL_MAX_WAIT_SECONDS", "1")
    monkeypatch.setenv("DEER_MINI_MODEL_CALL_TIMEOUT_SECONDS", "280")
    monkeypatch.setenv("DEER_MINI_MODEL_ATTEMPT_TIMEOUT_SECONDS", "260")

    docker = shutil.which(os.getenv("DEER_MINI_DOCKER_BINARY", "docker"))
    if docker is None:
        pytest.skip("Docker executable unavailable in live acceptance process")
    runner = DockerCommandRunner(DockerRunnerConfig(
        image="python:3.12-slim", timeout_seconds=35, network_enabled=False,
        docker_binary=docker, max_output_bytes=20_000,
    ))
    scope = f"native-live-test-{uuid4().hex}"
    manager = ThreadSandboxManager(runner, scope=scope)
    bridge = MemoryStreamBridge(retention_seconds=0)
    coordinator = RunCoordinator(bridge, run_timeout_seconds=RUN_TIMEOUT_SECONDS, bash_runner=manager)
    thread = ThreadService().create_thread(USER_ID, "native live acceptance")
    root = Path(thread.workspace_path).parent
    source = root / "workspace" / "source.txt"
    source.write_bytes(b"status=old\r\nkeep=1\r\n")
    collision = root / "outputs" / "collision.txt"
    collision.write_bytes(b"planned\n")
    report = root / "outputs" / "native-report.txt"
    effort = "medium" if thinking_enabled else None
    runs = []
    cleanup_confirmed = False
    record = {
        "recorded_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "model_profile": MODEL_NAME,
        "thinking_enabled": thinking_enabled,
        "reasoning_effort": effort,
        "docker_image": "python:3.12-slim",
        "container_scope_prefix": "native-live-test-",
        "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
        "model_max_attempts": 1,
        "max_tool_rounds": MAX_TOOL_ROUNDS,
        "result": "running",
    }

    async def one_run(message: str) -> tuple[object, list]:
        run = await coordinator.create_and_start_run(
            user_id=USER_ID, thread_id=thread.id, message=message,
            model_name=MODEL_NAME, thinking_enabled=thinking_enabled, reasoning_effort=effort,
        )
        runs.append(run)
        task = coordinator._tasks[run.id]
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=RUN_TIMEOUT_SECONDS + 10)
        except Exception as error:
            events = EventRepository().list_for_run(thread.id, run.id, USER_ID)
            summary = _run_evidence(events)
            summary["run_status"] = RunRepository().get(run.id, USER_ID).status
            if isinstance(error, ModelCallError):
                summary["failure_category"] = error.reason
                summary["failure_attempts"] = error.attempts
                summary["failure_status_code"] = error.status_code
            elif isinstance(error, RuntimeError) and str(error).startswith("Agent 已达到"):
                summary["failure_category"] = "round_limit"
                summary["round_limit"] = MAX_TOOL_ROUNDS
            else:
                summary["failure_type"] = type(error).__name__
            record.setdefault("runs", []).append(summary)
            raise AssertionError(f"live Agent Run failed: {summary.get('failure_category', summary.get('failure_type'))}") from None
        await asyncio.sleep(0)
        saved = RunRepository().get(run.id, USER_ID)
        assert saved is not None and saved.status == "success"
        events = EventRepository().list_for_run(thread.id, run.id, USER_ID)
        assert any(e.event_type == "run.end" for e in events)
        summary = _run_evidence(events)
        summary["model_name"] = saved.model_name
        summary["thinking_enabled"] = saved.thinking_enabled
        summary["run_status"] = saved.status
        record.setdefault("runs", []).append(summary)
        return saved, events

    async def execute() -> None:
        nonlocal cleanup_confirmed
        await manager.start()
        try:
            first, _ = await one_run(
                "Use glob and grep to locate workspace/source.txt, then read it with "
                "preserve_newlines=true and line_numbers=false. Use edit_file to change exactly "
                "status=old to status=new while preserving CRLF. Use write_file to create "
                "outputs/native-report.txt with exactly status=new followed by a newline. Then "
                "use bash with command printf 'from-bash\\n' > /mnt/user-data/workspace/shared.txt; "
                "cat /mnt/user-data/outputs/native-report.txt. "
                "Use all six tools and finish with a short confirmation."
            )
            assert first.model_name == MODEL_NAME and first.thinking_enabled is thinking_enabled
            first_summary = record["runs"][-1]
            assert FIRST_RUN_TOOLS <= set(first_summary["tool_names"])
            assert source.read_bytes() == b"status=new\r\nkeep=1\r\n"
            assert report.is_file() and report.stat().st_size > 0
            report_bytes = report.stat().st_size
            shared = root / "workspace" / "shared.txt"
            assert shared.read_bytes() == b"from-bash\n"

            second, _ = await one_run(
                "Continue in this same Thread. Read workspace/shared.txt with read_file, use "
                "edit_file to change from-bash to seen-by-native, then use bash command "
                "cat /mnt/user-data/workspace/shared.txt to verify. "
                "Call write_file on existing outputs/collision.txt with default overwrite=false "
                "and content overwrite-attempt; inspect the ordinary error, then recover with "
                "edit_file changing planned to recovered. Finally replace outputs/native-report.txt "
                "using write_file overwrite=true with exactly these three lines: status=new, "
                "shared=seen-by-native, collision=recovered. "
                "Use the four available tools and finish with a short confirmation."
            )
            assert second.model_name == MODEL_NAME and second.thinking_enabled is thinking_enabled
            second_summary = record["runs"][-1]
            assert SECOND_RUN_TOOLS <= set(second_summary["tool_names"])
            assert second_summary["ordinary_error_tool_names"] == ["write_file"]
            seq = second_summary["tool_name_sequence"]
            error_position = next(
                index for index, event in enumerate(
                    [e for e in EventRepository().list_for_run(thread.id, runs[1].id, USER_ID)
                     if e.event_type == "tool.end"]
                ) if event.payload.get("is_error")
            )
            assert any(name == "edit_file" for name in seq[error_position + 1:])

            assert source.read_bytes() == b"status=new\r\nkeep=1\r\n"
            shared = root / "workspace" / "shared.txt"
            assert shared.read_bytes() == b"seen-by-native\n"
            assert collision.read_bytes() == b"recovered\n"
            assert report.read_bytes() == b"status=new\nshared=seen-by-native\ncollision=recovered\n"
            assert second_summary["model_request_attempt_count"] <= MAX_TOOL_ROUNDS

            points = coordinator.file_checkpoints.list_points(thread)
            start = next(p for p in points if p["run_id"] == runs[0].id and p["kind"] == "turn_start")
            preview = await coordinator.preview_restore(
                user_id=USER_ID, thread_id=thread.id, point_id=start["id"],
            )
            assert "workspace/source.txt" in preview["modified"]
            assert "workspace/shared.txt" in preview["deleted"]
            assert "outputs/native-report.txt" in preview["deleted"]
            restored = await coordinator.restore_thread(
                user_id=USER_ID, thread_id=thread.id, operation_id=f"live-{uuid4().hex}",
                point_id=start["id"], revision=preview["revision"], fingerprint=preview["fingerprint"],
            )
            assert restored["status"] == "committed" and restored["cleaned"] == 1
            assert source.read_bytes() == b"status=old\r\nkeep=1\r\n"
            assert collision.read_bytes() == b"planned\n"
            assert not shared.exists() and not report.exists()
            restored_checkpoint = CheckpointRepository().latest(thread.id, USER_ID)
            assert restored_checkpoint is not None and restored_checkpoint.state.messages == []

            if thinking_enabled:
                assert all(r["reasoning_character_count"] > 0 for r in record["runs"])
            record.update({
                "result": "passed",
                "checkpoint_restore": "Run 1 turn_start committed and cleaned; source/collision restored, added files removed",
                "report_bytes_before_restore": report_bytes,
            })
        finally:
            await coordinator.shutdown()
            process = await asyncio.create_subprocess_exec(
                docker, "ps", "--all", "--quiet", "--filter",
                f"label=deer-mini.sandbox-owner={scope}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            remaining, _ = await process.communicate()
            assert process.returncode == 0 and not remaining.strip(), "isolated Docker scope still has containers"
            cleanup_confirmed = True
            record["docker_cleanup"] = "confirmed; scope has no containers after shutdown"
            record["docker_cleanup_confirmed"] = cleanup_confirmed

    try:
        asyncio.run(execute())
    except pytest.skip.Exception:
        raise
    except BaseException as error:
        record.setdefault("failure_type", type(error).__name__)
        frames = traceback.extract_tb(error.__traceback__)
        source_frames = [frame for frame in frames if Path(frame.filename).resolve() == Path(__file__).resolve()]
        if source_frames:
            record["failure_location"] = {
                "file": "test_native_tools_live.py",
                "line": source_frames[-1].lineno,
            }
        record["result"] = "failed"
        record["docker_cleanup_confirmed"] = cleanup_confirmed
        _append_evidence(record)
        raise
    _append_evidence(record)
