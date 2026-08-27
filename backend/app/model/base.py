"""
所有聊天模型都要遵守的统一接口。

Agent Loop 只依赖 ChatModel，
不需要知道背后使用的是 USTC DeepSeek、Qwen 还是其他模型。

"""



from collections.abc import Awaitable, Callable
from typing import Protocol

from app.domain.messages import Message
from app.domain.tools import ToolDefinition



TextDeltaHandler = Callable[[str], Awaitable[None]]  # 用于处理模型输出的回调函数类型函数类型



class ChatModel(Protocol):
    """
    聊天模型的统一接口。
    """

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,

        on_text_delta: TextDeltaHandler | None = None,
    ) -> Message:
        """
        发送一条消息给聊天模型，并获取模型的回复。

        :param messages: 消息列表，包含用户和模型的对话历史。
        :param tools: 可用的工具定义列表，模型可以调用这些工具。
        :param thinking_enabled: 是否启用模型的思考模式。
        :param reasoning_effort: 可选的推理努力参数，指定模型在生成回复时的推理深度或复杂度。
        :param on_text_delta: 可选的回调函数，用于处理模型输出的增量文本。
        :return: 消息
        """
        ...