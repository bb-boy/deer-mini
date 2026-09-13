"""不请求外部模型，验证供应商思考片段实时送达且完整 Message 仍可保存。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.messages import Message
from app.model.openai_compatible import OpenAICompatibleModel


@pytest.mark.parametrize("subscribe_reasoning", [True, False])
def test_reasoning_is_delivered_before_answer_and_retained_in_message(subscribe_reasoning):
    async def scenario():
        received = []

        async def reasoning(text):
            received.append(("reasoning", text))

        async def answer(text):
            received.append(("text", text))

        def chunk(thought=None, text=None):
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                reasoning_content=thought, content=text, tool_calls=None,
            ))])

        async def stream():
            yield chunk("先读取")
            # 下一段尚未产生时，前一段已经由回调交给界面。
            assert received == ([("reasoning", "先读取")] if subscribe_reasoning else [])
            yield chunk("资料。")
            yield chunk("", "这是回答。")

        model = object.__new__(OpenAICompatibleModel)
        model._model_name = "offline-fixture"
        model._profile = SimpleNamespace(
            replay_reasoning_content=True, supports_thinking=True,
            supports_reasoning_effort=False, thinking_format="deepseek",
        )
        model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=stream()),
        )))
        result = await model.chat(
            messages=[Message(role="user", content="读取资料")], tools=[],
            thinking_enabled=True, on_text_delta=answer,
            on_reasoning_delta=reasoning if subscribe_reasoning else None,
        )
        assert result.reasoning_content == "先读取资料。"
        assert result.content == "这是回答。"
        expected = [("reasoning", "先读取"), ("reasoning", "资料。")] if subscribe_reasoning else []
        assert received == expected + [("text", "这是回答。")]

    asyncio.run(scenario())
