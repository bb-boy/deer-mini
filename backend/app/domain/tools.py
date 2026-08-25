"""
功能
定义工具之间传递的两种数据：工具说明书和工具执行结果。
输入
- name：工具名，例如 read_file。
- description：告诉模型这个工具能做什么。
- parameters：模型调用工具时允许传哪些参数。
- tool_call_id：关联模型发起的某一次具体调用。
- content：工具返回给模型的文字。
- is_error：这次工具是否失败。
输出
- ToolDefinition：给模型看的工具说明书。
- ToolResult：给 Agent Loop 处理的工具执行结果。

"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolDefinition:

    """
    工具说明书,给模型看的说明书
    """
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolResult:
    
    """
    工具执行结果,给 Agent Loop 处理的工具执行结果
    """

    
    tool_call_id: str
    name: str
    content: str

    #默认工具执行结果是成功的,如果失败了,就把is_error设置为True
    is_error: bool = False