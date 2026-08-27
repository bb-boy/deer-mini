
"""
把python的message对象转化为OpenAI兼容的字典，方便存储和传输
同时吧OpenAI兼容的字典转化为python的message对象，方便从存储和传输中恢复

{
    # ① 告诉模型接口：这是“函数型工具”
    "type": "function",

    "function": {
        "name": "read_file",
        "description": "读取文本文件",

        # ② 告诉模型：调用 read_file 时，参数该怎样填写
        "parameters": {
            "type": "object",
        },
    },
}

"""


import json
from typing import Any

from app.domain.messages import Message, ToolCall
from openai import AsyncOpenAI
from app.domain.tools import ToolDefinition
from app.model.base import TextDeltaHandler
from dataclasses import dataclass, field




#用来收集流式返回的工具碎片
@dataclass
class _ToolCallPart:
    id: str = ""
    name: str = ""
    argument_json: str = ""



class OpenAICompatibleModel:
    """
    OpenAI兼容的客户端，model = OpenAiCompatibleMOdel(api_key, base_url, model_name),
    通过self._model_name来访问模型名称

    """




    def  __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str
        ) ->None:

        if not api_key:
            raise ValueError("API key is required")
        if not base_url:
            raise ValueError("Base URL is required")
        if not model_name:
            raise ValueError("Model name is required")


        self._model_name= model_name




        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )



    #这是一个静态方法，传入message对象就可以使用
    @staticmethod

    #处理message对象，把message对象转化为OpenAI兼容的字典
    def _message_to_api(message: Message) ->  dict[str, Any]:

        """
        输入：
        - message：一条 deer_mini 内部消息，可能是用户消息、模型消息或工具结果。
        输出：
        - 一个 Python 字典 dict[str, Any]。
        """


        #处理公共字段部分
        api_message: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }


        #处理工具调用部分
        if message.role == "assistant" and message.tool_calls :
            api_message["tool_calls"] = [
                {
                    "id": tool_call.id,

                    # 告诉模型接口：这是“函数型工具”
                    "type": "function",
                    "function": {
                    "name": tool_call.name,

                    #Openaicompletion接口要求arguments是一个json字符串，而不是一个python字典，把dic转位json
                    "arguments": json.dumps(tool_call.arguments,ensure_ascii=False),
                }
                }
                for tool_call in message.tool_calls
            ]


            #处理推理内容部分
        if message.role == "assistant" and (message.reasoning_content is not None):

            #这里的None表示模型没有返回内容，如果使用""则表示模型返回了空字符串,所以这里使用None来表示模型没有返回内容
            api_message["reasoning_content"] = message.reasoning_content

            #处理tool_call.id缺失
        if message.role == "tool":


            #工具执行的结果必须加上tool_call_id，否则模型无法知道这个工具结果是属于哪一次工具调用的
            if not message.tool_call_id:
                raise ValueError("工具id缺失")

            api_message["tool_call_id"] = message.tool_call_id


        return api_message




    #工具类转化为OpenAI兼容的字典
    @staticmethod
    def _tool_to_api(tool: ToolDefinition) -> dict[str, Any]:

        """
        输入：
        ToolDefiniton():
            name: str
            description: str
            parameters: dict[str, Any]

        输出：
        - 一个 Python 字典 dict[str, Any]，符合 OpenAI 的函数调用格式。
        {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定路径的文件内容",
            "parameters": {...},
                        },


        """

        api_tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        }

        return api_tool





    async def  chat(self,
        messages: list[Message],
        tools: list[ToolDefinition],
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        on_text_delta: TextDeltaHandler | None = None) -> Message:


        """
        Chat with the OpenAI-compatible model.

        输入
        - messages：当前完整对话历史；
        - tools：允许模型使用的工具说明；
        - thinking_enabled：是否开启思考模式；
        - reasoning_effort：思考强度；
        - on_text_delta：每收到一小段文字时调用的异步函数。
        输出
        返回完整的：
        Message(role="assistant", ...)

        Message
            → 转成接口字典
            → 请求 USTC DeepSeek
            → 模型不断返回文字片段
            → 每个片段通过 on_text_delta 实时推送
            → 同时拼成完整 assistant Message

        """

        #把Message对象转化为OpenAI兼容的字典
        api_messages = [self._message_to_api(message) for message in messages]

        #把ToolDefinition对象转化为OpenAI兼容的字典
        api_tools = [self._tool_to_api(tool) for tool in tools]

        #构造一个request字典，包含messages和tools
        request :dict[str, Any] = {
            "model": self._model_name,
            "messages": api_messages,

            #开启流式返回
            "stream": True,


            #sdk没有thinking这个，放到extra_body里，USTC DeepSeek会识别
            #"thinking": {"type": "enabled"}是deepseek风格的扩展字段
            "extra_body": {
                "thinking": {
                    "type": "enabled" if thinking_enabled else "disabled",
                }
            }
        }


        #如果tools为空就不传入
        if api_tools:
            request["tools"] = api_tools

        #如果reasoning_effort不为空就传入
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort


        #调用USTC DeepSeek的chat.completions.create接口，返回一个异步生成器
        response_stream = await self._client.chat.completions.create(**request)


        #收集delta的content
        text_parts = []

        #收集delta的reasoning_content
        reasoning_parts = []

        #收集delta的tool_calls碎片，int是tool_call的index，_ToolCallPart是tool_call的内容
        tool_call_parts: dict[int, _ToolCallPart] = {}

        #返回toolcall完整的
        completed_tool_calls: list[ToolCall] = []


        """
        # chunk 1：声明这条流属于 assistant
            {
                "id": "chatcmpl_abc123",
                "object": "chat.completion.chunk",
                "created": 1760000000,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": ""
                        },
                        "finish_reason": None
                    }
                ]
            }

            # chunk 2：第一个文字片段
                {
                    "id": "chatcmpl_abc123",
                    "object": "chat.completion.chunk",
                    "created": 1760000000,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": "你好！"
                            },
                            "finish_reason": None
                        }
                    ]
                }
                # chunk 3：结束
                        {
                            "id": "chatcmpl_abc123",
                            "object": "chat.completion.chunk",
                            "created": 1760000000,
                            "model": "deepseek-v4-pro",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop"
                                }
                            ]
                        }


                可以返回多个choice，但是要指定choice的数量

        """


        """
           choice = {
            "index": 0,

            "delta": {
                "reasoning_content": "<模型返回的一小段思考模式数据>",
                "content": None,
            },

            "finish_reason": None,
        }
            """


        #遍历流式返回的chunk
        async for chunk in response_stream:
            #SDK 把服务器返回的 JSON 数据，包装成了一个 Python 类的实例
            #chunk是一个类对象，不是字典，所以不能直接用chunk["choices"]，而是要用chunk.choices
            #判断choices是否为空
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta


            #1 处理工具调用内容

            ## 拿到所有的工具调用碎片，放到列表中，ID、index 、tpye 、function等，如果没有tool_calls就返回空列表
            tool_call_delta = getattr(delta, "tool_calls", None) or []

            ## 遍历工具调用碎片
            for tool_call in tool_call_delta:
                #拿到工具调用index
                index = tool_call.index
                #tool_call_parts中如果已经有就返回对象的_ToolCallPart(),如果没有就创建一个新的_ToolCallPart()，并返回
                part =tool_call_parts.setdefault(index, _ToolCallPart())


                #如果碎片有id，就把它加入part.id,一般id不会被拆开，所以这里直接赋值即可
                if tool_call.id:
                    part.id = tool_call.id

                #拿到函数对象，没有就返回None，有就返回字典
                delta_function = getattr(tool_call, "function", None)

                #如果函数对象为空，就跳过
                if not delta_function:
                    continue


                #如果函数名被拆分就拼接
                if delta_function.name:
                    part.name += delta_function.name

                #如果函数参数被拆分就拼接
                if delta_function.arguments:
                    part.argument_json += delta_function.arguments


            #2 处理推理内容，先取出来推理内容,有就返回到reasoning_delta，没有就返回None
            reasoning_delta = getattr(delta, "reasoning_content", None)

            #如果reasoning_delta不为None，就把它加入reasoning_parts，并调用on_reasoning_delta
            if reasoning_delta is not None:
                reasoning_parts.append(reasoning_delta)



            #3 处理文字内容，先取出来文字内容,有就返回到text_delta，没有就返回None

            text_delta = delta.content


            #模型返回内容是none，就进行下次循环
            if not text_delta:
                continue

            #如果有on_text_delta，就调用它，把text_delta传进去，实时传给SSE
            if on_text_delta is not None:
                await on_text_delta(text_delta)


            #把text_delta加入text_parts，拼成完整的assistant Message
            text_parts.append(text_delta)


        #拼接完整的content
        full_text = "".join(text_parts)

        #拼接完整的推理内容
        full_reasoning = "".join(reasoning_parts) if reasoning_parts else None

        #拼接完整的tool_calls ，按照顺序index排序，拼接成完整的ToolCall对象列表
        for index in sorted(tool_call_parts):
            part = tool_call_parts[index]

            #id和name不能为空，否则无法创建ToolCall对象
            if not part.id:
                raise ValueError(f"第{index}个工具调用缺少id")
            if not part.name:
                raise ValueError(f"第{index}个工具调用缺少name")


            #把argument_json是否缺失
            if not part.argument_json:
                arguments_json= "{}"
            else:
                arguments_json = part.argument_json

            try:
                arguments = json.loads(arguments_json)
            except json.JSONDecodeError as error:
                raise ValueError(f"第{index}个工具的参数不是合法的json: {arguments_json}") from error


            #判断转换为arguments是否是合法的dict
            if not isinstance(arguments, dict):
                raise ValueError(f"第{index}个工具的参数不是合法的dict: {arguments}")

            completed_tool_calls.append(
                ToolCall(
                    id=part.id,
                    name=part.name,
                    arguments=arguments
                )
            )





        return Message(
            role="assistant",
            content=full_text,
            reasoning_content=full_reasoning,
            tool_calls=completed_tool_calls
        )








    #流式返回的工具调用示例
    """
            {
        "choices": [
            {
            "index": 0,
            "delta": {
                "tool_calls": [
                {
                    "index": 0,
                    "id": "call_read_report",
                    "type": "function",
                    "function": {
                    "name": "read_file",
                    "arguments": "{\"path\": \"report.txt\"}"
                    }
                },
                {
                    "index": 1,
                    "id": "call_read_plan",
                    "type": "function",
                    "function": {
                    "name": "read_file",
                    "arguments": "{\"path\": \"plan.txt\"}"
                    }
                }
                ]
            },
            "finish_reason": null
            }
        ]
        }

    """
