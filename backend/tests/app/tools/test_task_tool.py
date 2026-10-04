"""task 的模型接口只暴露已实现能力，后台身份不能由模型伪造。"""

import asyncio
from copy import deepcopy

import pytest

from app.agents.prompts.builder import apply_prompt_template
from app.agents.middleware import MiddlewareManager
from app.agents.middleware_stack import build_runtime_middlewares
from app.domain.messages import ToolCall
from app.domain.threads import ThreadState
from app.domain.tools import ToolResult
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.task import TaskTool


class RecordingDispatcher:
    def __init__(self):
        self.tasks = []

    async def execute(self, task, context):
        self.tasks.append(task)
        return ToolResult(tool_call_id=task.tool_call_id, name="task", content="真实执行结果")


def invoke(arguments, *, bound=True):
    tool, dispatcher = TaskTool(), RecordingDispatcher()
    registry = ToolRegistry()
    registry.register(tool)
    if bound:
        tool.bind(dispatcher)
    context = RuntimeContext("owner", "thread-a", "run-a", "/tmp/workspace", None, None)
    call = ToolCall("call-a", "task", arguments)
    result = asyncio.run(MiddlewareManager(build_runtime_middlewares()).wrap_tool_call(
        ThreadState("thread-a", "owner"), context, call,
        lambda: ToolExecutor(registry).execute(call, context),
    ))
    return result, dispatcher


VALID = {"description": "读取一份报告", "prompt": "读取 report.txt 并提取日期", "subagent_type": "general-purpose"}


def test_task_parameters_and_backend_identity_are_separate():
    parameters = TaskTool.definition.parameters
    assert list(parameters["properties"]) == ["description", "prompt", "subagent_type"]
    assert parameters["required"] == list(parameters["properties"])
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["subagent_type"]["enum"] == ["general-purpose"]
    result, dispatcher = invoke(VALID)
    task = dispatcher.tasks[0]
    assert (task.user_id, task.thread_id, task.run_id, task.tool_call_id) == ("owner", "thread-a", "run-a", "call-a")
    assert task.description == VALID["description"] and task.prompt == VALID["prompt"]
    assert result.content == "真实执行结果" and result.tool_call_id == "call-a"


@pytest.mark.parametrize("arguments", [
    {}, {**VALID, "user_id": "someone-else"}, {**VALID, "run_id": "old-run"},
    {**VALID, "prompt": "  "}, {**VALID, "description": 123},
    {**VALID, "subagent_type": "bash"}, {**VALID, "subagent_type": "researcher"},
    ["not", "an", "object"],
])
def test_invalid_parameters_return_correlated_error_without_dispatch(arguments):
    result, dispatcher = invoke(deepcopy(arguments))
    assert result.role == "tool" and result.tool_call_id == "call-a"
    assert result.is_error and "出错" in result.content and not dispatcher.tasks


def test_unbound_tool_and_rebinding_are_rejected():
    result, dispatcher = invoke(VALID, bound=False)
    assert "尚未绑定" in result.content and not dispatcher.tasks
    tool = TaskTool()
    tool.bind(dispatcher)
    with pytest.raises(RuntimeError, match="跨 Run"):
        tool.bind(RecordingDispatcher())


@pytest.mark.parametrize("enabled", [False, True])
def test_delegation_rules_only_advertise_implemented_capabilities(enabled):
    prompt = apply_prompt_template(subagent_enabled=enabled)
    assert ("<subagent_system>" in prompt) is enabled
    if enabled:
        assert "up to 3 calls" in prompt and "MAXIMUM 6 `task` CALLS PER RUN" in prompt
        assert "Default to direct execution" in prompt
        assert "Inter-agent dependencies" in prompt and "Unsafe shared state" in prompt
    description = TaskTool.definition.description
    assert "general-purpose" in description
    for unsupported in ("custom_agents", "**bash**", "Skills", "skills", "present_files", "ask_clarification"):
        assert unsupported not in description
        assert unsupported not in prompt
