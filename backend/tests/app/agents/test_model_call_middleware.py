"""Show how model-call middlewares surround one model request."""

import asyncio

from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext


def test_model_middlewares_wrap_execution_in_registration_order(tmp_path):
    steps: list[str] = []

    class RecordingMiddleware(AgentMiddleware):
        def __init__(self, name: str):
            self.name = name

        async def wrap_model_call(self, state, context, call_next):
            steps.append(f"{self.name}.before")
            result = await call_next()
            steps.append(f"{self.name}.after")
            return result

    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    async def fake_model():
        steps.append("model")
        return Message(role="assistant", content="hello")

    state = ThreadState(thread_id="thread-1", user_id="user-1")
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(tmp_path),
        record_event, save_checkpoint,
    )
    middlewares = MiddlewareManager([
        RecordingMiddleware("outer"), RecordingMiddleware("inner"),
    ])

    result = asyncio.run(middlewares.wrap_model_call(state, context, fake_model))

    assert result.content == "hello"
    assert steps == [
        "outer.before", "inner.before", "model", "inner.after", "outer.after",
    ]
