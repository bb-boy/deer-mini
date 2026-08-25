"""
功能
规定所有真实工具都必须提供“说明书”和“执行方法”。后面的 ReadFileTool 会遵守这个约定。
输入
- arguments：模型给工具的参数，例如 {"path": "report.txt"}。
- context：当前 Run 的上下文；其中的 workspace_path 表示这个工具唯一允许操作的 Thread 工作目录。
输出
- ToolResult：统一的成功或失败结果。
副作用
- 这里也没有。Protocol 只是规则，不会执行工具。
"""


from typing import Any, Protocol
from app.domain.messages import ToolCall
from app.domain.tools import ToolResult, ToolDefinition
from app.runtime.context import RuntimeContext



class Tool(Protocol):
    """
    规定工具类要有哪些属性和方法。Protocol 只是规则，不会执行工具。
    """

    #所有符合Tool协议的对象都要有一个definiton属性
    definition: ToolDefinition


    #所有符合Tool协议的对象都要有一个execute方法，接受arguments和context参数，返回ToolResult
    async def execute(
        self, 
        call:ToolCall,
        context: RuntimeContext) -> ToolResult:

        """
        执行工具的主要方法。所有真实工具都必须实现这个方法。
        :param arguments: 模型给工具的参数，例如 {"path": "report.txt"}。
        :param context: 当前 Run 的上下文；其中的 workspace_path 表示这个工具唯一允许操作的 Thread 工作目录。
        :return: ToolResult：统一的成功或失败结果。
        """
        ...


    """

    
    模型回复：
    - 我要调用 read_file
    - 参数：{"path": "report.txt"}
    - 调用编号：call_abc123


    把模型回复的json转位为ToolCall对象，方便后续调用工具
    ToolCall(
        id="call_abc123",
        name="read_file",
        arguments={"path": "report.txt"}
    )
    
    工具执行返回结果：
    ToolResult(
        tool_call_id="call_abc123",
        name="read_file",
        content="文件内容是……",
        is_error=False
    )
    
    
    """