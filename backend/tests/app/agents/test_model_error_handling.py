"""Model retry policy boundaries; no network/model charges."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import openai
import pytest

from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError


def api_error(status, code=None, headers=None, text="UPSTREAM_SECRET"):
    response = httpx.Response(status, request=httpx.Request("POST", "https://model.invalid"), headers=headers)
    return openai.APIStatusError(text, response=response, body={"code": code, "message": text})


def context(tmp_path, events):
    async def record(kind, payload):
        events.append((kind, payload))

    async def save(state):
        pass

    return RuntimeContext("alice", "thread", "run", str(tmp_path), record, save)


def middleware(**kwargs):
    from app.agents.model_error_handling_middleware import ModelErrorHandlingMiddleware
    from app.model.call_policy import ModelCallPolicy
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    return ModelErrorHandlingMiddleware(ModelCallPolicy(**kwargs), sleep=sleep, random_value=lambda: 0), delays


@pytest.mark.parametrize("status,code,reason,retry,attempts", [
    (429, "rate_limit_exceeded", "rate_limit", True, 3),
    (429, "insufficient_quota", "quota", False, 1),
    (429, "invalid_api_key", "auth", False, 1),
    (429, "limit_burst_rate", "burst_rate", True, 2),
    (401, None, "auth", False, 1), (403, None, "auth", False, 1),
    (402, None, "quota", False, 1),
    (408, None, "timeout", True, 2),
    (409, None, "transient", True, 3), (425, None, "transient", True, 3),
    (500, None, "transient", True, 3), (502, None, "transient", True, 3),
    (503, None, "transient", True, 3), (504, None, "transient", True, 3),
    (400, "context_length_exceeded", "context_length", False, 1),
    (400, None, "request", False, 1), (404, None, "not_found", False, 1),
    (501, None, "request", False, 1),
])
def test_classification(status, code, reason, retry, attempts):
    from app.model.errors import classify_model_error
    decision = classify_model_error(api_error(status, code))
    assert decision.reason == reason
    assert decision.retryable is retry
    assert decision.max_attempts == attempts


def test_quota_word_in_rate_limit_message_does_not_disable_retry():
    from app.model.errors import classify_model_error
    decision = classify_model_error(api_error(429, "rate_limit_exceeded", text="Request quota per minute exceeded"))
    assert decision.retryable and decision.reason == "rate_limit"


@pytest.mark.parametrize("error,reason,attempts", [
    (openai.APIConnectionError(request=httpx.Request("POST", "https://model.invalid")), "connection", 3),
    (openai.APITimeoutError(request=httpx.Request("POST", "https://model.invalid")), "timeout", 2),
    (httpx.ReadTimeout("secret"), "timeout", 2),
    (httpx.RemoteProtocolError("secret"), "connection", 3),
])
def test_transport_classification(error, reason, attempts):
    from app.model.errors import classify_model_error
    decision = classify_model_error(error)
    assert decision.reason == reason and decision.retryable and decision.max_attempts == attempts


def test_retry_then_success_returns_one_message_and_safe_status(tmp_path):
    instance, delays = middleware()
    events, calls = [], []
    answer = Message(role="assistant", content="answer")

    async def request():
        calls.append(1)
        if len(calls) < 3:
            raise api_error(503)
        return answer

    result = asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, events), request))
    assert result is answer and len(calls) == 3 and delays == [1, 2]
    assert [p["attempt"] for k, p in events if p.get("phase") == "retry"] == [2, 3]
    assert "UPSTREAM_SECRET" not in str(events)


@pytest.mark.parametrize("status,code,attempts,delays", [
    (503, None, 3, [1, 2]), (429, "limit_burst_rate", 2, [5]),
    (408, None, 2, [1]), (429, "insufficient_quota", 1, []),
    (401, None, 1, []), (400, None, 1, []),
])
def test_exhaustion_is_safe_failure_not_assistant_success(tmp_path, status, code, attempts, delays):
    from app.model.errors import ModelCallError
    instance, waited = middleware()
    calls, events = [], []
    original = api_error(status, code)

    async def request():
        calls.append(1)
        raise original

    with pytest.raises(ModelCallError) as caught:
        asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, events), request))
    assert caught.value.attempts == attempts and len(calls) == attempts and waited == delays
    assert caught.value.__cause__ is original
    assert "UPSTREAM_SECRET" not in str(caught.value)


@pytest.mark.parametrize("header,expected", [("10", 10), ("0.5", 1), ("bad", 1), ("-1", 1), ("nan", 1)])
def test_retry_after_is_minimum_wait(tmp_path, header, expected):
    instance, delays = middleware(max_attempts=2)
    calls = []

    async def request():
        calls.append(1)
        if len(calls) == 1:
            raise api_error(429, headers={"Retry-After": header})
        return Message(role="assistant", content="ok")

    asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request))
    assert delays == [expected]


def test_retry_after_http_date():
    from app.model.errors import retry_after_seconds
    now = datetime.now(timezone.utc)
    error = api_error(429, headers={"Retry-After": format_datetime(now + timedelta(seconds=10), usegmt=True)})
    assert 9 <= retry_after_seconds(error, now=now) <= 10


@pytest.mark.parametrize("header,policy", [("61", {}), ("10", {"call_timeout_seconds": 0.05})])
def test_retry_after_outside_budget_does_not_retry_early(tmp_path, header, policy):
    from app.model.errors import ModelCallError
    instance, delays = middleware(**policy)
    calls = []

    async def request():
        calls.append(1)
        raise api_error(429, headers={"Retry-After": header})

    with pytest.raises(ModelCallError):
        asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request))
    assert len(calls) == 1 and delays == []


@pytest.mark.parametrize("error", [asyncio.CancelledError(), StatePersistenceError("checkpoint"), ValueError("local bug")])
def test_fatal_unknown_and_cancellation_propagate_unchanged(tmp_path, error):
    instance, delays = middleware()

    async def request():
        raise error

    with pytest.raises(type(error)) as caught:
        asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request))
    assert caught.value is error and not delays


def test_cancellation_during_retry_wait(tmp_path):
    from app.agents.model_error_handling_middleware import ModelErrorHandlingMiddleware
    from app.model.call_policy import ModelCallPolicy

    async def scenario():
        sleeping = asyncio.Event()
        calls = []

        async def sleep(seconds):
            sleeping.set()
            await asyncio.Event().wait()

        async def request():
            calls.append(1)
            raise api_error(503)

        instance = ModelErrorHandlingMiddleware(ModelCallPolicy(), sleep=sleep)
        task = asyncio.create_task(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1

    asyncio.run(scenario())


def test_empty_response_budget_is_once_per_run(tmp_path):
    from app.model.errors import ModelCallError
    instance, delays = middleware()
    state, ctx = ThreadState("thread", "alice"), context(tmp_path, [])
    calls = []

    async def empty():
        calls.append(1)
        return Message(role="assistant", content="")

    async def scenario():
        await instance.before_agent(state, ctx)
        for expected in [2, 3]:
            with pytest.raises(ModelCallError) as caught:
                await instance.wrap_model_call(state, ctx, empty)
            assert caught.value.reason == "empty_response" and len(calls) == expected

    asyncio.run(scenario())
    assert delays == [1]


def test_empty_response_retry_budget_is_shared_with_children(tmp_path):
    from app.model.errors import ModelCallError
    parent, parent_delays = middleware()
    child, child_delays = middleware()
    state, ctx = ThreadState("thread", "alice"), context(tmp_path, [])
    calls = []

    async def empty():
        calls.append(1)
        return Message(role="assistant", content="")

    async def scenario():
        await parent.before_agent(state, ctx)
        with pytest.raises(ModelCallError):
            await parent.wrap_model_call(state, ctx, empty)
        # 子 Context replace 保留同一个父 Run 的重试额度。
        await child.before_agent(state, replace(ctx))
        with pytest.raises(ModelCallError):
            await child.wrap_model_call(state, replace(ctx), empty)

    asyncio.run(scenario())
    assert len(calls) == 3 and parent_delays == [1] and child_delays == []


def test_tool_only_response_is_not_empty(tmp_path):
    instance, delays = middleware()
    message = Message(role="assistant", content="", tool_calls=[ToolCall("id", "read_file", {"path": "a"})])

    async def request():
        return message

    assert asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request)) is message
    assert delays == []


def test_attempt_and_call_deadlines_are_safe_and_do_not_hang(tmp_path):
    from app.model.errors import ModelCallError
    instance, delays = middleware(attempt_timeout_seconds=0.01, call_timeout_seconds=0.1)
    calls = []

    async def request():
        calls.append(1)
        await asyncio.Event().wait()

    with pytest.raises(ModelCallError) as caught:
        asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request))
    assert caught.value.reason == "timeout" and len(calls) == 1
    assert delays == []  # 1s backoff exceeds remaining 0.1s budget


def test_call_deadline_interrupts_active_attempt(tmp_path):
    from app.model.errors import ModelCallError
    instance, delays = middleware(attempt_timeout_seconds=1, call_timeout_seconds=0.01)

    async def request():
        await asyncio.Event().wait()

    with pytest.raises(ModelCallError) as caught:
        asyncio.run(instance.wrap_model_call(ThreadState("thread", "alice"), context(tmp_path, []), request))
    assert caught.value.reason == "call_timeout" and caught.value.attempts == 1 and not delays


@pytest.mark.parametrize("channel", ["on_text_delta", "on_reasoning_delta"])
def test_visible_partial_output_stops_retry_and_is_not_committed(tmp_path, channel):
    from app.agents.lead_agent import LeadAgent
    from app.model.errors import ModelCallError
    from app.tools.registry import ToolRegistry
    from app.tools.executor import ToolExecutor
    calls, events, snapshots = [], [], []

    class Model:
        async def chat(self, **kwargs):
            calls.append(1)
            await kwargs[channel]("partial")
            raise api_error(503)

        async def close(self):
            pass

    async def save(state):
        snapshots.append(list(state.messages))

    async def scenario():
        registry = ToolRegistry()
        state = ThreadState("thread", "alice", messages=[Message(role="user", content="test")])
        ctx = replace(context(tmp_path, events), save_checkpoint=save)
        with pytest.raises(ModelCallError) as caught:
            await LeadAgent(Model(), registry, ToolExecutor(registry)).run(state, ctx)
        assert caught.value.reason == "stream_interrupted"
        assert not any(m.role == "assistant" for m in state.messages)
        assert not snapshots

    asyncio.run(scenario())
    assert len(calls) == 1
    assert any(kind == "model.interrupted" for kind, _ in events)


def test_model_retry_does_not_repeat_preceding_tool(tmp_path):
    from app.agents.lead_agent import LeadAgent
    from app.tools.registry import ToolRegistry
    from app.tools.executor import ToolExecutor
    from app.domain.tools import ToolDefinition, ToolResult
    calls, executed = [], []

    class Tool:
        definition = ToolDefinition("count", "count", {})

        async def execute(self, call, context):
            executed.append(1)
            return ToolResult(tool_call_id=call.id, name="count", content="stored result")

    class Model:
        async def chat(self, **kwargs):
            calls.append(kwargs["messages"])
            if len(calls) == 1:
                return Message(role="assistant", content="", tool_calls=[ToolCall("call", "count", {})])
            if len(calls) == 2:
                raise api_error(503)
            assert kwargs["messages"][-1].content == "stored result"
            return Message(role="assistant", content="done")

        async def close(self):
            pass

    async def scenario():
        registry = ToolRegistry()
        registry.register(Tool())
        state = ThreadState("thread", "alice", messages=[Message(role="user", content="test")])
        instance, _ = middleware()
        result = await LeadAgent(Model(), registry, ToolExecutor(registry), middlewares=[instance]).run(state, context(tmp_path, []))
        assert result.messages[-1].content == "done"

    asyncio.run(scenario())
    assert len(calls) == 3 and len(executed) == 1


def test_child_context_maps_model_events(tmp_path):
    from app.subagents.executor import SubagentExecutor
    from app.domain.subagents import SubagentTask
    from app.tools.registry import ToolRegistry
    events = []
    task = SubagentTask(task_id="task", user_id="alice", thread_id="thread", run_id="run", tool_call_id="call", description="child", prompt="test")
    parent = ThreadState("thread", "alice", subtasks={task.task_id: task})
    executor = SubagentExecutor(parent_state=parent, context=context(tmp_path, events), tool_registry=ToolRegistry(), model_factory=lambda: None)

    async def scenario():
        child = executor._child_context(task)
        await child.record_event("model.status", {"phase": "retry"})
        await child.record_event("model.interrupted", {"message_id": "partial"})

    asyncio.run(scenario())
    assert [kind for kind, _ in events] == ["subagent.model.status", "subagent.model.interrupted"]
    assert all(payload["task_id"] == "task" for _, payload in events)
