"""Tests for backend/app/tools/read_file.py."""

# 在这里编写 pytest 的 test_* 函数。




import asyncio

import pytest

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



@pytest.mark.parametrize(
    ("directory", "requested_path"),
    [
        ("workspace", "report.txt"),
        ("workspace", "workspace/report.txt"),
        ("uploads", "uploads/report.txt"),
        ("outputs", "outputs/report.txt"),
        ("workspace", "/mnt/user-data/workspace/report.txt"),
        ("uploads", "/mnt/user-data/uploads/report.txt"),
        ("outputs", "/mnt/user-data/outputs/report.txt"),
    ],
)
def test_read_file_tool_execute(tmp_path, directory, requested_path):
    """
    测试 ReadFileTool 的 execute 方法是否能正确读取文件内容。
    """


    thread_dir = tmp_path / "thread_001"
    workspace = thread_dir / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (thread_dir / name).mkdir(parents=True)

    report_path = thread_dir / directory / "report.txt"
    report_content = "这是一个测试报告。"
    report_path.write_text(report_content, encoding="utf-8")

    call = ToolCall(
        id="call_001",
        name="read_file",
        arguments={"path": requested_path}
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


@pytest.mark.parametrize(
    "requested_path",
    [
        "../../thread_002/workspace/secret.txt",
        "uploads/../../thread_002/workspace/secret.txt",
        "uploads/../workspace/escape.txt",
        "/mnt/user-data/workspace/../../thread_002/workspace/secret.txt",
        "/mnt/user-data/workspace/escape.txt",
    ],
)
def test_read_file_rejects_path_outside_current_workspace(tmp_path, requested_path):
    """
    即使模型试图使用 ../../ 越出当前 workspace，
    ReadFileTool 也必须拒绝读取其他 Thread 的文件。
    """

    # 当前用户正在运行的 Thread 工作目录。
    own_workspace = tmp_path / "thread_001" / "workspace"
    own_workspace.mkdir(parents=True)

    # 模拟另一个 Thread 的工作目录和秘密文件。
    other_workspace = tmp_path / "thread_002" / "workspace"
    other_workspace.mkdir(parents=True)
    (other_workspace / "secret.txt").write_text(
        "这是不应被读取的秘密",
        encoding="utf-8",
    )

    # 符号链接和 .. 都不能成为访问另一个 Thread 的入口。
    (own_workspace / "escape.txt").symlink_to(other_workspace / "secret.txt")

    # 模型试图从 thread_001 向上返回，或通过链接进入 thread_002。
    call = ToolCall(
        id="call_outside_001",
        name="read_file",
        arguments={
            "path": requested_path,
        },
    )

    context = RuntimeContext(
        user_id="alice",
        thread_id="thread_001",
        run_id="run_001",
        workspace_path=str(own_workspace),
        record_event=None,
        save_checkpoint=None,
    )

    result = asyncio.run(
        ReadFileTool().execute(call, context)
    )

    # 必须失败，且绝不能把 secret.txt 的内容返回。
    assert result.is_error is True
    assert "超出了当前 Thread 的指定目录" in result.content
    assert "这是不应被读取的秘密" not in result.content
