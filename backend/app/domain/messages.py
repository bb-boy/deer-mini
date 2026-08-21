"""
你：读取 report.txt
模型：我要调用 read_file，参数是 report.txt
工具：文件内容是……
模型：根据文件内容，回答你……
"""

from typing import Any, List, Literal
from dataclasses import dataclass, field

role = Literal["user", "assistant", "system", "tool"]


@dataclass
class ToolCall:
    id: str                 # 工具调用的唯一标识符，一次run可能会有很多次同一个toolcall
    name: str              # 工具名称
    args: dict[str, Any]      #工具调用的参数  



@dataclass
class Message:
    role: role          
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list) #每次创建一个实例，都默认生成一个新的list
    tool_call_id: str | None = None