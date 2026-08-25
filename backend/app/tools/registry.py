"""
功能：保存已注册工具，并按 read_file 这样的名字取出工具。
输入：一个 Tool，例如 ReadFileTool。
输出：对应的 Tool，或“找不到工具”。
副作用：只有内存中的字典，没有 SQLite、文件或 Stream。

"""



from app.tools.base import Tool
from app.domain.tools import ToolDefinition


class ToolRegistry:

    def __init__(self):

        #key是工具名，value是Tool对象，例如 ReadFileTool，包含定义和执行方法
        self._tools: dict[str, Tool] = {}



    def register(self, tool: Tool) -> None:
        """
        注册一个工具，返回所有已注册的工具。
        :param tool: 一个符合 Tool 协议的对象，例如 ReadFileTool。
        :return: None
        """
        name = tool.definition.name

        if name in self._tools:
            raise ValueError(f"工具 {name} 已经注册过了。")
        
        self._tools[name] = tool


    def get(self, name: str) -> Tool | None:
        """
        按工具名取出一个工具。
        :param name: 工具名，例如 read_file。
        :return: 对应的 Tool，或 None。
        """
        
        return self._tools.get(name)

    


    #返回工具定义
    def definitions(self) -> list[ToolDefinition]:


        """
        返回所有已注册工具的定义。
        :return: 所有已注册工具的定义列表。
        """
        
        return [tool.definition for tool in self._tools.values()]