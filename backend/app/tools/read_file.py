



import asyncio

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from pathlib import Path
from app.filesystem.thread_paths import ThreadPaths



MAX_CONTENT_CHARS = 10000

class ReadFileTool:


   


    definition = ToolDefinition(
        name="read_file",
        description=(
            "读取指定路径的文件内容。仅限当前 Thread 的 "
            "workspace、uploads、outputs 目录中的 UTF-8 文本文件。"
        ),
        parameters={
            "type": "object",   #JSON Schema中key，value这种形式叫做object
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "带区域前缀的路径，例如 uploads/资料.txt、outputs/report.md、"
                        "workspace/script.py；也支持文件虚拟绝对路径，例如 "
                        "/mnt/user-data/uploads/report.txt。"
                        "也兼容 workspace 内的相对路径，例如 report.txt。"
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,  

        },
    )


    async def execute(self, call:ToolCall, context:RuntimeContext) -> ToolResult:
        """
        执行工具的主要方法。所有真实工具都必须实现这个方法。
        :param call: 模型的工具调用，包含调用 ID 和 path 文件路径参数。
        :param context: 当前 Run 的上下文；从服务器 workspace 定位当前 Thread 的三个标准目录。
        :return: ToolResult：统一的成功或失败结果。
        """
       
        #1 从模型调用中获取虚拟绝对路径或 workspace 相对路径
        raw_path = call.arguments.get("path")

        #检查路径是否为空或者是否以一个字符串，如果不是，就返回一个错误的ToolResult
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="文件路径不能为空。",
                is_error=True,
            )


        # 当前 Thread 的目录由 Runtime 提供。
        if not context.workspace_path:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="当前工作目录不存在。",
                is_error=True,
            )

        try:
            # workspace 的父目录就是包含三个标准目录的 Thread 目录。
            thread_dir = Path(context.workspace_path).parent
            paths = ThreadPaths(thread_dir)

            # 统一处理虚拟绝对路径和旧的 workspace 相对路径。
            target = paths.resolve_agent_path(raw_path)
        except (ValueError, OSError, RuntimeError) as error:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"文件路径无效：{error}",
                is_error=True,
            )

        #4 处理文件不存在的情况
        if not target.exists():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"文件 {raw_path} 不存在。",
                is_error=True,
            )
        #处理是文件夹的情况
        if not target.is_file():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"{raw_path} 是一个文件夹，不能读取。",
                is_error=True,
            )

        #5 读取文件内容
        try:

            #1. 把读文件任务交给一个线程
            #2. 当前协程暂停等待
            #3. 事件循环此时可以去处理 Stream 推送等其他协程
            #4. 文件读完后，当前协程恢复
            #5. 得到文件文字
            content = await asyncio.to_thread(target.read_text,encoding ="utf-8")


        except UnicodeDecodeError:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"文件 {raw_path} 不是一个 UTF-8 编码的文本文件，无法读取。",
                is_error=True,
            )

        except OSError as e:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"读取文件 {raw_path} 时发生错误：{str(e)}",
                is_error=True,
            )

        #6 如果文件内容过长，就截断
        if(len(content) > MAX_CONTENT_CHARS):
            content = content[:MAX_CONTENT_CHARS] + "\n\n[文件内容过长，已截断]"


        
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=content,
        )
