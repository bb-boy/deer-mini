"""Tests for backend/app/model/openai_compatible.py."""

# 在这里编写 pytest 的 test_* 函数。



import pytest
from app.model.openai_compatible import OpenAICompatibleModel
from app.domain.messages import Message, ToolCall
from app.model.openai_compatible import OpenAICompatibleModel
import pytest

def test_message_to_api():

    # Test the message_to_api function

    message = Message(
        role="user",
        content="Hello, how are you?"
    )

    expected_output = {
        "role": "user",
        "content": "Hello, how are you?"
    }

    assert OpenAICompatibleModel._message_to_api(message) == expected_output




    #处理带工具调用的message
    message = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(

                id= "tool_123",
                name= "my_tool",
                arguments= {"arg1": "value1"}

        ),ToolCall(

                id= "tool_456",
                name= "my_tool2",
                arguments= {"arg2": "value2"}

        )]
    )

    #这里是json
    expected_output = {
        'role':'assistant',
        'content': '',
        'tool_calls': [
            {

                'id': 'tool_123',
                'type': 'function',
                'function': {
                    'name': 'my_tool',
                    'arguments': "{\"arg1\": \"value1\"}"
                }
            },
            {
                'id': 'tool_456',
                'type': 'function',
                'function': {
                    'name': 'my_tool2',
                    'arguments': "{\"arg2\": \"value2\"}"
                }
            }
        ]
    }

    assert OpenAICompatibleModel._message_to_api(message) == expected_output




    #处理需要需要推理内容的message

    message = Message(
        role="assistant",
        content="",
        reasoning_content="This is the reasoning content."
    )

    expected_output = {
        'role': 'assistant',
        'content': '',
        'reasoning_content': "This is the reasoning content."
    }

    assert OpenAICompatibleModel._message_to_api(message) == expected_output


    #a验证没有带 tool_call_id的toolmessage会抛出异常
    message = Message(
        role="tool",
        content="This is the tool result.",

    )
    with pytest.raises(ValueError, match="工具id缺失"):
        OpenAICompatibleModel._message_to_api(message)
