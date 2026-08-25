



import asyncio

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from pathlib import Path



MAX_CONTENT_CHARS = 10000

class ReadFileTool:


   


    definition = ToolDefinition(
        name="read_file",
        description="读取指定路径的文件内容，返回给模型。注意：只能读取当前 Thread 的工作目录下的文件，不能读取其他目录的文件。",
        parameters={
            "type": "object",   #JSON Schema中key，value这种形式叫做object
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于当前工作目录的文件路径，例如 report.txt。",
                },
            },
            "required": ["path"],
            "additionalProperties": False,  

        },
    )


    async def execute(self, call:ToolCall, context:RuntimeContext) -> ToolResult:
        """
        执行工具的主要方法。所有真实工具都必须实现这个方法。
        :param arguments: 模型给工具的参数，例如 {"path": "report.txt"}。
        :param context: 当前 Run 的上下文；其中的 workspace_path 表示这个工具唯一允许操作的 Thread 工作目录。
        :return: ToolResult：统一的成功或失败结果。
        """
       
        #1 从模型调用中获取文件相对路径
        raw_path = call.arguments.get("path")

        #检查路径是否为空或者是否以一个字符串，如果不是，就返回一个错误的ToolResult
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="文件路径不能为空。",
                is_error=True,
            )


        #2 判断是否是绝对路径，如果是绝对路径，就返回一个错误的ToolResult
        requeted_path = Path(raw_path)

        if requeted_path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="只能读取当前工作目录下的文件，不能读取绝对路径的文件。",
                is_error=True,
            )

        # 判断当前工作目录是否为空
        if context.workspace_path is None:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="当前工作目录不存在。",
                is_error=True,
            )


        #3 拼接出文件的绝对路径
        try:

            #把/home/alice/workspace/../../other_thread/secret.txt中的..解析掉
            workspace = Path(context.workspace_path).resolve() 
            target = (workspace / requeted_path).resolve()


            #防止最后的路径跑到工作目录之外
            target.relative_to(workspace)  # 检查 target 是否在 workspace 下，如果不在，会抛出 ValueError
        except ValueError:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="只能读取当前工作目录下的文件，不能读取其他目录的文件。",
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
