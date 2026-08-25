"""Tests for backend/app/tools/read_file.py."""

# 在这里编写 pytest 的 test_* 函数。




import asyncio

from app.domain.messages import ToolCall
from app.runtime import context
from app.runtime.context import RuntimeContext
from app.tools.read_file import ReadFileTool


def test_read_file_tool_definition():
    """
    测试 ReadFileTool 的定义是否符合预期。
    """
    from app.tools.read_file import ReadFileTool

    tool = ReadFileTool()

    assert tool.definition.name == "read_file"
    assert "读取指定路径的文件内容" in tool.definition.description
    assert "path" in tool.definition.parameters["properties"]
    assert tool.definition.parameters["required"] == ["path"]



def test_read_file_tool_execute(tmp_path):
    """
    测试 ReadFileTool 的 execute 方法是否能正确读取文件内容。
    """


    workspace = tmp_path / "thread_001" / "workspace"
    workspace.mkdir(parents=True)


    report_path = workspace / "report.txt"
    report_content = "这是一个测试报告。"
    report_path.write_text(report_content, encoding="utf-8")

    call = ToolCall(
        id="call_001",
        name="read_file",
        arguments={"path": "report.txt"}
    )


    context = RuntimeContext(
        user_id="test_user",
        thread_id="thread_001",
        run_id="run_001",
        workspace_path=str(workspace),
    
        record_event = None,
    
        save_checkpoint=None,
    )


    result = asyncio.run(
        ReadFileTool().execute(call, context)
    )

    
    # 6. 验证用户真正关心的结果。
    assert result.is_error is False
    assert result.tool_call_id == "call_001"
    assert result.content == "这是一个测试报告。"
