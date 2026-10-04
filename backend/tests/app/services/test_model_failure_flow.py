"""Real temporary Run/Thread lifecycle on classified provider failures."""

import asyncio
import logging

import httpx
import openai
import pytest

from app.domain.messages import Message
from app.infrastructure import database
from app.model.factory import ModelFactory
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService


def test_auth_error_finishes_run_and_keeps_raw_response_out_of_logs(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "model.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    monkeypatch.setenv("TAVILY_API_KEY", "")
    database.initialize_database()
    thread = ThreadService().create_thread("alice", "model failure")
    caplog.set_level(logging.INFO)
    calls = []

    class Model:
        async def chat(self, **kwargs):
            calls.append(1)
            response = httpx.Response(401, request=httpx.Request("POST", "https://model.invalid"))
            raise openai.APIStatusError("RAW_RESPONSE_SECRET", response=response, body={"code": "invalid_api_key"})

        async def close(self):
            pass

    async def scenario():
        from app.model.errors import ModelCallError
        monkeypatch.setattr(ModelFactory, "create_chat_model", lambda self: Model())
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None)
        try:
            run = await coordinator.create_and_start_run(user_id="alice", thread_id=thread.id,
                message="test", model_name="fake", thinking_enabled=False, reasoning_effort=None)
            worker = coordinator._tasks[run.id]
            events = [event async for event in bridge.subscribe(run.id)]
            with pytest.raises(ModelCallError):
                await worker
            await asyncio.sleep(0)  # observe background task error logging
            saved = RunRepository().get(run.id, "alice")
            assert saved.status == "error" and "鉴权" in saved.error
            assert "RAW_RESPONSE_SECRET" not in saved.error
            assert ThreadRepository().get(thread.id, "alice").status == "idle"
            state = CheckpointRepository().latest(thread.id, "alice").state
            assert not any(m.role == "assistant" for m in state.messages)
            assert any(e.data.get("event_type") == "run.error" for e in events)
            assert not any(e.data.get("event_type") == "run.end" for e in events)
        finally:
            await coordinator.shutdown()

    asyncio.run(scenario())
    assert len(calls) == 1 and "RAW_RESPONSE_SECRET" not in caplog.text
