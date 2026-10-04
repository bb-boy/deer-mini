"""Validate completion and stream cleanup before tool execution."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from app.model.openai_compatible import OpenAICompatibleModel
from app.model.config import get_model_profile


def chunk(text=None, reasoning=None, tools=None, finish=None):
    return NS(choices=[NS(delta=NS(content=text, reasoning_content=reasoning, tool_calls=tools), finish_reason=finish)])


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for item in self.chunks:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def close(self):
        self.closed = True


def model_with_stream(stream):
    model = object.__new__(OpenAICompatibleModel)
    model._profile = get_model_profile("siliconflow-deepseek-flash")
    model._model_name = model._profile.model_id

    async def create(**kwargs):
        return stream

    model._client = NS(chat=NS(completions=NS(create=create)))
    return model


def test_sdk_retries_disabled_and_timeouts_explicit(monkeypatch):
    import app.model.openai_compatible as module
    options = []
    monkeypatch.setattr(module, "AsyncOpenAI", lambda **kwargs: options.append(kwargs) or NS())
    monkeypatch.setattr(module, "wrap_openai", lambda client: client)
    OpenAICompatibleModel("placeholder", "https://model.invalid", get_model_profile("siliconflow-deepseek-flash"))
    assert options[0]["max_retries"] == 0
    timeout = options[0]["timeout"]
    assert timeout.connect == 10 and timeout.read == 120 and timeout.write == 30 and timeout.pool == 10


def test_stream_requires_finish_marker_and_closes():
    from app.model.errors import IncompleteModelResponseError
    stream = Stream([chunk("half")])
    with pytest.raises(IncompleteModelResponseError):
        asyncio.run(model_with_stream(stream).chat([], []))
    assert stream.closed


def test_successful_stream_closes_and_returns_text():
    stream = Stream([chunk("hello"), chunk(finish="stop")])
    result = asyncio.run(model_with_stream(stream).chat([], []))
    assert result.content == "hello" and stream.closed


@pytest.mark.parametrize("error", [RuntimeError("local"), asyncio.CancelledError()])
def test_failed_stream_closes_and_preserves_original_error(error):
    stream = Stream([error])
    with pytest.raises(type(error)) as caught:
        asyncio.run(model_with_stream(stream).chat([], []))
    assert caught.value is error and stream.closed


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_truncated_or_refused_response_is_not_success(finish):
    from app.model.errors import InvalidModelResponseError
    stream = Stream([chunk("partial", finish=finish)])
    with pytest.raises(InvalidModelResponseError):
        asyncio.run(model_with_stream(stream).chat([], []))
    assert stream.closed


def test_malformed_tool_parameters_do_not_expose_raw_arguments():
    from app.model.errors import InvalidModelResponseError
    tools = [NS(index=0, id="call", function=NS(name="bash", arguments="UPSTREAM_SECRET{"))]
    stream = Stream([chunk(tools=tools), chunk(finish="tool_calls")])
    with pytest.raises(InvalidModelResponseError) as caught:
        asyncio.run(model_with_stream(stream).chat([], []))
    assert "UPSTREAM_SECRET" not in str(caught.value) and stream.closed


@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"max_attempts": True}, {"read_timeout_seconds": float("nan")}, {"base_delay_seconds": -1}])
def test_policy_rejects_invalid_configuration(kwargs):
    from app.model.call_policy import ModelCallPolicy
    with pytest.raises(ValueError):
        ModelCallPolicy(**kwargs)


def test_policy_environment_configuration(monkeypatch):
    from app.model.call_policy import ModelCallPolicy
    monkeypatch.setenv("DEER_MINI_MODEL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("DEER_MINI_MODEL_READ_TIMEOUT_SECONDS", "180")
    policy = ModelCallPolicy.from_env()
    assert policy.max_attempts == 2 and policy.read_timeout_seconds == 180
    monkeypatch.setenv("DEER_MINI_MODEL_MAX_ATTEMPTS", "not-an-integer")
    with pytest.raises(ValueError, match="DEER_MINI_MODEL_MAX_ATTEMPTS"):
        ModelCallPolicy.from_env()
