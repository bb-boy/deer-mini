"""离线供应商协议验证：usage 流包、输出限额及已有位置参数兼容。"""
import asyncio
from types import SimpleNamespace as NS

import pytest

from app.domain.messages import Message
from app.model.config import get_model_profile
from app.model.openai_compatible import OpenAICompatibleModel


def chunk(text='', usage=None, *, empty=False):
    return NS(usage=NS(prompt_tokens=usage), choices=[] if empty else [NS(delta=NS(content=text, reasoning_content=None, tool_calls=None), finish_reason='stop')])


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for part in self.chunks:
            if isinstance(part, BaseException):
                raise part
            yield part

    async def close(self):
        self.closed = True


def model_client(create):
    model = object.__new__(OpenAICompatibleModel)
    model._profile = get_model_profile('siliconflow-deepseek-flash')
    model._model_name = model._profile.model_id
    model._client = NS(chat=NS(completions=NS(create=create)))
    return model


@pytest.mark.parametrize('empty', [False, True])
def test_usage_with_or_without_choices_and_output_limit(empty):
    async def scenario():
        stream = Stream([chunk('hello'), chunk(usage=321, empty=empty)])
        requests = []
        async def create(**kwargs):
            requests.append(kwargs)
            return stream
        model = model_client(create)
        received = []
        async def on_text(text):
            received.append(text)
        # The old callback positions remain unchanged.
        result = await model.chat([Message(role='user', content='hi')], [], False, None, on_text, None, max_output_tokens=8000)
        assert result.content == 'hello' and received == ['hello'] and stream.closed
        assert model.last_prompt_tokens == 321
        assert requests[0]['stream_options'] == {'include_usage': True}
        assert requests[0]['max_tokens'] == 8000
    asyncio.run(scenario())


@pytest.mark.parametrize('usage', [True, False, 0, -1, '1', None])
def test_invalid_usage_not_recorded(usage):
    async def create(**kwargs):
        return Stream([chunk('answer', usage)])
    model = model_client(create)
    asyncio.run(model.chat([], []))
    assert model.last_prompt_tokens is None


def test_usage_reset_before_failed_api_attempt():
    model = None
    async def create(**kwargs):
        assert model.last_prompt_tokens is None
        raise RuntimeError('request failed')
    model = model_client(create)
    model.last_prompt_tokens = 999
    with pytest.raises(RuntimeError):
        asyncio.run(model.chat([], []))
    assert model.last_prompt_tokens is None


@pytest.mark.parametrize('limit', [True, 0, -1, '8000'])
def test_invalid_output_limit_rejected_before_api(limit):
    async def create(**kwargs):
        raise AssertionError('API must not run')
    with pytest.raises(ValueError):
        asyncio.run(model_client(create).chat([], [], max_output_tokens=limit))
