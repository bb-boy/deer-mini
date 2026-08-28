"""Tests for backend/app/model/factory.py."""

# 在这里编写 pytest 的 test_* 函数。

from email.mime import message

from app.model.openai_compatible import OpenAICompatibleModel

from app.model.factory import ModelFactory

from app.domain.messages import Message

import asyncio
from app.tools.registry import ToolRegistry
from app.tools.read_file import ReadFileTool

def test_model_factory():

    factory = ModelFactory()

    chatModel = factory.create_chat_model()

    

    assert isinstance(chatModel, OpenAICompatibleModel)

    message = []
    tools = []

    user_message   = Message(
        role="user",
        content="现在是几点了"
    )

    message.append(user_message)

    async def run_test():
        return await chatModel.chat(message,tools)
    result = asyncio.run(run_test())



    assert result is not None

    assert result.content is not None
    assert isinstance(result.content, str)  
    print(result.content)



def test_toolcall_model_factory():

    factory = ModelFactory()

    chatModel = factory.create_chat_model()

    assert isinstance(chatModel, OpenAICompatibleModel) 
    messages = []
    tools = []

    user_message = Message(
        role="user",
        content="帮我读一下工作目录下面的代码"
    )
    messages.append(user_message)

    tool_registry = ToolRegistry()

    tool_registry.register(ReadFileTool())
    tools = tool_registry.definitions()
    async def run_test():
        return await chatModel.chat(messages, tools)
    result = asyncio.run(run_test())
    assert result is not None
    assert result.content is not None
    assert isinstance(result.content, str)  

    assert result.tool_calls is not None
    assert isinstance(result.tool_calls, list)
    print(result.tool_calls)
    print(result.content)
    
